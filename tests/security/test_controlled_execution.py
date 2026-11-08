from __future__ import annotations

import unittest
from dataclasses import replace

from app.contracts import AuthorizationSnapshot
from app.executor import (
    BackupRecord,
    ControlledExecutionError,
    ControlledExecutionRunner,
    ExecutionBackendError,
    ExecutionLedgerRegistry,
    VerificationDecision,
)
from app.plans import build_plan_action, build_reviewable_plan
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.safety.preflight import ExecutionPreflightApproval, PreflightActionBoundary
from app.tools import build_mvp_tool_registry


HASH_A = "sha256:" + "a" * 64


def snapshot(path: str, purpose: str) -> PathSnapshot:
    return PathSnapshot(
        requested_path=path,
        purpose=purpose,
        normalized_path=path,
        resolved_path=path,
        exists=True,
        target_identity=(1, 5),
        anchor_path=path,
        anchor_identity=(1, 5),
        component_identities=((path, (1, 5)),),
    )


class SequenceClock:
    def __init__(self) -> None:
        self.second = 0

    def __call__(self) -> str:
        value = f"2026-09-21T10:00:{self.second:02d}Z"
        self.second += 1
        return value


class FakeBackend:
    def __init__(self, *, fail_backup: set[str] | None = None, fail_execute: dict[str, bool] | None = None) -> None:
        self.fail_backup = fail_backup or set()
        self.fail_execute = fail_execute or {}
        self.events: list[str] = []
        self.calls: dict[str, int] = {}

    def prepare_backup(self, *, action, execution_id: str, attempt_id: str, created_at: str):
        self.events.append(f"backup:{action.action_id}")
        if action.action_id in self.fail_backup:
            raise ExecutionBackendError("backup_failed", "备份失败", side_effect_may_have_occurred=False)
        if action.tool_name not in {"write_text_file", "move_file", "rename_file", "move_to_recycle_bin"}:
            return None
        return BackupRecord(
            backup_id=f"backup-{action.action_id}",
            action_id=action.action_id,
            attempt_id=attempt_id,
            created_at=created_at,
            source_paths=(r"G:\study\project\source.txt",),
            backup_paths=(r"G:\study\backups\source.txt",),
            source_digests=(HASH_A,),
        )

    def execute(self, *, action, backup):
        self.events.append(f"execute:{action.action_id}")
        self.calls[action.action_id] = self.calls.get(action.action_id, 0) + 1
        if action.action_id in self.fail_execute:
            raise ExecutionBackendError(
                "tool_failed",
                "工具执行失败",
                side_effect_may_have_occurred=self.fail_execute[action.action_id],
            )
        return {"action_id": action.action_id, "after_hash": HASH_A, "pid": 1234}


class FakeVerifier:
    def __init__(self, decisions: dict[str, VerificationDecision] | None = None) -> None:
        self.decisions = decisions or {}
        self.calls: list[str] = []

    def verify(self, *, action, attempt_id: str, output, backup, verified_at: str) -> VerificationDecision:
        self.calls.append(action.action_id)
        return self.decisions.get(
            action.action_id,
            VerificationDecision(status="completed", facts={"observed": True}),
        )


class FakeAudit:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.events: list[str] = []

    def append(self, event_type: str, *, subject_id: str, details=None, created_at: str | None = None):
        self.events.append(event_type)
        if event_type == self.fail_on:
            raise RuntimeError("注入的审计追加失败")
        return None


class ControlledExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.policy = PolicyEngine(self.registry, path_revalidator=lambda value: value)

    def make_action(self, action_id: str, order: int, tool_name: str = "start_whitelisted_application"):
        manifest = self.registry.require(tool_name)
        if tool_name == "write_text_file":
            path = rf"G:\study\project\{action_id}.txt"
            arguments = {
                "path": path,
                "mode": "append",
                "content": "x",
                "expected_source_hash": HASH_A,
            }
            snapshots = (snapshot(path, "write_target"),)
            preview = {
                "path": path,
                "mode": "append",
                "expected_source_hash": HASH_A,
                "content_size_bytes": 1,
                "content_hash": "sha256:" + "2" * 64,
                "whole_file_rewrite": False,
            }
        else:
            arguments = {"application_id": "approved.editor"}
            snapshots = ()
            preview = {"application_id": "approved.editor", "application_name": "Editor"}
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=snapshots,
        )
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=self.policy.evaluate(policy_input),
            preview=preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_plan(self, tools=("write_text_file", "start_whitelisted_application")):
        actions = tuple(self.make_action(f"action-{index}", index, tool) for index, tool in enumerate(tools, 1))
        return build_reviewable_plan(
            plan_id="plan-1",
            plan_version=1,
            task_id="task-1",
            created_at="2026-09-21T09:55:00Z",
            goal="执行冻结动作",
            scope=(r"G:\study\project",),
            exclusions=("不覆盖冲突目标",),
            file_rules=("严格按动作顺序",),
            recursive=False,
            conflict_policy="deny",
            actions=actions,
        )

    def make_approval(self, plan):
        action_ids = tuple(action.action_id for action in plan.actions)
        authorization = AuthorizationSnapshot(
            authorization_id="authorization-1",
            issuer_instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash="sha256:target",
            allowed_action_ids=action_ids,
            risk_level="L2",
            confirmation_window_id="window-1",
            authorization_method="modal",
            confirmation_stage="modal",
            confirmation_phrase=None,
            created_at="2026-09-21T09:58:00Z",
            expires_at="2026-09-21T10:05:00Z",
            consumed_at="2026-09-21T09:59:00Z",
        )
        return ExecutionPreflightApproval(
            instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash="sha256:target",
            action_ids=action_ids,
            checked_at="2026-09-21T09:59:00Z",
            authorization=authorization,
            action_boundaries=tuple(
                PreflightActionBoundary(
                    action_id=action.action_id,
                    tool_name=action.tool_name,
                    manifest_hash=action.manifest_hash,
                    cancellation_mode=self.registry.require(action.tool_name).cancellation,
                )
                for action in plan.actions
            ),
        )

    def run_plan(self, plan, backend, verifier):
        runner = ControlledExecutionRunner(
            registry=self.registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=verifier,
            audit_store=FakeAudit(),
            clock=SequenceClock(),
        )
        return runner.run(
            plan=plan,
            approval=self.make_approval(plan),
            execution_id="execution-1",
            executor_instance_id="executor-1",
            result_id="result-1",
        )

    def test_audit_failure_before_execution_prevents_backend_call(self) -> None:
        plan = self.make_plan(("start_whitelisted_application",))
        backend = FakeBackend()
        runner = ControlledExecutionRunner(
            registry=self.registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=FakeVerifier(),
            audit_store=FakeAudit(fail_on="execution_started"),
            clock=SequenceClock(),
        )
        with self.assertRaises(ControlledExecutionError) as raised:
            runner.run(
                plan=plan,
                approval=self.make_approval(plan),
                execution_id="execution-audit-failure",
                executor_instance_id="executor-1",
                result_id="result-audit-failure",
            )
        self.assertEqual(raised.exception.code, "audit_append_failed")
        self.assertEqual(backend.events, [])

    def test_operation_start_audit_failure_prevents_backend_call(self) -> None:
        plan = self.make_plan(("start_whitelisted_application",))
        backend = FakeBackend()
        runner = ControlledExecutionRunner(
            registry=self.registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=FakeVerifier(),
            audit_store=FakeAudit(fail_on="operation_started"),
            clock=SequenceClock(),
        )
        with self.assertRaises(ControlledExecutionError) as raised:
            runner.run(
                plan=plan,
                approval=self.make_approval(plan),
                execution_id="execution-operation-audit-failure",
                executor_instance_id="executor-1",
                result_id="result-operation-audit-failure",
            )
        self.assertEqual(raised.exception.code, "audit_append_failed")
        self.assertEqual(backend.events, [])


    def test_result_audit_failure_stops_before_later_action(self) -> None:
        plan = self.make_plan(("start_whitelisted_application", "start_whitelisted_application"))
        backend = FakeBackend()
        runner = ControlledExecutionRunner(
            registry=self.registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=FakeVerifier(),
            audit_store=FakeAudit(fail_on="operation_result"),
            clock=SequenceClock(),
        )
        with self.assertRaises(ControlledExecutionError) as raised:
            runner.run(
                plan=plan,
                approval=self.make_approval(plan),
                execution_id="execution-audit-result-failure",
                executor_instance_id="executor-1",
                result_id="result-audit-result-failure",
            )
        self.assertEqual(raised.exception.code, "audit_append_failed")
        self.assertEqual(backend.calls, {"action-1": 1})


    def test_plan_and_preflight_identity_mismatch_is_rejected_before_any_backend_call(self) -> None:
        plan = self.make_plan()
        approval = replace(self.make_approval(plan), plan_hash="sha256:" + "f" * 64)
        backend = FakeBackend()
        runner = ControlledExecutionRunner(
            registry=self.registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=FakeVerifier(),
            audit_store=FakeAudit(),
            clock=SequenceClock(),
        )
        with self.assertRaises(ControlledExecutionError) as raised:
            runner.run(plan=plan, approval=approval, execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1")
        self.assertEqual(raised.exception.code, "preflight_plan_mismatch")
        self.assertEqual(backend.events, [])

    def test_required_backup_is_completed_before_tool_execution(self) -> None:
        plan = self.make_plan(("write_text_file",))
        backend = FakeBackend()
        outcome = self.run_plan(plan, backend, FakeVerifier())
        self.assertEqual(backend.events, ["backup:action-1", "execute:action-1"])
        self.assertEqual(outcome.document.overall_status, "completed")
        self.assertEqual(outcome.snapshot.ledger.status, "COMPLETED")
        self.assertEqual(outcome.document.actions[0].verification_status, "completed")

    def test_backup_failure_prevents_tool_call_and_skips_later_actions(self) -> None:
        plan = self.make_plan(("write_text_file", "start_whitelisted_application"))
        backend = FakeBackend(fail_backup={"action-1"})
        outcome = self.run_plan(plan, backend, FakeVerifier())
        self.assertEqual(backend.events, ["backup:action-1"])
        self.assertEqual([item.outcome for item in outcome.document.actions], ["failed", "skipped"])
        self.assertEqual(outcome.document.overall_status, "failed")
        self.assertEqual(outcome.snapshot.ledger.status, "FAILED")

    def test_success_then_deterministic_failure_is_partial_and_not_retried(self) -> None:
        plan = self.make_plan(("start_whitelisted_application", "start_whitelisted_application", "start_whitelisted_application"))
        backend = FakeBackend(fail_execute={"action-2": False})
        outcome = self.run_plan(plan, backend, FakeVerifier())
        self.assertEqual([item.outcome for item in outcome.document.actions], ["succeeded", "failed", "skipped"])
        self.assertEqual(outcome.document.overall_status, "partially_completed")
        self.assertEqual(outcome.snapshot.ledger.status, "PARTIALLY_COMPLETED")
        self.assertEqual(backend.calls, {"action-1": 1, "action-2": 1})

    def test_possible_side_effect_failure_is_state_uncertain_and_blocks_following_action(self) -> None:
        plan = self.make_plan(("start_whitelisted_application", "start_whitelisted_application"))
        backend = FakeBackend(fail_execute={"action-1": True})
        outcome = self.run_plan(plan, backend, FakeVerifier())
        self.assertEqual([item.outcome for item in outcome.document.actions], ["state_uncertain", "skipped"])
        self.assertEqual(outcome.document.overall_status, "state_uncertain")
        self.assertEqual(outcome.snapshot.ledger.status, "UNCERTAIN")
        self.assertEqual(backend.calls, {"action-1": 1})

    def test_inconclusive_verification_is_state_uncertain_not_failure(self) -> None:
        plan = self.make_plan(("start_whitelisted_application", "start_whitelisted_application"))
        verifier = FakeVerifier({
            "action-1": VerificationDecision(
                status="state_uncertain",
                facts={"pid_observed": False},
                error_code="verification_evidence_missing",
                error_message="无法确认进程真实状态",
            )
        })
        outcome = self.run_plan(plan, FakeBackend(), verifier)
        self.assertEqual([item.outcome for item in outcome.document.actions], ["state_uncertain", "skipped"])
        self.assertEqual(outcome.snapshot.ledger.status, "UNCERTAIN")

    def test_manifest_drift_is_rejected_before_execution(self) -> None:
        plan = self.make_plan(("start_whitelisted_application",))
        approval = self.make_approval(plan)
        changed = replace(approval.action_boundaries[0], manifest_hash="sha256:" + "0" * 64)
        approval = replace(approval, action_boundaries=(changed,))
        backend = FakeBackend()
        runner = ControlledExecutionRunner(
            registry=self.registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=FakeVerifier(),
            audit_store=FakeAudit(),
            clock=SequenceClock(),
        )
        with self.assertRaises(ControlledExecutionError) as raised:
            runner.run(plan=plan, approval=approval, execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1")
        self.assertEqual(raised.exception.code, "execution_boundary_mismatch")
        self.assertEqual(backend.events, [])


if __name__ == "__main__":
    unittest.main()
