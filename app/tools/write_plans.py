"""P4 批次 3：受控副作用工具的纯计划生成与预览。

本模块不会读取或改变文件系统，也不会启动应用。路径类工具必须注入
``path_validator``；它目前是下一批 PathGuard 的窄接口占位。生成结果只描述
候选动作，不能被当作授权或执行入口。
"""

from __future__ import annotations

import hashlib
import ntpath
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .manifest import ManifestError, ToolRegistry

PathValidator = Callable[[str, str], Path]
ApplicationResolver = Callable[[str], str]

_PLANNABLE_TOOLS = frozenset({
    "create_text_file",
    "write_text_file",
    "copy_file",
    "move_file",
    "rename_file",
    "move_to_recycle_bin",
    "start_whitelisted_application",
})
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_APPLICATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_INVALID_WINDOWS_NAME_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


class WritePlanError(ValueError):
    """计划生成阶段的稳定、结构化拒绝。"""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        self.code = code
        self.message = message
        self.field = field
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            result["field"] = self.field
        return result


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class WriteActionPreview:
    """不可执行的候选动作；只携带结构化参数和审查摘要。"""

    tool_name: str
    tool_version: str
    manifest_hash: str
    risk_level: str
    requires_confirmation: bool
    normalized_arguments: Mapping[str, Any]
    preview: Mapping[str, Any]
    verification_method: str
    rollback_strategy: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "normalized_arguments", _freeze(self.normalized_arguments))
        object.__setattr__(self, "preview", _freeze(self.preview))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "manifest_hash": self.manifest_hash,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "normalized_arguments": _thaw(self.normalized_arguments),
            "preview": _thaw(self.preview),
            "verification_method": self.verification_method,
            "rollback_strategy": self.rollback_strategy,
            "preview_only": True,
        }


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise WritePlanError("invalid_argument", "arguments 必须是对象")
    if not all(isinstance(key, str) for key in arguments):
        raise WritePlanError("invalid_argument", "arguments 字段名必须是字符串")
    return dict(arguments)


def _require_fields(
    arguments: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    names = set(arguments)
    missing = sorted(required - names)
    if missing:
        raise WritePlanError("invalid_argument", f"缺少字段: {', '.join(missing)}")
    unknown = sorted(names - required - optional)
    if unknown:
        raise WritePlanError("invalid_argument", f"未知字段: {', '.join(unknown)}")


def _require_string(name: str, value: Any, *, allow_empty: bool = False, maximum: int | None = None) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise WritePlanError("invalid_argument", f"{name} 必须是有效字符串", field=name)
    if maximum is not None and len(value) > maximum:
        raise WritePlanError("limit_exceeded", f"{name} 超过长度上限", field=name)
    return value


def _optional_bool(arguments: Mapping[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise WritePlanError("invalid_argument", f"{name} 必须是布尔值", field=name)
    return value


def _require_hash(name: str, value: Any) -> str:
    text = _require_string(name, value)
    if not _HASH_PATTERN.fullmatch(text):
        raise WritePlanError("invalid_argument", f"{name} 必须是小写 SHA-256", field=name)
    return text


def _validate_path(path_validator: PathValidator, value: Any, purpose: str, field: str) -> str:
    text = _require_string(field, value, maximum=32767)
    checked = path_validator(text, purpose)
    if not isinstance(checked, Path):
        raise WritePlanError("path_validator_error", "path_validator 必须返回 pathlib.Path", field=field)
    return str(checked)


def _same_windows_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def _encode_text(content: str, encoding: str, maximum: int) -> bytes:
    try:
        encoded = content.encode(encoding)
    except UnicodeEncodeError as exc:
        raise WritePlanError("encoding_error", f"内容不能使用 {encoding} 编码", field="content") from exc
    if len(encoded) > maximum:
        raise WritePlanError("limit_exceeded", "文本内容超过 10 MiB 字节上限", field="content")
    return encoded


def _create_preview(arguments: dict[str, Any], path_validator: PathValidator, maximum: int) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_fields(arguments, required=frozenset({"path", "content", "encoding"}), optional=frozenset({"create_parent"}))
    path = _validate_path(path_validator, arguments["path"], "create_target", "path")
    content = _require_string("content", arguments["content"], allow_empty=True)
    encoding = arguments["encoding"]
    if encoding not in {"utf-8", "gbk"}:
        raise WritePlanError("invalid_argument", "encoding 只允许 utf-8 或 gbk", field="encoding")
    create_parent = _optional_bool(arguments, "create_parent", False)
    encoded = _encode_text(content, encoding, maximum)
    normalized = {"path": path, "content": content, "encoding": encoding, "create_parent": create_parent}
    preview = {
        "path": path,
        "encoding": encoding,
        "size_bytes": len(encoded),
        "content_hash": _sha256(encoded),
        "create_parent": create_parent,
        "expected_target_state": "absent",
        "conflict_policy": "deny",
    }
    return normalized, preview


def _write_preview(arguments: dict[str, Any], path_validator: PathValidator, maximum: int) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_fields(
        arguments,
        required=frozenset({"path", "mode", "content", "expected_source_hash"}),
        optional=frozenset({"target_text"}),
    )
    path = _validate_path(path_validator, arguments["path"], "write_target", "path")
    mode = arguments["mode"]
    if mode not in {"append", "replace_exact"}:
        raise WritePlanError("invalid_argument", "mode 只允许 append 或 replace_exact", field="mode")
    content = _require_string("content", arguments["content"], allow_empty=True)
    content_bytes = _encode_text(content, "utf-8", maximum)
    expected_source_hash = _require_hash("expected_source_hash", arguments["expected_source_hash"])
    target_text = arguments.get("target_text")
    if mode == "replace_exact":
        target_text = _require_string("target_text", target_text)
    elif target_text is not None:
        raise WritePlanError("invalid_argument", "append 模式不得提供 target_text", field="target_text")
    normalized: dict[str, Any] = {
        "path": path,
        "mode": mode,
        "content": content,
        "expected_source_hash": expected_source_hash,
    }
    preview: dict[str, Any] = {
        "path": path,
        "mode": mode,
        "expected_source_hash": expected_source_hash,
        "content_size_bytes": len(content_bytes),
        "content_hash": _sha256(content_bytes),
        "whole_file_rewrite": False,
    }
    if target_text is not None:
        normalized["target_text"] = target_text
        target_bytes = target_text.encode("utf-8")
        preview["target_text_hash"] = _sha256(target_bytes)
        preview["target_text_size_bytes"] = len(target_bytes)
        preview["replacement_occurrence_policy"] = "exactly_one"
    return normalized, preview


def _transfer_preview(tool_name: str, arguments: dict[str, Any], path_validator: PathValidator) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_fields(
        arguments,
        required=frozenset({"source", "destination", "conflict_policy"}),
        optional=frozenset({"recursive"}),
    )
    source = _validate_path(path_validator, arguments["source"], f"{tool_name}_source", "source")
    destination = _validate_path(path_validator, arguments["destination"], f"{tool_name}_destination", "destination")
    if _same_windows_path(source, destination):
        raise WritePlanError("source_equals_destination", "源路径和目标路径不得相同")
    if arguments["conflict_policy"] != "deny":
        raise WritePlanError("invalid_argument", "conflict_policy 只允许 deny", field="conflict_policy")
    recursive = _optional_bool(arguments, "recursive", False)
    normalized = {
        "source": source,
        "destination": destination,
        "recursive": recursive,
        "conflict_policy": "deny",
    }
    preview: dict[str, Any] = {
        **normalized,
        "overwrite": False,
        "max_items": 20,
    }
    if tool_name == "move_file":
        preview["cross_volume_strategy"] = "copy_verify_then_remove_source"
        preview["remove_source_only_after_verification"] = True
    else:
        preview["reparse_target"] = "deny"
    return normalized, preview


def _validate_new_name(value: Any) -> str:
    name = _require_string("new_name", value, maximum=255)
    if name in {".", ".."} or name[-1] in {" ", "."} or any(char in _INVALID_WINDOWS_NAME_CHARS for char in name):
        raise WritePlanError("invalid_argument", "new_name 不是有效的 Windows 文件名", field="new_name")
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        raise WritePlanError("invalid_argument", "new_name 是 Windows 保留名称", field="new_name")
    return name


def _rename_preview(arguments: dict[str, Any], path_validator: PathValidator) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_fields(arguments, required=frozenset({"path", "new_name"}), optional=frozenset({"allow_extension_change"}))
    source = _validate_path(path_validator, arguments["path"], "rename_source", "path")
    new_name = _validate_new_name(arguments["new_name"])
    allow_extension_change = _optional_bool(arguments, "allow_extension_change", False)
    source_path = Path(source)
    candidate = source_path.parent / new_name
    target = _validate_path(path_validator, str(candidate), "rename_target", "new_name")
    if not _same_windows_path(str(Path(target).parent), str(source_path.parent)):
        raise WritePlanError("invalid_argument", "重命名不得改变父目录", field="new_name")
    old_extension = source_path.suffix.casefold()
    new_extension = Path(target).suffix.casefold()
    extension_changed = old_extension != new_extension
    if extension_changed and not allow_extension_change:
        raise WritePlanError("extension_change_denied", "默认禁止改变扩展名", field="new_name")
    if _same_windows_path(source, target):
        raise WritePlanError("source_equals_destination", "新名称与原名称相同")
    normalized = {"path": source, "new_name": new_name, "allow_extension_change": allow_extension_change}
    preview = {
        "old_path": source,
        "new_path": target,
        "same_parent": True,
        "extension_changed": extension_changed,
        "conflict_policy": "deny",
    }
    return normalized, preview


def _recycle_preview(arguments: dict[str, Any], path_validator: PathValidator, maximum: int) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_fields(arguments, required=frozenset({"paths"}), optional=frozenset({"recursive"}))
    values = arguments["paths"]
    if not isinstance(values, list) or not values:
        raise WritePlanError("invalid_argument", "paths 必须是非空数组", field="paths")
    if len(values) > maximum:
        raise WritePlanError("limit_exceeded", f"paths 最多允许 {maximum} 项", field="paths")
    checked: list[str] = []
    identities: set[str] = set()
    for value in values:
        path = _validate_path(path_validator, value, "recycle_target", "paths")
        identity = ntpath.normcase(ntpath.normpath(path))
        if identity in identities:
            raise WritePlanError("duplicate_path", "paths 不得包含重复路径", field="paths")
        identities.add(identity)
        checked.append(path)
    recursive = _optional_bool(arguments, "recursive", False)
    normalized = {"paths": checked, "recursive": recursive}
    preview = {
        "paths": checked,
        "item_count": len(checked),
        "recursive": recursive,
        "permanent_delete": False,
        "reparse_target": "deny",
    }
    return normalized, preview


def _application_preview(arguments: dict[str, Any], resolver: ApplicationResolver) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_fields(arguments, required=frozenset({"application_id"}))
    application_id = _require_string("application_id", arguments["application_id"], maximum=64)
    if not _APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise WritePlanError("invalid_argument", "application_id 格式无效", field="application_id")
    try:
        application_name = resolver(application_id)
    except (KeyError, LookupError) as exc:
        raise WritePlanError("application_not_whitelisted", "应用不在本地白名单中", field="application_id") from exc
    if not isinstance(application_name, str) or not application_name.strip():
        raise WritePlanError("application_registry_error", "应用白名单返回了非法登记项")
    normalized = {"application_id": application_id}
    preview = {"application_id": application_id, "application_name": application_name.strip()}
    return normalized, preview


def build_write_action_preview(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    registry: ToolRegistry,
    path_validator: PathValidator | None = None,
    application_resolver: ApplicationResolver | None = None,
) -> WriteActionPreview:
    """校验并生成一个不可执行的副作用动作预览。"""

    if not isinstance(tool_name, str) or not tool_name:
        raise WritePlanError("unknown_tool", "tool_name 必须是已注册工具名")
    try:
        manifest = registry.require(tool_name)
    except ManifestError as exc:
        raise WritePlanError("unknown_tool", f"未注册工具: {tool_name!r}") from exc
    if tool_name not in _PLANNABLE_TOOLS:
        raise WritePlanError("tool_not_plannable", f"工具 {tool_name!r} 不是副作用计划工具")
    supplied = _require_arguments(arguments)

    if manifest.allowed_path_scopes and path_validator is None:
        raise TypeError("路径类计划必须显式提供 path_validator")

    if tool_name == "create_text_file":
        assert path_validator is not None
        normalized, preview = _create_preview(supplied, path_validator, int(manifest.limits["max_file_bytes"]))
    elif tool_name == "write_text_file":
        assert path_validator is not None
        normalized, preview = _write_preview(supplied, path_validator, int(manifest.limits["max_file_bytes"]))
    elif tool_name in {"copy_file", "move_file"}:
        assert path_validator is not None
        normalized, preview = _transfer_preview(tool_name, supplied, path_validator)
    elif tool_name == "rename_file":
        assert path_validator is not None
        normalized, preview = _rename_preview(supplied, path_validator)
    elif tool_name == "move_to_recycle_bin":
        assert path_validator is not None
        normalized, preview = _recycle_preview(supplied, path_validator, int(manifest.limits["max_items"]))
    else:
        if application_resolver is None:
            raise TypeError("应用启动计划必须显式提供 application_resolver")
        normalized, preview = _application_preview(supplied, application_resolver)

    return WriteActionPreview(
        tool_name=manifest.name,
        tool_version=manifest.version,
        manifest_hash=manifest.content_hash,
        risk_level=manifest.risk_level,
        requires_confirmation=manifest.requires_confirmation,
        normalized_arguments=normalized,
        preview=preview,
        verification_method=manifest.verification_method,
        rollback_strategy=manifest.rollback_strategy,
    )