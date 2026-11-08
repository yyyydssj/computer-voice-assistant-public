"""P4 批次 2：四个只读工具的受限、结构化行为。

本模块不包含 PathGuard 的实现。所有路径工具都必须显式接收调用方提供的
``path_validator``，因此在正式 PathGuard 接入前不会形成未校验的文件访问通道。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Any

MAX_READ_ONLY_ITEMS = 200
MAX_SEARCH_MATCHES = 100
MAX_RECURSION_DEPTH = 5
MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_READ_OUTPUT_BYTES = 1 * 1024 * 1024
DEFAULT_SEARCH_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_MS = 30_000

PathValidator = Callable[[str, str], Path]
Clock = Callable[[], float]


class ReadOnlyToolError(ValueError):
    """只读工具的可审计错误，不把底层异常直接暴露给模型。"""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True, slots=True)
class DirectoryItem:
    name: str
    item_type: str
    size_bytes: int
    modified_at: str | None
    relative_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.item_type,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class DirectoryResult:
    items: tuple[DirectoryItem, ...]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items], "truncated": self.truncated}


@dataclass(frozen=True, slots=True)
class TextFileResult:
    text: str
    encoding: str
    size_bytes: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "encoding": self.encoding,
            "size_bytes": self.size_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class SearchMatch:
    relative_path: str
    line_number: int | None
    summary: str | None
    size_bytes: int
    modified_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "line_number": self.line_number,
            "summary": self.summary,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[SearchMatch, ...]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {"matches": [match.to_dict() for match in self.matches], "truncated": self.truncated}


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """进程提供方的内部快照；完整路径永远不会进入工具输出。"""

    pid: int
    name: str
    memory_bytes: int | None = None
    executable_path: str | None = None
    started_at: str | None = None

    def public_dict(self, *, include_details: bool) -> dict[str, Any]:
        executable_summary = None
        if self.executable_path:
            executable_summary = Path(self.executable_path).name or None
        result: dict[str, Any] = {
            "pid": self.pid,
            "name": self.name,
            "memory_bytes": self.memory_bytes,
            "executable_summary": executable_summary,
            "started_at": self.started_at,
        }
        # include_details 只影响已批准的摘要字段，不会开放命令行、环境变量或完整用户信息。
        if not include_details:
            result.pop("memory_bytes")
            result.pop("executable_summary")
            result.pop("started_at")
        return result


@dataclass(frozen=True, slots=True)
class ProcessResult:
    processes: tuple[ProcessSnapshot, ...]
    truncated: bool
    include_details: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "processes": [
                item.public_dict(include_details=self.include_details) for item in self.processes
            ],
            "truncated": self.truncated,
        }


def _require_limit(name: str, value: int, *, minimum: int = 1, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReadOnlyToolError("invalid_argument", f"{name} 必须是有效整数")
    if maximum is not None and value > maximum:
        raise ReadOnlyToolError("limit_exceeded", f"{name} 不得超过 {maximum}")


def _require_timeout(timeout_ms: int) -> None:
    _require_limit("timeout_ms", timeout_ms)


def _deadline(clock: Clock, timeout_ms: int) -> float:
    _require_timeout(timeout_ms)
    return clock() + timeout_ms / 1000


def _check_deadline(clock: Clock, deadline: float) -> None:
    if clock() > deadline:
        raise ReadOnlyToolError("timeout", "只读工具达到时间上限")


def _validate_path_validator(path_validator: PathValidator | None) -> PathValidator:
    if not callable(path_validator):
        raise TypeError("path_validator 是必需的可调用路径校验接口")
    return path_validator


def _modified_at(stat_result: os.stat_result) -> str | None:
    try:
        return datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _is_hidden(entry: os.DirEntry[str]) -> bool:
    if entry.name.startswith("."):
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & 0x2)


def _entry_item(entry: os.DirEntry[str], root: Path) -> DirectoryItem | None:
    if entry.is_symlink():
        return None
    try:
        stat_result = entry.stat(follow_symlinks=False)
        is_directory = entry.is_dir(follow_symlinks=False)
    except (OSError, PermissionError):
        raise ReadOnlyToolError("permission_denied", "无法读取目录项元数据", path=entry.path)
    relative = Path(entry.path).relative_to(root).as_posix()
    return DirectoryItem(
        name=entry.name,
        item_type="directory" if is_directory else "file",
        size_bytes=0 if is_directory else stat_result.st_size,
        modified_at=_modified_at(stat_result),
        relative_path=relative,
    )


def _sort_directory_items(items: list[DirectoryItem], sort_by: str) -> None:
    if sort_by == "name":
        items.sort(key=lambda item: (item.name.casefold(), item.name))
    elif sort_by == "size":
        items.sort(key=lambda item: (item.size_bytes, item.relative_path.casefold()))
    elif sort_by == "modified_at":
        items.sort(key=lambda item: (item.modified_at or "", item.relative_path.casefold()))
    else:
        raise ReadOnlyToolError("invalid_argument", "sort_by 只支持 name、modified_at 或 size")


def list_directory(
    path: str,
    *,
    recursive: bool = False,
    sort_by: str = "name",
    include_hidden: bool = False,
    max_items: int = MAX_READ_ONLY_ITEMS,
    max_depth: int = MAX_RECURSION_DEPTH,
    timeout_ms: int = 10_000,
    path_validator: PathValidator | None = None,
    clock: Clock = time.monotonic,
) -> DirectoryResult:
    validator = _validate_path_validator(path_validator)
    _require_limit("max_items", max_items, maximum=MAX_READ_ONLY_ITEMS)
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= MAX_RECURSION_DEPTH:
        raise ReadOnlyToolError("limit_exceeded", f"max_depth 必须在 0..{MAX_RECURSION_DEPTH} 内")
    if not isinstance(recursive, bool) or not isinstance(include_hidden, bool):
        raise ReadOnlyToolError("invalid_argument", "recursive 和 include_hidden 必须是布尔值")
    root = validator(path, "directory")
    if not isinstance(root, Path):
        raise TypeError("path_validator 必须返回 pathlib.Path")
    deadline = _deadline(clock, timeout_ms)
    items: list[DirectoryItem] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        _check_deadline(clock, deadline)
        current, depth = pending.pop()
        try:
            entries = list(os.scandir(current))
        except PermissionError as exc:
            raise ReadOnlyToolError("permission_denied", "无权读取目录", path=str(current)) from exc
        except OSError as exc:
            raise ReadOnlyToolError("io_error", "读取目录失败", path=str(current)) from exc
        for entry in entries:
            _check_deadline(clock, deadline)
            try:
                if not include_hidden and _is_hidden(entry):
                    continue
            except PermissionError:
                continue
            if recursive and depth + 1 > max_depth:
                continue
            item = _entry_item(entry, root)
            if item is None:
                continue
            items.append(item)
            if recursive and item.item_type == "directory" and depth < max_depth:
                pending.append((Path(entry.path), depth + 1))
    _sort_directory_items(items, sort_by)
    return DirectoryResult(items=tuple(items[:max_items]), truncated=len(items) > max_items)


def _looks_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    if not data:
        return False
    control_count = sum(byte < 9 or 13 < byte < 32 for byte in data)
    return control_count / len(data) > 0.10


def _decode_text(data: bytes, encoding_hint: str | None) -> tuple[str, str]:
    if _looks_binary(data):
        raise ReadOnlyToolError("binary_file", "目标文件看起来不是文本文件")
    candidates = [encoding_hint] if encoding_hint else []
    candidates.extend(item for item in ("utf-8", "gbk") if item not in candidates)
    for encoding in candidates:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ReadOnlyToolError("decode_failed", "文件不是受支持的 UTF-8 或 GBK 文本")


def _truncate_text(text: str, max_output_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_output_bytes:
        return text, False
    return encoded[:max_output_bytes].decode("utf-8", errors="ignore"), True


def read_text_file(
    path: str,
    *,
    start_line: int = 1,
    max_lines: int | None = None,
    encoding_hint: str | None = None,
    max_file_bytes: int = MAX_TEXT_FILE_BYTES,
    max_output_bytes: int = DEFAULT_READ_OUTPUT_BYTES,
    timeout_ms: int = 10_000,
    path_validator: PathValidator | None = None,
    clock: Clock = time.monotonic,
) -> TextFileResult:
    validator = _validate_path_validator(path_validator)
    _require_limit("start_line", start_line)
    if max_lines is not None:
        _require_limit("max_lines", max_lines)
    _require_limit("max_file_bytes", max_file_bytes, maximum=MAX_TEXT_FILE_BYTES)
    _require_limit("max_output_bytes", max_output_bytes)
    if encoding_hint not in (None, "utf-8", "gbk"):
        raise ReadOnlyToolError("invalid_argument", "encoding_hint 只支持 utf-8 或 gbk")
    file_path = validator(path, "file")
    if not isinstance(file_path, Path):
        raise TypeError("path_validator 必须返回 pathlib.Path")
    deadline = _deadline(clock, timeout_ms)
    _check_deadline(clock, deadline)
    try:
        size_bytes = file_path.stat().st_size
    except PermissionError as exc:
        raise ReadOnlyToolError("permission_denied", "无权读取文件元数据", path=path) from exc
    except OSError as exc:
        raise ReadOnlyToolError("io_error", "读取文件元数据失败", path=path) from exc
    if size_bytes > max_file_bytes:
        raise ReadOnlyToolError("file_too_large", f"文件超过 {max_file_bytes} 字节上限", path=path)
    try:
        data = file_path.read_bytes()
    except PermissionError as exc:
        raise ReadOnlyToolError("permission_denied", "无权读取文件", path=path) from exc
    except OSError as exc:
        raise ReadOnlyToolError("io_error", "读取文件失败", path=path) from exc
    _check_deadline(clock, deadline)
    text, encoding = _decode_text(data, encoding_hint)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    selected = lines[start_line - 1 :]
    range_truncated = start_line != 1 or start_line - 1 > 0
    if max_lines is not None:
        if len(selected) > max_lines:
            range_truncated = True
        selected = selected[:max_lines]
    selected_text = "".join(selected)
    if selected_text != text:
        range_truncated = True
    output_text, output_truncated = _truncate_text(selected_text, max_output_bytes)
    return TextFileResult(
        text=output_text,
        encoding=encoding,
        size_bytes=size_bytes,
        truncated=range_truncated or output_truncated,
    )


def _iter_files(root: Path, *, recursive: bool, max_depth: int, clock: Clock, deadline: float) -> Iterator[Path]:
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        _check_deadline(clock, deadline)
        current, depth = pending.pop()
        try:
            entries = list(os.scandir(current))
        except PermissionError:
            if current == root:
                raise ReadOnlyToolError("permission_denied", "无权读取搜索目录", path=str(current))
            continue
        except OSError:
            continue
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        for entry in entries:
            _check_deadline(clock, deadline)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive and depth < max_depth:
                        pending.append((Path(entry.path), depth + 1))
                    continue
                if entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except (OSError, PermissionError):
                continue


def _file_match(path: Path, root: Path, pattern: str, mode: str, *, max_file_bytes: int) -> SearchMatch | None:
    try:
        stat_result = path.stat()
    except (OSError, PermissionError):
        return None
    relative = path.relative_to(root).as_posix()
    if mode == "name" and pattern.casefold() not in path.name.casefold():
        return None
    if mode == "extension":
        extension = pattern if pattern.startswith(".") else "." + pattern
        if path.suffix.casefold() != extension.casefold():
            return None
    if mode in {"name", "extension"}:
        return SearchMatch(relative, None, None, stat_result.st_size, _modified_at(stat_result))
    if stat_result.st_size > max_file_bytes:
        return None
    try:
        data = path.read_bytes()
    except (OSError, PermissionError):
        return None
    try:
        text, _ = _decode_text(data, None)
    except ReadOnlyToolError:
        return None
    needle = pattern.casefold()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if needle in line.casefold():
            summary = line.strip().replace("\x00", "")[:240]
            return SearchMatch(relative, line_number, summary, stat_result.st_size, _modified_at(stat_result))
    return None


def search_files(
    root: str,
    pattern: str,
    *,
    mode: str,
    recursive: bool = False,
    max_matches: int = MAX_SEARCH_MATCHES,
    max_depth: int = MAX_RECURSION_DEPTH,
    max_file_bytes: int = MAX_TEXT_FILE_BYTES,
    max_output_bytes: int = DEFAULT_SEARCH_OUTPUT_BYTES,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    path_validator: PathValidator | None = None,
    clock: Clock = time.monotonic,
) -> SearchResult:
    validator = _validate_path_validator(path_validator)
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 512:
        raise ReadOnlyToolError("invalid_argument", "pattern 必须是 1..512 个字符")
    if mode not in {"name", "extension", "content"}:
        raise ReadOnlyToolError("invalid_argument", "mode 只支持 name、extension 或 content")
    if not isinstance(recursive, bool):
        raise ReadOnlyToolError("invalid_argument", "recursive 必须是布尔值")
    _require_limit("max_matches", max_matches, maximum=MAX_SEARCH_MATCHES)
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= MAX_RECURSION_DEPTH:
        raise ReadOnlyToolError("limit_exceeded", f"max_depth 必须在 0..{MAX_RECURSION_DEPTH} 内")
    _require_limit("max_file_bytes", max_file_bytes, maximum=MAX_TEXT_FILE_BYTES)
    _require_limit("max_output_bytes", max_output_bytes)
    search_root = validator(root, "directory")
    if not isinstance(search_root, Path):
        raise TypeError("path_validator 必须返回 pathlib.Path")
    deadline = _deadline(clock, timeout_ms)
    matches: list[SearchMatch] = []
    truncated = False
    for path in _iter_files(search_root, recursive=recursive, max_depth=max_depth, clock=clock, deadline=deadline):
        _check_deadline(clock, deadline)
        # 重新通过调用方校验接口确认每个结果仍是文件；后续 PathGuard 可在此拒绝链接或竞态路径。
        try:
            checked = validator(str(path), "file")
        except ReadOnlyToolError:
            continue
        except (OSError, PermissionError, ValueError):
            continue
        match = _file_match(checked, search_root, pattern, mode, max_file_bytes=max_file_bytes)
        if match is None:
            continue
        matches.append(match)
        if len(matches) >= max_matches:
            truncated = True
            break
        encoded_size = len(json.dumps([item.to_dict() for item in matches], ensure_ascii=False).encode("utf-8"))
        if encoded_size > max_output_bytes:
            matches.pop()
            truncated = True
            break
    return SearchResult(matches=tuple(matches), truncated=truncated)


def _query_executable_path(pid: int) -> str | None:
    if os.name != "nt":
        return None
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        query = ctypes.windll.kernel32.QueryFullProcessImageNameW
        if query(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _default_process_provider() -> Iterable[ProcessSnapshot]:
    if os.name != "nt":
        return ()

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        raise PermissionError("无法枚举进程")
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        first = ctypes.windll.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while first:
            pid = int(entry.th32ProcessID)
            yield ProcessSnapshot(
                pid=pid,
                name=str(entry.szExeFile),
                executable_path=_query_executable_path(pid),
            )
            first = ctypes.windll.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        ctypes.windll.kernel32.CloseHandle(snapshot)


def get_processes(
    *,
    name_filter: str | None = None,
    include_details: bool = False,
    max_items: int = MAX_READ_ONLY_ITEMS,
    timeout_ms: int = 10_000,
    process_provider: Callable[[], Iterable[ProcessSnapshot]] | None = None,
    clock: Clock = time.monotonic,
) -> ProcessResult:
    if name_filter is not None and (not isinstance(name_filter, str) or not 1 <= len(name_filter) <= 260):
        raise ReadOnlyToolError("invalid_argument", "name_filter 必须是 1..260 个字符")
    if not isinstance(include_details, bool):
        raise ReadOnlyToolError("invalid_argument", "include_details 必须是布尔值")
    _require_limit("max_items", max_items, maximum=MAX_READ_ONLY_ITEMS)
    deadline = _deadline(clock, timeout_ms)
    provider = process_provider or _default_process_provider
    results: list[ProcessSnapshot] = []
    truncated = False
    try:
        iterator = iter(provider())
    except PermissionError as exc:
        raise ReadOnlyToolError("permission_denied", "无权读取进程摘要") from exc
    except OSError as exc:
        raise ReadOnlyToolError("process_query_failed", "读取进程摘要失败") from exc
    while True:
        _check_deadline(clock, deadline)
        try:
            snapshot = next(iterator)
        except StopIteration:
            break
        except ProcessLookupError:
            # 进程可能在枚举期间退出；保留已获得的稳定快照。
            continue
        except PermissionError as exc:
            if results:
                continue
            raise ReadOnlyToolError("permission_denied", "无权读取进程摘要") from exc
        except OSError as exc:
            if results:
                continue
            raise ReadOnlyToolError("process_query_failed", "读取进程摘要失败") from exc
        if not isinstance(snapshot, ProcessSnapshot):
            raise ReadOnlyToolError("provider_error", "进程提供方返回了非法快照")
        if snapshot.pid <= 0 or not snapshot.name:
            continue
        if name_filter is not None and name_filter.casefold() not in snapshot.name.casefold():
            continue
        results.append(snapshot)
        if len(results) >= max_items:
            truncated = True
            break
    return ProcessResult(tuple(results), truncated=truncated, include_details=include_details)
