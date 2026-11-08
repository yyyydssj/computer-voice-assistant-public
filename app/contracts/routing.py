"""P7-R-01: strictly validated, immutable routing hints (never authorization)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

from .base import ContractError, StrictContract, parse_utc_timestamp, require_identifier, require_non_empty

_READ_TOOLS = frozenset({"list_directory", "read_text_file", "search_files", "get_file_info",
                         "get_system_info", "get_processes"})
_WRITE_TOOLS = frozenset({"create_text_file", "write_text_file", "copy_file", "move_file",
                          "rename_file", "move_to_recycle_bin", "start_whitelisted_application"})
_INTENT_TOOLS = {
    "general_chat": frozenset(), "system_status": frozenset({"get_system_info"}),
    "process_query": frozenset({"get_processes"}), "file_read": frozenset({"read_text_file"}),
    "directory_listing": frozenset({"list_directory"}), "file_search": frozenset({"search_files"}),
    "file_info": frozenset({"get_file_info"}),
    "open_application": frozenset({"start_whitelisted_application"}),
    "file_operation": _WRITE_TOOLS - frozenset({"start_whitelisted_application", "move_to_recycle_bin"}),
    "planning_request": frozenset(), "delete_file": frozenset({"move_to_recycle_bin"}),
    "overwrite_file": frozenset({"write_text_file"}),
}
_ROUTE_INTENTS = {
    "chat": frozenset({"general_chat"}),
    "query": frozenset({"system_status", "process_query", "file_read", "directory_listing", "file_search", "file_info"}),
    "task": frozenset({"open_application", "file_operation", "planning_request"}),
    "dangerous": frozenset({"delete_file", "overwrite_file"}),
    "ambiguous": frozenset({"unknown", "missing_target", "low_confidence", "router_unavailable", "invalid_router_output"}),
    "unsupported": frozenset({"unsupported_request", "policy_sensitive", "unimplemented_capability"}),
}
_MODES = {"chat": "chat_reply", "query": "read_only_tool", "task": "plan_then_execute",
          "dangerous": "plan_then_safety", "ambiguous": "clarify", "unsupported": "reject"}
_SLOT_NAMES = frozenset({"application_name", "source_path", "destination_path", "target_path",
                         "directory_path", "file_pattern", "operation", "content"})
_STATUS_CODES = {
    "success": frozenset({None}),
    "unavailable": frozenset({"router_unavailable"}),
    "timeout": frozenset({"router_unavailable"}),
    "transport_error": frozenset({"router_unavailable"}),
    "invalid_response": frozenset({"invalid_router_output"}),
    "low_confidence": frozenset({"low_confidence"}),
}


def _keys(payload: Any, expected: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or any(type(k) is not str for k in payload):
        raise ContractError(f"{name} 必须是 JSON 对象")
    extra = set(payload) - expected
    missing = expected - set(payload)
    if extra or missing:
        raise ContractError(f"{name} 未知字段或缺少字段: {sorted(extra | missing)}")
    return payload


def _confidence(value: Any, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ContractError(f"{name} 必须是 0..1 的有限数字")


def _choice(name: str, value: Any, choices: Mapping | set | frozenset) -> None:
    if type(value) is not str or value not in choices:
        raise ContractError(f"{name} 不支持的枚举值")


def _freeze_json(value: Any, ancestors: frozenset[int] = frozenset(), depth: int = 0) -> Any:
    """Copy finite JSON candidates into immutable, non-cyclic, depth-bounded structures."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if depth >= 32 or id(value) in ancestors:
        raise ContractError("SlotCandidate.value 不能循环引用或嵌套过深")
    lineage = ancestors | {id(value)}
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, lineage, depth + 1) for item in value)
    if isinstance(value, Mapping) and all(type(k) is str for k in value):
        return MappingProxyType({key: _freeze_json(item, lineage, depth + 1) for key, item in value.items()})
    raise ContractError("SlotCandidate.value 必须是有限 JSON 候选值")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("JSON 重复字段")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ContractError("JSON 不允许非标准数字")


@dataclass(frozen=True, slots=True)
class RouteTrace:
    request_id: str
    conversation_id: str
    turn_id: str
    source: str
    raw_text: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("request_id", "conversation_id", "turn_id"):
            require_identifier(name, getattr(self, name))
        _choice("RouteTrace.source", self.source, {"text_shell", "asr"})
        require_non_empty("raw_text", self.raw_text, maximum=32768)
        parse_utc_timestamp("created_at", self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RouteTrace:
        return cls(**_keys(payload, frozenset(cls.__dataclass_fields__), "trace"))


@dataclass(frozen=True, slots=True)
class SlotCandidate:
    value: Any
    source: str
    confidence: float
    confirmed: bool

    def __post_init__(self) -> None:
        _choice("SlotCandidate.source", self.source, {"utterance", "normalization", "classifier"})
        _confidence(self.confidence, "SlotCandidate.confidence")
        if type(self.confirmed) is not bool or self.confirmed:
            raise ContractError("分类器 SlotCandidate.confirmed 必须为 false")
        object.__setattr__(self, "value", _freeze_json(self.value))

    def to_dict(self) -> dict[str, Any]:
        return {"value": _thaw_json(self.value), "source": self.source,
                "confidence": self.confidence, "confirmed": self.confirmed}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SlotCandidate:
        return cls(**_keys(payload, frozenset(cls.__dataclass_fields__), "slot"))


@dataclass(frozen=True, slots=True)
class RouterStatus:
    provider: str
    model_id: str
    model_version: str
    status: str
    failure_code: str | None

    def __post_init__(self) -> None:
        for name in ("provider", "model_id", "model_version"):
            require_identifier(name, getattr(self, name))
        _choice("RouterStatus.status", self.status, frozenset(_STATUS_CODES))
        if (self.failure_code is not None and type(self.failure_code) is not str) or self.failure_code not in _STATUS_CODES[self.status]:
            raise ContractError("RouterStatus.failure_code 与 status 不一致")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RouterStatus:
        return cls(**_keys(payload, frozenset(cls.__dataclass_fields__), "router"))


@dataclass(frozen=True, slots=True)
class RouteHandoff(StrictContract):
    """Untrusted routing suggestion; construction grants no tool or execution authority."""

    SCHEMA_VERSION: ClassVar[int] = 1

    trace: RouteTrace
    route: str
    intent: str
    handling_mode: str
    confidence: float
    slots: Mapping[str, SlotCandidate]
    required_confirmation: str
    tool_scope: tuple[str, ...]
    router: RouterStatus

    def __post_init__(self) -> None:
        if not isinstance(self.trace, RouteTrace) or not isinstance(self.router, RouterStatus):
            raise ContractError("trace/router 必须是已校验的契约对象")
        _choice("RouteHandoff.route", self.route, frozenset(_ROUTE_INTENTS))
        _choice("RouteHandoff.intent", self.intent, _ROUTE_INTENTS[self.route])
        if self.handling_mode != _MODES[self.route]:
            raise ContractError("RouteHandoff.handling_mode 与 route 不一致")
        _confidence(self.confidence, "RouteHandoff.confidence")
        _choice("RouteHandoff.required_confirmation", self.required_confirmation,
                       {"none", "defer_to_safety", "must_clarify"})
        expected_confirmation = ("defer_to_safety" if self.route in {"task", "dangerous"}
                                 else "must_clarify" if self.route == "ambiguous" else "none")
        if self.required_confirmation != expected_confirmation:
            raise ContractError("RouteHandoff.required_confirmation 与 route 不一致")
        if self.route != "ambiguous" and self.confidence < 0.80:
            raise ContractError("低置信度必须进入 ambiguous")
        if self.router.status == "low_confidence" and self.confidence >= 0.80:
            raise ContractError("low_confidence 状态不能声称达到最低置信度")
        if self.router.status == "success" and self.route == "ambiguous" and self.intent in {"router_unavailable", "invalid_router_output", "low_confidence"}:
            raise ContractError("RouteHandoff.router 与故障意图不一致")
        if self.router.status != "success" and (self.route != "ambiguous" or self.intent != self.router.failure_code):
            raise ContractError("路由器失败只能进入匹配的 ambiguous 交接")
        if not isinstance(self.slots, Mapping) or any(
            type(name) is not str or name not in _SLOT_NAMES or not isinstance(slot, SlotCandidate)
            for name, slot in self.slots.items()
        ):
            raise ContractError("RouteHandoff.slots 含未知或未校验候选")
        object.__setattr__(self, "slots", MappingProxyType(dict(self.slots)))
        if not isinstance(self.tool_scope, tuple) or any(type(name) is not str for name in self.tool_scope):
            raise ContractError("RouteHandoff.tool_scope 必须是工具元组")
        if len(self.tool_scope) != len(set(self.tool_scope)):
            raise ContractError("RouteHandoff.tool_scope 含重复工具")
        allowed = _INTENT_TOOLS.get(self.intent, frozenset())
        if any(tool not in allowed for tool in self.tool_scope):
            raise ContractError("RouteHandoff.tool_scope 不在意图允许范围内")
        if self.route == "query" and any(tool not in _READ_TOOLS for tool in self.tool_scope):
            raise ContractError("只读路由不得携带副作用工具")
        if self.route in {"chat", "ambiguous", "unsupported"} and self.tool_scope:
            raise ContractError("非工具路由不得携带工具范围")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, "trace": self.trace.to_dict(),
                "route": self.route, "intent": self.intent, "handling_mode": self.handling_mode,
                "confidence": self.confidence,
                "slots": {name: slot.to_dict() for name, slot in self.slots.items()},
                "required_confirmation": self.required_confirmation, "tool_scope": list(self.tool_scope),
                "router": self.router.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RouteHandoff:
        data = _keys(payload, frozenset({"schema_version", "trace", "route", "intent", "handling_mode",
                                         "confidence", "slots", "required_confirmation", "tool_scope", "router"}),
                     "RouteHandoff")
        if type(data["schema_version"]) is not int or data["schema_version"] != cls.SCHEMA_VERSION:
            raise ContractError("RouteHandoff 不支持 schema_version")
        slots = data["slots"]
        if not isinstance(slots, Mapping) or any(type(name) is not str for name in slots):
            raise ContractError("slots 必须是 JSON 对象")
        tools = data["tool_scope"]
        if not isinstance(tools, list):
            raise ContractError("tool_scope 必须是 JSON 数组")
        return cls(trace=RouteTrace.from_dict(data["trace"]), route=data["route"], intent=data["intent"],
                   handling_mode=data["handling_mode"], confidence=data["confidence"],
                   slots={name: SlotCandidate.from_dict(slot) for name, slot in slots.items()},
                   required_confirmation=data["required_confirmation"], tool_scope=tuple(tools),
                   router=RouterStatus.from_dict(data["router"]))

    @classmethod
    def from_json(cls, payload: str) -> RouteHandoff:
        try:
            decoded = json.loads(payload, object_pairs_hook=_no_duplicate_pairs, parse_constant=_invalid_constant)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError("RouteHandoff JSON 无效") from exc
        return cls.from_dict(decoded)