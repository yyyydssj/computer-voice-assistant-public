"""P9 UI 改版：纯像素渲染核心（``app/ui/hud_render.py``）的离线测试。

全部测试**不打开任何真实窗口**、不联网、不读写文件，也不导入 ``ctypes``；只断言
「外观参数 + 状态 → 预乘 BGRA 像素」这一层的可观测事实：

- 几何：卡片/窗口尺寸与偏移；
- 透明度不变量：四角与窗外 alpha=0、卡片内部 alpha=255；
- 圆角抗锯齿：圆角过渡带存在 0 < alpha < 255 的像素；
- 外阴影：黑、向下偏移、离卡片越远越淡；
- 状态色条：四态颜色与胶囊底（状态色 12% 与白底混合）；
- 契约：模块只导入 stdlib、``__all__`` 完整、渲染不抛错且长度恒定。
"""

from __future__ import annotations

import ast
import time
import unittest
from pathlib import Path

from app.ui import hud_render as render
from app.ui.hud_render import (
    DEFAULT_STATE_COLORS,
    HudCardRenderer,
    HudError,
    HudRenderSpec,
    HudTheme,
    body_text,
    card_origin_in_window,
    card_pixel_size,
    render_card_pixels,
    state_color,
    window_pixel_size,
)

MODULE_PATH = Path(render.__file__)

STATES = ("idle", "listening", "speaking", "failed")

# 与效果图 mock-light-4-card.png 冻结一致的期望值。
EXPECTED_STATE_COLORS = {
    "idle": (110, 118, 132),
    "listening": (26, 110, 220),
    "speaking": (16, 140, 92),
    "failed": (200, 48, 48),
}
EXPECTED_BODY_TEXTS = {"idle": "待机", "listening": "聆听", "speaking": "播报", "failed": "失败"}


class PixelBuffer:
    """把渲染出的 BGRA 缓冲包一层便于断言的读取器（测试专用，不参与生产代码）。"""

    def __init__(self, spec: HudRenderSpec, state: str = "listening") -> None:
        self.spec = spec
        self.width = spec.window_width
        self.height = spec.window_height
        self.buffer = render_card_pixels(spec, state)
        self.stride = self.width * 4

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        index = y * self.stride + x * 4
        return (
            self.buffer[index + 2],
            self.buffer[index + 1],
            self.buffer[index],
            self.buffer[index + 3],
        )

    def alpha(self, x: int, y: int) -> int:
        return self.pixel(x, y)[3]


class GeometryTests(unittest.TestCase):
    def test_default_sizes_match_frozen_card(self) -> None:
        spec = HudRenderSpec()
        self.assertEqual(card_pixel_size(spec), (320, 96))
        self.assertEqual(window_pixel_size(spec), (336, 112))
        self.assertEqual(card_origin_in_window(spec), (8, 8))

    def test_custom_surface_expands_window_by_shadow_margin(self) -> None:
        spec = HudRenderSpec(surface_width=200, surface_height=64, shadow_margin_px=12)
        self.assertEqual(card_pixel_size(spec), (200, 64))
        self.assertEqual(window_pixel_size(spec), (224, 88))
        self.assertEqual(card_origin_in_window(spec), (12, 12))
        self.assertEqual(spec.window_width, 224)

    def test_buffers_are_window_sized_bgra(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        self.assertEqual(len(pixels.buffer), 336 * 112 * 4)
        for state in STATES:
            with self.subTest(state=state):
                self.assertEqual(len(render_card_pixels(spec, state)), 336 * 112 * 4)

    def test_rects_follow_card_origin(self) -> None:
        spec = HudRenderSpec()
        self.assertEqual(spec.card_rect(), (8, 8, 328, 104))
        self.assertEqual(spec.bar_rect(), (8, 8, 12, 104))
        left, top, right, bottom = spec.title_chip_rect()
        self.assertGreater(right, left)
        self.assertGreater(bottom, top)
        self.assertLessEqual(right, spec.surface_width - spec.content_right_px)
        body = spec.body_rect()
        self.assertEqual(body[0], spec.content_left_px)
        self.assertLess(body[2], spec.surface_width)


class TransparencyTests(unittest.TestCase):
    def test_far_corners_are_fully_transparent(self) -> None:
        """远离阴影的窗口四角必须完全透明（阴影带内允许有很低 alpha，见 ShadowTests）。"""

        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        width, height = spec.window_width, spec.window_height
        for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
            with self.subTest(point=(x, y)):
                self.assertEqual(pixels.alpha(x, y), 0)

    def test_card_corners_themselves_are_outside_the_rounded_card(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        left, top = spec.card_origin
        # 卡片四角像素本身在圆角之外；紧邻的窗口角（阴影外沿）也必须透明。
        self.assertEqual(pixels.alpha(left, top), 0)
        self.assertEqual(pixels.alpha(0, 0), 0)

    def test_shadow_tail_fades_almost_to_zero_at_window_edge(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        width, height = spec.window_width, spec.window_height
        self.assertLessEqual(pixels.alpha(width - 1, 60), 30)
        self.assertLessEqual(pixels.alpha(160, height - 1), 30)
        self.assertLessEqual(pixels.alpha(160, 1), 0)
        self.assertLessEqual(pixels.alpha(width - 1, 1), 0)

    def test_card_interior_is_opaque_white(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        for x, y in ((160, 40), (300, 90), (200, 20), (160, 98)):
            with self.subTest(point=(x, y)):
                self.assertEqual(pixels.pixel(x, y), (255, 255, 255, 255))

    def test_premultiplied_channels_never_exceed_alpha(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        for y in range(0, pixels.height, 7):
            for x in range(0, pixels.width, 5):
                red, green, blue, alpha = pixels.pixel(x, y)
                with self.subTest(point=(x, y)):
                    self.assertLessEqual(max(red, green, blue), alpha)

    def test_rounded_corner_has_antialiased_alpha(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        corner_alphas = [pixels.alpha(x, y) for y in range(8, 26) for x in range(8, 26)]
        fractional = [value for value in corner_alphas if 0 < value < 255]
        self.assertTrue(fractional, "圆角必须存在抗锯齿过渡像素")
        self.assertGreaterEqual(len(fractional), 8)
        # 卡片外角（贴窗口角）仍必须完全透明。
        self.assertEqual(pixels.alpha(8, 8), 0)

    def test_corner_radius_changes_corner_shape(self) -> None:
        square = HudRenderSpec(corner_radius_px=0, shadow_margin_px=0)
        round_ = HudRenderSpec(corner_radius_px=20, shadow_margin_px=0)
        square_pixels = PixelBuffer(square)
        round_pixels = PixelBuffer(round_)
        # 直角：窗口四角就是卡片角（左侧 4px 除外是状态色条），且卡片边缘像素完全不透明。
        self.assertEqual(square_pixels.pixel(10, 0), (255, 255, 255, 255))
        self.assertEqual(square_pixels.pixel(319, 95), (255, 255, 255, 255))
        # 圆角 20：同一批角像素都在圆角之外，窗口角完全透明。
        self.assertEqual(round_pixels.alpha(10, 0), 0)
        self.assertEqual(round_pixels.alpha(319, 95), 0)
        self.assertEqual(round_pixels.pixel(160, 48), (255, 255, 255, 255))
        # 卡片边缘（非角）在两套配置下都必须有抗锯齿过渡像素。
        for pixels in (square_pixels, round_pixels):
            edge = [pixels.alpha(10, y) for y in range(0, 6)]
            self.assertIn(255, edge)


class ShadowTests(unittest.TestCase):
    def test_shadow_exists_below_and_right_of_card(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        card_bottom = spec.card_rect()[3]
        card_right = spec.card_rect()[2]
        # 柔和阴影：紧邻卡片外侧的一带 alpha 落在 40..70，向外迅速衰减。
        self.assertGreater(pixels.alpha(160, card_bottom), 40)
        self.assertGreater(pixels.alpha(card_right, 60), 30)

    def test_shadow_is_black_and_fades_away_from_card(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        width, height = spec.window_width, spec.window_height
        below = [pixels.alpha(160, y) for y in range(height - 9, height)]
        right = [pixels.alpha(x, 60) for x in range(width - 9, width)]
        self.assertEqual(below, sorted(below, reverse=True))
        self.assertEqual(right, sorted(right, reverse=True))
        red, green, blue, alpha = pixels.pixel(160, height - 1)
        self.assertEqual((red, green, blue), (0, 0, 0))
        self.assertGreater(alpha, 0)

    def test_shadow_peak_is_soft_not_solid(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        card_bottom = spec.card_rect()[3]
        # 采样卡片正下方紧邻的一带（阴影核心），峰值必须落在柔和区间。
        peak = max(pixels.alpha(160, y) for y in range(card_bottom, card_bottom + 4))
        self.assertGreaterEqual(peak, 40)
        self.assertLessEqual(peak, 70)

    def test_shadow_is_offset_downwards(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        # 对比「卡片左下角外」与「卡片自身下沿」：同一列上整体下移的阴影使后者更深。
        self.assertLess(pixels.alpha(10, 104), pixels.alpha(160, 104))

    def test_no_shadow_when_margin_is_zero(self) -> None:
        spec = HudRenderSpec(shadow_margin_px=0, corner_radius_px=0)
        pixels = PixelBuffer(spec)
        self.assertEqual(len(pixels.buffer), 320 * 96 * 4)
        # 无阴影留白 + 直角：卡片铺满整窗；左侧 4px 是状态色条，其余角必须不透明。
        self.assertEqual(pixels.pixel(10, 0), (255, 255, 255, 255))
        self.assertEqual(pixels.pixel(10, 95), (255, 255, 255, 255))
        self.assertEqual(pixels.pixel(319, 0), (255, 255, 255, 255))
        self.assertEqual(pixels.pixel(319, 95), (255, 255, 255, 255))
        self.assertEqual(pixels.pixel(0, 48), EXPECTED_STATE_COLORS["listening"] + (255,))

    def test_margin_zero_with_round_corners_keeps_corners_transparent(self) -> None:
        spec = HudRenderSpec(shadow_margin_px=0)
        pixels = PixelBuffer(spec)
        self.assertEqual(pixels.alpha(0, 0), 0)
        self.assertEqual(pixels.alpha(319, 95), 0)
        self.assertEqual(pixels.pixel(160, 48), (255, 255, 255, 255))


class AccentBarTests(unittest.TestCase):
    def test_bar_uses_state_color_for_every_state(self) -> None:
        spec = HudRenderSpec()
        for state, expected in EXPECTED_STATE_COLORS.items():
            with self.subTest(state=state):
                pixels = PixelBuffer(spec, state)
                self.assertEqual(pixels.pixel(9, 48)[:3], expected)
                self.assertEqual(pixels.alpha(9, 48), 255)

    def test_bar_is_four_pixels_wide_and_inside_card(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        for x in range(8, 12):
            self.assertEqual(pixels.pixel(x, 48)[:3], EXPECTED_STATE_COLORS["listening"])
        self.assertEqual(pixels.pixel(12, 48), (255, 255, 255, 255))

    def test_bar_fades_in_rounded_corner(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        alphas = [pixels.alpha(9, y) for y in range(8, 26)]
        self.assertIn(255, alphas)
        self.assertIn(0, alphas)
        self.assertTrue(any(0 < value < 255 for value in alphas), "色条端头必须有抗锯齿过渡")

    def test_bar_opacity_scales_without_changing_hue_order(self) -> None:
        spec = HudRenderSpec(bar_opacity=128)
        pixels = PixelBuffer(spec)
        red, green, blue, alpha = pixels.pixel(9, 48)
        expected = render._mix_rgb_tuple((255, 255, 255),
                                         EXPECTED_STATE_COLORS["listening"], 128)
        self.assertEqual((red, green, blue), expected)
        self.assertEqual(alpha, 255)


class ChipTests(unittest.TestCase):
    def test_chip_background_is_twelve_percent_of_state_color(self) -> None:
        spec = HudRenderSpec()
        for state, expected in EXPECTED_STATE_COLORS.items():
            with self.subTest(state=state):
                pixels = PixelBuffer(spec, state)
                chip = pixels.pixel(30, 24)
                self.assertEqual(chip, render._mix_rgb_tuple((255, 255, 255), expected, 31) + (255,))

    def test_chip_is_confined_to_its_rect(self) -> None:
        spec = HudRenderSpec()
        pixels = PixelBuffer(spec)
        left, top, right, bottom = spec.title_chip_rect()
        expected = render._mix_rgb_tuple((255, 255, 255), EXPECTED_STATE_COLORS["listening"], 31)
        self.assertEqual(pixels.pixel(60, 20)[:3], expected)
        self.assertEqual(pixels.pixel(right + 6, top + 8), (255, 255, 255, 255))
        self.assertEqual(pixels.pixel(60, bottom + 6), (255, 255, 255, 255))
        self.assertEqual(pixels.pixel(left - 2, top + 8), (255, 255, 255, 255))
        # 胶囊右端圆角是自由圆角（左端与状态色条相接）：圆角过渡带必须有半透明覆盖度，
        # 渲染后表现为白色卡片与胶囊底之间的颜色过渡。
        mask = render._rounded_coverage(spec.title_chip_rect(), spec.chip_radius_px, 4, spec)
        corner = [mask[y * spec.window_width + x]
                  for y in range(top, top + 14) for x in range(right - 14, right)]
        self.assertTrue(any(0 < value < 255 for value in corner), "胶囊圆角掩码缺少抗锯齿过渡")
        edge_reds = {pixels.pixel(x, top + 1)[0] for x in range(right - 6, right - 1)}
        self.assertTrue(any(0 < distance <= 40 for distance in
                            (255 - value for value in edge_reds)), edge_reds)

    def test_chip_darkens_when_state_color_darkens(self) -> None:
        spec = HudRenderSpec()
        idle = PixelBuffer(spec, "idle").pixel(30, 24)
        failed = PixelBuffer(spec, "failed").pixel(30, 24)
        self.assertNotEqual(idle, failed)

    def test_title_and_body_colors_derive_from_state(self) -> None:
        theme = HudTheme()
        self.assertEqual(theme.title_color_for("listening"),
                         tuple(channel * 85 // 100 for channel in (26, 110, 220)))
        self.assertEqual(theme.color_for("nope"), EXPECTED_STATE_COLORS["idle"])
        self.assertEqual(theme.color_for(None), EXPECTED_STATE_COLORS["idle"])


class StateMappingTests(unittest.TestCase):
    def test_frozen_state_colors_and_texts(self) -> None:
        self.assertEqual(dict(DEFAULT_STATE_COLORS), EXPECTED_STATE_COLORS)
        for state, expected in EXPECTED_STATE_COLORS.items():
            with self.subTest(state=state):
                self.assertEqual(state_color(state), expected)
                self.assertEqual(body_text(state), EXPECTED_BODY_TEXTS[state])

    def test_unknown_state_never_echoes_input(self) -> None:
        for value in ("未定义状态", None, 42, object()):
            with self.subTest(value=value):
                self.assertEqual(state_color(value), EXPECTED_STATE_COLORS["idle"])
                self.assertEqual(body_text(value), EXPECTED_BODY_TEXTS["idle"])

    def test_custom_state_colors_are_honoured(self) -> None:
        theme = HudTheme(state_colors={"idle": (1, 2, 3), "listening": (4, 5, 6)})
        spec = HudRenderSpec(theme=theme)
        self.assertEqual(state_color("listening", theme=theme), (4, 5, 6))
        self.assertEqual(PixelBuffer(spec, "listening").pixel(9, 48)[:3], (4, 5, 6))
        self.assertEqual(state_color("speaking", theme=theme), (1, 2, 3))

    def test_opacity_is_folded_into_pixel_alpha(self) -> None:
        theme = HudTheme(opacity=128)
        spec = HudRenderSpec(theme=theme)
        pixels = PixelBuffer(spec)
        self.assertEqual(pixels.alpha(160, 40), 128)
        red, green, blue, alpha = pixels.pixel(160, 40)
        self.assertEqual((red, green, blue), (128, 128, 128))
        self.assertEqual(alpha, 128)


class LayerMaskTests(unittest.TestCase):
    """GDI 文字叠加前的图层分级（窗外/卡片/色条/胶囊）。"""

    def test_mask_is_one_byte_per_pixel_with_five_layers(self) -> None:
        spec = HudRenderSpec()
        masks = render.paint_layer_masks(spec, "listening")
        self.assertEqual(len(masks), spec.window_width * spec.window_height)
        self.assertEqual(set(masks),
                         {render.LAYER_OUTSIDE, render.LAYER_CARD, render.LAYER_BAR,
                          render.LAYER_CHIP, render.LAYER_SHADOW})

    def test_mask_separates_transparent_outside_from_soft_shadow(self) -> None:
        """窗口最外圈必须「完全透明」，紧贴卡片的一侧是「阴影」，两者不能混。"""

        spec = HudRenderSpec()
        masks = render.paint_layer_masks(spec, "listening")
        width = spec.window_width
        self.assertEqual(masks[0], render.LAYER_OUTSIDE)
        shadow_row = spec.card_rect()[3] * width + 160
        self.assertEqual(masks[shadow_row], render.LAYER_SHADOW)
        # 该阴影像素在渲染缓冲里必须真的是半透明（否则降级成纯黑描边）。
        pixels = render_card_pixels(spec, "listening")
        alpha = pixels[shadow_row * 4 + 3]
        self.assertGreater(alpha, 0)
        self.assertLess(alpha, 255)

    def test_mask_classifies_representative_points(self) -> None:
        spec = HudRenderSpec()
        masks = render.paint_layer_masks(spec, "listening")
        width = spec.window_width
        self.assertEqual(masks[0], render.LAYER_OUTSIDE)                      # 窗口左上角
        self.assertEqual(masks[40 * width + 160], render.LAYER_CARD)             # 卡片中部
        self.assertEqual(masks[48 * width + 9], render.LAYER_BAR)                # 左侧色条
        self.assertEqual(masks[24 * width + 30], render.LAYER_CHIP)              # 标题胶囊

    def test_mask_marks_rounded_corner_pixels_as_background(self) -> None:
        spec = HudRenderSpec(shadow_margin_px=0)
        masks = render.paint_layer_masks(spec, "idle")
        self.assertEqual(masks[0], render.LAYER_OUTSIDE)
        self.assertEqual(masks[95 * spec.window_width], render.LAYER_OUTSIDE)

    def test_mask_follows_state_colors_without_changing_layers(self) -> None:
        spec = HudRenderSpec()
        for state in STATES:
            with self.subTest(state=state):
                self.assertEqual(render.paint_layer_masks(spec, state),
                                 render.paint_layer_masks(spec, "listening"))

    def test_mask_rejects_non_spec(self) -> None:
        with self.assertRaises(TypeError):
            render.paint_layer_masks("nope", "listening")  # type: ignore[arg-type]

    def test_text_colors_derive_from_state(self) -> None:
        theme = HudTheme()
        self.assertEqual(theme.title_text_color_for("listening"),
                         render._mix_rgb_tuple((255, 255, 255),
                                               theme.title_color_for("listening"), 217))
        self.assertEqual(theme.title_text_color_for("nope"),
                         theme.title_text_color_for("idle"))
        # 标题字色必须比胶囊底深（可读性），正文用状态色本身。
        for state in STATES:
            with self.subTest(state=state):
                title = theme.title_text_color_for(state)
                chip = render._mix_rgb_tuple((255, 255, 255), theme.color_for(state), 31)
                self.assertLess(sum(title), sum(chip))


class ContractTests(unittest.TestCase):
    def test_module_only_imports_standard_library(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "dataclasses", "enum", "math"})

    def test_module_has_no_io_network_or_thread_usage(self) -> None:
        """去掉文档字符串后，源码中不得出现 IO/网络/线程/Win32 相关的任何名字。"""

        import tokenize

        with MODULE_PATH.open("rb") as handle:
            tokens = list(tokenize.tokenize(handle.readline))
        code_tokens = [
            token.string for token in tokens
            if token.type not in (tokenize.STRING, tokenize.COMMENT, tokenize.NL,
                                  tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                                  tokenize.ENCODING, tokenize.ENDMARKER)
        ]
        code = " ".join(code_tokens)
        for forbidden in ("ctypes", "socket", "urllib", "subprocess", "threading",
                          "open", "Path", "sleep"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, code)

    def test_public_api_is_frozen(self) -> None:
        expected = {
            "DEFAULT_STATE_COLORS", "HudCardRenderer", "HudError", "HudRenderSpec",
            "HudTheme", "LAYER_OUTSIDE", "LAYER_BAR", "LAYER_CARD", "LAYER_CHIP",
            "LAYER_SHADOW",
            "body_text", "card_origin_in_window", "card_pixel_size", "paint_layer_masks",
            "render_card_pixels", "state_color", "window_pixel_size",
        }
        self.assertEqual(set(render.__all__), expected)
        for name in expected:
            self.assertTrue(hasattr(render, name))

    def test_render_is_deterministic_and_pure(self) -> None:
        spec = HudRenderSpec()
        first = render_card_pixels(spec, "listening")
        second = render_card_pixels(spec, "listening")
        self.assertEqual(first, second)
        other = render_card_pixels(HudRenderSpec(theme=HudTheme(opacity=200)), "listening")
        self.assertNotEqual(first, other)

    def test_custom_renderer_matches_module_function(self) -> None:
        spec = HudRenderSpec()
        renderer = HudCardRenderer(spec)
        self.assertEqual(renderer.window_size, (336, 112))
        self.assertEqual(renderer.render_card_pixels("listening"), render_card_pixels(spec, "listening"))
        self.assertIs(renderer.spec, spec)

    def test_renderer_rejects_non_spec(self) -> None:
        with self.assertRaises(TypeError):
            HudCardRenderer("nope")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            render_card_pixels("nope", "listening")  # type: ignore[arg-type]


class ValidationTests(unittest.TestCase):
    def test_spec_rejects_invalid_values(self) -> None:
        bad_kwargs = (
            {"surface_width": 0}, {"surface_width": -5}, {"surface_height": 0},
            {"surface_width": 5000}, {"corner_radius_px": -1}, {"corner_radius_px": 600},
            {"shadow_margin_px": -1}, {"shadow_offset_y_px": -1},
            {"shadow_blur_px": -1}, {"shadow_blur_px": 999},
            {"bar_width_px": 0}, {"bar_opacity": -1}, {"bar_opacity": 256},
            {"chip_opacity": 300}, {"chip_radius_px": 0},
            {"chip_x_px": -1}, {"chip_y_px": -1}, {"chip_pad_x_px": 0},
            {"chip_pad_y_px": 0}, {"content_left_px": 0}, {"content_right_px": 0},
            {"body_top_px": -1}, {"theme": "nope"},
        )
        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(HudError):
                    HudRenderSpec(**kwargs)

    def test_theme_rejects_invalid_values(self) -> None:
        bad_kwargs = (
            {"background_rgb": (0, 0)}, {"background_rgb": (0, 0, 999)},
            {"border_rgb": "nope"}, {"title_text": ""}, {"title_font_px": 0},
            {"body_font_px": -1}, {"opacity": -1}, {"opacity": 256},
            {"state_colors": []}, {"state_colors": {"": (1, 2, 3)}},
            {"state_colors": {"idle": (1, 2)}},
        )
        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(HudError):
                    HudTheme(**kwargs)

    def test_hud_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(HudError, ValueError))
        with self.assertRaises(ValueError):
            HudRenderSpec(surface_width=0)

    def test_cache_key_is_stable_and_sensitive(self) -> None:
        first = HudRenderSpec()
        second = HudRenderSpec()
        self.assertEqual(first.cache_key(), second.cache_key())
        self.assertNotEqual(first.cache_key(), HudRenderSpec(theme=HudTheme(opacity=200)).cache_key())
        self.assertNotEqual(first.cache_key(), HudRenderSpec(surface_width=300).cache_key())


class PerformanceTests(unittest.TestCase):
    def test_first_render_is_fast_enough_for_state_changes(self) -> None:
        spec = HudRenderSpec(surface_width=320, surface_height=96, shadow_margin_px=8)
        started = time.perf_counter()
        render_card_pixels(spec, "listening")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0, f"首次渲染耗时 {elapsed:.3f}s，超出预算")

    def test_cached_render_is_cheap(self) -> None:
        spec = HudRenderSpec()
        render_card_pixels(spec, "listening")
        started = time.perf_counter()
        for state in STATES * 5:
            render_card_pixels(spec, state)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, f"缓存渲染 20 帧耗时 {elapsed:.3f}s，超出预算")


if __name__ == "__main__":
    unittest.main()
