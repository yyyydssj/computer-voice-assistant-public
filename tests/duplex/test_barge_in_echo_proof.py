"""P10 R4-B：**回声可观测性判定收紧**的离线测试（真机假证明的回归）。

真机事实（2026-10-03，最后一次真机会话）：本机相关性能到 0.60~0.75，但估计延迟出现
**746 / 1589 / −780 ms**（本机实测参考→采集延迟只有 ≈390–410 ms）。旧门槛"只看相关性 ≥ 0.2"
因此被伪相关骗过 → `echo_proven=True` → R4-A 安全门放行 → **用户没插话也停播**（turn 2/4）。

收紧后的判定（三条同时满足才算证明；**只收紧、不放松**）：
1. 对齐状态 ``ok`` 且相关性 ≥ ``echo_proof_correlation``；
2. 延迟落在 ``[echo_proof_delay_min_ms, echo_proof_delay_max_ms]``；
3. 连续 ``echo_proof_consecutive`` 次成立且相邻延迟差 ≤ ``echo_proof_delay_tolerance_ms``。

全部离线：假对齐结果（`types.SimpleNamespace`）+ 假环境，不打开设备、不放音。
"""

from __future__ import annotations

import types
import unittest

from app.contracts.base import ContractError
from app.duplex.barge_in_channel import (
    PROOF_STATE_ACCUMULATING,
    PROOF_STATE_ALIGNMENT_NOT_OK,
    PROOF_STATE_DELAY_IMPLAUSIBLE,
    PROOF_STATE_LOW_CORRELATION,
    PROOF_STATE_NO_ESTIMATE,
    PROOF_STATE_PROVEN,
    BargeInChannel,
    BargeInChannelConfig,
)


def build_channel(**overrides: object) -> BargeInChannel:
    config = BargeInChannelConfig(**overrides)  # type: ignore[arg-type]
    return BargeInChannel(environment=types.SimpleNamespace(), config=config, clock=lambda: 1000.0)


def estimate(channel: BargeInChannel, *, correlation: float, delay_ms: int, state: str = "ok") -> None:
    """喂一次对齐估计（等价于 `_observe_alignment()` 里算出 `self._alignment` 的那一步）。"""

    channel._alignment = types.SimpleNamespace(  # noqa: SLF001 - 契约测试直接注入对齐结果
        state=state, correlation=correlation, delay_ms=delay_ms
    )
    channel._note_proof_estimate()  # noqa: SLF001


class EchoProofConfigTests(unittest.TestCase):
    def test_defaults_are_tight_and_documented(self) -> None:
        config = BargeInChannelConfig()
        self.assertEqual(config.echo_proof_correlation, 0.2)
        self.assertEqual(config.echo_proof_delay_min_ms, 0)
        self.assertEqual(config.echo_proof_delay_max_ms, 500)
        self.assertEqual(config.echo_proof_consecutive, 3)
        self.assertEqual(config.echo_proof_delay_tolerance_ms, 50)

    def test_invalid_windows_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            BargeInChannelConfig(echo_proof_delay_min_ms=600, echo_proof_delay_max_ms=500)
        with self.assertRaises(ContractError):
            BargeInChannelConfig(echo_proof_delay_min_ms=-1)
        with self.assertRaises(ContractError):
            BargeInChannelConfig(echo_proof_delay_max_ms=-5)
        with self.assertRaises(ContractError):
            BargeInChannelConfig(echo_proof_consecutive=0)
        with self.assertRaises(ContractError):
            BargeInChannelConfig(echo_proof_delay_tolerance_ms=-1)

    def test_details_expose_the_proof_parameters(self) -> None:
        details = BargeInChannelConfig().to_details()
        for name in (
            "echo_proof_correlation",
            "echo_proof_delay_min_ms",
            "echo_proof_delay_max_ms",
            "echo_proof_consecutive",
            "echo_proof_delay_tolerance_ms",
        ):
            self.assertIn(name, details)


class RealMachineFalseProofRegressionTests(unittest.TestCase):
    """用真机实测的"伪证明"数据做回归：这些组合**永远不能**算证明。"""

    def test_real_machine_implausible_delays_never_prove(self) -> None:
        cases = [
            (0.6005, 746),   # turn 2
            (0.7172, -780),  # turn 4（负延迟）
            (0.7458, 1589),  # turn 3
        ]
        for correlation, delay in cases:
            with self.subTest(correlation=correlation, delay=delay):
                channel = build_channel(echo_proof_consecutive=1)
                for _ in range(10):  # 反复出现也不许证明
                    estimate(channel, correlation=correlation, delay_ms=delay)
                self.assertFalse(channel.echo_proven)
                self.assertEqual(channel.to_details()["echo_proof_state"], PROOF_STATE_DELAY_IMPLAUSIBLE)

    def test_real_machine_correlation_is_still_recorded_honestly(self) -> None:
        channel = build_channel()
        estimate(channel, correlation=0.6005, delay_ms=746)
        self.assertAlmostEqual(channel.max_echo_correlation, 0.6005, places=4)
        self.assertFalse(channel.echo_proven)


class PlausibleProofTests(unittest.TestCase):
    def test_stable_plausible_estimates_prove_after_three(self) -> None:
        channel = build_channel()
        estimate(channel, correlation=0.5, delay_ms=400)
        self.assertEqual(channel.to_details()["echo_proof_state"], PROOF_STATE_ACCUMULATING)
        estimate(channel, correlation=0.6, delay_ms=395)
        self.assertFalse(channel.echo_proven)
        estimate(channel, correlation=0.55, delay_ms=405)
        self.assertTrue(channel.echo_proven)
        self.assertEqual(channel.to_details()["echo_proof_state"], PROOF_STATE_PROVEN)

    def test_consecutive_one_proves_immediately(self) -> None:
        channel = build_channel(echo_proof_consecutive=1)
        estimate(channel, correlation=0.3, delay_ms=100)
        self.assertTrue(channel.echo_proven)

    def test_boundary_delays_are_accepted(self) -> None:
        for delay in (0, 500):
            with self.subTest(delay=delay):
                channel = build_channel(echo_proof_consecutive=1)
                estimate(channel, correlation=0.3, delay_ms=delay)
                self.assertTrue(channel.echo_proven)

    def test_one_millisecond_outside_the_window_is_rejected(self) -> None:
        for delay in (-1, 501):
            with self.subTest(delay=delay):
                channel = build_channel(echo_proof_consecutive=1)
                estimate(channel, correlation=0.3, delay_ms=delay)
                self.assertFalse(channel.echo_proven)
                self.assertEqual(
                    channel.to_details()["echo_proof_state"], PROOF_STATE_DELAY_IMPLAUSIBLE
                )


class ProofRejectionTests(unittest.TestCase):
    def test_low_correlation_is_rejected(self) -> None:
        channel = build_channel(echo_proof_consecutive=1)
        estimate(channel, correlation=0.19, delay_ms=400)
        self.assertFalse(channel.echo_proven)
        self.assertEqual(
            channel.to_details()["echo_proof_state"], PROOF_STATE_LOW_CORRELATION
        )

    def test_non_ok_alignment_state_is_rejected(self) -> None:
        channel = build_channel(echo_proof_consecutive=1)
        estimate(channel, correlation=0.9, delay_ms=400, state="history_short")
        self.assertFalse(channel.echo_proven)
        self.assertEqual(
            channel.to_details()["echo_proof_state"], PROOF_STATE_ALIGNMENT_NOT_OK
        )

    def test_unstable_delay_restarts_the_run(self) -> None:
        channel = build_channel()
        estimate(channel, correlation=0.5, delay_ms=400)
        estimate(channel, correlation=0.5, delay_ms=410)  # 累计 2
        estimate(channel, correlation=0.5, delay_ms=480)  # 差 70 > 50 → 重新计数
        self.assertFalse(channel.echo_proven)
        self.assertEqual(channel.to_details()["echo_proof_run"], 1)
        estimate(channel, correlation=0.5, delay_ms=485)
        estimate(channel, correlation=0.5, delay_ms=490)
        self.assertTrue(channel.echo_proven)

    def test_bad_estimate_resets_the_run(self) -> None:
        channel = build_channel()
        estimate(channel, correlation=0.5, delay_ms=400)
        estimate(channel, correlation=0.5, delay_ms=400)
        estimate(channel, correlation=0.05, delay_ms=400)  # 相关性掉下去 → 清零
        estimate(channel, correlation=0.5, delay_ms=400)
        self.assertFalse(channel.echo_proven)
        self.assertEqual(channel.to_details()["echo_proof_run"], 1)

    def test_proof_stays_true_after_it_is_earned(self) -> None:
        channel = build_channel(echo_proof_consecutive=1)
        estimate(channel, correlation=0.5, delay_ms=400)
        self.assertTrue(channel.echo_proven)
        estimate(channel, correlation=0.01, delay_ms=9000)  # 后面变坏也不撤销已得的证明
        self.assertTrue(channel.echo_proven)

    def test_details_start_with_no_estimate(self) -> None:
        channel = build_channel()
        details = channel.to_details()
        self.assertEqual(details["echo_proof_state"], PROOF_STATE_NO_ESTIMATE)
        self.assertEqual(details["echo_proof_run"], 0)
        self.assertIsNone(details["echo_proof_delay_ms"])
        for name in ("pcm", "payload", "audio", "samples"):
            self.assertNotIn(name, details)


if __name__ == "__main__":
    unittest.main()
