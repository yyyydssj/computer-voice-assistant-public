r"""P9 真机缺陷回归：edge 离线播报不得因"合成快于实时"被背压掐断。

真机证据（2026-10-01，用户明确反馈"完全没听到声音"）：

```text
播报诊断：{'route': 'edge_sapi_local', 'chunks': 40, 'error': 'PCMPlaybackBackpressure'}
[real_waveout] chunks=40 bytes=384000 synth_s=0.38 audio_s=8.00 state=QUEUED
[wide_queue]   chunks=42 synth_s=0.38 final_state=COMPLETED total_s=8.67
```

离线 SAPI 合成远快于实时，`EdgeStream.push_text_chunk` 一次性推入全部分片会立刻顶到
`PCMPlaybackWorker` 的队列预算（默认 8 秒音频），`push_chunk` 抛
`PCMPlaybackBackpressure`，异常被静默记录后**播放从未开始**。

本测试用**真实任务文本的等价长度**（8.4 秒 PCM）加一个"按实时速度归还设备缓冲"的
离线假后端复现该现象，并验证节流后能正常交付到设备。
"""

from __future__ import annotations

import threading
import time
import unittest

from app.tts.pcm_playback import PCMPlaybackBackpressure, PCMPlaybackWorker, StopResult
from app.tts.providers import PCMChunk, VoiceStudioSentenceRequest
from app.tts.private_selection import CONFIRMED_PROFILE
from app.runtime.voice_entry import EdgeStream

SAMPLE_RATE = 24000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000
TEXT = "我听到你说：你好，小饼。这是免按键自动收音的真机验证回复。"


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


class RealTimePCMBackend:
    """按设备速度（每毫秒音频 1 毫秒）归还缓冲的离线后端；绝不打开真实设备。

    ``slots`` 个缓冲被占用时 ``writable()`` 为假，``poll_drained()`` 只有在全部归还
    后才为真——与真实 ``waveOut`` 的三缓冲复用语义一致。
    """

    backend_name = "realtime-fake-pcm"
    max_buffer_bytes = 9600
    slots = 3

    def __init__(self) -> None:
        self.submitted: list[bytes] = []
        self.open_calls = 0
        self.close_calls = 0
        self._inflight: list[float] = []  # 每项是"归还时刻"
        self._lock = threading.Lock()

    def open(self) -> None:
        self.open_calls += 1

    def writable(self) -> bool:
        with self._lock:
            self._collect()
            return len(self._inflight) < self.slots

    def submit(self, pcm: bytes) -> None:
        with self._lock:
            self._collect()
            if len(self._inflight) >= self.slots:
                raise RuntimeError("fake_backend_busy")
            self.submitted.append(bytes(pcm))
            self._inflight.append(time.monotonic() + len(pcm) / BYTES_PER_MS / 1000.0)

    def poll_drained(self) -> bool:
        with self._lock:
            self._collect()
            return not self._inflight

    def stop(self, timeout: float) -> StopResult:
        del timeout
        with self._lock:
            self._inflight.clear()
        return StopResult(True, "backend_buffer_returned", "fake_reset")

    def close(self) -> None:
        self.close_calls += 1

    def _collect(self) -> None:
        now = time.monotonic()
        self._inflight = [deadline for deadline in self._inflight if deadline > now]


class ScriptedChunkProvider:
    """按分片脚本输出 PCM；不联网、不合成。"""

    def __init__(self, *, chunk_ms: int) -> None:
        self.chunk_ms = chunk_ms
        self.calls: list[VoiceStudioSentenceRequest] = []

    def stream_sentence(self, request: VoiceStudioSentenceRequest, *, cancelled: threading.Event):
        self.calls.append(request)
        sequence = 1
        for _ in range(42):  # 42 × 200ms = 8.4 秒音频（与真机那句回复等长）
            if cancelled.is_set():
                return
            yield PCMChunk(
                reply_id=request.reply_id,
                segment_id=request.segment_id,
                provider_request_id=request.provider_request_id,
                provider_generation=request.provider_generation,
                sequence=sequence,
                sample_rate=SAMPLE_RATE,
                channels=1,
                sample_width=2,
                data=pcm_bytes(self.chunk_ms),
                is_final=sequence == 42,
            )
            sequence += 1

    def close(self) -> None:
        return None


def make_selection():
    from app.tts.private_selection import VoiceSelection

    return VoiceSelection(CONFIRMED_PROFILE, "tester", speed=1.0)


def make_stream(provider, backend, **worker_kwargs) -> EdgeStream:
    worker = PCMPlaybackWorker(lambda: backend, clock=time.monotonic, **worker_kwargs)
    return EdgeStream(provider, worker, make_selection())


class EdgePlaybackPacingTests(unittest.TestCase):
    def test_unpaced_push_hits_the_queue_budget(self) -> None:
        """基线：不做节流时，同样长度的音频必然撞队列预算（真机复现）。"""

        backend = RealTimePCMBackend()
        worker = PCMPlaybackWorker(lambda: backend, clock=time.monotonic)
        request = VoiceStudioSentenceRequest(
            reply_id="reply-x",
            segment_id="segment-1",
            provider_request_id="request-x",
            provider_generation=1,
            text=TEXT,
            profile_id=CONFIRMED_PROFILE,
            language="Chinese",
            speed=1.0,
        )
        provider = ScriptedChunkProvider(chunk_ms=200)
        worker.begin_sentence(request)
        with self.assertRaises(PCMPlaybackBackpressure):
            for chunk in provider.stream_sentence(request, cancelled=threading.Event()):
                worker.push_chunk(chunk)
        worker.close()

    def test_throttled_edge_stream_delivers_the_whole_sentence(self) -> None:
        backend = RealTimePCMBackend()
        provider = ScriptedChunkProvider(chunk_ms=200)
        stream = make_stream(provider, backend)
        stream.push_text_chunk(TEXT)
        self.assertIsNone(stream.record.get("error"), stream.record)
        self.assertEqual(stream.record["chunks"], 42)
        self.assertEqual(stream.record["playback_state"], "COMPLETED")
        # 节流确实生效：水位从未超过安全线，且至少等待过一次。
        self.assertGreater(stream.record["throttle_waits"], 0)
        self.assertLessEqual(stream.record["queued_audio_ms_peak"], 6000 + 200)
        self.assertIn("gate_to_device_ms", stream.record)
        total_bytes = sum(len(item) for item in backend.submitted)
        self.assertEqual(total_bytes, 42 * len(pcm_bytes(200)))
        stream.finish()
        self.assertEqual(backend.close_calls, 1)

    def test_short_sentence_needs_no_throttling_and_still_completes(self) -> None:
        backend = RealTimePCMBackend()

        class ShortProvider(ScriptedChunkProvider):
            def stream_sentence(self, request, *, cancelled):
                self.calls.append(request)
                yield PCMChunk(
                    reply_id=request.reply_id,
                    segment_id=request.segment_id,
                    provider_request_id=request.provider_request_id,
                    provider_generation=request.provider_generation,
                    sequence=1,
                    sample_rate=SAMPLE_RATE,
                    channels=1,
                    sample_width=2,
                    data=pcm_bytes(200),
                    is_final=True,
                )

        stream = make_stream(ShortProvider(chunk_ms=200), backend)
        stream.push_text_chunk("短句。")
        self.assertIsNone(stream.record.get("error"), stream.record)
        self.assertEqual(stream.record["chunks"], 1)
        self.assertEqual(stream.record["playback_state"], "COMPLETED")
        stream.finish()

    def test_cancel_path_still_discards_without_error(self) -> None:
        backend = RealTimePCMBackend()
        stream = make_stream(ScriptedChunkProvider(chunk_ms=200), backend)
        stream.cancel()
        self.assertTrue(stream.record["cancelled"])
        self.assertEqual(backend.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
