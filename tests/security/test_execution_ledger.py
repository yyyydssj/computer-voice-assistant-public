from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

from app.contracts import ActionResult, AuthorizationSnapshot
from app.executor import (
    ExecutionLedgerError,
    ExecutionLedgerRegistry,
    ExecutionLedgerStore,
    LedgerClassification,
)
from app.safety.preflight import ExecutionPreflightApproval


class ExecutionLedgerTests(unittest.TestCase):
    def make_approval(
        self,
        *,
        authorization_id: str = "authorization-1",
        instance_id: str = "instance-1",
        plan_id: str = "plan-1",
        plan_hash: str = "sha256:plan",
    ) -> ExecutionPreflightApproval:
        authorization = AuthorizationSnapshot(
            authorization_id=authorization_id,
            issuer_instance_id=instance_id,
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan_id,
            plan_version=1,
            plan_hash=plan_hash,
            target_state_hash="sha256:target",
            allowed_action_ids=("action-1", "action-2"),
            risk_level="L2",
            confirmation_window_id=f"window-{authorization_id}",
            authorization_method="modal",
            confirmation_stage="modal",
            confirmation_phrase=None,
            created_at="2026-09-20T09:59:00Z",
            expires_at="2026-09-20T10:05:00Z",
            consumed_at="2026-09-20T10:00:00Z",
        )
        return ExecutionPreflightApproval(
            instance_id=instance_id,
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan_id,
            plan_version=1,
            plan_hash=plan_hash,
            target_state_hash="sha256:target",
            action_ids=("action-1", "action-2"),
            checked_at="2026-09-20T10:00:00Z",
            authorization=authorization,
        )

    def make_registry(self) -> ExecutionLedgerRegistry:
        return ExecutionLedgerRegistry(instance_id="instance-1")

    def make_store(self) -> ExecutionLedgerStore:
        return self.make_registry().create(
            execution_id="execution-1",
            approval=self.make_approval(),
            created_at="2026-09-20T10:00:00Z",
        )

    def complete_action(
        self,
        executor: object,
        *,
        action_id: str,
        attempt_id: str,
        second: int,
    ) -> None:
        executor.operation_started(  # type: ignore[attr-defined]
            action_id=action_id,
            attempt_id=attempt_id,
            started_at=f"2026-09-20T10:00:{second:02d}Z",
        )
        executor.operation_returned(  # type: ignore[attr-defined]
            action_id=action_id,
            attempt_id=attempt_id,
            finished_at=f"2026-09-20T10:00:{second + 1:02d}Z",
        )
        executor.record_result(  # type: ignore[attr-defined]
            ActionResult(
                execution_id="execution-1",
                action_id=action_id,
                attempt_id=attempt_id,
                status="completed",
                error_code=None,
                completed_at=f"2026-09-20T10:00:{second + 2:02d}Z",
            )
        )

    def test_creation_binds_consumed_preflight_facts_and_action_set(self) -> None:
        approval = self.make_approval()
        store = self.make_registry().create(
            execution_id="execution-1", approval=approval, created_at="2026-09-20T10:00:00Z"
        )
        snapshot = store.snapshot()

        self.assertEqual(snapshot.ledger.plan_id, approval.plan_id)
        self.assertEqual(snapshot.ledger.plan_version, approval.plan_version)
        self.assertEqual(snapshot.ledger.plan_hash, approval.plan_hash)
        self.assertEqual(snapshot.ledger.authorization_snapshot_hash, approval.authorization.stable_hash())
        self.assertEqual(snapshot.action_ids, approval.action_ids)
        with self.assertRaises(FrozenInstanceError):
            snapshot.action_ids = ()  # type: ignore[misc]

    def test_same_approval_cannot_create_second_execution(self) -> None:
        registry = self.make_registry()
        approval = self.make_approval()
        registry.create(
            execution_id="execution-1", approval=approval, created_at="2026-09-20T10:00:00Z"
        )

        with self.assertRaisesRegex(ExecutionLedgerError, "同一 preflight 授权") as raised:
            registry.create(
                execution_id="execution-2", approval=approval, created_at="2026-09-20T10:00:01Z"
            )
        self.assertEqual(raised.exception.code, "authorization_replayed")

    def test_concurrent_creation_from_same_approval_has_one_winner(self) -> None:
        registry = self.make_registry()
        approval = self.make_approval()

        def create(index: int) -> str:
            try:
                registry.create(
                    execution_id=f"execution-{index}",
                    approval=approval,
                    created_at="2026-09-20T10:00:00Z",
                )
                return "created"
            except ExecutionLedgerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create, range(8)))
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(results.count("authorization_replayed"), 7)

    def test_execution_id_cannot_bind_different_approval(self) -> None:
        registry = self.make_registry()
        registry.create(
            execution_id="execution-1",
            approval=self.make_approval(authorization_id="authorization-1"),
            created_at="2026-09-20T10:00:00Z",
        )

        with self.assertRaises(ExecutionLedgerError) as raised:
            registry.create(
                execution_id="execution-1",
                approval=self.make_approval(
                    authorization_id="authorization-2",
                    plan_id="plan-2",
                    plan_hash="sha256:plan-2",
                ),
                created_at="2026-09-20T10:00:01Z",
            )
        self.assertEqual(raised.exception.code, "execution_id_collision")

    def test_registry_rejects_approval_from_other_instance(self) -> None:
        registry = self.make_registry()
        with self.assertRaises(ExecutionLedgerError) as raised:
            registry.create(
                execution_id="execution-1",
                approval=self.make_approval(instance_id="instance-2"),
                created_at="2026-09-20T10:00:00Z",
            )
        self.assertEqual(raised.exception.code, "approval_wrong_instance")

    def test_ledger_cannot_be_created_before_preflight(self) -> None:
        with self.assertRaisesRegex(ExecutionLedgerError, "不得早于 preflight") as raised:
            self.make_registry().create(
                execution_id="execution-1",
                approval=self.make_approval(),
                created_at="2026-09-20T09:59:59Z",
            )
        self.assertEqual(raised.exception.code, "ledger_before_preflight")

    def test_unapproved_action_cannot_create_operation_started_fact(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        with self.assertRaisesRegex(ExecutionLedgerError, "授权动作集合"):
            executor.operation_started(
                action_id="action-other", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
            )
        self.assertEqual(store.snapshot().attempts, ())

    def test_ledger_binds_operation_to_execution_and_action_identity(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        attempt = executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        self.assertEqual(attempt.execution_id, "execution-1")
        self.assertEqual(attempt.action_id, "action-1")
        self.assertEqual(attempt.executor_instance_id, "executor-1")
        self.assertEqual(attempt.status, "STARTED")
        with self.assertRaisesRegex(ExecutionLedgerError, "execution_id"):
            store.assert_attempt_identity(
                attempt_id="attempt-1", execution_id="execution-other", action_id="action-1"
            )

    def test_attempt_id_is_immutable_and_cannot_rebind(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        with self.assertRaisesRegex(ExecutionLedgerError, "attempt_id.*重绑"):
            executor.operation_started(
                action_id="action-2", attempt_id="attempt-1", started_at="2026-09-20T10:00:02Z"
            )
        self.assertEqual(len(store.snapshot().attempts), 1)

    def test_new_executor_fences_old_instance_writes(self) -> None:
        store = self.make_store()
        old = store.acquire_executor("executor-old")
        new = store.acquire_executor("executor-new")
        with self.assertRaisesRegex(ExecutionLedgerError, "旧 fencing token"):
            old.operation_started(
                action_id="action-1", attempt_id="attempt-old", started_at="2026-09-20T10:00:01Z"
            )
        attempt = new.operation_started(
            action_id="action-1", attempt_id="attempt-new", started_at="2026-09-20T10:00:02Z"
        )
        self.assertEqual(attempt.executor_instance_id, "executor-new")
        self.assertNotEqual(old.fencing_token, new.fencing_token)

    def test_reused_executor_instance_id_cannot_take_over_old_fencing_token(self) -> None:
        store = self.make_store()
        old = store.acquire_executor("executor-1")
        old.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        replacement = store.acquire_executor("executor-1")

        with self.assertRaisesRegex(ExecutionLedgerError, "fencing token"):
            replacement.operation_returned(
                action_id="action-1", attempt_id="attempt-1", finished_at="2026-09-20T10:00:02Z"
            )
        self.assertEqual(store.classify("action-1"), LedgerClassification.STARTED)

    def test_operation_started_is_atomic_on_rejection(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        with self.assertRaises(ExecutionLedgerError):
            executor.operation_started(
                action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:02Z"
            )
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot.attempts), 1)
        self.assertEqual(snapshot.attempts[0].started_at, "2026-09-20T10:00:01Z")

    def test_concurrent_same_attempt_has_one_winner(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")

        def start(_: int) -> str:
            try:
                executor.operation_started(
                    action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
                )
                return "started"
            except ExecutionLedgerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start, range(8)))
        self.assertEqual(results.count("started"), 1)
        self.assertEqual(results.count("attempt_replayed"), 7)

    def test_concurrent_different_attempts_for_same_action_have_one_winner(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")

        def start(index: int) -> str:
            try:
                executor.operation_started(
                    action_id="action-1",
                    attempt_id=f"attempt-{index}",
                    started_at="2026-09-20T10:00:01Z",
                )
                return "started"
            except ExecutionLedgerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(start, range(8)))
        self.assertEqual(results.count("started"), 1)
        self.assertEqual(results.count("action_already_recorded"), 7)
        self.assertEqual(len(store.snapshot().attempts), 1)

    def test_crash_boundary_classification_is_explicit(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        self.assertEqual(store.classify("action-1"), LedgerClassification.NOT_STARTED)
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        self.assertEqual(store.classify("action-1"), LedgerClassification.STARTED)
        executor.operation_returned(
            action_id="action-1", attempt_id="attempt-1", finished_at="2026-09-20T10:00:02Z"
        )
        self.assertEqual(store.classify("action-1"), LedgerClassification.RETURNED)
        executor.record_result(
            ActionResult(
                execution_id="execution-1",
                action_id="action-1",
                attempt_id="attempt-1",
                status="completed",
                error_code=None,
                completed_at="2026-09-20T10:00:03Z",
            )
        )
        self.assertEqual(store.classify("action-1"), LedgerClassification.COMPLETED)

    def test_completed_result_cannot_skip_returned_boundary(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )

        with self.assertRaises(ExecutionLedgerError) as raised:
            executor.record_result(
                ActionResult(
                    execution_id="execution-1",
                    action_id="action-1",
                    attempt_id="attempt-1",
                    status="completed",
                    error_code=None,
                    completed_at="2026-09-20T10:00:02Z",
                )
            )
        self.assertEqual(raised.exception.code, "completion_before_return")
        self.assertEqual(store.classify("action-1"), LedgerClassification.STARTED)

    def test_ledger_completes_only_after_every_authorized_action_completes(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        self.complete_action(executor, action_id="action-1", attempt_id="attempt-1", second=1)
        self.assertEqual(store.snapshot().ledger.status, "RUNNING")

        self.complete_action(executor, action_id="action-2", attempt_id="attempt-2", second=4)
        self.assertEqual(store.snapshot().ledger.status, "COMPLETED")
        with self.assertRaises(ExecutionLedgerError) as raised:
            store.acquire_executor("executor-2")
        self.assertEqual(raised.exception.code, "ledger_terminal")

    def test_return_time_cannot_precede_started_time(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:02Z"
        )
        with self.assertRaisesRegex(ExecutionLedgerError, "finished_at"):
            executor.operation_returned(
                action_id="action-1", attempt_id="attempt-1", finished_at="2026-09-20T10:00:01Z"
            )
        self.assertEqual(store.classify("action-1"), LedgerClassification.STARTED)

    def test_state_uncertain_is_terminal_observation_not_success(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        executor.record_result(
            ActionResult(
                execution_id="execution-1",
                action_id="action-1",
                attempt_id="attempt-1",
                status="state_uncertain",
                error_code="verification_evidence_missing",
                completed_at="2026-09-20T10:00:03Z",
            )
        )
        self.assertEqual(store.classify("action-1"), LedgerClassification.STATE_UNCERTAIN)
        self.assertEqual(store.snapshot().ledger.status, "UNCERTAIN")
        with self.assertRaisesRegex(ExecutionLedgerError, "state_uncertain"):
            executor.operation_started(
                action_id="action-1", attempt_id="attempt-2", started_at="2026-09-20T10:00:04Z"
            )

    def test_snapshot_and_nested_records_are_frozen(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-20T10:00:01Z"
        )
        snapshot = store.snapshot()

        with self.assertRaises(FrozenInstanceError):
            snapshot.ledger.status = "COMPLETED"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.attempts[0].status = "RETURNED"  # type: ignore[misc]
        self.assertEqual(store.classify("action-1"), LedgerClassification.STARTED)

    def test_ledger_does_not_expose_execution_method_or_side_effect_hook(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        for forbidden_name in ("execute", "run_tool", "write_file", "launch_application"):
            self.assertFalse(hasattr(store, forbidden_name))
            self.assertFalse(hasattr(executor, forbidden_name))


if __name__ == "__main__":
    unittest.main()