"""P10 批次 R2b-1：AEC 参考帧时间线与 NLMS 回声消除的离线测试。

覆盖：参考窗口状态（ok/history_short/future/too_old）、延迟对齐、设备变化与延迟下一轮生效、
缓冲有界性；NLMS 合成回声的收敛（累计 ERLE ≥ 20 dB）、双讲回退直通、门限回退、无参考直通，
以及"绝不伪称已消除回声"的硬不变量。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import numpy as np

from app.contracts.base import ContractError
from app.audio.aec.nlms import (
    AecProcessingChain,
    NlmsConfig,
    NlmsEchoCanceller,
    float_to_pcm16,
    pcm16_to_float,
)
from app.audio.aec.reference import (
    REFERENCE_FUTURE,
    REFERENCE_HISTORY_SHORT,
    REFERENCE_OK,
    REFERENCE_TOO_OLD,
    AecReferenceConfig,
    AecReferenceError,
    AecReferenceTimeline,
)

FRAME_MS = 20
SR = 16000
FRAME_SAMPLES = SR * FRAME_MS // 1000


def frame_bytes(values) -> bytes:
    return float_to_pcm16(np.asarray(values, dtype=np.float32))


def rms(samples: np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float64)
    return float(np.sqrt(np.mean(array * array)))


def erle_db(reference: np.ndarray, error: np.ndarray) -> float:
    ref = float(np.sum(np.asarray(reference, dtype=np.float64) ** 2))
    err = float(np.sum(np.asarray(error, dtype=np.float64) ** 2))
    if err <= 0.0:
        return float("inf")
    if ref <= 0.0:
        return 0.0
    return 10.0 * float(np.log10(ref / err))


class AecReferenceConfigTests(unittest.TestCase):
    def test_defaults_are_usable(self) -> None:
        config = AecReferenceConfig()
        self.assertEqual(config.sample_rate, 16000)
        self.assertEqual(config.frame_samples, 320)
        self.assertGreaterEqual(config.max_delay_ms, 100)

    def test_zero_sample_rate_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AecReferenceConfig(sample_rate=0)

    def test_delay_may_not_exceed_capacity(self) -> None:
        with self.assertRaises(ContractError):
            AecReferenceConfig(max_delay_ms=3000, max_capacity_ms=1000)

    def test_capacity_frames_is_bounded(self) -> None:
        config = AecReferenceConfig(max_capacity_ms=2000, frame_duration_ms=20)
        self.assertEqual(config.capacity_frames, 100)


class AecReferenceTimelineTests(unittest.TestCase):
    def _filled(self, frames: int = 5, *, delay_ms: int = 0, start_ms: int = 0):
        timeline = AecReferenceTimeline()
        values = (np.arange(FRAME_SAMPLES, dtype=np.float32) % 100) / 100.0 - 0.5
        for index in range(frames):
            timeline.push(start_ms + index * FRAME_MS, frame_bytes(values))
        return timeline

    def test_window_is_ok_after_history_is_deep_enough(self) -> None:
        timeline = self._filled()
        window = timeline.window(0)
        self.assertEqual(window.state, REFERENCE_OK)
        self.assertTrue(window.ok)
        self.assertIsNotNone(window.pcm)
        self.assertEqual(int(window.pcm.size), FRAME_SAMPLES)

    def test_missing_history_reports_history_short(self) -> None:
        timeline = AecReferenceTimeline()
        window = timeline.window(0)
        self.assertEqual(window.state, REFERENCE_HISTORY_SHORT)
        self.assertFalse(window.ok)

    def test_future_capture_time_reports_future(self) -> None:
        timeline = self._filled(frames=2)
        window = timeline.window(1000)
        self.assertEqual(window.state, REFERENCE_FUTURE)

    def test_too_old_capture_time_reports_too_old(self) -> None:
        timeline = self._filled(frames=2, start_ms=10_000)
        window = timeline.window(5_000)
        self.assertEqual(window.state, REFERENCE_TOO_OLD)

    def test_device_delay_offsets_the_lookup(self) -> None:
        timeline = AecReferenceTimeline(config=AecReferenceConfig(max_delay_ms=200))
        timeline.push(0, frame_bytes(np.ones(FRAME_SAMPLES, dtype=np.float32) * 0.5))
        self.assertEqual(timeline.window(0).state, REFERENCE_OK)
        # 延迟 40 ms 后，t=0 的采集对应 t=-40 的参考：历史不足。
        timeline.note_device_change(device_epoch=2, estimated_delay_ms=40)
        timeline.push(FRAME_MS, frame_bytes(np.ones(FRAME_SAMPLES, dtype=np.float32) * 0.5))
        window = timeline.window(FRAME_MS * 3)
        self.assertEqual(window.delay_ms, 40)
        self.assertEqual(window.reference_at_ms, FRAME_MS * 3 - 40)

    def test_pending_delay_applies_from_the_next_push(self) -> None:
        timeline = AecReferenceTimeline()
        timeline.push(0, frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        self.assertEqual(timeline.device_delay_ms, 0)
        timeline.note_device_change(device_epoch=2, estimated_delay_ms=60)
        self.assertTrue(timeline.pending_delay)
        timeline.push(FRAME_MS, frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        self.assertFalse(timeline.pending_delay)
        self.assertEqual(timeline.device_delay_ms, 60)

    def test_delay_above_max_is_rejected(self) -> None:
        timeline = AecReferenceTimeline()
        with self.assertRaises(ContractError):
            timeline.note_device_change(device_epoch=2, estimated_delay_ms=5000)

    def test_non_monotonic_push_is_rejected(self) -> None:
        timeline = AecReferenceTimeline()
        timeline.push(40, frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        with self.assertRaises(ContractError):
            timeline.push(20, frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))

    def test_odd_pcm_length_is_rejected(self) -> None:
        timeline = AecReferenceTimeline()
        with self.assertRaises(AecReferenceError):
            timeline.push(0, b"\x01\x02\x03")

    def test_wrong_frame_length_is_rejected(self) -> None:
        timeline = AecReferenceTimeline()
        with self.assertRaises(AecReferenceError):
            timeline.push(0, frame_bytes(np.zeros(FRAME_SAMPLES - 1, dtype=np.float32)))

    def test_buffer_is_bounded_by_capacity(self) -> None:
        timeline = AecReferenceTimeline(
            config=AecReferenceConfig(max_capacity_ms=100, max_delay_ms=100)
        )
        for index in range(20):
            timeline.push(index * FRAME_MS, frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        self.assertEqual(timeline.frames_buffered, 5)

    def test_clear_resets_the_timeline(self) -> None:
        timeline = self._filled()
        timeline.clear()
        self.assertEqual(timeline.frames_buffered, 0)
        self.assertEqual(timeline.window(0).state, REFERENCE_HISTORY_SHORT)

    def test_window_details_are_numbers_only(self) -> None:
        details = self._filled().window(0).to_details()
        self.assertEqual(details["state"], REFERENCE_OK)
        for name in ("pcm", "payload", "audio"):
            self.assertNotIn(name, details)


class NlmsConfigTests(unittest.TestCase):
    def test_defaults_cover_a_reasonable_tail(self) -> None:
        config = NlmsConfig()
        self.assertEqual(config.filter_samples, 2560)  # 160 ms @16k
        self.assertGreater(config.erle_gate_db, 0.0)

    def test_zero_filter_length_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            NlmsConfig(filter_length_ms=0)

    def test_step_size_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            NlmsConfig(step_size=2.5)

    def test_negative_regularization_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            NlmsConfig(regularization=-1.0)


class NlmsEchoCancellerTests(unittest.TestCase):
    """合成回声：``echo = 0.5*ref[n-3] + 0.3*ref[n-11]``（因果、含延迟）。"""

    ECHO_TAPS = ((3, 0.5), (11, 0.3))

    def _run(self, *, blocks: int = 200, mic_noise: float = 0.0, double_talk_frame=None):
        rng = np.random.default_rng(20261002)
        ref_history = [0.0] * 64
        canceller = NlmsEchoCanceller(
            config=NlmsConfig(filter_length_ms=80, erle_gate_db=0.5, step_size=0.6)
        )
        refs: list[np.ndarray] = []
        echoes: list[np.ndarray] = []
        results = []
        for block in range(blocks):
            ref = (rng.standard_normal(FRAME_SAMPLES) * 0.3).astype(np.float32)
            refs.append(ref)
            ref_history.extend(ref.tolist())
            echo = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            for delay, gain in self.ECHO_TAPS:
                for index in range(FRAME_SAMPLES):
                    source = len(ref_history) - FRAME_SAMPLES + index - delay
                    echo[index] += gain * ref_history[source]
            echoes.append(echo)
            mic = echo.copy()
            if mic_noise:
                mic = mic + (rng.standard_normal(FRAME_SAMPLES) * mic_noise).astype(np.float32)
            double_talk = double_talk_frame == block
            results.append(
                canceller.cancel(
                    reference=frame_bytes(ref), capture=frame_bytes(mic), double_talk=double_talk
                )
            )
        return canceller, np.concatenate(refs), np.concatenate(echoes), results

    def test_synthetic_echo_is_strongly_suppressed(self) -> None:
        _, _, _, results = self._run(blocks=300)
        # 前若干块用于收敛，用后 100 块评估稳定抑制量。
        tail_erle = [result.erle_db for result in results[-100:]]
        self.assertGreater(float(np.median(tail_erle)), 20.0)

    def test_cumulative_erle_reflects_the_whole_run(self) -> None:
        canceller, _, _, results = self._run(blocks=300)
        self.assertGreater(canceller.cumulative_erle_db, 20.0)
        self.assertGreater(results[-1].cumulative_erle_db, 20.0)

    def test_echo_residual_rms_drops_against_echo_rms(self) -> None:
        _, _, echo, results = self._run(blocks=300)
        tail_output = np.concatenate([result.output for result in results[-100:]])
        tail_echo = echo[-100 * FRAME_SAMPLES :]
        self.assertGreater(erle_db(tail_echo, tail_output), 15.0)

    def test_cleaned_output_is_not_silence(self) -> None:
        _, _, _, results = self._run(blocks=50)
        self.assertGreater(rms(results[-1].output), 1e-4)

    def test_double_talk_never_attacks_near_end_speech(self) -> None:
        _, _, _, results = self._run(blocks=40, double_talk_frame=20)
        target = results[20]
        self.assertTrue(target.double_talk)
        self.assertFalse(target.applied)
        self.assertEqual(target.fallback_reason, "double_talk_detected")

    def test_double_talk_flag_must_be_boolean(self) -> None:
        canceller = NlmsEchoCanceller()
        reference = frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32))
        with self.assertRaises(ContractError):
            canceller.cancel(reference=reference, capture=reference, double_talk="yes")  # type: ignore[arg-type]

    def test_silence_input_falls_back_below_gate(self) -> None:
        canceller = NlmsEchoCanceller(config=NlmsConfig(erle_gate_db=20.0))
        silence = frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32))
        result = canceller.cancel(reference=silence, capture=silence)
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, "erle_below_gate")

    def test_wrong_frame_length_is_rejected(self) -> None:
        canceller = NlmsEchoCanceller()
        with self.assertRaises(AecReferenceError):
            canceller.cancel(reference=b"\x00\x00", capture=b"\x00\x00")

    def test_reset_clears_adaptive_state(self) -> None:
        canceller, _, _, _ = self._run(blocks=60)
        self.assertGreater(canceller.frames_processed, 0)
        canceller.reset()
        self.assertEqual(canceller.frames_processed, 0)
        self.assertEqual(float(np.abs(canceller.weights).sum()), 0.0)

    def test_weights_grow_while_adapting(self) -> None:
        canceller, _, _, _ = self._run(blocks=80)
        self.assertGreater(float(np.abs(canceller.weights).sum()), 0.1)

    def test_result_details_are_numbers_only(self) -> None:
        _, _, _, results = self._run(blocks=10)
        details = results[-1].to_details()
        self.assertIn("erle_db", details)
        for name in ("pcm", "reference", "capture", "payload"):
            self.assertNotIn(name, details)


class AecProcessingChainTests(unittest.TestCase):
    def _chain(self):
        chain = AecProcessingChain(
            reference_config=AecReferenceConfig(max_delay_ms=200),
            nlms_config=NlmsConfig(filter_length_ms=80, erle_gate_db=0.5),
        )
        return chain

    def test_missing_reference_falls_through_with_reason(self) -> None:
        chain = self._chain()
        mic = frame_bytes(np.ones(FRAME_SAMPLES, dtype=np.float32) * 0.1)
        frame = chain.process(capture_at_ms=0, capture=mic)
        self.assertFalse(frame.applied)
        self.assertEqual(frame.reference_state, REFERENCE_HISTORY_SHORT)
        self.assertEqual(frame.fallback_reason, "reference_history_short")
        np.testing.assert_allclose(frame.output, pcm16_to_float(mic), rtol=0, atol=0)

    def test_future_reference_falls_through_with_reason(self) -> None:
        chain = self._chain()
        chain.push_playback(at_ms=0, pcm=frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        frame = chain.process(capture_at_ms=5000, capture=frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        self.assertFalse(frame.applied)
        self.assertEqual(frame.reference_state, REFERENCE_FUTURE)

    def test_chain_suppresses_synthetic_echo_end_to_end(self) -> None:
        chain = self._chain()
        rng = np.random.default_rng(7)
        refs: list[np.ndarray] = []
        echoes: list[np.ndarray] = []
        frames = []
        history = [0.0] * 64
        for block in range(300):
            ref = (rng.standard_normal(FRAME_SAMPLES) * 0.3).astype(np.float32)
            refs.append(ref)
            history.extend(ref.tolist())
            echo = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            for index in range(FRAME_SAMPLES):
                echo[index] = 0.5 * history[len(history) - FRAME_SAMPLES + index - 3]
            echoes.append(echo)
            chain.push_playback(at_ms=block * FRAME_MS, pcm=frame_bytes(ref))
            frames.append(
                chain.process(capture_at_ms=block * FRAME_MS, capture=frame_bytes(echo))
            )
        applied_ratio = sum(1 for frame in frames if frame.applied) / len(frames)
        tail = frames[-100:]
        tail_output = np.concatenate([frame.output for frame in tail])
        tail_echo = np.concatenate(echoes[-100:])
        self.assertGreater(applied_ratio, 0.3)
        self.assertGreater(erle_db(tail_echo, tail_output), 15.0)

    def test_chain_reports_details_without_audio(self) -> None:
        chain = self._chain()
        chain.push_playback(at_ms=0, pcm=frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32)))
        details = chain.process(
            capture_at_ms=0, capture=frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32))
        ).to_details()
        for name in ("pcm", "audio", "output"):
            self.assertNotIn(name, details)

    def test_chain_has_no_authorization_surface(self) -> None:
        chain = self._chain()
        details = chain.process(
            capture_at_ms=0, capture=frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32))
        ).to_details()
        for name in ("authorization", "confirmation", "plan", "tool"):
            self.assertNotIn(name, details)


class AecModuleBoundaryTests(unittest.TestCase):
    def _roots(self, name: str) -> set[str]:
        source = Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / name
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        return imported

    def test_reference_module_has_no_device_or_network_imports(self) -> None:
        banned = {"socket", "subprocess", "threading", "ctypes", "os", "shutil", "wave", "sounddevice", "pyaudiowpatch", "requests", "urllib", "http"}
        for name in ("reference.py", "nlms.py"):
            self.assertFalse(self._roots(name) & banned, name)

    def test_reference_module_never_imports_safety_chain(self) -> None:
        for name in ("reference.py", "nlms.py"):
            source = Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / name
            tree = ast.parse(source.read_text(encoding="utf-8"))
            modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
            banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
            offenders = {module for module in modules if ".".join(module.split(".")[:2]) in banned}
            self.assertEqual(offenders, set(), name)


if __name__ == "__main__":
    unittest.main()
