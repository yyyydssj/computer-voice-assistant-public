from __future__ import annotations

import unittest
from threading import Event

from app.tts.providers import (
    PCMChunk,
    VoiceStudioHttpWavError,
    VoiceStudioSentenceRequest,
    VoiceStudioWebSocketError,
)
from app.tts.transport import (
    TransportFailureStage,
    VoiceStudioTransport,
    VoiceStudioTransportError,
    VoiceStudioTransportRouter,
)


def make_request(**overrides: object) -> VoiceStudioSentenceRequest:
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


def make_chunk(request: VoiceStudioSentenceRequest, sequence: int, *, final: bool,
               data: bytes = b"\x01\x00" * 4) -> PCMChunk:
    return PCMChunk(
        reply_id=request.reply_id,
        segment_id=request.segment_id,
        provider_request_id=request.provider_request_id,
        provider_generation=request.provider_generation,
        sequence=sequence,
        sample_rate=24_000,
        channels=1,
        sample_width=2,
        data=data,
        is_final=final,
    )


class _ScriptedProvider:
    """One queued behaviour per call: an exception, or a list of chunks."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        if not self.script:
            raise AssertionError("no scripted behaviour left for this provider")
        behaviour = self.script.pop(0)
        if isinstance(behaviour, BaseException):
            raise behaviour
        for item in behaviour:  # type: ignore[union-attr]
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self) -> None:
        self.close_calls += 1


class ScriptedWsProvider(_ScriptedProvider):
    pass


class ScriptedHttpProvider(_ScriptedProvider):
    pass


class TransportRouterTestCase(unittest.TestCase):
    def make_router(self, *, mode: str, ws_script: list[object] | None = None,
                    http_script: list[object] | None = None, **overrides: object):
        ws = ScriptedWsProvider(ws_script if ws_script is not None else [])
        http = ScriptedHttpProvider(http_script if http_script is not None else [])
        router = VoiceStudioTransportRouter(
            mode=mode, ws_provider=ws, http_provider=http, **overrides  # type: ignore[arg-type]
        )
        self.addCleanup(router.close)
        return router, ws, http

    def collect(self, router: VoiceStudioTransportRouter, request: VoiceStudioSentenceRequest,
                cancelled: Event | None = None) -> list[PCMChunk]:
        return list(router.stream_sentence(request, cancelled=cancelled or Event()))

    def assert_error(self, code: str, callable_: object) -> VoiceStudioTransportError:
        with self.assertRaises(VoiceStudioTransportError) as context:
            callable_()  # type: ignore[operator]
        self.assertEqual(context.exception.code, code)
        return context.exception


class VoiceStudioTransportConfigTests(TransportRouterTestCase):
    def test_unsupported_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            VoiceStudioTransportRouter(mode="legacy", ws_provider=object(), http_provider=object())
        self.assertEqual(str(context.exception), "unsupported_transport_mode")

    def test_providers_and_limits_are_validated(self) -> None:
        with self.assertRaises(TypeError):
            VoiceStudioTransportRouter(mode="auto", ws_provider=object())  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            VoiceStudioTransportRouter(
                mode="auto", ws_provider=ScriptedWsProvider([]),
                http_provider=ScriptedHttpProvider([]), max_http_fallbacks_per_sentence=-1,
            )


class VoiceStudioTransportSelectionTests(TransportRouterTestCase):
    def test_http_wav_mode_never_touches_websocket(self) -> None:
        request = make_request()
        router, ws, http = self.make_router(
            mode="http_wav",
            ws_script=[VoiceStudioWebSocketError("unexpected_ws_call")],
            http_script=[[make_chunk(request, 1, final=True)]],
        )
        chunks = self.collect(router, request)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(ws.calls, [])
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(router.snapshot.last_transport, VoiceStudioTransport.HTTP_WAV.value)

    def test_ws_pcm_mode_never_touches_http_and_does_not_fall_back(self) -> None:
        request = make_request()
        router, ws, http = self.make_router(
            mode="ws_pcm",
            ws_script=[VoiceStudioWebSocketError("connect_failed")],
            http_script=[[make_chunk(request, 1, final=True)]],
        )
        error = self.assert_error("connect_failed", lambda: self.collect(router, request))
        self.assertEqual(error.stage, TransportFailureStage.CONNECT.value)
        self.assertEqual(error.transport, VoiceStudioTransport.WS_PCM.value)
        self.assertEqual(len(ws.calls), 1)
        self.assertEqual(http.calls, [])

    def test_auto_uses_websocket_when_it_succeeds(self) -> None:
        request = make_request()
        chunk = make_chunk(request, 1, final=True)
        router, _ws, http = self.make_router(mode="auto", ws_script=[[chunk]])
        chunks = self.collect(router, request)
        self.assertEqual(chunks, [chunk])
        self.assertEqual(http.calls, [])
        self.assertEqual(router.snapshot.last_transport, VoiceStudioTransport.WS_PCM.value)


class VoiceStudioTransportFallbackTests(TransportRouterTestCase):
    def test_connect_failure_before_audio_falls_back_to_http_once(self) -> None:
        request = make_request()
        http_chunk = make_chunk(request, 1, final=True)
        router, ws, http = self.make_router(
            mode="auto",
            ws_script=[VoiceStudioWebSocketError("connect_failed")],
            http_script=[[http_chunk]],
        )
        chunks = self.collect(router, request)
        self.assertEqual(chunks, [http_chunk])
        self.assertEqual(len(ws.calls), 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(router.snapshot.fallbacks, 1)
        record = router.fallbacks[0]
        self.assertEqual(record.from_transport, VoiceStudioTransport.WS_PCM.value)
        self.assertEqual(record.to_transport, VoiceStudioTransport.HTTP_WAV.value)
        self.assertEqual(record.stage, TransportFailureStage.CONNECT.value)
        self.assertEqual(record.reason, "connect_failed")
        self.assertGreaterEqual(record.latency_ms, 0.0)
        self.assertEqual(record.segment_id, request.segment_id)

    def test_protocol_and_timeout_failures_before_audio_also_fall_back(self) -> None:
        cases = {
            "start_format_invalid": TransportFailureStage.PROTOCOL.value,
            "message_type_invalid": TransportFailureStage.PROTOCOL.value,
            "audio_before_start": TransportFailureStage.PROTOCOL.value,
            "frame_idle_timeout": TransportFailureStage.TIMEOUT_BEFORE_AUDIO.value,
            "sentence_timeout": TransportFailureStage.TIMEOUT_BEFORE_AUDIO.value,
        }
        for code, expected_stage in cases.items():
            with self.subTest(code=code):
                request = make_request()
                router, _ws, http = self.make_router(
                    mode="auto",
                    ws_script=[VoiceStudioWebSocketError(code)],
                    http_script=[[make_chunk(request, 1, final=True)]],
                )
                self.assertEqual(len(self.collect(router, request)), 1)
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(router.fallbacks[0].stage, expected_stage)

    def test_failure_after_audio_never_falls_back_or_replays(self) -> None:
        request = make_request()
        first = make_chunk(request, 1, final=False)
        router, _ws, http = self.make_router(
            mode="auto",
            ws_script=[[first, VoiceStudioWebSocketError("server_error")]],
            http_script=[[make_chunk(request, 1, final=True)]],
        )
        stream = router.stream_sentence(request, cancelled=Event())
        self.assertEqual(next(stream), first)
        with self.assertRaises(VoiceStudioTransportError) as context:
            next(stream)
        self.assertEqual(context.exception.code, "server_error")
        self.assertEqual(context.exception.stage, TransportFailureStage.AFTER_AUDIO.value)
        self.assertEqual(http.calls, [])
        self.assertEqual(router.snapshot.fallbacks, 0)

    def test_http_fallback_failure_is_reported_and_not_retried(self) -> None:
        request = make_request()
        router, ws, http = self.make_router(
            mode="auto",
            ws_script=[VoiceStudioWebSocketError("connect_failed")],
            http_script=[VoiceStudioHttpWavError("http_status_503")],
        )
        error = self.assert_error("http_status_503", lambda: self.collect(router, request))
        self.assertEqual(error.transport, VoiceStudioTransport.HTTP_WAV.value)
        self.assertEqual(error.stage, TransportFailureStage.HTTP.value)
        self.assertEqual(error.fallback_from, VoiceStudioTransport.WS_PCM.value)
        self.assertEqual(len(ws.calls), 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(router.snapshot.fallbacks, 1)

    def test_zero_fallback_budget_keeps_the_websocket_failure(self) -> None:
        request = make_request()
        router, _ws, http = self.make_router(
            mode="auto",
            ws_script=[VoiceStudioWebSocketError("connect_failed")],
            http_script=[[make_chunk(request, 1, final=True)]],
            max_http_fallbacks_per_sentence=0,
        )
        error = self.assert_error("connect_failed", lambda: self.collect(router, request))
        self.assertEqual(error.stage, TransportFailureStage.CONNECT.value)
        self.assertEqual(http.calls, [])
        self.assertEqual(router.snapshot.fallbacks, 0)

    def test_each_sentence_retries_the_websocket_first(self) -> None:
        first_request = make_request()
        second_request = make_request(segment_id="segment-2", provider_request_id="request-2")
        router, ws, http = self.make_router(
            mode="auto",
            ws_script=[
                VoiceStudioWebSocketError("connect_failed"),
                [make_chunk(second_request, 1, final=True)],
            ],
            http_script=[[make_chunk(first_request, 1, final=True)]],
        )
        self.assertEqual(len(self.collect(router, first_request)), 1)
        self.assertEqual(len(self.collect(router, second_request)), 1)
        self.assertEqual(len(ws.calls), 2)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(router.snapshot.ws_attempts, 2)
        self.assertEqual(router.snapshot.http_attempts, 1)


class VoiceStudioTransportCancellationTests(TransportRouterTestCase):
    def test_cancel_before_request_is_not_a_fallback(self) -> None:
        request = make_request()
        router, _ws, http = self.make_router(
            mode="auto",
            ws_script=[VoiceStudioWebSocketError("cancelled_before_request")],
            http_script=[[make_chunk(request, 1, final=True)]],
        )
        error = self.assert_error(
            "cancelled_before_request", lambda: self.collect(router, request)
        )
        self.assertEqual(error.stage, TransportFailureStage.CANCELLED.value)
        self.assertEqual(http.calls, [])
        self.assertEqual(router.snapshot.fallbacks, 0)

    def test_cancel_during_stream_after_audio_is_not_a_fallback(self) -> None:
        request = make_request()
        router, _ws, http = self.make_router(
            mode="auto",
            ws_script=[[make_chunk(request, 1, final=False),
                        VoiceStudioWebSocketError("cancelled_during_stream")]],
            http_script=[[make_chunk(request, 1, final=True)]],
        )
        stream = router.stream_sentence(request, cancelled=Event())
        next(stream)
        with self.assertRaises(VoiceStudioTransportError) as context:
            next(stream)
        self.assertEqual(context.exception.stage, TransportFailureStage.CANCELLED.value)
        self.assertEqual(http.calls, [])

    def test_pre_cancelled_event_short_circuits_before_any_transport(self) -> None:
        request = make_request()
        router, ws, http = self.make_router(
            mode="auto", ws_script=[[make_chunk(request, 1, final=True)]]
        )
        cancelled = Event()
        cancelled.set()
        self.assert_error(
            "cancelled_before_request", lambda: self.collect(router, request, cancelled)
        )
        self.assertEqual(ws.calls, [])
        self.assertEqual(http.calls, [])


class VoiceStudioTransportLifecycleTests(TransportRouterTestCase):
    def test_snapshot_reports_attempts_and_last_failure(self) -> None:
        request = make_request()
        router, _ws, _http = self.make_router(
            mode="auto",
            ws_script=[VoiceStudioWebSocketError("start_sample_rate_invalid")],
            http_script=[[make_chunk(request, 1, final=True)]],
        )
        self.collect(router, request)
        snapshot = router.snapshot
        self.assertEqual(snapshot.mode, "auto")
        self.assertEqual(snapshot.ws_attempts, 1)
        self.assertEqual(snapshot.http_attempts, 1)
        self.assertEqual(snapshot.fallbacks, 1)
        self.assertEqual(snapshot.last_failure_code, "start_sample_rate_invalid")
        self.assertEqual(snapshot.last_failure_stage, TransportFailureStage.PROTOCOL.value)
        self.assertEqual(snapshot.last_transport, VoiceStudioTransport.HTTP_WAV.value)

    def test_close_is_idempotent_and_closes_both_providers(self) -> None:
        router, ws, http = self.make_router(mode="auto")
        router.close()
        router.close()
        self.assertEqual(ws.close_calls, 1)
        self.assertEqual(http.close_calls, 1)

    def test_error_messages_never_leak_provider_detail(self) -> None:
        request = make_request()
        router, _ws, http = self.make_router(
            mode="auto",
            ws_script=[VoiceStudioWebSocketError("connect_failed")],
            http_script=[VoiceStudioHttpWavError("http_status_503")],
        )
        with self.assertRaises(VoiceStudioTransportError) as context:
            self.collect(router, request)
        self.assertEqual(str(context.exception), "http_status_503")
        self.assertEqual(len(http.calls), 1)

    def test_argument_types_are_validated(self) -> None:
        router, _ws, _http = self.make_router(mode="auto")
        with self.assertRaises(TypeError):
            router.stream_sentence("not-a-request", cancelled=Event())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            router.stream_sentence(make_request(), cancelled="nope")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
