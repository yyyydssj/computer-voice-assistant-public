"""P10 R4-B：**播报事实可视化 + 截断如实上报**的离线测试（真机"说一半就停"的回归）。

真机动机（2026-10-03，`--barge-in-mode off` 仍偶发中断，但每一轮都记
`spoken=true / reason=speech_finished`）：
`SessionStream.finish()` 之后紧跟的 `close()` 会**取消仍在播放的语音**（设计如此：有界收尾），
而旧代码只记 played/completed，调用方无从区分"真的播完"与"被截断"。
现在：① `SessionStream` 如实记 `finish_timed_out`/`late_events`/`dropped_pushes`/`cancelled`；
② `chat_loop._speak()` 在 `finish_timed_out` 为真时记 **`speech_truncated`**（不再声称播完）；
③ 会话把每轮 TTS 事实带进 `voice_turn`（按单调 `index` 取，绝不报上一轮的旧事实）。

本批（三份只读彻查后）追加：**排空全是空转**（`pending_sentences` 正常路径恒 0、
`PCMPlaybackWorker` 上根本没有 `finalize_completed`）⇒ 删掉 `_await_drain` 与 worker 排空等待；
真凶是 `streaming_session._enqueue()` 越界后只置 `overloaded`、不清队列，**已接受未播出的句段被
静默丢弃**（`played == completed` 同源、`error=None` ⇒ 对外看着像"播完"）⇒ 只加**只读事实**
（`overloaded` / `pending_sentences_after_finish` / `played_chars` / `late_event_codes`），
不加任何等待、不改时序；切分恢复**硬上限**（`MAX_PUSH_CHARS=120`，真句末优先）。

全部离线：假 session / 假 speaker，不打开设备、不放音。
"""

from __future__ import annotations

import types
import unittest
from typing import Any

from app.runtime.voice_entry import (
    MAX_PUSH_CHARS,
    SessionStream,
    StreamingSpeaker,
    split_for_push,
)
from tests.voice_loop.support import FakeSpeaker, FakeStream
from tests.voice_loop.test_chat_loop import FakeDriver, build_loop


class FakeSession:
    """`StreamingVoiceSession` 的最小替身；可注入截断/错误/丢推/过载事实。"""

    def __init__(
        self,
        *,
        chunks: tuple[str, ...] = ("你好",),
        timing: int = 1,
        error: Exception | None = None,
        finish_timed_out: bool = False,
        late_events: tuple[str, ...] = (),
        dropped_pushes: int = 0,
        overloaded: bool = False,
        pending_sentences: int = 0,
        drain_seconds: float | None = None,
        stall_timeouts: int | None = None,
        last_progress: str | None = None,
    ) -> None:
        self._chunks = chunks
        self._timing = timing
        self.error = error
        self.finish_timed_out = finish_timed_out
        self.late_events = late_events
        self.dropped_pushes = dropped_pushes
        self.cancelled = False
        # R4-B 真机真凶：越界过载后被静默丢弃的句段（`played == completed`、`error is None`）。
        self.overloaded = overloaded
        self.pending_sentences = pending_sentences
        self.provider_error_code = None
        self.failed_sentence_index = None
        self.failed_request_chars = None
        self.sentences_total = None
        self.sentences_synthesized = None
        self.sentences_played = None
        # P1 收尾事实（2026-10-13）：真机 10/10 条为 None 的根因是 `_take_speech_facts` 的
        # 白名单复制漏了这三个键；默认 None 表示"旧会话没有这些接口"，与本批前行为一致。
        self.drain_seconds = drain_seconds
        self.stall_timeouts = stall_timeouts
        self.last_progress = last_progress
        self.pushed: list[str] = []
        self.closed = 0

    @property
    def timing_samples(self) -> tuple[object, ...]:
        return tuple(object() for _ in range(self._timing))

    def push_text_chunk(self, text: str) -> None:
        self.pushed.append(text)

    def finish(self) -> tuple[str, ...]:
        return self._chunks

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed += 1


class SessionStreamFactsTests(unittest.TestCase):
    def test_complete_reply_records_no_truncation(self) -> None:
        session = FakeSession(chunks=("a。", "b。"), timing=2)
        record: dict[str, object] = {"route": "ws_pcm", "index": 0, "text_length": 0, "error": None}
        stream = SessionStream(session, record)
        stream.push_text_chunk("a。b。")
        played = stream.finish()
        self.assertEqual(len(played), 2)
        self.assertEqual(record["played_segments"], 2)
        self.assertEqual(record["played_chars"], 4)  # 真正播出的字数（只读事实）
        self.assertEqual(record["completed_sentences"], 2)
        self.assertIs(record["finish_timed_out"], False)
        self.assertIsNone(record["truncation_reason"])
        self.assertEqual(record["late_events"], ())
        self.assertEqual(record["dropped_pushes"], 0)
        self.assertIs(record["cancelled"], False)
        self.assertIs(record["overloaded"], False)
        self.assertEqual(record["pending_sentences_after_finish"], 0)
        # 端到端判据：推送的非空白字数 == 播出字数（假会话没有三计数接口，如实记 None）。
        self.assertEqual(record["pushed_characters"], 4)
        self.assertEqual(record["played_chars"], record["pushed_characters"])
        self.assertIsNone(record["accepted_segments"])
        self.assertIsNone(record["synthesized_segments"])
        self.assertEqual(session.closed, 1)

    def test_truncated_reply_records_the_fact(self) -> None:
        session = FakeSession(
            chunks=("你好。",),
            timing=1,
            finish_timed_out=True,
            late_events=("finish_timeout",),
            dropped_pushes=2,
        )
        record: dict[str, object] = {"route": "ws_pcm", "index": 1, "text_length": 0, "error": None}
        stream = SessionStream(session, record)
        stream.push_text_chunk("你好。")
        stream.finish()
        self.assertIs(record["finish_timed_out"], True)
        self.assertEqual(record["truncation_reason"], "finish_timeout")
        self.assertEqual(record["late_events"], ("finish_timeout",))
        self.assertEqual(record["dropped_pushes"], 2)

    def test_overloaded_reply_records_the_untold_fact(self) -> None:
        """真凶（已核实）：队列越界只置 `overloaded=True` 并让 `_run` 退出 ⇒ 已接受未播出的
        句段被静默丢弃，而 `played_segments == completed_sentences`、`error=None` 看起来像"播完"。
        因此 `overloaded`/`pending_sentences_after_finish`/`played_chars` 必须如实进 record。

        **契约变化（本批）**：旧实现只把这三项记成"只读事实"，`finish_timed_out` 仍是 False ⇒
        `chat_loop._speak()` 依旧记 `speech_finished`（用户听到"少播"，轮次却说"播完"）。
        现在只要"已接受未播出"的残留还在（或 `played != accepted`、或推送字数 > 播出字数），
        `finish_timed_out` 就必须为真并给出 `truncation_reason`，让整条链路记 `speech_truncated`。
        """

        session = FakeSession(
            chunks=("第一点",),
            timing=1,
            dropped_pushes=2,
            overloaded=True,
            pending_sentences=2,
        )
        record: dict[str, object] = {"route": "ws_pcm", "index": 0, "text_length": 0, "error": None}
        SessionStream(session, record).finish()
        self.assertIs(record["overloaded"], True)
        self.assertEqual(record["pending_sentences_after_finish"], 2)
        self.assertEqual(record["played_segments"], record["completed_sentences"])  # 同源假象
        self.assertEqual(record["played_chars"], 3)  # 只有"第一点"三个字真的播了
        # 队列还有残留 ⇒ 绝不允许被当作"完整播报"。
        self.assertIs(record["finish_timed_out"], True)
        self.assertEqual(record["truncation_reason"], "pending_after_finish")

    def test_error_is_reduced_to_type_name(self) -> None:
        session = FakeSession(error=RuntimeError("/secret/path"))
        record: dict[str, object] = {"route": "ws_pcm", "index": 2, "text_length": 0, "error": None}
        SessionStream(session, record).finish()
        self.assertEqual(record["error"], "RuntimeError")
        self.assertNotIn("secret", str(record["error"]))

    def test_speaker_records_get_a_monotonic_index(self) -> None:
        speaker = StreamingSpeaker(lambda: FakeSession(), label="ws_pcm")
        first = speaker.start_stream()
        second = speaker.start_stream()
        self.assertEqual(first.record["index"], 0)
        self.assertEqual(second.record["index"], 1)


class TruncationReportingTests(unittest.TestCase):
    def _loop_with_record(self, record: dict[str, object]):
        stream = FakeStream()

        class Recorder:
            def __init__(self) -> None:
                self.record = record

            def push_text_chunk(self, text: str) -> None:
                stream.push_text_chunk(text)

            def finish(self) -> None:
                stream.finish()

            def cancel(self) -> None:
                stream.cancel()

        loop, _registry, machine, _seam = build_loop(speaker=FakeSpeaker(Recorder()))
        return loop, machine

    def test_finish_timed_out_is_reported_as_truncated(self) -> None:
        loop, machine = self._loop_with_record({"finish_timed_out": True})
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "speech_truncated")
        self.assertFalse(report.spoken)  # 绝不声称完整播报
        self.assertTrue(report.submitted)
        self.assertEqual(machine.state.value, "idle")

    def test_complete_reply_still_reports_speech_finished(self) -> None:
        loop, _machine = self._loop_with_record({"finish_timed_out": False})
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "speech_finished")
        self.assertTrue(report.spoken)

    def test_stream_without_record_keeps_previous_behaviour(self) -> None:
        loop, _registry, _machine, _seam = build_loop(speaker=FakeSpeaker(FakeStream()))
        loop.start_turn()
        loop.release_input()
        report = loop.complete_turn()
        self.assertEqual(report.reason, "speech_finished")
        self.assertTrue(report.spoken)

    def test_driver_is_used(self) -> None:
        """确认测试替身确实走的是普通驱动路径（防止用例本身失效）。"""

        loop, _, _, _ = build_loop(driver=FakeDriver(), speaker=FakeSpeaker(FakeStream()))
        loop.start_turn()
        loop.release_input()
        self.assertTrue(loop.complete_turn().submitted)


class SentencePushTests(unittest.TestCase):
    """R4-B P2：长回复按句推送 + 有界背压（真机 249 字一次性推送撞队列预算）。

    切分口径（本批收窄）：**真句末优先 + 硬上限生效**。`52c4a61` 把上限放到 200 且只在真句末切，
    离线实测段数无收益，却让"无标点超长文本整段进一次合成请求"成为新失效模式。
    """

    def test_split_preserves_text_and_order(self) -> None:
        text = "第一句。第二句！第三句？" + "长" * 200
        pieces = split_for_push(text)
        self.assertEqual("".join(pieces), text)
        self.assertGreater(len(pieces), 3)
        self.assertLessEqual(max(len(item) for item in pieces), MAX_PUSH_CHARS + 1)

    def test_split_prefers_real_sentence_ends(self) -> None:
        self.assertEqual(
            split_for_push("第一句。第二句！第三句？"),
            ("第一句。", "第二句！", "第三句？"),
        )
        # `\n` 也是真句末：紧跟别的句末时自成一片（仍不增删字符、顺序不变）。
        text = "前半句\n后半句。"
        pieces = split_for_push(text)
        self.assertEqual(pieces, ("前半句\n", "后半句。"))
        self.assertEqual("".join(pieces), text)

    def test_unpunctuated_text_is_hard_cut_at_the_limit(self) -> None:
        """无标点超长文本（`52c4a61` 的失效模式）：必须硬切，且不增删字符。"""

        text = "长" * 500
        pieces = split_for_push(text)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(len(pieces), 5)  # 500 / 120 → 4×120 + 20
        self.assertLessEqual(max(len(item) for item in pieces), MAX_PUSH_CHARS + 1)
        self.assertEqual(pieces[0], "长" * MAX_PUSH_CHARS)

    def test_overlong_sentence_is_cut_after_the_last_secondary_break(self) -> None:
        text = "甲" * 100 + "，" + "乙" * 100
        pieces = split_for_push(text)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(pieces[0], "甲" * 100 + "，")  # 次级标点之后切
        self.assertLessEqual(max(len(item) for item in pieces), MAX_PUSH_CHARS + 1)

    def test_split_handles_empty_and_single_sentence(self) -> None:
        self.assertEqual(split_for_push(""), ())
        self.assertEqual(split_for_push("你好。"), ("你好。",))

    def test_push_is_sentence_wise_and_keeps_full_length(self) -> None:
        pushed: list[str] = []

        class Session:
            pending_sentences = 0

            def push_text_chunk(self, piece: str) -> None:
                pushed.append(piece)

        record: dict[str, object] = {"route": "ws_pcm", "index": 0, "text_length": 0, "error": None}
        stream = SessionStream(Session(), record)
        text = "第一句。第二句。第三句。"
        stream.push_text_chunk(text)
        self.assertEqual(pushed, ["第一句。", "第二句。", "第三句。"])
        self.assertEqual(record["text_length"], len(text))  # 记录的是整段长度

    def test_backpressure_waits_but_stays_bounded(self) -> None:
        import app.runtime.voice_entry as voice_entry

        pushed: list[str] = []

        class BusySession:
            """`pending_sentences` 永远不降：验证我们会等，但**有界**、绝不挂死。"""

            pending_sentences = 8

            def push_text_chunk(self, piece: str) -> None:
                pushed.append(piece)

        original = voice_entry.PUSH_BACKPRESSURE_TIMEOUT_S
        voice_entry.PUSH_BACKPRESSURE_TIMEOUT_S = 0.05
        try:
            record: dict[str, object] = {"route": "ws_pcm", "index": 0, "text_length": 0, "error": None}
            stream = SessionStream(BusySession(), record)
            stream.push_text_chunk("一句。二句。")
        finally:
            voice_entry.PUSH_BACKPRESSURE_TIMEOUT_S = original
        self.assertEqual(pushed, ["一句。", "二句。"])  # 超时后仍然全部推送
        self.assertGreater(int(record.get("backpressure_waits", 0) or 0), 0)

    def test_no_backpressure_api_is_tolerated(self) -> None:
        pushed: list[str] = []

        class PlainSession:
            def push_text_chunk(self, piece: str) -> None:
                pushed.append(piece)

        record: dict[str, object] = {"route": "ws_pcm", "index": 0, "text_length": 0, "error": None}
        SessionStream(PlainSession(), record).push_text_chunk("一句。二句。")
        self.assertEqual(pushed, ["一句。", "二句。"])


def _speech_facts(record: dict[str, object]) -> dict[str, object]:
    """走**真实**映射路径（`AutoVoiceSession._take_speech_facts`）取本轮播报事实。

    不实例化整个免按键会话：该函数只读 `_environment.speaker.records` 与 `_last_speech_index`。
    """

    from app.runtime.auto_voice import AutoVoiceSession

    holder = types.SimpleNamespace(
        _last_speech_index=-1,
        _environment=types.SimpleNamespace(speaker=types.SimpleNamespace(records=[record])),
    )
    facts = AutoVoiceSession._take_speech_facts(holder)
    assert facts is not None
    return facts


def _turn_facts(speech: dict[str, Any] | None) -> Any:
    from app.runtime.auto_voice import AutoTurnFacts

    return AutoTurnFacts(
        index=1,
        boundary=None,
        frames=1,
        speech_seen=True,
        end_reason="silence_timeout",
        accepted=True,
        submitted=True,
        spoken=True,
        reason="speech_finished",
        seconds=1.0,
        speech=speech,
    )


class SpeechFactMappingTests(unittest.TestCase):
    """`record` → 本轮事实 → `log_details()` 的映射（真机上唯一看得见的地方）。"""

    def _record(self, index: int = 0) -> dict[str, object]:
        return {"route": "ws_pcm", "index": index, "text_length": 0, "error": None}

    def test_overloaded_is_visible_in_log_details(self) -> None:
        session = FakeSession(chunks=("第一点",), timing=1, overloaded=True, pending_sentences=2)
        record = self._record()
        SessionStream(session, record).finish()
        details = _turn_facts(_speech_facts(record)).log_details()
        self.assertIs(details["speech_overloaded"], True)
        self.assertEqual(details["speech_pending_after_finish"], 2)
        self.assertEqual(details["speech_played_chars"], 3)

    def test_late_event_codes_are_joined_not_counted_only(self) -> None:
        """真机码丢失：旧实现只报个数（`late_events`），这里必须带上**码**。"""

        session = FakeSession(
            chunks=("a",),
            timing=1,
            late_events=("audio_backpressure", "finish_timeout"),
        )
        record = self._record()
        SessionStream(session, record).finish()
        facts = _speech_facts(record)
        self.assertEqual(facts["late_events"], 2)  # 个数照旧
        self.assertEqual(facts["late_event_codes"], "audio_backpressure|finish_timeout")
        details = _turn_facts(facts).log_details()
        self.assertEqual(details["speech_late_event_codes"], "audio_backpressure|finish_timeout")
        # 走**真实**映射路径的结果同样只允许扁平基础类型（真机教训：tuple 会让整个会话降级）。
        for key, value in details.items():
            with self.subTest(key=key):
                self.assertIn(
                    type(value),
                    (str, int, float, bool, type(None)),
                    f"{key} 必须是扁平基础类型，实际 {type(value).__name__}",
                )

    def test_late_event_codes_are_truncated_to_64_chars(self) -> None:
        session = FakeSession(chunks=("a",), timing=1, late_events=("x" * 40, "y" * 40))
        record = self._record()
        SessionStream(session, record).finish()
        codes = _speech_facts(record)["late_event_codes"]
        self.assertIsInstance(codes, str)
        self.assertLessEqual(len(codes), 64)
        self.assertTrue(codes.startswith("x" * 40 + "|"))

    def test_missing_late_events_stays_none(self) -> None:
        record = self._record()
        record["late_events"] = None  # 会话没有这个接口时
        self.assertIsNone(_speech_facts(record)["late_event_codes"])

    def test_pcm_seq_provider_error_and_sentence_facts_are_flat(self) -> None:
        session = FakeSession(chunks=("segment-2",), timing=1)
        session.provider_error_code = "audio_too_large"
        session.failed_sentence_index = 2
        session.failed_request_chars = 48
        session.sentences_total = 5
        session.sentences_synthesized = 1
        session.sentences_played = 1
        record = self._record()
        SessionStream(session, record).finish()

        self.assertEqual(record["provider_error_code"], "audio_too_large")
        self.assertEqual(record["failed_sentence_index"], 2)
        self.assertEqual(record["failed_request_chars"], 48)
        self.assertEqual(record["sentences_total"], 5)
        self.assertEqual(record["sentences_synthesized"], 1)
        self.assertEqual(record["sentences_played"], 1)
        facts = _speech_facts(record)
        self.assertEqual(facts["provider_error_code"], "audio_too_large")
        self.assertEqual(facts["failed_sentence_index"], 2)
        self.assertEqual(facts["failed_request_chars"], 48)

    def test_provider_failure_states_are_reduced_to_fixed_codes(self) -> None:
        """GitHub 调研（HA #182944「只播第一句」）：后续段在 provider 侧被拒必须可见。"""

        class StateSession(FakeSession):
            failure_states = {"req-1": type("S", (), {"value": "failed"})(), "req-2": "ok"}

        session = StateSession(chunks=("a",), timing=1)
        record = self._record()
        SessionStream(session, record).finish()
        self.assertEqual(record["provider_failure_codes"], ("failed", "ok"))
        self.assertEqual(record["provider_failure_count"], 2)

    def test_record_index_and_segment_flow_fields_are_visible_in_log_details(self) -> None:
        """R4-B 追加取证（只读）：`record_index` + 7 个段流字段必须出现在 `log_details()` 里。

        真机那一轮 `pushed_chars=282 / accepted_segments=1 /
        truncation_reason=played_chars_below_pushed` 三者不可能同时为真（282 字只要真到过
        segmenter 就必然切出 ≥5 段）⇒ 必须能从 `voice_turn` 的扁平字段里读出"段在哪一步消失"，
        以及 `speech_*` 到底属于**哪一条**播报流（`speech_record_index`）。
        这里只断言"字段出现 + 值如实 + 仍是扁平基础类型"，不改任何既有判据。
        """

        session = FakeSession(chunks=("第一句。",), timing=1)
        # 假会话没有这些计数接口时就该如实记 None（旧会话/替身零破坏）。
        record = self._record()
        SessionStream(session, record).finish()
        self.assertIsNone(record["dequeued_segments"])
        self.assertIsNone(record["segmenter_buffered_chars"])
        self.assertIsNone(_speech_facts(record)["dequeued_segments"])

        session = FakeSession(chunks=("第一句。",), timing=1)
        session.dequeued_segments_total = 5
        session.dequeued_characters_total = 282
        session.enqueued_segments_total = 1
        session.enqueued_characters_total = 12
        session.enqueue_dropped_segments_total = 4
        session.enqueue_dropped_characters_total = 270
        session.segmenter_buffered_characters = 0
        record = self._record(index=7)
        SessionStream(session, record).finish()
        self.assertEqual(record["dequeued_segments"], 5)
        self.assertEqual(record["dequeued_chars"], 282)
        self.assertEqual(record["enqueued_segments"], 1)
        self.assertEqual(record["enqueued_chars"], 12)
        self.assertEqual(record["enqueue_dropped_segments"], 4)
        self.assertEqual(record["enqueue_dropped_chars"], 270)
        self.assertEqual(record["segmenter_buffered_chars"], 0)

        facts = _speech_facts(record)
        self.assertEqual(facts["record_index"], 7)
        self.assertEqual(facts["dequeued_segments"], 5)
        self.assertEqual(facts["dequeued_chars"], 282)
        self.assertEqual(facts["enqueued_segments"], 1)
        self.assertEqual(facts["enqueued_chars"], 12)
        self.assertEqual(facts["enqueue_dropped_segments"], 4)
        self.assertEqual(facts["enqueue_dropped_chars"], 270)
        self.assertEqual(facts["segmenter_buffered_chars"], 0)

        details = _turn_facts(facts).log_details()
        self.assertEqual(details["speech_record_index"], 7)
        self.assertEqual(details["speech_dequeued_segments"], 5)
        self.assertEqual(details["speech_dequeued_chars"], 282)
        self.assertEqual(details["speech_enqueued_segments"], 1)
        self.assertEqual(details["speech_enqueued_chars"], 12)
        self.assertEqual(details["speech_enqueue_dropped_segments"], 4)
        self.assertEqual(details["speech_enqueue_dropped_chars"], 270)
        self.assertEqual(details["speech_segmenter_buffered_chars"], 0)
        # 既有那条"只含扁平基础类型"的断言在本批新字段上同样成立（真机教训：tuple 会让整个
        # 免按键会话被判不可用而降级为文字）。
        for key, value in details.items():
            with self.subTest(key=key):
                self.assertIn(
                    type(value),
                    (str, int, float, bool, type(None)),
                    f"{key} 必须是扁平基础类型，实际 {type(value).__name__}",
                )

    def test_segment_flow_fields_stay_flat_without_the_session_api(self) -> None:
        """没有这些接口的旧会话：新字段一律 None，仍然只含扁平基础类型。"""

        details = _turn_facts(_speech_facts(self._record())).log_details()
        for key in (
            "speech_record_index",
            "speech_dequeued_segments",
            "speech_dequeued_chars",
            "speech_enqueued_segments",
            "speech_enqueued_chars",
            "speech_enqueue_dropped_segments",
            "speech_enqueue_dropped_chars",
            "speech_segmenter_buffered_chars",
        ):
            with self.subTest(key=key):
                self.assertIn(key, details)
        self.assertEqual(details["speech_record_index"], 0)  # `start_stream()` 写的原值
        self.assertIsNone(details["speech_dequeued_segments"])
        self.assertIsNone(details["speech_segmenter_buffered_chars"])

    def test_p1_drain_facts_survive_the_real_mapping(self) -> None:
        """真机根因回归（2026-10-13）：P1 的 `drain_seconds`/`stall_timeouts`/`last_progress`
        必须穿过 `_take_speech_facts` 的**白名单复制**到达 `voice_turn`。

        证据：553 条真机 `voice_turn` 里，auto_voice 的 P1 映射落地之后只有最后 10 条带
        `speech_drain_seconds` 键，且 **10/10 全为 None**；而 `streaming_session.finish()`
        无条件写 `_drain_seconds`、`_stall_timeouts` 初值为 0 ⇒ 事实是在这一层被丢掉的。
        本测试走**真实**映射路径（不自己造事实 dict）。
        """

        session = FakeSession(
            chunks=("第一句。", "第二句。"),
            timing=2,
            drain_seconds=1.234,
            stall_timeouts=2,
            last_progress="segment_played",
        )
        record = self._record()
        SessionStream(session, record).finish()
        self.assertEqual(record["drain_seconds"], 1.234)
        self.assertEqual(record["stall_timeouts"], 2)
        self.assertEqual(record["last_progress"], "segment_played")

        facts = _speech_facts(record)
        self.assertEqual(facts["drain_seconds"], 1.234)
        self.assertEqual(facts["stall_timeouts"], 2)
        self.assertEqual(facts["last_progress"], "segment_played")

        details = _turn_facts(facts).log_details()
        self.assertEqual(details["speech_drain_seconds"], 1.234)
        self.assertEqual(details["speech_stall_timeouts"], 2)
        self.assertEqual(details["speech_last_progress"], "segment_played")

    def test_p1_drain_facts_are_none_without_the_session_api(self) -> None:
        """没有这些接口的旧会话：三个事实一律 None（读不到就如实记，绝不猜）。"""

        session = FakeSession(chunks=("你好。",), timing=1)
        record = self._record()
        SessionStream(session, record).finish()
        self.assertIsNone(record["drain_seconds"])
        self.assertIsNone(record["stall_timeouts"])
        self.assertIsNone(record["last_progress"])

        details = _turn_facts(_speech_facts(record)).log_details()
        for key in ("speech_drain_seconds", "speech_stall_timeouts", "speech_last_progress"):
            with self.subTest(key=key):
                self.assertIn(key, details)
                self.assertIsNone(details[key])

    def test_log_details_only_contains_flat_types(self) -> None:
        """真机教训（2026-10-03）：`voice_turn` 事实里出现 tuple（我的 `speech_provider_failures`）
        会让生命周期 emit 抛"只允许扁平基础类型"，整个免按键会话被降级为文字。
        ⇒ 任何新字段都必须是 str/int/float/bool/None（含本批新增的过载/码/字数事实）。"""

        from app.runtime.auto_voice import AutoTurnFacts

        facts = AutoTurnFacts(
            index=1,
            boundary=None,
            frames=1,
            speech_seen=True,
            end_reason="silence_timeout",
            accepted=True,
            submitted=True,
            spoken=True,
            reason="speech_finished",
            seconds=1.0,
            speech={
                "route": "ws_pcm",
                "text_length": 10,
                "played_segments": 1,
                "completed_sentences": 1,
                "finish_timed_out": False,
                "late_events": 0,
                "dropped_pushes": 0,
                "cancelled": False,
                "error": None,
                "provider_failure_codes": ("failed",),  # 原始 tuple 不得直接进日志
                "provider_failure_count": 1,
                "edge_failures": 0,
                "overloaded": False,
                "late_event_codes": "audio_backpressure",
                "pending_sentences_after_finish": 0,
                "played_chars": 10,
            },
        )
        details = facts.log_details()
        for key, value in details.items():
            with self.subTest(key=key):
                self.assertIn(
                    type(value),
                    (str, int, float, bool, type(None)),
                    f"{key} 必须是扁平基础类型，实际 {type(value).__name__}",
                )
        self.assertEqual(details["speech_provider_failures"], "failed")
        self.assertIs(details["speech_overloaded"], False)
        self.assertEqual(details["speech_late_event_codes"], "audio_backpressure")


if __name__ == "__main__":
    unittest.main()
