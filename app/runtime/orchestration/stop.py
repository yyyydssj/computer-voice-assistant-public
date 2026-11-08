"""P8 批次 5：一次“停”按上下文路由到唯一动作。

优先级固定为 **执行 > 授权 > 聊天 > 空闲**：正在执行的任务最需要明确停止语义，
其次是等待用户授权的模态窗口，“聊天”只停止当前播报并继续监听。本模块只编排既有
本地能力（TTS 取消、P5 确认取消、P5 账本协作取消），不提供强杀、不自动重试、
不自动回滚、不自动续跑，也不改变任何授权或执行事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.contracts.base import require_choice, require_identifier, require_non_empty
from app.executor import CancellationDisposition, ExecutionLedgerError, ExecutionLedgerStore
from app.plans import ReviewableActionPlan
from app.safety.confirmation import ConfirmationError

STOP_REASONS = frozenset({"user_stopped", "user_cancelled", "system_aborted"})


class StopContext(str, Enum):
    EXECUTION = "execution"
    AUTHORIZATION = "authorization"
    CHAT = "chat"
    IDLE = "idle"


class StopStatus(str, Enum):
    SPEECH_STOPPED = "speech_stopped"
    SPEECH_STOP_UNCONFIRMED = "speech_stop_unconfirmed"
    CONFIRMATION_CANCELLED = "confirmation_cancelled"
    EXECUTION_STOP_REQUESTED = "execution_stop_requested"
    EXECUTION_ATOMIC_WAIT = "execution_atomic_wait"
    EXECUTION_BLOCKED_BEFORE_START = "execution_blocked_before_start"
    NOTHING_ACTIVE = "nothing_active"
    ALREADY_STOPPED = "already_stopped"


@dataclass(frozen=True, slots=True)
class StopReport:
    """一次“停”的本地结果；``resume_allowed`` 永远为假。"""

    context: StopContext
    status: StopStatus
    reason: str
    speech_stopped: bool = False
    confirmation_invalidated: bool = False
    stop_requested: bool = False
    atomic_wait_required: bool = False
    late_event_isolated: bool = False
    resume_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.context, StopContext):
            raise TypeError("context 必须是 StopContext")
        if not isinstance(self.status, StopStatus):
            raise TypeError("status 必须是 StopStatus")
        require_choice("reason", self.reason, set(STOP_REASONS))
        for name in (
            "speech_stopped",
            "confirmation_invalidated",
            "stop_requested",
            "atomic_wait_required",
            "late_event_isolated",
            "resume_allowed",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} 必须是布尔值")
        if self.resume_allowed:
            raise ValueError("停止之后永不允许自动续跑")

    def to_details(self) -> dict[str, str]:
        return {
            "context": self.context.value,
            "status": self.status.value,
            "reason": self.reason,
            "atomic_wait_required": str(self.atomic_wait_required),
            "late_event_isolated": str(self.late_event_isolated),
        }


@dataclass(frozen=True, slots=True)
class AuthorizationStopInput:
    """取消一次等待中的确认窗口所需的全部本地事实。"""

    controller: object
    confirmation_window_id: str
    session_id: str
    current_plan: ReviewableActionPlan
    current_target_state_hash: str

    def __post_init__(self) -> None:
        if not callable(getattr(self.controller, "resolve", None)):
            raise TypeError("controller 必须提供 resolve")
        require_identifier("confirmation_window_id", self.confirmation_window_id)
        require_identifier("session_id", self.session_id)
        if not isinstance(self.current_plan, ReviewableActionPlan):
            raise TypeError("current_plan 必须是 ReviewableActionPlan")
        require_non_empty(
            "current_target_state_hash", self.current_target_state_hash, maximum=128
        )


class VoiceStopRouter:
    """把“停”路由到当前唯一活动上下文，并保持所有安全事实不变。"""

    def __init__(
        self,
        *,
        clock: Callable[[], str],
        diagnostic_sink: Callable[[StopReport], None] | None = None,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._clock = clock
        self._diagnostic_sink = diagnostic_sink
        self._last_report: StopReport | None = None

    @property
    def last_report(self) -> StopReport | None:
        return self._last_report

    @staticmethod
    def resolve_context(
        *,
        execution_running: bool,
        authorization_pending: bool,
        speech_active: bool,
    ) -> StopContext:
        for name, value in (
            ("execution_running", execution_running),
            ("authorization_pending", authorization_pending),
            ("speech_active", speech_active),
        ):
            if type(value) is not bool:
                raise TypeError(f"{name} 必须是布尔值")
        if execution_running:
            return StopContext.EXECUTION
        if authorization_pending:
            return StopContext.AUTHORIZATION
        if speech_active:
            return StopContext.CHAT
        return StopContext.IDLE

    def stop_chat(self, *, stream: object, reason: str = "user_stopped") -> StopReport:
        """停止当前播报并继续监听；绝不复用旧回复、旧请求或旧授权。"""

        self._require_reason(reason)
        cancel = getattr(stream, "cancel", None)
        if not callable(cancel):
            return self._report(
                StopReport(
                    context=StopContext.CHAT,
                    status=StopStatus.NOTHING_ACTIVE,
                    reason=reason,
                )
            )
        try:
            cancel()
        except Exception:  # noqa: BLE001 - cancelling is best effort
            return self._report(
                StopReport(
                    context=StopContext.CHAT,
                    status=StopStatus.SPEECH_STOP_UNCONFIRMED,
                    reason=reason,
                )
            )
        return self._report(
            StopReport(
                context=StopContext.CHAT,
                status=StopStatus.SPEECH_STOPPED,
                reason=reason,
                speech_stopped=True,
            )
        )

    def stop_authorization(
        self,
        stop: AuthorizationStopInput,
        *,
        reason: str = "user_stopped",
        occurred_at: str | None = None,
    ) -> StopReport:
        """取消等待中的确认；停止之后不可能再产生任何授权。"""

        self._require_reason(reason)
        if not isinstance(stop, AuthorizationStopInput):
            raise TypeError("stop 必须是 AuthorizationStopInput")
        at = occurred_at if occurred_at is not None else self._now()
        try:
            resolution = stop.controller.resolve(
                confirmation_window_id=stop.confirmation_window_id,
                session_id=stop.session_id,
                event="stop",
                occurred_at=at,
                current_plan=stop.current_plan,
                current_target_state_hash=stop.current_target_state_hash,
            )
        except ConfirmationError:
            return self._report(
                StopReport(
                    context=StopContext.AUTHORIZATION,
                    status=StopStatus.ALREADY_STOPPED,
                    reason=reason,
                    confirmation_invalidated=True,
                    late_event_isolated=True,
                )
            )
        status = getattr(resolution, "status", "")
        if status not in {"cancelled", "denied", "invalidated", "expired"}:
            return self._report(
                StopReport(
                    context=StopContext.AUTHORIZATION,
                    status=StopStatus.ALREADY_STOPPED,
                    reason=reason,
                    confirmation_invalidated=True,
                    late_event_isolated=True,
                )
            )
        return self._report(
            StopReport(
                context=StopContext.AUTHORIZATION,
                status=StopStatus.CONFIRMATION_CANCELLED,
                reason=reason,
                confirmation_invalidated=True,
            )
        )

    def stop_execution(
        self, *, store: ExecutionLedgerStore, reason: str = "user_stopped"
    ) -> StopReport:
        """请求协作停止：可安全取消的立即登记，不可中断的等待原子动作返回。"""

        self._require_reason(reason)
        if not isinstance(store, ExecutionLedgerStore):
            raise TypeError("store 必须是 ExecutionLedgerStore")
        requested_at = self._now()
        try:
            request = store.request_stop(reason=reason, requested_at=requested_at)
        except ExecutionLedgerError as exc:
            if exc.code != "stop_replayed":
                raise
            return self._report(
                StopReport(
                    context=StopContext.EXECUTION,
                    status=StopStatus.ALREADY_STOPPED,
                    reason=reason,
                    stop_requested=True,
                    late_event_isolated=True,
                )
            )
        disposition = request.disposition
        if disposition is CancellationDisposition.BLOCKED_BEFORE_START:
            report = StopReport(
                context=StopContext.EXECUTION,
                status=StopStatus.EXECUTION_BLOCKED_BEFORE_START,
                reason=reason,
                stop_requested=True,
            )
        elif disposition is CancellationDisposition.COOPERATIVE_REQUESTED:
            report = StopReport(
                context=StopContext.EXECUTION,
                status=StopStatus.EXECUTION_STOP_REQUESTED,
                reason=reason,
                stop_requested=True,
            )
        else:
            # DEFERRED_UNSAFE_ATOMIC / WAIT_FOR_ATOMIC_RETURN：只延迟到安全点，绝不强杀。
            report = StopReport(
                context=StopContext.EXECUTION,
                status=StopStatus.EXECUTION_ATOMIC_WAIT,
                reason=reason,
                stop_requested=True,
                atomic_wait_required=True,
            )
        return self._report(report)

    def note_late_event(self, *, source: str, reason: str = "user_stopped") -> StopReport:
        """停止后到达的旧事件只做隔离记录，永远不恢复播放、执行或授权。"""

        self._require_reason(reason)
        require_identifier("source", source)
        return self._report(
            StopReport(
                context=StopContext.IDLE,
                status=StopStatus.ALREADY_STOPPED,
                reason=reason,
                late_event_isolated=True,
            )
        )

    def _require_reason(self, reason: str) -> None:
        if reason not in STOP_REASONS:
            raise ValueError(f"不支持的停止原因: {reason!r}")

    def _now(self) -> str:
        value = self._clock()
        require_non_empty("clock", value, maximum=64)
        return value

    def _report(self, report: StopReport) -> StopReport:
        self._last_report = report
        if self._diagnostic_sink is not None:
            self._diagnostic_sink(report)
        return report


__all__ = [
    "STOP_REASONS",
    "AuthorizationStopInput",
    "StopContext",
    "StopReport",
    "StopStatus",
    "VoiceStopRouter",
]
