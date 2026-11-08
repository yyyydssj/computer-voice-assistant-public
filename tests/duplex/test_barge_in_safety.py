"""P10 R4：打断安全门（``app/duplex/barge_in_safety.py``）的离线契约测试。

核心不变量：**插话永不授权**；安全确认提示被打断必须完整重播且不能建立可用轮次。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.contracts.base import ContractError
from app.duplex.barge_in_safety import (
    ACTION_BLOCK,
    ACTION_HANDOFF,
    ACTION_STOP_AND_REPLAY,
    PROMPT_KIND_CHAT,
    PROMPT_KIND_SAFETY,
    REASON_AWAITING_CONFIRMATION,
    REASON_CHANNEL_UNAVAILABLE,
    REASON_HALF_DUPLEX,
    REASON_NORMAL_REPLY,
    REASON_PLAYBACK_INACTIVE,
    REASON_SAFETY_PROMPT,
    BargeInSafetyDecision,
    BargeInSafetyGate,
)

MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "duplex" / "barge_in_safety.py"


class NormalReplyTests(unittest.TestCase):
    def test_normal_reply_handoff_is_allowed_and_never_authorizing(self) -> None:
        decision = BargeInSafetyGate().decide()
        self.assertEqual(decision.action, ACTION_HANDOFF)
        self.assertEqual(decision.reason, REASON_NORMAL_REPLY)
        self.assertTrue(decision.stop_playback)
        self.assertTrue(decision.allow_new_turn)
        self.assertTrue(decision.handoff_pre_roll)
        self.assertFalse(decision.prompt_replay_required)
        self.assertFalse(decision.authorization_possible)
        self.assertFalse(decision.blocked)

    def test_details_expose_only_codes_and_booleans(self) -> None:
        details = BargeInSafetyGate().decide().to_details()
        self.assertEqual(
            set(details),
            {
                "action",
                "reason",
                "stop_playback",
                "allow_new_turn",
                "handoff_pre_roll",
                "prompt_replay_required",
                "authorization_possible",
            },
        )
        self.assertIs(details["authorization_possible"], False)


class SafetyPromptTests(unittest.TestCase):
    def test_safety_prompt_barge_in_invalidates_and_requires_full_replay(self) -> None:
        decision = BargeInSafetyGate().decide(prompt_kind=PROMPT_KIND_SAFETY)
        self.assertEqual(decision.action, ACTION_STOP_AND_REPLAY)
        self.assertEqual(decision.reason, REASON_SAFETY_PROMPT)
        self.assertTrue(decision.stop_playback)
        self.assertTrue(decision.prompt_replay_required)
        self.assertFalse(decision.allow_new_turn)
        self.assertFalse(decision.handoff_pre_roll)
        self.assertFalse(decision.authorization_possible)

    def test_awaiting_confirmation_is_treated_as_a_safety_prompt(self) -> None:
        decision = BargeInSafetyGate().decide(awaiting_confirmation=True)
        self.assertEqual(decision.action, ACTION_STOP_AND_REPLAY)
        self.assertEqual(decision.reason, REASON_AWAITING_CONFIRMATION)
        self.assertFalse(decision.allow_new_turn)

    def test_safety_branch_wins_over_the_prompt_kind_label(self) -> None:
        decision = BargeInSafetyGate().decide(
            prompt_kind=PROMPT_KIND_CHAT, awaiting_confirmation=True
        )
        self.assertEqual(decision.reason, REASON_AWAITING_CONFIRMATION)


class BlockedTests(unittest.TestCase):
    def test_half_duplex_never_stops_playback(self) -> None:
        decision = BargeInSafetyGate().decide(realtime_available=False)
        self.assertEqual(decision.action, ACTION_BLOCK)
        self.assertEqual(decision.reason, REASON_HALF_DUPLEX)
        self.assertFalse(decision.stop_playback)
        self.assertTrue(decision.blocked)

    def test_unavailable_channel_never_stops_playback(self) -> None:
        decision = BargeInSafetyGate().decide(channel_available=False)
        self.assertEqual(decision.reason, REASON_CHANNEL_UNAVAILABLE)
        self.assertFalse(decision.stop_playback)

    def test_no_playback_never_stops_anything(self) -> None:
        decision = BargeInSafetyGate().decide(playback_active=False)
        self.assertEqual(decision.reason, REASON_PLAYBACK_INACTIVE)
        self.assertFalse(decision.allow_new_turn)

    def test_capability_is_checked_before_the_playback_window(self) -> None:
        # 能力不足时即使"正在播报"也不允许打断。
        decision = BargeInSafetyGate().decide(
            playback_active=True, channel_available=False, realtime_available=False
        )
        self.assertEqual(decision.reason, REASON_HALF_DUPLEX)


class ValidationTests(unittest.TestCase):
    def test_unknown_prompt_kind_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInSafetyGate().decide(prompt_kind="system_prompt")

    def test_non_boolean_flags_are_rejected(self) -> None:
        for kwargs in (
            {"playback_active": 1},
            {"channel_available": None},
            {"realtime_available": "yes"},
            {"awaiting_confirmation": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    BargeInSafetyGate().decide(**kwargs)  # type: ignore[arg-type]

    def test_decision_cannot_claim_authorization(self) -> None:
        with self.assertRaises(ContractError):
            BargeInSafetyDecision(
                action=ACTION_HANDOFF,
                reason=REASON_NORMAL_REPLY,
                stop_playback=True,
                allow_new_turn=True,
                handoff_pre_roll=True,
                prompt_replay_required=False,
                authorization_possible=True,
            )

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInSafetyDecision(
                action="authorize",
                reason=REASON_NORMAL_REPLY,
                stop_playback=True,
                allow_new_turn=True,
                handoff_pre_roll=True,
                prompt_replay_required=False,
            )

    def test_non_boolean_fields_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInSafetyDecision(
                action=ACTION_BLOCK,
                reason=REASON_HALF_DUPLEX,
                stop_playback="no",  # type: ignore[arg-type]
                allow_new_turn=False,
                handoff_pre_roll=False,
                prompt_replay_required=False,
            )


class BoundaryTests(unittest.TestCase):
    def _modules(self) -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        return modules

    def test_never_imports_the_safety_chain(self) -> None:
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
        offenders = {
            name for name in self._modules() if ".".join(name.split(".")[:2]) in banned
        }
        self.assertEqual(offenders, set())

    def test_source_never_sets_authorization_possible_true(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("authorization_possible=True", source)

    def test_no_audio_or_device_surface_is_imported(self) -> None:
        banned = {"sounddevice", "sherpa_onnx", "subprocess", "socket", "app.audio"}
        offenders = {
            name for name in self._modules() if name.split(".")[0] in banned or name in banned
        }
        self.assertEqual(offenders, set())


if __name__ == "__main__":
    unittest.main()
