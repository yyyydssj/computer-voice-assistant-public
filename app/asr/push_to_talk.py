"""P6 阶段级按键语音编排。

该模块只把一次半双工 Capture/VAD/Provider/ResultGate 轮次接到既有
ConversationManager。ASR 结果不拥有任务、计划、授权、执行或 TTS 权限；
所有失败均先关闭当前语音边界并保持文字输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol
from uuid import uuid4

from app.asr.recovery import (
    AsrFailure,
    AsrFailureCode,
    AsrFailureStage,
    AsrRecoveryConfig,
    AsrRecoveryController,
    AsrRecoveryState,
    AsrSessionBoundary,
)
from app.asr.result_gate import (
    AsrResultCandidate,
    ResultGate,
    ResultGateDecision,
    ResultGateResult,
)
from app.asr import AsrEvent, AsrProviderUnavailable, SenseVoiceRecognizerAdapter, SenseVoiceSession
from app.audio import AudioBus, AudioPacket
from app.audio.capture import CaptureConfig, CaptureSession, TemporaryAudioStore
from app.audio.vad import VadConfig, VadDecision, VadFrameStatus, VadSession
from app.conversation import ConversationManager
from app.agent import AgentTurnOutcome
from app.contracts.base import require_identifier, require_positive


class PushToTalkError(RuntimeError):
    """按键语音轮次未能安全建立或提交。"""


class PushToTalkState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    FINISHING = "finishing"
    TERMINAL = "terminal"


class CaptureFactory(Protocol):
    def __call__(self, bus: AudioBus) -> CaptureSession: ...


class VadFactory(Protocol):
    def __call__(self, bus: AudioBus) -> VadSession: ...


@dataclass(frozen=True, slots=True)
class PushToTalkDiagnostic:
    """只包含枚举化边界事实；不包含 PCM、识别文本、路径或异常正文。"""

    event_type: str
    subject_id: str
    reason: str
    state: PushToTalkState
    recovery_state: AsrRecoveryState

    def to_details(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "state": self.state.value,
            "recovery_state": self.recovery_state.value,
        }


@dataclass(frozen=True, slots=True)
class PushToTalkStartResult:
    started: bool
    boundary: AsrSessionBoundary | None = None
    reason: str | None = None
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class PushToTalkPumpResult:
    processed_frames: int
    last_vad: VadDecision | None = None
    provider_failed: bool = False


@dataclass(frozen=True, slots=True)
class PushToTalkTurnResult:
    gate_result: ResultGateResult
    provider_event: AsrEvent | None
    outcome: AgentTurnOutcome | None
    route: str | None = None
    """本轮路由交接的固定短码（如 ``chat``/``task``）；未启用路由或未提交时为 ``None``。

    诊断（2026-10-24）：`ConversationManager._submit_routed()` **只有** ``route == "chat"``
    才会调用 Agent，其余路由的 ``outcome`` 恒为 ``None``，真机上表现为"识别到了、没有
    回复、没有播报"。旧事实里这与"Agent 失败"完全同形（都只剩一个 ``text_only``），
    所以把路由短码如实带到轮次事实里；它不含任何正文，也不改变路由结果。
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


def _speech_eligible(outcome: object | None) -> bool:
    """Only a complete, failure-free Agent turn may be spoken."""

    return (
        isinstance(outcome, AgentTurnOutcome)
        and outcome.completed
        and outcome.failure is None
        and type(outcome.text) is str
        and bool(outcome.text.strip())
        and type(outcome.finish_reason) is str
        and bool(outcome.finish_reason.strip())
    )


def drive_turn_stream(
    speaker: object | None,
    submit: Callable[[Callable[[str], None] | None], AgentTurnOutcome | None],
) -> AgentTurnOutcome | None:
    """Drive one voice turn through an optional opt-in realtime speech stream.

    The stream starts before submission so sentence-level playback can begin
    while the Agent is still producing text. Only a complete, failure-free
    outcome finishes the stream; anything else cancels it so no partial reply is
    spoken. A missing or failing stream never blocks the turn and never changes
    the returned outcome — speech has no authority over the conversation result.
    """

    stream = None
    start_stream = getattr(speaker, "start_stream", None)
    if callable(start_stream):
        try:
            stream = start_stream()
        except Exception:  # noqa: BLE001 - speech is optional
            stream = None
    push_chunk = getattr(stream, "push_text_chunk", None) if stream is not None else None
    try:
        outcome = submit(push_chunk if callable(push_chunk) else None)
    except Exception:
        _cancel_stream(stream)
        raise
    if _speech_eligible(outcome):
        if stream is not None:
            finish = getattr(stream, "finish", None)
            if callable(finish):
                try:
                    finish()
                except Exception:  # noqa: BLE001 - TTS failure must not change the outcome
                    _cancel_stream(stream)
    else:
        _cancel_stream(stream)
    return outcome


class PushToTalkCoordinator:
    """显式驱动的一次按键语音编排器。

    ``start`` 建立新边界，``pump`` 只消费当前 AudioBus，``release`` 结束当前
    utterance 并最多提交一次；不创建后台任务，也不自动切回语音。
    """

    def __init__(
        self,
        *,
        conversation: ConversationManager,
        provider: SenseVoiceRecognizerAdapter,
        capture_factory: CaptureFactory,
        vad_factory: VadFactory,
        source_instance_id: str,
        device_epoch: int = 1,
        audio_capacity_frames: int = 32,
        recovery_config: AsrRecoveryConfig | None = None,
        temporary_audio_store: TemporaryAudioStore | None = None,
        diagnostic_sink: Callable[[PushToTalkDiagnostic], None] | None = None,
        realtime_speaker: object | None = None,
    ) -> None:
        if not isinstance(conversation, ConversationManager):
            raise TypeError("conversation 必须是 ConversationManager")
        if not isinstance(provider, SenseVoiceRecognizerAdapter):
            raise TypeError("provider 必须是 SenseVoiceRecognizerAdapter")
        if not callable(capture_factory) or not callable(vad_factory):
            raise TypeError("capture_factory/vad_factory 必须可调用")
        require_identifier("source_instance_id", source_instance_id)
        require_positive("device_epoch", device_epoch)
        require_positive("audio_capacity_frames", audio_capacity_frames)
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._conversation = conversation
        self._realtime_speaker = realtime_speaker
        self._provider = provider
        self._capture_factory = capture_factory
        self._vad_factory = vad_factory
        self._source_instance_id = source_instance_id
        self._device_epoch = device_epoch
        self._audio_capacity_frames = audio_capacity_frames
        self._temporary_audio_store = temporary_audio_store
        self._diagnostic_sink = diagnostic_sink
        self._state = PushToTalkState.IDLE
        self._boundary: AsrSessionBoundary | None = None
        self._bus: AudioBus | None = None
        self._capture: CaptureSession | None = None
        self._vad: VadSession | None = None
        self._provider_session: SenseVoiceSession | None = None
        self._gate: ResultGate | None = None
        self._temporary_audio_path = None
        self._last_provider_event: AsrEvent | None = None
        self._last_gate_result = ResultGateResult(ResultGateDecision.INVALID_EVENT)
        self._recovery = AsrRecoveryController(
            recovery_config or AsrRecoveryConfig(),
            discard_resources=self._discard_resources,
        )

    @property
    def state(self) -> PushToTalkState:
        return self._state

    @property
    def recovery(self) -> AsrRecoveryController:
        return self._recovery

    @property
    def boundary(self) -> AsrSessionBoundary | None:
        return self._boundary

    @property
    def gate(self) -> ResultGate | None:
        return self._gate

    def startup_succeeded(self) -> None:
        try:
            self._provider.load()
        except AsrProviderUnavailable:
            self.startup_failed(code=AsrFailureCode.PROVIDER_UNAVAILABLE)
            return
        result = self._recovery.startup_succeeded()
        if result.decision.value == "rejected":
            raise PushToTalkError("ASR startup 状态非法")

    def startup_failed(self, *, code: AsrFailureCode) -> None:
        self._recovery.startup_failed(self._failure(AsrFailureStage.STARTUP, code))
        self._emit("asr_startup_failed", "startup", code.value)

    def start(self, boundary: AsrSessionBoundary, *, temporary_audio_path=None) -> PushToTalkStartResult:
        if self._state is not PushToTalkState.IDLE:
            return PushToTalkStartResult(False, reason="语音轮次已在进行或已终止")
        if not isinstance(boundary, AsrSessionBoundary):
            return PushToTalkStartResult(False, reason="语音边界无效")
        if self._recovery.state is not AsrRecoveryState.READY:
            return PushToTalkStartResult(False, reason="ASR 当前不可用")
        if self._recovery.begin_session_warmup().decision.value == "rejected":
            return PushToTalkStartResult(False, reason="ASR 预热被拒绝")
        self._boundary = boundary
        self._bus = AudioBus(
            audio_session_id=boundary.audio_session_id,
            provider_generation=boundary.provider_generation,
            stream_id=boundary.stream_id,
            device_epoch=self._device_epoch,
            capacity_frames=self._audio_capacity_frames,
        )
        self._gate = ResultGate(
            session_id=boundary.session_id,
            audio_session_id=boundary.audio_session_id,
            provider_generation=boundary.provider_generation,
            stream_id=boundary.stream_id,
            utterance_id=boundary.utterance_id,
            source_instance_id=self._source_instance_id,
        )
        self._capture = self._capture_factory(self._bus)
        self._vad = self._vad_factory(self._bus)
        try:
            self._provider_session = self._provider.start_session(
                bus=self._bus,
                utterance_id=boundary.utterance_id,
            )
            capture_result = self._capture.start()
            if not capture_result.started:
                raise PushToTalkError(f"capture_unavailable: {capture_result.check.reason}")
            vad_result = self._vad.start()
            if vad_result.status is not VadFrameStatus.STARTED:
                raise PushToTalkError("vad_unavailable")
            transition = self._recovery.session_warmup_succeeded(boundary)
            if transition.decision.value == "rejected":
                raise PushToTalkError("session_boundary_rejected")
            self._state = PushToTalkState.ACTIVE
            self._temporary_audio_path = temporary_audio_path
            self._emit("asr_session_started", boundary.utterance_id, "started")
            return PushToTalkStartResult(True, boundary=boundary)
        except Exception as exc:  # noqa: BLE001 - 预热失败必须安全降级为文字
            detail = str(exc.args[0]) if exc.args else "warmup_failed"
            code = detail.split(":", 1)[0].strip() or "warmup_failed"
            self._recovery.session_warmup_failed(
                self._failure(AsrFailureStage.SESSION, AsrFailureCode.PROVIDER_UNAVAILABLE)
            )
            self._emit("asr_session_failed", boundary.utterance_id, code)
            return PushToTalkStartResult(
                False, boundary=boundary, reason="ASR 预热失败", diagnostic=detail
            )

    def pump(self) -> PushToTalkPumpResult:
        if self._state is not PushToTalkState.ACTIVE or self._bus is None or self._vad is None or self._provider_session is None:
            return PushToTalkPumpResult(0)
        processed = 0
        last_vad: VadDecision | None = None
        while True:
            packet = self._bus.read_nowait()
            if packet is None:
                break
            last_vad = self._vad.process(packet)
            if last_vad.status in {VadFrameStatus.FAILED, VadFrameStatus.CLOSED}:
                self.runtime_failed(code=AsrFailureCode.STREAM_FAILED)
                return PushToTalkPumpResult(processed, last_vad, True)
            if not self._provider_session.feed(packet):
                self.runtime_failed(code=AsrFailureCode.DECODE_FAILED)
                return PushToTalkPumpResult(processed, last_vad, True)
            processed += 1
            if last_vad.status is VadFrameStatus.ENDED:
                break
        return PushToTalkPumpResult(processed, last_vad)

    def release(self, *, turn_id: str | None = None, request_id: str | None = None) -> PushToTalkTurnResult:
        if self._state is not PushToTalkState.ACTIVE or self._provider_session is None or self._gate is None:
            return PushToTalkTurnResult(self._last_gate_result, None, None)
        # 收尾前先把总线上"已采集但尚未处理"的帧全部喂给 provider：它们就是这句话的尾巴，
        # 在解码前丢掉等于把最后一个字吞掉（P9 待修 B 的附带项）。
        # ``pump()`` 内部若判定 VAD/provider 故障会自行 ``runtime_failed``（状态转为 IDLE），
        # 那种情况下沿用既有失败语义，绝不覆盖它的状态。
        self.pump()
        if self._state is not PushToTalkState.ACTIVE:
            return PushToTalkTurnResult(self._last_gate_result, None, None)
        self._state = PushToTalkState.FINISHING
        if self._vad is not None:
            self._vad.finish()
        event = self._provider_session.finish()
        self._last_provider_event = event
        if event is None:
            self.runtime_failed(code=AsrFailureCode.DECODE_FAILED)
            return PushToTalkTurnResult(self._last_gate_result, None, None)
        accepted = self.accept_provider_event(event, turn_id=turn_id, request_id=request_id)
        self._finish_session()
        return accepted

    def accept_provider_event(
        self,
        event: AsrEvent,
        *,
        turn_id: str | None = None,
        request_id: str | None = None,
    ) -> PushToTalkTurnResult:
        """接纳一次 Provider 事件。

        ``turn_id`` / ``request_id`` 是可选的外部轮次身份透传：调用方（例如 P8
        语音闭环）可以提供自己的轮次标识，使提交给 ConversationManager 的
        trace 与本地轮次边界一致；不提供时保持原有行为。
        """

        if self._gate is None or self._boundary is None:
            return PushToTalkTurnResult(self._last_gate_result, event, None)
        try:
            candidate = AsrResultCandidate.from_provider_event(
                event,
                session_id=self._boundary.session_id,
                source_instance_id=self._source_instance_id,
            )
            result = self._gate.evaluate(candidate)
        except (TypeError, ValueError):
            result = ResultGateResult(ResultGateDecision.INVALID_EVENT)
        self._last_gate_result = result
        if result.decision is not ResultGateDecision.ACCEPTED_FINAL or result.event is None:
            return PushToTalkTurnResult(result, event, None)
        effective_turn_id = turn_id if turn_id is not None else self._boundary.utterance_id
        # 诊断（2026-10-24）：把路由交接的 `route` 一路带到本轮事实（不含正文、不改路由结果）。
        route: str | None = None
        if self._conversation.routing_enabled:
            captured: dict[str, object] = {}

            def _submit(
                on_text_chunk: Callable[[str], None] | None,
            ) -> AgentTurnOutcome | None:
                routed = self._conversation.submit_asr_final(
                    result.text,
                    turn_id=effective_turn_id,
                    request_id=request_id,
                    on_text_chunk=on_text_chunk,
                )
                captured["route"] = getattr(getattr(routed, "handoff", None), "route", None)
                return routed.outcome

            outcome = drive_turn_stream(self._realtime_speaker, _submit)
            raw_route = captured.get("route")
            route = raw_route if isinstance(raw_route, str) else None
        else:
            outcome = self._conversation.send_chat(text=result.text)
        self._emit("asr_final_submitted", self._boundary.utterance_id, "accepted_final")
        return PushToTalkTurnResult(result, event, outcome, route=route)

    def runtime_failed(self, *, code: AsrFailureCode) -> None:
        if self._state not in {PushToTalkState.ACTIVE, PushToTalkState.FINISHING}:
            return
        if self._gate is not None:
            self._gate.close()
        if self._boundary is not None:
            self._recovery.runtime_failed(self._failure(AsrFailureStage.RUNTIME, code))
            self._emit("asr_runtime_failed", self._boundary.utterance_id, code.value)
        self._state = PushToTalkState.IDLE
        self._discard_resources("runtime_failure")

    def abandon_turn(self, *, reason: str) -> bool:
        """放弃一轮**没有可用输入**的语音轮次，且不把它记成 ASR 故障。

        P9 免按键连续对话里"这一轮没听到人说话"是常态；若沿用
        ``runtime_failed``，``AsrRecoveryController`` 会进入
        ``SESSION_FAILED_RUNTIME``，而 ``start()`` 只接受 ``READY``，于是**一次丢弃
        就会锁死后续所有轮次**（真机已复现）。

        本方法只做安全收尾：
        - 关闭闸门、provider session、采集、总线与 pre-roll（复用既有清理路径）；
        - 已建立的语音边界按"本轮完成"回收（``session_completed``）；
        - 若恢复状态仍不可用，只走既有低频探测路径（``begin_probe`` →
          ``record_probe(True)`` → ``request_voice_switch(safe_idle=True)``）把它
          带回 ``READY``；任一步被拒就保持不可用（fail closed，不伪造恢复）。
        - **不**产生任何 ASR 故障事实，也**不**创建计划、授权或执行事实。

        返回 ``True`` 表示收尾后确实回到 ``READY``（可以开始下一轮）。
        """

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串")
        require_identifier("reason", reason)
        if self._gate is not None:
            self._gate.close()
        self._state = PushToTalkState.IDLE
        self._discard_resources(reason)
        self._recovery.session_completed()
        if self._recovery.state is AsrRecoveryState.READY:
            self._emit("asr_turn_abandoned", self._subject_id(), reason)
            return True
        if self._recovery.begin_probe().decision.value == "rejected":
            self._emit("asr_turn_abandoned", self._subject_id(), reason)
            return False
        for _ in range(self._recovery.config.required_probe_passes):
            if self._recovery.record_probe(True).decision.value == "rejected":
                self._emit("asr_turn_abandoned", self._subject_id(), reason)
                return False
        switched = self._recovery.request_voice_switch(safe_idle=True)
        ready = (
            switched.decision.value == "accepted"
            and self._recovery.state is AsrRecoveryState.READY
        )
        self._emit("asr_turn_abandoned", self._subject_id(), reason)
        return ready

    def _subject_id(self) -> str:
        if self._boundary is not None:
            return self._boundary.utterance_id
        return "voice-turn-abandoned"

    def submit_text(self, text: str) -> AgentTurnOutcome:
        if self._conversation.routing_enabled:
            routed = self._conversation.submit_text(text)
            if routed.outcome is None:
                raise PushToTalkError("文字输入未进入聊天路由")
            return routed.outcome
        return self._conversation.send_chat(text=text)

    def _finish_session(self) -> None:
        if self._recovery.state is AsrRecoveryState.SESSION_ACTIVE:
            self._recovery.session_completed()
        self._discard_resources("session_completed")
        self._state = PushToTalkState.IDLE

    def _discard_resources(self, reason: str) -> None:
        if self._gate is not None:
            self._gate.close()
        if self._provider_session is not None:
            self._provider_session.cancel()
        if self._vad is not None:
            self._vad.cancel()
        if self._capture is not None:
            try:
                self._capture.stop(reason=reason, discard=True)
            except Exception:
                pass
        if self._bus is not None:
            self._bus.close(reason=reason, discard=True)
        if self._temporary_audio_path is not None and self._temporary_audio_store is not None:
            self._temporary_audio_store.cleanup(self._temporary_audio_path)
        self._temporary_audio_path = None
        self._provider_session = None
        self._vad = None
        self._capture = None
        self._bus = None

    def _failure(self, stage: AsrFailureStage, code: AsrFailureCode) -> AsrFailure:
        return AsrFailure(stage=stage, code=code, occurred_at="2026-09-21T14:00:00Z")

    def _emit(self, event_type: str, subject_id: str, reason: str) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            PushToTalkDiagnostic(
                event_type=event_type,
                subject_id=subject_id,
                reason=reason,
                state=self._state,
                recovery_state=self._recovery.state,
            )
        )


__all__ = [
    "PushToTalkCoordinator",
    "PushToTalkDiagnostic",
    "PushToTalkError",
    "PushToTalkPumpResult",
    "PushToTalkStartResult",
    "PushToTalkState",
    "PushToTalkTurnResult",
]
