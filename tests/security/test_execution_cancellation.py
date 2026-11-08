from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from app.contracts import ActionResult, AuthorizationSnapshot
from app.executor import (
    CancellationDisposition,
    ExecutionLedgerError,
    ExecutionLedgerRegistry,
)
from app.safety.preflight import ExecutionPreflightApproval, PreflightActionBoundary


class ExecutionCancellationTests(unittest.TestCase):
    def make_approval(self) -> ExecutionPreflightApproval:
        authorization = AuthorizationSnapshot(
            authorization_id="authorization-cancel-1",
            issuer_instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id="plan-1",
            plan_version=1,
            plan_hash="sha256:plan",
            target_state_hash="sha256:target",
            allowed_action_ids=("action-1", "action-2"),
            risk_level="L2",
            confirmation_window_id="window-cancel-1",
            authorization_method="modal",
            confirmation_stage="modal",
            confirmation_phrase=None,
            created_at="2026-09-21T09:59:00Z",
            expires_at="2026-09-21T10:05:00Z",
            consumed_at="2026-09-21T10:00:00Z",
        )
        return ExecutionPreflightApproval(
            instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id="plan-1",
            plan_version=1,
            plan_hash="sha256:plan",
            target_state_hash="sha256:target",
            action_ids=("action-1", "action-2"),
            checked_at="2026-09-21T10:00:00Z",
            authorization=authorization,
            action_boundaries=(
                PreflightActionBoundary(
                    action_id="action-1",
                    tool_name="copy_file",
                    manifest_hash="sha256:manifest-1",
                    cancellation_mode="cooperative",
                ),
                PreflightActionBoundary(
                    action_id="action-2",
                    tool_name="write_text_file",
                    manifest_hash="sha256:manifest-2",
                    cancellation_mode="not_supported",
                ),
            ),
        )

    def make_store(self):
        return ExecutionLedgerRegistry(instance_id="instance-1").create(
            execution_id="execution-1",
            approval=self.make_approval(),
            created_at="2026-09-21T10:00:00Z",
        )

    def complete_first_action(self, store):
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-21T10:00:01Z"
        )
        executor.operation_returned(
            action_id="action-1", attempt_id="attempt-1", finished_at="2026-09-21T10:00:02Z"
        )
        executor.record_result(ActionResult(
            execution_id="execution-1",
            action_id="action-1",
            attempt_id="attempt-1",
            status="completed",
            error_code=None,
            completed_at="2026-09-21T10:00:03Z",
        ))
        return executor

    def test_boundaries_are_frozen_from_preflight_and_keep_action_order(self) -> None:
        snapshot = self.make_store().snapshot()
        self.assertEqual(
            tuple((item.action_id, item.tool_name, item.cancellation_mode) for item in snapshot.action_boundaries),
            (
                ("action-1", "copy_file", "cooperative"),
                ("action-2", "write_text_file", "not_supported"),
            ),
        )

    def test_action_cannot_start_out_of_plan_order(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        with self.assertRaises(ExecutionLedgerError) as raised:
            executor.operation_started(
                action_id="action-2", attempt_id="attempt-2", started_at="2026-09-21T10:00:01Z"
            )
        self.assertEqual(raised.exception.code, "action_out_of_order")
        self.assertEqual(store.snapshot().attempts, ())

    def test_concurrent_start_and_stop_have_one_atomic_winner(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")

        def start() -> str:
            try:
                executor.operation_started(
                    action_id="action-1", attempt_id="attempt-1", started_at="2026-09-21T10:00:01Z"
                )
                return "started"
            except ExecutionLedgerError as exc:
                return exc.code

        def stop() -> str:
            try:
                request = store.request_stop(reason="user_stopped", requested_at="2026-09-21T10:00:01Z")
                return request.disposition.value
            except ExecutionLedgerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = {future.result() for future in (pool.submit(start), pool.submit(stop))}

        snapshot = store.snapshot()
        self.assertIsNotNone(snapshot.stop_request)
        if snapshot.attempts:
            self.assertEqual(snapshot.attempts[0].status, "CANCEL_REQUESTED")
            self.assertIn("started", outcomes)
            self.assertIn(CancellationDisposition.COOPERATIVE_REQUESTED.value, outcomes)
        else:
            self.assertEqual(snapshot.ledger.status, "CANCELLED")
            self.assertIn("execution_stopped", outcomes)
            self.assertIn(CancellationDisposition.BLOCKED_BEFORE_START.value, outcomes)

    def test_stop_before_start_blocks_every_action_and_executor_acquisition(self) -> None:
        store = self.make_store()
        request = store.request_stop(reason="user_cancelled", requested_at="2026-09-21T10:00:01Z")
        self.assertEqual(request.disposition, CancellationDisposition.BLOCKED_BEFORE_START)
        self.assertEqual(store.snapshot().ledger.status, "CANCELLED")
        with self.assertRaises(ExecutionLedgerError) as raised:
            store.acquire_executor("executor-1")
        self.assertEqual(raised.exception.code, "execution_stopped")

    def test_safe_cooperative_stop_marks_attempt_and_blocks_following_action(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-21T10:00:01Z"
        )
        request = store.request_stop(reason="user_stopped", requested_at="2026-09-21T10:00:02Z")
        self.assertEqual(request.disposition, CancellationDisposition.COOPERATIVE_REQUESTED)
        self.assertEqual(store.snapshot().attempts[0].status, "CANCEL_REQUESTED")
        with self.assertRaises(ExecutionLedgerError) as raised:
            executor.operation_started(
                action_id="action-2", attempt_id="attempt-2", started_at="2026-09-21T10:00:03Z"
            )
        self.assertEqual(raised.exception.code, "execution_stopped")

    def test_unsafe_interval_defers_cooperative_request_until_safe_point(self) -> None:
        store = self.make_store()
        executor = store.acquire_executor("executor-1")
        executor.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-21T10:00:01Z"
        )
        executor.enter_unsafe_atomic(
            action_id="action-1", attempt_id="attempt-1", entered_at="2026-09-21T10:00:02Z"
        )
        request = store.request_stop(reason="user_stopped", requested_at="2026-09-21T10:00:03Z")
        self.assertEqual(request.disposition, CancellationDisposition.DEFERRED_UNSAFE_ATOMIC)
        self.assertEqual(store.snapshot().attempts[0].status, "STARTED")

        released = executor.leave_unsafe_atomic(
            action_id="action-1", attempt_id="attempt-1", exited_at="2026-09-21T10:00:04Z"
        )
        self.assertEqual(released.status, "CANCEL_REQUESTED")
        self.assertEqual(released.cancellation_requested_at, "2026-09-21T10:00:03Z")
        self.assertEqual(
            store.snapshot().stop_request.disposition,
            CancellationDisposition.COOPERATIVE_REQUESTED,
        )

    def test_not_supported_action_is_never_force_cancelled_and_waits_for_return(self) -> None:
        store = self.make_store()
        executor = self.complete_first_action(store)
        executor.operation_started(
            action_id="action-2", attempt_id="attempt-2", started_at="2026-09-21T10:00:04Z"
        )
        request = store.request_stop(reason="user_cancelled", requested_at="2026-09-21T10:00:05Z")
        self.assertEqual(request.disposition, CancellationDisposition.WAIT_FOR_ATOMIC_RETURN)
        self.assertEqual(store.snapshot().attempts[-1].status, "STARTED")

        returned = executor.operation_returned(
            action_id="action-2", attempt_id="attempt-2", finished_at="2026-09-21T10:00:06Z"
        )
        self.assertEqual(returned.status, "RETURNED")
        self.assertEqual(store.snapshot().ledger.status, "CANCELLED")

    def test_system_abort_finishes_as_aborted_after_current_atomic_action_returns(self) -> None:
        store = self.make_store()
        executor = self.complete_first_action(store)
        executor.operation_started(
            action_id="action-2", attempt_id="attempt-2", started_at="2026-09-21T10:00:04Z"
        )
        store.request_stop(reason="system_aborted", requested_at="2026-09-21T10:00:05Z")
        executor.operation_returned(
            action_id="action-2", attempt_id="attempt-2", finished_at="2026-09-21T10:00:06Z"
        )
        self.assertEqual(store.snapshot().ledger.status, "ABORTED")

    def test_invalid_stop_timestamp_leaves_no_partial_control_fact(self) -> None:
        store = self.make_store()
        with self.assertRaises(ExecutionLedgerError) as raised:
            store.request_stop(reason="user_stopped", requested_at="2026-09-21T09:59:59Z")
        self.assertEqual(raised.exception.code, "invalid_stop_time")
        self.assertIsNone(store.snapshot().stop_request)
        self.assertEqual(store.snapshot().ledger.status, "CREATED")

    def test_stale_executor_cannot_change_unsafe_boundary(self) -> None:
        store = self.make_store()
        stale = store.acquire_executor("executor-1")
        current = store.acquire_executor("executor-2")
        current.operation_started(
            action_id="action-1", attempt_id="attempt-1", started_at="2026-09-21T10:00:01Z"
        )
        with self.assertRaises(ExecutionLedgerError) as raised:
            stale.enter_unsafe_atomic(
                action_id="action-1", attempt_id="attempt-1", entered_at="2026-09-21T10:00:02Z"
            )
        self.assertEqual(raised.exception.code, "stale_fencing_token")


if __name__ == "__main__":
    unittest.main()