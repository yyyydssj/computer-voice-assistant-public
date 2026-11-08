"""P10 批次 R4-A：回声对齐与相关性估计（``app/audio/aec/alignment.py``）的离线契约测试。

全部离线：合成"参考 + 延迟/增益/噪声后的采集"，检查估计出的延迟与相关性；
不打开任何设备、不创建线程。
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from app.audio.aec.alignment import (
    STATE_HISTORY_SHORT,
    STATE_OK,
    STATE_SILENT,
    EchoAlignment,
    estimate_echo_alignment,
)
from app.contracts.base import ContractError

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
SECONDS = 1.0


def frames_from(signal: np.ndarray, *, start_ms: int = 0) -> list[tuple[int, np.ndarray]]:
    frames: list[tuple[int, np.ndarray]] = []
    for index in range(signal.size // FRAME_SAMPLES):
        chunk = signal[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]
        frames.append((start_ms + index * FRAME_MS, chunk.astype(np.float32)))
    return frames


def speech_like(seconds: float = SECONDS, *, seed: int = 5, level: float = 0.15) -> np.ndarray:
    """类语音信号：带包络的带限噪声（有方差、非周期）。"""

    rng = np.random.default_rng(seed)
    size = int(SAMPLE_RATE * seconds)
    noise = rng.normal(0.0, level, size).astype(np.float32)
    envelope = 0.5 * (1.0 + np.sin(2.0 * np.pi * 3.0 * np.arange(size) / SAMPLE_RATE))
    return (noise * envelope).astype(np.float32)


class EchoAlignmentTests(unittest.TestCase):
    def _delayed_capture(
        self, reference: np.ndarray, *, delay_ms: int, gain: float, noise: float, seed: int = 9
    ) -> np.ndarray:
        delay = int(round(delay_ms * SAMPLE_RATE / 1000.0))
        rng = np.random.default_rng(seed)
        capture = np.zeros_like(reference)
        if delay < reference.size:
            capture[delay:] = gain * reference[: reference.size - delay]
        return (capture + rng.normal(0.0, noise, reference.size).astype(np.float32)).astype(
            np.float32
        )

    def test_known_delay_and_gain_are_recovered(self) -> None:
        reference = speech_like()
        for delay_ms in (40, 140, 300):
            with self.subTest(delay_ms=delay_ms):
                capture = self._delayed_capture(
                    reference, delay_ms=delay_ms, gain=0.3, noise=0.01
                )
                result = estimate_echo_alignment(
                    reference_frames=frames_from(reference),
                    capture_frames=frames_from(capture),
                )
                self.assertEqual(result.state, STATE_OK)
                self.assertAlmostEqual(
                    result.delay_ms, delay_ms, delta=int(1000.0 / FRAME_MS)
                )
                self.assertGreater(result.correlation, 0.5)

    def test_no_echo_gives_a_low_correlation(self) -> None:
        reference = speech_like(seed=1)
        rng = np.random.default_rng(2)
        capture = rng.normal(0.0, 0.05, reference.size).astype(np.float32)
        result = estimate_echo_alignment(
            reference_frames=frames_from(reference), capture_frames=frames_from(capture)
        )
        self.assertEqual(result.state, STATE_OK)
        self.assertLess(result.correlation, 0.3)

    def test_short_history_is_reported_not_guessed(self) -> None:
        reference = speech_like()
        capture = speech_like(seed=3)
        result = estimate_echo_alignment(
            reference_frames=frames_from(reference)[:5],  # 100 ms
            capture_frames=frames_from(capture),
        )
        self.assertEqual(result.state, STATE_HISTORY_SHORT)
        self.assertEqual(result.correlation, 0.0)

    def test_silence_is_reported_as_silent(self) -> None:
        reference = speech_like()
        capture = np.zeros_like(reference)
        result = estimate_echo_alignment(
            reference_frames=frames_from(reference), capture_frames=frames_from(capture)
        )
        self.assertEqual(result.state, STATE_SILENT)

    def test_degenerate_short_overlap_is_never_a_perfect_correlation(self) -> None:
        """真机/离线都踩过：重叠只剩几毫秒时，任何两段都"完全相关"（假的 ±1）。"""

        reference = speech_like(seed=4)
        rng = np.random.default_rng(6)
        capture = rng.normal(0.0, 0.05, reference.size).astype(np.float32)
        result = estimate_echo_alignment(
            reference_frames=frames_from(reference), capture_frames=frames_from(capture)
        )
        self.assertLess(result.correlation, 0.3)
        self.assertNotEqual(result.correlation, 1.0)

    def test_alignment_is_scale_invariant(self) -> None:
        reference = speech_like()
        quiet = self._delayed_capture(reference, delay_ms=120, gain=0.05, noise=0.002)
        loud = self._delayed_capture(reference, delay_ms=120, gain=1.0, noise=0.002)
        quiet_result = estimate_echo_alignment(
            reference_frames=frames_from(reference), capture_frames=frames_from(quiet)
        )
        loud_result = estimate_echo_alignment(
            reference_frames=frames_from(reference), capture_frames=frames_from(loud)
        )
        self.assertGreater(quiet_result.correlation, 0.5)
        self.assertGreater(loud_result.correlation, 0.5)
        self.assertAlmostEqual(quiet_result.delay_ms, loud_result.delay_ms, delta=FRAME_MS)

    def test_details_contain_codes_and_numbers_only(self) -> None:
        result = estimate_echo_alignment(
            reference_frames=frames_from(speech_like()), capture_frames=frames_from(speech_like())
        )
        details = result.to_details()
        self.assertEqual(
            set(details),
            {
                "state",
                "correlation",
                "delay_ms",
                "reference_seconds",
                "capture_seconds",
                "searched_candidates",
                "reference_rms",
                "capture_rms",
            },
        )
        self.assertNotIn("pcm", details)

    def test_invalid_arguments_are_rejected(self) -> None:
        reference = frames_from(speech_like())
        with self.assertRaises(ContractError):
            estimate_echo_alignment(
                reference_frames=reference, capture_frames=reference, sample_rate=0
            )
        with self.assertRaises(ContractError):
            estimate_echo_alignment(
                reference_frames=reference, capture_frames=reference, search_ms=-1
            )
        with self.assertRaises(ContractError):
            estimate_echo_alignment(
                reference_frames=reference, capture_frames=reference, min_seconds=0
            )

    def test_empty_frames_are_history_short(self) -> None:
        result = estimate_echo_alignment(reference_frames=[], capture_frames=[])
        self.assertEqual(result.state, STATE_HISTORY_SHORT)
        self.assertIsInstance(result, EchoAlignment)
        self.assertFalse(result.ok)


class ReferenceResamplingTests(unittest.TestCase):
    """参考源把 24 kHz 播放 PCM 重采样到 16 kHz（R4-A 的同钟前提）。"""

    def _reference(self):
        from app.audio.aec.reference_source import AecReferencePCM, SameProcessAecReference

        return SameProcessAecReference(
            pcm=AecReferencePCM(
                sample_rate=SAMPLE_RATE,
                frame_duration_ms=FRAME_MS,
                max_capacity_ms=8000,
                source_sample_rate=24_000,
            )
        )

    def _playback_tone(self, seconds: float, *, frequency: float = 800.0) -> bytes:
        size = int(24_000 * seconds)
        t = np.arange(size, dtype=np.float32) / 24_000.0
        samples = 0.3 * np.sin(2.0 * np.pi * frequency * t)
        return (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()

    def test_one_second_of_playback_becomes_one_second_of_reference(self) -> None:
        reference = self._reference()
        payload = self._playback_tone(1.0)
        chunk = 24_000 * 2 // 5  # waveOut 实际按 200 ms 大块提交
        for index in range(0, len(payload), chunk):
            reference.submit(
                payload[index : index + chunk],
                submitted_at_seconds=1.0 + index / (2 * 24_000),
            )
        total = sum(frame[1].size for frame in reference.timeline_frames())
        self.assertEqual(total, SAMPLE_RATE)
        details = reference.to_details()
        self.assertTrue(details["resampling"])
        self.assertEqual(details["source_sample_rate"], 24_000)
        self.assertEqual(details["frame_samples"], FRAME_SAMPLES)

    def test_block_size_does_not_change_the_timeline(self) -> None:
        """大块提交与逐帧提交必须得到同一条时间线（跨块连续重采样）。"""

        payload = self._playback_tone(1.0)

        def build(chunk_bytes: int):
            reference = self._reference()
            for index in range(0, len(payload), chunk_bytes):
                reference.submit(
                    payload[index : index + chunk_bytes],
                    submitted_at_seconds=1.0 + index / (2 * 24_000),
                )
            return reference

        big = build(24_000 * 2 // 5)
        small = build(FRAME_SAMPLES * 2)
        big_signal = np.concatenate([frame[1] for frame in big.timeline_frames()])
        small_signal = np.concatenate([frame[1] for frame in small.timeline_frames()])
        self.assertEqual(big_signal.size, small_signal.size)
        self.assertLess(float(np.max(np.abs(big_signal - small_signal))), 5e-3)

    def test_same_clock_skips_resampling_entirely(self) -> None:
        from app.audio.aec.reference_source import AecReferencePCM, SameProcessAecReference

        reference = SameProcessAecReference(
            pcm=AecReferencePCM(
                sample_rate=SAMPLE_RATE, frame_duration_ms=FRAME_MS, max_capacity_ms=8000
            )
        )
        payload = bytes(FRAME_SAMPLES * 2 * 3)
        self.assertEqual(reference.submit(payload, submitted_at_seconds=1.0), 3)
        self.assertFalse(reference.to_details()["resampling"])

    def test_resampling_resets_on_device_epoch_change(self) -> None:
        reference = self._reference()
        reference.submit(self._playback_tone(0.2), submitted_at_seconds=1.0)
        self.assertTrue(reference.note_device_epoch(device_epoch=2, estimated_delay_ms=100))
        self.assertEqual(reference.frames_buffered, 0)
        # 换设备后重新开始：不残留旧相位/旧样本。
        frames = reference.submit(self._playback_tone(0.2), submitted_at_seconds=2.0)
        self.assertGreaterEqual(frames, 1)

    def test_resampled_reference_drives_nlms_to_erle_above_15db(self) -> None:
        """A3 的关键证据：**重采样后的**参考喂给 NLMS，合成回声必须被真实抑制（>15 dB）。

        构造：24 kHz 播放信号 → 参考源重采样到 16 kHz 时间线；麦克风 = 该 16 kHz 信号
        延迟 140 ms、增益 0.3、叠加少量噪声。为模拟声学延迟，参考按 ``t - delay`` 提交
        （于是时间线在采集时刻正好对齐到"设备刚才播出的那一帧"）。
        """

        seconds = 1.5
        delay_ms = 140
        gain = 0.3
        playback = self._playback_tone(seconds)
        reference = self._reference()
        chunk = 24_000 * 2 // 5  # 200 ms
        for index in range(0, len(playback), chunk):
            at_seconds = 1.0 + index / (2 * 24_000)
            reference.submit(
                playback[index : index + chunk],
                submitted_at_seconds=at_seconds - delay_ms / 1000.0,
            )
        played = np.concatenate([frame[1] for frame in reference.timeline_frames()])
        delay_samples = int(round(delay_ms * SAMPLE_RATE / 1000.0))
        rng = np.random.default_rng(23)
        capture = np.zeros_like(played)
        capture[delay_samples:] = gain * played[: played.size - delay_samples]
        capture = capture + rng.normal(0.0, 0.002, played.size).astype(np.float32)

        erle_tail: list[float] = []
        for index in range(played.size // FRAME_SAMPLES):
            frame_samples = capture[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]
            payload = (np.clip(frame_samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
            result = reference.process(capture_at_ms=1000 + index * FRAME_MS, capture=payload)
            if index >= played.size // FRAME_SAMPLES - 20:  # 尾部 20 帧（已收敛）
                erle_tail.append(result.erle_db)
        convergent = [value for value in erle_tail if math.isfinite(value)]
        self.assertTrue(convergent, "尾部没有有效 ERLE")
        median = float(np.median(convergent))
        self.assertGreater(median, 15.0, f"尾部 ERLE 中位数只有 {median:.1f} dB")


if __name__ == "__main__":
    unittest.main()
