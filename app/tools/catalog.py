"""P4 批次 1：已批准的 13 个 MVP 工具 Manifest。"""

from __future__ import annotations

from typing import Any

from .manifest import ToolManifest, ToolRegistry

ROOT = (r"G:\study",)
WORKDIR = r"G:\study\AI工作区"
MIB = 1024 * 1024


def _object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _path_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": 32767}


def _manifest(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    risk_level: str,
    paths: bool,
    timeout_ms: int,
    max_output_bytes: int,
    idempotency: str,
    cancellation: str,
    preview_method: str,
    verification_method: str,
    rollback_strategy: str,
    limits: dict[str, Any],
) -> ToolManifest:
    return ToolManifest(
        name=name,
        version="1.0.0",
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        risk_level=risk_level,
        requires_confirmation=risk_level in {"L2", "L3"},
        allowed_path_scopes=ROOT if paths else (),
        default_working_directory=WORKDIR if paths else None,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        idempotency=idempotency,
        cancellation=cancellation,
        preview_method=preview_method,
        verification_method=verification_method,
        rollback_strategy=rollback_strategy,
        audit_event_type=f"tool.{name}",
        limits=limits,
    )


def build_mvp_tool_registry() -> ToolRegistry:
    """返回固定顺序、不可变且不含执行入口的 MVP 注册表。"""

    path = _path_schema()
    manifests = (
        _manifest(
            name="list_directory",
            description="列出允许根目录内的目录项；默认不递归且隐藏项不显示。",
            input_schema=_object_schema({
                "path": path,
                "recursive": {"type": "boolean"},
                "sort_by": {"type": "string", "enum": ["name", "modified_at", "size"]},
                "include_hidden": {"type": "boolean"},
            }, ("path",)),
            output_schema=_object_schema({
                "items": {"type": "array", "maxItems": 200},
                "truncated": {"type": "boolean"},
            }, ("items", "truncated")),
            risk_level="L0", paths=True, timeout_ms=10_000, max_output_bytes=1 * MIB,
            idempotency="idempotent", cancellation="cooperative",
            preview_method="返回规范化目录和数量上限，不产生副作用。",
            verification_method="结果项必须位于已校验目录内并满足数量上限。",
            rollback_strategy="只读工具，无需撤销。",
            limits={"max_items": 200, "max_depth": 5, "recursive_default": False, "include_hidden_default": False},
        ),
        _manifest(
            name="read_text_file",
            description="读取允许根目录内的受限文本范围，不读取二进制或超大文件。",
            input_schema=_object_schema({
                "path": path,
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1},
                "encoding_hint": {"type": "string", "enum": ["utf-8", "gbk"]},
            }, ("path",)),
            output_schema=_object_schema({
                "text": {"type": "string"},
                "encoding": {"type": "string"},
                "size_bytes": {"type": "integer", "minimum": 0},
                "truncated": {"type": "boolean"},
            }, ("text", "encoding", "size_bytes", "truncated")),
            risk_level="L1", paths=True, timeout_ms=10_000, max_output_bytes=1 * MIB,
            idempotency="idempotent", cancellation="cooperative",
            preview_method="先报告规范化路径、文件大小和将读取的范围。",
            verification_method="验证实际编码、返回范围、大小上限和截断标记。",
            rollback_strategy="只读工具，无需撤销。",
            limits={"max_file_bytes": 10 * MIB, "encoding_order": ["utf-8", "gbk"], "binary": "deny"},
        ),
        _manifest(
            name="search_files",
            description="在明确根目录内按名称、扩展名或普通文本进行受限搜索。",
            input_schema=_object_schema({
                "root": path,
                "pattern": {"type": "string", "minLength": 1, "maxLength": 512},
                "mode": {"type": "string", "enum": ["name", "extension", "content"]},
                "recursive": {"type": "boolean"},
            }, ("root", "pattern", "mode")),
            output_schema=_object_schema({
                "matches": {"type": "array", "maxItems": 100},
                "truncated": {"type": "boolean"},
            }, ("matches", "truncated")),
            risk_level="L1", paths=True, timeout_ms=30_000, max_output_bytes=2 * MIB,
            idempotency="idempotent", cancellation="cooperative",
            preview_method="显示规范化搜索根、搜索模式、递归设置和上限。",
            verification_method="验证每个结果仍位于根内并记录截断和超时事实。",
            rollback_strategy="只读工具，无需撤销。",
            limits={"max_matches": 100, "max_depth": 5, "regex": "deny", "recursive_default": False},
        ),
        _manifest(
            name="get_file_info",
            description="查询受限数量文件或目录的元数据与 reparse 状态，不读取内容。",
            input_schema=_object_schema({"paths": {"type": "array", "items": path, "minItems": 1, "maxItems": 50}}, ("paths",)),
            output_schema=_object_schema({"items": {"type": "array", "maxItems": 50}}, ("items",)),
            risk_level="L0", paths=True, timeout_ms=10_000, max_output_bytes=512 * 1024,
            idempotency="idempotent", cancellation="cooperative",
            preview_method="显示规范化路径清单，不读取文件内容。",
            verification_method="每项明确返回存在性、类型和 link/junction/reparse 标记。",
            rollback_strategy="只读工具，无需撤销。",
            limits={"max_items": 50, "read_content": False},
        ),
        _manifest(
            name="get_system_info",
            description="只返回预定义白名单中的系统信息类别并过滤敏感字段。",
            input_schema=_object_schema({"categories": {"type": "array", "items": {"type": "string", "enum": ["windows", "resources", "disk_space", "workspace", "python"]}, "minItems": 1, "maxItems": 5}}, ("categories",)),
            output_schema=_object_schema({"facts": {"type": "object", "additionalProperties": False, "properties": {}}}, ("facts",)),
            risk_level="L1", paths=False, timeout_ms=10_000, max_output_bytes=256 * 1024,
            idempotency="idempotent", cancellation="cooperative",
            preview_method="显示请求的信息类别白名单。",
            verification_method="拒绝环境变量、密钥、注册表和未声明字段。",
            rollback_strategy="只读工具，无需撤销。",
            limits={"categories": ["windows", "resources", "disk_space", "workspace", "python"], "sensitive_fields": "deny"},
        ),
        _manifest(
            name="get_processes",
            description="查询受限进程摘要，默认不返回命令行、环境变量或完整用户信息。",
            input_schema=_object_schema({
                "name_filter": {"type": "string", "minLength": 1, "maxLength": 260},
                "include_details": {"type": "boolean"},
            }),
            output_schema=_object_schema({"processes": {"type": "array", "maxItems": 200}, "truncated": {"type": "boolean"}}, ("processes", "truncated")),
            risk_level="L1", paths=False, timeout_ms=10_000, max_output_bytes=1 * MIB,
            idempotency="idempotent", cancellation="cooperative",
            preview_method="显示过滤条件和敏感字段排除规则。",
            verification_method="结果只含 PID、名称、资源摘要、路径摘要和启动时间。",
            rollback_strategy="只读工具，无需撤销。",
            limits={"max_items": 200, "command_line": "deny", "environment": "deny", "full_user_info": "deny"},
        ),
        _manifest(
            name="create_text_file",
            description="仅生成创建文本文件的受控动作计划；目标存在时拒绝。",
            input_schema=_object_schema({
                "path": path,
                "content": {"type": "string", "maxLength": 10 * MIB},
                "encoding": {"type": "string", "enum": ["utf-8", "gbk"]},
                "create_parent": {"type": "boolean"},
            }, ("path", "content", "encoding")),
            output_schema=_object_schema({"path": path, "size_bytes": {"type": "integer", "minimum": 0}, "content_hash": {"type": "string"}}, ("path", "size_bytes", "content_hash")),
            risk_level="L2", paths=True, timeout_ms=30_000, max_output_bytes=256 * 1024,
            idempotency="conditional", cancellation="not_supported",
            preview_method="展示规范化目标、内容哈希、编码、字节数和父目录策略。",
            verification_method="未来执行后重读并核对路径、大小、编码和内容哈希。",
            rollback_strategy="未来执行前确认目标不存在；创建后可按独立新计划移入回收站。",
            limits={"max_file_bytes": 10 * MIB, "target_exists": "deny", "create_parent_default": False, "text_only": True},
        ),
        _manifest(
            name="write_text_file",
            description="仅生成追加或指定文本替换的受控动作计划，不允许任意整文件重写。",
            input_schema=_object_schema({
                "path": path,
                "mode": {"type": "string", "enum": ["append", "replace_exact"]},
                "content": {"type": "string", "maxLength": 10 * MIB},
                "expected_source_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "target_text": {"type": "string"},
            }, ("path", "mode", "content", "expected_source_hash")),
            output_schema=_object_schema({"before_hash": {"type": "string"}, "after_hash": {"type": "string"}, "written_bytes": {"type": "integer", "minimum": 0}}, ("before_hash", "after_hash", "written_bytes")),
            risk_level="L2", paths=True, timeout_ms=30_000, max_output_bytes=256 * 1024,
            idempotency="conditional", cancellation="not_supported",
            preview_method="展示模式、精确修改范围、源哈希、内容哈希和预期结果。",
            verification_method="未来执行前后核对源哈希、修改范围和最终哈希。",
            rollback_strategy="未来执行前保存受控备份；恢复必须形成新计划。",
            limits={"max_file_bytes": 10 * MIB, "modes": ["append", "replace_exact"], "whole_file_rewrite": "deny", "overwrite": "deny"},
        ),
        _manifest(
            name="copy_file",
            description="仅生成文件或显式递归目录复制计划；默认拒绝目标冲突和覆盖。",
            input_schema=_object_schema({"source": path, "destination": path, "recursive": {"type": "boolean"}, "conflict_policy": {"type": "string", "enum": ["deny"]}}, ("source", "destination", "conflict_policy")),
            output_schema=_object_schema({"source": path, "destination": path, "size_bytes": {"type": "integer", "minimum": 0}, "content_hash": {"type": "string"}}, ("source", "destination", "size_bytes", "content_hash")),
            risk_level="L2", paths=True, timeout_ms=120_000, max_output_bytes=512 * 1024,
            idempotency="conditional", cancellation="cooperative",
            preview_method="展开源、目标、递归范围、项目数、冲突策略和哈希。",
            verification_method="未来执行后核对目标大小和哈希；链接目标拒绝。",
            rollback_strategy="未来撤销必须作为新计划处理目标副本，不自动回滚。",
            limits={"max_items": 20, "recursive_default": False, "overwrite": "deny", "reparse_target": "deny"},
        ),
        _manifest(
            name="move_file",
            description="仅生成受控移动计划；默认单项且拒绝冲突，跨盘须复制验证后再删源。",
            input_schema=_object_schema({"source": path, "destination": path, "recursive": {"type": "boolean"}, "conflict_policy": {"type": "string", "enum": ["deny"]}}, ("source", "destination", "conflict_policy")),
            output_schema=_object_schema({"source": path, "destination": path, "result": {"type": "string"}, "verification": {"type": "object"}}, ("source", "destination", "result", "verification")),
            risk_level="L2", paths=True, timeout_ms=120_000, max_output_bytes=512 * 1024,
            idempotency="conditional", cancellation="cooperative",
            preview_method="展开源、目标、是否跨盘、项目数、冲突策略和验证步骤。",
            verification_method="未来执行后确认目标事实；跨盘验证失败时不得删除源。",
            rollback_strategy="反向移动必须生成新计划并重新授权，不自动回滚。",
            limits={"max_items": 20, "recursive_default": False, "overwrite": "deny", "cross_volume": "copy_verify_then_remove_source"},
        ),
        _manifest(
            name="rename_file",
            description="仅生成同一父目录内的受控重命名计划；默认禁止改扩展名和覆盖。",
            input_schema=_object_schema({"path": path, "new_name": {"type": "string", "minLength": 1, "maxLength": 255}, "allow_extension_change": {"type": "boolean"}}, ("path", "new_name")),
            output_schema=_object_schema({"old_path": path, "new_path": path, "extension_changed": {"type": "boolean"}}, ("old_path", "new_path", "extension_changed")),
            risk_level="L2", paths=True, timeout_ms=30_000, max_output_bytes=256 * 1024,
            idempotency="conditional", cancellation="not_supported",
            preview_method="展示原路径、新路径、扩展名变化和冲突检查。",
            verification_method="未来执行后确认原路径消失且新路径身份与哈希匹配。",
            rollback_strategy="反向重命名必须形成新计划并重新授权。",
            limits={"max_items": 20, "same_parent": True, "extension_change_default": False, "overwrite": "deny"},
        ),
        _manifest(
            name="move_to_recycle_bin",
            description="仅生成移入 Windows 回收站的 L3 计划；永久删除始终禁用。",
            input_schema=_object_schema({"paths": {"type": "array", "items": path, "minItems": 1, "maxItems": 20}, "recursive": {"type": "boolean"}}, ("paths",)),
            output_schema=_object_schema({"items": {"type": "array", "maxItems": 20}, "failed_items": {"type": "array", "maxItems": 20}}, ("items", "failed_items")),
            risk_level="L3", paths=True, timeout_ms=120_000, max_output_bytes=512 * 1024,
            idempotency="non_idempotent", cancellation="cooperative",
            preview_method="展示完整规范化待删清单、数量、递归范围和 L3 风险。",
            verification_method="未来执行后确认原路径状态和回收站操作结果；不宣称可恢复成功。",
            rollback_strategy="不自动恢复；恢复操作必须由独立新计划驱动。",
            limits={"max_items": 20, "permanent_delete": "deny", "recursive_default": False, "reparse_target": "deny"},
        ),
        _manifest(
            name="start_whitelisted_application",
            description="仅生成启动预先登记应用的计划；不接受路径、参数、终端或解释器。",
            input_schema=_object_schema({"application_id": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"}}, ("application_id",)),
            output_schema=_object_schema({"application_name": {"type": "string"}, "pid": {"type": "integer", "minimum": 1}, "result": {"type": "string"}}, ("application_name", "pid", "result")),
            risk_level="L2", paths=False, timeout_ms=30_000, max_output_bytes=256 * 1024,
            idempotency="non_idempotent", cancellation="not_supported",
            preview_method="展示白名单应用 ID、登记名称和固定启动方式。",
            verification_method="未来执行后核对新进程 PID 和登记可执行文件身份。",
            rollback_strategy="不自动终止已启动应用；终止进程工具未注册。",
            limits={"arguments": "deny", "arbitrary_path": "deny", "installer": "deny", "terminal": "deny", "interpreter": "deny"},
        ),
    )
    return ToolRegistry(manifests)