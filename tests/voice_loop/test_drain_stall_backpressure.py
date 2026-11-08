"""P1 · `SessionStream` 的背压等待与收尾进展事实。

两件事一起钉住：

1. **背压等待遇进展顺延**（``_await_room``）：旧实现是固定
   :data:`PUSH_BACKPRESSURE_TIMEOUT_S`（15 s）——与收尾上限同属"按总时长"的判据。长回复只要
   播放比合成慢，生产者就会在 15 s 后放弃等待并继续推送，把压力全推给 ``_enqueue`` 的整批拒绝。
   现在只要 ``pending_sentences`` 在下降就一直等；不下降（真卡住）仍在同一预算后放弃，且
   **放弃后照旧继续推送**（这里从不丢东西，失败由 ``_enqueue`` 显式给出）。
2. **收尾进展事实**：``drain_seconds`` / ``stall_timeouts`` / ``last_progress`` 三个只读、
   扁平字段；非法值（NaN/∞/负数/bool/大写码）一律记 ``None``，绝不猜。

完全离线：无 socket、无设备、无 VoiceStudio。等待预算可注入，所以"顺延"能被**确定性**断言，
而不是靠 15 s 的真实等待。
"""
from __future__ import annotations

import threading
import time
import unittest

from app.runtime.voice_entry import (
    PUSH_BACKPRESSURE_PENDING,
    SessionStream,
)


class DrainingQueueSession:
    """``pending_sentences`` 每 ``tick_s`` 递减一格，模拟"播放一直在推进"。"""

    def __init__(self, *, start: int = 8, floor: int = 3, tick_s: float = 0.05) -> None:
        self._pending = int(start)
        self._floor = int(floor)
        self._tick_s = float(tick_s)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def pending_sentences(self) -> int:
        return self._pending

    def _run(self) -> None:
        while not self._stop.wait(self._tick_s):
            if self._pending > self._floor:
                self._pending -= 1

    def close(self) -> None:
        self._stop.set()


class StuckQueueSession:
    """``pending_sentences`` 恒定不动：模拟队列真卡住。"""

    def __init__(self, *, pending: int = 9) -> None:
        self.pending_sentences = int(pending)


class FactSession:
    """只提供 ``_record_speech_facts`` 会读到的字段的最小替身。"""

    def __init__(self, **facts: object) -> None:
        self.pending_sentences = int(facts.pop("pending_sentences", 0))
        self.cancelled = bool(facts.pop("cancelled", False))
        self.played_segments = tuple(facts.pop("played_segments", ("句子。",)))
        self.timing_samples = tuple(facts.pop("timing_samples", (object(),)))
        for name, value in facts.items():
            setattr(self, name, value)

    def finish(self) -> tuple[str, ...]:
        return tuple(self.played_segments)

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        return None


class MinimalSession:
    """连 ``pending_sentences`` 都没有的最小替身。"""


def make_record() -> dict[str, object]:
    return {"route": "ws_pcm", "index": 0, "text_length": 0, "error": None}


class BackpressureProgressTests(unittest.TestCase):
    def test_wait_is_extended_while_the_queue_drains(self) -> None:
        """队列在下降 ⇒ 必须等到低于阈值，而不是在固定预算处放弃。

        预算 0.1 s、每 0.05 s 才降一格、从 8 降到 3：固定预算下最多撑到 ~0.1 s（此时
        ``pending_sentences`` 仍是 6~7，远高于阈值），只有"看到进展就顺延"才可能等到 3。
        """

        session = DrainingQueueSession(start=8, floor=3, tick_s=0.05)
        self.addCleanup(session.close)
        record = make_record()
        stream = SessionStream(session, record, backpressure_timeout_s=0.1)

        started = time.monotonic()
        stream._await_room()  # noqa: SLF001 - 本用例测的就是这个等待
        elapsed = time.monotonic() - started

        self.assertLessEqual(
            session.pending_sentences,
            PUSH_BACKPRESSURE_PENDING - 1,
            f"队列在推进，却在高水位处放弃：pending={session.pending_sentences}",
        )
        self.assertGreater(
            elapsed, 0.1, f"没有越过固定预算 ⇒ 顺延没生效（elapsed={elapsed:.3f}s）"
        )
        waits = record.get("backpressure_waits")
        self.assertIsInstance(waits, int)
        self.assertGreaterEqual(int(waits or 0), 6, f"等待轮次太少：{waits}")

    def test_stuck_queue_gives_up_after_the_budget(self) -> None:
        """队列不下降 ⇒ 仍在固定预算后放弃（看门狗不会变成永久等待）。"""

        session = StuckQueueSession(pending=9)
        record = make_record()
        stream = SessionStream(session, record, backpressure_timeout_s=0.1)

        started = time.monotonic()
        stream._await_room()  # noqa: SLF001
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, f"卡住的队列等待过久：{elapsed:.3f}s")
        self.assertEqual(session.pending_sentences, 9, "等待不得丢任何东西")
        self.assertLessEqual(int(record.get("backpressure_waits") or 0), 8)

    def test_missing_pending_counter_returns_immediately(self) -> None:
        """读不到 ``pending_sentences`` 就立即返回（与改动前一致）。"""

        record = make_record()
        stream = SessionStream(MinimalSession(), record)
        started = time.monotonic()
        stream._await_room()  # noqa: SLF001
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertNotIn("backpressure_waits", record)

    def test_constructor_rejects_a_non_positive_budget(self) -> None:
        for bad in (0, -1, 0.0):
            with self.assertRaises(ValueError):
                SessionStream(MinimalSession(), make_record(), backpressure_timeout_s=bad)


class DrainStallFactTests(unittest.TestCase):
    def test_p1_facts_are_recorded(self) -> None:
        session = FactSession(
            drain_seconds=2.4567, stall_timeouts=1, last_progress="segment_played"
        )
        record = make_record()
        stream = SessionStream(session, record)
        stream.finish()
        self.assertEqual(record["drain_seconds"], 2.457)
        self.assertEqual(record["stall_timeouts"], 1)
        self.assertEqual(record["last_progress"], "segment_played")

    def test_non_flat_or_invalid_values_are_recorded_as_none(self) -> None:
        session = FactSession(
            drain_seconds=float("nan"),
            stall_timeouts=True,  # bool 不是整数事实
            last_progress="NotACode!",  # 大写与感叹号都不是固定短码
        )
        record = make_record()
        stream = SessionStream(session, record)
        stream.finish()
        self.assertIsNone(record["drain_seconds"])
        self.assertIsNone(record["stall_timeouts"])
        self.assertIsNone(record["last_progress"])

    def test_absent_facts_stay_none(self) -> None:
        """`SequentialVoiceSession` 没有 ``stall_timeouts`` ⇒ 如实记 None，绝不猜。"""

        session = FactSession(drain_seconds=1.0, last_progress="sentence_played")
        record = make_record()
        stream = SessionStream(session, record)
        stream.finish()
        self.assertEqual(record["drain_seconds"], 1.0)
        self.assertIsNone(record["stall_timeouts"])
        self.assertEqual(record["last_progress"], "sentence_played")

    def test_negative_and_infinite_seconds_are_none(self) -> None:
        for bad in (-0.5, float("inf"), float("-inf")):
            session = FactSession(drain_seconds=bad)
            record = make_record()
            stream = SessionStream(session, record)
            stream.finish()
            self.assertIsNone(record["drain_seconds"], f"{bad!r} 不该被当成秒数事实")


if __name__ == "__main__":
    unittest.main()
