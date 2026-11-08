"""P5 批次 6：冻结计划的受控执行、备份、验证与结果分类。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from app.contracts import ActionResult
from app.contracts.base import parse_utc_timestamp, require_choice, require_identifier
from app.plans import (
    PlanAction,
    ResultDocument,
    ReviewableActionPlan,
    build_result_action,
    build_result_document,
)
from app.safety.preflight import ExecutionPreflightApproval
from app.tools.manifest import ToolRegistry

from .ledger import ExecutionLedgerError, ExecutionLedgerRegistry, ExecutionLedgerSnapshot


class ControlledExecutionError(ValueError):
    def __init__(self, code: str, message: str, *, action_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.action_id = action_id
        super().__init__(message)


class ExecutionBackendError(RuntimeError):
    def __init__(self, code: str, message: str, *, side_effect_may_have_occurred: bool) -> None:
        require_identifier("ExecutionBackendError.code", code)
        self.code = code
        self.message = message
        self.side_effect_may_have_occurred = bool(side_effect_may_have_occurred)
        super().__init__(message)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("执行事实只允许 JSON 值")


class AuditAppender(Protocol):
    def append(self, event_type: str, *, subject_id: str, details: Mapping[str, Any] | None = None,
               created_at: str | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class BackupRecord:
    backup_id: str
    action_id: str
    attempt_id: str
    created_at: str
    source_paths: tuple[str, ...]
    backup_paths: tuple[str, ...]
    source_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        require_identifier("BackupRecord.backup_id", self.backup_id)
        require_identifier("BackupRecord.action_id", self.action_id)
        require_identifier("BackupRecord.attempt_id", self.attempt_id)
        parse_utc_timestamp("BackupRecord.created_at", self.created_at)
        object.__setattr__(self, "source_paths", tuple(self.source_paths))
        object.__setattr__(self, "backup_paths", tuple(self.backup_paths))
        object.__setattr__(self, "source_digests", tuple(self.source_digests))
        if not self.source_paths or len(self.source_paths) != len(self.backup_paths) or len(self.source_paths) != len(self.source_digests):
            raise ValueError("备份源、目标与摘要必须非空且一一对应")


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    status: str
    facts: Mapping[str, Any]
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        require_choice("VerificationDecision.status", self.status, {"completed", "not_completed", "state_uncertain"})
        if not isinstance(self.facts, Mapping):
            raise TypeError("facts 必须是对象")
        object.__setattr__(self, "facts", _freeze(self.facts))
        if self.status == "completed":
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("completed 验证不得携带错误")
        elif not self.error_code or not self.error_message:
            raise ValueError("非 completed 验证必须携带稳定错误")
        if self.error_code is not None:
            require_identifier("VerificationDecision.error_code", self.error_code)


class ExecutionBackend(Protocol):
    def prepare_backup(self, *, action: PlanAction, execution_id: str, attempt_id: str, created_at: str) -> BackupRecord | None: ...
    def execute(self, *, action: PlanAction, backup: BackupRecord | None) -> Mapping[str, Any]: ...


class ResultVerifier(Protocol):
    def verify(self, *, action: PlanAction, attempt_id: str, output: Mapping[str, Any],
               backup: BackupRecord | None, verified_at: str) -> VerificationDecision: ...


@dataclass(frozen=True, slots=True)
class ControlledExecutionOutcome:
    document: ResultDocument
    snapshot: ExecutionLedgerSnapshot
    backups: tuple[BackupRecord, ...]


Clock = Callable[[], str]
AttemptIdFactory = Callable[[PlanAction], str]


class ControlledExecutionRunner:
    """只执行与已消费 preflight 完全一致的冻结动作；每个动作最多调用一次。"""

    def __init__(self, *, registry: ToolRegistry, ledger_registry: ExecutionLedgerRegistry,
                 backend: ExecutionBackend, verifier: ResultVerifier, audit_store: AuditAppender, clock: Clock,
                 attempt_id_factory: AttemptIdFactory | None = None) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry 必须是 ToolRegistry")
        if not isinstance(ledger_registry, ExecutionLedgerRegistry):
            raise TypeError("ledger_registry 必须是 ExecutionLedgerRegistry")
        if not callable(clock) or not callable(getattr(backend, "prepare_backup", None)) or not callable(getattr(backend, "execute", None)):
            raise TypeError("backend 与 clock 必须提供受控接口")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("verifier 必须提供 verify")
        if not callable(getattr(audit_store, "append", None)):
            raise TypeError("audit_store 必须提供 append")
        self._registry = registry
        self._ledgers = ledger_registry
        self._backend = backend
        self._verifier = verifier
        self._audit = audit_store
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory or (lambda action: f"attempt-{uuid.uuid4().hex}")

    def run(self, *, plan: ReviewableActionPlan, approval: ExecutionPreflightApproval,
            execution_id: str, executor_instance_id: str, result_id: str) -> ControlledExecutionOutcome:
        self._require_frozen_identity(plan, approval)
        created_at = self._now()
        store = self._ledgers.create(execution_id=execution_id, approval=approval, created_at=created_at)
        executor = store.acquire_executor(executor_instance_id)
        self._append_audit(
            "execution_started",
            subject_id=execution_id,
            details={
                "plan_id": plan.plan_id,
                "plan_version": plan.plan_version,
                "plan_hash": plan.plan_hash,
                "action_count": len(plan.actions),
            },
            created_at=created_at,
        )
        result_actions = []
        backups: list[BackupRecord] = []
        terminal = False

        for action in plan.actions:
            if terminal:
                result_actions.append(self._skipped(action, "previous_action_not_completed"))
                continue
            attempt_id = self._attempt_id_factory(action)
            require_identifier("attempt_id", attempt_id)
            started_at = self._now()
            executor.operation_started(action_id=action.action_id, attempt_id=attempt_id, started_at=started_at)
            self._append_audit(
                "operation_started",
                subject_id=execution_id,
                details={
                    "action_id": action.action_id,
                    "attempt_id": attempt_id,
                    "tool_name": action.tool_name,
                },
                created_at=started_at,
            )
            backup: BackupRecord | None = None
            try:
                backup = self._backend.prepare_backup(
                    action=action, execution_id=execution_id, attempt_id=attempt_id, created_at=self._now()
                )
                if backup is not None:
                    if backup.action_id != action.action_id or backup.attempt_id != attempt_id:
                        raise ExecutionBackendError("backup_identity_mismatch", "备份身份与当前动作不一致", side_effect_may_have_occurred=False)
                    backups.append(backup)
                boundary = next(item for item in approval.action_boundaries if item.action_id == action.action_id)
                unsafe = boundary.cancellation_mode == "not_supported"
                if unsafe:
                    executor.enter_unsafe_atomic(action_id=action.action_id, attempt_id=attempt_id, entered_at=self._now())
                try:
                    raw_output = self._backend.execute(action=action, backup=backup)
                finally:
                    if unsafe:
                        executor.leave_unsafe_atomic(action_id=action.action_id, attempt_id=attempt_id, exited_at=self._now())
                if not isinstance(raw_output, Mapping):
                    raise ExecutionBackendError("invalid_tool_output", "工具返回必须是结构化对象", side_effect_may_have_occurred=True)
                output = _freeze(raw_output)
                finished_at = self._now()
                executor.operation_returned(action_id=action.action_id, attempt_id=attempt_id, finished_at=finished_at)
                verified_at = self._now()
                decision = self._verifier.verify(
                    action=action, attempt_id=attempt_id, output=output, backup=backup, verified_at=verified_at
                )
                result_actions.append(self._verified_result(
                    executor=executor, execution_id=execution_id, action=action, attempt_id=attempt_id,
                    started_at=started_at, finished_at=finished_at, verified_at=verified_at,
                    output=output, decision=decision,
                ))
                terminal = decision.status != "completed"
            except ExecutionBackendError as exc:
                finished_at = self._now()
                executor.operation_returned(action_id=action.action_id, attempt_id=attempt_id, finished_at=finished_at)
                verified_at = self._now()
                status = "state_uncertain" if exc.side_effect_may_have_occurred else "not_completed"
                decision = VerificationDecision(
                    status=status,
                    facts={"backend_error": exc.code, "side_effect_may_have_occurred": exc.side_effect_may_have_occurred},
                    error_code=exc.code,
                    error_message=exc.message,
                )
                result_actions.append(self._verified_result(
                    executor=executor, execution_id=execution_id, action=action, attempt_id=attempt_id,
                    started_at=started_at, finished_at=finished_at, verified_at=verified_at,
                    output={}, decision=decision,
                ))
                terminal = True
            except (ControlledExecutionError, ExecutionLedgerError):
                raise
            except Exception as exc:
                finished_at = self._now()
                executor.operation_returned(action_id=action.action_id, attempt_id=attempt_id, finished_at=finished_at)
                verified_at = self._now()
                decision = VerificationDecision(
                    status="state_uncertain",
                    facts={"exception_type": type(exc).__name__},
                    error_code="execution_exception_unknown_state",
                    error_message="执行边界出现未分类异常，无法确认真实状态",
                )
                result_actions.append(self._verified_result(
                    executor=executor, execution_id=execution_id, action=action, attempt_id=attempt_id,
                    started_at=started_at, finished_at=finished_at, verified_at=verified_at,
                    output={}, decision=decision,
                ))
                terminal = True

        completed_at = self._now()
        document = build_result_document(
            plan=plan, result_id=result_id, execution_id=execution_id,
            created_at=created_at, completed_at=completed_at, actions=tuple(result_actions),
        )
        if document.overall_status in {"failed", "partially_completed"}:
            executor.finalize(overall_status=document.overall_status)
        self._append_audit(
            "execution_finished",
            subject_id=execution_id,
            details={
                "result_id": document.result_id,
                "result_hash": document.result_hash,
                "overall_status": document.overall_status,
            },
            created_at=completed_at,
        )
        return ControlledExecutionOutcome(document=document, snapshot=store.snapshot(), backups=tuple(backups))

    def _append_audit(self, event_type: str, *, subject_id: str, details: Mapping[str, Any], created_at: str) -> None:
        try:
            self._audit.append(event_type, subject_id=subject_id, details=details, created_at=created_at)
        except Exception as exc:
            raise ControlledExecutionError("audit_append_failed", "审计追加失败，执行已安全中止") from exc


    def _verified_result(self, *, executor: Any, execution_id: str, action: PlanAction,
                         attempt_id: str, started_at: str, finished_at: str, verified_at: str,
                         output: Mapping[str, Any], decision: VerificationDecision):
        ledger_result = ActionResult(
            execution_id=execution_id,
            action_id=action.action_id,
            attempt_id=attempt_id,
            status=decision.status,
            error_code=decision.error_code,
            completed_at=verified_at,
        )
        executor.record_result(ledger_result)
        self._append_audit(
            "operation_result",
            subject_id=execution_id,
            details={
                "action_id": action.action_id,
                "attempt_id": attempt_id,
                "verification_status": decision.status,
                "error_code": decision.error_code,
            },
            created_at=verified_at,
        )
        if decision.status == "completed":
            outcome = "succeeded"
        elif decision.status == "not_completed":
            outcome = "failed"
        else:
            outcome = "state_uncertain"
        return build_result_action(
            plan_action=action, outcome=outcome, attempt_id=attempt_id,
            started_at=started_at, finished_at=finished_at,
            verification_status=decision.status, verified_at=verified_at,
            verifier=type(self._verifier).__name__, verification_facts=decision.facts,
            output=output, error_code=decision.error_code, error_message=decision.error_message,
        )

    def _skipped(self, action: PlanAction, reason: str):
        return build_result_action(
            plan_action=action, outcome="skipped", attempt_id=None, started_at=None, finished_at=None,
            verification_status="not_completed", verified_at=self._now(),
            verifier=type(self._verifier).__name__, verification_facts={"started": False},
            output={}, skip_reason=reason,
        )

    def _require_frozen_identity(self, plan: ReviewableActionPlan, approval: ExecutionPreflightApproval) -> None:
        if not isinstance(plan, ReviewableActionPlan) or not isinstance(approval, ExecutionPreflightApproval):
            raise TypeError("plan 与 approval 类型无效")
        identity = (plan.plan_id, plan.plan_version, plan.plan_hash, tuple(item.action_id for item in plan.actions))
        approved = (approval.plan_id, approval.plan_version, approval.plan_hash, approval.action_ids)
        if identity != approved:
            raise ControlledExecutionError("preflight_plan_mismatch", "执行计划与 preflight 通过事实不一致")
        for action, boundary in zip(plan.actions, approval.action_boundaries):
            manifest = self._registry.require(action.tool_name)
            if (
                boundary.action_id != action.action_id
                or boundary.tool_name != action.tool_name
                or boundary.manifest_hash != action.manifest_hash
                or manifest.content_hash != action.manifest_hash
                or boundary.cancellation_mode != manifest.cancellation
            ):
                raise ControlledExecutionError("execution_boundary_mismatch", "动作工具或 Manifest 边界已变化", action_id=action.action_id)

    def _now(self) -> str:
        value = self._clock()
        parse_utc_timestamp("clock", value)
        return value
