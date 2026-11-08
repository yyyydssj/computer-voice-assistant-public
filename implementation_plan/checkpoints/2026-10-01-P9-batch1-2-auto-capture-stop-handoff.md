# P9 批次 1-2 停止交接：自动收音与连续对话边界（含真机试听）

> 日期：2026-10-01（本地）  
> 阶段：P9（VAD、连续对话、唤醒词与声纹过滤）——**批次 1-2 完成，阶段仍为 in_progress**  
> 前置：`P8-handoff.md`、`implementation-roadmap.md` P9 卡、`2026-09-30-P9-batch1-2-interface-freeze.md`
> 停点：**P9 B3（唤醒词）与 B4（声纹）未开始**；未接入 `app/main.py` 默认运行时；未提交、未 push。
> **后续更新（2026-10-01 UI 批次 1-2）**：右下角提示窗已收敛为 4 态（待机/聆听/播报/失败），并修复「`main()` 覆盖 `hud` 导致全程待机」等两处缺陷；§5.4 的"逐段投射/实测逐段变化"已更正。**当前全量基线 1808 tests OK (skipped=3)**；详见 `2026-10-01-P9-hud-and-capture-issues-handoff.md`。

## 1. 本批次唯一目标

在已稳定的 P8 按键半双工闭环上加入**自动收音**与**连续对话边界**，且 VAD/自动收音只决定输入边界，绝不决定授权、计划或执行。

## 2. 明确不做（仍然有效）

- 不做唤醒词（B3）、不做声纹过滤（B4）、不做 AEC/播放期间监听/全双工（P10）。
- 不允许 VAD/自动收音决定授权；误触发不产生计划、授权、执行或工具调用。
- 不复用旧 `utterance`/`request`/授权/音频缓冲；窗口关闭或失效后旧边界一律作废。
- 不修改 `app/main.py`、`app/runtime/voice_entry.py`、`app/safety/**`、`app/executor/**`、`app/plans/**`、`app/auditing/**`、`app/verification/**`、`app/tools/**`、`app/tts/**`、`app/providers/**`。

## 3. 新增/修改文件（实际）

```text
新增 app/dialog/__init__.py            对话包导出（含 DIALOG_INPUT_REFUSAL_REASONS）
新增 app/dialog/state.py               DialogState 11 态、迁移白名单、accepts_input、recover 换 window_id
新增 app/dialog/window.py              连续对话窗口：闲置/整段/轮数上限、注入时钟、不可复活
新增 app/dialog/coordinator.py         turn claim、输入许可、授权/执行期间只读拒绝
新增 app/audio/vad/auto_capture.py     语音起始检测 + 被动 pre-roll 缓冲 + 自动输入边界
新增 app/voice_loop/auto_driver.py     免按键自动收音驱动（复用 coordinator + SpeechStartDetector）
新增 tools/p9_auto_voice_runner.py     真机免按键试听 runner（--live-opt-in 必需）
新增 tools/p9_mic_level_calibration.py 麦克风电平标定（只报 RMS 分位数，音频不落盘）
新增 tests/dialog/{__init__,test_state,test_window,test_coordinator}.py        56 项
新增 tests/audio/test_auto_capture.py                                          52 项
新增 tests/voice_loop/test_auto_driver.py                                      27 项
新增 tests/integration/test_p9_auto_runner.py                                  9 项（离线契约）
新增 tests/integration/test_p9_batch1_2_audit.py                               62 项（只读审计）
新增 tests/integration/test_p9_turn_abandon.py                                 5 项（真机缺陷回归）
新增 tests/integration/test_p9_edge_playback_pacing.py                         4 项（edge 播报背压回归）
新增 app/audio/vad/adaptive.py          环境自适应阈值（B2.1；噪声采样 → floor/multiplier/ceiling，fail closed）
新增 tests/audio/test_adaptive_threshold.py                                    45 项
新增 app/audio/vad/silero.py            Silero VAD 人声判定适配器（B2.2；512 整窗聚合、粘滞、固定错误码）
新增 tests/audio/test_silero_vad.py                                            47 项

修改 app/voice_loop/__init__.py        仅追加 AutoCaptureVoiceDriver 等导出（纯增量）
修改 app/asr/push_to_talk.py           仅新增 abandon_turn(reason) + _subject_id()（纯增量，见 §6）
修改 app/asr/recovery.py               仅新增只读属性 config（纯增量）
修改 app/runtime/voice_entry.py        EdgeStream 按播放进度节流 + 播报机械诊断（见 §5.1）
修改 app/voice_loop/auto_driver.py     新增 speech_detector / speech_detector_frames_required（silero 接线）+ 判定顺序修正
修改 tools/p9_auto_voice_runner.py     --threshold-mode/--vad-mode/--vad-threshold/--vad-frames、vad_hits 诊断、播报诊断
修改 tests/integration/test_p9_auto_runner.py  silero/adaptive 装配与回归用例（含 2 处断言改为读 _p9_vad_detector）
修改 implementation_plan/，task_plan.md，progress.md，NEXT-SESSION.md（文档收口）
```

## 4. 验证结果（真实数字）

| 命令 | 结果 |
|---|---|
| `unittest discover -s tests -t . -p "test_*.py" -q` | **Ran 1747 tests, OK (skipped=3)**（基线 1394；含 B2.1/B2.2 后为最终数字） |
| `tests/dialog` | Ran 56, OK |
| `tests/audio` | Ran 127, OK（含 auto_capture 52） |
| `tests/voice_loop` | Ran 94, OK（含 auto_driver 27） |
| `tests/integration` | Ran 134, OK（含 B1/B2 审计 62、runner 契约 9、放弃轮次 5） |
| `compileall -q app tests tools` | exit 0 |
| 只读审计 `test_p9_batch1_2_audit.py` | 62/62 OK：冻结名称/签名/枚举逐条比对、严格 UTF-8 无 BOM/U+FFFD、执行面 AST（危险调用 0、禁用导入根 0）、`app/dialog` 与 `auto_capture` 安全链引用数 0 |
| 对抗性尝试 | 伪造 claim、跨窗口复用、invalidate 后 observe/request_input、deliver 后 observe、非连续 sequence、max_turns 边界、双超时同时到期：全部被正确拒绝 |

## 5. 真机试听证据（用户本人执行）

| 项 | 事实 |
|---|---|
| 命令 | `python tools/p9_auto_voice_runner.py --live-opt-in --turns 1 --tts-mode edge --silence-ms 3000 --speech-start-timeout-ms 25000 --max-record-seconds 10` |
| 输入设备 | 麦克风阵列 (Realtek(R) Audio)，设备 1 |
| ASR | 本地 sherpa-onnx SenseVoice `models/sense-voice/model.int8.onnx` |
| 免按键 | 是（`button_required=false`，未按任何键） |
| 自动起始 | `▶ 检测到语音`（`speech_seen=true`） |
| 识别结果 | **`'你好，小饼，测试一下自动收音，看看识别。'`** |
| 提交 | `submitted=true`（同一个 `ConversationManager`） |
| 播报 | `spoken=true` 且修复 edge 播报后**用户本人确认听到**（`user_heard=true`，见 §5.1） |
| 结束方式 | `max_utterance_duration`（该段音频没有出现持续 3s 的静音） |
| artifact | `implementation_plan/checkpoints/artifacts/2026-09-30-P9-auto-voice-loop/p9-auto-voice-loop-20261001T022233Z-88878d31faa74358.json`（只记长度与 SHA-256，未落盘原始音频） |

电平标定（同一设备，`tools/p9_mic_level_calibration.py`）：

| 标签 | p50 | p95 | max | ≥600 比例 |
|---|---|---|---|---|
| quiet（安静环境） | 43.2 | 55.0 | 130.3 | 0% |
| speech（正常说话） | 1949.4 | 8392.7 | 11448.8 | 58.7% |

结论：默认阈值 600 RMS 对**本机**是合适的（噪声 max 130，说话 p50 约 1949，余量约 15 倍）。

**听感确认：`user_heard=true`（用户 2026-10-01 明确确认"听到了，声音清楚"）。设备/软件本身也经隔离验证正常：**

| 隔离测试 | 结果 |
|---|---|
| SAPI 直接 `Speak()` | 用户听到 ✅ |
| 原始 440Hz PCM 经 winmm `waveOut`（`waveOutOpen rc=0`/`waveOutWrite rc=0`） | 用户听到 ✅ |

### 5.1 真机暴露的第三个缺陷：edge 播报被背压掐断（**P9 之外，既有缺陷**）

- 现象：`spoken=true` 但用户听不到任何声音；`spoken` 只是"闭环把完整回复交给了播报通道"，不代表出声。
- 诊断（runner 已加 `播报诊断` 输出）：`{'route': 'edge_sapi_local', 'chunks': 40, 'error': 'PCMPlaybackBackpressure'}`
- 根因（只读探针实测，同一句、真实设备）：
  ```text
  [real_waveout] chunks=40 bytes=384000 synth_s=0.38 audio_s=8.00
                 state=QUEUED error=PCMPlaybackBackpressure: queued_audio_limit
  [wide_queue]   chunks=42 synth_s=0.38 error=None final_state=COMPLETED total_s=8.67
  ```
  离线 SAPI 合成远快于实时（0.38 s 产出 8.4 s 音频），而 `PCMPlaybackWorker` 默认 `max_queued_audio_ms=8000`（=384000 字节）；`app/runtime/voice_entry.py::EdgeStream.push_text_chunk` 全速推入全部 chunk，队列立即顶格 → `push_chunk` 抛 `PCMPlaybackBackpressure` → `EdgeStream` 捕获后只记 `error` 字段、静默返回 → **播放从未开始**（`state=QUEUED`，从未进入 `PLAYING`）。
  把队列上限放宽到 60 s 后，同一段音频真实播出并 `COMPLETED`（8.67 s），证明设备与音频格式本身没有问题。
- 影响范围：所有"合成快于实时"的离线播报路径（`edge` 声线），且任何**时长超过队列上限**的回复必然受影响；P8 真机验收当时未暴露该路径。
- 状态：**已修复并真机复测通过（2026-10-01，用户本人授权）**。
  - 修复：`app/runtime/voice_entry.py::EdgeStream.push_text_chunk` 改为按 `PCMPlaybackWorker.queued_audio_ms` **按播放进度节流**（安全水位 6000 ms、20 ms 轮询、总等待上限 60 s、背压竞态最多重试一次），并新增 `queued_audio_ms_peak` / `throttle_waits` / `gate_to_device_ms` 机械诊断字段；未修改 `app/tts/**`，未改动任何公共默认值。
  - 复测（真机，同一设备与声线）：`error=None`、`playback_state=COMPLETED`、`queued_audio_ms_peak=6000`、`throttle_waits=129`、`gate_to_device_ms=0.021`、识别 `'你好，小饼测试一下。'`、`submitted=true`。
  - 用户听感：**听到播报，声音清楚** → `user_heard=true`，登记件 `artifacts/2026-09-30-P9-auto-voice-loop/p9-user-heard-confirmation-20261001T023017Z.json`。
  - 回归：`tests/integration/test_p9_edge_playback_pacing.py` 4/4（含"不节流必然撞队列预算"的基线复现与"短句无需节流"对照）；全量 **1620 tests OK (skipped=3)**。


## 5.2 主路线（VoiceStudio `ws_pcm`）真机验收与 B2.1/B2.2 增补（2026-10-01 追加）

主路线环境（用户明确要求：**VoiceStudio 是主路线，edge 只是备用**）：

- VoiceStudio v0.5.6 后端：`127.0.0.1:3900`，`/health` 200，`device=cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`。
- **启动方式（重要）**：直接运行 `VoiceStudio.exe` 无效（进程立即退出、3900 不监听）；必须运行其后端入口
  `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\.venv\Scripts\python.exe backend\main.py`
  （Python 3.11.16，内部即 uvicorn 监听 3900）。本机声线选择件校验通过（`profile_id=e8572e8e`）。

B2.1 环境自适应阈值（`app/audio/vad/adaptive.py` + runner `--threshold-mode auto`）：

- 静音采样 1.2 秒 → `noise p50=42 / p95=113 / max=126 RMS` → `threshold=339`（`source=adaptive`）。
- 效果：`end_reason` 由"撞 15 秒上限"变为正常 `silence_timeout`；但**音乐能量仍在噪声之上**，
  仍被判为语音起始——证明固定/自适应能量阈值无法区分音乐与人声。

B2.2 Silero VAD 接入（`app/audio/vad/silero.py` + 驱动 `speech_detector` + runner `--vad-mode silero` 默认）：

- 真机实测：音乐/键鼠环境里整段 90 秒只有 198 次**零星**命中、无连续命中 → 不再被音乐持续误触发（相较能量判定的根本改善）。
- 修掉一个**接线缺陷**：Silero 按 512 样本整窗判定、采集帧为 320 样本，窗口每 1.6 帧才凑齐，
  直接做帧级"连续命中计数"会永远凑不满阈值（窗口级连续 264 次也不行）。修法：窗口结论粘滞到未满窗的帧，
  帧级阈值取 1（窗口级阈值由 `speech_frames_required` 承担）；同时修正判定顺序——未确认起始时
  `speech_start_timeout` 为硬上限，不再被 `max_utterance` 截断。
- **主路线真机验收通过**（免按键 + 音乐环境）：
  - 识别：`'你好，小饼，主路线自动收音测试能不能听到我说话？你好，小饼主路线自动收音测试…'`（75 字，与用户实际说话一致）
  - 提交：`submitted=true`（同一 `ConversationManager`）
  - 播报：`route=voicestudio_ws_pcm`、`error=None`、`played_segments=3`、`completed_sentences=3`
  - 用户听感：**听到且声音清楚** → `user_heard=true`
  - 登记件：`artifacts/2026-09-30-P9-auto-voice-loop/p9-user-heard-main-route-confirmation-20261001T030624Z.json`
  - 运行 artifact：`artifacts/2026-09-30-P9-auto-voice-loop/p9-auto-voice-loop-20261001T030624Z-e30162aab0024500.json`

### 5.3 全量回归（本文件最终）

`unittest discover -s tests -t . -p "test_*.py"` → **Ran 1747 tests, OK (skipped=3)**；`compileall -q app tests tools` → exit 0。



### 5.4 右下角「正在对话」状态提示窗（P9 UI 批次 1，2026-10-01 追加）

- 需求来源：用户要求"电脑右下角要有提示，显示目前正在和语音助手对话"。
- 技术选型（先做过 GitHub/PyPI 调研）：**零依赖 `ctypes` 自绘 Win32 置顶小窗**；未引入任何新依赖、不联网。
  - 调研结论：`winotify`（零依赖 MIT）只适合一次性 toast，不适合常驻状态；`win11toast` 需 11 个 `winrt-*` 依赖；
    `PyQt-Frameless-Window` 需 PyQt 且 GPL；`tkinter-widgets` 依赖本机缺失的 tkinter；`pywin32`/`PyWinCtl` 只解决窗口控制不解决绘制。
  - 本机事实：`.venv`、系统 Python 3.12、`.python312` **都没有 tkinter**。
- 交付：
  - 新增 `app/ui/voice_hud.py`：`VoiceHudState`（9 态）、`VoiceHudConfig`、`VoiceStatusHud`、可注入的 `VoiceHudApi`、真实 `build_win32_api()`（手写 `ctypes.Structure` + user32/gdi32；`WS_POPUP | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE`；`PeekMessageW` 非阻塞；`WM_LBUTTONDOWN/MOUSEMOVE` 拖动；绝不调用 `SetForegroundWindow`）。
  - 新增 `tests/foundation/test_voice_hud.py`：54 项（全部注入 fake api，离线；含"不泄漏识别正文/路径/异常正文"与 fail-closed 固定短码断言）。
  - 修改 `tools/p9_auto_voice_runner.py`：新增 `--no-hud`（默认显示 HUD）、`_build_hud()`/`_hud_set()`/`_hud_tick()`、会话只读阶段回调 `set_event_sink()`，把 监听→听到了→识别中→思考中→播报中 逐段投射到窗口；提示窗任何故障都被隔离，绝不影响对话。
    - ⚠️ **2026-10-01 更正（UI 批次 2）**：上述"逐段投射"在 `main()` 真实路径上**从未生效**——`main()` 在建窗后又写了一次 `hud: Any | None = None`，把 HUD 覆盖成 `None`，窗口恒为「待机」且退出时不隐藏。该缺陷已修复并加回归测试，详见 `2026-10-01-P9-hud-and-capture-issues-handoff.md` §1.5/§2。
- 真机验证（用户本人参与）：
  - 窗口创建/位置：1920×1080 主屏，`GetWindowRect=(1576,960)-(1896,1056)`（右下角，200×96 减 margin）；`IsWindowVisible=True`。
  - 状态文案实测逐段变化：`正在监听…` → `听到了，正在收音…` → `识别中…` → `思考中…` → `播报中…`，正文含 `第 1 轮`。
    - ⚠️ **2026-10-01 更正**：该观测**不是** `main()` 路径产生的（见上）；`main()` 路径当时恒显示「待机」。文案与轮次行也已按用户要求收敛/去除（可见文案 4 态）。
  - **不抢焦点**：`GetForegroundWindow` 前后一致、等于 HUD 句柄为 False（`stole_focus=False`）。
  - **用户本人确认"看到了，右下角有小窗"**。
  - runner 真机整轮同轮通过：`route=voicestudio_ws_pcm`、`error=None`、`played_segments=2`、`completed_sentences=2`，用户确认听到播报。
- 回归：全量 **1801 tests OK (skipped=3)**、`compileall` exit 0（UI 批次 2 后为 **1808 tests OK (skipped=3)**）。
- 未做/延期：`app/main.py` 未接 HUD（`--hud` 只在试听 runner 上）；**不显示识别/回复正文**（按用户选择）；未叠加系统 toast（可选 `winotify`，未采用）。

## 6. 真机暴露并修复的三个缺陷（关键）

1. **放弃轮次会锁死 ASR 通道（P9 特有、必现）**
   - 现象：第一轮「没听到人说话」被丢弃后，第二轮 `start_rejected: ASR 当前不可用`。
   - 根因：丢弃路径走 `ChatVoiceLoop.cancel_turn` → `driver.cancel()` → `PushToTalkCoordinator.runtime_failed(CANCELLED)`，把 `AsrRecoveryController` 置为 `SESSION_FAILED_RUNTIME`；而 `start()` 只接受 `READY`，仓库里没有任何代码在丢弃后走恢复探测。
   - 修复：`PushToTalkCoordinator.abandon_turn(reason)`（纯增量）只做安全收尾、**不产生 ASR 故障事实**，并通过既有 `begin_probe → record_probe(True)×required_probe_passes → request_voice_switch(safe_idle=True)` 把通道带回 `READY`；任一步被拒即 fail closed（不伪造恢复）。`AsrRecoveryController` 仅新增只读 `config` 属性。`AutoCaptureVoiceDriver` 据此区分「本地无可用输入」（走 abandon）与「真实 ASR/provider 故障」（仍走 `runtime_failed`，fail closed 不削弱）。
   - 回归：`tests/integration/test_p9_turn_abandon.py` 5/5；驱动侧连续三轮（丢弃→成功提交→丢弃）三轮 `start` 全部成功。
2. **自动结束曾被误当成故障丢弃**（实现期内发现并改正）：`silence_timeout` / `max_utterance_duration` 必须走 `coordinator.release()` 正常收尾让 provider 解码，只有 `detector_failure` / `provider_failed` / `no_speech` 才丢弃。
3. **Silero 窗口/帧错位导致起始永不确认**（2026-10-01 真机定位）：Silero 按 512 样本整窗判定、采集帧为 320 样本，窗口每 1.6 帧才凑齐一次；帧级"连续命中"因此交替真假，永远达不到阈值（实测 90 秒内 `vad_hits=957`、窗口级连续 264 次，驱动 `_speech_started` 始终 False）。修复：窗口结论粘滞到未满窗的帧 + 帧级阈值取 1（窗口级阈值由 `speech_frames_required` 承担）+ 未确认起始时 `speech_start_timeout` 为硬上限。修复后主路线真机一次通过。

## 7. 冻结口径的澄清（Lead 裁定，已写入接口冻结文档 §6.1）

- 窗口必须显式 `open()`，每次 `open()` 换新 `window_id`；授权/执行期间 `request_input()` 只读拒绝且不改状态。
- `AutoCaptureSession` 自持连续性台账（以第一帧为基线），**禁止反射修改 `VadSession` 私有台账**；喂给 `VadSession` 的是「重锚定逻辑视图副本」（仅重编号 `event_sequence`/`frame_index`），交付始终是原始帧、pre-roll 必须完整回放。
- `AutoCaptureEndReason` 纯增量新增 `SESSION_CLOSED`/`SESSION_ENDED_INTERNAL`/`SESSION_INVALIDATED`（14→17）。
- `AutoCaptureDecision.passive_decision` 为诊断字段（第 7 字段、可缺省），不进入任何安全/授权/交付判定。
- `cancel()` 幂等且必须在 bus 仍 OPEN 时关闭总线；`finish()` 为正常结束（`terminal_reason=None`，已捕获帧可交付一次）。

## 8. 已知风险与延期项

1. **唤醒词（B3）与声纹（B4）未实现**：路线图 P9 退出条件中「唤醒词只开启新输入会话」「声纹只做误触发过滤」尚无实现；用户已选定技术方向（sherpa-onnx KWS 中文模型 / sherpa-onnx speaker embedding），执行前需再次确认细节。
2. **主程序默认运行时未接自动收音**：`app/main.py --voice` 仍是按键模式；本批次未改 `main.py`（新增入口属单独授权项）。
3. **能量门控对短促/轻声说话敏感**：连续 3 帧命中才判定起始；真机实测「说一句立刻安静」时偶发不触发起始（正确行为，但需用户说话清楚）。可用 `--speech-threshold` / 说话方式调整。
4. **自动静音结束在持续环境声下不触发**：会走 `max_utterance_duration`；已提交的轮次不再误判为无效，但仍建议试听时关闭音乐。
5. **B1 对话协调器未接入** `app/voice_loop`/`app/dialog` 的运行链路：本批次只交付状态机与边界契约，`listen_timeout_seconds` 目前无消费方（B3 接线）。
6. **录音期回放回声**：试听时若扬声器与麦克风距离近，播报声可能被当作输入；属 P10 AEC 范围。
7. 未提交、未 push；工作区仍含 P7-R-29 那批未提交改动（本批次未触碰其文件）。

## 9. 下一批次入口与禁止事项

- 入口：**P9 B3 唤醒词门禁**（sherpa-onnx KWS，唤醒词只开启新的输入会话），或先做 B2 的默认运行时接线（需单独授权）。
- 禁止：不得让 VAD/唤醒词/声纹决定授权；不得进入 P10 的 AEC/全双工；不得改默认语音模式；不得降低 L2/L3、跳过 `PathGuard`/`PolicyEngine`/复检/账本/审计；不得把音频故障写成任务事实。

## 10. 下一步最小读取集合

`P8-handoff.md`、`implementation-roadmap.md` P9 卡、`2026-09-30-P9-batch1-2-interface-freeze.md`、本文件、`app/dialog/**`、`app/audio/vad/auto_capture.py`、`app/voice_loop/auto_driver.py`、`app/asr/push_to_talk.py::abandon_turn`、`tests/integration/test_p9_turn_abandon.py`。
