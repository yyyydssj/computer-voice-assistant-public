"""P6 批次 6：ASR 故障、文字降级和恢复待用户状态机。

本模块只管理输入通道事实与会话边界。它不启动后台任务、不自动重试或切换
Provider，不创建 Agent request，也不改变任务、计划、授权或执行状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.contracts.base import parse_utc_timestamp, require_identifier, require_positive


class AsrInputMode(str, Enum):
    TEXT = "text"
    VOICE = "voice"


class AsrRecoveryState(str, Enum):
    STARTUP_LOADING = "asr_startup_loading"
    UNAVAILABLE_STARTUP = "asr_unavailable_startup"
    READY = "asr_ready"
    SESSION_WARMING = "asr_session_warming"
    SESSION_ACTIVE = "asr_session_active"
    SESSION_FAILED_RUNTIME = "asr_session_failed_runtime"
    COOLDOWN = "asr_cooldown"
    PROBING = "asr_probing"
    RECOVERED_PENDING_USER = "asr_recovered_pending_user"


class AsrFailureStage(str, Enum):
    STARTUP = "startup"
    SESSION = "session"
    RUNTIME = "runtime"


class AsrFailureCode(str, Enum):
    TIMEOUT = "timeout"
    MODEL_INCOMPLETE = "model_incomplete"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INITIALIZATION_FAILED = "initialization_failed"
    DEVICE_UNAVAILABLE = "device_unavailable"
    STREAM_FAILED = "stream_failed"
    DECODE_FAILED = "decode_failed"
    CANCELLED = "cancelled"


class AsrTransitionDecision(str, Enum):
    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    NOOP = "noop"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AsrFailure:
    """可展示的 ASR 故障分类；不携带异常正文、路径或堆栈。"""

    stage: AsrFailureStage
    code: AsrFailureCode
    occurred_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, AsrFailureStage):
            raise TypeError("stage 必须是 AsrFailureStage")
        if not isinstance(self.code, AsrFailureCode):
            raise TypeError("code 必须是 AsrFailureCode")
        parse_utc_timestamp("occurred_at", self.occurred_at)


@dataclass(frozen=True, slots=True)
class AsrSessionBoundary:
    """一次正式语音输入的全新边界；旧边界不得在恢复后复用。"""

    session_id: str
    audio_session_id: str
    provider_generation: int
    stream_id: str
    utterance_id: str

    def __post_init__(self) -> None:
        require_identifier("session_id", self.session_id)
        require_identifier("audio_session_id", self.audio_session_id)
        require_positive("provider_generation", self.provider_generation)
        require_identifier("stream_id", self.stream_id)
        require_identifier("utterance_id", self.utterance_id)

    @property
    def identity(self) -> tuple[str, str, int, str, str]:
        return (
            self.session_id,
            self.audio_session_id,
            self.provider_generation,
            self.stream_id,
            self.utterance_id,
        )


@dataclass(frozen=True, slots=True)
class AsrRecoveryConfig:
    required_probe_passes: int = 2

    def __post_init__(self) -> None:
        require_positive("required_probe_passes", self.required_probe_passes)


@dataclass(frozen=True, slots=True)
class AsrTransitionResult:
    decision: AsrTransitionDecision
    state: AsrRecoveryState
    input_mode: AsrInputMode
    public_status: str


_DiscardResources = Callable[[str], None]


class AsrRecoveryController:
    """显式、无定时器的 ASR 输入通道状态机。

    调用方负责实施启动期限、会话期限和低频探测调度。本控制器只接收已发生的
    成功/失败事实，因此不会自行循环、监听用户语音或抢回当前输入模式。
    """

    def __init__(
        self,
        config: AsrRecoveryConfig,
        *,
        discard_resources: _DiscardResources,
    ) -> None:
        if not isinstance(config, AsrRecoveryConfig):
            raise TypeError("config 必须是 AsrRecoveryConfig")
        if not callable(discard_resources):
            raise TypeError("discard_resources 必须可调用")
        self._config = config
        self._discard_resources = discard_resources
        self._state = AsrRecoveryState.STARTUP_LOADING
        self._input_mode = AsrInputMode.TEXT
        self._public_status = "正在准备语音识别；当前为文字输入"
        self._last_failure: AsrFailure | None = None
        self._active_boundary: AsrSessionBoundary | None = None
        self._used_boundaries: set[tuple[str, str, int, str, str]] = set()
        self._probe_passes = 0
        self._voice_switch_pending = False
        self._cleanup_failed = False

    @property
    def state(self) -> AsrRecoveryState:
        return self._state

    @property
    def config(self) -> AsrRecoveryConfig:
        """只读恢复参数；供调用方按 ``required_probe_passes`` 驱动既有探测路径。"""

        return self._config

    @property
    def input_mode(self) -> AsrInputMode:
        return self._input_mode

    @property
    def public_status(self) -> str:
        return self._public_status

    @property
    def last_failure(self) -> AsrFailure | None:
        return self._last_failure

    @property
    def active_boundary(self) -> AsrSessionBoundary | None:
        return self._active_boundary

    @property
    def voice_switch_pending(self) -> bool:
        return self._voice_switch_pending

    @property
    def consecutive_probe_passes(self) -> int:
        return self._probe_passes

    @property
    def cleanup_failed(self) -> bool:
        return self._cleanup_failed

    def startup_succeeded(self) -> AsrTransitionResult:
        if self._state is AsrRecoveryState.READY:
            return self._result(AsrTransitionDecision.NOOP)
        if self._state is not AsrRecoveryState.STARTUP_LOADING:
            return self._result(AsrTransitionDecision.REJECTED)
        self._state = AsrRecoveryState.READY
        self._input_mode = AsrInputMode.TEXT
        self._public_status = "语音识别已就绪；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def startup_failed(self, failure: AsrFailure) -> AsrTransitionResult:
        if not self._valid_failure(failure, AsrFailureStage.STARTUP):
            return self._result(AsrTransitionDecision.REJECTED)
        if self._state is AsrRecoveryState.UNAVAILABLE_STARTUP:
            return self._result(AsrTransitionDecision.NOOP)
        if self._state is not AsrRecoveryState.STARTUP_LOADING:
            return self._result(AsrTransitionDecision.REJECTED)
        self._close_voice_boundary("startup_failure")
        self._last_failure = failure
        self._state = AsrRecoveryState.UNAVAILABLE_STARTUP
        self._public_status = f"语音识别不可用：{failure.code.value}；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def begin_session_warmup(self) -> AsrTransitionResult:
        if self._state is AsrRecoveryState.SESSION_WARMING:
            return self._result(AsrTransitionDecision.NOOP)
        if self._state is not AsrRecoveryState.READY:
            return self._result(AsrTransitionDecision.REJECTED)
        self._active_boundary = None
        self._input_mode = AsrInputMode.TEXT
        self._state = AsrRecoveryState.SESSION_WARMING
        self._public_status = "正在准备语音输入；文字输入仍可用"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def session_warmup_succeeded(self, boundary: AsrSessionBoundary) -> AsrTransitionResult:
        if self._state is not AsrRecoveryState.SESSION_WARMING:
            return self._result(AsrTransitionDecision.REJECTED)
        if not isinstance(boundary, AsrSessionBoundary) or boundary.identity in self._used_boundaries:
            self._close_voice_boundary("stale_session_boundary")
            self._state = AsrRecoveryState.COOLDOWN
            self._public_status = "语音识别不可用；当前为文字输入"
            return self._result(AsrTransitionDecision.REJECTED)
        self._used_boundaries.add(boundary.identity)
        self._active_boundary = boundary
        self._state = AsrRecoveryState.SESSION_ACTIVE
        self._input_mode = AsrInputMode.VOICE
        self._public_status = "语音输入已启用"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def session_warmup_failed(self, failure: AsrFailure) -> AsrTransitionResult:
        if not self._valid_failure(failure, AsrFailureStage.SESSION):
            return self._result(AsrTransitionDecision.REJECTED)
        if self._state is not AsrRecoveryState.SESSION_WARMING:
            return self._result(AsrTransitionDecision.NOOP)
        self._close_voice_boundary("session_warmup_failure")
        self._last_failure = failure
        self._state = AsrRecoveryState.COOLDOWN
        self._public_status = f"语音识别不可用：{failure.code.value}；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def cancel_session_warmup(self) -> AsrTransitionResult:
        if self._state is not AsrRecoveryState.SESSION_WARMING:
            return self._result(AsrTransitionDecision.NOOP)
        self._close_voice_boundary("session_warmup_cancelled")
        self._last_failure = None
        self._state = AsrRecoveryState.COOLDOWN
        self._public_status = "已取消语音输入准备；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def runtime_failed(self, failure: AsrFailure) -> AsrTransitionResult:
        if not self._valid_failure(failure, AsrFailureStage.RUNTIME):
            return self._result(AsrTransitionDecision.REJECTED)
        if self._state is AsrRecoveryState.SESSION_FAILED_RUNTIME:
            return self._result(AsrTransitionDecision.NOOP)
        if self._state is not AsrRecoveryState.SESSION_ACTIVE:
            return self._result(AsrTransitionDecision.REJECTED)
        self._close_voice_boundary("runtime_failure")
        self._last_failure = failure
        self._state = AsrRecoveryState.SESSION_FAILED_RUNTIME
        self._public_status = f"语音识别不可用：{failure.code.value}；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def session_completed(self) -> AsrTransitionResult:
        """结束一轮成功语音输入，释放 active 边界并回到可重新预热状态。"""
        if self._state is not AsrRecoveryState.SESSION_ACTIVE:
            return self._result(AsrTransitionDecision.REJECTED)
        self._close_voice_boundary("session_completed")
        self._last_failure = None
        self._state = AsrRecoveryState.READY
        self._input_mode = AsrInputMode.TEXT
        self._public_status = "语音输入已完成；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def begin_probe(self) -> AsrTransitionResult:
        if self._state is AsrRecoveryState.PROBING:
            return self._result(AsrTransitionDecision.NOOP)
        if self._state not in {
            AsrRecoveryState.UNAVAILABLE_STARTUP,
            AsrRecoveryState.SESSION_FAILED_RUNTIME,
            AsrRecoveryState.COOLDOWN,
        }:
            return self._result(AsrTransitionDecision.REJECTED)
        self._probe_passes = 0
        self._state = AsrRecoveryState.PROBING
        self._input_mode = AsrInputMode.TEXT
        self._public_status = "正在重新检测语音识别；当前为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def record_probe(self, passed: bool) -> AsrTransitionResult:
        if not isinstance(passed, bool):
            return self._result(AsrTransitionDecision.REJECTED)
        if self._state is not AsrRecoveryState.PROBING:
            return self._result(AsrTransitionDecision.REJECTED)
        if not passed:
            self._probe_passes = 0
            self._state = AsrRecoveryState.COOLDOWN
            self._input_mode = AsrInputMode.TEXT
            self._public_status = "语音识别不可用；当前为文字输入"
            return self._result(AsrTransitionDecision.ACCEPTED)
        self._probe_passes += 1
        if self._probe_passes < self._config.required_probe_passes:
            return self._result(AsrTransitionDecision.ACCEPTED)
        self._state = AsrRecoveryState.RECOVERED_PENDING_USER
        self._input_mode = AsrInputMode.TEXT
        self._public_status = "语音识别已恢复，可以切回语音输入。"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def request_voice_switch(self, *, safe_idle: bool) -> AsrTransitionResult:
        if not isinstance(safe_idle, bool):
            return self._result(AsrTransitionDecision.REJECTED)
        if self._state is not AsrRecoveryState.RECOVERED_PENDING_USER:
            return self._result(AsrTransitionDecision.REJECTED)
        if not safe_idle:
            self._voice_switch_pending = True
            self._public_status = "将在当前流程结束后切回语音输入"
            return self._result(AsrTransitionDecision.DEFERRED)
        return self._prepare_user_requested_voice_switch()

    def reach_safe_idle(self) -> AsrTransitionResult:
        if not self._voice_switch_pending:
            return self._result(AsrTransitionDecision.NOOP)
        if self._state is not AsrRecoveryState.RECOVERED_PENDING_USER:
            self._voice_switch_pending = False
            return self._result(AsrTransitionDecision.REJECTED)
        return self._prepare_user_requested_voice_switch()

    def _prepare_user_requested_voice_switch(self) -> AsrTransitionResult:
        self._close_voice_boundary("voice_switch_prepare")
        self._state = AsrRecoveryState.READY
        self._input_mode = AsrInputMode.TEXT
        self._public_status = "可以重新准备语音输入；当前仍为文字输入"
        return self._result(AsrTransitionDecision.ACCEPTED)

    def _close_voice_boundary(self, reason: str) -> None:
        self._active_boundary = None
        self._input_mode = AsrInputMode.TEXT
        self._voice_switch_pending = False
        self._probe_passes = 0
        try:
            self._discard_resources(reason)
        except Exception:
            # 清理失败只形成本地诊断事实。不得泄露异常正文，也不得让语音轮次继续活跃。
            self._cleanup_failed = True

    @staticmethod
    def _valid_failure(failure: AsrFailure, stage: AsrFailureStage) -> bool:
        return isinstance(failure, AsrFailure) and failure.stage is stage

    def _result(self, decision: AsrTransitionDecision) -> AsrTransitionResult:
        return AsrTransitionResult(
            decision=decision,
            state=self._state,
            input_mode=self._input_mode,
            public_status=self._public_status,
        )


__all__ = [
    "AsrFailure",
    "AsrFailureCode",
    "AsrFailureStage",
    "AsrInputMode",
    "AsrRecoveryConfig",
    "AsrRecoveryController",
    "AsrRecoveryState",
    "AsrSessionBoundary",
    "AsrTransitionDecision",
    "AsrTransitionResult",
]
