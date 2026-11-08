from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.plans import build_plan_action, build_reviewable_plan
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.safety.confirmation import AuthorizationConsumptionStore, ConfirmationError, L2ConfirmationController
from app.tools import build_mvp_tool_registry


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
CREATED_AT = "2026-09-20T08:00:00Z"
EXPIRES_AT = "2026-09-20T08:05:00Z"


def snapshot(path: str = r"G:\study\project\old.txt") -> PathSnapshot:
    return PathSnapshot(
        requested_path=path,
        purpose="recycle_target",
        normalized_path=path,
        resolved_path=path,
        exists=True,
        target_identity=(1, 5),
        anchor_path=path,
        anchor_identity=(1, 5),
        component_identities=(("G:\\", (1, 1)), (r"G:\study", (1, 2)), (path, (1, 5))),
    )


class L2ConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.engine = PolicyEngine(self.registry, path_revalidator=lambda value: value)
        self.store = AuthorizationConsumptionStore(instance_id="instance-001")
        self.controller = L2ConfirmationController(instance_id="instance-001", authorization_store=self.store)

    def make_action(self, tool_name: str = "start_whitelisted_application", *, action_id: str = "action-1", order: int = 1):
        manifest = self.registry.require(tool_name)
        if tool_name == "start_whitelisted_application":
            arguments = {"application_id": "approved.editor"}
            snapshots = ()
            preview = {"application_id": "approved.editor", "application_name": "Approved Editor"}
        elif tool_name == "get_processes":
            arguments = {"include_details": False}
            snapshots = ()
            preview = {"include_details": False}
        elif tool_name == "move_to_recycle_bin":
            value = snapshot()
            arguments = {"paths": [value.normalized_path], "recursive": False}
            snapshots = (value,)
            preview = {"paths": [value.normalized_path], "item_count": 1, "permanent_delete": False}
        else:
            raise AssertionError(tool_name)
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=snapshots,
        )
        decision = self.engine.evaluate(policy_input)
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=decision,
            preview=preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_plan(self, *, actions=None, plan_version: int = 1, goal: str = "启动已登记编辑器"):
        return build_reviewable_plan(
            plan_id="plan-001",
            plan_version=plan_version,
            task_id="task-001",
            created_at=CREATED_AT,
            goal=goal,
            scope=(r"G:\study\project",),
            exclusions=("不传递命令行参数",),
            file_rules=(),
            recursive=False,
            conflict_policy="deny",
            actions=tuple(actions or (self.make_action(),)),
        )

    def begin(self, *, plan=None, window_id: str = "window-001", session_id: str = "session-001", authorization_id: str | None = None):
        return self.controller.begin(
            plan=plan or self.make_plan(),
            session_id=session_id,
            operation_id="operation-001",
            authorization_id=authorization_id or "auth-" + window_id.removeprefix("window-"),
            confirmation_window_id=window_id,
            target_state_hash=HASH_A,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )

    def test_begin_builds_immutable_modal_prompt_with_fixed_labels_and_complete_l2_summary(self) -> None:
        window = self.begin()
        self.assertEqual(window.status, "pending")
        self.assertTrue(window.prompt.modal)
        self.assertEqual((window.prompt.allow_label, window.prompt.deny_label), ("允许执行", "拒绝"))
        self.assertEqual((window.prompt.plan_id, window.prompt.plan_version, window.prompt.plan_hash), (window.plan_id, 1, window.plan_hash))
        self.assertEqual(window.prompt.session_id, "session-001")
        self.assertEqual(window.prompt.target_state_hash, HASH_A)
        self.assertEqual(len(window.prompt.actions), 1)
        self.assertEqual(window.prompt.actions[0].tool_name, "start_whitelisted_application")
        self.assertEqual(window.prompt.actions[0].risk_level, "L2")
        with self.assertRaises(FrozenInstanceError):
            window.status = "authorized"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            window.prompt.actions[0].preview["application_id"] = "attacker"  # type: ignore[index]
        self.assertFalse(hasattr(window, "execute"))
        self.assertFalse(hasattr(window, "consume"))

    def test_explicit_allow_creates_exact_unconsumed_l2_authorization_snapshot(self) -> None:
        plan = self.make_plan()
        window = self.begin(plan=plan)
        resolution = self.controller.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id=window.session_id,
            event="allow",
            occurred_at="2026-09-20T08:01:00Z",
            current_plan=plan,
            current_target_state_hash=HASH_A,
        )
        snapshot_value = resolution.authorization
        self.assertEqual((resolution.status, resolution.reason_code), ("authorized", "user_allowed"))
        self.assertIsNotNone(snapshot_value)
        assert snapshot_value is not None
        self.assertEqual(snapshot_value.authorization_id, "auth-001")
        self.assertEqual(snapshot_value.session_id, "session-001")
        self.assertEqual((snapshot_value.plan_id, snapshot_value.plan_version, snapshot_value.plan_hash), (plan.plan_id, plan.plan_version, plan.plan_hash))
        self.assertEqual(snapshot_value.target_state_hash, HASH_A)
        self.assertEqual(snapshot_value.allowed_action_ids, ("action-1",))
        self.assertEqual((snapshot_value.risk_level, snapshot_value.authorization_method), ("L2", "modal"))
        self.assertEqual(snapshot_value.confirmation_window_id, "window-001")
        self.assertIsNone(snapshot_value.consumed_at)
        self.assertIsNone(self.controller.active_window)
        self.assertFalse(hasattr(resolution, "execute"))

    def test_l2_authorization_is_registered_for_single_use_consumption(self) -> None:
        plan = self.make_plan()
        window = self.begin(plan=plan)
        result = self.controller.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id=window.session_id,
            event="allow",
            occurred_at="2026-09-20T08:01:00Z",
            current_plan=plan,
            current_target_state_hash=HASH_A,
        )
        authorization = result.authorization
        self.assertIsNotNone(authorization)
        consumed = self.store.consume(
            authorization,
            issuer_instance_id="instance-001",
            session_id="session-001",
            operation_id="operation-001",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=HASH_A,
            action_ids=("action-1",),
            consumed_at="2026-09-20T08:02:00Z",
        )
        self.assertEqual(consumed.consumed_at, "2026-09-20T08:02:00Z")

    def test_deny_close_escape_and_stop_never_create_authorization(self) -> None:
        expected = {
            "deny": ("denied", "user_denied"),
            "close": ("cancelled", "window_closed"),
            "escape": ("cancelled", "escape_pressed"),
            "stop": ("cancelled", "user_stopped"),
        }
        for index, (event, outcome) in enumerate(expected.items(), start=1):
            with self.subTest(event=event):
                window = self.begin(window_id=f"window-{index}")
                result = self.controller.resolve(
                    confirmation_window_id=window.confirmation_window_id,
                    session_id=window.session_id,
                    event=event,
                    occurred_at="2026-09-20T08:01:00Z",
                )
                self.assertEqual((result.status, result.reason_code), outcome)
                self.assertIsNone(result.authorization)
                self.assertIsNone(self.controller.active_window)

    def test_expired_window_defaults_to_no_authorization_even_when_allow_arrives(self) -> None:
        window = self.begin()
        result = self.controller.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id=window.session_id,
            event="allow",
            occurred_at=EXPIRES_AT,
            current_plan=self.make_plan(),
            current_target_state_hash=HASH_A,
        )
        self.assertEqual((result.status, result.reason_code), ("expired", "confirmation_expired"))
        self.assertIsNone(result.authorization)
        self.assertIsNone(self.controller.active_window)

    def test_natural_language_or_unknown_events_do_not_authorize_or_close_window(self) -> None:
        window = self.begin()
        for event in ("可以", "好", "confirm", "authorize", ""):
            with self.subTest(event=event), self.assertRaises(ConfirmationError) as caught:
                self.controller.resolve(
                    confirmation_window_id=window.confirmation_window_id,
                    session_id=window.session_id,
                    event=event,
                    occurred_at="2026-09-20T08:01:00Z",
                )
            self.assertEqual(caught.exception.code, "invalid_event")
            self.assertEqual(self.controller.active_window, window)

    def test_plan_or_target_state_drift_invalidates_window_without_authorization(self) -> None:
        original = self.make_plan()
        changed = self.make_plan(plan_version=2, goal="改变后的计划")
        window = self.begin(plan=original)
        result = self.controller.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id=window.session_id,
            event="allow",
            occurred_at="2026-09-20T08:01:00Z",
            current_plan=changed,
            current_target_state_hash=HASH_A,
        )
        self.assertEqual((result.status, result.reason_code), ("invalidated", "plan_changed"))
        self.assertIsNone(result.authorization)

        window = self.begin(plan=original, window_id="window-002")
        result = self.controller.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id=window.session_id,
            event="allow",
            occurred_at="2026-09-20T08:01:00Z",
            current_plan=original,
            current_target_state_hash=HASH_B,
        )
        self.assertEqual((result.status, result.reason_code), ("invalidated", "target_state_changed"))
        self.assertIsNone(result.authorization)

    def test_only_l2_confirmation_plans_can_open_l2_modal(self) -> None:
        cases = {
            "L1": self.make_plan(actions=(self.make_action("get_processes"),)),
            "L3": self.make_plan(actions=(self.make_action("move_to_recycle_bin"),)),
            "mixed": self.make_plan(actions=(self.make_action(action_id="action-1"), self.make_action("move_to_recycle_bin", action_id="action-2", order=2))),
        }
        for name, plan in cases.items():
            with self.subTest(name=name), self.assertRaises(ConfirmationError) as caught:
                self.begin(plan=plan)
            self.assertEqual(caught.exception.code, "l2_plan_required")
            self.assertIsNone(self.controller.active_window)

    def test_single_active_window_and_stale_window_events_are_rejected(self) -> None:
        first = self.begin(window_id="window-001")
        with self.assertRaises(ConfirmationError) as caught:
            self.begin(window_id="window-002")
        self.assertEqual(caught.exception.code, "confirmation_already_active")
        self.assertEqual(self.controller.active_window, first)

        self.controller.resolve(
            confirmation_window_id=first.confirmation_window_id,
            session_id=first.session_id,
            event="deny",
            occurred_at="2026-09-20T08:01:00Z",
        )
        second = self.begin(window_id="window-002")
        with self.assertRaises(ConfirmationError) as caught:
            self.controller.resolve(
                confirmation_window_id="window-001",
                session_id=second.session_id,
                event="allow",
                occurred_at="2026-09-20T08:02:00Z",
                current_plan=self.make_plan(),
                current_target_state_hash=HASH_A,
            )
        self.assertEqual(caught.exception.code, "confirmation_window_mismatch")
        self.assertEqual(self.controller.active_window, second)

    def test_finished_window_and_authorization_ids_cannot_be_reused(self) -> None:
        first = self.begin(window_id="window-001")
        self.controller.resolve(
            confirmation_window_id=first.confirmation_window_id,
            session_id=first.session_id,
            event="deny",
            occurred_at="2026-09-20T08:01:00Z",
        )
        with self.assertRaises(ConfirmationError) as caught:
            self.begin(window_id="window-001")
        self.assertEqual(caught.exception.code, "confirmation_window_reused")

        with self.assertRaises(ConfirmationError) as caught:
            self.controller.begin(
                plan=self.make_plan(),
                session_id="session-001",
                operation_id="operation-001",
                authorization_id="auth-001",
                confirmation_window_id="window-002",
                target_state_hash=HASH_A,
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
            )
        self.assertEqual(caught.exception.code, "authorization_id_reused")
        self.assertIsNone(self.controller.active_window)
    def test_wrong_session_and_missing_current_facts_cannot_authorize(self) -> None:
        window = self.begin()
        with self.assertRaises(ConfirmationError) as caught:
            self.controller.resolve(
                confirmation_window_id=window.confirmation_window_id,
                session_id="session-other",
                event="allow",
                occurred_at="2026-09-20T08:01:00Z",
                current_plan=self.make_plan(),
                current_target_state_hash=HASH_A,
            )
        self.assertEqual(caught.exception.code, "session_mismatch")
        self.assertEqual(self.controller.active_window, window)

        with self.assertRaises(ConfirmationError) as caught:
            self.controller.resolve(
                confirmation_window_id=window.confirmation_window_id,
                session_id=window.session_id,
                event="allow",
                occurred_at="2026-09-20T08:01:00Z",
            )
        self.assertEqual(caught.exception.code, "current_facts_required")
        self.assertEqual(self.controller.active_window, window)

    def test_invalid_time_range_and_early_timeout_are_rejected_without_state_change(self) -> None:
        with self.assertRaises(ConfirmationError) as caught:
            self.controller.begin(
                plan=self.make_plan(),
                session_id="session-001",
                operation_id="operation-001",
                authorization_id="auth-001",
                confirmation_window_id="window-001",
                target_state_hash=HASH_A,
                created_at=EXPIRES_AT,
                expires_at=CREATED_AT,
            )
        self.assertEqual(caught.exception.code, "invalid_time_range")

        window = self.begin()
        with self.assertRaises(ConfirmationError) as caught:
            self.controller.resolve(
                confirmation_window_id=window.confirmation_window_id,
                session_id=window.session_id,
                event="timeout",
                occurred_at="2026-09-20T08:01:00Z",
            )
        self.assertEqual(caught.exception.code, "timeout_not_reached")
        self.assertEqual(self.controller.active_window, window)


if __name__ == "__main__":
    unittest.main()