"""P7 Batch 75：只读路由绑定现有元数据。"""
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from app.tts import (
    ProviderRequestLifecycleState,
    TTSFallbackRouter,
    TTSRoute,
    TTSRoutingActionPlan,
    TTSRoutingActionStep,
    TTSRoutingActionPreflight,
    TTSRoutingPreflightState,
    TTSRoutingPreflightRejection,
    TTSRoutingRebuildInput,
    TTSRoutingRebuildConsistencyState,
    TTSRoutingRebuildObservationConsumer,
    TTSRoutingRebuildObservationPurpose,
    TTSRoutingRebuildObservationDisposition,
    TTSRoutingRebuildMismatch,
    TTSRoutingRebuildObservationReview,
    TTSRoutingRebuildObservationWindowReview,
    TTSRoutingRebuildObservationWindowSummary,
    TTSRoutingBinding,
    TTSRoutingCoordinator,
    TTSRoutingError,
)
from app.tts.playback.queue import PlaybackQueue
from tests.tts import test_playback_facts


class RoutingBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_playback_facts.ProviderRequestPlaybackTests(
            "test_playback_facts_progress_in_order_and_preserve_snapshot_identity"
        )
        fixture.setUp()
        self.fixture = fixture
        self.request = fixture.request
        self.queue = PlaybackQueue(fixture.lifecycle, fixture.registry)
        self.router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        self.coordinator = TTSRoutingCoordinator(self.router, fixture.lifecycle, fixture.registry, self.queue)
        self.router.start_reply(self.request.reply_id)

    def _failed(self):
        return self.coordinator.primary_failed(
            self.request.reply_id, self.request.segment_id, self.request.provider_request_id
        )


    def _rebuild_input_for(self, binding: TTSRoutingBinding) -> TTSRoutingRebuildInput:
        return TTSRoutingRebuildInput(
            provider_request_id=binding.provider_request_id,
            reply_id=binding.decision.reply_id,
            segment_id=binding.decision.segment_id,
            request_sequence=binding.request_sequence,
            provider_generation=binding.provider_generation,
            lifecycle_state=binding.lifecycle_state,
            playback_state=binding.playback_state,
            queue_pending_provider_request_ids=binding.queue_pending_provider_request_ids,
            queue_active_provider_request_id=binding.queue_active_provider_request_id,
            router_snapshot=self.router.snapshot,
        )

    def test_uncommitted_state_and_lifecycle_bound_without_mutation(self) -> None:
        before = self.fixture.lifecycle.get(self.request.provider_request_id)
        binding = self._failed()
        self.assertEqual(binding.decision.route, TTSRoute.EDGE)
        self.assertEqual(binding.playback_state.value, "NOT_COMMITTED")
        self.assertIs(binding.lifecycle_state, before.state)
        self.assertEqual(binding.provider_generation, self.request.provider_generation)
        self.assertFalse(binding.queued)
        self.assertIs(self.fixture.lifecycle.get(self.request.provider_request_id), before)
        with self.assertRaises(FrozenInstanceError):
            binding.provider_request_id = "changed"

    def test_committed_pending_switches_edge_but_does_not_clear_queue(self) -> None:
        entry = self.queue.enqueue(self.request.provider_request_id, audio_chunk_event_id="audio-chunk-1", audio_duration_ms=200)
        self.fixture.registry.commit(self.request.provider_request_id, event_id="binding-commit", audio_chunk_event_id="audio-chunk-1")
        binding = self._failed()
        self.assertEqual(binding.decision.route, TTSRoute.EDGE)
        self.assertEqual(binding.playback_state.value, "COMMITTED_NOT_STARTED")
        self.assertTrue(binding.queued)
        self.assertFalse(binding.active)
        self.assertEqual(self.queue.pending, (entry,))

    def test_started_active_switches_text_and_does_not_stop_device(self) -> None:
        entry = self.queue.enqueue(self.request.provider_request_id, audio_chunk_event_id="audio-chunk-1", audio_duration_ms=200)
        self.queue.begin()
        self.fixture.registry.commit(self.request.provider_request_id, event_id="binding-commit", audio_chunk_event_id="audio-chunk-1")
        self.fixture.registry.start(self.request.provider_request_id, event_id="binding-start", audio_chunk_event_id="audio-chunk-1")
        binding = self._failed()
        self.assertEqual(binding.decision.route, TTSRoute.TEXT)
        self.assertEqual(binding.playback_state.value, "PLAYBACK_STARTED")
        self.assertTrue(binding.active)
        self.assertEqual(binding.queue_active_provider_request_id, entry.provider_request_id)
        self.assertIs(self.queue.active, entry)

    def test_wrong_segment_and_unknown_request_fail_before_route_mutation(self) -> None:
        for segment, request in (("other-segment", self.request.provider_request_id), (self.request.segment_id, "unknown-request")):
            with self.assertRaises(TTSRoutingError):
                self.coordinator.primary_failed(self.request.reply_id, segment, request)
            self.assertEqual(self.router.snapshot.active_route, TTSRoute.VOICESTUDIO)

    def test_edge_failure_rejects_wrong_provider_before_route_mutation(self) -> None:
        self._failed()
        with self.assertRaises(TTSRoutingError):
            self.coordinator.edge_failed(self.request.reply_id, self.request.segment_id, self.request.provider_request_id)
        self.assertEqual(self.router.snapshot.active_route, TTSRoute.EDGE)

    def test_wrong_registry_binding_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            TTSRoutingCoordinator(self.router, self.fixture.lifecycle, self.fixture.registry, object())

    def test_uncommitted_binding_plans_lifecycle_then_queue_cancel(self) -> None:
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.assertIsInstance(plan, TTSRoutingActionPlan)
        self.assertEqual(plan.steps, (TTSRoutingActionStep.CANCEL_LIFECYCLE,))
        self.assertIsNone(plan.queue_cancel_reply_id)
        self.assertEqual(self.fixture.lifecycle.get(self.request.provider_request_id).state, ProviderRequestLifecycleState.SYNTHESIZING)
        self.assertEqual(self.queue.pending, ())

    def test_started_binding_plans_lifecycle_then_abort_then_queue_cancel(self) -> None:
        entry = self.queue.enqueue(self.request.provider_request_id, audio_chunk_event_id="audio-chunk-1", audio_duration_ms=200)
        self.queue.begin()
        self.fixture.registry.commit(self.request.provider_request_id, event_id="plan-commit", audio_chunk_event_id="audio-chunk-1")
        self.fixture.registry.start(self.request.provider_request_id, event_id="plan-start", audio_chunk_event_id="audio-chunk-1")
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding, lifecycle_cancel_reason="superseded", playback_abort_reason="route changed")
        self.assertEqual(
            plan.steps,
            (
                TTSRoutingActionStep.CANCEL_LIFECYCLE,
                TTSRoutingActionStep.ABORT_PLAYBACK,
                TTSRoutingActionStep.CANCEL_QUEUE,
            ),
        )
        self.assertEqual(plan.binding, binding)
        self.assertEqual(self.queue.active, entry)

    def test_terminal_binding_does_not_schedule_lifecycle_cancel_but_can_clear_queue(self) -> None:
        self.fixture.lifecycle.fail(
            self.request.provider_request_id,
            event_id="plan-failed",
            reason="provider failed",
        )
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.assertEqual(plan.steps, ())
        self.assertIsNone(plan.lifecycle_cancel_reason)
        self.assertIsNone(plan.queue_cancel_reply_id)

    def test_stale_binding_cannot_create_action_plan(self) -> None:
        binding = self._failed()
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="plan-stale-cancel",
            reason="already cancelled",
        )
        with self.assertRaises(TTSRoutingError):
            self.coordinator.plan_actions(binding)

    def test_action_plan_is_frozen_and_rejects_wrong_order(self) -> None:
        binding = self._failed()
        with self.assertRaises(TTSRoutingError):
            TTSRoutingActionPlan(
                binding=binding,
                steps=(TTSRoutingActionStep.CANCEL_QUEUE, TTSRoutingActionStep.CANCEL_LIFECYCLE),
                lifecycle_cancel_reason="cancel",
                playback_abort_reason=None,
                queue_cancel_reply_id=self.request.reply_id,
            )
        with self.assertRaises(FrozenInstanceError):
            self.coordinator.plan_actions(binding).steps = ()


    def test_preflight_ready_is_read_only_and_frozen(self) -> None:
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        before_lifecycle = self.fixture.lifecycle.get(self.request.provider_request_id)
        before_pending = self.queue.pending
        result = self.coordinator.preflight_actions(plan)
        self.assertIsInstance(result, TTSRoutingActionPreflight)
        self.assertIs(result.state, TTSRoutingPreflightState.READY)
        self.assertTrue(result.ready)
        self.assertFalse(result.requires_rebuild)
        self.assertIsNone(result.reason)
        self.assertIsNone(result.rejection)
        self.assertIsNone(result.rebuild_hint)
        self.assertIs(self.fixture.lifecycle.get(self.request.provider_request_id), before_lifecycle)
        self.assertEqual(self.queue.pending, before_pending)
        with self.assertRaises(FrozenInstanceError):
            result.reason = "changed"

    def test_rejected_preflight_exposes_current_rebuild_input_without_execution(self) -> None:
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-input-lifecycle-drift",
            reason="drift",
        )
        before_pending = self.queue.pending
        result = self.coordinator.preflight_actions(plan)
        self.assertIs(result.state, TTSRoutingPreflightState.REJECTED)
        self.assertIs(result.rejection, TTSRoutingPreflightRejection.LIFECYCLE_DRIFT)
        self.assertIsInstance(result.rebuild_input, TTSRoutingRebuildInput)
        rebuild_input = result.rebuild_input
        assert rebuild_input is not None
        self.assertEqual(rebuild_input.provider_request_id, self.request.provider_request_id)
        self.assertEqual(rebuild_input.reply_id, self.request.reply_id)
        self.assertEqual(rebuild_input.segment_id, self.request.segment_id)
        self.assertEqual(rebuild_input.request_sequence, self.request.request_sequence)
        self.assertEqual(rebuild_input.provider_generation, self.request.provider_generation)
        self.assertIs(rebuild_input.lifecycle_state, ProviderRequestLifecycleState.CANCELLED)
        self.assertEqual(rebuild_input.playback_state, binding.playback_state)
        self.assertEqual(rebuild_input.queue_pending_provider_request_ids, tuple())
        self.assertIsNone(rebuild_input.queue_active_provider_request_id)
        self.assertEqual(rebuild_input.router_snapshot.active_reply_id, self.request.reply_id)
        self.assertIs(rebuild_input.router_snapshot.active_route, TTSRoute.EDGE)
        self.assertEqual(self.queue.pending, before_pending)

    def test_ready_preflight_does_not_expose_rebuild_input(self) -> None:
        self.fixture.lifecycle.fail(
            self.request.provider_request_id,
            event_id="ready-no-rebuild-input",
            reason="provider failed",
        )
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        result = self.coordinator.preflight_actions(plan)
        self.assertIs(result.state, TTSRoutingPreflightState.READY)
        self.assertIsNone(result.rebuild_input)

    def test_preflight_rejects_lifecycle_drift_without_executing_actions(self) -> None:
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="preflight-lifecycle-drift",
            reason="drift",
        )
        result = self.coordinator.preflight_actions(plan)
        self.assertIs(result.state, TTSRoutingPreflightState.REJECTED)
        self.assertFalse(result.ready)
        self.assertTrue(result.requires_rebuild)
        self.assertIs(result.rejection, TTSRoutingPreflightRejection.LIFECYCLE_DRIFT)
        self.assertIn("binding", result.reason or "")
        self.assertIn("重新生成", result.rebuild_hint or "")

    def test_preflight_rejects_playback_drift(self) -> None:
        self.queue.enqueue(self.request.provider_request_id, audio_chunk_event_id="audio-chunk-1", audio_duration_ms=200)
        self.queue.begin()
        self.fixture.registry.commit(self.request.provider_request_id, event_id="preflight-commit", audio_chunk_event_id="audio-chunk-1")
        self.fixture.registry.start(self.request.provider_request_id, event_id="preflight-start", audio_chunk_event_id="audio-chunk-1")
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.fixture.registry.abort(
            self.request.provider_request_id,
            event_id="preflight-playback-drift",
            audio_chunk_event_id="audio-chunk-1",
            playback_offset_ms=0,
            reason="drift",
        )
        result = self.coordinator.preflight_actions(plan)
        self.assertIs(result.state, TTSRoutingPreflightState.REJECTED)
        self.assertIs(result.rejection, TTSRoutingPreflightRejection.PLAYBACK_DRIFT)
        self.assertTrue(result.requires_rebuild)
        self.assertIn("binding", result.reason or "")

    def test_preflight_rejects_queue_drift(self) -> None:
        self.queue.enqueue(self.request.provider_request_id, audio_chunk_event_id="audio-chunk-1", audio_duration_ms=200)
        self.fixture.registry.commit(self.request.provider_request_id, event_id="preflight-queue-commit", audio_chunk_event_id="audio-chunk-1")
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.queue.cancel(self.request.reply_id)
        result = self.coordinator.preflight_actions(plan)
        self.assertIs(result.state, TTSRoutingPreflightState.REJECTED)
        self.assertIs(result.rejection, TTSRoutingPreflightRejection.QUEUE_DRIFT)
        self.assertTrue(result.requires_rebuild)
        self.assertIn("queue", result.reason or "")

    def test_preflight_rejects_plan_drift_with_rebuild_hint(self) -> None:
        binding = self._failed()
        stale_plan = TTSRoutingActionPlan(
            binding=binding,
            steps=(),
            lifecycle_cancel_reason=None,
            playback_abort_reason=None,
            queue_cancel_reply_id=None,
        )
        result = self.coordinator.preflight_actions(stale_plan)
        self.assertIs(result.state, TTSRoutingPreflightState.REJECTED)
        self.assertIs(result.rejection, TTSRoutingPreflightRejection.PLAN_DRIFT)
        self.assertTrue(result.requires_rebuild)
        self.assertIsNotNone(result.rebuild_hint)

    def test_preflight_rejects_router_drift(self) -> None:
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        self.router.finish_reply(self.request.reply_id)
        result = self.coordinator.preflight_actions(plan)
        self.assertIs(result.state, TTSRoutingPreflightState.REJECTED)
        self.assertIs(result.rejection, TTSRoutingPreflightRejection.ROUTER_DRIFT)

    def test_empty_preflight_is_ready(self) -> None:
        self.fixture.lifecycle.fail(
            self.request.provider_request_id,
            event_id="preflight-empty-failed",
            reason="provider failed",
        )
        binding = self._failed()
        plan = self.coordinator.plan_actions(binding)
        result = self.coordinator.preflight_actions(plan)
        self.assertEqual(plan.steps, ())
        self.assertIs(result.state, TTSRoutingPreflightState.READY)
        self.assertIsNone(result.rejection)


    def test_rebuild_observation_consistent_is_read_only(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        result = self.coordinator.validate_rebuild_observation(rebuild_input, binding)

        self.assertIs(result.state, TTSRoutingRebuildConsistencyState.CONSISTENT)
        self.assertTrue(result.consistent)
        self.assertEqual(result.mismatches, ())
        self.assertIn("不执行任何动作", result.notice)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)

    def test_rebuild_observation_reads_current_facts_without_mutation(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        observed = self.coordinator.observe_rebuild_input(rebuild_input)
        result = self.coordinator.validate_rebuild_observation(rebuild_input, observed)

        self.assertEqual(observed, rebuild_input)
        self.assertIs(result.state, TTSRoutingRebuildConsistencyState.CONSISTENT)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)

    def test_rebuild_observation_consume_returns_read_only_retain_review(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = self.coordinator.observe_rebuild_input(rebuild_input)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        review = self.coordinator.consume_rebuild_observation(rebuild_input, observed)

        self.assertIsInstance(review, TTSRoutingRebuildObservationReview)
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY)
        self.assertTrue(review.consistent)
        self.assertFalse(review.requires_reobservation)
        self.assertIn("保留", review.notice)
        self.assertEqual(review.rebuild_input, rebuild_input)
        self.assertEqual(review.observed, observed)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)
        with self.assertRaises(FrozenInstanceError):
            review.disposition = TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE

    def test_rebuild_observation_consume_returns_discard_review_on_drift(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-consume-cancel",
            reason="consumer observed lifecycle drift",
        )
        observed = self.coordinator.observe_rebuild_input(rebuild_input)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        review = self.coordinator.consume_rebuild_observation(rebuild_input, observed)

        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertFalse(review.consistent)
        self.assertTrue(review.requires_reobservation)
        self.assertIn("丢弃", review.notice)
        self.assertIn("重新观察", review.notice)
        self.assertEqual(review.consistency.mismatches, (TTSRoutingRebuildMismatch.LIFECYCLE,))
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)

    def test_rebuild_observation_consume_does_not_store_or_execute_review(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = self.coordinator.observe_rebuild_input(rebuild_input)

        review = self.coordinator.consume_rebuild_observation(rebuild_input, observed)

        self.assertFalse(hasattr(self.coordinator, "rebuild_observation_review"))
        self.assertFalse(hasattr(self.coordinator, "_rebuild_observation_review"))
        self.assertIsNot(review, self.coordinator)
        self.assertIsInstance(review.consistency, type(self.coordinator.validate_rebuild_observation(rebuild_input, observed)))

    def test_rebuild_observation_review_rejects_forged_consistency(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = replace(rebuild_input, request_sequence=rebuild_input.request_sequence + 1)
        consistency = self.coordinator.validate_rebuild_observation(rebuild_input, rebuild_input)

        with self.assertRaises(TTSRoutingError):
            TTSRoutingRebuildObservationReview(
                rebuild_input=rebuild_input,
                observed=observed,
                consistency=consistency,
                disposition=TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY,
            )

    def test_consumption_of_old_observation_is_not_a_fresh_read(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = self.coordinator.observe_rebuild_input(rebuild_input)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-consume-after-observe",
            reason="fact changed after observation",
        )
        current_lifecycle = self.fixture.lifecycle.get(self.request.provider_request_id)

        review = self.coordinator.consume_rebuild_observation(rebuild_input, observed)

        self.assertTrue(review.consistent)
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY)
        self.assertIs(current_lifecycle.state, ProviderRequestLifecycleState.CANCELLED)
        self.assertIsNot(review.observed.lifecycle_state, current_lifecycle.state)
        self.assertTrue(review.notice)

    def test_rebuild_observation_window_retains_multiple_stable_observations(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        second = self.coordinator.observe_rebuild_input(rebuild_input)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second)
        )

        self.assertIsInstance(review, TTSRoutingRebuildObservationWindowReview)
        self.assertTrue(review.consistent)
        self.assertFalse(review.requires_reobservation)
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.RETAIN_READ_ONLY)
        self.assertEqual(review.observations, (first, second))
        self.assertEqual(len(review.consistencies), 2)
        self.assertIn("多个显式观察结果", review.notice)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)
        with self.assertRaises(FrozenInstanceError):
            review.disposition = TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE

    def test_rebuild_observation_window_discards_when_one_explicit_observation_drifts(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-window-cancel",
            reason="consumer observed lifecycle drift between windows",
        )
        second = self.coordinator.observe_rebuild_input(rebuild_input)

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second)
        )

        self.assertFalse(review.consistent)
        self.assertTrue(review.requires_reobservation)
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertTrue(review.consistencies[0].consistent)
        self.assertEqual(review.consistencies[1].mismatches, (TTSRoutingRebuildMismatch.LIFECYCLE,))
        self.assertIn("至少一个显式观察结果", review.notice)

    def test_rebuild_observation_window_requires_two_explicit_observations(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = self.coordinator.observe_rebuild_input(rebuild_input)

        with self.assertRaises(TTSRoutingError):
            self.coordinator.consume_rebuild_observation_window(rebuild_input, (observed,))
        with self.assertRaises(TTSRoutingError):
            self.coordinator.consume_rebuild_observation_window(rebuild_input, [observed, observed])  # type: ignore[arg-type]


    def test_rebuild_observation_window_exposes_stable_empty_reason_summary(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        second = self.coordinator.observe_rebuild_input(rebuild_input)

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second)
        )

        self.assertEqual(review.mismatches, ())
        self.assertIn("未发现不一致原因", review.mismatch_notice)

    def test_rebuild_observation_window_deduplicates_and_orders_reason_summary(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-window-reason-cancel",
            reason="consumer observed lifecycle drift",
        )
        second = self.coordinator.observe_rebuild_input(rebuild_input)
        drifted = replace(
            second,
            request_sequence=second.request_sequence + 1,
            playback_state=self.fixture.registry.get(self.request.provider_request_id).state,
            queue_pending_provider_request_ids=("other-request",),
        )

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second, drifted)
        )

        self.assertEqual(
            review.mismatches,
            (
                TTSRoutingRebuildMismatch.REQUEST_VERSION,
                TTSRoutingRebuildMismatch.LIFECYCLE,
                TTSRoutingRebuildMismatch.QUEUE,
            ),
        )
        self.assertIn("request_version、lifecycle、queue", review.mismatch_notice)
        self.assertEqual(review.mismatch_notice.count("lifecycle"), 1)

    def test_rebuild_observation_window_summary_is_minimal_and_frozen(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        second = self.coordinator.observe_rebuild_input(rebuild_input)

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second)
        )
        summary = review.summary

        self.assertIsInstance(summary, TTSRoutingRebuildObservationWindowSummary)
        self.assertEqual(summary.observation_count, 2)
        self.assertEqual(summary.mismatches, ())
        self.assertTrue(summary.consistent)
        self.assertFalse(summary.requires_reobservation)
        self.assertNotIn("rebuild_input", summary.__slots__)
        self.assertNotIn("reviews", summary.__slots__)
        with self.assertRaises(FrozenInstanceError):
            summary.observation_count = 3

    def test_rebuild_observation_window_summary_preserves_disposition_and_reasons(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-summary-cancel",
            reason="consumer observed lifecycle drift",
        )
        second = self.coordinator.observe_rebuild_input(rebuild_input)
        drifted = replace(
            second,
            request_sequence=second.request_sequence + 1,
            queue_pending_provider_request_ids=("other-request",),
        )

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second, drifted)
        )
        summary = review.summary

        self.assertEqual(summary.observation_count, 3)
        self.assertIs(summary.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertEqual(
            summary.mismatches,
            (
                TTSRoutingRebuildMismatch.REQUEST_VERSION,
                TTSRoutingRebuildMismatch.LIFECYCLE,
                TTSRoutingRebuildMismatch.QUEUE,
            ),
        )
        self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
        self.assertFalse(summary.consistent)
        self.assertTrue(summary.requires_reobservation)
        self.assertIn("丢弃旧输入", summary.notice)

    def test_rebuild_observation_window_summary_has_explicit_consumer_boundary(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        first = self.coordinator.observe_rebuild_input(rebuild_input)
        second = self.coordinator.observe_rebuild_input(rebuild_input)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        summary = self.coordinator.summarize_rebuild_observation_window(
            rebuild_input, (first, second)
        )

        self.assertIsInstance(summary, TTSRoutingRebuildObservationWindowSummary)
        self.assertIs(summary.consumer, TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY)
        self.assertEqual(summary.consumer.value, "summary_only")
        self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
        self.assertEqual(summary.purpose.value, "window_stability_review")
        self.assertEqual(summary, self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (first, second)
        ).summary)
        self.assertEqual(summary.observation_count, 2)
        self.assertTrue(summary.consistent)
        self.assertNotIn("rebuild_input", summary.__slots__)
        self.assertNotIn("reviews", summary.__slots__)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)

    def test_rebuild_observation_window_summary_purpose_is_fixed_and_read_only(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = self.coordinator.observe_rebuild_input(rebuild_input)
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending
        summary = self.coordinator.summarize_rebuild_observation_window(
            rebuild_input, (observed, observed)
        )

        with self.assertRaises(TTSRoutingError):
            replace(summary, purpose="execution")  # type: ignore[arg-type]
        with self.assertRaises(FrozenInstanceError):
            summary.purpose = TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW
        self.assertNotIn("rebuild_input", summary.__slots__)
        self.assertNotIn("reviews", summary.__slots__)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)

    def test_rebuild_observation_window_summary_consumer_keeps_window_input_contract(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = self.coordinator.observe_rebuild_input(rebuild_input)

        with self.assertRaises(TTSRoutingError):
            self.coordinator.summarize_rebuild_observation_window(rebuild_input, (observed,))
        with self.assertRaises(TTSRoutingError):
            self.coordinator.summarize_rebuild_observation_window(rebuild_input, [observed, observed])  # type: ignore[arg-type]
        with self.assertRaises(TTSRoutingError):
            self.coordinator.summarize_rebuild_observation_window(rebuild_input, (observed, object()))  # type: ignore[arg-type]


    def test_rebuild_observation_playback_boundary_discards_without_leaking_or_mutating(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = replace(
            rebuild_input,
            playback_state=type(rebuild_input.playback_state).PLAYBACK_STARTED,
        )
        before = (
            self.router.snapshot,
            self.queue.pending,
            self.fixture.lifecycle.get(self.request.provider_request_id),
            self.fixture.registry.get(self.request.provider_request_id),
        )

        consistency = self.coordinator.validate_rebuild_observation(rebuild_input, observed)
        self.assertIs(consistency.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(consistency.mismatches, (TTSRoutingRebuildMismatch.PLAYBACK,))

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        summary = self.coordinator.summarize_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertEqual(summary, review.summary)
        self.assertEqual(summary.mismatches, (TTSRoutingRebuildMismatch.PLAYBACK,))
        self.assertEqual(summary.observation_count, 2)
        self.assertIs(summary.consumer, TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY)
        self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
        self.assertNotIn(observed.playback_state.value, summary.notice)
        self.assertNotIn('observed', summary.__slots__)
        self.assertNotIn('rebuild_input', summary.__slots__)
        self.assertNotIn('reviews', summary.__slots__)
        self.assertEqual(
            (
                self.router.snapshot,
                self.queue.pending,
                self.fixture.lifecycle.get(self.request.provider_request_id),
                self.fixture.registry.get(self.request.provider_request_id),
            ),
            before,
        )
    def test_rebuild_observation_frozen_router_snapshot_boundary_discards_without_leaking_or_mutating(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        foreign_reply_id = "router-foreign-reply"
        observed = replace(
            rebuild_input,
            router_snapshot=replace(rebuild_input.router_snapshot, active_reply_id=foreign_reply_id),
        )
        self.assertEqual(replace(observed, router_snapshot=rebuild_input.router_snapshot), rebuild_input)
        before = (
            self.router.snapshot,
            self.queue.pending,
            self.queue.active,
            self.fixture.lifecycle.get(self.request.provider_request_id),
            self.fixture.registry.get(self.request.provider_request_id),
        )

        consistency = self.coordinator.validate_rebuild_observation(rebuild_input, observed)
        self.assertIs(consistency.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(consistency.mismatches, (TTSRoutingRebuildMismatch.ROUTER,))

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        summary = self.coordinator.summarize_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertEqual(summary, review.summary)
        self.assertEqual(summary.mismatches, (TTSRoutingRebuildMismatch.ROUTER,))
        self.assertEqual(summary.observation_count, 2)
        self.assertIs(summary.consumer, TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY)
        self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
        self.assertNotIn(foreign_reply_id, summary.notice)
        self.assertNotIn("observed", summary.__slots__)
        self.assertNotIn("rebuild_input", summary.__slots__)
        self.assertNotIn("reviews", summary.__slots__)
        self.assertEqual(
            (
                self.router.snapshot,
                self.queue.pending,
                self.queue.active,
                self.fixture.lifecycle.get(self.request.provider_request_id),
                self.fixture.registry.get(self.request.provider_request_id),
            ),
            before,
        )
    def test_rebuild_observation_queue_boundary_discards_without_leaking_or_mutating(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = replace(
            rebuild_input,
            queue_pending_provider_request_ids=("queue-foreign-pending",),
            queue_active_provider_request_id="queue-foreign-active",
        )
        before = (
            self.router.snapshot,
            self.queue.pending,
            self.fixture.lifecycle.get(self.request.provider_request_id),
            self.fixture.registry.get(self.request.provider_request_id),
        )

        consistency = self.coordinator.validate_rebuild_observation(rebuild_input, observed)
        self.assertIs(consistency.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(consistency.mismatches, (TTSRoutingRebuildMismatch.QUEUE,))

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        summary = self.coordinator.summarize_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertEqual(summary, review.summary)
        self.assertEqual(summary.mismatches, (TTSRoutingRebuildMismatch.QUEUE,))
        self.assertEqual(summary.observation_count, 2)
        self.assertIs(summary.consumer, TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY)
        self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
        self.assertNotIn("queue-foreign-pending", summary.notice)
        self.assertNotIn("queue-foreign-active", summary.notice)
        self.assertNotIn("observed", summary.__slots__)
        self.assertNotIn("rebuild_input", summary.__slots__)
        self.assertNotIn("reviews", summary.__slots__)
        self.assertEqual(
            (
                self.router.snapshot,
                self.queue.pending,
                self.fixture.lifecycle.get(self.request.provider_request_id),
                self.fixture.registry.get(self.request.provider_request_id),
            ),
            before,
        )

    def test_rebuild_observation_version_boundary_discards_without_leaking_or_mutating(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        before = (
            self.router.snapshot,
            self.queue.pending,
            self.fixture.lifecycle.get(self.request.provider_request_id),
            self.fixture.registry.get(self.request.provider_request_id),
        )

        for changes in (
            {"request_sequence": 720031},
            {"provider_generation": 830047},
            {"request_sequence": 720031, "provider_generation": 830047},
        ):
            with self.subTest(changes=changes):
                observed = replace(rebuild_input, **changes)
                consistency = self.coordinator.validate_rebuild_observation(rebuild_input, observed)
                self.assertIs(consistency.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
                self.assertEqual(consistency.mismatches, (TTSRoutingRebuildMismatch.REQUEST_VERSION,))

                review = self.coordinator.consume_rebuild_observation_window(
                    rebuild_input, (rebuild_input, observed)
                )
                summary = self.coordinator.summarize_rebuild_observation_window(
                    rebuild_input, (rebuild_input, observed)
                )
                self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
                self.assertEqual(summary, review.summary)
                self.assertEqual(summary.mismatches, (TTSRoutingRebuildMismatch.REQUEST_VERSION,))
                self.assertEqual(summary.observation_count, 2)
                self.assertIs(summary.consumer, TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY)
                self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
                for value in changes.values():
                    self.assertNotIn(str(value), summary.notice)
                self.assertEqual(
                    (
                        self.router.snapshot,
                        self.queue.pending,
                        self.fixture.lifecycle.get(self.request.provider_request_id),
                        self.fixture.registry.get(self.request.provider_request_id),
                    ),
                    before,
                )

    def test_rebuild_observation_identity_and_version_boundary_aggregates_without_leaking_or_mutating(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = replace(
            rebuild_input,
            provider_request_id="composite-foreign-request",
            reply_id="composite-foreign-reply",
            segment_id="composite-foreign-segment",
            request_sequence=rebuild_input.request_sequence + 1,
            provider_generation=rebuild_input.provider_generation + 1,
        )
        before = (
            self.router.snapshot,
            self.queue.pending,
            self.fixture.lifecycle.get(self.request.provider_request_id),
            self.fixture.registry.get(self.request.provider_request_id),
        )

        consistency = self.coordinator.validate_rebuild_observation(rebuild_input, observed)
        self.assertIs(consistency.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(
            consistency.mismatches,
            (
                TTSRoutingRebuildMismatch.REQUEST_IDENTITY,
                TTSRoutingRebuildMismatch.REQUEST_VERSION,
            ),
        )

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        summary = self.coordinator.summarize_rebuild_observation_window(
            rebuild_input, (rebuild_input, observed)
        )
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertEqual(summary, review.summary)
        self.assertEqual(summary.mismatches, consistency.mismatches)
        self.assertEqual(summary.observation_count, 2)
        self.assertIs(summary.consumer, TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY)
        self.assertIs(summary.purpose, TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW)
        for value in (
            "composite-foreign-request",
            "composite-foreign-reply",
            "composite-foreign-segment",
            observed.request_sequence,
            observed.provider_generation,
        ):
            self.assertNotIn(str(value), summary.notice)
        self.assertEqual(
            (
                self.router.snapshot,
                self.queue.pending,
                self.fixture.lifecycle.get(self.request.provider_request_id),
                self.fixture.registry.get(self.request.provider_request_id),
            ),
            before,
        )

    def test_rebuild_observation_identity_boundary_discards_without_leaking_or_mutating(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        foreign = replace(
            rebuild_input,
            provider_request_id="foreign-request",
            reply_id="foreign-reply",
            segment_id="foreign-segment",
        )
        before = (
            self.router.snapshot,
            self.queue.pending,
            self.fixture.lifecycle.get(self.request.provider_request_id),
            self.fixture.registry.get(self.request.provider_request_id),
        )

        consistency = self.coordinator.validate_rebuild_observation(rebuild_input, foreign)
        self.assertIs(consistency.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(consistency.mismatches, (TTSRoutingRebuildMismatch.REQUEST_IDENTITY,))

        review = self.coordinator.consume_rebuild_observation_window(
            rebuild_input, (rebuild_input, foreign)
        )
        self.assertIs(review.disposition, TTSRoutingRebuildObservationDisposition.DISCARD_AND_REOBSERVE)
        self.assertEqual(review.summary.mismatches, (TTSRoutingRebuildMismatch.REQUEST_IDENTITY,))
        self.assertNotIn("foreign-request", review.summary.notice)
        self.assertNotIn("foreign-reply", review.summary.notice)
        self.assertNotIn("foreign-segment", review.summary.notice)
        self.assertEqual(
            (
                self.router.snapshot,
                self.queue.pending,
                self.fixture.lifecycle.get(self.request.provider_request_id),
                self.fixture.registry.get(self.request.provider_request_id),
            ),
            before,
        )

    def test_rebuild_observation_reads_lifecycle_drift_as_current_fact(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        self.fixture.lifecycle.cancel(
            self.request.provider_request_id,
            event_id="rebuild-observe-cancel",
            reason="observed lifecycle drift",
        )

        observed = self.coordinator.observe_rebuild_input(rebuild_input)
        result = self.coordinator.validate_rebuild_observation(rebuild_input, observed)

        self.assertIs(result.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(result.mismatches, (TTSRoutingRebuildMismatch.LIFECYCLE,))
        self.assertIn("重新读取本地事实", result.notice)

    def test_rebuild_observation_reports_local_binding_drift_without_actions(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        observed = replace(
            binding,
            request_sequence=binding.request_sequence + 1,
            lifecycle_state=ProviderRequestLifecycleState.CANCELLED,
            queue_pending_provider_request_ids=("other-request",),
        )
        before_snapshot = self.router.snapshot
        before_pending = self.queue.pending

        result = self.coordinator.validate_rebuild_observation(rebuild_input, observed)

        self.assertIs(result.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertFalse(result.consistent)
        self.assertEqual(
            result.mismatches,
            (
                TTSRoutingRebuildMismatch.REQUEST_VERSION,
                TTSRoutingRebuildMismatch.LIFECYCLE,
                TTSRoutingRebuildMismatch.QUEUE,
            ),
        )
        self.assertIn("不得继续沿用旧计划", result.notice)
        self.assertEqual(self.router.snapshot, before_snapshot)
        self.assertEqual(self.queue.pending, before_pending)

    def test_rebuild_observation_reports_router_drift_without_actions(self) -> None:
        binding = self._failed()
        rebuild_input = self._rebuild_input_for(binding)
        self.router.finish_reply(self.request.reply_id)

        result = self.coordinator.validate_rebuild_observation(rebuild_input, binding)

        self.assertIs(result.state, TTSRoutingRebuildConsistencyState.INCONSISTENT)
        self.assertEqual(result.mismatches, (TTSRoutingRebuildMismatch.ROUTER,))
        self.assertIn("重新读取本地事实", result.notice)

if __name__ == "__main__":
    unittest.main()
