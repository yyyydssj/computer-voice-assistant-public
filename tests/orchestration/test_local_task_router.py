"""P10 · T1 批次 1 红测：**本地任务路由**（确定性句式匹配）。

全部离线：只构造 `RouteInput`/`RouteTrace`；不联网、不执行、不碰白名单与安全链。
"""

from __future__ import annotations

import unittest

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.routing import RouteInput
from app.runtime.orchestration import (
    INTENT_UNSUPPORTED,
    OP_CLOSE_APPLICATION,
    OP_DELETE_FILES,
    QUESTION_FALLBACK_PROVIDER,
    ROUTE_CHAT,
    ROUTE_UNSUPPORTED,
    LocalTaskRouter,
    TaskFirstRouter,
    downgrade_question_unsupported,
    match_application_name,
    match_unsupported_command,
    normalize_command,
)

CREATED_AT = "2026-10-13T00:00:00Z"


def make_request(text: str, *, source: str = "text_shell") -> RouteInput:
    trace = RouteTrace(
        request_id="req-1",
        conversation_id="conv-1",
        turn_id="turn-1",
        source=source,
        raw_text=text,
        created_at=CREATED_AT,
    )
    return RouteInput(raw_text=text, trace=trace)


class NormalizeCommandTests(unittest.TestCase):
    def test_strips_honorific_prefix_and_tail(self) -> None:
        self.assertEqual(normalize_command("请帮我打开计算器。"), "打开计算器")
        self.assertEqual(normalize_command("麻烦打开记事本吧"), "打开记事本")
        self.assertEqual(normalize_command("能不能帮我启动计算器"), "启动计算器")

    def test_full_width_and_inner_spaces(self) -> None:
        self.assertEqual(normalize_command("　打开　计算器！　"), "打开 计算器")

    def test_english_keeps_one_space(self) -> None:
        self.assertEqual(normalize_command("  Open   Calculator "), "Open Calculator")

    def test_empty_and_non_string(self) -> None:
        self.assertEqual(normalize_command("   "), "")
        with self.assertRaises(TypeError):
            normalize_command(None)  # type: ignore[arg-type]


class MatchApplicationNameTests(unittest.TestCase):
    def test_chinese_prefix_verbs(self) -> None:
        for text in ("打开计算器", "启动记事本", "运行计算器", "开启记事本"):
            with self.subTest(text=text):
                matched, name = match_application_name(text)
                self.assertTrue(matched)
                self.assertIn(name, {"计算器", "记事本"})

    def test_chinese_suffix_form(self) -> None:
        self.assertEqual(match_application_name("把记事本打开"), (True, "记事本"))

    def test_verb_without_target_is_matched_but_empty(self) -> None:
        for text in ("打开", "打开。", "帮我打开吧", "open"):
            with self.subTest(text=text):
                self.assertEqual(match_application_name(text), (True, ""))

    def test_english_prefix(self) -> None:
        matched, name = match_application_name("Open Notepad")
        self.assertTrue(matched)
        self.assertEqual(name, "Notepad")

    def test_non_task_text_is_not_matched(self) -> None:
        for text in ("今天天气怎么样", "你好", "", "计算器"):
            with self.subTest(text=text):
                self.assertEqual(match_application_name(text), (False, ""))


class LocalTaskRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = LocalTaskRouter()

    def test_open_application_becomes_a_task_handoff(self) -> None:
        handoff = self.router.route(make_request("请帮我打开计算器。"))
        assert handoff is not None
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.intent, "open_application")
        self.assertEqual(handoff.handling_mode, "plan_then_execute")
        self.assertEqual(handoff.required_confirmation, "defer_to_safety")
        self.assertEqual(handoff.tool_scope, ("start_whitelisted_application",))
        self.assertGreaterEqual(handoff.confidence, 0.80)
        slot = handoff.slots["application_name"]
        self.assertEqual(slot.value, "计算器")
        self.assertIs(slot.confirmed, False)
        self.assertEqual(handoff.router.status, "success")
        self.assertEqual(handoff.router.provider, "local")

    def test_trace_is_preserved_for_both_sources(self) -> None:
        for source in ("text_shell", "asr"):
            with self.subTest(source=source):
                request = make_request("打开记事本", source=source)
                handoff = self.router.route(request)
                assert handoff is not None
                self.assertIs(handoff.trace, request.trace)
                self.assertEqual(handoff.trace.raw_text, "打开记事本")

    def test_verb_without_target_requires_clarification(self) -> None:
        handoff = self.router.route(make_request("打开"))
        assert handoff is not None
        self.assertEqual(handoff.route, "ambiguous")
        self.assertEqual(handoff.intent, "missing_target")
        self.assertEqual(handoff.handling_mode, "clarify")
        self.assertEqual(handoff.required_confirmation, "must_clarify")
        self.assertEqual(handoff.tool_scope, ())
        self.assertEqual(dict(handoff.slots), {})
        self.assertLess(handoff.confidence, 0.80)

    def test_chat_text_returns_none_so_fallback_can_take_over(self) -> None:
        self.assertIsNone(self.router.route(make_request("今天天气怎么样")))
        self.assertIsNone(self.router.route(make_request("你好呀")))

    def test_rejects_non_route_input(self) -> None:
        with self.assertRaises(TypeError):
            self.router.route("打开计算器")  # type: ignore[arg-type]


class _RecordingRouter:
    def __init__(self, handoff: object | None) -> None:
        self._handoff = handoff
        self.calls = 0

    def route(self, request: RouteInput) -> object:
        self.calls += 1
        return self._handoff


class WakeNameTests(unittest.TestCase):
    """真机教训（2026-10-13，`--voice`）：ASR 会把唤醒名一起识别进来
    （说"打开计算器"→「小饼打开计算器。」）⇒ 句首要能**精确**剥掉调用方给的称呼。"""

    NAMES = ("小饼",)

    def test_normalize_strips_a_leading_wake_name(self) -> None:
        self.assertEqual(normalize_command("小饼打开计算器", names=self.NAMES), "打开计算器")
        self.assertEqual(normalize_command("小饼，打开计算器。", names=self.NAMES), "打开计算器")
        self.assertEqual(normalize_command("小饼 打开记事本", names=self.NAMES), "打开记事本")
        self.assertEqual(normalize_command("小饼请帮我打开计算器", names=self.NAMES), "打开计算器")

    def test_without_names_behaviour_is_unchanged(self) -> None:
        self.assertEqual(normalize_command("小饼打开计算器"), "小饼打开计算器")
        self.assertIsNone(LocalTaskRouter().route(make_request("小饼打开计算器")))

    def test_router_matches_with_names_and_extracts_the_target(self) -> None:
        router = LocalTaskRouter(names=self.NAMES)
        for text in ("小饼打开计算器。", "小饼，打开计算器", "小饼 打开 计算器"):
            with self.subTest(text=text):
                handoff = router.route(make_request(text, source="asr"))
                assert handoff is not None
                self.assertEqual(handoff.route, "task")
                self.assertEqual(handoff.slots["application_name"].value, "计算器")

    def test_non_task_sentence_with_a_wake_name_is_not_a_task(self) -> None:
        router = LocalTaskRouter(names=self.NAMES)
        self.assertIsNone(router.route(make_request("小饼，你能听见我说话吗？", source="asr")))

    def test_names_are_deduplicated_and_sorted_longest_first(self) -> None:
        router = LocalTaskRouter(names=("小饼", "小饼", "小饼助手"))
        self.assertEqual(router.names, ("小饼助手", "小饼"))
        handoff = router.route(make_request("小饼助手，打开记事本"))
        assert handoff is not None
        self.assertEqual(handoff.slots["application_name"].value, "记事本")

    def test_names_must_be_strings(self) -> None:
        with self.assertRaises(TypeError):
            LocalTaskRouter(names=("小饼", 1))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            normalize_command("x", names=("小饼", None))  # type: ignore[arg-type]

    def test_match_application_name_accepts_names(self) -> None:
        matched, name = match_application_name("小饼，打开计算器。", names=self.NAMES)
        self.assertTrue(matched)
        self.assertEqual(name, "计算器")


class TaskFirstRouterTests(unittest.TestCase):
    def test_primary_hit_skips_fallback(self) -> None:
        primary = LocalTaskRouter()
        fallback = _RecordingRouter(None)
        handoff = TaskFirstRouter(primary, fallback).route(make_request("打开计算器"))
        self.assertEqual(handoff.route, "task")
        self.assertEqual(fallback.calls, 0)

    def test_miss_delegates_to_fallback(self) -> None:
        sentinel = object()
        fallback = _RecordingRouter(sentinel)
        result = TaskFirstRouter(LocalTaskRouter(), fallback).route(make_request("今天天气怎么样"))
        self.assertIs(result, sentinel)
        self.assertEqual(fallback.calls, 1)

    def test_requires_callable_routes(self) -> None:
        with self.assertRaises(TypeError):
            TaskFirstRouter(object(), _RecordingRouter(None))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            TaskFirstRouter(LocalTaskRouter(), object())  # type: ignore[arg-type]


class MatchUnsupportedCommandTests(unittest.TestCase):
    """P10 · T1 #12：本批不支持的句式必须**可识别**（如实拒绝），且不得吃掉闲聊。"""

    def test_close_application_forms_are_recognised(self) -> None:
        for text in (
            "关闭计算器", "关闭记事本。", "把记事本关掉", "退出程序", "关闭它",
            "你能不能帮我关闭计算器", "你帮我把记事本关掉", "关闭窗口",
        ):
            with self.subTest(text=text):
                self.assertEqual(match_unsupported_command(text), OP_CLOSE_APPLICATION)

    def test_delete_forms_are_recognised(self) -> None:
        for text in ("删除文件", "删掉 report.txt", "把桌面上的文件删除", "清空回收站", "移除目录"):
            with self.subTest(text=text):
                self.assertEqual(match_unsupported_command(text), OP_DELETE_FILES)

    def test_questions_negations_and_chat_are_not_matched(self) -> None:
        for text in (
            "关闭是什么意思", "不要关闭计算器", "你知道怎么关闭计算器吗", "今天天气怎么样",
            "你好呀", "你能听见我说话吗", "打开计算器", "把记事本打开", "计算器",
            "删除", "关闭", "", "   ",
        ):
            with self.subTest(text=text):
                self.assertIsNone(match_unsupported_command(text))

    def test_wake_name_is_stripped_before_matching(self) -> None:
        self.assertEqual(
            match_unsupported_command("小饼，关闭计算器。", names=("小饼",)),
            OP_CLOSE_APPLICATION,
        )
        self.assertIsNone(match_unsupported_command("小饼，关闭计算器。"))

    def test_non_string_raises(self) -> None:
        with self.assertRaises(TypeError):
            match_unsupported_command(None)  # type: ignore[arg-type]


class LocalTaskRouterUnsupportedTests(unittest.TestCase):
    """#12：命中不支持句式 ⇒ 契约原生 ``unsupported`` 交接（**不**再静默落回聊天）。"""

    def test_close_application_becomes_an_unsupported_handoff(self) -> None:
        request = make_request("关闭计算器")
        handoff = LocalTaskRouter().route(request)
        assert handoff is not None
        self.assertIs(handoff.trace, request.trace)
        self.assertEqual(handoff.route, ROUTE_UNSUPPORTED)
        self.assertEqual(handoff.intent, INTENT_UNSUPPORTED)
        self.assertEqual(handoff.handling_mode, "reject")
        self.assertEqual(handoff.required_confirmation, "none")
        self.assertEqual(handoff.tool_scope, ())
        self.assertGreaterEqual(handoff.confidence, 0.80)
        self.assertEqual(handoff.slots["operation"].value, OP_CLOSE_APPLICATION)
        self.assertEqual(handoff.router.provider, "local")
        self.assertEqual(handoff.router.status, "success")

    def test_delete_files_becomes_an_unsupported_handoff(self) -> None:
        handoff = LocalTaskRouter().route(make_request("删除文件"))
        assert handoff is not None
        self.assertEqual(handoff.route, ROUTE_UNSUPPORTED)
        self.assertEqual(handoff.slots["operation"].value, OP_DELETE_FILES)

    def test_detect_can_be_disabled_for_old_behaviour(self) -> None:
        router = LocalTaskRouter(detect_unsupported=False)
        self.assertFalse(router.detect_unsupported)
        self.assertIsNone(router.route(make_request("关闭计算器")))

    def test_detect_flag_must_be_bool(self) -> None:
        with self.assertRaises(TypeError):
            LocalTaskRouter(detect_unsupported="yes")  # type: ignore[arg-type]

    def test_unsupported_handoff_skips_the_fallback(self) -> None:
        sentinel = object()
        fallback = _RecordingRouter(sentinel)
        handoff = TaskFirstRouter(LocalTaskRouter(), fallback).route(make_request("关闭计算器"))
        self.assertIsNot(handoff, sentinel)
        self.assertEqual(getattr(handoff, "route", None), ROUTE_UNSUPPORTED)
        self.assertEqual(fallback.calls, 0)

    def test_chat_text_still_delegates_to_the_fallback(self) -> None:
        sentinel = object()
        fallback = _RecordingRouter(sentinel)
        result = TaskFirstRouter(LocalTaskRouter(), fallback).route(make_request("今天天气怎么样"))
        self.assertIs(result, sentinel)
        self.assertEqual(fallback.calls, 1)

    def test_open_application_still_wins_over_unsupported_detection(self) -> None:
        handoff = LocalTaskRouter().route(make_request("把记事本打开"))
        assert handoff is not None
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.intent, "open_application")


def _provider_handoff(
    request: RouteInput,
    *,
    route: str = ROUTE_UNSUPPORTED,
    intent: str = INTENT_UNSUPPORTED,
    handling_mode: str = "reject",
    confidence: float = 0.95,
) -> RouteHandoff:
    """构造一条"Provider 给的"交接（真机缺陷里它就是 unsupported/unimplemented_capability）。"""

    return RouteHandoff(
        trace=request.trace,
        route=route,
        intent=intent,
        handling_mode=handling_mode,
        confidence=confidence,
        slots={},
        required_confirmation="none",
        tool_scope=(),
        router=RouterStatus(
            provider="deepseek",
            model_id="deepseek-chat",
            model_version="1",
            status="success",
            failure_code=None,
        ),
    )


class _FixedRouter:
    def __init__(self, handoff: RouteHandoff) -> None:
        self._handoff = handoff
        self.calls = 0

    def route(self, request: RouteInput) -> RouteHandoff:
        self.calls += 1
        return self._handoff


class QuestionFallbackTests(unittest.TestCase):
    """2026-10-23 真机缺陷：Provider 把**疑问/否定/假设句**判成"本批不支持"并如实拒绝。

    本地句式层有 ``UNSUPPORTED_BLOCKERS`` 白名单，Provider 路由没有 ⇒ 事件后补一条与模型
    无关的确定性兜底：疑问/否定/假设 ⇒ 回落聊天；只有明确命令句才保持 unsupported。
    """

    QUESTION_TEXTS = (
        "你能关闭应用吗？",
        "怎么关闭记事本",
        "不要删除桌面文件",
        "如果关闭应用会怎样",
        "可以清空回收站吗",
    )

    def test_question_and_negation_unsupported_are_downgraded_to_chat(self) -> None:
        for text in self.QUESTION_TEXTS:
            with self.subTest(text=text):
                request = make_request(text)
                fallback = _FixedRouter(_provider_handoff(request))
                handoff = TaskFirstRouter(LocalTaskRouter(), fallback).route(request)
                self.assertEqual(fallback.calls, 1)  # 仍然问了 Provider（此处是替身）
                self.assertEqual(handoff.route, ROUTE_CHAT)
                self.assertEqual(handoff.intent, "general_chat")
                self.assertEqual(handoff.handling_mode, "chat_reply")
                self.assertEqual(handoff.required_confirmation, "none")
                self.assertEqual(handoff.tool_scope, ())
                self.assertIs(handoff.trace, request.trace)
                self.assertEqual(handoff.router.provider, QUESTION_FALLBACK_PROVIDER)
                self.assertGreaterEqual(handoff.confidence, 0.80)

    def test_explicit_imperative_command_keeps_unsupported(self) -> None:
        for text in ("关闭应用", "删除桌面文件", "把记事本关掉", "你能不能帮我关闭计算器？", "那你能不能帮我关闭计算器？"):
            with self.subTest(text=text):
                request = make_request(text)
                handoff = _provider_handoff(request)
                result = downgrade_question_unsupported(handoff, request)
                self.assertIs(result, handoff)
                self.assertEqual(result.route, ROUTE_UNSUPPORTED)

    def test_negated_imperative_is_still_chat(self) -> None:
        """「不要帮我关闭计算器」是劝阻，不是请求 ⇒ 仍回落聊天。"""

        for text in ("不要帮我关闭计算器", "别帮我删除文件", "如果帮我关闭计算器会怎样"):
            with self.subTest(text=text):
                request = make_request(text)
                handoff = _provider_handoff(request)
                result = downgrade_question_unsupported(handoff, request)
                self.assertEqual(result.route, ROUTE_CHAT)

    def test_non_unsupported_handoffs_are_untouched(self) -> None:
        request = make_request("你能关闭应用吗？")
        chat = _provider_handoff(
            request, route=ROUTE_CHAT, intent="general_chat", handling_mode="chat_reply"
        )
        self.assertIs(downgrade_question_unsupported(chat, request), chat)

    def test_without_request_text_nothing_changes(self) -> None:
        request = make_request("关闭应用")
        handoff = _provider_handoff(request)
        self.assertIs(downgrade_question_unsupported(handoff, object()), handoff)

    def test_bare_honorific_question_is_a_question_not_a_command(self) -> None:
        """2026-11-08 口径 A：句首「能不能」不改变句子类型。

        只有疑问标记、没有祈使标记 ⇒ 是在问"能不能"，交聊天（由模型如实回答做不到），
        **不**再因为 `normalize_command` 剥掉前缀就把它当命令拒绝。
        """

        for text in (
            "能不能退出程序",
            "你能不能退出程序",
            "能不能关闭计算器",
            "能不能删除文件",
            "小饼，能不能退出程序。",
            "能不能退出程序？",
            "那你能不能退出程序？",
        ):
            with self.subTest(text=text):
                self.assertIsNone(match_unsupported_command(text))

    def test_imperative_honorific_still_matches(self) -> None:
        """有祈使标记（帮我/给我/请你…）就是真的请求：#12 口径不变，仍如实拒绝。"""

        cases = {
            "你能不能帮我关闭计算器": OP_CLOSE_APPLICATION,
            "你能不能帮我关闭计算器？": OP_CLOSE_APPLICATION,
            "那你能不能帮我关闭计算器？": OP_CLOSE_APPLICATION,
            "能不能帮我关掉程序": OP_CLOSE_APPLICATION,
            "可以帮我关闭计算器": OP_CLOSE_APPLICATION,
            "帮我关闭计算器吗": OP_CLOSE_APPLICATION,
            "能帮我关闭计算器吗": OP_CLOSE_APPLICATION,
            "帮我关计算器": OP_CLOSE_APPLICATION,
            "关计算器": OP_CLOSE_APPLICATION,
            "给我退出程序": OP_CLOSE_APPLICATION,
            "请你帮我关闭计算器": OP_CLOSE_APPLICATION,
            "麻烦你帮我删除文件": OP_DELETE_FILES,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(match_unsupported_command(text), expected)

    def test_question_and_imperative_together_follow_imperative(self) -> None:
        """疑问标记与祈使标记同时出现 ⇒ 按"请求"处理（中文礼貌命令的常见形态）。"""

        self.assertEqual(
            match_unsupported_command("能不能帮我关掉程序"),
            OP_CLOSE_APPLICATION,
        )


if __name__ == "__main__":
    unittest.main()
