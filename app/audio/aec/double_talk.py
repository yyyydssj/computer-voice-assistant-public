"""P10 批次 R4-B5（B1）：**双讲检测（DTD）**——纯离线、只吃可观测事实。

背景（原文第 4 点 + 本仓实测缺口）：

- 只有远端说话（单讲）时，AEC 可以放心减回声；**双讲**（近端与远端同时说）时误差信号里
  既有残余回声又有近端语音，此时继续全速自适应会把近端语音学进滤波器（吸收近端语音 /
  发散），输出侧也可能把用户开头几句削掉，所以要"检测 + 保护"。
- 本仓现状（2026-10-13 只读核实）：`NlmsEchoCanceller` 的步长固定 0.4、**双讲时照样更新
  权重**（`nlms.py` 的 `cancel()`），`SameProcessAecReference.process()` 连 `double_talk`
  都没往下传；`BargeInChannel._effective_energy_threshold()` 的残余地板**无条件**吃"参考有
  能量"帧的残差 ⇒ 双讲帧的残差就是用户语音，会把地板抬高、进而把动态门抬高。

本模块只做**判定**，不做任何处理（无 numpy、无设备、无网络、无线程、无安全链导入）：

1. `DoubleTalkConfig` —— 判据阈值（都有物理含义与默认值）；
2. `DoubleTalkObservation` —— 一帧的**可观测事实**（帧序、采集 RMS、参考 RMS、对齐后回声
   相关性、单帧 ERLE、参考是否静音）；
3. `DoubleTalkDetector` —— 流式状态机，输出 `DoubleTalkResult`（状态 + 证据码 + 供上层消费的
   两个只读开关）。

**硬不变量（与 R4-A/R4-B 同口径，绝不能违反）**：

- **证据不足一律不认**：默认 `min_evidence=2`，且"回声相似度高 + ERLE 达标"作为**反向证据**
  会直接压掉同帧的双讲候选。没有 2 条独立证据 ⇒ `unclear`，事实 `double_talk=None`。
- **参考侧没有能量 ⇒ 不存在双讲**：参考帧静音时（`reference_active=False` 或参考 RMS 低于
  `reference_active_rms`）一律不可能是双讲——这与既有实测一致
  （`near_only` 12/12 次 AEC3 完全不碰用户语音）。
- **`unavailable` ≠ 双讲**：对齐尚未得出 `ok`、相关性 `None` 时，"回声相似度低"这条证据
  **不存在**，也**绝不**当作低相关性使用。
- **双讲本身不触发任何停播**：本模块不输出音量、不输出轮次边界、不改变授权
  （插话永不授权）。是否打断仍由既有确认链 + 冷却窗口决定。
- **只读事实**：`counts_toward_residual_floor` / `gate_hold` / `step_scale` 只是给上层消费的
  事实，本批（B1）**不接任何生产路径**，因此关掉开关时行为与今天逐字节一致。

依据：`implementation_plan/research/2026-10-03-P10-R4B-2-doubletalk-findings.md`、
`implementation_plan/research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`
（真机：ERLE 5.87 dB、137/289 帧超门、双讲后用户语音 1648 = 门 +8.8 dB）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import math

from app.contracts.base import ContractError, require_positive

# ------------------------------------------------------------------ 固定短码

DT_MODE_OFF = "off"
DT_MODE_FACTS = "facts"
DT_MODE_PROTECT = "protect"
DT_MODES = (DT_MODE_OFF, DT_MODE_FACTS, DT_MODE_PROTECT)
"""档位：``off`` 关闭 / ``facts`` 只记录事实 / ``protect`` 启用保护（冻结步长 + 地板剔除）。"""

DT_STATE_SILENT = "silent"
"""两边都没有能量：既不是单讲也不是双讲（事实为"未知"）。"""
DT_STATE_POSSIBLY_NEAR = "possibly_near"
"""参考侧没有能量、近端有能量：**不可能是双讲**（没有回声可消）。"""
DT_STATE_UNPROVEN = "unproven"
"""远端在说话，但证据不足以判定双讲或单讲（**不得猜**）。"""
DT_STATE_SINGLE_TALK = "single_talk"
"""远端在说话且证据支持"只有回声、近端没有说话"。"""
DT_STATE_DOUBLE_TALK = "double_talk"
"""双讲已检出（≥2 条独立证据且无反向证据）。"""

DT_STATES = (
    DT_STATE_SILENT,
    DT_STATE_POSSIBLY_NEAR,
    DT_STATE_UNPROVEN,
    DT_STATE_SINGLE_TALK,
    DT_STATE_DOUBLE_TALK,
)

REASON_INPUT_INVALID = "input_invalid"
REASON_BOTH_SILENT = "both_silent"
REASON_NEAR_ONLY = "near_only_no_reference"
REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
REASON_COUNTER_EVIDENCE = "counter_evidence_echo_dominant"
REASON_NO_NEW_EVIDENCE = "no_new_evidence"
REASON_EVIDENCE_ACCUMULATING = "evidence_accumulating"
REASON_ENTER_FRAMES_NOT_MET = "enter_frames_not_met"
REASON_EXIT_FRAMES_NOT_MET = "exit_frames_not_met"
REASON_DOUBLE_TALK = "double_talk_detected"
REASON_SINGLE_TALK = "echo_dominant"
REASONS = (
    REASON_INPUT_INVALID,
    REASON_BOTH_SILENT,
    REASON_NEAR_ONLY,
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_COUNTER_EVIDENCE,
    REASON_NO_NEW_EVIDENCE,
    REASON_EVIDENCE_ACCUMULATING,
    REASON_ENTER_FRAMES_NOT_MET,
    REASON_EXIT_FRAMES_NOT_MET,
    REASON_DOUBLE_TALK,
    REASON_SINGLE_TALK,
)

# 证据码（固定短码，绝不含音频内容）
DT_EVIDENCE_CORRELATION_LOW = "echo_correlation_low"
DT_EVIDENCE_ERLE_INSUFFICIENT = "erle_insufficient"
DT_EVIDENCE_NEAR_EXCESS = "near_excess_over_echo_expectation"
DT_EVIDENCE_COUNTER_CORRELATION_HIGH = "counter_echo_correlation_high"
DT_EVIDENCE_COUNTER_ERLE_SUFFICIENT = "counter_erle_sufficient"
DT_EVIDENCE_CODES = (
    DT_EVIDENCE_CORRELATION_LOW,
    DT_EVIDENCE_ERLE_INSUFFICIENT,
    DT_EVIDENCE_NEAR_EXCESS,
    DT_EVIDENCE_COUNTER_CORRELATION_HIGH,
    DT_EVIDENCE_COUNTER_ERLE_SUFFICIENT,
)
DT_FOR_CODES = (
    DT_EVIDENCE_CORRELATION_LOW,
    DT_EVIDENCE_ERLE_INSUFFICIENT,
    DT_EVIDENCE_NEAR_EXCESS,
)

# 证据位（用于"进入双讲后不重复计入同一码"，避免长 episode 里同一证据被反复数）。
_BIT_CORRELATION_LOW = 1
_BIT_ERLE_INSUFFICIENT = 2
_BIT_NEAR_EXCESS = 4

DEFAULT_STEP_SCALE = 0.0
"""保护档在双讲帧使用的 NLMS 步长倍率（0 = 冻结权重更新；由 B3 消费，本批不接线）。"""


def _require_non_negative_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} 必须是数值")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ContractError(f"{name} 必须是有限非负数")
    return number


def _require_unit_fraction(name: str, value: object) -> float:
    number = _require_non_negative_number(name, value)
    if not 0.0 <= number <= 1.0:
        raise ContractError(f"{name} 必须在 0~1 内")
    return number


def double_talk_config_ok(mode: object) -> bool:
    """档位是否合法（供 CLI/配置层复用，避免各处重复写字符串常量）。"""

    return isinstance(mode, str) and mode in DT_MODES


@dataclass(frozen=True, slots=True)
class DoubleTalkConfig:
    """双讲判据阈值；默认值按既有实测口径取，全部有物理含义。"""

    reference_active_rms: float = 60.0
    """参考帧"有能量"的下限（PCM16 RMS）；低于它一律不算远端在说话。

    本机环境噪声 RMS p50 为 47.7（见 NEXT-SESSION §2），因此 60 只把真正静音帧排除掉；
    真实播放帧的 RMS 远高于它（真机回声 p50 ≈ 846）。
    """
    near_rms_floor: float = 300.0
    """近端人声的能量下限（PCM16 RMS）；低于它时采集侧的"存在感"不足以构成双讲。"""
    correlation_dt_max: float = 0.2
    """对齐后回声相关性的**低**门槛：低于它说明"采集里的东西不像自己的回声"。

    与 `BargeInChannelConfig.echo_proof_correlation` 同量级（0.2）；本机实测回声相关性
    始终 <0.2、`echo_proven=false`，因此该证据在本机**只作为一条候选证据**，必须与
    `erle_insufficient` 等组合才可能成立（单靠它不够）。
    """
    correlation_block_min: float = 0.5
    """对齐后回声相关性的**高**门槛：达到它且 ERLE 达标 ⇒ 反向证据（回声主导）。"""
    near_over_echo_ratio: float = 2.0
    """采集 RMS / 参考 RMS 的比值门槛：超过它说明"采集比参考还响"。

    真实回声只会比参考更弱（声学增益 <1），所以采集明显比参考响时，多出来的能量不可能
    都来自回声 ⇒ 这条是"近端在说话"的候选证据（需与 ERLE/相关性组合）。
    """
    erle_min_db: float = 6.0
    """单帧 ERLE 的**低**门槛（与 `nlms.DEFAULT_ERLE_GATE_DB` 同口径）：低于它说明这一帧
    基本没消掉什么，若采集仍有能量 ⇒ 候选证据。"""
    erle_sufficient_db: float = 12.0
    """ERLE 的**够用**门槛：达到它视为"回声被有效压制"（反向证据的一半）。"""
    min_evidence: int = 2
    """进入双讲所需的最少**独立**证据条数；默认 2（宁可漏、不可猜）。"""
    enter_frames: int = 5
    """连续满足进入条件多少帧才真正进入双讲（5 帧 = 100 ms）。"""
    exit_frames: int = 15
    """连续不满足多少帧才退出双讲（15 帧 = 300 ms，滞回防抖）。"""
    freeze_step_scale: float = DEFAULT_STEP_SCALE
    """保护档在双讲帧使用的步长倍率（0 = 冻结；供 B3 消费）。"""

    def __post_init__(self) -> None:
        for name in ("reference_active_rms", "near_rms_floor"):
            object.__setattr__(
                self, name, _require_non_negative_number(name, getattr(self, name))
            )
        object.__setattr__(
            self,
            "correlation_dt_max",
            _require_unit_fraction("correlation_dt_max", self.correlation_dt_max),
        )
        object.__setattr__(
            self,
            "correlation_block_min",
            _require_unit_fraction("correlation_block_min", self.correlation_block_min),
        )
        if self.correlation_dt_max > self.correlation_block_min:
            raise ContractError("correlation_dt_max 不得大于 correlation_block_min")
        ratio = _require_non_negative_number("near_over_echo_ratio", self.near_over_echo_ratio)
        if ratio <= 0.0:
            raise ContractError("near_over_echo_ratio 必须为正数")
        object.__setattr__(self, "near_over_echo_ratio", ratio)
        erle_min = _require_non_negative_number("erle_min_db", self.erle_min_db)
        erle_ok = _require_non_negative_number("erle_sufficient_db", self.erle_sufficient_db)
        if erle_min >= erle_ok:
            raise ContractError("erle_min_db 必须小于 erle_sufficient_db")
        object.__setattr__(self, "erle_min_db", erle_min)
        object.__setattr__(self, "erle_sufficient_db", erle_ok)
        for name in ("min_evidence", "enter_frames", "exit_frames"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractError(f"{name} 必须是整数")
            require_positive(name, value)
        if self.min_evidence > len(DT_FOR_CODES):
            raise ContractError(f"min_evidence 不得大于可用证据数 {len(DT_FOR_CODES)}")
        object.__setattr__(
            self,
            "freeze_step_scale",
            _require_unit_fraction("freeze_step_scale", self.freeze_step_scale),
        )

    def to_details(self) -> dict[str, object]:
        return {
            "reference_active_rms": self.reference_active_rms,
            "near_rms_floor": self.near_rms_floor,
            "correlation_dt_max": self.correlation_dt_max,
            "correlation_block_min": self.correlation_block_min,
            "near_over_echo_ratio": self.near_over_echo_ratio,
            "erle_min_db": self.erle_min_db,
            "erle_sufficient_db": self.erle_sufficient_db,
            "min_evidence": self.min_evidence,
            "enter_frames": self.enter_frames,
            "exit_frames": self.exit_frames,
            "freeze_step_scale": self.freeze_step_scale,
        }


@dataclass(frozen=True, slots=True)
class DoubleTalkObservation:
    """一帧的**可观测事实**（由调用方从既有事实里取，绝不由本模块推断音频内容）。"""

    frame_index: int
    """单调递增的帧序（-1 表示未知/首帧前）；用于重置与日志对账。"""
    capture_rms: float
    """采集帧 RMS（PCM16 口径）。"""
    reference_rms: float
    """参考帧 RMS（PCM16 口径；没有参考时传 0.0）。"""
    reference_active: bool = True
    """参考是否真的有能量（由上层事实决定，例如 AEC 的 `reference_silent`）。"""
    echo_correlation: float | None = None
    """**对齐后**的回声相关性（0~1）；`None` = 还没得出/不可用（**不得当低相关性用**）。"""
    erle_db: float | None = None
    """本帧回声抑制量（dB）；`None` = 不可用（**不得当 0 用**）。"""
    erle_sufficient: bool | None = None
    """抑制量是否达"够用"门槛；`None` = 不可用。"""

    def to_details(self) -> dict[str, object]:
        return {
            "frame_index": self.frame_index,
            "capture_rms": round(float(self.capture_rms), 3),
            "reference_rms": round(float(self.reference_rms), 3),
            "reference_active": bool(self.reference_active),
            "echo_correlation": (
                None if self.echo_correlation is None else round(float(self.echo_correlation), 4)
            ),
            "erle_db": None if self.erle_db is None else round(float(self.erle_db), 3),
            "erle_sufficient": self.erle_sufficient,
        }


@dataclass(frozen=True, slots=True)
class DoubleTalkResult:
    """一帧的双讲判定结果（只读事实 + 供上层消费的只读开关）。"""

    state: str
    double_talk: bool | None
    """`True` 双讲 / `False` 单讲 / `None` 未知（证据不足）——**绝不猜**。"""
    evidence_codes: tuple[str, ...]
    reason: str
    enter_run: int
    exit_run: int
    step_scale: float
    counts_toward_residual_floor: bool
    gate_hold: bool

    def to_details(self) -> dict[str, object]:
        return {
            "state": self.state,
            "double_talk": self.double_talk,
            "evidence_codes": "|".join(self.evidence_codes),
            "reason": self.reason,
            "enter_run": self.enter_run,
            "exit_run": self.exit_run,
            "step_scale": self.step_scale,
            "counts_toward_residual_floor": self.counts_toward_residual_floor,
            "gate_hold": self.gate_hold,
        }


class DoubleTalkDetector:
    """流式双讲检测：每帧吃一条 `DoubleTalkObservation`，输出 `DoubleTalkResult`。

    与 `mode` 无关：检测本身总是运行（`facts` 与 `protect` 都需要它）；`mode` 决定上层是否
    消费 `step_scale` 与地板/门开关。`mode=off` 时由调用方**不要调用**本类（保持零开销），
    但本类本身不会因为 `mode` 改变判定结果。

    判定流水线（每一帧）：

    1. 校验输入（非法 → `input_invalid`，**不推进、不猜**）；
    2. 参考侧没有能量 ⇒ `possibly_near`（近端有能量）或 `silent`，**一定不是双讲**；
    3. 汇总支持证据（相关性低 / ERLE 不足 / 采集超参考）与反向证据（回声相关性高且 ERLE
       够用）；反向证据成立 ⇒ 压掉本帧候选；
    4. 支持证据以"位"累积（同一码只算一次），**证据条数 ≥ `min_evidence`** 且**连续
       `enter_frames` 帧**才进入双讲；证据不足一律 `unproven`（`double_talk=None`）；
    5. 退出走 `exit_frames` 滞回：>= 阈值帧没有够强证据才真正退出；退出倒计时中再次出现
       够强证据则清零倒计时（新的语音爆发）。

    只读开关（供 B3 消费，本批不接任何生产路径）：
    `step_scale`（双讲帧 = `freeze_step_scale`，其余 = 1.0）、
    `counts_toward_residual_floor`（双讲帧为 `False`）、`gate_hold`（双讲帧为 `True`）。
    """

    def __init__(
        self,
        *,
        config: DoubleTalkConfig | None = None,
        mode: str = DT_MODE_FACTS,
    ) -> None:
        if not double_talk_config_ok(mode):
            raise ContractError("double_talk mode 只支持 off/facts/protect")
        self._config = config or DoubleTalkConfig()
        self._mode = mode
        self._state = DT_STATE_UNPROVEN
        self._dt_run = 0
        self._exit_run = 0
        self._evidence_mask = 0
        self._dt_frames = 0
        self._frames = 0
        self._last_frame_index: int | None = None

    # ------------------------------------------------------------- read-only facts

    @property
    def config(self) -> DoubleTalkConfig:
        return self._config

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def state(self) -> str:
        return self._state

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def double_talk_frames(self) -> int:
        return self._dt_frames

    @property
    def enter_run(self) -> int:
        """连续"证据够强"的帧数（`double_talk` 的进入计数）。"""

        return self._dt_run

    @property
    def exit_run(self) -> int:
        """退出倒计时的连续帧数（`exit_frames` 滞回）。"""

        return self._exit_run

    def to_details(self) -> dict[str, object]:
        return {
            "mode": self._mode,
            "state": self._state,
            "frames": self._frames,
            "double_talk_frames": self._dt_frames,
            "enter_run": self._dt_run,
            "exit_run": self._exit_run,
            "step_scale": self._step_scale(),
            **self._config.to_details(),
        }

    # ------------------------------------------------------------- control

    def reset(self) -> None:
        """清空状态（设备变化 / 新会话必须重校准，与既有 AEC 口径一致）。"""

        self._state = DT_STATE_UNPROVEN
        self._dt_run = 0
        self._exit_run = 0
        self._evidence_mask = 0
        self._dt_frames = 0
        self._frames = 0
        self._last_frame_index = None

    # ------------------------------------------------------------- processing

    def observe(self, observation: DoubleTalkObservation) -> DoubleTalkResult:
        """推进一帧；非法输入按 `input_invalid` 如实返回，**不推进也不猜**。"""

        if not isinstance(observation, DoubleTalkObservation):
            raise ContractError("observation 必须是 DoubleTalkObservation")
        if not self._observation_valid(observation):
            return self._result(
                state=self._state,
                double_talk=self._tri_state(),
                evidence=(),
                reason=REASON_INPUT_INVALID,
            )

        if self._last_frame_index is not None and observation.frame_index < self._last_frame_index:
            # 帧序回退：调用方换了新会话（或测试重放），按新会话重新开始计数。
            self.reset()
        self._last_frame_index = observation.frame_index
        self._frames += 1

        reference_active = bool(observation.reference_active) and (
            observation.reference_rms >= self._config.reference_active_rms
        )
        near_present = observation.capture_rms >= self._config.near_rms_floor

        if not reference_active:
            # 参考侧没有能量 ⇒ 不存在双讲（既有实测：没有参考能量时 AEC3 完全不碰近端语音）。
            self._clear_episode()
            if near_present:
                return self._leave(REASON_NEAR_ONLY, DT_STATE_POSSIBLY_NEAR, False, ())
            return self._leave(REASON_BOTH_SILENT, DT_STATE_SILENT, None, ())

        for_codes, counter_codes, bits = self._evaluate(observation)
        if counter_codes:
            # 反向证据（回声相关性高 **且** ERLE 够用）：压掉本帧候选，按"非双讲"退出。
            self._clear_episode()
            return self._leave(
                REASON_COUNTER_EVIDENCE, DT_STATE_SINGLE_TALK, False, counter_codes
            )
        if not for_codes:
            # 远端在说话、没有反向证据、也没有支持证据 ⇒ 如实"未证明"（不是单讲）。
            self._clear_episode()
            return self._leave(REASON_INSUFFICIENT_EVIDENCE, DT_STATE_UNPROVEN, None, ())

        # 支持证据以"位"累积：同一码只算一次，不同码可叠加（长 episode 不重复计数）。
        self._evidence_mask |= bits
        if self._mask_strength() < self._config.min_evidence:
            # 证据条数不够：**不得**进入，也不允许靠"重复帧"把计数堆到门槛。
            self._dt_run = 0
            return self._result(
                state=DT_STATE_UNPROVEN,
                double_talk=None,
                evidence=for_codes,
                reason=REASON_EVIDENCE_ACCUMULATING,
            )

        if self._state == DT_STATE_DOUBLE_TALK:
            if self._exit_run > 0:
                # 退出倒计时中再次出现够强证据 ⇒ 清零倒计时（新的语音爆发）。
                self._exit_run = 0
                self._dt_run = 1
                return self._result(
                    state=DT_STATE_DOUBLE_TALK,
                    double_talk=True,
                    evidence=for_codes,
                    reason=REASON_DOUBLE_TALK,
                )
            self._dt_frames += 1
            return self._result(
                state=DT_STATE_DOUBLE_TALK,
                double_talk=True,
                evidence=for_codes,
                reason=REASON_DOUBLE_TALK,
            )

        self._dt_run += 1
        if self._dt_run >= self._config.enter_frames:
            return self._enter(for_codes)
        return self._result(
            state=DT_STATE_UNPROVEN,
            double_talk=None,
            evidence=for_codes,
            reason=REASON_ENTER_FRAMES_NOT_MET,
        )

    def observe_many(
        self, observations: Iterable[DoubleTalkObservation]
    ) -> Iterator[DoubleTalkResult]:
        """按序推进一批观测（离线重放/标注工具用）。"""

        for observation in observations:
            yield self.observe(observation)

    # ------------------------------------------------------------- internals

    def _clear_episode(self) -> None:
        """清零本 episode 的累积证据与进入计数（参考静音 / 反向证据 / 未证明）。"""

        self._dt_run = 0
        self._evidence_mask = 0

    def _mask_strength(self) -> int:
        """当前累积证据的**条数**（位计数；同一码只算一次）。"""

        return bin(self._evidence_mask).count("1")

    def _current_evidence(self) -> tuple[str, ...]:
        """把当前证据掩码还原成固定顺序的短码（只用于事实，绝不参与判定顺序）。"""

        mapping = (
            (_BIT_CORRELATION_LOW, DT_EVIDENCE_CORRELATION_LOW),
            (_BIT_ERLE_INSUFFICIENT, DT_EVIDENCE_ERLE_INSUFFICIENT),
            (_BIT_NEAR_EXCESS, DT_EVIDENCE_NEAR_EXCESS),
        )
        return tuple(code for bit, code in mapping if self._evidence_mask & bit)

    def _enter(self, for_codes: tuple[str, ...]) -> DoubleTalkResult:
        self._state = DT_STATE_DOUBLE_TALK
        self._dt_frames += 1
        self._dt_run = 0
        self._exit_run = 0
        return self._result(
            state=DT_STATE_DOUBLE_TALK,
            double_talk=True,
            evidence=for_codes,
            reason=REASON_DOUBLE_TALK,
        )

    def _leave(
        self,
        reason: str,
        next_state: str,
        double_talk: bool | None,
        evidence: tuple[str, ...],
    ) -> DoubleTalkResult:
        """非双讲路径的统一出口；双讲状态下走 `exit_frames` 滞回。"""

        if self._state == DT_STATE_DOUBLE_TALK:
            self._exit_run += 1
            if self._exit_run < self._config.exit_frames:
                # 滞回：还没连续够帧，**内部状态与对外事实都保持双讲**（避免抖动）。
                self._dt_frames += 1
                return self._result(
                    state=DT_STATE_DOUBLE_TALK,
                    double_talk=True,
                    evidence=self._current_evidence(),
                    reason=REASON_EXIT_FRAMES_NOT_MET,
                )
            # 滞回结束：真正退出，本 episode 的累积证据清零（下次进入要重新累积）。
            self._clear_episode()
        self._exit_run = 0
        self._state = next_state
        return self._result(
            state=next_state,
            double_talk=double_talk,
            evidence=evidence,
            reason=reason,
        )

    def _evaluate(
        self, observation: DoubleTalkObservation
    ) -> tuple[tuple[str, ...], tuple[str, ...], int]:
        """返回 `(支持证据码, 反向证据码, 支持证据位)`。"""

        config = self._config
        for_codes: list[str] = []
        counter: list[str] = []
        bits = 0

        correlation = observation.echo_correlation
        if correlation is None:
            # 相关性不可用：这条证据"不存在"——既不算低，也不算高（unavailable ≠ 双讲）。
            pass
        elif correlation <= config.correlation_dt_max:
            for_codes.append(DT_EVIDENCE_CORRELATION_LOW)
            bits |= _BIT_CORRELATION_LOW
        elif correlation >= config.correlation_block_min:
            counter.append(DT_EVIDENCE_COUNTER_CORRELATION_HIGH)

        erle = observation.erle_db
        if erle is None:
            sufficient = observation.erle_sufficient
        elif erle >= config.erle_sufficient_db:
            sufficient = True
        elif erle <= config.erle_min_db:
            sufficient = False
        else:
            sufficient = None
        if sufficient is False:
            for_codes.append(DT_EVIDENCE_ERLE_INSUFFICIENT)
            bits |= _BIT_ERLE_INSUFFICIENT
        elif sufficient is True:
            counter.append(DT_EVIDENCE_COUNTER_ERLE_SUFFICIENT)

        if observation.reference_rms > 0.0:
            ratio = observation.capture_rms / observation.reference_rms
            if ratio >= config.near_over_echo_ratio:
                for_codes.append(DT_EVIDENCE_NEAR_EXCESS)
                bits |= _BIT_NEAR_EXCESS

        # 反向证据要两条同时成立（回声相关性高 **且** ERLE 够用）才压掉候选。
        counter_hit = len(counter) >= 2
        return tuple(for_codes), tuple(counter) if counter_hit else (), bits

    def _observation_valid(self, observation: DoubleTalkObservation) -> bool:
        if isinstance(observation.frame_index, bool) or not isinstance(
            observation.frame_index, int
        ):
            return False
        for name in ("capture_rms", "reference_rms"):
            value = getattr(observation, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            number = float(value)
            if not math.isfinite(number) or number < 0.0:
                return False
        if not isinstance(observation.reference_active, bool):
            return False
        correlation = observation.echo_correlation
        if correlation is not None:
            if isinstance(correlation, bool) or not isinstance(correlation, (int, float)):
                return False
            number = float(correlation)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                return False
        erle = observation.erle_db
        if erle is not None:
            if isinstance(erle, bool) or not isinstance(erle, (int, float)):
                return False
            if not math.isfinite(float(erle)):
                return False
        if observation.erle_sufficient is not None and not isinstance(
            observation.erle_sufficient, bool
        ):
            return False
        return True

    def _tri_state(self) -> bool | None:
        if self._state == DT_STATE_DOUBLE_TALK:
            return True
        if self._state in (DT_STATE_SINGLE_TALK, DT_STATE_POSSIBLY_NEAR):
            return False
        return None

    def _step_scale(self) -> float:
        if self._state != DT_STATE_DOUBLE_TALK:
            return 1.0
        return self._config.freeze_step_scale

    def _result(
        self,
        *,
        state: str,
        double_talk: bool | None,
        evidence: tuple[str, ...],
        reason: str,
    ) -> DoubleTalkResult:
        in_double_talk = state == DT_STATE_DOUBLE_TALK
        return DoubleTalkResult(
            state=state,
            double_talk=double_talk,
            evidence_codes=evidence,
            reason=reason,
            enter_run=self._dt_run,
            exit_run=self._exit_run,
            step_scale=self._step_scale(),
            counts_toward_residual_floor=not in_double_talk,
            gate_hold=in_double_talk,
        )
__all__ = [
    "DEFAULT_STEP_SCALE",
    "DT_EVIDENCE_CODES",
    "DT_EVIDENCE_CORRELATION_LOW",
    "DT_EVIDENCE_COUNTER_CORRELATION_HIGH",
    "DT_EVIDENCE_COUNTER_ERLE_SUFFICIENT",
    "DT_EVIDENCE_ERLE_INSUFFICIENT",
    "DT_EVIDENCE_NEAR_EXCESS",
    "DT_FOR_CODES",
    "DT_MODE_FACTS",
    "DT_MODE_OFF",
    "DT_MODE_PROTECT",
    "DT_MODES",
    "DT_STATES",
    "DT_STATE_DOUBLE_TALK",
    "DT_STATE_POSSIBLY_NEAR",
    "DT_STATE_SILENT",
    "DT_STATE_SINGLE_TALK",
    "DT_STATE_UNPROVEN",
    "DoubleTalkConfig",
    "DoubleTalkDetector",
    "DoubleTalkObservation",
    "DoubleTalkResult",
    "REASONS",
    "double_talk_config_ok",
]
