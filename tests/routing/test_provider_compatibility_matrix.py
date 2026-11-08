"""Offline Provider compatibility matrix and ConversationManager seam tests."""

from __future__ import annotations

import unittest

from app.agent import AgentTurnOutcome
from app.contracts import RouteHandoff, RouteTrace, RouterStatus
from app.conversation import ConversationManager
from app.routing import ProviderIntentRouter, ProviderRequest, RouteInput


class _ConversationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.calls.append((request_id, session_id, text))
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"回复：{text}",
            finish_reason="completed",
        )

    def close(self) -> None:
        pass


class ProviderCompatibilityMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = RouteTrace(
            request_id="request-matrix",
            conversation_id="conversation-matrix",
            turn_id="turn-matrix",
            source="text_shell",
            raw_text="打开计算器",
            created_at="2026-09-25T10:00:00Z",
        )
        self.request = RouteInput(raw_text=self.trace.raw_text, trace=self.trace)

    @staticmethod
    def success_payload(*, route: str, intent: str, handling_mode: str, confidence: float = 0.96,
                        slots: dict[str, object] | None = None,
                        required_confirmation: str = "none",
                        tool_scope: list[str] | None = None) -> dict[str, object]:
        return {
            "status": "success",
            "route": route,
            "intent": intent,
            "handling_mode": handling_mode,
            "confidence": confidence,
            "slots": slots or {},
            "required_confirmation": required_confirmation,
            "tool_scope": tool_scope or [],
        }

    def route(self, payload: object) -> RouteHandoff:
        return ProviderIntentRouter(
            lambda _: payload,
            provider_name="matrix-provider",
            model_id="matrix-model",
            model_version="v1",
        ).route(self.request)

    def test_success_matrix_covers_chat_query_and_task(self) -> None:
        cases = (
            ("chat", "general_chat", "chat_reply", "none", []),
            ("query", "system_status", "read_only_tool", "none", ["get_system_info"]),
            ("task", "open_application", "plan_then_execute", "defer_to_safety", ["start_whitelisted_application"]),
        )
        for route, intent, mode, confirmation, tools in cases:
            with self.subTest(route=route):
                result = self.route(self.success_payload(
                    route=route,
                    intent=intent,
                    handling_mode=mode,
                    required_confirmation=confirmation,
                    tool_scope=tools,
                ))
                self.assertIs(result.trace, self.trace)
                self.assertEqual((result.route, result.intent, result.router.status), (route, intent, "success"))
                self.assertEqual(result.required_confirmation, confirmation)
                self.assertEqual(result.tool_scope, tuple(tools))

    def test_failure_matrix_always_stays_ambiguous_and_clears_payload(self) -> None:
        failures = (
            ("timeout", "router_unavailable"),
            ("unavailable", "router_unavailable"),
            ("transport_error", "router_unavailable"),
            ("invalid_response", "invalid_router_output"),
            ("low_confidence", "low_confidence"),
        )
        for status, intent in failures:
            with self.subTest(status=status):
                payload: object = {"status": status}
                if status == "low_confidence":
                    payload = {"status": status, "confidence": 0.42}
                result = self.route(payload)
                self.assertIs(result.trace, self.trace)
                self.assertEqual((result.route, result.intent), ("ambiguous", intent))
                self.assertEqual(result.router.status, status)
                self.assertEqual(result.slots, {})
                self.assertEqual(result.tool_scope, ())
                self.assertEqual(result.required_confirmation, "must_clarify")

    def test_exception_matrix_is_normalized_without_exception_text(self) -> None:
        errors: tuple[tuple[type[BaseException], str], ...] = (
            (TimeoutError, "router_unavailable"),
            (ConnectionError, "router_unavailable"),
            (OSError, "router_unavailable"),
            (ValueError, "invalid_router_output"),
        )
        for error_type, intent in errors:
            with self.subTest(error=error_type.__name__):
                def provider(_: ProviderRequest, error_type: type[BaseException] = error_type) -> object:
                    raise error_type("provider secret must not cross boundary")

                result = ProviderIntentRouter(
                    provider,
                    provider_name="matrix-provider",
                    model_id="matrix-model",
                    model_version="v1",
                ).route(self.request)
                self.assertEqual((result.route, result.intent), ("ambiguous", intent))
                self.assertNotIn("secret", repr(result))

    def test_provider_adapter_and_conversation_manager_share_one_trace_boundary(self) -> None:
        def provider(request: ProviderRequest) -> object:
            if request.raw_text == "你好":
                return self.success_payload(
                    route="chat", intent="general_chat", handling_mode="chat_reply"
                )
            return self.success_payload(
                route="task", intent="open_application", handling_mode="plan_then_execute",
                required_confirmation="defer_to_safety",
                tool_scope=["start_whitelisted_application"],
                slots={
                    "application_name": {
                        "value": "calc",
                        "source": "classifier",
                        "confidence": 0.91,
                        "confirmed": False,
                    }
                },
            )

        adapter = _ConversationAdapter()
        router = ProviderIntentRouter(
            provider,
            provider_name="matrix-provider",
            model_id="matrix-model",
            model_version="v1",
        )
        manager = ConversationManager(adapter, router=router)

        text_result = manager.submit_text("你好", request_id="request-text", turn_id="turn-text")
        asr_result = manager.submit_asr_final(
            "打开计算器", request_id="request-asr", turn_id="turn-asr"
        )

        self.assertEqual(text_result.handoff.trace.source, "text_shell")
        self.assertEqual(asr_result.handoff.trace.source, "asr")
        self.assertEqual(text_result.handoff.trace.raw_text, "你好")
        self.assertEqual(asr_result.handoff.trace.raw_text, "打开计算器")
        self.assertIsNotNone(text_result.outcome)
        self.assertIsNone(asr_result.outcome)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0][2], "你好")
        self.assertEqual(text_result.handoff.trace.conversation_id, asr_result.handoff.trace.conversation_id)

    def test_ambiguous_provider_result_never_enters_conversation_adapter(self) -> None:
        adapter = _ConversationAdapter()
        router = ProviderIntentRouter(
            lambda _: {"status": "low_confidence", "confidence": 0.42},
            provider_name="matrix-provider",
            model_id="matrix-model",
            model_version="v1",
        )
        manager = ConversationManager(adapter, router=router)

        result = manager.submit_text("可能的指令", request_id="request-ambiguous", turn_id="turn-ambiguous")

        self.assertEqual((result.handoff.route, result.handoff.intent), ("ambiguous", "low_confidence"))
        self.assertIsNone(result.outcome)
        self.assertEqual(adapter.calls, [])

    def test_direct_failure_handoff_is_normalized_before_conversation(self) -> None:
        def provider(request: ProviderRequest) -> RouteHandoff:
            return RouteHandoff(
                trace=request.trace,
                route="ambiguous",
                intent="router_unavailable",
                handling_mode="clarify",
                confidence=0.0,
                slots={},
                required_confirmation="must_clarify",
                tool_scope=(),
                router=RouterStatus(
                    provider="matrix-provider",
                    model_id="matrix-model",
                    model_version="v1",
                    status="timeout",
                    failure_code="router_unavailable",
                ),
            )

        adapter = _ConversationAdapter()
        manager = ConversationManager(
            adapter,
            router=ProviderIntentRouter(
                provider,
                provider_name="matrix-provider",
                model_id="matrix-model",
                model_version="v1",
            ),
        )

        result = manager.submit_text("稍后再试", request_id="request-timeout", turn_id="turn-timeout")

        self.assertEqual(result.handoff.router.status, "timeout")
        self.assertEqual(result.handoff.tool_scope, ())
        self.assertEqual(result.handoff.slots, {})
        self.assertIsNone(result.outcome)
        self.assertEqual(adapter.calls, [])


if __name__ == "__main__":
    unittest.main()