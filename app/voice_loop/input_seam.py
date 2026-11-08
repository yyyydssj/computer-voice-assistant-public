"""P8 批次 2：统一输入接缝与文字降级。

TextShell 与 ASR accepted final 都必须经过同一个 ``ConversationManager`` 入口、
同一个轮次边界和同一套安全链；本模块只决定“当前输入通道”和“是否允许提交”，
不创建授权、计划、执行或审计事实，也不得在任何情况下降低授权等级。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from app.conversation.manager import ConversationRouteResult
from app.voice_loop.boundary import (
    VoiceInputSource,
    VoiceLoopError,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
    require_reason,
)

MAX_TEXT_LENGTH = 32768

DEGRADE_REASONS = frozenset(
    {
        "asr_unavailable",
        "asr_startup_failed",
        "asr_session_failed",
        "asr_stream_failed",
        "provider_unavailable",
        "cleanup_failed",
        "user_request",
    }
)

SWITCH_REFUSAL_REASONS = frozenset(
    {
        "already_voice",
        "turn_in_progress",
        "conversation_busy",
        "voice_not_available",
        "local_check_failed",
    }
)

_STATUS_VOICE = "当前为语音输入"
_STATUS_TEXT_READY = "语音识别已就绪；当前为文字输入，需用户明确请求后切回"
_STATUS_TEXT_UNAVAILABLE = "语音识别不可用；当前为文字输入"


class VoiceInputMode(str, Enum):
    VOICE = "voice"
    TEXT = "text"


class UnifiedConversation(Protocol):
    """两种输入通道唯一允许的会话入口；不暴露任何授权或执行能力。"""

    def submit_text(
        self,
        text: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ConversationRouteResult: ...

    def submit_asr_final(
        self,
        text: str,
        *,
        turn_id: str,
        request_id: str | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ConversationRouteResult: ...


@dataclass(frozen=True, slots=True)
class VoiceInputDiagnostic:
    """只包含通道与原因码；不含识别文本、回复文本或异常正文。"""

    event_type: str
    subject_id: str
    reason: str
    mode: VoiceInputMode
    source: VoiceInputSource | None

    def to_details(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "mode": self.mode.value,
            "source": self.source.value if self.source is not None else "none",
        }


@dataclass(frozen=True, slots=True)
class VoiceSwitchResult:
    switched: bool
    reason: str
    generation: int

    def __post_init__(self) -> None:
        if type(self.switched) is not bool:
            raise TypeError("switched 必须是布尔值")
        require_reason("reason", self.reason)
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 0:
            raise ValueError("generation 必须是非负整数")


@dataclass(frozen=True, slots=True)
class VoiceInputSubmission:
    """一次已提交或已拒绝的输入；拒绝时不携带会话结果。"""

    boundary: VoiceTurnBoundary
    mode: VoiceInputMode
    result: ConversationRouteResult | None = None
    refusal: str | None = None

    @property
    def submitted(self) -> bool:
        return self.result is not None and self.refusal is None


class VoiceInputSeam:
    """决定当前输入通道，并把每次输入绑定到一个全新轮次边界。"""

    def __init__(
        self,
        *,
        conversation: UnifiedConversation,
        registry: VoiceTurnRegistry,
        busy_probe: Callable[[], bool] | None = None,
        diagnostic_sink: Callable[[VoiceInputDiagnostic], None] | None = None,
    ) -> None:
        if not isinstance(registry, VoiceTurnRegistry):
            raise TypeError("registry 必须是 VoiceTurnRegistry")
        for name in ("submit_text", "submit_asr_final"):
            if not callable(getattr(conversation, name, None)):
                raise TypeError(f"conversation 必须提供 {name}")
        if busy_probe is not None and not callable(busy_probe):
            raise TypeError("busy_probe 必须可调用")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._conversation = conversation
        self._registry = registry
        self._busy_probe = busy_probe
        self._diagnostic_sink = diagnostic_sink
        self._mode = VoiceInputMode.TEXT
        self._degraded_reason: str | None = None
        self._voice_generation = 0
        self._voice_available = False
        self._submitted: set[tuple[str, str, str, str, str, str, str]] = set()

    @property
    def mode(self) -> VoiceInputMode:
        return self._mode

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def voice_generation(self) -> int:
        return self._voice_generation

    @property
    def voice_available(self) -> bool:
        return self._voice_available

    @property
    def status_text(self) -> str:
        if self._mode is VoiceInputMode.VOICE:
            return _STATUS_VOICE
        if self._degraded_reason is not None:
            return _STATUS_TEXT_UNAVAILABLE
        if self._voice_available:
            return _STATUS_TEXT_READY
        return _STATUS_TEXT_UNAVAILABLE

    def note_voice_ready(self) -> None:
        """只记录语音可用事实；绝不自动切回语音通道。"""

        self._voice_available = True

    def degrade(self, *, reason: str) -> tuple[VoiceTurnBoundary, ...]:
        """进入文字输入：只关闭未完成的语音输入轮次，不触碰授权或执行事实。"""

        require_reason("reason", reason)
        if reason not in DEGRADE_REASONS:
            raise VoiceLoopError("降级原因不受支持")
        retired: tuple[VoiceTurnBoundary, ...] = ()
        if self._registry.active is not None:
            retired = self._registry.cancel_all(reason="degraded")
        self._mode = VoiceInputMode.TEXT
        self._degraded_reason = reason
        self._emit("voice_input_degraded", retired[0] if retired else None, reason)
        return retired

    def request_voice_switch(self, *, local_check_passed: bool) -> VoiceSwitchResult:
        """只有用户明确请求、本机联合检查通过且处于安全空闲点时才切回语音。"""

        if type(local_check_passed) is not bool:
            raise TypeError("local_check_passed 必须是布尔值")
        if self._mode is VoiceInputMode.VOICE:
            return self._refuse("already_voice")
        if self._registry.active is not None:
            return self._refuse("turn_in_progress")
        if self._busy():
            return self._refuse("conversation_busy")
        if not self._voice_available:
            return self._refuse("voice_not_available")
        if not local_check_passed:
            return self._refuse("local_check_failed")
        self._voice_generation += 1
        self._mode = VoiceInputMode.VOICE
        self._degraded_reason = None
        self._emit("voice_input_switched", None, "switched")
        return VoiceSwitchResult(switched=True, reason="switched", generation=self._voice_generation)

    def open_text_turn(self) -> VoiceTurnBoundary:
        if self._mode is not VoiceInputMode.TEXT:
            raise VoiceLoopError("当前为语音输入，不得开启文字轮次")
        return self._registry.open_turn(source=VoiceInputSource.TEXT_SHELL.value)

    def open_voice_turn(self) -> VoiceTurnBoundary:
        if self._mode is not VoiceInputMode.VOICE:
            raise VoiceLoopError("当前为文字输入，不得开启语音轮次")
        return self._registry.open_turn(source=VoiceInputSource.ASR.value)

    def close_voice_turn(self, boundary: object, *, reason: str) -> VoiceTurnBoundary:
        """语音管道自己完成提交后，由接缝统一回收该轮次边界。

        本方法只做身份与通道校验和边界回收，不执行第二次提交，也不产生任何
        会话请求。
        """

        if not isinstance(boundary, VoiceTurnBoundary):
            raise TypeError("boundary 必须是 VoiceTurnBoundary")
        if boundary.source is not VoiceInputSource.ASR:
            raise VoiceLoopError("只能回收语音轮次边界")
        if self._mode is not VoiceInputMode.VOICE:
            raise VoiceLoopError("当前为文字输入，不得回收语音轮次")
        if not self._registry.is_active(boundary):
            raise VoiceLoopError("轮次边界不是当前活动边界")
        closed = self._registry.close_turn(boundary, reason=reason)
        self._emit("voice_input_turn_closed", closed, reason)
        return closed

    def submit(self, boundary: object, text: str) -> VoiceInputSubmission:
        if not isinstance(boundary, VoiceTurnBoundary):
            raise TypeError("boundary 必须是 VoiceTurnBoundary")
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        if not text.strip() or len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"text 必须是 1..{MAX_TEXT_LENGTH} 个非空白字符")
        if boundary.identity in self._submitted:
            raise VoiceLoopError("同一语音轮次不得重复提交")
        if not self._registry.is_active(boundary):
            raise VoiceLoopError("轮次边界不是当前活动边界")
        expected = (
            VoiceInputMode.VOICE
            if boundary.source is VoiceInputSource.ASR
            else VoiceInputMode.TEXT
        )
        if self._mode is not expected:
            raise VoiceLoopError("输入通道与轮次来源不一致")
        self._submitted.add(boundary.identity)
        if boundary.source is VoiceInputSource.ASR:
            result = self._conversation.submit_asr_final(
                text,
                turn_id=boundary.turn_id,
                request_id=boundary.request_id,
            )
        else:
            result = self._conversation.submit_text(
                text,
                request_id=boundary.request_id,
                turn_id=boundary.turn_id,
            )
        if not isinstance(result, ConversationRouteResult):
            raise VoiceLoopError("会话入口返回了非契约结果")
        close_reason = (
            "clarified"
            if result.handoff.route == "ambiguous"
            else "completed"
            if result.outcome is not None and result.outcome.completed
            else "rejected"
        )
        if self._registry.is_active(boundary):
            self._registry.close_turn(boundary, reason=close_reason)
        self._emit("voice_input_submitted", boundary, close_reason)
        return VoiceInputSubmission(boundary=boundary, mode=self._mode, result=result)

    def _refuse(self, reason: str) -> VoiceSwitchResult:
        self._emit("voice_input_switch_refused", None, reason)
        return VoiceSwitchResult(switched=False, reason=reason, generation=self._voice_generation)

    def _busy(self) -> bool:
        if self._busy_probe is None:
            return False
        return bool(self._busy_probe())

    def _emit(self, event_type: str, boundary: VoiceTurnBoundary | None, reason: str) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            VoiceInputDiagnostic(
                event_type=event_type,
                subject_id=boundary.turn_id if boundary is not None else "voice-loop",
                reason=reason,
                mode=self._mode,
                source=boundary.source if boundary is not None else None,
            )
        )


__all__ = [
    "DEGRADE_REASONS",
    "MAX_TEXT_LENGTH",
    "SWITCH_REFUSAL_REASONS",
    "UnifiedConversation",
    "VoiceInputDiagnostic",
    "VoiceInputMode",
    "VoiceInputSeam",
    "VoiceInputSubmission",
    "VoiceSwitchResult",
]
