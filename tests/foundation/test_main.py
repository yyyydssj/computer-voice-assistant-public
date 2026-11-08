from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import build_provider_integration, run
from app.config import load_settings


class MainTests(unittest.TestCase):
    @staticmethod
    def configure_private_root(root: Path) -> Path:
        data_root = root.parent / (root.name + "-runtime-data")
        (root / ".env").write_text(
            "ASSISTANT_DATA_ROOT=" + str(data_root) + "\n",
            encoding="utf-8",
        )
        return data_root


    def test_runtime_builds_default_local_provider_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = self.configure_private_root(root)
            settings = load_settings(
                project_root=root,
                environ={"ASSISTANT_DATA_ROOT": str(data_root)},
            )

            integration = build_provider_integration(settings)

            self.assertEqual(integration.config.provider, "local")
            self.assertTrue(integration.provider_ready)
            self.assertEqual(
                integration.capabilities.to_dict(),
                {
                    "provider": "local",
                    "adapter_injected": False,
                    "adapter_ready": False,
                    "provider_ready": True,
                    "runtime_ready": False,
                    "agent_ready": False,
                    "transport_ready": False,
                },
            )

    def test_runtime_keeps_explicit_deepseek_selection_not_ready_without_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = self.configure_private_root(root)
            settings = load_settings(
                project_root=root,
                environ={
                    "ASSISTANT_DATA_ROOT": str(data_root),
                    "ASSISTANT_PROVIDER": "deepseek",
                    "DEEPSEEK_API_KEY": "must-not-be-read",
                },
            )

            integration = build_provider_integration(settings)

            self.assertEqual(integration.config.provider, "deepseek")
            self.assertFalse(integration.provider_ready)
            self.assertFalse(integration.capabilities.adapter_injected)
            self.assertFalse(integration.capabilities.adapter_ready)
            self.assertFalse(integration.capabilities.runtime_ready)
            self.assertFalse(integration.capabilities.agent_ready)
            self.assertFalse(integration.capabilities.transport_ready)

    def test_run_starts_text_shell_and_closes_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = self.configure_private_root(root)
            output = io.StringIO()
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}, clear=False):
                result = run(
                    project_root=root,
                    input_stream=io.StringIO("你好\n/exit\n"),
                    output_stream=output,
                )

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertIn("文字模式", rendered)
            self.assertIn("语音不可用", rendered)
            self.assertIn("任务空闲", rendered)
            self.assertIn("Agent 尚未接入", rendered)
            self.assertIn("已请求安全关闭", rendered)

            records = [
                json.loads(line)
                for line in (data_root / "logs" / "runtime.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            config_record = next(
                record for record in records if record["event_type"] == "config_checked"
            )
            self.assertTrue(config_record["details"]["provider_ready"])
            self.assertFalse(config_record["details"]["runtime_ready"])
            self.assertFalse(config_record["details"]["agent_ready"])
            self.assertFalse(config_record["details"]["transport_ready"])
            runtime_record = next(
                record for record in records if record["event_type"] == "runtime_ready"
            )
            self.assertEqual(
                runtime_record["details"]["provider_ready"],
                config_record["details"]["provider_ready"],
            )
            self.assertFalse(runtime_record["details"]["runtime_ready"])
            self.assertEqual(records[0]["event_type"], "runtime_started")
            self.assertEqual(records[-1]["event_type"], "runtime_closing")
            self.assertEqual(
                [record["event_sequence"] for record in records],
                list(range(1, len(records) + 1)),
            )
            instance_ids = {record["instance_id"] for record in records}
            self.assertEqual(len(instance_ids), 1)

    def test_run_logs_selected_provider_without_secret_or_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = self.configure_private_root(root)
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "ASSISTANT_DATA_ROOT": str(data_root),
                    "ASSISTANT_PROVIDER": "deepseek",
                    "DEEPSEEK_API_KEY": "secret-marker-not-for-logs",
                },
                clear=False,
            ):
                result = run(
                    project_root=root,
                    input_stream=io.StringIO("/exit\n"),
                    output_stream=output,
                )

            self.assertEqual(result, 0)
            records = [
                json.loads(line)
                for line in (data_root / "logs" / "runtime.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            config_record = next(
                record for record in records if record["event_type"] == "config_checked"
            )
            self.assertEqual(config_record["details"]["provider"], "deepseek")
            self.assertFalse(config_record["details"]["adapter_injected"])
            self.assertFalse(config_record["details"]["adapter_ready"])
            self.assertFalse(config_record["details"]["provider_ready"])
            self.assertFalse(config_record["details"]["runtime_ready"])
            self.assertFalse(config_record["details"]["agent_ready"])
            self.assertFalse(config_record["details"]["transport_ready"])
            log_text = (data_root / "logs" / "runtime.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret-marker-not-for-logs", log_text)

    def test_eof_is_a_safe_close_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.configure_private_root(root)
            output = io.StringIO()
            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(root.parent / (root.name + "-runtime-data"))}, clear=False):
                self.assertEqual(
                    run(
                        project_root=root,
                        input_stream=io.StringIO(""),
                        output_stream=output,
                    ),
                    0,
                )
            self.assertIn("已请求安全关闭", output.getvalue())

    def test_startup_reports_possible_unfinished_plan_without_resuming_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = self.configure_private_root(root)
            plan_dir = data_root / "plans" / "old-task" / "versions" / "v001"
            plan_dir.mkdir(parents=True)
            plan_path = plan_dir / "plan.json"
            plan_text = '{"marker": "must-not-be-executed"}\n'
            plan_path.write_text(plan_text, encoding="utf-8")
            output = io.StringIO()

            with patch.dict("os.environ", {"ASSISTANT_DATA_ROOT": str(data_root)}, clear=False):
                result = run(
                    project_root=root,
                    input_stream=io.StringIO("/exit\n"),
                    output_stream=output,
                )

            self.assertEqual(result, 0)
            self.assertIn("发现可能未完成的旧任务", output.getvalue())
            self.assertIn("未自动续跑", output.getvalue())
            self.assertEqual(plan_path.read_text(encoding="utf-8"), plan_text)
            self.assertFalse((plan_dir / "result.json").exists())

            records = [
                json.loads(line)
                for line in (data_root / "logs" / "runtime.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            recovery_records = [
                record for record in records
                if record["event_type"] == "unfinished_task_detected"
            ]
            self.assertEqual(len(recovery_records), 1)
            self.assertEqual(recovery_records[0]["details"]["unfinished_count"], 1)
            self.assertFalse(recovery_records[0]["details"]["auto_resume"])
            self.assertNotIn(str(plan_path), json.dumps(recovery_records[0], ensure_ascii=False))

    def test_sensitive_environment_values_are_not_written_to_output_or_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = self.configure_private_root(root)
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "ASSISTANT_DATA_ROOT": str(data_root),
                    "DEEPSEEK_API_KEY": "secret-marker-not-for-logs",
                },
                clear=False,
            ):
                run(
                    project_root=root,
                    input_stream=io.StringIO("/exit\n"),
                    output_stream=output,
                )
            log_text = (data_root / "logs" / "runtime.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret-marker-not-for-logs", output.getvalue())
            self.assertNotIn("secret-marker-not-for-logs", log_text)


class VoiceHudWiringTests(unittest.TestCase):
    """语音模式的右下角提示窗接线（离线注入；绝不创建真实窗口）。"""

    class _Facts:
        def __init__(self, *, accepted: bool = True) -> None:
            self.reason = "ok"
            self.asr_text = "你好"
            self.reply_text = "回复"
            self.accepted = accepted

        def log_details(self) -> dict[str, object]:
            return {"accepted": self.accepted}

    class _Loop:
        def __init__(self, log: list[str]) -> None:
            self._speaker = None
            self.log = log

        def release_input(self) -> None:
            self.log.append("release")

        def complete_turn(self) -> str:
            self.log.append("complete")
            return "report"

        def _speak(self, text: str) -> tuple[bool, str]:
            del text
            self.log.append("speak")
            return False, "text_only"

    class _Session:
        def __init__(self, log: list[str]) -> None:
            self.loop = VoiceHudWiringTests._Loop(log)
            self.turns = 0
            self.closed = 0

        def run_turn(self) -> "VoiceHudWiringTests._Facts":
            self.turns += 1
            # 真实 ``ChatVoiceLoop.complete_turn()`` 内部会依次触发这三个动作。
            self.loop.release_input()
            self.loop.complete_turn()
            self.loop._speak("回复")
            return VoiceHudWiringTests._Facts()

        def close(self) -> None:
            self.closed += 1

    class _Hud:
        def __init__(self) -> None:
            self.states: list[str] = []
            self.ticks = 0
            self.hidden = 0
            self.destroyed = 0

        def set_state(self, state: object, *, turn_index: int | None = None) -> str:
            del turn_index
            self.states.append(getattr(state, "value", str(state)))
            return self.states[-1]

        def tick(self) -> int:
            self.ticks += 1
            return 0

        def hide(self) -> None:
            self.hidden += 1

        def destroy(self) -> None:
            self.destroyed += 1

    class _Lifecycle:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def emit(self, event_type: str, details: dict) -> None:
            self.events.append((event_type, dict(details)))

    def _run_voice(self, *, show_hud: bool, hud_factory=None):
        from app.main import _run_push_to_talk
        from app.runtime.voice_entry import VoiceEntryConfig

        log: list[str] = []
        session = self._Session(log)
        output = io.StringIO()
        lifecycle = self._Lifecycle()
        code = _run_push_to_talk(
            conversation=object(),
            config=VoiceEntryConfig(tts_mode="none"),
            session_factory=lambda conversation: session,
            input_stream=io.StringIO("\n/exit\n"),
            output_stream=output,
            lifecycle=lifecycle,
            agent_wired=False,
            show_hud=show_hud,
            hud_factory=hud_factory,
        )
        return code, session, output, lifecycle

    def test_voice_mode_delivers_states_and_closes_the_window(self) -> None:
        hud = self._Hud()
        code, session, output, lifecycle = self._run_voice(
            show_hud=True, hud_factory=lambda: hud
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 1)
        self.assertEqual(session.closed, 1)
        self.assertEqual(hud.states[0], "idle")
        for state in ("listening", "transcribing", "thinking", "speaking", "listening"):
            self.assertIn(state, hud.states)
        self.assertEqual(hud.states[-1], "stopped")
        self.assertEqual((hud.hidden, hud.destroyed), (1, 1))
        self.assertGreaterEqual(hud.ticks, 1)
        self.assertIn("右下角状态提示窗已启用", output.getvalue())
        # 阶段钩子必须还原：不得把展示层包装永久留在会话上。
        self.assertNotIn("release_input", vars(session.loop))
        self.assertIn("voice_entry_ready", [event for event, _ in lifecycle.events])

    def test_no_hud_never_builds_a_window(self) -> None:
        calls: list[str] = []

        def factory() -> object:
            calls.append("built")
            return self._Hud()

        code, _session, output, _lifecycle = self._run_voice(
            show_hud=False, hud_factory=factory
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertNotIn("提示窗", output.getvalue())

    def test_unavailable_window_is_reported_and_voice_still_runs(self) -> None:
        code, session, output, _lifecycle = self._run_voice(
            show_hud=True, hud_factory=lambda: None
        )
        self.assertEqual(code, 0)
        self.assertEqual(session.turns, 1)
        self.assertIn("右下角状态提示窗不可用", output.getvalue())

    def test_main_forwards_the_hud_switch_to_run(self) -> None:
        import app.main as main_module

        captured: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            captured.clear()
            captured.update(kwargs)
            return 0

        with patch.object(main_module, "run", side_effect=fake_run):
            self.assertEqual(main_module.main(["--voice", "--no-hud"]), 0)
        self.assertTrue(captured["push_to_talk"])
        self.assertFalse(captured["voice_show_hud"])

        with patch.object(main_module, "run", side_effect=fake_run):
            self.assertEqual(main_module.main(["--voice"]), 0)
        self.assertTrue(captured["voice_show_hud"])

        with patch.object(main_module, "run", side_effect=fake_run):
            self.assertEqual(main_module.main([]), 0)
        self.assertFalse(captured["push_to_talk"])


if __name__ == "__main__":
    unittest.main()
