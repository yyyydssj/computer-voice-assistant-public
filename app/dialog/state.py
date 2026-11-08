"""P9 批次 1：对话状态、迁移白名单与显式状态机。

边界：本模块只维护本地对话状态事实（状态、受限原因码、脱敏迁移记录）。它不决定
授权、不接触任务/计划/授权/执行/结果事实、不读写文件、不联网、不创建线程，也不
采集或保存任何音频与自由文本。实现规则全部 fail closed：未声明的迁移一律拒绝。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.contracts.base import ContractError, require_identifier

DIALOG_SCHEMA_VERSION = 1

DIALOG_TRANSITION_REASONS: frozenset[str] = frozenset(
    {
        "turn_started",
        "voice_turn_started",
        "text_turn_started",
        "listening_started",
        "speech_started",
        "speech_finished",
        "transcription_ready",
        "route_handled",
        "clarify_required",
        "reply_ready",
        "authorization_required",
        "execution_requested",
        "speech_started_output",
        "speech_finished_output",
        "turn_completed",
        "idle_timeout",
        "window_timeout",
        "cancelled",
        "stopped",
        "input_degraded",
        "audio_failed",
        "asr_failed",
        "agent_failed",
        "provider_unavailable",
        "user_requested_text",
        "recovered",
        "shutdown",
    }
)


class DialogState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    CLARIFYING = "clarifying"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    FAIL_CLOSED = "fail_closed"
    CLOSED = "closed"


class DialogInputMode(str, Enum):
    PASSIVE = "passive"
    VOICE_TURN = "voice_turn"
    TEXT = "text"


class DialogError(RuntimeError):
    """对话边界或状态迁移不满足本地硬规则。"""


def require_transition_reason(name: str, value: str) -> str:
    """原因码必须是受限标识符且属于迁移原因白名单；不接受自由文本。"""

    try:
        require_identifier(name, value)
    except ContractError as exc:
        raise DialogError(f"{name} 必须是受限原因码") from exc
    if value not in DIALOG_TRANSITION_REASONS:
        raise DialogError(f"{name} 不在对话迁移原因白名单内")
    return value


def _new_window_id() -> str:
    return f"dialog-window-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class DialogDiagnostic:
    """只包含枚举化对话边界事实；不含识别文本、回复文本、音频或异常正文。"""

    event_type: str
    subject_id: str
    reason: str
    state: DialogState
    mode: DialogInputMode | None

    def to_details(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "state": self.state.value,
            "mode": self.mode.value if self.mode is not None else "none",
        }


@dataclass(frozen=True, slots=True)
class DialogTransition:
    subject_id: str
    from_state: DialogState
    to_state: DialogState
    reason: str
    mode: DialogInputMode | None


# 迁移白名单（§2.1）：未列出的迁移一律抛 DialogError。
_TRANSITIONS: dict[DialogState, frozenset[DialogState]] = {
    DialogState.IDLE: frozenset(
        {
            DialogState.LISTENING,
            DialogState.USER_SPEAKING,
            DialogState.THINKING,
            DialogState.FAIL_CLOSED,
            DialogState.CLOSED,
        }
    ),
    DialogState.LISTENING: frozenset(
        {
            DialogState.USER_SPEAKING,
            DialogState.THINKING,
            DialogState.IDLE,
            DialogState.FAIL_CLOSED,
            DialogState.CLOSED,
        }
    ),
    DialogState.USER_SPEAKING: frozenset(
        {DialogState.TRANSCRIBING, DialogState.IDLE, DialogState.FAIL_CLOSED, DialogState.CLOSED}
    ),
    DialogState.TRANSCRIBING: frozenset(
        {
            DialogState.THINKING,
            DialogState.CLARIFYING,
            DialogState.IDLE,
            DialogState.FAIL_CLOSED,
            DialogState.CLOSED,
        }
    ),
    DialogState.THINKING: frozenset(
        {
            DialogState.SPEAKING,
            DialogState.CLARIFYING,
            DialogState.AWAITING_AUTHORIZATION,
            DialogState.EXECUTING,
            DialogState.IDLE,
            DialogState.FAIL_CLOSED,
            DialogState.CLOSED,
        }
    ),
    DialogState.CLARIFYING: frozenset(
        {
            DialogState.LISTENING,
            DialogState.USER_SPEAKING,
            DialogState.IDLE,
            DialogState.FAIL_CLOSED,
            DialogState.CLOSED,
        }
    ),
    # 授权等待与执行期间只允许取消/恢复；不得自行继续。
    DialogState.AWAITING_AUTHORIZATION: frozenset(
        {DialogState.IDLE, DialogState.FAIL_CLOSED, DialogState.CLOSED}
    ),
    # 执行事实由 P5 链负责；这里只挡输入。
    DialogState.EXECUTING: frozenset(
        {DialogState.IDLE, DialogState.FAIL_CLOSED, DialogState.CLOSED}
    ),
    DialogState.SPEAKING: frozenset(
        {
            DialogState.LISTENING,
            DialogState.IDLE,
            DialogState.FAIL_CLOSED,
            DialogState.CLOSED,
        }
    ),
    DialogState.FAIL_CLOSED: frozenset({DialogState.IDLE, DialogState.CLOSED}),
    DialogState.CLOSED: frozenset(),
}

_ACCEPTING_STATES = frozenset({DialogState.IDLE, DialogState.LISTENING})
_MODE_CLEARING_STATES = frozenset(
    {DialogState.IDLE, DialogState.FAIL_CLOSED, DialogState.CLOSED}
)


class DialogStateMachine:
    """显式对话状态机：只接受白名单迁移，非法迁移一律 fail closed。"""

    def __init__(
        self,
        *,
        source_instance_id: str,
        window_id: str | None = None,
        diagnostic_sink: Callable[[DialogDiagnostic], None] | None = None,
    ) -> None:
        require_identifier("source_instance_id", source_instance_id)
        if window_id is None:
            window_id = _new_window_id()
        else:
            require_identifier("window_id", window_id)
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._source_instance_id = source_instance_id
        self._window_id = window_id
        self._diagnostic_sink = diagnostic_sink
        self._state = DialogState.IDLE
        self._mode: DialogInputMode | None = None
        self._transitions: list[DialogTransition] = []

    @property
    def state(self) -> DialogState:
        return self._state

    @property
    def mode(self) -> DialogInputMode | None:
        return self._mode

    @property
    def window_id(self) -> str:
        return self._window_id

    @property
    def source_instance_id(self) -> str:
        return self._source_instance_id

    @property
    def transitions(self) -> tuple[DialogTransition, ...]:
        return tuple(self._transitions)

    @property
    def is_terminal(self) -> bool:
        return self._state is DialogState.CLOSED

    @property
    def accepts_input(self) -> bool:
        """唯一权威定义：只有 IDLE / LISTENING 接受输入，且 CLOSED 永不包括在内。"""

        return self._state in _ACCEPTING_STATES and self._state is not DialogState.CLOSED

    def transition(self, target: DialogState, *, reason: str) -> DialogTransition:
        if not isinstance(target, DialogState):
            raise TypeError("target 必须是 DialogState")
        require_transition_reason("reason", reason)
        if self._state is DialogState.CLOSED:
            raise DialogError("状态机已关闭，不得继续迁移")
        if target is self._state:
            raise DialogError("不允许自迁移")
        if target not in _TRANSITIONS[self._state]:
            raise DialogError(f"不允许从 {self._state.value} 迁移到 {target.value}")
        return self._apply(target, reason=reason)

    def begin_voice_turn(self, *, reason: str = "voice_turn_started") -> DialogTransition:
        if self._state is not DialogState.IDLE:
            raise DialogError("只有空闲状态才能开始语音轮次")
        return self._apply(
            DialogState.USER_SPEAKING, reason=reason, mode=DialogInputMode.VOICE_TURN
        )

    def begin_text_turn(self, *, reason: str = "text_turn_started") -> DialogTransition:
        if self._state is not DialogState.IDLE:
            raise DialogError("只有空闲状态才能开始文字轮次")
        return self._apply(DialogState.THINKING, reason=reason, mode=DialogInputMode.TEXT)

    def fail_closed(self, *, reason: str) -> DialogTransition:
        if self._state is DialogState.CLOSED:
            raise DialogError("状态机已关闭")
        if self._state is DialogState.FAIL_CLOSED:
            raise DialogError("状态机已处于 FAIL_CLOSED")
        return self._apply(DialogState.FAIL_CLOSED, reason=reason)

    def recover(self, *, reason: str) -> DialogTransition:
        """FAIL_CLOSED -> IDLE；恢复必须换新 window_id，不得复用旧窗口身份。"""

        if self._state is not DialogState.FAIL_CLOSED:
            raise DialogError("只有 FAIL_CLOSED 状态才能恢复")
        require_transition_reason("reason", reason)
        self._window_id = self._issue_window_id()
        return self._apply(DialogState.IDLE, reason=reason)

    def close(self, *, reason: str) -> DialogTransition:
        if self._state is DialogState.CLOSED:
            raise DialogError("状态机已经关闭")
        return self._apply(DialogState.CLOSED, reason=reason)

    def _issue_window_id(self) -> str:
        while True:
            candidate = _new_window_id()
            if candidate != self._window_id:
                return candidate

    def _resolve_mode(
        self, target: DialogState, mode: DialogInputMode | None
    ) -> DialogInputMode | None:
        if mode is not None:
            return mode
        if target in _MODE_CLEARING_STATES:
            return None
        return self._mode

    def _apply(
        self,
        target: DialogState,
        *,
        reason: str,
        mode: DialogInputMode | None = None,
    ) -> DialogTransition:
        require_transition_reason("reason", reason)
        resolved_mode = self._resolve_mode(target, mode)
        transition = DialogTransition(
            subject_id=self._window_id,
            from_state=self._state,
            to_state=target,
            reason=reason,
            mode=resolved_mode,
        )
        self._transitions.append(transition)
        self._state = target
        self._mode = resolved_mode
        self._emit(transition)
        return transition

    def _emit(self, transition: DialogTransition) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            DialogDiagnostic(
                event_type="dialog_transition",
                subject_id=transition.subject_id,
                reason=transition.reason,
                state=transition.to_state,
                mode=transition.mode,
            )
        )


__all__ = [
    "DIALOG_SCHEMA_VERSION",
    "DIALOG_TRANSITION_REASONS",
    "DialogDiagnostic",
    "DialogError",
    "DialogInputMode",
    "DialogState",
    "DialogStateMachine",
    "DialogTransition",
    "require_transition_reason",
]
