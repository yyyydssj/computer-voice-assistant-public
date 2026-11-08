from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from threading import Event
from typing import Iterator, Protocol

from app.contracts.base import ContractError, require_identifier, require_non_empty, require_positive


class VoiceStudioContractError(ValueError):
    """VoiceStudio dual-path local contract violation.

    ``code`` carries the optional fixed short code of the violated rule as an
    attribute of the exception itself, so the runtime fact layer can log **which**
    contract rule failed without ever logging a character of text. The message is
    unchanged; the code is additive and keyword-only.
    """

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


_SAMPLE_RATE = 24_000
_CHANNELS = 1
_SAMPLE_WIDTH = 2
_MAX_TEXT_LENGTH = 4096
_MAX_AUDIO_BYTES = 16 * 1024 * 1024


def _require_id(name: str, value: str) -> None:
    try:
        require_identifier(name, value)
    except ContractError as exc:
        raise VoiceStudioContractError(str(exc), code="identifier_invalid") from exc


def _require_text(name: str, value: str) -> None:
    try:
        require_non_empty(name, value, maximum=_MAX_TEXT_LENGTH)
    except ContractError as exc:
        raise VoiceStudioContractError(str(exc), code="text_invalid") from exc
    if not value.strip():
        raise VoiceStudioContractError(
            f"{name} 不得为空或只包含空白字符", code="text_blank"
        )


@dataclass(frozen=True, slots=True)
class VoiceStudioSentenceRequest:
    """One already-segmented sentence submitted to one VoiceStudio transport."""

    reply_id: str
    segment_id: str
    provider_request_id: str
    provider_generation: int
    text: str
    profile_id: str
    language: str
    speed: float
    engine: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
            ("provider_request_id", self.provider_request_id),
            ("profile_id", self.profile_id),
            ("language", self.language),
        ):
            _require_id(name, value)
        try:
            require_positive("provider_generation", self.provider_generation)
        except ContractError as exc:
            raise VoiceStudioContractError(str(exc), code="generation_invalid") from exc
        _require_text("text", self.text)
        if type(self.speed) is not float or not isfinite(self.speed) or not 0.1 <= self.speed <= 4.0:
            raise VoiceStudioContractError(
                "speed 必须是 0.1..4.0 范围内的有限浮点数", code="speed_invalid"
            )
        if self.engine is not None:
            _require_id("engine", self.engine)


@dataclass(frozen=True, slots=True)
class PCMChunk:
    """One ordered PCM16 chunk normalized across WS and HTTP transports."""

    reply_id: str
    segment_id: str
    provider_request_id: str
    provider_generation: int
    sequence: int
    sample_rate: int
    channels: int
    sample_width: int
    data: bytes
    is_final: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
            ("provider_request_id", self.provider_request_id),
        ):
            _require_id(name, value)
        try:
            require_positive("provider_generation", self.provider_generation)
            require_positive("sequence", self.sequence)
        except ContractError as exc:
            raise VoiceStudioContractError(str(exc), code="counter_invalid") from exc
        if self.sample_rate != _SAMPLE_RATE:
            raise VoiceStudioContractError("sample_rate 必须是 24000", code="chunk_rate_invalid")
        if self.channels != _CHANNELS:
            raise VoiceStudioContractError("channels 必须是 1", code="chunk_channels_invalid")
        if self.sample_width != _SAMPLE_WIDTH:
            raise VoiceStudioContractError(
                "sample_width 必须是 2", code="chunk_width_invalid"
            )
        if not isinstance(self.data, bytes):
            raise VoiceStudioContractError("data 必须是 bytes", code="chunk_data_invalid")
        if not self.data:
            raise VoiceStudioContractError("PCM chunk 不得为空", code="chunk_empty")
        if len(self.data) % _SAMPLE_WIDTH:
            raise VoiceStudioContractError("PCM data 长度必须为偶数", code="chunk_length_invalid")
        if len(self.data) > _MAX_AUDIO_BYTES:
            raise VoiceStudioContractError(
                "单个 PCM chunk 超过 16 MB 上限", code="chunk_too_large"
            )
        if not isinstance(self.is_final, bool):
            raise VoiceStudioContractError("is_final 必须是 bool", code="chunk_final_invalid")


class VoiceStudioAudioProvider(Protocol):
    """Synchronous, cancellable provider seam used by the future adapters."""

    def stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        *,
        cancelled: Event,
    ) -> Iterator[PCMChunk]:
        ...

    def close(self) -> None:
        ...


class PCMChunkStreamValidator:
    """Fail-closed validator for identity, sequence and cancellation fences."""

    def __init__(self, request: VoiceStudioSentenceRequest) -> None:
        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        self._request = request
        self._expected_sequence = 1
        self._cancelled = False
        self._finished = False
        self.accepted_chunks: list[PCMChunk] = []

    @property
    def expected_sequence(self) -> int:
        return self._expected_sequence

    @property
    def finished(self) -> bool:
        return self._finished

    def cancel(self) -> None:
        self._cancelled = True

    def accept(self, chunk: PCMChunk) -> PCMChunk:
        if self._cancelled:
            raise VoiceStudioContractError(
                "取消后的 PCM chunk 不得进入播放", code="validator_cancelled"
            )
        if self._finished:
            raise VoiceStudioContractError(
                "final PCM chunk 之后不得继续接收音频", code="validator_after_final"
            )
        if not isinstance(chunk, PCMChunk):
            raise VoiceStudioContractError(
                "chunk 必须是 PCMChunk", code="validator_chunk_type_invalid"
            )
        expected_identity = (
            self._request.reply_id,
            self._request.segment_id,
            self._request.provider_request_id,
            self._request.provider_generation,
        )
        actual_identity = (
            chunk.reply_id,
            chunk.segment_id,
            chunk.provider_request_id,
            chunk.provider_generation,
        )
        if actual_identity != expected_identity:
            raise VoiceStudioContractError(
                "PCM chunk 身份与当前 request 不一致", code="validator_identity_mismatch"
            )
        if chunk.sequence != self._expected_sequence:
            raise VoiceStudioContractError(
                f"PCM sequence 必须连续，期望 {self._expected_sequence}，收到 {chunk.sequence}",
                code="validator_sequence_mismatch",
            )
        self.accepted_chunks.append(chunk)
        self._expected_sequence += 1
        if chunk.is_final:
            self._finished = True
        return chunk

    def finish(self) -> tuple[PCMChunk, ...]:
        if self._cancelled:
            raise VoiceStudioContractError(
                "取消后的 PCM stream 不得完成", code="validator_finish_cancelled"
            )
        if not self._finished:
            raise VoiceStudioContractError(
                "PCM stream 缺少 is_final=True 的最后音频块", code="validator_missing_final"
            )
        return tuple(self.accepted_chunks)


__all__ = [
    "PCMChunk",
    "PCMChunkStreamValidator",
    "VoiceStudioAudioProvider",
    "VoiceStudioContractError",
    "VoiceStudioSentenceRequest",
]
