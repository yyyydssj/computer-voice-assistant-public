from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audio import AudioBus, AudioPacket
from app.asr import (
    AsrEventKind,
    AsrProviderUnavailable,
    DetectionStatus,
    SenseVoiceConfig,
    SenseVoiceRecognizerAdapter,
    SenseVoiceSessionState,
)
from app.contracts import AudioFrame


class FakeStream:
    def __init__(self) -> None:
        self.waveforms: list[tuple[int, list[float]]] = []
        self.result = types.SimpleNamespace(text="  你好，世界。  ")

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        self.waveforms.append((sample_rate, samples))


class FakeRecognizer:
    def __init__(self) -> None:
        self.stream = FakeStream()
        self.decode_calls = 0

    def create_stream(self) -> FakeStream:
        return self.stream

    def decode(self, stream: FakeStream) -> None:
        self.decode_calls += 1

    @property
    def result(self) -> object:
        return types.SimpleNamespace(text="  你好，世界。  ")


class FakeOfflineRecognizer:
    calls: list[dict[str, object]] = []
    instance = FakeRecognizer()

    @classmethod
    def from_sense_voice(cls, **kwargs: object) -> FakeRecognizer:
        cls.calls.append(kwargs)
        return cls.instance


class SenseVoiceAdapterTests(unittest.TestCase):
    def make_config(self) -> SenseVoiceConfig:
        return SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt"))

    def make_module(self) -> types.ModuleType:
        module = types.ModuleType("sherpa_onnx")
        module.OfflineRecognizer = FakeOfflineRecognizer  # type: ignore[attr-defined]
        return module

    def make_bus(self) -> AudioBus:
        return AudioBus(
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            device_epoch=1,
            capacity_frames=8,
        )

    def make_packet(self, *, sequence: int = 1, frame_index: int = 0, **overrides: object) -> AudioPacket:
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
        return AudioPacket(AudioFrame(**values), bytes(640))

    def test_capabilities_keep_unsupported_features_distinct(self) -> None:
        adapter = SenseVoiceRecognizerAdapter(self.make_config(), module_loader=lambda: self.make_module())
        capabilities = adapter.capabilities
        self.assertFalse(capabilities.supports_interim)
        self.assertTrue(capabilities.supports_final)
        self.assertEqual(capabilities.overlap_detection, DetectionStatus.UNSUPPORTED)
        self.assertEqual(capabilities.speaker_identity, DetectionStatus.UNSUPPORTED)
        self.assertEqual(capabilities.confidence, DetectionStatus.UNSUPPORTED)

    def test_load_is_lazy_and_uses_sense_voice_factory(self) -> None:
        FakeOfflineRecognizer.calls.clear()
        adapter = SenseVoiceRecognizerAdapter(self.make_config(), module_loader=lambda: self.make_module())
        self.assertEqual(adapter.state, SenseVoiceSessionState.UNLOADED)
        adapter.load()
        self.assertEqual(adapter.state, SenseVoiceSessionState.READY)
        self.assertEqual(FakeOfflineRecognizer.calls[0]["model"], "model.onnx")
        self.assertEqual(FakeOfflineRecognizer.calls[0]["tokens"], "tokens.txt")
        self.assertEqual(FakeOfflineRecognizer.calls[0]["sample_rate"], 16000)

    def test_missing_runtime_is_reported_without_import_side_effect(self) -> None:
        adapter = SenseVoiceRecognizerAdapter(
            self.make_config(),
            module_loader=lambda: (_ for _ in ()).throw(ModuleNotFoundError("sherpa_onnx")),
        )
        with self.assertRaises(AsrProviderUnavailable):
            adapter.load()
        self.assertEqual(adapter.state, SenseVoiceSessionState.FAILED)

    def test_feed_converts_pcm_and_finish_emits_provider_final_only(self) -> None:
        FakeOfflineRecognizer.instance = FakeRecognizer()
        adapter = SenseVoiceRecognizerAdapter(self.make_config(), module_loader=lambda: self.make_module())
        adapter.load()
        session = adapter.start_session(
            bus=self.make_bus(),
            utterance_id="utterance-1",
        )
        payload = (1).to_bytes(2, "little", signed=True) + (-2).to_bytes(2, "little", signed=True) + bytes(636)
        packet = self.make_packet()
        packet = AudioPacket(packet.frame, payload)
        self.assertTrue(session.feed(packet))
        self.assertEqual(FakeOfflineRecognizer.instance.stream.waveforms[0][0], 16000)
        self.assertAlmostEqual(FakeOfflineRecognizer.instance.stream.waveforms[0][1][0], 1 / 32768, places=7)
        event = session.finish()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.kind, AsrEventKind.FINAL)
        self.assertEqual(event.text, "你好，世界。")
        self.assertTrue(event.is_final)
        self.assertEqual(event.overlap_detection, DetectionStatus.UNSUPPORTED)
        self.assertEqual(event.utterance_id, "utterance-1")
        self.assertEqual(session.state, SenseVoiceSessionState.ENDED)

    def test_duplicate_or_gap_fails_closed_and_does_not_decode(self) -> None:
        FakeOfflineRecognizer.instance = FakeRecognizer()
        adapter = SenseVoiceRecognizerAdapter(self.make_config(), module_loader=lambda: self.make_module())
        adapter.load()
        session = adapter.start_session(bus=self.make_bus(), utterance_id="utterance-1")
        self.assertTrue(session.feed(self.make_packet()))
        self.assertFalse(session.feed(self.make_packet(sequence=1, frame_index=0)))
        self.assertEqual(session.state, SenseVoiceSessionState.FAILED)
        self.assertIsNone(session.finish())
        self.assertEqual(FakeOfflineRecognizer.instance.decode_calls, 0)

    def test_stale_packet_is_rejected_without_final_result(self) -> None:
        adapter = SenseVoiceRecognizerAdapter(self.make_config(), module_loader=lambda: self.make_module())
        adapter.load()
        session = adapter.start_session(bus=self.make_bus(), utterance_id="utterance-1")
        self.assertFalse(session.feed(self.make_packet(stream_id="old-stream")))
        self.assertEqual(session.state, SenseVoiceSessionState.FAILED)
        self.assertIsNone(session.finish())

    def test_cancel_closes_session_and_late_packets_are_rejected(self) -> None:
        adapter = SenseVoiceRecognizerAdapter(self.make_config(), module_loader=lambda: self.make_module())
        adapter.load()
        session = adapter.start_session(bus=self.make_bus(), utterance_id="utterance-1")
        session.cancel()
        self.assertEqual(session.state, SenseVoiceSessionState.CANCELLED)
        self.assertFalse(session.feed(self.make_packet()))
        self.assertIsNone(session.finish())

    def test_config_rejects_non_pcm_shape(self) -> None:
        with self.assertRaises(ValueError):
            SenseVoiceConfig(
                model_path=Path("model.onnx"),
                tokens_path=Path("tokens.txt"),
                sample_rate_hz=8000,
            )


if __name__ == "__main__":
    unittest.main()
