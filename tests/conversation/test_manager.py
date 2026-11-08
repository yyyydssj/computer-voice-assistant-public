from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from app.contracts import RouteHandoff, RouterStatus, RouteTrace
from app.routing import RouteInput


from app.agent import AgentFailure, AgentFailureCategory, AgentTurnOutcome
from app.conversation import (
    AgentStreamChunk,
    ConversationError,
    ConversationManager,
    ConversationStatus,
)


UTC = timezone.utc


@dataclass
class FakeAdapter:
    runtime_created: bool = False

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.close_calls = 0

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.runtime_created = True
        self.calls.append((request_id, session_id, text))
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"回复：{text}",
            finish_reason="completed",
        )

    def close(self) -> None:
        self.close_calls += 1


class ConversationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        self.adapter = FakeAdapter()
        self.manager = ConversationManager(self.adapter, now=lambda: self.now)

    def test_routed_text_creates_trace_and_calls_router_before_chat_agent(self) -> None:
        class Router:
            def __init__(self) -> None:
                self.requests = []

            def route(self, request: RouteInput) -> RouteHandoff:
                self.requests.append(request)
                return RouteHandoff(
                    trace=request.trace,
                    route="chat",
                    intent="general_chat",
                    handling_mode="chat_reply",
                    confidence=0.95,
                    slots={},
                    required_confirmation="none",
                    tool_scope=(),
                    router=RouterStatus(
                        provider="stub", model_id="router", model_version="1",
                        status="success", failure_code=None,
                    ),
                )

        router = Router()
        manager = ConversationManager(self.adapter, now=lambda: self.now, router=router)

        routed = manager.submit_text("你好", request_id="request-route", turn_id="turn-route")

        self.assertEqual(routed.handoff.route, "chat")
        self.assertIsNotNone(routed.outcome)
        self.assertEqual(len(router.requests), 1)
        trace = router.requests[0].trace
        self.assertEqual((trace.request_id, trace.conversation_id, trace.turn_id, trace.source, trace.raw_text), ("request-route", manager.chat.session_id, "turn-route", "text_shell", "你好"))

    def test_same_turn_id_is_idempotent_and_cannot_be_replaced(self) -> None:
        class Router:
            def route(self, request: RouteInput) -> RouteHandoff:
                return RouteHandoff(
                    trace=request.trace, route="ambiguous", intent="low_confidence",
                    handling_mode="clarify", confidence=0.2, slots={},
                    required_confirmation="must_clarify", tool_scope=(),
                    router=RouterStatus(provider="stub", model_id="router", model_version="1",
                                        status="low_confidence", failure_code="low_confidence"),
                )

        manager = ConversationManager(self.adapter, now=lambda: self.now, router=Router())
        first = manager.submit_text("不确定", request_id="request-1", turn_id="turn-1")
        second = manager.submit_text("不确定", request_id="request-1", turn_id="turn-1")
        self.assertIs(first, second)
        self.assertIsNone(first.outcome)
        with self.assertRaises(ConversationError):
            manager.submit_text("换一句", request_id="request-1", turn_id="turn-1")

    def test_asr_final_uses_same_route_contract_and_turn_id_boundary(self) -> None:
        class Router:
            def route(self, request: RouteInput) -> RouteHandoff:
                return RouteHandoff(
                    trace=request.trace, route="ambiguous", intent="router_unavailable",
                    handling_mode="clarify", confidence=0.0, slots={},
                    required_confirmation="must_clarify", tool_scope=(),
                    router=RouterStatus(provider="stub", model_id="router", model_version="1",
                                        status="unavailable", failure_code="router_unavailable"),
                )

        manager = ConversationManager(self.adapter, now=lambda: self.now, router=Router())
        result = manager.submit_asr_final("打开计算器", turn_id="utterance-1", request_id="request-asr")
        self.assertEqual(result.handoff.trace.source, "asr")
        self.assertEqual(result.handoff.trace.turn_id, "utterance-1")
        self.assertIsNone(result.outcome)
        self.assertEqual(self.adapter.calls, [])

    def test_creating_containers_does_not_create_runtime(self) -> None:
        chat = self.manager.get_or_create_chat()
        work = self.manager.get_or_create_work("task-1")

        self.assertFalse(self.adapter.runtime_created)
        self.assertEqual(chat.kind, "chat")
        self.assertEqual(work.kind, "work")
        self.assertNotEqual(chat.session_id, work.session_id)

    def test_first_send_uses_lazy_adapter_and_keeps_contexts_isolated(self) -> None:
        chat = self.manager.get_or_create_chat()
        work = self.manager.get_or_create_work("task-1")

        chat_outcome = self.manager.send_chat(request_id="request-chat-1", text="闲聊")
        work_outcome = self.manager.send_work(
            task_id="task-1", request_id="request-work-1", text="工作占位"
        )

        self.assertTrue(self.adapter.runtime_created)
        self.assertEqual(chat_outcome.session_id, chat.session_id)
        self.assertEqual(work_outcome.session_id, work.session_id)
        self.assertEqual(
            [call[1] for call in self.adapter.calls],
            [chat.session_id, work.session_id],
        )

    def test_same_task_reuses_work_container_but_different_tasks_do_not(self) -> None:
        first = self.manager.get_or_create_work("task-1")
        same = self.manager.get_or_create_work("task-1")
        other = self.manager.get_or_create_work("task-2")

        self.assertIs(first, same)
        self.assertNotEqual(first.session_id, other.session_id)

    def test_chat_is_not_destroyed_before_thirty_minutes(self) -> None:
        chat = self.manager.get_or_create_chat()
        self.now += timedelta(minutes=29, seconds=59)

        expired = self.manager.expire_idle_chat()

        self.assertFalse(expired)
        self.assertIs(self.manager.get_or_create_chat(), chat)

    def test_chat_is_destroyed_at_thirty_minutes_and_next_request_gets_new_session(self) -> None:
        old_chat = self.manager.get_or_create_chat()
        self.now += timedelta(minutes=30)

        expired = self.manager.expire_idle_chat()
        new_chat = self.manager.get_or_create_chat()

        self.assertTrue(expired)
        self.assertNotEqual(old_chat.session_id, new_chat.session_id)
        self.assertEqual(old_chat.status, ConversationStatus.EXPIRED)
        self.assertEqual(old_chat.close_reason, "idle_timeout")

    def test_next_chat_access_automatically_replaces_idle_session(self) -> None:
        old_chat = self.manager.get_or_create_chat()
        self.now += timedelta(minutes=30)

        new_chat = self.manager.get_or_create_chat()

        self.assertEqual(old_chat.status, ConversationStatus.EXPIRED)
        self.assertEqual(old_chat.close_reason, "idle_timeout")
        self.assertNotEqual(old_chat.session_id, new_chat.session_id)

    def test_next_chat_send_automatically_uses_a_new_session_after_idle_timeout(self) -> None:
        old_chat = self.manager.get_or_create_chat()
        self.now += timedelta(minutes=30)

        outcome = self.manager.send_chat(request_id="request-after-idle", text="回来继续聊")

        self.assertEqual(old_chat.status, ConversationStatus.EXPIRED)
        self.assertEqual(old_chat.close_reason, "idle_timeout")
        self.assertNotEqual(outcome.session_id, old_chat.session_id)
        self.assertEqual(self.adapter.calls, [("request-after-idle", outcome.session_id, "回来继续聊")])

    def test_chat_activity_refreshes_idle_deadline(self) -> None:
        chat = self.manager.get_or_create_chat()
        self.now += timedelta(minutes=29)
        self.manager.send_chat(request_id="request-1", text="继续聊")
        self.now += timedelta(minutes=29, seconds=59)

        self.assertFalse(self.manager.expire_idle_chat())
        self.assertEqual(chat.status, ConversationStatus.OPEN)

    def test_work_closes_after_completion_cancellation_or_abort(self) -> None:
        cases = (
            ("complete_work", "completed"),
            ("cancel_work", "cancelled"),
            ("abort_work", "aborted"),
        )
        for index, (close_method, expected_reason) in enumerate(cases, start=1):
            with self.subTest(close_method=close_method):
                manager = ConversationManager(FakeAdapter(), now=lambda: self.now)
                work = manager.get_or_create_work(f"task-{index}")
                getattr(manager, close_method)(f"task-{index}")

                self.assertEqual(work.status, ConversationStatus.CLOSED)
                self.assertEqual(work.close_reason, expected_reason)
                with self.assertRaisesRegex(ConversationError, "已关闭"):
                    manager.send_work(
                        task_id=f"task-{index}",
                        request_id=f"request-after-{index}",
                        text="不能继续",
                    )

    def test_failed_work_turn_closes_task_without_retry(self) -> None:
        class FailedAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return AgentTurnOutcome(
                    request_id=request_id,
                    session_id=session_id,
                    completed=False,
                    finish_reason="error",
                )

        adapter = FailedAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)
        work = manager.get_or_create_work("task-failed")

        outcome = manager.send_work(
            task_id="task-failed",
            request_id="request-work-failed",
            text="工作失败测试",
        )

        self.assertFalse(outcome.completed)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(work.status, ConversationStatus.CLOSED)
        self.assertEqual(work.close_reason, "agent_failure")

    def test_closed_work_task_cannot_be_recreated_or_reused(self) -> None:
        work = self.manager.get_or_create_work("task-1")
        self.manager.complete_work("task-1")

        with self.assertRaisesRegex(ConversationError, "已关闭"):
            self.manager.get_or_create_work("task-1")
        self.assertEqual(work.status, ConversationStatus.CLOSED)

    def test_failed_adapter_outcome_does_not_retry_or_create_new_request(self) -> None:
        class FailedAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return AgentTurnOutcome(
                    request_id=request_id,
                    session_id=session_id,
                    completed=False,
                    finish_reason="error",
                )

        adapter = FailedAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)
        outcome = manager.send_chat(request_id="request-failed", text="失败测试")

        self.assertFalse(outcome.completed)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0][0], "request-failed")

    def test_request_id_is_generated_and_complete_turn_enters_history(self) -> None:
        outcome = self.manager.send_chat(text="自动请求编号")

        self.assertTrue(outcome.completed)
        self.assertTrue(outcome.request_id.startswith("chat-request-"))
        self.assertEqual(outcome.request_id, self.adapter.calls[0][0])
        self.assertEqual(len(self.manager.chat.history), 1)
        entry = self.manager.chat.history[0]
        self.assertEqual(entry.request_id, outcome.request_id)
        self.assertEqual(entry.user_text, "自动请求编号")
        self.assertEqual(entry.assistant_text, "回复：自动请求编号")

    def test_stream_chunks_are_concatenated_before_history_commit(self) -> None:
        class StreamingAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str):
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return iter(
                    (
                        AgentStreamChunk(text="第一段"),
                        AgentStreamChunk(text="第二段"),
                        AgentStreamChunk(text="", finish_reason="completed"),
                    )
                )

        adapter = StreamingAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)

        outcome = manager.send_chat(text="流式测试")

        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.text, "第一段第二段")
        self.assertEqual(outcome.finish_reason, "completed")
        self.assertEqual(manager.chat.history[0].assistant_text, "第一段第二段")

    def test_incomplete_stream_does_not_enter_formal_history(self) -> None:
        class InterruptedAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str):
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return iter((AgentStreamChunk(text="已收到但未结束"),))

        adapter = InterruptedAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)

        outcome = manager.send_chat(text="中断测试")

        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.text, "已收到但未结束")
        self.assertIsNone(outcome.finish_reason)
        self.assertEqual(manager.chat.history, [])

    def test_empty_completed_stream_does_not_enter_formal_history(self) -> None:
        class EmptyAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str):
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return iter((AgentStreamChunk(text="", finish_reason="completed"),))

        adapter = EmptyAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)

        outcome = manager.send_chat(text="空响应测试")

        self.assertFalse(outcome.completed)
        self.assertEqual(manager.chat.history, [])
    def test_stream_failure_keeps_partial_text_out_of_history_and_preserves_category(self) -> None:
        class FailedStreamAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str):
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return iter(
                    (
                        AgentStreamChunk(text="未完成片段"),
                        AgentStreamChunk(
                            failure=AgentFailure(
                                category=AgentFailureCategory.RATE_LIMITED,
                                stage="request",
                                diagnostic_id="agent-rate-limit-test",
                            )
                        ),
                    )
                )

        adapter = FailedStreamAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)

        outcome = manager.send_chat(text="限流故障测试")

        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.text, "未完成片段")
        self.assertIsNotNone(outcome.failure)
        self.assertEqual(outcome.failure.category, AgentFailureCategory.RATE_LIMITED)
        self.assertEqual(manager.chat.history, [])
        self.assertEqual(len(adapter.calls), 1)

    def test_invalid_completed_outcome_identity_is_schema_error(self) -> None:
        class WrongIdentityAdapter(FakeAdapter):
            def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
                self.runtime_created = True
                self.calls.append((request_id, session_id, text))
                return AgentTurnOutcome(
                    request_id="different-request",
                    session_id=session_id,
                    completed=True,
                    text="不应采纳",
                    finish_reason="completed",
                )

        adapter = WrongIdentityAdapter()
        manager = ConversationManager(adapter, now=lambda: self.now)

        outcome = manager.send_chat(request_id="expected-request", text="身份错配测试")

        self.assertFalse(outcome.completed)
        self.assertIsNotNone(outcome.failure)
        self.assertEqual(outcome.failure.category, AgentFailureCategory.SCHEMA_ERROR)
        self.assertEqual(manager.chat.history, [])
    def test_close_is_idempotent_and_closes_adapter_once(self) -> None:
        self.manager.get_or_create_chat()
        self.manager.get_or_create_work("task-1")

        self.manager.close()
        self.manager.close()

        self.assertEqual(self.adapter.close_calls, 1)
        with self.assertRaisesRegex(ConversationError, "管理器已关闭"):
            self.manager.get_or_create_chat()


if __name__ == "__main__":
    unittest.main()
