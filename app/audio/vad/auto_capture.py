"""P9 批次 2：自动收音的语音起始、被动帧缓冲与结束边界。

本模块只决定输入边界：被动拾音何时确认语音起始、一轮 utterance 何时结束、以及把
哪些帧交给下游。它**不决定 ASR 可靠性、不决定授权**，也不生成计划、执行或审计事实；
误触发、边界可疑或检测器异常时一律 fail closed（不交付任何音频）。

复用边界：
- 总线身份（``audio_session_id`` / ``provider_generation`` / ``stream_id`` / ``device_epoch``）
  与帧连续性由本会话自有台账校验，旧边界帧一律 fail closed，不进入交付；
- 语音起始由 ``SpeechStartDetector``（能量门槛 + 连续/累计命中计数）判定；
- 语音结束复用既有 ``VadSession`` 的静音判定，不重复实现静音计数逻辑。

连续性台账规则（自有台账，作用于原始帧）：
- 以 ``observe()`` 收到的第一帧为基线，其后 ``event_sequence`` 与 ``frame_index`` 必须严格
  各 +1，否则 ``FRAME_DISCONTINUITY`` fail closed；允许流中途接入（第一帧不必是 1/0）。
- ``VadSession`` 自己的连续性台账固定从 ``(event_sequence=1, frame_index=0)`` 起算，且没有
  公开的基线入口。因此喂给它的只是**重锚定的逻辑视图副本**：``audio_session_id``、
  ``provider_generation``、``stream_id``、``device_epoch``、``payload`` 与原始帧一致，
  ``event_sequence`` / ``frame_index`` 从 1/0 重新编号，``captured_at`` 固定为 UTC epoch
  ——该时间戳只代表副本语义，**不代表真实采集时间**；真实时间以原始帧为准。
- 交付（``AutoCaptureDecision.emitted`` / ``deliver()``）始终是**原始帧**，顺序严格按原始
  ``event_sequence`` 递增，无重复、无跳号；``deliver()`` 只能成功一次。
- 总线 ``FAILED``（链路损坏）一律 ``BUS_CLOSED`` fail closed；总线 ``CLOSED``（正常停止）
  不阻断对内存中已捕获帧的继续判定。
- 终止后的拒绝不再返回 ``reason=None``：``finish()`` 之后的拒绝为 ``SESSION_CLOSED``，其他
  已 ``ENDED`` / ``FAILED`` 的拒绝为 ``SESSION_ENDED_INTERNAL``，``invalidate()`` 之后的拒绝为
  ``SESSION_INVALIDATED``；会话自身的 ``terminal_reason`` 不被改写。
- ``AutoCaptureDecision.passive_decision`` 只是被动路径的**诊断记录**（观察缓冲与拒绝原因），
  **不进入任何安全判断、授权判断或交付判定**；交付内容只由 ``emitted`` / ``deliver()`` 决定。
"""

from __future__ import annotations

import math
import struct
from collections import deque
from dataclasses import dataclass
from enum import Enum

from app.audio.bus import AudioBus, AudioBusState, AudioPacket
from app.contracts import AudioFrame
from app.contracts.base import ContractError, require_identifier, require_positive

from . import SpeechDetector, VadConfig, VadEndReason, VadFrameStatus, VadSession

_VAD_VIEW_CAPTURED_AT = "1970-01-01T00:00:00Z"
_S16_FULL_SCALE = 32768.0


@dataclass(frozen=True, slots=True)
class SpeechStartConfig:
    """语音起始判定参数；只描述输入边界，不代表 ASR 可靠性。"""

    speech_frame_threshold: int = 3
    reset_on_miss: bool = True
    min_rms_dbfs: float | None = None

    def __post_init__(self) -> None:
        require_positive("speech_frame_threshold", self.speech_frame_threshold)
        if not isinstance(self.reset_on_miss, bool):
            raise ContractError("reset_on_miss 必须是布尔值")
        if self.min_rms_dbfs is not None:
            value = self.min_rms_dbfs
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ContractError("min_rms_dbfs 必须是有限数值或 None")


@dataclass(frozen=True, slots=True)
class SpeechStartDecision:
    """单帧语音起始判定的事实。

    ``observed`` 表示本帧是否被计为一次语音命中（有效帧且能量达标；``min_rms_dbfs`` 为
    ``None`` 即未启用能量门槛时，任何有效帧都计为命中）；``started`` 表示是否已达到起始
    门槛（达到后保持为真，直到 ``reset()``）；``speech_frames`` 是当前连续
    （``reset_on_miss=True``）或累计（``False``）命中数；``observed_frames`` 是累计观察到的
    有效帧数（无效载荷不计入）。
    """

    observed: bool
    started: bool
    speech_frames: int
    observed_frames: int


def _rms_dbfs(payload: bytes | bytearray | memoryview) -> float:
    """按 ``pcm_s16le`` 计算 RMS 电平（dBFS）；全零帧返回 ``-inf``。"""

    raw = bytes(payload)
    count = len(raw) // 2
    if count == 0:
        return float("-inf")
    total = 0
    for sample in struct.unpack(f"<{count}h", raw):
        total += sample * sample
    if total == 0:
        return float("-inf")
    return 20.0 * math.log10(math.sqrt(total / count) / _S16_FULL_SCALE)


class SpeechStartDetector:
    """语音起始判定器：只做能量门槛与有界计数，不持有设备、线程或时钟。"""

    def __init__(self, *, config: SpeechStartConfig) -> None:
        if not isinstance(config, SpeechStartConfig):
            raise ContractError("config 必须是 SpeechStartConfig")
        self._config = config
        self._started = False
        self._speech_frames = 0
        self._observed_frames = 0

    @property
    def config(self) -> SpeechStartConfig:
        return self._config

    @property
    def started(self) -> bool:
        return self._started

    @property
    def speech_frames(self) -> int:
        return self._speech_frames

    @property
    def observed_frames(self) -> int:
        return self._observed_frames

    def observe(self, packet: AudioPacket) -> SpeechStartDecision:
        if not _has_usable_payload(packet):
            return self._decision(observed=False)
        self._observed_frames += 1
        hit = self._meets_energy_gate(packet)
        if hit:
            self._speech_frames += 1
        elif self._config.reset_on_miss:
            self._speech_frames = 0
        if self._speech_frames >= self._config.speech_frame_threshold:
            self._started = True
        return self._decision(observed=hit)

    def reset(self) -> None:
        self._started = False
        self._speech_frames = 0
        self._observed_frames = 0

    def _meets_energy_gate(self, packet: AudioPacket) -> bool:
        threshold = self._config.min_rms_dbfs
        if threshold is None:
            return True
        return _rms_dbfs(packet.payload) >= float(threshold)

    def _decision(self, *, observed: bool) -> SpeechStartDecision:
        return SpeechStartDecision(
            observed=observed,
            started=self._started,
            speech_frames=self._speech_frames,
            observed_frames=self._observed_frames,
        )


def _has_usable_payload(packet: AudioPacket) -> bool:
    if not isinstance(packet, AudioPacket):
        return False
    payload = packet.payload
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return False
    size = len(payload)
    return size >= 2 and size % 2 == 0 and size == packet.frame.payload_size_bytes


class PassiveFrameDecision(str, Enum):
    BUFFERED = "buffered"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_DEVICE_EPOCH = "stale_device_epoch"
    INVALID_AUDIO_FRAME = "invalid_audio_frame"
    FRAME_DISCONTINUITY = "frame_discontinuity"
    OVERFLOW = "overflow"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class PassiveFrameObservation:
    """被动路径的单帧记录；仅用于内部判定与 ``to_details()`` 诊断。"""

    decision: PassiveFrameDecision
    buffered_frames: int
    speech_started: bool


class AutoCaptureConfig:
    """组合既有 ``VadConfig`` 与被动缓冲参数；全部 fail closed。"""

    def __init__(
        self,
        *,
        vad: VadConfig,
        passive_capacity_frames: int = 25,
        speech_start: SpeechStartConfig | None = None,
        max_utterance_ms: int = 30000,
        speech_start_timeout_ms: int = 8000,
    ) -> None:
        if not isinstance(vad, VadConfig):
            raise ContractError("vad 必须是 VadConfig")
        require_positive("passive_capacity_frames", passive_capacity_frames)
        if speech_start is not None and not isinstance(speech_start, SpeechStartConfig):
            raise ContractError("speech_start 必须是 SpeechStartConfig 或 None")
        require_positive("max_utterance_ms", max_utterance_ms)
        require_positive("speech_start_timeout_ms", speech_start_timeout_ms)
        self._vad = vad
        self._passive_capacity_frames = passive_capacity_frames
        self._speech_start = SpeechStartConfig() if speech_start is None else speech_start
        self._max_utterance_ms = max_utterance_ms
        self._speech_start_timeout_ms = speech_start_timeout_ms

    @property
    def vad(self) -> VadConfig:
        return self._vad

    @property
    def passive_capacity_frames(self) -> int:
        return self._passive_capacity_frames

    @property
    def speech_start(self) -> SpeechStartConfig:
        return self._speech_start

    @property
    def max_utterance_ms(self) -> int:
        return self._max_utterance_ms

    @property
    def speech_start_timeout_ms(self) -> int:
        return self._speech_start_timeout_ms


class AutoCaptureEndReason(str, Enum):
    SILENCE_TIMEOUT = "silence_timeout"
    MAX_UTTERANCE_DURATION = "max_utterance_duration"
    SPEECH_START_TIMEOUT = "speech_start_timeout"
    CANCELLED = "cancelled"
    INVALID_AUDIO_FRAME = "invalid_audio_frame"
    FRAME_DISCONTINUITY = "frame_discontinuity"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_DEVICE_EPOCH = "stale_device_epoch"
    DETECTOR_UNAVAILABLE = "detector_unavailable"
    DETECTOR_FAILURE = "detector_failure"
    BUS_CLOSED = "bus_closed"
    INVALIDATED = "invalidated"
    SESSION_CLOSED = "session_closed"              # finish() 之后的拒绝
    SESSION_ENDED_INTERNAL = "session_ended"       # 已 ENDED/FAILED 的其他内部拒绝
    SESSION_INVALIDATED = "session_invalidated"    # invalidate() 之后的拒绝


class AutoCaptureState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"     # 被动拾音，等待语音起始
    CAPTURING = "capturing"     # 已确认语音起始，累积 utterance
    ENDED = "ended"
    FAILED = "failed"


_PASSIVE_TO_END_REASON: dict[PassiveFrameDecision, AutoCaptureEndReason] = {
    PassiveFrameDecision.STALE_AUDIO_SESSION: AutoCaptureEndReason.STALE_AUDIO_SESSION,
    PassiveFrameDecision.STALE_GENERATION: AutoCaptureEndReason.STALE_GENERATION,
    PassiveFrameDecision.STALE_STREAM: AutoCaptureEndReason.STALE_STREAM,
    PassiveFrameDecision.STALE_DEVICE_EPOCH: AutoCaptureEndReason.STALE_DEVICE_EPOCH,
    PassiveFrameDecision.INVALID_AUDIO_FRAME: AutoCaptureEndReason.INVALID_AUDIO_FRAME,
    PassiveFrameDecision.FRAME_DISCONTINUITY: AutoCaptureEndReason.FRAME_DISCONTINUITY,
}

_END_REASON_TO_PASSIVE: dict[AutoCaptureEndReason, PassiveFrameDecision] = {
    reason: decision for decision, reason in _PASSIVE_TO_END_REASON.items()
}

# VadSession 的终止原因映射；BUTTON_RELEASE 只可能由 VadSession.finish() 产生，
# 本模块不调用它，保留映射仅为穷尽枚举。
_VAD_TO_END_REASON: dict[VadEndReason, AutoCaptureEndReason] = {
    VadEndReason.SILENCE_TIMEOUT: AutoCaptureEndReason.SILENCE_TIMEOUT,
    VadEndReason.CANCELLED: AutoCaptureEndReason.CANCELLED,
    VadEndReason.BUTTON_RELEASE: AutoCaptureEndReason.CANCELLED,
    VadEndReason.INVALID_AUDIO_FRAME: AutoCaptureEndReason.INVALID_AUDIO_FRAME,
    VadEndReason.FRAME_DISCONTINUITY: AutoCaptureEndReason.FRAME_DISCONTINUITY,
    VadEndReason.STALE_AUDIO_SESSION: AutoCaptureEndReason.STALE_AUDIO_SESSION,
    VadEndReason.STALE_GENERATION: AutoCaptureEndReason.STALE_GENERATION,
    VadEndReason.STALE_STREAM: AutoCaptureEndReason.STALE_STREAM,
    VadEndReason.STALE_DEVICE_EPOCH: AutoCaptureEndReason.STALE_DEVICE_EPOCH,
    VadEndReason.DETECTOR_UNAVAILABLE: AutoCaptureEndReason.DETECTOR_UNAVAILABLE,
    VadEndReason.DETECTOR_FAILURE: AutoCaptureEndReason.DETECTOR_FAILURE,
    VadEndReason.BUS_CLOSED: AutoCaptureEndReason.BUS_CLOSED,
}


def _end_reason_from_vad(reason: VadEndReason | None) -> AutoCaptureEndReason:
    """把 VadSession 的终止原因映射为自动收音的终止原因；未知原因 fail closed。"""

    if reason is None:
        return AutoCaptureEndReason.FRAME_DISCONTINUITY
    return _VAD_TO_END_REASON.get(reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)


@dataclass(frozen=True, slots=True)
class AutoCaptureDecision:
    """一次自动收音判定的只读投影。

    ``emitted`` 只在 ``ENDED``（可交付）或明确交付时非空；``passive_decision`` 是产生本判定
    的被动路径记录，仅用于诊断，不参与授权或任务事实。
    """

    state: AutoCaptureState
    reason: AutoCaptureEndReason | None
    speech_started: bool
    buffered_frames: int
    captured_frames: int
    emitted: tuple[AudioPacket, ...]
    passive_decision: PassiveFrameDecision | None = None

    def to_details(self) -> dict[str, str]:
        return {
            "state": self.state.value,
            "reason": self.reason.value if self.reason is not None else "none",
            "speech_started": "true" if self.speech_started else "false",
            "buffered_frames": str(self.buffered_frames),
            "captured_frames": str(self.captured_frames),
            "emitted_frames": str(len(self.emitted)),
            "passive_decision": (
                self.passive_decision.value if self.passive_decision is not None else "none"
            ),
        }


class AutoCaptureSession:
    """P9 自动收音：语音起始 -> 复用 VadSession 结束判定 -> 一次性交付 pre-roll + 语音帧。"""

    def __init__(
        self,
        *,
        bus: AudioBus,
        config: AutoCaptureConfig,
        detector: SpeechDetector | None = None,
        start_detector: SpeechStartDetector | None = None,
    ) -> None:
        if not isinstance(bus, AudioBus):
            raise ContractError("bus 必须是 AudioBus")
        if not isinstance(config, AutoCaptureConfig):
            raise ContractError("config 必须是 AutoCaptureConfig")
        if detector is not None and not callable(detector):
            raise ContractError("detector 必须是可调用对象或 None")
        if start_detector is None:
            resolved_start_detector: object = SpeechStartDetector(config=config.speech_start)
        elif callable(getattr(start_detector, "observe", None)):
            resolved_start_detector = start_detector
        else:
            raise ContractError("start_detector 必须提供 observe() 或为 None")
        self._bus = bus
        self._config = config
        self._detector = detector
        self._start_detector = resolved_start_detector
        self._state = AutoCaptureState.IDLE
        self._terminal_reason: AutoCaptureEndReason | None = None
        self._passive: deque[AudioPacket] = deque(maxlen=config.passive_capacity_frames)
        self._captured: list[AudioPacket] = []
        self._speech_started = False
        self._delivered = False
        self._invalidated = False
        self._externally_finished = False
        self._invalidate_reason: str | None = None
        self._listening_ms = 0
        self._utterance_ms = 0
        self._expected_event_sequence: int | None = None
        self._expected_frame_index: int | None = None
        self._vad: VadSession | None = None
        self._vad_event_sequence = 1
        self._vad_frame_index = 0

    @property
    def state(self) -> AutoCaptureState:
        return self._state

    @property
    def terminal_reason(self) -> AutoCaptureEndReason | None:
        return self._terminal_reason

    @property
    def buffered_frames(self) -> int:
        return len(self._passive)

    @property
    def captured_frames(self) -> int:
        return len(self._captured)

    @property
    def speech_started(self) -> bool:
        return self._speech_started

    def listen(self) -> AutoCaptureDecision:
        if self._state is AutoCaptureState.LISTENING:
            return self._decision()
        if self._state is not AutoCaptureState.IDLE:
            return self._decision(reason=self._rejection_reason())
        if self._bus.state is not AudioBusState.OPEN:
            return self._fail(AutoCaptureEndReason.BUS_CLOSED)
        self._state = AutoCaptureState.LISTENING
        return self._decision()

    def observe(self, packet: AudioPacket) -> AutoCaptureDecision:
        if self._state is AutoCaptureState.LISTENING:
            return self._observe_passive(packet)
        if self._state is AutoCaptureState.CAPTURING:
            return self._observe_active(packet)
        return self._rejected()

    def finish(self) -> AutoCaptureDecision:
        """外部要求结束本轮输入（例如唤醒窗口关闭）：正常结束并保留已捕获音频。

        仅在 ``LISTENING`` / ``CAPTURING`` 下生效；其他状态是 no-op（不迁移、不改写既有
        终止原因），其后的拒绝原因是 ``SESSION_CLOSED``。
        """

        if self._state in (AutoCaptureState.LISTENING, AutoCaptureState.CAPTURING):
            self._externally_finished = True
            return self._end(None, keep_audio=True)
        return self._decision(reason=self._rejection_reason())

    def deliver(self) -> tuple[AudioPacket, ...]:
        if self._state is not AutoCaptureState.ENDED or self._delivered:
            return ()
        self._delivered = True
        return tuple(self._captured)

    def cancel(self) -> AutoCaptureDecision:
        """幂等取消：只要总线仍 ``OPEN`` 就关闭（``discard=True``），并作废未交付的帧。

        终止态下不迁移状态机、也不改写既有终止原因（``finish()`` 之后的 ``ENDED`` 语义不变），
        但已捕获且未交付的帧会被一并作废，``deliver()`` 返回空元组。
        """

        if self._bus.state is AudioBusState.OPEN:
            self._bus.close(reason=AutoCaptureEndReason.CANCELLED.value, discard=True)
        if self._state in (AutoCaptureState.ENDED, AutoCaptureState.FAILED):
            self._captured.clear()
            return self._decision(reason=self._terminal_reason)
        return self._end(AutoCaptureEndReason.CANCELLED, keep_audio=False)

    def invalidate(self, *, reason: str) -> AutoCaptureDecision:
        require_identifier("reason", reason)
        self._invalidate_reason = reason
        self._invalidated = True
        self._passive.clear()
        self._captured.clear()
        self._vad = None
        if self._state is not AutoCaptureState.FAILED:
            self._state = AutoCaptureState.FAILED
        if self._terminal_reason is None:
            self._terminal_reason = AutoCaptureEndReason.INVALIDATED
        return self._decision(
            reason=self._terminal_reason,
            observation=PassiveFrameObservation(
                decision=PassiveFrameDecision.INVALIDATED,
                buffered_frames=0,
                speech_started=self._speech_started,
            ),
        )

    def _observe_passive(self, packet: AudioPacket) -> AutoCaptureDecision:
        if self._bus.state is AudioBusState.FAILED:
            return self._fail(AutoCaptureEndReason.BUS_CLOSED)
        reason = self._validate_packet(packet)
        if reason is not None:
            return self._fail(reason, self._passive_observation(_END_REASON_TO_PASSIVE[reason]))
        self._advance_ledger(packet)
        observation = self._record_passive_frame(packet)
        self._listening_ms += packet.frame.duration_ms
        start = self._observe_start_detector(packet)
        if start is None:
            return self._fail(AutoCaptureEndReason.DETECTOR_FAILURE)
        if start.started:
            return self._begin_capture(observation)
        if self._listening_ms >= self._config.speech_start_timeout_ms:
            return self._end(AutoCaptureEndReason.SPEECH_START_TIMEOUT, keep_audio=False)
        return self._decision(observation=observation)

    def _observe_active(self, packet: AudioPacket) -> AutoCaptureDecision:
        if self._bus.state is AudioBusState.FAILED:
            return self._fail(AutoCaptureEndReason.BUS_CLOSED)
        reason = self._validate_packet(packet)
        if reason is not None:
            return self._fail(reason)
        self._advance_ledger(packet)
        terminal = self._feed_vad(packet)
        if terminal is not None:
            return terminal
        return self._decision()

    def _begin_capture(self, observation: PassiveFrameObservation) -> AutoCaptureDecision:
        self._speech_started = True
        if self._detector is None:
            return self._fail(AutoCaptureEndReason.DETECTOR_UNAVAILABLE, observation)
        self._state = AutoCaptureState.CAPTURING
        self._vad = VadSession(bus=self._bus, config=self._config.vad, detector=self._detector)
        start = self._vad.start()
        if start.status is not VadFrameStatus.STARTED:
            return self._fail(_end_reason_from_vad(start.reason))
        pre_roll = tuple(self._passive)
        self._passive.clear()
        for packet in pre_roll:
            terminal = self._feed_vad(packet)
            if terminal is not None:
                return terminal
        return self._decision(observation=observation)

    def _feed_vad(self, packet: AudioPacket) -> AutoCaptureDecision | None:
        """把一帧交给 VadSession；返回终止判定，或 ``None`` 表示继续累积。"""

        vad = self._vad
        if vad is None:
            return self._fail(AutoCaptureEndReason.DETECTOR_UNAVAILABLE)
        duration_ms = packet.frame.duration_ms
        if self._utterance_ms + duration_ms > self._config.max_utterance_ms:
            return self._end(AutoCaptureEndReason.MAX_UTTERANCE_DURATION, keep_audio=True)
        decision = vad.process(self._vad_view(packet))
        if decision.status is VadFrameStatus.FAILED:
            return self._fail(_end_reason_from_vad(decision.reason))
        self._captured.append(packet)
        self._utterance_ms += duration_ms
        if decision.status is VadFrameStatus.ENDED:
            return self._end(AutoCaptureEndReason.SILENCE_TIMEOUT, keep_audio=True)
        if self._utterance_ms >= self._config.max_utterance_ms:
            return self._end(AutoCaptureEndReason.MAX_UTTERANCE_DURATION, keep_audio=True)
        return None

    def _vad_view(self, packet: AudioPacket) -> AudioPacket:
        """构造只给 VadSession 使用的重锚定视图副本（见模块 docstring 的连续性台账规则）。"""

        frame = packet.frame
        view = AudioFrame(
            audio_session_id=frame.audio_session_id,
            provider_generation=frame.provider_generation,
            stream_id=frame.stream_id,
            event_sequence=self._vad_event_sequence,
            frame_index=self._vad_frame_index,
            captured_at=_VAD_VIEW_CAPTURED_AT,
            duration_ms=frame.duration_ms,
            device_epoch=frame.device_epoch,
            sample_rate_hz=frame.sample_rate_hz,
            channel_count=frame.channel_count,
            sample_format=frame.sample_format,
            sample_count=frame.sample_count,
            payload_size_bytes=frame.payload_size_bytes,
        )
        self._vad_event_sequence += 1
        self._vad_frame_index += 1
        return AudioPacket(frame=view, payload=bytes(packet.payload))

    def _validate_packet(self, packet: AudioPacket) -> AutoCaptureEndReason | None:
        if not isinstance(packet, AudioPacket):
            return AutoCaptureEndReason.INVALID_AUDIO_FRAME
        frame = packet.frame
        payload = packet.payload
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return AutoCaptureEndReason.INVALID_AUDIO_FRAME
        if len(payload) != frame.payload_size_bytes:
            return AutoCaptureEndReason.INVALID_AUDIO_FRAME
        if frame.audio_session_id != self._bus.audio_session_id:
            return AutoCaptureEndReason.STALE_AUDIO_SESSION
        if frame.provider_generation != self._bus.provider_generation:
            return AutoCaptureEndReason.STALE_GENERATION
        if frame.stream_id != self._bus.stream_id:
            return AutoCaptureEndReason.STALE_STREAM
        if frame.device_epoch != self._bus.device_epoch:
            return AutoCaptureEndReason.STALE_DEVICE_EPOCH
        if self._expected_event_sequence is not None:
            if (
                frame.event_sequence != self._expected_event_sequence
                or frame.frame_index != self._expected_frame_index
            ):
                return AutoCaptureEndReason.FRAME_DISCONTINUITY
        return None

    def _advance_ledger(self, packet: AudioPacket) -> None:
        self._expected_event_sequence = packet.frame.event_sequence + 1
        self._expected_frame_index = packet.frame.frame_index + 1

    def _record_passive_frame(self, packet: AudioPacket) -> PassiveFrameObservation:
        overflow = len(self._passive) >= self._config.passive_capacity_frames
        self._passive.append(packet)
        return PassiveFrameObservation(
            decision=PassiveFrameDecision.OVERFLOW if overflow else PassiveFrameDecision.BUFFERED,
            buffered_frames=len(self._passive),
            speech_started=self._speech_started,
        )

    def _observe_start_detector(self, packet: AudioPacket) -> SpeechStartDecision | None:
        try:
            decision = self._start_detector.observe(packet)  # type: ignore[attr-defined]
        except Exception:
            return None
        if not isinstance(decision, SpeechStartDecision):
            return None
        return decision

    def _passive_observation(self, decision: PassiveFrameDecision) -> PassiveFrameObservation:
        return PassiveFrameObservation(
            decision=decision,
            buffered_frames=len(self._passive),
            speech_started=self._speech_started,
        )

    def _rejected(self) -> AutoCaptureDecision:
        observation = self._passive_observation(PassiveFrameDecision.INVALIDATED)
        return self._decision(reason=self._rejection_reason(), observation=observation)

    def _rejection_reason(self) -> AutoCaptureEndReason | None:
        """非迁移拒绝的受限原因码；非终止状态没有专属原因码时返回 ``None``。"""

        if self._invalidated:
            return AutoCaptureEndReason.SESSION_INVALIDATED
        if self._state is AutoCaptureState.ENDED:
            if self._externally_finished:
                return AutoCaptureEndReason.SESSION_CLOSED
            return AutoCaptureEndReason.SESSION_ENDED_INTERNAL
        if self._state is AutoCaptureState.FAILED:
            return AutoCaptureEndReason.SESSION_ENDED_INTERNAL
        if self._state is AutoCaptureState.IDLE:
            return AutoCaptureEndReason.INVALIDATED
        return None

    def _end(
        self,
        reason: AutoCaptureEndReason | None,
        *,
        keep_audio: bool,
        observation: PassiveFrameObservation | None = None,
    ) -> AutoCaptureDecision:
        self._state = AutoCaptureState.ENDED
        self._terminal_reason = reason
        self._passive.clear()
        self._vad = None
        if not keep_audio:
            self._captured.clear()
        emitted = tuple(self._captured) if keep_audio else ()
        return self._decision(reason=reason, emitted=emitted, observation=observation)

    def _fail(
        self,
        reason: AutoCaptureEndReason,
        observation: PassiveFrameObservation | None = None,
    ) -> AutoCaptureDecision:
        self._state = AutoCaptureState.FAILED
        self._terminal_reason = reason
        self._passive.clear()
        self._captured.clear()
        self._vad = None
        return self._decision(reason=reason, observation=observation)

    def _decision(
        self,
        *,
        reason: AutoCaptureEndReason | None = None,
        emitted: tuple[AudioPacket, ...] = (),
        observation: PassiveFrameObservation | None = None,
    ) -> AutoCaptureDecision:
        return AutoCaptureDecision(
            state=self._state,
            reason=reason,
            speech_started=self._speech_started,
            buffered_frames=len(self._passive),
            captured_frames=len(self._captured),
            emitted=emitted,
            passive_decision=None if observation is None else observation.decision,
        )


__all__ = [
    "AutoCaptureConfig",
    "AutoCaptureDecision",
    "AutoCaptureEndReason",
    "AutoCaptureSession",
    "AutoCaptureState",
    "PassiveFrameDecision",
    "PassiveFrameObservation",
    "SpeechStartConfig",
    "SpeechStartDecision",
    "SpeechStartDetector",
]
