"""P6 批次 5：ASR 结果接纳闸门。

ResultGate 位于 Provider adapter 与对话主控之间。它只负责当前会话边界内的
事件身份、顺序、终态和 final 接纳，不创建 Agent request，不调用 TTS，也不
把 Provider 的 ``is_final`` 字段单独当作业务可靠性证明。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.contracts.base import ContractError, parse_utc_timestamp, require_identifier, require_positive
from app.events import EventDecision, EventEnvelope, EventGate

from . import AsrEvent, AsrEventKind


class ResultGateDecision(str, Enum):
    ACCEPTED_FINAL = "accepted_final"
    DROP_INTERIM = "drop_interim"
    REJECT_EMPTY = "reject_empty"
    INVALID_EVENT = "invalid_event"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    CROSS_SESSION = "cross_session"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_UTTERANCE = "stale_utterance"
    STALE_SOURCE_INSTANCE = "stale_source_instance"
    AFTER_TERMINAL = "after_terminal"


class ResultGateState(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AsrResultCandidate:
    """已带完整边界身份、但尚未通过 ResultGate 的 Provider 事件。"""

    event_id: str
    event_type: str
    source_instance_id: str
    session_id: str
    audio_session_id: str
    provider_generation: int
    stream_id: str
    utterance_id: str
    event_sequence: int
    text: str
    is_final: bool
    provider_timestamp: str
    received_at: str

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "event_type",
            "source_instance_id",
            "session_id",
            "audio_session_id",
            "stream_id",
            "utterance_id",
        ):
            require_identifier(name, getattr(self, name))
        require_positive("provider_generation", self.provider_generation)
        require_positive("event_sequence", self.event_sequence)
        if not isinstance(self.text, str):
            raise TypeError("text 必须是字符串")
        if not isinstance(self.is_final, bool):
            raise TypeError("is_final 必须是 bool")
        parse_utc_timestamp("provider_timestamp", self.provider_timestamp)
        parse_utc_timestamp("received_at", self.received_at)

    def to_envelope(self) -> EventEnvelope:
        return EventEnvelope(
            event_id=self.event_id,
            event_type=self.event_type,
            source="asr",
            source_instance_id=self.source_instance_id,
            session_id=self.session_id,
            event_sequence=self.event_sequence,
            created_at=self.provider_timestamp,
            received_at=self.received_at,
            audio_session_id=self.audio_session_id,
            provider_generation=self.provider_generation,
            stream_id=self.stream_id,
            utterance_id=self.utterance_id,
        )

    @classmethod
    def from_provider_event(
        cls,
        event: AsrEvent,
        *,
        session_id: str,
        source_instance_id: str,
    ) -> "AsrResultCandidate":
        if not isinstance(event, AsrEvent):
            raise TypeError("event 必须是 AsrEvent")
        require_identifier("session_id", session_id)
        require_identifier("source_instance_id", source_instance_id)
        if event.kind is AsrEventKind.FINAL:
            event_type = "asr.final"
        elif event.kind is AsrEventKind.EMPTY:
            event_type = "asr.empty"
        else:  # pragma: no cover - Enum 扩展时保持 fail-closed
            raise ContractError("未知的 ASR Provider 事件类型")
        return cls(
            event_id=event.event_id,
            event_type=event_type,
            source_instance_id=source_instance_id,
            session_id=session_id,
            audio_session_id=event.audio_session_id,
            provider_generation=event.provider_generation,
            stream_id=event.stream_id,
            utterance_id=event.utterance_id,
            event_sequence=event.event_sequence,
            text=event.text,
            is_final=event.is_final,
            provider_timestamp=event.provider_timestamp,
            received_at=event.received_at,
        )


@dataclass(frozen=True, slots=True)
class ResultGateResult:
    decision: ResultGateDecision
    event: AsrResultCandidate | None = None
    text: str = ""
    reliable: bool = False


class ResultGate:
    """只接纳当前 utterance 的一次、非空、顺序可靠的 final。"""

    def __init__(
        self,
        *,
        session_id: str,
        audio_session_id: str,
        provider_generation: int,
        stream_id: str,
        utterance_id: str,
        source_instance_id: str,
    ) -> None:
        require_identifier("session_id", session_id)
        require_identifier("audio_session_id", audio_session_id)
        require_positive("provider_generation", provider_generation)
        require_identifier("stream_id", stream_id)
        require_identifier("utterance_id", utterance_id)
        require_identifier("source_instance_id", source_instance_id)
        self._source_instance_id = source_instance_id
        self._state = ResultGateState.OPEN
        self._seen_event_ids: set[str] = set()
        self._event_gate = EventGate(
            session_id=session_id,
            audio_session_id=audio_session_id,
            provider_generation=provider_generation,
            stream_id=stream_id,
            utterance_id=utterance_id,
        )

    @property
    def state(self) -> ResultGateState:
        return self._state

    def evaluate(self, candidate: AsrResultCandidate) -> ResultGateResult:
        if not isinstance(candidate, AsrResultCandidate):
            return ResultGateResult(ResultGateDecision.INVALID_EVENT)

        # A replay of the exact event keeps the more precise duplicate diagnosis,
        # even after the utterance is already terminal.
        if candidate.event_id in self._seen_event_ids:
            return ResultGateResult(ResultGateDecision.DUPLICATE)
        if candidate.source_instance_id != self._source_instance_id:
            return ResultGateResult(ResultGateDecision.STALE_SOURCE_INSTANCE)
        if self._state is not ResultGateState.OPEN:
            return ResultGateResult(ResultGateDecision.AFTER_TERMINAL)

        try:
            envelope = candidate.to_envelope()
        except (ContractError, TypeError, ValueError):
            return ResultGateResult(ResultGateDecision.INVALID_EVENT)
        decision = self._event_gate.evaluate(envelope)
        if decision is not EventDecision.ACCEPT:
            return ResultGateResult(self._map_event_decision(decision))
        self._seen_event_ids.add(candidate.event_id)

        if not candidate.is_final:
            return ResultGateResult(ResultGateDecision.DROP_INTERIM)
        if candidate.event_type not in {"asr.final", "asr.empty"}:
            self._state = ResultGateState.REJECTED
            return ResultGateResult(ResultGateDecision.INVALID_EVENT)

        normalized = " ".join(candidate.text.split())
        if not normalized:
            # An empty final closes this utterance. A later event cannot turn an
            # already rejected utterance into a new accepted request.
            self._state = ResultGateState.REJECTED
            return ResultGateResult(ResultGateDecision.REJECT_EMPTY)

        accepted = replace(candidate, text=normalized)
        self._state = ResultGateState.ACCEPTED
        return ResultGateResult(
            decision=ResultGateDecision.ACCEPTED_FINAL,
            event=accepted,
            text=normalized,
            reliable=True,
        )

    def close(self) -> None:
        """关闭当前 utterance；迟到 Provider 事件只能得到 after_terminal。"""
        if self._state is ResultGateState.OPEN:
            self._state = ResultGateState.REJECTED

    @staticmethod
    def _map_event_decision(decision: EventDecision) -> ResultGateDecision:
        mapping = {
            EventDecision.DUPLICATE: ResultGateDecision.DUPLICATE,
            EventDecision.OUT_OF_ORDER: ResultGateDecision.OUT_OF_ORDER,
            EventDecision.CROSS_SESSION: ResultGateDecision.CROSS_SESSION,
            EventDecision.STALE_AUDIO_SESSION: ResultGateDecision.STALE_AUDIO_SESSION,
            EventDecision.STALE_GENERATION: ResultGateDecision.STALE_GENERATION,
            EventDecision.STALE_STREAM: ResultGateDecision.STALE_STREAM,
            EventDecision.STALE_UTTERANCE: ResultGateDecision.STALE_UTTERANCE,
            EventDecision.AFTER_TERMINAL: ResultGateDecision.AFTER_TERMINAL,
        }
        return mapping.get(decision, ResultGateDecision.INVALID_EVENT)


__all__ = [
    "AsrResultCandidate",
    "ResultGate",
    "ResultGateDecision",
    "ResultGateResult",
    "ResultGateState",
]
