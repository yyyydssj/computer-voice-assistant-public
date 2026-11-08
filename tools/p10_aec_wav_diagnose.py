"""只读诊断：把 --save-audio 写出的 WAV 与参考信号逐段比对，定位回环/麦克风到底抓住了什么。

用法：
    .\\.venv\\Scripts\\python.exe tools\\p10_aec_wav_diagnose.py <audio-dir> [<audio-dir> ...]

只读取 WAV，不改写任何文件，不打开设备。
"""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("probe", REPO_ROOT / "tools" / "p10_aec_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

REF_RATE = 24_000


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(frames)
    if width != 2:
        raise SystemExit(f"{path.name}: 只支持 16-bit WAV（实际 {width * 8} bit）")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def envelope(signal: np.ndarray, *, rate: int, block_ms: int = 50) -> np.ndarray:
    block = max(int(rate * block_ms / 1000), 1)
    usable = signal.size // block * block
    if usable == 0:
        return np.zeros(0, dtype=np.float64)
    shaped = signal[:usable].reshape(-1, block)
    return np.sqrt(np.mean(shaped.astype(np.float64) ** 2, axis=1))


def best_offset(reference: np.ndarray, recorded: np.ndarray, *, rate: int, search_seconds: float = 3.0):
    return probe._find_offset(reference, recorded, search_seconds=search_seconds, rate=rate)


def scan_offsets(reference: np.ndarray, recorded: np.ndarray, *, rate: int, step_seconds: float = 0.5):
    """在整段录制上滑动模板，报告前几个候选峰（判断是否只有某个位置对上）。"""

    template = probe._analysis_segment(reference, rate=rate)
    scores = []
    span = int(step_seconds * rate)
    step = max(span // 4, 1)
    for start in range(0, max(recorded.size - template.size, 0), step):
        segment = recorded[start : start + template.size]
        if segment.size < template.size:
            break
        scores.append((start, probe._normalized_correlation_score(template, segment)))
    scores.sort(key=lambda item: item[1], reverse=True)
    return template, scores[:5]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for directory in argv:
        root = Path(directory)
        print("=" * 78)
        print(f"目录：{root}")
        reference = probe.build_reference_signal(seconds=6.0, rate=REF_RATE)
        for name in ("loopback.wav", "mic.wav"):
            path = root / name
            if not path.exists():
                print(f"[{name}] 不存在")
                continue
            signal, rate = read_wav(path)
            reference_at_rate = probe._resample(reference, src_rate=REF_RATE, dst_rate=rate)
            offset, score = best_offset(reference_at_rate, signal, rate=rate, search_seconds=3.0)
            template, top = scan_offsets(reference_at_rate, signal, rate=rate)
            print(f"[{name}] {signal.size / rate:.2f}s @ {rate} Hz  rms={probe._rms(signal):.5f}  "
                  f"peak={float(np.max(np.abs(signal))):.5f}")
            print(f"         全段最佳偏移 {offset} ({offset / rate * 1000:.0f} ms) 对齐={score:.4f}")
            print(f"         滑动扫描候选：" + ", ".join(
                f"{start / rate * 1000:.0f}ms:{value:.3f}" for start, value in top
            ))
            ref_env = envelope(reference_at_rate, rate=rate)
            sig_env = envelope(signal, rate=rate)
            size = min(ref_env.size, sig_env.size)
            if size:
                correlation = float(np.corrcoef(ref_env[:size], sig_env[:size])[0, 1])
                print(f"         包络相关（参考 vs 该路，未对齐）={correlation:.3f}")
            # 参考在回环中"应当出现"的位置：按播放起点找，再做局部细化
            print(f"         带内占比={probe._band_ratio(signal, rate=rate)}  过零率="
                  f"{probe._zero_crossing_rate(signal, rate=rate)}")
        print()

    # 参考自身的自相关锐度（用于判断模板是否可用）
    print("=" * 78)
    reference = probe.build_reference_signal(seconds=6.0, rate=REF_RATE)
    template = probe._analysis_segment(reference, rate=REF_RATE)
    noiseless = np.concatenate((np.zeros(REF_RATE, dtype=np.float32), template, np.zeros(REF_RATE, dtype=np.float32)))
    offset, score = best_offset(template, noiseless, rate=REF_RATE, search_seconds=2.0)
    print(f"参考自检：模板在纯自身副本中的对齐={score:.4f}（偏移 {offset}），应接近 1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
