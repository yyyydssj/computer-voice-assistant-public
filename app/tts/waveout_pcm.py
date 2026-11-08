"""Windows multi-buffer ``waveOut`` backend for streaming PCM16 playback.

Three reusable ``WAVEHDR`` buffers rotate so a sentence can keep playing while
later chunks arrive. A buffer is only rewritten after its own ``WOM_DONE`` has
been observed, and the stop acknowledgement keeps the existing project meaning:
``waveOutReset`` plus every submitted buffer returning. That confirms the backend
released its buffers; it is not evidence that a physical speaker went silent or
that a person heard anything.

The WinMM facade is injectable, so the whole three-buffer lifecycle is testable
offline through a deterministic fake that performs no device I/O.
"""
from __future__ import annotations

import array
import ctypes
from dataclasses import dataclass, replace
from enum import Enum
import math
import sys
import threading
from typing import Any, Callable

from .clock import monotonic_seconds
from .pcm_playback import PCMBufferBusy, PCMPlaybackError
from .playback_worker import StopResult


SAMPLE_RATE = 24_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000
_WAVE_FORMAT_PCM = 1
_CALLBACK_FUNCTION = 0x00030000
_WOM_DONE = 0x3BD
_MMSYSERR_NOERROR = 0
_WAVE_MAPPER = 0xFFFFFFFF
# WHDR_DONE is set by the driver when a buffer returns and must be cleared
# before resubmitting. WHDR_PREPARED is set by waveOutPrepareHeader and must
# survive, otherwise waveOutWrite fails with WAVERR_UNPREPARED.
_WHDR_DONE = 0x00000001
_WHDR_PREPARED = 0x00000002
_DEFAULT_BUFFER_BYTES = 200 * BYTES_PER_MS  # 200 ms, matching VoiceStudio's chunk
_DEFAULT_BUFFER_COUNT = 3


class _PCMBufferState(str, Enum):
    IDLE = "IDLE"
    SUBMITTED = "SUBMITTED"
    RETURNED = "RETURNED"


class PlaybackGain:
    """播放增益的会话级共享状态（读写两侧分离）。

    打断通道（写侧）把当前音量级别放进来，播放后端（读侧）在**提交给设备之前**缩放
    PCM16。默认值 1.0 表示"完全不动音频"；任何读取异常都退回 1.0，绝不让音量控制
    影响播放可用性。

    如实说明粒度：增益作用于**已提交的那一块缓冲**，因此降音量的生效时间受后端缓冲块
    大小约束（本项目默认 200 ms）。这不是"≤100 ms 内降音量"的证据。
    """

    def __init__(self, initial: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._value = _clamp_gain(initial)

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    def read(self) -> float:
        """后端读侧：绝不抛错，异常一律退回 1.0。"""

        try:
            with self._lock:
                return self._value
        except Exception:  # noqa: BLE001 - 音量控制不得影响播放
            return 1.0

    def set(self, level: float) -> float:
        """打断通道写侧：把级别钳到 [0,1] 并返回生效值。"""

        value = _clamp_gain(level)
        with self._lock:
            self._value = value
        return value


def _clamp_gain(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    result = float(value)
    if not math.isfinite(result):
        return 1.0
    return max(0.0, min(1.0, result))


def _scale_pcm16(pcm: bytes, gain: float) -> bytes:
    """按 ``gain`` 缩放 PCM16LE；``gain >= 1`` 时原样返回（字节级不变）。"""

    if gain >= 1.0:
        return pcm
    values = array.array("h")
    values.frombytes(pcm)
    for index, value in enumerate(values):
        values[index] = int(value * gain)
    return values.tobytes()


@dataclass(frozen=True, slots=True)
class PCMWaveOutDiagnostic:
    """Read-only WinMM lifecycle facts for the streaming PCM backend.

    These fields describe calls and callbacks observed by this backend only.
    They never assert speaker silence or user audibility.
    """

    open_result: int | None = None
    prepare_results: tuple[int, ...] = ()
    write_results: tuple[int, ...] = ()
    write_count: int = 0
    reset_result: int | None = None
    unprepare_results: tuple[int, ...] = ()
    close_result: int | None = None
    opened_at: float | None = None
    prepared_at: float | None = None
    last_write_at: float | None = None
    reset_at: float | None = None
    last_done_at: float | None = None
    unprepared_at: float | None = None
    closed_at: float | None = None
    header_addresses: tuple[int, ...] = ()
    submitted_count: int = 0
    returned_count: int = 0
    cleanup_verified: bool = False
    scaled_submissions: int = 0
    """按 <1 的增益缩放过的提交次数（降音量真实作用的证据）。"""
    min_gain_applied: float = 1.0
    """观察到的最小增益（1.0 表示从未降过音量）。"""


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class _WaveHeader(ctypes.Structure):
    _fields_ = [("lpData", ctypes.POINTER(ctypes.c_char)), ("dwBufferLength", ctypes.c_uint),
                ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


@dataclass(slots=True)
class _PCMSlot:
    buffer: Any
    header: Any
    state: _PCMBufferState = _PCMBufferState.IDLE
    submitted_bytes: int = 0


# An uncertain device may still own the WAVEHDR and its callback. Retain the
# backend (and therefore the buffers) until process exit rather than risk a
# use-after-free.
_uncertain_pcm_backends: list["WaveOutPCMStreamBackend"] = []


class WaveOutPCMStreamBackend:
    """Reusable-buffer streaming PCM backend over ``winmm``."""

    backend_name = "windows_waveout_pcm"

    def __init__(
        self,
        *,
        buffer_bytes: int = _DEFAULT_BUFFER_BYTES,
        buffer_count: int = _DEFAULT_BUFFER_COUNT,
        winmm: Any | None = None,
        clock: Callable[[], float] = monotonic_seconds,
        submit_observer: Callable[[bytes, float], None] | None = None,
        gain_provider: Callable[[], float] | None = None,
    ) -> None:
        if type(buffer_bytes) is not int or buffer_bytes < 2 or buffer_bytes % 2:
            raise ValueError("buffer_bytes_must_be_positive_even")
        if type(buffer_count) is not int or not 2 <= buffer_count <= 8:
            raise ValueError("buffer_count_out_of_range")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if submit_observer is not None and not callable(submit_observer):
            raise TypeError("submit_observer 必须可调用")
        if gain_provider is not None and not callable(gain_provider):
            raise TypeError("gain_provider 必须可调用")
        if winmm is None:
            if sys.platform != "win32":
                raise RuntimeError("windows_output_required")
            winmm = ctypes.WinDLL("winmm")
            _configure_winmm(winmm)
        self.max_buffer_bytes = buffer_bytes
        self._buffer_count = buffer_count
        self._winmm = winmm
        self._clock = clock
        # 只读旁路：仅供 AEC 取"实际提交给设备的 PCM"作参考；绝不参与播放决策，
        # 异常也不会冒泡到提交路径（见 _notify_submit_observer）。
        self._submit_observer = submit_observer
        # 可选的播放增益（打断降音量用）：默认 None → 提交字节级不变。
        self._gain_provider = gain_provider
        self._condition = threading.Condition()
        self._handle = ctypes.c_void_p()
        self._slots: list[_PCMSlot] = []
        self._callback = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(self._on_message)
        self._diagnostic = PCMWaveOutDiagnostic()
        self._opened = False
        self._closed = False
        self._uncertain = False

    # ---------------------------------------------------------------- properties

    @property
    def diagnostics(self) -> PCMWaveOutDiagnostic:
        return self._diagnostic

    @property
    def diagnostic(self) -> PCMWaveOutDiagnostic:
        return self._diagnostic

    @property
    def uncertain(self) -> bool:
        with self._condition:
            return self._uncertain

    # --------------------------------------------------------------- device seam

    def open(self) -> None:
        with self._condition:
            if self._closed:
                raise PCMPlaybackError("backend_closed")
            if self._opened:
                raise PCMPlaybackError("backend_already_open")
            if self._uncertain:
                raise PCMPlaybackError("backend_uncertain")
            fmt = _WaveFormatEx(_WAVE_FORMAT_PCM, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, 0)
            result = self._winmm.waveOutOpen(
                ctypes.byref(self._handle), ctypes.c_uint(_WAVE_MAPPER), ctypes.byref(fmt),
                ctypes.cast(self._callback, ctypes.c_void_p), 0, _CALLBACK_FUNCTION,
            )
            self._record(open_result=result, opened_at=self._clock())
            if result != _MMSYSERR_NOERROR:
                self._uncertain = True
                raise OSError(f"waveOutOpen:{result}")
            prepare_results: list[int] = []
            for _ in range(self._buffer_count):
                buffer = ctypes.create_string_buffer(self.max_buffer_bytes)
                header = _WaveHeader(
                    ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)), 0, 0, None, 0, 0, None, None
                )
                result = self._winmm.waveOutPrepareHeader(
                    self._handle, ctypes.byref(header), ctypes.sizeof(header)
                )
                prepare_results.append(result)
                if result != _MMSYSERR_NOERROR:
                    self._uncertain = True
                    self._record(prepare_results=tuple(prepare_results), prepared_at=self._clock())
                    raise OSError(f"waveOutPrepareHeader:{result}")
                self._slots.append(_PCMSlot(buffer=buffer, header=header))
            self._opened = True
            self._record(
                prepare_results=tuple(prepare_results), prepared_at=self._clock(),
                header_addresses=tuple(ctypes.addressof(slot.header) for slot in self._slots),
            )

    def writable(self) -> bool:
        with self._condition:
            return any(slot.state is not _PCMBufferState.SUBMITTED for slot in self._slots)

    def submit(self, pcm: bytes) -> None:
        if type(pcm) is not bytes or not pcm:
            raise PCMPlaybackError("pcm_must_be_non_empty_bytes")
        if len(pcm) % 2:
            raise PCMPlaybackError("pcm_must_be_even_bytes")
        if len(pcm) > self.max_buffer_bytes:
            raise PCMPlaybackError("pcm_exceeds_device_buffer")
        gain = self._current_gain()
        payload = pcm if gain >= 1.0 else _scale_pcm16(pcm, gain)
        with self._condition:
            if self._uncertain:
                raise PCMPlaybackError("backend_uncertain")
            if not self._opened:
                raise PCMPlaybackError("backend_not_open")
            slot = next(
                (item for item in self._slots if item.state is not _PCMBufferState.SUBMITTED), None
            )
            if slot is None:
                raise PCMBufferBusy("no_free_pcm_buffer")
            ctypes.memmove(slot.buffer, payload, len(payload))
            slot.header.dwBufferLength = len(pcm)
            # Clear only WHDR_DONE so a returned header can be submitted again;
            # WHDR_PREPARED must be preserved for the driver.
            slot.header.dwFlags = slot.header.dwFlags & ~_WHDR_DONE
            if not slot.header.dwFlags & _WHDR_PREPARED:
                self._uncertain = True
                raise PCMPlaybackError("waveout_header_not_prepared")
            slot.submitted_bytes = len(pcm)
            result = self._winmm.waveOutWrite(
                self._handle, ctypes.byref(slot.header), ctypes.sizeof(slot.header)
            )
            self._record(
                write_results=self._diagnostic.write_results + (result,),
                write_count=self._diagnostic.write_count + 1,
                last_write_at=self._clock(),
                submitted_count=self._diagnostic.submitted_count + 1,
                scaled_submissions=self._diagnostic.scaled_submissions + (0 if gain >= 1.0 else 1),
                min_gain_applied=min(self._diagnostic.min_gain_applied, gain),
            )
            if result != _MMSYSERR_NOERROR:
                self._uncertain = True
                raise OSError(f"waveOutWrite:{result}")
            slot.state = _PCMBufferState.SUBMITTED
        if self._submit_observer is not None:
            # 锁外调用，避免观察者回调再入提交路径；时间戳取本次提交时刻。
            # 参考取**实际提交给设备的 PCM**（已含增益），这才是设备真正会放出的信号。
            self._notify_submit_observer(payload)

    def _current_gain(self) -> float:
        provider = self._gain_provider
        if provider is None:
            return 1.0
        try:
            return _clamp_gain(provider())
        except Exception:  # noqa: BLE001 - 音量控制不得影响播放可用性
            return 1.0

    def _notify_submit_observer(self, pcm: bytes) -> None:
        observer = self._submit_observer
        if observer is None:
            return
        try:
            submitted_at = float(self._clock())
        except Exception:  # noqa: BLE001 - 旁路不得影响播放
            return
        try:
            observer(bytes(pcm), submitted_at)
        except Exception:  # noqa: BLE001 - 旁路不得影响播放
            return

    def poll_drained(self) -> bool:
        with self._condition:
            return not any(slot.state is _PCMBufferState.SUBMITTED for slot in self._slots)

    def stop(self, timeout: float) -> StopResult:
        with self._condition:
            if not self._opened:
                return StopResult(False, "unknown", "waveOut_handle_missing")
            handle = self._handle
        # Called outside the condition: waveOutReset synchronously returns the
        # buffers and the WOM_DONE callback needs this lock.
        result = self._winmm.waveOutReset(handle)
        with self._condition:
            self._record(reset_result=result, reset_at=self._clock())
            if result != _MMSYSERR_NOERROR:
                self._uncertain = True
                return StopResult(False, "unknown", f"waveOutReset:{result}")
            deadline = self._clock() + max(0.0, float(timeout))
            while any(slot.state is _PCMBufferState.SUBMITTED for slot in self._slots):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._uncertain = True
                    return StopResult(False, "timeout", "waveOut_buffer_return_timeout")
                self._condition.wait(timeout=min(remaining, 0.05))
            return StopResult(True, "backend_buffer_returned", "waveOutReset")

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            if not self._opened:
                return
            if self._uncertain or any(slot.state is _PCMBufferState.SUBMITTED for slot in self._slots):
                self._uncertain = True
                _uncertain_pcm_backends.append(self)
                return
            unprepare_results: list[int] = []
            for slot in self._slots:
                unprepare_results.append(
                    self._winmm.waveOutUnprepareHeader(
                        self._handle, ctypes.byref(slot.header), ctypes.sizeof(slot.header)
                    )
                )
            self._record(unprepare_results=tuple(unprepare_results), unprepared_at=self._clock())
            if any(result != _MMSYSERR_NOERROR for result in unprepare_results):
                self._uncertain = True
                _uncertain_pcm_backends.append(self)
                return
            result = self._winmm.waveOutClose(self._handle)
            self._record(close_result=result, closed_at=self._clock())
            if result != _MMSYSERR_NOERROR:
                self._uncertain = True
                _uncertain_pcm_backends.append(self)
                return
            self._opened = False
            self._handle = ctypes.c_void_p()
            self._record(cleanup_verified=True)

    def _on_message(self, _handle: object, message: int, _instance: object,
                    param1: object, _param2: object) -> None:
        if message != _WOM_DONE:
            return
        with self._condition:
            for slot in self._slots:
                if (ctypes.addressof(slot.header) == param1
                        and slot.state is _PCMBufferState.SUBMITTED):
                    slot.state = _PCMBufferState.RETURNED
                    self._record(
                        last_done_at=self._clock(),
                        returned_count=sum(
                            1 for item in self._slots if item.state is _PCMBufferState.RETURNED
                        ),
                    )
                    self._condition.notify_all()
                    return

    def _record(self, **changes: object) -> None:
        self._diagnostic = replace(self._diagnostic, **changes)


def _configure_winmm(winmm: Any) -> None:
    winmm.waveOutOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.POINTER(_WaveFormatEx),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ]
    winmm.waveOutPrepareHeader.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint
    ]
    winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint]
    winmm.waveOutReset.argtypes = [ctypes.c_void_p]
    winmm.waveOutUnprepareHeader.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_WaveHeader), ctypes.c_uint
    ]
    winmm.waveOutClose.argtypes = [ctypes.c_void_p]
    for name in ("waveOutOpen", "waveOutPrepareHeader", "waveOutWrite", "waveOutReset",
                 "waveOutUnprepareHeader", "waveOutClose"):
        getattr(winmm, name).restype = ctypes.c_uint


__all__ = ["PCMWaveOutDiagnostic", "PlaybackGain", "WaveOutPCMStreamBackend"]
