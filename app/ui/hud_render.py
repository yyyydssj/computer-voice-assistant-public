"""P9 UI 改版（方案 4 卡片胶囊版）：状态提示窗的**纯像素渲染核心**。

本模块只做一件事：把「外观参数 + 状态」变成一帧**预乘 alpha 的 BGRA 像素缓冲**。
它不导入 ``ctypes``、不接触任何 Win32、不联网、不读写文件、不创建线程，因此可以完全
离线测试（四角透明、圆角抗锯齿、阴影梯度、状态色、标题胶囊底都在这里断言）。

分工：``app/ui/voice_hud.py`` 负责 Windows 胶水（窗口、消息、GDI 文字叠加、
``UpdateLayeredWindow``），本模块负责像素；文字**不在本模块绘制**（由 GDI 叠加），
这也是纯函数像素断言能稳定的原因。

坐标口径（冻结）：
- 卡片 = ``surface_w × surface_h``（默认 320×96）；
- 窗口 = 卡片四周各留 ``shadow_margin_px``（默认 8）用于柔和外阴影，故 336×112；
- 卡片在窗口内的偏移恒为 ``(shadow_margin_px, shadow_margin_px)``；
- 缓冲行序自上而下，每像素 4 字节 ``B, G, R, A``，且 RGB 已按 alpha 预乘。

硬边界：模块级只导入 stdlib（``dataclasses`` / ``enum``）；不出现任何第三方依赖、
任何文件/网络访问，也不接受外部文本参与渲染（标题与正文都由状态枚举生成）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math

# ------------------------------------------------------------------ 外观默认值
_SURFACE_WIDTH = 320
_SURFACE_HEIGHT = 96
_SHADOW_MARGIN = 8
_CORNER_RADIUS = 16
_BORDER_RGB = (223, 226, 232)
_TITLE_FONT_PX = 13
_BODY_FONT_PX = 16
_BAR_WIDTH = 4
_BAR_OPACITY = 255
_CHIP_OPACITY = 31
_CHIP_RADIUS = 8
_CHIP_X = 14
_CHIP_Y = 12
_CHIP_PAD_X = 6
_CHIP_PAD_Y = 4
_CONTENT_LEFT = 16
_CONTENT_RIGHT = 12
_BODY_TOP = 44
_TITLE_DARKEN_NUM = 85
_TITLE_DARKEN_DEN = 100
_SHADOW_DOWN_OFFSET = 2
_SHADOW_BLUR_RADIUS = 5
_SHADOW_PEAK = 128
"""阴影实心掩码值。"""

_SHADOW_BLUR_PEAK = 99.0
"""两次半径 ``blur//2`` 盒式模糊后，阴影实心掩码的实测峰值（缩放基准）。"""

_SHADOW_TARGET_ALPHA = 57.0
"""阴影实心区目标 alpha（≈ 60/255 的柔和阴影，与效果图口径一致）。"""
_DEFAULT_OPACITY = 255
_DEFAULT_STATE = "idle"

_MAX_DIMENSION = 4096
_MAX_SHADOW_RADIUS = 64
_MAX_CORNER_RADIUS = 512
_RENDERER_CACHE_LIMIT = 8
_TEXT_BLEND_ALPHA = 217
"""标题字色的等效 alpha（85%）：GDI 文字不覆盖 alpha，故先把半透明折算成不透明色。"""

# 浅色主题四态色（与效果图 mock-light-4-card.png 一致；未知状态收敛为待机色）。
DEFAULT_STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "idle": (110, 118, 132),
    "listening": (26, 110, 220),
    "speaking": (16, 140, 92),
    "failed": (200, 48, 48),
}
"""四态固定色表（键是状态名，不是对话内容）；任何外部文本都进不到这里。"""

_TITLE_TEXT = "电脑语音助手"
_BODY_TEXTS: dict[str, str] = {
    "idle": "待机",
    "listening": "聆听",
    "speaking": "播报",
    "failed": "失败",
}

LAYER_OUTSIDE = 0
"""绘制分级：完全透明像素（卡片外且无阴影；GDI 文字前可安全写成不透明黑）。"""
LAYER_CARD = 1
"""绘制分级：卡片底色/描边像素（需写回 alpha=255）。"""
LAYER_BAR = 2
"""绘制分级：左侧状态色条像素。"""
LAYER_CHIP = 3
"""绘制分级：标题胶囊底像素。"""
LAYER_SHADOW = 4
"""绘制分级：卡片外的柔和阴影像素（必须保留其半透明 alpha）。"""


class HudError(ValueError):
    """渲染参数非法时的 fail-closed 结果（只用于参数校验，绝不携带外部正文）。"""


@dataclass(frozen=True, slots=True)
class HudTheme:
    """提示窗浅色主题；只有 ``state_colors`` 参与逐状态取色。

    ``opacity``（0..255）是整窗不透明度，渲染时折算进像素 alpha（默认 255 即不透明）。
    """

    background_rgb: tuple[int, int, int] = (255, 255, 255)
    border_rgb: tuple[int, int, int] = _BORDER_RGB
    title_text: str = _TITLE_TEXT
    title_font_px: int = _TITLE_FONT_PX
    body_font_px: int = _BODY_FONT_PX
    opacity: int = _DEFAULT_OPACITY
    state_colors: dict[str, tuple[int, int, int]] = field(
        default_factory=lambda: dict(DEFAULT_STATE_COLORS)
    )

    def __post_init__(self) -> None:
        for name in ("background_rgb", "border_rgb"):
            _require_rgb(name, getattr(self, name))
        if type(self.title_text) is not str or not self.title_text:
            raise HudError("title_text 必须是非空字符串")
        _require_positive_int("title_font_px", self.title_font_px)
        _require_positive_int("body_font_px", self.body_font_px)
        _require_int_range("opacity", self.opacity, 0, 255)
        if type(self.state_colors) is not dict:
            raise HudError("state_colors 必须是 dict")
        for key, value in self.state_colors.items():
            if type(key) is not str or not key:
                raise HudError("state_colors 的键必须是非空字符串")
            _require_rgb(f"state_colors[{key!r}]", value)

    def color_for(self, state: object) -> tuple[int, int, int]:
        """状态 → 状态色；未知状态收敛为待机色（不抛错、不回显外部内容）。"""

        key = _state_key(state)
        if key in self.state_colors:
            return self.state_colors[key]
        return self.state_colors.get(_DEFAULT_STATE, DEFAULT_STATE_COLORS[_DEFAULT_STATE])

    def title_color_for(self, state: object) -> tuple[int, int, int]:
        """标题字色 = 状态色加深 15%（乘 0.85），保证浅色胶囊上仍可读。"""

        return tuple(
            channel * _TITLE_DARKEN_NUM // _TITLE_DARKEN_DEN
            for channel in self.color_for(state)
        )

    def title_text_color_for(self, state: object) -> tuple[int, int, int]:
        """GDI 叠加标题时用的不透明字色：状态色加深 15% 与卡片底按 85% 混合。"""

        return _mix_rgb_tuple((255, 255, 255), self.title_color_for(state), _TEXT_BLEND_ALPHA)

    def chip_rgb_for(self, state: object) -> tuple[int, int, int]:
        """标题胶囊底色（不透明混合前的状态色；实际底色按 ``chip_opacity`` 与卡片底混）。"""

        return self.color_for(state)


@dataclass(frozen=True, slots=True)
class HudRenderSpec:
    """一帧渲染的完整参数（几何 + 主题）；不可变，可作为缓存键。"""

    surface_width: int = _SURFACE_WIDTH
    surface_height: int = _SURFACE_HEIGHT
    shadow_margin_px: int = _SHADOW_MARGIN
    corner_radius_px: int = _CORNER_RADIUS
    shadow_blur_px: int = _SHADOW_BLUR_RADIUS
    shadow_offset_y_px: int = _SHADOW_DOWN_OFFSET
    bar_width_px: int = _BAR_WIDTH
    bar_opacity: int = _BAR_OPACITY
    chip_opacity: int = _CHIP_OPACITY
    chip_radius_px: int = _CHIP_RADIUS
    chip_x_px: int = _CHIP_X
    chip_y_px: int = _CHIP_Y
    chip_pad_x_px: int = _CHIP_PAD_X
    chip_pad_y_px: int = _CHIP_PAD_Y
    content_left_px: int = _CONTENT_LEFT
    content_right_px: int = _CONTENT_RIGHT
    body_top_px: int = _BODY_TOP
    theme: HudTheme = field(default_factory=HudTheme)

    def __post_init__(self) -> None:
        if not isinstance(self.theme, HudTheme):
            raise HudError("theme 必须是 HudTheme")
        for name in ("surface_width", "surface_height", "bar_width_px",
                     "chip_radius_px", "chip_pad_x_px", "chip_pad_y_px",
                     "content_left_px", "content_right_px"):
            _require_positive_int(name, getattr(self, name))
        _require_non_negative_int("corner_radius_px", self.corner_radius_px)
        for name in ("surface_width", "surface_height"):
            if getattr(self, name) > _MAX_DIMENSION:
                raise HudError(f"{name} 不得超过 {_MAX_DIMENSION}")
        if self.corner_radius_px > _MAX_CORNER_RADIUS:
            raise HudError(f"corner_radius_px 不得超过 {_MAX_CORNER_RADIUS}")
        _require_non_negative_int("shadow_margin_px", self.shadow_margin_px)
        _require_non_negative_int("shadow_offset_y_px", self.shadow_offset_y_px)
        _require_non_negative_int("chip_x_px", self.chip_x_px)
        _require_non_negative_int("chip_y_px", self.chip_y_px)
        _require_non_negative_int("body_top_px", self.body_top_px)
        _require_non_negative_int("shadow_blur_px", self.shadow_blur_px)
        if self.shadow_blur_px > _MAX_SHADOW_RADIUS:
            raise HudError(f"shadow_blur_px 不得超过 {_MAX_SHADOW_RADIUS}")
        _require_int_range("bar_opacity", self.bar_opacity, 0, 255)
        _require_int_range("chip_opacity", self.chip_opacity, 0, 255)

    # ------------------------------------------------------------------ 几何
    @property
    def window_width(self) -> int:
        """真实窗口宽度 = 卡片宽 + 两侧阴影留白。"""

        return self.surface_width + 2 * self.shadow_margin_px

    @property
    def window_height(self) -> int:
        """真实窗口高度 = 卡片高 + 两侧阴影留白。"""

        return self.surface_height + 2 * self.shadow_margin_px

    @property
    def card_origin(self) -> tuple[int, int]:
        """卡片左上角在窗口内的偏移（恒为阴影留白）。"""

        return (self.shadow_margin_px, self.shadow_margin_px)

    def card_rect(self) -> tuple[int, int, int, int]:
        """卡片矩形 ``(left, top, right, bottom)``。"""

        left, top = self.card_origin
        return (left, top, left + self.surface_width, top + self.surface_height)

    def bar_rect(self) -> tuple[int, int, int, int]:
        """左侧状态色条矩形 ``(left, top, right, bottom)``。"""

        left, top = self.card_origin
        return (left, top, left + self.bar_width_px, top + self.surface_height)

    def title_chip_rect(self) -> tuple[int, int, int, int]:
        """标题胶囊矩形 ``(left, top, right, bottom)``，按标题字号与左右内边距推导。"""

        font_px = self.theme.title_font_px
        height = font_px + 2 * self.chip_pad_y_px
        left = self.chip_x_px
        top = self.chip_y_px
        width = _text_width_estimate(self.theme.title_text, font_px) + 2 * self.chip_pad_x_px
        max_right = self.surface_width - self.content_right_px
        right = min(left + width, max_right)
        return (left, top, right, top + height)

    def body_rect(self) -> tuple[int, int, int, int]:
        """正文矩形 ``(left, top, right, bottom)``。"""

        return (
            self.content_left_px,
            self.body_top_px,
            max(self.content_left_px, self.surface_width - self.content_right_px),
            self.surface_height,
        )

    def cache_key(self) -> tuple[object, ...]:
        """可哈希的渲染缓存键（状态色表折算为有序元组）。"""

        theme = self.theme
        return (
            self.surface_width, self.surface_height, self.shadow_margin_px,
            self.corner_radius_px, self.shadow_blur_px, self.shadow_offset_y_px,
            self.bar_width_px, self.bar_opacity, self.chip_opacity, self.chip_radius_px,
            self.chip_x_px, self.chip_y_px, self.chip_pad_x_px, self.chip_pad_y_px,
            self.content_left_px, self.content_right_px, self.body_top_px,
            theme.background_rgb, theme.border_rgb, theme.title_text,
            theme.title_font_px, theme.body_font_px, theme.opacity,
            tuple(sorted(theme.state_colors.items())),
        )


def state_color(state: object, *, theme: HudTheme | None = None) -> tuple[int, int, int]:
    """四态状态色；未知状态回退待机色（模块级便捷入口）。"""

    return (theme or HudTheme()).color_for(state)


def body_text(state: object) -> str:
    """状态 → 固定正文文案；未知状态收敛为待机文案（绝不回显外部内容）。"""

    return _BODY_TEXTS.get(_state_key(state), _BODY_TEXTS[_DEFAULT_STATE])


def card_pixel_size(spec: HudRenderSpec) -> tuple[int, int]:
    """卡片像素尺寸。"""

    return (spec.surface_width, spec.surface_height)


def window_pixel_size(spec: HudRenderSpec) -> tuple[int, int]:
    """窗口像素尺寸（含四周阴影留白）。"""

    return (spec.window_width, spec.window_height)


def card_origin_in_window(spec: HudRenderSpec) -> tuple[int, int]:
    """卡片在窗口内的偏移。"""

    return spec.card_origin


class HudCardRenderer:
    """把 ``HudRenderSpec`` 渲染成预乘 alpha 的 BGRA 缓冲。

    构造时一次性算好不随状态变化的三张覆盖度掩码（阴影、卡片、状态色条），此后每次
    ``render_card_pixels()`` 只做固定开销的合成。窗口小于等于 ``shadow_margin_px``
    的退化配置会 fail closed（``HudError``）。
    """

    def __init__(self, spec: HudRenderSpec | None = None) -> None:
        if spec is None:
            spec = HudRenderSpec()
        elif not isinstance(spec, HudRenderSpec):
            raise TypeError("spec 必须是 HudRenderSpec 或 None")
        self._spec = spec
        self._window_width = spec.window_width
        self._window_height = spec.window_height
        margin = spec.shadow_margin_px
        self._card_left = margin
        self._bar_right = margin + spec.bar_width_px
        self._chip_rect = spec.title_chip_rect()
        self._shadow_mask = _shadow_mask(spec)
        self._card_coverage = _rounded_coverage(spec.card_rect(), spec.corner_radius_px, 4, spec)
        self._bar_coverage = _rounded_coverage(
            spec.bar_rect(),
            min(spec.corner_radius_px, spec.surface_height // 2),
            4,
            spec,
        )
        self._chip_coverage = _rounded_coverage(
            self._chip_rect, spec.chip_radius_px, 4, spec
        )

    @property
    def spec(self) -> HudRenderSpec:
        return self._spec

    @property
    def window_size(self) -> tuple[int, int]:
        return (self._window_width, self._window_height)

    @property
    def card_coverage(self) -> bytearray:
        """卡片覆盖度掩码（1 字节/像素，供图层分级使用）。"""

        return self._card_coverage

    @property
    def shadow_mask(self) -> bytearray:
        """外阴影 alpha 掩码（1 字节/像素）。"""

        return self._shadow_mask

    @property
    def bar_coverage(self) -> bytearray:
        """左侧状态色条覆盖度掩码。"""

        return self._bar_coverage

    @property
    def chip_coverage(self) -> bytearray:
        """标题胶囊底覆盖度掩码。"""

        return self._chip_coverage

    @property
    def chip_rect(self) -> tuple[int, int, int, int]:
        """标题胶囊矩形。"""

        return self._chip_rect

    @property
    def bar_right(self) -> int:
        """状态色条右边界（左闭右开）。"""

        return self._bar_right

    @property
    def card_left(self) -> int:
        """卡片左边界。"""

        return self._card_left

    def render_card_pixels(self, state: object) -> bytes:
        """渲染一帧：返回长度 ``window_w * window_h * 4`` 的预乘 BGRA 缓冲。

        图层自下而上：外阴影 → 卡片（底 + 描边）→ 左侧状态色条 → 标题胶囊底。
        文字不在此绘制（由 GDI 叠加）；未知状态收敛为待机色，绝不抛错。
        """

        spec = self._spec
        theme = spec.theme
        background = theme.background_rgb
        border = theme.border_rgb
        accent = theme.color_for(state)
        chip = _mix_rgb_tuple(background, theme.chip_rgb_for(state), spec.chip_opacity)
        overall = theme.opacity
        window_width = self._window_width
        shadow = self._shadow_mask
        card = self._card_coverage
        bar = self._bar_coverage
        chip_mask = self._chip_coverage
        bar_opacity = spec.bar_opacity
        card_left = self._card_left
        chip_left, chip_top, chip_right, chip_bottom = self._chip_rect
        body = bytearray(4 * window_width * self._window_height)
        for pixel in range(window_width * self._window_height):
            shadow_alpha = shadow[pixel]
            card_alpha = card[pixel]
            if shadow_alpha == 0 and card_alpha == 0:
                continue
            index = pixel * 4
            x = pixel % window_width
            y = pixel // window_width
            red = green = blue = 0
            alpha = shadow_alpha
            if card_alpha:
                if card_alpha == 255:
                    red, green, blue = background
                else:
                    red, green, blue = _mix_rgb_tuple(background, border, card_alpha)
                if card_left <= x < self._bar_right:
                    bar_alpha = bar[pixel] * bar_opacity // 255
                    if bar_alpha:
                        red, green, blue = _mix_rgb_tuple((red, green, blue), accent, bar_alpha)
                if chip_left <= x < chip_right and chip_top <= y < chip_bottom:
                    chip_alpha = chip_mask[pixel]
                    if chip_alpha:
                        red, green, blue = _mix_rgb_tuple((red, green, blue), chip, chip_alpha)
                # 卡片内一律不透明：圆角/色条的抗锯齿覆盖率是相对底色的混合比例，
                # 不是像素 alpha；若留成部分 alpha，GDI 文字会把它写成透明洞。
                alpha = 255 if overall == 255 else overall
            body[index] = blue * alpha // 255
            body[index + 1] = green * alpha // 255
            body[index + 2] = red * alpha // 255
            body[index + 3] = alpha
        return bytes(body)


_RENDERER_CACHE: dict[tuple[object, ...], HudCardRenderer] = {}


def render_card_pixels(spec: HudRenderSpec, state: object) -> bytes:
    """渲染一帧预乘 BGRA 像素缓冲（同一 spec 复用已算好的掩码）。"""

    if not isinstance(spec, HudRenderSpec):
        raise TypeError("spec 必须是 HudRenderSpec")
    return _renderer_for(spec).render_card_pixels(state)


def paint_layer_masks(spec: HudRenderSpec, state: object) -> bytes:
    """返回每个像素的**绘制图层分级**掩码（1 字节/像素，取值见 ``LAYER_*``）。

    GDI 文字会直接把颜色写进 32 位 DIB：``BLACKNESS`` 位图模式下 alpha=0 的像素被写成
    「黑 + alpha=255」，alpha>0 的像素被写成「黑 + alpha=0」。因此 Win32 侧需要这份
    分级掩码把像素先归一化成正确的不透明底色，再叠加文字：

    - ``LAYER_OUTSIDE``：完全透明（渲染缓冲 alpha=0），需要写「黑 + alpha=255」；
    - ``LAYER_SHADOW``：柔和阴影，**必须原样保留**半透明 alpha；
    - ``LAYER_CARD`` / ``LAYER_BAR`` / ``LAYER_CHIP``：卡片内（alpha=255），需要保留
      渲染缓冲里的颜色并把 alpha 写回 255。

    本函数只依赖本模块的纯几何与覆盖度，不接触任何系统 API。
    """

    if not isinstance(spec, HudRenderSpec):
        raise TypeError("spec 必须是 HudRenderSpec")
    renderer = _renderer_for(spec)
    window_width = spec.window_width
    height = spec.window_height
    card = renderer.card_coverage
    bar = renderer.bar_coverage
    chip = renderer.chip_coverage
    shadow = renderer.shadow_mask
    chip_rect = renderer.chip_rect
    bar_right = renderer.bar_right
    card_left = renderer.card_left
    chip_left, chip_top, chip_right, chip_bottom = chip_rect
    masks = bytearray(window_width * height)
    for pixel in range(window_width * height):
        if not card[pixel]:
            masks[pixel] = LAYER_SHADOW if shadow[pixel] else LAYER_OUTSIDE
            continue
        x = pixel % window_width
        y = pixel // window_width
        if (chip_left <= x < chip_right and chip_top <= y < chip_bottom and chip[pixel]):
            masks[pixel] = LAYER_CHIP
        elif card_left <= x < bar_right and bar[pixel]:
            masks[pixel] = LAYER_BAR
        else:
            masks[pixel] = LAYER_CARD
    return bytes(masks)

def _renderer_for(spec: HudRenderSpec) -> HudCardRenderer:
    """按 spec 复用渲染器（与 ``render_card_pixels`` 共用同一缓存）。"""

    key = spec.cache_key()
    renderer = _RENDERER_CACHE.get(key)
    if renderer is None:
        renderer = HudCardRenderer(spec)
        if len(_RENDERER_CACHE) >= _RENDERER_CACHE_LIMIT:
            _RENDERER_CACHE.clear()
        _RENDERER_CACHE[key] = renderer
    return renderer


# ------------------------------------------------------------------ 覆盖度掩码


def _shadow_mask(spec: HudRenderSpec) -> bytearray:
    """阴影 alpha 掩码：卡片实心掩码（下移 ``shadow_offset_y_px``）盒式模糊后缩放。

    数值口径（冻结）：卡片掩码取 ``_SHADOW_PEAK``（128），经**两次**半径 ``blur//2``
    的盒式模糊后实心区实测 ``_SHADOW_BLUR_PEAK``（99），再乘
    ``_SHADOW_TARGET_ALPHA/_SHADOW_BLUR_PEAK`` 缩放到 40..70 的柔和区间；窗口内
    卡片以外一律为 0，保证外圈不残留阴影。
    """

    window_width = spec.window_width
    window_height = spec.window_height
    mask = bytearray(window_width * window_height)
    if spec.shadow_margin_px == 0:
        return mask
    left, top, right, bottom = spec.card_rect()
    offset_y = spec.shadow_offset_y_px
    radius = spec.corner_radius_px
    for y in range(window_height):
        base = y * window_width
        for x in range(window_width):
            if _inside_rounded_rect(x + 0.5, y + 0.5, left, top + offset_y,
                                    right, bottom + offset_y, radius):
                mask[base + x] = _SHADOW_PEAK
    blur = spec.shadow_blur_px
    if blur > 0:
        half = max(1, blur // 2)
        half = min(half, (window_width - 1) // 2, (window_height - 1) // 2)
        if half > 0:
            _box_blur(mask, window_width, window_height, half)
            _box_blur(mask, window_width, window_height, half)
    scale = _SHADOW_TARGET_ALPHA / _SHADOW_BLUR_PEAK
    for index in range(len(mask)):
        value = mask[index]
        if value:
            mask[index] = min(255, int(value * scale + 0.5))
    return mask


def _rounded_coverage(rect: tuple[int, int, int, int], radius: int, scale: int,
                      spec: HudRenderSpec) -> bytearray:
    """整窗大小的圆角矩形覆盖度掩码（0..255），矩形外为 0。

    覆盖度用**解析式签名距离**的 1px 线性过渡计算（``clamp(0.5 - d, 0, 1)``），
    只对边缘过渡带内的像素按 ``scale×scale`` 超采样细化，因此既精确又便宜。
    掩码按整窗输出（矩形外的窗口像素一律为 0），避免外圈残留污染阴影层。
    """

    left, top, right, bottom = rect
    width = spec.window_width
    height = spec.window_height
    mask = bytearray(width * height)
    right = min(right, width)
    bottom = min(bottom, height)
    left = max(left, 0)
    top = max(top, 0)
    if right <= left or bottom <= top:
        return mask
    radius = max(0, min(radius, (right - left) // 2, (bottom - top) // 2))
    for y in range(top, bottom):
        base = y * width
        for x in range(left, right):
            distance = _signed_distance_rounded_rect(x + 0.5, y + 0.5, left, top, right, bottom,
                                                     radius)
            if distance <= -0.5:
                mask[base + x] = 255
            elif distance >= 0.5:
                mask[base + x] = 0
            else:
                mask[base + x] = _supersample_alpha(x, y, left, top, right, bottom, radius, scale)
    return mask


def _supersample_alpha(x: int, y: int, left: int, top: int, right: int, bottom: int,
                       radius: int, scale: int) -> int:
    """对边缘过渡带单像素做 ``scale×scale`` 超采样，返回 0..255 覆盖度。"""

    inside = 0
    divisor = scale * scale
    for sub_y in range(scale):
        sample_y = y + (sub_y + 0.5) / scale
        for sub_x in range(scale):
            sample_x = x + (sub_x + 0.5) / scale
            if _signed_distance_rounded_rect(sample_x, sample_y, left, top, right, bottom,
                                             radius) <= 0.0:
                inside += 1
    if inside == 0:
        return 0
    if inside == divisor:
        return 255
    return inside * 255 // divisor


def _signed_distance_rounded_rect(x: float, y: float, left: int, top: int,
                                  right: int, bottom: int, radius: int) -> float:
    """点到圆角矩形的签名距离：内部为负、边界为 0、外部为正。"""

    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    half_width = (right - left) / 2.0
    half_height = (bottom - top) / 2.0
    delta_x = abs(x - center_x) - (half_width - radius)
    delta_y = abs(y - center_y) - (half_height - radius)
    outside_x = delta_x if delta_x > 0 else 0.0
    outside_y = delta_y if delta_y > 0 else 0.0
    outside = math.sqrt(outside_x * outside_x + outside_y * outside_y)
    inside = min(max(delta_x, delta_y), 0.0)
    return outside + inside - radius


def _box_blur(mask: bytearray, width: int, height: int, radius: int) -> None:
    """对掩码做一次水平 + 垂直盒式模糊（原地、分离两趟）。"""

    if radius <= 0 or width <= 0 or height <= 0:
        return
    window = 2 * radius + 1
    # 水平趟：每行维护长度为 window 的滑动和；写入前先同步窗口（先减后加），
    # 这样窗口内元素都是当前（可能已更新）值，不会出现加减顺序错位的漂移。
    for y in range(height):
        base = y * width
        total = 0
        for x in range(-radius, radius + 1):
            if 0 <= x < width:
                total += mask[base + x]
        for x in range(width):
            drop = x - radius
            if 0 <= drop < width:
                total -= mask[base + drop]
            add = x + radius
            if 0 <= add < width:
                total += mask[base + add]
            mask[base + x] = total // window
    # 垂直趟：同一套滑动和写法，按列处理。
    for x in range(width):
        total = 0
        for y in range(-radius, radius + 1):
            if 0 <= y < height:
                total += mask[y * width + x]
        for y in range(height):
            drop = y - radius
            if 0 <= drop < height:
                total -= mask[drop * width + x]
            add = y + radius
            if 0 <= add < height:
                total += mask[add * width + x]
            mask[y * width + x] = total // window


def _inside_rounded_rect(x: float, y: float, left: int, top: int, right: int, bottom: int,
                         radius: int) -> bool:
    """点是否在圆角矩形内（半径 0 时退化为普通矩形）。"""

    if x < left or x >= right or y < top or y >= bottom:
        return False
    if radius <= 0:
        return True
    inner_left = left + radius
    inner_right = right - radius
    inner_top = top + radius
    inner_bottom = bottom - radius
    if inner_left <= x < inner_right or inner_top <= y < inner_bottom:
        return True
    center_x = inner_left if x < inner_left else inner_right
    center_y = inner_top if y < inner_top else inner_bottom
    dx = x - center_x
    dy = y - center_y
    return dx * dx + dy * dy <= radius * radius


# ------------------------------------------------------------------ 取值与校验


def _state_key(state: object) -> str:
    if isinstance(state, Enum):
        return str(state.value)
    if type(state) is str:
        return state
    return _DEFAULT_STATE


def _text_width_estimate(text: str, font_px: int) -> int:
    """中文字宽估算（用于胶囊与内容区宽度；实际绘制尺寸由 GDI 决定）。"""

    width = 0
    for char in text:
        width += font_px if ord(char) > 0x2E80 else max(1, font_px // 2)
    return width


def _mix_rgb_tuple(base: tuple[int, int, int], overlayer: tuple[int, int, int],
                   alpha: int) -> tuple[int, int, int]:
    """预乘安全的两色混合：``base*(255-a)/255 + over*a/255``，返回 0..255 整数三元组。"""

    if alpha <= 0:
        return base
    if alpha >= 255:
        return overlayer
    inverse = 255 - alpha
    return (
        (base[0] * inverse + overlayer[0] * alpha) // 255,
        (base[1] * inverse + overlayer[1] * alpha) // 255,
        (base[2] * inverse + overlayer[2] * alpha) // 255,
    )


def _require_rgb(name: str, value: object) -> None:
    if (not isinstance(value, tuple) or len(value) != 3
            or any(type(channel) is not int or not 0 <= channel <= 255 for channel in value)):
        raise HudError(f"{name} 必须是 3 个 0..255 整数")


def _require_int_range(name: str, value: object, minimum: int, maximum: int | None) -> None:
    if type(value) is not int:
        raise HudError(f"{name} 必须是整数")
    if value < minimum:
        raise HudError(f"{name} 不得小于 {minimum}")
    if maximum is not None and value > maximum:
        raise HudError(f"{name} 不得大于 {maximum}")


def _require_positive_int(name: str, value: object) -> None:
    _require_int_range(name, value, 1, None)


def _require_non_negative_int(name: str, value: object) -> None:
    _require_int_range(name, value, 0, None)


__all__ = [
    "DEFAULT_STATE_COLORS",
    "LAYER_BAR",
    "LAYER_CARD",
    "LAYER_CHIP",
    "LAYER_OUTSIDE",
    "LAYER_SHADOW",
    "HudCardRenderer",
    "HudError",
    "HudRenderSpec",
    "HudTheme",
    "body_text",
    "card_origin_in_window",
    "card_pixel_size",
    "paint_layer_masks",
    "render_card_pixels",
    "state_color",
    "window_pixel_size",
]
