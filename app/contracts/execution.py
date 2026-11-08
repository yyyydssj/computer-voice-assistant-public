"""执行身份、结果验证和审计链契约。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .base import ContractError, StrictContract, parse_utc_timestamp, require_choice, require_identifier, require_non_empty, require_optional_timestamp, require_positive


@dataclass(frozen=True, slots=True)
class ExecutionLedger(StrictContract):
    execution_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    authorization_snapshot_hash: str
    status: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("execution_id", self.execution_id)
        require_identifier("plan_id", self.plan_id)
        require_positive("plan_version", self.plan_version)
        require_non_empty("plan_hash", self.plan_hash, maximum=256)
        require_non_empty("authorization_snapshot_hash", self.authorization_snapshot_hash, maximum=256)
        require_choice("ExecutionLedger.status", self.status, {"CREATED", "RUNNING", "VERIFYING", "COMPLETED", "FAILED", "PARTIALLY_COMPLETED", "CANCELLED", "ABORTED", "UNCERTAIN"})
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class ActionAttempt(StrictContract):
    execution_id: str
    action_id: str
    attempt_id: str
    executor_instance_id: str
    fencing_token: str
    status: str
    started_at: str | None
    finished_at: str | None
    cancellation_requested_at: str | None

    def __post_init__(self) -> None:
        require_identifier("execution_id", self.execution_id)
        require_identifier("action_id", self.action_id)
        require_identifier("attempt_id", self.attempt_id)
        require_identifier("executor_instance_id", self.executor_instance_id)
        require_non_empty("fencing_token", self.fencing_token, maximum=256)
        require_choice("ActionAttempt.status", self.status, {"NOT_STARTED", "STARTED", "RETURNED", "CANCEL_REQUESTED", "CANCELLED", "INTERRUPTED"})
        require_optional_timestamp("started_at", self.started_at)
        require_optional_timestamp("finished_at", self.finished_at)
        require_optional_timestamp("cancellation_requested_at", self.cancellation_requested_at)


_RESULT_STATES = {"completed", "not_completed", "state_uncertain"}


@dataclass(frozen=True, slots=True)
class ActionResult(StrictContract):
    execution_id: str
    action_id: str
    attempt_id: str
    status: str
    error_code: str | None
    completed_at: str

    def __post_init__(self) -> None:
        require_identifier("execution_id", self.execution_id)
        require_identifier("action_id", self.action_id)
        require_identifier("attempt_id", self.attempt_id)
        require_choice("ActionResult.status", self.status, _RESULT_STATES)
        if self.error_code is not None:
            require_identifier("error_code", self.error_code)
        parse_utc_timestamp("completed_at", self.completed_at)


@dataclass(frozen=True, slots=True)
class ResultVerification(StrictContract):
    execution_id: str
    action_id: str
    attempt_id: str
    status: str
    verified_at: str
    verifier: str

    def __post_init__(self) -> None:
        require_identifier("execution_id", self.execution_id)
        require_identifier("action_id", self.action_id)
        require_identifier("attempt_id", self.attempt_id)
        require_choice("ResultVerification.status", self.status, _RESULT_STATES)
        parse_utc_timestamp("verified_at", self.verified_at)
        require_identifier("verifier", self.verifier)


@dataclass(frozen=True, slots=True)
class AuditEvent(StrictContract):
    audit_event_id: str
    event_type: str
    source_instance_id: str
    event_sequence: int
    created_at: str
    subject_id: str
    details: Mapping[str, Any] = field(default_factory=dict)
    previous_event_hash: str | None = None
    event_hash: str | None = None

    def __post_init__(self) -> None:
        require_identifier("audit_event_id", self.audit_event_id)
        require_identifier("event_type", self.event_type)
        require_identifier("source_instance_id", self.source_instance_id)
        require_positive("event_sequence", self.event_sequence)
        parse_utc_timestamp("created_at", self.created_at)
        require_identifier("subject_id", self.subject_id)
        if not isinstance(self.details, Mapping):
            raise ContractError("AuditEvent.details 必须是对象")
        object.__setattr__(self, "details", dict(self.details))
        if self.previous_event_hash is not None:
            require_non_empty("previous_event_hash", self.previous_event_hash, maximum=256)
        if self.event_hash is not None:
            require_non_empty("event_hash", self.event_hash, maximum=256)

def assess_execution_records(
    ledger: ExecutionLedger,
    *,
    attempts: tuple[ActionAttempt, ...],
    results: tuple[ActionResult, ...],
    current_fencing_token: str,
) -> str:
    """校验账本记录身份；只分类事实，不恢复、重试或执行动作。"""

    require_non_empty("current_fencing_token", current_fencing_token, maximum=256)
    attempt_identities: dict[str, tuple[str, str, str, str]] = {}
    attempts_by_id: dict[str, ActionAttempt] = {}
    for attempt in attempts:
        if attempt.execution_id != ledger.execution_id:
            raise ContractError("attempt_id 与 execution_id 身份冲突")
        identity = (
            attempt.execution_id,
            attempt.action_id,
            attempt.executor_instance_id,
            attempt.fencing_token,
        )
        previous = attempt_identities.get(attempt.attempt_id)
        if previous is not None and previous != identity:
            raise ContractError("attempt_id 身份冲突")
        if attempt.fencing_token != current_fencing_token:
            raise ContractError("检测到旧 fencing token")
        attempt_identities[attempt.attempt_id] = identity
        attempts_by_id[attempt.attempt_id] = attempt

    results_by_attempt: dict[str, ActionResult] = {}
    for result in results:
        attempt = attempts_by_id.get(result.attempt_id)
        if attempt is None or (result.execution_id, result.action_id) != (
            attempt.execution_id,
            attempt.action_id,
        ):
            raise ContractError("结果记录与 attempt_id 身份冲突")
        if result.attempt_id in results_by_attempt:
            raise ContractError("attempt_id 存在重复完成记录")
        results_by_attempt[result.attempt_id] = result

    returned_without_result = [
        attempt.attempt_id
        for attempt in attempts
        if attempt.status == "RETURNED" and attempt.attempt_id not in results_by_attempt
    ]
    if ledger.status == "COMPLETED" and (not results or returned_without_result):
        raise ContractError("账本完成记录缺失")
    if ledger.status == "COMPLETED" and any(
        result.status != "completed" for result in results
    ):
        raise ContractError("账本完成状态与动作结果冲突")

    unresolved = any(
        attempt.status in {"STARTED", "INTERRUPTED", "RETURNED"}
        and attempt.attempt_id not in results_by_attempt
        for attempt in attempts
    )
    if ledger.status == "UNCERTAIN" or unresolved:
        return "state_uncertain"
    return "consistent"


def _audit_event_digest(event: AuditEvent) -> str:
    payload = event.to_dict()
    payload["event_hash"] = None
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def seal_audit_event(
    event: AuditEvent, *, previous_event_hash: str | None
) -> AuditEvent:
    """为不可变审计事件绑定前序哈希并生成确定性事件哈希。"""

    if previous_event_hash is not None:
        require_non_empty("previous_event_hash", previous_event_hash, maximum=256)
    unsealed = replace(
        event,
        previous_event_hash=previous_event_hash,
        event_hash=None,
    )
    return replace(unsealed, event_hash=_audit_event_digest(unsealed))


def validate_audit_chain(events: tuple[AuditEvent, ...]) -> bool:
    """验证内存中的审计事件序列连续性，不执行任何持久化。"""

    previous_hash: str | None = None
    previous_sequence: int | None = None
    for event in events:
        sequence_is_continuous = (
            previous_sequence is None
            or event.event_sequence == previous_sequence + 1
        )
        hash_is_valid = (
            event.previous_event_hash == previous_hash
            and event.event_hash is not None
            and event.event_hash == _audit_event_digest(event)
        )
        if not sequence_is_continuous or not hash_is_valid:
            raise ContractError("审计链断裂")
        previous_hash = event.event_hash
        previous_sequence = event.event_sequence
    return True
