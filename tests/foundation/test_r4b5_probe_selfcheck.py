"""P10 R4-B5（B4）真机探针的**离线自检**：合成真机信号验证判定链，不打开任何设备。

`tools/p10_r4b5_doubletalk_probe.py` 只在真机上跑；本文件用合成信号把它的
**判定链**钉住，保证真机那一轮不会因为探针自身的口径错误而给出假结论：

1. 只放音（单讲真值）⇒ **不得**判双讲（误报率必须 0）；
2. 放音 + 近端压倒性人声 ⇒ 必须检出双讲（检出率 ≥ 0.8）；
3. 近端-only（参考静音）⇒ 一定不是双讲；
4. 冻结支路的双讲帧权重漂移**小于**不冻结支路（保护方向正确）。

全部离线：合成回声路径 + 生产同款检测器/NLMS，不联网、不开设备。
"""

from __future__ import annotations

import unittest

import numpy as np

from app.audio.aec.double_talk import DoubleTalkConfig, DoubleTalkDetector
from tools.p10_r4b5_doubletalk_probe import CAPTURE_RATE, analyse_phase, run_pipeline

DELAY_SAMPLES = 6400  # 400 ms
ECHO_GAIN = 0.5
SECONDS = 6


def signals(*, user_scale: float, seed: int = 5) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    count = CAPTURE_RATE * SECONDS
    far = rng.normal(0.0, 0.25, count).astype(np.float32)
    echo = np.zeros_like(far)
    echo[DELAY_SAMPLES:] = ECHO_GAIN * far[:-DELAY_SAMPLES]
    user = rng.normal(0.0, user_scale, count).astype(np.float32)
    noise = rng.normal(0.0, 0.02, count).astype(np.float32)
    return far, echo, user + noise


class ProbeSelfCheckTests(unittest.TestCase):
    def test_echo_only_is_never_double_talk(self) -> None:
        far, echo, noise = signals(user_scale=0.0)
        result = analyse_phase(
            mic16=(echo + noise).astype(np.float32),
            far16=far,
            delay_samples=DELAY_SAMPLES,
            detector=DoubleTalkDetector(config=DoubleTalkConfig(), mode="facts"),
            nlms_scale=0.0,
            truth_double_talk=False,
            erle_flags=None,
        )
        self.assertEqual(result["double_talk_frames"], 0, result["states"])

    def test_dominant_near_speaker_is_detected(self) -> None:
        far, echo, near = signals(user_scale=0.9)
        result = analyse_phase(
            mic16=(echo + near).astype(np.float32),
            far16=far,
            delay_samples=DELAY_SAMPLES,
            detector=DoubleTalkDetector(config=DoubleTalkConfig(), mode="facts"),
            nlms_scale=0.0,
            truth_double_talk=True,
            erle_flags=None,
        )
        ratio = result["double_talk_ratio"] or 0.0
        self.assertGreaterEqual(ratio, 0.8, result["states"])
        self.assertIn("double_talk", result["states"])

    def test_frozen_branch_drifts_less_than_unfrozen(self) -> None:
        far, echo, near = signals(user_scale=0.9)
        frozen = analyse_phase(
            mic16=(echo + near).astype(np.float32),
            far16=far,
            delay_samples=DELAY_SAMPLES,
            detector=DoubleTalkDetector(config=DoubleTalkConfig(), mode="facts"),
            nlms_scale=0.0,
            truth_double_talk=True,
            erle_flags=None,
        )
        unfrozen = analyse_phase(
            mic16=(echo + near).astype(np.float32),
            far16=far,
            delay_samples=DELAY_SAMPLES,
            detector=DoubleTalkDetector(config=DoubleTalkConfig(), mode="facts"),
            nlms_scale=1.0,
            truth_double_talk=True,
            erle_flags=None,
        )
        self.assertLess(
            frozen["weight_drift_on_double_talk"], unfrozen["weight_drift_on_double_talk"]
        )

    def test_near_only_reference_silent_is_not_double_talk(self) -> None:
        _far, _echo, near = signals(user_scale=0.3)
        result = analyse_phase(
            mic16=near,
            far16=np.zeros_like(near),
            delay_samples=0,
            detector=DoubleTalkDetector(config=DoubleTalkConfig(), mode="facts"),
            nlms_scale=0.0,
            truth_double_talk=False,
            erle_flags=None,
        )
        self.assertEqual(result["double_talk_frames"], 0)
        self.assertEqual(result["states"].get("possibly_near"), result["frames_analysed"])

    def test_pipeline_reports_facts_and_a_verdict(self) -> None:
        far, echo, noise = signals(user_scale=0.0)
        _far2, _echo2, near_dt = signals(user_scale=0.9)
        report = run_pipeline(
            echo_mic16=(echo + noise).astype(np.float32),
            far_echo16=far,
            dt_mic16=(echo + near_dt).astype(np.float32),
            far_dt16=far,
            delay_samples=DELAY_SAMPLES,
            user_mic16=near_dt,
            talk_threshold=600.0,
            residual_margin=3.0,
        )
        self.assertEqual(report["false_positive_ratio"], 0.0)
        self.assertGreaterEqual(report["detection_ratio"] or 0.0, 0.8)
        self.assertIn("decision", report)
        # 冻结支路的漂移必须小于不冻结支路（保护方向正确）。
        self.assertLess(
            report["frozen_vs_unfrozen"]["frozen_weight_drift"],
            report["frozen_vs_unfrozen"]["unfrozen_weight_drift"],
        )
        self.assertIn("erle", report)


if __name__ == "__main__":  # pragma: no cover - 便于单文件直跑
    unittest.main()
