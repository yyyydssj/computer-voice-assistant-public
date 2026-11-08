"""Optional Windows audio endpoint discovery and bounded capture helpers.

The module never imports an audio backend at import time. Missing optional
packages, muted endpoints, zero-length streams, or invalid device pairing are
reported as an unusable measurement instead of silence evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import struct
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class AudioEndpoint:
    index: int
    name: str
    host_api: str
    is_loopback: bool
    max_input_channels: int
    default_sample_rate: float
    # PyAudio exposes hostApi as an integer. Keep a normalized string for
    # compatibility while retaining the raw value for audit metadata.
    host_api_index: int | None = None

    @property
    def native_channels(self) -> int:
        return self.max_input_channels

    @property
    def native_sample_rate_hz(self) -> int:
        return round(self.default_sample_rate)


@dataclass(frozen=True, slots=True)
class CaptureSummary:
    valid: bool
    reason: str | None
    endpoint: AudioEndpoint | None
    sample_rate_hz: int
    channels: int
    frames: int
    lead_in_rms_dbfs: float | None
    active_rms_dbfs: float | None
    post_rms_dbfs: float | None
    active_nonzero: bool
    post_quiet: bool | None
    post_quiet_basis: str | None
    correlation_observed: bool | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["endpoint"] = asdict(self.endpoint) if self.endpoint else None
        return result


def _rms_dbfs(samples: Iterable[int]) -> float | None:
    values = list(samples)
    if not values:
        return None
    rms = math.sqrt(sum(float(v) * float(v) for v in values) / len(values))
    if rms <= 0:
        return -float("inf")
    return 20.0 * math.log10(rms / 32768.0)


def summarize_pcm16(
    pcm: bytes,
    *,
    endpoint: AudioEndpoint,
    sample_rate_hz: int,
    channels: int,
    lead_frames: int,
    active_frames: int,
    post_frames: int,
    quiet_dbfs: float = -60.0,
) -> CaptureSummary:
    """Summarize already-captured PCM without retaining raw audio."""
    window_lengths = (lead_frames, active_frames, post_frames)
    if (
        type(pcm) is not bytes
        or type(sample_rate_hz) is not int
        or sample_rate_hz <= 0
        or type(channels) is not int
        or channels <= 0
        or any(type(length) is not int or length <= 0 for length in window_lengths)
    ):
        return CaptureSummary(False, "invalid_capture_input", endpoint, sample_rate_hz, channels, 0, None, None, None, False, None, None, None)
    frame_width = channels * 2
    if len(pcm) % frame_width:
        return CaptureSummary(False, "unaligned_pcm", endpoint, sample_rate_hz, channels, 0, None, None, None, False, None, None, None)
    frames = len(pcm) // frame_width
    requested = lead_frames + active_frames + post_frames
    if frames < requested:
        return CaptureSummary(False, "insufficient_frames", endpoint, sample_rate_hz, channels, frames, None, None, None, False, None, None, None)
    values = struct.unpack("<" + "h" * (frames * channels), pcm)
    lead_end = lead_frames * channels
    active_end = lead_end + active_frames * channels
    post_end = active_end + post_frames * channels
    lead = values[:lead_end]
    active = values[lead_end:active_end]
    post = values[active_end:post_end]
    lead_db = _rms_dbfs(lead)
    active_db = _rms_dbfs(active)
    post_db = _rms_dbfs(post)
    active_nonzero = any(active)
    absolute_quiet = post_db is not None and post_db <= quiet_dbfs
    baseline_quiet = (
        post_db is not None and lead_db is not None and math.isfinite(lead_db)
        and post_db <= lead_db + 3.0
    )
    adaptive_baseline_quiet = (not endpoint.is_loopback) and baseline_quiet
    post_quiet = absolute_quiet or adaptive_baseline_quiet
    post_quiet_basis = (
        "absolute_threshold" if absolute_quiet
        else "lead_noise_floor_margin" if adaptive_baseline_quiet
        else None
    )
    return CaptureSummary(
        valid=bool(lead) and active_nonzero,
        reason=None if active_nonzero else "active_capture_zero",
        endpoint=endpoint,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        frames=frames,
        lead_in_rms_dbfs=lead_db,
        active_rms_dbfs=active_db,
        post_rms_dbfs=post_db,
        active_nonzero=active_nonzero,
        post_quiet=post_quiet,
        post_quiet_basis=post_quiet_basis,
        correlation_observed=None,
    )


def enumerate_pyaudiowpatch() -> tuple[AudioEndpoint, ...]:
    """Enumerate WASAPI endpoints when PyAudioWPatch is installed."""
    try:
        import pyaudiowpatch as pyaudio  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("pyaudiowpatch_not_installed") from exc
    with pyaudio.PyAudio() as audio:
        result: list[AudioEndpoint] = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            raw_host_api = info.get("hostApi")
            result.append(AudioEndpoint(
                index=index,
                name=str(info.get("name", "")),
                host_api=str(raw_host_api if raw_host_api is not None else ""),
                is_loopback=bool(info.get("isLoopbackDevice", False)),
                max_input_channels=int(info.get("maxInputChannels", 0)),
                default_sample_rate=float(info.get("defaultSampleRate", 0)),
                host_api_index=raw_host_api if isinstance(raw_host_api, int) else None,
            ))
        return tuple(result)


def choose_independent_endpoints(endpoints: tuple[AudioEndpoint, ...]) -> tuple[AudioEndpoint, AudioEndpoint]:
    """Choose distinct WASAPI loopback and physical-microphone endpoints.

    MME/DirectSound-looking entries are not accepted for this evidence path.
    The real capture path opens each endpoint with its advertised format rather
    than forcing a mono/48 kHz format that some drivers silently zero-fill.
    """
    wasapi = {"windows wasapi", "2"}
    loopbacks = [
        item for item in endpoints
        if item.is_loopback
        and item.max_input_channels > 0
        and item.host_api.casefold() in wasapi
        and item.native_sample_rate_hz > 0
    ]
    microphones = [
        item for item in endpoints
        if not item.is_loopback
        and item.max_input_channels > 0
        and item.host_api.casefold() in wasapi
        and item.native_sample_rate_hz > 0
    ]
    if not loopbacks:
        raise RuntimeError("wasapi_loopback_endpoint_missing")
    # Stereo Mix/What U Hear are system-mix inputs, not independent acoustic
    # sensors. Never fall back to one when selecting the microphone endpoint.
    microphones = [
        item for item in microphones
        if not any(token in item.name.casefold() for token in (
            "stereo mix",
            "what u hear",
            "what-u-hear",
        ))
    ]
    if not microphones:
        raise RuntimeError("independent_microphone_endpoint_missing")
    microphone = microphones[0]
    loopback = loopbacks[0]
    if loopback.index == microphone.index:
        raise RuntimeError("capture_endpoints_not_independent")
    return loopback, microphone


__all__ = ["AudioEndpoint", "CaptureSummary", "choose_independent_endpoints", "enumerate_pyaudiowpatch", "summarize_pcm16"]
