"""P10 批次 R1b：两阶段打断的编排语义（平滑恢复、轮次交接、提示失效）。

本模块把 R1a 的纯状态机包成**编排层**，补齐设计文档 12 的第二阶段后续动作：

- 疑似插话 → 降音量（R1a）；
- 误判恢复 → 按 ``restore_ms`` **平滑**恢复原音量，且不建立用户轮次；
- 确认插话 → 音量归零、旧回复继续提交被明确禁止，前置缓冲交出以并入**同一** ``utterance_id``；
- 安全确认提示被打断 → 本次提示立即失效，必须完整重新显示与播报（``PromptPolicy``）。

硬边界（与 R1a 同口径）：不访问设备/文件/网络、不创建线程、不导入安全链模块；
只产出"音量/播放/轮次边界/提示失效要求"的决策，**不**产生授权事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_identifier
from app.duplex.barge_in import (
    ACTION_CONFIRMED,
    ACTION_DUCK,
    ACTION_RESTORE,
    BargeInConfig,
    BargeInController,
    BargeInDecision,
    BargeInObservation,
    BargeInPreRoll,
    BargeInState,
)

# 播放侧事件
EVENT_FRAME = "frame"
EVENT_PLAYBACK_ENDED = "playback_ended"

# 音量/交接原因（固定短代码）
OUTCOME_REASON_PLAYBACK = "playback"
OUTCOME_REASON_DUCKING = "suspected_ducking"
OUTCOME_REASON_RESTORING = "restoring_volume"
OUTCOME_REASON_CONFIRMED = "confirmed_barge_in"
OUTCOME_REASON_ENDED = "playback_ended"

# 安全确认提示状态
PROMPT_STATE_VALID = "valid"
PROMPT_STATE_INVALIDATED = "invalidated"
PROMPT_STATE_COMPLETED = "completed"

# 提示失效原因
PROMPT_REASON_BARGE_IN = "barge_in_interrupted"
PROMPT_REASON_REPLAY_INCOMPLETE = "replay_incomplete"
PROMPT_REASON_REPLAY_COMPLETED = "replay_completed"

_ERROR_CODES = frozenset({"unknown_prompt_state", "playback_not_active"})


class BargeInOrchestrationError(ContractError):
    """编排层拒绝输入；``str()`` 只返回固定短代码。"""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "playback_not_active"
        super().__init__(self.code)


def _require_unit_fraction(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} 必须是 0~1 的数值")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ContractError(f"{name} 必须在 [0,1] 内")
    return result


@dataclass(frozen=True, slots=True)
class PromptPolicy:
    """安全确认提示的失效语义（设计文档 12「安全确认例外」）。

    - 提示被插话打断 → 本次提示立即失效，必须**完整**重新显示与播报，不接受残缺确认；
    - 用户插话本身**不等于**授权；重新完整播报后仍需走原有 L2/L3 流程。
    """

    state: str = PROMPT_STATE_VALID
    replay_required: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in (PROMPT_STATE_VALID, PROMPT_STATE_INVALIDATED, PROMPT_STATE_COMPLETED):
            raise BargeInOrchestrationError("unknown_prompt_state")
        if not isinstance(self.replay_required, bool):
            raise ContractError("replay_required 必须是布尔值")

    @property
    def authorization_possible(self) -> bool:
        """只有"提示有效且未被打断"时才可能继续原确认流程；插话永不直接授权。"""

        return self.state == PROMPT_STATE_VALID

    def to_details(self) -> dict[str, object]:
        return {
            "state": self.state,
            "replay_required": self.replay_required,
            "reason": self.reason,
            "authorization_possible": self.authorization_possible,
        }


@dataclass(frozen=True, slots=True)
class BargeInOutcome:
    """一次播放侧事件的编排结果。``to_details()`` 只含数值与固定短代码。"""

    frame_number: int
    duration_ms: int
    state: BargeInState
    action: str
    reason: str
    level: float
    target_level: float
    continue_playback: bool
    handoff_to_turn: bool
    utterance_id: str | None
    pre_roll_frames: int
    pre_roll_duration_ms: int
    prompt: PromptPolicy

    @property
    def ducking(self) -> bool:
        return self.level < 1.0

    @property
    def resume_reply_submission(self) -> bool:
        """旧回复是否还能继续提交剩余文本；确认打断后**永远**为 False。"""

        return self.continue_playback and not self.handoff_to_turn

    def to_details(self) -> dict[str, object]:
        return {
            "frame_number": self.frame_number,
            "duration_ms": self.duration_ms,
            "state": self.state.value,
            "action": self.action,
            "reason": self.reason,
            "level": round(self.level, 4),
            "target_level": round(self.target_level, 4),
            "continue_playback": self.continue_playback,
            "handoff_to_turn": self.handoff_to_turn,
            "resume_reply_submission": self.resume_reply_submission,
            "pre_roll_frames": self.pre_roll_frames,
            "pre_roll_duration_ms": self.pre_roll_duration_ms,
            "prompt": self.prompt.to_details(),
        }


class BargeInPlaybackController:
    """播放期间的打断编排控制器（纯离线、确定性）。

    典型用法（``frame_number`` 决定时间语义，不使用墙上时钟）::

        controller = BargeInPlaybackController()
        for index, frame in enumerate(frames):
            outcome = controller.play_frame(index, observation)
            apply_volume(outcome.level)
            if not outcome.continue_playback:
                break
            if outcome.handoff_to_turn:
                start_user_turn(outcome.utterance_id, controller.take_pre_roll())
    """

    def __init__(
        self,
        *,
        config: BargeInConfig | None = None,
        prompt_state: str = PROMPT_STATE_VALID,
    ) -> None:
        self._controller = BargeInController(config=config)
        self._config = self._controller.config
        self._fade_steps = max(1, -(-self._config.restore_ms // self._config.frame_duration_ms))
        self._restore_started_at_ms: int | None = None
        self._restore_start_level = 1.0
        self._episode_level = 1.0
        self._prompt = PromptPolicy(state=prompt_state)

    # ---------------------------------------------------------------- properties

    @property
    def config(self) -> BargeInConfig:
        return self._config

    @property
    def state(self) -> BargeInState:
        return self._controller.state

    @property
    def level(self) -> float:
        return self._controller.level

    @property
    def prompt(self) -> PromptPolicy:
        return self._prompt

    @property
    def fade_steps(self) -> int:
        """平滑恢复所需帧数（``restore_ms / frame_duration_ms`` 向上取整）。"""

        return self._fade_steps

    def take_pre_roll(self) -> tuple[BargeInObservation, ...]:
        return self._controller.take_pre_roll()

    # ---------------------------------------------------------------- core

    def play_frame(self, frame_number: int, observation: BargeInObservation) -> BargeInOutcome:
        """处理播放期间的一帧。``frame_number`` 必须从 0 起单调递增。"""

        if isinstance(frame_number, bool) or not isinstance(frame_number, int) or frame_number < 0:
            raise ContractError("frame_number 必须是非负整数")
        if not isinstance(observation, BargeInObservation):
            raise ContractError("observation 必须是 BargeInObservation")

        if self._controller.state is BargeInState.CONFIRMED:
            # 已确认打断：不回退、不恢复；旧回复继续提交被禁止。
            self._episode_level = 0.0
            return self._build(
                frame_number,
                observation,
                action=ACTION_CONFIRMED,
                reason=OUTCOME_REASON_CONFIRMED,
                level=0.0,
                target=0.0,
                continue_playback=False,
                handoff=True,
            )

        decision = self._controller.observe(observation)
        if decision.state is BargeInState.CONFIRMED:
            self._invalidate_prompt_on_barge_in()
            self._episode_level = 0.0
            return self._build(
                frame_number,
                observation,
                action=ACTION_CONFIRMED,
                reason=OUTCOME_REASON_CONFIRMED,
                level=0.0,
                target=0.0,
                continue_playback=False,
                handoff=True,
            )

        level = self._level_for(decision, observation)
        action, reason = self._action_and_reason(decision, level)
        return self._build(
            frame_number,
            observation,
            action=action,
            reason=reason,
            level=level,
            target=self._config.duck_fraction if level < 1.0 else 1.0,
            continue_playback=True,
            handoff=False,
        )

    def playback_ended(self, frame_number: int) -> BargeInOutcome:
        """播放自然结束（不是被插话打断）：恢复原音量并停止判定。"""

        if isinstance(frame_number, bool) or not isinstance(frame_number, int) or frame_number < 0:
            raise ContractError("frame_number 必须是非负整数")
        observation = BargeInObservation(
            at_ms=frame_number * self._config.frame_duration_ms,
            voice_probability=0.0,
            playback_active=False,
        )
        self._controller.release(observation)
        self._restore_started_at_ms = None
        self._episode_level = 1.0
        return self._build(
            frame_number,
            observation,
            action="restore",
            reason=OUTCOME_REASON_ENDED,
            level=1.0,
            target=1.0,
            continue_playback=False,
            handoff=False,
        )

    def mark_prompt_completed(self, *, replay_confirmed: bool) -> PromptPolicy:
        """标记提示已**完整**重播并完成确认；``replay_confirmed=False`` 时保持失效。

        该方法只描述提示状态，不产生任何授权事实。
        """

        if not isinstance(replay_confirmed, bool):
            raise ContractError("replay_confirmed 必须是布尔值")
        if self._prompt.state == PROMPT_STATE_INVALIDATED and not replay_confirmed:
            self._prompt = PromptPolicy(
                state=PROMPT_STATE_INVALIDATED,
                replay_required=True,
                reason=PROMPT_REASON_REPLAY_INCOMPLETE,
            )
        else:
            self._prompt = PromptPolicy(
                state=PROMPT_STATE_COMPLETED if replay_confirmed else self._prompt.state,
                replay_required=False,
                reason=PROMPT_REASON_REPLAY_COMPLETED,
            )
        return self._prompt

    def reset(self) -> None:
        self._controller.reset()
        self._restore_started_at_ms = None
        self._restore_start_level = 1.0
        self._episode_level = 1.0
        self._prompt = PromptPolicy()

    # ---------------------------------------------------------------- internals

    def _level_for(self, decision: BargeInDecision, observation: BargeInObservation) -> float:
        if decision.action == ACTION_DUCK:
            self._restore_started_at_ms = None
            self._episode_level = self._config.duck_fraction
            return self._episode_level
        if self._episode_level >= 1.0:
            self._restore_started_at_ms = None
            return 1.0
        if self._restore_started_at_ms is None:
            # 从当前降级音量开始按帧数线性回升（平滑恢复，不跳变）。
            self._restore_started_at_ms = observation.at_ms
            self._restore_start_level = self._episode_level
        elapsed_frames = max(
            0, (observation.at_ms - self._restore_started_at_ms) // self._config.frame_duration_ms
        )
        progress = min(1.0, (elapsed_frames + 1) / self._fade_steps)
        level = min(1.0, self._restore_start_level + (1.0 - self._restore_start_level) * progress)
        if level >= 1.0:
            level = 1.0
            self._restore_started_at_ms = None
        self._episode_level = level
        return level

    def _action_and_reason(self, decision: BargeInDecision, level: float) -> tuple[str, str]:
        """动作与原因按**状态机判定**决定，音量只描述结果，不能反过来覆盖原因。"""

        if decision.action == ACTION_DUCK:
            return ACTION_DUCK, OUTCOME_REASON_DUCKING
        if level < 1.0:
            return ACTION_RESTORE, OUTCOME_REASON_RESTORING
        return decision.action, OUTCOME_REASON_PLAYBACK

    def _invalidate_prompt_on_barge_in(self) -> None:
        if self._prompt.state != PROMPT_STATE_COMPLETED:
            self._prompt = PromptPolicy(
                state=PROMPT_STATE_INVALIDATED,
                replay_required=True,
                reason=PROMPT_REASON_BARGE_IN,
            )

    def _build(
        self,
        frame_number: int,
        observation: BargeInObservation,
        *,
        action: str,
        reason: str,
        level: float,
        target: float,
        continue_playback: bool,
        handoff: bool,
    ) -> BargeInOutcome:
        pre_roll: BargeInPreRoll = self._controller.pre_roll()
        utterance_id: str | None = None
        if handoff:
            utterance_id = f"utterance-{self._controller.frames_seen}-{observation.at_ms}"
            require_identifier("utterance_id", utterance_id)
        return BargeInOutcome(
            frame_number=frame_number,
            duration_ms=frame_number * self._config.frame_duration_ms,
            state=self._controller.state,
            action=action,
            reason=reason,
            level=round(min(1.0, max(0.0, level)), 6),
            target_level=target,
            continue_playback=continue_playback,
            handoff_to_turn=handoff,
            utterance_id=utterance_id,
            pre_roll_frames=pre_roll.frames,
            pre_roll_duration_ms=pre_roll.duration_ms,
            prompt=self._prompt,
        )
