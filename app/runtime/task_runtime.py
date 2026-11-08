"""P10 · T1 批次 2b：任务执行的**装配层**（唯一允许读环境/文件系统/哈希的地方）。

`app/runtime/orchestration/**` 被 P8 阶段验收硬性禁止导入 `os`/`pathlib`/`shutil` 等（"编排层无任意
执行面"），因此"把 P5/P8 的既有部件接起来"这件事必须落在这里：

```text
build_text_task_dispatcher(data_root)
├─ 白名单      : calc / notepad —— **装配时计算真实 SHA-256**
├─ previewer   : TaskPlanPreviewer（真实 ToolRegistry + PolicyEngine）
├─ 每任务      : TaskFlowOrchestrator（单任务状态机）
│    ├─ AuthorizationConsumptionStore + ExecutionPreflightGate
│    └─ ControlledExecutionRunner(LocalExecutionBackend + LocalResultVerifier + AuditStore)
└─ dispatcher  : 文本入口用的 handles()/run()（确认读取器由调用方注入）
```

安全口径（用户 2026-10-13 决定 = 方案 C）：

- `target_state_hasher(plan)` = **白名单可执行文件身份**的 sha256（`application_id` + 装配时登记的
  哈希 + **执行前实测哈希**）；**非启动动作直接抛错** ⇒ 预检 `target_state_unavailable` ⇒ 拒绝执行；
- `content_hash_reader(path)` = 真实文件 sha256（启动动作用不到，但预检门构造必须传）；
- `path_revalidator` = 本批**被调用即抛错**（启动动作没有路径快照；文件类真正接入时再实现）；
- 白名单任一可执行文件缺失/不可读 ⇒ 装配失败 ⇒ **任务执行整体降级为不可用**（聊天与语音不受影响）。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from app.auditing import AuditStore
from app.contracts.routing import RouteHandoff
from app.executor import (
    ApplicationSpec,
    ControlledExecutionRunner,
    ExecutionLedgerRegistry,
    LocalExecutionBackend,
)
from app.runtime.orchestration import (
    TASK_ROUTES,
    TaskDispatchRequest,
    TaskDispatchResult,
    TaskDispatchSession,
    TaskFlowOrchestrator,
    TaskPlanPreviewer,
    TaskPreparation,
    VoiceLines,
    default_applications,
)
from app.runtime.orchestration.task_dispatch import (
    DENIED_TEXT,
    REASON_STALE_PREPARATION,
    STATUS_DENIED,
    TaskDispatchError,
    unsupported_from_handoff,
)
from app.safety import PolicyEngine
from app.safety.confirmation import AuthorizationConsumptionStore
from app.safety.preflight import ExecutionPreflightGate
from app.tools import build_mvp_tool_registry
from app.verification import LocalResultVerifier

LAUNCH_SCOPE = ("application-launch",)
CONFIRM_WINDOW_SECONDS = 300
LAUNCH_TOOL_NAME = "start_whitelisted_application"
_LAUNCH_PATH_REVALIDATOR_ERROR = "path_revalidator_unimplemented"
#: 任务执行**专用**审计文件与固定来源实例。
#: 真机教训（2026-10-13）：`AuditStore` 强制"同一文件只能来自同一 `source_instance_id`"
#: （`app/auditing/store.py` 的"审计来源实例不一致"），而装配层原来每次启动都生成随机 id，
#: 于是**第二次执行**必然 `audit_append_failed`（执行被安全中止、计算器没打开）。
#: 因此：① 用独立文件，不干扰 P5 的 `audit.jsonl`；② 用固定 id，跨进程可继续追加。
TASK_AUDIT_FILENAME = "task-execution.jsonl"
TASK_AUDIT_SOURCE_INSTANCE = "task-runtime"


class TaskRuntimeError(RuntimeError):
    """装配失败（白名单不可用等）；调用方据此把任务执行降级为不可用。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def file_sha256(path: str | Path) -> str:
    """真实文件 sha256（`sha256:` + 64 hex）；不可读一律抛错（fail closed）。"""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def utc_timestamp(moment: datetime) -> str:
    """契约要求的 `...Z` 形式 UTC 时间戳。"""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_timestamp(value: str) -> datetime:
    """解析自己产出的 `...Z` 时间戳（用于从注入时钟推算授权窗口）。"""

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_application_whitelist(*, system_root: str | None = None) -> dict[str, ApplicationSpec]:
    """首批白名单：`calc` / `notepad`，并把**真实文件哈希**填进 `executable_hash`。"""

    root = system_root or os.environ.get("SystemRoot") or r"C:\Windows"
    specs = default_applications(system_root=root)
    registered: dict[str, ApplicationSpec] = {}
    for application_id, spec in specs.items():
        try:
            digest = file_sha256(spec.executable_path)
        except OSError as exc:
            raise TaskRuntimeError(
                "application_unavailable", f"白名单可执行文件不可读：{application_id}"
            ) from exc
        registered[application_id] = ApplicationSpec(
            application_id=spec.application_id,
            application_name=spec.application_name,
            executable_path=spec.executable_path,
            executable_hash=digest,
        )
    return registered


def launch_target_state_hasher(
    applications: Mapping[str, ApplicationSpec],
) -> Callable[[Any], str]:
    """方案 C：目标状态 = 白名单可执行文件身份（`application_id` + **执行前实测** sha256）。

    授权时与执行前各算一次；两次之间 exe 被替换/系统更新 ⇒ 哈希不同 ⇒ 预检
    `target_state_changed` 拒绝执行（**由预检报告差异**，本函数不自己抛"身份已变"，
    否则会退化成 `target_state_unavailable`、丢失原因码）。装配时登记的哈希仍由执行后端
    与结果验证器校验（`application_identity_changed` / 进程映像核对）。

    **非启动动作一律抛错**（预检转成 `target_state_unavailable`）：没实现的口径宁可不执行。
    """

    def hasher(plan: Any) -> str:
        facts: list[dict[str, str]] = []
        for action in plan.actions:
            if action.tool_name != LAUNCH_TOOL_NAME:
                raise ValueError(f"target_state 口径未覆盖的工具：{action.tool_name}")
            application_id = str(action.normalized_arguments["application_id"])
            spec = applications.get(application_id)
            if spec is None:
                raise ValueError(f"白名单缺少应用：{application_id}")
            facts.append(
                {"application_id": application_id, "executable_hash": file_sha256(spec.executable_path)}
            )
        blob = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    return hasher


def _unsupported_path_revalidator(value: str) -> Any:
    """启动类任务没有路径快照；被调用说明有未接线的文件动作 ⇒ 直接抛错（fail closed）。"""

    raise RuntimeError(_LAUNCH_PATH_REVALIDATOR_ERROR)


def application_resolver(applications: Mapping[str, ApplicationSpec]) -> Callable[[str], str]:
    def resolve(application_id: str) -> str:
        spec = applications.get(application_id)
        if spec is None:
            raise ValueError(f"应用不在本地白名单：{application_id}")
        return spec.application_name

    return resolve


def _default_clock() -> str:
    return utc_timestamp(datetime.now(timezone.utc))


@dataclass(slots=True)
class TextTaskDispatcher:
    """文本与语音共用的派发器。

    - 文本入口：`handles()` 判路由 + `run()` 一步跑完（打印计划 → 读确认 → 执行 → 摘要）；
    - 语音"独立确认轮"：`prepare()` 冻结计划（**不执行**）→ 下一轮 `execute(prepared, answer=ASR原文)`
      或 `cancel(prepared)`。同一时刻**只持有一台派发会话**（对应同一份冻结计划）。
    """

    previewer: TaskPlanPreviewer
    orchestrator_factory: Callable[[], TaskFlowOrchestrator]
    target_state_hasher: Callable[[Any], str]
    applications: Mapping[str, ApplicationSpec]
    aliases: Mapping[str, str] | None = None
    clock: Callable[[], str] = _default_clock
    scope: tuple[str, ...] = LAUNCH_SCOPE
    confirm_window_seconds: int = CONFIRM_WINDOW_SECONDS
    audit: AuditStore | None = None
    voice_lines: VoiceLines | None = None
    session: TaskDispatchSession | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.previewer, TaskPlanPreviewer):
            raise TypeError("previewer 必须是 TaskPlanPreviewer")
        for name, value in (
            ("orchestrator_factory", self.orchestrator_factory),
            ("target_state_hasher", self.target_state_hasher),
            ("clock", self.clock),
        ):
            if not callable(value):
                raise TypeError(f"{name} 必须可调用")
        if not self.applications:
            raise ValueError("applications 不能为空")

    def handles(self, handoff: object) -> bool:
        return isinstance(handoff, RouteHandoff) and handoff.route in TASK_ROUTES

    def notice(self, handoff: object) -> str | None:
        """P10 · T1 #12：`unsupported` 路由的**如实说明**文本（文本入口打印用）。

        非 `unsupported` 路由返回 ``None``（交回原来的"不打印"路径）；这里**不执行任何动作**，
        也不改路由判定 —— 只是把"本批暂不支持"这句话说出来。
        """

        return unsupported_from_handoff(handoff)

    # ---- 两步式（语音"独立确认轮"） ----------------------------------------

    def prepare(self, handoff: RouteHandoff) -> TaskPreparation:
        """冻结计划并给出可播报摘要；**不执行任何动作**（确认读取器在 prepare 阶段用不到）。"""

        if not isinstance(handoff, RouteHandoff):
            raise TypeError("handoff 必须是 RouteHandoff")
        session = self._build_session(lambda plan_text: "")
        outcome = session.prepare(self._build_request(handoff))
        self.session = session if outcome.prepared is not None else None
        return outcome

    def execute(self, prepared: object, *, answer: object = None) -> TaskDispatchResult:
        """用确认轮那句 ASR 原文执行；拿不到对应会话/计划时报 `stale_preparation`（fail closed）。"""

        session = self.session
        self.session = None
        if session is None:
            return self._stale_result()
        try:
            return session.execute(prepared, answer=answer)  # type: ignore[arg-type]
        except TaskDispatchError:
            return self._stale_result()

    def cancel(self, prepared: object) -> TaskDispatchResult:
        """作废冻结计划（超时/说了别的/被新请求打断）。"""

        session = self.session
        self.session = None
        if session is None:
            return self._stale_result()
        try:
            return session.cancel(prepared)  # type: ignore[arg-type]
        except TaskDispatchError:
            return self._stale_result()

    def run(self, handoff: RouteHandoff, *, confirm_reader: Callable[[str], str]) -> TaskDispatchResult:
        if not isinstance(handoff, RouteHandoff):
            raise TypeError("handoff 必须是 RouteHandoff")
        if not callable(confirm_reader):
            raise TypeError("confirm_reader 必须可调用")
        session = self._build_session(confirm_reader)
        return session.handle(self._build_request(handoff))

    # ---- 内部 ---------------------------------------------------------------

    def _build_session(self, confirm_reader: Callable[[str], str]) -> TaskDispatchSession:
        return TaskDispatchSession(
            previewer=self.previewer,
            orchestrator_factory=self.orchestrator_factory,
            target_state_hasher=lambda preview: self.target_state_hasher(preview.plan),
            confirm_reader=confirm_reader,
            clock=self.clock,
            applications=self.applications,
            aliases=self.aliases,
            voice_lines=self.voice_lines,
        )

    def _build_request(self, handoff: RouteHandoff) -> TaskDispatchRequest:
        created_at = self.clock()
        created_moment = parse_utc_timestamp(created_at)
        suffix = uuid.uuid4().hex[:12]
        slot = handoff.slots.get("application_name")
        name = getattr(slot, "value", None)
        goal = f"打开应用：{name}" if type(name) is str and name.strip() else "打开应用"
        return TaskDispatchRequest(
            handoff=handoff,
            task_id=f"task-{suffix}",
            plan_id=f"plan-{suffix}",
            goal=goal,
            scope=self.scope,
            created_at=created_at,
            session_id=f"session-{suffix}",
            operation_id=f"operation-{suffix}",
            authorization_id=f"authorization-{suffix}",
            confirmation_window_id=f"window-{suffix}",
            expires_at=utc_timestamp(
                created_moment + timedelta(seconds=self.confirm_window_seconds)
            ),
            execution_id=f"execution-{suffix}",
            executor_instance_id=f"executor-{suffix}",
            result_id=f"result-{suffix}",
        )

    @staticmethod
    def _stale_result() -> TaskDispatchResult:
        """没有可执行的冻结计划（已作废/已被别的请求打断）⇒ fail closed。"""

        return TaskDispatchResult(
            status=STATUS_DENIED, reason_code=REASON_STALE_PREPARATION, text=DENIED_TEXT
        )


def build_text_task_dispatcher(
    *,
    data_root: str | Path,
    audit_root: str | Path | None = None,
    system_root: str | None = None,
    clock: Callable[[], str] | None = None,
    source_instance_id: str | None = None,
    applications: Mapping[str, ApplicationSpec] | None = None,
) -> TextTaskDispatcher:
    """装配一个文本任务派发器；任何装配失败都抛 `TaskRuntimeError`（调用方降级为不可用）。"""

    root = Path(data_root)
    audit_path = Path(audit_root) if audit_root is not None else root / "audit"
    backup_root = root / "task-backups"
    try:
        audit_path.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TaskRuntimeError("data_root_unavailable", "无法准备任务数据目录") from exc

    specs = dict(applications) if applications is not None else build_application_whitelist(
        system_root=system_root
    )
    registry = build_mvp_tool_registry()
    audit = AuditStore(
        audit_path,
        source_instance_id=source_instance_id or TASK_AUDIT_SOURCE_INSTANCE,
        filename=TASK_AUDIT_FILENAME,
    )
    clock = clock or _default_clock
    target_state_hasher = launch_target_state_hasher(specs)
    resolve_application = application_resolver(specs)
    backend = LocalExecutionBackend(
        allowed_root=root,
        backup_root=backup_root,
        applications=specs,
    )
    verifier = LocalResultVerifier(applications=specs)
    previewer = TaskPlanPreviewer(
        registry=registry,
        policy_engine=PolicyEngine(registry, path_revalidator=_unsupported_path_revalidator),
        application_resolver=resolve_application,
    )

    def orchestrator_factory() -> TaskFlowOrchestrator:
        instance_id = f"instance-{uuid.uuid4().hex[:12]}"
        store = AuthorizationConsumptionStore(instance_id=instance_id)
        gate = ExecutionPreflightGate(
            instance_id=instance_id,
            registry=registry,
            authorization_store=store,
            path_revalidator=_unsupported_path_revalidator,
            target_state_hasher=target_state_hasher,
            content_hash_reader=file_sha256,
            application_resolver=resolve_application,
        )
        runner = ControlledExecutionRunner(
            registry=registry,
            ledger_registry=ExecutionLedgerRegistry(instance_id=instance_id),
            backend=backend,
            verifier=verifier,
            audit_store=audit,
            clock=clock,
        )
        return TaskFlowOrchestrator(
            instance_id=instance_id,
            authorization_store=store,
            preflight_gate=gate,
            runner=runner,
            clock=clock,
        )

    return TextTaskDispatcher(
        previewer=previewer,
        orchestrator_factory=orchestrator_factory,
        target_state_hasher=target_state_hasher,
        applications=specs,
        clock=clock,
        audit=audit,
    )


__all__ = [
    "CONFIRM_WINDOW_SECONDS",
    "LAUNCH_SCOPE",
    "TASK_AUDIT_FILENAME",
    "TASK_AUDIT_SOURCE_INSTANCE",
    "TaskRuntimeError",
    "TextTaskDispatcher",
    "application_resolver",
    "build_application_whitelist",
    "build_text_task_dispatcher",
    "file_sha256",
    "launch_target_state_hasher",
    "parse_utc_timestamp",
    "utc_timestamp",
]
