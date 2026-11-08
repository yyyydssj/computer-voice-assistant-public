"""P8 语音闭环测试共享假件：只记录调用，不联网、不触设备。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.agent.adapter import AgentTurnOutcome
from app.conversation.manager import ConversationRouteResult
from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus

FIXED_NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
TIMESTAMP = "2026-10-01T09:00:00.000Z"


def chat_handoff(trace: RouteTrace) -> RouteHandoff:
    return RouteHandoff(
        trace=trace,
        route="chat",
        intent="general_chat",
        handling_mode="chat_reply",
        confidence=1.0,
        slots={},
        required_confirmation="none",
        tool_scope=(),
        router=RouterStatus(
            provider="local",
            model_id="fake-router",
            model_version="1",
            status="success",
            failure_code=None,
        ),
    )


def ambiguous_handoff(trace: RouteTrace) -> RouteHandoff:
    return RouteHandoff(
        trace=trace,
        route="ambiguous",
        intent="low_confidence",
        handling_mode="clarify",
        confidence=0.4,
        slots={},
        required_confirmation="must_clarify",
        tool_scope=(),
        router=RouterStatus(
            provider="local",
            model_id="fake-router",
            model_version="1",
            status="low_confidence",
            failure_code="low_confidence",
        ),
    )


class RecordingConversation:
    """记录统一会话入口调用；不启动 Agent、不联网。"""

    def __init__(self, *, route: str = "chat") -> None:
        self.route = route
        self.calls: list[dict[str, object]] = []

    def _result(
        self, *, trace: RouteTrace, request_id: str, session_id: str
    ) -> ConversationRouteResult:
        handoff = chat_handoff(trace) if self.route == "chat" else ambiguous_handoff(trace)
        outcome = None
        if self.route == "chat":
            outcome = AgentTurnOutcome(
                request_id=request_id,
                session_id=session_id,
                completed=True,
                text="收到。",
                finish_reason="stop",
            )
        return ConversationRouteResult(handoff=handoff, outcome=outcome)

    def submit_text(
        self,
        text: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
        on_text_chunk: object = None,
    ) -> ConversationRouteResult:
        self.calls.append(
            {
                "entry": "submit_text",
                "text": text,
                "request_id": request_id,
                "turn_id": turn_id,
                "on_text_chunk": on_text_chunk,
            }
        )
        trace = RouteTrace(
            request_id=request_id or "text_shell-request-x",
            conversation_id="chat-session-1",
            turn_id=turn_id or "text_shell-turn-x",
            source="text_shell",
            raw_text=text,
            created_at=TIMESTAMP,
        )
        return self._result(
            trace=trace, request_id=trace.request_id, session_id=trace.conversation_id
        )

    def submit_asr_final(
        self,
        text: str,
        *,
        turn_id: str,
        request_id: str | None = None,
        on_text_chunk: object = None,
    ) -> ConversationRouteResult:
        self.calls.append(
            {
                "entry": "submit_asr_final",
                "text": text,
                "request_id": request_id,
                "turn_id": turn_id,
                "on_text_chunk": on_text_chunk,
            }
        )
        trace = RouteTrace(
            request_id=request_id or "asr-request-x",
            conversation_id="chat-session-1",
            turn_id=turn_id,
            source="asr",
            raw_text=text,
            created_at=TIMESTAMP,
        )
        return self._result(
            trace=trace, request_id=trace.request_id, session_id=trace.conversation_id
        )


class FakeStream:
    """记录逐句推送；可注入推送/结束故障。"""

    def __init__(
        self,
        *,
        push_error: Exception | None = None,
        finish_error: Exception | None = None,
    ) -> None:
        self.chunks: list[str] = []
        self.finished = 0
        self.cancelled = 0
        self._push_error = push_error
        self._finish_error = finish_error

    def push_text_chunk(self, text: str) -> None:
        if self._push_error is not None:
            raise self._push_error
        self.chunks.append(text)

    def finish(self) -> None:
        if self._finish_error is not None:
            raise self._finish_error
        self.finished += 1

    def cancel(self) -> None:
        self.cancelled += 1


class FakeSpeaker:
    """只记录 start_stream；可注入启动故障。"""

    def __init__(self, stream: object | None = None, error: Exception | None = None) -> None:
        self.stream = stream
        self.error = error
        self.starts = 0

    def start_stream(self):
        self.starts += 1
        if self.error is not None:
            raise self.error
        return self.stream


__all__ = [
    "FIXED_NOW",
    "TIMESTAMP",
    "FakeSpeaker",
    "FakeStream",
    "RecordingConversation",
    "ambiguous_handoff",
    "chat_handoff",
]
