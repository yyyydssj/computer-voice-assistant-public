from __future__ import annotations

import struct
import sys
import types
import unittest
from unittest.mock import patch

from app.audio.windows.capture import (
    AudioEndpoint,
    choose_independent_endpoints,
    enumerate_pyaudiowpatch,
    summarize_pcm16,
)


LOOPBACK = AudioEndpoint(1, "Speakers (loopback)", "Windows WASAPI", True, 2, 48000.0)
MICROPHONE = AudioEndpoint(2, "USB Microphone", "Windows WASAPI", False, 1, 48000.0)


def pcm16(frames: list[tuple[int, ...]]) -> bytes:
    return b"".join(struct.pack("<" + "h" * len(frame), *frame) for frame in frames)


class CaptureSummaryTests(unittest.TestCase):
    def test_nonzero_active_window_and_quiet_post_window_are_valid(self) -> None:
        data = pcm16([(0,), (0,), (12000,), (12000,), (0,), (0,)])
        summary = summarize_pcm16(
            data,
            endpoint=MICROPHONE,
            sample_rate_hz=48000,
            channels=1,
            lead_frames=2,
            active_frames=2,
            post_frames=2,
        )
        self.assertTrue(summary.valid)
        self.assertIsNone(summary.reason)
        self.assertTrue(summary.active_nonzero)
        self.assertTrue(summary.post_quiet)
        self.assertEqual(summary.frames, 6)
        self.assertEqual(summary.to_dict()["endpoint"]["index"], 2)

    def test_all_zero_active_window_is_invalid_not_silence_evidence(self) -> None:
        data = pcm16([(1000,), (1000,), (0,), (0,), (0,), (0,)])
        summary = summarize_pcm16(
            data,
            endpoint=LOOPBACK,
            sample_rate_hz=48000,
            channels=1,
            lead_frames=2,
            active_frames=2,
            post_frames=2,
        )
        self.assertFalse(summary.valid)
        self.assertEqual(summary.reason, "active_capture_zero")
        self.assertFalse(summary.active_nonzero)
        self.assertTrue(summary.post_quiet)

    def test_multichannel_nonzero_signal_is_not_lost_by_mono_cancellation(self) -> None:
        data = pcm16([(0, 0), (1000, -1000), (0, 0)])
        summary = summarize_pcm16(
            data,
            endpoint=LOOPBACK,
            sample_rate_hz=48000,
            channels=2,
            lead_frames=1,
            active_frames=1,
            post_frames=1,
        )
        self.assertTrue(summary.valid)
        self.assertTrue(summary.active_nonzero)
        self.assertGreater(summary.active_rms_dbfs, -40.0)

    def test_invalid_and_insufficient_capture_windows_are_explicit(self) -> None:
        cases = (
            (b"not pcm", 1, 48000, 1, 1, 1, "unaligned_pcm"),
            (b"", 1, 48000, 1, 1, 1, "insufficient_frames"),
            (b"\x00\x00\x00\x00\x00\x00", 0, 48000, 1, 1, 1, "invalid_capture_input"),
            (b"\x00\x00\x00\x00\x00\x00", 1, 0, 1, 1, 1, "invalid_capture_input"),
            (b"\x00\x00\x00\x00\x00\x00", 1, 48000, 0, 1, 1, "invalid_capture_input"),
        )
        for data, channels, sample_rate, lead, active, post, reason in cases:
            with self.subTest(reason=reason):
                summary = summarize_pcm16(
                    data,
                    endpoint=MICROPHONE,
                    sample_rate_hz=sample_rate,
                    channels=channels,
                    lead_frames=lead,
                    active_frames=active,
                    post_frames=post,
                )
                self.assertFalse(summary.valid)
                self.assertEqual(summary.reason, reason)

    def test_negative_or_zero_window_lengths_are_rejected(self) -> None:
        data = pcm16([(1000,), (1000,), (0,)])
        for kwargs in (
            {"lead_frames": -1, "active_frames": 1, "post_frames": 1},
            {"lead_frames": 1, "active_frames": 0, "post_frames": 1},
            {"lead_frames": 1, "active_frames": 1, "post_frames": -1},
        ):
            with self.subTest(kwargs=kwargs):
                summary = summarize_pcm16(
                    data,
                    endpoint=MICROPHONE,
                    sample_rate_hz=48000,
                    channels=1,
                    **kwargs,
                )
                self.assertFalse(summary.valid)
                self.assertEqual(summary.reason, "invalid_capture_input")


class EndpointSelectionTests(unittest.TestCase):
    def test_choose_independent_endpoints_prefers_real_microphone_over_stereo_mix(self) -> None:
        stereo_mix = AudioEndpoint(3, "Stereo Mix", "Windows WASAPI", False, 2, 48000.0)
        loopback, microphone = choose_independent_endpoints((stereo_mix, LOOPBACK, MICROPHONE))
        self.assertEqual(loopback, LOOPBACK)
        self.assertEqual(microphone, MICROPHONE)

    def test_stereo_mix_is_not_accepted_as_the_only_microphone(self) -> None:
        stereo_mix = AudioEndpoint(3, "Stereo Mix (Realtek)", "Windows WASAPI", False, 2, 48000.0)
        with self.assertRaisesRegex(RuntimeError, "independent_microphone_endpoint_missing"):
            choose_independent_endpoints((LOOPBACK, stereo_mix))

    def test_choose_independent_endpoints_rejects_non_wasapi_candidates(self) -> None:
        mme_loopback = AudioEndpoint(9, "MME loopback", "0", True, 2, 48000.0)
        mme_microphone = AudioEndpoint(10, "MME microphone", "0", False, 2, 48000.0)
        with self.assertRaisesRegex(RuntimeError, "wasapi_loopback_endpoint_missing"):
            choose_independent_endpoints((mme_loopback, mme_microphone))

    def test_endpoint_keeps_native_format_metadata(self) -> None:
        endpoint = AudioEndpoint(13, "Speakers [Loopback]", "2", True, 2, 48000.0, host_api_index=2)
        self.assertEqual(endpoint.native_channels, 2)
        self.assertEqual(endpoint.native_sample_rate_hz, 48000)
        self.assertEqual(endpoint.host_api_index, 2)

    def test_choose_independent_endpoints_reports_missing_or_colliding_inputs(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "wasapi_loopback_endpoint_missing"):
            choose_independent_endpoints((MICROPHONE,))
        with self.assertRaisesRegex(RuntimeError, "independent_microphone_endpoint_missing"):
            choose_independent_endpoints((LOOPBACK,))
        same_index_mic = AudioEndpoint(1, "Microphone", "Windows WASAPI", False, 1, 48000.0)
        with self.assertRaisesRegex(RuntimeError, "capture_endpoints_not_independent"):
            choose_independent_endpoints((LOOPBACK, same_index_mic))


class EndpointEnumerationTests(unittest.TestCase):
    def test_missing_optional_dependency_is_explicit(self) -> None:
        with patch.dict(sys.modules, {"pyaudiowpatch": None}):
            with self.assertRaisesRegex(RuntimeError, "pyaudiowpatch_not_installed"):
                enumerate_pyaudiowpatch()

    def test_enumeration_uses_fake_dependency_without_opening_real_audio(self) -> None:
        class FakePyAudio:
            def __enter__(self) -> "FakePyAudio":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def get_device_count(self) -> int:
                return 2

            def get_device_info_by_index(self, index: int) -> dict[str, object]:
                return (
                    {
                        "name": "Speakers (loopback)",
                        "hostApi": 2,
                        "isLoopbackDevice": 1,
                        "maxInputChannels": 2,
                        "defaultSampleRate": 48000.0,
                    },
                    {
                        "name": "USB Microphone",
                        "hostApi": "Windows WASAPI",
                        "isLoopbackDevice": False,
                        "maxInputChannels": 1,
                        "defaultSampleRate": 44100,
                    },
                )[index]

        fake_module = types.SimpleNamespace(PyAudio=FakePyAudio)
        with patch.dict(sys.modules, {"pyaudiowpatch": fake_module}):
            endpoints = enumerate_pyaudiowpatch()
        self.assertEqual(len(endpoints), 2)
        self.assertEqual(endpoints[0].host_api, "2")
        self.assertTrue(endpoints[0].is_loopback)
        self.assertEqual(endpoints[1].default_sample_rate, 44100.0)


if __name__ == "__main__":
    unittest.main()