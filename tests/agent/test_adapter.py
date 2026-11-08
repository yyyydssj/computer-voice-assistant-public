from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agent import (
    AgentFailureCategory,
    HarnessAdapter,
    HarnessAdapterConfig,
    HarnessConfigurationError,
)


SAFE_PATCH = """\
- id: session-log-deepseek
  disabled: true
- id: plugin-package-inventory-deepseek
  disabled: true
- id: sandbox
  disabled: true
- id: sandbox-policy
  disabled: true
- id: subprocess
  disabled: true
- id: pty
  disabled: true
- id: terminal-bash
  disabled: true
- id: terminal-pwsh
  disabled: true
- id: session-title
  disabled: true
- id: mcp-resources
  disabled: true
- id: llm-retry
  disabled: true
- id: jobs
  disabled: true
- id: persistent-bash
  disabled: true
- id: persistent-pwsh
  disabled: true
- id: sessions
  disabled: true
"""


class FakeHarness:
    def __init__(self, result: object | None = None, error: BaseException | None = None) -> None:
        self.result = result or SimpleNamespace(
            final_response="学长，我在呢。",
            finish_reason="stop",
            events=[{"type": "turn/end", "data": {"reason": {"kind": "stop"}}}],
            notifications=[],
        )
        self.error = error
        self.run_calls: list[tuple[str, str]] = []
        self.close_calls = 0

    def run(self, text: str, *, session_id: str, on_notification=None) -> object:
        self.run_calls.append((text, session_id))
        if on_notification is not None:
            for notification in getattr(self.result, "notifications", []):
                on_notification(notification)
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.close_calls += 1


class TransportClosedError(Exception):
    pass


class JsonRpcError(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HarnessAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.patch_path = self.root / "p3-no-tools.patch.yml"
        self.patch_path.write_text(SAFE_PATCH, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_config(self, **changes: object) -> HarnessAdapterConfig:
        values: dict[str, object] = {
            "workspace": self.root.resolve(),
            "dsh_home": (self.root / "dsh-home").resolve(),
            "safety_patch": self.patch_path.resolve(),
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "system_prompt": "你是小饼；不得调用任何电脑工具。",
            "initialize_timeout_seconds": 30.0,
            "request_timeout_seconds": 120.0,
        }
        values.update(changes)
        return HarnessAdapterConfig(**values)

    def test_streaming_text_notifications_are_forwarded_without_reasoning(self) -> None:
        notifications = [
            {"method": "session.event", "payload": {"event": {"type": "assistant/message", "data": {"message": {"stream": {"type": "reasoning", "texts": ["????"]}}}}}},
            {"method": "session.event", "payload": {"event": {"type": "assistant/message", "data": {"message": {"stream": {"type": "text-chunks", "texts": ["???", "?"]}}}}}},
        ]
        harness = FakeHarness(result=SimpleNamespace(
            final_response="????", finish_reason="stop", events=[], notifications=notifications
        ))
        received = []
        adapter = HarnessAdapter(self.make_config(), sdk_factory=lambda _config: harness)
        outcome = adapter.submit_chat(
            request_id="request-stream", session_id="session-stream", text="??",
            on_text_chunk=received.append,
        )
        self.assertTrue(outcome.completed)
        self.assertEqual(received, ["????"])

    def test_constructor_does_not_create_or_start_sdk_runtime(self) -> None:
        factory_calls: list[HarnessAdapterConfig] = []

        def factory(config: HarnessAdapterConfig) -> FakeHarness:
            factory_calls.append(config)
            return FakeHarness()

        adapter = HarnessAdapter(self.make_config(), sdk_factory=factory)
        self.assertEqual(factory_calls, [])
        self.assertFalse(adapter.runtime_created)
        self.assertEqual(adapter.session_ids, frozenset())

    def test_first_request_lazily_creates_runtime_and_session_then_reuses_runtime(self) -> None:
        harness = FakeHarness()
        factory_calls: list[HarnessAdapterConfig] = []

        def factory(config: HarnessAdapterConfig) -> FakeHarness:
            factory_calls.append(config)
            return harness

        adapter = HarnessAdapter(self.make_config(), sdk_factory=factory)
        first = adapter.submit_chat(
            request_id="request-1",
            session_id="chat-session-1",
            text="在吗",
        )
        second = adapter.submit_chat(
            request_id="request-2",
            session_id="chat-session-1",
            text="接着聊",
        )

        self.assertTrue(first.completed)
        self.assertTrue(second.completed)
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(harness.run_calls, [("在吗", "chat-session-1"), ("接着聊", "chat-session-1")])
        self.assertEqual(adapter.session_ids, frozenset({"chat-session-1"}))

    def test_same_request_id_is_never_submitted_twice(self) -> None:
        harness = FakeHarness()
        adapter = HarnessAdapter(self.make_config(), sdk_factory=lambda _config: harness)
        adapter.submit_chat(request_id="request-1", session_id="chat-session-1", text="第一次")

        with self.assertRaisesRegex(ValueError, "request_id.*提交"):
            adapter.submit_chat(request_id="request-1", session_id="chat-session-1", text="重试")

        self.assertEqual(len(harness.run_calls), 1)

    def test_missing_required_no_tools_rows_rejects_configuration_before_sdk_creation(self) -> None:
        self.patch_path.write_text("- id: persistent-pwsh\n  disabled: true\n", encoding="utf-8")
        calls = 0

        def factory(_config: HarnessAdapterConfig) -> FakeHarness:
            nonlocal calls
            calls += 1
            return FakeHarness()

        with self.assertRaisesRegex(HarnessConfigurationError, "安全补丁"):
            HarnessAdapter(self.make_config(), sdk_factory=factory)
        self.assertEqual(calls, 0)

    def test_empty_or_incomplete_response_is_not_reported_as_completed(self) -> None:
        empty = FakeHarness(result=SimpleNamespace(final_response="", finish_reason="stop", events=[], notifications=[]))
        incomplete = FakeHarness(result=SimpleNamespace(final_response="半句", finish_reason=None, events=[], notifications=[]))

        empty_outcome = HarnessAdapter(self.make_config(), sdk_factory=lambda _config: empty).submit_chat(
            request_id="request-empty", session_id="chat-session-1", text="你好"
        )
        incomplete_outcome = HarnessAdapter(self.make_config(), sdk_factory=lambda _config: incomplete).submit_chat(
            request_id="request-incomplete", session_id="chat-session-2", text="你好"
        )

        self.assertFalse(empty_outcome.completed)
        self.assertEqual(empty_outcome.failure.category, AgentFailureCategory.EMPTY_RESPONSE)
        self.assertFalse(incomplete_outcome.completed)
        self.assertEqual(incomplete_outcome.failure.category, AgentFailureCategory.STREAM_INTERRUPTED)

    def test_timeout_stream_interrupt_protocol_and_rate_limit_are_mapped_without_retry(self) -> None:
        cases = [
            (TimeoutError("request timed out"), AgentFailureCategory.TIMEOUT),
            (TransportClosedError("runtime closed"), AgentFailureCategory.STREAM_INTERRUPTED),
            (ConnectionError("network unavailable"), AgentFailureCategory.CONNECTION),
            (RuntimeError("unknown notification"), AgentFailureCategory.UNKNOWN_EVENT),
            (JsonRpcError(429, "rate limit"), AgentFailureCategory.RATE_LIMITED),
            (JsonRpcError(-32602, "invalid schema"), AgentFailureCategory.SCHEMA_ERROR),
        ]
        for index, (error, expected) in enumerate(cases, start=1):
            with self.subTest(expected=expected):
                harness = FakeHarness(error=error)
                outcome = HarnessAdapter(self.make_config(), sdk_factory=lambda _config, h=harness: h).submit_chat(
                    request_id=f"request-{index}",
                    session_id=f"chat-session-{index}",
                    text="测试",
                )
                self.assertFalse(outcome.completed)
                self.assertEqual(outcome.failure.category, expected)
                self.assertEqual(len(harness.run_calls), 1)

    def test_runtime_initialization_failures_are_mapped_without_retry(self) -> None:
        cases = [
            (TimeoutError("initialization timed out"), AgentFailureCategory.TIMEOUT),
            (ConnectionError("runtime unavailable"), AgentFailureCategory.CONNECTION),
            (JsonRpcError(429, "rate limit"), AgentFailureCategory.RATE_LIMITED),
            (JsonRpcError(-32602, "invalid schema"), AgentFailureCategory.SCHEMA_ERROR),
            (RuntimeError("unknown startup event"), AgentFailureCategory.UNKNOWN_EVENT),
        ]
        for index, (error, expected) in enumerate(cases, start=1):
            with self.subTest(expected=expected):
                factory_calls = 0

                def factory(_config: HarnessAdapterConfig, *, failure=error) -> FakeHarness:
                    nonlocal factory_calls
                    factory_calls += 1
                    raise failure

                adapter = HarnessAdapter(self.make_config(), sdk_factory=factory)
                outcome = adapter.submit_chat(
                    request_id=f"request-init-{index}",
                    session_id=f"chat-session-{index}",
                    text="启动故障测试",
                )

                self.assertFalse(outcome.completed)
                self.assertIsNotNone(outcome.failure)
                self.assertEqual(outcome.failure.category, expected)
                self.assertEqual(outcome.failure.stage, "initialize")
                self.assertEqual(factory_calls, 1)

    def test_harness_configuration_failure_is_not_downgraded_to_agent_failure(self) -> None:
        def factory(_config: HarnessAdapterConfig) -> FakeHarness:
            raise HarnessConfigurationError("invalid runtime configuration")

        adapter = HarnessAdapter(self.make_config(), sdk_factory=factory)
        with self.assertRaisesRegex(HarnessConfigurationError, "invalid runtime configuration"):
            adapter.submit_chat(
                request_id="request-config-failure",
                session_id="chat-session-1",
                text="配置故障测试",
            )

    def test_malformed_runtime_result_is_schema_error(self) -> None:
        malformed = SimpleNamespace(
            final_response="正常文本",
            finish_reason="stop",
            events="not-a-list",
            notifications=[],
        )
        outcome = HarnessAdapter(
            self.make_config(), sdk_factory=lambda _config: FakeHarness(result=malformed)
        ).submit_chat(
            request_id="request-malformed-result",
            session_id="chat-session-1",
            text="结果结构测试",
        )

        self.assertFalse(outcome.completed)
        self.assertIsNotNone(outcome.failure)
        self.assertEqual(outcome.failure.category, AgentFailureCategory.SCHEMA_ERROR)
    def test_process_control_exceptions_are_not_swallowed_as_agent_failures(self) -> None:
        for error in (KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(error=type(error).__name__):
                harness = FakeHarness(error=error)
                adapter = HarnessAdapter(self.make_config(), sdk_factory=lambda _config, h=harness: h)
                with self.assertRaises(type(error)):
                    adapter.submit_chat(
                        request_id=f"request-{type(error).__name__.lower()}",
                        session_id="chat-session-1",
                        text="测试",
                    )
    def test_close_reaps_created_runtime_and_is_idempotent(self) -> None:
        harness = FakeHarness()
        adapter = HarnessAdapter(self.make_config(), sdk_factory=lambda _config: harness)
        adapter.close()
        adapter.submit_chat(request_id="request-1", session_id="chat-session-1", text="你好")
        adapter.close()
        adapter.close()
        self.assertEqual(harness.close_calls, 1)


if __name__ == "__main__":
    unittest.main()