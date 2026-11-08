"""P7 本地播放事实契约。

本模块只记录已经存在的 Provider request 音频块在本地播放边界上的
不可变元数据事实，不调用播放设备、不携带 PCM/编码 payload，也不表示
用户已经听到音频。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_identifier, require_non_empty, require_positive
from app.tts.lifecycle import (
    ProviderRequestLifecycleError,
    ProviderRequestLifecycleRegistry,
    ProviderRequestLifecycleState,
)


class ProviderRequestPlaybackError(ValueError):
    """本地播放事实违反绑定、状态或原子性约束。"""


class ProviderRequestPlaybackState(str, Enum):
    """句级本地播放状态；不表示设备或用户听见。"""

    NOT_COMMITTED = "NOT_COMMITTED"
    COMMITTED_NOT_STARTED = "COMMITTED_NOT_STARTED"
    PLAYBACK_STARTED = "PLAYBACK_STARTED"
    PARTIALLY_PLAYED = "PARTIALLY_PLAYED"
    PLAYED = "PLAYED"
    ABORTED = "ABORTED"


_TERMINAL_STATES = frozenset(
    {
        ProviderRequestPlaybackState.PLAYED,
        ProviderRequestPlaybackState.ABORTED,
    }
)


@dataclass(frozen=True, slots=True)
class ProviderRequestPlaybackEvent:
    """绑定到一个 Provider request 音频块的不可变播放事件。"""

    event_id: str
    event_sequence: int
    provider_request_id: str
    reply_id: str
    segment_id: str
    provider_generation: int
    audio_chunk_event_id: str
    audio_chunk_index: int
    state: ProviderRequestPlaybackState
    playback_offset_ms: int
    reason: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("provider_request_id", self.provider_request_id),
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
            ("audio_chunk_event_id", self.audio_chunk_event_id),
        ):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise ProviderRequestPlaybackError(str(exc)) from exc
        try:
            require_positive("event_sequence", self.event_sequence)
            require_positive("provider_generation", self.provider_generation)
            require_positive("audio_chunk_index", self.audio_chunk_index)
            require_positive("playback_offset_ms", self.playback_offset_ms, allow_zero=True)
        except ContractError as exc:
            raise ProviderRequestPlaybackError(str(exc)) from exc
        if not isinstance(self.state, ProviderRequestPlaybackState):
            raise ProviderRequestPlaybackError("state 必须是 ProviderRequestPlaybackState")
        if self.state is ProviderRequestPlaybackState.ABORTED:
            try:
                require_non_empty("reason", self.reason or "", maximum=512)
            except ContractError as exc:
                raise ProviderRequestPlaybackError(str(exc)) from exc
        elif self.reason is not None:
            raise ProviderRequestPlaybackError("非 aborted 事件不得携带 reason")
        if self.state in (
            ProviderRequestPlaybackState.COMMITTED_NOT_STARTED,
            ProviderRequestPlaybackState.PLAYBACK_STARTED,
        ) and self.playback_offset_ms != 0:
            raise ProviderRequestPlaybackError(
                f"{self.state.value} 的 playback_offset_ms 必须为 0"
            )
        if self.state in (ProviderRequestPlaybackState.PARTIALLY_PLAYED, ProviderRequestPlaybackState.PLAYED) and self.playback_offset_ms <= 0:
            raise ProviderRequestPlaybackError("partially played 的 playback_offset_ms 必须为正")


@dataclass(frozen=True, slots=True)
class ProviderRequestPlayback:
    """Provider request 的不可变播放事实快照。"""

    provider_request_id: str
    reply_id: str
    segment_id: str
    provider_generation: int
    state: ProviderRequestPlaybackState = ProviderRequestPlaybackState.NOT_COMMITTED
    events: tuple[ProviderRequestPlaybackEvent, ...] = ()
    playback_offset_ms: int = 0
    audio_chunk_event_id: str | None = None
    audio_chunk_index: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_request_id", self.provider_request_id),
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
        ):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise ProviderRequestPlaybackError(str(exc)) from exc
        try:
            require_positive("provider_generation", self.provider_generation)
            require_positive("playback_offset_ms", self.playback_offset_ms, allow_zero=True)
        except ContractError as exc:
            raise ProviderRequestPlaybackError(str(exc)) from exc
        if not isinstance(self.state, ProviderRequestPlaybackState):
            raise ProviderRequestPlaybackError("state 必须是 ProviderRequestPlaybackState")
        if not isinstance(self.events, tuple):
            raise ProviderRequestPlaybackError("events 必须是 tuple")
        if self.state is ProviderRequestPlaybackState.NOT_COMMITTED and self.events:
            raise ProviderRequestPlaybackError("未提交快照不得包含播放事件")
        if not self.events and self.state is not ProviderRequestPlaybackState.NOT_COMMITTED:
            raise ProviderRequestPlaybackError("已提交播放快照必须包含事件")
        seen_ids: set[str] = set()
        previous_offset = 0
        previous_chunk_index = 0
        previous_state = ProviderRequestPlaybackState.NOT_COMMITTED
        chunk_ids: dict[int, str] = {}
        chunk_indexes: dict[str, int] = {}
        for expected, event in enumerate(self.events, start=1):
            if not isinstance(event, ProviderRequestPlaybackEvent):
                raise ProviderRequestPlaybackError("events 只能包含 ProviderRequestPlaybackEvent")
            if event.event_sequence != expected:
                raise ProviderRequestPlaybackError("播放事件序号必须从 1 开始连续")
            if event.event_id in seen_ids:
                raise ProviderRequestPlaybackError("播放 event_id 不得重复")
            seen_ids.add(event.event_id)
            if (
                event.provider_request_id != self.provider_request_id
                or event.reply_id != self.reply_id
                or event.segment_id != self.segment_id
                or event.provider_generation != self.provider_generation
            ):
                raise ProviderRequestPlaybackError("播放事件 identity 必须与快照完整绑定")
            ProviderRequestPlaybackRegistry._assert_step(
                previous_state, previous_offset, event.state, event.playback_offset_ms
            )
            if (event.audio_chunk_index in chunk_ids and chunk_ids[event.audio_chunk_index] != event.audio_chunk_event_id) or (
                event.audio_chunk_event_id in chunk_indexes and chunk_indexes[event.audio_chunk_event_id] != event.audio_chunk_index
            ):
                raise ProviderRequestPlaybackError("audio chunk index and event_id mapping must remain stable")
            chunk_ids[event.audio_chunk_index] = event.audio_chunk_event_id
            chunk_indexes[event.audio_chunk_event_id] = event.audio_chunk_index
            if event.playback_offset_ms < previous_offset:
                raise ProviderRequestPlaybackError("playback_offset_ms 不得倒退")
            if event.audio_chunk_index < previous_chunk_index:
                raise ProviderRequestPlaybackError("audio_chunk_index 不得倒退")
            previous_state = event.state
            previous_offset = event.playback_offset_ms
            previous_chunk_index = event.audio_chunk_index
        if self.events:
            last = self.events[-1]
            if self.state is not last.state:
                raise ProviderRequestPlaybackError("快照 state 必须与最后一个播放事件一致")
            if self.playback_offset_ms != last.playback_offset_ms:
                raise ProviderRequestPlaybackError("快照 playback_offset_ms 必须与最后事件一致")
            if self.audio_chunk_event_id != last.audio_chunk_event_id:
                raise ProviderRequestPlaybackError("快照 audio_chunk_event_id 必须与最后事件一致")
            if self.audio_chunk_index != last.audio_chunk_index:
                raise ProviderRequestPlaybackError("快照 audio_chunk_index 必须与最后事件一致")
        elif self.playback_offset_ms != 0 or self.audio_chunk_event_id is not None or self.audio_chunk_index is not None:
            raise ProviderRequestPlaybackError("未提交快照不得携带播放进度或音频块")
        if (self.audio_chunk_event_id is None) != (self.audio_chunk_index is None):
            raise ProviderRequestPlaybackError("音频块事件 ID 与序号必须同时存在或同时为空")

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


class ProviderRequestPlaybackRegistry:
    """基于 Provider request lifecycle 的本地播放事实 registry。"""

    def __init__(self, lifecycles: ProviderRequestLifecycleRegistry) -> None:
        if not isinstance(lifecycles, ProviderRequestLifecycleRegistry):
            raise TypeError("lifecycles 必须是 ProviderRequestLifecycleRegistry")
        self._lifecycles = lifecycles
        self._snapshots: dict[str, ProviderRequestPlayback] = {}
        self._event_ids: set[str] = set()

    def get(self, provider_request_id: str) -> ProviderRequestPlayback:
        lifecycle = self._get_lifecycle(provider_request_id)
        snapshot = self._snapshots.get(provider_request_id)
        if snapshot is None:
            snapshot = ProviderRequestPlayback(
                provider_request_id=lifecycle.request.provider_request_id,
                reply_id=lifecycle.request.reply_id,
                segment_id=lifecycle.request.segment_id,
                provider_generation=lifecycle.request.provider_generation,
            )
            self._snapshots[provider_request_id] = snapshot
        return snapshot

    def commit(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        audio_chunk_event_id: str,
    ) -> ProviderRequestPlayback:
        return self._record(
            provider_request_id,
            event_id=event_id,
            audio_chunk_event_id=audio_chunk_event_id,
            state=ProviderRequestPlaybackState.COMMITTED_NOT_STARTED,
            playback_offset_ms=0,
        )

    def start(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        audio_chunk_event_id: str,
    ) -> ProviderRequestPlayback:
        return self._record(
            provider_request_id,
            event_id=event_id,
            audio_chunk_event_id=audio_chunk_event_id,
            state=ProviderRequestPlaybackState.PLAYBACK_STARTED,
            playback_offset_ms=0,
        )

    def progress(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        audio_chunk_event_id: str,
        playback_offset_ms: int,
    ) -> ProviderRequestPlayback:
        return self._record(
            provider_request_id,
            event_id=event_id,
            audio_chunk_event_id=audio_chunk_event_id,
            state=ProviderRequestPlaybackState.PARTIALLY_PLAYED,
            playback_offset_ms=playback_offset_ms,
        )

    def played(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        audio_chunk_event_id: str,
        playback_offset_ms: int,
    ) -> ProviderRequestPlayback:
        return self._record(
            provider_request_id,
            event_id=event_id,
            audio_chunk_event_id=audio_chunk_event_id,
            state=ProviderRequestPlaybackState.PLAYED,
            playback_offset_ms=playback_offset_ms,
        )

    def abort(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        audio_chunk_event_id: str,
        playback_offset_ms: int,
        reason: str,
    ) -> ProviderRequestPlayback:
        return self._record(
            provider_request_id,
            event_id=event_id,
            audio_chunk_event_id=audio_chunk_event_id,
            state=ProviderRequestPlaybackState.ABORTED,
            playback_offset_ms=playback_offset_ms,
            reason=reason,
        )

    def events_for(self, provider_request_id: str) -> tuple[ProviderRequestPlaybackEvent, ...]:
        return self.get(provider_request_id).events

    def _record(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        audio_chunk_event_id: str,
        state: ProviderRequestPlaybackState,
        playback_offset_ms: int,
        reason: str | None = None,
    ) -> ProviderRequestPlayback:
        if event_id in self._event_ids:
            raise ProviderRequestPlaybackError("播放 event_id 不得重复")
        lifecycle = self._get_lifecycle(provider_request_id)
        if self._lifecycles.active_generation(lifecycle.request.provider_name) not in (
            None, lifecycle.request.provider_generation
        ):
            raise ProviderRequestPlaybackError("provider_generation differs from active generation fence")
        if lifecycle.state in (ProviderRequestLifecycleState.FAILED, ProviderRequestLifecycleState.CANCELLED):
            if state is not ProviderRequestPlaybackState.ABORTED:
                raise ProviderRequestPlaybackError("terminal lifecycle disallows new playback progress")
        current = self._snapshots.get(provider_request_id)
        if current is None:
            current = ProviderRequestPlayback(
                provider_request_id=lifecycle.request.provider_request_id,
                reply_id=lifecycle.request.reply_id,
                segment_id=lifecycle.request.segment_id,
                provider_generation=lifecycle.request.provider_generation,
            )
        chunk = next(
            (item for item in lifecycle.audio_chunks if item.event_id == audio_chunk_event_id),
            None,
        )
        if chunk is None:
            raise ProviderRequestPlaybackError("audio_chunk_event_id 未绑定当前 Provider request")
        self._assert_transition(current, state, playback_offset_ms)
        event = ProviderRequestPlaybackEvent(
            event_id=event_id,
            event_sequence=len(current.events) + 1,
            provider_request_id=lifecycle.request.provider_request_id,
            reply_id=lifecycle.request.reply_id,
            segment_id=lifecycle.request.segment_id,
            provider_generation=lifecycle.request.provider_generation,
            audio_chunk_event_id=chunk.event_id,
            audio_chunk_index=chunk.audio_chunk_index,
            state=state,
            playback_offset_ms=playback_offset_ms,
            reason=reason,
        )
        updated = ProviderRequestPlayback(
            provider_request_id=current.provider_request_id,
            reply_id=current.reply_id,
            segment_id=current.segment_id,
            provider_generation=current.provider_generation,
            state=state,
            events=current.events + (event,),
            playback_offset_ms=playback_offset_ms,
            audio_chunk_event_id=chunk.event_id,
            audio_chunk_index=chunk.audio_chunk_index,
        )
        self._event_ids.add(event_id)
        self._snapshots[provider_request_id] = updated
        return updated

    def _get_lifecycle(self, provider_request_id: str):
        try:
            return self._lifecycles.get(provider_request_id)
        except (ProviderRequestLifecycleError, ContractError, TypeError) as exc:
            raise ProviderRequestPlaybackError(str(exc)) from exc

    @staticmethod
    def _assert_transition(
        snapshot: ProviderRequestPlayback,
        state: ProviderRequestPlaybackState,
        playback_offset_ms: int,
    ) -> None:
        ProviderRequestPlaybackRegistry._assert_step(
            snapshot.state, snapshot.playback_offset_ms, state, playback_offset_ms
        )

    @staticmethod
    def _assert_step(
        previous: ProviderRequestPlaybackState,
        previous_offset: int,
        state: ProviderRequestPlaybackState,
        playback_offset_ms: int,
    ) -> None:
        try:
            require_positive("playback_offset_ms", playback_offset_ms, allow_zero=True)
        except ContractError as exc:
            raise ProviderRequestPlaybackError(str(exc)) from exc
        if previous in _TERMINAL_STATES:
            raise ProviderRequestPlaybackError(f"terminal playback state {previous.value} rejects late facts")
        if state is ProviderRequestPlaybackState.COMMITTED_NOT_STARTED:
            if previous is not ProviderRequestPlaybackState.NOT_COMMITTED:
                raise ProviderRequestPlaybackError("only uncommitted segment can be committed_not_started")
            if playback_offset_ms != 0:
                raise ProviderRequestPlaybackError("commit offset must equal 0")
            return
        if state is ProviderRequestPlaybackState.PLAYBACK_STARTED:
            if previous is not ProviderRequestPlaybackState.COMMITTED_NOT_STARTED:
                raise ProviderRequestPlaybackError("only committed_not_started can be playback_started")
            if playback_offset_ms != 0:
                raise ProviderRequestPlaybackError("commit offset must equal 0")
            return
        if state is ProviderRequestPlaybackState.PARTIALLY_PLAYED:
            if previous not in (ProviderRequestPlaybackState.PLAYBACK_STARTED, ProviderRequestPlaybackState.PARTIALLY_PLAYED):
                raise ProviderRequestPlaybackError("only started playback can be partially_played")
            if playback_offset_ms <= previous_offset:
                raise ProviderRequestPlaybackError("playback offset must be strictly increasing")
            return
        if state is ProviderRequestPlaybackState.PLAYED:
            if previous not in (ProviderRequestPlaybackState.PLAYBACK_STARTED, ProviderRequestPlaybackState.PARTIALLY_PLAYED):
                raise ProviderRequestPlaybackError("only started playback can be played")
            if playback_offset_ms <= previous_offset:
                raise ProviderRequestPlaybackError("playback offset must be strictly increasing")
            return
        if state is ProviderRequestPlaybackState.ABORTED:
            if previous not in (
                ProviderRequestPlaybackState.COMMITTED_NOT_STARTED,
                ProviderRequestPlaybackState.PLAYBACK_STARTED,
                ProviderRequestPlaybackState.PARTIALLY_PLAYED,
            ):
                raise ProviderRequestPlaybackError("current playback state cannot be aborted")
            if playback_offset_ms < previous_offset:
                raise ProviderRequestPlaybackError("aborted playback offset must not decrease")
            return
        raise ProviderRequestPlaybackError("unknown playback fact state")


__all__ = [
    "ProviderRequestPlayback",
    "ProviderRequestPlaybackError",
    "ProviderRequestPlaybackEvent",
    "ProviderRequestPlaybackRegistry",
    "ProviderRequestPlaybackState",
]