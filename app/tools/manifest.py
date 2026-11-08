"""P4 批次 1：只读、可审查的工具 Manifest 与注册表。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_RISKS = frozenset({"L0", "L1", "L2", "L3"})
_ALLOWED_IDEMPOTENCY = frozenset({"idempotent", "conditional", "non_idempotent"})
_ALLOWED_CANCELLATION = frozenset({"not_supported", "cooperative"})
_APPROVED_TOOL_NAMES = frozenset({
    "list_directory",
    "read_text_file",
    "search_files",
    "get_file_info",
    "get_system_info",
    "get_processes",
    "create_text_file",
    "write_text_file",
    "copy_file",
    "move_file",
    "rename_file",
    "move_to_recycle_bin",
    "start_whitelisted_application",
})

_FORBIDDEN_TOOL_NAMES = frozenset({
    "run_command",
    "run_controlled_powershell",
    "powershell",
    "shell",
    "python",
    "terminate_process",
})
_APPROVED_ROOT = PureWindowsPath(r"G:\study")
_DEFAULT_WORKDIR = PureWindowsPath(r"G:\study\AI工作区")


class ManifestError(ValueError):
    """Manifest 缺失、被篡改或违反 P4 安全边界。"""


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ManifestError(f"Manifest 只允许 JSON 值，收到 {type(value).__name__}")


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _require_text(name: str, value: str, *, maximum: int = 4096) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ManifestError(f"{name} 必须是 1..{maximum} 个字符")


def _require_positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ManifestError(f"{name} 必须是正整数")


def _validate_schema(name: str, schema: Mapping[str, Any]) -> None:
    if not isinstance(schema, Mapping):
        raise ManifestError(f"{name} 必须是 JSON Schema 对象")
    if schema.get("type") != "object":
        raise ManifestError(f"{name}.type 必须是 object")
    if schema.get("additionalProperties") is not False:
        raise ManifestError(f"{name}.additionalProperties 必须为 false")
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise ManifestError(f"{name}.properties 必须是对象")
    required = schema.get("required", ())
    if not isinstance(required, (list, tuple)) or any(not isinstance(item, str) for item in required):
        raise ManifestError(f"{name}.required 必须是字符串数组")
    if len(set(required)) != len(required):
        raise ManifestError(f"{name}.required 不得重复")
    unknown_required = sorted(set(required) - set(properties))
    if unknown_required:
        raise ManifestError(f"{name}.required 引用了未知属性: {', '.join(unknown_required)}")


@dataclass(frozen=True, slots=True)
class ToolManifest:
    """不含执行入口的声明式工具能力描述。"""

    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    risk_level: str
    requires_confirmation: bool
    allowed_path_scopes: tuple[str, ...]
    default_working_directory: str | None
    timeout_ms: int
    max_output_bytes: int
    idempotency: str
    cancellation: str
    preview_method: str
    verification_method: str
    rollback_strategy: str
    audit_event_type: str
    limits: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _deep_freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _deep_freeze(self.output_schema))
        object.__setattr__(self, "limits", _deep_freeze(self.limits))
        object.__setattr__(self, "allowed_path_scopes", tuple(self.allowed_path_scopes))

        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ManifestError("name 必须是稳定的小写 snake_case 标识")
        if self.name in _FORBIDDEN_TOOL_NAMES or any(token in self.name for token in ("powershell", "shell", "command")):
            raise ManifestError(f"禁止注册任意命令或进程终止工具: {self.name}")
        if not isinstance(self.version, str) or not _VERSION_PATTERN.fullmatch(self.version):
            raise ManifestError("version 必须使用正整数 SemVer")
        _require_text("description", self.description)
        _validate_schema("input_schema", self.input_schema)
        _validate_schema("output_schema", self.output_schema)
        if self.risk_level == "L4":
            raise ManifestError("L4 工具不得在 MVP 注册")
        if self.risk_level not in _ALLOWED_RISKS:
            raise ManifestError(f"risk_level 不支持 {self.risk_level!r}")
        if not isinstance(self.requires_confirmation, bool):
            raise ManifestError("requires_confirmation 必须是布尔值")
        expected_confirmation = self.risk_level in {"L2", "L3"}
        if self.requires_confirmation is not expected_confirmation:
            raise ManifestError("确认要求与风险等级不一致")
        self._validate_paths()
        _require_positive("timeout_ms", self.timeout_ms)
        _require_positive("max_output_bytes", self.max_output_bytes)
        if self.idempotency not in _ALLOWED_IDEMPOTENCY:
            raise ManifestError(f"idempotency 不支持 {self.idempotency!r}")
        if self.cancellation not in _ALLOWED_CANCELLATION:
            raise ManifestError(f"cancellation 不支持 {self.cancellation!r}")
        _require_text("preview_method", self.preview_method)
        _require_text("verification_method", self.verification_method)
        _require_text("rollback_strategy", self.rollback_strategy)
        _require_text("audit_event_type", self.audit_event_type, maximum=128)
        if not isinstance(self.limits, Mapping):
            raise ManifestError("limits 必须是对象")

    def _validate_paths(self) -> None:
        for scope in self.allowed_path_scopes:
            if not isinstance(scope, str):
                raise ManifestError("allowed_path_scopes 必须是字符串数组")
            parsed = PureWindowsPath(scope)
            if not parsed.is_absolute() or parsed != _APPROVED_ROOT:
                raise ManifestError("allowed_path_scopes 只能声明 G:\\study")
        if len(set(self.allowed_path_scopes)) != len(self.allowed_path_scopes):
            raise ManifestError("allowed_path_scopes 不得重复")
        if self.allowed_path_scopes:
            if self.default_working_directory is None:
                raise ManifestError("文件工具必须声明默认工作目录")
            workdir = PureWindowsPath(self.default_working_directory)
            try:
                workdir.relative_to(_APPROVED_ROOT)
            except ValueError as exc:
                raise ManifestError("默认工作目录必须位于 G:\\study 内") from exc
            if workdir != _DEFAULT_WORKDIR:
                raise ManifestError("MVP 默认工作目录必须是 G:\\study\\AI工作区")
        elif self.default_working_directory is not None:
            raise ManifestError("非路径工具不得声明默认工作目录")

    def _content_payload(self) -> dict[str, Any]:
        return {
            field.name: _deep_thaw(getattr(self, field.name))
            for field in fields(self)
        }

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            self._content_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ToolManifest":
        if not isinstance(payload, Mapping):
            raise ManifestError("ToolManifest 必须是对象")
        expected = {field.name for field in fields(cls)}
        unknown = sorted(set(payload) - expected - {"content_hash"})
        if unknown:
            raise ManifestError(f"未知字段: {', '.join(unknown)}")
        missing = sorted(expected - set(payload))
        if missing:
            raise ManifestError(f"缺少字段: {', '.join(missing)}")
        supplied_hash = payload.get("content_hash")
        if supplied_hash is not None and (not isinstance(supplied_hash, str) or not _HASH_PATTERN.fullmatch(supplied_hash)):
            raise ManifestError("content_hash 格式无效")
        kwargs = {name: payload[name] for name in expected}
        kwargs["allowed_path_scopes"] = tuple(kwargs["allowed_path_scopes"])
        manifest = cls(**kwargs)
        if supplied_hash is not None and supplied_hash != manifest.content_hash:
            raise ManifestError("content_hash 与 Manifest 内容不一致")
        return manifest


class ToolRegistry:
    """构造后不可扩展、不可覆盖的 Manifest 注册表。"""

    __slots__ = ("_manifests", "_by_name", "_sealed")

    def __init__(self, manifests: tuple[ToolManifest, ...]) -> None:
        ordered = tuple(manifests)
        by_name: dict[str, ToolManifest] = {}
        for manifest in ordered:
            if not isinstance(manifest, ToolManifest):
                raise ManifestError("注册表只接受 ToolManifest")
            if manifest.name not in _APPROVED_TOOL_NAMES:
                raise ManifestError(f"未批准工具: {manifest.name}")
            if manifest.name in by_name:
                raise ManifestError(f"重复工具: {manifest.name}")
            by_name[manifest.name] = manifest
        object.__setattr__(self, "_manifests", ordered)
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("ToolRegistry 构造后不可修改")
        object.__setattr__(self, name, value)

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._manifests)

    def manifests(self) -> tuple[ToolManifest, ...]:
        return self._manifests

    def require(self, name: str) -> ToolManifest:
        try:
            return self._by_name[name]
        except (KeyError, TypeError) as exc:
            raise ManifestError(f"未注册工具: {name!r}") from exc
