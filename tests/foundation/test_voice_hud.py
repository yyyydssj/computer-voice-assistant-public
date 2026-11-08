"""P9 UI 批次 1：右下角状态提示窗（``app/ui/voice_hud.py``）的离线测试。

全部用注入的 fake api，**不打开任何真实窗口**、不联网、不读写文件；真实 Win32 部分只做
「可构造 + 函数签名已配置」级别的离线验证（必要时用 ``unittest.mock`` 替换 ``sys.platform`` /
``ctypes.WinDLL``）。真机窗口显示由 Lead 验收。
"""

from __future__ import annotations

import ast
import ctypes
import inspect
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ui import voice_hud as hud
from app.ui.voice_hud import (
    DEFAULT_SCREEN_SIZE,
    MODE_LEGACY,
    MODE_PIXEL,
    NOT_CREATED,
    STATE_LABELS,
    UNSUPPORTED_PLATFORM,
    VOICE_HUD_SCHEMA_VERSION,
    WINDOW_CLASS_NAME,
    WIN32_FAILED,
    VoiceHudConfig,
    VoiceHudError,
    VoiceHudState,
    VoiceStatusHud,
    build_win32_api,
    clamp_position,
    hud_text,
    screen_position_for,
    state_label,
)

MODULE_PATH = Path(hud.__file__)

EXPECTED_LABELS = {
    VoiceHudState.IDLE: "待机",
    VoiceHudState.LISTENING: "聆听",
    VoiceHudState.HEARING: "聆听",
    VoiceHudState.TRANSCRIBING: "聆听",
    VoiceHudState.THINKING: "聆听",
    VoiceHudState.SPEAKING: "播报",
    VoiceHudState.DEGRADED: "待机",
    VoiceHudState.FAILED: "失败",
    VoiceHudState.STOPPED: "待机",
}
VISIBLE_LABELS = ("待机", "聆听", "播报", "失败")
"""简化后的可见状态只有 4 种文案；内部 9 态映射到它们（失败不并入待机）。"""

# 测试用的“识别文本/回复文本/路径/异常正文”样本：任何一项出现在窗口文本里都算失败。
SECRET_TEXTS = (
    "帮我把机密文件复制到 D:\\secret\\plan.docx",
    "好的，已经完成",
    "Traceback (most recent call last): OSError",
    "C:\\Users\\someone\\.env",
)


class FakeHudApi:
    """记录全部调用的注入替身；不持任何系统资源。"""

    def __init__(self, *, screen: tuple[int, int] = DEFAULT_SCREEN_SIZE,
                 offset: tuple[int, int] | None = None,
                 pump_results: list[int] | None = None,
                 failing: str | None = None) -> None:
        self.screen = screen
        self.offset = offset
        self.pump_results = list(pump_results or [])
        self.failing = failing
        self.created: list[dict[str, object]] = []
        self.moves: list[tuple[object, int, int, int, int]] = []
        self.texts: list[tuple[object, str, str]] = []
        self.shown: list[object] = []
        self.hidden: list[object] = []
        self.pumped = 0
        self.offsets_read = 0
        self.destroyed: list[object] = []

    def _guard(self, name: str) -> None:
        if self.failing == name:
            raise RuntimeError("win32 exploded: C:\\secret\\path")

    def create_window(self, *, class_name: str, title: str, width: int, height: int,
                      topmost: bool, opacity: int, drag_snap_px: int = 0,
                      drag_settle_ms: int = 0) -> object:
        self._guard("create_window")
        handle = f"handle-{len(self.created)}"
        self.created.append({
            "class_name": class_name, "title": title, "width": width, "height": height,
            "topmost": topmost, "opacity": opacity, "handle": handle,
            "drag_snap_px": drag_snap_px, "drag_settle_ms": drag_settle_ms,
        })
        return handle

    def move_window(self, handle: object, *, x: int, y: int, width: int, height: int) -> None:
        self._guard("move_window")
        self.moves.append((handle, x, y, width, height))

    def set_text(self, handle: object, *, title: str, body: str) -> None:
        self._guard("set_text")
        self.texts.append((handle, title, body))

    def show(self, handle: object) -> None:
        self._guard("show")
        self.shown.append(handle)

    def hide(self, handle: object) -> None:
        self._guard("hide")
        self.hidden.append(handle)

    def pump_messages(self) -> int:
        self._guard("pump_messages")
        self.pumped += 1
        if self.pump_results:
            return self.pump_results.pop(0)
        return 0

    def dragged_offset(self, handle: object) -> tuple[int, int] | None:
        self._guard("dragged_offset")
        self.offsets_read += 1
        offset = self.offset
        self.offset = None
        return offset

    def destroy_window(self, handle: object) -> None:
        self._guard("destroy_window")
        self.destroyed.append(handle)

    def screen_size(self) -> tuple[int, int]:
        self._guard("screen_size")
        return self.screen


def window_bodies(api: FakeHudApi) -> list[str]:
    return [body for _handle, _title, body in api.texts]


class LabelTests(unittest.TestCase):
    def test_state_labels_are_frozen_chinese_mapping(self) -> None:
        self.assertEqual(dict(STATE_LABELS), EXPECTED_LABELS)
        for state, label in EXPECTED_LABELS.items():
            self.assertEqual(state_label(state), label)

    def test_schema_version_and_class_name_are_frozen(self) -> None:
        # v2：9 态收敛为 4 态文案；v3：浅色卡片胶囊外观 + 逐像素渲染 + 窗口含阴影留白。
        self.assertEqual(VOICE_HUD_SCHEMA_VERSION, 3)
        self.assertEqual(WINDOW_CLASS_NAME, "ComputerVoiceAssistantHud")

    def test_labels_collapse_to_four_visible_states(self) -> None:
        self.assertEqual(set(STATE_LABELS), set(VoiceHudState))
        self.assertEqual(set(STATE_LABELS.values()), set(VISIBLE_LABELS))

    def test_hud_text_uses_fixed_title_and_single_line_body(self) -> None:
        for state, label in EXPECTED_LABELS.items():
            for turn in (0, 4):
                title, body = hud_text(state, turn_index=turn)
                self.assertEqual(title, "电脑语音助手")
                self.assertEqual(body, label)
                self.assertNotIn("\n", body)
                self.assertNotIn("第 ", body)

    def test_hud_text_ignores_turn_index(self) -> None:
        for turn in (0, -1, -99, 4):
            self.assertEqual(hud_text(VoiceHudState.THINKING, turn_index=turn)[1], "聆听")

    def test_state_label_of_unknown_value_falls_back_without_echo(self) -> None:
        self.assertEqual(state_label("未定义状态"), "待机")
        self.assertEqual(state_label(None), "待机")


class ConfigTests(unittest.TestCase):
    def test_default_config_matches_frozen_values(self) -> None:
        config = VoiceHudConfig()
        self.assertEqual(
            (config.width, config.height, config.margin_px, config.opacity, config.font_px),
            (320, 96, 24, 235, 15),
        )
        # v3 浅色主题：白底 + 待机灰字/强调色 + 阴影留白与圆角。
        self.assertEqual(config.background_rgb, (255, 255, 255))
        self.assertEqual(config.foreground_rgb, (110, 118, 132))
        self.assertEqual(config.accent_rgb, (110, 118, 132))
        self.assertEqual(config.border_rgb, (223, 226, 232))
        self.assertEqual((config.shadow_margin_px, config.corner_radius_px), (8, 16))
        self.assertEqual((config.title_font_px, config.body_font_px), (13, 16))
        self.assertEqual(config.state_colors, ())
        self.assertEqual(config.title, "电脑语音助手")
        self.assertTrue(config.topmost)
        self.assertTrue(config.draggable)

    def test_config_rejects_out_of_range_values(self) -> None:
        bad_kwargs = (
            {"width": 0}, {"width": -1}, {"height": 0}, {"margin_px": -1},
            {"opacity": -1}, {"opacity": 256}, {"font_px": 0},
            {"background_rgb": (0, 0)}, {"foreground_rgb": (0, 0, 999)},
            {"accent_rgb": "not-rgb"}, {"title": ""}, {"topmost": 1}, {"draggable": 0},
        )
        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    VoiceHudConfig(**kwargs)


class PositionTests(unittest.TestCase):
    def test_screen_position_is_bottom_right_with_margin(self) -> None:
        self.assertEqual(
            screen_position_for(screen=(1920, 1080), width=320, height=96, margin_px=24),
            (1576, 960),
        )
        self.assertEqual(
            screen_position_for(screen=(1280, 720), width=320, height=96, margin_px=24),
            (936, 600),
        )
        self.assertEqual(
            screen_position_for(screen=(800, 600), width=100, height=50, margin_px=0),
            (700, 550),
        )
        self.assertEqual(
            screen_position_for(screen=(1024, 768), width=200, height=100, margin_px=48),
            (776, 620),
        )

    def test_screen_position_clamps_when_screen_smaller_than_window(self) -> None:
        self.assertEqual(
            screen_position_for(screen=(100, 100), width=320, height=96, margin_px=24),
            (0, 0),
        )

    def test_clamp_position_keeps_window_inside_screen(self) -> None:
        screen, width, height = (1920, 1080), 320, 96
        self.assertEqual(
            clamp_position(x=5000, y=5000, screen=screen, width=width, height=height),
            (1600, 984),
        )
        self.assertEqual(
            clamp_position(x=-500, y=-500, screen=screen, width=width, height=height),
            (0, 0),
        )
        self.assertEqual(
            clamp_position(x=100, y=200, screen=screen, width=width, height=height),
            (100, 200),
        )
        # 屏幕小于窗口时钳制到左/上边界，绝不返回负坐标。
        self.assertEqual(
            clamp_position(x=10, y=10, screen=(100, 100), width=320, height=96),
            (0, 4),
        )
        self.assertEqual(
            clamp_position(x=500, y=500, screen=(100, 100), width=320, height=96),
            (0, 4),
        )
        # 可选的 margin_px 让拖动结果保持贴边内缩。
        self.assertEqual(
            clamp_position(x=-10, y=-10, screen=screen, width=width, height=height, margin_px=24),
            (24, 24),
        )

    def test_hud_uses_injected_screen_and_places_window_at_bottom_right(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1280, 720))
        hud_instance.show()
        self.assertEqual(len(api.created), 1)
        # 窗口 = 卡片 320×96 + 四周 8px 阴影留白 = 336×112；窗口左上再各内移 8px，
        # 使卡片可见位置仍是右下角内缩 24px。
        self.assertEqual(api.moves[0][1:], (912, 576, 336, 112))
        self.assertEqual(hud_instance.position(), (912, 576))
        self.assertEqual(hud_instance.window_size, (336, 112))
        self.assertEqual(hud_instance.screen, (1280, 720))

    def test_hud_uses_api_screen_when_not_injected(self) -> None:
        api = FakeHudApi(screen=(1024, 768))
        hud_instance = VoiceStatusHud(api=api)
        self.assertEqual(hud_instance.screen, (1024, 768))
        hud_instance.show()
        self.assertEqual(api.moves[0][1:], (656, 624, 336, 112))

    def test_hud_falls_back_to_default_screen_when_api_cannot_report(self) -> None:
        api = FakeHudApi(failing="screen_size")
        hud_instance = VoiceStatusHud(api=api)
        self.assertEqual(hud_instance.screen, DEFAULT_SCREEN_SIZE)
        self.assertEqual(
            hud_instance.position(),
            screen_position_for(screen=DEFAULT_SCREEN_SIZE, width=336, height=112,
                                margin_px=24, pad=8),
        )

    def test_hud_rejects_invalid_screen_and_config(self) -> None:
        for screen in ((0, 100), (-1, 100), (100,), 100):
            with self.subTest(screen=screen):
                with self.assertRaises(ValueError):
                    VoiceStatusHud(api=FakeHudApi(), screen=screen)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            VoiceStatusHud(config="nope", api=FakeHudApi())  # type: ignore[arg-type]


class LifecycleTests(unittest.TestCase):
    def test_initial_state_is_idle_and_hidden(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        self.assertIs(hud_instance.state, VoiceHudState.IDLE)
        self.assertEqual(hud_instance.turn_index, 0)
        self.assertFalse(hud_instance.visible)
        self.assertFalse(hud_instance.created)
        self.assertEqual(hud_instance.last_text, ("电脑语音助手", "待机"))
        self.assertEqual(api.created, [])
        self.assertEqual(api.texts, [])
        self.assertEqual(api.shown, [])

    def test_show_creates_window_once_and_is_idempotent(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.show()
        hud_instance.show()
        self.assertTrue(hud_instance.visible)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.shown, ["handle-0"])
        # 建窗时下发一次初始位置与初始文本。
        self.assertEqual(api.moves[0][1:], (1552, 936, 336, 112))
        self.assertEqual(api.texts, [("handle-0", "电脑语音助手", "待机")])

    def test_create_window_parameters_carry_topmost_and_opacity(self) -> None:
        api = FakeHudApi()
        config = VoiceHudConfig(topmost=False, opacity=120, width=200, height=64)
        hud_instance = VoiceStatusHud(config=config, api=api, screen=(800, 600))
        hud_instance.show()
        # 窗口尺寸 = 卡片尺寸 + 两侧阴影留白（默认 8px）。
        self.assertEqual(
            api.created[0],
            {
                "class_name": WINDOW_CLASS_NAME,
                "title": "电脑语音助手",
                "width": 216,
                "height": 80,
                "topmost": False,
                "opacity": 120,
                "handle": "handle-0",
                # 批次 A：松手吸附参数随建窗一起下发（默认 24 px / 120 ms）。
                "drag_snap_px": 24,
                "drag_settle_ms": 120,
            },
        )

    def test_hide_is_idempotent_and_makes_show_effective_again(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.hide()
        hud_instance.hide()
        self.assertEqual(api.hidden, [])
        hud_instance.show()
        hud_instance.hide()
        hud_instance.hide()
        self.assertEqual(api.hidden, ["handle-0"])
        self.assertFalse(hud_instance.visible)
        self.assertTrue(hud_instance.created)
        hud_instance.show()
        self.assertEqual(api.shown, ["handle-0", "handle-0"])
        self.assertEqual(len(api.created), 1)

    def test_set_state_while_hidden_updates_text_without_api_call(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        self.assertEqual(hud_instance.set_state(VoiceHudState.LISTENING, turn_index=2), "聆听")
        self.assertEqual(hud_instance.last_text, ("电脑语音助手", "聆听"))
        self.assertEqual(api.texts, [])
        hud_instance.show()
        self.assertEqual(api.texts, [("handle-0", "电脑语音助手", "聆听")])

    def test_set_state_refreshes_text_when_visible(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        for state in VoiceHudState:
            self.assertEqual(hud_instance.set_state(state), EXPECTED_LABELS[state])
        self.assertEqual(window_bodies(api)[1:], [EXPECTED_LABELS[state] for state in VoiceHudState])
        self.assertIs(hud_instance.state, VoiceHudState.STOPPED)

    def test_set_state_keeps_previous_turn_when_omitted(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.HEARING, turn_index=7)
        hud_instance.set_state(VoiceHudState.TRANSCRIBING)
        self.assertEqual(hud_instance.turn_index, 7)
        self.assertEqual(hud_instance.last_text, ("电脑语音助手", "聆听"))
        hud_instance.set_state(VoiceHudState.FAILED, turn_index=0)
        self.assertEqual(hud_instance.turn_index, 0)
        self.assertEqual(hud_instance.last_text, ("电脑语音助手", "失败"))

    def test_set_state_rejects_invalid_arguments(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        with self.assertRaises(ValueError):
            hud_instance.set_state("listening")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            hud_instance.set_state(VoiceHudState.IDLE, turn_index=-1)
        with self.assertRaises(ValueError):
            hud_instance.set_state(VoiceHudState.IDLE, turn_index="2")  # type: ignore[arg-type]
        self.assertIs(hud_instance.state, VoiceHudState.IDLE)
        self.assertEqual(hud_instance.turn_index, 0)

    def test_destroy_is_idempotent_and_releases_window(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.destroy()
        hud_instance.destroy()
        self.assertEqual(api.destroyed, ["handle-0"])
        self.assertFalse(hud_instance.visible)
        self.assertFalse(hud_instance.created)
        # 未建窗时 destroy 是纯空操作，不触碰 api。
        untouched = FakeHudApi()
        VoiceStatusHud(api=untouched, screen=(1920, 1080)).destroy()
        self.assertEqual(untouched.destroyed, [])

    def test_after_destroy_tick_is_safe_and_state_properties_stay_readable(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.SPEAKING, turn_index=5)
        hud_instance.destroy()
        self.assertEqual(hud_instance.tick(), 0)
        self.assertEqual(api.pumped, 0)
        self.assertIs(hud_instance.state, VoiceHudState.SPEAKING)
        self.assertEqual(hud_instance.turn_index, 5)
        self.assertEqual(hud_instance.last_text, ("电脑语音助手", "播报"))
        self.assertFalse(hud_instance.visible)

    def test_after_destroy_set_state_show_hide_raise_fixed_code(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.destroy()
        for call in (
            lambda: hud_instance.set_state(VoiceHudState.IDLE),
            hud_instance.show,
            hud_instance.hide,
        ):
            with self.subTest(call=call):
                with self.assertRaises(VoiceHudError) as caught:
                    call()
                self.assertEqual(str(caught.exception), NOT_CREATED)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.destroyed, ["handle-0"])


class TickAndDragTests(unittest.TestCase):
    def test_tick_is_noop_before_window_creation(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        self.assertEqual(hud_instance.tick(), 0)
        self.assertEqual(api.pumped, 0)

    def test_tick_pumps_messages_without_blocking(self) -> None:
        api = FakeHudApi(pump_results=[3, 0, 7])
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        self.assertEqual(hud_instance.tick(), 3)
        self.assertEqual(hud_instance.tick(), 0)
        self.assertEqual(hud_instance.tick(), 7)
        self.assertEqual(api.pumped, 3)
        self.assertEqual(api.offsets_read, 3)

    def test_tick_rejects_non_integer_pump_result(self) -> None:
        class BadCountApi(FakeHudApi):
            def pump_messages(self) -> int:
                return "many"  # type: ignore[return-value]

        hud_instance = VoiceStatusHud(api=BadCountApi(), screen=(1920, 1080))
        hud_instance.show()
        with self.assertRaises(VoiceHudError) as caught:
            hud_instance.tick()
        self.assertEqual(str(caught.exception), WIN32_FAILED)

    def test_drag_offset_moves_window_and_is_reported_once(self) -> None:
        api = FakeHudApi(offset=(400, 300))
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        self.assertEqual(hud_instance.tick(), 0)
        moved = api.moves[-1]
        self.assertEqual(moved[1:], (400, 300, 336, 112))
        self.assertEqual(hud_instance.position(), (400, 300))
        self.assertEqual(hud_instance.tick(), 0)
        self.assertEqual(len(api.moves), 2)  # 只有建窗与一次拖动结果
        self.assertEqual(api.moves[-1][1:], (400, 300, 336, 112))

    def test_drag_offset_is_clamped_to_screen_bounds(self) -> None:
        # 批次 A：拖动结果**不再**按贴边 margin_px 二次内缩（`_track_drag` 已按工作区钳过），
        # 否则松手瞬间会被再拉进最多 24 px。这里只保留"绝不越出屏幕"的硬约束。
        api = FakeHudApi(offset=(99_999, 99_999))
        hud_instance = VoiceStatusHud(api=api, screen=(1280, 720))
        hud_instance.show()
        hud_instance.tick()
        self.assertEqual(api.moves[-1][1:], (944, 608, 336, 112))
        self.assertEqual(hud_instance.position(), (944, 608))

        api_negative = FakeHudApi(offset=(-50, -80))
        hud_negative = VoiceStatusHud(api=api_negative, screen=(1280, 720))
        hud_negative.show()
        hud_negative.tick()
        self.assertEqual(api_negative.moves[-1][1:], (0, 0, 336, 112))

        api_zero_margin = FakeHudApi(offset=(-50, -80))
        hud_zero_margin = VoiceStatusHud(
            config=VoiceHudConfig(margin_px=0), api=api_zero_margin, screen=(1280, 720)
        )
        hud_zero_margin.show()
        hud_zero_margin.tick()
        self.assertEqual(api_zero_margin.moves[-1][1:], (0, 0, 336, 112))

    def test_drag_offset_is_ignored_when_window_hidden(self) -> None:
        api = FakeHudApi(offset=(400, 300))
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.hide()
        moves_before = len(api.moves)
        self.assertEqual(hud_instance.tick(), 0)
        self.assertEqual(len(api.moves), moves_before)

    def test_drag_offset_with_wrong_shape_fails_closed(self) -> None:
        class BadOffsetApi(FakeHudApi):
            def dragged_offset(self, handle: object) -> object:
                return (5, "6")  # type: ignore[return-value]

        hud_instance = VoiceStatusHud(api=BadOffsetApi(), screen=(1920, 1080))
        hud_instance.show()
        with self.assertRaises(VoiceHudError) as caught:
            hud_instance.tick()
        self.assertEqual(str(caught.exception), WIN32_FAILED)


class NoContentLeakTests(unittest.TestCase):
    def test_window_text_never_contains_recognized_or_reply_text(self) -> None:
        api = FakeHudApi(offset=(150, 150))
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        for index, secret in enumerate(SECRET_TEXTS, start=1):
            # 即使外部把识别/回复正文塞进状态标签入口，窗口文本也只能由状态机生成。
            hud_instance.set_state(VoiceHudState.TRANSCRIBING, turn_index=index)
            self.assertNotIn(secret, hud_instance.last_text[1])
            hud_instance.tick()
            hud_instance.set_state(VoiceHudState.STOPPED)
            del secret
        allowed = set(EXPECTED_LABELS.values())
        allowed_titles = {"电脑语音助手"}
        self.assertTrue(api.texts)
        for _handle, title, body in api.texts:
            self.assertIn(title, allowed_titles)
            self.assertIn(body.split("\n")[0], allowed)
            for secret in SECRET_TEXTS:
                self.assertNotIn(secret, body)
                self.assertNotIn(secret, title)

    def test_last_text_only_has_state_label(self) -> None:
        api = FakeHudApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1920, 1080))
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.DEGRADED, turn_index=12)
        self.assertEqual(hud_instance.last_text, ("电脑语音助手", "待机"))
        self.assertEqual(hud_instance.turn_index, 12)
        self.assertNotIn(".env", hud_instance.last_text[1])
        self.assertNotIn("\\", hud_instance.last_text[1])

    def test_error_messages_only_carry_fixed_short_codes(self) -> None:
        sample = "C:\\Users\\someone\\.env Traceback（识别正文）"
        for code in (sample, "", "unknown_code", 42):
            with self.subTest(code=code):
                error = VoiceHudError(code)  # type: ignore[arg-type]
                self.assertEqual(str(error), WIN32_FAILED)
                self.assertIn(error.code, {NOT_CREATED, UNSUPPORTED_PLATFORM, WIN32_FAILED})
        for code in (NOT_CREATED, UNSUPPORTED_PLATFORM, WIN32_FAILED):
            self.assertEqual(str(VoiceHudError(code)), code)


class FailClosedTests(unittest.TestCase):
    def _hud(self, api: FakeHudApi) -> VoiceStatusHud:
        return VoiceStatusHud(api=api, screen=(1920, 1080))

    def test_api_failures_are_wrapped_into_fixed_code(self) -> None:
        for method in ("create_window",):
            with self.subTest(method=method):
                hud_instance = self._hud(FakeHudApi(failing=method))
                with self.assertRaises(VoiceHudError) as caught:
                    hud_instance.show()
                self.assertEqual(str(caught.exception), WIN32_FAILED)
                self.assertFalse(hud_instance.created)

    def test_visible_api_failures_are_wrapped_into_fixed_code(self) -> None:
        for method, action in (
            ("show", lambda hud_instance: hud_instance.show()),
            ("hide", lambda hud_instance: (hud_instance.show(), hud_instance.hide())),
            ("set_text", lambda hud_instance: (hud_instance.show(),
                                               hud_instance.set_state(VoiceHudState.THINKING))),
            ("pump_messages", lambda hud_instance: (hud_instance.show(), hud_instance.tick())),
            ("destroy_window", lambda hud_instance: (hud_instance.show(), hud_instance.destroy())),
        ):
            with self.subTest(method=method):
                api = FakeHudApi(failing=method)
                hud_instance = self._hud(api)
                with self.assertRaises(VoiceHudError) as caught:
                    action(hud_instance)
                self.assertEqual(str(caught.exception), WIN32_FAILED)

    def test_voicehuderror_is_never_leaked_from_api_layer(self) -> None:
        class RaisingApi(FakeHudApi):
            def show(self, handle: object) -> None:
                raise VoiceHudError(UNSUPPORTED_PLATFORM)

        hud_instance = self._hud(RaisingApi())
        with self.assertRaises(VoiceHudError) as caught:
            hud_instance.show()
        self.assertEqual(str(caught.exception), UNSUPPORTED_PLATFORM)

    def test_lazy_api_construction_failure_is_fixed_code(self) -> None:
        hud_instance = VoiceStatusHud(screen=(1920, 1080))
        with patch.object(hud, "build_win32_api",
                          side_effect=VoiceHudError(UNSUPPORTED_PLATFORM)):
            with self.assertRaises(VoiceHudError) as caught:
                hud_instance.show()
        self.assertEqual(str(caught.exception), UNSUPPORTED_PLATFORM)


class BuildWin32ApiTests(unittest.TestCase):
    def test_non_windows_platform_raises_fixed_short_code(self) -> None:
        with patch.object(sys, "platform", "linux"):
            with self.assertRaises(VoiceHudError) as caught:
                build_win32_api()
        self.assertEqual(str(caught.exception), UNSUPPORTED_PLATFORM)

    def test_dll_load_failure_raises_fixed_short_code(self) -> None:
        with patch.object(sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", side_effect=OSError("C:\\Windows\\System32")):
            with self.assertRaises(VoiceHudError) as caught:
                build_win32_api()
        self.assertEqual(str(caught.exception), WIN32_FAILED)
        self.assertNotIn("System32", str(caught.exception))

    def test_signature_configuration_never_calls_missing_exports(self) -> None:
        configured: list[tuple[str, str]] = []

        class FakeFunction:
            name = "unnamed"

            def __setattr__(self, key: str, value: object) -> None:
                if key == "argtypes":
                    configured.append(("argtypes", self.name))
                object.__setattr__(self, key, value)

            @classmethod
            def named(cls, name: str) -> "FakeFunction":
                instance = cls()
                object.__setattr__(instance, "name", name)
                return instance

        class FakeLibrary:
            def __getattr__(self, name: str) -> FakeFunction:
                # 库中没有的导出（如 gdi32.DrawTextW）必须在此暴露为 AttributeError。
                if name not in FIXTURE_EXPORTS:
                    raise AttributeError(f"function {name!r} not found")
                configured.append(("access", name))
                return FakeFunction.named(name)

        for configure, exports in (
            (hud._configure_user32, USER32_EXPORTS),
            (hud._configure_user32_layered, LAYERED_USER32_EXPORTS),
            (hud._configure_gdi32, GDI32_EXPORTS),
        ):
            configured.clear()
            FIXTURE_EXPORTS.clear()
            FIXTURE_EXPORTS.update(exports)
            configure(FakeLibrary())
            accessed = {name for kind, name in configured if kind == "access"}
            self.assertEqual(accessed, set(exports))
            # 分层窗口入口采用「软配置」：替身没有真实函数对象时只读属性、不写签名，
            # 因此这里只对核心 user32/gdi32 导出断言签名确实被配置过。
            if configure is not hud._configure_user32_layered:
                for name in exports:
                    self.assertIn(("argtypes", name), configured, name)

    def test_structures_are_handwritten_ctypes_with_frozen_layout(self) -> None:
        self.assertEqual(
            [name for name, _type in hud._WndClassW._fields_],
            ["style", "lpfnWndProc", "cbClsExtra", "cbWndExtra", "hInstance", "hIcon",
             "hCursor", "hbrBackground", "lpszMenuName", "lpszClassName"],
        )
        self.assertEqual([name for name, _type in hud._Msg._fields_],
                         ["hwnd", "message", "wParam", "lParam", "time", "pt", "lPrivate"])
        self.assertEqual([name for name, _type in hud._Rect._fields_],
                         ["left", "top", "right", "bottom"])
        self.assertEqual([name for name, _type in hud._Point._fields_], ["x", "y"])
        self.assertTrue(issubclass(hud._WndClassW, ctypes.Structure))
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        # 64 位 LP64 对齐下的真实布局：hIcon 位于 32、hCursor 位于 40、hbrBackground 位于 48。
        self.assertEqual(
            [name for name, _type in hud._WndClassW._fields_].index("hbrBackground"), 7
        )
        self.assertEqual(hud._WndClassW.hIcon.offset, 32)
        self.assertEqual(hud._WndClassW.hCursor.offset, 40)
        self.assertEqual(hud._WndClassW.hbrBackground.offset, 48)
        self.assertEqual(hud._WndClassW.hbrBackground.offset % ctypes.sizeof(ctypes.c_void_p), 0)

    def test_win32_styles_cover_topmost_toolwindow_layered_and_no_caption(self) -> None:
        self.assertEqual(hud._WS_EX_TOPMOST, 0x8)
        self.assertEqual(hud._WS_EX_TOOLWINDOW, 0x80)
        self.assertEqual(hud._WS_EX_LAYERED, 0x80000)
        self.assertEqual(hud._WS_EX_NOACTIVATE, 0x08000000)
        self.assertEqual(hud._WS_POPUP, 0x80000000)
        self.assertEqual(hud._LWA_ALPHA, 0x2)
        self.assertEqual(hud._PM_REMOVE, 0x1)
        self.assertEqual(hud._SM_CXSCREEN, 0)
        self.assertEqual(hud._SM_CYSCREEN, 1)
        self.assertEqual(hud._WM_LBUTTONDOWN, 0x201)
        self.assertEqual(hud._WM_MOUSEMOVE, 0x200)

    @unittest.skipUnless(sys.platform == "win32", "仅 Windows 真机校验 DLL 与签名")
    def test_real_win32_api_is_constructible_without_creating_a_window(self) -> None:
        api = build_win32_api()
        self.assertIsInstance(api, hud.Win32VoiceHudApi)
        # 只取分辨率，不建窗、不显示、不抽消息。
        self.assertIsInstance(api.screen_size(), tuple)

    def test_non_windows_never_touches_windll(self) -> None:
        with patch.object(sys, "platform", "darwin"), \
                patch.object(ctypes, "WinDLL", side_effect=AssertionError("must not load")):
            with self.assertRaises(VoiceHudError) as caught:
                build_win32_api()
        self.assertEqual(str(caught.exception), UNSUPPORTED_PLATFORM)


class MessageLoopContractTests(unittest.TestCase):
    @staticmethod
    def source_without_docstrings() -> str:
        """返回去掉所有 docstring 的源码；文档里可以出现“禁止调用”的符号名。"""

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = list(node.body)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    @staticmethod
    def imported_modules() -> set[str]:
        """只取模块级导入的顶层模块名（不含 ``from x import y`` 的符号名）。"""

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
                else:
                    names.add("<relative>")
        return names

    def test_source_never_calls_blocking_getmessage_or_setforegroundwindow(self) -> None:
        source = self.source_without_docstrings()
        self.assertNotIn("GetMessageW", source)
        self.assertNotIn("GetMessageA", source)
        self.assertNotIn("SetForegroundWindow", source)
        self.assertIn("PeekMessageW", source)

    def test_api_never_calls_foreground_or_message_loop(self) -> None:
        source = self.source_without_docstrings()
        self.assertNotIn("SetActiveWindow", source)
        self.assertNotIn("SetFocus", source)
        self.assertIn("_SW_SHOWNOACTIVATE", source)
        self.assertIn("_SWP_NOACTIVATE", source)

    def test_source_has_no_dangerous_imports_or_calls(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported.add(module.split(".")[0])
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
        forbidden_modules = {
            "socket", "subprocess", "threading", "multiprocessing", "urllib", "http",
            "requests", "asyncio", "os", "shutil", "glob", "tempfile", "logging",
        }
        self.assertEqual(imported & forbidden_modules, set())
        self.assertEqual(imported & {"app"}, {"app"})
        self.assertEqual(self.imported_from_app(), {"app.ui.hud_render"})
        for name in ("open", "exec", "eval", "compile", "__import__", "input"):
            self.assertNotIn(name, called)
        self.assertNotIn("Popen", called)
        self.assertNotIn("run", called)

    def test_module_does_not_import_safety_chain_or_tts_asr(self) -> None:
        imported = self.imported_modules()
        self.assertEqual(imported, {"__future__", "ctypes", "dataclasses", "enum", "sys",
                                    "time", "typing", "app"})
        # 唯一允许的内部依赖是同层的纯像素渲染核心；安全链与 TTS/ASR 一律禁止。
        self.assertEqual(self.imported_from_app(), {"app.ui.hud_render"})
        for forbidden in ("app.safety", "app.executor", "app.plans", "app.auditing",
                          "app.verification", "app.tools", "app.tts", "app.asr"):
            self.assertNotIn(forbidden, imported)

    @staticmethod
    def imported_from_app() -> set[str]:
        """本模块从 ``app`` 包内导入的完整模块名（用于收窄内部依赖面）。"""

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.startswith("app"):
                    names.add(node.module)
        return names

    def test_module_only_touches_whitelisted_windows_entry_points(self) -> None:
        """执行面白名单：源码里调用的 Win32 入口只能是冻结清单内的 ``*W`` 导出。"""

        source = self.source_without_docstrings()
        whitelisted = {
            "DefWindowProcW", "FillRect", "DrawTextW", "GetClassInfoW", "UnregisterClassW",
            "CreateWindowExW", "DestroyWindow", "ShowWindow", "SetWindowPos",
            "SetWindowTextW", "SetLayeredWindowAttributes", "UpdateLayeredWindow",
            "PeekMessageW", "TranslateMessage", "DispatchMessageW", "GetSystemMetrics",
            "SystemParametersInfoW", "GetWindowRect", "GetClientRect", "GetCursorPos",
            "SetCapture", "ReleaseCapture", "SetTimer", "KillTimer", "BeginPaint", "EndPaint",
            "InvalidateRect", "UpdateWindow", "RegisterClassW", "CreateSolidBrush", "CreateFontW",
            "SelectObject", "DeleteObject", "SetBkMode", "SetTextColor", "SetBkColor",
            "SetTextAlign", "CreateDIBSection", "CreateCompatibleDC", "DeleteDC",
            "GdiFlush", "GetModuleHandleW", "CreateWindowExA", "GetMessageA",
        }
        called = {
            node.attr for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr.endswith("W") and node.attr[0].isupper()
        }
        self.assertEqual(called - whitelisted, set())
        # 模块级导出清单必须与测试白名单一致，防止后续悄悄扩大调用面。
        self.assertEqual(
            set(USER32_EXPORTS) | set(GDI32_EXPORTS) | set(LAYERED_USER32_EXPORTS),
            whitelisted - {"GetModuleHandleW", "CreateWindowExA", "GetMessageA"},
        )

    def test_module_docstring_declares_the_safety_boundary(self) -> None:
        docstring = inspect.getdoc(hud) or ""
        for phrase in ("不联网", "不读写任何文件", "PeekMessageW", "not_created"):
            self.assertIn(phrase, docstring)
        for forbidden in ("app.safety", "app.executor", "app.plans", "app.auditing",
                          "app.verification", "app.tools", "app.tts", "app.asr"):
            # 文档里允许点名这些模块来表示“不导入”。
            self.assertIn(forbidden, docstring)

    def test_source_is_valid_utf8_without_replacement_character(self) -> None:
        raw = MODULE_PATH.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        self.assertNotIn("\ufffd", text)
        self.assertIn("电脑语音助手", text)

    def test_public_surface_is_frozen(self) -> None:
        self.assertEqual(
            set(hud.__all__),
            {
                "DEFAULT_SCREEN_SIZE", "NOT_CREATED", "STATE_LABELS", "TITLE_TEXT",
                "UNSUPPORTED_PLATFORM", "VOICE_HUD_SCHEMA_VERSION", "WINDOW_CLASS_NAME",
                "WIN32_FAILED", "VoiceHudApi", "VoiceHudConfig", "VoiceHudError",
                "VoiceHudState", "VoiceStatusHud", "Win32VoiceHudApi", "build_win32_api",
                "clamp_position", "hud_text", "screen_position_for", "state_label",
            },
        )
        self.assertEqual(
            set(hud.VoiceHudState),
            {
                VoiceHudState.IDLE, VoiceHudState.LISTENING, VoiceHudState.HEARING,
                VoiceHudState.TRANSCRIBING, VoiceHudState.THINKING, VoiceHudState.SPEAKING,
                VoiceHudState.DEGRADED, VoiceHudState.FAILED, VoiceHudState.STOPPED,
            },
        )
        for name in ("show", "hide", "set_state", "tick", "destroy"):
            self.assertTrue(callable(getattr(VoiceStatusHud, name)))
        for name in ("state", "turn_index", "visible", "last_text"):
            self.assertIsInstance(getattr(VoiceStatusHud, name), property)


class PixelModeTests(unittest.TestCase):
    """v3 逐像素渲染路径：能力探测、首帧与状态变化重绘、失败一次性降级。"""

    class PixelApi(FakeHudApi):
        """支持逐像素渲染的替身：记录 ``render_frame``/``blit_cached_frame``，可注入失败。"""

        def __init__(self, *, fail_render: bool = False, supported: bool = True,
                     animations: bool = True, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            self.fail_render = fail_render
            self.supported = supported
            self.animations = animations
            self.frames: list[dict[str, object]] = []
            self.blits: list[dict[str, object]] = []
            self.cached: set[tuple[str, str]] = set()
            self.support_probes = 0

        def animations_enabled(self) -> bool:
            return self.animations

        def layered_window_supported(self) -> bool:
            self.support_probes += 1
            return self.supported

        def render_frame(self, handle: object, *, pixels: bytes, masks: bytes, state: object,
                         title: str, body: str, spec: object, alpha_scale: int = 255) -> None:
            if self.fail_render:
                raise RuntimeError("layered window exploded: C:\\secret\\path")
            self.frames.append({
                "handle": handle, "pixels": pixels, "masks": masks, "state": state,
                "title": title, "body": body, "spec": spec, "alpha_scale": alpha_scale,
            })
            # 真实实现会在合成后回读 DIB 作为动效帧缓存；替身用"已渲染过"来等价建模。
            self.cached.add((title, body))

        def blit_cached_frame(self, handle: object, *, title: str, body: str,
                              alpha_scale: int = 255) -> bool:
            if (title, body) not in self.cached:
                return False
            self.blits.append({
                "title": title, "body": body, "alpha_scale": alpha_scale, "handle": handle,
            })
            return True

    def test_pixel_mode_is_used_when_api_supports_it(self) -> None:
        api = self.PixelApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1280, 720))
        self.assertEqual(hud_instance.render_mode, MODE_LEGACY)  # 未建窗前尚未探测
        hud_instance.show()
        self.assertEqual(hud_instance.render_mode, MODE_PIXEL)
        self.assertEqual(api.support_probes, 1)
        self.assertEqual(len(api.frames), 1)
        # 逐像素成功时不再下发旧的 set_text 路径。
        self.assertEqual(api.texts, [])
        frame = api.frames[0]
        width, height = hud_instance.window_size
        self.assertEqual(len(frame["pixels"]), width * height * 4)  # type: ignore[arg-type]
        self.assertEqual(len(frame["masks"]), width * height)  # type: ignore[arg-type]
        self.assertEqual(frame["title"], "电脑语音助手")
        self.assertEqual(frame["body"], "待机")

    def test_state_change_redraws_frame_without_touching_set_text(self) -> None:
        api = self.PixelApi()
        hud_instance = VoiceStatusHud(api=api, screen=(1280, 720))
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.SPEAKING)
        hud_instance.set_state(VoiceHudState.FAILED)
        self.assertEqual([frame["body"] for frame in api.frames], ["待机", "播报", "失败"])
        self.assertEqual(api.texts, [])
        self.assertEqual(hud_instance.render_mode, MODE_PIXEL)

    def test_first_frame_failure_degrades_once_and_keeps_displaying(self) -> None:
        api = self.PixelApi(fail_render=True)
        hud_instance = VoiceStatusHud(api=api, screen=(1280, 720))
        hud_instance.show()  # 首帧失败 → 降级为纯色卡片，且不抛错
        self.assertEqual(hud_instance.render_mode, MODE_LEGACY)
        self.assertTrue(hud_instance.visible)
        self.assertEqual(api.texts, [("handle-0", "电脑语音助手", "待机")])
        self.assertEqual(api.frames, [])
        # 降级后不再重复尝试逐像素渲染。
        hud_instance.set_state(VoiceHudState.LISTENING)
        self.assertEqual(api.frames, [])
        self.assertEqual(api.texts[-1], ("handle-0", "电脑语音助手", "聆听"))
        self.assertEqual(api.support_probes, 1)

    def test_unsupported_api_never_probes_twice(self) -> None:
        api = self.PixelApi(supported=False)
        hud_instance = VoiceStatusHud(api=api, screen=(1280, 720))
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.THINKING)
        self.assertEqual(hud_instance.render_mode, MODE_LEGACY)
        self.assertEqual(api.support_probes, 1)
        self.assertEqual(api.frames, [])
        self.assertEqual(api.texts[0], ("handle-0", "电脑语音助手", "待机"))

    def test_render_spec_reflects_config(self) -> None:
        config = VoiceHudConfig(width=200, height=64, shadow_margin_px=4,
                                corner_radius_px=6, background_rgb=(250, 250, 252))
        hud_instance = VoiceStatusHud(config=config, api=FakeHudApi(), screen=(800, 600))
        spec = hud_instance.render_spec
        self.assertEqual((spec.surface_width, spec.surface_height), (200, 64))
        self.assertEqual(spec.shadow_margin_px, 4)
        self.assertEqual(spec.corner_radius_px, 6)
        self.assertEqual(spec.theme.background_rgb, (250, 250, 252))
        self.assertEqual(hud_instance.window_size, (208, 72))

    def test_state_colors_override_is_honoured(self) -> None:
        config = VoiceHudConfig(state_colors=(("idle", (1, 2, 3)),))
        hud_instance = VoiceStatusHud(config=config, api=FakeHudApi(), screen=(800, 600))
        self.assertEqual(hud_instance.render_spec.theme.color_for("idle"), (1, 2, 3))
        # 未覆盖的状态仍落到模块冻结色。
        self.assertEqual(hud_instance.render_spec.theme.color_for("failed"), (200, 48, 48))

    def test_config_rejects_invalid_v3_fields(self) -> None:
        bad_kwargs = (
            {"shadow_margin_px": -1}, {"corner_radius_px": -1}, {"border_rgb": (1, 2)},
            {"title_font_px": 0}, {"body_font_px": -3}, {"state_colors": [("idle", (1, 2, 3))]},
            {"state_colors": (("", (1, 2, 3)),)}, {"state_colors": (("idle", (1, 2)),)},
            {"state_colors": (("idle", (1, 2, 3), "x"),)},
        )
        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    VoiceHudConfig(**kwargs)

    def test_build_render_spec_rejects_non_config(self) -> None:
        with self.assertRaises(TypeError):
            hud.build_render_spec("nope")  # type: ignore[arg-type]


class _DragUser32Double:
    """只实现拖动路径会用到的 user32 导出；其余属性与真实库一样抛 ``AttributeError``。"""

    def __init__(self, *, work: tuple[int, int, int, int], cursor: tuple[int, int],
                 window: tuple[int, int], animations: bool = True) -> None:
        self.work = work
        self.cursor = cursor
        self.window = window
        self.animations = animations
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def SystemParametersInfoW(self, action, param, pointer, flags):
        self.calls.append(("SystemParametersInfoW", (action, param, flags)))
        if action == hud._SPI_GETCLIENTAREAANIMATION:
            # 批次 C：系统"动画效果"查询，出参是 BOOL 而不是 RECT。
            value = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents
            value.value = 1 if self.animations else 0
            return 1
        rectangle = ctypes.cast(pointer, ctypes.POINTER(hud._Rect)).contents
        rectangle.left, rectangle.top, rectangle.right, rectangle.bottom = self.work
        return 1

    def GetCursorPos(self, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(hud._Point)).contents
        point.x, point.y = self.cursor
        return 1

    def GetWindowRect(self, handle, pointer):
        rectangle = ctypes.cast(pointer, ctypes.POINTER(hud._Rect)).contents
        rectangle.left, rectangle.top = self.window
        return 1

    def SetWindowPos(self, handle, insert_after, x, y, width, height, flags):
        self.calls.append(("SetWindowPos", (int(x), int(y), int(width), int(height), int(flags))))
        self.window = (int(x), int(y))
        return 1

    def SetTimer(self, handle, timer_id, interval, procedure):
        self.calls.append(("SetTimer", (timer_id, interval)))
        return timer_id

    def KillTimer(self, handle, timer_id):
        self.calls.append(("KillTimer", (timer_id,)))
        return 1

    def SetCapture(self, handle):
        self.calls.append(("SetCapture", ()))
        return 1

    def ReleaseCapture(self):
        self.calls.append(("ReleaseCapture", ()))
        return 1

    def DefWindowProcW(self, handle, message, wparam, lparam):
        return 0

    def positions(self) -> list[tuple[int, int]]:
        return [entry[1][:2] for entry in self.calls if entry[0] == "SetWindowPos"]  # type: ignore[misc]

    def timer_calls(self) -> list[tuple[str, tuple[object, ...]]]:
        return [entry for entry in self.calls if entry[0] in {"SetTimer", "KillTimer"}]


class BatchADragTests(unittest.TestCase):
    """批次 A：拖动跟手 + 松手不二次跳位 + 贴边吸附缓动。

    这些用例走**真实** ``Win32VoiceHudApi._wnd_proc``，只把 user32 换成记录型替身。
    被修的缺陷（同 id ``SetTimer`` 每来一次移动就被重置 ⇒ 连续拖动根本不触发、松手才跳）
    就长在这条路径上，而批次 A 之前**没有任何用例覆盖它**。
    """

    WORK = (0, 0, 1280, 720)
    HANDLE = "hud-window"

    def make_api(self, *, cursor: tuple[int, int], window: tuple[int, int],
                 snap_px: int = 24, settle_ms: int = 0, animations: bool = True):
        user32 = _DragUser32Double(
            work=self.WORK, cursor=cursor, window=window, animations=animations
        )
        api = hud.Win32VoiceHudApi(user32=user32, gdi32=object(), kernel32=object())
        api._width = 336
        api._height = 112
        api._draggable = True
        api._drag_snap_px = snap_px
        api._drag_settle_ms = settle_ms
        api._last_position = window
        return api, user32

    def test_mousemove_follows_the_cursor_and_never_arms_a_timer(self) -> None:
        api, user32 = self.make_api(cursor=(900, 300), window=(900, 300))
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONDOWN, 0, 0)
        user32.cursor = (934, 320)

        api._wnd_proc(self.HANDLE, hud._WM_MOUSEMOVE, 0, 0)

        self.assertEqual(user32.positions(), [(934, 320)], "移动必须立刻跟随光标")
        self.assertEqual(api._last_position, (934, 320))
        self.assertEqual(user32.timer_calls(), [], "移动事件不得再靠 SetTimer 合并")

    def test_release_without_snap_does_not_move_the_window_again(self) -> None:
        api, user32 = self.make_api(cursor=(900, 300), window=(900, 300), snap_px=0)
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONDOWN, 0, 0)
        user32.cursor = (700, 500)
        api._wnd_proc(self.HANDLE, hud._WM_MOUSEMOVE, 0, 0)
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONUP, 0, 0)

        self.assertEqual(user32.positions(), [(700, 500)], "松手不得再跳一次")
        self.assertEqual(api.dragged_offset(self.HANDLE), (700, 500))
        self.assertIsNone(api.dragged_offset(self.HANDLE), "拖动结果只允许被读一次")

    def test_release_snaps_to_the_work_area_edge(self) -> None:
        # 工作区右边界 1280、窗口宽 336 ⇒ 贴边 x=944；从 934（差 10 px ≤ 24）吸附过去。
        api, user32 = self.make_api(cursor=(900, 300), window=(900, 300), settle_ms=0)
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONDOWN, 0, 0)
        user32.cursor = (934, 300)
        api._wnd_proc(self.HANDLE, hud._WM_MOUSEMOVE, 0, 0)

        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONUP, 0, 0)

        self.assertEqual(user32.positions(), [(934, 300), (944, 300)])
        self.assertEqual(api.dragged_offset(self.HANDLE), (944, 300))

    def test_release_far_from_every_edge_does_not_snap(self) -> None:
        api, user32 = self.make_api(cursor=(400, 300), window=(400, 300))
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONDOWN, 0, 0)

        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONUP, 0, 0)

        self.assertEqual(user32.positions(), [], "离边缘超过阈值时不得移动窗口")
        self.assertEqual(api.dragged_offset(self.HANDLE), (400, 300))

    def test_settle_jumps_when_system_animations_are_off(self) -> None:
        """批次 C：系统关"动画效果"时，吸附必须直接到位（不做缓动）。"""

        api, user32 = self.make_api(cursor=(900, 300), window=(900, 300), settle_ms=60,
                                    animations=False)
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONDOWN, 0, 0)
        user32.cursor = (934, 300)
        api._wnd_proc(self.HANDLE, hud._WM_MOUSEMOVE, 0, 0)

        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONUP, 0, 0)

        self.assertEqual(user32.positions(), [(934, 300), (944, 300)])

    def test_settle_animates_through_intermediate_positions(self) -> None:
        api, user32 = self.make_api(cursor=(900, 300), window=(900, 300), settle_ms=60)
        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONDOWN, 0, 0)
        user32.cursor = (934, 300)
        api._wnd_proc(self.HANDLE, hud._WM_MOUSEMOVE, 0, 0)

        api._wnd_proc(self.HANDLE, hud._WM_LBUTTONUP, 0, 0)

        xs = [position[0] for position in user32.positions()]
        self.assertEqual(xs[0], 934)
        self.assertEqual(xs[-1], 944)
        self.assertTrue(any(934 < x < 944 for x in xs), f"没有中间帧，等于硬跳：{xs}")


class SnapHelperTests(unittest.TestCase):
    """吸附与缓动的纯函数（离线、无 Win32）。"""

    AREA = (0, 0, 1280, 720)

    def test_snap_only_within_the_threshold(self) -> None:
        inside = hud._snap_to_work_area(
            x=934, y=300, area=self.AREA, width=336, height=112, snap_px=24
        )
        self.assertEqual(inside, (944, 300))
        outside = hud._snap_to_work_area(
            x=900, y=300, area=self.AREA, width=336, height=112, snap_px=24
        )
        self.assertEqual(outside, (900, 300))

    def test_snap_covers_left_and_top_edges_too(self) -> None:
        snapped = hud._snap_to_work_area(
            x=10, y=5, area=self.AREA, width=336, height=112, snap_px=24
        )
        self.assertEqual(snapped, (0, 0))

    def test_zero_snap_disables_the_behaviour(self) -> None:
        self.assertEqual(
            hud._snap_to_work_area(x=934, y=300, area=self.AREA, width=336, height=112, snap_px=0),
            (934, 300),
        )

    def test_ease_out_cubic_endpoints_and_monotonic(self) -> None:
        self.assertEqual(hud._ease_out_cubic(0.0), 0.0)
        self.assertEqual(hud._ease_out_cubic(1.0), 1.0)
        self.assertEqual(hud._ease_out_cubic(-1.0), 0.0)
        self.assertEqual(hud._ease_out_cubic(2.0), 1.0)
        values = [hud._ease_out_cubic(step / 10) for step in range(11)]
        self.assertEqual(values, sorted(values))
        # ease-out：前半段走过的比例明显超过一半（起步快、收尾慢）。
        self.assertGreater(values[5], 0.5)


class BatchBAnimationTests(unittest.TestCase):
    """批次 B：状态切换软过渡（旧帧淡到最低点 → 新帧淡入）与首次显示淡入。"""

    def make(self, *, animation_ms: int = 60, show_fade_ms: int = 0, hide_fade_ms: int = 0,
             show_slide_px: int = 12, animations: bool = True,
             respect_system: bool = True):
        api = PixelModeTests.PixelApi(animations=animations)
        hud_instance = VoiceStatusHud(
            config=VoiceHudConfig(
                state_animation_ms=animation_ms,
                show_fade_ms=show_fade_ms,
                hide_fade_ms=hide_fade_ms,
                show_slide_px=show_slide_px,
                respect_system_animation=respect_system,
            ),
            api=api,
            screen=(1280, 720),
        )
        return api, hud_instance

    @staticmethod
    def resting_position() -> tuple[int, int]:
        return screen_position_for(
            screen=(1280, 720), width=336, height=112, margin_px=24, pad=8
        )

    def test_state_change_dips_then_ramps_back_without_a_flash(self) -> None:
        api, hud_instance = self.make(animation_ms=60)
        hud_instance.show()
        api.frames.clear()
        api.blits.clear()

        started = time.monotonic()
        hud_instance.set_state(VoiceHudState.SPEAKING)
        elapsed = time.monotonic() - started

        # 新帧**只以最低点 alpha** 渲染一次：不会先闪一下全不透明的新帧。
        self.assertEqual(
            [frame["alpha_scale"] for frame in api.frames], [hud._ANIMATION_DIP_ALPHA]
        )
        self.assertEqual(api.frames[0]["body"], "播报")
        # 旧帧先以全不透明提交，再淡到最低点；新帧随后从最低点升回 255。
        self.assertEqual(api.blits[0]["body"], "待机")
        self.assertEqual(api.blits[0]["alpha_scale"], 255)
        self.assertIn(hud._ANIMATION_DIP_ALPHA, [b["alpha_scale"] for b in api.blits])
        self.assertEqual(api.blits[-1]["body"], "播报")
        self.assertEqual(api.blits[-1]["alpha_scale"], 255)
        self.assertGreaterEqual(elapsed, 0.05, "动效必须真的跑了一段时间")
        self.assertLess(elapsed, 2.0, "动效不得远超配置时长")

    def test_disabled_animation_renders_once_at_full_alpha(self) -> None:
        api, hud_instance = self.make(animation_ms=0, show_fade_ms=0)
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.SPEAKING)

        self.assertEqual([frame["alpha_scale"] for frame in api.frames], [255, 255])
        self.assertEqual(api.blits, [], "关闭动效时不得提交任何缓存帧")

    def test_default_config_hard_cuts_every_state_change(self) -> None:
        """真机反馈"状态变化时不要闪烁" ⇒ 默认配置下状态切换必须零亮度变化。"""

        self.assertEqual(VoiceHudConfig().state_animation_ms, 0)
        api = PixelModeTests.PixelApi()
        hud_instance = VoiceStatusHud(
            config=VoiceHudConfig(show_fade_ms=0), api=api, screen=(1280, 720)
        )
        hud_instance.show()
        api.frames.clear()

        hud_instance.set_state(VoiceHudState.LISTENING)
        hud_instance.set_state(VoiceHudState.SPEAKING)
        hud_instance.set_state(VoiceHudState.IDLE)

        self.assertEqual([frame["alpha_scale"] for frame in api.frames], [255, 255, 255])
        self.assertEqual(api.blits, [], "默认必须硬切，不得有任何 alpha 过渡帧")

    def test_consecutive_switches_skip_the_animation(self) -> None:
        """防闪护栏：距上次切换不足 200 ms 时，第二段过渡必须直接硬切。"""

        api, hud_instance = self.make(animation_ms=60, show_fade_ms=0)
        hud_instance.show()
        api.frames.clear()
        api.blits.clear()

        hud_instance.set_state(VoiceHudState.SPEAKING)  # 第一段：允许动效
        blits_after_first = len(api.blits)
        self.assertGreater(blits_after_first, 0, "第一段过渡应当有动效帧")

        hud_instance.set_state(VoiceHudState.LISTENING)  # 紧随其后：必须硬切
        self.assertEqual(
            len(api.blits), blits_after_first, "几十毫秒内的第二段过渡不得再跑动效"
        )
        self.assertEqual(
            [frame["alpha_scale"] for frame in api.frames], [hud._ANIMATION_DIP_ALPHA, 255]
        )

    def test_first_show_fades_in_from_transparent(self) -> None:
        api, hud_instance = self.make(animation_ms=0, show_fade_ms=50)
        hud_instance.show()

        self.assertEqual(len(api.frames), 1)
        self.assertEqual(api.blits[0]["alpha_scale"], 0, "必须先提交全透明帧")
        self.assertEqual(api.blits[-1]["body"], "待机")
        self.assertEqual(api.blits[-1]["alpha_scale"], 255)

    def test_later_shows_do_not_fade_again(self) -> None:
        api, hud_instance = self.make(animation_ms=0, show_fade_ms=50)
        hud_instance.show()
        hud_instance.hide()  # 退场本身会提交缓存帧，所以清空要放在 hide 之后
        api.blits.clear()

        hud_instance.show()

        self.assertEqual(api.blits, [], "淡入只允许发生在首次显示")

    def test_api_without_blit_falls_back_to_a_hard_cut(self) -> None:
        class OldPixelApi(FakeHudApi):
            """只实现旧签名的替身（没有 ``blit_cached_frame``）。"""

            def __init__(self) -> None:
                super().__init__()
                self.frames: list[int] = []

            def layered_window_supported(self) -> bool:
                return True

            def render_frame(self, handle: object, *, pixels: bytes, masks: bytes,
                             state: object, title: str, body: str, spec: object,
                             alpha_scale: int = 255, **extra: object) -> None:
                del pixels, masks, state, title, body, spec, extra
                self.frames.append(alpha_scale)

        api = OldPixelApi()
        hud_instance = VoiceStatusHud(
            config=VoiceHudConfig(state_animation_ms=60, show_fade_ms=60),
            api=api,
            screen=(1280, 720),
        )
        hud_instance.show()
        hud_instance.set_state(VoiceHudState.SPEAKING)

        self.assertEqual(api.frames, [255, 255], "没有动效入口时必须退回硬切")
        self.assertEqual(hud_instance.render_mode, MODE_PIXEL, "不得因此降级为 legacy")


class AlphaScaleTableTests(unittest.TestCase):
    """整窗 alpha 缩放表的纯函数（离线）。"""

    def test_full_scale_is_the_identity_table(self) -> None:
        self.assertEqual(hud._alpha_scale_table(255), bytes(range(256)))

    def test_zero_scale_blanks_every_channel(self) -> None:
        self.assertEqual(set(hud._alpha_scale_table(0)), {0})

    def test_scale_applies_to_every_channel_including_alpha(self) -> None:
        table = hud._alpha_scale_table(64)
        self.assertEqual(table[255], 64)
        self.assertEqual(table[128], 32)
        self.assertEqual(table[0], 0)
        self.assertEqual(hud._alpha_scale_table(128)[255], 128)


class BatchCEnterExitTests(unittest.TestCase):
    """批次 C：进场滑入、退场淡出+滑出、尊重系统"动画效果"开关。"""

    RESTING = screen_position_for(
        screen=(1280, 720), width=336, height=112, margin_px=24, pad=8
    )

    def make(self, **kwargs: object):
        api = PixelModeTests.PixelApi(animations=bool(kwargs.pop("animations", True)))
        hud_instance = VoiceStatusHud(
            config=VoiceHudConfig(**kwargs),  # type: ignore[arg-type]
            api=api,
            screen=(1280, 720),
        )
        return api, hud_instance

    def test_enter_slides_in_from_the_right(self) -> None:
        api, hud_instance = self.make(show_fade_ms=40, show_slide_px=12,
                                      state_animation_ms=0, hide_fade_ms=0)

        hud_instance.show()

        xs = [move[1] for move in api.moves]
        self.assertEqual(xs[0], self.RESTING[0], "建窗先落在休息位")
        self.assertEqual(xs[1], self.RESTING[0] + 12, "进场必须从右侧 12 px 滑入")
        self.assertEqual(xs[-1], self.RESTING[0], "进场必须回到休息位")
        self.assertEqual(api.blits[0]["alpha_scale"], 0)
        self.assertEqual(api.blits[-1]["alpha_scale"], 255)

    def test_hide_fades_out_slides_and_restores_the_resting_position(self) -> None:
        api, hud_instance = self.make(show_fade_ms=0, hide_fade_ms=60, show_slide_px=12,
                                      state_animation_ms=0)
        hud_instance.show()
        api.blits.clear()
        api.moves.clear()

        hud_instance.hide()

        self.assertFalse(hud_instance.visible)
        self.assertEqual(api.hidden, ["handle-0"], "必须真的隐藏窗口")
        alphas = [blit["alpha_scale"] for blit in api.blits]
        self.assertTrue(alphas, "退场必须有过渡帧")
        self.assertLess(alphas[-1], alphas[0])
        self.assertEqual(alphas[-1], 0)
        self.assertEqual(api.moves[-1][1], self.RESTING[0], "退场后必须归位")

    def test_hide_is_immediate_when_the_exit_animation_is_disabled(self) -> None:
        api, hud_instance = self.make(show_fade_ms=0, hide_fade_ms=0, state_animation_ms=0)
        hud_instance.show()
        api.blits.clear()

        hud_instance.hide()

        self.assertEqual(api.blits, [])
        self.assertEqual(api.hidden, ["handle-0"])

    def test_system_animation_off_disables_every_transition(self) -> None:
        api, hud_instance = self.make(
            show_fade_ms=60, hide_fade_ms=60, state_animation_ms=60, animations=False
        )

        hud_instance.show()
        hud_instance.set_state(VoiceHudState.SPEAKING)
        hud_instance.hide()

        self.assertEqual(api.blits, [], "系统关动画时不得有任何过渡帧")
        self.assertFalse(hud_instance.visible)
        self.assertEqual(api.hidden, ["handle-0"])

    def test_respect_system_animation_false_overrides_the_setting(self) -> None:
        api, hud_instance = self.make(
            show_fade_ms=40, state_animation_ms=0, hide_fade_ms=0,
            animations=False, respect_system_animation=False,
        )

        hud_instance.show()

        self.assertTrue(api.blits, "显式覆盖后仍应做进场动效")


class SystemAnimationProbeTests(unittest.TestCase):
    """``_system_animations_enabled`` 的纯函数行为（fail-open）。"""

    def test_missing_probe_defaults_to_enabled(self) -> None:
        self.assertTrue(hud._system_animations_enabled(object()))

    def test_value_comes_from_the_client_area_animation_query(self) -> None:
        seen: list[int] = []

        class User32:
            def SystemParametersInfoW(self, action, param, pointer, flags):
                del param, flags
                seen.append(action)
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 0
                return 1

        self.assertFalse(hud._system_animations_enabled(User32()))
        self.assertEqual(seen, [hud._SPI_GETCLIENTAREAANIMATION])

    def test_probe_failure_defaults_to_enabled(self) -> None:
        class Broken:
            def SystemParametersInfoW(self, *args: object) -> int:
                raise OSError("no sysparam for you")

        self.assertTrue(hud._system_animations_enabled(Broken()))

    def test_call_returning_zero_defaults_to_enabled(self) -> None:
        class Refusing:
            def SystemParametersInfoW(self, *args: object) -> int:
                return 0

        self.assertTrue(hud._system_animations_enabled(Refusing()))


USER32_EXPORTS = (
    "DefWindowProcW", "FillRect", "DrawTextW", "GetClassInfoW", "UnregisterClassW",
    "CreateWindowExW", "DestroyWindow", "ShowWindow", "SetWindowPos", "SetWindowTextW",
    "SetLayeredWindowAttributes", "PeekMessageW", "TranslateMessage",
    "DispatchMessageW", "GetSystemMetrics", "SystemParametersInfoW", "GetWindowRect",
    "GetClientRect", "GetCursorPos", "SetCapture", "ReleaseCapture", "SetTimer", "KillTimer",
    "BeginPaint", "EndPaint", "InvalidateRect", "UpdateWindow", "RegisterClassW",
)
GDI32_EXPORTS = (
    "CreateSolidBrush", "CreateFontW", "SelectObject", "DeleteObject", "SetBkMode",
    "SetTextColor", "SetBkColor", "SetTextAlign", "CreateDIBSection",
    "CreateCompatibleDC", "DeleteDC", "GdiFlush",
)
LAYERED_USER32_EXPORTS = ("UpdateLayeredWindow",)
FIXTURE_EXPORTS: set[str] = set()


if __name__ == "__main__":
    unittest.main()
