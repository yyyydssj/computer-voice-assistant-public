"""Text-first completed-reply integration, offline only."""
from __future__ import annotations

import io
from types import SimpleNamespace
import unittest

from app.agent.adapter import AgentFailure, AgentFailureCategory, AgentTurnOutcome
from app.tts.completed_reply import CompletedReplySpeaker
from app.tts.real_playback import PreviewResult
from app.ui import TextShell


def outcome(text="第一句。第二句。", *, request_id="req-1", completed=True, failure=None):
    return AgentTurnOutcome(request_id=request_id, session_id="session-1", completed=completed,
                            text=text, finish_reason="stop" if completed else None, failure=failure)


class CompletedReplyTests(unittest.TestCase):
    def test_text_is_flushed_before_speech_and_same_exact_reply_is_used(self):
        output = io.StringIO()
        observed = []

        class Preview:
            def speak(self, text):
                observed.append((output.getvalue(), text))
                return PreviewResult(text, 2, 2, False)

        speaker = CompletedReplySpeaker(selection_reader=lambda: object(),
                                        preview_factory=lambda selection: Preview())
        shell = TextShell(input_stream=io.StringIO("你好\n/exit\n"), output_stream=output,
                          status_provider=lambda: {"mode": "text", "voice_available": False, "task_state": "idle"})
        shell.run_conversation(conversation=SimpleNamespace(submit_text=lambda text: SimpleNamespace(outcome=outcome())),
                               on_completed_reply=speaker)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][1], "第一句。第二句。")
        self.assertIn("第一句。第二句。\n", observed[0][0])

    def test_failed_absent_and_incomplete_outcomes_do_not_speak(self):
        output = io.StringIO()
        called = []
        responses = iter((None, outcome("部分", completed=False,
                                        failure=AgentFailure(AgentFailureCategory.TIMEOUT, "request", "id")),
                          outcome("完整。")))
        shell = TextShell(input_stream=io.StringIO("一\n二\n三\n/exit\n"), output_stream=output,
                          status_provider=lambda: {"mode": "text", "voice_available": False, "task_state": "idle"})
        shell.run_conversation(conversation=SimpleNamespace(submit_text=lambda text: SimpleNamespace(outcome=next(responses))),
                               on_completed_reply=lambda result: called.append(result.text))
        self.assertEqual(called, ["完整。"])
        self.assertIn("部分", output.getvalue())

    def test_duplicate_reply_is_not_spoken_twice(self):
        calls = []
        speaker = CompletedReplySpeaker(selection_reader=lambda: object(),
                                        preview_factory=lambda selection: SimpleNamespace(speak=lambda text: calls.append(text) or PreviewResult(text, 1, 1, False)))
        self.assertIsNotNone(speaker(outcome()))
        self.assertIsNone(speaker(outcome()))
        self.assertEqual(len(calls), 1)

    def test_mismatched_result_is_never_accepted_or_retried(self):
        calls = []
        speaker = CompletedReplySpeaker(selection_reader=lambda: object(),
                                        preview_factory=lambda selection: SimpleNamespace(speak=lambda text: calls.append(text) or PreviewResult("wrong reply", 1, 1, False)))
        self.assertIsNone(speaker(outcome()))
        self.assertIsNone(speaker(outcome(request_id="req-2")))
        self.assertEqual(calls, ["第一句。第二句。"])
    def test_selection_failure_disables_instance_without_changing_text(self):
        speaker = CompletedReplySpeaker(selection_reader=lambda: (_ for _ in ()).throw(ValueError("invalid")))
        self.assertIsNone(speaker(outcome()))
        self.assertIsNone(speaker(outcome(request_id="req-2")))

    def test_failed_playback_disables_next_reply(self):
        calls = []
        speaker = CompletedReplySpeaker(selection_reader=lambda: object(),
                                        preview_factory=lambda selection: SimpleNamespace(speak=lambda text: calls.append(text) or PreviewResult(text, 1, 0, True, "device_stop_unconfirmed")))
        self.assertTrue(speaker(outcome()).text_fallback)
        self.assertIsNone(speaker(outcome(request_id="req-2")))
        self.assertEqual(len(calls), 1)

    def test_unconfigured_default_has_no_speaker(self):
        output = io.StringIO()
        shell = TextShell(input_stream=io.StringIO("你好\n/exit\n"), output_stream=output,
                          status_provider=lambda: {"mode": "text", "voice_available": False, "task_state": "idle"})
        shell.run_conversation(conversation=SimpleNamespace(submit_text=lambda text: SimpleNamespace(outcome=outcome())))
        self.assertIn("第一句。第二句。", output.getvalue())

    def test_streaming_modes_require_an_explicit_session_factory(self):
        for mode in ("ws_pcm", "auto"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "explicit session_factory"):
                    CompletedReplySpeaker(selection_reader=lambda: object(), voicestudio_mode=mode)

    def test_unknown_mode_lists_every_allowed_value(self):
        with self.assertRaisesRegex(
            ValueError, "legacy, http_wav, ws_pcm or auto"
        ):
            CompletedReplySpeaker(selection_reader=lambda: object(), voicestudio_mode="streaming")

    def test_injected_streaming_factory_is_used_for_the_streaming_mode(self):
        created = []

        def factory(selection):
            created.append(selection)
            return SimpleNamespace(push_text_chunk=lambda text: None,
                                   finish=lambda: (), cancel=lambda: None)

        speaker = CompletedReplySpeaker(selection_reader=lambda: object(),
                                        voicestudio_mode="auto",
                                        session_factory=factory)
        stream = speaker.start_stream()
        self.assertIsNotNone(stream)
        self.assertEqual(len(created), 1)

    def test_legacy_and_http_wav_modes_do_not_require_injection(self):
        for mode in ("legacy", "http_wav"):
            with self.subTest(mode=mode):
                speaker = CompletedReplySpeaker(selection_reader=lambda: object(),
                                                voicestudio_mode=mode)
                self.assertIsNotNone(speaker)