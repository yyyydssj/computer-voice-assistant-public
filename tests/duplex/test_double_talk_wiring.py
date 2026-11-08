"""P10 批次 R4-B5（B2）：双讲检测**接线与事实**的离线契约测试。

覆盖四层：

1. 配置校验与透传：`BargeInChannelConfig.double_talk_mode`（off/facts/protect 闭集）、
   `AutoVoiceConfig.double_talk_mode`、非法值必须抛错；
2. **零行为差异**：`off` 档不建检测器对象、不算逐帧开销；`facts` 档与 `off` 档在**完全相同**
   的帧序列下给出完全相同的判定（音量/确认/停播/地板统计都不受双讲事实影响）；
3. 事实上报：`facts` 档在"对齐可用"的帧上产出 `double_talk_*` 扁平事实，且只含
   允许的短码/数值（证据不足时如实 `unproven` + `double_talk=None`）；
4. `protect` 档在 B2 里**只记事实**（`step_scale` 等只读开关如实暴露，但没有任何保护动作）。

全部离线：复用 `tests.duplex.test_barge_in_channel` 的假采集/假参考，不开设备、不联网。
"""

from __future__ import annotations

import subprocess
import sys
import types
import unittest
from pathlib import Path

from app.audio.aec.double_talk import (
    DT_MODES,
    DT_STATES,
    DoubleTalkConfig,
    DoubleTalkDetector,
)
from app.contracts.base import ContractError
from app.duplex.barge_in_channel import (
    BargeInChannel,
    BargeInChannelConfig,
)
from app.runtime.auto_voice import DOUBLE_TALK_MODES, AutoVoiceConfig
from tests.duplex.test_barge_in_channel import (
    CAPTURE_FRAME_BYTES,
    CAPTURE_SAMPLE_RATE_HZ,
    FakeEnvironment,
    FakeWindow,
    _noise_frames,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAME_SAMPLES = CAPTURE_FRAME_BYTES // 2  # 320 样本 / 20 ms


def pcm(amplitude: int, *, samples: int = FRAME_SAMPLES) -> bytes:
    import numpy as np

    return np.full(samples, amplitude, dtype="<i2").tobytes()


class AlignedReference:
    """能给出"对齐 ok"结论的最小参考替身（参考与采集不同源 ⇒ 相关性低）。

    只实现通道真正用到的面：``pcm.sample_rate``（同钟检查）、``timeline_frames()``
    （对齐估计）、``window()``（供 AEC 使用，本批不启用 AEC 也照给）。
    """

    def __init__(self, *, frames: int = 60, at_ms: int = 1_234_500) -> None:
        self.pcm = types.SimpleNamespace(sample_rate=CAPTURE_SAMPLE_RATE_HZ)
        self._frames = _noise_frames(frames)
        self._at_ms = at_ms
        self.calls = 0

    def window(self, capture_at_ms: int) -> FakeWindow:
        self.calls += 1
        return FakeWindow(pcm=__import__("numpy").zeros(FRAME_SAMPLES, dtype="float32"))

    def timeline_frames(self, *, limit: int = 0) -> tuple[tuple[int, object], ...]:
        frames = self._frames
        return tuple(frames[-limit:]) if limit else tuple(frames)


def noisy_pcm(*, seed: int = 7, scale: float = 3000.0) -> bytes:
    """能量足够高、且**有起伏**的采集帧（对齐估计需要非恒定信号）。"""

    import numpy as np

    rng = np.random.default_rng(seed)
    samples = np.clip(rng.normal(0.0, scale / 32768.0, FRAME_SAMPLES), -1.0, 1.0)
    return (samples * 32767.0).round().astype("<i2").tobytes()


def build_channel(
    *,
    mode: str,
    reference: object | None = None,
    detector: object | None = None,
    config: DoubleTalkConfig | None = None,
) -> BargeInChannel:
    # 对齐估计间隔压到 2 帧，好在少量帧内拿到 ok 结论（生产默认 25 帧 = 0.5 s）。
    channel_config = BargeInChannelConfig(
        alignment_interval_frames=2,
        double_talk_mode=mode,
        double_talk_config=config,
    )
    return BargeInChannel(
        environment=FakeEnvironment(),
        config=channel_config,
        reference=reference,
        voice_detector=detector,
        clock=lambda: 1234.5,
    )


def always_speech(packet: object) -> bool:
    return True


class ConfigTests(unittest.TestCase):
    def test_mode_set_is_closed(self) -> None:
        self.assertEqual(DT_MODES, ("off", "facts", "protect"))
        self.assertEqual(DOUBLE_TALK_MODES, DT_MODES)
        for mode in DT_MODES:
            self.assertEqual(AutoVoiceConfig(double_talk_mode=mode).double_talk_mode, mode)
            self.assertEqual(
                BargeInChannelConfig(double_talk_mode=mode).to_details()["double_talk_mode"], mode
            )

    def test_default_is_off_everywhere(self) -> None:
        self.assertEqual(AutoVoiceConfig().double_talk_mode, "off")
        self.assertEqual(BargeInChannelConfig().double_talk_mode, "off")
        self.assertIsNone(AutoVoiceConfig().double_talk_config)

    def test_auto_config_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            AutoVoiceConfig(double_talk_mode="on")

    def test_channel_config_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(double_talk_mode="on")

    def test_channel_config_rejects_bad_threshold_object(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(double_talk_mode="facts", double_talk_config=object())  # type: ignore[arg-type]

    def test_custom_thresholds_are_accepted(self) -> None:
        config = BargeInChannelConfig(
            double_talk_mode="facts",
            double_talk_config=DoubleTalkConfig(min_evidence=3),
        )
        self.assertEqual(config.double_talk_config.min_evidence, 3)


class ZeroOverheadTests(unittest.TestCase):
    def test_off_mode_does_not_build_a_detector(self) -> None:
        channel = build_channel(mode="off")
        self.assertFalse(channel.double_talk_enabled)
        self.assertIsNone(channel.double_talk_state)
        self.assertEqual(channel.double_talk_facts, {})
        facts = channel.double_talk_details()
        self.assertEqual(facts["double_talk_mode"], "off")
        self.assertFalse(facts["double_talk_enabled"])
        self.assertIsNone(facts["double_talk_state"])
        self.assertEqual(facts["double_talk_frames"], 0)

    def test_facts_mode_builds_a_detector(self) -> None:
        channel = build_channel(mode="facts")
        self.assertTrue(channel.double_talk_enabled)
        self.assertEqual(channel.double_talk_details()["double_talk_mode"], "facts")

    def test_details_include_double_talk_keys_and_stay_flat(self) -> None:
        for mode in DT_MODES:
            details = build_channel(mode=mode).to_details()
            self.assertIn("double_talk_mode", details)
            self.assertIn("double_talk_state", details)
            for key, value in details.items():
                self.assertIsInstance(key, str)
                self.assertIsInstance(value, (str, int, float, bool, type(None), tuple))


class FactRecordingTests(unittest.TestCase):
    def _feed(self, channel: BargeInChannel, frames: int, payload: bytes = b"") -> None:
        payload = payload or noisy_pcm()
        environment = channel._environment  # noqa: SLF001 - 测试需要复用同一假采集
        for _ in range(frames):
            environment.capture.push(payload)
            channel.observe_once()

    def test_facts_only_after_alignment_is_ok(self) -> None:
        channel = build_channel(mode="facts", reference=AlignedReference())
        channel.arm()
        # 第一帧：对齐还没结论 ⇒ 不产出双讲事实（绝不猜）。
        self._feed(channel, 1)
        self.assertEqual(channel.double_talk_facts, {})
        # 对齐估计需要约 0.6 s 采集才给 `ok`（既有口径），因此喂到 35 帧再断言。
        self._feed(channel, 34)
        facts = channel.double_talk_facts
        self.assertTrue(facts, f"对齐 ok 之后必须有双讲事实: {channel.double_talk_details()}")
        self.assertIn(str(facts["state"]), DT_STATES)
        self.assertGreater(channel.double_talk_details()["double_talk_frames"], 0)

    def test_facts_are_flat_and_short_codes(self) -> None:
        channel = build_channel(mode="facts", reference=AlignedReference())
        channel.arm()
        self._feed(channel, 30)
        facts = channel.double_talk_details()
        for key, value in facts.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, (str, int, float, bool, type(None)))
        evidence = facts["double_talk_evidence"]
        self.assertIsInstance(evidence, str)
        self.assertLessEqual(len(str(evidence)), 128)

    def test_unproven_is_not_double_talk(self) -> None:
        """参考与采集不相关（噪声参考）时最多只有 1~2 条证据；不足就必须如实不认。"""

        channel = build_channel(mode="facts", reference=AlignedReference())
        channel.arm()
        self._feed(channel, 30)
        facts = channel.double_talk_facts
        if facts.get("state") == "unproven":
            self.assertIsNone(facts["double_talk"])
        self.assertIn(facts["state"], DT_STATES)

    def test_reset_on_arm_clears_counts(self) -> None:
        channel = build_channel(mode="facts", reference=AlignedReference())
        channel.arm()
        self._feed(channel, 35)
        self.assertGreater(channel.double_talk_details()["double_talk_frames"], 0)
        channel.disarm()
        channel.arm()
        self.assertEqual(channel.double_talk_details()["double_talk_frames"], 0)
        self.assertIsNone(channel.double_talk_state)

    def test_protect_mode_only_records_facts_in_this_batch(self) -> None:
        """B2 里 protect 与 facts 的可观测差异为 **零**（保护动作留给 B3）。"""

        facts_channel = build_channel(mode="facts", reference=AlignedReference())
        protect_channel = build_channel(mode="protect", reference=AlignedReference())
        for channel in (facts_channel, protect_channel):
            channel.arm()
            self._feed(channel, 30)
        self.assertEqual(
            facts_channel.double_talk_facts,
            protect_channel.double_talk_facts,
            "B2 的 protect 档不得产生任何额外行为差异",
        )
        self.assertEqual(
            protect_channel.double_talk_details()["double_talk_mode"], "protect"
        )


class NoBehaviourChangeTests(unittest.TestCase):
    """**本批的核心承诺**：双讲事实不得改变音量/确认/停播/地板统计。"""

    def _run(self, mode: str) -> dict[str, object]:
        channel = build_channel(
            mode=mode,
            reference=AlignedReference(),
            detector=always_speech,
        )
        channel.arm()
        outcomes = []
        payload = noisy_pcm()
        environment = channel._environment  # noqa: SLF001
        for _ in range(40):
            environment.capture.push(payload)
            outcome = channel.observe_once()
            if outcome is not None:
                outcomes.append(
                    (
                        outcome.level,
                        outcome.ducking,
                        outcome.handoff_to_turn,
                        outcome.state,
                    )
                )
        return {
            "outcomes": outcomes,
            "confirmed": channel.confirmed,
            "level": channel.level,
            "reason": channel.reason,
            "residual_levels": tuple(channel._residual_levels),  # noqa: SLF001
            "duck_suppressed": channel.duck_suppressed,
            "gate_in_playback": channel.gate_in_playback,
        }

    def test_facts_mode_is_behaviourally_identical_to_off(self) -> None:
        off = self._run("off")
        facts = self._run("facts")
        protect = self._run("protect")
        self.assertEqual(off, facts, "facts 档改变了判定行为")
        self.assertEqual(off, protect, "protect 档在 B2 改变了判定行为")

    def test_observation_does_not_touch_residual_floor(self) -> None:
        """双讲事实**不参与**残余地板统计（B3 才做剔除）。"""

        channel = build_channel(
            mode="facts",
            reference=AlignedReference(),
            detector=always_speech,
            config=DoubleTalkConfig(min_evidence=2, enter_frames=2),
        )
        channel.arm()
        payload = noisy_pcm()
        environment = channel._environment  # noqa: SLF001
        for _ in range(30):
            environment.capture.push(payload)
            channel.observe_once()
        # 本批未启用参考 AEC ⇒ 地板统计本身不增长（口径不变），
        # 双讲结论不得让它增长或清零。
        self.assertEqual(len(channel._residual_levels), 0)  # noqa: SLF001


class DetectorReuseTests(unittest.TestCase):
    def test_channel_uses_the_canonical_detector_type(self) -> None:
        channel = build_channel(mode="facts")
        self.assertIsInstance(channel._double_talk_detector, DoubleTalkDetector)  # noqa: SLF001

    def test_channel_config_passes_thresholds_through(self) -> None:
        config = DoubleTalkConfig(enter_frames=2, min_evidence=2)
        channel = build_channel(mode="facts", config=config)
        self.assertEqual(channel._double_talk_detector.config.enter_frames, 2)  # noqa: SLF001


class CliWiringTests(unittest.TestCase):
    def test_help_lists_the_new_flag_with_off_default(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "app.main", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = (result.stdout or "") + (result.stderr or "")
        self.assertIn("--double-talk-detect", help_text)
        self.assertIn("off", help_text)

    def test_default_mode_is_off_without_the_flag(self) -> None:
        """不带开关时 `AutoVoiceConfig` 必须是 off（行为与改动前一致）。"""

        self.assertEqual(AutoVoiceConfig().double_talk_mode, "off")

    def test_illegal_mode_is_rejected_by_the_cli(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "app.main", "--double-talk-detect", "on"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":  # pragma: no cover - 便于单文件直跑
    unittest.main()
