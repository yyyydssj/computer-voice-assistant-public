r"""P8 真机按键语音闭环验收 runner（需要 ``--live-opt-in``）。

只验证一条真实链路（装配全部来自 `app.runtime.voice_entry`，与主程序共用同一份）：

```text
按 Enter 开始说话 → 真实麦克风采集 → 本地 sherpa-onnx SenseVoice 识别
→ 同一个 ConversationManager → 本地脚本 Agent 回复 → 真实 TTS 播报
```

硬边界（与 P8 阶段验收一致）：

- 没有 ``--live-opt-in`` 时立即以退出码 2 结束，**不导入** sounddevice/sherpa-onnx、
  不打开设备、不读 ``.env``、不联网；
- 不创建计划、授权、账本或执行事实，不调用任何工具，不操作电脑；
- 原始音频不落盘、不保存；识别文本只打印到控制台，artifact 里只记长度与 SHA-256；
- TTS 只允许本机 loopback 端点（127.0.0.1:3900）或本机离线声线；
- Agent 是**本地脚本替身**（回显识别结果），不代表真实模型；真实 Provider 属 R 支线授权门；
- 未检测到语音就丢弃本轮（SenseVoice 对静音可能产生幻觉文本，绝不提交/播报）。

用法：

```powershell
.\.venv\Scripts\python.exe tools\p8_real_voice_loop_runner.py --live-opt-in --check-only
.\.venv\Scripts\python.exe tools\p8_real_voice_loop_runner.py --live-opt-in --turns 2
.\.venv\Scripts\python.exe tools\p8_real_voice_loop_runner.py --live-opt-in --tts-mode edge
.\.venv\Scripts\python.exe tools\p8_real_voice_loop_runner.py --live-opt-in --tts-mode none
```

前置依赖（已离线安装并登记哈希，见 P8 验收文档 6.1）：`sounddevice 0.5.6`、
`sherpa-onnx 1.13.8`(+core)、`numpy 2.5.3`；模型位于 `models/sense-voice/`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.agent.adapter import AgentTurnOutcome  # noqa: E402
from app.conversation import ConversationManager  # noqa: E402
from app.runtime.assistant_runtime import LocalChatRouter  # noqa: E402
from app.runtime.voice_entry import (  # noqa: E402
    FRAME_MS,
    NO_SPEECH_REASON,
    TTS_MODES,
    VOICE_WS_ENDPOINT,
    VoiceEntryConfig,
    VoiceEntryEnvironment,
    VoiceEntryError,
    VoiceLoopSession,
    VoiceTurnFacts,
    build_real_environment,
    energy_detector,
)

Environment = VoiceEntryEnvironment
ARTIFACT_ROOT = _REPO / "implementation_plan" / "checkpoints" / "artifacts" / "2026-09-30-P8-real-voice-loop"

EXIT_OK = 0
EXIT_TURN_FAILED = 1
EXIT_NOT_OPTED_IN = 2
EXIT_PREFLIGHT_FAILED = 3


class RunnerError(RuntimeError):
    """真机验收环境不满足前置条件。"""


@dataclass
class ScriptedAgent:
    """本地脚本 Agent：只回显识别结果，不调用模型、不联网。"""

    calls: list[dict[str, str]] = field(default_factory=list)

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.calls.append({"request_id": request_id, "session_id": session_id, "text": text})
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"我听到你说：{text}。这是本机按键语音闭环的真机验收回复。",
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


def config_from_args(args: argparse.Namespace) -> VoiceEntryConfig:
    return VoiceEntryConfig(
        tts_mode=args.tts_mode,
        device_id=args.device_id,
        silence_ms=args.silence_ms,
        speech_timeout_seconds=args.speech_timeout_seconds,
        max_record_seconds=args.max_record_seconds,
        speech_threshold=args.speech_threshold,
        model_path=Path(args.model_path) if args.model_path else None,
        tokens_path=Path(args.tokens_path) if args.tokens_path else None,
        edge_voice=args.edge_voice,
    )


def build_environment(args: argparse.Namespace) -> Environment:
    """真实环境装配；只有 ``--live-opt-in`` 之后才会被调用。"""

    return build_real_environment(config_from_args(args))


def build_session(args: argparse.Namespace, environment: Environment) -> tuple[ScriptedAgent, VoiceLoopSession]:
    """装配验收用闭环：真实采集/ASR/TTS + 本地脚本 Agent。"""

    agent = ScriptedAgent()
    conversation = ConversationManager(
        agent,
        router=LocalChatRouter(),
        router_provider="local",
        router_model_id="p8-real-scripted-agent",
        router_model_version="1",
    )
    session = VoiceLoopSession(
        conversation=conversation,
        environment=environment,
        config=config_from_args(args),
        source_instance_id="p8-real-runner",
    )
    return agent, session


def _boundary_facts(boundary: object) -> dict[str, str]:
    if boundary is None:
        return {}
    return {
        "voice_session_id": boundary.voice_session_id,  # type: ignore[attr-defined]
        "utterance_id": boundary.utterance_id,  # type: ignore[attr-defined]
        "turn_id": boundary.turn_id,  # type: ignore[attr-defined]
        "request_id": boundary.request_id,  # type: ignore[attr-defined]
        "reply_id": boundary.reply_id,  # type: ignore[attr-defined]
    }


def _turn_record(facts: VoiceTurnFacts, *, index: int, session: VoiceLoopSession) -> dict[str, Any]:
    record: dict[str, Any] = {
        "turn": index,
        "boundary": facts.boundary_facts(),
        "frames": facts.frames,
        "speech_seen": facts.speech_seen,
        "end_reason": facts.end_reason,
        "accepted": facts.accepted,
        "submitted": facts.submitted,
        "spoken": facts.spoken,
        "reason": facts.reason,
        "seconds": facts.seconds,
    }
    if facts.asr_text is not None:
        record["asr_text_length"] = len(facts.asr_text)
        record["asr_text_sha256"] = hashlib.sha256(facts.asr_text.encode("utf-8")).hexdigest()
    else:
        record["asr_text_length"] = 0
        record["asr_text_sha256"] = None
    records = session.speaker_records
    if records:
        record["tts"] = records[-1]
    return record


def _use_utf8_console() -> None:
    """让控制台能打印任意识别文本（SenseVoice 在静音上可能输出非中文字符）。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 输出编码只是尽力而为
                pass


def _wait_for_enter(prompt: str) -> None:
    print(prompt)
    try:
        input()
    except EOFError:
        print("（当前标准输入不可交互，直接开始收音）")


def _drain_keyboard() -> None:
    try:
        import msvcrt
    except ImportError:
        return
    try:
        while msvcrt.kbhit():
            msvcrt.getwch()
    except Exception:  # noqa: BLE001 - 无控制台时忽略
        return


def _enter_pressed() -> bool:
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        if not msvcrt.kbhit():
            return False
        return msvcrt.getwch() in ("\r", "\n", " ")
    except Exception:  # noqa: BLE001 - 无控制台时忽略
        return False


def run_turn(args: argparse.Namespace, index: int, session: VoiceLoopSession) -> dict[str, Any]:
    """驱动一轮真实按键语音轮次，返回可持久化的机械事实（不含识别文本）。"""

    print(f"\n--- 第 {index} 轮 ---")
    _wait_for_enter("按 Enter 开始说话（随后请立即开口，例如：你好，测试一下）…")
    _drain_keyboard()
    print(
        f"● 正在收音…（说完停顿 {args.silence_ms / 1000:.1f}s 自动结束，"
        f"或再按 Enter 立即结束；最长 {args.max_record_seconds}s）"
    )
    facts = session.run_turn(stop_requested=_enter_pressed)
    record = _turn_record(facts, index=index, session=session)

    if facts.reason == NO_SPEECH_REASON:
        print("  未检测到语音（能量始终低于阈值）：本轮丢弃，不提交、不播报。")
        print(
            "  提示：若你确实说了话，请降低阈值重试，例如 "
            f"--speech-threshold {int(args.speech_threshold / 2)}"
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
        print("refusing to run without --live-opt-in（真机验收必须显式开启）", file=sys.stderr)
        print("该 runner 会打开本机麦克风、加载本地 ASR 模型并通过扬声器播报。", file=sys.stderr)
        return EXIT_NOT_OPTED_IN

    print("P8 真机按键语音闭环验收")
    print("边界：不操作电脑、不创建计划/授权/执行、不联网（仅本机 loopback TTS）、不落盘原始音频。")
    print(f"TTS 模式：{args.tts_mode}；轮数：{args.turns}；静音结束阈值：{args.silence_ms} ms")

    try:
        environment = build_environment(args)
        environment.preflight()
    except RunnerError as exc:
        print(f"前置检查失败：{exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    except VoiceEntryError as exc:
        print(f"前置检查失败：{exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    except Exception as exc:  # noqa: BLE001 - 环境问题统一按前置失败处理
        print(f"前置检查异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    print(f"输入设备：{environment.details.get('device')}（{environment.details.get('device_id')}）")
    print(f"ASR 模型：{environment.details.get('model')}")
    if args.check_only:
        print("--check-only：只做环境检查，未打开麦克风、未播报。")
        return EXIT_OK

    try:
        _agent, session = build_session(args, environment)
    except VoiceEntryError as exc:
        print(f"闭环装配失败：{exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    turns: list[dict[str, Any]] = []
    failed = False
    try:
        for index in range(1, args.turns + 1):
            try:
                turns.append(run_turn(args, index, session))
            except Exception as exc:  # noqa: BLE001 - 单轮异常不终止整批验收
                failed = True
                turns.append({"turn": index, "error": f"{type(exc).__name__}: {exc}"})
                print(f"本轮失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        session.close()

    heard = _ask_user_heard(args)
    batch_id = f"p8-real-voice-loop-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:16]}"
    result = {
        "batch_id": batch_id,
        "evidence_kind": "p8_real_voice_loop_acceptance",
        "live_opt_in": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "tts_mode": args.tts_mode,
        "agent": "local_scripted_echo（非真实模型）",
        "turns": turns,
        "user_heard": heard,
        "user_heard_note": "仅操作员本人确认后填写；设备完成不等于有人听见",
        "boundary": {
            "audio_written_to_disk": False,
            "computer_actions_executed": False,
            "plan_or_authorization_created": False,
            "external_network_used": False,
            "recognized_text_persisted": False,
            "network_target": "127.0.0.1:3900 / local SAPI",
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
    print(f"汇总：轮数 {len(turns)}，提交 {len(submitted)}，播报 {len(spoken)}，未检测到语音丢弃 {len(discarded)}")
    if discarded and not submitted:
        print(
            "全部轮次都未检测到语音：请确认麦克风位置与音量，或降低阈值后重试"
            f"（--speech-threshold {int(args.speech_threshold / 2)}）。"
        )
    if failed or not submitted:
        return EXIT_TURN_FAILED
    if args.tts_mode != "none" and not spoken:
        return EXIT_TURN_FAILED
    return EXIT_OK


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--live-opt-in", action="store_true", help="必需：确认打开麦克风与扬声器")
    parser.add_argument("--turns", type=_positive_int, default=2, help="验收轮数（默认 2）")
    parser.add_argument("--tts-mode", choices=TTS_MODES, default="ws_pcm", help="本次运行使用的 TTS 通道")
    parser.add_argument("--edge-voice", default="Microsoft Huihui Desktop", help="edge 模式的本机声线名")
    parser.add_argument("--device-id", default=None, help="固定输入设备 id（默认系统默认设备）")
    parser.add_argument("--silence-ms", type=_positive_int, default=1500, help="说完后的静音结束阈值")
    parser.add_argument("--speech-timeout-seconds", type=_positive_int, default=10, help="未检测到语音的超时")
    parser.add_argument("--max-record-seconds", type=_positive_int, default=20, help="单轮最长收音时间")
    parser.add_argument("--speech-threshold", type=float, default=600.0, help="语音能量阈值（PCM16 RMS）")
    parser.add_argument("--model-path", default=None, help="SenseVoice onnx 路径覆盖")
    parser.add_argument("--tokens-path", default=None, help="SenseVoice tokens 路径覆盖")
    parser.add_argument("--check-only", action="store_true", help="只做环境检查，不打开麦克风")
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


if __name__ == "__main__":
    raise SystemExit(main())
