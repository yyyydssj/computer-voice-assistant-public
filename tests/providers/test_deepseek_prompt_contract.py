from __future__ import annotations

import json
import unittest

from app.contracts.routing import _INTENT_TOOLS, _MODES, _ROUTE_INTENTS
from app.providers.deepseek_adapter import (
    DeepSeekAdapterConfig,
    DeepSeekProviderAdapter,
    _ROUTE_SYSTEM_PROMPT,
)
from app.routing import RouteInput, parse_deepseek_raw_route_response
from app.contracts import RouteTrace

_REQUIRED_KEYS = (
    "schema_version",
    "status",
    "route",
    "intent",
    "handling_mode",
    "confidence",
    "slots",
    "required_confirmation",
    "tool_scope",
)
# 这两个意图是本地路由器失败归一化的产物，绝不应由模型输出。
_LOCAL_FAILURE_INTENTS = frozenset({"router_unavailable", "invalid_router_output"})


class WrapperTransport:
    """真实模型曾返回的 ``{"response": ...}`` 包装形状（结构等价，固定非敏感文本）。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def send(self, request: object, *, timeout_seconds: float) -> str:
        return self.raw


class DeepSeekPromptContractTests(unittest.TestCase):
    def test_prompt_lists_every_required_contract_key(self) -> None:
        for key in _REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, _ROUTE_SYSTEM_PROMPT)

    def test_prompt_covers_every_route_intent_and_handling_mode(self) -> None:
        for route, intents in _ROUTE_INTENTS.items():
            with self.subTest(route=route):
                self.assertIn(route, _ROUTE_SYSTEM_PROMPT)
                self.assertIn(_MODES[route], _ROUTE_SYSTEM_PROMPT)
            for intent in intents:
                if intent in _LOCAL_FAILURE_INTENTS:
                    continue
                with self.subTest(intent=intent):
                    self.assertIn(intent, _ROUTE_SYSTEM_PROMPT)

    def test_prompt_covers_every_allowed_tool_name(self) -> None:
        for intent, tools in _INTENT_TOOLS.items():
            for tool in tools:
                with self.subTest(intent=intent, tool=tool):
                    self.assertIn(tool, _ROUTE_SYSTEM_PROMPT)

    def test_prompt_forbids_wrapper_object_and_fences(self) -> None:
        self.assertIn('{"response"', _ROUTE_SYSTEM_PROMPT)
        self.assertIn("Markdown", _ROUTE_SYSTEM_PROMPT)

    def test_embedded_example_is_accepted_by_the_strict_parser(self) -> None:
        example = _ROUTE_SYSTEM_PROMPT.split("Example: ", 1)[1]

        parsed = parse_deepseek_raw_route_response(example)

        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["route"], "task")
        for key in _REQUIRED_KEYS:
            with self.subTest(key=key):
                if key == "schema_version":
                    # 解析边界会消费掉 schema 标记，不向上层透传。
                    self.assertNotIn(key, parsed)
                    continue
                self.assertIn(key, parsed)

    def test_embedded_example_is_single_line_bare_json(self) -> None:
        example = _ROUTE_SYSTEM_PROMPT.split("Example: ", 1)[1]

        self.assertTrue(example.startswith("{"))
        self.assertTrue(example.endswith("}"))
        self.assertNotIn("```", example)
        self.assertEqual(len(example.splitlines()), 1)
        self.assertEqual(sorted(json.loads(example)), sorted(_REQUIRED_KEYS))

    def test_wrapper_shaped_model_output_is_rejected_and_fail_closed(self) -> None:
        trace = self.trace()
        transport = WrapperTransport(json.dumps({"response": "自由文本回答"}, ensure_ascii=False))
        adapter = DeepSeekProviderAdapter(
            transport,  # type: ignore[arg-type]
            config=DeepSeekAdapterConfig(enabled=True, thinking="disabled", max_tokens=256),
        )

        result = adapter.route(RouteInput(raw_text="你好", trace=trace))

        self.assertEqual(result.router.status, "invalid_response")
        self.assertEqual(result.route, "ambiguous")
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})
        self.assertNotIn("自由文本", repr(result))

    def trace(self) -> RouteTrace:
        return RouteTrace(
            request_id="request-prompt",
            conversation_id="conversation-prompt",
            turn_id="turn-prompt",
            source="text_shell",
            raw_text="你好",
            created_at="2026-09-30T10:00:00Z",
        )

    def test_prompt_states_the_minimal_low_confidence_rule(self) -> None:
        self.assertIn('"status":"low_confidence"', _ROUTE_SYSTEM_PROMPT)
        self.assertIn("no other keys", _ROUTE_SYSTEM_PROMPT)
        self.assertIn("must_clarify", _ROUTE_SYSTEM_PROMPT)

    def test_minimal_low_confidence_shape_is_accepted(self) -> None:
        minimal = json.dumps(
            {
                "schema_version": "deepseek-route/v1",
                "status": "low_confidence",
                "confidence": 0.3,
            }
        )

        parsed = parse_deepseek_raw_route_response(minimal)

        self.assertEqual(parsed["status"], "low_confidence")
        self.assertEqual(sorted(parsed), ["confidence", "status"])

    def test_low_confidence_with_full_route_fields_is_rejected(self) -> None:
        """本轮真实探测观测到的失败形状：low_confidence 却带完整 9 键。"""

        bloated = json.dumps(
            {
                "schema_version": "deepseek-route/v1",
                "status": "low_confidence",
                "route": "ambiguous",
                "intent": "low_confidence",
                "handling_mode": "clarify",
                "confidence": 0.4,
                "slots": {},
                "required_confirmation": "must_clarify",
                "tool_scope": [],
            }
        )

        with self.assertRaises(ValueError):
            parse_deepseek_raw_route_response(bloated)

    def test_full_ambiguous_shape_lands_as_non_executable_clarify(self) -> None:
        raw = json.dumps(
            {
                "schema_version": "deepseek-route/v1",
                "status": "success",
                "route": "ambiguous",
                "intent": "missing_target",
                "handling_mode": "clarify",
                "confidence": 0.85,
                "slots": {},
                "required_confirmation": "must_clarify",
                "tool_scope": [],
            }
        )
        transport = WrapperTransport(raw)
        adapter = DeepSeekProviderAdapter(
            transport,  # type: ignore[arg-type]
            config=DeepSeekAdapterConfig(enabled=True, thinking="disabled", max_tokens=256),
        )

        result = adapter.route(RouteInput(raw_text="你好", trace=self.trace()))

        self.assertEqual(result.router.status, "success")
        self.assertEqual(result.route, "ambiguous")
        self.assertEqual(result.intent, "missing_target")
        self.assertEqual(result.handling_mode, "clarify")
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})

    def test_full_ambiguous_shape_below_threshold_normalizes_to_low_confidence(self) -> None:
        raw = json.dumps(
            {
                "schema_version": "deepseek-route/v1",
                "status": "success",
                "route": "ambiguous",
                "intent": "missing_target",
                "handling_mode": "clarify",
                "confidence": 0.42,
                "slots": {},
                "required_confirmation": "must_clarify",
                "tool_scope": [],
            }
        )
        transport = WrapperTransport(raw)
        adapter = DeepSeekProviderAdapter(
            transport,  # type: ignore[arg-type]
            config=DeepSeekAdapterConfig(enabled=True, thinking="disabled", max_tokens=256),
        )

        result = adapter.route(RouteInput(raw_text="你好", trace=self.trace()))

        self.assertEqual(result.router.status, "low_confidence")
        self.assertEqual(result.router.failure_code, "low_confidence")
        self.assertEqual(result.route, "ambiguous")
        self.assertEqual(result.tool_scope, ())
        self.assertEqual(result.slots, {})


class QuestionIsNotUnsupportedPromptTests(unittest.TestCase):
    """2026-10-23 真机缺陷：说「你能关闭应用吗？」被判 ``unsupported`` 并如实拒绝。

    本地句式层早就有疑问/否定白名单，Provider 侧没有 ⇒ 除了代码里的确定性兜底
    （`downgrade_question_unsupported`），提示词也要写明"疑问/否定/假设不算不支持"。
    """

    def test_prompt_excludes_questions_from_the_unsupported_route(self) -> None:
        self.assertIn("Questions, negations and hypotheticals are NOT unsupported", _ROUTE_SYSTEM_PROMPT)
        self.assertIn("route it to chat", _ROUTE_SYSTEM_PROMPT)
        self.assertIn("explicit imperative command", _ROUTE_SYSTEM_PROMPT)

    def test_prompt_names_the_question_negation_markers(self) -> None:
        for marker in ("吗", "怎么", "能不能", "不要", "如果"):
            with self.subTest(marker=marker):
                self.assertIn(marker, _ROUTE_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
