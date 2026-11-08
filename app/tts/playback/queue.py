"""P7 本地播放队列预算和顺序门控；不保存音频、不操作设备。"""
from __future__ import annotations

from dataclasses import dataclass

from app.contracts.base import ContractError, require_positive
from app.tts.lifecycle import ProviderRequestLifecycleError, ProviderRequestLifecycleRegistry, ProviderRequestLifecycleState
from app.tts.playback import ProviderRequestPlaybackRegistry, ProviderRequestPlaybackState


class PlaybackQueueError(ValueError):
    """队列输入、绑定、顺序或终态不满足本地契约。"""


class PlaybackQueueBackpressure(PlaybackQueueError):
    """句数或显式报告的待播时长超过当前队列预算。"""


@dataclass(frozen=True, slots=True)
class PlaybackQueueEntry:
    """入队元数据；duration 是调用方报告的预算值，并非实测 PCM 时长。"""

    provider_request_id: str
    reply_id: str
    segment_id: str
    request_sequence: int
    provider_generation: int
    audio_chunk_event_id: str
    audio_duration_ms: int


class PlaybackQueue:
    """单回复 FIFO 队列；begin/finish 是控制事实，不自动生成播放事实。

    同步进程内对象。cancel 只清队列并阻止旧回复继续入队；调用者仍须
    单独停止设备、取消 Provider 并报告 playback.abort，不能把清队列等同停止。
    """

    def __init__(
        self,
        lifecycles: ProviderRequestLifecycleRegistry,
        playback: ProviderRequestPlaybackRegistry,
        *,
        max_segments: int = 3,
        max_duration_ms: int = 8000,
    ) -> None:
        if not isinstance(lifecycles, ProviderRequestLifecycleRegistry):
            raise TypeError("lifecycles 必须是 ProviderRequestLifecycleRegistry")
        if not isinstance(playback, ProviderRequestPlaybackRegistry) or playback._lifecycles is not lifecycles:
            raise TypeError("playback 必须绑定相同的 lifecycle registry")
        try:
            require_positive("max_segments", max_segments)
            require_positive("max_duration_ms", max_duration_ms)
        except ContractError as exc:
            raise PlaybackQueueError(str(exc)) from exc
        self._lifecycles = lifecycles
        self._playback = playback
        self.max_segments = max_segments
        self.max_duration_ms = max_duration_ms
        self._pending: list[PlaybackQueueEntry] = []
        self._active: PlaybackQueueEntry | None = None
        self._reply_id: str | None = None
        self._closed_replies: set[str] = set()
        self._seen_requests: set[str] = set()
        self._last_sequence: dict[str, int] = {}

    @property
    def pending(self) -> tuple[PlaybackQueueEntry, ...]:
        return tuple(self._pending)

    @property
    def active(self) -> PlaybackQueueEntry | None:
        return self._active

    @property
    def reserved_duration_ms(self) -> int:
        return sum(entry.audio_duration_ms for entry in self._pending)

    def _lifecycle(self, request_id: str):
        try:
            return self._lifecycles.get(request_id)
        except (ProviderRequestLifecycleError, ContractError, TypeError) as exc:
            raise PlaybackQueueError(str(exc)) from exc

    def _check_generation(self, lifecycle) -> None:
        request = lifecycle.request
        active = self._lifecycles.active_generation(request.provider_name)
        if active is not None and active != request.provider_generation:
            raise PlaybackQueueError("旧 provider_generation 不得进入当前播放队列")

    def enqueue(
        self, provider_request_id: str, *, audio_chunk_event_id: str, audio_duration_ms: int,
    ) -> PlaybackQueueEntry:
        try:
            require_positive("audio_duration_ms", audio_duration_ms)
        except ContractError as exc:
            raise PlaybackQueueError(str(exc)) from exc
        lifecycle = self._lifecycle(provider_request_id)
        request = lifecycle.request
        self._check_generation(lifecycle)
        if request.reply_id in self._closed_replies:
            raise PlaybackQueueError("已取消的旧回复不得重新入队")
        if lifecycle.state in (ProviderRequestLifecycleState.FAILED, ProviderRequestLifecycleState.CANCELLED):
            raise PlaybackQueueError("失败或取消的 Provider request 不得入队")
        if not lifecycle.audio_produced:
            raise PlaybackQueueError("必须在 audio_produced 后才能进入待播队列")
        if provider_request_id in self._seen_requests:
            raise PlaybackQueueError("同一 Provider request 不得重复入队")
        if self._pending and request.reply_id != self._reply_id:
            raise PlaybackQueueError("另一回复不能越过当前待播回复")
        expected = self._last_sequence.get(request.reply_id, 0) + 1
        if request.request_sequence != expected:
            raise PlaybackQueueError(f"request_sequence 必须按原句顺序连续入队，期望 {expected}")
        if not isinstance(audio_chunk_event_id, str) or not any(
            chunk.event_id == audio_chunk_event_id for chunk in lifecycle.audio_chunks
        ):
            raise PlaybackQueueError("audio_chunk_event_id 未绑定当前 Provider request")
        if len(self._pending) >= self.max_segments or self.reserved_duration_ms + audio_duration_ms > self.max_duration_ms:
            raise PlaybackQueueBackpressure("待播句数或报告时长达到预算，暂停提交新句")
        entry = PlaybackQueueEntry(
            provider_request_id=request.provider_request_id,
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            request_sequence=request.request_sequence,
            provider_generation=request.provider_generation,
            audio_chunk_event_id=audio_chunk_event_id,
            audio_duration_ms=audio_duration_ms,
        )
        self._pending.append(entry)
        self._reply_id = request.reply_id
        self._seen_requests.add(request.provider_request_id)
        self._last_sequence[request.reply_id] = request.request_sequence
        return entry

    def begin(self) -> PlaybackQueueEntry:
        if not self._pending or self._active is not None:
            raise PlaybackQueueError("队列为空或当前句尚未结束")
        entry = self._pending[0]
        lifecycle = self._lifecycle(entry.provider_request_id)
        self._check_generation(lifecycle)
        if lifecycle.state in (ProviderRequestLifecycleState.FAILED, ProviderRequestLifecycleState.CANCELLED):
            raise PlaybackQueueError("已失败或取消的句子不得开始待播")
        if self._playback.get(entry.provider_request_id).terminal:
            raise PlaybackQueueError("播放事实已终态，不得重新开始")
        self._active = entry
        return entry

    def finish(self, provider_request_id: str) -> PlaybackQueueEntry:
        if self._active is None or self._active.provider_request_id != provider_request_id:
            raise PlaybackQueueError("只能结束当前句")
        fact = self._playback.get(provider_request_id)
        if fact.state not in (ProviderRequestPlaybackState.PLAYED, ProviderRequestPlaybackState.ABORTED):
            raise PlaybackQueueError("只有显式终态播放事实才能结束当前句")
        entry = self._active
        self._pending.pop(0)
        self._active = None
        if fact.state is ProviderRequestPlaybackState.ABORTED:
            self._closed_replies.add(entry.reply_id)
            self._pending.clear()
        if not self._pending:
            self._reply_id = None
        return entry

    def cancel(self, reply_id: str) -> tuple[PlaybackQueueEntry, ...]:
        if not isinstance(reply_id, str) or not reply_id or reply_id != self._reply_id:
            raise PlaybackQueueError("只能取消当前队列所属回复")
        dropped = tuple(self._pending)
        self._closed_replies.add(reply_id)
        self._pending.clear()
        self._active = None
        self._reply_id = None
        return dropped


__all__ = ["PlaybackQueue", "PlaybackQueueBackpressure", "PlaybackQueueEntry", "PlaybackQueueError"]