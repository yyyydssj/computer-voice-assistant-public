"""P8 批次 4：任务路径编排（计划预览 → 授权 → 预检 → 执行 → 验证 → 摘要）。

本模块只编排既有本地能力，不新增任何工具、命令面或授权语义：

```text
RouteHandoff(task/dangerous)
→ PolicyEngine + 冻结 ActionPlan（不可执行）
→ L2 / L3 确认（复用 P5 控制器，固定短语与一次性消费不变）
→ ExecutionPreflightGate（执行前复检并原子消费授权）
→ ControlledExecutionRunner（备份、固定后端、ResultVerifier、ExecutionLedger）
→ 结构化 ResultDocument
→ 只由结构化事实生成的摘要（语音/文字）
```

硬规则：计划必须来自 Agent/调用方提供的结构化候选动作，Manifest 事实一律由本地
注册表提供；路由范围之外的候选动作、混合风险计划、只读直执行路径、已消费授权、
旧窗口、计划漂移、任意命令入口全部 fail closed。本模块不写计划或结果文件，不自动
重试、不自动回滚、不自动续跑，也不触碰 P5 的授权规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from app.contracts import AuthorizationSnapshot
from app.contracts.base import (
    parse_utc_timestamp,
    require_choice,
    require_identifier,
    require_non_empty,
    require_positive,
)
from app.contracts.routing import RouteHandoff
from app.executor import ControlledExecutionError
from app.plans import (
    ActionPlanError,
    ResultDocument,
    ReviewableActionPlan,
    build_plan_action,
    build_reviewable_plan,
    render_execution_plan_markdown,
)
from app.runtime.orchestration.summary import (
    summarize_plan_for_speech,
    summarize_result_document,
)
from app.safety import PolicyEngine, PolicyInput
from app.safety.confirmation import (
    AuthorizationConsumptionStore,
    ConfirmationError,
    L2ConfirmationController,
    L3ConfirmationController,
)
from app.safety.path_guard import PathSnapshot
from app.safety.preflight import ExecutionPreflightGate, PreflightError
from app.tools import (
    ManifestError,
    ToolRegistry,
    WritePlanError,
    build_write_action_preview,
)

TASK_ROUTES = frozenset({"task", "dangerous"})
_HANDLING_MODES = {"task": "plan_then_execute", "dangerous": "plan_then_safety"}
_LEVELS = frozenset({"L2", "L3"})
_DECISIONS = frozenset({"allow", "deny", "close", "escape", "stop", "timeout"})
_DOCUMENT_STATUSES = frozenset({"completed", "failed", "partially_completed", "state_uncertain"})
_EXECUTION_STATUSES = _DOCUMENT_STATUSES | frozenset({"preflight_refused"})
_REPORT_STATUSES = _EXECUTION_STATUSES | frozenset({"rejected"})

_REJECTED_TEXT = "任务已拒绝，未执行任何操作。"
_PREFLIGHT_REFUSED_TEXT = "执行前复检未通过，任务已中止，未执行任何操作。"
_UNCERTAIN_TEXT = "执行结果状态不确定，需要人工确认；系统不会自动重试或回滚。"


class TaskFlowError(RuntimeError):
    """任务编排状态、路由或计划事实不满足本地硬规则。"""

    def __init__(self, code: str, message: str) -> None:
        require_identifier("TaskFlowError.code", code)
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class TaskFlowState(str, Enum):
    IDLE = "idle"
    PREVIEW_READY = "preview_ready"
    AUTHORIZED = "authorized"
    EXECUTED = "executed"
    REPORTED = "reported"
    REJECTED = "rejected"
    FAILED = "failed"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class TaskFlowDiagnostic:
    """脱敏诊断：只有状态与原因码，不含参数、路径或载荷。"""

    event_type: str
    subject_id: str
    reason: str
    state: TaskFlowState

    def to_details(self) -> dict[str, str]:
        return {"reason": self.reason, "state": self.state.value}


@dataclass(frozen=True, slots=True)
class TaskActionProposal:
    """Agent/调用方给出的候选动作；Manifest 事实一律由本地注册表提供。"""

    action_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    path_snapshots: tuple[PathSnapshot, ...] = ()
    order: int = 1

    def __post_init__(self) -> None:
        require_identifier("action_id", self.action_id)
        require_identifier("tool_name", self.tool_name)
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments 必须是对象")
        if any(type(name) is not str for name in self.arguments):
            raise TypeError("arguments 的键必须是字符串")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))
        snapshots = tuple(self.path_snapshots)
        if any(not isinstance(item, PathSnapshot) for item in snapshots):
            raise TypeError("path_snapshots 必须是 PathSnapshot 元组")
        object.__setattr__(self, "path_snapshots", snapshots)
        require_positive("order", self.order)


@dataclass(frozen=True, slots=True)
class TaskPlanRequest:
    """一次任务计划的本地冻结输入。"""

    handoff: RouteHandoff
    task_id: str
    plan_id: str
    goal: str
    scope: tuple[str, ...]
    actions: tuple[TaskActionProposal, ...]
    created_at: str
    plan_version: int = 1
    exclusions: tuple[str, ...] = ()
    file_rules: tuple[str, ...] = ()
    recursive: bool = False
    conflict_policy: str = "deny"

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, RouteHandoff):
            raise TypeError("handoff 必须是 RouteHandoff")
        require_identifier("task_id", self.task_id)
        require_identifier("plan_id", self.plan_id)
        require_non_empty("goal", self.goal, maximum=4096)
        require_positive("plan_version", self.plan_version)
        parse_utc_timestamp("created_at", self.created_at)
        if type(self.recursive) is not bool:
            raise TypeError("recursive 必须是布尔值")
        require_non_empty("conflict_policy", self.conflict_policy, maximum=64)
        scope = tuple(self.scope)
        actions = tuple(self.actions)
        if not scope or any(not isinstance(item, str) or not item for item in scope):
            raise ValueError("scope 必须是非空字符串元组")
        if not actions or any(not isinstance(item, TaskActionProposal) for item in actions):
            raise TypeError("actions 必须是非空 TaskActionProposal 元组")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "file_rules", tuple(self.file_rules))
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True, slots=True)
class TaskPlanPreview:
    """不可执行的冻结计划预览；不携带任何执行入口。"""

    handoff: RouteHandoff
    plan: ReviewableActionPlan
    required_level: str
    execution_plan_markdown: str
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, RouteHandoff):
            raise TypeError("handoff 必须是 RouteHandoff")
        if not isinstance(self.plan, ReviewableActionPlan):
            raise TypeError("plan 必须是 ReviewableActionPlan")
        require_choice("required_level", self.required_level, set(_LEVELS))
        require_non_empty("execution_plan_markdown", self.execution_plan_markdown, maximum=131072)
        parse_utc_timestamp("created_at", self.created_at)

    def to_review_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan.plan_id,
            "plan_version": self.plan.plan_version,
            "plan_hash": self.plan.plan_hash,
            "route": self.handoff.route,
            "intent": self.handoff.intent,
            "required_level": self.required_level,
            "action_count": len(self.plan.actions),
            "executable": False,
        }


@dataclass(frozen=True, slots=True)
class TaskAuthorizationRequest:
    """一次用户授权交互的本地输入；决策事件来自确认界面/固定短语通道。"""

    plan_id: str
    plan_version: int
    plan_hash: str
    session_id: str
    operation_id: str
    authorization_id: str
    confirmation_window_id: str
    target_state_hash: str
    created_at: str
    expires_at: str
    decision_at: str
    user_decision: str = "allow"
    confirmation_phrase: str | None = None

    def __post_init__(self) -> None:
        for name in ("plan_id", "session_id", "operation_id", "authorization_id", "confirmation_window_id"):
            require_identifier(name, getattr(self, name))
        require_positive("plan_version", self.plan_version)
        require_identifier("plan_hash", self.plan_hash)
        require_identifier("target_state_hash", self.target_state_hash)
        require_choice("user_decision", self.user_decision, set(_DECISIONS))
        for name in ("created_at", "expires_at", "decision_at"):
            parse_utc_timestamp(name, getattr(self, name))
        if self.confirmation_phrase is not None:
            require_non_empty("confirmation_phrase", self.confirmation_phrase, maximum=64)


@dataclass(frozen=True, slots=True)
class TaskAuthorizationOutcome:
    status: str
    reason_code: str
    authorization: AuthorizationSnapshot | None = None

    def __post_init__(self) -> None:
        require_choice(
            "status", self.status, {"authorized", "awaiting_phrase", "denied", "refused"}
        )
        require_identifier("reason_code", self.reason_code)
        if self.status == "authorized":
            if not isinstance(self.authorization, AuthorizationSnapshot):
                raise ValueError("authorized 必须携带 AuthorizationSnapshot")
        elif self.authorization is not None:
            raise ValueError("非授权结果不得携带 AuthorizationSnapshot")


@dataclass(frozen=True, slots=True)
class TaskExecutionOutcome:
    status: str
    reason_code: str
    document: ResultDocument | None = None
    ledger_status: str | None = None

    def __post_init__(self) -> None:
        require_choice("status", self.status, set(_EXECUTION_STATUSES))
        require_identifier("reason_code", self.reason_code)
        if self.document is not None:
            if not isinstance(self.document, ResultDocument):
                raise TypeError("document 必须是 ResultDocument")
            if self.document.overall_status != self.status:
                raise ValueError("执行状态与结构化结果总状态不一致")
        if self.ledger_status is not None:
            require_identifier("ledger_status", self.ledger_status)


@dataclass(frozen=True, slots=True)
class TaskReport:
    status: str
    text: str
    spoken: bool
    document: ResultDocument | None = None

    def __post_init__(self) -> None:
        require_choice("status", self.status, set(_REPORT_STATUSES))
        require_non_empty("text", self.text, maximum=4096)
        if type(self.spoken) is not bool:
            raise TypeError("spoken 必须是布尔值")


class RunExecutor(Protocol):
    def run(
        self,
        *,
        plan: ReviewableActionPlan,
        approval: Any,
        execution_id: str,
        executor_instance_id: str,
        result_id: str,
    ) -> Any: ...


ConfirmationFactory = Callable[[str], object]


def require_task_handoff(handoff: RouteHandoff, registry: ToolRegistry) -> None:
    """只允许 task/dangerous 路由进入任务编排；其余路径 fail closed。"""

    if not isinstance(handoff, RouteHandoff):
        raise TypeError("handoff 必须是 RouteHandoff")
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry 必须是 ToolRegistry")
    if handoff.route not in TASK_ROUTES:
        raise TaskFlowError(
            "route_not_supported",
            f"任务编排只接受 task/dangerous 路由，实际为 {handoff.route}；只读直执行路径尚未实现",
        )
    if handoff.handling_mode != _HANDLING_MODES[handoff.route]:
        raise TaskFlowError("handling_mode_mismatch", "路由处理模式与任务编排不一致")
    if handoff.required_confirmation != "defer_to_safety":
        raise TaskFlowError("confirmation_mode_mismatch", "电脑任务必须交由本地安全层确认")
    if handoff.router.status != "success":
        raise TaskFlowError("router_not_success", "路由器未成功的交接不得进入任务编排")
    if not handoff.tool_scope:
        raise TaskFlowError("empty_tool_scope", "任务路由必须携带明确的工具范围")
    for tool_name in handoff.tool_scope:
        try:
            registry.require(tool_name)
        except ManifestError as exc:
            raise TaskFlowError("tool_not_registered", f"工具 {tool_name!r} 不在本地注册表") from exc


def required_authorization_level(plan: ReviewableActionPlan) -> str:
    """同质 L2/L3 计划各自的确认等级；其余风险组合一律拒绝。"""

    if not isinstance(plan, ReviewableActionPlan):
        raise TypeError("plan 必须是 ReviewableActionPlan")
    levels = {action.risk_level for action in plan.actions}
    if any(
        action.requires_confirmation is not True
        or action.policy_decision.decision != "require_confirmation"
        for action in plan.actions
    ):
        raise TaskFlowError(
            "unsupported_risk_profile",
            "任务编排只接受必须经本地确认的冻结动作",
        )
    if levels == {"L2"}:
        return "L2"
    if levels == {"L3"}:
        return "L3"
    raise TaskFlowError(
        "unsupported_risk_profile",
        f"任务编排只接受同质 L2 或 L3 计划，实际为 {sorted(levels)}",
    )


class TaskPlanPreviewer:
    """把结构化候选动作冻结成本地可审查、不可执行的计划预览。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy_engine: PolicyEngine | None = None,
        path_validator: Callable[[str, str], Any] | None = None,
        application_resolver: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry 必须是 ToolRegistry")
        if policy_engine is None:
            policy_engine = PolicyEngine(registry, path_revalidator=_identity_snapshot)
        if not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine 必须是 PolicyEngine")
        if path_validator is not None and not callable(path_validator):
            raise TypeError("path_validator 必须可调用")
        if application_resolver is not None and not callable(application_resolver):
            raise TypeError("application_resolver 必须可调用")
        self._registry = registry
        self._policy = policy_engine
        self._path_validator = path_validator
        self._application_resolver = application_resolver

    def preview(self, request: TaskPlanRequest) -> TaskPlanPreview:
        if not isinstance(request, TaskPlanRequest):
            raise TypeError("request 必须是 TaskPlanRequest")
        require_task_handoff(request.handoff, self._registry)
        actions = []
        for proposal in request.actions:
            if proposal.tool_name not in request.handoff.tool_scope:
                raise TaskFlowError(
                    "tool_outside_route_scope",
                    f"候选工具 {proposal.tool_name!r} 不在路由工具范围内",
                )
            manifest = self._require_manifest(proposal.tool_name)
            action_preview = self._action_preview(proposal)
            policy_input = PolicyInput(
                tool_name=manifest.name,
                tool_version=manifest.version,
                manifest_hash=manifest.content_hash,
                risk_level=manifest.risk_level,
                requires_confirmation=manifest.requires_confirmation,
                normalized_arguments=action_preview.normalized_arguments,
                path_snapshots=proposal.path_snapshots,
            )
            decision = self._policy.evaluate(policy_input)
            if decision.decision == "deny":
                raise TaskFlowError("policy_denied", decision.reason_code)
            try:
                actions.append(
                    build_plan_action(
                        action_id=proposal.action_id,
                        policy_input=policy_input,
                        policy_decision=decision,
                        preview=action_preview.preview,
                        verification_method=action_preview.verification_method,
                        rollback_strategy=action_preview.rollback_strategy,
                        order=proposal.order,
                    )
                )
            except ActionPlanError as exc:
                raise TaskFlowError("plan_rejected", exc.code) from exc
        try:
            plan = build_reviewable_plan(
                plan_id=request.plan_id,
                plan_version=request.plan_version,
                task_id=request.task_id,
                created_at=request.created_at,
                goal=request.goal,
                scope=request.scope,
                exclusions=request.exclusions,
                file_rules=request.file_rules,
                recursive=request.recursive,
                conflict_policy=request.conflict_policy,
                actions=tuple(actions),
            )
        except ActionPlanError as exc:
            raise TaskFlowError("plan_rejected", exc.code) from exc
        return TaskPlanPreview(
            handoff=request.handoff,
            plan=plan,
            required_level=required_authorization_level(plan),
            execution_plan_markdown=render_execution_plan_markdown(plan),
            created_at=request.created_at,
        )

    def _require_manifest(self, tool_name: str):
        try:
            return self._registry.require(tool_name)
        except ManifestError as exc:
            raise TaskFlowError("tool_not_registered", f"工具 {tool_name!r} 不在本地注册表") from exc

    def _action_preview(self, proposal: TaskActionProposal):
        try:
            return build_write_action_preview(
                proposal.tool_name,
                proposal.arguments,
                registry=self._registry,
                path_validator=self._path_validator,
                application_resolver=self._application_resolver,
            )
        except WritePlanError as exc:
            raise TaskFlowError("action_rejected", exc.code) from exc
        except (ValueError, KeyError) as exc:
            raise TaskFlowError("action_rejected", "local_validation_failed") from exc
        except TypeError as exc:
            raise TaskFlowError("action_rejected", "missing_local_validator") from exc


class TaskFlowOrchestrator:
    """显式驱动的单任务编排：预览 → 授权 → 执行 → 汇报。"""

    def __init__(
        self,
        *,
        instance_id: str,
        authorization_store: AuthorizationConsumptionStore,
        preflight_gate: ExecutionPreflightGate,
        runner: RunExecutor,
        clock: Callable[[], str],
        confirmation_factory: ConfirmationFactory | None = None,
        speaker: object | None = None,
        text_sink: Callable[[str], None] | None = None,
        diagnostic_sink: Callable[[TaskFlowDiagnostic], None] | None = None,
    ) -> None:
        require_identifier("instance_id", instance_id)
        if not isinstance(authorization_store, AuthorizationConsumptionStore):
            raise TypeError("authorization_store 必须是 AuthorizationConsumptionStore")
        if not isinstance(preflight_gate, ExecutionPreflightGate):
            raise TypeError("preflight_gate 必须是 ExecutionPreflightGate")
        if authorization_store.instance_id != instance_id:
            raise ValueError("编排实例与授权存储实例不一致")
        if not callable(getattr(runner, "run", None)):
            raise TypeError("runner 必须提供 run")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if confirmation_factory is not None and not callable(confirmation_factory):
            raise TypeError("confirmation_factory 必须可调用")
        if text_sink is not None and not callable(text_sink):
            raise TypeError("text_sink 必须可调用")
        if diagnostic_sink is not None and not callable(diagnostic_sink):
            raise TypeError("diagnostic_sink 必须可调用")
        self._instance_id = instance_id
        self._store = authorization_store
        self._preflight = preflight_gate
        self._runner = runner
        self._clock = clock
        self._confirmation_factory = confirmation_factory or self._default_confirmation
        self._speaker = speaker
        self._text_sink = text_sink
        self._diagnostic_sink = diagnostic_sink
        self._state = TaskFlowState.IDLE
        self._preview: TaskPlanPreview | None = None
        self._authorization: AuthorizationSnapshot | None = None
        self._pending: TaskAuthorizationRequest | None = None
        self._active_controller: object | None = None
        self._document: ResultDocument | None = None
        self._ledger_status: str | None = None
        self._execution_status: str | None = None
        self._session_id: str | None = None
        self._operation_id: str | None = None
        self._last_report: TaskReport | None = None

    @property
    def state(self) -> TaskFlowState:
        return self._state

    @property
    def preview(self) -> TaskPlanPreview | None:
        return self._preview

    @property
    def authorization(self) -> AuthorizationSnapshot | None:
        return self._authorization

    @property
    def document(self) -> ResultDocument | None:
        return self._document

    @property
    def last_report(self) -> TaskReport | None:
        return self._last_report

    def accept_preview(self, preview: TaskPlanPreview) -> TaskPlanPreview:
        if not isinstance(preview, TaskPlanPreview):
            raise TypeError("preview 必须是 TaskPlanPreview")
        self._require_state(TaskFlowState.IDLE, "accept_preview")
        self._preview = preview
        self._transition(TaskFlowState.PREVIEW_READY, "preview_accepted")
        return preview

    def announce_plan(self) -> str:
        self._require_state(TaskFlowState.PREVIEW_READY, "announce_plan")
        assert self._preview is not None
        text = summarize_plan_for_speech(self._preview.plan)
        self._speak(text)
        return text

    def authorize(self, request: TaskAuthorizationRequest) -> TaskAuthorizationOutcome:
        self._require_state(TaskFlowState.PREVIEW_READY, "authorize")
        if not isinstance(request, TaskAuthorizationRequest):
            raise TypeError("request 必须是 TaskAuthorizationRequest")
        preview = self._preview
        assert preview is not None
        expected = (preview.plan.plan_id, preview.plan.plan_version, preview.plan.plan_hash)
        actual = (request.plan_id, request.plan_version, request.plan_hash)
        if actual != expected:
            raise TaskFlowError("preview_drift", "授权请求绑定的计划与当前冻结计划不一致")
        if self._pending is not None:
            if request.confirmation_phrase is None:
                raise TaskFlowError("confirmation_pending", "L3 第二阶段固定短语尚未提交")
            return self._submit_phrase(request)
        level = preview.required_level
        controller = self._controller(level)
        self._active_controller = controller
        try:
            controller.begin(
                plan=preview.plan,
                session_id=request.session_id,
                operation_id=request.operation_id,
                authorization_id=request.authorization_id,
                confirmation_window_id=request.confirmation_window_id,
                target_state_hash=request.target_state_hash,
                created_at=request.created_at,
                expires_at=request.expires_at,
            )
            resolution = controller.resolve(
                confirmation_window_id=request.confirmation_window_id,
                session_id=request.session_id,
                event=request.user_decision,
                occurred_at=request.decision_at,
                current_plan=preview.plan,
                current_target_state_hash=request.target_state_hash,
            )
        except ConfirmationError as exc:
            return self._refuse(exc.code)
        if resolution.status == "awaiting_phrase":
            if level != "L3":
                return self._refuse("unexpected_awaiting_phrase")
            if request.confirmation_phrase is None:
                self._pending = request
                self._emit("task_authorization_awaiting_phrase", request.operation_id, "modal_allowed")
                return TaskAuthorizationOutcome(
                    status="awaiting_phrase",
                    reason_code=resolution.reason_code,
                )
            return self._submit_phrase(request)
        return self._accept(resolution, request)

    def execute(
        self,
        *,
        execution_id: str,
        executor_instance_id: str,
        result_id: str,
    ) -> TaskExecutionOutcome:
        self._require_state(TaskFlowState.AUTHORIZED, "execute")
        preview = self._preview
        authorization = self._authorization
        assert preview is not None and authorization is not None
        for name, value in (
            ("execution_id", execution_id),
            ("executor_instance_id", executor_instance_id),
            ("result_id", result_id),
        ):
            require_identifier(name, value)
        checked_at = self._now()
        try:
            approval = self._preflight.check(
                authorization=authorization,
                session_id=self._session_id,
                operation_id=self._operation_id,
                current_plan=preview.plan,
                checked_at=checked_at,
            )
        except PreflightError as exc:
            self._authorization = None
            self._execution_status = "preflight_refused"
            self._transition(TaskFlowState.FAILED, "preflight_refused")
            return TaskExecutionOutcome(
                status="preflight_refused", reason_code=exc.code, document=None, ledger_status=None
            )
        self._authorization = None
        try:
            outcome = self._runner.run(
                plan=preview.plan,
                approval=approval,
                execution_id=execution_id,
                executor_instance_id=executor_instance_id,
                result_id=result_id,
            )
        except ControlledExecutionError as exc:
            self._execution_status = "state_uncertain"
            self._transition(TaskFlowState.FAILED, "execution_error")
            return TaskExecutionOutcome(
                status="state_uncertain", reason_code=exc.code, document=None, ledger_status=None
            )
        self._document = outcome.document
        self._ledger_status = outcome.snapshot.ledger.status
        self._execution_status = outcome.document.overall_status
        self._transition(TaskFlowState.EXECUTED, "executed")
        return TaskExecutionOutcome(
            status=outcome.document.overall_status,
            reason_code="executed",
            document=outcome.document,
            ledger_status=outcome.snapshot.ledger.status,
        )

    def report(self) -> TaskReport:
        if self._state not in (TaskFlowState.EXECUTED, TaskFlowState.FAILED, TaskFlowState.REJECTED):
            raise TaskFlowError("invalid_state", f"当前状态 {self._state.value} 不允许 report")
        if self._state is TaskFlowState.REJECTED:
            status = "rejected"
            text = _REJECTED_TEXT
        elif self._state is TaskFlowState.FAILED:
            status = self._execution_status or "state_uncertain"
            text = (
                _PREFLIGHT_REFUSED_TEXT
                if status == "preflight_refused"
                else _UNCERTAIN_TEXT
            )
        else:
            assert self._document is not None
            status = self._document.overall_status
            text = summarize_result_document(self._document)
        spoken = self._speak(text)
        if self._text_sink is not None:
            try:
                self._text_sink(text)
            except Exception:  # noqa: BLE001 - text output must not change facts
                pass
        self._transition(TaskFlowState.REPORTED, "reported")
        report = TaskReport(status=status, text=text, spoken=spoken, document=self._document)
        self._last_report = report
        self._emit("task_reported", "task-flow", status)
        return report

    def close(self) -> None:
        if self._state is TaskFlowState.TERMINAL:
            raise TaskFlowError("invalid_state", "任务编排已终止")
        self._pending = None
        self._active_controller = None
        self._authorization = None
        self._transition(TaskFlowState.TERMINAL, "closed")

    def _submit_phrase(self, request: TaskAuthorizationRequest) -> TaskAuthorizationOutcome:
        preview = self._preview
        pending = self._pending or request
        controller = self._active_controller
        assert preview is not None
        if controller is None:
            self._pending = None
            raise TaskFlowError("confirmation_pending", "L3 确认窗口已失效，必须重新开始确认")
        try:
            resolution = controller.submit_phrase(
                confirmation_window_id=pending.confirmation_window_id,
                session_id=pending.session_id,
                phrase=request.confirmation_phrase,
                occurred_at=request.decision_at,
                current_plan=preview.plan,
                current_target_state_hash=pending.target_state_hash,
            )
        except ConfirmationError as exc:
            self._pending = None
            return self._refuse(exc.code)
        if resolution.status == "awaiting_phrase":
            self._pending = pending
            return TaskAuthorizationOutcome(
                status="awaiting_phrase", reason_code=resolution.reason_code
            )
        self._pending = None
        return self._accept(resolution, request)

    def _accept(self, resolution: object, request: TaskAuthorizationRequest) -> TaskAuthorizationOutcome:
        status = getattr(resolution, "status", "")
        reason_code = getattr(resolution, "reason_code", "unknown")
        authorization = getattr(resolution, "authorization", None)
        self._active_controller = None
        if status != "authorized" or not isinstance(authorization, AuthorizationSnapshot):
            self._execution_status = "rejected"
            self._transition(TaskFlowState.REJECTED, "authorization_refused")
            return TaskAuthorizationOutcome(status="denied", reason_code=reason_code)
        self._authorization = authorization
        self._session_id = request.session_id
        self._operation_id = request.operation_id
        self._transition(TaskFlowState.AUTHORIZED, "authorized")
        self._emit("task_authorized", request.operation_id, reason_code)
        return TaskAuthorizationOutcome(
            status="authorized", reason_code=reason_code, authorization=authorization
        )

    def _refuse(self, reason_code: str) -> TaskAuthorizationOutcome:
        self._pending = None
        self._active_controller = None
        self._execution_status = "rejected"
        self._transition(TaskFlowState.REJECTED, "confirmation_refused")
        return TaskAuthorizationOutcome(status="refused", reason_code=reason_code)

    def _default_confirmation(self, level: str) -> object:
        if level == "L2":
            return L2ConfirmationController(
                instance_id=self._instance_id, authorization_store=self._store
            )
        return L3ConfirmationController(
            instance_id=self._instance_id, authorization_store=self._store
        )

    def _controller(self, level: str) -> object:
        controller = self._confirmation_factory(level)
        for name in ("begin", "resolve"):
            if not callable(getattr(controller, name, None)):
                raise TypeError(f"确认控制器必须提供 {name}")
        if level == "L3" and not callable(getattr(controller, "submit_phrase", None)):
            raise TypeError("L3 确认控制器必须提供 submit_phrase")
        return controller

    def _require_state(self, expected: TaskFlowState, action: str) -> None:
        if self._state is not expected:
            raise TaskFlowError(
                "invalid_state", f"当前状态 {self._state.value} 不允许 {action}"
            )

    def _transition(self, target: TaskFlowState, reason: str) -> None:
        self._state = target
        self._emit(f"task_flow_{target.value}", "task-flow", reason)

    def _speak(self, text: str) -> bool:
        start_stream = getattr(self._speaker, "start_stream", None)
        if not callable(start_stream):
            return False
        try:
            stream = start_stream()
        except Exception:  # noqa: BLE001 - speech is optional
            return False
        if stream is None:
            return False
        push = getattr(stream, "push_text_chunk", None)
        try:
            if callable(push):
                push(text)
            finish = getattr(stream, "finish", None)
            if callable(finish):
                finish()
        except Exception:  # noqa: BLE001 - TTS failure must not change facts
            cancel = getattr(stream, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            return False
        return True

    def _now(self) -> str:
        value = self._clock()
        parse_utc_timestamp("clock", value)
        return value

    def _emit(self, event_type: str, subject_id: str, reason: str) -> None:
        if self._diagnostic_sink is None:
            return
        self._diagnostic_sink(
            TaskFlowDiagnostic(
                event_type=event_type,
                subject_id=subject_id,
                reason=reason,
                state=self._state,
            )
        )


def _identity_snapshot(value: PathSnapshot) -> PathSnapshot:
    return value


__all__ = [
    "TASK_ROUTES",
    "TaskActionProposal",
    "TaskAuthorizationOutcome",
    "TaskAuthorizationRequest",
    "TaskExecutionOutcome",
    "TaskFlowDiagnostic",
    "TaskFlowError",
    "TaskFlowOrchestrator",
    "TaskFlowState",
    "TaskPlanPreview",
    "TaskPlanPreviewer",
    "TaskPlanRequest",
    "TaskReport",
    "require_task_handoff",
    "required_authorization_level",
    "summarize_plan_for_speech",
    "summarize_result_document",
]
