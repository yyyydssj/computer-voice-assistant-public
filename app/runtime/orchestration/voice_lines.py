"""P10 · T1 #16：**语音台词出口**（把"说什么"与"确认协议/安全链"解耦）。

真机需求（2026-10-14）：现在播报只是"照着念"，后面要**拟人化**（例如只播一句
「好的，已为你执行。」）。因此把语音台词集中到这一个可注入的出口：

- `plan`（计划征询）/ `reprompt`（重问）/ `result`（执行结果）/ `unsupported` / `preview_failed`；
- 缺省实例 `DEFAULT_VOICE_LINES` 与接入前的固定短句**逐字一致**（行为零变化）；
- 拟人化时用同一个接口注入 persona/LLM 渲染器即可：**状态机、安全链、审计口径都不用改**。

约束（与既有事实口径一致）：台词只用于**语音**；控制台/审计仍看完整文本
（`task_dispatch` 的 `text`），工具名/风险等级只留在那里。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 2026-10-14 真机：旧句 22 字要念 **6.1 s**（实测 3.6 字/秒），用户反馈"说完话好久没反应"；
#: 且旧句承诺的"其它回答取消"与实现**不符**（含糊回答只重问一次、不作废计划，真机 turn 12 复现），
#: 因此缩成 11 字并删掉那条不准确的承诺；"怎么说确认/怎么取消"改由控制台文本给出
#: （`voice_task_turn.CONFIRM_HINT`）。
DEFAULT_PLAN_TEMPLATE = "要打开{target}，说确认。"
DEFAULT_REPROMPT = "还没听到确认。说「确认」执行。"
DEFAULT_RESULT_TEMPLATE = "帮你打开{target}了。"
DEFAULT_UNSUPPORTED = "这条任务本批暂不支持，没有执行。"
DEFAULT_PREVIEW_FAILED = "计划没通过本地校验，没有执行。"
#: 2026-10-14 真机（Win11「打开记事本」）：`System32\notepad.exe` 只是**应用商店版 Notepad 的
#: 启动器**，动作其实成功了，但"进程可执行文件身份与白名单不一致"的校验必然过不去（fail-closed
#: 判 `state_uncertain`）。用户听到旧文案「任务结果不确定…错误码：application_identity_mismatch」
#: 既长又无法理解 ⇒ 语音只念这句可解释的如实说明；完整结构化汇报与错误码仍留在控制台/审计。
DEFAULT_UNCERTAIN_IDENTITY = (
    "已启动{target}，但身份验证没通过（可能是应用商店版本），没有自动重试。"
)


def _render(template: str, target: str, fallback: str) -> str:
    """渲染带 `{target}` 的模板；模板有问题时回落到缺省台词（绝不因文案让链路失败）。"""

    try:
        return template.format(target=target)
    except Exception:  # noqa: BLE001 - 文案模板异常不得影响任务事实
        return fallback.format(target=target)


@dataclass(frozen=True, slots=True)
class VoiceLines:
    """语音台词模板（拟人化的唯一出口）。"""

    plan: str = DEFAULT_PLAN_TEMPLATE
    reprompt: str = DEFAULT_REPROMPT
    result: str = DEFAULT_RESULT_TEMPLATE
    unsupported: str = DEFAULT_UNSUPPORTED
    preview_failed: str = DEFAULT_PREVIEW_FAILED
    uncertain_identity: str = DEFAULT_UNCERTAIN_IDENTITY

    def __post_init__(self) -> None:
        for name in (
            "plan",
            "reprompt",
            "result",
            "unsupported",
            "preview_failed",
            "uncertain_identity",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"VoiceLines.{name} 必须是非空字符串")

    def plan_line(self, target: str) -> str:
        return _render(self.plan, target, DEFAULT_PLAN_TEMPLATE)

    def result_line(self, target: str) -> str:
        return _render(self.result, target, DEFAULT_RESULT_TEMPLATE)

    def uncertain_identity_line(self, target: str) -> str:
        return _render(self.uncertain_identity, target, DEFAULT_UNCERTAIN_IDENTITY)

    def reprompt_line(self) -> str:
        return self.reprompt

    def unsupported_line(self) -> str:
        return self.unsupported

    def preview_failed_line(self) -> str:
        return self.preview_failed


DEFAULT_VOICE_LINES = VoiceLines()

__all__ = [
    "DEFAULT_PLAN_TEMPLATE",
    "DEFAULT_PREVIEW_FAILED",
    "DEFAULT_REPROMPT",
    "DEFAULT_RESULT_TEMPLATE",
    "DEFAULT_UNCERTAIN_IDENTITY",
    "DEFAULT_UNSUPPORTED",
    "DEFAULT_VOICE_LINES",
    "VoiceLines",
]
