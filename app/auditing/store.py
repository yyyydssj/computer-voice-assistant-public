"""P5 批次 7：脱敏、哈希链与 fail-closed 审计持久化。"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from app.contracts import AuditEvent, seal_audit_event, validate_audit_chain
from app.contracts.base import parse_utc_timestamp, require_identifier


class AuditPersistenceError(RuntimeError):
    """审计无法可靠追加、读取或验证时的 fail-closed 错误。"""


_SECRET_NAME = re.compile(r"(?:api[_-]?key|authorization|password|passwd|secret|token|credential|cookie)", re.I)
_SECRET_VALUE = re.compile(r"(?i)(?:bearer\s+|sk-|key[-_ ]?)[A-Za-z0-9._~+/=-]{8,}")
_PATH_NAME = re.compile(r"(?:path|directory|filename|file_name|source|target|backup)", re.I)
_PRIVATE_NAME = re.compile(r"(?:content|body|text|audio|voiceprint|embedding|prompt|response)", re.I)
_MAX_TEXT = 256


def _safe_value(key: str, value: Any) -> Any:
    if _SECRET_NAME.search(key):
        return "[REDACTED]"
    if _PATH_NAME.search(key):
        return "[PATH_OMITTED]"
    if _PRIVATE_NAME.search(key):
        return "[OMITTED]"
    if isinstance(value, Mapping):
        return {str(child): _safe_value(str(child), item) for child, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(key, item) for item in value]
    if isinstance(value, bytes):
        return "[OMITTED]"
    if isinstance(value, Path):
        return "[PATH_OMITTED]"
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return "[REDACTED]"
        if len(value) > _MAX_TEXT:
            return value[:_MAX_TEXT] + "…"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_TEXT]


def _safe_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if details is None:
        return {}
    if not isinstance(details, Mapping):
        raise TypeError("details 必须是对象")
    return {str(key): _safe_value(str(key), value) for key, value in details.items()}


class AuditStore:
    """单实例追加式 JSONL 审计存储；任何链路/写入异常都拒绝继续。"""

    def __init__(self, audit_directory: Path | str, *, source_instance_id: str, filename: str = "audit.jsonl") -> None:
        require_identifier("source_instance_id", source_instance_id)
        if not filename or Path(filename).name != filename or not filename.endswith(".jsonl"):
            raise AuditPersistenceError("审计文件名必须是当前目录下的 .jsonl 文件")
        self.audit_directory = Path(audit_directory).expanduser().resolve()
        self.path = self.audit_directory / filename
        self.source_instance_id = source_instance_id
        self._lock = threading.Lock()
        self._closed = False
        self._initialize()

    def _initialize(self) -> None:
        try:
            self.audit_directory.mkdir(parents=True, exist_ok=True)
            if not self.audit_directory.is_dir():
                raise AuditPersistenceError("审计路径不是目录")
            if self.path.exists() and not self.path.is_file():
                raise AuditPersistenceError("审计文件不是普通文件")
            self.path.touch(exist_ok=True)
        except OSError as exc:
            raise AuditPersistenceError("审计目录或文件不可用") from exc

    def append(self, event_type: str, *, subject_id: str, details: Mapping[str, Any] | None = None,
               created_at: str | None = None) -> AuditEvent:
        if self._closed:
            raise AuditPersistenceError("审计存储已经关闭")
        require_identifier("event_type", event_type)
        require_identifier("subject_id", subject_id)
        occurred_at = created_at or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
        parse_utc_timestamp("created_at", occurred_at)
        clean = _safe_details(details)
        with self._lock:
            try:
                events = self._read_events_locked()
                sequence = events[-1].event_sequence + 1 if events else 1
                previous = events[-1].event_hash if events else None
                event = seal_audit_event(
                    AuditEvent(
                        audit_event_id=f"audit-{self.source_instance_id}-{sequence}",
                        event_type=event_type,
                        source_instance_id=self.source_instance_id,
                        event_sequence=sequence,
                        created_at=occurred_at,
                        subject_id=subject_id,
                        details=clean,
                    ),
                    previous_event_hash=previous,
                )
                self._append_locked(event)
                return event
            except AuditPersistenceError:
                raise
            except Exception as exc:
                raise AuditPersistenceError("审计追加失败，已拒绝继续") from exc

    def read_events(self) -> tuple[AuditEvent, ...]:
        if self._closed:
            raise AuditPersistenceError("审计存储已经关闭")
        with self._lock:
            return self._read_events_locked()

    def close(self) -> None:
        self._closed = True

    def _read_events_locked(self) -> tuple[AuditEvent, ...]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            events = tuple(AuditEvent.from_json(line) for line in lines if line.strip())
            if events:
                validate_audit_chain(events)
                if any(event.source_instance_id != self.source_instance_id for event in events):
                    raise AuditPersistenceError("审计来源实例不一致")
            return events
        except AuditPersistenceError:
            raise
        except Exception as exc:
            raise AuditPersistenceError("审计读取或哈希链验证失败") from exc

    def _append_locked(self, event: AuditEvent) -> None:
        encoded = event.to_json() + "\n"
        try:
            with self.path.open("a+", encoding="utf-8", newline="\n") as stream:
                stream.seek(0, os.SEEK_END)
                start = stream.tell()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            try:
                with self.path.open("r+", encoding="utf-8", newline="\n") as stream:
                    stream.seek(start)
                    stream.truncate()
                    stream.flush()
            except Exception:
                pass
            raise AuditPersistenceError("审计写入失败，执行必须 fail-closed") from exc