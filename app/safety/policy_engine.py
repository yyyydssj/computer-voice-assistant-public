"""P4 批次 5：只读取可信结构化快照的拒绝优先策略引擎。

本模块只产生 ``allow``、``deny`` 或 ``require_confirmation`` 决策。它不解释
自然语言、不生成授权、不消费确认，也不执行任何工具或文件动作。
"""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from app.tools.manifest import ManifestError, ToolManifest, ToolRegistry

from .path_guard import DEFAULT_ALLOWED_ROOT, PathGuardError, PathSnapshot

POLICY_VERSION = "1.0.0"

_ALLOWED_DECISIONS = frozenset({"allow", "deny", "require_confirmation"})
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH_KEYS = ("path", "root", "source", "destination", "paths")
_RESERVED_POLICY_FIELDS = frozenset({
    "allow",
    "allowed_path_scopes",
    "allowed_roots",
    "authorization",
    "authorize",
    "command",
    "confirmation",
    "manifest_hash",
    "policy",
    "policy_version",
    "requires_confirmation",
    "risk",
    "risk_level",
    "shell",
    "system_blacklist",
    "tool_name",
    "tool_version",
})

PathRevalidator = Callable[[PathSnapshot], PathSnapshot]


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("normalized_arguments 字段名必须是字符串")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"normalized_arguments 只允许 JSON 值，收到 {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value.replace("/", "\\")))


def _is_within_allowed_root(value: str) -> bool:
    candidate = _path_key(value)
    allowed = _path_key(DEFAULT_ALLOWED_ROOT)
    try:
        return ntpath.commonpath((allowed, candidate)) == allowed
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """由本地可信代码构造、已经过 Schema 和 PathGuard 的策略输入。"""

    tool_name: str
    tool_version: str
    manifest_hash: str
    risk_level: str
    requires_confirmation: bool
    normalized_arguments: Mapping[str, Any]
    path_snapshots: tuple[PathSnapshot, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise TypeError("tool_name 必须是非空字符串")
        if not isinstance(self.tool_version, str) or not self.tool_version:
            raise TypeError("tool_version 必须是非空字符串")
        if not isinstance(self.manifest_hash, str) or not _HASH_PATTERN.fullmatch(self.manifest_hash):
            raise TypeError("manifest_hash 必须是 sha256 哈希")
        if not isinstance(self.risk_level, str) or not self.risk_level:
            raise TypeError("risk_level 必须是非空字符串")
        if not isinstance(self.requires_confirmation, bool):
            raise TypeError("requires_confirmation 必须是布尔值")
        if not isinstance(self.normalized_arguments, Mapping):
            raise TypeError("normalized_arguments 必须是对象")
        snapshots = tuple(self.path_snapshots)
        if any(not isinstance(item, PathSnapshot) for item in snapshots):
            raise TypeError("path_snapshots 只能包含 PathSnapshot")
        object.__setattr__(self, "normalized_arguments", _freeze_json(self.normalized_arguments))
        object.__setattr__(self, "path_snapshots", snapshots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "manifest_hash": self.manifest_hash,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "normalized_arguments": _thaw_json(self.normalized_arguments),
            "path_snapshots": [item.to_dict() for item in self.path_snapshots],
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """不可变、可序列化且不携带授权或执行能力的策略事实。"""

    decision: str
    reason_code: str
    reason: str
    policy_version: str
    tool_name: str
    tool_version: str
    manifest_hash: str
    risk_level: str
    requires_confirmation: bool
    normalized_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in _ALLOWED_DECISIONS:
            raise ValueError(f"未知策略决策: {self.decision!r}")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise TypeError("reason_code 必须是非空字符串")
        if not isinstance(self.reason, str) or not self.reason:
            raise TypeError("reason 必须是非空字符串")
        object.__setattr__(self, "normalized_paths", tuple(self.normalized_paths))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "manifest_hash": self.manifest_hash,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "normalized_paths": list(self.normalized_paths),
        }


class PolicyEngine:
    """以本地注册表和当前路径事实为唯一权限来源的固定策略。"""

    __slots__ = ("_registry", "_path_revalidator")

    def __init__(self, registry: ToolRegistry, *, path_revalidator: PathRevalidator | None = None) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry 必须是 ToolRegistry")
        if path_revalidator is not None and not callable(path_revalidator):
            raise TypeError("path_revalidator 必须可调用")
        self._registry = registry
        self._path_revalidator = path_revalidator

    def evaluate(self, policy_input: PolicyInput) -> PolicyDecision:
        """评估一个可信输入快照；任何不一致都返回稳定的拒绝事实。"""

        if not isinstance(policy_input, PolicyInput):
            raise TypeError("PolicyEngine 只接受 PolicyInput，不接受自然语言或普通映射")
        try:
            manifest = self._registry.require(policy_input.tool_name)
        except ManifestError:
            return self._deny(policy_input, "tool_not_registered", "工具未在本地固定注册表中登记")

        identity_error = self._validate_manifest_identity(policy_input, manifest)
        if identity_error is not None:
            return identity_error
        reserved = self._find_reserved_field(policy_input.normalized_arguments)
        if reserved is not None:
            return self._deny(
                policy_input,
                "reserved_policy_field",
                f"参数包含不得由不可信输入控制的策略字段: {reserved}",
                manifest=manifest,
            )
        path_result = self._validate_paths(policy_input, manifest)
        if isinstance(path_result, PolicyDecision):
            return path_result
        if manifest.risk_level in {"L0", "L1"}:
            return self._decision(
                manifest,
                decision="allow",
                reason_code="policy_allows_without_confirmation",
                reason="固定策略允许该工具在无需确认的情况下继续形成计划",
                normalized_paths=path_result,
            )
        return self._decision(
            manifest,
            decision="require_confirmation",
            reason_code="confirmation_required_by_policy",
            reason=f"固定策略要求 {manifest.risk_level} 工具在执行前完成对应确认",
            normalized_paths=path_result,
        )

    def _validate_manifest_identity(self, value: PolicyInput, manifest: ToolManifest) -> PolicyDecision | None:
        checks = (
            (value.tool_version != manifest.version, "tool_version_mismatch", "工具版本与本地注册表不一致"),
            (value.manifest_hash != manifest.content_hash, "manifest_hash_mismatch", "工具 Manifest 哈希与本地注册表不一致"),
            (value.risk_level != manifest.risk_level, "risk_level_mismatch", "风险等级不得由调用方覆盖"),
            (
                value.requires_confirmation is not manifest.requires_confirmation,
                "confirmation_requirement_mismatch",
                "确认要求不得由调用方降低或覆盖",
            ),
        )
        for failed, code, message in checks:
            if failed:
                return self._deny(value, code, message, manifest=manifest)
        return None

    def _validate_paths(self, value: PolicyInput, manifest: ToolManifest) -> tuple[str, ...] | PolicyDecision:
        expects_paths = bool(manifest.allowed_path_scopes)
        if not expects_paths:
            if value.path_snapshots:
                return self._deny(value, "unexpected_path_snapshot", "非路径工具不得携带路径快照", manifest=manifest)
            return ()
        if not value.path_snapshots:
            return self._deny(value, "path_snapshot_missing", "路径工具必须携带当前 PathGuard 快照", manifest=manifest)
        for item in value.path_snapshots:
            if not _is_within_allowed_root(item.normalized_path) or not _is_within_allowed_root(item.resolved_path):
                return self._deny(value, "path_outside_allowed_scope", "路径快照越出固定允许根目录", manifest=manifest)
        try:
            expected_paths = self._expected_paths(value.tool_name, value.normalized_arguments)
        except (TypeError, ValueError) as exc:
            return self._deny(value, "invalid_policy_input", str(exc), manifest=manifest)

        supplied_by_key: dict[str, PathSnapshot] = {}
        for item in value.path_snapshots:
            key = _path_key(item.normalized_path)
            if key in supplied_by_key:
                return self._deny(value, "duplicate_path_snapshot", "同一路径不得重复提供 PathGuard 快照", manifest=manifest)
            supplied_by_key[key] = item
        expected_keys = tuple(_path_key(item) for item in expected_paths)
        if set(expected_keys) != set(supplied_by_key):
            return self._deny(
                value,
                "path_snapshot_mismatch",
                "规范化参数中的路径与 PathGuard 快照不完全一致",
                manifest=manifest,
            )
        if self._path_revalidator is None:
            return self._deny(value, "path_revalidator_missing", "路径策略评估必须显式提供 PathGuard.revalidate", manifest=manifest)
        try:
            for key in expected_keys:
                supplied = supplied_by_key[key]
                current = self._path_revalidator(supplied)
                if not isinstance(current, PathSnapshot) or current != supplied:
                    raise PathGuardError(
                        "path_snapshot_changed",
                        "路径复检结果与规划快照不一致",
                        path=supplied.normalized_path,
                        purpose=supplied.purpose,
                    )
        except PathGuardError as exc:
            return self._deny(value, "path_snapshot_changed", f"PathGuard 复检拒绝: {exc.code}", manifest=manifest)
        except Exception as exc:
            return self._deny(value, "path_snapshot_changed", f"PathGuard 复检异常: {type(exc).__name__}", manifest=manifest)
        return tuple(supplied_by_key[key].normalized_path for key in expected_keys)

    @staticmethod
    def _expected_paths(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        values: list[str] = []
        for key in _PATH_KEYS:
            if key not in arguments:
                continue
            raw = arguments[key]
            if key == "paths":
                if not isinstance(raw, tuple) or not raw or any(not isinstance(item, str) for item in raw):
                    raise TypeError("paths 必须是非空字符串数组")
                values.extend(raw)
            else:
                if not isinstance(raw, str) or not raw:
                    raise TypeError(f"{key} 必须是非空路径字符串")
                values.append(raw)
        if tool_name == "rename_file":
            source = arguments.get("path")
            new_name = arguments.get("new_name")
            if not isinstance(source, str) or not isinstance(new_name, str) or not new_name:
                raise TypeError("rename_file 必须包含 path 和 new_name")
            values.append(ntpath.join(ntpath.dirname(source), new_name))
        if not values:
            raise ValueError("路径工具的规范化参数中没有可验证路径")
        return tuple(values)

    @staticmethod
    def _find_reserved_field(value: Mapping[str, Any]) -> str | None:
        for key, item in value.items():
            if key.casefold() in _RESERVED_POLICY_FIELDS:
                return key
            if isinstance(item, Mapping):
                nested = PolicyEngine._find_reserved_field(item)
                if nested is not None:
                    return nested
        return None

    @staticmethod
    def _deny(
        value: PolicyInput,
        reason_code: str,
        reason: str,
        *,
        manifest: ToolManifest | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            decision="deny",
            reason_code=reason_code,
            reason=reason,
            policy_version=POLICY_VERSION,
            tool_name=value.tool_name,
            tool_version=manifest.version if manifest is not None else value.tool_version,
            manifest_hash=manifest.content_hash if manifest is not None else value.manifest_hash,
            risk_level=manifest.risk_level if manifest is not None else value.risk_level,
            requires_confirmation=manifest.requires_confirmation if manifest is not None else value.requires_confirmation,
            normalized_paths=tuple(item.normalized_path for item in value.path_snapshots),
        )

    @staticmethod
    def _decision(
        manifest: ToolManifest,
        *,
        decision: str,
        reason_code: str,
        reason: str,
        normalized_paths: tuple[str, ...],
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=decision,
            reason_code=reason_code,
            reason=reason,
            policy_version=POLICY_VERSION,
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_paths=normalized_paths,
        )
