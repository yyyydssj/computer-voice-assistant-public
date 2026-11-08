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
                "confidence": 0.93,
                "slots": {},
                "required_confirmation": "none",
                "tool_scope": [],
            },
            ensure_ascii=False,
        )


def make_request(**changes: object) -> DeepSeekRequest:
    values: dict[str, object] = {
        "raw_text": "你好",
        "model_id": "deepseek-flash",
        "messages": (("system", "route"), ("user", "你好")),
    }
    values.update(changes)
    return DeepSeekRequest(**values)  # type: ignore[arg-type]


class DeepSeekThinkingModeTests(unittest.TestCase):
    def trace(self) -> RouteTrace:
        return RouteTrace(
            request_id="request-thinking",
            conversation_id="conversation-thinking",
            turn_id="turn-thinking",
            source="text_shell",
            raw_text="你好",
            created_at="2026-09-30T10:00:00Z",
        )

    def test_default_omits_thinking_field(self) -> None:
        payload = make_request().to_payload()

        self.assertNotIn("thinking", payload)
        self.assertIsNone(make_request().thinking)

    def test_disabled_and_enabled_are_sent_as_nested_type(self) -> None:
        for mode in ("disabled", "enabled"):
            with self.subTest(mode=mode):
                payload = make_request(thinking=mode).to_payload()
                self.assertEqual(payload["thinking"], {"type": mode})

    def test_thinking_combines_with_max_tokens(self) -> None:
        payload = make_request(thinking="disabled", max_tokens=256).to_payload()

        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["max_tokens"], 256)

    def test_config_thinking_reaches_transport_request(self) -> None:
        transport = RecordingTransport()
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(
                enabled=True,
                max_tokens=256,
                thinking="disabled",
            ),
        )

        result = adapter.route(RouteInput(raw_text="你好", trace=self.trace()))

        self.assertEqual(result.router.status, "success")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0].thinking, "disabled")
        self.assertEqual(
            transport.requests[0].to_payload()["thinking"],
            {"type": "disabled"},
        )

    def test_config_without_thinking_keeps_payload_clean(self) -> None:
        transport = RecordingTransport()
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(enabled=True),
        )

        adapter.route(RouteInput(raw_text="你好", trace=self.trace()))

        self.assertIsNone(transport.requests[0].thinking)
        self.assertNotIn("thinking", transport.requests[0].to_payload())

    def test_invalid_thinking_values_are_rejected(self) -> None:
        invalid_values = ("off", "on", "", "DISABLED", True, 1, ["disabled"])
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    make_request(thinking=value)
                with self.assertRaises(ContractError):
                    DeepSeekAdapterConfig(thinking=value)  # type: ignore[arg-type]

    def test_from_provider_request_propagates_thinking(self) -> None:
        from app.routing import ProviderRequest

        request = DeepSeekRequest.from_provider_request(
            ProviderRequest(raw_text="你好", trace=self.trace()),
            model_id="deepseek-flash",
            thinking="disabled",
        )

        self.assertEqual(request.thinking, "disabled")
        self.assertEqual(request.to_payload()["thinking"], {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()
