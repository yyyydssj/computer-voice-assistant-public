from __future__ import annotations

"""Test-only playback failure fixtures.

These backends implement the existing ``PlaybackBackend`` protocol without
opening an audio device, calling WinMM/PortAudio, or sending audio anywhere.
They are deliberately scoped to controlled fault evidence and must never be
used as evidence of physical speaker output or user audibility.
"""

from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Final

from .playback_worker import StopResult


EVIDENCE_KIND: Final[str] = "controlled_fault_fixture"


class FaultFixtureMode(str, Enum):
    """Supported deterministic faults for the offline playback seam."""

    RESET_ERROR = "reset_error"
    DROP_CALLBACK = "drop_callback"
    DELAY_CALLBACK = "delay_callback"
    WRONG_BUFFER_CALLBACK = "wrong_buffer_callback"


@dataclass(slots=True)
class ControlledFaultBackend:
    """An offline ``PlaybackBackend`` implementation for failure tests.

    ``PlaybackWorker`` sees the same ``start``/``poll_complete``/``stop``/
    ``close`` methods as it sees on a real backend.  The fixture only records
    submitted bytes and returns controlled protocol outcomes; it never opens a
    device or invokes an audio callback.
    """

    mode: FaultFixtureMode
    delay_seconds: float = 0.0
    backend_name: str = "controlled_fault_fixture"
    evidence_kind: str = EVIDENCE_KIND
    started: list[bytes] = field(default_factory=list)
    stop_calls: int = 0
    close_calls: int = 0
    callback_attempts: int = 0
    submitted_buffer_id: str | None = None
    callback_buffer_id: str | None = None
    device_opened: bool = False
    real_audio_calls: int = 0
    _done: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if self.mode is not FaultFixtureMode.DELAY_CALLBACK and self.delay_seconds:
            raise ValueError("delay_seconds is only valid for delay_callback")

    def start(self, audio: bytes) -> None:
        """Record a submission; deliberately do not play it."""

        if type(audio) is not bytes or not audio:
            raise ValueError("audio must be non-empty bytes")
        self.started.append(audio)
        self.submitted_buffer_id = f"submitted-{len(self.started)}"
        self._done.clear()

    def poll_complete(self) -> bool:
        """Keep the ticket active until the worker exercises the fault."""

        return self._done.is_set()

    def stop(self, timeout: float) -> StopResult:
        """Return the mode-specific fault without touching an audio device."""

        del timeout
        self.stop_calls += 1
        self.callback_attempts += 1

        if self.mode is FaultFixtureMode.RESET_ERROR:
            raise OSError("fixture_waveout_reset_error")

        if self.mode is FaultFixtureMode.DROP_CALLBACK:
            return StopResult(False, "timeout", "fixture_callback_dropped")

        if self.mode is FaultFixtureMode.DELAY_CALLBACK:
            # A late callback is intentionally returned after the caller's
            # stop boundary.  PlaybackWorker must keep STOP_TIMEOUT terminal
            # and must not upgrade it when this result arrives later.
            time.sleep(self.delay_seconds)
            self.callback_buffer_id = self.submitted_buffer_id
            self._done.set()
            return StopResult(True, "backend_buffer_returned", "fixture_delayed_callback")

        if self.mode is FaultFixtureMode.WRONG_BUFFER_CALLBACK:
            # Simulate WOM_DONE for a different buffer.  No completion event is
            # set and no stop acknowledgement is returned.
            self.callback_buffer_id = "wrong-buffer"
            return StopResult(False, "unknown", "fixture_wrong_buffer_callback")

        raise AssertionError(f"unsupported fault fixture mode: {self.mode!r}")

    def close(self) -> None:
        self.close_calls += 1
        # This is intentionally a no-op: there is no device handle to close.


def create_fault_fixture(
    mode: FaultFixtureMode | str,
    *,
    delay_seconds: float = 0.0,
) -> ControlledFaultBackend:
    """Create one explicit offline fixture backend.

    The factory accepts string mode names to keep test parametrization simple,
    while returning a backend that remains compatible with ``PlaybackBackend``.
    """

    resolved_mode = mode if isinstance(mode, FaultFixtureMode) else FaultFixtureMode(mode)
    return ControlledFaultBackend(resolved_mode, delay_seconds=delay_seconds)


def reset_error() -> ControlledFaultBackend:
    """Fixture: reset raises, so the worker must enter ``UNKNOWN``."""

    return create_fault_fixture(FaultFixtureMode.RESET_ERROR)


def drop_callback() -> ControlledFaultBackend:
    """Fixture: callback never arrives, so the worker must enter ``STOP_TIMEOUT``."""

    return create_fault_fixture(FaultFixtureMode.DROP_CALLBACK)


def delay_callback(delay_seconds: float = 0.35) -> ControlledFaultBackend:
    """Fixture: callback arrives after a configurable stop boundary."""

    return create_fault_fixture(
        FaultFixtureMode.DELAY_CALLBACK,
        delay_seconds=delay_seconds,
    )


def wrong_buffer_callback() -> ControlledFaultBackend:
    """Fixture: callback identifies another buffer and cannot acknowledge stop."""

    return create_fault_fixture(FaultFixtureMode.WRONG_BUFFER_CALLBACK)


__all__ = [
    "ControlledFaultBackend",
    "EVIDENCE_KIND",
    "FaultFixtureMode",
    "create_fault_fixture",
    "delay_callback",
    "drop_callback",
    "reset_error",
    "wrong_buffer_callback",
]
