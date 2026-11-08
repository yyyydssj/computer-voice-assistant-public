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


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[DeepSeekRequest] = []

    def send(self, request: DeepSeekRequest, *, timeout_seconds: float) -> str:
        self.requests.append(request)
        return json.dumps(
            {
                "schema_version": "deepseek-route/v1",
                "status": "success",
                "route": "chat",
                "intent": "general_chat",
                "handling_mode": "chat_reply",
                "confidence": 0.91,
                "slots": {},
                "required_confirmation": "none",
                "tool_scope": [],
            },
            ensure_ascii=False,
        )


class DeepSeekMaxTokensTests(unittest.TestCase):
    def trace(self) -> RouteTrace:
        return RouteTrace(
            request_id="request-max-tokens",
            conversation_id="conversation-max-tokens",
            turn_id="turn-max-tokens",
            source="text_shell",
            raw_text="你好",
            created_at="2026-09-30T10:00:00Z",
        )

    def test_absent_max_tokens_keeps_legacy_payload_shape(self) -> None:
        request = DeepSeekRequest(
            raw_text="你好",
            model_id="deepseek-flash",
            messages=(("system", "route"), ("user", "你好")),
        )

        payload = request.to_payload()

        self.assertNotIn("max_tokens", payload)
        self.assertEqual(
            sorted(payload),
            ["messages", "model", "response_format"],
        )
        self.assertIsNone(request.max_tokens)

    def test_explicit_max_tokens_is_sent_in_payload(self) -> None:
        request = DeepSeekRequest(
            raw_text="你好",
            model_id="deepseek-flash",
            messages=(("system", "route"), ("user", "你好")),
            max_tokens=256,
        )

        first = request.to_payload()
        second = request.to_payload()

        self.assertEqual(first["max_tokens"], 256)
        self.assertIsNot(first, second)
        self.assertEqual(second["max_tokens"], 256)

    def test_config_max_tokens_reaches_transport_request(self) -> None:
        transport = RecordingTransport()
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(enabled=True, max_tokens=256),
        )

        result = adapter.route(RouteInput(raw_text="你好", trace=self.trace()))

        self.assertEqual(result.router.status, "success")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0].max_tokens, 256)
        self.assertEqual(transport.requests[0].to_payload()["max_tokens"], 256)

    def test_config_without_max_tokens_leaves_request_unbounded_by_field(self) -> None:
        transport = RecordingTransport()
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(enabled=True),
        )

        adapter.route(RouteInput(raw_text="你好", trace=self.trace()))

        self.assertIsNone(transport.requests[0].max_tokens)
        self.assertNotIn("max_tokens", transport.requests[0].to_payload())

    def test_invalid_max_tokens_values_are_rejected(self) -> None:
        invalid_values = (0, -1, 4097, True, 1.5, "256", [], 10**9)
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    DeepSeekRequest(
                        raw_text="你好",
                        model_id="deepseek-flash",
                        messages=(("system", "route"), ("user", "你好")),
                        max_tokens=value,  # type: ignore[arg-type]
                    )
                with self.assertRaises(ContractError):
                    DeepSeekAdapterConfig(max_tokens=value)  # type: ignore[arg-type]

    def test_boundary_values_one_and_limit_are_accepted(self) -> None:
        for value in (1, 4096):
            with self.subTest(value=value):
                request = DeepSeekRequest(
                    raw_text="你好",
                    model_id="deepseek-flash",
                    messages=(("system", "route"), ("user", "你好")),
                    max_tokens=value,
                )
                self.assertEqual(request.to_payload()["max_tokens"], value)

    def test_from_provider_request_propagates_max_tokens(self) -> None:
        from app.routing import ProviderRequest

        request = DeepSeekRequest.from_provider_request(
            ProviderRequest(raw_text="你好", trace=self.trace()),
            model_id="deepseek-flash",
            max_tokens=128,
        )

        self.assertEqual(request.max_tokens, 128)
        self.assertEqual(request.to_payload()["max_tokens"], 128)


if __name__ == "__main__":
    unittest.main()
