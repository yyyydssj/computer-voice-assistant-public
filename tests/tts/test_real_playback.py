"""Offline checks for private selection and real audio-byte preview seam."""
from __future__ import annotations

import getpass
import io
import json
from unittest.mock import patch
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave

from app.tts.private_selection import (
    CONFIRMED_NAME, CONFIRMED_PROFILE, DEFAULT_SPEED, SELECTION_VERSION, VoiceSelection,
    VoiceSelectionError, read_selection, selection_path, verify_provider, verify_reference,
    write_selection,
)
from app.tts.playback_worker import PlaybackState, PlaybackWorker, StopResult
from app.tts.real_playback import (
    RealVoicePreview, WaveOutPlaybackBackend, _checked_wave, _generate,
    _wave_with_leading_pause,
)


def wav_bytes(frames: int = 2400) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x10\x04" * frames)
    return buffer.getvalue()


class PrivateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())

    def test_persists_across_read_and_rejects_tampered_owner(self):
        write_selection(self.root, self.selection)
        self.assertEqual(read_selection(self.root), self.selection)
        path = selection_path(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["owner"] = "another-user"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(VoiceSelectionError):
            read_selection(self.root)
        with self.assertRaises(VoiceSelectionError):
            write_selection(self.root, self.selection)

    def test_fail_closed_on_bad_version_speed_reference_and_extra_fields(self):
        for key, value in (("version", SELECTION_VERSION + 1), ("speed", 0.4),
                           ("fade_ms", 0), ("reference_sha256", "0" * 64), ("extra", "unused")):
            with self.subTest(key=key):
                write_selection(self.root, self.selection)
                path = selection_path(self.root)
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload[key] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(VoiceSelectionError):
                    read_selection(self.root)
                path.unlink()

    def test_speed_within_measured_range_is_accepted(self):
        """语速是运行参数（0.5~2.0），不再钉死 1.0；越界仍 fail closed。"""

        for speed in (0.5, 1.0, 1.4, 2.0):
            with self.subTest(speed=speed):
                selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser(), speed=speed)
                write_selection(self.root, selection)
                self.assertEqual(read_selection(self.root), selection)
                selection_path(self.root).unlink()

    def test_speed_outside_range_is_rejected(self):
        for speed in (0.4, 2.1, 3.0):
            with self.subTest(speed=speed):
                with self.assertRaises(VoiceSelectionError):
                    VoiceSelection(CONFIRMED_PROFILE, getpass.getuser(), speed=speed).validate()

    def test_existing_selection_never_overwritten(self):
        write_selection(self.root, self.selection)
        path = selection_path(self.root)
        original = path.read_bytes()
        with self.assertRaisesRegex(VoiceSelectionError, "already exists"):
            write_selection(self.root, self.selection)
        self.assertEqual(path.read_bytes(), original)

    def test_rejects_non_loopback_provider_before_network(self):
        with patch("app.tts.private_selection.urllib.request.urlopen") as request:
            with self.assertRaises(VoiceSelectionError):
                verify_provider(self.selection, "http://public.example:3900")
            request.assert_not_called()
    def test_reference_digest_mismatch_prevents_playback(self):
        reference = self.root / "ref.wav"
        reference.write_bytes(wav_bytes())
        with self.assertRaisesRegex(VoiceSelectionError, "changed"):
            verify_reference(self.selection, reference)


class _FakeProviderReply:
    """`urlopen` 的最小替身：只提供 `status`、`read` 与上下文协议。"""

    def __init__(self, payload: bytes = b"", *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self.payload

    def __enter__(self) -> "_FakeProviderReply":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class ProviderVerificationCodeTests(unittest.TestCase):
    """真机教训（2026-10-09）：每一类失败都必须带自己的**固定短码**。

    此前六类原因共用一句 `provider profile verification failed`，于是真机上"本机服务
    没在跑"和"参考音频被换过"完全不可分——诊断时白花了一轮时间。
    """

    def setUp(self):
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())

    def _profiles(self, **overrides: object) -> bytes:
        profile: dict[str, object] = {
            "id": CONFIRMED_PROFILE,
            "name": CONFIRMED_NAME,
            "ref_audio_path": "e8572e8e.wav",
            "kind": "design",
        }
        profile.update(overrides)
        return json.dumps([profile]).encode("utf-8")

    def _code(self, replies: list[object]) -> str:
        """按调用顺序喂响应，返回抛出的固定短码；顺带钉住 `str()` 就是那个码。"""

        with patch("app.tts.private_selection.urllib.request.urlopen") as opener:
            opener.side_effect = list(replies)
            with self.assertRaises(VoiceSelectionError) as caught:
                verify_provider(self.selection)
        error = caught.exception
        self.assertIsInstance(error.code, str)
        self.assertEqual(str(error), error.code)
        return str(error.code)

    def test_unreachable_and_not_ready_are_distinguishable(self) -> None:
        self.assertEqual(self._code([OSError("connection refused")]), "provider_unreachable")
        self.assertEqual(self._code([_FakeProviderReply(status=503)]), "provider_not_ready")

    def test_oversized_and_unparsable_responses_have_their_own_codes(self) -> None:
        self.assertEqual(
            self._code([_FakeProviderReply(b"x" * 256_001)]), "provider_response_too_large"
        )
        self.assertEqual(
            self._code([_FakeProviderReply(b"not-json")]), "provider_response_invalid"
        )
        self.assertEqual(self._code([_FakeProviderReply(b"{}")]), "provider_response_invalid")

    def test_profile_mismatch_names_the_profile_not_the_transport(self) -> None:
        for overrides in (
            {"name": "别的声线"},
            {"ref_audio_path": "other.wav"},
            {"kind": "clone"},
            {"id": "deadbeef"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    self._code([_FakeProviderReply(self._profiles(**overrides))]),
                    "provider_profile_not_found",
                )

    def test_audio_endpoint_and_audio_digest_have_their_own_codes(self) -> None:
        self.assertEqual(
            self._code([
                _FakeProviderReply(self._profiles()),
                _FakeProviderReply(status=404),
            ]),
            "provider_reference_audio_unavailable",
        )
        self.assertEqual(
            self._code([
                _FakeProviderReply(self._profiles()),
                _FakeProviderReply(b"\x00" * 16),
            ]),
            "provider_reference_audio_changed",
        )

    def test_non_loopback_endpoint_never_touches_the_network(self) -> None:
        with patch("app.tts.private_selection.urllib.request.urlopen") as opener:
            with self.assertRaises(VoiceSelectionError) as caught:
                verify_provider(self.selection, "http://public.example:3900")
            opener.assert_not_called()
        self.assertEqual(caught.exception.code, "provider_endpoint_not_loopback")


class TransportTests(unittest.TestCase):
    def test_explicit_profile_and_speed_in_multipart(self):
        selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
        class Headers:
            def get_content_type(self):
                return "audio/wav"
        class Reply:
            status = 200
            headers = Headers()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, count):
                assert count == 500001
                return wav_bytes()
        def urlopen(request, timeout):
            self.assertEqual(timeout, 18)
            self.assertEqual(request.full_url, "http://127.0.0.1:3900/generate")
            body = request.data
            self.assertIn(b'name="profile_id"\r\n\r\ne8572e8e', body)
            self.assertIn(b'name="speed"\r\n\r\n1.0', body)
            self.assertIn("第一句。".encode("utf-8"), body)
            self.assertNotIn(b"[pause", body)
            return Reply()
        with patch("app.tts.real_playback.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(_generate("第一句。", selection), wav_bytes())

    def test_saved_speed_is_explicit_in_provider_request(self):
        selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
        class Headers:
            def get_content_type(self):
                return "audio/wav"
        class Reply:
            status = 200
            headers = Headers()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, count):
                return wav_bytes()
        def urlopen(request, timeout):
            self.assertIn(b'name="speed"\r\n\r\n1.0', request.data)
            return Reply()
        with patch("app.tts.real_playback.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(_generate("one.", selection), wav_bytes())
        self.assertEqual(selection.speed, DEFAULT_SPEED)

    def test_rejects_oversized_provider_audio(self):
        class Headers:
            def get_content_type(self):
                return "audio/wav"
        class Reply:
            status = 200
            headers = Headers()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, count):
                return b"a" * count
        with patch("app.tts.real_playback.urllib.request.urlopen", return_value=Reply()):
            with self.assertRaisesRegex(RuntimeError, "audio_too_large"):
                _generate("一句。", VoiceSelection(CONFIRMED_PROFILE, getpass.getuser()))

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
        self.calls = []
        self.plays = []

    def synth(self, sentence, selection):
        self.calls.append((sentence, selection.profile_id, selection.speed))
        return wav_bytes()

    def play(self, data):
        pcm, count = _checked_wave(data)
        self.plays.append((pcm, count))

    def preview(self, **kwargs):
        return RealVoicePreview(self.selection, synthesizer=kwargs.get("synthesizer", self.synth),
                                player=kwargs.get("player", self.play),
                                worker=kwargs.get("worker"),
                                verifier=kwargs.get("verifier", lambda selection: None),
                                 fade_ms=kwargs.get("fade_ms", 0))

    def test_configured_selection_applies_fade_by_default(self):
        result = RealVoicePreview(self.selection, synthesizer=self.synth,
                                  player=self.play, verifier=lambda selection: None).speak("one.")
        self.assertFalse(result.text_fallback)
        self.assertEqual(self.plays[0][0][-2:], b"\x00\x00")

    def test_fifo_pause_and_user_heard_never_inferred(self):
        result = self.preview().speak("第一句。第二句。")
        self.assertEqual([call[0] for call in self.calls], ["第一句。", "第二句。"])
        self.assertEqual([call[1:] for call in self.calls], [(CONFIRMED_PROFILE, DEFAULT_SPEED)] * 2)
        self.assertEqual([count for _, count in self.plays], [2400, 12000])
        self.assertEqual(self.plays[1][0][:19200], b"\x00" * 19200)
        self.assertEqual((result.received_audio, result.playback_calls_returned), (2, 2))
        self.assertIsNone(result.user_heard)
        self.assertFalse(result.text_fallback)

    def test_gentle_fade_is_bounded_and_preserves_default_audio(self):
        plain = wav_bytes(2400)
        self.assertEqual(_checked_wave(_wave_with_leading_pause(plain, 0))[0], _checked_wave(plain)[0])
        faded = _wave_with_leading_pause(plain, 0, fade_ms=80)
        pcm, frames = _checked_wave(faded)
        self.assertEqual(frames, 2400)
        self.assertEqual(pcm[:960], _checked_wave(plain)[0][:960])
        self.assertEqual(pcm[-2:], b"\x00\x00")
        self.assertNotEqual(pcm[-400:], _checked_wave(plain)[0][-400:])

    def test_gentle_fade_is_applied_to_each_sentence_before_existing_pause(self):
        result = self.preview(fade_ms=80).speak("one. two.")
        self.assertFalse(result.text_fallback)
        self.assertEqual([count for _, count in self.plays], [2400, 12000])
        self.assertEqual(self.plays[0][0][-2:], b"\x00\x00")
        self.assertEqual(self.plays[1][0][:19200], b"\x00" * 19200)
        self.assertEqual(self.plays[1][0][-2:], b"\x00\x00")

    def test_later_generation_failure_never_submits_previous_segment(self):
        def second_fails(sentence, selection):
            self.calls.append(sentence)
            if len(self.calls) == 2:
                raise TimeoutError("late generation failed")
            return wav_bytes()
        text = "第一句。第二句。"
        result = self.preview(synthesizer=second_fails).speak(text)
        self.assertEqual(self.calls, ["第一句。", "第二句。"])
        self.assertEqual((result.received_audio, result.playback_calls_returned), (1, 0))
        self.assertEqual(result.text, text)
        self.assertEqual(self.plays, [])
        self.assertTrue(result.text_fallback)
    def test_provider_failure_retains_complete_text_and_skips_next(self):
        def broken(sentence, selection):
            self.calls.append(sentence)
            raise TimeoutError("private provider data")
        original = "第一句。第二句。"
        result = self.preview(synthesizer=broken).speak(original)
        self.assertEqual(result.text, original)
        self.assertEqual((result.received_audio, result.playback_calls_returned), (0, 0))
        self.assertEqual(self.calls, ["第一句。"])
        self.assertEqual(result.error_code, "provider_or_device_unavailable")

    def test_verification_failure_sends_nothing(self):
        def fail(selection):
            raise VoiceSelectionError("not available")
        result = self.preview(verifier=fail).speak("一句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual((result.received_audio, self.calls, self.plays), (0, [], []))

    def test_bad_format_fails_before_device_submission(self):
        result = self.preview(synthesizer=lambda *_: b"not a wave").speak("你好。")
        self.assertTrue(result.text_fallback)
        self.assertFalse(self.plays)
        self.assertEqual(result.error_code, "invalid_audio")

    def test_device_error_disables_instance_and_never_continues_old_reply(self):
        def fail_play(data):
            self.play(data)
            raise RuntimeError("device completion uncertain")
        preview = self.preview(player=fail_play)
        result = preview.speak("第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "device_stop_unconfirmed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.plays), 1)
        later = preview.speak("新回复。")
        self.assertEqual(later.error_code, "preview_unavailable")
        self.assertEqual(len(self.calls), 2)

    def test_cancellation_before_second_segment_prevents_new_synthesis(self):
        preview = None
        def play_and_cancel(data):
            self.play(data)
            preview.cancel()
        preview = self.preview(player=play_and_cancel)
        result = preview.speak("第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "cancelled_after_current_playback")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.plays), 1)

    def test_close_is_idempotent_and_speak_is_fail_closed_after_close(self):
        calls = []
        preview = self.preview(player=lambda data: calls.append(data))
        preview.close()
        preview.close()
        result = preview.speak("????")
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "preview_unavailable")
        self.assertTrue(result.voice_disabled)
        self.assertEqual(result.received_audio, 0)
        self.assertEqual(calls, [])

    def test_close_during_synthesis_does_not_claim_audibility(self):
        class Backend:
            backend_name = "close-test-backend"

            def __init__(self):
                self.started = []
                self.stopped = 0

            def start(self, audio):
                self.started.append(audio)

            def poll_complete(self):
                return False

            def stop(self, timeout):
                self.stopped += 1
                return StopResult(True, "backend_buffer_returned", "reset")

            def close(self):
                return None

        backend = Backend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.2)
        self.addCleanup(worker.close)
        entered = threading.Event()
        preview = self.preview(worker=worker, synthesizer=lambda text, selection: wav_bytes(), verifier=lambda selection: None)

        def synth(_text, _selection):
            entered.set()
            while not preview._cancelled.is_set():
                time.sleep(0.005)
            return wav_bytes()

        preview.synthesizer = synth
        result_box = {}
        thread = threading.Thread(target=lambda: result_box.setdefault("result", preview.speak("one.")))
        thread.start()
        self.assertTrue(entered.wait(1))
        preview.close()
        self.assertTrue(preview._cancelled.is_set())
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(backend.stopped, 0)
        result = result_box["result"]
        self.assertTrue(result.text_fallback)
        self.assertIsNone(result.user_heard)
        self.assertTrue(preview.speak("????").text_fallback)

    def test_cancel_while_synthesizing_never_submits_to_device(self):
        preview = None
        def synth_then_cancel(sentence, selection):
            preview.cancel()
            return wav_bytes()
        preview = self.preview(synthesizer=synth_then_cancel)
        result = preview.speak("第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "cancelled_before_submission")
        self.assertEqual(result.received_audio, 1)
        self.assertFalse(self.plays)
    def test_rejects_markup_and_large_or_unbounded_input(self):
        for text in ("", "甲" * 181, "[pause 400ms]甲。", "一。二。三。四。", "甲" * 81):
            with self.subTest(text=text[:12]):
                result = self.preview().speak(text)
                self.assertTrue(result.text_fallback)
        self.assertEqual(self.calls, [])


class WorkerPreviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())

    def test_default_preview_builds_one_persistent_worker(self):
        with patch("app.tts.real_playback.PlaybackWorker") as worker_factory:
            preview = RealVoicePreview(self.selection, verifier=lambda selection: None)
            self.addCleanup(preview.close)
            worker_factory.assert_called_once_with(WaveOutPlaybackBackend)
            self.assertIs(preview.worker, worker_factory.return_value)

    def test_cancel_ack_is_explicit_and_blocks_later_sentence_submission(self):
        class Backend:
            backend_name = "injected-backend"

            def __init__(self):
                self.started = []
                self.stop_calls = 0

            def start(self, audio):
                self.started.append(audio)

            def poll_complete(self):
                return False

            def stop(self, timeout):
                self.stop_calls += 1
                return StopResult(True, "backend_buffer_returned", "reset")

            def close(self):
                return None

        backend = Backend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.2)
        self.addCleanup(worker.close)
        calls = []

        def synth(sentence, selection):
            calls.append(sentence)
            return wav_bytes()

        preview = RealVoicePreview(self.selection, synthesizer=synth, worker=worker,
                                   verifier=lambda selection: None)
        result_box = {}

        def run():
            result_box["result"] = preview.speak("one. two.")

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.time() + 1
        while (preview.active_ticket is None or not backend.started) and time.time() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(preview.active_ticket)
        self.assertEqual(len(backend.started), 1)

        self.assertEqual(preview.cancel(), PlaybackState.STOP_ACKED)
        thread.join(1)
        self.assertFalse(thread.is_alive())

        result = result_box["result"]
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "stopped_acknowledged")
        self.assertTrue(result.stopped_acknowledged)
        self.assertFalse(result.stop_unconfirmed)
        self.assertEqual(result.playback_calls_returned, 0)
        self.assertEqual(len(backend.started), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(backend.stop_calls, 1)


    def test_idle_cancel_without_ticket_is_a_noop_and_does_not_poison_next_speak(self):
        class Backend:
            backend_name = "idle-cancel-backend"

            def __init__(self):
                self.started = []
                self.stop_calls = 0

            def start(self, audio):
                self.started.append(audio)

            def poll_complete(self):
                return True

            def stop(self, timeout):
                self.stop_calls += 1
                return StopResult(True, "backend_buffer_returned", "reset")

            def close(self):
                return None

        backend = Backend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        preview = RealVoicePreview(self.selection, synthesizer=lambda *_: wav_bytes(),
                                   worker=worker, verifier=lambda selection: None)

        self.assertIsNone(preview.cancel())
        result = preview.speak("one.")

        self.assertFalse(result.text_fallback)
        self.assertEqual(result.playback_calls_returned, 1)
        self.assertEqual(backend.stop_calls, 0)
        self.assertEqual(len(backend.started), 1)

    def test_queued_ticket_cancel_acknowledges_without_starting_queued_audio(self):
        class Backend:
            backend_name = "queued-cancel-backend"

            def __init__(self):
                self.started = []
                self.stop_calls = 0

            def start(self, audio):
                self.started.append(audio)

            def poll_complete(self):
                return False

            def stop(self, timeout):
                self.stop_calls += 1
                return StopResult(True, "backend_buffer_returned", "reset")

            def close(self):
                return None

        backend = Backend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        self.addCleanup(worker.close)
        blocker = worker.submit(b"blocker")
        deadline = time.monotonic() + 1
        while worker.active is not blocker and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIs(worker.active, blocker)

        preview = RealVoicePreview(self.selection, synthesizer=lambda *_: wav_bytes(),
                                   worker=worker, verifier=lambda selection: None)
        result_box = {}

        def run():
            result_box["result"] = preview.speak("queued.")

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 1
        while preview.active_ticket is None and time.monotonic() < deadline:
            time.sleep(0.005)
        queued = preview.active_ticket
        self.assertIsNotNone(queued)
        self.assertIsNot(worker.active, queued)

        self.assertEqual(preview.cancel(), PlaybackState.STOP_ACKED)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        result = result_box["result"]

        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "stopped_acknowledged")
        self.assertEqual(result.playback_calls_returned, 0)
        self.assertEqual(backend.started, [b"blocker"])
        self.assertEqual(backend.stop_calls, 0)

        self.assertEqual(worker.stop(blocker), PlaybackState.STOP_ACKED)

    def test_disabled_worker_rejects_speak_before_synthesis(self):
        class Backend:
            backend_name = "disabled-worker-backend"

            def start(self, audio):
                pass

            def poll_complete(self):
                return False

            def stop(self, timeout):
                return StopResult(False, "timeout", "test_timeout")

            def close(self):
                return None

        worker = PlaybackWorker(lambda: Backend(), stop_timeout=0.02)
        self.addCleanup(worker.close)
        ticket = worker.submit(b"audio")
        deadline = time.monotonic() + 1
        while worker.active is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIs(worker.active, ticket)
        self.assertEqual(worker.stop(ticket), PlaybackState.STOP_TIMEOUT)
        self.assertTrue(worker.disabled)

        calls = []
        preview = RealVoicePreview(
            self.selection,
            synthesizer=lambda sentence, selection: calls.append(sentence) or wav_bytes(),
            worker=worker,
            verifier=lambda selection: None,
        )
        result = preview.speak("must-not-synthesize.")

        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "preview_unavailable")
        self.assertEqual(calls, [])

    def test_close_is_idempotent_and_speak_after_close_fails_closed(self):
        class Backend:
            backend_name = "close-backend"

            def __init__(self):
                self.started = []
                self.closed = 0

            def start(self, audio):
                self.started.append(audio)

            def poll_complete(self):
                return True

            def stop(self, timeout):
                return StopResult(True, "backend_buffer_returned", "reset")

            def close(self):
                self.closed += 1

        backend = Backend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        with patch("app.tts.real_playback.PlaybackWorker", return_value=worker):
            preview = RealVoicePreview(self.selection, synthesizer=lambda *_: wav_bytes(),
                                       verifier=lambda selection: None)

        preview.close()
        preview.close()
        self.assertEqual(backend.closed, 1)

        result = preview.speak("after-close.")
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.error_code, "preview_unavailable")
        self.assertFalse(result.stop_unconfirmed)
        self.assertEqual(backend.started, [])

    def test_close_and_cancel_concurrently_are_safe_and_close_remains_idempotent(self):
        class Backend:
            backend_name = "close-cancel-backend"

            def __init__(self):
                self.started = []
                self.closed = 0

            def start(self, audio):
                self.started.append(audio)

            def poll_complete(self):
                return False

            def stop(self, timeout):
                return StopResult(True, "backend_buffer_returned", "reset")

            def close(self):
                self.closed += 1

        backend = Backend()
        worker = PlaybackWorker(lambda: backend, stop_timeout=0.1)
        with patch("app.tts.real_playback.PlaybackWorker", return_value=worker):
            preview = RealVoicePreview(self.selection, synthesizer=lambda *_: wav_bytes(),
                                       verifier=lambda selection: None)

        result_box = {}
        speak_thread = threading.Thread(
            target=lambda: result_box.setdefault("result", preview.speak("one."))
        )
        speak_thread.start()
        deadline = time.monotonic() + 1
        while preview.active_ticket is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(preview.active_ticket)

        barrier = threading.Barrier(4)
        errors = []

        def close_thread():
            try:
                barrier.wait(1)
                preview.close()
            except Exception as exc:  # pragma: no cover - failure is asserted below
                errors.append(exc)

        def cancel_thread():
            try:
                barrier.wait(1)
                result_box["cancel"] = preview.cancel()
            except Exception as exc:  # pragma: no cover - failure is asserted below
                errors.append(exc)

        threads = [threading.Thread(target=close_thread),
                   threading.Thread(target=close_thread),
                   threading.Thread(target=cancel_thread)]
        for thread in threads:
            thread.start()
        barrier.wait(1)
        for thread in threads:
            thread.join(1)
        speak_thread.join(1)

        self.assertFalse(errors)
        self.assertFalse(speak_thread.is_alive())
        self.assertTrue(result_box["result"].text_fallback)
        self.assertIn(result_box["result"].error_code,
                      {"stopped_acknowledged", "device_stop_unconfirmed"})
        self.assertIn(result_box.get("cancel"),
                      {PlaybackState.STOP_ACKED, PlaybackState.STOP_TIMEOUT,
                       PlaybackState.UNKNOWN, PlaybackState.FAILED})
        preview.close()
        self.assertEqual(backend.closed, 1)
if __name__ == "__main__":
    unittest.main()






