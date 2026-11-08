"""R4-B follow-up: the streaming queue is a **waiting** backpressure point.

Real-machine defect (2026-10-03): a long reply spoke only part of its text while
the turn reported "播完" (``played_segments == completed_sentences``,
``error=None``, ``overloaded=False``, no queue residue). The offline root cause in
``app.tts.streaming_session`` was that ``_enqueue()`` only set
``self._overloaded = True`` once ``len(self._pending) > self._max_pending`` while
``_run``'s exit condition *included* ``self._overloaded`` ⇒ accepted-but-unspoken
sentences were dropped silently, and the caller had no fact to see it with.

What this module pins down, fully offline (no socket, no VoiceStudio, no audio
device, no subprocess):

1. **Waiting, not dropping** — a batch that does not fit the pending bound waits
   on the session condition (the lock is released, so the consumer keeps popping)
   and is admitted **in full**; a batch that cannot be admitted within
   ``queue_wait_timeout_s`` is refused **in full** with the explicit
   ``streaming_queue_budget_exceeded`` error, written into ``session.error``.
2. **No tail loss on a long reply** — a 250–350 character reply pushed through
   :class:`SessionStream` over a device that consumes audio at its real duration
   plays **every** character (``played_chars == total_chars``) with no error.
3. **Never "success but less spoken"** — the invariant
   ``error is None and finish_timed_out is False ⇒ played_chars == total_chars``
   is asserted for both the healthy and the queue-overflow scenario.
4. **The PCM budget and the segment length are self-consistent** — the assembled
   budget covers two longest segments and one longest segment's audio really fits
   the queue (the old 8000 ms budget was *shorter than one 10 s segment*).

Playback pacing: the fake device returns each buffer after the buffer's **audio
duration** divided by :data:`AUDIO_TIME_SCALE`, so audio-time accounting is exact
while the wall clock stays test-fast. Phrased honestly: the device consumes
audio ``AUDIO_TIME_SCALE`` times faster than real time; the queued-audio
milliseconds, the segment durations and the resulting truncation facts are the
real ones.
"""
from __future__ import annotations

import getpass
import inspect
import threading
import time
import unittest
from math import ceil
from threading import Event

from app.tts.pcm_playback import PCMPlaybackWorker
from app.tts.playback_worker import StopResult
from app.tts.private_selection import (
    CONFIRMED_PROFILE,
    CONFIRMED_REFERENCE_SHA256,
    VoiceSelection,
)
from app.tts.providers import PCMChunk, VoiceStudioSentenceRequest
from app.tts.streaming_session import (
    DEFAULT_PCM_QUEUE_BUDGET_MS,
    MAX_SEGMENT_CHARACTERS,
    QUEUE_BUDGET_EXCEEDED,
    SPEECH_CHARACTERS_PER_SECOND,
    StreamingVoiceSession,
    StreamingVoiceSessionError,
    build_streaming_session,
    minimum_pcm_queue_budget_ms,
    speech_seconds_for_characters,
)
from app.runtime.voice_entry import SessionStream
from app.tts.transport import VoiceStudioTransportRouter

SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # PCM16 mono: 48 bytes per millisecond
AUDIO_TIME_SCALE = 60.0
"""The fake device consumes audio this many times faster than real time."""

CHUNK_MS = 200


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


def make_selection() -> VoiceSelection:
    return VoiceSelection(
        profile_id=CONFIRMED_PROFILE,
        owner=getpass.getuser(),
        reference_sha256=CONFIRMED_REFERENCE_SHA256,
    )


def make_reply_text() -> str:
    """A 250–350 character reply made of real sentences and no whitespace."""

    sentences = [
        f"这是第{index}句测试文本用来验证长回复不会被截断"
        for index in range(1, 14)
    ]
    text = "".join(sentence + "。" for sentence in sentences)
    assert 250 <= len(text) <= 350, len(text)
    return text


def sentence_duration_ms(text: str) -> int:
    """Audio duration of one sentence at the pinned characters-per-second rate."""

    return max(1, ceil(speech_seconds_for_characters(len(text)) * 1000.0))


class TimedPCMBackend:
    """In-memory device that returns every buffer after its audio duration.

    ``stalled=True`` never returns a buffer, which is how a provably stuck device
    is modelled without any real sleep-based guess about machine speed.
    """

    backend_name = "timed-pcm"
    max_buffer_bytes = CHUNK_MS * BYTES_PER_MS

    def __init__(self, *, scale: float = AUDIO_TIME_SCALE, stalled: bool = False) -> None:
        self.scale = float(scale)
        self.stalled = bool(stalled)
        self.submitted_bytes = 0
        self.buffers = 0
        self.open_calls = 0
        self.close_calls = 0
        self._inflight: list[float] = []
        self._lock = threading.Lock()

    def open(self) -> None:
        self.open_calls += 1

    def _release_expired_locked(self) -> None:
        if self.stalled:
            return
        now = time.monotonic()
        self._inflight = [deadline for deadline in self._inflight if deadline > now]

    def writable(self) -> bool:
        with self._lock:
            self._release_expired_locked()
            return len(self._inflight) < 2

    def submit(self, pcm: bytes) -> None:
        duration_ms = len(pcm) // BYTES_PER_MS
        with self._lock:
            self.submitted_bytes += len(pcm)
            self.buffers += 1
            if self.stalled:
                # Never returned: the device owns the buffer forever, so the worker
                # can neither drain its queue nor complete the sentence.
                self._inflight.append(float("inf"))
            else:
                self._inflight.append(
                    time.monotonic() + (duration_ms / 1000.0) / self.scale
                )

    def poll_drained(self) -> bool:
        with self._lock:
            self._release_expired_locked()
            return not self._inflight

    def submitted_ms(self) -> int:
        with self._lock:
            return self.submitted_bytes // BYTES_PER_MS

    def stop(self, timeout: float) -> StopResult:
        del timeout
        with self._lock:
            self._inflight.clear()
        return StopResult(True, "backend_buffer_returned", "timed_reset")

    def close(self) -> None:
        self.close_calls += 1


class DurationProvider:
    """Synthesises each sentence's **real** audio duration as ordered chunks."""

    def __init__(self) -> None:
        self.calls: list[VoiceStudioSentenceRequest] = []
        self.started = Event()
        self.close_calls = 0
        self._lock = threading.Lock()

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        with self._lock:
            self.calls.append(request)
        self.started.set()
        total_ms = sentence_duration_ms(request.text)
        sequence = 0
        produced = 0
        while produced < total_ms:
            if cancelled.is_set():
                return
            block = min(CHUNK_MS, total_ms - produced)
            produced += block
            sequence += 1
            yield PCMChunk(
                reply_id=request.reply_id,
                segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation,
                sequence=sequence,
                sample_rate=SAMPLE_RATE,
                channels=1,
                sample_width=2,
                data=pcm_bytes(block),
                is_final=produced >= total_ms,
            )

    def close(self) -> None:
        self.close_calls += 1


class QueueBackpressureCase(unittest.TestCase):
    def make_stream(
        self,
        *,
        backend: TimedPCMBackend | None = None,
        max_pending_segments: int = 8,
        queue_wait_timeout_s: float | None = None,
        max_queued_audio_ms: int = DEFAULT_PCM_QUEUE_BUDGET_MS,
    ) -> tuple[SessionStream, TimedPCMBackend, DurationProvider, StreamingVoiceSession]:
        backend = backend or TimedPCMBackend()
        provider = DurationProvider()
        session = build_streaming_session(
            make_selection(),
            mode="ws_pcm",
            ws_provider=provider,
            http_provider=provider,
            backend_factory=lambda: backend,
            start_buffer_ms=1,
            max_queued_audio_ms=max_queued_audio_ms,
            max_pending_segments=max_pending_segments,
            queue_wait_timeout_s=queue_wait_timeout_s,
            verifier=lambda selection: None,
        )
        self.addCleanup(session.close)
        record: dict[str, object] = {
            "route": "ws_pcm",
            "index": 0,
            "text_length": 0,
            "error": None,
        }
        return SessionStream(session, record), backend, provider, session


def assert_never_success_with_less_speech(
    case: unittest.TestCase, record: dict[str, object], total_chars: int
) -> None:
    """The one invariant: success implies every character was spoken."""

    failed = record.get("error") is not None
    truncated = record.get("finish_timed_out") is True
    played = int(record.get("played_chars") or 0)
    if not failed and not truncated:
        case.assertEqual(
            played,
            total_chars,
            "声称播完却少播：record 允许'成功'，但播出字数不等于推送字数",
        )
    else:
        case.assertTrue(
            failed or truncated,
            "失败/截断必须可见（error 或 finish_timed_out 至少一个）",
        )


class LongReplyTests(QueueBackpressureCase):
    def test_long_reply_plays_every_character(self) -> None:
        """250–350 字长回复：`played_chars == total_chars`，没有错误、没有截断。"""

        stream, backend, provider, session = self.make_stream()
        text = make_reply_text()
        total_chars = sum(1 for char in text if not char.isspace())

        stream.push_text_chunk(text)
        played = stream.finish()

        record = stream.record
        self.assertEqual(sum(len(item) for item in played), total_chars)
        self.assertEqual(record["played_chars"], total_chars)
        self.assertEqual(record["pushed_characters"], total_chars)
        self.assertIsNone(record["error"])
        self.assertIs(record["finish_timed_out"], False)
        self.assertIsNone(record["truncation_reason"])
        self.assertIs(record["cancelled"], False)
        self.assertEqual(record["dropped_pushes"], 0)
        # The three independent counters agree, and nothing is left outstanding.
        self.assertEqual(record["accepted_segments"], record["played_segments"])
        self.assertEqual(record["accepted_chars"], record["played_chars"])
        self.assertEqual(record["pending_segments"], 0)
        self.assertEqual(record["pending_sentences_after_finish"], 0)
        self.assertEqual(session.accepted_segments, session.played_count)
        self.assertEqual(session.pending_segments, 0)
        self.assertGreater(session.synthesized_segments, 0)
        self.assertLessEqual(session.synthesized_segments, session.accepted_segments)
        # Every sentence really reached the device with its full audio duration.
        self.assertEqual(provider.calls and len(provider.calls), session.played_count)
        self.assertEqual(backend.submitted_ms(), sum(
            sentence_duration_ms(request.text) for request in provider.calls
        ))
        assert_never_success_with_less_speech(self, record, total_chars)

    def test_tiny_pending_bound_still_plays_every_character(self) -> None:
        """上界只有 2 段时，等待式背压接管：全部播出、一次推送都没被拒、也没有越界。"""

        stream, _backend, _provider, session = self.make_stream(max_pending_segments=2)
        text = make_reply_text()
        total_chars = sum(1 for char in text if not char.isspace())

        stream.push_text_chunk(text)
        played = stream.finish()

        record = stream.record
        self.assertEqual(record["played_chars"], total_chars)
        self.assertEqual(sum(len(item) for item in played), total_chars)
        self.assertIsNone(record["error"])
        self.assertIs(record["finish_timed_out"], False)
        self.assertEqual(record["dropped_pushes"], 0)
        self.assertEqual(record["pending_segments"], 0)
        self.assertEqual(session.accepted_segments, session.played_count)
        # 每片正文只有一句（≤48 字 ⇒ 一段），所以"整批越界"根本不发生：上界是被
        # **等待**尊重的，而不是靠"多接受几段再丢掉"。真正的"等待而非拒绝"由
        # tests/tts/test_streaming_verification_and_overload.py 的 sender 阻塞用例守着。
        self.assertFalse(session.overloaded)
        self.assertEqual(record["overloaded"], False)
        assert_never_success_with_less_speech(self, record, total_chars)


class QueueOverflowTests(QueueBackpressureCase):
    @staticmethod
    def drive_like_chat_loop(stream: SessionStream, text: str) -> Exception | None:
        """Mimic ``chat_loop._speak``: push, and cancel the stream when pushing fails.

        Returns the explicit error, or ``None`` when the reply reached ``finish()``
        successfully.
        """

        try:
            stream.push_text_chunk(text)
        except StreamingVoiceSessionError as exc:
            stream.cancel()
            return exc
        try:
            stream.finish()
        except StreamingVoiceSessionError as exc:
            return exc
        return None

    def test_stalled_device_refuses_the_whole_reply_visibly(self) -> None:
        """设备永不归还缓冲 ⇒ 队列腾不出位置 ⇒ 等待超时、整批拒绝、错误可见。

        关键断言：**不存在**"报成功但少播"的结果——要么全部播出，要么明确失败。
        """

        stalled = TimedPCMBackend(stalled=True)
        stream, _backend, _provider, session = self.make_stream(
            backend=stalled, max_pending_segments=1, queue_wait_timeout_s=0.25
        )
        text = make_reply_text()
        total_chars = sum(1 for char in text if not char.isspace())

        outcome_error = self.drive_like_chat_loop(stream, text)

        record = stream.record
        self.assertIsNotNone(
            outcome_error, "设备卡死时必须在有限时间内以明确错误结束，绝不挂死"
        )
        self.assertEqual(str(outcome_error), QUEUE_BUDGET_EXCEEDED)
        self.assertIs(session.error, outcome_error)
        # 失败在 record 里同样看得见（真实链路 push 抛错 ⇒ `_cancel_stream` ⇒ cancel 记事实）。
        self.assertEqual(record["error"], "StreamingVoiceSessionError")
        self.assertLess(int(record["played_chars"] or 0), total_chars)
        self.assertNotEqual(
            (record["error"], record["finish_timed_out"]),
            (None, False),
            "失败必须可见：error 或 finish_timed_out 至少一个",
        )
        # The refusal is all-or-nothing: no partial batch ever entered the queue
        # without being spoken, and `overloaded` stayed a diagnostic fact.
        self.assertEqual(
            session.accepted_segments,
            session.played_count + session.pending_segments,
        )
        assert_never_success_with_less_speech(self, record, total_chars)

    def test_overflow_never_reports_success_with_a_short_reply(self) -> None:
        """越界场景的最小复现：越界批次要么全播，要么整批失败——判据是真空的。"""

        stalled = TimedPCMBackend(stalled=True)
        stream, _backend, _provider, session = self.make_stream(
            backend=stalled, max_pending_segments=1, queue_wait_timeout_s=0.2
        )
        text = "第一句测试文本。" * 6  # 8 字 x 6 = 48 字，一片一句
        total_chars = len(text)

        self.drive_like_chat_loop(stream, text)

        record = stream.record
        played = int(record["played_chars"] or 0)
        if record["error"] is None and record["finish_timed_out"] is False:
            self.assertEqual(played, total_chars)
        else:
            self.assertIsNotNone(record["error"])
        self.assertNotEqual(
            (record["error"] is None, record["finish_timed_out"], played),
            (True, False, 0),
            "不许出现'无错误、未截断、却一句没播'",
        )
        assert_never_success_with_less_speech(self, record, total_chars)
        self.assertGreaterEqual(session.accepted_segments, session.played_count)


class PcmQueueBudgetTests(QueueBackpressureCase):
    def test_default_budget_covers_two_longest_segments(self) -> None:
        longest_seconds = speech_seconds_for_characters(MAX_SEGMENT_CHARACTERS)
        self.assertEqual(MAX_SEGMENT_CHARACTERS, 48)
        self.assertEqual(SPEECH_CHARACTERS_PER_SECOND, 4.8)
        self.assertAlmostEqual(longest_seconds, 10.0, places=6)
        self.assertGreaterEqual(
            DEFAULT_PCM_QUEUE_BUDGET_MS / 1000.0,
            2.0 * longest_seconds,
            "PCM 队列预算秒数必须 ≥ 2 x 最长句段秒数",
        )
        self.assertEqual(DEFAULT_PCM_QUEUE_BUDGET_MS, minimum_pcm_queue_budget_ms())

    def test_assembly_point_uses_the_derived_budget(self) -> None:
        parameter = inspect.signature(build_streaming_session).parameters[
            "max_queued_audio_ms"
        ]
        self.assertEqual(parameter.default, DEFAULT_PCM_QUEUE_BUDGET_MS)
        self.assertGreater(parameter.default, 8000, "旧预算 8000 ms 必须被抬高")

    def test_one_longest_segment_really_fits_the_queue(self) -> None:
        """一段最长句段的音频必须装得进队列（旧 8000 ms 预算会直接顶格）。"""

        stream, _backend, _provider, session = self.make_stream()
        del stream
        worker = session._worker  # noqa: SLF001 - 装配点的真实产物
        request = VoiceStudioSentenceRequest(
            reply_id=session.reply_id,
            segment_id="segment-budget-1",
            provider_request_id="budget-1",
            provider_generation=1,
            text="预" * MAX_SEGMENT_CHARACTERS,
            profile_id=make_selection().profile_id,
            language="Chinese",
            speed=1.0,
        )
        ticket = worker.begin_sentence(request)
        longest_ms = sentence_duration_ms("预" * MAX_SEGMENT_CHARACTERS)
        self.assertGreater(longest_ms, 8000, "最长句段必须长于旧预算，否则本用例无效")
        accepted = worker.push_chunk(
            PCMChunk(
                reply_id=request.reply_id,
                segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation,
                sequence=1,
                sample_rate=SAMPLE_RATE,
                channels=1,
                sample_width=2,
                data=pcm_bytes(longest_ms),
                is_final=True,
            )
        )
        self.assertTrue(accepted, "最长句段的音频必须被队列接受（不得抛 PCMPlaybackBackpressure）")
        worker.wait(ticket, 10.0)


class SessionContractTests(QueueBackpressureCase):
    def _session(self, **kwargs: object) -> StreamingVoiceSession:
        stream, _backend, _provider, session = self.make_stream(**kwargs)  # type: ignore[arg-type]
        del stream
        return session

    def _direct_session(
        self,
        *,
        segment_timeout_s: float = 30.0,
        queue_wait_timeout_s: float | None = None,
    ) -> StreamingVoiceSession:
        """Direct construction: ``build_streaming_session`` does not expose the join bound."""

        backend = TimedPCMBackend()
        worker = PCMPlaybackWorker(lambda: backend, start_buffer_ms=1, stop_timeout=0.5)
        self.addCleanup(worker.close)
        provider = DurationProvider()
        router = VoiceStudioTransportRouter(
            mode="ws_pcm", ws_provider=provider, http_provider=provider
        )
        self.addCleanup(router.close)
        session = StreamingVoiceSession(
            make_selection(),
            transport_router=router,
            worker=worker,
            verifier=lambda selection: None,
            segment_timeout_s=segment_timeout_s,
            queue_wait_timeout_s=queue_wait_timeout_s,
        )
        self.addCleanup(session.close)
        return session

    def test_queue_wait_timeout_defaults_to_the_segment_timeout(self) -> None:
        session = self._direct_session(segment_timeout_s=2.5)
        self.assertEqual(session.queue_wait_timeout_s, 2.5)

    def test_explicit_queue_wait_timeout_wins(self) -> None:
        session = self._direct_session(segment_timeout_s=2.5, queue_wait_timeout_s=0.125)
        self.assertEqual(session.queue_wait_timeout_s, 0.125)

    def test_bad_queue_wait_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._session(queue_wait_timeout_s=0)

    def test_counters_start_at_zero_and_pending_is_derived(self) -> None:
        session = self._session()
        self.assertEqual(session.accepted_segments, 0)
        self.assertEqual(session.accepted_characters, 0)
        self.assertEqual(session.synthesized_segments, 0)
        self.assertEqual(session.played_count, 0)
        self.assertEqual(session.played_characters, 0)
        self.assertEqual(session.pending_segments, 0)
        self.assertFalse(session.overloaded)

    def test_a_batch_larger_than_the_bound_is_accepted_whole_and_only_recorded(self) -> None:
        session = self._session(max_pending_segments=1)
        session.push_text_chunk("第一句。第二句。第三句。")
        self.assertEqual(session.accepted_segments, 3)
        self.assertEqual(session.accepted_characters, 12)
        self.assertEqual(session.dropped_pushes, 0)
        self.assertTrue(session.overloaded)
        self.assertEqual(session.pending_sentences + session.played_count, 3)
        self.assertEqual(session.finish(), ("第一句。", "第二句。", "第三句。"))
        self.assertEqual(session.played_count, 3)
        self.assertEqual(session.pending_segments, 0)
        self.assertIsNone(session.error)


class SegmentFlowEvidenceTests(QueueBackpressureCase):
    """R4-B 追加取证（只读）：段在 "segmenter → pending 队列" 之间的每一步都要可见。

    真机自相矛盾的一轮：`text=291 / pushed_chars=282 / accepted_segments=1`，而离线复现证明
    282 字只要真到过 :class:`ReplySegmenter` 就必然切出 ≥5 段
    （段上限 48 字）；`truncation_reason=played_chars_below_pushed` 又要求
    `accepted_chars == played_chars`。三者不可能同时为真 ⇒ 必须有字段回答"段在哪一步消失"。

    本类只用**新增的只读计数**回答它，且断言里不含任何新等待、新超时或新判定：

    1. 正常长回复里 `dequeued_* == enqueued_*`（与 `accepted_*` 同值）且丢弃为 0；
    2. 已取消的会话再推一批 ⇒ 该批**已出段却未入队**，`enqueue_dropped_* > 0`（无痕丢弃点可见）；
    3. 等待式背压期间取消 ⇒ 同一丢弃点同样计数，且 `dequeued = enqueued + dropped` 恒等。
    """

    def _await_pending(self, session: StreamingVoiceSession, expected: int) -> None:
        deadline = time.monotonic() + 5.0
        while session.pending_sentences != expected and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(session.pending_sentences, expected)

    def test_long_reply_dequeued_equals_enqueued_and_nothing_is_dropped(self) -> None:
        stream, _backend, _provider, session = self.make_stream()
        text = make_reply_text()

        stream.push_text_chunk(text)
        stream.finish()

        record = stream.record
        self.assertGreater(session.dequeued_segments_total, 1)
        self.assertEqual(session.dequeued_segments_total, session.enqueued_segments_total)
        self.assertEqual(
            session.dequeued_characters_total, session.enqueued_characters_total
        )
        # 入队计数与既有 `accepted_*` 完全同值：新计数只是把同一事实拆开看，不改语义。
        self.assertEqual(session.enqueued_segments_total, session.accepted_segments)
        self.assertEqual(session.enqueued_characters_total, session.accepted_characters)
        self.assertEqual(session.enqueue_dropped_segments_total, 0)
        self.assertEqual(session.enqueue_dropped_characters_total, 0)
        self.assertEqual(session.segmenter_buffered_characters, 0)
        # record 里同样可见（只读事实，非判定依据）。
        self.assertEqual(record["dequeued_segments"], session.dequeued_segments_total)
        self.assertEqual(record["dequeued_chars"], session.dequeued_characters_total)
        self.assertEqual(record["enqueued_segments"], session.enqueued_segments_total)
        self.assertEqual(record["enqueued_chars"], session.enqueued_characters_total)
        self.assertEqual(record["enqueue_dropped_segments"], 0)
        self.assertEqual(record["enqueue_dropped_chars"], 0)
        self.assertEqual(record["segmenter_buffered_chars"], 0)

    def test_cancelled_session_drops_a_pushed_batch_visibly(self) -> None:
        """已取消的会话再推一批：段已出 segmenter、却没进 `_pending` ⇒ 丢弃计数必须 >0。

        `cancel()` 只清 `_pending` 并置终态，**不动** segmenter；因此这一批真的走完了
        `submit → dequeue → _enqueue`，最后停在 `_enqueue` 的终态早退分支上——正是旧代码里
        不留任何痕迹的地方。这里不改该分支行为（不抛错、不写 late event），只把丢弃记下来。
        """

        stream, _backend, _provider, session = self.make_stream()
        session.cancel()
        session._submit("第一句。第二句。", is_final=False)  # noqa: SLF001 - 复现"终态后再推一批"
        stream.finish()  # 事实照旧落进 record（finish 在终态下只置 finished + join）

        record = stream.record
        self.assertEqual(session.dequeued_segments_total, 2)
        self.assertEqual(session.dequeued_characters_total, 8)
        self.assertEqual(session.enqueued_segments_total, 0)
        self.assertEqual(session.enqueue_dropped_segments_total, 2)
        self.assertEqual(session.enqueue_dropped_characters_total, 8)
        self.assertEqual(record["dequeued_segments"], 2)
        self.assertEqual(record["dequeued_chars"], 8)
        self.assertEqual(record["enqueued_segments"], 0)
        self.assertEqual(record["enqueued_chars"], 0)
        self.assertEqual(record["enqueue_dropped_segments"], 2)
        self.assertEqual(record["enqueue_dropped_chars"], 8)
        # 丢弃是**已出段未入队**的差额：恒等式必须成立，否则仍有看不见的消失点。
        self.assertEqual(
            session.dequeued_segments_total,
            session.enqueued_segments_total + session.enqueue_dropped_segments_total,
        )
        self.assertEqual(
            session.dequeued_characters_total,
            session.enqueued_characters_total + session.enqueue_dropped_characters_total,
        )

    def test_cancel_while_waiting_for_room_counts_the_dropped_batch(self) -> None:
        """等待式背压期间取消：走 `_enqueue` 的**第二个**早退分支，同样必须被计数。

        设备卡死 ⇒ 消费者永远占着第一句 ⇒ 第二句留在 `_pending`（`max_pending_segments=1`）
        ⇒ 第三句进入 `_enqueue` 的等待；此时取消，等待被唤醒后原样返回（不抛错），
        这一批"已出段未入队"必须在 `enqueue_dropped_*` 里看得见。
        """

        stalled = TimedPCMBackend(stalled=True)
        stream, _backend, _provider, session = self.make_stream(
            backend=stalled, max_pending_segments=1, queue_wait_timeout_s=5.0
        )
        del stream
        session.push_text_chunk("第一句。第二句。")
        self._await_pending(session, 1)
        errors: list[BaseException] = []

        def sender() -> None:
            try:
                session.push_text_chunk("第三句。")
            except BaseException as exc:  # noqa: BLE001 - 取消绝不改变推送的返回契约
                errors.append(exc)

        thread = threading.Thread(target=sender, name="segment-flow-sender", daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while session.dequeued_segments_total < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        # `dequeued` 在 `_enqueue` **之前**累加 ⇒ 读到 3 就证明该批已出 segmenter、
        # 必然落在 `_enqueue` 的第一个或第二个早退分支上。
        self.assertEqual(session.dequeued_segments_total, 3)
        session.cancel()
        thread.join(timeout=5.0)

        self.assertFalse(thread.is_alive(), "取消后推送必须立即返回，绝不挂死")
        self.assertEqual(errors, [])  # 早退分支照旧不抛错
        self.assertEqual(session.enqueued_segments_total, 2)
        self.assertEqual(session.enqueue_dropped_segments_total, 1)
        self.assertEqual(session.enqueue_dropped_characters_total, 4)
        self.assertEqual(
            session.dequeued_segments_total,
            session.enqueued_segments_total + session.enqueue_dropped_segments_total,
        )
        self.assertEqual(
            session.dequeued_characters_total,
            session.enqueued_characters_total + session.enqueue_dropped_characters_total,
        )

    def test_counters_start_at_zero_and_buffered_characters_is_readable(self) -> None:
        session = self._session_holder()
        self.assertEqual(session.dequeued_segments_total, 0)
        self.assertEqual(session.dequeued_characters_total, 0)
        self.assertEqual(session.enqueued_segments_total, 0)
        self.assertEqual(session.enqueued_characters_total, 0)
        self.assertEqual(session.enqueue_dropped_segments_total, 0)
        self.assertEqual(session.enqueue_dropped_characters_total, 0)
        self.assertEqual(session.segmenter_buffered_characters, 0)

    def test_buffered_characters_is_none_without_the_segmenter_api(self) -> None:
        """segmenter 没有 `buffered_text` 时如实记 `None`，绝不抛错。"""

        session = self._session_holder()
        session._segmenter = object()  # noqa: SLF001 - 故意去掉只读接口
        self.assertIsNone(session.segmenter_buffered_characters)

    def _session_holder(self) -> StreamingVoiceSession:
        stream, _backend, _provider, session = self.make_stream()
        del stream
        return session


if __name__ == "__main__":
    unittest.main()
