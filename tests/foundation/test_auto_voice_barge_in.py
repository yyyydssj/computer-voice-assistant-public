"""P10 R4：会话循环接线（``app/runtime/auto_voice.py``）的离线契约测试。

覆盖：默认全关零行为变化、独立打断通道的线程与音量语义、安全门在真实接线上的效果、
设备策略接线（安全空闲点 / 各试一次 / 回退文字）、以及一轮真实 ``run_turn`` 的回归。

全部离线：假采集、假 speaker、假 provider 模块；不打开任何真实设备、不联网。
"""

from __future__ import annotations

import ast
import array
import io
import math
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceCheckResult,
    DeviceCheckStatus,
    DeviceSelection,
)
from app.conversation import ConversationManager
from app.contracts.audio import AudioFrame
from app.runtime.assistant_runtime import LocalChatRouter
from app.runtime.auto_voice import (
    AEC_REFERENCE_MODES,
    BARGE_IN_MODES,
    CONFIRM_SUPPRESSED_REASON,
    DEVICE_RECOVERY_MODES,
    DUCK_UNAVAILABLE_REASON,
    PRE_ROLL_MERGE_UNSUPPORTED,
    AutoVoiceConfig,
    AutoVoiceSession,
)
from app.runtime.voice_entry import VoiceEntryEnvironment
from app.tts.waveout_pcm import PlaybackGain
from tests.duplex.test_barge_in_channel import (
    CAPTURE_FRAME_BYTES,
    CAPTURE_SAMPLE_RATE_HZ,
    speech_payload,
)
from tests.integration.test_p6_integration import _OfflineRecognizer, _Recognizer
from tests.integration.test_p9_auto_runner import (
    FAKE_RECOGNIZED_TEXT,
    _paced_run_turn,
    build_environment,
)

AUTO_VOICE_PATH = Path(__file__).resolve().parents[2] / "app" / "runtime" / "auto_voice.py"
FRAME_MS = 20
RECOGNIZED = "你好，测试一下"


# --------------------------------------------------------------------- 替身


class ChannelCapture:
    """既给轮次采集也给打断通道用的假采集会话（帧由测试按需推进）。"""

    def __init__(self, bus: object, *, started: bool = True) -> None:
        self.bus = bus
        self._started = started
        self._event_sequence = 1
        self._frame_index = 0
        self.stopped: tuple[str, bool] | None = None
        self.starts = 0

    def start(self) -> SimpleNamespace:
        self.starts += 1
        return SimpleNamespace(started=self._started)

    def stop(self, *, reason: str, discard: bool) -> None:
        self.stopped = (reason, discard)

    def push(self, payload: bytes) -> object:
        frame = AudioFrame(
            audio_session_id=self.bus.audio_session_id,  # type: ignore[attr-defined]
            provider_generation=self.bus.provider_generation,  # type: ignore[attr-defined]
            stream_id=self.bus.stream_id,  # type: ignore[attr-defined]
            event_sequence=self._event_sequence,
            frame_index=self._frame_index,
            captured_at="2026-10-02T00:00:00Z",
            duration_ms=FRAME_MS,
            device_epoch=self.bus.device_epoch,  # type: ignore[attr-defined]
            sample_rate_hz=CAPTURE_SAMPLE_RATE_HZ,
            sample_count=CAPTURE_SAMPLE_RATE_HZ * FRAME_MS // 1000,
            payload_size_bytes=CAPTURE_FRAME_BYTES,
        )
        self._event_sequence += 1
        self._frame_index += 1
        return self.bus.publish(frame, payload)  # type: ignore[attr-defined]


class DeadBackend:
    """设备不可用的假后端：采集永远起不来（用于"不打断"和快速收尾）。"""

    def check_device(self, selection, *, sample_rate_hz, channel_count):
        del selection, sample_rate_hz, channel_count
        return DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, None, "no device")

    def open_input_stream(self, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("设备不可用时不得打开采集流")


class FakeStream:
    def __init__(self, *, fail: bool = False) -> None:
        self.chunks: list[str] = []
        self.finished = 0
        self.cancelled = 0
        self._fail = fail

    def push_text_chunk(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("tts failed")
        self.chunks.append(text)

    def finish(self) -> None:
        if self._fail:
            raise RuntimeError("tts failed")
        self.finished += 1

    def cancel(self) -> None:
        self.cancelled += 1


class FakeSpeaker:
    def __init__(self, *, fail: bool = False, start_none: bool = False) -> None:
        self.streams: list[FakeStream] = []
        self.records: list[dict] = []
        self._fail = fail
        self._start_none = start_none

    def start_stream(self):
        if self._start_none:
            return None
        stream = FakeStream(fail=self._fail)
        self.streams.append(stream)
        self.records.append({"route": "fake_speaker"})
        return stream


class _OkWindow:
    """可用参考窗口替身（全零参考 → 相关性 0.0，不影响人声得分）。"""

    def __init__(self, samples: int = 320) -> None:
        import numpy as np

        self.state = "ok"
        self.pcm = np.zeros(samples, dtype=np.float32)

    @property
    def ok(self) -> bool:
        return True


class RecordingGain:
    """记录每一次音量写入的增益替身（用于断言"确实降过音量"）。"""

    def __init__(self, initial: float = 1.0) -> None:
        self.value = initial
        self.values: list[float] = []

    def read(self) -> float:
        return self.value

    def set(self, level: float) -> float:
        self.value = float(level)
        self.values.append(float(level))
        return self.value


class UsableReference:
    """与采集**同钟**、非静音且与采集不相关的参考替身（唯一允许降音量的条件）。

    R4-A 之后"可用"意味着能算出**对齐后的相关性**：必须提供最近约 1.6 s 的
    非静音参考帧（``timeline_frames()``），只给一个"同钟 pcm"是不够的。
    """

    def __init__(
        self, *, sample_rate: int = CAPTURE_SAMPLE_RATE_HZ, frames: int = 80, seed: int = 11
    ) -> None:
        import numpy as np

        rng = np.random.default_rng(seed)
        start = int(time.monotonic() * 1000.0)
        self._frames = tuple(
            (start + index * 20, rng.normal(0, 0.05, 320).astype(np.float32))
            for index in range(frames)
        )
        self.pcm = SimpleNamespace(sample_rate=sample_rate)

    def window(self, capture_at_ms: int) -> _OkWindow:
        del capture_at_ms
        return _OkWindow()

    def timeline_frames(self, *, limit: int = 0) -> tuple:
        if limit:
            return tuple(self._frames[-limit:])
        return tuple(self._frames)


class EchoLikeReference(UsableReference):
    """回声型参考：内容与采集一致 → 相关性≈1 → 得分被折减，**不会确认**（停在疑似并降音量）。"""

    def __init__(
        self, payload: bytes, *, count: int = 80, sample_rate: int = CAPTURE_SAMPLE_RATE_HZ
    ) -> None:
        import numpy as np

        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        start = int(time.monotonic() * 1000.0)
        self._frames = tuple((start + index * 20, samples.copy()) for index in range(count))
        self.pcm = SimpleNamespace(sample_rate=sample_rate)


class PartialEchoReference(EchoLikeReference):
    """部分回声参考：把相关性**精确**定在给定值（默认 0.25）。

    用于同时满足两件事：① ≥ `echo_proof_correlation`（证明"看得见自己的回声"）；
    ② 不至于把确认得分折减到门槛之下（score = 1.0 − 0.25 = 0.75 ≥ 0.7）。
    """

    def __init__(
        self,
        payload: bytes,
        *,
        correlation: float = 0.25,
        count: int = 80,
        sample_rate: int = CAPTURE_SAMPLE_RATE_HZ,
        seed: int = 17,
    ) -> None:
        import numpy as np

        if not 0.0 < correlation < 1.0:
            raise ValueError("correlation 必须在 (0,1) 内")
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        level = float(np.sqrt(np.mean(np.square(samples)))) or 1.0
        noise_level = level * math.sqrt(1.0 / (correlation * correlation) - 1.0)
        rng = np.random.default_rng(seed)
        mixed = (samples + rng.normal(0.0, noise_level, samples.size)).astype(np.float32)
        start = int(time.monotonic() * 1000.0)
        self._frames = tuple((start + index * 20, mixed.copy()) for index in range(count))
        self.pcm = SimpleNamespace(sample_rate=sample_rate)


class RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_chat(self, *, request_id: str, session_id: str, text: str):
        from app.agent.adapter import AgentTurnOutcome

        self.calls.append(text)
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"回复：{text}",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


class EnvironmentHolder:
    """假环境：捕获框记录每次采集会话，供测试推帧。"""

    def __init__(self, *, started: bool = True, dead: bool = False) -> None:
        self.captures: list[ChannelCapture] = []
        self._started = started
        self._dead = dead

    def capture_factory(self, bus: object):
        if self._dead:
            return CaptureSession(
                bus=bus,
                backend=DeadBackend(),  # type: ignore[arg-type]
                selection=DeviceSelection.fixed("mic-1"),
                config=CaptureConfig(),
            )
        capture = ChannelCapture(bus, started=self._started)
        self.captures.append(capture)
        return capture

    @property
    def capture(self) -> ChannelCapture:
        return self.captures[-1]


def fake_provider() -> SenseVoiceRecognizerAdapter:
    _OfflineRecognizer.recognizer = _Recognizer(RECOGNIZED)
    module = types.ModuleType("sherpa_onnx")
    module.OfflineRecognizer = _OfflineRecognizer  # type: ignore[attr-defined]
    return SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
        module_loader=lambda: module,
    )


def build_session(
    *,
    config: AutoVoiceConfig | None = None,
    speaker: object | None = None,
    holder: EnvironmentHolder | None = None,
    playback_gain: object | None = None,
    reference: object | None = None,
    details: dict | None = None,
) -> tuple[AutoVoiceSession, EnvironmentHolder, VoiceEntryEnvironment]:
    holder = holder or EnvironmentHolder()
    environment = VoiceEntryEnvironment(
        capture_factory=holder.capture_factory,
        provider=fake_provider(),
        speaker=speaker,
        devices=("Fake Mic",),
        tts_mode="none",
        aec_reference=reference,
        playback_gain=playback_gain,
        details=details or {"device": "Fake Mic", "device_id": "mic-1", "model": "fake.onnx"},
    )
    conversation = ConversationManager(RecordingAgent(), router=LocalChatRouter())
    session = AutoVoiceSession(
        conversation=conversation,
        environment=environment,
        config=config or AutoVoiceConfig(vad_mode="energy"),
        source_instance_id="r4-test",
    )
    return session, holder, environment


def barge_in_test_config(mode: str = "confirm", **overrides: object) -> AutoVoiceConfig:
    """测试用配置：把对齐估计间隔压到 5 帧，好在少量帧内拿到"回声可观测性"结论。"""

    values: dict[str, object] = {
        "vad_mode": "energy",
        "barge_in_mode": mode,
        "barge_in_alignment_interval_frames": 5,
    }
    values.update(overrides)
    return AutoVoiceConfig(**values)  # type: ignore[arg-type]


def force_echo_proven(session) -> None:
    """让"回声已证明"始终成立，用于只验证**停播路径**的用例。

    R4-B 收紧后（真机教训：本机相关性能到 0.6~0.75，但估计延迟出现 746/1589/−780 ms 的
    假值），单次高相关**不再**等于证明——需要"相关性达标 + 延迟物理合理 + 连续稳定"。
    证明规则本身由 `tests/duplex/test_barge_in_echo_proof.py` 覆盖；本套用例只关心
    "已经证明之后，确认插话能否真的停播"，因此把该判定的**替身**装上（注意 `arm()` 每次
    会清 `echo_proven`，所以必须在每次估计时重新置位）。
    """

    channel = session._barge_in_channel  # noqa: SLF001 - 契约测试直接注入前置状态
    assert channel is not None
    original = channel._note_proof_estimate  # noqa: SLF001

    def _proven() -> None:
        original()  # 保留真实的相关性/延迟统计（facts 里仍要看 max_echo_correlation）
        channel._echo_proven = True  # noqa: SLF001
        channel._proof_state = "echo_proven"  # noqa: SLF001

    channel._note_proof_estimate = _proven  # type: ignore[method-assign]  # noqa: SLF001
    _proven()


def wait_until(predicate, *, timeout: float = 20.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def wait_for_capture(holder: EnvironmentHolder, *, timeout: float = 20.0) -> ChannelCapture | None:
    wait_until(lambda: bool(holder.captures), timeout=timeout)
    return holder.captures[-1] if holder.captures else None


def drive_confirm(session: AutoVoiceSession, holder: EnvironmentHolder, stream) -> dict:
    """推帧到确认插话，然后收尾监视并返回最后一集事实。"""

    capture = wait_for_capture(holder)
    assert capture is not None
    config = session._barge_in_channel.config.barge_in  # noqa: SLF001
    # 多推几帧：对齐估计需要 0.3 s 历史，且监视线程是异步消费的。
    frames = config.min_suspect_frames - 1 + config.confirm_frames + 15
    for _ in range(frames):
        capture.push(speech_payload())
        time.sleep(0.002)
    wait_until(lambda: session.barge_in_details()["last"] is not None, timeout=20.0)
    # ``finish()`` 透传真实流后在 finally 里设 done 并 join 监视线程：返回时这一集已收尾。
    stream.finish()
    return session.barge_in_details()["last"] or {}


# --------------------------------------------------------------------- 配置


class ConfigTests(unittest.TestCase):
    def test_defaults_are_all_off(self) -> None:
        config = AutoVoiceConfig()
        self.assertEqual(config.barge_in_mode, "off")
        self.assertEqual(config.aec_reference_mode, "off")
        self.assertEqual(config.device_recovery_mode, "off")
        self.assertIsNone(config.barge_in_config)
        self.assertEqual(config.barge_in_max_monitor_seconds, 120.0)

    def test_mode_vocabularies_are_frozen(self) -> None:
        self.assertEqual(BARGE_IN_MODES, ("off", "duck", "confirm"))
        self.assertEqual(AEC_REFERENCE_MODES, ("off", "same_process"))
        self.assertEqual(DEVICE_RECOVERY_MODES, ("off", "safe_point"))

    def test_invalid_modes_are_rejected(self) -> None:
        for kwargs in (
            {"barge_in_mode": "loud"},
            {"aec_reference_mode": "loopback"},
            {"device_recovery_mode": "auto"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AutoVoiceConfig(**kwargs)  # type: ignore[arg-type]

    def test_invalid_monitor_seconds_and_config_type_are_rejected(self) -> None:
        for kwargs in (
            {"barge_in_max_monitor_seconds": 0},
            {"barge_in_max_monitor_seconds": -1.0},
            {"barge_in_max_monitor_seconds": float("nan")},
            {"barge_in_max_monitor_seconds": True},
            {"barge_in_config": "config"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AutoVoiceConfig(**kwargs)  # type: ignore[arg-type]

    def test_barge_in_config_accepts_a_real_config(self) -> None:
        from app.duplex.barge_in import BargeInConfig

        config = AutoVoiceConfig(barge_in_config=BargeInConfig(pre_roll_ms=500))
        self.assertEqual(config.barge_in_config.pre_roll_ms, 500)


# --------------------------------------------------------------------- 关闭态


class BargeInOffTests(unittest.TestCase):
    def test_off_builds_nothing_and_keeps_the_real_speaker(self) -> None:
        speaker = FakeSpeaker()
        session, _holder, environment = build_session(speaker=speaker)
        try:
            self.assertIsNone(session._barge_in_channel)
            self.assertIsNone(session._speaker_proxy)
            self.assertIs(session._loop._speaker, environment.speaker)
            details = session.barge_in_details()
            self.assertFalse(details["enabled"])
            self.assertEqual(details["mode"], "off")
        finally:
            session.close()

    def test_off_never_touches_the_capture_device_during_construction(self) -> None:
        session, holder, _environment = build_session(speaker=FakeSpeaker())
        try:
            self.assertEqual(holder.captures, [])
        finally:
            session.close()


class BargeInUnavailableTests(unittest.TestCase):
    def test_without_a_playback_channel_the_feature_is_reported_unavailable(self) -> None:
        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm"), speaker=None
        )
        try:
            details = session.barge_in_details()
            self.assertFalse(details["enabled"])
            self.assertEqual(details["unavailable_reason"], "no_playback_channel")
        finally:
            session.close()

    def test_without_a_gain_hook_ducking_is_reported_unavailable(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", barge_in_mode="duck"),
            speaker=FakeSpeaker(),
            reference=UsableReference(),
        )
        try:
            details = session.barge_in_details()
            self.assertTrue(details["enabled"])
            self.assertFalse(details["duck_available"])
            self.assertEqual(details["unavailable_reason"], DUCK_UNAVAILABLE_REASON)
        finally:
            session.close()

    def test_no_usable_echo_reference_disables_barge_in_entirely(self) -> None:
        """真机第三轮结论（fail-closed）：无可用参考 → 整个打断不启用（半双工）。"""

        holder = EnvironmentHolder()
        speaker = FakeSpeaker()
        session, _holder, environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=speaker,
            holder=holder,
        )
        try:
            details = session.barge_in_details()
            self.assertFalse(details["enabled"])
            self.assertEqual(details["unavailable_reason"], "no_usable_echo_reference")
            self.assertIsNone(session._barge_in_channel)
            self.assertIsNone(session._speaker_proxy)
            # 闭环仍然直接用原 speaker：播放期不守望输入，行为与 off 等价。
            self.assertIs(session._loop._speaker, environment.speaker)
            self.assertEqual(holder.captures, [])
            stream = environment.speaker.start_stream()
            stream.push_text_chunk("回复")
            self.assertEqual(speaker.streams[0].cancelled, 0)
        finally:
            session.close()

    def test_usable_reference_keeps_the_feature_enabled(self) -> None:
        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            reference=UsableReference(),
        )
        try:
            details = session.barge_in_details()
            self.assertTrue(details["enabled"])
            self.assertIsNotNone(session._speaker_proxy)
        finally:
            session.close()

    def test_aec_plan_degrades_to_strict_without_a_reference(self) -> None:
        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
        )
        try:
            plan = session.barge_in_details()["aec_plan"]
            self.assertEqual(plan["level"], "strict_no_aec")
            self.assertEqual(plan["barge_in_degradation"], "no_aec")
            self.assertTrue(plan["text_mode_available"])
            self.assertFalse(plan["aec_applied"])
        finally:
            session.close()

    def test_a_reference_with_a_clock_mismatch_never_claims_echo_cancellation(self) -> None:
        reference = SimpleNamespace(pcm=SimpleNamespace(sample_rate=24_000))
        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            reference=reference,
        )
        try:
            details = session.barge_in_details()
            self.assertEqual(details["aec_plan"]["level"], "strict_no_aec")
            self.assertFalse(details["aec_plan"]["aec_applied"])
            self.assertEqual(details["aec_plan"]["reason"], "speaker_reference_missing")
            # 异钟参考同样算"不可用"：不假装能打断。
            self.assertFalse(details["enabled"])
            self.assertEqual(details["unavailable_reason"], "no_usable_echo_reference")
        finally:
            session.close()


# --------------------------------------------------------------------- 真实接线


class MonitorWiringTests(unittest.TestCase):
    def test_capture_failure_never_interrupts_playback(self) -> None:
        holder = EnvironmentHolder(dead=True)
        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            holder=holder,
            reference=UsableReference(),
        )
        try:
            proxy = session._speaker_proxy
            self.assertIsNotNone(proxy)
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            wait_until(lambda: session.barge_in_details()["last"] is not None)
            stream.finish()
            facts = session.barge_in_details()["last"] or {}
            self.assertFalse(facts.get("confirmed", False))
            self.assertEqual(facts["reason"], "capture_unavailable")
            self.assertEqual(speaker_streams(session)[0].cancelled, 0)
        finally:
            session.close()

    def test_confirm_mode_stops_playback_and_keeps_the_pre_roll(self) -> None:
        speaker = FakeSpeaker()
        gain = PlaybackGain()
        session, holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=speaker,
            playback_gain=gain,
            reference=PartialEchoReference(speech_payload()),
        )
        try:
            force_echo_proven(session)
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            facts = drive_confirm(session, holder, stream)
            self.assertTrue(facts["confirmed"])
            self.assertEqual(speaker.streams[0].cancelled, 1)
            self.assertEqual(session.barge_in_details()["confirmations"], 1)
            self.assertGreater(len(session.pending_pre_roll_pcm()), 0)
            self.assertEqual(gain.value, 1.0)
        finally:
            session.close()

    def test_duck_mode_suppresses_the_confirmation_and_keeps_playing(self) -> None:
        speaker = FakeSpeaker()
        session, holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", barge_in_mode="duck"),
            speaker=speaker,
            playback_gain=PlaybackGain(),
            reference=UsableReference(),
        )
        try:
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            facts = drive_confirm(session, holder, stream)
            self.assertTrue(facts["confirmed"])
            self.assertEqual(facts["reason"], CONFIRM_SUPPRESSED_REASON)
            self.assertEqual(speaker.streams[0].cancelled, 0)
            self.assertEqual(session.barge_in_details()["suppressed_confirmations"], 1)
            self.assertEqual(session.pending_pre_roll_pcm(), b"")
        finally:
            session.close()

    def test_suspected_barge_in_ducks_through_the_playback_gain(self) -> None:
        """只有拿到"可用参考 + 对齐相关性"时才会降音量（否则见下一条）。"""

        gain = RecordingGain()
        session, holder, _environment = build_session(
            config=AutoVoiceConfig(
                vad_mode="energy",
                barge_in_mode="confirm",
                barge_in_alignment_interval_frames=5,
            ),
            speaker=FakeSpeaker(),
            playback_gain=gain,
            reference=EchoLikeReference(speech_payload()),
        )
        try:
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            capture = wait_for_capture(holder)
            assert capture is not None
            # 先凑够对齐历史（0.3 s）：期间按设计不降音量，之后才允许降。
            for _ in range(30):
                capture.push(speech_payload())
                time.sleep(0.002)
            self.assertTrue(
                wait_until(lambda: 0.25 in gain.values), "有判别依据后必须降音量"
            )
            stream.finish()
            self.assertEqual(gain.value, 1.0)
        finally:
            session.close()

    def test_no_reference_disables_the_duck_stage_at_session_level(self) -> None:
        """真机第三轮结论：没有可用回声参考时，整个打断都不启用（而不是只关降音量）。

        通道级的"无参考不降音量"由 `tests/duplex/test_barge_in_channel.py` 覆盖；
        会话层在更前面就 fail-closed：不建通道、不包 speaker、如实记原因。
        """

        gain = PlaybackGain()
        holder = EnvironmentHolder()
        session, _holder, environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            playback_gain=gain,
            holder=holder,
        )
        try:
            details = session.barge_in_details()
            self.assertFalse(details["enabled"])
            self.assertEqual(details["unavailable_reason"], "no_usable_echo_reference")
            self.assertEqual(gain.value, 1.0)
            self.assertEqual(holder.captures, [])
            self.assertIs(session._loop._speaker, environment.speaker)
        finally:
            session.close()

    def test_awaiting_confirmation_stops_playback_but_never_hands_off(self) -> None:
        speaker = FakeSpeaker()
        session, holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=speaker,
            playback_gain=PlaybackGain(),
            reference=PartialEchoReference(speech_payload()),
        )
        try:
            force_echo_proven(session)
            session.set_awaiting_confirmation(True)
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("安全确认提示")
            facts = drive_confirm(session, holder, stream)
            self.assertTrue(facts["confirmed"])
            self.assertEqual(facts["decision"]["action"], "stop_and_replay")
            self.assertTrue(facts["decision"]["prompt_replay_required"])
            self.assertEqual(speaker.streams[0].cancelled, 1)
            self.assertEqual(session.pending_pre_roll_pcm(), b"")
            self.assertEqual(session.barge_in_details()["confirmations"], 0)
        finally:
            session.close()

    def test_unproven_echo_never_stops_playback(self) -> None:
        """R4-A 安全门：还没证明"自己的回声可被观测"时，确认插话**不动手**。

        真机第三轮就是这种局面（6/6 假确认）——拿不到判别依据时宁可不停播。
        """

        speaker = FakeSpeaker()
        session, holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=speaker,
            playback_gain=PlaybackGain(),
            reference=UsableReference(),  # 相关 ≈0.03 < echo_proof 0.2
        )
        try:
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            facts = drive_confirm(session, holder, stream)
            self.assertTrue(facts["confirmed"])
            self.assertEqual(facts["reason"], "barge_in_unproven_echo")
            self.assertIs(facts["echo_proven"], False)
            # 播报其实播完了：`_barge_in_stopped_episode` 必须为假 —— 它决定 end_reason
            # 是否记成 barge_in_interrupted（真机第三轮曾把未停播的回合谎报成被打断）。
            self.assertFalse(session._barge_in_stopped_episode)  # noqa: SLF001
            self.assertEqual(speaker.streams[0].cancelled, 0)
            details = session.barge_in_details()
            self.assertEqual(details["unproven_confirmations"], 1)
            self.assertEqual(details["confirmations"], 0)
            self.assertEqual(session.pending_pre_roll_pcm(), b"")
        finally:
            session.close()

    def test_proven_echo_allows_the_stop(self) -> None:
        speaker = FakeSpeaker()
        session, holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=speaker,
            playback_gain=PlaybackGain(),
            reference=PartialEchoReference(speech_payload()),
        )
        try:
            force_echo_proven(session)
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            facts = drive_confirm(session, holder, stream)
            self.assertIs(facts["echo_proven"], True)
            self.assertGreaterEqual(float(facts["max_echo_correlation"]), 0.2)
            self.assertEqual(speaker.streams[0].cancelled, 1)
            self.assertEqual(session.barge_in_details()["confirmations"], 1)
            # 真的停过播 → end_reason 才可以记 barge_in_interrupted。
            self.assertTrue(session._barge_in_stopped_episode)  # noqa: SLF001
        finally:
            session.close()

    def test_log_details_carry_the_echo_evidence(self) -> None:
        """真机取证需要"回声可观测性"数字进入脱敏日志（否则无法复核本轮判定）。"""

        from app.runtime.auto_voice import AutoTurnFacts

        facts = AutoTurnFacts(
            index=1,
            boundary="silence_timeout",
            frames=120,
            speech_seen=True,
            end_reason="silence_timeout",
            accepted=True,
            submitted=True,
            spoken=True,
            reason="ok",
            seconds=1.5,
            barge_in={
                "enabled": True,
                "confirmed": False,
                "reason": None,
                "frames": 120,
                "echo_proven": False,
                "max_echo_correlation": 0.0731,
                "echo_delay_ms": 180,
                "alignment_estimates": 6,
            },
        )
        record = facts.log_details()
        self.assertFalse(record["barge_in_echo_proven"])
        self.assertAlmostEqual(record["barge_in_max_echo_correlation"], 0.0731)
        self.assertEqual(record["barge_in_echo_delay_ms"], 180)
        self.assertEqual(record["barge_in_alignment_estimates"], 6)
        # 仍然只有计数/原因码，不含文本。
        self.assertNotIn("asr_text", record)
        self.assertNotIn("pcm", record)

    def test_set_awaiting_confirmation_requires_a_boolean(self) -> None:
        session, _holder, _environment = build_session(speaker=FakeSpeaker())
        try:
            with self.assertRaises(TypeError):
                session.set_awaiting_confirmation(1)  # type: ignore[arg-type]
        finally:
            session.close()

    def test_pre_roll_can_be_taken_once(self) -> None:
        session, holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            playback_gain=PlaybackGain(),
            reference=PartialEchoReference(speech_payload()),
        )
        try:
            force_echo_proven(session)
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            drive_confirm(session, holder, stream)
            data = session.take_pending_pre_roll()
            self.assertGreater(len(data), 0)
            self.assertEqual(session.take_pending_pre_roll(), b"")
        finally:
            session.close()

    def test_details_never_expose_audio_or_text(self) -> None:
        session, holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            playback_gain=PlaybackGain(),
            reference=UsableReference(),
        )
        try:
            proxy = session._speaker_proxy
            stream = proxy.start_stream()
            stream.push_text_chunk("回复")
            drive_confirm(session, holder, stream)
            details = session.barge_in_details()
            self.assertNotIn("pcm", details)
            self.assertNotIn("text", details)
            self.assertIn("last", details)
            self.assertIs(details["pre_roll_merged"], False)
        finally:
            session.close()

    def test_close_disarms_the_channel_and_restores_the_gain(self) -> None:
        gain = RecordingGain()
        session, holder, _environment = build_session(
            config=AutoVoiceConfig(
                vad_mode="energy",
                barge_in_mode="confirm",
                barge_in_alignment_interval_frames=5,
            ),
            speaker=FakeSpeaker(),
            playback_gain=gain,
            reference=EchoLikeReference(speech_payload()),
        )
        proxy = session._speaker_proxy
        stream = proxy.start_stream()
        stream.push_text_chunk("回复")
        capture = wait_for_capture(holder)
        assert capture is not None
        for _ in range(30):
            capture.push(speech_payload())
            time.sleep(0.002)
        self.assertTrue(wait_until(lambda: 0.25 in gain.values, timeout=5.0))
        session.close()
        self.assertEqual(gain.value, 1.0)
        self.assertFalse(session._barge_in_channel.armed)
        self.assertEqual(capture.stopped[0], "barge-in-monitor")


def speaker_streams(session: AutoVoiceSession) -> list[FakeStream]:
    """取被包装的 speaker 上已创建的流（用于断言"没有被取消"）。"""

    speaker = session._loop._speaker
    real = getattr(speaker, "_speaker", None)
    return getattr(real, "streams", []) if real is not None else []


# --------------------------------------------------------------------- 设备策略


class DevicePolicyTests(unittest.TestCase):
    def test_off_keeps_the_policy_disabled(self) -> None:
        session, _holder, _environment = build_session(speaker=FakeSpeaker())
        try:
            self.assertIsNone(session._device_policy)
            self.assertFalse(session.device_details()["enabled"])
            self.assertIsNone(session._device_safe_point(turn_active=False))
        finally:
            session.close()

    def test_safe_point_skips_while_the_output_health_is_unknown(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            facts = session._device_safe_point(turn_active=False)
            self.assertEqual(facts["action"], "skipped")
            self.assertEqual(facts["reason"], "output_health_unknown")
        finally:
            session.close()

    def test_healthy_devices_need_no_action(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            session._observe_device("output", "ready")
            facts = session._device_safe_point(turn_active=False)
            self.assertEqual(facts["action"], "none")
            self.assertEqual(facts["reason"], "healthy")
        finally:
            session.close()

    def test_output_loss_is_deferred_while_a_turn_is_active(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            session._observe_device("output", "ready")
            session._observe_device("output", "disconnected")
            facts = session._device_safe_point(turn_active=True)
            self.assertEqual(facts["action"], "notify_user")
            self.assertEqual(facts["reason"], "deferred_active_turn")
            self.assertTrue(facts["safety_point_reached"] is False)
        finally:
            session.close()

    def test_output_loss_without_a_fixed_target_falls_back_to_text(self) -> None:
        speaker = FakeSpeaker()
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=speaker,
        )
        try:
            session._observe_device("output", "ready")
            session._observe_device("output", "disconnected")
            facts = session._device_safe_point(turn_active=False)
            # waveOut 走 WAVE_MAPPER，没有"固定输出设备 id"→ R3 语义不允许自动重连，
            # 按用户决定：停止播报并回退文字（如实记录，不猜设备）。
            self.assertEqual(facts["action"], "fallback_to_text")
            self.assertEqual(facts["reason"], "text_mode_kept")
            self.assertEqual(session.device_details()["text_fallback_reason"], "text_mode_kept")
            self.assertEqual(session._device_policy.attempts, 0)
            self.assertIn("no_fixed_device_target", facts["messages"])
            self.assertEqual(speaker.streams, [])
        finally:
            session.close()

    def test_output_reconnect_is_attempted_once_per_episode_when_a_target_is_fixed(self) -> None:
        speaker = FakeSpeaker()
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=speaker,
        )
        try:
            from app.devices import DeviceKind

            session._device_policy.note_device_switch(DeviceKind.OUTPUT, device_id="spk-1")
            session._observe_device("output", "ready")
            session._observe_device("output", "disconnected")
            facts = session._device_safe_point(turn_active=False)
            self.assertEqual(facts["action"], "reconnect_output")
            self.assertTrue(facts["attempted"])
            self.assertTrue(facts["attempt_succeeded"])
            self.assertEqual(session._device_policy.attempts, 1)
            self.assertEqual(len(speaker.streams), 1)
            self.assertEqual(speaker.streams[0].cancelled, 1)
            second = session._device_safe_point(turn_active=False)
            self.assertEqual(second["action"], "none")
            self.assertEqual(session._device_policy.attempts, 1)
        finally:
            session.close()

    def test_failed_reconnect_is_not_retried_in_the_same_episode(self) -> None:
        speaker = FakeSpeaker(start_none=True)
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=speaker,
        )
        try:
            from app.devices import DeviceKind

            session._device_policy.note_device_switch(DeviceKind.OUTPUT, device_id="spk-1")
            session._observe_device("output", "ready")
            session._observe_device("output", "disconnected")
            facts = session._device_safe_point(turn_active=False)
            self.assertEqual(facts["action"], "reconnect_output")
            self.assertFalse(facts["attempt_succeeded"])
            self.assertEqual(session._device_policy.attempts, 1)
            second = session._device_safe_point(turn_active=False)
            self.assertEqual(second["action"], "deferred")
            self.assertEqual(second["reason"], "already_attempted_this_episode")
            self.assertEqual(session._device_policy.attempts, 1)
        finally:
            session.close()

    def test_input_reconnect_succeeds_and_restores_ready(self) -> None:
        session, holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            session._observe_device("output", "ready")
            session._observe_device("input", "disconnected")
            facts = session._device_safe_point(turn_active=False)
            self.assertEqual(facts["action"], "reconnect_input")
            self.assertTrue(facts["attempt_succeeded"])
            from app.devices import DeviceHealth, DeviceKind

            self.assertIs(session._device_policy.health(DeviceKind.INPUT), DeviceHealth.READY)
            self.assertEqual(len(holder.captures), 1)
            self.assertEqual(holder.captures[0].stopped[0], "device-probe")
        finally:
            session.close()

    def test_busy_output_keeps_text_mode_and_never_auto_retries(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            session._observe_device("output", "ready")
            session._observe_device("output", "busy")
            facts = session._device_safe_point(turn_active=False)
            self.assertEqual(facts["action"], "fallback_to_text")
            self.assertEqual(session.device_details()["text_fallback_reason"], "text_mode_kept")
            self.assertEqual(session._device_policy.attempts, 0)
        finally:
            session.close()

    def test_repeated_observations_do_not_inflate_the_epoch(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            session._observe_device("output", "ready")
            epoch = session._device_policy.epoch(_kind("output"))
            session._observe_device("output", "ready")
            self.assertEqual(session._device_policy.epoch(_kind("output")), epoch)
        finally:
            session.close()

    def test_details_include_the_policy_view(self) -> None:
        session, _holder, _environment = build_session(
            config=AutoVoiceConfig(vad_mode="energy", device_recovery_mode="safe_point"),
            speaker=FakeSpeaker(),
        )
        try:
            details = session.device_details()
            self.assertTrue(details["enabled"])
            self.assertEqual(details["fixed_input_device"], "mic-1")
            self.assertIn("input_health", details)
        finally:
            session.close()


def _kind(name: str):
    from app.devices import DeviceKind

    return DeviceKind.INPUT if name == "input" else DeviceKind.OUTPUT


# --------------------------------------------------------------------- 一轮真实 run_turn


class FullTurnTests(unittest.TestCase):
    def test_off_mode_still_completes_a_turn_and_reports_no_barge_in(self) -> None:
        chunks = [speech_payload()] * 20 + [array.array("h", [0] * 320).tobytes()] * 20
        environment, backend = build_environment(chunks)
        environment.speaker = FakeSpeaker()
        conversation = ConversationManager(RecordingAgent(), router=LocalChatRouter())
        config = AutoVoiceConfig(
            vad_mode="energy",
            silence_ms=100,
            min_utterance_ms=300,
            max_record_seconds=10,
            barge_in_mode="off",
        )
        session = AutoVoiceSession(
            conversation=conversation,
            environment=environment,
            config=config,
            source_instance_id="r4-full-turn",
        )
        try:
            facts = _paced_run_turn(backend)(session)
        finally:
            session.close()
        self.assertEqual(facts.end_reason, "silence_timeout")
        self.assertTrue(facts.submitted)
        self.assertTrue(facts.spoken)
        self.assertIsNone(facts.barge_in)
        self.assertIsNone(facts.device)
        self.assertIsNone(facts.pre_roll)
        record = facts.log_details()
        self.assertFalse(record["barge_in_enabled"])
        self.assertIsNone(record["device_action"])

    def test_start_failure_is_reported_without_barge_in_facts(self) -> None:
        holder = EnvironmentHolder(dead=True)
        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm"),
            speaker=FakeSpeaker(),
            holder=holder,
        )
        try:
            facts = session.run_turn()
        finally:
            session.close()
        self.assertEqual(facts.end_reason, "start_failed")
        self.assertIsNone(facts.barge_in)
        self.assertFalse(facts.submitted)


# --------------------------------------------------- P10 · T1 #13 输入静默窗


class SpeechMuteWindowTurnTests(unittest.TestCase):
    """#13：播报后输入静默窗（默认 0 = 事实与行为都不变；开启后如实上报计数与原因码）。"""

    def _run_turn(self, *, mute_ms: int) -> object:
        chunks = [speech_payload()] * 20 + [array.array("h", [0] * 320).tobytes()] * 20
        environment, backend = build_environment(chunks)
        environment.speaker = FakeSpeaker()
        conversation = ConversationManager(RecordingAgent(), router=LocalChatRouter())
        config = AutoVoiceConfig(
            vad_mode="energy",
            silence_ms=100,
            min_utterance_ms=300,
            max_record_seconds=10,
            barge_in_mode="off",
            post_speech_mute_ms=mute_ms,
        )
        session = AutoVoiceSession(
            conversation=conversation,
            environment=environment,
            config=config,
            source_instance_id="p10-13-mute",
        )
        try:
            return _paced_run_turn(backend)(session)
        finally:
            session.close()

    def test_window_off_reports_no_mute_facts(self) -> None:
        facts = self._run_turn(mute_ms=0)
        self.assertTrue(facts.spoken)
        self.assertIsNone(facts.speech_mute)
        record = facts.log_details()
        for key in (
            "speech_mute_active",
            "speech_mute_frames",
            "speech_mute_reason",
            "speech_mute_ms",
        ):
            with self.subTest(key=key):
                self.assertIsNone(record[key])
        # 诊断时间线：首轮也应有"start→首帧"延迟；轮次间空档在第一轮必然为 None。
        self.assertIsNotNone(record["first_frame_ms"])
        self.assertIsNone(record["turn_gap_ms"])
        self.assertIsNone(record["kbd_codes"])  # 未注入按键源 ⇒ 无流水
        # 阶段耗时（2026-10-14 真机"说完话后没反应"）：收音与"说完之后"的耗时都要有数。
        self.assertIsNotNone(record["listen_ms"])
        self.assertIsNotNone(record["reply_ms"])
        self.assertGreaterEqual(record["listen_ms"], 0.0)
        self.assertGreaterEqual(record["reply_ms"], 0.0)

    def test_window_on_is_reported_and_the_turn_still_completes(self) -> None:
        facts = self._run_turn(mute_ms=800)
        self.assertTrue(facts.submitted)
        self.assertTrue(facts.spoken)                 # 附加保护不得影响播报
        assert facts.speech_mute is not None
        self.assertEqual(facts.speech_mute["gap_ms"], 800)
        self.assertEqual(facts.speech_mute["reason"], "post_speech_mute")
        self.assertTrue(facts.speech_mute["active"])   # 播报刚结束 ⇒ 冷却窗正在计
        record = facts.log_details()
        self.assertEqual(record["speech_mute_reason"], "post_speech_mute")
        self.assertEqual(record["speech_mute_ms"], 800)
        self.assertGreaterEqual(record["speech_mute_frames"], 0)

    def test_config_rejects_bad_window_values(self) -> None:
        for value in (-1, True, "800"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AutoVoiceConfig(post_speech_mute_ms=value)  # type: ignore[arg-type]


class KeyboardDiagnosticFactsTests(unittest.TestCase):
    """诊断（2026-10-14）：按键流水（键码/来源/处置）与轮次时间线进轮次事实。"""

    def test_key_event_log_is_folded_into_the_key_facts(self) -> None:
        pending: list[dict[str, str]] = [
            {"source": "between", "code": "0x1b", "action": "ignored_escape"},
            {"source": "turn", "code": "0x0d", "action": "confirm"},
        ]

        def take() -> list[dict[str, str]]:
            snapshot = list(pending)
            pending.clear()
            return snapshot

        session = object.__new__(AutoVoiceSession)
        session._key_source = SimpleNamespace(take_events=take)  # type: ignore[attr-defined]
        session._key_action = "confirm"  # type: ignore[attr-defined]
        session._key_interrupted = False  # type: ignore[attr-defined]
        facts = session._take_key_facts()  # type: ignore[attr-defined]
        assert facts is not None
        self.assertEqual(facts["action"], "confirm")
        self.assertIn("between:0x1b:ignored_escape", facts["codes"])
        self.assertIn("turn:0x0d:confirm", facts["codes"])
        # 读过即清：第二次没有动作也没有流水 ⇒ None（旧口径不变）。
        self.assertIsNone(session._take_key_facts())  # type: ignore[attr-defined]

    def test_key_event_log_is_optional_and_never_raises(self) -> None:
        session = object.__new__(AutoVoiceSession)

        def boom() -> list[dict[str, str]]:
            raise RuntimeError("日志坏了")

        session._key_source = SimpleNamespace(take_events=boom)  # type: ignore[attr-defined]
        session._key_action = None  # type: ignore[attr-defined]
        session._key_interrupted = False  # type: ignore[attr-defined]
        self.assertIsNone(session._take_key_facts())  # type: ignore[attr-defined]

    def test_timeline_helpers_tolerate_missing_state(self) -> None:
        session = object.__new__(AutoVoiceSession)
        self.assertIsNone(session._turn_first_frame_ms())  # type: ignore[attr-defined]
        self.assertIsNone(session._turn_gap_ms())  # type: ignore[attr-defined]
        self.assertIsNone(session._turn_listen_ms())  # type: ignore[attr-defined]
        self.assertIsNone(session._turn_reply_ms())  # type: ignore[attr-defined]
        session._turn_started_at = 10.0  # type: ignore[attr-defined]
        session._turn_first_frame_at = 10.25  # type: ignore[attr-defined]
        session._turn_gap_seconds = 0.5  # type: ignore[attr-defined]
        session._turn_listen_end_at = 13.0  # type: ignore[attr-defined]
        self.assertEqual(session._turn_first_frame_ms(), 250.0)  # type: ignore[attr-defined]
        self.assertEqual(session._turn_gap_ms(), 500.0)  # type: ignore[attr-defined]
        self.assertEqual(session._turn_listen_ms(), 3000.0)  # type: ignore[attr-defined]
        self.assertIsNotNone(session._turn_reply_ms())  # type: ignore[attr-defined]


# --------------------------------------------------------------------- 检测器粘滞


class _AlternatingDetector:
    """模拟 Silero 的整窗判定：只有"凑满窗"的帧返回 True（320 样本帧 → 真/假交替）。"""

    def __init__(self, pattern: list[bool]) -> None:
        self._pattern = list(pattern)
        self._index = 0
        self.speech_started = False

    def observe(self, packet: object) -> bool:
        del packet
        hit = self._pattern[self._index % len(self._pattern)]
        self._index += 1
        if hit:
            self.speech_started = True
        return hit

    def reset(self) -> None:
        self._index = 0
        self.speech_started = False


class StickyWindowObserveTests(unittest.TestCase):
    """真机第二轮回归：窗口结论必须粘滞，否则"连续 N 帧"结构性不可达。"""

    def test_alternating_window_hits_are_sticky(self) -> None:
        from app.runtime.auto_voice import _sticky_window_observe

        detector = _AlternatingDetector([True, False])
        observe = _sticky_window_observe(detector, 1)
        results = [observe(object()) for _ in range(6)]
        # 粘滞：一旦窗口命中，未满窗的帧沿用 True；不会出现真/假交替。
        self.assertEqual(results, [True] * 6)

    def test_no_hit_keeps_returning_false(self) -> None:
        from app.runtime.auto_voice import _sticky_window_observe

        detector = _AlternatingDetector([False])
        observe = _sticky_window_observe(detector, 1)
        self.assertEqual([observe(object()) for _ in range(4)], [False] * 4)

    def test_latched_window_fact_keeps_frames_true_after_a_hit(self) -> None:
        from app.runtime.auto_voice import _sticky_window_observe

        detector = _AlternatingDetector([False, True])
        observe = _sticky_window_observe(detector, 2)
        results = [observe(object()) for _ in range(4)]
        self.assertFalse(results[0])  # 还没有任何窗口命中
        self.assertTrue(results[1])  # 命中 → 窗口级事实成立
        self.assertTrue(all(results[1:]))  # 未满窗的帧沿用该事实

    def test_without_a_window_latch_consecutive_hits_are_required(self) -> None:
        from app.runtime.auto_voice import _sticky_window_observe

        class Bare:
            def __init__(self) -> None:
                self._hits = [False, True, True]

            def observe(self, packet: object) -> bool:
                del packet
                return self._hits.pop(0)

        observe = _sticky_window_observe(Bare(), 2)
        self.assertFalse(observe(object()))  # 第一次没命中
        self.assertFalse(observe(object()))  # 单次命中不足连续 2 次
        self.assertTrue(observe(object()))  # 连续 2 次命中

    def test_invalid_detector_and_window_are_rejected(self) -> None:
        from app.runtime.auto_voice import _sticky_window_observe

        with self.assertRaises(TypeError):
            _sticky_window_observe(object(), 1)
        with self.assertRaises(ValueError):
            _sticky_window_observe(_AlternatingDetector([True]), 0)

    def test_on_hit_callback_counts_window_hits(self) -> None:
        from app.runtime.auto_voice import _sticky_window_observe

        hits: list[int] = []
        detector = _AlternatingDetector([True, False])
        observe = _sticky_window_observe(detector, 1, on_hit=lambda: hits.append(1))
        for _ in range(6):
            observe(object())
        self.assertEqual(len(hits), 3)


# --------------------------------------------------------------------- 参考源工厂


class RealReferenceFactoryTests(unittest.TestCase):
    """方案 A 参考源的装配约束（离线，不打开设备）。

    真机 check-only 曾抓到：参考按 48 kHz 默认装配而 waveOut 硬编码 24 kHz →
    `SameProcessAecReference` 直接 `sample_rate_mismatch`。R4-A 又进一步要求
    **参考与采集同钟**（16 kHz），因此装配是"16 kHz 时间线 + 24 kHz 提交侧 + 重采样"。
    """

    def test_reference_matches_the_capture_clock_and_resamples_playback(self) -> None:
        from app.audio.capture import CaptureConfig
        from app.runtime.voice_entry import build_same_process_reference
        from app.tts.waveout_pcm import SAMPLE_RATE

        reference, gain = build_same_process_reference()
        capture_rate = CaptureConfig().sample_rate_hz
        self.assertEqual(reference.pcm.sample_rate, capture_rate)
        self.assertEqual(reference.pcm.source_sample_rate, SAMPLE_RATE)
        self.assertTrue(reference.pcm.resampling)
        self.assertEqual(reference.pcm.frame_samples, capture_rate * FRAME_MS // 1000)
        self.assertEqual(gain.read(), 1.0)

    def test_submitted_playback_audio_enters_the_reference_timeline(self) -> None:
        from app.runtime.voice_entry import build_same_process_reference
        from app.tts.waveout_pcm import SAMPLE_RATE

        reference, _gain = build_same_process_reference()
        # 200 ms 的 24 kHz 播放 PCM（33 ms 不够凑满一个 16 kHz 帧）。
        chunk = bytes(SAMPLE_RATE * 2 // 5)
        self.assertGreaterEqual(reference.submit(chunk, submitted_at_seconds=1.0), 1)
        window = reference.window(1000)
        self.assertTrue(window.ok)

    def test_reference_observer_never_raises_into_playback(self) -> None:
        from app.runtime.voice_entry import build_same_process_reference

        reference, _gain = build_same_process_reference()
        observer = reference.as_submit_observer()
        observer(b"", 1.0)  # 空块是非法输入，但旁路必须吞掉
        self.assertEqual(reference.observer_errors, 1)
        self.assertTrue(reference.last_observer_error)


# --------------------------------------------------------------------- CLI 折算


class CliWiringTests(unittest.TestCase):
    """``--barge-in-mode`` / ``--aec-reference-mode`` / ``--device-recovery-mode`` 的折算。"""

    def _captured(self, argv: list[str]) -> dict:
        from unittest import mock

        from app.main import main

        captured: dict = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with mock.patch("app.main.run", side_effect=fake_run):
            code = main(argv)
        self.assertEqual(code, 0)
        return captured

    def test_defaults_are_off(self) -> None:
        captured = self._captured(["--voice", "--auto"])
        config = captured["auto_config"]
        self.assertEqual(config.barge_in_mode, "off")
        self.assertEqual(config.aec_reference_mode, "off")
        self.assertEqual(config.device_recovery_mode, "off")

    def test_flags_flow_into_the_shared_config(self) -> None:
        captured = self._captured(
            [
                "--voice",
                "--auto",
                "--barge-in-mode",
                "confirm",
                "--aec-reference-mode",
                "same_process",
                "--device-recovery-mode",
                "safe_point",
            ]
        )
        config = captured["auto_config"]
        self.assertEqual(config.barge_in_mode, "confirm")
        self.assertEqual(config.aec_reference_mode, "same_process")
        self.assertEqual(config.device_recovery_mode, "safe_point")

    def test_reference_mode_is_forwarded_to_the_voice_environment_config(self) -> None:
        from app.main import _voice_entry_config_for_auto

        config = AutoVoiceConfig(aec_reference_mode="same_process")
        entry = _voice_entry_config_for_auto(config)
        self.assertEqual(entry.aec_reference_mode, "same_process")

    def test_invalid_choice_is_rejected_by_the_parser(self) -> None:
        from app.main import main

        with self.assertRaises(SystemExit):
            main(["--voice", "--auto", "--barge-in-mode", "loud"])

    def test_status_notice_is_silent_when_barge_in_is_off(self) -> None:
        from app.main import _report_barge_in_status

        output = io.StringIO()
        _report_barge_in_status(AutoVoiceConfig(), object(), output)
        self.assertEqual(output.getvalue(), "")

    def test_status_notice_reports_the_disabled_reason(self) -> None:
        from app.main import _report_barge_in_status

        class FakeSession:
            def barge_in_details(self) -> dict:
                return {"enabled": False, "unavailable_reason": "no_usable_echo_reference"}

        output = io.StringIO()
        _report_barge_in_status(
            AutoVoiceConfig(barge_in_mode="confirm"), FakeSession(), output
        )
        text = output.getvalue()
        self.assertIn("打断未启用", text)
        self.assertIn("no_usable_echo_reference", text)

    def test_status_notice_reports_an_enabled_feature(self) -> None:
        from app.main import _report_barge_in_status

        class FakeSession:
            def barge_in_details(self) -> dict:
                return {"enabled": True, "degradation": "full"}

        output = io.StringIO()
        _report_barge_in_status(
            AutoVoiceConfig(barge_in_mode="confirm"), FakeSession(), output
        )
        self.assertIn("打断已启用", output.getvalue())


# --------------------------------------------------------------------- 边界


class BoundaryTests(unittest.TestCase):
    def _names(self) -> set[str]:
        tree = ast.parse(AUTO_VOICE_PATH.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def test_session_wiring_never_touches_the_keyboard_interrupt_source(self) -> None:
        names = self._names()
        self.assertNotIn("interrupt_source", names)
        self.assertNotIn("_keyboard_interrupt_source", names)
        self.assertNotIn("app.main", names)

    def test_barge_in_channel_uses_its_own_audio_identity(self) -> None:
        from app.duplex.barge_in_channel import CHANNEL_AUDIO_SESSION_ID, CHANNEL_STREAM_ID

        self.assertNotEqual(CHANNEL_AUDIO_SESSION_ID, "p9-auto-runner")
        source = AUTO_VOICE_PATH.read_text(encoding="utf-8")
        self.assertIn("CHANNEL_AUDIO_SESSION_ID" if False else "BargeInChannel", source)
        self.assertTrue(CHANNEL_STREAM_ID.endswith("-stream"))

    def test_module_never_mentions_the_pre_roll_merge_as_done(self) -> None:
        self.assertTrue(PRE_ROLL_MERGE_UNSUPPORTED)
        source = AUTO_VOICE_PATH.read_text(encoding="utf-8")
        self.assertIn("pre_roll_merge_unsupported", source)
        self.assertIn("PRE_ROLL_MERGE_UNSUPPORTED", source)


if __name__ == "__main__":
    unittest.main()
