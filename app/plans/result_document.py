"""P4 批次 7：不可执行的 result.json 事实与严格稳定编解码。

本模块只描述动作返回、验证和错误事实；不执行动作、不生成授权、不写入磁盘。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from .action_plan import PlanAction, ReviewableActionPlan

RESULT_DOCUMENT_SCHEMA_VERSION = 1
RESULT_DOCUMENT_FORMAT_VERSION = "1.0.0"
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OUTCOMES = frozenset({"succeeded", "failed", "skipped", "state_uncertain"})
_VERIFICATION_STATUSES = frozenset({"completed", "not_completed", "state_uncertain"})
_RESULT_FIELDS = frozenset({
    "schema_version", "format_version", "result_id", "result_hash", "execution_id",
    "plan_id", "plan_version", "plan_hash", "created_at", "completed_at",
    "overall_status", "actions",
})
_ACTION_FIELDS = frozenset({
    "order", "action_id", "tool_name", "tool_version", "manifest_hash", "outcome",
    "attempt_id", "started_at", "finished_at", "verification_status", "verified_at",
    "verifier", "verification_facts", "output", "error_code", "error_message", "skip_reason",
})


class ResultDocumentError(ValueError):
    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        self.code = code
        self.message = message
        self.field = field
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.field is not None:
            value["field"] = self.field
        return value


def _freeze(value: Any, field: str) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ResultDocumentError("invalid_field", f"{field} 字段名必须是字符串", field=field)
        return MappingProxyType({key: _freeze(item, field) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, field) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ResultDocumentError("invalid_field", f"{field} 不允许非有限数字", field=field)
        return value
    raise ResultDocumentError("invalid_field", f"{field} 只允许 JSON 值", field=field)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _identifier(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ResultDocumentError("invalid_field", f"{name} 必须是稳定标识符", field=name)
    return value


def _text(name: str, value: Any, *, optional: bool = False, maximum: int = 4096) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ResultDocumentError("invalid_field", f"{name} 必须是有效文本", field=name)
    return value


def _positive(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResultDocumentError("invalid_field", f"{name} 必须是正整数", field=name)
    return value


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise ResultDocumentError("invalid_field", f"{name} 必须是 sha256 哈希", field=name)
    return value


def _utc(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ResultDocumentError("invalid_field", f"{name} 必须是 UTC 时间", field=name)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ResultDocumentError("invalid_field", f"{name} 必须是有效 UTC 时间", field=name) from exc
    if parsed.tzinfo != timezone.utc:
        raise ResultDocumentError("invalid_field", f"{name} 必须是 UTC 时间", field=name)
    return value


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultDocumentError("invalid_field", f"{name} 必须是对象", field=name)
    return value


def _fields(payload: Mapping[str, Any], expected: frozenset[str], context: str) -> None:
    unknown = sorted(set(payload) - expected)
    if unknown:
        raise ResultDocumentError("unknown_field", f"{context} 包含未知字段: {', '.join(unknown)}")
    missing = sorted(expected - set(payload))
    if missing:
        raise ResultDocumentError("missing_field", f"{context} 缺少字段: {', '.join(missing)}")


def _json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ResultDocumentError("invalid_json_value", "结果包含不能稳定序列化的 JSON 值") from exc


def _digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _parsed_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _derive_overall_status(outcomes: tuple[str, ...]) -> str:
    if any(item == "state_uncertain" for item in outcomes):
        return "state_uncertain"
    if all(item == "succeeded" for item in outcomes):
        return "completed"
    if any(item == "succeeded" for item in outcomes):
        return "partially_completed"
    if any(item == "failed" for item in outcomes):
        return "failed"
    return "not_completed"


@dataclass(frozen=True, slots=True)
class ResultAction:
    order: int
    action_id: str
    tool_name: str
    tool_version: str
    manifest_hash: str
    outcome: str
    attempt_id: str | None
    started_at: str | None
    finished_at: str | None
    verification_status: str
    verified_at: str
    verifier: str
    verification_facts: Mapping[str, Any]
    output: Mapping[str, Any]
    error_code: str | None = None
    error_message: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        _positive("order", self.order)
        _identifier("action_id", self.action_id)
        _identifier("tool_name", self.tool_name)
        _text("tool_version", self.tool_version, maximum=64)
        _hash("manifest_hash", self.manifest_hash)
        if self.outcome not in _OUTCOMES:
            raise ResultDocumentError("invalid_field", f"outcome 不支持值 {self.outcome!r}", field="outcome")
        _identifier("attempt_id", self.attempt_id, optional=True)
        _utc("started_at", self.started_at, optional=True)
        _utc("finished_at", self.finished_at, optional=True)
        if self.verification_status not in _VERIFICATION_STATUSES:
            raise ResultDocumentError("invalid_field", "verification_status 无效", field="verification_status")
        _utc("verified_at", self.verified_at)
        _identifier("verifier", self.verifier)
        _mapping("verification_facts", self.verification_facts)
        _mapping("output", self.output)
        _identifier("error_code", self.error_code, optional=True)
        _text("error_message", self.error_message, optional=True)
        _text("skip_reason", self.skip_reason, optional=True)
        object.__setattr__(self, "verification_facts", _freeze(self.verification_facts, "verification_facts"))
        object.__setattr__(self, "output", _freeze(self.output, "output"))
        self._validate_semantics()
        if self.started_at is not None and self.finished_at is not None and _parsed_utc(self.finished_at) < _parsed_utc(self.started_at):
            raise ResultDocumentError("timestamp_order_invalid", "finished_at 不得早于 started_at")
        if self.started_at is not None and _parsed_utc(self.verified_at) < _parsed_utc(self.started_at):
            raise ResultDocumentError("timestamp_order_invalid", "verified_at 不得早于 started_at")
        if self.finished_at is not None and _parsed_utc(self.verified_at) < _parsed_utc(self.finished_at):
            raise ResultDocumentError("timestamp_order_invalid", "verified_at 不得早于 finished_at")

    def _validate_semantics(self) -> None:
        if self.outcome == "succeeded":
            valid = (
                self.attempt_id is not None and self.started_at is not None and self.finished_at is not None
                and self.verification_status == "completed" and self.error_code is None
                and self.error_message is None and self.skip_reason is None
            )
        elif self.outcome == "failed":
            valid = (
                self.attempt_id is not None and self.started_at is not None and self.finished_at is not None
                and self.verification_status == "not_completed" and self.error_code is not None
                and self.error_message is not None and self.skip_reason is None
            )
        elif self.outcome == "skipped":
            valid = (
                self.attempt_id is None and self.started_at is None and self.finished_at is None
                and self.verification_status == "not_completed" and self.error_code is None
                and self.error_message is None and self.skip_reason is not None
            )
        else:
            valid = (
                self.attempt_id is not None and self.started_at is not None
                and self.verification_status == "state_uncertain" and self.error_code is not None
                and self.error_message is not None and self.skip_reason is None
            )
        if not valid:
            raise ResultDocumentError("result_semantics_invalid", "动作结果、验证、错误和跳过字段不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order, "action_id": self.action_id, "tool_name": self.tool_name,
            "tool_version": self.tool_version, "manifest_hash": self.manifest_hash,
            "outcome": self.outcome, "attempt_id": self.attempt_id,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "verification_status": self.verification_status, "verified_at": self.verified_at,
            "verifier": self.verifier, "verification_facts": _thaw(self.verification_facts),
            "output": _thaw(self.output), "error_code": self.error_code,
            "error_message": self.error_message, "skip_reason": self.skip_reason,
        }


@dataclass(frozen=True, slots=True)
class ResultDocument:
    result_id: str
    result_hash: str
    execution_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    created_at: str
    completed_at: str
    overall_status: str
    actions: tuple[ResultAction, ...]
    format_version: str = RESULT_DOCUMENT_FORMAT_VERSION
    schema_version: int = RESULT_DOCUMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESULT_DOCUMENT_SCHEMA_VERSION:
            raise ResultDocumentError("unsupported_schema_version", f"不支持 schema_version={self.schema_version!r}")
        if self.format_version != RESULT_DOCUMENT_FORMAT_VERSION:
            raise ResultDocumentError("unsupported_format_version", f"不支持 format_version={self.format_version!r}")
        _identifier("result_id", self.result_id)
        _hash("result_hash", self.result_hash)
        _identifier("execution_id", self.execution_id)
        _identifier("plan_id", self.plan_id)
        _positive("plan_version", self.plan_version)
        _hash("plan_hash", self.plan_hash)
        _utc("created_at", self.created_at)
        _utc("completed_at", self.completed_at)
        if _parsed_utc(self.completed_at) < _parsed_utc(self.created_at):
            raise ResultDocumentError("timestamp_order_invalid", "completed_at 不得早于 created_at")
        actions = tuple(self.actions)
        if not actions or any(not isinstance(item, ResultAction) for item in actions):
            raise ResultDocumentError("invalid_field", "actions 必须是非空 ResultAction 数组")
        object.__setattr__(self, "actions", actions)
        _validate_action_sequence(actions)
        expected_status = _derive_overall_status(tuple(item.outcome for item in actions))
        if self.overall_status != expected_status:
            raise ResultDocumentError("overall_status_mismatch", "overall_status 与动作结果不一致")

    def without_hash_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "format_version": self.format_version,
            "result_id": self.result_id, "execution_id": self.execution_id,
            "plan_id": self.plan_id, "plan_version": self.plan_version,
            "plan_hash": self.plan_hash, "created_at": self.created_at,
            "completed_at": self.completed_at, "overall_status": self.overall_status,
            "actions": [item.to_dict() for item in self.actions],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.without_hash_dict(), "result_hash": self.result_hash}


def _validate_action_sequence(actions: tuple[ResultAction, ...]) -> None:
    ids = [item.action_id for item in actions]
    if len(set(ids)) != len(ids):
        raise ResultDocumentError("duplicate_action_id", "结果动作不得包含重复 action_id")
    if [item.order for item in actions] != list(range(1, len(actions) + 1)):
        raise ResultDocumentError("action_sequence_mismatch", "结果动作 order 必须从 1 连续递增")


def build_plan_action(*args: Any, **kwargs: Any) -> PlanAction:
    """兼容从 app.plans 重新导出的 ActionPlan 构造器。"""
    from .action_plan import build_plan_action as _build
    return _build(*args, **kwargs)


def build_result_action(
    *, plan_action: PlanAction, outcome: str, attempt_id: str | None,
    started_at: str | None, finished_at: str | None, verification_status: str,
    verified_at: str, verifier: str, verification_facts: Mapping[str, Any],
    output: Mapping[str, Any], error_code: str | None = None,
    error_message: str | None = None, skip_reason: str | None = None,
) -> ResultAction:
    if not isinstance(plan_action, PlanAction):
        raise TypeError("plan_action 必须是 PlanAction")
    return ResultAction(
        order=plan_action.order, action_id=plan_action.action_id,
        tool_name=plan_action.tool_name, tool_version=plan_action.tool_version,
        manifest_hash=plan_action.manifest_hash, outcome=outcome,
        attempt_id=attempt_id, started_at=started_at, finished_at=finished_at,
        verification_status=verification_status, verified_at=verified_at,
        verifier=verifier, verification_facts=verification_facts, output=output,
        error_code=error_code, error_message=error_message, skip_reason=skip_reason,
    )


def build_result_document(
    *, plan: ReviewableActionPlan, result_id: str, execution_id: str,
    created_at: str, completed_at: str, actions: tuple[ResultAction, ...],
) -> ResultDocument:
    if not isinstance(plan, ReviewableActionPlan):
        raise TypeError("plan 必须是 ReviewableActionPlan")
    actions = tuple(actions)
    _validate_action_sequence(actions)
    if len(actions) != len(plan.actions):
        raise ResultDocumentError("action_sequence_mismatch", "结果动作必须与计划动作数量一致")
    for expected, actual in zip(plan.actions, actions):
        _assert_action_identity(expected, actual)
    overall_status = _derive_overall_status(tuple(item.outcome for item in actions))
    unhashed = {
        "schema_version": RESULT_DOCUMENT_SCHEMA_VERSION,
        "format_version": RESULT_DOCUMENT_FORMAT_VERSION,
        "result_id": result_id, "execution_id": execution_id,
        "plan_id": plan.plan_id, "plan_version": plan.plan_version,
        "plan_hash": plan.plan_hash, "created_at": created_at,
        "completed_at": completed_at, "overall_status": overall_status,
        "actions": [item.to_dict() for item in actions],
    }
    return ResultDocument(
        result_id=result_id, result_hash=_digest(unhashed), execution_id=execution_id,
        plan_id=plan.plan_id, plan_version=plan.plan_version, plan_hash=plan.plan_hash,
        created_at=created_at, completed_at=completed_at, overall_status=overall_status,
        actions=actions,
    )


def _assert_action_identity(expected: PlanAction, actual: ResultAction) -> None:
    if actual.action_id != expected.action_id or actual.tool_name != expected.tool_name \
            or actual.tool_version != expected.tool_version or actual.manifest_hash != expected.manifest_hash:
        raise ResultDocumentError("action_identity_mismatch", f"结果动作 {actual.action_id!r} 与计划身份不一致")
    if actual.order != expected.order:
        raise ResultDocumentError("action_sequence_mismatch", "结果动作顺序与计划不一致")


def serialize_result_json(document: ResultDocument) -> str:
    if not isinstance(document, ResultDocument):
        raise TypeError("document 必须是 ResultDocument")
    if document.result_hash != _digest(document.without_hash_dict()):
        raise ResultDocumentError("result_hash_mismatch", "结果对象的 result_hash 与内容不一致")
    return _json(document.to_dict()) + "\n"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ResultDocumentError("duplicate_json_key", f"JSON 字段重复: {key}")
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise ValueError(value)


def _parse_action(payload: Any) -> ResultAction:
    if not isinstance(payload, Mapping):
        raise ResultDocumentError("invalid_field", "actions 元素必须是对象")
    _fields(payload, _ACTION_FIELDS, "result action")
    return ResultAction(
        order=_positive("order", payload["order"]),
        action_id=_identifier("action_id", payload["action_id"]),
        tool_name=_identifier("tool_name", payload["tool_name"]),
        tool_version=_text("tool_version", payload["tool_version"], maximum=64),
        manifest_hash=_hash("manifest_hash", payload["manifest_hash"]),
        outcome=payload["outcome"], attempt_id=_identifier("attempt_id", payload["attempt_id"], optional=True),
        started_at=_utc("started_at", payload["started_at"], optional=True),
        finished_at=_utc("finished_at", payload["finished_at"], optional=True),
        verification_status=payload["verification_status"], verified_at=_utc("verified_at", payload["verified_at"]),
        verifier=_identifier("verifier", payload["verifier"]),
        verification_facts=_mapping("verification_facts", payload["verification_facts"]),
        output=_mapping("output", payload["output"]),
        error_code=_identifier("error_code", payload["error_code"], optional=True),
        error_message=_text("error_message", payload["error_message"], optional=True),
        skip_reason=_text("skip_reason", payload["skip_reason"], optional=True),
    )


def parse_result_json(payload: str, *, expected_plan: ReviewableActionPlan) -> ResultDocument:
    if not isinstance(payload, str) or not isinstance(expected_plan, ReviewableActionPlan):
        raise TypeError("必须提供 JSON 字符串和 ReviewableActionPlan")
    try:
        decoded = json.loads(payload, object_pairs_hook=_pairs, parse_constant=_constant)
    except ResultDocumentError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ResultDocumentError("invalid_json", "result.json 不是严格 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ResultDocumentError("invalid_json", "result.json 顶层必须是对象")
    _fields(decoded, _RESULT_FIELDS, "result.json")
    if decoded["schema_version"] != RESULT_DOCUMENT_SCHEMA_VERSION:
        raise ResultDocumentError("unsupported_schema_version", f"不支持 schema_version={decoded['schema_version']!r}")
    if decoded["format_version"] != RESULT_DOCUMENT_FORMAT_VERSION:
        raise ResultDocumentError("unsupported_format_version", f"不支持 format_version={decoded['format_version']!r}")
    supplied_hash = _hash("result_hash", decoded["result_hash"])
    unhashed = {key: item for key, item in decoded.items() if key != "result_hash"}
    if _digest(unhashed) != supplied_hash:
        raise ResultDocumentError("result_hash_mismatch", "result.json 内容与 result_hash 不一致")
    if decoded["plan_id"] != expected_plan.plan_id or decoded["plan_version"] != expected_plan.plan_version \
            or decoded["plan_hash"] != expected_plan.plan_hash:
        raise ResultDocumentError("plan_identity_mismatch", "结果绑定的计划身份与预期计划不一致")
    raw_actions = decoded["actions"]
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ResultDocumentError("invalid_field", "actions 必须是非空数组")
    actions = tuple(_parse_action(item) for item in raw_actions)
    _validate_action_sequence(actions)
    if len(actions) != len(expected_plan.actions):
        raise ResultDocumentError("action_sequence_mismatch", "结果动作数量与计划不一致")
    for expected, actual in zip(expected_plan.actions, actions):
        _assert_action_identity(expected, actual)
    expected_status = _derive_overall_status(tuple(item.outcome for item in actions))
    if decoded["overall_status"] != expected_status:
        raise ResultDocumentError("overall_status_mismatch", "overall_status 与动作结果不一致")
    document = ResultDocument(
        result_id=_identifier("result_id", decoded["result_id"]), result_hash=supplied_hash,
        execution_id=_identifier("execution_id", decoded["execution_id"]),
        plan_id=_identifier("plan_id", decoded["plan_id"]), plan_version=_positive("plan_version", decoded["plan_version"]),
        plan_hash=_hash("plan_hash", decoded["plan_hash"]), created_at=_utc("created_at", decoded["created_at"]),
        completed_at=_utc("completed_at", decoded["completed_at"]), overall_status=decoded["overall_status"],
        actions=actions, format_version=decoded["format_version"], schema_version=decoded["schema_version"],
    )
    if _digest(document.without_hash_dict()) != document.result_hash:
        raise ResultDocumentError("result_hash_mismatch", "解析后的结果事实与 result_hash 不一致")
    return document