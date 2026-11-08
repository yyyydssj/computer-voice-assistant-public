# P10 · T1：任务执行接线（首批＝打开白名单应用）——竞品调研 + 设计

- 建立时间：2026-10-13（本地）
- 目标：让「打开计算器」这类请求，在**文本**与**语音**两条入口上都能走完
  「本地路由 → 计划预览 → 说/输入「确认」 → 真实启动 → 摘要」，全程复用 P5 安全链。
- 用户已确认的三项决策：
  1. **首批范围**：只做「打开白名单应用」；`file_operation`（创建/写入/改名/复制/移动）与
     `dangerous`（删除/覆盖）本批**保持 fail closed**（只回一句"该操作需要 L3，暂未开放"）；
  2. **确认交互**：文本与语音都只认结果词 **「确认」**；其它输入、"取消/算了"、**超时**一律拒绝；
  3. **路由与提议**：**本地规则优先**（离线、零计费），未命中时若已 opt-in 再用真实 DeepSeek 路由兜底；
     计划提议用**本地确定性映射**（`application_name` → 白名单 `application_id`），不信 LLM 直接给动作参数。
- 硬纪律：不新增工具、不放宽授权语义；**授权前不执行任何东西**；不进新命令面。

---

## 1. 现状缺口（只读读码确证）

| 层 | 现状 | 证据 |
|---|---|---|
| 编排 | ✅ 就绪：预览 → L2/L3 → 预检 → 执行 → 验证 → 摘要 | `app/runtime/orchestration/task_flow.py:1-19`、`summary.py`、`stop.py` |
| 契约 | ✅ `task`＝`open_application`/`file_operation`/`planning_request`；`task`/`dangerous` 必须 `required_confirmation=defer_to_safety` 且置信度 ≥0.80 | `app/contracts/routing.py:17-34`、`:200-207` |
| ① 默认路由 | ❌ `LocalChatRouter` **恒返回 `chat`** ⇒ 不 opt-in 永远进不了 task 分支 | `app/runtime/assistant_runtime.py:21-41` |
| ② 计划提议 | ❌ `TaskActionProposal` 需要调用方给候选动作，**现在没有任何调用方** | `task_flow.py:116-138` |
| ③ 装配 | ❌ `app/main.py` 里没有 `ToolRegistry`／`ApplicationSpec` 白名单／`LocalExecutionBackend`／L2·L3／preflight | `app/main.py:176-360`（`run()` 只装配会话与 TTS） |
| ④ 确认交互 | ❌ 文本与语音都没有"预览 → 确认 → 执行 → 摘要"回路 | 同上 |

两条读码确证的硬约束：`start_whitelisted_application` 需要 `application_id`，
且执行后端会校验可执行文件**绝对路径 + SHA-256**；验证器还会探测进程真实映像路径：

- `app/executor/local_backend.py:50-63`（`ApplicationSpec.executable_hash` 必须是 `sha256:` 前缀 71 字符）、
  `:285-306`（白名单缺失 → `application_not_whitelisted`；哈希变化 → `application_identity_changed`）；
- `app/verification/result_verifier.py:117-123`（PID 的 observed executable 与白名单不一致 → `state_uncertain`）；
- `app/safety/preflight.py:264`（执行前复检 `start_whitelisted_application`）。

⇒ 白名单必须**真实登记**应用（路径 + 哈希），否则整条链 fail closed。

---

## 2. 竞品调研（2026-10-13 实测）

**抓取方式与失败项（如实）**：`api.github.com` 今天被本机解析到**非公网地址**（`web_fetch` 直接拒绝），
改用 **PowerShell 直连 GitHub API** 取元数据、`ghfast.top` 镜像取源码/官方文档正文；
`OHF-Voice/rhasspy` 的 API 返回 **404**（仓库已迁移/改名）、`ovos-yes-no-plugin` 的 README 返回 **404**
⇒ 这两项**未取到，不引用其数据**。

| 项目 | Stars | License | 最近推送 | 关键事实（实读） | 采不采纳 |
|---|---:|---|---|---|---|
| [openinterpreter/openinterpreter](https://github.com/openinterpreter/openinterpreter) | 68,566 | Apache-2.0 | 2026-10-11 | Approvals 三档：**Ask（默认，推荐）**/Auto-approve/Never；触发面＝"**改变世界的动作**才确认，只读检查不打扰"；批准提示必须给出 **工具名 + 确切参数 + diff/预览**，并提供"本次会话内批准"；官方"first-run rule"＝新流程先在**单个用例**上开最严档 | ✅ 采纳：默认 on + 预览显示**确切参数**；❌ 不采纳 Auto-approve（我们**永不自动执行**） |
| [microsoft/UFO](https://github.com/microsoft/UFO) | 10,005 | MIT | 2026-10-07 | UFO²＝Windows **UIA/Win32/WinCOM GUI 自动化**（LLM 驱动点击、混合 GUI+API），UFO³＝跨设备 DAG 编排 | ❌ 不采纳：走 GUI 点击太重、且绕开"白名单启动"；反证我们"白名单 + SHA-256 + PID 校验"更轻、可审计 |
| [home-assistant/intents](https://github.com/home-assistant/intents) | 629 | CC-BY-4.0 | 2026-10-12 | 模板句式：可选 `[the]`、交替 `(a|b)`、槽列表 `{slot}`、通配、扩展规则；**中文/日文要 `ignore_whitespace: true`**；有 `skip_words`（如 "please"）；响应模板由槽值生成 | ✅ 采纳：匹配前**去空白/标点**、跳过礼貌前缀；❌ 不引 DSL（我们只有 ~10 条固定句式，引 DSL 只会扩大面） |
| [OHF-Voice/hassil](https://github.com/OHF-Voice/hassil) | 115 | Apache-2.0 | 2026-10-13 | HA 的模板匹配器；两条硬约束：**槽引用不得放进可选/交替**（模板必须总是填同样的槽）、**规则不要嵌套/不要含槽列表**（可读性 + 组合爆炸） | ✅ 采纳为收敛原则：句式里槽"要么必有、要么整段可选"，不嵌套 |
| [OpenVoiceOS/ovos-core](https://github.com/OpenVoiceOS/ovos-core) | 296 | Apache-2.0 | 2026-10-08 | 离线语音助手核心（意图 + 对话管理）；其 yes/no 插件的 README **未取到（404）** | ⚠️ 仅作背景，不引用具体做法 |
| [MycroftAI/mycroft-core](https://github.com/MycroftAI/mycroft-core) | 未取到 API 元数据 | — | — | 实读 `mycroft/res/text/en-us/yes.voc`：`yes / yeah / yep / sure / please / confirm / confirmed / affirmative` | ⚠️ 说明主流是**同义词集合**；我们**只认「确认」更严**（将来要放宽再加同义词，需另批） |

**我们比竞品更严的三点**：① 永不自动执行（无 auto-approve）；② 确认词闭集且**超时即拒绝**；
③ 白名单绑 **SHA-256**、危险路由本批直接 fail closed。

---

## 3. 设计

### 3.1 数据流

```text
文本 / 语音(ASR accepted final)
  → ConversationManager._route()（既有，不改）
  → 组合路由器：LocalTaskRouter（本地句式，命中即用）
                 └─ 未命中 → 已 opt-in 的真实 Provider 路由 / 否则 LocalChatRouter
  → RouteHandoff(task / open_application / defer_to_safety / tool_scope=(start_whitelisted_application,))
  → AppLaunchPlanner：slots.application_name → 白名单 application_id → TaskActionProposal
  → TaskFlowOrchestrator
       TaskPlanPreviewer（冻结不可执行 ActionPlan + 摘要）
       → L2 确认（「确认」= allow；其它/超时 = deny）
       → ExecutionPreflightGate（原子消费授权）
       → ControlledExecutionRunner（LocalExecutionBackend + LocalResultVerifier + ExecutionLedger + 审计）
       → ResultDocument
  → summarize_result_document → 文本打印 / 语音播报
```

### 3.2 模块清单

| 批 | 文件 | 内容 |
|---|---|---|
| 1 | `app/runtime/orchestration/local_task_router.py` | `normalize_command(text)`（NFKC + 去空白/标点 + 去礼貌前缀/尾语气词）；`LocalTaskRouter.route(request) -> RouteHandoff \| None`；`TaskFirstRouter(primary, fallback)` |
| 1 | `app/runtime/orchestration/app_launch.py` | `ApplicationSpec` 白名单（`calc` / `notepad`）、中英别名表、`resolve_application_id()`、`build_launch_proposal()`、`describe_target()`、`AppLaunchError`（固定短码） |
| 2 | `app/runtime/orchestration/task_dispatch.py` | `TaskDispatchSession`：把 预览→确认→执行→摘要 串起来（依赖注入 orchestrator / 确认读取器 / 摘要器） |
| 2 | `app/main.py` | 文本路径接线 + `--no-task-exec`（默认执行 on）；启动时装配白名单（含真实哈希计算） |
| 3 | `app/runtime/auto_voice.py`（+ `voice_entry.py` 复用） | 语音：先播报计划摘要 → **独立确认轮**（复用 VAD+ASR，只收一次）→「确认」才执行 → 播报摘要；超时 15 s → 拒绝并播报"已取消" |
| 4 | 真机 + 文档 | 文本 1 次 / 语音 1 次"打开计算器"；失败路径 3 种；artifact + 交接 |

### 3.3 安全边界（硬规则，任何一批都不得破）

1. **授权前零执行**：未拿到 L2 允许前，不创建 backend/runner、不计算备份、不启动进程；
2. `dangerous` / `file_operation` / `planning_request`：本批**不执行**，只回固定文案（fail closed）；
3. `TaskActionProposal` 只由**本地**拟（`application_id` 来自白名单）；LLM 给的动作参数**一律不采信**；
4. 别名**完全匹配**才命中（不做模糊/编辑距离），避免误启动；
5. 确认窗口绑定**计划哈希**、一次性消费（P5 原样）；确认词闭集＝{`确认`}；
6. 任何异常/超时/未确认 → deny；审计事件沿用 P5 的 `task_flow_*`；
7. 不改 `app/safety`、`app/executor`、`app/plans` 的语义；不新增工具。

### 3.4 拒绝与降级口径（固定短码，全部只进日志/摘要，不含路径与载荷）

`application_unknown`（未登记应用）、`application_ambiguous`（别名多义）、
`application_name_missing`（说了"打开"但没给名字 → `ambiguous/missing_target`，要求澄清）、
`task_exec_disabled`（`--no-task-exec`）、`task_route_unsupported`（本批未开放的意图）、
`authorization_denied`（其它输入/超时/取消）。

### 3.5 默认值

| 项 | 值 |
|---|---|
| 白名单 | `calc` → `%SystemRoot%\System32\calc.exe`；`notepad` → `%SystemRoot%\System32\notepad.exe`（不含 mspaint） |
| 别名（完全匹配） | 计算器/计算器应用/calc/calculator → `calc`；记事本/记事簿/notepad/note → `notepad` |
| 计划提议 | `action_id=app-launch-1`、`tool_name=start_whitelisted_application`、`arguments={"application_id": …}`、`order=1` |
| 确认词 | `确认`（允许前后空白与标点） |
| 确认超时 | 15 s（文本与语音同） |
| 默认开关 | 任务执行 **on**；`--no-task-exec` 一键回到"只对话" |
| 目标描述 | 确认提示里显示：应用名 + 可执行文件绝对路径 + 哈希前 12 位（有值时） |

---

## 4. 批次计划与验收标准

| 批 | 交付 | 验收 |
|---|---|---|
| 0 ✅ | 本文档（调研 + 设计） | 用户已批准设计（2026-10-13） |
| 1 | 本地路由 + 提议（纯离线） | 单测：句式/礼貌前缀/中英混排/缺名/未登记/别名大小写/多义；全量基线不降 |
| 2 | 装配 + 文本回路（`--no-task-exec`） | 单测：task 路由 → 预览 → 确认 → 假 backend 执行 → 摘要；`dangerous`/`file_operation` fail closed；默认 on |
| 3 | 语音确认轮 + 播报 | 单测：确认轮只认「确认」、超时拒绝、不误触发插话；语音链路零行为变化 |
| 4 | 真机验收 | 文本 + 语音各一次"打开计算器"：真实 PID、`LocalResultVerifier` 通过、审计事件齐全；失败路径 3 种如实拒绝；**听感由用户确认** |

---

## 5. 未做 / 风险（如实）

1. **未做**：文件类与删除/覆盖（fail closed）；只读 `query` 直执行（P8 起就留白）；LLM 结构化提议；多动作计划（本批只支持单动作）。
2. **风险**：`calc.exe`/`notepad.exe` 属系统文件，**Windows 更新会改哈希** ⇒ 装配时校验失败属**设计内 fail closed**（需重新登记，不是 bug）；
3. **风险**：语音确认轮与 barge-in/安全门的关系需在批 3 明确（确认期间**不得**被插话通道误判、也不得因"自听回声"取消确认）；
4. **未测**：真实 Provider 兜底路由是否稳定给 `open_application`（P7-R-28 只验过 1 次 `0.95`），批 2 会以"本地优先"避免依赖它。
