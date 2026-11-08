"""P10 批次 R1b：两阶段打断编排语义的离线测试。

覆盖：疑似插话降音量与重复降音量幂等、误触发按 ``restore_ms`` **平滑**恢复、
确认后音量归零/禁止旧回复继续提交/交出同一 utterance 的前置缓冲、安全确认提示被
打断即失效且插话不产生授权，以及纯离线边界（不导入设备/文件/网络/安全链）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.contracts.base import ContractError
from app.duplex.barge_in import ACTION_RESTORE, BargeInObservation, BargeInState
from app.duplex.barge_in_orchestration import (
    OUTCOME_REASON_CONFIRMED,
    OUTCOME_REASON_DUCKING,
    OUTCOME_REASON_ENDED,
    OUTCOME_REASON_PLAYBACK,
    OUTCOME_REASON_RESTORING,
    PROMPT_REASON_BARGE_IN,
    PROMPT_REASON_REPLAY_INCOMPLETE,
    PROMPT_STATE_COMPLETED,
    PROMPT_STATE_INVALIDATED,
    PROMPT_STATE_VALID,
    BargeInOrchestrationError,
    BargeInPlaybackController,
    PromptPolicy,
)

FRAME_MS = 20
QUIET = 0.05
VOICE = 0.95


def frame(index: int, *, voice: float = QUIET, playback_active: bool = True, queued_ms: int = 0):
    return BargeInObservation(
        at_ms=index * FRAME_MS,
        voice_probability=voice,
        playback_active=playback_active,
        playback_queued_ms=queued_ms,
    )


class PromptPolicyTests(unittest.TestCase):
    def test_default_policy_keeps_authorization_path_open(self) -> None:
        policy = PromptPolicy()
        self.assertEqual(policy.state, PROMPT_STATE_VALID)
        self.assertFalse(policy.replay_required)
        self.assertTrue(policy.authorization_possible)

    def test_invalidated_policy_blocks_authorization(self) -> None:
        policy = PromptPolicy(
            state=PROMPT_STATE_INVALIDATED, replay_required=True, reason=PROMPT_REASON_BARGE_IN
        )
        self.assertFalse(policy.authorization_possible)

    def test_completed_policy_does_not_require_replay(self) -> None:
        policy = PromptPolicy(state=PROMPT_STATE_COMPLETED)
        self.assertFalse(policy.replay_required)

    def test_unknown_prompt_state_is_rejected(self) -> None:
        with self.assertRaises(BargeInOrchestrationError):
            PromptPolicy(state="half_spoken")

    def test_replay_required_must_be_boolean(self) -> None:
        with self.assertRaises(ContractError):
            PromptPolicy(replay_required=1)  # type: ignore[arg-type]

    def test_details_are_short_codes_only(self) -> None:
        details = PromptPolicy().to_details()
        self.assertEqual(details["state"], PROMPT_STATE_VALID)
        self.assertIn("authorization_possible", details)
        for name in ("authorization", "confirmation_id", "phrase"):
            self.assertNotIn(name, details)


class PlaybackVolumeTests(unittest.TestCase):
    def test_quiet_playback_stays_at_full_volume(self) -> None:
        controller = BargeInPlaybackController()
        outcome = controller.play_frame(0, frame(0))
        self.assertEqual(outcome.level, 1.0)
        self.assertTrue(outcome.continue_playback)
        self.assertFalse(outcome.ducking)
        self.assertEqual(outcome.reason, OUTCOME_REASON_PLAYBACK)

    def test_first_voiced_frame_ducks_to_target(self) -> None:
        controller = BargeInPlaybackController()
        outcome = controller.play_frame(0, frame(0, voice=VOICE))
        self.assertIs(outcome.state, BargeInState.SUSPECTED)
        self.assertEqual(outcome.level, controller.config.duck_fraction)
        self.assertEqual(outcome.target_level, controller.config.duck_fraction)
        self.assertEqual(outcome.reason, OUTCOME_REASON_DUCKING)
        self.assertTrue(outcome.continue_playback)

    def test_repeated_suspected_frames_keep_duck_stable(self) -> None:
        controller = BargeInPlaybackController()
        levels = [
            controller.play_frame(index, frame(index, voice=VOICE)).level
            for index in range(5)
        ]
        self.assertEqual(len(set(levels)), 1)
        self.assertEqual(levels[0], controller.config.duck_fraction)

    def test_duration_follows_frame_number(self) -> None:
        controller = BargeInPlaybackController()
        outcome = controller.play_frame(7, frame(7))
        self.assertEqual(outcome.duration_ms, 7 * FRAME_MS)

    def test_negative_frame_number_is_rejected(self) -> None:
        controller = BargeInPlaybackController()
        with self.assertRaises(ContractError):
            controller.play_frame(-1, frame(0))

    def test_observation_type_is_enforced(self) -> None:
        controller = BargeInPlaybackController()
        with self.assertRaises(ContractError):
            controller.play_frame(0, object())  # type: ignore[arg-type]

    def test_outcome_details_expose_only_short_codes_and_numbers(self) -> None:
        controller = BargeInPlaybackController()
        details = controller.play_frame(0, frame(0, voice=VOICE)).to_details()
        for value in details.values():
            self.assertIsInstance(value, (str, int, float, bool, dict, type(None)))
        self.assertIn("resume_reply_submission", details)
        self.assertIn("prompt", details)


class RestoreCurveTests(unittest.TestCase):
    """疑似 → 回落 → 误触发恢复的时间线（帧编号 × 20 ms）。

    0 帧：疑似降音量；1~19 帧（受 ``release_ms=400`` 滞后保护）保持降音量；
    20 帧起按 ``restore_ms=200`` 平滑回升；29 帧回到原音量。
    """

    def test_false_trigger_restores_smoothly_over_restore_ms(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        levels = [controller.play_frame(index, frame(index)).level for index in range(1, 30)]
        self.assertEqual(levels[-1], 1.0)
        # 滞后保护期（release_ms）保持降音量；恢复阶段严格单调递增，不跳变回原音量。
        held = levels[: controller.config.release_ms // FRAME_MS - 1]
        self.assertEqual(set(held), {controller.config.duck_fraction})
        restoring = [level for level in levels if level < 1.0 and level != controller.config.duck_fraction]
        self.assertGreater(len(restoring), 1)
        self.assertTrue(all(left < right for left, right in zip(restoring, restoring[1:])))
        self.assertLess(restoring[0], 1.0)

    def test_hysteresis_keeps_duck_during_release_window(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        hysteretic = [
            controller.play_frame(index, frame(index)).level
            for index in range(1, controller.config.release_ms // FRAME_MS)
        ]
        self.assertEqual(set(hysteretic), {controller.config.duck_fraction})

    def test_restore_takes_the_configured_number_of_frames(self) -> None:
        controller = BargeInPlaybackController()
        self.assertEqual(controller.fade_steps, 10)  # 200 ms / 20 ms
        controller.play_frame(0, frame(0, voice=VOICE))
        start = controller.config.release_ms // FRAME_MS  # 20
        for index in range(1, start):
            self.assertEqual(
                controller.play_frame(index, frame(index)).level, controller.config.duck_fraction
            )
        fading = [
            controller.play_frame(index, frame(index)).level
            for index in range(start, start + controller.fade_steps - 1)
        ]
        self.assertTrue(all(0.0 < level < 1.0 for level in fading))
        final = controller.play_frame(start + controller.fade_steps - 1, frame(start + controller.fade_steps - 1))
        self.assertEqual(final.level, 1.0)

    def test_restore_reason_is_reported_while_fading(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        start = controller.config.release_ms // FRAME_MS
        outcome = controller.play_frame(start, frame(start))
        self.assertEqual(outcome.reason, OUTCOME_REASON_RESTORING)
        self.assertEqual(outcome.action, ACTION_RESTORE)
        self.assertFalse(outcome.handoff_to_turn)

    def test_restore_never_creates_a_user_turn(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        for index in range(1, 32):
            outcome = controller.play_frame(index, frame(index))
            self.assertFalse(outcome.handoff_to_turn)
            self.assertIsNone(outcome.utterance_id)

    def test_playback_ended_restores_immediately(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        outcome = controller.playback_ended(13)
        self.assertEqual(outcome.level, 1.0)
        self.assertFalse(outcome.continue_playback)
        self.assertEqual(outcome.reason, OUTCOME_REASON_ENDED)
        self.assertIs(controller.state, BargeInState.IDLE)

    def test_reset_starts_a_clean_episode(self) -> None:
        controller = BargeInPlaybackController()
        for index in range(20):
            controller.play_frame(index, frame(index, voice=VOICE))
        controller.reset()
        self.assertEqual(controller.level, 1.0)
        self.assertIs(controller.state, BargeInState.IDLE)
        outcome = controller.play_frame(0, frame(0))
        self.assertEqual(outcome.reason, OUTCOME_REASON_PLAYBACK)


class ConfirmedBargeInTests(unittest.TestCase):
    def _confirm(self, controller: BargeInPlaybackController):
        outcomes = []
        for index in range(controller.config.confirm_frames):
            outcomes.append(controller.play_frame(index, frame(index, voice=VOICE)))
        return outcomes

    def test_sustained_voice_confirms_and_stops_playback(self) -> None:
        controller = BargeInPlaybackController()
        outcomes = self._confirm(controller)
        last = outcomes[-1]
        self.assertIs(last.state, BargeInState.CONFIRMED)
        self.assertFalse(last.continue_playback)
        self.assertTrue(last.handoff_to_turn)
        self.assertEqual(last.level, 0.0)
        self.assertEqual(last.reason, OUTCOME_REASON_CONFIRMED)

    def test_old_reply_can_never_resume_after_confirmation(self) -> None:
        controller = BargeInPlaybackController()
        confirmed = self._confirm(controller)[-1]
        self.assertFalse(confirmed.resume_reply_submission)
        follow_up = controller.play_frame(99, frame(99))
        self.assertFalse(follow_up.resume_reply_submission)
        self.assertFalse(follow_up.continue_playback)

    def test_confirmed_state_is_sticky_until_reset(self) -> None:
        controller = BargeInPlaybackController()
        self._confirm(controller)
        for index in range(30, 35):
            outcome = controller.play_frame(index, frame(index))
            self.assertIs(outcome.state, BargeInState.CONFIRMED)
            self.assertTrue(outcome.handoff_to_turn)
            self.assertEqual(outcome.level, 0.0)

    def test_confirmation_hands_off_a_single_utterance_id(self) -> None:
        controller = BargeInPlaybackController()
        outcomes = self._confirm(controller)
        ids = [outcome.utterance_id for outcome in outcomes if outcome.utterance_id]
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids[0].startswith("utterance-"))

    def test_pre_roll_is_released_for_the_same_utterance(self) -> None:
        controller = BargeInPlaybackController()
        self._confirm(controller)
        pre_roll = controller.take_pre_roll()
        self.assertEqual(len(pre_roll), controller.config.confirm_frames)
        self.assertEqual(controller.take_pre_roll(), ())

    def test_pre_roll_snapshot_is_reported_without_audio(self) -> None:
        controller = BargeInPlaybackController()
        outcome = self._confirm(controller)[-1]
        self.assertGreaterEqual(outcome.pre_roll_frames, 1)
        self.assertGreaterEqual(outcome.pre_roll_duration_ms, 200)
        self.assertNotIn("pcm", outcome.to_details())

    def test_confirmed_outcome_never_carries_authorization_fields(self) -> None:
        controller = BargeInPlaybackController()
        details = self._confirm(controller)[-1].to_details()
        for name in ("authorization", "authorized", "confirmation_window_id", "plan", "tool"):
            self.assertNotIn(name, details)

    def test_single_short_noise_does_not_confirm(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        for index in range(1, 30):
            outcome = controller.play_frame(index, frame(index))
        self.assertIsNot(outcome.state, BargeInState.CONFIRMED)
        self.assertFalse(outcome.handoff_to_turn)

    def test_queued_audio_keeps_barge_in_window_open(self) -> None:
        controller = BargeInPlaybackController()
        outcome = controller.play_frame(
            0, frame(0, voice=VOICE, playback_active=False, queued_ms=500)
        )
        self.assertIs(outcome.state, BargeInState.SUSPECTED)


class PromptInvalidationTests(unittest.TestCase):
    def _confirm(self, controller: BargeInPlaybackController):
        for index in range(controller.config.confirm_frames):
            controller.play_frame(index, frame(index, voice=VOICE))

    def test_barge_in_invalidates_an_active_prompt(self) -> None:
        controller = BargeInPlaybackController()
        self.assertIs(controller.prompt.state, PROMPT_STATE_VALID)
        self._confirm(controller)
        self.assertIs(controller.prompt.state, PROMPT_STATE_INVALIDATED)
        self.assertTrue(controller.prompt.replay_required)
        self.assertEqual(controller.prompt.reason, PROMPT_REASON_BARGE_IN)

    def test_invalidated_prompt_cannot_authorize(self) -> None:
        controller = BargeInPlaybackController()
        self._confirm(controller)
        self.assertFalse(controller.prompt.authorization_possible)

    def test_prompt_is_not_invalidated_by_suspicion_only(self) -> None:
        controller = BargeInPlaybackController()
        controller.play_frame(0, frame(0, voice=VOICE))
        self.assertIs(controller.prompt.state, PROMPT_STATE_VALID)
        self.assertTrue(controller.prompt.authorization_possible)

    def test_incomplete_replay_keeps_prompt_invalidated(self) -> None:
        controller = BargeInPlaybackController()
        self._confirm(controller)
        policy = controller.mark_prompt_completed(replay_confirmed=False)
        self.assertIs(policy.state, PROMPT_STATE_INVALIDATED)
        self.assertTrue(policy.replay_required)
        self.assertEqual(policy.reason, PROMPT_REASON_REPLAY_INCOMPLETE)

    def test_complete_replay_clears_the_replay_requirement(self) -> None:
        controller = BargeInPlaybackController()
        self._confirm(controller)
        policy = controller.mark_prompt_completed(replay_confirmed=True)
        self.assertIs(policy.state, PROMPT_STATE_COMPLETED)
        self.assertFalse(policy.replay_required)

    def test_completed_prompt_is_not_invalidated_by_later_barge_in(self) -> None:
        controller = BargeInPlaybackController()
        controller.mark_prompt_completed(replay_confirmed=True)
        self._confirm(controller)
        self.assertIs(controller.prompt.state, PROMPT_STATE_COMPLETED)

    def test_replay_flag_must_be_boolean(self) -> None:
        controller = BargeInPlaybackController()
        with self.assertRaises(ContractError):
            controller.mark_prompt_completed(replay_confirmed="yes")  # type: ignore[arg-type]

    def test_reset_clears_prompt_invalidation(self) -> None:
        controller = BargeInPlaybackController()
        self._confirm(controller)
        controller.reset()
        self.assertIs(controller.prompt.state, PROMPT_STATE_VALID)


class OrchestrationBoundaryTests(unittest.TestCase):
    def test_module_has_no_device_file_or_network_imports(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "app"
            / "duplex"
            / "barge_in_orchestration.py"
        )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        banned_roots = {
            "socket",
            "subprocess",
            "threading",
            "asyncio",
            "ctypes",
            "pathlib",
            "os",
            "shutil",
            "wave",
            "sounddevice",
            "sherpa_onnx",
            "requests",
            "urllib",
            "http",
        }
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned_roots)

    def test_module_never_imports_safety_chain_modules(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "app"
            / "duplex"
            / "barge_in_orchestration.py"
        )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
        offenders = {name for name in modules if ".".join(name.split(".")[:2]) in banned}
        self.assertEqual(offenders, set())

    def test_package_exports_the_r1b_surface(self) -> None:
        import app.duplex as duplex

        for name in (
            "BargeInPlaybackController",
            "BargeInOutcome",
            "PromptPolicy",
            "PROMPT_STATE_INVALIDATED",
            "OUTCOME_REASON_CONFIRMED",
        ):
            self.assertIn(name, duplex.__all__)


if __name__ == "__main__":
    unittest.main()
