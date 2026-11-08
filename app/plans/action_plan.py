"""P4 批次 6：不可执行的 ActionPlan 事实与稳定文本编解码。

只在内存中构造、校验和呈现 plan.json / execution_plan.md 内容；
不创建计划目录、不写文件、不生成授权，也不执行任何动作。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from app.safety.path_guard import PathSnapshot
from app.safety.policy_engine import POLICY_VERSION, PolicyDecision, PolicyInput
from app.tools.manifest import ManifestError, ToolRegistry

ACTION_PLAN_SCHEMA_VERSION = 1
ACTION_PLAN_FORMAT_VERSION = "1.0.0"
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_RISKS = frozenset({"L0", "L1", "L2", "L3"})
_PLAN_FIELDS = frozenset({"schema_version", "format_version", "plan_id", "plan_version", "plan_hash", "task_id", "created_at", "goal", "scope", "exclusions", "file_rules", "recursive", "conflict_policy", "actions"})
_ACTION_FIELDS = frozenset({"order", "action_id", "tool_name", "tool_version", "manifest_hash", "risk_level", "requires_confirmation", "normalized_arguments", "path_snapshots", "policy_decision", "preview", "verification_method", "rollback_strategy"})
_PATH_FIELDS = frozenset({"requested_path", "purpose", "normalized_path", "resolved_path", "exists", "target_identity", "anchor_path", "anchor_identity", "component_identities"})
_POLICY_FIELDS = frozenset({"decision", "reason_code", "reason", "policy_version", "tool_name", "tool_version", "manifest_hash", "risk_level", "requires_confirmation", "normalized_paths"})
_NOTES_START = "<!-- USER_NOTES_START -->"
_NOTES_END = "<!-- USER_NOTES_END -->"


class ActionPlanError(ValueError):
    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        self.code, self.message, self.field = code, message, field
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.field is not None:
            value["field"] = self.field
        return value


def _freeze(value: Any, field: str) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ActionPlanError("invalid_field", f"{field} 字段名必须是字符串", field=field)
        return MappingProxyType({key: _freeze(item, field) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, field) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ActionPlanError("invalid_field", f"{field} 不允许非有限数字", field=field)
        return value
    raise ActionPlanError("invalid_field", f"{field} 只允许 JSON 值", field=field)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _identifier(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ActionPlanError("invalid_field", f"{name} 必须是稳定标识符", field=name)
    return value


def _text(name: str, value: Any, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ActionPlanError("invalid_field", f"{name} 必须是有效文本", field=name)
    return value


def _positive(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ActionPlanError("invalid_field", f"{name} 必须是正整数", field=name)
    return value


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ActionPlanError("invalid_field", f"{name} 必须是布尔值", field=name)
    return value


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise ActionPlanError("invalid_field", f"{name} 必须是 sha256 哈希", field=name)
    return value


def _utc(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ActionPlanError("invalid_field", "created_at 必须是 UTC 时间", field="created_at")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ActionPlanError("invalid_field", "created_at 必须是有效 UTC 时间", field="created_at") from exc
    if parsed.tzinfo != timezone.utc:
        raise ActionPlanError("invalid_field", "created_at 必须是 UTC 时间", field="created_at")
    return value


def _texts(name: str, value: Any, *, empty: bool) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ActionPlanError("invalid_field", f"{name} 必须是字符串数组", field=name)
    result = tuple(_text(name, item) for item in value)
    if not empty and not result:
        raise ActionPlanError("invalid_field", f"{name} 不得为空", field=name)
    if len(set(result)) != len(result):
        raise ActionPlanError("invalid_field", f"{name} 不得重复", field=name)
    return result


def _fields(payload: Mapping[str, Any], expected: frozenset[str], context: str) -> None:
    unknown, missing = sorted(set(payload) - expected), sorted(expected - set(payload))
    if unknown:
        raise ActionPlanError("unknown_field", f"{context} 包含未知字段: {', '.join(unknown)}")
    if missing:
        raise ActionPlanError("missing_field", f"{context} 缺少字段: {', '.join(missing)}")


def _json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ActionPlanError("invalid_json_value", "计划包含不能稳定序列化的 JSON 值") from exc


def _digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
@dataclass(frozen=True, slots=True)
class PlanAction:
    order: int
    action_id: str
    tool_name: str
    tool_version: str
    manifest_hash: str
    risk_level: str
    requires_confirmation: bool
    normalized_arguments: Mapping[str, Any]
    path_snapshots: tuple[PathSnapshot, ...]
    policy_decision: PolicyDecision
    preview: Mapping[str, Any]
    verification_method: str
    rollback_strategy: str

    def __post_init__(self) -> None:
        _positive("order", self.order); _identifier("action_id", self.action_id); _identifier("tool_name", self.tool_name)
        _text("tool_version", self.tool_version, 64); _hash("manifest_hash", self.manifest_hash)
        if self.risk_level not in _ALLOWED_RISKS:
            raise ActionPlanError("invalid_field", "risk_level 无效", field="risk_level")
        _boolean("requires_confirmation", self.requires_confirmation)
        if not isinstance(self.normalized_arguments, Mapping) or not isinstance(self.preview, Mapping):
            raise ActionPlanError("invalid_field", "参数和预览必须是对象")
        snapshots = tuple(self.path_snapshots)
        if any(not isinstance(item, PathSnapshot) for item in snapshots):
            raise ActionPlanError("invalid_field", "path_snapshots 只能包含 PathSnapshot")
        if not isinstance(self.policy_decision, PolicyDecision):
            raise ActionPlanError("invalid_field", "policy_decision 必须是 PolicyDecision")
        _text("verification_method", self.verification_method); _text("rollback_strategy", self.rollback_strategy)
        object.__setattr__(self, "normalized_arguments", _freeze(self.normalized_arguments, "normalized_arguments"))
        object.__setattr__(self, "path_snapshots", snapshots)
        object.__setattr__(self, "preview", _freeze(self.preview, "preview"))
        _validate_action(self)

    def to_dict(self) -> dict[str, Any]:
        return {"order": self.order, "action_id": self.action_id, "tool_name": self.tool_name,
                "tool_version": self.tool_version, "manifest_hash": self.manifest_hash,
                "risk_level": self.risk_level, "requires_confirmation": self.requires_confirmation,
                "normalized_arguments": _thaw(self.normalized_arguments),
                "path_snapshots": [item.to_dict() for item in self.path_snapshots],
                "policy_decision": self.policy_decision.to_dict(), "preview": _thaw(self.preview),
                "verification_method": self.verification_method, "rollback_strategy": self.rollback_strategy}


@dataclass(frozen=True, slots=True)
class ReviewableActionPlan:
    plan_id: str
    plan_version: int
    plan_hash: str
    task_id: str
    created_at: str
    goal: str
    scope: tuple[str, ...]
    exclusions: tuple[str, ...]
    file_rules: tuple[str, ...]
    recursive: bool
    conflict_policy: str
    actions: tuple[PlanAction, ...]
    format_version: str = ACTION_PLAN_FORMAT_VERSION
    schema_version: int = ACTION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ACTION_PLAN_SCHEMA_VERSION:
            raise ActionPlanError("unsupported_schema_version", f"不支持 schema_version={self.schema_version!r}")
        if self.format_version != ACTION_PLAN_FORMAT_VERSION:
            raise ActionPlanError("unsupported_format_version", f"不支持 format_version={self.format_version!r}")
        _identifier("plan_id", self.plan_id); _positive("plan_version", self.plan_version); _hash("plan_hash", self.plan_hash)
        _identifier("task_id", self.task_id); _utc(self.created_at); _text("goal", self.goal)
        object.__setattr__(self, "scope", _texts("scope", self.scope, empty=False))
        object.__setattr__(self, "exclusions", _texts("exclusions", self.exclusions, empty=True))
        object.__setattr__(self, "file_rules", _texts("file_rules", self.file_rules, empty=True))
        _boolean("recursive", self.recursive)
        if self.conflict_policy != "deny":
            raise ActionPlanError("invalid_conflict_policy", "当前 MVP 冲突策略固定为 deny")
        actions = tuple(self.actions)
        if not actions or any(not isinstance(item, PlanAction) for item in actions):
            raise ActionPlanError("invalid_field", "actions 必须是非空 PlanAction 数组")
        object.__setattr__(self, "actions", actions); _validate_sequence(actions)

    def without_hash_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "format_version": self.format_version,
                "plan_id": self.plan_id, "plan_version": self.plan_version, "task_id": self.task_id,
                "created_at": self.created_at, "goal": self.goal, "scope": list(self.scope),
                "exclusions": list(self.exclusions), "file_rules": list(self.file_rules),
                "recursive": self.recursive, "conflict_policy": self.conflict_policy,
                "actions": [item.to_dict() for item in self.actions]}

    def to_dict(self) -> dict[str, Any]:
        value = self.without_hash_dict(); value["plan_hash"] = self.plan_hash; return value


@dataclass(frozen=True, slots=True)
class ExecutionPlanReview:
    plan_id: str
    plan_version: int
    plan_hash: str
    user_notes: str


def _validate_action(action: PlanAction) -> None:
    decision = action.policy_decision
    if decision.decision == "deny":
        raise ActionPlanError("policy_denied", "拒绝决策不能进入 ActionPlan")
    if decision.policy_version != POLICY_VERSION:
        raise ActionPlanError("policy_version_mismatch", "Policy 版本与当前固定版本不一致")
    left = (action.tool_name, action.tool_version, action.manifest_hash, action.risk_level, action.requires_confirmation)
    right = (decision.tool_name, decision.tool_version, decision.manifest_hash, decision.risk_level, decision.requires_confirmation)
    if left != right or decision.normalized_paths != tuple(item.normalized_path for item in action.path_snapshots):
        raise ActionPlanError("policy_fact_mismatch", "动作事实与 PolicyDecision 不一致")
    expected = "allow" if action.risk_level in {"L0", "L1"} else "require_confirmation"
    if decision.decision != expected:
        raise ActionPlanError("policy_fact_mismatch", "策略决策与固定风险等级不一致")


def _validate_sequence(actions: tuple[PlanAction, ...]) -> None:
    if tuple(item.order for item in actions) != tuple(range(1, len(actions) + 1)):
        raise ActionPlanError("action_order_invalid", "动作顺序必须从 1 连续递增且不得重排")
    if len({item.action_id for item in actions}) != len(actions):
        raise ActionPlanError("duplicate_action_id", "action_id 不得重复")


def build_plan_action(*, action_id: str, policy_input: PolicyInput, policy_decision: PolicyDecision,
                      preview: Mapping[str, Any], verification_method: str, rollback_strategy: str,
                      order: int = 1) -> PlanAction:
    if not isinstance(policy_input, PolicyInput) or not isinstance(policy_decision, PolicyDecision):
        raise TypeError("必须提供 PolicyInput 和 PolicyDecision")
    if policy_decision.decision == "deny":
        raise ActionPlanError("policy_denied", "PolicyEngine 已拒绝该动作")
    left = (policy_input.tool_name, policy_input.tool_version, policy_input.manifest_hash,
            policy_input.risk_level, policy_input.requires_confirmation,
            tuple(item.normalized_path for item in policy_input.path_snapshots))
    right = (policy_decision.tool_name, policy_decision.tool_version, policy_decision.manifest_hash,
             policy_decision.risk_level, policy_decision.requires_confirmation, policy_decision.normalized_paths)
    if left != right:
        raise ActionPlanError("policy_fact_mismatch", "PolicyInput 与 PolicyDecision 事实不一致")
    return PlanAction(order, action_id, policy_input.tool_name, policy_input.tool_version,
                      policy_input.manifest_hash, policy_input.risk_level, policy_input.requires_confirmation,
                      policy_input.normalized_arguments, policy_input.path_snapshots, policy_decision,
                      preview, verification_method, rollback_strategy)


def build_reviewable_plan(*, plan_id: str, plan_version: int, task_id: str, created_at: str,
                          goal: str, scope: tuple[str, ...], exclusions: tuple[str, ...],
                          file_rules: tuple[str, ...], recursive: bool, conflict_policy: str,
                          actions: tuple[PlanAction, ...]) -> ReviewableActionPlan:
    actions = tuple(actions); _validate_sequence(actions)
    unhashed = {"schema_version": ACTION_PLAN_SCHEMA_VERSION, "format_version": ACTION_PLAN_FORMAT_VERSION,
                "plan_id": plan_id, "plan_version": plan_version, "task_id": task_id,
                "created_at": created_at, "goal": goal, "scope": list(scope),
                "exclusions": list(exclusions), "file_rules": list(file_rules), "recursive": recursive,
                "conflict_policy": conflict_policy, "actions": [item.to_dict() for item in actions]}
    return ReviewableActionPlan(plan_id, plan_version, _digest(unhashed), task_id, created_at, goal,
                                scope, exclusions, file_rules, recursive, conflict_policy, actions)

def serialize_plan_json(plan: ReviewableActionPlan) -> str:
    if not isinstance(plan, ReviewableActionPlan):
        raise TypeError("plan 必须是 ReviewableActionPlan")
    if plan.plan_hash != _digest(plan.without_hash_dict()):
        raise ActionPlanError("plan_hash_mismatch", "计划对象的 plan_hash 与内容不一致")
    return _json(plan.to_dict()) + "\n"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ActionPlanError("duplicate_json_key", f"JSON 字段重复: {key}")
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise ValueError(value)


def parse_plan_json(payload: str, *, registry: ToolRegistry) -> ReviewableActionPlan:
    if not isinstance(payload, str) or not isinstance(registry, ToolRegistry):
        raise TypeError("必须提供 JSON 字符串和 ToolRegistry")
    try:
        decoded = json.loads(payload, object_pairs_hook=_pairs, parse_constant=_constant)
    except ActionPlanError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ActionPlanError("invalid_json", "plan.json 不是严格 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ActionPlanError("invalid_json", "plan.json 顶层必须是对象")
    unknown = sorted(set(decoded) - _PLAN_FIELDS)
    if unknown:
        raise ActionPlanError("unknown_field", f"plan.json 包含未知字段: {', '.join(unknown)}")
    missing = sorted(_PLAN_FIELDS - set(decoded))
    if missing:
        raise ActionPlanError("missing_field", f"plan.json 缺少字段: {', '.join(missing)}")
    if decoded["schema_version"] != ACTION_PLAN_SCHEMA_VERSION:
        raise ActionPlanError("unsupported_schema_version", f"不支持 schema_version={decoded['schema_version']!r}")
    if decoded["format_version"] != ACTION_PLAN_FORMAT_VERSION:
        raise ActionPlanError("unsupported_format_version", f"不支持 format_version={decoded['format_version']!r}")
    supplied = _hash("plan_hash", decoded["plan_hash"])
    unhashed = {key: item for key, item in decoded.items() if key != "plan_hash"}
    if _digest(unhashed) != supplied:
        raise ActionPlanError("plan_hash_mismatch", "plan.json 内容与 plan_hash 不一致")
    raw_actions = decoded["actions"]
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ActionPlanError("invalid_field", "actions 必须是非空数组")
    actions = tuple(_parse_action(item, registry) for item in raw_actions)
    plan = ReviewableActionPlan(decoded["plan_id"], decoded["plan_version"], supplied, decoded["task_id"],
                                decoded["created_at"], decoded["goal"], _texts("scope", decoded["scope"], empty=False),
                                _texts("exclusions", decoded["exclusions"], empty=True),
                                _texts("file_rules", decoded["file_rules"], empty=True), decoded["recursive"],
                                decoded["conflict_policy"], actions, decoded["format_version"], decoded["schema_version"])
    if _digest(plan.without_hash_dict()) != plan.plan_hash:
        raise ActionPlanError("plan_hash_mismatch", "解析后的计划事实与 plan_hash 不一致")
    return plan


def _parse_action(payload: Any, registry: ToolRegistry) -> PlanAction:
    if not isinstance(payload, Mapping):
        raise ActionPlanError("invalid_field", "action 必须是对象")
    _fields(payload, _ACTION_FIELDS, "action")
    name = _identifier("tool_name", payload["tool_name"])
    try:
        manifest = registry.require(name)
    except ManifestError as exc:
        raise ActionPlanError("tool_not_registered", f"未注册工具: {name}") from exc
    if (payload["tool_version"], payload["manifest_hash"], payload["risk_level"], payload["requires_confirmation"]) != (
        manifest.version, manifest.content_hash, manifest.risk_level, manifest.requires_confirmation):
        raise ActionPlanError("manifest_identity_mismatch", "动作事实与本地 ToolRegistry 不一致")
    raw_snapshots = payload["path_snapshots"]
    if not isinstance(raw_snapshots, list):
        raise ActionPlanError("invalid_field", "path_snapshots 必须是数组")
    return PlanAction(payload["order"], payload["action_id"], name, payload["tool_version"], payload["manifest_hash"],
                      payload["risk_level"], payload["requires_confirmation"], payload["normalized_arguments"],
                      tuple(_parse_snapshot(item) for item in raw_snapshots), _parse_decision(payload["policy_decision"]),
                      payload["preview"], payload["verification_method"], payload["rollback_strategy"])


def _parse_decision(payload: Any) -> PolicyDecision:
    if not isinstance(payload, Mapping):
        raise ActionPlanError("invalid_field", "policy_decision 必须是对象")
    _fields(payload, _POLICY_FIELDS, "policy_decision")
    paths = payload["normalized_paths"]
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        raise ActionPlanError("invalid_field", "normalized_paths 必须是字符串数组")
    try:
        return PolicyDecision(payload["decision"], payload["reason_code"], payload["reason"], payload["policy_version"],
                              payload["tool_name"], payload["tool_version"], payload["manifest_hash"], payload["risk_level"],
                              payload["requires_confirmation"], tuple(paths))
    except (TypeError, ValueError) as exc:
        raise ActionPlanError("invalid_field", f"policy_decision 无效: {exc}") from exc


def _identity(value: Any, name: str, optional: bool = False) -> tuple[int, int] | None:
    if optional and value is None:
        return None
    if not isinstance(value, list) or len(value) != 2 or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in value):
        raise ActionPlanError("invalid_field", f"{name} 必须是两个非负整数")
    return value[0], value[1]


def _parse_snapshot(payload: Any) -> PathSnapshot:
    if not isinstance(payload, Mapping):
        raise ActionPlanError("invalid_field", "PathSnapshot 必须是对象")
    _fields(payload, _PATH_FIELDS, "PathSnapshot")
    components = payload["component_identities"]
    if not isinstance(components, list):
        raise ActionPlanError("invalid_field", "component_identities 必须是数组")
    parsed = []
    for item in components:
        if not isinstance(item, Mapping):
            raise ActionPlanError("invalid_field", "component identity 必须是对象")
        _fields(item, frozenset({"path", "identity"}), "component identity")
        ident = _identity(item["identity"], "component identity"); assert ident is not None
        parsed.append((_text("component path", item["path"]), ident))
    exists = _boolean("exists", payload["exists"])
    target = _identity(payload["target_identity"], "target_identity", optional=True)
    if exists != (target is not None):
        raise ActionPlanError("invalid_field", "exists 与 target_identity 不一致")
    anchor = _identity(payload["anchor_identity"], "anchor_identity"); assert anchor is not None
    return PathSnapshot(_text("requested_path", payload["requested_path"]), _text("purpose", payload["purpose"], 128),
                        _text("normalized_path", payload["normalized_path"]), _text("resolved_path", payload["resolved_path"]),
                        exists, target, _text("anchor_path", payload["anchor_path"]), anchor, tuple(parsed))

def _notes(value: Any) -> str:
    if not isinstance(value, str):
        raise ActionPlanError("markdown_structure_invalid", "用户备注必须是文本")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > 4096:
        raise ActionPlanError("markdown_notes_too_large", "用户备注超过 4096 字符")
    lowered = value.casefold()
    if "\x00" in value or _NOTES_START in value or _NOTES_END in value:
        raise ActionPlanError("markdown_structure_invalid", "用户备注包含非法结构")
    if "```" in value or "~~~" in value or any(x in lowered for x in ("<script", "powershell.exe", "cmd.exe", ".bat", ".cmd")):
        raise ActionPlanError("markdown_script_denied", "Markdown 用户备注不得嵌入脚本或代码围栏")
    return value


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_execution_plan_markdown(plan: ReviewableActionPlan, *, user_notes: str = "") -> str:
    if not isinstance(plan, ReviewableActionPlan):
        raise TypeError("plan 必须是 ReviewableActionPlan")
    serialize_plan_json(plan); user_notes = _notes(user_notes)
    lines = ["# 执行计划（仅供审查，不代表授权）", "", f"- 计划 ID：{plan.plan_id}",
             f"- 计划版本：{plan.plan_version}", f"- 计划哈希：{plan.plan_hash}",
             f"- 格式版本：{plan.format_version}", f"- 创建时间：{plan.created_at}", f"- 目标：{plan.goal}",
             f"- 范围：{'；'.join(plan.scope)}", f"- 排除项：{'；'.join(plan.exclusions) if plan.exclusions else '无'}",
             f"- 文件规则：{'；'.join(plan.file_rules) if plan.file_rules else '无'}",
             f"- 递归：{'是' if plan.recursive else '否'}", f"- 冲突策略：{plan.conflict_policy}", "",
             "## 冻结动作", "", "| 顺序 | action_id | 工具 | 风险 | 策略 | 规范化路径 |",
             "|---:|---|---|---|---|---|"]
    for action in plan.actions:
        paths = "；".join(action.policy_decision.normalized_paths) or "无"
        lines.append(f"| {action.order} | {_cell(action.action_id)} | {_cell(action.tool_name)} | {action.risk_level} | {action.policy_decision.decision} | {_cell(paths)} |")
        lines += [f"  - Manifest：{action.tool_version} / {action.manifest_hash}",
                  f"  - Policy：{action.policy_decision.policy_version} / {action.policy_decision.reason_code}",
                  f"  - 参数：{_json(_thaw(action.normalized_arguments))}", f"  - 预览：{_json(_thaw(action.preview))}",
                  f"  - 验证：{action.verification_method}", f"  - 回滚：{action.rollback_strategy}"]
    lines += ["", "## 用户备注（不属于 plan.json，不改变安全字段，不产生授权）", "", _NOTES_START, user_notes, _NOTES_END, ""]
    return "\n".join(lines)


def parse_execution_plan_markdown(payload: str, *, expected_plan: ReviewableActionPlan) -> ExecutionPlanReview:
    if not isinstance(payload, str) or not isinstance(expected_plan, ReviewableActionPlan):
        raise TypeError("必须提供 Markdown 字符串和 ReviewableActionPlan")
    normalized = payload.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.count(_NOTES_START) != 1 or normalized.count(_NOTES_END) != 1:
        raise ActionPlanError("markdown_structure_invalid", "Markdown 用户备注标记必须各出现一次")
    start, end = normalized.index(_NOTES_START) + len(_NOTES_START), normalized.index(_NOTES_END)
    if end < start:
        raise ActionPlanError("markdown_structure_invalid", "Markdown 用户备注标记顺序无效")
    raw = normalized[start:end]
    if raw.startswith("\n"): raw = raw[1:]
    if raw.endswith("\n"): raw = raw[:-1]
    user_notes = _notes(raw)
    if normalized != render_execution_plan_markdown(expected_plan, user_notes=user_notes):
        raise ActionPlanError("markdown_security_mismatch", "Markdown 的冻结安全字段或动作表已被修改")
    return ExecutionPlanReview(expected_plan.plan_id, expected_plan.plan_version, expected_plan.plan_hash, user_notes)