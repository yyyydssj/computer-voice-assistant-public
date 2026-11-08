from __future__ import annotations

import time
import unittest

from app.tts.fault_fixtures import (
    EVIDENCE_KIND,
    delay_callback,
    drop_callback,
    reset_error,
    wrong_buffer_callback,
)
from app.tts.playback_worker import PlaybackState, PlaybackWorker


_AUDIO = b"offline-fixture-audio"


class FaultFixtureTests(unittest.TestCase):
    def _wait_until_active(self, worker: PlaybackWorker, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while worker.active is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(worker.active)

    def _assert_offline_backend(self, backend) -> None:
        self.assertEqual(backend.evidence_kind, EVIDENCE_KIND)
        self.assertFalse(backend.device_opened)
        self.assertEqual(backend.real_audio_calls, 0)
        self.assertEqual(backend.close_calls, 1)

    def test_reset_error_enters_unknown_and_disables_worker(self):
        backend = reset_error()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)

        ticket = worker.submit(_AUDIO)
        self._wait_until_active(worker)
        self.assertEqual(worker.stop(ticket), PlaybackState.UNKNOWN)

        self.assertTrue(worker.disabled)
        self.assertEqual(ticket.events[-1].state, PlaybackState.UNKNOWN)
        self.assertFalse(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "unknown")
        self.assertEqual(backend.stop_calls, 1)

    def test_drop_callback_enters_stop_timeout_and_disables_worker(self):
        backend = drop_callback()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)

        ticket = worker.submit(_AUDIO)
        self._wait_until_active(worker)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_TIMEOUT)

        self.assertTrue(worker.disabled)
        self.assertEqual(ticket.events[-1].state, PlaybackState.STOP_TIMEOUT)
        self.assertFalse(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "timeout")
        self.assertEqual(backend.stop_calls, 1)

    def test_delayed_callback_past_boundary_remains_stop_timeout(self):
        backend = delay_callback(delay_seconds=0.35)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.01)
        self.addCleanup(worker.close)

        ticket = worker.submit(_AUDIO)
        self._wait_until_active(worker)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_TIMEOUT)
        self.assertTrue(worker.disabled)
        self.assertFalse(ticket.events[-1].stop_ack)

        # Give the deliberately late callback time to return.  The worker's
        # terminal STOP_TIMEOUT must not be upgraded by a late positive result.
        time.sleep(0.12)
        self.assertEqual(ticket.state, PlaybackState.STOP_TIMEOUT)
        self.assertTrue(worker.disabled)

    def test_stop_failure_rejects_pending_tickets_without_starting_them(self):
        backend = drop_callback()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)

        active = worker.submit(b"active")
        self._wait_until_active(worker)
        pending = worker.submit(b"pending")

        self.assertEqual(worker.stop(active), PlaybackState.STOP_TIMEOUT)
        self.assertTrue(worker.disabled)
        self.assertEqual(pending.state, PlaybackState.FAILED)
        self.assertEqual(pending.events[-1].reason, "voice_disabled_after_stop_timeout")
        self.assertEqual(backend.started, [b"active"])

        with self.assertRaises(RuntimeError):
            worker.submit(b"after-disable")

    def test_wrong_buffer_callback_is_unknown_and_never_acknowledged(self):
        backend = wrong_buffer_callback()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)

        ticket = worker.submit(_AUDIO)
        self._wait_until_active(worker)
        self.assertEqual(worker.stop(ticket), PlaybackState.UNKNOWN)

        self.assertTrue(worker.disabled)
        self.assertFalse(ticket.events[-1].stop_ack)
        self.assertEqual(ticket.events[-1].confirmation, "unknown")
        self.assertEqual(backend.callback_buffer_id, "wrong-buffer")
        self.assertNotEqual(backend.callback_buffer_id, backend.submitted_buffer_id)

    def test_all_fixtures_are_offline_and_expose_controlled_evidence(self):
        for factory in (reset_error, drop_callback, wrong_buffer_callback):
            backend = factory()
            self.assertEqual(backend.evidence_kind, "controlled_fault_fixture")
            self.assertFalse(backend.device_opened)
            self.assertEqual(backend.real_audio_calls, 0)
            backend.close()
            self.assertEqual(backend.close_calls, 1)

        delayed = delay_callback(delay_seconds=0.0)
        self.assertEqual(delayed.evidence_kind, "controlled_fault_fixture")
        self.assertFalse(delayed.device_opened)
        self.assertEqual(delayed.real_audio_calls, 0)
        delayed.close()
        self.assertEqual(delayed.close_calls, 1)

    def test_fixture_backend_matches_playback_backend_lifecycle(self):
        backend = drop_callback()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.02)
        self.addCleanup(worker.close)
        ticket = worker.submit(_AUDIO)
        self._wait_until_active(worker)
        worker.stop(ticket)
        worker.close()
        self._assert_offline_backend(backend)


if __name__ == "__main__":
    unittest.main()
