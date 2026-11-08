"""Offline failure matrix for the opt-in VoiceStudio byte/device seam.

All provider and device implementations below are stubs. These tests verify the
current seam contract only; they cannot certify a real Windows hardware stop
ACK, user hearing, or default Agent-to-speaker wiring.
"""
from __future__ import annotations

import getpass
import io
import threading
import unittest
import urllib.error
import wave
from unittest.mock import patch

from app.tts.private_selection import CONFIRMED_PROFILE, VoiceSelection
from app.tts.playback_worker import PlaybackState, PlaybackWorker, StopResult
from app.tts.real_playback import RealVoicePreview, _generate


TEXT = "第一句。第二句。第三句。"


def wav_bytes(*, rate: int = 24000, frames: int = 2400, channels: int = 1,
              sample: bytes = b"\x10\x04") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(sample * (frames * channels))
    return output.getvalue()


class Response:
    def __init__(self, data: bytes, *, status: int = 200,
                 content_type: str = "audio/wav"):
        self.data = data
        self.status = status
        self.content_type = content_type
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    @property
    def headers(self):
        return self

    def get_content_type(self):
        return self.content_type

    def read(self, limit: int) -> bytes:
        self.read_sizes.append(limit)
        return self.data[:limit]


class FakeStopBackend:
    """Offline backend: protocol facts only, never real-device evidence."""

    backend_name = "offline-fake-backend"

    def __init__(self, stop_result: StopResult):
        self.stop_result = stop_result
        self.started: list[bytes] = []
        self.stop_calls = 0

    def start(self, audio: bytes) -> None:
        self.started.append(audio)

    def poll_complete(self) -> bool:
        return False

    def stop(self, timeout: float) -> StopResult:
        del timeout
        self.stop_calls += 1
        return self.stop_result

    def close(self) -> None:
        return None


class RealFailureMatrixTests(unittest.TestCase):
    def setUp(self):
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
        self.synthesis: list[str] = []
        self.submissions: list[bytes] = []
        self.verifications: list[VoiceSelection] = []

    def verify(self, selection: VoiceSelection) -> None:
        self.verifications.append(selection)

    def synth(self, sentence: str, selection: VoiceSelection) -> bytes:
        self.synthesis.append(sentence)
        self.assertEqual(selection, self.selection)
        return wav_bytes()

    def play(self, data: bytes) -> None:
        self.submissions.append(data)

    def preview(self, **overrides) -> RealVoicePreview:
        return RealVoicePreview(
            self.selection,
            synthesizer=overrides.get("synthesizer", self.synth),
            player=overrides.get("player", self.play),
            worker=overrides.get("worker"),
            verifier=overrides.get("verifier", self.verify),
        )

    def assert_fallback(self, result, *, code: str, received: int = 0,
                        completed: int = 0, text: str = TEXT) -> None:
        self.assertIsInstance(result.text, str)
        self.assertEqual(result.text, text)
        self.assertIsInstance(result.received_audio, int)
        self.assertGreaterEqual(result.received_audio, 0)
        self.assertIsInstance(result.playback_calls_returned, int)
        self.assertGreaterEqual(result.playback_calls_returned, 0)
        self.assertLessEqual(result.playback_calls_returned, result.received_audio)
        self.assertTrue(result.text_fallback)
        self.assertIsInstance(result.error_code, str)
        self.assertEqual(result.error_code, code)
        self.assertEqual(
            (result.received_audio, result.playback_calls_returned),
            (received, completed),
        )
        self.assertIsNone(result.user_heard)
        self.assertTrue(
            result.playback_state is None or isinstance(result.playback_state, PlaybackState)
        )
        self.assertIn(result.stop_status, (None, "stopped_acknowledged", "stop_unconfirmed"))
        self.assertIsInstance(result.stopped_acknowledged, bool)
        self.assertIsInstance(result.stop_unconfirmed, bool)
        self.assertIsInstance(result.voice_disabled, bool)
        if result.stop_status is None:
            self.assertIsNone(result.stop_confirmation)
            self.assertFalse(result.stopped_acknowledged)
            self.assertFalse(result.stop_unconfirmed)
        else:
            self.assertNotEqual(result.stopped_acknowledged, result.stop_unconfirmed)
            if result.stop_status == "stopped_acknowledged":
                self.assertIsInstance(result.stop_confirmation, str)
                self.assertTrue(result.stopped_acknowledged)
                self.assertFalse(result.stop_unconfirmed)
            else:
                # Legacy player failures have no backend StopResult; worker
                # timeout/unknown cases must carry an explicit confirmation.
                if result.error_code == "stop_unconfirmed":
                    self.assertIsInstance(result.stop_confirmation, str)
                else:
                    self.assertIsNone(result.stop_confirmation)
                self.assertFalse(result.stopped_acknowledged)
                self.assertTrue(result.stop_unconfirmed)

    def assert_audio_accounting(self, result, *, submitted: int,
                                returned: int | None = None) -> None:
        """Keep provider receipts distinct from device submissions and returns."""
        returned = result.playback_calls_returned if returned is None else returned
        self.assertEqual(result.playback_calls_returned, returned)
        self.assertGreaterEqual(submitted, 0)
        self.assertGreaterEqual(returned, 0)
        self.assertLessEqual(returned, submitted)
        self.assertLessEqual(submitted, result.received_audio)

    def assert_no_stop_fact(self, result) -> None:
        """Provider/pre-submission failures must not manufacture stop facts."""
        self.assertFalse(result.stopped_acknowledged)
        self.assertFalse(result.stop_unconfirmed)
        self.assertIsNone(result.stop_status)
        self.assertIsNone(result.stop_confirmation)

    def test_provider_timeout_http_error_and_bad_headers_fail_before_submission(self):
        failures = (
            ("timeout", TimeoutError("private timeout"),
             "provider_or_device_unavailable"),
            ("url_error_timeout", urllib.error.URLError(TimeoutError("private timeout")),
             "provider_or_device_unavailable"),
            ("http_exception", urllib.error.HTTPError(
                "http://127.0.0.1:3900/generate", 503, "private response", None, None),
             "provider_or_device_unavailable"),
            ("http_status", Response(wav_bytes(), status=503),
             "invalid_provider_response"),
            ("content_type", Response(wav_bytes(), content_type="text/html"),
             "invalid_provider_response"),
        )
        for label, response, code in failures:
            with self.subTest(case=label):
                preview = self.preview(
                    synthesizer=lambda part, selection: _generate(part, selection),
                )
                kwargs = (
                    {"side_effect": response}
                    if isinstance(response, Exception)
                    else {"return_value": response}
                )
                with patch("app.tts.real_playback.urllib.request.urlopen", **kwargs) as network:
                    result = preview.speak(TEXT)
                self.assert_fallback(result, code=code)
                self.assert_no_stop_fact(result)
                self.assertEqual(self.synthesis, [])
                self.assertEqual(self.submissions, [])
                self.assertEqual(network.call_count, 1)
                request, = network.call_args.args
                self.assertEqual(request.full_url, "http://127.0.0.1:3900/generate")
                self.assertEqual(network.call_args.kwargs["timeout"], 18)

    def test_received_audio_is_distinct_from_submitted_and_returned_segments(self):
        result = self.preview().speak(TEXT)

        self.assertFalse(result.text_fallback)
        self.assertEqual(result.received_audio, 3)
        self.assertEqual(len(self.submissions), 3)
        self.assertEqual(result.playback_calls_returned, 3)
        self.assert_audio_accounting(result, submitted=3, returned=3)
        self.assertIsNone(result.stop_status)
        self.assertIsNone(result.stop_confirmation)
        self.assertIsNone(result.user_heard)

    def test_cancel_without_active_ticket_is_not_a_stop_ack(self):
        preview = self.preview()

        self.assertIsNone(preview.cancel())
        self.assertIsNone(preview.worker)
        text = "\u65e0\u6d3b\u52a8\u7968\u636e\u65f6\u4ecd\u53ef\u64ad\u653e\u3002"
        result = preview.speak(text)

        self.assertFalse(result.text_fallback)
        self.assertEqual(result.text, text)
        self.assertEqual(result.received_audio, 1)
        self.assertEqual(result.playback_calls_returned, 1)
        self.assert_audio_accounting(result, submitted=1, returned=1)
        self.assertIsNone(result.stop_status)
        self.assertIsNone(result.stop_confirmation)
        self.assertFalse(result.stopped_acknowledged)
        self.assertFalse(result.stop_unconfirmed)
        self.assertFalse(result.voice_disabled)
        self.assertIsNone(result.user_heard)

    def test_provider_failure_does_not_disable_instance_for_later_new_reply(self):
        attempts = 0

        def synth(sentence, selection):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("private provider unavailable")
            return self.synth(sentence, selection)

        preview = self.preview(synthesizer=synth)
        first = preview.speak("失败回复。")
        self.assert_fallback(first, code="provider_or_device_unavailable", text="失败回复。")
        second = preview.speak("恢复回复。")
        self.assertFalse(second.text_fallback)
        self.assertEqual(second.error_code, None)
        self.assertEqual(second.received_audio, 1)
        self.assertEqual(second.playback_calls_returned, 1)

    def test_oversized_provider_audio_is_bounded_and_never_submitted(self):
        response = Response(b"x" * 500_001)
        preview = self.preview(
            synthesizer=lambda part, selection: _generate(part, selection),
        )
        with patch("app.tts.real_playback.urllib.request.urlopen", return_value=response):
            result = preview.speak(TEXT)
        self.assert_fallback(result, code="audio_too_large")
        self.assert_no_stop_fact(result)
        self.assertEqual(response.read_sizes, [500_001])
        self.assertEqual(self.submissions, [])

    def test_bad_or_unsupported_audio_fails_before_any_submission(self):
        cases = (
            ("bad_riff", b"not a wave", "invalid_audio"),
            ("truncated_pcm", wav_bytes()[:-2], "invalid_audio"),
            ("wrong_rate", wav_bytes(rate=16000), "unsupported_audio_format"),
            ("stereo", wav_bytes(channels=2), "unsupported_audio_format"),
            ("too_many_frames", wav_bytes(frames=192001), "unsupported_audio_format"),
            ("silence", wav_bytes(sample=b"\x00\x00"), "invalid_audio"),
        )
        for label, payload, code in cases:
            with self.subTest(case=label):
                calls: list[str] = []

                def synth(sentence, _selection):
                    calls.append(sentence)
                    return payload

                result = self.preview(synthesizer=synth).speak(TEXT)
                self.assert_fallback(result, code=code)
                self.assertEqual(calls, ["第一句。"])
                self.assertEqual(self.submissions, [])

    def test_later_provider_error_discards_all_prepared_audio_and_preserves_text(self):
        def synth(sentence, selection):
            self.synthesis.append(sentence)
            if len(self.synthesis) == 2:
                raise TimeoutError("private data never returned to caller")
            return wav_bytes()

        result = self.preview(synthesizer=synth).speak(TEXT)
        self.assert_fallback(result, code="provider_or_device_unavailable", received=1)
        self.assertEqual(self.synthesis, ["第一句。", "第二句。"])
        self.assertEqual(self.submissions, [])

    def test_cancel_before_synthesis_has_received_audio_zero(self):
        preview = None

        def verify(_selection):
            preview.cancel()

        preview = self.preview(verifier=verify)
        result = preview.speak(TEXT)
        # Cancellation is observed before synthesizer returns any audio, so the
        # seam's received_audio counter remains zero and no device call occurs.
        self.assert_fallback(result, code="cancelled_before_submission", received=0)
        self.assert_audio_accounting(result, submitted=0, returned=0)
        self.assert_no_stop_fact(result)
        self.assertEqual(self.synthesis, [])
        self.assertEqual(self.submissions, [])

    def test_cancel_after_each_segment_is_preparation_only_until_submission_gate(self):
        preview = None

        def synth(sentence, selection):
            audio = self.synth(sentence, selection)
            if len(self.synthesis) == 3:
                preview.cancel()
            return audio

        preview = self.preview(synthesizer=synth)
        result = preview.speak(TEXT)
        self.assert_fallback(result, code="cancelled_before_submission", received=3)
        self.assertEqual(self.synthesis, ["第一句。", "第二句。", "第三句。"])
        self.assertEqual(self.submissions, [])

    def test_cancel_during_current_playback_has_no_stop_ack_and_skips_later_segments(self):
        started = threading.Event()
        release = threading.Event()
        results = []

        def blocking_play(data):
            self.play(data)
            started.set()
            if not release.wait(3):
                raise TimeoutError("fake player release timed out")

        preview = self.preview(player=blocking_play)
        worker = threading.Thread(
            target=lambda: results.append(preview.speak(TEXT)), daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(started.wait(2), "first fake device call never started")
            preview.cancel()
            # cancel() is cooperative only; it cannot claim a current-device
            # stop acknowledgement while player() is still blocked.
            self.assertTrue(worker.is_alive())
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assert_fallback(
            results[0], code="cancelled_after_current_playback",
            received=3, completed=1,
        )
        self.assertEqual(len(self.submissions), 1)
        self.assert_audio_accounting(results[0], submitted=1, returned=1)
        self.assertEqual(self.synthesis, ["第一句。", "第二句。", "第三句。"])

    def test_device_timeout_is_unknown_stop_state_disables_instance(self):
        def timeout_play(data):
            self.play(data)
            raise TimeoutError("device completion timed out")

        preview = self.preview(player=timeout_play)
        result = preview.speak(TEXT)
        self.assert_fallback(
            result, code="device_stop_unconfirmed", received=3, completed=0,
        )
        self.assertEqual(len(self.submissions), 1)
        self.assert_audio_accounting(result, submitted=1, returned=0)
        self.assert_fallback(
            preview.speak("新回复。"),
            code="preview_unavailable", text="新回复。",
        )
        self.assertEqual(len(self.submissions), 1)

    def test_device_unknown_failure_disables_instance_and_never_submits_later_segments(self):
        def unknown_play(data):
            self.play(data)
            raise RuntimeError("device state unknown")

        preview = self.preview(player=unknown_play)
        result = preview.speak(TEXT)
        self.assert_fallback(
            result, code="device_stop_unconfirmed", received=3, completed=0,
        )
        self.assertEqual(result.stop_status, "stop_unconfirmed")
        self.assertTrue(result.stop_unconfirmed)
        self.assertFalse(result.stopped_acknowledged)
        self.assertIsNone(result.stop_confirmation)
        self.assertTrue(result.voice_disabled)
        self.assertEqual(len(self.submissions), 1)
        self.assert_audio_accounting(result, submitted=1, returned=0)
        # All audio was prepared before playback, but no later segment is
        # submitted after the device state becomes uncertain.
        self.assertEqual(self.synthesis, ["第一句。", "第二句。", "第三句。"])
        self.assert_fallback(
            preview.speak("新回复。"),
            code="preview_unavailable", text="新回复。",
        )

    def test_second_device_failure_does_not_submit_third_segment(self):
        def fail_on_second(data):
            self.play(data)
            if len(self.submissions) == 2:
                raise RuntimeError("device completion unknown")

        preview = self.preview(player=fail_on_second)
        result = preview.speak(TEXT)
        self.assert_fallback(
            result, code="device_stop_unconfirmed", received=3, completed=1,
        )
        self.assertEqual(result.stop_status, "stop_unconfirmed")
        self.assertTrue(result.stop_unconfirmed)
        self.assertFalse(result.stopped_acknowledged)
        self.assertIsNone(result.stop_confirmation)
        self.assertTrue(result.voice_disabled)
        self.assertEqual(len(self.submissions), 2)
        self.assert_audio_accounting(result, submitted=2, returned=1)
        self.assertEqual(self.synthesis, ["第一句。", "第二句。", "第三句。"])
        self.assert_fallback(
            preview.speak("新回复。"),
            code="preview_unavailable", text="新回复。",
        )


    def _start_blocked_preview(self, stop_result: StopResult):
        backend = FakeStopBackend(stop_result)
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.05)
        preview = self.preview(worker=worker)
        result_box = {}
        thread = threading.Thread(
            target=lambda: result_box.setdefault("result", preview.speak(TEXT)),
            daemon=True,
        )
        thread.start()
        import time
        deadline = time.monotonic() + 1.0
        while (preview.active_ticket is None or not backend.started) and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(preview.active_ticket)
        self.assertTrue(backend.started)
        return backend, worker, preview, thread, result_box

    def test_fake_stop_ack_is_explicit_backend_scoped_and_blocks_later_segments(self):
        backend, worker, preview, thread, result_box = self._start_blocked_preview(
            StopResult(True, "backend_buffer_returned", "fake_reset"),
        )
        try:
            self.assertEqual(preview.cancel(), PlaybackState.STOP_ACKED)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            result = result_box["result"]
            self.assert_fallback(
                result, code="stopped_acknowledged", received=3, completed=0,
            )
            self.assertEqual(result.playback_state, PlaybackState.STOP_ACKED)
            self.assertEqual(result.stop_status, "stopped_acknowledged")
            self.assertTrue(result.stopped_acknowledged)
            self.assertFalse(result.stop_unconfirmed)
            self.assertEqual(result.stop_confirmation, "backend_buffer_returned")
            self.assertFalse(result.voice_disabled)
            self.assertEqual(len(backend.started), 1)
            self.assertEqual(backend.stop_calls, 1)
            self.assert_audio_accounting(result, submitted=1, returned=0)
            self.assertEqual(self.synthesis, ["\u7b2c\u4e00\u53e5\u3002", "\u7b2c\u4e8c\u53e5\u3002", "\u7b2c\u4e09\u53e5\u3002"])
            # Three segments were prepared, but only the first reached the
            # backend before cancellation; later segments were not submitted.
            self.assertEqual(len(self.submissions), 0)
        finally:
            worker.close()

    def test_fake_stop_timeout_is_not_ack_and_disables_future_voice(self):
        backend, worker, preview, thread, result_box = self._start_blocked_preview(
            StopResult(False, "timeout", "fake_timeout"),
        )
        try:
            self.assertEqual(preview.cancel(), PlaybackState.STOP_TIMEOUT)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            result = result_box["result"]
            self.assert_fallback(
                result, code="stop_unconfirmed", received=3, completed=0,
            )
            self.assertEqual(result.playback_state, PlaybackState.STOP_TIMEOUT)
            self.assertEqual(result.stop_status, "stop_unconfirmed")
            self.assertFalse(result.stopped_acknowledged)
            self.assertTrue(result.stop_unconfirmed)
            self.assertEqual(result.stop_confirmation, "timeout")
            self.assertTrue(result.voice_disabled)
            self.assertEqual(len(backend.started), 1)
            self.assert_audio_accounting(result, submitted=1, returned=0)
            self.assert_fallback(
                preview.speak("????"),
                code="preview_unavailable", text="????",
            )
        finally:
            worker.close()

    def test_fake_unknown_stop_is_not_ack_and_disables_future_voice(self):
        backend, worker, preview, thread, result_box = self._start_blocked_preview(
            StopResult(False, "unknown", "fake_unknown"),
        )
        try:
            self.assertEqual(preview.cancel(), PlaybackState.UNKNOWN)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            result = result_box["result"]
            self.assert_fallback(
                result, code="stop_unconfirmed", received=3, completed=0,
            )
            self.assertEqual(result.playback_state, PlaybackState.UNKNOWN)
            self.assertEqual(result.stop_status, "stop_unconfirmed")
            self.assertFalse(result.stopped_acknowledged)
            self.assertTrue(result.stop_unconfirmed)
            self.assertEqual(result.stop_confirmation, "unknown")
            self.assertTrue(result.voice_disabled)
            self.assertEqual(len(backend.started), 1)
            self.assert_audio_accounting(result, submitted=1, returned=0)
            self.assert_fallback(
                preview.speak("????"),
                code="preview_unavailable", text="????",
            )
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
