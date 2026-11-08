"""P10 批次 R4-B P1/P2：打断通道的**真 AEC（AEC3）逐帧注入**与**残余回声地板感知动态门**。

覆盖四层：
1. `BargeInChannelConfig` 新字段校验（`use_reference_aec` / `residual_gate_margin` /
   `residual_floor_frames` / `residual_floor_quantile`）与 `to_details()`；
2. `_apply_reference_aec()`：未启用/无参考/无 ``process``/异常/输出非法/成功 六条路径，
   以及"参考帧静音不计入地板"；
3. 动态门：``margin=None`` 或地板未建立时**必须**退回固定门（行为不变），地板建立后
   ``max(固定门, 地板 × margin)``；`_analyze()` 真的用动态门；
4. 会话接线：`AutoVoiceConfig.aec_engine` 校验、只有"参考后端真是 webrtc 且依赖可用"才
   `aec_engine_running=True` 并把 `use_reference_aec` 传进通道。

全部离线：假参考、假环境，不打开任何设备、不放音。
"""

from __future__ import annotations

import types
import unittest

import numpy as np

from app.contracts.base import ContractError
from app.duplex.barge_in_channel import (
    AEC_REASON_OUTPUT_INVALID,
    AEC_REASON_PROCESS_FAILED,
    AEC_REASON_REFERENCE_SILENT,
    DEFAULT_ENERGY_THRESHOLD,
    BargeInChannel,
    BargeInChannelConfig,
)
from app.runtime.auto_voice import AEC_ENGINES, AutoVoiceConfig

FRAME_BYTES = 640
SAMPLES = FRAME_BYTES // 2


def pcm(amplitude: int, *, samples: int = SAMPLES) -> bytes:
    values = np.full(samples, amplitude, dtype="<i2")
    return values.tobytes()


class FakeCanceller:
    def __init__(self, *, erle_sufficient: bool = True, has_setter: bool = True) -> None:
        self.available = True
        self.erle_sufficient = erle_sufficient
        self.delays: list[int] = []
        if has_setter:
            self.set_stream_delay_ms = self._set_delay  # type: ignore[assignment]

    def _set_delay(self, value: int) -> None:
        self.delays.append(value)


class FakeAecReference:
    """`SameProcessAecReference` 的最小替身（只实现本批用到的面）。"""

    def __init__(
        self,
        *,
        output: np.ndarray | None = None,
        applied: bool = True,
        reason: str = "",
        erle: float = 12.0,
        erle_sufficient: bool = True,
        fail: bool = False,
        backend: str = "webrtc",
        rate: int = 16_000,
    ) -> None:
        self.pcm = types.SimpleNamespace(sample_rate=rate)
        self.backend = backend
        self.canceller = FakeCanceller(erle_sufficient=erle_sufficient)
        self.calls: list[tuple[int, bytes]] = []
        self._output = output
        self._applied = applied
        self._reason = reason
        self._erle = erle
        self._fail = fail

    def process(self, *, capture_at_ms: int, capture: bytes) -> object:
        self.calls.append((capture_at_ms, capture))
        if self._fail:
            raise RuntimeError("boom")
        size = len(capture) // 2
        output = self._output if self._output is not None else np.zeros(size, dtype=np.float32)
        return types.SimpleNamespace(
            output=output,
            applied=self._applied,
            fallback_reason=self._reason,
            cumulative_erle_db=self._erle,
            reference_state="ok",
        )


def build_channel(
    *,
    reference: object | None = None,
    use_reference_aec: bool = True,
    residual_gate_margin: float | None = 3.0,
    energy_threshold: float = DEFAULT_ENERGY_THRESHOLD,
    clock_value: float = 1234.5,
) -> BargeInChannel:
    config = BargeInChannelConfig(
        use_reference_aec=use_reference_aec,
        residual_gate_margin=residual_gate_margin,
        energy_threshold=energy_threshold,
    )
    return BargeInChannel(
        environment=types.SimpleNamespace(),
        config=config,
        reference=reference,
        clock=lambda: clock_value,
    )


# --------------------------------------------------------------------- 配置


class AecChannelConfigTests(unittest.TestCase):
    def test_defaults_keep_previous_behaviour(self) -> None:
        config = BargeInChannelConfig()
        self.assertFalse(config.use_reference_aec)
        self.assertIsNone(config.residual_gate_margin)
        self.assertEqual(config.residual_floor_frames, 50)
        self.assertAlmostEqual(config.residual_floor_quantile, 0.2)

    def test_use_reference_aec_must_be_bool(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(use_reference_aec="yes")  # type: ignore[arg-type]

    def test_margin_below_one_is_rejected(self) -> None:
        """不得把门降到固定门以下（那会让判定比改动前更敏感）。"""

        with self.assertRaises(ContractError):
            BargeInChannelConfig(residual_gate_margin=0.5)

    def test_margin_nan_and_bool_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(residual_gate_margin=float("nan"))
        with self.assertRaises(ContractError):
            BargeInChannelConfig(residual_gate_margin=True)  # type: ignore[arg-type]

    def test_quantile_and_frames_are_validated(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(residual_floor_quantile=0.0)
        with self.assertRaises(ContractError):
            BargeInChannelConfig(residual_floor_quantile=1.5)
        with self.assertRaises(ContractError):
            BargeInChannelConfig(residual_floor_frames=0)

    def test_details_expose_the_new_fields(self) -> None:
        details = BargeInChannelConfig(use_reference_aec=True, residual_gate_margin=4.0).to_details()
        self.assertTrue(details["use_reference_aec"])
        self.assertEqual(details["residual_gate_margin"], 4.0)
        self.assertEqual(details["residual_floor_frames"], 50)
        self.assertAlmostEqual(details["residual_floor_quantile"], 0.2)


# --------------------------------------------------------------------- AEC 注入


class ReferenceAecInjectionTests(unittest.TestCase):
    def test_disabled_returns_payload_untouched(self) -> None:
        payload = pcm(1200)
        channel = build_channel(reference=FakeAecReference(), use_reference_aec=False)
        result, active = channel._apply_reference_aec(payload)  # noqa: SLF001
        self.assertEqual(result, payload)
        self.assertFalse(active)
        self.assertFalse(channel.aec_running)

    def test_missing_reference_or_process_is_a_noop(self) -> None:
        payload = pcm(1200)
        self.assertEqual(build_channel(reference=None)._apply_reference_aec(payload)[0], payload)  # noqa: SLF001
        self.assertEqual(
            build_channel(reference=object())._apply_reference_aec(payload)[0],  # noqa: SLF001
            payload,
        )

    def test_process_exception_is_captured_not_raised(self) -> None:
        channel = build_channel(reference=FakeAecReference(fail=True))
        result, active = channel._apply_reference_aec(pcm(900))  # noqa: SLF001
        self.assertEqual(result, pcm(900))
        self.assertFalse(active)
        self.assertEqual(channel.aec_fallback_reason, AEC_REASON_PROCESS_FAILED)
        self.assertEqual(channel.config, channel.config)  # 只是可读性占位

    def test_invalid_output_length_is_rejected(self) -> None:
        channel = build_channel(
            reference=FakeAecReference(output=np.zeros(SAMPLES // 2, dtype=np.float32))
        )
        result, _ = channel._apply_reference_aec(pcm(900))  # noqa: SLF001
        self.assertEqual(len(result), FRAME_BYTES)
        self.assertEqual(channel.aec_fallback_reason, AEC_REASON_OUTPUT_INVALID)

    def test_successful_processing_returns_residual_and_counts_applied(self) -> None:
        residual = np.full(SAMPLES, 0.01, dtype=np.float32)
        channel = build_channel(reference=FakeAecReference(output=residual, erle=13.5))
        result, active = channel._apply_reference_aec(pcm(3000))  # noqa: SLF001
        self.assertEqual(len(result), FRAME_BYTES)
        self.assertTrue(active)
        self.assertEqual(channel.aec_frames_applied, 1)
        self.assertAlmostEqual(channel.aec_erle_db or 0.0, 13.5)
        self.assertTrue(channel.aec_erle_sufficient)

    def test_clock_is_used_as_capture_timestamp(self) -> None:
        reference = FakeAecReference()
        channel = build_channel(reference=reference, clock_value=9876.25)
        channel._apply_reference_aec(pcm(500))  # noqa: SLF001
        self.assertEqual(reference.calls[0][0], 9_876_250)

    def test_reference_silent_is_reported_as_inactive(self) -> None:
        channel = build_channel(
            reference=FakeAecReference(reason=AEC_REASON_REFERENCE_SILENT, applied=False)
        )
        _, active = channel._apply_reference_aec(pcm(500))  # noqa: SLF001
        self.assertFalse(active)
        self.assertEqual(channel.aec_frames_applied, 0)

    def test_details_report_aec_facts(self) -> None:
        channel = build_channel(reference=FakeAecReference(erle=9.5))
        channel._apply_reference_aec(pcm(500))  # noqa: SLF001
        details = channel.to_details()
        self.assertTrue(details["aec_running"])
        self.assertEqual(details["aec_frames_applied"], 1)
        self.assertAlmostEqual(details["aec_erle_db"], 9.5)
        for name in ("pcm", "payload", "audio", "samples"):
            self.assertNotIn(name, details)


# --------------------------------------------------------------------- 动态门


class ResidualGateTests(unittest.TestCase):
    def test_margin_none_keeps_fixed_gate(self) -> None:
        channel = build_channel(residual_gate_margin=None)
        channel._residual_levels.extend([500.0] * 10)  # noqa: SLF001
        self.assertEqual(channel._effective_energy_threshold(), DEFAULT_ENERGY_THRESHOLD)  # noqa: SLF001
        self.assertIsNone(channel.gate_in_playback)

    def test_empty_floor_keeps_fixed_gate(self) -> None:
        channel = build_channel()
        self.assertEqual(channel._effective_energy_threshold(), DEFAULT_ENERGY_THRESHOLD)  # noqa: SLF001
        self.assertIsNone(channel.gate_in_playback)

    def test_low_floor_does_not_lower_the_fixed_gate(self) -> None:
        channel = build_channel(residual_gate_margin=3.0)
        channel._residual_levels.extend([100.0] * 20)  # noqa: SLF001
        self.assertEqual(channel._effective_energy_threshold(), DEFAULT_ENERGY_THRESHOLD)  # noqa: SLF001

    def test_high_floor_raises_the_gate(self) -> None:
        channel = build_channel(residual_gate_margin=3.0)
        channel._residual_levels.extend([400.0] * 20)  # noqa: SLF001
        gate = channel._effective_energy_threshold()  # noqa: SLF001
        self.assertAlmostEqual(gate, 1200.0)
        self.assertAlmostEqual(channel.residual_echo_floor_pcm16 or 0.0, 400.0)
        self.assertAlmostEqual(channel.gate_in_playback or 0.0, 1200.0)

    def test_low_quantile_ignores_a_speaking_user(self) -> None:
        """用户说话的帧会抬高窗口，但低分位地板应贴近"只有回声"的水平。"""

        channel = build_channel(residual_gate_margin=2.0)
        channel._residual_levels.extend([200.0] * 40 + [2000.0] * 10)  # noqa: SLF001
        # p20 落在 200 那一档 → 门 = max(600, 400) = 600（不因用户说话而抬高）
        self.assertEqual(channel._effective_energy_threshold(), DEFAULT_ENERGY_THRESHOLD)  # noqa: SLF001

    def test_analyze_uses_the_dynamic_gate(self) -> None:
        channel = build_channel(residual_gate_margin=1.0, energy_threshold=600.0)
        channel._residual_levels.extend([1000.0] * 20)  # noqa: SLF001
        packet = types.SimpleNamespace(payload=pcm(900))
        probability, rms = channel._analyze(packet, pcm(900))  # noqa: SLF001
        # 动态门 = 1000 → 900/1000 = 0.9（若用固定门 600 会是 1.0）
        self.assertAlmostEqual(probability, 0.9, places=3)
        self.assertAlmostEqual(rms, 900.0, places=1)


class DelayHintTests(unittest.TestCase):
    def test_new_alignment_delay_is_forwarded_once(self) -> None:
        reference = FakeAecReference()
        channel = build_channel(reference=reference)
        channel._alignment = types.SimpleNamespace(delay_ms=350, state="ok")  # noqa: SLF001
        channel._note_alignment()  # noqa: SLF001
        channel._note_alignment()  # noqa: SLF001 - 同一个延迟不重复下发
        self.assertEqual(reference.canceller.delays, [350])
        self.assertEqual(channel.aec_stream_delay_ms, 350)

    def test_canceller_without_setter_is_tolerated(self) -> None:
        reference = FakeAecReference()
        reference.canceller = FakeCanceller(has_setter=False)
        channel = build_channel(reference=reference)
        channel._alignment = types.SimpleNamespace(delay_ms=120, state="ok")  # noqa: SLF001
        channel._note_alignment()  # noqa: SLF001 - 不抛错
        self.assertIsNone(channel.aec_stream_delay_ms)


# --------------------------------------------------------------------- 会话配置


class AutoVoiceAecEngineConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = AutoVoiceConfig()
        self.assertEqual(config.aec_engine, "nlms")
        self.assertAlmostEqual(config.aec_residual_gate_margin, 3.0)
        self.assertIn("webrtc", AEC_ENGINES)

    def test_invalid_engine_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AutoVoiceConfig(aec_engine="aec3")

    def test_margin_below_one_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AutoVoiceConfig(aec_residual_gate_margin=0.9)
        with self.assertRaises(ValueError):
            AutoVoiceConfig(aec_residual_gate_margin=True)


class AutoVoiceAecEngineWiringTests(unittest.TestCase):
    """真会话装配（假环境）：只有"参考后端真是 webrtc 且依赖可用"才真的跑 AEC3。"""

    def _session(self, reference: object):
        from tests.foundation.test_auto_voice_barge_in import (
            FakeSpeaker,
            barge_in_test_config,
            build_session,
        )
        from app.tts.waveout_pcm import PlaybackGain

        session, _holder, _environment = build_session(
            config=barge_in_test_config("confirm", aec_engine="webrtc", aec_reference_mode="same_process"),
            speaker=FakeSpeaker(),
            playback_gain=PlaybackGain(),
            reference=reference,
        )
        return session

    def test_reference_without_webrtc_backend_does_not_run_aec(self) -> None:
        from tests.foundation.test_auto_voice_barge_in import UsableReference

        session = self._session(UsableReference())
        try:
            channel = session._barge_in_channel  # noqa: SLF001
            self.assertIsNotNone(channel)
            self.assertFalse(channel.config.use_reference_aec)
            details = session.barge_in_details()
            self.assertEqual(details["aec_engine_requested"], "webrtc")
            self.assertFalse(details["aec_engine_running"])
            self.assertIsNone(details["gate_in_playback"])
        finally:
            session.close()

    def test_webrtc_reference_enables_aec_and_dynamic_gate(self) -> None:
        reference = FakeAecReference()
        reference.timeline_frames = lambda limit=0: ()  # type: ignore[attr-defined]
        session = self._session(reference)
        try:
            channel = session._barge_in_channel  # noqa: SLF001
            self.assertIsNotNone(channel)
            self.assertTrue(channel.config.use_reference_aec)
            self.assertAlmostEqual(channel.config.residual_gate_margin, 3.0)
            details = session.barge_in_details()
            self.assertTrue(details["aec_engine_running"])
            self.assertEqual(details["aec_plan"]["level"], "aec_ns")
            self.assertTrue(details["aec_plan"]["aec_applied"])
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
