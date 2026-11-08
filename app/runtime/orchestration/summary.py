"""P8 批次 4：结构化结果到语音/文字摘要的确定性映射。

摘要只能由已冻结的结构化事实生成：结果状态、动作数量与分类、稳定错误码。
禁止把错误正文、路径、载荷或模型输出放进摘要，也不得用摘要改写任务事实。
"""

from __future__ import annotations

from collections import Counter

from app.plans import ResultDocument, ReviewableActionPlan

_HEADLINES = {
    "completed": "任务执行完成",
    "failed": "任务执行失败",
    "partially_completed": "任务部分完成",
    "state_uncertain": "任务结果不确定",
}

_OUTCOME_LABELS = (
    ("succeeded", "成功"),
    ("failed", "失败"),
    ("skipped", "跳过"),
    ("state_uncertain", "状态不确定"),
)


def summarize_result_document(document: ResultDocument) -> str:
    """把 ``result.json`` 的结构化事实渲染成一句可播报摘要。"""

    if not isinstance(document, ResultDocument):
        raise TypeError("document 必须是 ResultDocument")
    headline = _HEADLINES.get(document.overall_status)
    if headline is None:
        raise ValueError(f"未知的结果总状态: {document.overall_status!r}")
    counts = Counter(action.outcome for action in document.actions)
    segments = [f"{headline}：共 {len(document.actions)} 个动作"]
    for outcome, label in _OUTCOME_LABELS:
        if counts[outcome]:
            segments.append(f"{label} {counts[outcome]}")
    text = "，".join(segments) + "。"
    codes = sorted({action.error_code for action in document.actions if action.error_code})
    if codes:
        text += "错误码：" + "、".join(codes) + "。"
    uncertain_tools = sorted(
        {action.tool_name for action in document.actions if action.outcome == "state_uncertain"}
    )
    if uncertain_tools:
        text += "需要人工确认的操作：" + "、".join(uncertain_tools) + "；系统不会自动重试或回滚。"
    return text


def summarize_plan_for_speech(plan: ReviewableActionPlan) -> str:
    """把冻结计划渲染成授权前的口播摘要；不复述路径与载荷细节。"""

    if not isinstance(plan, ReviewableActionPlan):
        raise TypeError("plan 必须是 ReviewableActionPlan")
    tools = sorted({action.tool_name for action in plan.actions})
    levels = sorted({action.risk_level for action in plan.actions})
    return (
        f"准备执行「{plan.goal}」：共 {len(plan.actions)} 个动作，"
        f"涉及 {'、'.join(tools)}，风险等级 {'/'.join(levels)}。请在方案窗口确认。"
    )


__all__ = ["summarize_plan_for_speech", "summarize_result_document"]
