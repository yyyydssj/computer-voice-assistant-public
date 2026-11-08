"""P10 批次 R4-B-4：真机 **AEC3 双讲**决定性实验（需 ``--live-opt-in``；会出声 + 录音）。

要回答的问题（R4-B-2 阶段 1 的前置条件）：
> 在本机真实声学路径上，AEC3 能不能"消掉自己的回声"**并且**"不把用户语音压到能量门下"？

R4-B-3 已实测本机"回声/用户语音比 = −7.1 dB"（用户语音比回声高 7.1 dB，属**好档**），
因此本实验用**三段真机录制 + 离线分析**给出直接答案（不需要实时线程，可重复复盘）：

| 段 | 播放 | 用户 | 用途 |
|---|---|---|---|
| A | 静音 | 说话 4 s | 用户语音参考电平 |
| B | 真实 TTS 6 s | **保持安静** | 真机 ERLE + 残余回声是否低于能量门 |
| C | 真实 TTS 6 s | **同时说话** | 双讲后用户语音是否仍高于能量门 |

分析全部在内存里做（**默认不落盘任何音频**）：
- 麦克风（设备原生 48 kHz）→ 16 kHz；far（24 kHz TTS）→ 16 kHz，按录制时间戳对齐；
- 用**生产同款 waveOut 后端**播放，far 就是实际提交给设备的那段信号；
- AEC3 用 `WebrtcEchoCanceller`，`stream_delay_ms` 由 B 段互相关实测延迟给出，跳过前 0.8 s 收敛期；
- 残余回声在 B 段、用户语音在 C 段分别取 20 ms 帧的 p50/p95（int16 数值域，能量门 600 同口径）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.audio.aec.webrtc_backend import WebrtcAecConfig, WebrtcEchoCanceller  # noqa: E402
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

CAPTURE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = CAPTURE_RATE * FRAME_MS // 1000  # 320
BARGE_IN_ENERGY_GATE_PCM16 = 600.0
MIC_NOISE_FLOOR_MAX_PCM16 = 150.0
SKIP_SECONDS = 0.8
ECHO_SUPPRESSION_TARGET_DB = 6.0


def to_capture_rate(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == CAPTURE_RATE:
        return samples
    return _resample(samples, src_rate=rate, dst_rate=CAPTURE_RATE)


def frame_levels(samples: np.ndarray, *, rate: int = CAPTURE_RATE, skip_s: float = 0.0) -> np.ndarray:
    start = max(0, int(rate * skip_s))
    segment = samples[start:]
    count = segment.size // FRAME_SAMPLES
    if count == 0:
        return np.zeros(0, dtype=np.float64)
    reshaped = segment[: count * FRAME_SAMPLES].reshape(count, FRAME_SAMPLES)
    return np.sqrt(np.mean(reshaped.astype(np.float64) ** 2, axis=1)) * 32768.0


def percentile(values: np.ndarray, q: float) -> float:
    return float(np.percentile(values, q)) if values.size else 0.0


def measure_delay_samples(far16: np.ndarray, mic16: np.ndarray) -> tuple[int, float]:
    """B 段互相关找"播放→麦克风"延迟（样本/毫秒）；对齐失败返回 0。"""

    max_lag = int(0.6 * CAPTURE_RATE)
    length = min(far16.size, mic16.size)
    if length < CAPTURE_RATE:
        return 0, 0.0
    far = far16[:length].astype(np.float64)
    mic = mic16[:length].astype(np.float64)
    lags = np.arange(0, max_lag + 1)
    # 只需正延迟（播放先于采集）：用滑动的点积近似足够稳，避免长序列全量互相关。
    best_lag, best_score = 0, -1.0
    far_norm = math.sqrt(float(np.dot(far, far))) or 1.0
    step = 1
    for lag in lags[::step]:
        shifted = mic[lag:]
        segment = far[: shifted.size]
        if segment.size < CAPTURE_RATE // 2:
            break
        score = float(np.dot(segment, shifted)) / (
            far_norm * (math.sqrt(float(np.dot(shifted, shifted))) or 1.0)
        )
        if score > best_score:
            best_lag, best_score = int(lag), score
    return best_lag, best_lag / CAPTURE_RATE * 1000.0


def run_aec3(*, mic16: np.ndarray, far16: np.ndarray, stream_delay_ms: int) -> tuple[np.ndarray, dict[str, Any]]:
    """逐 20 ms 帧跑 AEC3；返回残差与后端事实。"""

    canceller = WebrtcEchoCanceller(
        config=WebrtcAecConfig(sample_rate=CAPTURE_RATE, frame_duration_ms=FRAME_MS, stream_delay_ms=stream_delay_ms)
    )
    frames = min(mic16.size, far16.size) // FRAME_SAMPLES
    residual = np.zeros(frames * FRAME_SAMPLES, dtype=np.float32)
    for index in range(frames):
        start = index * FRAME_SAMPLES
        end = start + FRAME_SAMPLES
        result = canceller.cancel(
            reference=np.asarray(far16[start:end] * 32767.0, dtype=np.int16).tobytes(),
            capture=np.asarray(mic16[start:end] * 32767.0, dtype=np.int16).tobytes(),
        )
        residual[start:end] = result.output
    return residual, canceller.to_details()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P10 R4-B-4 真机 AEC3 双讲实验（需 --live-opt-in）")
    parser.add_argument("--live-opt-in", action="store_true", help="显式开启真机（会放音并录音）")
    parser.add_argument("--check-only", action="store_true", help="只做前置检查，不打开设备流")
    parser.add_argument("--wav", default=str(DEFAULT_WAV))
    parser.add_argument("--repeat", type=int, default=3, help="播放素材重复遍数（默认 3 ≈ 6 s）")
    parser.add_argument("--user-seconds", type=float, default=4.0, help="A 段用户说话时长")
    parser.add_argument("--mic-device", type=int, default=None)
    parser.add_argument("--json", default="", help="可选：数值摘要写入路径")
    parser.add_argument("--save-wav", action="store_true", help="把三段录音落盘（默认不落盘；写了就是本机音频落盘）")
    parser.add_argument("--save-dir", default="", help="配合 --save-wav 的输出目录")
    parser.add_argument(
        "--echo-only",
        action="store_true",
        help="只做「放音 + 保持安静」的 B 段（复核真机 ERLE，**不需要用户说话**）",
    )
    parser.add_argument(
        "--delay-sweep",
        action="store_true",
        help="对 stream_delay_ms 扫 0~500 ms 各跑一次 AEC3，确认 ERLE 低不是延迟提示估偏造成的",
    )
    args = parser.parse_args(argv)

    safe_write("[r4b4] 真机 AEC3 双讲实验；音频只在内存分析，默认不落盘\n")
    if not args.live_opt_in or args.check_only:
        safe_write("[r4b4] 只做前置检查（未提供 --live-opt-in 或指定了 --check-only）\n")
        _print_preflight(_preflight())
        safe_write("[r4b4] 加 --live-opt-in 才会真正放音与录音\n")
        return 0

    import pyaudiowpatch  # noqa: PLC0415

    preflight = _preflight()
    wasapi = preflight.get("wasapi") or {}
    mic_device = _pick_input(wasapi.get("inputs", []), args.mic_device)
    if mic_device is None:
        safe_write("[r4b4] 找不到可用的麦克风端点，终止\n")
        _print_preflight(preflight)
        return 1

    from tools.p10_aec_probe import _Recorder  # noqa: PLC0415

    wav_path = Path(args.wav)
    if not wav_path.exists():
        safe_write(f"[r4b4] 找不到播放素材：{wav_path}\n")
        return 2
    speech, speech_rate = read_wav_mono(wav_path)
    if speech_rate != PLAYBACK_RATE:
        speech = _resample(speech, src_rate=speech_rate, dst_rate=PLAYBACK_RATE)
    play_signal = np.tile(speech, max(1, int(args.repeat)))
    play_seconds = play_signal.size / PLAYBACK_RATE
    cue = build_cue()
    mic_rate = int(mic_device["rate"])

    report: dict[str, Any] = {
        "tool": "p10_r4b4_realtime_doubletalk_probe",
        "mic": {"name": mic_device.get("name"), "rate": mic_rate},
        "playback": {
            "wav": wav_path.name,
            "repeat": int(args.repeat),
            "seconds": round(play_seconds, 3),
            "backend": WaveOutPCMStreamBackend.backend_name,
        },
        "energy_gate_pcm16": BARGE_IN_ENERGY_GATE_PCM16,
        "skip_seconds": SKIP_SECONDS,
        "note": "数值只在当前系统音量 + 当前坐姿/音量下成立；三段录制与 AEC3 分析都在内存里完成",
    }
    captured: dict[str, np.ndarray] = {}
    audio = pyaudiowpatch.PyAudio()
    try:
        # ---- A 段：用户语音参考（--echo-only 时跳过，不需要用户配合）
        if not args.echo_only:
            safe_write("[r4b4] A 段：三声提示音后请说 4 秒话（不要等放音）\n")
            _play(cue, backend_factory=WaveOutPCMStreamBackend)
            recorder = _Recorder(audio, mic_device)
            recorder.start()
            time.sleep(max(1.0, args.user_seconds))
            captured["user_only"] = recorder.stop()

        # ---- B 段：只放音，用户保持安静
        safe_write("[r4b4] B 段：开始放音 6 秒，请保持安静\n")
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

        # ---- C 段：放音 + 用户同时说话（--echo-only 时跳过）
        dt_mic: np.ndarray | None = None
        if not args.echo_only:
            safe_write("[r4b4] C 段：三声提示音后一边放音一边说话 6 秒\n")
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
        out_dir = Path(args.save_dir) if args.save_dir else REPO_ROOT / "implementation_plan" / "checkpoints" / "artifacts" / "r4b4"
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, samples in captured.items():
            with wave.open(str(out_dir / f"{name}.wav"), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(mic_rate)
                handle.writeframes(np.asarray(np.clip(samples, -1.0, 1.0) * 32767.0, dtype="<i2").tobytes())
        for name, samples in (("echo_mic", echo_mic), ("double_talk_mic", dt_mic)):
            if samples is None:
                continue
            with wave.open(str(out_dir / f"{name}.wav"), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(mic_rate)
                handle.writeframes(np.asarray(np.clip(samples, -1.0, 1.0) * 32767.0, dtype="<i2").tobytes())
        report["saved_wav_dir"] = str(out_dir)

    # ---------------------------------------------------------------- 分析（纯内存）
    echo_mic16 = to_capture_rate(echo_mic, mic_rate)
    user_mic16 = (
        to_capture_rate(captured["user_only"], mic_rate) if captured.get("user_only") is not None else None
    )
    dt_mic16 = to_capture_rate(dt_mic, mic_rate) if dt_mic is not None else None

    def far_into(length: int, *, record_started_at: float, play_started_at: float) -> np.ndarray:
        """把 far 按"播放开始相对录制开始"的偏移放进与 mic 等长的时间轴。"""

        far16 = _resample(play_signal, src_rate=PLAYBACK_RATE, dst_rate=CAPTURE_RATE)
        offset = max(0, int((play_started_at - record_started_at) * CAPTURE_RATE))
        out = np.zeros(length, dtype=np.float32)
        end = min(length, offset + far16.size)
        if end > offset:
            out[offset:end] = far16[: end - offset]
        return out

    far_b = far_into(
        echo_mic16.size,
        record_started_at=report["phase_b_wall"]["record_started_at"],
        play_started_at=report["phase_b_wall"]["play_started_at"],
    )

    delay_samples, delay_ms = measure_delay_samples(far_b, echo_mic16)
    report["delay"] = {"samples": delay_samples, "ms": round(delay_ms, 2)}

    residual_b, backend = run_aec3(mic16=echo_mic16, far16=far_b, stream_delay_ms=int(delay_ms))
    mic_b_levels = frame_levels(echo_mic16, skip_s=SKIP_SECONDS)
    res_b_levels = frame_levels(residual_b, skip_s=SKIP_SECONDS)
    echo_p50 = percentile(mic_b_levels, 50)
    res_echo_p50 = percentile(res_b_levels, 50)

    report["backend"] = backend
    metrics: dict[str, Any] = {
        "echo_mic_p50": echo_p50,
        "echo_residual_p50": res_echo_p50,
        "real_erle_db": (
            20.0 * math.log10(echo_p50 / res_echo_p50) if echo_p50 > 0 and res_echo_p50 > 0 else None
        ),
        "residual_echo_vs_gate_db": (
            20.0 * math.log10(res_echo_p50 / BARGE_IN_ENERGY_GATE_PCM16) if res_echo_p50 > 0 else None
        ),
        "raw_echo_frames_over_gate": int(np.count_nonzero(mic_b_levels > BARGE_IN_ENERGY_GATE_PCM16)),
        "residual_echo_frames_over_gate": int(np.count_nonzero(res_b_levels > BARGE_IN_ENERGY_GATE_PCM16)),
        "echo_frames_total": int(mic_b_levels.size),
    }

    user_p50 = 0.0
    if user_mic16 is not None:
        user_levels = frame_levels(user_mic16, skip_s=0.2)
        noise_p50 = percentile(user_levels, 5) if user_levels.size else 0.0
        speech_mask = user_levels > max(noise_p50 * 2.0, MIC_NOISE_FLOOR_MAX_PCM16)
        user_speech = user_levels[speech_mask]
        user_p50 = percentile(user_speech, 50)
        metrics["user_speech_p50"] = user_p50
        metrics["user_speech_frames"] = int(user_speech.size)

    if dt_mic16 is not None:
        far_c = far_into(
            dt_mic16.size,
            record_started_at=report["phase_c_wall"]["record_started_at"],
            play_started_at=report["phase_c_wall"]["play_started_at"],
        )
        residual_c, _ = run_aec3(mic16=dt_mic16, far16=far_c, stream_delay_ms=int(delay_ms))
        mic_c_levels = frame_levels(dt_mic16, skip_s=SKIP_SECONDS)
        res_c_levels = frame_levels(residual_c, skip_s=SKIP_SECONDS)
        dt_raw_p50 = percentile(mic_c_levels, 50)
        dt_res_p50 = percentile(res_c_levels, 50)
        # 双讲后用户语音的估计：用能量相减去掉残余回声地板（p50 口径，保守）。
        preserved = math.sqrt(max(dt_res_p50**2 - res_echo_p50**2, 0.0))
        metrics.update(
            {
                "double_talk_raw_p50": dt_raw_p50,
                "double_talk_residual_p50": dt_res_p50,
                "preserved_user_p50": preserved,
                "preserved_user_gain_db": (
                    20.0 * math.log10(preserved / user_p50) if preserved > 0 and user_p50 > 0 else None
                ),
                "preserved_user_vs_gate_db": (
                    20.0 * math.log10(preserved / BARGE_IN_ENERGY_GATE_PCM16) if preserved > 0 else None
                ),
                "double_talk_frames_over_gate_after_aec": int(
                    np.count_nonzero(res_c_levels > BARGE_IN_ENERGY_GATE_PCM16)
                ),
            }
        )

    # 延迟扫描：确认"真机 ERLE 只有几 dB"不是我们把 stream_delay_ms 估偏造成的。
    if args.delay_sweep:
        sweep: list[dict[str, Any]] = []
        for candidate in range(0, 501, 50):
            if candidate != int(delay_ms):
                residual, _ = run_aec3(mic16=echo_mic16, far16=far_b, stream_delay_ms=candidate)
                levels = frame_levels(residual, skip_s=SKIP_SECONDS)
            else:
                levels = res_b_levels
            value = percentile(levels, 50)
            sweep.append(
                {
                    "stream_delay_ms": candidate,
                    "residual_p50": value,
                    "erle_db": (
                        20.0 * math.log10(echo_p50 / value) if echo_p50 > 0 and value > 0 else None
                    ),
                    "frames_over_gate": int(np.count_nonzero(levels > BARGE_IN_ENERGY_GATE_PCM16)),
                }
            )
        best = max(
            (item for item in sweep if item["erle_db"] is not None),
            key=lambda item: item["erle_db"],
            default=None,
        )
        report["delay_sweep"] = {"candidates": sweep, "best": best}
        metrics["best_sweep_erle_db"] = None if best is None else best["erle_db"]
        metrics["best_sweep_delay_ms"] = None if best is None else best["stream_delay_ms"]

    report["metrics"] = metrics

    erle = metrics["real_erle_db"]
    best_erle = metrics.get("best_sweep_erle_db") if args.delay_sweep else None
    residual_frames_over_gate = metrics["residual_echo_frames_over_gate"]
    echo_ok = (best_erle if best_erle is not None else erle)
    echo_ok = echo_ok is not None and echo_ok >= ECHO_SUPPRESSION_TARGET_DB
    echo_clean_ok = residual_frames_over_gate == 0
    if args.echo_only:
        # 只放音复核：判定只看"回声是否被压到门下"。
        if echo_ok and echo_clean_ok:
            decision, detail = "go", "回声被压到能量门内（B 段无超门帧）"
        else:
            failed = []
            if not echo_ok:
                failed.append(f"回声抑制不足（ERLE {echo_ok if echo_ok is not None else 'n/a'} dB）")
            if not echo_clean_ok:
                failed.append(f"残差仍有 {residual_frames_over_gate} 帧超门")
            decision, detail = "no_go", "未通过：" + "、".join(failed)
    else:
        gain = metrics.get("preserved_user_gain_db")
        preserved = metrics.get("preserved_user_p50", 0.0)
        user_ok = gain is not None and gain >= -10.0  # 不是"完全不动"，而是"没被压没"
        preserved_ok = preserved >= BARGE_IN_ENERGY_GATE_PCM16
        if metrics.get("user_speech_frames", 0) == 0:
            decision, detail = "invalid", "A 段没有测到用户语音；请重跑并在三声提示音后说话"
        elif echo_ok and echo_clean_ok and user_ok and preserved_ok:
            decision, detail = "go", "回声被压到门内、用户语音双讲后仍高于能量门"
        else:
            failed = [
                name
                for name, ok in (
                    ("回声抑制不足", echo_ok),
                    ("残差仍有超门帧", echo_clean_ok),
                    ("用户语音被压过头", user_ok),
                    ("双讲后用户语音低于能量门", preserved_ok),
                )
                if not ok
            ]
            decision, detail = "no_go", "未通过：" + "、".join(failed)
    report["verdict"] = {"decision": decision, "detail": detail}

    lines = [
        "=" * 72,
        "[r4b4] 真机 AEC3 实验（数值摘要；默认未落盘音频）",
        "=" * 72,
        f"麦克风：{report['mic']['name']} @ {mic_rate} Hz；素材：{wav_path.name} ×{args.repeat}（{play_seconds:.1f}s）",
        f"实测回声延迟：{delay_ms:.1f} ms（{delay_samples} 样本）",
        f"B 段回声：麦克风 p50={echo_p50:.0f} → AEC3 残差 p50={res_echo_p50:.0f}"
        + (f"（真机 ERLE {erle:.1f} dB）" if erle is not None else "（ERLE n/a）"),
        f"B 段残差超门帧数：{metrics['residual_echo_frames_over_gate']} / {metrics['echo_frames_total']}"
        f"（未处理时 {metrics['raw_echo_frames_over_gate']}）",
    ]
    if args.delay_sweep and report.get("delay_sweep", {}).get("best"):
        best = report["delay_sweep"]["best"]
        lines.append(
            f"延迟扫描最佳：stream_delay_ms={best['stream_delay_ms']} → ERLE {best['erle_db']:.1f} dB、"
            f"超门帧 {best['frames_over_gate']}"
        )
    if "user_speech_p50" in metrics:
        lines.append(f"A 段用户语音 p50：{metrics['user_speech_p50']:.0f}（有效帧 {metrics['user_speech_frames']}）")
    if "double_talk_raw_p50" in metrics:
        lines.append(
            f"C 段双讲：麦克风 p50={metrics['double_talk_raw_p50']:.0f} → AEC3 残差 p50={metrics['double_talk_residual_p50']:.0f}"
        )
        lines.append(
            f"双讲后用户语音估计：{metrics['preserved_user_p50']:.0f}"
            + (
                f"（相对原始用户语音 {metrics['preserved_user_gain_db']:.1f} dB）"
                if metrics.get("preserved_user_gain_db") is not None
                else ""
            )
        )
    lines.extend([f"判定：{decision} —— {detail}", "=" * 72])
    safe_write("\n".join(lines) + "\n")
    safe_write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
