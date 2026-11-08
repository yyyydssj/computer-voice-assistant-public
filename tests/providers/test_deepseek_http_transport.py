from __future__ import annotations

import json
import os
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from app.contracts import ContractError
from app.providers.deepseek_adapter import DeepSeekAdapterConfig, DeepSeekRequest
from app.providers.deepseek_http_transport import DeepSeekHTTPTransport
from app.routing import RouterProviderUnavailable


class FakeHTTPResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.payload if amount < 0 else self.payload[:amount]

    def close(self) -> None:
        self.closed = True


class DeepSeekHTTPTransportTests(unittest.TestCase):
    def request(self) -> DeepSeekRequest:
        return DeepSeekRequest(
            raw_text="固定 preflight 探针",
            model_id="deepseek-chat",
            messages=(("system", "route"), ("user", "固定 preflight 探针")),
        )

    def envelope(self, content: str) -> bytes:
        return json.dumps(
            {"choices": [{"message": {"content": content}}]},
            ensure_ascii=False,
        ).encode("utf-8")

    def test_constructs_one_credential_separated_post_request(self) -> None:
        calls: list[tuple[Request, float]] = []
        response = FakeHTTPResponse(self.envelope("{\"status\":\"unavailable\"}"))

        def opener(request: Request, *, timeout: float) -> FakeHTTPResponse:
            calls.append((request, timeout))
            return response

        transport = DeepSeekHTTPTransport(
            config=DeepSeekAdapterConfig(enabled=True),
            opener=opener,
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            result = transport.send(self.request(), timeout_seconds=2.5)

        self.assertEqual(result, '{"status":"unavailable"}')
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 2.5)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertNotIn("api_key", payload)
        self.assertNotIn("request_id", payload)
        self.assertNotIn("conversation_id", payload)
        self.assertNotIn("test-key", request.data.decode("utf-8"))
        self.assertTrue(response.closed)

    def test_reads_only_process_environment_and_fails_closed_when_missing(self) -> None:
        calls: list[object] = []
        transport = DeepSeekHTTPTransport(opener=lambda *args, **kwargs: calls.append(args) or None)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "API key is unavailable"):
                transport.send(self.request(), timeout_seconds=1.0)

        self.assertEqual(calls, [])

    def test_does_not_retry_inside_transport(self) -> None:
        calls = 0

        def opener(request: Request, *, timeout: float) -> object:
            nonlocal calls
            calls += 1
            raise URLError("network detail")

        transport = DeepSeekHTTPTransport(opener=opener)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with self.assertRaises(ConnectionError):
                transport.send(self.request(), timeout_seconds=1.0)

        self.assertEqual(calls, 1)

    def test_timeout_and_http_statuses_are_classified_without_body_leak(self) -> None:
        def timeout_opener(request: Request, *, timeout: float) -> object:
            raise URLError(TimeoutError("secret timeout"))

        timeout_transport = DeepSeekHTTPTransport(opener=timeout_opener)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with self.assertRaises(TimeoutError) as timeout_context:
                timeout_transport.send(self.request(), timeout_seconds=1.0)
        self.assertNotIn("secret", str(timeout_context.exception))

        def unavailable_opener(request: Request, *, timeout: float) -> object:
            headers = Message()
            raise HTTPError(request.full_url, 503, "secret upstream", headers, None)

        unavailable_transport = DeepSeekHTTPTransport(opener=unavailable_opener)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with self.assertRaises(RouterProviderUnavailable) as unavailable_context:
                unavailable_transport.send(self.request(), timeout_seconds=1.0)
        self.assertNotIn("secret", str(unavailable_context.exception))

        def rejected_opener(request: Request, *, timeout: float) -> object:
            headers = Message()
            raise HTTPError(request.full_url, 401, "secret unauthorized", headers, None)

        rejected_transport = DeepSeekHTTPTransport(opener=rejected_opener)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with self.assertRaises(ValueError) as rejected_context:
                rejected_transport.send(self.request(), timeout_seconds=1.0)
        self.assertNotIn("secret", str(rejected_context.exception))

    def test_rejects_invalid_timeout_and_oversized_or_invalid_envelopes(self) -> None:
        transport = DeepSeekHTTPTransport(
            opener=lambda request, timeout: FakeHTTPResponse(b"not-json")
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            for timeout in (0, -1, float("nan"), float("inf"), True):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(ContractError):
                        transport.send(self.request(), timeout_seconds=timeout)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                transport.send(self.request(), timeout_seconds=1.0)

        invalid = DeepSeekHTTPTransport(
            opener=lambda request, timeout: FakeHTTPResponse(json.dumps({"choices": []}).encode())
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            with self.assertRaises(ValueError):
                invalid.send(self.request(), timeout_seconds=1.0)


if __name__ == "__main__":
    unittest.main()