"""P5 批次 6：固定工具分派的本地副作用执行边界。"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from app.contracts.base import require_identifier
from app.plans import PlanAction

from .controlled import BackupRecord, ExecutionBackendError


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _hash_path(path: Path) -> str:
    if path.is_file():
        return _hash_file(path)
    if not path.is_dir():
        raise OSError("目标不是普通文件或目录")
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        if child.is_symlink():
            raise OSError("目录树包含链接")
        digest.update(b"D\0" if child.is_dir() else b"F\0")
        digest.update(child.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        if child.is_file():
            digest.update(bytes.fromhex(_hash_file(child).removeprefix("sha256:")))
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ApplicationSpec:
    application_id: str
    application_name: str
    executable_path: str
    executable_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier("ApplicationSpec.application_id", self.application_id)
        if not isinstance(self.application_name, str) or not self.application_name.strip():
            raise ValueError("application_name 必须是非空文本")
        if not Path(self.executable_path).is_absolute():
            raise ValueError("executable_path 必须是绝对路径")
        if self.executable_hash is not None and (not self.executable_hash.startswith("sha256:") or len(self.executable_hash) != 71):
            raise ValueError("executable_hash 必须是 sha256 哈希")


RecycleHandler = Callable[[tuple[Path, ...]], None]
ProcessLauncher = Callable[[ApplicationSpec], int]


class LocalExecutionBackend:
    """只分派七个固定副作用工具；不接受命令、参数串、Shell 或解释器。"""

    _BACKUP_TOOLS = frozenset({"write_text_file", "move_file", "rename_file", "move_to_recycle_bin"})

    def __init__(self, *, allowed_root: str | Path, backup_root: str | Path,
                 applications: Mapping[str, ApplicationSpec],
                 recycle_handler: RecycleHandler | None = None,
                 process_launcher: ProcessLauncher | None = None) -> None:
        root = Path(allowed_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("allowed_root 必须是既有目录")
        backup = Path(backup_root).resolve(strict=False)
        if not self._is_within(backup, root):
            raise ValueError("backup_root 必须位于 allowed_root 内")
        copied = dict(applications)
        if any(not isinstance(value, ApplicationSpec) or key != value.application_id for key, value in copied.items()):
            raise TypeError("applications 必须按 application_id 登记 ApplicationSpec")
        self._allowed_root = root
        self._backup_root = backup
        self._applications = MappingProxyType(copied)
        self._recycle_handler = recycle_handler or self._windows_recycle
        self._process_launcher = process_launcher or self._windows_launch

    def prepare_backup(self, *, action: PlanAction, execution_id: str, attempt_id: str,
                       created_at: str) -> BackupRecord | None:
        if action.tool_name not in self._BACKUP_TOOLS:
            return None
        require_identifier("execution_id", execution_id)
        sources = self._backup_sources(action)
        try:
            directory = self._safe_backup_directory(execution_id, action.action_id, attempt_id)
            directory.mkdir(parents=True, exist_ok=False)
            backup_paths: list[str] = []
            digests: list[str] = []
            source_paths: list[str] = []
            for index, source in enumerate(sources, start=1):
                checked = self._safe_path(source, must_exist=True)
                digest = _hash_path(checked)
                target = directory / f"{index:02d}-{checked.name}"
                if checked.is_dir():
                    shutil.copytree(checked, target, symlinks=False)
                else:
                    shutil.copy2(checked, target)
                if _hash_path(target) != digest:
                    raise OSError("备份摘要复核失败")
                source_paths.append(str(checked))
                backup_paths.append(str(target))
                digests.append(digest)
            return BackupRecord(
                backup_id=f"backup-{uuid.uuid4().hex}", action_id=action.action_id,
                attempt_id=attempt_id, created_at=created_at,
                source_paths=tuple(source_paths), backup_paths=tuple(backup_paths),
                source_digests=tuple(digests),
            )
        except ExecutionBackendError:
            raise
        except Exception as exc:
            raise ExecutionBackendError("backup_failed", "执行前备份失败，未调用目标工具", side_effect_may_have_occurred=False) from exc

    def execute(self, *, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        if not isinstance(action, PlanAction):
            raise TypeError("action 必须是 PlanAction")
        handlers = {
            "create_text_file": self._create_text_file,
            "write_text_file": self._write_text_file,
            "copy_file": self._copy_file,
            "move_file": self._move_file,
            "rename_file": self._rename_file,
            "move_to_recycle_bin": self._move_to_recycle_bin,
            "start_whitelisted_application": self._start_application,
        }
        handler = handlers.get(action.tool_name)
        if handler is None:
            raise ExecutionBackendError("tool_not_executable", "工具不属于固定执行清单", side_effect_may_have_occurred=False)
        if action.tool_name in self._BACKUP_TOOLS:
            self._require_backup(action, backup)
        return handler(action, backup)

    def _create_text_file(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        args = action.normalized_arguments
        target = self._safe_path(self._string(args, "path"), must_exist=False)
        if target.exists():
            raise ExecutionBackendError("target_exists", "创建目标已经存在", side_effect_may_have_occurred=False)
        encoding = self._string(args, "encoding")
        if encoding not in {"utf-8", "gbk"}:
            raise ExecutionBackendError("invalid_encoding", "编码不在固定白名单中", side_effect_may_have_occurred=False)
        content = self._string(args, "content", allow_empty=True).encode(encoding)
        if action.preview.get("content_hash") != _hash_bytes(content):
            raise ExecutionBackendError("preview_content_mismatch", "内容与冻结预览不一致", side_effect_may_have_occurred=False)
        create_parent = self._boolean(args, "create_parent")
        try:
            if create_parent:
                target.parent.mkdir(parents=True, exist_ok=True)
            elif not target.parent.is_dir():
                raise FileNotFoundError("父目录不存在")
            with target.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ExecutionBackendError("target_exists", "创建目标已经存在", side_effect_may_have_occurred=False) from exc
        except Exception as exc:
            raise ExecutionBackendError("create_failed", "创建文本文件失败", side_effect_may_have_occurred=target.exists()) from exc
        return {"path": str(target), "size_bytes": len(content), "content_hash": _hash_file(target)}

    def _write_text_file(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        assert backup is not None
        args = action.normalized_arguments
        target = self._safe_path(self._string(args, "path"), must_exist=True)
        expected = self._string(args, "expected_source_hash")
        self._require_backup_digest(target, expected, backup)
        if _hash_file(target) != expected:
            raise ExecutionBackendError("source_hash_changed", "写入前源文件摘要已变化", side_effect_may_have_occurred=False)
        mode = self._string(args, "mode")
        content = self._string(args, "content", allow_empty=True)
        try:
            original = target.read_bytes()
            if mode == "append":
                updated = original + content.encode("utf-8")
            elif mode == "replace_exact":
                target_text = self._string(args, "target_text")
                text = original.decode("utf-8")
                if text.count(target_text) != 1:
                    raise ExecutionBackendError("replacement_target_changed", "待替换文本不再唯一", side_effect_may_have_occurred=False)
                updated = text.replace(target_text, content, 1).encode("utf-8")
            else:
                raise ExecutionBackendError("invalid_write_mode", "写入模式不在固定白名单中", side_effect_may_have_occurred=False)
            with target.open("r+b") as handle:
                handle.seek(0); handle.write(updated); handle.truncate(); handle.flush(); os.fsync(handle.fileno())
        except ExecutionBackendError:
            raise
        except Exception as exc:
            raise ExecutionBackendError("write_failed", "写入文本文件失败", side_effect_may_have_occurred=True) from exc
        return {"path": str(target), "size_bytes": target.stat().st_size, "content_hash": _hash_file(target)}

    def _copy_file(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        source, destination, recursive = self._transfer_paths(action)
        source_digest = _hash_path(source)
        try:
            if source.is_dir():
                if not recursive:
                    raise ExecutionBackendError("recursive_required", "目录复制必须显式允许递归", side_effect_may_have_occurred=False)
                shutil.copytree(source, destination, symlinks=False)
            else:
                if not destination.parent.is_dir():
                    raise FileNotFoundError("目标父目录不存在")
                shutil.copy2(source, destination)
        except ExecutionBackendError:
            raise
        except Exception as exc:
            raise ExecutionBackendError("copy_failed", "复制失败", side_effect_may_have_occurred=destination.exists()) from exc
        return {"source": str(source), "destination": str(destination), "source_hash": source_digest, "destination_hash": _hash_path(destination), "result": "copied"}

    def _move_file(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        assert backup is not None
        source, destination, recursive = self._transfer_paths(action)
        source_digest = _hash_path(source)
        self._require_backup_digest(source, source_digest, backup)
        try:
            if source.is_dir() and not recursive:
                raise ExecutionBackendError("recursive_required", "目录移动必须显式允许递归", side_effect_may_have_occurred=False)
            if not destination.parent.is_dir():
                raise FileNotFoundError("目标父目录不存在")
            try:
                source.rename(destination)
            except OSError:
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=False)
                else:
                    shutil.copy2(source, destination)
                if _hash_path(destination) != source_digest:
                    raise OSError("跨卷复制摘要复核失败")
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
        except ExecutionBackendError:
            raise
        except Exception as exc:
            raise ExecutionBackendError("move_failed", "移动失败", side_effect_may_have_occurred=True) from exc
        return {"source": str(source), "destination": str(destination), "destination_hash": _hash_path(destination), "result": "moved"}

    def _rename_file(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        assert backup is not None
        args = action.normalized_arguments
        source = self._safe_path(self._string(args, "path"), must_exist=True)
        destination = self._safe_path(str(source.parent / self._string(args, "new_name")), must_exist=False)
        if destination.exists():
            raise ExecutionBackendError("target_exists", "重命名目标已经存在", side_effect_may_have_occurred=False)
        source_digest = _hash_path(source)
        self._require_backup_digest(source, source_digest, backup)
        try:
            source.rename(destination)
        except Exception as exc:
            raise ExecutionBackendError("rename_failed", "重命名失败", side_effect_may_have_occurred=True) from exc
        return {"old_path": str(source), "new_path": str(destination), "content_hash": _hash_path(destination), "extension_changed": source.suffix.casefold() != destination.suffix.casefold()}

    def _move_to_recycle_bin(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        assert backup is not None
        raw_paths = action.normalized_arguments.get("paths")
        if not isinstance(raw_paths, tuple) or not raw_paths:
            raise ExecutionBackendError("invalid_paths", "回收站路径列表无效", side_effect_may_have_occurred=False)
        paths = tuple(self._safe_path(str(item), must_exist=True) for item in raw_paths)
        for path in paths:
            self._require_backup_digest(path, _hash_path(path), backup)
        try:
            self._recycle_handler(paths)
        except ExecutionBackendError:
            raise
        except Exception as exc:
            raise ExecutionBackendError("recycle_failed", "移入回收站失败或结果不完整", side_effect_may_have_occurred=True) from exc
        return {"items": tuple(str(item) for item in paths), "failed_items": (), "recycle_requested": True, "recoverability_confirmed": False}

    def _start_application(self, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, object]:
        application_id = self._string(action.normalized_arguments, "application_id")
        spec = self._applications.get(application_id)
        if spec is None:
            raise ExecutionBackendError("application_not_whitelisted", "应用未登记在本地白名单", side_effect_may_have_occurred=False)
        try:
            executable = Path(spec.executable_path).resolve(strict=True)
        except OSError as exc:
            raise ExecutionBackendError("application_executable_invalid", "白名单可执行文件不存在", side_effect_may_have_occurred=False) from exc
        if not executable.is_file():
            raise ExecutionBackendError("application_executable_invalid", "白名单可执行文件不存在", side_effect_may_have_occurred=False)
        executable_hash = _hash_file(executable)
        if spec.executable_hash is not None and executable_hash != spec.executable_hash:
            raise ExecutionBackendError("application_identity_changed", "白名单可执行文件摘要已变化", side_effect_may_have_occurred=False)
        try:
            pid = self._process_launcher(spec)
        except ExecutionBackendError:
            raise
        except Exception as exc:
            raise ExecutionBackendError("application_launch_failed", "应用启动结果未知", side_effect_may_have_occurred=True) from exc
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ExecutionBackendError("application_pid_invalid", "应用启动未返回有效 PID", side_effect_may_have_occurred=True)
        return {"application_id": application_id, "application_name": spec.application_name, "executable_path": str(executable), "executable_hash": executable_hash, "pid": pid, "result": "started"}

    def _transfer_paths(self, action: PlanAction) -> tuple[Path, Path, bool]:
        args = action.normalized_arguments
        if self._string(args, "conflict_policy") != "deny":
            raise ExecutionBackendError("conflict_policy_invalid", "只允许拒绝冲突", side_effect_may_have_occurred=False)
        source = self._safe_path(self._string(args, "source"), must_exist=True)
        destination = self._safe_path(self._string(args, "destination"), must_exist=False)
        if destination.exists():
            raise ExecutionBackendError("target_exists", "目标已经存在", side_effect_may_have_occurred=False)
        return source, destination, self._boolean(args, "recursive")

    def _backup_sources(self, action: PlanAction) -> tuple[str, ...]:
        args = action.normalized_arguments
        if action.tool_name == "write_text_file":
            return (self._string(args, "path"),)
        if action.tool_name == "move_file":
            return (self._string(args, "source"),)
        if action.tool_name == "rename_file":
            return (self._string(args, "path"),)
        raw_paths = args.get("paths")
        if not isinstance(raw_paths, tuple) or not raw_paths or any(not isinstance(item, str) for item in raw_paths):
            raise ExecutionBackendError("invalid_paths", "备份路径列表无效", side_effect_may_have_occurred=False)
        return tuple(raw_paths)

    def _require_backup(self, action: PlanAction, backup: BackupRecord | None) -> None:
        if backup is None or backup.action_id != action.action_id:
            raise ExecutionBackendError("backup_required", "动作缺少匹配的执行前备份", side_effect_may_have_occurred=False)
        if len(backup.source_paths) != len(backup.source_digests):
            raise ExecutionBackendError("backup_invalid", "备份事实不完整", side_effect_may_have_occurred=False)

    def _require_backup_digest(self, source: Path, digest: str, backup: BackupRecord) -> None:
        matches = [item_digest for item_path, item_digest in zip(backup.source_paths, backup.source_digests) if self._same_path(item_path, source)]
        if matches != [digest]:
            raise ExecutionBackendError("backup_source_mismatch", "备份与当前源文件事实不一致", side_effect_may_have_occurred=False)

    def _safe_backup_directory(self, execution_id: str, action_id: str, attempt_id: str) -> Path:
        require_identifier("action_id", action_id)
        require_identifier("attempt_id", attempt_id)
        target = (self._backup_root / execution_id / action_id / attempt_id).resolve(strict=False)
        if not self._is_within(target, self._backup_root):
            raise ExecutionBackendError("backup_path_invalid", "备份路径越界", side_effect_may_have_occurred=False)
        return target

    def _safe_path(self, raw: str, *, must_exist: bool) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            raise ExecutionBackendError("path_not_absolute", "执行路径必须是绝对路径", side_effect_may_have_occurred=False)
        try:
            resolved = path.resolve(strict=must_exist)
        except OSError as exc:
            raise ExecutionBackendError("path_unavailable", "执行路径不存在或不可访问", side_effect_may_have_occurred=False) from exc
        if not self._is_within(resolved, self._allowed_root):
            raise ExecutionBackendError("path_outside_allowed_root", "执行路径越出允许根目录", side_effect_may_have_occurred=False)
        cursor = resolved if resolved.exists() else resolved.parent
        while self._is_within(cursor, self._allowed_root):
            if cursor.is_symlink():
                raise ExecutionBackendError("reparse_path_denied", "执行路径包含链接", side_effect_may_have_occurred=False)
            if cursor == self._allowed_root:
                break
            cursor = cursor.parent
        return resolved

    @staticmethod
    def _string(mapping: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
        value = mapping.get(key)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise ExecutionBackendError("invalid_argument", f"参数 {key} 无效", side_effect_may_have_occurred=False)
        return value

    @staticmethod
    def _boolean(mapping: Mapping[str, object], key: str) -> bool:
        value = mapping.get(key)
        if not isinstance(value, bool):
            raise ExecutionBackendError("invalid_argument", f"参数 {key} 无效", side_effect_may_have_occurred=False)
        return value

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
        except ValueError:
            return False

    @staticmethod
    def _same_path(left: str | Path, right: str | Path) -> bool:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))

    @staticmethod
    def _windows_recycle(paths: tuple[Path, ...]) -> None:
        if os.name != "nt":
            raise ExecutionBackendError("recycle_unavailable", "当前平台没有受控 Windows 回收站适配器", side_effect_may_have_occurred=False)

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        payload = "\0".join(str(item) for item in paths) + "\0\0"
        operation = SHFILEOPSTRUCTW(None, 3, payload, None, 0x0040 | 0x0010 | 0x0004 | 0x0400, 0, None, None)
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if result != 0 or operation.fAnyOperationsAborted:
            raise OSError(f"SHFileOperationW failed: {result}")

    @staticmethod
    def _windows_launch(spec: ApplicationSpec) -> int:
        if os.name != "nt":
            raise ExecutionBackendError("application_launch_unavailable", "当前平台没有受控应用启动适配器", side_effect_may_have_occurred=False)

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                        ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
                        ("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong), ("dwXCountChars", ctypes.c_ulong),
                        ("dwYCountChars", ctypes.c_ulong), ("dwFillAttribute", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort),
                        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", ctypes.c_void_p),
                        ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                        ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong)]

        startup = STARTUPINFOW(); startup.cb = ctypes.sizeof(startup); process = PROCESS_INFORMATION()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = kernel32.CreateProcessW(str(Path(spec.executable_path).resolve(strict=True)), None, None, None, False, 0, None, None, ctypes.byref(startup), ctypes.byref(process))
        if not ok:
            raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
        kernel32.CloseHandle(process.hThread); kernel32.CloseHandle(process.hProcess)
        return int(process.dwProcessId)
