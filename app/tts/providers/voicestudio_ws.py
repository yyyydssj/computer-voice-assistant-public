"""Offline-testable VoiceStudio ``/ws/tts`` PCM streaming provider.

This adapter owns only the bounded WebSocket transport and protocol boundary for
VoiceStudio v0.5.6 ``/ws/tts``. It never selects voices, re-segments text, starts
playback, retries automatically, or installs itself into the default runtime. One
connection carries exactly one in-flight sentence request and every accepted
audio frame is normalized to the Batch 1 ``PCMChunk`` contract so the future
playback layer stays transport-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import threading
import time
import urllib.parse
from threading import Event
from typing import Any, Callable, Iterator, Protocol

from .contracts import (
    PCMChunk,
    PCMChunkStreamValidator,
    VoiceStudioContractError,
    VoiceStudioSentenceRequest,
)


class VoiceStudioWebSocketError(VoiceStudioContractError):
    """Fail-closed WebSocket transport or protocol violation."""

    def __init__(self, code: str) -> None:
        # The code is both the fixed short code and (historically) the message text;
        # it is passed through the base constructor so the attribute cannot be reset.
        super().__init__(code, code=code)


class VoiceStudioWebSocketTransportError(Exception):
    """Connection-level failure raised by the socket adapter or a test double.

    The short ``code`` is transport detail. Callers only ever see the mapped
    ``VoiceStudioWebSocketError`` code, so socket text and server text never leak
    into provider errors.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WebSocketConnection(Protocol):
    """Minimal synchronous connection seam shared by the real client and fakes."""

    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, float, int], WebSocketConnection]


class _WebSocketsConnection:
    """Adapter translating ``websockets.sync`` behavior into the local seam."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def send(self, message: str) -> None:
        try:
            self._connection.send(message)
        except Exception:  # noqa: BLE001 - transport detail must not escape
            raise VoiceStudioWebSocketTransportError("send_failed") from None

    def recv(self, timeout: float | None = None) -> str | bytes:
        try:
            return self._connection.recv(timeout=timeout)
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001 - transport detail must not escape
            raise VoiceStudioWebSocketTransportError("connection_closed") from None

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


def _default_connection_factory(url: str, open_timeout: float, max_size: int) -> WebSocketConnection:
    """Connect with the pinned ``websockets`` sync client (imported lazily)."""

    from websockets.sync.client import connect

    # ``legacy=True`` keeps a directly managed connection instead of the
    # context-manager form, because one socket is deliberately reused across
    # sentences and closed by the provider's own lifecycle.
    return _WebSocketsConnection(
        connect(
            url,
            open_timeout=open_timeout,
            close_timeout=open_timeout,
            max_size=max_size,
            legacy=True,
        )
    )


_SAMPLE_RATE = 24_000
_ALLOWED_MESSAGE_TYPES = frozenset({"start", "routing", "done", "error"})


def _loopback_ws_endpoint(name: str, value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name}_required")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError(f"{name}_must_be_ws_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name}_unsafe_url")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"{name}_must_not_include_path")
    host = parsed.hostname.rstrip(".").casefold()
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"{name}_loopback_required") from None
        if not address.is_loopback:
            raise ValueError(f"{name}_loopback_required")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name}_invalid_port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name}_invalid_port")
    return value.rstrip("/")


def _path(base: str, path: str) -> str:
    if type(path) is not str or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("path_must_be_absolute_without_query")
    return base.rstrip("/") + path


def _positive_number(name: str, value: object) -> None:
    if type(value) not in {int, float} or isinstance(value, bool) or value <= 0:  # type: ignore[operator]
        raise ValueError(f"{name}_must_be_positive")


@dataclass(frozen=True, slots=True)
class VoiceStudioWebSocketConfig:
    """Loopback-only, strictly bounded configuration for the ``/ws/tts`` client."""

    endpoint: str = "ws://127.0.0.1:3900"
    path: str = "/ws/tts"
    connect_timeout_seconds: float = 2.0
    sentence_timeout_seconds: float = 18.0
    frame_idle_timeout_seconds: float = 3.0
    session_idle_seconds: float = 30.0
    max_frame_bytes: int = 16 * 1024 * 1024
    max_total_audio_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "endpoint", _loopback_ws_endpoint("voicestudio_endpoint", self.endpoint)
        )
        _path(self.endpoint, self.path)
        for name, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("sentence_timeout_seconds", self.sentence_timeout_seconds),
            ("frame_idle_timeout_seconds", self.frame_idle_timeout_seconds),
            ("session_idle_seconds", self.session_idle_seconds),
        ):
            _positive_number(name, value)
        for name, value in (
            ("max_frame_bytes", self.max_frame_bytes),
            ("max_total_audio_bytes", self.max_total_audio_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name}_must_be_positive")

    @property
    def url(self) -> str:
        return _path(self.endpoint, self.path)


class VoiceStudioWebSocketProvider:
    """Synchronous, cancellable, single-in-flight ``/ws/tts`` provider."""

    def __init__(
        self,
        config: VoiceStudioWebSocketConfig | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if config is not None and not isinstance(config, VoiceStudioWebSocketConfig):
            raise TypeError("config 必须是 VoiceStudioWebSocketConfig")
        if connection_factory is not None and not callable(connection_factory):
            raise TypeError("connection_factory 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self.config = config or VoiceStudioWebSocketConfig()
        self._connection_factory = connection_factory or _default_connection_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._connection: WebSocketConnection | None = None
        self._closed = False
        self._in_flight = False
        self._last_activity_at = clock()

    # ------------------------------------------------------------------ public

    def stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        *,
        cancelled: Event,
    ) -> Iterator[PCMChunk]:
        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        if not isinstance(cancelled, Event):
            raise TypeError("cancelled 必须是 threading.Event")
        return self._stream_sentence(request, cancelled)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop_connection()

    def maybe_close_idle(self) -> bool:
        """Release the socket when no sentence used it within the idle window."""

        with self._lock:
            if self._closed or self._in_flight:
                return False
            return self._close_idle_locked()

    # ----------------------------------------------------------------- internal

    def _begin_request(self) -> None:
        with self._lock:
            if self._closed:
                raise VoiceStudioWebSocketError("provider_closed")
            if self._in_flight:
                raise VoiceStudioWebSocketError("concurrent_request_not_allowed")
            self._in_flight = True
            self._close_idle_locked()

    def _end_request(self) -> None:
        with self._lock:
            self._in_flight = False
        self._last_activity_at = self._clock()

    def _close_idle_locked(self) -> bool:
        if self._connection is None:
            return False
        if self._clock() - self._last_activity_at < self.config.session_idle_seconds:
            return False
        self._drop_connection()
        return True

    def _drop_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    def _fail(self, code: str) -> VoiceStudioWebSocketError:
        self._drop_connection()
        return VoiceStudioWebSocketError(code)

    def _ensure_connection(self) -> WebSocketConnection:
        connection = self._connection
        if connection is not None:
            return connection
        try:
            connection = self._connection_factory(
                self.config.url,
                self.config.connect_timeout_seconds,
                self.config.max_frame_bytes,
            )
        except VoiceStudioWebSocketError:
            raise
        except Exception:  # noqa: BLE001 - connect detail must not escape
            raise VoiceStudioWebSocketError("connect_failed") from None
        if connection is None:
            raise VoiceStudioWebSocketError("connect_failed")
        self._connection = connection
        return connection

    def _recv_timeout(self, deadline: float, *, audio_started: bool) -> float:
        """Bound the next recv wait.

        VoiceStudio sends ``start`` only after the first sentence has been fully
        synthesized, so the wait before ``start`` is covered by the sentence
        budget. The frame-idle bound only applies between frames of a stream that
        has already started, where a half-open socket must not block forever.
        """

        remaining = deadline - self._clock()
        if remaining <= 0:
            raise self._fail("sentence_timeout")
        if not audio_started:
            return remaining
        return min(self.config.frame_idle_timeout_seconds, remaining)

    def _receive(
        self,
        connection: WebSocketConnection,
        timeout: float,
        deadline: float,
    ) -> str | bytes:
        try:
            return connection.recv(timeout=timeout)
        except TimeoutError:
            if self._clock() >= deadline:
                raise self._fail("sentence_timeout") from None
            raise self._fail("frame_idle_timeout") from None
        except VoiceStudioWebSocketTransportError:
            raise self._fail("connection_closed_before_done") from None
        except OSError:
            raise self._fail("transport_failed") from None

    @staticmethod
    def _parse(message: str) -> dict[str, Any]:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            raise VoiceStudioWebSocketError("message_not_json") from None
        if not isinstance(payload, dict):
            raise VoiceStudioWebSocketError("message_type_invalid")
        return payload

    @staticmethod
    def _chunk(
        request: VoiceStudioSentenceRequest,
        sequence: int,
        data: bytes,
        *,
        final: bool,
    ) -> PCMChunk:
        return PCMChunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=sequence,
            sample_rate=_SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=data,
            is_final=final,
        )

    def _stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        cancelled: Event,
    ) -> Iterator[PCMChunk]:
        self._begin_request()
        completed = False
        try:
            if cancelled.is_set():
                raise VoiceStudioWebSocketError("cancelled_before_request")
            connection = self._ensure_connection()
            payload: dict[str, Any] = {
                "text": request.text,
                "voice": request.profile_id,
                "language": request.language,
                "speed": request.speed,
            }
            if request.engine is not None:
                payload["engine"] = request.engine
            try:
                connection.send(json.dumps(payload, ensure_ascii=False))
            except VoiceStudioWebSocketTransportError:
                raise self._fail("send_failed") from None
            except OSError:
                raise self._fail("transport_failed") from None

            deadline = self._clock() + self.config.sentence_timeout_seconds
            validator = PCMChunkStreamValidator(request)
            started = False
            total_bytes = 0
            sequence = 0
            pending: tuple[int, bytes] | None = None

            while True:
                if cancelled.is_set():
                    raise self._fail("cancelled_during_stream")
                message = self._receive(
                    connection,
                    self._recv_timeout(deadline, audio_started=started),
                    deadline,
                )
                if isinstance(message, bytes):
                    if not started:
                        raise self._fail("audio_before_start")
                    if not message:
                        raise self._fail("audio_frame_empty")
                    if len(message) % 2:
                        raise self._fail("audio_frame_invalid")
                    if len(message) > self.config.max_frame_bytes:
                        raise self._fail("frame_too_large")
                    if total_bytes + len(message) > self.config.max_total_audio_bytes:
                        raise self._fail("audio_too_large")
                    if pending is not None:
                        yield validator.accept(
                            self._chunk(request, pending[0], pending[1], final=False)
                        )
                    total_bytes += len(message)
                    sequence += 1
                    pending = (sequence, message)
                    continue
                if not isinstance(message, str):
                    raise self._fail("message_type_invalid")
                data = self._parse(message)
                message_type = data.get("type")
                if message_type == "routing":
                    continue
                if message_type == "start":
                    if started:
                        raise self._fail("duplicate_start")
                    if data.get("format") != "pcm16":
                        raise self._fail("start_format_invalid")
                    if data.get("sample_rate") != _SAMPLE_RATE:
                        raise self._fail("start_sample_rate_invalid")
                    if data.get("channels") != 1:
                        raise self._fail("start_channels_invalid")
                    started = True
                    continue
                if message_type == "done":
                    if not started:
                        raise self._fail("done_before_start")
                    samples = data.get("samples")
                    if type(samples) is not int or samples < 0:
                        raise self._fail("done_samples_missing")
                    if samples * 2 != total_bytes:
                        raise self._fail("samples_mismatch")
                    if pending is None:
                        raise self._fail("audio_empty")
                    yield validator.accept(
                        self._chunk(request, pending[0], pending[1], final=True)
                    )
                    validator.finish()
                    completed = True
                    return
                if message_type == "error":
                    # VoiceStudio keeps the socket open after an error frame, so the
                    # connection is dropped here instead of being reused in an
                    # unknown protocol state.
                    raise self._fail("server_error")
                if message_type in _ALLOWED_MESSAGE_TYPES:
                    continue
                raise self._fail("message_type_invalid")
        finally:
            if not completed:
                self._drop_connection()
            self._end_request()


__all__ = [
    "ConnectionFactory",
    "VoiceStudioWebSocketConfig",
    "VoiceStudioWebSocketError",
    "VoiceStudioWebSocketProvider",
    "VoiceStudioWebSocketTransportError",
    "WebSocketConnection",
]
