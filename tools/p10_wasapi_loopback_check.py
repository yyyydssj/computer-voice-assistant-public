"""P10 R2b-2 诊断：在 **WASAPI 输出端点本身**上放音，并录同名端点的回环。

目的：把 "waveOut(WAVE_MAPPER) 映射到的端点" 与 "回环监听的端点" 是否为同一个 这件事
直接测出来。做法是不走 waveOut，而是用 PyAudioWPatch 打开某个 WASAPI 输出端点写音频，
同时打开它的 [Loopback] 端点读音频；若两者是同一条链路，相关性应接近 1.0。

用法（需显式 --live-opt-in）：
    .\\.venv\\Scripts\\python.exe tools\\p10_wasapi_loopback_check.py --live-opt-in
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location("probe", REPO_ROOT / "tools" / "p10_aec_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "implementation_plan" / "checkpoints" / "artifacts"


def _pair_outputs_with_loopback(audio) -> list[dict]:
    """把 WASAPI 输出端点与同名回环端点配对。"""

    host = None
    for index in range(audio.get_host_api_count()):
        api = audio.get_host_api_info_by_index(index)
        if "WASAPI" in str(api.get("name", "")):
            host = index
            break
    if host is None:
        return []
    outputs, loopbacks = [], []
    for index in range(audio.get_device_count()):
        device = audio.get_device_info_by_index(index)
        if int(device.get("hostApi", -1)) != host:
            continue
        entry = {
            "index": index,
            "name": str(device.get("name", "")),
            "rate": int(device.get("defaultSampleRate", 0)),
            "in": int(device.get("maxInputChannels", 0)),
            "out": int(device.get("maxOutputChannels", 0)),
            "loopback": bool(device.get("isLoopbackDevice", False)),
        }
        (loopbacks if entry["loopback"] else outputs).append(entry)
    pairs = []
    for output in outputs:
        base = output["name"].split(" (")[0]
        match = next(
            (item for item in loopbacks if item["name"].startswith(base)), None
        )
        if match is not None:
            pairs.append({"output": output, "loopback": match})
    return pairs


def _play_and_record(audio, output: dict, loopback: dict, reference: np.ndarray, rate: int):
    channels = max(1, int(output["out"]))
    frames = reference.size
    stream = audio.open(
        format=audio.get_format_from_width(2),
        channels=channels,
        rate=rate,
        output=True,
        output_device_index=int(output["index"]),
        frames_per_buffer=1024,
    )
    recorded: list[bytes] = []
    stop = threading.Event()
    loop_stream = audio.open(
        format=audio.get_format_from_width(2),
        channels=1,
        rate=rate,
        input=True,
        input_device_index=int(loopback["index"]),
        frames_per_buffer=1024,
    )

    def reader() -> None:
        while not stop.is_set():
            try:
                recorded.append(loop_stream.read(1024, exception_on_overflow=False))
            except Exception:  # noqa: BLE001
                break

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)
    pcm = probe.float_to_pcm16(reference)
    if channels > 1:
        pcm = b"".join(pcm[pos : pos + 2] * channels for pos in range(0, len(pcm), 2))
    try:
        stream.write(pcm)
    finally:
        time.sleep(0.4)
        stop.set()
        thread.join(timeout=2.0)
        for handle in (loop_stream, stream):
            try:
                handle.stop_stream()
                handle.close()
            except Exception:  # noqa: BLE001
                pass
    raw = b"".join(recorded)
    return probe.pcm16_to_float(raw) if raw else np.zeros(0, dtype=np.float32)


def _enumerate_inputs(audio) -> list[dict]:
    """列出 WASAPI 输入端点（含回环标记），用于选出真正的麦克风。"""

    host = None
    for index in range(audio.get_host_api_count()):
        api = audio.get_host_api_info_by_index(index)
        if "WASAPI" in str(api.get("name", "")):
            host = index
            break
    if host is None:
        return []
    entries = []
    for index in range(audio.get_device_count()):
        device = audio.get_device_info_by_index(index)
        if int(device.get("hostApi", -1)) != host or int(device.get("maxInputChannels", 0)) <= 0:
            continue
        entries.append(
            {
                "index": index,
                "name": str(device.get("name", "")),
                "rate": int(device.get("defaultSampleRate", 0)),
                "loopback": bool(device.get("isLoopbackDevice", False)),
            }
        )
    return entries


def _play_and_probe(
    audio,
    output: dict,
    loopback: dict,
    mic: dict,
    reference: np.ndarray,
    rate: int,
):
    """在指定输出端点放音，同时录回环与麦克风：判断声音到底从哪条路出来。"""

    channels = max(1, int(output["out"]))
    stream = audio.open(
        format=audio.get_format_from_width(2),
        channels=channels,
        rate=rate,
        output=True,
        output_device_index=int(output["index"]),
        frames_per_buffer=1024,
    )
    loop_stream = audio.open(
        format=audio.get_format_from_width(2),
        channels=1,
        rate=rate,
        input=True,
        input_device_index=int(loopback["index"]),
        frames_per_buffer=1024,
    )
    mic_rate = int(mic["rate"])
    mic_stream = audio.open(
        format=audio.get_format_from_width(2),
        channels=1,
        rate=mic_rate,
        input=True,
        input_device_index=int(mic["index"]),
        frames_per_buffer=1024,
    )
    loop_chunks: list[bytes] = []
    mic_chunks: list[bytes] = []
    stop = threading.Event()

    def reader(handle, sink) -> None:
        while not stop.is_set():
            try:
                sink.append(handle.read(1024, exception_on_overflow=False))
            except Exception:  # noqa: BLE001
                break

    loops = [
        threading.Thread(target=reader, args=(loop_stream, loop_chunks), daemon=True),
        threading.Thread(target=reader, args=(mic_stream, mic_chunks), daemon=True),
    ]
    for thread in loops:
        thread.start()
    time.sleep(0.3)
    pcm = probe.float_to_pcm16(reference)
    if channels > 1:
        pcm = b"".join(pcm[pos : pos + 2] * channels for pos in range(0, len(pcm), 2))
    try:
        stream.write(pcm)
    finally:
        time.sleep(0.4)
        stop.set()
        for thread in loops:
            thread.join(timeout=2.0)
        for handle in (loop_stream, mic_stream, stream):
            try:
                handle.stop_stream()
                handle.close()
            except Exception:  # noqa: BLE001
                pass
    loop_raw = b"".join(loop_chunks)
    mic_raw = b"".join(mic_chunks)
    return (
        probe.pcm16_to_float(loop_raw) if loop_raw else np.zeros(0, dtype=np.float32),
        probe.pcm16_to_float(mic_raw) if mic_raw else np.zeros(0, dtype=np.float32),
    )


def _record_only(audio, loopback: dict, *, seconds: float) -> np.ndarray:
    """只录回环，不播放任何东西（用于'用系统正常放音乐，看回环能否抓到'实验）。"""

    rate = int(loopback["rate"])
    stream = audio.open(
        format=audio.get_format_from_width(2),
        channels=1,
        rate=rate,
        input=True,
        input_device_index=int(loopback["index"]),
        frames_per_buffer=1024,
    )
    frames = int(seconds * rate)
    chunks: list[bytes] = []
    try:
        while sum(len(chunk) for chunk in chunks) < frames * 2:
            chunks.append(stream.read(1024, exception_on_overflow=False))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:  # noqa: BLE001
            pass
    raw = b"".join(chunks)
    return probe.pcm16_to_float(raw) if raw else np.zeros(0, dtype=np.float32)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WASAPI 输出端点与其回环的一致性检查")
    parser.add_argument("--live-opt-in", action="store_true", help="显式开启真机（会放音并录音）")
    parser.add_argument("--seconds", type=float, default=3.0, help="参考信号时长（秒）")
    parser.add_argument(
        "--watch",
        type=float,
        default=None,
        metavar="SECONDS",
        help="只录回环 N 秒、完全不播放：请在这段时间里用系统正常放一段音乐来验证回环是否有效",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="放音时同时录回环与麦克风，判断声音实际从哪条路出来（扬声器还是耳机）",
    )
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    args = parser.parse_args(argv)

    import pyaudiowpatch  # type: ignore[import-not-found]

    audio = pyaudiowpatch.PyAudio()
    try:
        pairs = _pair_outputs_with_loopback(audio)
        print("=" * 74)
        print(f"WASAPI 输出端点 {len(pairs)} 组配对：")
        for pair in pairs:
            print(
                f"  out[{pair['output']['index']}] {pair['output']['name']} "
                f"({pair['output']['rate']} Hz, {pair['output']['out']}ch)  <->  "
                f"loop[{pair['loopback']['index']}] {pair['loopback']['name']}"
            )
        if not args.live_opt_in:
            print("不提供 --live-opt-in：只列出配对，不打开设备流。")
            return 0
        if not pairs:
            print("没有可配对的 WASAPI 输出/回环端点。")
            return 2

        if args.watch is not None:
            if args.watch <= 0:
                print("--watch 必须为正")
                return 2
            loopback = pairs[0]["loopback"]
            print(f"\n[p10] 现在开始只录回环 {args.watch:.1f}s，**不播放任何东西**。")
            print("[p10] 请立刻用系统正常播放一段音乐/视频（音量正常），让它在这段时间里出声。")
            for remaining in (3, 2, 1):
                print(f"[p10] {remaining} ...")
                time.sleep(1.0)
            recorded = _record_only(audio, loopback, seconds=args.watch)
            rate = int(loopback["rate"])
            rms = probe._rms(recorded)
            peak = float(np.max(np.abs(recorded))) if recorded.size else 0.0
            active_ratio = float(np.mean(np.abs(recorded) > 0.01)) if recorded.size else 0.0
            summary = {
                "mode": "watch_loopback",
                "loopback": loopback,
                "seconds": round(recorded.size / rate, 3),
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "active_ratio": round(active_ratio, 4),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print(
                "[p10] 判读：如果你确实放了音乐而 rms 仍接近 0，说明**回环采集本身无效**；"
                "若 rms 明显 > 0，说明回环有效，问题在我们的播放路径。"
            )
            return 0

        reference = probe.build_reference_signal(seconds=args.seconds, rate=24000)
        if args.probe:
            mic = next(
                (
                    entry
                    for entry in _enumerate_inputs(audio)
                    if not entry["loopback"]
                ),
                None,
            )
            if mic is None:
                print("找不到麦克风端点。")
                return 2
            pair = pairs[0]
            rate = int(pair["output"]["rate"])
            reference_at_rate = probe._resample(reference, src_rate=24000, dst_rate=rate)
            print(
                f"\n[p10] 在 out[{pair['output']['index']}] 放音 {args.seconds:.1f}s，"
                f"同时录回环[{pair['loopback']['index']}]与麦克风[{mic['index']}]…"
            )
            print("[p10] 请留意：这段声音你**从哪个设备听到的**（扬声器/耳机/显示器音箱）？")
            time.sleep(1.0)
            loop_raw, mic_raw = _play_and_probe(
                audio, pair["output"], pair["loopback"], mic, reference_at_rate, rate
            )
            loop_rate = rate
            mic_rate = int(mic["rate"])
            loop_rms = probe._rms(loop_raw)
            mic_rms = probe._rms(mic_raw)
            loop_peak = float(np.max(np.abs(loop_raw))) if loop_raw.size else 0.0
            mic_peak = float(np.max(np.abs(mic_raw))) if mic_raw.size else 0.0
            print(
                json.dumps(
                    {
                        "output": pair["output"],
                        "loopback_rms": round(loop_rms, 6),
                        "loopback_peak": round(loop_peak, 5),
                        "loopback_seconds": round(loop_raw.size / loop_rate, 3),
                        "mic_rms": round(mic_rms, 6),
                        "mic_peak": round(mic_peak, 5),
                        "mic_seconds": round(mic_raw.size / mic_rate, 3),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print(
                "[p10] 判读：loopback_rms 接近 0 而 mic_rms 明显 > 0 → 声音没走这个输出端点"
                "（很可能被送到耳机/其它设备）；两者都接近 0 → 我们实际上没放出声音。"
            )
            return 0

        results = []
        for pair in pairs:
            rate = int(pair["output"]["rate"])
            reference_at_rate = probe._resample(reference, src_rate=24000, dst_rate=rate)
            print(f"\n[p10] 在 out[{pair['output']['index']}] 放音 {args.seconds:.1f}s，同时录其回环…")
            time.sleep(1.0)
            recorded = _play_and_record(audio, pair["output"], pair["loopback"], reference_at_rate, rate)
            template = probe._analysis_segment(reference_at_rate, rate=rate)
            offset, score = probe._find_offset(template, recorded, search_seconds=3.0, rate=rate)
            entry = {
                "output": pair["output"],
                "loopback": pair["loopback"],
                "recorded_seconds": round(recorded.size / rate, 3),
                "recorded_peak": round(float(np.max(np.abs(recorded))) if recorded.size else 0.0, 5),
                "recorded_rms": round(probe._rms(recorded), 5),
                "template_offset_ms": round(offset / rate * 1000.0, 1),
                "template_alignment": round(score, 4),
            }
            results.append(entry)
            print(f"      录制 {entry['recorded_seconds']}s peak={entry['recorded_peak']} "
                  f"对齐={entry['template_alignment']} 偏移={entry['template_offset_ms']}ms")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(args.artifact_root) / "2026-10-02-P10-aec-probe"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"p10-wasapi-pairs-{timestamp}.json"
        path.write_text(
            json.dumps(
                {"kind": "p10_wasapi_pair_check", "created_at": timestamp, "results": results},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n[p10] artifact：{path}")
        print("[p10] 判读：若某组的 template_alignment 接近 1.0，说明该 out<->loop 配对是同一链路；")
        print("       与 waveOut 的结果对比即可确认 waveOut 是否播到了别处。")
        return 0
    finally:
        audio.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
