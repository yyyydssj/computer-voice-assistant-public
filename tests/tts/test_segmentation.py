from __future__ import annotations

import unittest

from app.tts.segmentation import (
    ReplySegmenter,
    SegmentationConfig,
    SegmentationError,
    SegmentationDecision,
    SegmentBoundary,
    TextChunk,
)


class ReplySegmenterTests(unittest.TestCase):
    def make_chunk(
        self,
        sequence: int,
        text: str,
        *,
        chunk_id: str | None = None,
        reply_id: str = "reply-1",
        is_final: bool = False,
    ) -> TextChunk:
        return TextChunk(
            chunk_id=chunk_id or f"chunk-{sequence}",
            reply_id=reply_id,
            sequence=sequence,
            text=text,
            is_final=is_final,
        )

    def test_empty_text_is_rejected_without_advancing_sequence(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        rejected = segmenter.submit(self.make_chunk(1, ""))
        self.assertEqual(rejected.decision, SegmentationDecision.EMPTY_TEXT)
        self.assertEqual(segmenter.last_sequence, 0)
        accepted = segmenter.submit(self.make_chunk(1, "你好。", chunk_id="chunk-valid"))
        self.assertEqual(accepted.decision, SegmentationDecision.ACCEPTED)
        self.assertEqual([item.text for item in accepted.segments], ["你好。"])

    def test_extremely_short_token_fragments_are_buffered_until_complete_sentence(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        fragments = ("学", "长", "，", "你", "好", "。")
        for sequence, text in enumerate(fragments, start=1):
            result = segmenter.submit(self.make_chunk(sequence, text))
            if sequence < len(fragments):
                self.assertEqual(result.segments, ())
        self.assertEqual([item.text for item in result.segments], ["学长，你好。"])
        segment = result.segments[0]
        self.assertEqual(segment.boundary, SegmentBoundary.PUNCTUATION)
        self.assertEqual((segment.source_sequence_start, segment.source_sequence_end), (1, 6))
        self.assertEqual(segment.character_count, len("学长，你好。"))

    def test_multiple_complete_sentences_keep_original_order_and_metadata(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        result = segmenter.submit(self.make_chunk(1, "第一句。第二句！\n第三句？"))
        self.assertEqual(result.decision, SegmentationDecision.ACCEPTED)
        self.assertEqual([item.text for item in result.segments], ["第一句。", "第二句！", "第三句？"])
        self.assertEqual([item.segment_index for item in result.segments], [1, 2, 3])
        self.assertEqual(len({item.segment_id for item in result.segments}), 3)
        self.assertTrue(all(item.reply_id == "reply-1" for item in result.segments))
        self.assertEqual(segmenter.queued_segments(), result.segments)

    def test_newline_is_a_complete_sentence_boundary(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        result = segmenter.submit(self.make_chunk(1, "第一行\n第二行"))
        self.assertEqual([item.text for item in result.segments], ["第一行"])
        self.assertEqual(result.segments[0].boundary, SegmentBoundary.NEWLINE)
        self.assertEqual(segmenter.buffered_text, "第二行")

    def test_maximum_length_reply_id_still_produces_valid_fixed_length_segment_id(self) -> None:
        reply_id = "r" * 128
        segmenter = ReplySegmenter(reply_id=reply_id)
        result = segmenter.submit(self.make_chunk(1, "完整句。", reply_id=reply_id))
        self.assertEqual(len(result.segments), 1)
        self.assertLessEqual(len(result.segments[0].segment_id), 128)


    def test_duplicate_and_out_of_order_chunks_are_rejected_atomically(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        first = segmenter.submit(self.make_chunk(1, "前半句"))
        self.assertEqual(first.decision, SegmentationDecision.ACCEPTED)
        duplicate = segmenter.submit(self.make_chunk(2, "重复", chunk_id="chunk-1"))
        self.assertEqual(duplicate.decision, SegmentationDecision.DUPLICATE)
        out_of_order = segmenter.submit(self.make_chunk(3, "越序。"))
        self.assertEqual(out_of_order.decision, SegmentationDecision.OUT_OF_ORDER)
        completed = segmenter.submit(self.make_chunk(2, "后半句。"))
        self.assertEqual([item.text for item in completed.segments], ["前半句后半句。"])

    def test_old_reply_is_rejected_without_polluting_current_reply(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-current")
        stale = segmenter.submit(self.make_chunk(1, "旧回复。", reply_id="reply-old"))
        self.assertEqual(stale.decision, SegmentationDecision.STALE_REPLY)
        current = segmenter.submit(self.make_chunk(1, "当前回复。", reply_id="reply-current"))
        self.assertEqual([item.text for item in current.segments], ["当前回复。"])

    def test_queue_budget_applies_backpressure_without_consuming_rejected_chunk(self) -> None:
        segmenter = ReplySegmenter(
            reply_id="reply-1",
            config=SegmentationConfig(max_queued_segments=2),
        )
        accepted = segmenter.submit(self.make_chunk(1, "一。二。"))
        self.assertEqual(len(accepted.segments), 2)
        blocked = segmenter.submit(self.make_chunk(2, "三。"))
        self.assertEqual(blocked.decision, SegmentationDecision.QUEUE_BUDGET_EXCEEDED)
        self.assertEqual(segmenter.last_sequence, 1)
        self.assertEqual([item.text for item in segmenter.queued_segments()], ["一。", "二。"])
        self.assertEqual([item.text for item in segmenter.dequeue(1)], ["一。"])
        retried = segmenter.submit(self.make_chunk(2, "三。", chunk_id="chunk-2-retry"))
        self.assertEqual([item.text for item in retried.segments], ["三。"])
        self.assertEqual([item.text for item in segmenter.queued_segments()], ["二。", "三。"])

    def test_cancel_clears_buffer_and_queue_and_rejects_late_text(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        segmenter.submit(self.make_chunk(1, "已完成。还没说完"))
        cancelled = segmenter.cancel()
        self.assertEqual(cancelled.dropped_segment_count, 1)
        self.assertEqual(cancelled.dropped_buffered_characters, len("还没说完"))
        self.assertEqual(segmenter.queued_segments(), ())
        late = segmenter.submit(self.make_chunk(2, "迟到文本。"))
        self.assertEqual(late.decision, SegmentationDecision.AFTER_CANCELLED)
        self.assertEqual(late.segments, ())

    def test_final_chunk_flushes_tail_and_terminal_rejects_late_text(self) -> None:
        segmenter = ReplySegmenter(reply_id="reply-1")
        result = segmenter.submit(self.make_chunk(1, "最后没有标点", is_final=True))
        self.assertEqual([item.text for item in result.segments], ["最后没有标点"])
        self.assertEqual(result.segments[0].boundary, SegmentBoundary.FINAL)
        late = segmenter.submit(self.make_chunk(2, "不应接纳。"))
        self.assertEqual(late.decision, SegmentationDecision.AFTER_TERMINAL)

    def test_reasonable_length_fallback_never_emits_tiny_token_chunks(self) -> None:
        segmenter = ReplySegmenter(
            reply_id="reply-1",
            config=SegmentationConfig(min_segment_characters=4, max_segment_characters=8),
        )
        first = segmenter.submit(self.make_chunk(1, "甲乙"))
        self.assertEqual(first.segments, ())
        second = segmenter.submit(self.make_chunk(2, "丙丁戊己庚辛壬"))
        self.assertEqual([item.text for item in second.segments], ["甲乙丙丁戊己庚辛"])
        self.assertEqual(second.segments[0].boundary, SegmentBoundary.LENGTH)
        self.assertEqual(segmenter.buffered_text, "壬")

    def test_realtime_soft_policy_validation(self) -> None:
        config = SegmentationConfig(
            min_segment_characters=4, max_segment_characters=48,
            soft_target_characters=32, soft_search_min_characters=24,
            soft_search_max_characters=40,
        )
        self.assertEqual(config.soft_target_characters, 32)
        invalid = (
            {"soft_target_characters": 32},
            {"soft_target_characters": 32, "soft_search_min_characters": 24,
             "soft_search_max_characters": None},
            {"soft_target_characters": True, "soft_search_min_characters": 24,
             "soft_search_max_characters": 40},
            {"soft_target_characters": 22, "soft_search_min_characters": 24,
             "soft_search_max_characters": 40},
            {"soft_target_characters": 32, "soft_search_min_characters": 24,
             "soft_search_max_characters": 49},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(SegmentationError):
                SegmentationConfig(max_segment_characters=48, **values)

    def test_realtime_soft_boundary_at_target_and_hard_fallback(self) -> None:
        config = SegmentationConfig(
            min_segment_characters=4, max_segment_characters=48,
            soft_target_characters=32, soft_search_min_characters=24,
            soft_search_max_characters=40,
        )
        with_boundary = ReplySegmenter(reply_id="reply-1", config=config)
        text = "a" * 27 + " " + "b" * 4
        early = with_boundary.submit(self.make_chunk(1, text[:31]))
        self.assertEqual(early.segments, ())
        ready = with_boundary.submit(self.make_chunk(2, text[31:]))
        self.assertEqual([item.text for item in ready.segments], ["a" * 27])
        self.assertEqual(ready.segments[0].boundary, SegmentBoundary.LENGTH)
        self.assertEqual(with_boundary.buffered_text, "b" * 4)

        no_boundary = ReplySegmenter(reply_id="reply-1", config=config)
        self.assertEqual(no_boundary.submit(self.make_chunk(1, "x" * 32)).segments, ())
        self.assertEqual(no_boundary.submit(self.make_chunk(2, "x" * 15)).segments, ())
        forced = no_boundary.submit(self.make_chunk(3, "x" * 2))
        self.assertEqual([len(item.text) for item in forced.segments], [48])
        self.assertEqual(no_boundary.buffered_text, "x")
        self.assertEqual(forced.segments[0].boundary, SegmentBoundary.LENGTH)

    def test_default_length_policy_still_waits_until_hard_limit(self) -> None:
        segmenter = ReplySegmenter(
            reply_id="reply-1",
            config=SegmentationConfig(min_segment_characters=4, max_segment_characters=48),
        )
        self.assertEqual(segmenter.submit(self.make_chunk(1, "a" * 31 + " ")).segments, ())
        self.assertEqual(segmenter.submit(self.make_chunk(2, "b" * 15)).segments, ())
        result = segmenter.submit(self.make_chunk(3, "c"))
        self.assertEqual(result.segments[0].text, "a" * 31)
        self.assertEqual(result.segments[0].boundary, SegmentBoundary.LENGTH)


if __name__ == "__main__":
    unittest.main()
