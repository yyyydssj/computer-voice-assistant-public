"""Optional Windows audio endpoint and evidence-capture APIs.

Importing this package is side-effect free: optional audio backends are only
loaded by explicit capture functions with device I/O enabled.
"""

from .capture import (
    AudioEndpoint,
    CaptureSummary,
    choose_independent_endpoints,
    enumerate_pyaudiowpatch,
    summarize_pcm16,
)
from .evidence_capture import (
    CaptureDependencyStatus,
    CaptureStream,
    EndpointCaptureResult,
    EvidenceCaptureConfig,
    EvidenceCaptureReport,
    capture_summary_from_stream,
    collect_pyaudiowpatch_summaries,
    collect_serial_summaries,
    collect_synchronized_summaries,
)

__all__ = [
    "AudioEndpoint",
    "CaptureDependencyStatus",
    "CaptureStream",
    "CaptureSummary",
    "EndpointCaptureResult",
    "EvidenceCaptureConfig",
    "EvidenceCaptureReport",
    "capture_summary_from_stream",
    "choose_independent_endpoints",
    "collect_pyaudiowpatch_summaries",
    "collect_serial_summaries",
    "collect_synchronized_summaries",
    "enumerate_pyaudiowpatch",
    "summarize_pcm16",
]
