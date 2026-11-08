"""P10 R4-B：**播报通道自检**（判定真机为什么 `text_only`）。

背景：2026-10-03 三件套真机试听时，8 个轮次全部 `spoken=false / reason="text_only"`，
导致打断通道没启、AEC3 没跑（`runtime.jsonl` 里 `barge_in_enabled=false`）。
`text_only` 有两个可能来源，**只看日志分不出来**：

1. `voice_loop.chat_loop` 的 `speech_eligible(outcome)` 为假（回复不是"完整、无故障、
   带非空 finish_reason 的 AgentTurnOutcome"——例如被**本地路由**接走的闲聊）；
2. 走到了 `_speak()` 但 `speaker.start_stream()` 返回 ``None``（TTS 服务虽在，但没有拿到流）。

本工具用**一次真实请求**把这两条分开（默认不发请求、不打开任何流；必须 ``--live-opt-in``）：

- 只打印**结构事实**（类型名、布尔、固定短码、文本长度），**不打印回复正文、不落盘音频**；
- 打开 TTS 流后**立刻取消**，不推任何文本 ⇒ 不会真的念出来；
- 成本：1 次路由 + 1 次回复（与正常一轮相同）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WORK_TEXT = "帮我看看磁盘空间还剩多少，然后给出一个整理桌面文件的建议。"


def _safe(text: str) -> None:
    stream = sys.stdout
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
    stream.flush()


def _fact(name: str, value: Any) -> str:
    return f"  {name}: {value}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P10 R4-B 播报通道自检（需 --live-opt-in）")
    parser.add_argument("--live-opt-in", action="store_true", help="真的发 1 次真实请求并尝试打开 TTS 流")
    parser.add_argument("--tts-mode", default="ws_pcm", choices=("ws_pcm", "http_wav", "auto", "edge", "none"))
    parser.add_argument("--text", default=WORK_TEXT)
    parser.add_argument(
        "--router",
        choices=("local", "integration"),
        default="local",
        help="local=LocalChatRouter（自检默认）；integration=与真机同款（真实 transport 就绪时接管路由）",
    )
    parser.add_argument(
        "--entry",
        choices=("text", "asr"),
        default="text",
        help="text=submit_text（普通文本入口）；asr=submit_asr_final（**真机语音走的入口**，带流式回调）",
    )
    args = parser.parse_args(argv)

    from app.config.settings import load_settings

    settings = load_settings(project_root=REPO_ROOT)
    _safe("[selfcheck] 播报通道自检（只打印结构事实；不打印正文、不落盘音频）\n")
    _safe(_fact("provider", getattr(settings, "provider", None)) + "\n")
    _safe(_fact("deepseek_opt_in", getattr(settings, "deepseek_opt_in", None)) + "\n")

    from app.main import build_agent_adapter

    adapter = build_agent_adapter(settings)
    _safe(_fact("agent_adapter", type(adapter).__name__ if adapter is not None else None) + "\n")
    if adapter is None:
        _safe("[selfcheck] 没有真实 Agent（未 opt-in）→ 不构成结论，终止。\n")
        return 1

    # ---- 1) TTS 流：只开、不推文本、立刻取消
    from app.runtime.voice_entry import VoiceEntryConfig, build_real_environment

    stream_result = "not_attempted"
    environment: Any | None = None
    try:
        environment = build_real_environment(
            VoiceEntryConfig(
                tts_mode=args.tts_mode,
                aec_reference_mode="same_process",
                aec_engine="webrtc",
                speech_threshold=600.0,
            )
        )
        speaker = getattr(environment, "speaker", None)
        _safe(_fact("speaker", type(speaker).__name__ if speaker is not None else None) + "\n")
        reference = getattr(environment, "aec_reference", None)
        if reference is not None:
            canceller = getattr(reference, "canceller", None)
            _safe(
                _fact("aec_backend", getattr(reference, "backend", None))
                + _fact("aec_available", getattr(canceller, "available", None))
                + "\n"
            )
        start_stream = getattr(speaker, "start_stream", None)
        if callable(start_stream):
            stream = start_stream()
            stream_result = "none" if stream is None else f"ok:{type(stream).__name__}"
            if stream is not None:
                for closer in ("cancel", "finish"):
                    close = getattr(stream, closer, None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001 - 关闭失败不影响结论
                            stream_result += f":{closer}_failed"
                        break
        else:
            stream_result = "no_start_stream"
    except Exception as exc:  # noqa: BLE001 - 环境装配失败就是结论的一部分
        _safe(_fact("environment_error", type(exc).__name__) + "\n")
        stream_result = f"environment_error:{type(exc).__name__}"
    _safe(_fact("start_stream", stream_result) + "\n")

    if not args.live_opt_in:
        _safe("[selfcheck] 未加 --live-opt-in：不发真实请求（上面只有装配事实）。\n")
        return 0

    # ---- 2) 一次真实请求：只看 outcome 结构
    from app.conversation import ConversationManager
    from app.runtime.assistant_runtime import LocalChatRouter
    from app.voice_loop.drivers import speech_eligible

    router: Any = LocalChatRouter()
    router_metadata = None
    if args.router == "integration":
        # 与真机同款：真实 transport 就绪时由 provider integration 接管路由。
        from app.main import build_provider_integration, select_runtime_router

        integration = build_provider_integration(settings, agent_ready=True)
        effective, router_metadata = select_runtime_router(None, integration)
        router = effective if effective is not None else LocalChatRouter()
        _safe(_fact("router", type(router).__name__) + _fact("router_metadata", router_metadata) + "\n")
    else:
        _safe(_fact("router", type(router).__name__) + "\n")

    provider, model_id, model_version = router_metadata or ("local", "chat-only-router", "1")
    conversation = ConversationManager(
        adapter,
        router=router,
        router_provider=provider,
        router_model_id=model_id,
        router_model_version=model_version,
    )
    try:
        if args.entry == "asr":
            chunks: list[str] = []
            result = conversation.submit_asr_final(
                args.text,
                turn_id="selfcheck-asr-1",
                on_text_chunk=chunks.append,
            )
            _safe(_fact("stream_chunks", len(chunks)) + "\n")
        else:
            result = conversation.submit_text(args.text)
    except Exception as exc:  # noqa: BLE001 - 失败如实上报
        _safe(_fact("submit_error", type(exc).__name__) + "\n")
        return 1
    finally:
        try:
            conversation.close()
        except Exception:  # noqa: BLE001
            pass

    handoff = getattr(result, "handoff", None)
    outcome = getattr(result, "outcome", None)
    _safe(_fact("handoff_type", type(handoff).__name__ if handoff is not None else None) + "\n")
    for name in ("route", "decision", "reason", "kind"):
        value = getattr(handoff, name, None)
        if value is not None:
            _safe(_fact(f"handoff_{name}", value) + "\n")
    _safe(_fact("outcome_type", type(outcome).__name__ if outcome is not None else None) + "\n")
    if outcome is not None:
        text = getattr(outcome, "text", None)
        failure = getattr(outcome, "failure", None)
        finish = getattr(outcome, "finish_reason", None)
        _safe(_fact("completed", getattr(outcome, "completed", None)) + "\n")
        _safe(_fact("failure", type(failure).__name__ if failure is not None else None) + "\n")
        _safe(_fact("finish_reason_nonempty", bool(isinstance(finish, str) and finish.strip())) + "\n")
        _safe(_fact("text_length", len(text) if isinstance(text, str) else None) + "\n")
    _safe(_fact("speech_eligible", speech_eligible(outcome)) + "\n")

    verdict = "unknown"
    if outcome is None:
        verdict = "no_outcome（本轮没走到 Agent）"
    elif speech_eligible(outcome):
        verdict = "eligible（可播报）⇒ 若真机仍 text_only，问题在 start_stream/_speak 路径"
    else:
        verdict = "not_eligible（不可播报）⇒ 问题在回复本身（本地路由 / completed / finish_reason）"
    _safe(_fact("verdict", verdict) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
