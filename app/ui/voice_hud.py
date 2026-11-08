"""P9 UI 批次 1：右下角「正在对话」状态提示窗（零依赖 ctypes 自绘）。

本模块只把**语音链路已经到达的状态**渲染成一个常驻桌面角落的小窗：它不读取识别文本、
不读取回复文本、不显示任何路径或异常正文，窗口上的每一个字都由状态枚举生成。
它不代表 ASR/TTS 可靠性、不构成任何授权或任务事实，也不进入安全链。

显示口径（简化后只有 4 种文案）：内部 9 个状态经 ``STATE_LABELS`` 映射为
``待机`` / ``聆听`` / ``播报`` / ``失败``；不显示轮次序号，也不区分「听到了 / 识别中 /
思考中」等细粒度阶段（细粒度事实仍由语音链路自己的诊断面提供，不进入本窗口）。

硬边界（离线模块）：
- 不联网、不打开麦克风/扬声器、不导入 ``app.safety`` / ``app.executor`` / ``app.plans``
  / ``app.auditing`` / ``app.verification`` / ``app.tools``，也不导入 ``app.tts`` / ``app.asr``；
- 不读写任何文件，不创建线程或定时器；只做非阻塞的 ``PeekMessageW`` 抽消息，不使用会
  卡住语音链路的阻塞式 ``GetMessageW`` 消息循环，也不调用 ``SetForegroundWindow`` 抢焦点；
- 模块级只导入 stdlib（``ctypes`` / ``dataclasses`` / ``enum`` / ``sys`` / ``typing``）；
  真实 Win32 实现只在 ``build_win32_api()`` 或 ``show()`` 首次建窗时延迟构造。

可测性：所有 Win32 调用经窄接口 ``VoiceHudApi``；离线测试注入 fake api 即可完整覆盖文案、
坐标、幂等、拖动与失败收敛，不需要、也不会打开真实窗口。

销毁后语义（冻结，测试与 docstring 一致）：
- ``destroy()`` 幂等；第二次调用之后窗口句柄仍为“未创建”；
- 销毁后 ``tick()`` 返回 ``0``（安全空操作），``state`` / ``turn_index`` / ``visible`` /
  ``last_text`` 仍可读（只反映最后一次状态）；
- 销毁后 ``set_state()`` / ``show()`` / ``hide()`` 抛 ``VoiceHudError("not_created")``。

错误码（``VoiceHudError`` 的 ``str()`` 只能是固定短代码，绝不含异常正文、路径或文本）：
- ``unsupported_platform``：非 Windows 平台；
- ``win32_failed``：user32/gdi32 加载、建窗、绘制、抽消息等任一步骤失败；
- ``not_created``：窗口未创建或已销毁。
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
import sys
import time
from typing import Protocol

from app.ui.hud_render import (
    DEFAULT_STATE_COLORS as HUD_STATE_COLORS,
    HudError as HudRenderError,
    HudRenderSpec,
    HudTheme,
    LAYER_BAR,
    LAYER_CARD,
    LAYER_CHIP,
    LAYER_OUTSIDE,
    LAYER_SHADOW,
    body_text as body_text_for,
    paint_layer_masks,
    render_card_pixels,
)


VOICE_HUD_SCHEMA_VERSION = 3
"""状态提示窗契约版本号；变更文案/坐标语义时必须同步递增
（v2：9 态收敛为 4 种文案；v3：改为浅色卡片胶囊外观 + 逐像素渲染，窗口外扩阴影留白）。"""

WINDOW_CLASS_NAME = "ComputerVoiceAssistantHud"
"""真实 ``WNDCLASSW`` 注册用的窗口类名（隐藏类，不显示任务栏按钮）。"""

TITLE_TEXT = "电脑语音助手"
"""窗口标题与首行文案（固定中文，不接受外部内容）。"""

UNSUPPORTED_PLATFORM = "unsupported_platform"
WIN32_FAILED = "win32_failed"
NOT_CREATED = "not_created"

_ERROR_CODES = frozenset({UNSUPPORTED_PLATFORM, WIN32_FAILED, NOT_CREATED})

DEFAULT_SCREEN_SIZE = (1920, 1080)
"""未注入 ``screen`` 且真实实现取不到主屏分辨率时的保守回退分辨率。"""

# ---------------------------------------------------------------- Win32 常量
# 只声明本模块真正用到的少量常量，数值取自 Win32 SDK 头文件（user32/gdi32）
# 与本项目 ``app/tts/waveout_pcm.py`` 手写 winmm 的风格一致。
_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000

_SWP_NOACTIVATE = 0x0010

_LWA_ALPHA = 0x00000002
_SPI_GETWORKAREA = 0x0030

_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

_WM_PAINT = 0x000F
_WM_CLOSE = 0x0010
_WM_ERASEBKGND = 0x0014
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_MOUSEMOVE = 0x0200
_WM_TIMER = 0x0113

_PM_REMOVE = 0x0001

_FW_NORMAL = 400
_CLEARTYPE_QUALITY = 5
_DEFAULT_CHARSET = 1
_OUT_DEFAULT_PRECIS = 0
_CLIP_DEFAULT_PRECIS = 0
_DEFAULT_PITCH = 0
_FF_DONTCARE = 0
_BLACKNESS = 0x00000042
_TRANSPARENT_BKGMODE = 1
_DT_LEFT = 0x00000000
_DT_NOPREFIX = 0x00000800

_WM_LBUTTONDOWN_ID = 1
"""``KillTimer`` 用的定时器 id（批次 A 起拖动**不再**靠定时器合并，只在收尾时清理它）。"""
_DRAG_TIMER_MS = 16
"""历史常量：拖动合并定时器的间隔。批次 A 后**不再使用**（跟手改为移动事件直接定位），
保留它是为了不改变既有模块级名字、避免无谓的契约面变化。"""

# ------------------------------------------------- 逐像素渲染（v3）相关常量
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_ULW_ALPHA = 0x00000002
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_GDI_ERROR = 0xFFFFFFFF
_SRCCOPY = 0x00CC0020
"""渲染模式：``pixel`` 为逐像素分层窗口（v3 默认），``legacy`` 为纯色卡片降级路径。"""

# ------------------------------------------------- 批次 B：动效（帧缓存 + 整窗 alpha）
_ANIMATION_DIP_ALPHA = 120
"""状态切换软过渡的"最低点"alpha：旧帧淡到它、新帧从它淡起。

不降到 0 是为了避免整窗彻底消失（闪黑）；120/255 ≈ 47% 在浅色卡片上读起来是"柔和一沉"。
"""
_COMPOSED_FRAME_CACHE_LIMIT = 8
"""动效帧缓存的条目上限（每帧约 150 KB）；可见文案组合远小于它，超出即清空。"""

_STATE_SWITCH_GUARD_S = 0.2
"""防闪护栏：距上次状态切换不足这么久时，下一次切换**跳过动效**直接硬切。

真机场景：没有播报的轮次里 ``SPEAKING → LISTENING`` 可能只隔几十毫秒，连跑两段过渡看起来
就是"闪了两下"。护栏只影响动效（默认本就关闭），不改变任何状态语义与渲染结果。
"""

_SPI_GETCLIENTAREAANIMATION = 0x1042
"""``SystemParametersInfoW`` 的"动画效果"查询项（与真实 SDK ``WinUser.h`` 一致）。

批次 C：系统辅助功能里关掉动画效果时，本模块**强制不做任何动效**（业界 toast 库都遵
``prefers-reduced-motion``，Win32 侧的等价开关就是它）。
"""


def _system_animations_enabled(user32: object) -> bool:
    """系统"动画效果"是否开启；读不到或调用失败一律**当作开启**（fail-open）。

    fail-open 的理由：读不到就禁动效会让界面变得比用户预期更"死"，而这只是装饰性能力；
    真正的显示与状态语义完全不受影响。
    """

    getter = getattr(user32, "SystemParametersInfoW", None)
    if not callable(getter):
        return True
    value = ctypes.c_int(-1)
    try:
        ok = getter(_SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0)
    except Exception:  # noqa: BLE001 - 探测失败不得影响显示
        return True
    if not ok or value.value < 0:
        return True
    return bool(value.value)

_ALPHA_SCALE_TABLES: dict[int, bytes] = {}


def _alpha_scale_table(alpha_scale: int) -> bytes:
    """把整窗按 ``alpha_scale/255`` 缩放用的 256 项字节翻译表（带缓存）。

    预乘 BGRA 帧里四个通道**已经乘过 alpha**，所以整体淡出/淡入必须把 B/G/R/A **一起**
    按同一比例缩放；`bytes.translate` 因此就是一行 C 速度的整帧缩放。
    """

    table = _ALPHA_SCALE_TABLES.get(alpha_scale)
    if table is None:
        table = bytes((value * alpha_scale + 127) // 255 for value in range(256))
        _ALPHA_SCALE_TABLES[alpha_scale] = table
    return table

MODE_PIXEL = "pixel"
MODE_LEGACY = "legacy"


class VoiceHudState(str, Enum):
    """提示窗内部状态；多个状态映射到同一条可见文案（见 ``STATE_LABELS``）。"""

    IDLE = "idle"
    LISTENING = "listening"
    HEARING = "hearing"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


STATE_LABELS: dict[VoiceHudState, str] = {
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
"""冻结的固定中文文案映射（9 态 → 4 种可见文案：待机 / 聆听 / 播报 / 失败）。"""


class VoiceHudError(RuntimeError):
    """提示窗不可用或调用失败的 fail-closed 结果。

    ``str()`` **只**返回固定短代码；构造时的未知文本一律收敛为 ``win32_failed``，避免把
    异常正文、文件路径或任何对话内容带出模块。状态提示窗不是安全边界，失败只影响显示。
    """

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else WIN32_FAILED
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VoiceHudConfig:
    """提示窗的显示参数；只描述外观与位置，不改变任何安全判定。

    ``width`` / ``height`` 是窗口像素尺寸，``margin_px`` 是贴右下角的内缩距离，
    ``opacity`` 是 0..255 的整窗不透明度。``title`` 是固定标题文案。
    """

    width: int = 320
    height: int = 96
    margin_px: int = 24
    opacity: int = 235
    font_px: int = 15
    background_rgb: tuple[int, int, int] = (255, 255, 255)
    foreground_rgb: tuple[int, int, int] = (110, 118, 132)
    accent_rgb: tuple[int, int, int] = (110, 118, 132)
    title: str = TITLE_TEXT
    topmost: bool = True
    draggable: bool = True
    shadow_margin_px: int = 8
    corner_radius_px: int = 16
    border_rgb: tuple[int, int, int] = (223, 226, 232)
    title_font_px: int = 13
    body_font_px: int = 16
    state_colors: tuple[tuple[str, tuple[int, int, int]], ...] = ()
    """可选的四态色覆盖（有序 ``(状态名, RGB)`` 元组）；空表示使用模块冻结的四态色。"""

    drag_snap_px: int = 24
    """松手吸附距离（像素，批次 A）：距工作区边缘不超过它的窗口会在松手后**缓动**贴边。

    ``0`` 表示完全关闭吸附（松手即停在原地）；吸附只补"差一点"的距离，不会把窗口拖离
    用户松手的位置超过该像素数。
    """
    drag_settle_ms: int = 120
    """吸附缓动的时长（毫秒，批次 A）；``0`` 表示直接跳到贴边位置（无动画）。

    缓动在窗口过程内**内联**跑完（约 120 ms，鼠标已松开，不进入对话/播报路径），
    与状态动效一样不新增定时器、不新增线程。
    """

    state_animation_ms: int = 0
    """状态切换动效时长（毫秒，批次 B）；**默认 0 = 硬切**。

    真机反馈（2026-10-12）：开启时"旧帧淡到最低点再淡入新帧"会被看成**闪烁**——那本质是
    一次亮度往返脉冲。因此默认关闭，状态变化直接换帧（零亮度变化 = 绝不会闪）；
    只有显式设成 >0 才启用该脉冲式过渡，且仍受"短促连续切换跳过动效"的防闪护栏约束。
    """
    show_fade_ms: int = 260
    """首次 ``show()`` 的淡入时长（毫秒，批次 B/C）；``0`` = 直接显示。

    这是**单向**从透明淡入（不往返），与状态切换的脉冲不同，不会被看成闪烁。
    260 ms 对齐业界 toast 的进场时长（sonner 300 ms / react-hot-toast 350 ms）。
    """
    show_slide_px: int = 12
    """进场/退场的横向位移（像素，批次 C）：进场从右侧滑入、退场向右滑出。``0`` = 不滑。"""
    hide_fade_ms: int = 180
    """退场时长（毫秒，批次 C）：``hide()`` 先淡出+右滑再真正隐藏；``0`` = 立即隐藏。

    退场比进场短，与两个主流 toast 库一致（sonner 200/300 ms、react-hot-toast 400/350 ms）。
    """
    respect_system_animation: bool = True
    """是否尊重系统"动画效果"开关（批次 C，默认 True）。

    为 True 且系统关掉动画效果（``SPI_GETCLIENTAREAANIMATION`` 读到 0）时，本模块**强制
    不做任何动效**（拖动吸附、进场、退场、状态过渡全部按"0 时长"处理）。设 False 可覆盖，
    只在明确需要"无视系统设置"时才这么做。
    """

    def __post_init__(self) -> None:
        if type(self.width) is not int or self.width <= 0:
            raise ValueError("width 必须是正整数")
        if type(self.height) is not int or self.height <= 0:
            raise ValueError("height 必须是正整数")
        if type(self.margin_px) is not int or self.margin_px < 0:
            raise ValueError("margin_px 必须是非负整数")
        if type(self.opacity) is not int or not 0 <= self.opacity <= 255:
            raise ValueError("opacity 必须在 0..255 内")
        if type(self.font_px) is not int or self.font_px <= 0:
            raise ValueError("font_px 必须是正整数")
        for name in ("background_rgb", "foreground_rgb", "accent_rgb", "border_rgb"):
            value = getattr(self, name)
            if (not isinstance(value, tuple) or len(value) != 3
                    or any(type(channel) is not int or not 0 <= channel <= 255 for channel in value)):
                raise ValueError(f"{name} 必须是 3 个 0..255 整数")
        if type(self.title) is not str or not self.title:
            raise ValueError("title 必须是非空字符串")
        if type(self.topmost) is not bool or type(self.draggable) is not bool:
            raise ValueError("topmost/draggable 必须是 bool")
        for name in ("shadow_margin_px", "corner_radius_px", "drag_snap_px", "drag_settle_ms",
                     "state_animation_ms", "show_fade_ms", "show_slide_px", "hide_fade_ms"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if type(self.respect_system_animation) is not bool:
            raise ValueError("respect_system_animation 必须是 bool")
        for name in ("title_font_px", "body_font_px"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if not isinstance(self.state_colors, tuple):
            raise ValueError("state_colors 必须是有序元组")
        for entry in self.state_colors:
            if (not isinstance(entry, tuple) or len(entry) != 2
                    or type(entry[0]) is not str or not entry[0]):
                raise ValueError("state_colors 的每一项必须是 (状态名, RGB)")
            color = entry[1]
            if (not isinstance(color, tuple) or len(color) != 3
                    or any(type(channel) is not int or not 0 <= channel <= 255 for channel in color)):
                raise ValueError("state_colors 的颜色必须是 3 个 0..255 整数")


def state_label(state: VoiceHudState) -> str:
    """返回状态的固定中文文案；未知状态收敛为待机文案，绝不回显外部内容。"""

    if isinstance(state, VoiceHudState):
        return STATE_LABELS[state]
    return STATE_LABELS[VoiceHudState.IDLE]


def _in_text_area(x: int, y: int, title_rect: tuple[int, int, int, int],
                  body_rect: tuple[int, int, int, int]) -> bool:
    """像素是否落在标题胶囊或正文矩形内（只有这些完全透明像素需要文字底板）。"""

    for left, top, right, bottom in (title_rect, body_rect):
        if left <= x < right and top <= y < bottom:
            return True
    return False


def _snapshot_rects(source: int, width: int, rects: tuple[object, ...]) -> bytes:
    """按行抓取若干矩形区域的像素做基线，供绘制后比对（矩形按 ``_Rect`` 传入）。"""

    parts: list[bytes] = []
    for rect in rects:
        left, top = max(0, rect.left), max(0, rect.top)
        right, bottom = max(left, rect.right), max(top, rect.bottom)
        for row in range(top, bottom):
            offset = (row * width + left) * 4
            parts.append(ctypes.string_at(source + offset, (right - left) * 4))
    return b"".join(parts)


def _restore_text_alpha(source: int, width: int, rects: tuple[object, ...],
                        snapshot: bytes) -> None:
    """把文字矩形内发生变化的字形像素 alpha 补回 255。

    GDI 绘制文字时会写「正确 RGB + alpha=0」，而分层窗口按预乘 alpha 合成，alpha=0 会
    让字完全不可见。这里按帧前快照找出被文字改动的像素并只补 alpha，字形边缘的抗锯齿
    颜色保持不变。
    """

    cursor = 0
    for rect in rects:
        left, top = max(0, rect.left), max(0, rect.top)
        right, bottom = max(left, rect.right), max(top, rect.bottom)
        span = (right - left) * 4
        for row in range(top, bottom):
            base = (row * width + left) * 4
            current = ctypes.string_at(source + base, span)
            previous = snapshot[cursor:cursor + span]
            cursor += span
            if current == previous:
                continue
            buffer = bytearray(current)
            for index in range(3, span, 4):
                if buffer[index] != 255:
                    buffer[index] = 255
            ctypes.memmove(source + base, bytes(buffer), span)


def _state_value(state: object) -> str:
    """把状态收敛为渲染层使用的状态名（未知状态 → ``idle``）。"""

    if isinstance(state, VoiceHudState):
        return state.value
    if isinstance(state, Enum):
        return str(state.value)
    if type(state) is str:
        return state
    return VoiceHudState.IDLE.value


def hud_text(state: VoiceHudState, *, turn_index: int) -> tuple[str, str]:
    """由状态生成 ``(标题, 正文)``；正文只有一行固定文案。

    ``turn_index`` 仅为兼容既有调用方而保留，**不再参与渲染**（简化后窗口不显示轮次）。
    入参只有状态枚举与整数轮次，因此返回值不可能包含识别文本、回复文本、路径或异常正文。
    """

    return (TITLE_TEXT, state_label(state))


def screen_position_for(
    *, screen: tuple[int, int], width: int, height: int, margin_px: int, pad: int = 0
) -> tuple[int, int]:
    """右下角贴边坐标：``x = screen_w - width - margin``，``y = screen_h - height - margin``。

    ``pad`` 是窗口相对卡片外扩的阴影留白：``pad=0`` 时旧行为逐字节不变；``pad>0`` 时
    窗口左上角再向左上各移 ``pad``，使**卡片**右下角与工作区右下角仍保持 ``margin_px``。
    屏幕比窗口还小时钳制到 0，避免把窗口推出可见区域；调用方只传已验证的整数。
    """

    screen_width, screen_height = screen
    x = max(0, screen_width - width - margin_px - pad)
    y = max(0, screen_height - height - margin_px - pad)
    return (x, y)


def clamp_position(
    *, x: int, y: int, screen: tuple[int, int], width: int, height: int, margin_px: int = 0
) -> tuple[int, int]:
    """把拖动后的坐标约束在屏幕内（不得拖出屏幕），并保留 ``margin_px`` 内缩。

    ``margin_px=0`` 时边界就是屏幕边界；屏幕小于窗口（可用范围为空）时贴到最小内缩位置。
    """

    screen_width, screen_height = screen
    margin = max(0, margin_px)
    max_x = max(margin, screen_width - width - margin)
    max_y = max(margin, screen_height - height - margin)
    return (min(max(margin, x), max_x), min(max(margin, y), max_y))


def _snap_to_work_area(
    *, x: int, y: int, area: tuple[int, int, int, int], width: int, height: int, snap_px: int
) -> tuple[int, int]:
    """把松手位置吸附到工作区边缘，只在距边缘不超过 ``snap_px`` 时贴齐。

    ``area`` 是 ``(left, top, right, bottom)``（与 ``_work_area()`` 同形状）。吸附只补
    "差一点"的距离，绝不会把窗口移离用户松手的位置超过 ``snap_px`` 像素；``snap_px<=0``
    或本来就贴边时原样返回（调用方因此可以据此跳过整个缓动）。
    """

    if snap_px <= 0:
        return (x, y)
    left, top, right, bottom = area
    max_x = max(left, right - width)
    max_y = max(top, bottom - height)
    target_x = left if abs(x - left) <= snap_px else max_x if abs(x - max_x) <= snap_px else x
    target_y = top if abs(y - top) <= snap_px else max_y if abs(y - max_y) <= snap_px else y
    return (target_x, target_y)


def _ease_out_cubic(phase: float) -> float:
    """ease-out（三次）：起步快、收尾慢，用于松手吸附的减速手感（0..1 → 0..1）。"""

    clamped = 0.0 if phase < 0.0 else 1.0 if phase > 1.0 else phase
    remaining = 1.0 - clamped
    return 1.0 - remaining * remaining * remaining


def build_render_spec(config: VoiceHudConfig, state: object = None) -> HudRenderSpec:
    """把提示窗配置折算成渲染层规格（外观唯一来源，供像素渲染与真机绘制共用）。"""

    if not isinstance(config, VoiceHudConfig):
        raise TypeError("config 必须是 VoiceHudConfig")
    state_colors = dict(HUD_STATE_COLORS)
    if config.state_colors:
        # 局部覆盖：只替换显式给出的状态，其余仍用冻结四态色。
        state_colors.update(dict(config.state_colors))
    theme = HudTheme(
        background_rgb=config.background_rgb,
        border_rgb=config.border_rgb,
        title_text=config.title,
        title_font_px=config.title_font_px,
        body_font_px=config.body_font_px,
        opacity=config.opacity,
        state_colors=state_colors,
    )
    try:
        return HudRenderSpec(
            surface_width=config.width,
            surface_height=config.height,
            shadow_margin_px=config.shadow_margin_px,
            corner_radius_px=config.corner_radius_px,
            theme=theme,
        )
    except HudRenderError as exc:
        raise VoiceHudError(WIN32_FAILED) from exc


class VoiceHudApi(Protocol):
    """提示窗调用面的窄接口；真实实现由 ``build_win32_api()`` 提供。

    所有方法都可能抛 ``VoiceHudError``（fail closed）：调用方只需知道“显示不可用”，
    不需要也无法从异常里取出任何系统正文。

    ``layered_window_supported`` / ``render_frame`` 是**可选**的逐像素渲染入口：注入的
    替身若未提供它们（例如只实现旧接口），``VoiceStatusHud`` 会自动退回纯色卡片路径，
    行为与 v2 一致。
    """

    def layered_window_supported(self) -> bool:
        """本 api 是否支持逐像素渲染（``UpdateLayeredWindow``）。"""
        ...

    def animations_enabled(self) -> bool:
        """系统"动画效果"是否开启（``SPI_GETCLIENTAREAANIMATION``，批次 C）。"""
        ...

    def render_frame(self, handle: object, *, pixels: bytes, masks: bytes, state: object,
                     title: str, body: str, spec: HudRenderSpec,
                     alpha_scale: int = 255) -> None:
        """把一帧预乘 BGRA 像素与文字一起绘制并提交给分层窗口。

        ``alpha_scale``（0..255，默认 255 = 原样）让这一帧"一出来就是半透明"，供动效使用。
        """
        ...

    def blit_cached_frame(self, handle: object, *, title: str, body: str,
                          alpha_scale: int = 255) -> bool:
        """**可选**动效入口：把已缓存的合成帧按 ``alpha_scale`` 提交；未缓存返回 ``False``。

        未实现该方法的替身会被 ``VoiceStatusHud`` 自动当成"不支持动效"（退回硬切），
        因此旧替身与旧注入方不需要改动。
        """
        ...

    def animations_enabled(self) -> bool:
        """**可选**：系统"动画效果"是否开启（批次 C）；未实现一律按开启处理。"""
        ...

    def create_window(
        self, *, class_name: str, title: str, width: int, height: int, topmost: bool, opacity: int,
        drag_snap_px: int = 0, drag_settle_ms: int = 0,
    ) -> object:
        """创建（但不显示）窗口并返回句柄；不抢焦点、无任务栏按钮。

        ``drag_snap_px`` / ``drag_settle_ms`` 是批次 A 的松手吸附参数，**默认 0 = 关闭**，
        因此只实现旧签名的替身仍然可用（行为与之前一致）。
        """
        ...

    def move_window(self, handle: object, *, x: int, y: int, width: int, height: int) -> None:
        """移动/缩放窗口；真实实现使用 ``SWP_NOACTIVATE``，不抢焦点。"""
        ...

    def set_text(self, handle: object, *, title: str, body: str) -> None:
        """一次性刷新标题与正文。"""
        ...

    def show(self, handle: object) -> None:
        """显示窗口。"""
        ...

    def hide(self, handle: object) -> None:
        """隐藏窗口（不销毁）。"""
        ...

    def pump_messages(self) -> int:
        """非阻塞抽取并派发消息，返回处理条数；不得阻塞等待消息。"""
        ...

    def dragged_offset(self, handle: object) -> tuple[int, int] | None:
        """拖动完成后返回一次新位置，否则返回 ``None``。"""
        ...

    def destroy_window(self, handle: object) -> None:
        """销毁窗口并释放本模块持有的系统资源。"""
        ...


class VoiceStatusHud:
    """右下角状态提示窗；状态与轮次是唯一被渲染的内容来源。

    生命周期：``show()`` 首次调用时建窗（经注入 api，或延迟构造真实 Win32 api），此后
    ``set_state()`` 只在窗口可见时刷新文本；``hide()`` 只隐藏不销毁；``destroy()`` 幂等。
    窗口默认不抢焦点（不调用 ``SetForegroundWindow``）、不显示任务栏按钮、置顶可配、
    可拖动且拖动结果被约束在屏幕内。真实窗口是否开启、是否肉眼可见由真机验收负责，
    本模块与其离线测试都不打开任何真实窗口。
    """

    def __init__(
        self,
        *,
        config: VoiceHudConfig | None = None,
        api: VoiceHudApi | None = None,
        screen: tuple[int, int] | None = None,
    ) -> None:
        if config is None:
            config = VoiceHudConfig()
        elif not isinstance(config, VoiceHudConfig):
            raise TypeError("config 必须是 VoiceHudConfig 或 None")
        if screen is not None and (
            not isinstance(screen, tuple) or len(screen) != 2
            or any(type(side) is not int or side <= 0 for side in screen)
        ):
            raise ValueError("screen 必须是 (宽度, 高度) 正整数二元组")
        self._config = config
        self._api = api
        self._screen = screen
        self._handle: object | None = None
        self._position: tuple[int, int] | None = None
        self._state = VoiceHudState.IDLE
        self._turn_index = 0
        self._title, self._body = hud_text(self._state, turn_index=self._turn_index)
        self._visible = False
        self._destroyed = False
        self._render_spec = build_render_spec(config)
        self._render_mode = MODE_LEGACY
        self._pixel_mode_decided = False
        self._legacy_alpha_applied = False
        # 批次 B：动效能力由 api 是否提供 ``blit_cached_frame`` 决定（None = 尚未探测），
        # 以及"首次 show 才淡入"的一次性标记。
        self._fade_supported: bool | None = None
        self._shown_once = False
        # 防闪护栏用的"上一次真正切换文案"的时刻。
        self._last_state_change_at: float | None = None
        # 动效期间最后一次提交的窗口位置（避免重复下发同一位置）。
        self._last_animated_position: tuple[int, int] | None = None

    # ---------------------------------------------------------------- properties

    @property
    def config(self) -> VoiceHudConfig:
        return self._config

    @property
    def state(self) -> VoiceHudState:
        """最近一次设置的状态；未设置时为 ``IDLE``。"""
        return self._state

    @property
    def turn_index(self) -> int:
        """最近一次设置的轮次序号；未设置时为 ``0``。"""
        return self._turn_index

    @property
    def visible(self) -> bool:
        """窗口当前是否处于显示状态（``hide()`` / ``destroy()`` 后为 False）。"""
        return self._visible

    @property
    def created(self) -> bool:
        """窗口是否已创建且未被销毁（离线测试可据此断言惰性建窗）。"""
        return self._handle is not None

    @property
    def last_text(self) -> tuple[str, str]:
        """最近一次渲染的 ``(标题, 正文)``；只由状态与轮次生成。"""
        return (self._title, self._body)

    @property
    def render_mode(self) -> str:
        """当前渲染模式：``pixel``（浅色卡片胶囊逐像素）或 ``legacy``（纯色降级）。

        未建窗前统一报告 ``legacy``（尚未探测）；建窗时若 api 支持逐像素渲染则升级为
        ``pixel``，此后不再改变（降级只发生一次）。
        """

        return self._render_mode

    @property
    def render_spec(self) -> HudRenderSpec:
        """本次会话使用的渲染规格（外观唯一来源）。"""

        return self._render_spec

    @property
    def window_size(self) -> tuple[int, int]:
        """真实窗口像素尺寸（卡片 + 四周阴影留白）。"""

        spec = self._render_spec
        return (spec.window_width, spec.window_height)

    @property
    def screen(self) -> tuple[int, int]:
        """实际使用的屏幕分辨率；未注入时向 api 查询，查询不可用则用固定回退值。"""
        if self._screen is not None:
            return self._screen
        screen = self._resolve_screen()
        if screen is None:
            return DEFAULT_SCREEN_SIZE
        return screen

    def position(self) -> tuple[int, int]:
        """当前窗口左上角坐标（含阴影留白；卡片可见位置仍保持右下角内缩 ``margin_px``）。"""

        if self._position is None:
            width, height = self.window_size
            return screen_position_for(
                screen=self.screen,
                width=width,
                height=height,
                margin_px=self._config.margin_px,
                pad=self._config.shadow_margin_px,
            )
        return self._position

    # ------------------------------------------------------------------- 生命周期

    def show(self) -> None:
        """显示窗口；首次调用时惰性建窗。销毁后抛 ``VoiceHudError("not_created")``。

        幂等：已显示时重复调用不重复下发指令，也不重复建窗。
        """

        if self._destroyed:
            raise VoiceHudError(NOT_CREATED)
        handle = self._ensure_window()
        if not self._visible:
            first_show = not self._shown_once
            self._shown_once = True
            # 批次 B：首次显示先提交一帧**全透明**再 ShowWindow，窗口就是"从无到有"淡入；
            # 否则 `_ensure_window` 画好的不透明首帧会先闪一下。
            if (
                first_show
                and self._config.show_fade_ms > 0
                and self._animation_supported(self._ensure_api())
                and self._animations_allowed()
                and self._blit_frame(handle, self._title, self._body, 0)
            ):
                self._call(lambda api: api.show(handle))
                self._visible = True
                self._animate_show(handle)
                return
            self._call(lambda api: api.show(handle))
            self._visible = True

    def hide(self) -> None:
        """隐藏窗口（不销毁句柄）；重复调用幂等。销毁后抛 ``VoiceHudError``。

        批次 C：先做 ``hide_fade_ms`` 的淡出+右滑（在本次调用内**内联**跑完）再真正隐藏，
        并把窗口挪回休息位；``hide_fade_ms=0``、api 不支持动效或系统关动画时立即隐藏（旧行为）。
        ``visible`` 语义不变（返回后即 ``False``）。
        """

        if self._destroyed:
            raise VoiceHudError(NOT_CREATED)
        if self._visible and self._handle is not None:
            handle = self._handle
            self._animate_hide(handle)
            self._call(lambda api: api.hide(handle))
        self._visible = False

    def destroy(self) -> None:
        """销毁窗口并释放资源；幂等，重复调用不重复下发指令。

        销毁后 ``tick()`` 返回 ``0``，``set_state()`` / ``show()`` / ``hide()`` 抛
        ``VoiceHudError("not_created")``；状态类属性仍可读。
        """

        if self._destroyed:
            return
        handle = self._handle
        self._destroyed = True
        self._visible = False
        self._handle = None
        if handle is not None:
            self._call(lambda api: api.destroy_window(handle))

    def set_state(self, state: VoiceHudState, *, turn_index: int | None = None) -> str:
        """更新状态并刷新窗口文本，返回该状态的固定中文文案。

        ``turn_index`` 仍会被校验与记录（``turn_index`` 属性可读），但简化后不再渲染到
        窗口正文；可见文案只有 ``待机`` / ``聆听`` / ``播报`` / ``失败`` 四种。窗口
        可见时才向 api 下发 ``set_text``（已创建但被隐藏时不显示）与 ``move_window``
        （保持右下角贴边约束）。销毁后抛 ``VoiceHudError("not_created")``。
        """

        if self._destroyed:
            raise VoiceHudError(NOT_CREATED)
        if not isinstance(state, VoiceHudState):
            raise ValueError("state 必须是 VoiceHudState")
        if turn_index is not None and (type(turn_index) is not int or turn_index < 0):
            raise ValueError("turn_index 必须是非负整数或 None")
        if turn_index is not None:
            self._turn_index = turn_index
        previous = (self._title, self._body)
        self._state = state
        self._title, self._body = hud_text(self._state, turn_index=self._turn_index)
        if self._visible and self._handle is not None:
            handle = self._handle
            title, body = self._title, self._body
            # 批次 B：状态文案变化时走"旧帧淡到最低点 → 新帧淡入"的软过渡（内联 ~140 ms）。
            drawn = self._transition_frame(handle, previous)
            if not drawn:
                self._call(lambda api_ref: api_ref.set_text(handle, title=title, body=body))
            self._apply_drag(handle)
        return state_label(self._state)

    def tick(self) -> int:
        """非阻塞抽消息并处理拖动，返回处理条数；未建窗或已销毁时返回 ``0``。

        不使用阻塞式 ``GetMessageW``：真实实现只做 ``PeekMessageW``（``PM_REMOVE``）。
        api 抛错时 fail closed（``VoiceHudError``），不吞掉失败。
        """

        if self._destroyed or self._handle is None:
            return 0
        api = self._ensure_api()
        handle = self._handle
        try:
            count = api.pump_messages()
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - api 失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc
        if type(count) is not int or count < 0:
            raise VoiceHudError(WIN32_FAILED)
        if self._visible:
            self._apply_drag(handle)
        return count

    # -------------------------------------------------------------------- 内部实现

    def _ensure_api(self) -> VoiceHudApi:
        if self._api is None:
            self._api = build_win32_api()
        return self._api

    def _ensure_window(self) -> object:
        if self._handle is not None:
            return self._handle
        api = self._ensure_api()
        spec = self._render_spec
        width, height = spec.window_width, spec.window_height
        try:
            handle = api.create_window(
                class_name=WINDOW_CLASS_NAME,
                title=self._config.title,
                width=width,
                height=height,
                topmost=self._config.topmost,
                opacity=self._config.opacity,
                drag_snap_px=self._config.drag_snap_px,
                drag_settle_ms=self._config.drag_settle_ms,
            )
            if handle is None:
                raise VoiceHudError(WIN32_FAILED)
            x, y = screen_position_for(
                screen=self.screen,
                width=width,
                height=height,
                margin_px=self._config.margin_px,
                pad=self._config.shadow_margin_px,
            )
            api.move_window(handle, x=x, y=y, width=width, height=height)
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - 建窗失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc
        self._handle = handle
        self._position = (x, y)
        # 能力探测必须在建窗之后：真实 api 的逐像素能力依赖已创建的窗口句柄。
        mode = self._decide_render_mode(api)
        if mode == MODE_LEGACY:
            self._use_legacy_alpha()
        # 首帧：逐像素模式直接画整帧；降级模式沿用 v2 的纯色卡片 + 文本。
        if not self._draw_frame(handle):
            self._call(lambda api_ref: api_ref.set_text(handle, title=self._title,
                                                       body=self._body))
        return handle

    def _use_legacy_alpha(self) -> None:
        """降级路径补一次整窗不透明度（幂等；逐像素路径绝不调用）。"""

        if self._legacy_alpha_applied:
            return
        api = self._ensure_api()
        handle = self._handle
        if handle is None:
            return
        setter = getattr(api, "apply_layered_alpha", None)
        if callable(setter):
            self._call(lambda api_ref: api_ref.apply_layered_alpha())
        self._legacy_alpha_applied = True

    def _decide_render_mode(self, api: VoiceHudApi) -> str:
        """决定本次会话的渲染模式（只探测一次，结果不再变化）。"""

        if self._pixel_mode_decided:
            return self._render_mode
        self._pixel_mode_decided = True
        self._render_mode = MODE_LEGACY
        supported = getattr(api, "layered_window_supported", None)
        renderer = getattr(api, "render_frame", None)
        if callable(supported) and callable(renderer):
            try:
                if supported():
                    self._render_mode = MODE_PIXEL
            except Exception:  # noqa: BLE001 - 探测失败即降级，不影响显示
                self._render_mode = MODE_LEGACY
        return self._render_mode

    def _draw_frame(self, handle: object, *, alpha_scale: int = 255) -> bool:
        """用逐像素渲染刷新一帧；不可用或失败时返回 ``False``（由调用方走降级路径）。

        降级只在第一次失败时发生：此后 ``_render_mode`` 固定为 ``legacy``，不再重复
        尝试逐像素渲染，也不会把异常抛给语音链路。

        批次 B：``alpha_scale < 255`` 时这一帧**一出来就是半透明**（用于让新状态从"最低点"
        淡入，避免先闪一下全不透明的新帧）。替身若不接受该参数，会退回不带它的调用并**永久
        关闭动效**，绝不因此把渲染模式降级为 legacy。
        """

        if self._render_mode != MODE_PIXEL:
            self._use_legacy_alpha()
            return False
        api = self._ensure_api()
        spec = self._render_spec
        try:
            pixels = render_card_pixels(spec, self._state)
            masks = paint_layer_masks(spec, self._state)
            self._render_frame_call(api, handle, pixels, masks, spec, alpha_scale)
        except Exception:  # noqa: BLE001 - 逐像素渲染失败一律降级，不打断语音链路
            self._render_mode = MODE_LEGACY
            self._use_legacy_alpha()
            return False
        return True

    def _render_frame_call(self, api: VoiceHudApi, handle: object, pixels: bytes, masks: bytes,
                           spec: HudRenderSpec, alpha_scale: int) -> None:
        """调用 api 的 ``render_frame``；动效参数不被接受时退回普通调用并关闭动效。"""

        kwargs = {
            "pixels": pixels,
            "masks": masks,
            "state": self._state,
            "title": self._title,
            "body": self._body,
            "spec": spec,
        }
        if alpha_scale < 255 and self._animation_supported(api):
            try:
                api.render_frame(handle, alpha_scale=alpha_scale, **kwargs)  # type: ignore[call-arg]
                return
            except TypeError:
                # 替身只实现了旧签名：永久关闭动效，但仍按旧签名渲染（不降级为 legacy）。
                self._fade_supported = False
        api.render_frame(handle, **kwargs)  # type: ignore[arg-type]

    def _animation_supported(self, api: VoiceHudApi) -> bool:
        """api 是否支持动效（提供 ``blit_cached_frame``）；结果只探测一次。"""

        if self._fade_supported is None:
            self._fade_supported = callable(getattr(api, "blit_cached_frame", None))
        return bool(self._fade_supported)

    def _blit_frame(self, handle: object, title: str, body: str, alpha_scale: int) -> bool:
        """按整窗 alpha 提交已缓存的合成帧；不支持、未命中或已降级时返回 ``False``。"""

        if self._render_mode != MODE_PIXEL:
            # 降级路径没有逐像素帧，自然也没有可提交的缓存帧。
            return False
        api = self._ensure_api()
        blit = getattr(api, "blit_cached_frame", None)
        if not callable(blit):
            return False
        try:
            return bool(blit(handle, title=title, body=body, alpha_scale=alpha_scale))
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - api 失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc

    def _transition_frame(self, handle: object, previous: tuple[str, str]) -> bool:
        """批次 B：把"旧帧 → 最低点 → 新帧"这段过渡**内联**跑完；返回是否走了逐像素路径。

        顺序刻意如此：先把旧帧提交成全不透明（确保缓存里就是它）→ 旧帧淡到最低点 →
        新帧**以最低点 alpha 一次性画出来**（同时也进了缓存，不会先闪一下全不透明）→ 新帧淡入。

        **默认不会走到这里**：``state_animation_ms`` 默认 0（真机反馈该脉冲会被看成闪烁），
        而且距上次切换不足 :data:`_STATE_SWITCH_GUARD_S` 时也会跳过（连续两段过渡 = 闪两下）。
        跳过后退化为一次普通渲染，输出与批次 B 之前逐字节一致。
        """

        changed = previous != (self._title, self._body)
        now = time.monotonic()
        recent = (
            self._last_state_change_at is not None
            and now - self._last_state_change_at < _STATE_SWITCH_GUARD_S
        )
        if changed:
            self._last_state_change_at = now
        animation = (
            changed
            and not recent
            and self._config.state_animation_ms > 0
            and self._animation_supported(self._ensure_api())
            and self._animations_allowed()
        )
        if not animation:
            return self._draw_frame(handle)
        if not self._blit_frame(handle, previous[0], previous[1], 255):
            # 旧帧没有缓存（首次切换前的 legacy/降级路径）：直接渲染新帧。
            return self._draw_frame(handle)
        total_s = self._config.state_animation_ms / 1000.0
        out_s = total_s * 0.45
        in_s = total_s - out_s
        self._run_alpha_ramp(handle, previous[0], previous[1], 255, _ANIMATION_DIP_ALPHA, out_s)
        if not self._draw_frame(handle, alpha_scale=_ANIMATION_DIP_ALPHA):
            return False
        self._run_alpha_ramp(handle, self._title, self._body, _ANIMATION_DIP_ALPHA, 255, in_s)
        return True

    def _animations_allowed(self) -> bool:
        """配置允许 **且**（按需）系统允许时，动效才生效（批次 C）。

        每次动效前都重新探测系统设置：用户中途改"动画效果"能立刻生效；探测失败一律按开启
        （fail-open），绝不让装饰性能力影响显示与状态语义。
        """

        if not self._config.respect_system_animation:
            return True
        api = self._ensure_api()
        probe = getattr(api, "animations_enabled", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - 探测失败不得影响显示
            return True

    def _move_for_animation(self, handle: object, position: tuple[int, int]) -> None:
        """动效期间的窗口位移（不更新 ``_position``：休息位才是它的语义）。"""

        if position == self._last_animated_position:
            return
        width, height = self.window_size
        self._call(
            lambda api: api.move_window(
                handle, x=position[0], y=position[1], width=width, height=height
            )
        )
        self._last_animated_position = position

    def _run_transition_ramp(self, handle: object, title: str, body: str, *,
                             alpha_from: int, alpha_to: int,
                             duration_s: float,
                             position_from: tuple[int, int] | None = None,
                             position_to: tuple[int, int] | None = None) -> None:
        """在 ``duration_s`` 内把整窗 alpha（以及可选的位置）用 **ease-out** 推到目标。

        曲线与业界一致（sonner/react-hot-toast 都是 ease-out "先快后慢"）。
        """

        if duration_s <= 0:
            self._blit_frame(handle, title, body, alpha_to)
            if position_to is not None:
                self._move_for_animation(handle, position_to)
            return
        started = time.monotonic()
        while True:
            phase = (time.monotonic() - started) / duration_s
            if phase >= 1.0:
                break
            eased = _ease_out_cubic(0.0 if phase < 0.0 else phase)
            alpha = int(round(alpha_from + (alpha_to - alpha_from) * eased))
            self._blit_frame(handle, title, body, max(0, min(255, alpha)))
            if position_from is not None and position_to is not None:
                self._move_for_animation(handle, (
                    int(round(position_from[0] + (position_to[0] - position_from[0]) * eased)),
                    int(round(position_from[1] + (position_to[1] - position_from[1]) * eased)),
                ))
            time.sleep(0.008)
        self._blit_frame(handle, title, body, alpha_to)
        if position_to is not None:
            self._move_for_animation(handle, position_to)

    def _run_alpha_ramp(self, handle: object, title: str, body: str,
                        start: int, end: int, duration_s: float) -> None:
        """状态过渡用的纯 alpha 渐变（不动位置）。"""

        self._run_transition_ramp(
            handle, title, body, alpha_from=start, alpha_to=end, duration_s=duration_s
        )

    def _animate_show(self, handle: object) -> None:
        """首次显示：淡入 + 从右侧滑入（批次 C，对齐业界 toast 的 translate+opacity 进场）。"""

        duration_s = self._config.show_fade_ms / 1000.0
        if duration_s <= 0 or not self._animation_supported(self._ensure_api()):
            return
        if not self._animations_allowed():
            # 系统关动画：什么都不做——窗口里已经是 `_ensure_window` 画好的不透明帧。
            return
        resting = self._position
        if resting is None:
            return
        slide = max(0, self._config.show_slide_px)
        start_position = (resting[0] + slide, resting[1]) if slide else None
        if start_position is not None:
            self._move_for_animation(handle, start_position)
        self._run_transition_ramp(
            handle, self._title, self._body,
            alpha_from=0, alpha_to=255, duration_s=duration_s,
            position_from=start_position, position_to=resting if start_position else None,
        )

    def _animate_hide(self, handle: object) -> None:
        """退场：淡出 + 向右滑出（批次 C），跑完把窗口挪回休息位再隐藏。"""

        duration_s = self._config.hide_fade_ms / 1000.0
        if duration_s <= 0 or not self._animation_supported(self._ensure_api()):
            return
        if not self._animations_allowed():
            # 系统关动画：不提交任何过渡帧，直接让调用方隐藏窗口。
            return
        resting = self._position
        if resting is None:
            return
        slide = max(0, self._config.show_slide_px)
        end_position = (resting[0] + slide, resting[1]) if slide else None
        self._run_transition_ramp(
            handle, self._title, self._body,
            alpha_from=255, alpha_to=0, duration_s=duration_s,
            position_from=resting if end_position else None, position_to=end_position,
        )
        # 归位：下一次显示仍从休息位滑入，且窗口不会"留在屏幕外"。
        self._move_for_animation(handle, resting)

    def _apply_drag(self, handle: object) -> bool:
        """读取拖动结果并按屏幕约束移动窗口；无拖动时返回 False。"""

        api = self._ensure_api()
        try:
            offset = api.dragged_offset(handle)
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - api 失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc
        if offset is None:
            return False
        if (not isinstance(offset, tuple) or len(offset) != 2
                or any(type(value) is not int for value in offset)):
            raise VoiceHudError(WIN32_FAILED)
        width, height = self.window_size
        # 批次 A：拖动结果**不再**按贴边 `margin_px` 二次内缩。`_track_drag` 已经按工作区
        # 钳制过位置，这里再用 `margin_px=24` 复钳会把窗口在松手瞬间又拉进最多 24 px
        # （真机"松手跳一下"）。`margin_px=0` 仍然保证窗口不出屏，只是不再额外内缩。
        x, y = clamp_position(
            x=offset[0], y=offset[1], screen=self.screen,
            width=width, height=height,
            margin_px=0,
        )
        try:
            api.move_window(handle, x=x, y=y, width=width, height=height)
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - api 失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc
        self._position = (x, y)
        return True

    def _resolve_screen(self) -> tuple[int, int] | None:
        """向 api 查询主屏分辨率；查询不可用时返回 None（由调用方回退）。"""

        api = self._api
        probe = getattr(api, "screen_size", None)
        if api is None or not callable(probe):
            return None
        try:
            size = probe()
        except Exception:  # noqa: BLE001 - 查询失败即回退，不影响状态显示
            return None
        if (isinstance(size, tuple) and len(size) == 2
                and all(type(side) is int and side > 0 for side in size)):
            return size
        return None

    def _call(self, action: object) -> None:
        """执行一次 api 调用并把任何失败收敛为 ``VoiceHudError("win32_failed")``。"""

        try:
            action(self._ensure_api())  # type: ignore[operator]
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - api 失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc


def build_win32_api() -> VoiceHudApi:
    """构造真实 Win32 提示窗实现；非 Windows 或 user32/gdi32 不可用时 fail closed。

    失败一律抛固定短代码 ``VoiceHudError("unsupported_platform" / "win32_failed")``，
    不把 ``OSError`` 正文、DLL 名或路径带出模块。构造函数本身不建窗、不显示任何窗口；
    真实窗口只会在 ``show()`` 首次调用时创建。
    """

    if sys.platform != "win32":
        raise VoiceHudError(UNSUPPORTED_PLATFORM)
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    except Exception as exc:  # noqa: BLE001 - 依赖加载失败必须 fail closed
        raise VoiceHudError(WIN32_FAILED) from exc
    try:
        _configure_user32(user32)
        _configure_user32_layered(user32)
        _configure_gdi32(gdi32)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    except Exception as exc:  # noqa: BLE001 - 签名配置失败必须 fail closed
        raise VoiceHudError(WIN32_FAILED) from exc
    return Win32VoiceHudApi(user32=user32, gdi32=gdi32, kernel32=kernel32)


class Win32VoiceHudApi:
    """``VoiceHudApi`` 的真实实现：无边框分层窗口 + GDI 自绘 + ``PeekMessageW`` 抽消息。

    结构体（``WNDCLASSW`` / ``POINT`` / ``MSG`` / ``PAINTSTRUCT`` / ``RECT`` /
    ``WNDPROC``）全部按项目 ``app/tts/waveout_pcm.py`` 的风格用 ``ctypes.Structure``
    手写；不引入任何新依赖。构造只加载 DLL 与配置函数签名，不创建窗口。
    """

    screen_fallback_size = (0, 0)
    """屏幕上取不到分辨率时交给调用方回退（``(0, 0)`` 表示不可用）。"""

    def __init__(self, *, user32: object, gdi32: object, kernel32: object) -> None:
        self._user32 = user32
        self._gdi32 = gdi32
        self._kernel32 = kernel32
        self._handle: int | None = None
        self._instance: int | None = None
        self._font: int | None = None
        self._registered = False
        self._class_atom = 0
        self._class_name = WINDOW_CLASS_NAME
        self._title = TITLE_TEXT
        self._body = ""
        self._width = 0
        self._height = 0
        self._opacity = 255
        self._margin_px = 0
        self._font_px = 15
        self._draggable = True
        self._background = (0, 0, 0)
        self._foreground = (255, 255, 255)
        self._accent = (255, 255, 255)
        self._dragging = False
        self._drag_origin = (0, 0)
        self._drag_window_origin = (0, 0)
        self._drag_snap_px = 0
        self._drag_settle_ms = 0
        self._dragged_position: tuple[int, int] | None = None
        self._last_position: tuple[int, int] | None = None
        self._procedure = _WNDPROC(self._wnd_proc)
        self._wnd_class = None
        self._dib_handle: int | None = None
        self._memory_dc: int | None = None
        self._dib_pointer: int | None = None
        self._pixel_size: tuple[int, int] | None = None
        # 批次 B：已含文字的合成帧缓存（键 = (title, body)），动效帧只做整窗 alpha 缩放。
        self._composed_frames: dict[tuple[str, str], bytes] = {}

    # --------------------------------------------------------------- VoiceHudApi

    def animations_enabled(self) -> bool:
        """系统"动画效果"开关（``SPI_GETCLIENTAREAANIMATION``）；读不到按开启处理。"""

        return _system_animations_enabled(self._user32)

    def create_window(
        self, *, class_name: str, title: str, width: int, height: int, topmost: bool, opacity: int,
        drag_snap_px: int = 0, drag_settle_ms: int = 0,
    ) -> object:
        """注册窗口类并创建分层、置顶、无任务栏按钮、不抢焦点的弹窗，返回句柄。"""

        if self._handle is not None:
            raise VoiceHudError(WIN32_FAILED)
        self._class_name = class_name
        self._title = title
        self._pixel_size = (int(width), int(height))
        self._width = int(width)
        self._height = int(height)
        self._opacity = int(opacity)
        # 批次 A：松手吸附参数（0 = 关闭，行为与之前一致）。
        self._drag_snap_px = max(0, int(drag_snap_px))
        self._drag_settle_ms = max(0, int(drag_settle_ms))
        self._ensure_class()
        instance = self._module_instance()
        x, y = self._bottom_right(self._work_area())
        extra = _WS_EX_TOOLWINDOW | _WS_EX_LAYERED | _WS_EX_NOACTIVATE
        if topmost:
            extra |= _WS_EX_TOPMOST
        # 类名以字符串传给 ``CreateWindowExW``（``argtypes`` 精确匹配 ``c_wchar_p``）；
        # 类原子只用于离线核对注册是否成功，不用于建窗，避免 ``CreateWindowExW``
        # 在类名解析失败时静默返回 NULL。
        handle = self._call(
            lambda: self._user32.CreateWindowExW(
                extra, self._class_name, self._title, _WS_POPUP,
                x, y, self._width, self._height, 0, 0, instance, None,
            )
        )
        if not handle:
            raise VoiceHudError(WIN32_FAILED)
        self._handle = handle
        self._last_position = (x, y)
        # 注意：这里**不**调用 SetLayeredWindowAttributes。逐像素路径要用
        # UpdateLayeredWindow，而 Windows 不允许二者混用（实测混用会让
        # UpdateLayeredWindow 失败并返回 ERROR_INVALID_PARAMETER=87）。整窗 alpha 由
        # VoiceStatusHud 在确定走降级路径后调用 apply_layered_alpha() 补上。
        return handle

    def move_window(self, handle: object, *, x: int, y: int, width: int, height: int) -> None:
        """用 ``SWP_NOACTIVATE`` 移动/缩放，绝不抢占前台焦点。"""

        self._call(
            lambda: self._user32.SetWindowPos(
                handle, 0, int(x), int(y), int(width), int(height), _SWP_NOACTIVATE
            )
        )
        self._last_position = (int(x), int(y))

    def set_text(self, handle: object, *, title: str, body: str) -> None:
        """保存标题与正文并在窗口可见时请求重绘；正文只由状态与轮次生成。"""

        self._title = title
        self._body = body
        self._call(lambda: self._user32.SetWindowTextW(handle, title))
        if self._last_position is not None:
            self.invalidate()

    def show(self, handle: object) -> None:
        self._call(lambda: self._user32.ShowWindow(handle, _SW_SHOWNOACTIVATE))
        # 显示后立即同步重绘首帧；否则窗口要等下一次系统消息才绘制（真机表现为延迟出现）。
        self.invalidate()

    def hide(self, handle: object) -> None:
        self._call(lambda: self._user32.ShowWindow(handle, _SW_HIDE))

    def pump_messages(self) -> int:
        """非阻塞抽取（``PM_REMOVE``）并派发消息，返回本批处理条数；无消息立即返回 0。"""

        message = _Msg()
        processed = 0
        while self._call(
            lambda: self._user32.PeekMessageW(
                ctypes.byref(message), 0, 0, 0, _PM_REMOVE
            )
        ):
            self._call(lambda: self._user32.TranslateMessage(ctypes.byref(message)))
            self._call(lambda: self._user32.DispatchMessageW(ctypes.byref(message)))
            processed += 1
        return processed

    def dragged_offset(self, handle: object) -> tuple[int, int] | None:
        """拖动结束后返回一次新位置，随后返回 ``None``（未拖动时也返回 None）。"""

        position = self._dragged_position
        self._dragged_position = None
        return position

    def destroy_window(self, handle: object) -> None:
        """销毁窗口：结束拖动定时器、``DestroyWindow``，再释放 DIB/内存 DC/字体/窗口类。"""

        self._call(lambda: self._user32.KillTimer(handle, _WM_LBUTTONDOWN_ID))
        self._call(lambda: self._user32.DestroyWindow(handle))
        self._handle = None
        self._release_frame_resources()
        if self._font is not None:
            self._call(lambda: self._gdi32.DeleteObject(self._font))
            self._font = None
        if self._registered and self._wnd_class is not None:
            self._call(
                lambda: self._user32.UnregisterClassW(
                    self._class_name, self._module_instance()
                )
            )
            self._registered = False
            self._wnd_class = None

    def _release_frame_resources(self) -> None:
        """释放逐像素渲染的 DIB 与内存 DC（幂等；未创建时为空操作）。"""

        if self._memory_dc is not None:
            self._call(lambda: self._gdi32.DeleteDC(self._memory_dc))
            self._memory_dc = None
        if self._dib_handle is not None:
            self._call(lambda: self._gdi32.DeleteObject(self._dib_handle))
            self._dib_handle = None
        self._dib_pointer = None

    def screen_size(self) -> tuple[int, int]:
        """主屏分辨率；取不到时返回 ``(0, 0)`` 交由调用方回退，不抛错。"""
        try:
            width = int(self._user32.GetSystemMetrics(_SM_CXSCREEN))
            height = int(self._user32.GetSystemMetrics(_SM_CYSCREEN))
        except Exception:  # noqa: BLE001 - 探测失败由调用方回退固定分辨率
            return (0, 0)
        if width <= 0 or height <= 0:
            return (0, 0)
        return (width, height)

    def invalidate(self) -> None:
        """请求一次重绘并立即同步刷新（``InvalidateRect`` + ``UpdateWindow``）。

        真机缺陷修复：只调 ``InvalidateRect`` 时绘制要等下一次系统消息，窗口会"迟到"
        且内容停留在旧帧；``UpdateWindow`` 让 ``WM_PAINT`` 立即派发。
        """

        if self._handle is None:
            return
        self._call(lambda: self._user32.InvalidateRect(self._handle, None, True))
        self._call(lambda: self._user32.UpdateWindow(self._handle))

    # ------------------------------------------------- 逐像素渲染（v3）

    def layered_window_supported(self) -> bool:
        """本实现是否具备逐像素分层窗口能力（句柄已建且导出可用）。"""

        if self._handle is None:
            return False
        update = getattr(self._user32, "UpdateLayeredWindow", None)
        fill = getattr(self._user32, "FillRect", None)
        return callable(update) and callable(fill)

    def render_frame(self, handle: object, *, pixels: bytes, masks: bytes, state: object,
                     title: str, body: str, spec: object, alpha_scale: int = 255) -> None:
        """把整帧像素与文字绘制进 DIB，并用 ``UpdateLayeredWindow`` 一次性提交。

        任何一步失败都收敛为 ``VoiceHudError("win32_failed")``，由调用方降级为纯色卡片。

        批次 B：合成完成后先把**未缩放**的整帧回读进 ``_composed_frames``（动效帧缓存），
        再按 ``alpha_scale``（0..255，默认 255 = 原样）缩放提交。传小于 255 的值就能让
        这一帧"一出来就是半透明"，避免动效切换时先闪一下全不透明的新帧。
        """

        width, height = self._ensure_dib(handle)
        source = self._dib_pointer
        memory_dc = self._memory_dc
        if source is None or memory_dc is None:
            raise VoiceHudError(WIN32_FAILED)
        expected = width * height * 4
        if not isinstance(pixels, bytes) or len(pixels) != expected:
            raise VoiceHudError(WIN32_FAILED)
        self._write_pixels(source, pixels, masks, width * height, spec)
        try:
            self._draw_frame_text(memory_dc, width, height, spec, state, title, body)
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - GDI 文字失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc
        self._remember_composed_frame(title, body)
        self._apply_alpha_and_submit(handle, alpha_scale)

    def _submit_frame(self, handle: object) -> None:
        """把 DIB 当前内容提交给分层窗口；``pptDst`` 传 NULL 保持窗口位置。"""

        width, height = self._pixel_size if self._pixel_size is not None else (0, 0)
        blend = _BlendFunction(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        size = _Size(width, height)
        origin = _Point(0, 0)
        # pptDst 传 NULL：保持窗口当前位置，不因逐帧重绘把窗口挪到屏幕左上角。
        result = self._call(
            lambda: self._user32.UpdateLayeredWindow(
                handle, 0, None, ctypes.byref(size), self._memory_dc,
                ctypes.byref(origin), 0, ctypes.byref(blend), _ULW_ALPHA,
            )
        )
        if not result:
            raise VoiceHudError(WIN32_FAILED)

    def _remember_composed_frame(self, title: str, body: str) -> None:
        """回读刚合成完的 DIB，缓存成该 ``(title, body)`` 的动效帧（批次 B）。

        只缓存"已经画好文字"的最终帧，因此动效每帧只需一次 ``memmove`` + 一次字节缩放，
        不必重跑 37k 像素的合成循环与 GDI 文字。缓存失败（例如 DIB 尚未建立）一律安静跳过，
        **绝不影响显示**；条目上限只是内存护栏（可见文案组合远小于它）。
        """

        pointer = self._dib_pointer
        if pointer is None or self._pixel_size is None:
            return
        width, height = self._pixel_size
        try:
            self._composed_frames[(title, body)] = ctypes.string_at(pointer, width * height * 4)
        except Exception:  # noqa: BLE001 - 缓存失败不得影响显示
            return
        if len(self._composed_frames) > _COMPOSED_FRAME_CACHE_LIMIT:
            self._composed_frames.clear()

    def _apply_alpha_and_submit(self, handle: object, alpha_scale: int) -> None:
        """按整窗 alpha 缩放 DIB（预乘 BGRA 四通道等比）后提交。"""

        if type(alpha_scale) is not int or not 0 <= alpha_scale <= 255:
            raise VoiceHudError(WIN32_FAILED)
        if alpha_scale < 255:
            pointer = self._dib_pointer
            if pointer is None or self._pixel_size is None:
                raise VoiceHudError(WIN32_FAILED)
            width, height = self._pixel_size
            size = width * height * 4
            scaled = ctypes.string_at(pointer, size).translate(_alpha_scale_table(alpha_scale))
            ctypes.memmove(pointer, scaled, size)
        self._submit_frame(handle)

    def blit_cached_frame(self, handle: object, *, title: str, body: str,
                          alpha_scale: int = 255) -> bool:
        """命中动效帧缓存则按 ``alpha_scale`` 提交，未命中返回 ``False``（批次 B）。"""

        frame = self._composed_frames.get((title, body))
        pointer = self._dib_pointer
        if frame is None or pointer is None or self._memory_dc is None:
            return False
        if type(alpha_scale) is not int or not 0 <= alpha_scale <= 255:
            raise VoiceHudError(WIN32_FAILED)
        ctypes.memmove(pointer, frame, len(frame))
        self._apply_alpha_and_submit(handle, alpha_scale)
        return True

    def _ensure_dib(self, handle: object) -> tuple[int, int]:
        """惰性创建与窗口同尺寸的 32 位 top-down DIB + 内存 DC，返回 ``(宽, 高)``。"""

        if self._pixel_size is None:
            raise VoiceHudError(WIN32_FAILED)
        width, height = self._pixel_size
        if self._dib_handle is not None:
            return (width, height)
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(_BitmapInfoHeader)
        header.biWidth = width
        header.biHeight = -height  # 负数 = top-down，与渲染缓冲行序一致
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB
        info = _BitmapInfo()
        info.bmiHeader = header
        info.bmiColors[0] = 0
        pointer = ctypes.c_void_p()
        dib = self._call(
            lambda: self._gdi32.CreateDIBSection(
                0, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(pointer), 0, 0
            )
        )
        if not dib or not pointer:
            raise VoiceHudError(WIN32_FAILED)
        memory_dc = self._call(lambda: self._gdi32.CreateCompatibleDC(0))
        if not memory_dc:
            raise VoiceHudError(WIN32_FAILED)
        previous = self._call(lambda: self._gdi32.SelectObject(memory_dc, dib))
        if not previous or previous == _GDI_ERROR:
            raise VoiceHudError(WIN32_FAILED)
        self._dib_handle = int(dib)
        self._memory_dc = int(memory_dc)
        self._dib_pointer = int(pointer.value or 0)
        if self._dib_pointer == 0:
            raise VoiceHudError(WIN32_FAILED)
        return (width, height)

    def _write_pixels(self, destination: int, pixels: bytes, masks: bytes,
                      pixel_count: int, spec: object) -> None:
        """把预乘 BGRA 写进 DIB，并按图层分级掩码归一化 alpha。

        GDI 文字会直接把颜色写进 DIB 并混入抗锯齿，这里按图层分级先归一化 alpha：

        - 卡片/色条/胶囊：只把 alpha 写回 255（保留已与底色混合好的颜色）；
        - 文字矩形（标题胶囊 / 正文）内：同样写回 255，让 GDI 文字在**不透明底色**上做
          正常抗锯齿混合，这样字形边缘颜色正确、alpha 也保持不透明；
        - 柔和阴影：原样保留半透明 alpha（否则阴影会变成纯黑描边）；
        - 其余完全透明像素：保持透明（否则窗口留白会变成黑边）。
        """

        if not isinstance(masks, bytes) or len(masks) != pixel_count:
            raise VoiceHudError(WIN32_FAILED)
        title_rect = spec.title_chip_rect()
        body_rect = spec.body_rect()
        width = spec.window_width
        ctypes.memmove(destination, pixels, len(pixels))
        offset = 3
        pixel = 0
        for mask in masks:
            if mask > LAYER_SHADOW:
                raise VoiceHudError(WIN32_FAILED)
            if mask in (LAYER_CARD, LAYER_BAR, LAYER_CHIP):
                ctypes.memset(destination + offset, 255, 1)
            elif mask == LAYER_OUTSIDE and _in_text_area(pixel % width, pixel // width,
                                                        title_rect, body_rect):
                ctypes.memset(destination + offset, 255, 1)
            offset += 4
            pixel += 1

    def _draw_frame_text(self, memory_dc: int, width: int, height: int,
                         spec: object, state: object, title: str, body: str) -> None:
        """在内存 DC 上叠加标题与正文（文字由调用方传入，颜色只由状态推导）。

        注意：``self`` 是 api 实例，它自己也有 ``_title`` / ``_body``（旧 ``set_text`` 用的
        缓存），**不能**在这里读 ``self._title``，否则正文会永远画不出来。
        """

        theme = spec.theme
        source = self._dib_pointer
        if source is None:
            raise VoiceHudError(WIN32_FAILED)
        title_left, title_top, title_right, title_bottom = spec.title_chip_rect()
        title_rect = _Rect(title_left, title_top, min(title_right, width),
                           min(title_bottom, height))
        body_left, body_top, body_right, body_bottom = spec.body_rect()
        body_rect = _Rect(body_left, body_top, min(body_right, width), min(body_bottom, height))
        font = self._ensure_font()
        self._call(lambda: self._gdi32.SelectObject(memory_dc, font))
        self._gdi32.SetBkMode(memory_dc, _TRANSPARENT_BKGMODE)
        self._call(lambda: self._gdi32.SetBkColor(memory_dc, 0))
        # 状态名由状态枚举推导；这里只用于取固定色，绝不回显任何外部文本。
        state_name = _state_value(state)
        # GDI 文字会把字形像素写成「正确颜色 + alpha=0」；预乘语意下 alpha=0 就是全透明，
        # 因此画完必须把矩形内发生变化的像素 alpha 补回 255，否则文字会变成浅色残影。
        snapshot = _snapshot_rects(source, width, (title_rect, body_rect))
        self._draw_text(memory_dc, body, body_rect)
        self._gdi32.SetTextColor(memory_dc, _colorref(theme.title_text_color_for(state_name)))
        self._draw_text(memory_dc, title, title_rect)
        # 批量绘制后刷新 GDI，保证 DIB 位已落盘再读回来修 alpha。
        self._call(lambda: self._gdi32.GdiFlush())
        _restore_text_alpha(source, width, (title_rect, body_rect), snapshot)

    # -------------------------------------------------------------------- 内部实现

    def _module_instance(self) -> int:
        if self._instance is None:
            self._instance = self._call(
                lambda: self._kernel32.GetModuleHandleW(None)
            ) or 0
        return int(self._instance)

    def _ensure_class(self) -> None:
        if self._registered:
            return
        instance = self._module_instance()
        existing = _WndClassW()
        if self._call(
            lambda: self._user32.GetClassInfoW(instance, self._class_name, ctypes.byref(existing))
        ):
            # 类已由本进程更早的一次注册存在：直接复用，不重复 RegisterClassW。
            self._registered = True
            return
        # hbrBackground=0：分层窗口自身绘制背景，避免系统擦除时闪白。
        wnd_class = _WndClassW()
        wnd_class.lpfnWndProc = ctypes.cast(self._procedure, _WNDPROC_POINTER)
        wnd_class.hInstance = instance
        wnd_class.lpszClassName = self._class_name
        self._wnd_class = wnd_class
        atom = self._call(lambda: self._user32.RegisterClassW(ctypes.byref(wnd_class)))
        if not atom:
            raise VoiceHudError(WIN32_FAILED)
        self._registered = True
        self._class_atom = int(atom)

    def _apply_layered(self) -> None:
        handle = self._handle
        if handle is None:
            return
        alpha = max(0, min(255, self._opacity))
        self._call(
            lambda: self._user32.SetLayeredWindowAttributes(
                handle, 0, alpha, _LWA_ALPHA
            )
        )

    def apply_layered_alpha(self) -> None:
        """回退到纯色卡片路径时补上整窗不透明度（``SetLayeredWindowAttributes``）。

        逐像素路径不能调用它（与 ``UpdateLayeredWindow`` 冲突），因此只在降级路径上按需
        调用一次；重复调用是幂等的（幂等由调用方 ``VoiceStatusHud`` 的状态位保证）。
        """

        self._apply_layered()

    def _work_area(self) -> tuple[int, int, int, int]:
        rectangle = _Rect()
        result = self._call(
            lambda: self._user32.SystemParametersInfoW(
                _SPI_GETWORKAREA, 0, ctypes.byref(rectangle), 0
            )
        )
        if not result:
            width = int(self._user32.GetSystemMetrics(_SM_CXSCREEN))
            height = int(self._user32.GetSystemMetrics(_SM_CYSCREEN))
            return (0, 0, width, height)
        return (int(rectangle.left), int(rectangle.top),
                int(rectangle.right), int(rectangle.bottom))

    def _bottom_right(self, work_area: tuple[int, int, int, int]) -> tuple[int, int]:
        left, top, right, bottom = work_area
        margin = max(0, int(self._margin_px))
        x = max(left, right - self._width - margin)
        y = max(top, bottom - self._height - margin)
        return (x, y)

    def _ensure_font(self) -> int:
        if self._font is not None:
            return self._font
        font = self._call(
            lambda: self._gdi32.CreateFontW(
                -abs(int(self._font_px)), 0, 0, 0, _FW_NORMAL, 0, 0, 0,
                _DEFAULT_CHARSET, _OUT_DEFAULT_PRECIS, _CLIP_DEFAULT_PRECIS,
                _CLEARTYPE_QUALITY, _DEFAULT_PITCH | _FF_DONTCARE, _FONT_FACE,
            )
        )
        if not font:
            raise VoiceHudError(WIN32_FAILED)
        self._font = font
        return font

    def _wnd_proc(self, handle: object, message: int, wparam: object, lparam: object) -> int:
        """窗口过程：自绘 + 非阻塞拖动；拖动位移合并到 ``WM_TIMER`` 一次处理。"""

        try:
            if message == _WM_PAINT:
                self._paint(handle)
                return 0
            if message == _WM_ERASEBKGND:
                return 1
            if message == _WM_LBUTTONDOWN:
                if self._draggable:
                    self._begin_drag(handle)
                return 0
            if message == _WM_MOUSEMOVE:
                if self._dragging and self._draggable:
                    # 批次 A（真机"拖动太生硬"的根因）：移动事件里**直接**定位。
                    # 旧实现每次移动都 `SetTimer` 同一个 id，而同 id 定时器会被下一次调用
                    # **重置** ⇒ 连续拖动时它根本不触发，只有停下鼠标才跳一次，表现为
                    # "一卡一跳、不跟手"。直接定位是每帧一次 `SetWindowPos`，代价极低。
                    self._track_drag(handle)
                return 0
            if message == _WM_TIMER:
                # 兜底：定时器路径已不再用于合并拖动，但若系统仍派发该 id 就当成一次定位
                # （幂等，语义与直接定位一致）。
                if self._dragging and self._draggable:
                    self._track_drag(handle)
                return 0
            if message == _WM_LBUTTONUP:
                if self._dragging:
                    self._end_drag(handle)
                return 0
            if message == _WM_CLOSE:
                return 0
        except Exception:  # noqa: BLE001 - 窗口过程绝不能把异常抛回系统消息循环
            return 0
        return int(self._user32.DefWindowProcW(handle, message, wparam, lparam))

    def _begin_drag(self, handle: object) -> None:
        point = _Point(0, 0)
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return
        rectangle = _Rect()
        if not self._user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            return
        self._dragging = True
        self._drag_origin = (int(point.x), int(point.y))
        self._drag_window_origin = (int(rectangle.left), int(rectangle.top))
        self._user32.SetCapture(handle)

    def _track_drag(self, handle: object) -> None:
        point = _Point(0, 0)
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return
        target = (
            self._drag_window_origin[0] + int(point.x) - self._drag_origin[0],
            self._drag_window_origin[1] + int(point.y) - self._drag_origin[1],
        )
        area = self._work_area()
        max_x = max(area[0], area[2] - self._width)
        max_y = max(area[1], area[3] - self._height)
        clamped = (min(max(area[0], target[0]), max_x), min(max(area[1], target[1]), max_y))
        self._user32.SetWindowPos(
            handle, 0, clamped[0], clamped[1], self._width, self._height, _SWP_NOACTIVATE
        )
        self._last_position = clamped

    def _end_drag(self, handle: object) -> None:
        self._dragging = False
        self._user32.ReleaseCapture()
        self._user32.KillTimer(handle, _WM_LBUTTONDOWN_ID)
        # 批次 A：先做贴边吸附缓动（可能改变最终位置），再把**最终**位置作为一次性结果发布。
        position = self._settle_drag(handle)
        if position is None:
            position = self._last_position
        if position is not None:
            self._dragged_position = position

    def _settle_drag(self, handle: object) -> tuple[int, int] | None:
        """松手后把"差一点贴边"的位置用 ease-out 缓动补上；返回最终窗口位置。

        只在窗口距工作区左/右/上/下边缘不超过 ``drag_snap_px`` 时吸附；``drag_snap_px=0``
        或本来就贴边时**不做任何移动**（直接返回当前位置）。缓动在窗口过程内**内联**跑完
        （约 ``drag_settle_ms`` 毫秒，此时鼠标已松开），用真实时钟推进，不新增定时器/线程。
        """

        if self._last_position is None:
            return None
        x, y = self._last_position
        target = _snap_to_work_area(
            x=x, y=y, area=self._work_area(), width=self._width, height=self._height,
            snap_px=self._drag_snap_px,
        )
        if target == (x, y):
            return (x, y)
        if self._drag_settle_ms <= 0 or not _system_animations_enabled(self._user32):
            # 关闭吸附缓动（配置为 0）或系统关动画（批次 C）时：直接到位，不做缓动。
            self._move_during_settle(handle, target)
            return target
        started = time.monotonic()
        duration_s = self._drag_settle_ms / 1000.0
        while True:
            phase = (time.monotonic() - started) / duration_s
            if phase >= 1.0:
                break
            eased = _ease_out_cubic(0.0 if phase < 0.0 else phase)
            self._move_during_settle(
                handle,
                (
                    int(round(x + (target[0] - x) * eased)),
                    int(round(y + (target[1] - y) * eased)),
                ),
            )
            time.sleep(0.008)
        self._move_during_settle(handle, target)
        return target

    def _move_during_settle(self, handle: object, position: tuple[int, int]) -> None:
        """吸附缓动期间的定位：位置没变就不下发（避免无意义的重排）。"""

        if position == self._last_position:
            return
        self._user32.SetWindowPos(
            handle, 0, position[0], position[1], self._width, self._height, _SWP_NOACTIVATE
        )
        self._last_position = position

    def _paint(self, handle: object) -> None:
        paint = _PaintStruct()
        device_context = self._user32.BeginPaint(handle, ctypes.byref(paint))
        if not device_context:
            return
        try:
            client = _Rect()
            self._user32.GetClientRect(handle, ctypes.byref(client))
            width = int(client.right - client.left)
            height = int(client.bottom - client.top)
            background = _colorref(self._background)
            _fill_rect(self._user32, self._gdi32, device_context, client, background)
            # 左侧强调色竖条：只由配置颜色决定，不携带任何内容。
            accent_rect = _Rect(0, 0, _ACCENT_BAR_WIDTH, height)
            _fill_rect(self._user32, self._gdi32, device_context, accent_rect,
                       _colorref(self._accent))
            font = self._ensure_font()
            self._gdi32.SelectObject(device_context, font)
            self._gdi32.SetBkMode(device_context, _TRANSPARENT_BKGMODE)
            self._gdi32.SetTextColor(device_context, _colorref(self._foreground))
            self._gdi32.SetTextAlign(device_context, _TA_LEFT | _TA_TOP)
            self._gdi32.SetBkColor(device_context, background)
            text_width = max(0, width - _TEXT_LEFT - _TEXT_RIGHT)
            title_rect = _Rect(_TEXT_LEFT, _TITLE_TOP, _TEXT_LEFT + text_width,
                               _TITLE_TOP + self._font_px * 2)
            self._draw_text(device_context, self._title, title_rect)
            body_rect = _Rect(_TEXT_LEFT, _BODY_TOP, _TEXT_LEFT + text_width, height)
            self._draw_text(device_context, self._body, body_rect)
        finally:
            self._user32.EndPaint(handle, ctypes.byref(paint))

    def _draw_text(self, device_context: object, text: object, rectangle: object) -> None:
        """用 ``DrawTextW``（user32 导出）绘制多行文本；空文本直接跳过，不产生绘制副作用。"""

        if type(text) is not str or not text:
            return
        self._user32.DrawTextW(
            device_context, text, -1, ctypes.byref(rectangle),
            _DT_LEFT | _DT_NOPREFIX,
        )

    def _call(self, action: object) -> object:
        """执行一次 Win32 调用；``0``/``None`` 等失败由调用点判定，异常一律 fail closed。"""

        try:
            return action()  # type: ignore[operator]
        except VoiceHudError:
            raise
        except Exception as exc:  # noqa: BLE001 - Win32 调用失败必须 fail closed
            raise VoiceHudError(WIN32_FAILED) from exc


# --------------------------------------------------------------- ctypes 结构体
# 与 ``app/tts/waveout_pcm.py`` 一致：手写 Structure、手写 argtypes/restype。


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 1)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _Msg(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint), ("pt", _Point), ("lPrivate", ctypes.c_uint)]


class _PaintStruct(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p), ("fErase", ctypes.c_int),
                ("rcPaint", _Rect), ("fRestore", ctypes.c_int),
                ("fIncUpdate", ctypes.c_int), ("rgbReserved", ctypes.c_byte * 32)]


class _WndClassW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
)
_WNDPROC_POINTER = ctypes.c_void_p

_FONT_FACE = "Microsoft YaHei UI"
_ACCENT_BAR_WIDTH = 4
_TEXT_LEFT = 16
_TEXT_RIGHT = 12
_TITLE_TOP = 14
_BODY_TOP = 44

_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4

_TA_LEFT = 0
_TA_TOP = 0


def _colorref(rgb: tuple[int, int, int]) -> int:
    """把 ``(R, G, B)`` 折算为 Win32 ``COLORREF``（``0x00BBGGRR``）。"""

    red, green, blue = rgb
    return int(red) | (int(green) << 8) | (int(blue) << 16)


def _fill_rect(user32: object, gdi32: object, device_context: object,
               rectangle: object, colorref: int) -> bool:
    """用 ``CreateSolidBrush`` + ``FillRect`` 填一个矩形；失败返回 ``False`` 不抛错。

    ``FillRect`` 由 user32 导出（笔刷对象则来自 gdi32），与 Win32 SDK 的头文件归属一致。
    """

    brush = gdi32.CreateSolidBrush(ctypes.c_uint(colorref))
    if not brush:
        return False
    try:
        user32.FillRect(device_context, ctypes.byref(rectangle), brush)
    finally:
        gdi32.DeleteObject(brush)
    return True


def _configure_user32(user32: object) -> None:
    """按 Win32 SDK 语义配置 user32 函数签名；64 位下句柄必须显式声明避免被截断。"""

    user32.DefWindowProcW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
    ]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.FillRect.restype = ctypes.c_int
    user32.DrawTextW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint
    ]
    user32.DrawTextW.restype = ctypes.c_int
    user32.GetClassInfoW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p]
    user32.GetClassInfoW.restype = ctypes.c_int
    user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    user32.UnregisterClassW.restype = ctypes.c_int
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.DestroyWindow.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_int
    user32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    user32.SetWindowTextW.restype = ctypes.c_int
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint
    ]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_int
    user32.PeekMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
    ]
    user32.PeekMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.TranslateMessage.restype = ctypes.c_int
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.restype = ctypes.c_longlong
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SystemParametersInfoW.argtypes = [
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
    ]
    user32.SystemParametersInfoW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowRect.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetClientRect.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.c_void_p]
    user32.GetCursorPos.restype = ctypes.c_int
    user32.SetCapture.argtypes = [ctypes.c_void_p]
    user32.SetCapture.restype = ctypes.c_void_p
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = ctypes.c_int
    user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_void_p
    user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.KillTimer.restype = ctypes.c_int
    user32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.BeginPaint.restype = ctypes.c_void_p
    user32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.EndPaint.restype = ctypes.c_int
    user32.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    user32.InvalidateRect.restype = ctypes.c_int
    # 真机缺陷修复：只有 InvalidateRect 时首帧不会立即绘制，窗口要等后续消息才出现，
    # 表现为"播报完才看到窗口、内容还是旧帧"。这里声明 UpdateWindow 以强制同步重绘。
    user32.UpdateWindow.argtypes = [ctypes.c_void_p]
    user32.UpdateWindow.restype = ctypes.c_int
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = ctypes.c_ushort


def _configure_gdi32(gdi32: object) -> None:
    """配置 gdi32 绘制函数签名（字体、笔刷、颜色模式）。"""

    gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint]
    gdi32.CreateSolidBrush.restype = ctypes.c_void_p
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p,
    ]
    gdi32.CreateFontW.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    gdi32.SetTextColor.restype = ctypes.c_uint
    gdi32.SetBkColor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    gdi32.SetBkColor.restype = ctypes.c_uint
    gdi32.SetTextAlign.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    gdi32.SetTextAlign.restype = ctypes.c_uint
    # 逐像素渲染（v3）：32 位 DIB + 内存 DC，供 UpdateLayeredWindow 取帧。
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_uint,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_int
    gdi32.GdiFlush.argtypes = []
    gdi32.GdiFlush.restype = ctypes.c_int


def _configure_user32_layered(user32: object) -> None:
    """配置逐像素分层窗口相关的 user32 签名（``UpdateLayeredWindow``）。

    离线测试注入的替身可能没有这个导出：找不到时只跳过签名配置（能力探测会在真机路径
    上失败并降级），不把 ``AttributeError`` 抛回调用方。
    """

    update = getattr(user32, "UpdateLayeredWindow", None)
    if update is None or not hasattr(update, "argtypes"):
        return
    update.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    update.restype = ctypes.c_int


__all__ = [
    "DEFAULT_SCREEN_SIZE",
    "NOT_CREATED",
    "STATE_LABELS",
    "TITLE_TEXT",
    "UNSUPPORTED_PLATFORM",
    "VOICE_HUD_SCHEMA_VERSION",
    "WINDOW_CLASS_NAME",
    "WIN32_FAILED",
    "VoiceHudApi",
    "VoiceHudConfig",
    "VoiceHudError",
    "VoiceHudState",
    "VoiceStatusHud",
    "Win32VoiceHudApi",
    "build_win32_api",
    "clamp_position",
    "hud_text",
    "screen_position_for",
    "state_label",
]
