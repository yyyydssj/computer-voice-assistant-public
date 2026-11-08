"""P8 批次 2 红测：统一输入接缝、文字降级与显式切回边界（离线、无副作用）。"""

from __future__ import annotations

import unittest

from app.voice_loop import (
    VoiceInputMode,
    VoiceInputSeam,
    VoiceLoopError,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
)
from tests.voice_loop.support import (
    FIXED_NOW,
    TIMESTAMP,
    RecordingConversation,
)

ALLOWED_PUBLIC_API = {
    "mode",
    "degraded_reason",
    "voice_generation",
    "voice_available",
    "status_text",
    "note_voice_ready",
    "degrade",
    "request_voice_switch",
    "open_text_turn",
    "open_voice_turn",
    "submit",
    "close_voice_turn",
}


class Busy:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy

    def __call__(self) -> bool:
        return self.busy


def seam(**overrides: object) -> VoiceInputSeam:
    registry = VoiceTurnRegistry(source_instance_id="instance-1", now=lambda: FIXED_NOW)
    values: dict[str, object] = {
        "conversation": overrides.pop("conversation", RecordingConversation()),
        "registry": registry,
        "busy_probe": overrides.pop("busy_probe", Busy(False)),
    }
    values.update(overrides)
    return VoiceInputSeam(**values)  # type: ignore[arg-type]


class VoiceInputSeamModeTests(unittest.TestCase):
    def test_starts_in_text_mode(self) -> None:
        item = seam()
        self.assertEqual(item.mode, VoiceInputMode.TEXT)
        self.assertFalse(item.voice_available)
        self.assertEqual(item.voice_generation, 0)

    def test_voice_readiness_never_switches_automatically(self) -> None:
        item = seam()
        item.note_voice_ready()
        self.assertTrue(item.voice_available)
        self.assertEqual(item.mode, VoiceInputMode.TEXT)
        self.assertEqual(item.voice_generation, 0)

    def test_switch_requires_availability_local_check_and_idle(self) -> None:
        item = seam()
        refused = item.request_voice_switch(local_check_passed=True)
        self.assertFalse(refused.switched)
        self.assertEqual(refused.reason, "voice_not_available")
        item.note_voice_ready()
        refused = item.request_voice_switch(local_check_passed=False)
        self.assertFalse(refused.switched)
        self.assertEqual(refused.reason, "local_check_failed")
        self.assertEqual(item.mode, VoiceInputMode.TEXT)
        self.assertEqual(item.voice_generation, 0)

    def test_switch_is_refused_while_a_turn_or_reply_is_in_flight(self) -> None:
        busy = Busy(False)
        item = seam(busy_probe=busy)
        item.note_voice_ready()
        item.open_text_turn()
        refused = item.request_voice_switch(local_check_passed=True)
        self.assertFalse(refused.switched)
        self.assertEqual(refused.reason, "turn_in_progress")
        busy.busy = True
        item2 = seam(busy_probe=busy)
        item2.note_voice_ready()
        refused = item2.request_voice_switch(local_check_passed=True)
        self.assertFalse(refused.switched)
        self.assertEqual(refused.reason, "conversation_busy")

    def test_explicit_switch_opens_a_new_voice_generation(self) -> None:
        item = seam()
        item.note_voice_ready()
        switched = item.request_voice_switch(local_check_passed=True)
        self.assertTrue(switched.switched)
        self.assertEqual(switched.generation, 1)
        self.assertEqual(item.mode, VoiceInputMode.VOICE)
        self.assertEqual(item.voice_generation, 1)
        again = item.request_voice_switch(local_check_passed=True)
        self.assertFalse(again.switched)
        self.assertEqual(again.reason, "already_voice")
        self.assertEqual(item.voice_generation, 1)

    def test_degrade_cancels_the_active_turn_and_records_reason(self) -> None:
        item = seam()
        item.note_voice_ready()
        item.request_voice_switch(local_check_passed=True)
        active = item.open_voice_turn()
        retired = item.degrade(reason="asr_stream_failed")
        self.assertEqual(tuple(entry.identity for entry in retired), (active.identity,))
        self.assertEqual(item.mode, VoiceInputMode.TEXT)
        self.assertEqual(item.degraded_reason, "asr_stream_failed")
        self.assertEqual(item.degrade(reason="asr_unavailable"), ())

    def test_degrade_during_a_reply_only_changes_the_input_channel(self) -> None:
        busy = Busy(True)
        item = seam(busy_probe=busy)
        item.note_voice_ready()
        item.request_voice_switch(local_check_passed=True)
        retired = item.degrade(reason="asr_stream_failed")
        self.assertEqual(retired, ())
        self.assertEqual(item.mode, VoiceInputMode.TEXT)

    def test_bad_reason_codes_are_rejected(self) -> None:
        item = seam()
        with self.assertRaises(VoiceLoopError):
            item.degrade(reason="stack trace: boom")
        with self.assertRaises(VoiceLoopError):
            item.degrade(reason="not_a_reason")

    def test_status_text_is_a_fixed_redacted_string(self) -> None:
        item = seam()
        item.note_voice_ready()
        self.assertEqual(
            item.status_text, "语音识别已就绪；当前为文字输入，需用户明确请求后切回"
        )
        item.request_voice_switch(local_check_passed=True)
        self.assertEqual(item.status_text, "当前为语音输入")
        item.degrade(reason="asr_stream_failed")
        self.assertEqual(item.status_text, "语音识别不可用；当前为文字输入")


class VoiceInputSeamSubmissionTests(unittest.TestCase):
    def voice_seam(self, **overrides: object):
        conversation = overrides.pop("conversation", RecordingConversation())
        item = seam(conversation=conversation, **overrides)
        item.note_voice_ready()
        item.request_voice_switch(local_check_passed=True)
        return item, conversation

    def test_text_submission_uses_the_same_conversation_entry_point(self) -> None:
        conversation = RecordingConversation()
        item = seam(conversation=conversation)
        boundary = item.open_text_turn()
        submission = item.submit(boundary, "你好")
        self.assertTrue(submission.submitted)
        self.assertIsNone(submission.refusal)
        self.assertEqual(conversation.calls[0]["entry"], "submit_text")
        self.assertEqual(conversation.calls[0]["request_id"], boundary.request_id)
        self.assertEqual(conversation.calls[0]["turn_id"], boundary.turn_id)
        self.assertEqual(submission.mode, VoiceInputMode.TEXT)
        self.assertEqual(submission.boundary.identity, boundary.identity)

    def test_asr_submission_uses_the_same_conversation_entry_point(self) -> None:
        item, conversation = self.voice_seam()
        boundary = item.open_voice_turn()
        submission = item.submit(boundary, "打开记事本")
        self.assertTrue(submission.submitted)
        self.assertEqual(conversation.calls[0]["entry"], "submit_asr_final")
        self.assertEqual(conversation.calls[0]["request_id"], boundary.request_id)
        self.assertEqual(conversation.calls[0]["turn_id"], boundary.turn_id)
        self.assertEqual(submission.mode, VoiceInputMode.VOICE)

    def test_channels_cannot_be_crossed(self) -> None:
        item, _ = self.voice_seam()
        text_boundary = VoiceTurnBoundary(
            voice_session_id="voice-session-2",
            utterance_id="text-utterance-2",
            turn_id="text_shell-turn-2",
            request_id="text_shell-request-2",
            reply_id="text_shell-reply-2",
            source="text_shell",
            created_at=TIMESTAMP,
        )
        with self.assertRaises(VoiceLoopError):
            item.open_text_turn()
        with self.assertRaises(VoiceLoopError):
            item.submit(text_boundary, "你好")

    def test_text_mode_refuses_voice_turn_and_asr_submission(self) -> None:
        item = seam()
        with self.assertRaises(VoiceLoopError):
            item.open_voice_turn()
        foreign = VoiceTurnBoundary(
            voice_session_id="voice-session-3",
            utterance_id="asr-utterance-3",
            turn_id="asr-turn-3",
            request_id="asr-request-3",
            reply_id="asr-reply-3",
            source="asr",
            created_at=TIMESTAMP,
        )
        with self.assertRaises(VoiceLoopError):
            item.submit(foreign, "你好")

    def test_submission_requires_the_current_active_boundary(self) -> None:
        item, _ = self.voice_seam()
        boundary = item.open_voice_turn()
        with self.assertRaises(TypeError):
            item.submit("boundary", "你好")  # type: ignore[arg-type]
        item.degrade(reason="asr_stream_failed")
        with self.assertRaises(VoiceLoopError):
            item.submit(boundary, "你好")

    def test_one_submission_per_boundary(self) -> None:
        item, conversation = self.voice_seam()
        boundary = item.open_voice_turn()
        item.submit(boundary, "第一次")
        with self.assertRaises(VoiceLoopError):
            item.submit(boundary, "第二次")
        self.assertEqual(len(conversation.calls), 1)

    def test_voice_pipeline_can_close_its_own_turn(self) -> None:
        item, conversation = self.voice_seam()
        boundary = item.open_voice_turn()
        closed = item.close_voice_turn(boundary, reason="completed")
        self.assertEqual(closed.identity, boundary.identity)
        self.assertEqual(conversation.calls, [])
        follow_up = item.open_voice_turn()
        self.assertNotEqual(follow_up.identity, boundary.identity)

    def test_close_voice_turn_rejects_text_boundaries_and_foreign_turns(self) -> None:
        item, _ = self.voice_seam()
        voice_boundary = item.open_voice_turn()
        item.close_voice_turn(voice_boundary, reason="completed")
        with self.assertRaises(VoiceLoopError):
            item.close_voice_turn(voice_boundary, reason="completed")
        with self.assertRaises(VoiceLoopError):
            item.close_voice_turn(voice_boundary, reason="not_a_reason")
        text_mode = seam()
        text_boundary = text_mode.open_text_turn()
        with self.assertRaises(VoiceLoopError):
            text_mode.close_voice_turn(text_boundary, reason="completed")

    def test_next_turn_uses_fresh_identities(self) -> None:
        item, _ = self.voice_seam()
        first = item.open_voice_turn()
        item.close_voice_turn(first, reason="completed")
        second = item.open_voice_turn()
        self.assertNotEqual(first.identity, second.identity)
        for name in ("utterance_id", "turn_id", "request_id", "reply_id"):
            self.assertNotEqual(getattr(first, name), getattr(second, name))

    def test_empty_text_is_rejected_without_submitting(self) -> None:
        item, conversation = self.voice_seam()
        boundary = item.open_voice_turn()
        with self.assertRaises(ValueError):
            item.submit(boundary, "   ")
        self.assertEqual(conversation.calls, [])
        self.assertTrue(boundary.identity is not None)

    def test_ambiguous_route_is_reported_without_agent_outcome(self) -> None:
        item, _ = self.voice_seam(conversation=RecordingConversation(route="ambiguous"))
        boundary = item.open_voice_turn()
        submission = item.submit(boundary, "那个东西")
        self.assertTrue(submission.submitted)
        self.assertIsNone(submission.result.outcome)
        self.assertEqual(submission.result.handoff.route, "ambiguous")

    def test_public_surface_has_no_authorization_or_execution_api(self) -> None:
        item = seam()
        public = {name for name in dir(item) if not name.startswith("_")}
        self.assertEqual(public, ALLOWED_PUBLIC_API)
        for name in public:
            lowered = name.lower()
            for forbidden in ("authoriz", "approve", "execute", "plan", "ledger", "consume"):
                self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
