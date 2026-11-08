from __future__ import annotations

import inspect
import unittest

from app.contracts import ContractError, RouteTrace
from app.routing import normalize_router_failure


class FailureNormalizerTests(unittest.TestCase):
    def make_trace(self, raw_text: str = "打开计算器") -> RouteTrace:
        return RouteTrace(
            request_id="request-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            source="text_shell",
            raw_text=raw_text,
            created_at="2026-09-24T10:00:00Z",
        )

    def test_provider_failures_become_safe_ambiguous_handoffs(self) -> None:
        expected = {
            "unavailable": "router_unavailable",
            "timeout": "router_unavailable",
            "transport_error": "router_unavailable",
            "invalid_response": "invalid_router_output",
        }
        for status, intent in expected.items():
            with self.subTest(status=status):
                result = normalize_router_failure(
                    self.make_trace(),
                    provider="laya",
                    model_id="intent-router-v1",
                    model_version="1",
                    status=status,
                )
                self.assertEqual((result.route, result.intent), ("ambiguous", intent))
                self.assertEqual(result.router.status, status)
                self.assertEqual(result.router.failure_code, intent)
                self.assertEqual(result.handling_mode, "clarify")
                self.assertEqual(result.required_confirmation, "must_clarify")

    def test_low_confidence_preserves_safe_score_and_never_routes(self) -> None:
        result = normalize_router_failure(
            self.make_trace(),
            provider="laya",
            model_id="intent-router-v1",
            model_version="1",
            status="low_confidence",
            confidence=0.42,
        )
        self.assertEqual((result.route, result.intent), ("ambiguous", "low_confidence"))
        self.assertEqual(result.confidence, 0.42)
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})

    def test_failure_handoff_keeps_trace_identity_and_has_no_confirmed_slots(self) -> None:
        trace = self.make_trace()
        result = normalize_router_failure(
            trace,
            provider="laya",
            model_id="intent-router-v1",
            model_version="1",
            status="invalid_response",
        )
        self.assertIs(result.trace, trace)
        self.assertEqual(
            (result.trace.request_id, result.trace.conversation_id, result.trace.turn_id),
            ("request-1", "conversation-1", "turn-1"),
        )
        self.assertEqual(result.slots, {})
        self.assertEqual(result.tool_scope, ())

    def test_success_unknown_status_and_bad_confidence_are_rejected(self) -> None:
        common = dict(
            provider="laya",
            model_id="intent-router-v1",
            model_version="1",
        )
        for status in ("success", "unknown", "invalid_router_output", [], {"bad": "status"}):
            with self.subTest(status=status), self.assertRaises(ContractError):
                normalize_router_failure(self.make_trace(), status=status, **common)
        with self.assertRaises(ContractError):
            normalize_router_failure(self.make_trace(), status="low_confidence", confidence=0.80, **common)
        with self.assertRaises(ContractError):
            normalize_router_failure(self.make_trace(), status="low_confidence", confidence=-0.01, **common)

    def test_boundary_does_not_accept_or_expose_provider_payloads(self) -> None:
        source = inspect.getsource(normalize_router_failure)
        for forbidden in ("requests", "urllib", "subprocess", "threading", "ToolRegistry", "ExecutionLedger"):
            self.assertNotIn(forbidden, source)
        with self.assertRaises(ContractError):
            normalize_router_failure(
                self.make_trace(),
                provider="laya",
                model_id="intent-router-v1",
                model_version="1",
                status="invalid_response",
                error_text="secret provider response",
            )


if __name__ == "__main__":
    unittest.main()
