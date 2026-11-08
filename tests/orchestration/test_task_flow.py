"""P8 批次 4 红测：任务路径编排（计划预览 → L2/L3 → 预检 → 执行 → 验证 → 摘要）。

全部离线：真实 ToolRegistry / PolicyEngine / ActionPlan / L2·L3 控制器 / 复检门 /
受控执行器与注入式 backend·verifier·audit；不联网、不触真实文件系统。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.auditing import AuditStore
from app.contracts.base import ContractError
from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.executor import (
    ControlledExecutionRunner,
    ExecutionBackendError,
    ExecutionLedgerRegistry,
    VerificationDecision,
)
from app.plans import ReviewableActionPlan, build_plan_action, build_reviewable_plan
from app.runtime.orchestration import (
    TaskActionProposal,
    TaskAuthorizationOutcome,
    TaskAuthorizationRequest,
    TaskExecutionOutcome,
    TaskFlowError,
    TaskFlowOrchestrator,
    TaskFlowState,
    TaskPlanPreview,
    TaskPlanPreviewer,
    TaskPlanRequest,
    TaskReport,
    required_authorization_level,
    summarize_plan_for_speech,
    summarize_result_document,
)
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.safety.confirmation import AuthorizationConsumptionStore
from app.safety.preflight import ExecutionPreflightGate
from app.tools import build_mvp_tool_registry, build_write_action_preview

HASH_A = "sha256:" + "a" * 64
CREATED_AT = "2026-09-21T13:00:00Z"
EXPIRES_AT = "2026-09-21T13:05:00Z"
DECIDED_AT = "2026-09-21T13:01:00Z"
CHECKED_AT = "2026-09-21T13:02:00Z"
RECYCLE_PATH = r"G:\study\project\old.txt"
APPLICATION_ID = "approved.editor"
APPLICATION_NAME = "Approved Editor"


def router(status: str = "success") -> RouterStatus:
    return RouterStatus(
        provider="local",
        model_id="chat-only-router",
        model_version="1",
        status=status,
        failure_code=None if status == "success" else "low_confidence",
    )


def handoff(
    *,
    route: str = "task",
    intent: str = "open_application",
    tool_scope: tuple[str, ...] = ("start_whitelisted_application",),
    handling_mode: str = "plan_then_execute",
    required_confirmation: str = "defer_to_safety",
    router_status: str = "success",
    confidence: float = 1.0,
) -> RouteHandoff:
    trace = RouteTrace(
        request_id="asr-request-1",
        conversation_id="chat-session-1",
        turn_id="asr-turn-1",
        source="asr",
        raw_text="帮我打开编辑器",
        created_at=CREATED_AT,
    )
    return RouteHandoff(
        trace=trace,
        route=route,
        intent=intent,
        handling_mode=handling_mode,
        confidence=confidence,
        slots={},
        required_confirmation=required_confirmation,
        tool_scope=tool_scope,
        router=router(router_status),
    )


def path_snapshot(path: str, purpose: str, *, exists: bool = True) -> PathSnapshot:
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


def application_proposal(action_id: str = "action-1", order: int = 1) -> TaskActionProposal:
    return TaskActionProposal(
        action_id=action_id,
        tool_name="start_whitelisted_application",
        arguments={"application_id": APPLICATION_ID},
        order=order,
    )


def recycle_proposal(action_id: str = "action-1", order: int = 1, path: str = RECYCLE_PATH) -> TaskActionProposal:
    snapshot = path_snapshot(path, "recycle_target", exists=True)
    return TaskActionProposal(
        action_id=action_id,
        tool_name="move_to_recycle_bin",
        arguments={"paths": [path], "recursive": False},
        path_snapshots=(snapshot,),
        order=order,
    )


def plan_request(
    *actions: TaskActionProposal,
    handoff_value: RouteHandoff | None = None,
    plan_id: str = "plan-1",
    task_id: str = "task-1",
    goal: str = "打开已批准的应用",
) -> TaskPlanRequest:
    return TaskPlanRequest(
        handoff=handoff_value or handoff(),
        task_id=task_id,
        plan_id=plan_id,
        goal=goal,
        scope=(r"G:\study\project",),
        actions=tuple(actions) or (application_proposal(),),
        created_at=CREATED_AT,
    )


def previewer() -> TaskPlanPreviewer:
    registry = build_mvp_tool_registry()
    return TaskPlanPreviewer(
        registry=registry,
        policy_engine=PolicyEngine(registry, path_revalidator=lambda value: value),
        path_validator=lambda value, purpose: Path(value),
        application_resolver=lambda application_id: APPLICATION_NAME,
    )


def authorization_request(
    preview: TaskPlanPreview,
    *,
    user_decision: str = "allow",
    phrase: str | None = None,
    decision_at: str = DECIDED_AT,
    target_state_hash: str = HASH_A,
    session_id: str = "session-1",
    operation_id: str = "operation-1",
    authorization_id: str = "authorization-1",
    confirmation_window_id: str = "window-1",
    plan_id: str | None = None,
    plan_version: int | None = None,
    plan_hash: str | None = None,
) -> TaskAuthorizationRequest:
    return TaskAuthorizationRequest(
        plan_id=plan_id if plan_id is not None else preview.plan.plan_id,
        plan_version=plan_version if plan_version is not None else preview.plan.plan_version,
        plan_hash=plan_hash if plan_hash is not None else preview.plan.plan_hash,
        session_id=session_id,
        operation_id=operation_id,
        authorization_id=authorization_id,
        confirmation_window_id=confirmation_window_id,
        target_state_hash=target_state_hash,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        decision_at=decision_at,
        user_decision=user_decision,
        confirmation_phrase=phrase,
    )


class FakeBackend:
    def __init__(self, *, fail: bool = False, uncertain: bool = False) -> None:
        self.fail = fail
        self.uncertain = uncertain
        self.calls = 0

    def prepare_backup(self, *, action, execution_id: str, attempt_id: str, created_at: str):
        return None

    def execute(self, *, action, backup):
        self.calls += 1
        if self.fail:
            raise ExecutionBackendError(
                "tool_failed",
                "注入的确定性失败",
                side_effect_may_have_occurred=self.uncertain,
            )
        return {"application_name": APPLICATION_NAME, "pid": 1234}


class FakeVerifier:
    def __init__(self, *, status: str = "completed") -> None:
        self.status = status

    def verify(self, *, action, attempt_id: str, output, backup, verified_at: str) -> VerificationDecision:
        return VerificationDecision(
            status=self.status,
            facts={"observed": self.status == "completed"},
            error_code=None if self.status == "completed" else "verification_evidence_missing",
            error_message=None if self.status == "completed" else "无法确认真实状态",
        )


class FakeAudit:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_on = fail_on

    def append(self, event_type: str, *, subject_id: str, details=None, created_at: str | None = None):
        if self.fail_on == event_type:
            raise RuntimeError("注入的审计不可写")
        self.events.append(event_type)
        return None


class FakeStream:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.chunks: list[str] = []
        self.finished = 0
        self.cancelled = 0
        self.error = error

    def push_text_chunk(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.chunks.append(text)

    def finish(self) -> None:
        self.finished += 1

    def cancel(self) -> None:
        self.cancelled += 1


class FakeSpeaker:
    def __init__(self, stream: object | None = None, *, error: Exception | None = None) -> None:
        self.stream = stream
        self.error = error
        self.starts = 0

    def start_stream(self):
        self.starts += 1
        if self.error is not None:
            raise self.error
        return self.stream


def build_orchestrator(
    *,
    backend: FakeBackend | None = None,
    verifier: FakeVerifier | None = None,
    audit: FakeAudit | None = None,
    speaker: FakeSpeaker | None = None,
    text_sink=None,
    clock_value: str = CHECKED_AT,
):
    registry = build_mvp_tool_registry()
    store = AuthorizationConsumptionStore(instance_id="instance-1")
    gate = ExecutionPreflightGate(
        instance_id="instance-1",
        registry=registry,
        authorization_store=store,
        path_revalidator=lambda value: value,
        target_state_hasher=lambda current_plan: HASH_A,
        content_hash_reader=lambda path: HASH_A,
        application_resolver=lambda application_id: APPLICATION_NAME,
    )
    backend = backend or FakeBackend()
    audit = audit or FakeAudit()
    runner = ControlledExecutionRunner(
        registry=registry,
        ledger_registry=ExecutionLedgerRegistry(instance_id="instance-1"),
        backend=backend,
        verifier=verifier or FakeVerifier(),
        audit_store=audit,
        clock=lambda: clock_value,
    )
    orchestrator = TaskFlowOrchestrator(
        instance_id="instance-1",
        authorization_store=store,
        preflight_gate=gate,
        runner=runner,
        clock=lambda: clock_value,
        speaker=speaker,
        text_sink=text_sink,
    )
    return orchestrator, backend, audit


class RouteGateTests(unittest.TestCase):
    def test_only_task_and_dangerous_routes_enter_the_task_flow(self) -> None:
        for route, intent, tools, mode, confirmation, router_status in (
            ("chat", "general_chat", (), "chat_reply", "none", "success"),
            ("ambiguous", "low_confidence", (), "clarify", "must_clarify", "low_confidence"),
            ("unsupported", "unsupported_request", (), "reject", "none", "success"),
            ("query", "file_read", ("read_text_file",), "read_only_tool", "none", "success"),
        ):
            with self.subTest(route=route):
                item = handoff(
                    route=route,
                    intent=intent,
                    tool_scope=tools,
                    handling_mode=mode,
                    required_confirmation=confirmation,
                    router_status=router_status,
                    confidence=0.4 if router_status != "success" else 1.0,
                )
                with self.assertRaises(TaskFlowError) as ctx:
                    previewer().preview(plan_request(handoff_value=item))
                self.assertEqual(ctx.exception.code, "route_not_supported")

    def test_downgraded_handoffs_cannot_even_be_constructed(self) -> None:
        # 处理模式与确认方式由路由契约强制；本地编排只做防御性复核。
        with self.assertRaises(ContractError):
            handoff(
                route="dangerous",
                intent="delete_file",
                tool_scope=("move_to_recycle_bin",),
                handling_mode="plan_then_execute",
            )
        with self.assertRaises(ContractError):
            handoff(required_confirmation="none")

    def test_tool_scope_must_be_non_empty(self) -> None:
        item = handoff(route="task", intent="planning_request", tool_scope=())
        with self.assertRaises(TaskFlowError) as ctx:
            previewer().preview(plan_request(handoff_value=item))
        self.assertEqual(ctx.exception.code, "empty_tool_scope")

    def test_plan_actions_must_stay_inside_the_routing_tool_scope(self) -> None:
        item = handoff(
            route="dangerous",
            intent="delete_file",
            tool_scope=("move_to_recycle_bin",),
            handling_mode="plan_then_safety",
        )
        with self.assertRaises(TaskFlowError) as ctx:
            previewer().preview(plan_request(application_proposal(), handoff_value=item))
        self.assertEqual(ctx.exception.code, "tool_outside_route_scope")


class PreviewTests(unittest.TestCase):
    def test_l2_task_preview_is_frozen_and_hash_stable(self) -> None:
        preview = previewer().preview(plan_request())
        self.assertEqual(preview.required_level, "L2")
        self.assertIsInstance(preview.plan, ReviewableActionPlan)
        self.assertEqual(preview.plan.plan_id, "plan-1")
        self.assertEqual(preview.handoff.route, "task")
        again = previewer().preview(plan_request())
        self.assertEqual(preview.plan.plan_hash, again.plan.plan_hash)
        self.assertIn("plan-1", preview.execution_plan_markdown)
        self.assertFalse(hasattr(preview, "execute"))

    def test_l3_dangerous_preview_reports_the_highest_level(self) -> None:
        item = handoff(
            route="dangerous",
            intent="delete_file",
            tool_scope=("move_to_recycle_bin",),
            handling_mode="plan_then_safety",
        )
        preview = previewer().preview(plan_request(recycle_proposal(), handoff_value=item))
        self.assertEqual(preview.required_level, "L3")
        self.assertEqual(preview.plan.actions[0].risk_level, "L3")

    def test_unregistered_tool_cannot_be_proposed(self) -> None:
        with self.assertRaises(ValueError):
            TaskActionProposal(action_id="action-1", tool_name="run_any_command with args", arguments={})
        with self.assertRaises(TypeError):
            TaskActionProposal(action_id="action-1", tool_name="list_directory", arguments=[])  # type: ignore[arg-type]

    def test_path_outside_the_allowed_root_is_denied_by_the_policy_engine(self) -> None:
        outside = r"C:\Windows\System32\drivers\etc\hosts"
        item = handoff(
            route="dangerous",
            intent="delete_file",
            tool_scope=("move_to_recycle_bin",),
            handling_mode="plan_then_safety",
        )
        proposal = recycle_proposal(path=outside)
        with self.assertRaises(TaskFlowError) as ctx:
            previewer().preview(plan_request(proposal, handoff_value=item))
        self.assertIn(ctx.exception.code, {"policy_denied", "action_rejected"})

    def test_mixed_risk_plan_is_refused_by_the_level_resolver(self) -> None:
        # 通过路由门时 tool_scope 已保证风险同质；这里直接构造混合计划验证兜底规则。
        registry = build_mvp_tool_registry()
        engine = PolicyEngine(registry, path_revalidator=lambda value: value)
        recycle = recycle_proposal("action-1", 1)
        application = application_proposal("action-2", 2)
        actions = []
        for proposal in (recycle, application):
            manifest = registry.require(proposal.tool_name)
            preview = build_write_action_preview(
                proposal.tool_name,
                proposal.arguments,
                registry=registry,
                path_validator=lambda value, purpose: Path(value),
                application_resolver=lambda application_id: APPLICATION_NAME,
            )
            policy_input = PolicyInput(
                tool_name=manifest.name,
                tool_version=manifest.version,
                manifest_hash=manifest.content_hash,
                risk_level=manifest.risk_level,
                requires_confirmation=manifest.requires_confirmation,
                normalized_arguments=preview.normalized_arguments,
                path_snapshots=proposal.path_snapshots,
            )
            actions.append(
                build_plan_action(
                    action_id=proposal.action_id,
                    policy_input=policy_input,
                    policy_decision=engine.evaluate(policy_input),
                    preview=preview.preview,
                    verification_method=preview.verification_method,
                    rollback_strategy=preview.rollback_strategy,
                    order=proposal.order,
                )
            )
        plan = build_reviewable_plan(
            plan_id="plan-mixed",
            plan_version=1,
            task_id="task-1",
            created_at=CREATED_AT,
            goal="混合风险",
            scope=(r"G:\study\project",),
            exclusions=(),
            file_rules=(),
            recursive=False,
            conflict_policy="deny",
            actions=tuple(actions),
        )
        with self.assertRaises(TaskFlowError) as ctx:
            required_authorization_level(plan)
        self.assertEqual(ctx.exception.code, "unsupported_risk_profile")

    def test_application_resolver_failure_is_rejected(self) -> None:
        registry = build_mvp_tool_registry()

        def resolver(application_id: str) -> str:
            raise ValueError("not whitelisted")

        item = TaskPlanPreviewer(
            registry=registry,
            policy_engine=PolicyEngine(registry, path_revalidator=lambda value: value),
            path_validator=lambda value, purpose: Path(value),
            application_resolver=resolver,
        )
        with self.assertRaises(TaskFlowError) as ctx:
            item.preview(plan_request())
        self.assertEqual(ctx.exception.code, "action_rejected")

    def test_duplicate_action_ids_are_refused(self) -> None:
        with self.assertRaises(TaskFlowError) as ctx:
            previewer().preview(
                plan_request(application_proposal("action-1", 1), application_proposal("action-1", 2))
            )
        self.assertEqual(ctx.exception.code, "plan_rejected")

    def test_proposal_surface_cannot_supply_manifest_facts(self) -> None:
        fields = set(TaskActionProposal.__dataclass_fields__)
        self.assertEqual(fields, {"action_id", "tool_name", "arguments", "path_snapshots", "order"})

    def test_plan_speech_summary_is_deterministic_and_structured(self) -> None:
        preview = previewer().preview(plan_request())
        text = summarize_plan_for_speech(preview.plan)
        self.assertIn("打开已批准的应用", text)
        self.assertIn("start_whitelisted_application", text)
        self.assertIn("L2", text)
        self.assertNotIn("G:\\", text)


class AuthorizationTests(unittest.TestCase):
    def preview(self) -> TaskPlanPreview:
        return previewer().preview(plan_request())

    def test_l2_allow_produces_a_session_bound_authorization(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = self.preview()
        orchestrator.accept_preview(preview)
        outcome = orchestrator.authorize(authorization_request(preview))
        self.assertEqual(outcome.status, "authorized")
        self.assertIsNotNone(outcome.authorization)
        self.assertEqual(outcome.authorization.session_id, "session-1")
        self.assertEqual(outcome.authorization.risk_level, "L2")
        self.assertEqual(outcome.authorization.consumed_at, None)
        self.assertEqual(orchestrator.state, TaskFlowState.AUTHORIZED)

    def test_l2_deny_and_close_never_produce_an_authorization(self) -> None:
        for event in ("deny", "close", "escape", "stop"):
            with self.subTest(event=event):
                orchestrator, _, _ = build_orchestrator()
                preview = self.preview()
                orchestrator.accept_preview(preview)
                outcome = orchestrator.authorize(authorization_request(preview, user_decision=event))
                self.assertEqual(outcome.status, "denied")
                self.assertIsNone(outcome.authorization)
                self.assertEqual(orchestrator.state, TaskFlowState.REJECTED)

    def test_authorization_requires_a_frozen_preview_first(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.authorize(authorization_request(self.preview()))
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_authorize_rejects_another_preview_than_the_frozen_one(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        first = previewer().preview(plan_request(plan_id="plan-1"))
        second = previewer().preview(plan_request(plan_id="plan-2"))
        orchestrator.accept_preview(first)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.authorize(authorization_request(second))
        self.assertEqual(ctx.exception.code, "preview_drift")

    def test_l3_requires_the_second_stage_phrase(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        item = handoff(
            route="dangerous",
            intent="delete_file",
            tool_scope=("move_to_recycle_bin",),
            handling_mode="plan_then_safety",
        )
        preview = previewer().preview(plan_request(recycle_proposal(), handoff_value=item))
        orchestrator.accept_preview(preview)
        first = orchestrator.authorize(authorization_request(preview))
        self.assertEqual(first.status, "awaiting_phrase")
        self.assertIsNone(first.authorization)
        self.assertEqual(orchestrator.state, TaskFlowState.PREVIEW_READY)
        wrong = orchestrator.authorize(authorization_request(preview, phrase="执行吧"))
        self.assertEqual(wrong.status, "awaiting_phrase")
        self.assertEqual(wrong.reason_code, "phrase_mismatch")
        self.assertEqual(orchestrator.state, TaskFlowState.PREVIEW_READY)
        allowed = orchestrator.authorize(authorization_request(preview, phrase="确认执行"))
        self.assertEqual(allowed.status, "authorized")
        self.assertEqual(allowed.authorization.confirmation_phrase, "确认执行")
        self.assertEqual(orchestrator.state, TaskFlowState.AUTHORIZED)

    def test_pending_l3_window_refuses_a_second_window_without_a_phrase(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        item = handoff(
            route="dangerous",
            intent="delete_file",
            tool_scope=("move_to_recycle_bin",),
            handling_mode="plan_then_safety",
        )
        preview = previewer().preview(plan_request(recycle_proposal(), handoff_value=item))
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.authorize(
                authorization_request(preview, authorization_id="authorization-2", confirmation_window_id="window-2")
            )
        self.assertEqual(ctx.exception.code, "confirmation_pending")

    def test_expired_window_is_refused_without_authorization(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = self.preview()
        orchestrator.accept_preview(preview)
        outcome = orchestrator.authorize(authorization_request(preview, decision_at="2026-09-21T13:06:00Z"))
        self.assertIn(outcome.status, {"denied", "expired", "refused"})
        self.assertIsNone(outcome.authorization)
        self.assertEqual(orchestrator.state, TaskFlowState.REJECTED)

    def test_authorization_cannot_be_reused_after_execution(self) -> None:
        orchestrator, backend, _ = build_orchestrator()
        preview = self.preview()
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        orchestrator.execute(execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1")
        self.assertEqual(backend.calls, 1)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.execute(execution_id="execution-2", executor_instance_id="executor-1", result_id="result-2")
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(backend.calls, 1)


class ExecutionTests(unittest.TestCase):
    def run_flow(self, **kwargs):
        orchestrator, backend, audit = build_orchestrator(**kwargs)
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        authorization = orchestrator.authorize(authorization_request(preview))
        self.assertEqual(authorization.status, "authorized")
        outcome = orchestrator.execute(
            execution_id="execution-1",
            executor_instance_id="executor-1",
            result_id="result-1",
        )
        return orchestrator, outcome, backend, audit

    def test_completed_execution_uses_the_ledger_and_one_attempt(self) -> None:
        orchestrator, outcome, backend, audit = self.run_flow()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.ledger_status, "COMPLETED")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(
            audit.events,
            ["execution_started", "operation_started", "operation_result", "execution_finished"],
        )
        self.assertEqual(orchestrator.state, TaskFlowState.EXECUTED)
        self.assertEqual(orchestrator.document.overall_status, "completed")

    def test_preflight_failure_blocks_execution_entirely(self) -> None:
        orchestrator, backend, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview, target_state_hash="sha256:" + "b" * 64))
        outcome = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        self.assertEqual(outcome.status, "preflight_refused")
        self.assertEqual(outcome.reason_code, "target_state_changed")
        self.assertIsNone(outcome.document)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(orchestrator.state, TaskFlowState.FAILED)

    def test_deterministic_backend_failure_is_not_retried(self) -> None:
        _orchestrator, outcome, backend, _ = self.run_flow(backend=FakeBackend(fail=True))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(outcome.document.actions[0].outcome, "failed")
        self.assertEqual(outcome.document.actions[0].error_code, "tool_failed")

    def test_uncertain_side_effect_is_propagated_as_state_uncertain(self) -> None:
        _orchestrator, outcome, backend, _ = self.run_flow(backend=FakeBackend(fail=True, uncertain=True))
        self.assertEqual(outcome.status, "state_uncertain")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(outcome.document.actions[0].outcome, "state_uncertain")

    def test_verifier_uncertainty_is_propagated(self) -> None:
        _orchestrator, outcome, backend, _ = self.run_flow(verifier=FakeVerifier(status="state_uncertain"))
        self.assertEqual(outcome.status, "state_uncertain")
        self.assertEqual(backend.calls, 1)

    def test_audit_failure_stops_execution_with_an_uncertain_result(self) -> None:
        orchestrator, backend, _ = build_orchestrator(audit=FakeAudit(fail_on="operation_result"))
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        outcome = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        self.assertEqual(outcome.status, "state_uncertain")
        self.assertEqual(outcome.reason_code, "audit_append_failed")
        self.assertIsNone(outcome.document)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(orchestrator.state, TaskFlowState.FAILED)

    def test_execute_requires_a_consumable_authorization(self) -> None:
        orchestrator, backend, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.execute(
                execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
            )
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(backend.calls, 0)

    def test_consumption_is_single_use_across_two_orchestrators(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        outcome = orchestrator.authorize(authorization_request(preview))
        store = AuthorizationConsumptionStore(instance_id="instance-1")
        with self.assertRaises(Exception):
            store.consume(
                outcome.authorization,
                issuer_instance_id="instance-2",
                session_id="session-1",
                operation_id="operation-1",
                plan_id=preview.plan.plan_id,
                plan_version=preview.plan.plan_version,
                plan_hash=preview.plan.plan_hash,
                target_state_hash=HASH_A,
                action_ids=(preview.plan.actions[0].action_id,),
                consumed_at=CHECKED_AT,
            )


class ReportTests(unittest.TestCase):
    def test_report_speaks_only_the_structured_summary(self) -> None:
        stream = FakeStream()
        sink: list[str] = []
        orchestrator, _, _ = build_orchestrator(speaker=FakeSpeaker(stream), text_sink=sink.append)
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        orchestrator.execute(execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1")

        report = orchestrator.report()

        self.assertEqual(orchestrator.state, TaskFlowState.REPORTED)
        self.assertEqual(report.status, "completed")
        self.assertEqual(stream.chunks, [report.text])
        self.assertEqual(sink, [report.text])
        self.assertEqual(stream.finished, 1)
        self.assertIn("任务执行完成", report.text)
        self.assertIn("成功 1", report.text)
        self.assertNotIn(APPLICATION_NAME, report.text)

    def test_speaker_failure_never_changes_the_structured_result(self) -> None:
        orchestrator, _, _ = build_orchestrator(speaker=FakeSpeaker(error=RuntimeError("no device")))
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        execution = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        report = orchestrator.report()
        self.assertFalse(report.spoken)
        self.assertEqual(report.status, "completed")
        self.assertEqual(execution.document.overall_status, "completed")
        self.assertEqual(orchestrator.document, execution.document)

    def test_report_of_a_refused_preflight_has_no_document(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview, target_state_hash="sha256:" + "b" * 64))
        orchestrator.execute(execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1")
        report = orchestrator.report()
        self.assertEqual(report.status, "preflight_refused")
        self.assertIsNone(report.document)
        self.assertIn("复检", report.text)

    def test_report_of_a_rejection_never_claims_execution(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview, user_decision="deny"))
        report = orchestrator.report()
        self.assertEqual(report.status, "rejected")
        self.assertIn("未执行", report.text)

    def test_report_before_any_outcome_is_refused(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.report()
        self.assertEqual(ctx.exception.code, "invalid_state")


class SummaryTests(unittest.TestCase):
    def test_summary_uses_only_structured_facts(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        execution = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        text = summarize_result_document(execution.document)
        self.assertIn("任务执行完成", text)
        self.assertIn("共 1 个动作", text)
        self.assertIn("成功 1", text)

    def test_summary_never_leaks_error_messages_paths_or_payloads(self) -> None:
        orchestrator, _, _ = build_orchestrator(backend=FakeBackend(fail=True, uncertain=True))
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.authorize(authorization_request(preview))
        execution = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        text = summarize_result_document(execution.document)
        self.assertIn("状态不确定", text)
        self.assertIn("tool_failed", text)
        self.assertNotIn("注入的确定性失败", text)
        self.assertNotIn("G:\\", text)
        self.assertNotIn("path", text)

    def test_summary_rejects_a_non_document(self) -> None:
        with self.assertRaises(TypeError):
            summarize_result_document("document")  # type: ignore[arg-type]


class StateMachineTests(unittest.TestCase):
    def test_initial_state_and_close_are_fail_closed(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        self.assertEqual(orchestrator.state, TaskFlowState.IDLE)
        orchestrator.close()
        self.assertEqual(orchestrator.state, TaskFlowState.TERMINAL)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.close()
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_state_machine_refuses_out_of_order_calls_after_close(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        orchestrator.close()
        with self.assertRaises(TaskFlowError):
            orchestrator.authorize(authorization_request(preview))
        with self.assertRaises(TaskFlowError):
            orchestrator.execute(execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1")

    def test_accept_preview_twice_is_refused_without_state_change(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.accept_preview(preview)
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(orchestrator.state, TaskFlowState.PREVIEW_READY)

    def test_orchestrator_surface_has_no_tool_or_shell_entry(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        public = {name for name in dir(orchestrator) if not name.startswith("_")}
        self.assertEqual(
            public,
            {
                "state",
                "preview",
                "authorization",
                "document",
                "last_report",
                "accept_preview",
                "announce_plan",
                "authorize",
                "execute",
                "report",
                "close",
            },
        )
        for name in public:
            lowered = name.lower()
            for forbidden in ("shell", "command", "subprocess", "arbitrary", "force"):
                self.assertNotIn(forbidden, lowered)


class AnnouncementTests(unittest.TestCase):
    def test_announce_plan_speaks_the_frozen_plan_summary(self) -> None:
        stream = FakeStream()
        orchestrator, _, _ = build_orchestrator(speaker=FakeSpeaker(stream))
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        text = orchestrator.announce_plan()
        self.assertEqual(stream.chunks, [text])
        self.assertIn("start_whitelisted_application", text)
        self.assertEqual(orchestrator.state, TaskFlowState.PREVIEW_READY)

    def test_announce_plan_requires_a_preview_and_a_speaker(self) -> None:
        orchestrator, _, _ = build_orchestrator()
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.announce_plan()
        self.assertEqual(ctx.exception.code, "invalid_state")
        preview = previewer().preview(plan_request())
        orchestrator.accept_preview(preview)
        self.assertIn("start_whitelisted_application", orchestrator.announce_plan())


class AuditWiringTests(unittest.TestCase):
    def test_documents_are_not_written_to_disk_by_the_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AuditStore(Path(temp_dir), source_instance_id="instance-1")
            orchestrator, _, _ = build_orchestrator(audit=store)
            preview = previewer().preview(plan_request())
            orchestrator.accept_preview(preview)
            orchestrator.authorize(authorization_request(preview))
            orchestrator.execute(
                execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
            )
            self.assertEqual(
                [event.event_type for event in store.read_events()],
                ["execution_started", "operation_started", "operation_result", "execution_finished"],
            )
            self.assertEqual(list(Path(temp_dir).rglob("plan.json")), [])
            self.assertEqual(list(Path(temp_dir).rglob("result.json")), [])


if __name__ == "__main__":
    unittest.main()
