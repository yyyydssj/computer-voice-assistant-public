"""P8 批次 3 红测：按键闲聊语音闭环接线（离线、无真实设备/网络）。"""

from __future__ import annotations

import unittest

from app.agent.adapter import AgentTurnOutcome
from app.asr.push_to_talk import _speech_eligible
from app.asr.recovery import AsrFailureCode
from app.asr.result_gate import ResultGateDecision
from app.voice_loop import (
    ChatVoiceLoop,
    PushToTalkVoiceDriver,
    VoiceDriverTurnResult,
    VoiceInputSeam,
    VoiceLoopError,
    VoiceLoopState,
    VoiceLoopStateMachine,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
    speech_eligible,
)
from tests.voice_loop.support import (
    FIXED_NOW,
    FakeSpeaker,
    FakeStream,
    RecordingConversation,
)

LOOP_PUBLIC_API = {
    "state",
    "boundary",
    "last_report",
    "start_turn",
    "pump",
    "release_input",
    "complete_turn",
    "cancel_turn",
    # P10 · T1 批次 3：轮次之外的通知播报（不改状态机；不是授权/执行 API）
    "speak_notice",
}


def outcome(**overrides: object) -> AgentTurnOutcome:
    values: dict[str, object] = {
        "request_id": "asr-request-1",
        "session_id": "chat-session-1",
        "completed": True,
        "text": "好的，第一句。第二句。",
        "finish_reason": "stop",
    }
    values.update(overrides)
    return AgentTurnOutcome(**values)  # type: ignore[arg-type]


def driver_result(**overrides: object) -> VoiceDriverTurnResult:
    values: dict[str, object] = {
        "accepted": True,
        "submitted": True,
        "speech_eligible": True,
        "speech_handled": False,
        "reply_text": "好的，第一句。第二句。",
        "reason": "accepted_final",
    }
    values.update(overrides)
    return VoiceDriverTurnResult(**values)  # type: ignore[arg-type]


class FakeDriver:
    def __init__(
        self,
        *,
        start_ok: bool = True,
        start_error: Exception | None = None,
        result: VoiceDriverTurnResult | None = None,
        release_error: Exception | None = None,
        frames: int = 3,
    ) -> None:
        self.start_ok = start_ok
        self.start_error = start_error
        self.result = result if result is not None else driver_result()
        self.release_error = release_error
        self.frames = frames
        self.started: list[VoiceTurnBoundary] = []
        self.pumps = 0
        self.releases = 0
        self.cancels: list[str] = []

    def start(self, boundary: VoiceTurnBoundary) -> bool:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(boundary)
        return self.start_ok

    def pump(self) -> int:
        self.pumps += 1
        return self.frames

    def release(self) -> VoiceDriverTurnResult:
        self.releases += 1
        if self.release_error is not None:
            raise self.release_error
        return self.result

    def cancel(self, *, reason: str) -> None:
        self.cancels.append(reason)


def build_loop(*, driver=None, speaker=None, conversation=None, voice=True, sink=None):
    registry = VoiceTurnRegistry(source_instance_id="instance-1", now=lambda: FIXED_NOW)
    machine = VoiceLoopStateMachine(registry, diagnostic_sink=sink)
    seam = VoiceInputSeam(
        conversation=conversation or RecordingConversation(),
        registry=registry,
        diagnostic_sink=sink,
    )
    if voice:
        seam.note_voice_ready()
        seam.request_voice_switch(local_check_passed=True)
    loop = ChatVoiceLoop(
        registry=registry,
        machine=machine,
        seam=seam,
        driver=driver or FakeDriver(),
        speaker=speaker,
        diagnostic_sink=sink,
    )
    return loop, registry, machine, seam


def boundary() -> VoiceTurnBoundary:
    return VoiceTurnBoundary(
        voice_session_id="voice-session-1",
        utterance_id="asr-utterance-1",
        turn_id="asr-turn-1",
        request_id="asr-request-1",
        reply_id="asr-reply-1",
        source="asr",
        created_at="2026-10-01T09:00:00.000Z",
    )


class VoiceDriverTurnResultTests(unittest.TestCase):
    def test_invariants_are_enforced(self) -> None:
        with self.assertRaises(ValueError):
            driver_result(accepted=False, submitted=True)
        with self.assertRaises(ValueError):
            driver_result(speech_eligible=True, submitted=False)
        with self.assertRaises(ValueError):
            driver_result(speech_handled=True, accepted=False)
        with self.assertRaises(VoiceLoopError):
            driver_result(reason="not a reason!")

    def test_speech_eligibility_matches_the_p6_helper(self) -> None:
        cases = [
            outcome(),
            outcome(completed=False, text=None, finish_reason=None),
            outcome(text="   "),
            outcome(finish_reason=None),
            outcome(text=123),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(speech_eligible(case), _speech_eligible(case))


class ChatVoiceLoopHappyPathTests(unittest.TestCase):
    def test_voice_turn_reaches_speech_and_returns_to_idle(self) -> None:
        driver = FakeDriver()
        stream = FakeStream()
        speaker = FakeSpeaker(stream)
        loop, registry, machine, _ = build_loop(driver=driver, speaker=speaker)

        started = loop.start_turn()
        self.assertIsNotNone(started)
        self.assertEqual(machine.state, VoiceLoopState.CAPTURING)
        self.assertEqual(driver.started, [started])
        self.assertEqual(loop.pump(), 3)
        loop.release_input()
        self.assertEqual(machine.state, VoiceLoopState.TRANSCRIBING)

        report = loop.complete_turn()
        self.assertTrue(report.accepted)
        self.assertTrue(report.submitted)
        self.assertTrue(report.spoken)
        self.assertEqual(speaker.starts, 1)
        self.assertEqual(stream.chunks, ["好的，第一句。第二句。"])
        self.assertEqual(stream.finished, 1)
        self.assertEqual(stream.cancelled, 0)
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertIsNone(registry.active)
        self.assertEqual(
            [item.to_state for item in machine.transitions],
            [
                VoiceLoopState.CAPTURING,
                VoiceLoopState.TRANSCRIBING,
                VoiceLoopState.ROUTING,
                VoiceLoopState.REPLYING,
                VoiceLoopState.SPEAKING,
                VoiceLoopState.IDLE,
            ],
        )

    def test_second_turn_uses_a_fresh_boundary(self) -> None:
        driver = FakeDriver()
        loop, _, _, _ = build_loop(driver=driver, speaker=FakeSpeaker(FakeStream()))
        first = loop.start_turn()
        loop.release_input()
        loop.complete_turn()
        second = loop.start_turn()
        loop.release_input()
        loop.complete_turn()
        self.assertNotEqual(first.identity, second.identity)

    def test_text_channel_cannot_start_a_voice_turn(self) -> None:
        loop, _, machine, _ = build_loop(voice=False)
        with self.assertRaises(VoiceLoopError):
            loop.start_turn()
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_parallel_turns_are_refused(self) -> None:
        loop, _, _, _ = build_loop()
        loop.start_turn()
        with self.assertRaises(VoiceLoopError):
            loop.start_turn()


class TaskReplyAndNoticeTests(unittest.TestCase):
    """P10 · T1 批次 3：任务文本必须在**本轮 REPLYING 内**播报。

    真机教训（2026-10-13）：`--voice --auto` 里计划/结果只**打印**、一个字都没念 —— 因为旧实现
    在轮次结束后调 `_speak`，而状态机只允许 ``REPLYING -> SPEAKING``（非法迁移被吞掉）。
    """

    @staticmethod
    def _transcribing(loop):
        loop.start_turn()
        loop.release_input()  # CAPTURING -> TRANSCRIBING（等待 complete_turn）

    def test_reply_provider_text_is_spoken_inside_the_turn(self) -> None:
        stream = FakeStream()
        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(stream))
        self._transcribing(loop)
        calls: list[int] = []

        def provider() -> str:
            calls.append(1)
            return "准备执行「打开应用：计算器」。\n请说「确认」执行。"

        report = loop.complete_turn(reply_provider=provider)
        self.assertEqual(calls, [1])
        self.assertEqual(stream.chunks, ["准备执行「打开应用：计算器」。\n请说「确认」执行。"])
        self.assertEqual(stream.finished, 1)
        self.assertTrue(report.spoken)
        self.assertEqual(report.reason, "speech_finished")
        self.assertIs(machine.state, VoiceLoopState.IDLE)

    def test_absent_provider_keeps_the_driver_reply_path(self) -> None:
        stream = FakeStream()
        loop, _, _, _ = build_loop(speaker=FakeSpeaker(stream))
        self._transcribing(loop)
        report = loop.complete_turn()
        self.assertEqual(stream.chunks, ["好的，第一句。第二句。"])
        self.assertTrue(report.spoken)
        self.assertEqual(report.reason, "speech_finished")

    def test_blank_or_failing_provider_falls_back_to_the_driver_reply(self) -> None:
        for provider in (lambda: "   ", lambda: None):
            with self.subTest(provider=provider):
                stream = FakeStream()
                loop, _, machine, _ = build_loop(speaker=FakeSpeaker(stream))
                self._transcribing(loop)
                report = loop.complete_turn(reply_provider=provider)
                self.assertEqual(stream.chunks, ["好的，第一句。第二句。"])
                self.assertTrue(report.spoken)
                self.assertIs(machine.state, VoiceLoopState.IDLE)

        def boom() -> str:
            raise RuntimeError("任务层炸了")

        stream = FakeStream()
        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(stream))
        self._transcribing(loop)
        report = loop.complete_turn(reply_provider=boom)
        self.assertEqual(stream.chunks, ["好的，第一句。第二句。"])
        self.assertTrue(report.spoken)
        self.assertIs(machine.state, VoiceLoopState.IDLE)

    def test_speak_notice_works_while_idle_and_keeps_the_machine_idle(self) -> None:
        stream = FakeStream()
        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(stream))
        self.assertIs(machine.state, VoiceLoopState.IDLE)
        spoken, reason = loop.speak_notice("已取消，未执行任何操作。")
        self.assertTrue(spoken)
        self.assertEqual(reason, "speech_finished")
        self.assertEqual(stream.chunks, ["已取消，未执行任何操作。"])
        self.assertEqual(stream.finished, 1)
        self.assertIs(machine.state, VoiceLoopState.IDLE)

    def test_speak_notice_is_fail_safe(self) -> None:
        cases = (
            (FakeSpeaker(error=RuntimeError("no device")), "speech_start_failed"),
            (FakeSpeaker(FakeStream(push_error=ValueError("boom"))), "speech_failed"),
        )
        for speaker, prefix in cases:
            with self.subTest(prefix=prefix):
                loop, _, machine, _ = build_loop(speaker=speaker)
                spoken, reason = loop.speak_notice("x")
                self.assertFalse(spoken)
                self.assertTrue(reason.startswith(prefix), reason)
                self.assertIs(machine.state, VoiceLoopState.IDLE)
        loop, _, _, _ = build_loop(speaker=FakeSpeaker(None))
        self.assertEqual(loop.speak_notice("x"), (False, "text_only"))
        loop2, _, _, _ = build_loop(speaker=FakeSpeaker(FakeStream()))
        self.assertEqual(loop2.speak_notice("   "), (False, "text_only"))


class ChatVoiceLoopFailureTests(unittest.TestCase):
    def test_driver_start_refusal_retires_the_boundary(self) -> None:
        driver = FakeDriver(start_ok=False)
        loop, registry, machine, _ = build_loop(driver=driver)
        self.assertIsNone(loop.start_turn())
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertIsNone(registry.active)
        self.assertEqual(loop.last_report.reason, "start_rejected")
        self.assertFalse(loop.last_report.submitted)

    def test_driver_start_exception_is_fail_closed(self) -> None:
        driver = FakeDriver(start_error=RuntimeError("device gone"))
        loop, registry, machine, _ = build_loop(driver=driver)
        with self.assertRaises(VoiceLoopError):
            loop.start_turn()
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertIsNone(registry.active)

    def test_rejected_gate_result_produces_no_speech(self) -> None:
        driver = FakeDriver(
            result=driver_result(
                accepted=False,
                submitted=False,
                speech_eligible=False,
                reply_text=None,
                reason="reject_empty",
            )
        )
        stream = FakeStream()
        loop, registry, machine, _ = build_loop(driver=driver, speaker=FakeSpeaker(stream))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertFalse(report.accepted)
        self.assertFalse(report.spoken)
        self.assertEqual(stream.chunks, [])
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertIsNone(registry.active)

    def test_agent_failure_is_not_spoken(self) -> None:
        driver = FakeDriver(
            result=driver_result(
                speech_eligible=False,
                reply_text=None,
                reason="accepted_final",
            )
        )
        stream = FakeStream()
        loop, _, machine, _ = build_loop(driver=driver, speaker=FakeSpeaker(stream))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertTrue(report.submitted)
        self.assertFalse(report.spoken)
        self.assertEqual(stream.chunks, [])
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_speaker_failure_never_changes_the_conversation_fact(self) -> None:
        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(error=RuntimeError("no device")))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertTrue(report.submitted)
        self.assertFalse(report.spoken)
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_speaker_start_failure_gets_its_own_reason_code(self) -> None:
        """R4-B 真机教训：启动失败必须与"本轮没有播报通道"分开记，否则真因不可见。"""

        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(error=RuntimeError("no device")))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "speech_start_failed:RuntimeError")
        self.assertFalse(report.spoken)
        self.assertTrue(report.submitted)
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_speaker_error_name_is_the_only_extra_detail(self) -> None:
        """原因码只带异常**类型名**，不带异常正文（不泄露路径/设备细节）。"""

        speaker = FakeSpeaker(error=ValueError("/dev/secret path"))
        loop, _, _, _ = build_loop(speaker=speaker)
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "speech_start_failed:ValueError")
        self.assertNotIn("secret", report.reason)

    def test_missing_stream_keeps_text_only(self) -> None:
        """`start_stream()` 返回 None（本轮没有播报通道）→ 仍是 text_only，不是失败。"""

        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(None))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "text_only")
        self.assertFalse(report.spoken)
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_speaker_without_start_stream_keeps_text_only(self) -> None:
        class BareSpeaker:
            pass

        loop, _, machine, _ = build_loop(speaker=BareSpeaker())
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "text_only")
        self.assertFalse(report.spoken)

    def test_finish_failure_reason_stays_speech_failed(self) -> None:
        stream = FakeStream(finish_error=RuntimeError("provider failed"))
        loop, _, _, _ = build_loop(speaker=FakeSpeaker(stream))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        # R4-B：如实带上异常类型名（长回复真机"说一半就停"时只有 speech_failed 无法点名）。
        self.assertEqual(report.reason, "speech_failed:RuntimeError")
        self.assertFalse(report.spoken)

    def test_finish_failure_cancels_the_stream_and_keeps_the_report(self) -> None:
        stream = FakeStream(finish_error=RuntimeError("provider failed"))
        loop, _, machine, _ = build_loop(speaker=FakeSpeaker(stream))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertTrue(report.submitted)
        self.assertFalse(report.spoken)
        self.assertEqual(stream.cancelled, 1)
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_driver_release_exception_cleans_up_and_propagates(self) -> None:
        driver = FakeDriver(release_error=RuntimeError("asr failed"))
        loop, registry, machine, _ = build_loop(driver=driver)
        loop.start_turn()
        loop.release_input()
        with self.assertRaises(RuntimeError):
            loop.complete_turn()
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertIsNone(registry.active)

    def test_cancel_turn_stops_the_driver_and_retires_the_boundary(self) -> None:
        driver = FakeDriver()
        loop, registry, machine, _ = build_loop(driver=driver)
        active = loop.start_turn()
        cancelled = loop.cancel_turn(reason="cancelled")
        self.assertEqual(cancelled.identity, active.identity)
        self.assertEqual(driver.cancels, ["cancelled"])
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertIsNone(registry.active)
        with self.assertRaises(VoiceLoopError):
            loop.complete_turn()

    def test_input_degradation_mid_capture_is_detected(self) -> None:
        driver = FakeDriver()
        stream = FakeStream()
        loop, registry, machine, seam = build_loop(driver=driver, speaker=FakeSpeaker(stream))
        loop.start_turn()
        seam.degrade(reason="asr_stream_failed")
        report = loop.complete_turn()
        self.assertFalse(report.submitted)
        self.assertFalse(report.spoken)
        self.assertEqual(report.reason, "input_degraded")
        self.assertEqual(driver.cancels, ["degraded"])
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        self.assertEqual(stream.chunks, [])
        self.assertIsNone(registry.active)

    def test_driver_side_speech_is_not_repeated_by_the_loop(self) -> None:
        driver = FakeDriver(result=driver_result(speech_handled=True))
        stream = FakeStream()
        loop, _, machine, _ = build_loop(driver=driver, speaker=FakeSpeaker(stream))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertFalse(report.spoken)
        self.assertEqual(stream.chunks, [])
        self.assertEqual(machine.state, VoiceLoopState.IDLE)

    def test_wrong_call_order_is_refused(self) -> None:
        loop, _, machine, _ = build_loop()
        with self.assertRaises(VoiceLoopError):
            loop.complete_turn()
        with self.assertRaises(VoiceLoopError):
            loop.release_input()
        loop.start_turn()
        loop.release_input()
        self.assertEqual(machine.state, VoiceLoopState.TRANSCRIBING)
        with self.assertRaises(VoiceLoopError):
            loop.release_input()
        with self.assertRaises(VoiceLoopError):
            loop.cancel_turn(reason="not_a_reason")

    def test_public_surface_has_no_authorization_or_execution_api(self) -> None:
        loop, _, _, _ = build_loop()
        public = {name for name in dir(loop) if not name.startswith("_")}
        self.assertEqual(public, LOOP_PUBLIC_API)
        for name in public:
            lowered = name.lower()
            for forbidden in ("authoriz", "approve", "execute", "plan", "ledger", "consume"):
                self.assertNotIn(forbidden, lowered)


class PushToTalkVoiceDriverTests(unittest.TestCase):
    def stub(self, **overrides: object):
        class Stub:
            def __init__(self, *, started=True, decision=ResultGateDecision.ACCEPTED_FINAL, result=None, frames=2):
                self.started = started
                self.decision = decision
                self.result = result
                self.frames = frames
                self.boundary = None
                self.release_kwargs: dict[str, object] = {}
                self.failure_code = None

            def start(self, asr_boundary):
                self.boundary = asr_boundary
                return type("Start", (), {"started": self.started})()

            def pump(self):
                return type("Pump", (), {"processed_frames": self.frames})()

            def release(self, **kwargs):
                self.release_kwargs = kwargs
                return type(
                    "Turn",
                    (),
                    {
                        "gate_result": type("Gate", (), {"decision": self.decision})(),
                        "outcome": self.result,
                        "provider_event": None,
                    },
                )()

            def runtime_failed(self, *, code):
                self.failure_code = code

        values: dict[str, object] = {}
        values.update(overrides)
        return Stub(**values)  # type: ignore[arg-type]

    def test_start_maps_the_voice_boundary_into_an_asr_boundary(self) -> None:
        coordinator = self.stub()
        driver = PushToTalkVoiceDriver(
            coordinator=coordinator,
            audio_session_id="voice-loop-audio",
            stream_id="voice-loop-stream",
            provider_generation=3,
        )
        item = boundary()
        self.assertTrue(driver.start(item))
        self.assertEqual(coordinator.boundary.session_id, item.voice_session_id)
        self.assertEqual(coordinator.boundary.utterance_id, item.utterance_id)
        self.assertEqual(coordinator.boundary.audio_session_id, "voice-loop-audio")
        self.assertEqual(coordinator.boundary.stream_id, "voice-loop-stream")
        self.assertEqual(coordinator.boundary.provider_generation, 3)
        self.assertEqual(driver.pump(), 2)

    def test_release_forwards_turn_and_request_identity(self) -> None:
        coordinator = self.stub(result=outcome())
        driver = PushToTalkVoiceDriver(coordinator=coordinator)
        item = boundary()
        driver.start(item)
        result = driver.release()
        self.assertEqual(
            coordinator.release_kwargs,
            {"turn_id": item.turn_id, "request_id": item.request_id},
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertTrue(result.speech_eligible)
        self.assertEqual(result.reply_text, "好的，第一句。第二句。")
        self.assertEqual(result.reason, "accepted_final")

    def test_rejected_gate_result_is_not_submitted(self) -> None:
        coordinator = self.stub(
            decision=ResultGateDecision.REJECT_EMPTY,
            result=None,
        )
        driver = PushToTalkVoiceDriver(coordinator=coordinator)
        driver.start(boundary())
        result = driver.release()
        self.assertFalse(result.accepted)
        self.assertFalse(result.submitted)
        self.assertFalse(result.speech_eligible)
        self.assertIsNone(result.reply_text)
        self.assertEqual(result.reason, "reject_empty")

    def test_task_route_submission_without_agent_reply_counts_as_submitted(self) -> None:
        coordinator = self.stub(result=None)
        driver = PushToTalkVoiceDriver(coordinator=coordinator)
        driver.start(boundary())
        result = driver.release()
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertFalse(result.speech_eligible)
        self.assertIsNone(result.reply_text)

    def test_agent_failure_is_not_speech_eligible(self) -> None:
        coordinator = self.stub(result=outcome(completed=False, text=None, finish_reason=None))
        driver = PushToTalkVoiceDriver(coordinator=coordinator)
        driver.start(boundary())
        result = driver.release()
        self.assertTrue(result.accepted)
        self.assertTrue(result.submitted)
        self.assertFalse(result.speech_eligible)
        self.assertIsNone(result.reply_text)

    def test_cancel_maps_to_the_cancelled_failure_code(self) -> None:
        coordinator = self.stub()
        driver = PushToTalkVoiceDriver(coordinator=coordinator)
        driver.start(boundary())
        driver.cancel(reason="cancelled")
        self.assertIs(coordinator.failure_code, AsrFailureCode.CANCELLED)

    def test_release_and_cancel_before_start_are_refused(self) -> None:
        driver = PushToTalkVoiceDriver(coordinator=self.stub())
        with self.assertRaises(VoiceLoopError):
            driver.release()
        with self.assertRaises(VoiceLoopError):
            driver.cancel(reason="cancelled")
        self.assertEqual(driver.pump(), 0)

    def test_driver_requires_a_coordinator_with_the_p6_surface(self) -> None:
        with self.assertRaises(TypeError):
            PushToTalkVoiceDriver(coordinator=object())


if __name__ == "__main__":
    unittest.main()
