"""P10 批次 R4-B5（B4）：真机**双讲检测 + 近端保护**验收探针（需 ``--live-opt-in``）。

回答三个真机问题（全部用**生产同款代码**离线分析本机真实录音，不猜）：
| 真实段 | 真值 | 期望的检测结论 |
|---|---|---|
| B 段：放音 6 s + 你保持安静 | 单讲（只有回声） | `double_talk=False`（**误报率越低越好**） |
| C 段：放音 6 s + 你同时说话 | 双讲 | `double_talk=True`（**检出率越高越好**） |
| A 段：静音 + 你说话 4 s | 近端-only | 不判双讲（没有回声可消） |

每帧喂给**生产** `DoubleTalkDetector`（不是仿写），事实包括：

- `echo_correlation`：按 B 段实测延迟对齐后的**窗口级**归一化相关（与生产链路同口径）；
- `erle_sufficient`：生产 `NlmsEchoCanceller` 的累计抑制是否 ≥ 门槛（本机真值）；
- `reference_rms` / `capture_rms`：对齐窗口内的 PCM16 RMS。

另外量两件 B3 保护的关键数值（合成对照已在离线测试里钉住，这里是**真机**数值）：

1. **冻结 vs 不冻结的权重漂移**：C 段同时跑两条 NLMS（`double_talk_step_scale=0.0` 与 `1.0`），
   比较"检测为双讲的那些帧"上的系数漂移；
2. **残余地板是否被污染**：比较 `protect`（双讲帧剔除）与 `facts`（不剔除）两种口径下
   的双讲峰值 `max(speech_threshold, 地板 × margin)` —— 地板抬高会把动态门抬高、进而漏判人声。

输出：控制台数值 + 可选 `--json` 摘要。**默认不落盘任何音频**（`--save-wav` 才写）。

⚠️ 边界（不伪装）：数值只在**当前系统音量、当前坐姿**下成立；本工具不打开生产会话、
不写入 `runtime.jsonl`、不做任何授权判定。
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio.aec.double_talk import (  # noqa: E402
    DT_STATE_DOUBLE_TALK,
    DoubleTalkConfig,
    DoubleTalkDetector,
    DoubleTalkObservation,
)
from app.audio.aec.nlms import NlmsConfig, NlmsEchoCanceller  # noqa: E402
from app.tts.waveout_pcm import WaveOutPCMStreamBackend  # noqa: E402
from tools.p10_aec_probe import (  # noqa: E402
    PLAYBACK_RATE,
    _pick_input,
    _play,
    _preflight,
    _print_preflight,
    _resample,
)
from tools.p10_r4b3_echo_ratio_probe import (  # noqa: E402
    DEFAULT_WAV,
    build_cue,
    read_wav_mono,
    safe_write,
)
from tools.p10_r4b4_realtime_doubletalk_probe import (  # noqa: E402
    BARGE_IN_ENERGY_GATE_PCM16,
    CAPTURE_RATE,
    FRAME_MS,
    FRAME_SAMPLES,
    SKIP_SECONDS,
    frame_levels,
    measure_delay_samples,
    percentile,
    to_capture_rate,
)

TOOL_NAME = "p10_r4b5_doubletalk_probe"
DEFAULT_STALL_FRAMES = 25  # 0.5 s 一次窗口相关（与生产 alignment_interval_frames 同口径）
DEFAULT_RESIDUAL_MARGIN = 3.0
"""残余回声地板感知动态门的倍率（与 `AutoVoiceConfig.aec_residual_gate_margin` 同默认值）。"""
DEFAULT_RESIDUAL_FRAMES = 50
"""残余地板统计窗口（帧）；50 帧 = 1 s（与通道默认同值）。"""
DEFAULT_RESIDUAL_QUANTILE = 0.2


def frame_rms(samples: np.ndarray) -> float:
    """PCM16 口径的帧 RMS（int16 数值域）。"""

    array = np.asarray(samples, dtype=np.float64)
    if array.size == 0:
        return 0.0
    return float(math.sqrt(float(np.dot(array, array)) / array.size))


def normalized_correlation(left: np.ndarray, right: np.ndarray) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    denominator = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
    if denominator <= 0.0:
        return 0.0
    return abs(float(np.dot(a, b) / denominator))


def aligned_correlations(
    mic16: np.ndarray,
    far16: np.ndarray,
    *,
    delay_samples: int,
    window_frames: int = DEFAULT_STALL_FRAMES,
) -> list[float]:
    """按实测延迟对齐后的**窗口级**归一化相关（每个窗口一个值，落在该窗口末帧）。

    延迟尚未覆盖的窗口（对齐后参考窗口越界/全零）返回 ``0.0``——如实表示"这一窗口没有
    可用对齐证据"，`DoubleTalkDetector` 会把低相关当候选证据、叠加 ERLE 才能进双讲（不猜）。
    """

    frames = min(mic16.size, far16.size) // FRAME_SAMPLES
    correlations = [0.0] * frames
    for start in range(0, frames, window_frames):
        stop = min(frames, start + window_frames)
        mic_slice = mic16[start * FRAME_SAMPLES : stop * FRAME_SAMPLES]
        far_start = start * FRAME_SAMPLES - delay_samples
        far_stop = stop * FRAME_SAMPLES - delay_samples
        if far_stop <= 0 or far_start >= far16.size:
            continue
        # 只取真正重叠的那一段（延迟尚未覆盖的开头/结尾窗口按实际重叠算，不整段丢弃）。
        overlap_start = max(0, far_start)
        overlap_stop = min(far16.size, far_stop)
        if overlap_stop - overlap_start < FRAME_SAMPLES:
            continue
        mic_offset = overlap_start - far_start
        mic_segment = mic_slice[mic_offset : mic_offset + (overlap_stop - overlap_start)]
        far_segment = far16[overlap_start:overlap_stop]
        if mic_segment.size != far_segment.size or mic_segment.size == 0:
            continue
        value = normalized_correlation(mic_segment, far_segment)
        for index in range(start, stop):
            correlations[index] = value
    return correlations


class NlmsBranch:
    """一条 NLMS 分析支路：逐帧处理并记录权重漂移、累计 ERLE 与"是否真的应用"。"""

    def __init__(self, *, scale: float) -> None:
        self._canceller = NlmsEchoCanceller(
            config=NlmsConfig(double_talk_step_scale=scale)
        )
        self.weight_drift_on_double_talk = 0.0
        self.applied_frames = 0
        self.frames = 0
        self._before: np.ndarray | None = None
        self._reference_energy = 0.0
        self._error_energy = 0.0

    def before_double_talk(self) -> None:
        self._before = self._canceller.weights.copy()

    @property
    def cumulative_erle_db(self) -> float | None:
        """累计 ERLE（参考能量 / 残差能量）；还没有参考能量时为 ``None``。"""

        if self._reference_energy <= 0.0:
            return None
        if self._error_energy <= 0.0:
            return None
        return 10.0 * math.log10(self._reference_energy / self._error_energy)

    def process(
        self,
        *,
        reference: bytes,
        capture: bytes,
        double_talk: bool,
        track_drift: bool = False,
    ):
        result = self._canceller.cancel(
            reference=reference, capture=capture, double_talk=double_talk
        )
        self.frames += 1
        if result.applied:
            self.applied_frames += 1
        # 直接沿用消除器自己的累计口径（同一份能量账，避免两处各算一遍导致读数漂移）。
        self._reference_energy = self._canceller._reference_energy_total  # noqa: SLF001
        self._error_energy = self._canceller._error_energy_total  # noqa: SLF001
        if track_drift and self._before is not None:
            drift = float(np.abs(self._canceller.weights - self._before).sum())
            self.weight_drift_on_double_talk = max(self.weight_drift_on_double_talk, drift)
        return result

    @property
    def cancellations(self) -> int:
        return self.applied_frames


def apm_speech_evidence(
    *,
    mic16: np.ndarray,
    far16: np.ndarray,
    stream_delay_ms: int,
    skip_seconds: float = SKIP_SECONDS,
) -> dict[str, Any]:
    """用 `pywebrtc_audio.AudioProcessor`（完整 APM：AEC3 + NS + AGC）算**引擎级**判据。

    动机（2026-10-13 逆向对照）：火山实时语音的**双讲判定由 AEC 引擎内部完成**——
    官方方案把端上 AEC/3A 交给 RTC SDK；硬件参考项目 `Baji_Rtc_Toy` 的实测结论更直接：
    "**mic/ref 能量比值**"这类土办法会被**参考串扰**搞坏（他们实测比值不跳、打断抓不到），
    最终改用 esp-sr **FD 模式**（内置 DTD，即官方 AFE 那套）。本机 `pywebrtc_audio` 恰好暴露了
    同一层信号：`AudioProcessor.speech_probability`（在 AEC 残差上算的人声概率）。

    返回每帧的 `speech_probability` 与"概率² × 残差能量"（既有探针的能量门是同口径的
    `rms > max(600, p_speech²)`；这里给出右边那一项供直接比较）。
    """

    import pywebrtc_audio  # noqa: PLC0415 - 可选依赖，缺失时如实返回不可用

    frames = min(mic16.size, far16.size) // FRAME_SAMPLES
    skip_frames = int(skip_seconds * 1000 / FRAME_MS)
    processor = pywebrtc_audio.AudioProcessor()
    processor.stream_delay_ms = int(stream_delay_ms)
    probabilities: list[float] = []
    gated: list[float] = []
    residual_levels: list[float] = []
    for index in range(frames):
        start = index * FRAME_SAMPLES
        stop = start + FRAME_SAMPLES
        near = np.asarray(mic16[start:stop] * 32767.0, dtype=np.int16)
        far = np.asarray(far16[start:stop] * 32767.0, dtype=np.int16)
        out = processor.process(near=near, far=far)
        if index < skip_frames:
            continue
        probability = float(processor.speech_probability)
        residual = float(np.dot(np.asarray(out, dtype=np.float64), np.asarray(out, dtype=np.float64)))
        probabilities.append(probability)
        gated.append(probability * probability * residual)
        residual_levels.append(math.sqrt(residual / max(1, len(out))))
    bounded = np.asarray(probabilities, dtype=np.float64)
    bounded_gate = np.asarray(gated, dtype=np.float64)
    residual_array = np.asarray(residual_levels, dtype=np.float64)
    # 参考串扰诊断（Baji_Rtc_Toy 的教训）：用户说话会串进参考 ⇒ 参考 RMS 也会涨。
    return {
        "frames": len(probabilities),
        "speech_probability_p50": percentile(bounded, 50),
        "speech_probability_p90": percentile(bounded, 90),
        "speech_probability_max": float(bounded.max()) if bounded.size else 0.0,
        "gated_energy_p50": percentile(bounded_gate, 50),
        "gated_energy_p90": percentile(bounded_gate, 90),
        "residual_rms_p50": percentile(residual_array, 50),
        "high_probability_frames": int(np.count_nonzero(bounded >= 0.8)),
        "note": "speech_probability 来自 APM（AEC3+NS+AGC）残差；高概率帧数越多越像「有人在说」",
    }


def erle_residual_frames(
    *,
    far16: np.ndarray,
    mic16: np.ndarray,
    threshold_db: float,
) -> tuple[list[bool], list[float]]:
    """逐帧跑生产 NLMS，返回 `(erle 是否够用, 该帧残差的 PCM16 RMS)`。

    真机会话里这两个事实来自**正在跑的真 AEC 后端**（通道每帧先跑 AEC，再用**残差**算电平与
    能量门）；本探针用生产 NLMS 在真机录音上离线复算，口径一致，并如实标注"离线复算"。
    """

    branch = NlmsBranch(scale=1.0)
    frames = min(mic16.size, far16.size) // FRAME_SAMPLES
    flags: list[bool] = []
    residual_levels: list[float] = []
    for index in range(frames):
        start = index * FRAME_SAMPLES
        stop = start + FRAME_SAMPLES
        result = branch.process(
            reference=np.asarray(far16[start:stop] * 32767.0, dtype=np.int16).tobytes(),
            capture=np.asarray(mic16[start:stop] * 32767.0, dtype=np.int16).tobytes(),
            double_talk=False,
        )
        erle = branch.cumulative_erle_db
        flags.append(erle is not None and erle >= threshold_db)
        residual_levels.append(frame_rms(np.asarray(result.output, dtype=np.float32)))
    return flags, residual_levels


def analyse_phase(
    *,
    mic16: np.ndarray,
    far16: np.ndarray,
    delay_samples: int,
    detector: DoubleTalkDetector,
    nlms_scale: float,
    truth_double_talk: bool,
    erle_flags: list[bool] | None = None,
    residual16: np.ndarray | None = None,
    speech_threshold: float = BARGE_IN_ENERGY_GATE_PCM16,
    skip_seconds: float = SKIP_SECONDS,
) -> dict[str, Any]:
    """把一段真机录音逐帧喂给生产检测器 + 一条 NLMS 支路，返回只读事实。

    - ``erle_flags``：每帧"AEC 是否够用"的**离线复算**（真机会话里由正在跑的后端给出）；
      只有它为 ``True`` 时才会派生出 `erle_insufficient` 证据 —— 与生产链路同口径。
    - ``residual16``：AEC **残差**（生产链路的能量/地板那条腿用的就是它）。**采集 RMS 必须
      用残差而不是原始麦克风**，否则未消除的回声会让"采集比参考响"这条证据假成立
      （真机第一轮 100% 误报就是这个口径错造成的）。
    """

    frames = min(mic16.size, far16.size) // FRAME_SAMPLES
    # ⚠️ 相关性仍用**原始采集**：残差里的回声已被减掉，与参考做相关会被"洗掉"（生产链路
    # 的 `_observe_alignment` 也用原始帧，这里保持同源）。
    correlations = aligned_correlations(mic16, far16, delay_samples=delay_samples)
    branch = NlmsBranch(scale=nlms_scale)
    detector.reset()
    detections: list[bool | None] = []
    states: collections.Counter[str] = collections.Counter()
    evidence: collections.Counter[str] = collections.Counter()
    mic_levels: list[float] = []
    skip_frames = int(skip_seconds * 1000 / FRAME_MS)
    warmup_reference = 0.0
    warmup_error = 0.0
    # 与生产的执行顺序对齐：AEC 用的是**上一帧**的双讲判定（最多滞后 1 帧 = 20 ms）。
    last_double_talk = False
    for index in range(frames):
        start = index * FRAME_SAMPLES
        stop = start + FRAME_SAMPLES
        mic_frame = mic16[start:stop]
        far_frame = far16[start:stop]
        reference_bytes = np.asarray(far_frame * 32767.0, dtype=np.int16).tobytes()
        capture_bytes = np.asarray(mic_frame * 32767.0, dtype=np.int16).tobytes()
        if index < skip_frames:
            # 收敛期：照生产口径喂帧（吃满自适应），但**不计入判定统计**（与既有探针同口径）。
            result = branch._canceller.cancel(  # noqa: SLF001 - 只为暖机
                reference=reference_bytes, capture=capture_bytes, double_talk=False
            )
            warmup_reference = branch._canceller._reference_energy_total  # noqa: SLF001
            warmup_error = branch._canceller._error_energy_total  # noqa: SLF001
            continue
        if index == skip_frames:
            branch.before_double_talk()
        residual_frame = None
        if residual16 is not None and index * FRAME_SAMPLES < len(residual16):
            residual_frame = residual16[index * FRAME_SAMPLES : (index + 1) * FRAME_SAMPLES]
        capture_level = (
            frame_rms(np.asarray(residual_frame * 32767.0, dtype=np.int16))
            if residual_frame is not None
            else frame_rms(np.asarray(mic_frame * 32767.0, dtype=np.int16))
        )
        observation = DoubleTalkObservation(
            frame_index=index,
            capture_rms=capture_level,
            reference_rms=frame_rms(np.asarray(far_frame * 32767.0, dtype=np.int16)),
            reference_active=True,
            echo_correlation=float(correlations[index]),
            erle_sufficient=(None if not erle_flags else erle_flags[min(index, len(erle_flags) - 1)]),
        )
        result = detector.observe(observation)
        states[result.state] += 1
        for code in result.evidence_codes:
            evidence[code] += 1
        detections.append(result.double_talk)
        mic_levels.append(capture_level)
        # ⚠️ 与生产的执行顺序对齐：**先判双讲、再把结果传给本帧的 AEC**（因此这里用
        # 上一帧的 `last_double_talk`，最多滞后 1 帧 = 20 ms）。
        branch.process(
            reference=reference_bytes,
            capture=capture_bytes,
            double_talk=bool(last_double_talk),
            track_drift=bool(last_double_talk),
        )
        last_double_talk = result.double_talk is True
    analysed = [value for value in detections if value is not None]
    true_count = sum(1 for value in analysed if value is True)
    false_count = sum(1 for value in analysed if value is False)
    return {
        "frames_analysed": len(analysed),
        "states": dict(states),
        "evidence_codes": dict(evidence),
        "double_talk_frames": true_count,
        "single_talk_frames": false_count,
        "unproven_frames": sum(1 for value in detections if value is None),
        "double_talk_ratio": true_count / len(analysed) if analysed else None,
        "mic_p50": percentile(np.asarray(mic_levels, dtype=np.float64), 50),
        "mic_p95": percentile(np.asarray(mic_levels, dtype=np.float64), 95),
        "weight_drift_on_double_talk": branch.weight_drift_on_double_talk,
        "truth_double_talk": truth_double_talk,
        "erle_flags_source": "offline_nlms_recompute" if erle_flags else "unavailable",
        "erle_sufficient_true_frames": (
            None if not erle_flags else sum(1 for flag in erle_flags[skip_frames:] if flag)
        ),
        # R4-B5 诊断：把"判定依据"本身也暴露出来 —— 否则误报/漏报无法归因（真机第一轮就吃了这个亏）。
        "correlation_median": percentile(np.asarray(correlations[skip_frames:], dtype=np.float64), 50),
        "correlation_max": float(np.max(correlations[skip_frames:])) if frames > skip_frames else 0.0,
        "reference_rms_p50": percentile(
            np.asarray(
                [
                    frame_rms(np.asarray(far16[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES] * 32767.0, dtype=np.int16))
                    for i in range(skip_frames, frames)
                ],
                dtype=np.float64,
            ),
            50,
        ),
    }


def floor_gate(
    *,
    residual_levels: list[float], speech_threshold: float, margin: float
) -> tuple[float, float]:
    """按生产口径算 `(残余地板 p20, 动态门 max(固定门, 地板×margin))`。"""

    if not residual_levels:
        return 0.0, speech_threshold
    ordered = sorted(residual_levels)
    index = max(0, min(int(math.ceil(DEFAULT_RESIDUAL_QUANTILE * len(ordered))) - 1, len(ordered) - 1))
    floor = float(ordered[index])
    return floor, max(speech_threshold, floor * margin)


def run_pipeline(
    *,
    echo_mic16: np.ndarray,
    far_echo16: np.ndarray,
    dt_mic16: np.ndarray | None,
    far_dt16: np.ndarray | None,
    delay_samples: int,
    user_mic16: np.ndarray | None,
    talk_threshold: float,
    residual_margin: float,
    delay_ms: int = 0,
) -> dict[str, Any]:
    """跑生产检测器 + 两条 NLMS 支路，给出 B4 交付的数值。"""

    config = DoubleTalkConfig()
    report: dict[str, Any] = {"double_talk_config": config.to_details(), "phases": {}}
    # 引擎判据（APM）要喂 `stream_delay_ms`：优先用调用方传入的**实测延迟**（真机第一轮这里
    # 引用了不存在的 `delay_ms`，白跑一段；修完改成显式参数 + 报告回退）。
    stream_delay_ms = int(delay_ms or (report.get("delay") or {}).get("ms") or 0)

    # ---- "AEC 是否够用 + 残差电平"的离线复算：在**只放音**的那段上逐帧算（口径同生产 NLMS）。
    # 生产链路：每帧先跑 AEC，再用**残差**算电平/能量门与地板 ⇒ 这里必须同样用残差喂采集侧。
    erle_flags, residual_echo16 = erle_residual_frames(
        far16=far_echo16,
        mic16=echo_mic16,
        threshold_db=NlmsConfig().erle_gate_db,
    )
    sufficient_frames = sum(1 for flag in erle_flags if flag)
    report["erle"] = {
        "source": "offline_nlms_recompute",
        "threshold_db": NlmsConfig().erle_gate_db,
        "sufficient_frames": sufficient_frames,
        "total_frames": len(erle_flags),
        "note": "真机会话里这两个事实由正在跑的后端给出；本探针在真机录音上离线复算，口径一致",
    }

    # ---- B 段（单讲真值）：检测器 + 冻结支路（采集侧用残差）
    detector = DoubleTalkDetector(config=config, mode="facts")
    report["phases"]["echo_only"] = analyse_phase(
        mic16=echo_mic16,
        far16=far_echo16,
        delay_samples=delay_samples,
        detector=detector,
        nlms_scale=config.freeze_step_scale,
        truth_double_talk=False,
        erle_flags=erle_flags,
        residual16=residual_echo16,
    )
    false_positive = report["phases"]["echo_only"]["double_talk_ratio"] or 0.0
    report["false_positive_ratio"] = false_positive
    # ---- 引擎级判据（APM speech_probability）：本机有没有"可用的 DTD 信号"
    try:
        report["apm"] = {
            "echo_only": apm_speech_evidence(
                mic16=echo_mic16, far16=far_echo16, stream_delay_ms=stream_delay_ms
            )
        }
    except Exception as exc:  # noqa: BLE001 - 可选依赖/引擎异常一律如实记录，不中断验收
        report["apm"] = {"error": f"{type(exc).__name__}: {exc}"}

    # ---- C 段（双讲真值）：检测器 + 冻结/不冻结两条支路（各自的残差喂各自的检测器）
    if dt_mic16 is not None and far_dt16 is not None:
        dt_flags, dt_residual16 = erle_residual_frames(
            far16=far_dt16,
            mic16=dt_mic16,
            threshold_db=NlmsConfig().erle_gate_db,
        )
        detector_dt = DoubleTalkDetector(config=config, mode="facts")
        report["phases"]["double_talk"] = analyse_phase(
            mic16=dt_mic16,
            far16=far_dt16,
            delay_samples=delay_samples,
            detector=detector_dt,
            nlms_scale=config.freeze_step_scale,
            truth_double_talk=True,
            erle_flags=dt_flags,
            residual16=dt_residual16,
        )
        detector_legacy = DoubleTalkDetector(config=config, mode="facts")
        legacy = analyse_phase(
            mic16=dt_mic16,
            far16=far_dt16,
            delay_samples=delay_samples,
            detector=detector_legacy,
            nlms_scale=1.0,
            truth_double_talk=True,
            erle_flags=dt_flags,
            residual16=dt_residual16,
        )
        detection_ratio = report["phases"]["double_talk"]["double_talk_ratio"] or 0.0
        report["detection_ratio"] = detection_ratio
        report["frozen_vs_unfrozen"] = {
            "frozen_weight_drift": report["phases"]["double_talk"]["weight_drift_on_double_talk"],
            "unfrozen_weight_drift": legacy["weight_drift_on_double_talk"],
        }
        report["residual_floor"] = _residual_floor_comparison(
            dt_mic16=dt_mic16,
            far_dt16=far_dt16,
            delay_samples=delay_samples,
            detector=detector,
            speech_threshold=talk_threshold,
            margin=residual_margin,
            erle_flags=None,
        )
        try:
            report.setdefault("apm", {})["double_talk"] = apm_speech_evidence(
                mic16=dt_mic16, far16=far_dt16, stream_delay_ms=stream_delay_ms
            )
        except Exception as exc:  # noqa: BLE001
            report.setdefault("apm", {})["double_talk"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        report["detection_ratio"] = None
        report["frozen_vs_unfrozen"] = None
        report["residual_floor"] = None

    # ---- A 段（近端-only 真值）：不得判双讲
    if user_mic16 is not None:
        # 近端-only：参考按静音处理（没有回声可消），检测器必须报"不可能是双讲"。
        silent_far = np.zeros_like(user_mic16)
        detector_near = DoubleTalkDetector(config=config, mode="facts")
        report["phases"]["near_only"] = analyse_phase(
            mic16=user_mic16,
            far16=silent_far,
            delay_samples=0,
            detector=detector_near,
            nlms_scale=config.freeze_step_scale,
            truth_double_talk=False,
        )
        try:
            report.setdefault("apm", {})["near_only"] = apm_speech_evidence(
                mic16=user_mic16, far16=silent_far, stream_delay_ms=0
            )
        except Exception as exc:  # noqa: BLE001
            report.setdefault("apm", {})["near_only"] = {"error": f"{type(exc).__name__}: {exc}"}

    # ---- 结论（只按可观测门限判定，不猜）
    checks: dict[str, Any] = {
        "false_positive_within_budget": false_positive <= 0.05,
        "detection_ratio_meets_budget": (report["detection_ratio"] or 0.0) >= 0.5,
    }
    report["checks"] = checks
    if all(checks.values()):
        report["decision"] = "go"
        report["decision_detail"] = "只放音不误判、双讲能检出 ⇒ 双讲保护可进入真机会话试听"
    elif checks["false_positive_within_budget"]:
        report["decision"] = "go_with_caution"
        report["decision_detail"] = "误报在预算内但检出率偏低 ⇒ 可试听，但不得据此声称「已保护」"
    else:
        report["decision"] = "no_go"
        report["decision_detail"] = "只放音就误判双讲 ⇒ 维持 protect=关闭（避免无谓冻结步长）"
    return report


def _residual_floor_comparison(
    *,
    dt_mic16: np.ndarray,
    far_dt16: np.ndarray,
    delay_samples: int,
    detector: DoubleTalkDetector,
    speech_threshold: float,
    margin: float,
    erle_flags: list[bool] | None = None,
) -> dict[str, Any]:
    """双讲段上比较两种地板口径：`facts`（不剔除）vs `protect`（剔除双讲帧）。"""

    frames = min(dt_mic16.size, far_dt16.size) // FRAME_SAMPLES
    correlations = aligned_correlations(dt_mic16, far_dt16, delay_samples=delay_samples)
    detector.reset()
    facts_levels: list[float] = []
    protect_levels: list[float] = []
    mic_levels: list[float] = []
    skip_frames = int(SKIP_SECONDS * 1000 / FRAME_MS)
    for index in range(frames):
        start = index * FRAME_SAMPLES
        stop = start + FRAME_SAMPLES
        mic_frame = np.asarray(dt_mic16[start:stop] * 32767.0, dtype=np.int16)
        far_frame = np.asarray(far_dt16[start:stop] * 32767.0, dtype=np.int16)
        if index < skip_frames:
            continue
        result = detector.observe(
            DoubleTalkObservation(
                frame_index=index,
                capture_rms=frame_rms(mic_frame),
                reference_rms=frame_rms(far_frame),
                reference_active=True,
                echo_correlation=float(correlations[index]),
                erle_sufficient=(None if not erle_flags else erle_flags[min(index, len(erle_flags) - 1)]),
            )
        )
        level = frame_rms(mic_frame)
        mic_levels.append(level)
        facts_levels.append(level)
        if result.double_talk is not True:
            protect_levels.append(level)
    facts_floor, facts_gate = floor_gate(
        residual_levels=facts_levels, speech_threshold=speech_threshold, margin=margin
    )
    protect_floor, protect_gate = floor_gate(
        residual_levels=protect_levels, speech_threshold=speech_threshold, margin=margin
    )
    return {
        "note": "本段地板用整段双讲麦克风电平近似（真实链路用的是 AEC 残差；趋势同向）",
        "facts_floor_pcm16": facts_floor,
        "protect_floor_pcm16": protect_floor,
        "facts_gate_pcm16": facts_gate,
        "protect_gate_pcm16": protect_gate,
        "gate_delta_pcm16": facts_gate - protect_gate,
        "facts_samples": len(facts_levels),
        "protect_samples": len(protect_levels),
        "mic_p50": percentile(np.asarray(mic_levels, dtype=np.float64), 50),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P10 R4-B5 真机双讲检测 + 近端保护验收（需 --live-opt-in）"
    )
    parser.add_argument("--live-opt-in", action="store_true", help="显式开启真机（会放音并录音）")
    parser.add_argument("--check-only", action="store_true", help="只做前置检查，不打开设备流")
    parser.add_argument("--wav", default=str(DEFAULT_WAV))
    parser.add_argument("--repeat", type=int, default=3, help="播放素材重复遍数（默认 3 ≈ 6 s）")
    parser.add_argument("--user-seconds", type=float, default=4.0, help="A 段用户说话时长")
    parser.add_argument("--mic-device", type=int, default=None)
    parser.add_argument("--json", default="", help="可选：数值摘要写入路径")
    parser.add_argument(
        "--speech-threshold",
        type=float,
        default=BARGE_IN_ENERGY_GATE_PCM16,
        help="能量门（PCM16 口径，默认 600，与生产一致）",
    )
    parser.add_argument(
        "--residual-margin",
        type=float,
        default=DEFAULT_RESIDUAL_MARGIN,
        help="残余地板动态门倍率（默认 3.0，与生产一致）",
    )
    parser.add_argument(
        "--echo-only",
        action="store_true",
        help="只做 B 段（放音 + 保持安静），不需要用户说话 → 只测误报率",
    )
    parser.add_argument("--save-wav", action="store_true", help="把录音落盘（默认不落盘）")
    parser.add_argument("--save-dir", default="", help="配合 --save-wav 的输出目录")
    args = parser.parse_args(argv)

    safe_write(f"[{TOOL_NAME}] 真机双讲检测验收；音频只在内存分析，默认不落盘\n")
    if not args.live_opt_in or args.check_only:
        safe_write(f"[{TOOL_NAME}] 只做前置检查（未提供 --live-opt-in 或指定了 --check-only）\n")
        _print_preflight(_preflight())
        safe_write(f"[{TOOL_NAME}] 加 --live-opt-in 才会真正放音与录音\n")
        return 0

    import pyaudiowpatch  # noqa: PLC0415

    preflight = _preflight()
    wasapi = preflight.get("wasapi") or {}
    mic_device = _pick_input(wasapi.get("inputs", []), args.mic_device)
    if mic_device is None:
        safe_write(f"[{TOOL_NAME}] 找不到可用麦克风端点，终止\n")
        _print_preflight(preflight)
        return 1

    from tools.p10_aec_probe import _Recorder  # noqa: PLC0415

    wav_path = Path(args.wav)
    if not wav_path.exists():
        safe_write(f"[{TOOL_NAME}] 找不到播放素材：{wav_path}\n")
        return 2
    speech, speech_rate = read_wav_mono(wav_path)
    if speech_rate != PLAYBACK_RATE:
        speech = _resample(speech, src_rate=speech_rate, dst_rate=PLAYBACK_RATE)
    play_signal = np.tile(speech, max(1, int(args.repeat)))
    cue = build_cue()
    mic_rate = int(mic_device["rate"])

    report: dict[str, Any] = {
        "tool": TOOL_NAME,
        "mic": {"name": mic_device.get("name"), "rate": mic_rate},
        "playback": {
            "wav": wav_path.name,
            "repeat": int(args.repeat),
            "backend": WaveOutPCMStreamBackend.backend_name,
        },
        "speech_threshold": args.speech_threshold,
        "residual_margin": args.residual_margin,
        "note": "数值只在当前系统音量 + 当前坐姿下成立；不打开生产会话、不写 runtime.jsonl",
    }
    captured: dict[str, np.ndarray] = {}
    audio = pyaudiowpatch.PyAudio()
    try:
        if not args.echo_only:
            safe_write(f"[{TOOL_NAME}] A 段：三声提示音后请说 {args.user_seconds:.0f} 秒话\n")
            _play(cue, backend_factory=WaveOutPCMStreamBackend)
            recorder = _Recorder(audio, mic_device)
            recorder.start()
            time.sleep(max(1.0, args.user_seconds))
            captured["user_only"] = recorder.stop()

        safe_write(f"[{TOOL_NAME}] B 段：开始放音，请保持安静（不要说话）\n")
        recorder = _Recorder(audio, mic_device)
        recorder.start()
        time.sleep(0.3)
        play_started = time.monotonic()
        _play(play_signal, backend_factory=WaveOutPCMStreamBackend)
        play_ended = time.monotonic()
        time.sleep(0.25)
        echo_mic = recorder.stop()
        report["phase_b_wall"] = {
            "record_started_at": recorder.started_at,
            "play_started_at": play_started,
            "play_ended_at": play_ended,
            "mic_seconds": round(echo_mic.size / mic_rate, 3),
        }

        dt_mic: np.ndarray | None = None
        if not args.echo_only:
            safe_write(f"[{TOOL_NAME}] C 段：三声提示音后**一边放音一边说话**\n")
            _play(cue, backend_factory=WaveOutPCMStreamBackend)
            recorder = _Recorder(audio, mic_device)
            recorder.start()
            time.sleep(0.3)
            play_started_dt = time.monotonic()
            _play(play_signal, backend_factory=WaveOutPCMStreamBackend)
            play_ended_dt = time.monotonic()
            time.sleep(0.25)
            dt_mic = recorder.stop()
            report["phase_c_wall"] = {
                "record_started_at": recorder.started_at,
                "play_started_at": play_started_dt,
                "play_ended_at": play_ended_dt,
                "mic_seconds": round(dt_mic.size / mic_rate, 3),
            }
    finally:
        audio.terminate()

    if args.save_wav:
        out_dir = Path(args.save_dir) if args.save_dir else REPO_ROOT / "artifacts" / TOOL_NAME
        out_dir.mkdir(parents=True, exist_ok=True)
        import wave  # noqa: PLC0415

        for name, samples in (("echo_mic", echo_mic), ("double_talk_mic", dt_mic)):
            if samples is None:
                continue
            with wave.open(str(out_dir / f"{name}.wav"), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(mic_rate)
                handle.writeframes(
                    np.asarray(np.clip(samples, -1.0, 1.0) * 32767.0, dtype="<i2").tobytes()
                )
        report["saved_wav_dir"] = str(out_dir)

    # ---------------------------------------------------------------- 分析（纯内存）
    echo_mic16 = to_capture_rate(echo_mic, mic_rate)
    user_mic16 = (
        to_capture_rate(captured["user_only"], mic_rate)
        if captured.get("user_only") is not None
        else None
    )
    dt_mic16 = to_capture_rate(dt_mic, mic_rate) if dt_mic is not None else None

    def far_into(length: int, *, phase: str) -> np.ndarray:
        far16 = _resample(play_signal, src_rate=PLAYBACK_RATE, dst_rate=CAPTURE_RATE)
        offset = max(
            0,
            int(
                (report[phase]["play_started_at"] - report[phase]["record_started_at"])
                * CAPTURE_RATE
            ),
        )
        out = np.zeros(length, dtype=np.float32)
        end = min(length, offset + far16.size)
        if end > offset:
            out[offset:end] = far16[: end - offset]
        return out

    far_b = far_into(echo_mic16.size, phase="phase_b_wall")
    delay_samples, delay_ms = measure_delay_samples(far_b, echo_mic16)
    report["delay"] = {"samples": delay_samples, "ms": round(delay_ms, 2)}

    far_c = far_into(dt_mic16.size, phase="phase_c_wall") if dt_mic16 is not None else None
    pipeline = run_pipeline(
        echo_mic16=echo_mic16,
        far_echo16=far_b,
        dt_mic16=dt_mic16,
        far_dt16=far_c,
        delay_samples=delay_samples,
        delay_ms=int(round(delay_ms)),  # 引擎判据（APM）用的延迟提示
        user_mic16=user_mic16,
        talk_threshold=float(args.speech_threshold),
        residual_margin=float(args.residual_margin),
    )
    report.update(pipeline)

    safe_write(f"\n[{TOOL_NAME}] ===== 真机数值 =====\n")
    safe_write(
        f"[{TOOL_NAME}] 延迟实测：{report['delay']['ms']} ms（{report['delay']['samples']} 样本）；"
        f"ERLE 够用帧 {report['erle']['sufficient_frames']}/{report['erle']['total_frames']}"
        f"（门槛 {report['erle']['threshold_db']} dB，离线复算）\n"
    )
    echo_phase = report["phases"]["echo_only"]
    safe_write(
        f"[{TOOL_NAME}] B 段（单讲真值）：帧 {echo_phase['frames_analysed']}，"
        f"判双讲 {echo_phase['double_talk_frames']}（误报率 "
        f"{100 * (report.get('false_positive_ratio') or 0.0):.1f}%），"
        f"状态 {echo_phase['states']}\n"
    )
    safe_write(
        f"[{TOOL_NAME}] B 段判定依据：对齐相关 中位 {echo_phase['correlation_median']:.3f} / "
        f"最大 {echo_phase['correlation_max']:.3f}；参考 RMS p50 "
        f"{echo_phase['reference_rms_p50']:.0f}；采集 RMS p50 {echo_phase['mic_p50']:.0f}；"
        f"erle_sufficient=True 帧 {echo_phase['erle_sufficient_true_frames']}；"
        f"证据 {echo_phase['evidence_codes']}\n"
    )
    if report.get("phases", {}).get("double_talk"):
        dt_phase = report["phases"]["double_talk"]
        safe_write(
            f"[{TOOL_NAME}] C 段（双讲真值）：帧 {dt_phase['frames_analysed']}，"
            f"检出 {dt_phase['double_talk_frames']}（检出率 "
            f"{100 * (report.get('detection_ratio') or 0.0):.1f}%），"
            f"状态 {dt_phase['states']}\n"
        )
        safe_write(
            f"[{TOOL_NAME}] C 段判定依据：对齐相关 中位 {dt_phase['correlation_median']:.3f} / "
            f"最大 {dt_phase['correlation_max']:.3f}；参考 RMS p50 "
            f"{dt_phase['reference_rms_p50']:.0f}；采集 RMS p50 {dt_phase['mic_p50']:.0f}；"
            f"证据 {dt_phase['evidence_codes']}\n"
        )
        frozen = report.get("frozen_vs_unfrozen") or {}
        safe_write(
            f"[{TOOL_NAME}] 权重漂移（双讲帧）：冻结 {frozen.get('frozen_weight_drift'):.5f} / "
            f"不冻结 {frozen.get('unfrozen_weight_drift'):.5f}\n"
        )
    if report.get("phases", {}).get("near_only"):
        near = report["phases"]["near_only"]
        safe_write(
            f"[{TOOL_NAME}] A 段（近端-only）：判双讲 {near['double_talk_frames']} 帧，"
            f"状态 {near['states']}\n"
        )
    if report.get("residual_floor"):
        floor = report["residual_floor"]
        safe_write(
            f"[{TOOL_NAME}] 地板口径：facts 门 {floor['facts_gate_pcm16']:.1f} / "
            f"protect 门 {floor['protect_gate_pcm16']:.1f}（差 {floor['gate_delta_pcm16']:+.1f}）\n"
        )
    apm = report.get("apm") or {}
    for phase, label in (("echo_only", "B 段"), ("double_talk", "C 段"), ("near_only", "A 段")):
        evidence = apm.get(phase)
        if not evidence:
            continue
        if "error" in evidence:
            safe_write(f"[{TOOL_NAME}] {label} 引擎判据（APM）：不可用（{evidence['error']}）\n")
            continue
        safe_write(
            f"[{TOOL_NAME}] {label} 引擎判据（APM speech_probability，逆向自火山做法）："
            f"中位 {evidence['speech_probability_p50']:.3f} / p90 "
            f"{evidence['speech_probability_p90']:.3f} / 最大 "
            f"{evidence['speech_probability_max']:.3f}；概率≥0.8 帧 "
            f"{evidence['high_probability_frames']}/{evidence['frames']}；残差 RMS p50 "
            f"{evidence['residual_rms_p50']:.0f}\n"
        )
    safe_write(
        f"[{TOOL_NAME}] 结论：{report['decision']} —— {report['decision_detail']}\n"
    )
    if args.json:
        # 父目录不存在就建（否则用户给了子目录会直接 FileNotFoundError，白跑一轮真机）。
        try:
            json_path = Path(args.json)
            if json_path.parent and not json_path.parent.exists():
                json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            safe_write(f"[{TOOL_NAME}] JSON 摘要：{json_path}\n")
        except OSError as exc:  # noqa: BLE001 - 写盘失败不得吞掉已经测到的数值
            safe_write(f"[{TOOL_NAME}] JSON 写入失败（{type(exc).__name__}: {exc}）；数值见上方控制台输出\n")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
