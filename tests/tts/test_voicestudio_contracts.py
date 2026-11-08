from __future__ import annotations

import unittest
from threading import Event

from app.tts.providers import (
    FakeHttpProvider,
    FakePCMSink,
    FakeWebSocketProvider,
    PCMChunk,
    PCMChunkStreamValidator,
    VoiceStudioContractError,
    VoiceStudioSentenceRequest,
)


class VoiceStudioContractTests(unittest.TestCase):
    def make_request(self, **overrides: object) -> VoiceStudioSentenceRequest:
        values: dict[str, object] = {
            "reply_id": "reply-1",
            "segment_id": "segment-1",
            "provider_request_id": "req-1",
            "provider_generation": 1,
            "text": "你好。",
            "profile_id": "profile-1",
            "language": "zh",
            "speed": 1.0,
            "engine": "omnivoice",
        }
        values.update(overrides)
        return VoiceStudioSentenceRequest(**values)  # type: ignore[arg-type]

    def make_chunk(self, request: VoiceStudioSentenceRequest, **overrides: object) -> PCMChunk:
        values: dict[str, object] = {
            "reply_id": request.reply_id,
            "segment_id": request.segment_id,
            "provider_request_id": request.provider_request_id,
            "provider_generation": request.provider_generation,
            "sequence": 1,
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "data": b"\x01\x00" * 4,
            "is_final": True,
        }
        values.update(overrides)
        return PCMChunk(**values)  # type: ignore[arg-type]

    def test_sentence_request_is_immutable_and_rejects_invalid_identity(self) -> None:
        request = self.make_request()
        with self.assertRaises((AttributeError, TypeError)):
            request.text = "changed"  # type: ignore[misc]
        with self.assertRaises(VoiceStudioContractError):
            self.make_request(reply_id="bad id")
        with self.assertRaises(VoiceStudioContractError):
            self.make_request(provider_generation=0)
        with self.assertRaises(VoiceStudioContractError):
            self.make_request(text="   ")
        with self.assertRaises(VoiceStudioContractError):
            self.make_request(speed=1)  # type: ignore[arg-type]

    def test_contract_violations_carry_fixed_short_codes(self) -> None:
        """真机取证（2026-10-07）：无码的契约异常在日志里只剩一个类型名。

        `pcm_seq` 长回复只播开头时，日志记的是 `speech_error=VoiceStudioContractError`
        而没有任何错误码；每条契约规则因此带上固定短码，行为与消息一律不变。
        """

        cases = (
            (lambda: self.make_request(reply_id="bad id"), "identifier_invalid"),
            (lambda: self.make_request(text=""), "text_invalid"),
            (lambda: self.make_request(text="   "), "text_blank"),
            (lambda: self.make_request(text="\n"), "text_blank"),
            (lambda: self.make_request(provider_generation=0), "generation_invalid"),
            (lambda: self.make_request(speed=1), "speed_invalid"),  # type: ignore[arg-type]
        )
        for build, expected in cases:
            with self.subTest(code=expected), self.assertRaises(
                VoiceStudioContractError
            ) as caught:
                build()
            self.assertEqual(caught.exception.code, expected)

    def test_chunk_and_validator_violations_carry_fixed_short_codes(self) -> None:
        request = self.make_request()
        with self.assertRaises(VoiceStudioContractError) as caught:
            self.make_chunk(request, sample_rate=16000)
        self.assertEqual(caught.exception.code, "chunk_rate_invalid")
        with self.assertRaises(VoiceStudioContractError) as caught:
            self.make_chunk(request, data=b"")
        self.assertEqual(caught.exception.code, "chunk_empty")

        validator = PCMChunkStreamValidator(request)
        with self.assertRaises(VoiceStudioContractError) as caught:
            validator.accept(self.make_chunk(request, sequence=2))
        self.assertEqual(caught.exception.code, "validator_sequence_mismatch")
        with self.assertRaises(VoiceStudioContractError) as caught:
            validator.finish()
        self.assertEqual(caught.exception.code, "validator_missing_final")

    def test_pcm_chunk_rejects_format_and_payload_errors(self) -> None:
        request = self.make_request()
        for overrides in (
            {"sample_rate": 16000},
            {"channels": 2},
            {"sample_width": 1},
            {"data": b"\x00"},
            {"data": b""},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(VoiceStudioContractError):
                self.make_chunk(request, **overrides)

    def test_validator_accepts_only_matching_order_and_one_final_chunk(self) -> None:
        request = self.make_request()
        validator = PCMChunkStreamValidator(request)
        first = self.make_chunk(request, sequence=1, is_final=False)
        final = self.make_chunk(request, sequence=2, is_final=True)
        self.assertIs(validator.accept(first), first)
        self.assertIs(validator.accept(final), final)
        self.assertEqual(validator.finish(), (first, final))
        with self.assertRaises(VoiceStudioContractError):
            validator.accept(final)

    def test_validator_rejects_duplicate_and_skipped_sequence(self) -> None:
        request = self.make_request()
        skipped = PCMChunkStreamValidator(request)
        with self.assertRaises(VoiceStudioContractError):
            skipped.accept(self.make_chunk(request, sequence=2, is_final=True))

        duplicate = PCMChunkStreamValidator(request)
        duplicate.accept(self.make_chunk(request, sequence=1, is_final=False))
        with self.assertRaises(VoiceStudioContractError):
            duplicate.accept(self.make_chunk(request, sequence=1, is_final=True))

    def test_validator_rejects_old_generation_and_other_request_identity(self) -> None:
        request = self.make_request()
        validator = PCMChunkStreamValidator(request)
        with self.assertRaises(VoiceStudioContractError):
            validator.accept(self.make_chunk(request, provider_generation=2))
        with self.assertRaises(VoiceStudioContractError):
            validator.accept(self.make_chunk(request, segment_id="segment-2"))

    def test_validator_requires_final_chunk_and_rejects_cancelled_chunks(self) -> None:
        request = self.make_request()
        validator = PCMChunkStreamValidator(request)
        validator.accept(self.make_chunk(request, is_final=False))
        with self.assertRaises(VoiceStudioContractError):
            validator.finish()
        validator.cancel()
        with self.assertRaises(VoiceStudioContractError):
            validator.accept(self.make_chunk(request, sequence=2, is_final=True))
        with self.assertRaises(VoiceStudioContractError):
            validator.finish()

    def test_fake_ws_and_http_providers_share_the_same_pcm_contract(self) -> None:
        request = self.make_request()
        chunks = [
            self.make_chunk(request, sequence=1, is_final=False),
            self.make_chunk(request, sequence=2, is_final=True),
        ]
        for provider_type in (FakeWebSocketProvider, FakeHttpProvider):
            provider = provider_type(chunks)
            emitted = tuple(provider.stream_sentence(request, cancelled=Event()))
            self.assertEqual(emitted, tuple(chunks))
            self.assertEqual(provider.calls, [request])
            provider.close()
            self.assertEqual(provider.close_calls, 1)
            with self.assertRaises(VoiceStudioContractError):
                tuple(provider.stream_sentence(request, cancelled=Event()))

    def test_fake_provider_stops_after_cancellation_without_emitting_late_chunk(self) -> None:
        request = self.make_request()
        cancelled = Event()

        def script(_: VoiceStudioSentenceRequest):
            yield self.make_chunk(request, sequence=1, is_final=False)
            cancelled.set()
            yield self.make_chunk(request, sequence=2, is_final=True)

        provider = FakeWebSocketProvider(script)
        stream = provider.stream_sentence(request, cancelled=cancelled)
        self.assertEqual(next(stream).sequence, 1)
        with self.assertRaises(VoiceStudioContractError):
            next(stream)

    def test_fake_pcm_sink_enforces_request_identity_and_completion(self) -> None:
        request = self.make_request()
        sink = FakePCMSink()
        sink.start(request)
        first = self.make_chunk(request, is_final=False)
        final = self.make_chunk(request, sequence=2, is_final=True)
        sink.write(first)
        sink.write(final)
        self.assertEqual(sink.finish(request), (first, final))
        with self.assertRaises(VoiceStudioContractError):
            sink.write(self.make_chunk(request, sequence=3, is_final=True))

    def test_fake_pcm_sink_cancellation_blocks_late_audio(self) -> None:
        request = self.make_request()
        sink = FakePCMSink()
        sink.start(request)
        sink.cancel(request)
        with self.assertRaises(VoiceStudioContractError):
            sink.write(self.make_chunk(request))


if __name__ == "__main__":
    unittest.main()
