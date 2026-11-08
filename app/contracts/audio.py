"""音频、Provider 与可观测播放事实契约。"""

from __future__ import annotations

from dataclasses import dataclass

from .base import ContractError, StrictContract, parse_utc_timestamp, require_choice, require_identifier, require_non_empty, require_positive


@dataclass(frozen=True, slots=True)
class AudioFrame(StrictContract):
    audio_session_id: str
    provider_generation: int
    stream_id: str
    event_sequence: int
    frame_index: int
    captured_at: str
    duration_ms: int
    device_epoch: int = 1
    sample_rate_hz: int = 16000
    channel_count: int = 1
    sample_format: str = "pcm_s16le"
    sample_count: int = 320
    payload_size_bytes: int = 640

    def __post_init__(self) -> None:
        require_identifier("audio_session_id", self.audio_session_id)
        require_positive("provider_generation", self.provider_generation)
        require_identifier("stream_id", self.stream_id)
        require_positive("event_sequence", self.event_sequence)
        require_positive("frame_index", self.frame_index, allow_zero=True)
        parse_utc_timestamp("captured_at", self.captured_at)
        require_positive("duration_ms", self.duration_ms)
        require_positive("device_epoch", self.device_epoch)
        require_positive("sample_rate_hz", self.sample_rate_hz)
        require_positive("channel_count", self.channel_count)
        require_choice("AudioFrame.sample_format", self.sample_format, {"pcm_s16le"})
        require_positive("sample_count", self.sample_count)
        require_positive("payload_size_bytes", self.payload_size_bytes)
        if self.sample_count * 1000 != self.sample_rate_hz * self.duration_ms:
            raise ContractError("duration_ms 与 sample_rate_hz/sample_count 不一致")
        expected_payload_size = self.sample_count * self.channel_count * 2
        if self.payload_size_bytes != expected_payload_size:
            raise ContractError("payload_size_bytes 与 PCM 帧尺寸不一致")


@dataclass(frozen=True, slots=True)
class AudioSession(StrictContract):
    audio_session_id: str
    session_id: str
    provider_generation: int
    stream_id: str
    state: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("audio_session_id", self.audio_session_id)
        require_identifier("session_id", self.session_id)
        require_positive("provider_generation", self.provider_generation)
        require_identifier("stream_id", self.stream_id)
        require_choice("AudioSession.state", self.state, {"IDLE", "WARMING", "ACTIVE", "FINAL_ACCEPTED", "REJECTED", "CLOSING", "FAILED"})
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class ProviderState(StrictContract):
    provider_name: str
    generation: int
    state: str
    changed_at: str

    def __post_init__(self) -> None:
        require_identifier("provider_name", self.provider_name)
        require_positive("generation", self.generation)
        require_choice("ProviderState.state", self.state, {"READY", "FAILED", "COOLDOWN", "PROBING", "RECOVERED_PENDING"})
        parse_utc_timestamp("changed_at", self.changed_at)


@dataclass(frozen=True, slots=True)
class TTSReply(StrictContract):
    reply_id: str
    request_id: str
    state: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("reply_id", self.reply_id)
        require_identifier("request_id", self.request_id)
        require_choice("TTSReply.state", self.state, {"QUEUED", "SYNTHESIZING", "READY", "COMMITTED_NOT_STARTED", "PLAYING", "PLAYED", "ABORTED"})
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class TTSSegment(StrictContract):
    reply_id: str
    segment_id: str
    provider_request_id: str
    provider_generation: int
    state: str
    text_hash: str
    created_at: str

    def __post_init__(self) -> None:
        require_identifier("reply_id", self.reply_id)
        require_identifier("segment_id", self.segment_id)
        require_identifier("provider_request_id", self.provider_request_id)
        require_positive("provider_generation", self.provider_generation)
        require_choice("TTSSegment.state", self.state, {"NOT_COMMITTED", "COMMITTED_NOT_STARTED", "PLAYBACK_STARTED", "PARTIALLY_PLAYED", "PLAYED", "ABORTED"})
        require_non_empty("text_hash", self.text_hash, maximum=256)
        parse_utc_timestamp("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class PlaybackFact(StrictContract):
    reply_id: str
    segment_id: str
    state: str
    event_sequence: int
    observed_at: str
    playback_offset_ms: int

    def __post_init__(self) -> None:
        require_identifier("reply_id", self.reply_id)
        require_identifier("segment_id", self.segment_id)
        require_choice("PlaybackFact.state", self.state, {"COMMITTED_NOT_STARTED", "PLAYBACK_STARTED", "PARTIALLY_PLAYED", "PLAYED", "ABORTED"})
        require_positive("event_sequence", self.event_sequence)
        parse_utc_timestamp("observed_at", self.observed_at)
        require_positive("playback_offset_ms", self.playback_offset_ms, allow_zero=True)