"""P9 批次 2.2 契约测试：sherpa-onnx Silero VAD 人声判定（拒绝路径优先）。

覆盖：配置校验、错误码收敛（``str()`` 只有固定短代码）、模型缺失、依赖缺失、构造失败、
非契约输入与奇数字节载荷、采样率/格式/声道不支持、512 样本窗口聚合与残余样本、
连续命中阈值、检测异常与非布尔判定 fail closed、注入替身路径、``reset()``、
``to_details()`` 只含机械事实，以及模块执行面/纯净度审计（无设备/文件读写/网络/线程/
安全链，``sherpa_onnx`` 只在构造时延迟导入）。

真机 Silero 模型只在 ``skipUnless`` 冒烟里只读加载；其余用例全部注入替身。
"""

from __future__ import annotations

import ast
import importlib.util
import math
import struct
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.audio.bus import AudioPacket
from app.audio.vad import silero
from app.audio.vad.silero import (
    DEFAULT_SILERO_MODEL_PATH,
    DEPENDENCY_MISSING,
    DETECT_FAILED,
    INIT_FAILED,
    MODEL_MISSING,
    SILERO_WINDOW_SAMPLES,
    UNSUPPORTED_FORMAT,
    UNSUPPORTED_SAMPLE_RATE,
    SileroVadConfig,
    SileroVadDetector,
    SileroVadError,
    build_silero_vad_detector,
)
from app.contracts import AudioFrame
from app.contracts.base import ContractError

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "app" / "audio" / "vad" / "silero.py"
MODEL_PATH = REPO_ROOT / "models" / "openwakeword" / "silero_vad.onnx"
# 仓库内既有的 24kHz 单声道真机语音片段（只读用于 Silero 冒烟，不写回）。
SPEECH_WAV_PATH = (
    REPO_ROOT
    / "implementation_plan"
    / "checkpoints"
    / "artifacts"
    / "2026-09-28-P7-local-voicestudio"
    / "eff172cb.wav"
)
HAS_SHERPA = importlib.util.find_spec("sherpa_onnx") is not None
MISSING_MODEL_PATH = Path(__file__).parent / "__no_such_silero_model__.onnx"

SAMPLE_COUNT = 320
PAYLOAD_SIZE_BYTES = 640
_UNSET = object()
ALL_ERROR_CODES = (
    MODEL_MISSING,
    DEPENDENCY_MISSING,
    INIT_FAILED,
    DETECT_FAILED,
    UNSUPPORTED_SAMPLE_RATE,
    UNSUPPORTED_FORMAT,
)


def pcm16(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def window_payload(amplitude: int = 0, *, samples: int = SILERO_WINDOW_SAMPLES) -> bytes:
    """恰好 ``samples`` 个样本的 PCM16 LE 载荷（默认一个整窗）。"""

    return pcm16(*([amplitude] * samples))


def make_packet(*, payload: object = b"", sequence: int = 1, **frame_overrides: object) -> AudioPacket:
    """构造帧契约自洽的 ``AudioPacket``；``payload`` 允许是坏载荷（类型/长度不匹配）。"""

    values: dict[str, object] = {
        "audio_session_id": "audio-silero-1",
        "provider_generation": 1,
        "stream_id": "stream-silero-1",
        "event_sequence": sequence,
        "frame_index": sequence - 1,
        "captured_at": "2026-10-01T08:00:00Z",
        "duration_ms": 20,
        "device_epoch": 1,
        "sample_rate_hz": 16000,
        "channel_count": 1,
        "sample_format": "pcm_s16le",
        "sample_count": SAMPLE_COUNT,
        "payload_size_bytes": PAYLOAD_SIZE_BYTES,
    }
    values.update(frame_overrides)
    return AudioPacket(AudioFrame(**values), payload)  # type: ignore[arg-type]


def frame_view(**overrides: object) -> SimpleNamespace:
    """只带本模块读取字段的帧替身；用于 ``AudioFrame`` 契约不允许的非法格式/声道组合。"""

    values: dict[str, object] = {
        "sample_rate_hz": 16000,
        "sample_format": "pcm_s16le",
        "channel_count": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeVad:
    """Silero VAD 注入替身：记录喂入窗口，按窗口序号返回预设判定。"""

    def __init__(
        self,
        detections: object = (),
        *,
        accept_error: Exception | None = None,
        detect_error: Exception | None = None,
        reset_error: Exception | None = None,
        non_bool: object = _UNSET,
    ) -> None:
        self.windows: list[list[float]] = []
        self.detections = list(detections)  # type: ignore[arg-type]
        self.accept_error = accept_error
        self.detect_error = detect_error
        self.reset_error = reset_error
        self.non_bool = non_bool
        self.reset_calls = 0

    def accept_waveform(self, samples: object) -> None:
        if self.accept_error is not None:
            raise self.accept_error
        self.windows.append(list(samples))  # type: ignore[arg-type]

    def is_speech_detected(self) -> object:
        if self.detect_error is not None:
            raise self.detect_error
        if self.non_bool is not _UNSET:
            return self.non_bool
        index = len(self.windows) - 1
        if not self.detections:
            return False
        if index < len(self.detections):
            return self.detections[index]
        return self.detections[-1]

    def reset(self) -> None:
        self.reset_calls += 1
        if self.reset_error is not None:
            raise self.reset_error


def fake_detector(
    vad: FakeVad | None = None, **config_overrides: object
) -> tuple[SileroVadDetector, FakeVad]:
    resolved_vad = FakeVad() if vad is None else vad
    values: dict[str, object] = {"model_path": MISSING_MODEL_PATH}
    values.update(config_overrides)
    detector = SileroVadDetector(config=SileroVadConfig(**values), vad=resolved_vad)  # type: ignore[arg-type]
    return detector, resolved_vad


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_frozen_contract(self) -> None:
        config = SileroVadConfig()
        self.assertIsNone(config.model_path)
        self.assertEqual(config.threshold, 0.5)
        self.assertEqual(config.min_silence_duration, 0.5)
        self.assertEqual(config.min_speech_duration, 0.15)
        self.assertEqual(config.buffer_size_in_seconds, 30.0)
        self.assertEqual(config.sample_rate_hz, 16000)
        self.assertEqual(config.speech_frames_required, 2)
        self.assertEqual(config.resolved_model_path, DEFAULT_SILERO_MODEL_PATH)
        self.assertEqual(SILERO_WINDOW_SAMPLES, 512)

    def test_model_path_accepts_path_and_non_empty_string(self) -> None:
        self.assertEqual(SileroVadConfig(model_path="a/b.onnx").model_path, Path("a/b.onnx"))
        self.assertEqual(SileroVadConfig(model_path=Path("c.onnx")).model_path, Path("c.onnx"))
        for bad in ("", 1, True, object(), ["a.onnx"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    SileroVadConfig(model_path=bad)  # type: ignore[arg-type]

    def test_invalid_numbers_are_rejected(self) -> None:
        for kwargs in (
            {"threshold": 0.0},
            {"threshold": -0.1},
            {"threshold": 1.5},
            {"threshold": float("nan")},
            {"threshold": float("inf")},
            {"threshold": True},
            {"threshold": "0.5"},
            {"min_silence_duration": 0.0},
            {"min_silence_duration": -1.0},
            {"min_silence_duration": float("nan")},
            {"min_speech_duration": 0.0},
            {"min_speech_duration": -0.5},
            {"buffer_size_in_seconds": 0.0},
            {"buffer_size_in_seconds": -30.0},
            {"sample_rate_hz": 0},
            {"sample_rate_hz": -16000},
            {"sample_rate_hz": 16000.0},
            {"sample_rate_hz": True},
            {"speech_frames_required": 0},
            {"speech_frames_required": -2},
            {"speech_frames_required": 1.5},
            {"speech_frames_required": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    SileroVadConfig(**kwargs)  # type: ignore[arg-type]


class ErrorCodeTests(unittest.TestCase):
    def test_str_is_only_a_fixed_short_code(self) -> None:
        for code in ALL_ERROR_CODES:
            with self.subTest(code=code):
                error = SileroVadError(code)
                self.assertEqual(str(error), code)
                self.assertEqual(error.args, (code,))
                self.assertEqual(error.code, code)
                self.assertIsInstance(error, RuntimeError)
                for forbidden in ("\\", "/", ".onnx", ".py", "audio", "pcm", "Traceback", "sherpa"):
                    self.assertNotIn(forbidden, str(error))

    def test_unknown_text_collapses_to_init_failed_without_leaking(self) -> None:
        leaked = "raw audio C:\\secret\\mic.pcm 设备失败 sherpa_onnx"
        for code in (leaked, "", None, 3, object()):
            with self.subTest(code=type(code).__name__):
                error = SileroVadError(code)  # type: ignore[arg-type]
                self.assertEqual(str(error), INIT_FAILED)
                self.assertNotIn("secret", str(error))
                self.assertNotIn("pcm", str(error))


def raising_module(exc: Exception) -> types.ModuleType:
    """假 sherpa_onnx：构造入口一律抛 ``exc``（用于离线验证 fail-closed 收敛）。"""

    module = types.ModuleType("sherpa_onnx")

    def _raise(*args: object, **kwargs: object) -> object:
        raise exc

    module.VadModelConfig = _raise  # type: ignore[attr-defined]
    module.VoiceActivityDetector = _raise  # type: ignore[attr-defined]
    return module


class ConstructionRejectionTests(unittest.TestCase):
    def test_missing_model_file_is_rejected_with_a_fixed_code(self) -> None:
        with self.assertRaises(SileroVadError) as ctx:
            build_silero_vad_detector(config=SileroVadConfig(model_path=MISSING_MODEL_PATH))
        self.assertEqual(str(ctx.exception), MODEL_MISSING)
        self.assertNotIn("silero", str(ctx.exception))
        self.assertNotIn(".onnx", str(ctx.exception))

    def test_missing_dependency_is_rejected_with_a_fixed_code(self) -> None:
        # 存在一个非模型文件（本测试文件自身）以满足 is_file()，再阻断 sherpa_onnx 导入。
        config = SileroVadConfig(model_path=Path(__file__))
        with mock.patch.dict(sys.modules, {"sherpa_onnx": None}):
            with self.assertRaises(SileroVadError) as ctx:
                build_silero_vad_detector(config=config)
        self.assertEqual(str(ctx.exception), DEPENDENCY_MISSING)

    def test_construction_failure_is_rejected_with_a_fixed_code(self) -> None:
        config = SileroVadConfig(model_path=Path(__file__))
        leaked = RuntimeError("C:\\secret\\silero_vad.onnx raw audio failed")
        for module in (raising_module(leaked), self._config_only_failure_module(leaked)):
            with self.subTest(module=type(module).__name__):
                with mock.patch.dict(sys.modules, {"sherpa_onnx": module}):
                    with self.assertRaises(SileroVadError) as ctx:
                        build_silero_vad_detector(config=config)
                self.assertEqual(str(ctx.exception), INIT_FAILED)
                self.assertNotIn("secret", str(ctx.exception))
                self.assertNotIn("pcm", str(ctx.exception))

    @staticmethod
    def _config_only_failure_module(exc: Exception) -> types.ModuleType:
        """``VadModelConfig`` 可用但 ``VoiceActivityDetector`` 构造失败。"""

        module = types.ModuleType("sherpa_onnx")

        class _Config:
            def __init__(self) -> None:
                self.silero_vad = SimpleNamespace()
                self.sample_rate = 0

        def _raise(*args: object, **kwargs: object) -> object:
            raise exc

        module.VadModelConfig = _Config  # type: ignore[attr-defined]
        module.VoiceActivityDetector = _raise  # type: ignore[attr-defined]
        return module

    def test_injected_vad_bypasses_the_model_check_and_import(self) -> None:
        detector, vad = fake_detector()
        self.assertTrue(detector.injected)
        self.assertEqual(detector.model_path, MISSING_MODEL_PATH)
        self.assertIs(detector.config.model_path, MISSING_MODEL_PATH)
        self.assertEqual(vad.windows, [])

    def test_injected_vad_must_expose_the_three_methods(self) -> None:
        class OnlyAccept:
            def accept_waveform(self, samples: object) -> None:  # pragma: no cover - 不被调用
                del samples

        for bad in (SimpleNamespace(), OnlyAccept(), object(), 3):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises((ContractError, TypeError)):
                    SileroVadDetector(vad=bad)  # type: ignore[arg-type]

    def test_invalid_config_type_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            SileroVadDetector(config={"threshold": 0.5})  # type: ignore[arg-type]


class ObserveRejectionTests(unittest.TestCase):
    def test_non_contract_inputs_are_rejected(self) -> None:
        detector, vad = fake_detector()
        for bad in (b"raw-bytes", bytearray(b"x"), "text", None, 3, object(), SimpleNamespace()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises((ContractError, TypeError)):
                    detector.observe(bad)  # type: ignore[arg-type]
        self.assertEqual(detector.observed_frames, 0)
        self.assertEqual(detector.consecutive_speech, 0)
        self.assertEqual(vad.windows, [])

    def test_non_bytes_payload_is_rejected(self) -> None:
        detector, vad = fake_detector()
        for bad in ("text", 3, None, [1, 2], SimpleNamespace()):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(TypeError):
                    detector.observe(make_packet(payload=bad))
        self.assertEqual(detector.observed_frames, 0)
        self.assertEqual(vad.windows, [])

    def test_odd_length_payload_is_rejected(self) -> None:
        detector, vad = fake_detector()
        for payload in (b"\x01", b"\x01\x02\x03", b"\x00" * 1023):
            with self.subTest(size=len(payload)):
                with self.assertRaises(ValueError):
                    detector.observe(make_packet(payload=payload))
        self.assertEqual(detector.observed_frames, 0)
        self.assertEqual(vad.windows, [])

    def test_unsupported_sample_rate_is_rejected(self) -> None:
        detector, _vad = fake_detector()
        packet = make_packet(
            payload=window_payload(),
            sample_rate_hz=8000,
            sample_count=160,
            payload_size_bytes=320,
        )
        with self.assertRaises(SileroVadError) as ctx:
            detector.observe(packet)
        self.assertEqual(str(ctx.exception), UNSUPPORTED_SAMPLE_RATE)
        self.assertEqual(detector.observed_frames, 0)

    def test_unsupported_format_and_channels_are_rejected(self) -> None:
        detector, _vad = fake_detector()
        bad_format = AudioPacket(frame_view(sample_format="pcm_f32le"), b"\x00\x00")  # type: ignore[arg-type]
        with self.assertRaises(SileroVadError) as ctx:
            detector.observe(bad_format)
        self.assertEqual(str(ctx.exception), UNSUPPORTED_FORMAT)
        stereo = make_packet(
            payload=b"\x00\x00",
            channel_count=2,
            payload_size_bytes=SAMPLE_COUNT * 2 * 2,
        )
        with self.assertRaises(SileroVadError) as ctx:
            detector.observe(stereo)
        self.assertEqual(str(ctx.exception), UNSUPPORTED_FORMAT)
        self.assertEqual(detector.observed_frames, 0)

    def test_empty_payload_is_a_valid_no_op_frame(self) -> None:
        detector, vad = fake_detector()
        self.assertFalse(detector.observe(make_packet(payload=b"")))
        self.assertEqual(detector.observed_frames, 1)
        self.assertEqual(vad.windows, [])


class WindowAggregationTests(unittest.TestCase):
    def test_exact_window_is_normalized_as_float(self) -> None:
        vad = FakeVad([True])
        detector, _vad = fake_detector(vad)
        self.assertTrue(detector.observe(make_packet(payload=window_payload(16384))))
        self.assertEqual(len(vad.windows), 1)
        self.assertEqual(len(vad.windows[0]), SILERO_WINDOW_SAMPLES)
        self.assertEqual(vad.windows[0][0], 0.5)
        self.assertTrue(all(value == 0.5 for value in vad.windows[0]))
        self.assertTrue(all(isinstance(value, float) for value in vad.windows[0]))

    def test_two_windows_in_one_frame(self) -> None:
        vad = FakeVad([True, False])
        detector, _vad = fake_detector(vad)
        self.assertTrue(detector.observe(make_packet(payload=window_payload(100, samples=1024))))
        self.assertEqual(len(vad.windows), 2)
        self.assertEqual(detector.observed_frames, 1)

    def test_partial_frame_keeps_residual_samples(self) -> None:
        vad = FakeVad([True])
        detector, _vad = fake_detector(vad)
        first = make_packet(payload=window_payload(100, samples=320), sequence=1)
        second = make_packet(payload=window_payload(200, samples=320), sequence=2)
        self.assertFalse(detector.observe(first))
        self.assertEqual(vad.windows, [])
        self.assertTrue(detector.observe(second))
        self.assertEqual(len(vad.windows), 1)
        window = vad.windows[0]
        self.assertEqual(len(window), SILERO_WINDOW_SAMPLES)
        self.assertEqual(window[:320], [100 / 32768.0] * 320)
        self.assertEqual(window[320:], [200 / 32768.0] * 192)

    def test_residual_carries_across_frames(self) -> None:
        vad = FakeVad([True, True])
        detector, _vad = fake_detector(vad)
        hits = [
            detector.observe(make_packet(payload=window_payload(100, samples=320), sequence=index))
            for index in range(1, 5)
        ]
        # 4 帧 x 320 样本 = 1280 = 2 整窗 + 256 残余。
        self.assertEqual(hits, [False, True, False, True])
        self.assertEqual(len(vad.windows), 2)
        detector.reset()
        remaining = [
            detector.observe(make_packet(payload=window_payload(100, samples=320), sequence=index))
            for index in range(5, 7)
        ]
        # reset 已清空 256 残余：再喂 640 样本才凑满第 3 个整窗。
        self.assertEqual(remaining, [False, True])
        self.assertEqual(len(vad.windows), 3)

    def test_hit_is_reported_only_for_frames_with_a_window(self) -> None:
        vad = FakeVad([True])
        detector, _vad = fake_detector(vad)
        self.assertFalse(detector.observe(make_packet(payload=window_payload(0, samples=256))))
        self.assertTrue(detector.observe(make_packet(payload=window_payload(0, samples=256))))


class ConsecutiveSpeechTests(unittest.TestCase):
    def test_start_requires_consecutive_hits(self) -> None:
        vad = FakeVad([True, False, True, True])
        detector, _vad = fake_detector(vad, speech_frames_required=2)
        results = [
            detector.observe(make_packet(payload=window_payload(), sequence=index))
            for index in range(1, 5)
        ]
        self.assertEqual(results, [True, False, True, True])
        self.assertEqual(detector.consecutive_speech, 2)
        self.assertTrue(detector.speech_started)
        self.assertEqual(detector.observed_frames, 4)

    def test_start_is_latched_until_reset(self) -> None:
        vad = FakeVad([True, True, False, False])
        detector, _vad = fake_detector(vad, speech_frames_required=2)
        for index in range(1, 5):
            detector.observe(make_packet(payload=window_payload(), sequence=index))
        self.assertTrue(detector.speech_started)
        self.assertEqual(detector.consecutive_speech, 0)

    def test_three_consecutive_hits_are_required_when_configured(self) -> None:
        vad = FakeVad([True, False, True, True, False, True, True, True])
        detector, _vad = fake_detector(vad, speech_frames_required=3)
        states = []
        for index in range(1, 9):
            detector.observe(make_packet(payload=window_payload(), sequence=index))
            states.append(detector.speech_started)
        self.assertEqual(
            states, [False, False, False, False, False, False, False, True]
        )
        self.assertTrue(detector.speech_started)

    def test_zero_or_negative_required_is_rejected_by_config(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    SileroVadConfig(speech_frames_required=value)


class DetectFailureTests(unittest.TestCase):
    def test_accept_waveform_failure_is_fail_closed(self) -> None:
        vad = FakeVad(accept_error=RuntimeError("C:\\secret\\mic.pcm raw audio"))
        detector, _vad = fake_detector(vad)
        with self.assertRaises(SileroVadError) as ctx:
            detector.observe(make_packet(payload=window_payload()))
        self.assertEqual(str(ctx.exception), DETECT_FAILED)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("pcm", str(ctx.exception))
        self.assertEqual(detector.consecutive_speech, 0)
        self.assertFalse(detector.speech_started)

    def test_is_speech_detected_failure_is_fail_closed(self) -> None:
        vad = FakeVad(detect_error=RuntimeError("boom"))
        detector, _vad = fake_detector(vad)
        with self.assertRaises(SileroVadError) as ctx:
            detector.observe(make_packet(payload=window_payload()))
        self.assertEqual(str(ctx.exception), DETECT_FAILED)

    def test_non_boolean_detection_is_fail_closed(self) -> None:
        for value in (1, 0, None, "yes", [True]):
            with self.subTest(value=value):
                vad = FakeVad(non_bool=value)
                detector, _vad = fake_detector(vad)
                with self.assertRaises(SileroVadError) as ctx:
                    detector.observe(make_packet(payload=window_payload()))
                self.assertEqual(str(ctx.exception), DETECT_FAILED)

    def test_failure_on_a_later_window_still_raises(self) -> None:
        vad = FakeVad([False])
        vad.detect_error = RuntimeError("late failure")
        detector, _vad = fake_detector(vad)
        with self.assertRaises(SileroVadError):
            detector.observe(make_packet(payload=window_payload(samples=1024)))

    def test_reset_failure_is_fail_closed(self) -> None:
        vad = FakeVad(reset_error=RuntimeError("reset boom"))
        detector, _vad = fake_detector(vad)
        with self.assertRaises(SileroVadError) as ctx:
            detector.reset()
        self.assertEqual(str(ctx.exception), INIT_FAILED)
        self.assertEqual(vad.reset_calls, 1)


class LifecycleTests(unittest.TestCase):
    def test_reset_clears_residual_counters_and_calls_vad_reset(self) -> None:
        vad = FakeVad([True, True, True])
        detector, _vad = fake_detector(vad, speech_frames_required=1)
        detector.observe(make_packet(payload=window_payload(100, samples=320), sequence=1))
        detector.observe(make_packet(payload=window_payload(100, samples=320), sequence=2))
        self.assertTrue(detector.speech_started)
        self.assertEqual(detector.observed_frames, 2)
        detector.reset()
        self.assertEqual(vad.reset_calls, 1)
        self.assertEqual(detector.observed_frames, 0)
        self.assertEqual(detector.consecutive_speech, 0)
        self.assertFalse(detector.speech_started)

    def test_config_property_is_the_frozen_config(self) -> None:
        config = SileroVadConfig(model_path=MISSING_MODEL_PATH)
        detector = SileroVadDetector(config=config, vad=FakeVad())
        self.assertIs(detector.config, config)
        self.assertIs(detector.model_path, MISSING_MODEL_PATH)

    def test_details_are_mechanical_facts_only(self) -> None:
        detector, _vad = fake_detector()
        details = detector.to_details()
        self.assertEqual(details["mode"], "silero")
        self.assertEqual(details["model_name"], MISSING_MODEL_PATH.name)
        self.assertIs(details["model_present"], False)
        self.assertEqual(details["threshold"], 0.5)
        self.assertEqual(details["window_samples"], SILERO_WINDOW_SAMPLES)
        self.assertEqual(details["sample_rate_hz"], 16000)
        self.assertEqual(details["speech_frames_required"], 2)
        self.assertIs(details["injected"], True)
        for key in ("payload", "audio", "samples", "pcm"):
            self.assertNotIn(key, details)

    def test_factory_passes_the_config_through(self) -> None:
        config = SileroVadConfig(model_path=MISSING_MODEL_PATH)
        with mock.patch.object(silero, "SileroVadDetector") as detector_cls:
            build_silero_vad_detector(config=config)
        detector_cls.assert_called_once_with(config=config)


@unittest.skipUnless(
    MODEL_PATH.is_file() and HAS_SHERPA,
    "需要项目内 models/openwakeword/silero_vad.onnx 与 sherpa_onnx 才能做真机冒烟",
)
class RealModelSmokeTests(unittest.TestCase):
    """只读加载真机模型：纯静音与 440Hz 音调都不判为人声（不打开任何设备）。"""

    def test_default_model_exists_and_loads(self) -> None:
        detector = build_silero_vad_detector()
        self.assertFalse(detector.injected)
        self.assertEqual(detector.model_path, DEFAULT_SILERO_MODEL_PATH)
        details = detector.to_details()
        self.assertIs(details["model_present"], True)
        self.assertEqual(details["model_name"], "silero_vad.onnx")

    def test_pure_silence_is_not_speech(self) -> None:
        detector = build_silero_vad_detector()
        hits = [
            detector.observe(make_packet(payload=window_payload(0), sequence=index))
            for index in range(1, 17)
        ]
        self.assertEqual(hits, [False] * 16)
        self.assertFalse(detector.speech_started)
        self.assertEqual(detector.observed_frames, 16)

    def test_constant_tone_is_not_speech(self) -> None:
        detector = build_silero_vad_detector()
        tone = [
            0.3 * math.sin(2.0 * math.pi * 440.0 * index / 16000.0)
            for index in range(SILERO_WINDOW_SAMPLES)
        ]
        payload = struct.pack(f"<{len(tone)}h", *[int(value * 32767) for value in tone])
        self.assertFalse(detector.observe(make_packet(payload=payload)))
        self.assertFalse(detector.speech_started)

    def test_reset_keeps_the_real_vad_usable(self) -> None:
        detector = build_silero_vad_detector()
        detector.observe(make_packet(payload=window_payload(0)))
        detector.reset()
        self.assertEqual(detector.observed_frames, 0)
        self.assertFalse(detector.observe(make_packet(payload=window_payload(0))))


@unittest.skipUnless(
    MODEL_PATH.is_file() and HAS_SHERPA and SPEECH_WAV_PATH.is_file(),
    "需要 Silero 模型、sherpa_onnx 与仓库内语音片段才能做真人声冒烟",
)
class RealSpeechSmokeTests(unittest.TestCase):
    """仓库内 24kHz 语音片段重采样到 16kHz：真人声判为人声，其后静音恢复为未命中。"""

    def _speech_packets(self) -> list[AudioPacket]:
        import wave

        with wave.open(str(SPEECH_WAV_PATH), "rb") as stream:
            channels = stream.getnchannels()
            width = stream.getsampwidth()
            rate = stream.getframerate()
            frames = stream.getnframes()
            raw = stream.readframes(frames)
        self.assertEqual(channels, 1)
        self.assertEqual(width, 2)
        samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
        # 线性重采样到 16kHz（时长不变，只用于 VAD 冒烟）。
        step = rate / 16000.0
        resampled: list[int] = []
        position = 0.0
        while int(position) < len(samples):
            resampled.append(samples[int(position)])
            position += step
        packets: list[AudioPacket] = []
        for index, start in enumerate(
            range(0, len(resampled) - SILERO_WINDOW_SAMPLES + 1, SILERO_WINDOW_SAMPLES)
        ):
            window = resampled[start : start + SILERO_WINDOW_SAMPLES]
            payload = struct.pack(f"<{len(window)}h", *window)
            packets.append(
                make_packet(
                    payload=payload,
                    sequence=index + 1,
                    duration_ms=32,
                    sample_count=SILERO_WINDOW_SAMPLES,
                    payload_size_bytes=len(payload),
                )
            )
        return packets

    def test_real_speech_is_detected(self) -> None:
        detector = build_silero_vad_detector()
        packets = self._speech_packets()
        self.assertGreater(len(packets), 10)
        hits = sum(1 for packet in packets if detector.observe(packet))
        self.assertGreater(hits, 0)
        self.assertTrue(detector.speech_started)

    def test_silence_after_real_speech_clears_the_hit(self) -> None:
        detector = build_silero_vad_detector()
        for packet in self._speech_packets():
            detector.observe(packet)
        self.assertTrue(detector.speech_started)
        silence = make_packet(
            payload=window_payload(0),
            duration_ms=32,
            sample_count=SILERO_WINDOW_SAMPLES,
            payload_size_bytes=SILERO_WINDOW_SAMPLES * 2,
        )
        results = [detector.observe(silence) for _ in range(40)]
        self.assertFalse(results[-1])
        self.assertIn(False, results)


class _SegmentFakeVad(FakeVad):
    """既有 ``FakeVad`` 上补 ``empty()``/``flush()``，模拟 sherpa-onnx 1.13.8 的段级 API。

    ``empty()`` 返回 True 表示"没有已结束的人声段"，因此 ``segment_pending()`` 等价于取反。
    """

    def __init__(
        self,
        *,
        empty: object = True,
        empty_error: Exception | None = None,
        flush_error: Exception | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.empty_value = empty
        self.empty_error = empty_error
        self.flush_error = flush_error
        self.empty_calls = 0
        self.flush_calls = 0

    def empty(self) -> object:
        self.empty_calls += 1
        if self.empty_error is not None:
            raise self.empty_error
        return self.empty_value

    def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_error is not None:
            raise self.flush_error


def load_speech_packets() -> list[AudioPacket]:
    """把仓库内 24kHz 单声道语音片段线性重采样到 16kHz 并切成 512 样本整窗。"""

    import wave

    with wave.open(str(SPEECH_WAV_PATH), "rb") as stream:
        channels = stream.getnchannels()
        width = stream.getsampwidth()
        rate = stream.getframerate()
        raw = stream.readframes(stream.getnframes())
    if channels != 1 or width != 2:
        raise AssertionError("语音片段必须是 16bit 单声道")
    samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
    step = rate / 16000.0
    resampled: list[int] = []
    position = 0.0
    while int(position) < len(samples):
        resampled.append(samples[int(position)])
        position += step
    packets: list[AudioPacket] = []
    for index, start in enumerate(
        range(0, len(resampled) - SILERO_WINDOW_SAMPLES + 1, SILERO_WINDOW_SAMPLES)
    ):
        window = resampled[start : start + SILERO_WINDOW_SAMPLES]
        payload = struct.pack(f"<{len(window)}h", *window)
        packets.append(
            make_packet(
                payload=payload,
                sequence=index + 1,
                duration_ms=32,
                sample_count=SILERO_WINDOW_SAMPLES,
                payload_size_bytes=len(payload),
            )
        )
    return packets


class SegmentEndpointTests(unittest.TestCase):
    """段级事实（``empty()`` / ``flush()``）的纯增量契约；注入替身，离线。"""

    def test_legacy_fake_without_segment_api_is_safe_and_silent(self) -> None:
        detector, _vad = fake_detector(FakeVad([True]))
        self.assertFalse(detector.segment_api_available)
        self.assertFalse(detector.segment_pending())
        detector.flush_segment()
        self.assertEqual(detector.segment_probe_errors, 0)
        self.assertEqual(detector.flush_calls, 0)

    def test_finished_segment_is_reported_as_pending(self) -> None:
        detector, vad = fake_detector(_SegmentFakeVad(empty=False))
        self.assertTrue(detector.segment_api_available)
        self.assertTrue(detector.segment_pending())
        self.assertEqual(vad.empty_calls, 1)

    def test_no_finished_segment_is_not_pending(self) -> None:
        detector, _vad = fake_detector(_SegmentFakeVad(empty=True))
        self.assertFalse(detector.segment_pending())
        self.assertEqual(detector.segment_probe_errors, 0)

    def test_segment_probe_failure_is_counted_not_raised(self) -> None:
        detector, _vad = fake_detector(
            _SegmentFakeVad(empty_error=RuntimeError("C:\\secret\\mic.pcm raw audio"))
        )
        self.assertFalse(detector.segment_pending())
        self.assertEqual(detector.segment_probe_errors, 1)

    def test_non_boolean_empty_result_is_counted_not_used(self) -> None:
        detector, _vad = fake_detector(_SegmentFakeVad(empty="no"))
        self.assertFalse(detector.segment_pending())
        self.assertEqual(detector.segment_probe_errors, 1)

    def test_flush_forwards_to_underlying_vad_every_call(self) -> None:
        detector, vad = fake_detector(_SegmentFakeVad())
        detector.flush_segment()
        detector.flush_segment()
        self.assertEqual(vad.flush_calls, 2)
        self.assertEqual(detector.flush_calls, 2)
        self.assertEqual(detector.segment_probe_errors, 0)

    def test_flush_failure_is_counted_not_raised(self) -> None:
        detector, _vad = fake_detector(_SegmentFakeVad(flush_error=RuntimeError("flush boom")))
        detector.flush_segment()
        self.assertEqual(detector.flush_calls, 0)
        self.assertEqual(detector.segment_probe_errors, 1)

    def test_to_details_exposes_segment_facts_without_any_content(self) -> None:
        detector, _vad = fake_detector(_SegmentFakeVad(empty=False))
        detector.segment_pending()
        detector.flush_segment()
        details = detector.to_details()
        self.assertTrue(details["segment_api_available"])
        self.assertEqual(details["flush_calls"], 1)
        self.assertEqual(details["segment_probe_errors"], 0)
        self.assertNotIn("C:\\", repr(details))

    def test_reset_does_not_clear_lifetime_segment_counters(self) -> None:
        detector, _vad = fake_detector(_SegmentFakeVad())
        detector.flush_segment()
        detector.reset()
        self.assertEqual(detector.flush_calls, 1)


@unittest.skipUnless(
    MODEL_PATH.is_file() and HAS_SHERPA and SPEECH_WAV_PATH.is_file(),
    "需要 Silero 模型、sherpa_onnx 与仓库内语音片段才能做段级冒烟",
)
class RealSpeechSegmentTests(unittest.TestCase):
    """真机模型只读冒烟：尾巴必须能进入"已结束段"，句尾静音也必须能自己产出段。"""

    def test_flush_finalizes_the_tail_segment(self) -> None:
        detector = build_silero_vad_detector()
        for packet in load_speech_packets():
            detector.observe(packet)
        detector.flush_segment()
        self.assertTrue(detector.segment_pending())

    def test_trailing_silence_finishes_the_segment_without_flush(self) -> None:
        detector = build_silero_vad_detector()
        for packet in load_speech_packets():
            detector.observe(packet)
        silence = make_packet(
            payload=window_payload(0),
            duration_ms=32,
            sample_count=SILERO_WINDOW_SAMPLES,
            payload_size_bytes=SILERO_WINDOW_SAMPLES * 2,
        )
        for _ in range(60):  # 60 * 32ms ≈ 1.9s，足以覆盖 min_silence_duration=0.5s
            if detector.segment_pending():
                break
            detector.observe(silence)
        self.assertTrue(detector.segment_pending())


class ModulePurityAuditTests(unittest.TestCase):
    ALLOWED_IMPORT_ROOTS = {"__future__", "math", "struct", "dataclasses", "pathlib", "app"}
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
        "shutil",
        "tempfile",
        "time",
        "random",
        "sounddevice",
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

    def test_module_level_imports_are_stdlib_bus_contracts_only(self) -> None:
        imported: set[str] = set()
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {name.split(".")[0] for name in imported}
        self.assertEqual(roots - self.ALLOWED_IMPORT_ROOTS, set())
        self.assertEqual(roots & self.BANNED_IMPORT_ROOTS, set())
        self.assertNotIn("sherpa_onnx", roots)
        for name in imported:
            if name.startswith("app."):
                self.assertIn(name, {"app.audio.bus", "app.contracts.base"})

    def test_sherpa_import_is_deferred_into_a_function(self) -> None:
        self.assertIn("import sherpa_onnx", self.source)
        holders: list[str] = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for child in ast.walk(node):
                    if isinstance(child, ast.Import) and any(
                        alias.name.split(".")[0] == "sherpa_onnx" for alias in child.names
                    ):
                        holders.append(node.name)
        self.assertEqual(holders, ["_build_sherpa_vad"])

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

    def test_module_has_no_file_write_or_full_read_access(self) -> None:
        self.assertIn("is_file", self.source)
        for forbidden in ("read_text", "read_bytes", "write_text", "write_bytes", "open("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)
        for forbidden in ("subprocess", "socket", "threading", "sounddevice"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_module_is_utf8_without_replacement_characters(self) -> None:
        raw = MODULE_PATH.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn("\ufffd", self.source)
        raw.decode("utf-8")


if __name__ == "__main__":
    unittest.main()
