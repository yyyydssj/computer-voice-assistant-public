from __future__ import annotations

import getpass
import threading
import time
import unittest
from threading import Event

from app.tts.pcm_playback import PCMPlaybackWorker
from app.tts.playback import ProviderRequestPlaybackState
from app.tts.playback_worker import PlaybackState, StopResult
from app.tts.private_selection import (
    CONFIRMED_PROFILE,
    CONFIRMED_REFERENCE_SHA256,
    VoiceSelection,
)
from app.tts.providers import (
    PCMChunk,
    VoiceStudioHttpWavError,
    VoiceStudioSentenceRequest,
    VoiceStudioWebSocketError,
)
from app.tts.routing import TTSFallbackRouter, TTSRoute
from app.tts.streaming_session import (
    PUSH_OUT_OF_ORDER,
    StreamingVoiceSession,
    StreamingVoiceSessionError,
)
from app.tts.transport import VoiceStudioTransportRouter

SAMPLE_RATE = 24_000
BYTES_PER_MS = 48


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


def make_selection() -> VoiceSelection:
    return VoiceSelection(
        profile_id=CONFIRMED_PROFILE,
        owner=getpass.getuser(),
        reference_sha256=CONFIRMED_REFERENCE_SHA256,
    )


class FakePCMBackend:
    backend_name = "fake-pcm"
    max_buffer_bytes = 9_600

    def __init__(self, *, auto_return: bool = True) -> None:
        self.auto_return = auto_return
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
            return self._inflight < 3

    def submit(self, pcm: bytes) -> None:
        with self._lock:
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
        with self._lock:
            self._inflight = 0
            self._drained = True
        return StopResult(True, "backend_buffer_returned", "fake_reset")

    def close(self) -> None:
        self.close_calls += 1


class ScriptedProvider:
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("no scripted behaviour left")
        behaviour = self.script.pop(0)
        if isinstance(behaviour, BaseException):
            raise behaviour
        for item in behaviour:  # type: ignore[union-attr]
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self) -> None:
        self.close_calls += 1


class ChunkScriptProvider:
    """Emits a final chunk for every sentence with fresh identities."""

    def __init__(self, *, milliseconds: int = 100) -> None:
        self.milliseconds = milliseconds
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=1,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(self.milliseconds), is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class PartialThenFailProvider:
    """Emits one correctly-identified chunk, then a transport failure."""

    def __init__(self, code: str = "server_error") -> None:
        self.code = code
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=1,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=False,
        )
        raise VoiceStudioWebSocketError(self.code)

    def close(self) -> None:
        self.close_calls += 1


class StreamingSessionTestCase(unittest.TestCase):
    def make_session(self, *, ws=None, http=None, mode: str = "auto",
                     fallback: TTSFallbackRouter | None = None, backend=None,
                     max_pending_segments: int = 8,
                     max_queued_audio_ms: int = 8000, **overrides: object):
        backend = backend or FakePCMBackend()
        worker = PCMPlaybackWorker(lambda: backend, start_buffer_ms=1, stop_timeout=0.5,
                                   max_queued_audio_ms=max_queued_audio_ms)
        self.addCleanup(worker.close)
        router = VoiceStudioTransportRouter(
            mode=mode,
            ws_provider=ws if ws is not None else ChunkScriptProvider(),
            http_provider=http if http is not None else ChunkScriptProvider(),
        )
        self.addCleanup(router.close)
        session = StreamingVoiceSession(
            make_selection(),
            transport_router=router,
            worker=worker,
            fallback_router=fallback,
            verifier=lambda selection: None,
            max_pending_segments=max_pending_segments,
            **overrides,  # type: ignore[arg-type]
        )
        self.addCleanup(session.close)
        return session, worker, router, backend


class StreamingVoiceSessionTests(StreamingSessionTestCase):
    def test_single_sentence_is_synthesised_and_played(self) -> None:
        session, _worker, _router, backend = self.make_session()
        session.push_text_chunk("第一句。")
        played = session.finish()
        self.assertEqual(played, ("第一句。",))
        self.assertIsNone(session.error)
        self.assertEqual(b"".join(backend.submitted), pcm_bytes(100))
        self.assertEqual(backend.stop_calls, 0)

    def test_sentences_are_spoken_in_original_order(self) -> None:
        session, _worker, router, backend = self.make_session()
        session.push_text_chunk("第一句。第二句。第三句。")
        played = session.finish()
        self.assertEqual(played, ("第一句。", "第二句。", "第三句。"))
        self.assertEqual(len(backend.submitted), 3)
        identities = [(request.segment_id, request.provider_request_id) for request in router._ws.calls]  # noqa: SLF001
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual(router._ws.calls[0].reply_id, session.reply_id)  # noqa: SLF001

    def test_final_tail_is_flushed_without_a_hard_boundary(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("没有句号的尾巴")
        self.assertEqual(session.finish(), ("没有句号的尾巴",))

    def test_pcm_chunk_identity_matches_the_sentence_request(self) -> None:
        provider = ChunkScriptProvider()
        session, _worker, _router, _backend = self.make_session(ws=provider)
        session.push_text_chunk("一句话。")
        session.finish()
        request = provider.calls[0]
        self.assertEqual(request.profile_id, make_selection().profile_id)
        self.assertEqual(request.provider_generation, 1)
        self.assertEqual(request.text, "一句话。")


class StreamingVoiceSessionBlankPushTests(StreamingSessionTestCase):
    """真机 2026-10-09（`ws_pcm`，18:54:59）：空行切出的纯空白 push 曾让整条尾巴**静默**消失。

    真机事实：`text_length=233 / pushed_chars=229 / accepted_chars=74 / played_chars=74`
    （只播第一段），而 `error` 为空、`enqueue_dropped=0`、`provider_failures=[]`——
    因为纯空白块被分段器以 `EMPTY_TEXT` 拒收时**不消费序号**，其后每个 chunk 都被判越序。
    """

    def test_pieces_with_a_blank_line_speak_the_whole_reply(self) -> None:
        from app.runtime.voice_entry import split_for_push

        session, _worker, _router, backend = self.make_session()
        pieces = split_for_push("第一句。\n\n第二句。")
        # The blank line really does produce standalone whitespace-only pieces.
        self.assertIn("\n", pieces)
        for piece in pieces:
            session.push_text_chunk(piece)
        played = session.finish()

        self.assertEqual(played, ("第一句。", "第二句。"))
        self.assertIsNone(session.error)
        self.assertEqual(len(backend.submitted), 2)

    def test_a_blank_push_never_advances_the_chunk_sequence(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("第一句。")
        session.push_text_chunk("\n")
        session.push_text_chunk("   ")
        session.push_text_chunk("第二句。")

        self.assertEqual(session.finish(), ("第一句。", "第二句。"))
        self.assertIsNone(session.error)

    def test_a_real_sequence_gap_is_recorded_instead_of_silently_dropped(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("第一句。")
        # 故意造一次错位（修复后公开 API 已不可能产生它）：只验证"错位必须响亮"。
        with session._condition:  # noqa: SLF001 - 测试专用：直接跳过序号
            session._sequence += 1

        with self.assertRaises(StreamingVoiceSessionError):
            session.push_text_chunk("第二句。")

        self.assertIsNotNone(session.error)
        self.assertEqual(str(session.error), PUSH_OUT_OF_ORDER)


class StreamingVoiceSessionFallbackTests(StreamingSessionTestCase):
    def test_transport_failure_without_edge_keeps_text_and_records_error(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        fallback = TTSFallbackRouter()
        session, _worker, _router, backend = self.make_session(mode="ws_pcm", ws=ws, fallback=fallback)
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertIsNotNone(session.error)
        self.assertEqual(backend.submitted, [])
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, TTSRoute.TEXT)
        self.assertFalse(decision.replay_current_segment)

    def test_transport_failure_before_audio_uses_configured_edge(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, _backend = self.make_session(mode="ws_pcm", ws=ws, fallback=fallback)
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, TTSRoute.EDGE)
        self.assertTrue(decision.cancel_previous_provider_request)

    def test_failure_after_audio_never_switches_voice(self) -> None:
        ws = PartialThenFailProvider("server_error")
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, _backend = self.make_session(ws=ws, fallback=fallback)
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, TTSRoute.TEXT)
        self.assertFalse(decision.replay_current_segment)
        self.assertEqual(decision.playback_state, ProviderRequestPlaybackState.PLAYBACK_STARTED)

    def test_failure_without_a_router_only_records_the_error(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("protocol_failed")])
        session, _worker, _router, _backend = self.make_session(mode="ws_pcm", ws=ws, fallback=None)
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertIsNone(session.routing_decision)
        self.assertIsNotNone(session.error)

    def test_ws_pcm_mode_reports_the_failure_without_http(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        http = ScriptedProvider([[PCMChunk(
            reply_id="r", segment_id="s", provider_request_id="p", provider_generation=1,
            sequence=1, sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=True,
        )]])
        session, _worker, _router, _backend = self.make_session(mode="ws_pcm", ws=ws, http=http)
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertEqual(http.calls, [])

    def test_auto_mode_falls_back_to_http_for_the_same_sentence(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        http = ChunkScriptProvider()
        session, _worker, router, backend = self.make_session(ws=ws, http=http)
        session.push_text_chunk("第一句。")
        self.assertEqual(session.finish(), ("第一句。",))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(router.snapshot.fallbacks, 1)
        self.assertEqual(b"".join(backend.submitted), pcm_bytes(100))


class StreamingVoiceSessionLifecycleTests(StreamingSessionTestCase):
    def test_cancel_stops_playback_and_ignores_late_text(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("第一句。")
        session.cancel()
        session.cancel()
        session.push_text_chunk("第二句。")
        self.assertTrue(session.cancelled)
        self.assertEqual(session.finish(), ())

    def test_finish_after_cancel_returns_no_new_segments(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("第一句。")
        session.cancel()
        self.assertEqual(session.finish(), ())

    def test_close_is_idempotent_and_marks_the_session_closed(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.close()
        session.close()
        self.assertTrue(session.closed)

    def test_owned_worker_is_closed_by_the_session(self) -> None:
        backend = FakePCMBackend()
        session, _worker, _router, _backend = self.make_session(
            backend=backend, owns_worker=True
        )
        session.close()
        self.assertGreaterEqual(backend.close_calls, 1)

    def test_empty_or_non_string_chunks_are_ignored(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("")
        session.push_text_chunk(None)  # type: ignore[arg-type]
        self.assertEqual(session.finish(), ())

    def test_agent_push_never_blocks_and_overload_is_recorded(self) -> None:
        slow = ChunkScriptProvider(milliseconds=400)
        session, _worker, _router, _backend = self.make_session(
            ws=slow, max_pending_segments=1
        )
        started = time.monotonic()
        session.push_text_chunk("第一句。第二句。第三句。")
        elapsed = time.monotonic() - started
        # The Agent thread returns immediately; the batch is still spoken in order.
        self.assertLess(elapsed, 1.0)
        self.assertEqual(session.finish(), ("第一句。", "第二句。", "第三句。"))
        self.assertTrue(session.overloaded)

    def test_error_property_is_none_after_a_clean_finish(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        session.push_text_chunk("第一句。")
        session.finish()
        self.assertIsNone(session.error)
        self.assertEqual(session.played_segments, ("第一句。",))
        self.assertTrue(session.finished)

    def test_selection_and_language_are_forwarded(self) -> None:
        provider = ChunkScriptProvider()
        session, _worker, _router, _backend = self.make_session(ws=provider)
        session.push_text_chunk("第一句。")
        session.finish()
        self.assertEqual(provider.calls[0].language, "Chinese")
        self.assertEqual(provider.calls[0].speed, 1.0)


class CancellingProvider:
    """Emits one chunk, cancels the session mid-stream, then emits a late chunk."""

    def __init__(self) -> None:
        self.session = None
        self.calls = 0
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls += 1
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=1,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=False,
        )
        self.session.cancel()
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=2,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class StreamingVoiceSessionBargeInTests(StreamingSessionTestCase):
    def test_cancel_is_idempotent_and_advances_generation_once(self) -> None:
        session, _worker, _router, _backend = self.make_session()
        before = session.generation
        session.cancel()
        session.cancel()
        self.assertEqual(session.generation, before + 1)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.played_segments, ())

    def test_cancel_drops_pending_and_returns_no_new_segments(self) -> None:
        slow = ChunkScriptProvider(milliseconds=400)
        session, _worker, _router, _backend = self.make_session(ws=slow)
        session.push_text_chunk("第一句。第二句。")
        session.cancel()
        self.assertEqual(session.finish(), ())

    def test_late_chunk_after_cancel_is_diagnostic_only(self) -> None:
        provider = CancellingProvider()
        session, _worker, _router, backend = self.make_session(mode="ws_pcm", ws=provider)
        provider.session = session
        session.push_text_chunk("第一句。")
        self.assertEqual(session.finish(), ())
        self.assertEqual(session.played_segments, ())
        self.assertTrue(session.late_events, "late audio must be recorded as a diagnostic")
        # The late chunk never reaches the device: at most the pre-cancel chunk was submitted.
        self.assertLessEqual(sum(len(block) for block in backend.submitted), len(pcm_bytes(40)))

    def test_old_generation_chunk_is_rejected_by_a_new_sentence(self) -> None:
        session, worker, _router, _backend = self.make_session()
        current = VoiceStudioSentenceRequest(
            reply_id=session.reply_id, segment_id="segment-1",
            provider_request_id="new-1", provider_generation=1,
            text="新句。", profile_id="profile-1", language="zh", speed=1.0,
        )
        ticket = worker.begin_sentence(current)
        stale = PCMChunk(
            reply_id="old-reply", segment_id="old-segment",
            provider_request_id="old-request", provider_generation=1, sequence=1,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=True,
        )
        with self.assertRaises(Exception):
            worker.push_chunk(stale)
        worker.stop(ticket)

    def test_generation_is_used_by_sentence_requests(self) -> None:
        provider = ChunkScriptProvider()
        session, _worker, _router, _backend = self.make_session(ws=provider)
        session.push_text_chunk("第一句。")
        session.finish()
        self.assertEqual(provider.calls[0].provider_generation, session.generation)

    def test_cancelled_session_never_reports_completion(self) -> None:
        provider = CancellingProvider()
        session, _worker, _router, _backend = self.make_session(mode="ws_pcm", ws=provider)
        provider.session = session
        session.push_text_chunk("第一句。")
        session.finish()
        self.assertFalse(session.finished and not session.cancelled and bool(session.played_segments))
        self.assertTrue(session.cancelled)


class MultiChunkProvider:
    """Emits several blocks for one sentence so device backpressure can build."""

    def __init__(self, *, blocks: int = 6, milliseconds: int = 200) -> None:
        self.blocks = blocks
        self.milliseconds = milliseconds
        self.calls = 0
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls += 1
        for index in range(self.blocks):
            yield PCMChunk(
                reply_id=request.reply_id, segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation, sequence=index + 1,
                sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
                data=pcm_bytes(self.milliseconds), is_final=(index == self.blocks - 1),
            )

    def close(self) -> None:
        self.close_calls += 1


class StreamingVoiceSessionBackpressureTests(StreamingSessionTestCase):
    def test_audio_backpressure_stops_remaining_speech(self) -> None:
        # The device never returns a buffer, so the worker's 200 ms PCM queue fills.
        backend = FakePCMBackend(auto_return=False)
        session, _worker, _router, _backend = self.make_session(
            mode="ws_pcm", ws=MultiChunkProvider(blocks=6, milliseconds=200),
            backend=backend, max_queued_audio_ms=200,
        )
        session.push_text_chunk("第一句。")
        played = session.finish()
        self.assertTrue(session.overloaded)
        self.assertEqual(played, ())
        self.assertIn("audio_backpressure", " ".join(session.late_events))
        session.close()

    def test_long_reply_is_not_truncated_before_audio_backpressure(self) -> None:
        # 8 sentences with a fast device: every sentence must be spoken.
        session, _worker, _router, _backend = self.make_session(
            mode="ws_pcm", ws=ChunkScriptProvider(milliseconds=100)
        )
        sentences = [f"第{index}句。" for index in range(1, 9)]
        for sentence in sentences:
            session.push_text_chunk(sentence)
        played = session.finish()
        self.assertEqual(len(played), 8)
        self.assertFalse(session.overloaded)


if __name__ == "__main__":
    unittest.main()
