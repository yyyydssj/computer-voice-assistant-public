"""Opt-in WebSocket-first streaming speech session.

This session type is **separate** from ``RealtimeVoiceSession``: the existing
``legacy`` and ``http_wav`` realtime paths are not touched at all, so their
behavior stays bit-for-bit identical. Here one sentence at a time goes through
``VoiceStudioTransportRouter`` (WebSocket first, HTTP once before any audio) and
the arriving PCM chunks are handed to :class:`PCMPlaybackWorker`.

Transport failures are not interpreted locally: the session reports the existing
:class:`TTSFallbackRouter` decision (VoiceStudio -> Edge -> text) and exposes it
as ``routing_decision``. It never switches voice mid-sentence and never replays:
once any PCM chunk was accepted for the current sentence the reported playback
state is ``PLAYBACK_STARTED``.

The Edge slot of that decision is now **wired but opt-in**: when an injected
``edge_provider`` is present and the router answers
``TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT`` — which the router only does while
the failed sentence never started playing — the *same* sentence is re-rendered
exactly **once** by that provider into the *same* :class:`PCMPlaybackWorker`, and
then follows the ordinary completion path (``_played`` plus one timing sample).
A sentence whose audio already reached the device is never re-rendered, a
``CONTINUE_TEXT_NO_REPLAY`` decision keeps the unchanged text-only path, an Edge
failure calls ``router.edge_failed(...)``, stops this reply's remaining speech
and never reaches for a third provider, and every sentence is attempted at most
once. With ``edge_provider=None`` this module behaves bit-for-bit as before.
Two read-only counters (``edge_attempts``/``edge_failures``) and two redacted
``late_events`` kinds (``edge_used``/``edge_failed``) make that path visible
without carrying any sentence text, path or URL.

Sentence-level timing instrumentation (dual-path plan section 19.2) is
**read-only and offline-first**: six monotonic stamps per sentence (ready,
submitted, first PCM, gate open, device submit) produce one redacted
:class:`SentenceTimingSample` per *completed* sentence, and
``timing_percentiles()`` reduces them with the explicit nearest-rank rule
:func:`nearest_rank_percentile`. Nothing here changes routing, generation,
backpressure, cancellation or stop semantics, nothing is written to the device,
and no real latency claim is made by this module: only the instrumentation is
defined here, the 20-sentence warm measurement needs separate authorization.

The stamps use ``app.tts.clock.monotonic_seconds`` by default: on Windows that
is ``time.perf_counter``, because ``time.monotonic`` there is ``GetTickCount64``
with a 15.625 ms granularity and cannot resolve worker-side spans at all (they
show up as ``0.000 ms``). An explicitly injected ``clock`` keeps priority and is
still shared by the session, the transport router and the playback worker.

Identity verification runs **once per reply**, not once per sentence: the
VoiceProfile/Provider may only change on a sentence/reply boundary, so the
boundary at the start of a reply is sufficient, while a per-sentence re-check
put two HTTP round-trips plus a SHA-256 of the reference audio (measured
18.4 / 29.7 / 56.4 ms min/p50/max) inside ``ready_to_submit``.

Measurement discipline: ``first_pcm_to_device_ms`` spans the first PCM stamp to
the device submit stamp and therefore **includes** the configured start-buffer
accumulation, so it must **never** be compared with the plan's 50 ms extra-cost
target. That judgement uses ``gate_to_device_ms`` —
``gate_open_at -> device_submit_at``, which starts counting only once the
start-buffer gate is already open. ``first_pcm_to_gate_ms`` is pure diagnostics,
and ``submit_to_first_pcm_ms`` (``tts_submitted_at -> first_pcm_at``) is the
synthesis/first-audio span the plan's ``auto`` vs ``http_wav`` median
start-latency comparison is read from. Samples produced under an injected clock
only self-check the instrumentation; they are not a real performance result.

Queue discipline (R4-B follow-up, real-machine "只播 74 字却说播完"): the
pending-sentence bound is a **waiting** backpressure point, never a drop point.
A batch of newly segmented sentences is either admitted in full once the consumer
made room — ``_enqueue`` waits on ``self._condition``, which releases the lock,
so the consumer thread keeps popping — or refused in full with the explicit
``streaming_queue_budget_exceeded`` error. A partial enqueue followed by a silent
tail loss is impossible, and ``overloaded`` only records that the bound was hit.
The consumer thread therefore has exactly two exits: the reply was explicitly
finished *and* the queue is drained, or a fatal error was recorded (``_error`` and
``_failed`` set, re-raised by ``finish()``). Three read-only counters —
``accepted_segments`` (enqueued), ``synthesized_segments`` (audio produced) and
``played_count`` (reached ``PlaybackState.COMPLETED``) — make "accepted but never
played" visible, so a reply can never report success while speaking less than it
accepted.

Segment-flow evidence (a later follow-up, real-machine "292 characters pushed but
``accepted_segments=1``"): six more **read-only, additive** counters split the
path between the segmenter and the pending queue into
``dequeued_segments_total``/``dequeued_characters_total`` (counted right after
``ReplySegmenter.dequeue`` and therefore *before* ``_enqueue``),
``enqueued_segments_total``/``enqueued_characters_total`` (counted once the batch
really entered ``_pending``) and
``enqueue_dropped_segments_total``/``enqueue_dropped_characters_total`` (counted
in ``_enqueue``'s terminal-state early return, the only place a dequeued batch can
disappear without a trace), plus the read-only
``segmenter_buffered_characters``. They are diagnostics only: no decision, no
timeout, no wait, no return value and no ``late_events`` entry is added, and the
early-return branch itself is unchanged.

The PCM queue budget of the assembled session must cover the longest possible
sentence segment with margin (:data:`DEFAULT_PCM_QUEUE_BUDGET_MS`); a budget
smaller than one segment's speech duration would itself become a truncation
source, because :class:`PCMPlaybackWorker` refuses audio that does not fit.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from math import ceil, isfinite
from threading import Condition, Event
from typing import Callable, Sequence

from app.tts.clock import monotonic_seconds
from app.tts.pcm_playback import PCMPlaybackBackpressure, PCMPlaybackWorker
from app.tts.playback import ProviderRequestPlaybackState
from app.tts.playback_worker import PlaybackState
from app.tts.private_selection import VoiceSelection, verify_provider, verify_reference
from app.tts.providers import VoiceStudioSentenceRequest
from app.tts.routing import TTSFallbackRouter, TTSRoutingAction, TTSRoutingDecision
from app.tts.segmentation import (
    ReplySegmenter,
    SegmentationConfig,
    SegmentationDecision,
    SentenceSegment,
    TextChunk,
)
from app.tts.transport import VoiceStudioTransportError, VoiceStudioTransportRouter


class StreamingVoiceSessionError(RuntimeError):
    """Streaming synthesis or playback failed; original text stays intact."""


QUEUE_BUDGET_EXCEEDED = "streaming_queue_budget_exceeded"
"""Fixed error code used when a sentence batch cannot be admitted within the budget.

The wording is identical to the pre-existing segmenter-budget code so the
``chat_loop._speak()`` reason (``speech_failed:StreamingVoiceSessionError``) and
the ``session.error`` type name stay the same fixed short code that was already
in use before this change.
"""

PUSH_OUT_OF_ORDER = "streaming_push_out_of_order"
"""Fixed error code used when a push broke the segmenter's chunk-sequence contract.

真机证据（2026-10-09，`ws_pcm`）：一个纯空白 push 被分段器的 `EMPTY_TEXT` 拒收且**不消费
序号**，于是其后每个 chunk 都变成"越序"，而 `_submit` 对越序什么都不做 ⇒ 233 字只播出 74 字
且 `error is None`。静默的那条路现在改为一律记录的致命错误；触发它的纯空白 push 也已按
"空 push"忽略（见 :meth:`StreamingVoiceSession.push_text_chunk`）。
"""

MAX_SEGMENT_CHARACTERS = 48
"""Hard character ceiling of one sentence segment in this session.

It is the single source for :class:`SegmentationConfig.max_segment_characters`
below and for the PCM queue-budget derivation; the value is unchanged (48) so
no existing segmentation behaviour moves.
"""

SPEECH_CHARACTERS_PER_SECOND = 4.8
"""Nominal Chinese speech rate used to turn characters into speech seconds.

Derivation, stated honestly: the repository has **no** pre-existing
characters-per-second constant, so this one is introduced here and pinned by a
test rather than guessed at call sites. It is the documented ratio behind the
existing ``max_segment_characters=48`` choice (a 48-character segment is about
10 seconds of speech): ``48 / 4.8 = 10.0 s``. Mandarin TTS at a natural rate
speaks roughly 4 to 5 characters per second, so 4.8 is the mid-point of that
band. The risk of the estimate is recorded in the report: a slower engine makes
the derived budget *too small* proportionally, which is exactly why the margin
below is ``2x`` and not ``1x``.
"""

PCM_QUEUE_BUDGET_MARGIN = 2.0
"""Required safety factor: the queue budget must cover two longest segments."""


def speech_seconds_for_characters(characters: int) -> float:
    """Nominal speech duration (seconds) of ``characters`` at the pinned rate."""

    if type(characters) is not int or characters < 0:
        raise ValueError("characters 必须是非负整数")
    return characters / SPEECH_CHARACTERS_PER_SECOND


def minimum_pcm_queue_budget_ms(
    *,
    max_segment_characters: int = MAX_SEGMENT_CHARACTERS,
    margin: float = PCM_QUEUE_BUDGET_MARGIN,
) -> int:
    """Lower bound of the PCM queue budget: one longest segment x ``margin``.

    ``PCMPlaybackWorker`` refuses (``PCMPlaybackBackpressure``) any chunk that
    does not fit its queue, so a budget below one segment's speech duration turns
    the device into a truncation source. The bound is expressed in whole
    milliseconds and rounded up, i.e. it can only be conservative.
    """

    if type(max_segment_characters) is not int or max_segment_characters <= 0:
        raise ValueError("max_segment_characters 必须是正整数")
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or margin <= 0:
        raise ValueError("margin 必须是正数")
    seconds = speech_seconds_for_characters(max_segment_characters) * float(margin)
    return ceil(seconds * 1000.0)


DEFAULT_PCM_QUEUE_BUDGET_MS = minimum_pcm_queue_budget_ms()
"""Default ``max_queued_audio_ms`` for :func:`build_streaming_session`.

48 characters x 2 margin at 4.8 characters/second = 20 000 ms, against the
previous 8 000 ms which was **smaller than one 10 s segment** — the conflict this
change removes. The segmentation character limit is deliberately *not* changed,
so no existing segmentation test or contract moves.
"""


DEFAULT_STALL_TIMEOUT_S = 15.0
"""Default ``stall_timeout_s``: how long收尾 may make **no progress** (P1).

Replaces the old "one reply may take at most ``segment_timeout_s``" cap. The
reference implementation (豆包实时语音) has no total-duration cap at all: its
timeouts are idle-based (``AudioTTSIdleTimeoutError(52000016)`` /
``DialogAudioIdleTimeoutError(52000042)`` / ``AudioChatRecvTimeoutError(52000011)``),
see ``implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md``.
15 s is a stall, not slowness: a 48-character segment is ~10 s of audio and the
real machine measured 1.2–2.9 s time-to-first-audio per segment, so nothing
progressing for 15 s means the pipeline is wedged.
"""

def _verify_selection(selection: VoiceSelection) -> None:
    verify_reference(selection)
    verify_provider(selection)


def _finite_sorted(samples: Sequence[float]) -> list[float]:
    """Return the numeric, finite members of ``samples`` sorted ascending."""

    values = [
        float(sample)
        for sample in samples
        if isinstance(sample, (int, float))
        and not isinstance(sample, bool)
        and isfinite(float(sample))
    ]
    values.sort()
    return values


def nearest_rank_percentile(samples: Sequence[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile of ``samples`` (no interpolation).

    The definition is fixed here, in a pure function, so a later reader cannot
    reinterpret the numbers produced by :meth:`StreamingVoiceSession.timing_percentiles`:

    1. every member of ``samples`` that is not a finite number is dropped
       (``None`` is never passed in; callers filter missing segments first);
    2. the remaining ``count`` values are sorted ascending;
    3. ``rank = ceil(percentile * count)``, clamped into ``[1, count]``;
    4. the result is ``sorted_values[rank - 1]`` — an actual observed sample,
       never an average of two middle values and never an interpolation.

    ``p50`` uses ``percentile=0.5`` and ``p95`` uses ``percentile=0.95``. An
    empty (or entirely non-finite) sample set returns ``None``. ``percentile``
    must lie in ``(0, 1]``; anything else raises ``ValueError``. Only the
    standard library is used.
    """

    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise TypeError("percentile 必须为数字")
    value = float(percentile)
    if not isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError("percentile 必须在 (0, 1] 范围内")
    values = _finite_sorted(samples)
    if not values:
        return None
    rank = ceil(value * len(values))
    rank = 1 if rank < 1 else (len(values) if rank > len(values) else rank)
    return values[rank - 1]


def _timing_percentiles(values: Sequence[float | None]) -> TimingPercentiles:
    """Reduce one millisecond segment to ``count/p50/p95``, skipping missing values."""

    present = [value for value in values if value is not None]
    return TimingPercentiles(
        count=len(present),
        p50=nearest_rank_percentile(present, 0.5),
        p95=nearest_rank_percentile(present, 0.95),
    )


def _elapsed_ms(start: float | None, end: float | None) -> float | None:
    """Milliseconds between two monotonic stamps; ``None`` when one is missing."""

    if start is None or end is None:
        return None
    return (end - start) * 1000.0


@dataclass(frozen=True, slots=True)
class SentenceTimingSample:
    """One sentence's redacted timing sample; milliseconds over one monotonic clock.

    The five monotonic stamps behind these spans are ``sentence_ready_at``,
    ``tts_submitted_at``, ``first_pcm_at``, ``gate_open_at`` (the instant the
    playback worker's start-buffer gate first opened) and ``device_submit_at``
    (the instant the first PCM batch reached the backend). The derived spans are:

    - ``ready_to_submit_ms`` = ``tts_submitted_at - sentence_ready_at``;
    - ``first_pcm_to_device_ms`` = ``device_submit_at - first_pcm_at``; this is
      the raw observation and it **includes** the configured start-buffer
      accumulation, so it must **never** be compared with the plan's 50 ms
      "extra cost excluding the start buffer" target;
    - ``first_pcm_to_gate_ms`` = ``gate_open_at - first_pcm_at``; the
      start-buffer accumulation itself, pure diagnostics;
    - ``gate_to_device_ms`` = ``device_submit_at - gate_open_at``; the extra cost
      *after* the start buffer is already open, and therefore the only span the
      plan's 50 ms criterion may be judged with. It is exactly ``0.0`` when the
      gate opened and the first submission fell on the same instant; that is an
      honest measurement, not a fabricated difference;
    - ``submit_to_first_pcm_ms`` = ``first_pcm_at - tts_submitted_at``; the
      synthesis/first-audio span of this sentence. The plan's third and fourth
      criteria compare the **median** start latency of ``auto`` against
      ``http_wav``, which is read from this segment's ``p50``/``p95``.

    Each span is ``None`` when one of its two stamps is unavailable. A sample
    never carries sentence text, PCM bytes, a file path or a URL: only the
    redacted reply id, the provider generation and the segment index identify it.
    """

    reply_id: str
    generation: int
    segment_index: int
    ready_to_submit_ms: float | None
    first_pcm_to_device_ms: float | None
    first_pcm_to_gate_ms: float | None
    gate_to_device_ms: float | None
    submit_to_first_pcm_ms: float | None


@dataclass(frozen=True, slots=True)
class TimingPercentiles:
    """Nearest-rank summary of one millisecond segment; ``None`` when empty."""

    count: int
    p50: float | None
    p95: float | None


@dataclass(frozen=True, slots=True)
class StreamingTimingPercentiles:
    """``timing_percentiles()`` result: every monotonic span, milliseconds.

    ``ready_to_submit``, ``first_pcm_to_device`` and ``gate_to_device`` keep
    their definitions; the added ``submit_to_first_pcm`` is
    ``tts_submitted_at -> first_pcm_at`` and carries the synthesis/first-audio
    latency the plan's third and fourth criteria (``auto`` vs ``http_wav`` median
    start latency) are judged with. ``gate_to_device`` is still the only segment
    that excludes the configured start-buffer accumulation, so it — not
    ``first_pcm_to_device`` — is what the plan's 50 ms criterion is read from.
    Missing values never enter a segment's ``count``; these numbers describe
    instrumentation, not measured performance.
    """

    ready_to_submit: TimingPercentiles
    first_pcm_to_device: TimingPercentiles
    gate_to_device: TimingPercentiles
    submit_to_first_pcm: TimingPercentiles


@dataclass(frozen=True, slots=True)
class _PendingSentence:
    """One already-segmented sentence waiting for synthesis, with its ready stamp."""

    text: str
    ready_at: float


class StreamingVoiceSession:
    """Sentence-at-a-time VoiceStudio streaming into the PCM playback worker."""

    def __init__(
        self,
        selection: VoiceSelection,
        *,
        transport_router: VoiceStudioTransportRouter,
        worker: PCMPlaybackWorker,
        fallback_router: TTSFallbackRouter | None = None,
        edge_provider: object | None = None,
        verifier: Callable[[VoiceSelection], None] | None = None,
        max_pending_segments: int = 8,
        language: str = "Chinese",
        owns_worker: bool = False,
        segment_timeout_s: float = 30.0,
        stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
        queue_wait_timeout_s: float | None = None,
        stop_timeout_s: float = 1.0,
        clock: Callable[[], float] = monotonic_seconds,
    ) -> None:
        selection.validate()
        if not isinstance(transport_router, VoiceStudioTransportRouter):
            raise TypeError("transport_router 必须是 VoiceStudioTransportRouter")
        if not isinstance(worker, PCMPlaybackWorker):
            raise TypeError("worker 必须是 PCMPlaybackWorker")
        if fallback_router is not None and not isinstance(fallback_router, TTSFallbackRouter):
            raise TypeError("fallback_router 必须是 TTSFallbackRouter")
        if edge_provider is not None and not callable(
            getattr(edge_provider, "stream_sentence", None)
        ):
            raise TypeError("edge_provider 必须提供 stream_sentence(request, *, cancelled)")
        if type(max_pending_segments) is not int or max_pending_segments <= 0:
            raise ValueError("max_pending_segments must be positive")
        if type(language) is not str or not language:
            raise ValueError("language 必须是非空字符串")
        if type(segment_timeout_s) not in {int, float} or segment_timeout_s <= 0:
            raise ValueError("segment_timeout_s 必须为正数")
        if type(stall_timeout_s) not in {int, float} or stall_timeout_s <= 0:
            raise ValueError("stall_timeout_s 必须为正数")
        if queue_wait_timeout_s is not None and (
            type(queue_wait_timeout_s) not in {int, float} or queue_wait_timeout_s <= 0
        ):
            raise ValueError("queue_wait_timeout_s 必须为正数或 None")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self.selection = selection
        self._transport_router = transport_router
        self._worker = worker
        self._fallback_router = fallback_router
        self._edge_provider = edge_provider
        self._verifier = verifier or _verify_selection
        self._language = language
        self._owns_worker = bool(owns_worker)
        self._segment_timeout_s = float(segment_timeout_s)
        # P1：收尾的「停滞」预算与「单段」预算分离。``segment_timeout_s`` 只描述
        # **一个句段**允许花的时间（provider 等待、队列等待、worker 等待），
        # ``stall_timeout_s`` 才是收尾判据：连续这么久**毫无进展**才允许放弃。
        self._stall_timeout_s = float(stall_timeout_s)
        self._stop_timeout_s = float(stop_timeout_s)
        # Bounded wait for room in the pending queue. ``None`` deliberately means
        # "the same bound as a segment", so the queue can never wait longer than
        # one synthesis/playback segment is allowed to take.
        self._queue_wait_timeout_s = float(
            self._segment_timeout_s if queue_wait_timeout_s is None else queue_wait_timeout_s
        )
        # The injected clock must share one monotonic basis with the worker clock,
        # otherwise the two spans cannot be subtracted. An explicit clock keeps
        # priority; the default is the shared high-resolution ``monotonic_seconds``.
        self._clock = clock
        self._condition = Condition()
        self._cancel_event = Event()
        self._reply_id = "stream-" + secrets.token_hex(8)
        self._segmenter = ReplySegmenter(
            reply_id=self._reply_id,
            config=SegmentationConfig(
                min_segment_characters=4,
                max_segment_characters=MAX_SEGMENT_CHARACTERS,
                max_queued_segments=8,
                soft_target_characters=32,
                soft_search_min_characters=24,
                soft_search_max_characters=40,
            ),
        )
        self._max_pending = max_pending_segments
        self._pending: list[_PendingSentence] = []
        self._played: list[str] = []
        # Three independent counters, so "accepted but never played" can never
        # hide behind two views of the same list (the real-machine defect):
        # ``accepted`` = sentences admitted into ``_pending``,
        # ``synthesized`` = sentences whose provider produced at least one PCM
        # block, ``played`` = the ``_played`` list (reached COMPLETED).
        self._accepted_segments = 0
        self._accepted_characters = 0
        self._synthesized_segments = 0
        # Segment-flow evidence, additive and read-only (R4-B follow-up: the
        # real-machine contradiction "text=291, pushed_chars=282, accepted=1").
        # Every counter below is written only *after* the step it describes has
        # already happened, so no decision, timeout, ordering or return value
        # changes:
        # ``_dequeued_*``   = segments/chars taken out of the segmenter, counted
        #                     before ``_enqueue`` runs, which is what makes
        #                     "dequeued but never enqueued" visible;
        # ``_enqueued_*``   = segments/chars that really entered ``_pending``;
        # ``_enqueue_dropped_*`` = segments/chars discarded by a terminal-state
        #                     early return of ``_enqueue`` (the silent drop point).
        self._dequeued_segments = 0
        self._dequeued_characters = 0
        self._enqueued_segments = 0
        self._enqueued_characters = 0
        self._enqueue_dropped_segments = 0
        self._enqueue_dropped_characters = 0
        self._sequence = 0
        self._segment_index = 0
        self._generation = 1
        self._late_events: list[str] = []
        self._timing_samples: list[SentenceTimingSample] = []
        self._active_ticket = None
        self._error: Exception | None = None
        self._routing_decision: TTSRoutingDecision | None = None
        self._failure_states: dict[str, ProviderRequestPlaybackState] = {}
        # Edge-slot bookkeeping: one attempt per sentence at most, counted for
        # read-only diagnostics. Nothing here can loop or retry a provider.
        self._edge_attempted: set[str] = set()
        self._edge_attempts = 0
        self._edge_failures = 0
        self._verified = False
        self._dropped_pushes = 0
        self._finish_timed_out = False
        # P1 收尾进展事实（只读，不参与任何判定）：
        self._drain_seconds: float | None = None
        self._stall_timeouts = 0
        self._last_progress: str | None = None
        self._finished = False
        self._cancelled = False
        self._failed = False
        self._overloaded = False
        self._closed = False
        self._reply_open = False
        if self._fallback_router is not None:
            self._fallback_router.start_reply(self._reply_id)
            self._reply_open = True
        self._thread = threading.Thread(
            target=self._run, name="streaming-voice-session", daemon=True
        )
        self._thread.start()

    # ---------------------------------------------------------------- properties

    @property
    def reply_id(self) -> str:
        return self._reply_id

    @property
    def error(self) -> Exception | None:
        with self._condition:
            return self._error

    @property
    def routing_decision(self) -> TTSRoutingDecision | None:
        with self._condition:
            return self._routing_decision

    @property
    def played_segments(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(self._played)

    @property
    def failure_states(self) -> dict[str, ProviderRequestPlaybackState]:
        with self._condition:
            return dict(self._failure_states)

    @property
    def finished(self) -> bool:
        with self._condition:
            return self._finished

    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancelled

    @property
    def overloaded(self) -> bool:
        """Diagnostic fact: the pending bound was reached at least once.

        It means exactly that and nothing more. It is **not** a reason to drop an
        accepted sentence and it is **not** an exit condition of the consumer
        thread: a single batch larger than ``max_pending_segments`` is admitted in
        full (there was nothing to wait for) and only recorded here, while a batch
        that cannot be admitted within the queue-wait budget is refused as a whole
        with the explicit ``streaming_queue_budget_exceeded`` error.
        """

        with self._condition:
            return self._overloaded

    @property
    def queue_wait_timeout_s(self) -> float:
        """Bound (seconds) on waiting for room in the pending queue."""

        return self._queue_wait_timeout_s

    @property
    def accepted_segments(self) -> int:
        """Sentences admitted into the pending queue (never a refused batch)."""

        with self._condition:
            return self._accepted_segments

    @property
    def accepted_characters(self) -> int:
        """Characters of the sentences admitted into the pending queue."""

        with self._condition:
            return self._accepted_characters

    @property
    def synthesized_segments(self) -> int:
        """Sentences whose provider produced at least one PCM block."""

        with self._condition:
            return self._synthesized_segments

    @property
    def dequeued_segments_total(self) -> int:
        """Segments taken out of the segmenter so far (read-only evidence).

        Counted immediately after every ``dequeue`` call and therefore **before**
        ``_enqueue`` admits the batch, so
        ``dequeued_total - enqueued_total - enqueue_dropped_total`` is exactly the
        number of segments currently on the short path between the segmenter and
        the pending queue. A segment that is dequeued and then neither enqueued nor
        counted as dropped would be the invisible loss this counter exists to rule
        out. Nothing here changes segmentation, waiting, cancellation or playback.
        """

        with self._condition:
            return self._dequeued_segments

    @property
    def dequeued_characters_total(self) -> int:
        """Characters of :attr:`dequeued_segments_total` (read-only, additive)."""

        with self._condition:
            return self._dequeued_characters

    @property
    def enqueued_segments_total(self) -> int:
        """Segments that really entered ``_pending`` (read-only, additive).

        Counted after ``self._pending.extend(batch)`` succeeded, so a batch refused
        by the terminal-state early return of ``_enqueue`` is never counted here
        (it is counted in :attr:`enqueue_dropped_segments_total` instead).
        """

        with self._condition:
            return self._enqueued_segments

    @property
    def enqueued_characters_total(self) -> int:
        """Characters of :attr:`enqueued_segments_total` (read-only, additive)."""

        with self._condition:
            return self._enqueued_characters

    @property
    def enqueue_dropped_segments_total(self) -> int:
        """Segments discarded by ``_enqueue``'s terminal-state early return.

        This is the silent drop point: the batch was already dequeued from the
        segmenter, the session is cancelled/failed/closed, and the batch is
        discarded without an error and without a late event. The counter makes that
        loss visible; the branch itself is deliberately left byte-for-byte
        unchanged (no raise, no late event — any behavior change is a later batch).
        """

        with self._condition:
            return self._enqueue_dropped_segments

    @property
    def enqueue_dropped_characters_total(self) -> int:
        """Characters of :attr:`enqueue_dropped_segments_total` (read-only)."""

        with self._condition:
            return self._enqueue_dropped_characters

    @property
    def segmenter_buffered_characters(self) -> int | None:
        """Characters still sitting in the segmenter's buffer (``None`` if unknown).

        A pure read of ``ReplySegmenter.buffered_text``: a segmenter without that
        attribute (or one that is not a string) is reported honestly as ``None``
        rather than raising.
        """

        buffered = getattr(self._segmenter, "buffered_text", None)
        if type(buffered) is not str:
            return None
        return len(buffered)

    @property
    def played_count(self) -> int:
        """Sentences that reached ``PlaybackState.COMPLETED``."""

        with self._condition:
            return len(self._played)

    @property
    def played_characters(self) -> int:
        """Characters of the sentences that reached COMPLETED."""

        with self._condition:
            return sum(len(sentence) for sentence in self._played)

    @property
    def pending_segments(self) -> int:
        """``accepted - played``: the derived "accepted but not completed" count.

        This is the honest truncation counter and it is deliberately not the live
        queue length (:attr:`pending_sentences`): a sentence that was popped,
        spoken past and then dropped by a cancellation still counts as pending
        here, which is exactly what "the reply claimed success but spoke less"
        looked like on the real machine.
        """

        with self._condition:
            outstanding = self._accepted_segments - len(self._played)
            return outstanding if outstanding > 0 else 0

    @property
    def generation(self) -> int:
        """Current provider generation; advanced by every cancellation."""

        with self._condition:
            return self._generation

    @property
    def verified(self) -> bool:
        """True once this reply's VoiceProfile/Provider identity was verified.

        Verification happens at most once per reply (see
        :meth:`_verify_once`); a new reply is a new session and therefore
        verifies again. The flag stays ``False`` when verification raised, which
        is the fail-closed case: nothing is spoken and no sample is produced.
        """

        with self._condition:
            return self._verified

    @property
    def dropped_pushes(self) -> int:
        """Number of Agent pushes refused by the terminal-state guard.

        A refused push used to return silently, leaving only the boolean
        ``overloaded`` flag as evidence. Every refusal is counted here **and**
        recorded in ``late_events`` as ``push_rejected_<reason>@generationN``;
        the pushed text itself is never stored. The reason set is limited to the
        terminal states (``closed``/``failed``/``cancelled``/``finished``): a full
        pending queue no longer refuses a push at all, it waits for room.
        """

        with self._condition:
            return self._dropped_pushes

    @property
    def pending_sentences(self) -> int:
        """Sentences accepted but not yet finished being spoken (queue length)."""

        with self._condition:
            return len(self._pending)

    @property
    def finish_timed_out(self) -> bool:
        """True when ``finish()`` gave up while the session thread was still busy.

        P1：判据已从「总时长超过 ``segment_timeout_s``」改成「**连续
        ``stall_timeout_s`` 毫无进展**」。慢但在推进的回复不再被判超时（旧实现会在这里
        把仍在播放的尾巴交给 ``close()`` 取消掉）；只有真的卡住才会是 True。无论哪种情况，
        该回复都不能被读成"全部播完"——``close()`` 仍按设计取消剩余语音，调用方看得见。
        """

        with self._condition:
            return self._finish_timed_out

    @property
    def stall_timeout_s(self) -> float:
        """Seconds of **no progress** after which收尾 gives up (P1)."""

        return self._stall_timeout_s

    @property
    def drain_seconds(self) -> float | None:
        """Wall seconds ``finish()`` spent waiting for the drain (``None`` before).

        Read-only fact: it is the only place where "一条回复实际排空了多久" is
        recorded, so a future batch can compare it against the audio duration
        instead of guessing.
        """

        with self._condition:
            return self._drain_seconds

    @property
    def stall_timeouts(self) -> int:
        """How many drain waits gave up because nothing progressed (P1, additive)."""

        with self._condition:
            return self._stall_timeouts

    @property
    def last_progress(self) -> str | None:
        """Fixed code of the furthest progress reached when收尾 gave up (P1).

        One of ``segment_played`` / ``segment_synthesized`` / ``segment_dequeued`` /
        ``none``. It explains *why* a stall was declared, and it never contains
        sentence text.
        """

        with self._condition:
            return self._last_progress

    @property
    def late_events(self) -> tuple[str, ...]:
        """Redacted diagnostics: post-stop-boundary events and refused pushes.

        Every entry is ``<kind>@generationN`` with a short kind only; no sentence
        text, PCM byte, path or URL can appear here. Routing diagnostics add two
        kinds beside the playback ones: ``edge_used`` when the configured Edge
        provider re-rendered the current sentence and ``edge_failed`` when that
        single attempt did not produce audio.
        """

        with self._condition:
            return tuple(self._late_events)

    @property
    def edge_attempts(self) -> int:
        """Read-only count of sentences handed to the injected Edge provider.

        At most one attempt is made per sentence (never a loop or a retry), and
        the count stays ``0`` for every session built without an
        ``edge_provider``, which keeps the pre-Edge behaviour observable.
        """

        with self._condition:
            return self._edge_attempts

    @property
    def edge_failures(self) -> int:
        """Read-only count of Edge attempts that produced no playable audio.

        A failure routes the reply to text through ``router.edge_failed(...)`` and
        stops its remaining speech; no third provider is ever consulted.
        """

        with self._condition:
            return self._edge_failures

    def _record_late(self, kind: str) -> None:
        """Record a late or refused event; it may never resume or reach playback."""

        with self._condition:
            self._late_events.append(f"{kind}@generation{self._generation}")

    def _reject_push_locked(self) -> None:
        """Count and record one refused push instead of dropping it silently.

        Called with ``self._condition`` already held (it is an ``RLock``, so the
        nested ``_record_late`` is safe). ``late_events`` keeps only the refusal
        reason and the current generation — never the pushed text — so a caller
        can distinguish "the Agent kept talking into a finished reply" from "the
        reply had already ended" without any text being retained. Only terminal
        states are refusals now: an over-full queue waits instead (``_enqueue``).
        """

        if self._closed:
            reason = "closed"
        elif self._failed:
            reason = "failed"
        elif self._cancelled:
            reason = "cancelled"
        else:
            reason = "finished"
        self._dropped_pushes += 1
        self._record_late(f"push_rejected_{reason}")

    @property
    def timing_samples(self) -> tuple[SentenceTimingSample, ...]:
        """One redacted sample per sentence that reached completed playback.

        Exactly one sample exists per completed sentence (never two for the same
        sentence), samples are appended in playback order, and cancelled,
        backpressured or failed sentences contribute nothing — their late events
        stay in ``late_events`` instead. The samples stay readable after
        ``close()``. A sample carries no sentence text, no PCM bytes, no path and
        no URL: only the redacted reply id, the provider generation, the segment
        index and the millisecond spans. ``first_pcm_to_device_ms`` includes the
        configured start-buffer accumulation and is diagnostics only; judge the
        exclusive span with ``gate_to_device_ms``, and read the synthesis/first
        audio span from ``submit_to_first_pcm_ms``.
        """

        with self._condition:
            return tuple(self._timing_samples)

    def timing_percentiles(self) -> StreamingTimingPercentiles:
        """Nearest-rank ``count/p50/p95`` (milliseconds) for all four spans.

        Members with a missing ``None`` endpoint are excluded from that segment's
        ``count``; an empty segment reports ``count=0`` with ``p50``/``p95`` both
        ``None``. The rank rule is fixed by :func:`nearest_rank_percentile`.

        Reading discipline: ``first_pcm_to_device`` contains the configured
        start-buffer accumulation, so a value from it must **not** be compared
        with the plan's 50 ms extra-cost criterion. That comparison belongs to
        ``gate_to_device``, whose clock starts only once the gate is already open.
        The plan's third/fourth criteria (``auto`` vs ``http_wav`` median start
        latency) are read from ``submit_to_first_pcm``. Samples taken under an
        injected clock self-check the instrumentation; they are not a real
        performance result, and real numbers need a separately authorized warm
        20-sentence run.
        """

        with self._condition:
            samples = tuple(self._timing_samples)
        return StreamingTimingPercentiles(
            ready_to_submit=_timing_percentiles(
                [sample.ready_to_submit_ms for sample in samples]
            ),
            first_pcm_to_device=_timing_percentiles(
                [sample.first_pcm_to_device_ms for sample in samples]
            ),
            gate_to_device=_timing_percentiles(
                [sample.gate_to_device_ms for sample in samples]
            ),
            submit_to_first_pcm=_timing_percentiles(
                [sample.submit_to_first_pcm_ms for sample in samples]
            ),
        )

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    # -------------------------------------------------------------------- public

    def push_text_chunk(self, text: str) -> None:
        """Admit one Agent push, or record a redacted refusal.

        An empty, whitespace-only or non-string push is still ignored silently: it
        carries nothing to speak, and a whitespace-only push must **never** advance
        the chunk sequence (real-machine evidence 2026-10-09, `ws_pcm`: a blank piece
        refused by the segmenter's ``EMPTY_TEXT`` guard desynced the sequence and
        silently discarded the whole tail). A push refused because the reply is
        closed, failed, cancelled or finished is counted in ``dropped_pushes`` and
        recorded as ``push_rejected_<reason>@generationN``; it is never dropped
        without a trace. An over-full pending queue is **not** a refusal any more:
        the push waits for room inside :meth:`_enqueue` and, if the queue-wait budget
        expires, the whole batch is refused with ``streaming_queue_budget_exceeded``
        instead of a silent tail loss.
        """

        if type(text) is not str or not text:
            return
        if not text.strip():
            # 真机证据（2026-10-09，`ws_pcm` 233 字只播 74 字、`error` 为空）：空行经
            # `split_for_push` 会切出**只有 `\n` 的纯空白片**；分段器以 `EMPTY_TEXT` 拒收它，
            # 而拒收**不消费序号**（`_last_sequence` 只在接受时推进），于是本会话的序号永远
            # 超前 1，其后每个 chunk 都撞 `OUT_OF_ORDER` ⇒ 静默丢尾。空白 push 与空 push
            # 一样不含可播内容：直接忽略，绝不推进序号。
            return
        with self._condition:
            if self._finished or self._cancelled or self._failed or self._closed:
                self._reject_push_locked()
                return
            self._submit(text, is_final=False)
            self._condition.notify_all()

    def finish(self) -> tuple[str, ...]:
        """Flush the tail, wait for the bounded join, then report the played text.

        Returns the spoken sentences (unchanged type), still raising the recorded
        session error unless the user cancelled. When the bounded join expires
        while speech is still in flight it does **not** claim the reply was fully
        spoken: ``finish_timed_out`` becomes ``True`` and ``finish_timeout`` is
        appended to ``late_events``. ``close()`` continues to cancel that
        remaining speech, which is the designed cancellation behaviour.

        A tail that cannot be admitted (segmenter budget, or the queue-wait budget
        expiring) is a **recorded fatal error**: it is written to ``error`` by
        :meth:`_fail_locked` and re-raised here, so "``error is None`` but less was
        spoken" is not representable.
        """

        with self._condition:
            already_stopped = self._cancelled or self._closed or self._failed
            if not already_stopped and not self._finished:
                result = self._segmenter.finalize()
                if result.decision is SegmentationDecision.QUEUE_BUDGET_EXCEEDED:
                    raise self._fail_locked(StreamingVoiceSessionError(QUEUE_BUDGET_EXCEEDED))
                if result.accepted:
                    if result.segments:
                        self._dequeue_segments(len(result.segments))
                    self._enqueue(result.segments)
                self._finished = True
                self._condition.notify_all()
            elif already_stopped:
                self._finished = True
                self._condition.notify_all()
        elapsed, stalled = self._await_drain()
        with self._condition:
            self._drain_seconds = elapsed
            if stalled:
                # P1：只有**连续无进展**才会走到这里（``_await_drain`` 每看到一次进展都会
                # 重置停滞计时）。这依然不是"全部播完"：``close()`` 按设计取消剩余语音，
                # 调用方据 ``finish_timed_out`` 如实记 ``speech_truncated``。
                self._finish_timed_out = True
                self._stall_timeouts += 1
                self._last_progress = self._progress_stage_locked()
        if stalled:
            self._record_late("finish_timeout")
        self._finalize_reply()
        with self._condition:
            error = self._error
            played = tuple(self._played)
            user_cancelled = self._cancelled
        if error is not None and not user_cancelled:
            raise error
        return played

    def cancel(self) -> None:
        """Stop this reply in the fixed order: mark, stop submitting, drop queues,
        advance the provider generation, stop device playback, then let any late
        event be recorded as a diagnostic only. Idempotent."""

        with self._condition:
            if self._cancelled:
                return
            self._cancelled = True
            self._cancel_event.set()
            self._finished = True
            self._pending.clear()
            # Old-generation chunks can no longer be accepted as current audio.
            self._generation += 1
            self._condition.notify_all()
        try:
            self._worker.stop(timeout=self._stop_timeout_s)
        except Exception:  # noqa: BLE001 - cancelling is best effort
            pass
        self._finalize_reply()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
        self.cancel()
        if self._thread.is_alive():
            self._thread.join(timeout=max(self._stop_timeout_s + 1.0, 2.0))
        if self._owns_worker:
            try:
                self._worker.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    # ------------------------------------------------------------------ internal

    # ------------------------------------------------------- drain / stall (P1)

    def _progress_mark_locked(self) -> tuple[int, int, int, int]:
        """四个只增不减的进展计数快照（调用方必须已持有 ``self._condition``）。

        - ``_dequeued_segments``：段已离开分段器（推入阶段在推进）；
        - ``_synthesized_segments``：provider 已产出音频（合成在推进）；
        - ``len(self._played)``：句段已播完并走进设备（播放在推进）；
        - ``_segment_index``：句段已开始提交（合成→播放的交接在推进）。

        任一项变大就说明"这条流水线还在动"，因此都算进展。绝不使用"队列长度下降"这种
        会被取消路径影响的量，也不读任何文本。
        """

        return (
            self._dequeued_segments,
            self._synthesized_segments,
            len(self._played),
            self._segment_index,
        )

    def _progress_stage_locked(self) -> str:
        """最远进展阶段的固定短码（只用于事实上报，绝不参与任何判定）。"""

        if self._played:
            return "segment_played"
        if self._synthesized_segments:
            return "segment_synthesized"
        if self._dequeued_segments:
            return "segment_dequeued"
        return "none"

    def _await_drain(self) -> tuple[float, bool]:
        """等排空，判据是「**停滞**」而不是「总时长」。

        返回 ``(elapsed_seconds, stalled)``。实现刻意**不分片轮询**：每次正好阻塞
        ``stall_timeout_s``，醒来后看进展标记有没有变——变了就再等一个预算，没变才判停滞。
        分片会引入最多一个切片的超时溢出（对 0.05 s 级预算就是 100 %，会把「有界收尾」
        推到 worker 自己的超时分支上）；而整段阻塞既没有溢出，也不会在回复完成时多等
        （``join`` 在线程结束的瞬间就返回）。

        旧实现用 ``join(timeout=segment_timeout_s)``：只要尾部剩余音频的播放墙钟超过那个
        上限就被判超时，``SessionStream.finish()`` 的 ``finally: close()`` 随后取消仍在播放
        的语音——真机「长回复只念开头」的结构性来源（离线复现见
        ``tests/tts/test_streaming_drain_stall.py``）。
        """

        started = monotonic_seconds()
        with self._condition:
            mark = self._progress_mark_locked()
        while True:
            self._thread.join(timeout=self._stall_timeout_s)
            if not self._thread.is_alive():
                return monotonic_seconds() - started, False
            with self._condition:
                current = self._progress_mark_locked()
            if current == mark:
                return monotonic_seconds() - started, True
            mark = current

    def _submit(self, text: str, *, is_final: bool) -> None:
        self._sequence += 1
        result = self._segmenter.submit(
            TextChunk(
                chunk_id=f"chunk-{self._sequence}",
                reply_id=self._reply_id,
                sequence=self._sequence,
                text=text,
                is_final=is_final,
            )
        )
        if result.decision is SegmentationDecision.QUEUE_BUDGET_EXCEEDED:
            # Refused **as a whole** by the segmenter; the reply ends with the
            # explicit error instead of a partially accepted push.
            raise self._fail_locked(StreamingVoiceSessionError(QUEUE_BUDGET_EXCEEDED))
        if result.decision is SegmentationDecision.OUT_OF_ORDER:
            # 序号错位是真正的协议破坏：以前这条分支什么都不做 ⇒ 静默丢尾
            # （真机 2026-10-09，`ws_pcm` 233 字只播 74 字、`error` 为空）。现在一律记录为
            # 致命错误，让"`error is None` 但少播"不再是可表示的状态。
            raise self._fail_locked(StreamingVoiceSessionError(PUSH_OUT_OF_ORDER))
        if result.accepted:
            if result.segments:
                self._dequeue_segments(len(result.segments))
            self._enqueue(result.segments)

    def _dequeue_segments(self, count: int) -> tuple[SentenceSegment, ...]:
        """``ReplySegmenter.dequeue`` plus the read-only dequeue counters.

        The counting happens here — right after the segmenter gave the segments up
        and *before* :meth:`_enqueue` sees them — so a batch that never reaches
        ``_pending`` is still visible as "dequeued". The returned tuple is exactly
        what ``dequeue`` returned; every existing caller contract is unchanged and
        no waiting, ordering or decision is added.
        """

        segments = self._segmenter.dequeue(count)
        with self._condition:
            self._dequeued_segments += len(segments)
            self._dequeued_characters += sum(len(segment.text) for segment in segments)
        return segments

    def _fail_locked(self, exc: Exception) -> Exception:
        """Record a fatal session error (idempotent) and return it for raising.

        Called with ``self._condition`` already held. ``_failed`` makes the
        consumer thread stop and makes ``finish()`` re-raise instead of reporting
        a truncated reply as a success; ``_error`` keeps the **first** error, so
        the root cause is never masked by a later one.
        """

        with self._condition:
            if self._error is None:
                self._error = exc
            self._failed = True
            self._cancel_event.set()
            self._condition.notify_all()
        return exc

    def _note_enqueue_dropped_locked(self, batch: list[_PendingSentence]) -> None:
        """Count one silently dropped batch (called with ``self._condition`` held).

        The batch was already dequeued from the segmenter and is now discarded
        because the session is cancelled, failed or closed. Only diagnostics are
        added here: the calling branch keeps returning exactly as before, without
        raising and without writing a late event.
        """

        self._enqueue_dropped_segments += len(batch)
        self._enqueue_dropped_characters += sum(len(item.text) for item in batch)

    def _enqueue(self, segments: tuple[object, ...]) -> None:
        """Admit one whole batch, waiting for room instead of dropping the tail.

        The batch is atomic: either every sentence of it enters ``self._pending``
        or none of them does. When the queue would exceed
        ``max_pending_segments`` the caller waits on ``self._condition`` — which
        releases the lock, so the consumer thread can keep popping — until the
        consumer made room, bounded by ``queue_wait_timeout_s``.

        On timeout the **whole batch is refused** and the reply ends with the
        explicit ``streaming_queue_budget_exceeded`` error (``_error``/``_failed``
        are written before raising, so ``error is None`` with less spoken is not
        representable). ``overloaded`` only records that the bound was hit: it is
        never a reason to drop a sentence and never an exit condition of ``_run``.

        The wait deadline uses the module's real monotonic clock on purpose
        instead of the injectable span clock: the bound must still hold when a
        caller injects a clock that does not advance (test doubles do exactly
        that), and a wait whose timeout could freeze is not a bound.

        ``sentence_ready_at`` is stamped here: the moment a complete sentence
        enters the pending synthesis queue.

        Read-only evidence added on top (no decision, timeout or ordering moves):
        a batch admitted into ``_pending`` bumps ``enqueued_*``, while a batch
        discarded by one of the terminal-state early returns below bumps
        ``enqueue_dropped_*``. Those two branches keep their exact previous
        behavior — no raise, no late event, no new wait.
        """

        if not segments:
            return
        ready_at = self._clock()
        batch = [
            _PendingSentence(text=segment.text, ready_at=ready_at)  # type: ignore[attr-defined]
            for segment in segments
        ]
        with self._condition:
            if self._cancelled or self._failed or self._closed:
                self._note_enqueue_dropped_locked(batch)
                return
            deadline = monotonic_seconds() + self._queue_wait_timeout_s
            while self._pending and len(self._pending) + len(batch) > self._max_pending:
                remaining = deadline - monotonic_seconds()
                if remaining <= 0.0:
                    self._overloaded = True
                    self._record_late("queue_budget_wait_timeout")
                    raise self._fail_locked(StreamingVoiceSessionError(QUEUE_BUDGET_EXCEEDED))
                self._condition.wait(timeout=remaining)
                if self._cancelled or self._failed or self._closed:
                    self._note_enqueue_dropped_locked(batch)
                    return
            self._pending.extend(batch)
            self._enqueued_segments += len(batch)
            self._enqueued_characters += sum(len(item.text) for item in batch)
            self._accepted_segments += len(batch)
            self._accepted_characters += sum(len(item.text) for item in batch)
            if len(self._pending) > self._max_pending:
                # A single batch may legitimately be larger than the bound — there
                # was nothing to wait for — and it is still admitted in full. The
                # fact is recorded, never acted on by dropping speech.
                self._overloaded = True
            self._condition.notify_all()

    def _finalize_reply(self) -> None:
        with self._condition:
            if self._fallback_router is None or not self._reply_open:
                return
            self._reply_open = False
        try:
            self._fallback_router.finish_reply(self._reply_id)
        except Exception:  # noqa: BLE001 - routing bookkeeping must not break speech
            pass

    def _run(self) -> None:
        """Consumer loop with exactly two exits.

        (a) the reply was explicitly finished — or cancelled/failed — **and** the
        pending queue is drained; (b) a fatal error was caught below, which writes
        ``_error``/``_failed`` and lets ``finish()`` re-raise it.

        ``self._overloaded`` is deliberately **not** an exit condition: it only
        records that the pending bound was reached, and exiting on it is precisely
        how accepted-but-unspoken sentences used to be discarded while the reply
        still reported success.
        """

        try:
            while True:
                with self._condition:
                    while not self._pending:
                        if self._cancelled or self._failed or self._finished:
                            return
                        self._condition.wait(timeout=0.1)
                    pending = self._pending.pop(0)
                    self._condition.notify_all()
                self._speak(pending)
        except Exception as exc:  # noqa: BLE001 - a session failure must be recorded
            with self._condition:
                self._error = exc
                self._failed = True
                self._cancel_event.set()
                self._pending.clear()
                self._condition.notify_all()

    def _verify_once(self) -> None:
        """Verify the VoiceProfile/Provider identity at most once per reply.

        VoiceProfile/Provider changes may only take effect on a sentence or reply
        boundary (never mid-sentence, never mid-reply), so verifying once when the
        reply starts already satisfies that boundary; a new reply runs in a new
        session and therefore verifies again exactly once.

        The previous per-sentence re-check ran ``verify_reference`` +
        ``verify_provider`` for every sentence — two HTTP round-trips
        (``/profiles`` and ``/profiles/{id}/audio``, the latter also hashing the
        reference audio) measured at 18.4 / 29.7 / 56.4 ms (min/p50/max) — and
        that cost landed inside ``ready_to_submit``, the span the plan requires to
        stay below 20 ms at p95.

        A failure still raises from here and follows the unchanged fail-closed
        path: ``_run`` records ``_error`` and ``_failed``, nothing is played and
        no timing sample is produced. The verifier is never called while
        ``self._condition`` is held.
        """

        with self._condition:
            if self._verified:
                return
        self._verifier(self.selection)
        with self._condition:
            self._verified = True

    def _speak(self, pending: _PendingSentence) -> None:
        sentence = pending.text
        self._verify_once()
        if self._cancel_event.is_set():
            return
        with self._condition:
            self._segment_index += 1
            index = self._segment_index
            generation = self._generation
        request = VoiceStudioSentenceRequest(
            reply_id=self._reply_id,
            segment_id=f"segment-{index}",
            provider_request_id=f"stream-{self._reply_id}-{index}",
            provider_generation=generation,
            text=sentence,
            profile_id=self.selection.profile_id,
            language=self._language,
            speed=self.selection.speed,
        )
        ticket = self._worker.begin_sentence(request)
        with self._condition:
            self._active_ticket = ticket
        pushed = 0
        # ``tts_submitted_at``: the instant the sentence is handed to the
        # transport. The router/provider only start running on the first
        # ``next()`` below, so this is the closest measurable submission point
        # without reaching into transport internals.
        submitted_at = self._clock()
        first_pcm_at: float | None = None
        try:
            for chunk in self._transport_router.stream_sentence(
                request, cancelled=self._cancel_event
            ):
                if self._cancel_event.is_set():
                    # Audio that arrives after the stop boundary is diagnostics
                    # only; it can never resume playback.
                    self._record_late("pcm_chunk")
                    return
                if first_pcm_at is None:
                    # ``first_pcm_at``: the first PCM block of this sentence came
                    # back from the provider.
                    first_pcm_at = self._clock()
                try:
                    if self._worker.push_chunk(chunk):
                        pushed += 1
                except PCMPlaybackBackpressure:
                    # The device is already 8 s behind. Stop this reply's remaining
                    # speech and keep the complete text instead of queueing more or
                    # blocking the Agent thread.
                    with self._condition:
                        self._overloaded = True
                    self._cancel_event.set()
                    self._record_late("audio_backpressure")
                    return
        except VoiceStudioTransportError as exc:
            if self._cancel_event.is_set():
                self._record_late(f"transport:{exc.code}")
                return
            decision = self._handle_transport_failure(request, exc, pushed=pushed)
            if not self._edge_may_render(decision, request):
                # Unchanged fail-closed path: VoiceStudio audio already reached
                # the device, Edge is unusable, or this sentence was already
                # attempted. Nothing is replayed and nothing is retried.
                raise
            edge_result = self._speak_over_edge(request, pushed=pushed)
            if edge_result is None:
                if self._cancel_event.is_set():
                    return
                # Edge also failed: the reply stops here in text mode, the
                # original transport failure stays the recorded error and no
                # third provider is consulted.
                raise
            first_pcm_at, pushed = edge_result
        finally:
            with self._condition:
                self._active_ticket = None
        if first_pcm_at is not None:
            # ``synthesized``: the provider produced at least one PCM block for
            # this sentence. Counted even when the reply is cancelled right after,
            # because "audio was generated" is an independent fact from "audio was
            # played" — keeping the two counters apart is what makes a short
            # playback visible.
            with self._condition:
                self._synthesized_segments += 1
        if self._cancel_event.is_set():
            self._record_late("sentence_completed_after_cancel")
            return
        state = self._worker.wait(ticket, self._segment_timeout_s)
        if self._cancel_event.is_set():
            self._record_late(f"playback:{state.value}")
            return
        if state is not PlaybackState.COMPLETED:
            raise StreamingVoiceSessionError(f"streaming_playback_not_completed:{state.value}")
        # ``device_submit_at``: read back from the playback worker, which stamps the
        # instant this sentence's first PCM batch was handed to the backend. The
        # read is a pure observation and never mutates playback state.
        device_submit_at = self._worker.device_submit_at(request)
        # ``gate_open_at``: the worker's separate stamp for the instant its
        # start-buffer gate first opened. ``first_pcm_to_device_ms`` above already
        # contains that accumulation and stays a raw diagnostic; the exclusive
        # extra cost is ``gate_to_device_ms``, and when both worker stamps are
        # equal it is honestly ``0.0``.
        gate_open_at = self._worker.device_gate_open_at(request)
        sample = SentenceTimingSample(
            reply_id=self._reply_id,
            generation=generation,
            segment_index=index,
            ready_to_submit_ms=_elapsed_ms(pending.ready_at, submitted_at),
            first_pcm_to_device_ms=_elapsed_ms(first_pcm_at, device_submit_at),
            first_pcm_to_gate_ms=_elapsed_ms(first_pcm_at, gate_open_at),
            gate_to_device_ms=_elapsed_ms(gate_open_at, device_submit_at),
            # ``submit_to_first_pcm_ms``: the synthesis/first-audio span, from the
            # sentence handed to the transport to the first PCM block coming back.
            # This is the segment the plan's auto-vs-http_wav median start-latency
            # comparison (criteria 3/4) is read from; it is exactly ``None`` when
            # the sentence never produced audio.
            submit_to_first_pcm_ms=_elapsed_ms(submitted_at, first_pcm_at),
        )
        with self._condition:
            self._played.append(sentence)
            # A cancelled session must never publish a normal sample; the played
            # bookkeeping above keeps its pre-existing semantics untouched.
            if not self._cancelled:
                self._timing_samples.append(sample)

    def _handle_transport_failure(
        self, request: VoiceStudioSentenceRequest, exc: VoiceStudioTransportError, *, pushed: int
    ) -> TTSRoutingDecision | None:
        """Record the VoiceStudio failure and return the router's decision.

        The returned decision is the only authority on whether the Edge slot may
        re-render this sentence; ``None`` means no usable routing decision exists
        (no router, or a routing rejection) and the existing text-only path stays.
        """
        # Any accepted PCM may already be playing, so the honest state is
        # PLAYBACK_STARTED; only a sentence that never produced audio may be
        # re-rendered by another provider.
        state = (
            ProviderRequestPlaybackState.PLAYBACK_STARTED
            if pushed
            else ProviderRequestPlaybackState.NOT_COMMITTED
        )
        with self._condition:
            self._failure_states[request.segment_id] = state
        router = self._fallback_router
        if router is None:
            return None
        try:
            decision = router.primary_failed(
                request.reply_id, request.segment_id, playback_state=state
            )
        except Exception:  # noqa: BLE001 - routing rejections must not mask the failure
            return None
        with self._condition:
            self._routing_decision = decision
        return decision

    def _edge_may_render(
        self, decision: TTSRoutingDecision | None, request: VoiceStudioSentenceRequest
    ) -> bool:
        """True only for the one allowed Edge re-render of an unplayed sentence.

        The router decides this: ``USE_EDGE_CURRENT_SEGMENT`` is returned exactly
        when the sentence never started playing and Edge is configured. This
        method adds the session facts — an injected provider and a per-sentence
        one-attempt bound — and never inspects text.
        """
        if self._edge_provider is None or decision is None:
            return False
        if decision.action is not TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT:
            return False
        if self._cancel_event.is_set():
            return False
        with self._condition:
            return request.provider_request_id not in self._edge_attempted

    def _speak_over_edge(
        self, request: VoiceStudioSentenceRequest, *, pushed: int
    ) -> tuple[float | None, int] | None:
        """Re-render the current sentence once with the injected Edge provider.

        Returns ``(first_pcm_at, pushed)`` when every Edge chunk reached the same
        playback worker, or ``None`` when Edge could not produce the sentence, the
        session was cancelled, or device backpressure stopped the reply. A failed
        attempt records ``edge_failed``, asks the router for the text decision and
        never tries another provider. No chunk arriving after cancellation is
        pushed: it is recorded as a redacted late event like the VoiceStudio path.
        """
        edge = self._edge_provider
        if edge is None:
            return None
        if self._cancel_event.is_set():
            return None
        with self._condition:
            self._edge_attempted.add(request.provider_request_id)
            self._edge_attempts += 1
        first_pcm_at: float | None = None
        try:
            for chunk in edge.stream_sentence(request, cancelled=self._cancel_event):
                if self._cancel_event.is_set():
                    # Late audio is diagnostics only; it can never resume playback.
                    self._record_late("pcm_chunk")
                    return None
                if first_pcm_at is None:
                    first_pcm_at = self._clock()
                try:
                    if self._worker.push_chunk(chunk):
                        pushed += 1
                except PCMPlaybackBackpressure:
                    with self._condition:
                        self._overloaded = True
                    self._cancel_event.set()
                    self._record_late("audio_backpressure")
                    return None
        except Exception:  # noqa: BLE001 - any Edge failure falls back to text
            if self._cancel_event.is_set():
                self._record_late("edge_cancelled")
                return None
            self._handle_edge_failure(request, pushed=pushed)
            return None
        self._record_late("edge_used")
        return first_pcm_at, pushed

    def _handle_edge_failure(self, request: VoiceStudioSentenceRequest, *, pushed: int) -> None:
        """Record one Edge failure and take the router's text decision.

        The message of whatever the Edge provider raised is deliberately dropped:
        diagnostics stay a short code, a counter and one redacted late event.
        """
        state = (
            ProviderRequestPlaybackState.PLAYBACK_STARTED
            if pushed
            else ProviderRequestPlaybackState.NOT_COMMITTED
        )
        with self._condition:
            self._edge_failures += 1
        self._record_late("edge_failed")
        router = self._fallback_router
        if router is None:
            return
        try:
            decision = router.edge_failed(
                request.reply_id, request.segment_id, playback_state=state
            )
        except Exception:  # noqa: BLE001 - routing rejections must not mask the failure
            return
        with self._condition:
            self._routing_decision = decision


def build_streaming_session(
    selection: VoiceSelection,
    *,
    mode: str,
    ws_provider: object,
    http_provider: object,
    backend_factory: Callable[[], object],
    fallback_router: TTSFallbackRouter | None = None,
    edge_provider: object | None = None,
    start_buffer_ms: int = 160,
    max_queued_audio_ms: int = DEFAULT_PCM_QUEUE_BUDGET_MS,
    stop_timeout: float = 1.0,
    max_pending_segments: int = 8,
    queue_wait_timeout_s: float | None = None,
    stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S,
    language: str = "Chinese",
    verifier: Callable[[VoiceSelection], None] | None = None,
    clock: Callable[[], float] = monotonic_seconds,
) -> StreamingVoiceSession:
    """Assemble an opt-in streaming session from explicit, injected parts.

    Nothing is read from the environment or the default runtime here; callers
    must pass both providers and the device backend factory explicitly. The
    optional ``edge_provider`` is the Edge slot of the injected fallback router's
    decision and is only ever consulted when that router allows re-rendering the
    current, not-yet-playing sentence; it stays unused by default, is not owned
    (never closed) by the session, and ``clock``
    defaults to the shared ``app.tts.clock.monotonic_seconds`` (``perf_counter``
    on Windows, ``monotonic`` elsewhere) and, when injected, keeps priority and is
    shared by the router, the playback worker and the session so every timing
    stamp sits on one monotonic basis with a resolution that can actually resolve
    the worker-side spans.

    ``max_queued_audio_ms`` defaults to :data:`DEFAULT_PCM_QUEUE_BUDGET_MS`
    (20 000 ms = one longest 48-character segment at the pinned 4.8
    characters/second, times the required 2x margin) instead of the previous
    8 000 ms, which was **shorter than a single segment** and therefore turned the
    device queue itself into a truncation source. The segmentation character limit
    is deliberately unchanged. ``queue_wait_timeout_s`` bounds the waiting
    backpressure of the pending-sentence queue; ``None`` uses the session's
    ``segment_timeout_s``.
    """

    router = VoiceStudioTransportRouter(
        mode=mode, ws_provider=ws_provider, http_provider=http_provider, clock=clock
    )
    worker = PCMPlaybackWorker(
        backend_factory,
        start_buffer_ms=start_buffer_ms,
        max_queued_audio_ms=max_queued_audio_ms,
        stop_timeout=stop_timeout,
        clock=clock,
    )
    return StreamingVoiceSession(
        selection,
        transport_router=router,
        worker=worker,
        fallback_router=fallback_router,
        edge_provider=edge_provider,
        verifier=verifier,
        max_pending_segments=max_pending_segments,
        queue_wait_timeout_s=queue_wait_timeout_s,
        stall_timeout_s=stall_timeout_s,
        language=language,
        owns_worker=True,
        clock=clock,
    )


__all__ = [
    "DEFAULT_PCM_QUEUE_BUDGET_MS",
    "DEFAULT_STALL_TIMEOUT_S",
    "MAX_SEGMENT_CHARACTERS",
    "PCM_QUEUE_BUDGET_MARGIN",
    "PUSH_OUT_OF_ORDER",
    "QUEUE_BUDGET_EXCEEDED",
    "SPEECH_CHARACTERS_PER_SECOND",
    "SentenceTimingSample",
    "StreamingTimingPercentiles",
    "StreamingVoiceSession",
    "StreamingVoiceSessionError",
    "TimingPercentiles",
    "build_streaming_session",
    "minimum_pcm_queue_budget_ms",
    "nearest_rank_percentile",
    "speech_seconds_for_characters",
]
