"""P9 B4 声纹注册工具的离线契约测试（``tools/p9_voiceprint_enroll.py``）。

覆盖：无 ``--live-opt-in`` 立即拒绝、``--check-only`` 不碰设备/不写文件、参数与模型检查、
``collect_pcm`` 收满即停/设备不给帧时及时退出、档案只含向量且 UTF-8 无 BOM、
只有给了 ``--out`` 才落盘。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.p9_voiceprint_enroll as enroll  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = enroll.DEFAULT_MODEL


class _BurstCapture:
    """开始采集时一次性把预设帧投进总线的替身（模拟设备回调，不建线程）。"""

    def __init__(self, bus: object, chunks: list[bytes]) -> None:
        self._bus = bus
        self._chunks = list(chunks)
        self.stopped: list[dict] = []

    def start(self) -> object:
        from app.contracts import AudioFrame

        for index, payload in enumerate(self._chunks):
            frame = AudioFrame(
                audio_session_id="p9-voiceprint-enroll",
                provider_generation=1,
                stream_id="p9-voiceprint-enroll-stream",
                event_sequence=index + 1,
                frame_index=index,
                captured_at="2026-10-01T09:00:00.000Z",
                duration_ms=20,
                device_epoch=1,
                sample_rate_hz=16000,
                channel_count=1,
                sample_format="pcm_s16le",
                sample_count=len(payload) // 2,
                payload_size_bytes=len(payload),
            )
            assert self._bus.publish(frame, payload).value == "accepted"  # type: ignore[attr-defined]
        return type("Start", (), {"started": True})()

    def stop(self, *, reason: str, discard: bool) -> None:
        self.stopped.append({"reason": reason, "discard": discard})


class _DeadCapture:
    def start(self) -> object:
        return type("Start", (), {"started": False, "reason": "no device"})()

    def stop(self, *, reason: str, discard: bool) -> None:  # pragma: no cover - 不会被调用
        raise AssertionError("start 失败后不应调用 stop")


class _FakeVoiceprint:
    def __init__(self, *, embedding: list[float] | None = None, returns_none: bool = False) -> None:
        self.embedding = [0.1, 0.2, 0.3] if embedding is None else list(embedding)
        self.returns_none = returns_none
        self.calls: list[bytes] = []

    def embed_from_pcm(self, payload: object, *, sample_rate_hz: int = 16000) -> object:
        del sample_rate_hz
        self.calls.append(bytes(payload))  # type: ignore[arg-type]
        return None if self.returns_none else list(self.embedding)


class OptInTests(unittest.TestCase):
    def test_without_live_opt_in_exits_two_without_touching_anything(self) -> None:
        with mock.patch.object(enroll, "collect_pcm") as collector:
            with redirect_stderr(io.StringIO()):
                code = enroll.main(["--check-only"])
        self.assertEqual(code, enroll.EXIT_NOT_OPTED_IN)
        collector.assert_not_called()

    def test_invalid_seconds_is_rejected_before_anything_else(self) -> None:
        with redirect_stderr(io.StringIO()):
            code = enroll.main(["--live-opt-in", "--seconds", "0"])
        self.assertEqual(code, enroll.EXIT_PREFLIGHT_FAILED)

    def test_missing_model_exits_three(self) -> None:
        with redirect_stderr(io.StringIO()):
            code = enroll.main(["--live-opt-in", "--model", str(REPO_ROOT / "models" / "nope.onnx")])
        self.assertEqual(code, enroll.EXIT_PREFLIGHT_FAILED)

    def test_check_only_never_opens_the_mic_or_writes_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            model = Path(raw) / "model.onnx"
            model.write_bytes(b"stub")
            out = Path(raw) / "profile.json"
            with mock.patch.object(enroll, "build_capture_factory") as factory:
                with mock.patch.object(enroll, "collect_pcm") as collector:
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        code = enroll.main(
                            ["--live-opt-in", "--check-only", "--model", str(model), "--out", str(out)]
                        )
            self.assertEqual(code, enroll.EXIT_OK)
            factory.assert_not_called()
            collector.assert_not_called()
            self.assertIn("未打开麦克风、未写任何文件", buffer.getvalue())
            self.assertFalse(out.exists())


class CollectPcmTests(unittest.TestCase):
    def test_collects_exactly_the_requested_bytes_and_stops_the_capture(self) -> None:
        holder: dict[str, _BurstCapture] = {}

        def factory(bus: object) -> _BurstCapture:
            # 1 秒需要 32000 字节 = 50 帧 × 640 字节；多给一些以确保收满即停。
            capture = _BurstCapture(bus, [bytes(640)] * 60)
            holder["capture"] = capture
            return capture

        pcm = enroll.collect_pcm(factory, seconds=1)
        self.assertEqual(len(pcm), enroll.required_bytes(1))
        self.assertEqual(holder["capture"].stopped, [{"reason": enroll.ENROLL_STOP_REASON, "discard": True}])

    def test_gives_up_quickly_when_the_device_delivers_nothing(self) -> None:
        ticks = [0.0]

        def clock() -> float:
            ticks[0] += 1.0
            return ticks[0]

        def factory(bus: object) -> _BurstCapture:
            return _BurstCapture(bus, [])

        pcm = enroll.collect_pcm(factory, seconds=1, clock=clock, sleep=lambda _s: None)
        self.assertEqual(pcm, b"")
        self.assertGreater(ticks[0], 1.0)

    def test_unavailable_capture_fails_closed(self) -> None:
        with self.assertRaises(enroll.EnrollError) as caught:
            enroll.collect_pcm(lambda bus: _DeadCapture(), seconds=1)
        self.assertEqual(str(caught.exception), "capture_unavailable")
        with self.assertRaises(enroll.EnrollError):
            enroll.collect_pcm(None, seconds=1)  # type: ignore[arg-type]


class ProfileTests(unittest.TestCase):
    def test_profile_contains_only_the_embedding_vector(self) -> None:
        profile = enroll.build_profile(
            speaker="我", embedding=[0.5, -0.25], model_path=Path("m/voiceprint.onnx")
        )
        self.assertEqual(profile["version"], enroll.PROFILE_VERSION)
        self.assertEqual(profile["speaker"], "我")
        self.assertEqual(profile["model"], "voiceprint.onnx")
        self.assertEqual(profile["embedding_dim"], 2)
        self.assertEqual(profile["embeddings"], [[0.5, -0.25]])
        self.assertIn("不含音频", profile["note"])
        serialized = json.dumps(profile, ensure_ascii=False)
        self.assertNotIn("pcm", serialized.lower())

    def test_empty_embedding_is_rejected(self) -> None:
        with self.assertRaises(enroll.EnrollError):
            enroll.build_profile(speaker="owner", embedding=[], model_path=Path("m.onnx"))

    def test_profile_is_utf8_without_bom_and_creates_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nested" / "profile.json"
            enroll.write_profile(path, enroll.build_profile(
                speaker="我", embedding=[1.0], model_path=Path("m.onnx")
            ))
            raw_bytes = path.read_bytes()
            self.assertFalse(raw_bytes.startswith(b"\xef\xbb\xbf"))
            payload = json.loads(raw_bytes.decode("utf-8"))
            self.assertEqual(payload["speaker"], "我")


class MainFlowTests(unittest.TestCase):
    def _run(
        self, argv: list[str], *, embedding: list[float] | None = None, returns_none: bool = False
    ) -> tuple[int, _FakeVoiceprint]:
        fake = _FakeVoiceprint(embedding=embedding, returns_none=returns_none)

        class FakeFilter:
            def __init__(self, *, config: object = None) -> None:
                del config

            def embed_from_pcm(self, payload: object, *, sample_rate_hz: int = 16000) -> object:
                return fake.embed_from_pcm(payload, sample_rate_hz=sample_rate_hz)

        with tempfile.TemporaryDirectory() as raw:
            model = Path(raw) / "model.onnx"
            model.write_bytes(b"stub")
            argv = [a.replace("<MODEL>", str(model)) for a in argv]
            with mock.patch.object(enroll, "build_capture_factory", return_value=lambda bus: None):
                with mock.patch.object(enroll, "collect_pcm", return_value=bytes(640)):
                    with mock.patch("app.audio.voiceprint.VoiceprintFilter", FakeFilter):
                        with redirect_stdout(io.StringIO()):
                            with redirect_stderr(io.StringIO()):
                                code = enroll.main(argv)
        return code, fake

    def test_writes_profile_only_when_out_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "profile.json"
            code, fake = self._run(
                ["--live-opt-in", "--model", "<MODEL>", "--out", str(out), "--speaker", "我"]
            )
            self.assertEqual(code, enroll.EXIT_OK)
            self.assertTrue(out.is_file())
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["speaker"], "我")
            self.assertEqual(payload["embeddings"], [[0.1, 0.2, 0.3]])
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(fake.calls[0]), 640)

    def test_without_out_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            before = set(Path(raw).iterdir())
            code, _fake = self._run(["--live-opt-in", "--model", "<MODEL>"])
            self.assertEqual(code, enroll.EXIT_OK)
            self.assertEqual(set(Path(raw).iterdir()), before)

    def test_short_audio_fails_closed_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "profile.json"
            code, _fake = self._run(
                ["--live-opt-in", "--model", "<MODEL>", "--out", str(out)], returns_none=True
            )
            self.assertEqual(code, enroll.EXIT_FAILED)
            self.assertFalse(out.exists())


@unittest.skipUnless(DEFAULT_MODEL.is_file(), "需要已下载的声纹模型（models/voiceprint）")
class RealModelSmokeTests(unittest.TestCase):
    def test_default_model_loads_and_produces_an_embedding(self) -> None:
        import wave

        from app.audio.voiceprint import VoiceprintConfig, VoiceprintFilter

        sample = REPO_ROOT / "models" / "kws" / "test_wavs" / "3.wav"
        if not sample.is_file():
            self.skipTest("需要 models/kws/test_wavs 里的真实语音样例")
        with wave.open(str(sample), "rb") as stream:
            raw = stream.readframes(stream.getnframes())
        filter_instance = VoiceprintFilter(config=VoiceprintConfig(model_path=DEFAULT_MODEL))
        embedding = filter_instance.embed_from_pcm(raw)
        self.assertIsNotNone(embedding)
        assert embedding is not None
        self.assertGreater(len(embedding), 32)
        profile = enroll.build_profile(speaker="owner", embedding=embedding, model_path=DEFAULT_MODEL)
        self.assertEqual(profile["embedding_dim"], len(embedding))
        self.assertFalse(profile["embeddings"][0][0] == 0.0 and profile["embeddings"][0][1] == 0.0)


if __name__ == "__main__":
    unittest.main()
