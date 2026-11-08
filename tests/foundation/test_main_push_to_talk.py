"""主程序 P8 按键语音入口测试：注入式会话替身，不打开真实设备。"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.adapter import AgentTurnOutcome
from app.conversation.manager import ConversationRouteResult
from app.main import main, run
from app.runtime.voice_entry import NO_SPEECH_REASON, VoiceEntryError, VoiceTurnFacts


def facts(**overrides: object) -> VoiceTurnFacts:
    values: dict[str, object] = {
        "index": 1,
        "boundary": None,
        "frames": 120,
        "speech_seen": True,
        "end_reason": "silence_timeout",
        "accepted": True,
        "submitted": True,
        "spoken": True,
        "reason": "speech_finished",
        "seconds": 3.5,
        "asr_text": "你好，测试一下",
        "reply_text": "回复：你好，测试一下",
    }
    values.update(overrides)
    return VoiceTurnFacts(**values)  # type: ignore[arg-type]


class StubSession:
    def __init__(self, results: list[VoiceTurnFacts] | None = None) -> None:
        self.results = results or [facts()]
        self.turns = 0
        self.closed = 0

    def run_turn(self, *, stop_requested=None) -> VoiceTurnFacts:
        del stop_requested
        self.turns += 1
        result = self.results[min(self.turns, len(self.results)) - 1]
        return result

    def close(self) -> None:
        self.closed += 1


class RecordingConversation:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_text(self, text, *args, **kwargs):
        self.calls.append(text)
        return ConversationRouteResult(
            outcome=AgentTurnOutcome("r", "s", True, "文字回复。", "stop"), handoff=None
        )

    def close(self) -> None:
        pass


class MainPushToTalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root.parent / (self.root.name + "-runtime-data")
        (self.root / ".env").write_text(
            "ASSISTANT_DATA_ROOT=" + str(self.data_root) + "\n", encoding="utf-8"
        )
        self.env = patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(self.data_root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def test_enter_drives_one_voice_turn_and_exit_returns_zero(self) -> None:
        session = StubSession()
        conversation = RecordingConversation()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("\n/exit\n"),
            output_stream=output,
            conversation=conversation,
            push_to_talk=True,
            voice_session_factory=lambda _conversation: session,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(conversation.calls, [])
        text = output.getvalue()
        self.assertIn("语音模式已就绪", text)
        self.assertIn("识别：你好，测试一下", text)
        self.assertIn("回复：回复：你好，测试一下", text)

    def test_agent_not_wired_is_stated_without_faking_a_reply(self) -> None:
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            push_to_talk=True,
            voice_session_factory=lambda _conversation: StubSession(),
        )
        self.assertEqual(code, 0)
        self.assertIn("未接入真实模型", output.getvalue())
        self.assertIn("不会伪造内容", output.getvalue())

    def test_text_command_switches_to_the_text_shell_without_auto_switch_back(self) -> None:
        session = StubSession()
        conversation = RecordingConversation()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/text\n你好\n/exit\n"),
            output_stream=output,
            conversation=conversation,
            push_to_talk=True,
            voice_session_factory=lambda _conversation: session,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 0)
        self.assertEqual(session.closed, 1)
        self.assertEqual(conversation.calls, ["你好"])
        self.assertIn("已切回文字输入（不会自动切回语音）", output.getvalue())

    def test_unavailable_voice_falls_back_to_text_without_switching_provider(self) -> None:
        conversation = RecordingConversation()
        output = io.StringIO()

        def unavailable(_conversation):
            raise VoiceEntryError("没有可用的输入设备")

        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=conversation,
            push_to_talk=True,
            voice_session_factory=unavailable,
        )
        self.assertEqual(code, 0)
        self.assertEqual(conversation.calls, ["你好"])
        text = output.getvalue()
        self.assertIn("语音输入不可用：没有可用的输入设备", text)
        self.assertIn("已按设计降级为文字输入", text)

    def test_injected_factory_never_builds_a_real_environment(self) -> None:
        with patch(
            "app.runtime.voice_entry.build_real_environment",
            side_effect=AssertionError("must not build a real environment"),
        ):
            code = run(
                project_root=self.root,
                input_stream=io.StringIO("/exit\n"),
                output_stream=io.StringIO(),
                conversation=RecordingConversation(),
                push_to_talk=True,
                voice_session_factory=lambda _conversation: StubSession(),
            )
        self.assertEqual(code, 0)

    def test_discarded_silence_turn_is_reported_as_not_submitted(self) -> None:
        session = StubSession(
            [facts(speech_seen=False, accepted=False, submitted=False, spoken=False,
                   reason=NO_SPEECH_REASON, asr_text=None, reply_text=None)]
        )
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("\n/exit\n"),
            output_stream=output,
            conversation=RecordingConversation(),
            push_to_talk=True,
            voice_session_factory=lambda _conversation: session,
        )
        self.assertEqual(code, 0)
        self.assertIn("未检测到语音：本轮已丢弃，未提交、未播报", output.getvalue())

    def test_eof_ends_the_voice_loop_without_turns(self) -> None:
        session = StubSession()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            conversation=RecordingConversation(),
            push_to_talk=True,
            voice_session_factory=lambda _conversation: session,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 0)
        self.assertEqual(session.closed, 1)

    def test_voice_is_off_by_default(self) -> None:
        conversation = RecordingConversation()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=conversation,
        )
        self.assertEqual(code, 0)
        self.assertEqual(conversation.calls, ["你好"])
        self.assertNotIn("语音模式已就绪", output.getvalue())


class MainCliTests(unittest.TestCase):
    def test_invalid_tts_mode_is_rejected_by_argparse(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--tts-mode", "bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_unknown_flag_is_rejected_by_argparse(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--not-a-real-flag"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
