from __future__ import annotations

import inspect
import unittest

from app.contracts import ContractError, RouteHandoff, RouteTrace, RouterStatus
from app.routing import IntentRouter, RouteInput


class StubRouter:
    def __init__(self, handoff: RouteHandoff) -> None:
        self.handoff = handoff
        self.calls: list[RouteInput] = []

    def route(self, request: RouteInput) -> RouteHandoff:
        self.calls.append(request)
        return self.handoff


class IntentRouterInterfaceTests(unittest.TestCase):
    def make_trace(self, *, raw_text: str = "打开计算器") -> RouteTrace:
        return RouteTrace(
            request_id="request-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            source="text_shell",
            raw_text=raw_text,
            created_at="2026-09-24T10:00:00Z",
        )

    def make_handoff(self, *, trace: RouteTrace | None = None) -> RouteHandoff:
        actual_trace = trace or self.make_trace()
        return RouteHandoff(
            trace=actual_trace,
            route="task",
            intent="open_application",
            handling_mode="plan_then_execute",
            confidence=0.94,
            slots={},
            required_confirmation="defer_to_safety",
            tool_scope=("start_whitelisted_application",),
            router=RouterStatus(
                provider="stub",
                model_id="intent-router-test",
                model_version="1",
                status="success",
                failure_code=None,
            ),
        )

    def test_route_input_is_frozen_and_keeps_raw_text_in_trace(self) -> None:
        trace = self.make_trace()
        request = RouteInput(raw_text=trace.raw_text, trace=trace)
        self.assertEqual(request.raw_text, "打开计算器")
        self.assertIs(request.trace, trace)
        with self.assertRaises(AttributeError):
            request.raw_text = "篡改"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            request.trace = self.make_trace(raw_text="另一个请求")  # type: ignore[misc]

    def test_route_input_rejects_empty_or_mismatched_text(self) -> None:
        trace = self.make_trace()
        for raw_text in ("", "   "):
            with self.subTest(raw_text=raw_text), self.assertRaises(ContractError):
                RouteInput(raw_text=raw_text, trace=trace)
        with self.assertRaises(ContractError):
            RouteInput(raw_text="删除文件", trace=trace)
        with self.assertRaises(ContractError):
            RouteInput(raw_text="打开计算器", trace=object())  # type: ignore[arg-type]

    def test_protocol_accepts_structural_router_and_returns_one_handoff(self) -> None:
        trace = self.make_trace()
        request = RouteInput(raw_text=trace.raw_text, trace=trace)
        expected = self.make_handoff(trace=trace)
        router: IntentRouter = StubRouter(expected)
        result = router.route(request)
        self.assertIs(result, expected)
        self.assertEqual(result.trace, trace)
        self.assertEqual(result.trace.raw_text, request.raw_text)
        self.assertEqual(len(router.__class__.__dict__["route"].__annotations__), 2)

    def test_router_interface_has_no_provider_or_execution_policy(self) -> None:
        self.assertTrue(getattr(IntentRouter, "_is_protocol", False))
        self.assertEqual(tuple(IntentRouter.__dict__.get("__annotations__", {})), ())
        source = inspect.getsource(IntentRouter)
        for forbidden in ("requests", "urllib", "subprocess", "threading", "Executor", "ToolRegistry"):
            self.assertNotIn(forbidden, source)

    def test_route_input_does_not_generate_or_replace_trace_identifiers(self) -> None:
        trace = self.make_trace()
        request = RouteInput(raw_text=trace.raw_text, trace=trace)
        self.assertEqual(
            (request.trace.request_id, request.trace.conversation_id, request.trace.turn_id),
            ("request-1", "conversation-1", "turn-1"),
        )


if __name__ == "__main__":
    unittest.main()