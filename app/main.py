"""P1 运行时入口：配置、日志、生命周期、文字壳与 P8 按键语音入口。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence, TextIO

from app.agent.adapter import AgentTurnOutcome
from app.agent.deepseek_chat_agent import DeepSeekChatAgentAdapter
from app.config import ConfigError, Settings, load_settings
from app.config.settings import inject_dotenv_secret
from app.logging import StructuredLogError, StructuredLogger
from app.persona import PersonaError, ResolvedPersona, resolve_persona
from app.providers import ProviderIntegration, ProviderIntegrationConfig
from app.providers.deepseek_adapter import DeepSeekAdapterConfig, DeepSeekProviderAdapter
from app.providers.deepseek_chat_transport import (
    DEFAULT_SYSTEM_PROMPT,
    DeepSeekChatConfig,
    DeepSeekChatTransport,
)
from app.providers.deepseek_http_transport import DeepSeekHTTPTransport
from app.routing import RouteInput
from app.runtime import AssistantRuntime, LocalChatRouter, RuntimeLifecycle, detect_unfinished_tasks
from app.runtime.orchestration import LocalTaskRouter, PendingConfirmationRouter
from app.runtime.tts_service import OwnedProcess
from app.ui import TextShell

if TYPE_CHECKING:  # 只用于类型标注，避免默认路径导入音频/TTS 依赖
    from app.runtime.auto_voice import AutoVoiceConfig, AutoVoiceSession
    from app.runtime.tts_service import TtsStartResult
    from app.runtime.voice_entry import VoiceEntryConfig, VoiceEntryEnvironment, VoiceLoopSession

# 真实 Provider 运行参数（与 P7-R-27 已真实验证过的一致；改值需重新授权验证）。
_DEEPSEEK_MODEL_ID = "deepseek-flash"
_DEEPSEEK_MODEL_VERSION = "runtime-2026-09-30"
_DEEPSEEK_TIMEOUT_SECONDS = 15.0
_DEEPSEEK_MAX_TOKENS = 256
# 对话（批次 B）参数：非流式、单轮、固定内置提示词；每次回复 1 次请求。
_DEEPSEEK_CHAT_MODEL_VERSION = "chat-2026-09-30"
_DEEPSEEK_CHAT_TIMEOUT_SECONDS = 30.0
_DEEPSEEK_CHAT_MAX_TOKENS = 512


class _RuntimeLocalRouter:
    """Safe runtime placeholder until a local classifier is authorized and wired."""

    def route(self, request: RouteInput):
        del request
        raise RuntimeError("local runtime router is not connected")

def build_settings(project_root: Path | str | None = None) -> Settings:
    """加载当前项目配置；不连接 Provider，也不读取音频设备。"""

    return load_settings(project_root=project_root)


def _deepseek_route_config() -> DeepSeekAdapterConfig:
    """真实路由（意图分类）配置；仅在双条件下被使用。"""

    return DeepSeekAdapterConfig(
        provider_name="deepseek-official",
        model_id=_DEEPSEEK_MODEL_ID,
        model_version=_DEEPSEEK_MODEL_VERSION,
        base_url="https://api.deepseek.com",
        enabled=True,
        authorized=True,
        timeout_seconds=_DEEPSEEK_TIMEOUT_SECONDS,
        max_retries=0,
        max_tokens=_DEEPSEEK_MAX_TOKENS,
        thinking="disabled",
    )


def _deepseek_chat_config(*, system_prompt: str | None = None) -> DeepSeekChatConfig:
    """真实对话（回复生成）配置；非流式、单轮、**人设由文件驱动**。

    ``system_prompt``（2026-10-24）由 :func:`resolve_run_persona` 从人物模板文件解析；
    未给出时保留内置兜底 prompt（配置文件缺失时的诚实降级）。
    """

    return DeepSeekChatConfig(
        provider_name="deepseek-official",
        model_id=_DEEPSEEK_MODEL_ID,
        model_version=_DEEPSEEK_CHAT_MODEL_VERSION,
        base_url="https://api.deepseek.com",
        enabled=True,
        authorized=True,
        timeout_seconds=_DEEPSEEK_CHAT_TIMEOUT_SECONDS,
        max_retries=0,
        max_tokens=_DEEPSEEK_CHAT_MAX_TOKENS,
        thinking="disabled",
        system_prompt=DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt,
    )


def build_provider_integration(
    settings: Settings, *, agent_ready: bool = False
) -> ProviderIntegration:
    """按双条件构建 provider 边界；默认 local，且不装配任何真实 transport。

    仅当 ``ASSISTANT_PROVIDER=deepseek`` **且** ``ASSISTANT_DEEPSEEK_OPT_IN=true`` 时，
    才装配真实 HTTPS transport。构造自身不读密钥、不联网（只有 ``route`` 才会请求），
    也不改变工具/授权/执行边界。未 opt-in 时保持 NOT READY，且不静默回退 local。

    ``agent_ready`` 由调用方在真实对话适配器就绪时显式传入，用于如实投影
    ``agent_ready`` 与 ``runtime_ready``；默认 ``False`` 保持既有语义。
    """

    if not isinstance(settings, Settings):
        raise TypeError("settings must be Settings")
    if settings.provider == "deepseek" and settings.deepseek_opt_in:
        config = _deepseek_route_config()
        adapter = DeepSeekProviderAdapter(
            DeepSeekHTTPTransport(config=config),
            config=config,
        )
        return ProviderIntegration(
            _RuntimeLocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=adapter,
            deepseek_transport_ready=True,
            agent_ready=agent_ready,
        )
    return ProviderIntegration(
        _RuntimeLocalRouter(),
        config=ProviderIntegrationConfig(provider=settings.provider),
    )


def resolve_run_persona(
    settings: Settings, persona_path: str | Path | None = None
) -> ResolvedPersona:
    """解析本轮要用的 system prompt（2026-10-24 起人设文件驱动）。

    优先级：``--persona`` > ``ASSISTANT_PERSONA_PATH`` > 仓库
    ``implementation_plan/persona.md`` > 内置兜底。显式指定的模板不可用时转成
    :class:`ConfigError`（启动失败、退出码 2）——换人设是用户的决定，程序不得静默替换。
    """

    explicit: str | Path | None = persona_path
    if explicit is None or not str(explicit).strip():
        explicit = settings.persona_path or None
    try:
        return resolve_persona(
            project_root=settings.project_root,
            fallback_prompt=DEFAULT_SYSTEM_PROMPT,
            explicit_path=explicit,
        )
    except PersonaError as exc:
        raise ConfigError(f"人物模板不可用：{exc}") from exc


def _persona_notice(persona: ResolvedPersona) -> str:
    """启动时的一行人设说明（换人设只改文件，所以必须说清这次用的是哪个）。"""

    short = persona.version.split(":", 1)[-1][:8]
    if persona.path is None:
        return f"人物模板：内置兜底（未找到 implementation_plan/persona.md，version {short}）\n"
    return (
        f"人物模板：{persona.name}（{persona.path}，version {short}；"
        "换人设改这个文件，重启生效）\n"
    )


def build_agent_adapter(
    settings: Settings, *, persona: ResolvedPersona | None = None
) -> object | None:
    """按同一双条件构建真实对话 Agent；默认返回 ``None``（保持如实不可用）。

    构造不读密钥、不联网；只有 ``submit_chat`` 才会发起请求。``persona`` 给出时用它
    的正文作为 system prompt（否则用内置兜底 prompt）。
    """

    if not isinstance(settings, Settings):
        raise TypeError("settings must be Settings")
    if settings.provider == "deepseek" and settings.deepseek_opt_in:
        config = _deepseek_chat_config(
            system_prompt=None if persona is None else persona.prompt
        )
        return DeepSeekChatAgentAdapter(
            DeepSeekChatTransport(config=config),
            config=config,
        )
    return None


def select_runtime_router(
    router: object | None, provider_integration: ProviderIntegration
) -> tuple[object | None, tuple[str, str, str] | None]:
    """决定运行时路由器：显式注入优先，其次仅在真实 transport 就绪时接管。"""

    if router is not None:
        return router, None
    capabilities = provider_integration.capabilities
    if not capabilities.transport_ready:
        return None, None
    return provider_integration, provider_integration.router_metadata


def task_name_prefixes(*configs: object) -> tuple[str, ...]:
    """语音链路的"称呼/唤醒名"清单。

    真机教训（2026-10-13，`--voice` 实测）：用户说"打开计算器"被 ASR 识别成「小饼打开计算器。」，
    句首多出唤醒名 ⇒ 本地任务句式完全不匹配。这里把各配置里的 `keyword`（`--keyword`，默认「小饼」）
    交给路由，由它在归一化阶段**精确**剥掉句首称呼；都没给就用 `DEFAULT_KEYWORD`。
    """

    from app.runtime.auto_voice import DEFAULT_KEYWORD

    names: list[str] = []
    for config in configs:
        keyword = getattr(config, "keyword", None)
        if type(keyword) is str and keyword.strip():
            names.append(keyword.strip())
    if not names:
        names.append(DEFAULT_KEYWORD)
    return tuple(dict.fromkeys(names))


def compose_task_router(router: object | None, *, names: Sequence[str] = ()) -> object:
    """把本地任务路由（含语音"待确认"状态）套在现有路由器外面（未命中交给 fallback）。

    真机教训（2026-10-13）：`select_runtime_router()` 在没有 Provider 且没有显式注入 router 时
    返回 ``(None, None)``；直接把 ``None`` 当 fallback 会让路由组合抛 TypeError，
    于是任务执行被误报成"白名单或数据目录不可用"并降级。缺 fallback 时这里使用与默认运行时
    **同源**的 ``LocalChatRouter``（`AssistantRuntime.build(router=None)` 内部也是它）。

    返回的是 `PendingConfirmationRouter`：没有 arm 时行为与"本地任务优先"逐字节一致；
    语音侧 arm 之后，下一句严格等于「确认」才会命中同一个任务（20 s 窗口，**从播报结束起算**）。
    """

    fallback = router if router is not None else LocalChatRouter()
    return PendingConfirmationRouter(first=LocalTaskRouter(names=names), fallback=fallback)


def with_task_execution(
    config: "AutoVoiceConfig | None", *, task_router: object | None, task_dispatch: object | None
) -> "AutoVoiceConfig | None":
    """把任务执行注入件合并进自动语音配置（缺任一个就原样返回，语音行为不变）。"""

    if task_router is None or task_dispatch is None:
        return config
    from dataclasses import replace

    from app.runtime.auto_voice import AutoVoiceConfig

    base = config if config is not None else AutoVoiceConfig()
    return replace(base, task_router=task_router, task_dispatch=task_dispatch)


def provider_startup_notice(
    settings: Settings, capabilities: object, chat_ready: bool = False
) -> str | None:
    """返回需要在启动时如实告知用户的 provider 事实（无可告知时返回 None）。"""

    transport_ready = bool(getattr(capabilities, "transport_ready", False))
    if transport_ready and chat_ready:
        return (
            "真实 DeepSeek 已就绪：每轮对话会产生 2 次真实请求（1 次路由 + 1 次回复）"
            "并向 api.deepseek.com 计费；失败时不回退本地、不伪造回复。"
        )
    if transport_ready:
        return (
            "真实 DeepSeek Provider 已就绪：每次路由会向 api.deepseek.com 发起真实请求并计费；"
            "失败时按设计不回退本地。"
        )
    if settings.provider == "deepseek" and not settings.deepseek_opt_in:
        return (
            "已选择 deepseek，但未开启 ASSISTANT_DEEPSEEK_OPT_IN=true："
            "真实路由不可用（fail closed），不会静默回退本地。"
        )
    return None


def run(
    *,
    project_root: Path | str | None = None,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    conversation: object | None = None,
    agent_adapter: object | None = None,
    router: object | None = None,
    voice_enabled: bool = False,
    completed_reply_speaker: Callable[[AgentTurnOutcome], object] | None = None,
    runtime: AssistantRuntime | None = None,
    push_to_talk: bool = False,
    voice_config: "VoiceEntryConfig | None" = None,
    voice_session_factory: Callable[[object], "VoiceLoopSession"] | None = None,
    voice_show_hud: bool = True,
    voice_hud_factory: Callable[[], object] | None = None,
    voice_auto: bool = False,
    auto_config: "AutoVoiceConfig | None" = None,
    auto_session_factory: Callable[[object], "AutoVoiceSession"] | None = None,
    auto_environment_factory: Callable[[], object] | None = None,
    auto_interrupt_source: Callable[[], str | None] | None = None,
    tts_autostart: str = "on",
    tts_start_timeout: float = 90.0,
    tts_start_command: Sequence[str] | None = None,
    enable_task_exec: bool = False,
    persona_path: str | Path | None = None,
) -> int:
    """运行文字壳（或在 ``push_to_talk`` 时运行按键语音入口）并返回退出码。

    ``persona_path``（2026-10-24）：本次运行显式指定的人物模板，等价于 ``--persona``；
    缺省时用 ``ASSISTANT_PERSONA_PATH`` / 仓库默认 ``implementation_plan/persona.md``。
    """

    settings = build_settings(project_root)
    settings.prepare_directories()
    # 仅在显式 opt-in 的真实 Provider 路径下，才把 .env 中的白名单密钥注入进程环境
    # （环境变量优先，不覆盖已有值；只写内存，不打印、不落盘）。
    if (
        settings.provider == "deepseek"
        and settings.deepseek_opt_in
        and settings.dotenv_path is not None
    ):
        inject_dotenv_secret("DEEPSEEK_API_KEY", dotenv_path=settings.dotenv_path)
    # 真实对话 Agent：显式注入优先，否则在双条件下构建；构造不读密钥、不联网。
    # 2026-10-24：人设在**启动时**从人物模板文件解析一次（换人设 = 改文件 + 重启），
    # 显式指定的模板不可用会在这里 fail closed（ConfigError → 退出码 2）。
    persona = resolve_run_persona(settings, persona_path)
    effective_agent = (
        agent_adapter
        if agent_adapter is not None
        else build_agent_adapter(settings, persona=persona)
    )
    chat_ready = bool(
        effective_agent is not None
        and getattr(effective_agent, "provider_ready", False)
    )
    provider_integration = build_provider_integration(settings, agent_ready=chat_ready)
    logger = StructuredLogger(settings.logs_root)
    lifecycle = RuntimeLifecycle(
        lock_path=settings.temp_root / "instance.lock",
        event_sink=lambda event: logger.write(
            event.event_type,
            instance_id=event.instance_id,
            event_sequence=event.sequence,
            details=event.details,
        ),
    )
    try:
        lifecycle.start()
        owned_tts = OwnedProcess()
        # 2026-10-24：人物模板是**文件驱动**的，所以每次启动都要留下"这次到底用了哪个人设"
        # 的只读事实（来源 file/builtin + 名称 + sha256 版本）；换人设不必改代码，
        # 事后靠这三项就能对上某段对话用的是哪一版人设。
        persona_details = {
            "persona_source": persona.source,
            "persona_name": persona.name[:64],
            "persona_version": persona.version[:80],
        }
        lifecycle.emit(
            "config_checked",
            {
                "data_root_configured": True,
                "model_root_configured": True,
                "logs_root_configured": True,
                "plans_root_configured": True,
                "audit_root_configured": True,
                "temp_root_configured": True,
                "wake_word_configured": bool(settings.wake_word),
                **provider_integration.capabilities.to_dict(),
                **persona_details,
                "mode": "text",
            },
        )
        output_stream.write(_persona_notice(persona))
        output_stream.flush()
        recovery_summary = detect_unfinished_tasks(settings.plans_root)
        if recovery_summary.possible_unfinished_count:
            lifecycle.emit(
                "unfinished_task_detected",
                {
                    "unfinished_count": recovery_summary.possible_unfinished_count,
                    "auto_resume": recovery_summary.auto_resume_allowed,
                },
            )
            output_stream.write(
                "安全提示：发现可能未完成的旧任务；本次启动未自动续跑。\n"
            )
            output_stream.flush()
        lifecycle.emit(
            "runtime_ready",
            {
                "mode": "text",
                **provider_integration.capabilities.to_dict(),
                "voice_available": False,
                "task_state": "idle",
            },
        )
        # The default path now owns a local, fail-closed ConversationManager:
        # it produces a visible unavailable result without creating a provider,
        # transport, SDK runtime, or TTS device. Injected adapters can exercise
        # the same Agent -> text -> optional TTS seam offline.
        capabilities = provider_integration.capabilities
        notice = provider_startup_notice(settings, capabilities, chat_ready)
        if notice is not None:
            output_stream.write(notice + "\n")
            output_stream.flush()
        # 真实 transport 就绪时才接管路由；显式注入的 router 始终优先。
        # 默认 local 路径保持既有 LocalChatRouter 行为不变。
        effective_router, router_metadata = select_runtime_router(router, provider_integration)
        # P10 · T1：文本任务执行（打开白名单应用）。库层默认关闭、CLI 默认打开；
        # 装配失败一律降级为"仅对话"（绝不半接一半），且未装成时不改路由行为。
        task_dispatch: object | None = None
        task_router: object | None = None
        if enable_task_exec:
            try:
                from app.runtime.task_runtime import build_text_task_dispatcher

                task_dispatch = build_text_task_dispatcher(
                    data_root=settings.data_root,
                    audit_root=settings.audit_root,
                )
                task_router = compose_task_router(
                    effective_router, names=task_name_prefixes(auto_config, voice_config)
                )
                effective_router = task_router
                output_stream.write(
                    "任务执行：开（本地句式优先；危险/文件类操作本批仍保持关闭）。\n"
                )
            except Exception as exc:
                # 失败原因必须如实给出**固定短码**（真机踩过：兜底消息写死"白名单或数据目录不可用"，
                # 而实际原因是 fallback 路由器为 None 的 TypeError，白查了一轮）。
                task_dispatch = None
                reason = getattr(exc, "code", None) or type(exc).__name__
                output_stream.write(f"任务执行：不可用（{reason}），已降级为仅对话。\n")
            output_stream.flush()
        active_runtime = runtime or AssistantRuntime.build(
            conversation=conversation,
            agent_adapter=effective_agent,
            router=effective_router,
            router_metadata=router_metadata,
            completed_reply_speaker=completed_reply_speaker,
            voice_enabled=voice_enabled,
        )
        runtime_closed = False

        def close_runtime() -> None:
            nonlocal runtime_closed
            if runtime_closed:
                return
            runtime_closed = True
            active_runtime.close()

        agent_wired = any(item is not None for item in (conversation, effective_agent, runtime))
        voice_requested = bool(voice_auto or push_to_talk)
        if voice_requested:
            tts_result = _ensure_tts_ready(
                tts_mode=voice_config.tts_mode if voice_config is not None else None,
                policy=tts_autostart,
                start_timeout_seconds=tts_start_timeout,
                start_command=tts_start_command,
                output_stream=output_stream,
            )
            if tts_result is not None:
                owned_tts.remember(tts_result)
                if tts_autostart == "require" and not tts_result.reachable:
                    close_runtime()
                    owned_tts.shutdown_owned()
                    return 3
        if voice_auto:
            auto_code = _run_auto_voice(
                conversation=active_runtime.conversation,
                config=with_task_execution(
                    auto_config, task_router=task_router, task_dispatch=task_dispatch
                ),
                session_factory=auto_session_factory,
                environment_factory=auto_environment_factory,
                interrupt_source=auto_interrupt_source,
                output_stream=output_stream,
                lifecycle=lifecycle,
                agent_wired=agent_wired,
                show_hud=voice_show_hud,
                hud_factory=voice_hud_factory,
            )
            if auto_code is not None:
                close_runtime()
                return auto_code
        if push_to_talk:
            voice_code = _run_push_to_talk(
                conversation=active_runtime.conversation,
                config=voice_config,
                session_factory=voice_session_factory,
                input_stream=input_stream,
                output_stream=output_stream,
                lifecycle=lifecycle,
                agent_wired=agent_wired,
                show_hud=voice_show_hud,
                hud_factory=voice_hud_factory,
            )
            if voice_code is not None:
                close_runtime()
                return voice_code

        shell = TextShell(
            input_stream=input_stream,
            output_stream=output_stream,
            status_provider=lambda: {
                "mode": "text",
                **provider_integration.capabilities.to_dict(),
                "voice_available": False,
                "task_state": "idle",
            },
        )
        callback = active_runtime.completed_reply_speaker if voice_enabled else None
        if voice_enabled and callback is None:
            from app.tts.completed_reply import CompletedReplySpeaker
            callback = CompletedReplySpeaker()

        def safe_completed_reply(outcome: AgentTurnOutcome) -> None:
            if callback is not None:
                try:
                    callback(outcome)
                except Exception:
                    # Speech cannot undo or reinterpret already flushed text.
                    pass

        shell.run_conversation(
            conversation=active_runtime.conversation,
            on_exit=close_runtime,
            on_completed_reply=safe_completed_reply if callback else None,
            realtime_speaker=callback,
            task_dispatch=task_dispatch,
        )
        close_runtime()
        return 0
    except (ConfigError, StructuredLogError):
        raise
    finally:
        # 只关掉本次由我们拉起的 TTS 服务；用户自己开着的那个绝不动。
        try:
            owned_tts.shutdown_owned()
        except Exception:  # noqa: BLE001 - 收尾失败不影响退出码
            pass
        if lifecycle.started:
            try:
                lifecycle.close()
            finally:
                logger.close()
        else:
            logger.close()


def _voice_unavailable(
    output_stream: TextIO, lifecycle: RuntimeLifecycle, exc: BaseException
) -> None:
    """语音入口不可用时如实说明并降级为文字输入（不自动切换 Provider）。"""

    reason = str(exc) if isinstance(exc, Exception) and str(exc) else type(exc).__name__
    output_stream.write(f"语音输入不可用：{reason}\n已按设计降级为文字输入。\n")
    output_stream.flush()
    code = getattr(exc, "code", None)
    lifecycle.emit(
        "voice_entry_unavailable",
        {
            "reason": type(exc).__name__,
            # 固定短码（若该异常带）：真机日志里"本机服务没在跑"与"选择/音频不对"必须可分。
            "code": code if isinstance(code, str) and code else None,
            "fallback": "text_input",
        },
    )


def _safe_write(output_stream: TextIO, text: str) -> None:
    """写一段文本；**编码不可表示时按 replace 重试**，绝不因此中断语音链路。

    真机教训（2026-10-02，批次 R4-A 实测）：控制台是 GBK(936) 时，识别文本/回复里出现
    韩文等不可编码字符会让 `output_stream.write` 抛 `UnicodeEncodeError`；该异常沿
    `run_turn` 冒泡 → 整个免按键会话被判"语音输入不可用"并降级为文字。展示层故障**不得**
    影响语音链路，因此这里统一做安全写。
    """

    try:
        output_stream.write(text)
        return
    except UnicodeEncodeError:
        pass
    except Exception:  # noqa: BLE001 - 展示层故障不得影响语音
        return
    try:
        encoding = getattr(output_stream, "encoding", None) or "utf-8"
        output_stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
    except Exception:  # noqa: BLE001 - 第二次失败就放弃这行输出
        return


def _render_turn(output_stream: TextIO, facts: "object") -> None:
    from app.runtime.voice_entry import NO_SPEECH_REASON

    reason = getattr(facts, "reason", "")
    text = getattr(facts, "asr_text", None)
    reply = getattr(facts, "reply_text", None)
    if reason == NO_SPEECH_REASON:
        _safe_write(output_stream, "未检测到语音：本轮已丢弃，未提交、未播报。\n")
    elif isinstance(text, str) and text:
        _safe_write(output_stream, f"识别：{text}\n")
    elif isinstance(reason, str) and reason.startswith("start_rejected"):
        _safe_write(output_stream, f"未能开始收音：{reason}\n")
    else:
        _safe_write(output_stream, "未得到可靠的识别结果（本轮未提交、未播报）。\n")
    if isinstance(reply, str) and reply:
        _safe_write(output_stream, f"回复：{reply}\n")
    try:
        output_stream.flush()
    except Exception:  # noqa: BLE001 - 展示层故障不得影响语音
        return


def _run_push_to_talk(
    *,
    conversation: object,
    config: "VoiceEntryConfig | None",
    session_factory: Callable[[object], "VoiceLoopSession"] | None,
    input_stream: TextIO,
    output_stream: TextIO,
    lifecycle: RuntimeLifecycle,
    agent_wired: bool,
    show_hud: bool = True,
    hud_factory: Callable[[], object] | None = None,
) -> int | None:
    """运行按键语音入口。

    返回 ``None`` 表示交给文字壳（语音不可用，或用户明确要求切回文字）；
    返回整数表示本次运行到此结束的退出码。

    ``show_hud``（默认开）启用右下角「正在对话」状态提示窗；窗口不可用时按设计跳过并
    在输出里如实说明，绝不影响语音链路。``hud_factory`` 仅供测试注入。
    """

    from app.runtime.voice_entry import (
        VoiceEntryConfig,
        VoiceEntryError,
        VoiceLoopSession,
        build_real_environment,
    )

    if config is not None and not isinstance(config, VoiceEntryConfig):
        raise TypeError("voice_config 必须是 VoiceEntryConfig")
    entry_config = config or VoiceEntryConfig()
    session: VoiceLoopSession | None = None
    if session_factory is not None:
        try:
            session = session_factory(conversation)
        except VoiceEntryError as exc:
            _voice_unavailable(output_stream, lifecycle, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - 注入装配失败同样降级
            _voice_unavailable(output_stream, lifecycle, exc)
            return None
    else:
        try:
            environment = build_real_environment(entry_config)
            environment.preflight()
            session = VoiceLoopSession(
                conversation=conversation,  # type: ignore[arg-type]
                environment=environment,
                config=entry_config,
                source_instance_id="runtime-voice-entry",
            )
        except Exception as exc:  # noqa: BLE001 - 设备/模型/依赖问题一律降级为文字
            _voice_unavailable(output_stream, lifecycle, exc)
            return None
        output_stream.write(
            f"输入设备：{environment.details.get('device')}"
            f"（{environment.details.get('device_id')}）；ASR 模型：{environment.details.get('model')}\n"
        )

    output_stream.write("语音模式已就绪：按 Enter 开始说话；输入 /text 切回文字输入，/exit 退出。\n")
    if not agent_wired:
        output_stream.write(
            "提示：当前未接入真实模型；语音识别照常进行，回复会如实显示模型不可用，不会伪造内容。\n"
        )
    output_stream.flush()
    lifecycle.emit(
        "voice_entry_ready", {"mode": "push_to_talk", "agent_wired": agent_wired}
    )
    from app.ui.voice_status import (
        StatusBridge,
        attach_stage_hooks,
        build_status_hud,
        restore_stage_hooks,
    )

    hud: object | None = None
    if show_hud:
        try:
            hud = build_status_hud() if hud_factory is None else hud_factory()
        except Exception:  # noqa: BLE001 - 展示层失败不影响语音
            hud = None
    bridge = StatusBridge(hud)
    if show_hud:
        if bridge.available:
            bridge.set_state("IDLE")
            output_stream.write("右下角状态提示窗已启用（不可点击、可拖动）。\n")
        else:
            output_stream.write("右下角状态提示窗不可用（已按设计跳过，不影响语音）。\n")
        output_stream.flush()
    turn_index = 0
    try:
        while True:
            output_stream.write("按 Enter 开始说话（/text 切回文字，/exit 退出）…\n")
            output_stream.flush()
            line = input_stream.readline()
            if line == "":
                break
            command = line.strip()
            if command == "/exit":
                break
            if command == "/text":
                lifecycle.emit("voice_entry_switched_to_text", {"reason": "user_request"})
                output_stream.write("已切回文字输入（不会自动切回语音）。\n")
                output_stream.flush()
                return None
            turn_index += 1
            bridge.set_state("LISTENING", turn_index=turn_index)
            bridge.tick()
            trace: dict[str, object] = {"spoken": False}
            loop = getattr(session, "loop", None)
            if loop is not None:
                trace = attach_stage_hooks(
                    loop,
                    set_state=lambda name, _index=turn_index: bridge.set_state(
                        name, turn_index=_index
                    ),
                    tick=bridge.tick,
                    index=turn_index,
                )
            try:
                facts = session.run_turn()
            finally:
                if loop is not None:
                    restore_stage_hooks(loop)
            _render_turn(output_stream, facts)
            if getattr(facts, "accepted", False) or trace.get("spoken"):
                bridge.set_state("LISTENING", turn_index=turn_index)
            else:
                bridge.set_state("FAILED", turn_index=turn_index)
            bridge.tick()
            lifecycle.emit("voice_turn", facts.log_details())
    except KeyboardInterrupt:
        output_stream.write("\n已停止语音输入。\n")
        output_stream.flush()
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - 收尾失败不影响已记录事实
            pass
        bridge.set_state("STOPPED")
        bridge.close()
    return 0


class _KeyEventLog:
    """P10 · T1（诊断）：控制台按键的**只读**流水（轮次内 / 轮次间共用，最多保留 32 条）。

    真机动机（2026-10-14）：用户报告"没按 Esc 却被取消了计划"，而旧日志只记 `key_action`、
    不记**原始键码**与**是哪条通道消费的** ⇒ 无法定案。本流水只记录事实（键码 + 来源 + 处置），
    不参与任何判定，也不落盘任何正文。
    """

    MAX_EVENTS = 32

    def __init__(self) -> None:
        self._events: list[dict[str, str]] = []

    def record(self, *, source: str, code: str, action: str) -> None:
        self._events.append({"source": source, "code": code, "action": action})
        if len(self._events) > self.MAX_EVENTS:
            del self._events[: -self.MAX_EVENTS]

    def take(self) -> list[dict[str, str]]:
        snapshot = list(self._events)
        self._events.clear()
        return snapshot


def _key_code(key: str) -> str:
    """把单个按键字符渲染成固定短码（如 ``0x1b`` / ``0x0d``）；不打印任何正文。"""

    if len(key) == 1:
        return f"0x{ord(key):02x}"
    return f"wide{len(key)}"


def _keyboard_interrupt_source(
    log: "_KeyEventLog | None" = None,
) -> Callable[[], str | None]:
    """返回一个**非阻塞**的命令读取器（轮次之间）；只有回车才算一条命令。

    真机教训（2026-10-14）：旧实现是 `kbhit()` + **阻塞式** `stdin.readline()`，控制台缓冲里
    只要有一个非回车键（例如误触的方向键前缀、或单独的 Esc），整个轮次循环就会**卡在那里等
    回车** —— 用户看到的就是"播报后有一段没反应"。现在改为逐字符累积、回车才返回：
    Esc / 扩展键一律消费掉并如实记录，绝不阻塞、也绝不把它们留到下一轮被当成确认/取消。
    """

    if os.name != "nt":
        return lambda: None
    try:
        import msvcrt
    except Exception:  # noqa: BLE001 - 没有可用控制台时安静降级
        return lambda: None
    buffer: list[str] = []

    def poll() -> str | None:
        try:
            if not msvcrt.kbhit():
                return None
            key = msvcrt.getwch()
        except Exception:  # noqa: BLE001 - 控制台不可用即视为无输入
            return None
        if key in ("\x00", "\xe0"):
            # 扩展键（方向/功能/Alt）：前缀后紧跟扫描码，必须一起消费，否则下一个字符会被误读。
            try:
                if msvcrt.kbhit():
                    msvcrt.getwch()
            except Exception:  # noqa: BLE001
                pass
            if log is not None:
                log.record(source="between", code=_key_code(key), action="ignored_extended")
            return None
        if key in ("\r", "\n"):
            line = "".join(buffer).strip()
            buffer.clear()
            if log is not None:
                log.record(
                    source="between",
                    code=_key_code(key),
                    action="command" if line else "enter",
                )
            return line
        if key == "\x1b":
            buffer.clear()
            if log is not None:
                log.record(source="between", code="0x1b", action="ignored_escape")
            return None
        if key.isprintable():
            buffer.append(key)
            if len(buffer) > 256:
                buffer.clear()
            if log is not None:
                log.record(source="between", code=_key_code(key), action="command_char")
        return None

    return poll


def _keyboard_key_source(log: "_KeyEventLog | None" = None) -> Callable[[], str | None]:
    """返回"非阻塞单键读取器"：Enter/空格 ⇒ ``confirm``，Esc ⇒ ``cancel``，其它键 ⇒ ``None``。

    P10 · T1 #16：键盘确认通道（**与回声无关**，因此不受本机 AEC fail-closed 影响）。
    非 Windows 或没有可用控制台时返回恒 ``None``：按键通道静默关闭，绝不假装启用。

    诊断（2026-10-14）：每一次按键（含被忽略的）都写进 `_KeyEventLog`，并挂上 ``take_events``
    供会话把"谁吃掉了哪个键"记进轮次事实。
    """

    if os.name != "nt":
        return lambda: None
    try:
        import msvcrt
    except Exception:  # noqa: BLE001 - 没有可用控制台时安静降级
        return lambda: None

    def poll() -> str | None:
        try:
            if not msvcrt.kbhit():
                return None
            key = msvcrt.getwch()
        except Exception:  # noqa: BLE001 - 控制台不可用即视为无输入
            return None
        if key in ("\x00", "\xe0"):
            try:
                if msvcrt.kbhit():
                    msvcrt.getwch()
            except Exception:  # noqa: BLE001
                pass
            if log is not None:
                log.record(source="turn", code=_key_code(key), action="ignored_extended")
            return None
        if key in ("\r", "\n", " "):
            if log is not None:
                log.record(source="turn", code=_key_code(key), action="confirm")
            return "confirm"
        if key == "\x1b":
            if log is not None:
                log.record(source="turn", code="0x1b", action="cancel")
            return "cancel"
        if log is not None:
            log.record(source="turn", code=_key_code(key), action="ignored")
        return None

    if log is not None:
        poll.take_events = log.take  # type: ignore[attr-defined]
    return poll


def _run_auto_voice(
    *,
    conversation: object,
    config: "AutoVoiceConfig | None",
    session_factory: Callable[[object], "AutoVoiceSession"] | None,
    environment_factory: Callable[[], object] | None,
    interrupt_source: Callable[[], str | None] | None,
    output_stream: TextIO,
    lifecycle: RuntimeLifecycle,
    agent_wired: bool,
    show_hud: bool = True,
    hud_factory: Callable[[], object] | None = None,
    key_source: Callable[[], str | None] | None = None,
) -> int | None:
    """运行免按键自动收音入口（``--voice --auto``）。

    返回 ``None`` 表示交给文字壳（装配失败降级，或用户 ``/text`` 切回文字）；
    返回整数表示本次运行到此结束的退出码。

    边界：自动收音只决定输入边界与轮次提交，**不改变授权/计划/执行语义**；
    可选能力（VAD/唤醒词/声纹）装不上一律如实报错并降级为文字，绝不静默假装启用。
    """

    from app.runtime.auto_voice import AutoVoiceConfig, AutoVoiceSession
    from app.runtime.voice_entry import build_real_environment

    if config is not None and not isinstance(config, AutoVoiceConfig):
        raise TypeError("auto_config 必须是 AutoVoiceConfig")
    entry_config = config or AutoVoiceConfig()
    session: AutoVoiceSession | None = None
    environment: object | None = None
    if session_factory is not None:
        try:
            session = session_factory(conversation)
        except Exception as exc:  # noqa: BLE001 - 注入装配失败同样降级为文字
            _voice_unavailable(output_stream, lifecycle, exc)
            return None
    else:
        try:
            environment = (
                environment_factory() if environment_factory is not None
                else build_real_environment(_voice_entry_config_for_auto(entry_config))
            )
            details = getattr(environment, "details", None)
            if isinstance(details, dict):
                output_stream.write(
                    f"输入设备：{details.get('device')}（{details.get('device_id')}）；"
                    f"ASR 模型：{details.get('model')}\n"
                )
                output_stream.flush()
            preflight = getattr(environment, "preflight", None)
            if callable(preflight):
                preflight()
            session = AutoVoiceSession(
                conversation=conversation,
                environment=environment,
                config=entry_config,
                source_instance_id="runtime-auto-voice",
                notify=lambda text: _write_line(output_stream, text),
            )
        except Exception as exc:  # noqa: BLE001 - 设备/模型/依赖问题一律降级为文字
            _voice_unavailable(output_stream, lifecycle, exc)
            return None

    output_stream.write(
        "免按键自动收音已开启：直接说话即可；输入 /text 切回文字输入，/exit 退出。\n"
    )
    if not agent_wired:
        output_stream.write(
            "提示：当前未接入真实模型；语音识别照常进行，回复会如实显示模型不可用，不会伪造内容。\n"
        )
    output_stream.flush()
    lifecycle.emit("voice_entry_ready", {"mode": "auto_capture", "agent_wired": agent_wired})
    _report_barge_in_status(entry_config, session, output_stream)

    from app.ui.voice_status import StatusBridge, build_status_hud

    hud: object | None = None
    if show_hud:
        try:
            hud = build_status_hud() if hud_factory is None else hud_factory()
        except Exception:  # noqa: BLE001 - 展示层失败不影响语音
            hud = None
    bridge = StatusBridge(hud)
    if show_hud:
        if bridge.available:
            bridge.set_state("IDLE")
            output_stream.write("右下角状态提示窗已启用（不可点击、可拖动）。\n")
        else:
            output_stream.write("右下角状态提示窗不可用（已按设计跳过，不影响语音）。\n")
        output_stream.flush()

    # P10 · T1（诊断）：轮次内与轮次间的按键共用一个只读流水，便于定位"谁吃掉了哪个键"。
    key_log = _KeyEventLog()
    poll = (
        interrupt_source
        if interrupt_source is not None
        else _keyboard_interrupt_source(key_log)
    )
    # #16：轮次内的键盘通道（确认/取消）；没注入时用真实控制台实现（非 Windows ⇒ 恒 None）。
    key_poll = key_source if key_source is not None else _keyboard_key_source(key_log)
    if key_source is not None and not hasattr(key_poll, "take_events"):
        try:
            key_poll.take_events = key_log.take  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 注入替身不可写属性时跳过诊断
            pass
    turn_index = 0

    def finish(reason_event: str) -> None:
        try:
            session.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 收尾失败不影响已记录事实
            pass
        bridge.set_state("STOPPED")
        bridge.close()
        lifecycle.emit("voice_entry_stopped", {"reason": reason_event})

    try:
        while True:
            command = poll() if poll is not None else None
            if command == "/exit":
                finish("user_exit")
                return 0
            if command == "/text":
                finish("user_request")
                output_stream.write("已切回文字输入（不会自动切回语音）。\n")
                output_stream.flush()
                return None
            turn_index += 1
            bridge.set_state("LISTENING", turn_index=turn_index)
            bridge.tick()
            facts = session.run_turn(  # type: ignore[union-attr]
                on_listening=lambda _index=turn_index: bridge.set_state(
                    "LISTENING", turn_index=_index
                ),
                on_speech_started=bridge.tick,
                hud=hud,
                key_source=key_poll,
            )
            _render_turn(output_stream, facts)
            # 真机事实（2026-10-09）：**播报中**的"播报"状态由 `AutoVoiceSession` 在真实
            # `_speak` 期间推给提示窗（`_complete_turn_with_hud`）。`run_turn()` 返回时音频
            # 已经播完，这里再设 `SPEAKING` 只会让窗口在轮次结束后谎报"正在播报"。
            bridge.set_state("IDLE", turn_index=turn_index)
            bridge.tick()
            lifecycle.emit("voice_turn", facts.log_details())
    except KeyboardInterrupt:
        output_stream.write("\n已停止免按键收音。\n")
        output_stream.flush()
        finish("keyboard_interrupt")
        return 0
    except Exception as exc:  # noqa: BLE001 - 轮次异常必须收尾后如实降级为文字
        finish("turn_failed")
        _voice_unavailable(output_stream, lifecycle, exc)
        return None


def _report_barge_in_status(
    config: "AutoVoiceConfig", session: object, output_stream: TextIO
) -> None:
    """如实报告打断功能是否真的启用；不启用时必须说明原因（绝不静默假装启用了）。"""

    if getattr(config, "barge_in_mode", "off") == "off":
        return
    details_getter = getattr(session, "barge_in_details", None)
    if not callable(details_getter):
        return
    try:
        details = dict(details_getter())
    except Exception:  # noqa: BLE001 - 提示失败不影响语音
        return
    if details.get("enabled"):
        cooldown_ms = details.get("cooldown_ms") or 0
        cooldown_note = (
            f"；方案 C 冷却窗口 {cooldown_ms} ms（窗口内不判定插话）" if cooldown_ms else ""
        )
        output_stream.write(
            f"打断已启用：{config.barge_in_mode}（降级级别 {details.get('degradation')}{cooldown_note}）。\n"
        )
        # R4-B P1/P2：如实报告 AEC 引擎是否真在跑、实测抑制量与残余地板门。
        engine_requested = details.get("aec_engine_requested") or "nlms"
        if details.get("aec_engine_running"):
            erle = details.get("aec_erle_db")
            erle_note = f"{erle:.1f} dB" if isinstance(erle, (int, float)) else "尚无实测"
            output_stream.write(
                f"AEC 引擎：webrtc/AEC3 正在逐帧运行；实测抑制量 {erle_note}"
                f"（门 {details.get('gate_in_playback') if details.get('gate_in_playback') else '固定'}）\n"
            )
        elif engine_requested == "webrtc":
            output_stream.write(
                "AEC 引擎：请求了 webrtc/AEC3 但**没有真的在跑**"
                "（需要 --aec-reference-mode same_process 且 pywebrtc-audio 可用）；"
                "已按 nlms 口径工作，不声称已消除回声。\n"
            )
        if not details.get("echo_proven"):
            # 如实提示：本机（内置麦克风阵列 + 内置扬声器）大概率不满足打断条件。
            output_stream.write(
                "提示：尚未证明能看见自己的回声（echo_proven=false），"
                "本机可能不具备打断条件；建议戴耳机或改用外接麦克风。\n"
            )
    else:
        output_stream.write(
            "打断未启用（如实说明）：原因 "
            f"{details.get('unavailable_reason') or 'unknown'}；"
            "没有可用回声参考时无法区分插话与扬声器自己的声音，已按设计退回半双工。\n"
        )
    output_stream.flush()


def _voice_entry_config_for_auto(config: "AutoVoiceConfig") -> object:
    """把自动收音配置折算成 ``VoiceEntryConfig``（设备 / TTS 通道共用同一装配）。"""

    from app.runtime.voice_entry import VoiceEntryConfig

    return VoiceEntryConfig(
        device_id=config.device_id,
        silence_ms=config.silence_ms,
        speech_threshold=config.speech_threshold,
        aec_reference_mode=config.aec_reference_mode,
        aec_engine=config.aec_engine,
        tts_mode=config.tts_mode,
        tts_speed=config.tts_speed,
        speech_stall_timeout_s=config.speech_stall_timeout_s,
    )


def _write_line(output_stream: TextIO, text: str) -> None:
    """把可选提示写到输出流（**编码不可表示也不抛错**，绝不影响语音链路）。"""

    _safe_write(output_stream, text + "\n")
    try:
        output_stream.flush()
    except Exception:  # noqa: BLE001 - 展示层失败不影响对话
        return


def _tts_mode_needs_local_service(tts_mode: str | None) -> bool:
    """播报通道是否需要本机 VoiceStudio 服务（``edge`` / ``none`` 不需要）。

    ``pcm_seq`` 与 ``ws_pcm`` 用同一个本机服务，所以它同样需要；判断依据用
    ``VOICESTUDIO_TTS_MODES``，``STREAMING_TTS_MODES`` 本身保持不变。
    """

    from app.runtime.voice_entry import VOICESTUDIO_TTS_MODES

    if tts_mode is None:
        return True  # 走默认通道（ws_pcm）
    return tts_mode in VOICESTUDIO_TTS_MODES


def _ensure_tts_ready(
    *,
    tts_mode: str | None,
    policy: str,
    start_timeout_seconds: float,
    start_command: Sequence[str] | None,
    output_stream: TextIO,
) -> "TtsStartResult | None":
    """按策略确保本机 TTS 服务可用；不需要时返回 ``None``（不产生任何副作用）。

    ``policy``：``on``（默认，不可达就拉起）、``off``（从不拉起）、``require``（必须可用，
    否则调用方应如实报错退出）。
    """

    if policy == "off" or not _tts_mode_needs_local_service(tts_mode):
        return None
    from app.runtime.tts_service import TtsStartCommand, ensure_tts_service

    command = (
        TtsStartCommand(argv=tuple(str(item) for item in start_command))
        if start_command
        else None
    )
    result = ensure_tts_service(timeout_seconds=start_timeout_seconds, command=command)
    output_stream.write(result.summary() + "\n")
    output_stream.flush()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；默认文字模式，``--voice`` 显式开启按键语音入口。"""

    parser = argparse.ArgumentParser(
        description="电脑语音助手：默认文字模式；--voice 进入按键语音模式（需要麦克风与本地 ASR 模型）。"
    )
    parser.add_argument("--voice", action="store_true", help="启用语音入口")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="与 --voice 同用时启用免按键自动收音（不带 --auto 时保持按键模式，行为不变）",
    )
    parser.add_argument(
        "--tts-mode",
        choices=("ws_pcm", "http_wav", "auto", "pcm_seq", "edge", "none"),
        default="ws_pcm",
        help=(
            "语音模式的播报通道（none 表示只识别不播报；"
            "pcm_seq 为新增的有界预取流水线通道，默认仍是 ws_pcm）"
        ),
    )
    parser.add_argument(
        "--tts-speed",
        type=float,
        default=None,
        help=(
            "播报语速倍数（默认沿用声线选择的 1.0，行为不变）；实测 1.4 自然可用"
            "（3.53 到 4.35 字每秒），可选区间 0.5 到 2.0。只覆盖本次运行，"
            "不改持久化的声线选择文件"
        ),
    )
    parser.add_argument("--device-id", default=None, help="固定输入设备 id（默认系统默认设备）")
    parser.add_argument("--speech-threshold", type=float, default=600.0, help="语音能量阈值（PCM16 RMS）")
    parser.add_argument("--silence-ms", type=int, default=1500, help="说完后的静音结束阈值")
    parser.add_argument(
        "--max-record-seconds",
        type=int,
        default=15,
        help="自动收音单轮最长录音秒数（仅 --auto 生效）",
    )
    parser.add_argument(
        "--min-utterance-ms",
        type=int,
        default=300,
        help="自动收音最小轮长毫秒（仅 --auto 生效；低于此长度不判说完）",
    )
    parser.add_argument(
        "--vad-mode",
        choices=("energy", "silero"),
        default="silero",
        help="自动收音的人声判定方式：silero（默认，需模型）或 energy（能量阈值）",
    )
    parser.add_argument(
        "--vad-model",
        default=None,
        help="Silero VAD onnx 模型路径（仅 --auto --vad-mode silero 生效；默认用项目 models 下模型）",
    )
    parser.add_argument(
        "--kws-mode",
        choices=("off", "gate"),
        default="off",
        help="唤醒词门禁：off（默认）或 gate（必须先听到唤醒词才开启新的输入会话；需 KWS 模型）",
    )
    parser.add_argument(
        "--kws-model-dir",
        default=None,
        help="唤醒词模型目录（gate 必需；含 tokens.txt/encoder.onnx/decoder.onnx/joiner.onnx）",
    )
    parser.add_argument(
        "--kws-keywords-file",
        default=None,
        help="唤醒词关键词文件（默认自动取 <kws-model-dir>/keywords.txt）",
    )
    parser.add_argument(
        "--persona",
        default=None,
        help=(
            "人物模板文件（UTF-8，带 front matter 的 markdown；默认用仓库 "
            "implementation_plan/persona.md）。换人设只改文件、不用改代码；"
            "显式指定的文件不可用会直接报错退出，不会静默换成别的人设"
        ),
    )
    parser.add_argument("--keyword", default="小饼", help="唤醒词文本（默认「小饼」）")
    parser.add_argument(
        "--kws-threshold",
        type=float,
        default=0.25,
        help="唤醒词命中阈值 0~1（默认 0.25）",
    )
    parser.add_argument(
        "--kws-wait-seconds",
        type=int,
        default=30,
        help="每轮等待唤醒词的最长秒数（默认 30）",
    )
    parser.add_argument(
        "--voiceprint-mode",
        choices=("off", "filter"),
        default="off",
        help="声纹误触发过滤：off（默认）或 filter（他人/电视说话在 ASR 之前丢弃；需模型 + 档案）",
    )
    parser.add_argument(
        "--voiceprint-model",
        default=None,
        help="声纹嵌入 onnx 模型路径（filter 必需）",
    )
    parser.add_argument(
        "--voiceprint-profile",
        default=None,
        help='已注册声纹档案 JSON（filter 必需；形如 {"embeddings": [[...]]}）',
    )
    parser.add_argument(
        "--voiceprint-threshold",
        type=float,
        default=0.5,
        help="声纹匹配阈值 (0,1]（默认 0.5；调低会放行更多说话人）",
    )
    parser.add_argument(
        "--tts-autostart",
        choices=("on", "off", "require"),
        default="on",
        help="本机 TTS（VoiceStudio）不可达时：on=自动拉起（默认）、off=从不拉起、require=必须可用否则报错退出",
    )
    parser.add_argument(
        "--tts-start-timeout",
        type=float,
        default=90.0,
        help="等待本机 TTS 服务就绪的秒数（默认 90）",
    )
    parser.add_argument(
        "--speech-stall-timeout",
        type=float,
        default=15.0,
        help=(
            "P1 收尾的**停滞预算**（秒，默认 15 = app.tts.streaming_session.DEFAULT_STALL_TIMEOUT_S）："
            "连续这么久毫无进展才判定收尾失败，只要流水线一直在推进就把整条回复播完。"
            "建议区间 5~120（只要求正数）；对两条本机通道（ws_pcm/http_wav/auto 与 pcm_seq）同时生效"
        ),
    )
    parser.add_argument(
        "--double-talk-detect",
        choices=("off", "facts", "protect"),
        default="off",
        help=(
            "R4-B5 双讲检测（默认 off，行为不变）：facts=打断通道逐帧记录 double_talk_* 只读事实；"
            "protect=同上（保护动作在 B3 才接线）。它对治「双讲时 AEC 继续全速自适应、把用户语音"
            "学进滤波器」与「双讲帧污染残余回声地板」。注意：只在打断通道真正启用时才工作"
            "（需配合 --barge-in-mode duck|confirm，且本机要求有可用回声参考）"
        ),
    )
    parser.add_argument(
        "--tts-start-command",
        default=None,
        help="自定义启动命令（可执行文件路径 + 参数，用空格分隔）；默认读 ASSISTANT_TTS_PYTHON/BACKEND",
    )
    parser.add_argument(
        "--no-hud",
        action="store_true",
        help="语音模式下不显示右下角「正在对话」状态提示窗（默认显示；窗口不可用时自动跳过）",
    )
    parser.add_argument(
        "--no-task-exec",
        action="store_true",
        help=(
            "关闭文本任务执行（默认开）：关掉后「打开计算器」这类句子只做普通对话，不进入任务链路。"
            "开启时也只支持打开白名单应用（calc/notepad）；删除/覆盖与文件类操作本批仍保持关闭"
        ),
    )
    parser.add_argument(
        "--barge-in-mode",
        choices=("off", "duck", "confirm"),
        default="off",
        help=(
            "P10 打断（默认 off）：duck=只做第一阶段降音量（确认不生效）；"
            "confirm=降音量 + 确认后停止播报（独立打断通道 + 安全门，不复用 /text、/exit 的键盘口）"
        ),
    )
    parser.add_argument(
        "--aec-reference-mode",
        choices=("off", "same_process"),
        default="off",
        help=(
            "AEC 参考来源（默认 off）：same_process=取「实际提交给设备的 PCM」作只读参考（方案 A）；"
            "采集 16 kHz 与播放 24 kHz 不同钟时会如实降级，不会声称已消除回声"
        ),
    )
    parser.add_argument(
        "--barge-in-cooldown-ms",
        type=int,
        default=0,
        help=(
            "方案 C 兜底（默认 0 = 关闭）：播放开始后的冷却窗口毫秒数，窗口内收帧、"
            "填前置缓冲，但不降音量、不建立疑似/确认。建议 1000（对齐 LiveKit "
            "backchannel_boundary）；它不改变「没证明回声就不真停播」的 fail-closed 口径"
        ),
    )
    parser.add_argument(
        "--post-speech-mute-ms",
        type=int,
        default=0,
        help=(
            "播报后的输入静默窗（默认 0 = 关闭）：窗口内自动收音只计数、不判语音起始。"
            "真机实测（2026-10-14）它会静默掉紧随播报那一轮开头 ~0.7 s 的用户语音"
            "（可能吞首字），且未观察到对应收益，因此不建议开启；若要试，建议 150~300"
        ),
    )
    parser.add_argument(
        "--aec-engine",
        choices=("nlms", "webrtc"),
        default="nlms",
        help=(
            "回声消除引擎（默认 nlms，行为不变）：webrtc=WebRTC AEC3（可选依赖 pywebrtc-audio，"
            "需配合 --aec-reference-mode same_process）；只有真在跑才会声称 AEC 可用，"
            "并如实暴露实测 ERLE 与残余回声地板"
        ),
    )
    parser.add_argument(
        "--device-recovery-mode",
        choices=("off", "safe_point"),
        default="off",
        help=(
            "设备恢复（默认 off）：safe_point=只在安全空闲点取决策、重连各试一次，"
            "输出断开时停止播报并回退文字"
        ),
    )
    args = parser.parse_args(argv)

    config = None
    if args.voice and not args.auto:
        from app.runtime.voice_entry import VoiceEntryConfig

        config = VoiceEntryConfig(
            tts_mode=args.tts_mode,
            device_id=args.device_id,
            silence_ms=args.silence_ms,
            speech_threshold=args.speech_threshold,
            speech_stall_timeout_s=args.speech_stall_timeout,
            tts_speed=args.tts_speed,
        )
    auto_config = None
    if args.auto:
        from app.runtime.auto_voice import AutoVoiceConfig

        auto_config = AutoVoiceConfig(
            device_id=args.device_id,
            # R4-B：把 CLI 的播报通道透传给免按键会话（此前被静默忽略，永远走 ws_pcm）。
            tts_mode=args.tts_mode if args.tts_mode is not None else AutoVoiceConfig.tts_mode,
            tts_speed=args.tts_speed,
            silence_ms=args.silence_ms,
            speech_threshold=args.speech_threshold,
            speech_stall_timeout_s=args.speech_stall_timeout,
            max_record_seconds=args.max_record_seconds,
            min_utterance_ms=args.min_utterance_ms,
            vad_mode=args.vad_mode,
            vad_model_path=Path(args.vad_model) if args.vad_model else None,
            kws_mode=args.kws_mode,
            kws_model_dir=Path(args.kws_model_dir) if args.kws_model_dir else None,
            kws_keywords_file=Path(args.kws_keywords_file) if args.kws_keywords_file else None,
            keyword=args.keyword,
            kws_threshold=args.kws_threshold,
            kws_wait_seconds=args.kws_wait_seconds,
            voiceprint_mode=args.voiceprint_mode,
            voiceprint_model_path=(
                Path(args.voiceprint_model) if args.voiceprint_model else None
            ),
            voiceprint_profile=(
                Path(args.voiceprint_profile) if args.voiceprint_profile else None
            ),
            voiceprint_threshold=args.voiceprint_threshold,
            barge_in_mode=args.barge_in_mode,
            barge_in_cooldown_ms=args.barge_in_cooldown_ms,
            post_speech_mute_ms=args.post_speech_mute_ms,
            aec_reference_mode=args.aec_reference_mode,
            aec_engine=args.aec_engine,
            device_recovery_mode=args.device_recovery_mode,
            double_talk_mode=args.double_talk_detect,
        )
    try:
        return run(
            push_to_talk=args.voice,
            voice_config=config,
            voice_show_hud=not args.no_hud,
            voice_auto=args.voice and args.auto,
            auto_config=auto_config,
            enable_task_exec=not args.no_task_exec,
            tts_autostart=args.tts_autostart,
            tts_start_timeout=args.tts_start_timeout,
            tts_start_command=(
                args.tts_start_command.split() if args.tts_start_command else None
            ),
            persona_path=args.persona,
        )
    except (ConfigError, StructuredLogError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
