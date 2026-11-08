"""P10 批次 R4-B-1：真 AEC（WebRTC APM/AEC3）后端离线测试。

覆盖：
- 配置校验（10 ms 整数倍、正采样率、非负延迟、门槛类型）；
- **依赖缺失 / 底层构造失败 / 底层调用异常 → 一律如实直通**（固定原因码、不伪装、不冒泡）；
- 假实现（注入 module）验证调用契约：每帧都调底层、双讲也调、reset 透传；
- 真实现（装了就测、没装自动 skip）：合成回声抑制、无回声不误报、静音、双讲、
  **20 ms 与 2×10 ms 逐样本等价**、reset 幂等、详情不含音频；
- `SameProcessAecReference(backend="webrtc")` 接线；**默认后端仍是 nlms**（行为不变）。

全部测试**不需要设备、不需要麦克风、不落盘音频**。
"""

from __future__ import annotations

import unittest

import numpy as np

from app.audio.aec.nlms import NlmsEchoCanceller, NlmsConfig
from app.audio.aec.reference_source import (
    BACKEND_NLMS,
    BACKEND_WEBRTC,
    AecReferencePCM,
    AecReferenceSourceError,
    SameProcessAecReference,
)
from app.audio.aec.webrtc_backend import (
    EVIDENCE_BELOW_GATE,
    EVIDENCE_NO_PROCESSING,
    EVIDENCE_REFERENCE_INSUFFICIENT,
    EVIDENCE_STATES,
    EVIDENCE_SUPPRESSION_ONLY,
    REASON_BACKEND_ERROR,
    REASON_DEPENDENCY_MISSING,
    REASON_DOUBLE_TALK,
    REASON_ERLE_BELOW_GATE,
    REASON_REFERENCE_SILENT,
    WebrtcAecConfig,
    WebrtcEchoCanceller,
    webrtc_backend_available,
)
from app.contracts.base import ContractError

RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000  # 320

HAVE_WEBRTC = webrtc_backend_available()
requires_webrtc = unittest.skipUnless(HAVE_WEBRTC, "pywebrtc_audio 未安装（真 AEC 后端不可用）")


# --------------------------------------------------------------------------- 工具


def pcm16(values: np.ndarray) -> bytes:
    return np.asarray(values, dtype=np.int16).tobytes()


def build_echo_pair(
    *,
    frames: int,
    delay_ms: int = 40,
    gain: float = 0.5,
    noise_rms: float = 100.0,
    seed: int = 7,
) -> tuple[list[bytes], list[bytes]]:
    """合成 (far, near)：``near = gain * far[滞后 delay_ms] + 白噪声``（非整数样本用整数近似）。"""

    rng = np.random.default_rng(seed)
    total = frames * FRAME_SAMPLES
    far = rng.standard_normal(total).astype(np.float32) * 3000.0
    delay = int(round(RATE * delay_ms / 1000.0))
    echo = np.concatenate((np.zeros(delay, dtype=np.float32), far))[:total] * gain
    near = echo + rng.standard_normal(total).astype(np.float32) * noise_rms
    far_frames = [
        pcm16(far[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]) for i in range(frames)
    ]
    near_frames = [
        pcm16(near[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]) for i in range(frames)
    ]
    return far_frames, near_frames


class _FakeNativeCanceller:
    """底层替身：可配置“原样返回”或“残差为零”或“抛错”。"""

    def __init__(self, *, mode: str = "zero", **kwargs: object) -> None:
        self.mode = mode
        self.kwargs = kwargs
        self.stream_delay_ms = int(kwargs.get("stream_delay_ms", 0))
        self.calls = 0
        self.resets = 0

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "passthrough":
            return np.asarray(near, dtype=np.int16)
        return np.zeros_like(np.asarray(near, dtype=np.int16))

    def reset(self) -> None:
        self.resets += 1


class _FakeModule:
    """替身模块：按 mode 产出底层实例；``raise_on_init`` 模拟构造失败。"""

    def __init__(self, mode: str = "zero", *, raise_on_init: bool = False) -> None:
        self.mode = mode
        self.raise_on_init = raise_on_init
        self.created: list[_FakeNativeCanceller] = []

    def EchoCanceller(self, **kwargs: object) -> _FakeNativeCanceller:  # noqa: N802 - 对齐真实 API
        if self.raise_on_init:
            raise RuntimeError("init boom")
        instance = _FakeNativeCanceller(mode=self.mode, **kwargs)
        self.created.append(instance)
        return instance


def quiet_frame(samples: int = FRAME_SAMPLES) -> bytes:
    return pcm16(np.zeros(samples, dtype=np.float32))


# --------------------------------------------------------------------------- 配置


class WebrtcAecConfigTests(unittest.TestCase):
    def test_defaults_match_16k_20ms(self) -> None:
        config = WebrtcAecConfig()
        self.assertEqual(config.sample_rate, RATE)
        self.assertEqual(config.frame_duration_ms, FRAME_MS)
        self.assertEqual(config.frame_samples, FRAME_SAMPLES)

    def test_frame_duration_must_be_multiple_of_apm_quantum(self) -> None:
        with self.assertRaises(ContractError):
            WebrtcAecConfig(frame_duration_ms=15)

    def test_zero_values_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            WebrtcAecConfig(sample_rate=0)
        with self.assertRaises(ContractError):
            WebrtcAecConfig(frame_duration_ms=0)

    def test_negative_stream_delay_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            WebrtcAecConfig(stream_delay_ms=-1)

    def test_non_numeric_gate_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            WebrtcAecConfig(erle_gate_db="high")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 依赖不可用


class WebrtcBackendUnavailableTests(unittest.TestCase):
    """依赖缺失/构造失败/调用异常都必须**如实直通**，绝不伪装已消除回声。"""

    def test_missing_dependency_reports_fixed_reason_and_passes_through(self) -> None:
        canceller = WebrtcEchoCanceller(module=None)
        self.assertFalse(canceller.available)
        self.assertEqual(canceller.available_reason, REASON_DEPENDENCY_MISSING)
        mic = pcm16(np.full(FRAME_SAMPLES, 1234, dtype=np.int16))
        result = canceller.cancel(reference=quiet_frame(), capture=mic)
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, REASON_DEPENDENCY_MISSING)
        self.assertEqual(int(result.output.size), FRAME_SAMPLES)
        self.assertTrue(np.allclose(result.output, 1234 / 32768.0, atol=1e-6))
        self.assertEqual(canceller.frames_processed, 0)
        self.assertEqual(canceller.frames_seen, 1)
        self.assertFalse(canceller.erle_sufficient)

    def test_init_failure_is_reported_not_raised(self) -> None:
        canceller = WebrtcEchoCanceller(module=_FakeModule(raise_on_init=True))
        self.assertFalse(canceller.available)
        self.assertEqual(canceller.available_reason, REASON_BACKEND_ERROR)
        details = canceller.to_details()
        self.assertEqual(details["init_error"], "RuntimeError")
        self.assertFalse(details["available"])

    def test_backend_exception_falls_back_without_bubbling(self) -> None:
        canceller = WebrtcEchoCanceller(module=_FakeModule(mode="raise"))
        self.assertTrue(canceller.available)
        result = canceller.cancel(reference=quiet_frame(), capture=quiet_frame())
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, REASON_BACKEND_ERROR)
        self.assertEqual(canceller.backend_errors, 1)
        self.assertEqual(canceller.last_backend_error, "RuntimeError")
        self.assertEqual(canceller.frames_processed, 0)

    def test_details_never_contain_audio(self) -> None:
        canceller = WebrtcEchoCanceller(module=None)
        details = canceller.to_details()
        for name in ("pcm", "payload", "audio", "samples"):
            self.assertNotIn(name, details)
        self.assertIn("available_reason", details)
        self.assertEqual(details["backend"], "webrtc_aec3")

    def test_contract_violations_raise(self) -> None:
        canceller = WebrtcEchoCanceller(module=None)
        with self.assertRaises(ContractError):
            canceller.cancel(reference=quiet_frame(160), capture=quiet_frame())
        with self.assertRaises(ContractError):
            canceller.cancel(reference="not-bytes", capture=quiet_frame())
        with self.assertRaises(ContractError):
            canceller.cancel(reference=quiet_frame(), capture=quiet_frame(), double_talk=1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 假实现契约


class WebrtcBackendContractTests(unittest.TestCase):
    def test_native_call_receives_int16_frames(self) -> None:
        module = _FakeModule(mode="zero")
        canceller = WebrtcEchoCanceller(module=module)
        result = canceller.cancel(
            reference=pcm16(np.full(FRAME_SAMPLES, 1000, dtype=np.int16)),
            capture=pcm16(np.full(FRAME_SAMPLES, 2000, dtype=np.int16)),
        )
        # 采集非静音 + 残差为零 → ERLE 无穷 → 应用
        self.assertTrue(result.applied)
        self.assertEqual(len(module.created), 1)
        self.assertEqual(module.created[0].calls, 1)
        self.assertEqual(module.created[0].kwargs["sample_rate"], RATE)
        self.assertEqual(module.created[0].kwargs["num_channels"], 1)

    def test_silence_without_reference_is_never_applied(self) -> None:
        """参考帧无能量 → 没有可消除的回声：不得动近端，也拿不到任何证据。"""

        module = _FakeModule(mode="zero")
        canceller = WebrtcEchoCanceller(module=module)
        result = canceller.cancel(
            reference=quiet_frame(), capture=pcm16(np.full(FRAME_SAMPLES, 2000, dtype=np.int16))
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, REASON_REFERENCE_SILENT)
        self.assertEqual(canceller.suppression_evidence, EVIDENCE_REFERENCE_INSUFFICIENT)

    def test_passthrough_residual_keeps_frame_below_gate(self) -> None:
        canceller = WebrtcEchoCanceller(module=_FakeModule(mode="passthrough"))
        result = canceller.cancel(
            reference=pcm16(np.full(FRAME_SAMPLES, 3000, dtype=np.int16)),
            capture=pcm16(np.full(FRAME_SAMPLES, 2000, dtype=np.int16)),
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, REASON_ERLE_BELOW_GATE)
        self.assertEqual(result.erle_db, 0.0)
        self.assertEqual(canceller.suppression_evidence, EVIDENCE_BELOW_GATE)

    def test_double_talk_passes_through_but_still_calls_native(self) -> None:
        module = _FakeModule(mode="zero")
        canceller = WebrtcEchoCanceller(module=module)
        result = canceller.cancel(
            reference=pcm16(np.full(FRAME_SAMPLES, 1000, dtype=np.int16)),
            capture=pcm16(np.full(FRAME_SAMPLES, 2000, dtype=np.int16)),
            double_talk=True,
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.fallback_reason, REASON_DOUBLE_TALK)
        self.assertTrue(result.double_talk)
        self.assertEqual(module.created[0].calls, 1)  # 双讲也必须喂底层，保持自适应连续
        self.assertEqual(canceller.frames_processed, 1)

    def test_evidence_states_never_include_echo_path_confirmation(self) -> None:
        """诚实性不变量：本后端**不提供**"已确认回声路径"这一档。"""

        fresh = WebrtcEchoCanceller(module=_FakeModule())
        self.assertEqual(fresh.suppression_evidence, EVIDENCE_NO_PROCESSING)
        joined = " ".join(EVIDENCE_STATES)
        self.assertNotIn("confirmed", joined)  # 没有任何"已确认"档
        self.assertNotIn("proven", joined)  # 也没有"已证明"档
        # 抑制量达标那一档必须自带"未证明回声路径"的措辞
        self.assertIn("without_echo_path_proof", EVIDENCE_SUPPRESSION_ONLY)

    def test_reset_clears_statistics_and_forwards_to_native(self) -> None:
        module = _FakeModule(mode="zero")
        canceller = WebrtcEchoCanceller(module=module)
        canceller.cancel(
            reference=pcm16(np.full(FRAME_SAMPLES, 1000, dtype=np.int16)),
            capture=pcm16(np.full(FRAME_SAMPLES, 2000, dtype=np.int16)),
        )
        canceller.reset()
        self.assertEqual(canceller.frames_processed, 0)
        self.assertEqual(canceller.frames_seen, 0)
        self.assertEqual(canceller.suppression_evidence, EVIDENCE_NO_PROCESSING)
        self.assertEqual(module.created[0].resets, 1)

    def test_configured_stream_delay_reaches_native(self) -> None:
        module = _FakeModule(mode="zero")
        canceller = WebrtcEchoCanceller(
            config=WebrtcAecConfig(stream_delay_ms=40), module=module
        )
        self.assertEqual(module.created[0].kwargs["stream_delay_ms"], 40)
        self.assertEqual(canceller.stream_delay_ms, 40)
        canceller.set_stream_delay_ms(80)
        self.assertEqual(canceller.stream_delay_ms, 80)
        with self.assertRaises(ContractError):
            canceller.set_stream_delay_ms(-5)


# --------------------------------------------------------------------------- 真实现


@requires_webrtc
class WebrtcBackendRealTests(unittest.TestCase):
    def test_synthetic_echo_is_suppressed(self) -> None:
        canceller = WebrtcEchoCanceller()
        far_frames, near_frames = build_echo_pair(frames=300)
        applied = 0
        for index, (far_frame, near_frame) in enumerate(zip(far_frames, near_frames)):
            result = canceller.cancel(reference=far_frame, capture=near_frame)
            if index >= 150 and result.applied:
                applied += 1
        self.assertEqual(canceller.frames_processed, 300)
        self.assertEqual(canceller.backend_errors, 0)
        self.assertGreater(canceller.cumulative_erle_db, 6.0)
        self.assertTrue(canceller.erle_sufficient)
        self.assertGreater(applied, 0)

    def test_uncorrelated_capture_with_active_reference_proves_suppression_only(self) -> None:
        """**如实记录的现实现象**：参考有能量时，即使近端与参考完全无关（没有回声路径），
        AEC3 也会把采集压到"残余抑制地板"（本机实测 ≈11.7 dB）。

        因此这里**只断言"有抑制"**，并断言证据等级是 ``suppression_only``——
        它**不是**"回声路径已确认"。把抑制量当成"已消除回声"是本项目明确禁止的。
        """

        canceller = WebrtcEchoCanceller()
        rng = np.random.default_rng(3)
        for _ in range(120):
            far_frame = pcm16(rng.standard_normal(FRAME_SAMPLES).astype(np.float32) * 3000)
            near_frame = pcm16(rng.standard_normal(FRAME_SAMPLES).astype(np.float32) * 300)
            canceller.cancel(reference=far_frame, capture=near_frame)
        self.assertGreater(canceller.cumulative_erle_db, 6.0)
        self.assertTrue(canceller.erle_sufficient)
        self.assertEqual(canceller.suppression_evidence, EVIDENCE_SUPPRESSION_ONLY)

    def test_reference_silence_yields_no_evidence(self) -> None:
        canceller = WebrtcEchoCanceller()
        rng = np.random.default_rng(5)
        for _ in range(60):
            near_frame = pcm16(rng.standard_normal(FRAME_SAMPLES).astype(np.float32) * 300)
            result = canceller.cancel(reference=quiet_frame(), capture=near_frame)
            self.assertFalse(result.applied)
            self.assertEqual(result.fallback_reason, REASON_REFERENCE_SILENT)
        self.assertLess(canceller.cumulative_erle_db, 6.0)
        self.assertEqual(canceller.suppression_evidence, EVIDENCE_REFERENCE_INSUFFICIENT)

    def test_silence_reports_zero_erle(self) -> None:
        canceller = WebrtcEchoCanceller()
        result = canceller.cancel(reference=quiet_frame(), capture=quiet_frame())
        self.assertFalse(result.applied)
        self.assertEqual(result.erle_db, 0.0)
        self.assertEqual(result.fallback_reason, REASON_REFERENCE_SILENT)

    def test_20ms_matches_two_10ms_calls(self) -> None:
        """绑定内部按 10 ms 切帧：一次 20 ms 调用必须与两次 10 ms 调用逐样本一致。"""

        single = WebrtcEchoCanceller(config=WebrtcAecConfig(frame_duration_ms=20))
        halves = WebrtcEchoCanceller(config=WebrtcAecConfig(frame_duration_ms=10))
        far_frames, near_frames = build_echo_pair(frames=80)
        half_bytes = (FRAME_SAMPLES // 2) * 2  # 字节切片：160 样本 = 320 字节
        for far_frame, near_frame in zip(far_frames, near_frames):
            whole = single.cancel(reference=far_frame, capture=near_frame).output
            left = halves.cancel(
                reference=far_frame[:half_bytes],
                capture=near_frame[:half_bytes],
            ).output
            right = halves.cancel(
                reference=far_frame[half_bytes:],
                capture=near_frame[half_bytes:],
            ).output
            self.assertTrue(np.array_equal(whole, np.concatenate((left, right))))

    def test_reset_matches_fresh_instance(self) -> None:
        far_frames, near_frames = build_echo_pair(frames=60)
        canceller = WebrtcEchoCanceller()
        for far_frame, near_frame in zip(far_frames, near_frames):
            canceller.cancel(reference=far_frame, capture=near_frame)
        canceller.reset()
        after_reset = canceller.cancel(reference=far_frames[0], capture=near_frames[0]).output
        fresh = WebrtcEchoCanceller()
        baseline = fresh.cancel(reference=far_frames[0], capture=near_frames[0]).output
        self.assertTrue(np.array_equal(after_reset, baseline))

    def test_realtime_budget(self) -> None:
        """单帧耗时必须有数量级余量（20 ms 帧的实时倍数 > 5×）。"""

        import time

        canceller = WebrtcEchoCanceller()
        far_frames, near_frames = build_echo_pair(frames=60)
        started = time.perf_counter()
        for far_frame, near_frame in zip(far_frames, near_frames):
            canceller.cancel(reference=far_frame, capture=near_frame)
        elapsed = time.perf_counter() - started
        per_frame_ms = elapsed * 1000.0 / len(far_frames)
        self.assertGreater(FRAME_MS / per_frame_ms, 5.0)


# --------------------------------------------------------------------------- 接线


class ReferenceSourceWebrtcBackendTests(unittest.TestCase):
    def test_default_backend_is_still_nlms(self) -> None:
        source = SameProcessAecReference(
            pcm=AecReferencePCM(sample_rate=RATE), nlms_config=NlmsConfig(sample_rate=RATE)
        )
        self.assertEqual(source.backend, BACKEND_NLMS)
        self.assertIsInstance(source.canceller, NlmsEchoCanceller)
        self.assertEqual(source.to_details()["aec_backend"], BACKEND_NLMS)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(AecReferenceSourceError):
            SameProcessAecReference(pcm=AecReferencePCM(sample_rate=RATE), backend="magic")

    def test_webrtc_backend_is_selected_and_reported(self) -> None:
        source = SameProcessAecReference(
            pcm=AecReferencePCM(sample_rate=RATE), backend=BACKEND_WEBRTC
        )
        self.assertEqual(source.backend, BACKEND_WEBRTC)
        self.assertIsInstance(source.canceller, WebrtcEchoCanceller)
        self.assertEqual(source.to_details()["aec_backend"], BACKEND_WEBRTC)

    def test_webrtc_config_rate_mismatch_is_rejected(self) -> None:
        with self.assertRaises(AecReferenceSourceError):
            SameProcessAecReference(
                pcm=AecReferencePCM(sample_rate=RATE),
                backend=BACKEND_WEBRTC,
                webrtc_config=WebrtcAecConfig(sample_rate=48_000),
            )

    def test_resampled_reference_still_feeds_the_webrtc_backend(self) -> None:
        """24 kHz 提交侧 → 16 kHz 目标时钟 + AEC3 后端：整条链路必须能跑通。"""

        source = SameProcessAecReference(
            pcm=AecReferencePCM(sample_rate=RATE, source_sample_rate=24_000),
            backend=BACKEND_WEBRTC,
        )
        source.submit(pcm16(np.full(480, 1000, dtype=np.int16)), submitted_at_seconds=0.0)
        self.assertTrue(source.to_details()["resampling"])
        result = source.process(capture_at_ms=0, capture=quiet_frame())
        self.assertEqual(int(result.output.size), FRAME_SAMPLES)
        self.assertIn(
            result.fallback_reason,
            {
                "",
                REASON_DEPENDENCY_MISSING,
                REASON_ERLE_BELOW_GATE,
                REASON_REFERENCE_SILENT,
                REASON_BACKEND_ERROR,
            },
        )


if __name__ == "__main__":
    unittest.main()
