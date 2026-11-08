"""P8 语音入口会话测试：真实 P6 管道 + 注入式离线环境（不开真实设备）。"""

from __future__ import annotations

import types
import unittest
from pathlib import Path

from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter
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
from app.runtime.voice_entry import (
    NO_SPEECH_REASON,
    VoiceEntryConfig,
    VoiceEntryEnvironment,
    VoiceEntryError,
    VoiceLoopSession,
    energy_detector,
    selection_with_speed,
)
from tests.integration.test_p6_integration import _OfflineRecognizer, _Recognizer
from tests.voice_loop.support import FakeSpeaker, FakeStream

SPOKEN_SENTENCE = "打开编辑器"


class EmittingStream(FakeCaptureStream):
    def __init__(self, payload: bytes, frames: int) -> None:
        super().__init__()
        self._payload = payload
        self._frames = frames

    def start(self) -> None:
        super().start()
        if self._payload:
            self.emit(self._payload, frames=self._frames)


class AutoCaptureBackend:
    def __init__(self, payload: bytes, frames: int) -> None:
        self.stream = EmittingStream(payload, frames)

    def check_device(self, selection, *, sample_rate_hz: int, channel_count: int) -> DeviceCheckResult:
        del selection, sample_rate_hz, channel_count
        return DeviceCheckResult(DeviceCheckStatus.READY, "mic-1", "ready")

    def open_input_stream(self, **kwargs: object) -> EmittingStream:
        self.stream.callback = kwargs["callback"]  # type: ignore[assignment]
        return self.stream


class RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_chat(self, *, request_id: str, session_id: str, text: str):
        from app.agent.adapter import AgentTurnOutcome

        self.calls.append(text)
        return AgentTurnOutcome(
            request_id=request_id, session_id=session_id, completed=True,
            text=f"回复：{text}", finish_reason="stop",
        )

    def close(self) -> None:
        pass


def speech_payload(*, amplitude: int = 3000, frames: int = 320) -> bytes:
    import array

    return array.array("h", [amplitude] * frames).tobytes()


def silence_payload(*, frames: int = 320) -> bytes:
    import array

    return array.array("h", [0] * frames).tobytes()


def environment(
    *, payload: bytes | None = None, load_failure: bool = False, speaker: object | None = None
) -> VoiceEntryEnvironment:
    _OfflineRecognizer.recognizer = _Recognizer(SPOKEN_SENTENCE)
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
    backend = AutoCaptureBackend(payload if payload is not None else speech_payload(), 320)

    def capture_factory(bus) -> CaptureSession:
        return CaptureSession(
            bus=bus, backend=backend, selection=DeviceSelection.fixed("mic-1"), config=CaptureConfig()
        )

    return VoiceEntryEnvironment(
        capture_factory=capture_factory,
        provider=provider,
        speaker=speaker,
        devices=("Fake Mic",),
        tts_mode="none",
        details={"device": "Fake Mic", "device_id": "mic-1", "model": "fake.onnx"},
    )


def session(**kwargs) -> tuple[VoiceLoopSession, RecordingAgent]:
    agent = RecordingAgent()
    conversation = ConversationManager(agent, router=LocalChatRouter())
    config = kwargs.pop("config", None) or VoiceEntryConfig(
        tts_mode="none", silence_ms=60, speech_timeout_seconds=1, max_record_seconds=3
    )
    return (
        VoiceLoopSession(conversation=conversation, environment=environment(**kwargs), config=config),
        agent,
    )


class RecordingSpeaker:
    """记录每次 start_stream，用于验证会话对 speaker.records 的透出。"""

    def __init__(self, stream: object) -> None:
        self.stream = stream
        self.records: list[dict[str, object]] = []

    def start_stream(self):
        self.records.append({"route": "recording_speaker", "text_length": 0, "error": None})
        return self.stream


class VoiceEntryConfigTests(unittest.TestCase):
    def test_invalid_modes_and_thresholds_are_rejected(self) -> None:
        for kwargs in (
            {"tts_mode": "legacy"},
            {"silence_ms": 0},
            {"speech_timeout_seconds": 0},
            {"max_record_seconds": -1},
            {"speech_threshold": 0.0},
            {"device_id": ""},
            {"edge_voice": ""},
            {"tts_speed": 0.4},
            {"tts_speed": 2.1},
            {"tts_speed": True},
            {"tts_speed": "1.4"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    VoiceEntryConfig(**kwargs)  # type: ignore[arg-type]

    def test_tts_speed_defaults_to_none_and_accepts_measured_range(self) -> None:
        self.assertIsNone(VoiceEntryConfig().tts_speed)
        self.assertEqual(VoiceEntryConfig(tts_speed=1.4).tts_speed, 1.4)
        self.assertIsInstance(VoiceEntryConfig(tts_speed=1).tts_speed, float)

    def test_selection_with_speed_overrides_only_the_effective_speed(self) -> None:
        from app.tts.private_selection import CONFIRMED_PROFILE, VoiceSelection

        selection = VoiceSelection(CONFIRMED_PROFILE, "tester")
        self.assertIs(selection_with_speed(selection, None), selection)
        self.assertIs(selection_with_speed(selection, 1.0), selection)
        faster = selection_with_speed(selection, 1.4)
        self.assertEqual(faster.speed, 1.4)
        self.assertEqual(selection.speed, 1.0)  # 原选择不被就地修改
        self.assertEqual(faster.profile_id, selection.profile_id)
        self.assertEqual(faster.reference_sha256, selection.reference_sha256)

    def test_default_model_paths_are_inside_the_project(self) -> None:
        config = VoiceEntryConfig()
        self.assertTrue(str(config.model).endswith("model.int8.onnx"))
        self.assertTrue(str(config.tokens).endswith("tokens.txt"))


class VoiceLoopSessionTests(unittest.TestCase):
    def test_speech_turn_submits_and_reports_text_and_reply(self) -> None:
        loop, agent = session(speaker=None)
        facts = loop.run_turn()
        self.assertTrue(facts.speech_seen)
        self.assertTrue(facts.accepted)
        self.assertTrue(facts.submitted)
        self.assertEqual(facts.asr_text, SPOKEN_SENTENCE)
        self.assertEqual(facts.reply_text, f"回复：{SPOKEN_SENTENCE}")
        self.assertEqual(agent.calls, [SPOKEN_SENTENCE])
        self.assertEqual(facts.boundary_facts()["turn_id"], facts.boundary.turn_id)
        self.assertEqual(loop.state.value, "idle")
        self.assertEqual(loop.turn_count, 1)
        self.assertEqual(loop.last_recognized_text, SPOKEN_SENTENCE)
        loop.close()

    def test_speech_turn_speaks_through_the_injected_speaker(self) -> None:
        stream = FakeStream()
        speaker = RecordingSpeaker(stream)
        loop, _agent = session(speaker=speaker)
        facts = loop.run_turn()
        self.assertTrue(facts.spoken)
        self.assertEqual(stream.chunks, [f"回复：{SPOKEN_SENTENCE}"])
        self.assertEqual(stream.finished, 1)
        self.assertEqual(len(loop.speaker_records), 1)
        self.assertEqual(loop.speaker_records[0]["route"], "recording_speaker")
        loop.close()

    def test_silence_turn_is_discarded_without_submitting(self) -> None:
        loop, agent = session(payload=silence_payload())
        facts = loop.run_turn()
        self.assertFalse(facts.speech_seen)
        self.assertFalse(facts.submitted)
        self.assertFalse(facts.spoken)
        self.assertEqual(facts.reason, NO_SPEECH_REASON)
        self.assertIsNone(facts.asr_text)
        self.assertEqual(agent.calls, [])
        loop.close()

    def test_each_turn_gets_a_fresh_boundary(self) -> None:
        loop, _agent = session()
        first = loop.run_turn()
        second = loop.run_turn()
        self.assertNotEqual(first.boundary.identity, second.boundary.identity)
        self.assertEqual(loop.turn_count, 2)
        loop.close()

    def test_asr_startup_failure_is_reported_before_any_turn(self) -> None:
        with self.assertRaises(VoiceEntryError) as ctx:
            session(load_failure=True)
        self.assertIn("降级为文字输入", str(ctx.exception))

    def test_closed_session_refuses_more_turns(self) -> None:
        loop, _agent = session()
        loop.close()
        loop.close()
        with self.assertRaises(VoiceEntryError):
            loop.run_turn()

    def test_log_details_exclude_recognized_text(self) -> None:
        loop, _agent = session()
        facts = loop.run_turn()
        details = facts.log_details()
        self.assertEqual(details["asr_text_length"], len(SPOKEN_SENTENCE))
        self.assertNotIn(SPOKEN_SENTENCE, str(details))
        self.assertEqual(set(details), {
            "turn", "frames", "speech_seen", "end_reason", "accepted", "submitted",
            "spoken", "reason", "asr_text_length",
            # 诊断（2026-10-24）：回复字数 / 跳过播报的固定短码 / 路由短码（都只含
            # 计数与固定码，不含识别或回复正文）。
            "reply_chars", "speech_skip_reason", "route",
        })
        loop.close()

    def test_session_rejects_wrong_input_types(self) -> None:
        with self.assertRaises(TypeError):
            VoiceLoopSession(conversation=object(), environment=environment())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            VoiceLoopSession(conversation=ConversationManager(RecordingAgent()), environment=object())  # type: ignore[arg-type]


class EnergyDetectorTests(unittest.TestCase):
    def test_detector_uses_pcm_energy_only(self) -> None:
        detector = energy_detector(600.0)
        loud = type("Packet", (), {"payload": speech_payload()})()
        quiet = type("Packet", (), {"payload": silence_payload()})()
        broken = type("Packet", (), {"payload": b""})()
        self.assertTrue(detector(loud))
        self.assertFalse(detector(quiet))
        self.assertFalse(detector(broken))


if __name__ == "__main__":
    unittest.main()
