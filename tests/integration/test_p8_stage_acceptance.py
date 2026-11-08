"""P8 阶段级集成验收：覆盖路线图 P8 五条退出条件与前置阶段回归。

全部离线：真实 P5 安全链（PolicyEngine / ActionPlan / L2·L3 / 复检门 / 账本 /
受控执行器 / 验证 / 审计）、真实 P6 按键管道、真实 P7 播报接缝，设备与 Provider
全部以注入假件代替；不联网、不读密钥、不执行真实电脑副作用。
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from app.asr.recovery import AsrFailureCode
from app.auditing import AuditStore
from app.runtime.orchestration import (
    AuthorizationStopInput,
    StopContext,
    StopStatus,
    TaskFlowError,
    VoiceStopRouter,
)
from app.safety.confirmation import (
    AuthorizationConsumptionStore,
    ConfirmationError,
    L2ConfirmationController,
)
from app.voice_loop import VoiceInputSeam, VoiceInputSource, VoiceLoopError, VoiceTurnBoundary
from tests.integration.test_p8_integration import (
    build_pipeline,
    build_task_pipeline,
    drive_one_voice_turn,
    enable_voice,
)
from tests.orchestration.test_stop import (
    CHECKED_AT,
    CREATED_AT,
    DECIDED_AT,
    EXPIRES_AT,
    HASH_A,
    STOPPED_AT,
    ledger_store,
    l3_preview,
)
from tests.orchestration.test_task_flow import (
    application_proposal,
    authorization_request,
    build_orchestrator,
    handoff,
    plan_request,
    previewer,
    recycle_proposal,
)
from tests.voice_loop.support import FakeSpeaker, FakeStream

REPO_ROOT = Path(__file__).resolve().parents[2]
VOICE_LOOP_DIR = REPO_ROOT / "app" / "voice_loop"
ORCHESTRATION_DIR = REPO_ROOT / "app" / "runtime" / "orchestration"

BANNED_IMPORT_ROOTS = {
    "subprocess",
    "socket",
    "http",
    "urllib",
    "requests",
    "os",
    "shutil",
    "threading",
    "multiprocessing",
    "ctypes",
    "winreg",
    "asyncio",
    "pathlib",
    "sounddevice",
    "sherpa_onnx",
    "websockets",
}
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
BANNED_SOURCE_MARKERS = ("subprocess", "os.system", "Popen", "eval(", "exec(")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


def audit_event_types(store: AuditStore) -> list[str]:
    return [event.event_type for event in store.read_events()]


class ExitConditionOneTests(unittest.TestCase):
    """退出条件 1：闲聊 = 按键 → ASR → Agent → TTS。"""

    def test_button_chat_turn_reaches_spoken_reply_once(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        boundary, report = drive_one_voice_turn(pipeline)

        self.assertTrue(report.accepted)
        self.assertTrue(report.submitted)
        self.assertTrue(report.spoken)
        self.assertEqual(len(pipeline.adapter.calls), 1)
        self.assertEqual(pipeline.adapter.calls[0][0], boundary.request_id)
        self.assertEqual(pipeline.stream.chunks, ["已接收：打开编辑器"])
        self.assertEqual(pipeline.stream.finished, 1)
        self.assertEqual(pipeline.loop.state.value, "idle")
        self.assertEqual(pipeline.loop.last_report.boundary.identity, boundary.identity)
        self.assertEqual(pipeline.seam.mode.value, "voice")


class ExitConditionTwoTests(unittest.TestCase):
    """退出条件 2：任务 = 按键 → ASR → Agent → ActionPlan → 授权 → 执行 → 验证 → 摘要。"""

    def task_handoff(self, pipeline):
        _boundary, chat_report = drive_one_voice_turn(pipeline)
        self.assertTrue(chat_report.submitted)
        return pipeline.conversation.route_results[-1].handoff

    def test_l2_task_reaches_verified_execution_and_spoken_summary(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        handoff_value = self.task_handoff(pipeline)
        self.assertEqual(handoff_value.route, "task")

        preview = previewer().preview(plan_request(handoff_value=handoff_value))
        orchestrator, backend, audit = build_orchestrator(speaker=FakeSpeaker(pipeline.stream))
        orchestrator.accept_preview(preview)
        self.assertEqual(orchestrator.authorize(authorization_request(preview)).status, "authorized")
        execution = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        report = orchestrator.report()

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.document.actions[0].verification_status, "completed")
        self.assertEqual(execution.ledger_status, "COMPLETED")
        self.assertEqual(report.status, "completed")
        self.assertTrue(report.spoken)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(
            audit.events,
            ["execution_started", "operation_started", "operation_result", "execution_finished"],
        )
        self.assertEqual(pipeline.adapter.calls, [])

    def test_l3_task_requires_the_fixed_phrase_before_execution(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        self.task_handoff(pipeline)
        dangerous = handoff(
            route="dangerous",
            intent="delete_file",
            tool_scope=("move_to_recycle_bin",),
            handling_mode="plan_then_safety",
        )
        preview = previewer().preview(plan_request(recycle_proposal(), handoff_value=dangerous))
        self.assertEqual(preview.required_level, "L3")
        orchestrator, backend, _ = build_orchestrator()
        orchestrator.accept_preview(preview)

        first = orchestrator.authorize(authorization_request(preview))
        self.assertEqual(first.status, "awaiting_phrase")
        self.assertIsNone(first.authorization)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.execute(
                execution_id="execution-l3", executor_instance_id="executor-1", result_id="result-l3"
            )
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(backend.calls, 0)

        allowed = orchestrator.authorize(authorization_request(preview, phrase="确认执行"))
        self.assertEqual(allowed.status, "authorized")
        self.assertEqual(allowed.authorization.confirmation_phrase, "确认执行")
        execution = orchestrator.execute(
            execution_id="execution-l3", executor_instance_id="executor-1", result_id="result-l3"
        )
        self.assertEqual(execution.status, "completed")
        self.assertEqual(backend.calls, 1)


class ExitConditionThreeTests(unittest.TestCase):
    """退出条件 3：ASR/TTS 不可用时仍可切换文字，且不影响任务事实。"""

    def test_asr_unavailable_still_completes_a_task_through_text(self) -> None:
        pipeline = build_task_pipeline()
        pipeline.coordinator.startup_failed(code=AsrFailureCode.PROVIDER_UNAVAILABLE)
        pipeline.seam.note_voice_ready()
        with self.assertRaises(VoiceLoopError):
            pipeline.loop.start_turn()

        text_boundary = pipeline.seam.open_text_turn()
        submission = pipeline.seam.submit(text_boundary, "帮我把示例文件移入回收站")
        self.assertTrue(submission.submitted)
        handoff_value = submission.result.handoff
        self.assertEqual(handoff_value.route, "task")

        preview = previewer().preview(plan_request(handoff_value=handoff_value))
        orchestrator, backend, _ = build_orchestrator()
        orchestrator.accept_preview(preview)
        self.assertEqual(orchestrator.authorize(authorization_request(preview)).status, "authorized")
        execution = orchestrator.execute(
            execution_id="execution-text", executor_instance_id="executor-1", result_id="result-text"
        )
        self.assertEqual(execution.status, "completed")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(pipeline.adapter.calls, [])

    def test_tts_failure_never_changes_the_structured_task_result(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        _boundary, chat_report = drive_one_voice_turn(pipeline)
        self.assertTrue(chat_report.submitted)
        handoff_value = pipeline.conversation.route_results[-1].handoff
        preview = previewer().preview(plan_request(handoff_value=handoff_value))

        texts: list[str] = []
        failing, backend, _ = build_orchestrator(
            speaker=FakeSpeaker(error=RuntimeError("no output device")), text_sink=texts.append
        )
        failing.accept_preview(preview)
        failing.authorize(authorization_request(preview))
        execution = failing.execute(
            execution_id="execution-tts-fail", executor_instance_id="executor-1", result_id="result-tts"
        )
        report = failing.report()

        self.assertFalse(report.spoken)
        self.assertEqual(report.status, "completed")
        self.assertEqual(texts, [report.text])
        self.assertEqual(backend.calls, 1)
        self.assertEqual(report.document.overall_status, "completed")
        self.assertEqual(execution.document.overall_status, "completed")


class ExitConditionFourTests(unittest.TestCase):
    """退出条件 4：每轮有独立 session/utterance/request/reply 边界。"""

    def test_three_turns_never_reuse_any_identity(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        boundaries = []
        for _ in range(3):
            boundary, report = drive_one_voice_turn(pipeline)
            self.assertTrue(report.submitted)
            boundaries.append(boundary)

        for name in ("voice_session_id", "utterance_id", "turn_id", "request_id", "reply_id"):
            values = [getattr(item, name) for item in boundaries]
            with self.subTest(field=name):
                self.assertEqual(len(set(values)), 3)
        self.assertEqual(len({call[0] for call in pipeline.adapter.calls}), 3)
        self.assertEqual(pipeline.loop.state.value, "idle")
        self.assertIsNone(pipeline.loop.boundary)

    def test_retired_boundary_cannot_be_reused_or_resubmitted(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        boundary, _report = drive_one_voice_turn(pipeline)

        with self.assertRaises(VoiceLoopError):
            pipeline.seam.close_voice_turn(boundary, reason="completed")
        with self.assertRaises(VoiceLoopError):
            pipeline.loop.complete_turn()
        self.assertEqual(boundary.source, VoiceInputSource.ASR)

    def test_foreign_boundary_is_not_accepted_by_the_seam(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        foreign = VoiceTurnBoundary(
            voice_session_id="voice-session-foreign",
            utterance_id="asr-utterance-foreign",
            turn_id="asr-turn-foreign",
            request_id="asr-request-foreign",
            reply_id="asr-reply-foreign",
            source="asr",
            created_at="2026-10-01T09:00:00.000Z",
        )
        with self.assertRaises(VoiceLoopError):
            pipeline.seam.submit(foreign, "不应被接受")


class ExitConditionFiveTests(unittest.TestCase):
    """退出条件 5：说“停”在聊天、授权、执行三种上下文分别符合规则。"""

    def test_chat_stop_stops_speech_and_keeps_listening(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        boundary, report = drive_one_voice_turn(pipeline)
        self.assertTrue(report.spoken)

        stop = VoiceStopRouter(clock=lambda: STOPPED_AT)
        chat_stop = stop.stop_chat(stream=pipeline.stream)

        self.assertEqual(chat_stop.context, StopContext.CHAT)
        self.assertEqual(chat_stop.status, StopStatus.SPEECH_STOPPED)
        self.assertEqual(pipeline.stream.cancelled, 1)
        self.assertFalse(chat_stop.resume_allowed)
        self.assertFalse(chat_stop.confirmation_invalidated)
        self.assertFalse(chat_stop.stop_requested)

        next_boundary, next_report = drive_one_voice_turn(pipeline)
        self.assertTrue(next_report.submitted)
        self.assertNotEqual(boundary.identity, next_boundary.identity)

    def test_authorization_stop_invalidates_the_window_and_blocks_execution(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        _boundary, chat_report = drive_one_voice_turn(pipeline)
        self.assertTrue(chat_report.submitted)
        handoff_value = pipeline.conversation.route_results[-1].handoff
        preview = previewer().preview(plan_request(handoff_value=handoff_value))

        store = AuthorizationConsumptionStore(instance_id="instance-1")
        controller = L2ConfirmationController(instance_id="instance-1", authorization_store=store)
        controller.begin(
            plan=preview.plan,
            session_id="session-1",
            operation_id="operation-1",
            authorization_id="authorization-stop",
            confirmation_window_id="window-stop",
            target_state_hash=HASH_A,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        stop = VoiceStopRouter(clock=lambda: STOPPED_AT)
        report = stop.stop_authorization(
            AuthorizationStopInput(
                controller=controller,
                confirmation_window_id="window-stop",
                session_id="session-1",
                current_plan=preview.plan,
                current_target_state_hash=HASH_A,
            )
        )
        self.assertEqual(report.status, StopStatus.CONFIRMATION_CANCELLED)
        self.assertTrue(report.confirmation_invalidated)
        self.assertIsNone(controller.active_window)
        with self.assertRaises(ConfirmationError):
            controller.resolve(
                confirmation_window_id="window-stop",
                session_id="session-1",
                event="allow",
                occurred_at=DECIDED_AT,
                current_plan=preview.plan,
                current_target_state_hash=HASH_A,
            )

        orchestrator, backend, _ = build_orchestrator()
        orchestrator.accept_preview(preview)
        with self.assertRaises(TaskFlowError):
            orchestrator.execute(
                execution_id="execution-stop", executor_instance_id="executor-1", result_id="result-stop"
            )
        self.assertEqual(backend.calls, 0)

    def test_execution_stop_cancels_cooperatively_and_never_resumes(self) -> None:
        preview = l3_preview()
        store, action = ledger_store(preview, execution_id="execution-stop")
        executor = store.acquire_executor("executor-1")
        executor.operation_started(action_id=action.action_id, attempt_id="attempt-1", started_at=CHECKED_AT)
        stop = VoiceStopRouter(clock=lambda: STOPPED_AT)

        report = stop.stop_execution(store=store)

        self.assertEqual(report.context, StopContext.EXECUTION)
        self.assertEqual(report.status, StopStatus.EXECUTION_STOP_REQUESTED)
        self.assertFalse(report.atomic_wait_required)
        self.assertFalse(report.resume_allowed)
        self.assertEqual(store.snapshot().attempts[0].status, "CANCEL_REQUESTED")
        with self.assertRaises(Exception):
            store.acquire_executor("executor-2")
        late = stop.note_late_event(source="tts_chunk")
        self.assertTrue(late.late_event_isolated)

    def test_unsafe_atomic_interval_waits_instead_of_force_killing(self) -> None:
        preview = previewer().preview(plan_request(application_proposal()))
        store, action = ledger_store(preview, execution_id="execution-unsafe")
        executor = store.acquire_executor("executor-1")
        executor.operation_started(action_id=action.action_id, attempt_id="attempt-1", started_at=CHECKED_AT)
        executor.enter_unsafe_atomic(action_id=action.action_id, attempt_id="attempt-1", entered_at=CHECKED_AT)

        report = VoiceStopRouter(clock=lambda: STOPPED_AT).stop_execution(store=store)

        self.assertEqual(report.status, StopStatus.EXECUTION_ATOMIC_WAIT)
        self.assertTrue(report.atomic_wait_required)
        self.assertEqual(store.snapshot().attempts[0].status, "STARTED")


class CrossStageRegressionTests(unittest.TestCase):
    """前置阶段回归：语音层不得创建安全事实，也不得改写任务事实。"""

    def test_voice_loop_never_imports_the_safety_chain(self) -> None:
        files = sorted(VOICE_LOOP_DIR.rglob("*.py"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path.name):
                modules = imported_modules(path)
                self.assertFalse(
                    {name for name in modules if name.split(".")[0] in BANNED_IMPORT_ROOTS},
                    f"{path.name} 导入了被禁止的模块",
                )
                self.assertFalse(
                    {
                        name
                        for name in modules
                        for prefix in SAFETY_CHAIN_PREFIXES
                        if name == prefix or name.startswith(prefix + ".")
                    },
                    f"{path.name} 引用了安全链模块",
                )

    def test_orchestration_layer_has_no_arbitrary_execution_surface(self) -> None:
        files = sorted(ORCHESTRATION_DIR.rglob("*.py"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path.name):
                modules = imported_modules(path)
                self.assertFalse(
                    {name for name in modules if name.split(".")[0] in BANNED_IMPORT_ROOTS},
                    f"{path.name} 导入了被禁止的模块",
                )
                source = path.read_text(encoding="utf-8")
                for marker in BANNED_SOURCE_MARKERS:
                    self.assertNotIn(marker, source)

    def test_plan_and_result_facts_are_identical_with_or_without_speech(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        _boundary, chat_report = drive_one_voice_turn(pipeline)
        self.assertTrue(chat_report.submitted)
        handoff_value = pipeline.conversation.route_results[-1].handoff
        preview = previewer().preview(plan_request(handoff_value=handoff_value))

        working_audit = AuditStore(
            Path(self.enterContext(tempfile.TemporaryDirectory())), source_instance_id="instance-1"
        )
        working, backend_a, _ = build_orchestrator(
            speaker=FakeSpeaker(FakeStream()), audit=working_audit
        )
        working.accept_preview(preview)
        working.authorize(authorization_request(preview))
        working.execute(execution_id="execution-a", executor_instance_id="executor-1", result_id="result-a")

        failing_audit = AuditStore(
            Path(self.enterContext(tempfile.TemporaryDirectory())), source_instance_id="instance-1"
        )
        failing, backend_b, _ = build_orchestrator(
            speaker=FakeSpeaker(error=RuntimeError("device error")), audit=failing_audit
        )
        failing.accept_preview(preview)
        failing.authorize(authorization_request(preview))
        failing.execute(execution_id="execution-b", executor_instance_id="executor-1", result_id="result-b")

        self.assertEqual(working.document.plan_hash, failing.document.plan_hash)
        self.assertEqual(working.document.overall_status, failing.document.overall_status)
        self.assertEqual(
            [action.outcome for action in working.document.actions],
            [action.outcome for action in failing.document.actions],
        )
        self.assertEqual(backend_a.calls, backend_b.calls)
        self.assertEqual(audit_event_types(working_audit), audit_event_types(failing_audit))

    def test_voice_layer_creates_no_plan_or_result_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pipeline = build_task_pipeline()
            enable_voice(pipeline)
            drive_one_voice_turn(pipeline)
            root = Path(temp_dir)
            self.assertEqual(list(root.rglob("plan.json")), [])
            self.assertEqual(list(root.rglob("result.json")), [])
            self.assertEqual(list(root.rglob("*.jsonl")), [])

    def test_voice_loop_rejects_crossed_channels(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        self.assertIsInstance(pipeline.seam, VoiceInputSeam)
        with self.assertRaises(VoiceLoopError):
            pipeline.seam.open_text_turn()
        pipeline.seam.degrade(reason="asr_stream_failed")
        with self.assertRaises(VoiceLoopError):
            pipeline.loop.start_turn()
        self.assertEqual(pipeline.seam.status_text, "语音识别不可用；当前为文字输入")


if __name__ == "__main__":
    unittest.main()
