from __future__ import annotations

import ctypes
import sys
import threading
import time
import unittest
from threading import Event

from app.tts.pcm_playback import (
    PCMBufferBusy,
    PCMPlaybackBackpressure,
    PCMPlaybackError,
    PCMPlaybackWorker,
)
from app.tts.playback_worker import PlaybackState, StopResult
from app.tts.providers import PCMChunk, VoiceStudioSentenceRequest

SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 48 bytes per millisecond of PCM16 mono


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


class FakePCMBackend:
    """Offline PCM stream backend; never opens a device."""

    backend_name = "fake-pcm"

    def __init__(self, *, max_buffer_bytes: int = 9600, slots: int = 3, auto_return: bool = True,
                 stop_result: StopResult | None = None) -> None:
        self.max_buffer_bytes = max_buffer_bytes
        self.slots = slots
        self.auto_return = auto_return
        self.stop_result = stop_result or StopResult(True, "backend_buffer_returned", "fake_reset")
        self.submitted: list[bytes] = []
        self.open_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self._inflight = 0
        self._drained = True
        self._lock = threading.Lock()

    def open(self) -> None:
        self.open_calls += 1

    def writable(self) -> bool:
        with self._lock:
            return self._inflight < self.slots

    def submit(self, pcm: bytes) -> None:
        with self._lock:
            if self._inflight >= self.slots:
                raise PCMBufferBusy("no_free_pcm_buffer")
            if len(pcm) > self.max_buffer_bytes:
                raise PCMPlaybackError("pcm_exceeds_device_buffer")
            self.submitted.append(bytes(pcm))
            self._inflight += 1
            self._drained = False
        if self.auto_return:
            self.release_one()

    def release_one(self) -> None:
        with self._lock:
            if self._inflight:
                self._inflight -= 1
            self._drained = self._inflight == 0

    def poll_drained(self) -> bool:
        with self._lock:
            return self._drained

    def stop(self, timeout: float) -> StopResult:
        del timeout
        self.stop_calls += 1
        if not self.stop_result.acknowledged:
            return self.stop_result
        with self._lock:
            self._inflight = 0
            self._drained = True
        return self.stop_result

    def close(self) -> None:
        self.close_calls += 1


class PCMPlaybackWorkerTests(unittest.TestCase):
    def make_request(self, **overrides: object) -> VoiceStudioSentenceRequest:
        values: dict[str, object] = {
            "reply_id": "reply-1",
            "segment_id": "segment-1",
            "provider_request_id": "request-1",
            "provider_generation": 1,
            "text": "一句话。",
            "profile_id": "profile-1",
            "language": "zh",
            "speed": 1.0,
        }
        values.update(overrides)
        return VoiceStudioSentenceRequest(**values)  # type: ignore[arg-type]

    def make_chunk(self, request: VoiceStudioSentenceRequest, sequence: int, milliseconds: int,
                   *, final: bool = False, **overrides: object) -> PCMChunk:
        values: dict[str, object] = {
            "reply_id": request.reply_id,
            "segment_id": request.segment_id,
            "provider_request_id": request.provider_request_id,
            "provider_generation": request.provider_generation,
            "sequence": sequence,
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "sample_width": 2,
            "data": pcm_bytes(milliseconds),
            "is_final": final,
        }
        values.update(overrides)
        return PCMChunk(**values)  # type: ignore[arg-type]

    def make_worker(self, backend: FakePCMBackend | None = None, **overrides: object):
        backend = backend or FakePCMBackend()
        worker = PCMPlaybackWorker(lambda: backend, **overrides)  # type: ignore[arg-type]
        self.addCleanup(worker.close)
        return worker, backend

    def stream(self, worker: PCMPlaybackWorker, request: VoiceStudioSentenceRequest,
               chunks: list[PCMChunk]) -> PlaybackState:
        ticket = worker.begin_sentence(request)
        for chunk in chunks:
            self.assertTrue(worker.push_chunk(chunk))
        return worker.wait(ticket, 2.0)

    def assert_error(self, code: str, callable_: object) -> None:
        with self.assertRaises((PCMPlaybackError, PCMPlaybackBackpressure)) as context:
            callable_()  # type: ignore[operator]
        self.assertEqual(str(context.exception), code)

    # ---------------------------------------------------------------- start gate

    def test_start_gate_holds_audio_until_the_configured_buffer(self) -> None:
        worker, backend = self.make_worker(start_buffer_ms=160)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 100))
        time.sleep(0.05)
        self.assertEqual(backend.submitted, [])
        self.assertEqual(worker.pending_bytes, 100 * BYTES_PER_MS)
        worker.push_chunk(self.make_chunk(request, 2, 100))
        deadline = time.time() + 2
        while not backend.submitted and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(backend.submitted)
        self.assertEqual(len(backend.submitted[0]), 200 * BYTES_PER_MS)
        worker.stop(ticket)

    def test_final_chunk_flushes_below_the_start_gate(self) -> None:
        worker, backend = self.make_worker(start_buffer_ms=160)
        request = self.make_request()
        chunks = [self.make_chunk(request, 1, 40, final=True)]
        self.assertEqual(self.stream(worker, request, chunks), PlaybackState.COMPLETED)
        self.assertEqual(b"".join(backend.submitted), pcm_bytes(40))

    def test_chunks_keep_their_original_order(self) -> None:
        worker, backend = self.make_worker(start_buffer_ms=1)
        request = self.make_request()
        chunks = [
            self.make_chunk(request, 1, 20),
            self.make_chunk(request, 2, 30),
            self.make_chunk(request, 3, 40, final=True),
        ]
        self.assertEqual(self.stream(worker, request, chunks), PlaybackState.COMPLETED)
        self.assertEqual(b"".join(backend.submitted), pcm_bytes(20) + pcm_bytes(30) + pcm_bytes(40))

    def test_natural_completion_requires_a_final_chunk(self) -> None:
        worker, backend = self.make_worker(start_buffer_ms=1)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 60))
        time.sleep(0.1)
        self.assertFalse(ticket.terminal)
        self.assertTrue(backend.submitted)
        worker.stop(ticket)

    # ------------------------------------------------------- identity and order

    def test_duplicate_and_skipped_sequences_are_rejected(self) -> None:
        worker, _ = self.make_worker()
        request = self.make_request()
        worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 20))
        self.assert_error("sequence_invalid", lambda: worker.push_chunk(self.make_chunk(request, 1, 20)))
        self.assert_error("sequence_invalid", lambda: worker.push_chunk(self.make_chunk(request, 3, 20)))

    def test_foreign_identity_is_rejected(self) -> None:
        worker, _ = self.make_worker()
        request = self.make_request()
        worker.begin_sentence(request)
        foreign = self.make_chunk(request, 1, 20, provider_generation=2)
        self.assert_error("identity_mismatch", lambda: worker.push_chunk(foreign))

    def test_second_sentence_is_rejected_while_one_is_active(self) -> None:
        worker, _ = self.make_worker(start_buffer_ms=1)
        request = self.make_request()
        worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 20))
        self.assert_error("sentence_already_active", lambda: worker.begin_sentence(self.make_request(segment_id="segment-2")))
        worker.close()

    def test_late_chunks_after_terminal_are_dropped_not_played(self) -> None:
        worker, backend = self.make_worker(start_buffer_ms=1)
        request = self.make_request()
        chunks = [self.make_chunk(request, 1, 40, final=True)]
        self.assertEqual(self.stream(worker, request, chunks), PlaybackState.COMPLETED)
        submitted_before = list(backend.submitted)
        self.assertFalse(worker.push_chunk(self.make_chunk(request, 2, 40)))
        self.assertEqual(backend.submitted, submitted_before)

    # ----------------------------------------------------------------- limits

    def test_sentence_memory_limit_is_enforced(self) -> None:
        worker, _ = self.make_worker(start_buffer_ms=100_000, max_sentence_bytes=4800)
        request = self.make_request()
        worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 50))
        self.assert_error("sentence_memory_limit", lambda: worker.push_chunk(self.make_chunk(request, 2, 60)))
        worker.close()

    def test_queued_audio_limit_applies_backpressure(self) -> None:
        worker, _ = self.make_worker(start_buffer_ms=100_000, max_queued_audio_ms=200)
        request = self.make_request()
        worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 150))
        self.assertEqual(worker.queued_audio_ms, 150)
        self.assert_error("queued_audio_limit", lambda: worker.push_chunk(self.make_chunk(request, 2, 100)))
        worker.close()

    # -------------------------------------------------------------------- stop

    def test_stop_before_any_submission_is_acknowledged_without_a_device_call(self) -> None:
        worker, backend = self.make_worker(start_buffer_ms=160)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 40))
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_ACKED)
        self.assertEqual(backend.stop_calls, 0)
        self.assertEqual(ticket.events[-1].confirmation, "not_submitted")

    def test_stop_requires_backend_buffer_return(self) -> None:
        backend = FakePCMBackend(auto_return=False)
        worker, _ = self.make_worker(backend, start_buffer_ms=1, stop_timeout=0.5)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 60))
        deadline = time.time() + 2
        while not backend.submitted and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_ACKED)
        self.assertEqual(backend.stop_calls, 1)
        self.assertTrue(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "backend_buffer_returned")

    def test_stop_timeout_is_terminal_and_disables_voice(self) -> None:
        backend = FakePCMBackend(auto_return=False, stop_result=StopResult(False, "timeout", "fake_timeout"))
        worker, _ = self.make_worker(backend, start_buffer_ms=1, stop_timeout=0.05)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 60))
        deadline = time.time() + 2
        while not backend.submitted and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_TIMEOUT)
        self.assertTrue(worker.disabled)
        self.assertEqual(ticket.events[-1].confirmation, "timeout")
        self.assert_error("voice_disabled", lambda: worker.begin_sentence(self.make_request(segment_id="segment-2")))

    def test_unknown_stop_is_terminal_and_disables_voice(self) -> None:
        backend = FakePCMBackend(auto_return=False, stop_result=StopResult(False, "unknown", "fake_unknown"))
        worker, _ = self.make_worker(backend, start_buffer_ms=1, stop_timeout=0.2)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 60))
        deadline = time.time() + 2
        while not backend.submitted and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.stop(ticket), PlaybackState.UNKNOWN)
        self.assertTrue(worker.disabled)

    def test_backend_submit_failure_is_fail_closed(self) -> None:
        class BrokenBackend(FakePCMBackend):
            def submit(self, pcm: bytes) -> None:
                raise OSError("device_gone")

        backend = BrokenBackend()
        worker, _ = self.make_worker(backend, start_buffer_ms=1, stop_timeout=0.1)
        request = self.make_request()
        ticket = worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 60))
        self.assertEqual(worker.wait(ticket, 2.0), PlaybackState.FAILED)
        self.assertTrue(worker.disabled)

    # --------------------------------------------------------------- lifecycle

    def test_close_is_idempotent_and_blocks_new_sentences(self) -> None:
        worker, backend = self.make_worker()
        worker.close()
        worker.close()
        self.assertGreaterEqual(backend.close_calls, 1)
        self.assert_error("worker_closed", lambda: worker.begin_sentence(self.make_request()))

    def test_push_without_an_active_sentence_is_rejected(self) -> None:
        worker, _ = self.make_worker()
        request = self.make_request()
        self.assert_error("no_active_sentence", lambda: worker.push_chunk(self.make_chunk(request, 1, 20)))

    def test_queued_audio_reports_pending_pcm_only(self) -> None:
        worker, _ = self.make_worker(start_buffer_ms=100_000)
        request = self.make_request()
        worker.begin_sentence(request)
        worker.push_chunk(self.make_chunk(request, 1, 40))
        self.assertEqual(worker.queued_audio_ms, 40)
        self.assertEqual(worker.pending_bytes, 40 * BYTES_PER_MS)
        worker.close()


if sys.platform == "win32":
    from app.tts.waveout_pcm import PCMWaveOutDiagnostic, WaveOutPCMStreamBackend  # noqa: E402

    _WOM_DONE = 0x3BD
    _MMSYSERR_NOERROR = 0

    class FakeWinMM:
        """Deterministic stand-in for winmm.dll; performs no device I/O."""

        def __init__(self, *, open_result: int = 0, prepare_result: int = 0,
                     write_result: int = 0, reset_result: int = 0,
                     unprepare_result: int = 0, close_result: int = 0) -> None:
            self.open_result = open_result
            self.prepare_result = prepare_result
            self.write_result = write_result
            self.reset_result = reset_result
            self.unprepare_result = unprepare_result
            self.close_result = close_result
            self.opened = 0
            self.prepared: list[int] = []
            self.written: list[tuple[int, int]] = []
            self.reset_calls = 0
            self.unprepared: list[int] = []
            self.closed = 0
            self.callback = None
            self.unprepared_writes = 0
            self.argtypes: dict[str, object] = {}
            self.restype: object = None

        def _set_handle(self, handle_pointer: object, value: int) -> None:
            ctypes.cast(handle_pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value

        @staticmethod
        def _target(header: object) -> object:
            """Resolve a header passed through ``ctypes.byref`` or ``pointer``."""

            target = getattr(header, "_obj", None)
            if target is None:
                target = header.contents  # type: ignore[union-attr]
            return target

        def waveOutOpen(self, handle, device, fmt, callback, instance, flags):  # noqa: N802
            del device, fmt, instance, flags
            if self.open_result == _MMSYSERR_NOERROR:
                self._set_handle(handle, 0x1234)
            self.opened += 1
            self.callback = callback
            return self.open_result

        def waveOutPrepareHeader(self, handle, header, size):  # noqa: N802
            del handle, size
            target = self._target(header)
            # The real driver sets WHDR_PREPARED here. Mirroring it means a
            # header that loses the flag fails exactly like on a real device.
            target.dwFlags |= 0x00000002
            self.prepared.append(ctypes.addressof(target))
            return self.prepare_result

        def waveOutWrite(self, handle, header, size):  # noqa: N802
            del handle, size
            target = self._target(header)
            self.written.append((ctypes.addressof(target), target.dwBufferLength, target.dwFlags))
            if not target.dwFlags & 0x00000002:
                self.unprepared_writes += 1
                return 34  # WAVERR_UNPREPARED
            return self.write_result

        def waveOutReset(self, handle):  # noqa: N802
            del handle
            self.reset_calls += 1
            return self.reset_result

        def waveOutUnprepareHeader(self, handle, header, size):  # noqa: N802
            del handle, size
            self.unprepared.append(ctypes.addressof(self._target(header)))
            return self.unprepare_result

        def waveOutClose(self, handle):  # noqa: N802
            del handle
            self.closed += 1
            return self.close_result


if sys.platform == "win32":

    class WaveOutPCMStreamBackendTests(unittest.TestCase):
        def make_backend(self, winmm: FakeWinMM | None = None, **overrides: object):
            winmm = winmm or FakeWinMM()
            backend = WaveOutPCMStreamBackend(winmm=winmm, **overrides)  # type: ignore[arg-type]
            self.addCleanup(backend.close)
            return backend, winmm

        @staticmethod
        def address_of(backend: WaveOutPCMStreamBackend, index: int) -> int:
            return backend.diagnostics.header_addresses[index]

        def test_open_prepares_three_reusable_headers(self) -> None:
            backend, winmm = self.make_backend()
            backend.open()
            self.assertEqual(winmm.opened, 1)
            self.assertEqual(len(winmm.prepared), 3)
            self.assertEqual(backend.diagnostics.open_result, 0)
            self.assertEqual(backend.diagnostics.prepare_results, (0, 0, 0))

        def test_submission_stops_when_no_header_has_returned(self) -> None:
            backend, winmm = self.make_backend(buffer_bytes=4800)
            backend.open()
            backend.submit(pcm_bytes(100))
            backend.submit(pcm_bytes(100))
            backend.submit(pcm_bytes(100))
            self.assertFalse(backend.writable())
            with self.assertRaises(PCMBufferBusy):
                backend.submit(pcm_bytes(100))
            self.assertEqual(len(winmm.written), 3)
            self.assertFalse(backend.poll_drained())

        def test_returned_header_becomes_reusable_only_after_wom_done(self) -> None:
            backend, winmm = self.make_backend(buffer_bytes=4800)
            backend.open()
            addresses = [self.address_of(backend, index) for index in range(3)]
            for _ in range(3):
                backend.submit(pcm_bytes(50))
            self.assertFalse(backend.writable())
            backend._on_message(None, _WOM_DONE, None, addresses[0], None)
            self.assertTrue(backend.writable())
            backend.submit(pcm_bytes(50))
            self.assertEqual(winmm.written[3][0], addresses[0])
            self.assertFalse(backend.writable())

        def test_wom_done_for_an_unknown_header_is_ignored(self) -> None:
            backend, _ = self.make_backend()
            backend.open()
            before = backend.diagnostics.returned_count
            backend._on_message(None, _WOM_DONE, None, 0xDEAD, None)
            self.assertEqual(backend.diagnostics.returned_count, before)
            self.assertTrue(backend.writable())

        def test_prepared_flag_survives_resubmission(self) -> None:
            backend, winmm = self.make_backend(buffer_bytes=4800)
            backend.open()
            address = self.address_of(backend, 0)
            backend.submit(pcm_bytes(50))
            backend._on_message(None, _WOM_DONE, None, address, None)
            backend.submit(pcm_bytes(50))
            self.assertEqual(winmm.unprepared_writes, 0)
            self.assertEqual(winmm.written[0][2] & 0x00000002, 0x00000002)
            self.assertEqual(winmm.written[1][2] & 0x00000002, 0x00000002)
            self.assertEqual(winmm.written[1][2] & 0x00000001, 0)

        def test_submission_larger_than_the_device_buffer_is_rejected(self) -> None:
            backend, _ = self.make_backend(buffer_bytes=4800)
            backend.open()
            with self.assertRaises(PCMPlaybackError):
                backend.submit(pcm_bytes(200))

        def test_empty_submission_is_rejected(self) -> None:
            backend, _ = self.make_backend()
            backend.open()
            with self.assertRaises(PCMPlaybackError):
                backend.submit(b"")

        def test_operations_before_open_or_after_close_are_rejected(self) -> None:
            backend, _ = self.make_backend()
            with self.assertRaises(PCMPlaybackError):
                backend.submit(pcm_bytes(20))
            backend.open()
            backend.close()
            with self.assertRaises(PCMPlaybackError):
                backend.submit(pcm_bytes(20))

        def test_stop_resets_and_waits_for_every_submitted_buffer(self) -> None:
            backend, winmm = self.make_backend(buffer_bytes=4800)
            backend.open()
            addresses = [self.address_of(backend, index) for index in range(3)]
            backend.submit(pcm_bytes(50))
            backend.submit(pcm_bytes(50))
            releasing = threading.Thread(
                target=self._release_after,
                args=(backend, addresses, 0.05),
                daemon=True,
            )
            releasing.start()
            result = backend.stop(1.0)
            releasing.join(timeout=2)
            self.assertTrue(result.acknowledged)
            self.assertEqual(result.confirmation, "backend_buffer_returned")
            self.assertEqual(winmm.reset_calls, 1)
            self.assertTrue(backend.poll_drained())

        @staticmethod
        def _release_after(backend: WaveOutPCMStreamBackend, addresses: list[int], delay: float) -> None:
            time.sleep(delay)
            for address in addresses:
                backend._on_message(None, _WOM_DONE, None, address, None)

        def test_stop_times_out_when_buffers_never_return(self) -> None:
            backend, _ = self.make_backend(buffer_bytes=4800)
            backend.open()
            backend.submit(pcm_bytes(50))
            result = backend.stop(0.05)
            self.assertFalse(result.acknowledged)
            self.assertEqual(result.confirmation, "timeout")
            self.assertTrue(backend.uncertain)

        def test_reset_failure_is_reported_as_unknown(self) -> None:
            backend, _ = self.make_backend(FakeWinMM(reset_result=5))
            backend.open()
            backend.submit(pcm_bytes(50))
            result = backend.stop(0.2)
            self.assertFalse(result.acknowledged)
            self.assertEqual(result.confirmation, "unknown")
            self.assertTrue(backend.uncertain)

        def test_open_failure_is_reported(self) -> None:
            backend, _ = self.make_backend(FakeWinMM(open_result=4))
            with self.assertRaises(OSError):
                backend.open()
            self.assertEqual(backend.diagnostics.open_result, 4)

        def test_uncertain_backend_never_releases_buffers(self) -> None:
            backend, winmm = self.make_backend(FakeWinMM(reset_result=5))
            backend.open()
            backend.submit(pcm_bytes(50))
            backend.stop(0.05)
            backend.close()
            self.assertEqual(winmm.unprepared, [])
            self.assertEqual(winmm.closed, 0)

        def test_clean_close_unprepares_every_header_and_closes_the_device(self) -> None:
            backend, winmm = self.make_backend()
            backend.open()
            backend.close()
            self.assertEqual(len(winmm.unprepared), 3)
            self.assertEqual(winmm.closed, 1)
            self.assertTrue(backend.diagnostics.cleanup_verified)

        def test_closed_backend_rejects_further_use(self) -> None:
            backend, _ = self.make_backend()
            backend.open()
            backend.close()
            with self.assertRaises(PCMPlaybackError):
                backend.open()

        def test_diagnostics_snapshot_is_immutable(self) -> None:
            backend, _ = self.make_backend()
            backend.open()
            snapshot = backend.diagnostics
            self.assertIsInstance(snapshot, PCMWaveOutDiagnostic)
            with self.assertRaises(Exception):
                snapshot.open_result = 9  # type: ignore[misc]

        def test_worker_drives_the_window_backend_through_a_fake_device(self) -> None:
            winmm = FakeWinMM()
            backend = WaveOutPCMStreamBackend(winmm=winmm, buffer_bytes=4800)
            worker = PCMPlaybackWorker(lambda: backend, start_buffer_ms=1, stop_timeout=0.5)
            self.addCleanup(worker.close)
            request = VoiceStudioSentenceRequest(
                reply_id="reply-1", segment_id="segment-1", provider_request_id="request-1",
                provider_generation=1, text="一句话。", profile_id="profile-1",
                language="zh", speed=1.0,
            )
            ticket = worker.begin_sentence(request)
            chunk = PCMChunk(
                reply_id="reply-1", segment_id="segment-1", provider_request_id="request-1",
                provider_generation=1, sequence=1, sample_rate=SAMPLE_RATE, channels=1,
                sample_width=2, data=pcm_bytes(100), is_final=True,
            )
            worker.push_chunk(chunk)
            deadline = time.time() + 2
            while not winmm.written and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(winmm.written)
            for address, _length, _flags in list(winmm.written):
                backend._on_message(None, _WOM_DONE, None, address, None)
            self.assertEqual(worker.wait(ticket, 2.0), PlaybackState.COMPLETED)
            worker.close()
            self.assertEqual(len(winmm.unprepared), 3)
            self.assertEqual(winmm.closed, 1)


if __name__ == "__main__":
    unittest.main()
