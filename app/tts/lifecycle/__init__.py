"""P7 批次 7：Provider request 音频块的本地契约与顺序门控。

本模块只记录已经准备好的 Provider request 的本地生命周期事件，以及由上层
显式报告的不可变音频块元数据。音频块事实不携带 PCM/编码 payload，也不表示
音频已经进入后续管道、开始播放、播放完成或用户已听到；本模块不调用 Provider、
不读取网络、不调用远端 cancel，不连接播放设备。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_identifier, require_non_empty, require_positive
from app.tts.capabilities import AudioFormat, CapabilityCheck, CapabilityDecision
from app.tts.requests import ProviderRequest, ProviderRequestError, ProviderRequestRegistry


class ProviderRequestLifecycleError(ValueError):
    """Provider request 生命周期事件违反本地门控约束。"""


class ProviderRequestLifecycleState(str, Enum):
    """生命周期快照状态；不表示播放或用户听见。"""

    PREPARED = "prepared"
    ACCEPTED = "accepted"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderRequestLifecycleEventKind(str, Enum):
    """可记录的本地生命周期事实。"""

    ACCEPTED = "accepted"
    SYNTHESIS_STARTED = "synthesis_started"
    AUDIO_PRODUCED = "audio_produced"
    AUDIO_ENQUEUED = "audio_enqueued"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderRequestLifecycleReasonCode(str, Enum):
    """失败/取消的稳定本地原因码；不等同于 Provider 远端错误码。"""

    LOCAL_GATE_REJECTED = "local_gate_rejected"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    INTERNAL_ERROR = "internal_error"
    USER_CANCELLED = "user_cancelled"
    SUPERSEDED = "superseded"
    SESSION_CLOSED = "session_closed"
    SHUTDOWN = "shutdown"


_FAILURE_REASON_CODES = frozenset(
    {
        ProviderRequestLifecycleReasonCode.LOCAL_GATE_REJECTED,
        ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT,
        ProviderRequestLifecycleReasonCode.INTERNAL_ERROR,
    }
)
_CANCEL_REASON_CODES = frozenset(
    {
        ProviderRequestLifecycleReasonCode.USER_CANCELLED,
        ProviderRequestLifecycleReasonCode.SUPERSEDED,
        ProviderRequestLifecycleReasonCode.SESSION_CLOSED,
        ProviderRequestLifecycleReasonCode.SHUTDOWN,
    }
)
_TERMINAL_STATES = frozenset(
    {
        ProviderRequestLifecycleState.COMPLETED,
        ProviderRequestLifecycleState.FAILED,
        ProviderRequestLifecycleState.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class ProviderRequestLifecycleEvent:
    """绑定到单一 Provider request 的不可变本地生命周期事件。"""

    event_id: str
    event_sequence: int
    provider_request_id: str
    reply_id: str
    segment_id: str
    provider_name: str
    provider_generation: int
    voice_profile_id: str
    voice_profile_version: int
    audio_format: AudioFormat
    capability_check: CapabilityCheck
    kind: ProviderRequestLifecycleEventKind
    reason: str | None = None
    reason_code: ProviderRequestLifecycleReasonCode | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("provider_request_id", self.provider_request_id),
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
            ("provider_name", self.provider_name),
            ("voice_profile_id", self.voice_profile_id),
        ):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise ProviderRequestLifecycleError(str(exc)) from exc
        try:
            require_positive("event_sequence", self.event_sequence)
            require_positive("provider_generation", self.provider_generation)
            require_positive("voice_profile_version", self.voice_profile_version)
        except ContractError as exc:
            raise ProviderRequestLifecycleError(str(exc)) from exc
        if not isinstance(self.audio_format, AudioFormat):
            raise ProviderRequestLifecycleError("audio_format 必须是 AudioFormat")
        if not isinstance(self.capability_check, CapabilityCheck):
            raise ProviderRequestLifecycleError("capability_check 必须是 CapabilityCheck")
        if self.capability_check.decision is not CapabilityDecision.COMPATIBLE:
            raise ProviderRequestLifecycleError("生命周期事件只能绑定兼容的 capability 判定")
        if self.provider_name != self.capability_check.provider_name:
            raise ProviderRequestLifecycleError("provider_name 必须与 capability_check 一致")
        if not isinstance(self.kind, ProviderRequestLifecycleEventKind):
            raise ProviderRequestLifecycleError("kind 必须是 ProviderRequestLifecycleEventKind")
        if self.reason_code is not None and not isinstance(
            self.reason_code, ProviderRequestLifecycleReasonCode
        ):
            raise ProviderRequestLifecycleError("reason_code 必须是 ProviderRequestLifecycleReasonCode")
        if self.kind is ProviderRequestLifecycleEventKind.FAILED:
            try:
                require_non_empty("reason", self.reason or "", maximum=512)
            except ContractError as exc:
                raise ProviderRequestLifecycleError(str(exc)) from exc
            code = self.reason_code or ProviderRequestLifecycleReasonCode.LOCAL_GATE_REJECTED
            if code not in _FAILURE_REASON_CODES:
                raise ProviderRequestLifecycleError("failed 事件只能使用失败原因码")
            object.__setattr__(self, "reason_code", code)
        elif self.kind is ProviderRequestLifecycleEventKind.CANCELLED:
            try:
                require_non_empty("reason", self.reason or "", maximum=512)
            except ContractError as exc:
                raise ProviderRequestLifecycleError(str(exc)) from exc
            code = self.reason_code or ProviderRequestLifecycleReasonCode.USER_CANCELLED
            if code not in _CANCEL_REASON_CODES:
                raise ProviderRequestLifecycleError("cancelled 事件只能使用取消原因码")
            object.__setattr__(self, "reason_code", code)
        else:
            if self.reason is not None or self.reason_code is not None:
                raise ProviderRequestLifecycleError("非失败/取消事件不得携带 reason 或 reason_code")


@dataclass(frozen=True, slots=True)
class ProviderRequestAudioChunk:
    """绑定到单一 Provider request 的不可变音频块元数据事实。

    该契约只描述 Provider 产生的块身份、块序号与本地接收顺序，不携带实际音频
    payload，也不表示块已经进入播放管道或已被用户听见。
    """

    event_id: str
    audio_chunk_index: int
    receive_sequence: int
    provider_request_id: str
    reply_id: str
    segment_id: str
    provider_name: str
    provider_generation: int
    voice_profile_id: str
    voice_profile_version: int
    audio_format: AudioFormat
    capability_check: CapabilityCheck

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("provider_request_id", self.provider_request_id),
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
            ("provider_name", self.provider_name),
            ("voice_profile_id", self.voice_profile_id),
        ):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise ProviderRequestLifecycleError(str(exc)) from exc
        try:
            require_positive("audio_chunk_index", self.audio_chunk_index)
            require_positive("receive_sequence", self.receive_sequence)
            require_positive("provider_generation", self.provider_generation)
            require_positive("voice_profile_version", self.voice_profile_version)
        except ContractError as exc:
            raise ProviderRequestLifecycleError(str(exc)) from exc
        if not isinstance(self.audio_format, AudioFormat):
            raise ProviderRequestLifecycleError("audio_format 必须是 AudioFormat")
        if not isinstance(self.capability_check, CapabilityCheck):
            raise ProviderRequestLifecycleError("capability_check 必须是 CapabilityCheck")
        if self.capability_check.decision is not CapabilityDecision.COMPATIBLE:
            raise ProviderRequestLifecycleError("音频块只能绑定兼容的 capability 判定")
        if self.provider_name != self.capability_check.provider_name:
            raise ProviderRequestLifecycleError("provider_name 必须与 capability_check 一致")


@dataclass(frozen=True, slots=True)
class ProviderRequestLifecycle:
    """Provider request 的不可变生命周期快照和事件历史。"""

    request: ProviderRequest
    state: ProviderRequestLifecycleState = ProviderRequestLifecycleState.PREPARED
    events: tuple[ProviderRequestLifecycleEvent, ...] = ()
    audio_produced: bool = False
    audio_enqueued: bool = False
    audio_chunks: tuple[ProviderRequestAudioChunk, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request, ProviderRequest):
            raise ProviderRequestLifecycleError("request 必须是 ProviderRequest")
        if not isinstance(self.state, ProviderRequestLifecycleState):
            raise ProviderRequestLifecycleError("state 必须是 ProviderRequestLifecycleState")
        if not isinstance(self.events, tuple):
            raise ProviderRequestLifecycleError("events 必须是 tuple")
        if not isinstance(self.audio_produced, bool) or not isinstance(self.audio_enqueued, bool):
            raise ProviderRequestLifecycleError("audio_produced/audio_enqueued 必须是 bool")
        if not isinstance(self.audio_chunks, tuple):
            raise ProviderRequestLifecycleError("audio_chunks 必须是 tuple")
        seen_event_ids: set[str] = set()
        for expected, event in enumerate(self.events, start=1):
            if not isinstance(event, ProviderRequestLifecycleEvent):
                raise ProviderRequestLifecycleError("events 只能包含 ProviderRequestLifecycleEvent")
            if event.event_sequence != expected:
                raise ProviderRequestLifecycleError("events 的 event_sequence 必须从 1 开始连续")
            if (
                event.provider_request_id != self.request.provider_request_id
                or event.reply_id != self.request.reply_id
                or event.segment_id != self.request.segment_id
                or event.provider_name != self.request.provider_name
                or event.provider_generation != self.request.provider_generation
                or event.voice_profile_id != self.request.voice_profile_id
                or event.voice_profile_version != self.request.voice_profile_version
                or event.audio_format != self.request.audio_format
                or event.capability_check != self.request.capability_check
            ):
                raise ProviderRequestLifecycleError("lifecycle event identity 必须与 Provider request 完整绑定")
            if event.event_id in seen_event_ids:
                raise ProviderRequestLifecycleError("event_id 不得在事件历史中重复")
            seen_event_ids.add(event.event_id)
        for expected, chunk in enumerate(self.audio_chunks, start=1):
            if not isinstance(chunk, ProviderRequestAudioChunk):
                raise ProviderRequestLifecycleError("audio_chunks 只能包含 ProviderRequestAudioChunk")
            if chunk.provider_request_id != self.request.provider_request_id:
                raise ProviderRequestLifecycleError("audio chunk 必须绑定当前 provider request")
            if chunk.event_id in seen_event_ids:
                raise ProviderRequestLifecycleError("event_id 不得在事件和音频块之间重复")
            seen_event_ids.add(chunk.event_id)
            if chunk.audio_chunk_index != expected or chunk.receive_sequence != expected:
                raise ProviderRequestLifecycleError("audio_chunks 必须从 1 开始按块序号和接收顺序连续")
            if (
                chunk.reply_id != self.request.reply_id
                or chunk.segment_id != self.request.segment_id
                or chunk.provider_name != self.request.provider_name
                or chunk.provider_generation != self.request.provider_generation
                or chunk.voice_profile_id != self.request.voice_profile_id
                or chunk.voice_profile_version != self.request.voice_profile_version
                or chunk.audio_format != self.request.audio_format
                or chunk.capability_check != self.request.capability_check
            ):
                raise ProviderRequestLifecycleError("audio chunk identity 必须与 Provider request 完整绑定")
        if self.audio_produced and not self.audio_chunks:
            raise ProviderRequestLifecycleError("audio_produced 必须至少对应一个音频块")
        if self.state is ProviderRequestLifecycleState.COMPLETED:
            if not self.audio_chunks:
                raise ProviderRequestLifecycleError("completed 必须至少对应一个音频块")
            if not self.audio_produced:
                raise ProviderRequestLifecycleError("completed 必须在 audio_produced 之后")
        if self.audio_enqueued and not self.audio_produced:
            raise ProviderRequestLifecycleError("audio_enqueued 必须建立在 audio_produced 之上")
        has_audio_produced_event = any(
            event.kind is ProviderRequestLifecycleEventKind.AUDIO_PRODUCED
            for event in self.events
        )
        if self.audio_produced != has_audio_produced_event:
            raise ProviderRequestLifecycleError("audio_produced 事实必须与事件历史一致")
        has_audio_enqueued_event = any(
            event.kind is ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED
            for event in self.events
        )
        if self.audio_enqueued != has_audio_enqueued_event:
            raise ProviderRequestLifecycleError("audio_enqueued 事实必须与事件历史一致")

        terminal_event_kinds = {
            ProviderRequestLifecycleEventKind.COMPLETED,
            ProviderRequestLifecycleEventKind.FAILED,
            ProviderRequestLifecycleEventKind.CANCELLED,
        }
        if self.state not in _TERMINAL_STATES and any(
            event.kind in terminal_event_kinds for event in self.events
        ):
            raise ProviderRequestLifecycleError("终态事件只能出现在历史末尾且 state 必须为终态")
        if self.state in _TERMINAL_STATES:
            if not self.events:
                raise ProviderRequestLifecycleError("终态快照必须包含终态事件")
            terminal_event = self.events[-1]
            expected_terminal_kind = {
                ProviderRequestLifecycleState.COMPLETED: ProviderRequestLifecycleEventKind.COMPLETED,
                ProviderRequestLifecycleState.FAILED: ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleState.CANCELLED: ProviderRequestLifecycleEventKind.CANCELLED,
            }[self.state]
            if terminal_event.kind is not expected_terminal_kind:
                raise ProviderRequestLifecycleError("终态快照的末事件必须与 state 一致")
            if any(event.kind in {
                ProviderRequestLifecycleEventKind.COMPLETED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleEventKind.CANCELLED,
            } for event in self.events[:-1]):
                raise ProviderRequestLifecycleError("终态事件只能出现在历史末尾")

        has_synthesis_started_event = any(
            event.kind is ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED
            for event in self.events
        )
        if self.audio_chunks and not has_synthesis_started_event:
            raise ProviderRequestLifecycleError(
                "audio_chunks 只能出现在 synthesis_started 之后"
            )

        if self.audio_chunks and self.state in (
            ProviderRequestLifecycleState.PREPARED,
            ProviderRequestLifecycleState.ACCEPTED,
        ):
            raise ProviderRequestLifecycleError(
                "audio_chunks 只能出现在 synthesis_started 之后"
            )
        replay_state = ProviderRequestLifecycleState.PREPARED
        replay_audio_produced = False
        replay_audio_enqueued = False
        for event in self.events:
            replay_state, replay_audio_produced, replay_audio_enqueued = self._replay_event(
                state=replay_state,
                audio_produced=replay_audio_produced,
                audio_enqueued=replay_audio_enqueued,
                has_audio_chunks=bool(self.audio_chunks),
                kind=event.kind,
            )
        if (
            replay_state is not self.state
            or replay_audio_produced != self.audio_produced
            or replay_audio_enqueued != self.audio_enqueued
        ):
            raise ProviderRequestLifecycleError("state 与音频事实必须与事件历史回放结果一致")
    @staticmethod
    def _replay_event(
        *,
        state: ProviderRequestLifecycleState,
        audio_produced: bool,
        audio_enqueued: bool,
        has_audio_chunks: bool,
        kind: ProviderRequestLifecycleEventKind,
    ) -> tuple[ProviderRequestLifecycleState, bool, bool]:
        if state in _TERMINAL_STATES:
            raise ProviderRequestLifecycleError("终态事件只能出现在历史末尾")
        if kind is ProviderRequestLifecycleEventKind.ACCEPTED:
            if state is not ProviderRequestLifecycleState.PREPARED:
                raise ProviderRequestLifecycleError("事件历史中的 accepted 顺序非法")
            return ProviderRequestLifecycleState.ACCEPTED, audio_produced, audio_enqueued
        if kind is ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED:
            if state is not ProviderRequestLifecycleState.ACCEPTED:
                raise ProviderRequestLifecycleError("事件历史中的 synthesis_started 顺序非法")
            return ProviderRequestLifecycleState.SYNTHESIZING, audio_produced, audio_enqueued
        if kind is ProviderRequestLifecycleEventKind.AUDIO_PRODUCED:
            if state is not ProviderRequestLifecycleState.SYNTHESIZING:
                raise ProviderRequestLifecycleError("事件历史中的 audio_produced 顺序非法")
            if not has_audio_chunks:
                raise ProviderRequestLifecycleError("audio_produced 必须至少对应一个音频块")
            if audio_produced:
                raise ProviderRequestLifecycleError("事件历史中的 audio_produced 不得重复")
            return state, True, audio_enqueued
        if kind is ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED:
            if state is not ProviderRequestLifecycleState.SYNTHESIZING:
                raise ProviderRequestLifecycleError("事件历史中的 audio_enqueued 顺序非法")
            if not audio_produced:
                raise ProviderRequestLifecycleError("事件历史中的 audio_enqueued 必须在 audio_produced 之后")
            if audio_enqueued:
                raise ProviderRequestLifecycleError("事件历史中的 audio_enqueued 不得重复")
            return state, audio_produced, True
        if kind is ProviderRequestLifecycleEventKind.COMPLETED:
            if state is not ProviderRequestLifecycleState.SYNTHESIZING:
                raise ProviderRequestLifecycleError("事件历史中的 completed 顺序非法")
            if not has_audio_chunks:
                raise ProviderRequestLifecycleError("completed 必须至少对应一个音频块")
            if not audio_produced:
                raise ProviderRequestLifecycleError("事件历史中的 completed 必须在 audio_produced 之后")
            return ProviderRequestLifecycleState.COMPLETED, audio_produced, audio_enqueued
        if kind is ProviderRequestLifecycleEventKind.FAILED:
            if state not in (
                ProviderRequestLifecycleState.PREPARED,
                ProviderRequestLifecycleState.ACCEPTED,
                ProviderRequestLifecycleState.SYNTHESIZING,
            ):
                raise ProviderRequestLifecycleError("事件历史中的 failed 顺序非法")
            return ProviderRequestLifecycleState.FAILED, audio_produced, audio_enqueued
        if kind is ProviderRequestLifecycleEventKind.CANCELLED:
            if state not in (
                ProviderRequestLifecycleState.PREPARED,
                ProviderRequestLifecycleState.ACCEPTED,
                ProviderRequestLifecycleState.SYNTHESIZING,
            ):
                raise ProviderRequestLifecycleError("事件历史中的 cancelled 顺序非法")
            return ProviderRequestLifecycleState.CANCELLED, audio_produced, audio_enqueued
        raise ProviderRequestLifecycleError("未知生命周期事件")
    @property
    def terminal(self) -> bool:

        return self.state in _TERMINAL_STATES

    @property
    def terminal_event(self) -> ProviderRequestLifecycleEvent | None:
        """返回首次且唯一锁定终态的事件；终态之后的迟到事件不会被接纳。"""

        if not self.terminal:
            return None
        return self.events[-1]

    @property
    def terminal_reason_code(self) -> ProviderRequestLifecycleReasonCode | None:
        event = self.terminal_event
        return event.reason_code if event is not None else None

    @property
    def terminal_reason(self) -> str | None:
        """返回终态事件的本地原因文本；未进入终态或 completed 时为 None。"""

        event = self.terminal_event
        return event.reason if event is not None else None


class ProviderRequestLifecycleRegistry:
    """为已准备的 request 提供原子、不可变事件门控和 generation fence。"""

    def __init__(self, requests: ProviderRequestRegistry) -> None:
        if not isinstance(requests, ProviderRequestRegistry):
            raise TypeError("requests 必须是 ProviderRequestRegistry")
        self._requests = requests
        self._snapshots: dict[str, ProviderRequestLifecycle] = {}
        self._event_ids: set[str] = set()
        self._active_generations: dict[str, int] = {}

    def get(self, provider_request_id: str) -> ProviderRequestLifecycle:
        request = self._get_request(provider_request_id)
        snapshot = self._snapshot_for(request)
        if request.provider_request_id not in self._snapshots:
            self._snapshots[request.provider_request_id] = snapshot
        return snapshot

    def _snapshot_for(self, request: ProviderRequest) -> ProviderRequestLifecycle:
        snapshot = self._snapshots.get(request.provider_request_id)
        if snapshot is not None:
            return snapshot
        return ProviderRequestLifecycle(request=request)

    def active_generation(self, provider_name: str) -> int | None:
        try:
            require_identifier("provider_name", provider_name)
        except ContractError as exc:
            raise ProviderRequestLifecycleError(str(exc)) from exc
        return self._active_generations.get(provider_name)

    def activate_generation(self, provider_name: str, provider_generation: int) -> int:
        """推进本地 generation fence；旧代事件此后只能被拒绝。"""

        try:
            require_identifier("provider_name", provider_name)
            require_positive("provider_generation", provider_generation)
        except ContractError as exc:
            raise ProviderRequestLifecycleError(str(exc)) from exc
        current = self._active_generations.get(provider_name)
        if current is not None and provider_generation < current:
            raise ProviderRequestLifecycleError(
                f"provider_generation 不得回退: 当前 {current}，实际 {provider_generation}"
            )
        self._active_generations[provider_name] = provider_generation
        return provider_generation

    def advance_generation(self, provider_name: str, provider_generation: int) -> int:
        """activate_generation 的语义别名，明确表示 generation 只能前进。"""

        return self.activate_generation(provider_name, provider_generation)

    def record(self, event: ProviderRequestLifecycleEvent) -> ProviderRequestLifecycle:
        if not isinstance(event, ProviderRequestLifecycleEvent):
            raise TypeError("event 必须是 ProviderRequestLifecycleEvent")
        if event.event_id in self._event_ids:
            raise ProviderRequestLifecycleError("event_id 不得重复")
        request = self._get_request(event.provider_request_id)
        snapshot = self._snapshot_for(request)
        self._assert_bound_to_request(event, request)
        self._assert_generation_current(event)
        expected_sequence = len(snapshot.events) + 1
        if event.event_sequence != expected_sequence:
            raise ProviderRequestLifecycleError(
                f"非法生命周期事件序列: request {request.provider_request_id} "
                f"期望 {expected_sequence}，实际 {event.event_sequence}"
            )
        next_state = self._next_state(snapshot, event.kind)
        next_snapshot = ProviderRequestLifecycle(
            request=request,
            state=next_state,
            events=(*snapshot.events, event),
            audio_produced=snapshot.audio_produced
            or event.kind is ProviderRequestLifecycleEventKind.AUDIO_PRODUCED,
            audio_enqueued=snapshot.audio_enqueued
            or event.kind is ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED,
            audio_chunks=snapshot.audio_chunks,
        )
        # 只有所有校验和快照构造成功后才写入新快照；generation fence 由显式 activate_generation 管理。
        self._snapshots[request.provider_request_id] = next_snapshot
        self._event_ids.add(event.event_id)
        return next_snapshot

    def record_audio_chunk(self, chunk: ProviderRequestAudioChunk) -> ProviderRequestLifecycle:
        """记录一个 Provider 产生的本地音频块元数据事实。

        只有 synthesizing request 可以接收块；块序号和本地接收顺序都必须从 1
        开始连续递增。所有校验在快照和全局 event ID 写入前完成，失败不会污染
        request、历史或 event ID 集合。
        """

        if not isinstance(chunk, ProviderRequestAudioChunk):
            raise TypeError("chunk 必须是 ProviderRequestAudioChunk")
        if chunk.event_id in self._event_ids:
            raise ProviderRequestLifecycleError("event_id 不得重复")
        request = self._get_request(chunk.provider_request_id)
        snapshot = self._snapshot_for(request)
        self._assert_audio_chunk_bound_to_request(chunk, request)
        self._assert_generation_current(chunk)
        if snapshot.terminal:
            raise ProviderRequestLifecycleError(
                f"终态 {snapshot.state.value} 已锁定，迟到 audio chunk 被拒绝"
            )
        if snapshot.state is not ProviderRequestLifecycleState.SYNTHESIZING:
            raise ProviderRequestLifecycleError("只有 synthesizing request 可以接收 audio chunk")
        if snapshot.audio_produced:
            raise ProviderRequestLifecycleError("audio_produced 已锁定，不能追加音频块")
        expected = len(snapshot.audio_chunks) + 1
        existing_indices = {item.audio_chunk_index for item in snapshot.audio_chunks}
        if chunk.audio_chunk_index in existing_indices:
            raise ProviderRequestLifecycleError("audio_chunk_index 不得重复")
        if chunk.audio_chunk_index != expected:
            raise ProviderRequestLifecycleError(
                f"非法 audio_chunk_index: 期望 {expected}，实际 {chunk.audio_chunk_index}"
            )
        existing_receive_sequences = {item.receive_sequence for item in snapshot.audio_chunks}
        if chunk.receive_sequence in existing_receive_sequences:
            raise ProviderRequestLifecycleError("receive_sequence 不得重复")
        if chunk.receive_sequence != expected:
            raise ProviderRequestLifecycleError(
                f"非法音频块接收顺序: 期望 {expected}，实际 {chunk.receive_sequence}"
            )
        next_snapshot = ProviderRequestLifecycle(
            request=request,
            state=snapshot.state,
            events=snapshot.events,
            audio_produced=snapshot.audio_produced,
            audio_enqueued=snapshot.audio_enqueued,
            audio_chunks=(*snapshot.audio_chunks, chunk),
        )
        self._snapshots[request.provider_request_id] = next_snapshot
        self._event_ids.add(chunk.event_id)
        return next_snapshot

    def accept(self, provider_request_id: str, *, event_id: str) -> ProviderRequestLifecycle:
        return self._record_simple(provider_request_id, event_id, ProviderRequestLifecycleEventKind.ACCEPTED)

    def synthesis_started(self, provider_request_id: str, *, event_id: str) -> ProviderRequestLifecycle:
        return self._record_simple(provider_request_id, event_id, ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED)

    def audio_produced(self, provider_request_id: str, *, event_id: str) -> ProviderRequestLifecycle:
        return self._record_simple(provider_request_id, event_id, ProviderRequestLifecycleEventKind.AUDIO_PRODUCED)

    def audio_enqueued(self, provider_request_id: str, *, event_id: str) -> ProviderRequestLifecycle:
        return self._record_simple(provider_request_id, event_id, ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED)

    def complete(self, provider_request_id: str, *, event_id: str) -> ProviderRequestLifecycle:
        return self._record_simple(provider_request_id, event_id, ProviderRequestLifecycleEventKind.COMPLETED)

    def fail(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        reason: str,
        reason_code: ProviderRequestLifecycleReasonCode = ProviderRequestLifecycleReasonCode.LOCAL_GATE_REJECTED,
    ) -> ProviderRequestLifecycle:
        return self._record_simple(
            provider_request_id,
            event_id,
            ProviderRequestLifecycleEventKind.FAILED,
            reason=reason,
            reason_code=reason_code,
        )

    def cancel(
        self,
        provider_request_id: str,
        *,
        event_id: str,
        reason: str,
        reason_code: ProviderRequestLifecycleReasonCode = ProviderRequestLifecycleReasonCode.USER_CANCELLED,
    ) -> ProviderRequestLifecycle:
        return self._record_simple(
            provider_request_id,
            event_id,
            ProviderRequestLifecycleEventKind.CANCELLED,
            reason=reason,
            reason_code=reason_code,
        )

    def events_for(self, provider_request_id: str) -> tuple[ProviderRequestLifecycleEvent, ...]:
        return self.get(provider_request_id).events

    def audio_chunks_for(self, provider_request_id: str) -> tuple[ProviderRequestAudioChunk, ...]:
        return self.get(provider_request_id).audio_chunks

    def _record_simple(
        self,
        provider_request_id: str,
        event_id: str,
        kind: ProviderRequestLifecycleEventKind,
        *,
        reason: str | None = None,
        reason_code: ProviderRequestLifecycleReasonCode | None = None,
    ) -> ProviderRequestLifecycle:
        request = self._get_request(provider_request_id)
        snapshot = self._snapshot_for(request)
        event = ProviderRequestLifecycleEvent(
            event_id=event_id,
            event_sequence=len(snapshot.events) + 1,
            provider_request_id=request.provider_request_id,
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_name=request.provider_name,
            provider_generation=request.provider_generation,
            voice_profile_id=request.voice_profile_id,
            voice_profile_version=request.voice_profile_version,
            audio_format=request.audio_format,
            capability_check=request.capability_check,
            kind=kind,
            reason=reason,
            reason_code=reason_code,
        )
        return self.record(event)

    def _get_request(self, provider_request_id: str) -> ProviderRequest:
        try:
            return self._requests.get(provider_request_id)
        except (ProviderRequestError, ContractError, TypeError) as exc:
            raise ProviderRequestLifecycleError(str(exc)) from exc

    def _assert_generation_current(self, event: ProviderRequestLifecycleEvent) -> None:
        current = self._active_generations.get(event.provider_name)
        if current is None:
            return
        if event.provider_generation < current:
            raise ProviderRequestLifecycleError(
                f"迟到 provider_generation: 当前 {current}，实际 {event.provider_generation}"
            )
        if event.provider_generation > current:
            raise ProviderRequestLifecycleError(
                f"provider_generation 尚未激活: 当前 {current}，实际 {event.provider_generation}"
            )

    @staticmethod
    def _assert_audio_chunk_bound_to_request(
        chunk: ProviderRequestAudioChunk,
        request: ProviderRequest,
    ) -> None:
        bound = (
            ("reply_id", chunk.reply_id, request.reply_id),
            ("segment_id", chunk.segment_id, request.segment_id),
            ("provider_name", chunk.provider_name, request.provider_name),
            ("provider_generation", chunk.provider_generation, request.provider_generation),
            ("voice_profile_id", chunk.voice_profile_id, request.voice_profile_id),
            ("voice_profile_version", chunk.voice_profile_version, request.voice_profile_version),
            ("audio_format", chunk.audio_format, request.audio_format),
            ("capability_check", chunk.capability_check, request.capability_check),
        )
        for name, actual, expected in bound:
            if actual != expected:
                raise ProviderRequestLifecycleError(f"音频块 {name} 与 Provider request 不一致")

    @staticmethod
    def _assert_bound_to_request(
        event: ProviderRequestLifecycleEvent,
        request: ProviderRequest,
    ) -> None:
        bound = (
            ("reply_id", event.reply_id, request.reply_id),
            ("segment_id", event.segment_id, request.segment_id),
            ("provider_name", event.provider_name, request.provider_name),
            ("provider_generation", event.provider_generation, request.provider_generation),
            ("voice_profile_id", event.voice_profile_id, request.voice_profile_id),
            ("voice_profile_version", event.voice_profile_version, request.voice_profile_version),
            ("audio_format", event.audio_format, request.audio_format),
            ("capability_check", event.capability_check, request.capability_check),
        )
        for name, actual, expected in bound:
            if actual != expected:
                raise ProviderRequestLifecycleError(f"生命周期事件 {name} 与 Provider request 不一致")

    @staticmethod
    def _next_state(
        snapshot: ProviderRequestLifecycle,
        kind: ProviderRequestLifecycleEventKind,
    ) -> ProviderRequestLifecycleState:
        state = snapshot.state
        if state in _TERMINAL_STATES:
            raise ProviderRequestLifecycleError(
                f"终态 {state.value} 已锁定，迟到 {kind.value} 事件被拒绝"
            )
        if kind is ProviderRequestLifecycleEventKind.ACCEPTED:
            if state is not ProviderRequestLifecycleState.PREPARED:
                raise ProviderRequestLifecycleError("只有 prepared request 可以被 accepted")
            return ProviderRequestLifecycleState.ACCEPTED
        if kind is ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED:
            if state is not ProviderRequestLifecycleState.ACCEPTED:
                raise ProviderRequestLifecycleError("只有 accepted request 可以开始 synthesizing")
            return ProviderRequestLifecycleState.SYNTHESIZING
        if kind is ProviderRequestLifecycleEventKind.AUDIO_PRODUCED:
            if state is not ProviderRequestLifecycleState.SYNTHESIZING:
                raise ProviderRequestLifecycleError("只有 synthesizing request 可以报告 audio_produced")
            if not snapshot.audio_chunks:
                raise ProviderRequestLifecycleError("audio_produced 必须至少对应一个音频块")
            if snapshot.audio_produced:
                raise ProviderRequestLifecycleError("audio_produced 事实不得重复")
            return state
        if kind is ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED:
            if state is not ProviderRequestLifecycleState.SYNTHESIZING:
                raise ProviderRequestLifecycleError("只有 synthesizing request 可以报告 audio_enqueued")
            if not snapshot.audio_produced:
                raise ProviderRequestLifecycleError("audio_enqueued 必须在 audio_produced 之后")
            if snapshot.audio_enqueued:
                raise ProviderRequestLifecycleError("audio_enqueued 事实不得重复")
            return state
        if kind is ProviderRequestLifecycleEventKind.COMPLETED:
            if state is not ProviderRequestLifecycleState.SYNTHESIZING:
                raise ProviderRequestLifecycleError("只有 synthesizing request 可以 completed")
            if not snapshot.audio_chunks:
                raise ProviderRequestLifecycleError("completed 必须至少对应一个音频块")
            if not snapshot.audio_produced:
                raise ProviderRequestLifecycleError("completed 必须在 audio_produced 之后")
            return ProviderRequestLifecycleState.COMPLETED
        if kind is ProviderRequestLifecycleEventKind.FAILED:
            if state not in (
                ProviderRequestLifecycleState.PREPARED,
                ProviderRequestLifecycleState.ACCEPTED,
                ProviderRequestLifecycleState.SYNTHESIZING,
            ):
                raise ProviderRequestLifecycleError("当前状态不得 failed")
            return ProviderRequestLifecycleState.FAILED
        if kind is ProviderRequestLifecycleEventKind.CANCELLED:
            if state not in (
                ProviderRequestLifecycleState.PREPARED,
                ProviderRequestLifecycleState.ACCEPTED,
                ProviderRequestLifecycleState.SYNTHESIZING,
            ):
                raise ProviderRequestLifecycleError("当前状态不得 cancelled")
            return ProviderRequestLifecycleState.CANCELLED
        raise ProviderRequestLifecycleError("未知生命周期事件")


__all__ = [
    "ProviderRequestAudioChunk",
    "ProviderRequestLifecycle",
    "ProviderRequestLifecycleError",
    "ProviderRequestLifecycleEvent",
    "ProviderRequestLifecycleEventKind",
    "ProviderRequestLifecycleReasonCode",
    "ProviderRequestLifecycleRegistry",
    "ProviderRequestLifecycleState",
]
