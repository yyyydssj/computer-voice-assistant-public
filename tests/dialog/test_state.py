"""P9 批次 1 红测：对话状态机、迁移白名单与脱敏诊断（离线、无副作用）。"""

from __future__ import annotations

import re
import unittest

from app.dialog.state import (
    DIALOG_SCHEMA_VERSION,
    DIALOG_TRANSITION_REASONS,
    DialogDiagnostic,
    DialogError,
    DialogInputMode,
    DialogState,
    DialogStateMachine,
    DialogTransition,
)

REQUIRED_REASON_CODES = frozenset(
    {
        "turn_started",
        "voice_turn_started",
        "text_turn_started",
        "listening_started",
        "speech_started",
        "speech_finished",
        "transcription_ready",
        "route_handled",
        "clarify_required",
        "reply_ready",
        "authorization_required",
        "execution_requested",
        "speech_started_output",
        "speech_finished_output",
        "turn_completed",
        "idle_timeout",
        "window_timeout",
        "cancelled",
        "stopped",
        "input_degraded",
        "audio_failed",
        "asr_failed",
        "agent_failed",
        "provider_unavailable",
        "user_requested_text",
        "recovered",
        "shutdown",
    }
)


def machine(**overrides: object) -> DialogStateMachine:
    values: dict[str, object] = {"source_instance_id": "dialog-instance-1"}
    values.update(overrides)
    return DialogStateMachine(**values)  # type: ignore[arg-type]


def thinking_machine() -> DialogStateMachine:
    item = machine()
    item.begin_text_turn()
    return item


class DialogReasonWhitelistTests(unittest.TestCase):
    def test_schema_version_and_reason_whitelist_are_frozen(self) -> None:
        self.assertEqual(DIALOG_SCHEMA_VERSION, 1)
        self.assertIsInstance(DIALOG_TRANSITION_REASONS, frozenset)
        self.assertTrue(REQUIRED_REASON_CODES <= DIALOG_TRANSITION_REASONS)
        pattern = re.compile(r"^[a-z][a-z0-9_]*$")
        for code in DIALOG_TRANSITION_REASONS:
            with self.subTest(code=code):
                self.assertRegex(code, pattern)

    def test_free_text_and_unknown_reasons_are_refused(self) -> None:
        item = machine()
        for reason in ("user said yes", "not_a_transition_reason", "", "Shutdown", "stopped!"):
            with self.subTest(reason=reason):
                with self.assertRaises(DialogError):
                    item.transition(DialogState.LISTENING, reason=reason)
        self.assertEqual(item.state, DialogState.IDLE)
        self.assertEqual(item.transitions, ())


class DialogStateMachinePathTests(unittest.TestCase):
    def test_voice_path_walks_declared_transitions(self) -> None:
        item = machine()
        self.assertEqual(item.state, DialogState.IDLE)
        self.assertTrue(item.accepts_input)
        item.begin_voice_turn()
        self.assertEqual(item.state, DialogState.USER_SPEAKING)
        self.assertEqual(item.mode, DialogInputMode.VOICE_TURN)
        self.assertFalse(item.accepts_input)
        item.transition(DialogState.TRANSCRIBING, reason="speech_finished")
        item.transition(DialogState.THINKING, reason="transcription_ready")
        item.transition(DialogState.CLARIFYING, reason="clarify_required")
        item.transition(DialogState.LISTENING, reason="listening_started")
        self.assertTrue(item.accepts_input)
        item.transition(DialogState.USER_SPEAKING, reason="speech_started")
        item.transition(DialogState.TRANSCRIBING, reason="speech_finished")
        item.transition(DialogState.IDLE, reason="turn_completed")
        self.assertIsNone(item.mode)
        self.assertTrue(item.accepts_input)
        item.close(reason="shutdown")
        self.assertTrue(item.is_terminal)
        self.assertEqual(len(item.transitions), 9)

    def test_accepts_input_follows_the_frozen_definition(self) -> None:
        item = machine()
        self.assertTrue(item.accepts_input)
        item.transition(DialogState.LISTENING, reason="listening_started")
        self.assertTrue(item.accepts_input)
        item.transition(DialogState.USER_SPEAKING, reason="speech_started")
        self.assertFalse(item.accepts_input)
        item.transition(DialogState.TRANSCRIBING, reason="speech_finished")
        self.assertFalse(item.accepts_input)
        item.transition(DialogState.CLARIFYING, reason="clarify_required")
        self.assertFalse(item.accepts_input)
        item.transition(DialogState.IDLE, reason="turn_completed")
        self.assertTrue(item.accepts_input)
        guarded = thinking_machine()
        guarded.transition(DialogState.AWAITING_AUTHORIZATION, reason="authorization_required")
        self.assertFalse(guarded.accepts_input)
        executing = thinking_machine()
        executing.transition(DialogState.EXECUTING, reason="execution_requested")
        self.assertFalse(executing.accepts_input)
        speaking = thinking_machine()
        speaking.transition(DialogState.SPEAKING, reason="reply_ready")
        self.assertFalse(speaking.accepts_input)
        failed = machine()
        failed.fail_closed(reason="audio_failed")
        self.assertFalse(failed.accepts_input)
        closed = machine()
        closed.close(reason="shutdown")
        self.assertFalse(closed.accepts_input)

    def test_guard_states_are_only_reachable_from_thinking(self) -> None:
        idle = machine()
        with self.assertRaises(DialogError):
            idle.transition(DialogState.AWAITING_AUTHORIZATION, reason="authorization_required")
        listening = machine()
        listening.transition(DialogState.LISTENING, reason="listening_started")
        with self.assertRaises(DialogError):
            listening.transition(DialogState.EXECUTING, reason="execution_requested")
        self.assertEqual(listening.state, DialogState.LISTENING)
        authorized = thinking_machine()
        authorized.transition(DialogState.AWAITING_AUTHORIZATION, reason="authorization_required")
        with self.assertRaises(DialogError):
            authorized.transition(DialogState.EXECUTING, reason="execution_requested")
        self.assertEqual(authorized.state, DialogState.AWAITING_AUTHORIZATION)
        executing = thinking_machine()
        executing.transition(DialogState.EXECUTING, reason="execution_requested")
        with self.assertRaises(DialogError):
            executing.transition(DialogState.SPEAKING, reason="reply_ready")
        self.assertEqual(executing.state, DialogState.EXECUTING)

    def test_illegal_and_self_transitions_fail_closed(self) -> None:
        item = machine()
        with self.assertRaises(DialogError):
            item.transition(DialogState.SPEAKING, reason="reply_ready")
        with self.assertRaises(DialogError):
            item.transition(DialogState.IDLE, reason="turn_completed")
        with self.assertRaises(DialogError):
            item.transition(DialogState.SPEAKING, reason="not_a_transition_reason")
        with self.assertRaises(TypeError):
            item.transition("listening", reason="listening_started")  # type: ignore[arg-type]
        self.assertEqual(item.state, DialogState.IDLE)
        self.assertEqual(item.transitions, ())

    def test_closed_state_is_absorbing(self) -> None:
        item = machine()
        item.close(reason="shutdown")
        self.assertTrue(item.is_terminal)
        self.assertFalse(item.accepts_input)
        for action in (
            lambda: item.transition(DialogState.IDLE, reason="recovered"),
            lambda: item.fail_closed(reason="audio_failed"),
            lambda: item.close(reason="shutdown"),
            lambda: item.recover(reason="recovered"),
            lambda: item.begin_voice_turn(),
            lambda: item.begin_text_turn(),
        ):
            with self.subTest(action=action):
                with self.assertRaises(DialogError):
                    action()
        self.assertEqual(item.state, DialogState.CLOSED)
        self.assertEqual(len(item.transitions), 1)

    def test_begin_helpers_require_idle(self) -> None:
        voice = machine()
        voice.begin_voice_turn()
        self.assertEqual(voice.mode, DialogInputMode.VOICE_TURN)
        with self.assertRaises(DialogError):
            voice.begin_voice_turn()
        with self.assertRaises(DialogError):
            voice.begin_text_turn()
        text = machine()
        text.begin_text_turn()
        self.assertEqual(text.state, DialogState.THINKING)
        self.assertEqual(text.mode, DialogInputMode.TEXT)
        with self.assertRaises(DialogError):
            text.begin_text_turn()
        with self.assertRaises(DialogError):
            text.begin_voice_turn()

    def test_fail_closed_then_recover_rotates_window_identity(self) -> None:
        item = machine(window_id="dialog-window-a")
        self.assertEqual(item.window_id, "dialog-window-a")
        self.assertEqual(item.source_instance_id, "dialog-instance-1")
        item.fail_closed(reason="asr_failed")
        self.assertEqual(item.state, DialogState.FAIL_CLOSED)
        transition = item.recover(reason="recovered")
        self.assertEqual(transition.from_state, DialogState.FAIL_CLOSED)
        self.assertEqual(transition.to_state, DialogState.IDLE)
        self.assertNotEqual(item.window_id, "dialog-window-a")
        self.assertTrue(item.accepts_input)
        self.assertIsNone(item.mode)
        with self.assertRaises(DialogError):
            item.recover(reason="recovered")
        item.fail_closed(reason="audio_failed")
        with self.assertRaises(DialogError):
            item.fail_closed(reason="audio_failed")

    def test_default_window_identity_is_fresh_per_machine(self) -> None:
        first = machine()
        second = machine()
        self.assertTrue(first.window_id)
        self.assertNotEqual(first.window_id, second.window_id)

    def test_constructor_argument_validation(self) -> None:
        with self.assertRaises(ValueError):
            machine(source_instance_id="")
        with self.assertRaises(ValueError):
            machine(window_id="bad id")
        with self.assertRaises(TypeError):
            DialogStateMachine("dialog-instance-1")  # type: ignore[misc]
        with self.assertRaises(TypeError):
            machine(diagnostic_sink="not-callable")

    def test_mode_is_preserved_inside_input_states_and_cleared_on_exit(self) -> None:
        item = machine()
        item.begin_voice_turn()
        item.transition(DialogState.TRANSCRIBING, reason="speech_finished")
        self.assertEqual(item.mode, DialogInputMode.VOICE_TURN)
        item.transition(DialogState.THINKING, reason="transcription_ready")
        self.assertEqual(item.mode, DialogInputMode.VOICE_TURN)
        item.transition(DialogState.IDLE, reason="turn_completed")
        self.assertIsNone(item.mode)
        item.transition(DialogState.LISTENING, reason="listening_started")
        self.assertIsNone(item.mode)

    def test_diagnostics_are_redacted_and_transitions_recorded(self) -> None:
        events: list[DialogDiagnostic] = []
        item = machine(diagnostic_sink=events.append)
        item.begin_voice_turn()
        self.assertEqual(len(events), 1)
        first = events[0]
        self.assertIsInstance(first, DialogDiagnostic)
        self.assertEqual(first.event_type, "dialog_transition")
        self.assertEqual(first.subject_id, item.window_id)
        self.assertEqual(first.reason, "voice_turn_started")
        self.assertEqual(first.state, DialogState.USER_SPEAKING)
        self.assertEqual(first.mode, DialogInputMode.VOICE_TURN)
        self.assertEqual(set(first.to_details()), {"reason", "state", "mode"})
        self.assertEqual(first.to_details()["mode"], "voice_turn")
        item.close(reason="shutdown")
        self.assertEqual(events[-1].to_details()["mode"], "none")
        self.assertTrue(all(isinstance(entry, DialogDiagnostic) for entry in events))
        self.assertEqual(len(item.transitions), 2)

    def test_records_are_frozen(self) -> None:
        item = machine()
        transition = item.begin_voice_turn()
        self.assertIsInstance(transition, DialogTransition)
        self.assertEqual(transition.reason, "voice_turn_started")
        with self.assertRaises(Exception):
            transition.reason = "other"  # type: ignore[misc]
        with self.assertRaises(Exception):
            item.window_id = "other"  # type: ignore[misc]
        self.assertEqual(tuple(item.transitions), (transition,))


if __name__ == "__main__":
    unittest.main()
