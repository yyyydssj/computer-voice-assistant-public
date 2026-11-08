r"""P9 免按键自动收音 runner 的离线契约测试。

只验证 runner 的边界与装配，不打开真实设备、不联网、不做任何电脑操作：

- 没有 ``--live-opt-in`` 时立即返回退出码 2，且不触碰设备环境装配；
- 参数非法在装配前被拒绝；
- 注入式离线环境 + 真实 P6 管道 + P9 自动收音驱动跑通一整轮（合成帧）；
- artifact 只记计数/状态/摘要哈希，绝不落识别文本或音频；
- 源码级执行面审计（无进程/网络/设备入口，无安全链引用）。
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.audio.bus import AudioBus, AudioBusState
from app.audio.vad import silero as silero_module
from app.contracts import AudioFrame
from app.voice_loop.auto_driver import (
    STOP_DETECTOR_FAILURE,
    AutoCaptureDriverConfig,
    AutoCaptureVoiceDriver,
)
from app.voice_loop.boundary import VoiceTurnBoundary
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceCheckResult,
    DeviceCheckStatus,
    DeviceSelection,
    FakeCaptureStream,
)
from app.conversation import ConversationManager
from app.runtime.assistant_runtime import LocalChatRouter
from app.runtime.voice_entry import VoiceEntryEnvironment
from tests.integration.test_p6_integration import _OfflineRecognizer, _Recognizer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.p9_auto_voice_runner as runner  # noqa: E402

SPOKEN_SENTENCE = "你好，测试一下自动收音"
RUNNER_PATH = Path(__file__).resolve().parents[2] / "tools" / "p9_auto_voice_runner.py"
SAMPLE_FRAMES = 320
FRAME_MS = 20
FAKE_RECOGNIZED_TEXT = "本行仅由测试替身产生，用于验证 artifact 只记摘要而不落正文"


def expected_sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def speech_payload(*, amplitude: int = 3000, frames: int = SAMPLE_FRAMES) -> bytes:
    import array

    return array.array("h", [amplitude] * frames).tobytes()


def silence_payload(*, frames: int = SAMPLE_FRAMES) -> bytes:
    import array

    return array.array("h", [0] * frames).tobytes()


def noise_payload(*, amplitude: int = 150, frames: int = SAMPLE_FRAMES) -> bytes:
    """恒定幅度环境噪声帧；其 PCM16 RMS 恰好等于 ``amplitude``。"""

    import array

    return array.array("h", [amplitude] * frames).tobytes()


class PacedStream(FakeCaptureStream):
    """按帧节拍投递合成帧，模拟实时分帧到达（不启动线程）。

    ``AudioBus`` 容量只有 64 帧且溢出即 fail closed，因此测试**不能**一次堆入整轮
    上百帧；这里用固定时间步长（每 ``FRAME_MS`` 一帧）把合成帧铺到时间轴上。
    """

    def __init__(self, chunks: list[bytes], *, frames: int = SAMPLE_FRAMES) -> None:
        super().__init__()
        self._chunks = list(chunks)
        self._frames = frames
        self.emitted = 0
        self._t0 = time.monotonic()

    def frame_time(self, frame_index: int) -> float:
        return self._t0 + (frame_index * FRAME_MS) / 1000.0

    def pump(self) -> bool:
        """投递下一帧之前，先把时钟推进到该帧应有的时刻。"""

        if self.emitted >= len(self._chunks) or self.callback is None:
            return False
        target = self.frame_time(self.emitted)
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        payload = self._chunks[self.emitted]
        self.emitted += 1
        self.emit(payload, frames=self._frames)
        return True


class PacedBackend:
    def __init__(self, chunks: list[bytes]) -> None:
        self.stream = PacedStream(chunks)

    def check_device(self, selection, *, sample_rate_hz: int, channel_count: int) -> DeviceCheckResult:
        del selection, sample_rate_hz, channel_count
        return DeviceCheckResult(DeviceCheckStatus.READY, "mic-1", "ready")

    def open_input_stream(self, **kwargs: object) -> PacedStream:
        self.stream.callback = kwargs["callback"]  # type: ignore[assignment]
        return self.stream


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


def build_environment(chunks: list[bytes], *, load_failure: bool = False) -> tuple[VoiceEntryEnvironment, PacedBackend]:
    _OfflineRecognizer.recognizer = _Recognizer(FAKE_RECOGNIZED_TEXT)
    module = types.ModuleType("sherpa_onnx")
    module.OfflineRecognizer = _OfflineRecognizer  # type: ignore[attr-defined]

    def loader():
        if load_failure:
            raise RuntimeError("sherpa init failed")
        return module

    provider = SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
        module_loader=loader,
    )
    backend = PacedBackend(chunks)

    def capture_factory(bus) -> CaptureSession:
        return CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(),
        )

    environment = VoiceEntryEnvironment(
        capture_factory=capture_factory,
        provider=provider,
        speaker=None,
        devices=("Fake Mic",),
        tts_mode="none",
        details={"device": "Fake Mic", "device_id": "mic-1", "model": "fake.onnx"},
    )
    return environment, backend


def session_factory(config: runner.AutoRunnerConfig, environment: VoiceEntryEnvironment):
    agent = RecordingAgent()
    conversation = ConversationManager(agent, router=LocalChatRouter())
    session = runner.AutoVoiceSession(
        conversation=conversation,
        environment=environment,
        config=config,
        source_instance_id="p9-auto-test",
    )
    return agent, session


class BurstStream(FakeCaptureStream):
    """在 ``start()`` 里一次性投递全部校准帧（模拟设备回调，不启动线程）。"""

    def __init__(
        self, chunks: list[bytes], *, frames: int = SAMPLE_FRAMES, status: object = None
    ) -> None:
        super().__init__()
        self._chunks = list(chunks)
        self._frames = frames
        self._status = status

    def start(self) -> None:
        super().start()
        for payload in self._chunks:
            self.emit(payload, frames=self._frames, status=self._status)


class SplitBackend:
    """第一次采集返回校准突发流，其后返回按节拍投递的试听流。"""

    def __init__(self, calibration: list[bytes], turn: list[bytes]) -> None:
        self._calibration = calibration
        self._turn = turn
        self.calibration_stream: BurstStream | None = None
        self.turn_stream: PacedStream | None = None
        self.open_count = 0

    @property
    def stream(self) -> PacedStream | None:
        """``_paced_run_turn`` 需要的节拍流视图。"""

        return self.turn_stream

    def check_device(self, selection, *, sample_rate_hz: int, channel_count: int) -> DeviceCheckResult:
        del selection, sample_rate_hz, channel_count
        return DeviceCheckResult(DeviceCheckStatus.READY, "mic-1", "ready")

    def open_input_stream(self, **kwargs: object):
        callback = kwargs["callback"]
        self.open_count += 1
        if self.open_count == 1:
            stream: FakeCaptureStream = BurstStream(self._calibration)
            self.calibration_stream = stream  # type: ignore[assignment]
        else:
            stream = PacedStream(self._turn)
            self.turn_stream = stream  # type: ignore[assignment]
        stream.callback = callback  # type: ignore[assignment]
        return stream


def build_split_environment(
    calibration_chunks: list[bytes], turn_chunks: list[bytes]
) -> tuple[VoiceEntryEnvironment, SplitBackend]:
    """校准帧与试听帧分开注入的离线环境（不打开真实设备）。"""

    _OfflineRecognizer.recognizer = _Recognizer(FAKE_RECOGNIZED_TEXT)
    module = types.ModuleType("sherpa_onnx")
    module.OfflineRecognizer = _OfflineRecognizer  # type: ignore[attr-defined]
    provider = SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
        module_loader=lambda: module,
    )
    backend = SplitBackend(calibration_chunks, turn_chunks)

    def capture_factory(bus) -> CaptureSession:
        return CaptureSession(
            bus=bus,
            backend=backend,  # type: ignore[arg-type]
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(),
        )

    environment = VoiceEntryEnvironment(
        capture_factory=capture_factory,
        provider=provider,
        speaker=None,
        devices=("Fake Mic",),
        tts_mode="none",
        details={"device": "Fake Mic", "device_id": "mic-1", "model": "fake.onnx"},
    )
    return environment, backend


class PacingMixin:
    """预留：把 PacedStream 接进驱动的 pump 调用（当前由 make_run_turn 完成）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._paced: PacedStream | None = None


class ConfigTests(unittest.TestCase):
    def test_positive_values_are_required(self) -> None:
        for kwargs in (
            {"turns": 0},
            {"silence_ms": 0},
            {"speech_start_timeout_ms": -1},
            {"max_record_seconds": 0},
            {"speech_threshold": 0.0},
            {"device_id": ""},
            {"edge_voice": ""},
            {"tts_mode": "legacy"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_defaults_match_the_documented_auto_capture_values(self) -> None:
        config = runner.AutoRunnerConfig()
        self.assertEqual(config.turns, 2)
        self.assertEqual(config.tts_mode, "ws_pcm")
        self.assertEqual(config.silence_ms, 2500)
        self.assertEqual(config.speech_start_timeout_ms, 8000)
        self.assertEqual(config.speech_threshold, 600.0)
        self.assertEqual(config.max_record_seconds, 15)


class OptInTests(unittest.TestCase):
    def test_without_live_opt_in_exits_two_and_touches_nothing(self) -> None:
        with mock.patch.object(runner, "build_environment") as builder:
            with redirect_stdout(io.StringIO()):
                code = runner.main(["--check-only"])
        self.assertEqual(code, 2)
        builder.assert_not_called()

    def test_invalid_arguments_exit_with_argparse_error_before_assembly(self) -> None:
        with mock.patch.object(runner, "build_environment") as builder:
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(io.StringIO()):
                    runner.main(["--live-opt-in", "--check-only", "--silence-ms", "0"])
        self.assertEqual(ctx.exception.code, 2)
        builder.assert_not_called()

    def test_check_only_never_writes_an_artifact(self) -> None:
        environment, _backend = build_environment([silence_payload()])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(runner, "build_environment", return_value=environment):
                with mock.patch.object(runner, "build_session") as builder:
                    with redirect_stdout(io.StringIO()):
                        code = runner.main(
                            [
                                "--live-opt-in",
                                "--check-only",
                                "--tts-mode",
                                "none",
                                "--artifact-root",
                                str(root),
                            ]
                        )
            self.assertEqual(code, 0)
            builder.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])


class ArtifactTests(unittest.TestCase):
    def _runner_main(self, environment: VoiceEntryEnvironment, backend: PacedBackend):
        def fake_session(config, env):
            return session_factory(config, env)

        def runner_main(argv: list[str]):
            real_run_turn = runner.AutoVoiceSession.run_turn
            pumped = _paced_run_turn(backend)

            def bound(self, *, on_listening=None, on_speech_started=None, hud=None):
                return pumped(
                    self, on_listening=on_listening, on_speech_started=on_speech_started
                )

            runner.AutoVoiceSession.run_turn = bound  # type: ignore[assignment]
            try:
                with mock.patch.object(runner, "build_environment", return_value=environment):
                    with mock.patch.object(runner, "build_session", side_effect=fake_session):
                        # 离线测试不创建真实提示窗（HUD 由专项测试与真机验收覆盖）。
                        with mock.patch.object(runner, "_build_hud", return_value=None):
                            with mock.patch.object(runner, "_ask_user_heard", return_value="yes"):
                                with redirect_stdout(io.StringIO()):
                                    return runner.main(argv)
            finally:
                runner.AutoVoiceSession.run_turn = real_run_turn  # type: ignore[assignment]

        return runner_main

    def test_artifact_records_counts_and_hash_without_text(self) -> None:
        # 3 帧环境静音 → 20 帧语音 → 尾部静音超过 silence_ms(2500ms=125 帧) 触发自动结束。
        chunks = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_environment(chunks)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--vad-mode",
                "energy",
                "--artifact-root",
                str(root),
            ]
            runner_main = self._runner_main(environment, backend)
            code = runner_main(argv)
            self.assertEqual(code, 0)
            files = list(root.glob("*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["evidence_kind"], "p9_auto_voice_loop_acceptance")
        self.assertTrue(payload["live_opt_in"])
        self.assertEqual(payload["tts_mode"], "none")
        self.assertEqual(payload["user_heard"], "yes")
        self.assertIs(payload["boundary"]["button_required"], False)
        self.assertIs(payload["boundary"]["recognized_text_persisted"], False)
        self.assertIs(payload["boundary"]["computer_actions_executed"], False)
        self.assertIs(payload["boundary"]["audio_written_to_disk"], False)
        self.assertEqual(len(payload["turns"]), 1)
        record = payload["turns"][0]
        self.assertTrue(record["submitted"])
        self.assertEqual(record["end_reason"], "silence_timeout")
        self.assertEqual(record["asr_text_length"], len(FAKE_RECOGNIZED_TEXT))
        self.assertEqual(record["asr_text_sha256"], expected_sha256(FAKE_RECOGNIZED_TEXT))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(FAKE_RECOGNIZED_TEXT, serialized)
        self.assertNotIn("回复：", serialized)
        self.assertNotIn("pcm", serialized.lower())
        self.assertIn("auto_capture", payload)


class RecordingHud:
    """只记录收到的状态与生命周期调用；不建窗、不碰任何系统资源。"""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.ticks = 0
        self.hidden = 0
        self.destroyed = 0

    def set_state(self, state: object, *, turn_index: int | None = None) -> str:
        del turn_index
        self.states.append(getattr(state, "value", str(state)))
        return self.states[-1]

    def tick(self) -> int:
        self.ticks += 1
        return 0

    def hide(self) -> None:
        self.hidden += 1

    def destroy(self) -> None:
        self.destroyed += 1


class HudWiringTests(unittest.TestCase):
    """回归：``main`` 必须把建好的提示窗一路带进轮次，绝不允许中途被覆盖成 ``None``。

    真机缺陷（2026-10-01）：``main`` 在 ``_build_hud()`` 之后又写了一次
    ``hud: Any | None = None``，窗口因此永远停在「待机」——所有状态更新与阶段钩子都变成
    空操作，且退出时不再隐藏/销毁窗口。这里用注入记录器把"窗口实际收到的状态序列"钉死。
    """

    def _run_with_recorder(
        self, *, chunks: list[bytes] | None = None, extra_argv: tuple[str, ...] = (),
        hud_object: object | None = None,
    ) -> tuple[int, RecordingHud, dict]:
        """跑一整轮 ``main()``，返回退出码、窗口调用记录与 artifact 载荷。"""

        if chunks is None:
            # 3 帧环境静音 → 20 帧语音 → 尾部静音超过 silence_ms(2500ms=125 帧) 触发自动结束。
            chunks = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_environment(chunks)
        recorder = RecordingHud()
        real_run_turn = runner.AutoVoiceSession.run_turn
        pumped = _paced_run_turn(backend)

        def bound(self, *, on_listening=None, on_speech_started=None, hud=None):
            return pumped(
                self,
                on_listening=on_listening,
                on_speech_started=on_speech_started,
                hud=hud,
            )

        def fake_session(config, env):
            return session_factory(config, env)

        runner.AutoVoiceSession.run_turn = bound  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as raw:
                with mock.patch.object(runner, "build_environment", return_value=environment):
                    with mock.patch.object(runner, "build_session", side_effect=fake_session):
                        with mock.patch.object(
                            runner, "_build_hud", return_value=recorder if hud_object is None else hud_object
                        ):
                            with mock.patch.object(runner, "_ask_user_heard", return_value="yes"):
                                with redirect_stdout(io.StringIO()):
                                    code = runner.main(
                                        [
                                            "--live-opt-in",
                                            "--turns",
                                            "1",
                                            "--tts-mode",
                                            "none",
                                            "--vad-mode",
                                            "energy",
                                            "--artifact-root",
                                            raw,
                                            *extra_argv,
                                        ]
                                    )
                payload = json.loads(
                    next(Path(raw).glob("*.json")).read_text(encoding="utf-8")
                )
        finally:
            runner.AutoVoiceSession.run_turn = real_run_turn  # type: ignore[assignment]
        return code, recorder, payload

    def test_main_delivers_hud_states_through_the_whole_turn(self) -> None:
        code, recorder, _payload = self._run_with_recorder()
        self.assertEqual(code, 0)
        # 建窗后立刻置「待机」，随后每个真实阶段都必须到达窗口。
        self.assertEqual(recorder.states[0], "idle")
        for state in ("listening", "hearing", "transcribing", "thinking"):
            self.assertIn(state, recorder.states)
        self.assertEqual(recorder.states[-1], "stopped")
        self.assertGreaterEqual(recorder.ticks, 1)
        self.assertEqual(recorder.hidden, 1)
        self.assertEqual(recorder.destroyed, 1)

    def test_hud_timeline_lands_in_the_artifact_with_internal_states(self) -> None:
        """阶段级时间线（内部 9 态 + 毫秒）必须落进 artifact，用于复核真实阶段耗时。"""

        code, _recorder, payload = self._run_with_recorder()
        self.assertEqual(code, 0)
        timeline = payload["turns"][0]["hud_timeline"]
        states = [entry["state"] for entry in timeline["entries"]]
        self.assertEqual(states[0], "listening")
        for state in ("listening", "hearing", "transcribing", "thinking", "speaking"):
            self.assertIn(state, states)
        # 相邻重复状态已去重。
        self.assertTrue(all(a != b for a, b in zip(states, states[1:])))
        self.assertEqual(set(timeline["durations_ms"]), set(states))
        self.assertTrue(all(value >= 0 for value in timeline["durations_ms"].values()))
        self.assertFalse(timeline["truncated"])
        # 时间线只含状态名与毫秒，绝不携带识别/回复正文。
        serialized = json.dumps(timeline, ensure_ascii=False)
        self.assertNotIn(FAKE_RECOGNIZED_TEXT, serialized)

    def test_hud_timeline_exists_even_without_a_window(self) -> None:
        """``--no-hud`` 时也要有时间线：诊断不应依赖窗口是否创建成功。"""

        code, recorder, payload = self._run_with_recorder(
            extra_argv=("--no-hud",), hud_object=None
        )
        self.assertEqual(code, 0)
        self.assertEqual(recorder.states, [])
        timeline = payload["turns"][0]["hud_timeline"]
        self.assertIn("listening", [entry["state"] for entry in timeline["entries"]])

    def test_missing_speech_channel_is_not_reported_as_failure(self) -> None:
        """``--tts-mode none`` 只是"本轮不播报"，绝不是失败：窗口不得闪出「失败」。

        真机缺陷（2026-10-01）：``speak_with_hud`` 把 ``_speak`` 返回的
        ``(False, "text_only")`` 当成播报失败，于是成功的一轮会先显示「播报」再闪「失败」。
        只有 ``(False, "speech_failed")`` 才是真实播报失败。
        """

        code, recorder, _payload = self._run_with_recorder()
        self.assertEqual(code, 0)
        self.assertIn("speaking", recorder.states)
        self.assertNotIn("failed", recorder.states)
        self.assertEqual(recorder.states[-1], "stopped")


class SpeechOutcomeLabelTests(unittest.TestCase):
    """播报结果到窗口文案的映射：真实失败必须显示「失败」，没通道不许显示「失败」。"""

    def _labels_for(self, result: object) -> list[str]:
        recorder = RecordingHud()
        loop = mock.Mock()
        loop._speaker = None
        loop._speak = lambda text: result  # noqa: ARG005 - 只需要固定返回值
        runner._wrap_loop_for_hud(loop, recorder, index=1)
        loop._speak("你好")  # 触发被包装后的真实动作
        return recorder.states

    def test_real_speech_failure_still_shows_failed(self) -> None:
        self.assertEqual(self._labels_for((False, "speech_failed")), ["speaking", "failed"])

    def test_successful_speech_returns_to_listening(self) -> None:
        self.assertEqual(self._labels_for((True, "speech_finished")), ["speaking", "listening"])

    def test_missing_channel_is_not_a_failure(self) -> None:
        self.assertEqual(self._labels_for((False, "text_only")), ["speaking", "listening"])

    def test_malformed_speak_result_falls_back_to_failure(self) -> None:
        self.assertEqual(self._labels_for("unexpected"), ["speaking", "failed"])


class HudTimelineTests(unittest.TestCase):
    """阶段时间线的纯本地契约：去重、上限、耗时累计、非字符串忽略、代理转发。"""

    def _timeline(self) -> tuple[runner.HudTimeline, list[float]]:
        ticks = [0.0]

        def clock() -> float:
            return ticks[0]

        return runner.HudTimeline(clock=clock), ticks

    def test_records_only_state_changes_and_accumulates_durations(self) -> None:
        timeline, ticks = self._timeline()
        timeline.reset()
        timeline.record("listening")
        ticks[0] = 0.5
        timeline.record("listening")  # 相邻重复 → 去重
        ticks[0] = 1.0
        timeline.record("thinking")
        ticks[0] = 2.5
        snapshot = timeline.snapshot()
        self.assertEqual([e["state"] for e in snapshot["entries"]], ["listening", "thinking"])
        self.assertEqual(snapshot["entries"][0]["t_ms"], 0)
        self.assertEqual(snapshot["entries"][1]["t_ms"], 1000)
        self.assertEqual(snapshot["durations_ms"], {"listening": 1000, "thinking": 1500})
        self.assertFalse(snapshot["truncated"])

    def test_reset_clears_entries_and_restarts_the_clock(self) -> None:
        timeline, ticks = self._timeline()
        timeline.reset()
        timeline.record("listening")
        ticks[0] = 10.0
        timeline.reset()  # 重新起算：上一轮条目必须清空，且时间基准回到 reset 时刻
        ticks[0] = 10.25
        timeline.record("hearing")
        snapshot = timeline.snapshot()
        self.assertEqual([e["state"] for e in snapshot["entries"]], ["hearing"])
        self.assertEqual(snapshot["entries"][0]["t_ms"], 250)

    def test_ignores_non_string_and_empty_states(self) -> None:
        timeline, _ticks = self._timeline()
        timeline.reset()
        for bad in (None, 3, "", object(), ("listening",)):
            timeline.record(bad)
        self.assertEqual(timeline.snapshot()["entries"], [])

    def test_entries_are_capped_and_flagged(self) -> None:
        timeline, _ticks = self._timeline()
        timeline.reset()
        for index in range(runner.HudTimeline.MAX_ENTRIES + 10):
            timeline.record(f"s{index}")
        snapshot = timeline.snapshot()
        self.assertEqual(len(snapshot["entries"]), runner.HudTimeline.MAX_ENTRIES)
        self.assertTrue(snapshot["truncated"])

    def test_proxy_records_and_forwards_with_or_without_a_window(self) -> None:
        from app.ui.voice_hud import VoiceHudState

        recorder = RecordingHud()
        timeline, _ticks = self._timeline()
        proxied = runner._TimelineHud(recorder, timeline)
        proxied.set_state(VoiceHudState.LISTENING, turn_index=1)
        self.assertEqual(recorder.states, ["listening"])
        self.assertEqual([e["state"] for e in timeline.snapshot()["entries"]], ["listening"])
        self.assertEqual(timeline.snapshot()["durations_ms"], {"listening": 0})

        bare_timeline, _ticks2 = self._timeline()
        bare = runner._TimelineHud(None, bare_timeline)
        bare.set_state(VoiceHudState.SPEAKING)
        self.assertEqual(bare.tick(), 0)
        bare.hide()
        bare.destroy()
        self.assertEqual([e["state"] for e in bare_timeline.snapshot()["entries"]], ["speaking"])


class HudTickDuringBlockingPhasesTests(unittest.TestCase):
    """§2-② 回归：ASR/Agent 阻塞段前后必须各抽一次窗口消息（窗口不建线程，见模块硬边界）。"""

    def test_ticks_bracket_every_blocking_phase(self) -> None:
        log: list[str] = []

        class LoggingHud(RecordingHud):
            def set_state(self, state: object, *, turn_index: int | None = None) -> str:
                result = super().set_state(state, turn_index=turn_index)
                log.append(f"set:{self.states[-1]}")
                return result

            def tick(self) -> int:
                log.append("tick")
                return super().tick()

        hud = LoggingHud()
        loop = mock.Mock()
        loop.release_input = lambda: log.append("release") or "released"
        loop.complete_turn = lambda: log.append("complete") or "completed"
        loop._speaker = None
        loop._speak = lambda text: (False, "text_only")  # noqa: ARG005
        runner._wrap_loop_for_hud(loop, hud, index=1)
        loop.release_input()
        loop.complete_turn()
        self.assertEqual(log[:4], ["set:transcribing", "tick", "release", "tick"])
        self.assertEqual(log[4:7], ["set:thinking", "tick", "complete"])
        self.assertEqual(log[7], "tick")


def _paced_run_turn(backend: PacedBackend):
    """在真实 ``AutoVoiceSession.run_turn`` 之上按帧节拍投递合成帧。

    直接替换模块级 ``run_turn`` 会与 ``main`` 内部调用同名属性产生递归，因此这里把
    真正的类方法取出，只在调用期间临时给 ``ChatVoiceLoop`` 实例挂上"先投一帧再推进"
    的 pump 包装。
    """

    real_run_turn = runner.AutoVoiceSession.run_turn

    def pumped(self, *, on_listening=None, on_speech_started=None, hud=None):
        loop = self._loop
        original_pump = type(loop).pump

        def pump_with_frame() -> int:
            # 延迟取流：auto 模式的第一轮采集流要等到 start_turn 之后才建立。
            stream = backend.stream
            if stream is not None:
                stream.pump()
            return original_pump(loop)

        loop.pump = pump_with_frame  # type: ignore[assignment]
        try:
            return real_run_turn(
                self,
                on_listening=on_listening,
                on_speech_started=on_speech_started,
                hud=hud,
            )
        finally:
            del loop.pump  # type: ignore[attr-defined]

    return pumped


class ThresholdModeConfigTests(unittest.TestCase):
    def test_defaults_stay_fixed_and_do_not_change_existing_behaviour(self) -> None:
        config = runner.AutoRunnerConfig()
        self.assertEqual(config.threshold_mode, "fixed")
        self.assertEqual(config.threshold_multiplier, 3.0)
        self.assertEqual(config.speech_threshold, 600.0)

    def test_invalid_threshold_options_are_rejected(self) -> None:
        for kwargs in (
            {"threshold_mode": "adaptive"},
            {"threshold_mode": ""},
            {"threshold_mode": None},
            {"threshold_multiplier": 1.0},
            {"threshold_multiplier": 0.5},
            {"threshold_multiplier": float("nan")},
            {"threshold_multiplier": float("inf")},
            {"threshold_multiplier": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_cli_rejects_bad_threshold_mode_and_multiplier(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            runner._parse_args(["--threshold-mode", "adaptive"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx:
            runner._parse_args(["--threshold-multiplier", "1.0"])
        self.assertEqual(ctx.exception.code, 2)
        parsed = runner._parse_args([])
        self.assertEqual(parsed.threshold_mode, "fixed")
        self.assertEqual(parsed.threshold_multiplier, 3.0)


class ThresholdCalibrationTests(unittest.TestCase):
    def test_calibration_uses_quiet_noise_and_clears_the_bus_residue(self) -> None:
        calibration = [noise_payload(amplitude=150)] * 60
        environment, backend = build_split_environment(calibration, [])
        seen: list[object] = []
        original = environment.capture_factory

        def recording_factory(bus):
            seen.append(bus)
            return original(bus)

        environment.capture_factory = recording_factory  # type: ignore[assignment]
        decision = runner.calibrate_speech_threshold(environment, multiplier=3.0)
        self.assertAlmostEqual(decision.threshold_rms, 450.0)
        self.assertEqual(decision.source, "adaptive")
        self.assertEqual(decision.noise.frame_count, 60)
        self.assertAlmostEqual(decision.noise.p95_rms, 150.0)
        self.assertEqual(len(seen), 1)
        bus = seen[0]
        self.assertIs(bus.state, AudioBusState.CLOSED)  # type: ignore[attr-defined]
        self.assertEqual(bus.queued_frames, 0)  # type: ignore[attr-defined]
        self.assertTrue(backend.calibration_stream.stopped)  # type: ignore[union-attr]
        self.assertTrue(backend.calibration_stream.closed)  # type: ignore[union-attr]

    def test_silent_noise_falls_back_to_the_floor_in_auto_mode(self) -> None:
        environment, _backend = build_split_environment([silence_payload()] * 60, [])
        decision = runner.calibrate_speech_threshold(environment, multiplier=3.0)
        self.assertEqual(decision.threshold_rms, 200.0)
        self.assertEqual(decision.source, "floor")

    def test_noise_too_high_is_rejected_without_falling_back_to_600(self) -> None:
        environment, _backend = build_split_environment(
            [noise_payload(amplitude=4000)] * 60, []
        )
        with self.assertRaises(runner.ThresholdCalibrationError) as ctx:
            runner.calibrate_speech_threshold(environment, multiplier=3.0)
        self.assertIn("noise_too_high", str(ctx.exception))
        self.assertNotIn("600", str(ctx.exception))

    def test_closed_bus_during_sampling_is_a_calibration_failure(self) -> None:
        environment, backend = build_split_environment([], [])

        def sleep(seconds: float) -> None:
            del seconds
            stream = backend.calibration_stream
            if stream is not None and not stream.stopped:
                # 采样过程中设备回调报错：采集会话以 discard=True 关闭总线。
                stream.emit(silence_payload(), frames=SAMPLE_FRAMES, status="overflow")

        with self.assertRaises(runner.ThresholdCalibrationError) as ctx:
            runner.calibrate_speech_threshold(environment, multiplier=3.0, sleep=sleep)
        self.assertIn("总线", str(ctx.exception))

    def test_sampling_deadline_reports_insufficient_frames(self) -> None:
        environment, _backend = build_split_environment([], [])
        ticks = {"now": 0.0}

        def clock() -> float:
            return ticks["now"]

        def sleep(seconds: float) -> None:
            ticks["now"] += seconds

        with self.assertRaises(runner.ThresholdCalibrationError) as ctx:
            runner.calibrate_speech_threshold(
                environment, multiplier=3.0, clock=clock, sleep=sleep
            )
        self.assertIn("静音采样超时", str(ctx.exception))
        self.assertIn("0/60", str(ctx.exception))

    def test_missing_capture_factory_is_rejected(self) -> None:
        environment, _backend = build_split_environment([], [])
        environment.capture_factory = None  # type: ignore[assignment]
        with self.assertRaises(runner.ThresholdCalibrationError):
            runner.calibrate_speech_threshold(environment, multiplier=3.0)


class AutoThresholdSessionTests(unittest.TestCase):
    def make_session(self, *, amplitude: int = 150, frames: int = 60):
        environment, backend = build_split_environment(
            [noise_payload(amplitude=amplitude)] * frames, [silence_payload()]
        )
        config = runner.AutoRunnerConfig(
            threshold_mode="auto", tts_mode="none", turns=1, vad_mode="energy"
        )
        _agent, session = session_factory(config, environment)
        return session, backend, config

    def test_auto_mode_passes_the_calibrated_threshold_to_the_driver(self) -> None:
        session, backend, config = self.make_session()
        try:
            self.assertAlmostEqual(session._driver.config.speech_threshold, 450.0)
            self.assertEqual(session._driver.config.silence_ms, config.silence_ms)
            details = session.threshold_details()
            self.assertEqual(details["source"], "adaptive")
            self.assertAlmostEqual(details["threshold_rms"], 450.0)
            self.assertIsNotNone(session.threshold_decision)
            # 校准只开一次采集，且发生在第一轮开始之前。
            self.assertEqual(backend.open_count, 1)
            self.assertTrue(backend.calibration_stream.stopped)
            self.assertTrue(backend.calibration_stream.closed)
            self.assertIsNone(backend.turn_stream)
        finally:
            session.close()

    def test_fixed_mode_never_calibrates_and_keeps_the_default_threshold(self) -> None:
        environment, backend = build_split_environment([], [silence_payload()])
        config = runner.AutoRunnerConfig(tts_mode="none", vad_mode="energy")
        with mock.patch.object(runner, "calibrate_speech_threshold") as calibrate:
            _agent, session = session_factory(config, environment)
        try:
            calibrate.assert_not_called()
            self.assertEqual(backend.open_count, 0)
            self.assertEqual(session._driver.config.speech_threshold, 600.0)
            self.assertIsNone(session.threshold_decision)
            self.assertEqual(session.threshold_details(), {})
        finally:
            session.close()

    def test_noise_too_high_aborts_session_assembly(self) -> None:
        environment, backend = build_split_environment(
            [noise_payload(amplitude=4000)] * 60, [silence_payload()]
        )
        config = runner.AutoRunnerConfig(
            threshold_mode="auto", tts_mode="none", turns=1, vad_mode="energy"
        )
        with self.assertRaises(runner.ThresholdCalibrationError):
            session_factory(config, environment)
        self.assertTrue(backend.calibration_stream.closed)  # type: ignore[union-attr]
        self.assertIsNone(backend.turn_stream)


class AutoModeMainTests(unittest.TestCase):
    """``main`` 级别的 auto 模式离线验收：阈值来源、打印事实、失败退出码 3。"""

    def _runner_main(self, environment: VoiceEntryEnvironment, backend: SplitBackend, captured: list[str]):
        def fake_session(config, env):
            return session_factory(config, env)

        def runner_main(argv: list[str]) -> int:
            real_run_turn = runner.AutoVoiceSession.run_turn
            pumped = _paced_run_turn(backend)  # type: ignore[arg-type]

            def bound(self, *, on_listening=None, on_speech_started=None, hud=None):
                return pumped(
                    self, on_listening=on_listening, on_speech_started=on_speech_started
                )

            runner.AutoVoiceSession.run_turn = bound  # type: ignore[assignment]
            buffer = io.StringIO()
            try:
                with mock.patch.object(runner, "build_environment", return_value=environment):
                    with mock.patch.object(runner, "build_session", side_effect=fake_session):
                        with mock.patch.object(runner, "_build_hud", return_value=None):
                            with mock.patch.object(runner, "_ask_user_heard", return_value="yes"):
                                with redirect_stdout(buffer):
                                    code = runner.main(argv)
            finally:
                runner.AutoVoiceSession.run_turn = real_run_turn  # type: ignore[assignment]
            captured.append(buffer.getvalue())
            return code

        return runner_main

    def test_auto_mode_runs_a_turn_and_records_calibration_facts(self) -> None:
        calibration = [noise_payload(amplitude=150)] * 60
        turn = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_split_environment(calibration, turn)
        captured: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--threshold-mode",
                "auto",
                "--vad-mode",
                "energy",
                "--artifact-root",
                str(root),
            ]
            code = self._runner_main(environment, backend, captured)(argv)
            self.assertEqual(code, 0)
            files = list(root.glob("*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            output = captured[0]
        facts = payload["auto_capture"]
        self.assertEqual(facts["threshold_mode"], "auto")
        self.assertEqual(facts["threshold_source"], "adaptive")
        self.assertAlmostEqual(facts["speech_threshold_rms"], 450.0)
        self.assertEqual(facts["noise_frame_count"], 60)
        self.assertAlmostEqual(facts["noise_p50_rms"], 150.0)
        self.assertAlmostEqual(facts["noise_p95_rms"], 150.0)
        self.assertAlmostEqual(facts["noise_max_rms"], 150.0)
        self.assertTrue(payload["turns"][0]["submitted"])
        self.assertIn("source=adaptive", output)
        self.assertIn("threshold=450", output)
        self.assertIn("p50=150", output)
        self.assertNotIn("pcm", json.dumps(payload, ensure_ascii=False).lower())

    def test_auto_mode_calibration_failure_exits_three_with_hints(self) -> None:
        environment, backend = build_split_environment(
            [noise_payload(amplitude=4000)] * 60, [silence_payload()]
        )
        errors = io.StringIO()
        captured: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--threshold-mode",
                "auto",
                "--vad-mode",
                "energy",
                "--artifact-root",
                str(root),
            ]
            with redirect_stderr(errors):
                code = self._runner_main(environment, backend, captured)(argv)
            self.assertEqual(code, 3)
            self.assertEqual(list(root.iterdir()), [])
        message = errors.getvalue()
        self.assertIn("环境自适应阈值校准失败", message)
        self.assertIn("音乐", message)
        self.assertIn("风扇", message)
        self.assertIn("noise_too_high", message)
        self.assertIsNone(backend.turn_stream)

    def test_fixed_mode_is_untouched_and_never_calibrates(self) -> None:
        turn = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_environment(turn)
        captured: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--vad-mode",
                "energy",
                "--artifact-root",
                str(root),
            ]
            with mock.patch.object(runner, "calibrate_speech_threshold") as calibrate:
                code = self._runner_main(environment, backend, captured)(argv)
            calibrate.assert_not_called()
            self.assertEqual(code, 0)
            payload = json.loads(next(root.glob("*.json")).read_text(encoding="utf-8"))
            self.assertNotIn("threshold_mode", payload["auto_capture"])
            self.assertEqual(payload["auto_capture"]["speech_threshold_rms"], 600.0)
            self.assertNotIn("阈值模式", captured[0])
            self.assertNotIn("环境自适应阈值校准", captured[0])

    def test_check_only_in_auto_mode_never_samples_or_assembles(self) -> None:
        environment, backend = build_split_environment([], [])
        with mock.patch.object(runner, "build_environment", return_value=environment):
            with mock.patch.object(runner, "build_session") as builder:
                with mock.patch.object(runner, "calibrate_speech_threshold") as calibrate:
                    with redirect_stdout(io.StringIO()):
                        code = runner.main(
                            ["--live-opt-in", "--check-only", "--threshold-mode", "auto"]
                        )
        self.assertEqual(code, 0)
        builder.assert_not_called()
        calibrate.assert_not_called()
        self.assertEqual(backend.open_count, 0)


class VadModeConfigTests(unittest.TestCase):
    """``--vad-mode`` / ``--vad-model`` 的默认值与参数校验（装配前即拒绝）。"""

    def test_silero_is_the_default_vad_mode(self) -> None:
        config = runner.AutoRunnerConfig()
        self.assertEqual(config.vad_mode, "silero")
        self.assertIsNone(config.vad_model_path)
        self.assertEqual(
            runner.DEFAULT_VAD_MODEL,
            RUNNER_PATH.parent.parent / "models" / "openwakeword" / "silero_vad.onnx",
        )

    def test_cli_defaults_and_rejections(self) -> None:
        parsed = runner._parse_args([])
        self.assertEqual(parsed.vad_mode, "silero")
        self.assertEqual(parsed.vad_model, str(runner.DEFAULT_VAD_MODEL))
        self.assertEqual(runner._parse_args(["--vad-mode", "energy"]).vad_mode, "energy")
        with self.assertRaises(SystemExit) as ctx:
            runner._parse_args(["--vad-mode", "kws"])
        self.assertEqual(ctx.exception.code, 2)

    def test_invalid_vad_options_are_rejected(self) -> None:
        for kwargs in (
            {"vad_mode": "kws"},
            {"vad_mode": ""},
            {"vad_mode": None},
            {"vad_model_path": ""},
            {"vad_model_path": 3},
            {"vad_model_path": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_vad_model_path_accepts_string_and_path(self) -> None:
        self.assertEqual(
            runner.AutoRunnerConfig(vad_model_path="a/b.onnx").vad_model_path, Path("a/b.onnx")
        )
        self.assertEqual(
            runner.AutoRunnerConfig(vad_model_path=Path("c.onnx")).vad_model_path, Path("c.onnx")
        )

    def test_vad_min_silence_defaults_to_500ms_and_is_validated(self) -> None:
        """待修 B：段级端点宽限（Silero ``min_silence_duration``）由 CLI 可调。"""

        self.assertEqual(runner.AutoRunnerConfig().vad_min_silence_ms, 500)
        self.assertEqual(runner._parse_args([]).vad_min_silence_ms, 500)
        parsed = runner._parse_args(["--vad-min-silence-ms", "800"])
        self.assertEqual(parsed.vad_min_silence_ms, 800)
        for bad in ("--vad-min-silence-ms", "0"):
            with self.subTest(argv=bad):
                with self.assertRaises(SystemExit) as ctx:
                    runner._parse_args([bad, "0"])
                self.assertEqual(ctx.exception.code, 2)
        for kwargs in (
            {"vad_min_silence_ms": 0},
            {"vad_min_silence_ms": -1},
            {"vad_min_silence_ms": True},
            {"vad_min_silence_ms": 0.5},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_silero_session_wires_segment_endpoint_and_min_silence(self) -> None:
        """装配期接线：驱动必须拿到段级探测/定稿钩子，且宽限值进入 SileroVadConfig。"""

        environment, _backend = build_environment([silence_payload()])
        detector = FakeSileroDetector()
        captured: dict[str, object] = {}

        def builder(*, config: object = None) -> object:
            captured["config"] = config
            return detector

        config = runner.AutoRunnerConfig(vad_min_silence_ms=800)
        with mock.patch.object(
            silero_module, "build_silero_vad_detector", side_effect=builder
        ):
            _agent, session = session_factory(config, environment)
        silero_config = captured["config"]
        self.assertAlmostEqual(silero_config.min_silence_duration, 0.8)  # type: ignore[attr-defined]
        driver_config = session._driver.config  # noqa: SLF001 - 只读断言接线
        # 绑定方法是每次访问新建的对象，因此比较 ``__func__`` 与 ``__self__``。
        self.assertIs(driver_config.segment_probe.__self__, detector)  # type: ignore[union-attr]
        self.assertIs(
            driver_config.segment_probe.__func__,  # type: ignore[union-attr]
            FakeSileroDetector.segment_pending,
        )
        self.assertIs(driver_config.segment_flush.__self__, detector)  # type: ignore[union-attr]
        self.assertTrue(session.vad_details()["segment_api_available"])


class FakeSileroDetector:
    """Silero 检测器注入替身：不加载真实模型、不导入 sherpa-onnx。"""

    def __init__(self, *, hit: bool = False, pending_segment: bool = False) -> None:
        self.hit = hit
        self.seen = 0
        self.pending_segment = pending_segment
        self.flush_calls = 0

    def observe(self, packet: object) -> bool:
        del packet
        self.seen += 1
        return self.hit

    def segment_pending(self) -> bool:
        """段级探测替身：``empty()`` 取反的等价物。"""

        return bool(self.pending_segment)

    def flush_segment(self) -> None:
        self.flush_calls += 1

    def to_details(self) -> dict[str, object]:
        return {
            "mode": "silero",
            "model_path": "models/openwakeword/silero_vad.onnx",
            "model_name": "silero_vad.onnx",
            "model_present": True,
            "threshold": 0.5,
            "min_silence_duration": 0.5,
            "min_speech_duration": 0.15,
            "speech_frames_required": 2,
            "window_samples": 512,
            "sample_rate_hz": 16000,
            "buffer_size_in_seconds": 30.0,
            "injected": True,
            "segment_api_available": True,
            "segment_probe_errors": 0,
            "flush_calls": self.flush_calls,
        }


class SileroSessionTests(unittest.TestCase):
    """silero 模式的装配边界：检测器进入驱动、能量校准不生效、失败一律 fail closed。"""

    def _session(self, *, config, environment, fake):
        with mock.patch.object(
            silero_module, "build_silero_vad_detector", return_value=fake
        ) as builder:
            _agent, session = session_factory(config, environment)
        return session, builder

    def test_silero_mode_passes_the_detector_to_the_driver(self) -> None:
        environment, _backend = build_split_environment([], [silence_payload()])
        fake = FakeSileroDetector()
        config = runner.AutoRunnerConfig(tts_mode="none", turns=1)
        session, builder = self._session(config=config, environment=environment, fake=fake)
        try:
            builder.assert_called_once()
            built_config = builder.call_args.kwargs["config"]
            self.assertIsInstance(built_config, silero_module.SileroVadConfig)
            self.assertIsNone(built_config.model_path)
            detector = session._driver.config.speech_detector
            self.assertIsNotNone(detector)
            self.assertIs(getattr(detector, "_p9_vad_detector", None), fake)
            self.assertEqual(session.vad_mode, "silero")
            self.assertIs(session.vad_detector, fake)
            self.assertEqual(session.vad_details()["model_name"], "silero_vad.onnx")
            self.assertIsNone(session.threshold_decision)
            self.assertEqual(session.threshold_details(), {})
        finally:
            session.close()

    def test_silero_mode_ignores_energy_calibration(self) -> None:
        environment, backend = build_split_environment(
            [noise_payload(amplitude=150)] * 60, [silence_payload()]
        )
        fake = FakeSileroDetector()
        config = runner.AutoRunnerConfig(threshold_mode="auto", tts_mode="none", turns=1)
        with mock.patch.object(runner, "calibrate_speech_threshold") as calibrate:
            session, _builder = self._session(
                config=config, environment=environment, fake=fake
            )
        try:
            calibrate.assert_not_called()
            self.assertEqual(backend.open_count, 0)
            self.assertEqual(session._driver.config.speech_threshold, 600.0)
            self.assertIsNone(session.threshold_decision)
        finally:
            session.close()

    def test_energy_mode_never_builds_a_silero_detector(self) -> None:
        environment, _backend = build_split_environment([], [silence_payload()])
        config = runner.AutoRunnerConfig(tts_mode="none", vad_mode="energy")
        with mock.patch.object(silero_module, "build_silero_vad_detector") as builder:
            _agent, session = session_factory(config, environment)
        try:
            builder.assert_not_called()
            self.assertIsNone(session._driver.config.speech_detector)
            self.assertIsNone(session.vad_detector)
            self.assertEqual(session.vad_details(), {})
            self.assertEqual(session.vad_mode, "energy")
        finally:
            session.close()

    def test_vad_unavailable_carries_only_the_fixed_code(self) -> None:
        for code in ("model_missing", "dependency_missing", "init_failed", "detect_failed"):
            with self.subTest(code=code):
                environment, backend = build_split_environment([], [silence_payload()])
                config = runner.AutoRunnerConfig(tts_mode="none", turns=1)
                with mock.patch.object(
                    silero_module,
                    "build_silero_vad_detector",
                    side_effect=silero_module.SileroVadError(code),
                ):
                    with self.assertRaises(runner.VadUnavailableError) as ctx:
                        session_factory(config, environment)
                self.assertEqual(str(ctx.exception), code)
                self.assertIsNone(backend.turn_stream)

    def test_unknown_assembly_failure_collapses_to_init_failed(self) -> None:
        environment, _backend = build_split_environment([], [silence_payload()])
        config = runner.AutoRunnerConfig(tts_mode="none", turns=1)
        with mock.patch.object(
            silero_module,
            "build_silero_vad_detector",
            side_effect=RuntimeError("C:\\secret\\silero_vad.onnx raw audio"),
        ):
            with self.assertRaises(runner.VadUnavailableError) as ctx:
                session_factory(config, environment)
        self.assertEqual(str(ctx.exception), "init_failed")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("pcm", str(ctx.exception))


class VadModeMainTests(unittest.TestCase):
    """``main`` 级别的 silero 验收：VAD 事实打印、模型缺失退出 3、显式 energy 退回。"""

    def _runner_main(self, environment, backend, captured: list[str], errors: io.StringIO):
        def fake_session(config, env):
            return session_factory(config, env)

        def runner_main(argv: list[str]) -> int:
            real_run_turn = runner.AutoVoiceSession.run_turn
            pumped = _paced_run_turn(backend)

            def bound(self, *, on_listening=None, on_speech_started=None, hud=None):
                return pumped(
                    self, on_listening=on_listening, on_speech_started=on_speech_started
                )

            runner.AutoVoiceSession.run_turn = bound  # type: ignore[assignment]
            buffer = io.StringIO()
            try:
                with mock.patch.object(runner, "build_environment", return_value=environment):
                    with mock.patch.object(runner, "build_session", side_effect=fake_session):
                        with mock.patch.object(runner, "_build_hud", return_value=None):
                            with mock.patch.object(runner, "_ask_user_heard", return_value="yes"):
                                with redirect_stdout(buffer):
                                    with redirect_stderr(errors):
                                        code = runner.main(argv)
            finally:
                runner.AutoVoiceSession.run_turn = real_run_turn  # type: ignore[assignment]
            captured.append(buffer.getvalue())
            return code

        return runner_main

    def test_silero_mode_prints_facts_and_runs_a_turn(self) -> None:
        environment, backend = build_environment([speech_payload()] * 80)
        fake = FakeSileroDetector(hit=True)
        captured: list[str] = []
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--max-record-seconds",
                "1",
                "--no-speech-limit",
                "1",
                "--artifact-root",
                raw,
            ]
            with mock.patch.object(
                silero_module, "build_silero_vad_detector", return_value=fake
            ) as builder:
                code = self._runner_main(environment, backend, captured, errors)(argv)
            self.assertEqual(code, 0)
            self.assertNotIn("Silero VAD 不可用", errors.getvalue())
            builder.assert_called_once()
            payload = json.loads(next(Path(raw).glob("*.json")).read_text(encoding="utf-8"))
        output = captured[0]
        self.assertIn("VAD 模式：silero", output)
        self.assertIn("由 Silero VAD 判定，能量阈值不生效", output)
        self.assertIn("Silero VAD 检测器：model=silero_vad.onnx", output)
        self.assertIn("threshold=0.5", output)
        self.assertGreater(fake.seen, 0)
        facts = payload["auto_capture"]
        self.assertEqual(facts["vad_mode"], "silero")
        self.assertEqual(facts["vad_model"], "silero_vad.onnx")
        self.assertEqual(facts["vad_threshold"], 0.5)
        self.assertIs(facts["energy_threshold_effective"], False)
        self.assertEqual(facts["vad_window_samples"], 512)
        self.assertTrue(payload["turns"][0]["submitted"])
        self.assertEqual(payload["turns"][0]["end_reason"], "max_utterance_duration")
        self.assertNotIn("pcm", json.dumps(payload, ensure_ascii=False).lower())

    def test_silero_no_speech_hint_never_suggests_an_energy_threshold(self) -> None:
        environment, backend = build_environment([silence_payload()] * 5)
        fake = FakeSileroDetector(hit=False)
        captured: list[str] = []
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--speech-start-timeout-ms",
                "1",
                "--no-speech-limit",
                "1",
                "--artifact-root",
                raw,
            ]
            with mock.patch.object(
                silero_module, "build_silero_vad_detector", return_value=fake
            ):
                code = self._runner_main(environment, backend, captured, errors)(argv)
        self.assertEqual(code, 1)
        self.assertGreater(fake.seen, 0)
        self.assertIn("Silero VAD 未判为人声", captured[0])
        self.assertNotIn("--speech-threshold", captured[0])
        self.assertIn("没等到 Silero VAD 判为人声", errors.getvalue())

    def test_missing_silero_model_exits_three_without_listening(self) -> None:
        turn = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_environment(turn)
        captured: list[str] = []
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--artifact-root",
                raw,
            ]
            with mock.patch.object(
                silero_module,
                "build_silero_vad_detector",
                side_effect=silero_module.SileroVadError("model_missing"),
            ):
                code = self._runner_main(environment, backend, captured, errors)(argv)
            self.assertEqual(list(Path(raw).iterdir()), [])
        self.assertEqual(code, 3)
        message = errors.getvalue()
        self.assertIn("Silero VAD 不可用", message)
        self.assertIn("model_missing", message)
        self.assertIn("--vad-mode energy", message)
        self.assertIn("VAD 模式：silero", captured[0])
        self.assertNotIn("--- 第", captured[0])
        self.assertIsNone(backend.stream.callback)

    def test_explicit_energy_mode_ignores_the_broken_silero_model(self) -> None:
        turn = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_environment(turn)
        captured: list[str] = []
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            argv = [
                "--live-opt-in",
                "--turns",
                "1",
                "--tts-mode",
                "none",
                "--vad-mode",
                "energy",
                "--artifact-root",
                raw,
            ]
            with mock.patch.object(
                silero_module,
                "build_silero_vad_detector",
                side_effect=silero_module.SileroVadError("model_missing"),
            ) as builder:
                code = self._runner_main(environment, backend, captured, errors)(argv)
            self.assertEqual(code, 0)
            builder.assert_not_called()
            payload = json.loads(next(Path(raw).glob("*.json")).read_text(encoding="utf-8"))
        output = captured[0]
        self.assertNotIn("Silero VAD 不可用", errors.getvalue())
        self.assertIn("VAD 模式：energy", output)
        self.assertNotIn("Silero VAD 检测器", output)
        self.assertNotIn("vad_mode", payload["auto_capture"])
        self.assertEqual(payload["auto_capture"]["speech_threshold_rms"], 600.0)
        self.assertEqual(payload["turns"][0]["end_reason"], "silence_timeout")
        self.assertTrue(payload["turns"][0]["submitted"])


@unittest.skipUnless(
    runner.DEFAULT_VAD_MODEL.is_file() and importlib.util.find_spec("sherpa_onnx") is not None,
    "需要项目内 Silero 模型与 sherpa_onnx 才能做真机装配冒烟",
)
class SileroRealModelSessionTests(unittest.TestCase):
    """只读加载真机模型并确认它真的接进了驱动（不打开任何设备）。"""

    def test_real_detector_is_wired_into_the_driver(self) -> None:
        from app.audio.bus import AudioPacket
        from app.contracts import AudioFrame

        environment, _backend = build_split_environment([], [silence_payload()])
        config = runner.AutoRunnerConfig(tts_mode="none", turns=1)
        _agent, session = session_factory(config, environment)
        try:
            details = session.vad_details()
            self.assertEqual(details["model_name"], "silero_vad.onnx")
            self.assertIs(details["model_present"], True)
            self.assertIs(details["injected"], False)
            detector = session._driver.config.speech_detector
            self.assertIsNotNone(detector)
            self.assertIsInstance(
                getattr(detector, "_p9_vad_detector", None), silero_module.SileroVadDetector
            )
            frame = AudioFrame(
                audio_session_id="audio-silero-smoke",
                provider_generation=1,
                stream_id="stream-silero-smoke",
                event_sequence=1,
                frame_index=0,
                captured_at="2026-10-01T00:00:00Z",
                duration_ms=20,
            )
            for _ in range(4):
                self.assertFalse(detector(AudioPacket(frame, silence_payload())))
        finally:
            session.close()


class _MinimalCoordinator:
    """最简 coordinator 替身：只记录驱动注入的 VAD 工厂，不做采集。"""

    def __init__(self) -> None:
        self.started = False
        self.pump_calls = 0

    def start(self, asr_boundary: object) -> object:
        del asr_boundary
        self.started = True
        return types.SimpleNamespace(started=True, reason=None, diagnostic=None)

    def pump(self) -> object:
        self.pump_calls += 1
        return types.SimpleNamespace(processed_frames=0, provider_failed=False)

    def release(self, **kwargs: object) -> object:
        del kwargs
        return types.SimpleNamespace(gate_result=None, outcome=None)

    def runtime_failed(self, *, code: object) -> None:
        del code


def _driver_boundary() -> VoiceTurnBoundary:
    return VoiceTurnBoundary(
        voice_session_id="p9-silero-priority-session",
        utterance_id="p9-silero-priority-utterance",
        turn_id="p9-silero-priority-turn",
        request_id="p9-silero-priority-request",
        reply_id="p9-silero-priority-reply",
        source="asr",
        created_at="2026-10-01T09:00:00.000Z",
    )


class DriverSpeechDetectorPriorityTests(unittest.TestCase):
    """驱动接线：``speech_detector`` 优先于能量阈值，异常仍按既有 fail closed 语义。"""

    def _pipeline(self, **overrides: object):
        captured: dict[str, object] = {}
        coordinator = _MinimalCoordinator()

        def factory(vad_factory):
            captured["factory"] = vad_factory
            return coordinator

        driver = AutoCaptureVoiceDriver(coordinator_factory=factory, **overrides)  # type: ignore[arg-type]
        self.assertTrue(driver.start(_driver_boundary()))
        return captured["factory"], coordinator, driver  # type: ignore[return-value]

    def _feed(self, vad_factory, count: int) -> None:
        bus = AudioBus(
            audio_session_id="voice-loop-auto-audio",
            provider_generation=1,
            stream_id="voice-loop-auto-stream",
            device_epoch=1,
            capacity_frames=16,
        )
        vad = vad_factory(bus)
        vad.start()
        for index in range(count):
            payload = bytes(320 * 2)
            frame = AudioFrame(
                audio_session_id=bus.audio_session_id,
                provider_generation=bus.provider_generation,
                stream_id=bus.stream_id,
                event_sequence=index + 1,
                frame_index=index,
                captured_at="2026-10-01T09:00:00.000Z",
                duration_ms=20,
                device_epoch=bus.device_epoch,
                sample_rate_hz=16000,
                channel_count=1,
                sample_format="pcm_s16le",
                sample_count=320,
                payload_size_bytes=len(payload),
            )
            decision = bus.publish(frame, payload)
            self.assertEqual(decision.value, "accepted")
            packet = bus.read_nowait()
            self.assertIsNotNone(packet)
            vad.process(packet)

    def test_config_rejects_a_non_callable_speech_detector(self) -> None:
        for bad in (1, "detect", object(), True):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):
                    AutoCaptureDriverConfig(speech_detector=bad)  # type: ignore[arg-type]

    def test_speech_detector_is_used_instead_of_energy(self) -> None:
        seen: list[object] = []

        def speech(packet: object) -> bool:
            seen.append(packet)
            return True

        def energy_boom(packet: object) -> bool:
            raise AssertionError("speech_detector 已配置时不得使用能量判定")

        vad_factory, coordinator, driver = self._pipeline(
            config=AutoCaptureDriverConfig(
                silence_ms=1200, speech_detector=speech
            ),
            energy_detector=energy_boom,
        )
        self._feed(vad_factory, 3)
        self.assertEqual(len(seen), 3)
        self.assertTrue(driver.speech_started)
        self.assertIsNone(driver.last_stop_reason)
        self.assertEqual(driver.frames_seen, 3)
        self.assertEqual(coordinator.pump_calls, 0)

    def test_energy_path_is_used_when_no_speech_detector_is_configured(self) -> None:
        def energy_boom(packet: object) -> bool:
            raise RuntimeError("energy failure")

        vad_factory, _coordinator, driver = self._pipeline(
            config=AutoCaptureDriverConfig(silence_ms=1200),
            energy_detector=energy_boom,
        )
        self._feed(vad_factory, 1)
        driver.pump()
        self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)
        self.assertFalse(driver.auto_released)

    def test_speech_detector_exception_is_fail_closed(self) -> None:
        def speech(packet: object) -> bool:
            raise RuntimeError("C:\\secret\\mic.pcm raw audio")

        vad_factory, _coordinator, driver = self._pipeline(
            config=AutoCaptureDriverConfig(speech_detector=speech)
        )
        self._feed(vad_factory, 1)
        driver.pump()
        self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)
        self.assertFalse(driver.auto_released)

    def test_non_boolean_speech_detector_result_is_fail_closed(self) -> None:
        for value in (1, 0, None, "yes"):
            with self.subTest(value=value):
                vad_factory, _coordinator, driver = self._pipeline(
                    config=AutoCaptureDriverConfig(speech_detector=lambda packet, v=value: v)  # type: ignore[arg-type,return-value]
                )
                self._feed(vad_factory, 1)
                driver.pump()
                self.assertEqual(driver.last_stop_reason, STOP_DETECTOR_FAILURE)
                self.assertFalse(driver.auto_released)


class BusBackpressureTests(unittest.TestCase):
    """已知残留回归：消费者不得落后于采集；输入停滞必须快速收尾而不是空转 23 秒。

    真机机制：``AudioBus.publish`` 在队列满时走 ``_fail_locked("overflow")``——总线 FAILED
    且队列清空，本轮音频全丢；旧循环每轮固定 ``sleep(20ms)``，只要单轮开销把周期推到 20ms
    以上，积压就单调增长，长轮次必然撑爆 64 帧（1.28s）容量。
    """

    def test_audio_capacity_has_seconds_of_headroom(self) -> None:
        self.assertGreaterEqual(runner.AUDIO_CAPACITY_FRAMES * runner.FRAME_MS, 5000)
        self.assertGreater(runner.AUDIO_CAPACITY_FRAMES, 64)

    def test_coordinator_uses_the_runner_capacity_constant(self) -> None:
        # 免按键装配层已抽到 app/runtime/auto_voice.py：容量常量与其使用点都在那边，
        # 这里断言"被 import 的那份实现的源码里确实用它建了总线"。
        import app.runtime.auto_voice as shared

        source = Path(shared.__file__).read_text(encoding="utf-8")
        self.assertIn("audio_capacity_frames=AUDIO_CAPACITY_FRAMES", source)

    def test_idle_sleep_policy_never_slows_the_consumer_behind_capture(self) -> None:
        # 泵到帧 → 不睡（立刻追平积压）；一帧都没有 → 让出一帧时长。
        self.assertEqual(runner._turn_idle_sleep_seconds(1), 0.0)
        self.assertEqual(runner._turn_idle_sleep_seconds(37), 0.0)
        self.assertAlmostEqual(runner._turn_idle_sleep_seconds(0), runner.FRAME_MS / 1000)

    def test_input_stall_defaults_and_validation(self) -> None:
        self.assertEqual(runner.INPUT_STALL_MS, 5000)
        self.assertEqual(runner.AutoRunnerConfig().input_stall_ms, runner.INPUT_STALL_MS)
        self.assertEqual(runner._parse_args([]).input_stall_ms, runner.INPUT_STALL_MS)
        self.assertEqual(runner._parse_args(["--input-stall-ms", "1500"]).input_stall_ms, 1500)
        with self.assertRaises(SystemExit):
            runner._parse_args(["--input-stall-ms", "0"])
        for kwargs in ({"input_stall_ms": 0}, {"input_stall_ms": -1}, {"input_stall_ms": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_turn_without_any_audio_frames_ends_fast_as_input_stalled(self) -> None:
        environment, backend = build_split_environment([], [])
        config = runner.AutoRunnerConfig(
            tts_mode="none", vad_mode="energy", turns=1, input_stall_ms=120
        )
        _agent, session = session_factory(config, environment)
        try:
            started = time.monotonic()
            facts = session.run_turn()
            elapsed = time.monotonic() - started
        finally:
            session.close()
        self.assertEqual(facts.end_reason, "input_stalled")
        self.assertEqual(facts.frames, 0)
        self.assertFalse(facts.accepted)
        self.assertFalse(facts.submitted)
        # 旧兜底是 speech_start_timeout_ms + max_utterance_ms（默认 8s + 15s）；这里必须秒级收尾。
        self.assertLess(elapsed, 5.0)
        self.assertGreaterEqual(facts.idle_seconds, 0.1)
        self.assertIsNone(backend.turn_stream)
        record = runner._turn_record(facts)
        self.assertEqual(record["end_reason"], "input_stalled")
        self.assertGreaterEqual(record["idle_seconds"], 0.1)


class _FakeWakeGate:
    """唤醒词门禁替身：记录喂入帧数，按配置命中或抛错。"""

    def __init__(self, *, hits: bool = True, error: Exception | None = None) -> None:
        self.hits = hits
        self.error = error
        self.observed = 0

    def observe(self, packet: object) -> bool:
        del packet
        self.observed += 1
        if self.error is not None:
            raise self.error
        return self.hits


class WakeWordGateTests(unittest.TestCase):
    """P9 B3：唤醒词只开启新的输入会话；超时/不可用一律 fail closed。"""

    def _config(self, **overrides: object) -> runner.AutoRunnerConfig:
        values: dict[str, object] = {
            "tts_mode": "none",
            "vad_mode": "energy",
            "turns": 1,
            "kws_mode": "gate",
            "keyword": "小饼",
        }
        values.update(overrides)
        return runner.AutoRunnerConfig(**values)  # type: ignore[arg-type]

    def test_keyword_hit_opens_a_new_capture_for_the_turn(self) -> None:
        turn_chunks = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_split_environment([silence_payload()] * 2, turn_chunks)
        gate = _FakeWakeGate(hits=True)
        with mock.patch("app.audio.kws.KeywordGate", return_value=gate):
            _agent, session = session_factory(self._config(), environment)
            facts = _paced_run_turn(backend)(session)
        # 唤醒阶段命中即回：只喂了 1 帧，且这些音频不进 ASR。
        self.assertEqual(gate.observed, 1)
        self.assertTrue(facts.submitted)
        self.assertGreater(facts.frames, 0)
        self.assertIsNotNone(backend.turn_stream)  # 命中后才开第二路采集（真正的轮次）

    def test_gate_state_is_printed_for_the_operator(self) -> None:
        """门禁是否生效必须能从控制台看出来（等待/命中/超时）。"""

        turn_chunks = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_split_environment([silence_payload()], turn_chunks)
        printed: list[str] = []
        with mock.patch("app.audio.kws.KeywordGate", return_value=_FakeWakeGate(hits=True)):
            with mock.patch.object(runner, "_safe_print", side_effect=printed.append):
                _agent, session = session_factory(self._config(), environment)
                _paced_run_turn(backend)(session)
        self.assertTrue(any("等待唤醒词" in item for item in printed))
        self.assertTrue(any("听到唤醒词" in item for item in printed))

    def test_gate_timeout_is_printed(self) -> None:
        environment, backend = build_split_environment([], [silence_payload()])
        printed: list[str] = []
        with mock.patch("app.audio.kws.KeywordGate", return_value=_FakeWakeGate(hits=False)):
            with mock.patch.object(runner, "_safe_print", side_effect=printed.append):
                _agent, session = session_factory(self._config(kws_wait_seconds=1), environment)
                _paced_run_turn(backend)(session)
        self.assertTrue(any("未听到唤醒词" in item for item in printed))

    def test_timeout_never_opens_the_turn_capture(self) -> None:
        environment, backend = build_split_environment([], [silence_payload()])
        gate = _FakeWakeGate(hits=False)
        with mock.patch("app.audio.kws.KeywordGate", return_value=gate):
            _agent, session = session_factory(self._config(kws_wait_seconds=1), environment)
            facts = _paced_run_turn(backend)(session)
        self.assertEqual(facts.end_reason, "wake_word_timeout")
        self.assertFalse(facts.submitted)
        self.assertFalse(facts.accepted)
        self.assertEqual(facts.frames, 0)
        self.assertIsNone(backend.turn_stream)
        self.assertEqual(gate.observed, 0)

    def test_gate_failure_is_fail_closed(self) -> None:
        from app.audio.kws import DETECT_FAILED, KeywordGateError

        environment, backend = build_split_environment([silence_payload()], [silence_payload()])
        gate = _FakeWakeGate(error=KeywordGateError(DETECT_FAILED))
        with mock.patch("app.audio.kws.KeywordGate", return_value=gate):
            _agent, session = session_factory(self._config(), environment)
            with self.assertRaises(runner.WakeWordUnavailableError):
                _paced_run_turn(backend)(session)

    def test_missing_model_fails_at_assembly(self) -> None:
        environment, _backend = build_split_environment([], [silence_payload()])
        with self.assertRaises(runner.WakeWordUnavailableError):
            session_factory(self._config(), environment)

    def test_defaults_and_validation(self) -> None:
        self.assertEqual(runner.AutoRunnerConfig().kws_mode, "off")
        self.assertEqual(runner.AutoRunnerConfig().kws_keywords_file, None)
        self.assertEqual(runner._parse_args([]).kws_mode, "off")
        self.assertEqual(runner._parse_args([]).keyword, runner.DEFAULT_KEYWORD)
        parsed = runner._parse_args(["--kws-mode", "gate", "--keyword", "小饼", "--kws-wait-seconds", "5"])
        self.assertEqual(parsed.kws_mode, "gate")
        self.assertEqual(parsed.kws_wait_seconds, 5)
        keywords = runner._parse_args(["--kws-keywords-file", "k.txt"])
        self.assertEqual(keywords.kws_keywords_file, "k.txt")
        self.assertEqual(
            runner.AutoRunnerConfig(kws_keywords_file="k.txt").kws_keywords_file, Path("k.txt")
        )
        with self.assertRaises(SystemExit):
            runner._parse_args(["--kws-mode", "always"])
        cases = (
            {"kws_mode": "on"},
            {"keyword": "   "},
            {"kws_threshold": 2.0},
            {"kws_threshold": True},
            {"kws_wait_seconds": 0},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_artifact_records_gate_facts(self) -> None:
        environment, _backend = build_split_environment([], [silence_payload()])
        config = self._config()
        with mock.patch("app.audio.kws.KeywordGate", return_value=_FakeWakeGate()):
            _agent, session = session_factory(config, environment)
        facts = runner._auto_capture_facts(config, session)
        self.assertEqual(facts["kws_mode"], "gate")
        self.assertEqual(facts["keyword"], "小饼")
        self.assertEqual(facts["kws_threshold"], 0.25)
        self.assertEqual(facts["kws_wait_seconds"], 30)

    def test_gate_receives_model_dir_and_keywords_file(self) -> None:
        """真机回归：runner 必须把 model_dir 与 keywords_file 都交给门禁（否则构造失败）。"""

        environment, _backend = build_split_environment([], [silence_payload()])
        captured: dict[str, object] = {}

        def builder(*, config: object = None) -> object:
            captured["config"] = config
            return _FakeWakeGate()

        config = self._config(
            kws_model_dir=Path("models/kws"), kws_keywords_file=Path("models/kws/keywords.txt")
        )
        with mock.patch("app.audio.kws.KeywordGate", side_effect=builder):
            session_factory(config, environment)
        gate_config = captured["config"]
        self.assertEqual(gate_config.model_dir, Path("models/kws"))  # type: ignore[attr-defined]
        self.assertEqual(gate_config.keywords_file, Path("models/kws/keywords.txt"))  # type: ignore[attr-defined]
        self.assertEqual(gate_config.keyword, "小饼")  # type: ignore[attr-defined]

    @unittest.skipUnless(
        (RUNNER_PATH.parent.parent / "models" / "kws" / "keywords.txt").is_file(),
        "需要已下载的唤醒词模型（models/kws）",
    )
    def test_real_model_gate_assembles_with_the_shipped_layout(self) -> None:
        """用户真机踩过的坑：model_dir 齐备但没传 keywords_file → 一度直接退出。

        这里**不 patch** 门禁，用真实 KeywordGate 走一遍装配（不打开设备）。
        """

        environment, _backend = build_split_environment([], [silence_payload()])
        config = self._config(
            kws_model_dir=RUNNER_PATH.parent.parent / "models" / "kws", keyword="小饼"
        )
        _agent, session = session_factory(config, environment)
        self.assertIsNotNone(session._wake_gate)  # noqa: SLF001 - 只读断言装配成功


class _FakeVoiceprintFilter:
    """声纹过滤替身：可配置匹配结果与异常，并记录收到的 PCM。"""

    def __init__(
        self, *, matched: bool = True, error: Exception | None = None, reason: str | None = None
    ) -> None:
        self.matched = matched
        self.error = error
        self.reason = reason
        self.calls: list[bytes] = []
        self.enrolled: list[list[float]] = []

    def enroll(self, embedding: object, *, name: str | None = None) -> bool:
        del name
        self.enrolled.append(list(embedding))  # type: ignore[arg-type]
        return True

    def verify_from_pcm(self, payload: object, *, sample_rate_hz: int = 16000) -> object:
        from app.audio.voiceprint import VoiceprintDecision

        del sample_rate_hz
        self.calls.append(bytes(payload))  # type: ignore[arg-type]
        if self.error is not None:
            raise self.error
        reason = self.reason or ("matched" if self.matched else "below_threshold")
        return VoiceprintDecision(self.matched, 0.9 if self.matched else 0.1, reason)

    @property
    def enrolled_speakers(self) -> list[str]:
        return ["owner"]

    @property
    def verified_count(self) -> int:
        return 1 if self.matched else 0

    @property
    def rejected_count(self) -> int:
        return 0 if self.matched else 1


class VoiceprintFilterTests(unittest.TestCase):
    """P9 B4：声纹只做误触发过滤——不匹配在提交前丢弃，故障绝不放行任何计划/执行事实。"""

    PROFILE = {"speaker": "owner", "embeddings": [[1.0, 0.0]]}
    TURN_CHUNKS = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140

    def _run(self, fake: object, *, profile: object = None, **overrides: object):
        environment, backend = build_environment(self.TURN_CHUNKS)
        values: dict[str, object] = {
            "tts_mode": "none",
            "vad_mode": "energy",
            "turns": 1,
            "voiceprint_mode": "filter",
        }
        values.update(overrides)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "profile.json"
            content = self.PROFILE if profile is None else profile
            path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            values.setdefault("voiceprint_profile", path)
            config = runner.AutoRunnerConfig(**values)  # type: ignore[arg-type]
            with mock.patch("app.audio.voiceprint.VoiceprintFilter", return_value=fake):
                agent, session = session_factory(config, environment)
            facts = _paced_run_turn(backend)(session)
        return facts, agent, session, config

    def test_non_matching_speaker_drops_the_turn_before_submitting(self) -> None:
        fake = _FakeVoiceprintFilter(matched=False)
        facts, agent, _session, _config = self._run(fake)
        self.assertEqual(facts.end_reason, "voiceprint_rejected")
        self.assertFalse(facts.submitted)
        self.assertFalse(facts.accepted)
        self.assertEqual(agent.calls, [])  # 未提交给 Agent：不产生任何计划/执行事实
        self.assertEqual(len(fake.calls), 1)
        self.assertGreater(len(fake.calls[0]), 0)  # 确实拿到了本轮 PCM
        self.assertEqual(fake.enrolled, [[1.0, 0.0]])  # 档案已注册

    def test_matching_speaker_proceeds_normally(self) -> None:
        fake = _FakeVoiceprintFilter(matched=True)
        facts, agent, session, _config = self._run(fake)
        self.assertTrue(facts.submitted)
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(len(fake.calls), 1)
        # 每轮声纹分数必须落进 artifact（调阈值靠它，而不是观感）。
        verification = session.observation_details()["speaker_verification"]
        self.assertIsNotNone(verification)
        self.assertTrue(verification["matched"])
        self.assertAlmostEqual(verification["score"], 0.9)
        self.assertEqual(verification["reason"], "matched")

    def test_rejected_turn_records_the_speaker_decision(self) -> None:
        fake = _FakeVoiceprintFilter(matched=False)
        facts, _agent, session, _config = self._run(fake)
        self.assertEqual(facts.end_reason, "voiceprint_rejected")
        verification = session.observation_details()["speaker_verification"]
        self.assertFalse(verification["matched"])
        self.assertEqual(verification["reason"], "below_threshold")
        self.assertLess(verification["score"], 0.5)

    def test_filter_failure_never_drops_the_turn(self) -> None:
        from app.audio.voiceprint import COMPUTE_FAILED, VoiceprintError

        fake = _FakeVoiceprintFilter(error=VoiceprintError(COMPUTE_FAILED))
        facts, agent, session, _config = self._run(fake)
        self.assertTrue(facts.submitted)
        self.assertEqual(len(agent.calls), 1)
        # 过滤链路报错只记只读计数：它不决定授权，也不因为故障丢弃正常轮次。
        self.assertEqual(session.voiceprint_details()["errors"], 1)

    def test_not_enrolled_is_treated_as_unavailable_not_as_rejection(self) -> None:
        fake = _FakeVoiceprintFilter(reason="not_enrolled")
        facts, _agent, session, _config = self._run(fake)
        self.assertTrue(facts.submitted)
        self.assertEqual(session.voiceprint_details()["errors"], 1)

    def test_missing_profile_or_model_fails_at_assembly(self) -> None:
        environment, _backend = build_environment([silence_payload()])
        with tempfile.TemporaryDirectory() as raw:
            absent = Path(raw) / "nope.json"
            config = runner.AutoRunnerConfig(
                tts_mode="none", vad_mode="energy", voiceprint_mode="filter", voiceprint_profile=absent
            )
            with self.assertRaises(runner.VoiceprintUnavailableError):
                session_factory(config, environment)
            # 空档案同样 fail closed。
            empty = Path(raw) / "empty.json"
            empty.write_text(json.dumps({"embeddings": []}), encoding="utf-8")
            empty_config = runner.AutoRunnerConfig(
                tts_mode="none", vad_mode="energy", voiceprint_mode="filter", voiceprint_profile=empty
            )
            with self.assertRaises(runner.VoiceprintUnavailableError):
                session_factory(empty_config, environment)
            # 模型缺失（未注入替身）同样 fail closed。
            good = Path(raw) / "good.json"
            good.write_text(json.dumps(self.PROFILE), encoding="utf-8")
            no_model = runner.AutoRunnerConfig(
                tts_mode="none", vad_mode="energy", voiceprint_mode="filter", voiceprint_profile=good
            )
            with self.assertRaises(runner.VoiceprintUnavailableError):
                session_factory(no_model, environment)

    def test_defaults_and_validation(self) -> None:
        self.assertEqual(runner.AutoRunnerConfig().voiceprint_mode, "off")
        self.assertEqual(runner._parse_args([]).voiceprint_mode, "off")
        parsed = runner._parse_args(["--voiceprint-mode", "filter", "--voiceprint-threshold", "0.7"])
        self.assertEqual(parsed.voiceprint_mode, "filter")
        self.assertEqual(parsed.voiceprint_threshold, 0.7)
        with self.assertRaises(SystemExit):
            runner._parse_args(["--voiceprint-mode", "always"])
        for kwargs in (
            {"voiceprint_mode": "on"},
            {"voiceprint_threshold": 0.0},
            {"voiceprint_threshold": 2.0},
            {"voiceprint_threshold": True},
            {"voiceprint_seconds": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**kwargs)  # type: ignore[arg-type]

    def test_artifact_records_voiceprint_facts(self) -> None:
        fake = _FakeVoiceprintFilter(matched=True)
        _facts, _agent, session, config = self._run(fake)
        details = runner._auto_capture_facts(config, session)
        self.assertEqual(details["voiceprint_threshold"], 0.5)
        self.assertEqual(details["voiceprint_seconds"], 8)
        self.assertEqual(details["voiceprint"]["mode"], "filter")
        self.assertTrue(details["voiceprint"]["enabled"])
        self.assertEqual(details["voiceprint"]["enrolled_speakers"], ["owner"])


class _ScriptedSileroDetector:
    """可编排的 Silero 替身：前 ``hits`` 帧命中（模拟说话），之后不命中（模拟静音）。

    ``pending_segment`` 模拟"底层 VAD 队列里还有一段已结束的人声段"——真机上第 1 轮结束后
    这个残留段会让第 2 轮第 1 帧就被判"说完了"（`reset()` 才会清掉）。
    """

    def __init__(self, *, hits: int = 0, pending_segment: bool = False) -> None:
        self.hits = hits
        self.seen = 0
        self.pending_value = pending_segment
        self.reset_calls = 0

    def observe(self, packet: object) -> bool:
        del packet
        self.seen += 1
        return self.seen <= self.hits

    def segment_pending(self) -> bool:
        return self.pending_value

    def flush_segment(self) -> None:
        return None

    def reset(self) -> None:
        self.reset_calls += 1
        self.pending_value = False
        self.seen = 0

    def to_details(self) -> dict[str, object]:
        return {"mode": "silero", "injected": True}


class LeftoverSegmentTests(unittest.TestCase):
    """真机缺陷回归：上一轮残留的"已结束段"不得让下一轮在第 1 帧就收尾。"""

    def test_each_turn_resets_the_detector_and_a_leftover_segment_cannot_end_it(self) -> None:
        chunks = [speech_payload()] * 20 + [silence_payload()] * 140
        environment, backend = build_split_environment(chunks, chunks)
        detector = _ScriptedSileroDetector(hits=20, pending_segment=True)
        config = runner.AutoRunnerConfig(
            tts_mode="none", vad_mode="silero", turns=2, silence_ms=1200, max_record_seconds=15
        )
        with mock.patch.object(silero_module, "build_silero_vad_detector", return_value=detector):
            _agent, session = session_factory(config, environment)
            pumped = _paced_run_turn(backend)
            first = pumped(session)
            first_details = session.observation_details()
            detector.pending_value = True  # 模拟第 1 轮结束后队列里仍留着已结束段
            second = pumped(session)
        # 每轮开始都重置 → 残留段被清掉，两轮都必须靠真实静音收尾（而不是残留段瞬间收尾）
        self.assertEqual(first_details["end_source"], "silence_frames")
        self.assertGreater(first.frames, 15)
        self.assertGreaterEqual(detector.reset_calls, 2)
        self.assertFalse(detector.pending_value)
        self.assertGreater(second.frames, 15)
        details = session.observation_details()
        self.assertEqual(details["end_source"], "silence_frames")

    def test_runner_cli_exposes_min_utterance(self) -> None:
        self.assertEqual(runner.AutoRunnerConfig().min_utterance_ms, 300)
        self.assertEqual(runner._parse_args([]).min_utterance_ms, 300)
        self.assertEqual(runner._parse_args(["--min-utterance-ms", "500"]).min_utterance_ms, 500)
        with self.assertRaises(SystemExit):
            runner._parse_args(["--min-utterance-ms", "0"])
        for bad in ({"min_utterance_ms": 0}, {"min_utterance_ms": -1}, {"min_utterance_ms": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    runner.AutoRunnerConfig(**bad)  # type: ignore[arg-type]


class VoiceprintDiagnosticsTests(unittest.TestCase):
    """声纹诊断可见性：被丢弃的轮次必须能看懂（分数 + 对症提示），不能笼统归咎 VAD。"""

    PROFILE = {"speaker": "owner", "embeddings": [[1.0, 0.0]]}
    TURN_CHUNKS = [silence_payload()] * 3 + [speech_payload()] * 20 + [silence_payload()] * 140

    def test_rejected_turn_is_reported_with_its_score(self) -> None:
        fake = _FakeVoiceprintFilter(matched=False)
        environment, _backend = build_split_environment(self.TURN_CHUNKS, self.TURN_CHUNKS)
        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw) / "profile.json"
            profile.write_text(json.dumps(self.PROFILE), encoding="utf-8")
            config = runner.AutoRunnerConfig(
                tts_mode="none",
                vad_mode="energy",
                turns=1,
                voiceprint_mode="filter",
                voiceprint_profile=profile,
            )
            with mock.patch("app.audio.voiceprint.VoiceprintFilter", return_value=fake):
                _agent, session = session_factory(config, environment)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                record = runner.run_turn(1, session)
        self.assertEqual(record["end_reason"], "voiceprint_rejected")
        text = buffer.getvalue()
        self.assertIn("声纹不匹配", text)
        self.assertIn("0.100", text)  # 替身分数
        self.assertNotIn("未得到可靠的识别结果", text)

    def test_zero_valid_summary_blames_the_voiceprint_filter(self) -> None:
        config = runner.AutoRunnerConfig(tts_mode="none", voiceprint_threshold=0.5)
        turns = [{"end_reason": "voiceprint_rejected"}, {"end_reason": "voiceprint_rejected"}]
        message = runner._zero_valid_summary(turns, config)
        self.assertIn("声纹过滤", message)
        self.assertIn("预期行为", message)
        self.assertNotIn("Silero VAD", message)

    def test_zero_valid_summary_falls_back_to_vad_hint(self) -> None:
        config = runner.AutoRunnerConfig(tts_mode="none", vad_mode="silero")
        message = runner._zero_valid_summary([{"end_reason": "no_speech"}], config)
        self.assertIn("Silero VAD", message)
        energy = runner.AutoRunnerConfig(tts_mode="none", vad_mode="energy", speech_threshold=600.0)
        self.assertIn("--speech-threshold 300", runner._zero_valid_summary([], energy))


class SourceAuditTests(unittest.TestCase):
    def test_runner_has_no_process_network_or_device_entrypoints(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        banned = {"subprocess", "socket", "threading", "multiprocessing", "winmm", "sounddevice", "sherpa_onnx"}
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & banned, set())

    def test_runner_never_references_the_safety_chain(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        for module in (
            "app.safety",
            "app.executor",
            "app.plans",
            "app.auditing",
            "app.verification",
            "app.tools",
        ):
            with self.subTest(module=module):
                self.assertNotIn(module, source)

    def test_runner_only_persists_summaries(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("asr_text_sha256", source)
        self.assertNotIn("write_bytes", source)
        self.assertNotIn('open("', source.replace("_artifact", ""))


if __name__ == "__main__":
    unittest.main()
