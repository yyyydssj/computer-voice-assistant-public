"""P8 批次 3：把既有 P6 按键语音管道适配成闭环驱动。

适配器只做身份映射与结果归一化：它把 P8 轮次边界映射成 P6 `AsrSessionBoundary`，
并把 P6 的闸门/终态结果翻译成 ``VoiceDriverTurnResult``。它不采集音频、不联网、
不创建授权或执行事实；语音输出由闭环决定，除非显式声明由管道自行播报。
"""

from __future__ import annotations

from app.agent.adapter import AgentTurnOutcome
from app.asr.recovery import AsrFailureCode, AsrSessionBoundary
from app.asr.result_gate import ResultGateDecision
from app.contracts.base import ContractError, require_identifier, require_positive
from app.voice_loop.boundary import VoiceLoopError, VoiceTurnBoundary
from app.voice_loop.chat_loop import VoiceDriverTurnResult

_COORDINATOR_METHODS = ("start", "pump", "release", "runtime_failed")


def speech_eligible(outcome: object) -> bool:
    """只有完整、无故障且带有效终止原因的回复才允许播报。

    该规则与 P6 ``_speech_eligible`` 保持一致，并由专项测试交叉校验。
    """

    return (
        isinstance(outcome, AgentTurnOutcome)
        and outcome.completed
        and outcome.failure is None
        and type(outcome.text) is str
        and bool(outcome.text.strip())
        and type(outcome.finish_reason) is str
        and bool(outcome.finish_reason.strip())
    )


def route_code(value: object) -> str | None:
    """把路由交接的 ``route`` 归一化成固定短码；**非受限标识符一律 ``None``**。

    只做只读投影：不接受自由文本（日志里绝不允许出现原文），也不改变路由结果本身。
    """

    if type(value) is not str:
        return None
    try:
        require_identifier("route", value)
    except ContractError:
        return None
    return value


def reply_chars(outcome: object) -> int | None:
    """本轮 Agent 回复的**字数**（含不可播报时的文本）；没有结果时为 ``None``。

    诊断动机（2026-10-24）：`text_only` 轮既没有播报也没有音频事实，而 `reply_text` 在
    不可播报时被置为 ``None``（见下方 ``release``），于是"到底有没有文字、文字多长"
    在真机上完全不可见。这里只报长度，**绝不记录正文**。
    """

    if isinstance(outcome, AgentTurnOutcome) and type(outcome.text) is str:
        return len(outcome.text)
    return None


def speech_skip_reason(outcome: object, *, route: str | None = None) -> str | None:
    """本轮**为什么没有可播报文本**的固定短码；可播报时为 ``None``。

    与 :func:`speech_eligible` 同源、逐条对应，只增加"是哪一条不满足"的定位信息：

    - ``no_outcome`` / ``no_outcome:route:<route>``：本轮**没有 Agent 结果**。路由交接不是
      ``chat`` 时 ``ConversationManager._submit_routed()`` 根本不会调 Agent（真机上这一轮
      既没有回复也没有播报，只剩一个 ``text_only``）；带上路由码即可区分"没调用"与"调用失败"。
    - ``not_completed:<category>``：Agent 轮未完成；``category`` 是
      :class:`AgentFailureCategory` 的固定值（如 ``stream_interrupted`` / ``empty_response``）。
    - ``failure:<category>``：``completed=True`` 却仍带故障（反常组合，如实分档）。
    - ``empty_text``：回复是空白。
    - ``missing_finish_reason``：回复有文字但没有终止原因。

    全部只读、只含固定短码与枚举值，不含任何识别/回复正文。
    """

    if not isinstance(outcome, AgentTurnOutcome):
        return f"no_outcome:route:{route}" if route is not None else "no_outcome"
    if not outcome.completed:
        category = getattr(getattr(outcome, "failure", None), "category", None)
        value = getattr(category, "value", None)
        return f"not_completed:{value}" if type(value) is str else "not_completed"
    if outcome.failure is not None:
        category = getattr(outcome.failure, "category", None)
        value = getattr(category, "value", None)
        return f"failure:{value}" if type(value) is str else "failure"
    if type(outcome.text) is not str or not outcome.text.strip():
        return "empty_text"
    if type(outcome.finish_reason) is not str or not outcome.finish_reason.strip():
        return "missing_finish_reason"
    return None


class PushToTalkVoiceDriver:
    """P6 `PushToTalkCoordinator` 的只读适配层；不拥有语音输出。"""

    def __init__(
        self,
        *,
        coordinator: object,
        audio_session_id: str = "voice-loop-audio",
        stream_id: str = "voice-loop-stream",
        provider_generation: int = 1,
        speech_handled_by_driver: bool = False,
    ) -> None:
        for name in _COORDINATOR_METHODS:
            if not callable(getattr(coordinator, name, None)):
                raise TypeError(f"coordinator 必须提供 {name}")
        require_identifier("audio_session_id", audio_session_id)
        require_identifier("stream_id", stream_id)
        require_positive("provider_generation", provider_generation)
        if type(speech_handled_by_driver) is not bool:
            raise TypeError("speech_handled_by_driver 必须是布尔值")
        self._coordinator = coordinator
        self._audio_session_id = audio_session_id
        self._stream_id = stream_id
        self._provider_generation = provider_generation
        self._speech_handled_by_driver = speech_handled_by_driver
        self._boundary: VoiceTurnBoundary | None = None
        self._last_pump: object | None = None
        self._last_start_reason: str | None = None
        self._last_result: VoiceDriverTurnResult | None = None

    @property
    def last_pump(self) -> object | None:
        """最近一次 ``pump`` 的原始结果；仅供只读诊断，不参与任何安全判断。"""

        return self._last_pump

    @property
    def last_start_reason(self) -> str | None:
        """最近一次启动失败的具体原因（本地固定代码 + 设备说明），仅供诊断。"""

        return self._last_start_reason

    @property
    def last_result(self) -> VoiceDriverTurnResult | None:
        """最近一次 ``release`` 的归一化结果；仅供只读诊断。"""

        return self._last_result

    def start(self, boundary: VoiceTurnBoundary) -> bool:
        if not isinstance(boundary, VoiceTurnBoundary):
            raise TypeError("boundary 必须是 VoiceTurnBoundary")
        if self._boundary is not None:
            raise VoiceLoopError("语音管道已在进行中")
        self._last_start_reason = None
        asr_boundary = AsrSessionBoundary(
            session_id=boundary.voice_session_id,
            audio_session_id=self._audio_session_id,
            provider_generation=self._provider_generation,
            stream_id=self._stream_id,
            utterance_id=boundary.utterance_id,
        )
        result = self._coordinator.start(asr_boundary)
        started = bool(getattr(result, "started", False))
        if started:
            self._boundary = boundary
        else:
            reason = getattr(result, "diagnostic", None) or getattr(result, "reason", None)
            self._last_start_reason = str(reason) if reason is not None else "start_rejected"
        return started

    def pump(self) -> int:
        if self._boundary is None:
            return 0
        result = self._coordinator.pump()
        self._last_pump = result
        frames = getattr(result, "processed_frames", 0)
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
            return 0
        return frames

    def release(self) -> VoiceDriverTurnResult:
        if self._boundary is None:
            raise VoiceLoopError("语音管道尚未开始")
        boundary = self._boundary
        turn = self._coordinator.release(
            turn_id=boundary.turn_id,
            request_id=boundary.request_id,
        )
        self._boundary = None
        decision = getattr(getattr(turn, "gate_result", None), "decision", None)
        accepted = decision is ResultGateDecision.ACCEPTED_FINAL
        outcome = getattr(turn, "outcome", None)
        event = getattr(getattr(turn, "gate_result", None), "event", None)
        recognized = getattr(event, "text", None) if accepted else None
        eligible = speech_eligible(outcome)
        # 诊断（2026-10-24）：`text_only` 轮必须能区分"路由没调 Agent"与"Agent 失败"。
        # 两者都让 `speech_eligible` 为假、`reply_text` 为 None，旧事实里完全同形。
        route = route_code(getattr(turn, "route", None))
        skip_reason = (
            None if eligible or not accepted else speech_skip_reason(outcome, route=route)
        )
        result = VoiceDriverTurnResult(
            accepted=accepted,
            # 被接纳即表示本轮已提交给统一会话入口：任务路由只记录 RouteHandoff，
            # 不产生 Agent 文字回复，因此不能以 outcome 是否存在判断是否提交。
            submitted=accepted,
            speech_eligible=eligible,
            speech_handled=accepted and self._speech_handled_by_driver,
            reply_text=outcome.text if eligible else None,
            reason=decision.value if isinstance(decision, ResultGateDecision) else "unknown",
            accepted_text=recognized if isinstance(recognized, str) else None,
            reply_chars=reply_chars(outcome),
            speech_skip_reason=skip_reason,
            route=route,
        )
        self._last_result = result
        return result

    def cancel(self, *, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串")
        if self._boundary is None:
            raise VoiceLoopError("语音管道尚未开始")
        self._boundary = None
        self._coordinator.runtime_failed(code=AsrFailureCode.CANCELLED)


__all__ = [
    "PushToTalkVoiceDriver",
    "reply_chars",
    "route_code",
    "speech_eligible",
    "speech_skip_reason",
]
