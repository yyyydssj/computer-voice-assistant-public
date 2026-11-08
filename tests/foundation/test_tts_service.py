"""本机 TTS 服务（VoiceStudio）可达性检查与按需自动拉起的离线测试。

全部注入 fake 的「探测 / 启动 / 时钟 / 休眠」：**不真的启进程、不发网络请求、不睡**。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.runtime import tts_service as tts


class ReachabilityTests(unittest.TestCase):
    def test_autostart_defaults_on_and_can_be_disabled(self) -> None:
        self.assertTrue(tts.autostart_enabled({}))
        self.assertTrue(tts.autostart_enabled({tts.ENV_AUTOSTART: "1"}))
        self.assertTrue(tts.autostart_enabled({tts.ENV_AUTOSTART: "ON"}))
        self.assertFalse(tts.autostart_enabled({tts.ENV_AUTOSTART: "0"}))
        self.assertFalse(tts.autostart_enabled({tts.ENV_AUTOSTART: "off"}))
        # 无法识别的值按“不启动”处理：宁可少做副作用。
        self.assertFalse(tts.autostart_enabled({tts.ENV_AUTOSTART: "maybe"}))

    def test_command_comes_from_environment_only(self) -> None:
        self.assertIsNone(tts.build_start_command({}))
        self.assertIsNone(tts.build_start_command({tts.ENV_PYTHON: "py.exe"}))
        self.assertIsNone(tts.build_start_command({tts.ENV_BACKEND: "b/main.py"}))

    def test_command_uses_backend_parent_as_default_cwd(self) -> None:
        command = tts.build_start_command({
            tts.ENV_PYTHON: "C:\\py\\python.exe",
            tts.ENV_BACKEND: "C:\\app\\backend\\main.py",
        })
        assert command is not None
        self.assertEqual(command.argv, ("C:\\py\\python.exe", "C:\\app\\backend\\main.py"))
        self.assertEqual(command.cwd, Path("C:\\app\\backend"))

    def test_command_honours_explicit_cwd(self) -> None:
        command = tts.build_start_command({
            tts.ENV_PYTHON: "py.exe",
            tts.ENV_BACKEND: "b.py",
            tts.ENV_CWD: "work",
        })
        assert command is not None
        self.assertEqual(command.cwd, Path("work"))

    def test_override_argument_wins(self) -> None:
        command = tts.build_start_command({}, override=["py.exe", "b.py", "--flag"])
        assert command is not None
        self.assertEqual(command.argv, ("py.exe", "b.py", "--flag"))
        self.assertIsNone(tts.build_start_command({}, override=[]))

    def test_command_rejects_invalid_shapes(self) -> None:
        with self.assertRaises(tts.TtsServiceError):
            tts.TtsStartCommand(argv=())
        with self.assertRaises(tts.TtsServiceError):
            tts.TtsStartCommand(argv=("py", ""))
        with self.assertRaises(tts.TtsServiceError):
            tts.TtsStartCommand(argv="py", cwd=None)  # type: ignore[arg-type]
        with self.assertRaises(tts.TtsServiceError):
            tts.TtsStartCommand(argv=("py",), cwd="work")  # type: ignore[arg-type]


class EnsureServiceTests(unittest.TestCase):
    def test_already_reachable_never_spawns(self) -> None:
        spawned: list[object] = []
        result = tts.ensure_tts_service(
            environ={},
            command=tts.TtsStartCommand(("py", "b.py")),
            reachable=lambda: True,
            spawn=lambda cmd: spawned.append(cmd) or 1,
        )
        self.assertTrue(result.already_reachable)
        self.assertFalse(result.started)
        self.assertTrue(result.reachable)
        self.assertEqual(spawned, [])
        self.assertIn("已在运行", result.summary())

    def test_unreachable_spawns_once_and_waits_until_ready(self) -> None:
        probes = {"count": 0}
        spawned: list[tts.TtsStartCommand] = []

        def reachable() -> bool:
            probes["count"] += 1
            return probes["count"] >= 3

        result = tts.ensure_tts_service(
            environ={},
            command=tts.TtsStartCommand(("py", "b.py")),
            reachable=reachable,
            spawn=lambda cmd: spawned.append(cmd) or 4242,
            timeout_seconds=5.0,
            poll_interval_seconds=0.01,
            clock=lambda: 0.0,
            sleep=lambda _s: None,
        )
        self.assertTrue(result.started)
        self.assertTrue(result.reachable)
        self.assertEqual(result.pid, 4242)
        self.assertEqual(len(spawned), 1)
        self.assertIn("已自动启动", result.summary())

    def test_timeout_is_reported_not_raised(self) -> None:
        ticks = {"now": 0.0}

        def clock() -> float:
            ticks["now"] += 1.0
            return ticks["now"]

        result = tts.ensure_tts_service(
            environ={},
            command=tts.TtsStartCommand(("py", "b.py")),
            reachable=lambda: False,
            spawn=lambda cmd: 99,
            timeout_seconds=2.0,
            poll_interval_seconds=0.01,
            clock=clock,
            sleep=lambda _s: None,
        )
        self.assertTrue(result.started)
        self.assertFalse(result.reachable)
        self.assertEqual(result.error, "start_timeout")
        self.assertIn("未就绪", result.summary())

    def test_missing_command_is_skipped_without_spawning(self) -> None:
        spawned: list[object] = []
        result = tts.ensure_tts_service(
            environ={},
            command=None,
            reachable=lambda: False,
            spawn=lambda cmd: spawned.append(cmd) or 1,
        )
        self.assertFalse(result.started)
        self.assertFalse(result.reachable)
        self.assertEqual(result.error, "command_not_configured")
        self.assertEqual(spawned, [])
        self.assertIn(tts.ENV_PYTHON, result.summary())

    def test_disabled_policy_never_probes_command_or_spawns(self) -> None:
        spawned: list[object] = []
        result = tts.ensure_tts_service(
            environ={},
            enabled=False,
            reachable=lambda: True,
            spawn=lambda cmd: spawned.append(cmd) or 1,
        )
        self.assertFalse(result.enabled)
        self.assertFalse(result.started)
        self.assertEqual(spawned, [])
        self.assertIn("已按配置关闭", result.summary())

    def test_spawn_failure_is_reported(self) -> None:
        result = tts.ensure_tts_service(
            environ={},
            command=tts.TtsStartCommand(("py", "b.py")),
            reachable=lambda: False,
            spawn=lambda cmd: None,
        )
        self.assertFalse(result.started)
        self.assertEqual(result.error, "spawn_failed")
        self.assertIn("失败", result.summary())

    def test_probe_failures_are_swallowed_by_default_probe(self) -> None:
        """默认探测实现必须把连接错误吞成 False，绝不把 OSError 抛给语音链路。"""

        self.assertFalse(tts._http_reachable("http://127.0.0.1:1", timeout=0.05))


class OwnedProcessTests(unittest.TestCase):
    def test_remembers_only_started_pids(self) -> None:
        owned = tts.OwnedProcess()
        owned.remember(tts.TtsStartResult(enabled=True, already_reachable=True,
                                          started=False, reachable=True, pid=1))
        self.assertIsNone(owned.pid)
        owned.remember(tts.TtsStartResult(enabled=True, already_reachable=False,
                                          started=True, reachable=True, pid=7))
        self.assertEqual(owned.pid, 7)

    def test_disabled_result_is_not_remembered(self) -> None:
        owned = tts.OwnedProcess()
        owned.remember(tts.TtsStartResult(enabled=False, already_reachable=False,
                                          started=True, reachable=True, pid=7))
        self.assertIsNone(owned.pid)

    def test_shutdown_without_owned_process_is_noop(self) -> None:
        self.assertFalse(tts.OwnedProcess().shutdown_owned())


class SummaryTests(unittest.TestCase):
    def test_summaries_cover_all_states(self) -> None:
        cases = {
            "already": (tts.TtsStartResult(True, True, False, True), "已在运行"),
            "started": (tts.TtsStartResult(True, False, True, True, pid=5,
                                           elapsed_seconds=1.5), "已自动启动"),
            "disabled": (tts.TtsStartResult(False, False, False, True), "已按配置关闭"),
            "no_command": (tts.TtsStartResult(True, False, False, False,
                                              error="command_not_configured"),
                           tts.ENV_BACKEND),
            "spawn_failed": (tts.TtsStartResult(True, False, False, False,
                                                error="spawn_failed"), "失败"),
            "timeout": (tts.TtsStartResult(True, False, True, False,
                                           error="start_timeout"), "未就绪"),
        }
        for name, (result, expected) in cases.items():
            with self.subTest(state=name):
                self.assertIn(expected, result.summary())

    def test_module_only_imports_standard_library(self) -> None:
        import ast

        import app.runtime.tts_service as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "collections", "dataclasses", "json", "os",
                                    "pathlib", "subprocess", "time", "urllib"})


if __name__ == "__main__":
    unittest.main()
