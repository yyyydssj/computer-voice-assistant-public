"""Offline provider schema fixtures: no API, model, or network dependency."""

from __future__ import annotations

import unittest

from app.contracts import RouteHandoff, RouteTrace, RouterStatus, SlotCandidate
from app.routing import ProviderIntentRouter, RouteInput


class ProviderSchemaFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = RouteTrace(
            request_id="request-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            source="text_shell",
            raw_text="打开计算器",
            created_at="2026-09-25T10:00:00Z",
        )
        self.request = RouteInput(raw_text=self.trace.raw_text, trace=self.trace)

    def route(self, payload: object) -> RouteHandoff:
        return ProviderIntentRouter(
            lambda _: payload,
            provider_name="fixture-provider",
            model_id="fixture-intent-model",
            model_version="v1",
        ).route(self.request)

    def success(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "success",
            "route": "task",
            "intent": "open_application",
            "handling_mode": "plan_then_execute",
            "confidence": 0.96,
            "slots": {
                "application_name": {
                    "value": "calc",
                    "source": "classifier",
                    "confidence": 0.91,
                    "confirmed": False,
                },
            },
            "required_confirmation": "defer_to_safety",
            "tool_scope": ["start_whitelisted_application"],
        }
        payload.update(changes)
        return payload

    def assert_safe_failure(self, payload: object, status: str = "invalid_response") -> RouteHandoff:
        result = self.route(payload)
        self.assertIs(result.trace, self.trace)
        self.assertEqual((result.route, result.handling_mode), ("ambiguous", "clarify"))
        self.assertEqual(result.router.status, status)
        self.assertEqual(result.slots, {})
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.required_confirmation, "must_clarify")
        return result

    def test_supported_schema_fixtures_preserve_trace_and_candidate_only(self) -> None:
        variants = (
            self.success(confidence=0.80),
            self.success(
                route="query", intent="system_status", handling_mode="read_only_tool",
                slots={}, required_confirmation="none", tool_scope=["get_system_info"],
            ),
            self.success(
                route="chat", intent="general_chat", handling_mode="chat_reply",
                slots={}, required_confirmation="none", tool_scope=[],
            ),
        )
        for payload in variants:
            with self.subTest(route=payload["route"]):
                result = self.route(payload)
                self.assertIs(result.trace, self.trace)
                self.assertEqual(result.router.status, "success")
                self.assertEqual(result.route, payload["route"])
                self.assertTrue(all(not slot.confirmed for slot in result.slots.values()))

    def test_mapping_payload_is_copied_before_return(self) -> None:
        payload = self.success()
        result = self.route(payload)
        payload["tool_scope"].append("move_to_recycle_bin")  # type: ignore[union-attr]
        payload["slots"]["application_name"]["value"] = "other"  # type: ignore[index]
        self.assertEqual(result.tool_scope, ("start_whitelisted_application",))
        self.assertEqual(result.slots["application_name"].value, "calc")

    def test_malformed_schema_fixtures_fail_closed(self) -> None:
        bad = (
            self.success(schema_version=2),
            self.success(route="dangerous"),
            self.success(confidence=True),
            self.success(confidence=float("nan")),
            self.success(tool_scope=["move_to_recycle_bin"]),
            self.success(tool_scope=("start_whitelisted_application",)),
            self.success(slots={"application_name": {"value": "calc", "source": "classifier", "confidence": 1.0, "confirmed": True}}),
            self.success(slots={"application_name": {"value": "secret", "source": "classifier", "confidence": float("inf"), "confirmed": False}}),
            self.success(status="unexpected"),
            {"status": "timeout", "debug": "secret"},
        )
        for index, payload in enumerate(bad):
            with self.subTest(index=index):
                result = self.assert_safe_failure(payload)
                self.assertNotIn("secret", repr(result))

    def test_low_confidence_success_does_not_keep_candidate_payload(self) -> None:
        result = self.assert_safe_failure(self.success(confidence=0.79), "low_confidence")
        self.assertEqual(result.confidence, 0.79)

    def test_direct_failure_handoff_must_be_normalized_and_strip_slots(self) -> None:
        for status, intent, score in (
            ("timeout", "router_unavailable", 0.0),
            ("low_confidence", "low_confidence", 0.42),
        ):
            with self.subTest(status=status):
                payload = RouteHandoff(
                    trace=self.trace, route="ambiguous", intent=intent,
                    handling_mode="clarify", confidence=score,
                    slots={"content": SlotCandidate("secret", "classifier", 0.9, False)},
                    required_confirmation="must_clarify", tool_scope=(),
                    router=RouterStatus(
                        provider="fixture-provider", model_id="fixture-intent-model",
                        model_version="v1", status=status, failure_code=intent,
                    ),
                )
                result = self.assert_safe_failure(payload, status)
                self.assertIsNot(result, payload)
                self.assertNotIn("secret", repr(result))
                self.assertEqual(result.confidence, score)


if __name__ == "__main__":
    unittest.main()