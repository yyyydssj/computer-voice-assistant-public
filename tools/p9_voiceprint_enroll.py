"""P9 B4：声纹注册（只落**嵌入向量**，绝不落原始音频）。

用法（需要显式 ``--live-opt-in`` 才会打开麦克风）：

```powershell
.\.venv\Scripts\python.exe tools\p9_voiceprint_enroll.py --live-opt-in --seconds 6 `
    --model models\voiceprint\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx `
    --speaker owner --out G:\study\电脑语音助手\data\voiceprint-owner.json
```

边界（与本项目其它真机工具一致）：

- 只有给出 ``--out`` 才落盘，且落盘的**只有嵌入向量**（浮点数组）——不保存任何音频、不打印正文；
- 不给 ``--out`` 时只在控制台打印摘要（仍是向量摘要，不是音频）；
- 不联网、不做电脑操作、不产生授权/计划/执行事实；声纹只用于"误触发过滤"；
- 任何失败（设备/模型/音频不足）一律 fail closed：固定短码 + 退出码 3，**不写半成品档案**。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_OPTED_IN = 2
EXIT_PREFLIGHT_FAILED = 3
EXIT_INTERRUPTED = 130

FRAME_MS = 20
SAMPLE_RATE_HZ = 16000
BYTES_PER_SAMPLE = 2
PROFILE_VERSION = 1
ENROLL_AUDIO_SESSION_ID = "p9-voiceprint-enroll"
ENROLL_STREAM_ID = "p9-voiceprint-enroll-stream"
ENROLL_STOP_REASON = "voiceprint-enroll"
ENROLL_CAPACITY_FRAMES = 256
DEFAULT_MODEL = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "voiceprint"
    / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
)


class EnrollError(RuntimeError):
    """注册前置条件不满足或注册失败（``str()`` 只给固定短码）。"""

    CODES = frozenset(
        {
            "capture_unavailable",
            "model_missing",
            "audio_too_short",
            "embedding_failed",
            "dependency_missing",
            "init_failed",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in self.CODES else "init_failed"
        super().__init__(self.code)


def required_bytes(seconds: int) -> int:
    """``seconds`` 秒 16kHz 单声道 PCM16 的字节数。"""

    return int(seconds) * SAMPLE_RATE_HZ * BYTES_PER_SAMPLE


def collect_pcm(
    capture_factory: Callable[[Any], Any],
    *,
    seconds: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """打开麦克风收 ``seconds`` 秒 PCM16LE 单声道音频并返回（**不落盘**）。"""

    from app.audio.bus import AudioBus

    if not callable(capture_factory):
        raise EnrollError("capture_unavailable")
    bus = AudioBus(
        audio_session_id=ENROLL_AUDIO_SESSION_ID,
        provider_generation=1,
        stream_id=ENROLL_STREAM_ID,
        device_epoch=1,
        capacity_frames=ENROLL_CAPACITY_FRAMES,
    )
    capture = capture_factory(bus)
    started = capture.start()
    if not getattr(started, "started", False):
        raise EnrollError("capture_unavailable")
    target = required_bytes(seconds)
    pcm = bytearray()
    deadline = clock() + float(seconds) * 2.0 + 5.0  # 宽容上限：设备不给帧时也要退出
    try:
        while len(pcm) < target and clock() < deadline:
            packet = bus.read_nowait()
            if packet is None:
                sleep(FRAME_MS / 1000)
                continue
            payload = getattr(packet, "payload", None)
            if isinstance(payload, (bytes, bytearray, memoryview)):
                pcm.extend(bytes(payload))
    finally:
        stop = getattr(capture, "stop", None)
        if callable(stop):
            try:
                stop(reason=ENROLL_STOP_REASON, discard=True)
            except Exception:  # noqa: BLE001 - 资源清理尽力而为
                pass
    return bytes(pcm[:target])


def build_profile(
    *, speaker: str, embedding: Sequence[float], model_path: Path
) -> dict[str, Any]:
    """构造档案载荷（只有嵌入向量与机械元数据，**不含音频**）。"""

    values = [float(value) for value in embedding]
    if not values:
        raise EnrollError("embedding_failed")
    return {
        "version": PROFILE_VERSION,
        "speaker": speaker,
        "model": model_path.name,
        "embedding_dim": len(values),
        "embeddings": [values],
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "只包含说话人嵌入向量，不含音频；仅用于本地误触发过滤。",
    }


def write_profile(path: Path, profile: dict[str, Any]) -> Path:
    """把档案写成 UTF-8 **无 BOM** JSON（目录不存在就创建）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):  # 防御：任何 BOM 都不允许写出去
        raise EnrollError("init_failed")
    return path


def build_capture_factory(device_id: str | None) -> Callable[[Any], Any]:
    """真实采集工厂（延迟导入，仅在真机路径调用）。"""

    from app.runtime.voice_entry import VoiceEntryConfig, build_real_environment

    environment = build_real_environment(VoiceEntryConfig(tts_mode="none", device_id=device_id))
    factory = getattr(environment, "capture_factory", None)
    if not callable(factory):
        raise EnrollError("capture_unavailable")
    return factory


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P9 B4 声纹注册：录一段本人口音，写出只含嵌入向量的档案 JSON。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--live-opt-in", action="store_true", help="真机注册必须显式开启（会打开麦克风）")
    parser.add_argument("--check-only", action="store_true", help="只检查模型/参数，不开麦克风、不写档案")
    parser.add_argument("--seconds", type=int, default=6, help="录音秒数（建议 ≥5，默认 6）")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="声纹嵌入 onnx 模型路径")
    parser.add_argument("--speaker", default="owner", help="档案里的说话人名字（默认 owner）")
    parser.add_argument("--out", default=None, help="档案输出路径；不给则只打印摘要（不落盘）")
    parser.add_argument("--device-id", default=None, help="固定输入设备 id（默认系统默认设备）")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    model_path = Path(args.model)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in（真机注册必须显式开启）", file=sys.stderr)
        print("该工具会打开本机麦克风，并写出一个只含嵌入向量的档案文件。", file=sys.stderr)
        return EXIT_NOT_OPTED_IN
    if args.seconds <= 0:
        print("参数非法：--seconds 必须是正整数", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if not model_path.is_file():
        print(f"声纹模型不存在：{model_path}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if args.check_only:
        print(f"检查通过：模型存在（{model_path.name}，{model_path.stat().st_size} 字节）")
        print(f"录音秒数：{args.seconds}；说话人：{args.speaker}")
        print(f"档案输出：{args.out or '（未指定，只打印摘要，不落盘）'}")
        print("--check-only：未打开麦克风、未写任何文件。")
        return EXIT_OK

    from app.audio.voiceprint import VoiceprintConfig, VoiceprintError, VoiceprintFilter

    print("P9 B4 声纹注册")
    print("边界：只写嵌入向量、不写音频、不联网、不产生授权/计划/执行事实。")
    try:
        voiceprint = VoiceprintFilter(config=VoiceprintConfig(model_path=model_path))
    except VoiceprintError as exc:
        print(f"声纹模型不可用（{exc}）：已按 fail closed 退出。", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    try:
        factory = build_capture_factory(args.device_id)
    except Exception as exc:  # noqa: BLE001 - 设备/依赖问题一律 fail closed
        print(f"采集不可用（{type(exc).__name__}）：已按 fail closed 退出。", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    print(f"请连续说话 {args.seconds} 秒（自然语速，正常音量）…")
    try:
        pcm = collect_pcm(factory, seconds=args.seconds)
    except KeyboardInterrupt:
        print("\n已中断，未写任何档案。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except EnrollError as exc:
        print(f"录音失败（{exc}）：未写任何档案。", file=sys.stderr)
        return EXIT_FAILED
    try:
        embedding = voiceprint.embed_from_pcm(pcm)
    except VoiceprintError as exc:
        print(f"取声纹失败（{exc}）：未写任何档案。", file=sys.stderr)
        return EXIT_FAILED
    if embedding is None:
        print("音频过短，未能算出声纹：未写任何档案（请重试并说满指定秒数）。", file=sys.stderr)
        return EXIT_FAILED
    profile = build_profile(speaker=args.speaker, embedding=embedding, model_path=model_path)
    print(f"声纹已生成：维度 {profile['embedding_dim']}，说话人 {profile['speaker']}")
    if args.out:
        try:
            path = write_profile(Path(args.out), profile)
        except EnrollError as exc:
            print(f"写档案失败（{exc}）：未写入。", file=sys.stderr)
            return EXIT_FAILED
        print(f"档案已写入：{path}（只含嵌入向量，不含音频）")
        print("提示：该文件属于生物特征数据，请自行保管；如需可随时删除。")
    else:
        print("未指定 --out：只打印摘要，未落盘。")
        print(f"  model={profile['model']} dim={profile['embedding_dim']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
