"""P8 真机验收 runner 的离线契约测试。

不打开麦克风、不加载真实 ASR、不播报：通过注入离线替身环境验证 runner 的
退出码、artifact 事实与隐私边界（识别文本不落盘），以及“未 opt-in 不触设备”。
"""

from __future__ import annotations

import ast
import array
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceCheckResult,
    DeviceCheckStatus,
    DeviceSelection,
    FakeCaptureStream,
)
from tests.integration.test_p6_integration import _OfflineRecognizer, _Recognizer
from tools import p8_real_voice_loop_runner as runner

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "tools" / "p8_real_voice_loop_runner.py"
SAFETY_CHAIN_PREFIXES = (
    "app.safety",
    "app.executor",
    "app.plans",
    "app.auditing",
    "app.verification",
    "app.tools",
    "app.recovery",
    "app.retry",
    "app.state",
    "app.ui",
)

SPOKEN_SENTENCE = "打开编辑器"


class EmittingStream(FakeCaptureStream):
    """一收到 start 就送出一帧带能量的 PCM16，模拟一次按键说话。"""

    def __init__(self, payload: bytes, frames: int) -> None:
        super().__init__()
        self._payload = payload
        self._frames = frames
        self.started = False

    def start(self) -> None:
        super().start()
        self.started = True
        self.emit(self._payload, frames=self._frames)


class AutoCaptureBackend:
    """离线采集替身：check_device 通过，start 时自动给出一帧语音。"""

    def __init__(self, payload: bytes, frames: int) -> None:
        self.stream = EmittingStream(payload, frames)

    def check_device(self, selection, *, sample_rate_hz: int, channel_count: int) -> DeviceCheckResult:
        del selection, sample_rate_hz, channel_count
        return DeviceCheckResult(DeviceCheckStatus.READY, "mic-1", "ready")

    def open_input_stream(self, **kwargs: object) -> EmittingStream:
        self.stream.callback = kwargs["callback"]  # type: ignore[assignment]
        return self.stream


def speech_payload(*, amplitude: int = 3000, frames: int = 320) -> bytes:
    return array.array("h", [amplitude] * frames).tobytes()


def silence_payload(*, frames: int = 320) -> bytes:
    return array.array("h", [0] * frames).tobytes()


def fake_environment(*, payload: bytes | None = None) -> runner.Environment:
    _OfflineRecognizer.recognizer = _Recognizer(SPOKEN_SENTENCE)
    module = types.ModuleType("sherpa_onnx")
    module.OfflineRecognizer = _OfflineRecognizer  # type: ignore[attr-defined]
    provider = SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
        module_loader=lambda: module,
    )
    backend = AutoCaptureBackend(payload if payload is not None else speech_payload(), 320)

    def capture_factory(bus) -> CaptureSession:
        return CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(),
        )

    return runner.Environment(
        capture_factory=capture_factory,
        provider=provider,
        speaker=None,
        devices=("Fake Mic",),
        tts_mode="none",
        details={"device": "Fake Mic", "device_id": "mic-1", "model": "fake.onnx"},
    )


def run_main(arguments: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = runner.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class NotOptedInTests(unittest.TestCase):
    def test_without_live_opt_in_it_refuses_and_touches_no_device(self) -> None:
        before = set(sys.modules)
        code, _out, err = run_main([])
        self.assertEqual(code, runner.EXIT_NOT_OPTED_IN)
        self.assertIn("--live-opt-in", err)
        after = set(sys.modules)
        self.assertNotIn("sounddevice", after - before)
        self.assertNotIn("sherpa_onnx", after - before)

    def test_check_only_still_requires_the_flag(self) -> None:
        code, _out, _err = run_main(["--check-only"])
        self.assertEqual(code, runner.EXIT_NOT_OPTED_IN)


class ArgumentContractTests(unittest.TestCase):
    def test_invalid_turn_count_and_tts_mode_are_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            runner.main(["--live-opt-in", "--turns", "0"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx:
            runner.main(["--live-opt-in", "--tts-mode", "bogus"])
        self.assertEqual(ctx.exception.code, 2)

    def test_defaults_are_local_only(self) -> None:
        args = runner._parse_args(["--live-opt-in"])
        self.assertEqual(args.turns, 2)
        self.assertEqual(args.tts_mode, "ws_pcm")
        self.assertIn(args.tts_mode, runner.TTS_MODES)
        self.assertEqual(runner.VOICE_WS_ENDPOINT, "ws://127.0.0.1:3900")


class FakeEnvironmentRunTests(unittest.TestCase):
    def test_offline_turn_submits_prints_text_and_keeps_artifact_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(runner, "build_environment", lambda args: fake_environment()):
                code, out, _err = run_main(
                    [
                        "--live-opt-in",
                        "--turns",
                        "1",
                        "--tts-mode",
                        "none",
                        "--silence-ms",
                        "60",
                        "--user-heard",
                        "no",
                        "--artifact-root",
                        str(root),
                    ]
                )
            artifacts = list(root.glob("*.json"))
            self.assertEqual(code, runner.EXIT_OK, out)
            self.assertEqual(len(artifacts), 1)
            self.assertIn(SPOKEN_SENTENCE, out)
            payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
            turn = payload["turns"][0]
            self.assertTrue(turn["accepted"])
            self.assertTrue(turn["submitted"])
            self.assertFalse(turn["spoken"])
            self.assertEqual(turn["end_reason"], "silence_timeout")
            self.assertEqual(turn["asr_text_length"], len(SPOKEN_SENTENCE))
            self.assertEqual(
                turn["asr_text_sha256"],
                runner.hashlib.sha256(SPOKEN_SENTENCE.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(turn["boundary"]["turn_id"], turn["boundary"]["turn_id"])
            self.assertEqual(payload["user_heard"], "no")
            self.assertEqual(payload["agent"], "local_scripted_echo（非真实模型）")
            for key, value in payload["boundary"].items():
                if key == "network_target":
                    continue
                self.assertIs(value, False, key)
            raw = artifacts[0].read_text(encoding="utf-8")
            self.assertNotIn(SPOKEN_SENTENCE, raw)
            self.assertNotIn("我听到你说", raw)

    def test_silence_only_turn_is_discarded_without_submitting_or_speaking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(
                runner, "build_environment", lambda args: fake_environment(payload=silence_payload())
            ):
                code, out, _err = run_main(
                    [
                        "--live-opt-in",
                        "--turns",
                        "1",
                        "--tts-mode",
                        "none",
                        "--silence-ms",
                        "60",
                        "--speech-timeout-seconds",
                        "1",
                        "--user-heard",
                        "unknown",
                        "--artifact-root",
                        str(root),
                    ]
                )
            artifacts = list(root.glob("*.json"))
            self.assertEqual(code, runner.EXIT_TURN_FAILED, out)
            self.assertIn("未检测到语音", out)
            self.assertEqual(len(artifacts), 1)
            turn = json.loads(artifacts[0].read_text(encoding="utf-8"))["turns"][0]
            self.assertFalse(turn["speech_seen"])
            self.assertFalse(turn["submitted"])
            self.assertFalse(turn["spoken"])
            self.assertEqual(turn["reason"], "no_speech_discarded")

    def test_check_only_does_not_write_an_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(runner, "build_environment", lambda args: fake_environment()):
                code, out, _err = run_main(
                    ["--live-opt-in", "--check-only", "--artifact-root", str(root)]
                )
            self.assertEqual(code, runner.EXIT_OK)
            self.assertIn("只做环境检查", out)
            self.assertEqual(list(root.glob("*")), [])

    def test_environment_failure_returns_preflight_exit_code(self) -> None:
        def boom(args):
            raise runner.RunnerError("没有可用的输入设备")

        with patch.object(runner, "build_environment", boom):
            code, _out, err = run_main(["--live-opt-in", "--check-only"])
        self.assertEqual(code, runner.EXIT_PREFLIGHT_FAILED)
        self.assertIn("没有可用的输入设备", err)

    def test_asr_startup_failure_is_reported_without_crashing(self) -> None:
        environment = fake_environment()

        def load_failure():
            raise RuntimeError("sherpa init failed")

        environment.provider = SenseVoiceRecognizerAdapter(
            SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
            module_loader=load_failure,
        )
        with patch.object(runner, "build_environment", lambda args: environment):
            code, _out, err = run_main(["--live-opt-in"])
        self.assertEqual(code, runner.EXIT_PREFLIGHT_FAILED)
        self.assertIn("降级为文字输入", err)


class RunnerBoundaryTests(unittest.TestCase):
    def test_runner_never_imports_the_safety_chain(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        for name in modules:
            for prefix in SAFETY_CHAIN_PREFIXES:
                with self.subTest(module=name):
                    self.assertFalse(name == prefix or name.startswith(prefix + "."))

    def test_runner_has_no_arbitrary_process_or_shell_surface(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        for marker in ("subprocess", "os.system", "Popen", "shell=True", "paramiko"):
            self.assertNotIn(marker, source)

    def test_energy_detector_only_needs_pcm_energy(self) -> None:
        detector = runner.energy_detector(600.0)
        packet = type("Packet", (), {"payload": speech_payload(amplitude=3000)})()
        quiet = type("Packet", (), {"payload": speech_payload(amplitude=10)})()
        broken = type("Packet", (), {"payload": b""})()
        self.assertTrue(detector(packet))
        self.assertFalse(detector(quiet))
        self.assertFalse(detector(broken))


if __name__ == "__main__":
    unittest.main()
