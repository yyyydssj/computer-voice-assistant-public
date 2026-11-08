"""P7 本地 TTS 路由与 VoiceStudio→Edge→文字降级状态契约。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_identifier, require_non_empty
from app.tts.lifecycle import ProviderRequestLifecycleRegistry, ProviderRequestLifecycleState
from app.tts.playback import ProviderRequestPlaybackRegistry, ProviderRequestPlaybackState
from app.tts.playback.queue import PlaybackQueue


class TTSRoutingError(ValueError):
    """本地路由状态、回复边界或降级输入违反契约。"""


class TTSRoutingPreflightRejection(str, Enum):
    """动作计划门控拒绝的稳定分类；所有分类都要求调用者重建计划。"""

    REQUEST_IDENTITY_DRIFT = "request_identity_drift"
    LIFECYCLE_DRIFT = "lifecycle_drift"
    PLAYBACK_DRIFT = "playback_drift"
    QUEUE_DRIFT = "queue_drift"
    ROUTER_DRIFT = "router_drift"
    PLAN_DRIFT = "plan_drift"


class _TTSRoutingPreflightRejected(TTSRoutingError):
    """内部携带稳定拒绝分类的只读门控异常。"""

    def __init__(self, rejection: TTSRoutingPreflightRejection, message: str) -> None:
        self.rejection = rejection
        super().__init__(message)



class TTSRoutingRebuildObservationConsumer(str, Enum):
    """摘要的固定消费边界；不表示重建、动作或执行许可。"""

    SUMMARY_ONLY = "summary_only"

class TTSRoutingRebuildObservationPurpose(str, Enum):
    """摘要的固定只读用途；不授予重建或动作执行权限。"""

    WINDOW_STABILITY_REVIEW = "window_stability_review"


class TTSRoutingRebuildConsistencyState(str, Enum):
    """重建输入与重新观察结果的只读一致性状态。"""

    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"


class TTSRoutingRebuildObservationDisposition(str, Enum):
    """观察结果的调用者处置提示；不授予重建或执行许可。"""

    RETAIN_READ_ONLY = "retain_read_only"
    DISCARD_AND_REOBSERVE = "discard_and_reobserve"


class TTSRoutingRebuildMismatch(str, Enum):
    """重建输入与重新观察 binding/路由快照之间的稳定不一致分类。"""

    REQUEST_IDENTITY = "request_identity"
    REQUEST_VERSION = "request_version"
    LIFECYCLE = "lifecycle"
    PLAYBACK = "playback"
    QUEUE = "queue"
    ROUTER = "router"


class TTSRoute(str, Enum):
    """当前或下一回复允许使用的本地输出路由。"""

    VOICESTUDIO = "voicestudio"
    EDGE = "edge"
    TEXT = "text"


class TTSRoutingAction(str, Enum):
    """调用者必须执行的有界路由动作；本对象不调用 Provider 或设备。"""

    USE_EDGE_CURRENT_SEGMENT = "use_edge_current_segment"
    CONTINUE_TEXT_NO_REPLAY = "continue_text_no_replay"


class TTSRoutingActionStep(str, Enum):
    """路由降级后调用者可执行的本地动作步骤；不执行任何动作。"""

    CANCEL_LIFECYCLE = "cancel_lifecycle"
    ABORT_PLAYBACK = "abort_playback"
    CANCEL_QUEUE = "cancel_queue"


class TTSRoutingPreflightState(str, Enum):
    """动作计划执行前门控结果；READY 也不表示任何动作已经执行。"""

    READY = "ready"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class TTSRoutingDecision:
    """一次 Provider 故障后的不可变本地路由决定。"""

    reply_id: str
    segment_id: str
    route: TTSRoute
    action: TTSRoutingAction
    playback_state: ProviderRequestPlaybackState
    replay_current_segment: bool
    drop_remaining_segments: bool
    cancel_previous_provider_request: bool
    next_reply_route: TTSRoute
    reason: str

    def __post_init__(self) -> None:
        for name, value in (("reply_id", self.reply_id), ("segment_id", self.segment_id)):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        if not isinstance(self.route, TTSRoute):
            raise TTSRoutingError("route 必须是 TTSRoute")
        if not isinstance(self.action, TTSRoutingAction):
            raise TTSRoutingError("action 必须是 TTSRoutingAction")
        if not isinstance(self.playback_state, ProviderRequestPlaybackState):
            raise TTSRoutingError("playback_state 必须是 ProviderRequestPlaybackState")
        if not isinstance(self.next_reply_route, TTSRoute):
            raise TTSRoutingError("next_reply_route 必须是 TTSRoute")
        for name, value in (
            ("replay_current_segment", self.replay_current_segment),
            ("drop_remaining_segments", self.drop_remaining_segments),
            ("cancel_previous_provider_request", self.cancel_previous_provider_request),
        ):
            if not isinstance(value, bool):
                raise TTSRoutingError(f"{name} 必须是 bool")
        try:
            require_non_empty("reason", self.reason, maximum=512)
        except ContractError as exc:
            raise TTSRoutingError(str(exc)) from exc
        if self.replay_current_segment:
            raise TTSRoutingError("本地降级契约禁止自动重播当前句")
        if not self.drop_remaining_segments:
            raise TTSRoutingError("故障降级必须丢弃当前回复剩余旧句")
        if self.action is TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT:
            if self.route is not TTSRoute.EDGE:
                raise TTSRoutingError("USE_EDGE_CURRENT_SEGMENT 必须路由到 Edge")
            if self.playback_state not in (
                ProviderRequestPlaybackState.NOT_COMMITTED,
                ProviderRequestPlaybackState.COMMITTED_NOT_STARTED,
            ):
                raise TTSRoutingError("只有未开始播放的句子才能切到 Edge")
        elif self.action is TTSRoutingAction.CONTINUE_TEXT_NO_REPLAY:
            if self.route is not TTSRoute.TEXT:
                raise TTSRoutingError("CONTINUE_TEXT_NO_REPLAY 必须路由到文字")


@dataclass(frozen=True, slots=True)
class TTSRoutingSnapshot:
    """路由器快照；不代表 Provider、网络、PCM 或播放设备状态。"""

    active_reply_id: str | None
    active_route: TTSRoute | None
    next_reply_route: TTSRoute
    edge_configured: bool


class TTSFallbackRouter:
    """在回复边界上固定 VoiceStudio→Edge→文字的本地路由。

    这是同步、进程内的元数据状态机。它只返回调用者应执行的动作，不取消
    Provider、不停止设备、不提交播放事实，也不证明用户听见了任何音频。
    """

    def __init__(self, *, edge_voice_profile_id: str | None = None) -> None:
        if edge_voice_profile_id is not None:
            try:
                require_identifier("edge_voice_profile_id", edge_voice_profile_id)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        self._edge_voice_profile_id = edge_voice_profile_id
        self._active_reply_id: str | None = None
        self._active_route: TTSRoute | None = None
        self._next_reply_route = TTSRoute.VOICESTUDIO
        self._primary_failed_segments: set[tuple[str, str]] = set()
        self._edge_failed_segments: set[tuple[str, str]] = set()

    @property
    def edge_configured(self) -> bool:
        return self._edge_voice_profile_id is not None

    @property
    def snapshot(self) -> TTSRoutingSnapshot:
        return TTSRoutingSnapshot(
            active_reply_id=self._active_reply_id,
            active_route=self._active_route,
            next_reply_route=self._next_reply_route,
            edge_configured=self.edge_configured,
        )

    def start_reply(self, reply_id: str) -> TTSRoute:
        try:
            require_identifier("reply_id", reply_id)
        except ContractError as exc:
            raise TTSRoutingError(str(exc)) from exc
        if self._active_reply_id is not None:
            raise TTSRoutingError("当前 reply 尚未结束，不得中途切换路由")
        self._active_reply_id = reply_id
        self._active_route = self._next_reply_route
        return self._active_route

    def _require_active(self, reply_id: str, segment_id: str) -> tuple[str, str]:
        for name, value in (("reply_id", reply_id), ("segment_id", segment_id)):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        if self._active_reply_id != reply_id or self._active_route is None:
            raise TTSRoutingError("reply 不在当前活动路由中")
        key = (reply_id, segment_id)
        return key

    def primary_failed(
        self,
        reply_id: str,
        segment_id: str,
        *,
        playback_state: ProviderRequestPlaybackState,
    ) -> TTSRoutingDecision:
        """处理 VoiceStudio 故障；不自动重播、不跨句续播。"""
        key = self._require_active(reply_id, segment_id)
        if self._active_route is not TTSRoute.VOICESTUDIO:
            raise TTSRoutingError("只有当前路由为 VoiceStudio 时才能处理 primary 故障")
        if not isinstance(playback_state, ProviderRequestPlaybackState):
            raise TTSRoutingError("playback_state 必须是 ProviderRequestPlaybackState")
        if key in self._primary_failed_segments:
            raise TTSRoutingError("同一 VoiceStudio 故障不得重复处理")
        self._primary_failed_segments.add(key)
        not_started = playback_state in (
            ProviderRequestPlaybackState.NOT_COMMITTED,
            ProviderRequestPlaybackState.COMMITTED_NOT_STARTED,
        )
        if not_started and self.edge_configured:
            self._active_route = TTSRoute.EDGE
            self._next_reply_route = TTSRoute.EDGE
            return TTSRoutingDecision(
                reply_id=reply_id,
                segment_id=segment_id,
                route=TTSRoute.EDGE,
                action=TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT,
                playback_state=playback_state,
                replay_current_segment=False,
                drop_remaining_segments=True,
                cancel_previous_provider_request=True,
                next_reply_route=TTSRoute.EDGE,
                reason="VoiceStudio 在当前句尚未开始播放，改用已配置 Edge；当前回复剩余旧句丢弃",
            )
        self._active_route = TTSRoute.TEXT
        self._next_reply_route = TTSRoute.EDGE if self.edge_configured else TTSRoute.TEXT
        return TTSRoutingDecision(
            reply_id=reply_id,
            segment_id=segment_id,
            route=TTSRoute.TEXT,
            action=TTSRoutingAction.CONTINUE_TEXT_NO_REPLAY,
            playback_state=playback_state,
            replay_current_segment=False,
            drop_remaining_segments=True,
            cancel_previous_provider_request=True,
            next_reply_route=self._next_reply_route,
            reason="VoiceStudio 已开始或已完成本句边界，禁止半句换声和整句重播；当前回复转文字",
        )

    def edge_failed(
        self,
        reply_id: str,
        segment_id: str,
        *,
        playback_state: ProviderRequestPlaybackState = ProviderRequestPlaybackState.NOT_COMMITTED,
    ) -> TTSRoutingDecision:
        """Edge 失败后进入文字模式；不尝试第三 Provider。"""
        key = self._require_active(reply_id, segment_id)
        if self._active_route is not TTSRoute.EDGE:
            raise TTSRoutingError("只有当前路由为 Edge 时才能处理 Edge 故障")
        if not isinstance(playback_state, ProviderRequestPlaybackState):
            raise TTSRoutingError("playback_state 必须是 ProviderRequestPlaybackState")
        if key in self._edge_failed_segments:
            raise TTSRoutingError("同一 Edge 故障不得重复处理")
        self._edge_failed_segments.add(key)
        self._active_route = TTSRoute.TEXT
        self._next_reply_route = TTSRoute.TEXT
        return TTSRoutingDecision(
            reply_id=reply_id,
            segment_id=segment_id,
            route=TTSRoute.TEXT,
            action=TTSRoutingAction.CONTINUE_TEXT_NO_REPLAY,
            playback_state=playback_state,
            replay_current_segment=False,
            drop_remaining_segments=True,
            cancel_previous_provider_request=True,
            next_reply_route=TTSRoute.TEXT,
            reason="Edge 也不可用，当前会话进入文字模式且不尝试第三 Provider",
        )

    def finish_reply(self, reply_id: str) -> None:
        if self._active_reply_id != reply_id:
            raise TTSRoutingError("只能结束当前活动 reply")
        self._active_reply_id = None
        self._active_route = None

    def primary_recovered(self) -> TTSRoutingSnapshot:
        """记录恢复；当前 reply 不切回，下一新 reply 才可恢复 VoiceStudio。"""
        self._next_reply_route = TTSRoute.VOICESTUDIO
        return self.snapshot

@dataclass(frozen=True, slots=True)
class TTSRoutingBinding:
    """一次降级决定与现有本地事实的不可变绑定；不代表动作已执行。"""

    decision: TTSRoutingDecision
    provider_request_id: str
    request_sequence: int
    provider_generation: int
    lifecycle_state: ProviderRequestLifecycleState
    playback_state: ProviderRequestPlaybackState
    queue_pending_provider_request_ids: tuple[str, ...]
    queue_active_provider_request_id: str | None

    @property
    def queued(self) -> bool:
        return self.provider_request_id in self.queue_pending_provider_request_ids

    @property
    def active(self) -> bool:
        return self.provider_request_id == self.queue_active_provider_request_id


@dataclass(frozen=True, slots=True)
class TTSRoutingRebuildInput:
    """拒绝后提供给调用者的当前本地重建输入；不包含可执行动作。"""

    provider_request_id: str
    reply_id: str
    segment_id: str
    request_sequence: int
    provider_generation: int
    lifecycle_state: ProviderRequestLifecycleState
    playback_state: ProviderRequestPlaybackState
    queue_pending_provider_request_ids: tuple[str, ...]
    queue_active_provider_request_id: str | None
    router_snapshot: TTSRoutingSnapshot

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_request_id", self.provider_request_id),
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
        ):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        for name, value in (("request_sequence", self.request_sequence), ("provider_generation", self.provider_generation)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise TTSRoutingError(f"{name} 必须是正整数")
        if not isinstance(self.lifecycle_state, ProviderRequestLifecycleState):
            raise TTSRoutingError("lifecycle_state 必须是 ProviderRequestLifecycleState")
        if not isinstance(self.playback_state, ProviderRequestPlaybackState):
            raise TTSRoutingError("playback_state 必须是 ProviderRequestPlaybackState")
        if not isinstance(self.queue_pending_provider_request_ids, tuple):
            raise TTSRoutingError("queue_pending_provider_request_ids 必须是 tuple")
        for value in self.queue_pending_provider_request_ids:
            try:
                require_identifier("queue_pending_provider_request_id", value)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        if self.queue_active_provider_request_id is not None:
            try:
                require_identifier("queue_active_provider_request_id", self.queue_active_provider_request_id)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        if not isinstance(self.router_snapshot, TTSRoutingSnapshot):
            raise TTSRoutingError("router_snapshot 必须是 TTSRoutingSnapshot")



@dataclass(frozen=True, slots=True)
class TTSRoutingRebuildConsistency:
    """重建输入与重新观察结果的不可执行、只读一致性提示。"""

    state: TTSRoutingRebuildConsistencyState
    mismatches: tuple[TTSRoutingRebuildMismatch, ...]
    notice: str

    @property
    def consistent(self) -> bool:
        return self.state is TTSRoutingRebuildConsistencyState.CONSISTENT

    def __post_init__(self) -> None:
        if not isinstance(self.state, TTSRoutingRebuildConsistencyState):
            raise TTSRoutingError("state 必须是 TTSRoutingRebuildConsistencyState")
        if not isinstance(self.mismatches, tuple):
            raise TTSRoutingError("mismatches 必须是 tuple")
        for mismatch in self.mismatches:
            if not isinstance(mismatch, TTSRoutingRebuildMismatch):
                raise TTSRoutingError("mismatches 只能包含 TTSRoutingRebuildMismatch")
        if self.state is TTSRoutingRebuildConsistencyState.CONSISTENT and self.mismatches:
            raise TTSRoutingError("一致结果不得携带不一致分类")
        if self.state is TTSRoutingRebuildConsistencyState.INCONSISTENT and not self.mismatches:
            raise TTSRoutingError("不一致结果必须携带至少一个不一致分类")
        try:
            require_non_empty("notice", self.notice, maximum=512)
        except ContractError as exc:
            raise TTSRoutingError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class TTSRoutingRebuildObservationReview:
    """一次重建观察消费回执；只描述保留或丢弃提示，不保存任何运行状态。"""

    rebuild_input: TTSRoutingRebuildInput
    observed: TTSRoutingRebuildInput
    consistency: TTSRoutingRebuildConsistency
    disposition: TTSRoutingRebuildObservationDisposition

    @property
    def consistent(self) -> bool:
        return self.consistency.consistent

    @property
    def requires_reobservation(self) -> bool:
        return self.disposition is TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE

    @property
    def notice(self) -> str:
        if self.requires_reobservation:
            return "只读消费提示：丢弃旧重建输入与旧计划，并重新观察当前本地事实；本回执不执行任何动作。"
        return "只读消费提示：可保留本次一致性回执供调用者审阅；本回执不执行任何动作。"

    def __post_init__(self) -> None:
        if not isinstance(self.rebuild_input, TTSRoutingRebuildInput):
            raise TTSRoutingError("rebuild_input 必须是 TTSRoutingRebuildInput")
        if not isinstance(self.observed, TTSRoutingRebuildInput):
            raise TTSRoutingError("observed 必须是 TTSRoutingRebuildInput")
        if not isinstance(self.consistency, TTSRoutingRebuildConsistency):
            raise TTSRoutingError("consistency 必须是 TTSRoutingRebuildConsistency")
        if not isinstance(self.disposition, TTSRoutingRebuildObservationDisposition):
            raise TTSRoutingError("disposition 必须是 TTSRoutingRebuildObservationDisposition")
        expected = (
            TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY
            if self.consistency.consistent
            else TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE
        )
        if self.disposition is not expected:
            raise TTSRoutingError("disposition 必须与 consistency 一致")
        if (self.rebuild_input == self.observed) is not self.consistency.consistent:
            raise TTSRoutingError("consistency must match observed snapshot")


@dataclass(frozen=True, slots=True)
class TTSRoutingRebuildObservationWindowSummary:
    """多观察窗口的最小只读审阅投影；不携带快照或执行状态。"""
    disposition: TTSRoutingRebuildObservationDisposition

    observation_count: int
    mismatches: tuple[TTSRoutingRebuildMismatch, ...]
    consumer: TTSRoutingRebuildObservationConsumer = TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY
    purpose: TTSRoutingRebuildObservationPurpose = TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW

    @property
    def consistent(self) -> bool:
        return self.disposition is TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY

    @property
    def requires_reobservation(self) -> bool:
        return self.disposition is TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE

    @property
    def notice(self) -> str:
        if self.requires_reobservation:
            return "只读审阅摘要：窗口存在不一致原因；调用者必须丢弃旧输入与旧计划并重新观察；本摘要不执行任何动作。"
        return "只读审阅摘要：窗口内观察结果一致；本摘要仅供调用者审阅，不执行任何动作。"

    def __post_init__(self) -> None:
        if self.purpose is not TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW:
            raise TTSRoutingError("purpose 必须是 TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW")
        if self.consumer is not TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY:
            raise TTSRoutingError("consumer 必须是 TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY")
        if not isinstance(self.disposition, TTSRoutingRebuildObservationDisposition):
            raise TTSRoutingError("disposition 必须是 TTSRoutingRebuildObservationDisposition")
        if isinstance(self.observation_count, bool) or not isinstance(self.observation_count, int) or self.observation_count < 2:
            raise TTSRoutingError("observation_count 必须是至少为 2 的正整数")
        if not isinstance(self.mismatches, tuple):
            raise TTSRoutingError("mismatches 必须是 tuple")
        if any(not isinstance(mismatch, TTSRoutingRebuildMismatch) for mismatch in self.mismatches):
            raise TTSRoutingError("mismatches 只能包含 TTSRoutingRebuildMismatch")
        if len(set(self.mismatches)) != len(self.mismatches):
            raise TTSRoutingError("mismatches 不得重复")
        ordered = tuple(mismatch for mismatch in TTSRoutingRebuildMismatch if mismatch in self.mismatches)
        if self.mismatches != ordered:
            raise TTSRoutingError("mismatches 必须按 TTSRoutingRebuildMismatch 契约顺序排列")
        if self.consistent and self.mismatches:
            raise TTSRoutingError("一致摘要不得携带不一致原因")
        if self.requires_reobservation and not self.mismatches:
            raise TTSRoutingError("需重新观察摘要必须携带至少一个不一致原因")


@dataclass(frozen=True, slots=True)
class TTSRoutingRebuildObservationWindowReview:
    """多次显式观察窗口的只读消费回执；不缓存或重读任何本地事实。"""

    rebuild_input: TTSRoutingRebuildInput
    reviews: tuple[TTSRoutingRebuildObservationReview, ...]
    disposition: TTSRoutingRebuildObservationDisposition

    @property
    def observations(self) -> tuple[TTSRoutingRebuildInput, ...]:
        return tuple(review.observed for review in self.reviews)

    @property
    def consistencies(self) -> tuple[TTSRoutingRebuildConsistency, ...]:
        return tuple(review.consistency for review in self.reviews)

    @property
    def consistent(self) -> bool:
        return all(review.consistent for review in self.reviews)

    @property
    def mismatches(self) -> tuple[TTSRoutingRebuildMismatch, ...]:
        """返回窗口内去重且按契约顺序排列的不一致原因摘要。"""
        present = {
            mismatch
            for consistency in self.consistencies
            for mismatch in consistency.mismatches
        }
        return tuple(
            mismatch
            for mismatch in TTSRoutingRebuildMismatch
            if mismatch in present
        )

    @property
    def mismatch_notice(self) -> str:
        """返回稳定的只读原因摘要；不表示任何动作许可。"""
        if not self.mismatches:
            return "只读窗口原因摘要：未发现不一致原因；本摘要不执行任何动作。"
        labels = "、".join(mismatch.value for mismatch in self.mismatches)
        return f"只读窗口原因摘要：{labels}；本摘要不执行任何动作。"

    @property
    def summary(self) -> TTSRoutingRebuildObservationWindowSummary:
        """返回不含原始快照的最小只读审阅摘要。"""
        return TTSRoutingRebuildObservationWindowSummary(
            disposition=self.disposition,
            observation_count=len(self.reviews),
            mismatches=self.mismatches,
        )

    @property
    def requires_reobservation(self) -> bool:
        return self.disposition is TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE

    @property
    def notice(self) -> str:
        if self.requires_reobservation:
            return "只读窗口提示：至少一个显式观察结果与重建输入不一致；丢弃旧输入与旧计划，并重新观察当前本地事实；本回执不执行任何动作。"
        return "只读窗口提示：多个显式观察结果均与重建输入一致；可保留本窗口回执供调用者审阅；本回执不执行任何动作。"

    def __post_init__(self) -> None:
        if not isinstance(self.rebuild_input, TTSRoutingRebuildInput):
            raise TTSRoutingError("rebuild_input 必须是 TTSRoutingRebuildInput")
        if not isinstance(self.reviews, tuple) or len(self.reviews) < 2:
            raise TTSRoutingError("reviews 必须是至少包含两个元素的 tuple")
        for review in self.reviews:
            if not isinstance(review, TTSRoutingRebuildObservationReview):
                raise TTSRoutingError("reviews 只能包含 TTSRoutingRebuildObservationReview")
            if review.rebuild_input != self.rebuild_input:
                raise TTSRoutingError("窗口内所有 review 必须绑定相同 rebuild_input")
        if not isinstance(self.disposition, TTSRoutingRebuildObservationDisposition):
            raise TTSRoutingError("disposition 必须是 TTSRoutingRebuildObservationDisposition")
        expected = (
            TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY
            if self.consistent
            else TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE
        )
        if self.disposition is not expected:
            raise TTSRoutingError("disposition 必须与观察窗口一致")


@dataclass(frozen=True, slots=True)
class TTSRoutingActionPlan:
    """绑定后的本地动作顺序；只描述调用者动作，不回写任何事实。"""

    binding: TTSRoutingBinding
    steps: tuple[TTSRoutingActionStep, ...]
    lifecycle_cancel_reason: str | None
    playback_abort_reason: str | None
    queue_cancel_reply_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, TTSRoutingBinding):
            raise TTSRoutingError("binding 必须是 TTSRoutingBinding")
        if not isinstance(self.steps, tuple):
            raise TTSRoutingError("steps 必须是 tuple")
        if any(not isinstance(step, TTSRoutingActionStep) for step in self.steps):
            raise TTSRoutingError("steps 必须只包含 TTSRoutingActionStep")
        if len(set(self.steps)) != len(self.steps):
            raise TTSRoutingError("steps 不得重复")
        if self.steps != tuple(
            sorted(self.steps, key=lambda step: {
                TTSRoutingActionStep.CANCEL_LIFECYCLE: 0,
                TTSRoutingActionStep.ABORT_PLAYBACK: 1,
                TTSRoutingActionStep.CANCEL_QUEUE: 2,
            }[step])
        ):
            raise TTSRoutingError("动作顺序必须是 lifecycle cancel → playback abort → queue cancel")
        if self.lifecycle_cancel_reason is not None:
            try:
                require_non_empty("lifecycle_cancel_reason", self.lifecycle_cancel_reason, maximum=512)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        if self.playback_abort_reason is not None:
            try:
                require_non_empty("playback_abort_reason", self.playback_abort_reason, maximum=512)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
        if self.queue_cancel_reply_id is not None:
            try:
                require_identifier("queue_cancel_reply_id", self.queue_cancel_reply_id)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
            if self.queue_cancel_reply_id != self.binding.decision.reply_id:
                raise TTSRoutingError("queue_cancel_reply_id 必须绑定当前 reply")
        self._require_reason_for_step(
            TTSRoutingActionStep.CANCEL_LIFECYCLE,
            self.lifecycle_cancel_reason,
            "lifecycle cancel 步骤必须有 reason",
        )
        self._require_reason_for_step(
            TTSRoutingActionStep.ABORT_PLAYBACK,
            self.playback_abort_reason,
            "playback abort 步骤必须有 reason",
        )
        if TTSRoutingActionStep.CANCEL_QUEUE in self.steps and self.queue_cancel_reply_id is None:
            raise TTSRoutingError("queue cancel 步骤必须绑定 reply")
        if TTSRoutingActionStep.ABORT_PLAYBACK in self.steps and self.binding.playback_state not in (
            ProviderRequestPlaybackState.PLAYBACK_STARTED,
            ProviderRequestPlaybackState.PARTIALLY_PLAYED,
        ):
            raise TTSRoutingError("只有已开始或部分播放的句子才能安排 playback abort")

    def _require_reason_for_step(
        self,
        step: TTSRoutingActionStep,
        reason: str | None,
        message: str,
    ) -> None:
        if step in self.steps and reason is None:
            raise TTSRoutingError(message)
        if step not in self.steps and reason is not None:
            raise TTSRoutingError(f"{step.value} 未安排时不得携带 reason")


@dataclass(frozen=True, slots=True)
class TTSRoutingActionPreflight:
    """动作计划的不可变执行前门控结果；不执行动作、不回写事实。"""

    plan: TTSRoutingActionPlan
    state: TTSRoutingPreflightState
    reason: str | None
    rejection: TTSRoutingPreflightRejection | None = None
    rebuild_input: TTSRoutingRebuildInput | None = None

    @property
    def ready(self) -> bool:
        return self.state is TTSRoutingPreflightState.READY

    @property
    def requires_rebuild(self) -> bool:
        """REJECTED 只要求调用者丢弃旧计划并基于新快照重建。"""
        return self.state is TTSRoutingPreflightState.REJECTED

    @property
    def rebuild_hint(self) -> str | None:
        if not self.requires_rebuild:
            return None
        return "丢弃当前动作计划，并基于最新本地快照重新生成 binding 与动作计划"

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TTSRoutingActionPlan):
            raise TTSRoutingError("plan 必须是 TTSRoutingActionPlan")
        if not isinstance(self.state, TTSRoutingPreflightState):
            raise TTSRoutingError("state 必须是 TTSRoutingPreflightState")
        if self.state is TTSRoutingPreflightState.READY:
            if self.reason is not None:
                raise TTSRoutingError("READY 门控结果不得携带 reason")
            if self.rejection is not None:
                raise TTSRoutingError("READY 门控结果不得携带 rejection")
            if self.rebuild_input is not None:
                raise TTSRoutingError("READY 门控结果不得携带 rebuild_input")
        else:
            try:
                require_non_empty("reason", self.reason or "", maximum=512)
            except ContractError as exc:
                raise TTSRoutingError(str(exc)) from exc
            if not isinstance(self.rejection, TTSRoutingPreflightRejection):
                raise TTSRoutingError("REJECTED 门控结果必须携带稳定 rejection")


class TTSRoutingCoordinator:
    """只读检查 request、lifecycle、playback 和 queue，再推进本地路由。"""

    def __init__(
        self,
        router: TTSFallbackRouter,
        lifecycles: ProviderRequestLifecycleRegistry,
        playback: ProviderRequestPlaybackRegistry,
        queue: PlaybackQueue,
    ) -> None:
        if not isinstance(router, TTSFallbackRouter):
            raise TypeError("router 必须是 TTSFallbackRouter")
        if not isinstance(lifecycles, ProviderRequestLifecycleRegistry):
            raise TypeError("lifecycles 必须是 ProviderRequestLifecycleRegistry")
        if not isinstance(playback, ProviderRequestPlaybackRegistry) or playback._lifecycles is not lifecycles:
            raise TypeError("playback 必须绑定相同 lifecycle registry")
        if not isinstance(queue, PlaybackQueue) or queue._lifecycles is not lifecycles or queue._playback is not playback:
            raise TypeError("queue 必须绑定相同 lifecycle/playback registry")
        self._router = router
        self._lifecycles = lifecycles
        self._playback = playback
        self._queue = queue

    @property
    def router(self) -> TTSFallbackRouter:
        return self._router

    def primary_failed(self, reply_id: str, segment_id: str, provider_request_id: str) -> TTSRoutingBinding:
        lifecycle, fact = self._observe(reply_id, segment_id, provider_request_id, TTSRoute.VOICESTUDIO)
        decision = self._router.primary_failed(reply_id, segment_id, playback_state=fact.state)
        return self._bind(decision, lifecycle, fact.state)

    def edge_failed(self, reply_id: str, segment_id: str, provider_request_id: str) -> TTSRoutingBinding:
        lifecycle, fact = self._observe(reply_id, segment_id, provider_request_id, TTSRoute.EDGE)
        decision = self._router.edge_failed(reply_id, segment_id, playback_state=fact.state)
        return self._bind(decision, lifecycle, fact.state)

    def plan_actions(
        self,
        binding: TTSRoutingBinding,
        *,
        lifecycle_cancel_reason: str = "routing fallback superseded the previous Provider request",
        playback_abort_reason: str = "routing fallback aborted the current local playback fact",
    ) -> TTSRoutingActionPlan:
        """为已绑定决定生成严格顺序的本地动作计划，不执行动作。"""
        self._assert_binding_current(binding)
        lifecycle = self._lifecycles.get(binding.provider_request_id)
        fact = self._playback.get(binding.provider_request_id)
        pending = self._queue.pending
        steps: list[TTSRoutingActionStep] = []
        lifecycle_reason: str | None = None
        abort_reason: str | None = None
        queue_reply_id: str | None = None

        if not lifecycle.terminal:
            steps.append(TTSRoutingActionStep.CANCEL_LIFECYCLE)
            lifecycle_reason = lifecycle_cancel_reason
        if fact.state in (
            ProviderRequestPlaybackState.PLAYBACK_STARTED,
            ProviderRequestPlaybackState.PARTIALLY_PLAYED,
        ):
            steps.append(TTSRoutingActionStep.ABORT_PLAYBACK)
            abort_reason = playback_abort_reason
        if pending:
            steps.append(TTSRoutingActionStep.CANCEL_QUEUE)
            queue_reply_id = binding.decision.reply_id
        return TTSRoutingActionPlan(
            binding=binding,
            steps=tuple(steps),
            lifecycle_cancel_reason=lifecycle_reason,
            playback_abort_reason=abort_reason,
            queue_cancel_reply_id=queue_reply_id,
        )

    def observe_rebuild_input(self, rebuild_input: TTSRoutingRebuildInput) -> TTSRoutingRebuildInput:
        """从当前本地事实生成一次只读观察快照；不创建 binding 或动作计划。"""
        if not isinstance(rebuild_input, TTSRoutingRebuildInput):
            raise TTSRoutingError("rebuild_input 必须是 TTSRoutingRebuildInput")
        try:
            lifecycle = self._lifecycles.get(rebuild_input.provider_request_id)
            fact = self._playback.get(rebuild_input.provider_request_id)
        except (ContractError, TypeError, ValueError) as exc:
            raise TTSRoutingError(str(exc)) from exc
        request = lifecycle.request
        pending_ids = tuple(entry.provider_request_id for entry in self._queue.pending)
        active = self._queue.active
        return TTSRoutingRebuildInput(
            provider_request_id=request.provider_request_id,
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            request_sequence=request.request_sequence,
            provider_generation=request.provider_generation,
            lifecycle_state=lifecycle.state,
            playback_state=fact.state,
            queue_pending_provider_request_ids=pending_ids,
            queue_active_provider_request_id=active.provider_request_id if active else None,
            router_snapshot=self._router.snapshot,
        )

    def validate_rebuild_observation(
        self,
        rebuild_input: TTSRoutingRebuildInput,
        observed: TTSRoutingRebuildInput | TTSRoutingBinding,
    ) -> TTSRoutingRebuildConsistency:
        """只读比较拒绝时输入与重新观察快照，不执行任何动作。"""
        if not isinstance(rebuild_input, TTSRoutingRebuildInput):
            raise TTSRoutingError("rebuild_input 必须是 TTSRoutingRebuildInput")
        if isinstance(observed, TTSRoutingRebuildInput):
            observed_input = observed
            observed_binding = None
        elif isinstance(observed, TTSRoutingBinding):
            observed_input = TTSRoutingRebuildInput(
                provider_request_id=observed.provider_request_id,
                reply_id=observed.decision.reply_id,
                segment_id=observed.decision.segment_id,
                request_sequence=observed.request_sequence,
                provider_generation=observed.provider_generation,
                lifecycle_state=observed.lifecycle_state,
                playback_state=observed.playback_state,
                queue_pending_provider_request_ids=observed.queue_pending_provider_request_ids,
                queue_active_provider_request_id=observed.queue_active_provider_request_id,
                router_snapshot=self._router.snapshot,
            )
            observed_binding = observed
        else:
            raise TTSRoutingError("observed 必须是 TTSRoutingRebuildInput 或 TTSRoutingBinding")

        mismatches: list[TTSRoutingRebuildMismatch] = []
        if (
            rebuild_input.provider_request_id,
            rebuild_input.reply_id,
            rebuild_input.segment_id,
        ) != (
            observed_input.provider_request_id,
            observed_input.reply_id,
            observed_input.segment_id,
        ):
            mismatches.append(TTSRoutingRebuildMismatch.REQUEST_IDENTITY)
        if (
            rebuild_input.request_sequence,
            rebuild_input.provider_generation,
        ) != (
            observed_input.request_sequence,
            observed_input.provider_generation,
        ):
            mismatches.append(TTSRoutingRebuildMismatch.REQUEST_VERSION)
        if rebuild_input.lifecycle_state is not observed_input.lifecycle_state:
            mismatches.append(TTSRoutingRebuildMismatch.LIFECYCLE)
        if rebuild_input.playback_state is not observed_input.playback_state:
            mismatches.append(TTSRoutingRebuildMismatch.PLAYBACK)
        if (
            rebuild_input.queue_pending_provider_request_ids,
            rebuild_input.queue_active_provider_request_id,
        ) != (
            observed_input.queue_pending_provider_request_ids,
            observed_input.queue_active_provider_request_id,
        ):
            mismatches.append(TTSRoutingRebuildMismatch.QUEUE)

        if rebuild_input.router_snapshot != observed_input.router_snapshot:
            mismatches.append(TTSRoutingRebuildMismatch.ROUTER)
        if observed_binding is not None:
            current_snapshot = self._router.snapshot
            if (
                current_snapshot.active_reply_id != observed_binding.decision.reply_id
                or current_snapshot.active_route is not observed_binding.decision.route
            ) and TTSRoutingRebuildMismatch.ROUTER not in mismatches:
                mismatches.append(TTSRoutingRebuildMismatch.ROUTER)

        if mismatches:
            return TTSRoutingRebuildConsistency(
                state=TTSRoutingRebuildConsistencyState.INCONSISTENT,
                mismatches=tuple(mismatches),
                notice="只读不一致提示：重建输入与重新观察 binding/路由快照不一致；不得继续沿用旧计划，需由调用者重新读取本地事实。",
            )
        return TTSRoutingRebuildConsistency(
            state=TTSRoutingRebuildConsistencyState.CONSISTENT,
            mismatches=(),
            notice="只读一致性提示：重建输入与重新观察 binding/路由快照一致；本结果不执行任何动作。",
        )

    def consume_rebuild_observation(
        self,
        rebuild_input: TTSRoutingRebuildInput,
        observed: TTSRoutingRebuildInput,
    ) -> TTSRoutingRebuildObservationReview:
        """只读消费一次观察快照，返回调用者保留或丢弃的稳定提示。"""
        if not isinstance(rebuild_input, TTSRoutingRebuildInput):
            raise TTSRoutingError("rebuild_input 必须是 TTSRoutingRebuildInput")
        if not isinstance(observed, TTSRoutingRebuildInput):
            raise TTSRoutingError("observed 必须是 TTSRoutingRebuildInput")
        consistency = self.validate_rebuild_observation(rebuild_input, observed)
        disposition = (
            TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY
            if consistency.consistent
            else TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE
        )
        return TTSRoutingRebuildObservationReview(
            rebuild_input=rebuild_input,
            observed=observed,
            consistency=consistency,
            disposition=disposition,
        )

    def consume_rebuild_observation_window(
        self,
        rebuild_input: TTSRoutingRebuildInput,
        observed_window: tuple[TTSRoutingRebuildInput, ...],
    ) -> TTSRoutingRebuildObservationWindowReview:
        """只读消费至少两个调用者观察结果，返回窗口稳定性提示。"""
        if not isinstance(rebuild_input, TTSRoutingRebuildInput):
            raise TTSRoutingError("rebuild_input 必须是 TTSRoutingRebuildInput")
        if not isinstance(observed_window, tuple) or len(observed_window) < 2:
            raise TTSRoutingError("observed_window 必须是至少包含两个元素的 tuple")
        if any(not isinstance(observed, TTSRoutingRebuildInput) for observed in observed_window):
            raise TTSRoutingError("observed_window 只能包含 TTSRoutingRebuildInput")
        reviews = tuple(
            self.consume_rebuild_observation(rebuild_input, observed)
            for observed in observed_window
        )
        disposition = (
            TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY
            if all(review.consistent for review in reviews)
            else TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE
        )
        return TTSRoutingRebuildObservationWindowReview(
            rebuild_input=rebuild_input,
            reviews=reviews,
            disposition=disposition,
        )

    def summarize_rebuild_observation_window(
        self,
        rebuild_input: TTSRoutingRebuildInput,
        observed_window: tuple[TTSRoutingRebuildInput, ...],
    ) -> TTSRoutingRebuildObservationWindowSummary:
        """只读消费观察窗口的最小摘要；不向调用者暴露原始窗口回执。"""
        return self.consume_rebuild_observation_window(rebuild_input, observed_window).summary


    def preflight_actions(self, plan: TTSRoutingActionPlan) -> TTSRoutingActionPreflight:
        """只读复核动作计划；事实漂移时返回 REJECTED，不执行任何动作。"""
        if not isinstance(plan, TTSRoutingActionPlan):
            raise TTSRoutingError("plan 必须是 TTSRoutingActionPlan")
        try:
            self._assert_binding_current(plan.binding)
            lifecycle = self._lifecycles.get(plan.binding.provider_request_id)
            fact = self._playback.get(plan.binding.provider_request_id)
            self._assert_plan_matches_current(plan, lifecycle, fact, self._queue.pending)
        except _TTSRoutingPreflightRejected as exc:
            return TTSRoutingActionPreflight(
                plan=plan,
                state=TTSRoutingPreflightState.REJECTED,
                reason=str(exc),
                rejection=exc.rejection,
                rebuild_input=self._current_rebuild_input(plan.binding.provider_request_id),
            )
        except TTSRoutingError as exc:
            return TTSRoutingActionPreflight(
                plan=plan,
                state=TTSRoutingPreflightState.REJECTED,
                reason=str(exc),
                rejection=TTSRoutingPreflightRejection.PLAN_DRIFT,
                rebuild_input=self._current_rebuild_input(plan.binding.provider_request_id),
            )
        return TTSRoutingActionPreflight(
            plan=plan,
            state=TTSRoutingPreflightState.READY,
            reason=None,
            rejection=None,
        )

    def _current_rebuild_input(self, provider_request_id: str) -> TTSRoutingRebuildInput | None:
        """只读取当前本地事实；读取失败时不给调用者伪造重建输入。"""
        try:
            lifecycle = self._lifecycles.get(provider_request_id)
            fact = self._playback.get(provider_request_id)
            request = lifecycle.request
            active = self._queue.active
            return TTSRoutingRebuildInput(
                provider_request_id=request.provider_request_id,
                reply_id=request.reply_id,
                segment_id=request.segment_id,
                request_sequence=request.request_sequence,
                provider_generation=request.provider_generation,
                lifecycle_state=lifecycle.state,
                playback_state=fact.state,
                queue_pending_provider_request_ids=tuple(
                    entry.provider_request_id for entry in self._queue.pending
                ),
                queue_active_provider_request_id=active.provider_request_id if active else None,
                router_snapshot=self._router.snapshot,
            )
        except (ContractError, TypeError, ValueError):
            return None

    def _assert_plan_matches_current(self, plan, lifecycle, fact, pending) -> None:
        expected_steps: list[TTSRoutingActionStep] = []
        if not lifecycle.terminal:
            expected_steps.append(TTSRoutingActionStep.CANCEL_LIFECYCLE)
        if fact.state in (
            ProviderRequestPlaybackState.PLAYBACK_STARTED,
            ProviderRequestPlaybackState.PARTIALLY_PLAYED,
        ):
            expected_steps.append(TTSRoutingActionStep.ABORT_PLAYBACK)
        if pending:
            expected_steps.append(TTSRoutingActionStep.CANCEL_QUEUE)
        if plan.steps != tuple(expected_steps):
            raise _TTSRoutingPreflightRejected(
                TTSRoutingPreflightRejection.PLAN_DRIFT,
                "动作计划与当前 lifecycle/playback/queue 事实不一致",
            )

    def _assert_binding_current(self, binding: TTSRoutingBinding) -> None:
        if not isinstance(binding, TTSRoutingBinding):
            raise TTSRoutingError("binding 必须是 TTSRoutingBinding")
        try:
            lifecycle = self._lifecycles.get(binding.provider_request_id)
            fact = self._playback.get(binding.provider_request_id)
        except (TypeError, ValueError) as exc:
            raise TTSRoutingError(str(exc)) from exc
        request = lifecycle.request
        if (
            request.reply_id != binding.decision.reply_id
            or request.segment_id != binding.decision.segment_id
            or request.request_sequence != binding.request_sequence
            or request.provider_generation != binding.provider_generation
        ):
            raise _TTSRoutingPreflightRejected(
                TTSRoutingPreflightRejection.REQUEST_IDENTITY_DRIFT,
                "binding 与当前 Provider request 身份或代际不一致",
            )
        if lifecycle.state is not binding.lifecycle_state:
            raise _TTSRoutingPreflightRejected(
                TTSRoutingPreflightRejection.LIFECYCLE_DRIFT,
                "binding 已过期，当前 lifecycle 事实已漂移",
            )
        if fact.state is not binding.playback_state:
            raise _TTSRoutingPreflightRejected(
                TTSRoutingPreflightRejection.PLAYBACK_DRIFT,
                "binding 已过期，当前 playback 事实已漂移",
            )
        pending_ids = tuple(entry.provider_request_id for entry in self._queue.pending)
        active = self._queue.active
        active_id = active.provider_request_id if active else None
        if pending_ids != binding.queue_pending_provider_request_ids or active_id != binding.queue_active_provider_request_id:
            raise _TTSRoutingPreflightRejected(
                TTSRoutingPreflightRejection.QUEUE_DRIFT,
                "binding 与当前 queue 事实不一致",
            )
        snapshot = self._router.snapshot
        if snapshot.active_reply_id != binding.decision.reply_id or snapshot.active_route is not binding.decision.route:
            raise _TTSRoutingPreflightRejected(
                TTSRoutingPreflightRejection.ROUTER_DRIFT,
                "binding 与当前 reply/路由不一致",
            )

    def _observe(self, reply_id: str, segment_id: str, provider_request_id: str, route: TTSRoute):
        try:
            require_identifier("reply_id", reply_id)
            require_identifier("segment_id", segment_id)
            require_identifier("provider_request_id", provider_request_id)
            lifecycle = self._lifecycles.get(provider_request_id)
            fact = self._playback.get(provider_request_id)
        except (ContractError, TypeError, ValueError) as exc:
            raise TTSRoutingError(str(exc)) from exc
        request = lifecycle.request
        if (request.reply_id, request.segment_id) != (reply_id, segment_id):
            raise TTSRoutingError("request 与回复/句身份不一致")
        if request.provider_name != route.value:
            raise TTSRoutingError("request 的 Provider 与故障路由不一致")
        if self._router.snapshot.active_reply_id != reply_id or self._router.snapshot.active_route is not route:
            raise TTSRoutingError("当前 reply/路由与故障不一致")
        active_generation = self._lifecycles.active_generation(request.provider_name)
        if active_generation is not None and active_generation != request.provider_generation:
            raise TTSRoutingError("旧 Provider generation 不得驱动路由")
        pending = self._queue.pending
        active = self._queue.active
        if any(entry.reply_id != reply_id for entry in pending):
            raise TTSRoutingError("队列存在其他回复，禁止跨回复降级")
        if active is not None and (not pending or active != pending[0]):
            raise TTSRoutingError("队列活动句与队首不一致")
        return lifecycle, fact

    def _bind(self, decision: TTSRoutingDecision, lifecycle, playback_state) -> TTSRoutingBinding:
        request = lifecycle.request
        active = self._queue.active
        return TTSRoutingBinding(
            decision=decision,
            provider_request_id=request.provider_request_id,
            request_sequence=request.request_sequence,
            provider_generation=request.provider_generation,
            lifecycle_state=lifecycle.state,
            playback_state=playback_state,
            queue_pending_provider_request_ids=tuple(entry.provider_request_id for entry in self._queue.pending),
            queue_active_provider_request_id=active.provider_request_id if active else None,
        )

__all__ = [
    "TTSFallbackRouter",
    "TTSRoute",
    "TTSRoutingAction",
    "TTSRoutingDecision",
    "TTSRoutingError",
    "TTSRoutingSnapshot",
    "TTSRoutingBinding",
    "TTSRoutingRebuildInput",
    "TTSRoutingRebuildConsistencyState",
    "TTSRoutingRebuildObservationConsumer",
    "TTSRoutingRebuildObservationPurpose",
    "TTSRoutingRebuildObservationDisposition",
    "TTSRoutingRebuildMismatch",
    "TTSRoutingRebuildConsistency",
    "TTSRoutingRebuildObservationReview",
    "TTSRoutingRebuildObservationWindowReview",
    "TTSRoutingRebuildObservationWindowSummary",
    "TTSRoutingActionStep",
    "TTSRoutingPreflightState",
    "TTSRoutingPreflightRejection",
    "TTSRoutingActionPlan",
    "TTSRoutingActionPreflight",
    "TTSRoutingCoordinator",
]
