"""P10 批次 R1a：两阶段打断（疑似插话 / 真人插话）的纯离线状态机。

本模块只描述**播放期间输入边界**的状态与时间语义，是设计文档
``implementation_plan/12-barge-in-and-aec.md``「已确认设计：两阶段打断」的离线核心。

硬边界（离线纯逻辑模块）：
- 不访问麦克风/扬声器/设备、不读写文件、不联网、不创建线程或定时器；
- 只导入 stdlib 与 ``app.contracts.base``；不导入 ``app.safety``/``app.executor``/
  ``app.plans``，不导入语音闭环驱动（避免拉起音频依赖）；
- 打断**不等于授权**：本模块只产出"音量/播放/轮次边界"的动作，不产生授权、计划或
  执行事实，也不消费任何工具面。

两阶段语义（时间语义对已交付的帧计时是确定性的）：
- 第一阶段「疑似插话」：检测到疑似真人开口后立刻进入降音量（目标约 100 ms 内），
  只改音量；**不**建立用户轮次、**不**产生 Agent 请求或授权；
- 第二阶段「确认真人插话」：在约 200~300 ms 持续可信人声后确认打断；
- 误判（回声/键盘/咳嗽/碰撞/短暂噪声）→ 平滑恢复原音量并继续播报，不建立用户轮次。

fail closed 规则：
- 降级级别只会**提高**确认门槛，绝不会把"未知/不可用"当成"已确认"；
- 缺少扬声器参考相关性（``echo_correlation is None``）时，必须在**播放中且仍有未播音频**
  的条件下才允许升级；没有任何回声依据时不允许更敏感；
- 非有限数值、非法时间戳、类型不符一律``BargeInError``（只含固定短代码），不猜测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_positive

# 动作（fail closed：动作是"音量/播放/轮次边界"，不是授权事实）
ACTION_IDLE = "idle"
ACTION_DUCK = "duck"
ACTION_RESTORE = "restore"
ACTION_CONFIRMED = "confirmed"

# 状态迁移原因（固定短代码）
REASON_PLAYBACK_ONLY = "playback_only"
REASON_SUSPECTED = "suspected_barge_in"
REASON_CONFIRMED = "user_barge_in"
REASON_FALSE_TRIGGER = "false_trigger"
REASON_PLAYBACK_ENDED = "playback_ended"
REASON_RESET = "reset"
REASON_COOLDOWN = "barge_in_cooldown"
"""播放开始后的**冷却窗口**内不进入疑似/确认（方案 C 兜底）。

依据（P10 R4-B-2 阶段 0 + 研究文档 §4.C）：本机拿不到可信回声依据、真 AEC 又会削用户语音，
因此用"轮次边界冷却"（LiveKit ``backchannel_boundary`` 式）把播放刚起步的那段时间排除在
插话判定之外——冷却期内帧照收、前置缓冲照存，但**不降音量、不建立疑似/确认**。
"""

# 降级级别
DEGRADE_FULL = "full"
DEGRADE_NO_AEC = "no_aec"
DEGRADE_STRICT = "strict"

DEGRADATION_LEVELS = (DEGRADE_FULL, DEGRADE_NO_AEC, DEGRADE_STRICT)

DEFAULT_SUSPECT_THRESHOLD = 0.6
DEFAULT_CONFIRM_THRESHOLD = 0.7
DEFAULT_STRICT_CONFIRM_THRESHOLD = 0.9

_ERROR_CODES = frozenset({"unknown_degradation_level", "invalid_transition"})


class BargeInError(ContractError):
    """打断状态机拒绝输入。

    ``str()`` 只返回固定短代码，不带音频内容、路径或异常正文。
    """

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "invalid_transition"
        super().__init__(self.code)


class BargeInState(str, Enum):
    """两阶段打断的状态。"""

    IDLE = "idle"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class BargeInConfig:
    """两阶段打断参数；时间单位为毫秒，音量为 0~1 的比例。

    默认值取设计文档 12 的初始建议区间（约 100 ms 内降音量、降至 20%~30%、
    保留至少约 300 ms 前置缓冲、约 200~300 ms 持续人声后确认），并可按阶段 13
    的调优结果覆盖。
    """

    duck_fraction: float = 0.25
    duck_deadline_ms: int = 100
    pre_roll_ms: int = 300
    confirm_window_ms: int = 250
    restore_ms: int = 200
    release_ms: int = 400
    frame_duration_ms: int = 20
    suspect_threshold: float = DEFAULT_SUSPECT_THRESHOLD
    confirm_threshold: float = DEFAULT_CONFIRM_THRESHOLD
    strict_confirm_threshold: float = DEFAULT_STRICT_CONFIRM_THRESHOLD
    max_pre_roll_frames: int = 64
    min_suspect_frames: int = 1
    """进入「疑似」所需的最少**连续**命中帧数（默认 1 保持既有语义）。

    真机教训（2026-10-02）：一帧达到 ``suspect_threshold`` 就降音量时，扬声器回声产生的
    零星命中会反复触发"降音量→回升"（听感上音量一直在变）。把它调大（会话层默认 3，
    即 60 ms）可把短促回声挡在第一阶段之外，真人开口仍会被快速接住。
    """

    cooldown_ms: int = 0
    """播放开始后的**冷却窗口**：窗口内不进入疑似/确认（默认 0 = 关闭，行为不变）。

    方案 C 兜底（见模块内 ``REASON_COOLDOWN``）：冷却期内**仍然**收帧并填前置缓冲
    （真人插话的音频不会丢），只是不降音量、不积累确认窗口；窗口一过按原有门槛判定。
    """

    def __post_init__(self) -> None:
        for name in (
            "duck_deadline_ms",
            "pre_roll_ms",
            "confirm_window_ms",
            "restore_ms",
            "release_ms",
            "frame_duration_ms",
            "max_pre_roll_frames",
            "min_suspect_frames",
        ):
            require_positive(name, getattr(self, name))
        cooldown = self.cooldown_ms
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
            raise ContractError("cooldown_ms 必须是非负整数毫秒")
        duck = _require_fraction("duck_fraction", self.duck_fraction)
        suspect = _require_fraction("suspect_threshold", self.suspect_threshold)
        confirm = _require_fraction("confirm_threshold", self.confirm_threshold)
        strict = _require_fraction("strict_confirm_threshold", self.strict_confirm_threshold)
        if duck >= 1.0:
            raise ContractError("duck_fraction 必须小于 1")
        if confirm < suspect:
            raise ContractError("confirm_threshold 不得低于 suspect_threshold")
        if strict < confirm:
            raise ContractError("strict_confirm_threshold 不得低于 confirm_threshold")
        if self.pre_roll_ms < 300:
            raise ContractError("pre_roll_ms 不得低于 300（设计文档 12 的最低下限）")
        object.__setattr__(self, "duck_fraction", duck)
        object.__setattr__(self, "suspect_threshold", suspect)
        object.__setattr__(self, "confirm_threshold", confirm)
        object.__setattr__(self, "strict_confirm_threshold", strict)

    @property
    def pre_roll_frames(self) -> int:
        """凑满 ``pre_roll_ms`` 需要的帧容量（且不超过 ``max_pre_roll_frames``）。

        容量比"纯时长除法"多一帧：疑似触发那一帧也会进入缓冲，要保证触发后仍能取出
        至少 ``pre_roll_ms`` 的音频（``(容量-1) * frame_duration_ms >= pre_roll_ms``）。
        """

        needed = -(-self.pre_roll_ms // self.frame_duration_ms) + 1
        return min(needed, self.max_pre_roll_frames)

    @property
    def confirm_frames(self) -> int:
        """在 ``confirm_window_ms`` 内需要连续满足阈值的帧数（向上取整）。"""

        return -(-self.confirm_window_ms // self.frame_duration_ms)

    def confirm_threshold_for(self, degradation: str) -> float:
        """按降级级别给出确认阈值：降级只会提高门槛。"""

        if degradation == DEGRADE_STRICT:
            return self.strict_confirm_threshold
        return self.confirm_threshold


def _require_fraction(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"{name} 必须是有限数值")
    result = float(value)
    if not 0.0 < result <= 1.0:
        raise ContractError(f"{name} 必须在 (0,1] 内")
    return result


def _require_score(name: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"{name} 必须是有限数值或 None")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ContractError(f"{name} 必须在 [0,1] 内")
    return result


@dataclass(frozen=True, slots=True)
class BargeInObservation:
    """一帧输入边界观察。

    ``voice_probability`` 是 VAD/人声概率，``echo_correlation`` 是扬声器参考音频相关性
    （``None`` 表示当前链路没有可用参考，必须按降级处理，不得当作"无回声"）。
    """

    at_ms: int
    voice_probability: float
    playback_active: bool
    echo_correlation: float | None = None
    playback_queued_ms: int = 0
    rms: float = 0.0
    aec_available: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.at_ms, bool) or not isinstance(self.at_ms, int) or self.at_ms < 0:
            raise ContractError("at_ms 必须是非负整数毫秒")
        voice = self.voice_probability
        if (
            isinstance(voice, bool)
            or not isinstance(voice, (int, float))
            or not math.isfinite(float(voice))
            or not 0.0 <= float(voice) <= 1.0
        ):
            raise ContractError("voice_probability 必须在 [0,1] 内")
        object.__setattr__(self, "voice_probability", float(voice))
        correlation = _require_score("echo_correlation", self.echo_correlation)
        object.__setattr__(self, "echo_correlation", correlation)
        if not isinstance(self.playback_active, bool):
            raise ContractError("playback_active 必须是布尔值")
        if not isinstance(self.aec_available, bool):
            raise ContractError("aec_available 必须是布尔值")
        if isinstance(self.playback_queued_ms, bool) or not isinstance(self.playback_queued_ms, int) or self.playback_queued_ms < 0:
            raise ContractError("playback_queued_ms 必须是非负整数毫秒")
        if isinstance(self.rms, bool) or not isinstance(self.rms, (int, float)) or not math.isfinite(float(self.rms)) or float(self.rms) < 0.0:
            raise ContractError("rms 必须是非负有限数值")
        object.__setattr__(self, "rms", float(self.rms))

    @property
    def in_playback(self) -> bool:
        """是否仍处于"有音频要播"的窗口：已开始播放，或队列里还有待播音频。"""

        return bool(self.playback_active or self.playback_queued_ms > 0)


@dataclass(frozen=True, slots=True)
class BargeInDecision:
    """一次观察后的判定结果；``to_details()`` 只含数值与固定短代码。"""

    state: BargeInState
    action: str
    reason: str
    degradation: str
    level: float
    target_level: float
    duck_deadline_ms: int | None
    pre_roll_frames: int
    confirm_frames: int
    frames_in_window: int
    peak_voice_probability: float
    cooldown_remaining_ms: int | None = None
    """方案 C 冷却窗口剩余毫秒（``None`` = 未启用或已结束）。"""

    cooldown_frames: int = 0
    """本 episode 被冷却窗口挡下的帧数。"""

    @property
    def ducking(self) -> bool:
        return self.level < 1.0

    @property
    def user_turn_created(self) -> bool:
        """只有确认插话才建立用户轮次；疑似/误判都不建立。"""

        return self.state is BargeInState.CONFIRMED

    def to_details(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "action": self.action,
            "reason": self.reason,
            "degradation": self.degradation,
            "level": round(self.level, 4),
            "target_level": round(self.target_level, 4),
            "duck_deadline_ms": self.duck_deadline_ms,
            "pre_roll_frames": self.pre_roll_frames,
            "confirm_frames": self.confirm_frames,
            "frames_in_window": self.frames_in_window,
            "peak_voice_probability": round(self.peak_voice_probability, 4),
            "user_turn_created": self.user_turn_created,
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "cooldown_frames": self.cooldown_frames,
        }


@dataclass(frozen=True, slots=True)
class BargeInPreRoll:
    """前置缓冲的只读摘要：只暴露帧数量与时长，**不**暴露音频内容。"""

    frames: int
    duration_ms: int
    first_at_ms: int | None

    def to_details(self) -> dict[str, object]:
        return {
            "frames": self.frames,
            "duration_ms": self.duration_ms,
            "first_at_ms": self.first_at_ms,
        }


class BargeInController:
    """两阶段打断状态机（纯离线、可注入、确定性）。

    典型用法::

        controller = BargeInController()
        for observation in stream:          # 播放期间的逐帧观察
            decision = controller.observe(observation)
            if decision.state is BargeInState.CONFIRMED:
                ...                           # 由编排层停止播报并进入用户下一轮
            elif decision.ducking:
                ...                           # 只降音量，不建立轮次
    """

    def __init__(self, *, config: BargeInConfig | None = None, degradation: str = DEGRADE_FULL) -> None:
        self._config = config or BargeInConfig()
        self._degradation = _require_degradation(degradation)
        self._state = BargeInState.IDLE
        self._level = 1.0
        self._last_at_ms: int | None = None
        self._pre_roll: list[BargeInObservation] = []
        self._window: list[float] = []
        self._duck_started_at_ms: int | None = None
        self._frames_seen = 0
        self._suspect_run = 0
        self._cooldown_started_at_ms: int | None = None
        self._cooldown_frames = 0

    # ---------------------------------------------------------------- properties

    @property
    def config(self) -> BargeInConfig:
        return self._config

    @property
    def degradation(self) -> str:
        return self._degradation

    @property
    def state(self) -> BargeInState:
        return self._state

    @property
    def level(self) -> float:
        """当前输出音量比例（1.0 = 原音量）。"""

        return self._level

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    @property
    def cooldown_frames(self) -> int:
        """本次 episode 里被冷却窗口挡下的帧数（0 = 没有发生过冷却判定）。"""

        return self._cooldown_frames

    @property
    def cooldown_remaining_ms(self) -> int | None:
        """冷却窗口剩余毫秒；未启用/未开始/已结束均为 ``None``。"""

        if self._config.cooldown_ms <= 0 or self._cooldown_started_at_ms is None:
            return None
        if self._last_at_ms is None:
            return self._config.cooldown_ms
        remaining = self._config.cooldown_ms - (self._last_at_ms - self._cooldown_started_at_ms)
        return max(0, remaining)

    @property
    def cooldown_ms(self) -> int:
        return self._config.cooldown_ms

    def pre_roll(self) -> BargeInPreRoll:
        """当前前置缓冲摘要。

        ``duration_ms`` 按"帧数 × 帧时长"计（不是首末帧跨度），因为每帧都贡献完整帧时长。
        缓冲**容量**保证 >= ``pre_roll_ms``；实际填充量受会话已走过的帧数限制——确认窗口
        默认 250 ms 早于 300 ms，因此正常的确认流程不会丢帧，但也不会凭空多出未观察过的音频。
        """

        if not self._pre_roll:
            return BargeInPreRoll(frames=0, duration_ms=0, first_at_ms=None)
        return BargeInPreRoll(
            frames=len(self._pre_roll),
            duration_ms=len(self._pre_roll) * self._config.frame_duration_ms,
            first_at_ms=self._pre_roll[0].at_ms,
        )

    def take_pre_roll(self) -> tuple[BargeInObservation, ...]:
        """取出并清空前置缓冲：确认插话后把这段音频并入**同一个**用户 utterance。

        只返回观察记录（时间戳/概率/时长语义），不含音频载荷；音频始终由调用方持有。
        """

        taken = tuple(self._pre_roll)
        self._pre_roll.clear()
        return taken

    def mark_confirmed(self) -> None:
        """把状态直接推到已确认（用于"用户插话"由外部规则先判定的接线场景）。

        fail closed：当前处于疑似态才允许升级，否则 ``BargeInError("invalid_transition")``。
        """

        self._transition(BargeInState.CONFIRMED, REASON_CONFIRMED)
        self._level = 0.0

    # ---------------------------------------------------------------- core

    def observe(self, observation: BargeInObservation) -> BargeInDecision:
        """吃进一帧观察并给出判定；时间戳必须单调不减。"""

        if not isinstance(observation, BargeInObservation):
            raise ContractError("observation 必须是 BargeInObservation")
        if self._last_at_ms is not None and observation.at_ms < self._last_at_ms:
            raise ContractError("at_ms 必须单调不减")
        self._last_at_ms = observation.at_ms
        self._frames_seen += 1

        if self._state is BargeInState.CONFIRMED:
            return self._decide(ACTION_CONFIRMED, REASON_CONFIRMED, observation)

        if not observation.in_playback:
            # 没有音频要播：任何疑似都不成立，直接回到原音量。
            self._transition(BargeInState.IDLE, REASON_PLAYBACK_ENDED)
            self._reset_window()
            self._suspect_run = 0
            self._level = 1.0
            self._duck_started_at_ms = None
            # 本 episode 结束：下一次播放开始时冷却窗口**重新**计时。
            self._cooldown_started_at_ms = None
            return self._decide(ACTION_RESTORE, REASON_PLAYBACK_ENDED, observation)

        self._remember_pre_roll(observation)

        if self._state is not BargeInState.SUSPECTED and self._cooldown_active(observation):
            # 方案 C 冷却窗口：收帧、填前置缓冲，但**不降音量、不建立疑似/确认**。
            self._cooldown_frames += 1
            self._suspect_run = 0
            self._reset_window()
            self._level = 1.0
            self._duck_started_at_ms = None
            return self._decide(ACTION_IDLE, REASON_COOLDOWN, observation)

        score = self._score(observation)

        if self._state is BargeInState.SUSPECTED:
            if score < self._config.suspect_threshold:
                # 疑似后音频回落：先清空确认窗口并保持降音量（滞后），持续 ``release_ms``
                # 仍不构成确认才判定为误触发并回 idle。
                self._reset_window()
                self._level = self._config.duck_fraction
                if self._duck_started_at_ms is not None and (
                    observation.at_ms - self._duck_started_at_ms >= self._config.release_ms
                ):
                    self._transition(BargeInState.IDLE, REASON_FALSE_TRIGGER)
                    self._level = 1.0
                    self._duck_started_at_ms = None
                    return self._decide(ACTION_RESTORE, REASON_FALSE_TRIGGER, observation)
                return self._decide(ACTION_DUCK, REASON_SUSPECTED, observation)
            self._window.append(score)
            self._trim_window(observation.at_ms)
            if self._confirmable(observation):
                self._transition(BargeInState.CONFIRMED, REASON_CONFIRMED)
                self._level = 0.0
                return self._decide(ACTION_CONFIRMED, REASON_CONFIRMED, observation)
            self._level = self._config.duck_fraction
            return self._decide(ACTION_DUCK, REASON_SUSPECTED, observation)

        if score >= self._config.suspect_threshold:
            self._suspect_run += 1
            if self._suspect_run < self._config.min_suspect_frames:
                # 命中还没达到"最小连续帧数"：**不降音量**、不建立任何疑似事实
                # （真机教训：零星回声会让音量反复跳动）。
                self._level = 1.0
                return self._decide(ACTION_IDLE, REASON_PLAYBACK_ONLY, observation)
            self._suspect_run = 0
            self._transition(BargeInState.SUSPECTED, REASON_SUSPECTED)
            self._window = [score]
            self._level = self._config.duck_fraction
            self._duck_started_at_ms = observation.at_ms
            return self._decide(ACTION_DUCK, REASON_SUSPECTED, observation)

        self._suspect_run = 0
        self._level = 1.0
        return self._decide(ACTION_IDLE, REASON_PLAYBACK_ONLY, observation)

    def release(self, observation: BargeInObservation) -> BargeInDecision:
        """声明"疑似插话是误判"（例如参考相关性复核或人声特征复核失败）。

        只做平滑恢复：回到 ``IDLE`` 与原音量，不建立用户轮次、不产生任何输入事实。
        """

        if not isinstance(observation, BargeInObservation):
            raise ContractError("observation 必须是 BargeInObservation")
        if self._last_at_ms is not None and observation.at_ms < self._last_at_ms:
            raise ContractError("at_ms 必须单调不减")
        self._last_at_ms = observation.at_ms
        self._transition(BargeInState.IDLE, REASON_FALSE_TRIGGER)
        self._level = 1.0
        self._duck_started_at_ms = None
        self._suspect_run = 0
        self._reset_window()
        return self._decide(ACTION_RESTORE, REASON_FALSE_TRIGGER, observation)

    def reset(self) -> None:
        """回到初始态（新一轮播报开始或会话结束时调用）。"""

        self._state = BargeInState.IDLE
        self._level = 1.0
        self._last_at_ms = None
        self._pre_roll.clear()
        self._reset_window()
        self._duck_started_at_ms = None
        self._suspect_run = 0
        self._cooldown_started_at_ms = None

    # ---------------------------------------------------------------- internals

    def _cooldown_active(self, observation: BargeInObservation) -> bool:
        """冷却窗口内返回 ``True``；窗口起点取本 episode 的**第一帧播放中观察**。"""

        if self._config.cooldown_ms <= 0:
            return False
        if self._cooldown_started_at_ms is None:
            self._cooldown_started_at_ms = observation.at_ms
        return observation.at_ms - self._cooldown_started_at_ms < self._config.cooldown_ms

    def _score(self, observation: BargeInObservation) -> float:
        """加权人声得分：有回声相关性时按回声风险折减，无参考时按降级取原值。"""

        voice = observation.voice_probability
        correlation = observation.echo_correlation
        if correlation is None:
            if self._degradation == DEGRADE_FULL:
                # 声称完整能力却没有参考：按降级处理，只允许更保守的判定。
                self._degradation = DEGRADE_STRICT
            return voice
        # 回声越强，越可能是自己在说话：折减但不归零，保留人声特征的作用。
        return max(0.0, voice - correlation)

    def _confirmable(self, observation: BargeInObservation) -> bool:
        threshold = self._config.confirm_threshold_for(self._degradation)
        if observation.voice_probability < threshold:
            return False
        if self._degradation == DEGRADE_STRICT and observation.echo_correlation is None:
            # 没有任何回声依据：只允许"播放仍在进行"的窗口内升级，且不放宽门槛。
            if not observation.in_playback:
                return False
        return len(self._window) >= self._config.confirm_frames

    def _remember_pre_roll(self, observation: BargeInObservation) -> None:
        self._pre_roll.append(observation)
        limit = self._config.pre_roll_frames
        if len(self._pre_roll) > limit:
            del self._pre_roll[: len(self._pre_roll) - limit]

    def _trim_window(self, now_ms: int) -> None:
        if self._duck_started_at_ms is None:
            return
        elapsed = now_ms - self._duck_started_at_ms
        allowed = max(1, -(-elapsed // self._config.frame_duration_ms) + 1)
        if len(self._window) > allowed:
            del self._window[: len(self._window) - allowed]

    def _reset_window(self) -> None:
        self._window.clear()

    def _transition(self, state: BargeInState, reason: str) -> None:
        if state is self._state:
            return
        allowed = {
            BargeInState.IDLE: {BargeInState.SUSPECTED, BargeInState.IDLE},
            BargeInState.SUSPECTED: {BargeInState.CONFIRMED, BargeInState.IDLE},
            BargeInState.CONFIRMED: {BargeInState.CONFIRMED, BargeInState.IDLE},
        }
        if state not in allowed[self._state]:
            raise BargeInError("invalid_transition")
        self._state = state
        if reason == REASON_RESET:
            self._reset_window()

    def _decide(self, action: str, reason: str, observation: BargeInObservation) -> BargeInDecision:
        deadline = None
        if action == ACTION_DUCK and self._duck_started_at_ms is not None:
            elapsed = observation.at_ms - self._duck_started_at_ms
            deadline = max(0, self._config.duck_deadline_ms - elapsed)
        peak = max(self._window) if self._window else 0.0
        return BargeInDecision(
            state=self._state,
            action=action,
            reason=reason,
            degradation=self._degradation,
            level=self._level,
            target_level=self._config.duck_fraction if action == ACTION_DUCK else self._level,
            duck_deadline_ms=deadline,
            pre_roll_frames=self._config.pre_roll_frames,
            confirm_frames=self._config.confirm_frames,
            frames_in_window=len(self._window),
            peak_voice_probability=peak,
            cooldown_remaining_ms=self.cooldown_remaining_ms,
            cooldown_frames=self._cooldown_frames,
        )


def _require_degradation(value: object) -> str:
    if isinstance(value, str) and value in DEGRADATION_LEVELS:
        return value
    raise BargeInError("unknown_degradation_level")
