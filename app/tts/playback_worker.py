from __future__ import annotations

"""Long-lived, cancellable playback worker and Windows backend seam.

The worker records backend lifecycle facts.  A subprocess exit, a callable
return, or a request being accepted is never treated as a stop acknowledgement.
The default Windows adapter uses waveOutReset plus the buffer-return callback;
that confirms the backend returned the submitted buffer, not that a person heard
silence from a physical speaker.
"""

from dataclasses import dataclass, field, replace
from enum import Enum
import ctypes
import queue
import secrets
import sys
import threading
import time
from typing import Callable, Protocol


class PlaybackState(str, Enum):
    QUEUED = "QUEUED"
    PLAYING = "PLAYING"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOP_ACKED = "STOP_ACKED"
    COMPLETED = "COMPLETED"
    STOP_TIMEOUT = "STOP_TIMEOUT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


_TERMINAL = frozenset({PlaybackState.STOP_ACKED, PlaybackState.COMPLETED,
                       PlaybackState.STOP_TIMEOUT, PlaybackState.FAILED,
                       PlaybackState.UNKNOWN})


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    event_id: str
    ticket_id: str
    state: PlaybackState
    observed_at: float
    backend: str
    reason: str | None = None
    stop_ack: bool | None = None
    confirmation: str | None = None
    voice_disabled: bool = False


@dataclass(frozen=True, slots=True)
class StopResult:
    acknowledged: bool
    confirmation: str
    reason: str | None = None


class WaveOutFaultMode(str, Enum):
    """Explicit, opt-in fault modes for the real Windows waveOut seam.

    The native device is still opened and ``waveOutReset`` is still called.
    The shim only changes the returned stop classification after that native
    call, so it never requires DLL replacement, process termination, or
    invalid-handle/device destruction.  This is controlled real-backend fault
    evidence, not a claim of a naturally occurring hardware fault.
    """

    UNKNOWN_AFTER_REAL_RESET = "unknown_after_real_reset"
    TIMEOUT_AFTER_REAL_RESET = "timeout_after_real_reset"


@dataclass(frozen=True, slots=True)
class WaveOutDiagnostic:
    """Read-only WinMM lifecycle facts for the current/last submission.

    These fields describe calls and callbacks observed by the ``waveOut``
    backend only.  They do not claim that a physical speaker became silent or
    that a person heard anything.  A new ``start`` replaces the snapshot after
    the previous submission has been released.
    """

    open_result: int | None = None
    prepare_result: int | None = None
    write_result: int | None = None
    reset_result: int | None = None
    unprepare_result: int | None = None
    close_result: int | None = None
    opened_at: float | None = None
    prepared_at: float | None = None
    submitted_at: float | None = None
    reset_at: float | None = None
    wom_done_at: float | None = None
    unprepared_at: float | None = None
    closed_at: float | None = None
    header_address: int | None = None
    cleanup_verified: bool = False


class PlaybackBackend(Protocol):
    backend_name: str

    def start(self, audio: bytes) -> None: ...
    def poll_complete(self) -> bool: ...
    def stop(self, timeout: float) -> StopResult: ...
    def finalize_completed(self, timeout: float) -> StopResult: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class PlaybackTicket:
    ticket_id: str
    backend: str
    events: list[PlaybackEvent] = field(default_factory=list)
    state: PlaybackState = PlaybackState.QUEUED
    error: str | None = None
    _condition: threading.Condition = field(default_factory=threading.Condition, repr=False)

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def wait(self, timeout: float | None = None) -> PlaybackState:
        with self._condition:
            if not self.terminal:
                self._condition.wait_for(lambda: self.terminal, timeout=timeout)
            return self.state


@dataclass(frozen=True, slots=True)
class _Command:
    kind: str
    ticket: PlaybackTicket | None = None
    audio: bytes | None = None


class PlaybackWorker:
    """One persistent worker serializing playback and independently verifiable stop."""

    def __init__(self, backend_factory: Callable[[], PlaybackBackend], *,
                 stop_timeout: float = 1.0,
                 event_sink: Callable[[PlaybackEvent], None] | None = None) -> None:
        if stop_timeout <= 0:
            raise ValueError("stop_timeout must be positive")
        self._backend_factory = backend_factory
        self._stop_timeout = float(stop_timeout)
        self._event_sink = event_sink
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._pending: list[PlaybackTicket] = []
        self._backend: PlaybackBackend | None = None
        self._ready = threading.Event()
        self._active: PlaybackTicket | None = None
        self._stop_requested: set[str] = set()
        self._stop_attempted: set[str] = set()
        self._disabled = False
        self._closed = False
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._run, name="tts-playback-worker", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=1.0):
            with self._lock:
                self._disabled = True
                self._closed = True
            raise RuntimeError("playback backend initialization timed out")

    @property
    def disabled(self) -> bool:
        with self._lock:
            return self._disabled

    @property
    def active(self) -> PlaybackTicket | None:
        with self._lock:
            return self._active

    def submit(self, audio: bytes, *, metadata: dict[str, str] | None = None) -> PlaybackTicket:
        del metadata  # reserved for redacted correlation fields at the caller boundary
        if type(audio) is not bytes or not audio:
            raise ValueError("audio must be non-empty bytes")
        with self._lock:
            if self._closed:
                raise RuntimeError("playback worker is closed")
            if self._disabled:
                raise RuntimeError("playback worker is disabled")
            backend = self._backend
            backend_name = backend.backend_name if backend is not None else "pending"
            ticket = PlaybackTicket(secrets.token_hex(10), backend_name)
            self._pending.append(ticket)
            self._commands.put(_Command("play", ticket, audio))
            return ticket

    def wait(self, ticket: PlaybackTicket, timeout: float | None = None) -> PlaybackState:
        return ticket.wait(timeout)

    def stop(self, ticket: PlaybackTicket | None = None, *, timeout: float | None = None) -> PlaybackState:
        with self._lock:
            target = ticket or self._active or (self._pending[0] if self._pending else None)
            if target is None:
                return PlaybackState.STOP_ACKED
            if not any(item is target for item in self._pending) and target is not self._active:
                raise ValueError("ticket is not owned by this playback worker")
            if target.terminal:
                return target.state
            if target is not self._active:
                # An unsubmitted queued segment cannot require a device ACK.
                # Do not time it out while a previous segment is still active.
                self._pending.remove(target)
                self._transition(target, PlaybackState.STOP_ACKED,
                                 reason="stopped_before_submission", stop_ack=True,
                                 confirmation="not_submitted")
                return PlaybackState.STOP_ACKED
            self._stop_requested.add(target.ticket_id)
            if target.state is not PlaybackState.STOP_REQUESTED:
                self._transition(target, PlaybackState.STOP_REQUESTED, reason="stop_requested")
        wait_timeout = self._stop_timeout if timeout is None else timeout
        state = target.wait(wait_timeout + 0.25)
        if not target.terminal:
            # The worker may be inside a blocking backend call. Do not leave a
            # caller-facing STOP_REQUESTED forever: timeout is a terminal,
            # fail-closed result and disables later voice use.
            with self._lock:
                if target.terminal:
                    return target.state
                self._disabled = True
                self._reject_pending(PlaybackState.FAILED, "voice_disabled_after_stop_timeout")
                self._transition(target, PlaybackState.STOP_TIMEOUT,
                                 reason="stop_wait_timeout", stop_ack=False,
                                 confirmation="timeout", voice_disabled=True)
            return PlaybackState.STOP_TIMEOUT
        return state

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._commands.put(_Command("close"))
        self._thread.join(timeout=max(self._stop_timeout + 1.0, 2.0))
        if self._thread.is_alive():
            with self._lock:
                self._disabled = True

    def _run(self) -> None:
        try:
            self._backend = self._backend_factory()
            self._ready.set()
            while True:
                with self._lock:
                    active = self._active
                    closed = self._closed
                if closed:
                    if active is not None and not active.terminal:
                        self._stop_internal(active)
                    with self._lock:
                        self._reject_pending(PlaybackState.FAILED, "worker_closed")
                    return
                if active is None:
                    try:
                        command = self._commands.get(timeout=0.03)
                    except queue.Empty:
                        continue
                    if command.kind == "play" and command.ticket is not None and command.audio is not None:
                        self._start(command.ticket, command.audio)
                    continue
                if active.terminal:
                    # stop() can time out while a backend poll is still in
                    # progress. The ticket is already terminal in that case,
                    # but the device may still be playing; make one best-effort
                    # backend stop before dropping the active reference. The
                    # result is deliberately ignored so a late ACK can never
                    # upgrade STOP_TIMEOUT.
                    if (active.state is PlaybackState.STOP_TIMEOUT
                            and active.ticket_id in self._stop_requested
                            and active.ticket_id not in self._stop_attempted):
                        self._stop_attempted.add(active.ticket_id)
                        try:
                            self._backend.stop(self._stop_timeout)  # type: ignore[union-attr]
                        except Exception:
                            pass
                    with self._lock:
                        self._stop_requested.discard(active.ticket_id)
                        self._stop_attempted.discard(active.ticket_id)
                        self._active = None
                    continue
                with self._lock:
                    stopping = active.ticket_id in self._stop_requested
                if stopping:
                    self._stop_internal(active)
                elif self._backend.poll_complete():
                    with self._lock:
                        completed_without_stop = active.ticket_id not in self._stop_requested
                    if completed_without_stop:
                        self._complete_internal(active)
                else:
                    time.sleep(0.01)
        except Exception as exc:
            self._ready.set()
            with self._lock:
                self._disabled = True
                self._reject_pending(PlaybackState.FAILED, "worker_exception")
                if self._active is not None and not self._active.terminal:
                    self._transition(self._active, PlaybackState.UNKNOWN,
                                     reason="worker_exception", confirmation="unknown")
                    self._active.error = type(exc).__name__
                    self._stop_requested.discard(self._active.ticket_id)
                    self._stop_attempted.discard(self._active.ticket_id)
                    self._active = None
        finally:
            try:
                if self._backend is not None:
                    self._backend.close()
            except Exception:
                with self._lock:
                    self._disabled = True
                # A failed WinMM close may leave a driver reference to the
                # callback or buffer; retain the backend for process lifetime.
                if isinstance(self._backend, WaveOutPlaybackBackend):
                    _uncertain_waveout_backends.append(self._backend)

    def _start(self, ticket: PlaybackTicket, audio: bytes) -> None:
        with self._lock:
            for index, pending in enumerate(self._pending):
                if pending is ticket:
                    self._pending.pop(index)
                    break
            if self._disabled or self._closed:
                if not ticket.terminal:
                    self._transition(ticket, PlaybackState.FAILED, reason="voice_disabled")
                return
            if self._active is not None:
                self._transition(ticket, PlaybackState.FAILED, reason="active_segment_unfinished")
                self._disabled = True
                self._reject_pending(PlaybackState.FAILED, "active_segment_unfinished")
                return
            if ticket.terminal:
                return
            if ticket.ticket_id in self._stop_requested:
                self._transition(ticket, PlaybackState.STOP_ACKED,
                                 reason="stopped_before_submission", stop_ack=True,
                                 confirmation="not_submitted")
                return
            self._active = ticket
            ticket.backend = self._backend.backend_name  # type: ignore[union-attr]
            # Backend.start may block. The STOP_REQUESTED transition can still
            # be recorded concurrently by stop(), but cannot become an ACK.
        try:
            self._backend.start(audio)  # type: ignore[union-attr]
            with self._lock:
                timed_out = ticket.terminal
                if ticket.state is PlaybackState.QUEUED:
                    self._transition(ticket, PlaybackState.PLAYING, reason="backend_start")
            if timed_out:
                # A delayed start after caller timeout might have submitted
                # audio. Best-effort stop; never upgrade the terminal timeout.
                with self._lock:
                    self._stop_attempted.add(ticket.ticket_id)
                try:
                    self._backend.stop(self._stop_timeout)  # type: ignore[union-attr]
                except Exception:
                    pass
        except Exception as exc:
            self._ready.set()
            with self._lock:
                self._disabled = True
                ticket.error = type(exc).__name__
                self._transition(ticket, PlaybackState.FAILED, reason="backend_start_failed")
                self._reject_pending(PlaybackState.FAILED, "backend_start_failed")
                self._active = None

    def _complete_internal(self, ticket: PlaybackTicket) -> None:
        """Finalize a naturally completed backend without changing its state."""
        with self._lock:
            self._stop_attempted.add(ticket.ticket_id)
        try:
            finalizer = getattr(self._backend, "finalize_completed", None)
            if callable(finalizer):
                result = finalizer(self._stop_timeout)
            else:
                # Legacy/custom backends may expose only the explicit-stop seam.
                # Do not call it for natural completion: that would turn a
                # completed playback into an implicit user stop.  Such backends
                # remain unconfirmed until they implement this seam.
                result = StopResult(False, "unknown", "backend_completion_cleanup_unavailable")
        except Exception as exc:
            result = StopResult(False, "unknown", type(exc).__name__)
        with self._lock:
            if ticket.terminal:
                self._active = None
                return
            if result.acknowledged and result.confirmation not in ("unknown", "timeout", "callable_returned"):
                self._transition(
                    ticket, PlaybackState.COMPLETED, reason=result.reason or "backend_complete_cleanup",
                    stop_ack=True, confirmation=result.confirmation,
                )
            else:
                # Natural completion remains distinct from an explicit STOP_ACKED.
                # A failed/unknown cleanup is recorded, never promoted to ACK.
                self._transition(
                    ticket, PlaybackState.COMPLETED, reason=result.reason or "backend_complete_cleanup_unconfirmed",
                    stop_ack=False, confirmation=result.confirmation or "unknown",
                )
            self._stop_requested.discard(ticket.ticket_id)
            self._stop_attempted.discard(ticket.ticket_id)
            self._active = None

    def _stop_internal(self, ticket: PlaybackTicket) -> None:
        if ticket.terminal:
            with self._lock:
                self._active = None
            return
        with self._lock:
            self._stop_attempted.add(ticket.ticket_id)
        try:
            result = self._backend.stop(self._stop_timeout)  # type: ignore[union-attr]
        except Exception as exc:
            result = StopResult(False, "unknown", type(exc).__name__)
        with self._lock:
            if ticket.terminal:
                self._active = None
                return
            if result.acknowledged and result.confirmation not in ("unknown", "timeout", "callable_returned"):
                self._transition(ticket, PlaybackState.STOP_ACKED, reason=result.reason or "backend_stop_ack",
                                 stop_ack=True, confirmation=result.confirmation)
            elif result.confirmation == "timeout":
                self._disabled = True
                self._reject_pending(PlaybackState.FAILED, "voice_disabled_after_stop_timeout")
                self._transition(ticket, PlaybackState.STOP_TIMEOUT, reason=result.reason or "backend_stop_timeout",
                                 stop_ack=False, confirmation="timeout", voice_disabled=True)
            else:
                self._disabled = True
                self._reject_pending(PlaybackState.FAILED, "voice_disabled_after_unknown_stop")
                self._transition(ticket, PlaybackState.UNKNOWN, reason=result.reason or "backend_stop_unknown",
                                 stop_ack=False, confirmation="unknown", voice_disabled=True)
            self._stop_requested.discard(ticket.ticket_id)
            self._stop_attempted.discard(ticket.ticket_id)
            self._active = None

    def _reject_pending(self, state: PlaybackState, reason: str) -> None:
        for pending in self._pending:
            if not pending.terminal:
                self._transition(pending, state, reason=reason)
        self._pending.clear()

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
            except Exception:
                pass


class CallablePlaybackBackend:
    """Test/compatibility backend. Its return is completion, not hardware evidence."""

    backend_name = "callable"

    def __init__(self, player: Callable[[bytes], None]) -> None:
        self._player = player
        self._done = threading.Event()
        self._started = False

    def start(self, audio: bytes) -> None:
        self._started = True
        self._player(audio)
        self._done.set()

    def poll_complete(self) -> bool:
        return self._done.is_set()

    def stop(self, timeout: float) -> StopResult:
        del timeout
        # Callable completion is not independent device stop evidence.
        return StopResult(False, "unknown", "callable_backend_cannot_confirm_stop")

    def finalize_completed(self, timeout: float) -> StopResult:
        del timeout
        # A callable return is completion only; it cannot confirm device cleanup.
        return StopResult(False, "unknown", "callable_backend_cannot_confirm_completion_cleanup")

    def close(self) -> None:
        return None


if sys.platform == "win32":
    _WAVE_FORMAT_PCM = 1
    _CALLBACK_FUNCTION = 0x00030000
    _WOM_DONE = 0x3BD
    _WAVERR_STILLPLAYING = 33
    _MMSYSERR_NOERROR = 0

    class _WaveFormatEx(ctypes.Structure):
        _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                    ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                    ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                    ("cbSize", ctypes.c_ushort)]

    class _WaveHeader(ctypes.Structure):
        _fields_ = [("lpData", ctypes.POINTER(ctypes.c_char)), ("dwBufferLength", ctypes.c_uint),
                    ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_void_p),
                    ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                    ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


# An uncertain device may still own the WAVEHDR and callback. Retain both until
# process exit instead of risking use-after-free of a ctypes callback/buffer.
_uncertain_waveout_backends: list["WaveOutPlaybackBackend"] = []


class WaveOutPlaybackBackend:
    """Controllable Windows PCM backend using waveOutReset and WOM_DONE.

    The stop acknowledgement is explicitly scoped to the waveOut backend
    returning its buffer after reset. It is not a hardware transducer or
    human-audibility acknowledgement.
    """

    backend_name = "windows_waveout"

    def __init__(self, *, fault_mode: WaveOutFaultMode | str | None = None) -> None:
        if sys.platform != "win32":
            raise RuntimeError("windows_output_required")
        self._winmm = ctypes.WinDLL("winmm")
        if fault_mode is None:
            self._fault_mode: WaveOutFaultMode | None = None
        else:
            self._fault_mode = (fault_mode if isinstance(fault_mode, WaveOutFaultMode)
                                else WaveOutFaultMode(fault_mode))
        # ctypes defaults to a 32-bit int return/argument; handles and callbacks
        # require pointer-width signatures on 64-bit Windows.
        self._winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
            ctypes.POINTER(_WaveFormatEx), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self._winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint]
        self._winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint]
        self._winmm.waveOutReset.argtypes = [ctypes.c_void_p]
        self._winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint]
        self._winmm.waveOutClose.argtypes = [ctypes.c_void_p]
        for name in ("waveOutOpen", "waveOutPrepareHeader", "waveOutWrite", "waveOutReset",
                     "waveOutUnprepareHeader", "waveOutClose"):
            getattr(self._winmm, name).restype = ctypes.c_uint
        self._handle = ctypes.c_void_p()
        self._done = threading.Event()
        self._header = None
        self._diagnostic = WaveOutDiagnostic()
        self._buffer = None
        self._prepared = False
        self._output_uncertain = False
        self._callback = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(self._on_message)

    def _has_valid_handle(self) -> bool:
        """Return whether WinMM populated a non-null device handle."""
        return self._handle is not None and self._handle.value not in (None, 0)

    @property
    def diagnostics(self) -> WaveOutDiagnostic:
        """Return an immutable snapshot of WinMM lifecycle diagnostics."""
        return self._diagnostic

    @property
    def diagnostic(self) -> WaveOutDiagnostic:
        """Singular compatibility alias for :attr:`diagnostics`."""
        return self._diagnostic

    def _record_diagnostic(self, **changes: object) -> None:
        self._diagnostic = replace(self._diagnostic, **changes)

    def start(self, audio: bytes) -> None:
        if self._output_uncertain:
            raise RuntimeError("waveOut_output_uncertain")
        if self._has_valid_handle():
            self._release_completed()
        if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise ValueError("invalid_wav")
        import io
        import wave
        with io.BytesIO(audio) as source:
            with wave.open(source, "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                    raise ValueError("unsupported_audio_format")
                frames = wav.getnframes()
                if not 0 < frames <= 192000:
                    raise ValueError("unsupported_audio_format")
                pcm = wav.readframes(frames)
                if len(pcm) != 2 * frames:
                    raise ValueError("invalid_audio")
        # A backend instance can be reused, so diagnostics are a per-submission
        # snapshot rather than an accumulating counter.
        self._diagnostic = WaveOutDiagnostic()
        fmt = _WaveFormatEx(_WAVE_FORMAT_PCM, 1, 24000, 48000, 2, 16, 0)
        result = self._winmm.waveOutOpen(ctypes.byref(self._handle), ctypes.c_uint(0xFFFFFFFF),
                                         ctypes.byref(fmt), ctypes.cast(self._callback, ctypes.c_void_p),
                                         0, _CALLBACK_FUNCTION)
        self._record_diagnostic(open_result=result, opened_at=time.monotonic())
        if result != _MMSYSERR_NOERROR:
            raise OSError(f"waveOutOpen:{result}")
        self._buffer = ctypes.create_string_buffer(pcm)
        self._header = _WaveHeader(ctypes.cast(self._buffer, ctypes.POINTER(ctypes.c_char)), len(pcm), 0, None, 0, 0, None, None)
        self._record_diagnostic(header_address=ctypes.addressof(self._header))
        self._done.clear()
        result = self._winmm.waveOutPrepareHeader(self._handle, ctypes.byref(self._header), ctypes.sizeof(self._header))
        self._record_diagnostic(prepare_result=result, prepared_at=time.monotonic())
        if result != _MMSYSERR_NOERROR:
            self.close()
            raise OSError(f"waveOutPrepareHeader:{result}")
        self._prepared = True
        result = self._winmm.waveOutWrite(self._handle, ctypes.byref(self._header), ctypes.sizeof(self._header))
        self._record_diagnostic(write_result=result, submitted_at=time.monotonic())
        if result != _MMSYSERR_NOERROR:
            self.close()
            raise OSError(f"waveOutWrite:{result}")

    def poll_complete(self) -> bool:
        return self._done.is_set()

    def stop(self, timeout: float) -> StopResult:
        if not self._has_valid_handle():
            return StopResult(False, "unknown", "waveOut_handle_missing_after_start")
        # Always touch the real WinMM handle first.  The explicit shim changes
        # only the post-reset classification, which keeps the device lifecycle
        # recoverable and makes the fault evidence auditable.
        result = self._winmm.waveOutReset(self._handle)
        self._record_diagnostic(reset_result=result, reset_at=time.monotonic())
        if result != _MMSYSERR_NOERROR:
            self._output_uncertain = True
            return StopResult(False, "unknown", f"waveOutReset:{result}")
        if self._fault_mode is WaveOutFaultMode.UNKNOWN_AFTER_REAL_RESET:
            self._output_uncertain = True
            return StopResult(False, "unknown", "fault_shim_after_real_waveOutReset")
        if self._fault_mode is WaveOutFaultMode.TIMEOUT_AFTER_REAL_RESET:
            self._output_uncertain = True
            # Do not wait for or promote WOM_DONE in the injected branch.
            # close() retains the backend until process exit if WinMM still
            # owns the callback/buffer.
            return StopResult(False, "timeout", "fault_shim_after_real_waveOutReset")
        if not self._done.wait(timeout):
            self._output_uncertain = True
            return StopResult(False, "timeout", "waveOut_buffer_return_timeout")
        return StopResult(True, "backend_buffer_returned", "waveOutReset")

    def finalize_completed(self, timeout: float) -> StopResult:
        """Reset and release a naturally completed waveOut submission."""
        result = self.stop(timeout)
        if not result.acknowledged:
            return result
        try:
            self._release_completed()
        except Exception as exc:
            return StopResult(False, "unknown", f"waveOut_cleanup:{type(exc).__name__}")
        return StopResult(True, "backend_buffer_returned", "waveOut_complete_cleanup")

    def _release_completed(self) -> None:
        if not self._has_valid_handle():
            return
        if not self._done.is_set():
            raise RuntimeError("waveOut_buffer_not_returned")
        if self._prepared:
            result = self._winmm.waveOutUnprepareHeader(
                self._handle, ctypes.byref(self._header), ctypes.sizeof(self._header))
            self._record_diagnostic(unprepare_result=result, unprepared_at=time.monotonic())
            if result != _MMSYSERR_NOERROR:
                self._output_uncertain = True
                raise OSError(f"waveOutUnprepareHeader:{result}")
            self._prepared = False
        result = self._winmm.waveOutClose(self._handle)
        self._record_diagnostic(close_result=result, closed_at=time.monotonic())
        if result != _MMSYSERR_NOERROR:
            self._output_uncertain = True
            raise OSError(f"waveOutClose:{result}")
        self._handle = ctypes.c_void_p()
        self._header = self._buffer = None
        self._record_diagnostic(cleanup_verified=True)

    def close(self) -> None:
        if not self._has_valid_handle():
            return
        if self._output_uncertain or not self._done.is_set():
            # Never release PCM or callback while WinMM may still reference them.
            self._output_uncertain = True
            _uncertain_waveout_backends.append(self)
            return
        self._release_completed()

    def _on_message(self, _handle, message, _instance, _param1, _param2) -> None:
        if (message == _WOM_DONE and self._header is not None
                and _param1 == ctypes.addressof(self._header)):
            self._record_diagnostic(wom_done_at=time.monotonic())
            self._done.set()


__all__ = ["CallablePlaybackBackend", "PlaybackBackend", "PlaybackEvent", "PlaybackState",
           "PlaybackTicket", "PlaybackWorker", "StopResult", "WaveOutDiagnostic",
           "WaveOutFaultMode", "WaveOutPlaybackBackend"]
