"""P10 批次 R4-A：回声对齐与相关性估计（纯离线、确定性）。

为什么需要它：R4 真机三轮的结论是"**缺的是可信判别依据**"——
16 kHz 采集 / 24 kHz 播放拿不到可用参考时，无论怎么调门槛都无法区分"用户插话"与
"扬声器自己的声音"。把参考重采样到采集时钟（`reference_source.AecReferencePCM.source_sample_rate`）
之后，还需要回答两个问题：

1. 参考与采集之间的**声学延迟**是多少（播放缓冲 + 设备 + 空气路径，本机实测可达数百毫秒）；
2. 在这个延迟下，**相关性**有多强（越强越像"麦克风听到的就是我们自己放的"）。

本模块只做这两件事：给定最近若干秒的参考帧与采集帧（都是同钟 float32 帧序列），
用**粗搜（FFT 互相关 + 前缀和归一化）+ 细搜（逐样本零均值归一化相关）**给出
``EchoAlignment(correlation, delay_ms, ...)``。

硬边界：不访问设备/文件/网络、不创建线程；只依赖 stdlib、numpy 与项目契约；
不导入安全链模块，也不产生任何授权事实。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from app.contracts.base import ContractError

STATE_OK = "ok"
STATE_HISTORY_SHORT = "history_short"
STATE_SILENT = "silent"

DEFAULT_SEARCH_MS = 600
DEFAULT_MIN_SECONDS = 0.3
DEFAULT_COARSE_CANDIDATES = 8
REFINE_MS = 3
"""细搜范围（毫秒）：在粗搜峰值左右 ± 该范围做逐样本精确相关。"""

MIN_RMS = 1e-4
"""能量下限（float32 [-1,1) 口径）；低于它算"静音"，相关性没有意义。"""

MIN_OVERLAP_SECONDS = 0.15
"""最小重叠时长（秒）：重叠太短时归一化相关会虚假地趋近 ±1。

真机/离线都踩过：细搜若允许"只剩 2 个样本"的偏移，任意两点都完全相关，
峰值会被这种退化候选抢走（实测噪声参考也能报出 correlation=1.0）。
"""


@dataclass(frozen=True, slots=True)
class EchoAlignment:
    """一次对齐估计的结果；只含数值与固定短代码。"""

    state: str
    correlation: float
    delay_ms: int
    reference_seconds: float
    capture_seconds: float
    searched_candidates: int
    reference_rms: float
    capture_rms: float

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    def to_details(self) -> dict[str, object]:
        return {
            "state": self.state,
            "correlation": round(self.correlation, 4),
            "delay_ms": self.delay_ms,
            "reference_seconds": round(self.reference_seconds, 3),
            "capture_seconds": round(self.capture_seconds, 3),
            "searched_candidates": self.searched_candidates,
            "reference_rms": round(self.reference_rms, 6),
            "capture_rms": round(self.capture_rms, 6),
        }


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def _concat_frames(frames: Sequence[tuple[int, np.ndarray]]) -> tuple[int | None, np.ndarray]:
    """把 ``(at_ms, samples)`` 帧序列拼成连续序列；返回起始毫秒与拼接结果。"""

    if not frames:
        return None, np.zeros(0, dtype=np.float32)
    start = int(frames[0][0])
    arrays = [np.asarray(samples, dtype=np.float32).reshape(-1) for _at, samples in frames]
    return start, np.concatenate(arrays) if arrays else np.zeros(0, dtype=np.float32)


def _prefix_sums(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    zeros = np.zeros(1, dtype=np.float64)
    return (
        np.concatenate((zeros, np.cumsum(values, dtype=np.float64))),
        np.concatenate((zeros, np.cumsum(np.square(values, dtype=np.float64)))),
    )


def _segment_sums(
    prefix_sum: np.ndarray, prefix_sq: np.ndarray, starts: np.ndarray, lengths: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    ends = starts + lengths
    sums = prefix_sum[ends] - prefix_sum[starts]
    squares = prefix_sq[ends] - prefix_sq[starts]
    return sums, squares


def estimate_echo_alignment(
    *,
    reference_frames: Sequence[tuple[int, np.ndarray]],
    capture_frames: Sequence[tuple[int, np.ndarray]],
    sample_rate: int = 16_000,
    search_ms: int = DEFAULT_SEARCH_MS,
    min_seconds: float = DEFAULT_MIN_SECONDS,
) -> EchoAlignment:
    """估计参考与采集之间的延迟与归一化相关性（0~1，绝对值）。

    对齐约定：找到 ``offset`` 使 ``capture[i] ≈ reference[i - offset]``（``np.correlate`` 的
    线性互相关峰），于是

    ``delay_ms = (capture_start_ms - reference_start_ms) + offset * 1000 / sample_rate``

    即"麦克风在参考被播放后多久听到它"；真实链路必然为正。搜索范围 ``[-search_ms, +search_ms]``。
    """

    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ContractError("sample_rate 必须是正整数")
    if isinstance(search_ms, bool) or not isinstance(search_ms, int) or search_ms < 0:
        raise ContractError("search_ms 必须是非负整数")
    if isinstance(min_seconds, bool) or not isinstance(min_seconds, (int, float)) or min_seconds <= 0:
        raise ContractError("min_seconds 必须是正数")

    ref_start, ref = _concat_frames(reference_frames)
    mic_start, mic = _concat_frames(capture_frames)
    ref_seconds = ref.size / sample_rate
    mic_seconds = mic.size / sample_rate
    ref_rms = _rms(ref)
    mic_rms = _rms(mic)

    def short(state: str) -> EchoAlignment:
        return EchoAlignment(
            state=state,
            correlation=0.0,
            delay_ms=0,
            reference_seconds=ref_seconds,
            capture_seconds=mic_seconds,
            searched_candidates=0,
            reference_rms=ref_rms,
            capture_rms=mic_rms,
        )

    if ref_start is None or mic_start is None:
        return short(STATE_HISTORY_SHORT)
    if min(ref_seconds, mic_seconds) < float(min_seconds):
        return short(STATE_HISTORY_SHORT)
    if ref_rms < MIN_RMS or mic_rms < MIN_RMS:
        return short(STATE_SILENT)

    # ---- 粗搜：线性互相关 + 前缀和归一化（近似），只用来挑候选 --------------------
    n_mic = mic.size
    n_ref = ref.size
    full = np.correlate(mic, ref, mode="full")
    offsets_all = np.arange(full.size, dtype=np.int64) - (n_ref - 1)
    limit = int(round(search_ms * sample_rate / 1000.0))
    keep = (offsets_all >= -limit) & (offsets_all <= limit)
    offsets = offsets_all[keep]
    values = full[keep]

    ref_sum, ref_sq = _prefix_sums(ref)
    mic_sum, mic_sq = _prefix_sums(mic)
    min_overlap = max(
        2,
        int(MIN_OVERLAP_SECONDS * sample_rate),
        int(0.2 * min(n_mic, n_ref)),
    )
    # capture[i] ≈ reference[i - offset]：两侧各自的重叠起点
    starts_mic = np.maximum(0, offsets)
    starts_ref = np.maximum(0, -offsets)
    lengths = np.minimum(n_mic - starts_mic, n_ref - starts_ref)
    valid = lengths >= min_overlap
    if not np.any(valid):
        return short(STATE_HISTORY_SHORT)
    mic_seg_sum, mic_seg_sq = _segment_sums(mic_sum, mic_sq, starts_mic, lengths)
    ref_seg_sum, ref_seg_sq = _segment_sums(ref_sum, ref_sq, starts_ref, lengths)
    denominator = np.sqrt(
        np.maximum(mic_seg_sq - np.square(mic_seg_sum) / np.maximum(lengths, 1), 0.0)
        * np.maximum(ref_seg_sq - np.square(ref_seg_sum) / np.maximum(lengths, 1), 0.0)
    )
    overlap_mean_product = (mic_seg_sum * ref_seg_sum) / np.maximum(lengths, 1)
    numerator = np.abs(values - overlap_mean_product)
    scores = np.where(
        (denominator > 0.0) & valid, numerator / np.where(denominator > 0.0, denominator, 1.0), 0.0
    )
    candidate_count = int(min(DEFAULT_COARSE_CANDIDATES, offsets.size))
    top = np.argsort(scores)[-candidate_count:]

    # ---- 细搜：逐样本零均值归一化相关（粗搜峰值左右 ±REFINE_MS） ------------------
    refine = int(round(REFINE_MS * sample_rate / 1000.0))
    best_score = -1.0
    best_offset = 0
    for index in top:
        center = int(offsets[index])
        for offset in range(center - refine, center + refine + 1):
            # 配对 (capture[i], reference[i - offset])
            if offset >= 0:
                count = min(n_mic - offset, n_ref)
                mic_slice = mic[offset : offset + count]
                ref_slice = ref[:count]
            else:
                back = -offset
                count = min(n_mic, n_ref - back)
                mic_slice = mic[:count]
                ref_slice = ref[back : back + count]
            if count < min_overlap or mic_slice.size < min_overlap:
                # 重叠太短：相关系数会虚假地趋近 ±1（退化候选），一律不算。
                continue
            mic_centered = mic_slice - float(mic_slice.mean())
            ref_centered = ref_slice - float(ref_slice.mean())
            denom = float(np.linalg.norm(mic_centered)) * float(np.linalg.norm(ref_centered))
            if denom <= 0.0:
                continue
            score = abs(float(np.dot(mic_centered, ref_centered))) / denom
            if score > best_score:
                best_score = score
                best_offset = offset
    if best_score < 0.0:
        return short(STATE_HISTORY_SHORT)
    delay_ms = int(
        round(
            (mic_start - ref_start) + best_offset * 1000.0 / float(sample_rate)
        )
    )
    return EchoAlignment(
        state=STATE_OK,
        correlation=max(0.0, min(1.0, best_score)),
        delay_ms=delay_ms,
        reference_seconds=ref_seconds,
        capture_seconds=mic_seconds,
        searched_candidates=candidate_count,
        reference_rms=ref_rms,
        capture_rms=mic_rms,
    )


__all__ = [
    "DEFAULT_COARSE_CANDIDATES",
    "DEFAULT_MIN_SECONDS",
    "DEFAULT_SEARCH_MS",
    "MIN_RMS",
    "REFINE_MS",
    "STATE_HISTORY_SHORT",
    "STATE_OK",
    "STATE_SILENT",
    "EchoAlignment",
    "estimate_echo_alignment",
]
