"""Offline tests for the routing-decided Edge fallback in the streaming session.

Everything here is offline and deterministic: the Edge provider is a duck-typed
fake (no PowerShell, no audio device, no network), the transport providers are
scripted fakes and no test sleeps. The suite pins the section 13.2 semantics:
one Edge attempt per not-yet-playing sentence, never after audio started, never
a replay, never a third provider, and bit-for-bit unchanged behaviour when no
Edge provider is injected.
"""
from __future__ import annotations

import getpass
import threading
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
    EdgeSapiError,
    PCMChunk,
    VoiceStudioSentenceRequest,
    VoiceStudioWebSocketError,
)
from app.tts.routing import TTSFallbackRouter, TTSRoute, TTSRoutingAction
from app.tts.streaming_session import StreamingVoiceSession, build_streaming_session
from app.tts.transport import VoiceStudioTransportRouter

SAMPLE_RATE = 24_000


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
    """One scripted behaviour per call: an exception or an iterable of chunks."""

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
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=1,
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=pcm_bytes(self.milliseconds),
            is_final=True,
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
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=1,
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=pcm_bytes(40),
            is_final=False,
        )
        raise VoiceStudioWebSocketError(self.code)

    def close(self) -> None:
        self.close_calls += 1


class FakeEdgeProvider:
    """Duck-typed Edge slot provider: no process, no device, no network."""

    def __init__(
        self,
        *,
        blocks: int = 2,
        milliseconds: int = 100,
        error: BaseException | None = None,
        cancel_after_first_block: bool = False,
    ) -> None:
        self.blocks = blocks
        self.milliseconds = milliseconds
        self.error = error
        self.cancel_after_first_block = cancel_after_first_block
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.session: StreamingVoiceSession | None = None
        self._cancelled = False

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        for index in range(self.blocks):
            yield PCMChunk(
                reply_id=request.reply_id,
                segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation,
                sequence=index + 1,
                sample_rate=SAMPLE_RATE,
                channels=1,
                sample_width=2,
                data=pcm_bytes(self.milliseconds),
                is_final=(index == self.blocks - 1),
            )
            if self.cancel_after_first_block and not self._cancelled:
                self._cancelled = True
                assert self.session is not None
                self.session.cancel()


class EdgeFallbackTestCase(unittest.TestCase):
    def make_session(
        self,
        *,
        ws=None,
        http=None,
        mode: str = "ws_pcm",
        fallback: TTSFallbackRouter | None = None,
        edge_provider=None,
        backend=None,
    ):
        backend = backend or FakePCMBackend()
        worker = PCMPlaybackWorker(
            lambda: backend, start_buffer_ms=1, stop_timeout=0.5
        )
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
            edge_provider=edge_provider,
            verifier=lambda selection: None,
        )
        self.addCleanup(session.close)
        return session, worker, router, backend


class EdgeFallbackSuccessTests(EdgeFallbackTestCase):
    def test_not_started_failure_renders_the_same_sentence_once_with_edge(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(blocks=2, milliseconds=100)
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, backend = self.make_session(
            ws=ws, fallback=fallback, edge_provider=edge
        )
        session.push_text_chunk("第一句。")
        played = session.finish()

        self.assertEqual(played, ("第一句。",))
        self.assertIsNone(session.error)
        self.assertEqual(session.played_segments, ("第一句。",))
        self.assertEqual(session.edge_attempts, 1)
        self.assertEqual(session.edge_failures, 0)
        self.assertIn("edge_used@generation1", session.late_events)
        self.assertNotIn("edge_failed@generation1", session.late_events)
        self.assertEqual(b"".join(backend.submitted), pcm_bytes(200))
        self.assertEqual(len(session.timing_samples), 1)
        self.assertTrue(session.verified)

    def test_edge_rerenders_exactly_the_failed_sentence_identity(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(blocks=1, milliseconds=50)
        session, _worker, _router, _backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=edge,
        )
        session.push_text_chunk("第一句。")
        session.finish()

        self.assertEqual(len(ws.calls), 1)
        self.assertEqual(len(edge.calls), 1)
        first, second = ws.calls[0], edge.calls[0]
        self.assertEqual(first, second)
        self.assertEqual(second.text, "第一句。")
        self.assertEqual(second.segment_id, "segment-1")
        self.assertEqual(second.provider_generation, 1)

    def test_decision_stays_the_edge_decision_after_a_successful_rerender(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        session, _worker, _router, _backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=FakeEdgeProvider(blocks=1, milliseconds=50),
        )
        session.push_text_chunk("第一句。")
        session.finish()
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertIs(decision.action, TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT)
        self.assertIs(decision.route, TTSRoute.EDGE)
        self.assertFalse(decision.replay_current_segment)

    def test_build_streaming_session_passes_the_edge_provider_through(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(blocks=1, milliseconds=50)
        backend = FakePCMBackend()
        session = build_streaming_session(
            make_selection(),
            mode="ws_pcm",
            ws_provider=ws,
            http_provider=ChunkScriptProvider(),
            backend_factory=lambda: backend,
            fallback_router=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=edge,
            start_buffer_ms=1,
            verifier=lambda selection: None,
        )
        self.addCleanup(session.close)
        session.push_text_chunk("第一句。")
        self.assertEqual(session.finish(), ("第一句。",))
        self.assertEqual(session.edge_attempts, 1)
        self.assertEqual(len(edge.calls), 1)


class EdgeFallbackBoundaryTests(EdgeFallbackTestCase):
    def test_failure_after_audio_never_calls_edge_and_keeps_the_text_path(self) -> None:
        ws = PartialThenFailProvider("server_error")
        edge = FakeEdgeProvider()
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, backend = self.make_session(
            ws=ws, fallback=fallback, edge_provider=edge
        )
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()

        self.assertEqual(edge.calls, [])
        self.assertEqual(session.edge_attempts, 0)
        self.assertEqual(session.edge_failures, 0)
        self.assertNotIn("edge_used@generation1", session.late_events)
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertIs(decision.route, TTSRoute.TEXT)
        self.assertIs(decision.action, TTSRoutingAction.CONTINUE_TEXT_NO_REPLAY)
        self.assertIs(decision.playback_state, ProviderRequestPlaybackState.PLAYBACK_STARTED)
        self.assertFalse(decision.replay_current_segment)
        # The pre-failure block is the only audio; the sentence is never replayed.
        self.assertLessEqual(sum(len(block) for block in backend.submitted), len(pcm_bytes(40)))

    def test_router_without_edge_configuration_never_uses_the_provider(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider()
        session, _worker, _router, backend = self.make_session(
            ws=ws, fallback=TTSFallbackRouter(), edge_provider=edge
        )
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertEqual(edge.calls, [])
        self.assertEqual(session.edge_attempts, 0)
        self.assertEqual(backend.submitted, [])
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertIs(decision.route, TTSRoute.TEXT)

    def test_edge_provider_absent_keeps_the_existing_behaviour(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, backend = self.make_session(ws=ws, fallback=fallback)
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertEqual(session.edge_attempts, 0)
        self.assertEqual(session.edge_failures, 0)
        self.assertNotIn("edge_used@generation1", session.late_events)
        self.assertNotIn("edge_failed@generation1", session.late_events)
        self.assertEqual(backend.submitted, [])
        # The router's own decision is unchanged: it still reports the Edge slot.
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertIs(decision.route, TTSRoute.EDGE)
        self.assertIs(decision.action, TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT)

    def test_edge_provider_must_expose_the_provider_seam(self) -> None:
        worker = PCMPlaybackWorker(lambda: FakePCMBackend(), start_buffer_ms=1)
        self.addCleanup(worker.close)
        router = VoiceStudioTransportRouter(
            mode="ws_pcm",
            ws_provider=ChunkScriptProvider(),
            http_provider=ChunkScriptProvider(),
        )
        self.addCleanup(router.close)
        with self.assertRaises(TypeError):
            StreamingVoiceSession(
                make_selection(),
                transport_router=router,
                worker=worker,
                edge_provider=object(),
            )

    def test_a_second_transport_failure_never_retries_edge(self) -> None:
        ws = ScriptedProvider(
            [
                VoiceStudioWebSocketError("connect_failed"),
                VoiceStudioWebSocketError("connect_failed"),
            ]
        )
        edge = FakeEdgeProvider(blocks=1, milliseconds=50)
        session, _worker, _router, _backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=edge,
        )
        session.push_text_chunk("第一句。第二句。")
        with self.assertRaises(Exception):
            session.finish()
        # The first sentence was re-rendered once; the second one stopped the
        # reply without a second Edge attempt and without a third provider.
        self.assertEqual(len(edge.calls), 1)
        self.assertEqual(session.edge_attempts, 1)
        self.assertEqual(session.edge_failures, 0)
        self.assertEqual(session.played_segments, ("第一句。",))
        self.assertEqual(len(ws.calls), 2)


class EdgeFallbackFailureTests(EdgeFallbackTestCase):
    def test_edge_failure_stops_the_remaining_speech_and_reports_text(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        http = ChunkScriptProvider()
        edge = FakeEdgeProvider(error=EdgeSapiError("sapi_timeout"))
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, backend = self.make_session(
            ws=ws, http=http, fallback=fallback, edge_provider=edge
        )
        session.push_text_chunk("第一句。第二句。")
        with self.assertRaises(Exception):
            session.finish()

        self.assertEqual(len(edge.calls), 1)
        self.assertEqual(session.edge_attempts, 1)
        self.assertEqual(session.edge_failures, 1)
        self.assertIn("edge_failed@generation1", session.late_events)
        self.assertNotIn("edge_used@generation1", session.late_events)
        self.assertEqual(session.played_segments, ())
        self.assertEqual(session.pending_sentences, 0)
        self.assertEqual(backend.submitted, [])
        self.assertEqual(len(ws.calls), 1)
        self.assertEqual(http.calls, [])
        decision = session.routing_decision
        self.assertIsNotNone(decision)
        self.assertIs(decision.route, TTSRoute.TEXT)
        self.assertIs(decision.action, TTSRoutingAction.CONTINUE_TEXT_NO_REPLAY)
        self.assertTrue(decision.drop_remaining_segments)
        self.assertFalse(decision.replay_current_segment)

    def test_unexpected_edge_error_is_treated_as_an_edge_failure(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(error=RuntimeError("秘密文本不应外泄"))
        session, _worker, _router, _backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=edge,
        )
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertEqual(session.edge_failures, 1)
        self.assertNotIn("秘密文本不应外泄", " ".join(session.late_events))

    def test_route_rejection_from_the_edge_router_is_not_fatal_by_itself(self) -> None:
        # A second Edge failure for the same segment is rejected by the router
        # contract; the session must keep its fail-closed text outcome instead of
        # propagating the routing error.
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(error=EdgeSapiError("sapi_exit_1"))
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, _backend = self.make_session(
            ws=ws, fallback=fallback, edge_provider=edge
        )
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertEqual(session.edge_failures, 1)
        self.assertEqual(session.edge_attempts, 1)

    def test_facts_are_not_rewritten_by_an_edge_failure(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        session, _worker, _router, _backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=FakeEdgeProvider(error=EdgeSapiError("sapi_exit_1")),
        )
        session.push_text_chunk("第一句。")
        with self.assertRaises(Exception):
            session.finish()
        self.assertEqual(session.failure_states, {"segment-1": ProviderRequestPlaybackState.NOT_COMMITTED})
        self.assertEqual(session.generation, 1)
        self.assertTrue(session.verified)
        self.assertFalse(session.overloaded)
        self.assertEqual(session.dropped_pushes, 0)


class EdgeFallbackCancellationTests(EdgeFallbackTestCase):
    def test_late_edge_chunks_after_cancel_never_reach_playback(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(blocks=2, milliseconds=100, cancel_after_first_block=True)
        session, _worker, _router, backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=edge,
        )
        edge.session = session
        session.push_text_chunk("第一句。")
        played = session.finish()

        self.assertTrue(session.cancelled)
        self.assertEqual(played, ())
        self.assertEqual(session.played_segments, ())
        self.assertTrue(
            any(event.startswith("pcm_chunk@") for event in session.late_events),
            session.late_events,
        )
        # At most the pre-cancel block was submitted; the late block never was.
        self.assertLessEqual(sum(len(block) for block in backend.submitted), len(pcm_bytes(100)))
        self.assertEqual(session.timing_samples, ())

    def test_cancel_before_the_edge_attempt_skips_it(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(blocks=1, milliseconds=50)
        session, _worker, _router, _backend = self.make_session(
            ws=ws,
            fallback=TTSFallbackRouter(edge_voice_profile_id="edge-voice-1"),
            edge_provider=edge,
        )
        session.cancel()
        session.push_text_chunk("第一句。")
        self.assertEqual(session.finish(), ())
        self.assertEqual(edge.calls, [])
        self.assertEqual(session.edge_attempts, 0)


class EdgeFallbackFactTests(EdgeFallbackTestCase):
    def test_successful_edge_fallback_keeps_every_existing_fact(self) -> None:
        ws = ScriptedProvider([VoiceStudioWebSocketError("connect_failed")])
        edge = FakeEdgeProvider(blocks=2, milliseconds=100)
        fallback = TTSFallbackRouter(edge_voice_profile_id="edge-voice-1")
        session, _worker, _router, _backend = self.make_session(
            ws=ws, fallback=fallback, edge_provider=edge
        )
        session.push_text_chunk("第一句。")
        session.finish()

        # The VoiceStudio failure remains the fact for this segment; the Edge
        # re-render adds no new failure state and rewrites no counter.
        self.assertEqual(session.failure_states, {"segment-1": ProviderRequestPlaybackState.NOT_COMMITTED})
        self.assertEqual(session.generation, 1)
        self.assertEqual(session.dropped_pushes, 0)
        self.assertFalse(session.overloaded)
        self.assertFalse(session.finish_timed_out)
        self.assertFalse(session.cancelled)
        self.assertTrue(session.finished)
        self.assertTrue(session.verified)
        # The reply already finished, so the router holds no active route; its
        # next-reply route is the Edge route the fallback selected.
        self.assertIsNone(fallback.snapshot.active_reply_id)
        self.assertIsNone(fallback.snapshot.active_route)
        self.assertEqual(fallback.snapshot.next_reply_route, TTSRoute.EDGE)
        self.assertNotIn("第一句。", " ".join(session.late_events))
        sample = session.timing_samples[0]
        self.assertEqual((sample.segment_index, sample.generation), (1, 1))
        self.assertIsNotNone(sample.submit_to_first_pcm_ms)
        self.assertIsNotNone(sample.first_pcm_to_device_ms)


if __name__ == "__main__":
    unittest.main()
