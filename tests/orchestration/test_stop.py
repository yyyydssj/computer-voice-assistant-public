"""P8 批次 5 测试：一次“停”在聊天、授权、执行三种上下文的本地语义。

全部离线：真实 L2/L3 确认控制器、真实 ExecutionLedgerStore、注入式 TTS 取消端口；
不联网、不打开设备、不执行真实电脑副作用。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.contracts import AuthorizationSnapshot
from app.executor import ExecutionLedgerRegistry
from app.runtime.orchestration import (
    AuthorizationStopInput,
    StopContext,
    StopReport,
    StopStatus,
    VoiceStopRouter,
)
from app.safety.confirmation import (
    AuthorizationConsumptionStore,
    ConfirmationError,
    L2ConfirmationController,
    L3ConfirmationController,
)
from app.safety.preflight import ExecutionPreflightApproval, PreflightActionBoundary
from app.tools import build_mvp_tool_registry
from tests.orchestration.test_task_flow import (
    APPLICATION_ID,
    APPLICATION_NAME,
    CHECKED_AT,
    CREATED_AT,
    DECIDED_AT,
    EXPIRES_AT,
    HASH_A,
    application_proposal,
    handoff,
    plan_request,
    previewer,
    recycle_proposal,
)

STOPPED_AT = "2026-09-21T13:03:00Z"


class FakeStream:
    def __init__(self, *, error: Exception | None = None, cancelable: bool = True) -> None:
        self.cancels = 0
        self.error = error
        if not cancelable:
            self.cancel = None  # type: ignore[assignment]

    def cancel(self) -> None:
        self.cancels += 1
        if self.error is not None:
            raise self.error


def router(**overrides: object) -> VoiceStopRouter:
    values: dict[str, object] = {"clock": lambda: STOPPED_AT}
    values.update(overrides)
    return VoiceStopRouter(**values)  # type: ignore[arg-type]


def l2_preview():
    return previewer().preview(plan_request())


def l3_preview():
    item = handoff(
        route="dangerous",
        intent="delete_file",
        tool_scope=("move_to_recycle_bin",),
        handling_mode="plan_then_safety",
    )
    return previewer().preview(plan_request(recycle_proposal(), handoff_value=item))


def l2_window(preview, *, window_id: str = "window-stop"):
    store = AuthorizationConsumptionStore(instance_id="instance-1")
    controller = L2ConfirmationController(instance_id="instance-1", authorization_store=store)
    controller.begin(
        plan=preview.plan,
        session_id="session-1",
        operation_id="operation-1",
        authorization_id="authorization-stop",
        confirmation_window_id=window_id,
        target_state_hash=HASH_A,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
    )
    return controller


def approval_for(preview, *, risk_level: str, authorization_method: str, stage: str, phrase: str | None = None):
    registry = build_mvp_tool_registry()
    action = preview.plan.actions[0]
    authorization = AuthorizationSnapshot(
        authorization_id="authorization-exec",
        issuer_instance_id="instance-1",
        session_id="session-1",
        operation_id="operation-1",
        plan_id=preview.plan.plan_id,
        plan_version=preview.plan.plan_version,
        plan_hash=preview.plan.plan_hash,
        target_state_hash=HASH_A,
        allowed_action_ids=(action.action_id,),
        risk_level=risk_level,
        confirmation_window_id="window-exec",
        authorization_method=authorization_method,
        confirmation_stage=stage,
        confirmation_phrase=phrase,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        consumed_at=CHECKED_AT,
    )
    return ExecutionPreflightApproval(
        instance_id="instance-1",
        session_id="session-1",
        operation_id="operation-1",
        plan_id=preview.plan.plan_id,
        plan_version=preview.plan.plan_version,
        plan_hash=preview.plan.plan_hash,
        target_state_hash=HASH_A,
        action_ids=(action.action_id,),
        checked_at=CHECKED_AT,
        authorization=authorization,
        action_boundaries=(
            PreflightActionBoundary(
                action_id=action.action_id,
                tool_name=action.tool_name,
                manifest_hash=action.manifest_hash,
                cancellation_mode=registry.require(action.tool_name).cancellation,
            ),
        ),
    )


def ledger_store(preview, *, execution_id: str = "execution-1"):
    approval = approval_for(
        preview,
        risk_level="L2",
        authorization_method="modal",
        stage="modal",
    )
    return (
        ExecutionLedgerRegistry(instance_id="instance-1").create(
            execution_id=execution_id,
            approval=approval,
            created_at=CHECKED_AT,
        ),
        preview.plan.actions[0],
    )


class ContextResolutionTests(unittest.TestCase):
    def test_execution_has_priority_then_authorization_then_chat(self) -> None:
        resolve = VoiceStopRouter.resolve_context
        self.assertIs(
            resolve(execution_running=True, authorization_pending=True, speech_active=True),
            StopContext.EXECUTION,
        )
        self.assertIs(
            resolve(execution_running=False, authorization_pending=True, speech_active=True),
            StopContext.AUTHORIZATION,
        )
        self.assertIs(
            resolve(execution_running=False, authorization_pending=False, speech_active=True),
            StopContext.CHAT,
        )
        self.assertIs(
            resolve(execution_running=False, authorization_pending=False, speech_active=False),
            StopContext.IDLE,
        )

    def test_non_boolean_context_flags_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            VoiceStopRouter.resolve_context(
                execution_running=1,  # type: ignore[arg-type]
                authorization_pending=False,
                speech_active=False,
            )


class ChatStopTests(unittest.TestCase):
    def test_chat_stop_cancels_speech_and_keeps_listening(self) -> None:
        stream = FakeStream()
        report = router().stop_chat(stream=stream)
        self.assertEqual(report.context, StopContext.CHAT)
        self.assertEqual(report.status, StopStatus.SPEECH_STOPPED)
        self.assertTrue(report.speech_stopped)
        self.assertFalse(report.confirmation_invalidated)
        self.assertFalse(report.stop_requested)
        self.assertFalse(report.resume_allowed)
        self.assertEqual(stream.cancels, 1)

    def test_chat_stop_failure_never_claims_the_speech_stopped(self) -> None:
        stream = FakeStream(error=RuntimeError("device error"))
        report = router().stop_chat(stream=stream)
        self.assertEqual(report.status, StopStatus.SPEECH_STOP_UNCONFIRMED)
        self.assertFalse(report.speech_stopped)
        self.assertEqual(stream.cancels, 1)

    def test_chat_stop_without_a_cancel_port_is_a_noop(self) -> None:
        report = router().stop_chat(stream=object())
        self.assertEqual(report.status, StopStatus.NOTHING_ACTIVE)
        self.assertFalse(report.speech_stopped)

    def test_chat_stop_rejects_unknown_reasons(self) -> None:
        with self.assertRaises(ValueError):
            router().stop_chat(stream=FakeStream(), reason="please_stop")


class AuthorizationStopTests(unittest.TestCase):
    def test_l2_stop_cancels_the_window_and_blocks_any_authorization(self) -> None:
        preview = l2_preview()
        controller = l2_window(preview)
        report = router().stop_authorization(
            AuthorizationStopInput(
                controller=controller,
                confirmation_window_id="window-stop",
                session_id="session-1",
                current_plan=preview.plan,
                current_target_state_hash=HASH_A,
            )
        )
        self.assertEqual(report.context, StopContext.AUTHORIZATION)
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

    def test_l3_stop_at_the_phrase_stage_prevents_the_second_stage(self) -> None:
        preview = l3_preview()
        store = AuthorizationConsumptionStore(instance_id="instance-1")
        controller = L3ConfirmationController(instance_id="instance-1", authorization_store=store)
        window = controller.begin(
            plan=preview.plan,
            session_id="session-1",
            operation_id="operation-1",
            authorization_id="authorization-l3-stop",
            confirmation_window_id="window-l3-stop",
            target_state_hash=HASH_A,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        modal = controller.resolve(
            confirmation_window_id=window.confirmation_window_id,
            session_id="session-1",
            event="allow",
            occurred_at=DECIDED_AT,
            current_plan=preview.plan,
            current_target_state_hash=HASH_A,
        )
        self.assertEqual(modal.status, "awaiting_phrase")
        report = router().stop_authorization(
            AuthorizationStopInput(
                controller=controller,
                confirmation_window_id=window.confirmation_window_id,
                session_id="session-1",
                current_plan=preview.plan,
                current_target_state_hash=HASH_A,
            )
        )
        self.assertEqual(report.status, StopStatus.CONFIRMATION_CANCELLED)
        with self.assertRaises(ConfirmationError):
            controller.submit_phrase(
                confirmation_window_id=window.confirmation_window_id,
                session_id="session-1",
                phrase="确认执行",
                occurred_at="2026-09-21T13:02:30Z",
                current_plan=preview.plan,
                current_target_state_hash=HASH_A,
            )

    def test_stop_on_a_missing_window_is_fail_closed(self) -> None:
        preview = l2_preview()
        store = AuthorizationConsumptionStore(instance_id="instance-1")
        controller = L2ConfirmationController(instance_id="instance-1", authorization_store=store)
        report = router().stop_authorization(
            AuthorizationStopInput(
                controller=controller,
                confirmation_window_id="window-missing",
                session_id="session-1",
                current_plan=preview.plan,
                current_target_state_hash=HASH_A,
            )
        )
        self.assertEqual(report.status, StopStatus.ALREADY_STOPPED)
        self.assertTrue(report.confirmation_invalidated)
        self.assertFalse(report.resume_allowed)

    def test_wrong_session_is_a_refusal_not_a_cancel(self) -> None:
        preview = l2_preview()
        controller = l2_window(preview)
        report = router().stop_authorization(
            AuthorizationStopInput(
                controller=controller,
                confirmation_window_id="window-stop",
                session_id="session-1",
                current_plan=preview.plan,
                current_target_state_hash="sha256:" + "b" * 64,
            )
        )
        self.assertEqual(report.status, StopStatus.CONFIRMATION_CANCELLED)

    def test_stop_input_requires_validated_facts(self) -> None:
        with self.assertRaises(TypeError):
            AuthorizationStopInput(
                controller=object(),
                confirmation_window_id="window-stop",
                session_id="session-1",
                current_plan=l2_preview().plan,
                current_target_state_hash=HASH_A,
            )
        with self.assertRaises(TypeError):
            AuthorizationStopInput(
                controller=l2_window(l2_preview()),
                confirmation_window_id="window-stop",
                session_id="session-1",
                current_plan="plan",  # type: ignore[arg-type]
                current_target_state_hash=HASH_A,
            )


class ExecutionStopTests(unittest.TestCase):
    def test_stop_before_any_action_attempt_cancels_the_ledger(self) -> None:
        preview = l2_preview()
        store, _action = ledger_store(preview)
        report = router().stop_execution(store=store)
        self.assertEqual(report.context, StopContext.EXECUTION)
        self.assertEqual(report.status, StopStatus.EXECUTION_BLOCKED_BEFORE_START)
        self.assertTrue(report.stop_requested)
        self.assertFalse(report.atomic_wait_required)
        self.assertEqual(store.snapshot().ledger.status, "CANCELLED")

    def test_system_abort_before_any_attempt_records_aborted(self) -> None:
        preview = l2_preview()
        store, _action = ledger_store(preview)
        report = router().stop_execution(store=store, reason="system_aborted")
        self.assertEqual(report.status, StopStatus.EXECUTION_BLOCKED_BEFORE_START)
        self.assertEqual(store.snapshot().ledger.status, "ABORTED")

    def test_cooperative_action_gets_an_immediate_cancel_request(self) -> None:
        preview = l3_preview()
        store, action = ledger_store(preview)
        executor = store.acquire_executor("executor-1")
        executor.operation_started(action_id=action.action_id, attempt_id="attempt-1", started_at=CHECKED_AT)

        report = router().stop_execution(store=store)

        self.assertEqual(report.status, StopStatus.EXECUTION_STOP_REQUESTED)
        self.assertTrue(report.stop_requested)
        self.assertFalse(report.atomic_wait_required)
        snapshot = store.snapshot()
        self.assertEqual(snapshot.attempts[0].status, "CANCEL_REQUESTED")
        self.assertEqual(snapshot.stop_request.requested_at, STOPPED_AT)
        self.assertEqual(snapshot.stop_request.action_id, action.action_id)

    def test_unsafe_atomic_interval_defers_the_stop_without_killing(self) -> None:
        preview = l2_preview()
        store, action = ledger_store(preview)
        executor = store.acquire_executor("executor-1")
        executor.operation_started(action_id=action.action_id, attempt_id="attempt-1", started_at=CHECKED_AT)
        executor.enter_unsafe_atomic(action_id=action.action_id, attempt_id="attempt-1", entered_at=CHECKED_AT)

        report = router().stop_execution(store=store)

        self.assertEqual(report.status, StopStatus.EXECUTION_ATOMIC_WAIT)
        self.assertTrue(report.stop_requested)
        self.assertTrue(report.atomic_wait_required)
        snapshot = store.snapshot()
        self.assertEqual(snapshot.attempts[0].status, "STARTED")
        self.assertIsNotNone(snapshot.stop_request)

    def test_non_cooperative_action_waits_for_the_atomic_return(self) -> None:
        preview = l2_preview()
        store, action = ledger_store(preview)
        executor = store.acquire_executor("executor-1")
        executor.operation_started(action_id=action.action_id, attempt_id="attempt-1", started_at=CHECKED_AT)

        report = router().stop_execution(store=store)

        self.assertEqual(report.status, StopStatus.EXECUTION_ATOMIC_WAIT)
        self.assertTrue(report.atomic_wait_required)
        self.assertEqual(store.snapshot().attempts[0].status, "STARTED")

    def test_stopped_execution_refuses_new_handles_and_second_stops(self) -> None:
        preview = l2_preview()
        store, _action = ledger_store(preview)
        first = router().stop_execution(store=store)
        self.assertEqual(first.status, StopStatus.EXECUTION_BLOCKED_BEFORE_START)
        second = router().stop_execution(store=store)
        self.assertEqual(second.status, StopStatus.ALREADY_STOPPED)
        self.assertTrue(second.late_event_isolated)
        with self.assertRaises(Exception):
            store.acquire_executor("executor-2")

    def test_stop_requires_a_real_ledger_store(self) -> None:
        with self.assertRaises(TypeError):
            router().stop_execution(store=object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            router().stop_execution(store=ledger_store(l2_preview())[0], reason="just_stop")


class LateEventIsolationTests(unittest.TestCase):
    def test_late_events_never_revive_playback_execution_or_authorization(self) -> None:
        preview = l2_preview()
        store, _action = ledger_store(preview)
        stopped = router().stop_execution(store=store)
        before = store.snapshot()

        report = router().note_late_event(source="tts_chunk")

        self.assertEqual(report.status, StopStatus.ALREADY_STOPPED)
        self.assertTrue(report.late_event_isolated)
        self.assertFalse(report.speech_stopped)
        self.assertFalse(report.stop_requested)
        after = store.snapshot()
        self.assertEqual(before.stop_request, after.stop_request)
        self.assertEqual(before.attempts, after.attempts)
        self.assertEqual(before.ledger.status, after.ledger.status)

    def test_late_event_requires_a_redacted_source_identifier(self) -> None:
        with self.assertRaises(ValueError):
            router().note_late_event(source="pcm payload G:\\secret.wav")


class StopReportInvariantTests(unittest.TestCase):
    def test_resume_is_never_allowed(self) -> None:
        with self.assertRaises(ValueError):
            StopReport(
                context=StopContext.CHAT,
                status=StopStatus.SPEECH_STOPPED,
                reason="user_stopped",
                resume_allowed=True,
            )

    def test_report_details_are_redacted_and_reason_is_validated(self) -> None:
        report = router().stop_chat(stream=FakeStream())
        details = report.to_details()
        self.assertEqual(set(details), {"context", "status", "reason", "atomic_wait_required", "late_event_isolated"})
        self.assertNotIn("path", details)
        with self.assertRaises(ValueError):
            StopReport(context=StopContext.CHAT, status=StopStatus.SPEECH_STOPPED, reason="stop")

    def test_router_surface_has_no_force_kill_or_retry_entry(self) -> None:
        item = router()
        public = {name for name in dir(item) if not name.startswith("_")}
        self.assertEqual(
            public,
            {
                "last_report",
                "resolve_context",
                "stop_chat",
                "stop_authorization",
                "stop_execution",
                "note_late_event",
            },
        )
        for name in public:
            lowered = name.lower()
            for forbidden in ("kill", "retry", "terminate", "resume", "rollback"):
                self.assertNotIn(forbidden, lowered)


class PlanFreshnessTests(unittest.TestCase):
    def test_stop_uses_the_current_plan_facts_only(self) -> None:
        preview = l2_preview()
        controller = l2_window(preview)
        other = previewer().preview(plan_request(plan_id="plan-other"))
        report = router().stop_authorization(
            AuthorizationStopInput(
                controller=controller,
                confirmation_window_id="window-stop",
                session_id="session-1",
                current_plan=other.plan,
                current_target_state_hash=HASH_A,
            )
        )
        # 漂移的当前计划只会让确认窗口作废，不会产生任何授权。
        self.assertEqual(report.status, StopStatus.CONFIRMATION_CANCELLED)
        self.assertTrue(report.confirmation_invalidated)

    def test_application_plan_fixture_is_consistent(self) -> None:
        preview = previewer().preview(plan_request(application_proposal()))
        self.assertEqual(preview.plan.actions[0].normalized_arguments["application_id"], APPLICATION_ID)
        self.assertEqual(preview.plan.actions[0].preview["application_name"], APPLICATION_NAME)


if __name__ == "__main__":
    unittest.main()
