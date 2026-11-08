"""P8 批次 1：按键语音轮次边界与显式状态机。

本模块只定义本地契约事实：一次语音轮次的独立标识、单轮唯一性、显式状态
迁移和脱敏诊断。它不采集音频、不调用 ASR/TTS/Agent、不读写文件、不联网，
也不创建授权、计划或执行事实。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from app.contracts.base import (
    ContractError,
    parse_utc_timestamp,
    require_choice,
    require_identifier,
)

VOICE_LOOP_SCHEMA_VERSION = 1

TURN_CLOSE_REASONS = frozenset(
    {
        "completed",
        "rejected",
        "agent_failure",
        "cancelled",
        "stopped",
        "degraded",
        "clarified",
        "shutdown",
    }
)


class VoiceLoopError(RuntimeError):
    """轮次边界或状态迁移不满足本地硬规则。"""


def require_reason(name: str, value: str) -> None:
    """原因码必须是受限标识符；不接受自由文本。"""

    try:
        require_identifier(name, value)
    except ContractError as exc:
        raise VoiceLoopError(f"{name} 必须是受限原因码") from exc


class VoiceInputSource(str, Enum):
    ASR = "asr"
    TEXT_SHELL = "text_shell"


class VoiceLoopState(str, Enum):
    IDLE = "idle"
    CAPTURING = "capturing"
    TRANSCRIBING = "transcribing"
    ROUTING = "routing"
    CLARIFYING = "clarifying"
    REPLYING = "replying"
    SPEAKING = "speaking"
    PLANNING = "planning"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class VoiceTurnBoundary:
    """一次语音/文字轮次的全部本地标识；旧轮次不得复用其中任何一个。"""

    voice_session_id: str
    utterance_id: str
    turn_id: str
    request_id: str
    reply_id: str
    source: VoiceInputSource
    created_at: str

    def __post_init__(self) -> None:
        for name in (
            "voice_session_id",
            "utterance_id",
            "turn_id",
            "request_id",
            "reply_id",
        ):
            require_identifier(name, getattr(self, name))
        require_choice(
            "source",
            self.source,
            {VoiceInputSource.ASR.value, VoiceInputSource.TEXT_SHELL.value},
        )
        object.__setattr__(self, "source", VoiceInputSource(self.source))
        parse_utc_timestamp("created_at", self.created_at)

    @property
    def identity(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.voice_session_id,
            self.utterance_id,
            self.turn_id,
            self.request_id,
            self.reply_id,
            self.source.value,
            self.created_at,
        )

    def to_trace_fields(self) -> dict[str, str]:
        """路由追踪字段视图；不包含任何音频或自由文本。"""

        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "source": self.source.value,
            "created_at": self.created_at,
        }

    def matches_trace(self, *, request_id: str, turn_id: str, source: str) -> bool:
        return (self.request_id, self.turn_id, self.source.value) == (
            request_id,
            turn_id,
            source,
        )


class VoiceTurnRegistry:
    """签发并回收语音轮次边界；同一时刻最多一个活动轮次。"""

    def __init__(
        self,
        *,
        source_instance_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        require_identifier("source_instance_id", source_instance_id)
        self._source_instance_id = source_instance_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._active: VoiceTurnBoundary | None = None
        self._retired: list[VoiceTurnBoundary] = []
        self._issued: set[tuple[str, str, str, str, str, str, str]] = set()

    @property
    def source_instance_id(self) -> str:
        return self._source_instance_id

    @property
    def active(self) -> VoiceTurnBoundary | None:
        return self._active

    @property
    def retired(self) -> tuple[VoiceTurnBoundary, ...]:
        return tuple(self._retired)

    def open_turn(
        self, *, source: str, voice_session_id: str | None = None
    ) -> VoiceTurnBoundary:
        require_choice(
            "source", source, {VoiceInputSource.ASR.value, VoiceInputSource.TEXT_SHELL.value}
        )
        if self._active is not None:
            raise VoiceLoopError("已有进行中的语音轮次，不得并行开始新轮次")
        if voice_session_id is None:
            voice_session_id = f"voice-session-{uuid.uuid4().hex}"
        else:
            require_identifier("voice_session_id", voice_session_id)
        prefix = VoiceInputSource(source).value
        while True:
            candidate = VoiceTurnBoundary(
                voice_session_id=voice_session_id,
                utterance_id=f"{prefix}-utterance-{uuid.uuid4().hex}",
                turn_id=f"{prefix}-turn-{uuid.uuid4().hex}",
                request_id=f"{prefix}-request-{uuid.uuid4().hex}",
                reply_id=f"{prefix}-reply-{uuid.uuid4().hex}",
                source=source,
                created_at=self._timestamp(self._now()),
            )
            if candidate.identity not in self._issued:
                break
        self._issued.add(candidate.identity)
        self._active = candidate
        return candidate

    def is_active(self, boundary: object) -> bool:
        return (
            isinstance(boundary, VoiceTurnBoundary)
            and self._active is not None
            and self._active.identity == boundary.identity
        )

    def is_retired(self, boundary: object) -> bool:
        return isinstance(boundary, VoiceTurnBoundary) and any(
            retired.identity == boundary.identity for retired in self._retired
        )

    def is_known(self, boundary: object) -> bool:
        return self.is_active(boundary) or self.is_retired(boundary)

    def close_turn(self, boundary: object, *, reason: str) -> VoiceTurnBoundary:
        if not isinstance(boundary, VoiceTurnBoundary):
            raise TypeError("boundary 必须是 VoiceTurnBoundary")
        require_reason("reason", reason)
        try:
            require_choice("reason", reason, TURN_CLOSE_REASONS)
        except ContractError as exc:
            raise VoiceLoopError("轮次关闭原因不受支持") from exc
        if not self.is_active(boundary):
            raise VoiceLoopError("只能关闭当前活动轮次")
        assert self._active is not None
        retired = self._active
        self._retired.append(retired)
        self._active = None
        return retired

    def cancel_all(self, *, reason: str) -> tuple[VoiceTurnBoundary, ...]:
        require_reason("reason", reason)
        try:
            require_choice("reason", reason, TURN_CLOSE_REASONS)
        except ContractError as exc:
            raise VoiceLoopError("轮次关闭原因不受支持") from exc
        if self._active is None:
            return ()
        retired = self._active
        self._retired.append(retired)
        self._active = None
        return (retired,)

    def _timestamp(self, value: datetime) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("轮次时间必须包含时区")
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )


@dataclass(frozen=True, slots=True)
class VoiceLoopDiagnostic:
    """只包含枚举化边界事实；不含识别文本、回复文本、音频或异常正文。"""

    event_type: str
    subject_id: str
    reason: str
    state: VoiceLoopState
    source: VoiceInputSource | None

    def to_details(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "state": self.state.value,
            "source": self.source.value if self.source is not None else "none",
        }


@dataclass(frozen=True, slots=True)
class VoiceLoopTransition:
    subject_id: str
    from_state: VoiceLoopState
    to_state: VoiceLoopState
    reason: str
    source: VoiceInputSource | None


_TRANSITIONS: dict[VoiceLoopState, frozenset[VoiceLoopState]] = {
    VoiceLoopState.IDLE: frozenset({VoiceLoopState.CAPTURING, VoiceLoopState.ROUTING}),
    VoiceLoopState.CAPTURING: frozenset({VoiceLoopState.TRANSCRIBING, VoiceLoopState.IDLE}),
    VoiceLoopState.TRANSCRIBING: frozenset({VoiceLoopState.ROUTING, VoiceLoopState.IDLE}),
    VoiceLoopState.ROUTING: frozenset(
        {VoiceLoopState.REPLYING, VoiceLoopState.CLARIFYING, VoiceLoopState.IDLE}
    ),
    VoiceLoopState.CLARIFYING: frozenset({VoiceLoopState.IDLE}),
    VoiceLoopState.REPLYING: frozenset({VoiceLoopState.SPEAKING, VoiceLoopState.IDLE}),
    VoiceLoopState.SPEAKING: frozenset({VoiceLoopState.IDLE}),
    # 任务路径状态先在契约中命名；对应迁移随实现该路径的批次一起开放。
    VoiceLoopState.PLANNING: frozenset(),
    VoiceLoopState.AWAITING_AUTHORIZATION: frozenset(),
    VoiceLoopState.EXECUTING: frozenset(),
    VoiceLoopState.VERIFYING: frozenset(),
    VoiceLoopState.TERMINAL: frozenset(),
}


class VoiceLoopStateMachine:
    """显式状态机：只接受已声明迁移，非法迁移一律 fail closed。"""

    def __init__(
        self,
        registry: VoiceTurnRegistry,
        *,
        diagnostic_sink: Callable[[VoiceLoopDiagnostic], None] | None = None,
    ) -> None:
        if not isinstance(registry, VoiceTurnRegistry):
            raise TypeError("registry 必须是 VoiceTurnRegistry")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._registry = registry
        self._diagnostic_sink = diagnostic_sink
        self._state = VoiceLoopState.IDLE
        self._boundary: VoiceTurnBoundary | None = None
        self._transitions: list[VoiceLoopTransition] = []

    @property
    def state(self) -> VoiceLoopState:
        return self._state

    @property
    def boundary(self) -> VoiceTurnBoundary | None:
        return self._boundary

    @property
    def transitions(self) -> tuple[VoiceLoopTransition, ...]:
        return tuple(self._transitions)

    @property
    def is_terminal(self) -> bool:
        return self._state is VoiceLoopState.TERMINAL

    def owns(self, boundary: object) -> bool:
        return (
            isinstance(boundary, VoiceTurnBoundary)
            and self._boundary is not None
            and self._boundary.identity == boundary.identity
        )

    def begin_turn(self, boundary: object) -> VoiceLoopTransition:
        if not isinstance(boundary, VoiceTurnBoundary):
            raise TypeError("boundary 必须是 VoiceTurnBoundary")
        if self._state is not VoiceLoopState.IDLE:
            raise VoiceLoopError("只有空闲状态才能开始新的语音轮次")
        if not self._registry.is_active(boundary):
            raise VoiceLoopError("轮次边界不是当前活动边界")
        target = (
            VoiceLoopState.CAPTURING
            if boundary.source is VoiceInputSource.ASR
            else VoiceLoopState.ROUTING
        )
        self._boundary = boundary
        return self._apply(target, reason="turn_started")

    def transition(self, target: VoiceLoopState, *, reason: str) -> VoiceLoopTransition:
        if not isinstance(target, VoiceLoopState):
            raise TypeError("target 必须是 VoiceLoopState")
        if self._state is VoiceLoopState.TERMINAL:
            raise VoiceLoopError("状态机已终止，不得继续迁移")
        if target is VoiceLoopState.TERMINAL:
            raise VoiceLoopError("终止必须经过 terminate()")
        if target is self._state:
            raise VoiceLoopError("不允许自迁移")
        if target not in _TRANSITIONS[self._state]:
            raise VoiceLoopError(f"不允许从 {self._state.value} 迁移到 {target.value}")
        return self._apply(target, reason=reason)

    def terminate(self, *, reason: str) -> VoiceLoopTransition:
        if self._state is VoiceLoopState.TERMINAL:
            raise VoiceLoopError("状态机已经终止")
        return self._apply(VoiceLoopState.TERMINAL, reason=reason)

    def _apply(self, target: VoiceLoopState, *, reason: str) -> VoiceLoopTransition:
        require_reason("reason", reason)
        boundary = self._boundary
        transition = VoiceLoopTransition(
            subject_id=boundary.turn_id if boundary is not None else "voice-loop",
            from_state=self._state,
            to_state=target,
            reason=reason,
            source=boundary.source if boundary is not None else None,
        )
        self._transitions.append(transition)
        self._state = target
        if target is VoiceLoopState.IDLE:
            self._boundary = None
        self._emit(transition)
        return transition

    def _emit(self, transition: VoiceLoopTransition) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            VoiceLoopDiagnostic(
                event_type="voice_loop_transition",
                subject_id=transition.subject_id,
                reason=transition.reason,
                state=transition.to_state,
                source=transition.source,
            )
        )


__all__ = [
    "TURN_CLOSE_REASONS",
    "VOICE_LOOP_SCHEMA_VERSION",
    "VoiceInputSource",
    "VoiceLoopDiagnostic",
    "VoiceLoopError",
    "VoiceLoopState",
    "VoiceLoopStateMachine",
    "VoiceLoopTransition",
    "VoiceTurnBoundary",
    "VoiceTurnRegistry",
    "require_reason",
]
