"""P7-R-01: the routing contract is data, never permission to execute."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import FrozenInstanceError

from app.contracts import ContractError, RouteHandoff, RouteTrace, RouterStatus, SlotCandidate


class RouteHandoffTests(unittest.TestCase):
    def trace(self, **changes):
        data = dict(request_id="request-1", conversation_id="chat-session-1", turn_id="turn-1",
                    source="asr", raw_text="打开微信", created_at="2026-09-24T12:00:00Z")
        data.update(changes)
        return RouteTrace(**data)

    def router(self, **changes):
        data = dict(provider="local", model_id="assistant-intent-v1", model_version="1",
                    status="success", failure_code=None)
        data.update(changes)
        return RouterStatus(**data)

    def handoff(self, **changes):
        data = dict(trace=self.trace(), route="task", intent="open_application",
                    handling_mode="plan_then_execute", confidence=0.94,
                    slots={"application_name": SlotCandidate(value="微信", source="utterance",
                                                         confidence=0.98, confirmed=False)},
                    required_confirmation="defer_to_safety",
                    tool_scope=("start_whitelisted_application",), router=self.router())
        data.update(changes)
        return RouteHandoff(**data)

    def test_public_frozen_versioned_round_trip_and_trace(self):
        original = self.handoff()
        self.assertEqual(RouteHandoff.from_dict(original.to_dict()), original)
        self.assertEqual(RouteHandoff.from_json(original.to_json()), original)
        self.assertEqual(original.to_dict()["schema_version"], 1)
        self.assertNotIn("schema_version", original.to_dict()["trace"])
        self.assertEqual(original.trace.request_id, "request-1")
        with self.assertRaises(FrozenInstanceError):
            original.route = "chat"
        with self.assertRaises(TypeError):
            original.slots["application_name"] = "changed"

    def test_missing_unknown_and_bad_versions_at_every_level(self):
        original = self.handoff().to_dict()
        for field in tuple(original):
            with self.subTest(missing=field):
                payload = copy.deepcopy(original)
                del payload[field]
                with self.assertRaises(ContractError):
                    RouteHandoff.from_dict(payload)
        for level in ((), ("trace",), ("router",), ("slots", "application_name")):
            with self.subTest(extra=level):
                payload = copy.deepcopy(original)
                target = payload
                for part in level:
                    target = target[part]
                target["unknown_field"] = "bad"
                with self.assertRaises(ContractError):
                    RouteHandoff.from_dict(payload)
        for version in (True, 0, 2, "1"):
            with self.subTest(version=version):
                with self.assertRaises(ContractError):
                    RouteHandoff.from_dict({**original, "schema_version": version})
        for field in tuple(original["trace"]):
            payload = copy.deepcopy(original)
            del payload["trace"][field]
            with self.subTest(missing_trace=field), self.assertRaises(ContractError):
                RouteHandoff.from_dict(payload)

    def test_trace_source_identity_timestamp_and_text(self):
        for changes in ({"request_id": ""}, {"conversation_id": "!"}, {"turn_id": ""},
                        {"source": "voice"}, {"raw_text": ""},
                        {"created_at": "2026-09-24T20:00:00+08:00"}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.trace(**changes)
        self.assertEqual(self.trace(source="text_shell").source, "text_shell")

    def test_route_intent_mode_confirmation_and_tool_scope_matrix(self):
        cases = (
            ("chat", "general_chat", "chat_reply", "none", ()),
            ("query", "file_read", "read_only_tool", "none", ("read_text_file",)),
            ("task", "file_operation", "plan_then_execute", "defer_to_safety", ("copy_file",)),
            ("task", "planning_request", "plan_then_execute", "defer_to_safety", ()),
            ("dangerous", "delete_file", "plan_then_safety", "defer_to_safety", ("move_to_recycle_bin",)),
            ("ambiguous", "unknown", "clarify", "must_clarify", ()),
            ("unsupported", "unsupported_request", "reject", "none", ()),
        )
        for route, intent, mode, confirm, scope in cases:
            with self.subTest(route=route):
                self.handoff(route=route, intent=intent, handling_mode=mode,
                             required_confirmation=confirm, tool_scope=scope)
        invalid = (
            dict(route="chat", intent="file_read"),
            dict(route="query", intent="file_read", handling_mode="plan_then_execute"),
            dict(route="query", intent="file_read", tool_scope=("write_text_file",)),
            dict(route="task", intent="open_application", tool_scope=("unknown_tool",)),
            dict(route="task", intent="open_application", tool_scope=("read_text_file",)),
            dict(route="dangerous", intent="delete_file", handling_mode="plan_then_execute"),
            dict(route="dangerous", intent="delete_file", required_confirmation="none"),
            dict(route="ambiguous", intent="unknown", handling_mode="clarify", tool_scope=("copy_file",)),
            dict(route="unsupported", intent="unsupported_request", handling_mode="reject", tool_scope=("read_text_file",)),
            dict(required_confirmation="none"),
            dict(tool_scope=("start_whitelisted_application", "start_whitelisted_application")),
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.handoff(**changes)

    def test_ambiguous_cannot_smuggle_tools_or_confirmed_slots(self):
        self.handoff(route="ambiguous", intent="unknown", handling_mode="clarify",
                     required_confirmation="must_clarify", tool_scope=())
        with self.assertRaises(ContractError):
            self.handoff(slots={"application_name": SlotCandidate(value="微信", source="classifier",
                                                                   confidence=1.0, confirmed=True)})
        payload = self.handoff().to_dict()
        payload["slots"]["application_name"]["confirmed"] = True
        with self.assertRaises(ContractError):
            RouteHandoff.from_dict(payload)

    def test_slot_names_sources_confidence_and_immutable_values(self):
        for source in ("utterance", "normalization", "classifier"):
            self.assertEqual(SlotCandidate(value="x", source=source, confidence=0.8, confirmed=False).source, source)
        for changes in (dict(source="model"), dict(confidence=True), dict(confidence=1.1),
                        dict(confidence=float("nan")), dict(confirmed=1), dict(value=object())):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                SlotCandidate(**{**dict(value="x", source="utterance", confidence=0.9, confirmed=False), **changes})
        for name in ("unexpected", "__proto__"):
            with self.assertRaises(ContractError):
                self.handoff(slots={name: SlotCandidate(value="x", source="utterance", confidence=0.9, confirmed=False)})
        source = {"nested": ["x", {"key": 1}]}
        slot = SlotCandidate(value=source, source="utterance", confidence=0.9, confirmed=False)
        source["nested"].append("mutation")
        self.assertEqual(slot.to_dict()["value"], {"nested": ["x", {"key": 1}]})
        with self.assertRaises(TypeError):
            slot.value["nested"] = ()

    def test_router_status_codes_and_low_confidence_are_safe(self):
        low = self.handoff(route="ambiguous", intent="low_confidence", handling_mode="clarify",
                           confidence=0.4, required_confirmation="must_clarify", tool_scope=(),
                           router=self.router(status="low_confidence", failure_code="low_confidence"))
        self.assertEqual(RouteHandoff.from_json(low.to_json()), low)
        with self.assertRaises(ContractError):
            self.handoff(route="ambiguous", intent="low_confidence", handling_mode="clarify",
                         confidence=0.94, required_confirmation="must_clarify", tool_scope=(),
                         router=self.router(status="low_confidence", failure_code="low_confidence"))
        with self.assertRaises(ContractError):
            self.handoff(confidence=0.79)
        with self.assertRaises(ContractError):
            self.handoff(route="unsupported", intent="unsupported_request", handling_mode="reject",
                         confidence=0.79, required_confirmation="none", tool_scope=())
        with self.assertRaises(ContractError):
            self.router(status="success", failure_code="timeout")
        with self.assertRaises(ContractError):
            self.router(status="timeout", failure_code=None)
        with self.assertRaises(ContractError):
            self.router(status="provider_exception_body", failure_code="some secret")
        with self.assertRaises(ContractError):
            self.handoff(router=self.router(status="timeout", failure_code="router_unavailable"))

    def test_malformed_enums_and_nested_values_raise_contract_error_not_type_error(self):
        for changes in (dict(route=[]), dict(intent=[]), dict(handling_mode=[]),
                        dict(required_confirmation=[]), dict(route=None)):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.handoff(**changes)
        for changes in (dict(source=[]), dict(source=None), dict(confirmed=[])):
            with self.subTest(slot=changes), self.assertRaises(ContractError):
                SlotCandidate(**{**dict(value="x", source="utterance", confidence=0.8, confirmed=False), **changes})
        for changes in (dict(status=[]), dict(failure_code=[]), dict(provider=None)):
            with self.subTest(router=changes), self.assertRaises(ContractError):
                self.router(**changes)
        with self.assertRaises(ContractError):
            self.handoff(slots={1: SlotCandidate(value="x", source="utterance", confidence=0.8, confirmed=False)})
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(ContractError):
            SlotCandidate(value=cyclic, source="utterance", confidence=0.8, confirmed=False)

    def test_failure_statuses_always_have_ambiguous_non_executable_handoffs(self):
        for status, intent in (("timeout", "router_unavailable"),
                               ("unavailable", "router_unavailable"),
                               ("transport_error", "router_unavailable"),
                               ("invalid_response", "invalid_router_output"),
                               ("low_confidence", "low_confidence")):
            with self.subTest(status=status):
                result = self.handoff(route="ambiguous", intent=intent, handling_mode="clarify",
                                      confidence=0.1, tool_scope=(), required_confirmation="must_clarify",
                                      router=self.router(status=status, failure_code=intent))
                self.assertEqual(RouteHandoff.from_json(result.to_json()), result)
                with self.assertRaises(ContractError):
                    self.handoff(route="task", intent="open_application", handling_mode="plan_then_execute",
                                 router=self.router(status=status, failure_code=intent))
    def test_json_rejects_duplicate_keys_nonstandard_numbers_and_wrong_types(self):
        valid = self.handoff().to_json()
        bad_jsons = (
            valid.replace('"route":"task"', '"route":"task","route":"chat"'),
            valid.replace('"source":"utterance"', '"source":"utterance","source":"classifier"'),
            valid.replace('"confidence":0.94', '"confidence":NaN'),
            valid.replace('"confidence":0.94', '"confidence":Infinity'),
        )
        for raw in bad_jsons:
            with self.subTest(raw=raw[:90]), self.assertRaises(ContractError):
                RouteHandoff.from_json(raw)
        for mutation in (dict(trace=[]), dict(slots=[]), dict(tool_scope="read_text_file"),
                         dict(confidence=True), dict(router=None)):
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                RouteHandoff.from_dict({**self.handoff().to_dict(), **mutation})


if __name__ == "__main__":
    unittest.main()