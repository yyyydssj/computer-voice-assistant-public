"""P10 批次 R2b-2（方案 A）：同进程参考源与 waveOut 只读旁路的离线测试。

覆盖：格式契约校验、提交帧校验与时间戳钳制、epoch 变化重校准、窗口状态、
合成回声的端到端抑制，以及"旁路钩子绝不改变播放路径"的硬约束
（异常/非 callable 都被安全处理，成功提交才回调，字节原样传递）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import numpy as np

from app.contracts.base import ContractError
from app.audio.aec.nlms import NlmsConfig, float_to_pcm16
from app.audio.aec.reference import (
    REFERENCE_HISTORY_SHORT,
    REFERENCE_OK,
    AecReferenceError,
)
from app.audio.aec.reference_source import (
    AecReferencePCM,
    AecReferenceSourceError,
    SameProcessAecReference,
)

RATE = 48_000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000  # 960


def frame_bytes(values) -> bytes:
    return float_to_pcm16(np.asarray(values, dtype=np.float32))


def quiet_frame() -> bytes:
    return frame_bytes(np.zeros(FRAME_SAMPLES, dtype=np.float32))


class AecReferencePCMTests(unittest.TestCase):
    def test_defaults_match_waveout_pcm_contract(self) -> None:
        pcm = AecReferencePCM()
        self.assertEqual(pcm.sample_rate, 48_000)
        self.assertEqual(pcm.frame_samples, 960)

    def test_zero_sample_rate_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AecReferencePCM(sample_rate=0)

    def test_zero_frame_duration_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AecReferencePCM(frame_duration_ms=0)

    def test_to_config_keeps_rate_and_duration(self) -> None:
        config = AecReferencePCM(sample_rate=16_000, frame_duration_ms=20).to_config()
        self.assertEqual(config.sample_rate, 16_000)
        self.assertEqual(config.frame_duration_ms, 20)


class SameProcessReferenceConfigTests(unittest.TestCase):
    def test_mismatched_nlms_rate_is_rejected(self) -> None:
        with self.assertRaises(AecReferenceSourceError):
            SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=16_000))

    def test_mismatched_frame_duration_is_rejected(self) -> None:
        with self.assertRaises(AecReferenceSourceError):
            SameProcessAecReference(nlms_config=NlmsConfig(frame_duration_ms=10))

    def test_matching_config_is_accepted(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        self.assertEqual(source.pcm.sample_rate, RATE)
        self.assertEqual(source.frames_submitted, 0)


class SameProcessReferenceSubmitTests(unittest.TestCase):
    def test_empty_frame_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(AecReferenceSourceError):
            source.submit(b"", submitted_at_seconds=0.0)

    def test_odd_length_chunk_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(AecReferenceSourceError):
            source.submit(b"\x00" * 3, submitted_at_seconds=0.0)

    def test_short_chunk_is_buffered_until_a_full_frame_arrives(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        half = FRAME_SAMPLES // 2
        source.submit(frame_bytes(np.zeros(half, dtype=np.float32)), submitted_at_seconds=0.0)
        self.assertEqual(source.frames_submitted, 0)
        self.assertGreater(source.pending_bytes, 0)
        source.submit(frame_bytes(np.zeros(half, dtype=np.float32)), submitted_at_seconds=0.02)
        self.assertEqual(source.frames_submitted, 1)
        self.assertEqual(source.pending_bytes, 0)

    def test_large_chunk_is_split_into_frames(self) -> None:
        """waveOut 默认提交 200 ms 大块（9600 字节）：必须被切成 10 个 20 ms 帧。"""

        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        chunk = frame_bytes(np.zeros(FRAME_SAMPLES * 10, dtype=np.float32))
        source.submit(chunk, submitted_at_seconds=1.0)
        self.assertEqual(source.chunks_submitted, 1)
        self.assertEqual(source.frames_submitted, 10)
        self.assertEqual(source.frames_buffered, 10)

    def test_non_bytes_frame_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(AecReferenceSourceError):
            source.submit("not bytes", submitted_at_seconds=0.0)  # type: ignore[arg-type]

    def test_negative_timestamp_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(AecReferenceSourceError):
            source.submit(quiet_frame(), submitted_at_seconds=-1.0)

    def test_nan_timestamp_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(AecReferenceSourceError):
            source.submit(quiet_frame(), submitted_at_seconds=float("nan"))

    def test_submitting_frames_fills_the_buffer(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        for index in range(5):
            source.submit(quiet_frame(), submitted_at_seconds=index * 0.02)
        self.assertEqual(source.frames_submitted, 5)
        self.assertEqual(source.frames_buffered, 5)

    def test_same_millisecond_submissions_stay_monotonic(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        # 三帧同一时间戳：时间线要求严格单调，第 2/3 帧应被钳到 +1 ms。
        for _ in range(3):
            source.submit(quiet_frame(), submitted_at_seconds=0.0)
        window = source.window(0)
        self.assertEqual(source.frames_buffered, 3)
        self.assertIn(window.state, {REFERENCE_OK, REFERENCE_HISTORY_SHORT})

    def test_details_are_numbers_only(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        details = source.to_details()
        for name in ("pcm", "payload", "audio", "samples"):
            self.assertNotIn(name, details)
        self.assertEqual(details["sample_rate"], RATE)
        self.assertIn("last_observer_error", details)


class SameProcessReferenceWindowTests(unittest.TestCase):
    def test_missing_history_reports_history_short(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        self.assertEqual(source.window(0).state, REFERENCE_HISTORY_SHORT)

    def test_window_is_ok_with_enough_history(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        for index in range(5):
            source.submit(quiet_frame(), submitted_at_seconds=index * 0.02)
        window = source.window(0)
        self.assertEqual(window.state, REFERENCE_OK)
        self.assertIsNotNone(window.pcm)
        self.assertEqual(int(window.pcm.size), FRAME_SAMPLES)


class SameProcessReferenceEpochTests(unittest.TestCase):
    def test_same_epoch_is_a_noop(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE), device_epoch=3)
        self.assertFalse(source.note_device_epoch(device_epoch=3))
        self.assertEqual(source.resets, 0)

    def test_new_epoch_clears_history_and_resets_adaptive_state(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        for index in range(5):
            source.submit(quiet_frame(), submitted_at_seconds=index * 0.02)
        self.assertTrue(source.note_device_epoch(device_epoch=2, estimated_delay_ms=40))
        self.assertEqual(source.device_epoch, 2)
        self.assertEqual(source.frames_buffered, 0)
        self.assertEqual(source.canceller.frames_processed, 0)
        self.assertEqual(source.resets, 1)

    def test_invalid_epoch_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(ContractError):
            source.note_device_epoch(device_epoch=0)


class SameProcessReferenceProcessTests(unittest.TestCase):
    """合成回声：**连续流的真 FIR 模型**（``echo[n] = gain * ref[n - delay]``）。

    早期版本用"逐帧独立的随机帧"伪造回声，那没有真实的样本级延迟，与消除器跨帧累积的
    regressor 不一致，会被误判成"消除失败"；改成本模型后测得真实抑制量。
    """

    GAIN = 0.3
    ECHO_DELAY_SAMPLES = 2
    ECHO_DELAY_FRAMES = 2  # delay < 一帧，因此补偿 2 帧即覆盖

    def _run(self, *, frames: int = 60, seed: int = 4242, device_delay_ms: int | None = None):
        rng = np.random.default_rng(seed)
        source = SameProcessAecReference(
            nlms_config=NlmsConfig(sample_rate=RATE, filter_length_ms=160, erle_gate_db=1.0)
        )
        delay_ms = (
            self.ECHO_DELAY_FRAMES * FRAME_MS if device_delay_ms is None else device_delay_ms
        )
        if delay_ms:
            source.note_device_epoch(device_epoch=2, estimated_delay_ms=delay_ms)
        reference = (rng.standard_normal(FRAME_SAMPLES * frames) * 0.3).astype(np.float32)
        echo = np.concatenate(
            (np.zeros(self.ECHO_DELAY_SAMPLES, dtype=np.float32), reference[: -self.ECHO_DELAY_SAMPLES] * self.GAIN)
        ).astype(np.float32)

        mic_frames: list[np.ndarray] = []
        outputs: list[np.ndarray] = []
        applied = 0
        lead = delay_ms // FRAME_MS
        for index in range(frames):
            ref_frame = reference[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]
            source.submit(frame_bytes(ref_frame), submitted_at_seconds=index * 0.02)
            mic_index = index - lead
            if mic_index < 0:
                continue
            mic_frame = echo[mic_index * FRAME_SAMPLES : (mic_index + 1) * FRAME_SAMPLES]
            mic_frames.append(mic_frame)
            result = source.process(
                capture_at_ms=index * FRAME_MS,
                capture=frame_bytes(mic_frame),
            )
            outputs.append(result.output)
            if result.applied:
                applied += 1
        return source, reference, mic_frames, outputs, applied

    def test_synthetic_echo_is_suppressed_end_to_end(self) -> None:
        _, _, mic_frames, outputs, _ = self._run()
        tail_mic = np.concatenate(mic_frames[-20:])
        tail_out = np.concatenate(outputs[-20:])
        mic_energy = float(np.dot(tail_mic.astype(np.float64), tail_mic.astype(np.float64)))
        residual_energy = float(np.dot(tail_out.astype(np.float64), tail_out.astype(np.float64)))
        self.assertGreater(residual_energy, 0.0)
        erle = 10.0 * np.log10(mic_energy / residual_energy)
        self.assertGreater(erle, 10.0)

    def test_cumulative_erle_reflects_the_whole_run(self) -> None:
        source, _, _, _, _ = self._run()
        self.assertGreater(source.canceller.cumulative_erle_db, 10.0)

    def test_compensated_delay_tracks_the_echo_path(self) -> None:
        source, _, _, _, _ = self._run()
        window = source.window(self.ECHO_DELAY_FRAMES * FRAME_MS)
        self.assertEqual(window.state, REFERENCE_OK)
        self.assertEqual(window.delay_ms, self.ECHO_DELAY_FRAMES * FRAME_MS)

    def test_learned_weights_match_the_echo_gain(self) -> None:
        source, _, _, _, _ = self._run(frames=40)
        weights = source.canceller.weights
        peak = int(np.argmax(np.abs(weights)))
        self.assertLessEqual(peak, 8)  # 真实延迟只有 2 个样本
        self.assertAlmostEqual(float(weights[peak]), self.GAIN, delta=0.08)

    def test_most_frames_apply_the_canceller(self) -> None:
        _, _, mic_frames, _, applied = self._run()
        self.assertGreater(applied / len(mic_frames), 0.9)

    def test_processed_output_is_not_silence(self) -> None:
        _, _, _, outputs, _ = self._run()
        tail = np.concatenate(outputs[-10:])
        self.assertGreater(float(np.sqrt(np.mean(tail.astype(np.float64) ** 2))), 1e-5)

    def test_result_details_are_short_codes_and_numbers(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        for index in range(5):
            source.submit(quiet_frame(), submitted_at_seconds=index * 0.02)
        details = source.process(capture_at_ms=0, capture=quiet_frame()).to_details()
        self.assertIn("reference_state", details)
        self.assertIn("applied", details)
        for name in ("pcm", "payload", "authorization", "plan", "tool"):
            self.assertNotIn(name, details)

    def test_zero_delay_echo_is_a_documented_limitation(self) -> None:
        """零延迟回声是本实现的已知约束（权重索引随帧边界位移 1）。

        实测：delay=0 时 ERLE ≈ −1 dB（不收敛）；delay≥1 时 ≈41 dB。真实声学路径必然
        至少 1 个样本延迟，所以这里**断言约束存在**，避免以后误以为它能处理零延迟。
        """

        rng = np.random.default_rng(3)
        source = SameProcessAecReference(
            nlms_config=NlmsConfig(sample_rate=RATE, filter_length_ms=160, erle_gate_db=1.0)
        )
        blocks = 60
        reference = (rng.standard_normal(FRAME_SAMPLES * blocks) * 0.3).astype(np.float32)
        echo = (reference * 0.5).astype(np.float32)  # 零延迟
        outputs = []
        for index in range(blocks):
            source.submit(
                frame_bytes(reference[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]),
                submitted_at_seconds=index * 0.02,
            )
            result = source.process(
                capture_at_ms=index * FRAME_MS,
                capture=frame_bytes(echo[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]),
            )
            outputs.append(result.output)
        tail_out = np.concatenate(outputs[-20:])
        tail_mic = echo[-20 * FRAME_SAMPLES :]
        erle = 10.0 * np.log10(
            float(np.dot(tail_mic, tail_mic)) / float(np.dot(tail_out, tail_out))
        )
        # 零延迟下不应出现高抑制；同时不能被误判为"大增益"式崩溃（|ERLE| 很小）。
        self.assertLess(erle, 6.0)
        self.assertGreater(erle, -20.0)

    def test_one_sample_delay_converges(self) -> None:
        rng = np.random.default_rng(3)
        source = SameProcessAecReference(
            nlms_config=NlmsConfig(sample_rate=RATE, filter_length_ms=160, erle_gate_db=1.0)
        )
        blocks = 60
        reference = (rng.standard_normal(FRAME_SAMPLES * blocks) * 0.3).astype(np.float32)
        echo = np.concatenate((np.zeros(1, dtype=np.float32), reference[:-1] * 0.5)).astype(
            np.float32
        )
        outputs = []
        for index in range(blocks):
            source.submit(
                frame_bytes(reference[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]),
                submitted_at_seconds=index * 0.02,
            )
            result = source.process(
                capture_at_ms=index * FRAME_MS,
                capture=frame_bytes(echo[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]),
            )
            outputs.append(result.output)
        tail_out = np.concatenate(outputs[-20:])
        tail_mic = echo[-20 * FRAME_SAMPLES :]
        erle = 10.0 * np.log10(
            float(np.dot(tail_mic, tail_mic)) / float(np.dot(tail_out, tail_out))
        )
        self.assertGreater(erle, 20.0)

    def test_processing_without_reference_falls_back_and_reports_state(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        result = source.process(capture_at_ms=0, capture=quiet_frame())
        self.assertFalse(result.applied)
        self.assertEqual(result.reference_state, REFERENCE_HISTORY_SHORT)
        self.assertEqual(result.fallback_reason, "reference_history_short")

    def test_wrong_capture_length_is_rejected(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        with self.assertRaises(AecReferenceError):
            source.process(capture_at_ms=0, capture=b"\x00\x00")

    def test_reset_clears_both_timeline_and_canceller(self) -> None:
        source, _, _, _, _ = self._run(frames=20)
        self.assertGreater(source.canceller.frames_processed, 0)
        source.reset()
        self.assertEqual(source.frames_buffered, 0)
        self.assertEqual(source.canceller.frames_processed, 0)


class SameProcessReferenceObserverTests(unittest.TestCase):
    def test_observer_callback_records_frames(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        observer = source.as_submit_observer()
        observer(quiet_frame(), 0.0)
        observer(quiet_frame(), 0.02)
        self.assertEqual(source.frames_submitted, 2)
        self.assertEqual(source.observer_errors, 0)

    def test_observer_swallows_bad_frames_and_records_reason(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        observer = source.as_submit_observer()
        observer(b"", 0.0)  # 非法帧
        self.assertEqual(source.frames_submitted, 0)
        self.assertEqual(source.observer_errors, 1)
        self.assertEqual(source.last_observer_error, "invalid_reference_frame")

    def test_observer_accepts_waveout_sized_chunks(self) -> None:
        """回归：waveOut 一次提交 200 ms（9600 字节），旁路必须能吃下并切成 10 帧。"""

        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        observer = source.as_submit_observer()
        observer(frame_bytes(np.zeros(FRAME_SAMPLES * 10, dtype=np.float32)), 0.0)
        self.assertEqual(source.frames_submitted, 10)
        self.assertEqual(source.observer_errors, 0)

    def test_observer_never_raises_even_on_bad_timestamp(self) -> None:
        source = SameProcessAecReference(nlms_config=NlmsConfig(sample_rate=RATE))
        observer = source.as_submit_observer()
        observer(quiet_frame(), "not-a-number")  # type: ignore[arg-type]
        self.assertGreaterEqual(source.observer_errors, 1)

    def test_module_has_no_device_or_network_imports(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / "reference_source.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        banned = {
            "socket",
            "subprocess",
            "threading",
            "ctypes",
            "os",
            "shutil",
            "wave",
            "sounddevice",
            "pyaudiowpatch",
            "requests",
            "urllib",
            "http",
        }
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned)

    def test_module_never_imports_safety_chain(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[2] / "app" / "audio" / "aec" / "reference_source.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
        offenders = {module for module in modules if ".".join(module.split(".")[:2]) in banned}
        self.assertEqual(offenders, set())


class WaveOutSubmitObserverTests(unittest.TestCase):
    """只读旁路必须有契约：仅在成功后回调、字节原样、异常不影响提交。"""

    def _backend(self, observer=None, *, write_result: int = 0):
        from app.tts.waveout_pcm import WaveOutPCMStreamBackend

        writes: list[bytes] = []

        class FakeWinMM:
            def __init__(self) -> None:
                self.write_results: list[int] = []

            def waveOutOpen(self, handle, device, fmt, callback, instance, flags):
                return 0

            def waveOutPrepareHeader(self, handle, header, size):
                header._obj.dwFlags |= 0x00000002
                return 0

            def waveOutWrite(self, handle, header, size):
                self.write_results.append(write_result)
                writes.append(bytes(ctypes_bytes(header)))
                return write_result

            def waveOutReset(self, handle):
                return 0

            def waveOutUnprepareHeader(self, handle, header, size):
                return 0

            def waveOutClose(self, handle):
                return 0

        import ctypes as _ctypes

        def ctypes_bytes(header) -> bytes:
            header_obj = header._obj
            if header_obj.dwBufferLength == 0:
                return b""
            buffer = _ctypes.cast(header_obj.lpData, _ctypes.POINTER(_ctypes.c_char))
            return _ctypes.string_at(buffer, header_obj.dwBufferLength)

        return WaveOutPCMStreamBackend(winmm=FakeWinMM(), submit_observer=observer), writes

    def test_observer_receives_submitted_bytes(self) -> None:
        seen: list[tuple[bytes, float]] = []
        backend, writes = self._backend(lambda pcm, at: seen.append((pcm, at)))
        backend.open()
        payload = float_to_pcm16(np.full(FRAME_SAMPLES, 0.25, dtype=np.float32))
        backend.submit(payload)
        backend.close()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], payload)
        self.assertIsInstance(seen[0][1], float)
        self.assertEqual(writes[0], payload)

    def test_observer_is_not_called_when_write_fails(self) -> None:
        seen: list[bytes] = []
        backend, _ = self._backend(lambda pcm, at: seen.append(pcm), write_result=5)
        backend.open()
        with self.assertRaises(OSError):
            backend.submit(quiet_frame())
        self.assertEqual(seen, [])

    def test_observer_exception_does_not_break_submit(self) -> None:
        def boom(pcm: bytes, at: float) -> None:
            raise RuntimeError("observer boom")

        backend, writes = self._backend(boom)
        backend.open()
        backend.submit(quiet_frame())  # 不应抛出
        backend.close()
        self.assertEqual(len(writes), 1)

    def test_non_callable_observer_is_rejected(self) -> None:
        from app.tts.waveout_pcm import WaveOutPCMStreamBackend

        with self.assertRaises(TypeError):
            WaveOutPCMStreamBackend(winmm=object(), submit_observer="nope")  # type: ignore[arg-type]

    def test_backend_without_observer_behaves_as_before(self) -> None:
        backend, writes = self._backend()
        backend.open()
        backend.submit(quiet_frame())
        backend.close()
        self.assertEqual(len(writes), 1)


if __name__ == "__main__":
    unittest.main()
