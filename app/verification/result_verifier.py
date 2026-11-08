"""P5 批次 6：执行后重新观察真实状态的 ResultVerifier。"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from app.executor.controlled import BackupRecord, VerificationDecision
from app.executor.local_backend import ApplicationSpec
from app.plans import PlanAction

ProcessProbe = Callable[[int], str | None]


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
        digest.update(child.relative_to(path).as_posix().encode("utf-8")); digest.update(b"\0")
        if child.is_file():
            digest.update(bytes.fromhex(_hash_file(child).removeprefix("sha256:")))
    return "sha256:" + digest.hexdigest()


class LocalResultVerifier:
    def __init__(self, *, applications: Mapping[str, ApplicationSpec], process_probe: ProcessProbe | None = None) -> None:
        copied = dict(applications)
        if any(not isinstance(value, ApplicationSpec) or key != value.application_id for key, value in copied.items()):
            raise TypeError("applications 必须按 application_id 登记 ApplicationSpec")
        self._applications = MappingProxyType(copied)
        self._process_probe = process_probe or self._windows_process_path

    def verify(self, *, action: PlanAction, attempt_id: str, output: Mapping[str, object], backup: BackupRecord | None, verified_at: str) -> VerificationDecision:
        handlers = {"create_text_file": self._verify_create, "write_text_file": self._verify_write,
                    "copy_file": self._verify_copy, "move_file": self._verify_move,
                    "rename_file": self._verify_rename, "move_to_recycle_bin": self._verify_recycle,
                    "start_whitelisted_application": self._verify_application}
        handler = handlers.get(action.tool_name)
        if handler is None:
            return self._uncertain("verification_tool_unknown", "没有该工具的固定验证器", {"tool_name": action.tool_name})
        try:
            return handler(action, output, backup)
        except Exception as exc:
            return self._uncertain("verification_observation_failed", "重新观察真实状态失败", {"exception_type": type(exc).__name__})

    def _verify_create(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        path = Path(str(action.normalized_arguments["path"]))
        if not path.exists(): return self._not_completed("created_file_missing", "创建目标不存在", {"path": str(path), "exists": False})
        content = str(action.normalized_arguments["content"]).encode(str(action.normalized_arguments["encoding"]))
        observed = _hash_file(path); expected = "sha256:" + hashlib.sha256(content).hexdigest()
        facts = {"path": str(path), "exists": True, "observed_hash": observed, "observed_size": path.stat().st_size}
        if observed == expected and output.get("content_hash") == observed: return VerificationDecision("completed", facts)
        return self._uncertain("created_file_mismatch", "创建目标存在但内容事实冲突", facts)

    def _verify_write(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        path = Path(str(action.normalized_arguments["path"]))
        if backup is None or not path.exists(): return self._uncertain("written_file_unavailable", "写入后的目标或备份不可用", {"path": str(path), "exists": path.exists()})
        original = self._backup_bytes(path, backup); mode = action.normalized_arguments["mode"]; content = str(action.normalized_arguments["content"])
        if mode == "append": expected_bytes = original + content.encode("utf-8")
        else:
            target_text = str(action.normalized_arguments["target_text"]); text = original.decode("utf-8")
            if text.count(target_text) != 1: return self._uncertain("backup_replacement_mismatch", "备份中的替换目标不唯一", {"path": str(path)})
            expected_bytes = text.replace(target_text, content, 1).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(expected_bytes).hexdigest(); observed = _hash_file(path)
        facts = {"path": str(path), "observed_hash": observed, "expected_hash": expected, "observed_size": path.stat().st_size}
        if observed == expected and output.get("content_hash") == observed: return VerificationDecision("completed", facts)
        return self._uncertain("written_file_mismatch", "写入后的内容与备份推导结果不一致", facts)

    def _verify_copy(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        source = Path(str(action.normalized_arguments["source"])); destination = Path(str(action.normalized_arguments["destination"]))
        if not destination.exists(): return self._not_completed("copy_destination_missing", "复制目标不存在", {"source_exists": source.exists(), "destination_exists": False})
        if not source.exists(): return self._uncertain("copy_source_missing", "复制后源路径异常消失", {"source_exists": False, "destination_exists": True})
        source_hash, destination_hash = _hash_path(source), _hash_path(destination); facts = {"source": str(source), "destination": str(destination), "source_hash": source_hash, "destination_hash": destination_hash}
        if source_hash == destination_hash and output.get("destination_hash") == destination_hash: return VerificationDecision("completed", facts)
        return self._uncertain("copy_hash_mismatch", "复制源与目标摘要不一致", facts)

    def _verify_move(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        return self._verify_relocation(Path(str(action.normalized_arguments["source"])), Path(str(action.normalized_arguments["destination"])), output, backup, "move")

    def _verify_rename(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        source = Path(str(action.normalized_arguments["path"])); return self._verify_relocation(source, source.parent / str(action.normalized_arguments["new_name"]), output, backup, "rename")

    def _verify_relocation(self, source: Path, destination: Path, output: Mapping[str, object], backup: BackupRecord | None, operation: str) -> VerificationDecision:
        source_exists, destination_exists = source.exists(), destination.exists(); facts: dict[str, object] = {"source": str(source), "destination": str(destination), "source_exists": source_exists, "destination_exists": destination_exists}
        if source_exists and not destination_exists: return self._not_completed(f"{operation}_not_observed", "源仍存在且目标不存在", facts)
        if source_exists or not destination_exists or backup is None: return self._uncertain(f"{operation}_state_conflict", "源和目标状态无法证明动作完整完成", facts)
        expected = self._backup_digest(source, backup); observed = _hash_path(destination); facts.update({"expected_hash": expected, "destination_hash": observed})
        returned_hash = output.get("destination_hash", output.get("content_hash"))
        if observed == expected and returned_hash == observed: return VerificationDecision("completed", facts)
        return self._uncertain(f"{operation}_hash_mismatch", "目标摘要与执行前备份不一致", facts)

    def _verify_recycle(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        paths = tuple(Path(str(item)) for item in action.normalized_arguments["paths"]); states = tuple(path.exists() for path in paths)
        facts = {"paths": tuple(str(path) for path in paths), "source_exists": states, "recycle_requested": output.get("recycle_requested") is True, "recoverability_confirmed": False}
        if all(states): return self._not_completed("recycle_not_observed", "待删除路径仍全部存在", facts)
        if any(states) or output.get("recycle_requested") is not True: return self._uncertain("recycle_partial_or_unconfirmed", "回收站动作存在部分结果或调用事实缺失", facts)
        return VerificationDecision("completed", facts)

    def _verify_application(self, action: PlanAction, output: Mapping[str, object], backup: BackupRecord | None) -> VerificationDecision:
        application_id = str(action.normalized_arguments["application_id"]); spec = self._applications.get(application_id); pid = output.get("pid")
        if spec is None or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0: return self._uncertain("application_evidence_invalid", "应用白名单或 PID 证据无效", {"application_id": application_id})
        observed_path = self._process_probe(pid); facts: dict[str, object] = {"application_id": application_id, "pid": pid, "observed_executable": observed_path}
        if observed_path is None: return self._uncertain("application_process_unobserved", "无法重新观察已启动进程", facts)
        expected_path = str(Path(spec.executable_path).resolve(strict=True)); observed_resolved = str(Path(observed_path).resolve(strict=True)); observed_hash = _hash_file(Path(observed_resolved)); facts["observed_executable_hash"] = observed_hash
        if os.path.normcase(expected_path) == os.path.normcase(observed_resolved) and output.get("executable_hash") == observed_hash and (spec.executable_hash is None or spec.executable_hash == observed_hash): return VerificationDecision("completed", facts)
        return self._uncertain("application_identity_mismatch", "进程可执行文件身份与白名单不一致", facts)

    @staticmethod
    def _backup_digest(source: Path, backup: BackupRecord) -> str:
        matches = [digest for raw, digest in zip(backup.source_paths, backup.source_digests) if os.path.normcase(os.path.abspath(raw)) == os.path.normcase(os.path.abspath(str(source)))]
        if len(matches) != 1: raise OSError("找不到唯一备份摘要")
        return matches[0]

    @staticmethod
    def _backup_bytes(source: Path, backup: BackupRecord) -> bytes:
        matches = [Path(saved) for raw, saved in zip(backup.source_paths, backup.backup_paths) if os.path.normcase(os.path.abspath(raw)) == os.path.normcase(os.path.abspath(str(source)))]
        if len(matches) != 1: raise OSError("找不到唯一备份文件")
        return matches[0].read_bytes()

    @staticmethod
    def _not_completed(code: str, message: str, facts: Mapping[str, object]) -> VerificationDecision: return VerificationDecision("not_completed", facts, code, message)
    @staticmethod
    def _uncertain(code: str, message: str, facts: Mapping[str, object]) -> VerificationDecision: return VerificationDecision("state_uncertain", facts, code, message)

    @staticmethod
    def _windows_process_path(pid: int) -> str | None:
        if os.name != "nt": return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True); handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle: return None
        try:
            size = ctypes.c_ulong(32768); buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)): return None
            return buffer.value
        finally: kernel32.CloseHandle(handle)
