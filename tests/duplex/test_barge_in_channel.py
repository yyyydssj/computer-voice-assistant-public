"""P10 R4：独立打断通道（``app/duplex/barge_in_channel.py``）的离线契约测试。

全部离线：假采集会话把合成帧推进通道**自有**的 ``AudioBus``，不打开任何真实设备、
不创建线程（监视循环在测试线程里同步跑）、不联网。
"""

from __future__ import annotations

import array
import ast
import math
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.audio.bus import AudioBusState
from app.contracts.audio import AudioFrame
from app.duplex.barge_in import DEGRADE_NO_AEC, DEGRADE_STRICT, BargeInState
from app.duplex.barge_in_channel import (
    CAPTURE_FRAME_BYTES,
    CAPTURE_SAMPLE_RATE_HZ,
    CHANNEL_AUDIO_SESSION_ID,
    CHANNEL_STOP_REASON,
    CHANNEL_STREAM_ID,
    FRAME_MS,
    PROBABILITY_MODE_DETECTOR,
    PROBABILITY_MODE_ENERGY,
    REASON_ARMED,
    REASON_CAPTURE_STOPPED,
    REASON_CAPTURE_UNAVAILABLE,
    REASON_CONFIRMED,
    REASON_DISARMED,
    REASON_NOT_ARMED,
    REASON_OBSERVING,
    REASON_PLAYBACK_ENDED,
    REFERENCE_SOURCE_CLOCK_MISMATCH,
    REFERENCE_SOURCE_NONE,
    BargeInChannel,
    BargeInChannelConfig,
)
from app.contracts.base import ContractError

MODULE_PATH = Path(__file__).resolve().parents[2] / "app" / "duplex" / "barge_in_channel.py"


def speech_payload(*, amplitude: int = 3000, samples: int = CAPTURE_FRAME_BYTES // 2) -> bytes:
    """类语音帧：**正弦**（RMS 恰为 amplitude，且有方差——对齐估计需要非恒定信号）。"""

    return array.array(
        "h",
        [
            int(round(amplitude * math.sqrt(2.0) * math.sin(2.0 * math.pi * index / 16.0)))
            for index in range(samples)
        ],
    ).tobytes()


def silence_payload(*, samples: int = CAPTURE_FRAME_BYTES // 2) -> bytes:
    return array.array("h", [0] * samples).tobytes()


def noise_payload(*, amplitude: int = 150, samples: int = CAPTURE_FRAME_BYTES // 2) -> bytes:
    """恒定幅度环境噪声帧（PCM16 RMS 恰好等于 amplitude）。"""

    return array.array("h", [amplitude] * samples).tobytes()


class FakeCapture:
    """把帧推进通道总线的假采集会话（序号/帧号按总线要求连续）。"""

    def __init__(self, bus: object, *, started: bool = True) -> None:
        self.bus = bus
        self._started = started
        self._event_sequence = 1
        self._frame_index = 0
        self.stopped: tuple[str, bool] | None = None
        self.starts = 0

    def start(self) -> SimpleNamespace:
        self.starts += 1
        return SimpleNamespace(started=self._started)

    def stop(self, *, reason: str, discard: bool) -> None:
        self.stopped = (reason, discard)

    def push(self, payload: bytes) -> object:
        frame = AudioFrame(
            audio_session_id=self.bus.audio_session_id,  # type: ignore[attr-defined]
            provider_generation=self.bus.provider_generation,  # type: ignore[attr-defined]
            stream_id=self.bus.stream_id,  # type: ignore[attr-defined]
            event_sequence=self._event_sequence,
            frame_index=self._frame_index,
            captured_at="2026-10-02T00:00:00Z",
            duration_ms=FRAME_MS,
            device_epoch=self.bus.device_epoch,  # type: ignore[attr-defined]
            sample_rate_hz=CAPTURE_SAMPLE_RATE_HZ,
            sample_count=CAPTURE_SAMPLE_RATE_HZ * FRAME_MS // 1000,
            payload_size_bytes=CAPTURE_FRAME_BYTES,
        )
        self._event_sequence += 1
        self._frame_index += 1
        return self.bus.publish(frame, payload)  # type: ignore[attr-defined]

    def close_bus(self, reason: str = "device_lost") -> None:
        self.bus.close(reason=reason, discard=True)  # type: ignore[attr-defined]


class FakeEnvironment:
    def __init__(
        self,
        *,
        started: bool = True,
        raise_on_factory: bool = False,
        missing_factory: bool = False,
    ) -> None:
        self.captures: list[FakeCapture] = []
        self._started = started
        self._raise = raise_on_factory
        if missing_factory:
            self.capture_factory = None  # type: ignore[assignment]
        else:
            self.capture_factory = self._factory

    def _factory(self, bus: object) -> FakeCapture:
        if self._raise:
            raise RuntimeError("device busy")
        capture = FakeCapture(bus, started=self._started)
        self.captures.append(capture)
        return capture

    @property
    def capture(self) -> FakeCapture:
        return self.captures[-1]


class FakeWindow:
    def __init__(self, *, state: str = "ok", pcm: object = None) -> None:
        self.state = state
        self.pcm = pcm

    @property
    def ok(self) -> bool:
        return self.state == "ok" and self.pcm is not None


class FakeReference:
    """同进程参考替身：只提供 ``pcm.sample_rate`` 与 ``window()``/``timeline_frames()``。"""

    def __init__(
        self,
        *,
        sample_rate: int = CAPTURE_SAMPLE_RATE_HZ,
        window: FakeWindow | None = None,
        raise_on_window: bool = False,
        frames: tuple[tuple[int, object], ...] = (),
        raise_on_timeline: bool = False,
    ) -> None:
        self.pcm = SimpleNamespace(sample_rate=sample_rate)
        self._window = window or FakeWindow(pcm=np.zeros(320, dtype=np.float32))
        self._raise = raise_on_window
        self._frames = frames
        self._raise_on_timeline = raise_on_timeline
        self.calls: list[int] = []

    def window(self, capture_at_ms: int) -> FakeWindow:
        self.calls.append(capture_at_ms)
        if self._raise:
            raise RuntimeError("reference broken")
        return self._window

    def timeline_frames(self, *, limit: int = 0) -> tuple[tuple[int, object], ...]:
        if self._raise_on_timeline:
            raise RuntimeError("timeline broken")
        if limit:
            return tuple(self._frames[-limit:])
        return tuple(self._frames)


def _noise_frames(count: int, *, seed: int = 3, seconds_per_frame: float = 0.02) -> tuple:
    """同钟、非静音、与采集**不相关**的参考帧（给出 ok 状态 + 低相关性）。"""

    rng = np.random.default_rng(seed)
    start = int(time.monotonic() * 1000.0)
    return tuple(
        (start + int(index * seconds_per_frame * 1000), rng.normal(0, 0.05, 320).astype(np.float32))
        for index in range(count)
    )


def _echo_frames(payload: bytes, count: int = 60, *, delay_frames: int = 0) -> tuple:
    """与采集内容一致的参考帧（模拟"麦克风听到的就是我们自己放的"）。"""

    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    start = int(time.monotonic() * 1000.0) - delay_frames * 20
    return tuple((start + index * 20, samples.copy()) for index in range(count))


class NoiseReference(FakeReference):
    """可用（同钟、非静音、低相关）参考：允许降音量，但几乎不折减人声得分。"""

    def __init__(self, *, sample_rate: int = CAPTURE_SAMPLE_RATE_HZ, frames: int = 80) -> None:
        super().__init__(sample_rate=sample_rate, frames=_noise_frames(frames))


class EchoReference(FakeReference):
    """回声参考：内容与采集一致 → 高相关性（用于验证折减与"判别依据"）。"""

    def __init__(self, payload: bytes, *, count: int = 80, delay_frames: int = 0) -> None:
        super().__init__(frames=_echo_frames(payload, count, delay_frames=delay_frames))


ZERO_WINDOW = lambda: FakeWindow(pcm=np.zeros(320, dtype=np.float32))  # noqa: E731


def build_channel(
    environment: FakeEnvironment,
    *,
    config: BargeInChannelConfig | None = None,
    reference: object | None = None,
    detector: object | None = None,
    gain: list[float] | None = None,
) -> BargeInChannel:
    gain_sink = None if gain is None else gain.append
    # 测试里把对齐估计间隔压小，好在少量帧内就能得到"可用参考"（生产默认 25 帧 = 0.5 s）。
    if config is None:
        config = BargeInChannelConfig(alignment_interval_frames=5)
    else:
        config = replace(config, alignment_interval_frames=5)
    return BargeInChannel(
        environment=environment,
        config=config,
        reference=reference,
        voice_detector=detector,
        gain_sink=gain_sink,
    )


def feed(channel: BargeInChannel, environment: FakeEnvironment, payload: bytes, count: int) -> None:
    for _ in range(count):
        environment.capture.push(payload)
        channel.observe_once()


def frames_to_confirm(channel: BargeInChannel) -> int:
    """凑满确认所需的帧数：先满足"最小连续疑似帧数"，再在窗口里凑满 confirm_frames。"""

    config = channel.config.barge_in
    return config.min_suspect_frames - 1 + config.confirm_frames


class BargeInChannelConfigTests(unittest.TestCase):
    def test_defaults_keep_the_design_ranges(self) -> None:
        config = BargeInChannelConfig()
        self.assertEqual(config.degradation, "full")
        self.assertEqual(config.to_details()["frame_duration_ms"], FRAME_MS)
        self.assertGreaterEqual(config.pre_roll_frames * FRAME_MS, 300)
        self.assertEqual(config.capacity_frames, 512)

    def test_frame_duration_must_match_the_capture_clock(self) -> None:
        from app.duplex.barge_in import BargeInConfig

        with self.assertRaises(ContractError):
            BargeInChannelConfig(barge_in=BargeInConfig(frame_duration_ms=10))

    def test_invalid_values_are_rejected(self) -> None:
        cases = (
            {"degradation": "loud"},
            {"probability_mode": "magic"},
            {"capacity_frames": 0},
            {"max_monitor_seconds": 0.0},
            {"max_monitor_seconds": float("nan")},
            {"energy_threshold": -1.0},
            {"reference_sample_rate": 0},
            {"barge_in": "nope"},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ContractError):
                    BargeInChannelConfig(**kwargs)  # type: ignore[arg-type]

    def test_details_have_no_audio_payload(self) -> None:
        details = BargeInChannelConfig().to_details()
        self.assertNotIn("pcm", details)
        self.assertEqual(details["probability_mode"], PROBABILITY_MODE_ENERGY)


class BargeInChannelArmTests(unittest.TestCase):
    def test_arm_without_capture_factory_reports_unavailable(self) -> None:
        channel = build_channel(FakeEnvironment(missing_factory=True))
        self.assertFalse(channel.arm())
        self.assertFalse(channel.armed)
        self.assertEqual(channel.reason, REASON_CAPTURE_UNAVAILABLE)

    def test_arm_with_factory_error_never_raises(self) -> None:
        channel = build_channel(FakeEnvironment(raise_on_factory=True))
        self.assertFalse(channel.arm())
        self.assertEqual(channel.reason, REASON_CAPTURE_UNAVAILABLE)

    def test_arm_with_capture_that_does_not_start_reports_unavailable(self) -> None:
        environment = FakeEnvironment(started=False)
        channel = build_channel(environment)
        self.assertFalse(channel.arm())
        self.assertEqual(channel.reason, REASON_CAPTURE_UNAVAILABLE)
        self.assertEqual(environment.capture.stopped[0], CHANNEL_STOP_REASON)
        self.assertTrue(environment.capture.stopped[1])

    def test_successful_arm_uses_its_own_audio_identity(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        self.assertTrue(channel.arm())
        self.assertTrue(channel.armed)
        self.assertEqual(channel.reason, REASON_ARMED)
        bus = environment.capture.bus
        self.assertEqual(bus.audio_session_id, CHANNEL_AUDIO_SESSION_ID)
        self.assertEqual(bus.stream_id, CHANNEL_STREAM_ID)

    def test_arm_is_idempotent(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        self.assertTrue(channel.arm())
        self.assertTrue(channel.arm())
        self.assertEqual(environment.capture.starts, 1)

    def test_disarm_stops_capture_with_discard_and_is_idempotent(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        channel.disarm()
        self.assertFalse(channel.armed)
        self.assertEqual(channel.reason, REASON_DISARMED)
        self.assertEqual(environment.capture.stopped, (CHANNEL_STOP_REASON, True))
        channel.disarm()

    def test_observe_before_arm_is_a_noop_with_fixed_reason(self) -> None:
        channel = build_channel(FakeEnvironment())
        self.assertIsNone(channel.observe_once())
        self.assertEqual(channel.reason, REASON_NOT_ARMED)
        self.assertEqual(channel.frames, 0)


class BargeInChannelStageTests(unittest.TestCase):
    def _channel_with_reference(
        self, environment: FakeEnvironment, gain: list[float] | None = None
    ) -> BargeInChannel:
        """带**可用**回声参考的通道：只有这种条件下才允许第一阶段降音量。

        用"同钟、非静音但与采集不相关"的参考（`NoiseReference`）：对齐估计给出 ``ok``
        状态 + 极低相关性，于是既允许降音量、又几乎不折减人声得分。
        """

        return build_channel(environment, reference=NoiseReference(), gain=gain)

    def test_single_frame_no_longer_ducks_before_the_minimum_run(self) -> None:
        """真机教训：单帧即降音量会被回声反复触发；现在要连续 3 帧才降。"""

        environment = FakeEnvironment()
        gain: list[float] = []
        channel = self._channel_with_reference(environment, gain)
        channel.arm()
        feed(channel, environment, speech_payload(), 1)
        self.assertIs(channel.state, BargeInState.IDLE)
        self.assertEqual(channel.level, 1.0)
        self.assertEqual(gain, [1.0])

    def test_three_consecutive_frames_duck_and_create_no_turn(self) -> None:
        environment = FakeEnvironment()
        gain: list[float] = []
        payload = speech_payload()
        # 回声参考：相关性接近 1 → 得分被折减到 ~0，因此会停在"疑似"而不会确认。
        channel = build_channel(environment, reference=EchoReference(payload), gain=gain)
        channel.arm()
        feed(channel, environment, payload, 20)
        self.assertIs(channel.state, BargeInState.SUSPECTED)
        self.assertAlmostEqual(channel.level, channel.config.barge_in.duck_fraction)
        self.assertFalse(channel.confirmed)
        self.assertEqual(gain[-1], channel.config.barge_in.duck_fraction)

    def test_sustained_voice_confirms_and_zeroes_the_level(self) -> None:
        environment = FakeEnvironment()
        gain: list[float] = []
        channel = build_channel(environment, gain=gain)
        channel.arm()
        needed = frames_to_confirm(channel)
        feed(channel, environment, speech_payload(), needed)
        self.assertTrue(channel.confirmed)
        self.assertIs(channel.state, BargeInState.CONFIRMED)
        self.assertEqual(channel.level, 0.0)
        self.assertEqual(channel.reason, REASON_CONFIRMED)
        self.assertEqual(gain[-1], 0.0)
        self.assertEqual(channel.frames, needed)

    def test_false_trigger_releases_back_to_full_volume(self) -> None:
        environment = FakeEnvironment()
        payload = speech_payload()
        channel = build_channel(environment, reference=EchoReference(payload))
        channel.arm()
        feed(channel, environment, payload, 20)
        self.assertIs(channel.state, BargeInState.SUSPECTED)
        # 疑似后回落：先按 release_ms 保持降音量，再平滑（restore_ms/fade_steps）回到原音量。
        # 回落期用**低电平但仍有方差**的帧（保持"参考仍可用"，否则按设计立即回到原音量）。
        quiet = speech_payload(amplitude=150)
        deadline = channel.config.barge_in.release_ms // FRAME_MS + 10
        for _ in range(deadline):
            environment.capture.push(quiet)
            channel.observe_once()
            if channel.state is BargeInState.IDLE:
                break
        self.assertIs(channel.state, BargeInState.IDLE)
        self.assertFalse(channel.confirmed)
        # 回到 IDLE 的这一刻音量仍在平滑回升途中（不是跳变回 1.0）。
        self.assertLess(channel.level, 1.0)
        for _ in range(channel.controller.fade_steps + 1):
            environment.capture.push(quiet)
            channel.observe_once()
        self.assertEqual(channel.level, 1.0)

    def test_silence_never_ducks(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        feed(channel, environment, silence_payload(), 5)
        self.assertIs(channel.state, BargeInState.IDLE)
        self.assertEqual(channel.level, 1.0)

    def test_energy_probability_is_bounded_and_honest(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(
            environment,
            config=BargeInChannelConfig(energy_threshold=600.0),
            reference=NoiseReference(),
        )
        channel.arm()
        environment.capture.push(silence_payload())
        channel.observe_once()
        self.assertEqual(channel.frames, 1)
        # RMS=600 时概率恰好 1.0（上限），不会超过 1.0 → 连续 3 帧即进入疑似。
        for _ in range(channel.config.barge_in.min_suspect_frames):
            environment.capture.push(speech_payload(amplitude=600))
            channel.observe_once()
        self.assertIs(channel.state, BargeInState.SUSPECTED)


class BargeInChannelPreRollTests(unittest.TestCase):
    def test_pre_roll_is_bounded_by_config_capacity(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        feed(channel, environment, silence_payload(), 50)
        facts = channel.pre_roll_facts()
        self.assertLessEqual(facts["frames"], channel.config.barge_in.pre_roll_frames)
        self.assertGreaterEqual(facts["duration_ms"], 300)

    def test_take_pre_roll_returns_bytes_then_clears(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        feed(channel, environment, speech_payload(), 3)
        data = channel.take_pre_roll_pcm()
        self.assertEqual(len(data), 3 * CAPTURE_FRAME_BYTES)
        self.assertEqual(channel.pre_roll_pcm(), b"")

    def test_details_expose_counts_only(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        feed(channel, environment, speech_payload(), 2)
        details = channel.to_details()
        self.assertEqual(details["frames"], 2)
        self.assertEqual(details["pre_roll_frames"], 2)
        self.assertNotIn("pcm", details)
        self.assertNotIn("payload", details)


class BargeInChannelRunTests(unittest.TestCase):
    def test_run_returns_when_playback_already_ended(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        outcome = channel.run(is_playback_done=lambda: True, max_seconds=1.0)
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome.handoff_to_turn)
        self.assertEqual(channel.reason, REASON_PLAYBACK_ENDED)
        self.assertEqual(channel.level, 1.0)

    def test_run_returns_confirmed_outcome(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        needed = frames_to_confirm(channel)
        state = {"done": False}
        pushed = {"n": 0}

        def is_done() -> bool:
            if pushed["n"] < needed:
                environment.capture.push(speech_payload())
                pushed["n"] += 1
            return state["done"]

        outcome = channel.run(is_playback_done=is_done, max_seconds=2.0)
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome.handoff_to_turn)
        self.assertEqual(channel.reason, REASON_CONFIRMED)

    def test_run_returns_none_when_the_bus_dies(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        environment.capture.close_bus()
        self.assertIsNone(channel.run(is_playback_done=lambda: False, max_seconds=1.0))
        self.assertEqual(channel.reason, REASON_CAPTURE_STOPPED)

    def test_run_returns_on_timeout_without_faking_a_decision(self) -> None:
        environment = FakeEnvironment()
        clock_values = iter([0.0, 5.0, 5.0, 5.0])
        channel = BargeInChannel(
            environment=environment,
            config=BargeInChannelConfig(max_monitor_seconds=0.01),
            clock=lambda: next(clock_values, 5.0),
            sleep=lambda _seconds: None,
        )
        channel.arm()
        outcome = channel.run(is_playback_done=lambda: False, max_seconds=0.01)
        self.assertIsNone(outcome)
        self.assertEqual(channel.reason, "monitor_timeout")

    def test_request_stop_ends_the_loop(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        channel.request_stop()
        self.assertIsNone(channel.run(is_playback_done=lambda: False, max_seconds=1.0))

    def test_run_rejects_invalid_arguments(self) -> None:
        channel = build_channel(FakeEnvironment())
        with self.assertRaises(ContractError):
            channel.run(is_playback_done="nope")  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            channel.run(is_playback_done=lambda: False, max_seconds=0.0)

    def test_run_without_arm_returns_none(self) -> None:
        channel = build_channel(FakeEnvironment())
        self.assertIsNone(channel.run(is_playback_done=lambda: False, max_seconds=1.0))


class BargeInChannelReferenceTests(unittest.TestCase):
    def test_no_reference_is_reported_as_none(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        feed(channel, environment, silence_payload(), 1)
        self.assertEqual(channel.to_details()["reference_state"], REFERENCE_SOURCE_NONE)

    def test_reference_with_a_different_clock_is_reported_and_unusable(self) -> None:
        environment = FakeEnvironment()
        reference = FakeReference(sample_rate=24_000)
        channel = build_channel(environment, reference=reference)
        channel.arm()
        feed(channel, environment, speech_payload(), 1)
        details = channel.to_details()
        self.assertEqual(details["reference_state"], REFERENCE_SOURCE_CLOCK_MISMATCH)
        self.assertIsNone(details["echo_correlation"])
        self.assertEqual(reference.calls, [])

    def test_reference_with_broken_timeline_is_reported_as_unavailable(self) -> None:
        environment = FakeEnvironment()
        reference = FakeReference(raise_on_timeline=True)
        channel = build_channel(environment, reference=reference)
        channel.arm()
        feed(channel, environment, speech_payload(), 10)
        self.assertGreaterEqual(channel.errors, 1)
        self.assertIsNone(channel.to_details()["echo_correlation"])

    def test_aligned_echo_reference_yields_a_high_correlation(self) -> None:
        """同钟 + 内容一致的参考 → 对齐估计应给出高相关性（"判别依据"真的拿到了）。"""

        payload = speech_payload()
        environment = FakeEnvironment()
        reference = EchoReference(payload)
        channel = build_channel(environment, reference=reference)
        channel.arm()
        feed(channel, environment, payload, 25)
        details = channel.to_details()
        self.assertEqual(details["reference_state"], "ok")
        self.assertGreater(float(details["echo_correlation"]), 0.5)
        self.assertIsNotNone(details["echo_delay_ms"])
        self.assertGreaterEqual(details["alignment_estimates"], 1)

    def test_echo_correlation_discounts_the_score(self) -> None:
        """相关性越高越像"自己在说话"：同样的人声不再能确认。"""

        def run_with(reference: object, payload: bytes) -> bool:
            environment = FakeEnvironment()
            channel = build_channel(environment, reference=reference)
            channel.arm()
            needed = frames_to_confirm(channel) + 2
            for _ in range(needed):
                environment.capture.push(payload)
                channel.observe_once()
            return channel.confirmed

        payload = speech_payload()
        # 参考与采集不相关（环境噪声）→ 得分不被折减 → 确认；
        self.assertTrue(run_with(NoiseReference(), payload))
        # 参考就是采集本身（回声）→ 相关性接近 1 → 得分被折减到 ~0 → 不确认。
        self.assertFalse(run_with(EchoReference(payload), payload))


class BargeInChannelDegradationTests(unittest.TestCase):
    def test_warmup_without_correlation_tightens_the_confirm_bar(self) -> None:
        """如实记录一个**保守**行为：暖机期没有相关性时，R1a 把 full 收紧为 strict。

        通道在拿到第一个对齐结论之前（约 0.3~0.5 s）不能给出相关性，R1a 会把
        ``full`` 自动降为 ``strict``（门槛 0.7 → 0.9），且该收紧对本次 episode 持续有效。
        保守方向符合设计文档 12，但断言必须如实反映：0.8 的人声不再确认，0.95 才确认。
        """

        def confirms(amplitude: int) -> bool:
            environment = FakeEnvironment()
            channel = build_channel(
                environment,
                config=BargeInChannelConfig(
                    degradation="full",
                    energy_threshold=600.0,
                    probability_mode=PROBABILITY_MODE_ENERGY,
                ),
                reference=NoiseReference(),
            )
            channel.arm()
            for _ in range(20):  # 暖机：低电平、不触发疑似
                environment.capture.push(speech_payload(amplitude=150))
                channel.observe_once()
            frames = channel.config.barge_in.confirm_frames + 5
            for _ in range(frames):
                environment.capture.push(speech_payload(amplitude=amplitude))
                channel.observe_once()
            return channel.confirmed

        self.assertFalse(confirms(480))  # 0.8 < 收紧后的 0.9
        self.assertTrue(confirms(570))  # 0.95 ≥ 0.9

    def test_missing_reference_auto_degrades_to_strict(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        # 缺少参考时 R1a 会把 full 降为 strict：0.8 的人声不再能确认。
        frames = channel.config.barge_in.confirm_frames + 5
        for _ in range(frames):
            environment.capture.push(speech_payload(amplitude=480))
            channel.observe_once()
        self.assertFalse(channel.confirmed)
        self.assertEqual(channel.controller._controller.degradation, DEGRADE_STRICT)  # noqa: SLF001

    def test_no_aec_degradation_is_accepted(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(
            environment, config=BargeInChannelConfig(degradation=DEGRADE_NO_AEC)
        )
        channel.arm()
        feed(channel, environment, speech_payload(), 3)
        self.assertTrue(channel.armed)

    def test_unknown_degradation_is_rejected(self) -> None:
        environment = FakeEnvironment()
        with self.assertRaises(ContractError):
            build_channel(environment, config=BargeInChannelConfig(degradation="loud"))


class BargeInChannelDetectorTests(unittest.TestCase):
    def test_detector_mode_uses_the_injected_detector(self) -> None:
        environment = FakeEnvironment()
        seen: list[object] = []
        channel = build_channel(
            environment,
            config=BargeInChannelConfig(probability_mode=PROBABILITY_MODE_DETECTOR),
            detector=lambda packet: seen.append(packet) or True,
            reference=NoiseReference(),
        )
        channel.arm()
        feed(channel, environment, speech_payload(), channel.config.barge_in.min_suspect_frames)
        self.assertEqual(len(seen), channel.config.barge_in.min_suspect_frames)
        self.assertIs(channel.state, BargeInState.SUSPECTED)

    def test_detector_hit_below_the_energy_gate_is_not_voice(self) -> None:
        """真机教训：只有检测器时回声也会被判成连续人声；必须叠加能量门。"""

        environment = FakeEnvironment()
        channel = build_channel(
            environment,
            config=BargeInChannelConfig(probability_mode=PROBABILITY_MODE_DETECTOR),
            detector=lambda packet: True,
            reference=NoiseReference(),
        )
        channel.arm()
        # 检测器恒真，但电平远低于能量门（回声本机实测就是这个量级）。
        feed(channel, environment, noise_payload(amplitude=150), 40)
        self.assertIs(channel.state, BargeInState.IDLE)
        self.assertFalse(channel.confirmed)
        self.assertEqual(channel.level, 1.0)

    def test_detector_hit_above_the_energy_gate_confirms(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(
            environment,
            config=BargeInChannelConfig(probability_mode=PROBABILITY_MODE_DETECTOR),
            detector=lambda packet: True,
            reference=NoiseReference(),
        )
        channel.arm()
        feed(channel, environment, speech_payload(), frames_to_confirm(channel))
        self.assertTrue(channel.confirmed)

    def test_detector_failure_never_lets_a_barge_in_through(self) -> None:
        environment = FakeEnvironment()

        def boom(_packet: object) -> bool:
            raise RuntimeError("detector exploded")

        channel = build_channel(
            environment,
            config=BargeInChannelConfig(probability_mode=PROBABILITY_MODE_DETECTOR),
            detector=boom,
        )
        channel.arm()
        feed(channel, environment, speech_payload(), 20)
        self.assertFalse(channel.confirmed)
        self.assertIs(channel.state, BargeInState.IDLE)
        self.assertGreaterEqual(channel.errors, 20)

    def test_detector_mode_without_detector_falls_back_to_energy_and_counts_an_error(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(
            environment, config=BargeInChannelConfig(probability_mode=PROBABILITY_MODE_DETECTOR)
        )
        channel.arm()
        feed(channel, environment, speech_payload(), 1)
        self.assertEqual(channel.errors, 1)


class BargeInNoReferenceDuckTests(unittest.TestCase):
    """真机缺陷回归（2026-10-02）：没有可用回声参考时，第一阶段降音量必须关闭。

    真机现象：扬声器回声造成零星人声命中 → 反复"降音量→回升"，听感是音量一直在变，
    而 13 帧连续命中又凑不满 → 永不确认。离线只有合成信号，没有回声源，所以这条只能
    用"参考不可用"的条件把该分支钉住。
    """

    def test_duck_is_disabled_without_a_usable_reference(self) -> None:
        environment = FakeEnvironment()
        gain: list[float] = []
        channel = build_channel(environment, gain=gain)
        channel.arm()
        feed(channel, environment, speech_payload(), channel.config.barge_in.confirm_frames)
        # 全程不降音量：应用过的音量只有 1.0。
        self.assertEqual(set(gain), {1.0})
        self.assertTrue(channel.duck_suppressed)
        details = channel.to_details()
        self.assertIs(details["duck_suppressed"], True)
        self.assertEqual(details["duck_suppressed_reason"], "duck_disabled_no_reference")

    def test_confirm_still_works_without_a_reference(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment)
        channel.arm()
        feed(channel, environment, speech_payload(), frames_to_confirm(channel))
        self.assertTrue(channel.confirmed)
        self.assertEqual(channel.level, 0.0)

    def test_clock_mismatch_reference_is_treated_as_unusable(self) -> None:
        environment = FakeEnvironment()
        channel = build_channel(environment, reference=FakeReference(sample_rate=24_000))
        channel.arm()
        feed(channel, environment, speech_payload(), channel.config.barge_in.min_suspect_frames)
        self.assertTrue(channel.duck_suppressed)
        self.assertEqual(channel.level, 1.0)

    def test_explicit_opt_out_restores_the_duck_stage(self) -> None:
        environment = FakeEnvironment()
        gain: list[float] = []
        channel = build_channel(
            environment,
            config=BargeInChannelConfig(duck_requires_reference=False),
            gain=gain,
        )
        channel.arm()
        feed(channel, environment, speech_payload(), channel.config.barge_in.min_suspect_frames)
        self.assertAlmostEqual(channel.level, channel.config.barge_in.duck_fraction)
        self.assertFalse(channel.duck_suppressed)

    def test_sporadic_echo_bursts_never_pump_the_volume(self) -> None:
        """复现真机场景：播报期间短促回声命中不断出现，音量必须保持恒定。"""

        environment = FakeEnvironment()
        gain: list[float] = []
        channel = build_channel(environment, gain=gain)
        channel.arm()
        # 每 10 帧里出现 2 帧短促人声（不足 min_suspect_frames=3，且远不够确认）。
        for _ in range(20):
            for _ in range(2):
                environment.capture.push(speech_payload())
                channel.observe_once()
            for _ in range(8):
                environment.capture.push(silence_payload())
                channel.observe_once()
        self.assertFalse(channel.confirmed)
        self.assertEqual(set(gain), {1.0})
        self.assertEqual(channel.level, 1.0)
        # 短促回声连"最小连续疑似帧数"都凑不满 → 根本没到降音量那一步（无需抑制）。
        self.assertFalse(channel.duck_suppressed)

    def test_duck_still_applies_when_a_usable_reference_exists(self) -> None:
        environment = FakeEnvironment()
        gain: list[float] = []
        channel = build_channel(environment, reference=NoiseReference(), gain=gain)
        channel.arm()
        # 先用低电平帧把对齐暖机到 ok（不触发疑似）；随后高电平帧进入疑似并降音量。
        feed(channel, environment, speech_payload(amplitude=150), 15)
        self.assertFalse(channel.duck_suppressed)
        confirm_frames = channel.config.barge_in.confirm_frames
        feed(channel, environment, speech_payload(), confirm_frames)  # 差 1 帧不确认
        self.assertFalse(channel.confirmed)
        self.assertAlmostEqual(channel.level, channel.config.barge_in.duck_fraction)
        self.assertIn(channel.config.barge_in.duck_fraction, gain)


class BargeInChannelBoundaryTests(unittest.TestCase):
    def _modules(self) -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        return modules

    def test_never_imports_the_safety_chain(self) -> None:
        banned = {"app.safety", "app.executor", "app.plans", "app.auditing", "app.tools"}
        offenders = {
            name for name in self._modules() if ".".join(name.split(".")[:2]) in banned
        }
        self.assertEqual(offenders, set())

    def test_never_reuses_the_keyboard_interrupt_source(self) -> None:
        # 允许在文档里说明"不复用"，但绝不允许 import app.main 或读取那个轮询口。
        self.assertNotIn("app.main", self._modules())
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        self.assertNotIn("interrupt_source", names)
        self.assertNotIn("_keyboard_interrupt_source", names)

    def test_channel_does_not_touch_app_main(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("_keyboard_interrupt_source", source)


if __name__ == "__main__":
    unittest.main()
