"""P8 批次 3：按键闲聊语音闭环编排。

本模块只编排“语音输入管道 + 统一输入接缝 + 逐句播报”，并强制每轮独立边界、
单次提交和只播报已建立的成功回复。它不实现授权、计划、执行、验证或审计，
也不允许语音改变任何会话、任务或安全事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from app.voice_loop.boundary import (
    TURN_CLOSE_REASONS,
    VoiceInputSource,
    VoiceLoopDiagnostic,
    VoiceLoopError,
    VoiceLoopState,
    VoiceLoopStateMachine,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
    require_reason,
)
from app.voice_loop.input_seam import VoiceInputSeam

MAX_REPLY_LENGTH = 32768

TEXT_ONLY_REASON = "text_only"
"""本轮**没有**播报通道（例如 ``--tts-mode none`` 或 speaker 不可用）——不是故障。"""

SPEECH_FAILED_PREFIX = "speech_failed"
"""播报过程中**抛异常**的原因前缀；实际原因码形如 ``speech_failed:StreamingVoiceSessionError``。

真机教训（R4-B，2026-10-03）：长回复（249 字）在 `push_text_chunk`/`finish` 抛异常 →
`_speak()` 的 except 分支取消剩余语音 ⇒ 用户听到"说一半就停"，但当时**只记 `speech_failed`**，
异常类型名丢失，无法点名（短回复 133 字则 6 段全部播完）。现在如实带上**异常类型名**
（固定短码，不含正文/路径）。
"""

SPEECH_START_FAILED_PREFIX = "speech_start_failed"
"""播报通道**启动失败**的原因前缀；实际原因码形如 ``speech_start_failed:RuntimeError``。

真机教训（R4-B，2026-10-03）：`_speak()` 原来把 `start_stream()` 的异常吞成 ``text_only``，
导致真机 8 个回合的真因不可见。两者语义不同：``text_only`` 是"本轮没这条通道"，
``speech_start_failed:*`` 是"有通道但起不来"（UI 侧按失败呈现）。
"""


def _cancel_stream(stream: object | None) -> None:
    if stream is None:
        return
    cancel = getattr(stream, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:  # noqa: BLE001 - cancelling is best effort
            pass


@dataclass(frozen=True, slots=True)
class VoiceDriverTurnResult:
    """语音输入管道在一轮结束时的本地归一化事实。"""

    accepted: bool
    submitted: bool
    speech_eligible: bool = False
    speech_handled: bool = False
    reply_text: str | None = None
    reason: str = "unknown"
    accepted_text: str | None = None
    reply_chars: int | None = None
    """本轮 Agent 回复的**字数**（不可播报时也如实给出）；无结果为 ``None``。"""
    speech_skip_reason: str | None = None
    """本轮**没有可播报文本**的固定短码（如 ``no_outcome:route:task``）；可播报为 ``None``。"""
    route: str | None = None
    """本轮意图路由交接的固定短码（如 ``chat``/``task``）；未路由时为 ``None``。"""

    def __post_init__(self) -> None:
        for name in ("accepted", "submitted", "speech_eligible", "speech_handled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} 必须是布尔值")
        if self.submitted and not self.accepted:
            raise ValueError("未接纳的轮次不得声称为已提交")
        if self.speech_eligible and not (self.accepted and self.submitted):
            raise ValueError("只有已接纳且已提交的完整回复才能播报")
        if self.speech_handled and not self.accepted:
            raise ValueError("未接纳的轮次不得声称为已播报")
        if self.reply_text is not None and (
            type(self.reply_text) is not str
            or not self.reply_text.strip()
            or len(self.reply_text) > MAX_REPLY_LENGTH
        ):
            raise ValueError("reply_text 必须是非空白受限字符串或 None")
        if self.accepted_text is not None:
            if type(self.accepted_text) is not str or not self.accepted_text.strip():
                raise ValueError("accepted_text 必须是非空白字符串或 None")
            if len(self.accepted_text) > MAX_REPLY_LENGTH:
                raise ValueError("accepted_text 超出长度上限")
            if not self.accepted:
                raise ValueError("未接纳的轮次不得携带 accepted_text")
        # 诊断（2026-10-24）：三个只读事实都必须保持"固定短码 + 计数"口径，
        # 绝不允许自由文本（识别/回复正文一律不落盘）。
        if self.reply_chars is not None and (
            type(self.reply_chars) is not int
            or self.reply_chars < 0
            or self.reply_chars > MAX_REPLY_LENGTH
        ):
            raise ValueError("reply_chars 必须是非负受限整数或 None")
        if self.speech_skip_reason is not None:
            if type(self.speech_skip_reason) is not str:
                raise ValueError("speech_skip_reason 必须是固定短码或 None")
            require_reason("speech_skip_reason", self.speech_skip_reason)
        if self.route is not None:
            if type(self.route) is not str:
                raise ValueError("route 必须是固定短码或 None")
            require_reason("route", self.route)
        require_reason("reason", self.reason)


class VoiceTurnDriver(Protocol):
    """语音输入管道；负责采集、识别、闸门与向会话提交。"""

    def start(self, boundary: VoiceTurnBoundary) -> bool: ...

    def pump(self) -> int: ...

    def release(self) -> VoiceDriverTurnResult: ...

    def cancel(self, *, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class VoiceChatTurnReport:
    """一轮按键语音闭环的最终本地事实；不含识别文本、回复文本或音频。"""

    boundary: VoiceTurnBoundary | None
    state: VoiceLoopState
    accepted: bool
    submitted: bool
    spoken: bool
    reason: str

    def __post_init__(self) -> None:
        for name in ("accepted", "submitted", "spoken"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} 必须是布尔值")
        if not isinstance(self.state, VoiceLoopState):
            raise TypeError("state 必须是 VoiceLoopState")
        require_reason("reason", self.reason)


class ChatVoiceLoop:
    """按键触发的半双工闲聊闭环：开始 → 收音 → 提交 → 播报 → 回到空闲。"""

    def __init__(
        self,
        *,
        registry: VoiceTurnRegistry,
        machine: VoiceLoopStateMachine,
        seam: VoiceInputSeam,
        driver: VoiceTurnDriver,
        speaker: object | None = None,
        diagnostic_sink: Callable[[VoiceLoopDiagnostic], None] | None = None,
    ) -> None:
        if not isinstance(registry, VoiceTurnRegistry):
            raise TypeError("registry 必须是 VoiceTurnRegistry")
        if not isinstance(machine, VoiceLoopStateMachine):
            raise TypeError("machine 必须是 VoiceLoopStateMachine")
        if not isinstance(seam, VoiceInputSeam):
            raise TypeError("seam 必须是 VoiceInputSeam")
        for name in ("start", "pump", "release", "cancel"):
            if not callable(getattr(driver, name, None)):
                raise TypeError(f"driver 必须提供 {name}")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._registry = registry
        self._machine = machine
        self._seam = seam
        self._driver = driver
        self._speaker = speaker
        self._diagnostic_sink = diagnostic_sink
        self._boundary: VoiceTurnBoundary | None = None
        self._last_report: VoiceChatTurnReport | None = None

    @property
    def state(self) -> VoiceLoopState:
        return self._machine.state

    @property
    def boundary(self) -> VoiceTurnBoundary | None:
        return self._boundary

    @property
    def last_report(self) -> VoiceChatTurnReport | None:
        return self._last_report

    def start_turn(self) -> VoiceTurnBoundary | None:
        if self._boundary is not None or self._machine.state is not VoiceLoopState.IDLE:
            raise VoiceLoopError("已有进行中的语音轮次")
        boundary = self._seam.open_voice_turn()
        self._machine.begin_turn(boundary)
        self._boundary = boundary
        try:
            started = bool(self._driver.start(boundary))
        except Exception as exc:  # noqa: BLE001 - pipeline startup must fail closed
            self._abandon(reason="driver_failed", cancel_reason="cancelled")
            raise VoiceLoopError("语音输入管道启动失败") from exc
        if not started:
            self._abandon(reason="start_rejected", cancel_reason="rejected")
            return None
        return boundary

    def pump(self) -> int:
        if self._machine.state is not VoiceLoopState.CAPTURING:
            raise VoiceLoopError("只有收音状态才能推进语音帧")
        return int(self._driver.pump())

    def release_input(self) -> VoiceChatTurnReport | None:
        """按键结束或 VAD 判定说完：结束收音边界。"""

        if self._machine.state is not VoiceLoopState.CAPTURING:
            raise VoiceLoopError("只有收音状态才能结束收音")
        if self._boundary_stale():
            return self._abandon(reason="input_degraded", cancel_reason="degraded")
        self._machine.transition(VoiceLoopState.TRANSCRIBING, reason="released")
        return None

    def complete_turn(
        self, *, reply_provider: Callable[[], str | None] | None = None
    ) -> VoiceChatTurnReport:
        """结束本轮；可选 `reply_provider` 在**本轮 REPLYING 状态内**提供要播报的文本。

        P10 · T1 批次 3：任务路径（打开白名单应用）的"计划/结果/已取消"必须在本轮内播报 ——
        状态机只允许 ``REPLYING -> SPEAKING``（见 `test_illegal_transitions_fail_closed`），
        轮次之外调 `_speak` 会抛非法迁移（真机表现为：文本打印了、**一个字都没念**）。
        提供者抛错时按"没有额外回复"处理，返回的播报事实仍以真实流为准。
        """

        if self._boundary is None:
            raise VoiceLoopError("没有进行中的语音轮次")
        if self._boundary_stale():
            return self._abandon(reason="input_degraded", cancel_reason="degraded")
        state = self._machine.state
        if state is VoiceLoopState.CAPTURING:
            raise VoiceLoopError("尚未结束收音")
        if state is not VoiceLoopState.TRANSCRIBING:
            raise VoiceLoopError("轮次状态不允许提交")
        self._machine.transition(VoiceLoopState.ROUTING, reason="transcription_ready")
        boundary = self._boundary
        try:
            result = self._driver.release()
        except Exception:
            self._abandon(reason="driver_failed", cancel_reason="cancelled")
            raise
        if not isinstance(result, VoiceDriverTurnResult):
            self._abandon(reason="driver_failed", cancel_reason="cancelled")
            raise VoiceLoopError("语音管道返回了非契约结果")
        if not result.accepted or not result.submitted:
            return self._finish(
                accepted=False,
                submitted=False,
                spoken=False,
                reason="rejected" if not result.accepted else "not_submitted",
                state_reason="rejected" if not result.accepted else "not_submitted",
            )
        self._machine.transition(VoiceLoopState.REPLYING, reason="route_handled")
        spoken = False
        extra_text: str | None = None
        if reply_provider is not None:
            try:
                candidate = reply_provider()
            except Exception:  # noqa: BLE001 - 提供者故障不得改变播报事实
                candidate = None
            if isinstance(candidate, str) and candidate.strip():
                extra_text = candidate
        if extra_text is not None:
            spoken, reason = self._speak(extra_text)
        elif result.speech_handled:
            reason = "driver_spoke"
        elif result.speech_eligible and result.reply_text:
            spoken, reason = self._speak(result.reply_text)
        else:
            reason = "text_only"
        return self._finish(
            accepted=True,
            submitted=True,
            spoken=spoken,
            reason=reason,
            state_reason=reason,
            boundary=boundary,
        )

    def speak_notice(self, text: str) -> tuple[bool, str]:
        """**轮次之外**的通知播报（P10 · T1 批次 3）。

        状态机只允许从 REPLYING 进 SPEAKING，所以空闲态（例如待确认窗口过期）不能走 `_speak`。
        这里用同一个 speaker 直接开流、推文本、收尾，**不改状态机**；任何失败都 fail-safe
        （返回 False + 固定短码），绝不因为"说不出来"而中断语音链路。
        """

        if type(text) is not str or not text.strip():
            return False, "text_only"
        stream = None
        start_stream = getattr(self._speaker, "start_stream", None)
        if callable(start_stream):
            try:
                stream = start_stream()
            except Exception as exc:  # noqa: BLE001 - 通知播报是尽力而为
                return False, f"{SPEECH_START_FAILED_PREFIX}:{type(exc).__name__}"
        if stream is None:
            return False, "text_only"
        push = getattr(stream, "push_text_chunk", None)
        try:
            if callable(push):
                push(text)
            finish = getattr(stream, "finish", None)
            if callable(finish):
                finish()
        except Exception as exc:  # noqa: BLE001 - 通知播报是尽力而为
            _cancel_stream(stream)
            return False, f"{SPEECH_FAILED_PREFIX}:{type(exc).__name__}"
        record = getattr(stream, "record", None)
        if isinstance(record, dict) and record.get("finish_timed_out") is True:
            return False, "speech_truncated"
        return True, "speech_finished"

    def cancel_turn(self, *, reason: str) -> VoiceTurnBoundary:
        if self._boundary is None:
            raise VoiceLoopError("没有进行中的语音轮次")
        require_reason("reason", reason)
        if reason not in TURN_CLOSE_REASONS:
            raise VoiceLoopError("轮次取消原因不受支持")
        boundary = self._boundary
        self._cancel_driver(reason)
        if self._registry.is_active(boundary):
            self._registry.close_turn(boundary, reason=reason)
        self._boundary = None
        if self._machine.state is not VoiceLoopState.IDLE and not self._machine.is_terminal:
            self._machine.transition(VoiceLoopState.IDLE, reason=reason)
        return boundary

    def _speak(self, text: str) -> tuple[bool, str]:
        stream = None
        start_stream = getattr(self._speaker, "start_stream", None)
        start_error = ""
        if callable(start_stream):
            try:
                stream = start_stream()
            except Exception as exc:  # noqa: BLE001 - speech is optional
                stream = None
                start_error = type(exc).__name__
        if stream is None:
            if start_error:
                # R4-B 真机教训（2026-10-03）：播报通道**启动失败**与"本轮没有播报通道"
                # 必须分开记。原来统一记 `text_only`，于是真机上 8 个回合的 TTS 启动异常
                # 全被吞成"没有播报通道"，真因不可见（`voice_turn` 里只有一个 text_only）。
                # 现在如实带上**异常类型名**（固定短码，不含正文/路径），原因码仍是合法标识符。
                return False, f"{SPEECH_START_FAILED_PREFIX}:{start_error}"
            return False, "text_only"
        self._machine.transition(VoiceLoopState.SPEAKING, reason="speech_started")
        push = getattr(stream, "push_text_chunk", None)
        try:
            if callable(push):
                push(text)
            finish = getattr(stream, "finish", None)
            if callable(finish):
                finish()
        except Exception as exc:  # noqa: BLE001 - TTS failure must not change the outcome
            _cancel_stream(stream)
            self._machine.transition(VoiceLoopState.IDLE, reason="speech_failed")
            return False, f"{SPEECH_FAILED_PREFIX}:{type(exc).__name__}"
        # R4-B 真机教训（"说一半就停"）：流式会话的有界收尾可能**取消仍在播放的语音**。
        # 这时绝不能声称"完整播报"——如实记 `speech_truncated`（事实来自流自己的 record）。
        record = getattr(stream, "record", None)
        if isinstance(record, dict) and record.get("finish_timed_out") is True:
            self._machine.transition(VoiceLoopState.IDLE, reason="speech_truncated")
            return False, "speech_truncated"
        self._machine.transition(VoiceLoopState.IDLE, reason="speech_finished")
        return True, "speech_finished"

    def _abandon(self, *, reason: str, cancel_reason: str) -> VoiceChatTurnReport:
        boundary = self._boundary
        self._cancel_driver(cancel_reason)
        if boundary is not None and self._registry.is_active(boundary):
            try:
                self._registry.close_turn(boundary, reason=cancel_reason)
            except VoiceLoopError:
                pass
        self._boundary = None
        if self._machine.state is not VoiceLoopState.IDLE and not self._machine.is_terminal:
            self._machine.transition(VoiceLoopState.IDLE, reason=reason)
        return self._report(
            boundary=boundary,
            accepted=False,
            submitted=False,
            spoken=False,
            reason=reason,
        )

    def _finish(
        self,
        *,
        accepted: bool,
        submitted: bool,
        spoken: bool,
        reason: str,
        state_reason: str,
        boundary: VoiceTurnBoundary | None = None,
    ) -> VoiceChatTurnReport:
        if boundary is None:
            boundary = self._boundary
        if boundary is not None and self._registry.is_active(boundary):
            self._registry.close_turn(boundary, reason="completed" if accepted else "rejected")
        self._boundary = None
        if self._machine.state is not VoiceLoopState.IDLE and not self._machine.is_terminal:
            self._machine.transition(VoiceLoopState.IDLE, reason=state_reason)
        return self._report(
            boundary=boundary,
            accepted=accepted,
            submitted=submitted,
            spoken=spoken,
            reason=reason,
        )

    def _report(
        self,
        *,
        boundary: VoiceTurnBoundary | None,
        accepted: bool,
        submitted: bool,
        spoken: bool,
        reason: str,
    ) -> VoiceChatTurnReport:
        report = VoiceChatTurnReport(
            boundary=boundary,
            state=self._machine.state,
            accepted=accepted,
            submitted=submitted,
            spoken=spoken,
            reason=reason,
        )
        self._last_report = report
        self._emit("voice_chat_turn_reported", boundary, reason)
        return report

    def _cancel_driver(self, reason: str) -> None:
        try:
            self._driver.cancel(reason=reason)
        except Exception:  # noqa: BLE001 - cancellation is best effort
            pass

    def _boundary_stale(self) -> bool:
        active = self._registry.active
        if self._boundary is None or active is None:
            return True
        return active.identity != self._boundary.identity

    def _emit(self, event_type: str, boundary: VoiceTurnBoundary | None, reason: str) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            VoiceLoopDiagnostic(
                event_type=event_type,
                subject_id=boundary.turn_id if boundary is not None else "voice-loop",
                reason=reason,
                state=self._machine.state,
                source=VoiceInputSource.ASR if boundary is not None else None,
            )
        )


__all__ = [
    "ChatVoiceLoop",
    "MAX_REPLY_LENGTH",
    "VoiceChatTurnReport",
    "VoiceDriverTurnResult",
    "VoiceTurnDriver",
]
