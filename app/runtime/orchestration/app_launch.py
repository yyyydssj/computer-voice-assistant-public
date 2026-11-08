"""P10 · T1 批次 1：**白名单应用提议器**（本地确定性映射；绝不采信 LLM 的动作参数）。

职责边界：

- 输入：`RouteHandoff(task/open_application)` 的 `slots.application_name`（不可信文本）；
- 输出：`TaskActionProposal`（**只含** `application_id`，来自本地白名单）；
- 白名单（`ApplicationSpec`：绝对路径 + 可选 SHA-256）由本地注册表提供，LLM 无权指定动作或路径；
- 别名**完全匹配**才命中（NFKC + casefold + 去空白/标点），**不做模糊/编辑距离**，避免误启动；
- 未登记应用、多义别名、缺名、非 `open_application` 意图、危险路由 ⇒ 抛 `AppLaunchError`（固定短码）。

只有 `code` 允许进审计/日志；`message` 是给人看的控制台文案。
"""

from __future__ import annotations

import unicodedata
from types import MappingProxyType
from typing import Any, Mapping

from app.contracts.routing import RouteHandoff
from app.executor import ApplicationSpec
from app.runtime.orchestration.task_flow import TASK_ROUTES, TaskActionProposal

LAUNCH_TOOL_NAME = "start_whitelisted_application"
DEFAULT_ACTION_ID = "app-launch-1"
ALLOWED_INTENTS = frozenset({"open_application"})

CODE_UNKNOWN = "application_unknown"
CODE_AMBIGUOUS = "application_ambiguous"
CODE_NAME_MISSING = "application_name_missing"
CODE_UNSUPPORTED = "task_route_unsupported"

_STRIP = " \t\r\n\u3000，。！？、；：,.!?;:\"'“”‘’（）()【】[]{}<>《》「」『』~～-—_·…"

#: 中文/英文别名 → `application_id`（键会在比较时归一化）
APPLICATION_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "计算器": "calc",
        "计算器应用": "calc",
        "计算機": "calc",
        "calc": "calc",
        "calculator": "calc",
        "记事本": "notepad",
        "记事簿": "notepad",
        "notepad": "notepad",
        "note": "notepad",
    }
)


class AppLaunchError(RuntimeError):
    """白名单解析或路由范围不满足本地硬规则。"""

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or not code:
            raise ValueError("code 必须是非空字符串")
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def default_applications(*, system_root: str = r"C:\Windows") -> Mapping[str, ApplicationSpec]:
    """首批白名单：`calc` / `notepad`（Windows 系统目录下的真实可执行文件）。

    **本模块不得读环境变量**：编排层被硬性禁止导入 `os`/`pathlib`
    （`tests/integration/test_p8_stage_acceptance.py::test_orchestration_layer_has_no_arbitrary_execution_surface`），
    因此 Windows 目录由**装配层（批次 2）** 显式传入；这里的默认值只用于本机默认安装路径。

    只登记**路径**；`executable_hash` 由装配层对真实文件计算并校验 ——
    系统更新导致哈希变化时应 **fail closed**（`application_identity_changed`），这是设计内行为。
    """

    root = system_root.rstrip("\\/")
    base = f"{root}\\System32"
    return {
        "calc": ApplicationSpec(
            application_id="calc",
            application_name="计算器",
            executable_path=f"{base}\\calc.exe",
        ),
        "notepad": ApplicationSpec(
            application_id="notepad",
            application_name="记事本",
            executable_path=f"{base}\\notepad.exe",
        ),
    }


def normalize_application_name(value: str) -> str:
    """归一化应用名/别名：NFKC → casefold → 去空白/标点（用于**完全匹配**）。"""

    if type(value) is not str:
        raise TypeError("value 必须是字符串")
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    return "".join(char for char in text if char not in _STRIP and not char.isspace())


def resolve_application_id(
    name: str,
    *,
    applications: Mapping[str, ApplicationSpec] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """把用户说的应用名解析成白名单 `application_id`；解析不了就**明确拒绝**。"""

    specs = dict(applications) if applications is not None else dict(default_applications())
    key = normalize_application_name(name)
    if not key:
        raise AppLaunchError(CODE_NAME_MISSING, "应用名为空：需要先澄清要打开哪一个应用")
    if key in specs:
        return key
    matched: set[str] = set()
    for alias, application_id in (aliases if aliases is not None else APPLICATION_ALIASES).items():
        if normalize_application_name(alias) == key:
            matched.add(application_id)
    if len(matched) > 1:
        raise AppLaunchError(CODE_AMBIGUOUS, f"应用名有多个候选：{sorted(matched)}")
    if not matched:
        raise AppLaunchError(CODE_UNKNOWN, f"未登记的应用：{name}")
    application_id = matched.pop()
    if application_id not in specs:
        raise AppLaunchError(CODE_UNKNOWN, f"别名指向未登记的白名单项：{application_id}")
    return application_id


def build_launch_proposal(
    handoff: RouteHandoff,
    *,
    applications: Mapping[str, ApplicationSpec] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> TaskActionProposal:
    """`RouteHandoff(task/open_application)` → **单个**启动动作的候选提议。"""

    if not isinstance(handoff, RouteHandoff):
        raise TypeError("handoff 必须是 RouteHandoff")
    if handoff.route not in TASK_ROUTES or handoff.intent not in ALLOWED_INTENTS:
        raise AppLaunchError(
            CODE_UNSUPPORTED,
            f"本批只支持 open_application（route={handoff.route!r}, intent={handoff.intent!r} 保持 fail closed）",
        )
    candidate: Any = handoff.slots.get("application_name")
    value = getattr(candidate, "value", None)
    if type(value) is not str or not value.strip():
        raise AppLaunchError(CODE_NAME_MISSING, "路线里没有可用的应用名（需要先澄清）")
    application_id = resolve_application_id(value, applications=applications, aliases=aliases)
    return TaskActionProposal(
        action_id=DEFAULT_ACTION_ID,
        tool_name=LAUNCH_TOOL_NAME,
        arguments={"application_id": application_id},
        order=1,
    )


def describe_target(
    application_id: str, *, applications: Mapping[str, ApplicationSpec] | None = None
) -> str:
    """确认提示里显示的**确切目标**（应用名 + 绝对路径 + 哈希前 12 位）。"""

    specs = dict(applications) if applications is not None else dict(default_applications())
    spec = specs.get(application_id)
    if spec is None:
        raise AppLaunchError(CODE_UNKNOWN, f"未登记的应用：{application_id}")
    digest = spec.executable_hash
    tail = f"{digest[:19]}…" if isinstance(digest, str) and digest.startswith("sha256:") else "哈希未登记"
    return f"{spec.application_name}（{spec.executable_path}；{tail}）"


def display_name(
    application_id: str, *, applications: Mapping[str, ApplicationSpec] | None = None
) -> str:
    """给**语音播报**用的应用名（只有名字，不带路径/哈希/风险等级）。

    真机反馈（2026-10-13）：口语里念内部工具名和 L2 码既长又难听；播报只念名字，
    路径与哈希仍由 `describe_target()` 提供给控制台与审计。
    """

    specs = dict(applications) if applications is not None else dict(default_applications())
    spec = specs.get(application_id)
    name = getattr(spec, "application_name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return application_id


__all__ = [
    "ALLOWED_INTENTS",
    "APPLICATION_ALIASES",
    "AppLaunchError",
    "CODE_AMBIGUOUS",
    "CODE_NAME_MISSING",
    "CODE_UNKNOWN",
    "CODE_UNSUPPORTED",
    "DEFAULT_ACTION_ID",
    "LAUNCH_TOOL_NAME",
    "build_launch_proposal",
    "default_applications",
    "describe_target",
    "display_name",
    "normalize_application_name",
    "resolve_application_id",
]
