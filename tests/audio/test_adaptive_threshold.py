"""P9 批次 2.1 契约测试：环境自适应语音阈值（拒绝路径优先）。

覆盖：配置校验、零帧/收不满、噪声过高（分位数与最大值两种参考值）、下限钳制与来源、
线性插值分位数、坏帧计 0 但仍计帧数、observe 非契约输入、reset、detector 与驱动口径
一致、``to_details()`` 不含音频、以及模块执行面/纯净度审计（无设备/文件/网络/线程/安全链）。

测试只用合成帧与内存对象，不打开任何设备、不读文件、不联网。
"""

from __future__ import annotations

import ast
import math
import struct
import unittest
from pathlib import Path

from app.audio.bus import AudioPacket
from app.audio.vad.adaptive import (
    INSUFFICIENT_NOISE_FRAMES,
    NOISE_TOO_HIGH,
    SOURCE_ADAPTIVE,
    SOURCE_FLOOR,
    AdaptiveThresholdCalibrator,
    AdaptiveThresholdConfig,
    AdaptiveThresholdDecision,
    AdaptiveThresholdError,
    NoiseFloorMethod,
    NoiseFloorSample,
    adaptive_energy_detector,
    measure_noise_floor,
)
from app.contracts import AudioFrame
from app.contracts.base import ContractError
from app.voice_loop.auto_driver import _energy_detector

SAMPLE_COUNT = 320
PAYLOAD_SIZE_BYTES = 640
REQUIRED_FRAMES = 60  # 1200ms / 20ms
MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "audio" / "vad" / "adaptive.py"


def amplitude_payload(amplitude: int, *, samples: int = SAMPLE_COUNT) -> bytes:
    """恒定幅度 PCM16 LE 帧；其 RMS 恰好等于 ``abs(amplitude)``。"""

    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def make_packet(*, payload: object = b"", sequence: int = 1) -> AudioPacket:
    """构造帧契约自洽的 ``AudioPacket``；``payload`` 允许是坏载荷（长度不匹配）。"""

    frame = AudioFrame(
        audio_session_id="audio-adaptive-1",
        provider_generation=1,
        stream_id="stream-adaptive-1",
        event_sequence=sequence,
        frame_index=sequence - 1,
        captured_at="2026-09-30T08:00:00Z",
        duration_ms=20,
        device_epoch=1,
        sample_rate_hz=16000,
        channel_count=1,
        sample_format="pcm_s16le",
        sample_count=SAMPLE_COUNT,
        payload_size_bytes=PAYLOAD_SIZE_BYTES,
    )
    return AudioPacket(frame, payload)  # type: ignore[arg-type]


def constant_packet(amplitude: int, *, sequence: int = 1) -> AudioPacket:
    return make_packet(payload=amplitude_payload(amplitude), sequence=sequence)


def feed(calibrator: AdaptiveThresholdCalibrator, amplitudes: list[int]) -> list[bool]:
    results: list[bool] = []
    for index, amplitude in enumerate(amplitudes, start=1):
        results.append(calibrator.observe(constant_packet(amplitude, sequence=index)))
    return results


def default_config(**overrides: object) -> AdaptiveThresholdConfig:
    values: dict[str, object] = {
        "sample_duration_ms": 1200,
        "frame_duration_ms": 20,
        "floor_rms": 200.0,
        "ceiling_rms": 800.0,
        "multiplier": 3.0,
        "percentile": 0.95,
    }
    values.update(overrides)
    return AdaptiveThresholdConfig(**values)  # type: ignore[arg-type]


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_frozen_contract(self) -> None:
        config = AdaptiveThresholdConfig()
        self.assertEqual(config.sample_duration_ms, 1200)
        self.assertEqual(config.frame_duration_ms, 20)
        self.assertEqual(config.floor_rms, 200.0)
        self.assertEqual(config.ceiling_rms, 800.0)
        self.assertEqual(config.multiplier, 3.0)
        self.assertEqual(config.percentile, 0.95)
        self.assertIs(config.method, NoiseFloorMethod.PERCENTILE)
        self.assertEqual(config.required_frames, REQUIRED_FRAMES)

    def test_required_frames_rounds_up(self) -> None:
        self.assertEqual(default_config(sample_duration_ms=1100).required_frames, 55)
        self.assertEqual(default_config(sample_duration_ms=1).required_frames, 1)

    def test_invalid_values_are_rejected(self) -> None:
        for kwargs in (
            {"sample_duration_ms": 0},
            {"sample_duration_ms": -5},
            {"sample_duration_ms": 1200.0},
            {"frame_duration_ms": 0},
            {"floor_rms": 0.5},
            {"floor_rms": 0},
            {"ceiling_rms": 200.0},
            {"ceiling_rms": 100.0},
            {"multiplier": 1.0},
            {"multiplier": 0.5},
            {"multiplier": float("nan")},
            {"percentile": 0.0},
            {"percentile": 1.0},
            {"percentile": -0.1},
            {"floor_rms": float("inf")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    default_config(**kwargs)

    def test_method_accepts_enum_and_string_value_only(self) -> None:
        self.assertIs(default_config(method=NoiseFloorMethod.MAX).method, NoiseFloorMethod.MAX)
        self.assertIs(default_config(method="max").method, NoiseFloorMethod.MAX)
        self.assertIs(default_config(method="percentile").method, NoiseFloorMethod.PERCENTILE)
        for bad in ("mean", 1, None, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    default_config(method=bad)


class ErrorCodeTests(unittest.TestCase):
    def test_str_is_only_a_fixed_short_code(self) -> None:
        for code in (INSUFFICIENT_NOISE_FRAMES, NOISE_TOO_HIGH):
            with self.subTest(code=code):
                error = AdaptiveThresholdError(code)
                self.assertEqual(str(error), code)
                self.assertEqual(error.args, (code,))
                self.assertIsInstance(error, ValueError)
                for forbidden in ("\\", "/", ".py", "audio", "pcm", "Traceback"):
                    self.assertNotIn(forbidden, str(error))

    def test_unknown_text_collapses_to_the_insufficient_code(self) -> None:
        leaked = "raw audio bytes C:\\secret\\mic.pcm 设备失败"
        error = AdaptiveThresholdError(leaked)
        self.assertEqual(str(error), INSUFFICIENT_NOISE_FRAMES)
        self.assertNotIn("secret", str(error))
        self.assertNotIn("pcm", str(error))


class NoiseFloorSampleTests(unittest.TestCase):
    def test_details_are_numbers_only(self) -> None:
        sample = NoiseFloorSample(
            frame_count=3, min_rms=1.0, p50_rms=2.0, p95_rms=3.0, max_rms=4.0
        )
        details = sample.to_details()
        self.assertEqual(
            details,
            {
                "frame_count": 3,
                "min_rms": 1.0,
                "p50_rms": 2.0,
                "p95_rms": 3.0,
                "max_rms": 4.0,
            },
        )
        for value in details.values():
            self.assertIsInstance(value, (int, float))


class DecisionTests(unittest.TestCase):
    def make_decision(self, **overrides: object) -> AdaptiveThresholdDecision:
        values: dict[str, object] = {
            "threshold_rms": 300.0,
            "noise": NoiseFloorSample(
                frame_count=60, min_rms=0.0, p50_rms=50.0, p95_rms=99.0, max_rms=100.0
            ),
            "multiplier": 3.0,
            "source": SOURCE_ADAPTIVE,
        }
        values.update(overrides)
        return AdaptiveThresholdDecision(**values)  # type: ignore[arg-type]

    def test_details_expose_threshold_source_and_noise_without_audio(self) -> None:
        details = self.make_decision().to_details()
        self.assertEqual(details["threshold_rms"], 300.0)
        self.assertEqual(details["source"], SOURCE_ADAPTIVE)
        self.assertEqual(details["multiplier"], 3.0)
        self.assertEqual(details["noise"]["p95_rms"], 99.0)  # type: ignore[index]
        self.assertNotIn("payload", details)
        self.assertNotIn("audio", details)

    def test_invalid_source_threshold_or_noise_are_rejected(self) -> None:
        for kwargs in (
            {"source": "percentile"},
            {"source": ""},
            {"threshold_rms": 0.0},
            {"threshold_rms": -1.0},
            {"threshold_rms": float("nan")},
            {"threshold_rms": True},
            {"noise": {"frame_count": 1}},
            {"multiplier": float("inf")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    self.make_decision(**kwargs)


class RejectionTests(unittest.TestCase):
    def test_zero_frames_is_insufficient(self) -> None:
        calibrator = AdaptiveThresholdCalibrator()
        self.assertTrue(calibrator.is_collecting)
        self.assertEqual(calibrator.frame_count, 0)
        with self.assertRaises(AdaptiveThresholdError) as ctx:
            calibrator.decide()
        self.assertEqual(str(ctx.exception), INSUFFICIENT_NOISE_FRAMES)

    def test_partial_collection_is_insufficient(self) -> None:
        calibrator = AdaptiveThresholdCalibrator()
        results = feed(calibrator, [0] * (REQUIRED_FRAMES - 1))
        self.assertNotIn(True, results)
        self.assertTrue(calibrator.is_collecting)
        self.assertEqual(calibrator.frame_count, REQUIRED_FRAMES - 1)
        with self.assertRaises(AdaptiveThresholdError) as ctx:
            calibrator.decide()
        self.assertEqual(str(ctx.exception), INSUFFICIENT_NOISE_FRAMES)

    def test_single_frame_config_still_requires_one_frame(self) -> None:
        calibrator = AdaptiveThresholdCalibrator(config=default_config(sample_duration_ms=1))
        with self.assertRaises(AdaptiveThresholdError):
            calibrator.decide()
        self.assertTrue(calibrator.observe(constant_packet(0)))
        self.assertFalse(calibrator.is_collecting)
        self.assertEqual(calibrator.decide().threshold_rms, 200.0)

    def test_measure_noise_floor_without_packets_is_insufficient(self) -> None:
        with self.assertRaises(AdaptiveThresholdError) as ctx:
            measure_noise_floor([])
        self.assertEqual(str(ctx.exception), INSUFFICIENT_NOISE_FRAMES)

    def test_observe_rejects_non_contract_inputs(self) -> None:
        calibrator = AdaptiveThresholdCalibrator()
        for bad in (b"raw-bytes", bytearray(b"x"), "text", None, 3, object(), [constant_packet(1)]):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises((ContractError, TypeError)):
                    calibrator.observe(bad)  # type: ignore[arg-type]
        self.assertEqual(calibrator.frame_count, 0)

    def test_duck_typed_packet_is_not_accepted(self) -> None:
        class PacketLike:
            payload = b"\x01\x02"
            frame = None

        calibrator = AdaptiveThresholdCalibrator()
        with self.assertRaises((ContractError, TypeError)):
            calibrator.observe(PacketLike())  # type: ignore[arg-type]

    def test_measure_noise_floor_rejects_non_packet_elements(self) -> None:
        with self.assertRaises((ContractError, TypeError)):
            measure_noise_floor([constant_packet(1), b"not-a-packet"])  # type: ignore[list-item]

    def test_detector_rejects_non_decision_input(self) -> None:
        for bad in (300.0, None, "adaptive", object()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises((ContractError, TypeError)):
                    adaptive_energy_detector(bad)  # type: ignore[arg-type]


class ReferenceValueTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self) -> None:
        # 100 帧恒定幅度 0..99：p50 落在 49/50 之间，p95 落在 94/95 之间。
        calibrator = AdaptiveThresholdCalibrator()
        feed(calibrator, list(range(100)))
        decision = calibrator.decide()
        self.assertEqual(decision.noise.frame_count, 100)
        self.assertEqual(decision.noise.min_rms, 0.0)
        self.assertEqual(decision.noise.max_rms, 99.0)
        self.assertAlmostEqual(decision.noise.p50_rms, 49.5)
        self.assertAlmostEqual(decision.noise.p95_rms, 94.05)
        self.assertAlmostEqual(decision.threshold_rms, 3.0 * 94.05)
        self.assertEqual(decision.source, SOURCE_ADAPTIVE)

    def test_custom_percentile_selects_the_reference(self) -> None:
        calibrator = AdaptiveThresholdCalibrator(config=default_config(percentile=0.5))
        feed(calibrator, list(range(200, 300)))
        decision = calibrator.decide()
        self.assertAlmostEqual(decision.noise.p50_rms, 249.5)
        self.assertAlmostEqual(decision.threshold_rms, 3.0 * 249.5)
        self.assertEqual(decision.source, SOURCE_ADAPTIVE)

    def test_max_method_uses_the_maximum(self) -> None:
        calibrator = AdaptiveThresholdCalibrator(
            config=default_config(method=NoiseFloorMethod.MAX)
        )
        feed(calibrator, list(range(100)))
        decision = calibrator.decide()
        self.assertAlmostEqual(decision.noise.p95_rms, 94.05)
        self.assertAlmostEqual(decision.threshold_rms, 3.0 * 99.0)
        self.assertEqual(decision.source, SOURCE_ADAPTIVE)

    def test_single_sample_percentile_is_the_value_itself(self) -> None:
        config = default_config(sample_duration_ms=20, frame_duration_ms=20)
        decision = measure_noise_floor([constant_packet(90)], config=config)
        self.assertEqual(decision.noise.frame_count, 1)
        self.assertAlmostEqual(decision.threshold_rms, 270.0)
        self.assertAlmostEqual(decision.noise.p50_rms, 90.0)
        self.assertAlmostEqual(decision.noise.p95_rms, 90.0)


class FloorAndCeilingTests(unittest.TestCase):
    def test_silent_noise_clamps_to_the_floor(self) -> None:
        decision = measure_noise_floor([constant_packet(0, sequence=i + 1) for i in range(60)])
        self.assertEqual(decision.threshold_rms, 200.0)
        self.assertEqual(decision.source, SOURCE_FLOOR)
        self.assertEqual(decision.noise.max_rms, 0.0)

    def test_just_below_the_floor_stays_floor_and_above_becomes_adaptive(self) -> None:
        below = measure_noise_floor([constant_packet(66, sequence=i + 1) for i in range(60)])
        self.assertAlmostEqual(below.threshold_rms, 200.0)
        self.assertEqual(below.source, SOURCE_FLOOR)
        above = measure_noise_floor([constant_packet(67, sequence=i + 1) for i in range(60)])
        self.assertAlmostEqual(above.threshold_rms, 201.0)
        self.assertEqual(above.source, SOURCE_ADAPTIVE)

    def test_reference_at_the_ceiling_is_rejected(self) -> None:
        with self.assertRaises(AdaptiveThresholdError) as ctx:
            measure_noise_floor([constant_packet(800, sequence=i + 1) for i in range(60)])
        self.assertEqual(str(ctx.exception), NOISE_TOO_HIGH)

    def test_reference_above_the_ceiling_is_rejected_for_both_methods(self) -> None:
        packets = [constant_packet(4000, sequence=i + 1) for i in range(60)]
        for method in (NoiseFloorMethod.PERCENTILE, NoiseFloorMethod.MAX):
            with self.subTest(method=method):
                with self.assertRaises(AdaptiveThresholdError) as ctx:
                    measure_noise_floor(packets, config=default_config(method=method))
                self.assertEqual(str(ctx.exception), NOISE_TOO_HIGH)

    def test_just_below_the_ceiling_is_accepted(self) -> None:
        decision = measure_noise_floor(
            [constant_packet(799, sequence=i + 1) for i in range(60)]
        )
        self.assertAlmostEqual(decision.threshold_rms, 2397.0)
        self.assertEqual(decision.source, SOURCE_ADAPTIVE)


class BadFrameTests(unittest.TestCase):
    def test_empty_payload_counts_as_zero_rms_and_still_counted(self) -> None:
        config = default_config(sample_duration_ms=20, frame_duration_ms=20)
        decision = measure_noise_floor([make_packet(payload=b"")], config=config)
        self.assertEqual(decision.noise.frame_count, 1)
        self.assertEqual(decision.noise.max_rms, 0.0)
        self.assertEqual(decision.threshold_rms, 200.0)
        self.assertEqual(decision.source, SOURCE_FLOOR)

    def test_odd_length_payload_ignores_the_trailing_byte(self) -> None:
        config = default_config(sample_duration_ms=20, frame_duration_ms=20)
        decision = measure_noise_floor(
            [make_packet(payload=b"\x01\x02\x03")], config=config
        )
        # \x01\x02 -> 小端 short 0x0201 = 513；尾部单字节被忽略（与驱动同口径）。
        self.assertAlmostEqual(decision.noise.max_rms, 513.0)
        self.assertEqual(decision.noise.frame_count, 1)

    def test_non_bytes_payload_counts_as_zero_rms(self) -> None:
        config = default_config(sample_duration_ms=20, frame_duration_ms=20)
        decision = measure_noise_floor(
            [make_packet(payload="not-bytes")], config=config
        )
        self.assertEqual(decision.noise.max_rms, 0.0)
        self.assertEqual(decision.noise.frame_count, 1)

    def test_mixed_bad_frames_still_produce_a_decision(self) -> None:
        config = default_config(sample_duration_ms=60, frame_duration_ms=20)
        packets = [
            make_packet(payload=b"", sequence=1),
            make_packet(payload=amplitude_payload(300), sequence=2),
            make_packet(payload=b"\x05", sequence=3),
        ]
        decision = measure_noise_floor(packets, config=config)
        self.assertEqual(decision.noise.frame_count, 3)
        self.assertEqual(decision.noise.min_rms, 0.0)
        self.assertAlmostEqual(decision.noise.max_rms, 300.0)
        # 三个样本 [0, 0, 300]：p95 线性插值为 0 + 0.9*300 = 270 -> 阈值 810。
        self.assertAlmostEqual(decision.noise.p95_rms, 270.0)
        self.assertAlmostEqual(decision.threshold_rms, 810.0)
        self.assertEqual(decision.source, SOURCE_ADAPTIVE)


class LifecycleTests(unittest.TestCase):
    def test_observe_returns_true_only_when_full(self) -> None:
        calibrator = AdaptiveThresholdCalibrator()
        results = feed(calibrator, [10] * REQUIRED_FRAMES)
        self.assertEqual(results, [False] * (REQUIRED_FRAMES - 1) + [True])
        self.assertFalse(calibrator.is_collecting)
        self.assertEqual(calibrator.frame_count, REQUIRED_FRAMES)
        # 收满后的额外帧仍如实计入（调用方应据返回值停止观察）。
        self.assertTrue(calibrator.observe(constant_packet(10, sequence=REQUIRED_FRAMES + 1)))
        self.assertEqual(calibrator.frame_count, REQUIRED_FRAMES + 1)

    def test_reset_restarts_collection_and_clears_stats(self) -> None:
        calibrator = AdaptiveThresholdCalibrator()
        feed(calibrator, [50] * REQUIRED_FRAMES)
        self.assertFalse(calibrator.is_collecting)
        calibrator.reset()
        self.assertTrue(calibrator.is_collecting)
        self.assertEqual(calibrator.frame_count, 0)
        with self.assertRaises(AdaptiveThresholdError):
            calibrator.decide()

    def test_config_property_is_the_frozen_config(self) -> None:
        config = default_config()
        calibrator = AdaptiveThresholdCalibrator(config=config)
        self.assertIs(calibrator.config, config)
        self.assertIs(AdaptiveThresholdCalibrator().config.method, NoiseFloorMethod.PERCENTILE)

    def test_invalid_calibrator_config_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            AdaptiveThresholdCalibrator(config={"sample_duration_ms": 1200})  # type: ignore[arg-type]

    def test_measure_noise_floor_accepts_a_generator(self) -> None:
        config = default_config(sample_duration_ms=60, frame_duration_ms=20)
        decision = measure_noise_floor(
            (constant_packet(120, sequence=i + 1) for i in range(3)), config=config
        )
        self.assertEqual(decision.noise.frame_count, 3)
        self.assertAlmostEqual(decision.threshold_rms, 360.0)


class DetectorParityTests(unittest.TestCase):
    def test_detector_matches_the_driver_energy_detector(self) -> None:
        calibrator = AdaptiveThresholdCalibrator()
        feed(calibrator, [150] * REQUIRED_FRAMES)
        decision = calibrator.decide()
        threshold = decision.threshold_rms  # 450.0
        mine = adaptive_energy_detector(decision)
        driver = _energy_detector(threshold)
        payloads = [
            b"",
            b"\x00",
            amplitude_payload(0),
            amplitude_payload(449),
            amplitude_payload(450),
            amplitude_payload(451),
            amplitude_payload(-450),
            amplitude_payload(-451),
            b"\x01\x02\x03",
        ]
        for index, payload in enumerate(payloads):
            packet = make_packet(
                payload=payload, sequence=index + 1
            )
            with self.subTest(payload=repr(payload)[:24]):
                self.assertEqual(mine(packet), driver(packet))

    def test_non_bytes_payload_is_counted_as_silence_by_the_detector(self) -> None:
        # 驱动的 RMS 只接受字节载荷；本模块按契约把坏帧计为 0 RMS（fail closed）。
        decision = measure_noise_floor([constant_packet(150, sequence=i + 1) for i in range(60)])
        detect = adaptive_energy_detector(decision)
        self.assertFalse(detect(make_packet(payload="not-bytes")))

    def test_threshold_comparison_is_inclusive(self) -> None:
        decision = measure_noise_floor([constant_packet(150, sequence=i + 1) for i in range(60)])
        self.assertEqual(decision.threshold_rms, 450.0)
        detect = adaptive_energy_detector(decision)
        self.assertTrue(detect(constant_packet(450)))
        self.assertFalse(detect(constant_packet(449)))

    def test_silence_threshold_floor_behaviour(self) -> None:
        decision = measure_noise_floor([constant_packet(0, sequence=i + 1) for i in range(60)])
        self.assertEqual(decision.source, SOURCE_FLOOR)
        detect = adaptive_energy_detector(decision)
        self.assertTrue(detect(constant_packet(200)))
        self.assertFalse(detect(constant_packet(199)))

    def test_decision_threshold_is_finite_and_positive(self) -> None:
        decision = measure_noise_floor([constant_packet(500, sequence=i + 1) for i in range(60)])
        self.assertTrue(math.isfinite(decision.threshold_rms))
        self.assertGreater(decision.threshold_rms, 0.0)


class ModulePurityAuditTests(unittest.TestCase):
    ALLOWED_IMPORT_ROOTS = {
        "__future__",
        "math",
        "struct",
        "collections",
        "dataclasses",
        "enum",
        "app",
    }
    BANNED_IMPORT_ROOTS = {
        "subprocess",
        "socket",
        "threading",
        "multiprocessing",
        "concurrent",
        "asyncio",
        "signal",
        "os",
        "sys",
        "io",
        "pathlib",
        "shutil",
        "tempfile",
        "time",
        "random",
        "sounddevice",
        "sherpa_onnx",
        "ctypes",
        "pickle",
    }
    BANNED_CALLS = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "system",
        "popen",
        "spawn",
        "fork",
        "Thread",
        "Timer",
        "connect",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_module_only_imports_stdlib_bus_and_contracts(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        self.assertEqual(roots - self.ALLOWED_IMPORT_ROOTS, set())
        self.assertEqual(roots & self.BANNED_IMPORT_ROOTS, set())
        # 只允许 app.audio.bus 与 app.contracts*（不得拉起 voice_loop/安全链）。
        for name in imported:
            if name.startswith("app."):
                self.assertTrue(
                    name == "app.audio.bus" or name == "app.contracts" or name.startswith("app.contracts."),
                    msg=f"未授权的 app 导入：{name}",
                )

    def test_no_dangerous_calls_in_the_module(self) -> None:
        called: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        self.assertEqual(called & self.BANNED_CALLS, set())

    def test_module_never_references_the_safety_chain(self) -> None:
        for module in (
            "app.safety",
            "app.executor",
            "app.plans",
            "app.auditing",
            "app.verification",
            "app.tools",
            "app.voice_loop",
            "app.main",
            "app.runtime",
        ):
            with self.subTest(module=module):
                self.assertNotIn(module, self.source)

    def test_module_has_no_file_or_network_access(self) -> None:
        self.assertNotIn("write_bytes", self.source)
        self.assertNotIn("read_bytes", self.source)
        self.assertNotIn("read_text", self.source)
        self.assertNotIn("socket", self.source)
        self.assertNotIn("threading", self.source)

    def test_module_is_utf8_without_replacement_characters(self) -> None:
        raw = MODULE_PATH.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn("\ufffd", self.source)
        raw.decode("utf-8")


if __name__ == "__main__":
    unittest.main()
