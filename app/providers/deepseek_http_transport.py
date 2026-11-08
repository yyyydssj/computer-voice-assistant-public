"""Isolated, credential-injected DeepSeek HTTPS transport.

This module is deliberately separate from the default runtime.  It reads one
API key from the current process environment only when ``send`` is called,
performs one HTTP request per call, and returns only the provider message
content to the existing strict route parser.
"""

from __future__ import annotations

import json
import math
import os
import socket
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.contracts.base import ContractError
from app.routing import RouterProviderUnavailable
from app.providers.deepseek_adapter import (
    DeepSeekAdapterConfig,
    DeepSeekRequest,
)


_MAX_RESPONSE_BYTES = 1_048_576
_DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429})


class DeepSeekHTTPResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes:
        ...

    def close(self) -> None:
        ...


class DeepSeekHTTPTransport:
    """Send one OpenAI-compatible DeepSeek chat request over HTTPS.

    The opener is injectable for offline tests.  No SDK, dotenv loader, logger,
    retry loop, or runtime wiring is owned by this class.
    """

    def __init__(
        self,
        *,
        config: DeepSeekAdapterConfig | None = None,
        api_key_env: str = _DEFAULT_API_KEY_ENV,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self._config = config or DeepSeekAdapterConfig()
        if type(api_key_env) is not str or not api_key_env or any(char.isspace() for char in api_key_env):
            raise ContractError("DeepSeek API key environment name must be non-empty")
        self._api_key_env = api_key_env
        self._opener = opener or urlopen

    @property
    def config(self) -> DeepSeekAdapterConfig:
        return self._config

    def send(self, request: DeepSeekRequest, *, timeout_seconds: float) -> str:
        if not isinstance(request, DeepSeekRequest):
            raise ContractError("DeepSeek HTTP request must be DeepSeekRequest")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ContractError("DeepSeek HTTP timeout must be positive and finite")

        api_key = os.environ.get(self._api_key_env)
        if type(api_key) is not str or not api_key.strip():
            raise ValueError("DeepSeek API key is unavailable")

        endpoint = self._config.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(
            request.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        http_request = Request(
            endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        response: DeepSeekHTTPResponse | None = None
        try:
            response = self._opener(http_request, timeout=float(timeout_seconds))  # type: ignore[assignment]
            status = getattr(response, "status", None)
            if type(status) is int and not 200 <= status < 300:
                self._raise_http_status(status)
            raw_body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            self._raise_http_status(exc.code)
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("DeepSeek request timed out") from None
            raise ConnectionError("DeepSeek connection failed") from None
        finally:
            if response is not None:
                response.close()

        if not isinstance(raw_body, (bytes, bytearray)):
            raise ValueError("DeepSeek response body is invalid")
        if len(raw_body) > _MAX_RESPONSE_BYTES:
            raise ValueError("DeepSeek response body is too large")
        try:
            envelope = json.loads(bytes(raw_body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("DeepSeek response is not valid JSON") from None
        return self._extract_message_content(envelope)

    @staticmethod
    def _raise_http_status(status: int) -> None:
        if status in _RETRYABLE_HTTP_STATUS_CODES or status >= 500:
            raise RouterProviderUnavailable("DeepSeek upstream unavailable")
        raise ValueError("DeepSeek HTTP response rejected")

    @staticmethod
    def _extract_message_content(envelope: object) -> str:
        if not isinstance(envelope, Mapping):
            raise ValueError("DeepSeek response envelope is invalid")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("DeepSeek response choices are invalid")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("DeepSeek response choice is invalid")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("DeepSeek response message is invalid")
        content = message.get("content")
        if type(content) is not str or not content.strip():
            raise ValueError("DeepSeek response content is invalid")
        return content


__all__ = ["DeepSeekHTTPTransport"]
