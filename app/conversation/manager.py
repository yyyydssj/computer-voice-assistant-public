"""P3 批次 2：闲聊与工作会话生命周期管理。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from collections.abc import Iterable
from typing import Callable, Protocol

from app.agent.adapter import (
    AgentFailure,
    AgentFailureCategory,
    AgentTurnOutcome,
)
from app.contracts.base import ContractError, require_identifier, require_non_empty
from app.contracts.routing import RouteHandoff, RouteTrace
from app.routing import IntentRouter, RouteInput, normalize_router_failure


class ConversationError(RuntimeError):
    """会话不存在、已关闭或管理器已关闭。"""


@dataclass(frozen=True, slots=True)
class AgentStreamChunk:
    """一次流式输出片段；finish_reason 只在终止片段出现。"""

    text: str = ""
    finish_reason: str | None = None
    failure: AgentFailure | None = None


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """已确认完整、可进入正式历史的一轮对话。"""

    request_id: str
    user_text: str
    assistant_text: str


@dataclass(frozen=True, slots=True)
class ConversationRouteResult:
    """一个入口轮次的已校验路由交接及可选 Agent 结果。"""

    handoff: RouteHandoff
    outcome: AgentTurnOutcome | None = None


class ConversationStatus(str, Enum):
    OPEN = "open"
    EXPIRED = "expired"
    CLOSED = "closed"


@dataclass(slots=True)
class Conversation:
    """只保存本地生命周期元数据；上下文由 Harness 以 session_id 隔离。"""

    session_id: str
    kind: str
    created_at: datetime
    last_activity_at: datetime
    task_id: str | None = None
    status: ConversationStatus = ConversationStatus.OPEN
    close_reason: str | None = None
    history: list[ConversationTurn] = field(default_factory=list)

    def touch(self, at: datetime) -> None:
        self.last_activity_at = at


class ConversationAdapter(Protocol):
    def submit_chat(
        self, *, request_id: str, session_id: str, text: str
    ) -> AgentTurnOutcome | Iterable[AgentStreamChunk]: ...

    def close(self) -> None: ...


Clock = Callable[[], datetime]


class ConversationManager:
    """维护一个闲聊会话和按 task_id 隔离的工作会话占位。"""

    CHAT_IDLE_TIMEOUT = timedelta(minutes=30)

    def __init__(
        self,
        adapter: ConversationAdapter,
        *,
        now: Clock | None = None,
        router: IntentRouter | None = None,
        router_provider: str = "unconfigured-router",
        router_model_id: str = "intent-router",
        router_model_version: str = "1",
    ) -> None:
        self._adapter = adapter
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._router = router
        self._router_provider = router_provider
        self._router_model_id = router_model_id
        self._router_model_version = router_model_version
        self._chat: Conversation | None = None
        self._work: dict[str, Conversation] = {}
        self._route_results: dict[str, ConversationRouteResult] = {}
        self._closed = False

    @property
    def routing_enabled(self) -> bool:
        return self._router is not None

    @property
    def route_results(self) -> tuple[ConversationRouteResult, ...]:
        return tuple(self._route_results.values())

    @property
    def chat(self) -> Conversation | None:
        return self._chat

    @property
    def work_sessions(self) -> tuple[Conversation, ...]:
        return tuple(self._work.values())

    def get_or_create_chat(self) -> Conversation:
        self._ensure_open()
        current = self._current_time()
        self._expire_idle_chat_at(current)
        if self._chat is None or self._chat.status is ConversationStatus.EXPIRED:
            self._chat = Conversation(
                session_id=self._new_session_id("chat"),
                kind="chat",
                created_at=current,
                last_activity_at=current,
            )
        elif self._chat.status is not ConversationStatus.OPEN:
            raise ConversationError("闲聊会话已关闭")
        return self._chat

    def get_or_create_work(self, task_id: str) -> Conversation:
        self._ensure_open()
        require_identifier("task_id", task_id)
        existing = self._work.get(task_id)
        if existing is not None:
            if existing.status is not ConversationStatus.OPEN:
                raise ConversationError(f"工作会话 {task_id} 已关闭")
            return existing
        current = self._current_time()
        conversation = Conversation(
            session_id=self._new_session_id("work"),
            kind="work",
            created_at=current,
            last_activity_at=current,
            task_id=task_id,
        )
        self._work[task_id] = conversation
        return conversation

    def send_chat(self, *, text: str, request_id: str | None = None) -> AgentTurnOutcome:
        conversation = self.get_or_create_chat()
        outcome = self._submit(conversation, request_id=request_id, text=text)
        if outcome.completed:
            conversation.touch(self._current_time())
            self._record_turn(conversation, user_text=text, outcome=outcome)
        return outcome

    def send_work(
        self, *, task_id: str, text: str, request_id: str | None = None
    ) -> AgentTurnOutcome:
        conversation = self.get_or_create_work(task_id)
        outcome = self._submit(conversation, request_id=request_id, text=text)
        conversation.touch(self._current_time())
        if outcome.completed:
            self._record_turn(conversation, user_text=text, outcome=outcome)
        else:
            conversation.status = ConversationStatus.CLOSED
            conversation.close_reason = "agent_failure"
        return outcome

    def submit_text(
        self,
        text: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ConversationRouteResult:
        """统一接收 TextShell 文本，并在 Agent 前建立 RouteHandoff。"""

        return self._submit_routed(
            text=text,
            source="text_shell",
            request_id=request_id,
            turn_id=turn_id,
            on_text_chunk=on_text_chunk,
        )

    def submit_asr_final(
        self,
        text: str,
        *,
        turn_id: str,
        request_id: str | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ConversationRouteResult:
        """只接收已通过 ResultGate 的 ASR accepted final。"""

        return self._submit_routed(
            text=text,
            source="asr",
            request_id=request_id,
            turn_id=turn_id,
            on_text_chunk=on_text_chunk,
        )

    def expire_idle_chat(self, *, at: datetime | None = None) -> bool:
        self._ensure_open()
        current = self._coerce_time(at) if at is not None else self._current_time()
        return self._expire_idle_chat_at(current)

    def complete_work(self, task_id: str) -> None:
        self._close_work(task_id, reason="completed")

    def cancel_work(self, task_id: str) -> None:
        self._close_work(task_id, reason="cancelled")

    def abort_work(self, task_id: str) -> None:
        self._close_work(task_id, reason="aborted")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._chat is not None and self._chat.status is ConversationStatus.OPEN:
            self._chat.status = ConversationStatus.CLOSED
            self._chat.close_reason = "manager_closed"
        for conversation in self._work.values():
            if conversation.status is ConversationStatus.OPEN:
                conversation.status = ConversationStatus.CLOSED
                conversation.close_reason = "manager_closed"
        self._adapter.close()

    def _submit(
        self, conversation: Conversation, *, request_id: str | None, text: str,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> AgentTurnOutcome:
        if request_id is None:
            request_id = self._new_request_id(conversation.kind)
        require_identifier("request_id", request_id)
        require_non_empty("text", text, maximum=32768)
        if conversation.status is not ConversationStatus.OPEN:
            raise ConversationError("会话已关闭")
        if on_text_chunk is None:
            raw_outcome = self._adapter.submit_chat(
                request_id=request_id,
                session_id=conversation.session_id,
                text=text,
            )
        else:
            try:
                raw_outcome = self._adapter.submit_chat(
                    request_id=request_id,
                    session_id=conversation.session_id,
                    text=text,
                    on_text_chunk=on_text_chunk,
                )
            except TypeError as exc:
                # Existing adapters/test doubles remain valid until they opt in.
                if "on_text_chunk" not in str(exc):
                    raise
                raw_outcome = self._adapter.submit_chat(
                    request_id=request_id,
                    session_id=conversation.session_id,
                    text=text,
                )
        return self._normalize_outcome(
            raw_outcome,
            request_id=request_id,
            session_id=conversation.session_id,
        )

    def _submit_routed(
        self,
        *,
        text: str,
        source: str,
        request_id: str | None,
        turn_id: str | None,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> ConversationRouteResult:
        self._ensure_open()
        if self._router is None:
            raise ConversationError("IntentRouter 尚未配置")
        if source not in {"text_shell", "asr"}:
            raise ConversationError("不支持的路由输入来源")
        require_non_empty("text", text, maximum=32768)
        if source == "asr":
            require_identifier("turn_id", turn_id)

        conversation = self.get_or_create_chat()
        if turn_id is not None:
            existing = self._route_results.get(turn_id)
            if existing is not None:
                if existing.handoff.trace.source != source or existing.handoff.trace.raw_text != text:
                    raise ConversationError("同一 turn_id 不得替换已生效的路由交接")
                if request_id is not None and existing.handoff.trace.request_id != request_id:
                    raise ConversationError("同一 turn_id 不得替换 request_id")
                return existing
        if turn_id is None:
            turn_id = self._new_turn_id(source)
        if request_id is None:
            request_id = self._new_request_id(source)
        require_identifier("request_id", request_id)

        trace = RouteTrace(
            request_id=request_id,
            conversation_id=conversation.session_id,
            turn_id=turn_id,
            source=source,
            raw_text=text,
            created_at=self._timestamp(self._current_time()),
        )
        route_input = RouteInput(raw_text=text, trace=trace)
        handoff = self._route(route_input)
        result = ConversationRouteResult(handoff=handoff)
        if handoff.route == "ambiguous":
            result = ConversationRouteResult(handoff=handoff)
            self._route_results[turn_id] = result
            return result
        if handoff.route == "chat":
            outcome = self._submit(
                conversation, request_id=request_id, text=text,
                on_text_chunk=on_text_chunk,
            )
            if outcome.completed:
                conversation.touch(self._current_time())
                self._record_turn(conversation, user_text=text, outcome=outcome)
            result = ConversationRouteResult(handoff=handoff, outcome=outcome)
        if turn_id in self._route_results:
            raise ConversationError("同一 turn_id 的路由交接已存在")
        self._route_results[turn_id] = result
        return result

    def _route(self, request: RouteInput) -> RouteHandoff:
        assert self._router is not None
        try:
            handoff = self._router.route(request)
        except TimeoutError:
            return normalize_router_failure(
                request.trace,
                provider=self._router_provider,
                model_id=self._router_model_id,
                model_version=self._router_model_version,
                status="timeout",
            )
        except (ConnectionError, OSError):
            return normalize_router_failure(
                request.trace,
                provider=self._router_provider,
                model_id=self._router_model_id,
                model_version=self._router_model_version,
                status="transport_error",
            )
        except Exception:
            return normalize_router_failure(
                request.trace,
                provider=self._router_provider,
                model_id=self._router_model_id,
                model_version=self._router_model_version,
                status="invalid_response",
            )
        if not isinstance(handoff, RouteHandoff) or handoff.trace is not request.trace:
            return normalize_router_failure(
                request.trace,
                provider=self._router_provider,
                model_id=self._router_model_id,
                model_version=self._router_model_version,
                status="invalid_response",
            )
        return handoff

    @staticmethod
    def _normalize_outcome(
        raw_outcome: AgentTurnOutcome | Iterable[AgentStreamChunk],
        *,
        request_id: str,
        session_id: str,
    ) -> AgentTurnOutcome:
        if isinstance(raw_outcome, AgentTurnOutcome):
            if (
                raw_outcome.request_id != request_id
                or raw_outcome.session_id != session_id
                or (raw_outcome.completed and raw_outcome.failure is not None)
                or (not raw_outcome.completed and raw_outcome.failure is None)
            ):
                return ConversationManager._incomplete_outcome(
                    request_id=request_id,
                    session_id=session_id,
                    category=AgentFailureCategory.SCHEMA_ERROR,
                )
            if raw_outcome.completed and (
                not raw_outcome.text.strip()
                or not isinstance(raw_outcome.finish_reason, str)
                or not raw_outcome.finish_reason.strip()
            ):
                return ConversationManager._incomplete_outcome(
                    request_id=request_id,
                    session_id=session_id,
                    text=raw_outcome.text,
                    category=(
                        AgentFailureCategory.EMPTY_RESPONSE
                        if not raw_outcome.text.strip()
                        else AgentFailureCategory.STREAM_INTERRUPTED
                    ),
                )
            return raw_outcome

        parts: list[str] = []
        finish_reason: str | None = None
        failure: AgentFailure | None = None
        try:
            for chunk in raw_outcome:
                if not isinstance(chunk, AgentStreamChunk):
                    return ConversationManager._incomplete_outcome(
                        request_id=request_id,
                        session_id=session_id,
                        text="".join(parts),
                        category=AgentFailureCategory.SCHEMA_ERROR,
                    )
                parts.append(chunk.text)
                if chunk.finish_reason is not None:
                    finish_reason = chunk.finish_reason
                if chunk.failure is not None:
                    failure = chunk.failure
        except Exception:
            return ConversationManager._incomplete_outcome(
                request_id=request_id,
                session_id=session_id,
                text="".join(parts),
                category=AgentFailureCategory.STREAM_INTERRUPTED,
            )

        text = "".join(parts)
        if failure is not None:
            return AgentTurnOutcome(
                request_id=request_id,
                session_id=session_id,
                completed=False,
                text=text,
                finish_reason=finish_reason,
                failure=failure,
            )
        if not text.strip():
            return ConversationManager._incomplete_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.EMPTY_RESPONSE,
            )
        if not isinstance(finish_reason, str) or not finish_reason.strip():
            return ConversationManager._incomplete_outcome(
                request_id=request_id,
                session_id=session_id,
                text=text,
                category=AgentFailureCategory.STREAM_INTERRUPTED,
            )
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=text,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _incomplete_outcome(
        *,
        request_id: str,
        session_id: str,
        category: AgentFailureCategory,
        text: str = "",
    ) -> AgentTurnOutcome:
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=False,
            text=text,
            failure=AgentFailure(
                category=category,
                stage="conversation",
                diagnostic_id=f"conversation-{uuid.uuid4().hex}",
            ),
        )

    @staticmethod
    def _record_turn(
        conversation: Conversation, *, user_text: str, outcome: AgentTurnOutcome
    ) -> None:
        if not outcome.completed or not outcome.text.strip():
            return
        conversation.history.append(
            ConversationTurn(
                request_id=outcome.request_id,
                user_text=user_text,
                assistant_text=outcome.text,
            )
        )

    def _expire_idle_chat_at(self, current: datetime) -> bool:
        if self._chat is None or self._chat.status is not ConversationStatus.OPEN:
            return False
        if current - self._chat.last_activity_at < self.CHAT_IDLE_TIMEOUT:
            return False
        self._chat.status = ConversationStatus.EXPIRED
        self._chat.close_reason = "idle_timeout"
        self._chat = None
        return True

    def _close_work(self, task_id: str, *, reason: str) -> None:
        self._ensure_open()
        require_identifier("task_id", task_id)
        conversation = self._work.get(task_id)
        if conversation is None:
            raise ConversationError(f"不存在工作会话 {task_id}")
        if conversation.status is not ConversationStatus.OPEN:
            raise ConversationError(f"工作会话 {task_id} 已关闭")
        conversation.status = ConversationStatus.CLOSED
        conversation.close_reason = reason

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConversationError("管理器已关闭")

    def _current_time(self) -> datetime:
        return self._coerce_time(self._now())

    @staticmethod
    def _coerce_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("会话时间必须包含时区")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _new_session_id(kind: str) -> str:
        return f"{kind}-session-{uuid.uuid4().hex}"
    @staticmethod
    def _new_request_id(kind: str) -> str:
        return f"{kind}-request-{uuid.uuid4().hex}"

    @staticmethod
    def _new_turn_id(source: str) -> str:
        return f"{source}-turn-{uuid.uuid4().hex}"

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
