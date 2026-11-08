"""P1 · pcm_seq 通道：收尾判据是「无进展」而不是「总时长」。

同一根因的第二条通路。``SequentialVoiceSession.finish()`` 原本对**两个**线程各做一次
``join(timeout=join_timeout_s)``（默认 40 s ⇒ 最长 80 s），超时就抛 ``pcm_seq_join_timeout``
⇒ 整条回复以 ``speech_failed`` 结束。离线复现见本模块第 1 条用例（旧行为替身对照在
``tests/tts/test_streaming_drain_stall.py`` 里有同构版本）。

P1 把收尾判据换成**停滞**：每次阻塞一个 ``stall_timeout_s``，醒来比对进展标记
（``(sentences_synthesized, len(played_order))`` 两个只增不减的句子级计数），标记变了就再来
一轮，没变才判停滞。停滞仍然是**显式失败**（``pcm_seq_join_timeout``，口径没放松），只是
detail 换成 ``stalled_on=<synth|play|both>,last_progress=<固定码>``。

三条用例：

1. ``ProgressingPipelineTests``：整条回复的排空时间**超过**旧的 ``join_timeout_s``，但一直在
   推进 ⇒ 必须全播。**P1 之前红**（旧实现两次 0.5 s join 就到期）。
2. ``StallWatchdogIsLiveTests``：停滞预算压到 0.3 s（远小于相邻进展间隔 ≈0.8 s）⇒ 必须判停滞
   ——证明看门狗不是「永不超时」的空转。
3. ``StuckPipelineTests``：设备永不完成 ⇒ 收尾必须有界结束并抛出固定错误。

完全离线：无 socket、无设备、无 VoiceStudio。假 worker 的播放时长是**累计**音频
（``played_audio_ms`` 不按句清零），所以「句 1 睡 0.8 s、句 2 再睡 1.6 s」。
"""
from __future__ import annotations

import time
import unittest

from app.tts.sequential_session import SequentialVoiceSessionError
from tests.tts.test_pcm_sequential_pipeline import (
    FakePlaybackWorker,
    FakeRouter,
    build_session,
)

TEXT = "第一句测试文本内容一。第二句测试文本内容二。"
"""2 个句段，各 11 字；字符精确、无空白，切句结果稳定。"""

CHUNK_COUNT = 4
CHUNK_MS = 200
"""句 1 的音频 800 ms、句 2 累计 1600 ms ⇒ 整条回复 ≈2.4 s 排空。"""

OLD_TOTAL_CAP_S = 0.5
"""旧实现的「总时长」上限：远小于 ≈2.4 s 的排空时间 ⇒ P1 之前必红。"""

PROGRESS_STALL_S = 5.0
"""停滞预算：远大于相邻进展间隔（≈0.8 s / 1.6 s）。"""

IMPATIENT_STALL_S = 0.3
"""停滞预算压到小于进展间隔 ⇒ 必须判停滞（看门狗有效性对照）。"""


def make_pipeline(**kwargs: object):
    return build_session(
        FakeRouter(chunk_count=CHUNK_COUNT, ms_each=CHUNK_MS),
        FakePlaybackWorker(),
        **kwargs,
    )


class ProgressingPipelineTests(unittest.TestCase):
    def test_slow_but_progressing_reply_must_complete(self) -> None:
        """排空时间超过旧的 ``join_timeout_s``，但一直在推进 ⇒ 不许判失败。

        P1 之前：两次 ``join(timeout=0.5 s)`` 到期 ⇒ 抛 ``pcm_seq_join_timeout`` ⇒ 红。
        P1 之后：收尾看 ``stall_timeout_s=5.0 s``，而进展间隔只有 ≈0.8 / 1.6 s ⇒ 全播。
        """

        session = make_pipeline(
            join_timeout_s=OLD_TOTAL_CAP_S,
            stall_timeout_s=PROGRESS_STALL_S,
        )
        self.addCleanup(session.close)

        session.push_text_chunk(TEXT)
        played = session.finish()

        self.assertEqual(len(played), 2, f"少播：{played}")
        self.assertEqual(session.sentences_total, 2)
        self.assertEqual(session.sentences_synthesized, 2)
        self.assertEqual(session.sentences_played, 2)
        self.assertIsNone(session.error, f"不该有错误：{session.error!r}")
        self.assertIsNotNone(session.drain_seconds)
        self.assertGreater(session.drain_seconds or 0.0, 1.5, "本用例必须真的等过 1.5 s 以上")
        self.assertIsNone(session.last_progress, "没有停滞就不该有停滞原因码")


class StallWatchdogIsLiveTests(unittest.TestCase):
    def test_stall_budget_below_progress_interval_still_fails(self) -> None:
        """停滞预算压到进展间隔以下 ⇒ 必须如实判停滞（仍然显式失败）。"""

        session = make_pipeline(
            join_timeout_s=OLD_TOTAL_CAP_S,
            stall_timeout_s=IMPATIENT_STALL_S,
        )
        self.addCleanup(session.close)

        session.push_text_chunk(TEXT)
        with self.assertRaises(SequentialVoiceSessionError) as caught:
            session.finish()

        self.assertEqual(caught.exception.code, "pcm_seq_join_timeout")
        self.assertIn("last_progress=", caught.exception.detail or "")
        self.assertIsNotNone(session.last_progress, "停滞必须留下固定原因码")
        self.assertLess(session.sentences_played, session.sentences_total)


class StuckPipelineTests(unittest.TestCase):
    def test_never_completing_device_fails_within_bound(self) -> None:
        """设备永不完成 ⇒ 收尾必须**有界**结束，并抛出会话自己的固定错误。"""

        session = build_session(
            FakeRouter(chunk_count=CHUNK_COUNT, ms_each=CHUNK_MS),
            FakePlaybackWorker(never_complete=True, block_until_stopped=True),
            join_timeout_s=OLD_TOTAL_CAP_S,
            stall_timeout_s=IMPATIENT_STALL_S,
            playback_timeout_s=30.0,
        )
        self.addCleanup(session.close)

        session.push_text_chunk(TEXT)
        started = time.monotonic()
        with self.assertRaises(SequentialVoiceSessionError) as caught:
            session.finish()
        elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.code, "pcm_seq_join_timeout")
        self.assertLess(elapsed, 5.0, f"卡住的管线没有有界结束，耗时 {elapsed:.1f}s")
        self.assertEqual(session.sentences_played, 0)


if __name__ == "__main__":
    unittest.main()
