"""P10 批次 R4-B-2 阶段 0/方案 C：打断**冷却窗口**（`barge_in_cooldown`）离线测试。

方案 C 的语义（研究文档 §4.C + R4-B-2 阶段 0 结论）：
播放开始后的 `cooldown_ms` 窗口内**照收帧、照填前置缓冲**，但**不降音量、不建立疑似/确认**
（如实记 ``barge_in_cooldown``）。它**不改变** R4-A 的 fail-closed 口径：没证明"能看见自己的
回声"仍然不真停播。

本文件覆盖四层：
1. `BargeInConfig.cooldown_ms` 校验 + 默认 0（行为不变）；
2. R1a 状态机：窗口内不疑似/不降音量、窗口后恢复判定、前置缓冲不丢、episode/reset 重新计时；
3. 通道配置：`BargeInChannelConfig(cooldown_ms=...)` **只覆盖冷却时长**，不动
   `min_suspect_frames=3`/`release_ms=800`（真机缺陷的历史教训）；
4. 会话接线：`AutoVoiceConfig.barge_in_cooldown_ms` 校验 + 真的传到通道 + 进只读详情。

全部离线：不打开设备、不联网。
"""

from __future__ import annotations

import unittest

from app.duplex.barge_in import (
    ACTION_IDLE,
    ACTION_DUCK,
    REASON_COOLDOWN,
    REASON_PLAYBACK_ENDED,
    REASON_SUSPECTED,
    BargeInConfig,
    BargeInController,
    BargeInDecision,
    BargeInObservation,
    BargeInState,
)
from app.duplex.barge_in_channel import BargeInChannelConfig
from app.contracts.base import ContractError

FRAME_MS = 20


def observe(
    controller: BargeInController,
    *,
    at_ms: int,
    probability: float = 1.0,
    playback_active: bool = True,
    queued_ms: int = 0,
) -> BargeInDecision:
    return controller.observe(
        BargeInObservation(
            at_ms=at_ms,
            voice_probability=probability,
            playback_active=playback_active,
            playback_queued_ms=queued_ms,
        )
    )


# --------------------------------------------------------------------- 配置


class CooldownConfigTests(unittest.TestCase):
    def test_default_is_off(self) -> None:
        self.assertEqual(BargeInConfig().cooldown_ms, 0)

    def test_zero_is_allowed(self) -> None:
        self.assertEqual(BargeInConfig(cooldown_ms=0).cooldown_ms, 0)

    def test_positive_is_allowed(self) -> None:
        self.assertEqual(BargeInConfig(cooldown_ms=1000).cooldown_ms, 1000)

    def test_negative_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(cooldown_ms=-1)

    def test_bool_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInConfig(cooldown_ms=True)


# --------------------------------------------------------------------- 状态机


class CooldownStateMachineTests(unittest.TestCase):
    def test_default_config_never_reports_cooldown(self) -> None:
        controller = BargeInController(config=BargeInConfig())
        for index in range(30):
            decision = observe(controller, at_ms=index * FRAME_MS)
            self.assertNotEqual(decision.reason, REASON_COOLDOWN)
        self.assertEqual(controller.cooldown_frames, 0)
        self.assertIsNone(controller.cooldown_remaining_ms)

    def test_window_blocks_suspect_and_ducking(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=1000))
        for index in range(50):  # 0 ~ 980 ms
            decision = observe(controller, at_ms=index * FRAME_MS)
            self.assertEqual(decision.action, ACTION_IDLE)
            self.assertEqual(decision.reason, REASON_COOLDOWN)
            self.assertEqual(decision.level, 1.0)
            self.assertEqual(controller.state, BargeInState.IDLE)
        self.assertEqual(controller.cooldown_frames, 50)

    def test_window_expires_then_normal_detection_resumes(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=1000))
        for index in range(50):
            observe(controller, at_ms=index * FRAME_MS)
        decision = observe(controller, at_ms=1000)
        self.assertEqual(decision.action, ACTION_DUCK)
        self.assertEqual(decision.reason, REASON_SUSPECTED)
        self.assertEqual(controller.state, BargeInState.SUSPECTED)

    def test_pre_roll_is_kept_during_the_window(self) -> None:
        """冷却期内**不丢音频**：真人插话的这段音频仍要能并入同一 utterance。"""

        controller = BargeInController(config=BargeInConfig(cooldown_ms=1000))
        for index in range(20):
            observe(controller, at_ms=index * FRAME_MS)
        pre_roll = controller.pre_roll()
        self.assertGreater(pre_roll.frames, 0)
        self.assertEqual(pre_roll.frames, len(controller.take_pre_roll()))

    def test_remaining_ms_counts_down_then_stops(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=200))
        observe(controller, at_ms=0)
        self.assertEqual(controller.cooldown_remaining_ms, 200)
        observe(controller, at_ms=100)
        self.assertEqual(controller.cooldown_remaining_ms, 100)
        observe(controller, at_ms=200)
        self.assertEqual(controller.cooldown_remaining_ms, 0)
        self.assertEqual(controller.cooldown_frames, 2)

    def test_playback_end_re_arms_the_window_for_the_next_episode(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=200))
        for index in range(10):
            observe(controller, at_ms=index * FRAME_MS)
        ended = observe(controller, at_ms=200, playback_active=False)
        self.assertEqual(ended.reason, REASON_PLAYBACK_ENDED)
        # 新 episode：冷却窗口重新计时（不是沿用上一集的起点）。
        again = observe(controller, at_ms=1000)
        self.assertEqual(again.reason, REASON_COOLDOWN)
        self.assertEqual(again.cooldown_remaining_ms, 200)

    def test_reset_clears_the_window(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=200))
        for index in range(5):
            observe(controller, at_ms=index * FRAME_MS)
        controller.reset()
        decision = observe(controller, at_ms=60)
        self.assertEqual(decision.reason, REASON_COOLDOWN)

    def test_confirmation_still_happens_after_the_window(self) -> None:
        config = BargeInConfig(cooldown_ms=100, min_suspect_frames=1)
        controller = BargeInController(config=config)
        confirmed = False
        for index in range(40):
            decision = observe(controller, at_ms=100 + index * FRAME_MS)
            if decision.state is BargeInState.CONFIRMED:
                confirmed = True
                break
        self.assertTrue(confirmed)

    def test_not_in_playback_is_reported_as_playback_ended(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=1000))
        decision = observe(controller, at_ms=0, playback_active=False)
        self.assertEqual(decision.reason, REASON_PLAYBACK_ENDED)
        self.assertEqual(controller.cooldown_frames, 0)

    def test_queued_audio_counts_as_in_playback(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=200))
        decision = observe(controller, at_ms=0, playback_active=False, queued_ms=100)
        self.assertEqual(decision.reason, REASON_COOLDOWN)

    def test_decision_details_expose_cooldown_and_no_audio(self) -> None:
        controller = BargeInController(config=BargeInConfig(cooldown_ms=200))
        details = observe(controller, at_ms=0).to_details()
        self.assertEqual(details["cooldown_remaining_ms"], 200)
        self.assertEqual(details["cooldown_frames"], 1)
        for name in ("pcm", "payload", "audio", "samples"):
            self.assertNotIn(name, details)

    def test_decision_defaults_keep_backward_compatibility(self) -> None:
        decision = BargeInDecision(
            state=BargeInState.IDLE,
            action=ACTION_IDLE,
            reason="playback_only",
            degradation="full",
            level=1.0,
            target_level=1.0,
            duck_deadline_ms=None,
            pre_roll_frames=16,
            confirm_frames=13,
            frames_in_window=0,
            peak_voice_probability=0.0,
        )
        self.assertIsNone(decision.cooldown_remaining_ms)
        self.assertEqual(decision.cooldown_frames, 0)


# --------------------------------------------------------------------- 通道配置


class ChannelCooldownConfigTests(unittest.TestCase):
    def _config(self) -> BargeInConfig:
        from app.duplex.barge_in_channel import DEFAULT_MIN_SUSPECT_FRAMES, DEFAULT_RELEASE_MS

        return BargeInConfig(
            min_suspect_frames=DEFAULT_MIN_SUSPECT_FRAMES, release_ms=DEFAULT_RELEASE_MS
        )

    def test_none_keeps_the_underlying_config(self) -> None:
        base = BargeInConfig(cooldown_ms=300)
        config = BargeInChannelConfig(barge_in=base)
        self.assertEqual(config.barge_in.cooldown_ms, 300)
        self.assertIsNone(config.cooldown_ms)

    def test_override_only_changes_the_cooldown(self) -> None:
        """历史教训：绝不能因为设一个冷却时长而重建整个配置、覆盖会话级修复。"""

        config = BargeInChannelConfig(barge_in=self._config(), cooldown_ms=600)
        self.assertEqual(config.barge_in.cooldown_ms, 600)
        self.assertEqual(config.barge_in.min_suspect_frames, self._config().min_suspect_frames)
        self.assertEqual(config.barge_in.release_ms, self._config().release_ms)

    def test_zero_override_turns_it_off(self) -> None:
        config = BargeInChannelConfig(barge_in=BargeInConfig(cooldown_ms=500), cooldown_ms=0)
        self.assertEqual(config.barge_in.cooldown_ms, 0)

    def test_negative_override_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(cooldown_ms=-20)

    def test_bool_override_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(cooldown_ms=True)

    def test_details_report_the_cooldown(self) -> None:
        config = BargeInChannelConfig(cooldown_ms=1000)
        self.assertEqual(config.to_details()["cooldown_ms"], 1000)


# --------------------------------------------------------------------- 会话接线


class SessionCooldownWiringTests(unittest.TestCase):
    """真会话装配（假环境）：开关必须真的落到通道，且不覆盖会话级修复值。"""

    def _session(self, **overrides: object):
        from tests.foundation.test_auto_voice_barge_in import (
            FakeSpeaker,
            UsableReference,
            barge_in_test_config,
            build_session,
        )
        from app.tts.waveout_pcm import PlaybackGain

        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm", **overrides),
            speaker=FakeSpeaker(),
            playback_gain=PlaybackGain(),
            reference=UsableReference(),
        )
        return session

    def test_cooldown_flag_reaches_the_channel(self) -> None:
        session = self._session(barge_in_cooldown_ms=1000)
        try:
            channel = session._barge_in_channel  # noqa: SLF001 - 契约测试直接看装配结果
            self.assertIsNotNone(channel)
            self.assertEqual(channel.cooldown_ms, 1000)
            self.assertEqual(channel.config.barge_in.min_suspect_frames, 3)
            self.assertEqual(channel.config.barge_in.release_ms, 800)
            self.assertEqual(session.barge_in_details()["cooldown_ms"], 1000)
        finally:
            session.close()

    def test_default_is_off(self) -> None:
        session = self._session()
        try:
            channel = session._barge_in_channel  # noqa: SLF001
            self.assertEqual(channel.cooldown_ms, 0)
            details = session.barge_in_details()
            self.assertEqual(details["cooldown_ms"], 0)
            self.assertIsNone(details["cooldown_remaining_ms"])
            self.assertEqual(details["cooldown_frames"], 0)
        finally:
            session.close()


class AutoVoiceCooldownConfigTests(unittest.TestCase):
    def test_default_is_zero(self) -> None:
        from app.runtime.auto_voice import AutoVoiceConfig

        self.assertEqual(AutoVoiceConfig().barge_in_cooldown_ms, 0)

    def test_negative_is_rejected(self) -> None:
        from app.runtime.auto_voice import AutoVoiceConfig

        with self.assertRaises(ValueError):
            AutoVoiceConfig(barge_in_cooldown_ms=-1)

    def test_bool_is_rejected(self) -> None:
        from app.runtime.auto_voice import AutoVoiceConfig

        with self.assertRaises(ValueError):
            AutoVoiceConfig(barge_in_cooldown_ms=True)


if __name__ == "__main__":
    unittest.main()
