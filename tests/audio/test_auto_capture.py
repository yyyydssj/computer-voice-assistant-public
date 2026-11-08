"""P9 批次 2 红测：自动收音的语音起始、被动回放与一次性交付边界。

本文件先于实现编写（红测），拒绝路径优先：旧边界帧、跳号、溢出、二次交付、
invalidate、检测器缺失/异常、语音起始超时、最长 utterance 截断、pre-roll 顺序。
测试只覆盖输入边界，不涉及 ASR 可靠性、授权、计划或执行。
"""

from __future__ import annotations

import dataclasses
import struct
import unittest

from app.audio.bus import AudioBus, AudioBusState, AudioBusDecision, AudioPacket
from app.audio.vad import VadConfig
from app.audio.vad.auto_capture import (
    AutoCaptureConfig,
    AutoCaptureDecision,
    AutoCaptureEndReason,
    AutoCaptureSession,
    AutoCaptureState,
    PassiveFrameDecision,
    PassiveFrameObservation,
    SpeechStartConfig,
    SpeechStartDecision,
    SpeechStartDetector,
)
from app.contracts import AudioFrame
from app.contracts.base import ContractError

SAMPLE_COUNT = 320
PAYLOAD_SIZE_BYTES = 640
ACCEPTED = AudioBusDecision.ACCEPTED

LOUD_PAYLOAD = struct.pack("<320h", *([12000] * SAMPLE_COUNT))
QUIET_PAYLOAD = struct.pack("<320h", *([4] * SAMPLE_COUNT))
SILENCE_PAYLOAD = bytes(PAYLOAD_SIZE_BYTES)


def is_not_silent(packet: AudioPacket) -> bool:
    return packet.payload[:2] != b"\x00\x00"


def is_speech_started(decision: AutoCaptureDecision) -> bool:
    return decision.state is AutoCaptureState.CAPTURING


class AutoCaptureTestCase(unittest.TestCase):
    def make_bus(self, *, capacity_frames: int = 16, **overrides: object) -> AudioBus:
        values: dict[str, object] = {
            "audio_session_id": "audio-1",
            "provider_generation": 1,
            "stream_id": "stream-1",
            "device_epoch": 1,
            "capacity_frames": capacity_frames,
        }
        values.update(overrides)
        return AudioBus(**values)  # type: ignore[arg-type]

    def make_packet(
        self,
        *,
        sequence: int = 1,
        frame_index: int | None = None,
        payload: bytes = SILENCE_PAYLOAD,
        duration_ms: int = 20,
        **overrides: object,
    ) -> AudioPacket:
        values: dict[str, object] = {
            "audio_session_id": "audio-1",
            "provider_generation": 1,
            "stream_id": "stream-1",
            "event_sequence": sequence,
            "frame_index": sequence - 1 if frame_index is None else frame_index,
            "captured_at": "2026-09-30T08:00:00Z",
            "duration_ms": duration_ms,
            "device_epoch": 1,
            "sample_rate_hz": 16000,
            "channel_count": 1,
            "sample_format": "pcm_s16le",
            "sample_count": SAMPLE_COUNT,
            "payload_size_bytes": PAYLOAD_SIZE_BYTES,
        }
        values.update(overrides)
        return AudioPacket(AudioFrame(**values), payload)

    def make_config(self, **overrides: object) -> AutoCaptureConfig:
        values: dict[str, object] = {
            "vad": VadConfig(silence_duration_ms=60, frame_duration_ms=20),
            "passive_capacity_frames": 25,
            "speech_start": SpeechStartConfig(speech_frame_threshold=3, min_rms_dbfs=-45.0),
            "max_utterance_ms": 30000,
            "speech_start_timeout_ms": 8000,
        }
        values.update(overrides)
        return AutoCaptureConfig(**values)  # type: ignore[arg-type]

    def make_session(
        self,
        *,
        bus: AudioBus | None = None,
        config: AutoCaptureConfig | None = None,
        detector: object = is_not_silent,
        start_detector: object | None = None,
    ) -> AutoCaptureSession:
        return AutoCaptureSession(
            bus=self.make_bus() if bus is None else bus,
            config=self.make_config() if config is None else config,
            detector=detector,  # type: ignore[arg-type]
            start_detector=start_detector,  # type: ignore[arg-type]
        )

    def observe_frames(
        self,
        session: AutoCaptureSession,
        *,
        count: int,
        payload: bytes = SILENCE_PAYLOAD,
        first_sequence: int = 1,
    ) -> AutoCaptureDecision:
        decision = session.observe(self.make_packet(sequence=first_sequence, payload=payload))
        for offset in range(1, count):
            decision = session.observe(
                self.make_packet(sequence=first_sequence + offset, payload=payload)
            )
        self.assertIsNotNone(decision)
        return decision


class SpeechStartConfigTests(AutoCaptureTestCase):
    def test_defaults_are_declared_and_not_interpreted_as_asr_reliability(self) -> None:
        config = SpeechStartConfig()
        self.assertEqual(config.speech_frame_threshold, 3)
        self.assertTrue(config.reset_on_miss)
        self.assertIsNone(config.min_rms_dbfs)

    def test_threshold_must_be_positive_integer(self) -> None:
        for value in (0, -1, 1.5, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    SpeechStartConfig(speech_frame_threshold=value)  # type: ignore[arg-type]

    def test_reset_on_miss_must_be_boolean(self) -> None:
        for value in (0, 1, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    SpeechStartConfig(reset_on_miss=value)  # type: ignore[arg-type]

    def test_min_rms_dbfs_must_be_finite_number_or_none(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf"), True, "loud"):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    SpeechStartConfig(min_rms_dbfs=value)  # type: ignore[arg-type]
        self.assertEqual(SpeechStartConfig(min_rms_dbfs=-40).min_rms_dbfs, -40)
        self.assertEqual(SpeechStartConfig(min_rms_dbfs=-40.5).min_rms_dbfs, -40.5)

    def test_config_is_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            SpeechStartConfig().speech_frame_threshold = 5  # type: ignore[misc]


class SpeechStartDetectorTests(AutoCaptureTestCase):
    def make_detector(self, **overrides: object) -> SpeechStartDetector:
        values: dict[str, object] = {"speech_frame_threshold": 3, "min_rms_dbfs": -45.0}
        values.update(overrides)
        return SpeechStartDetector(config=SpeechStartConfig(**values))  # type: ignore[arg-type]

    def test_consecutive_loud_frames_start_speech_on_exact_threshold(self) -> None:
        detector = self.make_detector()
        first = detector.observe(self.make_packet(payload=LOUD_PAYLOAD))
        second = detector.observe(self.make_packet(sequence=2, payload=LOUD_PAYLOAD))
        self.assertFalse(first.started)
        self.assertFalse(second.started)
        self.assertEqual(second.speech_frames, 2)
        third = detector.observe(self.make_packet(sequence=3, payload=LOUD_PAYLOAD))
        self.assertTrue(third.started)
        self.assertTrue(third.observed)
        self.assertEqual(third.speech_frames, 3)
        self.assertEqual(third.observed_frames, 3)
        self.assertTrue(detector.started)
        self.assertEqual(detector.speech_frames, 3)
        self.assertEqual(detector.observed_frames, 3)
        self.assertEqual(detector.config.speech_frame_threshold, 3)

    def test_reset_on_miss_clears_progress(self) -> None:
        detector = self.make_detector()
        self.assertTrue(detector.observe(self.make_packet(payload=LOUD_PAYLOAD)).observed)
        miss = detector.observe(self.make_packet(sequence=2, payload=SILENCE_PAYLOAD))
        self.assertFalse(miss.observed)
        self.assertEqual(miss.speech_frames, 0)
        detector.observe(self.make_packet(sequence=3, payload=LOUD_PAYLOAD))
        detector.observe(self.make_packet(sequence=4, payload=LOUD_PAYLOAD))
        result = detector.observe(self.make_packet(sequence=5, payload=LOUD_PAYLOAD))
        self.assertTrue(result.started)
        self.assertEqual(result.speech_frames, 3)
        self.assertEqual(result.observed_frames, 5)

    def test_accumulate_mode_never_resets_progress(self) -> None:
        detector = self.make_detector(reset_on_miss=False)
        detector.observe(self.make_packet(payload=LOUD_PAYLOAD))
        detector.observe(self.make_packet(sequence=2, payload=SILENCE_PAYLOAD))
        detector.observe(self.make_packet(sequence=3, payload=SILENCE_PAYLOAD))
        result = detector.observe(self.make_packet(sequence=4, payload=LOUD_PAYLOAD))
        self.assertEqual(result.speech_frames, 2)
        self.assertFalse(result.started)
        result = detector.observe(self.make_packet(sequence=5, payload=LOUD_PAYLOAD))
        self.assertTrue(result.started)
        self.assertEqual(result.speech_frames, 3)

    def test_energy_gate_rejects_quiet_frames(self) -> None:
        detector = self.make_detector()
        quiet = detector.observe(self.make_packet(payload=QUIET_PAYLOAD))
        self.assertFalse(quiet.observed)
        self.assertEqual(quiet.speech_frames, 0)
        self.assertEqual(quiet.observed_frames, 1)
        silence = detector.observe(self.make_packet(sequence=2, payload=SILENCE_PAYLOAD))
        self.assertFalse(silence.observed)

    def test_disabled_energy_gate_treats_every_valid_frame_as_speech(self) -> None:
        detector = self.make_detector(min_rms_dbfs=None)
        detector.observe(self.make_packet(payload=SILENCE_PAYLOAD))
        detector.observe(self.make_packet(sequence=2, payload=SILENCE_PAYLOAD))
        result = detector.observe(self.make_packet(sequence=3, payload=SILENCE_PAYLOAD))
        self.assertTrue(result.observed)
        self.assertTrue(result.started)

    def test_invalid_payload_is_not_observed_and_never_starts(self) -> None:
        detector = self.make_detector(speech_frame_threshold=1)
        result = detector.observe(self.make_packet(payload=bytes(639)))
        self.assertFalse(result.observed)
        self.assertFalse(result.started)
        self.assertEqual(result.observed_frames, 0)
        self.assertEqual(result.speech_frames, 0)
        self.assertFalse(detector.started)
        odd = detector.observe(self.make_packet(sequence=2, payload=b"\x01\x00\x00"))
        self.assertFalse(odd.observed)
        self.assertEqual(odd.observed_frames, 0)

    def test_start_is_latched_until_reset(self) -> None:
        detector = self.make_detector(speech_frame_threshold=1)
        self.assertTrue(detector.observe(self.make_packet(payload=LOUD_PAYLOAD)).started)
        after_miss = detector.observe(self.make_packet(sequence=2, payload=SILENCE_PAYLOAD))
        self.assertTrue(after_miss.started)
        self.assertTrue(detector.started)
        detector.reset()
        self.assertFalse(detector.started)
        self.assertEqual(detector.speech_frames, 0)
        self.assertEqual(detector.observed_frames, 0)

    def test_decision_is_frozen_and_slotted(self) -> None:
        decision = SpeechStartDecision(observed=True, started=False, speech_frames=1, observed_frames=1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.started = True  # type: ignore[misc]
        self.assertFalse(hasattr(decision, "__dict__"))


class AutoCaptureConfigTests(AutoCaptureTestCase):
    def test_defaults_and_properties(self) -> None:
        vad = VadConfig()
        config = AutoCaptureConfig(vad=vad)
        self.assertIs(config.vad, vad)
        self.assertEqual(config.passive_capacity_frames, 25)
        self.assertEqual(config.max_utterance_ms, 30000)
        self.assertEqual(config.speech_start_timeout_ms, 8000)
        self.assertIsInstance(config.speech_start, SpeechStartConfig)
        self.assertEqual(config.speech_start.speech_frame_threshold, 3)

    def test_explicit_speech_start_and_capacity_are_preserved(self) -> None:
        speech_start = SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=None)
        config = AutoCaptureConfig(
            vad=VadConfig(silence_duration_ms=20, frame_duration_ms=10),
            passive_capacity_frames=4,
            speech_start=speech_start,
            max_utterance_ms=1000,
            speech_start_timeout_ms=500,
        )
        self.assertIs(config.speech_start, speech_start)
        self.assertEqual(config.passive_capacity_frames, 4)
        self.assertEqual(config.max_utterance_ms, 1000)
        self.assertEqual(config.speech_start_timeout_ms, 500)

    def test_numeric_bounds_fail_closed(self) -> None:
        for value in (0, -1, True, 1.5, "25"):
            with self.subTest(field="passive_capacity_frames", value=value):
                with self.assertRaises(ContractError):
                    AutoCaptureConfig(vad=VadConfig(), passive_capacity_frames=value)  # type: ignore[arg-type]
        for value in (0, -1, True):
            with self.subTest(field="max_utterance_ms", value=value):
                with self.assertRaises(ContractError):
                    AutoCaptureConfig(vad=VadConfig(), max_utterance_ms=value)  # type: ignore[arg-type]
            with self.subTest(field="speech_start_timeout_ms", value=value):
                with self.assertRaises(ContractError):
                    AutoCaptureConfig(vad=VadConfig(), speech_start_timeout_ms=value)  # type: ignore[arg-type]

    def test_vad_and_speech_start_types_are_required(self) -> None:
        with self.assertRaises(ContractError):
            AutoCaptureConfig(vad=object())  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            AutoCaptureConfig(vad=VadConfig(), speech_start=object())  # type: ignore[arg-type]


class AutoCapturePassiveTests(AutoCaptureTestCase):
    def test_frames_are_buffered_before_speech_without_entering_vad(self) -> None:
        session = self.make_session()
        decision = session.listen()
        self.assertEqual(decision.state, AutoCaptureState.LISTENING)
        self.assertEqual(decision.reason, None)
        self.assertEqual(decision.buffered_frames, 0)
        self.assertFalse(decision.speech_started)

        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        self.assertEqual(session.state, AutoCaptureState.LISTENING)
        self.assertEqual(session.buffered_frames, 2)
        self.assertEqual(session.captured_frames, 0)
        self.assertFalse(session.speech_started)
        snapshot = session.observe(self.make_packet(sequence=3, payload=SILENCE_PAYLOAD))
        self.assertEqual(snapshot.state, AutoCaptureState.LISTENING)
        self.assertEqual(snapshot.buffered_frames, 3)
        self.assertEqual(snapshot.captured_frames, 0)
        self.assertEqual(snapshot.to_details()["passive_decision"], "buffered")

    def test_passive_buffer_is_bounded_and_reports_overflow(self) -> None:
        session = self.make_session(config=self.make_config(passive_capacity_frames=2))
        session.listen()
        self.assertLessEqual(session.buffered_frames, 2)
        first = session.observe(self.make_packet(sequence=1))
        second = session.observe(self.make_packet(sequence=2))
        third = session.observe(self.make_packet(sequence=3))
        fourth = session.observe(self.make_packet(sequence=4))
        self.assertEqual(first.to_details()["passive_decision"], "buffered")
        self.assertEqual(second.to_details()["passive_decision"], "buffered")
        self.assertEqual(third.to_details()["passive_decision"], "overflow")
        self.assertEqual(fourth.to_details()["passive_decision"], "overflow")
        self.assertEqual(session.buffered_frames, 2)
        self.assertEqual(fourth.state, AutoCaptureState.LISTENING)
        self.assertEqual(fourth.reason, None)

    def test_overflow_never_starts_speech_or_fails_closed_by_itself(self) -> None:
        config = self.make_config(
            passive_capacity_frames=2,
            speech_start=SpeechStartConfig(speech_frame_threshold=3, min_rms_dbfs=-45.0),
        )
        session = self.make_session(config=config)
        session.listen()
        decision = self.observe_frames(session, count=6, payload=SILENCE_PAYLOAD)
        self.assertEqual(decision.state, AutoCaptureState.LISTENING)
        self.assertIsNone(session.terminal_reason)
        self.assertLessEqual(session.buffered_frames, 2)
        self.assertEqual(session.captured_frames, 0)

    def test_listen_is_idempotent(self) -> None:
        session = self.make_session()
        session.listen()
        again = session.listen()
        self.assertEqual(again.state, AutoCaptureState.LISTENING)
        self.assertEqual(session.state, AutoCaptureState.LISTENING)
        self.assertIsNone(session.terminal_reason)


class AutoCaptureDeliveryTests(AutoCaptureTestCase):
    def test_speech_start_replays_preroll_then_silence_ends_and_delivers_once(self) -> None:
        session = self.make_session()
        session.listen()
        started = self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.assertTrue(is_speech_started(started))
        self.assertTrue(started.speech_started)
        self.assertEqual(started.buffered_frames, 0)
        self.assertEqual(started.captured_frames, 3)
        self.assertEqual(started.emitted, ())

        self.observe_frames(session, count=2, payload=SILENCE_PAYLOAD, first_sequence=4)
        ended = session.observe(self.make_packet(sequence=6, payload=SILENCE_PAYLOAD))
        self.assertEqual(ended.state, AutoCaptureState.ENDED)
        self.assertEqual(ended.reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        self.assertEqual(ended.captured_frames, 6)
        self.assertEqual(ended.buffered_frames, 0)
        self.assertEqual([packet.frame.event_sequence for packet in ended.emitted], [1, 2, 3, 4, 5, 6])
        self.assertEqual([packet.frame.frame_index for packet in ended.emitted], [0, 1, 2, 3, 4, 5])
        self.assertEqual(ended.to_details()["emitted_frames"], "6")

        delivered = session.deliver()
        self.assertEqual(delivered, ended.emitted)
        self.assertEqual(session.deliver(), ())
        self.assertEqual(session.state, AutoCaptureState.ENDED)

    def test_delivered_sequences_are_strictly_increasing_without_gap_or_duplicate(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=4)
        delivered = session.deliver()
        self.assertEqual(len(delivered), 6)
        sequences = [packet.frame.event_sequence for packet in delivered]
        indexes = [packet.frame.frame_index for packet in delivered]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(sequences, list(range(sequences[0], sequences[0] + len(sequences))))
        self.assertEqual(indexes, list(range(indexes[0], indexes[0] + len(indexes))))
        self.assertEqual(session.deliver(), ())

    def test_finish_ends_session_and_delivers_captured_frames_once(self) -> None:
        config = self.make_config(speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0))
        session = self.make_session(config=config)
        session.listen()
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        self.assertEqual(session.state, AutoCaptureState.CAPTURING)
        decision = session.finish()
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertIsNone(decision.reason)
        self.assertIsNone(session.terminal_reason)
        self.assertEqual([packet.frame.event_sequence for packet in decision.emitted], [1, 2])
        delivered = session.deliver()
        self.assertEqual(delivered, decision.emitted)
        self.assertEqual(session.deliver(), ())

    def test_finish_before_speech_delivers_nothing(self) -> None:
        session = self.make_session()
        session.listen()
        decision = session.finish()
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertIsNone(decision.reason)
        self.assertEqual(decision.emitted, ())
        self.assertEqual(session.deliver(), ())
        self.assertEqual(session.captured_frames, 0)

    def test_deliver_before_end_returns_empty_and_keeps_session_alive(self) -> None:
        session = self.make_session()
        session.listen()
        self.assertEqual(session.deliver(), ())
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        self.assertEqual(session.state, AutoCaptureState.LISTENING)
        self.assertEqual(session.deliver(), ())
        started = session.observe(self.make_packet(sequence=3, payload=LOUD_PAYLOAD))
        self.assertEqual(started.state, AutoCaptureState.CAPTURING)
        self.assertEqual(session.deliver(), ())
        self.assertEqual(session.captured_frames, 3)

    def test_cancel_closes_bus_and_never_delivers(self) -> None:
        bus = self.make_bus()
        config = self.make_config(speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0))
        session = self.make_session(bus=bus, config=config)
        session.listen()
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        decision = session.cancel()
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.CANCELLED)
        self.assertEqual(decision.emitted, ())
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.terminal_reason, "cancelled")
        self.assertEqual(session.deliver(), ())


class AutoCaptureRejectionTests(AutoCaptureTestCase):
    def test_observe_before_listen_is_rejected_without_transition(self) -> None:
        session = self.make_session()
        decision = session.observe(self.make_packet(payload=LOUD_PAYLOAD))
        self.assertEqual(decision.state, AutoCaptureState.IDLE)
        self.assertEqual(decision.reason, AutoCaptureEndReason.INVALIDATED)
        self.assertEqual(decision.passive_decision, PassiveFrameDecision.INVALIDATED)
        self.assertEqual(session.state, AutoCaptureState.IDLE)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(session.captured_frames, 0)

    def test_observe_after_end_is_rejected_and_keeps_terminal_reason(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=4)
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        late = session.observe(self.make_packet(sequence=7, payload=LOUD_PAYLOAD))
        self.assertEqual(late.state, AutoCaptureState.ENDED)
        self.assertEqual(late.reason, AutoCaptureEndReason.SESSION_ENDED_INTERNAL)
        self.assertEqual(late.emitted, ())
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        self.assertEqual(session.captured_frames, 6)

    def test_stale_boundaries_fail_closed_before_speech(self) -> None:
        cases = (
            ({"audio_session_id": "audio-old"}, AutoCaptureEndReason.STALE_AUDIO_SESSION),
            ({"provider_generation": 2}, AutoCaptureEndReason.STALE_GENERATION),
            ({"stream_id": "stream-old"}, AutoCaptureEndReason.STALE_STREAM),
            ({"device_epoch": 2}, AutoCaptureEndReason.STALE_DEVICE_EPOCH),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                session = self.make_session()
                session.listen()
                self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
                decision = session.observe(self.make_packet(sequence=3, payload=LOUD_PAYLOAD, **overrides))
                self.assertEqual(decision.state, AutoCaptureState.FAILED)
                self.assertEqual(decision.reason, expected)
                self.assertEqual(session.terminal_reason, expected)
                self.assertEqual(session.buffered_frames, 0)
                self.assertEqual(session.captured_frames, 0)
                self.assertEqual(session.deliver(), ())

    def test_stale_boundary_after_speech_discards_captured_audio(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        decision = session.observe(
            self.make_packet(sequence=4, payload=LOUD_PAYLOAD, stream_id="stream-old")
        )
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.STALE_STREAM)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_invalid_payload_fails_closed(self) -> None:
        session = self.make_session()
        session.listen()
        decision = session.observe(self.make_packet(payload=bytes(639)))
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.INVALID_AUDIO_FRAME)
        self.assertEqual(decision.passive_decision, PassiveFrameDecision.INVALID_AUDIO_FRAME)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_sequence_gap_and_duplicate_fail_closed(self) -> None:
        gap = self.make_session()
        gap.listen()
        gap.observe(self.make_packet(sequence=1))
        decision = gap.observe(self.make_packet(sequence=3))
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)
        self.assertEqual(decision.passive_decision, PassiveFrameDecision.FRAME_DISCONTINUITY)

        duplicate = self.make_session()
        duplicate.listen()
        duplicate.observe(self.make_packet(sequence=1))
        decision = duplicate.observe(self.make_packet(sequence=1))
        self.assertEqual(decision.reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)

        misaligned = self.make_session()
        misaligned.listen()
        misaligned.observe(self.make_packet(sequence=1))
        decision = misaligned.observe(self.make_packet(sequence=2, frame_index=5))
        self.assertEqual(decision.reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)

    def test_first_observed_frame_is_the_session_ledger_baseline(self) -> None:
        session = self.make_session()
        session.listen()
        first = session.observe(self.make_packet(sequence=7))
        self.assertEqual(first.state, AutoCaptureState.LISTENING)
        self.assertEqual(first.buffered_frames, 1)
        accepted = session.observe(self.make_packet(sequence=8))
        self.assertEqual(accepted.state, AutoCaptureState.LISTENING)
        gap = session.observe(self.make_packet(sequence=10))
        self.assertEqual(gap.state, AutoCaptureState.FAILED)
        self.assertEqual(gap.reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)

    def test_mid_stream_join_still_starts_speech_and_delivers_original_frames(self) -> None:
        session = self.make_session()
        session.listen()
        started = self.observe_frames(session, count=3, payload=LOUD_PAYLOAD, first_sequence=7)
        self.assertEqual(started.state, AutoCaptureState.CAPTURING)
        self.assertTrue(started.speech_started)
        self.assertEqual(started.captured_frames, 3)
        ended = self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=10)
        self.assertEqual(ended.state, AutoCaptureState.ENDED)
        self.assertEqual(ended.reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        self.assertEqual(
            [packet.frame.event_sequence for packet in ended.emitted], [7, 8, 9, 10, 11, 12]
        )

    def test_overflow_then_speech_start_still_delivers_preroll_and_utterance(self) -> None:
        session = self.make_session(config=self.make_config(passive_capacity_frames=2))
        session.listen()
        self.observe_frames(session, count=5, payload=SILENCE_PAYLOAD)
        self.assertEqual(session.buffered_frames, 2)
        started = self.observe_frames(session, count=3, payload=LOUD_PAYLOAD, first_sequence=6)
        self.assertEqual(started.state, AutoCaptureState.CAPTURING)
        self.assertEqual(started.captured_frames, 2)
        self.assertEqual(started.emitted, ())
        ended = self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=9)
        self.assertEqual(ended.state, AutoCaptureState.ENDED)
        self.assertEqual(ended.reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        self.assertEqual(
            [packet.frame.event_sequence for packet in ended.emitted], [7, 8, 9, 10, 11]
        )
        self.assertEqual(session.deliver(), ended.emitted)
        self.assertEqual(session.deliver(), ())

    def test_vad_sees_a_renumbered_logical_view_while_delivery_keeps_originals(self) -> None:
        seen: list[AudioPacket] = []

        def detector(packet: AudioPacket) -> bool:
            seen.append(packet)
            return is_not_silent(packet)

        session = self.make_session(detector=detector)
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD, first_sequence=7)
        self.assertEqual([packet.frame.event_sequence for packet in seen], [1, 2, 3])
        self.assertEqual([packet.frame.frame_index for packet in seen], [0, 1, 2])
        self.assertEqual(seen[0].payload, LOUD_PAYLOAD)
        for packet in seen:
            self.assertEqual(packet.frame.audio_session_id, "audio-1")
            self.assertEqual(packet.frame.provider_generation, 1)
            self.assertEqual(packet.frame.stream_id, "stream-1")
            self.assertEqual(packet.frame.device_epoch, 1)
            self.assertEqual(packet.frame.duration_ms, 20)
            self.assertNotEqual(packet.frame.captured_at, "2026-09-30T08:00:00Z")
        ended = self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=10)
        delivered = session.deliver()
        self.assertEqual(delivered, ended.emitted)
        self.assertEqual([packet.frame.event_sequence for packet in delivered], [7, 8, 9, 10, 11, 12])
        self.assertEqual([packet.frame.frame_index for packet in delivered], [6, 7, 8, 9, 10, 11])
        self.assertEqual(delivered[0].frame.captured_at, "2026-09-30T08:00:00Z")

    def test_detector_unavailable_fails_closed_at_speech_start(self) -> None:
        session = self.make_session(detector=None)
        session.listen()
        decision = self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.DETECTOR_UNAVAILABLE)
        self.assertTrue(decision.speech_started)
        self.assertEqual(session.deliver(), ())
        self.assertEqual(session.captured_frames, 0)

    def test_detector_failure_and_non_boolean_fail_closed(self) -> None:
        def explode(packet: AudioPacket) -> bool:
            raise RuntimeError("vad")

        exploding = self.make_session(detector=explode)
        exploding.listen()
        decision = self.observe_frames(exploding, count=3, payload=LOUD_PAYLOAD)
        self.assertEqual(decision.reason, AutoCaptureEndReason.DETECTOR_FAILURE)
        self.assertEqual(exploding.deliver(), ())

        wrong_type = self.make_session(detector=lambda packet: 1)
        wrong_type.listen()
        decision = self.observe_frames(wrong_type, count=3, payload=LOUD_PAYLOAD)
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.DETECTOR_FAILURE)
        self.assertEqual(wrong_type.deliver(), ())

    def test_speech_start_timeout_ends_without_delivery(self) -> None:
        config = self.make_config(speech_start_timeout_ms=60)
        session = self.make_session(config=config)
        session.listen()
        early = self.observe_frames(session, count=2, payload=SILENCE_PAYLOAD)
        self.assertEqual(early.state, AutoCaptureState.LISTENING)
        decision = session.observe(self.make_packet(sequence=3, payload=SILENCE_PAYLOAD))
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.SPEECH_START_TIMEOUT)
        self.assertEqual(decision.emitted, ())
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SPEECH_START_TIMEOUT)
        self.assertEqual(session.deliver(), ())

    def test_speech_start_wins_on_the_timeout_boundary_frame(self) -> None:
        config = self.make_config(
            speech_start_timeout_ms=60,
            speech_start=SpeechStartConfig(speech_frame_threshold=3, min_rms_dbfs=None),
            vad=VadConfig(silence_duration_ms=1000, frame_duration_ms=20),
        )
        session = self.make_session(config=config, detector=lambda packet: True)
        session.listen()
        decision = self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD)
        self.assertEqual(decision.state, AutoCaptureState.CAPTURING)
        self.assertTrue(decision.speech_started)
        self.assertIsNone(session.terminal_reason)

    def test_max_utterance_duration_truncates_at_boundary(self) -> None:
        config = self.make_config(
            max_utterance_ms=100,
            speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0),
        )
        session = self.make_session(config=config)
        session.listen()
        decision = self.observe_frames(session, count=5, payload=LOUD_PAYLOAD)
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.MAX_UTTERANCE_DURATION)
        self.assertEqual(len(decision.emitted), 5)
        self.assertEqual(session.captured_frames, 5)
        self.assertEqual([packet.frame.event_sequence for packet in decision.emitted], [1, 2, 3, 4, 5])

    def test_max_utterance_duration_never_exceeds_the_cap(self) -> None:
        config = self.make_config(
            max_utterance_ms=90,
            speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0),
        )
        session = self.make_session(config=config)
        session.listen()
        decision = self.observe_frames(session, count=5, payload=LOUD_PAYLOAD)
        self.assertEqual(decision.reason, AutoCaptureEndReason.MAX_UTTERANCE_DURATION)
        self.assertEqual(len(decision.emitted), 4)
        total_ms = sum(packet.frame.duration_ms for packet in decision.emitted)
        self.assertLessEqual(total_ms, 90)
        self.assertEqual(session.deliver(), decision.emitted)

    def test_invalidate_before_delivery_clears_everything_and_blocks_further_input(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.assertEqual(session.state, AutoCaptureState.CAPTURING)
        decision = session.invalidate(reason="window_closed")
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.INVALIDATED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.INVALIDATED)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())
        late = session.observe(self.make_packet(sequence=4, payload=LOUD_PAYLOAD))
        self.assertEqual(late.state, AutoCaptureState.FAILED)
        self.assertEqual(late.reason, AutoCaptureEndReason.SESSION_INVALIDATED)
        self.assertEqual(late.emitted, ())
        self.assertEqual(session.deliver(), ())

    def test_invalidate_does_not_close_bus_nor_reuse_old_frames(self) -> None:
        bus = self.make_bus()
        session = self.make_session(bus=bus)
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        session.invalidate(reason="window_closed")
        self.assertEqual(bus.state, AudioBusState.OPEN)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())
        late = session.observe(self.make_packet(sequence=4, payload=LOUD_PAYLOAD))
        self.assertEqual(late.state, AutoCaptureState.FAILED)
        self.assertEqual(late.reason, AutoCaptureEndReason.SESSION_INVALIDATED)
        self.assertEqual(late.emitted, ())
        self.assertEqual(session.deliver(), ())

    def test_invalidate_after_end_blocks_delivery(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=4)
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        session.invalidate(reason="turn_invalidated")
        self.assertEqual(session.state, AutoCaptureState.FAILED)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_invalidate_requires_restricted_identifier_reason(self) -> None:
        session = self.make_session()
        session.listen()
        for reason in ("", "bad reason", "tab\t", None, 1):
            with self.subTest(reason=reason):
                with self.assertRaises(ContractError):
                    session.invalidate(reason=reason)  # type: ignore[arg-type]
        self.assertEqual(session.state, AutoCaptureState.LISTENING)
        self.assertIsNone(session.terminal_reason)

    def test_listen_on_closed_or_failed_bus_fails_closed(self) -> None:
        closed_bus = self.make_bus()
        closed_bus.close(reason="capture_stopped", discard=False)
        closed = self.make_session(bus=closed_bus)
        decision = closed.listen()
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.BUS_CLOSED)

        failed_bus = self.make_bus(capacity_frames=1)
        self.assertEqual(failed_bus.publish(self.make_packet(sequence=1).frame, bytes(PAYLOAD_SIZE_BYTES)), ACCEPTED)
        self.assertEqual(
            failed_bus.publish(self.make_packet(sequence=2).frame, bytes(PAYLOAD_SIZE_BYTES)),
            AudioBusDecision.OVERFLOW,
        )
        failed = self.make_session(bus=failed_bus)
        decision = failed.listen()
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.BUS_CLOSED)

    def test_observe_fails_closed_when_bus_dies_after_listen(self) -> None:
        bus = self.make_bus(capacity_frames=1)
        session = self.make_session(bus=bus)
        self.assertEqual(session.listen().state, AutoCaptureState.LISTENING)
        self.assertEqual(bus.publish(self.make_packet(sequence=1).frame, bytes(PAYLOAD_SIZE_BYTES)), ACCEPTED)
        self.assertEqual(
            bus.publish(self.make_packet(sequence=2).frame, bytes(PAYLOAD_SIZE_BYTES)),
            AudioBusDecision.OVERFLOW,
        )
        decision = session.observe(self.make_packet(sequence=1, payload=LOUD_PAYLOAD))
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.BUS_CLOSED)
        self.assertEqual(session.deliver(), ())


class AutoCaptureLifecycleTests(AutoCaptureTestCase):
    """终止/失效后的拒绝原因码、no-op 分支与边界截断覆盖。"""

    def test_rejection_reason_codes_are_frozen_strings(self) -> None:
        self.assertEqual(AutoCaptureEndReason.SESSION_CLOSED.value, "session_closed")
        self.assertEqual(AutoCaptureEndReason.SESSION_ENDED_INTERNAL.value, "session_ended")
        self.assertEqual(AutoCaptureEndReason.SESSION_INVALIDATED.value, "session_invalidated")

    def test_observe_after_finish_reports_session_closed(self) -> None:
        config = self.make_config(
            speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0)
        )
        session = self.make_session(config=config)
        session.listen()
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        finished = session.finish()
        self.assertEqual(finished.state, AutoCaptureState.ENDED)
        self.assertEqual(len(finished.emitted), 2)
        late = session.observe(self.make_packet(sequence=3, payload=LOUD_PAYLOAD))
        self.assertEqual(late.state, AutoCaptureState.ENDED)
        self.assertEqual(late.reason, AutoCaptureEndReason.SESSION_CLOSED)
        self.assertEqual(late.emitted, ())
        self.assertIsNone(session.terminal_reason)
        self.assertEqual([packet.frame.event_sequence for packet in session.deliver()], [1, 2])
        self.assertEqual(session.deliver(), ())

    def test_finish_in_idle_is_a_noop(self) -> None:
        session = self.make_session()
        decision = session.finish()
        self.assertEqual(decision.state, AutoCaptureState.IDLE)
        self.assertEqual(decision.reason, AutoCaptureEndReason.INVALIDATED)
        self.assertEqual(decision.emitted, ())
        self.assertEqual(session.state, AutoCaptureState.IDLE)
        self.assertIsNone(session.terminal_reason)
        self.assertEqual(session.listen().state, AutoCaptureState.LISTENING)

    def test_finish_in_failed_state_is_a_noop(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        session.observe(
            self.make_packet(sequence=3, payload=LOUD_PAYLOAD, stream_id="stream-old")
        )
        self.assertEqual(session.state, AutoCaptureState.FAILED)
        decision = session.finish()
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.SESSION_ENDED_INTERNAL)
        self.assertEqual(session.state, AutoCaptureState.FAILED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.STALE_STREAM)
        self.assertEqual(session.deliver(), ())

    def test_finish_in_ended_state_is_a_noop_and_keeps_delivery(self) -> None:
        session = self.make_session()
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=4)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        decision = session.finish()
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.SESSION_ENDED_INTERNAL)
        self.assertEqual(decision.emitted, ())
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        delivered = session.deliver()
        self.assertEqual([packet.frame.event_sequence for packet in delivered], [1, 2, 3, 4, 5, 6])
        self.assertEqual(session.deliver(), ())

    def test_cancel_after_finish_closes_bus_and_keeps_ended_semantics(self) -> None:
        bus = self.make_bus()
        config = self.make_config(
            speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0)
        )
        session = self.make_session(bus=bus, config=config)
        session.listen()
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        session.finish()
        self.assertEqual(bus.state, AudioBusState.OPEN)
        cancelled = session.cancel()
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.terminal_reason, "cancelled")
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        self.assertIsNone(session.terminal_reason)
        self.assertEqual(cancelled.state, AutoCaptureState.ENDED)
        self.assertEqual(cancelled.emitted, ())
        self.assertEqual(session.deliver(), ())
        again = session.cancel()
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        self.assertEqual(again.emitted, ())
        self.assertEqual(session.deliver(), ())

    def test_cancel_is_idempotent_after_cancelled_end(self) -> None:
        bus = self.make_bus()
        config = self.make_config(
            speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0)
        )
        session = self.make_session(bus=bus, config=config)
        session.listen()
        self.observe_frames(session, count=2, payload=LOUD_PAYLOAD)
        first = session.cancel()
        self.assertEqual(first.reason, AutoCaptureEndReason.CANCELLED)
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        second = session.cancel()
        self.assertEqual(second.state, AutoCaptureState.ENDED)
        self.assertEqual(second.reason, AutoCaptureEndReason.CANCELLED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.CANCELLED)
        self.assertEqual(session.deliver(), ())

    def test_listen_after_end_never_resurrects_the_session(self) -> None:
        bus = self.make_bus()
        session = self.make_session(bus=bus)
        session.listen()
        self.observe_frames(session, count=3, payload=LOUD_PAYLOAD)
        self.observe_frames(session, count=3, payload=SILENCE_PAYLOAD, first_sequence=4)
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        self.assertEqual(bus.state, AudioBusState.OPEN)
        again = session.listen()
        self.assertEqual(again.state, AutoCaptureState.ENDED)
        self.assertEqual(again.reason, AutoCaptureEndReason.SESSION_ENDED_INTERNAL)
        self.assertEqual(session.state, AutoCaptureState.ENDED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        self.assertEqual(session.captured_frames, 6)

    def test_invalidate_in_idle_blocks_before_any_listen(self) -> None:
        session = self.make_session()
        decision = session.invalidate(reason="window_closed")
        self.assertEqual(decision.state, AutoCaptureState.FAILED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.INVALIDATED)
        self.assertEqual(session.deliver(), ())
        observed = session.observe(self.make_packet(sequence=1, payload=LOUD_PAYLOAD))
        self.assertEqual(observed.state, AutoCaptureState.FAILED)
        self.assertEqual(observed.reason, AutoCaptureEndReason.SESSION_INVALIDATED)
        listened = session.listen()
        self.assertEqual(listened.state, AutoCaptureState.FAILED)
        self.assertEqual(listened.reason, AutoCaptureEndReason.SESSION_INVALIDATED)
        self.assertEqual(session.deliver(), ())

    def test_max_utterance_smaller_than_one_frame_truncates_to_zero_frames(self) -> None:
        config = self.make_config(
            max_utterance_ms=10,
            speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-45.0),
        )
        session = self.make_session(config=config)
        session.listen()
        decision = session.observe(self.make_packet(sequence=1, payload=LOUD_PAYLOAD))
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.MAX_UTTERANCE_DURATION)
        self.assertEqual(decision.emitted, ())
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_detector_missing_without_speech_start_times_out_instead_of_failing_early(self) -> None:
        config = self.make_config(speech_start_timeout_ms=60)
        session = self.make_session(config=config, detector=None)
        session.listen()
        early = self.observe_frames(session, count=2, payload=SILENCE_PAYLOAD)
        self.assertEqual(early.state, AutoCaptureState.LISTENING)
        self.assertIsNone(session.terminal_reason)
        decision = session.observe(self.make_packet(sequence=3, payload=SILENCE_PAYLOAD))
        self.assertEqual(decision.state, AutoCaptureState.ENDED)
        self.assertEqual(decision.reason, AutoCaptureEndReason.SPEECH_START_TIMEOUT)
        self.assertFalse(decision.speech_started)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.SPEECH_START_TIMEOUT)
        self.assertEqual(session.deliver(), ())


class AutoCaptureDecisionTests(AutoCaptureTestCase):
    def test_decision_is_frozen_and_slotted(self) -> None:
        decision = AutoCaptureDecision(
            state=AutoCaptureState.IDLE,
            reason=None,
            speech_started=False,
            buffered_frames=0,
            captured_frames=0,
            emitted=(),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.state = AutoCaptureState.ENDED  # type: ignore[misc]
        self.assertFalse(hasattr(decision, "__dict__"))

    def test_details_are_restricted_strings(self) -> None:
        session = self.make_session()
        session.listen()
        details = self.observe_frames(session, count=1, payload=LOUD_PAYLOAD).to_details()
        for key, value in details.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, str)
        self.assertEqual(
            set(details),
            {
                "state",
                "reason",
                "speech_started",
                "buffered_frames",
                "captured_frames",
                "emitted_frames",
                "passive_decision",
            },
        )
        self.assertEqual(details["state"], "listening")
        self.assertEqual(details["reason"], "none")
        self.assertEqual(details["speech_started"], "false")
        self.assertEqual(details["buffered_frames"], "1")
        self.assertEqual(details["captured_frames"], "0")
        self.assertEqual(details["emitted_frames"], "0")
        self.assertEqual(details["passive_decision"], "buffered")

    def test_passive_observation_is_frozen_and_slotted(self) -> None:
        observation = PassiveFrameObservation(
            decision=PassiveFrameDecision.BUFFERED, buffered_frames=1, speech_started=False
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            observation.buffered_frames = 2  # type: ignore[misc]
        self.assertFalse(hasattr(observation, "__dict__"))


if __name__ == "__main__":
    unittest.main()
