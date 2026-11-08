"""Evidence ledger primitives for P7 real-device acceptance.

This module is deliberately side-effect free. It records facts supplied by a
runner; it does not start playback, capture audio, contact an Agent, or infer
human hearing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
import uuid

EvidenceKind = Literal["real_device", "controlled_fault_fixture", "offline_contract"]
TriState = bool | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    name: str
    observed_at: str = field(default_factory=_utc_now)
    monotonic_seconds: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class P7EvidenceLedger:
    """A strict, JSON-serializable ledger with independent evidence fields."""

    batch_id: str = field(default_factory=lambda: "p7-" + uuid.uuid4().hex[:16])
    evidence_kind: EvidenceKind = "real_device"
    request_id: str | None = None
    source_text_sha256: str | None = None
    audio_sha256: str | None = None
    backend_stop_ack: TriState = None
    loopback_output_stopped: TriState = None
    acoustic_output_stopped: TriState = None
    speaker_output_stopped: TriState = None
    user_heard: TriState = None
    status: str = "incomplete"
    blocked_reasons: list[str] = field(default_factory=list)
    events: list[EvidenceEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.evidence_kind not in {
            "real_device",
            "controlled_fault_fixture",
            "offline_contract",
        }:
            raise ValueError("unsupported evidence_kind")
        for name in (
            "backend_stop_ack",
            "loopback_output_stopped",
            "acoustic_output_stopped",
            "speaker_output_stopped",
            "user_heard",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be bool or None")

    @classmethod
    def for_text(cls, text: str, *, evidence_kind: EvidenceKind = "real_device") -> "P7EvidenceLedger":
        if type(text) is not str or not text.strip():
            raise ValueError("text must be a non-empty string")
        ledger = cls(evidence_kind=evidence_kind)
        ledger.source_text_sha256 = sha256_text(text)
        return ledger

    def add_event(self, name: str, *, monotonic_seconds: float | None = None, **details: Any) -> None:
        self.events.append(EvidenceEvent(name, monotonic_seconds=monotonic_seconds, details=details))

    def set_audio(self, audio: bytes) -> None:
        if type(audio) is not bytes or not audio:
            raise ValueError("audio must be non-empty bytes")
        self.audio_sha256 = sha256_bytes(audio)

    def derive_speaker_stop(self) -> TriState:
        """Only two valid, positive measurements can establish a combined stop."""
        if self.loopback_output_stopped is True and self.acoustic_output_stopped is True:
            self.speaker_output_stopped = True
        elif self.loopback_output_stopped is False or self.acoustic_output_stopped is False:
            self.speaker_output_stopped = False
        else:
            self.speaker_output_stopped = None
        return self.speaker_output_stopped

    def finalize(self) -> str:
        self.derive_speaker_stop()
        if self.evidence_kind != "real_device" and "evidence_kind_not_real_device" not in self.blocked_reasons:
            self.blocked_reasons.append("evidence_kind_not_real_device")
        if self.user_heard is None:
            self.blocked_reasons.append("user_heard_pending") if "user_heard_pending" not in self.blocked_reasons else None
        elif self.user_heard is not True:
            self.blocked_reasons.append("user_heard_not_confirmed") if "user_heard_not_confirmed" not in self.blocked_reasons else None
        if self.backend_stop_ack is not True:
            self.blocked_reasons.append("backend_stop_ack_missing") if "backend_stop_ack_missing" not in self.blocked_reasons else None
        if self.speaker_output_stopped is not True:
            self.blocked_reasons.append("speaker_output_stop_not_established") if "speaker_output_stop_not_established" not in self.blocked_reasons else None
        self.status = "complete" if not self.blocked_reasons else "blocked"
        return self.status

    def record_user_heard(self, *, batch_id: str, heard: bool) -> None:
        """Accept only an explicit user assertion for this exact batch."""
        if batch_id != self.batch_id:
            raise ValueError("user confirmation batch_id mismatch")
        if type(heard) is not bool:
            raise ValueError("heard must be bool")
        if self.user_heard is not None:
            raise ValueError("user_heard_already_recorded")
        self.user_heard = heard
        self.add_event("user_heard_confirmed", batch_id=batch_id, heard=heard)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = ["EvidenceEvent", "P7EvidenceLedger", "sha256_bytes", "sha256_text"]
