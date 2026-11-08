"""P10 · T1 回归测试：任务路由组合必须能吃下"没有 fallback"的情况。

真机教训（2026-10-13）：没有 Provider、也没有显式注入 router 时
`select_runtime_router()` 返回 `(None, None)`；把 `None` 直接当 fallback 会让
`TaskFirstRouter` 抛 `TypeError`，任务执行被误报成"白名单或数据目录不可用"并降级为仅对话。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.main import compose_task_router
from app.routing import RouteInput
from app.runtime.orchestration import is_pending_confirmation


def _trace(text: str) -> RouteTrace:
    return RouteTrace(
        request_id="req-1",
        conversation_id="conv-1",
        turn_id="turn-1",
        source="text_shell",
        raw_text=text,
        created_at="2026-10-13T00:00:00Z",
    )


def _request(text: str) -> RouteInput:
    return RouteInput(raw_text=text, trace=_trace(text))


class _StubRouter:
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


class ComposeTaskRouterTests(unittest.TestCase):
    def test_missing_fallback_does_not_raise_and_chats(self) -> None:
        router = compose_task_router(None)  # 真机 bug：这一句曾抛 TypeError
        handoff = router.route(_request("你好"))
        self.assertEqual(handoff.route, "chat")
        self.assertEqual(handoff.intent, "general_chat")

    def test_task_sentence_wins_before_the_fallback(self) -> None:
        handoff = compose_task_router(None).route(_request("请帮我打开计算器。"))
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.intent, "open_application")
        self.assertEqual(handoff.tool_scope, ("start_whitelisted_application",))

    def test_explicit_fallback_is_kept_and_only_used_on_miss(self) -> None:
        stub = _StubRouter()
        router = compose_task_router(stub)
        task_handoff = router.route(_request("打开记事本"))
        self.assertEqual(task_handoff.route, "task")
        self.assertEqual(stub.calls, 0)  # 本地命中 ⇒ 不打扰 fallback
        chat_handoff = router.route(_request("今天天气怎么样"))
        self.assertEqual(chat_handoff.route, "chat")
        self.assertEqual(stub.calls, 1)


class ComposeTaskRouterArmingTests(unittest.TestCase):
    """批次 3：`compose_task_router()` 返回的路由器必须支持语音侧的 arm/待确认。"""

    def test_router_can_be_armed_and_consumes_exactly_one_confirmation(self) -> None:
        router = compose_task_router(None)
        task = router.route(_request("打开计算器"))
        router.arm(task)
        confirmed = router.route(_request("确认"))
        self.assertTrue(is_pending_confirmation(confirmed))
        self.assertEqual(confirmed.route, "task")
        self.assertEqual(confirmed.slots["application_name"].value, "计算器")
        after = router.route(_request("确认"))  # 一次性：第二句「确认」只是普通聊天
        self.assertEqual(after.route, "chat")
        self.assertFalse(is_pending_confirmation(after))

    def test_router_keeps_pending_when_another_answer_arrives(self) -> None:
        """真机教训（2026-10-13）：含糊回答不作废计划（否则用户还没确认完就被取消）。"""

        router = compose_task_router(None)
        router.arm(router.route(_request("打开计算器")))
        other = router.route(_request("今天天气怎么样"))
        self.assertEqual(other.route, "chat")
        self.assertIsNotNone(router.pending)
        self.assertTrue(router.take_vague())
        self.assertIsNone(router.take_dropped())
        # 窗口内说「确认」仍然生效
        self.assertTrue(is_pending_confirmation(router.route(_request("确认执行"))))

    def test_router_cancels_on_an_explicit_cancel_word(self) -> None:
        from app.runtime.orchestration.pending_confirmation import DROPPED_CANCELLED

        router = compose_task_router(None)
        router.arm(router.route(_request("打开计算器")))
        self.assertEqual(router.route(_request("算了")).route, "chat")
        self.assertEqual(router.take_dropped(), DROPPED_CANCELLED)
        self.assertIsNone(router.pending)


class WithTaskExecutionTests(unittest.TestCase):
    def test_merges_both_injection_points_without_mutating_the_original(self) -> None:
        from app.main import with_task_execution
        from app.runtime.auto_voice import AutoVoiceConfig

        config = AutoVoiceConfig()
        merged = with_task_execution(config, task_router="R", task_dispatch="D")
        assert merged is not None
        self.assertIs(merged.task_router, "R")
        self.assertIs(merged.task_dispatch, "D")
        self.assertIsNone(config.task_router)  # 冻结对象不被就地修改

    def test_passthrough_when_a_piece_is_missing(self) -> None:
        from app.main import with_task_execution
        from app.runtime.auto_voice import AutoVoiceConfig

        config = AutoVoiceConfig()
        self.assertIs(with_task_execution(config, task_router="R", task_dispatch=None), config)
        self.assertIs(with_task_execution(config, task_router=None, task_dispatch="D"), config)
        self.assertIsNone(with_task_execution(None, task_router=None, task_dispatch=None))


class TaskNamePrefixesTests(unittest.TestCase):
    def test_default_keyword_is_used_when_no_config(self) -> None:
        from app.main import task_name_prefixes
        from app.runtime.auto_voice import DEFAULT_KEYWORD

        self.assertEqual(task_name_prefixes(None, None), (DEFAULT_KEYWORD,))

    def test_reads_keyword_from_any_config_and_deduplicates(self) -> None:
        from app.main import task_name_prefixes

        self.assertEqual(
            task_name_prefixes(SimpleNamespace(keyword="小爱"), SimpleNamespace(keyword="小饼")),
            ("小爱", "小饼"),
        )
        self.assertEqual(
            task_name_prefixes(SimpleNamespace(keyword="小饼"), SimpleNamespace(keyword="小饼")),
            ("小饼",),
        )
        self.assertEqual(
            task_name_prefixes(SimpleNamespace(keyword="  "), SimpleNamespace(keyword=None)),
            ("小饼",),
        )

    def test_compose_task_router_tolerates_the_wake_name(self) -> None:
        router = compose_task_router(None, names=("小饼",))
        handoff = router.route(_request("小饼打开计算器。"))
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.slots["application_name"].value, "计算器")
        # 不带 names 的旧行为保持不变
        self.assertEqual(compose_task_router(None).route(_request("小饼打开计算器。")).route, "chat")


if __name__ == "__main__":
    unittest.main()
