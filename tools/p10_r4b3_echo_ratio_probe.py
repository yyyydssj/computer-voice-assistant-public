"""P10 批次 R4-B-3：真机量测"回声 / 用户语音比"（需显式 ``--live-opt-in``；会出声 + 录音）。

要回答的问题（R4-B-2 阶段 0 留下的唯一未知量）：
> 本机实际使用时，**扬声器回声在麦克风上的电平** 与 **用户正常说话的麦克风电平** 相差多少？

阶段 0 已证明（合成回声路径）：AEC3 对用户语音的削减**随这个比值变化**——回声远低于用户时只削
1.6~5.9 dB，回声接近/高于用户时削 5.5~20.5 dB。因此这个比值决定本机到底落在哪一档。

三阶段（全部只在内存里算电平，**不落盘任何音频、不上传**）：
1. **噪声底**：只录麦 1.5 s → p50/max；
2. **回声段**：用**生产同款 waveOut 后端**播放本机真实 TTS wav（默认 24 kHz 单声道 2 s，重复两遍），
   同时录麦 → 回声电平 p50/p95 + ERL（播放→麦克风回损）；
3. **用户语音段**：先放**三声提示音**，再录 8 s → 用户语音电平 p50/p95（只统计噪声底之上的帧）。

输出：数值 + 档位判定 + **阶段 0 的预期双讲削减区间**（区间来自合成实验，**不是**本机承诺）。
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

from app.tts.waveout_pcm import WaveOutPCMStreamBackend  # noqa: E402
from tools.p10_aec_probe import (  # noqa: E402
    PLAYBACK_RATE,
    _pick_input,
    _play,
    _preflight,
    _print_preflight,
    _rms,
)

DEFAULT_WAV = (
    REPO_ROOT
    / "implementation_plan"
    / "checkpoints"
    / "artifacts"
    / "2026-09-28-P7-local-voicestudio"
    / "eff172cb.wav"
)
BARGE_IN_ENERGY_GATE_PCM16 = 600.0
MIC_NOISE_FLOOR_MAX_PCM16 = 150.0
FRAME_MS = 20
CUE_FREQUENCY_HZ = 880.0
CUE_TONE_SECONDS = 0.15
CUE_GAP_SECONDS = 0.12
CUE_AMPLITUDE = 0.25

# 阶段 0 的实测区间（合成回声路径）：回声/用户语音比 → 双讲时用户语音增益区间。
STAGE0_BANDS = (
    (3.0, "回声 ≥ 用户", -20.5, -5.5),
    (-3.0, "中间档", -12.8, -5.1),
    (-999.0, "用户 ≫ 回声", -5.9, -1.6),
)


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """读单声道 PCM16 wav → (float32 [-1,1), rate)；格式不符直接报错，不猜。"""

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if channels != 1 or width != 2:
        raise SystemExit(f"{path.name}: 需要单声道 PCM16，实际 {channels}ch/{width * 8}bit")
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0, rate


def build_cue() -> np.ndarray:
    """三声短提示音（同一路信号，含间隔静音）。"""

    tone = (
        np.sin(2 * np.pi * CUE_FREQUENCY_HZ * np.arange(int(PLAYBACK_RATE * CUE_TONE_SECONDS)) / PLAYBACK_RATE)
        * CUE_AMPLITUDE
    ).astype(np.float32)
    gap = np.zeros(int(PLAYBACK_RATE * CUE_GAP_SECONDS), dtype=np.float32)
    parts: list[np.ndarray] = []
    for index in range(3):
        parts.append(tone)
        if index < 2:
            parts.append(gap)
    return np.concatenate(parts)


def frame_levels(samples: np.ndarray, *, rate: int, start_s: float, end_s: float) -> np.ndarray:
    """取 ``[start_s, end_s)`` 时间窗内每 20 ms 帧的 RMS（int16 数值域）。"""

    first = max(0, int(rate * start_s))
    last = min(samples.size, int(rate * end_s))
    if last <= first:
        return np.zeros(0, dtype=np.float64)
    segment = samples[first:last]
    frame = max(1, int(rate * FRAME_MS / 1000))
    count = segment.size // frame
    if count == 0:
        return np.zeros(0, dtype=np.float64)
    reshaped = segment[: count * frame].reshape(count, frame)
    return np.sqrt(np.mean(reshaped.astype(np.float64) ** 2, axis=1)) * 32768.0


def percentile(values: np.ndarray, q: float) -> float | None:
    if values.size == 0:
        return None
    return float(np.percentile(values, q))


def safe_write(text: str) -> None:
    """把文本写进控制台；编码不可表示时按 ``replace`` 重试，**绝不能因编码崩掉工具**。

    真机教训（P10 R4-A：GBK 控制台遇到不可表示字符把整个免按键会话打崩；本工具在
    R4-B-3 首次真机运行时，同样在打印含 ``≫`` 的档位标签处抛 ``UnicodeEncodeError``）。
    """

    stream = sys.stdout
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
    stream.flush()


def human(report: dict[str, Any]) -> None:
    lines: list[str] = ["=" * 72, "[r4b3] 真机：回声 / 用户语音比（数值摘要；音频未落盘）", "=" * 72]
    echo = report["echo"]
    user = report["user"]
    noise = report["noise"]
    lines.append(f"麦克风：{report['mic']['name']} @ {report['mic']['rate']} Hz")
    lines.append(
        f"播放素材：{report['playback']['wav']}（{report['playback']['seconds']}s × {report['playback']['repeat']}）"
    )
    lines.append(
        f"噪声底（1.5s）：p50={noise['p50']:.0f} / max={noise['max']:.0f}"
        f"（本机历史 max≈{MIC_NOISE_FLOOR_MAX_PCM16:.0f}）"
    )
    lines.append(f"回声段（{echo['window_seconds']:.2f}s）：p50={echo['p50']:.0f} / p95={echo['p95']:.0f}")
    lines.append(
        f"用户语音段（{user['window_seconds']:.2f}s）：p50={user['p50']:.0f} / p95={user['p95']:.0f} / 有效帧={user['speech_frames']}"
    )
    lines.append(
        f"ERL（播放→麦克风回损）：{report['erl_db']:.1f} dB" if report["erl_db"] is not None else "ERL：n/a"
    )
    lines.append(
        f"回声 / 用户语音比：{report['echo_to_user_db']:.1f} dB"
        if report["echo_to_user_db"] is not None
        else "回声 / 用户语音比：n/a"
    )
    lines.append(
        f"用户语音相对能量门 600：{report['user_vs_gate_db']:.1f} dB"
        if report["user_vs_gate_db"] is not None
        else "用户语音相对能量门：n/a"
    )
    lines.append(
        f"回声相对能量门 600：{report['echo_vs_gate_db']:.1f} dB"
        if report["echo_vs_gate_db"] is not None
        else "回声相对能量门：n/a"
    )
    verdict = report["verdict"]
    lines.append(f"档位判定：{verdict['band']}（{verdict['decision']}）")
    if verdict["expected_double_talk_db"] is not None:
        low, high = verdict["expected_double_talk_db"]
        lines.append(f"阶段 0 预期双讲削减区间：{low:.1f} ~ {high:.1f} dB（合成实验区间，非本机承诺）")
    lines.append(f"说明：{verdict['note']}")
    lines.append("=" * 72)
    safe_write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P10 R4-B-3 真机回声/用户语音比（需 --live-opt-in）")
    parser.add_argument("--live-opt-in", action="store_true", help="显式开启真机（会放音并录音）")
    parser.add_argument("--check-only", action="store_true", help="只做前置检查，不打开设备流")
    parser.add_argument("--wav", default=str(DEFAULT_WAV), help="播放素材（单声道 PCM16）")
    parser.add_argument("--repeat", type=int, default=2, help="播放素材重复遍数")
    parser.add_argument("--gain", type=float, default=1.0, help="播放增益（1.0 = 原样）")
    parser.add_argument("--mic-device", type=int, default=None, help="麦克风设备索引（默认自动选）")
    parser.add_argument("--noise-seconds", type=float, default=1.5)
    parser.add_argument("--user-seconds", type=float, default=8.0)
    parser.add_argument("--json", default="", help="可选：把数值摘要写入该路径")
    args = parser.parse_args(argv)

    print("[r4b3] 真机量测；音频只在内存里算电平，产物只有数值摘要（不含音频/文本）")
    if not args.live_opt_in or args.check_only:
        print("[r4b3] 只做前置检查（未提供 --live-opt-in 或指定了 --check-only）。")
        _print_preflight(_preflight())
        print("[r4b3] 加 --live-opt-in 才会真正放音与录音（会占用扬声器与麦克风）。")
        return 0

    import pyaudiowpatch  # noqa: PLC0415 - 真机路径才需要

    preflight = _preflight()
    wasapi = preflight.get("wasapi") or {}
    mic_device = _pick_input(wasapi.get("inputs", []), args.mic_device)
    if mic_device is None:
        print("[r4b3] 找不到可用的麦克风端点，终止。")
        _print_preflight(preflight)
        return 1

    from tools.p10_aec_probe import _Recorder  # noqa: PLC0415 - 真机路径才需要

    wav_path = Path(args.wav)
    if not wav_path.exists():
        print(f"[r4b3] 找不到播放素材：{wav_path}")
        return 2
    audio = pyaudiowpatch.PyAudio()
    speech, speech_rate = read_wav_mono(wav_path)
    speech = speech * float(args.gain)
    if speech_rate != PLAYBACK_RATE:
        from tools.p10_aec_probe import _resample  # noqa: PLC0415

        speech = _resample(speech, src_rate=speech_rate, dst_rate=PLAYBACK_RATE)
    play_signal = np.tile(speech, max(1, int(args.repeat)))
    play_seconds = play_signal.size / PLAYBACK_RATE

    report: dict[str, Any] = {
        "tool": "p10_r4b3_echo_ratio_probe",
        "mic": {"name": mic_device.get("name"), "rate": int(mic_device["rate"])},
        "playback": {
            "wav": wav_path.name,
            "seconds": round(speech.size / PLAYBACK_RATE, 3),
            "repeat": int(args.repeat),
            "gain": float(args.gain),
            "total_seconds": round(play_seconds, 3),
            "backend": WaveOutPCMStreamBackend.backend_name,
        },
        "barge_in_energy_gate_pcm16": BARGE_IN_ENERGY_GATE_PCM16,
        "note": "数值只在当前系统音量 + 当前坐姿/音量下成立；素材是离线 TTS wav，不是实时 VoiceStudio 输出",
    }

    try:
        # ---- 1) 噪声底
        recorder = _Recorder(audio, mic_device)
        recorder.start()
        time.sleep(max(0.5, args.noise_seconds))
        noise_raw = recorder.stop()
        noise_levels = frame_levels(
            noise_raw, rate=recorder.rate, start_s=0.15, end_s=noise_raw.size / max(recorder.rate, 1)
        )
        report["noise"] = {
            "p50": percentile(noise_levels, 50) or 0.0,
            "max": float(noise_levels.max()) if noise_levels.size else 0.0,
            "frames": int(noise_levels.size),
        }

        # ---- 2) 回声段：生产同款 waveOut 播放 + 录麦
        recorder = _Recorder(audio, mic_device)
        recorder.start()
        time.sleep(0.3)
        play_started_at = time.monotonic()
        play_facts = _play(play_signal, backend_factory=WaveOutPCMStreamBackend)
        play_finished_at = time.monotonic()
        time.sleep(0.25)
        echo_raw = recorder.stop()
        rec_started_at = recorder.started_at
        window_start = max(0.0, (play_started_at - rec_started_at) + 0.25)
        window_end = (play_finished_at - rec_started_at) - 0.05
        echo_levels = frame_levels(echo_raw, rate=recorder.rate, start_s=window_start, end_s=window_end)
        report["echo"] = {
            "p50": percentile(echo_levels, 50) or 0.0,
            "p95": percentile(echo_levels, 95) or 0.0,
            "frames": int(echo_levels.size),
            "window_seconds": round(max(0.0, window_end - window_start), 3),
            "play_facts": play_facts,
        }
        playback_rms_pcm16 = _rms(play_signal) * 32768.0  # noqa: SLF001 - 与回损口径一致
        report["playback_rms_pcm16"] = playback_rms_pcm16
        report["erl_db"] = (
            20.0 * math.log10(playback_rms_pcm16 / report["echo"]["p50"])
            if report["echo"]["p50"] > 0.0
            else None
        )

        # ---- 3) 用户语音段：三声提示音 → 8 秒窗口
        print("[r4b3] 即将播放三声提示音；听到后请用平时音量说话。")
        _play(build_cue(), backend_factory=WaveOutPCMStreamBackend)
        recorder = _Recorder(audio, mic_device)
        recorder.start()
        time.sleep(max(2.0, args.user_seconds))
        user_raw = recorder.stop()
        user_levels = frame_levels(
            user_raw,
            rate=recorder.rate,
            start_s=0.0,
            end_s=user_raw.size / max(recorder.rate, 1),
        )
        noise_p50 = report["noise"]["p50"]
        speech_mask = user_levels > max(noise_p50 * 2.0, MIC_NOISE_FLOOR_MAX_PCM16)
        speech_levels = user_levels[speech_mask]
        report["user"] = {
            "p50": percentile(speech_levels, 50) if speech_levels.size else 0.0,
            "p95": percentile(speech_levels, 95) if speech_levels.size else 0.0,
            "frames": int(user_levels.size),
            "speech_frames": int(speech_levels.size),
            "window_seconds": round(user_raw.size / max(recorder.rate, 1), 3),
        }
    finally:
        audio.terminate()

    user_p50 = report["user"]["p50"] or 0.0
    echo_p50 = report["echo"]["p50"] or 0.0
    report["echo_to_user_db"] = (
        20.0 * math.log10(echo_p50 / user_p50) if echo_p50 > 0.0 and user_p50 > 0.0 else None
    )
    report["user_vs_gate_db"] = (
        20.0 * math.log10(user_p50 / BARGE_IN_ENERGY_GATE_PCM16) if user_p50 > 0.0 else None
    )
    report["echo_vs_gate_db"] = (
        20.0 * math.log10(echo_p50 / BARGE_IN_ENERGY_GATE_PCM16) if echo_p50 > 0.0 else None
    )

    ratio = report["echo_to_user_db"]
    if report["user"]["speech_frames"] == 0 or user_p50 <= max(report["noise"]["p50"] * 2.0, 1.0):
        verdict = {
            "band": "无效（未测到用户语音）",
            "decision": "invalid",
            "expected_double_talk_db": None,
            "note": "没有测到高于噪声底的用户语音帧；请重跑，并在听到三声提示音后按平时音量说话",
        }
    elif ratio is None:
        verdict = {
            "band": "无效（回声为 0）",
            "decision": "invalid",
            "expected_double_talk_db": None,
            "note": "回声段没有测到电平；请检查系统音量与输出设备",
        }
    else:
        band, label, low, high = next(
            (item for item in STAGE0_BANDS if ratio >= item[0]), STAGE0_BANDS[-1]
        )
        verdict = {
            "band": label,
            "decision": "measured",
            "echo_to_user_db": ratio,
            "expected_double_talk_db": [low, high],
            "note": (
                "区间来自阶段 0 的合成回声实验（research/2026-10-03-P10-R4B-2-doubletalk-findings.md），"
                "只作预期参考；本机真实双讲削减必须真机复测"
            ),
        }
    report["verdict"] = verdict

    human(report)
    safe_write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
