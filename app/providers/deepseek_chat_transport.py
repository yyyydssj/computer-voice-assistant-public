"""DeepSeek Chat Completions 对话传输（与路由 transport 有意分离）。

本模块只做一件事：把一段用户文本发给 DeepSeek 对话接口并取回助手文本。

硬边界：

- 不含 SDK、dotenv、日志、重试循环或运行时接线；
- 只在 ``send`` 时从当前进程环境读取一个 API key，绝不打印或落盘；
- 每次调用只发一个 HTTPS 请求，单次超时由调用方给出；
- 对话**不使用** ``response_format=json_object``（那是路由协议专用）；
- 失败一律枚举化（限流 / 不可用 / 拒绝 / 非法响应），绝不携带上游正文或凭据。
"""

from __future__ import annotations

import json
import math
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.contracts.base import ContractError, require_identifier, require_non_empty
from app.providers.deepseek_adapter import (
    _validate_max_tokens,
    _validate_thinking,
    validate_https_origin,
)

_MAX_RESPONSE_BYTES = 1_048_576
_MAX_SYSTEM_PROMPT_LENGTH = 131_072
_DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
_DEFAULT_ALLOWED_HOSTS = ("api.deepseek.com",)
_RATE_LIMIT_STATUS = 429
_UNAVAILABLE_STATUS_CODES = frozenset({408, 425, 500, 502, 503, 504})

DEFAULT_SYSTEM_PROMPT = (
    "你是「小饼」，一个运行在用户本机的简体中文语音助手。"
    "请用简体中文、简洁、直接地回答；不确定就说不确定，不要编造事实、链接或数据。"
    "你没有工具权限，也不能执行电脑操作；若用户要求执行操作，只说明你会先提出计划并等待确认。"
)
"""**内置兜底** system prompt（2026-10-24 起人设改为文件驱动）。

正常运行时 `persona` 来自人物模板文件（默认 `implementation_plan/persona.md`，可用
`--persona` / `ASSISTANT_PERSONA_PATH` 换），本常量只在**找不到人物模板**时使用，
并在启动提示与 `config_checked` 里以 `persona_source=builtin` 如实暴露。
"""

# 兼容既有私有引用；语义与公开常量完全相同。
_DEFAULT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT


class DeepSeekChatError(RuntimeError):
    """对话传输的枚举化失败基类。"""


class DeepSeekChatRateLimited(DeepSeekChatError):
    """上游限流（HTTP 429）。"""


class DeepSeekChatUnavailable(DeepSeekChatError):
    """上游暂时不可用（408/5xx/连接失败）。"""


@dataclass(frozen=True, slots=True)
class DeepSeekChatConfig:
    """经过校验的对话配置；``enabled`` 与 ``authorized`` 默认皆为 False。"""

    provider_name: str = "deepseek-official"
    model_id: str = "deepseek-flash"
    model_version: str = "chat-2026-09-30"
    base_url: str = "https://api.deepseek.com"
    enabled: bool = False
    authorized: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 0
    max_tokens: int = 512
    thinking: str | None = "disabled"
    allowed_hosts: tuple[str, ...] = _DEFAULT_ALLOWED_HOSTS
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT

    def __post_init__(self) -> None:
        require_identifier("provider_name", self.provider_name)
        require_identifier("model_id", self.model_id)
        require_identifier("model_version", self.model_version)
        if type(self.enabled) is not bool:
            raise ContractError("DeepSeek chat enabled must be a bool")
        if type(self.authorized) is not bool:
            raise ContractError("DeepSeek chat authorized must be a bool")
        if self.authorized and not self.enabled:
            raise ContractError("DeepSeek chat authorized requires enabled")
        if self.max_tokens is None:
            raise ContractError("DeepSeek chat max_tokens is required")
        _validate_max_tokens(self.max_tokens)
        _validate_thinking(self.thinking)
        if (
            type(self.timeout_seconds) not in (int, float)
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ContractError("DeepSeek chat timeout_seconds must be positive and finite")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 3:
            raise ContractError("DeepSeek chat max_retries must be an integer in 0..3")
        validate_https_origin(self.base_url, self.allowed_hosts)
        require_non_empty(
            "DeepSeek chat system_prompt",
            self.system_prompt,
            maximum=_MAX_SYSTEM_PROMPT_LENGTH,
        )

    @property
    def timeout(self) -> float:
        return float(self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class DeepSeekChatRequest:
    """一次对话请求的不可变描述（不含任何凭据或追踪标识）。"""

    model_id: str
    system_prompt: str
    user_text: str
    max_tokens: int
    thinking: str | None = "disabled"

    def __post_init__(self) -> None:
        require_identifier("DeepSeekChatRequest.model_id", self.model_id)
        require_non_empty(
            "DeepSeekChatRequest.system_prompt",
            self.system_prompt,
            maximum=_MAX_SYSTEM_PROMPT_LENGTH,
        )
        require_non_empty("DeepSeekChatRequest.user_text", self.user_text, maximum=32768)
        if not self.user_text.strip():
            raise ContractError("DeepSeekChatRequest.user_text must not be blank")
        if self.max_tokens is None:
            raise ContractError("DeepSeekChatRequest.max_tokens is required")
        _validate_max_tokens(self.max_tokens)
        _validate_thinking(self.thinking)

    @classmethod
    def from_config(
        cls, config: DeepSeekChatConfig, user_text: str
    ) -> "DeepSeekChatRequest":
        if not isinstance(config, DeepSeekChatConfig):
            raise ContractError("config must be a DeepSeekChatConfig")
        return cls(
            model_id=config.model_id,
            system_prompt=config.system_prompt,
            user_text=user_text,
            max_tokens=config.max_tokens,
            thinking=config.thinking,
        )

    def to_payload(self) -> dict[str, object]:
        """返回全新的、不含凭据的 OpenAI 兼容请求体。"""

        payload: dict[str, object] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self.user_text},
            ],
            "max_tokens": self.max_tokens,
        }
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        return payload


class DeepSeekChatResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes:
        ...

    def close(self) -> None:
        ...


class DeepSeekChatTransport:
    """发送单次对话请求并返回助手文本；opener 可注入以便离线测试。"""

    def __init__(
        self,
        *,
        config: DeepSeekChatConfig | None = None,
        api_key_env: str = _DEFAULT_API_KEY_ENV,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self._config = config or DeepSeekChatConfig()
        if (
            type(api_key_env) is not str
            or not api_key_env
            or any(char.isspace() for char in api_key_env)
        ):
            raise ContractError("DeepSeek chat API key environment name must be non-empty")
        self._api_key_env = api_key_env
        self._opener = opener or urlopen

    @property
    def config(self) -> DeepSeekChatConfig:
        return self._config

    def send(self, request: DeepSeekChatRequest, *, timeout_seconds: float) -> str:
        if not isinstance(request, DeepSeekChatRequest):
            raise ContractError("DeepSeek chat request must be DeepSeekChatRequest")
        if (
            type(timeout_seconds) not in (int, float)
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ContractError("DeepSeek chat timeout must be positive and finite")

        api_key = os.environ.get(self._api_key_env)
        if type(api_key) is not str or not api_key.strip():
            raise DeepSeekChatUnavailable("DeepSeek chat API key is unavailable")

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

        response: DeepSeekChatResponse | None = None
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
                raise TimeoutError("DeepSeek chat request timed out") from None
            raise DeepSeekChatUnavailable("DeepSeek chat connection failed") from None
        finally:
            if response is not None:
                response.close()

        if not isinstance(raw_body, (bytes, bytearray)):
            raise ValueError("DeepSeek chat response body is invalid")
        if len(raw_body) > _MAX_RESPONSE_BYTES:
            raise ValueError("DeepSeek chat response body is too large")
        try:
            envelope = json.loads(bytes(raw_body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("DeepSeek chat response is not valid JSON") from None
        return self._extract_message_content(envelope)

    @staticmethod
    def _raise_http_status(status: int) -> None:
        if status == _RATE_LIMIT_STATUS:
            raise DeepSeekChatRateLimited("DeepSeek chat upstream rate limited")
        if status in _UNAVAILABLE_STATUS_CODES or status >= 500:
            raise DeepSeekChatUnavailable("DeepSeek chat upstream unavailable")
        raise ValueError("DeepSeek chat HTTP response rejected")

    @staticmethod
    def _extract_message_content(envelope: object) -> str:
        if not isinstance(envelope, Mapping):
            raise ValueError("DeepSeek chat response envelope is invalid")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("DeepSeek chat response choices are invalid")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("DeepSeek chat response choice is invalid")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("DeepSeek chat response message is invalid")
        content = message.get("content")
        if type(content) is not str:
            raise ValueError("DeepSeek chat response content is invalid")
        return content


__all__ = [
    "DeepSeekChatConfig",
    "DeepSeekChatError",
    "DeepSeekChatRateLimited",
    "DeepSeekChatRequest",
    "DeepSeekChatTransport",
    "DeepSeekChatUnavailable",
]
