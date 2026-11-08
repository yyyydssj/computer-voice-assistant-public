from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import unittest

from app.tts import (
    AudioFormat,
    CapabilityCheck,
    CapabilityDecision,
    ProviderCapability,
    ProviderCapabilityRegistry,
    ProviderRequestAudioChunk,
    ProviderRequestLifecycle,
    ProviderRequestLifecycleError,
    ProviderRequestLifecycleEvent,
    ProviderRequestLifecycleEventKind,
    ProviderRequestLifecycleReasonCode,
    ProviderRequestLifecycleRegistry,
    ProviderRequestLifecycleState,
    ProviderRequestRegistry,
    VoiceProfile,
    VoiceProfileActor,
    VoiceProfileRegistry,
    VoiceProfileType,
)


class ProviderRequestLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio_format = AudioFormat("pcm_s16le", 24000, 1)
        capabilities = ProviderCapabilityRegistry(
            (
                ProviderCapability(
                    provider_name="voicestudio",
                    supports_text_stream=True,
                    supports_audio_stream=True,
                    supports_cancellation=True,
                    supports_voice_switching=True,
                    audio_formats=(self.audio_format,),
                    estimated_first_audio_latency_ms=350,
                ),
                ProviderCapability(
                    provider_name="edge_tts",
                    supports_text_stream=True,
                    supports_audio_stream=True,
                    supports_cancellation=True,
                    supports_voice_switching=True,
                    audio_formats=(self.audio_format,),
                    estimated_first_audio_latency_ms=420,
                ),
            )
        )
        profiles = VoiceProfileRegistry()
        profile = VoiceProfile(
            profile_id="assistant-default",
            name="默认助手",
            profile_type=VoiceProfileType.PRESET,
            provider="voicestudio",
            provider_profile_id="provider-profile-ref",
            language="zh-CN",
            enabled=False,
        )
        profiles.create(profile, actor=VoiceProfileActor.USER)
        profiles.enable(profile.profile_id, actor=VoiceProfileActor.USER)
        other_profile = VoiceProfile(
            profile_id="assistant-other",
            name="备用助手",
            profile_type=VoiceProfileType.PRESET,
            provider="edge_tts",
            provider_profile_id="edge-zh-CN-XiaoxiaoNeural",
            language="zh-CN",
            enabled=False,
        )
        profiles.create(other_profile, actor=VoiceProfileActor.USER)
        profiles.enable(other_profile.profile_id, actor=VoiceProfileActor.USER)
        profiles.select(profile.profile_id, actor=VoiceProfileActor.USER)
        session = profiles.bind_session("session-1")
        requests = ProviderRequestRegistry(capabilities, profiles)
        self.request = requests.prepare(
            provider_request_id="provider-request-1",
            reply_id="reply-1",
            segment_id="segment-1",
            request_sequence=1,
            text="你好，世界。",
            audio_format=self.audio_format,
            provider_generation=3,
            voice_session=session,
        )
        self.profiles = profiles
        self.requests = requests
        self.registry = ProviderRequestLifecycleRegistry(requests)

    def test_prepared_is_local_only_and_progresses_to_distinct_audio_facts(self) -> None:
        prepared = self.registry.get(self.request.provider_request_id)
        self.assertEqual(prepared.state, ProviderRequestLifecycleState.PREPARED)
        self.assertFalse(prepared.audio_produced)
        self.assertFalse(prepared.audio_enqueued)

        accepted = self.registry.accept(self.request.provider_request_id, event_id="event-1")
        synthesizing = self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        produced = self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        enqueued = self.registry.audio_enqueued(self.request.provider_request_id, event_id="event-4")
        completed = self.registry.complete(self.request.provider_request_id, event_id="event-5")

        self.assertEqual(accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertEqual(synthesizing.state, ProviderRequestLifecycleState.SYNTHESIZING)
        self.assertEqual(produced.state, ProviderRequestLifecycleState.SYNTHESIZING)
        self.assertTrue(enqueued.audio_produced)
        self.assertTrue(enqueued.audio_enqueued)
        self.assertEqual(completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertEqual(len(completed.events), 5)
        self.assertEqual(completed.events[2].kind, ProviderRequestLifecycleEventKind.AUDIO_PRODUCED)
        self.assertEqual(len(completed.audio_chunks), 1)

    def test_invalid_transitions_and_repeated_terminal_are_rejected_atomically(self) -> None:
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_produced"):
            self.registry.audio_produced(self.request.provider_request_id, event_id="event-1")
        self.assertEqual(self.registry.get(self.request.provider_request_id).events, ())

        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "completed"):
            self.registry.complete(self.request.provider_request_id, event_id="event-3")
        self.assertEqual(len(self.registry.events_for(self.request.provider_request_id)), 2)

        self.registry.fail(self.request.provider_request_id, event_id="event-3", reason="provider_timeout")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.cancel(self.request.provider_request_id, event_id="event-4", reason="late_cancel")
        self.assertEqual(len(self.registry.events_for(self.request.provider_request_id)), 3)

    def test_failure_and_cancellation_can_stop_before_provider_acceptance(self) -> None:
        failed = self.registry.fail(self.request.provider_request_id, event_id="event-f", reason="local_gate_rejected")
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)

        requests = ProviderRequestRegistry(self.requests._capabilities, self.requests._voice_profiles)  # type: ignore[attr-defined]
        request = requests.prepare(
            provider_request_id="provider-request-2",
            reply_id="reply-1",
            segment_id="segment-2",
            request_sequence=1,
            text="第二句。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )
        registry = ProviderRequestLifecycleRegistry(requests)
        cancelled = registry.cancel(request.provider_request_id, event_id="event-c", reason="user_cancelled")
        self.assertEqual(cancelled.state, ProviderRequestLifecycleState.CANCELLED)

    def test_rejected_event_identity_does_not_create_snapshot_or_consume_event_id(self) -> None:
        invalid = ProviderRequestLifecycleEvent(
            event_id="event-reusable",
            event_sequence=1,
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(invalid)
        self.assertNotIn(self.request.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertNotIn("event-reusable", self.registry._event_ids)  # type: ignore[attr-defined]

        accepted = self.registry.accept(self.request.provider_request_id, event_id="event-reusable")
        self.assertEqual(accepted.events[0].event_id, "event-reusable")

    def test_rejected_audio_chunk_identity_does_not_create_snapshot_or_consume_event_id(self) -> None:
        invalid = replace(self._chunk(event_id="chunk-reusable"), reply_id="reply-other")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record_audio_chunk(invalid)
        self.assertNotIn(self.request.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertNotIn("chunk-reusable", self.registry._event_ids)  # type: ignore[attr-defined]

        accepted = self.registry.accept(self.request.provider_request_id, event_id="chunk-reusable")
        self.assertEqual(accepted.events[0].event_id, "chunk-reusable")

    def test_rejected_reason_code_does_not_create_snapshot_or_consume_event_id(self) -> None:
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "失败原因码"):
            self.registry.fail(
                self.request.provider_request_id,
                event_id="event-reason-reusable",
                reason="provider failed",
                reason_code=ProviderRequestLifecycleReasonCode.USER_CANCELLED,
            )
        self.assertNotIn(self.request.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertNotIn("event-reason-reusable", self.registry._event_ids)  # type: ignore[attr-defined]

        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-reason-reusable",
            reason="provider failed",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        self.assertEqual(failed.terminal_reason_code, ProviderRequestLifecycleReasonCode.PROVIDER_ERROR)

    def test_cancelled_terminal_rejects_late_audio_chunk_without_mutation(self) -> None:
        cancelled = self.registry.cancel(
            self.request.provider_request_id,
            event_id="event-cancelled",
            reason="user cancelled",
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.record_audio_chunk(self._chunk(event_id="chunk-late-cancelled"))
        snapshot = self.registry.get(self.request.provider_request_id)
        self.assertEqual(snapshot, cancelled)
        self.assertEqual(snapshot.audio_chunks, ())
        self.assertNotIn("chunk-late-cancelled", self.registry._event_ids)  # type: ignore[attr-defined]

    def test_terminal_duplicate_event_id_rejection_preserves_first_terminal_snapshot(self) -> None:
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-terminal",
            reason="provider failed",
        )
        late = replace(failed.events[0], event_sequence=2)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.record(late)
        self.assertEqual(self.registry.get(self.request.provider_request_id), failed)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), failed.events)

    def test_terminal_late_identity_and_duplicate_rejections_keep_snapshot_atomic(self) -> None:
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-failed",
            reason="provider failed",
        )
        invalid_late = ProviderRequestLifecycleEvent(
            event_id="event-late-identity",
            event_sequence=2,
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(invalid_late)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.record(replace(failed.events[0], event_id="event-late-duplicate", event_sequence=2))
        snapshot = self.registry.get(self.request.provider_request_id)
        self.assertEqual(snapshot, failed)
        self.assertNotIn("event-late-identity", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertNotIn("event-late-duplicate", self.registry._event_ids)  # type: ignore[attr-defined]

    def test_unknown_request_and_cross_identity_events_are_rejected(self) -> None:
        unknown = ProviderRequestLifecycleEvent(
            event_id="event-unknown",
            event_sequence=1,
            provider_request_id="missing-request",
            reply_id=self.request.reply_id,
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
            self.registry.record(unknown)

        cross_reply = replace(
            unknown,
            event_id="event-cross",
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(cross_reply)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), ())

    def test_stale_generation_and_mismatched_capability_are_rejected(self) -> None:
        base = ProviderRequestLifecycleEvent(
            event_id="event-stale",
            event_sequence=1,
            provider_request_id=self.request.provider_request_id,
            reply_id=self.request.reply_id,
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation + 1,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "provider_generation"):
            self.registry.record(base)

        mismatched = replace(
            base,
            event_id="event-capability",
            provider_generation=self.request.provider_generation,
            capability_check=CapabilityCheck(
                provider_name="voicestudio",
                decision=CapabilityDecision.COMPATIBLE,
            ),
            audio_format=AudioFormat("opus", 48000, 2),
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_format"):
            self.registry.record(mismatched)

    def test_reason_codes_and_terminal_projection_are_local_facts(self) -> None:
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-f",
            reason="provider timed out",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT,
        )
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(failed.terminal_reason_code, ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT)
        self.assertEqual(failed.terminal_event, failed.events[-1])
        self.assertFalse(hasattr(failed, "provider_stopped"))

        requests = ProviderRequestRegistry(self.requests._capabilities, self.requests._voice_profiles)  # type: ignore[attr-defined]
        request = requests.prepare(
            provider_request_id="provider-request-2",
            reply_id="reply-1",
            segment_id="segment-2",
            request_sequence=1,
            text="第二句。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )
        registry = ProviderRequestLifecycleRegistry(requests)
        cancelled = registry.cancel(
            request.provider_request_id,
            event_id="event-c",
            reason="superseded by newer generation",
            reason_code=ProviderRequestLifecycleReasonCode.SUPERSEDED,
        )
        self.assertEqual(cancelled.terminal_reason_code, ProviderRequestLifecycleReasonCode.SUPERSEDED)

    def test_terminal_reason_projection_is_read_only_and_identity_stable(self) -> None:
        prepared = self.registry.get(self.request.provider_request_id)
        self.assertFalse(prepared.terminal)
        self.assertIsNone(prepared.terminal_reason)
        self.assertIs(self.registry.get(self.request.provider_request_id), prepared)

        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="batch70-failed",
            reason="provider timed out locally",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT,
        )
        self.assertEqual(failed.terminal_reason, "provider timed out locally")
        self.assertIs(failed.terminal_reason, failed.terminal_event.reason)
        self.assertIs(self.registry.get(self.request.provider_request_id), failed)

        cancelled_request = self._prepare_second_request()
        cancelled = self.registry.cancel(
            cancelled_request.provider_request_id,
            event_id="batch70-cancelled",
            reason="superseded by a newer request",
            reason_code=ProviderRequestLifecycleReasonCode.SUPERSEDED,
        )
        self.assertEqual(cancelled.terminal_reason, "superseded by a newer request")
        self.assertIs(cancelled.terminal_reason, cancelled.terminal_event.reason)
        self.assertIs(self.registry.get(cancelled_request.provider_request_id), cancelled)

        completed_request = self._prepare_third_request()
        self.registry.accept(completed_request.provider_request_id, event_id="batch70-completed-accepted")
        self.registry.synthesis_started(completed_request.provider_request_id, event_id="batch70-completed-started")
        self.registry.record_audio_chunk(
            self._chunk_for_request(completed_request, event_id="batch70-completed-chunk")
        )
        self.registry.audio_produced(completed_request.provider_request_id, event_id="batch70-completed-produced")
        completed = self.registry.complete(
            completed_request.provider_request_id,
            event_id="batch70-completed",
        )
        self.assertIsNone(completed.terminal_reason)
        self.assertIs(completed.terminal_event, completed.events[-1])
        self.assertIs(self.registry.get(completed_request.provider_request_id), completed)
    def test_reason_code_kind_mismatch_is_rejected_atomically(self) -> None:
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "失败原因码"):
            self.registry.fail(
                self.request.provider_request_id,
                event_id="event-f",
                reason="cancelled locally",
                reason_code=ProviderRequestLifecycleReasonCode.USER_CANCELLED,
            )
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), ())
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "取消原因码"):
            self.registry.cancel(
                self.request.provider_request_id,
                event_id="event-c",
                reason="provider failed",
                reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
            )
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), ())

    def test_generation_fence_rejects_late_future_and_rollback_generations(self) -> None:
        self.assertIsNone(self.registry.active_generation("voicestudio"))
        self.assertEqual(self.registry.activate_generation("voicestudio", 3), 3)
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)

        stale_requests = ProviderRequestRegistry(self.requests._capabilities, self.requests._voice_profiles)  # type: ignore[attr-defined]
        stale_request = stale_requests.prepare(
            provider_request_id="provider-request-stale",
            reply_id="reply-1",
            segment_id="segment-stale",
            request_sequence=1,
            text="旧代请求。",
            audio_format=self.audio_format,
            provider_generation=2,
            session_id="session-1",
        )
        stale_registry = ProviderRequestLifecycleRegistry(stale_requests)
        stale_registry.activate_generation("voicestudio", 3)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            stale_registry.accept(stale_request.provider_request_id, event_id="event-stale")
        self.assertEqual(stale_registry.events_for(stale_request.provider_request_id), ())

        future_requests = ProviderRequestRegistry(self.requests._capabilities, self.requests._voice_profiles)  # type: ignore[attr-defined]
        future_request = future_requests.prepare(
            provider_request_id="provider-request-future",
            reply_id="reply-1",
            segment_id="segment-future",
            request_sequence=1,
            text="未来代请求。",
            audio_format=self.audio_format,
            provider_generation=4,
            session_id="session-1",
        )
        future_registry = ProviderRequestLifecycleRegistry(future_requests)
        future_registry.activate_generation("voicestudio", 3)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "尚未激活"):
            future_registry.accept(future_request.provider_request_id, event_id="event-future")
        self.assertEqual(future_registry.events_for(future_request.provider_request_id), ())

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不得回退"):
            self.registry.activate_generation("voicestudio", 2)
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)
        self.assertEqual(self.registry.advance_generation("voicestudio", 4), 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.accept(self.request.provider_request_id, event_id="event-old")
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), ())

    def test_terminal_state_rejects_late_events_and_keeps_first_terminal_fact(self) -> None:
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-f",
            reason="local validation failed",
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "已锁定"):
            self.registry.cancel(
                self.request.provider_request_id,
                event_id="event-late-c",
                reason="user cancelled",
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "已锁定"):
            self.registry.accept(self.request.provider_request_id, event_id="event-late-a")
        snapshot = self.registry.get(self.request.provider_request_id)
        self.assertEqual(snapshot, failed)
        self.assertEqual(len(snapshot.events), 1)
        self.assertEqual(snapshot.terminal_reason_code, ProviderRequestLifecycleReasonCode.LOCAL_GATE_REJECTED)

    def test_global_event_ids_are_shared_across_requests_and_event_kinds(self) -> None:
        second = self._prepare_second_request()
        self.registry.accept(self.request.provider_request_id, event_id="event-shared")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.accept(second.provider_request_id, event_id="event-shared")
        self.assertNotIn(second.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]

        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-synthesis")
        self.registry.record_audio_chunk(self._chunk(event_id="event-cross-kind"))
        self.registry.accept(second.provider_request_id, event_id="event-second-accept")
        self.registry.synthesis_started(second.provider_request_id, event_id="event-second-start")
        duplicate_chunk = replace(
            self._chunk(event_id="event-cross-kind"),
            provider_request_id=second.provider_request_id,
            reply_id=second.reply_id,
            segment_id=second.segment_id,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.record_audio_chunk(duplicate_chunk)

        first = self.registry.get(self.request.provider_request_id)
        second_snapshot = self.registry.get(second.provider_request_id)
        self.assertEqual(tuple(event.event_id for event in first.events), ("event-shared", "event-synthesis"))
        self.assertEqual(tuple(chunk.event_id for chunk in first.audio_chunks), ("event-cross-kind",))
        self.assertEqual(
            tuple(event.event_id for event in second_snapshot.events),
            ("event-second-accept", "event-second-start"),
        )
        self.assertEqual(second_snapshot.audio_chunks, ())

    def test_identity_rejection_precedes_generation_fence_for_event_and_audio_chunk(self) -> None:
        second = self._prepare_second_request()
        invalid_event = ProviderRequestLifecycleEvent(
            event_id="event-identity-before-generation",
            event_sequence=1,
            provider_request_id=second.provider_request_id,
            reply_id="reply-other",
            segment_id=second.segment_id,
            provider_name=second.provider_name,
            provider_generation=second.provider_generation - 1,
            voice_profile_id=second.voice_profile_id,
            voice_profile_version=second.voice_profile_version,
            audio_format=second.audio_format,
            capability_check=second.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(invalid_event)
        self.assertNotIn(second.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertNotIn("event-identity-before-generation", self.registry._event_ids)  # type: ignore[attr-defined]
        accepted = self.registry.accept(second.provider_request_id, event_id="event-identity-before-generation")
        self.assertEqual(accepted.events[0].event_id, "event-identity-before-generation")

        third = self._prepare_third_request()
        invalid_chunk = replace(
            self._chunk(event_id="chunk-identity-before-generation", provider_generation=third.provider_generation - 1),
            provider_request_id=third.provider_request_id,
            reply_id="reply-other",
            segment_id=third.segment_id,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record_audio_chunk(invalid_chunk)
        self.assertNotIn(third.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertNotIn("chunk-identity-before-generation", self.registry._event_ids)  # type: ignore[attr-defined]
        accepted_third = self.registry.accept(third.provider_request_id, event_id="chunk-identity-before-generation")
        self.assertEqual(accepted_third.events[0].event_id, "chunk-identity-before-generation")

    def test_public_reads_may_lazily_create_prepared_snapshot_but_rejected_writes_do_not_mutate_it(self) -> None:
        second = self._prepare_second_request()
        self.assertNotIn(second.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.events_for(second.provider_request_id), ())
        self.assertEqual(self.registry.audio_chunks_for(second.provider_request_id), ())
        prepared = self.registry.get(second.provider_request_id)
        self.assertEqual(prepared.state, ProviderRequestLifecycleState.PREPARED)
        self.assertIn(second.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]

        invalid = ProviderRequestLifecycleEvent(
            event_id="event-after-public-read",
            event_sequence=1,
            provider_request_id=second.provider_request_id,
            reply_id="reply-other",
            segment_id=second.segment_id,
            provider_name=second.provider_name,
            provider_generation=second.provider_generation,
            voice_profile_id=second.voice_profile_id,
            voice_profile_version=second.voice_profile_version,
            audio_format=second.audio_format,
            capability_check=second.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(invalid)
        self.assertEqual(self.registry.get(second.provider_request_id), prepared)
        self.assertNotIn("event-after-public-read", self.registry._event_ids)  # type: ignore[attr-defined]

    def test_public_read_views_are_consistent_and_unknown_reads_do_not_mutate(self) -> None:
        second = self._prepare_second_request()
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
            self.registry.get("missing-request")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
            self.registry.events_for("missing-request")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
            self.registry.audio_chunks_for("missing-request")
        self.assertNotIn("missing-request", self.registry._snapshots)  # type: ignore[attr-defined]

        self.assertEqual(self.registry.events_for(second.provider_request_id), ())
        self.assertEqual(self.registry.audio_chunks_for(second.provider_request_id), ())
        prepared = self.registry.get(second.provider_request_id)
        self.assertIs(self.registry.get(second.provider_request_id), prepared)
        self.assertIs(self.registry.events_for(second.provider_request_id), prepared.events)
        self.assertIs(self.registry.audio_chunks_for(second.provider_request_id), prepared.audio_chunks)

        accepted = self.registry.accept(second.provider_request_id, event_id="event-read-accept")
        accepted_events = self.registry.events_for(second.provider_request_id)
        accepted_chunks = self.registry.audio_chunks_for(second.provider_request_id)
        self.assertIs(self.registry.get(second.provider_request_id), accepted)
        self.assertIs(accepted_events, accepted.events)
        self.assertIs(accepted_chunks, accepted.audio_chunks)
        self.assertEqual(tuple(event.event_id for event in accepted_events), ("event-read-accept",))

        started = self.registry.synthesis_started(second.provider_request_id, event_id="event-read-start")
        self.assertEqual(tuple(event.event_id for event in accepted_events), ("event-read-accept",))
        self.assertIs(self.registry.events_for(second.provider_request_id), started.events)
        self.assertIs(self.registry.audio_chunks_for(second.provider_request_id), started.audio_chunks)
        self.assertEqual(tuple(event.event_id for event in started.events), ("event-read-accept", "event-read-start"))

    def test_unknown_public_reads_preserve_all_observations_without_lazy_snapshot(self) -> None:
        before_snapshots = dict(self.registry._snapshots)  # type: ignore[attr-defined]
        before_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_generations = dict(self.registry._active_generations)  # type: ignore[attr-defined]
        for reader in (
            self.registry.get,
            self.registry.events_for,
            self.registry.audio_chunks_for,
        ):
            with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
                reader("missing-request")
        self.assertEqual(self.registry._snapshots, before_snapshots)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._event_ids, before_event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._active_generations, before_generations)  # type: ignore[attr-defined]

    def test_generation_observation_rejects_malformed_provider_names_without_side_effects(self) -> None:
        before_snapshots = dict(self.registry._snapshots)  # type: ignore[attr-defined]
        before_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_generations = dict(self.registry._active_generations)  # type: ignore[attr-defined]
        for invalid_provider_name in ("", "contains space", None, True):
            with self.assertRaises(ProviderRequestLifecycleError):
                self.registry.active_generation(invalid_provider_name)  # type: ignore[arg-type]
        self.assertEqual(self.registry._snapshots, before_snapshots)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._event_ids, before_event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._active_generations, before_generations)  # type: ignore[attr-defined]

    def test_public_reads_reject_malformed_request_ids_without_snapshot_creation(self) -> None:
        before_snapshots = dict(self.registry._snapshots)  # type: ignore[attr-defined]
        before_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        for invalid_request_id in ("", "contains space", None, True):
            for reader in (
                self.registry.get,
                self.registry.events_for,
                self.registry.audio_chunks_for,
            ):
                with self.assertRaises(ProviderRequestLifecycleError):
                    reader(invalid_request_id)  # type: ignore[arg-type]
        self.assertEqual(self.registry._snapshots, before_snapshots)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._event_ids, before_event_ids)  # type: ignore[attr-defined]

    def test_successful_writes_preserve_old_snapshot_and_keep_public_projections_aligned(self) -> None:
        prepared = self.registry.get(self.request.provider_request_id)
        accepted = self.registry.accept(self.request.provider_request_id, event_id="event-history-accept")
        self.assertEqual(prepared.state, ProviderRequestLifecycleState.PREPARED)
        self.assertEqual(prepared.events, ())
        self.assertEqual(prepared.audio_chunks, ())
        self.assertEqual(accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertIsNot(prepared, accepted)
        self.assertEqual(self.registry.get(self.request.provider_request_id), accepted)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), accepted.events)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), accepted.audio_chunks)

        started = self.registry.synthesis_started(self.request.provider_request_id, event_id="event-history-start")
        with_chunk = self.registry.record_audio_chunk(self._chunk(event_id="chunk-history"))
        self.assertEqual(tuple(event.event_id for event in started.events), ("event-history-accept", "event-history-start"))
        self.assertEqual(tuple(chunk.event_id for chunk in with_chunk.audio_chunks), ("chunk-history",))
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), with_chunk.events)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), with_chunk.audio_chunks)
        self.assertEqual(self.registry.get(self.request.provider_request_id).state, ProviderRequestLifecycleState.SYNTHESIZING)

    def test_generation_fences_are_isolated_per_provider(self) -> None:
        self.assertEqual(self.registry.activate_generation("voicestudio", 3), 3)
        self.assertEqual(self.registry.activate_generation("edge_tts", 7), 7)
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)
        self.assertEqual(self.registry.advance_generation("voicestudio", 4), 4)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不得回退"):
            self.registry.advance_generation("edge_tts", 6)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

    def test_generation_activation_validation_and_repeated_activation_are_atomic(self) -> None:
        self.assertIsNone(self.registry.active_generation("voicestudio"))
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "provider_name"):
            self.registry.activate_generation("", 3)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "provider_generation"):
            self.registry.activate_generation("voicestudio", 0)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "provider_generation"):
            self.registry.activate_generation("voicestudio", True)  # type: ignore[arg-type]
        self.assertIsNone(self.registry.active_generation("voicestudio"))

        self.assertEqual(self.registry.activate_generation("voicestudio", 3), 3)
        self.assertEqual(self.registry.activate_generation("voicestudio", 3), 3)
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)
        self.assertEqual(self.registry.advance_generation("voicestudio", 4), 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不得回退"):
            self.registry.activate_generation("voicestudio", 2)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不得回退"):
            self.registry.advance_generation("voicestudio", 1)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "provider_name"):
            self.registry.active_generation("")
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)

    def test_completed_terminal_projection_exposes_consistent_state_and_audio_facts(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        completed = self.registry.complete(self.request.provider_request_id, event_id="event-4")

        self.assertTrue(completed.terminal)
        self.assertEqual(completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertIs(completed.terminal_event, completed.events[-1])
        self.assertEqual(completed.terminal_event.kind, ProviderRequestLifecycleEventKind.COMPLETED)
        self.assertIsNone(completed.terminal_reason_code)
        self.assertTrue(completed.audio_produced)
        self.assertFalse(completed.audio_enqueued)
        self.assertEqual(tuple(event.event_sequence for event in completed.events), (1, 2, 3, 4))
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), completed.events)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), completed.audio_chunks)

    def test_terminal_projection_rejects_missing_completed_audio_and_nonterminal_terminal_history(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        completed = self.registry.complete(self.request.provider_request_id, event_id="event-4")

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "completed 必须在 audio_produced 之后"):
            replace(completed, audio_produced=False)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_produced 必须至少对应一个音频块"):
            replace(completed, audio_chunks=())
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态事件只能出现在历史末尾"):
            replace(completed, state=ProviderRequestLifecycleState.SYNTHESIZING)

        current = self.registry.get(self.request.provider_request_id)
        self.assertEqual(current, completed)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), completed.events)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), completed.audio_chunks)

    def test_failed_and_cancelled_terminal_projections_preserve_reason_and_audio_flags(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        self.registry.audio_enqueued(self.request.provider_request_id, event_id="event-4")
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-failed",
            reason="provider failed after enqueue",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(failed.terminal_event, failed.events[-1])
        self.assertEqual(failed.terminal_reason_code, ProviderRequestLifecycleReasonCode.PROVIDER_ERROR)
        self.assertTrue(failed.audio_produced)
        self.assertTrue(failed.audio_enqueued)
        self.assertEqual(tuple(chunk.event_id for chunk in failed.audio_chunks), ("chunk-1",))

        requests = ProviderRequestRegistry(self.requests._capabilities, self.requests._voice_profiles)  # type: ignore[attr-defined]
        request = requests.prepare(
            provider_request_id="provider-request-cancelled-terminal",
            reply_id="reply-cancelled-terminal",
            segment_id="segment-cancelled-terminal",
            request_sequence=1,
            text="取消终态。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )
        registry = ProviderRequestLifecycleRegistry(requests)
        registry.accept(request.provider_request_id, event_id="cancel-event-1")
        registry.synthesis_started(request.provider_request_id, event_id="cancel-event-2")
        registry.record_audio_chunk(
            replace(
                self._chunk(event_id="cancel-chunk-1"),
                provider_request_id=request.provider_request_id,
                reply_id=request.reply_id,
                segment_id=request.segment_id,
            )
        )
        registry.audio_produced(request.provider_request_id, event_id="cancel-event-3")
        registry.audio_enqueued(request.provider_request_id, event_id="cancel-event-4")
        cancelled = registry.cancel(
            request.provider_request_id,
            event_id="cancel-event-cancelled",
            reason="user cancelled after enqueue",
            reason_code=ProviderRequestLifecycleReasonCode.USER_CANCELLED,
        )
        self.assertEqual(cancelled.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(cancelled.terminal_event, cancelled.events[-1])
        self.assertEqual(cancelled.terminal_reason_code, ProviderRequestLifecycleReasonCode.USER_CANCELLED)
        self.assertTrue(cancelled.audio_produced)
        self.assertTrue(cancelled.audio_enqueued)
        self.assertNotIn(ProviderRequestLifecycleEventKind.COMPLETED, {event.kind for event in cancelled.events})

    def test_terminal_rejections_do_not_consume_ids_or_change_generation_observations(self) -> None:
        self.assertEqual(self.registry.activate_generation("voicestudio", 3), 3)
        self.registry.fail(self.request.provider_request_id, event_id="event-failed", reason="local failure")
        before = self.registry.get(self.request.provider_request_id)
        before_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_generation = self.registry.active_generation("voicestudio")

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "已锁定"):
            self.registry.accept(self.request.provider_request_id, event_id="event-late-accept")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "已锁定"):
            self.registry.record_audio_chunk(self._chunk(event_id="chunk-late"))
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不得回退"):
            self.registry.advance_generation("voicestudio", 2)

        after = self.registry.get(self.request.provider_request_id)
        self.assertEqual(after, before)
        self.assertEqual(set(self.registry._event_ids), before_event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.active_generation("voicestudio"), before_generation)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), before.events)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), before.audio_chunks)

    def test_generation_fence_observations_are_cross_request_and_rejections_are_reusable(self) -> None:
        second = self._prepare_second_request()
        generation_four = self.requests.prepare(
            provider_request_id="provider-request-generation-4",
            reply_id="reply-generation-4",
            segment_id="segment-generation-4",
            request_sequence=1,
            text="第四代请求。",
            audio_format=self.audio_format,
            provider_generation=4,
            session_id="session-1",
        )

        self.assertEqual(self.registry.activate_generation("voicestudio", 3), 3)
        first = self.registry.accept(self.request.provider_request_id, event_id="generation-first")
        self.assertIs(self.registry.get(self.request.provider_request_id), first)
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)

        self.assertEqual(self.registry.advance_generation("voicestudio", 4), 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.accept(second.provider_request_id, event_id="generation-reusable")
        self.assertNotIn(second.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertNotIn("generation-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.get(self.request.provider_request_id), first)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)

        accepted_generation_four = self.registry.accept(
            generation_four.provider_request_id,
            event_id="generation-reusable",
        )
        self.assertEqual(accepted_generation_four.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertEqual(
            tuple(event.event_id for event in accepted_generation_four.events),
            ("generation-reusable",),
        )
        self.assertEqual(self.registry.get(self.request.provider_request_id), first)

    def test_terminal_states_are_isolated_across_requests_and_share_commit_boundary(self) -> None:
        second = self._prepare_second_request()
        third = self._prepare_third_request()
        self.registry.accept(self.request.provider_request_id, event_id="completed-accept")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="completed-start")
        self.registry.record_audio_chunk(self._chunk(event_id="completed-chunk"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="completed-produced")
        completed = self.registry.complete(self.request.provider_request_id, event_id="completed-terminal")

        failed = self.registry.fail(
            second.provider_request_id,
            event_id="failed-terminal",
            reason="provider failed",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        cancelled = self.registry.cancel(
            third.provider_request_id,
            event_id="cancelled-terminal",
            reason="user cancelled",
        )

        self.assertEqual(completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(cancelled.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(tuple(event.event_id for event in completed.events), (
            "completed-accept",
            "completed-start",
            "completed-produced",
            "completed-terminal",
        ))
        self.assertEqual(tuple(event.event_id for event in failed.events), ("failed-terminal",))
        self.assertEqual(tuple(event.event_id for event in cancelled.events), ("cancelled-terminal",))
        self.assertEqual(tuple(chunk.event_id for chunk in completed.audio_chunks), ("completed-chunk",))
        self.assertEqual(failed.audio_chunks, ())
        self.assertEqual(cancelled.audio_chunks, ())
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.get(second.provider_request_id), failed)
        self.assertIs(self.registry.get(third.provider_request_id), cancelled)

        fourth = self.requests.prepare(
            provider_request_id="provider-request-terminal-boundary-4",
            reply_id="reply-terminal-boundary-4",
            segment_id="segment-terminal-boundary-4",
            request_sequence=1,
            text="终态边界第四个请求。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "已锁定"):
            self.registry.accept(self.request.provider_request_id, event_id="terminal-reusable")
        self.assertEqual(self.registry.get(self.request.provider_request_id), completed)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertNotIn("terminal-reusable", self.registry._event_ids)  # type: ignore[attr-defined]

        reusable = self.registry.accept(fourth.provider_request_id, event_id="terminal-reusable")
        self.assertEqual(reusable.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertEqual(self.registry.get(second.provider_request_id), failed)
        self.assertEqual(self.registry.get(third.provider_request_id), cancelled)
    def test_generation_fences_are_provider_scoped_and_mixed_history_stays_consistent(self) -> None:
        other = self._prepare_other_provider_request()
        self.registry.activate_generation("voicestudio", 3)
        self.registry.activate_generation("edge_tts", 7)

        self.registry.accept(self.request.provider_request_id, event_id="scoped-first-accept")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="scoped-first-start")
        first_started = self.registry.record_audio_chunk(
            self._chunk(event_id="scoped-first-chunk")
        )
        self.registry.accept(other.provider_request_id, event_id="scoped-other-accept")
        self.registry.synthesis_started(other.provider_request_id, event_id="scoped-other-start")
        other_started = self.registry.record_audio_chunk(
            self._chunk_for_request(other, event_id="scoped-other-chunk")
        )

        self.registry.advance_generation("voicestudio", 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.audio_produced(
                self.request.provider_request_id,
                event_id="scoped-stale-produced",
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), first_started)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

        other_produced = self.registry.audio_produced(
            other.provider_request_id,
            event_id="scoped-other-produced",
        )
        other_enqueued = self.registry.audio_enqueued(
            other.provider_request_id,
            event_id="scoped-other-enqueued",
        )
        other_completed = self.registry.complete(
            other.provider_request_id,
            event_id="scoped-other-completed",
        )
        self.assertIsNot(other_started, other_produced)
        self.assertEqual(other_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertTrue(other_completed.audio_produced)
        self.assertTrue(other_completed.audio_enqueued)
        self.assertEqual(
            tuple(chunk.event_id for chunk in other_completed.audio_chunks),
            ("scoped-other-chunk",),
        )
        self.assertEqual(
            tuple(event.event_id for event in other_enqueued.events),
            (
                "scoped-other-accept",
                "scoped-other-start",
                "scoped-other-produced",
                "scoped-other-enqueued",
            ),
        )

        generation_four = self._prepare_generation_four_request()
        accepted_generation_four = self.registry.accept(
            generation_four.provider_request_id,
            event_id="scoped-generation-four-accept",
        )
        self.assertEqual(accepted_generation_four.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)
        self.assertEqual(
            tuple(event.event_id for event in self.registry.events_for(self.request.provider_request_id)),
            ("scoped-first-accept", "scoped-first-start"),
        )
        self.assertEqual(
            tuple(chunk.event_id for chunk in self.registry.audio_chunks_for(self.request.provider_request_id)),
            ("scoped-first-chunk",),
        )

    def test_terminal_projections_keep_first_facts_across_generation_transition(self) -> None:
        self.registry.activate_generation("voicestudio", 3)
        self.registry.accept(self.request.provider_request_id, event_id="transition-completed-accept")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="transition-completed-start")
        self.registry.record_audio_chunk(self._chunk(event_id="transition-completed-chunk"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="transition-completed-produced")
        completed = self.registry.complete(
            self.request.provider_request_id,
            event_id="transition-completed-terminal",
        )
        self.registry.advance_generation("voicestudio", 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="transition-late-chunk")
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.accept(
                self.request.provider_request_id,
                event_id="transition-late-event",
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]

        failed_request = self._prepare_generation_four_request(
            provider_request_id="provider-request-generation-4-failed",
            segment_id="segment-generation-4-failed",
            request_sequence=2,
        )
        cancelled_request = self._prepare_generation_four_request(
            provider_request_id="provider-request-generation-4-cancelled",
            segment_id="segment-generation-4-cancelled",
            request_sequence=3,
        )
        failed = self.registry.fail(
            failed_request.provider_request_id,
            event_id="transition-failed-terminal",
            reason="generation four provider failure",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        cancelled = self.registry.cancel(
            cancelled_request.provider_request_id,
            event_id="transition-cancelled-terminal",
            reason="generation four user cancellation",
        )
        self.assertEqual(completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(cancelled.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertIsNone(completed.terminal_reason_code)
        self.assertEqual(failed.terminal_reason_code, ProviderRequestLifecycleReasonCode.PROVIDER_ERROR)
        self.assertEqual(cancelled.terminal_reason_code, ProviderRequestLifecycleReasonCode.USER_CANCELLED)
        self.assertEqual(tuple(chunk.event_id for chunk in completed.audio_chunks), ("transition-completed-chunk",))
        self.assertEqual(failed.audio_chunks, ())
        self.assertEqual(cancelled.audio_chunks, ())
        self.assertIs(self.registry.get(failed_request.provider_request_id), failed)
        self.assertIs(self.registry.get(cancelled_request.provider_request_id), cancelled)

    def test_interleaved_terminal_paths_preserve_other_request_projection_and_ids(self) -> None:
        self.registry.activate_generation("voicestudio", 3)
        prepared = self._prepare_batch18_request("prepared")
        accepted = self._prepare_batch18_request("accepted")
        synthesizing = self._prepare_batch18_request("synthesizing")
        produced = self._prepare_batch18_request("produced")
        enqueued = self._prepare_batch18_request("enqueued")
        survivor = self._prepare_batch18_request("survivor")

        failed_prepared = self.registry.fail(
            prepared.provider_request_id,
            event_id="batch18-prepared-failed",
            reason="failure before acceptance",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        accepted_snapshot = self.registry.accept(
            accepted.provider_request_id,
            event_id="batch18-accepted-accepted",
        )
        survivor_accepted = self.registry.accept(
            survivor.provider_request_id,
            event_id="batch18-survivor-accepted",
        )
        cancelled_accepted = self.registry.cancel(
            accepted.provider_request_id,
            event_id="batch18-accepted-cancelled",
            reason="cancelled after acceptance",
        )

        self.registry.accept(synthesizing.provider_request_id, event_id="batch18-synthesizing-accepted")
        self.registry.synthesis_started(
            synthesizing.provider_request_id,
            event_id="batch18-synthesizing-started",
        )
        failed_synthesizing = self.registry.fail(
            synthesizing.provider_request_id,
            event_id="batch18-synthesizing-failed",
            reason="failure during synthesis",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT,
        )

        self.registry.accept(produced.provider_request_id, event_id="batch18-produced-accepted")
        self.registry.synthesis_started(produced.provider_request_id, event_id="batch18-produced-started")
        self.registry.record_audio_chunk(self._chunk_for_request(produced, event_id="batch18-produced-chunk"))
        self.registry.audio_produced(produced.provider_request_id, event_id="batch18-produced-audio-produced")
        failed_produced = self.registry.fail(
            produced.provider_request_id,
            event_id="batch18-produced-failed",
            reason="failure after audio produced",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )

        self.registry.accept(enqueued.provider_request_id, event_id="batch18-enqueued-accepted")
        self.registry.synthesis_started(enqueued.provider_request_id, event_id="batch18-enqueued-started")
        self.registry.record_audio_chunk(self._chunk_for_request(enqueued, event_id="batch18-enqueued-chunk"))
        self.registry.audio_produced(enqueued.provider_request_id, event_id="batch18-enqueued-audio-produced")
        self.registry.audio_enqueued(enqueued.provider_request_id, event_id="batch18-enqueued-audio-enqueued")
        cancelled_enqueued = self.registry.cancel(
            enqueued.provider_request_id,
            event_id="batch18-enqueued-cancelled",
            reason="cancelled after audio enqueued",
        )

        self.registry.synthesis_started(survivor.provider_request_id, event_id="batch18-survivor-started")
        self.registry.record_audio_chunk(self._chunk_for_request(survivor, event_id="batch18-survivor-chunk"))
        self.registry.audio_produced(survivor.provider_request_id, event_id="batch18-survivor-audio-produced")
        self.registry.audio_enqueued(survivor.provider_request_id, event_id="batch18-survivor-audio-enqueued")
        survivor_completed = self.registry.complete(
            survivor.provider_request_id,
            event_id="batch18-survivor-completed",
        )

        self.assertEqual(failed_prepared.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(cancelled_accepted.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(failed_synthesizing.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(failed_produced.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(cancelled_enqueued.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(survivor_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertEqual(failed_prepared.events[-1].kind, ProviderRequestLifecycleEventKind.FAILED)
        self.assertEqual(cancelled_accepted.events[-1].kind, ProviderRequestLifecycleEventKind.CANCELLED)
        self.assertEqual(failed_synthesizing.events[-1].kind, ProviderRequestLifecycleEventKind.FAILED)
        self.assertTrue(failed_produced.audio_produced)
        self.assertFalse(failed_produced.audio_enqueued)
        self.assertTrue(cancelled_enqueued.audio_produced)
        self.assertTrue(cancelled_enqueued.audio_enqueued)
        self.assertEqual(
            tuple(chunk.event_id for chunk in failed_produced.audio_chunks),
            ("batch18-produced-chunk",),
        )
        self.assertEqual(
            tuple(chunk.event_id for chunk in cancelled_enqueued.audio_chunks),
            ("batch18-enqueued-chunk",),
        )
        self.assertEqual(
            tuple(event.event_id for event in survivor_completed.events),
            (
                "batch18-survivor-accepted",
                "batch18-survivor-started",
                "batch18-survivor-audio-produced",
                "batch18-survivor-audio-enqueued",
                "batch18-survivor-completed",
            ),
        )
        self.assertEqual(
            tuple(chunk.event_id for chunk in survivor_completed.audio_chunks),
            ("batch18-survivor-chunk",),
        )
        self.assertIs(self.registry.get(survivor.provider_request_id), survivor_completed)
        self.assertIs(self.registry.get(accepted.provider_request_id), cancelled_accepted)
        self.assertIs(self.registry.get(produced.provider_request_id), failed_produced)
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)

        event_ids_before_rejection = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "已锁定"):
            self.registry.accept(produced.provider_request_id, event_id="batch18-reusable-id")
        self.assertEqual(self.registry._event_ids, event_ids_before_rejection)  # type: ignore[attr-defined]

        reusable = self._prepare_batch18_request("reusable")
        reusable_accepted = self.registry.accept(reusable.provider_request_id, event_id="batch18-reusable-id")
        self.assertEqual(reusable_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertEqual(tuple(event.event_id for event in reusable_accepted.events), ("batch18-reusable-id",))
        self.assertIs(self.registry.get(survivor.provider_request_id), survivor_completed)
        self.assertIs(self.registry.get(prepared.provider_request_id), failed_prepared)

    def test_interleaved_rejected_writes_keep_snapshots_ids_and_generation_observations_atomic(self) -> None:
        old_first = self._prepare_batch18_request("old-first")
        old_second = self._prepare_batch18_request("old-second")
        other_provider = self._prepare_other_provider_request()
        self.registry.activate_generation("voicestudio", 3)
        self.registry.activate_generation("edge_tts", 7)

        first_started = self.registry.accept(old_first.provider_request_id, event_id="batch18-old-first-accepted")
        first_started = self.registry.synthesis_started(
            old_first.provider_request_id,
            event_id="batch18-old-first-started",
        )
        second_accepted = self.registry.accept(old_second.provider_request_id, event_id="batch18-old-second-accepted")
        other_started = self.registry.accept(other_provider.provider_request_id, event_id="batch18-other-accepted")
        other_started = self.registry.synthesis_started(
            other_provider.provider_request_id,
            event_id="batch18-other-started",
        )
        other_with_chunk = self.registry.record_audio_chunk(
            self._chunk_for_request(other_provider, event_id="batch18-other-chunk")
        )

        self.registry.advance_generation("voicestudio", 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_first = self.registry.get(old_first.provider_request_id)
        before_second = self.registry.get(old_second.provider_request_id)
        before_other = self.registry.get(other_provider.provider_request_id)

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.audio_produced(old_first.provider_request_id, event_id="batch18-stale-produced")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.record_audio_chunk(
                self._chunk_for_request(old_second, event_id="batch18-stale-chunk")
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.fail(
                old_second.provider_request_id,
                event_id="batch18-stale-failed",
                reason="stale generation failure",
            )

        self.assertIs(self.registry.get(old_first.provider_request_id), before_first)
        self.assertIs(self.registry.get(old_second.provider_request_id), before_second)
        self.assertIs(self.registry.get(other_provider.provider_request_id), before_other)
        self.assertEqual(self.registry.events_for(old_first.provider_request_id), before_first.events)
        self.assertEqual(self.registry.events_for(old_second.provider_request_id), before_second.events)
        self.assertEqual(self.registry.audio_chunks_for(other_provider.provider_request_id), before_other.audio_chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

        other_produced = self.registry.audio_produced(
            other_provider.provider_request_id,
            event_id="batch18-other-produced",
        )
        other_enqueued = self.registry.audio_enqueued(
            other_provider.provider_request_id,
            event_id="batch18-other-enqueued",
        )
        other_completed = self.registry.complete(
            other_provider.provider_request_id,
            event_id="batch18-other-completed",
        )
        self.assertIsNot(other_started, other_with_chunk)
        self.assertIsNot(other_with_chunk, other_produced)
        self.assertEqual(other_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertTrue(other_completed.audio_produced)
        self.assertTrue(other_completed.audio_enqueued)
        self.assertEqual(
            tuple(event.event_id for event in other_completed.events),
            (
                "batch18-other-accepted",
                "batch18-other-started",
                "batch18-other-produced",
                "batch18-other-enqueued",
                "batch18-other-completed",
            ),
        )
        self.assertEqual(tuple(chunk.event_id for chunk in other_completed.audio_chunks), ("batch18-other-chunk",))
        self.assertIs(self.registry.get(old_first.provider_request_id), before_first)
        self.assertIs(self.registry.get(old_second.provider_request_id), before_second)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)
    def test_terminal_projection_matrix_exposes_reason_flags_and_read_only_identity(self) -> None:
        self.registry.activate_generation("voicestudio", 3)

        prepared_failed = self.registry.fail(
            self._prepare_batch18_request("batch19-prepared-failed").provider_request_id,
            event_id="batch19-prepared-failed-terminal",
            reason="failed_before_acceptance",
            reason_code=ProviderRequestLifecycleReasonCode.LOCAL_GATE_REJECTED,
        )
        accepted_request = self._prepare_batch18_request("batch19-accepted-cancelled")
        self.registry.accept(accepted_request.provider_request_id, event_id="batch19-accepted-cancelled-accepted")
        accepted_cancelled = self.registry.cancel(
            accepted_request.provider_request_id,
            event_id="batch19-accepted-cancelled-terminal",
            reason="cancelled_after_acceptance",
            reason_code=ProviderRequestLifecycleReasonCode.USER_CANCELLED,
        )
        synthesizing_request = self._prepare_batch18_request("batch19-synthesizing-failed")
        self.registry.accept(synthesizing_request.provider_request_id, event_id="batch19-synthesizing-failed-accepted")
        self.registry.synthesis_started(
            synthesizing_request.provider_request_id,
            event_id="batch19-synthesizing-failed-started",
        )
        synthesizing_failed = self.registry.fail(
            synthesizing_request.provider_request_id,
            event_id="batch19-synthesizing-failed-terminal",
            reason="failed_during_synthesis",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT,
        )
        produced_request = self._prepare_batch18_request("batch19-produced-failed")
        self.registry.accept(produced_request.provider_request_id, event_id="batch19-produced-failed-accepted")
        self.registry.synthesis_started(produced_request.provider_request_id, event_id="batch19-produced-failed-started")
        self.registry.record_audio_chunk(self._chunk_for_request(produced_request, event_id="batch19-produced-failed-chunk"))
        self.registry.audio_produced(produced_request.provider_request_id, event_id="batch19-produced-failed-produced")
        produced_failed = self.registry.fail(
            produced_request.provider_request_id,
            event_id="batch19-produced-failed-terminal",
            reason="failed_after_audio_produced",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        enqueued_request = self._prepare_batch18_request("batch19-enqueued-cancelled")
        self.registry.accept(enqueued_request.provider_request_id, event_id="batch19-enqueued-cancelled-accepted")
        self.registry.synthesis_started(enqueued_request.provider_request_id, event_id="batch19-enqueued-cancelled-started")
        self.registry.record_audio_chunk(self._chunk_for_request(enqueued_request, event_id="batch19-enqueued-cancelled-chunk"))
        self.registry.audio_produced(enqueued_request.provider_request_id, event_id="batch19-enqueued-cancelled-produced")
        self.registry.audio_enqueued(enqueued_request.provider_request_id, event_id="batch19-enqueued-cancelled-enqueued")
        enqueued_cancelled = self.registry.cancel(
            enqueued_request.provider_request_id,
            event_id="batch19-enqueued-cancelled-terminal",
            reason="cancelled_after_audio_enqueued",
            reason_code=ProviderRequestLifecycleReasonCode.SUPERSEDED,
        )
        completed_request = self._prepare_batch18_request("batch19-completed")
        self.registry.accept(completed_request.provider_request_id, event_id="batch19-completed-accepted")
        self.registry.synthesis_started(completed_request.provider_request_id, event_id="batch19-completed-started")
        self.registry.record_audio_chunk(self._chunk_for_request(completed_request, event_id="batch19-completed-chunk"))
        self.registry.audio_produced(completed_request.provider_request_id, event_id="batch19-completed-produced")
        self.registry.audio_enqueued(completed_request.provider_request_id, event_id="batch19-completed-enqueued")
        completed = self.registry.complete(completed_request.provider_request_id, event_id="batch19-completed-terminal")

        cases = (
            (
                prepared_failed,
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.LOCAL_GATE_REJECTED,
                "failed_before_acceptance",
                (False, False),
                (),
                ("batch19-prepared-failed-terminal",),
            ),
            (
                accepted_cancelled,
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                "cancelled_after_acceptance",
                (False, False),
                (),
                ("batch19-accepted-cancelled-accepted", "batch19-accepted-cancelled-terminal"),
            ),
            (
                synthesizing_failed,
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_TIMEOUT,
                "failed_during_synthesis",
                (False, False),
                (),
                (
                    "batch19-synthesizing-failed-accepted",
                    "batch19-synthesizing-failed-started",
                    "batch19-synthesizing-failed-terminal",
                ),
            ),
            (
                produced_failed,
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                "failed_after_audio_produced",
                (True, False),
                ("batch19-produced-failed-chunk",),
                (
                    "batch19-produced-failed-accepted",
                    "batch19-produced-failed-started",
                    "batch19-produced-failed-produced",
                    "batch19-produced-failed-terminal",
                ),
            ),
            (
                enqueued_cancelled,
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.SUPERSEDED,
                "cancelled_after_audio_enqueued",
                (True, True),
                ("batch19-enqueued-cancelled-chunk",),
                (
                    "batch19-enqueued-cancelled-accepted",
                    "batch19-enqueued-cancelled-started",
                    "batch19-enqueued-cancelled-produced",
                    "batch19-enqueued-cancelled-enqueued",
                    "batch19-enqueued-cancelled-terminal",
                ),
            ),
            (
                completed,
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                None,
                (True, True),
                ("batch19-completed-chunk",),
                (
                    "batch19-completed-accepted",
                    "batch19-completed-started",
                    "batch19-completed-produced",
                    "batch19-completed-enqueued",
                    "batch19-completed-terminal",
                ),
            ),
        )
        for snapshot, state, terminal_kind, reason_code, reason, flags, chunk_ids, event_ids in cases:
            request_id = snapshot.request.provider_request_id
            self.assertTrue(snapshot.terminal)
            self.assertEqual(snapshot.state, state)
            self.assertIs(self.registry.get(request_id), snapshot)
            self.assertIs(self.registry.events_for(request_id), snapshot.events)
            self.assertIs(self.registry.audio_chunks_for(request_id), snapshot.audio_chunks)
            self.assertIs(snapshot.terminal_event, snapshot.events[-1])
            self.assertEqual(snapshot.terminal_event.kind, terminal_kind)
            self.assertEqual(snapshot.terminal_event.reason_code, reason_code)
            self.assertEqual(snapshot.terminal_event.reason, reason)
            self.assertEqual(snapshot.terminal_reason_code, reason_code)
            self.assertEqual((snapshot.audio_produced, snapshot.audio_enqueued), flags)
            self.assertEqual(tuple(chunk.event_id for chunk in snapshot.audio_chunks), chunk_ids)
            self.assertEqual(tuple(event.event_id for event in snapshot.events), event_ids)

    def test_generation_fence_final_rejection_matrix_preserves_public_projections_and_observations(self) -> None:
        old_event = self._prepare_batch18_request("batch19-old-event")
        old_audio = self._prepare_batch18_request("batch19-old-audio")
        other_provider = self._prepare_other_provider_request()
        self.registry.activate_generation("voicestudio", 3)
        self.registry.activate_generation("edge_tts", 7)

        old_event_accepted = self.registry.accept(old_event.provider_request_id, event_id="batch19-old-event-accepted")
        self.registry.accept(old_audio.provider_request_id, event_id="batch19-old-audio-accepted")
        self.registry.synthesis_started(old_audio.provider_request_id, event_id="batch19-old-audio-started")
        old_audio_started = self.registry.record_audio_chunk(
            self._chunk_for_request(old_audio, event_id="batch19-old-audio-chunk")
        )
        self.registry.accept(other_provider.provider_request_id, event_id="batch19-other-accepted")
        self.registry.synthesis_started(other_provider.provider_request_id, event_id="batch19-other-started")
        self.registry.record_audio_chunk(self._chunk_for_request(other_provider, event_id="batch19-other-chunk"))

        self.registry.advance_generation("voicestudio", 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_old_event = self.registry.get(old_event.provider_request_id)
        before_old_audio = self.registry.get(old_audio.provider_request_id)
        before_other = self.registry.get(other_provider.provider_request_id)
        before_voicestudio_generation = self.registry.active_generation("voicestudio")
        before_edge_generation = self.registry.active_generation("edge_tts")

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.audio_produced(old_audio.provider_request_id, event_id="batch19-late-produced")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.record_audio_chunk(
                self._chunk_for_request(
                    old_audio,
                    event_id="batch19-late-chunk",
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.fail(
                old_event.provider_request_id,
                event_id="batch19-late-failed",
                reason="late failure",
                reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.cancel(
                old_event.provider_request_id,
                event_id="batch19-late-cancelled",
                reason="late cancellation",
                reason_code=ProviderRequestLifecycleReasonCode.USER_CANCELLED,
            )

        self.assertIs(self.registry.get(old_event.provider_request_id), before_old_event)
        self.assertIs(self.registry.get(old_audio.provider_request_id), before_old_audio)
        self.assertIs(self.registry.get(other_provider.provider_request_id), before_other)
        self.assertIs(self.registry.events_for(old_event.provider_request_id), before_old_event.events)
        self.assertIs(self.registry.events_for(old_audio.provider_request_id), before_old_audio.events)
        self.assertIs(self.registry.audio_chunks_for(old_audio.provider_request_id), before_old_audio.audio_chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.active_generation("voicestudio"), before_voicestudio_generation)
        self.assertEqual(self.registry.active_generation("edge_tts"), before_edge_generation)
        self.assertIs(self.registry.get(old_audio.provider_request_id), old_audio_started)

        other_completed = self.registry.audio_produced(other_provider.provider_request_id, event_id="batch19-other-produced")
        other_completed = self.registry.audio_enqueued(other_provider.provider_request_id, event_id="batch19-other-enqueued")
        other_completed = self.registry.complete(other_provider.provider_request_id, event_id="batch19-other-completed")
        self.assertEqual(other_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertTrue(other_completed.audio_produced)
        self.assertTrue(other_completed.audio_enqueued)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)
        self.assertIs(self.registry.get(old_event.provider_request_id), before_old_event)
        self.assertIs(self.registry.get(old_audio.provider_request_id), before_old_audio)

    def _prepare_batch18_request(self, suffix: str):
        return self.requests.prepare(
            provider_request_id=f"provider-request-batch18-{suffix}",
            reply_id=f"reply-batch18-{suffix}",
            segment_id=f"segment-batch18-{suffix}",
            request_sequence=1,
            text=f"批次 18 {suffix} 句段。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )
    def _prepare_other_provider_request(
        self,
        suffix: str | None = None,
        *,
        provider_generation: int = 7,
    ):
        identity_suffix = "" if suffix is None else f"-{suffix}"
        self.profiles.select("assistant-other", actor=VoiceProfileActor.USER)
        other = self.requests.prepare(
            provider_request_id=f"provider-request-edge-tts{identity_suffix}",
            reply_id=f"reply-edge-tts{identity_suffix}",
            segment_id=f"segment-edge-tts{identity_suffix}",
            request_sequence=1,
            text="备用 Provider 句段。",
            audio_format=self.audio_format,
            provider_generation=provider_generation,
            session_id=f"session-edge-tts{identity_suffix}",
        )
        self.profiles.select("assistant-default", actor=VoiceProfileActor.USER)
        return other
    def _prepare_generation_four_request(
        self,
        *,
        provider_request_id: str = "provider-request-generation-4",
        segment_id: str = "segment-generation-4",
        request_sequence: int = 2,
        reply_id: str = "reply-1",
    ):
        return self.requests.prepare(
            provider_request_id=provider_request_id,
            reply_id=reply_id,
            segment_id=segment_id,
            request_sequence=request_sequence,
            text="第四代 Provider 句段。",
            audio_format=self.audio_format,
            provider_generation=4,
            session_id="session-1",
        )

    def _chunk_for_request(
        self,
        request,
        *,
        event_id: str,
        audio_chunk_index: int = 1,
        receive_sequence: int = 1,
    ) -> ProviderRequestAudioChunk:
        return ProviderRequestAudioChunk(
            event_id=event_id,
            audio_chunk_index=audio_chunk_index,
            receive_sequence=receive_sequence,
            provider_request_id=request.provider_request_id,
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_name=request.provider_name,
            provider_generation=request.provider_generation,
            voice_profile_id=request.voice_profile_id,
            voice_profile_version=request.voice_profile_version,
            audio_format=request.audio_format,
            capability_check=request.capability_check,
        )
    def _prepare_second_request(self):
        return self.requests.prepare(
            provider_request_id="provider-request-2",
            reply_id="reply-2",
            segment_id="segment-2",
            request_sequence=1,
            text="第二句。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )

    def _prepare_third_request(self):
        return self.requests.prepare(
            provider_request_id="provider-request-3",
            reply_id="reply-3",
            segment_id="segment-3",
            request_sequence=1,
            text="第三句。",
            audio_format=self.audio_format,
            provider_generation=3,
            session_id="session-1",
        )
    def _chunk(self, *, event_id: str, audio_chunk_index: int = 1, receive_sequence: int = 1, provider_request_id: str | None = None, provider_generation: int | None = None) -> ProviderRequestAudioChunk:
        request = self.request
        return ProviderRequestAudioChunk(
            event_id=event_id,
            audio_chunk_index=audio_chunk_index,
            receive_sequence=receive_sequence,
            provider_request_id=provider_request_id or request.provider_request_id,
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_name=request.provider_name,
            provider_generation=provider_generation or request.provider_generation,
            voice_profile_id=request.voice_profile_id,
            voice_profile_version=request.voice_profile_version,
            audio_format=request.audio_format,
            capability_check=request.capability_check,
        )

    def test_audio_chunks_are_immutable_bound_metadata_and_distinct_from_playback(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        first = self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        second = self.registry.record_audio_chunk(
            self._chunk(event_id="chunk-2", audio_chunk_index=2, receive_sequence=2)
        )

        self.assertEqual([chunk.audio_chunk_index for chunk in second.audio_chunks], [1, 2])
        self.assertEqual([chunk.receive_sequence for chunk in second.audio_chunks], [1, 2])
        self.assertEqual(first.audio_chunks[0].event_id, "chunk-1")
        self.assertTrue(second.audio_produced is False)
        self.assertFalse(second.audio_enqueued)
        self.assertFalse(hasattr(second, "played"))
        with self.assertRaises(FrozenInstanceError):
            second.audio_chunks[0].audio_chunk_index = 9  # type: ignore[misc]

    def test_audio_chunks_reject_duplicate_out_of_order_cross_request_and_atomic_failures(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不得重复"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="chunk-duplicate", audio_chunk_index=1, receive_sequence=1)
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "期望 2"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="chunk-out-of-order", audio_chunk_index=3, receive_sequence=3)
            )
        self.assertEqual(len(self.registry.audio_chunks_for(self.request.provider_request_id)), 1)

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "provider_request_id"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="chunk-cross", provider_request_id="missing-request")
            )
        self.assertEqual(len(self.registry.audio_chunks_for(self.request.provider_request_id)), 1)

    def test_audio_chunks_reject_stale_future_and_terminal_late_chunks(self) -> None:
        stale_registry = ProviderRequestLifecycleRegistry(self.requests)
        stale_registry.accept(self.request.provider_request_id, event_id="event-stale-accept")
        stale_registry.synthesis_started(self.request.provider_request_id, event_id="event-stale-start")
        stale_registry.activate_generation("voicestudio", 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            stale_registry.record_audio_chunk(
                self._chunk(event_id="chunk-stale", provider_generation=3)
            )
        self.assertEqual(stale_registry.audio_chunks_for(self.request.provider_request_id), ())

        future_registry = ProviderRequestLifecycleRegistry(self.requests)
        future_registry.accept(self.request.provider_request_id, event_id="event-future-accept")
        future_registry.synthesis_started(self.request.provider_request_id, event_id="event-future-start")
        future_registry.activate_generation("voicestudio", 2)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "尚未激活"):
            future_registry.record_audio_chunk(
                self._chunk(event_id="chunk-future", provider_generation=3)
            )
        self.assertEqual(future_registry.audio_chunks_for(self.request.provider_request_id), ())

        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.fail(self.request.provider_request_id, event_id="event-f", reason="local gate")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="chunk-late", audio_chunk_index=2, receive_sequence=2)
            )
        snapshot = self.registry.get(self.request.provider_request_id)
        self.assertEqual(len(snapshot.audio_chunks), 1)
        self.assertEqual(snapshot.state, ProviderRequestLifecycleState.FAILED)

    def test_audio_chunk_identity_mismatch_is_rejected_atomically(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        mismatched = self._chunk(event_id="chunk-bad")
        mismatched = replace(mismatched, audio_format=AudioFormat("opus", 48000, 2))
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_format"):
            self.registry.record_audio_chunk(mismatched)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), ())

    def test_audio_produced_requires_non_empty_chunk_projection(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "至少对应一个音频块"):
            self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), self.registry.events_for(self.request.provider_request_id)[:2])
        self.assertFalse(self.registry.get(self.request.provider_request_id).audio_produced)
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        produced = self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        self.assertTrue(produced.audio_produced)
        self.assertEqual(len(produced.audio_chunks), 1)

    def test_audio_chunk_set_is_frozen_after_audio_produced(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "不能追加"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="chunk-2", audio_chunk_index=2, receive_sequence=2)
            )
        snapshot = self.registry.get(self.request.provider_request_id)
        self.assertEqual(len(snapshot.audio_chunks), 1)
        self.assertEqual(len(snapshot.events), 3)
        completed = self.registry.complete(self.request.provider_request_id, event_id="event-4")
        self.assertEqual(completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertEqual(len(completed.audio_chunks), 1)

    def test_completed_requires_non_empty_chunk_projection(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "至少对应一个音频块"):
            self.registry.complete(self.request.provider_request_id, event_id="event-3")
        self.assertEqual(len(self.registry.events_for(self.request.provider_request_id)), 2)

    def test_event_identity_and_state_are_immutable(self) -> None:
        accepted = self.registry.accept(self.request.provider_request_id, event_id="event-1")
        with self.assertRaises(FrozenInstanceError):
            accepted.state = ProviderRequestLifecycleState.COMPLETED  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            accepted.events[0].kind = ProviderRequestLifecycleEventKind.COMPLETED  # type: ignore[misc]

    def test_audio_pipeline_fact_is_not_playback_or_user_heard_fact(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        snapshot = self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        self.assertTrue(snapshot.audio_produced)
        self.assertFalse(snapshot.audio_enqueued)
        self.assertFalse(hasattr(snapshot, "played"))
        self.assertNotIn("play", {event.kind.value for event in snapshot.events})


    def test_failure_without_audio_chunks_keeps_empty_audio_projection(self) -> None:
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-failed-empty",
            reason="provider_failed_before_audio",
        )
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(failed.audio_chunks, ())
        self.assertFalse(failed.audio_produced)
        self.assertFalse(failed.audio_enqueued)
        self.assertEqual(failed.terminal_event.kind, ProviderRequestLifecycleEventKind.FAILED)

    def test_cancellation_after_partial_audio_preserves_only_received_chunks(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.record_audio_chunk(
            self._chunk(event_id="chunk-2", audio_chunk_index=2, receive_sequence=2)
        )
        cancelled = self.registry.cancel(
            self.request.provider_request_id,
            event_id="event-cancelled-partial",
            reason="user_cancelled_after_partial_audio",
        )
        self.assertEqual(cancelled.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(tuple(chunk.event_id for chunk in cancelled.audio_chunks), ("chunk-1", "chunk-2"))
        self.assertFalse(cancelled.audio_produced)
        self.assertFalse(cancelled.audio_enqueued)
        self.assertEqual(
            tuple(event.kind for event in cancelled.events),
            (
                ProviderRequestLifecycleEventKind.ACCEPTED,
                ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
                ProviderRequestLifecycleEventKind.CANCELLED,
            ),
        )
    def test_failure_after_audio_produced_preserves_chunks_and_produced_fact_without_enqueue(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.record_audio_chunk(
            self._chunk(event_id="chunk-2", audio_chunk_index=2, receive_sequence=2)
        )
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-failed-after-produced",
            reason="provider_failed_after_audio_produced",
        )
        self.assertEqual(failed.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(tuple(chunk.event_id for chunk in failed.audio_chunks), ("chunk-1", "chunk-2"))
        self.assertTrue(failed.audio_produced)
        self.assertFalse(failed.audio_enqueued)
        self.assertNotIn(ProviderRequestLifecycleEventKind.COMPLETED, {event.kind for event in failed.events})

    def test_cancellation_after_audio_enqueued_preserves_all_local_facts_without_completed(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        self.registry.audio_enqueued(self.request.provider_request_id, event_id="event-4")
        cancelled = self.registry.cancel(
            self.request.provider_request_id,
            event_id="event-cancelled-after-enqueue",
            reason="user_cancelled_after_audio_enqueued",
        )
        self.assertEqual(cancelled.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(tuple(chunk.event_id for chunk in cancelled.audio_chunks), ("chunk-1",))
        self.assertTrue(cancelled.audio_produced)
        self.assertTrue(cancelled.audio_enqueued)
        self.assertEqual(cancelled.terminal_event.kind, ProviderRequestLifecycleEventKind.CANCELLED)
        self.assertNotIn(ProviderRequestLifecycleEventKind.COMPLETED, {event.kind for event in cancelled.events})

    def test_snapshot_rejects_non_contiguous_or_misbundled_event_history(self) -> None:
        accepted = self.registry.accept(self.request.provider_request_id, event_id="event-1")

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_sequence"):
            replace(
                accepted,
                events=(replace(accepted.events[0], event_sequence=2),),
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "完整绑定"):
            replace(
                accepted,
                events=(replace(accepted.events[0], reply_id="reply-other"),),
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "事件历史中重复"):
            replace(
                accepted,
                events=(
                    accepted.events[0],
                    replace(accepted.events[0], event_sequence=2),
                ),
            )

        self.assertEqual(self.registry.get(self.request.provider_request_id), accepted)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), accepted.events)

    def test_snapshot_rejects_event_and_audio_chunk_id_collision(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        started = self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        chunk = self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "事件和音频块之间重复"):
            replace(
                chunk,
                audio_chunks=(replace(chunk.audio_chunks[0], event_id="event-2"),),
            )

        current = self.registry.get(self.request.provider_request_id)
        self.assertIs(current, chunk)
        self.assertEqual(current.events, started.events)
        self.assertEqual(current.audio_chunks, chunk.audio_chunks)

    def test_snapshot_audio_facts_must_match_event_history_atomically(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="batch27-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch27-started")
        self.registry.record_audio_chunk(self._chunk(event_id="batch27-chunk"))
        produced = self.registry.audio_produced(self.request.provider_request_id, event_id="batch27-produced")

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_produced"):
            replace(produced, audio_produced=False)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_produced"):
            replace(produced, events=produced.events[:-1])

        enqueued = self.registry.audio_enqueued(self.request.provider_request_id, event_id="batch27-enqueued")
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_enqueued"):
            replace(enqueued, audio_enqueued=False)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_enqueued"):
            replace(enqueued, events=enqueued.events[:-1])

        current = self.registry.get(self.request.provider_request_id)
        self.assertIs(current, enqueued)
        self.assertTrue(current.audio_produced)
        self.assertTrue(current.audio_enqueued)
        self.assertEqual(
            tuple(event.kind for event in current.events),
            (
                ProviderRequestLifecycleEventKind.ACCEPTED,
                ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
                ProviderRequestLifecycleEventKind.AUDIO_PRODUCED,
                ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED,
            ),
        )


    def test_snapshot_audio_chunks_must_follow_synthesis_start_atomically(self) -> None:
        accepted = self.registry.accept(self.request.provider_request_id, event_id="batch29-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch29-started")
        with_chunks = self.registry.record_audio_chunk(self._chunk(event_id="batch29-chunk"))
        events = self.registry.events_for(self.request.provider_request_id)
        chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_chunks.*synthesis_started"):
            replace(accepted, audio_chunks=chunks)

        current = self.registry.get(self.request.provider_request_id)
        self.assertIs(current, with_chunks)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]

    def test_terminal_snapshot_audio_chunks_require_synthesis_start_atomically(self) -> None:
        cases = (
            (self.request, "failed", ProviderRequestLifecycleEventKind.FAILED),
            (self._prepare_second_request(), "cancelled", ProviderRequestLifecycleEventKind.CANCELLED),
        )
        for request, label, terminal_kind in cases:
            with self.subTest(state=label):
                accepted = self.registry.accept(
                    request.provider_request_id,
                    event_id=f"batch30-{label}-accepted",
                )
                self.registry.synthesis_started(
                    request.provider_request_id,
                    event_id=f"batch30-{label}-started",
                )
                with_chunks = self.registry.record_audio_chunk(
                    self._chunk_for_request(
                        request,
                        event_id=f"batch30-{label}-chunk",
                    )
                )
                events = self.registry.events_for(request.provider_request_id)
                chunks = self.registry.audio_chunks_for(request.provider_request_id)
                before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
                terminal_event = replace(
                    accepted.events[-1],
                    event_id=f"batch30-{label}-terminal",
                    event_sequence=2,
                    kind=terminal_kind,
                    reason=f"malformed {label} snapshot",
                    reason_code=(
                        ProviderRequestLifecycleReasonCode.PROVIDER_ERROR
                        if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                        else ProviderRequestLifecycleReasonCode.USER_CANCELLED
                    ),
                )
                with self.assertRaisesRegex(
                    ProviderRequestLifecycleError,
                    "audio_chunks.*synthesis_started",
                ):
                    replace(
                        accepted,
                        state=(
                            ProviderRequestLifecycleState.FAILED
                            if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                            else ProviderRequestLifecycleState.CANCELLED
                        ),
                        events=(accepted.events[0], terminal_event),
                        audio_chunks=chunks,
                    )

                self.assertIs(self.registry.get(request.provider_request_id), with_chunks)
                self.assertIs(self.registry.events_for(request.provider_request_id), events)
                self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), chunks)
                self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]


    def test_terminal_partial_audio_projection_replays_synthesis_boundary_atomically(self) -> None:
        cases = (
            (self.request, "failed", ProviderRequestLifecycleEventKind.FAILED),
            (self._prepare_second_request(), "cancelled", ProviderRequestLifecycleEventKind.CANCELLED),
        )
        for request, label, terminal_kind in cases:
            with self.subTest(state=label):
                self.registry.accept(request.provider_request_id, event_id=f"batch31-{label}-accepted")
                self.registry.synthesis_started(
                    request.provider_request_id,
                    event_id=f"batch31-{label}-started",
                )
                partial = self.registry.record_audio_chunk(
                    self._chunk_for_request(
                        request,
                        event_id=f"batch31-{label}-chunk",
                    )
                )
                terminal = (
                    self.registry.fail(
                        request.provider_request_id,
                        event_id=f"batch31-{label}-terminal",
                        reason="partial audio terminal failure",
                        reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else self.registry.cancel(
                        request.provider_request_id,
                        event_id=f"batch31-{label}-terminal",
                        reason="partial audio terminal cancellation",
                    )
                )
                self.assertEqual(terminal.state.value, label)
                self.assertEqual(
                    tuple(event.kind for event in terminal.events),
                    (
                        ProviderRequestLifecycleEventKind.ACCEPTED,
                        ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
                        terminal_kind,
                    ),
                )
                self.assertEqual(terminal.audio_chunks, partial.audio_chunks)
                self.assertFalse(terminal.audio_produced)
                self.assertFalse(terminal.audio_enqueued)

                before_snapshot = self.registry.get(request.provider_request_id)
                before_events = self.registry.events_for(request.provider_request_id)
                before_chunks = self.registry.audio_chunks_for(request.provider_request_id)
                before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
                malformed_terminal = replace(
                    terminal.events[-1],
                    event_id=f"batch31-{label}-malformed-terminal",
                    event_sequence=2,
                )
                with self.assertRaisesRegex(
                    ProviderRequestLifecycleError,
                    "audio_chunks.*synthesis_started",
                ):
                    replace(
                        terminal,
                        events=(terminal.events[0], malformed_terminal),
                        audio_chunks=terminal.audio_chunks,
                    )

                self.assertIs(self.registry.get(request.provider_request_id), before_snapshot)
                self.assertIs(self.registry.events_for(request.provider_request_id), before_events)
                self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), before_chunks)
                self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]

    def test_terminal_multi_partial_audio_projection_preserves_order_and_identity(self) -> None:
        cases = (
            (self.request, "failed", ProviderRequestLifecycleEventKind.FAILED),
            (self._prepare_second_request(), "cancelled", ProviderRequestLifecycleEventKind.CANCELLED),
        )
        for request, label, terminal_kind in cases:
            with self.subTest(state=label):
                self.registry.accept(request.provider_request_id, event_id=f"batch32-{label}-accepted")
                self.registry.synthesis_started(
                    request.provider_request_id,
                    event_id=f"batch32-{label}-started",
                )
                first = self.registry.record_audio_chunk(
                    self._chunk_for_request(
                        request,
                        event_id=f"batch32-{label}-chunk-1",
                    )
                )
                second = self.registry.record_audio_chunk(
                    self._chunk_for_request(
                        request,
                        event_id=f"batch32-{label}-chunk-2",
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                before_chunks = self.registry.audio_chunks_for(request.provider_request_id)
                self.assertIs(before_chunks, second.audio_chunks)
                terminal = (
                    self.registry.fail(
                        request.provider_request_id,
                        event_id=f"batch32-{label}-terminal",
                        reason="multi-partial audio terminal failure",
                        reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else self.registry.cancel(
                        request.provider_request_id,
                        event_id=f"batch32-{label}-terminal",
                        reason="multi-partial audio terminal cancellation",
                    )
                )
                self.assertEqual(terminal.state.value, label)
                self.assertEqual(terminal.events[:-1], second.events)
                self.assertEqual(terminal.events[-1].kind, terminal_kind)
                self.assertIs(terminal.audio_chunks, before_chunks)
                self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), before_chunks)
                self.assertIs(terminal.audio_chunks[0], first.audio_chunks[0])
                self.assertIs(terminal.audio_chunks[1], second.audio_chunks[1])
                self.assertEqual(
                    tuple(chunk.event_id for chunk in terminal.audio_chunks),
                    (f"batch32-{label}-chunk-1", f"batch32-{label}-chunk-2"),
                )
                self.assertFalse(terminal.audio_produced)
                self.assertFalse(terminal.audio_enqueued)

    def test_terminal_multi_partial_audio_rejects_late_chunk_atomically_and_reuses_id(self) -> None:
        cases = (
            (self.request, "failed", ProviderRequestLifecycleEventKind.FAILED),
            (self._prepare_second_request(), "cancelled", ProviderRequestLifecycleEventKind.CANCELLED),
        )
        for request, label, terminal_kind in cases:
            with self.subTest(state=label):
                self.registry.accept(request.provider_request_id, event_id=f"batch33-{label}-accepted")
                self.registry.synthesis_started(
                    request.provider_request_id,
                    event_id=f"batch33-{label}-started",
                )
                self.registry.record_audio_chunk(
                    self._chunk_for_request(
                        request,
                        event_id=f"batch33-{label}-chunk-1",
                    )
                )
                before_terminal = self.registry.record_audio_chunk(
                    self._chunk_for_request(
                        request,
                        event_id=f"batch33-{label}-chunk-2",
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                terminal = (
                    self.registry.fail(
                        request.provider_request_id,
                        event_id=f"batch33-{label}-terminal",
                        reason="multi-partial audio terminal failure",
                        reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else self.registry.cancel(
                        request.provider_request_id,
                        event_id=f"batch33-{label}-terminal",
                        reason="multi-partial audio terminal cancellation",
                    )
                )
                committed_events = self.registry.events_for(request.provider_request_id)
                committed_chunks = self.registry.audio_chunks_for(request.provider_request_id)
                before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
                reusable_id = f"batch33-{label}-late-chunk-reusable"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    self.registry.record_audio_chunk(
                        self._chunk_for_request(
                            request,
                            event_id=reusable_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )

                self.assertIs(self.registry.get(request.provider_request_id), terminal)
                self.assertIs(self.registry.events_for(request.provider_request_id), committed_events)
                self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), committed_chunks)
                self.assertEqual(terminal.events[:-1], before_terminal.events)
                for actual, expected in zip(terminal.events[:-1], before_terminal.events):
                    self.assertIs(actual, expected)
                self.assertIs(terminal.audio_chunks, before_terminal.audio_chunks)
                self.assertIs(terminal.audio_chunks[0], before_terminal.audio_chunks[0])
                self.assertIs(terminal.audio_chunks[1], before_terminal.audio_chunks[1])
                self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]
                self.assertFalse(terminal.audio_produced)
                self.assertFalse(terminal.audio_enqueued)

                reuse_request = self._prepare_batch18_request(f"batch33-reuse-{label}")
                self.registry.accept(
                    reuse_request.provider_request_id,
                    event_id=f"batch33-reuse-{label}-accepted",
                )
                self.registry.synthesis_started(
                    reuse_request.provider_request_id,
                    event_id=f"batch33-reuse-{label}-started",
                )
                reused = self.registry.record_audio_chunk(
                    self._chunk_for_request(reuse_request, event_id=reusable_id)
                )
                self.assertEqual(reused.audio_chunks[-1].event_id, reusable_id)
                self.assertIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(self.registry.get(request.provider_request_id), terminal)
                self.assertIs(self.registry.events_for(request.provider_request_id), committed_events)
                self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), committed_chunks)

    def test_completed_multi_partial_audio_rejects_late_chunk_atomically_and_reuses_id(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="batch34-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch34-started")
        self.registry.record_audio_chunk(self._chunk(event_id="batch34-chunk-1"))
        before_produced = self.registry.record_audio_chunk(
            self._chunk(event_id="batch34-chunk-2", audio_chunk_index=2, receive_sequence=2)
        )
        produced = self.registry.audio_produced(self.request.provider_request_id, event_id="batch34-produced")
        enqueued = self.registry.audio_enqueued(self.request.provider_request_id, event_id="batch34-enqueued")
        completed = self.registry.complete(self.request.provider_request_id, event_id="batch34-completed")

        committed_events = self.registry.events_for(self.request.provider_request_id)
        committed_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        reusable_id = "batch34-late-chunk-reusable"

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.record_audio_chunk(
                self._chunk(
                    event_id=reusable_id,
                    audio_chunk_index=3,
                    receive_sequence=3,
                )
            )

        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), committed_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), committed_chunks)
        self.assertEqual(completed.events[:-1], enqueued.events)
        for actual, expected in zip(completed.events[:-1], enqueued.events):
            self.assertIs(actual, expected)
        self.assertIs(completed.audio_chunks, before_produced.audio_chunks)
        self.assertIs(completed.audio_chunks, committed_chunks)
        self.assertIs(completed.audio_chunks[0], before_produced.audio_chunks[0])
        self.assertIs(completed.audio_chunks[1], before_produced.audio_chunks[1])
        self.assertTrue(completed.audio_produced)
        self.assertTrue(completed.audio_enqueued)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertNotIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]

        reuse_request = self._prepare_batch18_request("batch34-reuse")
        self.registry.accept(reuse_request.provider_request_id, event_id="batch34-reuse-accepted")
        self.registry.synthesis_started(reuse_request.provider_request_id, event_id="batch34-reuse-started")
        reused = self.registry.record_audio_chunk(
            self._chunk_for_request(reuse_request, event_id=reusable_id)
        )
        self.assertEqual(reused.audio_chunks[-1].event_id, reusable_id)
        self.assertIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), committed_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), committed_chunks)
        self.assertEqual(tuple(chunk.event_id for chunk in completed.audio_chunks), ("batch34-chunk-1", "batch34-chunk-2"))

    def test_completed_late_chunk_id_reuse_isolated_across_provider(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="batch35-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch35-started")
        first = self.registry.record_audio_chunk(self._chunk(event_id="batch35-chunk-1"))
        before_produced = self.registry.record_audio_chunk(
            self._chunk(event_id="batch35-chunk-2", audio_chunk_index=2, receive_sequence=2)
        )
        self.registry.audio_produced(self.request.provider_request_id, event_id="batch35-produced")
        enqueued = self.registry.audio_enqueued(self.request.provider_request_id, event_id="batch35-enqueued")
        completed = self.registry.complete(self.request.provider_request_id, event_id="batch35-completed")

        committed_events = self.registry.events_for(self.request.provider_request_id)
        committed_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        reusable_id = "batch35-cross-provider-late-chunk-reusable"

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.record_audio_chunk(
                self._chunk(
                    event_id=reusable_id,
                    audio_chunk_index=3,
                    receive_sequence=3,
                )
            )

        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), committed_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), committed_chunks)
        self.assertEqual(completed.events[:-1], enqueued.events)
        for actual, expected in zip(completed.events[:-1], enqueued.events):
            self.assertIs(actual, expected)
        self.assertIs(completed.audio_chunks, before_produced.audio_chunks)
        self.assertIs(completed.audio_chunks, committed_chunks)
        self.assertIs(completed.audio_chunks[0], first.audio_chunks[0])
        self.assertIs(completed.audio_chunks[1], before_produced.audio_chunks[1])
        self.assertTrue(completed.audio_produced)
        self.assertTrue(completed.audio_enqueued)
        self.assertEqual(completed.events[-1].kind, ProviderRequestLifecycleEventKind.COMPLETED)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertNotIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]

        other = self._prepare_other_provider_request()
        self.registry.accept(other.provider_request_id, event_id="batch35-other-accepted")
        self.registry.synthesis_started(other.provider_request_id, event_id="batch35-other-started")
        reused = self.registry.record_audio_chunk(
            self._chunk_for_request(other, event_id=reusable_id)
        )

        self.assertEqual(reused.audio_chunks[-1].event_id, reusable_id)
        self.assertEqual(reused.audio_chunks[-1].provider_name, "edge_tts")
        self.assertEqual(reused.audio_chunks[-1].provider_generation, 7)
        self.assertIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), committed_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), committed_chunks)
        self.assertEqual(tuple(chunk.event_id for chunk in completed.audio_chunks), ("batch35-chunk-1", "batch35-chunk-2"))
        self.assertEqual(tuple(event.kind for event in completed.events), (
            ProviderRequestLifecycleEventKind.ACCEPTED,
            ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
            ProviderRequestLifecycleEventKind.AUDIO_PRODUCED,
            ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED,
            ProviderRequestLifecycleEventKind.COMPLETED,
        ))

    def test_cross_provider_reused_late_chunk_can_finish_receiver_without_mutating_completed_source(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="batch36-source-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch36-source-started")
        source_first = self.registry.record_audio_chunk(
            self._chunk(event_id="batch36-source-chunk-1")
        )
        source_second = self.registry.record_audio_chunk(
            self._chunk(
                event_id="batch36-source-chunk-2",
                audio_chunk_index=2,
                receive_sequence=2,
            )
        )
        self.registry.audio_produced(self.request.provider_request_id, event_id="batch36-source-produced")
        source_enqueued = self.registry.audio_enqueued(
            self.request.provider_request_id,
            event_id="batch36-source-enqueued",
        )
        source_completed = self.registry.complete(
            self.request.provider_request_id,
            event_id="batch36-source-completed",
        )
        source_events = self.registry.events_for(self.request.provider_request_id)
        source_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        reusable_id = "batch36-cross-provider-late-chunk-reusable"
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.record_audio_chunk(
                self._chunk(
                    event_id=reusable_id,
                    audio_chunk_index=3,
                    receive_sequence=3,
                )
            )

        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertNotIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), source_completed)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), source_chunks)

        other = self._prepare_other_provider_request()
        self.registry.accept(other.provider_request_id, event_id="batch36-receiver-accepted")
        self.registry.synthesis_started(other.provider_request_id, event_id="batch36-receiver-started")
        receiver_first = self.registry.record_audio_chunk(
            self._chunk_for_request(other, event_id="batch36-receiver-chunk-1")
        )
        receiver_before_reuse_events = self.registry.events_for(other.provider_request_id)
        receiver_before_reuse_chunks = self.registry.audio_chunks_for(other.provider_request_id)
        receiver_second = self.registry.record_audio_chunk(
            self._chunk_for_request(
                other,
                event_id=reusable_id,
                audio_chunk_index=2,
                receive_sequence=2,
            )
        )
        receiver_produced = self.registry.audio_produced(
            other.provider_request_id,
            event_id="batch36-receiver-produced",
        )
        receiver_enqueued = self.registry.audio_enqueued(
            other.provider_request_id,
            event_id="batch36-receiver-enqueued",
        )
        receiver_completed = self.registry.complete(
            other.provider_request_id,
            event_id="batch36-receiver-completed",
        )

        self.assertIs(receiver_second.events, receiver_before_reuse_events)
        self.assertIsNot(receiver_second.audio_chunks, receiver_before_reuse_chunks)
        self.assertIs(receiver_second.audio_chunks[0], receiver_first.audio_chunks[0])
        self.assertEqual(
            tuple(chunk.event_id for chunk in receiver_second.audio_chunks),
            ("batch36-receiver-chunk-1", reusable_id),
        )
        self.assertEqual(
            tuple(chunk.audio_chunk_index for chunk in receiver_second.audio_chunks),
            (1, 2),
        )
        self.assertEqual(
            tuple(chunk.receive_sequence for chunk in receiver_second.audio_chunks),
            (1, 2),
        )
        self.assertEqual(receiver_second.audio_chunks[1].provider_name, "edge_tts")
        self.assertEqual(receiver_second.audio_chunks[1].provider_generation, 7)
        self.assertTrue(receiver_produced.audio_produced)
        self.assertTrue(receiver_enqueued.audio_enqueued)
        self.assertEqual(receiver_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertTrue(receiver_completed.audio_produced)
        self.assertTrue(receiver_completed.audio_enqueued)
        self.assertIs(receiver_completed.audio_chunks, receiver_second.audio_chunks)
        self.assertIs(receiver_completed.audio_chunks[0], receiver_first.audio_chunks[0])
        self.assertIs(receiver_completed.audio_chunks[1], receiver_second.audio_chunks[1])
        self.assertEqual(receiver_completed.events[:-1], receiver_enqueued.events)
        for actual, expected in zip(receiver_completed.events[:-1], receiver_enqueued.events):
            self.assertIs(actual, expected)
        self.assertIn(reusable_id, self.registry._event_ids)  # type: ignore[attr-defined]

        self.assertIs(self.registry.get(self.request.provider_request_id), source_completed)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
        self.assertIs(source_completed.audio_chunks, source_second.audio_chunks)
        self.assertIs(source_completed.audio_chunks[0], source_first.audio_chunks[0])
        self.assertIs(source_completed.audio_chunks[1], source_second.audio_chunks[1])
        self.assertEqual(
            tuple(event.event_id for event in source_completed.events),
            (
                "batch36-source-accepted",
                "batch36-source-started",
                "batch36-source-produced",
                "batch36-source-enqueued",
                "batch36-source-completed",
            ),
        )

    def test_cross_provider_reused_late_chunk_receiver_terminal_projection_isolated(self) -> None:
        cases = (
            (
                "failed",
                ProviderRequestLifecycleEventKind.FAILED,
                False,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleEventKind.CANCELLED,
                True,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
            ),
        )
        for label, terminal_kind, enqueue_before_terminal, reason_code in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.accept(self.request.provider_request_id, event_id=f"batch37-{label}-source-accepted")
                registry.synthesis_started(
                    self.request.provider_request_id,
                    event_id=f"batch37-{label}-source-started",
                )
                source_first = registry.record_audio_chunk(
                    self._chunk(event_id=f"batch37-{label}-source-chunk-1")
                )
                source_second = registry.record_audio_chunk(
                    self._chunk(
                        event_id=f"batch37-{label}-source-chunk-2",
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                registry.audio_produced(
                    self.request.provider_request_id,
                    event_id=f"batch37-{label}-source-produced",
                )
                source_enqueued = registry.audio_enqueued(
                    self.request.provider_request_id,
                    event_id=f"batch37-{label}-source-enqueued",
                )
                source_completed = registry.complete(
                    self.request.provider_request_id,
                    event_id=f"batch37-{label}-source-completed",
                )
                source_events = registry.events_for(self.request.provider_request_id)
                source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
                source_event_ids = tuple(event.event_id for event in source_completed.events)
                source_chunk_ids = tuple(chunk.event_id for chunk in source_completed.audio_chunks)
                reusable_id = f"batch37-{label}-cross-provider-reusable"
                before_ids = set(registry._event_ids)  # type: ignore[attr-defined]

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(
                        self._chunk(
                            event_id=reusable_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )

                self.assertEqual(registry._event_ids, before_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(self.request.provider_request_id), source_completed)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)

                other = self._prepare_other_provider_request(suffix=label)
                registry.accept(other.provider_request_id, event_id=f"batch37-{label}-receiver-accepted")
                registry.synthesis_started(
                    other.provider_request_id,
                    event_id=f"batch37-{label}-receiver-started",
                )
                receiver_first = registry.record_audio_chunk(
                    self._chunk_for_request(other, event_id=f"batch37-{label}-receiver-chunk-1")
                )
                receiver_before_reuse_events = registry.events_for(other.provider_request_id)
                receiver_before_reuse_chunks = registry.audio_chunks_for(other.provider_request_id)
                receiver_second = registry.record_audio_chunk(
                    self._chunk_for_request(
                        other,
                        event_id=reusable_id,
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                receiver_produced = registry.audio_produced(
                    other.provider_request_id,
                    event_id=f"batch37-{label}-receiver-produced",
                )
                receiver_enqueued = receiver_produced
                if enqueue_before_terminal:
                    receiver_enqueued = registry.audio_enqueued(
                        other.provider_request_id,
                        event_id=f"batch37-{label}-receiver-enqueued",
                    )
                receiver_terminal = (
                    registry.fail(
                        other.provider_request_id,
                        event_id=f"batch37-{label}-receiver-terminal",
                        reason=f"receiver {label} after cross-provider chunk reuse",
                        reason_code=reason_code,
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else registry.cancel(
                        other.provider_request_id,
                        event_id=f"batch37-{label}-receiver-terminal",
                        reason=f"receiver {label} after cross-provider chunk reuse",
                        reason_code=reason_code,
                    )
                )

                self.assertIs(receiver_second.events, receiver_before_reuse_events)
                self.assertIsNot(receiver_second.audio_chunks, receiver_before_reuse_chunks)
                self.assertIs(receiver_second.audio_chunks[0], receiver_first.audio_chunks[0])
                self.assertEqual(
                    tuple(chunk.event_id for chunk in receiver_second.audio_chunks),
                    (f"batch37-{label}-receiver-chunk-1", reusable_id),
                )
                self.assertEqual(
                    tuple(chunk.audio_chunk_index for chunk in receiver_second.audio_chunks),
                    (1, 2),
                )
                self.assertEqual(
                    tuple(chunk.receive_sequence for chunk in receiver_second.audio_chunks),
                    (1, 2),
                )
                self.assertEqual(receiver_second.audio_chunks[1].provider_name, "edge_tts")
                self.assertEqual(receiver_second.audio_chunks[1].provider_generation, 7)
                self.assertTrue(receiver_produced.audio_produced)
                self.assertEqual(receiver_enqueued.audio_enqueued, enqueue_before_terminal)
                self.assertEqual(receiver_terminal.state, (
                    ProviderRequestLifecycleState.FAILED
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else ProviderRequestLifecycleState.CANCELLED
                ))
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.audio_chunks, receiver_second.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_first.audio_chunks[0])
                self.assertIs(receiver_terminal.audio_chunks[1], receiver_second.audio_chunks[1])
                self.assertEqual(receiver_terminal.events[:-1], receiver_enqueued.events)
                for actual, expected in zip(receiver_terminal.events[:-1], receiver_enqueued.events):
                    self.assertIs(actual, expected)
                self.assertIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]

                self.assertIs(registry.get(self.request.provider_request_id), source_completed)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
                self.assertIs(source_completed.audio_chunks, source_second.audio_chunks)
                self.assertIs(source_completed.audio_chunks[0], source_first.audio_chunks[0])
                self.assertIs(source_completed.audio_chunks[1], source_second.audio_chunks[1])
                self.assertEqual(
                    tuple(event.event_id for event in source_completed.events),
                    source_event_ids,
                )
                self.assertEqual(
                    tuple(chunk.event_id for chunk in source_completed.audio_chunks),
                    source_chunk_ids,
                )
                self.assertEqual(
                    tuple(event.kind for event in source_enqueued.events),
                    (
                        ProviderRequestLifecycleEventKind.ACCEPTED,
                        ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
                        ProviderRequestLifecycleEventKind.AUDIO_PRODUCED,
                        ProviderRequestLifecycleEventKind.AUDIO_ENQUEUED,
                    ),
                )

    def test_cross_provider_reused_receiver_terminal_rejects_late_chunk_atomically_and_reuses_id(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleEventKind.COMPLETED,
                True,
                None,
            ),
            (
                "failed",
                ProviderRequestLifecycleEventKind.FAILED,
                False,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleEventKind.CANCELLED,
                True,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
            ),
        )
        for label, terminal_kind, enqueue_before_terminal, reason_code in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.accept(self.request.provider_request_id, event_id=f"batch38-{label}-source-accepted")
                registry.synthesis_started(
                    self.request.provider_request_id,
                    event_id=f"batch38-{label}-source-started",
                )
                source_first = registry.record_audio_chunk(
                    self._chunk(event_id=f"batch38-{label}-source-chunk-1")
                )
                source_second = registry.record_audio_chunk(
                    self._chunk(
                        event_id=f"batch38-{label}-source-chunk-2",
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                registry.audio_produced(
                    self.request.provider_request_id,
                    event_id=f"batch38-{label}-source-produced",
                )
                source_enqueued = registry.audio_enqueued(
                    self.request.provider_request_id,
                    event_id=f"batch38-{label}-source-enqueued",
                )
                source_completed = registry.complete(
                    self.request.provider_request_id,
                    event_id=f"batch38-{label}-source-completed",
                )
                source_events = registry.events_for(self.request.provider_request_id)
                source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
                source_event_ids = tuple(event.event_id for event in source_completed.events)
                source_chunk_ids = tuple(chunk.event_id for chunk in source_completed.audio_chunks)
                source_reusable_id = f"batch38-{label}-source-late-reusable"
                before_source_rejection_ids = set(registry._event_ids)  # type: ignore[attr-defined]

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(
                        self._chunk(
                            event_id=source_reusable_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )

                self.assertEqual(
                    registry._event_ids,  # type: ignore[attr-defined]
                    before_source_rejection_ids,
                )
                self.assertNotIn(source_reusable_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(suffix=f"batch38-{label}")
                registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch38-{label}-receiver-accepted",
                )
                registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch38-{label}-receiver-started",
                )
                receiver_first = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=f"batch38-{label}-receiver-chunk-1",
                    )
                )
                receiver_second = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=source_reusable_id,
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                receiver_produced = registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch38-{label}-receiver-produced",
                )
                receiver_before_terminal = (
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch38-{label}-receiver-enqueued",
                    )
                    if enqueue_before_terminal
                    else receiver_produced
                )
                if terminal_kind is ProviderRequestLifecycleEventKind.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch38-{label}-receiver-terminal",
                    )
                elif terminal_kind is ProviderRequestLifecycleEventKind.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch38-{label}-receiver-terminal",
                        reason="receiver failed after cross-provider chunk reuse",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch38-{label}-receiver-terminal",
                        reason="receiver cancelled after cross-provider chunk reuse",
                        reason_code=reason_code,
                    )

                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
                receiver_event_ids = tuple(event.event_id for event in receiver_terminal.events)
                receiver_chunk_ids = tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks)
                receiver_late_reusable_id = f"batch38-{label}-receiver-late-reusable"
                before_receiver_rejection_ids = set(registry._event_ids)  # type: ignore[attr-defined]

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=receiver_late_reusable_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )

                self.assertEqual(
                    registry._event_ids,  # type: ignore[attr-defined]
                    before_receiver_rejection_ids,
                )
                self.assertNotIn(receiver_late_reusable_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)
                self.assertEqual(
                    tuple(event.event_id for event in receiver_terminal.events),
                    receiver_event_ids,
                )
                self.assertEqual(
                    tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks),
                    receiver_chunk_ids,
                )
                self.assertEqual(
                    receiver_chunk_ids,
                    (f"batch38-{label}-receiver-chunk-1", source_reusable_id),
                )
                self.assertEqual(
                    tuple(chunk.audio_chunk_index for chunk in receiver_terminal.audio_chunks),
                    (1, 2),
                )
                self.assertEqual(
                    tuple(chunk.receive_sequence for chunk in receiver_terminal.audio_chunks),
                    (1, 2),
                )
                self.assertIs(receiver_terminal.audio_chunks, receiver_second.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_first.audio_chunks[0])
                self.assertIs(receiver_terminal.audio_chunks[1], receiver_second.audio_chunks[1])
                self.assertEqual(receiver_terminal.events[:-1], receiver_before_terminal.events)
                for actual, expected in zip(receiver_terminal.events[:-1], receiver_before_terminal.events):
                    self.assertIs(actual, expected)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)

                reuse_request = self._prepare_batch18_request(f"batch38-reuse-{label}")
                registry.accept(
                    reuse_request.provider_request_id,
                    event_id=f"batch38-{label}-reuse-accepted",
                )
                registry.synthesis_started(
                    reuse_request.provider_request_id,
                    event_id=f"batch38-{label}-reuse-started",
                )
                reuse_before_chunk_events = registry.events_for(reuse_request.provider_request_id)
                reuse_before_chunk_projection = registry.audio_chunks_for(reuse_request.provider_request_id)
                reused = registry.record_audio_chunk(
                    self._chunk_for_request(
                        reuse_request,
                        event_id=receiver_late_reusable_id,
                    )
                )

                self.assertIs(reused.events, reuse_before_chunk_events)
                self.assertIsNot(reused.audio_chunks, reuse_before_chunk_projection)
                self.assertEqual(len(reused.audio_chunks), 1)
                self.assertEqual(reused.audio_chunks[0].event_id, receiver_late_reusable_id)
                self.assertEqual(reused.audio_chunks[0].provider_name, "voicestudio")
                self.assertEqual(reused.audio_chunks[0].provider_generation, 3)
                self.assertIn(receiver_late_reusable_id, registry._event_ids)  # type: ignore[attr-defined]

                self.assertIs(registry.get(self.request.provider_request_id), source_completed)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
                self.assertIs(source_completed.audio_chunks, source_second.audio_chunks)
                self.assertIs(source_completed.audio_chunks[0], source_first.audio_chunks[0])
                self.assertIs(source_completed.audio_chunks[1], source_second.audio_chunks[1])
                self.assertEqual(
                    tuple(event.event_id for event in source_completed.events),
                    source_event_ids,
                )
                self.assertEqual(
                    tuple(chunk.event_id for chunk in source_completed.audio_chunks),
                    source_chunk_ids,
                )
                self.assertEqual(source_completed.events[:-1], source_enqueued.events)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)
                self.assertEqual(
                    tuple(event.event_id for event in receiver_terminal.events),
                    receiver_event_ids,
                )
                self.assertEqual(
                    tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks),
                    receiver_chunk_ids,
                )
    def test_cross_provider_reused_receiver_terminal_late_id_survives_generation_fences(self) -> None:
        cases = (
            ("completed", ProviderRequestLifecycleEventKind.COMPLETED, True, None),
            ("failed", ProviderRequestLifecycleEventKind.FAILED, False, ProviderRequestLifecycleReasonCode.PROVIDER_ERROR),
            ("cancelled", ProviderRequestLifecycleEventKind.CANCELLED, True, ProviderRequestLifecycleReasonCode.USER_CANCELLED),
        )
        for label, terminal_kind, enqueue_before_terminal, reason_code in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 3)
                registry.activate_generation("edge_tts", 7)

                registry.accept(self.request.provider_request_id, event_id=f"batch39-{label}-source-accepted")
                registry.synthesis_started(self.request.provider_request_id, event_id=f"batch39-{label}-source-started")
                source_first = registry.record_audio_chunk(self._chunk(event_id=f"batch39-{label}-source-chunk-1"))
                source_second = registry.record_audio_chunk(
                    self._chunk(event_id=f"batch39-{label}-source-chunk-2", audio_chunk_index=2, receive_sequence=2)
                )
                registry.audio_produced(self.request.provider_request_id, event_id=f"batch39-{label}-source-produced")
                source_enqueued = registry.audio_enqueued(self.request.provider_request_id, event_id=f"batch39-{label}-source-enqueued")
                source_terminal = registry.complete(self.request.provider_request_id, event_id=f"batch39-{label}-source-completed")
                source_events = registry.events_for(self.request.provider_request_id)
                source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
                source_event_ids = tuple(event.event_id for event in source_terminal.events)
                source_chunk_ids = tuple(chunk.event_id for chunk in source_terminal.audio_chunks)
                source_late_id = f"batch39-{label}-source-late-reused"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(self._chunk(event_id=source_late_id, audio_chunk_index=3, receive_sequence=3))
                self.assertNotIn(source_late_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(self.request.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)

                receiver = self._prepare_other_provider_request(suffix=f"batch39-{label}")
                registry.accept(receiver.provider_request_id, event_id=f"batch39-{label}-receiver-accepted")
                registry.synthesis_started(receiver.provider_request_id, event_id=f"batch39-{label}-receiver-started")
                receiver_first = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=f"batch39-{label}-receiver-chunk-1")
                )
                receiver_second = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=source_late_id, audio_chunk_index=2, receive_sequence=2)
                )
                registry.audio_produced(receiver.provider_request_id, event_id=f"batch39-{label}-receiver-produced")
                if enqueue_before_terminal:
                    registry.audio_enqueued(receiver.provider_request_id, event_id=f"batch39-{label}-receiver-enqueued")
                receiver_terminal = (
                    registry.complete(receiver.provider_request_id, event_id=f"batch39-{label}-receiver-terminal")
                    if terminal_kind is ProviderRequestLifecycleEventKind.COMPLETED
                    else registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch39-{label}-receiver-terminal",
                        reason=f"receiver {label} before generation transition",
                        reason_code=reason_code,
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch39-{label}-receiver-terminal",
                        reason=f"receiver {label} before generation transition",
                        reason_code=reason_code,
                    )
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
                receiver_event_ids = tuple(event.event_id for event in receiver_terminal.events)
                receiver_chunk_ids = tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks)
                receiver_late_id = f"batch39-{label}-receiver-late-reused"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(receiver, event_id=receiver_late_id, audio_chunk_index=3, receive_sequence=3)
                    )
                self.assertNotIn(receiver_late_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 8)
                registry.advance_generation("voicestudio", 4)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(receiver, event_id=receiver_late_id, audio_chunk_index=3, receive_sequence=3)
                    )
                self.assertNotIn(receiver_late_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertEqual(registry.active_generation("edge_tts"), 8)
                self.assertEqual(registry.active_generation("voicestudio"), 4)

                future = self._prepare_generation_four_request(
                    provider_request_id=f"provider-request-batch39-future-{label}",
                    reply_id=f"reply-batch39-future-{label}",
                    segment_id=f"segment-batch39-future-{label}",
                    request_sequence=1,
                )
                registry.accept(future.provider_request_id, event_id=f"batch39-{label}-future-accepted")
                registry.synthesis_started(future.provider_request_id, event_id=f"batch39-{label}-future-started")
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                reused = registry.record_audio_chunk(self._chunk_for_request(future, event_id=receiver_late_id))

                self.assertIs(reused.events, future_events)
                self.assertIsNot(reused.audio_chunks, future_chunks)
                self.assertEqual(tuple(chunk.event_id for chunk in reused.audio_chunks), (receiver_late_id,))
                self.assertEqual(reused.audio_chunks[0].provider_name, "voicestudio")
                self.assertEqual(reused.audio_chunks[0].provider_generation, 4)
                self.assertIn(receiver_late_id, registry._event_ids)  # type: ignore[attr-defined]

                self.assertIs(registry.get(self.request.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
                self.assertIs(source_terminal.audio_chunks, source_second.audio_chunks)
                self.assertIs(source_terminal.audio_chunks[0], source_first.audio_chunks[0])
                self.assertIs(source_terminal.audio_chunks[1], source_second.audio_chunks[1])
                self.assertEqual(tuple(event.event_id for event in source_terminal.events), source_event_ids)
                self.assertEqual(tuple(chunk.event_id for chunk in source_terminal.audio_chunks), source_chunk_ids)
                self.assertEqual(source_terminal.events[:-1], source_enqueued.events)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)
                self.assertEqual(tuple(event.event_id for event in receiver_terminal.events), receiver_event_ids)
                self.assertEqual(tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks), receiver_chunk_ids)
                self.assertIs(receiver_terminal.audio_chunks, receiver_second.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_first.audio_chunks[0])
                self.assertIs(receiver_terminal.audio_chunks[1], receiver_second.audio_chunks[1])
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
    def test_cross_provider_reused_receiver_terminal_late_id_can_become_future_lifecycle_event(self) -> None:
        cases = (
            ("completed", ProviderRequestLifecycleEventKind.COMPLETED, True, None),
            ("failed", ProviderRequestLifecycleEventKind.FAILED, False, ProviderRequestLifecycleReasonCode.PROVIDER_ERROR),
            ("cancelled", ProviderRequestLifecycleEventKind.CANCELLED, True, ProviderRequestLifecycleReasonCode.USER_CANCELLED),
        )
        for label, terminal_kind, enqueue_before_terminal, reason_code in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 3)
                registry.activate_generation("edge_tts", 7)

                registry.accept(self.request.provider_request_id, event_id=f"batch40-{label}-source-accepted")
                registry.synthesis_started(self.request.provider_request_id, event_id=f"batch40-{label}-source-started")
                source_first = registry.record_audio_chunk(self._chunk(event_id=f"batch40-{label}-source-chunk-1"))
                source_second = registry.record_audio_chunk(
                    self._chunk(
                        event_id=f"batch40-{label}-source-chunk-2",
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                registry.audio_produced(self.request.provider_request_id, event_id=f"batch40-{label}-source-produced")
                source_enqueued = registry.audio_enqueued(
                    self.request.provider_request_id,
                    event_id=f"batch40-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    self.request.provider_request_id,
                    event_id=f"batch40-{label}-source-completed",
                )
                source_events = registry.events_for(self.request.provider_request_id)
                source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
                source_event_ids = tuple(event.event_id for event in source_terminal.events)
                source_chunk_ids = tuple(chunk.event_id for chunk in source_terminal.audio_chunks)
                source_late_id = f"batch40-{label}-source-late-reused"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(
                        self._chunk(
                            event_id=source_late_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )
                self.assertNotIn(source_late_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(suffix=f"batch40-{label}")
                registry.accept(receiver.provider_request_id, event_id=f"batch40-{label}-receiver-accepted")
                registry.synthesis_started(receiver.provider_request_id, event_id=f"batch40-{label}-receiver-started")
                receiver_first = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=f"batch40-{label}-receiver-chunk-1")
                )
                receiver_second = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=source_late_id,
                        audio_chunk_index=2,
                        receive_sequence=2,
                    )
                )
                registry.audio_produced(receiver.provider_request_id, event_id=f"batch40-{label}-receiver-produced")
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch40-{label}-receiver-enqueued",
                    )
                receiver_terminal = (
                    registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch40-{label}-receiver-terminal",
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.COMPLETED
                    else registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch40-{label}-receiver-terminal",
                        reason=f"receiver {label} before future event reuse",
                        reason_code=reason_code,
                    )
                    if terminal_kind is ProviderRequestLifecycleEventKind.FAILED
                    else registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch40-{label}-receiver-terminal",
                        reason=f"receiver {label} before future event reuse",
                        reason_code=reason_code,
                    )
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
                receiver_event_ids = tuple(event.event_id for event in receiver_terminal.events)
                receiver_chunk_ids = tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks)
                receiver_late_id = f"batch40-{label}-receiver-late-reused-as-event"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=receiver_late_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )
                self.assertNotIn(receiver_late_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)

                registry.advance_generation("edge_tts", 8)
                registry.advance_generation("voicestudio", 4)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=receiver_late_id,
                            audio_chunk_index=3,
                            receive_sequence=3,
                        )
                    )
                self.assertNotIn(receiver_late_id, registry._event_ids)  # type: ignore[attr-defined]

                future = self._prepare_generation_four_request(
                    provider_request_id=f"provider-request-batch40-future-{label}",
                    reply_id=f"reply-batch40-future-{label}",
                    segment_id=f"segment-batch40-future-{label}",
                    request_sequence=1,
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=receiver_late_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)

                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(tuple(event.event_id for event in future_accepted.events), (receiver_late_id,))
                self.assertEqual(future_accepted.events[0].kind, ProviderRequestLifecycleEventKind.ACCEPTED)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 4)
                self.assertIs(future_accepted.events, future_events)
                self.assertIs(future_accepted.audio_chunks, future_chunks)
                self.assertEqual(future_accepted.audio_chunks, ())
                self.assertIn(receiver_late_id, registry._event_ids)  # type: ignore[attr-defined]

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            future,
                            event_id=receiver_late_id,
                        )
                    )
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                self.assertIs(registry.get(self.request.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
                self.assertIs(source_terminal.audio_chunks, source_second.audio_chunks)
                self.assertIs(source_terminal.audio_chunks[0], source_first.audio_chunks[0])
                self.assertIs(source_terminal.audio_chunks[1], source_second.audio_chunks[1])
                self.assertEqual(tuple(event.event_id for event in source_terminal.events), source_event_ids)
                self.assertEqual(tuple(chunk.event_id for chunk in source_terminal.audio_chunks), source_chunk_ids)
                self.assertEqual(source_terminal.events[:-1], source_enqueued.events)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)
                self.assertEqual(tuple(event.event_id for event in receiver_terminal.events), receiver_event_ids)
                self.assertEqual(tuple(chunk.event_id for chunk in receiver_terminal.audio_chunks), receiver_chunk_ids)
                self.assertIs(receiver_terminal.audio_chunks, receiver_second.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_first.audio_chunks[0])
                self.assertIs(receiver_terminal.audio_chunks[1], receiver_second.audio_chunks[1])
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertEqual(registry.active_generation("edge_tts"), 8)
                self.assertEqual(registry.active_generation("voicestudio"), 4)

    def test_cross_provider_reused_rejected_lifecycle_id_can_become_future_audio_chunk(self) -> None:
        registry = ProviderRequestLifecycleRegistry(self.requests)
        registry.activate_generation("voicestudio", 3)
        registry.activate_generation("edge_tts", 7)

        registry.accept(self.request.provider_request_id, event_id="batch41-source-accepted")
        source_before_rejection = registry.get(self.request.provider_request_id)
        source_events = registry.events_for(self.request.provider_request_id)
        source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
        reusable_id = "batch41-rejected-lifecycle-id-reusable"
        invalid_event = ProviderRequestLifecycleEvent(
            event_id=reusable_id,
            event_sequence=2,
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
        )

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            registry.record(invalid_event)

        self.assertNotIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
        self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
        self.assertEqual(source_before_rejection.events, (source_before_rejection.events[0],))
        self.assertEqual(source_before_rejection.audio_chunks, ())

        receiver = self._prepare_other_provider_request(suffix="batch41-receiver")
        registry.accept(receiver.provider_request_id, event_id="batch41-receiver-accepted")
        registry.synthesis_started(receiver.provider_request_id, event_id="batch41-receiver-started")
        receiver_before_chunk = registry.get(receiver.provider_request_id)
        receiver_events = registry.events_for(receiver.provider_request_id)
        receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
        receiver_with_chunk = registry.record_audio_chunk(
            self._chunk_for_request(receiver, event_id=reusable_id)
        )

        self.assertIs(receiver_with_chunk.events, receiver_events)
        self.assertIsNot(receiver_with_chunk.audio_chunks, receiver_chunks)
        self.assertEqual(tuple(chunk.event_id for chunk in receiver_with_chunk.audio_chunks), (reusable_id,))
        self.assertEqual(receiver_with_chunk.audio_chunks[0].provider_name, "edge_tts")
        self.assertEqual(receiver_with_chunk.audio_chunks[0].provider_generation, 7)
        self.assertIs(receiver_with_chunk.events, receiver_before_chunk.events)
        self.assertIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]

        future = self._prepare_second_request()
        future_before_duplicate = registry.get(future.provider_request_id)
        future_events = registry.events_for(future.provider_request_id)
        future_chunks = registry.audio_chunks_for(future.provider_request_id)
        committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            registry.accept(future.provider_request_id, event_id=reusable_id)

        self.assertIs(registry.get(future.provider_request_id), future_before_duplicate)
        self.assertIs(registry.events_for(future.provider_request_id), future_events)
        self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
        self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
        self.assertEqual(future_before_duplicate.events, ())
        self.assertEqual(future_before_duplicate.audio_chunks, ())
        self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
        self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
        self.assertIs(registry.get(receiver.provider_request_id), receiver_with_chunk)
        self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
        self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_with_chunk.audio_chunks)

    def test_cross_provider_reused_rejected_lifecycle_id_survives_generation_fences(self) -> None:
        registry = ProviderRequestLifecycleRegistry(self.requests)
        registry.activate_generation("voicestudio", 3)
        registry.activate_generation("edge_tts", 7)

        registry.accept(self.request.provider_request_id, event_id="batch42-source-accepted")
        source_before_rejection = registry.get(self.request.provider_request_id)
        source_events = registry.events_for(self.request.provider_request_id)
        source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
        reusable_id = "batch42-rejected-lifecycle-id-reusable"
        invalid_event = ProviderRequestLifecycleEvent(
            event_id=reusable_id,
            event_sequence=2,
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
        )

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            registry.record(invalid_event)

        self.assertNotIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
        self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)

        stale_event = replace(
            invalid_event,
            reply_id=self.request.reply_id,
        )
        registry.advance_generation("edge_tts", 8)
        registry.advance_generation("voicestudio", 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            registry.record(stale_event)

        self.assertNotIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
        self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)

        future_receiver = self._prepare_other_provider_request(
            suffix="batch42-future-receiver",
            provider_generation=8,
        )
        registry.accept(future_receiver.provider_request_id, event_id="batch42-future-accepted")
        registry.synthesis_started(future_receiver.provider_request_id, event_id="batch42-future-started")
        future_before_chunk = registry.get(future_receiver.provider_request_id)
        future_events = registry.events_for(future_receiver.provider_request_id)
        future_chunks = registry.audio_chunks_for(future_receiver.provider_request_id)
        future_with_chunk = registry.record_audio_chunk(
            self._chunk_for_request(future_receiver, event_id=reusable_id)
        )

        self.assertIs(future_with_chunk.events, future_events)
        self.assertIsNot(future_with_chunk.audio_chunks, future_chunks)
        self.assertEqual(tuple(chunk.event_id for chunk in future_with_chunk.audio_chunks), (reusable_id,))
        self.assertEqual(future_with_chunk.audio_chunks[0].provider_name, "edge_tts")
        self.assertEqual(future_with_chunk.audio_chunks[0].provider_generation, 8)
        self.assertIs(future_with_chunk.events, future_before_chunk.events)
        self.assertIn(reusable_id, registry._event_ids)  # type: ignore[attr-defined]

        duplicate_request = self._prepare_second_request()
        duplicate_before = registry.get(duplicate_request.provider_request_id)
        duplicate_events = registry.events_for(duplicate_request.provider_request_id)
        duplicate_chunks = registry.audio_chunks_for(duplicate_request.provider_request_id)
        committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            registry.accept(duplicate_request.provider_request_id, event_id=reusable_id)

        self.assertIs(registry.get(duplicate_request.provider_request_id), duplicate_before)
        self.assertIs(registry.events_for(duplicate_request.provider_request_id), duplicate_events)
        self.assertIs(registry.audio_chunks_for(duplicate_request.provider_request_id), duplicate_chunks)
        self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
        self.assertEqual(duplicate_before.events, ())
        self.assertEqual(duplicate_before.audio_chunks, ())
        self.assertEqual(registry.active_generation("edge_tts"), 8)
        self.assertEqual(registry.active_generation("voicestudio"), 4)
        self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
        self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
        self.assertIs(registry.get(future_receiver.provider_request_id), future_with_chunk)
        self.assertIs(registry.events_for(future_receiver.provider_request_id), future_events)
        self.assertIs(registry.audio_chunks_for(future_receiver.provider_request_id), future_with_chunk.audio_chunks)
    def test_cross_provider_reused_lifecycle_chunk_terminal_fence_and_reverse_reuse_isolated(self) -> None:
        registry = ProviderRequestLifecycleRegistry(self.requests)
        registry.activate_generation("voicestudio", 3)
        registry.activate_generation("edge_tts", 7)

        registry.accept(self.request.provider_request_id, event_id="batch43-source-accepted")
        source_before_rejection = registry.get(self.request.provider_request_id)
        source_events = registry.events_for(self.request.provider_request_id)
        source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
        reused_chunk_id = "batch43-rejected-lifecycle-id-reused-as-chunk"
        invalid_event = ProviderRequestLifecycleEvent(
            event_id=reused_chunk_id,
            event_sequence=2,
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            registry.record(invalid_event)

        registry.advance_generation("edge_tts", 8)
        registry.advance_generation("voicestudio", 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            registry.record(replace(invalid_event, reply_id=self.request.reply_id))

        receiver = self._prepare_other_provider_request(
            suffix="batch43-receiver",
            provider_generation=8,
        )
        registry.accept(receiver.provider_request_id, event_id="batch43-receiver-accepted")
        registry.synthesis_started(receiver.provider_request_id, event_id="batch43-receiver-started")
        receiver_with_chunk = registry.record_audio_chunk(
            self._chunk_for_request(receiver, event_id=reused_chunk_id)
        )
        registry.audio_produced(receiver.provider_request_id, event_id="batch43-receiver-produced")
        registry.audio_enqueued(receiver.provider_request_id, event_id="batch43-receiver-enqueued")
        receiver_terminal = registry.complete(
            receiver.provider_request_id,
            event_id="batch43-receiver-completed",
        )
        receiver_events = registry.events_for(receiver.provider_request_id)
        receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

        self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
        self.assertIs(receiver_terminal.events, receiver_events)
        self.assertEqual(receiver_terminal.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertTrue(receiver_terminal.audio_produced)
        self.assertTrue(receiver_terminal.audio_enqueued)
        self.assertEqual(tuple(chunk.event_id for chunk in receiver_chunks), (reused_chunk_id,))
        self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
        self.assertEqual(receiver_chunks[0].provider_generation, 8)

        late_reusable_id = "batch43-terminal-late-chunk-reusable-as-lifecycle"
        late_chunk = self._chunk_for_request(
            receiver,
            event_id=late_reusable_id,
            audio_chunk_index=2,
            receive_sequence=2,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            registry.record_audio_chunk(late_chunk)

        self.assertNotIn(late_reusable_id, registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
        self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
        self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

        registry.advance_generation("edge_tts", 9)
        registry.advance_generation("voicestudio", 5)
        committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            registry.record_audio_chunk(
                self._chunk_for_request(
                    receiver,
                    event_id=reused_chunk_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            registry.record_audio_chunk(late_chunk)

        self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
        self.assertNotIn(late_reusable_id, registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
        self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
        self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

        future = self.requests.prepare(
            provider_request_id="provider-request-batch43-future",
            reply_id="reply-1",
            segment_id="segment-batch43-future",
            request_sequence=2,
            text="第五代 Provider 句段。",
            audio_format=self.audio_format,
            provider_generation=5,
            session_id="session-1",
        )
        future_accepted = registry.accept(future.provider_request_id, event_id=late_reusable_id)
        future_events = registry.events_for(future.provider_request_id)
        future_chunks = registry.audio_chunks_for(future.provider_request_id)
        committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            registry.synthesis_started(future.provider_request_id, event_id=reused_chunk_id)

        self.assertIs(registry.get(future.provider_request_id), future_accepted)
        self.assertIs(registry.events_for(future.provider_request_id), future_events)
        self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
        self.assertEqual(tuple(event.event_id for event in future_events), (late_reusable_id,))
        self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertFalse(future_accepted.audio_produced)
        self.assertFalse(future_accepted.audio_enqueued)
        self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
        self.assertEqual(registry.active_generation("edge_tts"), 9)
        self.assertEqual(registry.active_generation("voicestudio"), 5)
        self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
        self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
        self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
        self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
        self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
        self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_cross_provider_reused_lifecycle_chunk_noncompleted_terminal_matrix_isolated(self) -> None:
        cases = (
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 3)
                registry.activate_generation("edge_tts", 7)

                registry.accept(
                    self.request.provider_request_id,
                    event_id=f"batch44-{label}-source-accepted",
                )
                source_before_rejection = registry.get(self.request.provider_request_id)
                source_events = registry.events_for(self.request.provider_request_id)
                source_chunks = registry.audio_chunks_for(self.request.provider_request_id)
                reused_chunk_id = f"batch44-{label}-rejected-lifecycle-id-reused-as-chunk"
                invalid_event = ProviderRequestLifecycleEvent(
                    event_id=reused_chunk_id,
                    event_sequence=2,
                    provider_request_id=self.request.provider_request_id,
                    reply_id=f"reply-batch44-{label}-other",
                    segment_id=self.request.segment_id,
                    provider_name=self.request.provider_name,
                    provider_generation=self.request.provider_generation,
                    voice_profile_id=self.request.voice_profile_id,
                    voice_profile_version=self.request.voice_profile_version,
                    audio_format=self.request.audio_format,
                    capability_check=self.request.capability_check,
                    kind=ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
                    registry.record(invalid_event)
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                registry.advance_generation("edge_tts", 8)
                registry.advance_generation("voicestudio", 4)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record(replace(invalid_event, reply_id=self.request.reply_id))
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch44-{label}-receiver",
                    provider_generation=8,
                )
                registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch44-{label}-receiver-accepted",
                )
                registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch44-{label}-receiver-started",
                )
                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch44-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch44-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch44-{label}-receiver-terminal",
                        reason="provider failed",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch44-{label}-receiver-terminal",
                        reason="user cancelled",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_with_chunk.audio_chunks[0])
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertEqual(tuple(chunk.event_id for chunk in receiver_chunks), (reused_chunk_id,))
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 8)

                late_reusable_id = f"batch44-{label}-terminal-late-chunk-reusable-as-lifecycle"
                late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=late_reusable_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(late_chunk)

                self.assertNotIn(late_reusable_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 9)
                registry.advance_generation("voicestudio", 5)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=reused_chunk_id,
                            audio_chunk_index=2,
                            receive_sequence=2,
                        )
                    )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(late_chunk)

                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(late_reusable_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch44-{label}-future",
                    reply_id=f"reply-batch44-{label}-future",
                    segment_id=f"segment-batch44-{label}-future",
                    request_sequence=1,
                    text="第五代 Provider 句段。",
                    audio_format=self.audio_format,
                    provider_generation=5,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=late_reusable_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )

                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(tuple(event.event_id for event in future_events), (late_reusable_id,))
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertFalse(future_accepted.audio_produced)
                self.assertFalse(future_accepted.audio_enqueued)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertEqual(registry.active_generation("edge_tts"), 9)
                self.assertEqual(registry.active_generation("voicestudio"), 5)
                self.assertIs(registry.get(self.request.provider_request_id), source_before_rejection)
                self.assertIs(registry.events_for(self.request.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(self.request.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)
    def test_future_terminal_lifecycle_id_survives_fences_and_becomes_cross_provider_chunk(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("edge_tts", 8)
                registry.activate_generation("voicestudio", 5)

                source = self._prepare_other_provider_request(
                    suffix=f"batch45-{label}-source",
                    provider_generation=8,
                )
                registry.accept(
                    source.provider_request_id,
                    event_id=f"batch45-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch45-{label}-source-started",
                )
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        source,
                        event_id=f"batch45-{label}-source-chunk",
                    )
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch45-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch45-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch45-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_accepted_id = f"batch45-{label}-source-late-chunk-reused-as-accepted"
                source_late_chunk = self._chunk_for_request(
                    source,
                    event_id=reused_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(source_late_chunk)

                self.assertNotIn(reused_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("edge_tts", 9)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(source_late_chunk)
                self.assertNotIn(reused_accepted_id, registry._event_ids)  # type: ignore[attr-defined]

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch45-{label}-future",
                    reply_id=f"reply-batch45-{label}-future",
                    segment_id=f"segment-batch45-{label}-future",
                    request_sequence=1,
                    text="第五代 Provider 终态句段。",
                    audio_format=self.audio_format,
                    provider_generation=5,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reused_accepted_id,
                )
                registry.synthesis_started(
                    future.provider_request_id,
                    event_id=f"batch45-{label}-future-started",
                )
                future_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        future,
                        event_id=f"batch45-{label}-future-chunk",
                    )
                )
                registry.audio_produced(
                    future.provider_request_id,
                    event_id=f"batch45-{label}-future-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        future.provider_request_id,
                        event_id=f"batch45-{label}-future-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    future_terminal = registry.complete(
                        future.provider_request_id,
                        event_id=f"batch45-{label}-future-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    future_terminal = registry.fail(
                        future.provider_request_id,
                        event_id=f"batch45-{label}-future-failed",
                        reason="future provider failure",
                        reason_code=reason_code,
                    )
                else:
                    future_terminal = registry.cancel(
                        future.provider_request_id,
                        event_id=f"batch45-{label}-future-cancelled",
                        reason="future user cancellation",
                        reason_code=reason_code,
                    )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)

                self.assertIs(future_terminal.audio_chunks, future_with_chunk.audio_chunks)
                self.assertIs(future_terminal.audio_chunks[0], future_with_chunk.audio_chunks[0])
                self.assertIs(future_accepted.events[0], future_terminal.events[0])
                self.assertEqual(future_terminal.state, terminal_state)
                self.assertEqual(future_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(future_terminal.terminal_reason_code, reason_code)
                self.assertTrue(future_terminal.audio_produced)
                self.assertEqual(future_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertEqual(future_terminal.events[0].event_id, reused_accepted_id)
                self.assertEqual(future_terminal.events[0].provider_name, "voicestudio")
                self.assertEqual(future_terminal.events[0].provider_generation, 5)

                reusable_chunk_id = f"batch45-{label}-future-late-event-reused-as-chunk"
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reusable_chunk_id,
                    )
                self.assertNotIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                registry.advance_generation("voicestudio", 6)
                registry.advance_generation("edge_tts", 10)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reusable_chunk_id,
                    )

                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch45-{label}-receiver",
                    provider_generation=10,
                )
                registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch45-{label}-receiver-accepted",
                )
                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch45-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=reused_accepted_id,
                        )
                    )

                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=reusable_chunk_id,
                    )
                )
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
                self.assertIs(receiver_with_chunk.events, receiver_events)
                self.assertIs(receiver_with_chunk.audio_chunks, receiver_chunks)
                self.assertIs(receiver_with_chunk.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(tuple(chunk.event_id for chunk in receiver_chunks), (reusable_chunk_id,))
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 10)
                self.assertIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertEqual(registry.active_generation("voicestudio"), 6)
                self.assertEqual(registry.active_generation("edge_tts"), 10)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

    def test_reused_lifecycle_chunk_receiver_terminal_fence_reverse_reuse_matrix(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 6)
                registry.activate_generation("edge_tts", 10)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch46-{label}-source",
                    reply_id=f"reply-batch46-{label}-source",
                    segment_id=f"segment-batch46-{label}-source",
                    request_sequence=1,
                    text="第六代 Provider 已完成句段。",
                    audio_format=self.audio_format,
                    provider_generation=6,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch46-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch46-{label}-source-started",
                )
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        source,
                        event_id=f"batch46-{label}-source-chunk",
                    )
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch46-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch46-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch46-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch46-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 7)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch46-{label}-receiver",
                    provider_generation=10,
                )
                registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch46-{label}-receiver-accepted",
                )
                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch46-{label}-receiver-started",
                )
                receiver_events_before_chunk = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events_before_chunk)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks_before)

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=reused_chunk_id,
                    )
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch46-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch46-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch46-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch46-{label}-receiver-failed",
                        reason="receiver provider failure",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch46-{label}-receiver-cancelled",
                        reason="receiver user cancellation",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_with_chunk.audio_chunks[0])
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 10)

                reusable_accepted_id = f"batch46-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 11)
                registry.advance_generation("voicestudio", 8)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch46-{label}-future",
                    reply_id=f"reply-batch46-{label}-future",
                    segment_id=f"segment-batch46-{label}-future",
                    request_sequence=1,
                    text="第八代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=8,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )

                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 8)
                self.assertEqual(registry.active_generation("voicestudio"), 8)
                self.assertEqual(registry.active_generation("edge_tts"), 11)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_receiver_late_chunk_future_terminal_fence_reverse_chunk_matrix(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("edge_tts", 10)
                registry.activate_generation("voicestudio", 8)

                source = self._prepare_other_provider_request(
                    suffix=f"batch47-{label}-source",
                    provider_generation=10,
                )
                registry.accept(
                    source.provider_request_id,
                    event_id=f"batch47-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch47-{label}-source-started",
                )
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        source,
                        event_id=f"batch47-{label}-source-chunk",
                    )
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch47-{label}-source-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        source.provider_request_id,
                        event_id=f"batch47-{label}-source-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    source_terminal = registry.complete(
                        source.provider_request_id,
                        event_id=f"batch47-{label}-source-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    source_terminal = registry.fail(
                        source.provider_request_id,
                        event_id=f"batch47-{label}-source-failed",
                        reason="source provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    source_terminal = registry.cancel(
                        source.provider_request_id,
                        event_id=f"batch47-{label}-source-cancelled",
                        reason="source cancelled after enqueue",
                        reason_code=reason_code,
                    )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reusable_accepted_id = f"batch47-{label}-source-late-chunk-reused-as-accepted"
                source_late_chunk = self._chunk_for_request(
                    source,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(source_late_chunk)

                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("edge_tts", 11)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(source_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch47-{label}-future",
                    reply_id=f"reply-batch47-{label}-future",
                    segment_id=f"segment-batch47-{label}-future",
                    request_sequence=1,
                    text="第八代 Provider 终态续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=8,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                registry.synthesis_started(
                    future.provider_request_id,
                    event_id=f"batch47-{label}-future-started",
                )
                future_chunk_id = f"batch47-{label}-future-chunk"
                future_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        future,
                        event_id=future_chunk_id,
                    )
                )
                registry.audio_produced(
                    future.provider_request_id,
                    event_id=f"batch47-{label}-future-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        future.provider_request_id,
                        event_id=f"batch47-{label}-future-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    future_terminal = registry.complete(
                        future.provider_request_id,
                        event_id=f"batch47-{label}-future-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    future_terminal = registry.fail(
                        future.provider_request_id,
                        event_id=f"batch47-{label}-future-failed",
                        reason="future provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    future_terminal = registry.cancel(
                        future.provider_request_id,
                        event_id=f"batch47-{label}-future-cancelled",
                        reason="future cancelled after enqueue",
                        reason_code=reason_code,
                    )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)

                self.assertEqual(future_terminal.state, terminal_state)
                self.assertEqual(future_terminal.events[-1].kind, terminal_kind)
                self.assertEqual(future_terminal.events[-1].reason_code, reason_code)
                self.assertTrue(future_terminal.audio_produced)
                self.assertEqual(future_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(future_terminal.audio_chunks, future_with_chunk.audio_chunks)
                self.assertIs(future_terminal.audio_chunks[0], future_chunks[0])
                self.assertIs(future_accepted.events[0], future_terminal.events[0])
                self.assertEqual(future_terminal.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_terminal.events[0].provider_name, "voicestudio")
                self.assertEqual(future_terminal.events[0].provider_generation, 8)
                self.assertEqual(future_chunks[0].event_id, future_chunk_id)
                self.assertEqual(future_chunks[0].audio_chunk_index, 1)
                self.assertEqual(future_chunks[0].receive_sequence, 1)
                self.assertEqual(future_chunks[0].provider_name, "voicestudio")
                self.assertEqual(future_chunks[0].provider_generation, 8)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                reusable_chunk_id = f"batch47-{label}-future-late-event-reused-as-chunk"
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reusable_chunk_id,
                    )
                self.assertNotIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                registry.advance_generation("voicestudio", 9)
                registry.advance_generation("edge_tts", 12)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reusable_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch47-{label}-receiver",
                    provider_generation=12,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch47-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=future_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch47-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=reusable_accepted_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=reusable_chunk_id,
                    )
                )
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
                self.assertIs(receiver_with_chunk.events, receiver_events)
                self.assertIs(receiver_with_chunk.audio_chunks, receiver_chunks)
                self.assertIs(receiver_with_chunk.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(tuple(chunk.event_id for chunk in receiver_chunks), (reusable_chunk_id,))
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 12)
                self.assertIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertEqual(registry.active_generation("voicestudio"), 9)
                self.assertEqual(registry.active_generation("edge_tts"), 12)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 9)
                registry.activate_generation("edge_tts", 12)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch48-{label}-source",
                    reply_id=f"reply-batch48-{label}-source",
                    segment_id=f"segment-batch48-{label}-source",
                    request_sequence=1,
                    text="第九代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=9,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch48-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch48-{label}-source-started",
                )
                source_chunk_id = f"batch48-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch48-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch48-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch48-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch48-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 10)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch48-{label}-receiver",
                    provider_generation=12,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch48-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch48-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch48-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch48-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch48-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch48-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch48-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 12)

                reusable_accepted_id = f"batch48-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 13)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch48-{label}-future",
                    reply_id=f"reply-batch48-{label}-future",
                    segment_id=f"segment-batch48-{label}-future",
                    request_sequence=1,
                    text="第十代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=10,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 10)
                self.assertEqual(registry.active_generation("voicestudio"), 10)
                self.assertEqual(registry.active_generation("edge_tts"), 13)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_receiver_late_chunk_future_terminal_fence_reverse_chunk_matrix_next_generations(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("edge_tts", 12)
                registry.activate_generation("voicestudio", 10)

                source = self._prepare_other_provider_request(
                    suffix=f"batch49-{label}-source",
                    provider_generation=12,
                )
                registry.accept(
                    source.provider_request_id,
                    event_id=f"batch49-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch49-{label}-source-started",
                )
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        source,
                        event_id=f"batch49-{label}-source-chunk",
                    )
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch49-{label}-source-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        source.provider_request_id,
                        event_id=f"batch49-{label}-source-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    source_terminal = registry.complete(
                        source.provider_request_id,
                        event_id=f"batch49-{label}-source-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    source_terminal = registry.fail(
                        source.provider_request_id,
                        event_id=f"batch49-{label}-source-failed",
                        reason="source provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    source_terminal = registry.cancel(
                        source.provider_request_id,
                        event_id=f"batch49-{label}-source-cancelled",
                        reason="source cancelled after enqueue",
                        reason_code=reason_code,
                    )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reusable_accepted_id = f"batch49-{label}-source-late-chunk-reused-as-accepted"
                source_late_chunk = self._chunk_for_request(
                    source,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(source_late_chunk)

                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("edge_tts", 13)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(source_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch49-{label}-future",
                    reply_id=f"reply-batch49-{label}-future",
                    segment_id=f"segment-batch49-{label}-future",
                    request_sequence=1,
                    text="第十代 Provider 终态续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=10,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                registry.synthesis_started(
                    future.provider_request_id,
                    event_id=f"batch49-{label}-future-started",
                )
                future_chunk_id = f"batch49-{label}-future-chunk"
                future_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        future,
                        event_id=future_chunk_id,
                    )
                )
                registry.audio_produced(
                    future.provider_request_id,
                    event_id=f"batch49-{label}-future-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        future.provider_request_id,
                        event_id=f"batch49-{label}-future-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    future_terminal = registry.complete(
                        future.provider_request_id,
                        event_id=f"batch49-{label}-future-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    future_terminal = registry.fail(
                        future.provider_request_id,
                        event_id=f"batch49-{label}-future-failed",
                        reason="future provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    future_terminal = registry.cancel(
                        future.provider_request_id,
                        event_id=f"batch49-{label}-future-cancelled",
                        reason="future cancelled after enqueue",
                        reason_code=reason_code,
                    )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)

                self.assertEqual(future_terminal.state, terminal_state)
                self.assertEqual(future_terminal.events[-1].kind, terminal_kind)
                self.assertEqual(future_terminal.events[-1].reason_code, reason_code)
                self.assertTrue(future_terminal.audio_produced)
                self.assertEqual(future_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(future_terminal.audio_chunks, future_with_chunk.audio_chunks)
                self.assertIs(future_terminal.audio_chunks[0], future_chunks[0])
                self.assertIs(future_accepted.events[0], future_terminal.events[0])
                self.assertEqual(future_terminal.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_terminal.events[0].provider_name, "voicestudio")
                self.assertEqual(future_terminal.events[0].provider_generation, 10)
                self.assertEqual(future_chunks[0].event_id, future_chunk_id)
                self.assertEqual(future_chunks[0].audio_chunk_index, 1)
                self.assertEqual(future_chunks[0].receive_sequence, 1)
                self.assertEqual(future_chunks[0].provider_name, "voicestudio")
                self.assertEqual(future_chunks[0].provider_generation, 10)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                reusable_chunk_id = f"batch49-{label}-future-late-event-reused-as-chunk"
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reusable_chunk_id,
                    )
                self.assertNotIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                registry.advance_generation("voicestudio", 11)
                registry.advance_generation("edge_tts", 14)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reusable_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch49-{label}-receiver",
                    provider_generation=14,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch49-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=future_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch49-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=reusable_accepted_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(
                        receiver,
                        event_id=reusable_chunk_id,
                    )
                )
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)
                self.assertIs(receiver_with_chunk.events, receiver_events)
                self.assertIs(receiver_with_chunk.audio_chunks, receiver_chunks)
                self.assertIs(receiver_with_chunk.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(tuple(chunk.event_id for chunk in receiver_chunks), (reusable_chunk_id,))
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 14)
                self.assertIn(reusable_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertEqual(registry.active_generation("voicestudio"), 11)
                self.assertEqual(registry.active_generation("edge_tts"), 14)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(future.provider_request_id), future_terminal)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_next_generations(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 11)
                registry.activate_generation("edge_tts", 14)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch50-{label}-source",
                    reply_id=f"reply-batch50-{label}-source",
                    segment_id=f"segment-batch50-{label}-source",
                    request_sequence=1,
                    text="第十一代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=11,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch50-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch50-{label}-source-started",
                )
                source_chunk_id = f"batch50-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch50-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch50-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch50-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch50-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 12)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch50-{label}-receiver",
                    provider_generation=14,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch50-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch50-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch50-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch50-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch50-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch50-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch50-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 14)

                reusable_accepted_id = f"batch50-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 15)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch50-{label}-future",
                    reply_id=f"reply-batch50-{label}-future",
                    segment_id=f"segment-batch50-{label}-future",
                    request_sequence=1,
                    text="第十二代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=12,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 12)
                self.assertEqual(registry.active_generation("voicestudio"), 12)
                self.assertEqual(registry.active_generation("edge_tts"), 15)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_next_generations_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 12)
                registry.activate_generation("edge_tts", 15)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch51-{label}-source",
                    reply_id=f"reply-batch51-{label}-source",
                    segment_id=f"segment-batch51-{label}-source",
                    request_sequence=1,
                    text="第十二代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=12,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch51-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch51-{label}-source-started",
                )
                source_chunk_id = f"batch51-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch51-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch51-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch51-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch51-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 13)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch51-{label}-receiver",
                    provider_generation=15,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch51-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch51-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch51-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch51-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch51-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch51-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch51-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 15)

                reusable_accepted_id = f"batch51-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 16)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch51-{label}-future",
                    reply_id=f"reply-batch51-{label}-future",
                    segment_id=f"segment-batch51-{label}-future",
                    request_sequence=1,
                    text="第十三代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=13,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 13)
                self.assertEqual(registry.active_generation("voicestudio"), 13)
                self.assertEqual(registry.active_generation("edge_tts"), 16)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_second_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 13)
                registry.activate_generation("edge_tts", 16)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch52-{label}-source",
                    reply_id=f"reply-batch52-{label}-source",
                    segment_id=f"segment-batch52-{label}-source",
                    request_sequence=1,
                    text="第十三代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=13,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch52-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch52-{label}-source-started",
                )
                source_chunk_id = f"batch52-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch52-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch52-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch52-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch52-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 14)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch52-{label}-receiver",
                    provider_generation=16,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch52-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch52-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch52-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch52-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch52-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch52-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch52-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 16)

                reusable_accepted_id = f"batch52-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 17)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch52-{label}-future",
                    reply_id=f"reply-batch52-{label}-future",
                    segment_id=f"segment-batch52-{label}-future",
                    request_sequence=1,
                    text="第十四代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=14,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 14)
                self.assertEqual(registry.active_generation("voicestudio"), 14)
                self.assertEqual(registry.active_generation("edge_tts"), 17)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_fifth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 16)
                registry.activate_generation("edge_tts", 19)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch55-{label}-source",
                    reply_id=f"reply-batch55-{label}-source",
                    segment_id=f"segment-batch55-{label}-source",
                    request_sequence=1,
                    text="第十六代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=16,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch55-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch55-{label}-source-started",
                )
                source_chunk_id = f"batch55-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch55-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch55-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch55-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch55-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 17)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch55-{label}-receiver",
                    provider_generation=19,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch55-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch55-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch55-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch55-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch55-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch55-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch55-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 19)

                reusable_accepted_id = f"batch55-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 20)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch55-{label}-future",
                    reply_id=f"reply-batch55-{label}-future",
                    segment_id=f"segment-batch55-{label}-future",
                    request_sequence=1,
                    text="第十七代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=17,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 17)
                self.assertEqual(registry.active_generation("voicestudio"), 17)
                self.assertEqual(registry.active_generation("edge_tts"), 20)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_sixth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 17)
                registry.activate_generation("edge_tts", 20)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch56-{label}-source",
                    reply_id=f"reply-batch56-{label}-source",
                    segment_id=f"segment-batch56-{label}-source",
                    request_sequence=1,
                    text="第十七代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=17,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch56-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch56-{label}-source-started",
                )
                source_chunk_id = f"batch56-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch56-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch56-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch56-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch56-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 18)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch56-{label}-receiver",
                    provider_generation=20,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch56-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch56-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch56-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch56-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch56-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch56-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch56-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 20)

                reusable_accepted_id = f"batch56-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 21)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch56-{label}-future",
                    reply_id=f"reply-batch56-{label}-future",
                    segment_id=f"segment-batch56-{label}-future",
                    request_sequence=1,
                    text="第十八代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=18,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 18)
                self.assertEqual(registry.active_generation("voicestudio"), 18)
                self.assertEqual(registry.active_generation("edge_tts"), 21)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_seventh_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 18)
                registry.activate_generation("edge_tts", 21)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch57-{label}-source",
                    reply_id=f"reply-batch57-{label}-source",
                    segment_id=f"segment-batch57-{label}-source",
                    request_sequence=1,
                    text="第十八代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=18,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch57-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch57-{label}-source-started",
                )
                source_chunk_id = f"batch57-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch57-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch57-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch57-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch57-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 19)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch57-{label}-receiver",
                    provider_generation=21,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch57-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch57-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch57-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch57-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch57-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch57-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch57-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 21)

                reusable_accepted_id = f"batch57-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 22)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch57-{label}-future",
                    reply_id=f"reply-batch57-{label}-future",
                    segment_id=f"segment-batch57-{label}-future",
                    request_sequence=1,
                    text="第十九代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=19,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 19)
                self.assertEqual(registry.active_generation("voicestudio"), 19)
                self.assertEqual(registry.active_generation("edge_tts"), 22)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_eighth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 19)
                registry.activate_generation("edge_tts", 22)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch58-{label}-source",
                    reply_id=f"reply-batch58-{label}-source",
                    segment_id=f"segment-batch58-{label}-source",
                    request_sequence=1,
                    text="第十九代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=19,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch58-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch58-{label}-source-started",
                )
                source_chunk_id = f"batch58-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch58-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch58-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch58-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch58-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 20)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch58-{label}-receiver",
                    provider_generation=22,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch58-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch58-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch58-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch58-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch58-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch58-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch58-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 22)

                reusable_accepted_id = f"batch58-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 23)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch58-{label}-future",
                    reply_id=f"reply-batch58-{label}-future",
                    segment_id=f"segment-batch58-{label}-future",
                    request_sequence=1,
                    text="第二十代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=20,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 20)
                self.assertEqual(registry.active_generation("voicestudio"), 20)
                self.assertEqual(registry.active_generation("edge_tts"), 23)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_ninth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 20)
                registry.activate_generation("edge_tts", 23)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch59-{label}-source",
                    reply_id=f"reply-batch59-{label}-source",
                    segment_id=f"segment-batch59-{label}-source",
                    request_sequence=1,
                    text="第二十代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=20,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch59-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch59-{label}-source-started",
                )
                source_chunk_id = f"batch59-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch59-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch59-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch59-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch59-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 22)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch59-{label}-receiver",
                    provider_generation=23,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch59-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch59-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch59-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch59-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch59-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch59-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch59-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 23)

                reusable_accepted_id = f"batch59-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 25)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch59-{label}-future",
                    reply_id=f"reply-batch59-{label}-future",
                    segment_id=f"segment-batch59-{label}-future",
                    request_sequence=1,
                    text="第二十一代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=22,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 22)
                self.assertEqual(registry.active_generation("voicestudio"), 22)
                self.assertEqual(registry.active_generation("edge_tts"), 25)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_eleventh_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 22)
                registry.activate_generation("edge_tts", 25)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch61-{label}-source",
                    reply_id=f"reply-batch61-{label}-source",
                    segment_id=f"segment-batch61-{label}-source",
                    request_sequence=1,
                    text="第二十二代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=22,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch61-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch61-{label}-source-started",
                )
                source_chunk_id = f"batch61-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch61-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch61-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch61-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch61-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 23)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch61-{label}-receiver",
                    provider_generation=25,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch61-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch61-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch61-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch61-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch61-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch61-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch61-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 25)

                reusable_accepted_id = f"batch61-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 26)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch61-{label}-future",
                    reply_id=f"reply-batch61-{label}-future",
                    segment_id=f"segment-batch61-{label}-future",
                    request_sequence=1,
                    text="第二十三代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=23,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 23)
                self.assertEqual(registry.active_generation("voicestudio"), 23)
                self.assertEqual(registry.active_generation("edge_tts"), 26)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_twelfth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 23)
                registry.activate_generation("edge_tts", 26)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch62-{label}-source",
                    reply_id=f"reply-batch62-{label}-source",
                    segment_id=f"segment-batch62-{label}-source",
                    request_sequence=1,
                    text="第二十三代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=23,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch62-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch62-{label}-source-started",
                )
                source_chunk_id = f"batch62-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch62-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch62-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch62-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch62-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 24)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch62-{label}-receiver",
                    provider_generation=26,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch62-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch62-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch62-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch62-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch62-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch62-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch62-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 26)

                reusable_accepted_id = f"batch62-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 27)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch62-{label}-future",
                    reply_id=f"reply-batch62-{label}-future",
                    segment_id=f"segment-batch62-{label}-future",
                    request_sequence=1,
                    text="第二十四代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=24,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 24)
                self.assertEqual(registry.active_generation("voicestudio"), 24)
                self.assertEqual(registry.active_generation("edge_tts"), 27)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_thirteenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 24)
                registry.activate_generation("edge_tts", 27)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch63-{label}-source",
                    reply_id=f"reply-batch63-{label}-source",
                    segment_id=f"segment-batch63-{label}-source",
                    request_sequence=1,
                    text="第二十四代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=24,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch63-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch63-{label}-source-started",
                )
                source_chunk_id = f"batch63-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch63-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch63-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch63-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch63-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 25)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch63-{label}-receiver",
                    provider_generation=27,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch63-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch63-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch63-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch63-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch63-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch63-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch63-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 27)

                reusable_accepted_id = f"batch63-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 28)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch63-{label}-future",
                    reply_id=f"reply-batch63-{label}-future",
                    segment_id=f"segment-batch63-{label}-future",
                    request_sequence=1,
                    text="第二十五代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=25,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 25)
                self.assertEqual(registry.active_generation("voicestudio"), 25)
                self.assertEqual(registry.active_generation("edge_tts"), 28)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_nineteenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 30)
                registry.activate_generation("edge_tts", 33)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch69-{label}-source",
                    reply_id=f"reply-batch69-{label}-source",
                    segment_id=f"segment-batch69-{label}-source",
                    request_sequence=1,
                    text="第三十代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=30,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch69-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch69-{label}-source-started",
                )
                source_chunk_id = f"batch69-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch69-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch69-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch69-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch69-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 31)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch69-{label}-receiver",
                    provider_generation=33,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch69-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch69-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch69-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch69-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch69-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch69-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch69-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 33)

                reusable_accepted_id = f"batch69-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 34)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch69-{label}-future",
                    reply_id=f"reply-batch69-{label}-future",
                    segment_id=f"segment-batch69-{label}-future",
                    request_sequence=1,
                    text="第三十一代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=31,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 31)
                self.assertEqual(registry.active_generation("voicestudio"), 31)
                self.assertEqual(registry.active_generation("edge_tts"), 34)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_eighteenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 29)
                registry.activate_generation("edge_tts", 32)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch68-{label}-source",
                    reply_id=f"reply-batch68-{label}-source",
                    segment_id=f"segment-batch68-{label}-source",
                    request_sequence=1,
                    text="第二十九代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=29,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch68-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch68-{label}-source-started",
                )
                source_chunk_id = f"batch68-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch68-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch68-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch68-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch68-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 30)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch68-{label}-receiver",
                    provider_generation=32,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch68-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch68-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch68-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch68-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch68-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch68-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch68-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 32)

                reusable_accepted_id = f"batch68-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 33)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch68-{label}-future",
                    reply_id=f"reply-batch68-{label}-future",
                    segment_id=f"segment-batch68-{label}-future",
                    request_sequence=1,
                    text="第三十代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=30,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 30)
                self.assertEqual(registry.active_generation("voicestudio"), 30)
                self.assertEqual(registry.active_generation("edge_tts"), 33)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_seventeenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 28)
                registry.activate_generation("edge_tts", 31)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch67-{label}-source",
                    reply_id=f"reply-batch67-{label}-source",
                    segment_id=f"segment-batch67-{label}-source",
                    request_sequence=1,
                    text="第二十八代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=28,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch67-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch67-{label}-source-started",
                )
                source_chunk_id = f"batch67-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch67-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch67-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch67-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch67-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 29)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch67-{label}-receiver",
                    provider_generation=31,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch67-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch67-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch67-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch67-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch67-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch67-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch67-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 31)

                reusable_accepted_id = f"batch67-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 32)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch67-{label}-future",
                    reply_id=f"reply-batch67-{label}-future",
                    segment_id=f"segment-batch67-{label}-future",
                    request_sequence=1,
                    text="第二十九代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=29,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 29)
                self.assertEqual(registry.active_generation("voicestudio"), 29)
                self.assertEqual(registry.active_generation("edge_tts"), 32)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_sixteenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 27)
                registry.activate_generation("edge_tts", 30)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch66-{label}-source",
                    reply_id=f"reply-batch66-{label}-source",
                    segment_id=f"segment-batch66-{label}-source",
                    request_sequence=1,
                    text="第二十七代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=27,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch66-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch66-{label}-source-started",
                )
                source_chunk_id = f"batch66-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch66-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch66-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch66-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch66-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 28)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch66-{label}-receiver",
                    provider_generation=30,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch66-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch66-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch66-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch66-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch66-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch66-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch66-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 30)

                reusable_accepted_id = f"batch66-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 31)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch66-{label}-future",
                    reply_id=f"reply-batch66-{label}-future",
                    segment_id=f"segment-batch66-{label}-future",
                    request_sequence=1,
                    text="第二十八代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=28,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 28)
                self.assertEqual(registry.active_generation("voicestudio"), 28)
                self.assertEqual(registry.active_generation("edge_tts"), 31)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_fifteenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 26)
                registry.activate_generation("edge_tts", 29)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch65-{label}-source",
                    reply_id=f"reply-batch65-{label}-source",
                    segment_id=f"segment-batch65-{label}-source",
                    request_sequence=1,
                    text="第二十六代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=26,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch65-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch65-{label}-source-started",
                )
                source_chunk_id = f"batch65-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch65-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch65-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch65-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch65-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 27)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch65-{label}-receiver",
                    provider_generation=29,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch65-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch65-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch65-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch65-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch65-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch65-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch65-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 29)

                reusable_accepted_id = f"batch65-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 30)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch65-{label}-future",
                    reply_id=f"reply-batch65-{label}-future",
                    segment_id=f"segment-batch65-{label}-future",
                    request_sequence=1,
                    text="第二十七代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=27,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 27)
                self.assertEqual(registry.active_generation("voicestudio"), 27)
                self.assertEqual(registry.active_generation("edge_tts"), 30)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_fourteenth_followup(self) -> None:
        cases = (
            (
                "completed",
                ProviderRequestLifecycleState.COMPLETED,
                ProviderRequestLifecycleEventKind.COMPLETED,
                None,
                True,
            ),
            (
                "failed",
                ProviderRequestLifecycleState.FAILED,
                ProviderRequestLifecycleEventKind.FAILED,
                ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                False,
            ),
            (
                "cancelled",
                ProviderRequestLifecycleState.CANCELLED,
                ProviderRequestLifecycleEventKind.CANCELLED,
                ProviderRequestLifecycleReasonCode.USER_CANCELLED,
                True,
            ),
        )
        for label, terminal_state, terminal_kind, reason_code, enqueue_before_terminal in cases:
            with self.subTest(state=label):
                registry = ProviderRequestLifecycleRegistry(self.requests)
                registry.activate_generation("voicestudio", 25)
                registry.activate_generation("edge_tts", 28)

                source = self.requests.prepare(
                    provider_request_id=f"provider-request-batch64-{label}-source",
                    reply_id=f"reply-batch64-{label}-source",
                    segment_id=f"segment-batch64-{label}-source",
                    request_sequence=1,
                    text="第二十五代 Provider 已完成续接句段。",
                    audio_format=self.audio_format,
                    provider_generation=25,
                    session_id="session-1",
                )
                source_accepted = registry.accept(
                    source.provider_request_id,
                    event_id=f"batch64-{label}-source-accepted",
                )
                registry.synthesis_started(
                    source.provider_request_id,
                    event_id=f"batch64-{label}-source-started",
                )
                source_chunk_id = f"batch64-{label}-source-chunk"
                source_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(source, event_id=source_chunk_id)
                )
                registry.audio_produced(
                    source.provider_request_id,
                    event_id=f"batch64-{label}-source-produced",
                )
                registry.audio_enqueued(
                    source.provider_request_id,
                    event_id=f"batch64-{label}-source-enqueued",
                )
                source_terminal = registry.complete(
                    source.provider_request_id,
                    event_id=f"batch64-{label}-source-completed",
                )
                source_events = registry.events_for(source.provider_request_id)
                source_chunks = registry.audio_chunks_for(source.provider_request_id)
                reused_chunk_id = f"batch64-{label}-source-late-event-reused-as-chunk"

                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(source_terminal.audio_chunks, source_with_chunk.audio_chunks)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)

                registry.advance_generation("voicestudio", 26)
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.synthesis_started(
                        source.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertNotIn(reused_chunk_id, registry._event_ids)  # type: ignore[attr-defined]

                receiver = self._prepare_other_provider_request(
                    suffix=f"batch64-{label}-receiver",
                    provider_generation=28,
                )
                receiver_accepted = registry.accept(
                    receiver.provider_request_id,
                    event_id=f"batch64-{label}-receiver-accepted",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks_before = registry.audio_chunks_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        receiver.provider_request_id,
                        event_id=source_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_accepted)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_started = registry.synthesis_started(
                    receiver.provider_request_id,
                    event_id=f"batch64-{label}-receiver-started",
                )
                receiver_events = registry.events_for(receiver.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(
                        self._chunk_for_request(
                            receiver,
                            event_id=source_accepted.events[0].event_id,
                        )
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_started)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(
                    registry.audio_chunks_for(receiver.provider_request_id),
                    receiver_chunks_before,
                )

                receiver_with_chunk = registry.record_audio_chunk(
                    self._chunk_for_request(receiver, event_id=reused_chunk_id)
                )
                registry.audio_produced(
                    receiver.provider_request_id,
                    event_id=f"batch64-{label}-receiver-produced",
                )
                if enqueue_before_terminal:
                    registry.audio_enqueued(
                        receiver.provider_request_id,
                        event_id=f"batch64-{label}-receiver-enqueued",
                    )
                if terminal_state is ProviderRequestLifecycleState.COMPLETED:
                    receiver_terminal = registry.complete(
                        receiver.provider_request_id,
                        event_id=f"batch64-{label}-receiver-completed",
                    )
                elif terminal_state is ProviderRequestLifecycleState.FAILED:
                    receiver_terminal = registry.fail(
                        receiver.provider_request_id,
                        event_id=f"batch64-{label}-receiver-failed",
                        reason="receiver provider failed after partial audio",
                        reason_code=reason_code,
                    )
                else:
                    receiver_terminal = registry.cancel(
                        receiver.provider_request_id,
                        event_id=f"batch64-{label}-receiver-cancelled",
                        reason="receiver cancelled after enqueue",
                        reason_code=reason_code,
                    )
                receiver_events = registry.events_for(receiver.provider_request_id)
                receiver_chunks = registry.audio_chunks_for(receiver.provider_request_id)

                self.assertEqual(receiver_terminal.state, terminal_state)
                self.assertEqual(receiver_terminal.terminal_event.kind, terminal_kind)
                self.assertEqual(receiver_terminal.terminal_reason_code, reason_code)
                self.assertTrue(receiver_terminal.audio_produced)
                self.assertEqual(receiver_terminal.audio_enqueued, enqueue_before_terminal)
                self.assertIs(receiver_terminal.events, receiver_events)
                self.assertIs(receiver_terminal.audio_chunks, receiver_with_chunk.audio_chunks)
                self.assertIs(receiver_terminal.audio_chunks, receiver_chunks)
                self.assertIs(receiver_terminal.audio_chunks[0], receiver_chunks[0])
                self.assertEqual(receiver_chunks[0].event_id, reused_chunk_id)
                self.assertEqual(receiver_chunks[0].audio_chunk_index, 1)
                self.assertEqual(receiver_chunks[0].receive_sequence, 1)
                self.assertEqual(receiver_chunks[0].provider_name, "edge_tts")
                self.assertEqual(receiver_chunks[0].provider_generation, 28)

                reusable_accepted_id = f"batch64-{label}-receiver-late-chunk-reused-as-accepted"
                receiver_late_chunk = self._chunk_for_request(
                    receiver,
                    event_id=reusable_accepted_id,
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                registry.advance_generation("edge_tts", 29)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.record_audio_chunk(receiver_chunks[0])
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    registry.record_audio_chunk(receiver_late_chunk)
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertNotIn(reusable_accepted_id, registry._event_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

                future = self.requests.prepare(
                    provider_request_id=f"provider-request-batch64-{label}-future",
                    reply_id=f"reply-batch64-{label}-future",
                    segment_id=f"segment-batch64-{label}-future",
                    request_sequence=1,
                    text="第二十六代 Provider 后续句段。",
                    audio_format=self.audio_format,
                    provider_generation=26,
                    session_id="session-1",
                )
                future_accepted = registry.accept(
                    future.provider_request_id,
                    event_id=reusable_accepted_id,
                )
                future_events = registry.events_for(future.provider_request_id)
                future_chunks = registry.audio_chunks_for(future.provider_request_id)
                committed_ids = set(registry._event_ids)  # type: ignore[attr-defined]
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
                    registry.synthesis_started(
                        future.provider_request_id,
                        event_id=reused_chunk_id,
                    )
                self.assertEqual(registry._event_ids, committed_ids)  # type: ignore[attr-defined]
                self.assertIs(registry.get(future.provider_request_id), future_accepted)
                self.assertIs(registry.events_for(future.provider_request_id), future_events)
                self.assertIs(registry.audio_chunks_for(future.provider_request_id), future_chunks)
                self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
                self.assertEqual(future_accepted.events[0].event_id, reusable_accepted_id)
                self.assertEqual(future_accepted.events[0].provider_name, "voicestudio")
                self.assertEqual(future_accepted.events[0].provider_generation, 26)
                self.assertEqual(registry.active_generation("voicestudio"), 26)
                self.assertEqual(registry.active_generation("edge_tts"), 29)
                self.assertIs(registry.get(source.provider_request_id), source_terminal)
                self.assertIs(registry.events_for(source.provider_request_id), source_events)
                self.assertIs(registry.audio_chunks_for(source.provider_request_id), source_chunks)
                self.assertIs(registry.get(receiver.provider_request_id), receiver_terminal)
                self.assertIs(registry.events_for(receiver.provider_request_id), receiver_events)
                self.assertIs(registry.audio_chunks_for(receiver.provider_request_id), receiver_chunks)

    def test_snapshot_event_history_must_replay_state_machine_in_order_atomically(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="batch28-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch28-started")
        self.registry.record_audio_chunk(self._chunk(event_id="batch28-chunk"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="batch28-produced")
        enqueued = self.registry.audio_enqueued(
            self.request.provider_request_id,
            event_id="batch28-enqueued",
        )
        events = self.registry.events_for(self.request.provider_request_id)
        chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        reordered_events = (
            enqueued.events[0],
            enqueued.events[1],
            replace(enqueued.events[3], event_sequence=3),
            replace(enqueued.events[2], event_sequence=4),
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "audio_enqueued"):
            replace(enqueued, events=reordered_events)

        current = self.registry.get(self.request.provider_request_id)
        self.assertIs(current, enqueued)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
    def test_public_projection_nested_tuples_are_immutable_observations(self) -> None:

        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        snapshot = self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        events = self.registry.events_for(self.request.provider_request_id)
        chunks = self.registry.audio_chunks_for(self.request.provider_request_id)

        self.assertIs(events, snapshot.events)
        self.assertIs(chunks, snapshot.audio_chunks)
        with self.assertRaises(AttributeError):
            events.append(snapshot.events[0])  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            events[0] = snapshot.events[0]  # type: ignore[index]
        with self.assertRaises(AttributeError):
            chunks.append(snapshot.audio_chunks[0])  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            chunks[0] = snapshot.audio_chunks[0]  # type: ignore[index]
        self.assertIs(self.registry.get(self.request.provider_request_id), snapshot)
        self.assertEqual(self.registry.events_for(self.request.provider_request_id), events)
        self.assertEqual(self.registry.audio_chunks_for(self.request.provider_request_id), chunks)
    def test_cross_request_projection_identity_and_shared_ids_remain_isolated(self) -> None:
        second = self._prepare_second_request()
        first = self.registry.accept(self.request.provider_request_id, event_id="cross-first-accept")
        first_events = self.registry.events_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.accept(second.provider_request_id, event_id="cross-first-accept")
        self.assertNotIn(second.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), first)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), first_events)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]

        second_accepted = self.registry.accept(second.provider_request_id, event_id="cross-second-accept")
        second_started = self.registry.synthesis_started(second.provider_request_id, event_id="cross-second-start")
        duplicate_chunk = replace(
            self._chunk(event_id="cross-first-accept"),
            provider_request_id=second.provider_request_id,
            reply_id=second.reply_id,
            segment_id=second.segment_id,
        )
        before_second_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.record_audio_chunk(duplicate_chunk)
        self.assertIs(self.registry.get(second.provider_request_id), second_started)
        self.assertIs(self.registry.events_for(second.provider_request_id), second_started.events)
        self.assertIs(self.registry.audio_chunks_for(second.provider_request_id), second_started.audio_chunks)
        self.assertEqual(self.registry._event_ids, before_second_ids)  # type: ignore[attr-defined]

        second_chunk = replace(
            self._chunk(event_id="cross-second-chunk"),
            provider_request_id=second.provider_request_id,
            reply_id=second.reply_id,
            segment_id=second.segment_id,
        )
        with_chunk = self.registry.record_audio_chunk(second_chunk)
        self.assertIsNot(self.registry.get(second.provider_request_id), first)
        self.assertIs(self.registry.get(self.request.provider_request_id), first)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), first_events)
        self.assertEqual(tuple(event.event_id for event in first_events), ("cross-first-accept",))
        self.assertEqual(
            tuple(event.event_id for event in with_chunk.events),
            ("cross-second-accept", "cross-second-start"),
        )
        self.assertEqual(tuple(chunk.event_id for chunk in with_chunk.audio_chunks), ("cross-second-chunk",))
        self.assertIs(self.registry.events_for(second.provider_request_id), with_chunk.events)
        self.assertIs(self.registry.audio_chunks_for(second.provider_request_id), with_chunk.audio_chunks)
        self.assertIsNot(first_events, with_chunk.events)

    def test_rejected_writes_preserve_snapshot_projection_identity_and_global_ids(self) -> None:
        accepted = self.registry.accept(self.request.provider_request_id, event_id="commit-accept")
        accepted_events = self.registry.events_for(self.request.provider_request_id)
        accepted_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        invalid_event = replace(
            accepted.events[0],
            event_id="rejected-sequence",
            event_sequence=3,
        )

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "非法生命周期事件序列"):
            self.registry.record(invalid_event)
        self.assertIs(self.registry.get(self.request.provider_request_id), accepted)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), accepted_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), accepted_chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]

        started = self.registry.synthesis_started(self.request.provider_request_id, event_id="commit-start")
        started_events = self.registry.events_for(self.request.provider_request_id)
        started_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "非法 audio_chunk_index"):
            self.registry.record_audio_chunk(
                self._chunk(event_id="rejected-chunk", audio_chunk_index=2, receive_sequence=2)
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), started)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), started_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), started_chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
    def test_terminal_projection_rejects_inconsistent_state_rewrite_atomically(self) -> None:
        self.registry.accept(self.request.provider_request_id, event_id="event-1")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="event-2")
        self.registry.record_audio_chunk(self._chunk(event_id="chunk-1"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="event-3")
        failed = self.registry.fail(
            self.request.provider_request_id,
            event_id="event-failed",
            reason="provider_failed",
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "末事件必须与 state 一致"):
            replace(failed, state=ProviderRequestLifecycleState.COMPLETED)
        current = self.registry.get(self.request.provider_request_id)
        self.assertEqual(current.state, ProviderRequestLifecycleState.FAILED)
        self.assertEqual(current.events, failed.events)
        self.assertEqual(current.audio_chunks, failed.audio_chunks)
    def test_public_snapshot_object_graph_is_read_only_and_preserves_identity(self) -> None:
        accepted = self.registry.accept(self.request.provider_request_id, event_id="batch20-accepted")
        started = self.registry.synthesis_started(self.request.provider_request_id, event_id="batch20-started")
        with_chunk = self.registry.record_audio_chunk(self._chunk(event_id="batch20-chunk"))
        snapshot = self.registry.get(self.request.provider_request_id)
        events = self.registry.events_for(self.request.provider_request_id)
        chunks = self.registry.audio_chunks_for(self.request.provider_request_id)

        self.assertIs(snapshot, with_chunk)
        self.assertIs(snapshot.request, self.request)
        self.assertIs(snapshot.events, events)
        self.assertIs(snapshot.audio_chunks, chunks)
        self.assertIs(snapshot.events[0], accepted.events[0])
        self.assertIs(snapshot.events[1], started.events[1])
        self.assertIs(snapshot.audio_chunks[0], with_chunk.audio_chunks[0])
        self.assertIs(snapshot.events[0].capability_check, self.request.capability_check)
        self.assertIs(snapshot.audio_chunks[0].audio_format, self.request.audio_format)

        with self.assertRaises(FrozenInstanceError):
            snapshot.state = ProviderRequestLifecycleState.COMPLETED  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.request.provider_name = "mutated"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.request.capability_request.audio_format = AudioFormat("opus", 48000, 2)  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.events[0].kind = ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.audio_chunks[0].audio_chunk_index = 99  # type: ignore[misc]

        self.assertIs(self.registry.get(self.request.provider_request_id), snapshot)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), chunks)

    def test_generation_observation_reads_are_side_effect_free_and_do_not_create_or_advance_fences(self) -> None:
        before_snapshots = dict(self.registry._snapshots)  # type: ignore[attr-defined]
        before_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_generations = dict(self.registry._active_generations)  # type: ignore[attr-defined]

        self.assertIsNone(self.registry.active_generation("voicestudio"))
        self.assertIsNone(self.registry.active_generation("unregistered-provider"))
        with self.assertRaises(ProviderRequestLifecycleError):
            self.registry.active_generation("")

        self.assertEqual(self.registry._snapshots, before_snapshots)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._event_ids, before_event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._active_generations, before_generations)  # type: ignore[attr-defined]

        self.registry.activate_generation("voicestudio", 3)
        after_activation_snapshots = dict(self.registry._snapshots)  # type: ignore[attr-defined]
        after_activation_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        after_activation_generations = dict(self.registry._active_generations)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.active_generation("voicestudio"), 3)
        self.assertIsNone(self.registry.active_generation("edge_tts"))
        self.assertEqual(self.registry._snapshots, after_activation_snapshots)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._event_ids, after_activation_event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry._active_generations, after_activation_generations)  # type: ignore[attr-defined]

    def test_empty_projection_identity_tracks_only_successful_write_commits(self) -> None:
        prepared = self.registry.get(self.request.provider_request_id)
        prepared_events = self.registry.events_for(self.request.provider_request_id)
        prepared_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        self.assertIs(prepared.events, prepared_events)
        self.assertIs(prepared.audio_chunks, prepared_chunks)
        self.assertEqual(prepared_events, ())
        self.assertEqual(prepared_chunks, ())

        rejected_event = ProviderRequestLifecycleEvent(
            event_id="batch22-event",
            event_sequence=1,
            provider_request_id=self.request.provider_request_id,
            reply_id="reply-other",
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(rejected_event)
        self.assertIs(self.registry.get(self.request.provider_request_id), prepared)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), prepared_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), prepared_chunks)
        self.assertNotIn("batch22-event", self.registry._event_ids)  # type: ignore[attr-defined]

        accepted = self.registry.accept(self.request.provider_request_id, event_id="batch22-event")
        self.assertIsNot(accepted, prepared)
        self.assertIs(self.registry.get(self.request.provider_request_id), accepted)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), accepted.events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), accepted.audio_chunks)
        self.assertIs(accepted.audio_chunks, prepared_chunks)
        self.assertEqual(prepared.events, ())
        self.assertEqual(prepared.audio_chunks, ())

        started = self.registry.synthesis_started(
            self.request.provider_request_id,
            event_id="batch22-started",
        )
        started_events = self.registry.events_for(self.request.provider_request_id)
        empty_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        rejected_chunk = self._chunk(
            event_id="batch22-chunk",
            audio_chunk_index=2,
            receive_sequence=2,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "非法 audio_chunk_index"):
            self.registry.record_audio_chunk(rejected_chunk)
        self.assertIs(self.registry.get(self.request.provider_request_id), started)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), started_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), empty_chunks)
        self.assertNotIn("batch22-chunk", self.registry._event_ids)  # type: ignore[attr-defined]

        with_chunk = self.registry.record_audio_chunk(self._chunk(event_id="batch22-chunk"))
        self.assertIsNot(with_chunk, started)
        self.assertIs(with_chunk.events, started_events)
        self.assertIsNot(with_chunk.audio_chunks, empty_chunks)
        self.assertIs(self.registry.get(self.request.provider_request_id), with_chunk)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), with_chunk.events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), with_chunk.audio_chunks)
        self.assertEqual(started.audio_chunks, ())

    def test_unknown_request_read_write_matrix_preserves_existing_commit_state(self) -> None:
        known = self.registry.get(self.request.provider_request_id)
        known_events = self.registry.events_for(self.request.provider_request_id)
        known_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        before_snapshots = dict(self.registry._snapshots)  # type: ignore[attr-defined]
        before_event_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        before_generations = dict(self.registry._active_generations)  # type: ignore[attr-defined]
        unknown_request_id = "missing-request"
        unknown_event = ProviderRequestLifecycleEvent(
            event_id="batch22-unknown-record",
            event_sequence=1,
            provider_request_id=unknown_request_id,
            reply_id=self.request.reply_id,
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        unknown_chunk = self._chunk(
            event_id="batch22-unknown-chunk",
            provider_request_id=unknown_request_id,
        )
        operations = (
            ("get", lambda: self.registry.get(unknown_request_id), None),
            ("events_for", lambda: self.registry.events_for(unknown_request_id), None),
            ("audio_chunks_for", lambda: self.registry.audio_chunks_for(unknown_request_id), None),
            (
                "accept",
                lambda: self.registry.accept(unknown_request_id, event_id="batch22-unknown-accept"),
                "batch22-unknown-accept",
            ),
            (
                "synthesis_started",
                lambda: self.registry.synthesis_started(
                    unknown_request_id,
                    event_id="batch22-unknown-started",
                ),
                "batch22-unknown-started",
            ),
            (
                "audio_produced",
                lambda: self.registry.audio_produced(
                    unknown_request_id,
                    event_id="batch22-unknown-produced",
                ),
                "batch22-unknown-produced",
            ),
            (
                "audio_enqueued",
                lambda: self.registry.audio_enqueued(
                    unknown_request_id,
                    event_id="batch22-unknown-enqueued",
                ),
                "batch22-unknown-enqueued",
            ),
            (
                "complete",
                lambda: self.registry.complete(unknown_request_id, event_id="batch22-unknown-complete"),
                "batch22-unknown-complete",
            ),
            (
                "fail",
                lambda: self.registry.fail(
                    unknown_request_id,
                    event_id="batch22-unknown-fail",
                    reason="unknown request",
                ),
                "batch22-unknown-fail",
            ),
            (
                "cancel",
                lambda: self.registry.cancel(
                    unknown_request_id,
                    event_id="batch22-unknown-cancel",
                    reason="unknown request",
                ),
                "batch22-unknown-cancel",
            ),
            ("record", lambda: self.registry.record(unknown_event), unknown_event.event_id),
            (
                "record_audio_chunk",
                lambda: self.registry.record_audio_chunk(unknown_chunk),
                unknown_chunk.event_id,
            ),
        )

        for operation_name, operation, event_id in operations:
            with self.subTest(operation=operation_name):
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
                    operation()
                self.assertEqual(self.registry._snapshots, before_snapshots)  # type: ignore[attr-defined]
                self.assertEqual(self.registry._event_ids, before_event_ids)  # type: ignore[attr-defined]
                self.assertEqual(self.registry._active_generations, before_generations)  # type: ignore[attr-defined]
                self.assertIs(self.registry.get(self.request.provider_request_id), known)
                self.assertIs(self.registry.events_for(self.request.provider_request_id), known_events)
                self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), known_chunks)
                if event_id is not None:
                    self.assertNotIn(event_id, self.registry._event_ids)  # type: ignore[attr-defined]

        accepted = self.registry.accept(
            self.request.provider_request_id,
            event_id="batch22-unknown-accept",
        )
        self.assertEqual(accepted.events[0].event_id, "batch22-unknown-accept")

    def test_cross_request_success_commits_survive_generation_and_shared_id_failures(self) -> None:
        second = self._prepare_second_request()
        generation_four = self._prepare_generation_four_request()
        self.registry.activate_generation("voicestudio", 3)

        first = self.registry.accept(
            self.request.provider_request_id,
            event_id="batch23-first-accepted",
        )
        second_accepted = self.registry.accept(
            second.provider_request_id,
            event_id="batch23-second-accepted",
        )
        first_events = self.registry.events_for(self.request.provider_request_id)
        first_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        second_events = self.registry.events_for(second.provider_request_id)
        second_chunks = self.registry.audio_chunks_for(second.provider_request_id)

        self.registry.advance_generation("voicestudio", 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.accept(
                generation_four.provider_request_id,
                event_id="batch23-first-accepted",
            )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.synthesis_started(
                second.provider_request_id,
                event_id="batch23-stale-reusable",
            )

        self.assertNotIn(generation_four.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), first)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), first_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), first_chunks)
        self.assertIs(self.registry.get(second.provider_request_id), second_accepted)
        self.assertIs(self.registry.events_for(second.provider_request_id), second_events)
        self.assertIs(self.registry.audio_chunks_for(second.provider_request_id), second_chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertNotIn("batch23-stale-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)

        generation_four_accepted = self.registry.accept(
            generation_four.provider_request_id,
            event_id="batch23-stale-reusable",
        )
        self.assertEqual(generation_four_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertEqual(
            tuple(event.event_id for event in generation_four_accepted.events),
            ("batch23-stale-reusable",),
        )
        self.assertIs(self.registry.get(self.request.provider_request_id), first)
        self.assertIs(self.registry.get(second.provider_request_id), second_accepted)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), first_events)
        self.assertIs(self.registry.events_for(second.provider_request_id), second_events)

    def test_stale_audio_id_reuse_and_cross_kind_collision_preserve_request_projections(self) -> None:
        generation_four = self._prepare_generation_four_request()
        self.registry.activate_generation("voicestudio", 3)
        self.registry.accept(self.request.provider_request_id, event_id="batch23-old-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch23-old-started")
        old_with_chunk = self.registry.record_audio_chunk(
            self._chunk(event_id="batch23-old-chunk")
        )
        old_events = self.registry.events_for(self.request.provider_request_id)
        old_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)

        self.registry.advance_generation("voicestudio", 4)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.record_audio_chunk(
                self._chunk(
                    event_id="batch23-stale-chunk-reusable",
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
            )

        self.assertIs(self.registry.get(self.request.provider_request_id), old_with_chunk)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), old_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), old_chunks)
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]
        self.assertNotIn("batch23-stale-chunk-reusable", self.registry._event_ids)  # type: ignore[attr-defined]

        future_accepted = self.registry.accept(
            generation_four.provider_request_id,
            event_id="batch23-stale-chunk-reusable",
        )
        future_started = self.registry.synthesis_started(
            generation_four.provider_request_id,
            event_id="batch23-future-started",
        )
        future_events = self.registry.events_for(generation_four.provider_request_id)
        future_chunks = self.registry.audio_chunks_for(generation_four.provider_request_id)
        committed_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]
        duplicate_chunk = self._chunk_for_request(
            generation_four,
            event_id="batch23-stale-chunk-reusable",
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.record_audio_chunk(duplicate_chunk)

        self.assertIs(self.registry.get(generation_four.provider_request_id), future_started)
        self.assertIs(self.registry.events_for(generation_four.provider_request_id), future_events)
        self.assertIs(self.registry.audio_chunks_for(generation_four.provider_request_id), future_chunks)
        self.assertEqual(self.registry._event_ids, committed_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), old_with_chunk)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), old_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), old_chunks)

        future_with_chunk = self.registry.record_audio_chunk(
            self._chunk_for_request(generation_four, event_id="batch23-future-chunk")
        )
        self.assertIs(future_with_chunk.events, future_events)
        self.assertEqual(
            tuple(event.event_id for event in future_accepted.events),
            ("batch23-stale-chunk-reusable",),
        )
        self.assertEqual(
            tuple(chunk.event_id for chunk in future_with_chunk.audio_chunks),
            ("batch23-future-chunk",),
        )
        self.assertIs(self.registry.get(self.request.provider_request_id), old_with_chunk)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
    def test_validation_priority_is_fail_closed_and_rejected_ids_remain_reusable(self) -> None:
        second = self._prepare_second_request()
        third = self._prepare_third_request()
        fourth = self._prepare_generation_four_request(
            provider_request_id="provider-request-batch24-fourth",
            segment_id="segment-batch24-fourth",
            request_sequence=2,
        )
        generation_four_retry = self._prepare_generation_four_request(
            provider_request_id="provider-request-batch24-fourth-retry",
            segment_id="segment-batch24-fourth-retry",
            request_sequence=3,
        )
        self.registry.activate_generation("voicestudio", 3)

        first = self.registry.accept(self.request.provider_request_id, event_id="batch24-committed")
        committed_event = first.events[-1]

        duplicate_unknown = replace(
            committed_event,
            provider_request_id="missing-request",
            reply_id="wrong-reply",
            provider_generation=99,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.record(duplicate_unknown)
        self.assertNotIn("missing-request", self.registry._snapshots)  # type: ignore[attr-defined]

        unknown_event = replace(
            committed_event,
            event_id="batch24-unknown-reusable",
            provider_request_id="missing-request",
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "未知 provider_request_id"):
            self.registry.record(unknown_event)
        self.assertNotIn("batch24-unknown-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        second_accepted = self.registry.accept(
            second.provider_request_id,
            event_id="batch24-unknown-reusable",
        )
        self.assertEqual(second_accepted.state, ProviderRequestLifecycleState.ACCEPTED)

        binding_then_generation = replace(
            committed_event,
            event_id="batch24-binding-reusable",
            provider_request_id=third.provider_request_id,
            reply_id="wrong-reply",
            provider_generation=2,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "reply_id"):
            self.registry.record(binding_then_generation)
        self.assertNotIn("batch24-binding-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        third_accepted = self.registry.accept(
            third.provider_request_id,
            event_id="batch24-binding-reusable",
        )
        self.assertEqual(third_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.registry.advance_generation("voicestudio", 4)

        generation_then_sequence = replace(
            committed_event,
            event_id="batch24-generation-reusable",
            event_sequence=99,
            provider_request_id=third.provider_request_id,
            reply_id=third.reply_id,
            segment_id=third.segment_id,
            provider_name=third.provider_name,
            provider_generation=third.provider_generation,
            voice_profile_id=third.voice_profile_id,
            voice_profile_version=third.voice_profile_version,
            audio_format=third.audio_format,
            capability_check=third.capability_check,
            kind=ProviderRequestLifecycleEventKind.ACCEPTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.record(generation_then_sequence)
        self.assertNotIn("batch24-generation-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        generation_four_retry_accepted = self.registry.accept(
            generation_four_retry.provider_request_id,
            event_id="batch24-generation-reusable",
        )
        self.assertEqual(generation_four_retry_accepted.state, ProviderRequestLifecycleState.ACCEPTED)

        sequence_then_state = replace(
            committed_event,
            event_id="batch24-sequence-reusable",
            event_sequence=99,
            provider_request_id=fourth.provider_request_id,
            reply_id=fourth.reply_id,
            segment_id=fourth.segment_id,
            provider_name=fourth.provider_name,
            provider_generation=fourth.provider_generation,
            voice_profile_version=fourth.voice_profile_version,
            audio_format=fourth.audio_format,
            capability_check=fourth.capability_check,
            kind=ProviderRequestLifecycleEventKind.SYNTHESIS_STARTED,
        )
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "非法生命周期事件序列"):
            self.registry.record(sequence_then_state)
        self.assertNotIn("batch24-sequence-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        fourth_accepted = self.registry.accept(
            fourth.provider_request_id,
            event_id="batch24-sequence-reusable",
        )
        self.assertEqual(fourth_accepted.state, ProviderRequestLifecycleState.ACCEPTED)

        self.assertIs(self.registry.get(self.request.provider_request_id), first)
        self.assertIs(self.registry.get(second.provider_request_id), second_accepted)
        self.assertEqual(self.registry.get(third.provider_request_id).state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertIs(self.registry.get(generation_four_retry.provider_request_id), generation_four_retry_accepted)
        self.assertIs(self.registry.get(fourth.provider_request_id), fourth_accepted)

    def test_cross_provider_generation_fences_are_isolated_and_failed_audio_ids_reuse_globally(self) -> None:
        other = self._prepare_other_provider_request()
        generation_four = self._prepare_generation_four_request()
        self.registry.activate_generation("voicestudio", 3)
        self.registry.activate_generation("edge_tts", 7)

        self.registry.accept(self.request.provider_request_id, event_id="batch24-voicestudio-accepted")
        old_started = self.registry.synthesis_started(
            self.request.provider_request_id,
            event_id="batch24-voicestudio-started",
        )
        old_with_chunk = self.registry.record_audio_chunk(
            self._chunk(event_id="batch24-voicestudio-chunk")
        )
        self.registry.accept(other.provider_request_id, event_id="batch24-edge-accepted")
        other_started = self.registry.synthesis_started(
            other.provider_request_id,
            event_id="batch24-edge-started",
        )
        other_with_chunk = self.registry.record_audio_chunk(
            self._chunk_for_request(other, event_id="batch24-edge-chunk")
        )
        old_events = self.registry.events_for(self.request.provider_request_id)
        old_chunks = self.registry.audio_chunks_for(self.request.provider_request_id)
        other_events = self.registry.events_for(other.provider_request_id)
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        self.registry.advance_generation("voicestudio", 4)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.record_audio_chunk(
                self._chunk_for_request(
                    self.request,
                    event_id="batch24-cross-provider-reusable",
                    audio_chunk_index=2,
                    receive_sequence=2,
                )
            )
        self.assertNotIn("batch24-cross-provider-reusable", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), old_with_chunk)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), old_events)
        self.assertIs(self.registry.audio_chunks_for(self.request.provider_request_id), old_chunks)
        self.assertIs(old_started.events, old_events)

        edge_with_second_chunk = self.registry.record_audio_chunk(
            self._chunk_for_request(
                other,
                event_id="batch24-cross-provider-reusable",
                audio_chunk_index=2,
                receive_sequence=2,
            )
        )
        self.assertIsNot(edge_with_second_chunk, other_with_chunk)
        self.assertEqual(
            tuple(chunk.event_id for chunk in edge_with_second_chunk.audio_chunks),
            ("batch24-edge-chunk", "batch24-cross-provider-reusable"),
        )
        self.assertIs(self.registry.events_for(other.provider_request_id), other_events)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "event_id 不得重复"):
            self.registry.accept(
                generation_four.provider_request_id,
                event_id="batch24-cross-provider-reusable",
            )
        self.assertNotIn(generation_four.provider_request_id, self.registry._snapshots)  # type: ignore[attr-defined]
        future_accepted = self.registry.accept(
            generation_four.provider_request_id,
            event_id="batch24-generation-four-accepted",
        )
        self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertIs(self.registry.get(self.request.provider_request_id), old_with_chunk)
        self.assertIs(self.registry.get(other.provider_request_id), edge_with_second_chunk)
        self.assertEqual(
            self.registry._event_ids,
            before_ids | {"batch24-cross-provider-reusable", "batch24-generation-four-accepted"},
        )  # type: ignore[attr-defined]
    def test_terminal_writes_and_generation_transitions_preserve_cross_provider_projection_identity(self) -> None:
        other = self._prepare_other_provider_request()
        failed_request = self._prepare_second_request()
        cancelled_request = self._prepare_third_request()
        generation_four = self._prepare_generation_four_request(
            provider_request_id="provider-request-batch25-generation-4",
            segment_id="segment-batch25-generation-4",
            request_sequence=2,
        )
        self.registry.activate_generation("voicestudio", 3)
        self.registry.activate_generation("edge_tts", 7)

        self.registry.accept(self.request.provider_request_id, event_id="batch25-completed-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch25-completed-started")
        self.registry.record_audio_chunk(self._chunk(event_id="batch25-completed-chunk"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="batch25-completed-produced")
        completed = self.registry.complete(self.request.provider_request_id, event_id="batch25-completed-terminal")

        failed = self.registry.fail(
            failed_request.provider_request_id,
            event_id="batch25-failed-terminal",
            reason="provider failed",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )
        cancelled = self.registry.cancel(
            cancelled_request.provider_request_id,
            event_id="batch25-cancelled-terminal",
            reason="user cancelled",
        )
        self.registry.accept(other.provider_request_id, event_id="batch25-edge-accepted")
        self.registry.synthesis_started(other.provider_request_id, event_id="batch25-edge-started")
        edge_started = self.registry.record_audio_chunk(
            self._chunk_for_request(other, event_id="batch25-edge-chunk")
        )

        projection_before_transition = {
            request.provider_request_id: (
                self.registry.get(request.provider_request_id),
                self.registry.events_for(request.provider_request_id),
                self.registry.audio_chunks_for(request.provider_request_id),
            )
            for request in (self.request, failed_request, cancelled_request, other)
        }
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.fail(
                self.request.provider_request_id,
                event_id="batch25-reusable-before-transition",
                reason="late failed",
                reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
            )
        self.assertNotIn("batch25-reusable-before-transition", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)

        self.registry.advance_generation("voicestudio", 4)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

        late_writes = (
            (
                self.request.provider_request_id,
                lambda: self.registry.fail(
                    self.request.provider_request_id,
                    event_id="batch25-stale-completed",
                    reason="stale completed failure",
                    reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                ),
            ),
            (
                failed_request.provider_request_id,
                lambda: self.registry.cancel(
                    failed_request.provider_request_id,
                    event_id="batch25-stale-failed",
                    reason="stale failed cancellation",
                ),
            ),
            (
                cancelled_request.provider_request_id,
                lambda: self.registry.accept(
                    cancelled_request.provider_request_id,
                    event_id="batch25-stale-cancelled",
                ),
            ),
        )
        for provider_request_id, operation in late_writes:
            with self.subTest(provider_request_id=provider_request_id):
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
                    operation()
        self.assertEqual(self.registry._event_ids, before_ids)  # type: ignore[attr-defined]

        for request in (self.request, failed_request, cancelled_request):
            before_snapshot, before_events, before_chunks = projection_before_transition[request.provider_request_id]
            self.assertIs(self.registry.get(request.provider_request_id), before_snapshot)
            self.assertIs(self.registry.events_for(request.provider_request_id), before_events)
            self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), before_chunks)
        self.assertIs(self.registry.get(other.provider_request_id), edge_started)
        self.assertIs(self.registry.events_for(other.provider_request_id), projection_before_transition[other.provider_request_id][1])
        self.assertIs(self.registry.audio_chunks_for(other.provider_request_id), projection_before_transition[other.provider_request_id][2])

        edge_produced = self.registry.audio_produced(other.provider_request_id, event_id="batch25-edge-produced")
        edge_completed = self.registry.complete(other.provider_request_id, event_id="batch25-edge-completed")
        self.assertEqual(edge_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertIsNot(edge_completed, edge_started)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

        future_accepted = self.registry.accept(
            generation_four.provider_request_id,
            event_id="batch25-stale-completed",
        )
        self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.get(failed_request.provider_request_id), failed)
        self.assertIs(self.registry.get(cancelled_request.provider_request_id), cancelled)
        self.assertIs(self.registry.get(other.provider_request_id), edge_completed)
        self.assertIs(self.registry.events_for(other.provider_request_id), edge_completed.events)
        self.assertIs(self.registry.audio_chunks_for(other.provider_request_id), edge_completed.audio_chunks)

        self.registry.advance_generation("voicestudio", 5)
        future_before_late = self.registry.get(generation_four.provider_request_id)
        future_events = self.registry.events_for(generation_four.provider_request_id)
        future_chunks = self.registry.audio_chunks_for(generation_four.provider_request_id)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.cancel(
                generation_four.provider_request_id,
                event_id="batch25-future-stale-cancel",
                reason="future generation superseded",
            )
        self.assertIs(self.registry.get(generation_four.provider_request_id), future_before_late)
        self.assertIs(self.registry.events_for(generation_four.provider_request_id), future_events)
        self.assertIs(self.registry.audio_chunks_for(generation_four.provider_request_id), future_chunks)
        self.assertEqual(self.registry._event_ids, before_ids | {
            "batch25-edge-produced",
            "batch25-edge-completed",
            "batch25-stale-completed",
        })  # type: ignore[attr-defined]
    def test_audio_enqueued_terminal_matrix_preserves_cross_provider_identity_after_generation_transition(self) -> None:
        other = self._prepare_other_provider_request()
        failed_request = self._prepare_second_request()
        cancelled_request = self._prepare_third_request()
        generation_four = self._prepare_generation_four_request(
            provider_request_id="provider-request-batch26-generation-4",
            segment_id="segment-batch26-generation-4",
            request_sequence=2,
        )
        self.registry.activate_generation("voicestudio", 3)
        self.registry.activate_generation("edge_tts", 7)

        self.registry.accept(self.request.provider_request_id, event_id="batch26-completed-accepted")
        self.registry.synthesis_started(self.request.provider_request_id, event_id="batch26-completed-started")
        self.registry.record_audio_chunk(self._chunk(event_id="batch26-completed-chunk"))
        self.registry.audio_produced(self.request.provider_request_id, event_id="batch26-completed-produced")
        self.registry.audio_enqueued(self.request.provider_request_id, event_id="batch26-completed-enqueued")
        completed = self.registry.complete(self.request.provider_request_id, event_id="batch26-completed-terminal")

        self.registry.accept(failed_request.provider_request_id, event_id="batch26-failed-accepted")
        self.registry.synthesis_started(failed_request.provider_request_id, event_id="batch26-failed-started")
        self.registry.record_audio_chunk(self._chunk_for_request(failed_request, event_id="batch26-failed-chunk"))
        self.registry.audio_produced(failed_request.provider_request_id, event_id="batch26-failed-produced")
        self.registry.audio_enqueued(failed_request.provider_request_id, event_id="batch26-failed-enqueued")
        failed = self.registry.fail(
            failed_request.provider_request_id,
            event_id="batch26-failed-terminal",
            reason="provider failed after enqueue",
            reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
        )

        self.registry.accept(cancelled_request.provider_request_id, event_id="batch26-cancelled-accepted")
        self.registry.synthesis_started(cancelled_request.provider_request_id, event_id="batch26-cancelled-started")
        self.registry.record_audio_chunk(self._chunk_for_request(cancelled_request, event_id="batch26-cancelled-chunk"))
        self.registry.audio_produced(cancelled_request.provider_request_id, event_id="batch26-cancelled-produced")
        self.registry.audio_enqueued(cancelled_request.provider_request_id, event_id="batch26-cancelled-enqueued")
        cancelled = self.registry.cancel(
            cancelled_request.provider_request_id,
            event_id="batch26-cancelled-terminal",
            reason="user cancelled after enqueue",
            reason_code=ProviderRequestLifecycleReasonCode.USER_CANCELLED,
        )

        self.registry.accept(other.provider_request_id, event_id="batch26-edge-accepted")
        self.registry.synthesis_started(other.provider_request_id, event_id="batch26-edge-started")
        self.registry.record_audio_chunk(self._chunk_for_request(other, event_id="batch26-edge-chunk"))
        self.registry.audio_produced(other.provider_request_id, event_id="batch26-edge-produced")
        edge_enqueued = self.registry.audio_enqueued(other.provider_request_id, event_id="batch26-edge-enqueued")

        projections_before_transition = {
            request.provider_request_id: (
                self.registry.get(request.provider_request_id),
                self.registry.events_for(request.provider_request_id),
                self.registry.audio_chunks_for(request.provider_request_id),
            )
            for request in (self.request, failed_request, cancelled_request, other)
        }
        before_ids = set(self.registry._event_ids)  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ProviderRequestLifecycleError, "终态"):
            self.registry.fail(
                self.request.provider_request_id,
                event_id="batch26-reusable-completed-late",
                reason="late completed failure",
                reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
            )
        self.assertNotIn("batch26-reusable-completed-late", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)

        self.registry.advance_generation("voicestudio", 4)
        self.assertEqual(self.registry.active_generation("voicestudio"), 4)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)

        late_writes = (
            (
                self.request.provider_request_id,
                lambda: self.registry.fail(
                    self.request.provider_request_id,
                    event_id="batch26-stale-completed",
                    reason="stale completed failure",
                    reason_code=ProviderRequestLifecycleReasonCode.PROVIDER_ERROR,
                ),
            ),
            (
                failed_request.provider_request_id,
                lambda: self.registry.cancel(
                    failed_request.provider_request_id,
                    event_id="batch26-stale-failed",
                    reason="stale failed cancellation",
                ),
            ),
            (
                cancelled_request.provider_request_id,
                lambda: self.registry.accept(
                    cancelled_request.provider_request_id,
                    event_id="batch26-stale-cancelled",
                ),
            ),
        )
        for provider_request_id, operation in late_writes:
            with self.subTest(provider_request_id=provider_request_id):
                with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"): 
                    operation()
        self.assertEqual(self.registry._event_ids, before_ids)

        edge_completed = self.registry.complete(other.provider_request_id, event_id="batch26-edge-completed")
        self.assertEqual(edge_completed.state, ProviderRequestLifecycleState.COMPLETED)
        self.assertTrue(edge_completed.audio_produced)
        self.assertTrue(edge_completed.audio_enqueued)
        self.assertEqual(edge_completed.events[:-1], edge_enqueued.events)
        self.assertIs(edge_completed.audio_chunks, edge_enqueued.audio_chunks)

        future_accepted = self.registry.accept(
            generation_four.provider_request_id,
            event_id="batch26-reusable-completed-late",
        )
        self.assertEqual(future_accepted.state, ProviderRequestLifecycleState.ACCEPTED)
        self.assertIs(self.registry.get(self.request.provider_request_id), completed)
        self.assertIs(self.registry.get(failed_request.provider_request_id), failed)
        self.assertIs(self.registry.get(cancelled_request.provider_request_id), cancelled)
        self.assertIs(self.registry.get(other.provider_request_id), edge_completed)

        future_events = self.registry.events_for(generation_four.provider_request_id)
        future_chunks = self.registry.audio_chunks_for(generation_four.provider_request_id)
        self.registry.advance_generation("voicestudio", 5)
        with self.assertRaisesRegex(ProviderRequestLifecycleError, "迟到"):
            self.registry.cancel(
                generation_four.provider_request_id,
                event_id="batch26-future-stale-cancel",
                reason="future generation superseded",
            )

        self.assertIs(self.registry.get(generation_four.provider_request_id), future_accepted)
        self.assertIs(self.registry.events_for(generation_four.provider_request_id), future_events)
        self.assertIs(self.registry.audio_chunks_for(generation_four.provider_request_id), future_chunks)
        for request in (self.request, failed_request, cancelled_request):
            before_snapshot, before_events, before_chunks = projections_before_transition[request.provider_request_id]
            self.assertIs(self.registry.get(request.provider_request_id), before_snapshot)
            self.assertIs(self.registry.events_for(request.provider_request_id), before_events)
            self.assertIs(self.registry.audio_chunks_for(request.provider_request_id), before_chunks)
        self.assertEqual(self.registry.active_generation("voicestudio"), 5)
        self.assertEqual(self.registry.active_generation("edge_tts"), 7)
if __name__ == "__main__":
    unittest.main()
