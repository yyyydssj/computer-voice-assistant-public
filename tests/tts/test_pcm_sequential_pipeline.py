"""Offline tests for the bounded-prefetch sequential speech channel.

Everything here runs against a fake transport router (a plain per-sentence PCM
producer), a fake playback worker that consumes audio in real time without any
device, and a fake synchronous transport router stand-in. No network, no device,
no real VoiceStudio session is touched, and `app.main` is never imported.

The two properties the channel exists for are pinned by name:

* seamlessness: :meth:`TestPrefetch.*test_next_sentence_is_ready_before_playback_ends`
  and :meth:`...test_synthesis_starts_next_sentence_before_playback_ends`
  (synthesis of sentence *i+1* starts, and finishes, while sentence *i* is still
  being played — the "synthesize one, speak it, synthesize the next" structure
  would make both assertions impossible);
* "never a silent tail": :meth:`TestFailures.test_synthesis_failure_raises_and_short_play_is_visible`,
  :meth:`TestFailures.test_playback_never_completes_times_out_within_bound` and
  :meth:`TestFailures.test_empty_pipeline_cannot_report_success`, plus the
  `sentences_played == sentences_synthesized == sentences_total` invariant
  asserted in every such test.
"""
from __future__ import annotations

import threading
import time
import unittest
from threading import Event
from typing import Iterator

from app.tts.pcm_playback import PCMPlaybackError
from app.tts.playback_worker import PlaybackState, PlaybackTicket
from app.tts.providers.contracts import (
    PCMChunk,
    VoiceStudioContractError,
    VoiceStudioSentenceRequest,
)
from app.tts.sequential_session import (
    MAX_SENTENCE_CHARACTERS,
    SEQUENTIAL_ERROR_CODES,
    SequentialVoiceSession,
    SequentialVoiceSessionError,
    split_sentences,
)
from app.tts.transport import VoiceStudioTransportRouter

SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # PCM16 mono: 48 bytes per millisecond
MS_PER_CHUNK = 50


class CodedProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


LONG_REPLY = (
    "第一句话说的是今天的天气还不错，适合出门走走，顺便把上周借的书还掉。"
    "第二句话稍微长一些，讲的是昨天下午在图书馆里读到的一本关于分布式系统的书，"
    "书里提到一个很有意思的观点：延迟和吞吐量往往不能同时最优，必须根据场景取舍，"
    "作者还举了一个跨机房复制的例子来说明这一点，读起来相当清楚。"
    "第三句话很短：谢谢。"
    "第四句话用来凑够长度，因为验收要求原文在二百五十到三百五十个字之间，"
    "所以这里再多写一些内容，但依然保证句末标点齐全，切句结果不会出现半句，"
    "也不会出现任何字符被增加或者删除的情况，这一点由 join 相等断言来保证。"
    "最后一句收尾。"
)


def pcm_chunk(
    *,
    reply_id: str,
    segment_id: str,
    provider_request_id: str,
    sequence: int,
    milliseconds: int = MS_PER_CHUNK,
    is_final: bool = False,
) -> PCMChunk:
    return PCMChunk(
        reply_id=reply_id,
        segment_id=segment_id,
        provider_request_id=provider_request_id,
        provider_generation=1,
        sequence=sequence,
        sample_rate=SAMPLE_RATE,
        channels=1,
        sample_width=2,
        data=b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000),
        is_final=is_final,
    )


def chunks_for(
    request: VoiceStudioSentenceRequest,
    *,
    count: int = 2,
    ms_each: int = MS_PER_CHUNK,
) -> tuple[PCMChunk, ...]:
    return tuple(
        pcm_chunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            sequence=index + 1,
            milliseconds=ms_each,
            is_final=index + 1 == count,
        )
        for index in range(count)
    )


class FakeRouter(VoiceStudioTransportRouter):
    """Per-sentence PCM producer standing in for the WS/HTTP transport router.

    It subclasses :class:`VoiceStudioTransportRouter` so the channel's type check
    is satisfied without weakening it, and never calls the real ``__init__``: there
    is no socket, no provider and no HTTP request anywhere in these tests.
    """

    def __init__(
        self,
        *,
        synth_seconds: float = 0.0,
        chunk_count: int = 2,
        ms_each: int = MS_PER_CHUNK,
        fail_on_index: int | None = None,
        failure_code: str | None = None,
    ) -> None:
        self.synth_seconds = synth_seconds
        self.chunk_count = chunk_count
        self.ms_each = ms_each
        self.fail_on_index = fail_on_index
        self.failure_code = failure_code
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.finished: list[tuple[str, float]] = []
        self.cancelled_streams: list[str] = []
        self.closed = False
        self.gates: dict[int, Event] = {}

    def stream_sentence(
        self, request: VoiceStudioSentenceRequest, *, cancelled: Event
    ) -> Iterator[PCMChunk]:
        text = request.text
        with self.lock:
            self.started.append(text)
            index = len(self.started)
        if self.synth_seconds:
            # The sleep is cancellable only in the sense that the transport
            # providers are: the channel never interrupts a provider mid-render.
            time.sleep(self.synth_seconds)
        if cancelled.is_set():
            return
        if index in self.gates:
            # Deterministic sequencing hook: this sentence is not rendered until
            # the test says so, which is how "the previous sentence really played"
            # is asserted without relying on wall-clock races.
            self.gates[index].wait(5.0)
            if cancelled.is_set():
                return
        if self.fail_on_index == index:
            if self.failure_code is not None:
                raise CodedProviderError(self.failure_code)
            raise RuntimeError("synthetic_provider_failure")
        for chunk in chunks_for(request, count=self.chunk_count, ms_each=self.ms_each):
            if cancelled.is_set():
                with self.lock:
                    self.cancelled_streams.append(text)
                return
            yield chunk
        with self.lock:
            self.finished.append((text, time.monotonic()))

    def texts(self) -> list[str]:
        with self.lock:
            return list(self.started)

    def close(self) -> None:
        self.closed = True


class FakePlaybackWorker:
    """Offline stand-in for :class:`PCMPlaybackWorker` driven by real elapsed time.

    The interface is exactly the one the channel is allowed to use
    (``begin_sentence``/``push_chunk``/``wait``/``stop``/``close``) plus the two
    read-only observations the deployed worker already exposes to the session
    (``device_submit_at``/``device_gate_open_at``). Playback is simulated by
    sleeping for the audio's real duration before the ticket becomes COMPLETED, so
    a test can assert that the playback thread really waited instead of spinning.
    """

    def __init__(
        self,
        *,
        never_complete: bool = False,
        block_full_timeout: bool = False,
        block_until_stopped: bool = False,
        keep_pushing_after_terminal: bool = False,
    ) -> None:
        self.never_complete = never_complete
        self.block_full_timeout = block_full_timeout
        self.block_until_stopped = block_until_stopped
        self.keep_pushing_after_terminal = keep_pushing_after_terminal
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.active: PlaybackTicket | None = None
        self.last_request: VoiceStudioSentenceRequest | None = None
        self.wait_states: list[PlaybackState] = []
        self.begin_count = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.push_after_terminal_accepted = 0
        self.played_audio_ms = 0
        self.submitted: list[tuple[str, int]] = []
        self._gates: dict[int, float] = {}
        self._submits: dict[int, float] = {}
        self.begin_at: list[float] = []
        self.completed_at: list[float] = []
        self._closed = False

    # ------------------------------------------------------------------ worker
    def begin_sentence(self, request: VoiceStudioSentenceRequest) -> PlaybackTicket:
        with self.condition:
            if self.active is not None and not self.active.terminal:
                raise PCMPlaybackError("sentence_already_active")
            ticket = PlaybackTicket(
                ticket_id="ticket-%d" % (self.begin_count + 1), backend="fake-pcm"
            )
            self.active = ticket
            self.last_request = request
            self.begin_count += 1
            self.begin_at.append(time.monotonic())
            return ticket

    def push_chunk(self, chunk: PCMChunk) -> bool:
        with self.condition:
            ticket = self.active
            if ticket is None:
                raise PCMPlaybackError("no_active_sentence")
            if ticket.terminal:
                if self.keep_pushing_after_terminal:
                    self.push_after_terminal_accepted += 1
                    return False
                raise PCMPlaybackError("no_active_sentence")
            duration_ms = len(chunk.data) // BYTES_PER_MS
            self.played_audio_ms += duration_ms
            self.submitted.append((chunk.segment_id, duration_ms))
            identity = id(ticket)
            self._gates.setdefault(identity, time.monotonic())
            self._submits.setdefault(identity, time.monotonic())
            if chunk.is_final:
                ticket.state = PlaybackState.PLAYING
                with ticket._condition:  # noqa: SLF001 - mirrors the real worker
                    ticket._condition.notify_all()  # noqa: SLF001
            return True

    def wait(self, ticket: PlaybackTicket, timeout: float | None = None) -> PlaybackState:
        if self.never_complete:
            self.wait_states.append(PlaybackState.QUEUED)
            if self.block_full_timeout:
                # Block for the whole bound the caller offered and report a state
                # that is not COMPLETED: the channel must time out by itself.
                if timeout:
                    time.sleep(timeout)
            elif self.block_until_stopped:
                # Stay inside the call until the caller stops the device, which is
                # what a real sentence that never drains looks like from here.
                self.stopped.wait(timeout if timeout else 30.0)
            return PlaybackState.QUEUED
        with self.condition:
            state = self.active.state if self.active is not None else PlaybackState.QUEUED
        # Simulated device playback: real elapsed time for this sentence's audio.
        duration_s = self.played_audio_ms / 1000.0
        if duration_s:
            time.sleep(duration_s)
        with self.condition:
            if not ticket.terminal:
                ticket.state = PlaybackState.COMPLETED
                with ticket._condition:  # noqa: SLF001 - mirrors the real worker
                    ticket._condition.notify_all()  # noqa: SLF001
            state = ticket.state
            self.active = None
            self.completed_at.append(time.monotonic())
        self.wait_states.append(state)
        return state

    def stop(self, ticket: PlaybackTicket | None = None, *, timeout: float | None = None) -> PlaybackState:
        del timeout
        with self.condition:
            self.stop_calls += 1
            target = ticket or self.active
            self.stopped.set()
            if target is None:
                return PlaybackState.STOP_ACKED
            if not target.terminal:
                target.state = PlaybackState.STOP_ACKED
                with target._condition:  # noqa: SLF001 - mirrors the real worker
                    target._condition.notify_all()  # noqa: SLF001
            self.active = None
            return PlaybackState.STOP_ACKED

    def close(self) -> None:
        with self.condition:
            if self._closed:
                return
            self._closed = True
            self.close_calls += 1
            self.stopped.set()

    # ---------------------------------------------------------- observations
    def device_submit_at(self, request: VoiceStudioSentenceRequest) -> float | None:
        del request
        return None

    def device_gate_open_at(self, request: VoiceStudioSentenceRequest) -> float | None:
        del request
        return None


class _Selection:
    """Minimal stand-in for ``VoiceSelection`` (pid/uid/speed only)."""

    profile_id = "confirmed-default"
    user_id = "tester"
    speed = 1.0


def build_session(
    router: FakeRouter,
    worker: FakePlaybackWorker,
    **kwargs: object,
) -> SequentialVoiceSession:
    defaults: dict[str, object] = {
        "synthesis_wait_s": 10.0,
        "playback_timeout_s": 10.0,
        "join_timeout_s": 20.0,
        "stop_timeout_s": 0.2,
        "wake_interval_s": 0.005,
    }
    defaults.update(kwargs)
    return SequentialVoiceSession(
        _Selection(),
        transport_router=router,  # type: ignore[arg-type]
        worker=worker,  # type: ignore[arg-type]
        **defaults,  # type: ignore[arg-type]
    )


class TestSplitSentences(unittest.TestCase):
    """Edge cases of the character-exact segmenter."""

    def test_empty_text_yields_nothing(self) -> None:
        self.assertEqual(split_sentences(""), ())
        self.assertEqual(split_sentences("", ), ())

    def test_single_sentence_without_ender_is_one_piece(self) -> None:
        self.assertEqual(split_sentences("只有一句话"), ("只有一句话",))

    def test_real_sentence_ends_close_each_piece(self) -> None:
        pieces = split_sentences("你好。世界！好；行\n结束…")
        self.assertEqual(pieces, ("你好。", "世界！", "好；", "行\n", "结束…"))

    def test_unpunctuated_overlong_text_is_cut_and_joins_back(self) -> None:
        text = "长" * 300
        pieces = split_sentences(text, max_chars=120)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(len(piece) <= 120 for piece in pieces))
        self.assertEqual([len(piece) for piece in pieces], [120, 120, 60])

    def test_secondary_break_is_preferred_at_the_ceiling(self) -> None:
        # 130 characters, a comma at index 100: the cut must land right after the
        # comma, so the first piece is 101 characters instead of being cut at the
        # hard 120-character ceiling and leaving "，" at the head of the next piece.
        text = "甲" * 100 + "，" + "乙" * 29
        pieces = split_sentences(text, max_chars=120)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(pieces[0], "甲" * 100 + "，")
        self.assertEqual(len(pieces[-1]), 29)
        self.assertTrue(all(len(piece) <= 120 for piece in pieces))

    def test_hard_ceiling_is_used_when_no_secondary_break_exists(self) -> None:
        text = "长" * 250
        pieces = split_sentences(text, max_chars=120)
        self.assertEqual("".join(pieces), text)
        self.assertEqual([len(piece) for piece in pieces], [120, 120, 10])

    def test_join_equality_holds_for_mixed_texts(self) -> None:
        for text in (
            "你好。",
            "一句话没有标点",
            "，、,：:）) " * 20,
            "长" * 300,
            LONG_REPLY,
            "换行\n也要切\n",
        ):
            with self.subTest(text=text[:12]):
                self.assertEqual("".join(split_sentences(text)), text)

    def test_characters_are_never_added_or_removed_for_a_long_reply(self) -> None:
        pieces = split_sentences(LONG_REPLY)
        self.assertEqual("".join(pieces), LONG_REPLY)
        self.assertEqual(sum(len(piece) for piece in pieces), len(LONG_REPLY))


class TestPrefetch(unittest.TestCase):
    """The channel speaks the whole reply, in order, without waiting on playback."""

    def test_long_reply_counts_are_exact_and_lookahead_is_bounded(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = build_session(router, worker)
        self.addCleanup(session.close)

        session.push_text_chunk(LONG_REPLY)
        played = session.finish()

        expected = split_sentences(LONG_REPLY)
        self.assertGreaterEqual(len(LONG_REPLY), 250)
        self.assertLessEqual(len(LONG_REPLY), 350)
        self.assertGreater(len(expected), 1)
        self.assertEqual(session.sentences_total, len(expected))
        self.assertEqual(session.sentences_synthesized, len(expected))
        self.assertEqual(session.sentences_played, len(expected))
        self.assertEqual(played, expected)
        self.assertEqual("".join(played), LONG_REPLY)
        self.assertEqual(session.text_length, len(LONG_REPLY))
        self.assertEqual(session.played_chars, len(LONG_REPLY))
        self.assertEqual(sum(len(piece) for piece in played), len(LONG_REPLY))
        self.assertEqual(session.played_characters, session.played_chars)
        self.assertLessEqual(session.lookahead_peak, session.READY_QUEUE_CAPACITY)
        self.assertEqual(session.lookahead_peak, 2)
        self.assertEqual(session.pending_segments, 0)
        self.assertIsNone(session.error)
        self.assertFalse(session.cancelled)
        # The channel is wired straight through to the injected router/worker.
        self.assertIsInstance(session.sentences_played, int)
        self.assertEqual(router.texts(), list(expected))
        self.assertEqual(worker.begin_count, len(expected))
        self.assertEqual(worker.push_after_terminal_accepted, 0)
        self.assertEqual(worker.stop_calls, 0)

    def test_multiple_pushes_are_concatenated_in_order(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = build_session(router, worker)
        self.addCleanup(session.close)

        session.push_text_chunk("第一句。")
        session.push_text_chunk("第二句")
        session.push_text_chunk("还在继续。")
        played = session.finish()

        self.assertEqual("".join(played), "第一句。第二句还在继续。")
        self.assertEqual(session.sentences_total, session.sentences_synthesized)
        self.assertEqual(session.sentences_played, session.sentences_total)
        self.assertEqual(session.text_length, len("第一句。第二句还在继续。"))

    def test_finish_flushes_an_unterminated_tail(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = build_session(router, worker)
        self.addCleanup(session.close)

        session.push_text_chunk("没有句末标点")
        # Nothing can be synthesized yet: the only sentence is still incomplete.
        self.assertEqual(session.sentences_total, 0)
        played = session.finish()
        self.assertEqual(played, ("没有句末标点",))
        self.assertEqual(session.sentences_total, 1)
        self.assertEqual(session.sentences_synthesized, 1)
        self.assertEqual(session.sentences_played, 1)

    def test_empty_text_finishes_without_speaking_and_without_failing(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = build_session(router, worker)
        self.addCleanup(session.close)

        session.push_text_chunk("")
        played = session.finish()
        self.assertEqual(played, ())
        self.assertEqual(session.sentences_total, 0)
        self.assertEqual(session.sentences_synthesized, 0)
        self.assertEqual(session.sentences_played, 0)
        self.assertEqual(session.text_length, 0)
        self.assertIsNone(session.error)

    def test_next_sentence_is_ready_before_playback_ends(self) -> None:
        """Synthesis of *i+1* completes while sentence *i* is still playing."""

        router = FakeRouter(synth_seconds=0.02)
        worker = FakePlaybackWorker()
        session = build_session(router, worker, playback_timeout_s=5.0)
        self.addCleanup(session.close)

        session.push_text_chunk("第一句。第二句。第三句。")
        session.finish()

        self.assertEqual(len(worker.completed_at), 3)
        first_finished, second_finished, third_finished = (item[1] for item in router.finished)
        first_played, second_played, _ = worker.completed_at
        # Prefetch: the second sentence was already synthesized (and waiting) when
        # the first sentence's playback ended; a "synthesize, then speak, then
        # synthesize" structure would put the second synthesis after first_played.
        self.assertLess(second_finished, first_played)
        self.assertLess(third_finished, second_played)
        self.assertLessEqual(session.lookahead_peak, session.READY_QUEUE_CAPACITY)
        # The wait for the device is really measured: three sentences of 100 ms
        # simulated playback each must add up in ``play_wait_ms``.
        self.assertGreaterEqual(session.play_wait_ms, 150)

    def test_synthesis_starts_next_sentence_before_playback_ends(self) -> None:
        """The synthesis thread never waits for the previous sentence to finish."""

        router = FakeRouter(synth_seconds=0.05)
        worker = FakePlaybackWorker()
        session = build_session(router, worker, playback_timeout_s=5.0)
        self.addCleanup(session.close)

        session.push_text_chunk("甲句。乙句。丙句。")
        session.finish()

        # Every sentence really was rendered, in order, through the injected router.
        self.assertEqual(router.texts(), ["甲句。", "乙句。", "丙句。"])
        self.assertEqual(len(worker.completed_at), 3)
        self.assertEqual(worker.begin_count, 3)
        self.assertEqual([item[0] for item in worker.submitted if True][:0], [])
        self.assertEqual(worker.submitted[0][0], "segment-1")
        # Every sentence was fed to the worker exactly once (no sentence was skipped
        # or replayed), and the played order matches the synthesis order.
        segments = [segment_id for segment_id, _ in worker.submitted]
        self.assertEqual(segments.count("segment-1"), 2)
        self.assertEqual(segments.count("segment-2"), 2)
        self.assertEqual(segments.count("segment-3"), 2)
        self.assertEqual(session.played_segments, ("甲句。", "乙句。", "丙句。"))

    def test_synthesis_waits_instead_of_dropping_when_the_queue_is_full(self) -> None:
        """A full published queue delays synthesis; it never loses a sentence."""

        router = FakeRouter(chunk_count=1)
        worker = FakePlaybackWorker()
        session = build_session(router, worker, playback_timeout_s=5.0)
        self.addCleanup(session.close)

        session.push_text_chunk("一二三四五六七八九十。二二三四五六七八九十。"
                                "三二三四五六七八九十。四二三四五六七八九十。")
        played = session.finish()

        self.assertEqual(len(played), 4)
        self.assertEqual(session.sentences_total, 4)
        self.assertEqual(session.sentences_played, 4)
        self.assertLessEqual(session.lookahead_peak, session.READY_QUEUE_CAPACITY)
        self.assertGreaterEqual(session.synth_wait_ms, 0)


class TestFailures(unittest.TestCase):
    """No failure may be reported as a success, and none may hang."""

    def setUp(self) -> None:
        self.addCleanup(self._close_sessions)
        self._sessions: list[SequentialVoiceSession] = []

    def _build(
        self, router: FakeRouter, worker: FakePlaybackWorker, **kwargs: object
    ) -> SequentialVoiceSession:
        session = build_session(router, worker, **kwargs)
        self._sessions.append(session)
        return session

    def _close_sessions(self) -> None:
        for session in self._sessions:
            session.close()

    def test_synthesis_failure_raises_and_short_play_is_visible(self) -> None:
        # The first sentence is allowed to really finish playing before the second
        # one is released to fail, so this test asserts the exact "later sentence
        # failed" shape instead of racing the two threads.
        router = FakeRouter(fail_on_index=2, chunk_count=1)
        router.gates[2] = Event()
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("第一句。第二句。第三句。")
        deadline = time.monotonic() + 3.0
        while not worker.completed_at and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(worker.completed_at)
        router.gates[2].set()

        with self.assertRaises(SequentialVoiceSessionError) as caught:
            session.finish()

        self.assertEqual(caught.exception.code, "pcm_seq_synthesis_failed")
        self.assertEqual(session.error_type, "SequentialVoiceSessionError")
        self.assertEqual(session.sentences_total, 3)
        self.assertEqual(session.sentences_synthesized, 1)
        self.assertEqual(session.sentences_played, 1)
        self.assertLess(session.sentences_played, session.sentences_total)
        self.assertEqual(session.played_segments, ("第一句。",))
        # The failure is what the caller records as speech_failed:<type name>.
        self.assertEqual(type(caught.exception).__name__, "SequentialVoiceSessionError")
        self.assertIsInstance(session.error, SequentialVoiceSessionError)
        self.assertEqual(
            "speech_failed:" + type(caught.exception).__name__,
            "speech_failed:SequentialVoiceSessionError",
        )

    def test_provider_error_code_and_failed_request_are_visible(self) -> None:
        router = FakeRouter(fail_on_index=2, failure_code="audio_too_large", chunk_count=1)
        router.gates[2] = Event()
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk('第一句。第二句。第三句。')
        deadline = time.monotonic() + 3.0
        while not worker.completed_at and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(worker.completed_at)
        router.gates[2].set()

        with self.assertRaises(SequentialVoiceSessionError):
            session.finish()

        self.assertEqual(session.provider_error_code, "audio_too_large")
        self.assertEqual(session.failed_sentence_index, 2)
        self.assertEqual(session.failed_request_chars, len('第二句。'))
        self.assertEqual(session.sentences_played, 1)
        self.assertLess(session.sentences_played, session.sentences_total)

    def test_blank_segments_are_dropped_and_the_reply_still_plays_in_full(self) -> None:
        """真机证据（2026-10-09，turn=2，`pcm_seq`）：回复里的空行被切成**纯空白碎片**，
        旧代码在**构造请求**时就以 `text_blank` 中止整条播报（真机 294 字回复：固定短码
        `text_blank`、失败请求只有 1 个字、13 段只播出 1 段，用户只听到第一句）。空白碎片
        没有任何可播内容，必须在**入队处**丢弃：整条回复照常播出，而本地契约对纯空白文本
        的拒绝（`text_blank`）一分都没有放松。
        """

        router = FakeRouter(chunk_count=1)
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("第一句。\n\n第二句。")
        played = session.finish()

        self.assertEqual(played, ("第一句。", "第二句。"))
        # The blank pieces never reach the provider at all.
        self.assertEqual(router.texts(), ["第一句。", "第二句。"])
        self.assertEqual(session.sentences_total, 2)
        self.assertEqual(session.sentences_synthesized, 2)
        self.assertEqual(session.sentences_played, 2)
        self.assertEqual(session.played_chars, len("第一句。第二句。"))
        self.assertIsNone(session.error)
        self.assertFalse(session.cancelled)
        self.assertIsNone(session.provider_error_code)
        # 契约层仍然拒绝纯空白请求：丢弃发生在入队处，不是把契约放松了。
        with self.assertRaises(VoiceStudioContractError) as caught:
            VoiceStudioSentenceRequest(
                reply_id="reply-blank",
                segment_id="segment-1",
                provider_request_id="reply-blank-1",
                provider_generation=1,
                text="\n",
                profile_id="confirmed-default",
                language="zh",
                speed=1.0,
            )
        self.assertEqual(caught.exception.code, "text_blank")

    def test_reply_with_paragraph_breaks_is_spoken_in_full(self) -> None:
        """回归（真机 2026-10-09 turn=2 的形状）：带段间空行的长回复必须全文播出，
        不允许再出现"13 段只播 1 段、其余被 `text_blank` 中止"。
        """

        reply = (
            "上海是中国最大的经济中心城市，位于长江入海口，也是全球重要的金融、贸易和航运枢纽。"
            "\n\n"
            "上海历史悠久，近代开埠后迅速发展为远东大都市，形成了独特的海派文化。"
            "\n\n"
            "上海交通便利，拥有两个国际机场、密集的地铁网络和高铁连接长三角及全国。"
        )
        router = FakeRouter(chunk_count=1)
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk(reply)
        played = session.finish()

        self.assertGreaterEqual(session.sentences_total, 3)
        self.assertEqual(session.sentences_synthesized, session.sentences_total)
        self.assertEqual(session.sentences_played, session.sentences_total)
        self.assertTrue(all(piece.strip() for piece in played))
        self.assertEqual("".join(played), reply.replace("\n", ""))
        self.assertIsNone(session.error)

    def test_synthesis_failure_is_never_reported_as_success(self) -> None:
        """The same shape without the gate: the counts stay honest, never equal."""

        router = FakeRouter(fail_on_index=2)
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("第一句。第二句。第三句。")
        with self.assertRaises(SequentialVoiceSessionError):
            session.finish()
        self.assertEqual(session.sentences_total, 3)
        self.assertEqual(session.sentences_synthesized, 1)
        self.assertLess(session.sentences_played, session.sentences_total)
        self.assertIsNotNone(session.error)

    def test_first_sentence_failure_plays_nothing(self) -> None:
        router = FakeRouter(fail_on_index=1)
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("第一句。第二句。")
        with self.assertRaises(SequentialVoiceSessionError):
            session.finish()
        self.assertEqual(session.sentences_total, 2)
        self.assertEqual(session.sentences_played, 0)
        self.assertEqual(session.sentences_synthesized, 0)

    def test_playback_never_completes_times_out_within_bound(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker(never_complete=True, block_full_timeout=True)
        session = self._build(
            router, worker, playback_timeout_s=0.3, join_timeout_s=5.0, synthesis_wait_s=0.3
        )

        session.push_text_chunk("唯一一句。")
        started = time.monotonic()
        with self.assertRaises(SequentialVoiceSessionError) as caught:
            session.finish()
        elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.code, "pcm_seq_playback_not_completed")
        self.assertEqual(session.sentences_total, 1)
        self.assertEqual(session.sentences_played, 0)
        self.assertLess(session.sentences_played, session.sentences_total)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 5.0)

    def test_playback_without_audio_is_a_recorded_failure(self) -> None:
        class EmptyRouter(FakeRouter):
            def stream_sentence(self, request, *, cancelled):  # type: ignore[override]
                del request, cancelled
                return iter(())

        router = EmptyRouter()
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("没有音频的一句。")
        with self.assertRaises(SequentialVoiceSessionError) as caught:
            session.finish()
        self.assertEqual(caught.exception.code, "pcm_seq_no_audio")
        self.assertEqual(session.sentences_synthesized, 0)
        self.assertEqual(session.sentences_played, 0)
        self.assertEqual(session.sentences_total, 1)

    def test_empty_pipeline_cannot_report_success(self) -> None:
        """The equality judged on the way out is exact, not approximate."""

        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("甲。乙。丙。")
        session.finish()
        self.assertEqual(
            session.sentences_played,
            session.sentences_synthesized,
        )
        self.assertEqual(session.sentences_synthesized, session.sentences_total)
        # Every code the channel can raise is a fixed short code (no text, no path).
        for code in SEQUENTIAL_ERROR_CODES:
            self.assertRegex(code, r"^[a-z0-9_]+$")
            self.assertTrue(code.startswith("pcm_seq_"))

    def test_push_after_finish_is_counted_not_swallowed(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = self._build(router, worker)

        session.push_text_chunk("第一句。")
        session.finish()
        session.push_text_chunk("这句话来得太晚了。")
        self.assertEqual(session.dropped_pushes, 1)
        self.assertEqual(session.sentences_total, 1)


class TestCancellation(unittest.TestCase):
    """``cancel()`` stops both threads, silences the device and never hangs."""

    def test_cancel_ends_both_threads_and_stops_the_worker(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker(never_complete=True, block_until_stopped=True)
        session = build_session(router, worker, playback_timeout_s=30.0, join_timeout_s=5.0)

        session.push_text_chunk("第一句。第二句。第三句。第四句。")
        # Wait until the playback thread really sits inside the ticket wait, which
        # is the state a user barge-in has to interrupt.
        deadline = time.monotonic() + 3.0
        while worker.begin_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(worker.begin_count, 1)

        started = time.monotonic()
        session.cancel()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertTrue(session.cancelled)
        self.assertGreaterEqual(worker.stop_calls, 1)
        synth = session._synth_thread  # noqa: SLF001 - the test owns the lifecycle
        play = session._play_thread  # noqa: SLF001 - the test owns the lifecycle
        synth.join(timeout=2.0)
        play.join(timeout=2.0)
        self.assertFalse(synth.is_alive())
        self.assertFalse(play.is_alive())
        # A cancellation is a designed stop: it is not a playback failure.
        played = session.finish()
        self.assertEqual(played, ())
        self.assertIsNone(session.error)
        session.close()

    def test_cancel_is_idempotent_and_close_is_safe(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = build_session(router, worker)
        session.push_text_chunk("一句话。")
        session.cancel()
        session.cancel()
        session.close()
        session.close()
        self.assertEqual(worker.close_calls, 1)

    def test_cancel_before_any_push_does_not_hang(self) -> None:
        router = FakeRouter()
        worker = FakePlaybackWorker()
        session = build_session(router, worker)
        started = time.monotonic()
        session.cancel()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(session.finish(), ())
        session.close()


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
