"""语音阶段 → 右下角「正在对话」提示窗的共享接线（``app`` 侧与 ``tools`` 侧同一实现）。

本模块只做**展示层接线**：把 ``ChatVoiceLoop`` 的真实动作（结束收音 / 提交轮次 / 播报）
映射成提示窗状态，并在进入会阻塞数秒的段落（ASR 定稿、Agent 往返）之前先抽一次窗口消息。
它不决定输入边界、不决定授权、不提交任何事实；所有调用都不抛错（展示层故障绝不影响对话）。

硬边界：
- 不创建线程、不联网、不读写文件（``voice_hud`` 的硬边界照旧）；
- 真实 Win32 窗口只在 :func:`build_status_hud` 中被尝试创建一次，失败即返回 ``None``；
- 状态名收敛为 ``voice_hud.VoiceHudState`` 的成员名（``IDLE``/``LISTENING``/…），
  非法名称会被丢弃（绝不把外部文本当状态）。
"""

from __future__ import annotations

from typing import Any, Callable

TEXT_ONLY_REASON = "text_only"
"""``ChatVoiceLoop._speak`` 在"本轮没有播报通道"时的固定原因码，**不是**播报失败。"""

SPEECH_FAILURE_REASON = "speech_failed"
"""``ChatVoiceLoop._speak`` 真实播报失败时的固定原因码。"""

_HOOK_NAMES = ("release_input", "complete_turn", "_speak")


def speech_result(result: object) -> tuple[bool, str]:
    """把 ``_speak`` 的返回值归一化成 ``(是否出声, 原因码)``；形状异常按失败处理。"""

    if isinstance(result, tuple) and len(result) == 2:
        spoken, reason = result
        if type(spoken) is bool and isinstance(reason, str):
            return spoken, reason
    return False, SPEECH_FAILURE_REASON


def attach_stage_hooks(
    loop: Any,
    *,
    set_state: Callable[[str], None],
    tick: Callable[[], None],
    index: int,
) -> dict[str, Any]:
    """把真实阶段挂到 ``set_state``；返回可变追踪字典 ``{"spoken": bool}``。

    ``set_state`` / ``tick`` 由调用方注入并负责隔离异常（例如 runner 的 ``_hud_set``、
    :class:`StatusBridge`）。
    """

    trace: dict[str, Any] = {"spoken": False}
    real_release = loop.release_input
    real_complete = loop.complete_turn
    real_speak = loop._speak  # noqa: SLF001 - 只读接线，不改变语义

    def release_with_status() -> Any:
        # 阻塞段（ASR 定稿）前后各抽一次窗口消息；期间不建线程。
        set_state("TRANSCRIBING")
        tick()
        result = real_release()
        tick()
        return result

    def complete_with_status() -> Any:
        # 阻塞段（ASR 解码 + Agent 往返，可能数秒）前后各抽一次。
        set_state("THINKING")
        tick()
        result = real_complete()
        tick()
        return result

    def speak_with_status(text: str) -> Any:
        set_state("SPEAKING")
        tick()
        stream = None
        start_stream = getattr(getattr(loop, "_speaker", None), "start_stream", None)
        if callable(start_stream):
            try:
                stream = start_stream()
            except Exception:  # noqa: BLE001 - 播报可选
                stream = None
        if stream is not None:
            push = getattr(stream, "push_text_chunk", None)
            finish = getattr(stream, "finish", None)

            def ticked_push(chunk: str) -> None:
                if callable(push):
                    push(chunk)
                tick()

            def ticked_finish() -> None:
                if callable(finish):
                    finish()
                tick()

            try:
                if callable(push):
                    ticked_push(text)
                if callable(finish):
                    ticked_finish()
            except Exception:  # noqa: BLE001 - 与 ChatVoiceLoop._speak 同语义：失败不影响事实
                cancel = getattr(stream, "cancel", None)
                if callable(cancel):
                    try:
                        cancel()
                    except Exception:  # noqa: BLE001
                        pass
            trace["spoken"] = True
            set_state("LISTENING")
            return True, "speech_finished"

        result = real_speak(text)
        spoken, reason = speech_result(result)
        trace["spoken"] = spoken
        # 只有真实播报失败才是「失败」；``text_only`` 表示本轮没有播报通道（例如 --tts-mode none）。
        if spoken or reason == TEXT_ONLY_REASON:
            set_state("LISTENING")
        else:
            set_state("FAILED")
        return result

    loop.release_input = release_with_status  # type: ignore[assignment]
    loop.complete_turn = complete_with_status  # type: ignore[assignment]
    loop._speak = speak_with_status  # type: ignore[assignment]
    return trace


def restore_stage_hooks(loop: Any) -> None:
    """恢复被 :func:`attach_stage_hooks` 包装过的方法（幂等）。"""

    for name in _HOOK_NAMES:
        if name in vars(loop):
            del vars(loop)[name]


class StatusBridge:
    """把（可能不存在的）提示窗包成"永不抛错"的展示接口。"""

    def __init__(self, hud: Any | None = None) -> None:
        self._hud = hud

    @property
    def available(self) -> bool:
        return self._hud is not None

    def set_state(self, state_name: str, *, turn_index: int | None = None) -> None:
        if self._hud is None:
            return
        try:
            from app.ui.voice_hud import VoiceHudState

            state = getattr(VoiceHudState, state_name, None)
            if state is None:
                return
            self._hud.set_state(state, turn_index=turn_index)
        except Exception:  # noqa: BLE001 - 展示层故障不得影响对话
            return

    def tick(self) -> None:
        if self._hud is None:
            return
        try:
            self._hud.tick()
        except Exception:  # noqa: BLE001 - 展示层故障不得影响对话
            return

    def close(self) -> None:
        if self._hud is None:
            return
        try:
            self._hud.hide()
            self._hud.destroy()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            return


def build_status_hud(
    *, api_factory: Callable[[], Any] | None = None, screen: tuple[int, int] | None = None
) -> Any | None:
    """尝试创建并显示右下角提示窗；任何失败（含非 Windows 平台）都返回 ``None``。

    绝不因为展示层不可用而影响语音链路，也绝不吞掉语音链路自身的失败语义。
    """

    try:
        from app.ui.voice_hud import VoiceStatusHud, build_win32_api

        api = build_win32_api() if api_factory is None else api_factory()
        hud = VoiceStatusHud(api=api, screen=screen)
        hud.show()
        return hud
    except Exception:  # noqa: BLE001 - 提示窗不可用只是没有提示
        return None


__all__ = [
    "SPEECH_FAILURE_REASON",
    "TEXT_ONLY_REASON",
    "StatusBridge",
    "attach_stage_hooks",
    "build_status_hud",
    "restore_stage_hooks",
    "speech_result",
]
