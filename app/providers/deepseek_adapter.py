"""Offline-first DeepSeek provider adapter boundary.

This module intentionally contains no SDK, HTTP client, credential lookup, or
runtime wiring.  It follows the existing provider-neutral routing seam and
keeps the real provider-specific transport injectable for a separately
authorized phase.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from app.contracts.base import ContractError, require_identifier, require_non_empty
from app.routing import (
    ProviderIntentRouter,
    ProviderRequest,
    RouterProviderUnavailable,
    RouteInput,
    parse_deepseek_raw_route_response,
)


_DEFAULT_ALLOWED_HOSTS = ("api.deepseek.com",)
_RETRYABLE_TRANSPORT_ERRORS = (
    TimeoutError,
    RouterProviderUnavailable,
    ConnectionError,
    OSError,
)
_ROUTE_SYSTEM_PROMPT = (
    "You are a strict intent router. Return exactly one bare JSON object using the "
    "deepseek-route/v1 schema. "
    "Never wrap it in another object such as {\"response\": ...}, never use Markdown "
    "fences, and never add any text before or after the JSON. "
    "Required keys: schema_version, status, route, intent, handling_mode, confidence, "
    "slots, required_confirmation, tool_scope. "
    "\"schema_version\" must be exactly \"deepseek-route/v1\". "
    "\"status\" must be \"success\" or \"low_confidence\". "
    "\"route\", \"intent\" and \"handling_mode\" must be one matching triple from: "
    "chat -> general_chat -> chat_reply; "
    "query -> system_status|process_query|file_read|directory_listing|file_search|file_info "
    "-> read_only_tool; "
    "task -> open_application|file_operation|planning_request -> plan_then_execute; "
    "dangerous -> delete_file|overwrite_file -> plan_then_safety; "
    "ambiguous -> unknown|missing_target|low_confidence -> clarify; "
    "unsupported -> unsupported_request|policy_sensitive|unimplemented_capability -> reject. "
    "Questions, negations and hypotheticals are NOT unsupported tasks: when the utterance "
    "asks about something (contains 吗/？/怎么/为什么/能不能/可以吗), or is a negation or "
    "hypothesis (不要/不用/如果/假设), route it to chat instead. Use the unsupported route "
    "only for an explicit imperative command to do something the tool list does not allow. "
    "\"confidence\" must be a number in 0..1, and at least 0.80 whenever \"route\" is not "
    "\"ambiguous\". "
    "When the request cannot be routed confidently, return exactly one of these two shapes: "
    "(a) the minimal object {\"schema_version\":\"deepseek-route/v1\","
    "\"status\":\"low_confidence\",\"confidence\":<number in 0..1>} with no other keys; or "
    "(b) the full object with \"status\":\"success\", route \"ambiguous\", intent \"unknown\" "
    "or \"missing_target\", handling_mode \"clarify\", confidence at least 0.80, "
    "required_confirmation \"must_clarify\", slots {}, and tool_scope []. "
    "Never return \"status\":\"low_confidence\" together with route, intent, handling_mode, "
    "slots, required_confirmation or tool_scope. "
    "\"required_confirmation\" must be \"defer_to_safety\" for task and dangerous, "
    "\"must_clarify\" for ambiguous, and \"none\" otherwise. "
    "\"slots\" must be an object (possibly empty) whose keys are only application_name, "
    "source_path, destination_path, target_path, directory_path, file_pattern, operation, "
    "content; each value must be {\"value\": <string>, \"source\": "
    "\"utterance\"|\"normalization\"|\"classifier\", \"confidence\": <number in 0..1>, "
    "\"confirmed\": false}. "
    "\"tool_scope\" must be an array of tool names allowed for the intent "
    "(general_chat and planning_request allow none; system_status allows get_system_info; "
    "process_query allows get_processes; file_read allows read_text_file; directory_listing "
    "allows list_directory; file_search allows search_files; file_info allows get_file_info; "
    "open_application allows start_whitelisted_application; file_operation allows "
    "create_text_file|write_text_file|copy_file|move_file|rename_file; delete_file allows "
    "move_to_recycle_bin; overwrite_file allows write_text_file), and it must be [] for the "
    "chat, ambiguous and unsupported routes. "
    "If the request cannot be routed confidently, follow shape (a) or (b) above exactly. "
    "Example: {\"schema_version\":\"deepseek-route/v1\",\"status\":\"success\","
    "\"route\":\"task\",\"intent\":\"open_application\","
    "\"handling_mode\":\"plan_then_execute\",\"confidence\":0.95,"
    "\"slots\":{\"application_name\":{\"value\":\"calc\",\"source\":\"classifier\","
    "\"confidence\":0.9,\"confirmed\":false}},"
    "\"required_confirmation\":\"defer_to_safety\","
    "\"tool_scope\":[\"start_whitelisted_application\"]}"
)
_MAX_TOKENS_LIMIT = 4096
_THINKING_TYPES = ("disabled", "enabled")


def validate_https_origin(base_url: object, allowed_hosts: object) -> str:
    """校验 ``base_url`` 是无凭据/端口/查询的 HTTPS origin 且主机在白名单内。

    路由配置与对话传输共用同一套规则，避免两处漂移；返回小写主机名。
    错误消息保持与历史实现一致。
    """

    if (
        type(base_url) is not str
        or not base_url
        or any(char.isspace() for char in base_url)
    ):
        raise ContractError("DeepSeek base_url must be a non-empty URL")
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ContractError("DeepSeek base_url must be a valid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port is not None
    ):
        raise ContractError(
            "DeepSeek base_url must be an HTTPS origin without credentials, port, or query"
        )
    if parsed.path not in ("", "/") or not hostname:
        raise ContractError("DeepSeek base_url must be an HTTPS origin")
    if (
        not isinstance(allowed_hosts, tuple)
        or not allowed_hosts
        or any(
            type(host) is not str
            or not host
            or any(char.isspace() for char in host)
            or any(char in host for char in ("/", "\\", ":", "@"))
            for host in allowed_hosts
        )
    ):
        raise ContractError("DeepSeek allowed_hosts must be a non-empty hostname tuple")
    if hostname.lower() not in {host.lower() for host in allowed_hosts}:
        raise ContractError("DeepSeek base_url host is not allowlisted")
    return hostname.lower()


def _validate_max_tokens(value: int | None) -> None:
    """``None`` 表示请求体不发送 ``max_tokens``；显式值必须是 1..4096 的整数。

    默认 ``None`` 保证既有离线契约与真实探测前的行为完全不变。
    """

    if value is None:
        return
    if type(value) is not int or not 1 <= value <= _MAX_TOKENS_LIMIT:
        raise ContractError(
            f"DeepSeek max_tokens must be an integer in 1..{_MAX_TOKENS_LIMIT}"
        )


def _validate_thinking(value: str | None) -> None:
    """``None`` 表示不发送 ``thinking`` 字段（沿用服务端默认）。

    服务端默认开启 thinking（effort=high），CoT 走 ``reasoning_content``；对需要裸 JSON
    输出的路由分类场景，可显式传 ``disabled``。
    """

    if value is None:
        return
    if value not in _THINKING_TYPES:
        raise ContractError("DeepSeek thinking must be 'disabled' or 'enabled'")


@dataclass(frozen=True, slots=True)
class DeepSeekAdapterConfig:
    """Validated, credential-free configuration for the offline adapter seam."""

    provider_name: str = "deepseek-official"
    model_id: str = "deepseek-chat"
    model_version: str = "unbound"
    base_url: str = "https://api.deepseek.com"
    enabled: bool = False
    authorized: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 0
    allowed_hosts: tuple[str, ...] = _DEFAULT_ALLOWED_HOSTS
    max_tokens: int | None = None
    thinking: str | None = None

    def __post_init__(self) -> None:
        require_identifier("provider_name", self.provider_name)
        require_identifier("model_id", self.model_id)
        require_identifier("model_version", self.model_version)
        if type(self.enabled) is not bool:
            raise ContractError("DeepSeek enabled must be a bool")
        if type(self.authorized) is not bool:
            raise ContractError("DeepSeek authorized must be a bool")
        if self.authorized and not self.enabled:
            raise ContractError("DeepSeek authorized requires enabled")
        _validate_max_tokens(self.max_tokens)
        _validate_thinking(self.thinking)
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ContractError("DeepSeek timeout_seconds must be positive and finite")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 3:
            raise ContractError("DeepSeek max_retries must be an integer in 0..3")
        if (
            type(self.base_url) is not str
            or not self.base_url
            or any(char.isspace() for char in self.base_url)
        ):
            raise ContractError("DeepSeek base_url must be a non-empty URL")
        validate_https_origin(self.base_url, self.allowed_hosts)

    @property
    def timeout(self) -> float:
        return float(self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class DeepSeekRequest:
    """Immutable provider payload description with no trace IDs or credentials."""

    raw_text: str
    model_id: str
    messages: tuple[tuple[str, str], ...]
    response_format: str = "json_object"
    max_tokens: int | None = None
    thinking: str | None = None

    def __post_init__(self) -> None:
        require_non_empty("DeepSeekRequest.raw_text", self.raw_text, maximum=32768)
        require_identifier("DeepSeekRequest.model_id", self.model_id)
        if type(self.response_format) is not str or self.response_format != "json_object":
            raise ContractError("DeepSeek response_format must be json_object")
        _validate_max_tokens(self.max_tokens)
        _validate_thinking(self.thinking)
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ContractError("DeepSeek messages must be a non-empty tuple")
        for message in self.messages:
            if (
                not isinstance(message, tuple)
                or len(message) != 2
                or type(message[0]) is not str
                or type(message[1]) is not str
                or not message[0]
                or not message[1]
            ):
                raise ContractError("DeepSeek messages must contain string role/content pairs")

    @classmethod
    def from_provider_request(
        cls,
        request: ProviderRequest,
        *,
        model_id: str,
        max_tokens: int | None = None,
        thinking: str | None = None,
    ) -> "DeepSeekRequest":
        if not isinstance(request, ProviderRequest):
            raise ContractError("DeepSeek provider request must be ProviderRequest")
        return cls(
            raw_text=request.raw_text,
            model_id=model_id,
            messages=(
                ("system", _ROUTE_SYSTEM_PROMPT),
                ("user", request.raw_text),
            ),
            max_tokens=max_tokens,
            thinking=thinking,
        )

    def to_payload(self) -> dict[str, object]:
        """Return a fresh OpenAI-compatible payload-shaped mapping."""

        payload: dict[str, object] = {
            "model": self.model_id,
            "messages": [
                {"role": role, "content": content}
                for role, content in self.messages
            ],
            "response_format": {"type": self.response_format},
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        return payload


class DeepSeekTransport(Protocol):
    """Injected transport seam; real HTTP/SDK implementations are out of scope."""

    def send(self, request: DeepSeekRequest, *, timeout_seconds: float) -> str:
        """Return one bare DeepSeek route JSON document or raise a classified error."""
        ...


class DeepSeekProviderAdapter:
    """Default-off adapter that reuses the existing provider-neutral router."""

    def __init__(
        self,
        transport: DeepSeekTransport,
        *,
        config: DeepSeekAdapterConfig | None = None,
    ) -> None:
        if not callable(getattr(transport, "send", None)):
            raise TypeError("DeepSeek transport must expose send()")
        self._transport = transport
        self._config = config or DeepSeekAdapterConfig()
        self._router = ProviderIntentRouter(
            self,
            provider_name=self._config.provider_name,
            model_id=self._config.model_id,
            model_version=self._config.model_version,
            timeout_seconds=self._config.timeout,
        )

    @property
    def config(self) -> DeepSeekAdapterConfig:
        return self._config

    @property
    def provider_ready(self) -> bool:
        """显式 ``enabled`` 且已由配置授权（``authorized``）时才声明就绪。

        默认两者皆为 ``False``，因此未配置时的行为与既有 NOT READY 完全一致。
        """

        return bool(self._config.enabled and self._config.authorized)

    def route(self, request: RouteInput):
        return self._router.route(request)

    def classify(self, request: ProviderRequest) -> object:
        if not self._config.enabled:
            raise RouterProviderUnavailable("DeepSeek adapter is disabled")
        deepseek_request = DeepSeekRequest.from_provider_request(
            request,
            model_id=self._config.model_id,
            max_tokens=self._config.max_tokens,
            thinking=self._config.thinking,
        )
        attempts = self._config.max_retries + 1
        for attempt in range(attempts):
            try:
                raw_response = self._transport.send(
                    deepseek_request,
                    timeout_seconds=self._config.timeout,
                )
                if type(raw_response) is not str:
                    raise ValueError("DeepSeek transport must return text")
                return parse_deepseek_raw_route_response(raw_response)
            except _RETRYABLE_TRANSPORT_ERRORS:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("unreachable retry loop")


__all__ = [
    "DeepSeekAdapterConfig",
    "DeepSeekProviderAdapter",
    "DeepSeekRequest",
    "DeepSeekTransport",
]