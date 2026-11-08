from __future__ import annotations

import unittest

from app.audio import (
    AudioBus,
    AudioPacket,
    VadConfig,
    VadEndReason,
    VadFrameStatus,
    VadSession,
    VadSessionState,
)
from app.contracts import AudioFrame


class VadSessionTests(unittest.TestCase):
    def make_bus(self, *, device_epoch: int = 1) -> AudioBus:
        return AudioBus(
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            device_epoch=device_epoch,
            capacity_frames=8,
        )

    def make_packet(self, *, sequence: int = 1, frame_index: int = 0, payload: bytes | None = None, **overrides: object) -> AudioPacket:
        values: dict[str, object] = {
            "audio_session_id": "audio-1",
            "provider_generation": 1,
            "stream_id": "stream-1",
            "event_sequence": sequence,
            "frame_index": frame_index,
            "captured_at": "2026-09-21T08:00:00Z",
            "duration_ms": 20,
            "device_epoch": 1,
            "sample_rate_hz": 16000,
            "channel_count": 1,
            "sample_format": "pcm_s16le",
            "sample_count": 320,
            "payload_size_bytes": 640,
        }
        values.update(overrides)
        return AudioPacket(AudioFrame(**values), bytes(640) if payload is None else payload)

    def test_explicit_button_release_ends_without_asr_semantics(self) -> None:
        session = VadSession(
            bus=self.make_bus(),
            config=VadConfig(silence_duration_ms=1000, frame_duration_ms=20),
        )
        self.assertEqual(session.start().status, VadFrameStatus.STARTED)
        result = session.finish()
        self.assertEqual(result.status, VadFrameStatus.ENDED)
        self.assertEqual(result.reason, VadEndReason.BUTTON_RELEASE)
        self.assertEqual(session.state, VadSessionState.ENDED)
        self.assertEqual(session.processed_frames, 0)

    def test_silence_timeout_is_only_an_input_boundary(self) -> None:
        bus = self.make_bus()
        session = VadSession(
            bus=bus,
            config=VadConfig(silence_duration_ms=60, frame_duration_ms=20),
            detector=lambda packet: packet.payload[0] == 1,
        )
        self.assertEqual(session.start().status, VadFrameStatus.STARTED)
        self.assertEqual(session.process(self.make_packet(payload=bytes([1]) + bytes(639))).status, VadFrameStatus.ACCEPTED)
        self.assertEqual(session.process(self.make_packet(sequence=2, frame_index=1)).status, VadFrameStatus.ACCEPTED)
        self.assertEqual(session.process(self.make_packet(sequence=3, frame_index=2)).status, VadFrameStatus.ACCEPTED)
        result = session.process(self.make_packet(sequence=4, frame_index=3))
        self.assertEqual(result.status, VadFrameStatus.ENDED)
        self.assertEqual(result.reason, VadEndReason.SILENCE_TIMEOUT)
        self.assertEqual(result.silence_frames, 3)
        self.assertEqual(session.process(self.make_packet(sequence=5, frame_index=4)).status, VadFrameStatus.CLOSED)

    def test_speech_resets_silence_counter(self) -> None:
        session = VadSession(
            bus=self.make_bus(),
            config=VadConfig(silence_duration_ms=40, frame_duration_ms=20),
            detector=lambda packet: packet.payload[0] == 1,
        )
        session.start()
        session.process(self.make_packet())
        session.process(self.make_packet(sequence=2, frame_index=1, payload=bytes([1]) + bytes(639)))
        result = session.process(self.make_packet(sequence=3, frame_index=2))
        self.assertEqual(result.status, VadFrameStatus.ACCEPTED)
        self.assertEqual(result.silence_frames, 1)

    def test_cancel_discards_pending_bus_audio_and_closes_session(self) -> None:
        bus = self.make_bus()
        self.assertIsNotNone(bus.publish(self.make_packet().frame, bytes(640)))
        session = VadSession(bus=bus, config=VadConfig(), detector=lambda packet: True)
        session.start()
        result = session.cancel()
        self.assertEqual(result.status, VadFrameStatus.CANCELLED)
        self.assertEqual(result.reason, VadEndReason.CANCELLED)
        self.assertEqual(session.state, VadSessionState.CANCELLED)
        self.assertEqual(bus.queued_frames, 0)

    def test_malformed_payload_fails_closed_without_accepting_frame(self) -> None:
        bus = self.make_bus()
        session = VadSession(bus=bus, config=VadConfig(), detector=lambda packet: True)
        session.start()
        result = session.process(self.make_packet(payload=bytes(639)))
        self.assertEqual(result.status, VadFrameStatus.FAILED)
        self.assertEqual(result.reason, VadEndReason.INVALID_AUDIO_FRAME)
        self.assertEqual(session.state, VadSessionState.FAILED)
        self.assertEqual(session.processed_frames, 0)
        self.assertEqual(bus.queued_frames, 0)

    def test_stale_boundary_fails_closed(self) -> None:
        cases = (
            ({"audio_session_id": "audio-old"}, VadEndReason.STALE_AUDIO_SESSION),
            ({"provider_generation": 2}, VadEndReason.STALE_GENERATION),
            ({"stream_id": "stream-old"}, VadEndReason.STALE_STREAM),
            ({"device_epoch": 2}, VadEndReason.STALE_DEVICE_EPOCH),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                session = VadSession(bus=self.make_bus(), config=VadConfig(), detector=lambda packet: True)
                session.start()
                result = session.process(self.make_packet(**overrides))
                self.assertEqual(result.status, VadFrameStatus.FAILED)
                self.assertEqual(result.reason, expected)

    def test_gap_or_duplicate_fails_closed_and_late_frames_are_closed(self) -> None:
        session = VadSession(bus=self.make_bus(), config=VadConfig(), detector=lambda packet: True)
        session.start()
        result = session.process(self.make_packet(sequence=2, frame_index=1))
        self.assertEqual(result.status, VadFrameStatus.FAILED)
        self.assertEqual(result.reason, VadEndReason.FRAME_DISCONTINUITY)
        self.assertEqual(session.process(self.make_packet()).status, VadFrameStatus.FAILED)

    def test_detector_missing_or_failing_never_accepts_audio(self) -> None:
        missing = VadSession(bus=self.make_bus(), config=VadConfig())
        missing.start()
        result = missing.process(self.make_packet())
        self.assertEqual(result.reason, VadEndReason.DETECTOR_UNAVAILABLE)

        failing = VadSession(bus=self.make_bus(), config=VadConfig(), detector=lambda packet: (_ for _ in ()).throw(RuntimeError("vad")))
        failing.start()
        result = failing.process(self.make_packet())
        self.assertEqual(result.reason, VadEndReason.DETECTOR_FAILURE)

    def test_non_boolean_detector_result_fails_closed(self) -> None:
        session = VadSession(bus=self.make_bus(), config=VadConfig(), detector=lambda packet: 1)  # type: ignore[return-value]
        session.start()
        result = session.process(self.make_packet())
        self.assertEqual(result.status, VadFrameStatus.FAILED)
        self.assertEqual(result.reason, VadEndReason.DETECTOR_FAILURE)


if __name__ == "__main__":
    unittest.main()