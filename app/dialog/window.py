"""P9 批次 1：连续对话窗口与闲置/超时/轮数边界。

边界：本模块只维护本地“连续对话窗口”的开合、轮数与超时事实；它不决定授权、
不接触任务/计划/授权/执行/结果事实、不读写文件、不联网、不创建线程。时间判定只
使用注入时钟（未注入时才读真实 UTC 时间），已关闭的窗口一律不得复活，也不得改写
既有关闭原因。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from app.contracts.base import require_identifier, require_positive

from app.dialog.state import DialogDiagnostic, DialogError, DialogState


@dataclass(frozen=True, slots=True)
class DialogWindowConfig:
    """连续对话窗口的硬上限；全部数值必须为正，任何越界都 fail closed。

    ``listen_timeout_seconds``（一次 LISTENING 内等待语音起始的上限）为 P9 B3 唤醒词
    与连续对话接线保留：本批次只校验取值范围，当前没有任何消费方，不得在此自行接线。
    """

    idle_timeout_seconds: int = 300
    max_turns: int = 8
    max_window_seconds: int = 1800
    listen_timeout_seconds: int = 8

    def __post_init__(self) -> None:
        require_positive("idle_timeout_seconds", self.idle_timeout_seconds)
        require_positive("max_turns", self.max_turns)
        require_positive("max_window_seconds", self.max_window_seconds)
        require_positive("listen_timeout_seconds", self.listen_timeout_seconds)


class DialogWindowState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class DialogWindowCloseReason(str, Enum):
    IDLE_TIMEOUT = "idle_timeout"
    MAX_TURNS = "max_turns"
    MAX_WINDOW_DURATION = "max_window_duration"
    USER_CANCELLED = "user_cancelled"
    FAIL_CLOSED = "fail_closed"
    SHUTDOWN = "shutdown"


# 只有自动失效（超时/超限）才算 expired；用户取消、fail closed、关停不算。
_EXPIRING_REASONS = frozenset(
    {
        DialogWindowCloseReason.IDLE_TIMEOUT,
        DialogWindowCloseReason.MAX_WINDOW_DURATION,
        DialogWindowCloseReason.MAX_TURNS,
    }
)


@dataclass(frozen=True, slots=True)
class DialogWindowStatus:
    window_id: str
    state: DialogWindowState
    turn_count: int
    close_reason: DialogWindowCloseReason | None
    expired: bool

    def to_details(self) -> dict[str, str]:
        return {
            "window_id": self.window_id,
            "state": self.state.value,
            "turn_count": str(self.turn_count),
            "close_reason": self.close_reason.value if self.close_reason is not None else "none",
            "expired": "true" if self.expired else "false",
        }


def _new_window_id() -> str:
    return f"dialog-window-{uuid.uuid4().hex}"


class DialogWindow:
    """连续对话窗口：新实例为 CLOSED，必须显式开始会话才能接受输入。"""

    def __init__(
        self,
        *,
        config: DialogWindowConfig,
        now: Callable[[], datetime] | None = None,
        window_id: str | None = None,
        diagnostic_sink: Callable[[DialogDiagnostic], None] | None = None,
    ) -> None:
        if not isinstance(config, DialogWindowConfig):
            raise TypeError("config 必须是 DialogWindowConfig")
        if now is not None and not callable(now):
            raise TypeError("now 必须可调用")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        if window_id is None:
            window_id = _new_window_id()
        else:
            require_identifier("window_id", window_id)
        self._config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._diagnostic_sink = diagnostic_sink
        self._window_id = window_id
        self._state = DialogWindowState.CLOSED
        self._turn_count = 0
        self._close_reason: DialogWindowCloseReason | None = None
        self._opened_at = self._read_now()
        self._last_activity = self._opened_at

    @property
    def config(self) -> DialogWindowConfig:
        return self._config

    @property
    def window_id(self) -> str:
        return self._window_id

    @property
    def status(self) -> DialogWindowStatus:
        return self._status()

    @property
    def state(self) -> DialogWindowState:
        return self._state

    @property
    def close_reason(self) -> DialogWindowCloseReason | None:
        return self._close_reason

    def is_open(self) -> bool:
        return self._state is DialogWindowState.OPEN

    def open(self) -> DialogWindowStatus:
        """开始一段新的连续对话会话；每次成功调用都必须产生新的 window_id。"""

        if self._state is DialogWindowState.OPEN:
            raise DialogError("窗口已打开，不得重复打开")
        self._window_id = self._issue_window_id()
        self._state = DialogWindowState.OPEN
        self._turn_count = 0
        self._close_reason = None
        self._opened_at = self._read_now()
        self._last_activity = self._opened_at
        self._emit("dialog_window_opened", "listening_started")
        return self._status()

    def note_activity(self) -> DialogWindowStatus:
        """刷新闲置计时；已关闭的窗口只返回关闭状态，绝不复活。"""

        if self._state is DialogWindowState.CLOSED:
            return self._status()
        self._last_activity = self._read_now()
        return self._status()

    def begin_turn(self) -> DialogWindowStatus:
        """开始新一轮；超过 max_turns 立即关闭且 turn_count 不得超过上限。"""

        if self._state is DialogWindowState.CLOSED:
            return self._status()
        if self._turn_count + 1 > self._config.max_turns:
            return self.close(reason=DialogWindowCloseReason.MAX_TURNS)
        self._turn_count += 1
        self._last_activity = self._read_now()
        return self._status()

    def end_turn(self) -> DialogWindowStatus:
        if self._state is DialogWindowState.CLOSED:
            return self._status()
        self._last_activity = self._read_now()
        return self._status()

    def check_timeouts(self) -> DialogWindowStatus:
        """同时判定整段窗口上限与闲置超时；判定顺序固定为整段上限优先。"""

        if self._state is DialogWindowState.CLOSED:
            return self._status()
        current = self._read_now()
        if self._elapsed(current, self._opened_at) >= self._config.max_window_seconds:
            return self.close(reason=DialogWindowCloseReason.MAX_WINDOW_DURATION)
        if self._elapsed(current, self._last_activity) >= self._config.idle_timeout_seconds:
            return self.close(reason=DialogWindowCloseReason.IDLE_TIMEOUT)
        return self._status()

    def close(self, *, reason: DialogWindowCloseReason) -> DialogWindowStatus:
        """关闭窗口；已关闭时幂等返回且不改写既有 close_reason。

        ``reason`` 必须是 ``DialogWindowCloseReason`` 实例，不做任何字符串强转：
        字符串（含等值枚举字符串如 ``"idle_timeout"`` 与自由文本）一律抛
        ``DialogError``；其它非实例类型抛 ``TypeError``。
        """

        if not isinstance(reason, DialogWindowCloseReason):
            if isinstance(reason, str):
                raise DialogError("关闭原因必须是 DialogWindowCloseReason 实例，不接受字符串原因码")
            raise TypeError("reason 必须是 DialogWindowCloseReason 实例")
        if self._state is DialogWindowState.CLOSED:
            return self._status()
        self._state = DialogWindowState.CLOSED
        self._close_reason = reason
        self._emit("dialog_window_closed", reason.value)
        return self._status()

    def _issue_window_id(self) -> str:
        while True:
            candidate = _new_window_id()
            if candidate != self._window_id:
                return candidate

    def _read_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("窗口时间必须包含时区")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _elapsed(current: datetime, since: datetime) -> float:
        return (current - since).total_seconds()

    def _status(self) -> DialogWindowStatus:
        return DialogWindowStatus(
            window_id=self._window_id,
            state=self._state,
            turn_count=self._turn_count,
            close_reason=self._close_reason,
            expired=self._state is DialogWindowState.CLOSED
            and self._close_reason in _EXPIRING_REASONS,
        )

    def _emit(self, event_type: str, reason: str) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            DialogDiagnostic(
                event_type=event_type,
                subject_id=self._window_id,
                reason=reason,
                state=DialogState.CLOSED
                if self._state is DialogWindowState.CLOSED
                else DialogState.IDLE,
                mode=None,
            )
        )


__all__ = [
    "DialogWindow",
    "DialogWindowCloseReason",
    "DialogWindowConfig",
    "DialogWindowState",
    "DialogWindowStatus",
]
