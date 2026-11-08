from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.contracts import ContractError
from app.providers.deepseek_chat_transport import (
    DeepSeekChatConfig,
    DeepSeekChatRateLimited,
    DeepSeekChatRequest,
    DeepSeekChatTransport,
    DeepSeekChatUnavailable,
)

_API_KEY_ENV = "DEEPSEEK_API_KEY"
_FAKE_KEY = "fake-key-must-not-leak"


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


def ok_body(content: str = "你好，我是小饼。") -> bytes:
    return json.dumps(
        {
            "id": "chat-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def make_opener(response: object):
    calls: list[object] = []

    def opener(request: object, *, timeout: float) -> object:
        calls.append(request)
        if isinstance(response, BaseException):
            raise response
        return response

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


class DeepSeekChatConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_and_authorization_requires_enabled(self) -> None:
        config = DeepSeekChatConfig()

        self.assertFalse(config.enabled)
        self.assertFalse(config.authorized)
        self.assertEqual(config.model_id, "deepseek-flash")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.allowed_hosts, ("api.deepseek.com",))
        self.assertEqual(config.max_tokens, 512)
        self.assertEqual(config.thinking, "disabled")
        self.assertEqual(config.max_retries, 0)
        with self.assertRaisesRegex(ContractError, "authorized requires enabled"):
            DeepSeekChatConfig(enabled=False, authorized=True)

    def test_origin_rules_match_the_route_adapter(self) -> None:
        invalid_values = (
            {"base_url": "http://api.deepseek.com"},
            {"base_url": "https://api.deepseek.com:443"},
            {"base_url": "https://api.deepseek.com/v1"},
            {"base_url": "https://user:pass@api.deepseek.com"},
            {"base_url": "https://api.deepseek.com?key=secret"},
            {"base_url": "https://not-allowed.example"},
            {"allowed_hosts": ("api.deepseek.com:443",)},
            {"allowed_hosts": []},
        )
        for changes in invalid_values:
            with self.subTest(changes=changes):
                with self.assertRaises(ContractError):
                    DeepSeekChatConfig(**changes)  # type: ignore[arg-type]

    def test_numeric_and_thinking_rules_are_strict(self) -> None:
        invalid_values = (
            {"max_tokens": None},
            {"max_tokens": 0},
            {"max_tokens": 4097},
            {"max_tokens": True},
            {"thinking": "off"},
            {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")},
            {"max_retries": -1},
            {"max_retries": 4},
            {"system_prompt": ""},
        )
        for changes in invalid_values:
            with self.subTest(changes=changes):
                with self.assertRaises(ContractError):
                    DeepSeekChatConfig(**changes)  # type: ignore[arg-type]


class DeepSeekChatRequestTests(unittest.TestCase):
    def test_payload_carries_messages_max_tokens_and_thinking_without_json_mode(self) -> None:
        config = DeepSeekChatConfig(enabled=True, authorized=True)
        request = DeepSeekChatRequest.from_config(config, "今天天气怎么样？")

        payload = request.to_payload()

        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("response_format", payload)
        messages = payload["messages"]
        self.assertEqual([item["role"] for item in messages], ["system", "user"])  # type: ignore[index]
        self.assertEqual(messages[1]["content"], "今天天气怎么样？")  # type: ignore[index]
        self.assertIn("小饼", messages[0]["content"])  # type: ignore[index]
        self.assertNotIn(_FAKE_KEY, json.dumps(payload, ensure_ascii=False))

    def test_payload_is_fresh_each_time(self) -> None:
        config = DeepSeekChatConfig(enabled=True)
        request = DeepSeekChatRequest.from_config(config, "你好")

        first = request.to_payload()
        first["messages"][1]["content"] = "changed"  # type: ignore[index]
        second = request.to_payload()

        self.assertEqual(second["messages"][1]["content"], "你好")  # type: ignore[index]

    def test_empty_user_text_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            DeepSeekChatRequest.from_config(DeepSeekChatConfig(enabled=True), "   ")


class DeepSeekChatTransportTests(unittest.TestCase):
    def config(self, **changes: object) -> DeepSeekChatConfig:
        values: dict[str, object] = {"enabled": True, "authorized": True}
        values.update(changes)
        return DeepSeekChatConfig(**values)  # type: ignore[arg-type]

    def send(self, opener: object, **changes: object) -> str:
        config = self.config(**changes)
        transport = DeepSeekChatTransport(config=config, opener=opener)  # type: ignore[arg-type]
        request = DeepSeekChatRequest.from_config(config, "你好")
        with patch.dict(os.environ, {_API_KEY_ENV: _FAKE_KEY}, clear=False):
            return transport.send(request, timeout_seconds=config.timeout)

    def test_successful_call_returns_assistant_text_and_closes_response(self) -> None:
        response = FakeResponse(ok_body("你好，我是小饼。"))
        opener = make_opener(response)

        text = self.send(opener)

        self.assertEqual(text, "你好，我是小饼。")
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 1)  # type: ignore[attr-defined]
        request = opener.calls[0]  # type: ignore[attr-defined]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["content-type"], "application/json")
        self.assertIn(_FAKE_KEY, headers["authorization"])

    def test_missing_credential_fails_closed_without_any_request(self) -> None:
        opener = make_opener(FakeResponse(ok_body()))
        config = self.config()
        transport = DeepSeekChatTransport(config=config, opener=opener)  # type: ignore[arg-type]
        request = DeepSeekChatRequest.from_config(config, "你好")
        environment = dict(os.environ)
        environment.pop(_API_KEY_ENV, None)

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(DeepSeekChatUnavailable) as caught:
                transport.send(request, timeout_seconds=config.timeout)

        self.assertEqual(opener.calls, [])  # type: ignore[attr-defined]
        self.assertNotIn(_FAKE_KEY, str(caught.exception))

    def test_rate_limit_and_unavailable_statuses_are_classified(self) -> None:
        for status, expected in (
            (429, DeepSeekChatRateLimited),
            (500, DeepSeekChatUnavailable),
            (503, DeepSeekChatUnavailable),
            (408, DeepSeekChatUnavailable),
        ):
            with self.subTest(status=status):
                opener = make_opener(HTTPError("https://api.deepseek.com", status, "err", {}, None))
                with self.assertRaises(expected):
                    self.send(opener)

    def test_rejected_status_is_a_value_error_without_leaking_body(self) -> None:
        opener = make_opener(HTTPError("https://api.deepseek.com", 400, "bad", {}, None))

        with self.assertRaises(ValueError) as caught:
            self.send(opener)

        self.assertNotIn(_FAKE_KEY, str(caught.exception))

    def test_timeout_and_connection_errors_are_classified(self) -> None:
        with self.assertRaises(TimeoutError):
            self.send(make_opener(URLError(TimeoutError("timed out"))))
        with self.assertRaises(DeepSeekChatUnavailable):
            self.send(make_opener(URLError(ConnectionRefusedError("refused"))))

    def test_invalid_or_incomplete_bodies_are_rejected(self) -> None:
        invalid_bodies = (
            b"not json",
            json.dumps({"choices": []}).encode("utf-8"),
            json.dumps({"choices": [{}]}).encode("utf-8"),
            json.dumps({"choices": [{"message": {}}]}).encode("utf-8"),
            json.dumps({"choices": [{"message": {"content": 5}}]}).encode("utf-8"),
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.send(make_opener(FakeResponse(body)))

    def test_non_200_inline_status_is_classified(self) -> None:
        opener = make_opener(FakeResponse(b"{}", status=503))

        with self.assertRaises(DeepSeekChatUnavailable):
            self.send(opener)


if __name__ == "__main__":
    unittest.main()
