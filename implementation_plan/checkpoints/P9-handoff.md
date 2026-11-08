# P9 阶段交接：VAD 自动收音、连续对话、唤醒词门禁、声纹过滤与 UI 提示窗

> 日期：2026-10-02（覆盖 2026-09-30 ~ 2026-10-02 的 P9 全部批次）
> 阶段状态：**complete**（2026-10-01 阶段级集成验收通过；2026-10-02 完成 P0–P9 整体测试与验证，阶段 4/阶段 5 判定完成）
> 本文件是 P9 的**阶段级交接**（对齐 `P0-handoff.md`…`P8-handoff.md` 的格式）。P7 / P9 此前缺此文件，P7 的替代入口为 `2026-09-30-P7-stage-acceptance-final.md`。

---

## 1. 阶段目标与不做清单

**唯一目标**：在已经稳定的半双工闭环上增加自动化输入，但任何误唤醒、声纹误判或会话恢复都不能绕过计划、授权和停止规则。

**本阶段不做**（路线图 P9 节）：
- 不做 AEC 的最终方案；不把声纹当授权或身份认证；不允许恢复组件自动恢复旧 utterance/request；不允许后台自动切入用户正在输入、查看方案或授权的时刻。

实际执行中额外守住：唤醒词/声纹/UI 均只作用于**输入边界或显示**，`app/safety`、`app/executor`、`app/plans` 语义零改动；未新增工具、命令面或任意脚本入口；未降低授权等级。

## 2. 批次与 commit 对照（分支 `codex/review-playback-lifecycle`）

| 批次 | 内容 | commit |
|---|---|---|
| P9 B1/B2 | 自动收音与连续对话边界：Silero 段级端点、VAD 自适应阈值、turn claim 与免按键驱动 | `b8ce529` |
| P9 UI 批次 1 | 右下角非模态 4 态状态窗（零依赖 ctypes），`--voice` 接入主程序 | `3491b5b` |
| P9 修复批次 3-4 + B3/B4 | 唤醒词门禁、声纹前置过滤、主程序提示窗接线修复 | `4a5f1e5` |
| 文档与 artifact | 批次交接同步 + 真机 artifact 入库 | `f9b0649` |
| P9 UI 视觉改版（方案 4） | 浅色卡片胶囊外观 + 逐像素分层窗口，修掉 6 个真机缺陷 | `7f2a2c9` |
| 免按键接入主程序 | 抽取共享会话层 `app/runtime/auto_voice.py`，新增 `--voice --auto` | `c53e5f6` |
| 真机验收记录 | `--voice --auto` 免按键一轮闭环用户确认 | `8b5b802` |
| 唤醒词/声纹 CLI | `--kws-mode gate` / `--voiceprint-mode filter` 接入主程序，默认全关 | `a9f8eeb` |
| 本机 TTS 按需拉起 | 播报通道不可达时自动拉起 VoiceStudio（只关自己拉起的进程） | `dccb377` |
| P9 阶段级集成验收 | 五条退出条件逐条判定 + 标记 complete | `a34af54`（P9 末次） |

## 3. 退出条件判定（路线图 P9 §退出条件）

| # | 退出条件 | 判定 | 关键证据 |
|---|---|---|---|
| 1 | VAD 只决定输入边界，不决定是否授权 | 通过 | `AutoTurnFacts` 只含声学边界与轮次事实；`app/dialog`、`app/audio/vad` 安全链引用 0；`tests/dialog` 56 + `tests/audio` 298 |
| 2 | 唤醒词只开启新的输入会话 | 通过 | `KWS_MODES=("off","gate")`、默认 `off`；未命中 `end_reason=wake_word_timeout`、`frames=0`、不提交不播报，唤醒音频不进 ASR |
| 3 | 声纹只做误触发过滤，不替代 L2/L3 | 通过 | 校验前置于 ASR 且在 `complete_turn()` **之前**；拒绝即 `voiceprint_rejected`、`accepted/submitted/spoken=False`；未注册/报错只计数不丢弃 |
| 4 | 连续对话闲置、取消、任务结束和错误后边界清晰 | 通过 | `tests/dialog` 56 + `tests/voice_loop` 113（含 `abandon_turn`）+ P9 集成 173 |
| 5 | 误唤醒不会调用工具或执行动作 | 通过 | 无新增工具入口；`test_p9_batch1_2_audit.py` AST 审计实测 `safety_refs=0 / dangerous_calls=0` |

详细判定表见 `2026-10-01-P9-stage-acceptance.md` §2；代码侧独立取证见 `2026-10-02-P0-P9-stage-acceptance.md` §4。

## 4. 交付物

| 模块 | 内容 |
|---|---|
| `app/dialog/**` | 连续对话状态机（`state.py`）、窗口（`window.py`，闲置/轮数/时长）、协调器（`coordinator.py`） |
| `app/audio/vad/**` | 自适应阈值（`adaptive.py`）、自动收音与段级端点（`auto_capture.py`）、Silero VAD（`silero.py`） |
| `app/audio/kws.py` · `app/audio/voiceprint.py` | 唤醒词门禁、声纹误触发过滤（前置于 ASR） |
| `app/voice_loop/auto_driver.py` | 免按键驱动：自动起始/段级结束/尾段定稿/停滞看门狗/每轮重置 VAD |
| `app/runtime/auto_voice.py` | 免按键会话层（主程序与 runner 共用）：装配 + `run_turn` 轮次循环 + fail-closed 错误族 |
| `app/runtime/tts_service.py` | 本机 TTS 可达性探测与按需自动拉起 |
| `app/ui/hud_render.py` · `voice_hud.py` · `voice_status.py` | 提示窗：纯像素渲染核心 + 逐像素分层窗口 + 一次性降级 + 状态桥（`VOICE_HUD_SCHEMA_VERSION=3`） |
| `app/main.py` | `--voice --auto` 入口、唤醒词/声纹/TTS 自动启动 CLI，异常一律降级为文字 |
| `tools/p9_auto_voice_runner.py` · `p9_mic_level_calibration.py` · `p9_voiceprint_enroll.py` | 真机试听 / 电平标定 / 声纹注册工具 |
| 模型 | `models/kws/`（wenetspeech KWS）、`models/voiceprint/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`、`models/openwakeword/silero_vad.onnx`、`models/sense-voice/`（均 `.gitignore`，来源与哈希见 `2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`） |
| 本机档案 | `data/voiceprint-owner.json`（192 维嵌入，无音频，不入库） |

## 5. 验证证据

- **阶段级验收**：`2026-10-01-P9-stage-acceptance.md`（五条退出条件逐条 + 全量 **2084 tests OK (skipped=3)** + 真机逐项确认）。
- **整体验收（阶段 4/5）**：`2026-10-02-P0-P9-stage-acceptance.md`（独立重跑复核 2084 → 收尾后 **2087**；`compileall` exit 0；交付物 21/21；文档差异与缺口清单）。
- **批次交接**：`2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`、`2026-10-01-P9-hud-and-capture-issues-handoff.md`、`2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`、`2026-10-01-P9-hud-visual-refresh-handoff.md`、`2026-10-01-P9-auto-capture-main-integration-handoff.md`。
- **设计规格**：`implementation_plan/2026-10-01-hud-visual-refresh-design.md`；**接口冻结**：`2026-09-30-P9-batch1-2-interface-freeze.md`。
- **真机 artifact**：`checkpoints/artifacts/2026-09-30-P9-auto-voice-loop/`（36 份 JSON，含 2 份 `p9-user-heard-*` 听感确认件）。

## 6. 真机验收如实口径（不代填）

用户 2026-10-01 逐项确认过：① 免按键试听（听到离线声线播报、识别正确，`user_heard=true`）；② B3 唤醒词门禁 / B4 声纹过滤（正例+负例均通过）；③ UI 视觉改版（"没问题"）；④ 主程序 `--voice --auto`（"跑了，可以"）；⑤ 唤醒词/声纹 CLI + TTS 自动拉起（"测完了"）。

必须说明的边界：
- 未采集识别正文、延迟毫秒数、声音清晰度评分；
- 第 ⑤ 项用户只给"测完了"确认、**未提供控制台输出**，故本机实际命中阈值与 TTS 拉起实测耗时无本机数值（TTS 耗时证据来自开发侧实测：`pid=468`、用时 53.3s、`/profiles` 45s 后 200）；
- P7/P8/P9 **无原始录音证据入库**（`.gitignore` 的 `*.wav` 排除），听感仅能靠 `p9-user-heard-*.json` 自述复核。

## 7. 本机私有配置（不入库）

| 名称 | 用途 | 状态 |
|---|---|---|
| `ASSISTANT_TTS_PYTHON` / `ASSISTANT_TTS_BACKEND` / `ASSISTANT_TTS_CWD` | 语音模式按需拉起 VoiceStudio 的启动命令 | 已写入**用户级**环境变量并校验路径存在（读取点 `app/runtime/tts_service.py:52-54/149-154`） |
| `ASSISTANT_DATA_ROOT` | 生产数据根 | 既有 `.env` 配置（读取点 `app/config/settings.py:195`） |
| `DEEPSEEK_API_KEY` | 真实 Provider 凭据 | 既有（`.env` + 用户级环境变量，均未入库） |
| `VOLC_TTS_*` | **已弃用**的豆包 TTS（仅存 `.env` 白名单，代码无读取点） | 历史遗留 |
| `models/**`、`data/**` | 模型与声纹档案 | `.gitignore:21/22` 排除 |

## 8. 回滚

- **免按键入口**：不带 `--auto` 即回到按键模式（逐字节不变）；`--tts-autostart off` 关闭自动拉起。
- **唤醒词/声纹**：不传 `--kws-mode` / `--voiceprint-mode` 即关闭（默认 `off`）。
- **提示窗**：`--no-hud` 关闭；逐像素渲染不支持时自动降级为旧纯色卡片（`render_mode=legacy` 可观测）。
- **整体**：`git revert` 对应批次 commit；UI 与免按键互不影响，语音链路不依赖提示窗。
- 本机私有配置可随时 `[Environment]::SetEnvironmentVariable(name, $null, 'User')` 清除。

## 9. 已知问题与遗留（如实登记）

| # | 事项 | 状态 |
|---|---|---|
| 1 | `tests/foundation/test_voice_hud.py` 曾带 UTF-8 BOM（P9 期间新增，违反接口冻结"无 BOM"要求） | **已修**（2026-10-02 去 BOM，全仓 BOM 26 → 25，回到基线） |
| 2 | `tests/conversation/test_manager.py` 3 个 `test_*` 落在非 `TestCase` 的 `FakeAdapter` 内、永不被收集（静态 2087 vs `Ran 2084` 的全因） | **已修**（2026-10-02 移入 `ConversationManagerTests`，19 → 22 项全绿，全量基线 2084 → **2087**） |
| 3 | 提示窗 GDI 文字阶段会让胶囊圆角约 2px 像素 alpha 归零 | 未修（**无像素级证据的自述项**，肉眼不可见；彻底消除需离屏预排版文字） |
| 4 | `threshold_mode=auto` 未接主程序 CLI（runner 已有 `--threshold-mode`，共享层已支持） | 未做（非缺陷） |
| 5 | 任务链路尚未消费真实路由（`app/runtime/orchestration/` 已就绪） | 未做（需真实 Agent 计划提议） |

## 10. 下一步

- **P10（另行授权）**：打断/AEC/设备恢复与长期运行。注：`device_epoch` 身份与变化检测已在 P2/P6 落地，P10 待做的是"安全空闲点的恢复策略"。
- 多轮上下文 / SSE 流式 / 费用限流 / persona 接入运行时（persona 加载器已交付但未接入）。
- 未完成/待办的完整清单见 `NEXT-SESSION.md` §4。
