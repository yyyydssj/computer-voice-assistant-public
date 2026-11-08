"""2026-10-24：`text_only` 定性所需的三个只读事实（`reply_chars`/`speech_skip_reason`/`route`）。

真机缺陷（交接文档 §5.1）：`voice_turn` 里 `spoken=false`、`reason=text_only` 的轮次既没有
任何 `speech_*` 事实，也没有回复文本 —— 因为 `VoiceDriverTurnResult.reply_text` 在不可播报时
被置为 `None`。于是两种完全不同的成因在日志里**同形**：

1. 路由交接不是 ``chat`` ⇒ `ConversationManager._submit_routed()` 根本不调用 Agent
   （`outcome is None`）；
2. `route == "chat"` 但 Agent 轮不可播报（`completed=False` / 空白回复 / 缺终止原因）。

本批只新增**只读事实**把两者分开，不改变任何播报、授权、执行或路由语义。
"""

from __future__ import annotations

import unittest

from app.agent.adapter import AgentFailure, AgentFailureCategory, AgentTurnOutcome
from app.asr.result_gate import ResultGateDecision
from app.contracts.base import require_identifier
from app.runtime.auto_voice import AutoTurnFacts
from app.runtime.voice_entry import VoiceTurnFacts
from app.voice_loop.boundary import VoiceLoopError, VoiceTurnBoundary
from app.voice_loop.chat_loop import MAX_REPLY_LENGTH, VoiceDriverTurnResult
from app.voice_loop.drivers import (
    PushToTalkVoiceDriver,
    reply_chars,
    route_code,
    speech_eligible,
    speech_skip_reason,
)

TIMESTAMP = "2026-10-01T09:00:00.000Z"
REPLY = "好的，第一句。第二句。"


def outcome(**overrides: object) -> AgentTurnOutcome:
    values: dict[str, object] = {
        "request_id": "asr-request-1",
        "session_id": "session-1",
        "completed": True,
        "text": REPLY,
        "finish_reason": "stop",
        "failure": None,
    }
    values.update(overrides)
    return AgentTurnOutcome(**values)  # type: ignore[arg-type]


def agent_failure(category: AgentFailureCategory = AgentFailureCategory.STREAM_INTERRUPTED) -> AgentFailure:
    return AgentFailure(category=category, stage="conversation", diagnostic_id="diag-1")


def boundary() -> VoiceTurnBoundary:
    return VoiceTurnBoundary(
        voice_session_id="voice-session-1",
        utterance_id="asr-utterance-1",
        turn_id="asr-turn-1",
        request_id="asr-request-1",
        reply_id="asr-reply-1",
        source="asr",
        created_at=TIMESTAMP,
    )


class StubTurn:
    """一轮协调器结果的最小替身：只提供 driver 真实读取的那几个字段。"""

    def __init__(
        self,
        *,
        outcome: object = None,
        route: object = None,
        decision: ResultGateDecision = ResultGateDecision.ACCEPTED_FINAL,
        event_text: object = "你好。",
    ) -> None:
        self.gate_result = type(
            "GateResult",
            (),
            {"decision": decision, "event": type("Event", (), {"text": event_text})()},
        )()
        self.outcome = outcome
        self.route = route


class StubCoordinator:
    """记录调用并回放一轮结果；不联网、不触设备。"""

    def __init__(self, turn: StubTurn) -> None:
        self.turn = turn
        self.release_kwargs: dict[str, object] = {}
        self.failure_code: object = None

    def start(self, boundary: object) -> object:
        self.boundary = boundary
        return type("StartResult", (), {"started": True})()

    def pump(self) -> object:
        return type("PumpResult", (), {"processed_frames": 0})()

    def release(self, **kwargs: object) -> StubTurn:
        self.release_kwargs = kwargs
        return self.turn

    def runtime_failed(self, *, code: object) -> None:
        self.failure_code = code


def release_with(turn: StubTurn) -> VoiceDriverTurnResult:
    driver = PushToTalkVoiceDriver(coordinator=StubCoordinator(turn))
    driver.start(boundary())
    return driver.release()


class SpeechSkipReasonTests(unittest.TestCase):
    """跳过播报的固定短码：逐条对应 `speech_eligible` 的每个不满足项。"""

    def test_eligible_reply_has_no_skip_reason(self) -> None:
        self.assertTrue(speech_eligible(outcome()))
        self.assertIsNone(speech_skip_reason(outcome()))

    def test_missing_outcome_says_so(self) -> None:
        """路由不是 chat 时 Agent 根本不会被调用 —— 这是 `text_only` 的头号候选成因。"""
        self.assertEqual(speech_skip_reason(None), "no_outcome")
        self.assertEqual(speech_skip_reason("not-an-outcome"), "no_outcome")

    def test_missing_outcome_carries_the_route(self) -> None:
        self.assertEqual(
            speech_skip_reason(None, route="task"), "no_outcome:route:task"
        )
        self.assertEqual(
            speech_skip_reason(None, route="unsupported"), "no_outcome:route:unsupported"
        )

    def test_not_completed_reports_the_failure_category(self) -> None:
        case = outcome(completed=False, failure=agent_failure())
        self.assertEqual(
            speech_skip_reason(case), "not_completed:stream_interrupted"
        )
        self.assertEqual(
            speech_skip_reason(outcome(completed=False, text="")), "not_completed"
        )

    def test_completed_with_failure_is_a_distinct_bucket(self) -> None:
        case = outcome(failure=agent_failure(AgentFailureCategory.RATE_LIMITED))
        self.assertFalse(speech_eligible(case))
        self.assertEqual(speech_skip_reason(case), "failure:rate_limited")

    def test_blank_text_and_missing_finish_reason_are_separate(self) -> None:
        self.assertEqual(speech_skip_reason(outcome(text="   ")), "empty_text")
        self.assertEqual(speech_skip_reason(outcome(finish_reason=None)), "missing_finish_reason")
        self.assertEqual(speech_skip_reason(outcome(finish_reason="  ")), "missing_finish_reason")

    def test_every_code_is_a_restricted_identifier(self) -> None:
        """日志里只允许固定短码：任何一条都必须是 `require_identifier` 能接受的。"""

        codes = [
            speech_skip_reason(None),
            speech_skip_reason(None, route="task"),
            speech_skip_reason(outcome(completed=False, failure=agent_failure())),
            speech_skip_reason(outcome(completed=False)),
            speech_skip_reason(outcome(failure=agent_failure(AgentFailureCategory.TIMEOUT))),
            speech_skip_reason(outcome(text="")),
            speech_skip_reason(outcome(finish_reason=None)),
        ]
        for code in codes:
            with self.subTest(code=code):
                self.assertIsNotNone(code)
                require_identifier("speech_skip_reason", code)


class ReadOnlyProjectionTests(unittest.TestCase):
    def test_reply_chars_counts_text_even_when_not_eligible(self) -> None:
        self.assertEqual(reply_chars(outcome()), len(REPLY))
        # 关键：**不可播报**时也如实给出字数 —— 否则真机上"有没有文字"完全不可见。
        self.assertEqual(
            reply_chars(outcome(completed=False, text="半句话", failure=agent_failure())), 3
        )
        self.assertEqual(reply_chars(outcome(text="")), 0)
        self.assertIsNone(reply_chars(None))
        self.assertIsNone(reply_chars(outcome(text=123)))

    def test_route_code_never_accepts_free_text(self) -> None:
        self.assertEqual(route_code("chat"), "chat")
        self.assertEqual(route_code("task"), "task")
        self.assertIsNone(route_code(None))
        self.assertIsNone(route_code(123))
        self.assertIsNone(route_code("这是一句自由文本"))
        self.assertIsNone(route_code("has space"))


class DriverReleaseTests(unittest.TestCase):
    """驱动层必须把三个事实一路带出来（两条驱动路径共用同一批助手）。"""

    def test_eligible_reply_carries_route_and_no_skip_reason(self) -> None:
        result = release_with(StubTurn(outcome=outcome(), route="chat"))
        self.assertTrue(result.speech_eligible)
        self.assertEqual(result.reply_text, REPLY)
        self.assertEqual(result.route, "chat")
        self.assertEqual(result.reply_chars, len(REPLY))
        self.assertIsNone(result.speech_skip_reason)

    def test_non_chat_route_is_told_apart_from_agent_failure(self) -> None:
        """这就是 `text_only` 的两种成因：旧事实里两者完全同形。"""

        routed = release_with(StubTurn(outcome=None, route="task"))
        self.assertTrue(routed.accepted)
        self.assertTrue(routed.submitted)
        self.assertFalse(routed.speech_eligible)
        self.assertIsNone(routed.reply_text)
        self.assertIsNone(routed.reply_chars)
        self.assertEqual(routed.route, "task")
        self.assertEqual(routed.speech_skip_reason, "no_outcome:route:task")

        failed = release_with(
            StubTurn(
                outcome=outcome(completed=False, text="半句话", failure=agent_failure()),
                route="chat",
            )
        )
        self.assertFalse(failed.speech_eligible)
        self.assertIsNone(failed.reply_text)
        self.assertEqual(failed.reply_chars, 3)
        self.assertEqual(failed.route, "chat")
        self.assertEqual(failed.speech_skip_reason, "not_completed:stream_interrupted")

    def test_rejected_turn_claims_no_skip_reason(self) -> None:
        """未接纳的轮次不是"跳过了播报"，不应冒充成因（原因码已由 `reason` 说明）。"""

        result = release_with(
            StubTurn(
                outcome=None,
                route=None,
                decision=ResultGateDecision.REJECT_EMPTY,
            )
        )
        self.assertFalse(result.accepted)
        self.assertIsNone(result.speech_skip_reason)
        self.assertIsNone(result.reply_chars)
        self.assertIsNone(result.route)


class ResultContractTests(unittest.TestCase):
    def test_new_fields_default_to_none(self) -> None:
        result = VoiceDriverTurnResult(accepted=True, submitted=True)
        self.assertIsNone(result.reply_chars)
        self.assertIsNone(result.speech_skip_reason)
        self.assertIsNone(result.route)

    def test_reply_chars_must_be_a_bounded_non_negative_int(self) -> None:
        for bad in (-1, True, "12", 1.5, MAX_REPLY_LENGTH + 1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    VoiceDriverTurnResult(accepted=True, submitted=True, reply_chars=bad)

    def test_skip_reason_and_route_must_be_restricted_codes(self) -> None:
        for kwargs in (
            {"speech_skip_reason": "这不是短码"},
            {"speech_skip_reason": "has space"},
            {"speech_skip_reason": ""},
            {"route": "这不是路由"},
            {"route": 7},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((VoiceLoopError, ValueError)):
                    VoiceDriverTurnResult(accepted=True, submitted=True, **kwargs)

    def test_no_free_text_is_smuggled_through_the_new_fields(self) -> None:
        result = VoiceDriverTurnResult(
            accepted=True,
            submitted=True,
            reply_chars=len(REPLY),
            speech_skip_reason="no_outcome:route:task",
            route="task",
        )
        # 三个新字段都只是计数/固定码：整个不可变结果的表示里都不该出现回复正文。
        self.assertNotIn(REPLY, repr(result))


class AutoDriverMappingTests(unittest.TestCase):
    """免按键路径（`auto_driver._run_coordinator_release`）必须与按键路径同口径。"""

    def _release(self, turn: StubTurn) -> VoiceDriverTurnResult:
        from app.voice_loop.auto_driver import AutoCaptureVoiceDriver

        driver = AutoCaptureVoiceDriver.__new__(AutoCaptureVoiceDriver)
        driver._coordinator = StubCoordinator(turn)
        driver._discard_on_release = False
        driver._stop_reason = None
        driver._speech_handled_by_driver = False
        return driver._run_coordinator_release(boundary())

    def test_non_chat_route_carries_the_route_code(self) -> None:
        result = self._release(StubTurn(outcome=None, route="ambiguous"))
        self.assertTrue(result.accepted)
        self.assertEqual(result.route, "ambiguous")
        self.assertEqual(result.speech_skip_reason, "no_outcome:route:ambiguous")
        self.assertIsNone(result.reply_chars)

    def test_agent_failure_carries_the_category_and_text_length(self) -> None:
        result = self._release(
            StubTurn(
                outcome=outcome(
                    completed=False, text=REPLY, failure=agent_failure(AgentFailureCategory.EMPTY_RESPONSE)
                ),
                route="chat",
            )
        )
        self.assertEqual(result.route, "chat")
        self.assertEqual(result.reply_chars, len(REPLY))
        self.assertEqual(result.speech_skip_reason, "not_completed:empty_response")


class LogDetailsTests(unittest.TestCase):
    """真机上唯一看得见的地方：`voice_turn` 的 details。"""

    def test_auto_turn_facts_expose_the_three_facts(self) -> None:
        facts = AutoTurnFacts(
            index=1,
            boundary=None,
            frames=10,
            speech_seen=True,
            end_reason="silence_timeout",
            accepted=True,
            submitted=True,
            spoken=False,
            reason="text_only",
            seconds=3.0,
            reply_chars=len(REPLY),
            speech_skip_reason="no_outcome:route:task",
            route="task",
        )
        details = facts.log_details()
        self.assertEqual(details["reply_chars"], len(REPLY))
        self.assertEqual(details["speech_skip_reason"], "no_outcome:route:task")
        self.assertEqual(details["route"], "task")

    def test_voice_turn_facts_expose_the_three_facts(self) -> None:
        facts = VoiceTurnFacts(
            index=1,
            boundary=None,
            frames=10,
            speech_seen=True,
            end_reason="released",
            accepted=True,
            submitted=True,
            spoken=False,
            reason="text_only",
            seconds=3.0,
            reply_chars=len(REPLY),
            speech_skip_reason="not_completed:stream_interrupted",
            route="chat",
        )
        details = facts.log_details()
        self.assertEqual(details["reply_chars"], len(REPLY))
        self.assertEqual(details["speech_skip_reason"], "not_completed:stream_interrupted")
        self.assertEqual(details["route"], "chat")

    def test_log_values_stay_flat_and_text_free(self) -> None:
        facts = AutoTurnFacts(
            index=1,
            boundary=None,
            frames=10,
            speech_seen=True,
            end_reason="silence_timeout",
            accepted=True,
            submitted=True,
            spoken=False,
            reason="text_only",
            seconds=3.0,
            reply_chars=len(REPLY),
            speech_skip_reason="no_outcome:route:task",
            route="task",
        )
        for name in ("reply_chars", "speech_skip_reason", "route"):
            with self.subTest(name=name):
                value = facts.log_details()[name]
                self.assertIn(type(value), (int, str, type(None)))
        # 生命周期事件只允许扁平基础类型，且绝不携带识别/回复正文。
        self.assertNotIn(REPLY, str(facts.log_details()))


if __name__ == "__main__":
    unittest.main()
