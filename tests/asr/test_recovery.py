from __future__ import annotations

import unittest

from app.asr import (
    AsrFailure,
    AsrFailureCode,
    AsrFailureStage,
    AsrInputMode,
    AsrRecoveryConfig,
    AsrRecoveryController,
    AsrRecoveryState,
    AsrSessionBoundary,
    AsrTransitionDecision,
)


class CleanupRecorder:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def __call__(self, reason: str) -> None:
        self.reasons.append(reason)


class AsrRecoveryControllerTests(unittest.TestCase):
    def make_controller(self, *, required_probe_passes: int = 2) -> tuple[AsrRecoveryController, CleanupRecorder]:
        cleanup = CleanupRecorder()
        controller = AsrRecoveryController(
            AsrRecoveryConfig(required_probe_passes=required_probe_passes),
            discard_resources=cleanup,
        )
        return controller, cleanup

    def startup_failure(self, code: AsrFailureCode = AsrFailureCode.PROVIDER_UNAVAILABLE) -> AsrFailure:
        return AsrFailure(
            stage=AsrFailureStage.STARTUP,
            code=code,
            occurred_at="2026-09-21T10:00:00Z",
        )

    def session_failure(self, code: AsrFailureCode = AsrFailureCode.TIMEOUT) -> AsrFailure:
        return AsrFailure(
            stage=AsrFailureStage.SESSION,
            code=code,
            occurred_at="2026-09-21T10:01:00Z",
        )

    def runtime_failure(self, code: AsrFailureCode = AsrFailureCode.STREAM_FAILED) -> AsrFailure:
        return AsrFailure(
            stage=AsrFailureStage.RUNTIME,
            code=code,
            occurred_at="2026-09-21T10:02:00Z",
        )

    def boundary(self, suffix: str = "1") -> AsrSessionBoundary:
        return AsrSessionBoundary(
            session_id=f"session-{suffix}",
            audio_session_id=f"audio-{suffix}",
            provider_generation=int(suffix),
            stream_id=f"stream-{suffix}",
            utterance_id=f"utterance-{suffix}",
        )

    def test_startup_failure_is_bounded_text_fallback_and_sanitized(self) -> None:
        controller, cleanup = self.make_controller()
        result = controller.startup_failed(self.startup_failure())
        self.assertEqual(result.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.UNAVAILABLE_STARTUP)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertEqual(controller.public_status, "语音识别不可用：provider_unavailable；当前为文字输入")
        self.assertEqual(cleanup.reasons, ["startup_failure"])
        self.assertIsNone(controller.active_boundary)

    def test_startup_timeout_does_not_retry_or_cleanup_twice(self) -> None:
        controller, cleanup = self.make_controller()
        first = controller.startup_failed(self.startup_failure(AsrFailureCode.TIMEOUT))
        second = controller.startup_failed(self.startup_failure(AsrFailureCode.TIMEOUT))
        self.assertEqual(first.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(second.decision, AsrTransitionDecision.NOOP)
        self.assertEqual(cleanup.reasons, ["startup_failure"])
        self.assertEqual(controller.state, AsrRecoveryState.UNAVAILABLE_STARTUP)

    def test_startup_ready_keeps_text_mode_until_explicit_voice_request(self) -> None:
        controller, cleanup = self.make_controller()
        result = controller.startup_succeeded()
        self.assertEqual(result.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.READY)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertEqual(cleanup.reasons, [])

    def test_warmup_failure_or_cancel_discards_attempt_and_stays_text(self) -> None:
        controller, cleanup = self.make_controller()
        controller.startup_succeeded()
        self.assertEqual(controller.begin_session_warmup().decision, AsrTransitionDecision.ACCEPTED)
        failed = controller.session_warmup_failed(self.session_failure())
        self.assertEqual(failed.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.COOLDOWN)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertEqual(cleanup.reasons, ["session_warmup_failure"])
        self.assertEqual(controller.cancel_session_warmup().decision, AsrTransitionDecision.NOOP)
        self.assertEqual(cleanup.reasons, ["session_warmup_failure"])

    def test_active_warmup_cancel_discards_attempt_and_stays_text(self) -> None:
        controller, cleanup = self.make_controller()
        controller.startup_succeeded()
        controller.begin_session_warmup()
        cancelled = controller.cancel_session_warmup()
        self.assertEqual(cancelled.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.COOLDOWN)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertIsNone(controller.active_boundary)
        self.assertEqual(cleanup.reasons, ["session_warmup_cancelled"])
        self.assertEqual(controller.cancel_session_warmup().decision, AsrTransitionDecision.NOOP)
        self.assertEqual(cleanup.reasons, ["session_warmup_cancelled"])
    def test_warmup_success_activates_only_fresh_boundary(self) -> None:
        controller, cleanup = self.make_controller()
        first = self.boundary("1")
        controller.startup_succeeded()
        controller.begin_session_warmup()
        self.assertEqual(controller.session_warmup_succeeded(first).decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.SESSION_ACTIVE)
        self.assertEqual(controller.input_mode, AsrInputMode.VOICE)
        self.assertEqual(controller.active_boundary, first)
        controller.runtime_failed(self.runtime_failure())
        self.assertEqual(cleanup.reasons, ["runtime_failure"])
        controller.begin_probe()
        controller.record_probe(True)
        controller.record_probe(True)
        controller.request_voice_switch(safe_idle=True)
        controller.begin_session_warmup()
        reused = controller.session_warmup_succeeded(first)
        self.assertEqual(reused.decision, AsrTransitionDecision.REJECTED)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertIsNone(controller.active_boundary)
        self.assertEqual(cleanup.reasons, ["runtime_failure", "voice_switch_prepare", "stale_session_boundary"])

    def test_runtime_failure_discards_current_turn_and_never_reopens_it(self) -> None:
        controller, cleanup = self.make_controller()
        controller.startup_succeeded()
        controller.begin_session_warmup()
        controller.session_warmup_succeeded(self.boundary("1"))
        first = controller.runtime_failed(self.runtime_failure())
        second = controller.runtime_failed(self.runtime_failure())
        self.assertEqual(first.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(second.decision, AsrTransitionDecision.NOOP)
        self.assertEqual(controller.state, AsrRecoveryState.SESSION_FAILED_RUNTIME)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertIsNone(controller.active_boundary)
        self.assertEqual(cleanup.reasons, ["runtime_failure"])

    def test_probe_requires_consecutive_successes_and_never_auto_switches(self) -> None:
        controller, cleanup = self.make_controller(required_probe_passes=2)
        controller.startup_failed(self.startup_failure())
        self.assertEqual(controller.begin_probe().decision, AsrTransitionDecision.ACCEPTED)
        one = controller.record_probe(True)
        self.assertEqual(one.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.PROBING)
        reset = controller.record_probe(False)
        self.assertEqual(reset.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.COOLDOWN)
        controller.begin_probe()
        controller.record_probe(True)
        recovered = controller.record_probe(True)
        self.assertEqual(recovered.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.RECOVERED_PENDING_USER)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertEqual(controller.public_status, "语音识别已恢复，可以切回语音输入。")
        self.assertEqual(cleanup.reasons, ["startup_failure"])

    def test_voice_switch_is_deferred_until_safe_idle_and_still_requires_warmup(self) -> None:
        controller, _ = self.make_controller(required_probe_passes=1)
        controller.startup_failed(self.startup_failure())
        controller.begin_probe()
        controller.record_probe(True)
        deferred = controller.request_voice_switch(safe_idle=False)
        self.assertEqual(deferred.decision, AsrTransitionDecision.DEFERRED)
        self.assertTrue(controller.voice_switch_pending)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        ready = controller.reach_safe_idle()
        self.assertEqual(ready.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.READY)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertFalse(controller.voice_switch_pending)
        self.assertEqual(controller.begin_session_warmup().decision, AsrTransitionDecision.ACCEPTED)

    def test_cleanup_failure_cannot_keep_voice_active(self) -> None:
        def broken_cleanup(_reason: str) -> None:
            raise OSError("sensitive temporary path")

        controller = AsrRecoveryController(
            AsrRecoveryConfig(required_probe_passes=1),
            discard_resources=broken_cleanup,
        )
        controller.startup_succeeded()
        controller.begin_session_warmup()
        controller.session_warmup_succeeded(self.boundary("1"))
        result = controller.runtime_failed(self.runtime_failure())
        self.assertEqual(result.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.SESSION_FAILED_RUNTIME)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertIsNone(controller.active_boundary)
        self.assertTrue(controller.cleanup_failed)
        self.assertNotIn("sensitive", controller.public_status)

    def test_probe_failure_is_quiet_and_remains_text_only(self) -> None:
        controller, cleanup = self.make_controller(required_probe_passes=1)
        controller.startup_failed(self.startup_failure())
        controller.begin_probe()
        failed = controller.record_probe(False)
        self.assertEqual(failed.decision, AsrTransitionDecision.ACCEPTED)
        self.assertEqual(controller.state, AsrRecoveryState.COOLDOWN)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertEqual(controller.public_status, "语音识别不可用；当前为文字输入")
        self.assertEqual(cleanup.reasons, ["startup_failure"])

    def test_invalid_transitions_fail_closed_without_resource_actions(self) -> None:
        controller, cleanup = self.make_controller()
        self.assertEqual(controller.begin_session_warmup().decision, AsrTransitionDecision.REJECTED)
        self.assertEqual(controller.runtime_failed(self.runtime_failure()).decision, AsrTransitionDecision.REJECTED)
        self.assertEqual(controller.record_probe(True).decision, AsrTransitionDecision.REJECTED)
        self.assertEqual(controller.request_voice_switch(safe_idle=True).decision, AsrTransitionDecision.REJECTED)
        self.assertEqual(controller.state, AsrRecoveryState.STARTUP_LOADING)
        self.assertEqual(controller.input_mode, AsrInputMode.TEXT)
        self.assertEqual(cleanup.reasons, [])


if __name__ == "__main__":
    unittest.main()