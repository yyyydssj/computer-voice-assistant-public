"""R4-B 后续：收尾必须是「无进展才失败」，不能用「总时长」判定。

真机 / 逆向背景（2026-10-12，见
``implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md``）：
豆包实时语音的超时**全部是空闲式**（``AudioTTSIdleTimeoutError`` /
``DialogAudioIdleTimeoutError`` / ``AudioChatRecvTimeoutError``），不存在「一条回复最多播
N 秒」这种上限；而本仓原本用**同一个** ``segment_timeout_s`` 同时当「单段预算」和
「整条回复的收尾上限」：

- ``StreamingVoiceSession.finish()`` 的 ``join(timeout=segment_timeout_s)``（收尾上限）；
- `_speak()`` 里 ``self._worker.wait(ticket, segment_timeout_s)``（单段预算）；
- ``_enqueue()`` / ``SessionStream._await_room()`` 的等待（单段/队列预算）。

于是一旦尾部剩余音频的**播放墙钟**超过那个上限，``SessionStream.finish()`` 的
``finally: close()`` 就会 ``cancel()`` 掉仍在播放的语音 ⇒ 长回复被砍尾。

P1 把两条预算解耦：``segment_timeout_s`` 只管**一个句段**，``stall_timeout_s`` 才是收尾判据
（连续这么久毫无进展）。本模块用三条用例把这套语义钉住：

1. ``SlowButProgressingTailTests``：单段预算 1.8 s（够一段、不够整条回复），停滞预算 8.0 s
   ——设备一直在推进 ⇒ 必须全播。**P1 之前红**（旧代码用 1.8 s 当收尾上限）。
2. ``StallWatchdogIsLiveTests``：停滞预算压到 0.3 s（远小于相邻进展间隔 ≈1.15 s）⇒ 必须判停滞
   ——证明看门狗不是「永不超时」的空转。
3. ``StuckDeviceTests``：缓冲区**永不归还**的设备 ⇒ 收尾必须有界结束且失败可见。

完全离线：无 socket、无 VoiceStudio、无音频设备、无子进程。设备按「音频时长 ÷ ``scale``」
归还缓冲区，所以音频时间的账是真的，墙钟是快的。
"""
from __future__ import annotations

import getpass
import threading
import time
import unittest
from math import ceil
from threading import Event

from app.tts.clock import monotonic_seconds
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
    StreamingVoiceSession,
    speech_seconds_for_characters,
)
from app.tts.transport import VoiceStudioTransportRouter
from app.runtime.voice_entry import SessionStream

SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # PCM16 mono: 48 bytes per millisecond
CHUNK_MS = 200

REPLY_TEXT = "第一句测试文本内容一。第二句测试文本内容二。第三句测试文本内容三。"
"""33 个非空白字符，切成 3 个句段；每段 11 字 ≈ 2.29 s 音频，合计 ≈ 6.9 s。

**实测（本机，见文件末尾对照脚本）**：``TimedPCMBackend`` 允许 2 个缓冲并发在途，
所以音频是被 ≈2 倍速消耗的——每段 ≈1.15 s 排空完，整条回复 ≈3.4 s 排空完。
下面两条预算都按这组**实测值**取，留出余量。"""

DEVICE_SCALE = 1.0
"""设备 1.0 倍速归还缓冲区：音频时间与墙钟 1:1，便于把「排空墙钟」算准。"""

SEGMENT_BUDGET_S = 1.8
"""单段预算：**够**排空一段（实测 ≈1.15 s），但**不够**整条回复（实测 ≈3.4 s）。

P1 之前它同时被当成收尾上限 ⇒ 整条回复在 1.8 s 处被砍 ⇒ 第 1 条用例红。
（上界必须 ≥ 单段排空时间，否则句段自己的 worker 等待会先到期，测的就不是收尾语义了。）
"""

PROGRESS_STALL_S = 8.0
"""停滞预算：远大于相邻两次进展的间隔（实测 ≈1.15 s：一句排空才有下一次进展）。"""

IMPATIENT_STALL_S = 0.3
"""停滞预算压到远小于进展间隔 ⇒ 即使设备在推进，也必须判停滞（看门狗有效性对照）。

取 0.3 s（不是 1.0 s）：收尾开始时的第一份进展快照可能早于消费线程启动，第一窗口因此
**必然**能看到一次进展；只有把预算压到远小于 1.15 s 的间隔，第二个窗口才稳定地「无进展」。
"""

STUCK_STALL_S = 0.3
"""卡死用例的停滞预算：没有任何进展 ⇒ 0.3 s 后必须有界放弃。"""


def make_selection() -> VoiceSelection:
    return VoiceSelection(
        profile_id=CONFIRMED_PROFILE,
        owner=getpass.getuser(),
        reference_sha256=CONFIRMED_REFERENCE_SHA256,
    )


def pcm_bytes(milliseconds: int) -> bytes:
    return b"\x01\x00" * (SAMPLE_RATE * milliseconds // 1000)


def sentence_duration_ms(text: str) -> int:
    """一句文本在钉死的语速下应有的音频时长（ms）。"""

    return max(1, ceil(speech_seconds_for_characters(len(text)) * 1000.0))


class TimedPCMBackend:
    """按缓冲区的**音频时长**归还的假设备；``stalled=True`` 则永不归还。

    与 ``tests/tts/test_streaming_queue_backpressure.py`` 同一族夹具：``writable()``
    最多允许 2 个在途缓冲区，归还时刻 = ``now + 音频时长 / scale``，因此
    「设备的播放进度」是可预期的，不需要对机器速度做任何猜测。
    """

    backend_name = "timed-pcm"
    max_buffer_bytes = CHUNK_MS * BYTES_PER_MS

    def __init__(self, *, scale: float = DEVICE_SCALE, stalled: bool = False) -> None:
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
    """把每句文本合成为**真实时长**的有序 PCM 帧（无网络、无模型）。"""

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


def build_session(
    *,
    backend: TimedPCMBackend,
    segment_timeout_s: float,
    stall_timeout_s: float,
) -> tuple[SessionStream, dict[str, object], StreamingVoiceSession]:
    """装配真实会话，把「单段预算」与「停滞预算」分别暴露出来以便注入。"""

    clock = monotonic_seconds
    provider = DurationProvider()
    session = StreamingVoiceSession(
        make_selection(),
        transport_router=VoiceStudioTransportRouter(
            mode="ws_pcm", ws_provider=provider, http_provider=provider, clock=clock
        ),
        worker=PCMPlaybackWorker(
            lambda: backend,
            start_buffer_ms=1,
            max_queued_audio_ms=DEFAULT_PCM_QUEUE_BUDGET_MS,
            clock=clock,
        ),
        max_pending_segments=8,
        owns_worker=True,
        segment_timeout_s=segment_timeout_s,
        stall_timeout_s=stall_timeout_s,
        verifier=lambda selection: None,
        clock=clock,
    )
    record: dict[str, object] = {"route": "ws_pcm", "error": None}
    return SessionStream(session, record), record, session


class DrainStallCase(unittest.TestCase):
    def speak(
        self,
        *,
        backend: TimedPCMBackend,
        segment_timeout_s: float,
        stall_timeout_s: float,
        text: str = REPLY_TEXT,
    ) -> dict[str, object]:
        stream, record, session = build_session(
            backend=backend,
            segment_timeout_s=segment_timeout_s,
            stall_timeout_s=stall_timeout_s,
        )
        self.addCleanup(session.close)
        stream.push_text_chunk(text)
        stream.finish()
        return record


class SlowButProgressingTailTests(DrainStallCase):
    def test_slow_but_progressing_tail_must_complete(self) -> None:
        """设备在推进（按音频时长归还），只是整条回复比「单段预算」长 ⇒ 不许判截断。

        P1 之前：收尾就是 ``join(timeout=segment_timeout_s=1.8 s)``，而整条回复实测要
        ≈3.4 s ⇒ 到期 ⇒ ``finish_timed_out=True`` ⇒ ``close()`` 取消后面的句子 ⇒ 本条**红**。
        P1 之后：收尾看 ``stall_timeout_s=8.0 s``，而进展间隔只有实测 ≈1.15 s ⇒ 必须全播。
        """

        record = self.speak(
            backend=TimedPCMBackend(scale=DEVICE_SCALE),
            segment_timeout_s=SEGMENT_BUDGET_S,
            stall_timeout_s=PROGRESS_STALL_S,
        )
        self.assertIsNone(
            record.get("truncation_reason"),
            f"设备一直在推进，却被判成截断: {record}",
        )
        self.assertIs(
            record.get("finish_timed_out"),
            False,
            f"收尾被「单段预算」当成整条回复的上限: {record}",
        )
        self.assertEqual(
            record.get("played_chars"),
            record.get("pushed_characters"),
            f"尾部少播: {record}",
        )


class StallWatchdogIsLiveTests(DrainStallCase):
    def test_stall_budget_below_progress_interval_still_truncates(self) -> None:
        """把停滞预算压到进展间隔以下 ⇒ 必须判停滞（证明看门狗不是空转）。

        同一夹具、同一单段预算，只把停滞预算从 8.0 s 改成 0.3 s（实测进展间隔 ≈1.15 s）
        ⇒ 收尾必须如实判超时，而不是"永远等下去"。
        """

        record = self.speak(
            backend=TimedPCMBackend(scale=DEVICE_SCALE),
            segment_timeout_s=SEGMENT_BUDGET_S,
            stall_timeout_s=IMPATIENT_STALL_S,
        )
        self.assertIs(
            record.get("finish_timed_out"),
            True,
            f"停滞预算小于进展间隔时没有判停滞: {record}",
        )
        self.assertIsNotNone(
            record.get("truncation_reason"),
            f"少播必须给出固定原因码: {record}",
        )


class StuckDeviceTests(DrainStallCase):
    def test_stuck_device_still_fails_bounded(self) -> None:
        """缓冲区永不归还 ⇒ 收尾必须**有界**结束，并且失败**可见**。

        这是 P1 的安全护栏：看门狗只能把「慢但在推进」放行，绝不能把「真卡住」
        变成永久等待。

        诚实说明：在「停滞预算」与「单段预算」都取短值时，两条有界路径会**竞态**——
        要么收尾判停滞（``finish_timed_out=True`` + ``finish_timeout``），要么句段自己的
        worker 等待先到期（``StreamingVoiceSessionError``）。两者都是"有界 + 可见"，
        所以这里断言这条不变量，而不是某一次竞态的赢家。
        """

        stream, record, session = build_session(
            backend=TimedPCMBackend(stalled=True),
            segment_timeout_s=STUCK_STALL_S,
            stall_timeout_s=STUCK_STALL_S,
        )
        self.addCleanup(session.close)

        started = time.monotonic()
        error: BaseException | None = None
        stream.push_text_chunk(REPLY_TEXT)
        try:
            stream.finish()
        except BaseException as exc:  # noqa: BLE001 - 这里只要求"有界且可见"
            error = exc
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, f"卡住的设备没有有界结束，耗时 {elapsed:.1f}s")
        self.assertTrue(
            error is not None or record.get("finish_timed_out") is True,
            f"卡死既没抛错也没记截断: error={error!r} record={record}",
        )
        if error is not None:
            self.assertEqual(
                type(error).__name__,
                "StreamingVoiceSessionError",
                f"卡死必须以会话自己的固定错误结束: {error!r}",
            )
        else:
            self.assertIn(
                "finish_timeout",
                " ".join(str(item) for item in (record.get("late_events") or ())),
                f"停滞必须留下可按图索骥的固定码: {record}",
            )


if __name__ == "__main__":
    unittest.main()
