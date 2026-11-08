r"""P9 麦克风电平标定（需要 ``--live-opt-in``）。

用途：在真机试听前量出**本机环境噪声**与**正常说话**的 PCM16 帧 RMS 分布，用来给
``tools/p9_auto_voice_runner.py --speech-threshold`` 选出有依据的阈值。不做任何声学
判断（不声称"这就是语音"），只报可观测统计量。

硬边界：

- 没有 ``--live-opt-in`` 立即退出码 2，**不导入** sounddevice、不打开设备；
- 原始音频**不落盘、不保存、不打印**；只输出每帧 RMS 的分位数与计数；
- 不联网、不读 ``.env``、不创建计划/授权/执行、不操作电脑。

用法：

```powershell
# 第 1 段：安静环境噪声（6 秒，请不要说话）
.\.venv\Scripts\python.exe tools\p9_mic_level_calibration.py --live-opt-in --seconds 6 --label quiet

# 第 2 段：正常说话（6 秒，请自然说话）
.\.venv\Scripts\python.exe tools\p9_mic_level_calibration.py --live-opt-in --seconds 6 --label speech
```

退出码：0 完成 / 2 未 opt-in / 3 前置失败（缺依赖、无设备或采集失败）。
"""
from __future__ import annotations

import argparse
import array
import math
import statistics
import sys
from collections.abc import Sequence

EXIT_OK = 0
EXIT_NOT_OPTED_IN = 2
EXIT_PREFLIGHT_FAILED = 3

FRAME_MS = 20
SAMPLE_RATE = 16000
CHANNELS = 1


def frame_rms(payload: bytes) -> float:
    """单帧 PCM16 little-endian 的 RMS 幅值；与自动收音的能量门控同一口径。"""

    usable = len(payload) - (len(payload) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(payload[:usable])
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples))


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in（标定会打开本机麦克风）", file=sys.stderr)
        return EXIT_NOT_OPTED_IN

    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001 - 依赖缺失按前置失败
        print(f"缺少音频采集依赖 sounddevice：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    try:
        device = args.device_id
        if device is None:
            default = getattr(sd.default, "device", None)
            candidate = getattr(default, "input", None)
            if candidate is None:
                try:
                    candidate = default[0]  # type: ignore[index]
                except Exception:  # noqa: BLE001 - 无法确认默认设备
                    candidate = None
            device = candidate
        if device is None:
            print("无法确认默认输入设备", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        info = sd.query_devices(device)
        blocksize = SAMPLE_RATE * FRAME_MS // 1000
        rms_values: list[float] = []
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=blocksize,
        ) as stream:
            print(f"输入设备：{info.get('name')}（{device}）| 标签：{args.label} | 时长：{args.seconds}s")
            print("请按标签要求发出相应声音…")
            for _ in range(max(1, int(args.seconds * 1000 / FRAME_MS))):
                block, overflowed = stream.read(blocksize)
                if overflowed:
                    continue
                rms_values.append(frame_rms(bytes(block)))
    except Exception as exc:  # noqa: BLE001 - 设备异常按前置失败
        print(f"采集失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    if not rms_values:
        print("没有采集到任何完整帧", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    stats = {
        "label": args.label,
        "frames": len(rms_values),
        "min": round(min(rms_values), 1),
        "p50": round(percentile(rms_values, 0.50), 1),
        "p90": round(percentile(rms_values, 0.90), 1),
        "p95": round(percentile(rms_values, 0.95), 1),
        "p99": round(percentile(rms_values, 0.99), 1),
        "max": round(max(rms_values), 1),
        "mean": round(statistics.fmean(rms_values), 1),
    }
    print("帧 RMS 统计（PCM16，20ms 帧）：")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    active = sum(1 for value in rms_values if value >= args.suggest_from)
    print(
        f"  ≥{args.suggest_from:g} 的帧数：{active}/{len(rms_values)}"
        f"（{active / len(rms_values) * 100:.1f}%）"
    )
    print("原始音频未落盘、未保存、未打印。")
    return EXIT_OK


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live-opt-in", action="store_true", help="必需：确认打开本机麦克风")
    parser.add_argument("--seconds", type=float, default=6.0, help="采集时长（秒，默认 6）")
    parser.add_argument("--label", default="quiet", help="本次标定标签，例如 quiet / speech")
    parser.add_argument("--device-id", default=None, help="固定输入设备 id（默认系统默认设备）")
    parser.add_argument(
        "--suggest-from",
        type=float,
        default=600.0,
        help="同时统计有多少帧不低于该值（默认 600，与 runner 默认阈值一致）",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
