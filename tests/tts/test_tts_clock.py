"""Offline tests for the shared default clock (``app.tts.clock``).

Everything here is offline: no socket, no subprocess, no audio device and no
dependence on a real duration. The clock module only re-exports a monotonic clock
and exposes one read-only resolution helper, so the tests check three things:

1. the platform default is the high-resolution clock the defect report requires
   (``time.perf_counter`` on Windows) and not the 15.625 ms ``GetTickCount64``
   ``time.monotonic``;
2. the resolution helper really reports the documented resolutions, including the
   measured Windows fact ``monotonic == 0.015625 s`` (15.625 ms) against
   ``perf_counter == 1e-07 s``;
3. all five construction points default to that same clock object while an
   explicitly injected clock still wins.
"""
from __future__ import annotations

import inspect
import sys
import time
import unittest

from app.tts.clock import clock_resolution_seconds, monotonic_seconds
from app.tts.pcm_playback import PCMPlaybackWorker
from app.tts.playback_worker import PlaybackState, StopResult
from app.tts.providers import PCMChunk, VoiceStudioSentenceRequest
from app.tts.streaming_session import StreamingVoiceSession, build_streaming_session
from app.tts.transport import VoiceStudioTransportRouter
from app.tts.waveout_pcm import WaveOutPCMStreamBackend

SAMPLE_RATE = 24_000
# The platform facts the defect report is built on, asserted only on Windows.
WINDOWS_MONOTONIC_RESOLUTION_S = 0.015625
WINDOWS_PERF_COUNTER_RESOLUTION_S = 1e-07

# Every construction point that must default to the module clock. The names are
# reported verbatim so a failure says which surface regressed.
CONSTRUCTION_POINTS: tuple[tuple[str, object], ...] = (
    ("StreamingVoiceSession.__init__", StreamingVoiceSession.__init__),
    ("build_streaming_session", build_streaming_session),
    ("PCMPlaybackWorker.__init__", PCMPlaybackWorker.__init__),
    ("VoiceStudioTransportRouter.__init__", VoiceStudioTransportRouter.__init__),
    ("WaveOutPCMStreamBackend.__init__", WaveOutPCMStreamBackend.__init__),
)


class _NullBackend:
    """Minimal in-memory backend: no device, no I/O, immediately drained."""

    backend_name = "null-test-backend"
    max_buffer_bytes = 9_600

    def __init__(self) -> None:
        self.submitted: list[bytes] = []
        self.open_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        self.open_calls += 1

    def writable(self) -> bool:
        return True

    def submit(self, pcm: bytes) -> None:
        self.submitted.append(bytes(pcm))

    def poll_drained(self) -> bool:
        return True

    def stop(self, timeout: float) -> StopResult:
        del timeout
        return StopResult(True, "backend_buffer_returned", "null_test_reset")

    def close(self) -> None:
        self.close_calls += 1


class _NullProvider:
    """Transport provider that is never asked to stream in these tests."""

    def stream_sentence(self, request, *, cancelled):  # pragma: no cover - unused
        raise AssertionError("no sentence may be streamed by the clock tests")
        yield request  # pragma: no cover - keeps this a generator

    def close(self) -> None:
        pass


class _FixedClock:
    """Callable clock returning one fixed value; proves injection precedence."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.value


def _request() -> VoiceStudioSentenceRequest:
    return VoiceStudioSentenceRequest(
        reply_id="stream-" + "0" * 16,
        segment_id="segment-1",
        provider_request_id="clock-test-request-1",
        provider_generation=1,
        text="一句。",
        profile_id="profile-1",
        language="zh",
        speed=1.0,
    )


class DefaultClockTests(unittest.TestCase):
    def test_default_clock_follows_the_platform(self) -> None:
        if sys.platform == "win32":
            self.assertIs(monotonic_seconds, time.perf_counter)
            self.assertIsNot(monotonic_seconds, time.monotonic)
        else:
            self.assertIs(monotonic_seconds, time.monotonic)

    def test_default_clock_is_monotonic_and_increasing(self) -> None:
        first = monotonic_seconds()
        second = monotonic_seconds()
        self.assertIsInstance(first, float)
        self.assertGreaterEqual(second, first)

    def test_resolution_helper_returns_a_positive_resolution(self) -> None:
        resolution = clock_resolution_seconds()
        self.assertIsInstance(resolution, float)
        self.assertGreater(resolution, 0.0)

    def test_resolution_helper_accepts_an_explicit_clock(self) -> None:
        self.assertEqual(
            clock_resolution_seconds(time.perf_counter),
            float(time.get_clock_info("perf_counter").resolution),
        )
        self.assertEqual(
            clock_resolution_seconds(time.monotonic),
            float(time.get_clock_info("monotonic").resolution),
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows clock facts only")
    def test_windows_resolutions_match_the_documented_defect(self) -> None:
        # ``time.monotonic`` is GetTickCount64: 15.625 ms, i.e. it cannot resolve
        # the worker spans at all; the shipped default must not be that clock.
        self.assertEqual(
            clock_resolution_seconds(time.monotonic), WINDOWS_MONOTONIC_RESOLUTION_S
        )
        self.assertEqual(
            clock_resolution_seconds(time.perf_counter), WINDOWS_PERF_COUNTER_RESOLUTION_S
        )
        self.assertEqual(clock_resolution_seconds(), WINDOWS_PERF_COUNTER_RESOLUTION_S)
        self.assertGreater(
            clock_resolution_seconds(time.monotonic), clock_resolution_seconds()
        )

    def test_resolution_helper_rejects_bad_arguments(self) -> None:
        with self.assertRaises(TypeError):
            clock_resolution_seconds("not-a-clock")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            clock_resolution_seconds(lambda: 0.0)

    def test_five_construction_points_default_to_the_module_clock(self) -> None:
        for name, target in CONSTRUCTION_POINTS:
            with self.subTest(construction_point=name):
                parameter = inspect.signature(target).parameters["clock"]
                self.assertIs(
                    parameter.default,
                    monotonic_seconds,
                    f"{name} 的 clock 默认值必须与 app.tts.clock.monotonic_seconds 是同一对象",
                )

    def test_explicitly_injected_clock_still_wins(self) -> None:
        injected = _FixedClock(7.0)
        router = VoiceStudioTransportRouter(
            mode="ws_pcm",
            ws_provider=_NullProvider(),
            http_provider=_NullProvider(),
            clock=injected,
        )
        self.addCleanup(router.close)
        self.assertIs(router._clock, injected)  # noqa: SLF001 - precedence is the point

        backend = _NullBackend()
        worker = PCMPlaybackWorker(
            lambda: backend, start_buffer_ms=1, stop_timeout=0.5, clock=injected
        )
        self.addCleanup(worker.close)
        request = _request()
        ticket = worker.begin_sentence(request)
        self.assertTrue(
            worker.push_chunk(
                PCMChunk(
                    reply_id=request.reply_id,
                    segment_id=request.segment_id,
                    provider_request_id=request.provider_request_id,
                    provider_generation=request.provider_generation,
                    sequence=1,
                    sample_rate=SAMPLE_RATE,
                    channels=1,
                    sample_width=2,
                    data=b"\x01\x00" * (SAMPLE_RATE * 20 // 1000),
                    is_final=True,
                )
            )
        )
        self.assertIs(worker.wait(ticket, 5.0), PlaybackState.COMPLETED)
        # Only the injected clock can produce this value: both worker observation
        # surfaces must read it instead of the module default.
        self.assertEqual(worker.device_submit_at(request), 7.0)
        self.assertEqual(worker.device_gate_open_at(request), 7.0)
        self.assertGreater(injected.reads, 0)


if __name__ == "__main__":
    unittest.main()
