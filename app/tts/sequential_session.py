"""Bounded-prefetch sequential speech channel (``tts_mode=pcm_seq``).

Why this module exists (real-machine fact, 2026-10-03): the deployed ``ws_pcm``
channel goes through :class:`app.tts.streaming_session.StreamingVoiceSession`
(segment queue + ``_overloaded`` + cancel/end-frame semantics) and on the real
machine a 394-character reply produced ``pushed=367 -> dequeued=10 (1 segment) ->
played=10`` with **no** drops, **no** late events, no cancellation, no overload
and no error. That structure still holds a defect we have not exhausted, so the
playback implementation is replaced here — while keeping first-audio latency at
least as good.

The design is the simplest one that satisfies both hard requirements:

* **Never slower.** The whole reply text is already in memory when the first push
  arrives, so synthesis is *prefetched*: while sentence *i* is being played the
  synthesis thread is already rendering sentence *i+1* (and *i+2*). It is never
  "synthesize a sentence -> speak it -> synthesize the next one".
* **Never a silent tail.** The playback loop only advances when the sentence's
  ticket reaches :attr:`PlaybackState.COMPLETED` — the only reliable "the device
  really finished this audio" signal in this repository, produced by
  ``WaveOutPCMStreamBackend.poll_drained()`` (WOM_DONE). A failure, an
  incompatible state or an expiry raises :class:`SequentialVoiceSessionError`
  with a fixed short code, so the caller records
  ``speech_failed:<ExceptionTypeName>`` and ``sentences_played <
  sentences_total`` stays visible. "Reported success while speaking less" is not
  representable: :meth:`SequentialVoiceSession.finish` raises unless
  ``sentences_played == sentences_synthesized == sentences_total``.

Two threads own the work and nothing else does:

```text
push_text_chunk(text) --> [text segmenter] --> task queue (sentence strings)
                                                   |
                                          synthesis thread (1)
                                                   |
                              published queue, capacity K=2 (never drops)
                                                   |
                                            playback thread (1)
                                                   |
                            PCMPlaybackWorker.begin_sentence / push_chunk
                                                   |
                            ticket.wait() is PlaybackState.COMPLETED ?
```

Unlike the deployed session, a sentence is **published while it is still being
synthesized**: the synthesis thread puts the sentence's handle into the published
queue and opens the sentence on the worker immediately, then keeps appending PCM
blocks to that handle. The playback thread therefore hands the first blocks to
the device as soon as they exist (the worker's start-buffer gate opens during
synthesis) instead of waiting for the whole sentence, which is what keeps the gap
between two sentences at the device level and not at the channel level.

Nothing in this module reads the environment, chooses a provider, opens a device
or installs itself into the default runtime: the transport router, the playback
worker and the clock are all injected, and no existing ``tts_mode`` behaviour is
touched. Only the read-only facts listed on :class:`SequentialVoiceSession` leave
this module.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from threading import Event, Thread
from typing import Callable

from .clock import monotonic_seconds
from .pcm_playback import PCMPlaybackWorker
from .playback_worker import PlaybackState, PlaybackTicket
from .providers.contracts import PCMChunk, VoiceStudioSentenceRequest
from .streaming_session import DEFAULT_STALL_TIMEOUT_S
from .transport import VoiceStudioTransportRouter


SENTENCE_ENDERS = "。！？!?；;…\n"
"""Characters that end a *real* sentence; a sentence closes right after one."""

SECONDARY_BREAKS = "，、,：:）) "
"""Break candidates used only to keep one sentence below the character ceiling."""

MAX_SENTENCE_CHARACTERS = 48
"""Hard character ceiling of one synthesis request in this channel. VoiceStudio
long-reply requests are kept below the local provider's contract threshold.

A buffer that reaches this length without a real sentence end is cut after its
last :data:`SECONDARY_BREAKS` character inside the buffer; a buffer with no
secondary break at all is cut at the ceiling itself. The pieces stay adjacent:
``"".join(pieces) == text`` always, character for character, with nothing added
and nothing removed.
"""

PROVIDER_CHUNK_MS = 100
"""Nominal audio duration of one produced PCM block, in milliseconds.

Used only for the read-only queue-budget sanity check and by the offline tests;
the real playback budget is configured by the assembler.
"""

SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000
"""PCM16 mono byte rate, mirroring :mod:`app.tts.pcm_playback`."""

DEFAULT_SYNTHESIS_WAIT_S = 20.0
"""Bounded wait of the synthesis thread for room / for the next block."""

DEFAULT_PLAYBACK_TIMEOUT_S = 30.0
"""Bounded wait of the playback thread for one ticket to reach COMPLETED."""

DEFAULT_JOIN_TIMEOUT_S = 40.0
"""Bounded join of **one** thread inside :meth:`SequentialVoiceSession.finish`.

P1：它只描述单线程预算，**不再**是整条回复的收尾上限——收尾判据改看
:data:`app.tts.streaming_session.DEFAULT_STALL_TIMEOUT_S`（连续无进展才放弃）。
"""

DEFAULT_WAKE_INTERVAL_S = 0.02
"""Poll interval of the condition waits (never the primary wakeup path)."""

_MIN_WAIT_SLICE_S = 0.05
"""Smallest slice handed to the playback worker's ``wait`` while polling for COMPLETED.

It only exists so the deadline arithmetic can never hand a non-positive timeout to
the worker (which would make the wait return immediately and turn this loop into a
spin); the loop itself is driven by the ticket going terminal, never by the slice.
"""

SEQUENTIAL_ERROR_CODES = (
    "pcm_seq_synthesis_failed",
    "pcm_seq_synthesis_timeout",
    "pcm_seq_synthesis_stalled",
    "pcm_seq_playback_failed",
    "pcm_seq_playback_not_completed",
    "pcm_seq_no_audio",
    "pcm_seq_join_timeout",
    "pcm_seq_tail_not_played",
    "pcm_seq_state_error",
)
"""Every fixed short code this channel can raise; pinned by a test."""


def split_sentences(text: str, *, max_chars: int = MAX_SENTENCE_CHARACTERS) -> tuple[str, ...]:
    """Split ``text`` into synthesis-sized sentence pieces, character-exact.

    Rules, in this order:

    1. a real sentence end (:data:`SENTENCE_ENDERS`) closes the current piece
       immediately, so a piece never spans two sentences;
    2. once the buffer reaches ``max_chars`` without a real sentence end, it is cut
       after its last :data:`SECONDARY_BREAKS` character (that break character
       stays with the earlier piece);
    3. a buffer with no secondary break at all is cut at ``max_chars`` characters.

    Guarantees: pure function; ``"".join(result) == text`` for every input; the
    pieces keep their original order; no piece is empty; every piece is at most
    ``max_chars + 1`` characters (the ``+1`` is a trailing real sentence end,
    which is never split away from its sentence).

    An empty or non-string input returns ``()``: there is nothing to speak and
    therefore nothing that could be lost.
    """

    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars 必须是正整数")
    if type(text) is not str or not text:
        return ()
    pieces: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in SENTENCE_ENDERS:
            pieces.append(buffer)
            buffer = ""
            continue
        if len(buffer) >= max_chars:
            cut = 0
            for candidate in SECONDARY_BREAKS:
                position = buffer.rfind(candidate)
                if position >= 0 and position + 1 > cut:
                    cut = position + 1
            if cut:
                pieces.append(buffer[:cut])
                buffer = buffer[cut:]
            else:
                pieces.append(buffer)
                buffer = ""
    if buffer:
        pieces.append(buffer)
    return tuple(pieces)


def minimum_pcm_queue_ms(max_sentence_characters: int = MAX_SENTENCE_CHARACTERS) -> int:
    """Audio budget one sentence may need, in milliseconds, rounded up.

    The published queue holds at most :attr:`SequentialVoiceSession.READY_QUEUE_CAPACITY`
    sentences, and the *first* one is submitted while the second is still being
    synthesized, so at most two sentences of PCM can ever be in the worker's queue
    at once. The assembler must configure ``max_queued_audio_ms`` at or above this
    value: :class:`PCMPlaybackWorker` refuses (``PCMPlaybackBackpressure``) audio
    that does not fit its queue, and a budget below one sentence would turn the
    device queue itself into a truncation source.
    """

    if type(max_sentence_characters) is not int or max_sentence_characters <= 0:
        raise ValueError("max_sentence_characters 必须是正整数")
    blocks = max_sentence_characters + 1
    return (blocks * PROVIDER_CHUNK_MS) * SequentialVoiceSession.READY_QUEUE_CAPACITY


@dataclass(slots=True)
class _PublishedSentence:
    """One sentence handed to the playback thread while it is still being rendered.

    The playback thread may read :attr:`chunks` as soon as the handle appears in
    the published queue; :attr:`complete` is set by the synthesis thread once the
    provider stream ended, so the playback thread knows that no further blocks are
    coming and it may wait for the ticket instead of waiting for PCM. ``published``
    is the exactly-once latch of the publish point and never leaves this module.
    """

    text: str
    index: int
    request: VoiceStudioSentenceRequest
    chunks: list[PCMChunk] = field(default_factory=list)
    complete: bool = False
    produced: bool = False
    published: bool = False


class SequentialVoiceSessionError(RuntimeError):
    """Sequential synthesis or playback failed; the original text stays intact.

    ``code`` is always one of :data:`SEQUENTIAL_ERROR_CODES`: a fixed short code
    with no text, no path and no URL. ``type(self).__name__`` is what the caller
    records as ``speech_failed:<ExceptionTypeName>``.
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}:{detail}")


class SequentialVoiceSession:
    """Prefetch-synthesis + serial-playback speech channel for one reply.

    Used exactly like the deployed streaming session: :meth:`push_text_chunk` with
    the whole reply (or with several pieces), :meth:`finish` to flush and report,
    :meth:`cancel` to stop. ``finish()`` returns the tuple of spoken sentence
    texts, so the existing ``SessionStream`` adapter in
    ``app/runtime/voice_entry.py`` reads it unchanged.

    Read-only facts (flat ``str``/``int``/``None`` values only):

    ==========================  ==================================================
    ``sentences_total``         sentences the segmenter produced for this reply
    ``sentences_synthesized``   sentences whose provider produced >= 1 PCM block
    ``sentences_played``        sentences whose ticket reached ``COMPLETED``
    ``text_length``             characters ever pushed on this session
    ``played_chars``            characters of the played sentences
    ``played_characters``       alias of ``played_chars`` (same number)
    ``lookahead_peak``          high-water mark of the published queue (bound: 2)
    ``synth_wait_ms``           synthesis thread's total wait for queue room, ms
    ``play_wait_ms``            playback thread's total wait for PCM/ticket, ms
    ``error``                   recorded exception type name, or ``None``
    ``cancelled``               ``cancel()`` was called
    ``dropped_pushes``          pushes refused by the terminal-state guard
    ==========================  ==================================================

    The two timing facts are whole milliseconds on the injected clock (which
    defaults to the shared high-resolution ``monotonic_seconds``).
    """

    #: Capacity of the "published, waiting to be played" queue. It is a **waiting**
    #: bound and never a drop point: the synthesis thread blocks until the playback
    #: thread takes a sentence, and a sentence is only ever discarded by an explicit
    #: failure or by :meth:`cancel`.
    READY_QUEUE_CAPACITY = 2

    def __init__(
        self,
        selection: object,
        *,
        transport_router: VoiceStudioTransportRouter,
        worker: PCMPlaybackWorker,
        language: str = "Chinese",
        clock: Callable[[], float] = monotonic_seconds,
        synthesis_wait_s: float = DEFAULT_SYNTHESIS_WAIT_S,
        playback_timeout_s: float = DEFAULT_PLAYBACK_TIMEOUT_S,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
        stop_timeout_s: float = 1.0,
        max_sentence_characters: int = MAX_SENTENCE_CHARACTERS,
        wake_interval_s: float = DEFAULT_WAKE_INTERVAL_S,
    ) -> None:
        if not isinstance(transport_router, VoiceStudioTransportRouter):
            raise TypeError("transport_router 必须是 VoiceStudioTransportRouter")
        missing = [
            name
            for name in ("begin_sentence", "push_chunk", "wait", "stop")
            if not callable(getattr(worker, name, None))
        ]
        if missing:
            # Duck-typed on purpose, exactly like ``edge_provider`` in the deployed
            # streaming session: any object that really offers the playback-worker
            # protocol is accepted, which is what keeps this channel offline
            # testable without weakening a single runtime guarantee.
            raise TypeError(
                "worker 必须提供 begin_sentence/push_chunk/wait/stop，缺少：" + ",".join(missing)
            )
        if type(language) is not str or not language:
            raise ValueError("language 必须是非空字符串")
        for name, value in (
            ("synthesis_wait_s", synthesis_wait_s),
            ("playback_timeout_s", playback_timeout_s),
            ("join_timeout_s", join_timeout_s),
            ("stall_timeout_s", stall_timeout_s),
            ("stop_timeout_s", stop_timeout_s),
            ("wake_interval_s", wake_interval_s),
        ):
            if type(value) not in {int, float} or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} 必须为正数")
        if type(max_sentence_characters) is not int or max_sentence_characters <= 0:
            raise ValueError("max_sentence_characters 必须是正整数")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        validate = getattr(selection, "validate", None)
        if callable(validate):
            validate()
        self.selection = selection
        self._transport_router = transport_router
        self._worker = worker
        self._language = language
        self._clock = clock
        self._synthesis_wait_s = float(synthesis_wait_s)
        self._playback_timeout_s = float(playback_timeout_s)
        self._join_timeout_s = float(join_timeout_s)
        # P1：``join_timeout_s`` 只是单线程预算；整条回复的收尾判据是「连续无进展」。
        self._stall_timeout_s = float(stall_timeout_s)
        self._stop_timeout_s = float(stop_timeout_s)
        self._max_sentence_characters = int(max_sentence_characters)
        self._wake_interval_s = float(wake_interval_s)
        self._condition = threading.Condition()
        self._cancelled = Event()
        self._reply_id = "pcmseq" + secrets.token_hex(8)
        # --- work handoff -----------------------------------------------------
        self._tasks: list[str] = []
        self._pending_text = ""
        self._finished = False
        self._sequence = 0
        self._sequence_consumed = 0
        self._published: list[_PublishedSentence] = []
        self._inflight: _PublishedSentence | None = None
        self._synthesis_done = False
        self._play_done = False
        self._played_order: list[_PublishedSentence] = []
        # --- read-only facts --------------------------------------------------
        self._sentences_total = 0
        self._sentences_synthesized = 0
        self._sentences_played = 0
        self._played_chars = 0
        self._text_length = 0
        self._lookahead_peak = 0
        self._synth_wait_seconds = 0.0
        self._play_wait_seconds = 0.0
        self._error: Exception | None = None
        self._provider_error_code: str | None = None
        self._failed_sentence_index: int | None = None
        self._failed_request_chars: int | None = None
        self._failed = False
        self._user_cancelled = False
        self._dropped_pushes = 0
        # P1 收尾进展事实（只读，不参与任何判定）：
        self._drain_seconds: float | None = None
        self._last_progress: str | None = None
        self._synth_thread = Thread(target=self._synthesize_loop, name="pcm-seq-synth", daemon=True)
        self._play_thread = Thread(target=self._play_loop, name="pcm-seq-play", daemon=True)
        self._synth_thread.start()
        self._play_thread.start()

    # ------------------------------------------------------------------ public

    @property
    def reply_id(self) -> str:
        return self._reply_id

    def push_text_chunk(self, text: str) -> None:
        """Admit one Agent push; it is segmented and prefetched immediately.

        An empty or non-string push carries nothing to speak and is ignored. A
        push arriving after ``cancel()``/``finish()``/a recorded failure is refused
        by the terminal-state guard, counted in ``dropped_pushes`` and never
        silently swallowed.
        """

        if type(text) is not str or not text:
            return
        with self._condition:
            if self._finished or self._failed or self._cancelled.is_set():
                self._dropped_pushes += 1
                return
            self._pending_text += text
            self._text_length += len(text)
            pieces = split_sentences(self._pending_text, max_chars=self._max_sentence_characters)
            if pieces and not self._pending_text.endswith(tuple(SENTENCE_ENDERS)):
                # The last piece may still be an unterminated sentence: hold it back
                # until a real sentence end, the ceiling or ``finish()`` arrives.
                self._pending_text = pieces[-1]
                pieces = pieces[:-1]
            else:
                self._pending_text = ""
            for piece in pieces:
                if not piece.strip():
                    # 真机证据（2026-10-09，turn=2，`pcm_seq`）：回复里的空行会被切成**纯空白
                    # 碎片**（换行本身就是一个独立碎片），本地契约在构造请求时就以 `text_blank`
                    # 拒绝它并中止整条播报——真机 294 字回复只播出 1/13 段，用户只听到第一句。
                    # 空白碎片不含任何可播内容，直接不入队：默认通道 `ws_pcm` 的
                    # `ReplySegmenter` 早就把这种块判为 `EMPTY_TEXT` 丢掉，这条通道此前漏了
                    # 同一层保护。`split_sentences` 的"字符精确"不变量保持不变。
                    continue
                self._sequence += 1
                self._tasks.append(piece)
            self._sentences_total = self._sequence
            self._condition.notify_all()

    def finish(self) -> tuple[str, ...]:
        """Flush the tail, wait for both threads, then report the spoken sentences.

        Returns the spoken sentence texts in playback order. Raises the recorded
        :class:`SequentialVoiceSessionError` when anything failed, timed out or did
        not reach ``PlaybackState.COMPLETED``.

        P1：等待判据是「**连续 ``stall_timeout_s`` 毫无进展**」，不再是「总时长超过
        ``join_timeout_s``」。慢但在推进的长回复不再被判 ``pcm_seq_join_timeout``；
        真的卡住仍然是**显式失败**（口径没有放松）。

        On a normal return the three counts are equal —
        ``sentences_played == sentences_synthesized == sentences_total`` — so a
        reply that spoke less than it accepted cannot look like a success. A
        user-initiated :meth:`cancel` is a designed stop, not a failure, and
        returns the sentences that did reach COMPLETED.
        """

        with self._condition:
            if not self._finished and not self._failed:
                tail = self._pending_text
                self._pending_text = ""
                if tail.strip():
                    # 与 `push_text_chunk` 同一判据：纯空白尾部（例如回复以空行结尾）
                    # 没有任何可播内容，绝不为此构造一次 `text_blank` 请求。
                    self._sequence += 1
                    self._tasks.append(tail)
                self._sentences_total = self._sequence
                self._finished = True
                self._condition.notify_all()
        elapsed, stalled = self._await_drain()
        with self._condition:
            self._drain_seconds = elapsed
            cancelled = self._cancelled.is_set()
            user_cancelled = self._user_cancelled
            error = self._error
            played = tuple(item.text for item in self._played_order)
            synthesised = self._sentences_synthesized
            total = self._sentences_total
            synth_alive = self._synth_thread.is_alive()
            play_alive = self._play_thread.is_alive()
            if stalled:
                self._last_progress = self._progress_stage_locked()
            last_progress = self._last_progress
        if synth_alive or play_alive:
            # P1：走到这里意味着收尾**连续无进展**（而不是"总时长到了"）。语义仍然
            # 是**显式失败**——绝不把"没播完"当成功；只是 detail 换成停滞的线程与最后进展
            # （固定短码，不含任何文本），让原因可按图索骥。
            stalled_on = (
                "both" if synth_alive and play_alive else "synth" if synth_alive else "play"
            )
            raise self._fatal(
                SequentialVoiceSessionError(
                    "pcm_seq_join_timeout",
                    "stalled_on=%s,last_progress=%s" % (stalled_on, last_progress),
                )
            )
        if error is not None:
            # A recorded failure is raised first, even though it also sets the stop
            # signal and therefore ``cancelled``. Only a *user* cancellation is a
            # designed stop; anything else must never be reported as a success.
            raise error
        if user_cancelled:
            return played
        if not (len(played) == synthesised == total):
            # Belt and braces: the bookkeeping invariant is re-checked on the way
            # out, so "returned successfully while speaking less" is not
            # representable even if a future edit breaks the in-loop accounting.
            raise self._fatal(
                SequentialVoiceSessionError(
                    "pcm_seq_tail_not_played",
                    "played=%d,synthesized=%d,total=%d" % (len(played), synthesised, total),
                )
            )
        return played

    def cancel(self) -> None:
        """Stop both threads and silence the device; bounded and idempotent.

        The order is fixed: mark cancelled (which unblocks both threads), then
        ``worker.stop()`` so the device is silenced without waiting for the
        producer, then a bounded join of both threads. Nothing is raised: a
        cancellation is a designed stop, not a failure.
        """

        with self._condition:
            if self._cancelled.is_set():
                return
            self._user_cancelled = True
            self._cancelled.set()
            self._finished = True
            self._tasks.clear()
            self._published.clear()
            self._condition.notify_all()
        try:
            self._worker.stop(timeout=self._stop_timeout_s)
        except Exception:  # noqa: BLE001 - cancelling is best effort
            pass
        bound = max(self._stop_timeout_s, self._wake_interval_s * 5.0)
        for thread in (self._synth_thread, self._play_thread):
            thread.join(timeout=bound)

    def close(self) -> None:
        """``cancel()`` plus a bounded join and ``worker.close()``; idempotent."""

        self.cancel()
        bound = max(self._stop_timeout_s + 1.0, 2.0)
        for thread in (self._synth_thread, self._play_thread):
            thread.join(timeout=bound)
        try:
            self._worker.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    # ------------------------------------------------------------------- facts

    @property
    def sentences_total(self) -> int:
        """Sentences the segmenter produced for this reply (read-only)."""

        with self._condition:
            return self._sentences_total

    @property
    def sentences_synthesized(self) -> int:
        """Sentences whose provider produced at least one PCM block (read-only)."""

        with self._condition:
            return self._sentences_synthesized

    @property
    def sentences_played(self) -> int:
        """Sentences whose ticket reached ``PlaybackState.COMPLETED`` (read-only)."""

        with self._condition:
            return self._sentences_played

    @property
    def text_length(self) -> int:
        """Characters ever pushed on this session (read-only)."""

        with self._condition:
            return self._text_length

    @property
    def played_chars(self) -> int:
        """Characters of the played sentences (read-only)."""

        with self._condition:
            return self._played_chars

    @property
    def played_characters(self) -> int:
        """Alias of :attr:`played_chars`; same number, same read-only contract."""

        return self.played_chars

    @property
    def lookahead_peak(self) -> int:
        """High-water mark of the published queue; bounded by ``READY_QUEUE_CAPACITY``."""

        with self._condition:
            return self._lookahead_peak

    @property
    def synth_wait_ms(self) -> int:
        """Total time the synthesis thread spent waiting for queue room, in ms."""

        with self._condition:
            return round(self._synth_wait_seconds * 1000.0)

    @property
    def play_wait_ms(self) -> int:
        """Total time the playback thread spent waiting for PCM or a ticket, in ms."""

        with self._condition:
            return round(self._play_wait_seconds * 1000.0)

    @property
    def error(self) -> Exception | None:
        """The recorded exception, or ``None``; the first one is always kept."""

        with self._condition:
            return self._error

    @property
    def provider_error_code(self) -> str | None:
        """Fixed provider/transport error code, when synthesis failed at the provider."""

        with self._condition:
            return self._provider_error_code

    @property
    def failed_sentence_index(self) -> int | None:
        """1-based sentence index of the provider request that failed."""

        with self._condition:
            return self._failed_sentence_index

    @property
    def failed_request_chars(self) -> int | None:
        """Character count of the provider request that failed."""

        with self._condition:
            return self._failed_request_chars

    @property
    def error_type(self) -> str | None:
        """Recorded exception **type name** only — what the caller logs."""

        with self._condition:
            return None if self._error is None else type(self._error).__name__

    @property
    def cancelled(self) -> bool:
        """``cancel()`` was called (read-only; a designed stop, not an error)."""

        return self._cancelled.is_set()

    @property
    def dropped_pushes(self) -> int:
        """Pushes refused by the terminal-state guard (read-only)."""

        with self._condition:
            return self._dropped_pushes

    @property
    def played_segments(self) -> tuple[str, ...]:
        """The played sentence texts, read-only and in playback order.

        This name exists beside ``sentences_played`` purely so the existing
        ``SessionStream`` adapter can read the same fact it already reads from the
        deployed channel.
        """

        with self._condition:
            return tuple(item.text for item in self._played_order)

    @property
    def pending_sentences(self) -> int:
        """Sentences synthesized and waiting to be played (published queue length)."""

        with self._condition:
            return len(self._published)

    @property
    def pending_segments(self) -> int:
        """``sentences_total - sentences_played``: the honest truncation counter."""

        with self._condition:
            outstanding = self._sentences_total - self._sentences_played
            return outstanding if outstanding > 0 else 0

    @property
    def accepted_segments(self) -> int:
        """Sentences admitted into the pipeline (equal to ``sentences_total``)."""

        with self._condition:
            return self._sentences_total

    @property
    def finished(self) -> bool:
        """``finish()`` was called (read-only)."""

        with self._condition:
            return self._finished

    @property
    def stall_timeout_s(self) -> float:
        """Seconds of **no progress** after which收尾 gives up (P1)."""

        return self._stall_timeout_s

    @property
    def drain_seconds(self) -> float | None:
        """Wall seconds ``finish()`` spent draining both threads (``None`` before)."""

        with self._condition:
            return self._drain_seconds

    @property
    def last_progress(self) -> str | None:
        """Fixed code of the furthest progress reached when收尾 gave up (P1).

        ``sentence_played`` / ``sentence_synthesized`` / ``none``. Read-only and
        never contains sentence text.
        """

        with self._condition:
            return self._last_progress

    # ------------------------------------------------------- drain / stall (P1)

    def _progress_mark_locked(self) -> tuple[int, int]:
        """两个只增不减的句子级进展计数（调用方必须已持有 ``self._condition``）。"""

        return (self._sentences_synthesized, len(self._played_order))

    def _progress_stage_locked(self) -> str:
        """最远进展阶段的固定短码（只用于事实上报，绝不参与判定）。"""

        if self._played_order:
            return "sentence_played"
        if self._sentences_synthesized:
            return "sentence_synthesized"
        return "none"

    def _await_drain(self) -> tuple[float, bool]:
        """等两个线程排空，判据是「**停滞**」而不是「总时长」（P1）。

        与 :mod:`app.tts.streaming_session` 的同名方法同构：每轮把**一个停滞预算**分给两个
        线程（``join`` 在线程结束的瞬间就返回），醒来比对进展标记；标记变了就再来一轮，
        没变才判停滞。刻意**不分片轮询**——分片会把有界预算整体拖长。

        旧实现是两次 ``join(timeout=join_timeout_s)``（默认各 40 s）：整条回复只要超过它就被
        判 ``pcm_seq_join_timeout`` 而**整条失败**，这是长回复在 pcm_seq 通道上的中止来源。
        """

        started = monotonic_seconds()
        with self._condition:
            mark = self._progress_mark_locked()
        while True:
            deadline = monotonic_seconds() + self._stall_timeout_s
            for thread in (self._synth_thread, self._play_thread):
                remaining = deadline - monotonic_seconds()
                if remaining > 0.0:
                    thread.join(timeout=remaining)
            if not (self._synth_thread.is_alive() or self._play_thread.is_alive()):
                return monotonic_seconds() - started, False
            with self._condition:
                current = self._progress_mark_locked()
            if current == mark:
                return monotonic_seconds() - started, True
            mark = current

    # ---------------------------------------------------------------- internal

    def _fatal(self, exc: Exception) -> Exception:
        """Record the first fatal error, unblock both threads, return ``exc``.

        ``_failed`` makes ``finish()`` re-raise instead of reporting a truncated
        reply as a success, and the **first** error is kept so a later one can
        never mask the root cause. This is the single place a failure is recorded,
        which is what makes ``error is None`` with a short playback unrepresentable.
        """

        with self._condition:
            if self._error is None:
                self._error = exc
            self._failed = True
            self._cancelled.set()
            self._tasks.clear()
            self._published.clear()
            self._condition.notify_all()
        return exc

    def _record_provider_failure(self, exc: Exception, *, index: int, text: str) -> None:
        """Record the **first** failing request: fixed short code, index and length.

        Real-machine evidence (2026-10-07, `pcm_seq`): long replies were aborted by a
        raw ``VoiceStudioContractError`` that carried **no** code at all, and it was
        raised while *building* the request — outside the provider call that the other
        fact path wraps. Only a short lowercase code is accepted (the same gate the
        fact layer applies); a code-less exception still records the failing sentence
        index and the request length, so "which sentence failed and how long it was"
        is never lost. Facts are first-wins and read-only: nothing here changes the
        raised exception or the playback behaviour.
        """

        code = getattr(exc, "code", None)
        if not (
            isinstance(code, str)
            and 0 < len(code) <= 64
            and all(char.islower() or char.isdigit() or char == "_" for char in code)
        ):
            code = None
        with self._condition:
            if self._failed_sentence_index is not None:
                return
            self._provider_error_code = code
            self._failed_sentence_index = int(index)
            self._failed_request_chars = len(text)

    def _make_request(self, index: int, text: str) -> VoiceStudioSentenceRequest:
        return VoiceStudioSentenceRequest(
            reply_id=self._reply_id,
            segment_id=f"segment-{index}",
            provider_request_id=f"{self._reply_id}-{index}",
            provider_generation=1,
            text=text,
            profile_id=getattr(self.selection, "profile_id", "confirmed-default"),
            language=self._language,
            speed=float(getattr(self.selection, "speed", 1.0)),
        )

    def _await_room(self, handle: _PublishedSentence) -> bool:
        """Bounded wait until the prefetch budget has room; ``False`` means stop.

        The budget is the number of sentences already **published** for playback:
        before publishing a new one the synthesis thread waits until fewer than
        :attr:`READY_QUEUE_CAPACITY` sentences are published. The published queue is
        never drained by dropping a sentence, and a wait that expires is a recorded
        failure (``pcm_seq_synthesis_timeout``) rather than a silent loss.

        The wait deadline uses the module's real monotonic clock on purpose
        instead of the injectable fact clock: the bound must still hold when a
        caller injects a clock that does not advance (test doubles do exactly
        that), and a wait whose timeout could freeze is not a bound.
        """

        deadline = monotonic_seconds() + self._synthesis_wait_s
        with self._condition:
            self._inflight = handle
            try:
                while len(self._published) >= self.READY_QUEUE_CAPACITY:
                    if self._cancelled.is_set() or self._failed:
                        return False
                    remaining = deadline - monotonic_seconds()
                    if remaining <= 0.0:
                        raise self._fatal(
                            SequentialVoiceSessionError("pcm_seq_synthesis_timeout")
                        )
                    started = self._clock()
                    self._condition.wait(timeout=remaining)
                    self._synth_wait_seconds += max(0.0, self._clock() - started)
                return True
            finally:
                self._inflight = None

    def _synthesize_one(self, handle: _PublishedSentence) -> str:
        """Render one sentence, publishing it for playback before it is finished.

        Returns ``"complete"`` when the provider produced audio and ended, or
        ``"stopped"`` when the reply was cancelled/failed under us. An audio-less
        stream is a recorded failure (``pcm_seq_no_audio``): the sentence is put
        into the published queue on its first block, *before* that block is handed
        over, so the playback thread can open the sentence on the worker and hand
        the first blocks to the device while the rest is still being rendered.
        """

        try:
            for chunk in self._transport_router.stream_sentence(
                handle.request, cancelled=self._cancelled
            ):
                with self._condition:
                    if self._cancelled.is_set() or self._failed:
                        return "stopped"
                    if not handle.published:
                        # The exactly-once publish point, taken before this block is
                        # handed over, so playback can start this instant.
                        self._published.append(handle)
                        handle.published = True
                        if len(self._published) > self._lookahead_peak:
                            self._lookahead_peak = len(self._published)
                    handle.chunks.append(chunk)
                    handle.produced = True
                    self._condition.notify_all()
        except SequentialVoiceSessionError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed with a fixed short code
            if self._cancelled.is_set() or self._failed:
                return "stopped"
            self._record_provider_failure(exc, index=handle.index, text=handle.text)
            raise self._fatal(
                SequentialVoiceSessionError("pcm_seq_synthesis_failed", type(exc).__name__)
            ) from None
        finally:
            with self._condition:
                handle.complete = True
                self._condition.notify_all()
        if not handle.produced:
            if self._cancelled.is_set() or self._failed:
                # Cancelled before the first block arrived: a designed stop, not an
                # audio-less provider, so nothing is recorded as a failure.
                return "stopped"
            # A provider that reported success while producing no audio at all is
            # exactly the "spoke nothing but claimed success" shape this channel
            # exists to rule out, so it is a recorded failure and never a sentence
            # that merely counts as synthesized.
            raise self._fatal(SequentialVoiceSessionError("pcm_seq_no_audio"))
        return "complete"

    def _synthesize_loop(self) -> None:
        """Serial synthesis loop: one sentence at a time, prefetched ahead of play.

        Exactly two stops exist: the reply was explicitly finished/cancelled and
        every task is consumed, or a fatal error was recorded (``_failed``), which
        ``finish()`` re-raises. There is deliberately **no** "queue was full"
        exit — the only thing a full published queue does is wait.
        """

        try:
            while True:
                with self._condition:
                    while not self._tasks:
                        if self._failed or self._cancelled.is_set() or self._finished:
                            return
                        self._condition.wait(timeout=0.05)
                    text = self._tasks.pop(0)
                    self._sequence_consumed += 1
                    index = self._sequence_consumed
                    try:
                        request = self._make_request(index, text)
                    except Exception as exc:  # noqa: BLE001 - a rejected request must be visible
                        # Real-machine evidence (2026-10-07, `pcm_seq`): the local
                        # contract can refuse the segment *before* the provider is
                        # called (a whitespace-only piece such as a blank line
                        # between paragraphs), and that failure used to reach the
                        # outer handler bare — no code, no sentence index. Record
                        # the facts first and then re-raise the **same** exception so
                        # the observable error type and behaviour stay unchanged.
                        self._record_provider_failure(exc, index=index, text=text)
                        raise
                    handle = _PublishedSentence(text=text, index=index, request=request)
                    if not self._await_room(handle):
                        return
                outcome = self._synthesize_one(handle)
                with self._condition:
                    self._inflight = None
                    if outcome == "complete":
                        # ``synthesized`` counts sentences whose provider really
                        # produced audio. A sentence handed to a failing provider is
                        # counted in ``total`` only, and that shortfall is exactly
                        # what the read-only facts (and ``finish()``) must show.
                        self._sentences_synthesized += 1
                    self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001 - a channel failure must be visible
            if not self._failed and not self._cancelled.is_set():
                self._fatal(exc)
        finally:
            with self._condition:
                self._synthesis_done = True
                self._condition.notify_all()

    def _play_loop(self) -> None:
        try:
            while True:
                handle = self._take_published()
                if handle is None:
                    return
                self._play_one(handle)
        except Exception as exc:  # noqa: BLE001 - a channel failure must be visible
            if not self._failed and not self._cancelled.is_set():
                self._fatal(exc)
        finally:
            with self._condition:
                self._play_done = True
                self._condition.notify_all()

    def _take_published(self) -> _PublishedSentence | None:
        """Pop the next published sentence, or ``None`` when there is none left."""

        with self._condition:
            while True:
                if self._published:
                    handle = self._published.pop(0)
                    self._condition.notify_all()
                    return handle
                if self._failed or self._cancelled.is_set():
                    return None
                if self._finished and self._synthesis_done:
                    return None
                started = self._clock()
                self._condition.wait(timeout=self._wake_interval_s)
                self._play_wait_seconds += max(0.0, self._clock() - started)

    def _play_one(self, handle: _PublishedSentence) -> None:
        """Open the sentence on the worker, feed it, then wait for COMPLETED."""

        ticket: PlaybackTicket | None = None
        pushed = 0
        try:
            ticket = self._worker.begin_sentence(handle.request)
            handed = 0
            while True:
                with self._condition:
                    while handed >= len(handle.chunks) and not handle.complete:
                        if self._cancelled.is_set() or self._failed:
                            break
                        if ticket.terminal:
                            break
                        started = self._clock()
                        self._condition.wait(timeout=self._wake_interval_s)
                        self._play_wait_seconds += max(0.0, self._clock() - started)
                    if handed < len(handle.chunks):
                        chunk = handle.chunks[handed]
                        handed += 1
                    else:
                        break
                if self._cancelled.is_set() or ticket.terminal:
                    break
                accepted = self._worker.push_chunk(chunk)
                if accepted:
                    pushed += 1
            if self._cancelled.is_set():
                return
            if pushed == 0:
                # Audio-less "success" is exactly the silent tail this channel
                # exists to rule out, so it is a recorded failure.
                raise self._fatal(SequentialVoiceSessionError("pcm_seq_no_audio"))
            # The device really has to finish this sentence before the next one may
            # start. ``PlaybackState.COMPLETED`` is the only reliable end-of-audio
            # signal in this repository (WOM_DONE via the waveOut backend), so the
            # wait below is a *wait for the device*, not a sleep: it returns the
            # instant the ticket goes terminal, and it is bounded by the configured
            # playback timeout.
            deadline = monotonic_seconds() + self._playback_timeout_s
            state: PlaybackState | None = None
            while True:
                started = self._clock()
                state = self._worker.wait(
                    ticket, max(_MIN_WAIT_SLICE_S, deadline - monotonic_seconds())
                )
                with self._condition:
                    self._play_wait_seconds += max(0.0, self._clock() - started)
                if state is PlaybackState.COMPLETED or ticket.terminal:
                    break
                if monotonic_seconds() >= deadline:
                    break
            if state is not PlaybackState.COMPLETED:
                if self._cancelled.is_set():
                    return
                raise self._fatal(
                    SequentialVoiceSessionError(
                        "pcm_seq_playback_not_completed",
                        getattr(state, "value", str(state)),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - the device refused audio: fail closed
            if isinstance(exc, SequentialVoiceSessionError) or self._failed:
                raise
            raise self._fatal(
                SequentialVoiceSessionError("pcm_seq_playback_failed", type(exc).__name__)
            ) from None
        else:
            with self._condition:
                self._played_order.append(handle)
                self._sentences_played += 1
                self._played_chars += len(handle.text)
                self._condition.notify_all()


__all__ = [
    "BYTES_PER_MS",
    "DEFAULT_JOIN_TIMEOUT_S",
    "DEFAULT_PLAYBACK_TIMEOUT_S",
    "DEFAULT_SYNTHESIS_WAIT_S",
    "DEFAULT_WAKE_INTERVAL_S",
    "MAX_SENTENCE_CHARACTERS",
    "PROVIDER_CHUNK_MS",
    "SAMPLE_RATE",
    "SECONDARY_BREAKS",
    "SENTENCE_ENDERS",
    "SEQUENTIAL_ERROR_CODES",
    "SequentialVoiceSession",
    "SequentialVoiceSessionError",
    "minimum_pcm_queue_ms",
    "split_sentences",
]
