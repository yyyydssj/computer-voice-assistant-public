# P9 UI 交接：提示窗拖动手感 + 进场/退场动效（批次 A/B/C）

- 日期：2026-10-12（本地）
- 范围：**只改 `app/ui/voice_hud.py` 的交互与动效** + 其离线测试；不动渲染规格、状态机、降级路径、语音链路与授权语义
- 触发：用户「拖动太生硬，整体要生动一点」→ 随后「状态变化时不要闪烁」→ 随后「去 GitHub 参考好的弹窗怎么做」
- 验证：`tests/foundation/test_voice_hud.py` **92 OK**（63 → 92）；全量 **2747 OK (skipped=3)**，153.3 s

---

## 1. 批次 A：拖动手感（**缺陷修复**，非审美）

**读码确证的根因**（批次 A 之前**没有任何测试覆盖这条路径**）：

| 环节 | 旧实现 | 后果 |
|---|---|---|
| `WM_MOUSEMOVE` | 每次移动都 `SetTimer(hwnd, id=1, 16ms)` | 同 id 定时器被下一次调用**重置** ⇒ 连续拖动时根本不触发 |
| `WM_TIMER` | 才 `_track_drag()` 一次定位 + `KillTimer` | 只有鼠标**停下**才跳一次 = "一卡一跳、不跟手" |
| 松手 | `_apply_drag()` 用 `clamp_position(margin_px=24)` 复钳 | 松手瞬间窗口被**再拉进最多 24 px** = 二次跳位 |

**改法**
1. `WM_MOUSEMOVE` → **直接** `_track_drag(handle)`（每帧一次 `SetWindowPos`，代价极低）；`SetTimer` 合并**删除**（保留一个幂等的 `WM_TIMER` 兜底分支）；
2. `_apply_drag()` 的复钳 `margin_px` **24 → 0**（`_track_drag` 已按**工作区**钳过；仍保证不出屏）；
3. 松手**贴边吸附**：距工作区边缘 ≤ `drag_snap_px`（默认 24）时用 **ease-out `drag_settle_ms`=120 ms** 缓动补到贴边；
4. 新增纯函数 `_snap_to_work_area()` / `_ease_out_cubic()`；
5. `Win32VoiceHudApi.create_window()` 与 `VoiceHudApi` 协议各加两个**默认 0** 的可选参数
   （`drag_snap_px` / `drag_settle_ms`），旧替身不传即行为不变。

---

## 2. 批次 B：帧缓存 + 整窗 alpha（后被真机判定为"闪"）

- **性能前提**：`_write_pixels()` 是逐像素 Python 循环 + 每像素一次 `ctypes.memset`（37 632 px/帧），
  直接拿它做动画会卡 ⇒ 新增 **"已含文字的合成帧缓存"**：
  - `render_frame()` 合成完（像素 + GDI 文字 + alpha 归一）后**回读 DIB** 存入 `_composed_frames[(title, body)]`；
  - 新增 `blit_cached_frame(handle, title, body, alpha_scale)`：命中则 `memmove` + `bytes.translate(alpha 表)`
    + `UpdateLayeredWindow`（<1 ms/帧）；未命中返回 `False`，调用方回退完整渲染；
  - `_alpha_scale_table()`：256 项字节翻译表（预乘 BGRA **四通道等比**缩放 = 数学正确的整窗淡入淡出）。
- 状态切换曾实现为"旧帧淡到 47% → 新帧淡入"（`_ANIMATION_DIP_ALPHA=120`）。

**真机反馈：这看起来是闪烁**（本质是一次**亮度往返脉冲**，浅色卡片尤其明显）⇒ 见 §3。

---

## 3. 防闪修正（用户指令「状态变化时候不要闪烁」）

1. **`state_animation_ms` 默认改 0 = 状态切换硬切**（零亮度变化 ⇒ 绝不会闪）；只有显式设 >0 才启用该脉冲过渡；
2. 新增**防闪护栏** `_STATE_SWITCH_GUARD_S = 0.2`：距上次真正切换文案不足 200 ms 时，下一次切换**跳过动效**直接硬切
   （真机场景：无播报的轮次里 `SPEAKING → LISTENING` 只隔几十毫秒，连跑两段过渡 = 闪两下）；
3. 保留 `show_fade_ms` 的**首次**淡入（单向、不往返，读起来是"淡入"而不是"闪"）。

---

## 4. 批次 C：进场/退场 + 尊重系统动画开关

GitHub 调研（实读源码，见 §5）后的落地：

| 项 | 实现 |
|---|---|
| 进场 | 淡入 **+ 从右侧 `show_slide_px`=12 px 滑入**，`show_fade_ms` 160 → **260 ms**，**ease-out** |
| 退场 | `hide()` 先 **`hide_fade_ms`=180 ms 淡出 + 右滑**，再 `ShowWindow(SW_HIDE)`，并把窗口**归位**（下次仍从休息位滑入）；`visible` 语义不变（返回后即 `False`） |
| 无障碍 | 新增 `respect_system_animation=True`：读 **`SPI_GETCLIENTAREAANIMATION=0x1042`**；系统关"动画效果"时**进场/退场/状态过渡/拖动吸附全部直接到位、零过渡帧** |
| 状态切换 | **保持硬切**（不动） |

- 新增可选协议方法 `VoiceHudApi.animations_enabled()`（未实现按"开启"处理 ⇒ 旧替身不动效、不报错）；
- `_animations_allowed()` **每次动效前重新探测**（用户中途改设置能立刻生效）；探测失败一律 fail-open（按开启）；
- 拖动吸附在系统关动画时**直接到位**（`_settle_drag` 内联判断）；
- 新增 `_run_transition_ramp()`（alpha + 可选位置的 ease-out 渐变）、`_move_for_animation()`。

---

## 5. GitHub 调研证据（2026-10-12，均为实读源码）

| 来源 | 许可/规模 | 关键事实 |
|---|---|---|
| [sonner](https://github.com/emilkowalski/sonner) `src/styles.css` | MIT，13.0k★ | 进场 `sonner-fade-in 300ms ease`（`opacity:0; scale(0.8)` → 1）；退场 `sonner-fade-out 0.2s ease`；堆叠 `transform/opacity/height 400ms`、`box-shadow 200ms`；滑走 `200ms ease-out`；**`@media (prefers-reduced-motion){ transition:none!important; animation:none!important }`** |
| [react-hot-toast](https://github.com/timolins/react-hot-toast) `src/components/toast-bar.tsx` | MIT，11.0k★ | 进场 `0.35s cubic-bezier(.21,1.02,.73,1)`（`translate3d(0,-200%,0) scale(.6) opacity:.5`）；退场 `0.4s cubic-bezier(.06,.71,.55,1)`；`prefersReducedMotion()` ⇒ **只保留 opacity 淡入淡出**；`REMOVE_DELAY=1000ms` |
| [`tpn/winsdk-10` WinUser.h](https://github.com/tpn/winsdk-10/blob/master/Include/10.0.16299.0/um/WinUser.h) | SDK 头文件 | `SPI_GETANIMATION=0x0048`、`SPI_GETCLIENTAREAANIMATION=0x1042`、`SPI_SETCLIENTAREAANIMATION=0x1043` |

**三条共识**：① 动效只发生在**出现/消失**，两个库都**不对内容变化做动画**（印证状态切换硬切）；
② 时长很短（进场 300–350 ms、退场 200–400 ms），一律 ease-out；③ 都尊重系统"减少动态效果"。
**未采纳**：`scale(0.8→1)` 缩放（需重绘或更大 DIB，成本不抵收益）；堆叠/滑动消失（我们是常驻单卡）。

---

## 6. 配置总表（全部可关，0 = 回到旧行为）

```python
VoiceHudConfig(
    show_fade_ms=260,            # 首次显示淡入（0 = 直接出现）
    show_slide_px=12,            # 进场滑入 / 退场滑出距离（0 = 不滑）
    hide_fade_ms=180,            # 退场时长（0 = 立即隐藏）
    state_animation_ms=0,        # 状态切换动效：0 = 硬切（默认，防闪）
    drag_snap_px=24,             # 松手吸附距离（0 = 不吸附）
    drag_settle_ms=120,          # 吸附缓动时长（0 = 直接到位）
    respect_system_animation=True,  # 系统关动画 ⇒ 全部直接到位
)
```

## 7. 测试与验证

| 检查 | 结果 |
|---|---|
| `tests/foundation/test_voice_hud.py` | **92 OK**（63 → 92；新增 A 9 + B 5/防闪 2 + C 9 + 拖动手感 1…按文件内类名可见） |
| 全量 `discover` | **2747 OK (skipped=3)**（2718 → +29），153.3 s |
| `compileall -q app tests tools` | exit 0 |
| 关键新用例 | 5 条跑**真实** `_wnd_proc`（只换 user32 替身）：跟手且不调 `SetTimer`、松手不二次移动、贴边吸附、离边不吸附、缓动有中间帧；另有系统动画开关、进场滑入、退场归位、防闪护栏等 |

## 8. 风险 / 未做

1. 进/退场动效是**内联阻塞**（进场 260 ms、退场 180 ms，只发生在 `show()`/`hide()` 两个点，不进对话/播报路径）；
2. `state_animation_ms>0` 时 `LISTENING→SPEAKING` 会内联阻塞该时长（**默认已关**，且受 200 ms 护栏约束）；
3. 合成帧缓存约 0.6 MB 内存（上限 8 条，超出清空）；
4. 动效帧缓存依赖"窗口句柄级 DIB"，只在 `pixel` 模式生效；`legacy` 降级路径一切动效自动跳过；
5. **真机 A/B/C 手感与观感尚未由用户复验**（命令见 §9）；
6. `_DRAG_TIMER_MS` 保留但**已不再使用**（只为不改动模块级名字）。

## 9. 真机复验命令

```powershell
cd '<PROJECT_ROOT>'
# 零成本：只看拖动 + 待机↔聆听
.\.venv\Scripts\python.exe -m app.main --voice --auto --tts-mode none
# 全状态（含"播报"，需真实 Provider，计费）
$env:ASSISTANT_PROVIDER='deepseek'; $env:ASSISTANT_DEEPSEEK_OPT_IN='true'
$env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
.\.venv\Scripts\python.exe -m app.main --voice --auto --tts-mode ws_pcm --tts-autostart off
```

## 10. 回滚

- `state_animation_ms=0` / `show_fade_ms=0` / `hide_fade_ms=0` / `drag_snap_px=0` 逐项关闭；
- 整批回滚＝revert 本批对 `app/ui/voice_hud.py` 与其测试的改动；不影响语音链路、自动收音、唤醒词与声纹。
