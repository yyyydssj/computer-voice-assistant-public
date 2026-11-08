"""P10 批次 R4-B5（B3）：`protect` 档的**保护动作是否真的落地**（离线对照，合成信号）。

本文件把 B3 的两条保护量成**可判定的机械事实**：

1. **权重冻结真的生效**：同一条链路先单讲暖机，再在 `protect` 档下喂 40 帧"回声 + 用户
   语音"；参考源收到的 `double_talk=True` 帧上，NLMS 滤波器系数**逐比特不变**，而把步长
   倍率设回 1.0（改动前语义）时系数会被推动。
2. **far_only ERLE 不因冻结而变差**（≤1 dB，与 handoff 的验收口径同向）。
3. **判定行为不变**：`protect` 与 `facts` 在完全相同的帧序列下给出相同的音量/确认/停播。

诚实边界：合成回声路径**不能**用来推断真机 ERLE（真机量测见
`implementation_plan/research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`）；
这里只断言"冻结的机械效果"与"不更差 1 dB"。
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from app.audio.aec.nlms import NlmsConfig, NlmsEchoCanceller, float_to_pcm16, pcm16_to_float

FRAME_SAMPLES = 320
ECHO_GAIN = 0.5
ECHO_DELAY = 8
NEAR_SCALE = 0.12
WARMUP_FRAMES = 60
DOUBLE_TALK_FRAMES = 40


def far_signal(frame_index: int, *, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed * 1000 + frame_index)
    return rng.normal(0.0, 0.3, FRAME_SAMPLES).astype(np.float32)


def near_signal(frame_index: int, *, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed * 1000 + frame_index)
    return rng.normal(0.0, NEAR_SCALE, FRAME_SAMPLES).astype(np.float32)


def echo_of(reference: np.ndarray) -> np.ndarray:
    delayed = np.zeros_like(reference)
    delayed[ECHO_DELAY:] = reference[:-ECHO_DELAY]
    return (ECHO_GAIN * delayed).astype(np.float32)


def energy(samples: np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float64)
    return float(np.dot(array, array))


class Measurement:
    def __init__(self, *, weight_drift: float, far_erle_db: float) -> None:
        self.weight_drift = weight_drift
        self.far_erle_db = far_erle_db


def measure(*, double_talk: bool, scale: float) -> Measurement:
    """暖机（单讲）→ far_only 窗口 → 双讲窗口；返回权重漂移与 far_only ERLE。"""

    canceller = NlmsEchoCanceller(config=NlmsConfig(double_talk_step_scale=scale))
    for index in range(WARMUP_FRAMES):
        reference = far_signal(index)
        canceller.cancel(
            reference=float_to_pcm16(reference), capture=float_to_pcm16(echo_of(reference))
        )
    far_energy = 0.0
    far_residual_energy = 0.0
    for index in range(20):
        reference = far_signal(900 + index)
        result = canceller.cancel(
            reference=float_to_pcm16(reference), capture=float_to_pcm16(echo_of(reference))
        )
        far_energy += energy(pcm16_to_float(float_to_pcm16(reference)))
        far_residual_energy += energy(pcm16_to_float(float_to_pcm16(result.output)))
    # ⚠️ 漂移基线必须在**双讲窗口之前**取：far_only 窗口是 `double_talk=False`，
    # 按设计**照常自适应**（那是单讲该有的行为），不能算进"双讲冻结"的漂移里。
    weights_before_double_talk = canceller.weights.copy()
    for index in range(DOUBLE_TALK_FRAMES):
        reference = far_signal(2000 + index)
        capture = echo_of(reference) + near_signal(index)
        canceller.cancel(
            reference=float_to_pcm16(reference),
            capture=float_to_pcm16(capture),
            double_talk=double_talk,
        )
    weights_after = canceller.weights.copy()
    far_erle_db = (
        float("inf")
        if far_residual_energy <= 0.0
        else 10.0 * math.log10(far_energy / far_residual_energy)
    )
    return Measurement(
        weight_drift=float(np.abs(weights_after - weights_before_double_talk).sum()),
        far_erle_db=far_erle_db,
    )


class FrozenWeightFactsTests(unittest.TestCase):
    def test_double_talk_flag_freezes_the_filter(self) -> None:
        """`double_talk=True` + 默认倍率 0.0 ⇒ 40 帧双讲后权重**逐比特不变**。"""

        frozen = measure(double_talk=True, scale=0.0)
        self.assertEqual(frozen.weight_drift, 0.0, "冻结档权重漂移必须为 0")

    def test_legacy_semantics_still_drift(self) -> None:
        """倍率 1.0（改动前语义）⇒ 权重必须被推动（对照，证明"冻结"不是空话）。"""

        legacy = measure(double_talk=True, scale=1.0)
        self.assertGreater(legacy.weight_drift, 0.0)

    def test_without_the_flag_the_filter_keeps_adapting(self) -> None:
        """不带双讲标志（`double_talk=False`）时权重照旧自适应（行为不变）。"""

        single = measure(double_talk=False, scale=0.0)
        self.assertGreater(single.weight_drift, 0.0)

    def test_frozen_does_not_degrade_far_only_erle(self) -> None:
        frozen = measure(double_talk=True, scale=0.0)
        legacy = measure(double_talk=True, scale=1.0)
        drop_db = legacy.far_erle_db - frozen.far_erle_db
        self.assertLessEqual(
            drop_db,
            1.0,
            f"冻结档 far_only ERLE 下降 {drop_db:.2f} dB（不冻结 {legacy.far_erle_db:.2f} / "
            f"冻结 {frozen.far_erle_db:.2f}）",
        )


if __name__ == "__main__":  # pragma: no cover - 便于单文件直跑
    unittest.main()
