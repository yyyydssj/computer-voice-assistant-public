from __future__ import annotations

import unittest
from dataclasses import replace

from app.contracts import (
    ActionAttempt,
    ActionResult,
    AuditEvent,
    ContractError,
    ExecutionLedger,
    assess_execution_records,
    seal_audit_event,
    validate_audit_chain,
)


class ExecutionIntegrityTests(unittest.TestCase):
    def make_ledger(self, *, status: str = "RUNNING") -> ExecutionLedger:
        return ExecutionLedger(
            execution_id="execution-1",
            plan_id="plan-1",
            plan_version=1,
            plan_hash="sha256:plan",
            authorization_snapshot_hash="sha256:auth",
            status=status,
            created_at="2026-09-19T10:00:00Z",
        )

    def make_attempt(self, **changes: object) -> ActionAttempt:
        values: dict[str, object] = {
            "execution_id": "execution-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "executor_instance_id": "instance-1",
            "fencing_token": "fence-2",
            "status": "STARTED",
            "started_at": "2026-09-19T10:00:01Z",
            "finished_at": None,
            "cancellation_requested_at": None,
        }
        values.update(changes)
        return ActionAttempt(**values)

    def test_interrupted_started_attempt_without_result_is_state_uncertain(self) -> None:
        assessment = assess_execution_records(
            self.make_ledger(),
            attempts=(self.make_attempt(status="INTERRUPTED"),),
            results=(),
            current_fencing_token="fence-2",
        )
        self.assertEqual(assessment, "state_uncertain")

    def test_completed_ledger_rejects_missing_completion_record(self) -> None:
        with self.assertRaisesRegex(ContractError, "完成记录缺失"):
            assess_execution_records(
                self.make_ledger(status="COMPLETED"),
                attempts=(self.make_attempt(status="RETURNED", finished_at="2026-09-19T10:00:02Z"),),
                results=(),
                current_fencing_token="fence-2",
            )

    def test_completed_ledger_rejects_uncertain_result(self) -> None:
        with self.assertRaisesRegex(ContractError, "完成状态.*结果冲突"):
            assess_execution_records(
                self.make_ledger(status="COMPLETED"),
                attempts=(self.make_attempt(status="RETURNED", finished_at="2026-09-19T10:00:02Z"),),
                results=(
                    ActionResult(
                        execution_id="execution-1",
                        action_id="action-1",
                        attempt_id="attempt-1",
                        status="state_uncertain",
                        error_code="verification_evidence_missing",
                        completed_at="2026-09-19T10:00:03Z",
                    ),
                ),
                current_fencing_token="fence-2",
            )

    def test_returned_attempt_without_result_is_state_uncertain(self) -> None:
        assessment = assess_execution_records(
            self.make_ledger(),
            attempts=(self.make_attempt(status="RETURNED", finished_at="2026-09-19T10:00:02Z"),),
            results=(),
            current_fencing_token="fence-2",
        )
        self.assertEqual(assessment, "state_uncertain")

    def test_attempt_id_cannot_rebind_to_different_action_identity(self) -> None:
        with self.assertRaisesRegex(ContractError, "attempt_id.*身份冲突"):
            assess_execution_records(
                self.make_ledger(),
                attempts=(self.make_attempt(), self.make_attempt(action_id="action-2")),
                results=(),
                current_fencing_token="fence-2",
            )

    def test_stale_fencing_token_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "旧 fencing token"):
            assess_execution_records(
                self.make_ledger(),
                attempts=(self.make_attempt(fencing_token="fence-1"),),
                results=(),
                current_fencing_token="fence-2",
            )


class AuditChainTests(unittest.TestCase):
    def make_event(self, sequence: int) -> AuditEvent:
        return AuditEvent(
            audit_event_id=f"audit-{sequence}",
            event_type="execution_observed",
            source_instance_id="instance-1",
            event_sequence=sequence,
            created_at=f"2026-09-19T10:00:0{sequence}Z",
            subject_id="execution-1",
        )

    def test_continuous_sealed_audit_chain_is_valid(self) -> None:
        first = seal_audit_event(self.make_event(1), previous_event_hash=None)
        second = seal_audit_event(self.make_event(2), previous_event_hash=first.event_hash)
        self.assertTrue(validate_audit_chain((first, second)))

    def test_hashed_audit_content_tampering_is_rejected(self) -> None:
        sealed = seal_audit_event(self.make_event(1), previous_event_hash=None)
        tampered = replace(sealed, subject_id="execution-2")
        with self.assertRaisesRegex(ContractError, "审计链断裂"):
            validate_audit_chain((tampered,))

    def test_broken_audit_link_is_rejected(self) -> None:
        first = seal_audit_event(self.make_event(1), previous_event_hash=None)
        second = seal_audit_event(self.make_event(2), previous_event_hash=first.event_hash)
        broken = replace(second, previous_event_hash="sha256:broken")
        with self.assertRaisesRegex(ContractError, "审计链断裂"):
            validate_audit_chain((first, broken))


if __name__ == "__main__":
    unittest.main()