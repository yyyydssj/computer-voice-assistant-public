"""P10 批次 R2a：AEC/降噪的**逐层降级框架**（纯离线）。

本模块只回答一个问题：当前设备与参考信号条件下**允许用哪一级处理链**，以及不可用时
如何如实降级。它不实现任何真实的音频处理、不访问设备，也不把"没做事"说成"已消除回声"。

设计依据 `implementation_plan/12-barge-in-and-aec.md`：
- 处理链原则上为 AEC/回声抑制 → 降噪（NS）→ 自动增益（AGC，谨慎）→ VAD/插话判断；
- 优先用真正送往输出设备的 TTS PCM 作为参考信号，而不是重新合成或估算；
- 设备切换后旧延迟估计失效，必须从下一轮重新校准；
- AEC 不可用或效果不足时**不得伪装**已可靠消除回声，降级到"播放状态 + 回声相关性 +
  更严格持续人声阈值 + 两阶段降音量"；
- 降级会降低打断灵敏度，但不得自动关闭实时会话，**也不得把扬声器回声提交给 Agent**。

硬边界：不访问设备/文件/网络、不创建线程、不导入安全链模块、不导入音频实现库。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.base import ContractError, require_positive
from app.duplex.barge_in import DEGRADE_FULL, DEGRADE_NO_AEC, DEGRADE_STRICT

# 处理链级别（从强到弱）
LEVEL_FULL = "aec_ns"
LEVEL_AEC_ONLY = "aec_only"
LEVEL_STRICT = "strict_no_aec"
LEVEL_HALF_DUPLEX = "half_duplex"

# 能力
CAPABILITY_HARDWARE = "hardware"
CAPABILITY_SOFTWARE = "software"
CAPABILITY_UNAVAILABLE = "unavailable"

# 选择原因（固定短代码）
REASON_FULL_CHAIN = "full_chain_available"
REASON_AEC_WITHOUT_NS = "noise_suppression_unavailable"
REASON_ECHO_PATH_EXCEEDS_BUDGET = "echo_path_exceeds_aec_budget"
REASON_REVERB_RISK = "echo_risk_without_safe_delay"
REASON_REFERENCE_MISSING = "speaker_reference_missing"
REASON_DEVICE_CHANGED = "device_changed_recalibration_required"
REASON_UNRELIABLE_DELAY = "device_delay_unreliable"

_ERROR_CODES = frozenset({"unknown_capability", "invalid_report_state"})

DEGRADATION_BY_LEVEL = {
    LEVEL_FULL: DEGRADE_FULL,
    LEVEL_AEC_ONLY: DEGRADE_FULL,
    LEVEL_STRICT: DEGRADE_NO_AEC,
    LEVEL_HALF_DUPLEX: DEGRADE_STRICT,
}


class AecError(ContractError):
    """AEC 能力/降级判定拒绝输入；``str()`` 只返回固定短代码。"""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "unknown_capability"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AecPolicy:
    """降级策略参数；默认值与设计文档 12 的"更严格"方向一致。"""

    noise_suppression_available: bool = True
    delay_estimate_reliable: bool = True
    max_echo_path_ms: int = 120
    frame_duration_ms: int = 20

    def __post_init__(self) -> None:
        require_positive("max_echo_path_ms", self.max_echo_path_ms)
        require_positive("frame_duration_ms", self.frame_duration_ms)
        for name in ("noise_suppression_available", "delay_estimate_reliable"):
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"{name} 必须是布尔值")


@dataclass(frozen=True, slots=True)
class AecCapabilityProbe:
    """一次能力探测的输入：设备与参考信号现状（不含音频载荷）。"""

    aec_capability: str = CAPABILITY_UNAVAILABLE
    reference_available: bool = False
    reference_matches_output: bool = False
    device_changed: bool = False
    estimated_echo_path_ms: int = 0
    device_epoch: int = 1

    def __post_init__(self) -> None:
        if self.aec_capability not in (
            CAPABILITY_HARDWARE,
            CAPABILITY_SOFTWARE,
            CAPABILITY_UNAVAILABLE,
        ):
            raise AecError("unknown_capability")
        for name in ("reference_available", "reference_matches_output", "device_changed"):
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"{name} 必须是布尔值")
        if isinstance(self.estimated_echo_path_ms, bool) or not isinstance(
            self.estimated_echo_path_ms, int
        ):
            raise ContractError("estimated_echo_path_ms 必须是整数毫秒")
        if self.estimated_echo_path_ms < 0:
            raise ContractError("estimated_echo_path_ms 必须非负")
        require_positive("device_epoch", self.device_epoch)


@dataclass(frozen=True, slots=True)
class AecProcessingPlan:
    """一次降级判定的结果：用哪一级、为什么、以及必须遵守的边界。"""

    level: str
    reason: str
    aec_applied: bool
    noise_suppression_applied: bool
    barge_in_degradation: str
    pause_capture_during_playback: bool
    echo_risk: bool
    realtime_conversation_available: bool
    text_mode_available: bool
    device_epoch: int

    @property
    def echo_cancellation_claimed(self) -> bool:
        """只有真正应用了 AEC 才允许声称"已消除回声"。"""

        return self.aec_applied

    def to_details(self) -> dict[str, object]:
        return {
            "level": self.level,
            "reason": self.reason,
            "aec_applied": self.aec_applied,
            "noise_suppression_applied": self.noise_suppression_applied,
            "barge_in_degradation": self.barge_in_degradation,
            "pause_capture_during_playback": self.pause_capture_during_playback,
            "echo_risk": self.echo_risk,
            "realtime_conversation_available": self.realtime_conversation_available,
            "text_mode_available": self.text_mode_available,
            "device_epoch": self.device_epoch,
        }


class AecDegradationController:
    """逐层降级判定（纯离线、确定性）。

    降级顺序：``aec_ns`` → ``aec_only`` → ``strict_no_aec`` → ``half_duplex``。
    任何一级都保留文字模式；``half_duplex``（播放期间暂停收音）是最后兜底，仍不关闭会话。
    """

    def __init__(self, *, policy: AecPolicy | None = None) -> None:
        self._policy = policy or AecPolicy()
        self._last_device_epoch: int | None = None

    @property
    def policy(self) -> AecPolicy:
        return self._policy

    @property
    def last_device_epoch(self) -> int | None:
        return self._last_device_epoch

    def plan(self, probe: AecCapabilityProbe) -> AecProcessingPlan:
        """给出当前条件允许的处理级别；**不**猜测、**不**夸大能力。"""

        if not isinstance(probe, AecCapabilityProbe):
            raise ContractError("probe 必须是 AecCapabilityProbe")

        has_aec = probe.aec_capability != CAPABILITY_UNAVAILABLE
        reference_ok = probe.reference_available and probe.reference_matches_output
        echo_risk = probe.estimated_echo_path_ms > 0
        device_changed = probe.device_changed or (
            self._last_device_epoch is not None and probe.device_epoch != self._last_device_epoch
        )
        self._last_device_epoch = probe.device_epoch

        # 前置条件：延迟估计不可靠时，任何 AEC 都无法正确对齐参考信号 → 直接退半双工。
        if echo_risk and not self._policy.delay_estimate_reliable:
            return self._build(LEVEL_HALF_DUPLEX, REASON_UNRELIABLE_DELAY, probe, aec_applied=False)

        if has_aec and reference_ok and not device_changed:
            if echo_risk and probe.estimated_echo_path_ms > self._policy.max_echo_path_ms:
                # AEC 在跑，但回声路径超出它能覆盖的预算：不谎称"已消除回声"，
                # 退到更保守的严格级别（收紧打断灵敏度），仍不关闭实时会话。
                return self._build(
                    LEVEL_STRICT, REASON_ECHO_PATH_EXCEEDS_BUDGET, probe, aec_applied=False
                )
            level = LEVEL_FULL if self._policy.noise_suppression_available else LEVEL_AEC_ONLY
            reason = REASON_FULL_CHAIN if level == LEVEL_FULL else REASON_AEC_WITHOUT_NS
            return self._build(level, reason, probe, aec_applied=True)

        # 以下全部是"没有可用 AEC"的分支：绝不允许声称已消除回声。
        if device_changed:
            return self._build(LEVEL_STRICT, REASON_DEVICE_CHANGED, probe, aec_applied=False)
        if echo_risk and probe.estimated_echo_path_ms > self._policy.max_echo_path_ms:
            return self._build(LEVEL_HALF_DUPLEX, REASON_REVERB_RISK, probe, aec_applied=False)
        if not reference_ok:
            return self._build(LEVEL_STRICT, REASON_REFERENCE_MISSING, probe, aec_applied=False)
        return self._build(LEVEL_STRICT, REASON_REVERB_RISK, probe, aec_applied=False)

    def _build(
        self,
        level: str,
        reason: str,
        probe: AecCapabilityProbe,
        *,
        aec_applied: bool,
    ) -> AecProcessingPlan:
        half_duplex = level == LEVEL_HALF_DUPLEX
        return AecProcessingPlan(
            level=level,
            reason=reason,
            aec_applied=aec_applied,
            noise_suppression_applied=aec_applied and self._policy.noise_suppression_available,
            barge_in_degradation=DEGRADATION_BY_LEVEL[level],
            pause_capture_during_playback=half_duplex,
            echo_risk=probe.estimated_echo_path_ms > 0,
            realtime_conversation_available=not half_duplex,
            text_mode_available=True,
            device_epoch=probe.device_epoch,
        )
