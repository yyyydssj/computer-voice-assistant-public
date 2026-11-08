from __future__ import annotations

import io
from email.message import Message
from threading import Event
import struct
import unittest
import wave
import urllib.error
import app.tts.providers.http_wav as http_wav
from urllib.request import Request

from app.tts.providers import (
    VoiceStudioHttpWavConfig,
    VoiceStudioHttpWavError,
    VoiceStudioHttpWavProvider,
    VoiceStudioSentenceRequest,
)


def wav_bytes(*, pcm: bytes = b"\x01\x00" * 12, channels: int = 1, width: int = 2, rate: int = 24000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(pcm)
    return stream.getvalue()


class FakeResponse:
    def __init__(self, status: int, content_type: str, body: bytes | object) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes | object:
        return self.body


class RecordingOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[Request, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.requests.append((request, timeout))
        return self.response


class VoiceStudioHttpWavProviderTests(unittest.TestCase):
    def make_request(self, **overrides: object) -> VoiceStudioSentenceRequest:
        values: dict[str, object] = {
            "reply_id": "reply-1",
            "segment_id": "segment-1",
            "provider_request_id": "req-1",
            "provider_generation": 2,
            "text": "第一句。",
            "profile_id": "xiaobing",
            "language": "zh-CN",
            "speed": 1.0,
            "engine": "omnivoice",
        }
        values.update(overrides)
        return VoiceStudioSentenceRequest(**values)  # type: ignore[arg-type]

    def test_sends_one_sentence_multipart_and_normalizes_complete_wav(self) -> None:
        opener = RecordingOpener(FakeResponse(200, "audio/wav; charset=binary", wav_bytes()))
        provider = VoiceStudioHttpWavProvider(
            VoiceStudioHttpWavConfig(timeout_seconds=7.5), opener=opener
        )
        request = self.make_request()
        chunks = list(provider.stream_sentence(request, cancelled=Event()))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].data, b"\x01\x00" * 12)
        self.assertTrue(chunks[0].is_final)
        self.assertEqual(chunks[0].provider_generation, 2)
        sent, timeout = opener.requests[0]
        self.assertEqual(timeout, 7.5)
        self.assertEqual(sent.full_url, "http://127.0.0.1:3900/generate")
        self.assertEqual(sent.get_header("Accept"), "audio/wav")
        self.assertEqual(sent.get_header("X-p7-request-id"), "req-1")
        self.assertEqual(sent.get_header("X-p7-generation"), "2")
        body = sent.data.decode("utf-8")
        boundary = sent.get_header("Content-type").split("boundary=", 1)[1]
        self.assertTrue(body.startswith(f"--{boundary}\r\n"))
        self.assertTrue(body.endswith(f"--{boundary}--\r\n"))
        self.assertEqual(body.count(f"--{boundary}\r\n"), 6)
        for field, value in (
            ("text", "第一句。"),
            ("profile_id", "xiaobing"),
            ("language", "zh-CN"),
            ("speed", "1.0"),
            ("stream", "false"),
            ("engine", "omnivoice"),
        ):
            self.assertIn(f'name="{field}"', body)
            self.assertIn(value, body)

    def test_rejects_non_loopback_config_and_invalid_limits(self) -> None:
        for endpoint in ("https://example.com", "http://192.168.1.2:3900", "http://127.0.0.1:3900?x=1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                VoiceStudioHttpWavConfig(endpoint=endpoint)
        with self.assertRaises(ValueError):
            VoiceStudioHttpWavConfig(timeout_seconds=0)
        with self.assertRaises(ValueError):
            VoiceStudioHttpWavConfig(max_audio_bytes=44)
        with self.assertRaises(ValueError):
            VoiceStudioHttpWavConfig(path="generate")
        for endpoint in ("http://localhost:3900", "http://127.0.0.1:3900", "http://[::1]:3900"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(VoiceStudioHttpWavConfig(endpoint=endpoint).endpoint, endpoint)
        for endpoint in ("http://[::2]:3900", "http://[::1]:bad", "http://[::1]:0"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                VoiceStudioHttpWavConfig(endpoint=endpoint)

    def test_rejects_bad_status_content_type_format_silence_and_oversize(self) -> None:
        cases = (
            (FakeResponse(503, "audio/wav", wav_bytes()), "http_status_503"),
            (FakeResponse(200, "application/octet-stream", wav_bytes()), "content_type_invalid"),
            (FakeResponse(200, "audio/wav", b"not wav"), "audio_invalid"),
            (FakeResponse(200, "audio/wav", wav_bytes(rate=16000)), "audio_format_invalid"),
            (FakeResponse(200, "audio/wav", wav_bytes(pcm=b"\x00" * 24)), "audio_silent"),
        )
        for response, code in cases:
            with self.subTest(code=code):
                provider = VoiceStudioHttpWavProvider(opener=RecordingOpener(response))
                with self.assertRaisesRegex(VoiceStudioHttpWavError, code):
                    list(provider.stream_sentence(self.make_request(), cancelled=Event()))
        oversized = VoiceStudioHttpWavProvider(
            VoiceStudioHttpWavConfig(max_audio_bytes=60),
            opener=RecordingOpener(FakeResponse(200, "audio/wav", wav_bytes())),
        )
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "audio_too_large"):
            list(oversized.stream_sentence(self.make_request(), cancelled=Event()))

    def test_cancellation_blocks_request_and_discards_late_response(self) -> None:
        opener = RecordingOpener(FakeResponse(200, "audio/wav", wav_bytes()))
        provider = VoiceStudioHttpWavProvider(opener=opener)
        before = Event()
        before.set()
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "cancelled_before_request"):
            list(provider.stream_sentence(self.make_request(), cancelled=before))
        self.assertEqual(opener.requests, [])
        after = Event()
        original = provider._request

        def request_then_cancel(http_request: Request, *, cancelled: Event):
            result = original(http_request, cancelled=cancelled)
            cancelled.set()
            return result

        provider._request = request_then_cancel  # type: ignore[method-assign]
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "cancelled_after_response"):
            list(provider.stream_sentence(self.make_request(), cancelled=after))

    def test_close_rejects_future_request_and_identity_is_from_snapshot(self) -> None:
        opener = RecordingOpener(FakeResponse(200, "audio/wav", wav_bytes()))
        provider = VoiceStudioHttpWavProvider(opener=opener)
        provider.close()
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "provider_closed"):
            list(provider.stream_sentence(self.make_request(), cancelled=Event()))
        self.assertEqual(opener.requests, [])

    def test_transport_status_and_malformed_response_fail_closed(self) -> None:
        errors = (
            (urllib.error.HTTPError("http://127.0.0.1/", 429, "rate", {}, None), "http_status_429"),
            (urllib.error.URLError("offline"), "transport_failed"),
            (TimeoutError("timeout"), "transport_failed"),
            (OSError("socket"), "transport_failed"),
        )
        for error, expected in errors:
            def opener(_request, *, timeout, exc=error):
                raise exc
            with self.subTest(error=type(error).__name__):
                provider = VoiceStudioHttpWavProvider(opener=opener)
                with self.assertRaises(VoiceStudioHttpWavError) as caught:
                    list(provider.stream_sentence(self.make_request(), cancelled=Event()))
                self.assertEqual(caught.exception.code, expected)
        for response, expected in (
            (FakeResponse(None, "audio/wav", wav_bytes()), "response_status_missing"),
            (FakeResponse(200, "audio/wav", "not-bytes"), "response_body_not_bytes"),
        ):
            with self.subTest(expected=expected):
                provider = VoiceStudioHttpWavProvider(opener=RecordingOpener(response))
                with self.assertRaises(VoiceStudioHttpWavError) as caught:
                    list(provider.stream_sentence(self.make_request(), cancelled=Event()))
                self.assertEqual(caught.exception.code, expected)

    def test_rejects_truncated_wav_and_enforces_inclusive_frame_limit(self) -> None:
        audio = wav_bytes()
        missing_byte = audio[:-1]
        mismatched_header = bytearray(audio)
        data_header = mismatched_header.find(b"data")
        struct.pack_into("<I", mismatched_header, data_header + 4, 26)
        for malformed in (missing_byte, bytes(mismatched_header)):
            with self.subTest(malformed=malformed[:12]):
                provider = VoiceStudioHttpWavProvider(opener=RecordingOpener(FakeResponse(200, "audio/wav", malformed)))
                with self.assertRaisesRegex(VoiceStudioHttpWavError, "audio_truncated"):
                    list(provider.stream_sentence(self.make_request(), cancelled=Event()))
        accepted = VoiceStudioHttpWavProvider(
            VoiceStudioHttpWavConfig(max_frames=12), opener=RecordingOpener(FakeResponse(200, "audio/wav", audio))
        )
        self.assertEqual(len(list(accepted.stream_sentence(self.make_request(), cancelled=Event()))), 1)
        rejected = VoiceStudioHttpWavProvider(
            VoiceStudioHttpWavConfig(max_frames=11), opener=RecordingOpener(FakeResponse(200, "audio/wav", audio))
        )
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "audio_frames_invalid"):
            list(rejected.stream_sentence(self.make_request(), cancelled=Event()))

    def test_default_opener_rejects_redirect_and_close_race(self) -> None:
        provider = VoiceStudioHttpWavProvider()
        handler = next(item for item in http_wav._DEFAULT_HTTP_OPENER.handlers if isinstance(item, http_wav._NoRedirectHandler))
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "redirect_not_allowed"):
            handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/")
        fake = VoiceStudioHttpWavProvider(opener=RecordingOpener(FakeResponse(200, "audio/wav", wav_bytes())))
        original = fake._request
        def close_after_response(request, *, cancelled):
            response = original(request, cancelled=cancelled)
            fake.close()
            return response
        fake._request = close_after_response  # type: ignore[method-assign]
        with self.assertRaisesRegex(VoiceStudioHttpWavError, "provider_closed"):
            list(fake.stream_sentence(self.make_request(), cancelled=Event()))


if __name__ == "__main__":
    unittest.main()