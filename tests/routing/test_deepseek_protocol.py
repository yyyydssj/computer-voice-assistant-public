from __future__ import annotations

import inspect
import json
import unittest

from app.contracts import RouteTrace
from app.routing import ProviderIntentRouter, RouteInput, parse_deepseek_raw_route_response, parse_deepseek_route_response


class DeepSeekStructuredRouteProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = RouteTrace(
            request_id="request-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            source="text_shell",
            raw_text="打开计算器",
            created_at="2026-09-25T13:00:00Z",
        )
        self.request = RouteInput(raw_text=self.trace.raw_text, trace=self.trace)

    def success(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "deepseek-route/v1",
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

    def route_json(self, payload: object):
        encoded = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ProviderIntentRouter(
            lambda _: parse_deepseek_route_response(encoded),
            provider_name="deepseek-official",
            model_id="deepseek-v4-flash",
            model_version="v1",
        ).route(self.request)

    def raw_route_json(self, payload: str) -> dict[str, object]:
        return parse_deepseek_raw_route_response(payload)

    def test_raw_output_accepts_only_one_bare_json_document(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        for raw in (payload, " \n" + payload + "\n\t"):
            with self.subTest(raw=raw):
                parsed = self.raw_route_json(raw)
                self.assertEqual(parsed["status"], "success")
                self.assertNotIn("schema_version", parsed)

    def test_raw_output_rejects_markdown_and_preamble_or_postamble(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        wrapped = ("```json\n" + payload + "\n```", "Here is the JSON:\n" + payload, payload + "\nDone")
        for raw in wrapped:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.raw_route_json(raw)

    def test_raw_output_rejects_multiple_documents_empty_and_non_text(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        invalid_text = (payload + payload, "", "   \n")
        for raw in invalid_text:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.raw_route_json(raw)
        for raw in (None, {"status": "success"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_deepseek_raw_route_response(raw)  # type: ignore[arg-type]


    def test_raw_output_rejects_nested_duplicate_fields(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        duplicate_slots = payload.replace(
            '"slots": {"application_name": {',
            '"slots": {"application_name": {"value": "calc", "value": "calculator",',
            1,
        )
        with self.assertRaises(ValueError):
            self.raw_route_json(duplicate_slots)

    def test_raw_output_rejects_non_standard_numeric_tokens(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        for token in ("NaN", "Infinity", "-Infinity"):
            raw = payload.replace("0.96", token, 1)
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.raw_route_json(raw)

    def test_raw_output_rejects_non_object_json_documents(self) -> None:
        for raw in ("[]", '"text"', "null", "42"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.raw_route_json(raw)

    def test_raw_output_accepts_strict_failure_envelopes_without_schema_metadata(self) -> None:
        for payload in (
            {"schema_version": "deepseek-route/v1", "status": "timeout"},
            {"schema_version": "deepseek-route/v1", "status": "low_confidence", "confidence": 0.42},
        ):
            raw = json.dumps(payload, ensure_ascii=False)
            with self.subTest(payload=payload):
                parsed = self.raw_route_json(raw)
                self.assertEqual(parsed["status"], payload["status"])
                self.assertNotIn("schema_version", parsed)

    def test_raw_output_rejects_failure_debug_and_candidate_fields(self) -> None:
        payload = {
            "schema_version": "deepseek-route/v1",
            "status": "timeout",
            "debug": "脱敏调试信息",
            "slots": {"candidate": "脱敏候选"},
        }
        with self.assertRaises(ValueError) as caught:
            self.raw_route_json(json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("脱敏调试信息", str(caught.exception))
        self.assertNotIn("脱敏候选", str(caught.exception))
    def test_raw_output_can_feed_existing_provider_router_without_raw_text_leak(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        router = ProviderIntentRouter(
            lambda _: parse_deepseek_raw_route_response(payload),
            provider_name="deepseek-official",
            model_id="deepseek-v4-flash",
            model_version="v1",
        )
        handoff = router.route(self.request)
        self.assertEqual((handoff.route, handoff.intent), ("task", "open_application"))
        self.assertNotIn(payload, repr(handoff))

    def test_raw_output_failure_does_not_expose_fixture_text(self) -> None:
        secret = "脱敏原始响应-不可传播"
        with self.assertRaises(ValueError) as caught:
            self.raw_route_json("preamble " + secret + " {not-json}")
        self.assertNotIn(secret, str(caught.exception))

    def test_valid_chat_query_and_task_json_is_mapped_without_schema_metadata(self) -> None:
        variants = (
            self.success(
                route="chat", intent="general_chat", handling_mode="chat_reply",
                slots={}, required_confirmation="none", tool_scope=[],
            ),
            self.success(
                route="query", intent="system_status", handling_mode="read_only_tool",
                slots={}, required_confirmation="none", tool_scope=["get_system_info"],
            ),
            self.success(),
        )
        for payload in variants:
            with self.subTest(route=payload["route"]):
                handoff = self.route_json(payload)
                self.assertEqual(handoff.router.status, "success")
                self.assertEqual(handoff.route, payload["route"])
                self.assertIs(handoff.trace, self.trace)

    def test_schema_version_is_required_and_exact(self) -> None:
        for version in (None, "deepseek-route/v2", 1):
            payload = self.success()
            if version is None:
                del payload["schema_version"]
            else:
                payload["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                parse_deepseek_route_response(json.dumps(payload))

    def test_unknown_and_duplicate_fields_are_rejected(self) -> None:
        payload = self.success(debug="secret")
        with self.assertRaises(ValueError):
            parse_deepseek_route_response(json.dumps(payload))
        duplicate = json.dumps(self.success())[:-1] + ', "status": "success"}'
        with self.assertRaises(ValueError):
            parse_deepseek_route_response(duplicate)

    def test_success_field_types_are_strict(self) -> None:
        invalid = (
            self.success(status=True),
            self.success(route=1),
            self.success(intent=None),
            self.success(handling_mode=[]),
            self.success(required_confirmation=False),
            self.success(confidence=True),
            self.success(confidence=float("nan")),
            self.success(confidence=float("inf")),
            self.success(slots=[]),
            self.success(tool_scope="start_whitelisted_application"),
            self.success(tool_scope=["get_system_info", 1]),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_deepseek_route_response(json.dumps(payload, allow_nan=True))

    def test_low_confidence_is_fail_closed_and_discards_candidates(self) -> None:
        result = self.route_json(self.success(confidence=0.79))
        self.assertEqual((result.route, result.intent), ("ambiguous", "low_confidence"))
        self.assertEqual(result.confidence, 0.79)
        self.assertEqual(result.slots, {})
        self.assertEqual(result.tool_scope, ())

    def test_failure_responses_are_strict_and_normalized(self) -> None:
        for status in ("timeout", "unavailable", "transport_error", "invalid_response"):
            payload = {"schema_version": "deepseek-route/v1", "status": status}
            result = self.route_json(payload)
            self.assertEqual(result.intent, "router_unavailable" if status != "invalid_response" else "invalid_router_output")
            self.assertEqual(result.router.status, status)
            self.assertEqual(result.tool_scope, ())
        result = self.route_json({
            "schema_version": "deepseek-route/v1",
            "status": "low_confidence",
            "confidence": 0.42,
        })
        self.assertEqual((result.intent, result.confidence), ("low_confidence", 0.42))

    def test_failure_payload_cannot_carry_debug_or_candidate_fields(self) -> None:
        payload = {
            "schema_version": "deepseek-route/v1",
            "status": "timeout",
            "debug": "secret",
            "slots": {"content": {"value": "secret"}},
        }
        result = self.route_json(payload)
        self.assertEqual(result.intent, "invalid_router_output")
        self.assertNotIn("secret", repr(result))
        self.assertEqual(result.slots, {})

    def test_contract_mismatch_and_dangerous_scope_fail_closed(self) -> None:
        cases = (
            self.success(route="chat", intent="open_application", handling_mode="chat_reply", slots={}, required_confirmation="none", tool_scope=[]),
            self.success(tool_scope=["move_to_recycle_bin"]),
            self.success(slots={"application_name": {
                "value": "calc", "source": "classifier", "confidence": 0.9, "confirmed": True,
            }}),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.route_json(payload)
                self.assertEqual(result.intent, "invalid_router_output")
                self.assertEqual(result.tool_scope, ())

    def test_protocol_has_no_network_or_execution_dependencies(self) -> None:
        source = inspect.getsource(parse_deepseek_route_response)
        for forbidden in ("requests", "urllib", "httpx", "subprocess", "threading", "ToolRegistry", "ExecutionLedger"):
            self.assertNotIn(forbidden, source)


    def test_raw_output_preserves_escaped_unicode_and_whitespace_inside_json_strings(self) -> None:
        payload = self.success(
            slots={
                "application_name": {
                    "value": "计算器\n\"测试\"",
                    "source": "classifier",
                    "confidence": 0.91,
                    "confirmed": False,
                },
            },
        )
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        parsed = self.raw_route_json(" \r\n" + raw + "\n\t")
        self.assertEqual(parsed["slots"]["application_name"]["value"], "计算器\n\"测试\"")
        self.assertNotIn("schema_version", parsed)

    def test_raw_output_accepts_each_strict_failure_envelope(self) -> None:
        for status in ("unavailable", "transport_error", "invalid_response"):
            raw = json.dumps({"schema_version": "deepseek-route/v1", "status": status})
            with self.subTest(status=status):
                parsed = self.raw_route_json(raw)
                self.assertEqual(parsed, {"status": status})

    def test_raw_output_returns_fresh_nested_mappings(self) -> None:
        raw = json.dumps(self.success(), ensure_ascii=False)
        first = self.raw_route_json(raw)
        first["slots"]["application_name"]["value"] = "mutated"
        second = self.raw_route_json(raw)
        self.assertEqual(second["slots"]["application_name"]["value"], "calc")
        self.assertIsNot(first, second)
        self.assertIsNot(first["slots"], second["slots"])

    def test_raw_output_accepts_success_fields_in_any_json_order(self) -> None:
        payload = self.success()
        reordered = {key: payload[key] for key in reversed(tuple(payload))}
        parsed = self.raw_route_json(json.dumps(reordered, ensure_ascii=False))
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["route"], "task")
        self.assertEqual(parsed["tool_scope"], ["start_whitelisted_application"])
        self.assertNotIn("schema_version", parsed)

    def test_raw_output_accepts_low_confidence_boundary_scores(self) -> None:
        for score in (0, 1):
            raw = json.dumps(
                {
                    "schema_version": "deepseek-route/v1",
                    "status": "low_confidence",
                    "confidence": score,
                },
            )
            with self.subTest(score=score):
                self.assertEqual(
                    self.raw_route_json(raw),
                    {"status": "low_confidence", "confidence": score},
                )

    def test_raw_output_returns_fresh_tool_scope_lists(self) -> None:
        raw = json.dumps(self.success(), ensure_ascii=False)
        first = self.raw_route_json(raw)
        first["tool_scope"].append("mutated-local-only")
        second = self.raw_route_json(raw)
        self.assertEqual(second["tool_scope"], ["start_whitelisted_application"])
        self.assertIsNot(first["tool_scope"], second["tool_scope"])

    def test_raw_output_rejects_utf8_bom_and_invalid_prefix_without_echo(self) -> None:
        payload = json.dumps(self.success(), ensure_ascii=False)
        secret = "脱敏-BOM-fixture"
        for raw in ("\ufeff" + payload, "\ufeff " + payload, "prefix " + secret + " " + payload):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                self.raw_route_json(raw)
            self.assertEqual(str(caught.exception), "DeepSeek raw route output is invalid")
            self.assertNotIn(secret, str(caught.exception))

    def test_raw_output_error_does_not_echo_fixture_content(self) -> None:
        secret = "脱敏秘密-debug-candidate"
        raw = json.dumps(
            self.success(debug=secret),
            ensure_ascii=False,
        )
        with self.assertRaises(ValueError) as raised:
            self.raw_route_json(raw)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(str(raised.exception), "DeepSeek raw route output is invalid")


if __name__ == "__main__":
    unittest.main()
