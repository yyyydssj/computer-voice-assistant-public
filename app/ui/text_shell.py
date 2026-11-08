"""P1 最小文字输入/输出壳。

本模块只负责展示当前状态、读取文字输入和通知已显示的完整回复；
不自行配置 Agent、执行工具/电脑动作或启用音频设备。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TextIO

from app.agent.adapter import AgentTurnOutcome


@dataclass(frozen=True)
class ShellCommand:
    """文字壳识别出的本地控制命令。"""

    name: str
    argument: str = ""


class TextShell:
    """以真实状态为依据运行的最小交互界面。"""

    EXIT_COMMANDS = frozenset({"/exit", ":q", "退出", "关闭"})

    def __init__(
        self,
        *,
        input_stream: TextIO,
        output_stream: TextIO,
        status_provider: Callable[[], dict[str, str | bool]],
    ) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.status_provider = status_provider

    def render_banner(self) -> None:
        status = self.status_provider()
        mode = "文字模式" if status.get("mode") == "text" else "未知模式"
        voice = "语音不可用" if status.get("voice_available") is False else "语音状态未知"
        task = "任务空闲" if status.get("task_state") == "idle" else "任务状态未知"
        self._write(f"电脑语音助手 | {mode} | {voice} | {task}")
        self._write("输入文字进行对话；输入 /exit 退出。")

    def read_command(self) -> ShellCommand | None:
        line = self.input_stream.readline()
        if line == "":
            return ShellCommand("exit")
        text = line.strip()
        if not text:
            return None
        if text.casefold() in {command.casefold() for command in self.EXIT_COMMANDS}:
            return ShellCommand("exit")
        return ShellCommand("text", text)

    def run(
        self,
        *,
        on_text: Callable[[str], str | None],
        on_exit: Callable[[], None] | None = None,
        on_rendered: Callable[[str], None] | None = None,
    ) -> None:
        self.render_banner()
        while True:
            self._write_prompt()
            command = self.read_command()
            if command is None:
                continue
            if command.name == "exit":
                if on_exit is not None:
                    on_exit()
                self._write("已请求安全关闭。")
                return
            response = on_text(command.argument)
            if response:
                self._write(response)
                if on_rendered is not None:
                    on_rendered(response)

    def run_conversation(
        self, *, conversation, on_exit: Callable[[], None] | None = None,
        on_completed_reply: Callable[[AgentTurnOutcome], None] | None = None,
        realtime_speaker: object | None = None,
        task_dispatch: object | None = None,
    ) -> None:
        """Render the verified conversation result before an opt-in speech callback.

        A missing/failed Agent outcome is never eligible for the callback.
        This does not configure an Agent or enable voice in the default runtime.

        ``task_dispatch``（可选，P10 · T1）：形如 ``handles(handoff) -> bool`` +
        ``run(handoff, confirm_reader=...) -> 结果对象（需有 text）``。注入后，任务路由
        （task/dangerous）会**在本地**先打印冻结计划、要求输入「确认」，再执行并打印摘要；
        **不注入时行为与之前逐字节一致**（任务路由只记录交接、不执行任何东西）。
        """
        pending: AgentTurnOutcome | None = None

        def handle(text: str) -> str | None:
            nonlocal pending
            pending = None
            stream = None
            start_stream = getattr(realtime_speaker, "start_stream", None)
            if callable(start_stream):
                stream = start_stream()
            on_chunk = getattr(stream, "push_text_chunk", None) if stream is not None else None
            try:
                try:
                    routed = conversation.submit_text(text, on_text_chunk=on_chunk)
                except TypeError as exc:
                    if "on_text_chunk" not in str(exc):
                        raise
                    routed = conversation.submit_text(text)
            except Exception:
                if stream is not None:
                    cancel = getattr(stream, "cancel", None)
                    if callable(cancel):
                        cancel()
                raise
            if (
                task_dispatch is not None
                and routed.outcome is None
                and callable(getattr(task_dispatch, "handles", None))
                and task_dispatch.handles(routed.handoff)
            ):
                if stream is not None:
                    stream.cancel()
                result = task_dispatch.run(routed.handoff, confirm_reader=self._read_confirmation)
                answer = getattr(result, "text", None)
                return answer if isinstance(answer, str) and answer.strip() else None
            if routed.outcome is None:
                if stream is not None:
                    stream.cancel()
                # P10 · T1 #12：`unsupported` 这类"没有聊天回复、但必须**如实说明**"的路由，
                # 交给注入方（可选鸭子方法 `notice(handoff) -> str | None`）给文本；
                # 注入方没实现该方法、或旧替身根本没有 `handoff` 属性时，行为与之前**逐字节一致**。
                return self._route_notice(task_dispatch, getattr(routed, "handoff", None))
            outcome = routed.outcome
            eligible = (isinstance(outcome, AgentTurnOutcome) and outcome.completed
                        and outcome.failure is None and type(outcome.text) is str
                        and outcome.text.strip() and type(outcome.finish_reason) is str
                        and outcome.finish_reason.strip())
            if stream is not None:
                if eligible:
                    try:
                        stream.finish()
                    except Exception:
                        # Preserve the complete text reply; TTS is optional and
                        # a provider/device failure must not break the chat turn.
                        pass
                else:
                    stream.cancel()
            if eligible and stream is None:
                # No streaming path was available, so the completion callback
                # synthesizes the reply exactly once.
                pending = outcome
            return outcome.text

        def rendered(text: str) -> None:
            nonlocal pending
            outcome = pending
            pending = None
            if outcome is not None and outcome.text == text and on_completed_reply is not None:
                on_completed_reply(outcome)

        self.run(on_text=handle, on_exit=on_exit,
                 on_rendered=rendered if on_completed_reply is not None else None)

    @staticmethod
    def _route_notice(task_dispatch: object | None, handoff: object) -> str | None:
        """可选鸭子方法 `notice(handoff) -> str | None`：给"没有聊天回复但必须如实说明"的路由。

        注入方不实现该方法、返回空串或抛错时一律返回 ``None``（行为与接入前逐字节一致；
        说明文本的失败**绝不**能改变路由事实）。
        """

        if task_dispatch is None:
            return None
        provide = getattr(task_dispatch, "notice", None)
        if not callable(provide):
            return None
        try:
            text = provide(handoff)
        except Exception:  # noqa: BLE001 - 说明文本失败不得影响路由事实
            return None
        return text if isinstance(text, str) and text.strip() else None

    def _read_confirmation(self, plan_text: str) -> str:
        """打印冻结计划并要求确认：只有「确认」才执行；EOF 与其它输入都按取消处理。"""

        self._write(plan_text)
        self._write("输入「确认」执行；其它任意输入（含直接回车）将取消。")
        self._write_prompt()
        line = self.input_stream.readline()
        return "" if line == "" else line.strip()

    def _write_prompt(self) -> None:
        self.output_stream.write("> ")
        self.output_stream.flush()

    def _write(self, text: str) -> None:
        self.output_stream.write(text + "\n")
        self.output_stream.flush()


def unavailable_text_response(text: str) -> str:
    """在 Agent 尚未接入时诚实说明边界，不伪造任务结果。"""

    if not text.strip():
        return "未收到文字输入。"
    return "文字输入已收到；Agent 尚未接入，本阶段不执行任务。"
