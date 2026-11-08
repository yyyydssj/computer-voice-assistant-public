from __future__ import annotations

import unittest

from app.asr import AsrEvent, AsrEventKind, DetectionStatus
from app.asr.result_gate import (
    AsrResultCandidate,
    ResultGate,
    ResultGateDecision,
)


class ResultGateTests(unittest.TestCase):
    def make_candidate(
        self,
        *,
        event_id: str = "asr-event-1",
        event_type: str = "asr.final",
        sequence: int = 1,
        generation: int = 1,
        provider_generation: int | None = None,
        stream_id: str = "stream-1",
        utterance_id: str = "utterance-1",
        session_id: str = "session-1",
        audio_session_id: str = "audio-1",
        text: str = "打开记事本",
        is_final: bool = True,
    ) -> AsrResultCandidate:
        return AsrResultCandidate(
            event_id=event_id,
            event_type=event_type,
            source_instance_id="sensevoice-1",
            session_id=session_id,
            audio_session_id=audio_session_id,
            provider_generation=generation if provider_generation is None else provider_generation,
            stream_id=stream_id,
            utterance_id=utterance_id,
            event_sequence=sequence,
            text=text,
            is_final=is_final,
            provider_timestamp="2026-09-21T10:00:00Z",
            received_at="2026-09-21T10:00:01Z",
        )

    def make_gate(self) -> ResultGate:
        return ResultGate(
            session_id="session-1",
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            utterance_id="utterance-1",
            source_instance_id="sensevoice-1",
        )

    def test_provider_final_is_accepted_once_as_current_result(self) -> None:
        gate = self.make_gate()
        result = gate.evaluate(self.make_candidate())
        self.assertEqual(result.decision, ResultGateDecision.ACCEPTED_FINAL)
        self.assertIsNotNone(result.event)
        self.assertEqual(result.text, "打开记事本")
        self.assertTrue(result.reliable)
        self.assertEqual(gate.state, "accepted")

    def test_interim_is_consumed_for_ordering_but_never_accepted(self) -> None:
        gate = self.make_gate()
        interim = self.make_candidate(
            event_type="asr.partial",
            is_final=False,
            text="打开",
        )
        result = gate.evaluate(interim)
        self.assertEqual(result.decision, ResultGateDecision.DROP_INTERIM)
        self.assertIsNone(result.event)
        self.assertFalse(result.reliable)
        final = gate.evaluate(self.make_candidate(event_id="asr-event-2", sequence=2))
        self.assertEqual(final.decision, ResultGateDecision.ACCEPTED_FINAL)

    def test_empty_final_is_rejected_without_terminal_acceptance(self) -> None:
        gate = self.make_gate()
        result = gate.evaluate(self.make_candidate(text="", event_type="asr.empty"))
        self.assertEqual(result.decision, ResultGateDecision.REJECT_EMPTY)
        self.assertIsNone(result.event)
        self.assertFalse(result.reliable)
        self.assertEqual(gate.state, "rejected")

    def test_duplicate_final_is_rejected_and_same_text_next_utterance_is_independent(self) -> None:
        gate = self.make_gate()
        first = gate.evaluate(self.make_candidate())
        self.assertEqual(first.decision, ResultGateDecision.ACCEPTED_FINAL)
        self.assertEqual(gate.evaluate(self.make_candidate()).decision, ResultGateDecision.DUPLICATE)
        next_gate = ResultGate(
            session_id="session-1",
            audio_session_id="audio-2",
            provider_generation=2,
            stream_id="stream-2",
            utterance_id="utterance-2",
            source_instance_id="sensevoice-1",
        )
        next_result = next_gate.evaluate(
            self.make_candidate(
                event_id="asr-event-2",
                sequence=1,
                generation=2,
                audio_session_id="audio-2",
                stream_id="stream-2",
                utterance_id="utterance-2",
                text="打开记事本",
            )
        )
        self.assertEqual(next_result.decision, ResultGateDecision.ACCEPTED_FINAL)

    def test_out_of_order_and_same_sequence_replay_are_rejected(self) -> None:
        gate = self.make_gate()
        interim = gate.evaluate(self.make_candidate(event_id="asr-event-2", sequence=2, event_type="asr.partial", is_final=False))
        self.assertEqual(interim.decision, ResultGateDecision.DROP_INTERIM)
        self.assertEqual(
            gate.evaluate(self.make_candidate(event_id="asr-event-1", sequence=1)).decision,
            ResultGateDecision.OUT_OF_ORDER,
        )
        self.assertEqual(
            gate.evaluate(self.make_candidate(event_id="asr-event-3", sequence=3)).decision,
            ResultGateDecision.ACCEPTED_FINAL,
        )
        self.assertEqual(
            gate.evaluate(self.make_candidate(event_id="asr-event-3", sequence=3)).decision,
            ResultGateDecision.DUPLICATE,
        )
        self.assertEqual(
            gate.evaluate(self.make_candidate(event_id="asr-event-4", sequence=4)).decision,
            ResultGateDecision.AFTER_TERMINAL,
        )

    def test_stale_boundaries_are_rejected_before_text_acceptance(self) -> None:
        cases = (
            ("session_id", ResultGateDecision.CROSS_SESSION),
            ("audio_session_id", ResultGateDecision.STALE_AUDIO_SESSION),
            ("provider_generation", ResultGateDecision.STALE_GENERATION),
            ("stream_id", ResultGateDecision.STALE_STREAM),
            ("utterance_id", ResultGateDecision.STALE_UTTERANCE),
        )
        for field, expected in cases:
            with self.subTest(field=field):
                gate = self.make_gate()
                values = {field: "other" if field != "provider_generation" else 2}
                result = gate.evaluate(self.make_candidate(**values))
                self.assertEqual(result.decision, expected)
                self.assertIsNone(result.event)
                self.assertFalse(result.reliable)

    def test_late_event_after_accepted_final_is_rejected(self) -> None:
        gate = self.make_gate()
        self.assertEqual(gate.evaluate(self.make_candidate()).decision, ResultGateDecision.ACCEPTED_FINAL)
        late = gate.evaluate(self.make_candidate(event_id="asr-event-2", sequence=2))
        self.assertEqual(late.decision, ResultGateDecision.AFTER_TERMINAL)
        self.assertIsNone(late.event)

    def test_provider_event_can_be_normalized_without_treating_is_final_as_gate_acceptance(self) -> None:
        provider_event = AsrEvent(
            event_id="asr-provider-1",
            kind=AsrEventKind.FINAL,
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            utterance_id="utterance-1",
            event_sequence=1,
            text="你好",
            is_final=True,
            overlap_detection=DetectionStatus.UNSUPPORTED,
            speaker_identity=DetectionStatus.UNSUPPORTED,
            confidence=DetectionStatus.UNSUPPORTED,
            provider_timestamp="2026-09-21T10:00:00Z",
            received_at="2026-09-21T10:00:01Z",
        )
        candidate = AsrResultCandidate.from_provider_event(
            provider_event,
            session_id="session-1",
            source_instance_id="sensevoice-1",
        )
        result = self.make_gate().evaluate(candidate)
        self.assertEqual(result.decision, ResultGateDecision.ACCEPTED_FINAL)
        self.assertTrue(result.reliable)

    def test_malformed_final_is_rejected(self) -> None:
        gate = self.make_gate()
        result = gate.evaluate(self.make_candidate(text="   "))
        self.assertEqual(result.decision, ResultGateDecision.REJECT_EMPTY)
        self.assertFalse(result.reliable)


if __name__ == "__main__":
    unittest.main()
