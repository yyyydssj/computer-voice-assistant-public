"""Offline tests for per-reply verification (defect B) and diagnosable overload (defect C).

Everything here is offline and deterministic: no socket, no VoiceStudio, no audio
device, no real-clock assertion and no ``time.sleep``. Progress is signalled with
``threading.Event`` objects, and the only real durations involved are ``finish()``'s
own bounded join (which must expire while the session thread is provably blocked,
so the timeout is a fact about the join, not about how long the machine is slow).

What is pinned down:

* **B** — identity verification runs **once per reply**, not once per sentence:
  20 paced sentences call the verifier exactly once, a verification failure is
  fail-closed (error recorded, nothing played, no sample), and a *new* reply does
  verify again — exactly once.
* **C** — a push refused by the closed/finished/cancelled/failed guard is no longer
  silent: it is counted in ``dropped_pushes`` and recorded as a redacted
  ``push_rejected_<reason>@generationN`` event, ``pending_sentences`` reports the
  live queue length, and a ``finish()`` whose join expires sets
  ``finish_timed_out`` plus a ``finish_timeout`` diagnostic instead of pretending
  the whole reply was spoken.
* **C'** (R4-B follow-up, real-machine "只播 74 字却说播完") — a *full queue* is no
  longer a refusal at all: ``_enqueue`` waits for room on the session condition
  (bounded by ``queue_wait_timeout_s``) and admits the batch **in full**, or refuses
  it **in full** with ``streaming_queue_budget_exceeded`` written to
  ``session.error``. There is no path that accepts a batch and then silently drops
  its tail, and ``overloaded`` is a diagnostic fact only.
"""
from __future__ import annotations

import getpass
import threading
import unittest
from threading import Event

from app.tts.pcm_playback import PCMPlaybackWorker
from app.tts.playback_worker import StopResult
from app.tts.private_selection import (
    CONFIRMED_PROFILE,
    CONFIRMED_REFERENCE_SHA256,
    VoiceSelection,
)
from app.tts.providers import PCMChunk, VoiceStudioSentenceRequest
from app.tts.streaming_session import StreamingVoiceSession, StreamingVoiceSessionError
from app.tts.transport import VoiceStudioTransportRouter

SAMPLE_RATE = 24_000
BLOCK_MS = 20
SENTENCE_COUNT = 20
# A join timeout small enough to expire while the provider is provably blocked on
# an Event that only the test releases (never a claim about machine speed).
SHORT_SEGMENT_TIMEOUT_S = 0.05


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


def make_selection() -> VoiceSelection:
    return VoiceSelection(
        profile_id=CONFIRMED_PROFILE,
        owner=getpass.getuser(),
        reference_sha256=CONFIRMED_REFERENCE_SHA256,
    )


class CountingVerifier:
    """Verifier that counts its calls and can fail on the first one."""

    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.calls = 0
        self.failure = failure
        self._lock = threading.Lock()

    def __call__(self, selection: VoiceSelection) -> None:
        del selection
        with self._lock:
            self.calls += 1
        if self.failure is not None:
            raise self.failure


class FastBackend:
    """Immediately-writable, always-drained in-memory backend (no device)."""

    backend_name = "verify-overload-fake"
    max_buffer_bytes = 9_600

    def __init__(self) -> None:
        self.submitted: list[bytes] = []
        self.stop_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        pass

    def writable(self) -> bool:
        return True

    def submit(self, pcm: bytes) -> None:
        self.submitted.append(bytes(pcm))

    def poll_drained(self) -> bool:
        return True

    def stop(self, timeout: float) -> StopResult:
        del timeout
        self.stop_calls += 1
        return StopResult(True, "backend_buffer_returned", "fake_reset")

    def close(self) -> None:
        self.close_calls += 1


class PerSentenceProvider:
    """One final PCM block per sentence plus a per-sentence ``started`` event."""

    def __init__(self, *, milliseconds: int = BLOCK_MS) -> None:
        self.milliseconds = milliseconds
        self.calls = 0
        self.close_calls = 0
        self._started: dict[int, Event] = {}
        self._lock = threading.Lock()

    def event(self, index: int) -> Event:
        """The Event that ``stream_sentence`` sets when sentence ``index`` starts."""

        with self._lock:
            return self._started.setdefault(index, Event())

    @staticmethod
    def _index(request: VoiceStudioSentenceRequest) -> int:
        try:
            return int(request.segment_id.rsplit("-", 1)[-1])
        except ValueError:  # pragma: no cover - segment ids are always segment-N
            return 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls += 1
        self.event(self._index(request)).set()
        yield PCMChunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=1,
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=pcm_bytes(self.milliseconds),
            is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class GatedProvider:
    """Blocks before yielding until the test releases it, so state stays stable."""

    def __init__(self, *, milliseconds: int = BLOCK_MS) -> None:
        self.milliseconds = milliseconds
        self.entered = Event()
        self.release = Event()
        self.calls = 0
        self.close_calls = 0

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: Event):
        self.calls += 1
        self.entered.set()
        self.release.wait(5.0)
        yield PCMChunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=1,
            sample_rate=SAMPLE_RATE,
            channels=1,
            sample_width=2,
            data=pcm_bytes(self.milliseconds),
            is_final=True,
        )

    def close(self) -> None:
        self.close_calls += 1


class _SessionCase(unittest.TestCase):
    def make_session(
        self, *, provider=None, verifier=None, max_pending_segments: int = 32, **overrides
    ):
        provider = provider if provider is not None else PerSentenceProvider()
        verifier = verifier if verifier is not None else CountingVerifier()
        backend = FastBackend()
        worker = PCMPlaybackWorker(
            lambda: backend, start_buffer_ms=1, stop_timeout=0.5
        )
        self.addCleanup(worker.close)
        router = VoiceStudioTransportRouter(
            mode="ws_pcm", ws_provider=provider, http_provider=provider
        )
        self.addCleanup(router.close)
        session = StreamingVoiceSession(
            make_selection(),
            transport_router=router,
            worker=worker,
            verifier=verifier,
            max_pending_segments=max_pending_segments,
            **overrides,  # type: ignore[arg-type]
        )
        self.addCleanup(session.close)
        return session, worker, router, backend


class PerReplyVerificationTests(_SessionCase):
    def test_twenty_paced_sentences_verify_exactly_once(self) -> None:
        verifier = CountingVerifier()
        provider = PerSentenceProvider()
        session, _worker, _router, backend = self.make_session(
            provider=provider, verifier=verifier
        )
        sentences = tuple(f"第{index}句。" for index in range(1, SENTENCE_COUNT + 1))
        self.assertFalse(session.verified, "verification must not happen before speech")

        # Paced style: push one sentence and wait for evidence that the session
        # actually reached that sentence before pushing the next one.
        for index, sentence in enumerate(sentences, start=1):
            session.push_text_chunk(sentence)
            self.assertTrue(
                provider.event(index).wait(5.0), f"第{index}句未进入 provider"
            )

        played = session.finish()

        self.assertEqual(played, sentences)
        self.assertEqual(provider.calls, SENTENCE_COUNT)
        self.assertEqual(
            verifier.calls,
            1,
            "一次回复（一次会话）只允许验证一次，不能每句重复验证",
        )
        self.assertTrue(session.verified)
        self.assertEqual(len(session.timing_samples), SENTENCE_COUNT)
        self.assertEqual(len(backend.submitted), SENTENCE_COUNT)

    def test_a_failed_verification_is_fail_closed(self) -> None:
        failure = RuntimeError("verify_provider_failed")
        verifier = CountingVerifier(failure=failure)
        provider = PerSentenceProvider()
        session, _worker, _router, backend = self.make_session(
            provider=provider, verifier=verifier
        )
        session.push_text_chunk("第一句。")

        with self.assertRaises(RuntimeError) as caught:
            session.finish()

        self.assertIs(caught.exception, failure)
        self.assertIs(session.error, failure)
        self.assertFalse(session.verified, "a failed verification must not be reported as verified")
        self.assertFalse(session.cancelled)
        self.assertEqual(session.played_segments, ())
        self.assertEqual(session.timing_samples, ())
        self.assertEqual(session.timing_percentiles().ready_to_submit.count, 0)
        self.assertEqual(backend.submitted, [])
        self.assertEqual(provider.calls, 0, "no sentence may be synthesised after a failed verification")
        self.assertEqual(verifier.calls, 1)

    def test_a_new_reply_verifies_again_exactly_once(self) -> None:
        verifier = CountingVerifier()
        provider = PerSentenceProvider()

        first, _worker, _router, _backend = self.make_session(
            provider=provider, verifier=verifier
        )
        first.push_text_chunk("第一句。")
        self.assertEqual(first.finish(), ("第一句。",))
        self.assertTrue(first.verified)
        self.assertEqual(verifier.calls, 1)

        # A new reply is a new session, so the identity is verified again — once.
        second, _worker2, _router2, _backend2 = self.make_session(
            provider=provider, verifier=verifier
        )
        second.push_text_chunk("第二句。")
        self.assertEqual(second.finish(), ("第二句。",))
        self.assertTrue(second.verified)
        self.assertEqual(verifier.calls, 2)
        self.assertNotEqual(first.reply_id, second.reply_id)


class DiagnosableOverloadTests(_SessionCase):
    def test_over_bound_batch_is_spoken_in_full_and_only_recorded(self) -> None:
        """一批本身就超过上界的句段：整批接受、全部播出，只记 `overloaded`。

        R4-B 契约变化：旧实现里"越界"= 后续推送一律被拒（`push_rejected_overloaded`），
        于是真机上"答复被砍掉一半"和"推送被拒"都藏在 `overloaded` 一个布尔里。现在越界
        只是**事实**：整批照播，队列满时后续推送等待而不是被拒。
        """

        provider = GatedProvider()
        session, _worker, _router, _backend = self.make_session(
            provider=provider, max_pending_segments=1
        )

        # One accepted batch of three sentences already exceeds the bound (there was
        # nothing to wait for): the batch is still admitted and spoken in full.
        session.push_text_chunk("第一句。第二句。第三句。")
        self.assertTrue(session.overloaded)
        self.assertEqual(session.dropped_pushes, 0)
        self.assertEqual(session.accepted_segments, 3)
        self.assertNotIn("push_rejected_overloaded@generation1", session.late_events)
        self.assertTrue(provider.entered.wait(5.0), "第一句必须已进入 provider")
        # The worker popped the sentence it is speaking; the other two are queued.
        self.assertEqual(session.pending_sentences, 2)

        # A further push waits for room instead of being refused: it must still be
        # blocked while the running sentence is provably gated, and it must be
        # admitted (in full) once the consumer makes room.
        sender = threading.Thread(target=lambda: session.push_text_chunk("第四句。"), daemon=True)
        sender.start()
        try:
            self.assertTrue(
                sender.is_alive(), "队列无余量时推送必须等待，绝不能被拒绝或静默丢弃"
            )
            provider.release.set()
            sender.join(5.0)
        finally:
            provider.release.set()
        self.assertFalse(sender.is_alive())

        played = session.finish()

        self.assertEqual(played, ("第一句。", "第二句。", "第三句。", "第四句。"))
        self.assertEqual(session.accepted_segments, 4)
        self.assertEqual(session.played_count, 4)
        self.assertEqual(session.pending_segments, 0)
        self.assertEqual(session.pending_sentences, 0)
        self.assertEqual(session.accepted_characters, session.played_characters)
        self.assertEqual(session.dropped_pushes, 0)
        self.assertNotIn("queue_budget_wait_timeout@generation1", session.late_events)

    def test_queue_wait_timeout_refuses_the_whole_batch_with_an_explicit_error(self) -> None:
        """等待超时：**整批拒绝** + 明确错误，绝不允许"部分入队后静默丢弃"。"""

        provider = GatedProvider()
        session, _worker, _router, _backend = self.make_session(
            provider=provider, max_pending_segments=1, queue_wait_timeout_s=0.05
        )

        session.push_text_chunk("第一句。第二句。")
        self.assertTrue(provider.entered.wait(5.0), "第一句必须已进入 provider（被门挡住）")
        self.assertEqual(session.pending_sentences, 1)
        self.assertEqual(session.accepted_segments, 2)

        with self.assertRaises(StreamingVoiceSessionError) as caught:
            session.push_text_chunk("第三句。")

        self.assertEqual(str(caught.exception), "streaming_queue_budget_exceeded")
        # The session recorded the error itself: "error is None while less was
        # spoken" must not be representable.
        self.assertIs(session.error, caught.exception)
        self.assertTrue(session.overloaded)
        self.assertIn("queue_budget_wait_timeout@generation1", session.late_events)
        # The refused batch was refused whole: its sentence is nowhere.
        self.assertEqual(session.accepted_segments, 2)
        self.assertEqual(session.pending_sentences, 1)
        self.assertNotIn("第三句。", session.played_segments)

        provider.release.set()
        # ``finish()`` re-raises the recorded error instead of reporting success.
        with self.assertRaises(StreamingVoiceSessionError):
            session.finish()
        self.assertIsNotNone(session.error)
        self.assertEqual(session.played_count, 0)

    def test_refusals_after_cancel_finish_and_close_carry_their_own_reason(self) -> None:
        provider = PerSentenceProvider()
        session, _worker, _router, _backend = self.make_session(provider=provider)

        session.push_text_chunk("第一句。")
        self.assertEqual(session.finish(), ("第一句。",))

        session.push_text_chunk("第二句。")
        self.assertEqual(session.dropped_pushes, 1)
        self.assertIn("push_rejected_finished@generation1", session.late_events)

        session.close()

        session.push_text_chunk("第三句。")
        self.assertEqual(session.dropped_pushes, 2)
        self.assertEqual(
            session.late_events[-1], f"push_rejected_closed@generation{session.generation}"
        )
        self.assertNotIn("第二句。", session.played_segments)
        self.assertNotIn("第三句。", session.played_segments)
        # No sentence text may ever reach the diagnostic surface.
        for event in session.late_events:
            self.assertNotIn("句", event)

    def test_refusal_after_cancel_is_recorded_with_the_generation(self) -> None:
        provider = GatedProvider()
        session, _worker, _router, _backend = self.make_session(provider=provider)
        session.cancel()
        generation = session.generation

        session.push_text_chunk("第一句。")

        self.assertEqual(session.dropped_pushes, 1)
        self.assertEqual(
            session.late_events[-1], f"push_rejected_cancelled@generation{generation}"
        )
        self.assertTrue(session.cancelled)

    def test_finish_reports_a_timeout_instead_of_pretending_the_reply_was_spoken(self) -> None:
        provider = GatedProvider()
        session, _worker, _router, _backend = self.make_session(
            provider=provider,
            segment_timeout_s=SHORT_SEGMENT_TIMEOUT_S,
            # P1：收尾判据已从「总时长」改为「连续无进展」。这里的 provider 被 Event
            # 永久挡住 ⇒ 真的没有任何进展，所以把**停滞预算**也设成同一个短值，
            # 断言与语义都不变（仍然必须有界放弃、仍然必须记 finish_timeout）。
            stall_timeout_s=SHORT_SEGMENT_TIMEOUT_S,
        )
        self.assertFalse(session.finish_timed_out)
        session.push_text_chunk("第一句。")
        self.assertTrue(provider.entered.wait(5.0), "句子必须已进入 provider")

        # The provider is blocked on an Event only this test can release, so the
        # bounded join must expire: the reply is provably not fully spoken.
        played = session.finish()

        self.assertEqual(played, ())
        self.assertTrue(session.finish_timed_out)
        self.assertIn("finish_timeout", " ".join(session.late_events))
        self.assertFalse(session.cancelled, "finish() must not silently cancel the speech itself")

        # ``close()`` keeps its designed cancellation semantics, and the timeout
        # makes that remaining-speech cancellation observable to the caller.
        provider.release.set()
        session.close()
        self.assertTrue(session.closed)
        self.assertTrue(session.cancelled)
        self.assertEqual(session.pending_sentences, 0)

    def test_a_normal_finish_reports_no_timeout_and_no_dropped_push(self) -> None:
        provider = PerSentenceProvider()
        session, _worker, _router, _backend = self.make_session(provider=provider)
        session.push_text_chunk("第一句。")

        self.assertEqual(session.finish(), ("第一句。",))

        self.assertFalse(session.finish_timed_out)
        self.assertNotIn("finish_timeout", session.late_events)
        self.assertEqual(session.dropped_pushes, 0)
        self.assertEqual(session.pending_sentences, 0)


if __name__ == "__main__":
    unittest.main()
