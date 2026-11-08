"""Streaming PCM playback worker for one VoiceStudio sentence at a time.

Batch 4 adds the PCM consumption seam required by the dual-path plan: a bounded
per-sentence queue, a start-buffer gate, and a reusable device-buffer contract
implemented by :mod:`app.tts.waveout_pcm`. Playback facts keep the existing
``PlaybackState`` semantics: ``STOP_ACKED`` still means only that the backend
returned its buffers, never that a physical speaker went silent or that a person
heard anything. This module never opens a device by itself; the backend is
injected.

The only timing surfaces added later are read-only:
:meth:`PCMPlaybackWorker.device_submit_at` returns the monotonic instant at which
one sentence's first PCM batch was handed to the injected backend, and
:meth:`PCMPlaybackWorker.device_gate_open_at` returns the instant the start-buffer
gate first opened for that sentence — the moment the worker decided to begin
submitting. Both expose a bare ``float`` (no text, PCM bytes, path or URL), they
never block a submission, and queueing, start-buffer, backpressure, stop-ACK,
reset and failure semantics are unchanged whether or not anyone reads them. The
gap ``device_submit_at - device_gate_open_at`` is the only span that excludes the
configured start-buffer accumulation, so it is the one comparable with the plan's
"less than 50 ms extra once the start buffer is excluded" criterion; a real
measurement still needs separate authorization.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Callable, Protocol

from .clock import monotonic_seconds
from .playback_worker import (
    PlaybackEvent,
    PlaybackState,
    PlaybackTicket,
    StopResult,
)
from .providers.contracts import PCMChunk, VoiceStudioSentenceRequest


SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # PCM16 mono: 48 bytes per millisecond
_DEFAULT_MAX_SENTENCE_BYTES = 16 * 1024 * 1024


class PCMPlaybackError(ValueError):
    """PCM queue, identity, order or lifecycle contract violation."""


class PCMPlaybackBackpressure(PCMPlaybackError):
    """The queued PCM for the current sentence reached its configured budget."""


class PCMBufferBusy(RuntimeError):
    """No device buffer has returned yet, so nothing can be submitted."""


class PCMStreamBackend(Protocol):
    """Reusable device-buffer seam driven by :class:`PCMPlaybackWorker`.

    ``writable`` reports whether a buffer has returned (or is still unused) so
    that its memory may be rewritten; ``poll_drained`` reports whether every
    submitted buffer has come back. A backend must never reuse a buffer that the
    device still owns.
    """

    backend_name: str
    max_buffer_bytes: int

    def open(self) -> None: ...

    def writable(self) -> bool: ...

    def submit(self, pcm: bytes) -> None: ...

    def poll_drained(self) -> bool: ...

    def stop(self, timeout: float) -> StopResult: ...

    def close(self) -> None: ...


class PCMPlaybackWorker:
    """Serialize one sentence of streamed PCM into a reusable-buffer backend."""

    def __init__(
        self,
        backend_factory: Callable[[], PCMStreamBackend],
        *,
        start_buffer_ms: int = 160,
        max_queued_audio_ms: int = 8000,
        max_sentence_bytes: int = _DEFAULT_MAX_SENTENCE_BYTES,
        stop_timeout: float = 1.0,
        event_sink: Callable[[PlaybackEvent], None] | None = None,
        clock: Callable[[], float] = monotonic_seconds,
    ) -> None:
        for name, value in (
            ("start_buffer_ms", start_buffer_ms),
            ("max_queued_audio_ms", max_queued_audio_ms),
            ("max_sentence_bytes", max_sentence_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name}_must_be_positive")
        if type(stop_timeout) not in {int, float} or isinstance(stop_timeout, bool) or stop_timeout <= 0:
            raise ValueError("stop_timeout_must_be_positive")
        if not callable(backend_factory):
            raise TypeError("backend_factory 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._backend_factory = backend_factory
        self._start_buffer_bytes = start_buffer_ms * BYTES_PER_MS
        self._max_queued_bytes = max_queued_audio_ms * BYTES_PER_MS
        self._max_sentence_bytes = max_sentence_bytes
        self._stop_timeout = float(stop_timeout)
        self._event_sink = event_sink
        # Observation only. The shared ``monotonic_seconds`` default keeps every
        # existing construction and every playback path bit-for-bit unchanged,
        # while a caller-injected clock still wins unchanged.
        self._clock = clock
        self._first_submit_identity: tuple[str, str, str, int] | None = None
        self._first_submit_at: float | None = None
        self._gate_open_identity: tuple[str, str, str, int] | None = None
        self._gate_open_at: float | None = None
        self._condition = threading.Condition()
        self._backend: PCMStreamBackend | None = None
        self._ticket: PlaybackTicket | None = None
        self._request: VoiceStudioSentenceRequest | None = None
        self._last_request: VoiceStudioSentenceRequest | None = None
        self._pending = bytearray()
        self._expected_sequence = 1
        self._accepted_bytes = 0
        self._submitted_any = False
        self._started = False
        self._final_received = False
        self._stop_requested = False
        self._stop_attempted = False
        self._disabled = False
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="pcm-playback-worker", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ public

    @property
    def disabled(self) -> bool:
        with self._condition:
            return self._disabled

    @property
    def active(self) -> PlaybackTicket | None:
        with self._condition:
            return self._ticket

    @property
    def pending_bytes(self) -> int:
        with self._condition:
            return len(self._pending)

    @property
    def queued_audio_ms(self) -> int:
        """PCM accepted but not yet handed to the device, in milliseconds."""

        with self._condition:
            return len(self._pending) // BYTES_PER_MS

    def device_submit_at(self, request: VoiceStudioSentenceRequest) -> float | None:
        """Read-only observation: when this sentence's first PCM reached the device.

        Returns the monotonic stamp taken at the moment the first accepted batch
        of ``request`` was handed to the injected backend, or ``None`` when that
        sentence never reached the device or the worker has already moved on to a
        different sentence identity. The value is a bare ``float``: no sentence
        text, PCM bytes, path or URL is exposed. Reading it changes nothing —
        queueing, the start-buffer gate, backpressure, stop/ACK, reset and
        fail-closed behaviour are all identical whether it is read or not.
        """

        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        with self._condition:
            identity = self._first_submit_identity
            submitted_at = self._first_submit_at
            if submitted_at is None or identity is None:
                return None
            if identity != self._identity(request):
                return None
            return submitted_at

    def device_gate_open_at(self, request: VoiceStudioSentenceRequest) -> float | None:
        """Read-only observation: when this sentence's start-buffer gate opened.

        Returns the monotonic stamp taken at the single moment the start-buffer
        gate was first satisfied for ``request`` — the instant ``_started`` flips
        to ``True`` inside ``_take_submission_locked`` and the worker decides to
        begin handing PCM to the backend. It is taken under the same
        ``self._condition`` lock that protects the gate itself and is matched by
        the same sentence identity as :meth:`device_submit_at`, so a different
        sentence returns ``None``; so does a sentence whose gate never opened.

        The value is a bare ``float``: no sentence text, PCM bytes, path or URL is
        exposed. ``device_submit_at(request) - device_gate_open_at(request)`` is
        the plan's "extra cost once the start buffer is excluded" span; the two
        stamps may legitimately be equal, in which case the honest answer is the
        exact ``0.0`` and never a manufactured difference. Reading this changes
        nothing: the start-buffer threshold, ``_started`` semantics, queueing,
        backpressure, stop/ACK, reset and fail-closed behaviour are all identical
        whether it is read or not.
        """

        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        with self._condition:
            identity = self._gate_open_identity
            opened_at = self._gate_open_at
            if opened_at is None or identity is None:
                return None
            if identity != self._identity(request):
                return None
            return opened_at

    def begin_sentence(self, request: VoiceStudioSentenceRequest) -> PlaybackTicket:
        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        with self._condition:
            if self._closed:
                raise PCMPlaybackError("worker_closed")
            if self._disabled:
                raise PCMPlaybackError("voice_disabled")
            if self._ticket is not None and not self._ticket.terminal:
                raise PCMPlaybackError("sentence_already_active")
            backend_name = self._backend.backend_name if self._backend is not None else "pending"
            ticket = PlaybackTicket(secrets.token_hex(10), backend_name)
            self._ticket = ticket
            self._request = request
            self._last_request = request
            self._expected_sequence = 1
            self._accepted_bytes = 0
            self._submitted_any = False
            self._started = False
            self._final_received = False
            self._stop_requested = False
            self._stop_attempted = False
            self._first_submit_identity = None
            self._first_submit_at = None
            self._gate_open_identity = None
            self._gate_open_at = None
            self._pending.clear()
            self._condition.notify_all()
            return ticket

    def push_chunk(self, chunk: PCMChunk) -> bool:
        """Accept one ordered chunk; return False when it is a dropped late chunk."""

        if not isinstance(chunk, PCMChunk):
            raise TypeError("chunk 必须是 PCMChunk")
        with self._condition:
            if self._closed:
                raise PCMPlaybackError("worker_closed")
            ticket = self._ticket
            if ticket is None or ticket.terminal:
                last = self._last_request
                if last is not None and self._identity(chunk) == self._identity(last):
                    return False
                raise PCMPlaybackError("no_active_sentence")
            if self._final_received:
                return False
            request = self._request
            assert request is not None
            if self._identity(chunk) != self._identity(request):
                raise PCMPlaybackError("identity_mismatch")
            if chunk.sequence != self._expected_sequence:
                raise PCMPlaybackError("sequence_invalid")
            if self._accepted_bytes + len(chunk.data) > self._max_sentence_bytes:
                raise PCMPlaybackError("sentence_memory_limit")
            if len(self._pending) + len(chunk.data) > self._max_queued_bytes:
                raise PCMPlaybackBackpressure("queued_audio_limit")
            self._expected_sequence += 1
            self._accepted_bytes += len(chunk.data)
            self._pending.extend(chunk.data)
            if chunk.is_final:
                self._final_received = True
            self._condition.notify_all()
            return True

    def wait(self, ticket: PlaybackTicket, timeout: float | None = None) -> PlaybackState:
        return ticket.wait(timeout)

    def stop(self, ticket: PlaybackTicket | None = None, *, timeout: float | None = None) -> PlaybackState:
        with self._condition:
            target = ticket or self._ticket
            if target is None:
                return PlaybackState.STOP_ACKED
            if target is not self._ticket:
                raise ValueError("ticket is not owned by this playback worker")
            if target.terminal:
                return target.state
            self._stop_requested = True
            if target.state is not PlaybackState.STOP_REQUESTED:
                self._transition(target, PlaybackState.STOP_REQUESTED, reason="stop_requested")
            self._condition.notify_all()
        wait_timeout = self._stop_timeout if timeout is None else timeout
        state = target.wait(wait_timeout + 0.25)
        if not target.terminal:
            with self._condition:
                if target.terminal:
                    return target.state
                self._disabled = True
                self._transition(
                    target, PlaybackState.STOP_TIMEOUT, reason="stop_wait_timeout",
                    stop_ack=False, confirmation="timeout", voice_disabled=True,
                )
                self._ticket = None
            return PlaybackState.STOP_TIMEOUT
        return state

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._stop_requested = True
            self._condition.notify_all()
        self._thread.join(timeout=max(self._stop_timeout + 1.0, 2.0))
        if self._thread.is_alive():
            with self._condition:
                self._disabled = True

    # ----------------------------------------------------------------- internal

    @staticmethod
    def _identity(value: PCMChunk | VoiceStudioSentenceRequest) -> tuple[str, str, str, int]:
        return (
            value.reply_id,
            value.segment_id,
            value.provider_request_id,
            value.provider_generation,
        )

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
            backend.open()
            self._backend = backend
            while True:
                with self._condition:
                    if self._closed:
                        break
                    stop_now = self._stop_requested and not self._stop_attempted
                    if stop_now:
                        self._stop_attempted = True
                    batch: bytes | None = None
                    if not stop_now:
                        batch = self._take_submission_locked(backend.max_buffer_bytes)
                    check_drained = (
                        self._ticket is not None and not self._ticket.terminal
                        and self._final_received and not self._pending
                    )
                if stop_now:
                    self._perform_stop()
                    continue
                if batch is not None:
                    self._submit_batch(batch)
                    continue
                if check_drained and backend.poll_drained():
                    self._complete()
                    continue
                with self._condition:
                    self._condition.wait(timeout=0.01)
        except Exception as exc:  # noqa: BLE001 - worker failure must disable voice
            with self._condition:
                self._disabled = True
                if self._ticket is not None and not self._ticket.terminal:
                    ticket = self._ticket
                    ticket.error = type(exc).__name__
                    self._transition(ticket, PlaybackState.FAILED, reason="worker_exception")
                self._ticket = None
                self._pending.clear()
        finally:
            with self._condition:
                backend = self._backend
            if backend is not None:
                try:
                    backend.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    with self._condition:
                        self._disabled = True

    def _take_submission_locked(self, max_bytes: int) -> bytes | None:
        # Every caller holds ``self._condition``; the gate stamp below is written
        # under that same lock, exactly like the device-submit stamp.
        if self._ticket is None or self._ticket.terminal or not self._pending:
            return None
        if not self._started:
            if not self._final_received and len(self._pending) < self._start_buffer_bytes:
                return None
            self._started = True
            # Observation only: the start-buffer gate just opened for this
            # sentence, so this is the instant the worker decided to begin
            # submitting. It deliberately does not defer to the first
            # ``backend.submit()``: if both fall on the same instant the two
            # stamps are equal, and that equality is recorded as it is.
            if self._gate_open_at is None and self._request is not None:
                self._gate_open_at = self._clock()
                self._gate_open_identity = self._identity(self._request)
        size = min(max_bytes, len(self._pending))
        batch = bytes(self._pending[:size])
        del self._pending[:size]
        return batch

    def _submit_batch(self, batch: bytes) -> None:
        backend = self._backend
        if backend is None:
            return
        if not backend.writable():
            with self._condition:
                self._pending[0:0] = batch
                self._condition.wait(timeout=0.01)
            return
        # Observation only: stamped before the handoff, published only after the
        # backend accepted the batch, so a retried/rejected batch keeps no stamp.
        submitted_at = self._clock()
        try:
            backend.submit(batch)
        except PCMBufferBusy:
            with self._condition:
                self._pending[0:0] = batch
                self._condition.wait(timeout=0.01)
            return
        except Exception as exc:  # noqa: BLE001 - device failure is fail-closed
            with self._condition:
                self._disabled = True
                ticket = self._ticket
                if ticket is not None and not ticket.terminal:
                    ticket.error = type(exc).__name__
                    self._transition(ticket, PlaybackState.FAILED, reason="backend_submit_failed")
                self._ticket = None
                self._pending.clear()
            return
        with self._condition:
            if self._first_submit_at is None and self._request is not None:
                self._first_submit_at = submitted_at
                self._first_submit_identity = self._identity(self._request)
            self._submitted_any = True
            self._condition.notify_all()

    def _perform_stop(self) -> None:
        with self._condition:
            ticket = self._ticket
            submitted = self._submitted_any
        if ticket is None or ticket.terminal:
            return
        if not submitted:
            with self._condition:
                self._pending.clear()
                self._transition(
                    ticket, PlaybackState.STOP_ACKED, reason="stopped_before_submission",
                    stop_ack=True, confirmation="not_submitted",
                )
                self._ticket = None
            return
        try:
            result = self._backend.stop(self._stop_timeout)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - unknown stop must not be an ACK
            result = StopResult(False, "unknown", type(exc).__name__)
        with self._condition:
            self._pending.clear()
            if ticket.terminal:
                self._ticket = None
                return
            if result.acknowledged and result.confirmation not in ("unknown", "timeout", "callable_returned"):
                self._transition(
                    ticket, PlaybackState.STOP_ACKED, reason=result.reason or "backend_stop_ack",
                    stop_ack=True, confirmation=result.confirmation,
                )
            elif result.confirmation == "timeout":
                self._disabled = True
                self._transition(
                    ticket, PlaybackState.STOP_TIMEOUT, reason=result.reason or "backend_stop_timeout",
                    stop_ack=False, confirmation="timeout", voice_disabled=True,
                )
            else:
                self._disabled = True
                self._transition(
                    ticket, PlaybackState.UNKNOWN, reason=result.reason or "backend_stop_unknown",
                    stop_ack=False, confirmation="unknown", voice_disabled=True,
                )
            self._ticket = None
            self._condition.notify_all()

    def _complete(self) -> None:
        with self._condition:
            ticket = self._ticket
            if ticket is None or ticket.terminal:
                return
            self._transition(
                ticket, PlaybackState.COMPLETED, reason="pcm_sentence_drained",
                stop_ack=True, confirmation="backend_buffer_returned",
            )
            self._ticket = None
            self._condition.notify_all()

    def _transition(self, ticket: PlaybackTicket, state: PlaybackState, *, reason: str,
                    stop_ack: bool | None = None, confirmation: str | None = None,
                    voice_disabled: bool | None = None) -> None:
        if ticket.terminal:
            return
        ticket.state = state
        event = PlaybackEvent(
            event_id=secrets.token_hex(10), ticket_id=ticket.ticket_id, state=state,
            observed_at=time.time(), backend=ticket.backend, reason=reason,
            stop_ack=stop_ack, confirmation=confirmation,
            voice_disabled=self._disabled if voice_disabled is None else voice_disabled,
        )
        ticket.events.append(event)
        with ticket._condition:
            ticket._condition.notify_all()
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception:  # noqa: BLE001 - diagnostics must not break playback
                pass


__all__ = [
    "BYTES_PER_MS",
    "PCMBufferBusy",
    "PCMPlaybackBackpressure",
    "PCMPlaybackError",
    "PCMPlaybackWorker",
    "PCMStreamBackend",
    "SAMPLE_RATE",
]
