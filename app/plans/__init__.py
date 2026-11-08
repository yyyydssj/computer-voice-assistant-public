"""P4 ActionPlan 与 result.json 公开入口；只提供不可执行事实与文本编解码。"""
from .action_plan import (
    ACTION_PLAN_FORMAT_VERSION, ACTION_PLAN_SCHEMA_VERSION, ActionPlanError,
    ExecutionPlanReview, PlanAction, ReviewableActionPlan, build_plan_action,
    build_reviewable_plan, parse_execution_plan_markdown, parse_plan_json,
    render_execution_plan_markdown, serialize_plan_json,
)
from .result_document import (
    RESULT_DOCUMENT_FORMAT_VERSION, RESULT_DOCUMENT_SCHEMA_VERSION, ResultAction,
    ResultDocument, ResultDocumentError, build_result_action, build_result_document,
    parse_result_json, serialize_result_json,
)

__all__ = [
    "ACTION_PLAN_FORMAT_VERSION", "ACTION_PLAN_SCHEMA_VERSION", "ActionPlanError",
    "ExecutionPlanReview", "PlanAction", "ReviewableActionPlan", "build_plan_action",
    "build_reviewable_plan", "parse_execution_plan_markdown", "parse_plan_json",
    "render_execution_plan_markdown", "serialize_plan_json",
    "RESULT_DOCUMENT_FORMAT_VERSION", "RESULT_DOCUMENT_SCHEMA_VERSION", "ResultAction",
    "ResultDocument", "ResultDocumentError", "build_result_action", "build_result_document",
    "parse_result_json", "serialize_result_json",
]