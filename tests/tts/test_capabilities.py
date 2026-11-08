from __future__ import annotations

import math
import unittest

from app.tts import (
    AudioFormat,
    CapabilityDecision,
    CapabilityError,
    ProviderCapability,
    ProviderCapabilityRegistry,
    ProviderCapabilityRequest,
)


class ProviderCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pcm = AudioFormat("pcm_s16le", 24000, 1)
        self.opus = AudioFormat("opus", 48000, 2)
        self.capability = ProviderCapability(
            provider_name="voicestudio",
            supports_text_stream=True,
            supports_audio_stream=True,
            supports_cancellation=True,
            supports_voice_switching=False,
            audio_formats=(self.pcm, self.opus),
            estimated_first_audio_latency_ms=350,
        )

    def test_audio_format_rejects_empty_or_invalid_sampling_parameters(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "format_name"):
            AudioFormat("", 24000, 1)
        with self.assertRaisesRegex(CapabilityError, "sample_rate_hz"):
            AudioFormat("pcm_s16le", 0, 1)
        with self.assertRaisesRegex(CapabilityError, "sample_rate_hz"):
            AudioFormat("pcm_s16le", -1, 1)
        with self.assertRaisesRegex(CapabilityError, "sample_rate_hz"):
            AudioFormat("pcm_s16le", 192001, 1)
        with self.assertRaisesRegex(CapabilityError, "channel_count"):
            AudioFormat("pcm_s16le", 24000, 0)
        with self.assertRaisesRegex(CapabilityError, "channel_count"):
            AudioFormat("pcm_s16le", 24000, 9)

    def test_capability_rejects_duplicate_or_inconsistent_audio_formats(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "duplicate audio format"):
            ProviderCapability("provider-1", True, True, False, False, (self.pcm, self.pcm), 0)
        with self.assertRaisesRegex(CapabilityError, "at least one audio format"):
            ProviderCapability("provider-1", True, True, False, False, (), 0)
        with self.assertRaisesRegex(CapabilityError, "requires audio stream support"):
            ProviderCapability("provider-1", True, False, False, False, (self.pcm,), 0)

    def test_capability_rejects_negative_bool_or_unbounded_latency(self) -> None:
        with self.assertRaisesRegex(CapabilityError, "estimated_first_audio_latency_ms"):
            ProviderCapability("provider-1", True, False, False, False, (), -1)
        with self.assertRaisesRegex(CapabilityError, "estimated_first_audio_latency_ms"):
            ProviderCapability("provider-1", True, False, False, False, (), math.inf)  # type: ignore[arg-type]
        with self.assertRaisesRegex(CapabilityError, "estimated_first_audio_latency_ms"):
            ProviderCapability("provider-1", True, False, False, False, (), 300001)
        with self.assertRaisesRegex(CapabilityError, "supports_text_stream"):
            ProviderCapability("provider-1", 1, False, False, False, (), 0)  # type: ignore[arg-type]

    def test_registry_rejects_unknown_and_duplicate_provider(self) -> None:
        registry = ProviderCapabilityRegistry((self.capability,))
        self.assertEqual(registry.registered_provider_names(), ("voicestudio",))
        with self.assertRaisesRegex(CapabilityError, "未知 Provider"):
            registry.get("edge_tts")
        with self.assertRaisesRegex(CapabilityError, "重复注册"):
            registry.register(self.capability)

    def test_compatible_request_is_only_a_local_capability_judgement(self) -> None:
        registry = ProviderCapabilityRegistry((self.capability,))
        request = ProviderCapabilityRequest(
            require_text_stream=True,
            require_audio_stream=True,
            require_cancellation=True,
            audio_format=self.pcm,
        )
        result = registry.check("voicestudio", request)
        self.assertEqual(result.decision, CapabilityDecision.COMPATIBLE)
        self.assertTrue(result.compatible)
        self.assertEqual(result.reasons, ())
        registry.assert_compatible("voicestudio", request)

    def test_undeclared_voice_switching_is_rejected(self) -> None:
        request = ProviderCapabilityRequest(require_voice_switching=True)
        result = self.capability.check_compatibility(request)
        self.assertFalse(result.compatible)
        self.assertIn("voice switching", result.reasons[0])
        with self.assertRaisesRegex(CapabilityError, "voice switching"):
            self.capability.assert_compatible(request)

    def test_undeclared_cancellation_and_audio_format_are_rejected(self) -> None:
        capability = ProviderCapability("edge_tts", True, True, False, False, (self.pcm,), 100)
        request = ProviderCapabilityRequest(
            require_audio_stream=True,
            require_cancellation=True,
            audio_format=self.opus,
        )
        result = capability.check_compatibility(request)
        self.assertEqual(result.decision, CapabilityDecision.INCOMPATIBLE)
        self.assertIn("cancellation", result.reasons[0])
        self.assertIn("audio format", result.reasons[1])
        with self.assertRaisesRegex(CapabilityError, "incompatible"):
            capability.assert_compatible(request)

    def test_audio_format_requires_an_audio_stream_request(self) -> None:
        request = ProviderCapabilityRequest(audio_format=self.pcm)
        result = self.capability.check_compatibility(request)
        self.assertFalse(result.compatible)
        self.assertIn("audio_stream", result.reasons[0])

    def test_request_and_result_are_strict_and_immutable(self) -> None:
        request = ProviderCapabilityRequest(require_text_stream=True)
        with self.assertRaises(AttributeError):
            request.require_text_stream = False  # type: ignore[misc]
        with self.assertRaises(CapabilityError):
            ProviderCapabilityRequest(require_audio_stream="yes")  # type: ignore[arg-type]
        with self.assertRaises(CapabilityError):
            ProviderCapabilityRequest(audio_format="pcm_s16le")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
