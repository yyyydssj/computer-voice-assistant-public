"""会话、输入和 Agent 边界契约。"""

from __future__ import annotations

from dataclasses import dataclass

from .base import StrictContract, parse_utc_timestamp, require_choice, require_identifier, require_non_empty


@dataclass(frozen=True, slots=True)
class SessionContext(StrictContract):
    session_id: str
    instance_id: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("session_id", self.session_id)
        require_identifier("instance_id", self.instance_id)
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class Utterance(StrictContract):
    utterance_id: str
    session_id: str
    input_mode: str
    text: str
    accepted_at: str

    def __post_init__(self) -> None:
        require_identifier("utterance_id", self.utterance_id)
        require_identifier("session_id", self.session_id)
        require_choice("Utterance.input_mode", self.input_mode, {"text", "voice"})
        require_non_empty("text", self.text, maximum=32768)
        parse_utc_timestamp("accepted_at", self.accepted_at)


@dataclass(frozen=True, slots=True)
class AgentRequest(StrictContract):
    request_id: str
    session_id: str
    utterance_id: str
    submitted_at: str

    def __post_init__(self) -> None:
        require_identifier("request_id", self.request_id)
        require_identifier("session_id", self.session_id)
        require_identifier("utterance_id", self.utterance_id)
        parse_utc_timestamp("submitted_at", self.submitted_at)


@dataclass(frozen=True, slots=True)
class AgentResponse(StrictContract):
    request_id: str
    response_id: str
    status: str
    completed_at: str
    text: str

    def __post_init__(self) -> None:
        require_identifier("request_id", self.request_id)
        require_identifier("response_id", self.response_id)
        require_choice("AgentResponse.status", self.status, {"completed", "failed", "cancelled"})
        parse_utc_timestamp("completed_at", self.completed_at)
        if not isinstance(self.text, str) or len(self.text) > 131072:
            raise ValueError("text 必须是受限长度字符串")


@dataclass(frozen=True, slots=True)
class TaskContext(StrictContract):
    task_id: str
    session_id: str
    request_id: str
    state: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("task_id", self.task_id)
        require_identifier("session_id", self.session_id)
        require_identifier("request_id", self.request_id)
        require_choice("TaskContext.state", self.state, {"IDLE", "UNDERSTANDING", "PLANNING", "WAITING_AUTH", "EXECUTING", "VERIFYING", "COMPLETED", "CANCELLED", "ABORTED", "UNCERTAIN"})
        parse_utc_timestamp("created_at", self.created_at)