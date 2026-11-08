"""P1 运行时生命周期、单实例和安全关闭。

本模块只处理本地进程生命周期事实，不连接 Agent、Provider、音频设备或电脑工具。
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

try:
    import msvcrt
except ImportError:  # pragma: no cover - only used on non-Windows development hosts.
    msvcrt = None  # type: ignore[assignment]


class LifecycleError(RuntimeError):
    """生命周期操作失败。"""


class InstanceAlreadyRunningError(LifecycleError):
    """同一个锁路径已有活动实例。"""


@dataclass(frozen=True)
class EventRecord:
    """不含敏感内容的结构化生命周期事件。"""

    instance_id: str
    sequence: int
    event_type: str
    occurred_at: str
    details: Mapping[str, str | int | float | bool | None]

    def as_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "details": dict(self.details),
        }


_SAFE_DETAIL_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_FORBIDDEN_DETAIL_TERMS = frozenset(
    {"api_key", "apikey", "token", "secret", "password", "audio", "file_content"}
)


def _validate_details(
    details: Mapping[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool | None]:
    clean: dict[str, str | int | float | bool | None] = {}
    for key, value in details.items():
        if not key or any(char not in _SAFE_DETAIL_KEY_CHARS for char in key):
            raise LifecycleError("生命周期事件字段名不安全")
        if key.lower() in _FORBIDDEN_DETAIL_TERMS:
            raise LifecycleError("生命周期事件不得包含敏感字段")
        if isinstance(value, (dict, list, tuple, set, bytes)):
            raise LifecycleError("生命周期事件只允许扁平基础类型")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise LifecycleError("生命周期事件包含不支持的字段类型")
        if isinstance(value, str) and len(value) > 256:
            raise LifecycleError("生命周期事件文本字段过长")
        clean[key] = value
    return clean


class _SingleInstanceLock:
    """使用 Windows 文件锁实现单实例约束，不依赖删除锁文件判断陈旧状态。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        if msvcrt is None:
            raise LifecycleError("当前平台不支持本阶段的单实例文件锁")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            try:
                handle.close()  # type: ignore[union-attr]
            except (UnboundLocalError, AttributeError):
                pass
            raise InstanceAlreadyRunningError("应用已有活动实例") from exc
        self._handle = handle
        self._write_owner_marker()

    def _write_owner_marker(self) -> None:
        if self._handle is None:
            raise LifecycleError("单实例锁尚未获取")
        marker = {
            "pid": os.getpid(),
            "nonce": secrets.token_hex(8),
        }
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(marker, ensure_ascii=True).encode("ascii"))
        self._handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if msvcrt is not None:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


class RuntimeLifecycle:
    """管理单实例生命周期和安全关闭事实。"""

    def __init__(
        self,
        *,
        lock_path: Path | str,
        instance_id: str | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
    ) -> None:
        self.lock_path = Path(lock_path).expanduser().resolve()
        self.instance_id = instance_id or str(uuid.uuid4())
        try:
            uuid.UUID(self.instance_id)
        except ValueError as exc:
            raise LifecycleError("instance_id 必须是 UUID") from exc
        self._event_sink = event_sink
        self._lock = _SingleInstanceLock(self.lock_path)
        self._sequence = 0
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> EventRecord:
        if self._closed:
            raise LifecycleError("已关闭的生命周期不能重新启动")
        if self._started:
            raise LifecycleError("生命周期已经启动")
        self._lock.acquire()
        self._started = True
        return self.emit("runtime_started")

    def emit(
        self,
        event_type: str,
        details: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> EventRecord:
        if not self._started:
            raise LifecycleError("生命周期尚未启动")
        if self._closed:
            raise LifecycleError("生命周期已经关闭")
        if not event_type or any(char not in _SAFE_DETAIL_KEY_CHARS for char in event_type):
            raise LifecycleError("事件类型不安全")
        self._sequence += 1
        record = EventRecord(
            instance_id=self.instance_id,
            sequence=self._sequence,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            details=_validate_details(details or {}),
        )
        if self._event_sink is not None:
            self._event_sink(record)
        return record

    def close(self) -> EventRecord:
        """安全关闭且幂等；重复调用只返回第一次关闭事件。"""

        if self._closed:
            return EventRecord(
                instance_id=self.instance_id,
                sequence=self._sequence,
                event_type="runtime_closed",
                occurred_at=datetime.now(timezone.utc).isoformat(),
                details={"idempotent": True},
            )
        if not self._started:
            self._closed = True
            return EventRecord(
                instance_id=self.instance_id,
                sequence=self._sequence,
                event_type="runtime_closed",
                occurred_at=datetime.now(timezone.utc).isoformat(),
                details={"idempotent": True, "started": False},
            )
        record = self.emit("runtime_closing")
        self._lock.release()
        self._closed = True
        return record