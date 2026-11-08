"""Offline tests for the SAPI-backed Edge slot provider.

Every test here is offline: no process is started (the runner is injected), no
sleep is used, no network is touched and no audio device is opened. The one
exception is the patched ``subprocess`` seam that proves the default runner's
exit-code, timeout and launch-failure codes without running PowerShell.
"""
from __future__ import annotations

import io
import tempfile
import unittest
import wave
from pathlib import Path
from threading import Event
from typing import Callable
from unittest import mock

from app.config.settings import ConfigError, load_settings
from app.tts.private_selection import private_data_root
from app.tts.providers import (
    EdgeSapiConfig,
    EdgeSapiError,
    EdgeSapiVoiceProvider,
    PCMChunk,
    VoiceStudioSentenceRequest,
    edge_sapi,
    list_edge_sapi_voices,
)

SCRIPT_PATH = Path(edge_sapi.__file__).resolve().parent / "edge_sapi_runner.ps1"
DEFAULT_VOICE = "Microsoft Huihui Desktop"
NETWORK_TOKENS = ("socket", "urllib", "requests", "http")


def wav_bytes(
    *,
    frames: int = 24_000,
    rate: int = 24_000,
    channels: int = 1,
    width: int = 2,
    value: int = 1,
) -> bytes:
    """Build a PCM WAV exactly like the fixed script is expected to emit."""

    frame = bytes([value, 0]) * (channels * (width // 2))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(channels)
        sink.setsampwidth(width)
        sink.setframerate(rate)
        sink.writeframes(frame * frames)
    return buffer.getvalue()


def pcm_payload(frame_count: int) -> bytes:
    return b"\x01\x00" * frame_count


def make_request(text: str = "离线合成测试句。", *, index: int = 1) -> VoiceStudioSentenceRequest:
    return VoiceStudioSentenceRequest(
        reply_id="reply-1",
        segment_id=f"segment-{index}",
        provider_request_id=f"request-{index}",
        provider_generation=3,
        text=text,
        profile_id="profile-1",
        language="Chinese",
        speed=1.0,
    )


class RecordingRunner:
    """Fake runner: it records every call and never starts a process."""

    def __init__(
        self,
        *,
        payload: bytes | None = None,
        error: BaseException | None = None,
        behaviour: "Callable[[Path], None] | None" = None,    ) -> None:
        self.calls: list[tuple[EdgeSapiConfig, str, Path, Path]] = []
        self.seen_texts: list[str] = []
        self._payload = payload
        self._error = error
        self._behaviour = behaviour

    def __call__(
        self, config: EdgeSapiConfig, script_path: str, text_path: Path, out_path: Path
    ) -> None:
        self.calls.append((config, script_path, text_path, out_path))
        self.seen_texts.append(text_path.read_text(encoding="utf-8"))
        if self._error is not None:
            raise self._error
        if self._behaviour is not None:
            self._behaviour(out_path)
            return
        if self._payload is not None:
            out_path.write_bytes(self._payload)


class EdgeSapiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_root = Path(self.temp_dir.name) / "edge-temp"

    def make_config(self, **overrides: object) -> EdgeSapiConfig:
        options: dict[str, object] = {"temp_root": self.temp_root}
        options.update(overrides)
        return EdgeSapiConfig(**options)  # type: ignore[arg-type]

    def assert_temp_root_clean(self) -> None:
        self.assertTrue(self.temp_root.is_dir())
        self.assertEqual(list(self.temp_root.iterdir()), [])

    @staticmethod
    def read_chunks(provider: EdgeSapiVoiceProvider, request, cancelled: Event | None = None):
        return list(provider.stream_sentence(request, cancelled=cancelled or Event()))


class EdgeSapiConfigTests(EdgeSapiTestCase):
    def test_defaults_match_the_offline_voice_and_the_voice_studio_format(self) -> None:
        config = EdgeSapiConfig()
        self.assertEqual(config.voice_name, DEFAULT_VOICE)
        self.assertEqual((config.sample_rate_hz, config.channels, config.sample_width), (24000, 1, 2))
        self.assertEqual(config.timeout_seconds, 10.0)
        self.assertEqual(config.max_audio_bytes, 8 * 1024 * 1024)
        self.assertEqual(config.chunk_samples, 4800)
        self.assertEqual(config.chunk_bytes, 9600)
        self.assertEqual(config.temp_root, private_data_root() / "temp")

    def test_temporary_root_is_created_below_the_requested_private_directory(self) -> None:
        config = self.make_config()
        self.assertEqual(config.temp_root, self.temp_root.resolve())
        self.assertTrue(config.temp_root.is_dir())

    def test_temporary_root_path_is_not_exposed_by_repr(self) -> None:
        config = self.make_config()
        self.assertNotIn(str(self.temp_root), repr(config))

    def test_unsupported_voice_names_are_rejected(self) -> None:
        for voice in (
            "",
            "Microsoft Zira\" Desktop",
            "Microsoft Zira' Desktop",
            "a;b",
            "Voice/Path",
            "line\nbreak",
            "tab\tvoice",
            "x" * 129,
        ):
            with self.subTest(voice=voice):
                with self.assertRaises(ValueError):
                    self.make_config(voice_name=voice)
        with self.assertRaises(ValueError):
            self.make_config(voice_name=None)
        with self.assertRaises(ValueError):
            self.make_config(voice_name=42)

    def test_only_24000_16_mono_is_supported(self) -> None:
        for name, value in (
            ("sample_rate_hz", 16_000),
            ("channels", 2),
            ("sample_width", 1),
            ("sample_rate_hz", 24_000.0),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    self.make_config(**{name: value})

    def test_timeout_limits_and_bounds_are_enforced(self) -> None:
        for value in (0, -1.0, "10", None, float("nan"), float("inf"), True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_config(timeout_seconds=value)
        for value in (0, 44, -1, "1048576", 1.5):
            with self.subTest(max_audio_bytes=value):
                with self.assertRaises(ValueError):
                    self.make_config(max_audio_bytes=value)
        for value in (0, -4800, "4800", 4800.0, True):
            with self.subTest(chunk_samples=value):
                with self.assertRaises(ValueError):
                    self.make_config(chunk_samples=value)

    def test_temporary_root_must_be_an_absolute_local_directory(self) -> None:
        occupied = Path(self.temp_dir.name) / "file.txt"
        occupied.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.make_config(temp_root=Path("relative-temp"))
        with self.assertRaises(ValueError):
            self.make_config(temp_root=occupied)
        with self.assertRaises(ValueError):
            self.make_config(temp_root=12345)
        with self.assertRaises(ValueError):
            self.make_config(temp_root=r"\\server\share\edge-temp")
        self.assertFalse((Path(self.temp_dir.name) / "relative-temp").exists())

    def test_temporary_root_rejects_a_symlink(self) -> None:
        target = Path(self.temp_dir.name) / "real-temp"
        target.mkdir()
        link = Path(self.temp_dir.name) / "link-temp"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("creating a directory symlink is not permitted here")
        with self.assertRaises(ValueError):
            self.make_config(temp_root=link)

    def test_settings_and_the_provider_share_one_voice_name_rule(self) -> None:
        """``TTS_EDGE_VOICE`` must accept exactly what ``EdgeSapiConfig`` accepts.

        The empty string is the one intentional difference: for the settings it
        means "Edge is not configured at all", while a provider that is actually
        asked to render needs a concrete installed voice name.
        """

        project_root = Path(self.temp_dir.name) / "repo"
        project_root.mkdir()
        data_root = str(Path(self.temp_dir.name) / "data")

        def settings_voice(voice: str) -> str:
            return load_settings(
                project_root=project_root,
                environ={"ASSISTANT_DATA_ROOT": data_root, "TTS_EDGE_VOICE": voice},
            ).tts_edge_voice

        self.assertEqual(
            load_settings(project_root=project_root, environ={"ASSISTANT_DATA_ROOT": data_root}).tts_edge_voice,
            "",
        )
        self.assertEqual(settings_voice("  " + DEFAULT_VOICE + "  "), DEFAULT_VOICE)
        with self.assertRaises(ValueError):
            self.make_config(voice_name="")

        good = (DEFAULT_VOICE, "Microsoft Zira Desktop", "voice_1.0 (x)")
        bad = ("a;b", 'a"b', "line\nbreak", "x" * 129)
        for voice in good:
            with self.subTest(voice=voice, expected="accepted"):
                self.assertEqual(self.make_config(voice_name=voice).voice_name, voice)
                self.assertEqual(settings_voice(voice), voice)
        for voice in bad:
            with self.subTest(voice=voice, expected="rejected"):
                with self.assertRaises(ValueError):
                    self.make_config(voice_name=voice)
                with self.assertRaises(ConfigError):
                    settings_voice(voice)


class EdgeSapiStreamTests(EdgeSapiTestCase):
    def test_chunks_follow_the_voice_studio_contract(self) -> None:
        frames = 20_000
        runner = RecordingRunner(payload=wav_bytes(frames=frames))
        config = self.make_config()
        provider = EdgeSapiVoiceProvider(config, runner=runner)
        request = make_request()
        chunks = self.read_chunks(provider, request)

        self.assertEqual([len(chunk.data) for chunk in chunks], [9600, 9600, 9600, 9600, 1600])
        self.assertEqual([chunk.sequence for chunk in chunks], [1, 2, 3, 4, 5])
        self.assertEqual([chunk.is_final for chunk in chunks], [False, False, False, False, True])
        self.assertTrue(all(isinstance(chunk, PCMChunk) for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(
                (chunk.reply_id, chunk.segment_id, chunk.provider_request_id, chunk.provider_generation),
                ("reply-1", "segment-1", "request-1", 3),
            )
            self.assertEqual(
                (chunk.sample_rate, chunk.channels, chunk.sample_width),
                (config.sample_rate_hz, config.channels, config.sample_width),
            )
        self.assertEqual(b"".join(chunk.data for chunk in chunks), pcm_payload(frames))
        self.assert_temp_root_clean()

    def test_single_block_audio_is_final_immediately(self) -> None:
        provider = EdgeSapiVoiceProvider(
            self.make_config(), runner=RecordingRunner(payload=wav_bytes(frames=100))
        )
        chunks = self.read_chunks(provider, make_request())
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].is_final)
        self.assertEqual(len(chunks[0].data), 200)

    def test_text_reaches_the_fixed_script_as_a_utf8_file(self) -> None:
        runner = RecordingRunner(payload=wav_bytes(frames=100))
        config = self.make_config()
        provider = EdgeSapiVoiceProvider(config, runner=runner)
        request = make_request("第二句：离线与真实发音无关。")
        self.read_chunks(provider, request)

        self.assertEqual(len(runner.calls), 1)
        recorded_config, script_path, text_path, out_path = runner.calls[0]
        self.assertIs(recorded_config, config)
        self.assertEqual(Path(script_path), SCRIPT_PATH)
        self.assertEqual(runner.seen_texts, [request.text])
        self.assertEqual(text_path.parent, config.temp_root)
        self.assertEqual(out_path.parent, config.temp_root)
        self.assertNotEqual(text_path, out_path)
        self.assertTrue(text_path.name.startswith("edge-sapi-"))
        self.assertTrue(text_path.name.endswith(".txt"))
        self.assertTrue(out_path.name.endswith(".wav"))

    def test_runner_error_is_reported_as_a_short_code(self) -> None:
        runner = RecordingRunner(error=EdgeSapiError("sapi_exit_1"))
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        with self.assertRaises(EdgeSapiError) as caught:
            self.read_chunks(provider, make_request())
        self.assertEqual(caught.exception.code, "sapi_exit_1")
        self.assert_temp_root_clean()

    def test_unexpected_runner_error_never_leaks_its_message(self) -> None:
        text = "绝密句子文本。"
        runner = RecordingRunner(error=RuntimeError(f"boom:{text}"))
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        with self.assertRaises(EdgeSapiError) as caught:
            self.read_chunks(provider, make_request(text))
        self.assertEqual(caught.exception.code, "sapi_runner_failed")
        self.assertNotIn(text, str(caught.exception))
        self.assertNotIn(text, repr(caught.exception))
        self.assert_temp_root_clean()

    def test_missing_invalid_and_oversized_wav_are_rejected(self) -> None:
        request = make_request()
        cases = (
            ("empty_file", RecordingRunner(), "sapi_audio_invalid"),
            (
                "missing",
                RecordingRunner(behaviour=lambda out_path: out_path.unlink()),
                "sapi_audio_missing",
            ),
            ("junk", RecordingRunner(payload=b"not a wav at all"), "sapi_audio_invalid"),
            ("wrong_rate", RecordingRunner(payload=wav_bytes(frames=100, rate=16_000)), "sapi_audio_format_invalid"),
            ("two_channels", RecordingRunner(payload=wav_bytes(frames=100, channels=2)), "sapi_audio_format_invalid"),
            ("empty", RecordingRunner(payload=wav_bytes(frames=0)), "sapi_audio_empty"),
            ("too_large", RecordingRunner(payload=wav_bytes(frames=50_000)), "sapi_audio_too_large"),
        )
        for name, runner, expected in cases:
            with self.subTest(case=name):
                config = self.make_config(max_audio_bytes=9600) if name == "too_large" else self.make_config()
                provider = EdgeSapiVoiceProvider(config, runner=runner)
                with self.assertRaises(EdgeSapiError) as caught:
                    self.read_chunks(provider, request)
                self.assertEqual(caught.exception.code, expected)
                self.assert_temp_root_clean()

    def test_truncated_wav_is_rejected(self) -> None:
        payload = wav_bytes(frames=1000)
        runner = RecordingRunner(payload=payload[: len(payload) // 2])
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        with self.assertRaises(EdgeSapiError) as caught:
            self.read_chunks(provider, make_request())
        self.assertIn(caught.exception.code, {"sapi_audio_invalid", "sapi_audio_truncated"})
        self.assert_temp_root_clean()

    def test_temporary_files_are_removed_when_the_stream_is_abandoned(self) -> None:
        runner = RecordingRunner(payload=wav_bytes(frames=20_000))
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        iterator = provider.stream_sentence(make_request(), cancelled=Event())
        first = next(iterator)
        self.assertEqual(first.sequence, 1)
        self.assertNotEqual(list(self.temp_root.iterdir()), [])
        iterator.close()
        self.assert_temp_root_clean()


class EdgeSapiCancellationTests(EdgeSapiTestCase):
    def test_cancelled_before_request_never_starts_the_runner(self) -> None:
        runner = RecordingRunner(payload=wav_bytes(frames=100))
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(EdgeSapiError) as caught:
            self.read_chunks(provider, make_request(), cancelled)
        self.assertEqual(caught.exception.code, "cancelled_before_request")
        self.assertEqual(runner.calls, [])
        self.assertEqual(list(self.temp_root.iterdir()), [])

    def test_cancelled_during_stream_stops_between_blocks_and_cleans_up(self) -> None:
        runner = RecordingRunner(payload=wav_bytes(frames=20_000))
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        cancelled = Event()
        iterator = provider.stream_sentence(make_request(), cancelled=cancelled)
        self.assertEqual(next(iterator).sequence, 1)
        cancelled.set()
        with self.assertRaises(EdgeSapiError) as caught:
            next(iterator)
        self.assertEqual(caught.exception.code, "cancelled_during_stream")
        self.assert_temp_root_clean()

    def test_closed_provider_refuses_new_sentences(self) -> None:
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=RecordingRunner())
        provider.close()
        self.assertTrue(provider.closed)
        with self.assertRaises(EdgeSapiError) as caught:
            self.read_chunks(provider, make_request())
        self.assertEqual(caught.exception.code, "provider_closed")


class EdgeSapiDefaultRunnerTests(EdgeSapiTestCase):
    """The default runner's failure paths, reached through a patched subprocess."""

    class Completed:
        def __init__(self, returncode: int, stdout: bytes = b"") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = b""

    def test_non_zero_exit_code_is_a_short_code_and_never_uses_a_shell(self) -> None:
        provider = EdgeSapiVoiceProvider(self.make_config())
        with mock.patch.object(edge_sapi.subprocess, "run", return_value=self.Completed(3)) as patched:
            with self.assertRaises(EdgeSapiError) as caught:
                self.read_chunks(provider, make_request())
        self.assertEqual(caught.exception.code, "sapi_exit_3")
        command = patched.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(part, str) for part in command))
        self.assertIn("-File", command)
        self.assertEqual(command[command.index("-File") + 1], str(SCRIPT_PATH))
        self.assertEqual(command[command.index("-VoiceName") + 1], DEFAULT_VOICE)
        self.assertIn("-TextPath", command)
        self.assertIn("-OutPath", command)
        self.assertNotIn("shell", patched.call_args.kwargs)
        self.assert_temp_root_clean()

    def test_timeout_and_launch_failure_are_short_codes(self) -> None:
        provider = EdgeSapiVoiceProvider(self.make_config())
        with mock.patch.object(
            edge_sapi.subprocess, "run", side_effect=edge_sapi.subprocess.TimeoutExpired(["ps"], 10.0)
        ):
            with self.assertRaises(EdgeSapiError) as caught:
                self.read_chunks(provider, make_request())
        self.assertEqual(caught.exception.code, "sapi_timeout")
        with mock.patch.object(edge_sapi.subprocess, "run", side_effect=OSError("no powershell")):
            with self.assertRaises(EdgeSapiError) as caught:
                self.read_chunks(provider, make_request())
        self.assertEqual(caught.exception.code, "sapi_launch_failed")
        self.assert_temp_root_clean()

    def test_child_output_never_reaches_the_error(self) -> None:
        provider = EdgeSapiVoiceProvider(self.make_config())
        with mock.patch.object(edge_sapi.subprocess, "run", return_value=self.Completed(1)):
            with self.assertRaises(EdgeSapiError) as caught:
                self.read_chunks(provider, make_request("不可泄露的句子。"))
        self.assertNotIn("不可泄露的句子。", repr(caught.exception))


class EdgeSapiVoiceListTests(EdgeSapiTestCase):
    def test_voice_list_parses_names_in_order_and_skips_junk(self) -> None:
        output = (
            "\ufeffMicrosoft Huihui Desktop|zh-CN|True\r\n"
            "Microsoft Zira Desktop|en-US|True\n"
            "\n"
            "no-separator-line\n"
            "Microsoft Huihui Desktop|zh-CN|True\n"
            "||\n"
        )
        voices = list_edge_sapi_voices(self.make_config(), runner=lambda config, script: output)
        self.assertEqual(voices, (DEFAULT_VOICE, "Microsoft Zira Desktop"))

    def test_voice_list_failures_are_short_codes(self) -> None:
        with self.assertRaises(EdgeSapiError) as caught:
            list_edge_sapi_voices(
                self.make_config(), runner=lambda config, script: (_ for _ in ()).throw(RuntimeError("x"))
            )
        self.assertEqual(caught.exception.code, "sapi_voice_list_failed")
        with self.assertRaises(EdgeSapiError) as caught:
            list_edge_sapi_voices(self.make_config(), runner=lambda config, script: None)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "sapi_voice_list_invalid")
        with self.assertRaises(EdgeSapiError) as caught:
            list_edge_sapi_voices(
                self.make_config(), runner=lambda config, script: (_ for _ in ()).throw(EdgeSapiError("sapi_exit_2"))
            )
        self.assertEqual(caught.exception.code, "sapi_exit_2")

    def test_voice_list_uses_the_fixed_script_and_the_same_timeout(self) -> None:
        calls: list[tuple[object, str]] = []

        def runner(config: EdgeSapiConfig, script_path: str) -> str:
            calls.append((config, script_path))
            return "Microsoft Huihui Desktop|zh-CN|True\n"

        config = self.make_config(timeout_seconds=2.5)
        self.assertEqual(list_edge_sapi_voices(config, runner=runner), (DEFAULT_VOICE,))
        self.assertEqual(calls, [(config, str(SCRIPT_PATH))])

    def test_default_voice_list_runner_reports_non_zero_exit(self) -> None:
        class Completed:
            returncode = 7
            stdout = b""
            stderr = b""

        with mock.patch.object(edge_sapi.subprocess, "run", return_value=Completed()) as patched:
            with self.assertRaises(EdgeSapiError) as caught:
                list_edge_sapi_voices(self.make_config())
        self.assertEqual(caught.exception.code, "sapi_exit_7")
        command = patched.call_args.args[0]
        self.assertIn("-ListVoices", command)


class EdgeSapiPrivacyTests(EdgeSapiTestCase):
    def test_provider_repr_and_errors_carry_no_text_or_temporary_path(self) -> None:
        text = "隐私句：不应出现在任何诊断里。"
        runner = RecordingRunner(payload=wav_bytes(frames=100))
        provider = EdgeSapiVoiceProvider(self.make_config(), runner=runner)
        self.assertNotIn(text, repr(provider))
        self.assertNotIn(str(self.temp_root), repr(provider))
        self.read_chunks(provider, make_request(text))
        text_path, out_path = runner.calls[0][2], runner.calls[0][3]
        for rendered in (repr(provider), repr(provider.config)):
            self.assertNotIn(str(text_path), rendered)
            self.assertNotIn(str(out_path), rendered)
            self.assertNotIn(text, rendered)

    def test_module_has_no_network_client_and_no_shell_invocation(self) -> None:
        source = Path(edge_sapi.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for token in NETWORK_TOKENS:
            self.assertNotIn(token, lowered)
        self.assertNotRegex(source, r"shell\s*=\s*True")
        self.assertNotIn("subprocess.Popen", source)
        self.assertIn("subprocess.run(", source)
        for attribute in ("socket", "urllib", "requests", "http"):
            self.assertFalse(hasattr(edge_sapi, attribute))

    def test_rendered_script_is_pure_ascii_and_fixed(self) -> None:
        raw = SCRIPT_PATH.read_bytes()
        self.assertEqual(sum(1 for byte in raw if byte > 127), 0)
        text = raw.decode("ascii")
        for parameter in ("-TextPath", "-OutPath", "-VoiceName", "-ListVoices"):
            self.assertIn(parameter, text)
        self.assertIn("Add-Type -AssemblyName System.Speech", text)
        self.assertIn("[System.IO.File]::ReadAllText", text)
        self.assertIn("System.Text.Encoding]::UTF8", text)
        self.assertIn("24000", text)
        self.assertGreaterEqual(text.count("exit 0"), 1)
        self.assertGreaterEqual(text.count("exit 1"), 5)


if __name__ == "__main__":
    unittest.main()
