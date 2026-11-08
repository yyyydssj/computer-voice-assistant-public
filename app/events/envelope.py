"""统一跨模块事件 envelope。"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.base import ContractError, StrictContract, parse_utc_timestamp, require_identifier, require_positive


@dataclass(frozen=True, slots=True)
class EventEnvelope(StrictContract):
    event_id: str
    event_type: str
    source: str
    source_instance_id: str
    session_id: str
    event_sequence: int
    created_at: str
    received_at: str
    request_id: str | None = None
    task_id: str | None = None
    plan_id: str | None = None
    plan_version: int | None = None
    plan_hash: str | None = None
    action_id: str | None = None
    attempt_id: str | None = None
    audio_session_id: str | None = None
    provider_generation: int | None = None
    stream_id: str | None = None
    utterance_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "source", "source_instance_id", "session_id"):
            require_identifier(name, getattr(self, name))
        require_positive("event_sequence", self.event_sequence)
        parse_utc_timestamp("created_at", self.created_at)
        parse_utc_timestamp("received_at", self.received_at)
        for name in ("request_id", "task_id", "plan_id", "action_id", "attempt_id", "audio_session_id", "stream_id", "utterance_id"):
            value = getattr(self, name)
            if value is not None:
                require_identifier(name, value)
        if self.plan_version is not None:
            require_positive("plan_version", self.plan_version)
        if self.provider_generation is not None:
            require_positive("provider_generation", self.provider_generation)
        if self.source in {"audio", "asr", "tts"} or self.event_type.startswith(("audio.", "asr.", "tts.")):
            missing = [name for name in ("audio_session_id", "provider_generation", "stream_id") if getattr(self, name) is None]
            if missing:
                raise ContractError(f"音频事件缺少字段: {', '.join(missing)}")