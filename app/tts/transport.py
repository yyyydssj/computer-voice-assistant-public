"""VoiceStudio internal transport routing: WebSocket PCM first, HTTP WAV once.

This is the *inside-VoiceStudio* layer. It decides whether one sentence is
streamed over ``/ws/tts`` or rendered by ``/generate``; the cross-provider layer
(VoiceStudio -> Edge -> text) stays in :mod:`app.tts.routing` and is not changed
here.

Rules fixed by the dual-path plan (sections 8 and 13):

- ``http_wav`` only ever uses the HTTP provider;
- ``ws_pcm`` only ever uses the WebSocket provider and never falls back;
- ``auto`` tries WebSocket, and may redo the **same sentence** over HTTP exactly
  once, but only when the WebSocket attempt failed **before any PCM was
  produced**;
- once any PCM was produced for the sentence, a failure is terminal for that
  transport: no redo, no voice switch. The caller (``TTSFallbackRouter``) owns
  the cross-provider decision.

Only transport, stage, short failure code and latency are recorded. Sentence
text and PCM are never stored here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Event
import threading
from typing import Callable, Iterator, Protocol

from .clock import monotonic_seconds
from .providers.contracts import PCMChunk, VoiceStudioContractError, VoiceStudioSentenceRequest
from .providers.http_wav import VoiceStudioHttpWavError
from .providers.voicestudio_ws import VoiceStudioWebSocketError


class VoiceStudioTransport(str, Enum):
    WS_PCM = "ws_pcm"
    HTTP_WAV = "http_wav"


class TransportFailureStage(str, Enum):
    CONNECT = "connect"
    PROTOCOL = "protocol"
    TIMEOUT_BEFORE_AUDIO = "timeout_before_audio"
    AFTER_AUDIO = "after_audio"
    CANCELLED = "cancelled"
    HTTP = "http"


_ALLOWED_MODES = ("http_wav", "ws_pcm", "auto")
_CONNECT_CODES = frozenset(
    {"connect_failed", "send_failed", "transport_failed", "connection_closed_before_done"}
)
_TIMEOUT_CODES = frozenset({"frame_idle_timeout", "sentence_timeout"})


def _stage_for(code: str, *, produced: bool) -> str:
    if code.startswith("cancelled"):
        return TransportFailureStage.CANCELLED.value
    if produced:
        return TransportFailureStage.AFTER_AUDIO.value
    if code in _CONNECT_CODES:
        return TransportFailureStage.CONNECT.value
    if code in _TIMEOUT_CODES:
        return TransportFailureStage.TIMEOUT_BEFORE_AUDIO.value
    return TransportFailureStage.PROTOCOL.value


class VoiceStudioTransportError(VoiceStudioContractError):
    """Fail-closed transport decision error carrying only a short code."""

    def __init__(self, code: str, *, transport: str, stage: str,
                 fallback_from: str | None = None) -> None:
        # The code is both the fixed short code and the message; it goes through the
        # base constructor so the base cannot reset the attribute to ``None``.
        self.transport = transport
        self.stage = stage
        self.fallback_from = fallback_from
        super().__init__(code, code=code)


class VoiceStudioTransportProvider(Protocol):
    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event) -> Iterator[PCMChunk]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TransportFallbackRecord:
    """One inside-VoiceStudio transport switch; not a playback or audibility fact."""

    reply_id: str
    segment_id: str
    provider_request_id: str
    provider_generation: int
    from_transport: str
    to_transport: str
    stage: str
    reason: str
    latency_ms: float


@dataclass(frozen=True, slots=True)
class TransportSnapshot:
    mode: str
    last_transport: str | None
    ws_attempts: int
    http_attempts: int
    fallbacks: int
    last_failure_code: str | None
    last_failure_stage: str | None


class VoiceStudioTransportRouter:
    """Synchronous, per-sentence WebSocket-first / HTTP-once transport router."""

    def __init__(
        self,
        *,
        mode: str,
        ws_provider: VoiceStudioTransportProvider,
        http_provider: VoiceStudioTransportProvider,
        clock: Callable[[], float] = monotonic_seconds,
        max_http_fallbacks_per_sentence: int = 1,
    ) -> None:
        if type(mode) is not str or mode not in _ALLOWED_MODES:
            raise ValueError("unsupported_transport_mode")
        if (type(max_http_fallbacks_per_sentence) is not int
                or max_http_fallbacks_per_sentence < 0):
            raise ValueError("max_http_fallbacks_per_sentence_must_be_non_negative")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._mode = mode
        self._ws = ws_provider
        self._http = http_provider
        self._clock = clock
        self._max_http = max_http_fallbacks_per_sentence
        self._lock = threading.Lock()
        self._closed = False
        self._ws_attempts = 0
        self._http_attempts = 0
        self._fallbacks: list[TransportFallbackRecord] = []
        self._last_transport: str | None = None
        self._last_failure_code: str | None = None
        self._last_failure_stage: str | None = None

    # ------------------------------------------------------------------ public

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fallbacks(self) -> tuple[TransportFallbackRecord, ...]:
        with self._lock:
            return tuple(self._fallbacks)

    @property
    def snapshot(self) -> TransportSnapshot:
        with self._lock:
            return TransportSnapshot(
                mode=self._mode,
                last_transport=self._last_transport,
                ws_attempts=self._ws_attempts,
                http_attempts=self._http_attempts,
                fallbacks=len(self._fallbacks),
                last_failure_code=self._last_failure_code,
                last_failure_stage=self._last_failure_stage,
            )

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
        return self._stream(request, cancelled)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for provider in (self._ws, self._http):
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass

    # ----------------------------------------------------------------- internal

    def _error(self, code: str, *, transport: str, stage: str,
               fallback_from: str | None = None) -> VoiceStudioTransportError:
        return VoiceStudioTransportError(
            code, transport=transport, stage=stage, fallback_from=fallback_from
        )

    def _count(self, transport: VoiceStudioTransport) -> None:
        with self._lock:
            if transport is VoiceStudioTransport.WS_PCM:
                self._ws_attempts += 1
            else:
                self._http_attempts += 1

    def _record_failure(self, code: str, stage: str) -> None:
        with self._lock:
            self._last_failure_code = code
            self._last_failure_stage = stage

    def _record_success(self, transport: VoiceStudioTransport) -> None:
        with self._lock:
            self._last_transport = transport.value

    def _record_fallback(self, request: VoiceStudioSentenceRequest, *, stage: str,
                         reason: str, latency_ms: float) -> None:
        with self._lock:
            self._fallbacks.append(
                TransportFallbackRecord(
                    reply_id=request.reply_id,
                    segment_id=request.segment_id,
                    provider_request_id=request.provider_request_id,
                    provider_generation=request.provider_generation,
                    from_transport=VoiceStudioTransport.WS_PCM.value,
                    to_transport=VoiceStudioTransport.HTTP_WAV.value,
                    stage=stage,
                    reason=reason,
                    latency_ms=latency_ms,
                )
            )

    def _stream(self, request: VoiceStudioSentenceRequest,
                cancelled: Event) -> Iterator[PCMChunk]:
        if cancelled.is_set():
            raise self._error(
                "cancelled_before_request", transport=self._mode,
                stage=TransportFailureStage.CANCELLED.value,
            )
        if self._mode == "http_wav":
            yield from self._stream_http(request, cancelled, fallback_from=None)
            return

        produced = False
        http_used = 0
        started = self._clock()
        self._count(VoiceStudioTransport.WS_PCM)
        try:
            for chunk in self._ws.stream_sentence(request, cancelled=cancelled):
                produced = True
                yield chunk
        except (VoiceStudioWebSocketError, VoiceStudioContractError) as exc:
            code = getattr(exc, "code", "ws_contract_error")
            stage = _stage_for(code, produced=produced)
            self._record_failure(code, stage)
            if (self._mode == "ws_pcm" or produced
                    or stage == TransportFailureStage.CANCELLED.value):
                raise self._error(
                    code, transport=VoiceStudioTransport.WS_PCM.value, stage=stage
                ) from None
            if http_used >= self._max_http:
                raise self._error(
                    code, transport=VoiceStudioTransport.WS_PCM.value, stage=stage
                ) from None
            http_used += 1
            self._record_fallback(
                request, stage=stage, reason=code,
                latency_ms=max(0.0, (self._clock() - started) * 1000.0),
            )
            yield from self._stream_http(
                request, cancelled, fallback_from=VoiceStudioTransport.WS_PCM.value
            )
            return
        self._record_success(VoiceStudioTransport.WS_PCM)

    def _stream_http(self, request: VoiceStudioSentenceRequest, cancelled: Event,
                     *, fallback_from: str | None) -> Iterator[PCMChunk]:
        if cancelled.is_set():
            raise self._error(
                "cancelled_before_request", transport=VoiceStudioTransport.HTTP_WAV.value,
                stage=TransportFailureStage.CANCELLED.value, fallback_from=fallback_from,
            )
        self._count(VoiceStudioTransport.HTTP_WAV)
        try:
            for chunk in self._http.stream_sentence(request, cancelled=cancelled):
                yield chunk
        except (VoiceStudioHttpWavError, VoiceStudioContractError) as exc:
            code = getattr(exc, "code", "http_contract_error")
            self._record_failure(code, TransportFailureStage.HTTP.value)
            raise self._error(
                code, transport=VoiceStudioTransport.HTTP_WAV.value,
                stage=TransportFailureStage.HTTP.value, fallback_from=fallback_from,
            ) from None
        self._record_success(VoiceStudioTransport.HTTP_WAV)


__all__ = [
    "TransportFailureStage",
    "TransportFallbackRecord",
    "TransportSnapshot",
    "VoiceStudioTransport",
    "VoiceStudioTransportError",
    "VoiceStudioTransportProvider",
    "VoiceStudioTransportRouter",
]
