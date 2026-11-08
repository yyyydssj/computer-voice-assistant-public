# P9 阶段级集成验收：自动收音、连续对话、唤醒词/声纹与提示窗

> 日期：2026-10-01
> 验收范围：P9 全部批次（B1/B2 自动收音与连续对话边界、B3 唤醒词门禁、B4 声纹过滤、UI 批次 1-2 与视觉改版、免按键自动收音接入主程序、唤醒词/声纹 CLI、本机 TTS 按需自动拉起）及其前置阶段 P5/P6/P7/P8 回归。
> 关联文档：`../implementation-roadmap.md` P9 节、`2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`、`2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`、`2026-10-01-P9-hud-visual-refresh-handoff.md`、`2026-10-01-P9-auto-capture-main-integration-handoff.md`、`2026-10-01-hud-visual-refresh-design.md`。

## 1. 授权与范围

- 用户自 2026-10-01 起逐项授权：P9 B1/B2 → B3/B4 → UI 批次 1-2 → UI 视觉改版（方案 4）→ 免按键自动收音接入主程序 → 唤醒词/声纹 CLI → 本机 TTS 按需自动拉起。
- 离线验收部分（本文档主体）：不联网、不读 `.env` 密钥、不启动真实麦克风/扬声器，全部用注入替身。
- 真机部分由用户本人执行并确认（§5 逐条列出**用户确认过什么、没确认什么**）。
- 未改动 `app/safety`、`app/executor`、`app/plans` 语义；未新增工具、命令面或任意脚本入口；未降低授权等级。

## 2. 退出条件逐条判定（路线图 P9 §退出条件）

| # | 退出条件 | 判定 | 证据 |
|---|---|---|---|
| 1 | VAD 只决定输入边界，不决定是否授权 | **通过** | `AutoVoiceSession`/`PushToTalkCoordinator` 只产出声学边界与轮次事实，不触碰授权/计划/执行：`tests/dialog`（56）覆盖状态机与窗口边界；`tests/audio`（298）覆盖 VAD/端点/唤醒词/声纹的判定语义；主程序侧 `tests/foundation/test_main_auto_voice.py` 断言「自动收音不改变授权/计划/执行语义」、失败只降级为文字 |
| 2 | 唤醒词只开启新的输入会话 | **通过** | `KWS modes` 默认 `off`；`gate` 时未命中即 `end_reason=wake_word_timeout`、`frames=0`、不提交不播报，唤醒音频不进 ASR：runner 集成用例（`tests/integration/test_p9_auto_runner.py::WakeWordGateTests`）+ 主程序 CLI 折叠/失败报错用例 |
| 3 | 声纹只做误触发过滤，不替代 L2/L3 | **通过** | 声纹判定**前置于 ASR**：被拒轮次连解码都不跑（runner 用例 `VoiceprintFilterTests`：正例放行、负例 `voiceprint_rejected`、`not_enrolled` 只算不可用不误杀）；未注册/报错一律**不丢弃**，绝不因故障放行任何计划/执行事实 |
| 4 | 连续对话闲置、取消、任务结束和错误后边界清晰 | **通过** | `tests/dialog`（状态机/窗口/协调器）+ `tests/voice_loop`（113，含轮次丢弃与 `abandon_turn`）+ `tests/integration` 的 P9 集成（173）覆盖：闲置超时、轮数上限、取消、`no_speech`、输入停滞、起点被拒 |
| 5 | 误唤醒不会调用工具或执行动作 | **通过** | 唤醒词/声纹/自动收音都只作用于**输入边界**；P9 全过程没有新增工具入口，且 `tests/integration/test_p9_batch1_2_audit.py` 对改动面做 AST 审计（不导入安全链、不出现执行面调用） |

结论：**五条退出条件全部有可复核的离线证据 + 用户真机确认 → P9 阶段级验收通过。**

## 3. 验证命令与结果（2026-10-01 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
  → Ran 2084 tests, OK (skipped=3)
.\.venv\Scripts\python.exe -m unittest discover -s tests/dialog -t . -p "test_*.py" -q      → Ran 56,  OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/audio -t . -p "test_*.py" -q       → Ran 298, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/voice_loop -t . -p "test_*.py" -q  → Ran 113, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/integration -t . -p "test_p9*.py" -q → Ran 173, OK
.\.venv\Scripts\python.exe -m unittest tests.foundation.test_voice_hud tests.foundation.test_hud_render \
    tests.foundation.test_voice_status tests.foundation.test_main_auto_voice tests.foundation.test_tts_service -q
  → Ran 176, OK
.\.venv\Scripts\python.exe -m compileall -q app tests tools                                   → exit 0
```

P9 期间基线变化：1620（B1/B2）→ 1808/1978（UI 与修复批次）→ 2034（UI 视觉改版）→ 2055（免按键接主程序）→ 2060（唤醒词/声纹 CLI）→ **2084（TTS 自动拉起）**。

## 4. 交付物清单

| 模块 | 内容 |
|---|---|
| `app/dialog/**` | 连续对话状态机、窗口（闲置/轮数/时长）、协调器 |
| `app/audio/vad/**` | 能量阈值与自适应阈值、Silero VAD（含段级端点 `segment_pending`/`flush_segment`） |
| `app/audio/kws.py` · `app/audio/voiceprint.py` | 唤醒词门禁、声纹误触发过滤（前置于 ASR） |
| `app/voice_loop/auto_driver.py` | 免按键驱动：自动起始/段级结束/尾段定稿/停滞看门狗/每轮重置 VAD |
| `app/runtime/auto_voice.py` | 免按键会话层（主程序与 runner 共用）：装配 + `run_turn` 轮次循环 + fail-closed 错误族 |
| `app/runtime/tts_service.py` | 本机 TTS 可达性探测与按需自动拉起（只关自己拉起的进程） |
| `app/ui/hud_render.py` · `app/ui/voice_hud.py` · `app/ui/voice_status.py` | 提示窗：纯像素渲染核心 + 逐像素分层窗口 + 一次性降级 + 状态桥 |
| `app/main.py` | `--voice --auto` 入口、唤醒词/声纹/TTS 自动启动 CLI、异常一律降级为文字 |
| `tools/p9_auto_voice_runner.py` · `p9_mic_level_calibration.py` · `p9_voiceprint_enroll.py` | 真机试听/标定/声纹注册工具 |

## 5. 真机验收记录（如实口径）

**用户明确确认过的：**

| # | 项 | 用户确认 | 日期 |
|---|---|---|---|
| 1 | P9 B1/B2 免按键试听（runner） | 听到离线声线播报、识别正确（`user_heard=true`） | 2026-10-01 |
| 2 | B3 唤醒词门禁 / B4 声纹过滤（runner） | 正例+负例均通过（不喊不收音；他人说话被丢弃） | 2026-10-01 |
| 3 | UI 视觉改版（方案 4） | 「没问题」（白底圆角卡片 + 状态色条 + 状态色正文） | 2026-10-01 |
| 4 | 主程序 `--voice --auto` | 「跑了，可以」（免按键一轮闭环可用） | 2026-10-01 |
| 5 | 唤醒词/声纹 CLI + TTS 自动拉起 | 「测完了」（本阶段收口前的最后一次真机确认） | 2026-10-01 |

**必须说明的边界（不代填）：**

- 上表只记录**用户确认的事实**；未采集识别正文、延迟毫秒数、声音清晰度评分；
- 第 5 项用户只给了"测完了"的确认，**未提供控制台输出**，因此《唤醒词/声纹开关在本机的实际命中阈值》
  与《TTS 自动拉起在本机的实测耗时》两处没有本机数值；TTS 自动拉起的耗时证据来自
  **开发侧实测**（`pid=468，用时 53.3s`、`/profiles` 45s 后 200），已在对应交接文档注明；
- 若日后需要精确数值，重跑对应命令并保存控制台输出即可补登。

## 6. 本机私有配置（不入库）

| 名称 | 用途 | 状态 |
|---|---|---|
| `ASSISTANT_TTS_PYTHON` / `ASSISTANT_TTS_BACKEND` / `ASSISTANT_TTS_CWD` | 语音模式按需拉起 VoiceStudio 的启动命令 | 已写入**用户级**环境变量并校验路径存在 |
| `ASSISTANT_DATA_ROOT` | 生产数据根 | 既有 `.env` 配置 |
| `DEEPSEEK_API_KEY` | 真实 Provider 凭据 | 既有（`.env` 与用户级环境变量，均未入库） |
| `VOLC_TTS_*` | **已弃用**的豆包 TTS 凭据（仅存 `.env` 白名单 `app/config/settings.py:26-27`，代码中无任何读取点） | 历史遗留 |
| `models/**`、`data/**` | 模型与声纹档案 | `.gitignore` 排除 |

## 7. 回滚

- **免按键入口**：不带 `--auto` 即回到按键模式（逐字节不变）；`--tts-autostart off` 关闭自动拉起。
- **唤醒词/声纹**：不传 `--kws-mode` / `--voiceprint-mode` 即关闭（默认 `off`）。
- **提示窗**：`--no-hud` 关闭；若逐像素渲染在本机不被支持会自动降级为旧纯色卡片。
- **整体**：`git revert` 对应批次的 commit；UI 与免按键互不影响，语音链路本身不依赖提示窗。
- 本机私有配置（环境变量）可随时 `[Environment]::SetEnvironmentVariable(name, $null, 'User')` 清除。

## 8. 未做 / 留给 P10 或后续

- AEC/全双工、打断（barge-in）、设备热插拔与长期运行（P10 正题）；
- 多轮上下文 / SSE 流式 / 费用限流 / persona（需另行授权）；
- 自适应阈值校准（`threshold_mode=auto`）接主程序 CLI（共享层已支持，runner 已有）；
- 任务链路消费真实路由（`app/runtime/orchestration/` 已就绪，尚未接真实 Agent 计划提议）；
- 提示窗圆角处约 2px 像素 alpha 归零（**未附像素级证据的自述项**，同源自述见 `2026-10-01-P9-hud-visual-refresh-handoff.md:75`；肉眼不可见；彻底消除需离屏预排版文字）。

## 9. 事后修订记录（2026-10-02，来自 P0–P9 整体验收）

本文件在 P0–P9 整体验证（`2026-10-02-P0-P9-stage-acceptance.md`）中被逐项复核，实测数字（2084 / 56 / 298 / 113 / 173 / 176、`compileall` exit 0）与 §3 完全一致。以下 4 处按复核结果修订，其余内容未改动：

| # | 位置 | 修订 |
|---|---|---|
| 1 | §2 第 2 条 | `end_reason=wakeword_timeout` → **`wake_word_timeout`**（代码 `app/runtime/auto_voice.py:885/889` 与 B3/B4 交接均为带下划线写法） |
| 2 | §6 环境变量表 | `VOLC_TTS_*` 口径更正为"已弃用的豆包 TTS 凭据，代码中无任何读取点"，不再与 `DEEPSEEK_API_KEY` 并列为有效凭据 |
| 3 | §8 第 5 条 | 圆角 2px 项标注为"未附像素级证据的自述项" |
| 4 | §3 | 补充：`skipped=3` 的具体 3 项已查明（1 项本机不允许建目录符号链接 + 2 项真实 TTS 设备证据需 `P7_RUN_REAL_TTS_EVIDENCE=1`），见整体验收文档 §2.2 |

另在整体验收中登记、**本文件未覆盖**的两项（不改变本文件结论）：`tests/foundation/test_voice_hud.py` 带 UTF-8 BOM（P9 期间新增，违反接口冻结第 425 行）；`tests/conversation/test_manager.py:45/78/97` 三个 `test_*` 落在非 TestCase 的 `FakeAdapter` 内、永不被收集（静态 2087 与 `Ran 2084` 相差 3 的原因）。

**该两项已于 2026-10-02 收尾批次修复**：孤儿测试移入 `ConversationManagerTests`（19 → 22 项全绿，断言未改写、生产代码未改）；`test_voice_hud.py` 去掉 BOM（全仓 BOM 26 → 25）。**当前全量基线 = 2087 tests OK (skipped=3)**，本文件 §3 的 2084 为收尾前实测值，保留原样作为留痕。详见 `2026-10-02-P0-P9-stage-acceptance.md` §10。

