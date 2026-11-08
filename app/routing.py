"""P7-R-02/R-03: pure intent-router and failure-normalization boundaries."""

from __future__ import annotations

import json
import math

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from app.contracts.base import ContractError, require_identifier, require_non_empty
from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus, SlotCandidate


@dataclass(frozen=True, slots=True)
class RouteInput:
    """Validated input owned by the conversation boundary.

    The router receives the caller-owned trace and the exact raw text. It must
    not create, replace, or reinterpret business identifiers at this boundary.
    """

    raw_text: str
    trace: RouteTrace

    def __post_init__(self) -> None:
        require_non_empty("RouteInput.raw_text", self.raw_text, maximum=32768)
        if not isinstance(self.trace, RouteTrace):
            raise ContractError("RouteInput.trace 必须是 RouteTrace")
        if self.raw_text != self.trace.raw_text:
            raise ContractError("RouteInput.raw_text 必须与 trace.raw_text 一致")


_ROUTER_FAILURE_CODES = {
    "unavailable": "router_unavailable",
    "timeout": "router_unavailable",
    "transport_error": "router_unavailable",
    "invalid_response": "invalid_router_output",
    "low_confidence": "low_confidence",
}


def normalize_router_failure(
    trace: RouteTrace,
    *,
    provider: str,
    model_id: str,
    model_version: str,
    status: str,
    confidence: float = 0.0,
    error_text: str | None = None,
) -> RouteHandoff:
    """Convert a router/provider failure into one safe, non-executable handoff.

    This boundary accepts only an enum-like status and an optional confidence
    score. Provider exception text or raw payloads are deliberately rejected;
    they must never cross into the routing contract or user-visible flow.
    """

    if not isinstance(trace, RouteTrace):
        raise ContractError("normalize_router_failure.trace 必须是 RouteTrace")
    if error_text is not None:
        raise ContractError("不得向路由交接传递 Provider 异常正文")
    if type(status) is not str or status not in _ROUTER_FAILURE_CODES:
        raise ContractError("不支持的路由器失败状态")
    if status != "low_confidence" and confidence != 0.0:
        raise ContractError("非 low_confidence 失败不得携带分类置信度")

    failure_code = _ROUTER_FAILURE_CODES[status]
    return RouteHandoff(
        trace=trace,
        route="ambiguous",
        intent=failure_code,
        handling_mode="clarify",
        confidence=confidence,
        slots={},
        required_confirmation="must_clarify",
        tool_scope=(),
        router=RouterStatus(
            provider=provider,
            model_id=model_id,
            model_version=model_version,
            status=status,
            failure_code=failure_code,
        ),
    )


class IntentRouter(Protocol):
    """Pure route-selection interface; implementations must have no side effects."""

    def route(self, request: RouteInput) -> RouteHandoff:
        """Return exactly one validated routing handoff for the supplied input."""
        ...


__all__ = ["IntentRouter", "RouteInput", "normalize_router_failure", "parse_deepseek_raw_route_response", "parse_deepseek_route_response"]

class RouterProviderError(RuntimeError):
    """Base error for provider-side routing failures."""


class RouterProviderUnavailable(RouterProviderError):
    """The configured provider cannot currently serve routing requests."""


ProviderUnavailableError = RouterProviderUnavailable


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Provider-neutral request; no credentials or provider payload are carried."""

    raw_text: str
    trace: RouteTrace

    def __post_init__(self) -> None:
        require_non_empty("ProviderRequest.raw_text", self.raw_text, maximum=32768)
        if not isinstance(self.trace, RouteTrace):
            raise ContractError("ProviderRequest.trace must be RouteTrace")
        if self.raw_text != self.trace.raw_text:
            raise ContractError("ProviderRequest.raw_text must match trace.raw_text")


class RoutingProvider(Protocol):
    """Minimal provider boundary used by the adapter; implementations stay outside app contracts."""

    def classify(self, request: ProviderRequest) -> object:
        """Return a provider-neutral mapping or raise a classified provider exception."""
        ...


_DEEPSEEK_ROUTE_SCHEMA = "deepseek-route/v1"
_DEEPSEEK_SUCCESS_FIELDS = frozenset({
    "schema_version", "status", "route", "intent", "handling_mode", "confidence",
    "slots", "required_confirmation", "tool_scope",
})
_DEEPSEEK_FAILURE_STATUSES = frozenset({
    "timeout", "unavailable", "transport_error", "invalid_response", "low_confidence",
})


def _reject_deepseek_constant(value: str) -> None:
    raise ValueError("DeepSeek route JSON 不允许非标准数字")


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DeepSeek route JSON 不允许重复字段")
        result[key] = value
    return result


def parse_deepseek_route_response(payload: str) -> dict[str, Any]:
    """Parse the isolated DeepSeek route/v1 JSON envelope into a fresh mapping.

    The schema marker is consumed at this boundary. The existing
    ProviderIntentRouter then applies the provider-neutral RouteHandoff
    validation. This parser performs no network, SDK, logging, authorization,
    or execution work.
    """

    if not isinstance(payload, str):
        raise ValueError("DeepSeek route payload must be JSON text")
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_deepseek_constant,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("DeepSeek route JSON is invalid") from exc
    if not isinstance(decoded, dict) or any(type(key) is not str for key in decoded):
        raise ValueError("DeepSeek route response must be a JSON object")
    if decoded.get("schema_version") != _DEEPSEEK_ROUTE_SCHEMA:
        raise ValueError("DeepSeek route schema_version is unsupported")
    status = decoded.get("status")
    if type(status) is not str:
        raise ValueError("DeepSeek route status must be a string")
    if status in _DEEPSEEK_FAILURE_STATUSES:
        allowed = {"schema_version", "status"}
        if status == "low_confidence":
            allowed.add("confidence")
        if set(decoded) != allowed:
            raise ValueError("DeepSeek failure response has an unexpected schema")
        if status == "low_confidence":
            score = decoded["confidence"]
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("DeepSeek confidence must be a finite value in 0..1")
        return {key: value for key, value in decoded.items() if key != "schema_version"}
    if status != "success":
        raise ValueError("DeepSeek route status is unsupported")
    if set(decoded) != _DEEPSEEK_SUCCESS_FIELDS:
        raise ValueError("DeepSeek success response has an unexpected schema")
    for name in ("route", "intent", "handling_mode", "required_confirmation"):
        if type(decoded[name]) is not str:
            raise ValueError(f"DeepSeek {name} must be a string")
    confidence = decoded["confidence"]
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("DeepSeek confidence must be a finite value in 0..1")
    if not isinstance(decoded["slots"], dict) or any(type(key) is not str for key in decoded["slots"]):
        raise ValueError("DeepSeek slots must be a JSON object")
    tools = decoded["tool_scope"]
    if not isinstance(tools, list) or any(type(tool) is not str for tool in tools):
        raise ValueError("DeepSeek tool_scope must be a string array")
    return {key: value for key, value in decoded.items() if key != "schema_version"}



def parse_deepseek_raw_route_response(payload: str) -> dict[str, Any]:
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("DeepSeek raw route output must be non-empty JSON text")
    try:
        return parse_deepseek_route_response(payload)
    except (TypeError, ValueError):
        raise ValueError("DeepSeek raw route output is invalid") from None

class ProviderIntentRouter:
    """Adapt one provider into the strict, provider-neutral IntentRouter contract.

    The adapter owns provider metadata and trace identity. Provider responses are
    validated into a fresh RouteHandoff; raw payloads and exception text are never
    retained or returned. Timeout enforcement belongs to the provider boundary,
    which reports a TimeoutError to this adapter.
    """

    def __init__(
        self,
        provider: RoutingProvider | Callable[[ProviderRequest], object],
        *,
        provider_name: str,
        model_id: str,
        model_version: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not callable(provider) and not callable(getattr(provider, "classify", None)):
            raise TypeError("provider must be callable or expose classify()")
        require_identifier("provider_name", provider_name)
        require_identifier("model_id", model_id)
        require_identifier("model_version", model_version)
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ContractError("timeout_seconds must be a positive finite number")
        self._provider = provider
        self._provider_name = provider_name
        self._model_id = model_id
        self._model_version = model_version
        self._timeout_seconds = float(timeout_seconds)

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def route(self, request: RouteInput) -> RouteHandoff:
        if not isinstance(request, RouteInput):
            raise ContractError("ProviderIntentRouter.route requires RouteInput")
        provider_request = ProviderRequest(raw_text=request.raw_text, trace=request.trace)
        try:
            response = self._invoke(provider_request)
        except TimeoutError:
            return self._failure(request.trace, "timeout")
        except RouterProviderUnavailable:
            return self._failure(request.trace, "unavailable")
        except (ConnectionError, OSError):
            return self._failure(request.trace, "transport_error")
        except Exception:
            return self._failure(request.trace, "invalid_response")

        try:
            return self._coerce_response(request.trace, response)
        except Exception:
            return self._failure(request.trace, "invalid_response")

    def _invoke(self, request: ProviderRequest) -> object:
        classify = getattr(self._provider, "classify", None)
        if callable(classify):
            return classify(request)
        return self._provider(request)  # type: ignore[misc]

    def _failure(self, trace: RouteTrace, status: str, confidence: float = 0.0) -> RouteHandoff:
        return normalize_router_failure(
            trace,
            provider=self._provider_name,
            model_id=self._model_id,
            model_version=self._model_version,
            status=status,
            confidence=confidence,
        )

    def _coerce_response(self, trace: RouteTrace, response: object) -> RouteHandoff:
        if isinstance(response, RouteHandoff):
            if response.trace is not trace:
                raise ContractError("provider response replaced the caller trace")
            if response.router.provider != self._provider_name or response.router.model_id != self._model_id or response.router.model_version != self._model_version:
                raise ContractError("provider response metadata does not match adapter metadata")
            if response.router.status != "success":
                score = response.confidence if response.router.status == "low_confidence" else 0.0
                return self._failure(trace, response.router.status, score)
            return response
        if not isinstance(response, Mapping) or any(type(key) is not str for key in response):
            raise ContractError("provider response must be a JSON-like object")
        response = dict(response)

        status = response.get("status", "success")
        if type(status) is not str:
            raise ContractError("provider status must be a string")
        if status != "success":
            allowed = {"status"}
            if status == "low_confidence":
                allowed.add("confidence")
            if set(response) != allowed:
                raise ContractError("provider failure response contains unsupported fields")
            confidence = response.get("confidence", 0.0)
            if status != "low_confidence":
                confidence = 0.0
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ContractError("provider confidence must be a finite value in 0..1")
            return self._failure(trace, status, float(confidence))

        expected = {
            "route",
            "intent",
            "handling_mode",
            "confidence",
            "slots",
            "required_confirmation",
            "tool_scope",
        }
        if set(response) != expected | {"status"} and set(response) != expected:
            raise ContractError("provider success response has an unexpected schema")
        confidence = response["confidence"]
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ContractError("provider confidence must be a finite value in 0..1")
        slots_payload = response["slots"]
        if not isinstance(slots_payload, Mapping):
            raise ContractError("provider slots must be an object")
        slots = {
            name: SlotCandidate.from_dict(slot)
            for name, slot in slots_payload.items()
            if isinstance(name, str)
        }
        if len(slots) != len(slots_payload):
            raise ContractError("provider slot names must be strings")
        tools = response["tool_scope"]
        if not isinstance(tools, list) or any(type(tool) is not str for tool in tools):
            raise ContractError("provider tool_scope must be a string array")
        if float(confidence) < 0.80:
            return self._failure(trace, "low_confidence", float(confidence))
        return RouteHandoff(
            trace=trace,
            route=response["route"],
            intent=response["intent"],
            handling_mode=response["handling_mode"],
            confidence=confidence,
            slots=slots,
            required_confirmation=response["required_confirmation"],
            tool_scope=tuple(tools),
            router=RouterStatus(
                provider=self._provider_name,
                model_id=self._model_id,
                model_version=self._model_version,
                status="success",
                failure_code=None,
            ),
        )


RouterProviderAdapter = ProviderIntentRouter


__all__ = [
    "IntentRouter",
    "ProviderIntentRouter",
    "ProviderRequest",
    "ProviderUnavailableError",
    "RouteInput",
    "RouterProviderAdapter",
    "RouterProviderError",
    "RouterProviderUnavailable",
    "parse_deepseek_raw_route_response",
    "parse_deepseek_route_response",
    "normalize_router_failure",
]
