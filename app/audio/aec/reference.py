"""P10 批次 R2b-1：AEC 参考信号时间线（纯离线、可注入）。

设计文档 12 要求：**优先获取实际送往输出设备的 TTS PCM 作为 AEC 参考信号**，而不是重新
合成或估算参考音频；并对齐时间戳/采样率/设备延迟；设备切换后旧延迟估计失效，从下一轮重
新校准。

本模块只负责"参考帧的存放与按采集时刻对齐"：
- 参考帧（送往扬声器的 PCM16）按时间戳进入有界环形缓冲；
- 按 ``采集时刻 - 设备延迟`` 取出对齐窗口；
- 明确区分三种"取不到"：历史不足（``history_short``）、尚未到达（``future``）、
  超出缓冲（``too_old``）——一律**不得**当成"无回声"。

硬边界：不访问设备/文件/网络、不创建线程、不导入安全链模块；只依赖 numpy 与项目契约。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from app.contracts.base import ContractError, require_positive

# 参考窗口状态（固定短代码）
REFERENCE_OK = "ok"
REFERENCE_HISTORY_SHORT = "history_short"
REFERENCE_FUTURE = "future"
REFERENCE_TOO_OLD = "too_old"

REFERENCE_STATES = (
    REFERENCE_OK,
    REFERENCE_HISTORY_SHORT,
    REFERENCE_FUTURE,
    REFERENCE_TOO_OLD,
)

_ERROR_CODES = frozenset({"unknown_reference_state", "invalid_pcm_length"})


class AecReferenceError(ContractError):
    """参考帧时间线拒绝输入；``str()`` 只返回固定短代码。"""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "invalid_pcm_length"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AecReferenceConfig:
    """参考时间线参数；时间单位为毫秒，采样率单位为 Hz。

    ``max_capacity_ms`` 决定"还能回看多久的参考"。同进程实测里参考时长可能达数秒，
    容量必须覆盖整段播放（另加余量），否则最早的帧会被静默挤出缓冲。
    """

    sample_rate: int = 16000
    frame_duration_ms: int = 20
    max_delay_ms: int = 200
    max_capacity_ms: int = 8000

    def __post_init__(self) -> None:
        for name in ("sample_rate", "frame_duration_ms", "max_delay_ms", "max_capacity_ms"):
            require_positive(name, getattr(self, name))
        if self.max_delay_ms > self.max_capacity_ms:
            raise ContractError("max_delay_ms 不得大于 max_capacity_ms")

    @property
    def frame_samples(self) -> int:
        """一帧的采样点数（``sample_rate * frame_duration_ms / 1000``，向上取整）。"""

        return -(-self.sample_rate * self.frame_duration_ms // 1000)

    @property
    def capacity_frames(self) -> int:
        return max(1, -(-self.max_capacity_ms // self.frame_duration_ms))


@dataclass(frozen=True, slots=True)
class ReferenceWindow:
    """一次对齐取数的结果；``pcm`` 为 float32 归一化数组，仅在 ``state == ok`` 时有效。"""

    state: str
    pcm: np.ndarray | None
    delay_ms: int
    reference_at_ms: int
    available_frames: int

    @property
    def ok(self) -> bool:
        return self.state == REFERENCE_OK

    def to_details(self) -> dict[str, object]:
        return {
            "state": self.state,
            "delay_ms": self.delay_ms,
            "reference_at_ms": self.reference_at_ms,
            "available_frames": self.available_frames,
            "samples": 0 if self.pcm is None else int(self.pcm.size),
        }


def _require_int_ms(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} 必须是整数毫秒")
    if value < 0:
        raise ContractError(f"{name} 必须非负")
    return value


def _pcm16_to_float(payload: object, *, expected_samples: int | None = None) -> np.ndarray:
    """与 ``nlms.pcm16_to_float`` 同口径；在本地实现以避免模块循环导入。"""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ContractError("pcm 必须是 bytes/bytearray/memoryview")
    raw = bytes(payload)
    if len(raw) % 2:
        raise AecReferenceError("invalid_pcm_length")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if expected_samples is not None and samples.size != expected_samples:
        raise AecReferenceError("invalid_pcm_length")
    return samples


class AecReferenceTimeline:
    """送往输出设备的参考 PCM 的有界时间线（纯离线、确定性）。"""

    def __init__(self, *, config: AecReferenceConfig | None = None) -> None:
        self._config = config or AecReferenceConfig()
        self._frames: deque[tuple[int, np.ndarray]] = deque(maxlen=self._config.capacity_frames)
        self._last_at_ms: int | None = None
        self._device_delay_ms = 0
        self._device_epoch = 1
        self._pending_delay_ms: int | None = None

    # ---------------------------------------------------------------- properties

    @property
    def config(self) -> AecReferenceConfig:
        return self._config

    @property
    def device_delay_ms(self) -> int:
        return self._device_delay_ms

    @property
    def device_epoch(self) -> int:
        return self._device_epoch

    @property
    def frames_buffered(self) -> int:
        return len(self._frames)

    @property
    def pending_delay(self) -> bool:
        """是否有待生效的延迟估计（设备变化后需要下一轮重新校准）。"""

        return self._pending_delay_ms is not None

    # ---------------------------------------------------------------- write side

    def push(self, at_ms: int, pcm: object, *, samples: int | None = None) -> int:
        """送入一帧**实际送往输出设备**的参考 PCM；返回缓冲中的帧数。"""

        at = _require_int_ms("at_ms", at_ms)
        if self._last_at_ms is not None and at < self._last_at_ms:
            raise ContractError("at_ms 必须单调不减")
        expected = samples if samples is not None else self._config.frame_samples
        payload = _pcm16_to_float(pcm, expected_samples=expected)
        self._frames.append((at, payload))
        self._last_at_ms = at
        if self._pending_delay_ms is not None:
            # 设备变化后的新延迟只从下一轮开始生效（不在本轮回改历史对齐）。
            self._device_delay_ms = self._pending_delay_ms
            self._pending_delay_ms = None
        return len(self._frames)

    def note_device_change(self, *, device_epoch: int, estimated_delay_ms: int) -> None:
        """标记设备/epoch 变化与新延迟估计；延迟下一轮生效，旧估计立即视为待校准。"""

        require_positive("device_epoch", device_epoch)
        _require_int_ms("estimated_delay_ms", estimated_delay_ms)
        if estimated_delay_ms > self._config.max_delay_ms:
            raise ContractError("estimated_delay_ms 不得超过 max_delay_ms")
        self._device_epoch = device_epoch
        self._pending_delay_ms = estimated_delay_ms

    def clear(self) -> None:
        self._frames.clear()
        self._last_at_ms = None
        self._pending_delay_ms = None

    def frames(self) -> tuple[tuple[int, np.ndarray], ...]:
        """缓冲快照 ``(at_ms, pcm)``（从旧到新），供离线复盘拼接时间轴。"""

        return tuple((at, payload.copy()) for at, payload in self._frames)

    # ---------------------------------------------------------------- read side

    def window(self, capture_at_ms: int, *, frame_samples: int | None = None) -> ReferenceWindow:
        """按 ``capture_at_ms - device_delay_ms`` 取出对齐的参考窗口。"""

        capture_at = _require_int_ms("capture_at_ms", capture_at_ms)
        wanted = capture_at - self._device_delay_ms
        samples = frame_samples if frame_samples is not None else self._config.frame_samples
        if isinstance(samples, bool) or not isinstance(samples, int) or samples <= 0:
            raise ContractError("frame_samples 必须是正整数")
        if not self._frames:
            return self._state(REFERENCE_HISTORY_SHORT, wanted, samples)
        oldest = self._frames[0][0]
        newest = self._frames[-1][0]
        if wanted < oldest:
            return self._state(REFERENCE_TOO_OLD, wanted, samples)
        if wanted > newest:
            return self._state(REFERENCE_FUTURE, wanted, samples)
        pcm = self._slice(wanted, samples)
        if pcm is None:
            return self._state(REFERENCE_HISTORY_SHORT, wanted, samples)
        return ReferenceWindow(
            state=REFERENCE_OK,
            pcm=pcm,
            delay_ms=self._device_delay_ms,
            reference_at_ms=wanted,
            available_frames=len(self._frames),
        )

    # ---------------------------------------------------------------- internals

    def _slice(self, wanted_ms: int, samples: int) -> np.ndarray | None:
        """把覆盖 ``wanted_ms`` 的连续帧拼成期望长度的窗口（不足则返回 None）。"""

        collected: list[np.ndarray] = []
        total = 0
        for at, payload in self._frames:
            if at < wanted_ms:
                continue
            collected.append(payload)
            total += payload.size
            if total >= samples:
                break
        if total < samples:
            return None
        return np.concatenate(collected)[:samples].copy()

    def _state(self, state: str, wanted_ms: int, samples: int) -> ReferenceWindow:
        return ReferenceWindow(
            state=state,
            pcm=None,
            delay_ms=self._device_delay_ms,
            reference_at_ms=wanted_ms,
            available_frames=len(self._frames),
        )
