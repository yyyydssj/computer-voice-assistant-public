"""P4 批次 4：Windows 文件路径的拒绝优先安全边界。

PathGuard 只做路径规范化、真实路径检查和稳定快照。它不授予权限、不执行文件
操作，也不替代后续 PolicyEngine、用户确认或执行前的动作状态校验。
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

DEFAULT_ALLOWED_ROOT = r"G:\study"
DEFAULT_WORKING_DIRECTORY = r"G:\study\AI工作区"

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ENVIRONMENT_PATTERN = re.compile(r"%([^%]+)%")
_SHORT_NAME_PATTERN = re.compile(r"^[^\\/]*~[0-9]+(?:\.[^\\/]*)?$", re.IGNORECASE)


class PathGuardError(ValueError):
    """可审计的路径拒绝；不向上层泄露底层异常详情。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        purpose: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.purpose = purpose
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        if self.purpose is not None:
            result["purpose"] = self.purpose
        return result


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    """规划时路径事实；未来执行前必须交给 ``PathGuard.revalidate`` 复检。"""

    requested_path: str
    purpose: str
    normalized_path: str
    resolved_path: str
    exists: bool
    target_identity: tuple[int, int] | None
    anchor_path: str
    anchor_identity: tuple[int, int]
    component_identities: tuple[tuple[str, tuple[int, int]], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_path": self.requested_path,
            "purpose": self.purpose,
            "normalized_path": self.normalized_path,
            "resolved_path": self.resolved_path,
            "exists": self.exists,
            "target_identity": list(self.target_identity) if self.target_identity is not None else None,
            "anchor_path": self.anchor_path,
            "anchor_identity": list(self.anchor_identity),
            "component_identities": [
                {"path": path, "identity": list(identity)}
                for path, identity in self.component_identities
            ],
        }


class PathProbe(Protocol):
    def lstat(self, path: str) -> os.stat_result: ...

    def realpath(self, path: str) -> str: ...

    def drive_kind(self, drive_root: str) -> str: ...


class WindowsPathProbe:
    """默认 Windows 文件系统探针；所有可变事实每次调用都重新读取。"""

    _DRIVE_KINDS = {
        0: "unknown",
        1: "missing",
        2: "removable",
        3: "fixed",
        4: "remote",
        5: "cdrom",
        6: "ramdisk",
    }

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def drive_kind(self, drive_root: str) -> str:
        if os.name != "nt":
            return "unknown"
        value = int(ctypes.windll.kernel32.GetDriveTypeW(str(drive_root)))
        return self._DRIVE_KINDS.get(value, "unknown")


class PathGuard:
    """规范化并验证固定 MVP 根目录内的普通 Windows 路径。"""

    allowed_root = DEFAULT_ALLOWED_ROOT
    working_directory = DEFAULT_WORKING_DIRECTORY

    def __init__(
        self,
        *,
        probe: PathProbe | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._probe = probe or WindowsPathProbe()
        source = os.environ if environment is None else environment
        self._environment = {
            str(key).casefold(): str(value)
            for key, value in source.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        self._allowed_root_key = ntpath.normcase(ntpath.normpath(self.allowed_root))
        self._working_directory_key = ntpath.normcase(ntpath.normpath(self.working_directory))
        if not self._is_within_allowed_root(self.working_directory):
            raise RuntimeError("默认工作目录必须位于允许根目录内")

    def __call__(self, value: str, purpose: str) -> Path:
        """兼容现有工具 ``path_validator(value, purpose)`` 窄接口。"""

        return Path(self.snapshot(value, purpose).normalized_path)

    def snapshot(self, value: str, purpose: str) -> PathSnapshot:
        requested = self._require_input(value, purpose)
        normalized = self._normalize(requested, purpose)
        self._require_fixed_allowed_drive(normalized, purpose)
        return self._inspect(requested, normalized, purpose)

    def revalidate(self, snapshot: PathSnapshot) -> PathSnapshot:
        """执行前重新解析路径，并拒绝链接替换、身份变化和出现/消失竞态。"""

        if not isinstance(snapshot, PathSnapshot):
            raise TypeError("snapshot 必须是 PathSnapshot")
        current = self.snapshot(snapshot.normalized_path, snapshot.purpose)

        if snapshot.exists and not current.exists:
            raise self._error("target_disappeared", "执行前目标已消失", snapshot.normalized_path, snapshot.purpose)
        if not snapshot.exists and current.exists:
            raise self._error("target_appeared", "执行前目标已出现", snapshot.normalized_path, snapshot.purpose)
        if snapshot.exists and snapshot.target_identity != current.target_identity:
            raise self._error("path_identity_changed", "执行前目标身份已变化", snapshot.normalized_path, snapshot.purpose)
        if snapshot.anchor_identity != current.anchor_identity:
            raise self._error("anchor_identity_changed", "执行前最近既有父路径身份已变化", snapshot.anchor_path, snapshot.purpose)
        if snapshot.component_identities != current.component_identities:
            raise self._error("path_identity_changed", "执行前路径组成身份已变化", snapshot.normalized_path, snapshot.purpose)
        if ntpath.normcase(snapshot.resolved_path) != ntpath.normcase(current.resolved_path):
            raise self._error("resolved_path_changed", "执行前真实目标路径已变化", snapshot.normalized_path, snapshot.purpose)
        return current

    def _require_input(self, value: str, purpose: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 32767 or "\x00" in value:
            raise self._error("invalid_path", "path 必须是非空 Windows 路径字符串", None, purpose)
        if not isinstance(purpose, str) or not purpose or len(purpose) > 64:
            raise TypeError("purpose 必须是 1..64 个字符的字符串")
        return value

    def _expand_environment(self, value: str, purpose: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            replacement = self._environment.get(name.casefold())
            if replacement is None:
                raise self._error(
                    "unresolved_environment_variable",
                    "路径包含未解析的环境变量",
                    value,
                    purpose,
                )
            return replacement

        expanded = _ENVIRONMENT_PATTERN.sub(replace, value)
        if "%" in expanded:
            raise self._error(
                "unresolved_environment_variable",
                "路径包含不完整或未解析的环境变量",
                value,
                purpose,
            )
        return expanded

    def _normalize(self, value: str, purpose: str) -> str:
        expanded = self._expand_environment(value, purpose).replace("/", "\\")
        if expanded.startswith("\\\\"):
            raise self._error("unc_path_denied", "UNC、设备和网络路径不在 MVP 范围内", value, purpose)
        drive, tail = ntpath.splitdrive(expanded)
        if drive and not tail.startswith("\\"):
            raise self._error("ambiguous_path", "拒绝盘符相对路径", value, purpose)
        if not drive and tail.startswith("\\"):
            raise self._error("ambiguous_path", "拒绝相对当前盘符根目录的路径", value, purpose)
        if ":" in tail:
            raise self._error("ambiguous_path", "拒绝备用数据流或含歧义冒号的路径", value, purpose)
        raw_parts = tuple(part for part in tail.split("\\") if part not in {"", ".", ".."})
        if any(part.endswith((".", " ")) for part in raw_parts):
            raise self._error("ambiguous_path", "拒绝 Win32 会折叠的尾随点或空格路径", value, purpose)

        candidate = expanded if drive else ntpath.join(self.working_directory, expanded)
        normalized = ntpath.normpath(candidate)
        normalized_drive, normalized_tail = ntpath.splitdrive(normalized)
        allowed_drive, _ = ntpath.splitdrive(self.allowed_root)
        if ntpath.normcase(normalized_drive) != ntpath.normcase(allowed_drive):
            raise self._error("drive_not_allowed", "路径盘符不在 MVP 允许范围内", normalized, purpose)
        if not normalized_tail.startswith("\\"):
            raise self._error("ambiguous_path", "路径无法规范化为绝对路径", normalized, purpose)
        if any(_SHORT_NAME_PATTERN.fullmatch(part) for part in self._parts(normalized)):
            raise self._error("short_name_denied", "拒绝 Windows 8.3 短文件名别名", normalized, purpose)
        if not self._is_within_allowed_root(normalized):
            raise self._error("path_outside_allowed_root", "路径越出允许根目录", normalized, purpose)
        return normalized

    def _require_fixed_allowed_drive(self, normalized: str, purpose: str) -> None:
        drive, _ = ntpath.splitdrive(normalized)
        drive_root = drive + "\\"
        try:
            kind = self._probe.drive_kind(drive_root)
        except (OSError, PermissionError) as exc:
            raise self._error("drive_resolution_failed", "无法可靠确认路径所在驱动器", normalized, purpose) from exc
        if kind == "remote":
            raise self._error("mapped_drive_denied", "映射盘或网络盘不在 MVP 范围内", normalized, purpose)
        if kind != "fixed":
            raise self._error("drive_not_allowed", "只允许固定本地驱动器", normalized, purpose)

    def _inspect(self, requested: str, normalized: str, purpose: str) -> PathSnapshot:
        components = self._components(normalized)
        identities: list[tuple[str, tuple[int, int]]] = []
        anchor_path: str | None = None
        anchor_identity: tuple[int, int] | None = None
        anchor_real: str | None = None
        missing_index: int | None = None

        for index, component in enumerate(components):
            try:
                stat_result = self._probe.lstat(component)
            except FileNotFoundError:
                missing_index = index
                break
            except PermissionError as exc:
                raise self._error("permission_denied", "无权检查路径安全状态", component, purpose) from exc
            except OSError as exc:
                raise self._error("path_resolution_failed", "无法可靠读取路径安全状态", component, purpose) from exc

            if self._is_reparse(stat_result):
                raise self._error("reparse_point_denied", "符号链接、Junction 或 Reparse Point 不在 MVP 范围内", component, purpose)
            identity = self._identity(stat_result)
            identities.append((component, identity))
            anchor_path = component
            anchor_identity = identity
            anchor_real = self._realpath(component, purpose)

        if anchor_path is None or anchor_identity is None or anchor_real is None:
            raise self._error("path_resolution_failed", "无法确认允许根目录的真实状态", normalized, purpose)

        exists = missing_index is None
        if exists:
            resolved = anchor_real
            if not self._is_within_allowed_root(resolved):
                raise self._error(
                    "resolved_path_outside_allowed_root",
                    "真实目标路径越出允许根目录",
                    resolved,
                    purpose,
                )
            target_identity = anchor_identity
        else:
            remainder = components[missing_index:]
            resolved = ntpath.normpath(ntpath.join(anchor_real, *[ntpath.basename(item) for item in remainder]))
            target_identity = None
            if not self._is_within_allowed_root(resolved):
                raise self._error(
                    "resolved_path_outside_allowed_root",
                    "真实目标路径越出允许根目录",
                    resolved,
                    purpose,
                )

        return PathSnapshot(
            requested_path=requested,
            purpose=purpose,
            normalized_path=normalized,
            resolved_path=resolved,
            exists=exists,
            target_identity=target_identity,
            anchor_path=anchor_path,
            anchor_identity=anchor_identity,
            component_identities=tuple(identities),
        )

    def _realpath(self, path: str, purpose: str) -> str:
        try:
            resolved = str(self._probe.realpath(path)).replace("/", "\\")
        except PermissionError as exc:
            raise self._error("permission_denied", "无权解析真实目标路径", path, purpose) from exc
        except OSError as exc:
            raise self._error("path_resolution_failed", "无法可靠解析真实目标路径", path, purpose) from exc
        if resolved.startswith("\\\\"):
            raise self._error("unc_path_denied", "真实目标解析为 UNC 或网络路径", resolved, purpose)
        resolved = ntpath.normpath(resolved)
        drive, tail = ntpath.splitdrive(resolved)
        allowed_drive, _ = ntpath.splitdrive(self.allowed_root)
        if ntpath.normcase(drive) != ntpath.normcase(allowed_drive) or not tail.startswith("\\"):
            raise self._error(
                "resolved_path_outside_allowed_root",
                "真实目标路径越出允许根目录",
                resolved,
                purpose,
            )
        if not self._is_related_to_allowed_root(resolved):
            raise self._error(
                "resolved_path_outside_allowed_root",
                "真实目标路径越出允许根目录",
                resolved,
                purpose,
            )
        return resolved

    def _components(self, path: str) -> tuple[str, ...]:
        drive, tail = ntpath.splitdrive(path)
        current = drive + "\\"
        result = [current]
        for part in (item for item in tail.split("\\") if item):
            current = ntpath.join(current, part)
            result.append(current)
        return tuple(result)

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        _, tail = ntpath.splitdrive(path)
        return tuple(part for part in tail.split("\\") if part not in {"", ".", ".."})

    def _is_within_allowed_root(self, path: str) -> bool:
        try:
            return ntpath.commonpath((self._allowed_root_key, ntpath.normcase(ntpath.normpath(path)))) == self._allowed_root_key
        except ValueError:
            return False

    def _is_related_to_allowed_root(self, path: str) -> bool:
        normalized = ntpath.normcase(ntpath.normpath(path))
        try:
            common = ntpath.commonpath((self._allowed_root_key, normalized))
        except ValueError:
            return False
        return common in {self._allowed_root_key, normalized}

    @staticmethod
    def _identity(stat_result: os.stat_result) -> tuple[int, int]:
        return int(stat_result.st_dev), int(stat_result.st_ino)

    @staticmethod
    def _is_reparse(stat_result: os.stat_result) -> bool:
        attributes = int(getattr(stat_result, "st_file_attributes", 0))
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(stat_result.st_mode)

    @staticmethod
    def _error(code: str, message: str, path: str | None, purpose: str | None) -> PathGuardError:
        return PathGuardError(code, message, path=path, purpose=purpose)