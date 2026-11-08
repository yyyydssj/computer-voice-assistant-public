from __future__ import annotations

import unittest

from app.agent.adapter import AgentFailureCategory
from app.agent.deepseek_chat_agent import DeepSeekChatAgentAdapter
from app.providers.deepseek_chat_transport import (
    DeepSeekChatConfig,
    DeepSeekChatRateLimited,
    DeepSeekChatRequest,
    DeepSeekChatUnavailable,
)


class FakeChatTransport:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.calls: list[tuple[DeepSeekChatRequest, float]] = []

    def send(self, request: DeepSeekChatRequest, *, timeout_seconds: float) -> str:
        self.calls.append((request, timeout_seconds))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome  # type: ignore[return-value]


def ready_config(**changes: object) -> DeepSeekChatConfig:
    values: dict[str, object] = {"enabled": True, "authorized": True}
    values.update(changes)
    return DeepSeekChatConfig(**values)  # type: ignore[arg-type]


def make_adapter(outcome: object, **changes: object):
    transport = FakeChatTransport(outcome)
    adapter = DeepSeekChatAgentAdapter(transport, config=ready_config(**changes))
    return adapter, transport


class DeepSeekChatAgentTests(unittest.TestCase):
    def test_default_adapter_is_not_ready(self) -> None:
        adapter = DeepSeekChatAgentAdapter(FakeChatTransport("hi"))

        self.assertFalse(adapter.provider_ready)
        self.assertFalse(adapter.config.enabled)
        self.assertFalse(adapter.config.authorized)

    def test_authorized_adapter_reports_ready(self) -> None:
        adapter, _ = make_adapter("hi")

        self.assertTrue(adapter.provider_ready)

    def test_successful_turn_returns_completed_text(self) -> None:
        adapter, transport = make_adapter("你好，我是小饼。")

        outcome = adapter.submit_chat(
            request_id="request-1",
            session_id="session-1",
            text="你好",
        )

        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.text, "你好，我是小饼。")
        self.assertEqual(outcome.finish_reason, "stop")
        self.assertIsNone(outcome.failure)
        self.assertEqual(len(transport.calls), 1)
        request, timeout = transport.calls[0]
        self.assertEqual(request.user_text, "你好")
        self.assertEqual(timeout, 30.0)
        self.assertEqual(request.model_id, "deepseek-flash")

    def test_not_ready_adapter_does_not_send_and_reports_config_stage(self) -> None:
        transport = FakeChatTransport("hi")
        adapter = DeepSeekChatAgentAdapter(transport)

        outcome = adapter.submit_chat(
            request_id="request-1",
            session_id="session-1",
            text="你好",
        )

        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.text, "")
        self.assertIsNotNone(outcome.failure)
        self.assertEqual(outcome.failure.category, AgentFailureCategory.CONNECTION)  # type: ignore[union-attr]
        self.assertEqual(outcome.failure.stage, "config")  # type: ignore[union-attr]
        self.assertEqual(transport.calls, [])

    def test_failure_categories_are_mapped(self) -> None:
        cases = (
            (TimeoutError("secret timeout detail"), AgentFailureCategory.TIMEOUT),
            (DeepSeekChatRateLimited("secret rate detail"), AgentFailureCategory.RATE_LIMITED),
            (DeepSeekChatUnavailable("secret unavailable detail"), AgentFailureCategory.CONNECTION),
            (ConnectionError("secret connection detail"), AgentFailureCategory.CONNECTION),
            (ValueError("secret schema detail"), AgentFailureCategory.SCHEMA_ERROR),
            (RuntimeError("secret unknown detail"), AgentFailureCategory.UNKNOWN_EVENT),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                adapter, _ = make_adapter(error)

                outcome = adapter.submit_chat(
                    request_id="request-1",
                    session_id="session-1",
                    text="你好",
                )

                self.assertFalse(outcome.completed)
                self.assertEqual(outcome.text, "")
                self.assertIsNotNone(outcome.failure)
                self.assertEqual(outcome.failure.category, expected)  # type: ignore[union-attr]
                self.assertNotIn("secret", repr(outcome))
                self.assertEqual(outcome.failure.retry_allowed_automatically, False)  # type: ignore[union-attr]

    def test_empty_response_is_reported_as_empty_not_as_answer(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                adapter, _ = make_adapter(value)

                outcome = adapter.submit_chat(
                    request_id="request-1",
                    session_id="session-1",
                    text="你好",
                )

                self.assertFalse(outcome.completed)
                self.assertEqual(outcome.text, "")
                self.assertEqual(  # type: ignore[union-attr]
                    outcome.failure.category,  # type: ignore[union-attr]
                    AgentFailureCategory.EMPTY_RESPONSE,
                )

    def test_same_request_id_cannot_be_submitted_twice(self) -> None:
        adapter, transport = make_adapter("你好")

        adapter.submit_chat(request_id="request-1", session_id="session-1", text="你好")
        with self.assertRaisesRegex(ValueError, "已经提交"):
            adapter.submit_chat(request_id="request-1", session_id="session-1", text="再来一次")

        self.assertEqual(len(transport.calls), 1)

    def test_blank_text_is_rejected_without_any_request(self) -> None:
        adapter, transport = make_adapter("你好")

        with self.assertRaises(ValueError):
            adapter.submit_chat(request_id="request-1", session_id="session-1", text="   ")

        self.assertEqual(transport.calls, [])

    def test_non_streaming_adapter_never_calls_chunk_callback(self) -> None:
        adapter, _ = make_adapter("你好，我是小饼。")
        chunks: list[str] = []

        outcome = adapter.submit_chat(
            request_id="request-1",
            session_id="session-1",
            text="你好",
            on_text_chunk=chunks.append,
        )

        self.assertTrue(outcome.completed)
        self.assertEqual(chunks, [])

    def test_close_makes_later_turns_fail_closed(self) -> None:
        adapter, transport = make_adapter("你好")

        adapter.close()
        outcome = adapter.submit_chat(
            request_id="request-1",
            session_id="session-1",
            text="你好",
        )

        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.failure.stage, "closed")  # type: ignore[union-attr]
        self.assertEqual(transport.calls, [])

    def test_transport_without_send_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            DeepSeekChatAgentAdapter(object())


if __name__ == "__main__":
    unittest.main()
