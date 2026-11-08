from __future__ import annotations

import json
import threading
import time
import unittest
from threading import Event

from app.tts.providers import (
    VoiceStudioSentenceRequest,
    VoiceStudioWebSocketConfig,
    VoiceStudioWebSocketError,
    VoiceStudioWebSocketProvider,
)
from app.tts.providers.voicestudio_ws import VoiceStudioWebSocketTransportError


def start_message(**overrides: object) -> str:
    payload: dict[str, object] = {
        "type": "start",
        "sample_rate": 24000,
        "channels": 1,
        "format": "pcm16",
        "engine": "omnivoice",
    }
    payload.update(overrides)
    return json.dumps(payload)


def done_message(samples: int = 8, **overrides: object) -> str:
    payload: dict[str, object] = {
        "type": "done",
        "duration_s": 0.2,
        "gen_time_s": 0.1,
        "ttfa_ms": 120.0,
        "rtf": 0.2,
        "samples": samples,
        "sample_rate": 24000,
        "engine": "omnivoice",
    }
    payload.update(overrides)
    return json.dumps(payload)


def pcm(samples: int = 4) -> bytes:
    return b"\x01\x00" * samples


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedConnection:
    """Deterministic WebSocket stand-in; never opens a socket."""

    def __init__(self, script: list[object], clock: FakeClock | None = None) -> None:
        self.script = list(script)
        self.clock = clock
        self.sent: list[str] = []
        self.recv_timeouts: list[float] = []
        self.closed = False

    def send(self, message: str) -> None:
        if self.closed:
            raise VoiceStudioWebSocketTransportError("connection_closed")
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str | bytes:
        if self.closed:
            raise VoiceStudioWebSocketTransportError("connection_closed")
        self.recv_timeouts.append(timeout)  # type: ignore[arg-type]
        while True:
            if not self.script:
                raise VoiceStudioWebSocketTransportError("connection_closed")
            item = self.script.pop(0)
            if isinstance(item, tuple) and item and item[0] == "advance":
                if self.clock is None:
                    raise AssertionError("script 需要 clock 才能推进时间")
                self.clock.advance(float(item[1]))
                continue
            if isinstance(item, Exception):
                raise item
            if isinstance(item, (str, bytes)):
                return item
            raise AssertionError(f"不支持的 script 元素: {item!r}")

    def close(self) -> None:
        self.closed = True


class ScriptedFactory:
    """Connection factory that hands out pre-scripted connections in order."""

    def __init__(self, scripts: list[list[object]], clock: FakeClock | None = None) -> None:
        self._scripts = list(scripts)
        self._clock = clock
        self.calls: list[tuple[str, float, int]] = []
        self.connections: list[ScriptedConnection] = []

    def __call__(self, url: str, open_timeout: float, max_size: int) -> ScriptedConnection:
        self.calls.append((url, open_timeout, max_size))
        if not self._scripts:
            raise AssertionError("没有更多预置连接")
        script = self._scripts.pop(0)
        if script and isinstance(script[0], Exception):
            raise script[0]
        connection = ScriptedConnection(script, clock=self._clock)
        self.connections.append(connection)
        return connection


class VoiceStudioWebSocketTestCase(unittest.TestCase):
    def make_request(self, **overrides: object) -> VoiceStudioSentenceRequest:
        values: dict[str, object] = {
            "reply_id": "reply-1",
            "segment_id": "segment-1",
            "provider_request_id": "request-1",
            "provider_generation": 1,
            "text": "这是一句完整的话。",
            "profile_id": "profile-1",
            "language": "zh",
            "speed": 0.8,
        }
        values.update(overrides)
        return VoiceStudioSentenceRequest(**values)  # type: ignore[arg-type]

    def make_provider(
        self,
        scripts: list[list[object]],
        *,
        clock: FakeClock | None = None,
        config: VoiceStudioWebSocketConfig | None = None,
    ) -> tuple[VoiceStudioWebSocketProvider, ScriptedFactory]:
        factory = ScriptedFactory(scripts, clock=clock)
        provider = VoiceStudioWebSocketProvider(
            config or VoiceStudioWebSocketConfig(),
            connection_factory=factory,
            clock=clock or FakeClock(),
        )
        return provider, factory

    def collect(
        self,
        provider: VoiceStudioWebSocketProvider,
        request: VoiceStudioSentenceRequest,
        cancelled: Event | None = None,
    ) -> list[object]:
        return list(provider.stream_sentence(request, cancelled=cancelled or Event()))

    def assert_code(self, code: str, callable_: object) -> VoiceStudioWebSocketError:
        with self.assertRaises(VoiceStudioWebSocketError) as context:
            callable_()  # type: ignore[operator]
        self.assertEqual(context.exception.code, code)
        return context.exception


class VoiceStudioWebSocketConfigTests(unittest.TestCase):
    def test_default_config_targets_loopback_ws_tts(self) -> None:
        config = VoiceStudioWebSocketConfig()
        self.assertEqual(config.url, "ws://127.0.0.1:3900/ws/tts")
        self.assertEqual(config.connect_timeout_seconds, 2.0)
        self.assertEqual(config.sentence_timeout_seconds, 18.0)
        self.assertEqual(config.frame_idle_timeout_seconds, 3.0)
        self.assertEqual(config.session_idle_seconds, 30.0)
        self.assertEqual(config.max_frame_bytes, 16 * 1024 * 1024)
        self.assertEqual(config.max_total_audio_bytes, 16 * 1024 * 1024)

    def test_remote_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            VoiceStudioWebSocketConfig(endpoint="ws://example.com:3900")
        self.assertEqual(str(context.exception), "voicestudio_endpoint_loopback_required")

    def test_credentials_query_and_fragment_are_rejected(self) -> None:
        for endpoint in (
            "ws://user:pass@127.0.0.1:3900",
            "ws://127.0.0.1:3900?token=1",
            "ws://127.0.0.1:3900#frag",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError) as context:
                    VoiceStudioWebSocketConfig(endpoint=endpoint)
                self.assertEqual(str(context.exception), "voicestudio_endpoint_unsafe_url")

    def test_non_websocket_scheme_and_embedded_path_are_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            VoiceStudioWebSocketConfig(endpoint="http://127.0.0.1:3900")
        self.assertEqual(str(context.exception), "voicestudio_endpoint_must_be_ws_url")
        with self.assertRaises(ValueError) as context:
            VoiceStudioWebSocketConfig(endpoint="ws://127.0.0.1:3900/ws/tts")
        self.assertEqual(str(context.exception), "voicestudio_endpoint_must_not_include_path")

    def test_invalid_port_and_non_positive_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError) as context:
            VoiceStudioWebSocketConfig(endpoint="ws://127.0.0.1:0")
        self.assertEqual(str(context.exception), "voicestudio_endpoint_invalid_port")
        with self.assertRaises(ValueError):
            VoiceStudioWebSocketConfig(connect_timeout_seconds=0)
        with self.assertRaises(ValueError):
            VoiceStudioWebSocketConfig(sentence_timeout_seconds=-1)
        with self.assertRaises(ValueError):
            VoiceStudioWebSocketConfig(frame_idle_timeout_seconds=0)
        with self.assertRaises(ValueError):
            VoiceStudioWebSocketConfig(session_idle_seconds=0)
        with self.assertRaises(ValueError):
            VoiceStudioWebSocketConfig(max_frame_bytes=0)
        with self.assertRaises(ValueError):
            VoiceStudioWebSocketConfig(max_total_audio_bytes=0)

    def test_ipv6_loopback_and_path_override_are_supported(self) -> None:
        config = VoiceStudioWebSocketConfig(endpoint="ws://[::1]:3900", path="/ws/tts")
        self.assertEqual(config.url, "ws://[::1]:3900/ws/tts")
        with self.assertRaises(ValueError) as context:
            VoiceStudioWebSocketConfig(path="ws/tts")
        self.assertEqual(str(context.exception), "path_must_be_absolute_without_query")


class VoiceStudioWebSocketProtocolTests(VoiceStudioWebSocketTestCase):
    def test_sentence_streams_chunks_and_marks_last_chunk_final(self) -> None:
        clock = FakeClock()
        provider, factory = self.make_provider(
            [[start_message(), pcm(), pcm(), done_message(8)]], clock=clock
        )
        request = self.make_request()
        chunks = self.collect(provider, request)
        self.assertEqual(len(chunks), 2)
        first, last = chunks
        for chunk in chunks:
            self.assertEqual(chunk.reply_id, request.reply_id)
            self.assertEqual(chunk.segment_id, request.segment_id)
            self.assertEqual(chunk.provider_request_id, request.provider_request_id)
            self.assertEqual(chunk.provider_generation, request.provider_generation)
            self.assertEqual((chunk.sample_rate, chunk.channels, chunk.sample_width), (24000, 1, 2))
        self.assertEqual(first.sequence, 1)
        self.assertFalse(first.is_final)
        self.assertEqual(last.sequence, 2)
        self.assertTrue(last.is_final)
        self.assertEqual(factory.calls[0][0], "ws://127.0.0.1:3900/ws/tts")
        sent = json.loads(factory.connections[0].sent[0])
        self.assertEqual(
            sent,
            {
                "text": "这是一句完整的话。",
                "voice": "profile-1",
                "language": "zh",
                "speed": 0.8,
            },
        )

    def test_engine_is_forwarded_when_selected(self) -> None:
        provider, factory = self.make_provider([[start_message(), pcm(), done_message(4)]])
        self.collect(provider, self.make_request(engine="omnivoice"))
        self.assertEqual(json.loads(factory.connections[0].sent[0])["engine"], "omnivoice")

    def test_routing_notice_before_start_is_tolerated(self) -> None:
        routing = json.dumps({"type": "routing", "status": "local_stream", "reason": "local"})
        provider, _ = self.make_provider([[routing, start_message(), pcm(), done_message(4)]])
        chunks = self.collect(provider, self.make_request())
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].is_final)

    def test_audio_before_start_is_rejected(self) -> None:
        provider, _ = self.make_provider([[pcm(), start_message(), done_message(4)]])
        self.assert_code(
            "audio_before_start", lambda: self.collect(provider, self.make_request())
        )

    def test_invalid_start_metadata_is_rejected(self) -> None:
        cases = {
            "start_format_invalid": start_message(format="float32"),
            "start_sample_rate_invalid": start_message(sample_rate=16000),
            "start_channels_invalid": start_message(channels=2),
        }
        for code, message in cases.items():
            with self.subTest(code=code):
                provider, _ = self.make_provider([[message, pcm(), done_message(4)]])
                self.assert_code(code, lambda: self.collect(provider, self.make_request()))

    def test_duplicate_start_is_rejected(self) -> None:
        provider, _ = self.make_provider([[start_message(), start_message(), pcm(), done_message(4)]])
        self.assert_code("duplicate_start", lambda: self.collect(provider, self.make_request()))

    def test_unknown_message_type_and_non_json_text_are_rejected(self) -> None:
        unknown = json.dumps({"type": "progress", "value": 1})
        provider, _ = self.make_provider([[unknown, start_message(), pcm(), done_message(4)]])
        self.assert_code("message_type_invalid", lambda: self.collect(provider, self.make_request()))
        provider, _ = self.make_provider([["not-json", start_message(), pcm(), done_message(4)]])
        self.assert_code("message_not_json", lambda: self.collect(provider, self.make_request()))

    def test_done_sample_count_must_match_received_pcm(self) -> None:
        provider, _ = self.make_provider([[start_message(), pcm(), pcm(), done_message(7)]])
        self.assert_code("samples_mismatch", lambda: self.collect(provider, self.make_request()))
        provider, _ = self.make_provider(
            [[start_message(), pcm(), done_message(samples=None)]]
        )
        self.assert_code("done_samples_missing", lambda: self.collect(provider, self.make_request()))

    def test_error_frame_fails_closed_and_stops_audio(self) -> None:
        error = json.dumps({"type": "error", "detail": "engine cannot run on this host"})
        provider, factory = self.make_provider([[start_message(), error, pcm(), done_message(4)]])
        exception = self.assert_code(
            "server_error", lambda: self.collect(provider, self.make_request())
        )
        self.assertNotIn("engine cannot run", str(exception))
        self.assertTrue(factory.connections[0].closed)

    def test_done_before_start_and_empty_audio_are_rejected(self) -> None:
        provider, _ = self.make_provider([[done_message(0)]])
        self.assert_code("done_before_start", lambda: self.collect(provider, self.make_request()))
        provider, _ = self.make_provider([[start_message(), done_message(0)]])
        self.assert_code("audio_empty", lambda: self.collect(provider, self.make_request()))

    def test_malformed_or_oversized_audio_frames_are_rejected(self) -> None:
        provider, _ = self.make_provider([[start_message(), b"\x01", done_message(1)]])
        self.assert_code("audio_frame_invalid", lambda: self.collect(provider, self.make_request()))
        provider, _ = self.make_provider([[start_message(), b"", done_message(0)]])
        self.assert_code("audio_frame_empty", lambda: self.collect(provider, self.make_request()))

    def test_frame_and_total_audio_limits_are_enforced(self) -> None:
        config = VoiceStudioWebSocketConfig(max_frame_bytes=8, max_total_audio_bytes=16)
        provider, _ = self.make_provider(
            [[start_message(), pcm(8), done_message(8)]], config=config
        )
        self.assert_code("frame_too_large", lambda: self.collect(provider, self.make_request()))
        config = VoiceStudioWebSocketConfig(max_frame_bytes=64, max_total_audio_bytes=8)
        provider, _ = self.make_provider(
            [[start_message(), pcm(4), pcm(4), done_message(8)]], config=config
        )
        self.assert_code("audio_too_large", lambda: self.collect(provider, self.make_request()))

    def test_connection_closed_before_done_is_rejected(self) -> None:
        provider, factory = self.make_provider(
            [[start_message(), pcm(), VoiceStudioWebSocketTransportError("connection_closed")]]
        )
        self.assert_code(
            "connection_closed_before_done", lambda: self.collect(provider, self.make_request())
        )
        self.assertTrue(factory.connections[0].closed)

    def test_connect_failure_is_reported_without_leaking_transport_detail(self) -> None:
        provider, _ = self.make_provider([[OSError("connect refused")]])
        exception = self.assert_code(
            "connect_failed", lambda: self.collect(provider, self.make_request())
        )
        self.assertNotIn("connect refused", str(exception))


class VoiceStudioWebSocketTimeoutTests(VoiceStudioWebSocketTestCase):
    def test_idle_frame_timeout_is_reported(self) -> None:
        config = VoiceStudioWebSocketConfig(
            frame_idle_timeout_seconds=1.0, sentence_timeout_seconds=10.0
        )
        provider, _ = self.make_provider([[start_message(), TimeoutError()]], config=config)
        self.assert_code("frame_idle_timeout", lambda: self.collect(provider, self.make_request()))

    def test_sentence_budget_exhaustion_is_reported(self) -> None:
        clock = FakeClock()
        config = VoiceStudioWebSocketConfig(
            frame_idle_timeout_seconds=5.0, sentence_timeout_seconds=6.0
        )
        provider, _ = self.make_provider(
            [[start_message(), ("advance", 10.0), TimeoutError()]], clock=clock, config=config
        )
        self.assert_code("sentence_timeout", lambda: self.collect(provider, self.make_request()))

    def test_recv_timeout_never_exceeds_configured_budget(self) -> None:
        config = VoiceStudioWebSocketConfig(
            frame_idle_timeout_seconds=3.0, sentence_timeout_seconds=4.0
        )
        provider, factory = self.make_provider(
            [[start_message(), pcm(), done_message(4)]], config=config
        )
        self.collect(provider, self.make_request())
        self.assertTrue(all(timeout <= 4.0 for timeout in factory.connections[0].recv_timeouts))

    def test_pre_start_wait_uses_sentence_budget_not_frame_idle(self) -> None:
        # VoiceStudio emits `start` only after the first sentence is fully
        # synthesized, so the pre-start wait must not be cut short by the
        # frame-idle bound.
        config = VoiceStudioWebSocketConfig(
            frame_idle_timeout_seconds=0.5, sentence_timeout_seconds=10.0
        )
        provider, factory = self.make_provider(
            [[start_message(), pcm(), pcm(), done_message(8)]], config=config
        )
        self.collect(provider, self.make_request())
        timeouts = factory.connections[0].recv_timeouts
        self.assertAlmostEqual(timeouts[0], 10.0)
        self.assertTrue(all(timeout <= 0.5 for timeout in timeouts[1:]))

    def test_first_frame_timeout_stays_within_the_sentence_budget(self) -> None:
        clock = FakeClock()
        config = VoiceStudioWebSocketConfig(
            frame_idle_timeout_seconds=1.0, sentence_timeout_seconds=5.0
        )
        provider, _ = self.make_provider(
            [[("advance", 6.0), TimeoutError()]], clock=clock, config=config
        )
        self.assert_code("sentence_timeout", lambda: self.collect(provider, self.make_request()))


class VoiceStudioWebSocketCancellationTests(VoiceStudioWebSocketTestCase):
    def test_cancel_before_request_does_not_touch_the_transport(self) -> None:
        provider, factory = self.make_provider([[start_message(), pcm(), done_message(4)]])
        cancelled = Event()
        cancelled.set()
        self.assert_code(
            "cancelled_before_request",
            lambda: self.collect(provider, self.make_request(), cancelled),
        )
        self.assertEqual(factory.calls, [])

    def test_cancel_during_stream_closes_socket_and_stops_chunks(self) -> None:
        provider, factory = self.make_provider(
            [[start_message(), pcm(), pcm(), pcm(), done_message(12)]]
        )
        cancelled = Event()
        stream = provider.stream_sentence(self.make_request(), cancelled=cancelled)
        self.assertFalse(next(stream).is_final)
        cancelled.set()
        with self.assertRaises(VoiceStudioWebSocketError) as context:
            next(stream)
        self.assertEqual(context.exception.code, "cancelled_during_stream")
        self.assertTrue(factory.connections[0].closed)

    def test_cancel_after_done_keeps_the_completed_stream(self) -> None:
        provider, _ = self.make_provider([[start_message(), pcm(), done_message(4)]])
        cancelled = Event()
        chunks = self.collect(provider, self.make_request(), cancelled)
        cancelled.set()
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].is_final)


class VoiceStudioWebSocketLifecycleTests(VoiceStudioWebSocketTestCase):
    def test_three_sentences_reuse_one_connection_serially(self) -> None:
        # One socket carries all three sentence exchanges in order.
        scripts = [
            [
                start_message(), pcm(), done_message(4),
                start_message(), pcm(), pcm(), done_message(8),
                start_message(), pcm(), done_message(4),
            ]
        ]
        provider, factory = self.make_provider(scripts)
        for index in range(3):
            chunks = self.collect(provider, self.make_request(segment_id=f"segment-{index}"))
            self.assertTrue(chunks[-1].is_final)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(len(factory.connections), 1)
        self.assertEqual(len(factory.connections[0].sent), 3)

    def test_second_in_flight_request_is_rejected(self) -> None:
        provider, _ = self.make_provider(
            [[start_message(), pcm(), pcm(), done_message(8)], [start_message(), pcm(), done_message(4)]]
        )
        running = provider.stream_sentence(self.make_request(), cancelled=Event())
        next(running)
        self.assert_code(
            "concurrent_request_not_allowed",
            lambda: self.collect(provider, self.make_request(segment_id="segment-2")),
        )
        running.close()
        chunks = self.collect(provider, self.make_request(segment_id="segment-3"))
        self.assertTrue(chunks[-1].is_final)

    def test_idle_session_releases_socket_and_next_sentence_reconnects(self) -> None:
        clock = FakeClock()
        scripts = [[start_message(), pcm(), done_message(4)], [start_message(), pcm(), done_message(4)]]
        provider, factory = self.make_provider(scripts, clock=clock)
        self.collect(provider, self.make_request())
        self.assertFalse(provider.maybe_close_idle())
        clock.advance(31.0)
        self.assertTrue(provider.maybe_close_idle())
        self.assertTrue(factory.connections[0].closed)
        self.collect(provider, self.make_request(segment_id="segment-2"))
        self.assertEqual(len(factory.calls), 2)
        self.assertFalse(factory.connections[1].closed)

    def test_idle_gap_is_closed_before_reusing_the_socket(self) -> None:
        clock = FakeClock()
        scripts = [[start_message(), pcm(), done_message(4)], [start_message(), pcm(), done_message(4)]]
        provider, factory = self.make_provider(scripts, clock=clock)
        self.collect(provider, self.make_request())
        clock.advance(45.0)
        self.collect(provider, self.make_request(segment_id="segment-2"))
        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(factory.connections[0].closed)

    def test_protocol_failure_drops_the_socket_for_the_next_sentence(self) -> None:
        scripts = [
            [start_message(format="float32")],
            [start_message(), pcm(), done_message(4)],
        ]
        provider, factory = self.make_provider(scripts)
        self.assert_code("start_format_invalid", lambda: self.collect(provider, self.make_request()))
        self.assertTrue(factory.connections[0].closed)
        chunks = self.collect(provider, self.make_request(segment_id="segment-2"))
        self.assertTrue(chunks[-1].is_final)
        self.assertEqual(len(factory.calls), 2)

    def test_close_is_idempotent_and_blocks_further_requests(self) -> None:
        provider, factory = self.make_provider([[start_message(), pcm(), done_message(4)]])
        self.collect(provider, self.make_request())
        provider.close()
        provider.close()
        self.assertTrue(factory.connections[0].closed)
        self.assert_code("provider_closed", lambda: self.collect(provider, self.make_request()))

    def test_invalid_arguments_are_rejected(self) -> None:
        provider, _ = self.make_provider([[start_message(), pcm(), done_message(4)]])
        with self.assertRaises(TypeError):
            provider.stream_sentence("not-a-request", cancelled=Event())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            provider.stream_sentence(self.make_request(), cancelled="nope")  # type: ignore[arg-type]


class LoopbackVoiceStudioServer:
    """Minimal /ws/tts stand-in served on 127.0.0.1 inside the test process."""

    def __init__(self, behaviour) -> None:  # type: ignore[no-untyped-def]
        self._behaviour = behaviour
        self.connection_count = 0
        self.requests: list[dict[str, object]] = []
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.socket.getsockname()[1])  # type: ignore[union-attr]

    def __enter__(self) -> "LoopbackVoiceStudioServer":
        from websockets.sync.server import serve

        def handler(connection) -> None:  # type: ignore[no-untyped-def]
            self.connection_count += 1
            for message in connection:
                self.requests.append(json.loads(message))
                self._behaviour(connection, len(self.requests))

        self._server = serve(handler, "127.0.0.1", 0)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.1)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)


class VoiceStudioWebSocketLoopbackTests(VoiceStudioWebSocketTestCase):
    @staticmethod
    def happy_behaviour(connection, _index: int) -> None:  # type: ignore[no-untyped-def]
        connection.send(start_message())
        connection.send(pcm())
        connection.send(pcm())
        connection.send(done_message(8))

    def test_loopback_server_reuses_one_connection_for_three_sentences(self) -> None:
        with LoopbackVoiceStudioServer(self.happy_behaviour) as server:
            provider = VoiceStudioWebSocketProvider(
                VoiceStudioWebSocketConfig(endpoint=f"ws://127.0.0.1:{server.port}")
            )
            try:
                for index in range(3):
                    request = self.make_request(
                        segment_id=f"segment-{index}",
                        provider_request_id=f"request-{index}",
                    )
                    chunks = self.collect(provider, request)
                    self.assertEqual(len(chunks), 2)
                    self.assertTrue(chunks[-1].is_final)
                    self.assertEqual(sum(len(chunk.data) for chunk in chunks), 16)
                    self.assertEqual(chunks[0].segment_id, f"segment-{index}")
            finally:
                provider.close()
            self.assertEqual(server.connection_count, 1)
            self.assertEqual(len(server.requests), 3)

    def test_loopback_server_closing_without_done_is_rejected(self) -> None:
        def behaviour(connection, _index: int) -> None:  # type: ignore[no-untyped-def]
            connection.send(start_message())
            connection.send(pcm())
            connection.close()

        with LoopbackVoiceStudioServer(behaviour) as server:
            provider = VoiceStudioWebSocketProvider(
                VoiceStudioWebSocketConfig(endpoint=f"ws://127.0.0.1:{server.port}")
            )
            try:
                self.assert_code(
                    "connection_closed_before_done",
                    lambda: self.collect(provider, self.make_request()),
                )
            finally:
                provider.close()


if __name__ == "__main__":
    unittest.main()
