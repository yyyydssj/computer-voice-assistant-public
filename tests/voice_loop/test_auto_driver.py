"""P9 批次 2 集成红测：自动收音驱动（离线、无真实设备/模型/网络）。

两段验收重点：
- **正常自动结束必须提交**：静音/语音/静音超过 ``silence_ms``（或超过 ``max_utterance_ms``）
  判定结束后，``release()`` 必须走 coordinator 正常收尾，让 provider 解码已缓存语音并
  返回 accepted/submitted + 识别正文；
- **失败路径 fail closed**：检测器抛错/返回非布尔、未起始超时（no_speech）都不自动
  release，且 ``release()``/``cancel()`` 绝不提交或泄漏正文。

真实管道用例复用 P6 ``PushToTalkCoordinator`` + 离线合成 provider/采集；帧时长不可控的
场景（真实 SenseVoice adapter 只接受 320 采样/20ms 帧）用最小 stub coordinator 覆盖。
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from app.agent.adapter import AgentTurnOutcome
from app.asr import PushToTalkCoordinator, SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.asr.recovery import AsrFailureCode, AsrRecoveryState
from app.audio.bus import AudioBus
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceCheckResult,
    DeviceCheckStatus,
    DeviceSelection,
    FakeCaptureStream,
)
from app.audio.vad import VadConfig, VadSession
from app.audio.vad.auto_capture import SpeechStartConfig, SpeechStartDecision, SpeechStartDetector
from app.conversation import ConversationManager
from app.contracts import AudioFrame
from app.voice_loop.auto_driver import (
    STOP_COORDINATOR_NOT_READY,
    STOP_DETECTOR_FAILURE,
    STOP_MAX_UTTERANCE_DURATION,
    STOP_NO_SPEECH,
    STOP_SILENCE_TIMEOUT,
    AutoCaptureDriverConfig,
    AutoCaptureVoiceDriver,
    _ObservingVadSession,
)
from app.voice_loop.boundary import VoiceLoopError, VoiceTurnBoundary

VOICE_SESSION_ID = "voice-session-auto-1"
UTTERANCE_ID = "asr-utterance-auto-1"
TURN_ID = "asr-turn-auto-1"
REQUEST_ID = "asr-request-auto-1"
RECOGNIZED_TEXT = "打开编辑器"
REPLY_TEXT = f"已接收：{RECOGNIZED_TEXT}"


class _Adapter:
    """只记录调用、返回固定闲聊结果的离线 Conversation 替身。"""

    def __init__(self, text: str = RECOGNIZED_TEXT) -> None:
        self.text = text
        self.calls: list[tuple[str, str, str]] = []

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.calls.append((request_id, session_id, text))
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"已接收：{text}",
            finish_reason="stop",
        )

    def close(self) -> None:
        pass


class _CaptureBackend:
    """只提供 FakeCaptureStream 的离线采集后端。"""

    def __init__(self) -> None:
        self.stream = FakeCaptureStream()

    def check_device(self, selection, *, sample_rate_hz: int, channel_count: int) -> DeviceCheckResult:
        del selection, sample_rate_hz, channel_count
        return DeviceCheckResult(DeviceCheckStatus.READY, "mic-1", "ready")

    def open_input_stream(self, **kwargs: object) -> FakeCaptureStream:
        self.stream.callback = kwargs["callback"]
        return self.stream


class _ProviderStream:
    def __init__(self, text: str) -> None:
        self.result = type("Result", (), {"text": text})()

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        del sample_rate, samples


class _Recognizer:
    def __init__(self, text: str) -> None:
        self.text = text

    def create_stream(self) -> _ProviderStream:
        return _ProviderStream(self.text)

    def decode(self, stream: _ProviderStream) -> None:
        del stream


class _OfflineRecognizer:
    recognizer = _Recognizer(RECOGNIZED_TEXT)

    @classmethod
    def from_sense_voice(cls, **kwargs: object) -> _Recognizer:
        del kwargs
        return cls.recognizer


class _NonBoolStartDetector(SpeechStartDetector):
    """故意返回非布尔 ``observed`` 的起始判定器；用于验证 fail closed。"""

    def observe(self, packet) -> SpeechStartDecision:
        del packet
        return SpeechStartDecision(
            observed="yes",  # type: ignore[arg-type]
            started=True,
            speech_frames=1,
            observed_frames=1,
        )


class _StubRecovery:
    """替身的恢复状态视图；只暴露驱动需要读取的 ``state``。"""

    def __init__(self, state: AsrRecoveryState = AsrRecoveryState.READY) -> None:
        self.state = state


class _StubCoordinator:
    """最小 coordinator 替身；按真实 coordinator 的方式使用驱动注入的 ``vad_factory``。

    它让测试可以喂入任意 ``duration_ms`` 的帧（真实 SenseVoice adapter 只接受
    320 采样/20ms 帧，无法覆盖帧时长台账场景）。默认**不提供** ``abandon_turn``，
    用于验证驱动的兼容回退路径。
    """

    def __init__(self, *, started: bool = True, frame_ms: int = 20, capacity: int = 256) -> None:
        self.started = started
        self.frame_ms = frame_ms
        self.start_calls = 0
        self.pump_calls = 0
        self.release_calls = 0
        self.failure_codes: list[object] = []
        self.release_kwargs: dict[str, object] = {}
        self.fed_frames = 0
        self.recovery = _StubRecovery()
        self._bus: AudioBus | None = None
        self._vad: object | None = None
        self._factory = None
        self._capacity = capacity
        self._next_event_sequence = 1
        self._next_frame_index = 0

    def start(self, asr_boundary) -> object:
        self.start_calls += 1
        bus = AudioBus(
            audio_session_id=asr_boundary.audio_session_id,
            provider_generation=asr_boundary.provider_generation,
            stream_id=asr_boundary.stream_id,
            device_epoch=1,
            capacity_frames=self._capacity,
        )
        self._bus = bus
        self._vad = self._factory(bus)
        self._vad.start()
        return type("Start", (), {"started": self.started, "reason": None, "diagnostic": None})()

    def feed(self, count: int, *, duration_ms: int | None = None, speech: bool = True) -> None:
        """向总线发布 ``count`` 帧；可逐次指定真实 ``duration_ms`` 与语音/静音。"""

        assert self._bus is not None
        for _ in range(count):
            current_ms = self.frame_ms if duration_ms is None else duration_ms
            sample_count = 16000 * current_ms // 1000
            if speech:
                payload = struct.pack(f"<{sample_count}h", *([2000] * sample_count))
            else:
                payload = bytes(sample_count * 2)
            frame = AudioFrame(
                audio_session_id=self._bus.audio_session_id,
                provider_generation=self._bus.provider_generation,
                stream_id=self._bus.stream_id,
                event_sequence=self._next_event_sequence,
                frame_index=self._next_frame_index,
                captured_at="2026-10-01T09:00:00.000Z",
                duration_ms=current_ms,
                device_epoch=self._bus.device_epoch,
                sample_rate_hz=16000,
                channel_count=1,
                sample_format="pcm_s16le",
                sample_count=sample_count,
                payload_size_bytes=len(payload),
            )
            decision = self._bus.publish(frame, payload)
            if decision.value != "accepted":
                raise AssertionError(f"总线拒绝了测试帧：{decision}")
            self._next_event_sequence += 1
            self._next_frame_index += 1
        self.fed_frames += count

    def pump(self) -> object:
        self.pump_calls += 1
        assert self._bus is not None and self._vad is not None
        processed = 0
        while True:
            packet = self._bus.read_nowait()
            if packet is None:
                break
            self._vad.process(packet)
            processed += 1
        return type("Pump", (), {"processed_frames": processed, "provider_failed": False})()

    def release(self, **kwargs: object) -> object:
        self.release_calls += 1
        self.release_kwargs = kwargs
        return type("Turn", (), {"gate_result": None, "outcome": None})()

    def runtime_failed(self, *, code) -> None:
        self.failure_codes.append(code)
        if self._vad is not None:
            self._vad.cancel()


class _StubCoordinatorWithAbandon(_StubCoordinator):
    """提供 ``abandon_turn`` 的替身；可配置返回值、恢复状态与异常。"""

    def __init__(
        self,
        *,
        abandon_result: object = True,
        ready_after: bool = True,
        abandon_error: Exception | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.abandon_calls: list[str] = []
        self._abandon_result = abandon_result
        self._ready_after = ready_after
        self._abandon_error = abandon_error

    def abandon_turn(self, *, reason: str) -> object:
        self.abandon_calls.append(reason)
        if self._abandon_error is not None:
            raise self._abandon_error
        self.recovery.state = (
            AsrRecoveryState.READY if self._ready_after else AsrRecoveryState.SESSION_FAILED_RUNTIME
        )
        return self._abandon_result


def build_coordinator(*, adapter: _Adapter, capture_config: CaptureConfig | None = None):
    """构造真实 P6 coordinator + 离线合成 provider/采集（不触设备、不联网）。"""

    module = type("Module", (), {"OfflineRecognizer": _OfflineRecognizer})()
    provider = SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
        module_loader=lambda: module,
    )
    provider.load()
    backends: list[_CaptureBackend] = []

    def capture_factory(bus: AudioBus) -> CaptureSession:
        backend = _CaptureBackend()
        backends.append(backend)
        return CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=capture_config or CaptureConfig(),
        )

    def default_vad_factory(bus: AudioBus) -> VadSession:
        # 真实按键路径的 VAD：本驱动必须把它替换成帧观察 VAD。
        return VadSession(
            bus=bus, config=VadConfig(silence_duration_ms=200), detector=lambda packet: True
        )

    coordinator = PushToTalkCoordinator(
        conversation=ConversationManager(adapter),
        provider=provider,
        capture_factory=capture_factory,
        vad_factory=default_vad_factory,
        source_instance_id="instance-1",
        device_epoch=1,
        audio_capacity_frames=256,
    )
    # ASR 就绪属于运行时启动前置条件，由真实入口在任何轮次前建立。
    coordinator.startup_succeeded()
    return coordinator, backends


def observing_vad(coordinator) -> _ObservingVadSession:
    """取当前轮次的帧观察 VAD；用于证明驱动确实看到了每一帧。"""

    vad = coordinator._vad  # noqa: SLF001 - 只读校验注入是否生效
    if not isinstance(vad, _ObservingVadSession):
        raise AssertionError("驱动未把自己的帧观察 VAD 注入 coordinator")
    return vad


def boundary(index: int = 1) -> VoiceTurnBoundary:
    """第 ``index`` 轮的轮次边界；同一标识不得跨轮复用（coordinator 会按陈旧边界拒绝）。"""

    suffix = str(index)
    return VoiceTurnBoundary(
        voice_session_id=f"{VOICE_SESSION_ID}-{suffix}",
        utterance_id=f"{UTTERANCE_ID}-{suffix}",
        turn_id=f"{TURN_ID}-{suffix}",
        request_id=f"{REQUEST_ID}-{suffix}",
        reply_id=f"asr-reply-auto-1-{suffix}",
        source="asr",
        created_at="2026-10-01T09:00:00.000Z",
    )


def frame_builder(duration_ms: int = 20, sample_rate_hz: int = 16000):
    sample_count = sample_rate_hz * duration_ms // 1000
    loud = struct.pack(f"<{sample_count}h", *([2000] * sample_count))
    quiet = bytes(sample_count * 2)

    def emit(backend: _CaptureBackend, *, speech: bool, frames: int = 1) -> None:
        payload = loud if speech else quiet
        for _ in range(frames):
            backend.stream.emit(payload, frames=sample_count)

    return emit


def make_driver(coordinator, **overrides: object) -> AutoCaptureVoiceDriver:
    """构造驱动；coordinator 的 VAD 工厂由驱动自己在 start() 时注入。"""

    return AutoCaptureVoiceDriver(coordinator=coordinator, **overrides)  # type: ignore[arg-type]


def build_pipeline(*, capture_config: CaptureConfig | None = None, **overrides: object):
    adapter = _Adapter()
    coordinator, backends = build_coordinator(adapter=adapter, capture_config=capture_config)
    driver = make_driver(coordinator, **overrides)
    if not driver.start(boundary()):
        raise AssertionError(f"驱动未能启动：{driver.last_start_reason}")
    return adapter, coordinator, backends[-1], driver


def build_stub_pipeline(*, frame_ms: int = 20, **overrides: object):
    """驱动 + 帧时长可控的 stub coordinator（走 coordinator_factory 注入路径）。"""

    coordinator = _StubCoordinator(frame_ms=frame_ms)

    def factory(vad_factory):
        coordinator._factory = vad_factory  # noqa: SLF001 - 记录驱动注入的 VAD 工厂
        return coordinator

    driver = AutoCaptureVoiceDriver(coordinator_factory=factory, **overrides)  # type: ignore[arg-type]
    if not driver.start(boundary()):
        raise AssertionError("驱动未能启动")
    return coordinator, driver


def build_coordinator_driver(coordinator: object, **overrides: object) -> AutoCaptureVoiceDriver:
    """在给定替身上构造驱动（走 coordinator_factory 路径，替身无需 _vad_factory）。"""

    def factory(vad_factory):
        coordinator._factory = vad_factory  # type: ignore[attr-defined]  # noqa: SLF001
        return coordinator

    return AutoCaptureVoiceDriver(coordinator_factory=factory, **overrides)  # type: ignore[arg-type]


class AutoCaptureDriverConfigTests(unittest.TestCase):
    def test_defaults_match_the_frozen_contract(self) -> None:
        config = AutoCaptureDriverConfig()
        self.assertTrue(config.auto_release)
        self.assertEqual(config.silence_ms, 1200)
        self.assertEqual(config.speech_start_timeout_ms, 8000)
        self.assertEqual(config.max_utterance_ms, 30000)
        self.assertEqual(config.speech_threshold, 600.0)

    def test_invalid_values_fail_closed_with_value_error(self) -> None:
        cases = [
            {"auto_release": "yes"},
            {"silence_ms": 0},
            {"silence_ms": -1},
            {"silence_ms": True},
            {"speech_start_timeout_ms": 0},
            {"max_utterance_ms": 0},
            {"speech_threshold": 0},
            {"speech_threshold": -1.0},
            {"speech_threshold": float("nan")},
            {"speech_threshold": True},
            {"speech_threshold": "loud"},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    AutoCaptureDriverConfig(**case)  # type: ignore[arg-type]


class AutoCaptureDriverRefusalTests(unittest.TestCase):
    def test_coordinator_or_factory_is_required(self) -> None:
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver()

    def test_coordinator_and_factory_are_mutually_exclusive(self) -> None:
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver(coordinator=object(), coordinator_factory=lambda vad: None)

    def test_coordinator_shape_is_validated(self) -> None:
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver(coordinator=object())

    def test_start_detector_must_be_a_speech_start_detector(self) -> None:
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), start_detector=object())

    def test_bad_identity_and_config_types_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), audio_session_id="")
        with self.assertRaises(ValueError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), stream_id="")
        with self.assertRaises(ValueError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), provider_generation=0)
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), config="cfg")
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), speech_handled_by_driver="yes")
        with self.assertRaises(TypeError):
            AutoCaptureVoiceDriver(coordinator=_StubCoordinator(), energy_detector="loud")

    def test_pump_without_start_returns_zero_and_release_raises(self) -> None:
        driver = AutoCaptureVoiceDriver(coordinator=_StubCoordinator())
        self.assertEqual(driver.pump(), 0)
        self.assertIsNone(driver.last_pump)
        self.assertIsNone(driver.last_stop_reason)
        self.assertFalse(driver.auto_released)
        self.assertFalse(driver.speech_started)
        with self.assertRaises(VoiceLoopError):
            driver.release()
        with self.assertRaises(VoiceLoopError):
            driver.cancel(reason="cancelled")
        with self.assertRaises(ValueError):
            driver.cancel(reason="  ")

    def test_start_is_refused_while_a_turn_is_running(self) -> None:
        adapter = _Adapter()
        coordinator, _backends = build_coordinator(adapter=adapter)
        driver = make_driver(coordinator)
        self.assertTrue(driver.start(boundary()))
        with self.assertRaises(VoiceLoopError):
            driver.start(boundary())
        driver.cancel(reason="cancelled")
        self.assertEqual(adapter.calls, [])


class AutoCaptureDriverAutoReleaseTests(unittest.TestCase):
    """正常自动结束（silence_timeout / max_utterance_duration）必须提交一次。"""

    def test_silence_timeout_turn_is_decoded_and_submitted(self) -> None:
        config = AutoCaptureDriverConfig(silence_ms=1200, speech_start_timeout_ms=8000)
        adapter, coordinator, backend, driver = build_pipeline(config=config)
        observing = observing_vad(coordinator)
        emit = frame_builder(20)

        # 起始前静音 → 语音 → 静音超过 silence_ms。
        emit(backend, speech=False, frames=5)
        self.assertEqual(driver.pump(), 5)
        self.assertFalse(driver.speech_started)
        self.assertFalse(driver.auto_released)

        emit(backend, speech=True, frames=3)
        self.assertEqual(driver.pump(), 3)
        self.assertTrue(driver.speech_started)
        self.assertFalse(driver.auto_released)

        emit(backend, speech=False, frames=60)  # 60 * 20ms = 1200ms = silence_ms
        self.assertEqual(driver.pump(), 60)
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        self.assertEqual(driver.frames_seen, 68)
        self.assertEqual(observing.seen_frames, 68)
        # 已判定结束的轮次不再推进帧。
        self.assertEqual(driver.pump(), 0)

        result = driver.release()
        # 自动结束 = 输入边界正常结束：provider 解码已缓存语音并提交一次。
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertTrue(result.speech_eligible)
        self.assertEqual(result.accepted_text, RECOGNIZED_TEXT)
        self.assertEqual(result.reply_text, REPLY_TEXT)
        self.assertEqual(result.reason, "accepted_final")
        self.assertEqual([call[2] for call in adapter.calls], [RECOGNIZED_TEXT])
        # 非路由路径（routing_enabled=False）沿用 ConversationManager 自有的 request_id。
        self.assertTrue(adapter.calls[0][0])
        self.assertIs(driver.last_result, result)

    def test_max_utterance_duration_turn_is_decoded_and_submitted(self) -> None:
        config = AutoCaptureDriverConfig(
            silence_ms=600000, speech_start_timeout_ms=8000, max_utterance_ms=400
        )
        adapter, coordinator, backend, driver = build_pipeline(config=config)
        observing = observing_vad(coordinator)
        emit = frame_builder(20)

        emit(backend, speech=True, frames=20)  # 20 * 20ms = 400ms = max_utterance_ms
        self.assertEqual(driver.pump(), 20)
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_MAX_UTTERANCE_DURATION)
        self.assertEqual(driver.frames_seen, 20)
        self.assertEqual(observing.seen_frames, 20)

        result = driver.release()
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertEqual(result.accepted_text, RECOGNIZED_TEXT)
        self.assertEqual([call[2] for call in adapter.calls], [RECOGNIZED_TEXT])
        self.assertEqual(coordinator.state.value, "idle")

    def test_frame_duration_ledger_handles_non_divisible_durations(self) -> None:
        # 先大量短帧（20ms）再长时间帧（300ms）：若按“总时长 // 帧数”反推帧时长，
        # 平均帧时长会被大量短帧稀释（3200 // 104 = 30ms），4 帧长静音根本判不出结束。
        coordinator, driver = build_stub_pipeline(
            config=AutoCaptureDriverConfig(
                silence_ms=1200, speech_start_timeout_ms=8000, max_utterance_ms=60000
            )
        )
        coordinator.feed(3, duration_ms=20, speech=True)
        self.assertEqual(driver.pump(), 3)
        self.assertTrue(driver.speech_started)

        coordinator.feed(97, duration_ms=20, speech=True)
        self.assertEqual(driver.pump(), 97)
        self.assertEqual(driver.frames_seen, 100)
        self.assertFalse(driver.auto_released)

        coordinator.feed(3, duration_ms=300, speech=False)
        self.assertEqual(driver.pump(), 3)
        self.assertFalse(driver.auto_released)  # 3 * 300 = 900 < 1200

        coordinator.feed(1, duration_ms=300, speech=False)
        self.assertEqual(driver.pump(), 1)
        self.assertTrue(driver.auto_released)  # 4 * 300 = 1200 → 必须结束
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        self.assertEqual(driver.frames_seen, 104)

    def test_speech_start_timeout_does_not_auto_release(self) -> None:
        config = AutoCaptureDriverConfig(speech_start_timeout_ms=1000, silence_ms=600000)
        adapter, coordinator, backend, driver = build_pipeline(config=config)
        emit = frame_builder(20)
        emit(backend, speech=False, frames=50)

        self.assertEqual(driver.pump(), 50)
        self.assertFalse(driver.auto_released)
        self.assertFalse(driver.speech_started)
        self.assertEqual(driver.last_stop_reason, STOP_NO_SPEECH)
        self.assertEqual(driver.frames_seen, 50)
        # 未起始超时不自动 release：由调用方取消本轮，且不提交任何内容。
        driver.cancel(reason="cancelled")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(coordinator.state.value, "idle")

    def test_auto_release_disabled_never_auto_ends(self) -> None:
        config = AutoCaptureDriverConfig(
            auto_release=False, silence_ms=1000, speech_start_timeout_ms=1000
        )
        adapter, coordinator, backend, driver = build_pipeline(config=config)
        emit = frame_builder(20)
        emit(backend, speech=True, frames=3)
        self.assertEqual(driver.pump(), 3)
        emit(backend, speech=False, frames=100)
        self.assertEqual(driver.pump(), 100)

        self.assertFalse(driver.auto_released)
        self.assertIsNone(driver.last_stop_reason)
        self.assertEqual(driver.frames_seen, 103)
        self.assertEqual(observing_vad(coordinator).seen_frames, 103)
        # 手动路径仍可提交一次。
        result = driver.release()
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertEqual(result.accepted_text, RECOGNIZED_TEXT)
        self.assertEqual([call[2] for call in adapter.calls], [RECOGNIZED_TEXT])

    def test_energy_detector_sees_every_frame_seen_by_the_vad(self) -> None:
        seen: list[bytes] = []

        def detector(packet) -> bool:
            seen.append(bytes(packet.payload))
            return True

        adapter, coordinator, backend, driver = build_pipeline(energy_detector=detector)
        emit = frame_builder(20)
        emit(backend, speech=True, frames=3)

        self.assertEqual(driver.pump(), 3)
        self.assertTrue(driver.speech_started)
        self.assertEqual(driver.frames_seen, 3)
        self.assertEqual(len(seen), 3)
        # 驱动在 start() 里把自己的帧观察 VAD 注入 coordinator（不修改既有文件）。
        observing = observing_vad(coordinator)
        self.assertEqual(observing.seen_frames, 3)
        # 驱动注入的 VAD 只做转发：每帧恒判有语音，静音计数恒为 0，绝不自行结束轮次。
        self.assertEqual(observing.silence_frames, 0)
        self.assertFalse(driver.auto_released)
        self.assertEqual(adapter.calls, [])
        driver.cancel(reason="cancelled")

    def test_speech_started_lifecycle_resets_on_a_new_turn(self) -> None:
        adapter, coordinator, backend, driver = build_pipeline()
        emit = frame_builder(20)
        self.assertFalse(driver.speech_started)
        self.assertIsInstance(driver.config, AutoCaptureDriverConfig)
        self.assertIsNone(driver.last_stop_reason)
        self.assertIsNone(driver.last_result)

        emit(backend, speech=True, frames=3)
        driver.pump()
        self.assertTrue(driver.speech_started)

        emit(backend, speech=False, frames=100)
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        driver.release()
        self.assertTrue(driver.last_result.accepted)

        # 新一轮（新的 coordinator + 新的驱动实例）必须清零全部轮次事实。
        _adapter2, coordinator2, backend2, driver2 = build_pipeline()
        self.assertFalse(driver2.speech_started)
        self.assertFalse(driver2.auto_released)
        self.assertIsNone(driver2.last_stop_reason)
        self.assertIsNone(driver2.last_result)
        emit2 = frame_builder(20)
        emit2(backend2, speech=True, frames=3)
        driver2.pump()
        self.assertTrue(driver2.speech_started)
        driver2.cancel(reason="cancelled")
        del coordinator, coordinator2
        # 第一轮自动结束并提交了一次；第二轮只起始后被取消，不产生第二次提交。
        self.assertEqual([call[2] for call in adapter.calls], [RECOGNIZED_TEXT])


class AutoCaptureDriverChannelRecoveryTests(unittest.TestCase):
    """真机缺陷回归：一次“没人说话就丢弃”不得锁死后续轮次。"""

    def test_three_consecutive_turns_after_a_discard(self) -> None:
        config = AutoCaptureDriverConfig(silence_ms=1200, speech_start_timeout_ms=1000)
        adapter = _Adapter()
        coordinator, backends = build_coordinator(adapter=adapter)
        driver = make_driver(coordinator, config=config)
        emit = frame_builder(20)

        # 第 1 轮：没人说话 → no_speech → 调用方取消（abandon，不产生 ASR 故障）。
        self.assertTrue(driver.start(boundary(1)))
        emit(backends[-1], speech=False, frames=50)  # 1000ms = speech_start_timeout_ms
        self.assertEqual(driver.pump(), 50)
        self.assertEqual(driver.last_stop_reason, STOP_NO_SPEECH)
        self.assertFalse(driver.auto_released)
        driver.cancel(reason="cancelled")
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)
        self.assertEqual(coordinator.state.value, "idle")

        # 第 2 轮：正常说话 → 静音自动结束 → release 必须真正提交。
        self.assertTrue(driver.start(boundary(2)))
        emit(backends[-1], speech=True, frames=3)
        self.assertEqual(driver.pump(), 3)
        self.assertTrue(driver.speech_started)
        emit(backends[-1], speech=False, frames=60)  # 1200ms = silence_ms
        self.assertEqual(driver.pump(), 60)
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        result = driver.release()
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertEqual(result.accepted_text, RECOGNIZED_TEXT)
        self.assertEqual(coordinator.recovery.state, AsrRecoveryState.READY)

        # 第 3 轮：再次丢弃，仍可立刻开始下一轮。
        self.assertTrue(driver.start(boundary(3)))
        emit(backends[-1], speech=False, frames=50)
        self.assertEqual(driver.pump(), 50)
        self.assertEqual(driver.last_stop_reason, STOP_NO_SPEECH)
        driver.cancel(reason="cancelled")
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)

        # 第 4 轮（额外确认闩锁没有误置位）：仍能 start。
        self.assertTrue(driver.start(boundary(4)))
        self.assertNotEqual(driver.last_start_reason, STOP_COORDINATOR_NOT_READY)
        driver.cancel(reason="cancelled")
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)

        # 只有第 2 轮提交过一次。
        self.assertEqual([call[2] for call in adapter.calls], [RECOGNIZED_TEXT])

    def test_asr_failure_still_locks_the_channel_and_refuses_the_next_start(self) -> None:
        def boom(packet) -> bool:
            del packet
            raise RuntimeError("detector exploded")

        config = AutoCaptureDriverConfig(silence_ms=1200, speech_start_timeout_ms=1000)
        adapter = _Adapter()
        coordinator, backends = build_coordinator(adapter=adapter)
        driver = make_driver(coordinator, config=config, energy_detector=boom)
        emit = frame_builder(20)

        self.assertTrue(driver.start(boundary()))
        emit(backends[-1], speech=True, frames=1)
        driver.pump()
        self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)

        driver.cancel(reason="cancelled")
        # 真实故障仍然走 runtime_failed：通道进入 SESSION_FAILED_RUNTIME（fail closed 不被削弱）。
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.SESSION_FAILED_RUNTIME)
        self.assertFalse(driver.start(boundary()))
        self.assertIsNotNone(driver.last_start_reason)
        self.assertEqual(adapter.calls, [])

    def test_cancel_without_abandon_turn_falls_back_to_runtime_failed(self) -> None:
        coordinator = _StubCoordinator()
        driver = build_coordinator_driver(coordinator)
        self.assertTrue(driver.start(boundary()))
        driver.cancel(reason="cancelled")
        # 旧替身没有 abandon_turn：保持既有 runtime_failed 回退行为。
        self.assertEqual(coordinator.failure_codes, [AsrFailureCode.CANCELLED])

    def test_abandon_with_abandon_turn_avoids_the_asr_failure_path(self) -> None:
        coordinator = _StubCoordinatorWithAbandon()
        driver = build_coordinator_driver(coordinator)
        self.assertTrue(driver.start(boundary(1)))
        driver.cancel(reason="cancelled")
        self.assertEqual(coordinator.abandon_calls, ["cancelled"])
        self.assertEqual(coordinator.failure_codes, [])
        self.assertIs(driver.last_stop_reason, None)
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)
        # 下一轮可以立刻开始（没有闩锁）。
        self.assertTrue(driver.start(boundary(2)))
        driver.cancel(reason="cancelled")
        self.assertEqual(coordinator.abandon_calls, ["cancelled", "cancelled"])

    def test_abandon_that_does_not_restore_ready_latches_fail_closed(self) -> None:
        coordinator = _StubCoordinatorWithAbandon(abandon_result=False, ready_after=False)
        driver = build_coordinator_driver(coordinator)
        self.assertTrue(driver.start(boundary()))
        driver.cancel(reason="cancelled")

        self.assertEqual(coordinator.abandon_calls, ["cancelled"])
        self.assertEqual(coordinator.failure_codes, [])
        self.assertEqual(driver.last_stop_reason, STOP_COORDINATOR_NOT_READY)
        # 不伪造恢复：后续 start 一律失败。
        self.assertFalse(driver.start(boundary()))
        self.assertEqual(driver.last_start_reason, STOP_COORDINATOR_NOT_READY)
        self.assertFalse(driver.start(boundary()))
        self.assertEqual(driver.last_start_reason, STOP_COORDINATOR_NOT_READY)

    def test_abandon_exception_latches_fail_closed(self) -> None:
        coordinator = _StubCoordinatorWithAbandon(abandon_error=RuntimeError("abandon failed"))
        driver = build_coordinator_driver(coordinator)
        self.assertTrue(driver.start(boundary()))
        driver.cancel(reason="cancelled")

        self.assertEqual(driver.last_stop_reason, STOP_COORDINATOR_NOT_READY)
        self.assertFalse(driver.start(boundary()))
        self.assertEqual(driver.last_start_reason, STOP_COORDINATOR_NOT_READY)

    def test_no_speech_release_uses_abandon_instead_of_runtime_failed(self) -> None:
        config = AutoCaptureDriverConfig(silence_ms=1200, speech_start_timeout_ms=1000)
        adapter, coordinator, backend, driver = build_pipeline(config=config)
        emit = frame_builder(20)
        emit(backend, speech=False, frames=50)
        self.assertEqual(driver.pump(), 50)
        self.assertEqual(driver.last_stop_reason, STOP_NO_SPEECH)

        result = driver.release()
        self.assertFalse(result.accepted)
        self.assertFalse(result.submitted)
        self.assertEqual(result.reason, STOP_NO_SPEECH)
        self.assertEqual(adapter.calls, [])
        # 丢弃路径同样不产生 ASR 故障：恢复状态仍可开下一轮。
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)
        self.assertTrue(driver.start(boundary(2)))
        driver.cancel(reason="cancelled")


class AutoCaptureDriverDetectorFailureTests(unittest.TestCase):
    def test_detector_exception_fails_closed_without_releasing(self) -> None:
        def boom(packet) -> bool:
            del packet
            raise RuntimeError(r"secret payload G:\private\voice.pcm")

        adapter, coordinator, backend, driver = build_pipeline(energy_detector=boom)
        observing = observing_vad(coordinator)
        emit = frame_builder(20)
        emit(backend, speech=True, frames=1)

        driver.pump()
        self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)
        self.assertFalse(driver.auto_released)
        self.assertFalse(driver.speech_started)
        self.assertEqual(driver.frames_seen, 0)
        self.assertEqual(observing.seen_frames, 1)

        result = driver.release()
        self.assertFalse(result.accepted)
        self.assertFalse(result.submitted)
        self.assertFalse(result.speech_eligible)
        self.assertIsNone(result.accepted_text)
        self.assertIsNone(result.reply_text)
        self.assertEqual(result.reason, STOP_DETECTOR_FAILURE)
        # 异常正文不入结果、不入会话：既不提交也不泄漏。
        self.assertEqual(adapter.calls, [])
        self.assertEqual(driver.pump(), 0)

    def test_non_boolean_energy_result_fails_closed(self) -> None:
        adapter, _coordinator, backend, driver = build_pipeline(energy_detector=lambda packet: 0)
        emit = frame_builder(20)
        emit(backend, speech=True, frames=1)
        driver.pump()

        self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)
        self.assertFalse(driver.auto_released)
        result = driver.release()
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, STOP_DETECTOR_FAILURE)
        self.assertEqual(adapter.calls, [])

    def test_non_boolean_start_decision_fails_closed(self) -> None:
        adapter, _coordinator, backend, driver = build_pipeline(
            start_detector=_NonBoolStartDetector(config=SpeechStartConfig())
        )
        emit = frame_builder(20)
        emit(backend, speech=True, frames=1)
        driver.pump()

        self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)
        self.assertFalse(driver.auto_released)
        result = driver.release()
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, STOP_DETECTOR_FAILURE)
        self.assertEqual(adapter.calls, [])

    def test_injected_start_detector_is_used_and_reset_per_turn(self) -> None:
        detector = SpeechStartDetector(config=SpeechStartConfig())
        adapter, _coordinator, backend, driver = build_pipeline(start_detector=detector)
        self.assertIs(driver._start_detector, detector)  # noqa: SLF001 - 验证注入生效
        emit = frame_builder(20)
        # 注入的检测器未启用内部能量门槛：静音帧同样计为命中并确认起始。
        emit(backend, speech=False, frames=3)
        driver.pump()
        self.assertTrue(detector.started)
        self.assertTrue(driver.speech_started)

        # 下一轮（新的 coordinator + 新的驱动实例）复用同一检测器并重新开始计数。
        adapter2, _coordinator2, backend2, driver2 = build_pipeline(start_detector=detector)
        self.assertIs(driver2._start_detector, detector)  # noqa: SLF001
        self.assertFalse(driver2.speech_started)
        self.assertFalse(detector.started)
        self.assertEqual(detector.observed_frames, 0)
        emit(backend, speech=False, frames=1)
        driver.cancel(reason="cancelled")
        driver2.cancel(reason="cancelled")
        del backend2, adapter2
        self.assertEqual(adapter.calls, [])


class SpeechMuteWindowTests(unittest.TestCase):
    """P10 · T1 #13：播报期/播报后的输入静默窗（默认 0 = 行为逐字节不变）。

    口径：窗口内**只计数、不判语音起始**（避免麦克风把本机 TTS 尾音当成用户语音的开头）。
    """

    def test_default_config_has_no_mute_window(self) -> None:
        config = AutoCaptureDriverConfig()
        self.assertEqual(config.speech_mute_gap_ms, 0)
        adapter, _coordinator, backend, driver = build_pipeline()
        self.assertFalse(driver.speech_mute_active)
        self.assertEqual(
            driver.mute_details(),
            {
                "active": False,
                "muted_frames": 0,
                "window_muted_frames": 0,
                "reason": None,
                "gap_ms": 0,
            },
        )
        emit = frame_builder(20)
        emit(backend, speech=True, frames=3)
        driver.pump()
        self.assertTrue(driver.speech_started)     # 默认路径照旧
        self.assertEqual(driver.muted_frames, 0)
        driver.cancel(reason="cancelled")
        self.assertEqual(adapter.calls, [])

    def test_playback_mute_blocks_start_and_counts_frames(self) -> None:
        _adapter, coordinator, backend, driver = build_pipeline()
        observing = observing_vad(coordinator)
        emit = frame_builder(20)
        driver.begin_speech_mute()
        self.assertTrue(driver.speech_mute_active)
        self.assertEqual(driver.mute_details()["reason"], "playback_mute")
        emit(backend, speech=True, frames=3)
        driver.pump()
        self.assertFalse(driver.speech_started)          # 窗口内不判起始
        self.assertEqual(driver.muted_frames, 3)         # 但如实计数
        self.assertEqual(observing.seen_frames, 3)       # 帧仍流经总线，只是不参与判定
        detector = driver._start_detector  # noqa: SLF001 - 只读校验
        assert detector is not None
        self.assertEqual(detector.observed_frames, 0)

        driver.end_speech_mute(now=1000.0)               # gap=0 ⇒ 立即恢复
        self.assertFalse(driver.speech_mute_active)
        emit(backend, speech=True, frames=3)
        driver.pump()
        self.assertTrue(driver.speech_started)
        driver.cancel(reason="cancelled")

    def test_muted_frame_count_does_not_leak_into_the_next_turn(self) -> None:
        """#13 修正（真机证据：turn 14/15 重复上报 34）：帧数按**轮**归属，窗口累计另记。"""

        _adapter, _coordinator, backend, driver = build_pipeline()
        emit = frame_builder(20)
        driver.begin_speech_mute()
        emit(backend, speech=True, frames=2)
        driver.pump()
        self.assertEqual(driver.muted_frames, 2)
        self.assertEqual(driver.mute_details()["window_muted_frames"], 2)
        driver.end_speech_mute(now=1000.0)
        driver.release()

        self.assertTrue(driver.start(boundary(2)))
        self.assertEqual(driver.muted_frames, 0)                              # 本轮：不再继承
        self.assertEqual(driver.mute_details()["window_muted_frames"], 2)     # 窗口累计：保留
        driver.cancel(reason="cancelled")

    def test_post_speech_gap_expires_with_the_clock(self) -> None:
        _adapter, _coordinator, backend, driver = build_pipeline(
            config=AutoCaptureDriverConfig(speech_mute_gap_ms=800)
        )
        emit = frame_builder(20)
        with mock.patch("app.voice_loop.auto_driver.time.monotonic", return_value=1000.0):
            driver.end_speech_mute(now=1000.0)
            self.assertTrue(driver.speech_mute_active)
            self.assertEqual(driver.mute_details()["reason"], "post_speech_mute")
        with mock.patch("app.voice_loop.auto_driver.time.monotonic", return_value=1000.5):
            self.assertTrue(driver.speech_mute_active)   # 窗口内
        with mock.patch("app.voice_loop.auto_driver.time.monotonic", return_value=1000.9):
            self.assertFalse(driver.speech_mute_active)  # 过期即自动结束
        self.assertEqual(driver.mute_details()["reason"], None)
        emit(backend, speech=True, frames=3)
        driver.pump()
        self.assertTrue(driver.speech_started)
        driver.cancel(reason="cancelled")

    def test_mute_argument_validation(self) -> None:
        with self.assertRaises(ValueError):
            AutoCaptureDriverConfig(speech_mute_gap_ms=-1)
        with self.assertRaises(ValueError):
            AutoCaptureDriverConfig(speech_mute_gap_ms=True)
        driver = AutoCaptureVoiceDriver(coordinator=_StubCoordinator())
        with self.assertRaises(ValueError):
            driver.begin_speech_mute(reason="")
        with self.assertRaises(ValueError):
            driver.end_speech_mute(now="now")
        with self.assertRaises(ValueError):
            driver.end_speech_mute(now=True)

    def test_start_counter_reset_failure_never_breaks_the_window(self) -> None:
        """附加保护（静默窗）绝不允许把正常轮次判成故障：没有 ``reset()`` / reset 抛错都静默跳过。"""

        driver = make_driver(_StubCoordinator())

        class _Boom:
            def reset(self) -> None:
                raise RuntimeError("boom")

        for replacement in (object(), _Boom()):
            with self.subTest(detector=type(replacement).__name__):
                driver._start_detector = replacement  # noqa: SLF001 - 只读校验容错路径
                driver.begin_speech_mute()
                driver.end_speech_mute(now=1000.0)
                self.assertFalse(driver.speech_mute_active)
                self.assertFalse(driver._detector_failed)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
