"""P10 批次 R4-B5（B3）：双讲保护的**离线确定性对照**测试。

覆盖四层：

1. `NlmsConfig.double_talk_step_scale` 校验（0~1 闭区间、拒绝 bool 与放大步长）；
2. `NlmsEchoCanceller.cancel()`：双讲帧**冻结权重**（默认 `0.0`）但输出仍带近端语音、
   仍如实记 `double_talk_detected`、且不应用消除输出；`step_scale` 覆盖只允许更小或相等；
3. `AecProcessingChain.process()` 与 `SameProcessAecReference.process()` 的双讲透传；
4. 通道 `protect` 档的两处保护：双讲帧不进残余地板、双讲标志真的传给了参考源；
   同时**冻结不改变判定行为**（音量/确认/停播与 `facts` 档一致），旧参考替身（不接受
   `double_talk` 关键字）仍走两参数调用。

全部离线：合成信号、假参考，不打开设备、不联网。
"""

from __future__ import annotations

import inspect
import types
import unittest

import numpy as np

from app.audio.aec.nlms import (
    AecProcessingChain,
    NlmsConfig,
    NlmsEchoCanceller,
    float_to_pcm16,
    pcm16_to_float,
)
from app.audio.aec.reference import AecReferenceConfig
from app.contracts.base import ContractError
from app.duplex.barge_in_channel import (
    BargeInChannel,
    BargeInChannelConfig,
)
from app.runtime.auto_voice import AutoVoiceConfig
from tests.duplex.test_barge_in_channel import (
    CAPTURE_SAMPLE_RATE_HZ,
    FakeEnvironment,
    FakeWindow,
)
from tests.duplex.test_double_talk_wiring import AlignedReference, noisy_pcm

FRAME_SAMPLES = 320
GAIN = 0.5
DELAY_SAMPLES = 8


def echo_of(reference: np.ndarray) -> np.ndarray:
    """已知增益 + 已知延迟的合成回声（真实线性回声路径）。"""

    delayed = np.zeros_like(reference)
    delayed[DELAY_SAMPLES:] = reference[:-DELAY_SAMPLES]
    return (GAIN * delayed).astype(np.float32)


def speech_like(*, samples: int = FRAME_SAMPLES, seed: int = 11, scale: float = 0.25) -> np.ndarray:
    """有起伏的"近端语音"合成信号（对齐/能量都真实）。"""

    rng = np.random.default_rng(seed)
    return rng.normal(0.0, scale, samples).astype(np.float32)


def build_frame_pair(*, near_scale: float) -> tuple[bytes, bytes]:
    """返回 `(reference_pcm, capture_pcm)`：采集 = 回声 + 近端。"""

    reference = speech_like(scale=0.3)
    near = speech_like(scale=near_scale, seed=13)
    capture = echo_of(reference) + near
    return float_to_pcm16(reference), float_to_pcm16(capture)


def energy(pcm: bytes) -> float:
    samples = pcm16_to_float(pcm)
    return float(np.dot(samples, samples))


class DoubleTalkStepScaleConfigTests(unittest.TestCase):
    def test_default_freezes_the_filter(self) -> None:
        self.assertEqual(NlmsConfig().double_talk_step_scale, 0.0)

    def test_scale_must_be_unit_fraction(self) -> None:
        for bad in (-0.01, 1.01, True, float("nan")):
            with self.assertRaises(ContractError):
                NlmsConfig(double_talk_step_scale=bad)

    def test_scale_may_be_one_for_legacy_behaviour(self) -> None:
        self.assertEqual(NlmsConfig(double_talk_step_scale=1.0).double_talk_step_scale, 1.0)


class FrozenWeightTests(unittest.TestCase):
    """B3 的核心：双讲帧**权重不动**（原实现会全速自适应、把近端语音学进去）。"""

    def test_frozen_weights_do_not_move(self) -> None:
        # 先让滤波器在单讲期自适应，得到一个非零权重。
        canceller = NlmsEchoCanceller(config=NlmsConfig())
        for _ in range(4):
            reference, capture = build_frame_pair(near_scale=0.0)
            canceller.cancel(reference=reference, capture=capture, double_talk=False)
        before = canceller.weights.copy()
        self.assertGreater(float(np.abs(before).sum()), 0.0, "单讲期应当已经学到非零权重")
        for _ in range(4):
            reference, capture = build_frame_pair(near_scale=0.4)
            canceller.cancel(reference=reference, capture=capture, double_talk=True)
        after = canceller.weights.copy()
        np.testing.assert_allclose(
            after, before, rtol=0.0, atol=0.0, err_msg="双讲帧冻结失效：权重被更新了"
        )

    def test_legacy_scale_one_still_adapts(self) -> None:
        """`double_talk_step_scale=1.0` = 改动前行为：双讲帧仍更新权重（对照用）。"""

        canceller = NlmsEchoCanceller(config=NlmsConfig(double_talk_step_scale=1.0))
        first_reference, first_capture = build_frame_pair(near_scale=0.0)
        canceller.cancel(reference=first_reference, capture=first_capture, double_talk=False)
        before = canceller.weights.copy()
        for _ in range(4):
            reference, capture = build_frame_pair(near_scale=0.4)
            canceller.cancel(reference=reference, capture=capture, double_talk=True)
        after = canceller.weights.copy()
        self.assertGreater(
            float(np.abs(after - before).sum()), 0.0, "1.0 倍率下权重必须继续变化"
        )

    def test_frozen_frame_keeps_near_end_energy_in_output(self) -> None:
        """双讲帧不应用消除输出 ⇒ 近端语音能量必须保留（不许被"顺手减掉"）。"""

        canceller = NlmsEchoCanceller(config=NlmsConfig())
        reference, capture = build_frame_pair(near_scale=0.4)
        result = canceller.cancel(reference=reference, capture=capture, double_talk=True)
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, "double_talk_detected")
        self.assertTrue(result.double_talk)
        self.assertAlmostEqual(
            energy(float_to_pcm16(result.output)), energy(capture), delta=energy(capture) * 0.02
        )

    def test_single_talk_frame_still_applies_when_erle_passes(self) -> None:
        canceller = NlmsEchoCanceller(config=NlmsConfig())
        # 先跑几帧让回声路径收敛，再断言单讲帧走"应用消除输出"分支。
        for _ in range(30):
            reference, capture = build_frame_pair(near_scale=0.0)
            result = canceller.cancel(reference=reference, capture=capture, double_talk=False)
        self.assertTrue(result.applied, result.to_details())
        self.assertIsNone(result.fallback_reason)

    def test_explicit_step_scale_only_shrinks(self) -> None:
        """显式 `step_scale` 只允许"更小或相等"（保护方向单向，不能放大步长）。"""

        canceller = NlmsEchoCanceller(config=NlmsConfig(double_talk_step_scale=0.0))
        reference, capture = build_frame_pair(near_scale=0.4)
        with self.assertRaises(ContractError):
            canceller.cancel(reference=reference, capture=capture, double_talk=True, step_scale=1.5)
        # 冻结档 + 显式 1.0 ⇒ 仍然冻结（取更小者）。
        before = canceller.weights.copy()
        for _ in range(3):
            canceller.cancel(
                reference=reference, capture=capture, double_talk=True, step_scale=1.0
            )
        np.testing.assert_allclose(canceller.weights, before, rtol=0.0, atol=0.0)

    def test_partial_scale_slows_but_does_not_stop_adaptation(self) -> None:
        """`double_talk_step_scale=0.1`：允许"缓慢跟踪"档，权重仍会动但幅度更小。"""

        frozen = NlmsEchoCanceller(config=NlmsConfig(double_talk_step_scale=0.0))
        slow = NlmsEchoCanceller(config=NlmsConfig(double_talk_step_scale=0.1))
        for canceller in (frozen, slow):
            for _ in range(4):
                reference, capture = build_frame_pair(near_scale=0.0)
                canceller.cancel(reference=reference, capture=capture, double_talk=False)
        frozen_before = frozen.weights.copy()
        slow_before = slow.weights.copy()
        for _ in range(4):
            reference, capture = build_frame_pair(near_scale=0.4)
            frozen.cancel(reference=reference, capture=capture, double_talk=True)
            slow.cancel(reference=reference, capture=capture, double_talk=True)
        self.assertEqual(float(np.abs(frozen.weights - frozen_before).sum()), 0.0)
        self.assertGreater(float(np.abs(slow.weights - slow_before).sum()), 0.0)

    def test_chain_forwards_double_talk(self) -> None:
        chain = AecProcessingChain(
            reference_config=AecReferenceConfig(), nlms_config=NlmsConfig()
        )
        reference = speech_like(scale=0.3)
        near = speech_like(scale=0.4, seed=17)
        for index in range(6):
            chain.push_playback(at_ms=index * 20, pcm=float_to_pcm16(reference))
        frame = chain.process(
            capture_at_ms=100,
            capture=float_to_pcm16(echo_of(reference) + near),
            double_talk=True,
        )
        self.assertTrue(frame.double_talk)
        self.assertEqual(frame.fallback_reason, "double_talk_detected")


class ReferenceSourceForwardingTests(unittest.TestCase):
    def test_process_accepts_and_forwards_double_talk(self) -> None:
        from app.audio.aec.reference_source import AecReferencePCM, SameProcessAecReference

        reference = SameProcessAecReference(
            pcm=AecReferencePCM(
                sample_rate=CAPTURE_SAMPLE_RATE_HZ,
                source_sample_rate=CAPTURE_SAMPLE_RATE_HZ,
            ),
            backend="nlms",
        )
        pcm = float_to_pcm16(speech_like(scale=0.3))
        reference.submit(pcm, submitted_at_seconds=1000.0)
        # 参考时间线以**提交毫秒**为准：取帧时刻必须落在该帧窗口内（否则如实 reference_future
        # / reference_too_old，那不是本用例要测的路径）。
        result = reference.process(capture_at_ms=1_000_000, capture=pcm, double_talk=True)
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, "double_talk_detected")
        reference.reset()

    def test_process_signature_exposes_double_talk(self) -> None:
        from app.audio.aec.reference_source import SameProcessAecReference

        parameters = inspect.signature(SameProcessAecReference.process).parameters
        self.assertIn("double_talk", parameters)


class HealthyResidualReference(AlignedReference):
    """既能给出"对齐 ok"、又像 AEC 后端那样逐帧返回残差的参考替身。

    本批测试需要两条链路同时成立：对齐 ok（才产出双讲事实）+ `process()` 被调用
    （才走残余地板）。`AlignedReference` 提供前者，这里补上 AEC 面。
    """

    def __init__(self, *, frames: int = 60) -> None:
        super().__init__(frames=frames)
        self.backend = "nlms"
        self.calls: list[dict[str, object]] = []
        self.canceller = types.SimpleNamespace(available=True, erle_sufficient=False)

    def process(self, *, capture_at_ms: int, capture: bytes, double_talk: bool = False):
        self.calls.append({"at_ms": capture_at_ms, "double_talk": double_talk})
        size = len(capture) // 2
        return types.SimpleNamespace(
            output=np.zeros(size, dtype=np.float32),
            applied=False,
            fallback_reason="erle_below_gate",
            cumulative_erle_db=0.0,
            reference_state="ok",
            double_talk=double_talk,
        )


class LegacyKeywordlessReference(AlignedReference):
    """**不接受** `double_talk` 关键字的旧参考替身（必须回退两参数调用）。"""

    def __init__(self, *, frames: int = 60) -> None:
        super().__init__(frames=frames)
        self.backend = "nlms"
        self.calls: list[dict[str, object]] = []
        self.canceller = types.SimpleNamespace(available=True, erle_sufficient=False)

    def process(self, *, capture_at_ms: int, capture: bytes):  # type: ignore[override]
        self.calls.append({"at_ms": capture_at_ms, "double_talk": None})
        size = len(capture) // 2
        return types.SimpleNamespace(
            output=np.zeros(size, dtype=np.float32),
            applied=False,
            fallback_reason="erle_below_gate",
            cumulative_erle_db=0.0,
            reference_state="ok",
        )


def build_channel(
    *,
    mode: str,
    reference: object,
    detector: object | None = None,
) -> BargeInChannel:
    return BargeInChannel(
        environment=FakeEnvironment(),
        config=BargeInChannelConfig(
            double_talk_mode=mode,
            use_reference_aec=True,
            alignment_interval_frames=2,
            reference_sample_rate=CAPTURE_SAMPLE_RATE_HZ,
        ),
        reference=reference,
        voice_detector=detector,
        clock=lambda: 1234.5,
    )


class ChannelProtectionTests(unittest.TestCase):
    def _drive(self, channel: BargeInChannel, frames: int = 40) -> None:
        payload = noisy_pcm()
        environment = channel._environment  # noqa: SLF001
        for _ in range(frames):
            environment.capture.push(payload)
            channel.observe_once()

    def test_double_talk_flag_reaches_the_reference(self) -> None:
        reference = HealthyResidualReference()
        channel = build_channel(
            mode="protect", reference=reference, detector=lambda packet: True
        )
        channel.arm()
        self._drive(channel)
        self.assertTrue(reference.calls, "参考源必须被逐帧调用")
        for call in reference.calls:
            self.assertIsInstance(call["double_talk"], bool)

    def test_legacy_reference_without_the_keyword_still_works(self) -> None:
        """旧替身不接受 `double_talk` ⇒ 通道回退两参数调用，绝不抛异常。"""

        reference = LegacyKeywordlessReference()
        channel = build_channel(mode="protect", reference=reference, detector=lambda packet: True)
        channel.arm()
        self._drive(channel)
        self.assertTrue(reference.calls)
        self.assertTrue(all(call["double_talk"] is None for call in reference.calls))

    def test_protect_excludes_double_talk_frames_from_the_floor(self) -> None:
        facts = build_channel(
            mode="facts", reference=HealthyResidualReference(), detector=lambda p: True
        )
        protect = build_channel(
            mode="protect", reference=HealthyResidualReference(), detector=lambda p: True
        )
        for channel in (facts, protect):
            channel.arm()
            self._drive(channel)
        facts_dt = facts.double_talk_details()
        protect_dt = protect.double_talk_details()
        # 两个档的**事实**应当一致；差异只允许出现在地板统计上。
        self.assertEqual(facts_dt["double_talk_state"], protect_dt["double_talk_state"])
        self.assertGreaterEqual(facts_dt["double_talk_frames"], 1)
        facts_floor = len(facts._residual_levels)  # noqa: SLF001
        protect_floor = len(protect._residual_levels)  # noqa: SLF001
        self.assertGreater(facts_floor, 0, "facts 档不得改变地板统计")
        self.assertLess(
            protect_floor, facts_floor, "protect 档必须把双讲帧从地板统计里剔除"
        )

    def test_protect_does_not_change_decision_behaviour(self) -> None:
        outcomes: dict[str, list] = {}
        for mode in ("facts", "protect"):
            channel = build_channel(
                mode=mode, reference=HealthyResidualReference(), detector=lambda p: True
            )
            channel.arm()
            payload = noisy_pcm()
            environment = channel._environment  # noqa: SLF001
            seen = []
            for _ in range(40):
                environment.capture.push(payload)
                outcome = channel.observe_once()
                if outcome is not None:
                    seen.append(
                        (outcome.level, outcome.ducking, outcome.handoff_to_turn, outcome.state)
                    )
            outcomes[mode] = seen
            self.assertFalse(channel.confirmed)
        self.assertEqual(
            outcomes["facts"], outcomes["protect"], "protect 档不得改变音量/确认/停播判定"
        )


class AutoConfigTests(unittest.TestCase):
    def test_auto_config_still_defaults_to_off(self) -> None:
        self.assertEqual(AutoVoiceConfig().double_talk_mode, "off")

    def test_protect_mode_is_accepted(self) -> None:
        self.assertEqual(AutoVoiceConfig(double_talk_mode="protect").double_talk_mode, "protect")


if __name__ == "__main__":  # pragma: no cover - 便于单文件直跑
    unittest.main()
