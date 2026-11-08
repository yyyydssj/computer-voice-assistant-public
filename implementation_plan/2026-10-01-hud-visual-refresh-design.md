# 状态提示窗视觉改版设计（方案 4 卡片胶囊版）

> 状态：**已与用户逐项确认，待实施**（2026-10-01）
> 范围：只改 `app/ui/voice_hud.py` 的**外观渲染**与其离线测试；不触碰语音链路、安全链与授权语义。
> 素材：`implementation_plan/checkpoints/artifacts/2026-10-01-hud-mockups/`（深色 4 张 + 浅色 4 张效果图，用户选定浅色第 4 张 `mock-light-4-card.png`）

## 1. 决策与范围

### 1.1 已确认的需求（用户逐项选定）

| 项 | 结论 |
|---|---|
| 目标 | 更好看：圆角 / 描边 / 弱阴影，像现代控件（不做状态动效，不做信息扩展） |
| 底色 | 浅色 / 白底 |
| 尺寸 | 卡片维持 320×96 |
| 状态提示 | 左侧状态色条 |
| 阴影 | 要真正的柔和外阴影（逐像素 alpha） |
| 标题 | 保留「电脑语音助手」 |
| 视觉版本 | 方案 4：标题坐在状态色浅底圆角胶囊上 |

### 1.2 明确不做（YAGNI）

- ~~不加动画、不做脉动/过渡~~ → **2026-10-12 变更**：已按用户要求加入"进场淡入+滑入 / 退场淡出+滑出 / 松手吸附缓动"，
  并**明确不对"状态切换"做动效**（真机反馈脉冲式过渡会被看成闪烁）；
  细则与证据见 `checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`；
- 不做待机自动隐藏（保持现有常驻行为，只有 `hide()` 被显式调用才隐藏）；
- 不加状态小图标、不加轮次行、不显示识别正文或回复正文；
- 不改 `STATE_LABELS` 的 4 态文案与 9 态映射；
- 不改可拖动、不抢焦点、置顶可配、fail-closed 等既有交互与失败语义。

### 1.3 方案对比（为何选 A）

| 方案 | 做法 | 结论 |
|---|---|---|
| **A 逐像素离屏渲染（选用）** | 纯 Python 生成 32 位 ARGB 缓冲（圆角覆盖度、外阴影、胶囊、色条），文字用 GDI `DrawTextW` 叠加；`CreateDIBSection` + `UpdateLayeredWindow(ULW_ALPHA)` 贴屏 | 像素生成为**纯函数**，可离线断言四角透明、阴影梯度、色条颜色；真阴影与抗锯齿圆角都能做到 |
| B GDI+ 变体 | `gdiplus.dll` 的 `GraphicsPath` / `DrawImage` | Win32 胶水层更厚，「文字不破坏 alpha」更难验证 |
| C 低风险折中 | 系统 `CreateRoundRectRgn` 裁圆角 + 描边 | 圆角有锯齿、无外阴影，与已确认需求不符 |

## 2. 视觉规格（冻结）

### 2.1 尺寸与位置

| 项 | 值 |
|---|---|
| 卡片 | 320×96 px |
| 阴影留白 | 每边 8 px（`shadow_margin_px`） |
| 真实窗口 | **336×112 px** = 卡片 + 两侧各 8 px |
| 贴边内缩 | `margin_px = 24`（指**卡片**右下角距工作区右下角 24 px，语义与今天一致） |
| 窗口左上角 | `(screen_w - 320 - 24 - 8, screen_h - 96 - 24 - 8)`（按工作区计算，并与今天一样钳制在屏内） |
| 卡片在窗口内偏移 | `(8, 8)` |

### 2.2 卡片

| 元素 | 规格 |
|---|---|
| 圆角 | 16 px |
| 底色 | `#FFFFFF` 纯白（不做渐变） |
| 描边 | 1 px `#DFE2E8`（沿圆角外沿，内侧绘制） |
| 外阴影 | 圆角矩形掩码做可分离盒式模糊（两次盒式模糊近似高斯），**峰值 alpha 60/255、向下偏移 2 px**，在 8 px 留白内衰减到 0 |
| 左色条 | 宽 4 px，胶囊状两端（随卡片圆角内裁切），颜色 = 当前状态色 |
| 整窗不透明度 | `opacity`（默认 235）折算进像素 alpha（乘到整体覆盖率上），字段与 `0..255` 校验区间不变 |

### 2.3 文字与胶囊

| 元素 | 规格 |
|---|---|
| 字体 | `Microsoft YaHei UI`（沿用，不引字体文件；粗体用 `FW_SEMIBOLD = 600`） |
| 标题 | 文案「电脑语音助手」，13 px，字色 = 状态色加深 15% |
| 标题胶囊 | 状态色**12% 不透明度**为底，圆角 8 px 胶囊；位置 (14, 12)、尺寸约 120×22（按字号定高，右端不超内容区） |
| 正文 | 四态文案（待机/聆听/播报/失败），16 px 加粗，字色 = 状态色 |
| 正文位置 | 内缩 16 px、基线区域自卡片顶部 44 px 起（与今天视觉位置一致） |
| 状态色 | 待机 `#6E7684` · 聆听 `#1A6EDC` · 播报 `#108C5C` · 失败 `#C83030` |
| 内容来源 | 仍只由 `VoiceHudState` 与固定文案表生成；**不存在任何外部文本进入渲染的路径** |

### 2.4 主题常量（浅色）

- 背景 `#FFFFFF`、正文色按状态、弱化文字灰 `#6E7684`、描边 `#DFE2E8`；
- 阴影色 `(0,0,0)` 仅通过 alpha 生效；
- 以上全部落在 `VoiceHudConfig` 的默认值或模块级冻结常量里，可被调用方覆盖。

## 3. 实现设计（方案 A）

### 3.1 纯函数边界（可离线测试的核心）

新增**不接触任何 Win32 调用**的渲染函数：

```python
def render_card_pixels(config: VoiceHudConfig, state: VoiceHudState) -> bytes
```

- 返回**预乘 alpha 的 BGRA 缓冲**（`len == window_w * window_h * 4`，`window_* = 卡片 + 2×shadow_margin_px`），行序自上而下、不做镜像；
- 覆盖度计算用 4× 超采样（`scale = 4`）后按面积平均到 1×，得到抗锯齿的圆角与阴影边缘；
- 文字**不在本函数内绘制**（文字由 GDI 叠加），因此纯函数测试可以稳定断言像素值。

辅助纯函数（同样可单测）：

- `card_pixel_size(config) -> (w, h)`：卡片尺寸；
- `window_pixel_size(config) -> (w, h)`：窗口尺寸（含阴影留白）；
- `card_origin_in_window(config) -> (x, y)`：卡片在窗口内的偏移（`(8, 8)`）；
- `state_color(state) -> (r, g, b)`：四态映射，未知状态收敛为待机色（不抛错、不回显外部内容）。

### 3.2 文字叠加与预乘 alpha（关键不变量）

实测结论（真机验证，务必照此实现）：

- GDI 把文字画进 32 位 DIB 时会写「正确 RGB + **alpha=0**」；分层窗口按预乘 alpha 合成，
  alpha=0 就等于完全不可见 → 文字会变成残影或彻底消失；
- 因此文字矩形内的像素必须**先**归一到 alpha=255（保留已与底色混合好的 RGB），再让 GDI
  做正常抗锯齿混合；画完后 `GdiFlush`，再按「帧前快照」把矩形内**发生变化的字形像素**
  alpha 补回 255（`_snapshot_rects` / `_restore_text_alpha`）；
- 不变量：叠加文字后，卡片内部（含文字矩形）像素 alpha 必须为 255；阴影保持半透明；
  窗外完全透明像素 alpha 保持 0。

### 3.3 Win32 侧

1. 建窗不变：`WS_POPUP` + `WS_EX_TOOLWINDOW|WS_EX_LAYERED|WS_EX_NOACTIVATE`（可选 `WS_EX_TOPMOST`）、不显任务栏、不抢焦点；
2. **能力探测必须在建窗之后**（`layered_window_supported()` 依赖已创建的句柄），否则会永久降级（真机实测踩过）；
3. 新增 `CreateDIBSection`（32 位、`BI_RGB`、`top-down`）+ 内存 DC，尺寸 = 窗口尺寸；结构体必须「先构造再赋值」，`_BitmapInfo(header, colors)` 形式会抛 `IndexError`；
4. 整帧一次性写入：像素缓冲 `memmove` 进 DIB → 按图层分级掩码归一化 alpha → 同 DC 上 `DrawTextW` 画标题与正文 → 补回字形 alpha；
5. `UpdateLayeredWindow(ULW_ALPHA)` 提交整帧（`BLENDFUNCTION{AC_SRC_OVER, 0, 255, AC_SRC_ALPHA}`）；`pptDst` 必须传 **NULL**，否则窗口会被挪到屏幕左上角；
6. **逐像素路径不得调用 `SetLayeredWindowAttributes`**：它与 `UpdateLayeredWindow` 互斥（实测返回 0 / GetLastError=87）。整窗 alpha 只在降级路径上通过 `apply_layered_alpha()` 补一次；
7. 需要重绘的时机：`set_state()` 状态变化、`show()` 首帧；拖动只改位置，不重算像素；
8. 资源释放：`destroy_window()` 里释放 DIB 与内存 DC（`_release_frame_resources`）以及既有字体/窗口类，幂等；
9. 任一环节失败（能力缺失 / 首帧抛错）→ **降级为纯色卡片路径**，只降级一次并保持后续走纯色，**不抛异常**、不打断语音链路；`render_mode` 属性可观测。

### 3.4 契约与配置兼容（按实际实现）

- `VOICE_HUD_SCHEMA_VERSION`：2 → **3**（文案未变，但视觉与坐标语义变了，按模块自身约定递增）；
- `VoiceHudConfig` **旧字段全部保留**（`width` / `height` / `margin_px` / `opacity` / `font_px` / `background_rgb` / `foreground_rgb` / `accent_rgb` / `title` / `topmost` / `draggable`），语义重映射到浅色主题；默认值改为 `background_rgb=(255,255,255)`、`foreground_rgb=(110,118,132)`、`accent_rgb=(110,118,132)`（待机灰）；
- 新增字段（全部带校验）：`shadow_margin_px=8`、`corner_radius_px=16`、`border_rgb=(223,226,232)`、`title_font_px=13`、`body_font_px=16`、`state_colors`（有序 `(状态名, RGB)` 元组的**局部覆盖**，未给出的状态仍取冻结四态色）；
- 胶囊透明度（31 ≈ 12%）、胶囊圆角（8）、色条宽度（4）、配色与字形尺寸等**渲染参数由 `hud_render.HudRenderSpec` 承载**，`VoiceHudConfig` 不重复暴露（避免两套真值源）；
- `screen_position_for` 增加可选参数 `pad: int = 0`：默认行为与今天逐字节一致；建窗时传 `pad=shadow_margin_px`，保证**卡片可见位置不变**；
- `hud_text()` 签名与返回值不变（`(标题, 正文)`），`STATE_LABELS` 文案表不变；
- 外观唯一来源：`build_render_spec(config, state)` 把 `VoiceHudConfig` 折算成 `HudRenderSpec`，像素渲染与真机绘制共用同一份规格。

### 3.5 硬边界（不得违反）

- 模块级仍只导入 stdlib（新增的纯计算不得引入第三方库；2026-10-12 批次 C 起另加 `time`）；
- 不联网、不打开麦克风/扬声器、不读写文件、**不创建线程，也不创建定时器**
  （2026-10-12 批次 A 起，拖动合并定时器已移除；拖动跟手改为移动事件里直接定位，
  进场/退场/吸附缓动都在 `show()`/`hide()`/窗口过程里**内联**跑完）；
- 动效还必须尊重系统"动画效果"开关（`SPI_GETCLIENTAREAANIMATION = 0x1042`）：关掉时**零过渡帧**；
- 不导入 `app.safety` / `app.executor` / `app.plans` / `app.auditing` / `app.verification` / `app.tools` / `app.tts` / `app.asr`；
- 非阻塞消息循环（`PeekMessageW`）与不抢焦点（无 `SetForegroundWindow`）保持不变；
- 失败仍收敛为固定短代码 `unsupported_platform` / `win32_failed` / `not_created`，绝不携带路径、异常正文或对话内容。

## 4. 测试计划

### 4.1 需要更新的既有测试

| 测试 | 改动原因 |
|---|---|
| `ConfigTests.test_default_config_matches_frozen_values` | 默认配色改为浅色主题 + 新增外观字段（`font_px` 仍是 15） |
| `ConfigTests.test_config_rejects_out_of_range_values` | 新增字段的取值范围校验（补正/负例） |
| `PositionTests` / `LifecycleTests` / `TickAndDragTests` | 窗口外扩 8 px：`move_window` 尺寸与坐标期望更新为 336×112；`pad` 默认 0 保持旧断言 |
| 契约测试（`VOICE_HUD_SCHEMA_VERSION == 2`、导入面、执行面白名单） | 版本改 3；允许同层 `app.ui.hud_render`；白名单补 `UpdateLayeredWindow` / `CreateDIBSection` / `CreateCompatibleDC` / `DeleteDC` / `GdiFlush` |

### 4.2 新增测试（实际 48 项，全部离线、不开真窗口）

1. **尺寸与几何**：`window_pixel_size` / `card_pixel_size` / `card_origin_in_window` 与配置一致；
2. **透明度不变量**：窗口四角 / 窗外 alpha = 0；卡片内部 alpha = 255；预乘通道不超过 alpha；
3. **圆角抗锯齿**：圆角过渡带存在 0<a<255 的像素；半径 0 与半径 20 的形状差异；
4. **外阴影**：黑色、紧贴卡片一带峰值落在 40..70、向外单调衰减、整体下移；`shadow_margin_px=0` 时无阴影；
5. **左色条**：宽 4px、四态各自取状态色、圆角处有抗锯齿过渡、`bar_opacity` 生效；
6. **标题胶囊**：底色 = 状态色 12% 混白、只在胶囊矩形内、右端圆角有抗锯齿过渡；
7. **状态色映射**：`state_color()` 四态值 + 未知状态回退待机 + 不回显外部内容；自定义色表生效；
8. **图层分级**：五类图层（透明/卡片/色条/胶囊/阴影）与代表点分类正确、阴影与透明严格区分；
9. **降级与模式**：`PixelModeTests` 覆盖能力探测只探一次、首帧与状态变化重绘、失败一次性降级且继续显示；
10. **契约**：`__all__` 冻结、模块只导入 stdlib、源码无 IO/网络/线程/Win32 名字、渲染确定性、缓存键敏感性。

### 4.3 回归与真机（实测结果）

- 全量回归：**2034 tests OK (skipped=3)**（改版前基线 1978；+48 纯像素测试 +8 逐像素路径测试）；
  `compileall -q app tests tools` exit 0；
- `voice_status` / `main` / `p9_auto_voice_runner` 现有测试保持通过；
- 真机确认（用户执行）：`python -m app.main --voice` 看到白底圆角卡片 + 状态色条 + 状态色正文，
  用户回复「没问题」；本机自证 `render_mode=pixel`、窗口 `(1552,936)`–`(1888,1048)`。

## 5. 验收标准（全部达成）

1. 四态颜色与胶囊外观符合方案 4 规格 —— ✅ 用户真机肉眼确认；
2. 卡片可见位置与旧版一致（右下角内缩 24 px），窗口外扩 8 px 不外溢 —— ✅ 实测 `(1552,936)`；
3. 离线新增测试 + 既有测试全绿，全量回归无新增失败 —— ✅ 2034 OK (skipped=3)；
4. 降级路径可用：能力缺失时仍显示纯色卡片且不报错 —— ✅ 由 `PixelModeTests` 覆盖；
5. 无新增依赖、无文件 IO、无网络、安全链零改动 —— ✅ 契约测试断言。

## 6. 回滚

- `git revert` 本批次的提交即可回到当前视觉（纯色卡片），不影响语音链路、自动收音、唤醒词与声纹；
- 只回滚视觉效果时不需改任何配置或环境变量；
- 若 `UpdateLayeredWindow` 在用户机器上表现不佳，可把渲染函数切回单色填充（保留新配置字段）作为最小改动回退。

## 7. 素材与后续批次

- 效果图（对照用）：`implementation_plan/checkpoints/artifacts/2026-10-01-hud-mockups/`（8 张 PNG）；
  **已加入 `.gitignore`（设计过程素材不入库）**，需要时按 §2 规格与 `HudRenderSpec` 默认值重生成；
- 本批次**不含**：待机自动隐藏、状态动效、图标、多语言文案、AEC/全双工；
- 并列待办（互不影响）：免按键自动收音接入 `app/main.py` 默认运行时（需单独授权）。
- 已知小瑕疵：GDI 文字阶段会让胶囊圆角处约 2px 的像素 alpha 归零（肉眼不可见）；
  如需彻底消除，可把文字改为离屏预排版后按像素写入。
