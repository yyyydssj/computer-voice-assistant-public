"""P6 批次 3：按键输入边界的最小 VAD/静音结束判定。

本模块只决定当前按键语音轮次何时结束，不产生 ASR 结果、授权或 Agent 请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import ceil
from typing import Callable, Protocol

from app.audio.bus import AudioBus, AudioBusState, AudioPacket
from app.contracts.base import ContractError, require_positive


class VadSessionState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    ENDED = "ended"
    CANCELLED = "cancelled"
    FAILED = "failed"


class VadFrameStatus(str, Enum):
    STARTED = "started"
    ACCEPTED = "accepted"
    ENDED = "ended"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    CLOSED = "closed"
    FAILED = "failed"


class VadEndReason(str, Enum):
    BUTTON_RELEASE = "button_release"
    SILENCE_TIMEOUT = "silence_timeout"
    CANCELLED = "cancelled"
    INVALID_AUDIO_FRAME = "invalid_audio_frame"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_DEVICE_EPOCH = "stale_device_epoch"
    FRAME_DISCONTINUITY = "frame_discontinuity"
    DETECTOR_UNAVAILABLE = "detector_unavailable"
    DETECTOR_FAILURE = "detector_failure"
    BUS_CLOSED = "bus_closed"


@dataclass(frozen=True, slots=True)
class VadConfig:
    """VAD 边界参数；数值只描述按键输入轮次，不代表 ASR 可靠性。"""

    silence_duration_ms: int = 1000
    frame_duration_ms: int = 20

    def __post_init__(self) -> None:
        require_positive("silence_duration_ms", self.silence_duration_ms)
        require_positive("frame_duration_ms", self.frame_duration_ms)

    @property
    def silence_frame_count(self) -> int:
        return max(1, ceil(self.silence_duration_ms / self.frame_duration_ms))


class SpeechDetector(Protocol):
    def __call__(self, packet: AudioPacket) -> bool: ...


@dataclass(frozen=True, slots=True)
class VadDecision:
    status: VadFrameStatus
    state: VadSessionState
    reason: VadEndReason | None
    speech_detected: bool | None
    silence_frames: int
    processed_frames: int


class VadSession:
    """绑定一个 AudioBus 的、仅用于输入结束判定的有界 VAD 会话。"""

    def __init__(
        self,
        *,
        bus: AudioBus,
        config: VadConfig,
        detector: SpeechDetector | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._detector = detector
        self._state = VadSessionState.IDLE
        self._terminal_reason: VadEndReason | None = None
        self._next_event_sequence = 1
        self._next_frame_index = 0
        self._silence_frames = 0
        self._processed_frames = 0

    @property
    def state(self) -> VadSessionState:
        return self._state

    @property
    def terminal_reason(self) -> VadEndReason | None:
        return self._terminal_reason

    @property
    def silence_frames(self) -> int:
        return self._silence_frames

    @property
    def processed_frames(self) -> int:
        return self._processed_frames

    def start(self) -> VadDecision:
        if self._state is not VadSessionState.IDLE:
            return self._decision(VadFrameStatus.CLOSED, reason=self._terminal_reason)
        if self._bus.state is AudioBusState.FAILED:
            return self._fail(VadEndReason.BUS_CLOSED)
        self._state = VadSessionState.ACTIVE
        return self._decision(VadFrameStatus.STARTED)

    def process(self, packet: AudioPacket) -> VadDecision:
        if self._state is not VadSessionState.ACTIVE:
            status = (
                VadFrameStatus.FAILED
                if self._state is VadSessionState.FAILED
                else VadFrameStatus.CLOSED
            )
            return self._decision(status, reason=self._terminal_reason)

        reason = self._validate_packet(packet)
        if reason is not None:
            return self._fail(reason)
        if self._detector is None:
            return self._fail(VadEndReason.DETECTOR_UNAVAILABLE)
        try:
            speech_detected = self._detector(packet)
        except Exception:
            return self._fail(VadEndReason.DETECTOR_FAILURE)
        if not isinstance(speech_detected, bool):
            return self._fail(VadEndReason.DETECTOR_FAILURE)

        self._processed_frames += 1
        self._next_event_sequence += 1
        self._next_frame_index += 1
        if speech_detected:
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        if self._silence_frames >= self._config.silence_frame_count:
            self._state = VadSessionState.ENDED
            self._terminal_reason = VadEndReason.SILENCE_TIMEOUT
            return self._decision(
                VadFrameStatus.ENDED,
                reason=self._terminal_reason,
                speech_detected=speech_detected,
            )
        return self._decision(
            VadFrameStatus.ACCEPTED,
            speech_detected=speech_detected,
        )

    def finish(self) -> VadDecision:
        if self._state is not VadSessionState.ACTIVE:
            return self._decision(VadFrameStatus.CLOSED, reason=self._terminal_reason)
        self._state = VadSessionState.ENDED
        self._terminal_reason = VadEndReason.BUTTON_RELEASE
        return self._decision(VadFrameStatus.ENDED, reason=self._terminal_reason)

    def cancel(self) -> VadDecision:
        if self._state is not VadSessionState.ACTIVE:
            return self._decision(VadFrameStatus.CLOSED, reason=self._terminal_reason)
        self._state = VadSessionState.CANCELLED
        self._terminal_reason = VadEndReason.CANCELLED
        if self._bus.state is AudioBusState.OPEN:
            self._bus.close(reason=VadEndReason.CANCELLED.value, discard=True)
        return self._decision(VadFrameStatus.CANCELLED, reason=self._terminal_reason)

    def _validate_packet(self, packet: AudioPacket) -> VadEndReason | None:
        frame = packet.frame
        if not isinstance(packet.payload, (bytes, bytearray, memoryview)):
            return VadEndReason.INVALID_AUDIO_FRAME
        if len(packet.payload) != frame.payload_size_bytes:
            return VadEndReason.INVALID_AUDIO_FRAME
        if frame.audio_session_id != self._bus.audio_session_id:
            return VadEndReason.STALE_AUDIO_SESSION
        if frame.provider_generation != self._bus.provider_generation:
            return VadEndReason.STALE_GENERATION
        if frame.stream_id != self._bus.stream_id:
            return VadEndReason.STALE_STREAM
        if frame.device_epoch != self._bus.device_epoch:
            return VadEndReason.STALE_DEVICE_EPOCH
        if frame.event_sequence != self._next_event_sequence or frame.frame_index != self._next_frame_index:
            return VadEndReason.FRAME_DISCONTINUITY
        return None

    def _fail(self, reason: VadEndReason) -> VadDecision:
        self._state = VadSessionState.FAILED
        self._terminal_reason = reason
        self._silence_frames = 0
        if self._bus.state is AudioBusState.OPEN:
            self._bus.close(reason=reason.value, discard=True)
        return self._decision(VadFrameStatus.FAILED, reason=reason)

    def _decision(
        self,
        status: VadFrameStatus,
        *,
        reason: VadEndReason | None = None,
        speech_detected: bool | None = None,
    ) -> VadDecision:
        return VadDecision(
            status=status,
            state=self._state,
            reason=reason,
            speech_detected=speech_detected,
            silence_frames=self._silence_frames,
            processed_frames=self._processed_frames,
        )


__all__ = [
    "SpeechDetector",
    "VadConfig",
    "VadDecision",
    "VadEndReason",
    "VadFrameStatus",
    "VadSession",
    "VadSessionState",
]