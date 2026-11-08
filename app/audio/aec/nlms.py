"""P10 批次 R2b-1：NLMS 回声消除与处理链（纯离线、可注入）。

只做两件事：
1. ``NlmsEchoCanceller`` —— 用参考信号自适应预测并减去扬声器回声（NLMS），
   并给出**可复核**的抑制量（ERLE）与是否真正应用；
2. ``AecProcessingChain`` —— 把参考时间线、消除器与 R2a 的降级级别接起来，
   输出每帧的 ``applied/fallback`` 事实。

硬不变量（与设计文档 12 一致）：
- 参考不存在/迟到/过期 → **不得**伪称已消除回声，回退直通并写明原因；
- 双讲（near-end 正在说话）时不应用消除输出（滤波器仍可继续自适应），
  避免把用户开头语音削掉；
- 输出只含处理后的 PCM 与数值指标，不产生任何授权/计划事实。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.audio.aec.reference import (
    REFERENCE_OK,
    AecReferenceConfig,
    AecReferenceError,
    AecReferenceTimeline,
    ReferenceWindow,
)
from app.contracts.base import ContractError, require_positive

# ERLE 门限口径：低于 gate 视为"未真正抑制回声"，必须回退
DEFAULT_ERLE_GATE_DB = 6.0


def pcm16_to_float(payload: object, *, expected_samples: int | None = None) -> np.ndarray:
    """PCM16 LE → [-1,1) float32；长度非法一律 ``AecReferenceError``。"""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ContractError("pcm 必须是 bytes/bytearray/memoryview")
    raw = bytes(payload)
    if len(raw) % 2:
        raise AecReferenceError("invalid_pcm_length")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if expected_samples is not None and samples.size != expected_samples:
        raise AecReferenceError("invalid_pcm_length")
    return samples


def float_to_pcm16(samples: np.ndarray) -> bytes:
    """[-1,1) float32 → PCM16 LE；越界样本按限幅处理。"""

    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).round().astype("<i2").tobytes()


def _energy(samples: np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float32)
    return float(np.dot(array, array))


def _erle_db(reference_energy: float, error_energy: float) -> float:
    """回声抑制量（dB）。

    口径：**没有参考能量就没有"回声被抑制"这回事** —— 静音帧一律返回 0 dB，
    而不是伪造 ∞；只有在参考非零且残差精确为零（理想数值情形）时才返回 ∞。
    """

    if reference_energy <= 0.0:
        return 0.0
    if error_energy <= 0.0:
        return float("inf")
    return float(10.0 * np.log10(reference_energy / error_energy))


@dataclass(frozen=True, slots=True)
class NlmsConfig:
    """NLMS 参数；``filter_length_ms`` 决定尾长（默认覆盖 120 ms 回声路径）。"""

    sample_rate: int = 16000
    frame_duration_ms: int = 20
    filter_length_ms: int = 160
    step_size: float = 0.4
    regularization: float = 1e-6
    erle_gate_db: float = DEFAULT_ERLE_GATE_DB
    double_talk_step_scale: float = 0.0
    """R4-B5（B3）：**双讲帧**的步长倍率（0~1，默认 0.0 = 冻结权重更新）。

    原文第 4 点：双讲时误差里既有残余回声又有近端语音，继续全速自适应会把近端语音学进
    滤波器（吸收 / 发散）。默认改为**冻结**（只输出直通、权重不动），要让滤波器在双讲期
    仍缓慢跟踪就调成小正数（例如 0.1）；``1.0`` 等价于改动前行为。
    """

    def __post_init__(self) -> None:
        for name in ("sample_rate", "frame_duration_ms", "filter_length_ms"):
            require_positive(name, getattr(self, name))
        step = self.step_size
        if isinstance(step, bool) or not isinstance(step, (int, float)) or not 0.0 < float(step) < 2.0:
            raise ContractError("step_size 必须在 (0,2) 内")
        reg = self.regularization
        if isinstance(reg, bool) or not isinstance(reg, (int, float)) or float(reg) < 0.0:
            raise ContractError("regularization 必须非负")
        gate = self.erle_gate_db
        if isinstance(gate, bool) or not isinstance(gate, (int, float)):
            raise ContractError("erle_gate_db 必须是数值")
        scale = self.double_talk_step_scale
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise ContractError("double_talk_step_scale 必须是数值")
        if not 0.0 <= float(scale) <= 1.0:
            raise ContractError("double_talk_step_scale 必须在 0~1 内（不得放大步长）")
        object.__setattr__(self, "step_size", float(step))
        object.__setattr__(self, "regularization", float(reg))
        object.__setattr__(self, "erle_gate_db", float(gate))
        object.__setattr__(self, "double_talk_step_scale", float(scale))

    @property
    def frame_samples(self) -> int:
        return -(-self.sample_rate * self.frame_duration_ms // 1000)

    @property
    def filter_samples(self) -> int:
        return -(-self.sample_rate * self.filter_length_ms // 1000)


@dataclass(frozen=True, slots=True)
class EchoCancellationResult:
    """一帧消除结果；``output`` 已按 double-talk/门限决定是"消除后"还是"直通"。"""

    output: np.ndarray
    applied: bool
    fallback_reason: str | None
    erle_db: float
    cumulative_erle_db: float
    double_talk: bool

    @property
    def frame_erle_db(self) -> float:
        return self.erle_db

    def to_details(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "fallback_reason": self.fallback_reason,
            "erle_db": None if self.erle_db == float("inf") else round(self.erle_db, 3),
            "cumulative_erle_db": (
                None
                if self.cumulative_erle_db == float("inf")
                else round(self.cumulative_erle_db, 3)
            ),
            "double_talk": self.double_talk,
            "samples": int(self.output.size),
        }


class NlmsEchoCanceller:
    """归一化最小均方（NLMS）回声消除器（纯离线、确定性）。"""

    def __init__(self, *, config: NlmsConfig | None = None) -> None:
        self._config = config or NlmsConfig()
        self._weights = np.zeros(self._config.filter_samples, dtype=np.float32)
        self._regressor = np.zeros(self._config.filter_samples, dtype=np.float32)
        self._reference_energy_total = 0.0
        self._error_energy_total = 0.0
        self._frames = 0

    @property
    def config(self) -> NlmsConfig:
        return self._config

    @property
    def weights(self) -> np.ndarray:
        """当前滤波器系数（只读快照）。"""

        return self._weights.copy()

    @property
    def frames_processed(self) -> int:
        return self._frames

    @property
    def cumulative_erle_db(self) -> float:
        return _erle_db(self._reference_energy_total, self._error_energy_total)

    def reset(self) -> None:
        """清空自适应状态（设备变化/新会话时必须重校准）。"""

        self._weights = np.zeros(self._config.filter_samples, dtype=np.float32)
        self._regressor = np.zeros(self._config.filter_samples, dtype=np.float32)
        self._reference_energy_total = 0.0
        self._error_energy_total = 0.0
        self._frames = 0

    def cancel(
        self,
        *,
        reference: object,
        capture: object,
        double_talk: bool = False,
        step_scale: float | None = None,
    ) -> EchoCancellationResult:
        """用 ``reference`` 预测并减去 ``capture`` 中的回声。

        ``double_talk=True`` 时不应用消除输出（回退直通）；此时**权重更新按
        ``double_talk_step_scale`` 缩放**（默认 0.0 = 冻结）——原文第 4 点：双讲时若继续
        全速自适应，滤波器会把近端语音学进去。``step_scale`` 可显式覆盖（只允许更小或相等，
        便于上层做分级保护）；参考与采集必须是同一长度的 PCM16 LE 帧。
        """

        expected = self._config.frame_samples
        ref = pcm16_to_float(reference, expected_samples=expected)
        mic = pcm16_to_float(capture, expected_samples=expected)
        if not isinstance(double_talk, bool):
            raise ContractError("double_talk 必须是布尔值")
        if step_scale is not None:
            if isinstance(step_scale, bool) or not isinstance(step_scale, (int, float)):
                raise ContractError("step_scale 必须是数值或 None")
            if not 0.0 <= float(step_scale) <= 1.0:
                raise ContractError("step_scale 必须在 0~1 内（不得放大步长）")
        effective_scale = self._config.double_talk_step_scale if double_talk else 1.0
        if step_scale is not None:
            effective_scale = min(effective_scale, float(step_scale))
        update_gain = float(self._config.step_size) * float(effective_scale)

        output = np.empty(expected, dtype=np.float32)
        # 逐样本递归 NLMS（标准归一化形式）：regressor 是"当前样本之前的滤波器窗口"，
        # 权重更新为 w += (mu / (||x||^2 + reg)) * e * x。
        # 不用整块梯度近似——块式更新里每个输出样本对应的权重索引不同，
        # 用同一段权重做线性梯度是错的（实测不收敛）。
        # ``update_gain == 0``（双讲冻结的默认档）时不写权重数组，避免无意义的整段矩阵运算。
        weights = self._weights
        regressor = self._regressor
        for index in range(expected):
            predicted = float(np.dot(weights, regressor))
            error_sample = float(mic[index]) - predicted
            if update_gain > 0.0:
                norm = float(np.dot(regressor, regressor)) + self._config.regularization
                weights += (update_gain / norm) * error_sample * regressor
            output[index] = np.float32(error_sample)
            regressor[1:] = regressor[:-1]
            regressor[0] = ref[index]

        error_energy = _energy(output)
        reference_energy = _energy(ref)
        self._reference_energy_total += reference_energy
        self._error_energy_total += error_energy

        frame_erle = _erle_db(reference_energy, error_energy)
        should_apply = (not double_talk) and frame_erle >= self._config.erle_gate_db
        reason: str | None = None
        if double_talk:
            reason = "double_talk_detected"
        elif frame_erle < self._config.erle_gate_db:
            reason = "erle_below_gate"
        if not should_apply:
            output = mic.astype(np.float32, copy=True)

        self._frames += 1
        return EchoCancellationResult(
            output=output,
            applied=should_apply,
            fallback_reason=reason,
            erle_db=frame_erle,
            cumulative_erle_db=self.cumulative_erle_db,
            double_talk=double_talk,
        )


@dataclass(frozen=True, slots=True)
class AecChainFrame:
    """处理链一帧的结果：处理后的 PCM + 事实（是否应用、为何回退、抑制量）。"""

    output: np.ndarray
    applied: bool
    fallback_reason: str
    reference_state: str
    erle_db: float
    cumulative_erle_db: float
    double_talk: bool

    def to_details(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "fallback_reason": self.fallback_reason,
            "reference_state": self.reference_state,
            "erle_db": None if self.erle_db == float("inf") else round(self.erle_db, 3),
            "cumulative_erle_db": (
                None
                if self.cumulative_erle_db == float("inf")
                else round(self.cumulative_erle_db, 3)
            ),
            "double_talk": self.double_talk,
            "samples": int(self.output.size),
        }


class AecProcessingChain:
    """参考时间线 + NLMS 的最小处理链（纯离线、可注入）。"""

    def __init__(
        self,
        *,
        reference_config: AecReferenceConfig | None = None,
        nlms_config: NlmsConfig | None = None,
    ) -> None:
        self._timeline = AecReferenceTimeline(config=reference_config)
        self._canceller = NlmsEchoCanceller(config=nlms_config)

    @property
    def timeline(self) -> AecReferenceTimeline:
        return self._timeline

    @property
    def canceller(self) -> NlmsEchoCanceller:
        return self._canceller

    def push_playback(self, *, at_ms: int, pcm: object) -> None:
        """送入实际送往输出设备的一帧参考 PCM。"""

        self._timeline.push(at_ms, pcm)

    def process(
        self,
        *,
        capture_at_ms: int,
        capture: object,
        double_talk: bool = False,
        step_scale: float | None = None,
    ) -> AecChainFrame:
        """处理一帧采集音频；参考不可用时如实回退直通。"""

        mic = pcm16_to_float(capture, expected_samples=self._canceller.config.frame_samples)
        window: ReferenceWindow = self._timeline.window(capture_at_ms)
        if not window.ok or window.pcm is None:
            return AecChainFrame(
                output=mic.astype(np.float32, copy=True),
                applied=False,
                fallback_reason=f"reference_{window.state}",
                reference_state=window.state,
                erle_db=0.0,
                cumulative_erle_db=self._canceller.cumulative_erle_db,
                double_talk=double_talk,
            )
        result = self._canceller.cancel(
            reference=float_to_pcm16(window.pcm),
            capture=float_to_pcm16(mic),
            double_talk=double_talk,
            step_scale=step_scale,
        )
        return AecChainFrame(
            output=result.output,
            applied=result.applied,
            fallback_reason=result.fallback_reason or "",
            reference_state=REFERENCE_OK,
            erle_db=result.erle_db,
            cumulative_erle_db=result.cumulative_erle_db,
            double_talk=result.double_talk,
        )


__all__ = [
    "DEFAULT_ERLE_GATE_DB",
    "AecChainFrame",
    "AecProcessingChain",
    "EchoCancellationResult",
    "NlmsConfig",
    "NlmsEchoCanceller",
    "float_to_pcm16",
    "pcm16_to_float",
]
