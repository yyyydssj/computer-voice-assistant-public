from __future__ import annotations

import unittest

from app.contracts import AuthorizationSnapshot, ContractError


class AuthorizationSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = AuthorizationSnapshot(
            authorization_id="auth-1",
            issuer_instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id="plan-1",
            plan_version=3,
            plan_hash="sha256:plan",
            target_state_hash="sha256:target",
            allowed_action_ids=("action-1", "action-2"),
            risk_level="L3",
            confirmation_window_id="window-1",
            authorization_method="modal_and_phrase",
            confirmation_stage="phrase",
            confirmation_phrase="确认执行",
            created_at="2026-09-19T10:00:00Z",
            expires_at="2026-09-19T10:05:00Z",
        )

    def test_authorization_is_valid_only_for_exact_frozen_facts(self) -> None:
        self.assertTrue(self.snapshot.is_valid_for(issuer_instance_id="instance-1", session_id="session-1", operation_id="operation-1", plan_version=3, plan_hash="sha256:plan", target_state_hash="sha256:target", action_ids=("action-1", "action-2"), now="2026-09-19T10:04:59Z"))

    def test_changed_plan_target_actions_or_expiry_invalidates_authorization(self) -> None:
        cases = [
            {"session_id": "session-other"},
            {"plan_version": 4},
            {"plan_hash": "sha256:changed"},
            {"target_state_hash": "sha256:changed"},
            {"action_ids": ("action-1",)},
            {"now": "2026-09-19T10:05:00Z"},
        ]
        base = dict(issuer_instance_id="instance-1", session_id="session-1", operation_id="operation-1", plan_version=3, plan_hash="sha256:plan", target_state_hash="sha256:target", action_ids=("action-1", "action-2"), now="2026-09-19T10:04:00Z")
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(self.snapshot.is_valid_for(**{**base, **change}))

    def test_expired_authorization_cannot_be_consumed(self) -> None:
        for consumed_at in ("2026-09-19T10:05:00Z", "2026-09-19T10:05:01Z"):
            with self.subTest(consumed_at=consumed_at):
                with self.assertRaisesRegex(ContractError, "过期"):
                    self.snapshot.consume(consumed_at)

    def test_deserialized_snapshot_cannot_claim_consumption_after_expiry(self) -> None:
        payload = self.snapshot.to_dict()
        payload["consumed_at"] = "2026-09-19T10:05:00Z"
        with self.assertRaisesRegex(ContractError, "过期"):
            AuthorizationSnapshot.from_dict(payload)

    def test_authorization_cannot_be_reused_after_consumption(self) -> None:
        consumed = self.snapshot.consume("2026-09-19T10:01:00Z")
        self.assertFalse(consumed.is_valid_for(issuer_instance_id="instance-1", session_id="session-1", operation_id="operation-1", plan_version=3, plan_hash="sha256:plan", target_state_hash="sha256:target", action_ids=("action-1", "action-2"), now="2026-09-19T10:02:00Z"))
        with self.assertRaisesRegex(ValueError, "已经使用"):
            consumed.consume("2026-09-19T10:03:00Z")

    def test_confirmation_method_stage_and_phrase_must_agree(self) -> None:
        payload = self.snapshot.to_dict()
        for changes in ({"confirmation_phrase": "确认执行吧"}, {"confirmation_stage": "modal"}, {"authorization_method": "modal"}, {"risk_level": "L2"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ContractError):
                    AuthorizationSnapshot.from_dict({**payload, **changes})


if __name__ == "__main__":
    unittest.main()
