from __future__ import annotations

import time
import threading
import unittest
from app.tts.playback_worker import (
    CallablePlaybackBackend, PlaybackState, PlaybackWorker, StopResult,
)


class FakeBackend:
    backend_name = "fake-device"

    def __init__(self, *, complete=True, stop_result=None):
        self.complete = complete
        self.stop_result = stop_result or StopResult(True, "fake_buffer_returned", "fake_reset")
        self.started = []
        self.stopped = 0
        self.closed = False

    def start(self, audio):
        self.started.append(audio)

    def poll_complete(self):
        return self.complete

    def stop(self, timeout):
        self.stopped += 1
        return self.stop_result

    def finalize_completed(self, timeout):
        del timeout
        self.stopped += 1
        return self.stop_result

    def close(self):
        self.closed = True


class PlaybackWorkerTests(unittest.TestCase):
    def test_fifo_completion_and_auditable_events(self):
        backend = FakeBackend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertEqual(worker.wait(ticket, 1), PlaybackState.COMPLETED)
        self.assertEqual(backend.started, [b"audio"])
        self.assertEqual([event.state for event in ticket.events], [PlaybackState.PLAYING, PlaybackState.COMPLETED])

    def test_stop_ack_requires_backend_confirmation(self):
        backend = FakeBackend(complete=False)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        deadline = time.time() + 1
        while worker.active is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_ACKED)
        self.assertEqual(backend.stopped, 1)
        self.assertTrue(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "fake_buffer_returned")

    def test_stop_timeout_disables_future_voice(self):
        backend = FakeBackend(complete=False, stop_result=StopResult(False, "timeout", "fake_timeout"))
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        deadline = time.time() + 1
        while worker.active is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_TIMEOUT)
        self.assertTrue(worker.disabled)
        self.assertEqual(ticket.events[-1].confirmation, "timeout")
        with self.assertRaises(RuntimeError):
            worker.submit(b"later")

    def test_unknown_stop_is_not_misreported_as_ack(self):
        backend = FakeBackend(complete=False, stop_result=StopResult(False, "unknown", "no_independent_stop"))
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        deadline = time.time() + 1
        while worker.active is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.stop(ticket), PlaybackState.UNKNOWN)
        self.assertFalse(ticket.events[-1].stop_ack)
        self.assertTrue(worker.disabled)

    def test_natural_completion_runs_cleanup_but_remains_completed(self):
        backend = FakeBackend(complete=True)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertEqual(worker.wait(ticket, 1), PlaybackState.COMPLETED)
        self.assertEqual(backend.stopped, 1)
        self.assertEqual(ticket.events[-1].state, PlaybackState.COMPLETED)
        self.assertTrue(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "fake_buffer_returned")
        self.assertNotIn(PlaybackState.STOP_ACKED, [event.state for event in ticket.events])

    def test_natural_completion_cleanup_failure_is_fail_closed(self):
        backend = FakeBackend(
            complete=True,
            stop_result=StopResult(False, "unknown", "cleanup_not_confirmed"),
        )
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertEqual(worker.wait(ticket, 1), PlaybackState.COMPLETED)
        self.assertEqual(ticket.events[-1].state, PlaybackState.COMPLETED)
        self.assertFalse(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "unknown")

    def test_callable_completion_never_pretends_to_be_stop_ack(self):
        backend = CallablePlaybackBackend(lambda audio: None)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertEqual(worker.wait(ticket, 1), PlaybackState.COMPLETED)
        self.assertFalse(ticket.events[-1].stop_ack)

    def test_stop_requested_before_worker_activation_acknowledges_without_backend_start(self):
        class GateBackend(FakeBackend):
            def __init__(self):
                super().__init__(complete=False)
                self.gate = __import__("threading").Event()

            def start(self, audio):
                self.gate.wait(1)
                super().start(audio)

        backend = GateBackend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_ACKED)
        backend.gate.set()
        self.assertEqual(ticket.state, PlaybackState.STOP_ACKED)
        self.assertEqual(backend.started, [])
        self.assertEqual(ticket.events[-1].confirmation, "not_submitted")

    def test_two_segments_are_serialized_without_disabling_voice(self):
        backend = FakeBackend(complete=False)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        first, second = worker.submit(b"first"), worker.submit(b"second")
        deadline = time.monotonic() + 1
        while first.state is not PlaybackState.PLAYING and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(backend.started, [b"first"])
        backend.complete = True
        self.assertEqual(worker.wait(first, 1), PlaybackState.COMPLETED)
        self.assertEqual(worker.wait(second, 1), PlaybackState.COMPLETED)
        self.assertEqual(backend.started, [b"first", b"second"])
        self.assertFalse(worker.disabled)

    def test_queued_second_sentence_can_cancel_without_touching_active_device(self):
        backend = FakeBackend(complete=False)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.05)
        self.addCleanup(worker.close)
        first, second = worker.submit(b"first"), worker.submit(b"second")
        deadline = time.monotonic() + 1
        while first.state is not PlaybackState.PLAYING and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(worker.stop(second), PlaybackState.STOP_ACKED)
        self.assertEqual(second.events[-1].confirmation, "not_submitted")
        self.assertEqual(backend.started, [b"first"])
        self.assertEqual(backend.stopped, 0)
        self.assertEqual(worker.stop(first), PlaybackState.STOP_ACKED)
        self.assertEqual(backend.stopped, 1)

    def test_poll_in_progress_after_stop_timeout_still_gets_best_effort_stop(self):
        entered, release = threading.Event(), threading.Event()

        class PollBlockingBackend(FakeBackend):
            def poll_complete(self):
                entered.set()
                release.wait(2)
                return False

        backend = PollBlockingBackend(complete=False)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertTrue(entered.wait(1))
        self.assertEqual(worker.stop(ticket, timeout=0.02), PlaybackState.STOP_TIMEOUT)
        release.set()
        deadline = time.monotonic() + 1
        while backend.stopped == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(backend.stopped, 1)
        self.assertEqual(ticket.state, PlaybackState.STOP_TIMEOUT)
        self.assertNotIn(PlaybackState.STOP_ACKED, [event.state for event in ticket.events])
        self.assertFalse(worker._stop_requested)
        self.assertFalse(worker._stop_attempted)

    def test_late_start_after_stop_timeout_never_becomes_ack(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingBackend(FakeBackend):
            def start(self, audio):
                entered.set()
                release.wait(2)
                super().start(audio)

        backend = BlockingBackend(complete=False)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        self.assertTrue(entered.wait(1))
        self.assertEqual(worker.stop(ticket, timeout=0.02), PlaybackState.STOP_TIMEOUT)
        release.set()
        deadline = time.monotonic() + 1
        while not backend.stopped and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(backend.stopped, 1)
        self.assertEqual(ticket.state, PlaybackState.STOP_TIMEOUT)
        self.assertNotIn(PlaybackState.STOP_ACKED, [e.state for e in ticket.events])
        self.assertTrue(worker.disabled)

    def test_stop_ack_is_backend_scoped_not_hardware_or_audibility(self):
        backend = FakeBackend(complete=False, stop_result=StopResult(True, "backend_buffer_returned", "reset"))
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        deadline = time.time() + 1
        while worker.active is None and time.time() < deadline:
            time.sleep(0.01)
        state = worker.stop(ticket)
        self.assertEqual(state, PlaybackState.STOP_ACKED)
        self.assertEqual(ticket.events[-1].confirmation, "backend_buffer_returned")
        self.assertNotIn("hardware", ticket.events[-1].confirmation)


if __name__ == "__main__":
    unittest.main()
