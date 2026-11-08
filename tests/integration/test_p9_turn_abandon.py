r"""P9 真机缺陷回归：放弃"没有可用输入"的语音轮次不得锁死后续 ASR 轮次。

真机现象（2026-10-01，P9 免按键自动收音）：第一轮没检测到语音被丢弃后，第二轮
`start_rejected: ASR 当前不可用`。根因是丢弃路径走 `runtime_failed(CANCELLED)`，
把 `AsrRecoveryController` 置为 `SESSION_FAILED_RUNTIME`，而 `start()` 只接受 `READY`。

本测试固化修复：`PushToTalkCoordinator.abandon_turn()` 只做安全收尾，不产生 ASR 故障
事实，并通过既有低频探测路径让通道回到 `READY`；真实 ASR 故障仍走 `runtime_failed`。
"""

from __future__ import annotations

import array
import types
import unittest
from pathlib import Path

from app.agent.adapter import AgentTurnOutcome
from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.asr.push_to_talk import PushToTalkCoordinator
from app.asr.recovery import AsrFailureCode, AsrRecoveryState, AsrSessionBoundary
from app.asr.result_gate import ResultGateDecision
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceCheckResult,
    DeviceCheckStatus,
    DeviceSelection,
    FakeCaptureStream,
)
from app.audio.vad import VadConfig, VadSession
from app.conversation import ConversationManager
from app.runtime.assistant_runtime import LocalChatRouter
from tests.integration.test_p6_integration import _OfflineRecognizer, _Recognizer

SPOKEN_SENTENCE = "打开编辑器"
SAMPLE_FRAMES = 320


def speech_payload(*, amplitude: int = 3000, frames: int = SAMPLE_FRAMES) -> bytes:
    return array.array("h", [amplitude] * frames).tobytes()


class EmittingStream(FakeCaptureStream):
    def __init__(self, payload: bytes, frames: int) -> None:
        super().__init__()
        self._payload = payload
        self._frames = frames

    def start(self) -> None:
        super().start()
        self.emit(self._payload, frames=self._frames)


class EmittingBackend:
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

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
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


def build_coordinator() -> tuple[PushToTalkCoordinator, RecordingAgent]:
    _OfflineRecognizer.recognizer = _Recognizer(SPOKEN_SENTENCE)
    module = types.ModuleType("sherpa_onnx")
    module.OfflineRecognizer = _OfflineRecognizer  # type: ignore[attr-defined]
    provider = SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
        module_loader=lambda: module,
    )
    backend = EmittingBackend(speech_payload(), SAMPLE_FRAMES)
    conversation = ConversationManager(RecordingAgent(), router=LocalChatRouter())
    agent = conversation._adapter  # noqa: SLF001 - 测试需要断言提交事实

    def capture_factory(bus) -> CaptureSession:
        return CaptureSession(
            bus=bus, backend=backend, selection=DeviceSelection.fixed("mic-1"), config=CaptureConfig()
        )

    coordinator = PushToTalkCoordinator(
        conversation=conversation,
        provider=provider,
        capture_factory=capture_factory,
        # 与本项目 P9 自动收音一致：转发 VAD，不自行结束轮次。
        vad_factory=lambda bus: VadSession(
            bus=bus, config=VadConfig(silence_duration_ms=600000), detector=lambda packet: True
        ),
        source_instance_id="p9-abandon-test",
    )
    coordinator.startup_succeeded()
    return coordinator, agent  # type: ignore[return-value]


def boundary(index: int) -> AsrSessionBoundary:
    return AsrSessionBoundary(
        session_id=f"session-{index}",
        audio_session_id=f"audio-{index}",
        provider_generation=1,
        stream_id=f"stream-{index}",
        utterance_id=f"utterance-{index}",
    )


class AbandonTurnTests(unittest.TestCase):
    def test_abandon_keeps_the_channel_ready_and_the_next_turn_works(self) -> None:
        coordinator, agent = build_coordinator()
        self.assertTrue(coordinator.start(boundary(1)).started)
        self.assertTrue(coordinator.abandon_turn(reason="no_speech"))
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)
        self.assertIsNone(coordinator.recovery.last_failure)

        started = coordinator.start(boundary(2))
        self.assertTrue(started.started, started.reason)
        accepted = coordinator.release()
        self.assertIs(accepted.gate_result.decision, ResultGateDecision.ACCEPTED_FINAL)
        self.assertEqual(agent.calls, [SPOKEN_SENTENCE])
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)

    def test_repeated_abandon_stays_idempotent(self) -> None:
        coordinator, _agent = build_coordinator()
        self.assertTrue(coordinator.start(boundary(1)).started)
        self.assertTrue(coordinator.abandon_turn(reason="no_speech"))
        # 没有进行中的轮次时，再放弃也必须安全且不改变可用状态。
        self.assertTrue(coordinator.abandon_turn(reason="cancelled"))
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.READY)
        self.assertTrue(coordinator.start(boundary(2)).started)

    def test_abandon_rejects_free_text_reason(self) -> None:
        coordinator, _agent = build_coordinator()
        self.assertTrue(coordinator.start(boundary(1)).started)
        with self.assertRaises(Exception):
            coordinator.abandon_turn(reason="这不是原因码")
        self.assertTrue(coordinator.abandon_turn(reason="cancelled"))

    def test_real_asr_failure_still_fails_closed(self) -> None:
        coordinator, _agent = build_coordinator()
        self.assertTrue(coordinator.start(boundary(1)).started)
        coordinator.runtime_failed(code=AsrFailureCode.STREAM_FAILED)
        self.assertIs(coordinator.recovery.state, AsrRecoveryState.SESSION_FAILED_RUNTIME)
        rejected = coordinator.start(boundary(2))
        self.assertFalse(rejected.started)
        self.assertEqual(rejected.reason, "ASR 当前不可用")

    def test_abandon_does_not_create_plan_authorization_or_execution(self) -> None:
        coordinator, _agent = build_coordinator()
        self.assertTrue(coordinator.start(boundary(1)).started)
        coordinator.abandon_turn(reason="no_speech")
        for name in (
            "authorization_snapshot",
            "plan",
            "execution_ledger",
            "result_document",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(coordinator, name))


if __name__ == "__main__":
    unittest.main()
