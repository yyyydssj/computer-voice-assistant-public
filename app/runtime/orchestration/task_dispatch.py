"""P10 · T1 批次 2a/3：**任务派发**（预览 → 确认 → 授权 → 执行 → 汇报）的纯逻辑。

只做顺序编排，不碰 I/O、不碰环境、不碰安全链语义：

- 计划提议来自 `app_launch.build_launch_proposal()`（本地白名单，**不信 LLM 动作参数**）；
- 确认由**注入的读取器**给出（文本 = 读一行；语音 = 独立确认轮），本模块只做判定：
  归一化后**严格等于「确认」**才算允许，其余（含空、超时、异常）一律拒绝；
- `target_state_hash` 由**注入的** `target_state_hasher(preview)` 提供 —— P5 执行前复检会拿它比对，
  它的语义属于安全链口径，因此**不在本模块内定义**（装配层显式决定）；
- `TaskFlowOrchestrator` 是**单任务**状态机（`close()` 后进入 TERMINAL，不可复用）⇒
  每次派发用注入的工厂新建一个；派发结束无论如何都把它收尾。

批次 3 起提供**两步 API**（语音"独立确认轮"要跨轮次持有一份冻结计划）：

```text
prepare(request) -> TaskPreparation(ready, prepared=PreparedTask)   # 只冻结+摘要，不执行
execute(prepared, answer=...) -> TaskDispatchResult                 # 校验确认词 -> 授权 -> 执行 -> 汇报
cancel(prepared) -> TaskDispatchResult                             # 收尾并给"已取消"文案
handle(request) = prepare + 读确认 + execute/cancel                 # 文本入口一步式（旧行为不变）
```

硬规则：**授权前不执行任何东西**；任何拒绝/异常都返回 fail-closed 结果，绝不"继续跑"。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from app.contracts.routing import RouteHandoff
from app.executor import ApplicationSpec
from app.runtime.orchestration.app_launch import (
    AppLaunchError,
    build_launch_proposal,
    display_name,
    resolve_application_id,
)
from app.runtime.orchestration.local_task_router import ROUTE_UNSUPPORTED, STRIP_CHARACTERS
from app.runtime.orchestration.voice_lines import DEFAULT_VOICE_LINES, VoiceLines
from app.runtime.orchestration.task_flow import (
    TaskAuthorizationRequest,
    TaskFlowError,
    TaskFlowOrchestrator,
    TaskFlowState,
    TaskPlanPreview,
    TaskPlanPreviewer,
    TaskPlanRequest,
)

#: 确认词：**以「确认」开头的短句**才算允许。
#: 真机教训（2026-10-13，`--voice`）：用户说「确认执行」，而旧口径"严格等于「确认」"把它判成
#: 未确认并直接作废计划 ⇒ 用户还没被识别完，计划就被取消了。现在放宽到"确认 + ≤6 字 +
#: 不含疑问/否定/取消类词"，于是「确认」「确认执行」「确认一下」「确认吧」都算；
#: 「请确认」（不在句首）、「确认吗」「确认取消」「我不确认」仍拒绝。
CONFIRM_PREFIX = "确认"
CONFIRM_MAX_LENGTH = 6

#: 出现任一标记即**不算**确认（疑问 / 否定 / 反悔）
CONFIRM_REJECT_MARKERS = ("吗", "不", "别", "取消", "算了", "？", "?")

STATUS_READY = "ready"
STATUS_EXECUTED = "executed"
STATUS_DENIED = "denied"
STATUS_REFUSED = "refused"
STATUS_UNSUPPORTED = "unsupported"
STATUS_PREVIEW_FAILED = "preview_failed"
#: 执行层"结果不确定"（验证没过）——**必须原样透出**，不得改写成 executed。
STATUS_STATE_UNCERTAIN = "state_uncertain"
#: 2026-10-14 真机：Win11 应用商店版重定向（记事本）导致"进程身份与白名单不一致"。
CODE_IDENTITY_MISMATCH = "application_identity_mismatch"

REASON_PREPARED = "prepared"
REASON_DENIED = "authorization_denied"
REASON_TIMEOUT = "authorization_timeout"
REASON_CONFIRM_UNAVAILABLE = "confirmation_unavailable"
REASON_AUTHORIZATION_REFUSED = "authorization_refused"
REASON_PREVIEW_FAILED = "preview_failed"
REASON_STALE_PREPARATION = "stale_preparation"

DENIED_TEXT = "已取消，未执行任何操作。"
TIMEOUT_TEXT = "等太久了，已经取消，没有执行。"
"""待确认窗口**到期**作废的专用文案（2026-10-23 真机）。

与 :data:`DENIED_TEXT` 分开：用户需要知道是"等太久"而不是"自己刚取消的"。
审计/事实仍靠 `DROPPED_TIMEOUT` 区分，不靠文案判断。
"""
NO_PENDING_TASK_TEXT = "现在没有等你确认的任务。"
"""没有待确认计划时听到「确认」的固定回应（2026-10-23）。

此前这种"裸确认"会被当成闲聊发给 Provider，用户听到一个与确认无关的回答；
更早的版本里它在超时作废后没有任何反馈。
"""
REFUSED_TEXT = "未获得授权，未执行任何操作。"
PREVIEW_FAILED_TEXT = "计划未通过本地校验，未执行任何操作。"
CONFIRM_UNAVAILABLE_TEXT = "没能拿到你的确认，已按未确认处理，未执行任何操作。"

#: P10 · T1 #14/#16：**口语播报**用的短文本（真机反馈："不要念全文"）。工具名、L2 等只留在
#: `text`（控制台/审计）里；`spoken_text` 只给语音。文案由 `VoiceLines` 出口提供（缺省 = 这两条）。
SPOKEN_UNSUPPORTED_TEXT = DEFAULT_VOICE_LINES.unsupported
SPOKEN_PREVIEW_FAILED_TEXT = DEFAULT_VOICE_LINES.preview_failed

#: P10 · T1 #12：`route="unsupported"` 的**控制台/审计全文**（语音念 `VoiceLines.unsupported`）。
#: 语音短句与文本全文保持**同源**，避免两条通道各写一套后漂移（#14 的教训）。
UNSUPPORTED_TEXT_TEMPLATE = "这条任务本批暂不支持（{code}），未执行任何操作。"


def unsupported_notice(code: str) -> str:
    """把不支持的原因短码渲染成一句如实说明（控制台/审计用）。"""

    return UNSUPPORTED_TEXT_TEMPLATE.format(code=code)


def identity_mismatch_unverified(document: object) -> bool:
    """结构化结果里是否存在"进程身份与白名单不一致"的动作（**读结构化字段**，不解析文本）。

    真机（2026-10-14）：Win11「打开记事本」动作成功但校验必然不一致 ⇒ 需要一句可解释的语音说明。
    """

    actions = getattr(document, "actions", None)
    if not isinstance(actions, tuple):
        return False
    return any(getattr(item, "error_code", None) == CODE_IDENTITY_MISMATCH for item in actions)


def unsupported_from_handoff(handoff: object) -> str | None:
    """`route="unsupported"` 的交接 ⇒ 如实说明全文；其它路由返回 ``None``（不猜测、不改路由）。

    原因短码优先取 `slots["operation"]`（`close_application`/`delete_files` 这类**具体**操作），
    没有就退回 `intent`（如 `unimplemented_capability`）。
    """

    if not isinstance(handoff, RouteHandoff) or handoff.route != ROUTE_UNSUPPORTED:
        return None
    operation = getattr(handoff.slots.get("operation"), "value", None)
    code = operation if type(operation) is str and operation.strip() else handoff.intent
    return unsupported_notice(code)


def _spoken_target(
    handoff: RouteHandoff,
    applications: Mapping[str, ApplicationSpec] | None,
    aliases: Mapping[str, str] | None,
) -> str:
    """把不可信的应用名换成白名单里的显示名（解析不了就用用户原话，绝不编造）。"""

    value = getattr(handoff.slots.get("application_name"), "value", None)
    if type(value) is not str or not value.strip():
        return "这个应用"
    try:
        application_id = resolve_application_id(
            value, applications=applications, aliases=aliases
        )
    except AppLaunchError:
        return value.strip()
    except Exception:  # noqa: BLE001 - 解析异常也按用户原话播报，不得影响任务事实
        return value.strip()
    return display_name(application_id, applications=applications)


class TaskDispatchError(RuntimeError):
    """派发会话的调用方用错 API（例如拿一份已作废的冻结计划去执行）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ConfirmReader(Protocol):
    """确认读取器：拿到计划摘要，返回用户的原始回答（可为空串）。"""

    def __call__(self, plan_text: str) -> str: ...


class OrchestratorFactory(Protocol):
    def __call__(self) -> TaskFlowOrchestrator: ...


TargetStateHasher = Callable[[TaskPlanPreview], str]
Clock = Callable[[], str]


def is_confirmation(text: object) -> bool:
    """只有**明确以「确认」开头的短句**才算允许。

    判定为确认需要同时满足：NFKC + 去首尾空白/标点后**以「确认」开头**、长度 ≤
    `CONFIRM_MAX_LENGTH`、且不含 `CONFIRM_REJECT_MARKERS` 里的疑问/否定/反悔标记。

    刻意**不**剥礼貌前缀（`normalize_command()` 会剥）：那会把"**请**确认"也算成确认，而
    "请确认"更像用户在提问/下指令。于是：
    「确认」「确认执行」「确认一下」「确认吧」⇒ 允许；
    「请确认」「确认一下吗」「确认取消」「我不确认」「好的」「嗯」⇒ 拒绝。
    """

    if type(text) is not str:
        return False
    value = unicodedata.normalize("NFKC", text).strip().strip(STRIP_CHARACTERS)
    if not value.startswith(CONFIRM_PREFIX) or len(value) > CONFIRM_MAX_LENGTH:
        return False
    return not any(marker in value for marker in CONFIRM_REJECT_MARKERS)


@dataclass(frozen=True, slots=True)
class TaskDispatchRequest:
    """一次任务的本地身份与时间事实（全部由调用方提供，模块内不生成、不读时钟）。"""

    handoff: RouteHandoff
    task_id: str
    plan_id: str
    goal: str
    scope: tuple[str, ...]
    created_at: str
    session_id: str
    operation_id: str
    authorization_id: str
    confirmation_window_id: str
    expires_at: str
    execution_id: str
    executor_instance_id: str
    result_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, RouteHandoff):
            raise TypeError("handoff 必须是 RouteHandoff")
        if not self.scope or any(type(item) is not str or not item for item in self.scope):
            raise ValueError("scope 必须是非空字符串元组")


@dataclass(frozen=True, slots=True)
class TaskDispatchResult:
    """派发结果：状态、固定短码、给用户看的文本（不含路径/载荷）、可选计划事实。

    `text` 是完整文本（控制台/审计可见）；`spoken_text`（可空）是**更短的口语播报**文本。
    """

    status: str
    reason_code: str
    text: str
    plan_text: str | None = None
    plan_hash: str | None = None
    spoken_text: str | None = None

    @property
    def executed(self) -> bool:
        return self.status == STATUS_EXECUTED


@dataclass(frozen=True, slots=True)
class PreparedTask:
    """已冻结、**尚未执行**的一份任务计划（持有那台单任务编排器）。"""

    request: TaskDispatchRequest
    preview: TaskPlanPreview
    orchestrator: TaskFlowOrchestrator
    plan_text: str

    @property
    def plan_hash(self) -> str:
        return self.preview.plan.plan_hash


@dataclass(frozen=True, slots=True)
class TaskPreparation:
    """`prepare()` 的结果：要么可执行（带 `prepared`），要么 fail-closed 文案。"""

    ok: bool
    status: str
    reason_code: str
    text: str
    prepared: PreparedTask | None = None
    spoken_text: str | None = None

    def __post_init__(self) -> None:
        if self.ok and not isinstance(self.prepared, PreparedTask):
            raise ValueError("ok=True 必须携带 prepared")
        if not self.ok and self.prepared is not None:
            raise ValueError("ok=False 不得携带 prepared")


class TaskDispatchSession:
    """把一条 `RouteHandoff(task/open_application)` 跑完：预览 → 确认 → 授权 → 执行 → 汇报。"""

    def __init__(
        self,
        *,
        previewer: TaskPlanPreviewer,
        orchestrator_factory: OrchestratorFactory,
        target_state_hasher: TargetStateHasher,
        confirm_reader: ConfirmReader,
        clock: Clock,
        applications: Mapping[str, ApplicationSpec] | None = None,
        aliases: Mapping[str, str] | None = None,
        voice_lines: VoiceLines | None = None,
    ) -> None:
        if not isinstance(previewer, TaskPlanPreviewer):
            raise TypeError("previewer 必须是 TaskPlanPreviewer")
        for name, value in (
            ("orchestrator_factory", orchestrator_factory),
            ("target_state_hasher", target_state_hasher),
            ("confirm_reader", confirm_reader),
            ("clock", clock),
        ):
            if not callable(value):
                raise TypeError(f"{name} 必须可调用")
        if voice_lines is not None and not isinstance(voice_lines, VoiceLines):
            raise TypeError("voice_lines 必须是 VoiceLines 或 None")
        self._previewer = previewer
        self._factory = orchestrator_factory
        self._target_state_hasher = target_state_hasher
        self._confirm_reader = confirm_reader
        self._clock = clock
        self._applications = applications
        self._aliases = aliases
        self._lines = voice_lines or DEFAULT_VOICE_LINES
        self._outstanding: PreparedTask | None = None

    # ---- 一步式（文本入口） -------------------------------------------------

    def handle(self, request: TaskDispatchRequest) -> TaskDispatchResult:
        if not isinstance(request, TaskDispatchRequest):
            raise TypeError("request 必须是 TaskDispatchRequest")
        preparation = self.prepare(request)
        prepared = preparation.prepared
        if prepared is None:
            return TaskDispatchResult(
                status=preparation.status, reason_code=preparation.reason_code, text=preparation.text
            )
        plan_text = prepared.plan_text
        answer, answer_problem = self._read_answer(plan_text)
        if answer_problem is not None:
            self.cancel(prepared)
            return TaskDispatchResult(
                status=STATUS_DENIED,
                reason_code=answer_problem,
                text=CONFIRM_UNAVAILABLE_TEXT,
                plan_text=plan_text,
                plan_hash=prepared.plan_hash,
            )
        if not is_confirmation(answer):
            # 明确拒绝（False）：注意这里**不能**用 `is None` 判断 —— 那会把"没确认"
            # 当成"已确认"继续往下执行（本批的红测抓到的真缺陷）。
            self.cancel(prepared)
            return TaskDispatchResult(
                status=STATUS_DENIED,
                reason_code=REASON_DENIED,
                text=DENIED_TEXT,
                plan_text=plan_text,
                plan_hash=prepared.plan_hash,
            )
        return self.execute(prepared, answer=answer)

    # ---- 两步式（语音"独立确认轮"） -----------------------------------------

    def prepare(self, request: TaskDispatchRequest) -> TaskPreparation:
        """冻结计划并产出可播报摘要；**不执行任何动作**。"""

        if not isinstance(request, TaskDispatchRequest):
            raise TypeError("request 必须是 TaskDispatchRequest")
        self._retire_outstanding()
        try:
            proposal = build_launch_proposal(
                request.handoff, applications=self._applications, aliases=self._aliases
            )
        except AppLaunchError as exc:
            # 危险路由、文件类意图、未登记/多义/缺名：一律**不执行**
            return TaskPreparation(
                ok=False,
                status=STATUS_UNSUPPORTED,
                reason_code=exc.code,
                text=unsupported_notice(exc.code),
                spoken_text=self._lines.unsupported_line(),
            )
        plan_request = TaskPlanRequest(
            handoff=request.handoff,
            task_id=request.task_id,
            plan_id=request.plan_id,
            goal=request.goal,
            scope=request.scope,
            actions=(proposal,),
            created_at=request.created_at,
        )
        try:
            preview = self._previewer.preview(plan_request)
        except TaskFlowError as exc:
            return TaskPreparation(
                ok=False, status=STATUS_PREVIEW_FAILED, reason_code=exc.code,
                text=PREVIEW_FAILED_TEXT, spoken_text=SPOKEN_PREVIEW_FAILED_TEXT,
            )
        except Exception:  # noqa: BLE001 - 计划层任何异常都不允许变成"继续执行"
            return TaskPreparation(
                ok=False,
                status=STATUS_PREVIEW_FAILED,
                reason_code=REASON_PREVIEW_FAILED,
                text=PREVIEW_FAILED_TEXT,
                spoken_text=self._lines.preview_failed_line(),
            )
        orchestrator = self._factory()
        try:
            orchestrator.accept_preview(preview)
            plan_text = orchestrator.announce_plan()
        except Exception:  # noqa: BLE001 - 冻结/播报失败同样不允许"继续执行"
            self._retire(orchestrator)
            return TaskPreparation(
                ok=False,
                status=STATUS_PREVIEW_FAILED,
                reason_code=REASON_PREVIEW_FAILED,
                text=PREVIEW_FAILED_TEXT,
                spoken_text=self._lines.preview_failed_line(),
            )
        prepared = PreparedTask(
            request=request, preview=preview, orchestrator=orchestrator, plan_text=plan_text
        )
        self._outstanding = prepared
        # #14：口语只念短句（工具名/L2 留在 `text` → 控制台与审计）。
        target = _spoken_target(request.handoff, self._applications, self._aliases)
        return TaskPreparation(
            ok=True,
            status=STATUS_READY,
            reason_code=REASON_PREPARED,
            text=plan_text,
            prepared=prepared,
            spoken_text=self._lines.plan_line(target),
        )

    def execute(self, prepared: PreparedTask, *, answer: object = None) -> TaskDispatchResult:
        """校验确认词 → 授权 → 执行 → 汇报；无论如何都收尾那台单任务编排器。

        `answer` **默认不给** ⇒ 不是「确认」⇒ 直接拒绝（fail closed）：调用方必须显式
        把用户的真实回答传进来（语音传确认轮那句 ASR 原文）。
        """

        prepared = self._require_outstanding(prepared)
        self._outstanding = None
        try:
            if not is_confirmation(answer):
                return TaskDispatchResult(
                    status=STATUS_DENIED,
                    reason_code=REASON_DENIED,
                    text=DENIED_TEXT,
                    plan_text=prepared.plan_text,
                    plan_hash=prepared.plan_hash,
                )
            plan = prepared.preview.plan
            authorization = TaskAuthorizationRequest(
                plan_id=plan.plan_id,
                plan_version=plan.plan_version,
                plan_hash=plan.plan_hash,
                session_id=prepared.request.session_id,
                operation_id=prepared.request.operation_id,
                authorization_id=prepared.request.authorization_id,
                confirmation_window_id=prepared.request.confirmation_window_id,
                target_state_hash=self._target_state_hasher(prepared.preview),
                created_at=prepared.request.created_at,
                expires_at=prepared.request.expires_at,
                decision_at=self._clock(),
                user_decision="allow",
                confirmation_phrase=None,
            )
            outcome = prepared.orchestrator.authorize(authorization)
            if outcome.status != "authorized":
                return TaskDispatchResult(
                    status=STATUS_REFUSED,
                    reason_code=outcome.reason_code or REASON_AUTHORIZATION_REFUSED,
                    text=REFUSED_TEXT,
                    plan_text=prepared.plan_text,
                    plan_hash=prepared.plan_hash,
                )
            execution = prepared.orchestrator.execute(
                execution_id=prepared.request.execution_id,
                executor_instance_id=prepared.request.executor_instance_id,
                result_id=prepared.request.result_id,
            )
            report = prepared.orchestrator.report()
            # 状态保真（真机教训 2026-10-13）：执行层"成功"的固定码是 ``completed``；
            # 其余（``state_uncertain`` / ``preflight_refused``）**必须原样透出**，
            # 绝不能再写死成 executed —— 否则日志声称"已执行"、而用户听到的是
            # "结果状态不确定"，事实与文本互相矛盾（真机就是这么发生的）。
            status = STATUS_EXECUTED if execution.status == "completed" else execution.status
            # #14：成功时口语只说"帮你打开X了"；状态不确定/预检拒绝时保持结构化汇报文本。
            spoken = None
            if status == STATUS_EXECUTED:
                target = _spoken_target(
                    prepared.request.handoff, self._applications, self._aliases
                )
                spoken = self._lines.result_line(target)
            elif status == STATUS_STATE_UNCERTAIN and identity_mismatch_unverified(
                getattr(execution, "document", None)
            ):
                # 文案 A（2026-10-14 真机）：动作其实成功、但应用商店版重定向让身份校验必然不一致。
                # 语音给一句可解释的如实说明；完整结构化汇报与错误码仍走 `text`（控制台/审计）。
                target = _spoken_target(
                    prepared.request.handoff, self._applications, self._aliases
                )
                spoken = self._lines.uncertain_identity_line(target)
            return TaskDispatchResult(
                status=status,
                reason_code=execution.reason_code,
                text=report.text,
                plan_text=prepared.plan_text,
                plan_hash=prepared.plan_hash,
                spoken_text=spoken,
            )
        finally:
            self._retire(prepared.orchestrator)

    def cancel(self, prepared: PreparedTask) -> TaskDispatchResult:
        """作废一份冻结计划（超时/用户未被确认/被新请求打断），返回"已取消"文案。"""

        prepared = self._require_outstanding(prepared)
        self._outstanding = None
        self._retire(prepared.orchestrator)
        return TaskDispatchResult(
            status=STATUS_DENIED,
            reason_code=REASON_DENIED,
            text=DENIED_TEXT,
            plan_text=prepared.plan_text,
            plan_hash=prepared.plan_hash,
        )

    @property
    def outstanding(self) -> PreparedTask | None:
        return self._outstanding

    # ---- 内部 ---------------------------------------------------------------

    def _require_outstanding(self, prepared: object) -> PreparedTask:
        if not isinstance(prepared, PreparedTask):
            raise TypeError("prepared 必须是 PreparedTask")
        if prepared is not self._outstanding:
            raise TaskDispatchError(
                REASON_STALE_PREPARATION, "该冻结计划已作废或已被执行，不得再次使用"
            )
        return prepared

    def _retire_outstanding(self) -> None:
        outstanding = self._outstanding
        if outstanding is not None:
            self._outstanding = None
            self._retire(outstanding.orchestrator)

    def _read_answer(self, plan_text: str) -> tuple[str | None, str | None]:
        """返回 `(用户原始回答, 失败短码)`；拿到回答就一定是 `(str, None)`。"""

        try:
            answer = self._confirm_reader(plan_text)
        except TimeoutError:
            return None, REASON_TIMEOUT
        except Exception:  # noqa: BLE001 - 读不到确认 ⇒ 按未确认处理（fail closed）
            return None, REASON_CONFIRM_UNAVAILABLE
        return (answer if type(answer) is str else ""), None

    @staticmethod
    def _retire(orchestrator: TaskFlowOrchestrator) -> None:
        """单任务编排器用后收尾；已经 TERMINAL 的不再 close（close 会抛错）。"""

        try:
            if orchestrator.state is not TaskFlowState.TERMINAL:
                orchestrator.close()
        except Exception:  # noqa: BLE001 - 收尾失败不得改变已产生的事实
            pass


__all__ = [
    "CODE_IDENTITY_MISMATCH",
    "CONFIRM_UNAVAILABLE_TEXT",
    "CONFIRM_MAX_LENGTH",
    "CONFIRM_PREFIX",
    "CONFIRM_REJECT_MARKERS",
    "DENIED_TEXT",
    "PREVIEW_FAILED_TEXT",
    "REASON_AUTHORIZATION_REFUSED",
    "REASON_CONFIRM_UNAVAILABLE",
    "REASON_DENIED",
    "REASON_PREPARED",
    "REASON_PREVIEW_FAILED",
    "REASON_STALE_PREPARATION",
    "REASON_TIMEOUT",
    "REFUSED_TEXT",
    "SPOKEN_PREVIEW_FAILED_TEXT",
    "SPOKEN_UNSUPPORTED_TEXT",
    "STATUS_DENIED",
    "STATUS_EXECUTED",
    "STATUS_PREVIEW_FAILED",
    "STATUS_READY",
    "STATUS_REFUSED",
    "STATUS_STATE_UNCERTAIN",
    "STATUS_UNSUPPORTED",
    "UNSUPPORTED_TEXT_TEMPLATE",
    "PreparedTask",
    "TaskDispatchError",
    "TaskDispatchRequest",
    "TaskDispatchResult",
    "TaskDispatchSession",
    "TaskPreparation",
    "identity_mismatch_unverified",
    "is_confirmation",
    "unsupported_from_handoff",
    "unsupported_notice",
]
