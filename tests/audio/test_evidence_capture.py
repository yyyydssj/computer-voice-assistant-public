from __future__ import annotations

import struct
import threading
import unittest

from app.audio.windows.capture import AudioEndpoint
from app.audio.windows.evidence_capture import (
    EvidenceCaptureConfig,
    capture_summary_from_stream,
    collect_pyaudiowpatch_summaries,
    collect_serial_summaries,
)


class FakeStream:
    def __init__(self, pcm: bytes, *, chunk_frames: int = 7) -> None:
        self._pcm = pcm
        self._chunk_frames = chunk_frames
        self._offset = 0
        self.read_started = threading.Event()
        self.read_calls: list[int] = []
        self.stop_calls = 0
        self.close_calls = 0

    def read(self, num_frames: int, exception_on_overflow: bool = False) -> bytes:
        del exception_on_overflow
        self.read_started.set()
        self.read_calls.append(num_frames)
        frame_width = 2
        take_frames = min(num_frames, self._chunk_frames)
        start = self._offset
        end = min(len(self._pcm), start + take_frames * frame_width)
        self._offset = end
        return self._pcm[start:end]

    def stop_stream(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def pcm16(values: list[int]) -> bytes:
    return struct.pack("<" + "h" * len(values), *values)


def endpoint(index: int, *, loopback: bool, name: str) -> AudioEndpoint:
    return AudioEndpoint(
        index=index,
        name=name,
        host_api="Windows WASAPI",
        is_loopback=loopback,
        max_input_channels=1,
        default_sample_rate=1_000,
    )


class EvidenceCaptureConfigTests(unittest.TestCase):
    def test_window_is_finite_and_converts_to_frames(self) -> None:
        config = EvidenceCaptureConfig(
            sample_rate_hz=1_000,
            channels=1,
            lead_in_ms=10,
            active_ms=20,
            post_ms=10,
            frames_per_buffer=7,
        )
        self.assertEqual(config.total_ms, 40)
        self.assertEqual(config.total_frames, 40)
        self.assertEqual(config.max_total_ms, 60_000)

    def test_window_cannot_exceed_explicit_bound(self) -> None:
        with self.assertRaisesRegex(ValueError, "capture_window_exceeds_bound"):
            EvidenceCaptureConfig(
                lead_in_ms=100,
                active_ms=100,
                post_ms=100,
                max_total_ms=200,
            )


class EndpointFormatResolutionTests(unittest.TestCase):
    def test_loopback_uses_advertised_two_channel_48khz_format(self) -> None:
        endpoint = AudioEndpoint(
            13,
            "Speakers [Loopback]",
            "2",
            True,
            2,
            48_000.0,
            host_api_index=2,
        )
        config = EvidenceCaptureConfig(sample_rate_hz=48_000, channels=1)
        resolved = config.for_endpoint(endpoint)
        self.assertEqual(resolved.channels, 2)
        self.assertEqual(resolved.sample_rate_hz, 48_000)

    def test_invalid_native_format_is_rejected_fail_closed(self) -> None:
        endpoint = AudioEndpoint(13, "Broken loopback", "2", True, 0, 0.0)
        config = EvidenceCaptureConfig()
        with self.assertRaisesRegex(ValueError, "endpoint_native_format_missing"):
            config.for_endpoint(endpoint)


class FakeStreamCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = EvidenceCaptureConfig(
            sample_rate_hz=1_000,
            channels=1,
            lead_in_ms=10,
            active_ms=20,
            post_ms=10,
            frames_per_buffer=7,
        )
        self.loopback = endpoint(1, loopback=True, name="Speakers (WASAPI loopback)")
        self.microphone = endpoint(2, loopback=False, name="USB microphone")

    def test_fake_stream_produces_summary_metadata_and_post_quiet(self) -> None:
        stream = FakeStream(pcm16([0] * 10 + [1_000] * 20 + [0] * 10))
        result = capture_summary_from_stream(
            stream,
            endpoint=self.loopback,
            config=self.config,
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.read_frames, 40)
        self.assertTrue(result.started)
        self.assertTrue(result.stopped)
        self.assertTrue(result.cleanup_ok)
        self.assertIsNotNone(result.summary)
        assert result.summary is not None
        self.assertEqual(result.summary.endpoint.name, "Speakers (WASAPI loopback)")
        self.assertTrue(result.summary.active_nonzero)
        self.assertTrue(result.summary.post_quiet)
        self.assertGreaterEqual(len(stream.read_calls), 6)
        self.assertEqual(stream.stop_calls, 1)
        self.assertEqual(stream.close_calls, 1)

    def test_all_zero_active_window_is_invalid_not_silence_evidence(self) -> None:
        stream = FakeStream(pcm16([0] * 40))
        result = capture_summary_from_stream(
            stream,
            endpoint=self.microphone,
            config=self.config,
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "active_capture_zero")
        self.assertIsNotNone(result.summary)
        assert result.summary is not None
        self.assertFalse(result.summary.valid)
        self.assertFalse(result.summary.active_nonzero)
        self.assertEqual(result.summary.reason, "active_capture_zero")
        self.assertEqual(result.summary.post_quiet, True)

    def test_serial_collection_never_overlaps_streams_and_keeps_endpoint_metadata(self) -> None:
        streams = {
            1: FakeStream(pcm16([0] * 10 + [1_000] * 20 + [0] * 10)),
            2: FakeStream(pcm16([0] * 10 + [500] * 20 + [0] * 10)),
        }
        opened: list[int] = []
        active_streams = 0
        peak_active_streams = 0

        def factory(item: AudioEndpoint, config: EvidenceCaptureConfig) -> FakeStream:
            nonlocal active_streams, peak_active_streams
            self.assertIs(config, self.config)
            opened.append(item.index)
            active_streams += 1
            peak_active_streams = max(peak_active_streams, active_streams)
            stream = streams[item.index]
            original_close = stream.close

            def close() -> None:
                nonlocal active_streams
                original_close()
                active_streams -= 1

            stream.close = close  # type: ignore[method-assign]
            return stream

        report = collect_serial_summaries(
            self.loopback,
            self.microphone,
            stream_factory=factory,
            config=self.config,
        )

        self.assertTrue(report.valid)
        self.assertFalse(report.blocked)
        self.assertEqual(opened, [1, 2])
        self.assertEqual(peak_active_streams, 1)
        self.assertEqual(active_streams, 0)
        self.assertEqual(report.loopback.endpoint.host_api, "Windows WASAPI")
        self.assertEqual(report.microphone.endpoint.name, "USB microphone")
        self.assertEqual(report.to_dict()["serial"], True)

    def test_zero_active_window_blocks_the_combined_report(self) -> None:
        streams = {
            1: FakeStream(pcm16([0] * 40)),
            2: FakeStream(pcm16([0] * 10 + [500] * 20 + [0] * 10)),
        }
        report = collect_serial_summaries(
            self.loopback,
            self.microphone,
            stream_factory=lambda item, config: streams[item.index],
            config=self.config,
        )

        self.assertFalse(report.valid)
        self.assertTrue(report.blocked)
        self.assertIn("loopback:active_capture_zero", report.blocked_reasons)

    def test_post_window_waits_for_playback_cleanup_gate(self) -> None:
        gate = threading.Event()

        class GateStream(FakeStream):
            def read(self, num_frames: int, exception_on_overflow: bool = False) -> bytes:
                if self._offset >= 30 * 2 and not gate.is_set():
                    gate.wait(1)
                return super().read(num_frames, exception_on_overflow)

        stream = GateStream(pcm16([0] * 10 + [500] * 20 + [0] * 20))
        result_box = {}

        def collect() -> None:
            result_box["result"] = capture_summary_from_stream(
                stream,
                endpoint=self.loopback,
                config=self.config,
                post_gate=gate,
            )

        thread = threading.Thread(target=collect)
        thread.start()
        self.assertTrue(stream.read_started.wait(1))
        self.assertTrue(thread.is_alive())
        gate.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result_box["result"].valid)

    def test_invalid_endpoint_pair_is_blocked_without_opening_streams(self) -> None:
        opened: list[int] = []
        same = endpoint(1, loopback=True, name="same")
        report = collect_serial_summaries(
            same,
            same,
            stream_factory=lambda item, config: opened.append(item.index),  # type: ignore[return-value]
            config=self.config,
        )

        self.assertFalse(report.valid)
        self.assertEqual(report.blocked_reasons, ("capture_endpoints_not_independent",))
        self.assertEqual(opened, [])


class OptionalDeviceCaptureTests(unittest.TestCase):
    def test_default_path_is_blocked_without_importing_or_touching_devices(self) -> None:
        def forbidden_import(name: str) -> object:
            raise AssertionError(f"unexpected import: {name}")

        report = collect_pyaudiowpatch_summaries(import_module=forbidden_import)

        self.assertTrue(report.blocked)
        self.assertFalse(report.valid)
        self.assertEqual(report.blocked_reasons, ("device_io_not_enabled",))
        self.assertFalse(report.dependency.device_io_enabled)

    def test_missing_optional_dependency_is_a_clear_blocked_result(self) -> None:
        def missing_import(name: str) -> object:
            raise ModuleNotFoundError(name)

        report = collect_pyaudiowpatch_summaries(
            enable_device_io=True,
            import_module=missing_import,
        )

        self.assertTrue(report.blocked)
        self.assertEqual(report.dependency.name, "pyaudiowpatch")
        self.assertFalse(report.dependency.available)
        self.assertEqual(report.dependency.reason, "pyaudiowpatch_not_installed")
        self.assertEqual(report.blocked_reasons, ("pyaudiowpatch_not_installed",))


if __name__ == "__main__":
    unittest.main()

class WindowsPackageExportsTests(unittest.TestCase):
    def test_windows_package_exports_capture_and_evidence_apis(self) -> None:
        import app.audio.windows as windows

        for name in (
            "AudioEndpoint",
            "CaptureSummary",
            "EvidenceCaptureConfig",
            "EvidenceCaptureReport",
            "capture_summary_from_stream",
            "collect_pyaudiowpatch_summaries",
            "collect_serial_summaries",
        ):
            with self.subTest(name=name):
                self.assertIn(name, windows.__all__)
                self.assertTrue(hasattr(windows, name))

    def test_fake_backend_preserves_missing_endpoint_reason(self) -> None:
        class FakePyAudio:
            def get_device_count(self) -> int:
                return 1

            def get_device_info_by_index(self, index: int) -> dict[str, object]:
                return {
                    "name": "USB microphone",
                    "hostApi": "Windows WASAPI",
                    "isLoopbackDevice": False,
                    "maxInputChannels": 1,
                    "defaultSampleRate": 48_000.0,
                }

            def terminate(self) -> None:
                pass

        class FakeModule:
            paInt16 = 8
            PyAudio = FakePyAudio

        report = collect_pyaudiowpatch_summaries(
            enable_device_io=True,
            pyaudio_module=FakeModule,
        )

        self.assertTrue(report.blocked)
        self.assertEqual(report.blocked_reasons, ("wasapi_loopback_endpoint_missing",))
        self.assertEqual(report.dependency.reason, "wasapi_loopback_endpoint_missing")
