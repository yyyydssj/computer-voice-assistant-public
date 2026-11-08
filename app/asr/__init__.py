"""P6 ASR 数据面：SenseVoice adapter、ResultGate 与故障降级状态机。

Provider adapter 只负责把当前音频会话的 PCM 帧交给本机 Provider，并返回带有
边界身份和 Provider 能力声明的识别事件；本文件不接入 Agent、
授权、工具、TTS 或电脑副作用。ResultGate 位于独立的 result_gate 模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import importlib
from typing import Any, Protocol
from pathlib import Path
import re
import struct
from uuid import uuid4

from app.audio.bus import AudioBus, AudioBusState, AudioPacket
from app.contracts.base import ContractError, require_identifier, require_positive


class AsrProviderUnavailable(RuntimeError):
    """Provider 运行时、模型工厂或初始化失败。"""


class AsrProviderError(RuntimeError):
    """Provider 在当前会话中返回不可用结果。"""


class DetectionStatus(str, Enum):
    DETECTED = "detected"
    NOT_DETECTED = "not_detected"
    UNSUPPORTED = "unsupported"


class AsrEventKind(str, Enum):
    FINAL = "final"
    EMPTY = "empty"


class SenseVoiceSessionState(str, Enum):
    UNLOADED = "unloaded"
    READY = "ready"
    ACTIVE = "active"
    ENDED = "ended"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SenseVoiceCapabilities:
    """能力矩阵；unsupported 不得被解释为 not_detected。"""

    supports_interim: bool
    supports_final: bool
    vad: DetectionStatus
    overlap_detection: DetectionStatus
    speaker_identity: DetectionStatus
    confidence: DetectionStatus
    language_detection: DetectionStatus


@dataclass(frozen=True, slots=True)
class SenseVoiceConfig:
    model_path: Path
    tokens_path: Path
    sample_rate_hz: int = 16000
    channel_count: int = 1
    num_threads: int = 2
    provider: str = "cpu"
    language: str = "auto"
    use_itn: bool = True
    feature_dim: int = 80

    def __post_init__(self) -> None:
        if not isinstance(self.model_path, Path):
            raise TypeError("model_path 必须是 Path")
        if not isinstance(self.tokens_path, Path):
            raise TypeError("tokens_path 必须是 Path")
        require_positive("sample_rate_hz", self.sample_rate_hz)
        if self.sample_rate_hz != 16000:
            raise ValueError("SenseVoice adapter 当前只接受 16000 Hz PCM")
        if self.channel_count != 1:
            raise ValueError("SenseVoice adapter 当前只接受单声道 PCM")
        require_positive("num_threads", self.num_threads)
        require_positive("feature_dim", self.feature_dim)
        if not self.provider or not self.language:
            raise ValueError("provider/language 不得为空")

    @property
    def sample_count_per_frame(self) -> int:
        return 320


@dataclass(frozen=True, slots=True)
class AsrEvent:
    event_id: str
    kind: AsrEventKind
    audio_session_id: str
    provider_generation: int
    stream_id: str
    utterance_id: str
    event_sequence: int
    text: str
    is_final: bool
    overlap_detection: DetectionStatus
    speaker_identity: DetectionStatus
    confidence: DetectionStatus
    provider_timestamp: str
    received_at: str

    def __post_init__(self) -> None:
        for name in ("event_id", "audio_session_id", "stream_id", "utterance_id"):
            require_identifier(name, getattr(self, name))
        require_positive("provider_generation", self.provider_generation)
        require_positive("event_sequence", self.event_sequence)
        if not isinstance(self.text, str):
            raise TypeError("text 必须是字符串")
        if not self.is_final:
            raise ContractError("SenseVoice adapter 不产生未经声明的 interim 事件")


class _ModuleLoader(Protocol):
    def __call__(self) -> Any: ...


class SenseVoiceRecognizerAdapter:
    """对 sherpa-onnx OfflineRecognizer.from_sense_voice 的最小惰性适配。"""

    _SPECIAL_TOKEN_RE = re.compile(r"<\|[^|\r\n]{1,64}\|>")

    def __init__(
        self,
        config: SenseVoiceConfig,
        *,
        module_loader: _ModuleLoader | None = None,
    ) -> None:
        self._config = config
        self._module_loader = module_loader or (lambda: importlib.import_module("sherpa_onnx"))
        self._recognizer: Any | None = None
        self._state = SenseVoiceSessionState.UNLOADED
        self._capabilities = SenseVoiceCapabilities(
            supports_interim=False,
            supports_final=True,
            vad=DetectionStatus.UNSUPPORTED,
            overlap_detection=DetectionStatus.UNSUPPORTED,
            speaker_identity=DetectionStatus.UNSUPPORTED,
            confidence=DetectionStatus.UNSUPPORTED,
            language_detection=DetectionStatus.UNSUPPORTED,
        )

    @property
    def state(self) -> SenseVoiceSessionState:
        return self._state

    @property
    def capabilities(self) -> SenseVoiceCapabilities:
        return self._capabilities

    def load(self) -> None:
        if self._state is SenseVoiceSessionState.READY:
            return
        if self._state is SenseVoiceSessionState.FAILED:
            raise AsrProviderUnavailable("SenseVoice provider 已失败，需创建新的 adapter")
        try:
            module = self._module_loader()
            factory = getattr(getattr(module, "OfflineRecognizer"), "from_sense_voice")
            self._recognizer = factory(
                model=str(self._config.model_path),
                tokens=str(self._config.tokens_path),
                num_threads=self._config.num_threads,
                sample_rate=self._config.sample_rate_hz,
                feature_dim=self._config.feature_dim,
                provider=self._config.provider,
                language=self._config.language,
                use_itn=self._config.use_itn,
            )
        except Exception as exc:
            self._recognizer = None
            self._state = SenseVoiceSessionState.FAILED
            if isinstance(exc, AsrProviderUnavailable):
                raise
            raise AsrProviderUnavailable("SenseVoice provider 加载失败") from exc
        self._state = SenseVoiceSessionState.READY

    def start_session(self, *, bus: AudioBus, utterance_id: str) -> "SenseVoiceSession":
        require_identifier("utterance_id", utterance_id)
        if self._state is not SenseVoiceSessionState.READY or self._recognizer is None:
            raise AsrProviderUnavailable("SenseVoice provider 尚未 ready")
        try:
            stream = self._recognizer.create_stream()
        except Exception as exc:
            raise AsrProviderUnavailable("SenseVoice stream 创建失败") from exc
        self._state = SenseVoiceSessionState.ACTIVE
        return SenseVoiceSession(
            adapter=self,
            recognizer=self._recognizer,
            stream=stream,
            bus=bus,
            utterance_id=utterance_id,
        )

    @classmethod
    def normalize_text(cls, text: object) -> str:
        if not isinstance(text, str):
            return ""
        without_tags = cls._SPECIAL_TOKEN_RE.sub("", text)
        return " ".join(without_tags.split())

    def _session_finished(self, state: SenseVoiceSessionState) -> None:
        self._state = state if state is SenseVoiceSessionState.FAILED else SenseVoiceSessionState.READY


class SenseVoiceSession:
    def __init__(
        self,
        *,
        adapter: SenseVoiceRecognizerAdapter,
        recognizer: Any,
        stream: Any,
        bus: AudioBus,
        utterance_id: str,
    ) -> None:
        self._adapter = adapter
        self._recognizer = recognizer
        self._stream = stream
        self._bus = bus
        self._utterance_id = utterance_id
        self._state = SenseVoiceSessionState.ACTIVE
        self._next_event_sequence = 1
        self._next_frame_index = 0
        self._provider_event_sequence = 0

    @property
    def state(self) -> SenseVoiceSessionState:
        return self._state

    @property
    def utterance_id(self) -> str:
        return self._utterance_id

    @property
    def processed_frames(self) -> int:
        return self._next_frame_index

    def feed(self, packet: AudioPacket) -> bool:
        if self._state is not SenseVoiceSessionState.ACTIVE:
            return False
        reason = self._validate_packet(packet)
        if reason is not None:
            self._fail(reason)
            return False
        try:
            samples = _pcm16le_to_float(packet.payload)
            self._stream.accept_waveform(self._bus_frame_sample_rate(packet), samples)
        except Exception:
            self._fail("provider_feed_failure")
            return False
        self._next_event_sequence += 1
        self._next_frame_index += 1
        return True

    def finish(self) -> AsrEvent | None:
        if self._state is not SenseVoiceSessionState.ACTIVE:
            return None
        try:
            decode = getattr(self._recognizer, "decode", None)
            if callable(decode):
                decode(self._stream)
            else:
                decode_stream = getattr(self._recognizer, "decode_stream", None)
                if not callable(decode_stream):
                    raise AsrProviderError("SenseVoice recognizer 缺少 decode 方法")
                decode_stream(self._stream)
            raw_result = getattr(self._stream, "result")
            raw_text = raw_result.text if hasattr(raw_result, "text") else raw_result
            text = self._adapter.normalize_text(raw_text)
        except Exception:
            self._fail("provider_decode_failure")
            return None
        self._state = SenseVoiceSessionState.ENDED
        self._close_bus("asr_final")
        self._adapter._session_finished(SenseVoiceSessionState.READY)
        self._provider_event_sequence += 1
        now = _utc_now()
        kind = AsrEventKind.FINAL if text else AsrEventKind.EMPTY
        return AsrEvent(
            event_id=f"asr-{uuid4().hex}",
            kind=kind,
            audio_session_id=self._bus.audio_session_id,
            provider_generation=self._bus.provider_generation,
            stream_id=self._bus.stream_id,
            utterance_id=self._utterance_id,
            event_sequence=self._provider_event_sequence,
            text=text,
            is_final=True,
            overlap_detection=self._adapter.capabilities.overlap_detection,
            speaker_identity=self._adapter.capabilities.speaker_identity,
            confidence=self._adapter.capabilities.confidence,
            provider_timestamp=now,
            received_at=now,
        )

    def cancel(self) -> None:
        if self._state is not SenseVoiceSessionState.ACTIVE:
            return
        self._state = SenseVoiceSessionState.CANCELLED
        self._close_bus("asr_cancelled")
        self._adapter._session_finished(SenseVoiceSessionState.READY)

    def _validate_packet(self, packet: AudioPacket) -> str | None:
        if not isinstance(packet, AudioPacket):
            return "invalid_audio_packet"
        frame = packet.frame
        if not isinstance(packet.payload, (bytes, bytearray, memoryview)):
            return "invalid_payload"
        if len(packet.payload) != frame.payload_size_bytes:
            return "invalid_payload"
        if frame.audio_session_id != self._bus.audio_session_id:
            return "stale_audio_session"
        if frame.provider_generation != self._bus.provider_generation:
            return "stale_generation"
        if frame.stream_id != self._bus.stream_id:
            return "stale_stream"
        if frame.device_epoch != self._bus.device_epoch:
            return "stale_device_epoch"
        if frame.sample_rate_hz != self._adapter._config.sample_rate_hz:
            return "incompatible_sample_rate"
        if frame.channel_count != self._adapter._config.channel_count:
            return "incompatible_channels"
        if frame.sample_format != "pcm_s16le":
            return "incompatible_sample_format"
        if frame.sample_count != self._adapter._config.sample_count_per_frame:
            return "incompatible_frame_size"
        if frame.event_sequence != self._next_event_sequence or frame.frame_index != self._next_frame_index:
            return "frame_discontinuity"
        return None

    def _bus_frame_sample_rate(self, packet: AudioPacket) -> int:
        return packet.frame.sample_rate_hz

    def _fail(self, _reason: str) -> None:
        self._state = SenseVoiceSessionState.FAILED
        self._close_bus("asr_failed")
        self._adapter._session_finished(SenseVoiceSessionState.FAILED)

    def _close_bus(self, reason: str) -> None:
        if self._bus.state is AudioBusState.OPEN:
            self._bus.close(reason=reason, discard=True)


def _pcm16le_to_float(payload: bytes | bytearray | memoryview) -> list[float]:
    raw = bytes(payload)
    if len(raw) % 2:
        raise ValueError("PCM16 payload 长度必须为偶数")
    sample_count = len(raw) // 2
    values = struct.unpack("<" + "h" * sample_count, raw)
    return [value / 32768.0 for value in values]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


from .result_gate import AsrResultCandidate, ResultGate, ResultGateDecision, ResultGateResult, ResultGateState
from .push_to_talk import (
    PushToTalkCoordinator,
    PushToTalkDiagnostic,
    PushToTalkError,
    PushToTalkPumpResult,
    PushToTalkStartResult,
    PushToTalkState,
    PushToTalkTurnResult,
)

from .recovery import (
    AsrFailure,
    AsrFailureCode,
    AsrFailureStage,
    AsrInputMode,
    AsrRecoveryConfig,
    AsrRecoveryController,
    AsrRecoveryState,
    AsrSessionBoundary,
    AsrTransitionDecision,
    AsrTransitionResult,
)

__all__ = [
    "AsrEvent",
    "AsrEventKind",
    "AsrFailure",
    "AsrFailureCode",
    "AsrFailureStage",
    "AsrInputMode",
    "AsrRecoveryConfig",
    "AsrRecoveryController",
    "AsrRecoveryState",
    "AsrSessionBoundary",
    "AsrTransitionDecision",
    "AsrTransitionResult",
    "AsrProviderError",
    "AsrProviderUnavailable",
    "DetectionStatus",
    "SenseVoiceCapabilities",
    "SenseVoiceConfig",
    "SenseVoiceRecognizerAdapter",
    "SenseVoiceSession",
    "SenseVoiceSessionState",
    "AsrResultCandidate",
    "ResultGate",
    "ResultGateDecision",
    "ResultGateResult",
    "ResultGateState",
    "PushToTalkCoordinator",
    "PushToTalkDiagnostic",
    "PushToTalkError",
    "PushToTalkPumpResult",
    "PushToTalkStartResult",
    "PushToTalkState",
    "PushToTalkTurnResult",
]
