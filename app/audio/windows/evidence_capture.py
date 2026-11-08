"""Bounded, opt-in audio evidence capture orchestration for Windows.

This module deliberately keeps device I/O out of import-time and default code
paths.  The optional ``pyaudiowpatch`` dependency is imported only when the
caller explicitly enables device I/O.  Unit tests can use the generic
``collect_serial_summaries`` function with fake streams and never touch audio
hardware.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import importlib
from typing import Any, Callable, Protocol
import threading

from app.audio.windows.capture import (
    AudioEndpoint,
    CaptureSummary,
    choose_independent_endpoints,
    summarize_pcm16,
)


class CaptureStream(Protocol):
    """Minimal stream surface used by the bounded capture loop."""

    def read(self, num_frames: int, exception_on_overflow: bool = False) -> bytes:
        ...

    def stop_stream(self) -> None:
        ...

    def close(self) -> None:
        ...


StreamFactory = Callable[[AudioEndpoint, "EvidenceCaptureConfig"], CaptureStream]
ConfigFactory = Callable[[AudioEndpoint, "EvidenceCaptureConfig"], "EvidenceCaptureConfig"]


@dataclass(frozen=True, slots=True)
class EvidenceCaptureConfig:
    """A finite capture window and its PCM interpretation."""

    sample_rate_hz: int = 48_000
    channels: int = 1
    lead_in_ms: int = 100
    active_ms: int = 500
    post_ms: int = 200
    frames_per_buffer: int = 1_024
    quiet_dbfs: float = -60.0
    max_total_ms: int = 60_000

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0 or self.channels <= 0:
            raise ValueError("capture_format_must_be_positive")
        if self.lead_in_ms <= 0 or self.active_ms <= 0 or self.post_ms <= 0:
            raise ValueError("capture_windows_must_be_positive")
        if self.frames_per_buffer <= 0:
            raise ValueError("frames_per_buffer_must_be_positive")
        if self.max_total_ms <= 0:
            raise ValueError("max_total_ms_must_be_positive")
        if self.total_ms > self.max_total_ms:
            raise ValueError("capture_window_exceeds_bound")

    @property
    def total_ms(self) -> int:
        return self.lead_in_ms + self.active_ms + self.post_ms

    def for_endpoint(self, endpoint: AudioEndpoint) -> "EvidenceCaptureConfig":
        """Resolve the bounded capture format against endpoint metadata."""
        if endpoint.max_input_channels <= 0 or endpoint.native_sample_rate_hz <= 0:
            raise ValueError("endpoint_native_format_missing")
        channels = min(self.channels, endpoint.native_channels)
        if channels <= 0:
            raise ValueError("endpoint_channel_format_incompatible")
        sample_rate = self.sample_rate_hz
        if endpoint.is_loopback:
            channels = endpoint.native_channels
            sample_rate = endpoint.native_sample_rate_hz
        return replace(self, channels=channels, sample_rate_hz=sample_rate)

    @property
    def lead_frames(self) -> int:
        return self._ms_to_frames(self.lead_in_ms)

    @property
    def active_frames(self) -> int:
        return self._ms_to_frames(self.active_ms)

    @property
    def post_frames(self) -> int:
        return self._ms_to_frames(self.post_ms)

    @property
    def total_frames(self) -> int:
        return self.lead_frames + self.active_frames + self.post_frames

    def _ms_to_frames(self, milliseconds: int) -> int:
        frames = round(self.sample_rate_hz * milliseconds / 1_000)
        return max(1, frames)


@dataclass(frozen=True, slots=True)
class CaptureDependencyStatus:
    """Dependency and device-I/O gate state suitable for an evidence ledger."""

    name: str
    available: bool
    device_io_enabled: bool
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.available or not self.device_io_enabled

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"blocked": self.blocked}


@dataclass(frozen=True, slots=True)
class EndpointCaptureResult:
    """Result for one endpoint, retaining metadata but not raw audio."""

    endpoint: AudioEndpoint
    summary: CaptureSummary | None
    requested_frames: int
    read_frames: int
    started: bool
    stopped: bool
    cleanup_ok: bool
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.summary is not None and self.summary.valid and self.cleanup_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": asdict(self.endpoint),
            "summary": self.summary.to_dict() if self.summary else None,
            "requested_frames": self.requested_frames,
            "read_frames": self.read_frames,
            "started": self.started,
            "stopped": self.stopped,
            "cleanup_ok": self.cleanup_ok,
            "reason": self.reason,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class EvidenceCaptureReport:
    """Serial loopback/microphone capture report with explicit block reasons."""

    dependency: CaptureDependencyStatus
    loopback: EndpointCaptureResult | None
    microphone: EndpointCaptureResult | None
    blocked_reasons: tuple[str, ...]
    serial: bool = True

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reasons) or not self.dependency.available

    @property
    def valid(self) -> bool:
        return (
            not self.blocked
            and self.loopback is not None
            and self.microphone is not None
            and self.loopback.valid
            and self.microphone.valid
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency": self.dependency.to_dict(),
            "loopback": self.loopback.to_dict() if self.loopback else None,
            "microphone": self.microphone.to_dict() if self.microphone else None,
            "blocked_reasons": list(self.blocked_reasons),
            "serial": self.serial,
            "blocked": self.blocked,
            "valid": self.valid,
        }


def capture_summary_from_stream(
    stream: CaptureStream,
    *,
    endpoint: AudioEndpoint,
    config: EvidenceCaptureConfig,
    post_gate: threading.Event | None = None,
) -> EndpointCaptureResult:
    """Read one bounded window, optionally gating post on playback cleanup.

    The lead and active windows are collected before the gate.  When supplied,
    ``post_gate`` must be set by the playback owner only after its terminal
    cleanup/stop attempt; this prevents post quiet from covering ongoing output.
    """
    requested_frames = config.total_frames
    frame_width = config.channels * 2
    chunks: list[bytes] = []
    read_frames = 0
    started = True
    stopped = False
    cleanup_ok = True
    reason: str | None = None

    try:
        # Collect lead + active first so the gate can be tied to the playback
        # terminal event without losing the evidence that output occurred.
        pre_gate_frames = config.lead_frames + config.active_frames
        while read_frames < pre_gate_frames:
            frames_to_read = min(config.frames_per_buffer, pre_gate_frames - read_frames)
            chunk = stream.read(frames_to_read, exception_on_overflow=False)
            if type(chunk) is not bytes:
                reason = "stream_read_not_bytes"
                break
            if not chunk:
                reason = "stream_read_empty"
                break
            if len(chunk) % frame_width:
                reason = "stream_chunk_unaligned"
                break
            chunk_frames = len(chunk) // frame_width
            if chunk_frames > frames_to_read:
                reason = "stream_read_exceeded_bound"
                break
            chunks.append(chunk)
            read_frames += chunk_frames

        if reason is None and post_gate is not None:
            # Keep draining while playback is still active.  Waiting without
            # reads lets WASAPI queue old playback frames, which would make the
            # first samples after the gate a false post-stop window.
            while not post_gate.is_set():
                chunk = stream.read(config.frames_per_buffer, exception_on_overflow=False)
                if type(chunk) is not bytes:
                    reason = "stream_read_not_bytes"
                    break
                if not chunk:
                    reason = "stream_read_empty"
                    break
                if len(chunk) % frame_width:
                    reason = "stream_chunk_unaligned"
                    break
                if len(chunk) // frame_width > config.frames_per_buffer:
                    reason = "stream_read_exceeded_bound"
                    break

        while reason is None and read_frames < requested_frames:
            frames_to_read = min(config.frames_per_buffer, requested_frames - read_frames)
            chunk = stream.read(frames_to_read, exception_on_overflow=False)
            if type(chunk) is not bytes:
                reason = "stream_read_not_bytes"
                break
            if not chunk:
                reason = "stream_read_empty"
                break
            if len(chunk) % frame_width:
                reason = "stream_chunk_unaligned"
                break
            chunk_frames = len(chunk) // frame_width
            if chunk_frames > frames_to_read:
                reason = "stream_read_exceeded_bound"
                break
            chunks.append(chunk)
            read_frames += chunk_frames

        if reason is None and read_frames == requested_frames:
            summary = summarize_pcm16(
                b"".join(chunks),
                endpoint=endpoint,
                sample_rate_hz=config.sample_rate_hz,
                channels=config.channels,
                lead_frames=config.lead_frames,
                active_frames=config.active_frames,
                post_frames=config.post_frames,
                quiet_dbfs=config.quiet_dbfs,
            )
            reason = summary.reason
        else:
            summary = None
            if reason is None:
                reason = "stream_read_short"
    except Exception as exc:  # noqa: BLE001 - evidence must fail closed on backend errors.
        summary = None
        reason = f"stream_read_error:{type(exc).__name__}"
    finally:
        try:
            stream.stop_stream()
            stopped = True
        except Exception:  # noqa: BLE001 - cleanup state is reported explicitly.
            cleanup_ok = False
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - cleanup state is reported explicitly.
            cleanup_ok = False

    if not cleanup_ok and reason is None:
        reason = "stream_cleanup_error"
    return EndpointCaptureResult(
        endpoint=endpoint,
        summary=summary,
        requested_frames=requested_frames,
        read_frames=read_frames,
        started=started,
        stopped=stopped,
        cleanup_ok=cleanup_ok,
        reason=reason,
    )


def _failed_endpoint_result(
    endpoint: AudioEndpoint,
    config: EvidenceCaptureConfig,
    reason: str,
) -> EndpointCaptureResult:
    return EndpointCaptureResult(
        endpoint=endpoint,
        summary=None,
        requested_frames=config.total_frames,
        read_frames=0,
        started=False,
        stopped=False,
        cleanup_ok=True,
        reason=reason,
    )


def collect_serial_summaries(
    loopback: AudioEndpoint,
    microphone: AudioEndpoint,
    *,
    stream_factory: StreamFactory,
    config: EvidenceCaptureConfig | None = None,
    config_factory: ConfigFactory | None = None,
    post_gate: threading.Event | None = None,
) -> EvidenceCaptureReport:
    """Capture loopback then microphone serially through an injected factory.

    The factory is called at most once per endpoint and no streams are opened by
    this function itself.  Serial ordering avoids concurrent hardware access and
    makes this function straightforward to exercise with fake streams.
    """
    config = config or EvidenceCaptureConfig()
    dependency = CaptureDependencyStatus(
        name="injected_stream_factory",
        available=True,
        device_io_enabled=True,
    )
    if loopback.index == microphone.index:
        reason = "capture_endpoints_not_independent"
        failed_loopback = _failed_endpoint_result(loopback, config, reason)
        failed_microphone = _failed_endpoint_result(microphone, config, reason)
        return EvidenceCaptureReport(dependency, failed_loopback, failed_microphone, (reason,))
    if not loopback.is_loopback:
        reason = "loopback_endpoint_required"
        return EvidenceCaptureReport(
            dependency,
            _failed_endpoint_result(loopback, config, reason),
            _failed_endpoint_result(microphone, config, reason),
            (reason,),
        )
    if microphone.is_loopback:
        reason = "independent_microphone_required"
        return EvidenceCaptureReport(
            dependency,
            _failed_endpoint_result(loopback, config, reason),
            _failed_endpoint_result(microphone, config, reason),
            (reason,),
        )

    results: list[EndpointCaptureResult] = []
    resolve_config = config_factory or (lambda _endpoint, original: original)
    for endpoint in (loopback, microphone):
        try:
            endpoint_config = resolve_config(endpoint, config)
        except Exception as exc:  # noqa: BLE001 - format mismatch is Blocked.
            result = _failed_endpoint_result(endpoint, config, f"endpoint_format_error:{type(exc).__name__}")
        else:
            try:
                stream = stream_factory(endpoint, endpoint_config)
            except Exception as exc:  # noqa: BLE001 - report the endpoint as blocked.
                result = _failed_endpoint_result(endpoint, endpoint_config, f"stream_open_error:{type(exc).__name__}")
            else:
                result = capture_summary_from_stream(
                    stream, endpoint=endpoint, config=endpoint_config, post_gate=post_gate,
                )
        results.append(result)

    loopback_result, microphone_result = results
    blocked_reasons = tuple(
        item
        for item in (
            f"loopback:{loopback_result.reason}" if loopback_result.reason else None,
            f"microphone:{microphone_result.reason}" if microphone_result.reason else None,
        )
        if item is not None
    )
    return EvidenceCaptureReport(
        dependency,
        loopback_result,
        microphone_result,
        blocked_reasons,
    )


def collect_synchronized_summaries(
    loopback: AudioEndpoint,
    microphone: AudioEndpoint,
    *,
    stream_factory: StreamFactory,
    config: EvidenceCaptureConfig | None = None,
    config_factory: ConfigFactory | None = None,
    post_gate: threading.Event | None = None,
) -> EvidenceCaptureReport:
    """Capture both endpoints over the same lead/active/post timeline.

    Unlike ``collect_serial_summaries``, this opens both streams before reading
    either one and reads them in separate bounded workers.  It is the required
    seam when active output and post-playback silence must be attributed to the
    same playback batch on two independent endpoints.
    """
    config = config or EvidenceCaptureConfig()
    dependency = CaptureDependencyStatus(
        name="injected_stream_factory",
        available=True,
        device_io_enabled=True,
    )
    if loopback.index == microphone.index:
        reason = "capture_endpoints_not_independent"
        return EvidenceCaptureReport(
            dependency,
            _failed_endpoint_result(loopback, config, reason),
            _failed_endpoint_result(microphone, config, reason),
            (reason,),
        )
    if not loopback.is_loopback:
        reason = "loopback_endpoint_required"
        return EvidenceCaptureReport(
            dependency,
            _failed_endpoint_result(loopback, config, reason),
            _failed_endpoint_result(microphone, config, reason),
            (reason,),
        )
    if microphone.is_loopback:
        reason = "independent_microphone_required"
        return EvidenceCaptureReport(
            dependency,
            _failed_endpoint_result(loopback, config, reason),
            _failed_endpoint_result(microphone, config, reason),
            (reason,),
        )

    resolve_config = config_factory or (lambda _endpoint, original: original)
    endpoint_configs: dict[int, EvidenceCaptureConfig] = {}
    streams: dict[int, CaptureStream] = {}
    results: dict[int, EndpointCaptureResult] = {}
    for endpoint in (loopback, microphone):
        try:
            endpoint_config = resolve_config(endpoint, config)
            endpoint_configs[endpoint.index] = endpoint_config
            streams[endpoint.index] = stream_factory(endpoint, endpoint_config)
        except Exception as exc:  # noqa: BLE001 - evidence must fail closed.
            reason = f"endpoint_format_error:{type(exc).__name__}" if endpoint.index not in endpoint_configs else f"stream_open_error:{type(exc).__name__}"
            results[endpoint.index] = _failed_endpoint_result(
                endpoint, endpoint_configs.get(endpoint.index, config), reason,
            )
            break

    if len(streams) != 2:
        for stream in streams.values():
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        for endpoint in (loopback, microphone):
            results.setdefault(
                endpoint.index,
                _failed_endpoint_result(endpoint, endpoint_configs.get(endpoint.index, config), "paired_stream_open_failed"),
            )
        blocked_reasons = tuple(
            item.reason for item in (results[loopback.index], results[microphone.index]) if item.reason
        )
        return EvidenceCaptureReport(dependency, results[loopback.index], results[microphone.index], blocked_reasons)

    def capture_endpoint(endpoint: AudioEndpoint) -> None:
        results[endpoint.index] = capture_summary_from_stream(
            streams[endpoint.index], endpoint=endpoint,
            config=endpoint_configs[endpoint.index], post_gate=post_gate,
        )

    threads = [threading.Thread(target=capture_endpoint, args=(endpoint,), daemon=True) for endpoint in (loopback, microphone)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    loopback_result, microphone_result = results[loopback.index], results[microphone.index]
    blocked_reasons = tuple(
        item for item in (
            f"loopback:{loopback_result.reason}" if loopback_result.reason else None,
            f"microphone:{microphone_result.reason}" if microphone_result.reason else None,
        ) if item is not None
    )
    return EvidenceCaptureReport(dependency, loopback_result, microphone_result, blocked_reasons, serial=False)


def _enumerate_from_audio(audio: Any) -> tuple[AudioEndpoint, ...]:
    endpoints: list[AudioEndpoint] = []
    for index in range(int(audio.get_device_count())):
        info = audio.get_device_info_by_index(index)
        raw_host_api = info.get("hostApi")
        endpoints.append(
            AudioEndpoint(
                index=index,
                name=str(info.get("name", "")),
                host_api=str(raw_host_api if raw_host_api is not None else ""),
                is_loopback=bool(info.get("isLoopbackDevice", False)),
                max_input_channels=int(info.get("maxInputChannels", 0)),
                default_sample_rate=float(info.get("defaultSampleRate", 0)),
                host_api_index=raw_host_api if isinstance(raw_host_api, int) else None,
            )
        )
    return tuple(endpoints)


def _missing_dependency_report(reason: str, *, device_io_enabled: bool) -> EvidenceCaptureReport:
    dependency = CaptureDependencyStatus(
        name="pyaudiowpatch",
        available=False,
        device_io_enabled=device_io_enabled,
        reason=reason,
    )
    return EvidenceCaptureReport(dependency, None, None, (reason,))


def collect_pyaudiowpatch_summaries(
    *,
    config: EvidenceCaptureConfig | None = None,
    enable_device_io: bool = False,
    pyaudio_module: Any | None = None,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> EvidenceCaptureReport:
    """Opt-in real endpoint capture using the optional PyAudioWPatch package.

    With the default ``enable_device_io=False`` this function returns a blocked
    report without importing the optional package or touching a device.
    """
    config = config or EvidenceCaptureConfig()
    if not enable_device_io:
        return _missing_dependency_report("device_io_not_enabled", device_io_enabled=False)

    if pyaudio_module is None:
        try:
            pyaudio_module = import_module("pyaudiowpatch")
        except (ImportError, ModuleNotFoundError):
            return _missing_dependency_report(
                "pyaudiowpatch_not_installed",
                device_io_enabled=True,
            )

    audio = None
    try:
        audio = pyaudio_module.PyAudio()
        endpoints = _enumerate_from_audio(audio)
        loopback, microphone = choose_independent_endpoints(endpoints)

        def stream_factory(endpoint: AudioEndpoint, capture_config: EvidenceCaptureConfig) -> CaptureStream:
            return audio.open(
                format=getattr(pyaudio_module, "paInt16", 8),
                channels=capture_config.channels,
                rate=capture_config.sample_rate_hz,
                input=True,
                frames_per_buffer=capture_config.frames_per_buffer,
                input_device_index=endpoint.index,
            )

        report = collect_serial_summaries(
            loopback,
            microphone,
            stream_factory=stream_factory,
            config=config,
            config_factory=lambda endpoint, original: original.for_endpoint(endpoint),
        )
    except RuntimeError as exc:
        # Endpoint selection already provides actionable, evidence-safe reasons;
        # preserve them instead of hiding them behind a generic backend error.
        reason = str(exc) or "pyaudiowpatch_runtime_error"
        return EvidenceCaptureReport(
            CaptureDependencyStatus(
                name="pyaudiowpatch",
                available=True,
                device_io_enabled=True,
                reason=reason,
            ),
            None,
            None,
            (reason,),
        )
    except Exception as exc:  # noqa: BLE001 - real device failures are Blocked.
        reason = f"pyaudiowpatch_capture_error:{type(exc).__name__}"
        return EvidenceCaptureReport(
            CaptureDependencyStatus(
                name="pyaudiowpatch",
                available=True,
                device_io_enabled=True,
                reason=reason,
            ),
            None,
            None,
            (reason,),
        )
    finally:
        if audio is not None:
            try:
                audio.terminate()
            except Exception:
                pass

    return EvidenceCaptureReport(
        CaptureDependencyStatus(
            name="pyaudiowpatch",
            available=True,
            device_io_enabled=True,
        ),
        report.loopback,
        report.microphone,
        report.blocked_reasons,
    )


__all__ = [
    "CaptureDependencyStatus",
    "CaptureStream",
    "EndpointCaptureResult",
    "EvidenceCaptureConfig",
    "EvidenceCaptureReport",
    "capture_summary_from_stream",
    "collect_pyaudiowpatch_summaries",
    "collect_serial_summaries",
    "collect_synchronized_summaries",
]


