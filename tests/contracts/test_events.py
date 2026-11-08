from __future__ import annotations

import unittest

from app.events import EventDecision, EventEnvelope, EventGate


class EventGateTests(unittest.TestCase):
    def make_event(self, *, sequence: int, generation: int = 1, stream_id: str = "stream-1", session_id: str = "session-1") -> EventEnvelope:
        return EventEnvelope(event_id=f"event-{generation}-{stream_id}-{sequence}", event_type="asr.final", source="asr", source_instance_id="instance-1", session_id=session_id, event_sequence=sequence, created_at="2026-09-19T10:00:00Z", received_at="2026-09-19T10:00:01Z", audio_session_id="audio-1", provider_generation=generation, stream_id=stream_id, utterance_id="utterance-1")

    def test_accepts_monotonic_event_once(self) -> None:
        gate = EventGate(session_id="session-1", audio_session_id="audio-1", provider_generation=1, stream_id="stream-1")
        self.assertEqual(gate.evaluate(self.make_event(sequence=1)), EventDecision.ACCEPT)
        self.assertEqual(gate.evaluate(self.make_event(sequence=2)), EventDecision.ACCEPT)

    def test_duplicate_and_out_of_order_events_are_diagnostic_only(self) -> None:
        gate = EventGate(session_id="session-1", audio_session_id="audio-1", provider_generation=1, stream_id="stream-1")
        event = self.make_event(sequence=2)
        self.assertEqual(gate.evaluate(event), EventDecision.ACCEPT)
        self.assertEqual(gate.evaluate(event), EventDecision.DUPLICATE)
        self.assertEqual(gate.evaluate(self.make_event(sequence=1)), EventDecision.OUT_OF_ORDER)

    def test_old_generation_stream_or_session_cannot_cross_boundary(self) -> None:
        gate = EventGate(session_id="session-1", audio_session_id="audio-1", provider_generation=2, stream_id="stream-2")
        self.assertEqual(gate.evaluate(self.make_event(sequence=1, generation=1, stream_id="stream-2")), EventDecision.STALE_GENERATION)
        self.assertEqual(gate.evaluate(self.make_event(sequence=1, generation=2, stream_id="stream-old")), EventDecision.STALE_STREAM)
        self.assertEqual(gate.evaluate(self.make_event(sequence=1, generation=2, stream_id="stream-2", session_id="session-old")), EventDecision.CROSS_SESSION)

    def test_audio_event_requires_complete_audio_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "audio_session_id"):
            EventEnvelope(event_id="event-1", event_type="asr.final", source="asr", source_instance_id="instance-1", session_id="session-1", event_sequence=1, created_at="2026-09-19T10:00:00Z", received_at="2026-09-19T10:00:01Z", provider_generation=1, stream_id="stream-1", utterance_id="utterance-1")


if __name__ == "__main__":
    unittest.main()