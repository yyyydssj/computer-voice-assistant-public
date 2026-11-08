"""事件身份、顺序与重复消费闸门。"""

from __future__ import annotations

from enum import Enum

from app.contracts.base import require_identifier

from .envelope import EventEnvelope


class EventDecision(str, Enum):
    ACCEPT = "accept"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    CROSS_SESSION = "cross_session"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_UTTERANCE = "stale_utterance"
    AFTER_TERMINAL = "after_terminal"


class EventGate:
    """只接受当前边界内、同一来源单调递增且未消费的事件。"""

    def __init__(
        self,
        *,
        session_id: str,
        audio_session_id: str | None = None,
        provider_generation: int | None = None,
        stream_id: str | None = None,
        utterance_id: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._audio_session_id = audio_session_id
        self._provider_generation = provider_generation
        self._stream_id = stream_id
        self._utterance_id = utterance_id
        self._seen_event_ids: set[str] = set()
        self._last_sequence_by_source: dict[tuple[str, str], int] = {}
        self._terminal_reached = False

    def evaluate(self, event: EventEnvelope) -> EventDecision:
        if event.session_id != self._session_id:
            return EventDecision.CROSS_SESSION
        if self._audio_session_id is not None and event.audio_session_id != self._audio_session_id:
            return EventDecision.STALE_AUDIO_SESSION
        if self._provider_generation is not None and event.provider_generation != self._provider_generation:
            return EventDecision.STALE_GENERATION
        if self._stream_id is not None and event.stream_id != self._stream_id:
            return EventDecision.STALE_STREAM
        if self._utterance_id is not None and event.utterance_id != self._utterance_id:
            return EventDecision.STALE_UTTERANCE
        if event.event_id in self._seen_event_ids:
            return EventDecision.DUPLICATE
        key = (event.source, event.source_instance_id)
        if self._terminal_reached:
            return EventDecision.AFTER_TERMINAL
        last_sequence = self._last_sequence_by_source.get(key)
        if last_sequence is not None and event.event_sequence < last_sequence:
            return EventDecision.OUT_OF_ORDER
        if last_sequence is not None and event.event_sequence == last_sequence:
            return EventDecision.DUPLICATE
        self._seen_event_ids.add(event.event_id)
        self._last_sequence_by_source[key] = event.event_sequence
        if event.event_type in {"asr.rejected", "audio.closed", "audio.failed"}:
            self._terminal_reached = True
        return EventDecision.ACCEPT


class RequestSubmissionGate:
    """请求 ID 一经提交便拒绝在本进程边界内重复提交。"""

    def __init__(self) -> None:
        self._submitted: set[str] = set()

    def submit_once(self, request_id: str) -> bool:
        require_identifier("request_id", request_id)
        if request_id in self._submitted:
            return False
        self._submitted.add(request_id)
        return True