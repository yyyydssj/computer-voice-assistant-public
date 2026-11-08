"""计划版本和授权事实契约。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from .base import ContractError, StrictContract, parse_utc_timestamp, require_choice, require_identifier, require_non_empty, require_optional_timestamp, require_positive, require_unique_identifiers


@dataclass(frozen=True, slots=True)
class ActionPlan(StrictContract):
    plan_id: str
    task_id: str
    current_version: int
    current_plan_hash: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("plan_id", self.plan_id)
        require_identifier("task_id", self.task_id)
        require_positive("current_version", self.current_version)
        require_non_empty("current_plan_hash", self.current_plan_hash, maximum=256)
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class PlanVersion(StrictContract):
    plan_id: str
    version: int
    plan_hash: str
    action_ids: tuple[str, ...]
    target_state_hash: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("plan_id", self.plan_id)
        require_positive("version", self.version)
        require_non_empty("plan_hash", self.plan_hash, maximum=256)
        require_unique_identifiers("action_ids", self.action_ids)
        require_non_empty("target_state_hash", self.target_state_hash, maximum=256)
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class AuthorizationSnapshot(StrictContract):
    authorization_id: str
    issuer_instance_id: str
    session_id: str
    operation_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    target_state_hash: str
    allowed_action_ids: tuple[str, ...]
    risk_level: str
    confirmation_window_id: str
    authorization_method: str
    confirmation_stage: str
    confirmation_phrase: str | None
    created_at: str
    expires_at: str
    consumed_at: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("authorization_id", self.authorization_id), ("issuer_instance_id", self.issuer_instance_id),
                            ("session_id", self.session_id), ("operation_id", self.operation_id),
                            ("plan_id", self.plan_id), ("confirmation_window_id", self.confirmation_window_id)):
            require_identifier(name, value)
        require_positive("plan_version", self.plan_version)
        require_non_empty("plan_hash", self.plan_hash, maximum=256)
        require_non_empty("target_state_hash", self.target_state_hash, maximum=256)
        require_unique_identifiers("allowed_action_ids", self.allowed_action_ids)
        require_choice("AuthorizationSnapshot.risk_level", self.risk_level, {"L1", "L2", "L3"})
        require_choice("AuthorizationSnapshot.authorization_method", self.authorization_method, {"none", "modal", "modal_and_phrase"})
        require_choice("AuthorizationSnapshot.confirmation_stage", self.confirmation_stage, {"none", "modal", "phrase"})
        if self.risk_level == "L3":
            if (self.authorization_method, self.confirmation_stage, self.confirmation_phrase) != ("modal_and_phrase", "phrase", "确认执行"):
                raise ContractError("L3 授权必须完成模态确认和固定短语确认")
        elif self.risk_level == "L2":
            if (self.authorization_method, self.confirmation_stage, self.confirmation_phrase) != ("modal", "modal", None):
                raise ContractError("L2 授权必须来自模态确认且不得携带确认短语")
        elif self.confirmation_phrase is not None:
            raise ContractError("非 L3 授权不得携带确认短语")
        created = parse_utc_timestamp("created_at", self.created_at)
        expires = parse_utc_timestamp("expires_at", self.expires_at)
        require_optional_timestamp("consumed_at", self.consumed_at)
        consumed = parse_utc_timestamp("consumed_at", self.consumed_at) if self.consumed_at is not None else None
        if expires <= created:
            raise ContractError("expires_at 必须晚于 created_at")
        if consumed is not None and consumed < created:
            raise ContractError("consumed_at 不得早于 created_at")
        if consumed is not None and consumed >= expires:
            raise ContractError("授权已过期，不能消费")

    def stable_hash(self) -> str:
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def is_valid_for(self, *, issuer_instance_id: str, session_id: str, operation_id: str, plan_version: int,
                     plan_hash: str, target_state_hash: str, action_ids: tuple[str, ...], now: str,
                     plan_id: str | None = None) -> bool:
        current = parse_utc_timestamp("now", now)
        return (
            self.consumed_at is None
            and issuer_instance_id == self.issuer_instance_id
            and session_id == self.session_id
            and operation_id == self.operation_id
            and (plan_id is None or plan_id == self.plan_id)
            and plan_version == self.plan_version
            and plan_hash == self.plan_hash
            and target_state_hash == self.target_state_hash
            and action_ids == self.allowed_action_ids
            and current < parse_utc_timestamp("expires_at", self.expires_at)
        )

    def consume(self, consumed_at: str) -> "AuthorizationSnapshot":
        if self.consumed_at is not None:
            raise ValueError("授权已经使用")
        consumed = parse_utc_timestamp("consumed_at", consumed_at)
        if consumed < parse_utc_timestamp("created_at", self.created_at):
            raise ContractError("consumed_at 不得早于 created_at")
        if consumed >= parse_utc_timestamp("expires_at", self.expires_at):
            raise ContractError("授权已过期，不能消费")
        return replace(self, consumed_at=consumed_at)
