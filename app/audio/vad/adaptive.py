"""P9 批次 2.1：环境自适应语音阈值（纯离线、fail closed）。

本模块把一段**环境静音采样**折算成一个能量阈值，供自动收音驱动使用。它只描述输入
边界，**不**代表 ASR 可靠性、**不**构成任何授权或任务事实，也不进入安全链。

硬边界（离线模块）：
- 不访问设备、不读写文件、不联网、不创建线程/定时器；
- 只导入 stdlib、``app.audio.bus``（``AudioPacket``）与 ``app.contracts.base``；
  不导入语音闭环驱动（避免拉起音频依赖）、不导入任何安全链模块。

判定规则（fail closed 优先）：
- 帧 RMS 与自动收音驱动 ``auto_driver.py::_pcm16_rms`` 同口径（PCM16 LE 单声道 RMS
  幅值，空载荷/坏帧记 0.0）；本模块只复刻同一算法，不导入该驱动；
- 参考值取噪声分位数（线性插值）或最大值（``NoiseFloorMethod``）；
- ``threshold = max(floor_rms, multiplier * reference)``；结果等于下限时来源记 ``floor``，
  否则记 ``adaptive``；
- 一帧未收到、未收满、或没有可判定的样本 → ``AdaptiveThresholdError``；
  参考值达到 ``ceiling_rms`` → ``AdaptiveThresholdError``：两种拒绝的 ``str()`` 只包含
  固定短代码（``insufficient_noise_frames`` / ``noise_too_high``），不含音频内容、路径
  或异常正文。
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from app.audio.bus import AudioPacket
from app.contracts.base import ContractError, require_positive

INSUFFICIENT_NOISE_FRAMES = "insufficient_noise_frames"
NOISE_TOO_HIGH = "noise_too_high"

SOURCE_ADAPTIVE = "adaptive"
SOURCE_FLOOR = "floor"

_ERROR_CODES = frozenset({INSUFFICIENT_NOISE_FRAMES, NOISE_TOO_HIGH})
_SOURCES = frozenset({SOURCE_ADAPTIVE, SOURCE_FLOOR})


class AdaptiveThresholdError(ValueError):
    """拒绝给出自适应阈值。

    ``str()`` **只**返回固定短代码；构造时的未知文本一律收敛为
    ``insufficient_noise_frames``，避免把音频内容、路径或异常正文带出模块。
    """

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else INSUFFICIENT_NOISE_FRAMES
        super().__init__(self.code)


class _NonContractObservation(ContractError, TypeError):
    """非 ``AudioPacket`` 的观察输入；同时满足 ``ContractError`` 与 ``TypeError`` 语义。"""


class NoiseFloorMethod(str, Enum):
    """噪声参考值的取法。"""

    PERCENTILE = "percentile"
    MAX = "max"


def _require_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"{name} 必须是有限数值")
    return float(value)


@dataclass(frozen=True, slots=True)
class AdaptiveThresholdConfig:
    """自适应阈值参数；只描述输入边界，不改变任何安全判定。

    ``method`` 是规格未列出的可选入口（默认 ``PERCENTILE``）：冻结 API 没有给出
    分位数/最大值两种参考值的选择字段，这里补一个带默认值的末尾字段，既保留默认
    分位数行为，也让 ``NoiseFloorMethod.MAX`` 可达；位置参数顺序不变。
    """

    sample_duration_ms: int = 1200
    frame_duration_ms: int = 20
    floor_rms: float = 200.0
    ceiling_rms: float = 800.0
    multiplier: float = 3.0
    percentile: float = 0.95
    method: NoiseFloorMethod = NoiseFloorMethod.PERCENTILE

    def __post_init__(self) -> None:
        require_positive("sample_duration_ms", self.sample_duration_ms)
        require_positive("frame_duration_ms", self.frame_duration_ms)
        floor = _require_number("floor_rms", self.floor_rms)
        ceiling = _require_number("ceiling_rms", self.ceiling_rms)
        multiplier = _require_number("multiplier", self.multiplier)
        percentile = _require_number("percentile", self.percentile)
        if floor < 1.0:
            raise ContractError("floor_rms 必须 >= 1")
        if ceiling <= floor:
            raise ContractError("ceiling_rms 必须大于 floor_rms")
        if multiplier <= 1.0:
            raise ContractError("multiplier 必须大于 1")
        if not 0.0 < percentile < 1.0:
            raise ContractError("percentile 必须在 (0,1) 内")
        method = self.method
        if isinstance(method, NoiseFloorMethod):
            resolved_method = method
        elif isinstance(method, str):
            try:
                resolved_method = NoiseFloorMethod(method)
            except ValueError as exc:
                raise ContractError("method 只支持 percentile 或 max") from exc
        else:
            raise ContractError("method 必须是 NoiseFloorMethod")
        object.__setattr__(self, "floor_rms", floor)
        object.__setattr__(self, "ceiling_rms", ceiling)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "percentile", percentile)
        object.__setattr__(self, "method", resolved_method)

    @property
    def required_frames(self) -> int:
        """``sample_duration_ms / frame_duration_ms`` 向上取整得到的所需帧数。"""

        return -(-self.sample_duration_ms // self.frame_duration_ms)


@dataclass(frozen=True, slots=True)
class NoiseFloorSample:
    """一段静音采样的只读统计；``to_details()`` 只含数值，不含任何音频。"""

    frame_count: int
    min_rms: float
    p50_rms: float
    p95_rms: float
    max_rms: float

    def to_details(self) -> dict[str, float | int]:
        return {
            "frame_count": self.frame_count,
            "min_rms": self.min_rms,
            "p50_rms": self.p50_rms,
            "p95_rms": self.p95_rms,
            "max_rms": self.max_rms,
        }


@dataclass(frozen=True, slots=True)
class AdaptiveThresholdDecision:
    """自适应阈值的只读结果；``source`` 只有 ``adaptive`` / ``floor`` 两种。"""

    threshold_rms: float
    noise: NoiseFloorSample
    multiplier: float
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.noise, NoiseFloorSample):
            raise ContractError("noise 必须是 NoiseFloorSample")
        if self.source not in _SOURCES:
            raise ContractError("source 只支持 adaptive 或 floor")
        threshold = self.threshold_rms
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ContractError("threshold_rms 必须是正有限数值")
        value = float(threshold)
        if not math.isfinite(value) or value <= 0.0:
            raise ContractError("threshold_rms 必须是正有限数值")
        object.__setattr__(self, "threshold_rms", value)
        object.__setattr__(self, "multiplier", _require_number("multiplier", self.multiplier))

    def to_details(self) -> dict[str, object]:
        return {
            "threshold_rms": self.threshold_rms,
            "multiplier": self.multiplier,
            "source": self.source,
            "noise": self.noise.to_details(),
        }


def _pcm16_rms(payload: object) -> float:
    """按 ``pcm_s16le`` 计算 PCM16 RMS 幅值；空载荷/非字节载荷返回 0.0。

    算法与自动收音驱动 ``auto_driver.py::_pcm16_rms`` 逐行一致（奇数字节只取可用
    偶数部分），保证自适应判定与驱动口径相同。
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return 0.0
    raw = bytes(payload)
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        return 0.0
    samples = struct.unpack(f"<{usable // 2}h", raw[:usable])
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples))


def _percentile(ordered: Sequence[float], percentile: float) -> float:
    """对已排序序列做线性插值分位数（与 ``numpy.percentile`` 默认口径一致）。"""

    count = len(ordered)
    if count == 0:
        raise AdaptiveThresholdError(INSUFFICIENT_NOISE_FRAMES)
    if count == 1:
        return float(ordered[0])
    rank = float(percentile) * (count - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[int(rank)])
    weight = rank - lower
    low = float(ordered[lower])
    high = float(ordered[upper])
    return low + (high - low) * weight


class AdaptiveThresholdCalibrator:
    """按帧累积环境静音样本，并在收满后给出阈值；零帧或收不满一律拒绝。"""

    def __init__(self, *, config: AdaptiveThresholdConfig | None = None) -> None:
        if config is None:
            config = AdaptiveThresholdConfig()
        elif not isinstance(config, AdaptiveThresholdConfig):
            raise ContractError("config 必须是 AdaptiveThresholdConfig 或 None")
        self._config = config
        self._values: list[float] = []

    @property
    def config(self) -> AdaptiveThresholdConfig:
        return self._config

    @property
    def frame_count(self) -> int:
        return len(self._values)

    @property
    def is_collecting(self) -> bool:
        """未收满所需帧数时为 ``True``；收满即为 ``False``。"""

        return len(self._values) < self._config.required_frames

    def observe(self, packet: AudioPacket) -> bool:
        """观察一帧并返回"是否已收满"；非 ``AudioPacket`` 输入一律拒绝。"""

        if not isinstance(packet, AudioPacket):
            raise _NonContractObservation("observe 只接受 AudioPacket")
        self._values.append(_pcm16_rms(packet.payload))
        return not self.is_collecting

    def decide(self) -> AdaptiveThresholdDecision:
        """给出阈值；零帧或未收满 → ``insufficient_noise_frames``，参考值过高 → ``noise_too_high``。"""

        values = self._values
        if not values or len(values) < self._config.required_frames:
            raise AdaptiveThresholdError(INSUFFICIENT_NOISE_FRAMES)
        ordered = sorted(values)
        sample = NoiseFloorSample(
            frame_count=len(values),
            min_rms=ordered[0],
            p50_rms=_percentile(ordered, 0.5),
            p95_rms=_percentile(ordered, 0.95),
            max_rms=ordered[-1],
        )
        if self._config.method is NoiseFloorMethod.MAX:
            reference = sample.max_rms
        else:
            reference = _percentile(ordered, float(self._config.percentile))
        if reference >= float(self._config.ceiling_rms):
            raise AdaptiveThresholdError(NOISE_TOO_HIGH)
        floor = float(self._config.floor_rms)
        threshold = max(floor, float(self._config.multiplier) * reference)
        source = SOURCE_FLOOR if threshold == floor else SOURCE_ADAPTIVE
        return AdaptiveThresholdDecision(
            threshold_rms=threshold,
            noise=sample,
            multiplier=float(self._config.multiplier),
            source=source,
        )

    def reset(self) -> None:
        self._values.clear()


def adaptive_energy_detector(decision: AdaptiveThresholdDecision) -> Callable[[AudioPacket], bool]:
    """把决策折算成与自动收音驱动同口径的能量判定（``rms >= threshold``）。"""

    if not isinstance(decision, AdaptiveThresholdDecision):
        raise ContractError("decision 必须是 AdaptiveThresholdDecision")
    threshold = float(decision.threshold_rms)

    def detect(packet: AudioPacket) -> bool:
        return _pcm16_rms(packet.payload) >= threshold

    return detect


def measure_noise_floor(
    packets: Iterable[AudioPacket], *, config: AdaptiveThresholdConfig | None = None
) -> AdaptiveThresholdDecision:
    """一次性口径：喂入若干静音帧并直接给出阈值；不足或过吵同样拒绝。"""

    calibrator = AdaptiveThresholdCalibrator(config=config)
    for packet in packets:
        calibrator.observe(packet)
    return calibrator.decide()


__all__ = [
    "AdaptiveThresholdCalibrator",
    "AdaptiveThresholdConfig",
    "AdaptiveThresholdDecision",
    "AdaptiveThresholdError",
    "INSUFFICIENT_NOISE_FRAMES",
    "NOISE_TOO_HIGH",
    "NoiseFloorMethod",
    "NoiseFloorSample",
    "SOURCE_ADAPTIVE",
    "SOURCE_FLOOR",
    "adaptive_energy_detector",
    "measure_noise_floor",
]
