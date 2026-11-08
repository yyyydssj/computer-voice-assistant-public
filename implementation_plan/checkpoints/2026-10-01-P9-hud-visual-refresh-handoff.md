# P9 UI 改版交接：浅色卡片胶囊外观（方案 4）与逐像素分层窗口

> 状态：**已实施并通过离线全量回归 + 用户真机肉眼确认**（2026-10-01）
> 设计文档：`implementation_plan/2026-10-01-hud-visual-refresh-design.md`
> 涉及模块：`app/ui/hud_render.py`（新增）、`app/ui/voice_hud.py`、`tests/foundation/test_hud_render.py`（新增）、`tests/foundation/test_voice_hud.py`

## 1. 一句话结论

状态提示窗从「深色直角纯色卡片」升级为「浅色圆角卡片 + 柔和外阴影 + 状态色条 + 标题胶囊 + 状态色正文」，
渲染走 `UpdateLayeredWindow` 逐像素路径；真机 `render_mode=pixel`，用户确认外观无误。
`VOICE_HUD_SCHEMA_VERSION` 2 → 3。

## 2. 交付物

| 文件 | 说明 |
|---|---|
| `app/ui/hud_render.py`（新增） | 纯像素渲染核心：`render_card_pixels` / `paint_layer_masks` / `HudRenderSpec` / `HudTheme`；零第三方依赖、无 Win32 |
| `app/ui/voice_hud.py`（改） | 窗口外扩阴影留白、DIB + `UpdateLayeredWindow` 提交、GDI 文字叠加、失败一次性降级；`VoiceHudConfig` 新增外观字段 |
| `tests/foundation/test_hud_render.py`（新增 48 项） | 几何 / 透明度不变量 / 圆角抗锯齿 / 阴影梯度 / 状态色 / 胶囊 / 图层分级 / 契约 |
| `tests/foundation/test_voice_hud.py`（改） | 坐标与默认配色期望更新；新增 `PixelModeTests`（能力探测、重绘、一次性降级、配置校验） |
| `.gitignore` | 新增 `/data/`（声纹档案）与设计效果图目录 |

## 3. 冻结的验收口径

- 卡片 320×96、圆角 16px、窗口 336×112（四周 8px 阴影留白）；卡片可见位置仍是**右下角内缩 24px**；
- 圆角 4× 超采样（解析签名距离）+ 阴影峰值 alpha ≈ 57（40..70 区间）；
- 四态色：待机 `#6E7684` / 聆听 `#1A6EDC` / 播报 `#108C5C` / 失败 `#C83030`；
- 标题胶囊底 = 状态色 12% 与白底混合；标题字 = 状态色加深 15%；正文用状态色、16px；
- 首帧渲染 84ms、缓存后 12ms（同一外观只算一次掩码）。

## 4. 真机才暴露的 6 个缺陷（全部已修，务必保留这些约束）

| # | 现象 | 根因 | 约束 |
|---|---|---|---|
| 1 | 永远显示旧外观 | `_decide_render_mode()` 在**建窗之前**被调用，而 `layered_window_supported()` 要求句柄存在 → 恒 False | 能力探测必须在建窗之后 |
| 2 | 首帧必然失败并降级 | `_BitmapInfo(header, (0,0,0,0))` 构造非法（ctypes 数组不能按位置传） | 结构体先建后赋值 |
| 3 | `UpdateLayeredWindow` 返回 0（GetLastError=87） | `create_window` 调了 `SetLayeredWindowAttributes`，**与 `UpdateLayeredWindow` 互斥** | 逐像素路径不得设置整窗 alpha；降级时才补 |
| 4 | 卡片是黑的 | `VoiceHudConfig` 默认配色仍是旧深色 (28,28,30) | 外观默认值必须与浅色主题一致 |
| 5 | 正文文字永远不显示 | `_draw_frame_text` 的 `self` 是 **api 实例**，其 `_body` 是旧 `set_text` 缓存（恒空） | api 与 HUD 同名属性不得互相读取；文字由参数传入 |
| 6 | 阴影变黑边 / 圆角出现透明洞 | 窗外像素被写成不透明黑；卡片抗锯齿像素 alpha 只代表覆盖率 | 分「完全透明 / 阴影 / 卡片」三级；文字矩形内先置不透明 |

补充：`UpdateLayeredWindow` 的 `pptDst` 必须传 NULL，否则窗口会被挪到屏幕左上角。

## 5. GDI 文字与预乘 alpha 的关键技巧（别改坏）

GDI 把文字画进 32 位 DIB 时会写「正确 RGB + **alpha=0**」；分层窗口按预乘 alpha 合成，
alpha=0 就等于完全不可见。因此流程是：

1. 纯像素缓冲先按图层分级（`paint_layer_masks`）把 alpha 归一化：
   卡片/色条/胶囊与**文字矩形内**的像素写回 255；**阴影保持半透明**；其余完全透明像素保持 0；
2. GDI 用正常抗锯齿混合绘制标题与正文（不做任何 ROP 花招）；
3. 画完 `GdiFlush`，再按「文字矩形帧前快照」把**发生变化的字形像素** alpha 补回 255
   （`_restore_text_alpha`），字形边缘颜色与抗锯齿不受影响。

配套纯函数：`_snapshot_rects` / `_restore_text_alpha` / `_in_text_area`（都在 `voice_hud.py`）。

## 6. 降级语义（不得回退）

- api 无 `render_frame` / `layered_window_supported`，或首帧抛错 → **只降级一次**，
  `render_mode` 固定为 `legacy`，回到 v2 的纯色卡片 + `set_text`，**不抛异常、不打断语音链路**；
- 降级时补一次 `apply_layered_alpha()`（整窗不透明度）；逐像素路径绝不调用它；
- `render_mode` 属性可观测（`pixel` / `legacy`），离线测试覆盖两条路径。

## 7. 测试与验收证据

- 全量回归：**2034 tests OK (skipped=3)**；`compileall -q app tests tools` exit 0；
- HUD 专项：`tests.foundation.test_voice_hud`（63 项）+ `test_hud_render`（48 项）+ `test_voice_status` 全绿；
- 真机自证（本机 1920×1080、DPI 100%）：窗口 `(1552, 936)`–`(1888, 1048)`，`render_mode=pixel`，
  四态截图逐态确认；用户确认「没问题」；
- 设计过程效果图（8 张 PNG，未入库）：`implementation_plan/checkpoints/artifacts/2026-10-01-hud-mockups/`。

## 8. 未做 / 后续

- 不做动画、图标、待机自动隐藏（YAGNI，与设计文档 §1.2 一致）；
- 圆角像素在 GDI 文字阶段会留下 4 个约 2px 的透明小缺口（可忽略；如需彻底消除需改为离屏预排版文字）；
- 仍待授权：免按键自动收音接入 `app/main.py` 默认运行时；多轮上下文 / SSE 流式 / 费用限流。
