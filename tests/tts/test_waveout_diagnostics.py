from __future__ import annotations

import ctypes
import io
import wave
import unittest
from unittest.mock import patch

from app.tts import playback_worker as module
from app.tts.playback_worker import (
    StopResult, WaveOutDiagnostic, WaveOutFaultMode, WaveOutPlaybackBackend,
)


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 240)
    return output.getvalue()


class FakeWinmmFunction:
    def __init__(self, result: int = 0, callback=None) -> None:
        self.result = result
        self.callback = callback
        self.calls = 0
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls += 1
        if self.callback is not None:
            self.callback(*args)
        return self.result


class FakeWinmm:
    def __init__(self, *, reset_result: int = 0) -> None:
        self.waveOutOpen = FakeWinmmFunction(callback=self._write_open_handle)
        self.waveOutPrepareHeader = FakeWinmmFunction()
        self.waveOutWrite = FakeWinmmFunction()
        self.waveOutReset = FakeWinmmFunction(reset_result)
        self.waveOutUnprepareHeader = FakeWinmmFunction()
        self.waveOutClose = FakeWinmmFunction()

    @staticmethod
    def _write_open_handle(handle_pointer, *_args) -> None:
        ctypes.cast(handle_pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1


class WaveOutDiagnosticTests(unittest.TestCase):
    def make_backend(self, *, reset_result: int = 0) -> tuple[WaveOutPlaybackBackend, FakeWinmm]:
        winmm = FakeWinmm(reset_result=reset_result)
        with patch.object(ctypes, "WinDLL", return_value=winmm):
            backend = WaveOutPlaybackBackend()
        return backend, winmm

    def test_diagnostics_capture_lifecycle_without_changing_stop_contract(self) -> None:
        backend, winmm = self.make_backend()

        backend.start(wav_bytes())
        diagnostic_after_start = backend.diagnostics
        self.assertIsInstance(diagnostic_after_start, WaveOutDiagnostic)
        self.assertEqual(diagnostic_after_start.open_result, 0)
        self.assertEqual(diagnostic_after_start.prepare_result, 0)
        self.assertEqual(diagnostic_after_start.write_result, 0)
        self.assertIsNotNone(diagnostic_after_start.opened_at)
        self.assertIsNotNone(diagnostic_after_start.prepared_at)
        self.assertIsNotNone(diagnostic_after_start.submitted_at)
        self.assertIsNotNone(diagnostic_after_start.header_address)
        self.assertGreater(diagnostic_after_start.header_address, 0)
        self.assertFalse(diagnostic_after_start.cleanup_verified)

        backend._on_message(
            None, module._WOM_DONE, None, diagnostic_after_start.header_address, None
        )
        result = backend.stop(timeout=0.01)
        self.assertEqual(result, StopResult(True, "backend_buffer_returned", "waveOutReset"))
        backend.close()

        diagnostic = backend.diagnostics
        self.assertEqual(diagnostic.reset_result, 0)
        self.assertEqual(diagnostic.unprepare_result, 0)
        self.assertEqual(diagnostic.close_result, 0)
        self.assertIsNotNone(diagnostic.reset_at)
        self.assertIsNotNone(diagnostic.wom_done_at)
        self.assertIsNotNone(diagnostic.unprepared_at)
        self.assertIsNotNone(diagnostic.closed_at)
        self.assertTrue(diagnostic.cleanup_verified)
        self.assertIs(backend.diagnostic, diagnostic)
        self.assertEqual(winmm.waveOutOpen.calls, 1)
        self.assertEqual(winmm.waveOutPrepareHeader.calls, 1)
        self.assertEqual(winmm.waveOutWrite.calls, 1)
        self.assertEqual(winmm.waveOutReset.calls, 1)
        self.assertEqual(winmm.waveOutUnprepareHeader.calls, 1)
        self.assertEqual(winmm.waveOutClose.calls, 1)

        with self.assertRaises(AttributeError):
            diagnostic.close_result = 99  # type: ignore[misc]

    def test_wrong_header_callback_does_not_acknowledge_or_mark_done(self) -> None:
        backend, _ = self.make_backend()
        backend.start(wav_bytes())
        header_address = backend.diagnostics.header_address
        self.assertIsNotNone(header_address)

        backend._on_message(None, module._WOM_DONE, None, header_address + 1, None)

        self.assertFalse(backend.poll_complete())
        self.assertIsNone(backend.diagnostics.wom_done_at)
        result = backend.stop(timeout=0)
        self.assertEqual(result.confirmation, "timeout")
        self.assertEqual(backend.diagnostics.reset_result, 0)
        self.assertFalse(backend.diagnostics.cleanup_verified)

    def test_reset_error_is_diagnostic_and_remains_unknown(self) -> None:
        backend, _ = self.make_backend(reset_result=7)
        backend.start(wav_bytes())

        result = backend.stop(timeout=0.01)

        self.assertEqual(result, StopResult(False, "unknown", "waveOutReset:7"))
        self.assertEqual(backend.diagnostics.reset_result, 7)
        self.assertIsNotNone(backend.diagnostics.reset_at)
        self.assertFalse(backend.diagnostics.cleanup_verified)


    def test_real_backend_fault_shim_unknown_calls_real_reset_and_fails_closed(self) -> None:
        winmm = FakeWinmm()
        with patch.object(ctypes, "WinDLL", return_value=winmm):
            backend = WaveOutPlaybackBackend(fault_mode=WaveOutFaultMode.UNKNOWN_AFTER_REAL_RESET)
        backend.start(wav_bytes())

        result = backend.stop(timeout=0.01)

        self.assertEqual(result, StopResult(False, "unknown", "fault_shim_after_real_waveOutReset"))
        self.assertEqual(winmm.waveOutReset.calls, 1)
        self.assertEqual(backend.diagnostics.reset_result, 0)
        self.assertFalse(backend.diagnostics.cleanup_verified)
        backend.close()
        self.assertEqual(winmm.waveOutClose.calls, 0)

    def test_real_backend_fault_shim_timeout_calls_real_reset_and_fails_closed(self) -> None:
        winmm = FakeWinmm()
        with patch.object(ctypes, "WinDLL", return_value=winmm):
            backend = WaveOutPlaybackBackend(fault_mode="timeout_after_real_reset")
        backend.start(wav_bytes())

        result = backend.stop(timeout=0.01)

        self.assertEqual(result, StopResult(False, "timeout", "fault_shim_after_real_waveOutReset"))
        self.assertEqual(winmm.waveOutReset.calls, 1)
        self.assertFalse(backend.diagnostics.cleanup_verified)
        backend.close()
        self.assertEqual(winmm.waveOutClose.calls, 0)


if __name__ == "__main__":
    unittest.main()
