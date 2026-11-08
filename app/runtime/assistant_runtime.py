"""Default Agent-to-reply runtime seam with a fail-closed local boundary.

The default runtime owns the conversation lifecycle and always routes through a
local chat-only handoff.  When no Agent adapter is explicitly supplied, it
returns a visible unavailable result rather than fabricating an Agent reply or
starting a provider/transport.  A test or authorized caller can inject an
adapter and completed-reply speaker to exercise the full text-first chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.agent.adapter import AgentFailure, AgentFailureCategory, AgentTurnOutcome
from app.contracts.routing import RouteHandoff, RouterStatus
from app.conversation.manager import ConversationManager, ConversationRouteResult
from app.routing import RouteInput
from app.ui.text_shell import unavailable_text_response


class LocalChatRouter:
    """A deterministic, side-effect-free local router for chat-only runtime."""

    def route(self, request: RouteInput) -> RouteHandoff:
        return RouteHandoff(
            trace=request.trace,
            route="chat",
            intent="general_chat",
            handling_mode="chat_reply",
            confidence=1.0,
            slots={},
            required_confirmation="none",
            tool_scope=(),
            router=RouterStatus(
                provider="local",
                model_id="chat-only-router",
                model_version="1",
                status="success",
                failure_code=None,
            ),
        )


class UnavailableAgentAdapter:
    """Safe default adapter: report unavailable, never claim a completed reply."""

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=False,
            text=unavailable_text_response(text),
            failure=AgentFailure(
                category=AgentFailureCategory.CONNECTION,
                stage="default_runtime",
                diagnostic_id="agent-default-unavailable",
            ),
        )

    def close(self) -> None:
        return None


@dataclass(slots=True)
class AssistantRuntime:
    """Own the default conversation seam and its shutdown contract."""

    conversation: object
    completed_reply_speaker: Callable[[AgentTurnOutcome], object] | None = None
    _closed: bool = False

    @classmethod
    def build(
        cls,
        *,
        conversation: object | None = None,
        agent_adapter: object | None = None,
        router: object | None = None,
        router_metadata: tuple[str, str, str] | None = None,
        completed_reply_speaker: Callable[[AgentTurnOutcome], object] | None = None,
        voice_enabled: bool = False,
    ) -> "AssistantRuntime":
        if conversation is not None and agent_adapter is not None:
            raise ValueError("conversation and agent_adapter are mutually exclusive")
        if voice_enabled and conversation is None and agent_adapter is None:
            raise ValueError("voice opt-in requires an injected Agent conversation or adapter")

        provider, model_id, model_version = router_metadata or (
            "local",
            "chat-only-router",
            "1",
        )
        if conversation is None:
            adapter = agent_adapter or UnavailableAgentAdapter()
            conversation = ConversationManager(
                adapter,
                router=router or LocalChatRouter(),
                router_provider=provider,
                router_model_id=model_id,
                router_model_version=model_version,
            )
        return cls(conversation=conversation, completed_reply_speaker=(
            completed_reply_speaker if voice_enabled else None
        ))

    def submit_text(self, text: str) -> ConversationRouteResult:
        return self.conversation.submit_text(text)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.conversation, "close", None)
        if callable(close):
            close()


__all__ = ["AssistantRuntime", "LocalChatRouter", "UnavailableAgentAdapter"]
