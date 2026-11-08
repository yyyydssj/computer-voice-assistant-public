# P0–P9 整体测试与验证：阶段验收（阶段 4 / 阶段 5）

> 日期：2026-10-02 08:28（+08:00）
> 范围：把 P0–P9 一起过一遍 —— 全量离线回归 + 编译检查 + 文档一致性 + 交付物完整性；对应 `task_plan.md` 的**阶段 4「测试与验证」**与**阶段 5「交付」**。
> 复核对象：`HEAD = a34af54274da1bb4a869bb538258e12c245dbbee`（分支 `codex/review-playback-lifecycle`，`git status --porcelain` 0 行 = 工作区干净）。
> 前序阶段验收：`P0-handoff.md`…`P8-handoff.md`、`2026-09-21-P5-integration-acceptance.md`、`2026-09-21-P6-integration-acceptance.md`、`2026-09-30-P7-stage-acceptance-final.md`、`2026-09-30-P8-integration-acceptance.md`、`2026-10-01-P9-stage-acceptance.md`。
> 性质：**独立重跑复核**，不是转载上一份验收文档的数字。本轮不联网、不读 `.env` 密钥、不启动麦克风/扬声器，全部为离线注入替身。

---

## 1. 验收范围与口径

- 覆盖 P0（冻结基线/环境）到 P9（VAD 自动收音、连续对话、唤醒词门禁、声纹过滤、UI 提示窗与视觉改版、免按键接入主程序、唤醒词/声纹 CLI、TTS 按需自动拉起）全部阶段的**离线可复核证据**。
- 真机部分（听感、真实设备）沿用用户 2026-10-01 的逐项确认记录，见 `2026-10-01-P9-stage-acceptance.md` §5；本轮**不重复真机**。
- 新增口径（此前各阶段验收未覆盖）：
  1. 文档间数值一致性（跨 `task_plan.md` / `progress.md` / `NEXT-SESSION.md` / roadmap / 各验收文档）；
  2. 交付物路径与 artifact 的存在性清点；
  3. 编码扫描从"只查本轮变更文件"扩展到**全仓 `app`/`tests`/`tools`**，并与基线提交逐 blob 对比；
  4. 静态测试点算与运行时收集数**双向核对**（由此发现 §6.11 的隐性缺陷）。

## 2. 验证命令与实测结果（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
  → Ran 2084 tests in 114.055s, OK (skipped=3)，exit 0
.\.venv\Scripts\python.exe -m unittest discover -s tests/dialog -t . -p "test_*.py" -q       → Ran 56,  OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/audio -t . -p "test_*.py" -q        → Ran 298, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/voice_loop -t . -p "test_*.py" -q   → Ran 113, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/integration -t . -p "test_p9*.py" -q → Ran 173, OK
.\.venv\Scripts\python.exe -m unittest tests.foundation.test_voice_hud tests.foundation.test_hud_render ^
    tests.foundation.test_voice_status tests.foundation.test_main_auto_voice tests.foundation.test_tts_service -q
  → Ran 176, OK
.\.venv\Scripts\python.exe -m compileall -q app tests tools                                 → exit 0
```

结论：**六项测试命令与编译检查全部通过，零 failure、零 error**；与 `2026-10-01-P9-stage-acceptance.md` §3 的记录逐项一致。

### 2.1 静态点算与运行时收集的双向核对

| 项 | 静态 `def test_` 点算 | 运行时实测 | 判定 |
|---|---|---|---|
| `tests/dialog` | 56 | 56 | 一致 |
| `tests/audio` | 298 | 298 | 一致 |
| `tests/voice_loop` | 113 | 113 | 一致 |
| `tests/integration/test_p9*.py`（5 文件） | 173 | 173 | 一致 |
| UI/HUD/状态/自动收音/TTS 服务专项（5 文件） | 176 | 176 | 一致 |
| `tests/**` 全量 | 2087 | **2084** | **差 3 → 见 §6.11（孤儿测试）** |

独立的收集核对（`unittest.TestLoader().discover('tests')`）：`collected = 2084`、`ran = 2084`、`skipped = 3`、`failures = 0`、`errors = 0`。

### 2.2 skipped=3 的具体项（本轮首次查明并登记）

| # | 测试 | skip 原因 |
|---|---|---|
| 1 | `tests.tts.test_edge_sapi.EdgeSapiConfigTests.test_temporary_root_rejects_a_symlink` | `creating a directory symlink is not permitted here`（本机不允许创建目录符号链接） |
| 2 | `tests.tts.test_real_failure_matrix_live.LiveVoiceStudioDeviceEvidenceTests.test_live_voicestudio_returns_bounded_valid_wav` | `set P7_RUN_REAL_TTS_EVIDENCE=1 to run live VoiceStudio/device evidence` |
| 3 | `tests.tts.test_real_failure_matrix_live.LiveVoiceStudioDeviceEvidenceTests.test_live_voicestudio_to_waveout_reports_backend_completion` | 同上 |

三项均为**既定门禁**（1 项受本机权限限制、2 项需真实 TTS 设备并显式开启环境变量），不是失败，也不应被默认打开。

## 3. 编译与编码检查

- `compileall -q app tests tools` → **exit 0**（无语法错误）。
- `app`/`tests`/`tools` 共 **294** 个 `.py`：**strict UTF-8 解码 0 失败、0 个 `U+FFFD`**；**26 个带 UTF-8 BOM**。
- BOM 债务与基线对比（逐 blob 扫描，非工作区猜测）：

| 提交 | BOM 文件数 |
|---|---|
| `fdb0ce1`（2026-09-30「项目首次完整入库」） | 25 |
| `HEAD a34af54` | **26**（新增 `tests/foundation/test_voice_hud.py`） |

新增项定性见 §6.10。Python 3.12 会把 `.py` 按 `utf-8-sig` 解释，BOM **不影响导入或运行**（`compileall` exit 0、全量回归绿灯均可佐证），属编码约定债务，不是功能缺陷。

## 4. 五条退出条件的代码侧独立取证

不依赖文档自述，本轮直接在代码上取证（行号均为 HEAD 版本）：

| # | 退出条件 | 代码证据 | 判定 |
|---|---|---|---|
| 1 | VAD 只决定输入边界，不决定是否授权 | `app/runtime/auto_voice.py` 的单轮事实 `AutoTurnFacts` 只含 `index/boundary/frames/speech_seen/end_reason/accepted/submitted/spoken/reason/seconds/vad_hits/idle_seconds`；`app/dialog`、`app/audio/vad` 安全链引用数为 **0**（`test_p9_batch1_2_audit.py` 实测输出 `safety_refs=0 dangerous_calls=0`）；`app/main.py` 侧异常一律降级为文字 | **通过** |
| 2 | 唤醒词只开启新的输入会话 | `app/runtime/auto_voice.py:38` `KWS_MODES=("off","gate")`、`:99` `kws_mode="off"` 默认关闭；`run_turn` 在采集前先过 gate（`:868-876`），未命中即 `end_reason="wake_word_timeout"`（`:885/889`）、`frames=0`、不提交不播报，唤醒音频不进 ASR；`app/main.py:845-873` CLI 默认 `off` | **通过** |
| 3 | 声纹只做误触发过滤，不替代 L2/L3 | 校验发生在 `self._loop.complete_turn()` **之前**（`auto_voice.py:995-1013`）：拒绝即 `end_reason="voiceprint_rejected"`、`accepted/submitted/spoken` 全 `False`、`_cancel_turn()`，不产生任何授权/执行事实；未注册/报错只计 `_voiceprint_errors`，**不丢弃** | **通过** |
| 4 | 连续对话闲置、取消、任务结束和错误后边界清晰 | `tests/dialog` 56 + `tests/voice_loop` 113（含 `abandon_turn` / 轮次丢弃）+ P9 集成 173 实测全绿；`DIALOG_TRANSITION_REASONS` 27 项、`AutoCaptureEndReason` 17 项（审计输出） | **通过** |
| 5 | 误唤醒不会调用工具或执行动作 | P9 全过程无新增工具入口；`test_p9_batch1_2_audit.py` 对改动面做 AST 审计并实测 `safety_refs=0 / dangerous_calls=0`；`app/runtime/auto_voice.py` 不导入 `app.safety`/`app.executor`/`app.plans` | **通过** |

## 5. 交付物完整性清点

### 5.1 P9 交付物（21 项，全部存在）

| 交付物 | 大小/行数 |
|---|---|
| `app/dialog/`（`coordinator.py` 13684B / `state.py` 11495B / `window.py` 9502B / `__init__.py` 1388B） | 4 文件 36.1KB |
| `app/audio/vad/`（`adaptive.py` 12423B / `auto_capture.py` 29171B / `silero.py` 16368B / `__init__.py` 7894B） | 4 文件 65.9KB |
| `app/audio/kws.py` | 16144B / 315 行 |
| `app/audio/voiceprint.py` | 14282B / 282 行 |
| `app/voice_loop/auto_driver.py` | 42966B / 690 行 |
| `app/runtime/auto_voice.py` | 47901B / 962 行 |
| `app/runtime/tts_service.py` | 12614B / 253 行 |
| `app/ui/hud_render.py` | 29722B / 592 行 |
| `app/ui/voice_hud.py` | 69500B / 1304 行 |
| `app/ui/voice_status.py` | 7109B / 147 行 |
| `app/main.py` | 39964B / 881 行 |
| `tools/p9_auto_voice_runner.py` / `p9_mic_level_calibration.py` / `p9_voiceprint_enroll.py` | 55145B / 6091B / 10302B |
| `implementation_plan/2026-10-01-hud-visual-refresh-design.md` | 13971B / 116 行 |

### 5.2 P0–P8 抽样

`app/safety`（8 个 .py，79.5KB）、`app/executor`（4，68.1KB）、`app/plans`（3，51.7KB）、`app/asr`（4，60.4KB）、`app/tts`（28，449KB）、`app/audio`（12，143KB）均存在。测试侧：`tests/dialog` 4 文件、`tests/audio` 11 文件、`tests/voice_loop` 7 文件、`tests/foundation/test_main_auto_voice.py` 29261B、`tests/integration/test_p9_auto_runner.py` 98090B、`tests/integration/test_p9_batch1_2_audit.py` 70801B，全部存在。

### 5.3 真机证据（artifact）

`implementation_plan/checkpoints/artifacts/`：**22 个子目录 / 128 个文件**，其中 119 个被 git 跟踪、9 个被 ignore。P9 主证据目录 `2026-09-30-P9-auto-voice-loop/` 有 **36 份 JSON**，含两份听感确认件：

- `p9-user-heard-confirmation-20261001T023017Z.json`（960B）
- `p9-user-heard-main-route-confirmation-20261001T030624Z.json`（1126B）

### 5.4 本机私有依赖（不可从仓库复核）

| 项 | 状态 |
|---|---|
| `models/sense-voice`（239.5MB）、`models/kws`（6.17MB）、`models/voiceprint`（28.3MB）、`models/openwakeword`（8.35MB） | 本机齐备 |
| `data/voiceprint-owner.json`（5391B / 204 行） | 本机齐备 |
| `.gitignore` 第 21 行 `/models/`、第 22 行 `/data/`、第 33 行 `*.wav`、第 40 行 HUD mockups | `git ls-files models data` 输出为空 ✅（符合预期） |

### 5.5 Git 侧交付物

`git log --oneline -10` 与 P9 各批次一一对应（`b8ce529` B1/B2 → `3491b5b` UI 提示窗 → `4a5f1e5` 修复批次 3-4 + B3/B4 → `f9b0649` 文档同步 + artifact → `7f2a2c9` UI 视觉改版 → `c53e5f6` `--voice --auto` → `8b5b802` 真机确认 → `a9f8eeb` KWS/声纹 CLI → `dccb377` TTS 按需拉起 → `a34af54` 阶段验收）；工作区干净。

## 6. 与既有文档的差异清单（如实登记，不代填）

| # | 位置 | 现状 | 性质 | 本轮处理 |
|---|---|---|---|---|
| 6.1 | `NEXT-SESSION.md:41` | 基线写 **2060**（标着"当前"） | 过期 | 已改 2084 |
| 6.2 | `NEXT-SESSION.md:24` | "免按键接主程序批次**待提交/待真机试听**" | 过期，与 `:85/:42` 矛盾 | 已更正 |
| 6.3 | `NEXT-SESSION.md:42` | "唤醒词/声纹默认关闭（**CLI 开关未接**）" | **与代码不符**（`app/main.py:845-895` 已接） | 已更正 |
| 6.4 | `task_plan.md:8` | 最新 commit 写 `dccb377` | 过期 | 已改 `a34af54` |
| 6.5 | `task_plan.md:77`、`:65`、`:74` | 阶段 3 "**in_progress**（停在 P5 验收门前）" | 与 `:7`（P9 complete）矛盾 | 已更正为完成 |
| 6.6 | `roadmap:555` | "全量 **2055**"未标批次 | 口径不清（同节 `:550` 已写 2084） | 已标注批次 |
| 6.7 | `2026-10-01-P9-stage-acceptance.md:19` | `end_reason=wakeword_timeout` | **拼写与代码不符**（代码与 B3/B4 交接均为 `wake_word_timeout`） | 已更正 |
| 6.8 | `2026-10-01-P9-stage-acceptance.md:83` | `VOLC_TTS_*` 列为"真实 Provider / TTS 凭据" | 口径不准：代码中**只有 `.env` 白名单**（`app/config/settings.py:26-27`），**无任何读取点**（豆包 TTS 已弃用，见 `progress.md:39`） | 已改口径 |
| 6.9 | `roadmap:612` | 把"设备 epoch 变化"列为 P10 待做 | 与代码不符：`device_epoch` **已实现**（`app/contracts/audio.py:19`、`app/audio/bus.py:25/108`、`app/audio/vad/auto_capture.py:572`） | 已改口径（P10 待做的是"恢复策略"） |
| 6.10 | 全仓 BOM | 26 个文件带 UTF-8 BOM；其中 `tests/foundation/test_voice_hud.py` 为 **P9 期间新增**（首次入库 `3491b5b`；P9 新增 39 个 .py 中唯一带 BOM 者） | 违反 `2026-09-30-P9-batch1-2-interface-freeze.md:425`「新增/修改文件必须是严格 UTF-8（无 BOM）」 | 登记为轻量缺陷（不影响运行），未修改文件 |
| 6.11 | `tests/conversation/test_manager.py:45/78/97` | 三个 `test_*` 方法定义在 **`@dataclass FakeAdapter`（`class FakeAdapter:`，非 `unittest.TestCase`）内部**，缩进层级与 `FakeAdapter.close()` 同级，位于 `class ConversationManagerTests`（`:116`）之前 | **新发现缺陷**：unittest 永不收集这 3 项；该文件静态 22 项 / 实际收集 19 项，正是静态 2087 与 `Ran 2084` 相差 3 的全部原因 | 已登记（§7 处置建议），本轮**未修改代码** |
| 6.12 | `2026-10-01-P9-stage-acceptance.md:100` | "圆角约 2px 像素 alpha 归零" | 自述项，**无像素级证据**（仅 `2026-10-01-P9-hud-visual-refresh-handoff.md:75` 同源自述） | 已标注"未附证据的自述项" |
| 6.13 | `P9-handoff.md` | **不存在**（P0–P6、P8 均有 `PN-handoff.md`；P7 亦缺） | 阶段文档链缺口 | 登记；本轮以本文件 + 5 份批次交接替代 |
| 6.14 | artifact 树 | 全树仅 1 个 `.wav`（`2026-09-28-P7-local-voicestudio/eff172cb.wav`）且被 `*.wav` ignore；HUD mockup 8 张 PNG 被 `.gitignore:40` ignore | 可复核性受限：P7/P8/P9 的"听感"只能靠 `user-heard` JSON 自述 | 登记，不视为伪造（用户已本人确认） |

### 6.11 补充：孤儿测试的内容与影响

| 方法（`tests/conversation/test_manager.py`） | 覆盖意图 |
|---|---|
| `:45 test_routed_text_creates_trace_and_calls_router_before_chat_agent` | 路由先于 chat agent 调用；`RouteHandoff` trace 字段绑定（`request_id/conversation_id/turn_id/source=text_shell/raw_text`） |
| `:78 test_same_turn_id_is_idempotent_and_cannot_be_replaced` | 同一 `turn_id` 幂等且**不可被替换**；换正文复用同 `turn_id` 抛 `ConversationError` |
| `:97 test_asr_final_uses_same_route_contract_and_turn_id_boundary` | ASR 终稿走同一路由契约、`trace.source="asr"`、`turn_id` 边界、router 不可用时不产生 outcome、不调用 agent |

三者对应的正是 **P7-R 路由接缝 + P8 输入接缝** 的 turn/route 身份与幂等契约。它们既不被收集，因此也**从未真正运行过**：按代码看，方法体引用了 `self.adapter` / `self.now` / `self.assertEqual`，而 `FakeAdapter` 只定义了 `calls` / `close_calls`，直接修复缩进（移入 `ConversationManagerTests`）后**预计会真实失败**，需要同时改造成 `self.assertEqual` → 断言可用的形式。**因此这不是"白捡 3 个测试"，而是一处需要按批次修复的覆盖缺口。**

## 7. 阶段 4 / 阶段 5 判定

| 阶段 | 验收项 | 判定 | 证据 |
|---|---|---|---|
| 阶段 4 测试与验证 | 验证所有需求已满足 | **通过（P0–P9）** | 路线图各阶段退出条件均逐条有离线证据；P9 五条退出条件本轮代码侧重新取证（§4） |
| 阶段 4 | 将测试结果记录到 `progress.md` | **完成** | `progress.md` 已追加本次记录 |
| 阶段 4 | 修复发现的问题 | **P0–P9 范围内无阻断缺陷**；发现 1 项轻量编码债务（§6.10）与 1 项测试覆盖缺口（§6.11） | 二者均不阻塞阶段通过，处置建议见下 |
| 阶段 5 交付 | 检查所有输出文件 | **通过** | §5 逐项清点，P9 交付物 21/21 存在 |
| 阶段 5 | 确保交付物完整 | **基本完整，有 3 处受限项** | 受限项：P9-handoff 缺失（§6.13）、原始录音证据不入库（§6.14）、`models/`+`data/` 本机私有不可从仓库重建 |
| 阶段 5 | 交付给用户 | **通过** | 本文件 + 同步后的 4 份状态文档 |

**结论：P0–P9 整体阶段验收通过（阶段 4 与阶段 5 判定为完成）。** 3 处受限项已在 §6 如实登记，属于"记录在案的可复核性限制"，不是未验证的功能。

### 处置建议（需另行授权，本轮未动手）

| 优先级 | 事项 | 说明 |
|---|---|---|
| 中 | `tests/conversation/test_manager.py` 3 个孤儿测试 | 修复后全量基线将从 2084 变为 2087（且需按 `TestCase` 语义改写断言）；属代码改动，请单独授权 |
| 低 | `tests/foundation/test_voice_hud.py` 去 BOM | 去掉 3 字节 BOM 后，全仓 BOM 债务回到基线 25 个，符合 P9 接口冻结约定 |
| 低 | 补 `P9-handoff.md` | 让 P9 与 P0–P6/P8 的阶段文档链对齐（内容可直接索引现有 5 份批次交接 + 本文件） |
| 低 | `models/`+`data/` 可重建性 | 换机复核需按各交接文档记录的来源 URL 与 `sha256` 重下 |

## 8. 本轮未做 / 不在范围内

- 不重复真机（麦克风/扬声器/真实 Provider）；沿用用户 2026-10-01 确认记录。
- 不做 AEC/全双工/真实 barge-in/设备热插拔与长期运行（P10 正题）；多轮上下文、SSE 流式、费用限流、persona 接入运行时（需另行授权）。
- 不修改 `app/**`、`tests/**`、`tools/**` 任何代码；不提交、不 push。
- `threshold_mode=auto` 仍未接主程序 CLI（仅 runner 有 `--threshold-mode`，`app/main.py` 无该参数）—— 属已知未做项，非缺陷。

## 9. 回滚

- 本轮仅新增/修改文档：`git checkout -- task_plan.md progress.md NEXT-SESSION.md implementation_plan/implementation-roadmap.md` 可还原既有文档；新建的本文件直接删除即可。
- 未触碰代码、未提交、未 push，无需代码回滚。

## 10. 收尾批次修订（2026-10-02 同日，用户确认后执行 A→B→C）

本文件 §7 登记的两项非阻断缺陷与一项文档缺口已由**收尾批次**修复，本文档 §2/§3/§6.10/§6.11 的"修复前状态"保留原样作为复核留痕：

| 项 | 修复内容 | 结果 |
|---|---|---|
| A | `tests/conversation/test_manager.py` 3 个孤儿测试移入 `ConversationManagerTests`（断言逐字未改写、**生产代码零改动**；修复前已核实 `submit_text`/`submit_asr_final`/`router=`/`ConversationRouteResult`/幂等分支均在位） | 专项 **19 → 22 项全绿**；其中"同 `turn_id` 幂等且不可替换"此前全仓无覆盖 |
| B | `tests/foundation/test_voice_hud.py` 删除 UTF-8 BOM（3 字节，47,680 → 47,677） | 全仓 BOM **26 → 25**，与基线 `fdb0ce1` 集合逐项一致 |
| C | 新建 `implementation_plan/checkpoints/P9-handoff.md`（阶段级交接） | 补上 P9 缺失的阶段文档（P7 仍缺，替代入口为 `2026-09-30-P7-stage-acceptance-final.md`） |

**收尾后复测**（2026-10-02）：

```text
.\.venv\Scripts\python.exe -m unittest tests.conversation.test_manager -q        → Ran 22,  OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q  → Ran 2087 tests in 113.078s, OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools                      → exit 0
编码扫描（294 个 .py）                                                            → 0 解码失败、0 U+FFFD、BOM 25
```

**结论不变**：阶段 4 / 阶段 5 判定维持**完成**；受损的仅是"修复前基线"。**当前全量基线口径统一为 `2087 tests OK (skipped=3)`**（本文件 §2 记录的 2084 是收尾前实测值）。`skipped=3` 仍为 §2.2 那三项门禁测试。剩余未修项：`models/`+`data/` 不可从仓库重建（本机私有）、提示窗圆角 2px 自述项、`threshold_mode=auto` 未接 CLI、任务链未消费真实路由。本轮收尾改动**未提交、未 push**。

