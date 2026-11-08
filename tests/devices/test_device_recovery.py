"""P10 批次 R3：设备会话与恢复策略的离线测试。

覆盖：观察校验、设备采用/新设备忽略、健康判定（只有输入输出都 READY 才算健康）、
安全空闲点判定（轮次中/服务中/等待确认一律延后）、**输出优先**、只重连固定设备、
忙/权限拒绝/不兼容不自动重试、失败计数与提示、`epoch` 下一轮生效，以及三条硬不变量
（恢复不触碰授权、不切未知设备、不静默）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.contracts.base import ContractError
from app.devices.session import (
    ACTION_FALLBACK_TO_TEXT,
    ACTION_NONE,
    ACTION_NOTIFY_USER,
    ACTION_RECONNECT_INPUT,
    ACTION_RECONNECT_OUTPUT,
    REASON_BUSY_ELSEWHERE,
    REASON_DEFERRED_ACTIVE_TURN,
    REASON_DEFERRED_AWAITING_CONFIRMATION,
    REASON_DEVICE_UNKNOWN,
    REASON_HEALTHY,
    REASON_INCOMPATIBLE,
    REASON_INPUT_LOST,
    REASON_NEW_DEVICE_IGNORED,
    REASON_NO_FIXED_DEVICE,
    REASON_OUTPUT_LOST,
    REASON_PERMISSION_DENIED,
    REASON_RECONNECT_ATTEMPTED,
    REASON_RECONNECT_FAILED,
    REASON_TEXT_MODE_KEPT,
    DeviceHealth,
    DeviceKind,
    DeviceObservation,
    DeviceRecoveryError,
    DeviceRecoveryPolicy,
)


def input_obs(health: DeviceHealth, device_id="mic-1", epoch=1) -> DeviceObservation:
    return DeviceObservation(DeviceKind.INPUT, health, device_id, epoch)


def output_obs(health: DeviceHealth, device_id="spk-1", epoch=1) -> DeviceObservation:
    return DeviceObservation(DeviceKind.OUTPUT, health, device_id, epoch)


def ready_policy(**kwargs) -> DeviceRecoveryPolicy:
    policy = DeviceRecoveryPolicy(**kwargs)
    policy.register(input_obs(DeviceHealth.READY))
    policy.register(output_obs(DeviceHealth.READY))
    return policy


def idle(policy: DeviceRecoveryPolicy):
    return policy.decide(turn_active=False, service_active=False, awaiting_confirmation=False)


class DeviceObservationTests(unittest.TestCase):
    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(DeviceRecoveryError):
            DeviceObservation("speaker", DeviceHealth.READY, "x", 1)  # type: ignore[arg-type]

    def test_invalid_health_is_rejected(self) -> None:
        with self.assertRaises(DeviceRecoveryError):
            DeviceObservation(DeviceKind.INPUT, "fine", "x", 1)  # type: ignore[arg-type]

    def test_zero_epoch_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            DeviceObservation(DeviceKind.INPUT, DeviceHealth.READY, "x", 0)

    def test_details_are_short_codes(self) -> None:
        details = input_obs(DeviceHealth.READY).to_details()
        self.assertEqual(details["kind"], "input")
        self.assertEqual(details["health"], "ready")
        for name in ("authorization", "plan", "pcm"):
            self.assertNotIn(name, details)


class DeviceAdoptionTests(unittest.TestCase):
    def test_first_ready_device_is_adopted(self) -> None:
        policy = DeviceRecoveryPolicy()
        facts = policy.register(input_obs(DeviceHealth.READY, "mic-1", 1))
        self.assertEqual(policy.fixed_device_id(DeviceKind.INPUT), "mic-1")
        self.assertEqual(facts["adopted_device_id"], "mic-1")

    def test_new_device_is_never_adopted_automatically(self) -> None:
        policy = DeviceRecoveryPolicy()
        policy.register(input_obs(DeviceHealth.READY, "mic-1", 1))
        policy.register(output_obs(DeviceHealth.READY, "spk-1", 1))
        policy.register(input_obs(DeviceHealth.READY, "mic-2", 2))  # 换了设备
        self.assertEqual(policy.fixed_device_id(DeviceKind.INPUT), "mic-1")
        self.assertTrue(policy.new_device_seen)

    def test_device_switch_applies_to_next_round_only(self) -> None:
        policy = ready_policy()
        policy.note_device_switch(DeviceKind.INPUT, device_id="mic-9")
        self.assertEqual(policy.fixed_device_id(DeviceKind.INPUT), "mic-9")
        self.assertEqual(policy.epoch(DeviceKind.INPUT), 2)

    def test_device_switch_requires_a_device(self) -> None:
        policy = ready_policy()
        with self.assertRaises(ContractError):
            policy.note_device_switch(DeviceKind.INPUT, device_id=None)


class DeviceHealthDecisionTests(unittest.TestCase):
    def test_all_ready_is_healthy(self) -> None:
        action = idle(ready_policy())
        self.assertEqual(action.action, ACTION_NONE)
        self.assertEqual(action.reason, REASON_HEALTHY)
        self.assertTrue(action.already_ready)
        self.assertTrue(action.safety_point_reached)

    def test_never_confirmed_device_is_not_healthy(self) -> None:
        policy = DeviceRecoveryPolicy()
        action = idle(policy)
        self.assertFalse(action.already_ready)
        self.assertIn(REASON_DEVICE_UNKNOWN, action.messages)

    def test_input_only_ready_is_not_healthy(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        action = idle(policy)
        self.assertFalse(action.already_ready)

    def test_disconnected_output_triggers_output_reconnect(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        action = idle(policy)
        self.assertEqual(action.action, ACTION_RECONNECT_OUTPUT)
        self.assertIsNotNone(action.open_request)
        self.assertEqual(action.open_request.preferred_device_id, "spk-1")
        self.assertIn(REASON_OUTPUT_LOST, action.messages)

    def test_disconnected_input_triggers_input_reconnect(self) -> None:
        policy = ready_policy()
        policy.register(input_obs(DeviceHealth.DISCONNECTED, "mic-1", 2))
        action = idle(policy)
        self.assertEqual(action.action, ACTION_RECONNECT_INPUT)
        self.assertEqual(action.open_request.preferred_device_id, "mic-1")
        self.assertIn(REASON_INPUT_LOST, action.messages)

    def test_output_is_recovered_before_input(self) -> None:
        policy = ready_policy()
        policy.register(input_obs(DeviceHealth.DISCONNECTED, "mic-1", 3))
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 3))
        action = idle(policy)
        self.assertEqual(action.action, ACTION_RECONNECT_OUTPUT)

    def test_busy_device_is_not_retried_automatically(self) -> None:
        policy = ready_policy()
        policy.register(input_obs(DeviceHealth.BUSY, "mic-1", 2))
        action = idle(policy)
        self.assertEqual(action.action, ACTION_FALLBACK_TO_TEXT)
        self.assertIn(REASON_BUSY_ELSEWHERE, action.messages)

    def test_permission_denied_is_not_retried_automatically(self) -> None:
        policy = ready_policy()
        policy.register(input_obs(DeviceHealth.PERMISSION_DENIED, "mic-1", 2))
        action = idle(policy)
        self.assertEqual(action.action, ACTION_FALLBACK_TO_TEXT)
        self.assertIn(REASON_PERMISSION_DENIED, action.messages)

    def test_incompatible_device_reports_fallback(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.INCOMPATIBLE, "spk-1", 2))
        action = idle(policy)
        self.assertIn(REASON_INCOMPATIBLE, action.messages)

    def test_missing_fixed_device_blocks_reconnect(self) -> None:
        policy = DeviceRecoveryPolicy()
        policy.register(input_obs(DeviceHealth.READY, None, 1))
        policy.register(output_obs(DeviceHealth.READY, None, 1))
        policy.register(input_obs(DeviceHealth.DISCONNECTED, None, 2))
        action = idle(policy)
        self.assertIn(REASON_NO_FIXED_DEVICE, action.messages)


class DeviceSafetyPointTests(unittest.TestCase):
    def _broken(self) -> DeviceRecoveryPolicy:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        return policy

    def test_active_turn_defers_recovery(self) -> None:
        action = self._broken().decide(
            turn_active=True, service_active=False, awaiting_confirmation=False
        )
        self.assertEqual(action.action, ACTION_NOTIFY_USER)
        self.assertEqual(action.reason, REASON_DEFERRED_ACTIVE_TURN)
        self.assertFalse(action.safety_point_reached)

    def test_service_active_defers_recovery(self) -> None:
        action = self._broken().decide(
            turn_active=False, service_active=True, awaiting_confirmation=False
        )
        self.assertEqual(action.action, ACTION_NOTIFY_USER)

    def test_awaiting_confirmation_defers_recovery(self) -> None:
        action = self._broken().decide(
            turn_active=False, service_active=False, awaiting_confirmation=True
        )
        self.assertEqual(action.reason, REASON_DEFERRED_AWAITING_CONFIRMATION)
        self.assertFalse(action.safety_point_reached)

    def test_text_mode_is_always_kept(self) -> None:
        for turn, service, confirm in (
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (False, False, True),
        ):
            action = self._broken().decide(
                turn_active=turn, service_active=service, awaiting_confirmation=confirm
            )
            self.assertTrue(action.text_mode_available)
            self.assertIn(REASON_TEXT_MODE_KEPT, action.messages)

    def test_flags_must_be_boolean(self) -> None:
        policy = self._broken()
        with self.assertRaises(ContractError):
            policy.decide(turn_active="yes", service_active=False, awaiting_confirmation=False)  # type: ignore[arg-type]


class DeviceAttemptTests(unittest.TestCase):
    def test_successful_attempt_restores_ready_and_bumps_epoch(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        action = idle(policy)
        self.assertEqual(action.action, ACTION_RECONNECT_OUTPUT)
        before = policy.epoch(DeviceKind.OUTPUT)
        health = policy.note_attempt(DeviceKind.OUTPUT, succeeded=True)
        self.assertIs(health, DeviceHealth.READY)
        self.assertEqual(policy.epoch(DeviceKind.OUTPUT), before + 1)
        self.assertEqual(policy.attempts, 1)
        self.assertTrue(idle(policy).already_ready)

    def test_failed_attempt_keeps_reported_state(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        idle(policy)
        policy.note_attempt(DeviceKind.OUTPUT, succeeded=False)
        self.assertIs(policy.health(DeviceKind.OUTPUT), DeviceHealth.DISCONNECTED)
        action = idle(policy)
        self.assertEqual(action.action, ACTION_RECONNECT_OUTPUT)

    def test_failure_is_reported_in_messages(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        idle(policy)
        policy.note_attempt(DeviceKind.OUTPUT, succeeded=False)
        self.assertIn(REASON_RECONNECT_FAILED, idle(policy).messages)

    def test_attempt_flag_must_be_boolean(self) -> None:
        policy = ready_policy()
        with self.assertRaises(ContractError):
            policy.note_attempt(DeviceKind.OUTPUT, succeeded="yes")  # type: ignore[arg-type]


class DeviceBehaviourDetailsTests(unittest.TestCase):
    def test_reconnect_attempt_reports_the_safe_point_and_request(self) -> None:
        policy = ready_policy()
        policy.register(input_obs(DeviceHealth.DISCONNECTED, "mic-1", 5))
        action = idle(policy)
        details = action.to_details()
        self.assertTrue(details["safety_point_reached"])
        self.assertEqual(details["open_request"]["preferred_device_id"], "mic-1")
        self.assertEqual(details["open_request"]["kind"], "input")
        self.assertEqual(details["device_epoch"], 5)

    def test_messages_are_fixed_short_codes(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        action = idle(policy)
        for message in action.messages:
            self.assertIsInstance(message, str)
            self.assertNotIn(" ", message)
            self.assertEqual(message, message.lower())

    def test_repeated_decide_does_not_repeat_messages(self) -> None:
        policy = ready_policy()
        policy.register(output_obs(DeviceHealth.DISCONNECTED, "spk-1", 2))
        first = idle(policy)
        second = idle(policy)
        self.assertIn(REASON_OUTPUT_LOST, first.messages)
        self.assertNotIn(REASON_OUTPUT_LOST, second.messages)

    def test_details_have_no_authorization_surface(self) -> None:
        policy = ready_policy()
        policy.register(input_obs(DeviceHealth.DISCONNECTED, "mic-1", 2))
        details = idle(policy).to_details()
        for name in ("authorization", "confirmation_window_id", "plan", "tool", "execute"):
            self.assertNotIn(name, details)


class DeviceModuleBoundaryTests(unittest.TestCase):
    def _module(self) -> Path:
        return Path(__file__).resolve().parents[2] / "app" / "devices" / "session.py"

    def test_no_device_file_or_network_imports(self) -> None:
        tree = ast.parse(self._module().read_text(encoding="utf-8"))
        banned = {
            "socket",
            "subprocess",
            "threading",
            "ctypes",
            "os",
            "shutil",
            "pathlib",
            "wave",
            "sounddevice",
            "pyaudiowpatch",
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
        self.assertFalse(imported & banned)

    def test_never_imports_the_safety_chain(self) -> None:
        tree = ast.parse(self._module().read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools", "app.recovery"}
        offenders = {module for module in modules if ".".join(module.split(".")[:2]) in banned}
        self.assertEqual(offenders, set())

    def test_package_exports_the_public_surface(self) -> None:
        import app.devices as devices

        for name in (
            "DeviceRecoveryPolicy",
            "DeviceAction",
            "ACTION_RECONNECT_OUTPUT",
            "REASON_DEFERRED_AWAITING_CONFIRMATION",
        ):
            self.assertIn(name, devices.__all__)


if __name__ == "__main__":
    unittest.main()
