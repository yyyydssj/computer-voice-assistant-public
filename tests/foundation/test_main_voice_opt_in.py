from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.adapter import AgentTurnOutcome
from app.main import run
from app.conversation.manager import ConversationRouteResult
from app.agent.adapter import AgentFailure, AgentFailureCategory
from app.runtime.assistant_runtime import AssistantRuntime


class MainVoiceOptInTests(unittest.TestCase):
    def test_voice_requires_explicit_conversation(self):
        with self.assertRaisesRegex(ValueError, "injected Agent"):
            run(voice_enabled=True)

    def test_injected_completed_reply_speaks_only_after_text_flush(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            output = io.StringIO()
            received = []
            outcome = AgentTurnOutcome("req-1", "session-1", True, "完成。", "stop")

            class Conversation:
                def submit_text(self, text):
                    received.append(("input", text))
                    return ConversationRouteResult(outcome=outcome, handoff=None)

            def speaker(result):
                self.assertIn("完成。\n", output.getvalue())
                received.append(("speak", result))

            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("你好\n/exit\n"),
                                     output_stream=output, conversation=Conversation(), voice_enabled=True,
                                     completed_reply_speaker=speaker), 0)
            self.assertEqual(received, [("input", "你好"), ("speak", outcome)])

    def test_speech_callback_failure_does_not_hide_rendered_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            output = io.StringIO()
            class Conversation:
                def submit_text(self, text):
                    return ConversationRouteResult(outcome=AgentTurnOutcome("r", "s", True, "???", "stop"), handoff=None)
            def fail(_):
                raise RuntimeError("device failed")
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("??\n/exit\n"),
                                     output_stream=output, conversation=Conversation(), voice_enabled=True,
                                     completed_reply_speaker=fail), 0)
            self.assertIn("???\n", output.getvalue())

    def test_injected_conversation_does_not_speak_without_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            class Conversation:
                def submit_text(self, text):
                    return ConversationRouteResult(outcome=AgentTurnOutcome("r", "s", True, "文字。", "stop"), handoff=None)
            called = []
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("你好\n/exit\n"),
                                     output_stream=io.StringIO(), conversation=Conversation(),
                                     completed_reply_speaker=called.append), 0)
            self.assertFalse(called)

    def test_default_runtime_is_local_fail_closed_and_closes_manager(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            output = io.StringIO()
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root), "DEEPSEEK_API_KEY": "must-not-be-read"}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("??\n/exit\n"), output_stream=output), 0)
            self.assertIn("Agent", output.getvalue())
            self.assertNotIn("deepseek", output.getvalue().casefold())

    def test_default_chain_accepts_injected_agent_adapter_and_speaker(self):
        events = []
        class Adapter:
            def submit_chat(self, *, request_id, session_id, text):
                events.append(("agent", text))
                return AgentTurnOutcome(request_id, session_id, True, "????", "stop")
            def close(self):
                events.append(("close",))
        def speaker(outcome):
            events.append(("speak", outcome.text))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            output = io.StringIO()
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("??\n/exit\n"), output_stream=output, agent_adapter=Adapter(), voice_enabled=True, completed_reply_speaker=speaker), 0)
        self.assertEqual(events[0], ("agent", "??"))
        self.assertEqual(events[1], ("speak", "????"))
        self.assertEqual(events[-1], ("close",))
        self.assertIn("????\n", output.getvalue())

    def test_default_chain_does_not_speak_failed_agent_outcome(self):
        called = []
        class Adapter:
            def submit_chat(self, *, request_id, session_id, text):
                return AgentTurnOutcome(request_id, session_id, False, "????", None, AgentFailure(AgentFailureCategory.TIMEOUT, "request", "diag"))
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            output = io.StringIO()
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("??\n/exit\n"), output_stream=output, agent_adapter=Adapter(), voice_enabled=True, completed_reply_speaker=called.append), 0)
        self.assertEqual(called, [])
        self.assertIn("????\n", output.getvalue())

    def test_injected_runtime_is_closed_on_exit(self):
        events = []
        class Runtime:
            conversation = type("Conversation", (), {"submit_text": lambda self, text: ConversationRouteResult(outcome=AgentTurnOutcome("r", "s", True, "??", "stop"), handoff=None)})()
            completed_reply_speaker = None
            def close(self):
                events.append("closed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text("ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8")
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(run(project_root=root, input_stream=io.StringIO("/exit\n"), output_stream=io.StringIO(), runtime=Runtime()), 0)
        self.assertEqual(events, ["closed"])



    def test_injected_runtime_voice_opt_in_uses_runtime_speaker_without_building_real_speaker(self):
        events = []
        outcome = AgentTurnOutcome("req-runtime", "session-runtime", True, "runtime回复。", "stop")

        class Conversation:
            def submit_text(self, text):
                events.append(("input", text))
                return ConversationRouteResult(outcome=outcome, handoff=None)

        class Runtime:
            def __init__(self):
                self.conversation = Conversation()
                self.completed_reply_speaker = lambda reply: events.append(("speak", reply.text))
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                events.append(("close",))

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as temp, patch(
            "app.tts.completed_reply.CompletedReplySpeaker"
        ) as real_speaker:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text(
                "ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8"
            )
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(
                    run(
                        project_root=root,
                        input_stream=io.StringIO("你好\n/exit\n"),
                        output_stream=io.StringIO(),
                        runtime=runtime,
                        voice_enabled=True,
                    ),
                    0,
                )

        self.assertEqual(events[:2], [("input", "你好"), ("speak", "runtime回复。")])
        self.assertEqual(events[-1], ("close",))
        self.assertEqual(runtime.close_calls, 1)
        real_speaker.assert_not_called()

    def test_injected_runtime_speaker_is_not_used_when_voice_is_disabled(self):
        events = []
        outcome = AgentTurnOutcome("req-runtime-off", "session-runtime", True, "不应播报。", "stop")

        class Conversation:
            def submit_text(self, text):
                return ConversationRouteResult(outcome=outcome, handoff=None)

        class Runtime:
            conversation = Conversation()
            completed_reply_speaker = lambda self, reply: events.append(reply.text)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text(
                "ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8"
            )
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(
                    run(
                        project_root=root,
                        input_stream=io.StringIO("你好\n/exit\n"),
                        output_stream=io.StringIO(),
                        runtime=Runtime(),
                        voice_enabled=False,
                    ),
                    0,
                )

        self.assertEqual(events, [])

    def test_assistant_runtime_close_is_idempotent_for_injected_conversation(self):
        close_calls = []

        class Conversation:
            def close(self):
                close_calls.append("close")

        runtime = AssistantRuntime(conversation=Conversation())

        runtime.close()
        runtime.close()

        self.assertEqual(close_calls, ["close"])

    def test_runtime_close_exception_is_not_retried(self):
        close_calls = []

        class Conversation:
            def submit_text(self, text):
                return ConversationRouteResult(outcome=None, handoff=None)

        class Runtime:
            conversation = Conversation()
            completed_reply_speaker = None

            def close(self):
                close_calls.append("close")
                raise RuntimeError("runtime close failed")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text(
                "ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8"
            )
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                with self.assertRaisesRegex(RuntimeError, "runtime close failed"):
                    run(
                        project_root=root,
                        input_stream=io.StringIO("/exit\n"),
                        output_stream=io.StringIO(),
                        runtime=Runtime(),
                    )

        self.assertEqual(close_calls, ["close"])

    def test_default_runtime_does_not_construct_agent_sdk_or_tts_device(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text(
                "ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8"
            )
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "ASSISTANT_DATA_ROOT": str(data_root),
                    "DEEPSEEK_API_KEY": "must-not-be-read",
                },
            ), patch("app.agent.adapter.HarnessAdapter") as harness, patch(
                "app.agent.adapter._official_sdk_factory"
            ) as sdk_factory, patch("app.tts.real_playback.RealVoicePreview") as preview:
                self.assertEqual(
                    run(
                        project_root=root,
                        input_stream=io.StringIO("你好\n/exit\n"),
                        output_stream=output,
                    ),
                    0,
                )

        harness.assert_not_called()
        sdk_factory.assert_not_called()
        preview.assert_not_called()
        self.assertNotIn("must-not-be-read", output.getvalue())

    def test_injected_runtime_failed_outcome_is_rendered_but_not_spoken(self):
        called = []
        failed = AgentTurnOutcome(
            "req-failed-runtime",
            "session-runtime",
            False,
            "保留这段失败文字。",
            None,
            AgentFailure(AgentFailureCategory.TIMEOUT, "request", "diag"),
        )

        class Conversation:
            def submit_text(self, text):
                return ConversationRouteResult(outcome=failed, handoff=None)

        class Runtime:
            conversation = Conversation()
            completed_reply_speaker = lambda self, reply: called.append(reply)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root.parent / (root.name + "-runtime-data")
            (root / ".env").write_text(
                "ASSISTANT_DATA_ROOT=" + str(data_root) + "\n", encoding="utf-8"
            )
            output = io.StringIO()
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}):
                self.assertEqual(
                    run(
                        project_root=root,
                        input_stream=io.StringIO("你好\n/exit\n"),
                        output_stream=output,
                        runtime=Runtime(),
                        voice_enabled=True,
                    ),
                    0,
                )

        self.assertEqual(called, [])
        self.assertIn("保留这段失败文字。\n", output.getvalue())

if __name__ == "__main__":
    unittest.main()
