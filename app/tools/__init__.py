"""P4 结构化工具公开入口；只允许只读行为和不可执行的写动作预览。"""

from .catalog import build_mvp_tool_registry
from .manifest import ManifestError, ToolManifest, ToolRegistry
from .write_plans import (
    WriteActionPreview,
    WritePlanError,
    build_write_action_preview,
)
from .read_only import (
    DirectoryItem,
    DirectoryResult,
    ProcessResult,
    ProcessSnapshot,
    ReadOnlyToolError,
    SearchMatch,
    SearchResult,
    TextFileResult,
    get_processes,
    list_directory,
    read_text_file,
    search_files,
)

__all__ = [
    "DirectoryItem",
    "DirectoryResult",
    "ManifestError",
    "ProcessResult",
    "ProcessSnapshot",
    "ReadOnlyToolError",
    "SearchMatch",
    "SearchResult",
    "TextFileResult",
    "ToolManifest",
    "ToolRegistry",
    "WriteActionPreview",
    "WritePlanError",
    "build_mvp_tool_registry",
    "build_write_action_preview",
    "get_processes",
    "list_directory",
    "read_text_file",
    "search_files",
]