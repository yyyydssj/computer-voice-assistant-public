"""不含敏感内容的 JSONL 结构化日志。

日志模块只记录生命周期和运行时元数据，不记录 API key、音频、完整文件内容
或可复原敏感信息的原始配置。日志目录由配置对象提供，默认位于仓库外。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


class StructuredLogError(RuntimeError):
    """结构化日志输入或输出失败。"""


_SENSITIVE_NAME_RE = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|token|credential|cookie)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+|sk-|key[-_ ]?)[A-Za-z0-9._~+/=-]{8,}"
)
_FORBIDDEN_NAME_RE = re.compile(
    r"(?i)(?:audio|wav|flac|mp3|voiceprint|embedding|file[_-]?content|raw[_-]?text)"
)
_MAX_TEXT_LENGTH = 512


def _sanitize_value(key: str, value: object) -> object:
    if _SENSITIVE_NAME_RE.search(key):
        return "[REDACTED]"
    if _FORBIDDEN_NAME_RE.search(key):
        return "[OMITTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _sanitize_value(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(key, item) for item in value]
    if isinstance(value, bytes):
        return "[OMITTED]"
    if isinstance(value, Path):
        return _sanitize_path(value)
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_LENGTH:
            return value[:_MAX_TEXT_LENGTH] + "…"
        if _SENSITIVE_VALUE_RE.search(value):
            return "[REDACTED]"
        return value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_TEXT_LENGTH]


def _sanitize_path(path: Path) -> str:
    """只记录路径类别，不把用户本地路径写入普通日志。"""

    suffix = path.suffix.lower()
    if suffix in {".wav", ".flac", ".mp3", ".ogg", ".m4a"}:
        return "[AUDIO_PATH_OMITTED]"
    return f"[PATH:{suffix or 'none'}]"


class StructuredLogger:
    """线程安全 JSONL 日志写入器。"""

    def __init__(self, log_directory: Path | str, *, filename: str = "runtime.jsonl") -> None:
        self.log_directory = Path(log_directory).expanduser().resolve()
        if not filename or Path(filename).name != filename or not filename.endswith(".jsonl"):
            raise StructuredLogError("日志文件名必须是当前目录下的 .jsonl 文件")
        self.log_path = self.log_directory / filename
        self._lock = threading.Lock()
        self._closed = False
        self._sequence = 0
        self._initialize_directory()

    def _initialize_directory(self) -> None:
        try:
            self.log_directory.mkdir(parents=True, exist_ok=True)
            if not self.log_directory.is_dir():
                raise StructuredLogError("日志路径不是目录")
            probe = self.log_directory / ".write-check"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise StructuredLogError("日志目录不可写") from exc

    def write(
        self,
        event_type: str,
        *,
        instance_id: str,
        event_sequence: int,
        details: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if self._closed:
            raise StructuredLogError("日志器已经关闭")
        if not event_type or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in event_type):
            raise StructuredLogError("事件类型不安全")
        if not instance_id or not isinstance(event_sequence, int) or event_sequence < 1:
            raise StructuredLogError("生命周期事件关联字段无效")
        clean_details = {
            str(key): _sanitize_value(str(key), value)
            for key, value in (details or {}).items()
        }
        with self._lock:
            self._sequence += 1
            record: dict[str, object] = {
                "log_sequence": self._sequence,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "instance_id": instance_id,
                "event_sequence": event_sequence,
                "event_type": event_type,
                "details": clean_details,
            }
            try:
                with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
                    json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise StructuredLogError("结构化日志写入失败") from exc
        return record

    def close(self) -> None:
        self._closed = True