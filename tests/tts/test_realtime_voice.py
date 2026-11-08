from __future__ import annotations

import io
import wave
import unittest
import getpass
import threading
from unittest.mock import patch

from app.tts.private_selection import CONFIRMED_PROFILE, VoiceSelection
from app.tts.real_playback import RealtimeVoiceSession
from app.tts.providers import FakeHttpProvider, PCMChunk

def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\x10\x04" * 2400)
    return out.getvalue()

class RealtimeVoiceTests(unittest.TestCase):
    def setUp(self):
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser(), speed=1.0)

    def test_first_complete_sentence_synthesizes_before_finish(self):
        calls = []
        def synth(text, selection):
            calls.append(text); return wav_bytes()
        session = RealtimeVoiceSession(self.selection, synthesizer=synth, player=lambda audio: calls.append("played"), verifier=lambda _: None)
        session.push_text_chunk("first sentence.")
        session.push_text_chunk("second sentence.")
        self.assertEqual(session.finish(), ("first sentence.", "second sentence."))
        self.assertEqual(calls, ["first sentence.", "played", "second sentence.", "played"])


    def test_http_wav_provider_mode_synthesizes_each_sentence_in_order(self):
        calls = []

        def script(request):
            calls.append(request)
            yield PCMChunk(
                reply_id=request.reply_id, segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation, sequence=1,
                sample_rate=24000, channels=1, sample_width=2,
                data=b"\x10\x04" * 12, is_final=True,
            )

        provider = FakeHttpProvider(script)
        played = []
        session = RealtimeVoiceSession(
            self.selection, provider=provider, provider_mode="http_wav",
            player=lambda audio: played.append(audio), verifier=lambda _: None,
        )
        session.push_text_chunk("first sentence. second sentence.")
        self.assertEqual(session.finish(), ("first sentence.", "second sentence."))
        self.assertEqual([request.text for request in calls], ["first sentence.", "second sentence."])
        self.assertEqual([request.segment_id for request in calls], ["segment-1", "segment-2"])
        self.assertEqual([request.provider_request_id for request in calls], [
            f"http-wav-{calls[0].reply_id}-1", f"http-wav-{calls[0].reply_id}-2",
        ])
        self.assertEqual([request.provider_generation for request in calls], [1, 1])
        self.assertEqual(len(played), 2)
        self.assertTrue(all(audio[:4] == b"RIFF" for audio in played))

    def test_http_wav_provider_failure_preserves_unplayed_text_in_session_error(self):
        class BrokenProvider:
            def stream_sentence(self, request, *, cancelled):
                raise RuntimeError("offline_provider_failure")
                yield  # pragma: no cover
            def close(self):
                pass

        session = RealtimeVoiceSession(
            self.selection, provider=BrokenProvider(), provider_mode="http_wav",
            player=lambda audio: None, verifier=lambda _: None,
        )
        session.push_text_chunk("first sentence. second sentence.")
        with self.assertRaises(RuntimeError):
            session.finish()
        self.assertEqual(session.error.args, ("offline_provider_failure",))

    def test_cancel_drops_pending_audio(self):
        calls = []
        session = RealtimeVoiceSession(self.selection, synthesizer=lambda text, selection: calls.append(text) or wav_bytes(), player=lambda audio: None, verifier=lambda _: None)
        session.push_text_chunk("????")
        session.cancel()
        session.close()
        self.assertLessEqual(len(calls), 1)

    def test_http_first_sentence_reaches_provider_before_finish(self):
        started = threading.Event()
        calls = []
        def script(request):
            calls.append(request.text)
            started.set()
            yield PCMChunk(
                reply_id=request.reply_id, segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation, sequence=1,
                sample_rate=24000, channels=1, sample_width=2,
                data=b"\x10\x04" * 12, is_final=True,
            )
        session = RealtimeVoiceSession(
            self.selection, provider=FakeHttpProvider(script), provider_mode="http_wav",
            player=lambda _: None, verifier=lambda _: None,
        )
        try:
            session.push_text_chunk("first sentence.")
            self.assertTrue(started.wait(2), "complete sentence should not wait for finish")
            self.assertEqual(calls, ["first sentence."])
            self.assertEqual(session.finish(), ("first sentence.",))
        finally:
            session.close()

    def test_http_cancel_drops_late_audio_and_does_not_submit_second_sentence(self):
        started = threading.Event()
        released = threading.Event()
        calls = []
        played = []
        def script(request):
            calls.append(request.text)
            started.set()
            if not released.wait(2):
                raise TimeoutError("test release timeout")
            yield PCMChunk(
                reply_id=request.reply_id, segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation, sequence=1,
                sample_rate=24000, channels=1, sample_width=2,
                data=b"\x10\x04" * 12, is_final=True,
            )
        session = RealtimeVoiceSession(
            self.selection, provider=FakeHttpProvider(script), provider_mode="http_wav",
            player=played.append, verifier=lambda _: None,
        )
        try:
            session.push_text_chunk("first sentence. second sentence.")
            self.assertTrue(started.wait(2))
            session.cancel()
            released.set()
            session.close()
            self.assertEqual(calls, ["first sentence."])
            self.assertEqual(played, [])
        finally:
            released.set()
            session.close()

    def test_http_failure_does_not_replay_old_sentence_in_new_session(self):
        def failed(_request):
            raise RuntimeError("provider_failed")
            yield  # pragma: no cover
        first = RealtimeVoiceSession(
            self.selection, provider=FakeHttpProvider(failed), provider_mode="http_wav",
            player=lambda _: None, verifier=lambda _: None,
        )
        first.push_text_chunk("old sentence.")
        with self.assertRaisesRegex(RuntimeError, "provider_failed"):
            first.finish()
        first.close()
        calls = []
        second = RealtimeVoiceSession(
            self.selection, provider=FakeHttpProvider(lambda request: (
                PCMChunk(reply_id=request.reply_id, segment_id=request.segment_id,
                         provider_request_id=request.provider_request_id,
                         provider_generation=request.provider_generation, sequence=1,
                         sample_rate=24000, channels=1, sample_width=2,
                         data=b"\x10\x04" * 12, is_final=True) for _ in range(1)
            )), provider_mode="http_wav", player=lambda _: calls.append("played"), verifier=lambda _: None,
        )
        try:
            second.push_text_chunk("new sentence.")
            self.assertEqual(second.finish(), ("new sentence.",))
            self.assertNotEqual(first._reply_id, second._reply_id)
            self.assertEqual(calls, ["played"])
        finally:
            second.close()

    def test_pending_bound_never_blocks_the_agent_thread(self):
        started = threading.Event()
        released = threading.Event()
        def script(request):
            started.set()
            if not released.wait(3):
                raise TimeoutError("test release timeout")
            yield PCMChunk(
                reply_id=request.reply_id, segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation, sequence=1,
                sample_rate=24000, channels=1, sample_width=2,
                data=b"\x10\x04" * 12, is_final=True,
            )
        session = RealtimeVoiceSession(
            self.selection, provider=FakeHttpProvider(script), provider_mode="http_wav",
            player=lambda _: None, verifier=lambda _: None, max_pending_segments=2,
        )
        sender = threading.Thread(target=lambda: session.push_text_chunk("one. two. three. four. five."), daemon=True)
        try:
            sender.start()
            sender.join(timeout=2)
            # The Agent thread must never wait on TTS; overload is reported instead.
            self.assertFalse(sender.is_alive(), "push_text_chunk must never block the Agent thread")
            self.assertTrue(session.overloaded)
            self.assertTrue(started.wait(2))
        finally:
            session.cancel()
            released.set()
            sender.join(2)
            session.close()
        self.assertFalse(sender.is_alive())

if __name__ == "__main__": unittest.main()

