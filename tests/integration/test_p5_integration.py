from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.auditing import AuditStore
from app.contracts import AuthorizationSnapshot
from app.executor import (
    ControlledExecutionRunner,
    ExecutionLedgerRegistry,
    VerificationDecision,
)
from app.plans import build_plan_action, build_reviewable_plan
from app.recovery import RecoveryScanner
from app.retry import ObservedState, RetryDisposition, RetryPolicy
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.safety.confirmation import (
    AuthorizationConsumptionError,
    AuthorizationConsumptionStore,
    L2ConfirmationController,
    L3ConfirmationController,
)
from app.safety.preflight import ExecutionPreflightGate
from app.tools import build_mvp_tool_registry


HASH_A = "sha256:" + "a" * 64
CREATED_AT = "2026-09-21T13:00:00Z"
EXPIRES_AT = "2026-09-21T13:05:00Z"
CHECKED_AT = "2026-09-21T13:01:00Z"


def _path_snapshot(path: str, purpose: str, *, exists: bool) -> PathSnapshot:
    parent = r"G:\study\project"
    components = (("G:\\", (1, 1)), (r"G:\study", (1, 2)), (parent, (1, 4)))
    if exists:
        components += ((path, (1, 5)),)
    return PathSnapshot(
        requested_path=path,
        purpose=purpose,
        normalized_path=path,
        resolved_path=path,
        exists=exists,
        target_identity=(1, 5) if exists else None,
        anchor_path=path if exists else parent,
        anchor_identity=(1, 5) if exists else (1, 4),
        component_identities=components,
    )


def _build_plan(*, tool_name: str = "start_whitelisted_application", risk_level: str | None = None):
    registry = build_mvp_tool_registry()
    manifest = registry.require(tool_name)
    if tool_name == "start_whitelisted_application":
        arguments = {"application_id": "approved.editor"}
        snapshots: tuple[PathSnapshot, ...] = ()
        preview = {"application_id": "approved.editor", "application_name": "Approved Editor"}
    elif tool_name == "move_to_recycle_bin":
        target = _path_snapshot(r"G:\study\project\old.txt", "recycle_target", exists=True)
        arguments = {"paths": [target.normalized_path], "recursive": False}
        snapshots = (target,)
        preview = {
            "paths": [target.normalized_path],
            "item_count": 1,
            "recursive": False,
            "permanent_delete": False,
            "reparse_target": "deny",
        }
    else:
        raise AssertionError(f"unsupported integration tool: {tool_name}")
    if risk_level is not None and manifest.risk_level != risk_level:
        raise AssertionError(f"manifest risk mismatch: {manifest.risk_level!r} != {risk_level!r}")
    policy_input = PolicyInput(
        tool_name=manifest.name,
        tool_version=manifest.version,
        manifest_hash=manifest.content_hash,
        risk_level=manifest.risk_level,
        requires_confirmation=manifest.requires_confirmation,
        normalized_arguments=arguments,
        path_snapshots=snapshots,
    )
    decision = PolicyEngine(registry, path_revalidator=lambda value: value).evaluate(policy_input)
    return registry, build_plan_action(
        action_id="action-1",
        policy_input=policy_input,
        policy_decision=decision,
        preview=preview,
        verification_method=manifest.verification_method,
        rollback_strategy=manifest.rollback_strategy,
        order=1,
    )


def _build_reviewable_plan(action, *, plan_id: str = "plan-1"):
    return build_reviewable_plan(
        plan_id=plan_id,
        plan_version=1,
        task_id="task-1",
        created_at=CREATED_AT,
        goal="执行冻结动作",
        scope=(r"G:\study\project",),
        exclusions=("不扩大动作范围",),
        file_rules=("执行前复检",),
        recursive=False,
        conflict_policy="deny",
        actions=(action,),
    )


class _Backend:
    def __init__(self, *, fail: bool = False, uncertain: bool = False) -> None:
        self.fail = fail
        self.uncertain = uncertain
        self.calls = 0

    def prepare_backup(self, *, action, execution_id: str, attempt_id: str, created_at: str):
        return None

    def execute(self, *, action, backup):
        self.calls += 1
        if self.fail:
            from app.executor import ExecutionBackendError

            raise ExecutionBackendError(
                "tool_failed",
                "注入的确定性失败",
                side_effect_may_have_occurred=self.uncertain,
            )
        return {"application_name": "Approved Editor", "pid": 1234}


class IntegrationVerifier:
    def __init__(self, *, status: str = "completed") -> None:
        self.status = status

    def verify(self, *, action, attempt_id: str, output, backup, verified_at: str):
        return VerificationDecision(
            status=self.status,
            facts={"observed": self.status == "completed"},
            error_code=None if self.status == "completed" else "verification_evidence_missing",
            error_message=None if self.status == "completed" else "无法确认真实状态",
        )


class IntegrationAudit:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append(self, event_type: str, *, subject_id: str, details=None, created_at: str | None = None):
        self.events.append(event_type)
        return None


class P5IntegrationTests(unittest.TestCase):
    def test_l2_to_preflight_ledger_execution_verification_and_result_are_one_chain(self) -> None:
        registry, action = _build_plan()
        plan = _build_reviewable_plan(action)
        store = AuthorizationConsumptionStore(instance_id="instance-1")
        confirmation = L2ConfirmationController(instance_id="instance-1", authorization_store=store)
        window = confirmation.begin(
            plan=plan,
            session_id="session-1",
            operation_id="operation-1",
            authorization_id="authorization-1",
            confirmation_window_id="window-1",
            target_state_hash=HASH_A,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        resolution = confirmation.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id="session-1",
            event="allow",
            occurred_at=CHECKED_AT,
            current_plan=plan,
            current_target_state_hash=HASH_A,
        )
        self.assertEqual(resolution.status, "authorized")
        self.assertIsNotNone(resolution.authorization)
        authorization = resolution.authorization
        assert authorization is not None

        gate = ExecutionPreflightGate(
            instance_id="instance-1",
            registry=registry,
            authorization_store=store,
            path_revalidator=lambda value: value,
            target_state_hasher=lambda current_plan: HASH_A,
            content_hash_reader=lambda path: HASH_A,
            application_resolver=lambda application_id: "Approved Editor",
        )
        approval = gate.check(
            authorization=authorization,
            session_id="session-1",
            operation_id="operation-1",
            current_plan=plan,
            checked_at=CHECKED_AT,
        )
        self.assertEqual(approval.action_ids, ("action-1",))
        self.assertEqual(approval.authorization.consumed_at, CHECKED_AT)

        backend = _Backend()
        audit = AuditStore(
            Path(self.enterContext(tempfile.TemporaryDirectory())),
            source_instance_id="instance-1",
        )
        outcome = ControlledExecutionRunner(
            registry=registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=IntegrationVerifier(),
            audit_store=audit,
            clock=lambda: "2026-09-21T13:02:00Z",
        ).run(
            plan=plan,
            approval=approval,
            execution_id="execution-1",
            executor_instance_id="executor-1",
            result_id="result-1",
        )
        self.assertEqual(backend.calls, 1)
        self.assertEqual(outcome.document.overall_status, "completed")
        self.assertEqual(outcome.document.actions[0].verification_status, "completed")
        self.assertEqual(outcome.snapshot.ledger.status, "COMPLETED")
        self.assertFalse(hasattr(outcome.document, "execute"))
        self.assertEqual(
            [event.event_type for event in audit.read_events()],
            ["execution_started", "operation_started", "operation_result", "execution_finished"],
        )
        reported_documents: list[object] = []

        def report_natural_language(document: object) -> None:
            reported_documents.append(document)
            raise RuntimeError("注入的自然语言汇报失败")

        with self.assertRaisesRegex(RuntimeError, "自然语言汇报失败"):
            report_natural_language(outcome.document)
        self.assertEqual(reported_documents, [outcome.document])
        self.assertEqual(backend.calls, 1)
        self.assertEqual(
            RetryPolicy().side_effect(attempt=1, observed_state=ObservedState.COMPLETED).disposition,
            RetryDisposition.NO_RETRY,
        )

    def test_l3_phrase_and_restart_boundary_require_exactly_one_consumption(self) -> None:
        registry, action = _build_plan(tool_name="move_to_recycle_bin", risk_level="L3")
        plan = _build_reviewable_plan(action, plan_id="plan-l3")
        store = AuthorizationConsumptionStore(instance_id="instance-1")
        confirmation = L3ConfirmationController(instance_id="instance-1", authorization_store=store)
        window = confirmation.begin(
            plan=plan,
            session_id="session-1",
            operation_id="operation-1",
            authorization_id="authorization-l3",
            confirmation_window_id="window-l3",
            target_state_hash=HASH_A,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        modal = confirmation.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id="session-1",
            event="allow",
            occurred_at=CHECKED_AT,
            current_plan=plan,
            current_target_state_hash=HASH_A,
        )
        self.assertEqual(modal.status, "awaiting_phrase")
        authorized = confirmation.submit_phrase(
            confirmation_window_id=window.confirmation_window_id,
            session_id="session-1",
            phrase="确认执行",
            occurred_at="2026-09-21T13:01:30Z",
            current_plan=plan,
            current_target_state_hash=HASH_A,
        )
        authorization = authorized.authorization
        self.assertIsNotNone(authorization)
        assert authorization is not None
        consumed = store.consume(
            authorization,
            issuer_instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=HASH_A,
            action_ids=(action.action_id,),
            consumed_at="2026-09-21T13:02:00Z",
        )
        self.assertEqual(consumed.consumed_at, "2026-09-21T13:02:00Z")
        with self.assertRaises(AuthorizationConsumptionError) as replay:
            AuthorizationConsumptionStore(instance_id="instance-1").consume(
                authorization,
                issuer_instance_id="instance-1",
                session_id="session-1",
                operation_id="operation-1",
                plan_id=plan.plan_id,
                plan_version=plan.plan_version,
                plan_hash=plan.plan_hash,
                target_state_hash=HASH_A,
                action_ids=(action.action_id,),
                consumed_at="2026-09-21T13:02:00Z",
            )
        self.assertEqual(replay.exception.code, "authorization_unknown")

    def test_stop_unknown_state_recovery_and_retry_are_fail_closed(self) -> None:
        report = RecoveryScanner().scan(
            execution_id="execution-uncertain",
            plan_id="plan-1",
            plan_version=1,
            interruption_reason="user_stop",
            last_known_at="2026-09-21T13:03:00Z",
            actions=(
                {"action_id": "action-1", "tool_name": "write_text_file"},
                {"action_id": "action-2", "tool_name": "move_to_recycle_bin"},
            ),
            audit_hints={"action-1": "completed", "action-2": "completed"},
            observe=lambda action: (
                ("completed", {"verified": True})
                if action["action_id"] == "action-1"
                else ("state_uncertain", {"verified": False})
            ),
        )
        self.assertEqual([item.status for item in report.actions], ["completed", "state_uncertain"])
        self.assertFalse(report.auto_resume_allowed)
        self.assertIn("不会自动补做", report.render_text())
        policy = RetryPolicy()
        self.assertEqual(
            policy.side_effect(attempt=1, observed_state=ObservedState.UNKNOWN_RESULT).disposition,
            RetryDisposition.SCAN_BEFORE_DECISION,
        )
        self.assertEqual(
            policy.side_effect(attempt=1, observed_state=ObservedState.STATE_UNCERTAIN).disposition,
            RetryDisposition.BLOCKED_STATE_UNCERTAIN,
        )
        self.assertEqual(
            policy.side_effect(attempt=1, observed_state=ObservedState.COMPLETED).disposition,
            RetryDisposition.NO_RETRY,
        )

    def test_deterministic_failure_is_not_reexecuted_and_audit_chain_is_fail_closed(self) -> None:
        registry, action = _build_plan()
        plan = _build_reviewable_plan(action, plan_id="plan-failure")
        authorization = AuthorizationSnapshot(
            authorization_id="authorization-failure",
            issuer_instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=HASH_A,
            allowed_action_ids=(action.action_id,),
            risk_level="L2",
            confirmation_window_id="window-failure",
            authorization_method="modal",
            confirmation_stage="modal",
            confirmation_phrase=None,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
            consumed_at=CHECKED_AT,
        )
        from app.safety.preflight import ExecutionPreflightApproval, PreflightActionBoundary

        approval = ExecutionPreflightApproval(
            instance_id="instance-1",
            session_id="session-1",
            operation_id="operation-1",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=HASH_A,
            action_ids=(action.action_id,),
            checked_at=CHECKED_AT,
            authorization=authorization,
            action_boundaries=(
                PreflightActionBoundary(
                    action_id=action.action_id,
                    tool_name=action.tool_name,
                    manifest_hash=action.manifest_hash,
                    cancellation_mode=registry.require(action.tool_name).cancellation,
                ),
            ),
        )
        backend = _Backend(fail=True, uncertain=False)
        outcome = ControlledExecutionRunner(
            registry=registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
            backend=backend,
            verifier=IntegrationVerifier(),
            audit_store=IntegrationAudit(),
            clock=lambda: "2026-09-21T13:04:00Z",
        ).run(
            plan=plan,
            approval=approval,
            execution_id="execution-failure",
            executor_instance_id="executor-1",
            result_id="result-failure",
        )
        self.assertEqual(backend.calls, 1)
        self.assertEqual(outcome.document.overall_status, "failed")
        self.assertEqual(
            RetryPolicy().side_effect(attempt=1, observed_state=ObservedState.NOT_COMPLETED).disposition,
            RetryDisposition.NO_RETRY,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            first = AuditStore(Path(temp_dir), source_instance_id="instance-1").append(
                "execution_failed",
                subject_id="execution-failure",
                details={"api_key": "secret", "safe": "failed"},
                created_at="2026-09-21T13:04:30Z",
            )
            events = AuditStore(Path(temp_dir), source_instance_id="instance-1").read_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_hash, first.event_hash)
            self.assertNotIn("secret", (Path(temp_dir) / "audit.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
