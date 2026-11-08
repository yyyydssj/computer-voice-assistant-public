from __future__ import annotations

import inspect
import unittest

from app.contracts import ContractError, RouteTrace
from app.routing import (
    ProviderIntentRouter,
    ProviderRequest,
    RouterProviderUnavailable,
    RouteInput,
)


class ProviderIntentRouterTests(unittest.TestCase):
    def trace(self, raw_text: str = "打开计算器") -> RouteTrace:
        return RouteTrace(
            request_id="request-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            source="text_shell",
            raw_text=raw_text,
            created_at="2026-09-25T10:00:00Z",
        )

    def route_input(self) -> RouteInput:
        trace = self.trace()
        return RouteInput(raw_text=trace.raw_text, trace=trace)

    def success_payload(self) -> dict[str, object]:
        return {
            "status": "success",
            "route": "task",
            "intent": "open_application",
            "handling_mode": "plan_then_execute",
            "confidence": 0.96,
            "slots": {},
            "required_confirmation": "defer_to_safety",
            "tool_scope": ["start_whitelisted_application"],
        }

    def router(self, provider: object) -> ProviderIntentRouter:
        return ProviderIntentRouter(
            provider,
            provider_name="stub-provider",
            model_id="intent-router",
            model_version="2026-09-25",
        )

    def test_provider_request_is_trace_bound_and_immutable(self) -> None:
        request = ProviderRequest(raw_text="打开计算器", trace=self.trace())
        self.assertIs(request.trace, request.trace)
        with self.assertRaises(AttributeError):
            request.raw_text = "替换"  # type: ignore[misc]
        with self.assertRaises(ContractError):
            ProviderRequest(raw_text="另一句", trace=self.trace())

    def test_success_mapping_becomes_valid_handoff_and_keeps_trace_identity(self) -> None:
        received: list[ProviderRequest] = []

        def provider(request: ProviderRequest) -> object:
            received.append(request)
            return self.success_payload()

        handoff = self.router(provider).route(self.route_input())

        self.assertEqual((handoff.route, handoff.intent), ("task", "open_application"))
        self.assertEqual(handoff.router.provider, "stub-provider")
        self.assertIs(handoff.trace, received[0].trace)
        self.assertEqual(handoff.trace.raw_text, "打开计算器")

    def test_provider_failure_statuses_are_normalized_without_payload(self) -> None:
        for status, expected in (
            ("unavailable", "router_unavailable"),
            ("timeout", "router_unavailable"),
            ("low_confidence", "low_confidence"),
        ):
            with self.subTest(status=status):
                def provider(_: ProviderRequest, status: str = status) -> object:
                    return {"status": status, **({"confidence": 0.42} if status == "low_confidence" else {})}

                result = self.router(provider).route(self.route_input())
                self.assertEqual((result.route, result.intent), ("ambiguous", expected))
                self.assertEqual(result.router.status, status)
                self.assertEqual(result.tool_scope, ())

    def test_timeout_and_transport_exceptions_are_normalized(self) -> None:
        for error, expected in ((TimeoutError(), "timeout"), (ConnectionError(), "transport_error")):
            with self.subTest(error=type(error).__name__):
                def provider(_: ProviderRequest, error: BaseException = error) -> object:
                    raise error

                result = self.router(provider).route(self.route_input())
                self.assertEqual(result.intent, "router_unavailable")
                self.assertEqual(result.router.status, expected)

    def test_unavailable_exception_is_normalized(self) -> None:
        def provider(_: ProviderRequest) -> object:
            raise RouterProviderUnavailable("provider secret must not cross boundary")

        result = self.router(provider).route(self.route_input())
        self.assertEqual((result.route, result.intent), ("ambiguous", "router_unavailable"))
        self.assertEqual(result.router.status, "unavailable")

    def test_invalid_payload_is_safe_ambiguous_and_does_not_expose_payload(self) -> None:
        def provider(_: ProviderRequest) -> object:
            return {"route": "task", "secret": "do-not-return"}

        result = self.router(provider).route(self.route_input())
        self.assertEqual((result.route, result.intent), ("ambiguous", "invalid_router_output"))
        self.assertNotIn("secret", repr(result))
        self.assertEqual(result.slots, {})
        self.assertEqual(result.tool_scope, ())

    def test_provider_handoff_metadata_must_match_adapter(self) -> None:
        from app.contracts import RouteHandoff, RouterStatus

        def provider(request: ProviderRequest) -> object:
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
                    provider="other-provider",
                    model_id="other-model",
                    model_version="other-version",
                    status="success",
                    failure_code=None,
                ),
            )

        result = self.router(provider).route(self.route_input())
        self.assertEqual(result.intent, "invalid_router_output")
        self.assertEqual(result.router.provider, "stub-provider")

    def test_trace_replacement_is_rejected_and_normalized(self) -> None:
        def provider(_: ProviderRequest) -> object:
            replacement = self.trace("替换文本")
            return {
                **self.success_payload(),
                "trace": replacement,
            }

        result = self.router(provider).route(self.route_input())
        self.assertEqual(result.intent, "invalid_router_output")
        self.assertEqual(result.trace.raw_text, "打开计算器")

    def test_low_confidence_success_is_normalized(self) -> None:
        payload = self.success_payload()
        payload["confidence"] = 0.79
        result = self.router(lambda _: payload).route(self.route_input())
        self.assertEqual(result.intent, "low_confidence")
        self.assertEqual(result.confidence, 0.79)

    def test_unknown_fields_and_confirmed_slots_are_rejected(self) -> None:
        payload = self.success_payload()
        payload["unexpected"] = True
        result = self.router(lambda _: payload).route(self.route_input())
        self.assertEqual(result.intent, "invalid_router_output")

        payload = self.success_payload()
        payload["slots"] = {
            "application_name": {
                "value": "calc",
                "source": "classifier",
                "confidence": 0.9,
                "confirmed": True,
            }
        }
        result = self.router(lambda _: payload).route(self.route_input())
        self.assertEqual(result.intent, "invalid_router_output")

    def test_adapter_has_no_network_or_execution_dependencies(self) -> None:
        source = inspect.getsource(ProviderIntentRouter)
        for forbidden in ("requests", "urllib", "subprocess", "threading", "ToolRegistry", "ExecutionLedger"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
