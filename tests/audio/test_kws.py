"""P9 B3 契约测试：唤醒词门禁（``app/audio/kws.py``，拒绝路径优先）。

覆盖：配置校验、错误码收敛（``str()`` 只有固定短代码）、模型缺失 / 依赖缺失 / 构造失败、
注入替身的接口要求、逐帧喂入与在线流推进（含单帧解码步数上限）、命中/未命中/多步解码、
异常与非字符串结果的 fail closed、帧契约拒绝、``to_details()`` 只含机械事实，以及模块
执行面审计（无设备/文件写/网络/线程/安全链，``sherpa_onnx`` 只在构造时延迟导入）。

真实 KWS 模型不在仓库内（需单独下载），因此这里**全部**注入替身；真实模型只在
``skipUnless`` 冒烟里只读加载。
"""

from __future__ import annotations

import ast
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.audio.bus import AudioPacket
from app.audio import kws
from app.audio.kws import (
    DEPENDENCY_MISSING,
    DETECT_FAILED,
    INIT_FAILED,
    MODEL_MISSING,
    UNSUPPORTED_FORMAT,
    UNSUPPORTED_SAMPLE_RATE,
    KeywordGate,
    KeywordGateConfig,
    KeywordGateError,
)
from app.contracts.base import ContractError
from tests.audio.test_silero_vad import frame_view, make_packet, window_payload

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "app" / "audio" / "kws.py"
MISSING_MODEL_DIR = REPO_ROOT / "models" / "_not_present_kws"
SAMPLE_COUNT = 320
#: 已下载的中文 KWS 模型（`models/kws`，冻结布局）+ 官方自带测试音频。
KWS_MODEL_DIR = REPO_ROOT / "models" / "kws"
KWS_SAMPLE_DIR = KWS_MODEL_DIR / "test_wavs"
_KWS_FILES_PRESENT = all((KWS_MODEL_DIR / name).is_file() for name in kws.KWS_MODEL_FILES)


def packet(payload: bytes | None = None, sequence: int = 1, **overrides: object) -> AudioPacket:
    if payload is None:
        payload = window_payload(0, samples=SAMPLE_COUNT)
    return make_packet(payload=payload, sequence=sequence, **overrides)


class FakeSpotter:
    """``KeywordSpotter`` 替身：记录调用序列，按帧返回预设结果。"""

    def __init__(
        self,
        *,
        results: object = (),
        ready_steps: int = 1,
        create_error: Exception | None = None,
        decode_error: Exception | None = None,
        result_error: Exception | None = None,
        reset_error: Exception | None = None,
        accept_error: Exception | None = None,
        non_str_result: bool = False,
        non_bool_ready: bool = False,
        endless_ready: bool = False,
    ) -> None:
        self.results = list(results)  # type: ignore[arg-type]
        self.ready_steps = ready_steps
        self.create_error = create_error
        self.decode_error = decode_error
        self.result_error = result_error
        self.reset_error = reset_error
        self.accept_error = accept_error
        self.non_str_result = non_str_result
        self.non_bool_ready = non_bool_ready
        self.endless_ready = endless_ready
        self.calls: list[str] = []
        self.streams: list[object] = []
        self.waveforms: list[tuple[float, list[float]]] = []
        self._steps: dict[int, int] = {}
        self._results: dict[int, int] = {}

    def create_stream(self) -> object:
        self.calls.append("create_stream")
        if self.create_error is not None:
            raise self.create_error
        stream = SimpleNamespace(
            accept_waveform=self._accept,
            input_finished=lambda: self.calls.append("input_finished"),
        )
        self.streams.append(stream)
        return stream

    def _accept(self, sample_rate: float, samples: list[float]) -> None:
        self.calls.append("accept_waveform")
        if self.accept_error is not None:
            raise self.accept_error
        self.waveforms.append((sample_rate, list(samples)))

    def is_ready(self, stream: object) -> object:
        if self.non_bool_ready:
            return "yes"
        if self.endless_ready:
            return True
        key = id(stream)
        steps = self._steps.get(key, 0)
        if steps < self.ready_steps:
            self._steps[key] = steps + 1
            return True
        return False

    def decode_stream(self, stream: object) -> None:
        self.calls.append("decode_stream")
        if self.decode_error is not None:
            raise self.decode_error

    def get_result(self, stream: object) -> object:
        self.calls.append("get_result")
        if self.result_error is not None:
            raise self.result_error
        if self.non_str_result:
            return 42
        key = id(stream)
        index = self._results.get(key, 0)
        self._results[key] = index + 1
        if index < len(self.results):
            return self.results[index]
        return ""

    def reset_stream(self, stream: object) -> None:
        self.calls.append("reset_stream")
        if self.reset_error is not None:
            raise self.reset_error


def gate(spotter: FakeSpotter, **config_overrides: object) -> KeywordGate:
    return KeywordGate(config=KeywordGateConfig(**config_overrides), spotter=spotter)  # type: ignore[arg-type]


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_frozen_contract(self) -> None:
        config = KeywordGateConfig()
        self.assertEqual(config.sample_rate_hz, 16000)
        self.assertEqual(config.feature_dim, 80)
        self.assertEqual(config.num_threads, 1)
        self.assertEqual(config.keywords_threshold, 0.25)
        self.assertIsNone(config.keyword)
        self.assertIsNone(config.model_dir)

    def test_resolved_paths_prefer_explicit_files_over_model_dir(self) -> None:
        config = KeywordGateConfig(
            model_dir=Path("m"), tokens_path=Path("custom/tokens.txt"), keywords_file=Path("k.txt")
        )
        paths = config.resolved_paths()
        self.assertEqual(paths["tokens.txt"], Path("custom/tokens.txt"))
        self.assertEqual(paths["encoder.onnx"], Path("m/encoder.onnx"))
        self.assertEqual(paths["keywords.txt"], Path("k.txt"))

    def test_keywords_file_is_auto_discovered_under_model_dir(self) -> None:
        """模型目录里有 keywords.txt 就自动采用（真机踩过：没传 keywords_file → 一上手就退出）。"""

        config = KeywordGateConfig(model_dir=Path("m"))
        with mock.patch.object(Path, "is_file", return_value=True):
            paths = config.resolved_paths()
        self.assertEqual(paths["keywords.txt"], Path("m/keywords.txt"))

    def test_no_keywords_file_anywhere_reports_a_dedicated_code(self) -> None:
        config = KeywordGateConfig(
            tokens_path=Path("t"), encoder_path=Path("e"), decoder_path=Path("d"), joiner_path=Path("j")
        )
        with mock.patch.object(Path, "is_file", return_value=True):
            with self.assertRaises(KeywordGateError) as caught:
                kws._build_spotter(config, lambda: object())  # noqa: SLF001
        self.assertEqual(str(caught.exception), kws.KEYWORDS_MISSING)

    def test_rejects_invalid_values(self) -> None:
        cases = (
            {"sample_rate_hz": 0},
            {"sample_rate_hz": True},
            {"feature_dim": -1},
            {"num_threads": 0},
            {"max_active_paths": 0},
            {"num_trailing_blanks": -1},
            {"keywords_threshold": -0.5},
            {"keywords_score": "high"},
            {"keyword": "   "},
            {"provider": ""},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    KeywordGateConfig(**kwargs)  # type: ignore[arg-type]


class ConstructionTests(unittest.TestCase):
    def test_missing_model_files_fail_closed(self) -> None:
        with self.assertRaises(KeywordGateError) as caught:
            KeywordGate(config=KeywordGateConfig(model_dir=MISSING_MODEL_DIR))
        self.assertEqual(str(caught.exception), MODEL_MISSING)

    def test_dependency_missing_is_reported(self) -> None:
        def broken_loader() -> object:
            raise ImportError("no module named sherpa_onnx")

        with self.assertRaises(KeywordGateError) as caught:
            _build_with_files(module_loader=broken_loader)
        self.assertEqual(str(caught.exception), DEPENDENCY_MISSING)

    def test_init_failure_is_reported(self) -> None:
        def loader() -> object:
            class Module:
                def KeywordSpotter(self, **kwargs: object) -> object:  # noqa: N802
                    raise RuntimeError("secret path G:\\private\\kws.onnx")

            return Module()

        with self.assertRaises(KeywordGateError) as caught:
            _build_with_files(module_loader=loader)
        self.assertEqual(str(caught.exception), INIT_FAILED)

    def test_injected_spotter_must_provide_the_frozen_methods(self) -> None:
        class OnlyCreate:
            def create_stream(self) -> object:
                return object()

        with self.assertRaises(ContractError):
            KeywordGate(config=KeywordGateConfig(), spotter=OnlyCreate())

    def test_injected_spotter_config_type_is_checked(self) -> None:
        with self.assertRaises(ContractError):
            KeywordGate(config=object(), spotter=FakeSpotter())  # type: ignore[arg-type]

    def test_build_passes_frozen_kwargs_to_the_module(self) -> None:
        calls: list[dict[str, object]] = []

        class Spotter:
            def __init__(self, **kwargs: object) -> None:
                calls.append(kwargs)

        class Module:
            KeywordSpotter = Spotter

        _build_with_files(module_loader=lambda: Module())
        self.assertEqual(len(calls), 1)
        kwargs = calls[0]
        self.assertEqual(kwargs["num_threads"], 3)
        self.assertEqual(kwargs["sample_rate"], 16000)
        self.assertEqual(kwargs["feature_dim"], 80)
        self.assertEqual(kwargs["keywords_threshold"], 0.4)
        self.assertEqual(kwargs["provider"], "cpu")
        self.assertTrue(str(kwargs["tokens"]).endswith("tokens.txt"))
        self.assertTrue(str(kwargs["keywords_file"]).endswith("keywords.txt"))


def _build_with_files(*, module_loader: object) -> object:
    """用**临时存在的假文件**驱动 ``_build_spotter``（只读检查通过，构造可控）。"""

    with mock.patch.object(Path, "is_file", return_value=True):
        config = KeywordGateConfig(
            model_dir=Path("m"),
            keywords_file=Path("m/keywords.txt"),
            num_threads=3,
            keywords_threshold=0.4,
        )
        return kws._build_spotter(config, module_loader)  # noqa: SLF001


class ObserveTests(unittest.TestCase):
    def test_no_detection_returns_false(self) -> None:
        spotter = FakeSpotter(results=["", ""], ready_steps=2)
        gate_instance = gate(spotter, keyword="小饼")
        self.assertFalse(gate_instance.observe(packet()))
        self.assertEqual(gate_instance.detected_count, 0)
        self.assertEqual(gate_instance.decoded_steps, 2)

    def test_matching_keyword_is_detected_and_stream_reset(self) -> None:
        spotter = FakeSpotter(results=["小饼"], ready_steps=1)
        gate_instance = gate(spotter, keyword="小饼")
        self.assertTrue(gate_instance.observe(packet()))
        self.assertEqual(gate_instance.detected_count, 1)
        self.assertIn("reset_stream", spotter.calls)
        self.assertIn("accept_waveform", spotter.calls)

    def test_text_normalisation_ignores_spaces_and_case(self) -> None:
        spotter = FakeSpotter(results=[" 小 饼 "], ready_steps=1)
        gate_instance = gate(spotter, keyword="小饼")
        self.assertTrue(gate_instance.observe(packet()))

    def test_non_matching_result_is_not_a_hit_but_does_not_reset(self) -> None:
        spotter = FakeSpotter(results=["你好"], ready_steps=1)
        gate_instance = gate(spotter, keyword="小饼")
        self.assertFalse(gate_instance.observe(packet()))
        self.assertNotIn("reset_stream", spotter.calls)

    def test_any_non_empty_result_counts_when_no_keyword_configured(self) -> None:
        spotter = FakeSpotter(results=["随便什么"], ready_steps=1)
        gate_instance = gate(spotter)
        self.assertTrue(gate_instance.observe(packet()))
        self.assertEqual(gate_instance.keyword, None)

    def test_reset_opens_a_fresh_stream(self) -> None:
        spotter = FakeSpotter(results=[], ready_steps=0)
        gate_instance = gate(spotter)
        self.assertEqual(len(spotter.streams), 1)
        gate_instance.reset()
        self.assertEqual(len(spotter.streams), 2)

    def test_frame_is_converted_to_normalised_floats(self) -> None:
        spotter = FakeSpotter(results=[], ready_steps=0)
        gate_instance = gate(spotter)
        gate_instance.observe(packet(struct.pack("<2h", 16384, -32768)))
        rate, samples = spotter.waveforms[0]
        self.assertEqual(rate, 16000)
        self.assertEqual(len(samples), 2)
        self.assertAlmostEqual(samples[0], 0.5)
        self.assertAlmostEqual(samples[1], -1.0)


class FailClosedTests(unittest.TestCase):
    def test_bad_payloads_are_rejected_without_being_swallowed(self) -> None:
        gate_instance = gate(FakeSpotter())
        with self.assertRaises(TypeError):
            gate_instance.observe("not a packet")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            gate_instance.observe(AudioPacket(frame_view(), "text"))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            gate_instance.observe(AudioPacket(frame_view(), b"\x00"))

    def test_unsupported_frame_contract_is_reported(self) -> None:
        gate_instance = gate(FakeSpotter())
        with self.assertRaises(KeywordGateError) as rate_error:
            gate_instance.observe(AudioPacket(frame_view(sample_rate_hz=8000), b"\x00\x00"))  # type: ignore[arg-type]
        self.assertEqual(str(rate_error.exception), UNSUPPORTED_SAMPLE_RATE)
        with self.assertRaises(KeywordGateError) as format_error:
            gate_instance.observe(AudioPacket(frame_view(channel_count=2), b"\x00\x00"))  # type: ignore[arg-type]
        self.assertEqual(str(format_error.exception), UNSUPPORTED_FORMAT)

    def test_accept_waveform_failure_is_detect_failed(self) -> None:
        spotter = FakeSpotter(accept_error=RuntimeError("secret pcm G:\\private\\a.pcm"))
        with self.assertRaises(KeywordGateError) as caught:
            gate(spotter).observe(packet())
        self.assertEqual(str(caught.exception), DETECT_FAILED)

    def test_decode_failure_is_detect_failed(self) -> None:
        spotter = FakeSpotter(ready_steps=1, decode_error=RuntimeError("boom"))
        with self.assertRaises(KeywordGateError) as caught:
            gate(spotter).observe(packet())
        self.assertEqual(str(caught.exception), DETECT_FAILED)

    def test_get_result_failure_and_non_string_are_detect_failed(self) -> None:
        for spotter in (
            FakeSpotter(ready_steps=1, result_error=RuntimeError("boom")),
            FakeSpotter(ready_steps=1, non_str_result=True),
        ):
            with self.subTest(spotter=spotter):
                with self.assertRaises(KeywordGateError) as caught:
                    gate(spotter).observe(packet())
                self.assertEqual(str(caught.exception), DETECT_FAILED)

    def test_non_boolean_ready_is_detect_failed(self) -> None:
        spotter = FakeSpotter(non_bool_ready=True)
        with self.assertRaises(KeywordGateError) as caught:
            gate(spotter).observe(packet())
        self.assertEqual(str(caught.exception), DETECT_FAILED)

    def test_endless_ready_stream_is_capped_instead_of_hanging(self) -> None:
        spotter = FakeSpotter(endless_ready=True, results=[""], ready_steps=0)
        with self.assertRaises(KeywordGateError) as caught:
            gate(spotter).observe(packet())
        self.assertEqual(str(caught.exception), DETECT_FAILED)
        self.assertEqual(spotter.calls.count("decode_stream"), kws.MAX_DECODE_STEPS_PER_FRAME)

    def test_reset_failure_is_detect_failed(self) -> None:
        spotter = FakeSpotter(results=["小饼"], ready_steps=1, reset_error=RuntimeError("boom"))
        with self.assertRaises(KeywordGateError) as caught:
            gate(spotter, keyword="小饼").observe(packet())
        self.assertEqual(str(caught.exception), DETECT_FAILED)

    def test_create_stream_failure_is_init_failed(self) -> None:
        spotter = FakeSpotter(create_error=RuntimeError("boom"))
        with self.assertRaises(KeywordGateError) as caught:
            gate(spotter)
        self.assertEqual(str(caught.exception), INIT_FAILED)

    def test_error_codes_never_leak_details(self) -> None:
        error = KeywordGateError("C:\\secret\\kws.onnx boom")
        self.assertEqual(str(error), INIT_FAILED)
        self.assertEqual(error.code, INIT_FAILED)


class DetailsTests(unittest.TestCase):
    def test_details_only_contain_mechanical_facts(self) -> None:
        spotter = FakeSpotter()
        details = gate(spotter, keyword="小饼").to_details()
        self.assertEqual(details["mode"], "keyword")
        self.assertEqual(details["keyword"], "小饼")
        self.assertEqual(details["sample_rate_hz"], 16000)
        self.assertTrue(details["injected"])  # 注入替身
        self.assertNotIn("C:\\", repr(details))

    def test_injected_gate_reports_injected_true(self) -> None:
        self.assertTrue(gate(FakeSpotter()).injected)


@unittest.skipUnless(
    _KWS_FILES_PRESENT and (KWS_SAMPLE_DIR / "3.wav").is_file() and (KWS_MODEL_DIR / "keywords.txt").is_file(),
    "需要已下载的中文 KWS 模型（models/kws：tokens/encoder/decoder/joiner + keywords.txt + test_wavs）",
)
class RealKwsModelTests(unittest.TestCase):
    """真实模型只读冒烟：逐帧流式喂入必须能检出官方样例里的关键词。

    这些用例只在模型已下载时运行；模型不在仓库内（`.gitignore`），因此默认会被跳过。
    """

    WINDOW = 512

    def _gate(self, keywords_file: Path, keyword: str | None) -> KeywordGate:
        return KeywordGate(
            config=KeywordGateConfig(
                model_dir=KWS_MODEL_DIR, keywords_file=keywords_file, keyword=keyword
            )
        )

    def _packets(self, name: str) -> list[AudioPacket]:
        import wave

        with wave.open(str(KWS_SAMPLE_DIR / name), "rb") as stream:
            self.assertEqual(stream.getframerate(), 16000)
            self.assertEqual(stream.getnchannels(), 1)
            self.assertEqual(stream.getsampwidth(), 2)
            raw = stream.readframes(stream.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
        packets: list[AudioPacket] = []
        step = self.WINDOW
        for index, start in enumerate(range(0, len(samples) - step + 1, step)):
            window = samples[start : start + step]
            payload = struct.pack(f"<{step}h", *window)
            packets.append(
                make_packet(
                    payload=payload,
                    sequence=index + 1,
                    duration_ms=32,  # 512 样本 @16kHz
                    sample_count=step,
                    payload_size_bytes=len(payload),
                )
            )
        return packets

    def test_streaming_detection_on_the_shipped_sample(self) -> None:
        # 测试音频 3.wav 含「文森特卡索/法国」，用官方 keyword 表 + 任意命中口径。
        gate = self._gate(KWS_SAMPLE_DIR / "test_keywords.txt", None)
        hit = False
        for packet in self._packets("3.wav"):
            if gate.observe(packet):
                hit = True
                break
        self.assertTrue(hit)
        self.assertEqual(gate.detected_count, 1)
        self.assertGreater(gate.decoded_steps, 0)
        self.assertFalse(gate.injected)
        self.assertTrue(gate.to_details()["model_present"])

    def test_generated_wake_word_file_loads_and_does_not_misfire(self) -> None:
        # 「小饼」关键词文件（x iǎo b ǐng @小饼）必须能被解析；他人音频不得误触发。
        gate = self._gate(KWS_MODEL_DIR / "keywords.txt", "小饼")
        self.assertFalse(any(gate.observe(packet) for packet in self._packets("3.wav")))
        self.assertEqual(gate.detected_count, 0)

    def test_silence_never_triggers_the_wake_word(self) -> None:
        gate = self._gate(KWS_MODEL_DIR / "keywords.txt", "小饼")
        silence = bytes(self.WINDOW * 2)
        hit = False
        for index in range(50):
            packet = make_packet(
                payload=silence,
                sequence=index + 1,
                duration_ms=32,
                sample_count=self.WINDOW,
                payload_size_bytes=len(silence),
            )
            if gate.observe(packet):
                hit = True
                break
        self.assertFalse(hit)
        self.assertEqual(gate.detected_count, 0)


class ModulePurityAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MODULE_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_module_is_utf8_without_replacement_characters(self) -> None:
        raw = MODULE_PATH.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn("\ufffd", self.source)
        raw.decode("utf-8")

    def test_no_device_file_write_network_or_threads(self) -> None:
        for forbidden in (
            "sounddevice",
            "import wave",
            "open(",
            "write_text",
            "write_bytes",
            "socket",
            "threading",
            "subprocess",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)
        self.assertIn("is_file", self.source)

    def test_sherpa_onnx_is_imported_lazily_and_no_safety_chain(self) -> None:
        top_level = {
            node.names[0].name.split(".")[0]
            for node in self.tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        }
        self.assertNotIn("sherpa_onnx", top_level)
        self.assertIn("import sherpa_onnx", self.source)
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


if __name__ == "__main__":
    unittest.main()
