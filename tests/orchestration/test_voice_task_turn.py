"""P10 · T1 批次 3 红测：语音任务轮状态机（全部离线）。

- 控制类用例（不受支持 / 计划失败 / 确认失败 / 陈旧确认）用 `SimpleNamespace` 替身；
- 主链路（待确认 → 确认执行 / 取消 / 超时）直接接**真实派发会话 + 假 backend**，
  因此可以断言真实事实：**确认之前零执行**、确认之后恰好执行一次、编排器被收尾。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.routing import RouteInput
from app.runtime.orchestration.local_task_router import LocalTaskRouter
from app.runtime.orchestration.pending_confirmation import (
    DROPPED_CANCELLED,
    DROPPED_REPLACED,
    DROPPED_TIMEOUT,
    PENDING_CONFIRMATION_PROVIDER,
    PendingConfirmationRouter,
    is_pending_confirmation,
    is_repeat_confirmation,
)
from app.runtime.orchestration.task_dispatch import DENIED_TEXT, TIMEOUT_TEXT, TaskDispatchResult
from app.runtime.orchestration.task_flow import TaskFlowState
from app.runtime.orchestration.voice_lines import VoiceLines
from app.runtime.orchestration.voice_task_turn import (
    CONFIRM_HINT,
    CONFIRM_KEY_ANSWER,
    CONFIRM_SOURCE_ASR,
    CONFIRM_SOURCE_KEYBOARD,
    KIND_AWAITING,
    KIND_CANCELLED,
    KIND_DENIED,
    KIND_EXECUTED,
    KIND_PREVIEW_FAILED,
    KIND_STALE,
    KIND_UNSUPPORTED,
    REASON_KEY_CANCEL,
    REASON_REPEAT,
    REASON_REPROMPT,
    VoiceTaskTurn,
)
from tests.orchestration.test_task_dispatch import _Factory, build_session, make_request
from tests.orchestration.test_task_flow import FakeAudit

CREATED_AT = "2026-10-13T00:00:00Z"


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakePrepared:
    def __init__(self, plan_hash: str = "sha256:" + "a" * 64) -> None:
        self.plan_hash = plan_hash


class _FakeDispatcher:
    """控制类用例用：返回值全部可定制，记录调用顺序。"""

    def __init__(
        self,
        *,
        prepare: object | None = None,
        result: TaskDispatchResult | None = None,
    ) -> None:
        self.prepare_calls: list[object] = []
        self.execute_calls: list[tuple[object, object]] = []
        self.cancel_calls: list[object] = []
        self._prepare = prepare
        self._result = result or TaskDispatchResult(
            status=KIND_EXECUTED, reason_code="executed", text="任务执行完成：共 1 个动作，成功 1。",
            plan_hash="sha256:" + "b" * 64,
        )

    def prepare(self, handoff: object) -> object:
        self.prepare_calls.append(handoff)
        if self._prepare is not None:
            return self._prepare
        return SimpleNamespace(
            ok=True, status="ready", reason_code="prepared",
            text="准备执行「打开应用：计算器」：共 1 个动作，风险等级 L2。",
            prepared=_FakePrepared(),
        )

    def execute(self, prepared: object, *, answer: object = None) -> TaskDispatchResult:
        self.execute_calls.append((prepared, answer))
        return self._result

    def cancel(self, prepared: object) -> TaskDispatchResult:
        self.cancel_calls.append(prepared)
        return TaskDispatchResult(
            status="denied", reason_code="authorization_denied", text=DENIED_TEXT,
            plan_hash="sha256:" + "c" * 64,
        )


class _RealDispatcher:
    """真实派发会话的薄包装（记录调用，返回真实结果）。"""

    def __init__(self, session) -> None:  # noqa: ANN001 - 测试替身
        self._session = session
        self.prepare_calls: list[object] = []
        self.execute_calls: list[tuple[object, object]] = []

    def prepare(self, handoff: RouteHandoff) -> object:
        self.prepare_calls.append(handoff)
        return self._session.prepare(make_request(handoff=handoff))

    def execute(self, prepared: object, *, answer: object = None) -> TaskDispatchResult:
        self.execute_calls.append((prepared, answer))
        return self._session.execute(prepared, answer=answer)

    def cancel(self, prepared: object) -> TaskDispatchResult:
        return self._session.cancel(prepared)


def _trace(text: str, source: str = "asr") -> RouteTrace:
    return RouteTrace(
        request_id="req-1", conversation_id="conv-1", turn_id="turn-1", source=source,
        raw_text=text, created_at=CREATED_AT,
    )


def _task_handoff(text: str = "打开计算器") -> RouteHandoff:
    handoff = LocalTaskRouter().route(RouteInput(raw_text=text, trace=_trace(text)))
    assert handoff is not None and handoff.route == "task"
    return handoff


def _chat_handoff(text: str = "你好") -> RouteHandoff:
    return RouteHandoff(
        trace=_trace(text), route="chat", intent="general_chat", handling_mode="chat_reply",
        confidence=1.0, slots={}, required_confirmation="none", tool_scope=(),
        router=RouterStatus(
            provider="stub", model_id="stub-router", model_version="1", status="success",
            failure_code=None,
        ),
    )


def _confirmed_handoff(text: str = "确认") -> RouteHandoff:
    task = _task_handoff()
    return RouteHandoff(
        trace=_trace(text), route=task.route, intent=task.intent, handling_mode=task.handling_mode,
        confidence=task.confidence, slots=dict(task.slots),
        required_confirmation=task.required_confirmation, tool_scope=task.tool_scope,
        router=RouterStatus(
            provider=PENDING_CONFIRMATION_PROVIDER, model_id=task.router.model_id,
            model_version=task.router.model_version, status="success", failure_code=None,
        ),
    )


class _StubFallback:
    def route(self, request: RouteInput) -> RouteHandoff:
        return _chat_handoff(request.trace.raw_text)


def _router(clock: _Clock | None = None, names: tuple[str, ...] = ()) -> PendingConfirmationRouter:
    return PendingConfirmationRouter(
        first=LocalTaskRouter(names=names), fallback=_StubFallback(), clock=clock or _Clock()
    )


class AwaitingTests(unittest.TestCase):
    def test_new_task_is_announced_armed_and_not_executed(self) -> None:
        clock = _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        outcomes = turn.handle(_task_handoff())
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(outcome.kind, KIND_AWAITING)
        self.assertEqual(outcome.reason, "task_awaiting_confirmation")
        self.assertIn("请说「确认」执行", outcome.spoken_text or "")
        self.assertIn("风险等级 L2", outcome.spoken_text or "")
        self.assertTrue((outcome.plan_hash or "").startswith("sha256:"))
        self.assertIn("task_kind", outcome.to_details())
        self.assertIsNotNone(router.pending)
        self.assertEqual(dispatcher.execute_calls, [])
        self.assertIsNotNone(turn.pending)

    def test_chat_turn_is_untouched(self) -> None:
        clock = _Clock()
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=dispatcher, clock=clock)
        self.assertEqual(turn.handle(_chat_handoff()), ())
        self.assertEqual(dispatcher.prepare_calls, [])

    def test_unsupported_task_is_announced_without_arming(self) -> None:
        clock = _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher(
            prepare=SimpleNamespace(
                ok=False, status="unsupported", reason_code="application_unknown",
                text="这条任务本批暂不支持（application_unknown），未执行任何操作。", prepared=None,
            )
        )
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        outcome = turn.handle(_task_handoff("打开微信"))[0]
        self.assertEqual(outcome.kind, KIND_UNSUPPORTED)
        self.assertEqual(outcome.reason, "task_unsupported")
        self.assertIsNone(router.pending)
        self.assertIsNone(turn.pending)

    def test_preview_failure_is_announced_as_such(self) -> None:
        clock = _Clock()
        dispatcher = _FakeDispatcher(
            prepare=SimpleNamespace(
                ok=False, status="preview_failed", reason_code="tool_not_registered",
                text="计划未通过本地校验，未执行任何操作。", prepared=None,
            )
        )
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=dispatcher, clock=clock)
        outcome = turn.handle(_task_handoff())[0]
        self.assertEqual(outcome.kind, KIND_PREVIEW_FAILED)
        self.assertEqual(outcome.reason_code, "tool_not_registered")


class ShortSpeechTests(unittest.TestCase):
    """#14 真机反馈：不要把全文念出来；工具名/L2 只留在控制台文本（`console_text`）。"""

    def test_dispatcher_short_text_is_spoken_and_full_text_is_kept_for_console(self) -> None:
        clock = _Clock()
        dispatcher = _FakeDispatcher(
            prepare=SimpleNamespace(
                ok=True, status="ready", reason_code="prepared",
                text="准备执行「打开应用：计算器」：共 1 个动作，涉及 start_whitelisted_application，风险等级 L2。",
                spoken_text="要打开计算器，说确认。",
                prepared=_FakePrepared(),
            ),
            result=TaskDispatchResult(
                status="executed", reason_code="executed",
                text="任务执行完成：共 1 个动作，成功 1。",
                spoken_text="帮你打开计算器了。",
            ),
        )
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=dispatcher, clock=clock)

        awaiting = turn.handle(_task_handoff())[0]
        self.assertEqual(awaiting.spoken_text, "要打开计算器，说确认。")
        self.assertIn("start_whitelisted_application", awaiting.console_text or "")
        self.assertIn("风险等级 L2", awaiting.console_text or "")

        executed = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(executed.spoken_text, "帮你打开计算器了。")
        self.assertIn("共 1 个动作", executed.console_text or "")

    def test_unsupported_uses_the_short_spoken_text(self) -> None:
        clock = _Clock()
        dispatcher = _FakeDispatcher(
            prepare=SimpleNamespace(
                ok=False, status="unsupported", reason_code="application_unknown",
                text="这条任务本批暂不支持（application_unknown），未执行任何操作。",
                spoken_text="这条任务本批暂不支持，没有执行。",
                prepared=None,
            )
        )
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=dispatcher, clock=clock)
        outcome = turn.handle(_task_handoff("打开微信"))[0]
        self.assertEqual(outcome.spoken_text, "这条任务本批暂不支持，没有执行。")
        self.assertIn("application_unknown", outcome.console_text or "")


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_executes_and_announces_the_summary(self) -> None:
        clock = _Clock()
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=dispatcher, clock=clock)
        turn.handle(_task_handoff())
        outcome = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(outcome.kind, KIND_EXECUTED)
        self.assertEqual(outcome.status, KIND_EXECUTED)
        self.assertEqual(outcome.reason, "task_executed")
        self.assertIn("任务执行完成", outcome.spoken_text or "")
        self.assertEqual(len(dispatcher.execute_calls), 1)
        self.assertEqual(dispatcher.execute_calls[0][1], "确认")  # 真实 ASR 原文被传下去
        self.assertIsNone(turn.pending)

    def test_confirmation_failure_is_reported_as_denied(self) -> None:
        clock = _Clock()
        dispatcher = _FakeDispatcher(
            result=TaskDispatchResult(
                status="denied", reason_code="authorization_denied", text=DENIED_TEXT
            )
        )
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=dispatcher, clock=clock)
        turn.handle(_task_handoff())
        outcome = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(outcome.kind, KIND_DENIED)
        self.assertEqual(outcome.reason, "task_denied")
        self.assertEqual(outcome.spoken_text, DENIED_TEXT)

    def test_confirmation_without_a_prepared_plan_is_stale(self) -> None:
        clock = _Clock()
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=_FakeDispatcher(), clock=clock)
        outcome = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(outcome.kind, KIND_STALE)
        self.assertEqual(outcome.reason, "task_stale_confirmation")
        self.assertEqual(outcome.spoken_text, DENIED_TEXT)


class DropAndExpireTests(unittest.TestCase):
    def _armed_turn(self, clock: _Clock | None = None):
        clock = clock or _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        turn.handle(_task_handoff())
        return turn, router, dispatcher, clock

    def test_vague_answer_keeps_the_plan_and_reprompts_once(self) -> None:
        """真机教训（2026-10-13）：含糊回答**不得**立刻作废计划。"""

        turn, router, dispatcher, _ = self._armed_turn()
        router.route(RouteInput(raw_text="嗯", trace=_trace("嗯")))
        first = turn.handle(_chat_handoff("嗯"))
        self.assertEqual([item.kind for item in first], [KIND_AWAITING])
        self.assertEqual(first[0].reason_code, REASON_REPROMPT)
        self.assertIn("还没听到确认", first[0].spoken_text or "")
        self.assertEqual(dispatcher.cancel_calls, [])
        self.assertIsNotNone(turn.pending)
        self.assertIsNotNone(router.pending)
        # 第二次含糊回答不再重问（避免反复念叨），但计划仍有效
        router.route(RouteInput(raw_text="那个", trace=_trace("那个")))
        second = turn.handle(_chat_handoff("那个"))
        self.assertEqual(second, ())
        self.assertIsNotNone(turn.pending)

    def test_confirmation_after_a_vague_answer_still_executes(self) -> None:
        turn, router, dispatcher, clock = self._armed_turn()
        router.route(RouteInput(raw_text="嗯", trace=_trace("嗯")))
        turn.handle(_chat_handoff("嗯"))
        clock.now += 3.0
        router.route(RouteInput(raw_text="确认执行", trace=_trace("确认执行")))
        outcome = turn.handle(_confirmed_handoff("确认执行"))[0]
        self.assertEqual(outcome.kind, KIND_EXECUTED)
        self.assertEqual(len(dispatcher.execute_calls), 1)
        self.assertEqual(dispatcher.execute_calls[0][1], "确认执行")

    def test_cancel_word_cancels_immediately_and_is_announced(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        router.route(RouteInput(raw_text="算了", trace=_trace("算了")))
        outcomes = turn.handle(_chat_handoff("算了"))
        self.assertEqual([item.kind for item in outcomes], [KIND_CANCELLED])
        self.assertEqual(outcomes[0].reason_code, DROPPED_CANCELLED)
        self.assertEqual(outcomes[0].spoken_text, DENIED_TEXT)
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertIsNone(turn.pending)

    def test_new_task_sentence_cancels_then_prepares_the_new_one(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        new_handoff = _task_handoff("打开记事本")
        router.route(RouteInput(raw_text="打开记事本", trace=_trace("打开记事本")))
        outcomes = turn.handle(new_handoff)
        self.assertEqual([item.kind for item in outcomes], [KIND_CANCELLED, KIND_AWAITING])
        self.assertEqual(outcomes[0].reason_code, DROPPED_REPLACED)
        # D-A（2026-10-14 真机）：换目标时旧计划仍作废，但**不再连播"已取消"**（省 ~3.5 s）；
        # 控制台/审计照旧能看到"已取消"。
        self.assertIsNone(outcomes[0].spoken_text)
        self.assertEqual(outcomes[0].display_text, DENIED_TEXT)
        self.assertIn("请说「确认」执行", outcomes[1].spoken_text or "")
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertEqual(len(dispatcher.prepare_calls), 2)
        self.assertIsNotNone(turn.pending)

    def test_expire_after_the_window_cancels_with_timeout_reason(self) -> None:
        clock = _Clock()
        turn, router, dispatcher, _ = self._armed_turn(clock)
        clock.now += 21.0
        outcome = turn.expire()
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.kind, KIND_CANCELLED)
        self.assertEqual(outcome.reason_code, DROPPED_TIMEOUT)
        # 2026-10-23：超时用专用文案，和"换目标/按键取消"的 DENIED_TEXT 区分开。
        self.assertEqual(outcome.spoken_text, TIMEOUT_TEXT)
        self.assertEqual(outcome.display_text, TIMEOUT_TEXT)
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertIsNone(turn.pending)
        self.assertIsNone(router.pending)

    def test_expire_within_the_window_does_nothing(self) -> None:
        clock = _Clock()
        turn, _router, dispatcher, _ = self._armed_turn(clock)
        clock.now += 14.0
        self.assertIsNone(turn.expire())
        self.assertIsNotNone(turn.pending)
        self.assertEqual(dispatcher.cancel_calls, [])

    def test_expire_without_pending_does_nothing(self) -> None:
        clock = _Clock()
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=_FakeDispatcher(), clock=clock)
        self.assertIsNone(turn.expire())


    def test_execution_failure_is_reported_as_denied_not_executed(self) -> None:
        """真机教训：审计追加失败（`audit_append_failed`）时不得记成 executed。"""

        session, factory = build_session(
            lambda plan_text: "确认", factory=_Factory(audit=FakeAudit(fail_on="execution_started"))
        )
        clock = _Clock()
        router = _router(clock)
        turn = VoiceTaskTurn(router=router, dispatcher=_RealDispatcher(session), clock=clock)
        turn.handle(_task_handoff())
        outcome = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(outcome.kind, KIND_DENIED)
        self.assertEqual(outcome.status, "state_uncertain")
        self.assertEqual(outcome.reason, "task_state_uncertain")
        self.assertEqual(outcome.reason_code, "audit_append_failed")
        self.assertIn("状态不确定", outcome.spoken_text or "")
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)


class RealSessionIntegrationTests(unittest.TestCase):
    """接真实派发会话：断言"确认前零执行 / 确认后恰好一次"这类硬事实。"""

    def test_short_plan_line_keeps_the_hint_on_the_console(self) -> None:
        """D-B（2026-10-14 真机）：计划句 22→11 字（省 ~3 s），"怎么说确认"移到控制台文本。"""

        session, _factory = build_session(lambda plan_text: "确认")
        clock = _Clock()
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=_RealDispatcher(session), clock=clock)
        awaiting = turn.handle(_task_handoff())[0]
        self.assertEqual(awaiting.spoken_text, "要打开计算器，说确认。")
        self.assertIn("请说「确认」执行", awaiting.console_text or "")
        self.assertIn("按 Esc 取消", awaiting.console_text or "")

    def test_confirm_hint_no_longer_promises_that_other_answers_cancel(self) -> None:
        """旧提示"其它任意回答将取消"与实现不符（含糊回答只重问一次、不作废计划，真机 turn 12）。"""

        self.assertIn("请说「确认」执行", CONFIRM_HINT)
        self.assertNotIn("其它任意回答将取消", CONFIRM_HINT)

    def test_awaiting_does_not_execute_then_confirmation_executes_once(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        clock = _Clock()
        router = _router(clock)
        turn = VoiceTaskTurn(router=router, dispatcher=_RealDispatcher(session), clock=clock)

        awaiting = turn.handle(_task_handoff())[0]
        self.assertEqual(awaiting.kind, KIND_AWAITING)
        self.assertEqual(factory.backends[0].calls, 0)           # 冻结≠执行（真实证据）
        self.assertTrue((awaiting.plan_hash or "").startswith("sha256:"))
        # #14：语音只念短句；工具名/L2 仍在控制台文本里
        self.assertEqual(awaiting.spoken_text, "要打开计算器，说确认。")
        self.assertIn("风险等级 L2", awaiting.console_text or "")

        executed = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(executed.kind, KIND_EXECUTED)
        self.assertEqual(factory.backends[0].calls, 1)
        self.assertEqual(executed.spoken_text, "帮你打开计算器了。")
        self.assertIn("共 1 个动作", executed.console_text or "")
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)

    def test_vague_answer_keeps_the_plan_until_the_window_expires(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        clock = _Clock()
        router = _router(clock)
        turn = VoiceTaskTurn(router=router, dispatcher=_RealDispatcher(session), clock=clock)
        turn.handle(_task_handoff())
        router.route(RouteInput(raw_text="嗯", trace=_trace("嗯")))
        outcomes = turn.handle(_chat_handoff("嗯"))
        self.assertEqual([item.kind for item in outcomes], [KIND_AWAITING])
        self.assertEqual(factory.backends[0].calls, 0)                    # 仍未执行
        self.assertIsNot(factory.orchestrators[0].state, TaskFlowState.TERMINAL)  # 计划仍在
        # 窗口内说「确认」⇒ 恰好执行一次
        confirmed = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(confirmed.kind, KIND_EXECUTED)
        self.assertEqual(factory.backends[0].calls, 1)
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)

    def test_expiry_cancels_without_any_execution(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        clock = _Clock()
        router = _router(clock)
        turn = VoiceTaskTurn(router=router, dispatcher=_RealDispatcher(session), clock=clock)
        turn.handle(_task_handoff())
        clock.now += 21.0
        outcome = turn.expire()
        self.assertIsNotNone(outcome)
        self.assertEqual(factory.backends[0].calls, 0)
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)
        self.assertIsNone(router.pending)


class ContractTests(unittest.TestCase):
    def test_constructor_validation(self) -> None:
        router = _router(_Clock())
        with self.assertRaises(TypeError):
            VoiceTaskTurn(router=object(), dispatcher=_FakeDispatcher())
        with self.assertRaises(TypeError):
            VoiceTaskTurn(router=router, dispatcher=object())
        with self.assertRaises(TypeError):
            VoiceTaskTurn(router=router, dispatcher=_FakeDispatcher(), clock="不是时钟")
        with self.assertRaises(ValueError):
            VoiceTaskTurn(router=router, dispatcher=_FakeDispatcher(), hint="   ")


class KeyboardConfirmTests(unittest.TestCase):
    """#16：键盘确认通道（Enter/空格=确认、Esc=取消），与语音确认走**同一条** execute 链路。"""

    def _armed_turn(self, clock: _Clock | None = None):
        clock = clock or _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        turn.handle(_task_handoff())
        return turn, router, dispatcher, clock

    def test_keyboard_confirm_executes_with_a_local_answer(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        outcome = turn.confirm_by_keyboard()
        assert outcome is not None
        self.assertEqual(outcome.kind, KIND_EXECUTED)
        self.assertEqual(outcome.confirm_source, CONFIRM_SOURCE_KEYBOARD)
        self.assertEqual(outcome.to_details()["task_confirm_source"], CONFIRM_SOURCE_KEYBOARD)
        # 答案来自**本地按键事件**，不是 ASR 原文。
        self.assertEqual(len(dispatcher.execute_calls), 1)
        self.assertEqual(dispatcher.execute_calls[0][1], CONFIRM_KEY_ANSWER)
        self.assertIsNone(turn.pending)
        self.assertIsNone(router.pending)  # 待确认状态被清掉（事后说「确认」不会重复执行）
        self.assertIsNone(turn.confirm_by_keyboard())  # 一次性消费

    def test_keyboard_cancel_cancels_and_clears_pending(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        outcome = turn.cancel_by_keyboard()
        assert outcome is not None
        self.assertEqual(outcome.kind, KIND_CANCELLED)
        self.assertEqual(outcome.reason_code, REASON_KEY_CANCEL)
        self.assertEqual(outcome.spoken_text, DENIED_TEXT)
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertEqual(len(dispatcher.execute_calls), 0)  # 取消绝不执行
        self.assertIsNone(router.pending)

    def test_keyboard_without_a_prepared_plan_does_nothing(self) -> None:
        clock = _Clock()
        turn = VoiceTaskTurn(router=_router(clock), dispatcher=_FakeDispatcher(), clock=clock)
        self.assertIsNone(turn.confirm_by_keyboard())
        self.assertIsNone(turn.cancel_by_keyboard())

    def test_asr_confirmation_records_its_own_source(self) -> None:
        turn, _, _, _ = self._armed_turn()
        outcome = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(outcome.confirm_source, CONFIRM_SOURCE_ASR)


class VoiceLinesTests(unittest.TestCase):
    """#16：台词出口可注入（拟人化时只换这一处），状态机与安全链不动。"""

    def test_injected_lines_drive_the_reprompt(self) -> None:
        lines = VoiceLines(reprompt="再说一遍「确认」就好。")
        clock = _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(
            router=router, dispatcher=dispatcher, clock=clock, voice_lines=lines
        )
        turn.handle(_task_handoff())
        router.route(RouteInput(raw_text="嗯", trace=_trace("嗯")))
        outcome = turn.handle(_chat_handoff("嗯"))[0]
        self.assertEqual(outcome.spoken_text, "再说一遍「确认」就好。")

    def test_rejects_a_non_voice_lines_object(self) -> None:
        with self.assertRaises(TypeError):
            VoiceTaskTurn(
                router=_router(_Clock()), dispatcher=_FakeDispatcher(), voice_lines="不是台词"
            )


class SameTargetRepeatTests(unittest.TestCase):
    """真机缺陷（2026-10-13，#8）：确认轮里 ASR 又把整句任务句识别一遍。

    旧行为 = 作废计划 + 播「已取消」+ 重新出计划 ⇒ 死循环；新行为 = 保持待确认 + 最多重问一次。
    """

    def _armed_turn(self, text: str = "打开计算器", clock: _Clock | None = None):
        clock = clock or _Clock()
        router = _router(clock, names=("小饼",))
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        turn.handle(_task_handoff(text))
        return turn, router, dispatcher, clock

    @staticmethod
    def _repeat_handoff(router: PendingConfirmationRouter, text: str = "小饼打开计算器。"):
        handoff = router.route(RouteInput(raw_text=text, trace=_trace(text)))
        assert is_repeat_confirmation(handoff), "本用例必须走重复句分支"
        return handoff

    def test_repeat_keeps_the_plan_and_reprompts_once(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        outcomes = turn.handle(self._repeat_handoff(router))
        self.assertEqual([item.kind for item in outcomes], [KIND_AWAITING])
        self.assertEqual(outcomes[0].reason_code, REASON_REPEAT)
        self.assertEqual(outcomes[0].reason, "task_awaiting_confirmation")
        self.assertIn("还没听到确认", outcomes[0].spoken_text or "")
        self.assertEqual(dispatcher.cancel_calls, [])                  # 计划未被作废
        self.assertEqual(len(dispatcher.prepare_calls), 1)             # 没有重新出计划
        self.assertIsNotNone(turn.pending)
        self.assertIsNotNone(router.pending)

    def test_second_repeat_is_silent_and_still_keeps_the_plan(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        turn.handle(self._repeat_handoff(router))
        second = turn.handle(self._repeat_handoff(router))
        self.assertEqual(second, ())                                   # 不再念叨
        self.assertEqual(dispatcher.cancel_calls, [])
        self.assertEqual(len(dispatcher.prepare_calls), 1)
        self.assertIsNotNone(turn.pending)
        self.assertIsNotNone(router.pending)

    def test_confirmation_after_a_repeat_executes_exactly_once(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        turn.handle(self._repeat_handoff(router))
        outcome = turn.handle(_confirmed_handoff("确认"))[0]
        self.assertEqual(outcome.kind, KIND_EXECUTED)
        self.assertEqual(len(dispatcher.execute_calls), 1)
        self.assertEqual(dispatcher.execute_calls[0][1], "确认")

    def test_different_target_after_a_repeat_cancels_then_prepares(self) -> None:
        turn, router, dispatcher, _ = self._armed_turn()
        turn.handle(self._repeat_handoff(router))
        router.route(RouteInput(raw_text="打开记事本", trace=_trace("打开记事本")))
        outcomes = turn.handle(_task_handoff("打开记事本"))
        self.assertEqual([item.kind for item in outcomes], [KIND_CANCELLED, KIND_AWAITING])
        self.assertEqual(outcomes[0].reason_code, DROPPED_REPLACED)
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertEqual(len(dispatcher.prepare_calls), 2)

    def test_vague_then_repeat_shares_the_single_reprompt_budget(self) -> None:
        turn, router, _, _ = self._armed_turn()
        router.route(RouteInput(raw_text="嗯", trace=_trace("嗯")))
        first = turn.handle(_chat_handoff("嗯"))
        self.assertEqual(first[0].reason_code, REASON_REPROMPT)
        self.assertEqual(turn.handle(self._repeat_handoff(router)), ())  # 预算已用完 ⇒ 静默

    def test_repeat_after_a_long_think_still_confirms_inside_the_rearmed_window(self) -> None:
        clock = _Clock()
        turn, router, dispatcher, clock = self._armed_turn(clock=clock)
        clock.now += 14.0
        self.assertEqual(turn.handle(self._repeat_handoff(router))[0].reason_code, REASON_REPEAT)
        clock.now += 14.0                                              # 距重问 14 s
        self.assertTrue(is_pending_confirmation(router.route(RouteInput(raw_text="确认", trace=_trace("确认")))))
        self.assertEqual(turn.handle(_confirmed_handoff("确认"))[0].kind, KIND_EXECUTED)

    def test_silent_repeat_does_not_extend_the_window(self) -> None:
        clock = _Clock()
        turn, router, _, clock = self._armed_turn(clock=clock)
        turn.handle(self._repeat_handoff(router))                      # 首次 ⇒ 重问 + 重新计时
        clock.now += 14.0
        turn.handle(self._repeat_handoff(router))                      # 第二次 ⇒ 静默、不重新计时
        clock.now += 7.0                                               # 距重问共 21 s（> 20 s）
        outcome = turn.expire()
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.reason_code, DROPPED_TIMEOUT)

    def test_repeat_without_a_prepared_plan_does_nothing(self) -> None:
        """路由有待确认、但会话没冻结计划（状态不一致）时：不猜、不 arm、不播报。"""

        clock = _Clock()
        router = _router(clock, names=("小饼",))
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        router.arm(router.route(RouteInput(raw_text="打开计算器", trace=_trace("打开计算器"))))
        self.assertEqual(turn.handle(self._repeat_handoff(router)), ())
        self.assertEqual(dispatcher.prepare_calls, [])


def _unsupported_handoff(text: str = "关闭计算器") -> RouteHandoff:
    handoff = LocalTaskRouter().route(RouteInput(raw_text=text, trace=_trace(text)))
    assert handoff is not None and handoff.route == "unsupported"
    return handoff


class UnsupportedRouteTests(unittest.TestCase):
    """P10 · T1 #12：本批不支持的任务句 ⇒ 如实播报，且**绝不准备计划/绝不执行**。"""

    def test_unsupported_route_is_announced_without_preparing(self) -> None:
        clock = _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        outcomes = turn.handle(_unsupported_handoff())
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(outcome.kind, KIND_UNSUPPORTED)
        self.assertEqual(outcome.status, "unsupported")
        self.assertEqual(outcome.reason, "task_unsupported")
        self.assertEqual(outcome.reason_code, "unimplemented_capability")
        self.assertEqual(outcome.spoken_text, "这条任务本批暂不支持，没有执行。")
        self.assertIn("close_application", outcome.display_text or "")
        self.assertEqual(dispatcher.prepare_calls, [])
        self.assertEqual(dispatcher.execute_calls, [])
        self.assertEqual(dispatcher.cancel_calls, [])
        self.assertIsNone(router.pending)
        self.assertIsNone(turn.pending)

    def test_voice_lines_override_the_spoken_unsupported_line(self) -> None:
        clock = _Clock()
        turn = VoiceTaskTurn(
            router=_router(clock),
            dispatcher=_FakeDispatcher(),
            clock=clock,
            voice_lines=VoiceLines(unsupported="这项暂时不行。"),
        )
        outcome = turn.handle(_unsupported_handoff("删除文件"))[0]
        self.assertEqual(outcome.kind, KIND_UNSUPPORTED)
        self.assertEqual(outcome.spoken_text, "这项暂时不行。")
        self.assertIn("delete_files", outcome.display_text or "")

    def test_unsupported_while_pending_cancels_the_old_plan_first(self) -> None:
        clock = _Clock()
        router = _router(clock)
        dispatcher = _FakeDispatcher()
        turn = VoiceTaskTurn(router=router, dispatcher=dispatcher, clock=clock)
        first = turn.handle(router.route(RouteInput(raw_text="打开计算器", trace=_trace("打开计算器"))))
        self.assertEqual([item.kind for item in first], [KIND_AWAITING])
        self.assertIsNotNone(router.pending)

        text = "关闭计算器"
        outcomes = turn.handle(router.route(RouteInput(raw_text=text, trace=_trace(text))))
        self.assertEqual([item.kind for item in outcomes], [KIND_CANCELLED, KIND_UNSUPPORTED])
        self.assertEqual(
            [item.reason_code for item in outcomes],
            [DROPPED_REPLACED, "unimplemented_capability"],
        )
        # D-A 同口径：不支持句自己会播报 ⇒ "已取消"只留控制台，不占语音时间。
        self.assertIsNone(outcomes[0].spoken_text)
        self.assertEqual(outcomes[0].display_text, DENIED_TEXT)
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertEqual(dispatcher.execute_calls, [])
        self.assertIsNone(router.pending)
        self.assertIsNone(turn.pending)


if __name__ == "__main__":
    unittest.main()
