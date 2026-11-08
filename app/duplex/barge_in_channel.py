"""P10 批次 R4：播放期间的**独立打断通道**（自有采集身份 + 前置缓冲 + 音量语义）。

本模块是 R1a/R1b 纯离线判定与真实音频链路之间的**唯一**接线层：它自己开一路采集
（自有 :class:`~app.audio.bus.AudioBus` 身份、自有采集会话），在播报期间逐帧观察，
把观察喂给 :class:`~app.duplex.barge_in_orchestration.BargeInPlaybackController`，
并按判定结果输出音量级别与"确认插话"事实。

与 ``interrupt_source`` 的关系（硬边界）：
- 本通道**不复用** ``app.main`` 的 ``interrupt_source``（那是"中止整个会话"的键盘轮询口），
  也不导入 ``app.main``；打断只影响"本次播报的音量与停止"，不终止会话、不改变输入模式；
- 本通道不产生任何授权/计划/执行事实，不导入 ``app.safety``/``app.executor``/``app.plans``。

不伪装的规则：
- 拿不到可用参考（未配置参考、参考与采集时钟不一致、参考窗口不可用）时
  ``echo_correlation`` 一律为 ``None``，并如实登记参考状态；这会让 R1a 自动降级到更保守
  的门槛（``strict``），**绝不**把"没有回声依据"当成"没有回声"；
- 人声概率按可观测事实映射（检测器命中=1.0 / 能量=rms÷阈值），**不编造**概率分布；
- 采集装不上、总线失效、观察失败：一律"不打断 + 固定原因码"，保持原播报，绝不假装有
  打断能力（也绝不因为通道故障停掉播报）。
"""

from __future__ import annotations

import collections
from collections.abc import Callable
from dataclasses import dataclass, replace
import inspect
import math
import threading
import time
from typing import Any

import numpy as np

from app.contracts.base import ContractError, require_positive
from app.audio.aec.double_talk import (
    DT_MODE_OFF,
    DT_MODE_PROTECT,
    DT_STATES,
    DoubleTalkConfig,
    DoubleTalkDetector,
    DoubleTalkObservation,
    double_talk_config_ok,
)
from app.duplex.barge_in import (
    BargeInConfig,
    BargeInObservation,
    BargeInState,
    DEGRADATION_LEVELS,
    DEGRADE_FULL,
)
from app.duplex.barge_in_orchestration import (
    PROMPT_STATE_VALID,
    BargeInOutcome,
    BargeInPlaybackController,
)

FRAME_MS = 20
"""通道帧时长（毫秒）；与采集帧（16 kHz / 20 ms / 320 样本 / 640 字节）一致。"""

CAPTURE_SAMPLE_RATE_HZ = 16000
"""采集时钟（Hz）：参考要参与判定必须与它同钟。"""

CAPTURE_FRAME_BYTES = 640
"""16 kHz / 20 ms / 单声道 / PCM16 的帧字节数。"""

CHANNEL_AUDIO_SESSION_ID = "p10-barge-in-channel"
"""打断通道自有音频会话身份（**不是**轮次采集的身份）。"""

AEC_REASON_PROCESS_FAILED = "aec_process_failed"
AEC_REASON_OUTPUT_INVALID = "aec_output_invalid"
AEC_REASON_REFERENCE_SILENT = "reference_silent"
"""真 AEC 的固定失败/状态原因码（与 `webrtc_backend` 的 `reference_silent` 同字面值）。"""

CHANNEL_STREAM_ID = "p10-barge-in-channel-stream"
"""打断通道自有流身份。"""

CHANNEL_STOP_REASON = "barge-in-monitor"
"""通道采集停止原因码（固定短代码）。"""

REFERENCE_SOURCE_NONE = "none"
REFERENCE_SOURCE_UNAVAILABLE = "unavailable"
REFERENCE_SOURCE_CLOCK_MISMATCH = "clock_mismatch"

# 回声可观测性判定的固定状态码（R4-B 收紧；只用于如实上报"为什么还没证明"）
PROOF_STATE_NO_ESTIMATE = "no_estimate"
PROOF_STATE_ALIGNMENT_NOT_OK = "alignment_not_ok"
PROOF_STATE_LOW_CORRELATION = "correlation_below_threshold"
PROOF_STATE_DELAY_IMPLAUSIBLE = "delay_implausible"
PROOF_STATE_ACCUMULATING = "accumulating"
PROOF_STATE_PROVEN = "echo_proven"
PROOF_STATES = (
    PROOF_STATE_NO_ESTIMATE,
    PROOF_STATE_ALIGNMENT_NOT_OK,
    PROOF_STATE_LOW_CORRELATION,
    PROOF_STATE_DELAY_IMPLAUSIBLE,
    PROOF_STATE_ACCUMULATING,
    PROOF_STATE_PROVEN,
)

PROBABILITY_MODE_DETECTOR = "detector"
PROBABILITY_MODE_ENERGY = "energy"
PROBABILITY_MODES = (PROBABILITY_MODE_DETECTOR, PROBABILITY_MODE_ENERGY)

REASON_ARMED = "armed"
REASON_NOT_ARMED = "not_armed"
REASON_CAPTURE_UNAVAILABLE = "capture_unavailable"
REASON_CAPTURE_STOPPED = "capture_stopped"
REASON_PLAYBACK_ENDED = "playback_ended"
REASON_TIMEOUT = "monitor_timeout"
REASON_OBSERVING = "observing"
REASON_CONFIRMED = "barge_in_confirmed"
REASON_DISARMED = "disarmed"
REASON_DUCK_DISABLED_NO_REFERENCE = "duck_disabled_no_reference"
"""没有可用回声参考时的如实原因码：**不降音量**（只保留确认后停播）。"""

DEFAULT_MIN_SUSPECT_FRAMES = 3
"""会话层默认的"最小连续疑似帧数"（3 帧 = 60 ms）。

真机教训（2026-10-02）：单帧即降音量会被扬声器回声反复触发，听感上是"音量一直在变"；
60 ms 的连续命中要求能挡掉零星回声，同时不耽误真人开口。
"""

DEFAULT_RELEASE_MS = 800
"""会话层默认的误触发滞后（毫秒）：比 R1a 默认 400 ms 更长，减少音量往复次数。"""

DEFAULT_CAPACITY_FRAMES = 512
"""通道总线容量（帧）；20 ms/帧 → 约 10 秒积压上限。"""

DEFAULT_MAX_MONITOR_SECONDS = 120.0
"""单次播报监视的硬上限（秒）；到点即收尾，绝不无限守望。"""

DEFAULT_ENERGY_THRESHOLD = 600.0
"""能量概率映射的默认阈值（PCM16 RMS），与主链路 ``speech_threshold`` 同口径。"""


def _require_unit_fraction(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} 必须是数值")
    result = float(value)
    if not math.isfinite(result) or not 0.0 < result <= 1.0:
        raise ContractError(f"{name} 必须在 (0,1] 内")
    return result


def _require_positive_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} 必须是数值")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ContractError(f"{name} 必须是正数")
    return result


@dataclass(frozen=True, slots=True)
class BargeInChannelConfig:
    """独立打断通道参数；时间单位为毫秒，音量是 0~1 的比例。"""

    barge_in: BargeInConfig = BargeInConfig(
        min_suspect_frames=DEFAULT_MIN_SUSPECT_FRAMES, release_ms=DEFAULT_RELEASE_MS
    )
    degradation: str = DEGRADE_FULL
    capacity_frames: int = DEFAULT_CAPACITY_FRAMES
    max_monitor_seconds: float = DEFAULT_MAX_MONITOR_SECONDS
    energy_threshold: float = DEFAULT_ENERGY_THRESHOLD
    probability_mode: str = PROBABILITY_MODE_ENERGY
    reference_sample_rate: int | None = None
    prompt_state: str = PROMPT_STATE_VALID
    duck_requires_reference: bool = True
    alignment_interval_frames: int = 25
    """每隔多少帧做一次"回声对齐 + 相关性"估计（25 帧 = 500 ms）。

    真机教训（R4-A）：点击式单帧相关没有意义——必须先在**数百毫秒**的量级上找到声学
    延迟，再看该延迟下的相关性；估计本身约 30~50 ms，因此按 0.5 s 一次做，其余帧沿用
    最近一次结论。
    """
    alignment_history_frames: int = 100
    """用于对齐估计的最近采集帧数（100 帧 = 2 秒）。"""
    cooldown_ms: int | None = None
    """方案 C 冷却窗口覆盖值（``None`` = 不改动 ``barge_in.cooldown_ms``）。

    单独放在通道配置里，是为了**避免**会话层为了设一个冷却时长而重建整个
    ``BargeInConfig``——那正是早期把 ``min_suspect_frames=3``/``release_ms=800``
    覆盖回 1/400 的缺陷来源。
    """
    echo_proof_correlation: float = 0.2
    """**回声可观测性门槛**：对齐后相关性达到该值，才算"我们确实能看见自己的回声"。

    R4-A 的安全门：只有先证明"参考与采集之间存在可测的声学耦合"，才允许把确认插话真正
    落到停播上；否则本次 episode 只观测不动手（如实记原因）。**默认 fail-closed**：
    拿不到证明时宁可不停播，也绝不靠猜门槛停播（真机第三轮 6/6 假确认的教训）。
    """
    echo_proof_delay_min_ms: int = 0
    echo_proof_delay_max_ms: int = 500
    """回声延迟的**物理合理窗口**（毫秒，含端点）。超出范围的对齐估计一律不算证明。

    真机教训（R4-B，2026-10-03）：本机相关性能到 0.60~0.75，但估计延迟出现 **746 / 1589 /
    −780 ms**（本机实测参考→采集延迟只有 ≈390–410 ms）——负值与 1.6 s 物理上不可能，说明
    对齐匹配到了错误的东西。旧的"只看相关性"门槛因此被伪相关骗过，`echo_proven=True`
    让安全门放行 ⇒ **用户没插话也停播**（turn 2/4 真机事实）。
    """
    echo_proof_consecutive: int = 3
    """需要**连续**多少次合格估计（相关性达标 + 延迟合理且稳定）才算证明；默认 3（≈1.5 s）。"""

    echo_proof_delay_tolerance_ms: int = 50
    """相邻两次合格估计的延迟差允许值（毫秒）；超过就重新计数（防"延迟乱跳但相关性高"）。"""
    use_reference_aec: bool = False
    """R4-B P1：是否用参考源的**真 AEC 后端**（AEC3）处理每一帧采集。

    默认 ``False`` → 与改动前完全一致（参考只用于对齐/相关性）。开启后：
    - 每帧调用 ``reference.process(capture_at_ms=<单调毫秒>, capture=data)``，**用残差**算
      帧电平与能量门；检测器（结构判定）仍看**原始**帧（能量看残差、结构看原帧）。
    - 参考侧的延迟估计会实时喂给 AEC3 的 ``stream_delay_ms``（真机实测该提示敏感：
      0–500 ms 内 ERLE 7–15.5 dB）。
    """
    residual_gate_margin: float | None = None
    """R4-B P2：**残余回声地板感知**的动态能量门倍率（``None`` = 关闭，用固定门）。

    真机实测（`research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`）：AEC3 最佳
    情况下残差 p50 已低于能量门，但仍有 **28%** 的 20 ms 帧超门（TTS 起音尖峰）⇒ 只靠固定门
    仍会自触发。开启后播放期有效门 = ``max(energy_threshold, 残余地板 × margin)``，
    地板取最近 ``residual_floor_frames`` 帧残差的低分位（默认 p20，抗用户说话干扰）。
    """
    residual_floor_frames: int = 50
    """残余地板统计窗口（帧）；50 帧 = 1 秒。"""

    residual_floor_quantile: float = 0.2
    """残余地板分位（0~1）；低分位更接近"只有回声、用户没说话"的地板。"""
    double_talk_mode: str = DT_MODE_OFF
    """R4-B5：**双讲检测（DTD）**档位（``off`` / ``facts`` / ``protect``，默认 off）。

    - ``off``：零新增对象、零逐帧开销（与改动前逐字节一致）；
    - ``facts``（B2）：逐帧记录 `double_talk_*` 只读事实——**不改变**任何判定/音量/停播/
      地板统计；
    - ``protect``（B3）：在 ``facts`` 基础上**只做两件事**：① 把双讲标志透传给参考源的
      ``process()``（NLMS 权重按 `NlmsConfig.double_talk_step_scale` 缩放，默认冻结）；
      ② 双讲帧**不进残余地板统计**。它**不**改变音量、确认、停播与安全门。
    """
    double_talk_config: DoubleTalkConfig | None = None
    """双讲判据阈值（``None`` = 用 `DoubleTalkConfig()` 默认值）。"""
    """没有可用回声参考时是否禁止第一阶段降音量（默认禁止）。

    真机教训（2026-10-02）：拿不到参考就无法把"自己的回声"折减掉，降音量会被回声反复
    触发（听感＝音量一直在变），所以默认**宁可不降音量**，只保留"确认后停播"。
    """

    def __post_init__(self) -> None:
        if not isinstance(self.barge_in, BargeInConfig):
            raise ContractError("barge_in 必须是 BargeInConfig")
        if self.degradation not in DEGRADATION_LEVELS:
            raise ContractError("degradation 必须是已知降级级别")
        require_positive("capacity_frames", self.capacity_frames)
        if not isinstance(self.duck_requires_reference, bool):
            raise ContractError("duck_requires_reference 必须是布尔值")
        require_positive("alignment_interval_frames", self.alignment_interval_frames)
        require_positive("alignment_history_frames", self.alignment_history_frames)
        if self.cooldown_ms is not None:
            if isinstance(self.cooldown_ms, bool) or not isinstance(self.cooldown_ms, int):
                raise ContractError("cooldown_ms 必须是整数毫秒或 None")
            if self.cooldown_ms < 0:
                raise ContractError("cooldown_ms 必须非负")
            # 只覆盖冷却时长，其余字段原样保留。
            object.__setattr__(self, "barge_in", replace(self.barge_in, cooldown_ms=self.cooldown_ms))
        object.__setattr__(
            self,
            "echo_proof_correlation",
            _require_unit_fraction("echo_proof_correlation", self.echo_proof_correlation),
        )
        for name in (
            "echo_proof_delay_min_ms",
            "echo_proof_delay_max_ms",
            "echo_proof_consecutive",
            "echo_proof_delay_tolerance_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{name} 必须是非负整数毫秒")
        if self.echo_proof_delay_min_ms >= self.echo_proof_delay_max_ms:
            raise ContractError("echo_proof_delay_min_ms 必须小于 echo_proof_delay_max_ms")
        if self.echo_proof_consecutive < 1:
            raise ContractError("echo_proof_consecutive 必须 ≥ 1")
        object.__setattr__(
            self,
            "max_monitor_seconds",
            _require_positive_number("max_monitor_seconds", self.max_monitor_seconds),
        )
        object.__setattr__(
            self,
            "energy_threshold",
            _require_positive_number("energy_threshold", self.energy_threshold),
        )
        if self.probability_mode not in PROBABILITY_MODES:
            raise ContractError("probability_mode 只支持 detector 或 energy")
        if self.barge_in.frame_duration_ms != FRAME_MS:
            # 采集帧就是 20 ms；两者不一致会让 at_ms 与真实时间轴静默错位。
            raise ContractError("frame_duration_ms 必须与采集帧时长 20ms 一致")
        if self.reference_sample_rate is not None:
            require_positive("reference_sample_rate", self.reference_sample_rate)
        if not isinstance(self.use_reference_aec, bool):
            raise ContractError("use_reference_aec 必须是布尔值")
        margin = self.residual_gate_margin
        if margin is not None:
            if isinstance(margin, bool) or not isinstance(margin, (int, float)):
                raise ContractError("residual_gate_margin 必须是数值或 None")
            if not math.isfinite(float(margin)) or float(margin) < 1.0:
                raise ContractError("residual_gate_margin 必须 ≥ 1.0（不得把门降到固定门以下）")
            object.__setattr__(self, "residual_gate_margin", float(margin))
        require_positive("residual_floor_frames", self.residual_floor_frames)
        quantile = self.residual_floor_quantile
        if (
            isinstance(quantile, bool)
            or not isinstance(quantile, (int, float))
            or not 0.0 < float(quantile) <= 1.0
        ):
            raise ContractError("residual_floor_quantile 必须在 (0,1] 内")
        object.__setattr__(self, "residual_floor_quantile", float(quantile))
        if not double_talk_config_ok(self.double_talk_mode):
            raise ContractError("double_talk_mode 只支持 off/facts/protect")
        if self.double_talk_config is not None and not isinstance(
            self.double_talk_config, DoubleTalkConfig
        ):
            raise ContractError("double_talk_config 必须是 DoubleTalkConfig 或 None")

    @property
    def pre_roll_frames(self) -> int:
        return self.barge_in.pre_roll_frames

    def to_details(self) -> dict[str, object]:
        return {
            "degradation": self.degradation,
            "capacity_frames": self.capacity_frames,
            "max_monitor_seconds": self.max_monitor_seconds,
            "probability_mode": self.probability_mode,
            "reference_sample_rate": self.reference_sample_rate,
            "prompt_state": self.prompt_state,
            "pre_roll_frames": self.pre_roll_frames,
            "duck_fraction": self.barge_in.duck_fraction,
            "frame_duration_ms": self.barge_in.frame_duration_ms,
            "min_suspect_frames": self.barge_in.min_suspect_frames,
            "release_ms": self.barge_in.release_ms,
            "cooldown_ms": self.barge_in.cooldown_ms,
            "duck_requires_reference": self.duck_requires_reference,
            "alignment_interval_frames": self.alignment_interval_frames,
            "alignment_history_frames": self.alignment_history_frames,
            "echo_proof_correlation": self.echo_proof_correlation,
            "echo_proof_delay_min_ms": self.echo_proof_delay_min_ms,
            "echo_proof_delay_max_ms": self.echo_proof_delay_max_ms,
            "echo_proof_consecutive": self.echo_proof_consecutive,
            "echo_proof_delay_tolerance_ms": self.echo_proof_delay_tolerance_ms,
            "use_reference_aec": self.use_reference_aec,
            "residual_gate_margin": self.residual_gate_margin,
            "residual_floor_frames": self.residual_floor_frames,
            "residual_floor_quantile": self.residual_floor_quantile,
            "double_talk_mode": self.double_talk_mode,
        }


class BargeInChannel:
    """播放期间的独立打断通道（真实 I/O 只在 :meth:`arm` / :meth:`disarm`）。

    典型用法::

        channel = BargeInChannel(environment=environment, config=config)
        if channel.arm():
            outcome = channel.run(is_playback_done=lambda: playback_done.is_set())
            if outcome is not None and outcome.handoff_to_turn:
                stop_playback()                      # 由会话层按安全门决定
                pre_roll = channel.pre_roll_pcm()     # 并入同一 utterance
        channel.disarm()
    """

    def __init__(
        self,
        *,
        environment: Any,
        config: BargeInChannelConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_playback: Callable[[], None] | None = None,
        voice_detector: Callable[[Any], bool] | None = None,
        reference: Any | None = None,
        gain_sink: Callable[[float], None] | None = None,
    ) -> None:
        if not callable(clock):
            raise ContractError("clock 必须可调用")
        if not callable(sleep):
            raise ContractError("sleep 必须可调用")
        if stop_playback is not None and not callable(stop_playback):
            raise ContractError("stop_playback 必须可调用或 None")
        if voice_detector is not None and not callable(voice_detector):
            raise ContractError("voice_detector 必须可调用或 None")
        if gain_sink is not None and not callable(gain_sink):
            raise ContractError("gain_sink 必须可调用或 None")
        self._environment = environment
        self._config = config or BargeInChannelConfig()
        self._clock = clock
        self._sleep = sleep
        self._stop_playback = stop_playback
        self._voice_detector = voice_detector
        self._reference = reference
        self._gain_sink = gain_sink
        self._controller = BargeInPlaybackController(
            config=self._config.barge_in, prompt_state=self._config.prompt_state
        )
        self._controller_applied = False
        if self._config.degradation != DEGRADE_FULL:
            self._apply_degradation(self._config.degradation)
        self._bus: Any | None = None
        self._capture: Any | None = None
        self._armed = False
        self._frames = 0
        self._frame_number = 0
        self._level = 1.0
        self._pre_roll: list[bytes] = []
        self._confirmed = False
        self._last_outcome: BargeInOutcome | None = None
        self._reason = REASON_NOT_ARMED
        self._reference_state = REFERENCE_SOURCE_NONE
        self._last_correlation: float | None = None
        self._stop_requested = threading.Event()
        self._errors = 0
        self._duck_suppressed = False
        self._detector_energy_gate_applied = False
        # R4-A：回声对齐（最近采集帧 + 每 0.5 s 一次的相关性/延迟估计）
        self._capture_history: collections.deque[tuple[int, Any]] = collections.deque(
            maxlen=self._config.alignment_history_frames
        )
        self._alignment: Any | None = None
        self._frames_since_alignment = 0
        self._alignment_estimates = 0
        self._echo_proven = False
        self._max_echo_correlation = 0.0
        # R4-B 收紧：回声证明需要"相关性达标 + 延迟物理合理 + 连续稳定"
        self._proof_state = PROOF_STATE_NO_ESTIMATE
        self._proof_run = 0
        self._proof_last_delay_ms: int | None = None
        self._proof_delay_ms: int | None = None
        # R4-B P1/P2：真 AEC（AEC3）逐帧残差 + 残余回声地板感知的动态门。
        self._residual_levels: collections.deque[float] = collections.deque(
            maxlen=self._config.residual_floor_frames
        )
        self._aec_frames_applied = 0
        self._aec_fallback_reason = ""
        self._aec_erle_db: float | None = None
        self._aec_erle_sufficient = False
        self._aec_stream_delay_ms: int | None = None
        self._residual_echo_floor_pcm16: float | None = None
        self._gate_in_playback: float | None = None
        # R4-B5（B2）：双讲检测器（`off` 时**完全不建对象**，零开销、零行为差异）。
        # 它只读既有事实、只产出只读事实；不参与音量/确认/停播/地板统计的任何判定。
        self._double_talk_detector: DoubleTalkDetector | None = (
            None
            if self._config.double_talk_mode == DT_MODE_OFF
            else DoubleTalkDetector(
                config=self._config.double_talk_config, mode=self._config.double_talk_mode
            )
        )
        self._double_talk_last: dict[str, object] | None = None
        self._last_reference_active = False
        # R4-B5（B3）：本帧的双讲标志（`protect` 档用它冻结 NLMS 步长 / 剔除地板样本）。
        self._double_talk_now = False
        # 参考源是否支持 `double_talk` 关键字（旧替身/旧后端一律回退两参数调用，行为不变）。
        self._passes_double_talk = _reference_accepts_double_talk(reference)

    # ---------------------------------------------------------------- properties

    @property
    def config(self) -> BargeInChannelConfig:
        return self._config

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def confirmed(self) -> bool:
        return self._confirmed

    @property
    def level(self) -> float:
        return self._level

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def errors(self) -> int:
        return self._errors

    @property
    def controller(self) -> BargeInPlaybackController:
        return self._controller

    @property
    def state(self) -> BargeInState:
        return self._controller.state

    @property
    def duck_suppressed(self) -> bool:
        """是否因"没有可用回声参考"而按设计**没有**降音量。"""

        return self._duck_suppressed

    @property
    def echo_proven(self) -> bool:
        """本 episode 内是否已证明"自己的回声可被观测"（回声可观测性安全门）。"""

        return self._echo_proven

    @property
    def max_echo_correlation(self) -> float:
        return self._max_echo_correlation

    @property
    def cooldown_ms(self) -> int:
        """方案 C 冷却窗口时长（0 = 关闭）。"""

        return self._config.barge_in.cooldown_ms

    @property
    def cooldown_remaining_ms(self) -> int | None:
        return getattr(self._controller, "cooldown_remaining_ms", None)

    @property
    def cooldown_frames(self) -> int:
        return getattr(self._controller, "cooldown_frames", 0)

    # ------------------------------------------------- R4-B P1/P2：真 AEC 与动态门

    @property
    def aec_running(self) -> bool:
        """真 AEC 后端是否在逐帧处理（配置开启 **且** 参考源提供 ``process``）。"""

        return bool(self._config.use_reference_aec and self._reference is not None)

    @property
    def aec_frames_applied(self) -> int:
        return self._aec_frames_applied

    @property
    def aec_fallback_reason(self) -> str:
        return self._aec_fallback_reason

    @property
    def aec_erle_db(self) -> float | None:
        return self._aec_erle_db

    @property
    def aec_erle_sufficient(self) -> bool:
        return self._aec_erle_sufficient

    @property
    def aec_stream_delay_ms(self) -> int | None:
        return self._aec_stream_delay_ms

    @property
    def residual_echo_floor_pcm16(self) -> float | None:
        return self._residual_echo_floor_pcm16

    @property
    def double_talk_enabled(self) -> bool:
        """R4-B5：双讲检测是否**真的在跑**（``off`` 档恒 ``False``，且不建检测器对象）。"""

        return self._double_talk_detector is not None

    @property
    def double_talk_state(self) -> str | None:
        """最近一帧的双讲状态短码（未启用时为 ``None``）。"""

        if self._double_talk_last is None:
            return None
        return str(self._double_talk_last.get("state"))

    @property
    def double_talk_facts(self) -> dict[str, object]:
        """双讲检测的只读事实（未启用/还没得出时返回空字典）。"""

        return dict(self._double_talk_last or {})

    @property
    def gate_in_playback(self) -> float | None:
        """当前播放期有效能量门（未启用动态门时为 ``None``）。"""

        return self._gate_in_playback

    def _effective_energy_threshold(self) -> float:
        """播放期能量门：启用动态门且已有地板时取 ``max(固定门, 地板 × 倍率)``。"""

        margin = self._config.residual_gate_margin
        if margin is None or not self._residual_levels:
            self._gate_in_playback = None
            return self._config.energy_threshold
        level = math.ceil(self._config.residual_floor_quantile * len(self._residual_levels)) - 1
        ordered = sorted(self._residual_levels)
        floor = float(ordered[max(0, min(level, len(ordered) - 1))])
        self._residual_echo_floor_pcm16 = floor
        gate = max(self._config.energy_threshold, floor * float(margin))
        self._gate_in_playback = gate
        return gate

    def _apply_reference_aec(self, payload: bytes) -> tuple[bytes, bool]:
        """用参考源的真 AEC 处理一帧；返回 ``(用于算电平的 payload, 参考是否有能量)``。

        - 未启用/参考没有 ``process`` → 原样返回（行为与改动前一致）；
        - 任何失败（异常 / 输出非法）→ 原样返回并记固定原因，**绝不冒泡**；
        - 参考帧无能量（``reference_silent``）→ 该帧不进入残余地板统计（没有回声可测）。
        """

        if not self._config.use_reference_aec or self._reference is None:
            return payload, False
        process = getattr(self._reference, "process", None)
        if not callable(process):
            return payload, False
        try:
            result = process(
                capture_at_ms=int(self._clock() * 1000.0),
                capture=payload,
                # R4-B5（B3）：`protect` 档把本帧双讲标志透传下去（NLMS 冻结步长 / 直通）。
                # 兼容旧接口：不接受该关键字时回退为两参数调用（行为与改动前一致）。
                **({"double_talk": self._double_talk_now} if self._passes_double_talk else {}),
            )
        except Exception:  # noqa: BLE001 - AEC 失败绝不影响守望与播报
            self._errors += 1
            self._aec_fallback_reason = AEC_REASON_PROCESS_FAILED
            return payload, False
        output = getattr(result, "output", None)
        if output is None or getattr(output, "size", 0) * 2 != len(payload):
            self._errors += 1
            self._aec_fallback_reason = AEC_REASON_OUTPUT_INVALID
            return payload, False
        reason = str(getattr(result, "fallback_reason", "") or "")
        if getattr(result, "applied", False):
            self._aec_frames_applied += 1
        self._aec_fallback_reason = reason
        cumulative = getattr(result, "cumulative_erle_db", None)
        if isinstance(cumulative, (int, float)) and math.isfinite(float(cumulative)):
            self._aec_erle_db = float(cumulative)
        canceller = getattr(self._reference, "canceller", None)
        if canceller is not None and hasattr(canceller, "erle_sufficient"):
            self._aec_erle_sufficient = bool(canceller.erle_sufficient)
        reference_active = reason != AEC_REASON_REFERENCE_SILENT
        self._last_reference_active = bool(reference_active)
        residual = (np.clip(np.asarray(output, dtype=np.float32), -1.0, 1.0) * 32767.0).round().astype("<i2")
        return residual.tobytes(), reference_active

    def _observe_double_talk(self) -> None:
        """R4-B5（B2）：把本帧的既有事实喂给双讲检测器，只**记录**结论。

        硬约束（本批不改变任何行为）：
        - 只用**已经算出来的**事实（`echo_correlation` / 对齐窗口的参考与采集 RMS /
          AEC 引擎的实测抑制是否达标），不新增任何处理、不读音频内容；
        - 检测结果**不参与**音量、确认、停播、前置缓冲与残余地板统计的任何判定；
        - 引擎没真在跑或对齐还没给出 `ok` 时，对应证据一律按"不可用"传（`None`），
          **绝不**当成"低相关性/无抑制"使用。
        """

        detector = self._double_talk_detector
        if detector is None:
            return
        alignment = self._alignment
        if alignment is None or str(getattr(alignment, "state", "")) != "ok":
            return
        correlation = self._last_correlation
        erle_sufficient: bool | None = None
        if self.aec_running:
            canceller = getattr(self._reference, "canceller", None)
            if canceller is not None and hasattr(canceller, "erle_sufficient"):
                erle_sufficient = bool(getattr(canceller, "erle_sufficient"))
            elif self._aec_frames_applied > 0:
                erle_sufficient = bool(self._aec_erle_sufficient)
        # ⚠️ 单位换算：对齐估计给的是**归一化浮点 RMS**（±1.0 满量程），而双讲检测的
        # 阈值是 PCM16 口径（`reference_active_rms=60` / `near_rms_floor=300`）。这里是
        # 唯一的口径转换点，不换算会让"参考有没有能量"永远判错（真机表现为永远 silent）。
        pcm16_scale = 32768.0
        observation = DoubleTalkObservation(
            frame_index=self._frame_number - 1,
            capture_rms=float(getattr(alignment, "capture_rms", 0.0) or 0.0) * pcm16_scale,
            reference_rms=float(getattr(alignment, "reference_rms", 0.0) or 0.0) * pcm16_scale,
            reference_active=bool(self._last_reference_active),
            echo_correlation=correlation,
            erle_sufficient=erle_sufficient,
        )
        self._double_talk_last = detector.observe(observation).to_details()

    def _apply_double_talk_protect(self) -> None:
        """R4-B5（B3）：``protect`` 档的两处**保护动作**（仅此两处，不碰任何判定）。

        1. 本帧双讲 ⇒ 置 `_double_talk_now`，由 `_apply_reference_aec()` 透传给参考源
           （NLMS 权重按 `double_talk_step_scale` 缩放，默认**冻结**）；
        2. 本帧双讲 ⇒ **不进残余地板统计**（双讲帧的残差就是用户语音，会把地板抬高、
           进而把动态门抬高 ⇒ 漏判有人说话）。

        音量 / 确认 / 停播 / 安全门**一律不变**；`facts` 档不执行本方法。
        """

        self._double_talk_now = bool((self._double_talk_last or {}).get("double_talk") is True)

    def _track_residual_floor(self, *, reference_active: bool, double_talk: bool, rms: float) -> None:
        """残余地板样本入队（R4-B5 起双讲帧被剔除；``protect`` 档才生效）。

        只改**统计口径**，不改帧电平、不改能量门本身的计算方式。
        """

        if not self.aec_running or not reference_active:
            return
        if double_talk and self._config.double_talk_mode == DT_MODE_PROTECT:
            return
        self._residual_levels.append(rms)

    def _note_alignment(self) -> None:
        """把对齐估计出的回声延迟喂给 AEC3 的 ``stream_delay_ms``（真机实测该提示敏感）。"""

        alignment = self._alignment
        if alignment is None or self._reference is None:
            return
        delay = getattr(alignment, "delay_ms", None)
        if not isinstance(delay, int) or delay < 0 or delay == self._aec_stream_delay_ms:
            return
        canceller = getattr(self._reference, "canceller", None)
        setter = getattr(canceller, "set_stream_delay_ms", None)
        if not callable(setter):
            return
        try:
            setter(delay)
        except Exception:  # noqa: BLE001 - 提示失败不影响判定
            self._errors += 1
            return
        self._aec_stream_delay_ms = delay

    def to_details(self) -> dict[str, object]:
        return {
            **self._config.to_details(),
            "armed": self._armed,
            "frames": self._frames,
            "confirmed": self._confirmed,
            "level": round(self._level, 4),
            "reason": self._reason,
            "reference_state": self._reference_state,
            "echo_correlation": (
                None if self._last_correlation is None else round(self._last_correlation, 4)
            ),
            "errors": self._errors,
            "pre_roll_frames": self._pre_roll_frames(),
            "duck_suppressed": self._duck_suppressed,
            "duck_suppressed_reason": (
                REASON_DUCK_DISABLED_NO_REFERENCE if self._duck_suppressed else None
            ),
            "detector_energy_gate_applied": self._detector_energy_gate_applied,
            "echo_delay_ms": (
                int(getattr(self._alignment, "delay_ms", 0)) if self._alignment else None
            ),
            "alignment_state": (
                str(getattr(self._alignment, "state", "")) if self._alignment else None
            ),
            "alignment_estimates": self._alignment_estimates,
            "echo_proven": self._echo_proven,
            "max_echo_correlation": round(self._max_echo_correlation, 4),
            # R4-B 收紧：如实暴露"为什么还没证明"（固定短码）+ 连续计数与延迟。
            "echo_proof_state": self._proof_state,
            "echo_proof_run": self._proof_run,
            "echo_proof_delay_ms": self._proof_delay_ms,
            # 方案 C：冷却窗口事实（未启用时 remaining 为 None、frames 为 0）。
            "cooldown_ms": self._config.barge_in.cooldown_ms,
            "cooldown_remaining_ms": getattr(self._controller, "cooldown_remaining_ms", None),
            "cooldown_frames": getattr(self._controller, "cooldown_frames", 0),
            # R4-B P1/P2：真 AEC 与残余地板感知动态门的事实（未启用时全部为默认/None）。
            "aec_running": self.aec_running,
            "aec_frames_applied": self._aec_frames_applied,
            "aec_fallback_reason": self._aec_fallback_reason or None,
            "aec_erle_db": None if self._aec_erle_db is None else round(self._aec_erle_db, 3),
            "aec_erle_sufficient": self._aec_erle_sufficient,
            "aec_stream_delay_ms": self._aec_stream_delay_ms,
            "residual_echo_floor_pcm16": (
                None
                if self._residual_echo_floor_pcm16 is None
                else round(self._residual_echo_floor_pcm16, 1)
            ),
            "gate_in_playback": None if self._gate_in_playback is None else round(self._gate_in_playback, 1),
            # R4-B5（B2）：双讲只读事实一并上报（`off` 档恒为"未启用"），供 voice_turn 对账。
            **self.double_talk_details(),
        }

    def double_talk_details(self) -> dict[str, object]:
        """R4-B5（B2）双讲检测事实（**只读、扁平**；`off` 档恒为"未启用"）。

        键前缀统一 `double_talk_`，值只允许 str/int/float/bool/None（含 `mode=off` 时）。
        """

        mode = self._config.double_talk_mode
        detector = self._double_talk_detector
        last = self._double_talk_last or {}
        return {
            "double_talk_mode": mode,
            "double_talk_enabled": detector is not None,
            "double_talk_state": last.get("state") or (None if detector is None else detector.state),
            "double_talk_double_talk": last.get("double_talk"),
            "double_talk_evidence": last.get("evidence_codes") or "",
            "double_talk_reason": last.get("reason"),
            "double_talk_enter_run": last.get("enter_run", 0 if detector is not None else None),
            "double_talk_frames": (0 if detector is None else detector.frames),
            "double_talk_frames_in_double_talk": (
                None if detector is None else detector.double_talk_frames
            ),
            "double_talk_step_scale": last.get("step_scale"),
            "double_talk_counts_toward_residual_floor": last.get("counts_toward_residual_floor"),
            "double_talk_gate_hold": last.get("gate_hold"),
        }

    def _reset_double_talk(self) -> None:
        """清空双讲累积（重新开闸 / 设备变化时必须重校准，与 AEC 口径一致）。"""

        if self._double_talk_detector is not None:
            self._double_talk_detector.reset()
        self._double_talk_last = None
        self._double_talk_now = False

    def pre_roll_facts(self) -> dict[str, object]:
        """前置缓冲摘要（帧数/时长），**不含音频**。"""

        frames = self._pre_roll_frames()
        return {
            "frames": frames,
            "duration_ms": frames * self._config.barge_in.frame_duration_ms,
        }

    # ---------------------------------------------------------------- lifecycle

    def arm(self) -> bool:
        """打开自有采集通道。任何失败都返回 ``False`` 并保留固定原因码（不打断）。"""

        if self._armed:
            return True
        self._stop_requested.clear()
        self._confirmed = False
        self._last_outcome = None
        self._level = 1.0
        self._controller.reset()
        self._pre_roll.clear()
        self._frames = 0
        self._frame_number = 0
        self._last_correlation = None
        self._reset_double_talk()
        self._duck_suppressed = False
        self._detector_energy_gate_applied = False
        self._echo_proven = False
        self._max_echo_correlation = 0.0
        # 新 episode：证明计数与状态一并复位（延迟仍可沿用上次的合理值判断）。
        self._proof_state = PROOF_STATE_NO_ESTIMATE
        self._proof_run = 0
        self._proof_last_delay_ms = None
        self._proof_delay_ms = None
        self._reason = REASON_ARMED
        try:
            from app.audio.bus import AudioBus

            factory = getattr(self._environment, "capture_factory", None)
            if not callable(factory):
                self._reason = REASON_CAPTURE_UNAVAILABLE
                return False
            self._bus = AudioBus(
                audio_session_id=CHANNEL_AUDIO_SESSION_ID,
                provider_generation=1,
                stream_id=CHANNEL_STREAM_ID,
                device_epoch=1,
                capacity_frames=self._config.capacity_frames,
            )
            self._capture = factory(self._bus)
            started = self._capture.start()
            if not getattr(started, "started", False):
                raise ContractError("capture_not_started")
        except Exception:  # noqa: BLE001 - 通道装不上必须降级为"不打断"
            self._discard_resources()
            self._reason = REASON_CAPTURE_UNAVAILABLE
            return False
        self._armed = True
        self._reason = REASON_ARMED
        return True

    def request_stop(self) -> None:
        """请求监视循环尽快返回（线程安全、无 I/O）；收尾仍由 :meth:`disarm` 完成。"""

        self._stop_requested.set()

    def disarm(self) -> None:
        """停止采集并释放通道资源；幂等，可重复调用。"""

        self._stop_requested.set()
        self._discard_resources()
        if self._armed:
            self._reason = REASON_DISARMED
        self._armed = False

    def _discard_resources(self) -> None:
        capture = self._capture
        self._capture = None
        stop = getattr(capture, "stop", None)
        if callable(stop):
            try:
                stop(reason=CHANNEL_STOP_REASON, discard=True)
            except Exception:  # noqa: BLE001 - 通道清理尽力而为
                self._errors += 1
        bus = self._bus
        self._bus = None
        if bus is not None:
            close = getattr(bus, "close", None)
            if callable(close):
                try:
                    close(reason=CHANNEL_STOP_REASON, discard=True)
                except Exception:  # noqa: BLE001 - 通道清理尽力而为
                    self._errors += 1

    # ---------------------------------------------------------------- observation

    def observe_once(self, *, playback_active: bool = True) -> BargeInOutcome | None:
        """读一帧并推进状态机；没有帧时返回 ``None``（不推进、不造事实）。"""

        if not self._armed or self._bus is None:
            self._reason = REASON_NOT_ARMED
            return None
        try:
            packet = self._bus.read_nowait()
        except Exception:  # noqa: BLE001 - 读失败按通道不可用处理
            self._errors += 1
            self._reason = REASON_CAPTURE_STOPPED
            return None
        if packet is None:
            if not self._bus_open():
                self._reason = REASON_CAPTURE_STOPPED
            return None
        payload = getattr(packet, "payload", None)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            self._errors += 1
            return None
        data = bytes(payload)
        # R4-B5（B3）：**先**判双讲（用**上一帧**的既有事实：对齐结论 / ERLE 是否达标 /
        # 上一帧的参考是否活跃），**再**决定本帧的 AEC 处理与地板统计。
        # 为什么必须这个顺序：真 AEC 一旦先跑，双讲帧里"近端语音 + 回声"就已经被喂进
        # 自适应滤波器（NLMS 会当场用近端语音更新权重）；先判双讲才能让 `protect` 档真正
        # 拦住那一帧的自适应。代价是判定最多滞后 1 帧（20 ms），如实记录、不回避。
        self._observe_double_talk()
        self._apply_double_talk_protect()
        # R4-B P1：用真 AEC 把"自己的回声"从采集里减掉，再算电平/概率。
        analysis_payload, reference_active = self._apply_reference_aec(data)
        probability, rms = self._analyze(packet, analysis_payload)
        # R4-B P2 原口径：只把"参考有能量"的帧计入地板（没有回声的帧不构成地板）。
        self._track_residual_floor(
            reference_active=reference_active, double_talk=self._double_talk_now, rms=rms
        )
        self._observe_alignment(data)
        correlation, aec_available, reference_state = self._echo_correlation(data)
        self._reference_state = reference_state
        self._last_correlation = correlation
        observation = BargeInObservation(
            at_ms=self._frame_number * self._config.barge_in.frame_duration_ms,
            voice_probability=probability,
            playback_active=bool(playback_active),
            echo_correlation=correlation,
            playback_queued_ms=0,
            rms=rms,
            aec_available=bool(aec_available),
        )
        self._pre_roll.append(data)
        limit = self._config.barge_in.pre_roll_frames
        if len(self._pre_roll) > limit:
            del self._pre_roll[: len(self._pre_roll) - limit]
        self._frames += 1
        self._frame_number += 1
        outcome = self._controller.play_frame(self._frame_number - 1, observation)
        self._last_outcome = outcome
        ducking_allowed = not (
            self._config.duck_requires_reference and not aec_available
        )
        suppressed = (not ducking_allowed) and outcome.ducking and not outcome.handoff_to_turn
        if suppressed:
            # 没有可用回声参考：**不降音量**（否则会被自己的回声反复触发），
            # 只保留"确认后停播"这条路；如实记录，绝不假装已经降过音量。
            self._duck_suppressed = True
            self._apply_level(1.0)
        else:
            self._apply_level(outcome.level)
        if outcome.handoff_to_turn:
            self._confirmed = True
            self._reason = REASON_CONFIRMED
        elif self._reason in (REASON_ARMED,):
            self._reason = REASON_OBSERVING
        return outcome

    def run(
        self,
        *,
        is_playback_done: Callable[[], bool],
        max_seconds: float | None = None,
        playback_active: bool = True,
    ) -> BargeInOutcome | None:
        """守望一次播报：确认插话、播报结束、通道失效或超时都会返回。

        返回 ``None`` 表示"没有可用的打断判定"（通道未打开/已失效），调用方必须按
        "不打断"处理。返回值只描述音量/播放/轮次边界，**不**包含授权。
        """

        if not callable(is_playback_done):
            raise ContractError("is_playback_done 必须可调用")
        timeout = (
            self._config.max_monitor_seconds if max_seconds is None else float(max_seconds)
        )
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ContractError("max_seconds 必须是正数")
        if not self._armed:
            return None
        deadline = self._clock() + timeout
        while True:
            if self._stop_requested.is_set():
                return self._last_outcome
            if is_playback_done():
                self._reason = REASON_PLAYBACK_ENDED
                self._release_playback()
                return self._last_outcome
            outcome = self.observe_once(playback_active=playback_active)
            if outcome is not None and outcome.handoff_to_turn:
                return outcome
            if self._reason == REASON_CAPTURE_STOPPED:
                return None
            if self._clock() >= deadline:
                self._reason = REASON_TIMEOUT
                return self._last_outcome
            if outcome is None:
                self._sleep(self._config.barge_in.frame_duration_ms / 1000.0)

    def _release_playback(self) -> None:
        if not self._armed:
            return
        outcome = self._controller.playback_ended(self._frame_number)
        self._last_outcome = outcome
        self._apply_level(outcome.level)

    # ---------------------------------------------------------------- pre-roll

    def take_pre_roll_pcm(self) -> bytes:
        """取出并清空前置缓冲音频（供并入**同一** ``utterance_id`` 的下一轮使用）。"""

        data = b"".join(self._pre_roll)
        self._pre_roll.clear()
        return data

    def pre_roll_pcm(self) -> bytes:
        """只读查看当前前置缓冲音频（不清空）。"""

        return b"".join(self._pre_roll)

    def _pre_roll_frames(self) -> int:
        return len(self._pre_roll)

    # ---------------------------------------------------------------- internals

    def _bus_open(self) -> bool:
        bus = self._bus
        if bus is None:
            return False
        try:
            from app.audio.bus import AudioBusState

            return bus.state is AudioBusState.OPEN
        except Exception:  # noqa: BLE001 - 状态不可知按不可用处理
            self._errors += 1
            return False

    def _analyze(self, packet: Any, payload: bytes) -> tuple[float, float]:
        """返回 ``(人声概率, RMS)``；概率只由可观测事实映射，不编造。

        检测器模式采用**双重条件**（与主链路 ``observed and energy_hit`` 同口径）：
        结构（检测器说本帧有人声，含窗口粘滞）**且**电平（RMS ≥ 能量门）同时满足才算人声。
        真机教训（2026-10-02）：只有检测器时，扬声器回声也会被判成"连续人声"→ 确认会被
        自己的声音触发；加上能量门后，本机实测低于能量门的回声不再触发（详见交接 §9）。
        """

        rms = _rms(payload)
        threshold = self._effective_energy_threshold()
        if self._config.probability_mode == PROBABILITY_MODE_ENERGY:
            return min(1.0, rms / threshold), rms
        detector = self._voice_detector
        if detector is None:
            # 声称用检测器却没有检测器：退回能量映射，并计一个错误事实。
            self._errors += 1
            return min(1.0, rms / threshold), rms
        try:
            hit = detector(packet)
        except Exception:  # noqa: BLE001 - 检测失败按"没有人声"处理，绝不放行
            self._errors += 1
            return 0.0, rms
        if hit is not True:
            return 0.0, rms
        self._detector_energy_gate_applied = True
        if rms < threshold:
            return 0.0, rms
        return 1.0, rms

    def _echo_correlation(self, payload: bytes) -> tuple[float | None, bool, str]:
        """返回 ``(回声相关性, 是否真正可用, 参考状态)``。

        相关性来自 :meth:`_observe_alignment` 的**对齐后**估计（需要"同钟参考 + 数百毫秒
        量级的延迟搜索"）；没有可用参考、参考异钟或估计还没得出 ``ok`` 时一律 ``None``，
        并如实登记原因（``none`` / ``unavailable`` / ``clock_mismatch`` / 对齐状态）。
        """

        reference = self._reference
        if reference is None:
            return None, False, REFERENCE_SOURCE_NONE
        pcm = getattr(reference, "pcm", None)
        rate = getattr(pcm, "sample_rate", None)
        expected = self._config.reference_sample_rate or CAPTURE_SAMPLE_RATE_HZ
        if not isinstance(rate, int) or int(rate) != int(expected):
            return None, False, REFERENCE_SOURCE_CLOCK_MISMATCH
        alignment = self._alignment
        if alignment is None:
            return None, False, REFERENCE_SOURCE_UNAVAILABLE
        state = str(getattr(alignment, "state", REFERENCE_SOURCE_UNAVAILABLE))
        if state != "ok":
            return None, False, state
        value = float(getattr(alignment, "correlation", 0.0))
        if not math.isfinite(value):
            return None, False, REFERENCE_SOURCE_UNAVAILABLE
        return max(0.0, min(1.0, value)), True, state

    def _observe_alignment(self, payload: bytes) -> None:
        """维护最近采集帧并周期性估计"回声延迟 + 相关性"（每 ``alignment_interval_frames`` 帧）。

        估计需要参考时间线的**同一时刻集合**；两条时间线都以 ``clock()``（单调毫秒）为准，
        因此这里用观察时的时间戳入队。任何失败都只计数并保留上一次结论。
        """

        reference = self._reference
        if reference is None:
            return
        pcm = getattr(reference, "pcm", None)
        expected = self._config.reference_sample_rate or CAPTURE_SAMPLE_RATE_HZ
        if getattr(pcm, "sample_rate", None) != expected:
            return
        try:
            import numpy as np
        except Exception:  # noqa: BLE001 - 没有 numpy 就无法做相关性（如实不可用）
            self._errors += 1
            return
        trimmed = payload[: len(payload) - (len(payload) % 2)]
        if not trimmed:
            return
        samples = np.frombuffer(trimmed, dtype="<i2").astype(np.float32) / 32768.0
        self._capture_history.append((int(self._clock() * 1000.0), samples))
        self._frames_since_alignment += 1
        if self._frames_since_alignment < self._config.alignment_interval_frames:
            return
        self._frames_since_alignment = 0
        timeline_frames = getattr(reference, "timeline_frames", None)
        if not callable(timeline_frames):
            return
        try:
            from app.audio.aec.alignment import estimate_echo_alignment

            reference_frames = timeline_frames(limit=self._config.alignment_history_frames)
            self._alignment = estimate_echo_alignment(
                reference_frames=reference_frames,
                capture_frames=tuple(self._capture_history),
                sample_rate=int(expected),
            )
            self._alignment_estimates += 1
            # R4-B P1：把这次对齐的延迟估计喂给 AEC3（延迟提示敏感：0–500 ms 内 ERLE 7–15.5 dB）。
            self._note_alignment()
            # R4-B 收紧：只在"相关性达标 + 延迟物理合理 + 连续稳定"时才认回声可观测。
            self._note_proof_estimate()
        except Exception:  # noqa: BLE001 - 估计失败只计数，绝不影响判定链路
            self._errors += 1

    def _note_proof_estimate(self) -> None:
        """回声可观测性判定（R4-B 收紧；**只收紧、不放松**）。

        真机教训（2026-10-03，`research/2026-10-03-P10-R4B-playback-channel-selfcheck.md` 之后
        的 turn 2/4）：本机相关性能到 **0.60~0.75**，但估计延迟出现 **746 / 1589 / −780 ms**，
        而本机实测参考→采集延迟只有 ≈390–410 ms。旧的"只看相关性"门槛被伪相关骗过，
        `echo_proven=True` 让 R4-A 的安全门放行 ⇒ **用户没插话也停播**。

        现在三条同时满足才算证明（否则维持 fail-closed，行为只会更保守）：
        1. 对齐状态 ``ok`` 且相关性 ≥ ``echo_proof_correlation``；
        2. 延迟落在 ``[echo_proof_delay_min_ms, echo_proof_delay_max_ms]``；
        3. 连续 ``echo_proof_consecutive`` 次成立，且相邻延迟差 ≤ ``echo_proof_delay_tolerance_ms``。
        """

        alignment = self._alignment
        if alignment is None:
            self._reset_proof_run(PROOF_STATE_NO_ESTIMATE)
            return
        state = str(getattr(alignment, "state", "") or "")
        correlation = float(getattr(alignment, "correlation", 0.0) or 0.0)
        if correlation > self._max_echo_correlation:
            self._max_echo_correlation = correlation
        if state != "ok":  # 与 `app.audio.aec.reference.REFERENCE_OK` 同字面值
            self._reset_proof_run(PROOF_STATE_ALIGNMENT_NOT_OK)
            return
        if correlation < self._config.echo_proof_correlation:
            self._reset_proof_run(PROOF_STATE_LOW_CORRELATION)
            return
        delay = getattr(alignment, "delay_ms", None)
        if (
            isinstance(delay, bool)
            or not isinstance(delay, int)
            or not (
                self._config.echo_proof_delay_min_ms
                <= delay
                <= self._config.echo_proof_delay_max_ms
            )
        ):
            self._reset_proof_run(PROOF_STATE_DELAY_IMPLAUSIBLE)
            return
        if (
            self._proof_last_delay_ms is not None
            and abs(delay - self._proof_last_delay_ms) > self._config.echo_proof_delay_tolerance_ms
        ):
            # 延迟乱跳：本次重新计数（不重置整条历史，但也不累加证明）。
            self._proof_run = 1
        else:
            self._proof_run += 1
        self._proof_last_delay_ms = delay
        self._proof_delay_ms = delay
        if self._proof_run >= self._config.echo_proof_consecutive:
            # 证明"参考与采集之间确实有可测的、延迟稳定的声学耦合"——只有这时才允许真正停播。
            self._echo_proven = True
            self._proof_state = PROOF_STATE_PROVEN
        else:
            self._proof_state = PROOF_STATE_ACCUMULATING

    def _reset_proof_run(self, state: str) -> None:
        self._proof_run = 0
        self._proof_last_delay_ms = None
        self._proof_state = state

    def _apply_level(self, level: float) -> None:
        self._level = float(level)
        sink = self._gain_sink
        if sink is None:
            return
        try:
            sink(self._level)
        except Exception:  # noqa: BLE001 - 音量通道故障不得影响判定
            self._errors += 1

    def _apply_degradation(self, degradation: str) -> None:
        """降级只提高门槛：通过替换状态机实例实现，绝不改低门槛。"""

        if degradation not in DEGRADATION_LEVELS:
            raise ContractError("degradation 必须是已知降级级别")
        if degradation == DEGRADE_FULL:
            return
        from app.duplex.barge_in import BargeInController

        config = self._config.barge_in
        controller = BargeInController(config=config, degradation=degradation)
        self._controller = _DegradedPlaybackController(
            controller=controller, prompt_state=self._config.prompt_state
        )
        self._controller_applied = True


class _DegradedPlaybackController(BargeInPlaybackController):
    """把已按降级级别构造的 R1a 状态机接到 R1b 编排层（门槛只升不降）。"""

    def __init__(self, *, controller: Any, prompt_state: str) -> None:
        super().__init__(config=controller.config, prompt_state=prompt_state)
        self._controller = controller


def _reference_accepts_double_talk(reference: Any | None) -> bool:
    """参考源的 ``process`` 是否接受 ``double_talk`` 关键字（只查签名，不调用）。

    真 AEC 参考（`SameProcessAecReference`）与 R4-B5 之后的实现支持；旧替身/旧后端不写这个
    关键字时返回 ``False``，通道回退两参数调用 ⇒ **与改动前逐字节一致**。
    """

    if reference is None:
        return False
    process = getattr(reference, "process", None)
    if not callable(process):
        return False
    try:
        signature = inspect.signature(process)
    except (TypeError, ValueError):  # pragma: no cover - 无法内省时按不支持处理
        return False
    if "double_talk" in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _rms(payload: bytes) -> float:
    """PCM16 小端 RMS；空载荷返回 0。"""

    if not payload:
        return 0.0
    trimmed = payload[: len(payload) - (len(payload) % 2)]
    if not trimmed:
        return 0.0
    total = 0
    count = 0
    for index in range(0, len(trimmed), 2):
        value = int.from_bytes(trimmed[index : index + 2], "little", signed=True)
        total += value * value
        count += 1
    if count == 0:
        return 0.0
    return math.sqrt(total / count)


def _normalized_correlation(reference_samples: Any, payload: bytes) -> float | None:
    """参考与采集的归一化相关性（绝对值，0~1）；样本不足或静音返回 ``None``/``0.0``。

    只在参考与采集**同钟且对齐**时才有物理意义，调用方负责同钟检查。
    """

    try:
        import numpy as np
    except Exception:  # noqa: BLE001 - numpy 不可用即无法算相关性（如实返回不可用）
        return None
    reference = np.asarray(reference_samples, dtype=np.float32).reshape(-1)
    capture = np.frombuffer(payload[: len(payload) - (len(payload) % 2)], dtype="<i2").astype(
        np.float32
    )
    size = min(reference.size, capture.size)
    if size < 2:
        return None
    reference = reference[:size]
    capture = capture[:size]
    reference_norm = float(np.linalg.norm(reference))
    capture_norm = float(np.linalg.norm(capture))
    if reference_norm <= 0.0 or capture_norm <= 0.0:
        return 0.0
    value = abs(float(np.dot(reference, capture))) / (reference_norm * capture_norm)
    if not math.isfinite(value):
        return None
    return max(0.0, min(1.0, value))


__all__ = [
    "CAPTURE_FRAME_BYTES",
    "CAPTURE_SAMPLE_RATE_HZ",
    "CHANNEL_AUDIO_SESSION_ID",
    "CHANNEL_STOP_REASON",
    "CHANNEL_STREAM_ID",
    "DEFAULT_CAPACITY_FRAMES",
    "DEFAULT_ENERGY_THRESHOLD",
    "DEFAULT_MAX_MONITOR_SECONDS",
    "DEFAULT_MIN_SUSPECT_FRAMES",
    "DEFAULT_RELEASE_MS",
    "FRAME_MS",
    "PROBABILITY_MODE_DETECTOR",
    "PROBABILITY_MODE_ENERGY",
    "PROBABILITY_MODES",
    "REASON_ARMED",
    "REASON_CAPTURE_STOPPED",
    "REASON_CAPTURE_UNAVAILABLE",
    "REASON_CONFIRMED",
    "REASON_DISARMED",
    "REASON_DUCK_DISABLED_NO_REFERENCE",
    "REASON_NOT_ARMED",
    "REASON_OBSERVING",
    "REASON_PLAYBACK_ENDED",
    "REASON_TIMEOUT",
    "REFERENCE_SOURCE_CLOCK_MISMATCH",
    "REFERENCE_SOURCE_NONE",
    "REFERENCE_SOURCE_UNAVAILABLE",
    "BargeInChannel",
    "BargeInChannelConfig",
]
