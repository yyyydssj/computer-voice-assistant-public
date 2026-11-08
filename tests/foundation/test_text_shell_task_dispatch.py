"""P10 · T1 批次 2b 红测：**文本壳的任务派发挂点**（不注入时行为逐字节不变）。

全部离线：假会话 + 假派发器；不联网、不执行、不启动进程。
"""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.routing import RouteInput
from app.runtime.orchestration import LocalTaskRouter, unsupported_from_handoff
from app.ui import TextShell

CREATED_AT = "2026-10-13T00:00:00Z"


def _trace(text: str) -> RouteTrace:
    return RouteTrace(
        request_id="req-1",
        conversation_id="conv-1",
        turn_id="turn-1",
        source="text_shell",
        raw_text=text,
        created_at=CREATED_AT,
    )


def task_handoff(text: str = "打开计算器") -> RouteHandoff:
    handoff = LocalTaskRouter().route(RouteInput(raw_text=text, trace=_trace(text)))
    assert handoff is not None
    return handoff


def chat_handoff() -> RouteHandoff:
    return RouteHandoff(
        trace=_trace("你好"),
        route="chat",
        intent="general_chat",
        handling_mode="chat_reply",
        confidence=1.0,
        slots={},
        required_confirmation="none",
        tool_scope=(),
        router=RouterStatus(
            provider="local", model_id="chat-only-router", model_version="1", status="success",
            failure_code=None,
        ),
    )


class FakeConversation:
    def __init__(self, routed: object) -> None:
        self._routed = routed
        self.calls: list[str] = []

    def submit_text(self, text: str, on_text_chunk: object | None = None) -> object:
        self.calls.append(text)
        return self._routed


class FakeDispatcher:
    def __init__(self) -> None:
        self.handled: list[object] = []
        self.runs: list[tuple[object, str]] = []

    def handles(self, handoff: object) -> bool:
        self.handled.append(handoff)
        return getattr(handoff, "route", None) in {"task", "dangerous"}

    def run(self, handoff: object, *, confirm_reader) -> object:
        answer = confirm_reader("【计划】打开 计算器（C:\\Windows\\System32\\calc.exe；哈希未登记）")
        self.runs.append((handoff, answer))
        return SimpleNamespace(text="任务完成：已启动 计算器。")


class TextShellTaskDispatchTests(unittest.TestCase):
    def _shell(self, lines: list[str], routed: object) -> tuple[TextShell, io.StringIO, FakeConversation]:
        output = io.StringIO()
        shell = TextShell(
            input_stream=io.StringIO("\n".join(lines) + "\n"),
            output_stream=output,
            status_provider=lambda: {"mode": "text", "voice_available": False, "task_state": "idle"},
        )
        return shell, output, FakeConversation(routed)

    def test_task_route_prints_plan_then_result(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=task_handoff())
        dispatcher = FakeDispatcher()
        shell, output, conversation = self._shell(["打开计算器", "确认", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=dispatcher)
        text = output.getvalue()
        self.assertIn("C:\\Windows\\System32\\calc.exe", text)  # 计划（确切目标）被打印
        self.assertIn("输入「确认」执行", text)                  # 确认提示被打印
        self.assertIn("任务完成：已启动 计算器。", text)          # 结果被渲染
        self.assertEqual(len(dispatcher.runs), 1)
        self.assertEqual(dispatcher.runs[0][1], "确认")           # 确认读取器拿到用户输入

    def test_other_answer_is_passed_through_unchanged(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=task_handoff())
        dispatcher = FakeDispatcher()
        shell, output, conversation = self._shell(["打开计算器", "好的", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=dispatcher)
        self.assertEqual(dispatcher.runs[0][1], "好的")  # 判定在派发器里（严格只认「确认」）

    def test_eof_during_confirmation_yields_empty_answer(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=task_handoff())
        dispatcher = FakeDispatcher()
        shell, _output, conversation = self._shell(["打开计算器"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=dispatcher)
        self.assertEqual(dispatcher.runs[0][1], "")

    def test_task_route_without_dispatch_stays_silent(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=task_handoff())
        shell, output, conversation = self._shell(["打开计算器", "/exit"], routed)
        shell.run_conversation(conversation=conversation)
        self.assertNotIn("输入「确认」执行", output.getvalue())
        self.assertNotIn("任务完成", output.getvalue())

    def test_chat_handoff_never_reaches_the_dispatcher(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=chat_handoff())
        dispatcher = FakeDispatcher()
        shell, output, conversation = self._shell(["你好", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=dispatcher)
        self.assertEqual(dispatcher.runs, [])
        self.assertNotIn("输入「确认」执行", output.getvalue())


def unsupported_handoff(text: str = "关闭计算器") -> RouteHandoff:
    handoff = LocalTaskRouter().route(RouteInput(raw_text=text, trace=_trace(text)))
    assert handoff is not None and handoff.route == "unsupported"
    return handoff


class FakeNoticeDispatcher:
    """实现 #12 的可选鸭子方法 `notice()`；**不**处理 task/dangerous 路由。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.handled: list[object] = []
        self._fail = fail

    def handles(self, handoff: object) -> bool:
        self.handled.append(handoff)
        return getattr(handoff, "route", None) in {"task", "dangerous"}

    def notice(self, handoff: object) -> str | None:
        if self._fail:
            raise RuntimeError("notice_failed")
        return unsupported_from_handoff(handoff)


class TextShellUnsupportedNoticeTests(unittest.TestCase):
    """P10 · T1 #12：`unsupported` 路由在文本入口**如实打印**；不提供 `notice()` 时保持静默。"""

    def _shell(self, lines: list[str], routed: object) -> tuple[TextShell, io.StringIO, FakeConversation]:
        output = io.StringIO()
        shell = TextShell(
            input_stream=io.StringIO("\n".join(lines) + "\n"),
            output_stream=output,
            status_provider=lambda: {"mode": "text", "voice_available": False, "task_state": "idle"},
        )
        return shell, output, FakeConversation(routed)

    def test_unsupported_route_is_printed_when_notice_is_available(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=unsupported_handoff())
        shell, output, conversation = self._shell(["关闭计算器", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=FakeNoticeDispatcher())
        text = output.getvalue()
        self.assertIn("本批暂不支持", text)
        self.assertIn("close_application", text)
        self.assertIn("未执行任何操作", text)
        self.assertEqual(conversation.calls, ["关闭计算器"])

    def test_unsupported_route_without_notice_stays_silent(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=unsupported_handoff())
        shell, output, conversation = self._shell(["关闭计算器", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=FakeDispatcher())
        self.assertNotIn("本批暂不支持", output.getvalue())

    def test_notice_failure_never_breaks_the_turn(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=unsupported_handoff())
        shell, output, conversation = self._shell(["关闭计算器", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=FakeNoticeDispatcher(fail=True))
        text = output.getvalue()
        self.assertNotIn("本批暂不支持", text)
        self.assertIn("已请求安全关闭。", text)  # 会话照常走到 /exit

    def test_chat_route_is_never_announced_as_unsupported(self) -> None:
        routed = SimpleNamespace(outcome=None, handoff=chat_handoff())
        shell, output, conversation = self._shell(["你好", "/exit"], routed)
        shell.run_conversation(conversation=conversation, task_dispatch=FakeNoticeDispatcher())
        self.assertNotIn("本批暂不支持", output.getvalue())


if __name__ == "__main__":
    unittest.main()
