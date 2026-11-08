"""P9 待修 B：Silero **段级**结束判定 + 尾段定稿（离线红测）。

真机根因（`2026-10-01-P9-hud-and-capture-issues-handoff.md` §3）：帧级能量/粘滞判定在真实
环境噪声下静音计数被反复清零，永远等不到 ``silence_ms``，只能撞 ``max_utterance_duration``
（15–20 秒），ASR 拿到整段噪声 → 识别错乱、末尾丢字。

本驱动因此支持两个**可选**注入：

- ``segment_probe``：零参可调用，返回"底层 VAD 是否已产出一个**已结束的人声段**"
  （sherpa-onnx ``VoiceActivityDetector.empty()`` 取反）。非 None 且返回 True 时，在
  **已确认语音起始**之后作为第一顺位结束依据（仍按正常 ``silence_timeout`` 收尾提交）。
- ``segment_flush``：``release()`` 前调用一次，把缓冲尾段定稿（``flush()``），保证最后一个字
  进入已结束段。

拒绝路径（必须成立：宁可多录，绝不切用户）：
- 起始未确认时段级结束**不得**生效；
- 探测抛错 / 返回非布尔 → **不得**结束，只累加只读计数 ``segment_probe_failures``；
- 两个注入都不提供时，行为与既有帧级判定逐字一致（现有驱动测试继续覆盖）。
"""

from __future__ import annotations

import struct
import unittest

from app.audio.vad.silero import SileroVadConfig, build_silero_vad_detector
from app.contracts import AudioFrame
from app.voice_loop.auto_driver import (
    END_SOURCE_MAX_UTTERANCE,
    END_SOURCE_SEGMENT,
    END_SOURCE_SILENCE_FRAMES,
    STOP_MAX_UTTERANCE_DURATION,
    STOP_SILENCE_TIMEOUT,
    AutoCaptureDriverConfig,
    AutoCaptureVoiceDriver,
)
from app.voice_loop.boundary import VoiceLoopError
from tests.audio.test_silero_vad import (
    HAS_SHERPA,
    MODEL_PATH,
    SPEECH_WAV_PATH,
    load_speech_packets,
)
from tests.voice_loop.test_auto_driver import _StubCoordinator, boundary, build_stub_pipeline

# 让"帧级静音判定"与"最长轮次上限"都够不到，从而只可能由被测分支结束本轮。
_NEVER_MS = 600000


class _Probe:
    """段级探测器替身：记录调用次数，返回值可为任意类型（含非布尔）。"""

    def __init__(self, value: object = False) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self.value


class _BoomProbe:
    """总是抛错的探测器替身（异常正文含路径，用于确认既不被利用也不外泄）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        raise RuntimeError("C:\\secret\\mic.pcm raw audio")


def _pipeline(**config_kwargs: object):
    """构造并启动驱动，返回 ``(coordinator, driver)``。

    默认 ``min_utterance_ms=20``（一帧）：这些用例只关心段级判定本身；专门的
    最小时长用例会显式覆盖它。
    """

    values: dict[str, object] = {
        "silence_ms": _NEVER_MS,
        "speech_start_timeout_ms": 8000,
        "max_utterance_ms": _NEVER_MS,
        "min_utterance_ms": 20,
    }
    values.update(config_kwargs)
    return build_stub_pipeline(config=AutoCaptureDriverConfig(**values))  # type: ignore[arg-type]


def _started_pipeline(probe: object):
    """起始已确认的管道：3 帧环境静音 → 3 帧语音（能量判定命中）。"""

    coordinator, driver = _pipeline(segment_probe=probe)
    coordinator.feed(3, speech=False)
    driver.pump()
    coordinator.feed(3, speech=True)
    driver.pump()
    assert driver.speech_started, "测试前置：起始必须已确认"
    return coordinator, driver


class SegmentEndpointTests(unittest.TestCase):
    def test_finished_segment_ends_the_turn_as_silence_timeout(self) -> None:
        probe = _Probe(False)
        coordinator, driver = _started_pipeline(probe)
        self.assertFalse(driver.auto_released)
        probe.value = True
        coordinator.feed(1, speech=True)
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        details = driver.observation_details()
        self.assertEqual(details["end_source"], END_SOURCE_SEGMENT)
        self.assertEqual(details["segment_ends"], 1)
        self.assertEqual(details["segment_probe_failures"], 0)
        self.assertTrue(details["segment_end_available"])
        # 正常结束（不是丢弃）：release() 仍然按既有语义走 coordinator 收尾。
        result = driver.release()
        self.assertIsNotNone(result)

    def test_segment_end_before_speech_start_never_ends_the_turn(self) -> None:
        probe = _Probe(True)
        config = AutoCaptureDriverConfig(
            silence_ms=_NEVER_MS,
            speech_start_timeout_ms=_NEVER_MS,
            max_utterance_ms=_NEVER_MS,
            segment_probe=probe,
        )
        coordinator, driver = build_stub_pipeline(config=config)
        coordinator.feed(2, speech=False)
        driver.pump()
        self.assertFalse(driver.speech_started)
        self.assertFalse(driver.auto_released)
        self.assertIsNone(driver.last_stop_reason)
        self.assertEqual(driver.observation_details()["segment_ends"], 0)

    def test_probe_failure_is_counted_and_never_ends_the_turn(self) -> None:
        probe = _BoomProbe()
        coordinator, driver = _started_pipeline(probe)
        coordinator.feed(1, speech=True)
        driver.pump()
        self.assertGreaterEqual(probe.calls, 1)
        self.assertFalse(driver.auto_released)
        self.assertIsNone(driver.last_stop_reason)
        details = driver.observation_details()
        self.assertGreaterEqual(details["segment_probe_failures"], 1)
        self.assertEqual(details["segment_ends"], 0)
        self.assertNotIn("mic.pcm", repr(details))

    def test_non_boolean_probe_result_is_counted_and_never_ends(self) -> None:
        probe = _Probe("yes")
        coordinator, driver = _started_pipeline(probe)
        coordinator.feed(1, speech=True)
        driver.pump()
        self.assertFalse(driver.auto_released)
        details = driver.observation_details()
        self.assertGreaterEqual(details["segment_probe_failures"], 1)
        self.assertEqual(details["segment_ends"], 0)

    def test_segment_end_is_ignored_before_min_utterance(self) -> None:
        """太短的一轮不允许被"段结束"收尾：防止上一轮残留段/噪声瞬间结束本轮。

        真机复现（2026-10-01）：第 2 轮 `frames=2`、识别 `'。'`，却仍提交并播报。
        """

        probe = _Probe(True)
        coordinator, driver = _pipeline(segment_probe=probe, min_utterance_ms=300)
        coordinator.feed(3, speech=True)  # 起始确认
        driver.pump()
        self.assertTrue(driver.speech_started)
        self.assertFalse(driver.auto_released)  # 60ms < 300ms：不得收尾
        self.assertIsNone(driver.last_stop_reason)
        coordinator.feed(12, speech=True)  # 累计 15 帧 = 300ms
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.observation_details()["end_source"], END_SOURCE_SEGMENT)

    def test_min_utterance_does_not_disable_the_hard_cap(self) -> None:
        coordinator, driver = _pipeline(min_utterance_ms=5000, max_utterance_ms=400)
        coordinator.feed(20, speech=True)  # 400ms 硬上限
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_MAX_UTTERANCE_DURATION)

    def test_default_min_utterance_is_frozen(self) -> None:
        self.assertEqual(AutoCaptureDriverConfig().min_utterance_ms, 300)
        for bad in (0, -1, True, "300"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    AutoCaptureDriverConfig(min_utterance_ms=bad)  # type: ignore[arg-type]

    def test_frame_level_silence_still_ends_when_no_segment_probe(self) -> None:
        coordinator, driver = _pipeline(silence_ms=1200)
        coordinator.feed(3, speech=False)
        driver.pump()
        coordinator.feed(3, speech=True)
        driver.pump()
        coordinator.feed(60, speech=False)  # 60 * 20ms = 1200ms = silence_ms
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        details = driver.observation_details()
        self.assertEqual(details["end_source"], END_SOURCE_SILENCE_FRAMES)
        self.assertFalse(details["segment_end_available"])

    def test_max_utterance_still_wins_over_frame_silence(self) -> None:
        coordinator, driver = _pipeline(silence_ms=_NEVER_MS, max_utterance_ms=400)
        coordinator.feed(20, speech=True)  # 20 * 20ms = 400ms
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_MAX_UTTERANCE_DURATION)
        self.assertEqual(driver.observation_details()["end_source"], END_SOURCE_MAX_UTTERANCE)

    def test_segment_end_still_cannot_exceed_max_utterance(self) -> None:
        probe = _Probe(False)
        coordinator, driver = _pipeline(segment_probe=probe, max_utterance_ms=200)
        coordinator.feed(10, speech=True)  # 200ms = 上限
        driver.pump()
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_MAX_UTTERANCE_DURATION)
        self.assertEqual(probe.calls, 0)

    def test_defaults_have_no_segment_endpoint(self) -> None:
        config = AutoCaptureDriverConfig()
        self.assertIsNone(config.segment_probe)
        self.assertIsNone(config.segment_flush)

    def test_config_rejects_non_callable_segment_hooks(self) -> None:
        config = AutoCaptureDriverConfig()
        cases = (
            {"segment_probe": 1},
            {"segment_flush": "flush"},
            {"segment_probe": []},
            {"segment_flush": object()},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AutoCaptureDriverConfig(**kwargs)  # type: ignore[arg-type]
        self.assertIsNone(config.segment_probe)


class TailFlushTests(unittest.TestCase):
    def test_release_flushes_tail_segment_before_coordinator_release(self) -> None:
        log: list[str] = []
        coordinator, driver = build_stub_pipeline(
            config=AutoCaptureDriverConfig(segment_flush=lambda: log.append("flush"))
        )
        real_release = coordinator.release

        def release(**kwargs: object) -> object:
            log.append("release")
            return real_release(**kwargs)

        coordinator.release = release  # type: ignore[assignment]
        coordinator.feed(3, speech=True)
        driver.pump()
        driver.release()
        self.assertEqual(log, ["flush", "release"])
        # 同一轮次重复 release 是非法调用，且不得再次 flush。
        with self.assertRaises(VoiceLoopError):
            driver.release()
        self.assertEqual(log, ["flush", "release"])

    def test_flush_failure_is_counted_and_never_breaks_release(self) -> None:
        def broken_flush() -> None:
            raise RuntimeError("C:\\secret\\pcm out")

        coordinator, driver = build_stub_pipeline(
            config=AutoCaptureDriverConfig(segment_flush=broken_flush)
        )
        coordinator.feed(3, speech=True)
        driver.pump()
        result = driver.release()
        self.assertIsNotNone(result)
        self.assertGreaterEqual(driver.observation_details()["segment_probe_failures"], 1)

    def test_absent_flush_is_a_noop(self) -> None:
        coordinator, driver = build_stub_pipeline()
        coordinator.feed(3, speech=True)
        driver.pump()
        self.assertIsNotNone(driver.release())


class ObservationTimelineTests(unittest.TestCase):
    def test_observation_details_report_bounded_timeline_facts(self) -> None:
        coordinator, driver = _pipeline()
        coordinator.feed(3, speech=False)
        driver.pump()
        coordinator.feed(3, speech=True)  # 连续 3 帧命中才确认起始
        driver.pump()
        coordinator.feed(5, speech=False)
        driver.pump()
        coordinator.feed(1, speech=True)
        driver.pump()
        coordinator.feed(4, speech=False)
        driver.pump()
        details = driver.observation_details()
        self.assertEqual(details["frames"], 16)
        self.assertEqual(details["hits"], 4)
        self.assertEqual(details["max_silence_run_frames"], 5)
        self.assertEqual(details["max_silence_run_ms"], 100)
        self.assertEqual(details["spoken_silence_ms"], 80)
        self.assertEqual(details["frame_ms"], 20)
        self.assertTrue(details["speech_started"])
        self.assertEqual(details["silence_ms"], _NEVER_MS)
        self.assertIsNone(details["end_source"])
        self.assertIsNone(details["stop_reason"])

    def test_timeline_is_per_turn_and_never_carries_content(self) -> None:
        coordinator, driver = _pipeline()
        coordinator.feed(2, speech=True)
        driver.pump()
        details = driver.observation_details()
        self.assertEqual(details["frames"], 2)
        serialized = repr(details)
        self.assertNotIn("mic.pcm", serialized)
        self.assertNotIn("C:\\", serialized)
        # 新一轮（同一驱动再次 start）必须清空上一轮的台账。
        driver.release()
        self.assertTrue(driver.start(boundary(2)))
        fresh = driver.observation_details()
        self.assertEqual(fresh["frames"], 0)
        self.assertEqual(fresh["hits"], 0)
        self.assertEqual(fresh["segment_ends"], 0)
        self.assertIsNone(fresh["end_source"])


class _RealAudioCoordinator(_StubCoordinator):
    """能投递**任意真实载荷**的 stub coordinator（仓库内语音片段 / 静音窗）。"""

    def feed_payload(self, payload: bytes, *, duration_ms: int = 32) -> None:
        bus = self._bus
        assert bus is not None
        frame = AudioFrame(
            audio_session_id=bus.audio_session_id,
            provider_generation=bus.provider_generation,
            stream_id=bus.stream_id,
            event_sequence=self._next_event_sequence,
            frame_index=self._next_frame_index,
            captured_at="2026-10-01T09:00:00.000Z",
            duration_ms=duration_ms,
            device_epoch=bus.device_epoch,
            sample_rate_hz=16000,
            channel_count=1,
            sample_format="pcm_s16le",
            sample_count=len(payload) // 2,
            payload_size_bytes=len(payload),
        )
        decision = bus.publish(frame, payload)
        assert decision.value == "accepted", decision
        self._next_event_sequence += 1
        self._next_frame_index += 1


_SILENCE_WINDOW = struct.pack("<512h", *([0] * 512))


def _run_real_audio(
    *, segment_endpoint: bool, silence_ms: int, silence_limit: int
) -> tuple[AutoCaptureVoiceDriver, int]:
    """用仓库内真实语音片段跑一轮：真实 Silero 模型 + 真实 16kHz 波形，无设备无网络。"""

    detector = build_silero_vad_detector(
        config=SileroVadConfig(speech_frames_required=2, min_silence_duration=0.5)
    )
    coordinator = _RealAudioCoordinator(frame_ms=32)

    def factory(vad_factory):
        coordinator._factory = vad_factory  # noqa: SLF001 - 复用既有 stub 注入方式
        return coordinator

    config = AutoCaptureDriverConfig(
        silence_ms=silence_ms,
        speech_start_timeout_ms=_NEVER_MS,
        max_utterance_ms=_NEVER_MS,
        speech_detector=detector.observe,
        speech_detector_frames_required=1,
        segment_probe=detector.segment_pending if segment_endpoint else None,
        segment_flush=detector.flush_segment if segment_endpoint else None,
    )
    driver = AutoCaptureVoiceDriver(coordinator_factory=factory, config=config)
    assert driver.start(boundary(1)), driver.last_start_reason
    for packet in load_speech_packets():
        coordinator.feed_payload(bytes(packet.payload))
        driver.pump()
    silence_used = 0
    for _ in range(silence_limit):
        coordinator.feed_payload(_SILENCE_WINDOW)
        silence_used += 1
        driver.pump()
        if driver.auto_released:
            break
    return driver, silence_used


@unittest.skipUnless(
    MODEL_PATH.is_file() and HAS_SHERPA and SPEECH_WAV_PATH.is_file(),
    "需要 Silero 模型、sherpa_onnx 与仓库内语音片段才能做段级端点真实音频回归",
)
class RealAudioSegmentEndpointTests(unittest.TestCase):
    """真实音频回归（待修 B）：段级端点必须紧跟"说完"，而不是等到 2.5s 或最长上限。

    仓库内语音片段约 2.0 秒；喂完之后只投静音窗，观察自动结束所需的静音时长。
    """

    def test_segment_endpoint_closes_the_boundary_right_after_speech(self) -> None:
        driver, silence_used = _run_real_audio(
            segment_endpoint=True, silence_ms=_NEVER_MS, silence_limit=120
        )
        self.assertTrue(driver.auto_released)
        self.assertEqual(driver.last_stop_reason, STOP_SILENCE_TIMEOUT)
        details = driver.observation_details()
        self.assertEqual(details["end_source"], END_SOURCE_SEGMENT)
        self.assertEqual(details["segment_ends"], 1)
        self.assertEqual(details["segment_probe_failures"], 0)
        # Silero 端点宽限 0.5s：所需静音必须远小于帧级 2500ms 路径（≤40 窗 = 1.28s）。
        self.assertLessEqual(silence_used, 40)

    def test_frame_level_path_needs_much_longer_silence_on_the_same_audio(self) -> None:
        driver, silence_used = _run_real_audio(
            segment_endpoint=False, silence_ms=2500, silence_limit=120
        )
        self.assertTrue(driver.auto_released)
        details = driver.observation_details()
        self.assertEqual(details["end_source"], END_SOURCE_SILENCE_FRAMES)
        self.assertEqual(details["segment_ends"], 0)
        # 2.5s 静音 = 79 窗；这条对照证明"变快"来自段级端点，而不是别的路径。
        self.assertGreater(silence_used, 60)
        self.assertGreaterEqual(details["max_silence_run_ms"], 2500)


if __name__ == "__main__":
    unittest.main()
