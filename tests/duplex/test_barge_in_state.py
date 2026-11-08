"""P10 批次 R1a：两阶段打断状态机的离线测试。

覆盖：配置与观察的 fail-closed 校验、第一阶段快速降音量与 100 ms 期限、第二阶段
持续人声确认、误触发恢复、前置缓冲上限、播放窗口结束、降级只会更保守，以及
"打断不代表授权/不建立虚假轮次"的边界不变量。
"""

from __future__ import annotations

import ast
import time
import unittest
from pathlib import Path

from app.contracts.base import ContractError
from app.duplex.barge_in import (
    ACTION_CONFIRMED,
    ACTION_DUCK,
    ACTION_IDLE,
    ACTION_RESTORE,
    DEGRADE_FULL,
    DEGRADE_STRICT,
    REASON_CONFIRMED,
    REASON_FALSE_TRIGGER,
    REASON_PLAYBACK_ENDED,
    REASON_SUSPECTED,
    BargeInConfig,
    BargeInController,
    BargeInError,
    BargeInObservation,
    BargeInState,
)

FRAME_MS = 20
QUIET = 0.05


def frame(
    index: int,
    *,
    voice: float = QUIET,
    playback_active: bool = True,
    queued_ms: int = 0,
    correlation: float | None = None,
    aec_available: bool = False,
) -> BargeInObservation:
    return BargeInObservation(
        at_ms=index * FRAME_MS,
        voice_probability=voice,
        playback_active=playback_active,
        echo_correlation=correlation,
        playback_queued_ms=queued_ms,
        aec_available=aec_available,
    )


def run_frames(controller: BargeInController, count: int, *, voice: float = QUIET, start: int = 0, **kwargs):
    decisions = []
    for offset in range(count):
        decisions.append(
            controller.observe(frame(start + offset, voice=voice, **kwargs))
        )
    return decisions


class BargeInConfigTests(unittest.TestCase):
    def test_defaults_match_design_document_ranges(self) -> None:
        config = BargeInConfig()
        self.assertLessEqual(config.duck_deadline_ms, 100)
        self.assertLessEqual(config.duck_fraction, 0.30)
        self.assertGreaterEqual(config.duck_fraction, 0.20)
        self.assertGreaterEqual(config.pre_roll_ms, 300)
        self.assertGreaterEqual(config.confirm_window_ms, 200)
        self.assertLessEqual(config.confirm_window_ms, 300)

    def test_pre_roll_frames_cover_configured_ms(self) -> None:
        config = BargeInConfig(pre_roll_ms=300, frame_duration_ms=20)
        # 容量 = 300/20 + 1：疑似触发那一帧也占位，保证触发后仍能取出 >=300 ms。
        self.assertEqual(config.pre_roll_frames, 16)
        self.assertGreaterEqual(
            (config.pre_roll_frames - 1) * config.frame_duration_ms, 300
        )

    def test_confirm_frames_cover_configured_window(self) -> None:
        config = BargeInConfig(confirm_window_ms=250, frame_duration_ms=20)
        self.assertEqual(config.confirm_frames, 13)

    def test_pre_roll_below_design_minimum_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(pre_roll_ms=299)

    def test_duck_fraction_must_be_below_full_volume(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(duck_fraction=1.0)

    def test_confirm_threshold_cannot_be_below_suspect_threshold(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(suspect_threshold=0.8, confirm_threshold=0.7)

    def test_strict_threshold_cannot_be_below_confirm_threshold(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(confirm_threshold=0.9, strict_confirm_threshold=0.8)

    def test_non_finite_fraction_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(duck_fraction=float("nan"))

    def test_zero_pre_roll_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(pre_roll_ms=0)

    def test_confirm_threshold_for_degradation_only_raises_for_strict(self) -> None:
        config = BargeInConfig(confirm_threshold=0.7, strict_confirm_threshold=0.9)
        self.assertEqual(config.confirm_threshold_for(DEGRADE_FULL), 0.7)
        self.assertEqual(config.confirm_threshold_for(DEGRADE_STRICT), 0.9)


class BargeInObservationTests(unittest.TestCase):
    def test_negative_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInObservation(at_ms=-1, voice_probability=0.5, playback_active=True)

    def test_boolean_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInObservation(at_ms=True, voice_probability=0.5, playback_active=True)

    def test_voice_probability_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInObservation(at_ms=0, voice_probability=1.5, playback_active=True)

    def test_echo_correlation_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInObservation(
                at_ms=0, voice_probability=0.5, playback_active=True, echo_correlation=-0.1
            )

    def test_non_boolean_playback_flag_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInObservation(at_ms=0, voice_probability=0.5, playback_active=1)  # type: ignore[arg-type]

    def test_negative_queued_ms_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInObservation(
                at_ms=0, voice_probability=0.5, playback_active=True, playback_queued_ms=-1
            )

    def test_in_playback_covers_queued_audio_without_active_flag(self) -> None:
        observation = BargeInObservation(
            at_ms=0, voice_probability=0.5, playback_active=False, playback_queued_ms=120
        )
        self.assertTrue(observation.in_playback)

    def test_out_of_playback_when_nothing_left(self) -> None:
        observation = BargeInObservation(
            at_ms=0, voice_probability=0.5, playback_active=False, playback_queued_ms=0
        )
        self.assertFalse(observation.in_playback)


class BargeInStageOneTests(unittest.TestCase):
    def test_first_suspected_frame_ducks_immediately(self) -> None:
        controller = BargeInController()
        decision = controller.observe(frame(0, voice=0.9))
        self.assertIs(decision.state, BargeInState.SUSPECTED)
        self.assertEqual(decision.action, ACTION_DUCK)
        self.assertEqual(decision.reason, REASON_SUSPECTED)
        self.assertTrue(decision.ducking)
        self.assertEqual(decision.level, controller.config.duck_fraction)
        self.assertEqual(decision.duck_deadline_ms, controller.config.duck_deadline_ms)

    def test_duck_target_stays_within_design_volume_range(self) -> None:
        controller = BargeInController()
        decision = controller.observe(frame(0, voice=0.9))
        self.assertGreaterEqual(decision.level, 0.20)
        self.assertLessEqual(decision.level, 0.30)

    def test_no_turn_is_created_while_only_suspected(self) -> None:
        controller = BargeInController()
        decisions = run_frames(controller, 5, voice=0.9)
        for decision in decisions:
            self.assertIs(decision.state, BargeInState.SUSPECTED)
            self.assertFalse(decision.user_turn_created)

    def test_quiet_frames_keep_full_volume_and_idle_state(self) -> None:
        controller = BargeInController()
        decision = controller.observe(frame(0, voice=QUIET))
        self.assertIs(decision.state, BargeInState.IDLE)
        self.assertEqual(decision.action, ACTION_IDLE)
        self.assertFalse(decision.ducking)

    def test_deadline_counts_down_on_later_frames(self) -> None:
        controller = BargeInController()
        first = controller.observe(frame(0, voice=0.9))
        second = controller.observe(frame(1, voice=0.9))
        self.assertEqual(first.duck_deadline_ms, 100)
        self.assertEqual(second.duck_deadline_ms, 80)

    def test_duck_deadline_never_negative(self) -> None:
        controller = BargeInController()
        controller.observe(frame(0, voice=0.9))
        decision = None
        for index in range(1, 6):
            decision = controller.observe(frame(index, voice=0.9))
        self.assertIsNotNone(decision)
        self.assertGreaterEqual(decision.duck_deadline_ms, 0)

    def test_observation_type_is_enforced(self) -> None:
        controller = BargeInController()
        with self.assertRaises(ContractError):
            controller.observe(object())  # type: ignore[arg-type]

    def test_timestamps_must_be_monotonic(self) -> None:
        controller = BargeInController()
        controller.observe(frame(5, voice=0.9))
        with self.assertRaises(ContractError):
            controller.observe(frame(4, voice=0.9))

    def test_pre_roll_capacity_covers_design_minimum(self) -> None:
        controller = BargeInController()
        capacity_ms = controller.config.pre_roll_frames * controller.config.frame_duration_ms
        self.assertGreaterEqual(capacity_ms, 300)

    def test_pre_roll_keeps_every_observed_frame_until_confirmation(self) -> None:
        controller = BargeInController()
        run_frames(controller, 20, voice=0.95)
        pre_roll = controller.pre_roll()
        # 确认窗口（250 ms）早于前置 300 ms，确认时刻缓冲里应保留全部已观察帧，无丢帧。
        self.assertEqual(controller.state, BargeInState.CONFIRMED)
        self.assertGreaterEqual(pre_roll.duration_ms, 200)
        self.assertEqual(pre_roll.first_at_ms, 0)
        self.assertEqual(
            pre_roll.frames, controller.config.confirm_frames
        )

    def test_pre_roll_duration_counts_frames_not_span(self) -> None:
        controller = BargeInController()
        run_frames(controller, 5, voice=QUIET)
        pre_roll = controller.pre_roll()
        self.assertEqual(pre_roll.frames, 5)
        self.assertEqual(pre_roll.duration_ms, 5 * controller.config.frame_duration_ms)

    def test_pre_roll_is_bounded_by_frame_cap(self) -> None:
        controller = BargeInController(config=BargeInConfig(pre_roll_ms=600, max_pre_roll_frames=12))
        run_frames(controller, 40, voice=0.9)
        self.assertEqual(controller.pre_roll().frames, 12)

    def test_pre_roll_summary_is_empty_before_any_frame(self) -> None:
        controller = BargeInController()
        summary = controller.pre_roll()
        self.assertEqual(summary.frames, 0)
        self.assertIsNone(summary.first_at_ms)
        self.assertEqual(summary.duration_ms, 0)


class BargeInStageTwoTests(unittest.TestCase):
    def test_confirmation_requires_sustained_voice(self) -> None:
        controller = BargeInController()
        decisions = run_frames(controller, controller.config.confirm_frames, voice=0.95)
        self.assertTrue(all(decision.state is BargeInState.SUSPECTED for decision in decisions[:-1]))
        last = decisions[-1]
        self.assertIs(last.state, BargeInState.CONFIRMED)
        self.assertEqual(last.action, ACTION_CONFIRMED)
        self.assertEqual(last.reason, REASON_CONFIRMED)
        self.assertTrue(last.user_turn_created)

    def test_confirmation_happens_within_design_window(self) -> None:
        controller = BargeInController()
        decisions = run_frames(controller, controller.config.confirm_frames, voice=0.95)
        confirmed_at = decisions[-1].frames_in_window * controller.config.frame_duration_ms
        self.assertGreaterEqual(confirmed_at, 200)
        self.assertLessEqual(confirmed_at, 300)

    def test_single_short_noise_never_confirms(self) -> None:
        controller = BargeInController()
        controller.observe(frame(0, voice=0.95))
        for index in range(1, 10):
            controller.observe(frame(index, voice=QUIET))
        self.assertIs(controller.state, BargeInState.SUSPECTED)
        self.assertFalse(controller.pre_roll().frames == 0)

    def test_confirmed_state_is_terminal_until_reset(self) -> None:
        controller = BargeInController()
        run_frames(controller, controller.config.confirm_frames, voice=0.95)
        decisions = run_frames(controller, 5, voice=QUIET, start=20)
        for decision in decisions:
            self.assertIs(decision.state, BargeInState.CONFIRMED)
            self.assertEqual(decision.action, ACTION_CONFIRMED)
            self.assertTrue(decision.user_turn_created)

    def test_release_after_false_trigger_restores_full_volume(self) -> None:
        controller = BargeInController()
        controller.observe(frame(0, voice=0.95))
        decision = controller.release(frame(1, voice=QUIET))
        self.assertEqual(decision.action, ACTION_RESTORE)
        self.assertEqual(decision.reason, REASON_FALSE_TRIGGER)
        self.assertIs(decision.state, BargeInState.IDLE)
        self.assertEqual(decision.level, 1.0)
        self.assertFalse(decision.user_turn_created)

    def test_release_clears_confirmation_window(self) -> None:
        controller = BargeInController()
        run_frames(controller, 6, voice=0.95)
        controller.release(frame(6, voice=QUIET))
        decision = controller.observe(frame(7, voice=0.9))
        self.assertIs(decision.state, BargeInState.SUSPECTED)
        self.assertEqual(decision.frames_in_window, 1)

    def test_reset_returns_to_idle_and_full_volume(self) -> None:
        controller = BargeInController()
        run_frames(controller, controller.config.confirm_frames, voice=0.95)
        controller.reset()
        self.assertIs(controller.state, BargeInState.IDLE)
        self.assertEqual(controller.level, 1.0)
        self.assertEqual(controller.pre_roll().frames, 0)

    def test_reset_allows_a_new_timestamp_baseline(self) -> None:
        controller = BargeInController()
        run_frames(controller, 3, voice=0.9)
        controller.reset()
        decision = controller.observe(frame(0, voice=QUIET))
        self.assertIs(decision.state, BargeInState.IDLE)


class BargeInPlaybackWindowTests(unittest.TestCase):
    def test_playback_end_restores_idle_even_while_suspected(self) -> None:
        controller = BargeInController()
        controller.observe(frame(0, voice=0.95))
        decision = controller.observe(
            frame(1, voice=0.95, playback_active=False, queued_ms=0)
        )
        self.assertIs(decision.state, BargeInState.IDLE)
        self.assertEqual(decision.action, ACTION_RESTORE)
        self.assertEqual(decision.reason, REASON_PLAYBACK_ENDED)

    def test_queued_audio_keeps_the_barge_in_window_open(self) -> None:
        controller = BargeInController()
        decision = controller.observe(
            frame(0, voice=0.95, playback_active=False, queued_ms=400)
        )
        self.assertIs(decision.state, BargeInState.SUSPECTED)
        self.assertEqual(decision.action, ACTION_DUCK)

    def test_no_barge_in_when_nothing_is_playing(self) -> None:
        controller = BargeInController()
        decision = controller.observe(
            frame(0, voice=0.99, playback_active=False, queued_ms=0)
        )
        self.assertEqual(decision.action, ACTION_RESTORE)
        self.assertFalse(decision.user_turn_created)


class BargeInDegradationTests(unittest.TestCase):
    def test_partial_echo_correlation_lowers_the_voice_score(self) -> None:
        controller = BargeInController()
        decision = controller.observe(frame(0, voice=0.7, correlation=0.5))
        self.assertEqual(decision.action, ACTION_IDLE)

    def test_echo_dominated_frame_is_not_suspected(self) -> None:
        controller = BargeInController()
        decision = controller.observe(frame(0, voice=0.6, correlation=0.6))
        self.assertEqual(decision.action, ACTION_IDLE)

    def test_missing_reference_downgrades_a_full_capability_controller(self) -> None:
        controller = BargeInController(degradation=DEGRADE_FULL)
        controller.observe(frame(0, voice=0.95))
        self.assertEqual(controller.degradation, DEGRADE_STRICT)

    def test_strict_degradation_raises_the_confirmation_threshold(self) -> None:
        controller = BargeInController(degradation=DEGRADE_STRICT)
        decisions = run_frames(controller, 20, voice=0.8)
        self.assertTrue(all(decision.state is BargeInState.SUSPECTED for decision in decisions))
        self.assertTrue(all(not decision.user_turn_created for decision in decisions))

    def test_strict_degradation_still_confirms_clearly_voiced_speech(self) -> None:
        controller = BargeInController(degradation=DEGRADE_STRICT)
        decisions = run_frames(controller, controller.config.confirm_frames, voice=0.98)
        self.assertIs(decisions[-1].state, BargeInState.CONFIRMED)

    def test_unknown_degradation_level_is_rejected(self) -> None:
        with self.assertRaises(BargeInError):
            BargeInController(degradation="loud")

    def test_degradation_only_raises_thresholds(self) -> None:
        config = BargeInConfig()
        self.assertGreaterEqual(
            config.confirm_threshold_for(DEGRADE_STRICT),
            config.confirm_threshold_for(DEGRADE_FULL),
        )


class BargeInDecisionDetailsTests(unittest.TestCase):
    def test_details_contain_only_numbers_and_short_codes(self) -> None:
        controller = BargeInController()
        details = controller.observe(frame(0, voice=0.95)).to_details()
        for key, value in details.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, (str, int, float, bool, type(None)))
        self.assertEqual(details["state"], BargeInState.SUSPECTED.value)
        self.assertIn("degradation", details)
        self.assertIn("user_turn_created", details)

    def test_details_never_carry_audio_payload_fields(self) -> None:
        controller = BargeInController()
        details = controller.observe(frame(0, voice=0.95)).to_details()
        forbidden = {"pcm", "payload", "audio", "frames", "samples", "text"}
        self.assertFalse(forbidden & set(details))

    def test_frames_seen_counts_every_observation(self) -> None:
        controller = BargeInController()
        run_frames(controller, 7, voice=QUIET)
        self.assertEqual(controller.frames_seen, 7)


class BargeInBoundaryTests(unittest.TestCase):
    def test_module_imports_no_device_file_or_network_surface(self) -> None:
        source = Path(__file__).resolve().parents[2] / "app" / "duplex" / "barge_in.py"
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
            "sherpa",
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
        source = Path(__file__).resolve().parents[2] / "app" / "duplex" / "barge_in.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
        offenders = {
            name for name in modules if ".".join(name.split(".")[:2]) in banned
        }
        self.assertEqual(offenders, set())

    def test_confirm_decision_creates_a_turn_but_never_an_authorization_field(self) -> None:
        controller = BargeInController()
        decision = run_frames(controller, controller.config.confirm_frames, voice=0.95)[-1]
        self.assertTrue(decision.user_turn_created)
        details = decision.to_details()
        for name in ("authorization", "authorized", "confirmation", "plan", "execute", "tool"):
            self.assertNotIn(name, details)

    def test_fifty_frames_stay_far_below_the_duck_deadline(self) -> None:
        controller = BargeInController()
        started = time.perf_counter()
        run_frames(controller, 50, voice=0.95)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.assertLess(elapsed_ms, controller.config.duck_deadline_ms)


class MinimumSuspectRunTests(unittest.TestCase):
    """真机回归（2026-10-02）：一帧即降音量会被回声反复触发，需支持"最小连续命中帧数"。"""

    def test_default_keeps_the_existing_single_frame_semantics(self) -> None:
        self.assertEqual(BargeInConfig().min_suspect_frames, 1)
        controller = BargeInController()
        decision = controller.observe(frame(0, voice=0.95))
        self.assertIs(decision.state, BargeInState.SUSPECTED)
        self.assertTrue(decision.ducking)

    def test_frames_below_the_minimum_do_not_duck(self) -> None:
        controller = BargeInController(config=BargeInConfig(min_suspect_frames=3))
        first = controller.observe(frame(0, voice=0.95))
        second = controller.observe(frame(1, voice=0.95))
        for decision in (first, second):
            self.assertIs(decision.state, BargeInState.IDLE)
            self.assertFalse(decision.ducking)
            self.assertEqual(decision.level, 1.0)
        third = controller.observe(frame(2, voice=0.95))
        self.assertIs(third.state, BargeInState.SUSPECTED)
        self.assertTrue(third.ducking)

    def test_non_consecutive_hits_never_reach_the_minimum(self) -> None:
        controller = BargeInController(config=BargeInConfig(min_suspect_frames=4))
        for index in range(6):
            controller.observe(frame(index * 2, voice=0.95))
            controller.observe(frame(index * 2 + 1, voice=0.10))
        self.assertIs(controller.state, BargeInState.IDLE)
        self.assertEqual(controller.level, 1.0)

    def test_reset_clears_the_pending_run(self) -> None:
        controller = BargeInController(config=BargeInConfig(min_suspect_frames=3))
        controller.observe(frame(0, voice=0.95))
        controller.observe(frame(1, voice=0.95))
        controller.reset()
        decision = controller.observe(frame(2, voice=0.95))
        self.assertIs(decision.state, BargeInState.IDLE)
        self.assertFalse(decision.ducking)

    def test_invalid_minimum_is_rejected(self) -> None:
        for value in (0, -1, True, 1.5, "3"):
            with self.subTest(value=value):
                with self.assertRaises((ContractError, TypeError)):
                    BargeInConfig(min_suspect_frames=value)  # type: ignore[arg-type]

    def test_minimum_longer_than_the_confirm_window_is_still_consistent(self) -> None:
        # 最小疑似帧数大于确认窗口时：先凑满"最小连续命中"才进入疑似，不会提前降音量。
        config = BargeInConfig(min_suspect_frames=20, confirm_window_ms=250)
        controller = BargeInController(config=config)
        decisions = run_frames(controller, 19, voice=0.95)
        self.assertTrue(all(item.state is BargeInState.IDLE for item in decisions))
        self.assertIs(controller.observe(frame(19, voice=0.95)).state, BargeInState.SUSPECTED)


if __name__ == "__main__":
    unittest.main()
