"""P10 批次 R4-B5（B1）：双讲检测（DTD）纯离线测试。

覆盖：配置校验（阈值关系、布尔/NaN 拒绝）、帧输入校验（非法输入不推进、不猜）、
参考静音 ⇒ 不可能是双讲（`near_only` / 静音两态）、单讲（回声相关性强 + ERLE 够用 ⇒
反向证据）、双讲需"≥2 条独立证据 + 连续 enter_frames"、滞回退出（exit_frames）、
证据不可用（`correlation=None` / `erle=None`）不得当作低值使用、帧序回退按新会话重置、
以及三个只读开关（`step_scale` / `counts_toward_residual_floor` / `gate_hold`）的语义。

全部离线：无设备、无网络、无音频播放；只构造 `DoubleTalkObservation` 数值。
"""

from __future__ import annotations

import ast
import math
import unittest
from pathlib import Path

from app.contracts.base import ContractError
from app.audio.aec.double_talk import (
    DT_EVIDENCE_CORRELATION_LOW,
    DT_EVIDENCE_COUNTER_CORRELATION_HIGH,
    DT_EVIDENCE_COUNTER_ERLE_SUFFICIENT,
    DT_EVIDENCE_ERLE_INSUFFICIENT,
    DT_EVIDENCE_NEAR_EXCESS,
    DT_MODE_FACTS,
    DT_MODE_OFF,
    DT_MODE_PROTECT,
    DT_MODES,
    DT_STATE_DOUBLE_TALK,
    DT_STATE_POSSIBLY_NEAR,
    DT_STATE_SILENT,
    DT_STATE_SINGLE_TALK,
    DT_STATE_UNPROVEN,
    DoubleTalkConfig,
    DoubleTalkDetector,
    DoubleTalkObservation,
    double_talk_config_ok,
    REASON_BOTH_SILENT,
    REASON_COUNTER_EVIDENCE,
    REASON_DOUBLE_TALK,
    REASON_ENTER_FRAMES_NOT_MET,
    REASON_EVIDENCE_ACCUMULATING,
    REASON_EXIT_FRAMES_NOT_MET,
    REASON_INPUT_INVALID,
    REASON_NEAR_ONLY,
    REASON_NO_NEW_EVIDENCE,
    REASON_SINGLE_TALK,
)

MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / "double_talk.py"

# 本机口径（见 NEXT-SESSION §2）：真机回声 p50 ≈ 846、用户语音 p50 ≈ 1917、环境噪声 p50 47.7。
ECHO_RMS = 846.0
USER_RMS = 1917.0
NOISE_RMS = 47.7


def observation(
    frame_index: int,
    *,
    capture_rms: float,
    reference_rms: float,
    reference_active: bool = True,
    echo_correlation: float | None = None,
    erle_db: float | None = None,
    erle_sufficient: bool | None = None,
) -> DoubleTalkObservation:
    return DoubleTalkObservation(
        frame_index=frame_index,
        capture_rms=capture_rms,
        reference_rms=reference_rms,
        reference_active=reference_active,
        echo_correlation=echo_correlation,
        erle_db=erle_db,
        erle_sufficient=erle_sufficient,
    )


def dt_frame(frame_index: int) -> DoubleTalkObservation:
    """典型双讲帧：采集比参考响得多（1917 / 846 ≈ 2.27 ≥ 2.0）、相关性低、ERLE 低于门槛。"""

    return observation(
        frame_index,
        capture_rms=USER_RMS,
        reference_rms=ECHO_RMS,
        echo_correlation=0.05,
        erle_db=3.0,
    )


def single_talk_frame(frame_index: int) -> DoubleTalkObservation:
    """典型单讲帧：回声主导（相关性 0.7、ERLE 20 dB），采集不比参考响。"""

    return observation(
        frame_index,
        capture_rms=ECHO_RMS,
        reference_rms=ECHO_RMS,
        echo_correlation=0.72,
        erle_db=20.0,
    )


class DoubleTalkConfigTests(unittest.TestCase):
    def test_defaults_are_usable(self) -> None:
        config = DoubleTalkConfig()
        self.assertGreater(config.reference_active_rms, 0.0)
        self.assertGreater(config.near_rms_floor, 0.0)
        self.assertLess(config.correlation_dt_max, config.correlation_block_min)
        self.assertLess(config.erle_min_db, config.erle_sufficient_db)
        self.assertGreaterEqual(config.min_evidence, 2)
        self.assertEqual(config.freeze_step_scale, 0.0)

    def test_mode_values_are_closed_set(self) -> None:
        self.assertEqual(DT_MODES, ("off", "facts", "protect"))
        self.assertTrue(double_talk_config_ok(DT_MODE_OFF))
        self.assertTrue(double_talk_config_ok(DT_MODE_FACTS))
        self.assertTrue(double_talk_config_ok(DT_MODE_PROTECT))
        self.assertFalse(double_talk_config_ok("ON"))
        self.assertFalse(double_talk_config_ok(None))

    def test_correlation_thresholds_must_be_ordered(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkConfig(correlation_dt_max=0.8, correlation_block_min=0.5)

    def test_erle_thresholds_must_be_ordered(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkConfig(erle_min_db=12.0, erle_sufficient_db=12.0)

    def test_rms_thresholds_reject_bool_and_negative(self) -> None:
        for bad in (True, -1.0, float("nan")):
            with self.assertRaises(ContractError):
                DoubleTalkConfig(near_rms_floor=bad)

    def test_ratio_must_be_positive(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkConfig(near_over_echo_ratio=0.0)

    def test_frame_counts_must_be_positive_integers(self) -> None:
        for bad in (0, -3, 2.5, True):
            with self.assertRaises(ContractError):
                DoubleTalkConfig(enter_frames=bad)

    def test_min_evidence_cannot_exceed_available_codes(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkConfig(min_evidence=9)

    def test_freeze_scale_must_be_unit_fraction(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkConfig(freeze_step_scale=1.5)

    def test_details_are_flat_and_complete(self) -> None:
        details = DoubleTalkConfig().to_details()
        for key in (
            "reference_active_rms",
            "near_rms_floor",
            "correlation_dt_max",
            "correlation_block_min",
            "near_over_echo_ratio",
            "erle_min_db",
            "erle_sufficient_db",
            "min_evidence",
            "enter_frames",
            "exit_frames",
            "freeze_step_scale",
        ):
            self.assertIn(key, details)
            self.assertIsInstance(details[key], (int, float))


class DoubleTalkDetectorInputTests(unittest.TestCase):
    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkDetector(mode="hard")

    def test_invalid_observation_does_not_advance(self) -> None:
        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(0, capture_rms=float("nan"), reference_rms=ECHO_RMS)
        )
        self.assertEqual(result.reason, REASON_INPUT_INVALID)
        self.assertEqual(detector.frames, 0)
        self.assertIsNone(result.double_talk)

    def test_bool_rms_is_not_a_number_fact(self) -> None:
        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(0, capture_rms=True, reference_rms=ECHO_RMS)  # type: ignore[arg-type]
        )
        self.assertEqual(result.reason, REASON_INPUT_INVALID)

    def test_correlation_out_of_range_is_rejected(self) -> None:
        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(0, capture_rms=USER_RMS, reference_rms=ECHO_RMS, echo_correlation=1.4)
        )
        self.assertEqual(result.reason, REASON_INPUT_INVALID)

    def test_non_observation_type_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            DoubleTalkDetector().observe(object())  # type: ignore[arg-type]

    def test_frames_only_count_valid_observations(self) -> None:
        detector = DoubleTalkDetector()
        detector.observe(observation(0, capture_rms=NOISE_RMS, reference_rms=0.0))
        detector.observe(observation(1, capture_rms=float("inf"), reference_rms=ECHO_RMS))
        detector.observe(observation(2, capture_rms=NOISE_RMS, reference_rms=0.0))
        self.assertEqual(detector.frames, 2)


class NoReferenceMeansNoDoubleTalkTests(unittest.TestCase):
    def test_silent_room_is_not_double_talk(self) -> None:
        result = DoubleTalkDetector().observe(
            observation(0, capture_rms=NOISE_RMS, reference_rms=0.0, reference_active=False)
        )
        self.assertEqual(result.state, DT_STATE_SILENT)
        self.assertIsNone(result.double_talk)
        self.assertEqual(result.reason, REASON_BOTH_SILENT)

    def test_near_only_is_not_double_talk(self) -> None:
        """参考帧静音 + 用户在说话：没有回声可消 ⇒ **绝不**判双讲（既有实测 near_only −0.00 dB）。"""

        result = DoubleTalkDetector().observe(
            observation(0, capture_rms=USER_RMS, reference_rms=0.0, reference_active=False)
        )
        self.assertEqual(result.state, DT_STATE_POSSIBLY_NEAR)
        self.assertIs(result.double_talk, False)
        self.assertEqual(result.reason, REASON_NEAR_ONLY)

    def test_quiet_reference_alone_does_not_support_double_talk(self) -> None:
        """参考 RMS 低于 reference_active_rms（即使 reference_active=True）也按"没有远端"处理。"""

        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(
                0,
                capture_rms=USER_RMS,
                reference_rms=10.0,
                reference_active=True,
                echo_correlation=0.05,
                erle_db=1.0,
            )
        )
        self.assertEqual(result.state, DT_STATE_POSSIBLY_NEAR)
        self.assertIs(result.double_talk, False)

    def test_near_only_frame_does_not_build_evidence_for_later(self) -> None:
        detector = DoubleTalkDetector()
        detector.observe(
            observation(0, capture_rms=USER_RMS, reference_rms=0.0, reference_active=False)
        )
        result = detector.observe(dt_frame(1))
        self.assertEqual(result.state, DT_STATE_UNPROVEN)
        self.assertIsNone(result.double_talk)


class DoubleTalkEvidenceTests(unittest.TestCase):
    def test_single_evidence_is_never_enough(self) -> None:
        """只有"相关性低"一条证据（ERLE 达标、能量也不超）⇒ 不得进双讲。"""

        detector = DoubleTalkDetector()
        for index in range(20):
            result = detector.observe(
                observation(
                    index,
                    capture_rms=ECHO_RMS,
                    reference_rms=ECHO_RMS,
                    echo_correlation=0.05,
                    erle_db=20.0,
                )
            )
        self.assertEqual(result.state, DT_STATE_UNPROVEN)
        self.assertIsNone(result.double_talk)
        self.assertEqual(result.reason, REASON_EVIDENCE_ACCUMULATING)
        self.assertEqual(result.evidence_codes, (DT_EVIDENCE_CORRELATION_LOW,))

    def test_low_correlation_plus_insufficient_erle_enters_double_talk(self) -> None:
        detector = DoubleTalkDetector()
        for index in range(5):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertIs(result.double_talk, True)
        self.assertEqual(result.reason, REASON_DOUBLE_TALK)
        self.assertEqual(
            result.evidence_codes,
            (DT_EVIDENCE_CORRELATION_LOW, DT_EVIDENCE_ERLE_INSUFFICIENT, DT_EVIDENCE_NEAR_EXCESS),
        )

    def test_two_evidence_without_excess_ratio_still_enters(self) -> None:
        """相关性低 + ERLE 不足（采集不比参考响）也够 2 条证据。"""

        detector = DoubleTalkDetector()
        for index in range(5):
            result = detector.observe(
                observation(
                    index,
                    capture_rms=400.0,
                    reference_rms=ECHO_RMS,
                    echo_correlation=0.1,
                    erle_db=2.0,
                )
            )
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(
            result.evidence_codes,
            (DT_EVIDENCE_CORRELATION_LOW, DT_EVIDENCE_ERLE_INSUFFICIENT),
        )

    def test_excess_ratio_plus_erle_enters_without_correlation(self) -> None:
        """相关性还不可用（None）时，剩余两条证据仍可成立；`None` 绝不当低相关性用。"""

        detector = DoubleTalkDetector()
        for index in range(5):
            result = detector.observe(
                observation(
                    index,
                    capture_rms=USER_RMS,
                    reference_rms=ECHO_RMS,
                    echo_correlation=None,
                    erle_db=2.0,
                )
            )
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(
            result.evidence_codes,
            (DT_EVIDENCE_ERLE_INSUFFICIENT, DT_EVIDENCE_NEAR_EXCESS),
        )

    def test_erle_unavailable_is_not_insufficient(self) -> None:
        """ERLE 不可用（None）不得当成"不足"：只剩 1 条证据 ⇒ 未证明。"""

        detector = DoubleTalkDetector()
        for index in range(10):
            result = detector.observe(
                observation(
                    index,
                    capture_rms=USER_RMS,
                    reference_rms=ECHO_RMS,
                    echo_correlation=None,
                    erle_db=None,
                    erle_sufficient=None,
                )
            )
        self.assertEqual(result.state, DT_STATE_UNPROVEN)
        self.assertIsNone(result.double_talk)

    def test_erle_sufficient_flag_can_stand_alone(self) -> None:
        """只看 `erle_sufficient` 事实（无 dB 数值）也要能表达"够用/不够用"。"""

        detector = DoubleTalkDetector()
        for index in range(5):
            result = detector.observe(
                observation(
                    index,
                    capture_rms=USER_RMS,
                    reference_rms=ECHO_RMS,
                    echo_correlation=0.05,
                    erle_sufficient=False,
                )
            )
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)

        single = DoubleTalkDetector()
        for index in range(5):
            result = single.observe(
                observation(
                    index,
                    capture_rms=ECHO_RMS,
                    reference_rms=ECHO_RMS,
                    echo_correlation=0.7,
                    erle_sufficient=True,
                )
            )
        self.assertEqual(result.state, DT_STATE_SINGLE_TALK)

    def test_counter_evidence_requires_both_conditions(self) -> None:
        """反向证据必须"相关性高 + ERLE 够用"同时成立；只有一条时走普通证据路径。"""

        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(
                0,
                capture_rms=USER_RMS,
                reference_rms=ECHO_RMS,
                echo_correlation=0.7,
                erle_db=3.0,
            )
        )
        # 相关性高（一条反向）+ ERLE 不足（一条支持）+ 采集超参考（一条支持）
        self.assertEqual(result.state, DT_STATE_UNPROVEN)
        self.assertEqual(result.reason, REASON_ENTER_FRAMES_NOT_MET)

    def test_counter_evidence_suppresses_candidates(self) -> None:
        detector = DoubleTalkDetector()
        result = detector.observe(single_talk_frame(0))
        self.assertEqual(result.state, DT_STATE_SINGLE_TALK)
        self.assertIs(result.double_talk, False)
        self.assertEqual(result.reason, REASON_COUNTER_EVIDENCE)
        self.assertEqual(
            result.evidence_codes,
            (
                DT_EVIDENCE_COUNTER_CORRELATION_HIGH,
                DT_EVIDENCE_COUNTER_ERLE_SUFFICIENT,
            ),
        )

    def test_counter_evidence_resets_accumulation(self) -> None:
        """累积到一半时出现反向证据 ⇒ 计数清零，必须重新累积。"""

        detector = DoubleTalkDetector()
        for index in range(3):
            detector.observe(dt_frame(index))
        detector.observe(single_talk_frame(3))
        result = detector.observe(dt_frame(4))
        self.assertEqual(result.reason, REASON_ENTER_FRAMES_NOT_MET)
        self.assertEqual(result.enter_run, 1)


class HysteresisTests(unittest.TestCase):
    def _drive_into_double_talk(self, detector: DoubleTalkDetector) -> None:
        for index in range(5):
            detector.observe(dt_frame(index))

    def test_enter_frames_are_required(self) -> None:
        detector = DoubleTalkDetector(config=DoubleTalkConfig(enter_frames=3))
        for index in range(2):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_UNPROVEN)
        result = detector.observe(dt_frame(2))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)

    def test_repeated_evidence_frames_still_reach_enter_threshold(self) -> None:
        """同一批证据的重复帧不得让计数停住（长 episode 必须仍能进入双讲）。"""

        detector = DoubleTalkDetector()
        for index in range(5):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(detector.double_talk_frames, 1)

    def test_exit_requires_consecutive_frames(self) -> None:
        detector = DoubleTalkDetector()
        self._drive_into_double_talk(detector)
        for index in range(5, 5 + 14):
            result = detector.observe(single_talk_frame(index))
            self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
            self.assertEqual(result.reason, REASON_EXIT_FRAMES_NOT_MET)
        result = detector.observe(single_talk_frame(5 + 14))
        self.assertEqual(result.state, DT_STATE_SINGLE_TALK)
        self.assertIs(result.double_talk, False)

    def test_exit_countdown_is_cancelled_by_new_double_talk_frames(self) -> None:
        """退出倒计时被新的双讲帧打断：期间保持双讲，且重新进入仍需再满足 enter_frames。"""

        detector = DoubleTalkDetector()
        self._drive_into_double_talk(detector)
        for index in range(5, 12):
            detector.observe(single_talk_frame(index))
        self.assertEqual(detector.exit_run, 7)
        detector.observe(dt_frame(12))
        self.assertEqual(detector.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(detector.exit_run, 0)
        result = None
        for index in range(13, 13 + 6):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(result.reason, REASON_DOUBLE_TALK)

    def test_reentry_after_full_exit_requires_enter_frames_again(self) -> None:
        """完全退出后重新进入必须再满足 enter_frames（不降门槛）。"""

        detector = DoubleTalkDetector()
        self._drive_into_double_talk(detector)
        for index in range(5, 5 + 15):
            detector.observe(single_talk_frame(index))
        self.assertEqual(detector.state, DT_STATE_SINGLE_TALK)
        for index in range(20, 25):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(result.reason, REASON_DOUBLE_TALK)

    def test_single_talk_then_double_talk_reenters(self) -> None:
        detector = DoubleTalkDetector()
        for index in range(20):
            detector.observe(single_talk_frame(index))
        self.assertEqual(detector.state, DT_STATE_SINGLE_TALK)
        for index in range(20, 25):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)

    def test_frame_index_rollback_resets_counts(self) -> None:
        """帧序回退（新会话/重放）必须清零累积，绝不继承上一会话的计数。"""

        detector = DoubleTalkDetector()
        for index in range(3):
            detector.observe(dt_frame(index))
        self.assertEqual(detector.enter_run, 3)
        detector.observe(dt_frame(0))
        self.assertEqual(detector.enter_run, 1)
        self.assertEqual(detector.frames, 1)

    def test_reset_clears_everything(self) -> None:
        detector = DoubleTalkDetector()
        self._drive_into_double_talk(detector)
        detector.reset()
        self.assertEqual(detector.state, DT_STATE_UNPROVEN)
        self.assertEqual(detector.double_talk_frames, 0)
        self.assertEqual(detector.frames, 0)
        self.assertEqual(detector.enter_run, 0)


class ReadOnlySwitchTests(unittest.TestCase):
    def test_step_scale_is_zero_only_in_double_talk(self) -> None:
        detector = DoubleTalkDetector(config=DoubleTalkConfig(freeze_step_scale=0.0))
        result = detector.observe(single_talk_frame(0))
        self.assertEqual(result.step_scale, 1.0)
        detector.reset()
        for index in range(5):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.state, DT_STATE_DOUBLE_TALK)
        self.assertEqual(result.step_scale, 0.0)

    def test_step_scale_follows_config(self) -> None:
        detector = DoubleTalkDetector(config=DoubleTalkConfig(freeze_step_scale=0.25))
        for index in range(5):
            result = detector.observe(dt_frame(index))
        self.assertEqual(result.step_scale, 0.25)

    def test_residual_floor_and_gate_switches(self) -> None:
        detector = DoubleTalkDetector()
        single = detector.observe(single_talk_frame(0))
        self.assertTrue(single.counts_toward_residual_floor)
        self.assertFalse(single.gate_hold)
        detector.reset()
        for index in range(5):
            double = detector.observe(dt_frame(index))
        self.assertFalse(double.counts_toward_residual_floor)
        self.assertTrue(double.gate_hold)

    def test_unknown_state_keeps_counting_toward_floor(self) -> None:
        """未证明（证据不足）不得被当成双讲排除地板——否则会凭空丢失地板样本。"""

        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(
                0,
                capture_rms=ECHO_RMS,
                reference_rms=ECHO_RMS,
                echo_correlation=0.05,
                erle_db=20.0,
            )
        )
        self.assertIsNone(result.double_talk)
        self.assertTrue(result.counts_toward_residual_floor)
        self.assertFalse(result.gate_hold)

    def test_details_are_flat_and_include_mode(self) -> None:
        detector = DoubleTalkDetector(mode=DT_MODE_PROTECT)
        details = detector.to_details()
        self.assertEqual(details["mode"], DT_MODE_PROTECT)
        self.assertEqual(details["state"], DT_STATE_UNPROVEN)
        for key, value in details.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, (str, int, float, bool, type(None)))


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_does_not_import_devices_network_or_safety_chain(self) -> None:
        """硬边界：本模块纯离线、不碰设备/音频依赖/安全链（AST 级检查）。"""

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden = (
            "numpy",
            "sounddevice",
            "app.safety",
            "app.executor",
            "app.plans",
            "app.audio.capture",
            "app.tts",
        )
        for name in imported:
            for prefix in forbidden:
                self.assertFalse(
                    name == prefix or name.startswith(prefix + "."),
                    f"double_talk.py 不得导入 {name}",
                )

    def test_evidence_codes_are_ascii_short_codes(self) -> None:
        from app.audio.aec import double_talk as module

        for code in module.DT_EVIDENCE_CODES + module.REASONS + module.DT_STATES:
            self.assertLessEqual(len(code), 64)
            self.assertTrue(code.isascii())
            self.assertEqual(code, code.lower())

    def test_all_results_expose_flat_details(self) -> None:
        result = DoubleTalkDetector().observe(dt_frame(0))
        details = result.to_details()
        self.assertIsInstance(details["evidence_codes"], str)
        for value in details.values():
            self.assertIsInstance(value, (str, int, float, bool, type(None)))

    def test_observe_many_matches_sequential_observations(self) -> None:
        frames = [dt_frame(index) for index in range(6)]
        batch = list(DoubleTalkDetector().observe_many(frames))
        sequential = DoubleTalkDetector()
        one_by_one = [sequential.observe(frame) for frame in frames]
        self.assertEqual([item.state for item in batch], [item.state for item in one_by_one])
        self.assertEqual(batch[-1].double_talk, one_by_one[-1].double_talk)

    def test_no_math_domain_errors_on_extreme_numbers(self) -> None:
        detector = DoubleTalkDetector()
        result = detector.observe(
            observation(
                0,
                capture_rms=0.0,
                reference_rms=0.0,
                reference_active=False,
            )
        )
        self.assertTrue(math.isfinite(result.step_scale))


if __name__ == "__main__":  # pragma: no cover - 便于单文件直跑
    unittest.main()
