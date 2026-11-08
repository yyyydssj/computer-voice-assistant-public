"""P10 · T1 批次 3 红测：待确认路由（语音"独立确认轮"的路由层状态）。"""

from __future__ import annotations

import unittest

from app.contracts.base import ContractError
from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.routing import RouteInput
from app.runtime.orchestration.local_task_router import LocalTaskRouter
from app.runtime.orchestration.pending_confirmation import (
    DROPPED_CANCELLED,
    DROPPED_REPLACED,
    DROPPED_TIMEOUT,
    PENDING_CONFIRMATION_PROVIDER,
    PENDING_CONFIRMATION_REPEAT_PROVIDER,
    PendingConfirmationRouter,
    is_pending_confirmation,
    is_repeat_confirmation,
)

CREATED_AT = "2026-10-13T00:00:00Z"


def _request(text: str, source: str = "asr") -> RouteInput:
    return RouteInput(
        raw_text=text,
        trace=RouteTrace(
            request_id="req-1",
            conversation_id="conv-1",
            turn_id="turn-1",
            source=source,
            raw_text=text,
            created_at=CREATED_AT,
        ),
    )


class _StubChatRouter:
    def __init__(self) -> None:
        self.calls = 0

    def route(self, request: RouteInput) -> RouteHandoff:
        self.calls += 1
        return RouteHandoff(
            trace=request.trace,
            route="chat",
            intent="general_chat",
            handling_mode="chat_reply",
            confidence=1.0,
            slots={},
            required_confirmation="none",
            tool_scope=(),
            router=RouterStatus(
                provider="stub", model_id="stub-router", model_version="1", status="success",
                failure_code=None,
            ),
        )


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _router(clock: _Clock | None = None, window: float = 15.0, names: tuple[str, ...] = ()):
    clock = clock or _Clock()
    fallback = _StubChatRouter()
    router = PendingConfirmationRouter(
        first=LocalTaskRouter(names=names), fallback=fallback, window_seconds=window, clock=clock
    )
    return router, fallback, clock


def _task_handoff(text: str = "打开计算器") -> RouteHandoff:
    handoff = LocalTaskRouter().route(_request(text))
    assert handoff is not None and handoff.route == "task"
    return handoff


class WithoutPendingTests(unittest.TestCase):
    def test_behaves_like_task_first_router(self) -> None:
        router, fallback, _ = _router()
        task = router.route(_request("打开计算器"))
        self.assertEqual(task.route, "task")
        self.assertEqual(fallback.calls, 0)
        chat = router.route(_request("你好"))
        self.assertEqual(chat.route, "chat")
        self.assertEqual(fallback.calls, 1)
        self.assertIs(router.last_handoff, chat)


class ConfirmTests(unittest.TestCase):
    def test_confirm_rebinds_the_same_task_with_a_marker(self) -> None:
        router, fallback, _ = _router()
        pending = _task_handoff()
        router.arm(pending)
        handoff = router.route(_request("确认"))
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.intent, pending.intent)
        self.assertEqual(handoff.tool_scope, pending.tool_scope)
        self.assertEqual(handoff.slots, pending.slots)
        self.assertEqual(handoff.required_confirmation, "defer_to_safety")
        self.assertEqual(handoff.router.provider, PENDING_CONFIRMATION_PROVIDER)
        self.assertTrue(is_pending_confirmation(handoff))
        self.assertEqual(handoff.trace.raw_text, "确认")
        self.assertEqual(fallback.calls, 0)  # 确认不经过 fallback（不触发聊天/Provider）

    def test_confirm_is_one_shot(self) -> None:
        router, fallback, _ = _router()
        router.arm(_task_handoff())
        router.route(_request("确认"))
        second = router.route(_request("确认"))
        self.assertEqual(second.route, "chat")
        self.assertEqual(fallback.calls, 1)

    def test_vague_answers_do_not_confirm_and_do_not_drop_the_plan(self) -> None:
        for text in ("请确认", "好的", "嗯", "今天天气怎么样"):
            with self.subTest(text=text):
                router, fallback, _ = _router()
                router.arm(_task_handoff())
                handoff = router.route(_request(text))
                self.assertEqual(handoff.route, "chat")
                self.assertIsNotNone(router.pending)      # 真机教训：含糊回答不得作废计划
                self.assertTrue(router.take_vague())
                self.assertFalse(router.take_vague())     # 只报一次
                self.assertIsNone(router.take_dropped())

    def test_vague_answer_then_confirmation_within_the_window_still_confirms(self) -> None:
        clock = _Clock()
        router, _, _ = _router(clock=clock)
        router.arm(_task_handoff())
        router.route(_request("嗯"))
        clock.now += 5.0
        self.assertTrue(is_pending_confirmation(router.route(_request("确认执行"))))

    def test_decorated_confirmation_still_confirms(self) -> None:
        for text in ("确认", " 确认 ", "确认。", "确认执行", "确认一下"):
            with self.subTest(text=text):
                router, _, _ = _router()
                router.arm(_task_handoff())
                self.assertTrue(is_pending_confirmation(router.route(_request(text))))


class DropTests(unittest.TestCase):
    def test_cancel_words_drop_pending_immediately(self) -> None:
        for text in ("取消", "算了", "不用了", "停止", "别打开"):
            with self.subTest(text=text):
                router, _, _ = _router()
                router.arm(_task_handoff())
                handoff = router.route(_request(text))
                self.assertEqual(handoff.route, "chat")
                self.assertEqual(router.take_dropped(), DROPPED_CANCELLED)
                self.assertIsNone(router.pending)

    def test_new_task_sentence_replaces_pending_and_routes_as_new_task(self) -> None:
        router, _, _ = _router()
        router.arm(_task_handoff("打开计算器"))
        handoff = router.route(_request("打开记事本"))
        self.assertEqual(handoff.route, "task")
        self.assertFalse(is_pending_confirmation(handoff))
        self.assertEqual(handoff.slots["application_name"].value, "记事本")
        self.assertEqual(router.take_dropped(), DROPPED_REPLACED)

    def test_unsupported_task_sentence_replaces_pending_instead_of_being_vague(self) -> None:
        """P10 · T1 #12：待确认期间说「关闭计算器」不是含糊回答 —— 旧计划作废，如实拒绝下传。"""

        for text in ("关闭计算器", "删除文件"):
            with self.subTest(text=text):
                router, fallback, _ = _router()
                router.arm(_task_handoff("打开计算器"))
                handoff = router.route(_request(text))
                self.assertEqual(handoff.route, "unsupported")
                self.assertEqual(handoff.intent, "unimplemented_capability")
                self.assertIs(router.last_handoff, handoff)
                self.assertEqual(router.take_dropped(), DROPPED_REPLACED)
                self.assertIsNone(router.pending)
                self.assertFalse(router.take_vague())  # 不再当"含糊回答"
                self.assertEqual(fallback.calls, 0)    # 不触发聊天/Provider

    def test_timeout_drops_pending(self) -> None:
        clock = _Clock()
        router, fallback, _ = _router(clock=clock)
        router.arm(_task_handoff())
        clock.now += 21.0
        handoff = router.route(_request("确认"))
        self.assertEqual(handoff.route, "chat")  # 超时后「确认」不再是确认
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(router.take_dropped(), DROPPED_TIMEOUT)

    def test_default_window_is_twenty_seconds(self) -> None:
        """#15：窗口默认 20 s（并且由会话在**播报结束**后重新计时）。"""

        from app.runtime.orchestration.pending_confirmation import (
            DEFAULT_CONFIRM_WINDOW_SECONDS,
        )

        self.assertEqual(DEFAULT_CONFIRM_WINDOW_SECONDS, 20.0)

        inside = _Clock()
        router = PendingConfirmationRouter(
            first=LocalTaskRouter(), fallback=_StubChatRouter(), clock=inside
        )
        router.arm(_task_handoff())
        inside.now += 19.9
        self.assertTrue(is_pending_confirmation(router.route(_request("确认"))))

        outside = _Clock()
        router2 = PendingConfirmationRouter(
            first=LocalTaskRouter(), fallback=_StubChatRouter(), clock=outside
        )
        router2.arm(_task_handoff())
        outside.now += 20.1
        router2.route(_request("确认"))
        self.assertEqual(router2.take_dropped(), DROPPED_TIMEOUT)

    def test_confirmation_inside_the_window_is_accepted(self) -> None:
        clock = _Clock()
        router, _, _ = _router(clock=clock)
        router.arm(_task_handoff())
        clock.now += 14.9
        self.assertTrue(is_pending_confirmation(router.route(_request("确认"))))

    def test_clear_and_arm_reset_dropped(self) -> None:
        router, _, _ = _router()
        router.arm(_task_handoff())
        router.clear()
        self.assertIsNone(router.pending)
        router.arm(_task_handoff())
        self.assertIsNone(router.take_dropped())
        self.assertIsNotNone(router.pending)


class ArmContractTests(unittest.TestCase):
    def test_rejects_non_task_handoffs(self) -> None:
        router, _, _ = _router()
        with self.assertRaises(TypeError):
            router.arm("不是交接")  # type: ignore[arg-type]
        chat = _StubChatRouter().route(_request("你好"))
        with self.assertRaises(ValueError):
            router.arm(chat)

    def test_rejects_unsupported_intent(self) -> None:
        router, _, _ = _router()
        task = _task_handoff()
        file_intent = RouteHandoff(
            trace=task.trace,
            route="task",
            intent="file_operation",
            handling_mode=task.handling_mode,
            confidence=task.confidence,
            slots=dict(task.slots),
            required_confirmation=task.required_confirmation,
            tool_scope=("create_text_file",),
            router=task.router,
        )
        with self.assertRaises(ValueError):
            router.arm(file_intent)

    def test_contract_already_forbids_task_without_local_confirmation(self) -> None:
        """第二道防线（required_confirmation != defer_to_safety）在契约层就已不可达 —— 如实断言。"""

        task = _task_handoff()
        with self.assertRaises(ContractError):
            RouteHandoff(
                trace=task.trace,
                route="task",
                intent=task.intent,
                handling_mode=task.handling_mode,
                confidence=task.confidence,
                slots=dict(task.slots),
                required_confirmation="none",
                tool_scope=task.tool_scope,
                router=task.router,
            )

    def test_constructor_validation(self) -> None:
        with self.assertRaises(TypeError):
            PendingConfirmationRouter(first="没有 route", fallback=_StubChatRouter())
        with self.assertRaises(TypeError):
            PendingConfirmationRouter(first=LocalTaskRouter(), fallback="没有 route")
        with self.assertRaises(ValueError):
            PendingConfirmationRouter(first=LocalTaskRouter(), fallback=_StubChatRouter(), window_seconds=0)
        with self.assertRaises(TypeError):
            PendingConfirmationRouter(
                first=LocalTaskRouter(), fallback=_StubChatRouter(), clock="不是时钟"
            )


class SameTargetRepeatTests(unittest.TestCase):
    """真机缺陷（2026-10-13）：确认轮里 ASR 又把整句任务句识别一遍 ⇒ 计划被作废 ⇒ 死循环。

    修法借公开实现：AgenticSeek `sources/echo_filter.py`（撞上"自己正在等的那句话"按回声丢弃）、
    docker-talkies `sherpa.py`（同一批文本完全相同即去重）。文本入口（`text_shell`）行为不变。
    """

    def _armed(self, text: str = "打开计算器", clock: _Clock | None = None):
        router, fallback, clock = _router(clock, names=("小饼",))
        router.arm(router.route(_request(text)))
        return router, fallback, clock

    def test_same_target_asr_repeat_keeps_pending_and_marks_repeat(self) -> None:
        router, fallback, _ = self._armed()
        handoff = router.route(_request("小饼打开计算器。"))
        self.assertTrue(is_repeat_confirmation(handoff))
        self.assertFalse(is_pending_confirmation(handoff))
        self.assertEqual(handoff.route, "task")                       # 仍是任务路由：不触发聊天/Provider
        self.assertEqual(handoff.router.provider, PENDING_CONFIRMATION_REPEAT_PROVIDER)
        self.assertEqual(handoff.slots["application_name"].value, "计算器")
        self.assertIsNotNone(router.pending)                          # 计划未被作废
        self.assertIsNone(router.take_dropped())                      # 不是 replaced
        self.assertEqual(fallback.calls, 0)
        self.assertIs(router.last_handoff, handoff)

    def test_repeat_then_confirmation_still_confirms(self) -> None:
        router, _, _ = self._armed()
        router.route(_request("小饼打开计算器。"))
        self.assertTrue(is_pending_confirmation(router.route(_request("确认"))))

    def test_alias_equivalent_target_is_a_repeat(self) -> None:
        router, _, _ = self._armed("打开 calc")
        self.assertTrue(is_repeat_confirmation(router.route(_request("打开计算器"))))

    def test_echo_with_trailing_words_is_a_repeat(self) -> None:
        """真机可能把残留任务句和新话并成一句（如「打开计算器确认」）——连续片段重叠即算重复。"""

        router, _, _ = self._armed("打开计算器")
        self.assertTrue(is_repeat_confirmation(router.route(_request("打开计算器确认"))))

    def test_different_target_still_replaces(self) -> None:
        router, _, _ = self._armed("打开计算器")
        handoff = router.route(_request("打开记事本"))
        self.assertFalse(is_repeat_confirmation(handoff))
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.slots["application_name"].value, "记事本")
        self.assertEqual(router.take_dropped(), DROPPED_REPLACED)
        self.assertIsNone(router.pending)

    def test_two_char_common_prefix_is_not_a_repeat(self) -> None:
        """`REPEAT_OVERLAP_CHARACTERS=3`：只有「打开」2 字公共前缀不算重复，换目标仍按 replaced。"""

        router, _, _ = self._armed("打开计算器")
        self.assertFalse(is_repeat_confirmation(router.route(_request("打开记事本"))))
        self.assertEqual(router.take_dropped(), DROPPED_REPLACED)

    def test_text_source_same_sentence_keeps_the_old_replaced_behaviour(self) -> None:
        router, _, _ = self._armed("打开计算器")
        handoff = router.route(_request("打开计算器", source="text_shell"))
        self.assertFalse(is_repeat_confirmation(handoff))
        self.assertEqual(router.take_dropped(), DROPPED_REPLACED)

    def test_timeout_wins_over_repeat(self) -> None:
        clock = _Clock()
        router, _, _ = self._armed(clock=clock)
        clock.now += 21.0
        handoff = router.route(_request("小饼打开计算器。"))
        self.assertFalse(is_repeat_confirmation(handoff))
        self.assertEqual(router.take_dropped(), DROPPED_TIMEOUT)

    def test_repeat_itself_does_not_extend_the_window(self) -> None:
        """静默忽略的重复句**不**重新计时（避免 ASR 卡死时计划无限期挂着）。"""

        clock = _Clock()
        router, _, _ = self._armed(clock=clock)
        clock.now += 10.0
        router.route(_request("小饼打开计算器。"))
        clock.now += 11.0                                             # 距 arm 共 21 s（> 20 s）
        self.assertFalse(is_pending_confirmation(router.route(_request("确认"))))
        self.assertEqual(router.take_dropped(), DROPPED_TIMEOUT)

    def test_rearm_restarts_the_window(self) -> None:
        clock = _Clock()
        router, _, _ = self._armed(clock=clock)
        clock.now += 10.0
        self.assertIsNotNone(router.rearm())
        clock.now += 10.0                                             # 距 rearm 10 s，仍在窗口内
        self.assertTrue(is_pending_confirmation(router.route(_request("确认"))))

    def test_rearm_without_pending_returns_none(self) -> None:
        router, _, _ = _router()
        self.assertIsNone(router.rearm())


class _UnsupportedProviderFallback:
    """Provider 替身：对任何输入都返回 unsupported/unimplemented_capability（真机缺陷形态）。"""

    def __init__(self) -> None:
        self.calls = 0

    def route(self, request: RouteInput) -> RouteHandoff:
        self.calls += 1
        return RouteHandoff(
            trace=request.trace,
            route="unsupported",
            intent="unimplemented_capability",
            handling_mode="reject",
            confidence=0.95,
            slots={},
            required_confirmation="none",
            tool_scope=(),
            router=RouterStatus(
                provider="deepseek",
                model_id="deepseek-chat",
                model_version="1",
                status="success",
                failure_code=None,
            ),
        )


class PendingQuestionFallbackTests(unittest.TestCase):
    """2026-10-23：待确认路由器也要把 Provider 的"疑问句不支持"回落成聊天。"""

    def _setup(self):
        fallback = _UnsupportedProviderFallback()
        router = PendingConfirmationRouter(
            first=LocalTaskRouter(), fallback=fallback, window_seconds=15.0, clock=_Clock()
        )
        return router, fallback

    def test_unsupported_question_from_provider_is_downgraded(self) -> None:
        router, fallback = self._setup()
        handoff = router.route(_request("你能关闭应用吗？"))
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(handoff.route, "chat")
        self.assertEqual(handoff.router.provider, "question_fallback")

    def test_unsupported_command_from_provider_is_kept(self) -> None:
        # 用本地句式**不命中**的命令句（"关闭屏幕"没有可识别的应用/对象）才会真的问 Provider，
        # 这样验证的是兜底不误伤：Provider 说的 unsupported 原样保留。
        router, fallback = self._setup()
        handoff = router.route(_request("关闭屏幕"))
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(handoff.route, "unsupported")


if __name__ == "__main__":
    unittest.main()
