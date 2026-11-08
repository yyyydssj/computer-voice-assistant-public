"""Default monotonic clock for the streaming TTS timing instrumentation.

The timestamps in :mod:`app.tts.streaming_session` and
:mod:`app.tts.pcm_playback` are only comparable when every stamp shares one
monotonic basis, and they are only useful when that basis can resolve the spans
being reported. On Windows the obvious candidate fails the second requirement:

- ``time.monotonic`` is ``GetTickCount64()`` — a millisecond counter whose
  granularity is really **15.625 ms** (``time.get_clock_info("monotonic")
  .resolution == 0.015625``). Measured on the real host: 200 000 consecutive
  calls produced **zero** positive deltas, so worker-side spans collapse to
  ``0.000 ms`` (read as "unmeasurable", not as "instantaneous");
- ``time.perf_counter`` is ``QueryPerformanceCounter`` with a resolution of
  ``1e-07`` s, and it is also monotonic (it never goes backwards while the
  process runs), so it is a drop-in replacement for stamping.

``monotonic_seconds`` is therefore ``time.perf_counter`` on ``win32`` and
``time.monotonic`` everywhere else. Only the **default** changes: an explicitly
injected ``clock`` still wins, and every consumer keeps its existing semantics
because both choices are monotonic clocks used purely for differences.

Nothing here touches the network, a device, the registry or a child process; the
module only re-exports a clock and one read-only resolution helper.
"""
from __future__ import annotations

import sys
import time
from typing import Callable

#: The process-wide default monotonic clock used when no ``clock`` is injected.
#: ``time.perf_counter`` on Windows because ``GetTickCount64`` cannot resolve the
#: worker spans (15.625 ms granularity, see the module docstring); the ordinary
#: ``time.monotonic`` elsewhere.
monotonic_seconds: Callable[[], float] = (
    time.perf_counter if sys.platform == "win32" else time.monotonic
)

#: Names that :func:`time.get_clock_info` knows and that may back a clock object.
_KNOWN_CLOCK_NAMES = ("perf_counter", "monotonic", "process_time", "thread_time", "time")


def clock_resolution_seconds(clock: Callable[[], float] | None = None) -> float:
    """Resolution in seconds of ``clock``; the default clock when omitted.

    This is the read-only diagnostic that turns the Windows fact above into
    something a test or a runner can check instead of assume: ask it about
    ``time.monotonic`` and it answers ``0.015625`` (15.625 ms), ask it about the
    shipped default and it answers ``1e-07`` on Windows. It never calls the
    clock, never stores anything and changes no behaviour; an unknown callable
    raises ``ValueError`` and a non-callable raises ``TypeError``.
    """

    target = monotonic_seconds if clock is None else clock
    if not callable(target):
        raise TypeError("clock 必须可调用")
    for name in _KNOWN_CLOCK_NAMES:
        if getattr(time, name) is target:
            return float(time.get_clock_info(name).resolution)
    raise ValueError("clock_resolution_unknown")


__all__ = [
    "clock_resolution_seconds",
    "monotonic_seconds",
]
