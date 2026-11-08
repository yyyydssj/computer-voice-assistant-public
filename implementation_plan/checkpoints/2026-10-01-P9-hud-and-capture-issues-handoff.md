# P9 UI 交接：右下角「正在对话」提示窗 + 两个待修问题

> 日期：2026-10-01（本地）
> 阶段：P9（VAD、连续对话、唤醒词与声纹过滤）——批次 1-2 + UI 批次 1-2 已完成，阶段仍 `in_progress`
> 本文件是**下次新对话的最小读取入口之一**；上位交接：`../checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`
> 全量基线：**1978 tests OK (skipped=3)**；`compileall -q app tests tools` exit 0
> **2026-10-01 追加（UI 批次 2）**：可见文案由 9 态收敛为 4 态（待机/聆听/播报/失败），并修复两处真机缺陷，见 §1.5；原「待修问题 A」已定位并修复，见 §2。
> **2026-10-01 追加（修复批次 3）**：§3 待修 B 已定位并修复（Silero **段级端点** + 尾段定稿 + 结束判定时间线）；§2 遗留①「阶段级时间线打点」已完成（内部 9 态 + 单调毫秒落 artifact）；另新增输入停滞看门狗。
> **2026-10-01 追加（修复批次 4）**：§2 遗留②「阻塞段前后抽消息点」已完成（共享模块 `app/ui/voice_status.py`）；`PushToTalkCoordinator.release()` 改为**先排空总线再解码**（§3.5 已关闭）；**`app/main.py --voice` 已接提示窗**（`--no-hud` 可关）；配置基线见 §5。**真机复测/参数回填仍待用户执行**。
> **2026-10-01 追加（B3/B4）**：唤醒词门禁与声纹误触发过滤的**代码/接线/离线测试已完成**（默认 off）；真实模型不在仓库内，真机验证与下载授权见 `2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`。

## 1. 本批次已完成（UI 批次 1：右下角状态提示窗）

| 文件 | 说明 |
|---|---|
| `app/ui/voice_hud.py` | 零依赖 `ctypes` 自绘 Win32 置顶小窗；内部 9 态、**可见文案 4 态**（UI 批次 2 收敛）；可注入 `VoiceHudApi`；真实 `build_win32_api()`（手写 `ctypes.Structure` + user32/gdi32；`WS_POPUP | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE`；`PeekMessageW` 非阻塞；鼠标拖动；**绝不 `SetForegroundWindow`**）；窗口文本只由状态生成，**不显示识别/回复正文、路径、异常正文** |
| `tests/foundation/test_voice_hud.py` | 55 项离线测试（注入 fake api；含内容不泄漏、4 态收敛与 fail-closed 固定短码断言） |
| `tools/p9_auto_voice_runner.py` | `--no-hud`（默认显示 HUD）、`_build_hud/_hud_set/_hud_tick/_safe_print`、会话只读阶段回调 `set_event_sink()`、`_wrap_loop_for_hud()` 把状态挂到真实动作上；UI 批次 2 修复"`hud` 被覆盖成 `None`"与「失败」误报（见 §1.5） |

### 状态文案（固定，UI 批次 2 收敛为 4 种）

| 可见文案 | 映射的内部状态 |
|---|---|
| **待机** | `IDLE`、`STOPPED`、`DEGRADED` |
| **聆听** | `LISTENING`、`HEARING`、`TRANSCRIBING`、`THINKING` |
| **播报** | `SPEAKING` |
| **失败** | `FAILED`（仅真实播报失败 / 真实故障） |

标题固定「电脑语音助手」；正文**只有一行状态文案**，不再显示「第 N 轮」（`turn_index` 参数保留、属性仍可读，只是不渲染）。

> UI 批次 1 的 9 种文案（正在监听…/听到了，正在收音…/识别中…/思考中…/播报中…/语音不可用（文字模式）/本轮失败/已停止）**已废止**；`VOICE_HUD_SCHEMA_VERSION` 由 1 升到 2。
> 原因：细粒度文案与真实阶段名不副实（「识别中」实际只存在几微秒，ASR 解码全程落在「思考中」标签下），用户观感即"状态和操作不同步"。
> 内部 9 态枚举**完整保留**，未来做阶段时间线诊断时仍可用。

### 真机验证过的事实（用户参与）

| 项 | 结论 |
|---|---|
| 窗口创建/位置 | 1920×1080 主屏，`GetWindowRect=(1576,960)-(1896,1056)`（右下角）；`IsWindowVisible=True` |
| 置顶/无任务栏/不抢焦点 | `exstyle` 含 TOPMOST/TOOLWINDOW/LAYERED/NOACTIVATE；`GetForegroundWindow` 前后一致、`stole_focus=False` |
| **重绘确实发生** | GDI `PrintWindow` + `GetDIBits` 像素比对：`LISTENING` 与 `SPEAKING` 两状态窗口渲染 SHA-256 不同（`40466021…` vs `c3f0e953…`） |
| 用户肉眼 | 用户确认"看到了，右下角有小窗" |
| `UpdateWindow` 修复 | 先前 `show()` 只 `InvalidateRect`，首帧要等后续系统消息才绘制（表现为"播报完才出现、内容还是旧帧"）；已在 `invalidate()` 补 `UpdateWindow` 同步重绘 |

## 1.5 UI 批次 2：文案收敛为 4 态 + 修复两处真机缺陷（2026-10-01 追加）

**需求来源**：用户要求"简化弹窗，状态只分为待机/聆听/播报"，并在真机上反馈"全程都是待机"。

**改动（4 个文件）**：

| 文件 | 改动 |
|---|---|
| `app/ui/voice_hud.py` | `STATE_LABELS` 由 9 项收敛为 4 种文案（9 态映射见 §1）；`hud_text()` 不再渲染「第 N 轮」；`VOICE_HUD_SCHEMA_VERSION` 1 → 2；模块/枚举/`set_state` 文档同步 |
| `tests/foundation/test_voice_hud.py` | 文案断言同步为 4 态；新增"9 个内部状态只产出 4 种文案"收敛断言（54 → 55 项） |
| `tools/p9_auto_voice_runner.py` | ① 删掉 `main()` 里把 `hud` 重新赋值为 `None` 的那一行（"全程待机"根因）② 新增 `TEXT_ONLY_REASON` / `_speech_result()`，「失败」只在真实 `speech_failed` 时才显示 |
| `tests/integration/test_p9_auto_runner.py` | 新增 `RecordingHud` + `HudWiringTests`（2 项）+ `SpeechOutcomeLabelTests`（4 项，含"真实失败仍显示失败"负路径）；`_paced_run_turn` 支持转发 `hud` |

**验证（修复前先红、修复后绿）**：

| 项 | 结果 |
|---|---|
| 窗口实际收到的状态序列（注入记录器跑完整 `main()`） | `idle → listening → hearing → transcribing → thinking → speaking → listening → stopped`；修复前仅 `['idle']` |
| `tests/integration/test_p9_auto_runner.py` | Ran 50, OK |
| `tests/foundation/test_voice_hud.py` | Ran 55, OK |
| 全量回归 | **Ran 1808 tests, OK (skipped=3)**（该批次当时值；修复批次 3 后为 **1849**，见 §3） |
| `compileall -q app tests tools` | exit 0 |

**尚未做**：真机肉眼复验 4 态（命令见 §5，需用户执行并登记）。文档更正已完成（本节即该项）。

## 2. 待修问题 A：提示窗状态与用户操作"不同步"（**已定位并修复**）

**用户原话**：「'播报中''思考中'这类的和当前状态不一致」「还是不同步」「全程都是'待机'」。

**根因（已定位、已修复，见 §1.5）**：

1. **主因（决定性）**：`tools/p9_auto_voice_runner.py::main()` 在建窗之后又写了一次
   `hud: Any | None = None`，把刚建好的提示窗**覆盖成 `None`**。于是 `_hud_set(hud, "IDLE")`
   之后所有状态更新都成了空操作，`_wrap_loop_for_hud(loop, None)` 也直接返回（阶段钩子根本没挂上），
   窗口**恒为「待机」**；退出时 `if hud is not None` 为假，窗口也不再被隐藏/销毁。
   这同时解释了"全程都是待机"与更早的"和当前状态不一致"。
2. **次因（观感放大）**：旧文案把 `release_input()`（只做状态迁移、几微秒）标成「识别中…」，
   而 `complete_turn() → driver.release() → coordinator.release()`（ASR 解码 + Agent 往返）
   全程显示的是「思考中…」——标签名与真实阶段不符。
3. **非根因（此前猜测，已排除）**：不是绘制问题（`set_text → invalidate → InvalidateRect + UpdateWindow`
   同步重绘）；不是状态映射缺失。"说完判定滞后"确有其事，但**不是本问题的原因**。
4. **修复过程中发现的第三个缺陷**：`speak_with_hud` 把 `_speak` 返回的 `(False, "text_only")`
   （本轮没有播报通道，例如 `--tts-mode none`）当成播报失败 → 成功的一轮会闪出「失败」。
   已改为只有真实 `speech_failed` 才置「失败」。

**教训（写进纪律）**：展示层"静默失效"必须有测试兜住——本批次之前所有 runner 离线测试都把
`_build_hud` patch 成 `return_value=None`，因此**没有任何测试跑过"带真实 HUD 对象的 `main()`"**，
这个必现缺陷才能一路通过全量回归。

**遗留**：

- ① **阶段级时间线打点 → 已完成（修复批次 3）**：`tools/p9_auto_voice_runner.py` 新增 `HudTimeline` /
  `_TimelineHud`（记录内部 9 态变化 + 单调毫秒；相邻重复去重、上限 64 条、记录失败完全隔离），
  每轮写进 artifact 的 `hud_timeline`（`entries` / `durations_ms` / `truncated`）并打印「提示窗阶段耗时」；
  **`--no-hud` 或建窗失败时同样记录**（诊断不依赖窗口是否存在）。实测样例（合成帧，"识别中/思考中"
  恰为 0 ms，正是 §2 次因的量化证据）：
  `listening 110ms → hearing 2828ms → transcribing 0ms → thinking 0ms → speaking 0ms`。
- ② **ASR/Agent 阻塞期间窗口消息抽点 → 已完成（修复批次 4）**：共享实现
  `app/ui/voice_status.py::attach_stage_hooks` 在每段阻塞动作（`release_input` 定稿 /
  `complete_turn` 解码+Agent）**前后各抽一次**窗口消息，播报仍按推片抽；**不创建线程**
  （`voice_hud` 的硬边界）。真机限制如实说明：多秒级阻塞期间窗口消息仍不被处理，只能靠
  进入阻塞前把积压排空（这是不建线程前提下的上限）。
- ③ **参数标定 → 半完成**：配置口径与决策规则已写入 §5「配置基线」；**真机数值仍需你跑一次**
  （`tools/p9_mic_level_calibration.py` 的 quiet/speech 两趟）后回填。§3 段级端点上线后
  `silence_ms` 已降为兜底参数，主路径由 `--vad-min-silence-ms`（默认 500ms）决定。

## 3. 待修问题 B：收音截断──"最后一个字有时被吞"（**已定位并修复**）

**用户原话**：「有时候最后一个字就被吞了，有时候正常」。

### 3.1 现象与证据（真机，修复前）

- 同一天真机上，识别结果时好时坏：`'你好，小饼。'`（正确）、`'你好你好，小饼。'`（多字）、
  `'。'`（几乎全丢）、`'看的用我给弄一份垃圾。'`（错乱）。
- 结束时好时坏：既有 `silence_timeout`（说完自动结束，正确），也有 `max_utterance_duration`（拖到上限，识别被拉长后错乱）。
- 该轮 `frames` 会明显偏大（例如 750/1000 帧 = 15/20 秒），而用户实际只说了一两秒。

### 3.2 根因（已核实）

1. **主因**：项目只用了 Silero 的**帧级** `is_speech_detected()`，把 sherpa-onnx **自带的段级端点**整套丢掉了。
   段级端点（`empty()` / `front` / `pop` / **`flush()`** + `min_silence_duration` 宽限）才是"人声段结束"的判定；
   帧级判定在真实环境噪声下会持续命中，`_consecutive_silence` 被反复清零 → 永远等不到 `silence_ms` →
   只能撞 `max_utterance_duration`（15–20 s）→ ASR 拿到整段噪声 → 识别错乱/末尾丢字。
   原先"帧/窗错位已修但仍不稳定"的困惑由此解释：粘滞补丁只修了**起始确认**，没解决**结束判定**。
2. **附带（已修，修复批次 4）**：`PushToTalkCoordinator.release()` 原先不排空总线就 `finish()` 解码，
   已采集但未 pump 的帧会被丢弃（量小，20–60 ms）。现改为**收尾前先 `pump()` 排空**；
   若排空期间 VAD/provider 报故障则沿用 `runtime_failed` 语义、绝不覆盖状态。

### 3.3 修复（修复批次 3：3 个源文件 + 3 个测试文件）

| 文件 | 改动 |
|---|---|
| `app/audio/vad/silero.py` | 新增段级只读事实 `segment_pending()`（`empty()` 取反）、`flush_segment()`（尾段定稿）+ `segment_api_available` / `segment_probe_errors` / `flush_calls` 计数；不可判定一律返回 False、异常不抛出 |
| `app/voice_loop/auto_driver.py` | 结束判定顺序改为：未起始硬超时 → `max_utterance_ms`（硬上限）→ **段级人声段结束**（`end_source=segment`，仍按 `silence_timeout` 正常收尾提交）→ 帧级静音兜底；`release()` 前 `flush` 尾段；新增 `observation_details()` 时间线 |
| `tools/p9_auto_voice_runner.py` | 新增 `--vad-min-silence-ms`（默认 500）与段级钩子接线；artifact 每轮新增 `end_decision`；另新增输入停滞看门狗 `--input-stall-ms`（默认 5000）与总线容量 64→500 帧 |
| 测试 | 新增 `tests/voice_loop/test_auto_driver_segment_end.py`（16 项，含 2 项真实音频对照）；`tests/audio/test_silero_vad.py` +12 项（含 2 项真实模型段级冒烟）；`tests/integration/test_p9_auto_runner.py` 增段级接线、看门狗与 HUD 时间线用例 |

### 3.4 证据：真实语音片段 + 真实 Silero 模型（离线，无麦克风）

同一条 1.98 s 语音，只投静音窗，观察谁先关掉输入边界：

| 判定路径 | 说完后需要的静音 | frames | `end_source` |
|---|---|---|---|
| **新：段级端点** | **0.42 s（13 窗）** | 75 | `segment` |
| 旧：帧级静音 2500 ms | 2.91 s（91 窗） | 153 | `silence_frames` |

两条对照已固化为 `tests/voice_loop/test_auto_driver_segment_end.py::RealAudioSegmentEndpointTests`（`skipUnless` 真实模型）。

### 3.5 尚未做

- **真机复测**（需用户执行，命令见 §5）：预期 `end_decision.end_source=segment`、说完后约 0.5 s 内收尾、末字完整；
  若句中停顿被误判成说完，调大 `--vad-min-silence-ms`（600–800）。
- **参数校准**（原"下次要做的 3"）：用 `tools/p9_mic_level_calibration.py` 配一组真机稳定值并写进配置说明——
  仍未做；`--vad-min-silence-ms` 默认 500 只是起点。
- **`release()` 排空总线 → 已完成（修复批次 4）**：先 `pump()` 再 `finish()`。验证：
  采集 3 帧但**故意不 pump**，旧实现下 provider 收到 **0** 帧（`AssertionError: 0 != 3`），
  新实现收到 **3** 帧；回归测试 `tests/integration/test_p6_integration.py::test_release_drains_queued_frames_before_decoding`。

### 3.6 顺带排查：音频总线溢出（**结论纠正**，避免以后误读）

- 起因：怀疑 runner 每轮固定 `sleep(20ms)` 会让消费者永久落后于采集，最终撑爆 64 帧（1.28 s）容量。
- 核实结论：**该推断不成立**。`PushToTalkCoordinator.pump()` 每次调用都把总线一次抽干，队列只可能积压
  "一次迭代内新到的少数帧"。确定性仿真（单轮 25 ms 开销、20 s、50 帧/秒采集）：
  容量 64 + 旧策略 / 容量 500 + 旧策略 / 容量 500 + 新策略 **全部 OPEN，1000/1000 帧全部被消费**。
- 但排查中确有一个**真缺陷**：总线一旦失效（`overflow`，或设备重开导致的 `frame_discontinuity`——
  `AudioBus.publish` 两种情况都 `_fail_locked` **并清空队列**），此后 `pump()` 永远返回 0 帧 →
  驱动的 `_elapsed_ms` 冻结 → 自动结束永不触发 → 旧代码要空转到
  `speech_start_timeout_ms + max_utterance_ms`（默认 **23 s**）才以 `stalled` 收尾，且控制台无任何提示。
- 已修：新增**输入停滞看门狗**（`--input-stall-ms`，默认 5000；只在"完全没有帧"时生效，不影响"有环境声没人说话"的空轮）
  + `idle_seconds` 落 artifact + 明确提示；并把轮询改为"有帧就不睡"（`_turn_idle_sleep_seconds`）。
- 容量 64→500 是**纯余量**（最坏多占 ~300 KB PCM），**不是**根因修复；如认为无必要可单独回退。

### 3.7 真机复现并修复：上一轮"残留段"让下一轮 2 帧收尾（2026-10-01，B3 真机试听时发现）

**现象（用户真机日志）**：第 1 轮正常（`frames=112`、识别 `'你能听见我说话吗？'`、`end_source=segment`）；
第 2 轮 `frames=2 hits=2 segment_ends=1`、识别出 `'。'`，**却仍然 accepted/submitted/spoken 并播报**——
用户观感是"我话还没说完就开始播报了"。

**根因**：Silero 的"已结束段"一旦产出就留在 `VoiceActivityDetector` 队列里（本项目从不 `pop`），
`segment_pending()` 因此在**下一轮第 1 帧**就返回 True；而残留段的物理长度只有 1~2 帧（40ms），
本轮于是 40ms 就被判"说完了"。第 1 轮正常，是因为开局队列为空。

**修复（3 处）**：
1. **每轮重置底层 VAD**：`AutoVoiceSession._reset_speech_detector()`（runner 侧，在 `start_turn()` 之前）
   → `SileroVadDetector.reset()`，清掉残留已结束段与计数；注入替身没有 `reset` 时是安全空操作。
2. **驱动新增 `min_utterance_ms`（默认 300）**：已确认起始后**至少收满这么久**才允许"说话结束"类判定
   （段级端点 / 帧级静音）收尾；`max_utterance_ms` 硬上限不受影响（仍优先）。runner 暴露 `--min-utterance-ms`。
3. **唤醒词门禁可见化**：打印 `⏳ 等待唤醒词「小饼」…` / `✔ 听到唤醒词：开启新一轮收音` / `✖ 未听到唤醒词…`，
   否则从日志看不出门禁是否生效。

**回归**：`tests/voice_loop/test_auto_driver_segment_end.py`（最小时长 3 项，含"不得绕过硬上限"）、
`tests/integration/test_p9_auto_runner.py::LeftoverSegmentTests`（每轮重置 + CLI 2 项）、门禁可见性 2 项。
全量 **1972 tests OK (skipped=3)**、`compileall` exit 0。

## 4. 下次新对话的最小读取集合（按顺序）

1. 本文件；
2. `NEXT-SESSION.md`（顶部「当前阶段」与未完成表）；
3. `implementation_plan/checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`（§5.2 主路线 / §5.4 HUD / §6 三个真机缺陷）；
4. `implementation_plan/checkpoints/2026-09-30-P9-batch1-2-interface-freeze.md`（P9 B1/B2 冻结接口 + §6.1 实现期裁定）；
5. 需要动代码时再读：`app/ui/voice_hud.py`、**`app/ui/voice_status.py`（阶段→提示窗的共享接线）**、`app/voice_loop/auto_driver.py`、`app/audio/vad/silero.py`、`app/audio/vad/auto_capture.py`、`tools/p9_auto_voice_runner.py` 与对应测试（HUD 侧看 `tests/foundation/test_voice_hud.py`、`tests/foundation/test_voice_status.py`、`tests/foundation/test_main.py::VoiceHudWiringTests` 与 `tests/integration/test_p9_auto_runner.py::HudWiringTests` / `SpeechOutcomeLabelTests` / `HudTimelineTests` / `HudTickDuringBlockingPhasesTests`；段级端点看 `tests/voice_loop/test_auto_driver_segment_end.py` 与 `tests/audio/test_silero_vad.py::SegmentEndpointTests`；总线排空看 `tests/integration/test_p6_integration.py::test_release_drains_queued_frames_before_decoding`）。

## 5. 环境事实（下次直接用，别重新摸索）

| 项 | 值 |
|---|---|
| 主路线 TTS | VoiceStudio v0.5.6 后端：`<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\.venv\Scripts\python.exe backend\main.py`（uvicorn 监听 `127.0.0.1:3900`，cuda / RTX 3050 Laptop） |
| **坑** | 直接运行 `VoiceStudio.exe` **无效**（进程立即退出、3900 不监听） |
| 备用 TTS | `--tts-mode edge`（本机离线 SAPI 声线 `Microsoft Huihui Desktop`）；曾修复 edge 播报被 `PCMPlaybackBackpressure` 掐断的缺陷 |
| ASR | 本地 sherpa-onnx SenseVoice：`models/sense-voice/model.int8.onnx` |
| VAD 模型 | `models/openwakeword/silero_vad.onnx`（sherpa-onnx `VoiceActivityDetector`，`window_size=512@16kHz`） |
| 输入设备 | 麦克风阵列 (Realtek(R) Audio)，设备 id `1` |
| 全量回归 | `.\.venv\Scripts\python.exe -c "import sys,unittest,io; sys.path.insert(0,'.'); print(unittest.TextTestRunner(stream=io.StringIO(),verbosity=0).run(unittest.TestLoader().discover('tests',top_level_dir='.',pattern='test_*.py')).testsRun)"` → 当前 **1870** |
| 真机试听命令（**主路线 VoiceStudio**） | 先确认 `http://127.0.0.1:3900/health` 返回 200（`device=cuda`），再 `.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 1 --tts-mode ws_pcm`（默认即 `ws_pcm` + `--vad-mode silero` + 段级端点 + `--vad-min-silence-ms 500`）。预期：提示窗 **待机 → 聆听 → 播报 → 待机**；控制台 `结束判定：end_source=segment …`、`提示窗阶段耗时：…`；artifact `end_decision.end_source=segment`、`hud_timeline.entries` 含内部 9 态；播报诊断 `route=voicestudio_ws_pcm`、`error=None` |
| 备注（参数） | 句中停顿被误判成说完 → 调大 `--vad-min-silence-ms`（600–800）；采集停滞误报 → 调大 `--input-stall-ms`（默认 5000）。`--silence-ms` 在 silero 模式已降为兜底 |
| 真机试听命令（能量判定备选） | `.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 1 --tts-mode ws_pcm --vad-mode energy --speech-threshold 500 --silence-ms 2200 --max-record-seconds 10` |

### 5.1 配置基线（参数口径与决策规则，§2 遗留③ 的文档半）

**主路线（silero + 段级端点）推荐起点**

| 参数 | 默认 | 何时改 |
|---|---|---|
| `--vad-mode silero` | 默认 | 保持；`energy` 只作对照 |
| `--vad-min-silence-ms` | 500 | 句中停顿被当成"说完了"（切太早）→ 600~800 |
| `--vad-threshold` | 0.5 | 音乐/风扇误触发多 → 0.6~0.7；轻声漏检 → 0.4 |
| `--vad-frames` | 2 | 环境声多 → 4~6（更保守的起始确认） |
| `--silence-ms` | 2500 | silero 模式已降为**帧级兜底**；仅段级端点不可用时生效 |
| `--input-stall-ms` | 5000 | 采集停滞误报（长 GC 等）→ 8000 |
| `--max-record-seconds` | 15 | 硬上限；**撞到它就说明结束判定没生效**，应查 `end_decision` |

**本机标定事实（2026-10-01，历史值，待复核）**：噪声 `p50≈43 / max≈130 RMS`、说话 `p50≈1949 / p95≈8393`；
能量阈值 600 对能量模式约 15 倍余量。silero 模式下这些数字只用于**判断异常**（例如起始不稳定）。

**回填流程（需你执行一次）**：

```powershell
.\.venv\Scripts\python.exe tools\p9_mic_level_calibration.py --live-opt-in --seconds 6 --label quiet
.\.venv\Scripts\python.exe tools\p9_mic_level_calibration.py --live-opt-in --seconds 6 --label speech
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 2 --tts-mode ws_pcm
```

把两趟标定的分位数，与整轮 artifact 里 `auto_capture.speech_threshold_rms` / `vad_min_silence_ms` /
`input_stall_ms` / `audio_capacity_frames` 抄回本表，即完成"真机稳定值"记录。

**可复核口径**：每轮 artifact 的 `end_decision`（`end_source` / `frames` / `hits` / `max_silence_run_ms` /
`last_hit_elapsed_ms`）与 `hud_timeline`（内部 9 态 + 毫秒）就是判参依据——先看 `end_source` 是否为 `segment`，
再看 `max_silence_run_ms` 与 `silence_ms` 的差距。

## 6. 明确未做（禁止顺手扩项）

- **`app/main.py --voice` 已接提示窗（修复批次 4）**：默认显示（`--no-hud` 可关闭），窗口不可用时
  如实提示并继续；语音闭环仍是**按键**模式。
- **未接默认运行时自动收音**：`app/main.py --voice` 仍按 Enter 触发一轮，未换成 `AutoCaptureVoiceDriver`
  （交互模型会变，需单独授权）。
- 未做系统 toast（调研过 `winotify`，零依赖 MIT，未采用）。
- P9 B3 唤醒词（`sherpa_onnx.KeywordSpotter` 本机可用）、B4 声纹（`SpeakerEmbeddingExtractor`）**代码与接线已完成**
  （`app/audio/kws.py` / `app/audio/voiceprint.py` + runner `--kws-mode` / `--voiceprint-mode`，默认 off），
  但**真实模型不在仓库内**，真机验证待授权下载：见 `2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`。
- 未做 AEC/全双工（P10）。
- **参数真机回填未做**（§5「配置基线」已给口径与规则，等你跑一次标定）。
- 本轮全部改动**未提交、未 push**（工作区另有 P7-R-29 那批未提交改动）。
