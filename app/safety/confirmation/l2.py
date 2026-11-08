"""P5 批次 1：L2 模态确认与未消费 AuthorizationSnapshot。

本模块只管理内存中的确认窗口和确认结果。它不消费授权、不执行动作、
不持久化文件，也不把自然语言肯定词解释为授权。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from app.contracts import AuthorizationSnapshot
from app.contracts.base import parse_utc_timestamp, require_identifier
from app.plans import ReviewableActionPlan

from .consumption import AuthorizationConsumptionStore

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_EVENTS = frozenset({"allow", "deny", "close", "escape", "stop", "timeout"})


class ConfirmationError(ValueError):
    """确认请求或事件不满足冻结安全边界。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _HASH_PATTERN.fullmatch(value):
        raise ConfirmationError("invalid_hash", f"{name} 必须是 sha256 哈希")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ConfirmationError("invalid_summary", "确认摘要字段名必须是字符串")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ConfirmationError("invalid_summary", "确认摘要不允许非有限数字")
        return value
    raise ConfirmationError("invalid_summary", "确认摘要只允许 JSON 值")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ConfirmationActionSummary:
    order: int
    action_id: str
    tool_name: str
    risk_level: str
    normalized_arguments: Mapping[str, Any]
    normalized_paths: tuple[str, ...]
    preview: Mapping[str, Any]
    verification_method: str
    rollback_strategy: str

    def __post_init__(self) -> None:
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order <= 0:
            raise ConfirmationError("invalid_summary", "order 必须是正整数")
        for name, value in (("action_id", self.action_id), ("tool_name", self.tool_name)):
            try:
                require_identifier(name, value)
            except (TypeError, ValueError) as exc:
                raise ConfirmationError("invalid_summary", f"{name} 无效") from exc
        if self.risk_level not in {"L2", "L3"}:
            raise ConfirmationError("confirmation_plan_required", "确认摘要只能包含 L2 或 L3 动作")
        if not isinstance(self.normalized_arguments, Mapping) or not isinstance(self.preview, Mapping):
            raise ConfirmationError("invalid_summary", "参数和预览必须是对象")
        if not isinstance(self.normalized_paths, tuple) or any(
            not isinstance(path, str) or not path for path in self.normalized_paths
        ):
            raise ConfirmationError("invalid_summary", "normalized_paths 必须是字符串元组")
        if not isinstance(self.verification_method, str) or not self.verification_method:
            raise ConfirmationError("invalid_summary", "verification_method 不得为空")
        if not isinstance(self.rollback_strategy, str) or not self.rollback_strategy:
            raise ConfirmationError("invalid_summary", "rollback_strategy 不得为空")
        object.__setattr__(self, "normalized_arguments", _freeze(self.normalized_arguments))
        object.__setattr__(self, "preview", _freeze(self.preview))

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action_id": self.action_id,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "normalized_arguments": _thaw(self.normalized_arguments),
            "normalized_paths": list(self.normalized_paths),
            "preview": _thaw(self.preview),
            "verification_method": self.verification_method,
            "rollback_strategy": self.rollback_strategy,
        }


@dataclass(frozen=True, slots=True)
class L2ConfirmationPrompt:
    modal: bool
    allow_label: str
    deny_label: str
    issuer_instance_id: str
    session_id: str
    operation_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    target_state_hash: str
    goal: str
    actions: tuple[ConfirmationActionSummary, ...]

    def __post_init__(self) -> None:
        if self.modal is not True:
            raise ConfirmationError("invalid_prompt", "L2 正式确认必须是模态窗口")
        if (self.allow_label, self.deny_label) != ("允许执行", "拒绝"):
            raise ConfirmationError("invalid_prompt", "L2 按钮文案必须固定")
        for name, value in (("issuer_instance_id", self.issuer_instance_id), ("session_id", self.session_id),
                            ("operation_id", self.operation_id), ("plan_id", self.plan_id)):
            try:
                require_identifier(name, value)
            except (TypeError, ValueError) as exc:
                raise ConfirmationError("invalid_prompt", f"{name} 无效") from exc
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version <= 0:
            raise ConfirmationError("invalid_prompt", "plan_version 必须是正整数")
        _hash("plan_hash", self.plan_hash)
        _hash("target_state_hash", self.target_state_hash)
        if not isinstance(self.goal, str) or not self.goal:
            raise ConfirmationError("invalid_prompt", "goal 不得为空")
        actions = tuple(self.actions)
        if not actions or any(not isinstance(action, ConfirmationActionSummary) for action in actions):
            raise ConfirmationError("invalid_prompt", "actions 必须是非空确认摘要")
        if any(action.risk_level != "L2" for action in actions):
            raise ConfirmationError("l2_plan_required", "L2 提示只能包含 L2 动作")
        object.__setattr__(self, "actions", actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "modal": self.modal,
            "allow_label": self.allow_label,
            "deny_label": self.deny_label,
            "issuer_instance_id": self.issuer_instance_id,
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_hash": self.plan_hash,
            "target_state_hash": self.target_state_hash,
            "goal": self.goal,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class L2ConfirmationWindow:
    authorization_id: str
    confirmation_window_id: str
    issuer_instance_id: str
    session_id: str
    operation_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    target_state_hash: str
    allowed_action_ids: tuple[str, ...]
    created_at: str
    expires_at: str
    prompt: L2ConfirmationPrompt
    status: str = "pending"

    def __post_init__(self) -> None:
        for name, value in (
            ("authorization_id", self.authorization_id),
            ("confirmation_window_id", self.confirmation_window_id),
            ("issuer_instance_id", self.issuer_instance_id),
            ("session_id", self.session_id),
            ("operation_id", self.operation_id),
            ("plan_id", self.plan_id),
        ):
            try:
                require_identifier(name, value)
            except (TypeError, ValueError) as exc:
                raise ConfirmationError("invalid_window", f"{name} 无效") from exc
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version <= 0:
            raise ConfirmationError("invalid_window", "plan_version 必须是正整数")
        _hash("plan_hash", self.plan_hash)
        _hash("target_state_hash", self.target_state_hash)
        if not self.allowed_action_ids or any(not isinstance(value, str) or not value for value in self.allowed_action_ids):
            raise ConfirmationError("invalid_window", "allowed_action_ids 不得为空")
        if len(set(self.allowed_action_ids)) != len(self.allowed_action_ids):
            raise ConfirmationError("invalid_window", "allowed_action_ids 不得重复")
        created = parse_utc_timestamp("created_at", self.created_at)
        expires = parse_utc_timestamp("expires_at", self.expires_at)
        if expires <= created:
            raise ConfirmationError("invalid_time_range", "expires_at 必须晚于 created_at")
        if not isinstance(self.prompt, L2ConfirmationPrompt):
            raise ConfirmationError("invalid_window", "prompt 必须是 L2ConfirmationPrompt")
        if self.status != "pending":
            raise ConfirmationError("invalid_window", "新确认窗口状态必须是 pending")
        prompt_identity = (
            self.prompt.issuer_instance_id,
            self.prompt.session_id,
            self.prompt.operation_id,
            self.prompt.plan_id,
            self.prompt.plan_version,
            self.prompt.plan_hash,
            self.prompt.target_state_hash,
            tuple(action.action_id for action in self.prompt.actions),
        )
        window_identity = (
            self.issuer_instance_id,
            self.session_id,
            self.operation_id,
            self.plan_id,
            self.plan_version,
            self.plan_hash,
            self.target_state_hash,
            self.allowed_action_ids,
        )
        if prompt_identity != window_identity:
            raise ConfirmationError("invalid_window", "窗口身份与确认摘要不一致")


@dataclass(frozen=True, slots=True)
class ConfirmationResolution:
    confirmation_window_id: str
    status: str
    reason_code: str
    occurred_at: str
    authorization: AuthorizationSnapshot | None = None

    def __post_init__(self) -> None:
        try:
            require_identifier("confirmation_window_id", self.confirmation_window_id)
        except (TypeError, ValueError) as exc:
            raise ConfirmationError("invalid_resolution", "confirmation_window_id 无效") from exc
        if self.status not in {"authorized", "awaiting_phrase", "denied", "cancelled", "expired", "invalidated"}:
            raise ConfirmationError("invalid_resolution", "确认结果状态无效")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ConfirmationError("invalid_resolution", "reason_code 不得为空")
        parse_utc_timestamp("occurred_at", self.occurred_at)
        if self.status == "authorized":
            if not isinstance(self.authorization, AuthorizationSnapshot):
                raise ConfirmationError("invalid_resolution", "authorized 必须携带 AuthorizationSnapshot")
        elif self.authorization is not None:
            raise ConfirmationError("invalid_resolution", "非授权结果不得携带 AuthorizationSnapshot")


class L2ConfirmationController:
    """单活动窗口的 L2 确认控制器；不提供消费或执行能力。"""

    def __init__(self, *, instance_id: str, authorization_store: AuthorizationConsumptionStore | None = None) -> None:
        try:
            require_identifier("instance_id", instance_id)
        except (TypeError, ValueError) as exc:
            raise ConfirmationError("invalid_instance", "instance_id 无效") from exc
        if authorization_store is not None and authorization_store.instance_id != instance_id:
            raise ConfirmationError("instance_mismatch", "消费存储实例不匹配")
        self._instance_id = instance_id
        self._authorization_store = authorization_store or AuthorizationConsumptionStore(instance_id=instance_id)
        self._active_window: L2ConfirmationWindow | None = None
        self._used_window_ids: set[str] = set()
        self._used_authorization_ids: set[str] = set()

    @property
    def active_window(self) -> L2ConfirmationWindow | None:
        return self._active_window

    def begin(
        self,
        *,
        plan: ReviewableActionPlan,
        session_id: str,
        operation_id: str,
        authorization_id: str,
        confirmation_window_id: str,
        target_state_hash: str,
        created_at: str,
        expires_at: str,
    ) -> L2ConfirmationWindow:
        if self._active_window is not None:
            raise ConfirmationError("confirmation_already_active", "已有活动确认窗口")
        if confirmation_window_id in self._used_window_ids:
            raise ConfirmationError("confirmation_window_reused", "confirmation_window_id 不得复用")
        if authorization_id in self._used_authorization_ids:
            raise ConfirmationError("authorization_id_reused", "authorization_id 不得复用")
        if not isinstance(plan, ReviewableActionPlan):
            raise TypeError("plan 必须是 ReviewableActionPlan")
        _hash("target_state_hash", target_state_hash)
        self._validate_l2_plan(plan)
        summaries = tuple(
            ConfirmationActionSummary(
                order=action.order,
                action_id=action.action_id,
                tool_name=action.tool_name,
                risk_level=action.risk_level,
                normalized_arguments=action.normalized_arguments,
                normalized_paths=action.policy_decision.normalized_paths,
                preview=action.preview,
                verification_method=action.verification_method,
                rollback_strategy=action.rollback_strategy,
            )
            for action in plan.actions
        )
        prompt = L2ConfirmationPrompt(
            modal=True,
            allow_label="允许执行",
            deny_label="拒绝",
            issuer_instance_id=self._instance_id,
            session_id=session_id,
            operation_id=operation_id,
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=target_state_hash,
            goal=plan.goal,
            actions=summaries,
        )
        window = L2ConfirmationWindow(
            authorization_id=authorization_id,
            confirmation_window_id=confirmation_window_id,
            issuer_instance_id=self._instance_id,
            session_id=session_id,
            operation_id=operation_id,
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=target_state_hash,
            allowed_action_ids=tuple(action.action_id for action in plan.actions),
            created_at=created_at,
            expires_at=expires_at,
            prompt=prompt,
        )
        self._used_window_ids.add(window.confirmation_window_id)
        self._used_authorization_ids.add(window.authorization_id)
        self._active_window = window
        return window

    def resolve(
        self,
        *,
        confirmation_window_id: str,
        session_id: str,
        event: str,
        occurred_at: str,
        current_plan: ReviewableActionPlan | None = None,
        current_target_state_hash: str | None = None,
    ) -> ConfirmationResolution:
        window = self._active_window
        if window is None:
            raise ConfirmationError("no_active_confirmation", "当前没有活动确认窗口")
        if confirmation_window_id != window.confirmation_window_id:
            raise ConfirmationError("confirmation_window_mismatch", "确认事件不属于当前窗口")
        if session_id != window.session_id:
            raise ConfirmationError("session_mismatch", "确认事件不属于当前会话")
        if event not in _ALLOWED_EVENTS:
            raise ConfirmationError("invalid_event", "只接受固定的本地确认事件")
        occurred = parse_utc_timestamp("occurred_at", occurred_at)
        created = parse_utc_timestamp("created_at", window.created_at)
        expires = parse_utc_timestamp("expires_at", window.expires_at)
        if occurred < created:
            raise ConfirmationError("event_before_window", "确认事件早于窗口创建时间")
        if event == "timeout" and occurred < expires:
            raise ConfirmationError("timeout_not_reached", "确认窗口尚未到期")
        if occurred >= expires:
            return self._finish(window, "expired", "confirmation_expired", occurred_at)
        if event == "deny":
            return self._finish(window, "denied", "user_denied", occurred_at)
        if event == "close":
            return self._finish(window, "cancelled", "window_closed", occurred_at)
        if event == "escape":
            return self._finish(window, "cancelled", "escape_pressed", occurred_at)
        if event == "stop":
            return self._finish(window, "cancelled", "user_stopped", occurred_at)
        if event == "timeout":
            raise AssertionError("未到期 timeout 已在前面拒绝")
        if current_plan is None or current_target_state_hash is None:
            raise ConfirmationError("current_facts_required", "允许执行前必须提供当前计划和目标状态事实")
        if not isinstance(current_plan, ReviewableActionPlan):
            raise TypeError("current_plan 必须是 ReviewableActionPlan")
        _hash("current_target_state_hash", current_target_state_hash)
        current_identity = (
            current_plan.plan_id,
            current_plan.plan_version,
            current_plan.plan_hash,
            tuple(action.action_id for action in current_plan.actions),
        )
        expected_identity = (
            window.plan_id,
            window.plan_version,
            window.plan_hash,
            window.allowed_action_ids,
        )
        if current_identity != expected_identity:
            return self._finish(window, "invalidated", "plan_changed", occurred_at)
        try:
            self._validate_l2_plan(current_plan)
        except ConfirmationError:
            return self._finish(window, "invalidated", "plan_changed", occurred_at)
        if current_target_state_hash != window.target_state_hash:
            return self._finish(window, "invalidated", "target_state_changed", occurred_at)
        authorization = AuthorizationSnapshot(
            authorization_id=window.authorization_id,
            issuer_instance_id=window.issuer_instance_id,
            session_id=window.session_id,
            operation_id=window.operation_id,
            plan_id=window.plan_id,
            plan_version=window.plan_version,
            plan_hash=window.plan_hash,
            target_state_hash=window.target_state_hash,
            allowed_action_ids=window.allowed_action_ids,
            risk_level="L2",
            confirmation_window_id=window.confirmation_window_id,
            authorization_method="modal",
            confirmation_stage="modal",
            confirmation_phrase=None,
            created_at=occurred_at,
            expires_at=window.expires_at,
        )
        self._authorization_store.register(authorization)
        return self._finish(window, "authorized", "user_allowed", occurred_at, authorization)

    @staticmethod
    def _validate_l2_plan(plan: ReviewableActionPlan) -> None:
        if any(
            action.risk_level != "L2"
            or action.requires_confirmation is not True
            or action.policy_decision.decision != "require_confirmation"
            for action in plan.actions
        ):
            raise ConfirmationError("l2_plan_required", "L2 模态确认只接受全部动作为 L2 的计划")

    def _finish(
        self,
        window: L2ConfirmationWindow,
        status: str,
        reason_code: str,
        occurred_at: str,
        authorization: AuthorizationSnapshot | None = None,
    ) -> ConfirmationResolution:
        self._active_window = None
        return ConfirmationResolution(
            confirmation_window_id=window.confirmation_window_id,
            status=status,
            reason_code=reason_code,
            occurred_at=occurred_at,
            authorization=authorization,
        )
