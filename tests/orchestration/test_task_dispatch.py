"""P10 · T1 批次 2a 红测：**任务派发**（预览 → 确认 → 授权 → 执行 → 汇报）。

全部离线：复用 `tests.orchestration.test_task_flow` 的假 backend/verifier/audit 与真实
ToolRegistry/PolicyEngine/L2 控制器/预检门/受控执行器；不联网、不启动任何进程。
"""

from __future__ import annotations

import unittest

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus, SlotCandidate
from app.routing import RouteInput
from app.runtime.orchestration import (
    TaskDispatchRequest,
    TaskDispatchSession,
    is_confirmation,
)
from app.runtime.orchestration.task_dispatch import (
    DENIED_TEXT,
    PREVIEW_FAILED_TEXT,
    REASON_CONFIRM_UNAVAILABLE,
    REASON_DENIED,
    REASON_PREPARED,
    REASON_STALE_PREPARATION,
    REASON_TIMEOUT,
    SPOKEN_UNSUPPORTED_TEXT,
    STATUS_DENIED,
    STATUS_EXECUTED,
    STATUS_PREVIEW_FAILED,
    STATUS_READY,
    STATUS_UNSUPPORTED,
    TaskDispatchError,
    TaskPreparation,
)
from app.runtime.orchestration.local_task_router import LocalTaskRouter
from app.runtime.orchestration.task_flow import TaskFlowState, TaskPlanPreviewer
from app.runtime.orchestration.voice_lines import VoiceLines
from app.safety import PolicyEngine
from app.tools import ToolRegistry
from tests.orchestration.test_task_flow import (
    CREATED_AT,
    DECIDED_AT,
    EXPIRES_AT,
    HASH_A,
    FakeAudit,
    FakeBackend,
    FakeVerifier,
    build_orchestrator,
    previewer,
)
from app.executor import VerificationDecision


def make_handoff(text: str) -> RouteHandoff:
    trace = RouteTrace(
        request_id="req-1",
        conversation_id="conv-1",
        turn_id="turn-1",
        source="text_shell",
        raw_text=text,
        created_at=CREATED_AT,
    )
    routed = LocalTaskRouter().route(RouteInput(raw_text=text, trace=trace))
    assert routed is not None
    return routed


def unsupported_handoff(
    *,
    route: str = "task",
    intent: str = "file_operation",
    handling_mode: str = "plan_then_execute",
    tool_scope: tuple[str, ...] = ("create_text_file",),
) -> RouteHandoff:
    trace = RouteTrace(
        request_id="req-1",
        conversation_id="conv-1",
        turn_id="turn-1",
        source="text_shell",
        raw_text="随便写点文件",
        created_at=CREATED_AT,
    )
    return RouteHandoff(
        trace=trace,
        route=route,
        intent=intent,
        handling_mode=handling_mode,
        confidence=0.95,
        slots={
            "target_path": SlotCandidate(value="x.txt", source="utterance", confidence=0.95, confirmed=False)
        },
        required_confirmation="defer_to_safety",
        tool_scope=tool_scope,
        router=RouterStatus(
            provider="local", model_id="task-rule-router", model_version="1", status="success",
            failure_code=None,
        ),
    )


def make_request(*, handoff: RouteHandoff, scope: tuple[str, ...] = ("application-launch",)) -> TaskDispatchRequest:
    return TaskDispatchRequest(
        handoff=handoff,
        task_id="task-1",
        plan_id="plan-1",
        goal="打开计算器",
        scope=scope,
        created_at=CREATED_AT,
        session_id="session-1",
        operation_id="operation-1",
        authorization_id="authorization-1",
        confirmation_window_id="window-1",
        expires_at=EXPIRES_AT,
        execution_id="execution-1",
        executor_instance_id="executor-1",
        result_id="result-1",
    )


class _Factory:
    """每次调用新建一个单任务编排器（并记录假 backend / audit 以便断言）。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.orchestrators: list[object] = []
        self.backends: list[FakeBackend] = []
        self.audits: list[object] = []

    def __call__(self):
        orchestrator, backend, audit = build_orchestrator(**self.kwargs)  # type: ignore[arg-type]
        self.orchestrators.append(orchestrator)
        self.backends.append(backend)
        self.audits.append(audit)
        return orchestrator


def build_session(reader, *, factory=None, plan_previewer=None, voice_lines=None) -> tuple[TaskDispatchSession, _Factory]:
    factory = factory or _Factory()
    session = TaskDispatchSession(
        previewer=plan_previewer or previewer(),
        orchestrator_factory=factory,
        target_state_hasher=lambda preview: HASH_A,
        confirm_reader=reader,
        clock=lambda: DECIDED_AT,
        voice_lines=voice_lines,
    )
    return session, factory


class IsConfirmationTests(unittest.TestCase):
    def test_confirmation_leading_short_sentences_are_allowed(self) -> None:
        for text in ("确认", " 确认 ", "确认。", "确认！", "　确认　", "确认执行", "确认一下", "确认吧", "确认了"):
            with self.subTest(text=text):
                self.assertTrue(is_confirmation(text))

    def test_questions_negations_and_long_sentences_are_refused(self) -> None:
        for text in (
            "请确认", "确认吗", "确认一下吗", "确认取消", "我不确认", "确认不了",
            "确认执行打开计算器",  # 超过 6 字：不允许把新任务句当确认
            "好的", "可以", "嗯", "yes", "OK", "", "  ", None, 1, True,
        ):
            with self.subTest(text=text):
                self.assertFalse(is_confirmation(text))


class TaskDispatchHappyPathTests(unittest.TestCase):
    def test_confirmed_launch_executes_exactly_one_action(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_EXECUTED)
        self.assertTrue(result.executed)
        self.assertEqual(len(factory.backends), 1)
        self.assertEqual(factory.backends[0].calls, 1)
        self.assertIsNotNone(result.plan_text)
        self.assertTrue(result.text)
        self.assertTrue(result.plan_hash)
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)
        # 受控执行器的审计事件（任务流自身的 `task_*` 诊断走 diagnostic_sink，本测试未注入）。
        self.assertIn("execution_finished", factory.audits[0].events)

    def test_confirmed_launch_reads_the_plan_before_asking(self) -> None:
        seen: list[str] = []

        def reader(plan_text: str) -> str:
            seen.append(plan_text)
            return "确认"

        session, _ = build_session(reader)
        session.handle(make_request(handoff=make_handoff("打开记事本")))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].strip())

    def test_single_task_orchestrator_is_rebuilt_for_the_next_turn(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        first = session.handle(make_request(handoff=make_handoff("打开计算器")))
        second = session.handle(make_request(handoff=make_handoff("打开记事本")))
        self.assertEqual((first.status, second.status), (STATUS_EXECUTED, STATUS_EXECUTED))
        self.assertEqual(len(factory.orchestrators), 2)
        self.assertIsNot(factory.orchestrators[0], factory.orchestrators[1])
        for orchestrator in factory.orchestrators:
            self.assertIs(orchestrator.state, TaskFlowState.TERMINAL)


class SpokenTextTests(unittest.TestCase):
    """#14 真机反馈（"不要念全文"）：`spoken_text` 是给**语音**的短句；`text` 保留完整计划/结果。"""

    def test_preparation_and_result_carry_short_spoken_text(self) -> None:
        session, _ = build_session(lambda plan_text: "确认")
        preparation = session.prepare(make_request(handoff=make_handoff("打开计算器")))
        self.assertTrue(preparation.ok)
        # 2026-10-14 真机：计划句从 22 字缩到 11 字（旧句念 6.1 s；"其它回答取消"也与实现不符）。
        self.assertEqual(preparation.spoken_text, "要打开计算器，说确认。")
        self.assertIn("start_whitelisted_application", preparation.text)
        self.assertIn("风险等级 L2", preparation.text)

        prepared = preparation.prepared
        assert prepared is not None
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.status, STATUS_EXECUTED)
        self.assertEqual(result.spoken_text, "帮你打开计算器了。")
        self.assertIn("共 1 个动作", result.text)

    def test_unsupported_task_has_a_short_spoken_text(self) -> None:
        session, _ = build_session(lambda plan_text: "确认")
        preparation = session.prepare(make_request(handoff=unsupported_handoff()))
        self.assertFalse(preparation.ok)
        self.assertEqual(preparation.status, STATUS_UNSUPPORTED)
        self.assertEqual(preparation.spoken_text, SPOKEN_UNSUPPORTED_TEXT)
        self.assertIn(preparation.reason_code, preparation.text)  # 短码只留在完整文本里

    def test_denied_result_keeps_its_short_text_without_spoken_override(self) -> None:
        session, _ = build_session(lambda plan_text: "好的")
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_DENIED)
        self.assertIsNone(result.spoken_text)  # 已经够短：语音直接用 `text`
        self.assertEqual(result.text, DENIED_TEXT)

    def test_injected_voice_lines_change_only_the_spoken_text(self) -> None:
        """#16 台词出口：拟人化只换话术，完整文本（控制台/审计）逐字不变。"""

        lines = VoiceLines(
            plan="我这就帮你打开{target}，可以吗？",
            result="好的，已为你执行。",
            unsupported="这项暂时不行。",
        )
        session, _ = build_session(lambda plan_text: "确认", voice_lines=lines)

        preparation = session.prepare(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(preparation.spoken_text, "我这就帮你打开计算器，可以吗？")
        self.assertIn("start_whitelisted_application", preparation.text)
        self.assertIn("风险等级 L2", preparation.text)

        prepared = preparation.prepared
        assert prepared is not None
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.spoken_text, "好的，已为你执行。")
        self.assertIn("共 1 个动作", result.text)

        unsupported, _ = build_session(lambda plan_text: "确认", voice_lines=lines)
        rejected = unsupported.prepare(make_request(handoff=unsupported_handoff()))
        self.assertEqual(rejected.spoken_text, "这项暂时不行。")

    def test_injected_voice_lines_must_be_voice_lines(self) -> None:
        with self.assertRaises(TypeError):
            build_session(lambda plan_text: "确认", voice_lines="不是台词")


class IdentityMismatchVerifier:
    """2026-10-14 真机：Win11 记事本是应用商店版启动器 ⇒ 身份校验必然不一致。"""

    def verify(self, *, action, attempt_id: str, output, backup, verified_at: str):
        return VerificationDecision(
            status="state_uncertain",
            facts={"observed_executable": r"C:\Program Files\WindowsApps\Notepad.exe"},
            error_code="application_identity_mismatch",
            error_message="进程可执行文件身份与白名单不一致",
        )


class IdentityUncertainSpeechTests(unittest.TestCase):
    """文案 A（2026-10-14 真机）：身份验证没通过时，语音只念一句可解释的如实说明。"""

    def test_identity_mismatch_is_spoken_short_while_the_full_text_is_kept(self) -> None:
        factory = _Factory(verifier=IdentityMismatchVerifier())
        session, _ = build_session(lambda plan_text: "确认", factory=factory)
        preparation = session.prepare(make_request(handoff=make_handoff("打开记事本")))
        prepared = preparation.prepared
        assert prepared is not None
        result = session.execute(prepared, answer="确认")

        self.assertEqual(result.status, "state_uncertain")
        self.assertEqual(
            result.spoken_text,
            "已启动记事本，但身份验证没通过（可能是应用商店版本），没有自动重试。",
        )
        # 完整结构化汇报（含错误码）仍留给控制台与审计。
        self.assertIn("application_identity_mismatch", result.text)
        self.assertNotEqual(result.spoken_text, result.text)

    def test_other_uncertain_results_keep_the_full_text(self) -> None:
        factory = _Factory(verifier=FakeVerifier(status="state_uncertain"))
        session, _ = build_session(lambda plan_text: "确认", factory=factory)
        prepared = session.prepare(make_request(handoff=make_handoff("打开记事本"))).prepared
        assert prepared is not None
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.status, "state_uncertain")
        self.assertIsNone(result.spoken_text)  # 其它不确定：维持原口径（语音用完整文本）

    def test_injected_voice_lines_change_only_the_short_line(self) -> None:
        lines = VoiceLines(uncertain_identity="打开了{target}，但没能核实身份。")
        factory = _Factory(verifier=IdentityMismatchVerifier())
        session, _ = build_session(lambda plan_text: "确认", factory=factory, voice_lines=lines)
        prepared = session.prepare(make_request(handoff=make_handoff("打开记事本"))).prepared
        assert prepared is not None
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.spoken_text, "打开了记事本，但没能核实身份。")
        self.assertIn("application_identity_mismatch", result.text)


class TaskDispatchDeniedTests(unittest.TestCase):
    def test_other_answer_denies_without_any_execution(self) -> None:
        session, factory = build_session(lambda plan_text: "好的")
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_DENIED)
        self.assertEqual(result.reason_code, REASON_DENIED)
        self.assertEqual(result.text, DENIED_TEXT)
        self.assertEqual(len(factory.backends), 1)
        self.assertEqual(factory.backends[0].calls, 0)  # 授权前零执行
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)

    def test_timeout_denies(self) -> None:
        def reader(plan_text: str) -> str:
            raise TimeoutError("15 秒内没有确认")

        session, factory = build_session(reader)
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_DENIED)
        self.assertEqual(result.reason_code, REASON_TIMEOUT)
        self.assertEqual(factory.backends[0].calls, 0)

    def test_reader_failure_denies(self) -> None:
        def reader(plan_text: str) -> str:
            raise RuntimeError("麦克风炸了")

        session, factory = build_session(reader)
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_DENIED)
        self.assertEqual(result.reason_code, REASON_CONFIRM_UNAVAILABLE)
        self.assertEqual(factory.backends[0].calls, 0)

    def test_empty_answer_denies(self) -> None:
        session, factory = build_session(lambda plan_text: "")
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_DENIED)
        self.assertEqual(factory.backends[0].calls, 0)


class TaskDispatchFailClosedTests(unittest.TestCase):
    def test_file_operation_intent_is_unsupported_and_never_builds_an_orchestrator(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        result = session.handle(make_request(handoff=unsupported_handoff()))
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertEqual(result.reason_code, "task_route_unsupported")
        self.assertEqual(factory.orchestrators, [])

    def test_dangerous_route_is_unsupported(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        result = session.handle(
            make_request(
                handoff=unsupported_handoff(
                    route="dangerous",
                    intent="delete_file",
                    handling_mode="plan_then_safety",
                    tool_scope=("move_to_recycle_bin",),
                )
            )
        )
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertEqual(result.reason_code, "task_route_unsupported")
        self.assertEqual(factory.orchestrators, [])

    def test_unregistered_application_is_unsupported(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        result = session.handle(make_request(handoff=make_handoff("打开微信")))
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertEqual(result.reason_code, "application_unknown")
        self.assertEqual(factory.orchestrators, [])

    def test_preview_failure_is_reported_and_nothing_executes(self) -> None:
        registry = ToolRegistry(())
        broken_previewer = TaskPlanPreviewer(
            registry=registry,
            policy_engine=PolicyEngine(registry, path_revalidator=lambda value: value),
            application_resolver=lambda application_id: "计算器",
        )
        session, factory = build_session(lambda plan_text: "确认", plan_previewer=broken_previewer)
        result = session.handle(make_request(handoff=make_handoff("打开计算器")))
        self.assertEqual(result.status, STATUS_PREVIEW_FAILED)
        self.assertEqual(result.reason_code, "tool_not_registered")
        self.assertEqual(result.text, PREVIEW_FAILED_TEXT)
        self.assertEqual(factory.orchestrators, [])


class TwoStepApiTests(unittest.TestCase):
    """批次 3：语音"独立确认轮"用的 prepare/execute/cancel 两步 API。"""

    def test_prepare_freezes_without_executing(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        preparation = session.prepare(make_request(handoff=make_handoff("打开计算器")))
        self.assertTrue(preparation.ok)
        self.assertEqual(preparation.status, STATUS_READY)
        self.assertIsNotNone(preparation.prepared)
        self.assertTrue(preparation.text.strip())  # 可播报的计划摘要
        self.assertEqual(len(factory.backends), 1)
        self.assertEqual(factory.backends[0].calls, 0)  # 冻结 ≠ 执行
        self.assertIs(session.outstanding, preparation.prepared)

    def test_execute_requires_a_real_confirmation_word(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        preparation = session.prepare(make_request(handoff=make_handoff("打开计算器")))
        denied = session.execute(preparation.prepared)  # 不给 answer ⇒ fail closed
        self.assertEqual(denied.status, STATUS_DENIED)
        self.assertEqual(factory.backends[0].calls, 0)

        session2, factory2 = build_session(lambda plan_text: "确认")
        prepared2 = session2.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        denied2 = session2.execute(prepared2, answer="好的")
        self.assertEqual(denied2.status, STATUS_DENIED)
        self.assertEqual(factory2.backends[0].calls, 0)

    def test_execute_after_confirmation_runs_exactly_once(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        prepared = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.status, STATUS_EXECUTED)
        self.assertEqual(factory.backends[0].calls, 1)
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)
        self.assertIsNone(session.outstanding)

    def test_stale_preparation_cannot_be_reused(self) -> None:
        session, _ = build_session(lambda plan_text: "确认")
        prepared = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        session.cancel(prepared)
        with self.assertRaises(TaskDispatchError) as ctx:
            session.execute(prepared, answer="确认")
        self.assertEqual(ctx.exception.code, REASON_STALE_PREPARATION)

    def test_cancel_retires_the_orchestrator(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        prepared = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        result = session.cancel(prepared)
        self.assertEqual(result.status, STATUS_DENIED)
        self.assertEqual(result.text, DENIED_TEXT)
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)
        self.assertEqual(factory.backends[0].calls, 0)

    def test_new_prepare_retires_the_previous_plan(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        first = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        second = session.prepare(make_request(handoff=make_handoff("打开记事本"))).prepared
        self.assertIsNot(first, second)
        self.assertIs(factory.orchestrators[0].state, TaskFlowState.TERMINAL)
        self.assertIs(session.outstanding, second)
        with self.assertRaises(TaskDispatchError):
            session.execute(first, answer="确认")

    def test_prepare_fail_closed_paths(self) -> None:
        session, factory = build_session(lambda plan_text: "确认")
        preparation = session.prepare(make_request(handoff=make_handoff("打开微信")))
        self.assertFalse(preparation.ok)
        self.assertEqual(preparation.reason_code, "application_unknown")
        self.assertIsNone(preparation.prepared)
        self.assertIsNone(session.outstanding)
        self.assertEqual(factory.orchestrators, [])

    def test_preparation_contract(self) -> None:
        with self.assertRaises(ValueError):
            TaskPreparation(ok=True, status=STATUS_READY, reason_code=REASON_PREPARED, text="x")
        with self.assertRaises(ValueError):
            TaskPreparation(
                ok=False, status=STATUS_DENIED, reason_code=REASON_DENIED, text="x", prepared="y"  # type: ignore[arg-type]
            )

    def test_execute_rejects_non_prepared(self) -> None:
        session, _ = build_session(lambda plan_text: "确认")
        with self.assertRaises(TypeError):
            session.execute("不是计划", answer="确认")  # type: ignore[arg-type]


class ExecutionFidelityTests(unittest.TestCase):
    """真机教训（2026-10-13）：执行失败/结果不确定**绝不能**被记成 executed。

    真机事实：审计追加失败 ⇒ 执行层给出 ``state_uncertain`` + "结果状态不确定"文案，
    而派发结果把状态写死成 ``executed``，日志与用户听到的事实互相矛盾（计算器也没打开）。
    """

    def test_audit_append_failure_surfaces_as_state_uncertain(self) -> None:
        session, _factory = build_session(
            lambda plan_text: "确认",
            factory=_Factory(audit=FakeAudit(fail_on="execution_started")),
        )
        prepared = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.status, "state_uncertain")
        self.assertEqual(result.reason_code, "audit_append_failed")
        self.assertFalse(result.executed)
        self.assertIn("状态不确定", result.text)

    def test_backend_failure_is_never_reported_as_executed(self) -> None:
        session, factory = build_session(
            lambda plan_text: "确认",
            factory=_Factory(backend=FakeBackend(fail=True, uncertain=True)),
        )
        prepared = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        result = session.execute(prepared, answer="确认")
        self.assertNotEqual(result.status, STATUS_EXECUTED)
        self.assertFalse(result.executed)
        self.assertEqual(factory.backends[0].calls, 1)  # 确实尝试过执行

    def test_success_still_reports_executed(self) -> None:
        session, _factory = build_session(lambda plan_text: "确认")
        prepared = session.prepare(make_request(handoff=make_handoff("打开计算器"))).prepared
        result = session.execute(prepared, answer="确认")
        self.assertEqual(result.status, STATUS_EXECUTED)
        self.assertTrue(result.executed)


class TaskDispatchContractTests(unittest.TestCase):
    def test_request_validation(self) -> None:
        handoff = make_handoff("打开计算器")
        with self.assertRaises(TypeError):
            TaskDispatchRequest(  # type: ignore[arg-type]
                handoff="不是交接", task_id="task-1", plan_id="plan-1", goal="g",
                scope=("s",), created_at=CREATED_AT, session_id="session-1", operation_id="operation-1",
                authorization_id="authorization-1", confirmation_window_id="window-1",
                expires_at=EXPIRES_AT, execution_id="execution-1",
                executor_instance_id="executor-1", result_id="result-1",
            )
        with self.assertRaises(ValueError):
            make_request(handoff=handoff, scope=())

    def test_handle_rejects_non_request(self) -> None:
        session, _ = build_session(lambda plan_text: "确认")
        with self.assertRaises(TypeError):
            session.handle("打开计算器")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
