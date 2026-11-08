r"""P9 真机自动收音试听 runner（需要 ``--live-opt-in``）。

验证一条**免按键**的真实链路：

```text
持续监听 → P9 自动收音判定语音起始 → 静音自动结束一轮 → 真实麦克风采集
→ 本地 sherpa-onnx SenseVoice 识别 → 同一个 ConversationManager
→ 本地脚本 Agent 回复 → 真实 TTS 播报 → 回到监听（下一轮）
```

与 `tools/p8_real_voice_loop_runner.py` 的区别：P8 需要按 Enter 开始说话，本 runner
用 P9 `AutoCaptureVoiceDriver` 自动判定「开始说话」与「说完了」，全程不需要按键。

硬边界（与 P9 路线图一致）：

- 没有 ``--live-opt-in`` 时立即以退出码 2 结束，**不导入** sounddevice/sherpa-onnx、
  不打开设备、不读 ``.env``、不联网；
- P9 的 VAD/自动收音只决定**输入边界**，不决定授权；不创建计划、授权、账本或执行事实，
  不调用任何工具，不操作电脑；
- 原始音频不落盘、不保存；识别文本只打印到控制台，artifact 只记长度与 SHA-256；
- TTS 只允许本机 loopback 端点（127.0.0.1:3900）或本机离线声线；
- 未检测到语音就丢弃本轮（SenseVoice 对静音可能产生幻觉文本，绝不提交/播报）；
- Agent 默认是**本地脚本替身**（回显识别结果），不代表真实模型；真实 Provider 属 R 支线授权门。

用法：

```powershell
# 前置检查（不打开麦克风）
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --check-only

# 真机免按键试听：静默等待你开口，说完停顿约 1.2s 自动结束一轮
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 2 --tts-mode ws_pcm

# 环境自适应阈值：第一轮开始前先采样 1.2s 环境静音，再开始免按键试听
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --threshold-mode auto --vad-mode energy --turns 2

# 只想验证识别与提交（不播报）
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 1 --tts-mode none
```

退出码：0 通过 / 1 某轮未提交或未播报 / 2 未 opt-in / 3 前置检查、VAD 不可用或自动阈值校准失败 / 130 用户中断。

语音判定模式（``--vad-mode``）：

- ``silero``（默认）用项目内 ``models/openwakeword/silero_vad.onnx``（``--vad-model`` 可覆盖）
  的 sherpa-onnx Silero VAD 逐帧判定人声，**优先于能量阈值**：能量阈值不生效、不跑能量
  校准；模型缺失 / 依赖缺失 / 初始化失败一律**如实报错并退出 3**，不静默假装可用；
- ``energy`` 保持历史行为：能量阈值判定，可配合 ``--threshold-mode auto`` 采样环境静音。

阈值模式（``--threshold-mode``）：

- ``fixed``（默认）与历史行为逐字一致：固定 ``--speech-threshold``（默认 600），不采样；
- ``auto`` 在 ``AutoVoiceSession`` 装配时、第一轮开始**之前**采样一次环境静音，用
  ``app.audio.vad.adaptive`` 的分位数法算出阈值并交给 ``AutoCaptureDriverConfig``；
  采样结束即清空音频总线残留，校准音频只用于统计、绝不进入第一轮；仅 ``--vad-mode energy``
  下生效（``silero`` 模式由 Silero VAD 判定，能量阈值不生效、不采样）；
- ``auto`` 校准被拒绝（采样不足 / 环境太吵）时**如实报错并退出 3**，不会静默回退到 600。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

ARTIFACT_ROOT = (
    _REPO
    / "implementation_plan"
    / "checkpoints"
    / "artifacts"
    / "2026-09-30-P9-auto-voice-loop"
)

EXIT_OK = 0
EXIT_TURN_FAILED = 1
EXIT_NOT_OPTED_IN = 2
EXIT_PREFLIGHT_FAILED = 3
EXIT_INTERRUPTED = 130

FRAME_MS = 20
TTS_MODES = ("ws_pcm", "http_wav", "auto", "pcm_seq", "edge", "none")
THRESHOLD_MODES = ("fixed", "auto")
VAD_MODES = ("energy", "silero")
KWS_MODES = ("off", "gate")
VOICEPRINT_MODES = ("off", "filter")
DEFAULT_KEYWORD = "小饼"
# 唤醒词门禁使用自有总线身份；唤醒阶段的音频**不进 ASR**（唤醒词只开启新会话）。
WAKE_AUDIO_SESSION_ID = "p9-auto-wakeword"
WAKE_STREAM_ID = "p9-auto-wakeword-stream"
WAKE_STOP_REASON = "wakeword-gate"
WAKE_CAPACITY_FRAMES = 64
DEFAULT_VAD_MODEL = _REPO / "models" / "openwakeword" / "silero_vad.onnx"
NO_SPEECH_REASON = "no_speech_discarded"

# 单轮自动收音的音频总线容量（帧）；20ms/帧 → 10 秒积压。
# 旧值 64（1.28s）对"消费者暂时落后于采集"没有任何余量：``AudioBus.publish`` 在队列满时
# 走 ``_fail_locked("overflow")``，会把总线置为 FAILED **并清空队列**，本轮已采集音频全部作废、
# 轮次只能被丢弃。这里的容量只用于兜住异常积压（内存上限仍是几十 KB 的 PCM），正常路径由
# "有帧就立刻继续泵、不固定睡满一帧"保证消费者不落后（见 ``_turn_idle_sleep_seconds``）。
AUDIO_CAPACITY_FRAMES = 500

# 已开始收音但连续这么多毫秒**一帧都没有** → 判定输入停滞（设备断开/总线失效）并立即收尾。
# 旧实现只能靠 ``speech_start_timeout_ms + max_utterance_ms``（默认 23s）兜底空转。
# 只在"完全没有帧"时生效：有环境声但没人说话属于正常空轮（帧仍在推进），不受影响。
INPUT_STALL_MS = 5000


def _turn_idle_sleep_seconds(processed_frames: int) -> float:
    """本轮的轮询睡眠策略：泵到帧就不睡（先把积压追平），一帧都没有才让出 CPU 一帧时长。

    固定"每轮睡 20ms"会让消费者永久慢于采集（20ms 命中 + 任何开销 > 采集周期），积压
    只会单调增长并最终撑爆总线；这里让泵在有帧时立即重试，从而与采集同速、不积压。
    """

    return 0.0 if processed_frames > 0 else FRAME_MS / 1000


# 校准采样使用自有的音频总线身份与容量；容量必须容纳整个采样窗口（1200ms/20ms=60 帧）。
CALIBRATION_AUDIO_SESSION_ID = "p9-auto-threshold-calibration"
CALIBRATION_STREAM_ID = "p9-auto-threshold-calibration-stream"
CALIBRATION_STOP_REASON = "threshold-calibration"
CALIBRATION_CAPACITY_FRAMES = 256
CALIBRATION_DEADLINE_FACTOR = 4


# ----------------------------------------------------------------- 共享实现（P9 抽取）
# 免按键链路的中性实现已抽到 app/runtime/auto_voice.py：主程序与真机 runner 共用同一份，
# 避免两处装配漂移。这里保留 runner 专属的配置子类（turns/tts_mode/edge_voice/hud）。
from app.runtime.auto_voice import (  # noqa: E402 - 与工具脚本的导入顺序保持一致
    AUDIO_CAPACITY_FRAMES,
    CALIBRATION_AUDIO_SESSION_ID,
    CALIBRATION_CAPACITY_FRAMES,
    CALIBRATION_DEADLINE_FACTOR,
    CALIBRATION_STOP_REASON,
    CALIBRATION_STREAM_ID,
    DEFAULT_KEYWORD,
    DEFAULT_VAD_MODEL,
    FRAME_MS,
    INPUT_STALL_MS,
    KWS_MODES,
    NO_SPEECH_REASON,
    THRESHOLD_MODES,
    VAD_MODES,
    VOICEPRINT_MODES,
    WAKE_AUDIO_SESSION_ID,
    WAKE_CAPACITY_FRAMES,
    WAKE_STOP_REASON,
    WAKE_STREAM_ID,
    _hud_set,
    _hud_tick,
    AutoTurnFacts,
    AutoVoiceConfig,
    AutoVoiceSession as _SharedAutoVoiceSession,
    AutoVoiceError,
    ThresholdCalibrationError,
    VadUnavailableError,
    VoiceprintUnavailableError,
    WakeWordUnavailableError,
    calibrate_speech_threshold,
    load_voiceprint_profile,
    wait_for_wake_word,
)
from app.runtime.auto_voice import AutoVoiceError as RunnerError

# runner 侧兼容别名：既有调用方/测试仍以 RunnerError 捕获装配失败。


class AutoVoiceSession(_SharedAutoVoiceSession):
    """runner 用的会话：与共享实现完全一致，只把本地提示默认接到 ``_safe_print``。

    提示文本仍走模块级 ``_safe_print``（测试会替换它看控制台输出），因此在调用时才解析，
    不把函数对象固化进实例状态。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("notify", lambda text: _safe_print(text))
        super().__init__(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class AutoRunnerConfig(AutoVoiceConfig):
    """runner 本地参数：中性自动收音参数见 ``AutoVoiceConfig``，这里只加跑批相关项。"""

    turns: int = 2
    tts_mode: str = "ws_pcm"
    device_id: str | None = None
    edge_voice: str = "Microsoft Huihui Desktop"
    hud: bool = True

    def __post_init__(self) -> None:
        super(AutoRunnerConfig, self).__post_init__()
        if isinstance(self.turns, bool) or not isinstance(self.turns, int) or self.turns <= 0:
            raise ValueError("turns 必须是正整数")
        if self.tts_mode not in TTS_MODES:
            raise ValueError("tts_mode 非法")
        if self.device_id is not None and (
            not isinstance(self.device_id, str) or not self.device_id
        ):
            raise ValueError("device_id 必须是非空字符串或 None")
        if not isinstance(self.edge_voice, str) or not self.edge_voice:
            raise ValueError("edge_voice 必须是非空字符串")





@dataclass
class ScriptedAgent:
    """本地脚本 Agent：只回显识别结果，不调用模型、不联网。"""

    calls: list[dict[str, str]] = field(default_factory=list)

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> Any:
        from app.agent.adapter import AgentTurnOutcome

        self.calls.append({"request_id": request_id, "session_id": session_id, "text": text})
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"我听到你说：{text}。这是免按键自动收音的真机验证回复。",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


def build_environment(config: AutoRunnerConfig) -> Any:
    """真实环境装配；只有 ``--live-opt-in`` 之后才会被调用。"""

    from app.runtime.voice_entry import VoiceEntryConfig, build_real_environment

    return build_real_environment(
        VoiceEntryConfig(
            tts_mode=config.tts_mode,
            device_id=config.device_id,
            silence_ms=config.silence_ms,
            speech_timeout_seconds=max(1, config.speech_start_timeout_ms // 1000),
            max_record_seconds=config.max_record_seconds,
            speech_threshold=config.speech_threshold,
            model_path=config.model_path,
            tokens_path=config.tokens_path,
            edge_voice=config.edge_voice,
        )
    )


def build_session(config: AutoRunnerConfig, environment: Any) -> tuple[Any, AutoVoiceSession]:
    """装配验收用闭环：真实采集/ASR/TTS + 本地脚本 Agent。"""

    from app.conversation import ConversationManager
    from app.runtime.assistant_runtime import LocalChatRouter

    agent = ScriptedAgent()
    conversation = ConversationManager(
        agent,
        router=LocalChatRouter(),
        router_provider="local",
        router_model_id="p9-auto-scripted-agent",
        router_model_version="1",
    )
    session = AutoVoiceSession(
        conversation=conversation,
        environment=environment,
        config=config,
        source_instance_id="p9-auto-runner",
    )
    return agent, session


def _turn_record(facts: AutoTurnFacts) -> dict[str, Any]:
    """artifact 记录：只有计数/状态/原因码与文本摘要哈希，绝不含识别正文。"""

    record: dict[str, Any] = {
        "turn": facts.index,
        "boundary": facts.boundary_facts(),
        "frames": facts.frames,
        "speech_seen": facts.speech_seen,
        "vad_hits": facts.vad_hits,
        "end_reason": facts.end_reason,
        "accepted": facts.accepted,
        "submitted": facts.submitted,
        "spoken": facts.spoken,
        "reason": facts.reason,
        "seconds": facts.seconds,
        "idle_seconds": facts.idle_seconds,
    }
    if facts.asr_text is not None:
        record["asr_text_length"] = len(facts.asr_text)
        record["asr_text_sha256"] = hashlib.sha256(
            facts.asr_text.encode("utf-8")
        ).hexdigest()
    else:
        record["asr_text_length"] = 0
        record["asr_text_sha256"] = None
    return record


def _print_hud_timeline(details: object) -> None:
    """打印提示窗阶段时间线：只有内部状态名与毫秒，绝不含识别正文或回复正文。"""

    if not isinstance(details, dict):
        return
    durations = details.get("durations_ms")
    if not isinstance(durations, dict) or not durations:
        return
    rendered = " ".join(
        f"{name}={value}ms" for name, value in durations.items() if isinstance(value, int)
    )
    _safe_print(f"  提示窗阶段耗时：{rendered}")


def _print_end_timeline(details: object) -> None:
    """打印结束判定时间线：只含计数/原因码/毫秒，绝不含音频或识别正文。"""

    if not isinstance(details, dict) or not details:
        return
    _safe_print(
        "  结束判定：end_source={source} frames={frames} hits={hits} "
        "max_silence={max_ms}ms spoken_silence={spoken_ms}ms last_hit={last_hit}ms "
        "speech_started={started} segment_ends={seg} probe_errors={errors}".format(
            source=details.get("end_source"),
            frames=details.get("frames"),
            hits=details.get("hits"),
            max_ms=details.get("max_silence_run_ms"),
            spoken_ms=details.get("spoken_silence_ms"),
            last_hit=details.get("last_hit_elapsed_ms"),
            started=details.get("speech_started"),
            seg=details.get("segment_ends"),
            errors=details.get("segment_probe_failures"),
        )
    )


def _safe_print(text: str) -> None:
    """控制台编码不可靠时（GBK 下 emoji 会抛 UnicodeEncodeError）也不得中断轮次。

    真机实测：`▶` 在 GBK 控制台会抛 `UnicodeEncodeError`，原先只被 `run_turn` 的
    兜底 except 捕获，表现为"反复异常空转"而看不出原因。这里统一降级为 ASCII 记号。
    """

    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def _safe_print(text: str) -> None:
    """控制台编码不可靠时（GBK 下 emoji 会抛 UnicodeEncodeError）也不得中断轮次。

    真机实测：`▶` 在 GBK 控制台会抛 `UnicodeEncodeError`，原先只被 `run_turn` 的
    兜底 except 捕获，表现为"反复异常空转"而看不出原因。这里统一降级为 ASCII 记号。
    """

    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def _use_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 输出编码只是尽力而为
                pass


def _speaker_diagnostics(speaker: object | None) -> list[dict[str, Any]]:
    """把播报记录里可观测的机械事实读出来；不揣测是否被听到。"""

    if speaker is None:
        return []
    records = getattr(speaker, "records", None)
    if not isinstance(records, list):
        return []
    out: list[dict[str, Any]] = []
    for item in records:
        if isinstance(item, dict):
            out.append({key: value for key, value in item.items() if _is_scalar(value)})
    return out


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _print_speaker_diagnostics(speaker: object | None) -> None:
    records = _speaker_diagnostics(speaker)
    if not records:
        print("  播报诊断：没有产生任何播报记录（说明本轮没有进入播报通道）。")
        return
    for index, record in enumerate(records, start=1):
        print(f"  播报诊断[{index}]：{record}")


def _config_from_args(args: argparse.Namespace) -> AutoRunnerConfig:
    return AutoRunnerConfig(
        turns=args.turns,
        tts_mode=args.tts_mode,
        device_id=args.device_id,
        speech_threshold=args.speech_threshold,
        silence_ms=args.silence_ms,
        speech_start_timeout_ms=args.speech_start_timeout_ms,
        max_record_seconds=args.max_record_seconds,
        edge_voice=args.edge_voice,
        model_path=Path(args.model_path) if args.model_path else None,
        tokens_path=Path(args.tokens_path) if args.tokens_path else None,
        threshold_mode=args.threshold_mode,
        threshold_multiplier=args.threshold_multiplier,
        vad_mode=args.vad_mode,
        vad_model_path=Path(args.vad_model) if args.vad_model else None,
        vad_threshold=args.vad_threshold,
        vad_frames=args.vad_frames,
        vad_min_silence_ms=args.vad_min_silence_ms,
        input_stall_ms=args.input_stall_ms,
        min_utterance_ms=args.min_utterance_ms,
        kws_mode=args.kws_mode,
        kws_model_dir=Path(args.kws_model_dir) if args.kws_model_dir else None,
        kws_keywords_file=Path(args.kws_keywords_file) if args.kws_keywords_file else None,
        keyword=args.keyword,
        kws_threshold=args.kws_threshold,
        kws_wait_seconds=args.kws_wait_seconds,
        voiceprint_mode=args.voiceprint_mode,
        voiceprint_model_path=Path(args.voiceprint_model) if args.voiceprint_model else None,
        voiceprint_profile=Path(args.voiceprint_profile) if args.voiceprint_profile else None,
        voiceprint_threshold=args.voiceprint_threshold,
        voiceprint_seconds=args.voiceprint_seconds,
        hud=not getattr(args, "no_hud", False),
    )


def _print_threshold_calibration(session: AutoVoiceSession) -> None:
    """打印校准事实：noise p50/p95/max、最终阈值与来源；不打印任何音频内容。"""

    details = session.threshold_details()
    noise = details.get("noise")
    if not isinstance(noise, dict):
        return
    print(
        "环境自适应阈值校准：noise p50={p50:g} p95={p95:g} max={max:g} RMS"
        "（{frames} 帧）；threshold={threshold:g}；source={source}".format(
            p50=float(noise.get("p50_rms", 0.0)),
            p95=float(noise.get("p95_rms", 0.0)),
            max=float(noise.get("max_rms", 0.0)),
            frames=noise.get("frame_count", 0),
            threshold=float(details.get("threshold_rms", 0.0)),
            source=details.get("source", "unknown"),
        )
    )


def _print_vad_facts(session: AutoVoiceSession) -> None:
    """打印 Silero 检测器的机械事实（模型、阈值、窗口、采样率）；不含任何音频内容。"""

    details = session.vad_details()
    if not details:
        return
    print(
        "Silero VAD 检测器：model={model}（文件存在={present}）threshold={threshold:g} "
        "min_silence={silence:g}s min_speech={speech:g}s window={window} samples"
        "@{rate}Hz".format(
            model=details.get("model_name", "unknown"),
            present="是" if details.get("model_present") else "否",
            threshold=float(details.get("threshold", 0.0)),
            silence=float(details.get("min_silence_duration", 0.0)),
            speech=float(details.get("min_speech_duration", 0.0)),
            window=details.get("window_samples", 0),
            rate=details.get("sample_rate_hz", 0),
        )
    )


def _auto_capture_facts(config: AutoRunnerConfig, session: AutoVoiceSession) -> dict[str, Any]:
    """artifact 的自动收音段：``energy`` 模式保持历史字段与顺序不变。"""

    facts: dict[str, Any] = {
        "speech_threshold_rms": config.speech_threshold,
        "silence_ms": config.silence_ms,
        "speech_start_timeout_ms": config.speech_start_timeout_ms,
        "max_record_seconds": config.max_record_seconds,
        "audio_capacity_frames": AUDIO_CAPACITY_FRAMES,
        "input_stall_ms": config.input_stall_ms,
        "min_utterance_ms": config.min_utterance_ms,
        "kws_mode": config.kws_mode,
    }
    if config.kws_mode == "gate":
        facts["keyword"] = config.keyword
        facts["kws_threshold"] = config.kws_threshold
        facts["kws_wait_seconds"] = config.kws_wait_seconds
    if config.voiceprint_mode == "filter":
        facts["voiceprint_threshold"] = config.voiceprint_threshold
        facts["voiceprint_seconds"] = config.voiceprint_seconds
        facts["voiceprint"] = session.voiceprint_details()
    if config.vad_mode == "silero":
        details = session.vad_details()
        facts["vad_mode"] = "silero"
        facts["vad_model"] = details.get("model_name", DEFAULT_VAD_MODEL.name)
        facts["vad_threshold"] = details.get("threshold")
        facts["vad_min_silence_duration"] = details.get("min_silence_duration")
        facts["vad_min_speech_duration"] = details.get("min_speech_duration")
        facts["vad_window_samples"] = details.get("window_samples")
        facts["vad_sample_rate_hz"] = details.get("sample_rate_hz")
        facts["vad_min_silence_ms"] = config.vad_min_silence_ms
        # 待修 B：段级端点是否真正接线（无段级 API 的替身/旧版本为 False）。
        facts["segment_endpoint_available"] = bool(details.get("segment_api_available"))
        facts["segment_probe_errors"] = details.get("segment_probe_errors")
        facts["segment_flush_calls"] = details.get("flush_calls")
        facts["energy_threshold_effective"] = False
        return facts
    if config.threshold_mode != "auto":
        return facts
    details = session.threshold_details()
    noise = details.get("noise")
    noise_map = noise if isinstance(noise, dict) else {}
    facts["speech_threshold_rms"] = details.get("threshold_rms", config.speech_threshold)
    facts["threshold_mode"] = "auto"
    facts["threshold_multiplier"] = config.threshold_multiplier
    facts["threshold_source"] = details.get("source")
    facts["noise_frame_count"] = noise_map.get("frame_count")
    facts["noise_p50_rms"] = noise_map.get("p50_rms")
    facts["noise_p95_rms"] = noise_map.get("p95_rms")
    facts["noise_max_rms"] = noise_map.get("max_rms")
    return facts


def _build_hud() -> Any | None:
    """创建右下角状态提示窗；不可用时返回 None 并如实提示。"""

    try:
        from app.ui.voice_hud import VoiceStatusHud, build_win32_api

        hud = VoiceStatusHud(api=build_win32_api())
        hud.show()
        return hud
    except Exception as exc:  # noqa: BLE001 - 提示窗失败不阻塞试听
        print(f"提示窗不可用（{type(exc).__name__}: {exc}），本次不使用右下角提示。", file=sys.stderr)
        return None


class HudTimeline:
    """提示窗可见状态的**单调时间线**（只读展示层事实，绝不影响对话）。

    只记录**变化**（相邻重复状态去重）、条数有上限、只保存内部状态名（9 态）与相对毫秒，
    因此可以复核"每个阶段真实可见多久"，而不是靠观感判断。
    """

    MAX_ENTRIES = 64

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at: float | None = None
        self._entries: list[dict[str, Any]] = []
        self._truncated = False

    def reset(self) -> None:
        self._started_at = self._clock()
        self._entries = []
        self._truncated = False

    def record(self, state_name: object) -> None:
        """记录一次状态（非字符串忽略；与上一状态相同则忽略）。"""

        if not isinstance(state_name, str) or not state_name:
            return
        if self._started_at is None:
            self._started_at = self._clock()
        if self._entries and self._entries[-1]["state"] == state_name:
            return
        if len(self._entries) >= self.MAX_ENTRIES:
            self._truncated = True
            return
        self._entries.append(
            {"state": state_name, "t_ms": self._elapsed_ms()}
        )

    def snapshot(self) -> dict[str, Any]:
        """只读快照：条目（状态 + 相对毫秒）、每个状态累计可见毫秒、是否被截断。"""

        durations: dict[str, int] = {}
        for index, entry in enumerate(self._entries):
            if index + 1 < len(self._entries):
                span = self._entries[index + 1]["t_ms"] - entry["t_ms"]
            else:
                span = self._elapsed_ms() - entry["t_ms"]
            durations[entry["state"]] = durations.get(entry["state"], 0) + max(0, span)
        return {
            "entries": [dict(entry) for entry in self._entries],
            "durations_ms": durations,
            "truncated": self._truncated,
        }

    def _elapsed_ms(self) -> int:
        if self._started_at is None:
            return 0
        return int(round((self._clock() - self._started_at) * 1000))


class _TimelineHud:
    """提示窗的时间线代理：先记录状态，再原样转发给真实提示窗（可为 ``None``）。

    ``--no-hud`` 或建窗失败时内层为 ``None``：状态依然被记录（时间线不依赖窗口是否存在），
    只是不再有真实绘制。任何记录失败都被隔离，绝不影响语音链路。
    """

    def __init__(self, hud: Any | None, timeline: HudTimeline) -> None:
        self._hud = hud
        self.timeline = timeline

    def set_state(self, state: Any, *, turn_index: int | None = None) -> Any:
        try:
            self.timeline.record(getattr(state, "value", None))
        except Exception:  # noqa: BLE001 - 展示层诊断绝不影响对话
            pass
        if self._hud is None:
            return getattr(state, "value", "")
        return self._hud.set_state(state, turn_index=turn_index)

    def tick(self) -> Any:
        return 0 if self._hud is None else self._hud.tick()

    def hide(self) -> None:
        if self._hud is not None:
            self._hud.hide()

    def destroy(self) -> None:
        if self._hud is not None:
            self._hud.destroy()


def _wrap_loop_for_hud(loop: Any, hud: Any | None, *, index: int) -> dict[str, Any]:
    """把真实阶段挂到 ``ChatVoiceLoop`` 的对应动作上，并返回可变追踪字典。

    实现只有一份：``app/ui/voice_status.attach_stage_hooks``（``app/main.py` 的语音入口
    用的是同一个实现）。这里只负责把 HUD 状态/抽消息接到 runner 自己的隔离包装
    （``_hud_set`` / ``_hud_tick``）与时间线代理上。

    与"一轮结束后按结果补设状态"不同，这里在**真实发生的那一刻**下发：
    ``release_input`` → 识别中；``complete_turn`` → 思考中（路由/Agent）；
    ``_speak`` → 播报中；阻塞段前后与播放推片处抽窗口消息。
    """

    if hud is None:
        return {"spoken": False}
    from app.ui.voice_status import attach_stage_hooks

    return attach_stage_hooks(
        loop,
        set_state=lambda name: _hud_set(hud, name, turn_index=index),
        tick=lambda: _hud_tick(hud),
        index=index,
    )


def _restore_loop_hooks(loop: Any) -> None:
    """恢复 ``ChatVoiceLoop`` 的方法（避免同一实例被重复包装）。"""

    from app.ui.voice_status import restore_stage_hooks

    restore_stage_hooks(loop)


def run_turn(
    index: int,
    session: AutoVoiceSession,
    hud: Any | None = None,
    *,
    attempt: int | None = None,
) -> dict[str, Any]:
    """驱动一轮免按键语音，返回可持久化的机械事实（不含识别文本）。

    提示窗状态一律挂在**真实动作**上：监听→听到了→识别中→思考中→播报中→监听。
    ``attempt`` 只用于显示：被丢弃的轮次不占有效轮号，控制台会标注"第 N 次尝试"。
    """

    if attempt is not None and attempt != index:
        print(f"\n--- 第 {index} 轮（第 {attempt} 次尝试）---")
    else:
        print(f"\n--- 第 {index} 轮 ---")
    timeline = getattr(hud, "timeline", None)
    if timeline is not None:
        timeline.reset()
    state: dict[str, bool] = {"listening": False, "speech": False}
    _hud_set(hud, "LISTENING", turn_index=index)

    def on_listening() -> None:
        state["listening"] = True
        _safe_print(
            f"● 正在监听…请自然开口说话（说完停顿 {session._driver.config.silence_ms / 1000:.1f}s 自动结束）"
        )
        _hud_set(hud, "LISTENING", turn_index=index)

    def on_speech_started() -> None:
        state["speech"] = True
        _safe_print("  ▶ 检测到语音，继续收音…")
        _hud_set(hud, "HEARING", turn_index=index)

    trace = _wrap_loop_for_hud(session._loop, hud, index=index)
    try:
        facts = session.run_turn(
            on_listening=on_listening, on_speech_started=on_speech_started, hud=hud
        )
    finally:
        _restore_loop_hooks(session._loop)
    _hud_tick(hud)
    record = _turn_record(facts)
    # 待修 B：结束判定时间线（只读机械事实）落 artifact，供真机复核"最后一个字被谁切掉"。
    record["end_decision"] = session.observation_details()
    _print_end_timeline(record["end_decision"])
    if timeline is not None:
        # 提示窗阶段时间线（内部 9 态 + 单调毫秒），用于复核"每个阶段真实可见多久"。
        record["hud_timeline"] = timeline.snapshot()
        _print_hud_timeline(record["hud_timeline"])
    if facts.reason == NO_SPEECH_REASON or facts.end_reason == "no_speech":
        print("  未检测到语音：本轮丢弃，不提交、不播报（本轮不算有效轮次）。")
        if session.vad_mode == "silero":
            print("  提示：Silero VAD 未判为人声；请靠近麦克风、关掉背景音乐/噪声后重试。")
        else:
            print(
                "  提示：若你确实说了话，请降低阈值重试，例如 "
                f"--speech-threshold {int(max(200, session._driver.config.speech_threshold / 2))}"
            )
    elif facts.end_reason == "input_stalled":
        print(
            f"  输入停滞：已开始收音但连续 {facts.idle_seconds:.1f}s 收不到任何音频帧"
            "（麦克风断开或采集总线失效）——本轮已安全丢弃，请检查设备后重试。"
        )
    elif facts.end_reason == "voiceprint_rejected":
        verification = (session.observation_details() or {}).get("speaker_verification") or {}
        score = verification.get("score") if isinstance(verification, dict) else None
        detail = (
            f"（score={float(score):.3f}，阈值 {session._config.voiceprint_threshold:g}）"
            if isinstance(score, (int, float))
            else ""
        )
        print(
            f"  声纹不匹配：按误触发丢弃本轮{detail}——不提交、不播报（**未跑 ASR**）。"
        )
    elif facts.asr_text:
        print(f"  识别结果：{facts.asr_text!r}")
    elif facts.reason.startswith("start_rejected"):
        print(f"  未能开始收音：{facts.reason}")
    else:
        print("  未得到可靠的识别结果（本轮不提交、不播报）。")
    if facts.accepted:
        print(
            f"  结果：accepted={facts.accepted} submitted={facts.submitted} "
            f"spoken={facts.spoken} reason={facts.reason}"
        )
    if facts.spoken or facts.reply_text:
        _print_speaker_diagnostics(getattr(session._loop, "_speaker", None))
    # 一轮结束后回到"正在监听"（试听模式会继续下一轮）；只有失败才显示失败状态。
    if facts.accepted or trace.get("spoken"):
        _hud_set(hud, "LISTENING", turn_index=index)
    else:
        _hud_set(hud, "FAILED", turn_index=index)
    return record


def _ask_user_heard(args: argparse.Namespace) -> str:
    if args.user_heard != "unknown":
        return args.user_heard
    try:
        answer = input("你听到语音回复了吗？[y/n/回车跳过] ").strip().lower()
    except EOFError:
        return "unknown"
    if answer.startswith("y"):
        return "yes"
    if answer.startswith("n"):
        return "no"
    return "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_console()
    args = _parse_args(argv)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in（真机试听必须显式开启）", file=sys.stderr)
        print("该 runner 会打开本机麦克风、加载本地 ASR 模型并通过扬声器播报。", file=sys.stderr)
        return EXIT_NOT_OPTED_IN

    try:
        config = _config_from_args(args)
    except ValueError as exc:
        print(f"参数非法：{exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    print("P9 真机免按键自动收音试听")
    print("边界：不操作电脑、不创建计划/授权/执行、不联网（仅本机 loopback TTS）、不落盘原始音频。")
    print(f"TTS 模式：{config.tts_mode}；目标轮数：{config.turns}")
    if config.vad_mode == "silero":
        print(
            "VAD 模式：silero（sherpa-onnx Silero VAD 人声判定；"
            f"模型：{config.vad_model_path or DEFAULT_VAD_MODEL}）"
        )
        print("由 Silero VAD 判定，能量阈值不生效。")
        if config.threshold_mode == "auto":
            print("说明：silero 模式下不采样环境静音，--threshold-mode auto 不生效。")
    else:
        print("VAD 模式：energy（沿用能量判定，与历史行为一致）")
    if config.threshold_mode == "auto" and config.vad_mode == "energy":
        print(
            f"阈值模式：auto（第一轮开始前采样环境静音自适应定阈值，"
            f"multiplier={config.threshold_multiplier:g}；--speech-threshold 不生效）"
        )
    if config.vad_mode == "silero":
        print(
            f"自动收音：起始由 Silero VAD 人声命中确认；静音结束 {config.silence_ms} ms；"
            f"开口超时 {config.speech_start_timeout_ms} ms"
        )
    else:
        print(
            f"自动收音：起始需连续命中 {config.speech_threshold:g} RMS 阈值；"
            f"静音结束 {config.silence_ms} ms；开口超时 {config.speech_start_timeout_ms} ms"
        )

    try:
        environment = build_environment(config)
    except RunnerError as exc:
        print(f"前置检查失败：{exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    except Exception as exc:  # noqa: BLE001 - 依赖/设备/模型问题统一按前置失败
        print(f"前置检查失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    print(f"输入设备：{environment.details.get('device')}（{environment.details.get('device_id')}）")
    print(f"ASR 模型：{environment.details.get('model')}")
    try:
        environment.preflight()
    except Exception as exc:  # noqa: BLE001 - 本机 TTS 不可用按前置失败
        print(f"本机 TTS 前置检查失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if args.check_only:
        print("--check-only：只做环境检查，未打开麦克风、未播报、未写 artifact。")
        return EXIT_OK

    try:
        _agent, session = build_session(config, environment)
    except VadUnavailableError as exc:
        print(f"Silero VAD 不可用（{exc}）：已按 fail closed 退出，未开始监听。", file=sys.stderr)
        print(
            "提示：确认 Silero 模型文件存在且 sherpa-onnx 可用；"
            "或显式用 --vad-mode energy 退回能量判定（音乐/噪声环境下不推荐）。",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT_FAILED
    except WakeWordUnavailableError as exc:
        print(f"唤醒词门禁不可用（{exc}）：已按 fail closed 退出，未开始监听。", file=sys.stderr)
        if str(exc) == "keywords_missing":
            print(
                "原因：没有可用的关键词文件。请在 --kws-model-dir 下放 keywords.txt，"
                "或用 --kws-keywords-file 指向它（格式示例：x iǎo b ǐng @小饼）。",
                file=sys.stderr,
            )
        else:
            print(
                "提示：确认 --kws-model-dir 下的 tokens.txt/encoder.onnx/decoder.onnx/joiner.onnx 齐备，"
                "且 sherpa-onnx 可用；或显式用 --kws-mode off 关闭唤醒词门禁（默认即 off）。",
                file=sys.stderr,
            )
        return EXIT_PREFLIGHT_FAILED
    except VoiceprintUnavailableError as exc:
        print(f"声纹过滤不可用（{exc}）：已按 fail closed 退出，未开始监听。", file=sys.stderr)
        print(
            "提示：--voiceprint-mode filter 需要 --voiceprint-model 指向声纹 onnx 模型，"
            '且 --voiceprint-profile 指向已注册档案（{"embeddings": [[...]]}）；'
            "或显式用 --voiceprint-mode off 关闭过滤（默认即 off）。",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT_FAILED
    except ThresholdCalibrationError as exc:
        print(f"环境自适应阈值校准失败：{exc}", file=sys.stderr)
        print(
            "提示：请关掉音乐/风扇，换一个更安静的位置后重试；"
            "或用 --threshold-mode fixed 显式固定阈值（不推荐在嘈杂环境使用）。",
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT_FAILED
    except RunnerError as exc:
        print(f"闭环装配失败：{exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    except Exception as exc:  # noqa: BLE001 - 装配失败按前置失败
        print(f"闭环装配失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    if config.vad_mode == "silero":
        _print_vad_facts(session)
    elif config.threshold_mode == "auto":
        _print_threshold_calibration(session)

    # 右下角"正在对话"状态提示窗（默认开启；不可用时如实提示并继续）。
    window_hud = _build_hud() if config.hud else None
    # 阶段时间线总是可用：即使没有真实窗口（--no-hud 或建窗失败），也照常记录
    # 内部 9 态与单调时间戳，供 artifact 复核真实阶段耗时。
    hud: Any = _TimelineHud(window_hud, HudTimeline())
    if window_hud is not None:
        _hud_set(hud, "IDLE")
        print("提示窗：右下角「正在对话」状态提示已启用（不可点击、可拖动）。")

    print("\n提示：本 runner 会持续监听，不需要按键；直接自然说话即可。按 Ctrl+C 可随时结束。")
    turns: list[dict[str, Any]] = []
    failed = False
    interrupted = False
    valid = 0
    attempt = 0
    consecutive_discards = 0
    over_limit_hits = 0
    try:
        while valid < config.turns:
            index = valid + 1
            attempt += 1
            try:
                record = run_turn(index, session, hud, attempt=attempt)
            except KeyboardInterrupt:
                interrupted = True
                break
            except Exception as exc:  # noqa: BLE001 - 单轮异常不终止整批试听
                failed = True
                turns.append({"turn": index, "error": f"{type(exc).__name__}: {exc}"})
                print(f"本轮失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                break
            turns.append(record)
            end_reason = record.get("end_reason")
            if record.get("reason") == NO_SPEECH_REASON or end_reason == "no_speech":
                consecutive_discards += 1
                if args.no_speech_limit is not None and consecutive_discards >= args.no_speech_limit:
                    if config.vad_mode == "silero":
                        print(
                            f"连续 {consecutive_discards} 轮没等到 Silero VAD 判为人声，停止试听"
                            "（请靠近麦克风、关掉背景音乐/风扇后重试）。",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"连续 {consecutive_discards} 轮没等到有效语音，停止试听"
                            "（可降低 --speech-threshold 或缩短 --speech-start-timeout-ms 后重试）。",
                            file=sys.stderr,
                        )
                    break
                continue
            if end_reason == "max_utterance_duration":
                # 一直没等到静音结束：环境持续噪声（风扇、音乐、说话声）会这样。
                # 只要本轮仍然提交成功，就不算无效轮次——只是结束方式不是自动静音。
                if record.get("submitted"):
                    print(
                        "  说明：本轮已提交成功，但结束方式是撞到最长收音上限"
                        f"（这段音频没有出现持续 {config.silence_ms} ms 的静音）。"
                    )
                    consecutive_discards = 0
                    valid += 1
                    continue
                over_limit_hits += 1
                print(
                    "  注意：本轮是撞到最长收音上限才结束的，说明这段音频一直没出现"
                    f"持续 {config.silence_ms} ms 的静音——通常是环境音乐/风扇/说话声。"
                )
                if args.over_limit_limit is not None and over_limit_hits >= args.over_limit_limit:
                    if config.vad_mode == "silero":
                        print(
                            f"连续 {over_limit_hits} 轮撞到最长收音上限，停止试听"
                            "（Silero VAD 一直判为人声：请关掉音乐/风扇，"
                            "或缩短 --max-record-seconds）。",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"连续 {over_limit_hits} 轮撞到最长收音上限，停止试听"
                            "（请关掉音乐/风扇，或提高 --speech-threshold 后重试）。",
                            file=sys.stderr,
                        )
                    break
            consecutive_discards = 0
            valid += 1
    finally:
        session.close()
        _hud_set(hud, "STOPPED")
        if hud is not None:
            try:
                hud.hide()
                hud.destroy()
            except Exception:  # noqa: BLE001 - 关闭提示窗失败不影响退出
                pass

    if interrupted:
        print("\n已中断（Ctrl+C）。")
        return EXIT_INTERRUPTED

    heard = _ask_user_heard(args)
    batch_id = (
        f"p9-auto-voice-loop-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        f"-{uuid.uuid4().hex[:16]}"
    )
    result = {
        "batch_id": batch_id,
        "evidence_kind": "p9_auto_voice_loop_acceptance",
        "live_opt_in": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "tts_mode": config.tts_mode,
        "agent": "local_scripted_echo（非真实模型）",
        "auto_capture": _auto_capture_facts(config, session),
        "turns": turns,
        "valid_turns": valid,
        "user_heard": heard,
        "user_heard_note": "仅操作员本人确认后填写；设备完成不等于有人听见",
        "boundary": {
            "audio_written_to_disk": False,
            "computer_actions_executed": False,
            "plan_or_authorization_created": False,
            "external_network_used": False,
            "recognized_text_persisted": False,
            "network_target": "127.0.0.1:3900 / local SAPI",
            "button_required": False,
        },
    }
    args.artifact_root.mkdir(parents=True, exist_ok=True)
    path = args.artifact_root / f"{batch_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nartifact={path}")
    print(f"user_heard={heard}（如需更改，请确认后另行登记）")

    submitted = [item for item in turns if item.get("submitted")]
    spoken = [item for item in turns if item.get("spoken")]
    discarded = [item for item in turns if item.get("reason") == NO_SPEECH_REASON]
    voiceprint_rejected = [
        item for item in turns if item.get("end_reason") == "voiceprint_rejected"
    ]
    print(
        f"汇总：目标轮数 {config.turns}，有效轮次 {valid}，提交 {len(submitted)}，"
        f"播报 {len(spoken)}，未检测到语音丢弃 {len(discarded)}，声纹丢弃 {len(voiceprint_rejected)}"
    )
    if not submitted:
        print(_zero_valid_summary(turns, config))
        return EXIT_TURN_FAILED
    if failed:
        return EXIT_TURN_FAILED
    if config.tts_mode != "none" and not spoken:
        return EXIT_TURN_FAILED
    return EXIT_OK


def _zero_valid_summary(turns: list[dict[str, Any]], config: AutoRunnerConfig) -> str:
    """没有任何轮次被提交时，给出对症的提示（声纹全丢 / VAD 未判人声 / 能量阈值）。

    真机教训（2026-10-01）：声纹把三轮全丢掉时，旧提示会误报"Silero VAD 未判为人声"。
    """

    rejected = [item for item in turns if item.get("end_reason") == "voiceprint_rejected"]
    if turns and rejected and len(rejected) == len(turns):
        return (
            f"没有任何轮次被提交：{len(rejected)} 轮全部被**声纹过滤**丢弃"
            f"（阈值 {config.voiceprint_threshold:g}）——说明说话的不是已注册的说话人。"
            "这是预期行为，不是故障（要放行请调低 --voiceprint-threshold）。"
        )
    if config.vad_mode == "silero":
        return (
            "没有任何轮次被提交：Silero VAD 未判为人声或识别未被接纳，"
            "请确认麦克风位置、环境噪声与 ASR 模型后重试。"
        )
    return (
        "没有任何轮次被提交：请确认麦克风位置与音量，或降低阈值后重试"
        f"（--speech-threshold {int(max(200, config.speech_threshold / 2))}）。"
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live-opt-in", action="store_true", help="必需：确认打开麦克风与扬声器")
    parser.add_argument("--turns", type=_positive_int, default=2, help="目标有效轮数（默认 2）")
    parser.add_argument(
        "--tts-mode", choices=TTS_MODES, default="ws_pcm", help="本次运行使用的 TTS 通道"
    )
    parser.add_argument("--edge-voice", default="Microsoft Huihui Desktop", help="edge 模式的本机声线名")
    parser.add_argument("--device-id", default=None, help="固定输入设备 id（默认系统默认设备）")
    parser.add_argument("--silence-ms", type=_positive_int, default=2500, help="说完后的静音结束阈值")
    parser.add_argument(
        "--speech-start-timeout-ms",
        type=_positive_int,
        default=8000,
        help="一轮内等待你开口的时长（超时丢弃本轮）",
    )
    parser.add_argument(
        "--max-record-seconds", type=_positive_int, default=15, help="单轮最长收音时间（默认 15）"
    )
    parser.add_argument("--speech-threshold", type=float, default=600.0, help="语音能量阈值（PCM16 RMS）")
    parser.add_argument(
        "--threshold-mode",
        choices=THRESHOLD_MODES,
        default="fixed",
        help="阈值来源：fixed 固定（默认，与历史行为一致）或 auto 采样环境静音自适应",
    )
    parser.add_argument(
        "--threshold-multiplier",
        type=_threshold_multiplier,
        default=3.0,
        help="auto 模式的噪声分位数倍率（>1，默认 3.0）",
    )
    parser.add_argument(
        "--vad-mode",
        choices=VAD_MODES,
        default="silero",
        help="语音判定：silero（默认，sherpa-onnx Silero VAD 人声判定）或 energy（能量阈值）",
    )
    parser.add_argument(
        "--vad-model",
        default=str(DEFAULT_VAD_MODEL),
        help="Silero VAD onnx 路径（默认项目内 models/openwakeword/silero_vad.onnx）",
    )
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=0.5,
        help="Silero VAD 人声判定阈值（0~1，默认 0.5；环境音乐多时可用 0.7 收紧）",
    )
    parser.add_argument(
        "--vad-frames",
        type=_positive_int,
        default=2,
        help="连续多少次窗口命中才确认语音起始（默认 2；环境音乐多时可调到 4~6）",
    )
    parser.add_argument(
        "--vad-min-silence-ms",
        type=_positive_int,
        default=500,
        help="段级端点宽限（毫秒，默认 500）：Silero 在人声段结束后判定「说完了」所需的静音时长；"
        "句中停顿常被误判时可调到 600~800",
    )
    parser.add_argument(
        "--input-stall-ms",
        type=_positive_int,
        default=INPUT_STALL_MS,
        help="已开始收音但连续这么久收不到任何音频帧就结束本轮（毫秒，默认 5000）",
    )
    parser.add_argument(
        "--min-utterance-ms",
        type=_positive_int,
        default=300,
        help="已确认起始后至少收这么久才允许判定「说完了」（毫秒，默认 300）",
    )
    parser.add_argument(
        "--kws-mode",
        choices=KWS_MODES,
        default="off",
        help="唤醒词门禁：off（默认，不启用）或 gate（必须听到唤醒词才开启新的输入会话）",
    )
    parser.add_argument(
        "--kws-model-dir",
        default=None,
        help="唤醒词模型目录（含 tokens.txt/encoder.onnx/decoder.onnx/joiner.onnx，可选 keywords.txt）",
    )
    parser.add_argument(
        "--kws-keywords-file",
        default=None,
        help="唤醒词关键词文件（默认自动用 <kws-model-dir>/keywords.txt）",
    )
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD, help=f"唤醒词文本（默认 {DEFAULT_KEYWORD}）")
    parser.add_argument(
        "--kws-threshold",
        type=float,
        default=0.25,
        help="唤醒词触发阈值（0~1，越大越难触发；默认 0.25）",
    )
    parser.add_argument(
        "--kws-wait-seconds",
        type=_positive_int,
        default=30,
        help="每轮等待唤醒词的最长秒数（默认 30；超时本轮不开启收音）",
    )
    parser.add_argument(
        "--voiceprint-mode",
        choices=VOICEPRINT_MODES,
        default="off",
        help="声纹误触发过滤：off（默认）或 filter（必须在 --voiceprint-profile 注册过声纹）",
    )
    parser.add_argument("--voiceprint-model", default=None, help="声纹嵌入 onnx 模型路径（filter 模式必需）")
    parser.add_argument(
        "--voiceprint-profile",
        default=None,
        help='声纹档案 JSON（{"speaker": "...", "embeddings": [[...]]}；filter 模式必需）',
    )
    parser.add_argument(
        "--voiceprint-threshold",
        type=float,
        default=0.5,
        help="声纹余弦相似度阈值（0~1，越大越严；默认 0.5）",
    )
    parser.add_argument(
        "--voiceprint-seconds",
        type=_positive_int,
        default=8,
        help="声纹判定使用本轮最近多少秒音频（默认 8）",
    )
    parser.add_argument(
        "--no-hud",
        action="store_true",
        help="不显示右下角「正在对话」状态提示窗（默认显示；窗口不可用时自动跳过）",
    )
    parser.add_argument("--model-path", default=None, help="SenseVoice onnx 路径覆盖")
    parser.add_argument("--tokens-path", default=None, help="SenseVoice tokens 路径覆盖")
    parser.add_argument("--check-only", action="store_true", help="只做环境检查，不打开麦克风")
    parser.add_argument(
        "--no-speech-limit",
        type=_positive_int,
        default=3,
        help="连续多少轮没等到有效语音后停止（默认 3）",
    )
    parser.add_argument(
        "--over-limit-limit",
        type=_positive_int,
        default=2,
        help="连续多少轮撞到最长收音上限后停止（默认 2，防止音乐/噪声把试听拖长）",
    )
    parser.add_argument(
        "--user-heard",
        choices=("yes", "no", "unknown"),
        default="unknown",
        help="操作员听感结论；unknown 时会在结束时询问",
    )
    parser.add_argument("--artifact-root", type=Path, default=ARTIFACT_ROOT, help="artifact 输出目录")
    return parser.parse_args(argv)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _threshold_multiplier(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 1.0:
        raise argparse.ArgumentTypeError("必须是大于 1 的数值")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
