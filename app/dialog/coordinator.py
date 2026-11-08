"""P9 批次 1：turn claim 与输入许可（对话协调器）。

边界：本模块只决定“当前是否允许开始一次输入”以及“哪一次输入轮次仍然有效”。它不
决定授权、不接触任务/计划/授权/执行/结果事实；等待授权与执行期间一律 fail closed
（拒绝输入且不迁移状态），迟到/伪造/跨窗口的 claim 一律作废。模块不引用安全链模块。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.contracts.base import (
    ContractError,
    parse_utc_timestamp,
    require_choice,
    require_identifier,
)

from app.dialog.state import (
    DIALOG_TRANSITION_REASONS,
    DialogDiagnostic,
    DialogError,
    DialogInputMode,
    DialogState,
    DialogStateMachine,
    DialogTransition,
)
from app.dialog.window import DialogWindow
from app.voice_loop.boundary import VoiceInputSource, VoiceLoopError, require_reason

CLAIM_LAYERS: frozenset[str] = frozenset({"voice", "text"})

DIALOG_INPUT_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "granted",
        "claim_active",
        "window_closed",
        "state_blocked",
        "closed",
        "invalidated",
    }
)


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _require_claim_reason(value: str) -> None:
    """协调器原因码：复用语音闭环的受限标识符校验，不接受自由文本。"""

    try:
        require_reason("reason", value)
    except (VoiceLoopError, ContractError) as exc:
        raise DialogError("reason 必须是受限原因码") from exc


def _require_transition_reason(value: str) -> None:
    """迁移原因码：受限标识符且必须属于 DIALOG_TRANSITION_REASONS。"""

    _require_claim_reason(value)
    if value not in DIALOG_TRANSITION_REASONS:
        raise DialogError("reason 不在对话迁移原因白名单内")


@dataclass(frozen=True, slots=True)
class TurnClaim:
    """一次输入轮次的本地许可标识；被动收音的 source 为 None。

    ``source`` 只接受 ``VoiceInputSource`` 实例或 ``None``：字符串（含等值枚举字符串
    如 ``"asr"``）一律抛 ``TypeError``，不做任何隐式转换。
    """

    claim_id: str
    window_id: str
    turn_id: str
    source: VoiceInputSource | None
    layer: str
    issued_at: str

    def __post_init__(self) -> None:
        for name in ("claim_id", "window_id", "turn_id"):
            require_identifier(name, getattr(self, name))
        if self.layer not in CLAIM_LAYERS:
            raise DialogError("layer 只允许 voice / text")
        if self.source is not None and not isinstance(self.source, VoiceInputSource):
            raise TypeError("source 必须是 VoiceInputSource 实例或 None")
        parse_utc_timestamp("issued_at", self.issued_at)


@dataclass(frozen=True, slots=True)
class TurnClaimDecision:
    granted: bool
    claim: TurnClaim | None
    reason: str

    def __post_init__(self) -> None:
        if type(self.granted) is not bool:
            raise TypeError("granted 必须是布尔值")
        require_choice("reason", self.reason, DIALOG_INPUT_REFUSAL_REASONS)
        if self.granted:
            if not isinstance(self.claim, TurnClaim):
                raise ValueError("授予的决策必须携带 claim")
            if self.reason != "granted":
                raise ValueError("授予的决策必须使用 granted 原因码")
        else:
            if self.claim is not None:
                raise ValueError("拒绝的决策不得携带 claim")
            if self.reason == "granted":
                raise ValueError("拒绝的决策不得使用 granted 原因码")


@dataclass(frozen=True, slots=True)
class InputPermit:
    granted: bool
    reason: str
    state: DialogState
    mode: DialogInputMode | None

    def __post_init__(self) -> None:
        if type(self.granted) is not bool:
            raise TypeError("granted 必须是布尔值")
        require_choice("reason", self.reason, DIALOG_INPUT_REFUSAL_REASONS)
        if not isinstance(self.state, DialogState):
            raise TypeError("state 必须是 DialogState")
        if self.mode is not None and not isinstance(self.mode, DialogInputMode):
            raise TypeError("mode 必须是 DialogInputMode 或 None")
        if self.granted and self.reason != "granted":
            raise ValueError("授予的许可必须使用 granted 原因码")
        if not self.granted and self.reason == "granted":
            raise ValueError("拒绝的许可不得使用 granted 原因码")


class DialogCoordinator:
    """把对话状态机与连续对话窗口绑在一起，只回答“这次输入是否被许可”。"""

    def __init__(
        self,
        *,
        machine: DialogStateMachine,
        window: DialogWindow,
        diagnostic_sink: Callable[[DialogDiagnostic], None] | None = None,
    ) -> None:
        if not isinstance(machine, DialogStateMachine):
            raise TypeError("machine 必须是 DialogStateMachine")
        if not isinstance(window, DialogWindow):
            raise TypeError("window 必须是 DialogWindow")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._machine = machine
        self._window = window
        self._diagnostic_sink = diagnostic_sink
        self._active: TurnClaim | None = None
        self._closed: list[TurnClaim] = []
        self._issued: set[str] = set()

    @property
    def machine(self) -> DialogStateMachine:
        return self._machine

    @property
    def window(self) -> DialogWindow:
        return self._window

    @property
    def active_claim(self) -> TurnClaim | None:
        """当前有效 claim；窗口关闭或窗口身份变化后旧 claim 立即失效。"""

        return self._current_claim()

    @property
    def closed_claims(self) -> tuple[TurnClaim, ...]:
        return tuple(self._closed)

    def request_input(
        self, *, layer: str, source: VoiceInputSource | None = None
    ) -> InputPermit:
        """只读判定；任何拒绝都不得改变状态机状态或窗口状态。"""

        self._require_layer_source(layer, source)
        if not self._window.is_open():
            return self._permit(False, "window_closed")
        if self._current_claim() is not None:
            return self._permit(False, "claim_active")
        if not self._machine.accepts_input:
            return self._permit(False, "state_blocked")
        return self._permit(True, "granted")

    def claim_turn(
        self,
        *,
        layer: str,
        source: VoiceInputSource | None = None,
        turn_id: str | None = None,
    ) -> TurnClaimDecision:
        """同一时刻最多一个活动 claim；第二个请求只被拒绝，不改变任何状态。"""

        resolved_source = self._require_layer_source(layer, source)
        if not self._window.is_open():
            return self._refuse_claim("window_closed")
        if self._current_claim() is not None:
            return self._refuse_claim("claim_active")
        if not self._machine.accepts_input:
            return self._refuse_claim("state_blocked")
        claim = TurnClaim(
            claim_id=f"claim-{uuid.uuid4().hex}",
            window_id=self._window.window_id,
            turn_id=turn_id if turn_id is not None else f"turn-{uuid.uuid4().hex}",
            source=resolved_source,
            layer=layer,
            issued_at=_utc_timestamp(),
        )
        self._issued.add(claim.claim_id)
        self._active = claim
        self._emit("dialog_turn_claimed", "turn_started")
        return TurnClaimDecision(granted=True, claim=claim, reason="granted")

    def release_turn(self, claim: TurnClaim, *, reason: str) -> None:
        if not isinstance(claim, TurnClaim):
            raise TypeError("claim 必须是 TurnClaim")
        _require_claim_reason(reason)
        current = self._require_current_claim(claim)
        self._retire(current)
        self._emit("dialog_turn_released", reason)

    def suspend_for_authorization(self, *, reason: str) -> DialogTransition:
        """进入等待授权：迁移到 AWAITING_AUTHORIZATION 并作废当前输入 claim。"""

        transition = self._machine.transition(
            DialogState.AWAITING_AUTHORIZATION, reason=reason
        )
        self._retire_active()
        return transition

    def suspend_for_execution(self, *, reason: str) -> DialogTransition:
        """进入执行：迁移到 EXECUTING 并作废当前输入 claim。"""

        transition = self._machine.transition(DialogState.EXECUTING, reason=reason)
        self._retire_active()
        return transition

    def resume_listening(self, *, reason: str) -> DialogTransition:
        """回到监听；窗口已关闭时必须先重新开始会话，不得原地复活。"""

        _require_transition_reason(reason)
        if not self._window.is_open():
            raise DialogError("对话窗口已关闭，必须重新唤醒")
        transition = self._machine.transition(DialogState.LISTENING, reason=reason)
        self._window.note_activity()
        self._emit("dialog_listening_resumed", reason)
        return transition

    def note_speech_started(self, *, claim: TurnClaim) -> DialogTransition:
        self._require_current_claim(claim)
        transition = self._machine.transition(
            DialogState.USER_SPEAKING, reason="speech_started"
        )
        self._window.note_activity()
        self._emit("dialog_speech_started", "speech_started")
        return transition

    def note_speech_finished(self, *, claim: TurnClaim) -> DialogTransition:
        self._require_current_claim(claim)
        transition = self._machine.transition(
            DialogState.TRANSCRIBING, reason="speech_finished"
        )
        self._window.note_activity()
        self._emit("dialog_speech_finished", "speech_finished")
        return transition

    def invalidate(self, *, reason: str) -> None:
        """作废当前输入会话：清除活动 claim 并 fail closed（已终结状态不改写）。"""

        _require_transition_reason(reason)
        self._retire_active()
        if self._machine.state not in (DialogState.CLOSED, DialogState.FAIL_CLOSED):
            self._machine.fail_closed(reason=reason)
        self._emit("dialog_invalidated", reason)

    def _require_layer_source(
        self, layer: object, source: object
    ) -> VoiceInputSource | None:
        """layer 必须是 voice/text（否则 DialogError）；source 必须是枚举实例或 None。"""

        if not isinstance(layer, str) or layer not in CLAIM_LAYERS:
            raise DialogError("layer 只允许 voice / text")
        if source is None:
            return None
        if not isinstance(source, VoiceInputSource):
            raise TypeError("source 必须是 VoiceInputSource 实例或 None")
        if layer == "voice" and source is not VoiceInputSource.ASR:
            raise DialogError("voice 层只接受 ASR 来源")
        if layer == "text" and source is not VoiceInputSource.TEXT_SHELL:
            raise DialogError("text 层只接受 TEXT_SHELL 来源")
        return source

    def _current_claim(self) -> TurnClaim | None:
        claim = self._active
        if claim is None:
            return None
        if claim.claim_id not in self._issued:
            self._retire(claim)
            return None
        if claim.window_id != self._window.window_id or not self._window.is_open():
            self._retire(claim)
            return None
        return claim

    def _require_current_claim(self, claim: object) -> TurnClaim:
        if not isinstance(claim, TurnClaim):
            raise TypeError("claim 必须是 TurnClaim")
        current = self._current_claim()
        if current is None or claim != current:
            raise DialogError("claim 不是当前活动轮次（迟到、伪造或已失效）")
        return current

    def _retire(self, claim: TurnClaim) -> None:
        if claim not in self._closed:
            self._closed.append(claim)
        if self._active is not None and self._active.claim_id == claim.claim_id:
            self._active = None

    def _retire_active(self) -> None:
        claim = self._active
        if claim is not None:
            self._retire(claim)

    def _permit(self, granted: bool, reason: str) -> InputPermit:
        permit = InputPermit(
            granted=granted,
            reason=reason,
            state=self._machine.state,
            mode=self._machine.mode,
        )
        self._emit("dialog_input_evaluated", reason)
        return permit

    def _refuse_claim(self, reason: str) -> TurnClaimDecision:
        decision = TurnClaimDecision(granted=False, claim=None, reason=reason)
        self._emit("dialog_turn_refused", reason)
        return decision

    def _emit(self, event_type: str, reason: str) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            DialogDiagnostic(
                event_type=event_type,
                subject_id=self._window.window_id,
                reason=reason,
                state=self._machine.state,
                mode=self._machine.mode,
            )
        )


__all__ = [
    "CLAIM_LAYERS",
    "DIALOG_INPUT_REFUSAL_REASONS",
    "DialogCoordinator",
    "InputPermit",
    "TurnClaim",
    "TurnClaimDecision",
]
