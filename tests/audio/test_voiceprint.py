"""P9 B4 契约测试：声纹误触发过滤（``app/audio/voiceprint.py``，拒绝路径优先）。

覆盖：配置校验、错误码收敛、模型缺失 / 依赖缺失 / 构造失败、注入替身接口要求、
注册与判定（命中 / 低于阈值 / 未注册 / 音频过短）、嵌入形状校验、计算失败 fail closed、
``to_details()`` 只含机械事实，以及模块执行面审计（无设备/文件写/网络/线程/安全链）。

真实声纹模型（onnx）不在仓库内，因此这里**全部**注入替身。
"""

from __future__ import annotations

import ast
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.audio import voiceprint
from app.audio.voiceprint import (
    BELOW_THRESHOLD,
    COMPUTE_FAILED,
    DEPENDENCY_MISSING,
    INIT_FAILED,
    MATCHED,
    MODEL_MISSING,
    NOT_ENROLLED,
    TOO_SHORT,
    VoiceprintConfig,
    VoiceprintError,
    VoiceprintFilter,
)
from app.contracts.base import ContractError

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "app" / "audio" / "voiceprint.py"
MISSING_MODEL = REPO_ROOT / "models" / "_not_present_voiceprint.onnx"
#: 已下载的中文声纹模型（`models/voiceprint`）与仓库内真实语音样例（`models/kws/test_wavs`）。
VOICEPRINT_MODEL = REPO_ROOT / "models" / "voiceprint" / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
SPEECH_SAMPLES = REPO_ROOT / "models" / "kws" / "test_wavs"


def pcm16(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


class FakeExtractor:
    """``SpeakerEmbeddingExtractor`` 替身：记录喂入的波形，返回预设嵌入。"""

    def __init__(
        self,
        *,
        embedding: object = (1.0, 0.0),
        ready: bool = True,
        create_error: Exception | None = None,
        compute_error: Exception | None = None,
        accept_error: Exception | None = None,
    ) -> None:
        self.embedding = embedding
        self.ready = ready
        self.create_error = create_error
        self.compute_error = compute_error
        self.accept_error = accept_error
        self.streams: list[SimpleNamespace] = []
        self.waveforms: list[tuple[int, list[float]]] = []
        self.compute_calls = 0

    def create_stream(self) -> SimpleNamespace:
        if self.create_error is not None:
            raise self.create_error
        stream = SimpleNamespace(
            accept_waveform=self._accept,
            input_finished=lambda: None,
        )
        self.streams.append(stream)
        return stream

    def _accept(self, sample_rate: int, samples: list[float]) -> None:
        if self.accept_error is not None:
            raise self.accept_error
        self.waveforms.append((sample_rate, list(samples)))

    def is_ready(self, stream: object) -> bool:
        return self.ready

    def compute(self, stream: object) -> object:
        self.compute_calls += 1
        if self.compute_error is not None:
            raise self.compute_error
        return list(self.embedding) if isinstance(self.embedding, (list, tuple)) else self.embedding


class FakeManager:
    """``SpeakerEmbeddingManager`` 替身。"""

    def __init__(self, *, score_value: float = 0.9, add_result: bool = True,
                 score_error: Exception | None = None, add_error: Exception | None = None) -> None:
        self.score_value = score_value
        self.add_result = add_result
        self.score_error = score_error
        self.add_error = add_error
        self.registered: dict[str, list[float]] = {}
        self.score_calls = 0

    def add(self, name: str, v: object) -> bool:
        if self.add_error is not None:
            raise self.add_error
        if not self.add_result:
            return False
        self.registered[name] = list(v)  # type: ignore[arg-type]
        return True

    def score(self, name: str, v: object) -> float:
        self.score_calls += 1
        if self.score_error is not None:
            raise self.score_error
        return self.score_value

    def remove(self, name: str) -> bool:
        return self.registered.pop(name, None) is not None

    @property
    def all_speakers(self) -> list[str]:
        return sorted(self.registered)

    @property
    def num_speakers(self) -> int:
        return len(self.registered)


def filter_with(
    *,
    extractor: FakeExtractor | None = None,
    manager: FakeManager | None = None,
    config: VoiceprintConfig | None = None,
) -> tuple[VoiceprintFilter, FakeExtractor, FakeManager]:
    resolved_extractor = extractor if extractor is not None else FakeExtractor()
    resolved_manager = manager if manager is not None else FakeManager()
    resolved_config = config if config is not None else VoiceprintConfig()
    instance = VoiceprintFilter(
        config=resolved_config, extractor=resolved_extractor, manager=resolved_manager
    )
    return instance, resolved_extractor, resolved_manager


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_frozen_contract(self) -> None:
        config = VoiceprintConfig()
        self.assertEqual(config.threshold, 0.5)
        self.assertEqual(config.speaker_name, "owner")
        self.assertEqual(config.num_threads, 1)
        self.assertFalse(config.debug)
        self.assertIsNone(config.model_path)

    def test_rejects_invalid_values(self) -> None:
        for kwargs in (
            {"threshold": 0.0},
            {"threshold": 1.5},
            {"threshold": True},
            {"threshold": "0.5"},
            {"num_threads": 0},
            {"speaker_name": ""},
            {"provider": ""},
            {"debug": 1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    VoiceprintConfig(**kwargs)  # type: ignore[arg-type]

    def test_path_like_model_path_is_normalised(self) -> None:
        self.assertEqual(VoiceprintConfig(model_path="a/b.onnx").model_path, Path("a/b.onnx"))


class ConstructionTests(unittest.TestCase):
    def test_missing_model_file_fails_closed(self) -> None:
        with self.assertRaises(VoiceprintError) as caught:
            VoiceprintFilter(config=VoiceprintConfig(model_path=MISSING_MODEL))
        self.assertEqual(str(caught.exception), MODEL_MISSING)

    def test_dependency_missing_is_reported(self) -> None:
        def broken_loader() -> object:
            raise ImportError("no sherpa_onnx")

        with self.assertRaises(VoiceprintError) as caught:
            _build_with_fake_model(module_loader=broken_loader)
        self.assertEqual(str(caught.exception), DEPENDENCY_MISSING)

    def test_init_failure_is_reported(self) -> None:
        def broken_loader() -> object:
            class Module:
                def SpeakerEmbeddingExtractorConfig(self, **kwargs: object) -> object:  # noqa: N802
                    raise RuntimeError("secret G:\\private\\spk.onnx")

            return Module()

        with self.assertRaises(VoiceprintError) as caught:
            _build_with_fake_model(module_loader=broken_loader)
        self.assertEqual(str(caught.exception), INIT_FAILED)

    def test_build_passes_frozen_config(self) -> None:
        captured: dict[str, object] = {}

        class ExtractorConfig:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        class Extractor:
            dim = 192

            def __init__(self, config: object) -> None:
                captured["extractor_config"] = config

        class Manager:
            def __init__(self, dim: int) -> None:
                captured["dim"] = dim

        class Module:
            SpeakerEmbeddingExtractorConfig = ExtractorConfig
            SpeakerEmbeddingExtractor = Extractor
            SpeakerEmbeddingManager = Manager

        _build_with_fake_model(module_loader=lambda: Module(), num_threads=2, threshold=0.7)
        self.assertEqual(captured["num_threads"], 2)
        self.assertEqual(captured["provider"], "cpu")
        self.assertEqual(captured["dim"], 192)

    def test_injection_requires_both_objects_and_methods(self) -> None:
        with self.assertRaises(ContractError):
            VoiceprintFilter(config=VoiceprintConfig(), extractor=FakeExtractor())
        with self.assertRaises(ContractError):
            VoiceprintFilter(config=VoiceprintConfig(), manager=FakeManager())

        class OnlyCreate:
            def create_stream(self) -> object:
                return object()

        with self.assertRaises(ContractError):
            VoiceprintFilter(config=VoiceprintConfig(), extractor=OnlyCreate(), manager=FakeManager())

        class BadManager:
            def add(self, name: str, v: object) -> bool:
                return True

        with self.assertRaises(ContractError):
            VoiceprintFilter(config=VoiceprintConfig(), extractor=FakeExtractor(), manager=BadManager())


def _build_with_fake_model(*, module_loader: object, **config_overrides: object) -> object:
    """用临时存在的假模型文件驱动 ``_build_extractor``（只读检查通过，构造可控）。"""

    with mock.patch.object(Path, "is_file", return_value=True):
        config = VoiceprintConfig(model_path=Path("m/spk.onnx"), **config_overrides)  # type: ignore[arg-type]
        return voiceprint._build_extractor(config, module_loader)  # noqa: SLF001


class EnrollAndVerifyTests(unittest.TestCase):
    def test_enroll_registers_the_embedding_under_the_configured_name(self) -> None:
        instance, _extractor, manager = filter_with()
        self.assertTrue(instance.enroll([1.0, 0.0, 0.5]))
        self.assertEqual(list(manager.registered), ["owner"])
        self.assertTrue(instance.enrolled)
        self.assertEqual(instance.enrolled_speakers, ["owner"])

    def test_enroll_rejects_malformed_embeddings(self) -> None:
        instance, _extractor, _manager = filter_with()
        for bad in ([], (), "abc", b"\x00\x00", [1.0, "x"], [True, 1.0], 3.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    instance.enroll(bad)  # type: ignore[arg-type]

    def test_enroll_failure_is_fail_closed(self) -> None:
        instance, _extractor, _manager = filter_with(manager=FakeManager(add_error=RuntimeError("boom")))
        with self.assertRaises(VoiceprintError) as caught:
            instance.enroll([1.0, 0.0])
        self.assertEqual(str(caught.exception), COMPUTE_FAILED)

    def test_verify_without_enrolment_reports_not_enrolled(self) -> None:
        instance, _extractor, _manager = filter_with()
        decision = instance.verify_from_pcm(pcm16(1000, -1000))
        self.assertFalse(decision.matched)
        self.assertEqual(decision.reason, NOT_ENROLLED)
        self.assertEqual(instance.verified_count, 0)
        self.assertEqual(instance.rejected_count, 0)

    def test_verify_matches_when_score_reaches_the_threshold(self) -> None:
        instance, _extractor, manager = filter_with(manager=FakeManager(score_value=0.8))
        instance.enroll([1.0, 0.0])
        decision = instance.verify_from_pcm(pcm16(*([100] * 320)))
        self.assertTrue(decision.matched)
        self.assertEqual(decision.reason, MATCHED)
        self.assertAlmostEqual(decision.score, 0.8)
        self.assertEqual(instance.verified_count, 1)
        self.assertEqual(manager.score_calls, 1)

    def test_verify_rejects_below_the_threshold(self) -> None:
        instance, _extractor, _manager = filter_with(manager=FakeManager(score_value=0.2))
        instance.enroll([1.0, 0.0])
        decision = instance.verify_from_pcm(pcm16(*([100] * 320)))
        self.assertFalse(decision.matched)
        self.assertEqual(decision.reason, BELOW_THRESHOLD)
        self.assertEqual(instance.rejected_count, 1)
        self.assertEqual(instance.verified_count, 0)

    def test_short_audio_is_reported_without_computing(self) -> None:
        instance, extractor, _manager = filter_with(extractor=FakeExtractor(ready=False))
        instance.enroll([1.0, 0.0])
        decision = instance.verify_from_pcm(pcm16(1, 2, 3))
        self.assertFalse(decision.matched)
        self.assertEqual(decision.reason, TOO_SHORT)
        self.assertEqual(extractor.compute_calls, 0)

    def test_audio_is_converted_to_normalised_floats(self) -> None:
        instance, extractor, _manager = filter_with()
        instance.enroll([1.0, 0.0])
        instance.verify_from_pcm(pcm16(16384, -32768), sample_rate_hz=16000)
        rate, samples = extractor.waveforms[0]
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(samples[0], 0.5)
        self.assertAlmostEqual(samples[1], -1.0)

    def test_payload_contract_is_enforced(self) -> None:
        instance, _extractor, _manager = filter_with()
        instance.enroll([1.0, 0.0])
        with self.assertRaises(ContractError):
            instance.verify_from_pcm("not bytes")  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            instance.verify_from_pcm(b"\x00")
        with self.assertRaises(ContractError):
            instance.verify_from_pcm(pcm16(1), sample_rate_hz=0)

    def test_compute_failure_never_returns_matched(self) -> None:
        instance, _extractor, _manager = filter_with(extractor=FakeExtractor(compute_error=RuntimeError("boom")))
        instance.enroll([1.0, 0.0])
        with self.assertRaises(VoiceprintError) as caught:
            instance.verify_from_pcm(pcm16(*([10] * 320)))
        self.assertEqual(str(caught.exception), COMPUTE_FAILED)

    def test_accept_waveform_failure_is_compute_failed(self) -> None:
        instance, _extractor, _manager = filter_with(
            extractor=FakeExtractor(accept_error=RuntimeError("secret pcm G:\\private\\a.pcm"))
        )
        instance.enroll([1.0, 0.0])
        with self.assertRaises(VoiceprintError) as caught:
            instance.verify_from_pcm(pcm16(*([10] * 320)))
        self.assertEqual(str(caught.exception), COMPUTE_FAILED)

    def test_non_numeric_embedding_from_extractor_is_compute_failed(self) -> None:
        instance, _extractor, _manager = filter_with(extractor=FakeExtractor(embedding=("x", "y")))
        instance.enroll([1.0, 0.0])
        with self.assertRaises(ContractError):
            instance.verify_from_pcm(pcm16(*([10] * 320)))

    def test_score_failure_is_fail_closed(self) -> None:
        instance, _extractor, _manager = filter_with(
            manager=FakeManager(score_error=RuntimeError("boom"))
        )
        instance.enroll([1.0, 0.0])
        with self.assertRaises(VoiceprintError) as caught:
            instance.verify_from_pcm(pcm16(*([10] * 320)))
        self.assertEqual(str(caught.exception), COMPUTE_FAILED)

    def test_embed_from_pcm_returns_the_vector_without_enrolling(self) -> None:
        instance, extractor, manager = filter_with()
        embedding = instance.embed_from_pcm(pcm16(*([100] * 320)))
        self.assertEqual(embedding, [1.0, 0.0])
        self.assertEqual(manager.registered, {})  # 只算向量，不注册
        self.assertEqual(len(extractor.waveforms), 1)

    def test_embed_from_pcm_returns_none_when_audio_is_too_short(self) -> None:
        instance, _extractor, _manager = filter_with(extractor=FakeExtractor(ready=False))
        self.assertIsNone(instance.embed_from_pcm(pcm16(1, 2)))

    def test_enroll_from_pcm_uses_the_same_embedding_path(self) -> None:
        instance, extractor, manager = filter_with()
        self.assertTrue(instance.enroll_from_pcm(pcm16(*([100] * 320))))
        self.assertEqual(len(extractor.waveforms), 1)
        self.assertEqual(list(manager.registered), ["owner"])

    def test_enroll_from_pcm_returns_false_when_audio_is_too_short(self) -> None:
        instance, _extractor, manager = filter_with(extractor=FakeExtractor(ready=False))
        self.assertFalse(instance.enroll_from_pcm(pcm16(1, 2, 3)))
        self.assertEqual(manager.registered, {})


class DetailsTests(unittest.TestCase):
    def test_details_only_contain_mechanical_facts(self) -> None:
        instance, _extractor, _manager = filter_with()
        instance.enroll([1.0, 0.0])
        details = instance.to_details()
        self.assertEqual(details["mode"], "voiceprint")
        self.assertEqual(details["threshold"], 0.5)
        self.assertEqual(details["speaker_name"], "owner")
        self.assertTrue(details["enrolled"])
        self.assertEqual(details["enrolled_speakers"], ["owner"])
        self.assertTrue(details["injected"])
        self.assertNotIn("C:\\", repr(details))
        self.assertNotIn("embedding", repr(details))

    def test_decision_details_are_rounded_and_mechanical(self) -> None:
        from app.audio.voiceprint import VoiceprintDecision

        details = VoiceprintDecision(True, 0.12345678, MATCHED).to_details()
        self.assertEqual(details, {"matched": True, "score": 0.123457, "reason": "matched"})


@unittest.skipUnless(
    VOICEPRINT_MODEL.is_file() and (SPEECH_SAMPLES / "3.wav").is_file(),
    "需要已下载的声纹模型（models/voiceprint）与真实语音样例（models/kws/test_wavs）",
)
class RealVoiceprintModelTests(unittest.TestCase):
    """真实模型跨说话人负例：官方样例里 3/4 是同一人，0/1/2/5/6 明显不同。

    实测分离度（2026-10-01）：同人 0.83~1.00，他人 ≤0.31 → 默认阈值 0.5 落在空档里。
    """

    OTHERS = ("0.wav", "1.wav", "2.wav", "5.wav", "6.wav")

    def _pcm(self, name: str) -> bytes:
        import wave

        with wave.open(str(SPEECH_SAMPLES / name), "rb") as stream:
            self.assertEqual(stream.getframerate(), 16000)
            self.assertEqual(stream.getnchannels(), 1)
            self.assertEqual(stream.getsampwidth(), 2)
            return stream.readframes(stream.getnframes())

    def _owner_filter(self) -> "voiceprint.VoiceprintFilter":
        instance = VoiceprintFilter(config=VoiceprintConfig(model_path=VOICEPRINT_MODEL))
        self.assertTrue(instance.enroll_from_pcm(self._pcm("3.wav")))
        return instance

    def test_same_speaker_matches_and_other_speakers_are_rejected(self) -> None:
        instance = self._owner_filter()
        same = instance.verify_from_pcm(self._pcm("4.wav"))
        self.assertTrue(same.matched)
        self.assertGreater(same.score, 0.6)
        lowest_other = 1.0
        for name in self.OTHERS:
            decision = instance.verify_from_pcm(self._pcm(name))
            self.assertFalse(decision.matched, name)
            self.assertEqual(decision.reason, BELOW_THRESHOLD, name)
            lowest_other = min(lowest_other, decision.score)
        # 分离度：同人分数必须明显高于所有他人分数（默认阈值 0.5 位于空档）。
        self.assertGreater(same.score, lowest_other + 0.3)
        self.assertLess(lowest_other, 0.5)

    def test_enrolled_owner_verifies_itself(self) -> None:
        instance = self._owner_filter()
        decision = instance.verify_from_pcm(self._pcm("3.wav"))
        self.assertTrue(decision.matched)
        self.assertAlmostEqual(decision.score, 1.0, places=3)


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
            "json",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)
        self.assertIn("is_file", self.source)

    def test_sherpa_onnx_is_lazy_and_no_safety_chain(self) -> None:
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
