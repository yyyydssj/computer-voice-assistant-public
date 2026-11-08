from __future__ import annotations

import json
import unittest

from app.contracts import ContractError, RouteTrace
from app.providers.deepseek_adapter import (
    DeepSeekAdapterConfig,
    DeepSeekProviderAdapter,
    DeepSeekRequest,
)
from app.routing import RouteInput


class FakeDeepSeekTransport:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[DeepSeekRequest, float]] = []

    def send(self, request: DeepSeekRequest, *, timeout_seconds: float) -> str:
        self.calls.append((request, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class DeepSeekAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = RouteTrace(
            request_id="request-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            source="text_shell",
            raw_text="打开计算器",
            created_at="2026-09-27T10:00:00Z",
        )
        self.route_input = RouteInput(raw_text=self.trace.raw_text, trace=self.trace)

    def success_raw(self, **changes: object) -> str:
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
        return json.dumps(payload, ensure_ascii=False)

    def enabled_config(self, **changes: object) -> DeepSeekAdapterConfig:
        values: dict[str, object] = {
            "enabled": True,
            "timeout_seconds": 2.5,
            "max_retries": 0,
        }
        values.update(changes)
        return DeepSeekAdapterConfig(**values)  # type: ignore[arg-type]

    def test_default_off_never_calls_transport_and_returns_safe_failure(self) -> None:
        transport = FakeDeepSeekTransport([self.success_raw()])
        adapter = DeepSeekProviderAdapter(transport)

        result = adapter.route(self.route_input)

        self.assertFalse(adapter.provider_ready)
        self.assertEqual(transport.calls, [])
        self.assertEqual(result.router.status, "unavailable")
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})

    def test_enabled_fake_transport_reuses_existing_raw_parser_and_metadata(self) -> None:
        transport = FakeDeepSeekTransport([self.success_raw()])
        adapter = DeepSeekProviderAdapter(
            transport,
            config=self.enabled_config(model_version="fixture-v1"),
        )

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.provider, "deepseek-official")
        self.assertEqual(result.router.model_id, "deepseek-chat")
        self.assertEqual(result.router.model_version, "fixture-v1")
        self.assertEqual(result.route, "task")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], 2.5)
        self.assertEqual(transport.calls[0][0].raw_text, self.trace.raw_text)

    def test_retry_is_bounded_and_only_classified_transport_failures_retry(self) -> None:
        transport = FakeDeepSeekTransport([
            TimeoutError("secret timeout detail"),
            ConnectionError("secret connection detail"),
            self.success_raw(),
        ])
        adapter = DeepSeekProviderAdapter(
            transport,
            config=self.enabled_config(max_retries=2),
        )

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.status, "success")
        self.assertEqual(len(transport.calls), 3)

    def test_exhausted_timeout_is_normalized_without_exception_text(self) -> None:
        transport = FakeDeepSeekTransport([
            TimeoutError("API key=secret must not escape"),
            TimeoutError("second secret"),
        ])
        adapter = DeepSeekProviderAdapter(
            transport,
            config=self.enabled_config(max_retries=1),
        )

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.status, "timeout")
        self.assertNotIn("secret", repr(result))
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})

    def test_invalid_raw_response_does_not_retry_or_leak_payload(self) -> None:
        transport = FakeDeepSeekTransport(["Here is the JSON: {\"secret\": true}"])
        adapter = DeepSeekProviderAdapter(
            transport,
            config=self.enabled_config(max_retries=3),
        )

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.status, "invalid_response")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("secret", repr(result))

    def test_low_confidence_response_is_fail_closed(self) -> None:
        transport = FakeDeepSeekTransport([
            self.success_raw(confidence=0.79),
        ])
        adapter = DeepSeekProviderAdapter(transport, config=self.enabled_config())

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.status, "low_confidence")
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})
        self.assertEqual(result.confidence, 0.79)

    def test_request_payload_is_fresh_and_credential_free(self) -> None:
        request = DeepSeekRequest(
            raw_text="打开计算器",
            model_id="deepseek-chat",
            messages=(("system", "route"), ("user", "打开计算器")),
        )

        first = request.to_payload()
        first["messages"][1]["content"] = "changed"  # type: ignore[index]
        second = request.to_payload()

        self.assertEqual(second["messages"][1]["content"], "打开计算器")  # type: ignore[index]
        self.assertNotIn("api_key", first)
        self.assertNotIn("request_id", first)
        self.assertNotIn("conversation_id", first)

    def test_configuration_rejects_unallowlisted_origin_and_unbounded_retry(self) -> None:
        invalid_values = (
            {"base_url": "http://api.deepseek.com"},
            {"base_url": "https://api.deepseek.com:443"},
            {"base_url": "https://api.deepseek.com/v1"},
            {"base_url": "https://user:pass@api.deepseek.com"},
            {"base_url": "https://api.deepseek.com?key=secret"},
            {"base_url": "https://api.deepseek.com#fragment"},
            {"base_url": "https://not-allowed.example"},
            {"allowed_hosts": ("api.deepseek.com:443",)},
            {"allowed_hosts": ("api.deepseek.com example",)},
            {"allowed_hosts": []},
            {"allowed_hosts": ("",)},
            {"timeout_seconds": 0},
            {"timeout_seconds": -1},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": True},
            {"max_retries": -1},
            {"max_retries": 4},
            {"max_retries": True},
        )
        for changes in invalid_values:
            with self.subTest(changes=changes):
                with self.assertRaises(ContractError):
                    DeepSeekAdapterConfig(**changes)  # type: ignore[arg-type]

    def test_configuration_accepts_case_insensitive_allowlisted_origin_without_port(self) -> None:
        config = DeepSeekAdapterConfig(
            base_url="HTTPS://API.DEEPSEEK.COM/",
            allowed_hosts=("Api.DeepSeek.Com",),
        )

        self.assertEqual(config.timeout, 30.0)

    def test_retry_boundaries_are_exactly_zero_through_three_retries(self) -> None:
        no_retry_transport = FakeDeepSeekTransport([TimeoutError("secret")])
        no_retry_adapter = DeepSeekProviderAdapter(
            no_retry_transport,
            config=self.enabled_config(max_retries=0),
        )
        no_retry_result = no_retry_adapter.route(self.route_input)
        self.assertEqual(no_retry_result.router.status, "timeout")
        self.assertEqual(len(no_retry_transport.calls), 1)

        three_retry_transport = FakeDeepSeekTransport([
            TimeoutError("secret-1"),
            TimeoutError("secret-2"),
            TimeoutError("secret-3"),
            TimeoutError("secret-4"),
        ])
        three_retry_adapter = DeepSeekProviderAdapter(
            three_retry_transport,
            config=self.enabled_config(max_retries=3),
        )
        three_retry_result = three_retry_adapter.route(self.route_input)
        self.assertEqual(three_retry_result.router.status, "timeout")
        self.assertEqual(len(three_retry_transport.calls), 4)
        self.assertNotIn("secret", repr(three_retry_result))

    def test_unexpected_transport_error_is_not_retried_or_exposed(self) -> None:
        transport = FakeDeepSeekTransport([RuntimeError("secret internal detail")])
        adapter = DeepSeekProviderAdapter(
            transport,
            config=self.enabled_config(max_retries=3),
        )

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.status, "invalid_response")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("secret", repr(result))
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})

    def test_unavailable_transport_is_retried_but_remains_non_executable(self) -> None:
        from app.routing import RouterProviderUnavailable

        transport = FakeDeepSeekTransport([
            RouterProviderUnavailable("secret unavailable-1"),
            RouterProviderUnavailable("secret unavailable-2"),
        ])
        adapter = DeepSeekProviderAdapter(
            transport,
            config=self.enabled_config(max_retries=1),
        )

        result = adapter.route(self.route_input)

        self.assertEqual(result.router.status, "unavailable")
        self.assertEqual(len(transport.calls), 2)
        self.assertNotIn("secret", repr(result))
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})


if __name__ == "__main__":
    unittest.main()