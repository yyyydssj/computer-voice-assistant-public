"""P2 跨模块数据契约公开入口。"""

from .audio import AudioFrame, AudioSession, PlaybackFact, ProviderState, TTSReply, TTSSegment
from .base import ContractError, StrictContract, migrate_legacy_v0
from .execution import (
    ActionAttempt,
    ActionResult,
    AuditEvent,
    ExecutionLedger,
    ResultVerification,
    assess_execution_records,
    seal_audit_event,
    validate_audit_chain,
)
from .planning import ActionPlan, AuthorizationSnapshot, PlanVersion
from .routing import RouteHandoff, RouteTrace, RouterStatus, SlotCandidate
from .session import AgentRequest, AgentResponse, SessionContext, TaskContext, Utterance

__all__ = ["ActionAttempt", "ActionPlan", "ActionResult", "AgentRequest", "AgentResponse", "AudioFrame", "AudioSession", "AuditEvent", "AuthorizationSnapshot", "ContractError", "ExecutionLedger", "PlaybackFact", "PlanVersion", "ProviderState", "ResultVerification", "RouteHandoff", "RouteTrace", "RouterStatus", "SlotCandidate", "SessionContext", "StrictContract", "TaskContext", "TTSReply", "TTSSegment", "Utterance", "assess_execution_records", "migrate_legacy_v0", "seal_audit_event", "validate_audit_chain"]