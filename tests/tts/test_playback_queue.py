"""P7 本地播放队列：仅元数据、顺序与预算，不代表设备实际播放。"""
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.tts import ProviderRequestPlaybackState
from app.tts.playback.queue import PlaybackQueue, PlaybackQueueBackpressure, PlaybackQueueError
from tests.tts import test_playback_facts


class PlaybackQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_playback_facts.ProviderRequestPlaybackTests("test_playback_facts_progress_in_order_and_preserve_snapshot_identity")
        fixture.setUp()
        self.fixture = fixture
        self.reply_id = fixture.request.reply_id
        self.queue = PlaybackQueue(fixture.lifecycle, fixture.registry)

    def _prepare(self, index: int):
        fixture = self.fixture
        request = fixture.requests.prepare(
            provider_request_id=f"queue-request-{index}", reply_id=self.reply_id,
            segment_id=f"queue-segment-{index}", request_sequence=index,
            text=f"第 {index} 句。", audio_format=fixture.audio_format,
            provider_generation=1, voice_session=fixture.profiles.bind_session(f"queue-session-{index}"),
        )
        fixture.lifecycle.accept(request.provider_request_id, event_id=f"queue-accepted-{index}")
        fixture.lifecycle.synthesis_started(request.provider_request_id, event_id=f"queue-started-{index}")
        fixture.lifecycle.record_audio_chunk(fixture._chunk_for(request, f"queue-chunk-{index}"))
        fixture.lifecycle.audio_produced(request.provider_request_id, event_id=f"queue-produced-{index}")
        return request

    def _enqueue(self, request, duration: int):
        chunk = "audio-chunk-1" if request is self.fixture.request else f"queue-chunk-{request.request_sequence}"
        return self.queue.enqueue(request.provider_request_id, audio_chunk_event_id=chunk, audio_duration_ms=duration)

    def _play(self, entry) -> None:
        playback = self.fixture.registry
        request_id = entry.provider_request_id
        chunk_id = entry.audio_chunk_event_id
        playback.commit(request_id, event_id=f"commit-{request_id}", audio_chunk_event_id=chunk_id)
        playback.start(request_id, event_id=f"start-{request_id}", audio_chunk_event_id=chunk_id)
        playback.played(request_id, event_id=f"played-{request_id}", audio_chunk_event_id=chunk_id, playback_offset_ms=1)

    def test_capacity_budget_and_backpressure_are_atomic(self) -> None:
        first = self._enqueue(self.fixture.request, 3000)
        second_request = self._prepare(2)
        second = self._enqueue(second_request, 3000)
        third_request = self._prepare(3)
        third = self._enqueue(third_request, 2000)
        self.assertEqual(self.queue.pending, (first, second, third))
        self.assertEqual(self.queue.reserved_duration_ms, 8000)
        fourth_request = self._prepare(4)
        with self.assertRaises(PlaybackQueueBackpressure):
            self._enqueue(fourth_request, 1)
        self.assertEqual(self.queue.pending, (first, second, third))
        self.assertIsNone(self.queue.active)
        self.assertIs(self.queue.begin(), first)
        with self.assertRaises(PlaybackQueueError):
            self.queue.finish(first.provider_request_id)
        self.assertEqual(self.queue.pending, (first, second, third))
        self._play(first)
        self.assertIs(self.queue.finish(first.provider_request_id), first)
        self.assertEqual(self.queue.reserved_duration_ms, 5000)
        self.assertIs(self._enqueue(fourth_request, 3000), self.queue.pending[-1])

    def test_duration_limit_and_invalid_input_do_not_consume_sequence(self) -> None:
        first = self._enqueue(self.fixture.request, 5000)
        second_request = self._prepare(2)
        for duration, error in ((3001, PlaybackQueueBackpressure), (0, PlaybackQueueError), (True, PlaybackQueueError)):
            with self.assertRaises(error):
                self._enqueue(second_request, duration)
        self.assertEqual(self.queue.pending, (first,))
        self.assertEqual(self.queue.reserved_duration_ms, 5000)
        self._enqueue(second_request, 3000)
        with self.assertRaises(PlaybackQueueError):
            self._enqueue(second_request, 1)
        self.assertEqual(len(self.queue.pending), 2)

    def test_strict_order_and_no_second_begin_until_terminal_finish(self) -> None:
        second_request = self._prepare(2)
        with self.assertRaises(PlaybackQueueError):
            self._enqueue(second_request, 1000)
        first = self._enqueue(self.fixture.request, 1000)
        second = self._enqueue(second_request, 1000)
        self.assertIs(self.queue.begin(), first)
        with self.assertRaises(PlaybackQueueError):
            self.queue.begin()
        self._play(first)
        self.queue.finish(first.provider_request_id)
        self.assertIs(self.queue.begin(), second)

    def test_cancellation_clears_local_queue_not_playback_or_device(self) -> None:
        first = self._enqueue(self.fixture.request, 1000)
        second_request = self._prepare(2)
        second = self._enqueue(second_request, 2000)
        self.queue.begin()
        self.assertEqual(self.queue.cancel(self.reply_id), (first, second))
        self.assertEqual(self.queue.pending, ())
        self.assertIsNone(self.queue.active)
        self.assertEqual(self.queue.reserved_duration_ms, 0)
        self.assertEqual(self.fixture.registry.get(first.provider_request_id).state, ProviderRequestPlaybackState.NOT_COMMITTED)
        with self.assertRaises(PlaybackQueueError):
            self._enqueue(self.fixture.request, 1000)
        with self.assertRaises(PlaybackQueueError):
            self.queue.begin()

    def test_aborted_playback_clears_remainder_and_fences_old_reply(self) -> None:
        first = self._enqueue(self.fixture.request, 1000)
        second_request = self._prepare(2)
        self._enqueue(second_request, 1000)
        self.queue.begin()
        registry = self.fixture.registry
        registry.commit(first.provider_request_id, event_id="abort-commit", audio_chunk_event_id=first.audio_chunk_event_id)
        registry.abort(first.provider_request_id, event_id="abort-fact", audio_chunk_event_id=first.audio_chunk_event_id, playback_offset_ms=0, reason="local stop")
        self.queue.finish(first.provider_request_id)
        self.assertEqual(self.queue.pending, ())
        with self.assertRaises(PlaybackQueueError):
            self._enqueue(second_request, 1000)

    def test_chunk_lifecycle_and_generation_fences_are_fail_closed(self) -> None:
        first = self._enqueue(self.fixture.request, 1000)
        second_request = self._prepare(2)
        with self.assertRaises(PlaybackQueueError):
            self.queue.enqueue(second_request.provider_request_id, audio_chunk_event_id="audio-chunk-1", audio_duration_ms=1000)
        self.assertEqual(self.queue.pending, (first,))
        self.fixture.lifecycle.activate_generation(second_request.provider_name, 2)
        with self.assertRaises(PlaybackQueueError):
            self._enqueue(second_request, 1000)
        with self.assertRaises(PlaybackQueueError):
            self.queue.begin()
        self.assertEqual(self.queue.pending, (first,))

    def test_constructor_and_lifecycle_terminal_fail_closed(self) -> None:
        for kwargs in ({"max_segments": 0}, {"max_duration_ms": True}):
            with self.assertRaises(PlaybackQueueError):
                PlaybackQueue(self.fixture.lifecycle, self.fixture.registry, **kwargs)
        first = self._enqueue(self.fixture.request, 1000)
        self.fixture.lifecycle.fail(first.provider_request_id, event_id="queue-lifecycle-failed", reason="local failure")
        with self.assertRaises(PlaybackQueueError):
            self.queue.begin()
        self.assertEqual(self.queue.pending, (first,))

    def test_chunk_requires_audio_produced_before_enqueue(self) -> None:
        request = self.fixture.requests.prepare(
            provider_request_id="queue-unproduced", reply_id="queue-unproduced-reply",
            segment_id="queue-unproduced-segment", request_sequence=1,
            text="尚未完成。", audio_format=self.fixture.audio_format,
            provider_generation=1,
            voice_session=self.fixture.profiles.bind_session("queue-unproduced-session"),
        )
        self.fixture.lifecycle.accept(request.provider_request_id, event_id="unproduced-accepted")
        self.fixture.lifecycle.synthesis_started(request.provider_request_id, event_id="unproduced-started")
        self.fixture.lifecycle.record_audio_chunk(self.fixture._chunk_for(request, "unproduced-chunk"))
        with self.assertRaises(PlaybackQueueError):
            self.queue.enqueue(request.provider_request_id, audio_chunk_event_id="unproduced-chunk", audio_duration_ms=1000)
        self.assertEqual(self.queue.pending, ())
        self.fixture.lifecycle.audio_produced(request.provider_request_id, event_id="unproduced-produced")
        self.assertEqual(self.queue.enqueue(request.provider_request_id, audio_chunk_event_id="unproduced-chunk", audio_duration_ms=1000).reply_id, request.reply_id)
    def test_queue_release_allows_next_reply_and_rejects_replay(self) -> None:
        first = self._enqueue(self.fixture.request, 1000)
        self.queue.begin()
        self._play(first)
        self.queue.finish(first.provider_request_id)
        with self.assertRaises(PlaybackQueueError):
            self._enqueue(self.fixture.request, 1000)
        other = self.fixture._prepare_other_request()
        self.fixture.lifecycle.accept(other.provider_request_id, event_id="other-accepted")
        self.fixture.lifecycle.synthesis_started(other.provider_request_id, event_id="other-synthesizing")
        self.fixture.lifecycle.record_audio_chunk(self.fixture._chunk_for(other, "other-queue-chunk"))
        self.fixture.lifecycle.audio_produced(other.provider_request_id, event_id="other-produced")
        entry = self.queue.enqueue(other.provider_request_id, audio_chunk_event_id="other-queue-chunk", audio_duration_ms=2000)
        self.assertIs(self.queue.begin(), entry)
    def test_entry_is_immutable_and_cancel_rejects_wrong_reply(self) -> None:
        first = self._enqueue(self.fixture.request, 1000)
        with self.assertRaises(FrozenInstanceError):
            first.audio_duration_ms = 2000
        with self.assertRaises(PlaybackQueueError):
            self.queue.cancel("other-reply")
        self.assertEqual(self.queue.pending, (first,))


if __name__ == "__main__":
    unittest.main()