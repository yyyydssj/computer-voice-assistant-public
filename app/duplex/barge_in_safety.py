"""P10 批次 R4：打断的**安全门**（插话永不授权；安全确认提示被打断即失效）。

设计依据：``implementation_plan/12-barge-in-and-aec.md``「安全确认例外」：

- 安全确认提示被打断时，本次提示立即失效，必须重新完整显示和播报；
- 用户插话本身**不等于**授权；第一阶段降音量也不进入授权识别；
- L3 仍只接受提示完整结束后、有效独立确认状态中的完整固定短语。

本模块只做**一个决策**：确认插话之后，允许做什么。它没有副作用、不碰设备、不碰
``app.safety``/``app.executor``/``app.plans``，也不产生任何授权事实；``authorization_possible``
在该模块的输出里**恒为 False**（这是可测试的不变量，不是承诺）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.base import ContractError

ACTION_HANDOFF = "handoff"
"""普通回复被插话：允许停止播报，并把前置音频并入用户下一轮。"""

ACTION_STOP_AND_REPLAY = "stop_and_replay"
"""安全确认提示被插话：停止播报、提示失效、必须完整重播，且**不**建立可用轮次。"""

ACTION_BLOCK = "block"
"""当前条件不允许打断（半双工 / 通道不可用 / 没有播报）：保持原状。"""

ACTIONS = (ACTION_HANDOFF, ACTION_STOP_AND_REPLAY, ACTION_BLOCK)

REASON_NORMAL_REPLY = "normal_reply_handoff"
REASON_SAFETY_PROMPT = "safety_prompt_replay_required"
REASON_AWAITING_CONFIRMATION = "awaiting_confirmation_replay_required"
REASON_HALF_DUPLEX = "half_duplex_no_barge_in"
REASON_CHANNEL_UNAVAILABLE = "channel_unavailable"
REASON_PLAYBACK_INACTIVE = "playback_not_active"
REASON_NEVER_AUTHORIZES = "barge_in_never_authorizes"

PROMPT_KIND_CHAT = "chat_reply"
PROMPT_KIND_SAFETY = "safety_confirmation"
PROMPT_KINDS = (PROMPT_KIND_CHAT, PROMPT_KIND_SAFETY)


def _require_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} 必须是布尔值")
    return value


@dataclass(frozen=True, slots=True)
class BargeInSafetyDecision:
    """确认插话后的允许动作；``to_details()`` 只含固定短代码与布尔事实。"""

    action: str
    reason: str
    stop_playback: bool
    allow_new_turn: bool
    handoff_pre_roll: bool
    prompt_replay_required: bool
    authorization_possible: bool = False

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ContractError("action 必须是已知动作")
        if self.authorization_possible:
            # 硬不变量：打断永不产生授权。
            raise ContractError("打断不得声称可授权")
        for name in (
            "stop_playback",
            "allow_new_turn",
            "handoff_pre_roll",
            "prompt_replay_required",
        ):
            _require_bool(name, getattr(self, name))

    @property
    def blocked(self) -> bool:
        return self.action == ACTION_BLOCK

    def to_details(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "stop_playback": self.stop_playback,
            "allow_new_turn": self.allow_new_turn,
            "handoff_pre_roll": self.handoff_pre_roll,
            "prompt_replay_required": self.prompt_replay_required,
            "authorization_possible": self.authorization_possible,
        }


class BargeInSafetyGate:
    """打断安全门（纯离线、确定性）。"""

    def decide(
        self,
        *,
        prompt_kind: str = PROMPT_KIND_CHAT,
        awaiting_confirmation: bool = False,
        playback_active: bool = True,
        channel_available: bool = True,
        realtime_available: bool = True,
    ) -> BargeInSafetyDecision:
        """给出确认插话后的允许动作；判定顺序按"能力从低到高"fail closed。"""

        if prompt_kind not in PROMPT_KINDS:
            raise ContractError("prompt_kind 必须是已知提示类型")
        awaiting_confirmation = _require_bool("awaiting_confirmation", awaiting_confirmation)
        playback_active = _require_bool("playback_active", playback_active)
        channel_available = _require_bool("channel_available", channel_available)
        realtime_available = _require_bool("realtime_available", realtime_available)

        if not realtime_available:
            # 半双工（播放期间不收音）：不存在可用的打断判定。
            return self._block(REASON_HALF_DUPLEX)
        if not channel_available:
            return self._block(REASON_CHANNEL_UNAVAILABLE)
        if not playback_active:
            return self._block(REASON_PLAYBACK_INACTIVE)
        if prompt_kind == PROMPT_KIND_SAFETY or awaiting_confirmation:
            # 提示被打断即失效：停播 + 必须完整重播；打断音频**不**建立可用轮次，
            # 更不可能成为 L3 的固定短语确认。
            return BargeInSafetyDecision(
                action=ACTION_STOP_AND_REPLAY,
                reason=REASON_SAFETY_PROMPT if prompt_kind == PROMPT_KIND_SAFETY
                else REASON_AWAITING_CONFIRMATION,
                stop_playback=True,
                allow_new_turn=False,
                handoff_pre_roll=False,
                prompt_replay_required=True,
            )
        return BargeInSafetyDecision(
            action=ACTION_HANDOFF,
            reason=REASON_NORMAL_REPLY,
            stop_playback=True,
            allow_new_turn=True,
            handoff_pre_roll=True,
            prompt_replay_required=False,
        )

    def _block(self, reason: str) -> BargeInSafetyDecision:
        return BargeInSafetyDecision(
            action=ACTION_BLOCK,
            reason=reason,
            stop_playback=False,
            allow_new_turn=False,
            handoff_pre_roll=False,
            prompt_replay_required=False,
        )


__all__ = [
    "ACTION_BLOCK",
    "ACTION_HANDOFF",
    "ACTION_STOP_AND_REPLAY",
    "ACTIONS",
    "PROMPT_KINDS",
    "PROMPT_KIND_CHAT",
    "PROMPT_KIND_SAFETY",
    "REASON_AWAITING_CONFIRMATION",
    "REASON_CHANNEL_UNAVAILABLE",
    "REASON_HALF_DUPLEX",
    "REASON_NEVER_AUTHORIZES",
    "REASON_NORMAL_REPLY",
    "REASON_PLAYBACK_INACTIVE",
    "REASON_SAFETY_PROMPT",
    "BargeInSafetyDecision",
    "BargeInSafetyGate",
]
