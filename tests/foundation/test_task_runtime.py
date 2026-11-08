"""P10 · T1 批次 2b 红测：**任务执行装配层**（`app/runtime/task_runtime.py`）。

全部离线：白名单用临时目录里的假 exe；编排器用测试替身；**不启动任何真实进程**。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.contracts.routing import RouteTrace
from app.routing import RouteInput
from app.runtime.orchestration import LocalTaskRouter
from app.runtime.orchestration.task_dispatch import (
    DENIED_TEXT,
    REASON_STALE_PREPARATION,
    STATUS_DENIED,
    STATUS_EXECUTED,
)
from app.runtime.task_runtime import (
    TaskRuntimeError,
    TextTaskDispatcher,
    build_application_whitelist,
    build_text_task_dispatcher,
    file_sha256,
    launch_target_state_hasher,
    utc_timestamp,
)
from tests.orchestration.test_task_dispatch import _Factory, make_handoff
from tests.orchestration.test_task_flow import HASH_A, previewer


def make_fake_app(root: Path, name: str, *, content: bytes = b"fake-exe") -> str:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(content)
    return str(path)


class FileSha256Tests(unittest.TestCase):
    def test_reads_a_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blob.bin"
            path.write_bytes(b"abc")
            self.assertEqual(
                file_sha256(path),
                "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_missing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(OSError):
                file_sha256(Path(temp_dir) / "nope.bin")


class WhitelistTests(unittest.TestCase):
    def test_registers_real_hashes_for_calc_and_notepad(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_fake_app(root / "System32", "calc.exe", content=b"calc")
            make_fake_app(root / "System32", "notepad.exe", content=b"notepad")
            specs = build_application_whitelist(system_root=str(root))
            self.assertEqual(set(specs), {"calc", "notepad"})
            for application_id, spec in specs.items():
                with self.subTest(application_id=application_id):
                    self.assertIsNotNone(spec.executable_hash)
                    self.assertTrue(spec.executable_hash.startswith("sha256:"))
                    self.assertEqual(spec.executable_hash, file_sha256(spec.executable_path))

    def test_missing_executable_raises_a_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_fake_app(root / "System32", "calc.exe")
            with self.assertRaises(TaskRuntimeError) as ctx:
                build_application_whitelist(system_root=str(root))
            self.assertEqual(ctx.exception.code, "application_unavailable")


class TargetStateHasherTests(unittest.TestCase):
    def _specs(self, root: Path):
        make_fake_app(root / "System32", "calc.exe", content=b"calc-v1")
        make_fake_app(root / "System32", "notepad.exe", content=b"notepad-v1")
        return build_application_whitelist(system_root=str(root))

    @staticmethod
    def _plan(application_id: str, tool: str = "start_whitelisted_application"):
        return SimpleNamespace(
            actions=[SimpleNamespace(tool_name=tool, normalized_arguments={"application_id": application_id})]
        )

    def test_hash_is_stable_and_depends_on_executable_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            specs = self._specs(root)
            hasher = launch_target_state_hasher(specs)
            first = hasher(self._plan("calc"))
            self.assertEqual(first, hasher(self._plan("calc")))
            self.assertNotEqual(first, hasher(self._plan("notepad")))
            # 执行前 exe 被替换 ⇒ 哈希变化（预检会据此拒绝）
            (root / "System32" / "calc.exe").write_bytes(b"calc-v2")
            self.assertNotEqual(first, hasher(self._plan("calc")))

    def test_non_launch_action_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            specs = self._specs(Path(temp_dir))
            hasher = launch_target_state_hasher(specs)
            with self.assertRaises(ValueError):
                hasher(self._plan("calc", tool="write_text_file"))

    def test_unknown_application_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            specs = self._specs(Path(temp_dir))
            hasher = launch_target_state_hasher(specs)
            with self.assertRaises(ValueError):
                hasher(self._plan("ghost"))


class DispatcherTests(unittest.TestCase):
    def _dispatcher(self, temp_dir: Path) -> tuple[TextTaskDispatcher, _Factory]:
        root = Path(temp_dir)
        make_fake_app(root / "System32", "calc.exe")
        make_fake_app(root / "System32", "notepad.exe")
        specs = build_application_whitelist(system_root=str(root))
        # 替身编排器的时钟（授权快照 consumed_at / 预检 checked_at）必须晚于派发器时钟
        factory = _Factory(clock_value="2026-10-13T13:02:00Z")
        return TextTaskDispatcher(
            previewer=previewer(),
            orchestrator_factory=factory,
            target_state_hasher=lambda plan: HASH_A,  # 与替身预检门的期望一致
            applications=specs,
            clock=lambda: "2026-10-13T13:01:00Z",
        ), factory

    def test_handles_only_task_and_dangerous_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, _ = self._dispatcher(temp_dir)
            self.assertTrue(dispatcher.handles(make_handoff("打开计算器")))
            chat_handoff = LocalTaskRouter().route(
                RouteInput(
                    raw_text="你好",
                    trace=RouteTrace(
                        request_id="req-1", conversation_id="conv-1", turn_id="turn-1",
                        source="text_shell", raw_text="你好", created_at="2026-10-13T00:00:00Z",
                    ),
                )
            )
            self.assertIsNone(chat_handoff)  # 聊天由 fallback 处理，不归任务派发
            self.assertFalse(dispatcher.handles(object()))

    def test_confirmed_run_executes_through_the_injected_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            seen: list[str] = []

            def reader(plan_text: str) -> str:
                seen.append(plan_text)
                return "确认"

            result = dispatcher.run(make_handoff("打开计算器"), confirm_reader=reader)
            self.assertEqual(result.status, STATUS_EXECUTED)
            self.assertTrue(seen and seen[0].strip())
            self.assertEqual(len(factory.backends), 1)
            self.assertEqual(factory.backends[0].calls, 1)

    def test_non_confirmation_run_denies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            result = dispatcher.run(make_handoff("打开计算器"), confirm_reader=lambda plan_text: "嗯")
            self.assertEqual(result.status, STATUS_DENIED)
            self.assertEqual(factory.backends[0].calls, 0)

    def test_run_rejects_bad_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, _ = self._dispatcher(temp_dir)
            with self.assertRaises(TypeError):
                dispatcher.run("不是交接", confirm_reader=lambda plan_text: "确认")  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                dispatcher.run(make_handoff("打开计算器"), confirm_reader="确认")  # type: ignore[arg-type]

    def test_requires_typed_components(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, _ = self._dispatcher(temp_dir)
            with self.assertRaises(TypeError):
                TextTaskDispatcher(
                    previewer="nope",  # type: ignore[arg-type]
                    orchestrator_factory=dispatcher.orchestrator_factory,
                    target_state_hasher=dispatcher.target_state_hasher,
                    applications=dispatcher.applications,
                )


class AssemblyTests(unittest.TestCase):
    def test_build_text_task_dispatcher_assembles_with_a_temp_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            make_fake_app(root / "System32", "calc.exe")
            make_fake_app(root / "System32", "notepad.exe")
            specs = build_application_whitelist(system_root=str(root))
            dispatcher = build_text_task_dispatcher(
                data_root=root,
                audit_root=root / "audit",
                applications=specs,
            )
            self.assertTrue(dispatcher.handles(make_handoff("打开计算器")))
            self.assertTrue((root / "audit").is_dir())

    def test_missing_whitelist_fails_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(TaskRuntimeError):
                build_text_task_dispatcher(data_root=Path(temp_dir), system_root=str(Path(temp_dir)))


class TwoStepDispatcherTests(unittest.TestCase):
    """批次 3：语音"独立确认轮"用的两步 API 必须长在**注入进去的那个对象**上。

    真机教训（2026-10-13）：`prepare/execute/cancel` 只加在 `TaskDispatchSession` 上，
    而语音注入的是 `TextTaskDispatcher` ⇒ 会话构造直接报
    「dispatcher 必须提供 prepare()」，整条语音输入被降级。
    """

    def test_dispatcher_satisfies_the_voice_turn_contract(self) -> None:
        from app.runtime.orchestration.pending_confirmation import PendingConfirmationRouter
        from app.runtime.orchestration.voice_task_turn import KIND_AWAITING, VoiceTaskTurn

        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            router = PendingConfirmationRouter(
                first=LocalTaskRouter(), fallback=_StubFallback()
            )
            turn = VoiceTaskTurn(router=router, dispatcher=dispatcher)  # 真机就是这一步炸的
            router._last_handoff = make_handoff("打开计算器")  # noqa: SLF001
            outcomes = turn.handle(router.last_handoff)
            self.assertEqual([item.kind for item in outcomes], [KIND_AWAITING])
            self.assertEqual(factory.backends[0].calls, 0)
            self.assertIsNotNone(turn.pending)

    def test_prepare_execute_cancel_through_the_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            preparation = dispatcher.prepare(make_handoff("打开计算器"))
            self.assertTrue(preparation.ok)
            prepared = preparation.prepared
            self.assertIsNotNone(prepared)
            self.assertEqual(factory.backends[0].calls, 0)  # 冻结 ≠ 执行

            denied = dispatcher.execute(prepared)  # 不给确认词 ⇒ fail closed
            self.assertEqual(denied.status, STATUS_DENIED)
            self.assertEqual(factory.backends[0].calls, 0)

            dispatched = dispatcher.prepare(make_handoff("打开计算器"))
            executed = dispatcher.execute(dispatched.prepared, answer="确认")
            self.assertEqual(executed.status, STATUS_EXECUTED)
            # 每次 prepare 都会新建一台编排器（各自带假 backend）⇒ 跨全部编排器恰好执行一次
            self.assertEqual(sum(item.calls for item in factory.backends), 1)

    def test_cancel_then_reuse_is_stale_and_never_executes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            prepared = dispatcher.prepare(make_handoff("打开计算器")).prepared
            cancelled = dispatcher.cancel(prepared)
            self.assertEqual(cancelled.status, STATUS_DENIED)
            self.assertEqual(cancelled.text, DENIED_TEXT)
            again = dispatcher.execute(prepared, answer="确认")
            self.assertEqual(again.reason_code, REASON_STALE_PREPARATION)
            self.assertEqual(factory.backends[0].calls, 0)

    def test_execute_without_prepare_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            self.assertEqual(
                dispatcher.execute(None, answer="确认").reason_code, REASON_STALE_PREPARATION
            )
            self.assertEqual(dispatcher.cancel(None).reason_code, REASON_STALE_PREPARATION)
            self.assertEqual(factory.backends, [])

    def test_unsupported_handoff_keeps_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, factory = self._dispatcher(temp_dir)
            preparation = dispatcher.prepare(make_handoff("打开微信"))
            self.assertFalse(preparation.ok)
            self.assertIsNone(dispatcher.session)
            self.assertEqual(factory.orchestrators, [])

    def test_notice_explains_unsupported_routes_only(self) -> None:
        """P10 · T1 #12：`TextTaskDispatcher.notice()` 只对 `unsupported` 路由给如实说明。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            dispatcher, _factory = self._dispatcher(temp_dir)
            handoff = LocalTaskRouter().route(
                RouteInput(
                    raw_text="关闭计算器",
                    trace=RouteTrace(
                        request_id="req-1", conversation_id="conv-1", turn_id="turn-1",
                        source="text_shell", raw_text="关闭计算器",
                        created_at="2026-10-13T00:00:00Z",
                    ),
                )
            )
            assert handoff is not None and handoff.route == "unsupported"
            notice = dispatcher.notice(handoff)
            self.assertIsNotNone(notice)
            self.assertIn("本批暂不支持", notice or "")
            self.assertIn("close_application", notice or "")
            self.assertIsNone(dispatcher.notice(make_handoff("打开计算器")))
            self.assertIsNone(dispatcher.notice(None))

    def _dispatcher(self, temp_dir: str):
        root = Path(temp_dir)
        make_fake_app(root / "System32", "calc.exe")
        make_fake_app(root / "System32", "notepad.exe")
        specs = build_application_whitelist(system_root=str(root))
        factory = _Factory(clock_value="2026-10-13T13:02:00Z")
        return TextTaskDispatcher(
            previewer=previewer(),
            orchestrator_factory=factory,
            target_state_hasher=lambda plan: HASH_A,
            applications=specs,
            clock=lambda: "2026-10-13T13:01:00Z",
        ), factory


class _StubFallback:
    def route(self, request):  # noqa: ANN001, ANN201 - 测试替身
        return SimpleNamespace(route="chat")


class TaskAuditTests(unittest.TestCase):
    """真机教训（2026-10-13）：任务执行审计必须能**跨进程重复追加**。

    `AuditStore` 强制"同一审计文件只能来自同一个 `source_instance_id`"；装配层原来每次启动
    都生成随机 id ⇒ 第二次执行必然 `audit_append_failed`（执行被安全中止、计算器没打开）。
    """

    def _specs(self, root: Path):
        make_fake_app(root / "System32", "calc.exe")
        make_fake_app(root / "System32", "notepad.exe")
        return build_application_whitelist(system_root=str(root))

    def test_two_runs_can_both_append_to_the_task_audit_file(self) -> None:
        from app.runtime.task_runtime import TASK_AUDIT_FILENAME, TASK_AUDIT_SOURCE_INSTANCE

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            specs = self._specs(root)
            for index in range(2):
                dispatcher = build_text_task_dispatcher(
                    data_root=root, audit_root=root / "audit", applications=specs
                )
                assert dispatcher.audit is not None
                dispatcher.audit.append(
                    "execution_started",
                    subject_id=f"execution-{index}",
                    details={"action_count": 1},
                    created_at="2026-10-13T13:00:00Z",
                )
            path = root / "audit" / TASK_AUDIT_FILENAME
            self.assertTrue(path.is_file())
            self.assertEqual(
                sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), 2
            )
            self.assertEqual(TASK_AUDIT_SOURCE_INSTANCE, "task-runtime")

    def test_a_random_source_instance_would_break_the_shared_file(self) -> None:
        """复现旧 bug 的机制：换一个来源实例再追加 ⇒ 存储层直接拒绝。"""

        from app.auditing import AuditPersistenceError, AuditStore

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = AuditStore(root, source_instance_id="run-aaaa")
            first.append("execution_started", subject_id="execution-1", details={}, created_at="2026-10-13T13:00:00Z")
            second = AuditStore(root, source_instance_id="run-bbbb")
            with self.assertRaises(AuditPersistenceError):
                second.append(
                    "execution_started", subject_id="execution-2", details={},
                    created_at="2026-10-13T13:01:00Z",
                )


class TimestampTests(unittest.TestCase):
    def test_formats_z_suffixed_utc(self) -> None:
        from datetime import datetime, timezone

        self.assertEqual(
            utc_timestamp(datetime(2026, 10, 13, 13, 1, 0, tzinfo=timezone.utc)),
            "2026-10-13T13:01:00Z",
        )


if __name__ == "__main__":
    unittest.main()
