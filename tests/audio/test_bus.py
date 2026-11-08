from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.audio import AudioBus, AudioBusDecision, AudioBusState
from app.contracts import AudioFrame, ContractError


class AudioFrameTests(unittest.TestCase):
    def make_frame(self, **overrides: object) -> AudioFrame:
        values: dict[str, object] = {
            "audio_session_id": "audio-1",
            "provider_generation": 1,
            "stream_id": "stream-1",
            "device_epoch": 3,
            "event_sequence": 1,
            "frame_index": 0,
            "captured_at": "2026-09-21T08:00:00Z",
            "duration_ms": 20,
            "sample_rate_hz": 16000,
            "channel_count": 1,
            "sample_format": "pcm_s16le",
            "sample_count": 320,
            "payload_size_bytes": 640,
        }
        values.update(overrides)
        return AudioFrame(**values)

    def test_frame_is_strict_frozen_and_round_trips_without_pcm_payload(self) -> None:
        frame = self.make_frame()
        self.assertEqual(AudioFrame.from_json(frame.to_json()), frame)
        self.assertNotIn("pcm", frame.to_dict())
        with self.assertRaises(FrozenInstanceError):
            frame.frame_index = 2  # type: ignore[misc]

    def test_frame_rejects_invalid_format_size_and_duration(self) -> None:
        with self.assertRaisesRegex(ContractError, "sample_format"):
            self.make_frame(sample_format="wav")
        with self.assertRaisesRegex(ContractError, "payload_size_bytes"):
            self.make_frame(payload_size_bytes=639)
        with self.assertRaisesRegex(ContractError, "duration_ms"):
            self.make_frame(duration_ms=30)


class AudioBusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = AudioBus(
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            device_epoch=3,
            capacity_frames=2,
        )

    def make_frame(self, *, sequence: int, frame_index: int, **overrides: object) -> AudioFrame:
        values: dict[str, object] = {
            "audio_session_id": "audio-1",
            "provider_generation": 1,
            "stream_id": "stream-1",
            "device_epoch": 3,
            "event_sequence": sequence,
            "frame_index": frame_index,
            "captured_at": "2026-09-21T08:00:00Z",
            "duration_ms": 20,
            "sample_rate_hz": 16000,
            "channel_count": 1,
            "sample_format": "pcm_s16le",
            "sample_count": 320,
            "payload_size_bytes": 640,
        }
        values.update(overrides)
        return AudioFrame(**values)

    def test_accepts_contiguous_frames_and_copies_mutable_payload(self) -> None:
        payload = bytearray(640)
        self.assertEqual(self.bus.publish(self.make_frame(sequence=1, frame_index=0), payload), AudioBusDecision.ACCEPTED)
        payload[0] = 99
        self.assertEqual(self.bus.publish(self.make_frame(sequence=2, frame_index=1), bytes(640)), AudioBusDecision.ACCEPTED)

        first = self.bus.read_nowait()
        second = self.bus.read_nowait()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.payload[0], 0)
        self.assertEqual((first.frame.frame_index, second.frame.frame_index), (0, 1))
        self.assertIsNone(self.bus.read_nowait())

    def test_cross_boundary_and_old_device_frames_are_diagnostic_only(self) -> None:
        cases = (
            ({"audio_session_id": "audio-old"}, AudioBusDecision.STALE_AUDIO_SESSION),
            ({"provider_generation": 2}, AudioBusDecision.STALE_GENERATION),
            ({"stream_id": "stream-old"}, AudioBusDecision.STALE_STREAM),
            ({"device_epoch": 2}, AudioBusDecision.STALE_DEVICE_EPOCH),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                decision = self.bus.publish(self.make_frame(sequence=1, frame_index=0, **overrides), bytes(640))
                self.assertEqual(decision, expected)
                self.assertEqual(self.bus.state, AudioBusState.OPEN)
                self.assertEqual(self.bus.queued_frames, 0)

    def test_duplicate_or_gap_fails_closed_and_discards_partial_audio(self) -> None:
        self.assertEqual(self.bus.publish(self.make_frame(sequence=1, frame_index=0), bytes(640)), AudioBusDecision.ACCEPTED)
        self.assertEqual(self.bus.publish(self.make_frame(sequence=1, frame_index=0), bytes(640)), AudioBusDecision.DISCONTINUITY)
        self.assertEqual(self.bus.state, AudioBusState.FAILED)
        self.assertEqual(self.bus.terminal_reason, "frame_discontinuity")
        self.assertEqual(self.bus.queued_frames, 0)
        self.assertIsNone(self.bus.read_nowait())
        self.assertEqual(self.bus.publish(self.make_frame(sequence=2, frame_index=1), bytes(640)), AudioBusDecision.CLOSED)

        other = AudioBus(audio_session_id="audio-1", provider_generation=1, stream_id="stream-1", device_epoch=3, capacity_frames=2)
        self.assertEqual(other.publish(self.make_frame(sequence=2, frame_index=1), bytes(640)), AudioBusDecision.DISCONTINUITY)
        self.assertEqual(other.state, AudioBusState.FAILED)

    def test_payload_mismatch_is_rejected_without_consuming_sequence(self) -> None:
        frame = self.make_frame(sequence=1, frame_index=0)
        self.assertEqual(self.bus.publish(frame, bytes(639)), AudioBusDecision.INVALID_PAYLOAD)
        self.assertEqual(self.bus.state, AudioBusState.OPEN)
        self.assertEqual(self.bus.publish(frame, bytes(640)), AudioBusDecision.ACCEPTED)

    def test_overflow_fails_closed_and_discards_all_audio(self) -> None:
        self.assertEqual(self.bus.publish(self.make_frame(sequence=1, frame_index=0), bytes(640)), AudioBusDecision.ACCEPTED)
        self.assertEqual(self.bus.publish(self.make_frame(sequence=2, frame_index=1), bytes(640)), AudioBusDecision.ACCEPTED)
        self.assertEqual(self.bus.publish(self.make_frame(sequence=3, frame_index=2), bytes(640)), AudioBusDecision.OVERFLOW)
        self.assertEqual(self.bus.state, AudioBusState.FAILED)
        self.assertEqual(self.bus.terminal_reason, "overflow")
        self.assertEqual(self.bus.queued_frames, 0)

    def test_close_can_preserve_complete_queue_or_discard_cancelled_audio(self) -> None:
        self.assertEqual(self.bus.publish(self.make_frame(sequence=1, frame_index=0), bytes(640)), AudioBusDecision.ACCEPTED)
        self.bus.close(reason="capture_completed", discard=False)
        self.assertEqual(self.bus.state, AudioBusState.CLOSED)
        self.assertIsNotNone(self.bus.read_nowait())
        self.assertEqual(self.bus.publish(self.make_frame(sequence=2, frame_index=1), bytes(640)), AudioBusDecision.CLOSED)

        cancelled = AudioBus(audio_session_id="audio-1", provider_generation=1, stream_id="stream-1", device_epoch=3, capacity_frames=2)
        self.assertEqual(cancelled.publish(self.make_frame(sequence=1, frame_index=0), bytes(640)), AudioBusDecision.ACCEPTED)
        cancelled.close(reason="cancelled", discard=True)
        self.assertEqual(cancelled.queued_frames, 0)
        self.assertIsNone(cancelled.read_nowait())


if __name__ == "__main__":
    unittest.main()
