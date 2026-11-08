"""P10 · T1 批次 1 红测：**白名单应用提议器**（本地确定性映射）。

全部离线：用临时目录里的假可执行文件路径构造 `ApplicationSpec`；不启动任何进程、不碰真实白名单。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus, SlotCandidate
from app.executor import ApplicationSpec
from app.routing import RouteInput
from app.runtime.orchestration import (
    APPLICATION_ALIASES,
    AppLaunchError,
    build_launch_proposal,
    default_applications,
    describe_target,
    normalize_application_name,
    resolve_application_id,
)
from app.runtime.orchestration.app_launch import (
    CODE_AMBIGUOUS,
    CODE_NAME_MISSING,
    CODE_UNKNOWN,
    CODE_UNSUPPORTED,
    DEFAULT_ACTION_ID,
    LAUNCH_TOOL_NAME,
)
from app.runtime.orchestration.local_task_router import LocalTaskRouter

CREATED_AT = "2026-10-13T00:00:00Z"
FAKE_ROOT = Path(tempfile.gettempdir()) / "voice-assistant-app-launch-test"


def spec(application_id: str, name: str, *, digest: str | None = None) -> ApplicationSpec:
    return ApplicationSpec(
        application_id=application_id,
        application_name=name,
        executable_path=str(FAKE_ROOT / f"{application_id}.exe"),
        executable_hash=digest,
    )


def trace(text: str = "打开计算器") -> RouteTrace:
    return RouteTrace(
        request_id="req-1",
        conversation_id="conv-1",
        turn_id="turn-1",
        source="text_shell",
        raw_text=text,
        created_at=CREATED_AT,
    )


def handoff(
    *,
    route: str = "task",
    intent: str = "open_application",
    handling_mode: str = "plan_then_execute",
    tool_scope: tuple[str, ...] = ("start_whitelisted_application",),
    slots: dict[str, SlotCandidate] | None = None,
    confidence: float = 0.95,
) -> RouteHandoff:
    return RouteHandoff(
        trace=trace(),
        route=route,
        intent=intent,
        handling_mode=handling_mode,
        confidence=confidence,
        slots=slots if slots is not None else {},
        required_confirmation="defer_to_safety",
        tool_scope=tool_scope,
        router=RouterStatus(
            provider="local",
            model_id="task-rule-router",
            model_version="1",
            status="success",
            failure_code=None,
        ),
    )


class NormalizeApplicationNameTests(unittest.TestCase):
    def test_normalizes_case_width_and_punctuation(self) -> None:
        self.assertEqual(normalize_application_name(" 计算器 "), "计算器")
        self.assertEqual(normalize_application_name("Calculator"), "calculator")
        self.assertEqual(normalize_application_name("Ｎｏｔｅｐａｄ"), "notepad")
        self.assertEqual(normalize_application_name("记事本。"), "记事本")

    def test_rejects_non_string(self) -> None:
        with self.assertRaises(TypeError):
            normalize_application_name(123)  # type: ignore[arg-type]


class DefaultApplicationsTests(unittest.TestCase):
    def test_first_batch_whitelist_is_calc_and_notepad(self) -> None:
        specs = default_applications(system_root=r"C:\Windows")
        self.assertEqual(set(specs), {"calc", "notepad"})
        for application_id, item in specs.items():
            with self.subTest(application_id=application_id):
                self.assertEqual(item.application_id, application_id)
                self.assertTrue(Path(item.executable_path).is_absolute())
                self.assertEqual(item.executable_path, rf"C:\Windows\System32\{application_id}.exe")
                self.assertIsNone(item.executable_hash)  # 装配时（批次 2）再计算并校验

    def test_trailing_separator_is_tolerated(self) -> None:
        specs = default_applications(system_root="D:\\Win\\")
        self.assertEqual(specs["calc"].executable_path, r"D:\Win\System32\calc.exe")


class ResolveApplicationIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.apps = {"calc": spec("calc", "计算器"), "notepad": spec("notepad", "记事本")}

    def test_chinese_and_english_aliases(self) -> None:
        for name in ("计算器", "计算器应用", "calc", "Calculator", "  CALC  "):
            with self.subTest(name=name):
                self.assertEqual(resolve_application_id(name, applications=self.apps), "calc")
        for name in ("记事本", "记事簿", "notepad", "note"):
            with self.subTest(name=name):
                self.assertEqual(resolve_application_id(name, applications=self.apps), "notepad")

    def test_resolves_by_application_id_directly(self) -> None:
        self.assertEqual(resolve_application_id("notepad", applications=self.apps), "notepad")

    def test_unknown_application_is_refused(self) -> None:
        with self.assertRaises(AppLaunchError) as ctx:
            resolve_application_id("微信", applications=self.apps)
        self.assertEqual(ctx.exception.code, CODE_UNKNOWN)

    def test_empty_name_is_refused(self) -> None:
        with self.assertRaises(AppLaunchError) as ctx:
            resolve_application_id("   ", applications=self.apps)
        self.assertEqual(ctx.exception.code, CODE_NAME_MISSING)

    def test_ambiguous_alias_table_is_refused(self) -> None:
        aliases = {"同名": "calc", "同名 ": "notepad"}
        with self.assertRaises(AppLaunchError) as ctx:
            resolve_application_id("同名", applications=self.apps, aliases=aliases)
        self.assertEqual(ctx.exception.code, CODE_AMBIGUOUS)

    def test_alias_pointing_to_unregistered_id_is_refused(self) -> None:
        with self.assertRaises(AppLaunchError) as ctx:
            resolve_application_id("幽灵", applications=self.apps, aliases={"幽灵": "ghost"})
        self.assertEqual(ctx.exception.code, CODE_UNKNOWN)


class BuildLaunchProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.apps = {"calc": spec("calc", "计算器"), "notepad": spec("notepad", "记事本")}

    def test_router_output_becomes_a_single_launch_action(self) -> None:
        routed = LocalTaskRouter().route(
            RouteInput(raw_text="请帮我打开计算器。", trace=trace("请帮我打开计算器。"))
        )
        assert routed is not None
        proposal = build_launch_proposal(routed, applications=self.apps)
        self.assertEqual(proposal.action_id, DEFAULT_ACTION_ID)
        self.assertEqual(proposal.tool_name, LAUNCH_TOOL_NAME)
        self.assertEqual(dict(proposal.arguments), {"application_id": "calc"})
        self.assertEqual(proposal.order, 1)

    def test_dangerous_route_is_refused(self) -> None:
        blocked = handoff(
            route="dangerous",
            intent="delete_file",
            handling_mode="plan_then_safety",
            tool_scope=("move_to_recycle_bin",),
            slots={"target_path": SlotCandidate(value="x.txt", source="utterance", confidence=0.9, confirmed=False)},
        )
        with self.assertRaises(AppLaunchError) as ctx:
            build_launch_proposal(blocked, applications=self.apps)
        self.assertEqual(ctx.exception.code, CODE_UNSUPPORTED)

    def test_file_operation_intent_is_refused(self) -> None:
        blocked = handoff(
            intent="file_operation",
            tool_scope=("create_text_file",),
            slots={"target_path": SlotCandidate(value="x.txt", source="utterance", confidence=0.9, confirmed=False)},
        )
        with self.assertRaises(AppLaunchError) as ctx:
            build_launch_proposal(blocked, applications=self.apps)
        self.assertEqual(ctx.exception.code, CODE_UNSUPPORTED)

    def test_missing_or_non_string_slot_is_refused(self) -> None:
        with self.assertRaises(AppLaunchError) as ctx:
            build_launch_proposal(handoff(), applications=self.apps)
        self.assertEqual(ctx.exception.code, CODE_NAME_MISSING)
        bad = handoff(
            slots={
                "application_name": SlotCandidate(value=7, source="utterance", confidence=0.9, confirmed=False)
            }
        )
        with self.assertRaises(AppLaunchError) as ctx2:
            build_launch_proposal(bad, applications=self.apps)
        self.assertEqual(ctx2.exception.code, CODE_NAME_MISSING)

    def test_unregistered_application_is_refused(self) -> None:
        routed = LocalTaskRouter().route(
            RouteInput(raw_text="打开微信", trace=trace("打开微信"))
        )
        assert routed is not None
        with self.assertRaises(AppLaunchError) as ctx:
            build_launch_proposal(routed, applications=self.apps)
        self.assertEqual(ctx.exception.code, CODE_UNKNOWN)

    def test_rejects_non_handoff(self) -> None:
        with self.assertRaises(TypeError):
            build_launch_proposal("打开计算器")  # type: ignore[arg-type]


class DescribeTargetTests(unittest.TestCase):
    def test_describes_name_path_and_unregistered_hash(self) -> None:
        apps = {"calc": spec("calc", "计算器")}
        text = describe_target("calc", applications=apps)
        self.assertIn("计算器", text)
        self.assertIn("calc.exe", text)
        self.assertIn("哈希未登记", text)

    def test_truncates_registered_hash_to_twelve_hex(self) -> None:
        digest = "sha256:" + "a" * 64
        apps = {"calc": spec("calc", "计算器", digest=digest)}
        text = describe_target("calc", applications=apps)
        self.assertIn(f"{digest[:19]}…", text)

    def test_unknown_id_is_refused(self) -> None:
        with self.assertRaises(AppLaunchError) as ctx:
            describe_target("ghost", applications={"calc": spec("calc", "计算器")})
        self.assertEqual(ctx.exception.code, CODE_UNKNOWN)


class AliasesExposureTests(unittest.TestCase):
    def test_default_alias_table_only_points_at_first_batch_ids(self) -> None:
        self.assertEqual(set(APPLICATION_ALIASES.values()), {"calc", "notepad"})


if __name__ == "__main__":
    unittest.main()
