"""主程序 P9 免按键自动收音入口测试：注入式会话/环境替身，不打开真实设备。

覆盖：CLI 开关与折算、`--auto` 关闭时零行为变化、装配失败降级为文字、`/text` 与 `/exit`、
HUD 状态推送、轮次异常收尾，以及共享配置类的校验。
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.adapter import AgentTurnOutcome
from app.conversation.manager import ConversationRouteResult
from app.main import _voice_entry_config_for_auto, main, run
from app.runtime.auto_voice import AutoTurnFacts, AutoVoiceConfig
from app.runtime.voice_entry import VoiceEntryEnvironment


def facts(**overrides: object) -> AutoTurnFacts:
    values: dict[str, object] = {
        "index": 1,
        "boundary": None,
        "frames": 140,
        "speech_seen": True,
        "end_reason": "auto_released",
        "accepted": True,
        "submitted": True,
        "spoken": True,
        "reason": "speech_finished",
        "seconds": 3.2,
        "asr_text": "你好，测试一下",
        "reply_text": "回复：你好，测试一下",
    }
    values.update(overrides)
    return AutoTurnFacts(**values)  # type: ignore[arg-type]


class StubAutoSession:
    """自动收音会话替身：记录每次 ``run_turn`` 的入参，事件回调为安全空操作。"""

    def __init__(self, results: list[AutoTurnFacts] | None = None) -> None:
        self.results = results or [facts()]
        self.turns = 0
        self.closed = 0
        self.turn_kwargs: list[dict[str, object]] = []
        self.fail_with: Exception | None = None

    def run_turn(self, **kwargs: object) -> AutoTurnFacts:
        self.turn_kwargs.append(kwargs)
        self.turns += 1
        on_listening = kwargs.get("on_listening")
        if callable(on_listening):
            on_listening()
        on_speech = kwargs.get("on_speech_started")
        if callable(on_speech):
            on_speech()
        if self.fail_with is not None:
            raise self.fail_with
        return self.results[min(self.turns, len(self.results)) - 1]

    def close(self) -> None:
        self.closed += 1


class RecordingConversation:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_text(self, text, *args, **kwargs):
        self.calls.append(text)
        return ConversationRouteResult(
            outcome=AgentTurnOutcome("r", "s", True, "文字回复。", "stop"), handoff=None
        )


class FakeHud:
    """状态提示窗替身：只记录状态序列。"""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.ticks = 0

    def set_state(self, state: object, *, turn_index: int | None = None) -> str:
        name = getattr(state, "name", str(state))
        self.states.append(name)
        return name

    def tick(self) -> int:
        self.ticks += 1
        return 0


class StubEnvironment:
    """语音环境替身：只提供自动会话需要的字段与 ``preflight``。

    ``PushToTalkCoordinator`` 会硬校验 ``isinstance(provider, SenseVoiceRecognizerAdapter)``，
    所以这里用**真实 adapter 对象**，但把模型加载器换成惰性替身（测试中绝不加载模型）。
    """

    def __init__(self, *, tts_mode: str = "none") -> None:
        from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter

        self.capture_factory = lambda bus: None
        self.provider = SenseVoiceRecognizerAdapter(
            SenseVoiceConfig(
                model_path=Path("fake-model.onnx"),
                tokens_path=Path("fake-tokens.txt"),
            ),
            module_loader=lambda: (_ for _ in ()).throw(RuntimeError("测试中绝不加载模型")),
        )
        self.speaker = None
        self.devices = ("fake-mic",)
        self.tts_mode = tts_mode
        self.details = {"device": "fake-mic", "device_id": "1", "model": "fake-asr"}
        self.preflights = 0

    def preflight(self) -> None:
        self.preflights += 1


class AutoVoiceMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root.parent / (self.root.name + "-runtime-data")
        (self.root / ".env").write_text(
            "ASSISTANT_DATA_ROOT=" + str(self.data_root) + "\n", encoding="utf-8"
        )
        self.env = patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(self.data_root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        # 装配路径会真的调用 provider.load()；测试里替换成空操作（不加载模型），
        # 但保留 isinstance 硬校验与其余启动语义。
        loader = patch("app.asr.SenseVoiceRecognizerAdapter.load", return_value=None)
        loader.start()
        self.addCleanup(loader.stop)

    # ------------------------------------------------------------ CLI 与折算
    def test_auto_flag_builds_auto_config_and_keeps_voice_config_none(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            code = main([
                "--voice", "--auto",
                "--silence-ms", "2000",
                "--max-record-seconds", "9",
                "--min-utterance-ms", "400",
                "--vad-mode", "energy",
                "--speech-threshold", "750",
                "--device-id", "3",
            ])
        self.assertEqual(code, 0)
        self.assertTrue(captured["voice_auto"])
        self.assertTrue(captured["push_to_talk"])
        self.assertIsNone(captured["voice_config"])
        config = captured["auto_config"]
        self.assertIsInstance(config, AutoVoiceConfig)
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.silence_ms, 2000)
        self.assertEqual(config.max_record_seconds, 9)
        self.assertEqual(config.min_utterance_ms, 400)
        self.assertEqual(config.vad_mode, "energy")
        self.assertEqual(config.speech_threshold, 750.0)
        self.assertEqual(config.device_id, "3")
        self.assertFalse(config.kws_mode == "gate")  # 唤醒词默认关闭
        self.assertEqual(config.voiceprint_mode, "off")

    def test_post_speech_mute_ms_is_folded_and_defaults_to_off(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            main(["--voice", "--auto"])
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.post_speech_mute_ms, 0)  # 默认关闭（零行为变化）

        captured.clear()
        with patch("app.main.run", side_effect=fake_run):
            main(["--voice", "--auto", "--post-speech-mute-ms", "800"])
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.post_speech_mute_ms, 800)

    def test_tts_speed_is_folded_into_auto_config_and_entry_config(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            main(["--voice", "--auto"])
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertIsNone(config.tts_speed)  # 默认不覆盖（行为不变）

        captured.clear()
        with patch("app.main.run", side_effect=fake_run):
            main(["--voice", "--auto", "--tts-speed", "1.4"])
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.tts_speed, 1.4)
        self.assertEqual(_voice_entry_config_for_auto(config).tts_speed, 1.4)

    def test_tts_speed_out_of_range_fails_closed(self) -> None:
        for speed in ("2.5", "0.4"):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    main(["--voice", "--auto", "--tts-speed", speed])

    def test_vad_model_path_is_folded_to_a_path(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            main(["--voice", "--auto", "--vad-model", "models/x/silero.onnx"])
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.vad_model_path, Path("models/x/silero.onnx"))

    def test_voice_without_auto_keeps_push_to_talk_and_no_auto_config(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            main(["--voice"])
        self.assertFalse(captured["voice_auto"])
        self.assertIsNotNone(captured["voice_config"])
        self.assertIsNone(captured["auto_config"])

    def test_auto_without_voice_stays_text_mode(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            main(["--auto"])
        self.assertFalse(captured["voice_auto"])
        self.assertFalse(captured["push_to_talk"])

    def test_cli_rejects_non_positive_auto_options(self) -> None:
        # argparse 自身不校验正数，但 AutoVoiceConfig 必须拦住（fail closed，不静默修正）。
        for argv in (["--voice", "--auto", "--max-record-seconds", "0"],
                     ["--voice", "--auto", "--min-utterance-ms", "-3"]):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    main(argv)

    # ------------------------------------------------------------ 运行路径
    def test_auto_off_never_touches_the_auto_machinery(self) -> None:
        """`voice_auto=False` 时必须完全走按键路径：自动环境/会话一概不被触碰。"""

        from app.runtime.voice_entry import VoiceEntryConfig

        session = StubAutoSession()
        output = io.StringIO()

        def forbidden_environment() -> object:
            raise AssertionError("voice_auto=False 时不得构建自动收音环境")

        # 按键路径用 tts_mode="none" 免掉真实 VoiceStudio 健康检查，保证用例确定性。
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("\n/exit\n"),
            output_stream=output,
            push_to_talk=True,
            voice_config=VoiceEntryConfig(tts_mode="none", silence_ms=1500),
            voice_auto=False,
            auto_session_factory=lambda _c: session,
            auto_environment_factory=forbidden_environment,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 0)
        self.assertNotIn("免按键自动收音已开启", output.getvalue())

    def test_auto_session_drives_turns_and_exits_on_command(self) -> None:
        session = StubAutoSession([facts(), facts(index=2, asr_text="第二句")])
        hud = FakeHud()
        output = io.StringIO()
        polls = iter([None, None, "/exit"])
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            conversation=RecordingConversation(),
            voice_auto=True,
            auto_session_factory=lambda _c: session,
            auto_interrupt_source=lambda: next(polls, "/exit"),
            voice_hud_factory=lambda: hud,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 2)
        self.assertEqual(session.closed, 1)
        text = output.getvalue()
        self.assertIn("免按键自动收音已开启", text)
        self.assertIn("识别：你好，测试一下", text)
        self.assertIn("识别：第二句", text)
        self.assertIn("右下角状态提示窗已启用", text)
        # HUD 状态序列：IDLE → 每轮 LISTENING（循环入口 + on_listening 各一次）→ 轮次结束后
        # IDLE，最后 STOPPED。**"播报"不在这里**：它由 `AutoVoiceSession` 在真实 `_speak`
        # 期间推给窗口（真机 2026-10-09 修复；见
        # `test_voice_status.AutoVoiceSpeakingStateTests`）。注入的会话替身不经过真实
        # `_speak`，因此这里 SPEAKING 必须是 0，而不是"在整轮结束之后补一个播报"。
        self.assertEqual(hud.states[0], "IDLE")
        self.assertEqual(hud.states[-1], "STOPPED")
        self.assertEqual(hud.states.count("LISTENING"), 4)
        self.assertEqual(hud.states.count("SPEAKING"), 0)
        self.assertEqual(hud.states.count("IDLE"), 3)
        self.assertGreater(hud.ticks, 0)

    def test_injected_session_factory_skips_environment_build(self) -> None:
        built: list[int] = []
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=io.StringIO(),
            voice_auto=True,
            auto_session_factory=lambda _c: StubAutoSession(),
            auto_environment_factory=lambda: built.append(1) or StubEnvironment(),
            auto_interrupt_source=lambda: "/exit",
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(built, [])

    def test_environment_is_preflighted_and_notify_is_wired(self) -> None:
        environment = StubEnvironment()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            voice_auto=True,
            auto_environment_factory=lambda: environment,
            auto_interrupt_source=lambda: "/exit",
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(environment.preflights, 1)
        self.assertIn("输入设备：fake-mic", output.getvalue())

    def test_production_factory_uses_real_environment_builder(self) -> None:
        """不注入环境工厂时，必须走 ``build_real_environment``（这里把它替换掉）。"""

        environment = StubEnvironment()
        with patch("app.runtime.voice_entry.build_real_environment",
                   return_value=environment) as builder:
            code = run(
                project_root=self.root,
                input_stream=io.StringIO("/exit\n"),
                output_stream=io.StringIO(),
                voice_auto=True,
                auto_interrupt_source=lambda: "/exit",
                voice_show_hud=False,
            )
        self.assertEqual(code, 0)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(environment.preflights, 1)

    def test_session_build_failure_falls_back_to_text(self) -> None:
        conversation = RecordingConversation()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=conversation,
            voice_auto=True,
            auto_session_factory=self._raise("模型缺失：C:\\secret\\model.onnx"),
        )
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("语音输入不可用", text)
        self.assertIn("模型缺失", text)
        self.assertEqual(conversation.calls, ["你好"])

    def test_environment_build_failure_falls_back_to_text(self) -> None:
        output = io.StringIO()

        def broken_environment() -> object:
            raise RuntimeError("没有可用的输入设备")

        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=RecordingConversation(),
            voice_auto=True,
            auto_environment_factory=broken_environment,
        )
        self.assertEqual(code, 0)
        self.assertIn("语音输入不可用", output.getvalue())

    def test_text_command_switches_to_text_input(self) -> None:
        session = StubAutoSession()
        conversation = RecordingConversation()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=conversation,
            voice_auto=True,
            auto_session_factory=lambda _c: session,
            auto_interrupt_source=lambda: "/text",
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 0)
        self.assertEqual(session.closed, 1)
        self.assertIn("已切回文字输入", output.getvalue())
        self.assertEqual(conversation.calls, ["你好"])

    def test_turn_failure_is_reported_and_degrades_to_text(self) -> None:
        session = StubAutoSession()
        session.fail_with = RuntimeError("设备断开")
        conversation = RecordingConversation()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=conversation,
            voice_auto=True,
            auto_session_factory=lambda _c: session,
            auto_interrupt_source=lambda: None,
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.closed, 1)
        self.assertIn("语音输入不可用", output.getvalue())
        self.assertEqual(conversation.calls, ["你好"])

    def test_keyboard_interrupt_stops_cleanly(self) -> None:
        session = StubAutoSession()
        session.fail_with = KeyboardInterrupt()
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            voice_auto=True,
            auto_session_factory=lambda _c: session,
            auto_interrupt_source=lambda: None,
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.closed, 1)
        self.assertIn("已停止免按键收音", output.getvalue())

    def test_kws_and_voiceprint_flags_are_folded(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            code = main([
                "--voice", "--auto",
                "--kws-mode", "gate",
                "--kws-model-dir", "models/kws",
                "--kws-keywords-file", "kws.txt",
                "--keyword", "小饼",
                "--kws-threshold", "0.3",
                "--kws-wait-seconds", "12",
                "--voiceprint-mode", "filter",
                "--voiceprint-model", "models/vp.onnx",
                "--voiceprint-profile", "data/vp.json",
                "--voiceprint-threshold", "0.6",
            ])
        self.assertEqual(code, 0)
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.kws_mode, "gate")
        self.assertEqual(config.kws_model_dir, Path("models/kws"))
        self.assertEqual(config.kws_keywords_file, Path("kws.txt"))
        self.assertEqual(config.keyword, "小饼")
        self.assertEqual(config.kws_threshold, 0.3)
        self.assertEqual(config.kws_wait_seconds, 12)
        self.assertEqual(config.voiceprint_mode, "filter")
        self.assertEqual(config.voiceprint_model_path, Path("models/vp.onnx"))
        self.assertEqual(config.voiceprint_profile, Path("data/vp.json"))
        self.assertEqual(config.voiceprint_threshold, 0.6)

    def test_kws_and_voiceprint_default_to_off(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        with patch("app.main.run", side_effect=fake_run):
            main(["--voice", "--auto"])
        config = captured["auto_config"]
        assert isinstance(config, AutoVoiceConfig)
        self.assertEqual(config.kws_mode, "off")
        self.assertEqual(config.voiceprint_mode, "off")
        self.assertEqual(config.keyword, "小饼")
        self.assertIsNone(config.kws_model_dir)
        self.assertIsNone(config.voiceprint_profile)

    def test_cli_rejects_bad_kws_and_voiceprint_options(self) -> None:
        for argv, expected in (
            (["--voice", "--auto", "--kws-mode", "always"], SystemExit),
            (["--voice", "--auto", "--voiceprint-mode", "on"], SystemExit),
            (["--voice", "--auto", "--kws-threshold", "1.5"], ValueError),
            (["--voice", "--auto", "--voiceprint-threshold", "0"], ValueError),
            (["--voice", "--auto", "--kws-wait-seconds", "0"], ValueError),
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(expected):
                    main(argv)

    def test_gate_model_missing_is_reported_not_silently_ignored(self) -> None:
        """显式要求 gate 但模型缺失时必须如实报错并降级为文字，绝不静默退化成无门禁。"""

        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("你好\n/exit\n"),
            output_stream=output,
            conversation=RecordingConversation(),
            voice_auto=True,
            auto_config=AutoVoiceConfig(kws_mode="gate",
                                        kws_model_dir=Path("no-such-kws-dir")),
            auto_environment_factory=lambda: StubEnvironment(),
            auto_interrupt_source=lambda: None,
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("语音输入不可用", text)
        self.assertIn("已按设计降级为文字输入", text)
        self.assertNotIn("免按键自动收音已开启", text)

    def test_voiceprint_profile_missing_is_reported(self) -> None:
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            voice_auto=True,
            auto_config=AutoVoiceConfig(
                voiceprint_mode="filter",
                voiceprint_model_path=Path("no-such-vp.onnx"),
                voiceprint_profile=Path("no-such-profile.json"),
            ),
            auto_environment_factory=lambda: StubEnvironment(),
            auto_interrupt_source=lambda: None,
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("语音输入不可用", output.getvalue())

    def test_agent_not_wired_is_stated_without_faking_a_reply(self) -> None:
        output = io.StringIO()
        code = run(
            project_root=self.root,
            input_stream=io.StringIO("/exit\n"),
            output_stream=output,
            voice_auto=True,
            auto_session_factory=lambda _c: StubAutoSession(),
            auto_interrupt_source=lambda: "/exit",
            voice_show_hud=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("未接入真实模型", output.getvalue())
        self.assertIn("不会伪造内容", output.getvalue())

    def test_rejects_wrong_config_type(self) -> None:
        with self.assertRaises(TypeError):
            run(
                project_root=self.root,
                input_stream=io.StringIO("/exit\n"),
                output_stream=io.StringIO(),
                voice_auto=True,
                auto_config="nope",  # type: ignore[arg-type]
            )

    @staticmethod
    def _raise(message: str):
        def factory(_conversation: object) -> object:
            raise RuntimeError(message)

        return factory


class AutoVoiceConfigTests(unittest.TestCase):
    def test_defaults_are_fail_closed_and_wake_word_off(self) -> None:
        config = AutoVoiceConfig()
        self.assertEqual(config.vad_mode, "silero")
        self.assertEqual(config.kws_mode, "off")
        self.assertEqual(config.voiceprint_mode, "off")
        self.assertEqual(config.silence_ms, 2500)
        self.assertEqual(config.max_record_seconds, 15)
        self.assertEqual(config.min_utterance_ms, 300)

    def test_invalid_values_are_rejected(self) -> None:
        for kwargs in (
            {"silence_ms": 0},
            {"max_record_seconds": -1},
            {"min_utterance_ms": True},
            {"vad_mode": "kws"},
            {"vad_threshold": 0.0},
            {"threshold_mode": "adaptive"},
            {"threshold_multiplier": 1.0},
            {"kws_mode": "always"},
            {"keyword": "   "},
            {"voiceprint_mode": "on"},
            {"voiceprint_threshold": 2.0},
            {"device_id": ""},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AutoVoiceConfig(**kwargs)  # type: ignore[arg-type]

    def test_path_strings_are_converted(self) -> None:
        config = AutoVoiceConfig(vad_model_path="models/vad.onnx",
                                 voiceprint_profile=Path("data/p.json"))
        self.assertEqual(config.vad_model_path, Path("models/vad.onnx"))
        self.assertEqual(config.voiceprint_profile, Path("data/p.json"))

    def test_environment_contract_matches_shared_session(self) -> None:
        """共享会话只依赖环境的三个字段；这里锁住契约，避免日后偷偷加依赖。"""

        for name in ("provider", "capture_factory", "speaker", "preflight"):
            with self.subTest(attribute=name):
                self.assertTrue(hasattr(StubEnvironment(), name), name)
        real = VoiceEntryEnvironment(capture_factory=lambda bus: None, provider=object(),
                                     speaker=None)
        for name in ("provider", "capture_factory", "speaker"):
            with self.subTest(real=name):
                self.assertTrue(hasattr(real, name), name)


class TtsAutostartTests(unittest.TestCase):
    """主程序侧：只有需要本机 TTS 的语音模式才尝试自动拉起，且可关。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root.parent / (self.root.name + "-runtime-data")
        (self.root / ".env").write_text(
            "ASSISTANT_DATA_ROOT=" + str(self.data_root) + "\n", encoding="utf-8"
        )
        self.env = patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(self.data_root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def _run_voice(self, *, tts_mode: str | None = None, policy: str = "on",
                   voice_auto: bool = True, expect_autostart: bool = True,
                   expect_code: int = 0) -> tuple[int, str, list]:
        from app.runtime.tts_service import TtsStartResult
        from app.runtime.voice_entry import VoiceEntryConfig

        calls: list[dict[str, object]] = []

        def fake_ensure(**kwargs: object) -> TtsStartResult:
            calls.append(kwargs)
            return TtsStartResult(enabled=True, already_reachable=False, started=True,
                                  reachable=True, pid=1234, elapsed_seconds=0.1)

        output = io.StringIO()
        kwargs: dict[str, object] = {
            "project_root": self.root,
            "input_stream": io.StringIO("/exit\n"),
            "output_stream": output,
            "push_to_talk": True,
            "tts_autostart": policy,
            "voice_show_hud": False,
            # 注入会话替身：这些用例只验证 TTS 自动启动策略，不碰真实设备/模型。
            "auto_session_factory": lambda _c: StubAutoSession(),
            "auto_interrupt_source": lambda: "/exit",
            "voice_auto": voice_auto,
        }
        if tts_mode is not None:
            kwargs["voice_config"] = VoiceEntryConfig(tts_mode=tts_mode, silence_ms=1500)
        with patch("app.runtime.tts_service.ensure_tts_service", side_effect=fake_ensure):
            code = run(**kwargs)  # type: ignore[arg-type]
        self.assertEqual(code, expect_code)
        if expect_autostart:
            self.assertEqual(len(calls), 1)
        else:
            self.assertEqual(calls, [])
        return code, output.getvalue(), calls

    def test_voice_with_local_tts_mode_autostarts_and_reports(self) -> None:
        _code, text, calls = self._run_voice()
        self.assertEqual(len(calls), 1)
        self.assertIn("已自动启动本机 TTS 服务", text)

    def test_edge_mode_never_autostarts(self) -> None:
        self._run_voice(tts_mode="edge", expect_autostart=False)

    def test_none_mode_never_autostarts(self) -> None:
        self._run_voice(tts_mode="none", expect_autostart=False)

    def test_policy_off_never_autostarts(self) -> None:
        _code, _text, calls = self._run_voice(policy="off", expect_autostart=False)
        self.assertEqual(calls, [])

    def test_no_voice_mode_never_autostarts(self) -> None:
        output = io.StringIO()
        calls: list[object] = []
        with patch("app.runtime.tts_service.ensure_tts_service",
                   side_effect=lambda **kw: calls.append(kw)):
            code = run(
                project_root=self.root,
                input_stream=io.StringIO("/exit\n"),
                output_stream=output,
                voice_show_hud=False,
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_timeout_degrades_to_text_with_a_clear_message(self) -> None:
        from app.runtime.tts_service import TtsStartResult

        def unavailable_session(_conversation: object) -> object:
            raise RuntimeError("TTS 服务未就绪")

        output = io.StringIO()
        with patch(
            "app.runtime.tts_service.ensure_tts_service",
            return_value=TtsStartResult(enabled=True, already_reachable=False, started=True,
                                        reachable=False, pid=9, elapsed_seconds=3.0,
                                        error="start_timeout"),
        ):
            code = run(
                project_root=self.root,
                input_stream=io.StringIO("你好\n/exit\n"),
                output_stream=output,
                conversation=RecordingConversation(),
                voice_auto=True,
                auto_session_factory=unavailable_session,
                auto_interrupt_source=lambda: "/exit",
                voice_show_hud=False,
            )
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("未就绪", text)
        self.assertIn("语音输入不可用", text)
        self.assertIn("已按设计降级为文字输入", text)


class ConsoleEncodingIsolationTests(unittest.TestCase):
    """真机教训（2026-10-02，批次 R4-A）：GBK 控制台遇到不可编码字符**不得**打断语音链路。

    实测现象：识别文本/回复里出现韩文 `응` 时 `output_stream.write` 抛 `UnicodeEncodeError`，
    异常沿 `run_turn` 冒泡 → 整个免按键会话被判"语音输入不可用"并降级为文字。
    """

    class GbkLikeStream(io.StringIO):
        """模拟 GBK 控制台：不可编码字符抛 UnicodeEncodeError。"""

        encoding = "gbk"

        def write(self, text: str) -> int:  # type: ignore[override]
            text.encode(self.encoding)  # 不可编码即抛，与真实流一致
            return super().write(text)

    def test_safe_write_falls_back_to_replace(self) -> None:
        from app.main import _safe_write

        stream = self.GbkLikeStream()
        _safe_write(stream, "回复：응답\n")
        written = stream.getvalue()
        self.assertIn("回复：", written)
        self.assertNotIn("응", written)  # 被替换成 '?'，但不抛错

    def test_render_turn_never_raises_on_unencodable_text(self) -> None:
        from app.main import _render_turn

        stream = self.GbkLikeStream()
        _render_turn(stream, facts(asr_text="응답", reply_text="응답"))
        written = stream.getvalue()
        self.assertIn("识别：", written)
        self.assertIn("回复：", written)
        self.assertNotIn("응", written)

    def test_safe_write_swallows_a_broken_stream(self) -> None:
        from app.main import _safe_write

        class BrokenStream:
            encoding = "gbk"

            def write(self, text: str) -> int:
                raise OSError("console gone")

        _safe_write(BrokenStream(), "回复：任意\n")  # 不应抛出


if __name__ == "__main__":
    unittest.main()
