from __future__ import annotations

import unittest
from dataclasses import is_dataclass

from app.contracts import (
    ActionAttempt,
    ActionPlan,
    ActionResult,
    AgentRequest,
    AgentResponse,
    AudioFrame,
    AudioSession,
    AuditEvent,
    AuthorizationSnapshot,
    ContractError,
    ExecutionLedger,
    PlaybackFact,
    PlanVersion,
    ProviderState,
    ResultVerification,
    SessionContext,
    TaskContext,
    TTSReply,
    TTSSegment,
    Utterance,
)


class ContractModelTests(unittest.TestCase):
    def test_all_core_objects_round_trip_with_strict_schema(self) -> None:
        objects = [
            SessionContext(session_id="session-1", instance_id="instance-1", created_at="2026-09-19T10:00:00Z"),
            Utterance(utterance_id="utterance-1", session_id="session-1", input_mode="text", text="你好", accepted_at="2026-09-19T10:00:01Z"),
            AgentRequest(request_id="request-1", session_id="session-1", utterance_id="utterance-1", submitted_at="2026-09-19T10:00:02Z"),
            AgentResponse(request_id="request-1", response_id="response-1", status="completed", completed_at="2026-09-19T10:00:03Z", text="你好"),
            TaskContext(task_id="task-1", session_id="session-1", request_id="request-1", state="PLANNING", created_at="2026-09-19T10:00:04Z"),
            ActionPlan(plan_id="plan-1", task_id="task-1", current_version=1, current_plan_hash="sha256:plan", created_at="2026-09-19T10:00:05Z"),
            PlanVersion(plan_id="plan-1", version=1, plan_hash="sha256:plan", action_ids=("action-1",), target_state_hash="sha256:target", created_at="2026-09-19T10:00:06Z"),
            AuthorizationSnapshot(authorization_id="auth-1", issuer_instance_id="instance-1", session_id="session-1", operation_id="operation-1", plan_id="plan-1", plan_version=1, plan_hash="sha256:plan", target_state_hash="sha256:target", allowed_action_ids=("action-1",), risk_level="L2", confirmation_window_id="window-1", authorization_method="modal", confirmation_stage="modal", confirmation_phrase=None, created_at="2026-09-19T10:00:07Z", expires_at="2026-09-19T10:05:07Z"),
            ExecutionLedger(execution_id="execution-1", plan_id="plan-1", plan_version=1, plan_hash="sha256:plan", authorization_snapshot_hash="sha256:auth", status="CREATED", created_at="2026-09-19T10:00:08Z"),
            ActionAttempt(execution_id="execution-1", action_id="action-1", attempt_id="attempt-1", executor_instance_id="instance-1", fencing_token="fence-1", status="NOT_STARTED", started_at=None, finished_at=None, cancellation_requested_at=None),
            ActionResult(execution_id="execution-1", action_id="action-1", attempt_id="attempt-1", status="state_uncertain", error_code="verification_evidence_missing", completed_at="2026-09-19T10:00:09Z"),
            ResultVerification(execution_id="execution-1", action_id="action-1", attempt_id="attempt-1", status="state_uncertain", verified_at="2026-09-19T10:00:10Z", verifier="local"),
            AudioFrame(audio_session_id="audio-1", provider_generation=1, stream_id="stream-1", event_sequence=1, frame_index=0, captured_at="2026-09-19T10:00:11Z", duration_ms=20),
            AudioSession(audio_session_id="audio-1", session_id="session-1", provider_generation=1, stream_id="stream-1", state="ACTIVE", created_at="2026-09-19T10:00:11Z"),
            ProviderState(provider_name="local-asr", generation=1, state="READY", changed_at="2026-09-19T10:00:12Z"),
            TTSReply(reply_id="reply-1", request_id="request-1", state="QUEUED", created_at="2026-09-19T10:00:13Z"),
            TTSSegment(reply_id="reply-1", segment_id="segment-1", provider_request_id="provider-request-1", provider_generation=1, state="COMMITTED_NOT_STARTED", text_hash="sha256:text", created_at="2026-09-19T10:00:14Z"),
            PlaybackFact(reply_id="reply-1", segment_id="segment-1", state="PLAYBACK_STARTED", event_sequence=1, observed_at="2026-09-19T10:00:15Z", playback_offset_ms=0),
            AuditEvent(audit_event_id="audit-1", event_type="authorization_created", source_instance_id="instance-1", event_sequence=1, created_at="2026-09-19T10:00:16Z", subject_id="auth-1"),
        ]

        for original in objects:
            with self.subTest(contract=type(original).__name__):
                restored = type(original).from_json(original.to_json())
                self.assertEqual(restored, original)

    def test_core_object_inventory_is_exact_frozen_and_slotted(self) -> None:
        contract_types = (
            SessionContext, Utterance, AgentRequest, AgentResponse, TaskContext,
            ActionPlan, PlanVersion, AuthorizationSnapshot, ExecutionLedger,
            ActionAttempt, ActionResult, ResultVerification, AudioFrame,
            AudioSession, ProviderState, TTSReply, TTSSegment, PlaybackFact,
            AuditEvent,
        )
        self.assertEqual(
            tuple(contract.__name__ for contract in contract_types),
            (
                "SessionContext", "Utterance", "AgentRequest", "AgentResponse",
                "TaskContext", "ActionPlan", "PlanVersion",
                "AuthorizationSnapshot", "ExecutionLedger", "ActionAttempt",
                "ActionResult", "ResultVerification", "AudioFrame",
                "AudioSession", "ProviderState", "TTSReply", "TTSSegment",
                "PlaybackFact", "AuditEvent",
            ),
        )
        self.assertEqual(len(contract_types), 19)
        for contract in contract_types:
            with self.subTest(contract=contract.__name__):
                self.assertTrue(is_dataclass(contract))
                self.assertTrue(contract.__dataclass_params__.frozen)
                self.assertIn("__slots__", contract.__dict__)

    def test_playback_facts_only_express_observable_states(self) -> None:
        allowed = (
            "COMMITTED_NOT_STARTED",
            "PLAYBACK_STARTED",
            "PARTIALLY_PLAYED",
            "PLAYED",
            "ABORTED",
        )
        for state in allowed:
            with self.subTest(state=state):
                fact = PlaybackFact(
                    reply_id="reply-1",
                    segment_id="segment-1",
                    state=state,
                    event_sequence=1,
                    observed_at="2026-09-19T10:00:15Z",
                    playback_offset_ms=0,
                )
                self.assertEqual(fact.state, state)
        with self.assertRaisesRegex(ContractError, "PlaybackFact.state"):
            PlaybackFact(
                reply_id="reply-1",
                segment_id="segment-1",
                state="USER_DEFINITELY_HEARD",
                event_sequence=1,
                observed_at="2026-09-19T10:00:15Z",
                playback_offset_ms=0,
            )

    def test_unknown_and_missing_fields_are_rejected(self) -> None:
        valid = SessionContext(session_id="session-1", instance_id="instance-1", created_at="2026-09-19T10:00:00Z").to_dict()
        with self.assertRaisesRegex(ContractError, "未知字段"):
            SessionContext.from_dict({**valid, "unexpected": True})
        del valid["instance_id"]
        with self.assertRaisesRegex(ContractError, "缺少字段"):
            SessionContext.from_dict(valid)

    def test_schema_version_is_required_and_future_version_is_rejected(self) -> None:
        payload = SessionContext(session_id="session-1", instance_id="instance-1", created_at="2026-09-19T10:00:00Z").to_dict()
        del payload["schema_version"]
        with self.assertRaisesRegex(ContractError, "schema_version"):
            SessionContext.from_dict(payload)
        payload["schema_version"] = 2
        with self.assertRaisesRegex(ContractError, "不支持"):
            SessionContext.from_dict(payload)

    def test_identifier_timestamp_and_enum_values_are_validated(self) -> None:
        with self.assertRaisesRegex(ContractError, "session_id"):
            SessionContext(session_id="", instance_id="instance-1", created_at="2026-09-19T10:00:00Z")
        with self.assertRaisesRegex(ContractError, "UTC"):
            SessionContext(session_id="session-1", instance_id="instance-1", created_at="2026-09-19 10:00:00")
        with self.assertRaisesRegex(ContractError, "ProviderState.state"):
            ProviderState(provider_name="local-asr", generation=1, state="MAGIC", changed_at="2026-09-19T10:00:00Z")

    def test_action_identity_and_audio_identity_are_mandatory(self) -> None:
        with self.assertRaisesRegex(ContractError, "attempt_id"):
            ActionAttempt(execution_id="execution-1", action_id="action-1", attempt_id="", executor_instance_id="instance-1", fencing_token="fence-1", status="NOT_STARTED", started_at=None, finished_at=None, cancellation_requested_at=None)
        with self.assertRaisesRegex(ContractError, "provider_generation"):
            AudioFrame(audio_session_id="audio-1", provider_generation=0, stream_id="stream-1", event_sequence=1, frame_index=0, captured_at="2026-09-19T10:00:00Z", duration_ms=20)


if __name__ == "__main__":
    unittest.main()