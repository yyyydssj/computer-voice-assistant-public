"""Opt-in, bounded Windows VoiceStudio playback; independent of fake TTS orchestration.

The returned device-call flag is not evidence that a person heard speech. This
module is not wired to the Agent, task ledger, authorization, or default runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import secrets
import subprocess
import struct
import threading
import sys
import urllib.request
import wave
from typing import Callable, Iterator

from app.tts.segmentation import ReplySegmenter, SegmentationConfig, SegmentationDecision, SentenceSegment, TextChunk

from app.tts.playback_worker import PlaybackState, PlaybackWorker, WaveOutPlaybackBackend

from app.tts.private_selection import VoiceSelection, verify_provider, verify_reference
from app.tts.providers import (
    PCMChunkStreamValidator,
    VoiceStudioHttpWavProvider,
    VoiceStudioSentenceRequest,
)


class RealPlaybackError(RuntimeError):
    """Unsafe synthesis, output format or playback state; keep original text."""


@dataclass(frozen=True, slots=True)
class PreviewResult:
    text: str
    received_audio: int
    playback_calls_returned: int
    text_fallback: bool
    error_code: str | None = None
    # Only the user can confirm listening; never infer it from a device call.
    user_heard: bool | None = None
    playback_state: PlaybackState | None = None
    stop_confirmation: str | None = None
    voice_disabled: bool = False
    # These facts are scoped to the playback backend protocol.  They do not
    # assert that a physical transducer stopped or that a person heard silence.
    stop_status: str | None = None
    stopped_acknowledged: bool = False
    stop_unconfirmed: bool = False


def _split_sentences(text: str) -> tuple[str, ...]:
    if type(text) is not str or not text or len(text) > 180 or "[" in text or "]" in text or any(ord(c) < 32 and c != "\n" for c in text):
        raise RealPlaybackError("invalid_text")
    endings = frozenset("。！？；.!?;\n")
    sentences: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in endings:
            if current.strip():
                sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())
    if not sentences or len(sentences) > 3 or any(len(part) > 80 for part in sentences):
        raise RealPlaybackError("invalid_segments")
    return tuple(sentences)


def _generate(segment: str, selection: VoiceSelection, *, timeout: int = 18) -> bytes:
    boundary = "----AssistantVoice" + secrets.token_hex(12)
    fields = (("text", segment), ("profile_id", selection.profile_id),
              ("language", "Chinese"), ("speed", str(selection.speed)), ("stream", "false"))
    body = b"".join(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode("utf-8")
        for key, value in fields
    ) + f"--{boundary}--\r\n".encode("ascii")
    request = urllib.request.Request(
        "http://127.0.0.1:3900/generate", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary, "Accept": "audio/wav"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200 or response.headers.get_content_type() != "audio/wav":
            raise RealPlaybackError("invalid_provider_response")
        audio = response.read(500_001)
    if len(audio) > 500_000:
        raise RealPlaybackError("audio_too_large")
    return audio


def _checked_wave(data: bytes) -> tuple[bytes, int]:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RealPlaybackError("invalid_audio")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            channels, width, rate, frames = (source.getnchannels(), source.getsampwidth(),
                                             source.getframerate(), source.getnframes())
            if channels != 1 or width != 2 or rate != 24000 or not 0 < frames <= 192000:
                raise RealPlaybackError("unsupported_audio_format")
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2 or not any(pcm) or (not any(pcm[::2]) and not any(pcm[1::2])):
                raise RealPlaybackError("invalid_audio")
            return pcm, frames
    except (EOFError, ValueError, wave.Error) as exc:
        raise RealPlaybackError("invalid_audio") from exc


def _wave_with_leading_pause(data: bytes, pause_ms: int, *, fade_ms: int = 0) -> bytes:
    pcm, frames = _checked_wave(data)
    if fade_ms not in (0, 80):
        raise RealPlaybackError("unsupported_preview_fade")
    if fade_ms:
        # Fade only the final 80 ms of the checked 24 kHz PCM16 audio; never
        # alter the leading silence used for the existing inter-sentence pause.
        samples = min(frames, 24000 * fade_ms // 1000)
        output_pcm = bytearray(pcm)
        for index in range(samples):
            offset = (frames - samples + index) * 2
            value = struct.unpack_from("<h", pcm, offset)[0]
            gain = (samples - 1 - index) / max(samples - 1, 1)
            struct.pack_into("<h", output_pcm, offset, round(value * gain))
        pcm = bytes(output_pcm)
    if pause_ms:
        pcm = b"\x00\x00" * (24000 * pause_ms // 1000) + pcm
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(pcm)
    return output.getvalue()


def _play_windows(data: bytes) -> None:
    """Legacy bounded output port; child return is not a stop acknowledgement."""
    if sys.platform != "win32":
        raise RealPlaybackError("windows_output_required")
    # Keep this compatibility port intentionally non-cancellable. A child exit
    # or timeout is never promoted to a hardware stop ACK; the caller disables
    # voice on any uncertain device outcome.
    program = ("import sys,winsound; "
               "audio=sys.stdin.buffer.read(500001); "
               "assert len(audio)<=500000; "
               "winsound.PlaySound(audio,winsound.SND_MEMORY|winsound.SND_NODEFAULT)")
    try:
        subprocess.run([sys.executable, "-B", "-c", program], input=data, timeout=12,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    except Exception as exc:
        raise RealPlaybackError("device_stop_unconfirmed") from exc


class RealVoicePreview:
    def __init__(self, selection: VoiceSelection,
                 *, synthesizer: Callable[[str, VoiceSelection], bytes] = _generate,
                 player: Callable[[bytes], None] = _play_windows,
                 worker: PlaybackWorker | None = None,
                 verifier: Callable[[VoiceSelection], None] | None = None,
                 fade_ms: int | None = None) -> None:
        selection.validate()
        selected_fade_ms = selection.fade_ms if fade_ms is None else fade_ms
        if selected_fade_ms not in (0, 80):
            raise RealPlaybackError("unsupported_preview_fade")
        self.fade_ms = selected_fade_ms
        self.selection = selection
        self.synthesizer = synthesizer
        self.player = player
        # The real Windows path owns one long-lived worker.  Injected players
        # remain a lightweight test/compatibility port unless the caller also
        # supplies a worker with an explicit stop protocol.
        self._owns_worker = worker is None and player is _play_windows
        self.worker = worker or (PlaybackWorker(WaveOutPlaybackBackend) if self._owns_worker else None)
        self.verifier = verifier or _verify_selection
        self._busy = False
        self._disabled = False
        self._closed = False
        self._cancelled = threading.Event()
        self._active_ticket = None
        self._lock = threading.Lock()

    @property
    def active_ticket(self):
        """The currently submitted ticket, if any; backend-scoped only."""
        with self._lock:
            return self._active_ticket

    def cancel(self):
        """Request cancellation and synchronously obtain the worker stop fact.

        ``PlaybackState.STOP_ACKED`` means the injected/backend stop protocol
        acknowledged return/reset.  It is deliberately not a hardware or
        human-audibility acknowledgement.  A timeout/unknown/failed state is
        reported as ``stop_unconfirmed`` and disables this preview instance.
        """
        self._cancelled.set()
        with self._lock:
            worker = self.worker
            ticket = self._active_ticket
        if worker is None or ticket is None:
            return None
        state = worker.stop(ticket)
        if state in (PlaybackState.STOP_TIMEOUT, PlaybackState.UNKNOWN, PlaybackState.FAILED):
            self._disabled = True
        return state

    def close(self) -> None:
        """Permanently close this preview without claiming audible silence.

        ``close`` is idempotent. It records cancellation and requests a
        best-effort backend stop for an active ticket; only a preview-owned
        worker is then closed. A caller-owned injected worker remains open,
        but this preview itself is fail-closed and cannot speak again.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancelled.set()
            worker = self.worker
            ticket = self._active_ticket
            owns_worker = self._owns_worker
        if worker is not None and ticket is not None:
            try:
                state = worker.stop(ticket)
                if state in (PlaybackState.STOP_TIMEOUT, PlaybackState.UNKNOWN, PlaybackState.FAILED):
                    with self._lock:
                        self._disabled = True
            except Exception:
                with self._lock:
                    self._disabled = True
        if owns_worker and worker is not None:
            worker.close()

    def speak(self, text: str) -> PreviewResult:
        with self._lock:
            worker_unavailable = self.worker is not None and self.worker.disabled
            if self._closed or self._busy or self._disabled or worker_unavailable:
                return PreviewResult(
                    text, 0, 0, True, "preview_unavailable",
                    voice_disabled=self._closed or self._disabled or worker_unavailable,
                )
            self._busy = True
            self._cancelled.clear()
        received = completed = 0
        last_state: PlaybackState | None = None
        confirmation: str | None = None
        try:
            sentences = _split_sentences(text)
            # A cancellation observed before provider work must not issue any
            # provider or device call.
            if self._cancelled.is_set():
                raise RealPlaybackError("cancelled_before_submission")
            # Pre-generate all short sentences before playing the first one:
            # otherwise network synthesis latency becomes an uncontrolled gap.
            prepared: list[bytes] = []
            for index, sentence in enumerate(sentences):
                if self._cancelled.is_set():
                    raise RealPlaybackError("cancelled_before_submission")
                self.verifier(self.selection)
                if self._cancelled.is_set():
                    raise RealPlaybackError("cancelled_before_submission")
                data = self.synthesizer(sentence, self.selection)
                audio = _wave_with_leading_pause(data, self.selection.pause_ms if index else 0,
                                                 fade_ms=self.fade_ms)
                received += 1
                prepared.append(audio)
            # No new synthesis after submitting to the output device.
            for audio in prepared:
                if self._cancelled.is_set():
                    raise RealPlaybackError("cancelled_before_submission")
                try:
                    if self.worker is not None:
                        ticket = self.worker.submit(audio)
                        with self._lock:
                            self._active_ticket = ticket
                            cancellation_raced_with_submit = self._cancelled.is_set()
                        # Close the cancel-before-worker-activation race: a
                        # cancellation may have happened between the precheck
                        # and submit, before the worker became active.
                        if cancellation_raced_with_submit:
                            state = self.worker.stop(ticket)
                        else:
                            state = self.worker.wait(ticket, timeout=13.0)
                            if not ticket.terminal:
                                # A timed-out wait is not a completion fact.
                                # Request bounded stop before losing the ticket.
                                state = self.worker.stop(ticket)
                        with self._lock:
                            if self._active_ticket is ticket:
                                self._active_ticket = None
                        last_state = state
                        if ticket.events:
                            confirmation = ticket.events[-1].confirmation
                        if state is PlaybackState.STOP_ACKED:
                            raise RealPlaybackError("stopped_acknowledged")
                        if state is not PlaybackState.COMPLETED:
                            self._disabled = True
                            raise RealPlaybackError("stop_unconfirmed")
                    else:
                        self.player(audio)
                except RealPlaybackError:
                    raise
                except Exception:
                    self._disabled = True
                    raise RealPlaybackError("device_stop_unconfirmed") from None
                completed += 1
                if self._cancelled.is_set():
                    # A completed ticket has no active backend stop to ACK; the
                    # cancellation still prevents any later sentence submit.
                    raise RealPlaybackError("cancelled_after_current_playback")
            return PreviewResult(text, received, completed, False,
                                 playback_state=last_state, voice_disabled=self._disabled)
        except RealPlaybackError as exc:
            code = str(exc)
            if code in {"stop_unconfirmed", "device_stop_unconfirmed",
                        "provider_or_device_unavailable"}:
                self._disabled = True
            if code == "stopped_acknowledged":
                return PreviewResult(text, received, completed, True, code,
                                      stop_status="stopped_acknowledged",
                                      stopped_acknowledged=True, playback_state=last_state,
                                      stop_confirmation=confirmation, voice_disabled=self._disabled)
            if code in {"stop_unconfirmed", "device_stop_unconfirmed"}:
                return PreviewResult(text, received, completed, True, code,
                                      stop_status="stop_unconfirmed",
                                      stop_unconfirmed=True, playback_state=last_state,
                                      stop_confirmation=confirmation, voice_disabled=self._disabled)
            return PreviewResult(text, received, completed, True, code)
        except Exception:
            # Never print text, audio, HTTP response body, or credentials.
            return PreviewResult(text, received, completed, True, "provider_or_device_unavailable",
                                 playback_state=last_state, voice_disabled=self._disabled)
        finally:
            with self._lock:
                self._active_ticket = None
                self._busy = False


class RealtimeVoiceSession:
    """Bounded sentence-at-a-time VoiceStudio playback for Agent text chunks.

    Text ingestion is deliberately non-blocking with respect to synthesis/playback;
    a small condition-backed queue provides backpressure while preserving sentence
    order.  This is a listening-path optimization only and does not create P7
    user-heard or physical-speaker evidence.
    """

    def __init__(self, selection: VoiceSelection, *,
                 synthesizer: Callable[[str, VoiceSelection], bytes] = _generate,
                 player: Callable[[bytes], None] = _play_windows,
                 worker: PlaybackWorker | None = None,
                 verifier: Callable[[VoiceSelection], None] | None = None,
                 provider: object | None = None,
                 provider_mode: str = "legacy",
                 max_pending_segments: int = 8) -> None:
        selection.validate()
        if max_pending_segments <= 0:
            raise ValueError("max_pending_segments must be positive")
        if provider_mode not in {"legacy", "http_wav"}:
            raise ValueError("provider_mode must be legacy or http_wav")
        if provider_mode == "http_wav" and provider is None:
            provider = VoiceStudioHttpWavProvider()
        if provider_mode == "http_wav" and not hasattr(provider, "stream_sentence"):
            raise TypeError("http_wav provider must implement stream_sentence")
        self.selection = selection
        self.synthesizer = synthesizer
        self.player = player
        self.verifier = verifier or _verify_selection
        self.provider = provider
        self.provider_mode = provider_mode
        self.worker = worker or (PlaybackWorker(WaveOutPlaybackBackend) if player is _play_windows else None)
        self._owns_worker = worker is None and self.worker is not None
        self._max_pending = max_pending_segments
        self._condition = threading.Condition()
        self._pending: list[bytes] = []
        self._segments: list[str] = []
        self._last_chunk: str | None = None
        self._sequence = 0
        self._reply_id = "realtime-" + secrets.token_hex(8)
        self._provider_generation = 1
        self._provider_request_sequence = 0
        self._segmenter = ReplySegmenter(
            reply_id=self._reply_id,
            config=SegmentationConfig(
                min_segment_characters=4,
                max_segment_characters=48,
                max_queued_segments=8,
                soft_target_characters=32,
                soft_search_min_characters=24,
                soft_search_max_characters=40,
            ),
        )
        self._finished = False
        self._cancelled = False
        self._overloaded = False
        self._cancel_event = threading.Event()
        self._error: Exception | None = None
        self._received = 0
        self._played = 0
        self._thread = threading.Thread(target=self._run, name="realtime-voice", daemon=True)
        self._thread.start()

    @property
    def error(self) -> Exception | None:
        with self._condition:
            return self._error

    @property
    def overloaded(self) -> bool:
        """True once the pending bound was hit and remaining speech was dropped."""

        with self._condition:
            return self._overloaded

    def push_text_chunk(self, text: str) -> None:
        if type(text) is not str or not text:
            return
        with self._condition:
            if self._finished or self._cancelled or self._overloaded:
                return
            self._submit(text, is_final=False)
            self._condition.notify_all()

    def finish(self) -> tuple[str, ...]:
        with self._condition:
            if not self._finished:
                result = self._segmenter.finalize()
                if result.decision is SegmentationDecision.QUEUE_BUDGET_EXCEEDED:
                    raise RealPlaybackError("realtime_queue_budget_exceeded")
                if result.accepted:
                    if result.segments:
                        self._segmenter.dequeue(len(result.segments))
                    self._enqueue_segments(result.segments)
                self._finished = True
                self._condition.notify_all()
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            self.cancel()
            self._thread.join(timeout=2)
        if self._error is not None:
            raise self._error
        return tuple(self._segments)

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._cancel_event.set()
            self._finished = True
            self._pending.clear()
            self._condition.notify_all()
        if self.worker is not None:
            try:
                self.worker.close()
            except Exception:
                pass
        if self.provider is not None:
            close = getattr(self.provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def close(self) -> None:
        self.cancel()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _submit(self, text: str, *, is_final: bool) -> None:
        self._sequence += 1
        result = self._segmenter.submit(TextChunk(
            chunk_id=f"chunk-{self._sequence}", reply_id=self._reply_id,
            sequence=self._sequence, text=text, is_final=is_final,
        ))
        if result.decision is SegmentationDecision.QUEUE_BUDGET_EXCEEDED:
            raise RealPlaybackError("realtime_queue_budget_exceeded")
        if result.accepted:
            # Release admission-control entries before moving them into the
            # bounded worker queue. Never temporarily exceed the pending cap.
            if result.segments:
                self._segmenter.dequeue(len(result.segments))
            self._enqueue_segments(result.segments)

    def _enqueue_segments(self, segments: tuple[SentenceSegment, ...]) -> None:
        """Accept segments without ever blocking the caller (Agent) thread.

        The pending queue is a hard bound. When it is full the reply's remaining
        speech is dropped, the complete text stays available, and the overload is
        recorded instead of stalling the Agent turn on TTS.
        """
        if self._cancelled:
            return
        # One Agent push is admitted as a batch (the segmenter already bounds a
        # single batch). The pending bound decides whether *further* pushes are
        # accepted, never whether the current reply is silently truncated, and
        # the caller thread is never made to wait on TTS.
        self._pending.extend(segment.text for segment in segments)
        if len(self._pending) > self._max_pending:
            self._overloaded = True
        self._condition.notify_all()

    def _synthesize_http_wav(self, sentence: str) -> bytes:
        self._provider_request_sequence += 1
        request = VoiceStudioSentenceRequest(
            reply_id=self._reply_id,
            segment_id=f"segment-{self._provider_request_sequence}",
            provider_request_id=f"http-wav-{self._reply_id}-{self._provider_request_sequence}",
            provider_generation=self._provider_generation,
            text=sentence,
            profile_id=self.selection.profile_id,
            language="Chinese",
            speed=self.selection.speed,
        )
        if self._cancel_event.is_set():
            raise RealPlaybackError("realtime_cancelled")
        validator = PCMChunkStreamValidator(request)
        chunks = self.provider.stream_sentence(request, cancelled=self._cancel_event)  # type: ignore[union-attr]
        pcm = bytearray()
        for chunk in chunks:
            if self._cancel_event.is_set():
                validator.cancel()
                raise RealPlaybackError("realtime_cancelled")
            validator.accept(chunk)
            pcm.extend(chunk.data)
        if self._cancel_event.is_set():
            validator.cancel()
            raise RealPlaybackError("realtime_cancelled")
        validator.finish()
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(24000)
            target.writeframes(bytes(pcm))
        return _wave_with_leading_pause(output.getvalue(), self.selection.pause_ms if self._played else 0, fade_ms=self.selection.fade_ms)

    def _run(self) -> None:
        try:
            index = 0
            while True:
                with self._condition:
                    while not self._pending:
                        if self._cancelled or self._finished or self._overloaded:
                            return
                        self._condition.wait(timeout=0.1)
                    sentence = self._pending.pop(0)
                    self._condition.notify_all()
                self.verifier(self.selection)
                if self._cancel_event.is_set():
                    return
                if self.provider_mode == "http_wav":
                    audio = self._synthesize_http_wav(sentence)
                else:
                    audio = _wave_with_leading_pause(
                        self.synthesizer(sentence, self.selection),
                        self.selection.pause_ms if index else 0,
                        fade_ms=self.selection.fade_ms,
                    )
                if self._cancel_event.is_set():
                    return
                self._received += 1
                if self.worker is not None:
                    ticket = self.worker.submit(audio)
                    state = self.worker.wait(ticket, timeout=13.0)
                    if state is not PlaybackState.COMPLETED:
                        raise RealPlaybackError("realtime_playback_failed")
                else:
                    self.player(audio)
                self._segments.append(sentence)
                self._played += 1
                index += 1
        except Exception as exc:
            with self._condition:
                self._error = exc
                self._cancelled = True
                self._pending.clear()
                self._condition.notify_all()


def _verify_selection(selection: VoiceSelection) -> None:
    verify_reference(selection)
    verify_provider(selection)





