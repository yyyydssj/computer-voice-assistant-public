"""有界、内存优先且连续性失败即关闭的音频帧总线。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from threading import Lock

from app.contracts import AudioFrame
from app.contracts.base import require_identifier, require_positive


class AudioBusState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    FAILED = "failed"


class AudioBusDecision(str, Enum):
    ACCEPTED = "accepted"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_DEVICE_EPOCH = "stale_device_epoch"
    INVALID_PAYLOAD = "invalid_payload"
    DISCONTINUITY = "discontinuity"
    OVERFLOW = "overflow"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AudioPacket:
    frame: AudioFrame
    payload: bytes


class AudioBus:
    """隔离采集 callback 与后续解码器，不阻塞、不落盘、不修补丢帧。"""

    def __init__(
        self,
        *,
        audio_session_id: str,
        provider_generation: int,
        stream_id: str,
        device_epoch: int,
        capacity_frames: int,
    ) -> None:
        require_identifier("audio_session_id", audio_session_id)
        require_positive("provider_generation", provider_generation)
        require_identifier("stream_id", stream_id)
        require_positive("device_epoch", device_epoch)
        require_positive("capacity_frames", capacity_frames)
        self._audio_session_id = audio_session_id
        self._provider_generation = provider_generation
        self._stream_id = stream_id
        self._device_epoch = device_epoch
        self._capacity_frames = capacity_frames
        self._queue: deque[AudioPacket] = deque()
        self._state = AudioBusState.OPEN
        self._terminal_reason: str | None = None
        self._next_event_sequence = 1
        self._next_frame_index = 0
        self._lock = Lock()

    @property
    def audio_session_id(self) -> str:
        return self._audio_session_id

    @property
    def provider_generation(self) -> int:
        return self._provider_generation

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def device_epoch(self) -> int:
        return self._device_epoch

    @property
    def state(self) -> AudioBusState:
        with self._lock:
            return self._state

    @property
    def terminal_reason(self) -> str | None:
        with self._lock:
            return self._terminal_reason

    @property
    def queued_frames(self) -> int:
        with self._lock:
            return len(self._queue)

    def publish(self, frame: AudioFrame, payload: bytes | bytearray | memoryview) -> AudioBusDecision:
        with self._lock:
            if self._state is not AudioBusState.OPEN:
                return AudioBusDecision.CLOSED
            if frame.audio_session_id != self._audio_session_id:
                return AudioBusDecision.STALE_AUDIO_SESSION
            if frame.provider_generation != self._provider_generation:
                return AudioBusDecision.STALE_GENERATION
            if frame.stream_id != self._stream_id:
                return AudioBusDecision.STALE_STREAM
            if frame.device_epoch != self._device_epoch:
                return AudioBusDecision.STALE_DEVICE_EPOCH
            if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) != frame.payload_size_bytes:
                return AudioBusDecision.INVALID_PAYLOAD
            if frame.event_sequence != self._next_event_sequence or frame.frame_index != self._next_frame_index:
                self._fail_locked("frame_discontinuity")
                return AudioBusDecision.DISCONTINUITY
            if len(self._queue) >= self._capacity_frames:
                self._fail_locked("overflow")
                return AudioBusDecision.OVERFLOW
            self._queue.append(AudioPacket(frame=frame, payload=bytes(payload)))
            self._next_event_sequence += 1
            self._next_frame_index += 1
            return AudioBusDecision.ACCEPTED

    def read_nowait(self) -> AudioPacket | None:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    def close(self, *, reason: str, discard: bool) -> None:
        require_identifier("reason", reason)
        with self._lock:
            if self._state is not AudioBusState.OPEN:
                return
            self._state = AudioBusState.CLOSED
            self._terminal_reason = reason
            if discard:
                self._queue.clear()

    def _fail_locked(self, reason: str) -> None:
        self._state = AudioBusState.FAILED
        self._terminal_reason = reason
        self._queue.clear()
