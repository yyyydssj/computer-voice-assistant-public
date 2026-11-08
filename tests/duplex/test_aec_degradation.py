"""P10 批次 R2a：AEC/降噪逐层降级框架的离线测试。

覆盖：能力探测输入校验、完整链、缺 NS、缺参考帧、设备变化重校准、回声路径过长、
延迟估计不可靠、以及"不伪装已消除回声 / 保留文字模式 / 降级不关闭会话"三条硬不变量。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.contracts.base import ContractError
from app.duplex.barge_in import DEGRADE_FULL, DEGRADE_NO_AEC, DEGRADE_STRICT
from app.audio.aec import (
    CAPABILITY_HARDWARE,
    CAPABILITY_SOFTWARE,
    CAPABILITY_UNAVAILABLE,
    LEVEL_AEC_ONLY,
    LEVEL_FULL,
    LEVEL_HALF_DUPLEX,
    LEVEL_STRICT,
    REASON_AEC_WITHOUT_NS,
    REASON_DEVICE_CHANGED,
    REASON_ECHO_PATH_EXCEEDS_BUDGET,
    REASON_FULL_CHAIN,
    REASON_REFERENCE_MISSING,
    REASON_REVERB_RISK,
    REASON_UNRELIABLE_DELAY,
    AecCapabilityProbe,
    AecDegradationController,
    AecError,
    AecPolicy,
    AecProcessingPlan,
)


def probe(**overrides) -> AecCapabilityProbe:
    values = {
        "aec_capability": CAPABILITY_HARDWARE,
        "reference_available": True,
        "reference_matches_output": True,
        "estimated_echo_path_ms": 40,
    }
    values.update(overrides)
    return AecCapabilityProbe(**values)


class AecPolicyTests(unittest.TestCase):
    def test_defaults_enable_ns_and_trust_the_delay_estimate(self) -> None:
        policy = AecPolicy()
        self.assertTrue(policy.noise_suppression_available)
        self.assertTrue(policy.delay_estimate_reliable)

    def test_zero_max_echo_path_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AecPolicy(max_echo_path_ms=0)

    def test_non_boolean_ns_flag_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AecPolicy(noise_suppression_available=1)  # type: ignore[arg-type]

    def test_non_boolean_delay_flag_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AecPolicy(delay_estimate_reliable="yes")  # type: ignore[arg-type]


class AecProbeTests(unittest.TestCase):
    def test_unknown_capability_is_rejected(self) -> None:
        with self.assertRaises(AecError):
            probe(aec_capability="neural")

    def test_negative_echo_path_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            probe(estimated_echo_path_ms=-1)

    def test_non_integer_echo_path_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            probe(estimated_echo_path_ms=1.5)  # type: ignore[arg-type]

    def test_non_boolean_reference_flags_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            probe(reference_available=1)  # type: ignore[arg-type]

    def test_zero_device_epoch_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            probe(device_epoch=0)


class AecFullChainTests(unittest.TestCase):
    def test_hardware_aec_with_ns_keeps_the_full_chain(self) -> None:
        plan = AecDegradationController().plan(probe())
        self.assertEqual(plan.level, LEVEL_FULL)
        self.assertEqual(plan.reason, REASON_FULL_CHAIN)
        self.assertTrue(plan.aec_applied)
        self.assertTrue(plan.noise_suppression_applied)
        self.assertTrue(plan.echo_cancellation_claimed)
        self.assertEqual(plan.barge_in_degradation, DEGRADE_FULL)
        self.assertFalse(plan.pause_capture_during_playback)
        self.assertTrue(plan.realtime_conversation_available)

    def test_software_aec_is_accepted_like_hardware(self) -> None:
        plan = AecDegradationController().plan(probe(aec_capability=CAPABILITY_SOFTWARE))
        self.assertEqual(plan.level, LEVEL_FULL)
        self.assertTrue(plan.aec_applied)

    def test_missing_ns_downgrades_to_aec_only(self) -> None:
        controller = AecDegradationController(policy=AecPolicy(noise_suppression_available=False))
        plan = controller.plan(probe())
        self.assertEqual(plan.level, LEVEL_AEC_ONLY)
        self.assertEqual(plan.reason, REASON_AEC_WITHOUT_NS)
        self.assertTrue(plan.aec_applied)
        self.assertFalse(plan.noise_suppression_applied)
        self.assertEqual(plan.barge_in_degradation, DEGRADE_FULL)

    def test_no_echo_path_keeps_the_full_chain(self) -> None:
        plan = AecDegradationController().plan(probe(estimated_echo_path_ms=0))
        self.assertEqual(plan.level, LEVEL_FULL)
        self.assertFalse(plan.echo_risk)

    def test_plan_details_are_short_codes_and_numbers(self) -> None:
        details = AecDegradationController().plan(probe()).to_details()
        self.assertEqual(details["level"], LEVEL_FULL)
        self.assertIn("text_mode_available", details)
        for name in ("pcm", "payload", "audio", "path"):
            self.assertNotIn(name, details)


class AecDegradationLadderTests(unittest.TestCase):
    def test_unavailable_aec_with_reference_downgrades_to_strict(self) -> None:
        plan = AecDegradationController().plan(probe(aec_capability=CAPABILITY_UNAVAILABLE))
        self.assertEqual(plan.level, LEVEL_STRICT)
        self.assertFalse(plan.aec_applied)
        self.assertFalse(plan.echo_cancellation_claimed)
        self.assertEqual(plan.barge_in_degradation, DEGRADE_NO_AEC)
        self.assertTrue(plan.realtime_conversation_available)

    def test_missing_reference_downgrades_to_strict(self) -> None:
        plan = AecDegradationController().plan(probe(reference_available=False))
        self.assertEqual(plan.level, LEVEL_STRICT)
        self.assertEqual(plan.reason, REASON_REFERENCE_MISSING)

    def test_reference_not_matching_output_downgrades_to_strict(self) -> None:
        plan = AecDegradationController().plan(probe(reference_matches_output=False))
        self.assertEqual(plan.level, LEVEL_STRICT)
        self.assertFalse(plan.aec_applied)

    def test_long_echo_path_with_aec_downgrades_without_claiming_cancellation(self) -> None:
        plan = AecDegradationController().plan(probe(estimated_echo_path_ms=250))
        self.assertEqual(plan.level, LEVEL_STRICT)
        self.assertEqual(plan.reason, REASON_ECHO_PATH_EXCEEDS_BUDGET)
        self.assertFalse(plan.aec_applied)
        self.assertFalse(plan.echo_cancellation_claimed)
        self.assertTrue(plan.echo_risk)
        # 仍保留实时会话与文字模式，只是打断更保守。
        self.assertTrue(plan.realtime_conversation_available)
        self.assertFalse(plan.pause_capture_during_playback)
        self.assertEqual(plan.barge_in_degradation, DEGRADE_NO_AEC)

    def test_long_echo_path_without_aec_falls_back_to_half_duplex(self) -> None:
        plan = AecDegradationController().plan(
            probe(aec_capability=CAPABILITY_UNAVAILABLE, estimated_echo_path_ms=250)
        )
        self.assertEqual(plan.level, LEVEL_HALF_DUPLEX)
        self.assertEqual(plan.reason, REASON_REVERB_RISK)
        self.assertTrue(plan.pause_capture_during_playback)
        self.assertFalse(plan.realtime_conversation_available)
        self.assertEqual(plan.barge_in_degradation, DEGRADE_STRICT)

    def test_unreliable_delay_estimate_falls_back_to_half_duplex(self) -> None:
        controller = AecDegradationController(policy=AecPolicy(delay_estimate_reliable=False))
        plan = controller.plan(probe(estimated_echo_path_ms=40))
        self.assertEqual(plan.level, LEVEL_HALF_DUPLEX)
        self.assertEqual(plan.reason, REASON_UNRELIABLE_DELAY)
        self.assertFalse(plan.aec_applied)

    def test_device_change_requires_recalibration_at_strict_level(self) -> None:
        plan = AecDegradationController().plan(probe(device_changed=True))
        self.assertEqual(plan.level, LEVEL_STRICT)
        self.assertEqual(plan.reason, REASON_DEVICE_CHANGED)

    def test_device_epoch_change_is_detected_between_plans(self) -> None:
        controller = AecDegradationController()
        first = controller.plan(probe(device_epoch=7))
        self.assertEqual(first.level, LEVEL_FULL)
        second = controller.plan(probe(device_epoch=8))
        self.assertEqual(second.level, LEVEL_STRICT)
        self.assertEqual(second.reason, REASON_DEVICE_CHANGED)
        self.assertEqual(controller.last_device_epoch, 8)

    def test_same_epoch_keeps_capability_stable(self) -> None:
        controller = AecDegradationController()
        controller.plan(probe(device_epoch=3))
        second = controller.plan(probe(device_epoch=3))
        self.assertEqual(second.level, LEVEL_FULL)

    def test_no_aec_and_no_echo_risk_still_downgrades(self) -> None:
        plan = AecDegradationController().plan(
            probe(aec_capability=CAPABILITY_UNAVAILABLE, estimated_echo_path_ms=0)
        )
        self.assertEqual(plan.level, LEVEL_STRICT)
        self.assertFalse(plan.echo_cancellation_claimed)

    def test_ladder_never_upgrades_on_missing_capability(self) -> None:
        controller = AecDegradationController()
        levels = {
            controller.plan(probe(aec_capability=CAPABILITY_UNAVAILABLE)).level,
            controller.plan(probe(aec_capability=CAPABILITY_UNAVAILABLE, estimated_echo_path_ms=300)).level,
        }
        self.assertNotIn(LEVEL_FULL, levels)

    def test_probe_type_is_enforced(self) -> None:
        with self.assertRaises(ContractError):
            AecDegradationController().plan(object())  # type: ignore[arg-type]


class AecHardInvariantTests(unittest.TestCase):
    def _all_levels(self):
        return [
            AecDegradationController().plan(probe()),  # full
            AecDegradationController(policy=AecPolicy(noise_suppression_available=False)).plan(probe()),
            AecDegradationController().plan(probe(aec_capability=CAPABILITY_UNAVAILABLE)),
            AecDegradationController().plan(
                probe(aec_capability=CAPABILITY_UNAVAILABLE, estimated_echo_path_ms=300)
            ),
        ]

    def test_text_mode_is_always_available(self) -> None:
        for plan in self._all_levels():
            self.assertTrue(plan.text_mode_available, plan.level)

    def test_echo_cancellation_is_never_claimed_without_aec(self) -> None:
        for plan in self._all_levels():
            if not plan.aec_applied:
                self.assertFalse(plan.echo_cancellation_claimed, plan.level)

    def test_half_duplex_only_appears_with_paused_capture(self) -> None:
        for plan in self._all_levels():
            self.assertEqual(plan.pause_capture_during_playback, plan.level == LEVEL_HALF_DUPLEX)

    def test_degradation_strings_stay_within_barge_in_vocabulary(self) -> None:
        allowed = {DEGRADE_FULL, DEGRADE_NO_AEC, DEGRADE_STRICT}
        for plan in self._all_levels():
            self.assertIn(plan.barge_in_degradation, allowed)

    def test_every_level_reports_a_fixed_reason_code(self) -> None:
        allowed = {
            REASON_FULL_CHAIN,
            REASON_AEC_WITHOUT_NS,
            REASON_ECHO_PATH_EXCEEDS_BUDGET,
            REASON_REFERENCE_MISSING,
            REASON_DEVICE_CHANGED,
            REASON_REVERB_RISK,
            REASON_UNRELIABLE_DELAY,
        }
        for plan in self._all_levels():
            self.assertIn(plan.reason, allowed)

    def test_plan_result_type_is_stable(self) -> None:
        for plan in self._all_levels():
            self.assertIsInstance(plan, AecProcessingPlan)


class AecBoundaryTests(unittest.TestCase):
    def test_module_has_no_device_file_or_network_imports(self) -> None:
        source = (
            Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / "__init__.py"
        )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        banned_roots = {
            "socket",
            "subprocess",
            "threading",
            "asyncio",
            "ctypes",
            "pathlib",
            "os",
            "shutil",
            "wave",
            "sounddevice",
            "sherpa_onnx",
            "numpy",
            "requests",
            "urllib",
            "http",
        }
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned_roots)

    def test_module_never_imports_safety_chain_modules(self) -> None:
        source = (
            Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / "__init__.py"
        )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
        offenders = {name for name in modules if ".".join(name.split(".")[:2]) in banned}
        self.assertEqual(offenders, set())


if __name__ == "__main__":
    unittest.main()
