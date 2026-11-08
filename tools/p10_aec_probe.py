"""P10 批次 R2b-2：AEC 真机实测工具（需显式 ``--live-opt-in``）。

用途（全部**只在内存中**处理音频，不落盘任何 wav/pcm）：

1. **设备前置检查**（不带 ``--live-opt-in`` 或 ``--check-only``）：列出 WASAPI 输出回环端点、
   麦克风端点、采样率与延迟，不打开任何流；确认 `pyaudiowpatch` 与 waveOut 播放后端可用。
2. **真机测量**：用与生产同一条 waveOut 24 kHz 单声道 PCM16 路径播出一段合成参考信号，
   同时录制 **WASAPI 回环**（= 实际送往扬声器的 PCM，即 AEC 参考）与**麦克风**，然后：
   - 互相关定位参考在回环/麦克风中的偏移，估计"输出→麦克风"回声延迟；
   - 估计未消除回声量级（ERL）；用 R2b-1 的逐样本 NLMS 实测收敛后 ERLE；
   - 报告 `applied` 比例与是否触发 `erle_below_gate` 回退。

安全与隐私纪律：
- 必须显式 ``--live-opt-in`` 才会打开设备；不加只做前置检查；
- 音频只在内存，产物只有 JSON 摘要（数值/设备名/时间戳），**不含音频、不含文本**；
- 不联网、不读密钥、不调用 Agent/计划/授权链。
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio.aec.nlms import (  # noqa: E402
    AecProcessingChain,
    NlmsConfig,
    float_to_pcm16,
    pcm16_to_float,
)
from app.audio.aec.reference import AecReferenceConfig  # noqa: E402

DEFAULT_SECONDS = 6.0
DEFAULT_TAIL_SILENCE_SECONDS = 0.8
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "implementation_plan" / "checkpoints" / "artifacts"
PLAYBACK_RATE = 24_000  # waveOut 播放后端固定 24 kHz 单声道 PCM16
CANCEL_RATE = 16_000
FRAME_MS = 20


# --------------------------------------------------------------------------- 参考信号


def build_reference_signal(
    *,
    seconds: float,
    rate: int = PLAYBACK_RATE,
    seed: int = 20261002,
    burst_seconds: float = 0.6,
    gap_seconds: float = 0.15,
) -> np.ndarray:
    """合成参考信号：**多段带限噪声突发**（每段 0.6 s、间隔 0.15 s、逐段不同增益）。

    为什么不是一段长信号（实测教训）：
    - 扫频（chirp）+ 4 Hz 包络的自相关呈宽带多峰，真机会挑到伪峰；
    - 即使换成带限噪声，**播放与回环采集是两条独立时钟**：实测漂移 1.0005 时，
      6 s 长参考只累积 0.5 ms 相位差，对齐分就从 1.0 掉到 0.03（真机实测 0.0248）。
    因此参考被切成 ~0.6 s 的段：单段内漂移 < 0.05 ms，相关峰稳定；逐段不同增益让
    "第一段"唯一可辨，也便于按段统计回声/噪声。
    """

    rng = np.random.default_rng(seed)
    total = int(seconds * rate)
    burst = max(int(burst_seconds * rate), int(0.2 * rate))
    gap = max(int(gap_seconds * rate), 0)
    gains = (1.00, 1.08, 0.92, 1.12, 0.88, 1.04, 0.96, 1.10)
    signal = np.zeros(total, dtype=np.float64)
    position = 0
    index = 0
    while position + burst <= total:
        noise = rng.standard_normal(burst)
        spectrum = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(burst, d=1.0 / rate)
        spectrum[(freqs < 300.0) | (freqs > 3400.0)] = 0.0
        shaped = np.fft.irfft(spectrum, burst)
        peak = float(np.max(np.abs(shaped))) or 1.0
        shaped = shaped / peak * gains[index % len(gains)]
        # 段首尾各 5 ms 淡入淡出，避免爆音式突变。
        ramp = int(0.005 * rate)
        if ramp > 1:
            shaped[:ramp] *= np.linspace(0.0, 1.0, ramp)
            shaped[-ramp:] *= np.linspace(1.0, 0.0, ramp)
        signal[position : position + burst] = shaped
        position += burst + gap
        index += 1
    peak = float(np.max(np.abs(signal))) or 1.0
    return (signal / peak * 0.6).astype(np.float32)


# --------------------------------------------------------------------------- 设备枚举


def _wasapi_host_index(audio: Any) -> int | None:
    for index in range(audio.get_host_api_count()):
        api = audio.get_host_api_info_by_index(index)
        if "WASAPI" in str(api.get("name", "")):
            return index
    return None


def _enumerate_wasapi(audio: Any) -> dict[str, list[dict[str, Any]]]:
    host_index = _wasapi_host_index(audio)
    result: dict[str, list[dict[str, Any]]] = {"outputs": [], "inputs": [], "loopbacks": []}
    if host_index is None:
        return result
    for index in range(audio.get_device_count()):
        device = audio.get_device_info_by_index(index)
        if int(device.get("hostApi", -1)) != host_index:
            continue
        entry = {
            "index": index,
            "name": str(device.get("name", "")),
            "rate": int(device.get("defaultSampleRate", 0)),
            "in": int(device.get("maxInputChannels", 0)),
            "out": int(device.get("maxOutputChannels", 0)),
            "loopback": bool(device.get("isLoopbackDevice", False)),
        }
        if entry["loopback"]:
            result["loopbacks"].append(entry)
        if entry["in"] > 0:
            result["inputs"].append(entry)
        if entry["out"] > 0 and not entry["loopback"]:
            result["outputs"].append(entry)
    return result


def _pick_loopback(loopbacks: list[dict[str, Any]], wanted: int | None) -> dict[str, Any] | None:
    if wanted is not None:
        return next((entry for entry in loopbacks if entry["index"] == wanted), None)
    return loopbacks[0] if loopbacks else None


def _pick_input(inputs: list[dict[str, Any]], wanted: int | None) -> dict[str, Any] | None:
    """选麦克风端点；**排除回环端点**（loopback 也带输入通道，误选会把参考当麦克风）。"""

    if wanted is not None:
        return next(
            (entry for entry in inputs if entry["index"] == wanted and not entry["loopback"]),
            None,
        )
    candidates = [entry for entry in inputs if not entry["loopback"]]
    for entry in candidates:
        name = entry["name"]
        if "麦克风" in name or "Microphone" in name or "Mic" in name:
            return entry
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------- 录制


class _Recorder:
    """一路 WASAPI 16-bit mono 录制到内存（永不落盘）。"""

    def __init__(self, audio: Any, device: dict[str, Any], *, chunk_seconds: float = 0.02) -> None:
        self._audio = audio
        self._device = device
        self._rate = int(device["rate"])
        self._chunk_frames = max(160, int(self._rate * chunk_seconds))
        self._stream: Any | None = None
        self._chunks: list[bytes] = []
        self._error: Exception | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def started_at(self) -> float:
        return self._started_at

    def start(self) -> None:
        try:
            self._stream = self._audio.open(
                format=self._audio.get_format_from_width(2),
                channels=1,
                rate=self._rate,
                input=True,
                input_device_index=int(self._device["index"]),
                frames_per_buffer=self._chunk_frames,
            )
        except Exception as exc:  # noqa: BLE001 - 设备错误必须如实上报
            raise RuntimeError(f"open_failed:{type(exc).__name__}:{exc}") from exc
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="p10-probe-rec", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._stream is not None
        while not self._stop.is_set():
            try:
                self._chunks.append(self._stream.read(self._chunk_frames, exception_on_overflow=False))
            except Exception as exc:  # noqa: BLE001
                self._error = exc
                break

    def stop(self) -> np.ndarray:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:  # noqa: BLE001 - 关闭失败不掩盖主结果
                pass
        raw = b"".join(self._chunks)
        return pcm16_to_float(raw) if raw else np.zeros(0, dtype=np.float32)


# --------------------------------------------------------------------------- 信号分析


def _resample(signal: np.ndarray, *, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or signal.size == 0:
        return signal.astype(np.float32, copy=False)
    count = max(int(round(signal.size * dst_rate / src_rate)), 1)
    positions = np.linspace(0.0, signal.size - 1, num=count)
    return np.interp(positions, np.arange(signal.size), signal).astype(np.float32)


def _normalized_correlation_score(reference: np.ndarray, segment: np.ndarray) -> float:
    """归一到 [0,1] 的对齐分数：1 表示形状完全一致（去均值后算余弦相似度）。"""

    ref = reference.astype(np.float64)
    seg = segment.astype(np.float64)
    ref = ref - ref.mean()
    seg = seg - seg.mean()
    denom = float(np.sqrt(np.sum(ref * ref)) * np.sqrt(np.sum(seg * seg)))
    if denom <= 0.0:
        return 0.0
    return float(np.dot(ref, seg)) / denom


def _fft_cross_correlation(reference: np.ndarray, window: np.ndarray, *, max_lag: int) -> np.ndarray:
    """FFT 互相关：返回 ``corr[k]`` = window 相对 reference 偏移 k 个样本的匹配度。

    直接 ``np.correlate`` 在 6 s × 48 kHz 上是 O(N·M)，实测跑不完；FFT 版本是
    O(N log N)，并且只保留前 ``max_lag + 1`` 个 lag。
    """

    ref = np.asarray(reference, dtype=np.float64)
    win = np.asarray(window, dtype=np.float64)
    ref_centered = ref - ref.mean()
    win_centered = win - win.mean()
    size = 1 << int(math.ceil(math.log2(ref_centered.size + win_centered.size)))
    spectrum = np.fft.rfft(win_centered, size) * np.conj(np.fft.rfft(ref_centered, size))
    correlation = np.fft.irfft(spectrum, size)[: max_lag + 1]
    return correlation


def _find_offset(
    reference: np.ndarray, recorded: np.ndarray, *, search_seconds: float, rate: int
) -> tuple[int, float]:
    """在 recorded 中定位 reference 起始偏移（FFT 互相关），返回 (offset, 归一化分数)。"""

    if reference.size == 0 or recorded.size <= reference.size:
        return 0, 0.0
    max_lag = min(int(search_seconds * rate), recorded.size - reference.size)
    window = recorded[: reference.size + max_lag]
    correlation = _fft_cross_correlation(reference, window, max_lag=max_lag)
    offset = int(np.argmax(np.abs(correlation)))
    segment = recorded[offset : offset + reference.size]
    if segment.size < reference.size:
        return offset, 0.0
    return offset, _normalized_correlation_score(reference, segment)


def _align(
    reference: np.ndarray, recorded: np.ndarray, *, search_seconds: float, rate: int
) -> tuple[np.ndarray | None, int, float]:
    offset, score = _find_offset(reference, recorded, search_seconds=search_seconds, rate=rate)
    segment = recorded[offset : offset + reference.size]
    if segment.size < reference.size:
        return None, offset, score
    return segment, offset, score


def _cross_correlation_delay(reference: np.ndarray, target: np.ndarray, *, max_lag: int) -> tuple[int, float]:
    """估计 target 相对 reference 的样本延迟（target 落后 reference 为正）。

    用**归一化**互相关（每个 lag 都用能量归一），避免长信号直接点积造成的偏置。
    """

    size = int(min(reference.size, target.size))
    if size <= 0 or max_lag <= 0:
        return 0, 0.0
    max_lag = min(max_lag, size // 2)
    ref = reference[:size].astype(np.float64)
    tgt = target[:size].astype(np.float64)
    correlation = _fft_cross_correlation(ref, tgt, max_lag=max_lag)
    # 归一化：用滑动的累积能量（前缀和）近似每个 lag 的窗口能量。
    prefix_ref = np.concatenate(([0.0], np.cumsum(ref * ref)))
    prefix_tgt = np.concatenate(([0.0], np.cumsum(tgt * tgt)))
    best_lag = 0
    best_score = 0.0
    for lag in range(0, max_lag + 1):
        span = size - lag
        ref_energy = prefix_ref[span] - prefix_ref[lag]
        tgt_energy = prefix_tgt[span]
        denom = math.sqrt(max(ref_energy, 0.0) * max(tgt_energy, 0.0))
        if denom <= 0.0:
            continue
        score = float(correlation[lag]) / denom
        if score > best_score:
            best_score = score
            best_lag = lag
    return best_lag, max(best_score, 0.0)


def _energy(samples: np.ndarray) -> float:
    return float(np.dot(np.asarray(samples, dtype=np.float64), np.asarray(samples, dtype=np.float64)))


def _rms(samples: np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float64)
    if array.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(array * array)))


def _band_ratio(samples: np.ndarray, *, rate: int, low: float = 300.0, high: float = 3400.0) -> float:
    """300~3400 Hz 频带能量占比（用于判断麦克风里是回声还是别的噪声）。"""

    array = np.asarray(samples, dtype=np.float64)
    if array.size < 256:
        return 0.0
    array = array - array.mean()
    spectrum = np.abs(np.fft.rfft(array)) ** 2
    freqs = np.fft.rfftfreq(array.size, d=1.0 / rate)
    band = float(np.sum(spectrum[(freqs >= low) & (freqs <= high)]))
    total = float(np.sum(spectrum))
    return round(band / total, 4) if total > 0 else 0.0


def _zero_crossing_rate(samples: np.ndarray, *, rate: int) -> float:
    array = np.asarray(samples, dtype=np.float64)
    if array.size < 2:
        return 0.0
    crossings = int(np.sum(np.diff(np.signbit(array))))
    return round(crossings * rate / array.size, 2)


def _db_ratio(numerator: float, denominator: float) -> float | None:
    if numerator <= 0.0 or denominator <= 0.0:
        return None
    return round(10.0 * math.log10(numerator / denominator), 3)


def _analysis_segment(reference: np.ndarray, *, rate: int, burst_seconds: float = 0.6) -> np.ndarray:
    """取第一段突发作为对齐模板（段内漂移 < 0.05 ms，相关峰稳定）。"""

    size = int(burst_seconds * rate)
    head = reference[:size].astype(np.float32)
    # 丢掉淡入段的一部分，避免模板本身带上升沿失真。
    skip = int(0.01 * rate)
    return head[skip:] if head.size > skip * 2 else head


def analyze(
    *,
    reference: np.ndarray,
    loopback: np.ndarray,
    mic: np.ndarray,
    rate: int,
    chain: AecProcessingChain,
    window_seconds: float = 1.0,
) -> dict[str, Any]:
    """对齐参考/回环/麦克风三路，给出延迟、ERL 与实测 ERLE。

    对齐策略（实测教训）：参考是**多段 0.6 s 突发**，用一个 0.6 s 模板定位播放起点，
    再取固定长度窗口做后续分析——避免"长参考 + 双时钟漂移"把相关性抹平。
    """

    template = _analysis_segment(reference, rate=rate)
    result: dict[str, Any] = {
        "rate": rate,
        "reference_seconds": round(reference.size / rate, 3),
        "loopback_seconds": round(loopback.size / rate, 3),
        "mic_seconds": round(mic.size / rate, 3),
        "template_seconds": round(template.size / rate, 3),
    }
    loop_start, loop_score = _find_offset(template, loopback, search_seconds=3.0, rate=rate)
    mic_start, mic_score = _find_offset(template, mic, search_seconds=5.0, rate=rate)
    result["loopback_offset_samples"] = loop_start
    result["loopback_alignment"] = round(loop_score, 4)
    result["mic_offset_samples"] = mic_start
    result["mic_alignment"] = round(mic_score, 4)
    if loop_score <= 0.0 or mic_score <= 0.0:
        result["status"] = "alignment_failed"
        result["reason"] = "template_not_found_in_loopback_or_mic"
        return result

    skip = int(0.2 * rate)  # 越过段内淡入与设备启动抖动
    span = int(round(window_seconds * rate))
    loop_seg = loopback[loop_start + skip : loop_start + skip + span]
    mic_seg = mic[mic_start + skip : mic_start + skip + span]
    if loop_seg.size < span // 2 or mic_seg.size < span // 2:
        result["status"] = "too_short"
        result["reason"] = "analysis_window_incomplete"
        return result
    usable = int(min(loop_seg.size, mic_seg.size))
    loop_seg = loop_seg[:usable]
    mic_seg = mic_seg[:usable]

    # 两路窗口已各自按"播放起点"对齐：此时只需要补偿**残余声学延迟**（几十 ms 量级），
    # 不能拿 offset_delta（= 两条流的启动偏斜 + 声学延迟，可达数百 ms）去推麦克风，
    # 否则会把已对齐的信号推歪（测试实测 ERLE 因此掉到 ≈0）。
    offset_delta = int(mic_start - loop_start)
    mic_shift = 0
    loop_gate = loop_seg[: int(0.3 * rate)]
    search = int(0.12 * rate)
    if loop_gate.size > 0 and mic_seg.size > loop_gate.size + search:
        best_lag = 0
        best_score = -1.0
        for lag in range(0, search, 8):
            head = mic_seg[lag : lag + loop_gate.size]
            if head.size < loop_gate.size:
                break
            denom = float(np.sqrt(_energy(head)) * math.sqrt(_energy(loop_gate)))
            if denom <= 0.0:
                continue
            score = float(np.dot(loop_gate.astype(np.float64), head.astype(np.float64))) / denom
            if score > best_score:
                best_score = score
                best_lag = lag
        mic_shift = best_lag
        result["acoustic_delay_samples"] = int(best_lag)
        result["acoustic_delay_ms"] = round(best_lag * 1000.0 / rate, 2)
        result["acoustic_delay_score"] = round(max(best_score, 0.0), 4)
        mic_seg = mic_seg[mic_shift:]

    result["offset_delta_samples"] = offset_delta
    result["echo_delay_note"] = "offset_delta = inter_stream_start_skew + acoustic_delay"
    result["loop_seg_band_ratio"] = _band_ratio(loop_seg, rate=rate)
    result["loop_seg_zero_crossing_rate"] = _zero_crossing_rate(loop_seg, rate=rate)
    result["mic_seg_band_ratio"] = _band_ratio(mic_seg, rate=rate)
    result["mic_seg_zero_crossing_rate"] = _zero_crossing_rate(mic_seg, rate=rate)

    loop16 = _resample(loop_seg, src_rate=rate, dst_rate=CANCEL_RATE)
    mic16 = _resample(mic_seg, src_rate=rate, dst_rate=CANCEL_RATE)
    frame = CANCEL_RATE * FRAME_MS // 1000
    usable = int(min(loop16.size, mic16.size))
    if usable < frame * 5:
        result["status"] = "too_short"
        result["reason"] = "usable_window_below_5_frames"
        return result
    loop16 = loop16[:usable]
    mic16 = mic16[:usable]

    # 噪声底基线：播放开始前的静音段（留 100 ms 余量），重采样到 16 kHz。
    baseline_len = int(0.6 * rate)
    lead_in = max(0, min(loop_start, mic_start) - int(0.1 * rate))
    mic_floor = mic[lead_in - baseline_len : lead_in] if lead_in > baseline_len else None

    reference_energy = _energy(loop16)
    mic_energy = _energy(mic16)
    result["loopback_rms"] = round(math.sqrt(reference_energy / max(loop16.size, 1)), 6)
    result["mic_rms"] = round(math.sqrt(mic_energy / max(mic16.size, 1)), 6)
    result["erl_db"] = _db_ratio(mic_energy, reference_energy)

    # 噪声底（重采样到 16 kHz）：麦克风里到底有多少是"回声之外的噪声"。
    floor16 = _resample(mic_floor, src_rate=rate, dst_rate=CANCEL_RATE) if mic_floor is not None else None
    noise_energy_total = 0.0
    if floor16 is not None and floor16.size >= frame:
        noise_rms = _rms(floor16)
        noise_energy_total = float(noise_rms * noise_rms) * loop16.size
        result["mic_noise_floor_rms_16k"] = round(noise_rms, 6)
        result["enr_db"] = _db_ratio(mic_energy, noise_energy_total)
        result["max_erle_db"] = _db_ratio(mic_energy, noise_energy_total)

    frame = CANCEL_RATE * FRAME_MS // 1000
    frames = int(loop16.size // frame)
    if frames < 5:
        result["status"] = "too_short"
        result["reason"] = "frames_below_5"
        return result

    outputs: list[np.ndarray] = []
    applied = 0
    for index in range(frames):
        start = index * frame
        outcome = chain.canceller.cancel(
            reference=float_to_pcm16(loop16[start : start + frame]),
            capture=float_to_pcm16(mic16[start : start + frame]),
        )
        outputs.append(outcome.output)
        if outcome.applied:
            applied += 1
    residual = np.concatenate(outputs) if outputs else np.zeros(0, dtype=np.float32)
    half = residual.size // 2
    tail_residual = residual[half:]
    tail_mic = mic16[half : half + tail_residual.size]
    result["status"] = "ok"
    result["frames"] = frames
    result["applied_ratio"] = round(applied / frames, 4)
    result["erle_tail_db"] = _db_ratio(_energy(tail_mic), _energy(tail_residual))
    if noise_energy_total > 0.0:
        tail_noise = noise_energy_total / max(loop16.size, 1) * max(tail_residual.size, 1)
        result["erle_tail_vs_noise_floor_db"] = _db_ratio(_energy(tail_mic), tail_noise)
    cumulative = chain.canceller.cumulative_erle_db
    result["cumulative_erle_db"] = None if math.isinf(cumulative) else round(cumulative, 3)
    return result


# --------------------------------------------------------------------------- 播放


def _play(signal: np.ndarray, *, backend_factory) -> dict[str, Any]:
    """用生产同款 waveOut 后端播放 24 kHz PCM16；返回播放事实与提交统计。"""

    pcm = float_to_pcm16(signal)
    backend = backend_factory()
    facts: dict[str, Any] = {
        "backend": getattr(backend, "backend_name", "unknown"),
        "bytes": len(pcm),
        "seconds": round(len(pcm) / (PLAYBACK_RATE * 2), 3),
    }
    backend.open()
    submitted_bytes = 0
    submit_calls = 0
    blocked_waits = 0
    started = time.monotonic()
    try:
        chunk_bytes = int(getattr(backend, "max_buffer_bytes", 9600))
        offset = 0
        deadline = time.monotonic() + facts["seconds"] + 5.0
        while offset < len(pcm):
            if not backend.writable():
                blocked_waits += 1
                time.sleep(0.005)
                if time.monotonic() > deadline:
                    raise RuntimeError("waveout_submit_timeout")
                continue
            block = pcm[offset : offset + chunk_bytes]
            backend.submit(block)
            offset += len(block)
            submitted_bytes += len(block)
            submit_calls += 1
        drain_deadline = time.monotonic() + 5.0
        while not backend.poll_drained() and time.monotonic() < drain_deadline:
            time.sleep(0.01)
        facts["drained"] = bool(backend.poll_drained())
    finally:
        facts.update(
            {
                "submit_calls": submit_calls,
                "submitted_bytes": submitted_bytes,
                "blocked_waits": blocked_waits,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            facts["close_error"] = True
    return facts


# --------------------------------------------------------------------------- 主流程


def _save_wav(path: Path, signal: np.ndarray, *, rate: int) -> int:
    """把 float32 信号写成 16-bit 单声道 WAV（仅用于离线复盘的显式请求；默认不写）。"""

    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = float_to_pcm16(signal)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return len(pcm)


TONE_FREQUENCIES = (200.0, 400.0, 700.0, 1000.0, 1400.0, 2000.0, 2800.0, 3600.0, 5000.0)


def build_tone_signal(
    *, seconds: float, rate: int = PLAYBACK_RATE, amplitude: float = 0.25, gap_seconds: float = 0.15
) -> tuple[np.ndarray, list[dict[str, float]]]:
    """每个频点一段正弦（等幅、带淡入淡出），用于量测"播放→回环"的每频点增益。"""

    total = int(seconds * rate)
    signal = np.zeros(total, dtype=np.float64)
    segment = max(int(0.4 * rate), 1)
    gap = max(int(gap_seconds * rate), 0)
    schedule: list[dict[str, float]] = []
    position = 0
    for frequency in TONE_FREQUENCIES:
        if position + segment > total:
            break
        t = np.arange(segment, dtype=np.float64) / rate
        tone = np.sin(2.0 * np.pi * frequency * t) * amplitude
        ramp = max(int(0.01 * rate), 1)
        tone[:ramp] *= np.linspace(0.0, 1.0, ramp)
        tone[-ramp:] *= np.linspace(1.0, 0.0, ramp)
        signal[position : position + segment] = tone
        schedule.append(
            {
                "frequency": frequency,
                "start_seconds": round(position / rate, 4),
                "seconds": round(segment / rate, 4),
            }
        )
        position += segment + gap
    # 末尾补一个间隔，避免最后一段被录制缓冲区尾部截断。
    if position < total:
        signal[position:] = 0.0
    return signal.astype(np.float32), schedule


def analyze_tones(
    *,
    playback: np.ndarray,
    recorded: np.ndarray,
    schedule: list[dict[str, float]],
    rate: int,
    amplitude: float,
) -> dict[str, Any]:
    """量测每频点增益（回环/播放）与是否削顶。"""

    peak = float(np.max(np.abs(recorded))) if recorded.size else 0.0
    clipped = int(np.sum(np.abs(recorded) >= 0.999)) if recorded.size else 0
    reference_rms = amplitude / math.sqrt(2.0)
    tones: list[dict[str, Any]] = []
    for entry in schedule:
        start = int(entry["start_seconds"] * rate)
        length = int(entry["seconds"] * rate)
        # 只取中段：避开 10 ms 淡入淡出与设备缓冲带来的边界失真。
        head = int(0.1 * length)
        tail = int(0.1 * length)
        segment = recorded[start + head : start + length - tail]
        if segment.size < 64:
            continue
        measured_rms = _rms(segment)
        gain_db = (
            None
            if measured_rms <= 0.0 or reference_rms <= 0.0
            else round(20.0 * math.log10(measured_rms / reference_rms), 2)
        )
        tones.append(
            {
                "frequency": float(entry["frequency"]),
                "measured_rms": round(measured_rms, 6),
                "gain_db": gain_db,
                "band_ratio": _band_ratio(segment, rate=rate, low=entry["frequency"] * 0.7, high=entry["frequency"] * 1.4),
            }
        )
    return {
        "kind": "tone_response",
        "rate": rate,
        "reference_amplitude": amplitude,
        "recorded_seconds": round(recorded.size / max(rate, 1), 3),
        "recorded_peak": round(peak, 6),
        "clipped_samples": clipped,
        "tones": tones,
    }


def analyze_same_process(
    *,
    source,
    mic: np.ndarray,
    mic_start_seconds: float,
    rate: int,
    frame_ms: int = FRAME_MS,
) -> dict[str, Any]:
    """方案 A：参考来自"提交给设备的 PCM"旁路，不需要回环对齐。

    参考帧带单调时间戳（提交时刻）；麦克风录制按同一挂钟时间轴切片。
    由于两条时间轴来自同一进程，偏移只有"麦克风流启动时刻"一个未知量，用整帧对齐全扫描。
    """

    frame = int(rate * frame_ms / 1000)
    source_frames: list[tuple[float, np.ndarray]] = []
    for stamp_ms, pcm in source.timeline_frames(limit=0):
        source_frames.append((stamp_ms / 1000.0, pcm))
    if not source_frames:
        return {"status": "no_reference_frames", "reason": "submit_observer_never_fired"}
    reference = np.concatenate([frame for _, frame in source_frames]).astype(np.float32)
    reference_start = source_frames[0][0]
    reference_seconds = reference.size / rate
    result: dict[str, Any] = {
        "status": "ok",
        "rate": rate,
        "reference_seconds": round(reference_seconds, 3),
        "reference_frames": len(source_frames),
        "mic_seconds": round(mic.size / rate, 3),
        "reference_start_seconds": round(reference_start, 4),
        "mic_start_seconds": round(mic_start_seconds, 4),
    }
    if mic.size < frame * 5:
        result["status"] = "mic_too_short"
        return result

    # 用"参考起点的一小段"在麦克风**整段**里找起点：同进程下偏移可能达数秒，
    # 不能只搜前 3 秒（真机踩坑：偏移 ~0.92 s 处只有伪峰）。
    # 同时报告前几名候选与原始相关值，避免"失败但不知道为什么"。
    template_len = min(int(0.6 * rate), reference.size, mic.size)
    template = reference[:template_len]
    max_lag = max(mic.size - template_len, 0)
    correlation = _fft_cross_correlation(template, mic[: template_len + max_lag], max_lag=max_lag)
    order = np.argsort(np.abs(correlation))[::-1][:3]
    best_offset = int(order[0])
    best_score = _normalized_correlation_score(
        template, mic[best_offset : best_offset + template_len]
    )
    result["mic_reference_offset_samples"] = int(best_offset)
    result["mic_alignment"] = round(best_score, 4)
    result["top_offsets"] = [
        {
            "offset_samples": int(index),
            "offset_ms": round(index * 1000.0 / rate, 1),
            "normalized": round(
                _normalized_correlation_score(template, mic[int(index) : int(index) + template_len]),
                4,
            ),
        }
        for index in order
    ]
    # 即使对齐失败也要给出"麦克风是否听到我们的声音"这一关键判读。
    lead = int(0.8 * rate)
    floor_len = int(0.5 * rate)
    if mic.size > lead + floor_len:
        floor = mic[lead : lead + floor_len]
        active = mic[lead + floor_len :]
        floor_rms = _rms(floor)
        active_rms = _rms(active)
        result["mic_floor_rms"] = round(floor_rms, 6)
        result["mic_active_rms"] = round(active_rms, 6)
        result["mic_level_vs_floor_db"] = (
            None
            if floor_rms <= 0.0 or active_rms <= 0.0
            else round(20.0 * math.log10(active_rms / floor_rms), 3)
        )

    if best_score <= 0.0:
        result["status"] = "alignment_failed"
        result["reason"] = "reference_not_found_in_mic"
        result["search_span_samples"] = int(max_lag)
        return result

    skip = int(0.1 * rate)
    usable = int(min(mic.size - best_offset - skip, reference.size))
    if usable < frame * 5:
        result["status"] = "too_short"
        result["reason"] = "usable_window_below_5_frames"
        return result
    # best_offset 就是"需要补偿的延迟"：麦克风从 best_offset 起、参考从 0 起，
    # 两段随后逐帧对齐（不要再把 best_offset 当作参考内部偏移）。
    seg_len = usable - usable % frame
    mic_seg = mic[best_offset + skip : best_offset + skip + seg_len]
    ref_seg = reference[skip : skip + seg_len]
    seg_len = min(mic_seg.size, ref_seg.size)
    seg_len -= seg_len % frame
    mic_seg = mic_seg[:seg_len]
    ref_seg = ref_seg[:seg_len]
    result["compensation_samples"] = int(best_offset + skip)
    result["mic_seg_seconds"] = round(mic_seg.size / rate, 3)
    result["mic_seg_band_ratio"] = _band_ratio(mic_seg, rate=rate)
    result["reference_band_ratio"] = _band_ratio(ref_seg, rate=rate)

    reference_energy = _energy(ref_seg)
    mic_energy = _energy(mic_seg)
    result["reference_rms"] = round(math.sqrt(reference_energy / max(ref_seg.size, 1)), 6)
    result["mic_rms"] = round(math.sqrt(mic_energy / max(mic_seg.size, 1)), 6)
    result["erl_db"] = _db_ratio(mic_energy, reference_energy)

    # 关键判读："麦克风有没有听到我们的声音"。
    # 播放前静音段 vs 播放期电平差 → 若几乎无差别，说明麦克风里根本没有我们的信号（无回声可消）。
    baseline_len = int(0.5 * rate)
    if best_offset > baseline_len:
        floor = mic[max(0, best_offset - baseline_len) : best_offset]
        floor_rms = _rms(floor)
        result["mic_floor_rms"] = round(floor_rms, 6)
        result["mic_level_vs_floor_db"] = (
            None
            if floor_rms <= 0.0
            else round(20.0 * math.log10(max(math.sqrt(mic_energy / max(mic_seg.size, 1)), 1e-12) / floor_rms), 3)
        )
        result["mic_floor_band_ratio"] = _band_ratio(floor, rate=rate)

    # 帧数必须按**截齐后**的长度算（`usable` 是参考长度受限前的上界，两者可能不同）。
    frames = seg_len // frame
    outputs: list[np.ndarray] = []
    applied = 0
    for index in range(frames):
        start = index * frame
        outcome = source.canceller.cancel(
            reference=float_to_pcm16(ref_seg[start : start + frame]),
            capture=float_to_pcm16(mic_seg[start : start + frame]),
        )
        outputs.append(outcome.output)
        if outcome.applied:
            applied += 1
    residual = np.concatenate(outputs)
    half = residual.size // 2
    tail_residual = residual[half:]
    tail_mic = mic_seg[half : half + tail_residual.size]
    result["frames"] = frames
    result["applied_ratio"] = round(applied / frames, 4)
    result["erle_tail_db"] = _db_ratio(_energy(tail_mic), _energy(tail_residual))
    cumulative = source.canceller.cumulative_erle_db
    result["cumulative_erle_db"] = None if math.isinf(cumulative) else round(cumulative, 3)
    return result


def _preflight() -> dict[str, Any]:
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": np.__version__,
        "modules": {},
    }
    try:
        import pyaudiowpatch  # type: ignore[import-not-found]

        report["modules"]["pyaudiowpatch"] = getattr(pyaudiowpatch, "__version__", "unknown")
        audio = pyaudiowpatch.PyAudio()
        try:
            report["wasapi"] = _enumerate_wasapi(audio)
        finally:
            audio.terminate()
    except Exception as exc:  # noqa: BLE001
        report["modules"]["pyaudiowpatch"] = f"unavailable:{type(exc).__name__}"
    try:
        from app.tts.waveout_pcm import WaveOutPCMStreamBackend

        report["modules"]["waveout_backend"] = WaveOutPCMStreamBackend.backend_name
    except Exception as exc:  # noqa: BLE001
        report["modules"]["waveout_backend"] = f"unavailable:{type(exc).__name__}"
    return report


def _print_preflight(report: dict[str, Any]) -> None:
    wasapi = report.get("wasapi") or {}
    print(f"[p10-aec] python={report['python']} platform={report['platform']}")
    print(f"[p10-aec] modules={json.dumps(report['modules'], ensure_ascii=False)}")
    print(
        f"[p10-aec] 回环端点 {len(wasapi.get('loopbacks', []))} 个，"
        f"输入端点 {len(wasapi.get('inputs', []))} 个"
    )
    for entry in wasapi.get("loopbacks", [])[:5]:
        print(f"  loopback[{entry['index']}] {entry['name']} rate={entry['rate']}")
    for entry in wasapi.get("inputs", [])[:5]:
        print(f"  input[{entry['index']}] {entry['name']} rate={entry['rate']}")


def _run_same_process(*, args, reference: np.ndarray, mic_device: dict[str, Any]) -> int:
    """方案 A：用"提交给设备的 PCM"作参考，测真机 ERLE（不回环）。"""

    import pyaudiowpatch  # type: ignore[import-not-found]

    from app.audio.aec.nlms import NlmsConfig
    from app.audio.aec.reference_source import AecReferencePCM, SameProcessAecReference
    from app.tts.waveout_pcm import WaveOutPCMStreamBackend

    mic_rate = int(mic_device["rate"])
    # waveOut 后端的原生格式是 24 kHz 单声道 PCM16：参考与时间戳都必须用它这一段时钟。
    device_rate = PLAYBACK_RATE
    source = SameProcessAecReference(
        pcm=AecReferencePCM(
            sample_rate=device_rate,
            frame_duration_ms=FRAME_MS,
            # 容量必须覆盖整段播放，否则最早的参考帧会被挤出缓冲（真机踩坑）。
            max_capacity_ms=int(args.seconds * 1000) + 3000,
        ),
        nlms_config=NlmsConfig(
            sample_rate=device_rate, filter_length_ms=160, erle_gate_db=1.0
        ),
    )
    audio = pyaudiowpatch.PyAudio()
    mic_rec = _Recorder(audio, mic_device)
    mic_rec.start()
    play_started_at = time.monotonic()
    print(
        f"[p10-aec] 方案 A：参考=提交给设备的 PCM 旁路；麦克风={mic_device['name']} "
        f"({mic_rate} Hz)；即将放音。"
    )
    try:
        play_facts = _play(
            _resample(reference, src_rate=PLAYBACK_RATE, dst_rate=device_rate),
            backend_factory=lambda: WaveOutPCMStreamBackend(
                submit_observer=source.as_submit_observer()
            ),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[p10-aec] 播放失败：{type(exc).__name__}:{exc}")
        mic_rec.stop()
        audio.terminate()
        return 1
    time.sleep(DEFAULT_TAIL_SILENCE_SECONDS)
    mic_raw = mic_rec.stop()
    audio.terminate()

    # 麦克风重采样到**设备播放采样率**：参考时间戳与麦克风必须在同一时间轴。
    # 之前把 48 kHz 参考交给 24 kHz 后端，播放时长翻倍、录制只够一半（真机踩坑）。
    mic_at_device_rate = _resample(mic_raw, src_rate=mic_rate, dst_rate=device_rate)

    print(
        f"[p10-aec] 录制完成：麦克风 {mic_raw.size / max(mic_rec.rate, 1):.2f}s；"
        f"旁路收到参考帧 {source.frames_submitted} 帧"
        + (f"（旁路错误 {source.observer_errors} 次：{source.last_observer_error}）"
           if source.observer_errors
           else "")
    )
    analysis = analyze_same_process(
        source=source,
        mic=mic_at_device_rate,
        mic_start_seconds=mic_rec.started_at - play_started_at,
        rate=device_rate,
        frame_ms=FRAME_MS,
    )
    analysis["reference_mode"] = "same_process_pcm"
    analysis["reference_frames_submitted"] = source.frames_submitted

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = Path(args.artifact_root) / "2026-10-02-P10-aec-probe"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "kind": "p10_aec_same_process_probe",
        "created_at": timestamp,
        "preflight": {"python": sys.version.split()[0], "platform": platform.platform()},
        "devices": {"mic": mic_device},
        "reference_source": source.to_details(),
        "playback": play_facts,
        "analysis": analysis,
        "no_audio_persisted": True,
    }
    path = artifact_dir / f"p10-aec-same-process-{timestamp}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print("-" * 72)
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    print(f"[p10-aec] artifact：{path}")
    return 0 if analysis.get("status") == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P10 AEC 真机实测（需 --live-opt-in）")
    parser.add_argument("--live-opt-in", action="store_true", help="显式开启真机（会放音并录音）")
    parser.add_argument("--check-only", action="store_true", help="只做前置检查，不打开设备流")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="参考信号时长（秒）")
    parser.add_argument("--mic-device", type=int, default=None, help="麦克风设备索引（默认自动选）")
    parser.add_argument("--loopback-device", type=int, default=None, help="回环设备索引（默认自动选）")
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT), help="artifact 输出根目录")
    parser.add_argument(
        "--reference",
        choices=("loopback", "same-process"),
        default="loopback",
        help="AEC 参考来源：loopback=WASAPI 回环（本机实测不可用）；same-process=提交给设备的 PCM 旁路",
    )
    parser.add_argument(
        "--loopback-only",
        action="store_true",
        help="只录回环、不开麦克风（用于排查'回环是否真的抓到了播放内容'，更快且不采集环境声）",
    )
    parser.add_argument(
        "--tones",
        action="store_true",
        help="多音测试：逐频点等幅正弦，量测'播放→回环'的每频点增益（用于定位设备效果链）",
    )
    parser.add_argument("--tone-amplitude", type=float, default=0.25, help="多音测试的每音幅度（默认 0.25）")
    parser.add_argument("--tone-seconds", type=float, default=0.35, help="多音测试每音时长（默认 0.35s）")
    parser.add_argument(
        "--save-audio",
        action="store_true",
        help="把参考/回环/麦克风三路信号写成 WAV 供离线复盘（默认不写；写了就是本机音频落盘）",
    )
    args = parser.parse_args(argv)

    if args.seconds <= 0:
        print("[p10-aec] --seconds 必须为正")
        return 2

    print("=" * 72)
    print("[p10-aec] AEC 真机实测；音频只在内存处理，产物只有 JSON 摘要（不含音频/文本）")
    print("=" * 72)

    if not args.live_opt_in or args.check_only:
        print("[p10-aec] 只做前置检查（未提供 --live-opt-in 或指定了 --check-only）。")
        _print_preflight(_preflight())
        if not args.live_opt_in:
            print("[p10-aec] 加 --live-opt-in 才会真正放音与录音（会占用扬声器与麦克风）。")
        return 0

    preflight = _preflight()
    wasapi = preflight.get("wasapi") or {}
    loopback = _pick_loopback(wasapi.get("loopbacks", []), args.loopback_device)
    mic_device = _pick_input(wasapi.get("inputs", []), args.mic_device)
    if loopback is None or mic_device is None:
        print("[p10-aec] 找不到可用的回环端点或麦克风端点，终止。")
        _print_preflight(preflight)
        return 2
    print(f"[p10-aec] 回环参考：{loopback['name']} (index={loopback['index']}, {loopback['rate']} Hz)")
    print(f"[p10-aec] 麦克风　：{mic_device['name']} (index={mic_device['index']}, {mic_device['rate']} Hz)")
    print(f"[p10-aec] 参考信号 {args.seconds:.1f}s；即将放音，请保持安静、不要说话（除非做双讲测试）。")
    for remaining in (3, 2, 1):
        print(f"[p10-aec] {remaining} ...")
        time.sleep(1.0)

    import pyaudiowpatch  # type: ignore[import-not-found]

    from app.tts.waveout_pcm import WaveOutPCMStreamBackend

    reference = build_reference_signal(seconds=args.seconds)
    if args.tones:
        # 总时长必须覆盖"最后一个音 + 间隔 + 余量"，否则末尾频点会被截掉。
        tone_seconds = len(TONE_FREQUENCIES) * (args.tone_seconds + 0.15) + 0.3
        reference, tone_schedule = build_tone_signal(
            seconds=tone_seconds, amplitude=args.tone_amplitude
        )
        print(f"[p10-aec] 多音测试：{len(tone_schedule)} 个频点，共 {reference.size / PLAYBACK_RATE:.2f}s")
    else:
        tone_schedule = []
    if args.reference == "same-process":
        # 方案 A：参考取自"提交给设备的 PCM"旁路，不需要回环，也不需要回环对齐。
        return _run_same_process(args=args, reference=reference, mic_device=mic_device)
    audio = pyaudiowpatch.PyAudio()
    loop_rec = _Recorder(audio, loopback)
    mic_rec = None if args.loopback_only else _Recorder(audio, mic_device)
    loop_rec.start()
    if mic_rec is not None:
        mic_rec.start()
    time.sleep(0.2)
    try:
        play_facts = _play(reference, backend_factory=WaveOutPCMStreamBackend)
    except Exception as exc:  # noqa: BLE001
        print(f"[p10-aec] 播放失败：{type(exc).__name__}:{exc}")
        loop_rec.stop()
        if mic_rec is not None:
            mic_rec.stop()
        audio.terminate()
        return 1
    time.sleep(DEFAULT_TAIL_SILENCE_SECONDS)
    loop_raw = loop_rec.stop()
    mic_raw = mic_rec.stop() if mic_rec is not None else np.zeros(0, dtype=np.float32)
    audio.terminate()

    if loop_rec.error is not None or (mic_rec is not None and mic_rec.error is not None):
        print(
            f"[p10-aec] 录制错误：loop={loop_rec.error} "
            f"mic={None if mic_rec is None else mic_rec.error}"
        )
    print(
        f"[p10-aec] 录制完成：回环 {loop_raw.size / max(loop_rec.rate, 1):.2f}s、"
        f"麦克风 {mic_raw.size / max(mic_rec.rate if mic_rec else loop_rec.rate, 1):.2f}s"
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = Path(args.artifact_root) / "2026-10-02-P10-aec-probe"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    if args.tones:
        summary = analyze_tones(
            playback=_resample(reference, src_rate=PLAYBACK_RATE, dst_rate=loop_rec.rate),
            recorded=loop_raw,
            schedule=tone_schedule,
            rate=loop_rec.rate,
            amplitude=args.tone_amplitude,
        )
        artifact = {
            "kind": "p10_aec_tone_response",
            "created_at": timestamp,
            "devices": {"loopback": loopback},
            "playback": play_facts,
            "summary": summary,
            "no_audio_persisted": not args.save_audio,
        }
        path = artifact_dir / f"p10-aec-tones-{timestamp}.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[p10-aec] artifact：{path}")
        if args.save_audio:
            debug_dir = artifact_dir / f"audio-{timestamp}"
            _save_wav(debug_dir / "loopback.wav", loop_raw, rate=loop_rec.rate)
            _save_wav(
                debug_dir / "reference.wav",
                _resample(reference, src_rate=PLAYBACK_RATE, dst_rate=loop_rec.rate),
                rate=loop_rec.rate,
            )
            print(f"[p10-aec] 已按 --save-audio 写出 WAV：{debug_dir}")
        return 0

    if args.loopback_only:
        template = _analysis_segment(reference, rate=loop_rec.rate)
        offset, score = _find_offset(template, loop_raw, search_seconds=3.0, rate=loop_rec.rate)
        summary = {
            "mode": "loopback_only",
            "rate": loop_rec.rate,
            "loopback_seconds": round(loop_raw.size / loop_rec.rate, 3),
            "template_offset_samples": offset,
            "template_alignment": round(score, 4),
            "loopback_rms": round(_rms(loop_raw), 6),
            "loopback_peak": round(float(np.max(np.abs(loop_raw))) if loop_raw.size else 0.0, 6),
            "band_ratio": _band_ratio(loop_raw, rate=loop_rec.rate),
        }
        artifact = {
            "kind": "p10_aec_loopback_only_probe",
            "created_at": timestamp,
            "devices": {"loopback": loopback},
            "playback": play_facts,
            "summary": summary,
            "no_audio_persisted": not args.save_audio,
        }
        path = artifact_dir / f"p10-aec-loopback-{timestamp}.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"[p10-aec] artifact：{path}")
        print("[p10-aec] 判读：template_alignment 应接近 1.0；若仍接近 0，说明回环抓的不是播放内容。")
        if args.save_audio:
            debug_dir = artifact_dir / f"audio-{timestamp}"
            reference48 = _resample(reference, src_rate=PLAYBACK_RATE, dst_rate=loop_rec.rate)
            _save_wav(debug_dir / "reference.wav", reference48, rate=loop_rec.rate)
            _save_wav(debug_dir / "loopback.wav", loop_raw, rate=loop_rec.rate)
            print(f"[p10-aec] 已按 --save-audio 写出 WAV：{debug_dir}")
        return 0 if score >= 0.3 else 1

    chain = AecProcessingChain(
        reference_config=AecReferenceConfig(max_delay_ms=200),
        nlms_config=NlmsConfig(filter_length_ms=160, step_size=0.5, erle_gate_db=1.0),
    )
    analysis = analyze(
        reference=_resample(reference, src_rate=PLAYBACK_RATE, dst_rate=loop_rec.rate),
        loopback=loop_raw,
        mic=mic_raw,
        rate=loop_rec.rate,
        chain=chain,
    )

    path = artifact_dir / f"p10-aec-probe-{timestamp}.json"
    artifact = {
        "kind": "p10_aec_real_device_probe",
        "created_at": timestamp,
        "preflight": {
            "python": preflight["python"],
            "platform": preflight["platform"],
            "modules": preflight["modules"],
        },
        "devices": {"loopback": loopback, "mic": mic_device},
        "playback": play_facts,
        "analysis": analysis,
        "no_audio_persisted": not args.save_audio,
    }
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    saved: list[str] = []
    if args.save_audio:
        debug_dir = artifact_dir / f"audio-{timestamp}"
        reference48 = _resample(reference, src_rate=PLAYBACK_RATE, dst_rate=loop_rec.rate)
        saved.append(str(_save_wav(debug_dir / "reference.wav", reference48, rate=loop_rec.rate)))
        saved.append(str(_save_wav(debug_dir / "loopback.wav", loop_raw, rate=loop_rec.rate)))
        saved.append(str(_save_wav(debug_dir / "mic.wav", mic_raw, rate=mic_rec.rate)))
        print(f"[p10-aec] 已按 --save-audio 写出 3 个 WAV（本机音频落盘）：{debug_dir}")

    print("-" * 72)
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    print(f"[p10-aec] artifact：{path}")
    print("[p10-aec] 说明：erl_db = 麦克风里回声相对回环参考的电平比（越负说明回声越弱）；")
    print("[p10-aec]       erle_tail_db = 收敛后残差相对麦克风的抑制量（越大越好）。")
    return 0 if analysis.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
