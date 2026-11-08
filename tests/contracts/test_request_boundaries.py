from __future__ import annotations

import unittest

from app.events import EventDecision, EventEnvelope, EventGate, RequestSubmissionGate


class RequestSubmissionTests(unittest.TestCase):
    def test_same_request_id_cannot_be_resubmitted_after_transport_failure(self) -> None:
        gate = RequestSubmissionGate()
        self.assertTrue(gate.submit_once("request-1"))
        # 传输、ASR 或 TTS 故障不释放已提交请求的身份。
        self.assertFalse(gate.submit_once("request-1"))
        self.assertTrue(gate.submit_once("request-2"))

    def test_invalid_request_id_is_rejected(self) -> None:
        gate = RequestSubmissionGate()
        with self.assertRaisesRegex(ValueError, "request_id"):
            gate.submit_once("")


class LateAudioEventTests(unittest.TestCase):
    def make_event(self, *, event_id: str, sequence: int, utterance_id: str = "utterance-1", event_type: str = "asr.partial", source: str = "asr") -> EventEnvelope:
        return EventEnvelope(
            event_id=event_id,
            event_type=event_type,
            source=source,
            source_instance_id="instance-1",
            session_id="session-1",
            event_sequence=sequence,
            created_at="2026-09-19T10:00:00Z",
            received_at="2026-09-19T10:00:01Z",
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            utterance_id=utterance_id,
        )

    def make_gate(self) -> EventGate:
        return EventGate(
            session_id="session-1",
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            utterance_id="utterance-1",
        )

    def test_same_sequence_with_different_event_id_is_duplicate(self) -> None:
        gate = self.make_gate()
        self.assertEqual(gate.evaluate(self.make_event(event_id="event-1", sequence=1)), EventDecision.ACCEPT)
        self.assertEqual(gate.evaluate(self.make_event(event_id="event-2", sequence=1)), EventDecision.DUPLICATE)

    def test_old_utterance_is_diagnostic_only(self) -> None:
        gate = self.make_gate()
        decision = gate.evaluate(self.make_event(event_id="event-old", sequence=1, utterance_id="utterance-old"))
        self.assertEqual(decision, EventDecision.STALE_UTTERANCE)

    def test_event_after_final_terminal_is_diagnostic_only(self) -> None:
        gate = self.make_gate()
        final = self.make_event(event_id="event-final", sequence=1, event_type="audio.closed", source="audio")
        late = self.make_event(event_id="event-late", sequence=2)
        self.assertEqual(gate.evaluate(final), EventDecision.ACCEPT)
        self.assertEqual(gate.evaluate(late), EventDecision.AFTER_TERMINAL)


if __name__ == "__main__":
    unittest.main()