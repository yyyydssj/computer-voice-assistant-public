"""Offline sentence-level timing instrumentation tests for the streaming session.

Everything here is offline: no socket, no subprocess, no audio device and no real
clock. A scripted monotonic clock is injected into the session, the transport
router and the playback worker, and every stamp is written by a deterministic
hook that runs on the very thread that is about to read it, so the expected
millisecond values are hand-computed and exact.

The exact arithmetic rests on ``TICK_S = 2 ** -10`` seconds (= 0.9765625 ms):
every stamped value is that integer tick times a dyadic constant, so every
difference and every ``* 1000.0`` is exactly representable in a double and
``assertEqual`` is a valid assertion rather than an approximation.

These tests prove the instrumentation and the percentile definition only. They
do not measure, prove or claim the plan section 19.2 latency targets (p95 < 20 ms
and < 50 ms); that needs a separately authorized 20-sentence warm run on real
hardware.

Reading discipline under test: ``first_pcm_to_device_ms`` contains the configured
start-buffer accumulation, so it must **never** be compared with the 50 ms
"extra cost excluding the start buffer" criterion. That criterion belongs to
``gate_to_device_ms`` (``gate_open_at -> device_submit_at``), whose clock starts
only once the worker's start-buffer gate is already open. ``first_pcm_to_gate_ms``
(``first_pcm_at -> gate_open_at``) is the start-buffer accumulation itself and is
diagnostics only. ``submit_to_first_pcm_ms`` (``tts_submitted_at ->
first_pcm_at``) is the synthesis/first-audio span that the plan's ``auto`` vs
``http_wav`` median start-latency criteria are read from.

Per-sentence hook placement follows the production code: identity verification now
runs **once per reply** (defect-B behaviour), so ``on_verify`` only releases the
gate, while the per-sentence ``ready -> submit`` stamp is taken from
``PCMPlaybackWorker.begin_sentence`` — still on the session thread and still
immediately before ``tts_submitted_at = self._clock()``.
"""
from __future__ import annotations

import dataclasses
import getpass
import os
import threading
import unittest
from threading import Event
from typing import Callable

from app.tts.pcm_playback import PCMPlaybackWorker
from app.tts.playback_worker import StopResult
from app.tts.private_selection import (
    CONFIRMED_PROFILE,
    CONFIRMED_REFERENCE_SHA256,
    VoiceSelection,
)
from app.tts.providers import PCMChunk, VoiceStudioSentenceRequest
from app.tts.streaming_session import (
    SentenceTimingSample,
    StreamingVoiceSession,
    nearest_rank_percentile,
)
from app.tts.transport import VoiceStudioTransportRouter

SAMPLE_RATE = 24_000
TICK_S = 2 ** -10            # 0.0009765625 s, exactly representable
TICK_MS = TICK_S * 1000.0    # 0.9765625 ms, exact
READY_VALUE = 0.0
BLOCK_MS = 20
SENTENCE_COUNT = 20
# Per sentence: (ticks ready -> submit, ticks submit -> first PCM, ticks first PCM -> device).
# ``10 * index`` keeps the scripted clock globally monotonic across sentences.
SENTENCE_SCRIPT: tuple[tuple[int, int, int], ...] = tuple(
    (10 * index, 1, (index % 5) + 1) for index in range(1, SENTENCE_COUNT + 1)
)
# Hand-computed hand-check table below (ticks):
#   ready_to_submit_ms(index)      = 10 * index * TICK_MS
#   first_pcm_to_device_ms(index)  = ((index % 5) + 1) * TICK_MS
#   submit_to_first_pcm_ms(index)  = 1 * TICK_MS (constant on this timeline)
#   ready_to_submit p50 = rank ceil(0.5 * 20)  = 10 -> 100 * TICK_MS =  97.65625 ms
#   ready_to_submit p95 = rank ceil(0.95 * 20) = 19 -> 190 * TICK_MS = 185.546875 ms
#   device p50          = rank ceil(0.5 * 20)  = 10 ->   3 * TICK_MS =   2.9296875 ms
#   device p95          = rank ceil(0.95 * 20) = 19 ->   5 * TICK_MS =   4.8828125 ms

# ------------------------------------------------------------------ synthesis
# The dedicated synthesis/first-audio timeline for the fourth segment: the
# ``submit -> first PCM`` step varies so its p50 and p95 are provably different
# values, and the ``first PCM -> device`` step is a different series again.
SYNTHESIS_SENTENCE_COUNT = 20
SYNTHESIS_SCRIPT: tuple[tuple[int, int, int], ...] = tuple(
    (10 * index, (index % 4) + 1, (index % 3) + 1)
    for index in range(1, SYNTHESIS_SENTENCE_COUNT + 1)
)
# Hand-checked table (ticks):
#   submit_to_first_pcm_ms(index) = ((index % 4) + 1) * TICK_MS
#     -> {1,2,3,4} five times each; sorted [1]*5+[2]*5+[3]*5+[4]*5
#     -> p50 = rank ceil(0.5 * 20)  = 10 -> 2 * TICK_MS = 1.953125 ms
#     -> p95 = rank ceil(0.95 * 20) = 19 -> 4 * TICK_MS = 3.90625 ms
#   first_pcm_to_device_ms(index) = ((index % 3) + 1) * TICK_MS
#     -> {1,2,3} with counts {6,7,7} -> p50 = 2 ticks, p95 = 3 ticks
#   ready_to_submit_ms(index) = 10 * index * TICK_MS (as in SENTENCE_SCRIPT)

# ------------------------------------------------------------------ gate script
# Two blocks per sentence, so the start-buffer gate can only be satisfied by the
# final block; ``GATE_START_BUFFER_MS`` therefore sits above the first block
# (10 ms) and the second block opens the gate through ``is_final``.
GATE_SENTENCE_COUNT = 20
GATE_FIRST_BLOCK_MS = 10
GATE_SECOND_BLOCK_MS = 10
GATE_START_BUFFER_MS = 15
# Per sentence: (ready -> submit ticks, submit -> first PCM ticks,
#                first PCM -> gate ticks, gate -> device ticks).
GATE_SCRIPT: tuple[tuple[int, int, int, int], ...] = tuple(
    (10 * index, 1, (index % 5) + 1, (index % 3) + 1)
    for index in range(1, GATE_SENTENCE_COUNT + 1)
)
# Hand-checked table (ticks):
#   first_pcm_to_gate_ms(index) = ((index % 5) + 1) * TICK_MS   -> {1..5} four times
#   gate_to_device_ms(index)    = ((index % 3) + 1) * TICK_MS   -> 1 six times
#                                  (index%3==0), 2 seven times, 3 seven times
#   first_pcm_to_device_ms(index) = first_pcm_to_gate + gate_to_device
#   gate_to_device  p50 = rank ceil(0.5 * 20)  = 10 -> 2 * TICK_MS =  1.953125 ms
#   gate_to_device  p95 = rank ceil(0.95 * 20) = 19 -> 3 * TICK_MS =  2.9296875 ms
#   first_pcm_to_device 20 values [4,6,5,7,4,3,5,7,6,3,5,4,6,8,2,4,6,5,7,4]:
#     sorted 2,3,3,4,4,4,4,4,5,5,5,5,6,6,6,6,7,7,7,8
#     -> p50 rank 10 = 5 * TICK_MS = 4.8828125 ms, p95 rank 19 = 7 * TICK_MS
#   ready_to_submit p50 = 100 * TICK_MS, p95 = 190 * TICK_MS (unchanged)
# The gate block above the first block also means the worker cannot submit before
# the final block arrives, so the gate stamp always lands on the value written by
# ``on_second_block`` on the session thread, whatever the interleaving.


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


def make_selection() -> VoiceSelection:
    return VoiceSelection(
        profile_id=CONFIRMED_PROFILE,
        owner=getpass.getuser(),
        reference_sha256=CONFIRMED_REFERENCE_SHA256,
    )


class ScriptedClock:
    """Monotonic clock whose value is written by deterministic test hooks."""

    def __init__(self) -> None:
        self.value = READY_VALUE
        self.reads = 0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.reads += 1
            return self.value


class TimedBackend:
    """Immediately-writable backend that lets a hook stamp the submit instant."""

    backend_name = "timed-fake"
    max_buffer_bytes = 9_600

    def __init__(self, harness: "TimingHarness") -> None:
        self.harness = harness
        self.submitted: list[bytes] = []
        self.open_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self._drained = True
        self._lock = threading.Lock()

    def open(self) -> None:
        self.open_calls += 1

    def writable(self) -> bool:
        self.harness.on_device_writable()
        return True

    def submit(self, pcm: bytes) -> None:
        with self._lock:
            self.submitted.append(bytes(pcm))
            self._drained = True

    def poll_drained(self) -> bool:
        with self._lock:
            return self._drained

    def stop(self, timeout: float) -> StopResult:
        del timeout
        self.stop_calls += 1
        with self._lock:
            self._drained = True
        return StopResult(True, "backend_buffer_returned", "timed_fake_reset")

    def close(self) -> None:
        self.close_calls += 1


class TimedProvider:
    """One final PCM block per sentence; moves the clock before the first block."""

    def __init__(self, harness: "TimingHarness") -> None:
        self.harness = harness
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        self.harness.on_stream_start(request)
        yield PCMChunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=1,
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=pcm_bytes(BLOCK_MS),
            is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class GateTimedProvider:
    """Two blocks per sentence; only the second (final) block can open the gate.

    ``on_stream_start`` stamps submit -> first PCM before the first block and
    ``on_second_block`` stamps first PCM -> gate on the session thread before the
    final block. Because the worker's start-buffer gate cannot be satisfied by the
    first block, its gate stamp always reads the value ``on_second_block`` wrote:
    the scripted spans stay exact regardless of thread scheduling.
    """

    def __init__(self, harness: "TimingHarness") -> None:
        self.harness = harness
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.close_calls = 0

    def _chunk(self, request: VoiceStudioSentenceRequest, sequence: int, milliseconds: int,
               *, is_final: bool) -> PCMChunk:
        return PCMChunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=sequence,
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=pcm_bytes(milliseconds),
            is_final=is_final,
        )

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls.append(request)
        self.harness.on_stream_start(request)
        yield self._chunk(request, 1, GATE_FIRST_BLOCK_MS, is_final=False)
        self.harness.on_second_block()
        yield self._chunk(request, 2, GATE_SECOND_BLOCK_MS, is_final=True)

    def close(self) -> None:
        self.close_calls += 1


class CancellingTimedProvider:
    """Yields one block, cancels the session mid-stream, then yields a late block."""

    def __init__(self) -> None:
        self.session: StreamingVoiceSession | None = None
        self.calls = 0
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls += 1
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=1,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=False,
        )
        assert self.session is not None
        self.session.cancel()
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=2,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class BlockingTimedProvider:
    """Emits one block, then blocks until the test thread cancels the session."""

    def __init__(self) -> None:
        self.reached = Event()
        self.resume = Event()
        self.calls = 0
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls += 1
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=1,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=False,
        )
        self.reached.set()
        self.resume.wait(5.0)
        yield PCMChunk(
            reply_id=request.reply_id, segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation, sequence=2,
            sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
            data=pcm_bytes(40), is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class _ObservationUnavailableWorker(PCMPlaybackWorker):
    """Worker that reports no observation stamp for some sentences.

    Both read-only observation surfaces are covered by the same switch:
    ``available`` hides the device-submit stamp, ``gate_available`` hides the
    gate-open stamp and falls back to ``available`` when it is not given, so the
    gate diagnostics and the exclusive ``gate_to_device`` span can be hidden
    independently of each other.

    ``on_begin`` is the per-sentence ``ready -> submit`` hook. It is taken here
    because ``begin_sentence()`` is called by ``_speak()`` on the session thread
    immediately before ``tts_submitted_at = self._clock()``, which is the only
    per-sentence stamp point left now that verification runs once per reply.
    """

    def __init__(self, backend_factory, *, available, gate_available=None,
                 on_begin=None, **kwargs) -> None:
        super().__init__(backend_factory, **kwargs)
        self._available = available
        self._gate_available = available if gate_available is None else gate_available
        self._on_begin = on_begin

    def begin_sentence(self, request: VoiceStudioSentenceRequest):
        if self._on_begin is not None:
            self._on_begin(request)
        return super().begin_sentence(request)

    @staticmethod
    def _index(request: VoiceStudioSentenceRequest) -> int:
        return int(request.segment_id.rsplit("-", 1)[-1])

    def device_submit_at(self, request: VoiceStudioSentenceRequest) -> float | None:
        # A non-request must still raise the base class's TypeError, so the type
        # check comes before the index is read.
        if isinstance(request, VoiceStudioSentenceRequest) and not self._available(
            self._index(request)
        ):
            return None
        return super().device_submit_at(request)

    def device_gate_open_at(self, request: VoiceStudioSentenceRequest) -> float | None:
        if isinstance(request, VoiceStudioSentenceRequest) and not self._gate_available(
            self._index(request)
        ):
            return None
        return super().device_gate_open_at(request)


class TimingHarness:
    """Deterministic offline timeline over one scripted monotonic clock.

    Hook order per sentence is fixed by the production code. Verification runs
    **once per reply**, so ``on_verify`` only releases the gate; the per-sentence
    ``ready -> submit`` step is stamped by ``on_sentence_begin``, which the hooked
    playback worker runs inside ``begin_sentence()`` — on the session thread and
    immediately before the submit stamp. The provider body runs before the first
    PCM stamp, and ``backend.writable()`` runs in the playback worker before the
    device-submit stamp. Each hook therefore writes the clock immediately before
    the matching read on the same thread, which makes every observed value
    independent of thread scheduling.

    ``gate_script`` switches the harness to the two-block timeline: rows become
    ``(ready, submit->first PCM, first PCM->gate, gate->device)`` ticks and
    ``on_second_block`` writes the ``first PCM -> gate`` step before the final
    block, i.e. before the gate can possibly open. ``script`` replaces the
    default single-block timeline (rows ``(ready, submit->first PCM,
    first PCM->device)``) with a custom one; the default timeline stays
    byte-for-byte the one the pre-existing tests use.
    """

    def __init__(
        self,
        case: unittest.TestCase,
        *,
        sentence_count: int = SENTENCE_COUNT,
        available: Callable[[int], bool] | None = None,
        gate_available: Callable[[int], bool] | None = None,
        provider: object | None = None,
        start_buffer_ms: int = 1,
        gate_script: tuple[tuple[int, ...], ...] | None = None,
        script: tuple[tuple[int, ...], ...] | None = None,
    ) -> None:
        self.clock = ScriptedClock()
        if gate_script is None:
            base_script = SENTENCE_SCRIPT if script is None else script
            self.script: tuple[tuple[int, ...], ...] = base_script[:sentence_count]
            self._gate_gap_ticks: list[int] = []
            self._device_ticks = [row[2] for row in self.script]
        else:
            self.script = gate_script[:sentence_count]
            self._gate_gap_ticks = [row[2] for row in self.script]
            self._device_ticks = [row[3] for row in self.script]
        self.sentences = tuple(f"第{index}句。" for index in range(1, sentence_count + 1))
        self.submit_ticks: list[int] = []
        self.stream_segment_ids: list[str] = []
        self.verify_gate = Event()
        self.gate_timeout = False
        self.verify_calls = 0
        self._begin_index = 0
        self._pcm_index = 0
        self._device_index = 0
        self._second_block_index = 0
        self._awaiting_device = False
        self.backend = TimedBackend(self)
        if provider is not None:
            self.provider = provider
        elif gate_script is None:
            self.provider = TimedProvider(self)
        else:
            self.provider = GateTimedProvider(self)
        worker_kwargs = {"start_buffer_ms": start_buffer_ms, "stop_timeout": 0.5, "clock": self.clock}
        worker = _ObservationUnavailableWorker(
            lambda: self.backend,
            available=available if available is not None else (lambda index: True),
            gate_available=gate_available,
            on_begin=self.on_sentence_begin,
            **worker_kwargs,
        )
        self.worker = worker
        self.router = VoiceStudioTransportRouter(
            mode="ws_pcm",
            ws_provider=self.provider,
            http_provider=self.provider,
            clock=self.clock,
        )
        self.session = StreamingVoiceSession(
            make_selection(),
            transport_router=self.router,
            worker=self.worker,
            verifier=self.on_verify,
            max_pending_segments=32,
            clock=self.clock,
        )
        case.addCleanup(self.session.close)
        case.addCleanup(self.worker.close)
        case.addCleanup(self.router.close)

    # ------------------------------------------------------------------- hooks

    def on_verify(self, selection: VoiceSelection) -> None:
        """Session-thread hook: release the gate; runs once per reply.

        Verification is once per reply, so this hook must not stamp the
        per-sentence ``ready -> submit`` step any more — ``on_sentence_begin``
        does that. It only counts the call and opens the gate the first (and
        only) sentence waits for.
        """

        del selection
        self.verify_calls += 1
        if not self.verify_gate.wait(5.0):
            self.gate_timeout = True

    def on_sentence_begin(self, request: VoiceStudioSentenceRequest) -> None:
        """Session-thread hook: stamp ready -> submit before the submit stamp.

        Runs inside the worker's ``begin_sentence()``, i.e. after verification and
        immediately before ``_speak`` reads ``tts_submitted_at``. Requests that do
        not belong to the scripted timeline (a test calling ``begin_sentence``
        directly) are ignored, exactly like an extra sentence would be.
        """

        del request
        index = self._begin_index
        self._begin_index += 1
        if index >= len(self.script):
            return
        ticks = self.script[index][0]
        self.submit_ticks.append(ticks)
        self.clock.value = READY_VALUE + ticks * TICK_S

    def on_stream_start(self, request: VoiceStudioSentenceRequest) -> None:
        """Session-thread hook: stamp submit -> first PCM (runs at first ``next()``)."""

        index = self._pcm_index
        self._pcm_index += 1
        self.stream_segment_ids.append(request.segment_id)
        self.clock.value = self.clock.value + self.script[index][1] * TICK_S
        self._awaiting_device = True

    def on_second_block(self) -> None:
        """Session-thread hook: stamp first PCM -> gate, before the final block."""

        index = self._second_block_index
        self._second_block_index += 1
        self.clock.value = self.clock.value + self._gate_gap_ticks[index] * TICK_S

    def on_device_writable(self) -> None:
        """Worker-thread hook: stamp first PCM -> device submit."""

        if not self._awaiting_device:
            return
        self._awaiting_device = False
        index = self._device_index
        self._device_index += 1
        self.clock.value = self.clock.value + self._device_ticks[index] * TICK_S

    # ------------------------------------------------------------------- drive

    def push_all(self) -> None:
        """Push every sentence, then release the gate so speaking can start."""

        self.clock.value = READY_VALUE
        for sentence in self.sentences:
            self.session.push_text_chunk(sentence)
        self.verify_gate.set()

    def finish(self) -> tuple[str, ...]:
        return self.session.finish()


class StreamingTimingSampleTests(unittest.TestCase):
    def test_twenty_sentences_yield_exact_per_sentence_spans(self) -> None:
        harness = TimingHarness(self)
        harness.push_all()
        played = harness.finish()

        # The instrumentation must not change the audio path or the played result.
        self.assertEqual(played, harness.sentences)
        self.assertEqual(len(harness.backend.submitted), SENTENCE_COUNT)
        self.assertTrue(
            all(len(block) == len(pcm_bytes(BLOCK_MS)) for block in harness.backend.submitted)
        )
        self.assertEqual(
            harness.stream_segment_ids, [f"segment-{index}" for index in range(1, 21)]
        )
        self.assertFalse(harness.gate_timeout)
        self.assertFalse(harness.session.overloaded)

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), SENTENCE_COUNT)
        self.assertEqual([sample.segment_index for sample in samples], list(range(1, 21)))
        self.assertEqual({sample.reply_id for sample in samples}, {harness.session.reply_id})
        self.assertEqual({sample.generation for sample in samples}, {harness.session.generation})
        for index, sample in enumerate(samples, start=1):
            submit_ticks, pcm_ticks, device_ticks = SENTENCE_SCRIPT[index - 1]
            self.assertEqual(sample.ready_to_submit_ms, submit_ticks * TICK_MS)
            self.assertEqual(sample.submit_to_first_pcm_ms, pcm_ticks * TICK_MS)
            self.assertEqual(sample.first_pcm_to_device_ms, device_ticks * TICK_MS)
            # ``start_buffer_ms=1`` is already satisfied by the single 20 ms
            # block, so this timeline's gate opens with the first PCM: the
            # diagnostic span is exactly 0 and the exclusive span is the whole
            # observed device span.
            self.assertEqual(sample.first_pcm_to_gate_ms, 0.0)
            self.assertEqual(sample.gate_to_device_ms, device_ticks * TICK_MS)

    def test_percentiles_use_the_nearest_rank_rule(self) -> None:
        harness = TimingHarness(self)
        harness.push_all()
        harness.finish()

        percentiles = harness.session.timing_percentiles()
        self.assertEqual(percentiles.ready_to_submit.count, 20)
        # rank ceil(0.5 * 20) = 10 -> the 10th smallest of 10*index ticks = 100 ticks
        self.assertEqual(percentiles.ready_to_submit.p50, 100 * TICK_MS)
        # rank ceil(0.95 * 20) = 19 -> 190 ticks
        self.assertEqual(percentiles.ready_to_submit.p95, 190 * TICK_MS)
        self.assertEqual(percentiles.first_pcm_to_device.count, 20)
        # ticks are {1,2,3,4,5} four times -> 10th smallest = 3, 19th smallest = 5
        self.assertEqual(percentiles.first_pcm_to_device.p50, 3 * TICK_MS)
        self.assertEqual(percentiles.first_pcm_to_device.p95, 5 * TICK_MS)
        # The gate opens with the first PCM on this timeline, so the exclusive
        # segment repeats the raw one exactly — the two definitions coincide only
        # here, never in general.
        self.assertEqual(percentiles.gate_to_device.count, 20)
        self.assertEqual(percentiles.gate_to_device.p50, 3 * TICK_MS)
        self.assertEqual(percentiles.gate_to_device.p95, 5 * TICK_MS)
        # The synthesis segment is constant on this timeline (1 tick per sentence),
        # so its p50 and p95 are both that one observed value.
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 1 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 1 * TICK_MS)

    def test_each_sentence_is_counted_exactly_once(self) -> None:
        harness = TimingHarness(self)
        harness.push_all()
        harness.finish()

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), len({sample.segment_index for sample in samples}))
        self.assertEqual(len(samples), len(harness.sentences))
        self.assertEqual(
            len(harness.session.played_segments), len(samples)
        )

    def test_unavailable_device_observation_leaves_only_missing_spans(self) -> None:
        harness = TimingHarness(self, available=lambda index: False)
        harness.push_all()
        harness.finish()

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), SENTENCE_COUNT)
        for index, sample in enumerate(samples, start=1):
            self.assertEqual(sample.ready_to_submit_ms, 10 * index * TICK_MS)
            # The synthesis span comes from the session's own two stamps, so it
            # survives a missing worker observation like nothing happened.
            self.assertEqual(sample.submit_to_first_pcm_ms, 1 * TICK_MS)
            self.assertIsNone(sample.first_pcm_to_device_ms)
            # The same switch hides the gate observation surface too.
            self.assertIsNone(sample.first_pcm_to_gate_ms)
            self.assertIsNone(sample.gate_to_device_ms)
        percentiles = harness.session.timing_percentiles()
        self.assertEqual(percentiles.ready_to_submit.count, 20)
        self.assertEqual(percentiles.ready_to_submit.p50, 100 * TICK_MS)
        self.assertEqual(percentiles.ready_to_submit.p95, 190 * TICK_MS)
        self.assertEqual(percentiles.first_pcm_to_device.count, 0)
        self.assertIsNone(percentiles.first_pcm_to_device.p50)
        self.assertIsNone(percentiles.first_pcm_to_device.p95)
        self.assertEqual(percentiles.gate_to_device.count, 0)
        self.assertIsNone(percentiles.gate_to_device.p50)
        self.assertIsNone(percentiles.gate_to_device.p95)
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 1 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 1 * TICK_MS)

    def test_missing_segments_are_excluded_from_count_and_ranks(self) -> None:
        harness = TimingHarness(self, available=lambda index: index % 2 == 1)
        harness.push_all()
        harness.finish()

        samples = harness.session.timing_samples
        self.assertEqual(
            [sample.first_pcm_to_device_ms is None for sample in samples],
            [index % 2 == 0 for index in range(1, SENTENCE_COUNT + 1)],
        )
        self.assertEqual(
            [sample.gate_to_device_ms is None for sample in samples],
            [index % 2 == 0 for index in range(1, SENTENCE_COUNT + 1)],
        )
        percentiles = harness.session.timing_percentiles()
        self.assertEqual(percentiles.ready_to_submit.count, 20)
        self.assertEqual(percentiles.first_pcm_to_device.count, 10)
        # available (odd) sentences give device ticks {2,4,1,3,5} twice:
        # sorted [1,1,2,2,3,3,4,4,5,5] -> rank 5 = 3 ticks, rank 10 = 5 ticks
        self.assertEqual(percentiles.first_pcm_to_device.p50, 3 * TICK_MS)
        self.assertEqual(percentiles.first_pcm_to_device.p95, 5 * TICK_MS)
        self.assertEqual(percentiles.gate_to_device.count, 10)
        self.assertEqual(percentiles.gate_to_device.p50, 3 * TICK_MS)
        self.assertEqual(percentiles.gate_to_device.p95, 5 * TICK_MS)
        # Missing worker observations never remove a session-side segment.
        self.assertEqual(
            [sample.submit_to_first_pcm_ms for sample in samples], [1 * TICK_MS] * 20
        )
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 1 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 1 * TICK_MS)

    def test_statistics_stay_readable_after_close(self) -> None:
        harness = TimingHarness(self)
        harness.push_all()
        harness.finish()
        samples = harness.session.timing_samples
        percentiles = harness.session.timing_percentiles()

        harness.session.close()

        self.assertTrue(harness.session.closed)
        self.assertEqual(harness.session.timing_samples, samples)
        self.assertEqual(harness.session.timing_percentiles(), percentiles)

    def test_late_block_after_in_stream_cancel_never_becomes_a_sample(self) -> None:
        provider = CancellingTimedProvider()
        harness = TimingHarness(self, sentence_count=1, provider=provider)
        provider.session = harness.session
        harness.session.push_text_chunk("第一句。")
        harness.verify_gate.set()

        self.assertEqual(harness.session.finish(), ())
        self.assertTrue(harness.session.cancelled)
        self.assertTrue(harness.session.late_events, "late audio stays a diagnostic")
        self.assertEqual(harness.session.timing_samples, ())
        percentiles = harness.session.timing_percentiles()
        for summary in (
            percentiles.ready_to_submit,
            percentiles.first_pcm_to_device,
            percentiles.gate_to_device,
            percentiles.submit_to_first_pcm,
        ):
            self.assertEqual(summary.count, 0)
            self.assertIsNone(summary.p50)
            self.assertIsNone(summary.p95)

    def test_cancel_from_the_test_thread_leaves_no_normal_sample(self) -> None:
        provider = BlockingTimedProvider()
        harness = TimingHarness(self, sentence_count=1, provider=provider)
        harness.session.push_text_chunk("第一句。")
        harness.verify_gate.set()
        self.assertTrue(provider.reached.wait(5.0), "session must reach the provider")

        harness.session.cancel()
        provider.resume.set()

        self.assertEqual(harness.session.finish(), ())
        self.assertTrue(harness.session.cancelled)
        self.assertTrue(harness.session.late_events)
        self.assertEqual(harness.session.timing_samples, ())
        self.assertEqual(harness.session.timing_percentiles().ready_to_submit.count, 0)
        self.assertEqual(harness.session.timing_percentiles().gate_to_device.count, 0)
        self.assertEqual(harness.session.timing_percentiles().submit_to_first_pcm.count, 0)


class StreamingGateTimingTests(unittest.TestCase):
    """Exact spans for the start-buffer gate stamp and its exclusive segment."""

    def test_gate_block_yields_exact_spans_and_third_segment(self) -> None:
        harness = TimingHarness(
            self, gate_script=GATE_SCRIPT, start_buffer_ms=GATE_START_BUFFER_MS
        )
        harness.push_all()
        played = harness.finish()

        self.assertEqual(played, harness.sentences)
        self.assertEqual(len(harness.backend.submitted), GATE_SENTENCE_COUNT)
        # The two scripted blocks of one sentence are coalesced into one batch.
        self.assertTrue(
            all(
                len(block) == len(pcm_bytes(GATE_FIRST_BLOCK_MS + GATE_SECOND_BLOCK_MS))
                for block in harness.backend.submitted
            )
        )

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), GATE_SENTENCE_COUNT)
        for index, sample in enumerate(samples, start=1):
            _ready, pcm_ticks, gate_ticks, device_ticks = GATE_SCRIPT[index - 1]
            self.assertEqual(sample.ready_to_submit_ms, 10 * index * TICK_MS)
            # The synthesis span ends at the first PCM stamp and is unaffected by
            # the start-buffer gate or by the device submission.
            self.assertEqual(sample.submit_to_first_pcm_ms, pcm_ticks * TICK_MS)
            # first PCM -> gate is the start-buffer accumulation itself.
            self.assertEqual(sample.first_pcm_to_gate_ms, gate_ticks * TICK_MS)
            # gate -> device is the exclusive extra cost the 50 ms criterion uses.
            self.assertEqual(sample.gate_to_device_ms, device_ticks * TICK_MS)
            # The raw span is exactly the two parts added together, never less.
            self.assertEqual(
                sample.first_pcm_to_device_ms, (gate_ticks + device_ticks) * TICK_MS
            )
            self.assertGreater(sample.first_pcm_to_device_ms, sample.gate_to_device_ms)

        percentiles = harness.session.timing_percentiles()
        self.assertEqual(percentiles.ready_to_submit.count, 20)
        self.assertEqual(percentiles.ready_to_submit.p50, 100 * TICK_MS)
        self.assertEqual(percentiles.ready_to_submit.p95, 190 * TICK_MS)
        # first PCM -> device sums to [4,6,5,7,4,3,5,7,6,3,5,4,6,8,2,4,6,5,7,4];
        # sorted rank 10 = 5 ticks, rank 19 = 7 ticks.
        self.assertEqual(percentiles.first_pcm_to_device.count, 20)
        self.assertEqual(percentiles.first_pcm_to_device.p50, 5 * TICK_MS)
        self.assertEqual(percentiles.first_pcm_to_device.p95, 7 * TICK_MS)
        # gate -> device is {1,2,3} with counts {6,7,7}: sorted rank 10 = 2 ticks,
        # rank 19 = 3 ticks. It is deliberately a different distribution from the
        # raw segment above, so the two are provably not the same series.
        self.assertEqual(percentiles.gate_to_device.count, 20)
        self.assertEqual(percentiles.gate_to_device.p50, 2 * TICK_MS)
        self.assertEqual(percentiles.gate_to_device.p95, 3 * TICK_MS)
        self.assertNotEqual(
            percentiles.gate_to_device.p50, percentiles.first_pcm_to_device.p50
        )
        # The synthesis segment is constant at 1 tick on the gate timeline too and
        # is provably a different series from both device-side segments.
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 1 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 1 * TICK_MS)
        self.assertNotEqual(
            percentiles.submit_to_first_pcm.p95, percentiles.gate_to_device.p95
        )

    def test_gate_and_submission_on_the_same_instant_report_exact_zero(self) -> None:
        # The only zero-length step: the gate opens and the first submit lands on
        # the same scripted instant, so the honest value is exactly 0.0.
        script = ((10, 1, 4, 0),)
        harness = TimingHarness(
            self, sentence_count=1, gate_script=script, start_buffer_ms=GATE_START_BUFFER_MS
        )
        harness.push_all()
        self.assertEqual(harness.finish(), harness.sentences)

        sample = harness.session.timing_samples[0]
        self.assertEqual(sample.submit_to_first_pcm_ms, 1 * TICK_MS)
        self.assertEqual(sample.first_pcm_to_gate_ms, 4 * TICK_MS)
        self.assertEqual(sample.gate_to_device_ms, 0.0)
        self.assertEqual(sample.first_pcm_to_device_ms, 4 * TICK_MS)

        percentiles = harness.session.timing_percentiles()
        # A measured 0.0 is present data and must be counted, not treated as a
        # missing value.
        self.assertEqual(percentiles.gate_to_device.count, 1)
        self.assertIsNotNone(percentiles.gate_to_device.p50)
        self.assertEqual(percentiles.gate_to_device.p50, 0.0)
        self.assertEqual(percentiles.gate_to_device.p95, 0.0)

    def test_missing_gate_observation_only_removes_the_exclusive_segment(self) -> None:
        harness = TimingHarness(
            self,
            gate_script=GATE_SCRIPT,
            start_buffer_ms=GATE_START_BUFFER_MS,
            available=lambda index: True,
            gate_available=lambda index: False,
        )
        harness.push_all()
        harness.finish()

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), GATE_SENTENCE_COUNT)
        for index, sample in enumerate(samples, start=1):
            _ready, pcm_ticks, gate_ticks, device_ticks = GATE_SCRIPT[index - 1]
            self.assertEqual(sample.submit_to_first_pcm_ms, pcm_ticks * TICK_MS)
            self.assertIsNone(sample.first_pcm_to_gate_ms)
            self.assertIsNone(sample.gate_to_device_ms)
            # The raw device span does not depend on the gate observation.
            self.assertEqual(
                sample.first_pcm_to_device_ms, (gate_ticks + device_ticks) * TICK_MS
            )

        percentiles = harness.session.timing_percentiles()
        self.assertEqual(percentiles.gate_to_device.count, 0)
        self.assertIsNone(percentiles.gate_to_device.p50)
        self.assertIsNone(percentiles.gate_to_device.p95)
        self.assertEqual(percentiles.first_pcm_to_device.count, 20)
        self.assertEqual(percentiles.first_pcm_to_device.p50, 5 * TICK_MS)
        self.assertEqual(percentiles.first_pcm_to_device.p95, 7 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 1 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 1 * TICK_MS)

    def test_missing_submission_keeps_gate_diagnostics_out_of_the_count(self) -> None:
        harness = TimingHarness(
            self,
            gate_script=GATE_SCRIPT,
            start_buffer_ms=GATE_START_BUFFER_MS,
            available=lambda index: index % 2 == 1,
            gate_available=lambda index: True,
        )
        harness.push_all()
        harness.finish()

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), GATE_SENTENCE_COUNT)
        for index, sample in enumerate(samples, start=1):
            _ready, pcm_ticks, gate_ticks, _device = GATE_SCRIPT[index - 1]
            self.assertEqual(sample.submit_to_first_pcm_ms, pcm_ticks * TICK_MS)
            # The gate diagnostic survives on its own; the exclusive span needs
            # both ends and is therefore missing for the even sentences.
            self.assertEqual(sample.first_pcm_to_gate_ms, gate_ticks * TICK_MS)
            if index % 2 == 1:
                self.assertIsNotNone(sample.gate_to_device_ms)
            else:
                self.assertIsNone(sample.gate_to_device_ms)

        percentiles = harness.session.timing_percentiles()
        self.assertEqual(percentiles.first_pcm_to_device.count, 10)
        self.assertEqual(percentiles.gate_to_device.count, 10)
        # Odd sentences carry device ticks [2,1,3,2,1,3,2,1,3,2]:
        # sorted [1,1,1,2,2,2,2,2,3,3] -> rank 5 = 2 ticks, rank 10 = 3 ticks.
        self.assertEqual(percentiles.gate_to_device.p50, 2 * TICK_MS)
        self.assertEqual(percentiles.gate_to_device.p95, 3 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 1 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 1 * TICK_MS)

    def test_gate_observation_is_identity_matched_and_reset_per_sentence(self) -> None:
        harness = TimingHarness(
            self,
            sentence_count=1,
            gate_script=GATE_SCRIPT[:1],
            start_buffer_ms=GATE_START_BUFFER_MS,
        )
        harness.push_all()
        self.assertEqual(harness.finish(), harness.sentences)

        request = harness.provider.calls[-1]
        opened_at = harness.worker.device_gate_open_at(request)
        submitted_at = harness.worker.device_submit_at(request)
        self.assertIsNotNone(opened_at)
        self.assertIsNotNone(submitted_at)
        assert opened_at is not None and submitted_at is not None
        # Same instant basis as the session sample: gate -> device is the exact
        # scripted step, no time may be double counted.
        self.assertEqual(submitted_at - opened_at, GATE_SCRIPT[0][3] * TICK_S)

        other = dataclasses.replace(request, segment_id="segment-99")
        self.assertIsNone(harness.worker.device_gate_open_at(other))
        self.assertIsNone(harness.worker.device_submit_at(other))
        with self.assertRaises(TypeError):
            harness.worker.device_gate_open_at("not-a-request")  # type: ignore[arg-type]

        # ``begin_sentence`` opens a fresh observation window: the previous
        # sentence's gate stamp must no longer be reachable.
        harness.worker.begin_sentence(other)
        self.assertIsNone(harness.worker.device_gate_open_at(request))
        self.assertIsNone(harness.worker.device_submit_at(request))


class StreamingSynthesisLatencyTests(unittest.TestCase):
    """Exact ``tts_submitted_at -> first_pcm_at`` spans and the fourth segment.

    ``submit_to_first_pcm_ms`` is the synthesis/first-audio latency the plan's
    third and fourth criteria (``auto`` vs ``http_wav`` median start latency) are
    read from, so its per-sentence values and its nearest-rank p50/p95 are pinned
    down exactly on a scripted clock whose ``submit -> first PCM`` step varies.
    """

    def test_synthesis_span_and_fourth_segment_are_exact(self) -> None:
        harness = TimingHarness(self, script=SYNTHESIS_SCRIPT)
        harness.push_all()
        played = harness.finish()

        self.assertEqual(played, harness.sentences)
        self.assertEqual(len(harness.backend.submitted), SYNTHESIS_SENTENCE_COUNT)

        samples = harness.session.timing_samples
        self.assertEqual(len(samples), SYNTHESIS_SENTENCE_COUNT)
        self.assertEqual(
            [sample.submit_to_first_pcm_ms for sample in samples],
            [((index % 4) + 1) * TICK_MS for index in range(1, 21)],
        )
        for index, sample in enumerate(samples, start=1):
            ready_ticks, pcm_ticks, device_ticks = SYNTHESIS_SCRIPT[index - 1]
            self.assertEqual(sample.ready_to_submit_ms, ready_ticks * TICK_MS)
            self.assertEqual(sample.submit_to_first_pcm_ms, pcm_ticks * TICK_MS)
            self.assertEqual(sample.first_pcm_to_device_ms, device_ticks * TICK_MS)

        percentiles = harness.session.timing_percentiles()
        # {1,2,3,4} five times each: rank 10 = 2 ticks, rank 19 = 4 ticks.
        self.assertEqual(percentiles.submit_to_first_pcm.count, 20)
        self.assertEqual(percentiles.submit_to_first_pcm.p50, 2 * TICK_MS)
        self.assertEqual(percentiles.submit_to_first_pcm.p95, 4 * TICK_MS)
        # The new segment is provably its own series: on this timeline the device
        # segment's p95 is 3 ticks, which the synthesis segment never reports.
        self.assertEqual(percentiles.first_pcm_to_device.p95, 3 * TICK_MS)
        self.assertNotEqual(
            percentiles.submit_to_first_pcm.p95, percentiles.first_pcm_to_device.p95
        )


class StreamingTimingPrivacyTests(unittest.TestCase):
    def test_samples_and_percentiles_expose_no_text_pcm_or_path(self) -> None:
        harness = TimingHarness(self)
        harness.push_all()
        harness.finish()

        samples = harness.session.timing_samples
        percentiles = harness.session.timing_percentiles()
        rendered = repr(samples) + repr(percentiles)

        self.assertEqual(
            {field.name for field in dataclasses.fields(SentenceTimingSample)},
            {
                "reply_id",
                "generation",
                "segment_index",
                "ready_to_submit_ms",
                "first_pcm_to_device_ms",
                "first_pcm_to_gate_ms",
                "gate_to_device_ms",
                "submit_to_first_pcm_ms",
            },
        )
        self.assertEqual(
            {field.name for field in dataclasses.fields(percentiles)},
            {
                "ready_to_submit",
                "first_pcm_to_device",
                "gate_to_device",
                "submit_to_first_pcm",
            },
        )
        for summary in (
            percentiles.ready_to_submit,
            percentiles.first_pcm_to_device,
            percentiles.gate_to_device,
            percentiles.submit_to_first_pcm,
        ):
            self.assertEqual(
                {field.name for field in dataclasses.fields(summary)}, {"count", "p50", "p95"}
            )

        for sentence in harness.sentences:
            self.assertNotIn(sentence, rendered)
        self.assertNotIn("\\x01", rendered, "no PCM byte may be rendered")
        self.assertNotIn(os.sep, rendered, "no path separator may be rendered")
        self.assertNotIn("/", rendered, "no path or URL separator may be rendered")
        self.assertNotIn(":\\", rendered, "no Windows drive path may be rendered")

        for sample in samples:
            self.assertRegex(sample.reply_id, r"^stream-[0-9a-f]{16}$")
            for field in dataclasses.fields(sample):
                value = getattr(sample, field.name)
                self.assertNotIsInstance(value, bytes)
                if isinstance(value, str):
                    self.assertRegex(value, r"^stream-[0-9a-f]{16}$")


class NearestRankPercentileTests(unittest.TestCase):
    def test_empty_samples_have_no_percentile(self) -> None:
        for percentile in (0.5, 0.95):
            self.assertIsNone(nearest_rank_percentile([], percentile))
        self.assertIsNone(nearest_rank_percentile([], 1.0))

    def test_single_and_all_equal_samples_return_that_value(self) -> None:
        self.assertEqual(nearest_rank_percentile([7.5], 0.5), 7.5)
        self.assertEqual(nearest_rank_percentile([7.5], 0.95), 7.5)
        self.assertEqual(nearest_rank_percentile([4.0, 4.0, 4.0], 0.5), 4.0)
        self.assertEqual(nearest_rank_percentile([4.0, 4.0, 4.0], 0.95), 4.0)

    def test_even_and_odd_counts_take_a_ranked_sample_not_an_average(self) -> None:
        even = [4.0, 1.0, 3.0, 2.0]  # sorted 1,2,3,4
        self.assertEqual(nearest_rank_percentile(even, 0.5), 2.0)   # rank ceil(2.0) = 2
        self.assertEqual(nearest_rank_percentile(even, 0.95), 4.0)  # rank ceil(3.8) = 4
        odd = [30.0, 10.0, 20.0]  # sorted 10,20,30
        self.assertEqual(nearest_rank_percentile(odd, 0.5), 20.0)   # rank ceil(1.5) = 2
        self.assertEqual(nearest_rank_percentile(odd, 0.95), 30.0)  # rank ceil(2.85) = 3

    def test_non_finite_and_boolean_members_are_ignored(self) -> None:
        values = [1.0, float("nan"), 3.0, float("inf"), True]
        self.assertEqual(nearest_rank_percentile(values, 0.95), 3.0)
        self.assertEqual(nearest_rank_percentile(values, 0.5), 1.0)
        self.assertIsNone(nearest_rank_percentile([float("nan")], 0.5))

    def test_percentile_range_is_validated(self) -> None:
        for percentile in (0.0, -0.1, 1.5, float("nan")):
            with self.assertRaises(ValueError):
                nearest_rank_percentile([1.0], percentile)
        with self.assertRaises(TypeError):
            nearest_rank_percentile([1.0], "0.5")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
