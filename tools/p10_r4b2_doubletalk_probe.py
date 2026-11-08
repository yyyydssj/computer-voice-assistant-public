"""P10 批次 R4-B-2 阶段 0：**双讲保持** go/no-go 探针（离线、不需要设备）。

要回答的问题（R4-B-1 遗留的第一个风险）：
> 播出助手语音（far）的同时用户说话（near）时，AEC3 会不会把**用户自己的语音**一起压掉？

如果会，把 AEC3 接进会话路径就有害（伤 ASR 与打断判别），本批必须停在这里改走兜底路线。
因此本工具用**本机真实中文语音**（`models/kws/test_wavs/*.wav`，16 kHz 单声道 PCM16）做三组
对照实验，全部只在内存里合成，**不打开设备、不落盘音频**：

| 场景 | far（播放） | near（麦克风，含回声） | 看什么 |
|---|---|---|---|
| FAR_ONLY | 真实语音 | 纯回声 + 噪声 | 回声能不能被消掉（ERLE） |
| NEAR_ONLY | 静音 | 纯用户语音 + 噪声 | 没有回声时**不许**动用户语音（增益 ≈ 0 dB） |
| DOUBLE_TALK | 真实语音 | 用户语音 + 回声 + 噪声 | **用户语音保留度**（核心判据） |

回声路径是**合成**的（延迟 + 增益 + 一个反射抽头 + 一阶低通 + 噪声底），只是"像"，**不是**
本机真实声学路径；本工具的结论只能用来判断"算法会不会吃掉用户语音"，不能替代真机 ERLE。

判据（脚本内 `verdict()`；数值全部打印，便于复核）：
- FAR_ONLY：ERLE ≥ `--min-erle-db`（默认 10 dB）；
- NEAR_ONLY：用户语音增益 ≥ `--min-near-gain-db`（默认 −3 dB）；
- DOUBLE_TALK：用户语音增益 ≥ `--min-double-talk-gain-db`（默认 −6 dB）。
三项全过 → `go`；否则 `no_go`（并说明哪一项没过）。
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio.aec.nlms import NlmsConfig, NlmsEchoCanceller  # noqa: E402
from app.audio.aec.reference_source import (  # noqa: E402
    BACKEND_NLMS,
    BACKEND_WEBRTC,
    AecReferencePCM,
    SameProcessAecReference,
)
from app.audio.aec.webrtc_backend import WebrtcAecConfig, WebrtcEchoCanceller  # noqa: E402

RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000  # 320
DEFAULT_WAV_DIR = REPO_ROOT / "models" / "kws" / "test_wavs"
# 项目打断能量门（`app/runtime/auto_voice.py` 的 `speech_threshold`，int16 RMS 口径）。
BARGE_IN_GATE_DEFAULT = 600.0
# 本机实测麦克风噪声底（R2b-2：p50≈47.7 / max≈150）。
MIC_NOISE_FLOOR_MAX = 150.0
EPS = 1e-12

SCENARIO_FAR_ONLY = "far_only"
SCENARIO_NEAR_ONLY = "near_only"
SCENARIO_DOUBLE_TALK = "double_talk"
SCENARIOS = (SCENARIO_FAR_ONLY, SCENARIO_NEAR_ONLY, SCENARIO_DOUBLE_TALK)


# --------------------------------------------------------------------------- 音频 I/O


def read_wav(path: Path) -> np.ndarray:
    """读 16 kHz 单声道 PCM16 wav → float32 [-1,1)（格式不符直接报错，不猜）。"""

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if (channels, width, rate) != (1, 2, RATE):
        raise SystemExit(f"{path.name}: 需要 16 kHz 单声道 PCM16，实际 {rate} Hz/{channels}ch/{width*8}bit")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return samples


def normalize_rms(samples: np.ndarray, target_rms: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    if rms <= EPS:
        return samples.copy()
    return (samples * (target_rms / rms)).astype(np.float32)


def tile_to(samples: np.ndarray, length: int) -> np.ndarray:
    if samples.size == 0:
        raise SystemExit("音频为空")
    repeats = int(np.ceil(length / samples.size))
    return np.tile(samples, repeats)[:length].astype(np.float32)


def synth_echo_path(
    far: np.ndarray,
    *,
    delay_ms: int,
    gain: float,
    reflection_gain: float,
    lowpass: bool,
) -> np.ndarray:
    """合成"像扬声器→麦克风"的回声：延迟 + 增益 + 一个反射抽头 + 一阶低通。"""

    delay = int(round(RATE * delay_ms / 1000.0))
    echo = np.zeros_like(far)
    echo[delay:] = far[: far.size - delay] * gain
    if reflection_gain > 0.0:
        second = delay + int(round(RATE * 15 / 1000.0))
        if second < far.size:
            echo[second:] += far[: far.size - second] * reflection_gain
    if lowpass:
        echo = np.convolve(echo, np.array([0.5, 0.5], dtype=np.float32), mode="same")
    return echo.astype(np.float32)


# --------------------------------------------------------------------------- 度量


def energy(samples: np.ndarray) -> float:
    return float(np.mean(samples.astype(np.float64) ** 2)) if samples.size else 0.0


def erle_db(mic: np.ndarray, residual: np.ndarray) -> float | None:
    mic_e = energy(mic)
    res_e = energy(residual)
    if mic_e <= EPS:
        return None
    if res_e <= EPS:
        return float("inf")
    return 10.0 * float(np.log10(mic_e / res_e))


def best_projection(residual: np.ndarray, clean: np.ndarray, max_lag: int) -> dict[str, float]:
    """在 ±max_lag 内找最佳时移，给出"残差里还剩下多少用户语音"。

    - ``gain_db``：用户语音被放大/衰减了多少（0 dB = 完全保留）；
    - ``preserved_db``：残差中的用户语音能量 / 残差中**无法用用户语音解释**的能量的比值（越高越好）；
    - ``corr``：残差与用户语音的相关系数；
    - ``lag``：最佳时移（样本）。
    """

    best: dict[str, float] = {"gain_db": float("-inf"), "preserved_db": float("-inf"), "corr": 0.0, "lag": 0.0}
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            res = residual[lag:]
            ref = clean[: residual.size - lag] if lag else clean
        else:
            res = residual[:lag]
            ref = clean[-lag:]
        size = min(res.size, ref.size)
        if size < FRAME_SAMPLES:
            continue
        res = res[:size].astype(np.float64)
        ref = ref[:size].astype(np.float64)
        denom = float(np.dot(ref, ref))
        if denom <= EPS:
            continue
        coeff = float(np.dot(res, ref) / denom)
        explained = coeff * ref
        unexplained = res - explained
        gain_db = 20.0 * float(np.log10(max(abs(coeff), EPS)))
        unexplained_e = energy(unexplained.astype(np.float32))
        explained_e = energy(explained.astype(np.float32))
        preserved_db = float("inf") if unexplained_e <= EPS else 10.0 * float(np.log10(max(explained_e, EPS) / unexplained_e))
        res_std = float(np.std(res))
        ref_std = float(np.std(ref))
        corr = 0.0 if res_std <= EPS or ref_std <= EPS else float(np.corrcoef(res, ref)[0, 1])
        if preserved_db > best["preserved_db"] or (
            preserved_db == best["preserved_db"] and abs(coeff) > 10 ** (best["gain_db"] / 20.0)
        ):
            best = {
                "gain_db": gain_db,
                "preserved_db": preserved_db,
                "corr": corr,
                "lag": float(lag),
            }
    return best


# --------------------------------------------------------------------------- 引擎


def build_engine(name: str) -> Any:
    """按名字造一个与 `cancel(reference=, capture=)` 同构的消除器。"""

    if name == BACKEND_WEBRTC:
        return WebrtcEchoCanceller(config=WebrtcAecConfig(sample_rate=RATE, frame_duration_ms=FRAME_MS))
    return NlmsEchoCanceller(config=NlmsConfig(sample_rate=RATE, frame_duration_ms=FRAME_MS))


def run_scenario(
    engine: Any,
    *,
    mic: np.ndarray,
    far: np.ndarray,
    clean: np.ndarray,
    max_lag: int,
    segments: int = 3,
) -> dict[str, Any]:
    """逐 20 ms 帧喂消除器；返回残差与度量（消音帧同样喂，保持自适应连续）。"""

    frames = mic.size // FRAME_SAMPLES
    residual = np.empty(frames * FRAME_SAMPLES, dtype=np.float32)
    processed = 0
    for index in range(frames):
        start = index * FRAME_SAMPLES
        end = start + FRAME_SAMPLES
        result = engine.cancel(
            reference=np.asarray(far[start:end] * 32767.0, dtype=np.int16).tobytes(),
            capture=np.asarray(mic[start:end] * 32767.0, dtype=np.int16).tobytes(),
        )
        residual[start:end] = result.output
        processed += 1
    total = frames * FRAME_SAMPLES
    mic_used = mic[:total]
    far_used = far[:total]
    clean_used = clean[:total]
    metrics: dict[str, Any] = {
        "frames": frames,
        "processed": processed,
        "erle_db": erle_db(mic_used, residual),
        "mic_rms_db": 20.0 * float(np.log10(max(float(np.sqrt(energy(mic_used))), EPS))),
        "residual_rms_db": 20.0 * float(np.log10(max(float(np.sqrt(energy(residual))), EPS))),
        "clean_rms_db": 20.0 * float(np.log10(max(float(np.sqrt(energy(clean_used))), EPS))),
    }
    metrics.update(best_projection(residual, clean_used, max_lag))
    # 残差里"用户语音成分"折算回 int16 RMS：用于和项目的打断能量门（speech_threshold=600）对齐。
    speech_linear = 10.0 ** (metrics["gain_db"] / 20.0) if metrics["gain_db"] != float("-inf") else 0.0
    clean_rms_pcm16 = float(np.sqrt(energy(clean_used))) * 32768.0
    metrics["residual_speech_rms_pcm16"] = speech_linear * clean_rms_pcm16
    metrics["clean_speech_rms_pcm16"] = clean_rms_pcm16
    # 分段（默认 3 段）：看抑制/削语音是"收敛期瞬态"还是"稳态"。
    span = total // max(segments, 1)
    segment_metrics: list[dict[str, Any]] = []
    for index in range(segments):
        start = index * span
        end = total if index == segments - 1 else (index + 1) * span
        if end - start < FRAME_SAMPLES:
            continue
        piece = best_projection(residual[start:end], clean_used[start:end], max_lag)
        segment_metrics.append(
            {
                "index": index,
                "start_s": round(start / RATE, 3),
                "erle_db": erle_db(mic_used[start:end], residual[start:end]),
                "gain_db": piece["gain_db"],
                "corr": piece["corr"],
                "residual_speech_rms_pcm16": 10.0 ** (piece["gain_db"] / 20.0) * clean_rms_pcm16
                if piece["gain_db"] != float("-inf")
                else 0.0,
            }
        )
    metrics["segments"] = segment_metrics
    # 与项目打断能量门（默认 speech_threshold=600，int16 RMS 口径）的余量：负数=会被门挡掉。
    metrics["residual_speech_vs_gate_db"] = (
        20.0 * float(np.log10(max(metrics["residual_speech_rms_pcm16"], EPS) / BARGE_IN_GATE_DEFAULT))
    )
    if hasattr(engine, "suppression_evidence"):
        metrics["suppression_evidence"] = engine.suppression_evidence
    return metrics


def verdict(report: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    far_only = report["scenarios"][SCENARIO_FAR_ONLY]
    near_only = report["scenarios"][SCENARIO_NEAR_ONLY]
    double_talk = report["scenarios"][SCENARIO_DOUBLE_TALK]
    checks = {
        "far_only_erle": {
            "value": far_only["erle_db"],
            "threshold": args.min_erle_db,
            "pass": far_only["erle_db"] is not None and far_only["erle_db"] >= args.min_erle_db,
        },
        "near_only_speech_gain": {
            "value": near_only["gain_db"],
            "threshold": args.min_near_gain_db,
            "pass": near_only["gain_db"] >= args.min_near_gain_db,
        },
        "double_talk_speech_gain": {
            "value": double_talk["gain_db"],
            "threshold": args.min_double_talk_gain_db,
            "pass": double_talk["gain_db"] >= args.min_double_talk_gain_db,
        },
    }
    failed = [name for name, item in checks.items() if not item["pass"]]
    return {
        "decision": "go" if not failed else "no_go",
        "failed": failed,
        "checks": checks,
        "note": (
            "判定随'回声/用户语音比'变化：回声远低于用户时双讲衰减小（本机实测 1.6~5.9 dB），"
            "回声接近或超过用户时衰减大（6~20 dB）。本工具用的是合成回声路径，"
            "真机能否使用必须实测回声/用户比与噪声底。"
        ),
    }


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R4-B-2 阶段 0：双讲保持 go/no-go 探针（离线）")
    parser.add_argument("--near-wav", type=str, default=str(DEFAULT_WAV_DIR / "0.wav"), help="用户语音")
    parser.add_argument("--far-wav", type=str, default=str(DEFAULT_WAV_DIR / "1.wav"), help="助手播放语音")
    parser.add_argument("--engine", choices=(BACKEND_WEBRTC, BACKEND_NLMS), default=BACKEND_WEBRTC)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--speech-rms", type=float, default=0.03)
    parser.add_argument("--far-rms", type=float, default=0.09)
    parser.add_argument("--echo-delay-ms", type=int, default=40)
    parser.add_argument("--echo-gain", type=float, default=0.5)
    parser.add_argument("--echo-reflection-gain", type=float, default=0.15)
    parser.add_argument("--noise-rms", type=float, default=0.001)
    parser.add_argument("--max-lag", type=int, default=16)
    parser.add_argument("--segments", type=int, default=3, help="分段数（看抑制/削语音是收敛期还是稳态）")
    parser.add_argument("--min-erle-db", type=float, default=10.0)
    parser.add_argument("--min-near-gain-db", type=float, default=-3.0)
    parser.add_argument("--min-double-talk-gain-db", type=float, default=-6.0)
    parser.add_argument("--json", type=str, default="", help="可选：写入数值摘要路径")
    args = parser.parse_args(argv)

    near_path = Path(args.near_wav)
    far_path = Path(args.far_wav)
    if not near_path.exists() or not far_path.exists():
        print(f"找不到 wav：near={near_path} far={far_path}", file=sys.stderr)
        return 2

    length = int(args.seconds * RATE)
    rng = np.random.default_rng(13)
    clean = normalize_rms(tile_to(read_wav(near_path), length), args.speech_rms)
    far_speech = normalize_rms(tile_to(read_wav(far_path), length), args.far_rms)
    noise = (rng.standard_normal(length).astype(np.float32) * args.noise_rms).astype(np.float32)
    echo = synth_echo_path(
        far_speech,
        delay_ms=args.echo_delay_ms,
        gain=args.echo_gain,
        reflection_gain=args.echo_reflection_gain,
        lowpass=True,
    )
    silence = np.zeros(length, dtype=np.float32)

    report: dict[str, Any] = {
        "tool": "p10_r4b2_doubletalk_probe",
        "engine": args.engine,
        "sample_rate": RATE,
        "frame_ms": FRAME_MS,
        "seconds": args.seconds,
        "near_wav": near_path.name,
        "far_wav": far_path.name,
        "synthetic_echo_path": {
            "delay_ms": args.echo_delay_ms,
            "gain": args.echo_gain,
            "reflection_gain": args.echo_reflection_gain,
            "lowpass": True,
            "speech_rms": args.speech_rms,
            "far_rms": args.far_rms,
            "noise_rms": args.noise_rms,
            # 相对关系才是决定"削减多少"的变量（见 doubletalk findings 文档）。
            "echo_to_speech_db": 10.0
            * float(np.log10(max(energy(echo), EPS) / max(energy(clean), EPS))),
        },
        "barge_in_gate": {
            "energy_gate_pcm16": BARGE_IN_GATE_DEFAULT,
            "mic_noise_floor_max_pcm16": MIC_NOISE_FLOOR_MAX,
            "source": "auto_voice.speech_threshold / R2b-2 真机噪声底",
        },
        "note": "回声路径是合成的，只能判'算法是否吃掉用户语音'，不能替代真机 ERLE",
        "scenarios": {},
    }

    plan = {
        SCENARIO_FAR_ONLY: (silence + echo + noise, far_speech, silence),
        SCENARIO_NEAR_ONLY: (clean + noise, silence, clean),
        SCENARIO_DOUBLE_TALK: (clean + echo + noise, far_speech, clean),
    }
    for name in SCENARIOS:
        mic, far, clean_ref = plan[name]
        engine = build_engine(args.engine)
        if hasattr(engine, "available") and not engine.available:
            print(
                f"引擎不可用：{engine.available_reason}（本次数值不作为结论）",
                file=sys.stderr,
            )
            return 1
        metrics = run_scenario(
            engine, mic=mic, far=far, clean=clean_ref, max_lag=args.max_lag, segments=args.segments
        )
        report["scenarios"][name] = metrics

    report["verdict"] = verdict(report, args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
