"""P10 批次 R4-B-1：真 AEC（WebRTC APM/AEC3）**离线**基准工具（不需要设备、不落盘音频）。

用途：
1. 合成回声（延迟 + 增益 + 噪声）跑 `WebrtcEchoCanceller`，报告**可复核**的抑制量与
   `applied` 比例——用来验证"实现正确"，**不**用来证明本机能消掉回声（那必须真机）；
2. 实测**本机**每帧耗时与实时倍数（补上可行性核查里"本机 CPU 未实测"的空档）；
3. `--check-continuity`：验证"一次 20 ms 调用"与"两次 10 ms 调用"结果是否逐样本一致
   （绑定内部按 10 ms 切帧，等价性是调用契约的一部分）。

安全与隐私纪律：
- 全程只在内存里合成音频，**不打开任何设备、不落盘任何 wav/pcm**；
- 不联网、不读密钥、不导入安全链模块；
- 只有显式给 `--json <path>` 才写一份数值摘要（不含音频、不含文本）。
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio.aec.webrtc_backend import (  # noqa: E402
    WebrtcAecConfig,
    WebrtcEchoCanceller,
    webrtc_backend_available,
)

DEFAULT_FRAMES = 400


def build_echo_pair(
    *,
    frames: int,
    frame_samples: int,
    delay_ms: int = 40,
    gain: float = 0.5,
    noise_rms: float = 100.0,
    sample_rate: int = 16_000,
    seed: int = 7,
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """合成 (far, near)：``near = gain * far[滞后 delay_ms] + 白噪声``。"""

    rng = np.random.default_rng(seed)
    total = frames * frame_samples
    far_flat = (rng.standard_normal(total) * 3000.0).astype(np.float32)
    delay_samples = int(round(sample_rate * delay_ms / 1000.0))
    history = np.zeros(delay_samples, dtype=np.float32)
    extended = np.concatenate((history, far_flat))
    echo = extended[:total] * gain
    noise = (rng.standard_normal(total) * noise_rms).astype(np.float32)
    near_flat = echo + noise
    far_frames = [
        far_flat[i * frame_samples : (i + 1) * frame_samples].astype(np.int16)
        for i in range(frames)
    ]
    near_frames = [
        near_flat[i * frame_samples : (i + 1) * frame_samples].astype(np.int16)
        for i in range(frames)
    ]
    return far_frames, near_frames


def run_frames(
    canceller: WebrtcEchoCanceller, far_frames: list[np.ndarray], near_frames: list[np.ndarray]
) -> tuple[list[np.ndarray], list[float]]:
    """逐帧处理；返回 (输出帧, 每帧耗时毫秒)。"""

    outputs: list[np.ndarray] = []
    timings: list[float] = []
    for far_frame, near_frame in zip(far_frames, near_frames):
        started = time.perf_counter()
        result = canceller.cancel(
            reference=far_frame.astype(np.int16).tobytes(),
            capture=near_frame.astype(np.int16).tobytes(),
        )
        timings.append((time.perf_counter() - started) * 1000.0)
        outputs.append((np.clip(result.output, -1.0, 1.0) * 32767.0).astype(np.int16))
    return outputs, timings


def energy(values: list[np.ndarray]) -> float:
    if not values:
        return 0.0
    array = np.concatenate(values).astype(np.float32) / 32768.0
    return float(np.dot(array, array))


def erle_db(mic_energy: float, residual_energy: float) -> float | None:
    if mic_energy <= 0.0:
        return None
    if residual_energy <= 0.0:
        return float("inf")
    return float(10.0 * np.log10(mic_energy / residual_energy))


def check_continuity(
    *, frame_samples: int, sample_rate: int, delay_ms: int, gain: float, noise_rms: float, frames: int
) -> dict[str, Any]:
    """一次 20 ms 调用 vs 两次 10 ms 调用：输出是否逐样本一致。"""

    far_frames, near_frames = build_echo_pair(
        frames=frames,
        frame_samples=frame_samples,
        delay_ms=delay_ms,
        gain=gain,
        noise_rms=noise_rms,
        sample_rate=sample_rate,
    )
    config = WebrtcAecConfig(sample_rate=sample_rate, frame_duration_ms=20)
    single = WebrtcEchoCanceller(config=config)
    # 对照组必须按 10 ms 帧长构造（帧长是配置项，不是"同一实例换喂法"）。
    halves = WebrtcEchoCanceller(
        config=WebrtcAecConfig(sample_rate=sample_rate, frame_duration_ms=10)
    )

    single_outputs, _ = run_frames(single, far_frames, near_frames)

    half_outputs: list[np.ndarray] = []
    for far_frame, near_frame in zip(far_frames, near_frames):
        pieces = []
        for offset in (0, frame_samples // 2):
            result = halves.cancel(
                reference=far_frame[offset : offset + frame_samples // 2].tobytes(),
                capture=near_frame[offset : offset + frame_samples // 2].tobytes(),
            )
            pieces.append(
                (np.clip(result.output, -1.0, 1.0) * 32767.0).astype(np.int16)
            )
        half_outputs.append(np.concatenate(pieces))

    joined_single = np.concatenate(single_outputs).astype(np.int32)
    joined_half = np.concatenate(half_outputs).astype(np.int32)
    diff = np.abs(joined_single - joined_half)
    return {
        "identical": bool(np.array_equal(joined_single, joined_half)),
        "samples": int(diff.size),
        "differing_samples": int(np.count_nonzero(diff)),
        "max_abs_diff": int(diff.max()) if diff.size else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P10 R4-B-1 真 AEC（AEC3）离线基准")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--sample-rate", type=int, default=16_000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--delay-ms", type=int, default=40)
    parser.add_argument("--gain", type=float, default=0.5)
    parser.add_argument("--noise-rms", type=float, default=100.0)
    parser.add_argument("--stream-delay-ms", type=int, default=0)
    parser.add_argument("--erle-gate", type=float, default=6.0)
    parser.add_argument("--warmup-frames", type=int, default=0, help="计时前先跑的预热帧数")
    parser.add_argument("--check-continuity", action="store_true", help="校验 20ms 与 2×10ms 等价性")
    parser.add_argument("--json", type=str, default="", help="可选：把数值摘要写入该路径")
    args = parser.parse_args(argv)

    if args.frames <= 0:
        print("--frames 必须为正整数", file=sys.stderr)
        return 2

    config = WebrtcAecConfig(
        sample_rate=args.sample_rate,
        frame_duration_ms=args.frame_ms,
        stream_delay_ms=args.stream_delay_ms,
        erle_gate_db=args.erle_gate,
    )
    frame_samples = config.frame_samples
    canceller = WebrtcEchoCanceller(config=config)

    summary: dict[str, Any] = {
        "backend": "webrtc_aec3",
        "dependency_available": webrtc_backend_available(),
        "available": canceller.available,
        "available_reason": canceller.available_reason,
        "init_error": canceller.to_details()["init_error"],
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sample_rate": args.sample_rate,
        "frame_ms": args.frame_ms,
        "frame_samples": frame_samples,
        "frames": args.frames,
        "synthetic": {
            "delay_ms": args.delay_ms,
            "gain": args.gain,
            "noise_rms": args.noise_rms,
        },
    }

    far_frames, near_frames = build_echo_pair(
        frames=args.frames,
        frame_samples=frame_samples,
        delay_ms=args.delay_ms,
        gain=args.gain,
        noise_rms=args.noise_rms,
        sample_rate=args.sample_rate,
    )

    if canceller.available and args.warmup_frames > 0:
        warm_far, warm_near = build_echo_pair(
            frames=args.warmup_frames,
            frame_samples=frame_samples,
            delay_ms=args.delay_ms,
            gain=args.gain,
            noise_rms=args.noise_rms,
            sample_rate=args.sample_rate,
            seed=11,
        )
        run_frames(canceller, warm_far, warm_near)
        canceller.reset()

    outputs, timings = run_frames(canceller, far_frames, near_frames)

    mic_energy = energy(near_frames)
    residual_energy = energy(outputs)
    tail_start = max(0, int(args.frames * 0.75))
    tail_mic = energy(near_frames[tail_start:])
    tail_residual = energy(outputs[tail_start:])

    summary["result"] = {
        "frames_processed": canceller.frames_processed,
        "frames_seen": canceller.frames_seen,
        "backend_errors": canceller.backend_errors,
        "last_backend_error": canceller.to_details()["last_backend_error"],
        "cumulative_erle_db": erle_db(mic_energy, residual_energy),
        "tail_erle_db": erle_db(tail_mic, tail_residual),
        "erle_sufficient": canceller.erle_sufficient,
    }
    if timings:
        per_frame_ms = statistics.median(timings)
        summary["timing"] = {
            "median_ms": per_frame_ms,
            "p95_ms": sorted(timings)[min(len(timings) - 1, int(len(timings) * 0.95))],
            "max_ms": max(timings),
            "realtime_factor": (args.frame_ms / per_frame_ms) if per_frame_ms > 0 else None,
            "frames_timed": len(timings),
        }
    if args.check_continuity and canceller.available:
        summary["continuity"] = check_continuity(
            frame_samples=frame_samples,
            sample_rate=args.sample_rate,
            delay_ms=args.delay_ms,
            gain=args.gain,
            noise_rms=args.noise_rms,
            frames=min(args.frames, 200),
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
        )
    if not canceller.available:
        print(
            f"真 AEC 后端不可用：{canceller.available_reason}（本工具其余数值不作为结论）",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
