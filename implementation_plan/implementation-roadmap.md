# 分阶段代码实现路线图（规划阶段，不是代码）

> 编制日期：2026-09-19  
> 7. P0-P8 已完成并通过阶段级验收（P7 证据 `checkpoints/2026-09-30-P7-stage-acceptance-final.md`；P8 证据 `checkpoints/2026-09-30-P8-integration-acceptance.md` 与 `checkpoints/P8-handoff.md`）；主程序语音入口已接线（`python -m app.main --voice`，默认仍文字模式）。P7-R 支线已完成真实 DeepSeek 全链：R-27 真实调用与 `deepseek-route/v1` 协议验证、R-28 解除 provider 运行时门禁（双条件 opt-in、不 fallback）、R-29 接通真实对话 Agent；当前停在语音链真机确认、多轮/流式、任务链与 P9 的授权门前。
> 适用范围：电脑语音助手 MVP-1 至后续产品化。  
> 设计依据：`implementation_plan/` 下的架构、安全、错误恢复、音频、Agent 和 ActionPlan 文档，以及 `research/github-comparison-and-optimization.md`。

## 0. 本文件的职责

现有 `implementation_plan/` 保留为**设计与安全决策库**：解释“为什么这样设计、哪些规则不能改变”。本文件只解决“以后写代码时先写什么、一次写到哪里、怎样验收、什么时候必须停”。

未来任何实现会话都必须遵守：

1. 一次只执行一个阶段，默认一次只完成该阶段中的一个实现批次。
2. 阶段未通过退出条件，不进入下一阶段，不顺手重构、不顺手加功能、不顺手打包。
3. 每阶段开始前只读取本阶段卡片、上一阶段 handoff、明确列出的共享契约；不得默认读取整个 `../progress.md` 或全部讨论记录。
4. 每阶段只能修改该阶段的文件范围；跨范围修改必须先更新本路线图或明确记录为阻塞项。
5. 先写测试和故障注入场景，再写最小实现；涉及安全边界时，拒绝路径优先于成功路径。
6. 每阶段结束必须生成 handoff，记录实际完成内容、测试结果、已知风险和下一阶段入口。
7. P0-P8 已完成并通过阶段级验收（P7 证据 `checkpoints/2026-09-30-P7-stage-acceptance-final.md`；P8 证据 `checkpoints/2026-09-30-P8-integration-acceptance.md` 与 `checkpoints/P8-handoff.md`）；主程序语音入口已接线（`python -m app.main --voice`，默认仍文字模式）。P7-R 支线已完成真实 DeepSeek 全链：R-27 真实调用与 `deepseek-route/v1` 协议验证、R-28 解除 provider 运行时门禁（双条件 opt-in、不 fallback）、R-29 接通真实对话 Agent；当前停在语音链真机确认、多轮/流式、任务链与 P9 的授权门前。
8. 每个阶段完成全部批次后，必须先完成覆盖该阶段及其前置阶段的跨阶段集成验收、全量回归和安全审计，记录验收文档并生成 handoff；验收失败不得标记阶段 complete 或进入下一阶段。P1-P4、P5 和 P6 已按此规则通过集成验收，P6 证据为 `checkpoints/2026-09-21-P6-integration-acceptance.md`。

## 1. 总体实现顺序

```text
P0 实现冻结与环境基线
→ P1 运行时外壳与最小界面
→ P2 跨模块契约、事件和状态模型
→ P3 文字聊天与 Harness 会话
→ P4 结构化工具、路径安全、策略和 ActionPlan
→ P5 授权、执行、验证、审计与恢复
→ P6 ASR 按键输入
→ P7-R 意图路由契约与 ConversationManager 接缝（设计先行）
→ P7 TTS 输出与人物表达
→ P8 按键触发语音闭环
→ P9 VAD、连续对话、唤醒词与声纹过滤
→ P10 打断、AEC、设备恢复与长期运行
→ P11 Skill、隐私、手册、测试收口与 Windows 发布
```

这个顺序有意不同于主题编号 `02-tts`、`03-audio-capture`、`04-asr`、`05-agent`。主题编号是设计导航，不是编码依赖顺序。先做文字安全闭环，能在麦克风、模型或 TTS 不可用时验证最重要的任务安全事实。

## 2. 统一阶段卡片格式

每个阶段完成时必须将下面的字段填入该阶段 handoff；缺少任何一项都不能标记 `complete`：

```text
阶段编号：
阶段状态：not_started / in_progress / blocked / complete
本阶段唯一目标：
本阶段明确不做：
进入条件：
允许修改的文件范围：
依赖的稳定契约：
实现批次：
测试与故障注入：
退出条件（可观察事实）：
已知风险：
未解决但明确延期的问题：
下一阶段入口：
下一阶段禁止提前做的事项：
需要读取的最小文件集合：
```

## 3. 阶段总览

| 阶段 | 独立交付目标 | 进入前提 | 主要产出 | 通过后才允许做什么 |
|---|---|---|---|---|
| P0 | 把设计库冻结为可执行路线 | 规划文档完成本轮整理 | MVP 范围、契约清单、参数表、源码对比记录 | 建立项目代码外壳 |
| P1 | 程序可安全启动、关闭、记录 | P0 完成 | 配置加载、实例生命周期、结构化日志、最小文字壳 | 建立跨模块契约 |
| P2 | 所有关键对象和事件可序列化、可校验 | P1 可启动 | Session/Request/Plan/Auth/Action/Result/Audio/Provider 契约 | 写文字主链 |
| P3 | 文字闲聊与 Harness 会话隔离可用 | P2 契约通过 | 文字输入、persona、request 生命周期、Agent 故障映射 | 接入结构化工具 |
| P4 | 工具只能提出受控计划，不能直接越权 | P2/P3 完成 | ToolRegistry、Schema、PathGuard、PolicyEngine、ActionPlan | 实现授权和执行器 |
| P5 | 文字版安全执行闭环可验证 | P4 完成 | L2/L3、执行账本、验证、审计、停止/取消/恢复 | 接入 ASR/TTS 数据面 |
| P6 | Reliable single-turn ASR input | P5 complete | AudioBus, Capture, ASR, ResultGate, text fallback | Complete P7-R design seam before TTS implementation |
| P7-R | 意图路由契约与 ConversationManager 接入设计 | P6 完成 | RouteHandoff、两层 route+intent、追踪字段、失败规范化、文字/语音接缝 | 仅在实现授权后进入 IntentRouter 接入；未授权不得写代码 |
| P7 | Safe spoken output | P2/P3/P5 complete and P7-R design synced | Sentence, TTS Router, Playback, VoiceProfile, fallback | Compose the voice loop |
| P8 | 按键触发的半双工语音闭环可用 | P5/P6/P7 完成 | ASR→Agent→TTS 组合、统一任务事实 | 增加自动化交互 |
| P9 | 自动收音与连续对话不改变安全边界 | P8 稳定 | VAD、唤醒词、声纹过滤、会话状态 | 处理全双工体验 |
| P10 | 打断/AEC/设备故障可控 | P9 稳定 | Barge-in、AEC、设备 epoch、恢复 | 产品化收口 |
| P11 | Skill、隐私、手册和发布验收完成 | P5-P10 有稳定基线 | Skill Manifest、保留清理、攻击性测试、打包 | 进入发布/维护 |

---

## P0：实现冻结与环境基线

> 阶段状态：**complete（2026-09-19）**；handoff：`checkpoints/P0-handoff.md`。

### 唯一目标
把现有设计决策整理成“能交给一个编码会话执行”的冻结清单，并建立不依赖具体个人设备、路径、API key 或模型缓存的环境基线。

### 本阶段不做
- 不创建业务代码骨架。
- 不安装或下载最终运行依赖。
- 不实现任何 Tool、ASR、TTS、Agent 或界面功能。
- 不把用户个人路径、音色、声纹和 API key 写入公开实现计划。

### 进入条件
- `implementation_plan/` 的安全、错误恢复和音频故障原则已整理。
- GitHub 源码对比记录已经固定到 commit，而不是只写项目名称。
- MVP-1 与延期功能边界明确。

### 允许修改的文件范围
- `implementation_plan/` 文档；
- `../task_plan.md`、`../progress.md`；
- 项目根目录的 `../.gitignore`，仅用于排除敏感信息、本地环境和本地运行数据；
- 不允许创建项目源代码目录。

### 必须冻结的跨模块契约
- 标识：`instance_id`、`session_id`、`utterance_id`、`request_id`、`task_id`、`plan_id`、`execution_id`、`action_id`、`attempt_id`。
- 安全事实：`plan_hash`、`authorization_snapshot_hash`、目标状态快照、确认窗口、过期时间。
- 状态：任务状态与音频状态正交；音频故障不能改变任务/授权事实。
- 重试：传输层、Agent 请求层、电脑动作层分开；未知副作用结果先扫描，不重试。
- 恢复：崩溃后不续跑、不复用旧授权、不自动补做、不自动回滚。

### 退出条件
- 有一份可执行的阶段表；每个阶段有文件范围、入口、出口和停止点。
- 有一份固定 commit 的源码对比表。
- 所有未决项都被标记为“冻结”“默认值+可校准范围”或“明确延期”。

### 停止点
达到上述条件后停止；不要开始 P1。

### 最小读取文件
`implementation_plan/README.md`、本文件、`shared/architecture.md`、`shared/safety-boundaries.md`、`shared/error-recovery.md`、`shared/action-plan.md`。

---

## P1：运行时外壳与最小界面

> 阶段状态：**complete（2026-09-19）**；handoff：`checkpoints/P1-handoff.md`。

### 唯一目标
程序能够启动、显示正式文字界面、接受键盘输入、执行安全关闭，并记录不含敏感内容的结构化生命周期事件。

### 本阶段不做
- 不接 DeepSeek 请求。
- 不接麦克风、ASR、TTS、VoiceStudio。
- 不实现电脑工具和执行动作。
- 不创建唤醒词、VAD、AEC、Skill 或 Windows 常驻。

### 进入条件
P0 退出条件通过，Python 版本、依赖安装方式、模型目录、用户配置和本地数据根目录已确定为可配置项。

### 建议文件范围
```text
app/main.py
app/config/
app/runtime/lifecycle.py
app/ui/text_shell.py
app/logging/
tests/foundation/
```

不要一次创建未来所有空目录；只有进入相应阶段时再创建 `audio/`、`asr/`、`tts/`、`tools/` 等实现目录。

### 实现批次
1. 配置加载与敏感配置检查；
2. 单实例/实例标识和安全退出；
3. 结构化日志、脱敏和日志目录初始化；
4. 最小文字输入/输出壳；
5. 临时目录、计划目录、审计目录的归属检查。

### 测试与故障注入
- 缺少 API key、配置格式错误、目录不可写、重复启动、正常关闭。
- 日志中不得出现 API key、音频内容、参考音频路径、完整文件内容。
- 强制结束模拟只验证启动时能发现未完成任务，不续跑。

### 退出条件
- 程序可启动和正常关闭；
- 生命周期事件有 `instance_id` 和单调序号；
- 配置和本地私有数据与公开代码分离；
- UI 可显示“文字模式/语音不可用/任务空闲”等状态，但不伪造成功事实。

### 停止点
通过后只提交本阶段变更和 handoff；不得顺手接入 Agent。

### 最小读取文件
本阶段卡片、`01-project-foundation.md`、`shared/architecture.md`、`shared/data-and-privacy.md`。

---

## P2：跨模块契约、事件和状态模型

> 阶段状态：**complete（2026-09-19）**；handoff：`checkpoints/P2-handoff.md`。

### 唯一目标
建立所有后续模块共同使用的可校验数据模型和事件边界，先保证“状态能被准确表达”，再写功能。

### 本阶段不做
- 不调用真实 Agent、ASR、TTS 或电脑工具。
- 不实现 UI 业务流程。
- 不实现自动恢复执行。

### 建议文件范围
```text
app/contracts/
app/events/
app/state/
tests/contracts/
tests/state/
```

### 必须定义的对象
```text
SessionContext
Utterance
AgentRequest / AgentResponse
TaskContext
ActionPlan / PlanVersion
AuthorizationSnapshot
ExecutionLedger
ActionAttempt
ActionResult / ResultVerification
AudioFrame / AudioSession / ProviderState
TTSReply / TTSSegment / PlaybackFact
AuditEvent
```

### 必须定义的正交状态
```text
任务：IDLE / UNDERSTANDING / PLANNING / WAITING_AUTH / EXECUTING / VERIFYING / COMPLETED / CANCELLED / ABORTED / UNCERTAIN
Provider：READY / FAILED / COOLDOWN / PROBING / RECOVERED_PENDING
输入：IDLE / WARMING / ACTIVE / FINAL_ACCEPTED / REJECTED / CLOSING / FAILED
输出：QUEUED / SYNTHESIZING / READY / COMMITTED_NOT_STARTED / PLAYING / PLAYED / ABORTED
```

不要把所有状态压进一个总枚举；状态机组合必须由事件和不变量约束。

### 关键不变量
- `request_id` 一经提交，ASR/TTS/网络故障都不能让同一请求被隐式重发。
- `plan_hash`、授权快照或目标状态变化会使授权失效。
- 任何副作用动作都有 `execution_id + action_id + attempt_id`。
- `state_uncertain` 永远不能自动变成成功、失败或可重试。
- 音频事件必须带 `audio_session_id`、`provider_generation`、`stream_id`、`event_sequence`。
- TTS 播放边界只使用可观测事实，不声称“用户一定听见”。

### 测试与故障注入
- JSON Schema/类型校验、未知字段、缺字段、版本迁移。
- 事件乱序、迟到、重复、旧 generation、旧 stream、重复消费。
- 授权快照被修改、过期、重复使用。
- 账本事件中断、完成记录缺失、审计链断裂。

### 退出条件
所有核心对象可序列化、反序列化、拒绝非法状态转移；测试可证明旧授权、旧音频、旧请求不能跨边界复用。

### 停止点
不实现 P3 主流程；先把契约和测试结果写入 handoff。

### 最小读取文件
`shared/action-plan.md`、`shared/error-recovery.md`、`shared/safety-boundaries.md`、`02-tts-voicestudio.md`、`04-asr.md`。

---

## P3：文字聊天与 Harness 会话

> 阶段状态：**complete（2026-09-21，阶段级）**（由 P1–P4 集成验收覆盖）；验收：`checkpoints/2026-09-21-P1-P4-integration-acceptance.md`；handoff：`checkpoints/P3-handoff.md`。

### 唯一目标
用键盘完成自然文字闲聊和最小 Agent 请求，验证 persona、会话隔离、请求 ID 和 Agent/API 故障停止规则。

### 本阶段不做
- 不暴露电脑工具。
- 不生成或执行 ActionPlan。
- 不接语音。
- 不自动重试或自动切换其他 Agent。

### 建议文件范围
```text
app/agent/
app/conversation/
app/persona/
tests/agent/
tests/conversation/
```

### 实现批次
1. DeepSeekHarness adapter 与延迟会话创建；
2. 闲聊会话和工作会话容器（工作会话先只做占位）；
3. `request_id` 生成、流式文本拼接和完整性判定；
4. persona 读取、版本和用户修改边界；
5. 错误映射：连接、限流、超时、流中断、空响应、未知事件、Schema 错误。

### 退出条件
- 首次请求前不创建模型会话；
- 闲聊与工作上下文不串；
- 不完整输出不进入正式历史；
- Agent 故障不自动重试、不自动换 Agent；
- 只有用户明确要求，才产生新的 `request_id`。

### 停止点
通过后不要接工具；下一阶段单独建立 ToolRegistry。

### 最小读取文件
`05-agent-integration.md`、`00-project-overview.md`、`persona.md`、`shared/error-recovery.md`。

---

## P4：结构化工具、路径安全、策略和 ActionPlan

> 阶段状态：**complete（2026-09-21，阶段级）**（由 P1–P4 集成验收覆盖）；验收：`checkpoints/2026-09-21-P1-P4-integration-acceptance.md`；handoff：`checkpoints/P4-handoff.md`。

### 唯一目标
让 Agent 只能提出结构化、可审查的计划；任何电脑动作必须经过本地 Schema、PathGuard、PolicyEngine 和 ActionPlan，不允许模型直接执行命令。

### 本阶段不做
- 不接语音。
- 不做最终授权弹窗和执行。
- 不实现 Skill 动态导入。
- 不开放任意 PowerShell、Python、Shell 或管理员权限。

### 建议文件范围
```text
tools/
safety/schema_validator.py
safety/path_guard.py
safety/policy_engine.py
plans/
tests/tools/
tests/safety/
tests/plans/
```

### 实现批次
1. [x] Tool Manifest：名称、输入/输出 Schema、风险级别、路径范围、超时、输出上限、幂等性、取消能力、预览和验证方式（2026-09-20 完成；交接：`checkpoints/2026-09-20-P4-batch1-tool-manifest-stop-handoff.md`）；
2. [x] 先实现只读工具：`read_text_file`、`list_directory`、`search_files`、`get_processes`（2026-09-20 完成；交接：`checkpoints/2026-09-20-P4-batch2-read-only-stop-handoff.md`）；
3. [x] 再实现受控写工具的“计划生成/预览”而不执行（2026-09-20 完成；交接：`checkpoints/2026-09-20-P4-batch3-write-preview-stop-handoff.md`）；
4. [x] PathGuard：规范路径、真实路径、Reparse/Junction/UNC/映射盘处理、执行前再次解析（2026-09-20 完成；交接：`checkpoints/2026-09-20-P4-batch4-path-guard-stop-handoff.md`）；
5. [x] PolicyEngine：只接受可信结构化快照，输出 `allow/deny/require_confirmation`（2026-09-20 完成；交接：`checkpoints/2026-09-20-P4-batch5-policy-engine-stop-handoff.md`）；
6. [x] plan.json 与 execution_plan.md 的版本、哈希和严格解析规则；Markdown 只允许隔离的用户备注，不能改变安全字段（2026-09-20 完成；交接：checkpoints/2026-09-20-P4-batch6-action-plan-stop-handoff.md）；
7. [x] `result.json` 的版本、哈希和严格解析规则，并完成 P4 阶段回归与收口（2026-09-20 完成；交接：`checkpoints/P4-handoff.md`）。

### 退出条件
- 非法工具名、参数、路径、风险等级和任意命令全部被本地拒绝；
- 工具返回的自然语言不能直接变成下一步参数；
- 只读工具可生成结构化结果；
- 计划哈希稳定且 Markdown 不能改变安全字段；
- 模型不能修改黑名单、授权规则和工具注册表。

### 停止点
只完成“可审查计划”，不要进入执行器和授权。

### 最小读取文件
`06-tools-and-policy.md`、`shared/action-plan.md`、`shared/safety-boundaries.md`、GitHub 对比文档中 MCP/OPA/Windows 路径条目。

---

## P5：授权、执行、验证、审计与恢复

> 阶段状态：**complete（2026-09-21）**；验收：`checkpoints/2026-09-21-P5-integration-acceptance.md`；handoff：`checkpoints/P5-handoff.md`。

### 唯一目标
在文字模式下完成最小、可停止、可验证的电脑任务安全闭环；这是整个项目的主安全基线。

### 本阶段不做
- 不接麦克风和 TTS。
- 不自动续跑崩溃任务。
- 不实现 Skill。
- 不扩大工具清单。

### 建议文件范围
```text
safety/confirmation/
executor/
verification/
auditing/
recovery/
tests/security/
tests/recovery/
```

### 实现批次
1. [x] L2 模态确认和 `AuthorizationSnapshot`（2026-09-20 完成；交接：`checkpoints/2026-09-20-P5-batch1-l2-confirmation-stop-handoff.md`）；
2. [x] L3 独立确认状态、固定短语、一次性消费和防重放（2026-09-20 完成；交接：`checkpoints/2026-09-20-P5-batch2-l3-consumption-stop-handoff.md`）；
3. [x] 执行前重新校验路径、哈希、参数、计划版本和目标状态（2026-09-20 完成；交接：`checkpoints/2026-09-20-P5-batch3-execution-preflight-stop-handoff.md`）；
4. [x] Execution Ledger：`execution_id/action_id/attempt_id/executor_instance_id/fencing_token`（2026-09-20 完成；交接：`checkpoints/2026-09-20-P5-batch4-execution-ledger-stop-handoff.md`）；
5. [x] 原子动作边界、协作取消和不可安全中断声明（2026-09-21 完成；交接：`checkpoints/2026-09-21-P5-batch5-atomic-cancellation-stop-handoff.md`）；
6. [x] 备份、执行、ResultVerifier、部分成功和 `state_uncertain`（2026-09-21 完成；交接：`checkpoints/2026-09-21-P5-batch6-backup-execution-verification-stop-handoff.md`）；
7. [x] 审计哈希链、脱敏、恢复扫描和恢复报告（2026-09-21 完成；交接：`checkpoints/2026-09-21-P5-batch7-audit-recovery-stop-handoff.md`）；
8. [x] 分层重试：只读查询可有限重试；Agent 生成必须新 request；副作用动作默认不自动重试；未知结果先扫描（2026-09-21 完成；交接：`checkpoints/2026-09-21-P5-batch8-layered-retry-stop-handoff.md`）。

### 关键安全流程
```text
plan.json 冻结
→ 生成授权快照
→ 用户确认
→ 执行前重新扫描
→ 账本原子记录 operation_started
→ 单个动作/原子动作执行
→ 记录完成或 state_uncertain
→ ResultVerifier
→ result.json + 审计
```

### 故障注入
- API 故障发生在规划、授权等待、执行前、执行中和汇报后；
- 用户“停”、取消、关闭程序；
- 文件哈希变化、路径替换、链接/Junction 替换；
- 进程崩溃、审计写入失败、结果写回失败；
- L3 短语重复提交、半句、过期窗口、旧 plan_hash。

### 退出条件
- 文字任务能完成只读、L2 和最小 L3；
- 停止后不补做、不回滚、不重试，能重新扫描并准确分类；
- 崩溃重启后旧计划和授权不可执行；
- 已完成动作但自然语言汇报失败不会重复执行；
- 测试证明音频未接入时安全链也完整。

### 停止点
这是第一个强制停点。即使文字闭环已经可用，也不要在同一会话接 ASR/TTS。

### 最小读取文件
`07-confirmation-and-stop.md`、`08-text-end-to-end.md`、`shared/action-plan.md`、`shared/error-recovery.md`、`shared/safety-boundaries.md`。

---

## P6：ASR 按键输入

> 阶段状态：**complete（2026-09-21）**；批次 1–6 complete；阶段验收：`checkpoints/2026-09-21-P6-integration-acceptance.md`；handoff：`checkpoints/P6-handoff.md`；当前停在 P7/TTS 授权门前。

### 唯一目标
一次按键触发一次半双工语音输入；只有通过 ResultGate 的可靠 final 结果才能创建 Agent 请求，否则回到文字模式或提示重说。

### 本阶段不做
- 不做连续监听、唤醒词、声纹、AEC、播放期间监听、实时打断。
- 不做语音 L3 授权；MVP 中高风险语音确认默认转文字或界面确认，除非实现了额外可靠性证明。
- 不接未配置的云端 ASR 或 Windows 语音服务。

### 建议文件范围
```text
audio/capture/
audio/bus/
asr/
tests/audio/
tests/asr/
```

### 实现批次
1. AudioBus 与 `AudioFrame`：设备 epoch、sequence、采样率、声道、格式、时间戳；**complete（2026-09-21）**；
2. [x] sounddevice 采集、设备选择、pre-roll、临时资源清理（2026-09-21 完成；交接：`checkpoints/2026-09-21-P6-batch2-capture-device-preroll-cleanup-stop-handoff.md`）；
3. [x] VAD 只作为本阶段的结束判定，可先使用固定按键释放或最小静音策略（2026-09-21 完成；交接：`checkpoints/2026-09-21-P6-batch3-vad-stop-handoff.md`）；
4. [x] sherpa-onnx/SenseVoice adapter（2026-09-21 完成；交接：`checkpoints/2026-09-21-P6-batch4-sensevoice-adapter-stop-handoff.md`）；
5. [x] final 接纳、interim 丢弃、重复/迟到/乱序/旧 generation 拒绝（2026-09-21 完成；交接：`checkpoints/2026-09-21-P6-batch5-result-gate-stop-handoff.md`）；
6. [x] ASR startup/session/runtime 故障和文字模式降级（2026-09-21 完成；交接：`checkpoints/2026-09-21-P6-batch6-recovery-text-fallback-stop-handoff.md`）。

### 音频事实
- `unsupported` 不等于 `not_detected`；没有可靠的多人重叠能力时不假设安全。
- 采集 callback 粒度、AudioBus 帧大小和 ASR 解码块大小分离；20ms 只作为候选总线/VAD粒度，最终以实测校准。
- 事件必须验证 `audio_session_id + provider_generation + stream_id + utterance_id + event_sequence`。

### 退出条件
- 清晰语音能生成一次 final 并只提交一个新 request；
- 迟到、重复、半句、低可靠或故障后的 final 不会提交；
- ASR 不可用时文字输入仍能完成 P5；
- 临时音频按规则清理，不进入 Agent/日志/审计正文。

### 停止点
通过后只总结 ASR，并强制停在 P7/TTS 授权门前；不要接 TTS、连续监听或 P8 组合闭环。

### 最小读取文件
`03-audio-capture.md`、`04-asr.md`、`shared/data-and-privacy.md`、`shared/error-recovery.md`。

---

## P7：TTS 输出与人物表达

> 阶段状态：**complete（2026-09-30，阶段级）**；验收：`checkpoints/2026-09-30-P7-stage-acceptance-final.md`（五项退出条件逐条证据）；批次跟踪：`P7-followup-execution-tracker.md`。
> 边界：默认语音仍为 opt-in（`TTS_VOICESTUDIO_MODE=disabled`，用户 2026-09-30 决定）；未接"播放时监听/AEC/全双工"；未进入 P8。真实 DeepSeek 路由（R 支线）仍 `NOT READY / STOP`，与本次验收无关。

### 唯一目标
把已确认的文字回复按句子边界播报，并在 VoiceStudio、预先配置的 Edge TTS 和文字输出之间安全降级。

### 本阶段不做
- 不接 ASR。
- 不实现播放期间持续监听、AEC、全双工打断。
- 不自动创建、切换、覆盖、删除 VoiceProfile。
- 不把“音频生成完成”当作“用户已经听到”。

### 建议文件范围
```text
tts/domain/
tts/segmentation/
tts/providers/
tts/playback/
tts/recovery/
tests/tts/
```

### 实现批次
1. VoiceProfile 版本与用户唯一修改权限；
2. 文本流→完整句 segment；
3. Provider capability：文本流、音频流、取消、音色切换、格式和估计延迟；
4. `reply_id/segment_id/provider_request_id/chunk_index/playback_offset_ms`；
5. 播放事实：`NOT_COMMITTED`、`COMMITTED_NOT_STARTED`、`PLAYBACK_STARTED`、`PARTIALLY_PLAYED`、`PLAYED`；
6. 队列句数、PCM 时长预算、背压、取消和清空；
7. VoiceStudio→Edge→文字降级；半句已提交/已播放时不混声、不重播；
8. 固定无隐私探测、冷却和恢复待启用。

### 退出条件
- 文字回复可按原序播放；
- 未提交播放设备前可按配置切 Edge；播放已开始后不在当前 segment 换声；
- 被打断的旧回复不跨 Provider 续播；
- TTS 故障不改变任务、授权、计划和结果事实；
- VoiceProfile 变更只在允许的句子/回复边界生效。

### 停止点
通过后仍不接“播放时监听”；下一阶段只做组合闭环。

### 最小读取文件
`02-tts-voicestudio.md`、`persona.md`、`shared/data-and-privacy.md`、TTS GitHub 对比条目。

---

## P8：按键触发的半双工语音闭环

> 阶段状态：complete（2026-09-30，阶段级）。批次 1-6 全部完成：轮次边界与状态契约、统一输入接缝与文字降级、闲聊闭环接线（真实 P6 管道离线集成）、任务路径编排（冻结计划 → L2/L3 → 复检 → 执行 → 验证 → 摘要）、“停”三语境与迟到事件隔离、阶段级集成验收。路线图五条退出条件逐条通过（离线阶段级证据）；全量 1266 tests OK (skipped=3)。
> 验收：`checkpoints/2026-09-30-P8-integration-acceptance.md`；阶段交接：`checkpoints/P8-handoff.md`；批次 1-3 交接：`checkpoints/2026-09-30-P8-batch1-3-voice-loop-chat-closed-loop-stop-handoff.md`。
> 停点：未接真实 DeepSeek Provider（R 支线仍 NOT READY / STOP）、未做真实麦克风/扬声器之外的额外真机项、未进入 P9。主程序语音入口已于 2026-09-30 接线（`python -m app.main --voice`，默认仍文字模式；见 `checkpoints/2026-09-30-P8-voice-entry-main-wiring-handoff.md`）。

### 唯一目标
组合 P5、P6、P7：按键开始说话，得到文字，调用同一个 Harness，文字回复可播报，电脑任务仍走完全相同的安全链。

### 本阶段不做
- 不增加自动唤醒。
- 不增加连续多轮自动收音。
- 不增加播放期间监听、AEC 或声纹认证。
- 不改变 P5 的授权规则。

### 建议文件范围
```text
app/voice_loop/
app/runtime/orchestration/
tests/voice_loop/
```

### 退出条件
- 闲聊：按键→ASR→Agent→TTS；
- 任务：按键→ASR→Agent→ActionPlan→授权→执行→验证→播报摘要；
- ASR/TTS 任一不可用时仍可切换文字，不影响任务事实；
- 每轮有独立 session/utterance/request/reply 边界；
- 说“停”在聊天、授权、执行三种上下文分别符合规则。

### 停止点
这是 MVP-1 的语音闭环交付点。先运行一段时间和记录问题，不要直接进入全双工。

---

## P9：VAD、连续对话、唤醒词与声纹过滤

> 阶段状态：**complete（2026-10-01；阶段级集成验收通过）**。验收证据：`checkpoints/2026-10-01-P9-stage-acceptance.md`（五条退出条件逐条判定 + 全量 2084 tests OK + 真机逐项确认）。**P0–P9 整体测试与验证（阶段 4/阶段 5）已于 2026-10-02 完成**：全量重跑复核 2084 tests OK (skipped=3)、`compileall` exit 0、交付物 21/21 齐备，证据 `checkpoints/2026-10-02-P0-P9-stage-acceptance.md`；同日**收尾批次**修复 3 个孤儿测试（19→22）并去掉 1 个 BOM，当前基线 **2087 tests OK (skipped=3)**、全仓 BOM 回到基线 25，阶段交接 `checkpoints/P9-handoff.md`。批次 1-2（自动收音与连续对话边界）已于 2026-10-01 完成并真机验证：`app/dialog/**`、`app/audio/vad/auto_capture.py`、`app/voice_loop/auto_driver.py`、`tools/p9_auto_voice_runner.py`；真机免按键试听通过且用户确认听到播报（`user_heard=true`）。
> B3 唤醒词门禁与 B4 声纹过滤已同日完成并真机正/负例验收（`app/audio/kws.py`、`app/audio/voiceprint.py`）。
> UI 批次 1-2 与**视觉改版（方案 4：浅色卡片胶囊 + 逐像素分层窗口）**已完成并真机确认：`app/ui/hud_render.py`（新增纯像素核心）、`app/ui/voice_hud.py`（DIB/`UpdateLayeredWindow`/GDI 文字/一次性降级）、`VOICE_HUD_SCHEMA_VERSION=3`。
> **免按键自动收音已接入主程序**：链路抽成共享层 `app/runtime/auto_voice.py`，主程序用 `python -m app.main --voice --auto` 复用同一条实现（Silero VAD + 段级端点），runner 改为消费同一份代码；装配失败一律降级为文字。
> 交接：`checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`、`checkpoints/2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`、`checkpoints/2026-10-01-P9-hud-visual-refresh-handoff.md`、`checkpoints/2026-10-01-P9-auto-capture-main-integration-handoff.md`；设计规格：`2026-10-01-hud-visual-refresh-design.md`；接口冻结：`checkpoints/2026-09-30-P9-batch1-2-interface-freeze.md`。
> 同批修复的真机缺陷：丢失轮次锁死 ASR 通道（`abandon_turn`）、自动结束被误当故障丢弃、edge 播报 `PCMPlaybackBackpressure`（`EdgeStream` 按 `queued_audio_ms` 节流）；UI 改版的 6 个真机缺陷（能力探测早于建窗、DIB 结构体构造、`SetLayeredWindowAttributes` 与 `UpdateLayeredWindow` 互斥、旧深色默认值、api/HUD 同名属性误读、窗外像素不透明化 + 抗锯齿 alpha）见 UI 交接 §4；抽取共享层时踩到的 5 类问题见免按键交接 §5。全量 **2055 tests OK (skipped=3)**（"免按键接入主程序"批次的中间值；本阶段收口值见下）。
> 未做（留给 P10 / 后续）：AEC 与全双工、打断、设备热插拔与长期运行（P10 正题）；多轮/SSE/限流/persona；`threshold_mode=auto` 接 CLI；任务链消费真实路由；提示窗圆角约 2px 透明小瑕疵。

### 唯一目标
在已经稳定的半双工闭环上增加自动化输入，但任何误唤醒、声纹误判或会话恢复都不能绕过计划、授权和停止规则。

### 本阶段不做
- 不做 AEC 的最终方案。
- 不把声纹当授权或身份认证。
- 不允许恢复组件自动恢复旧 utterance/request。
- 不允许后台自动切入用户正在输入、查看方案或授权的时刻。

### 建议文件范围
```text
wake/
voiceprint/
audio/vad/
dialog/
tests/wake/
tests/dialog/
```

### 退出条件
- VAD 只决定输入边界，不决定是否授权；
- 唤醒词只开启新的输入会话；
- 声纹只做误触发过滤，不替代 L2/L3；
- 连续对话闲置、取消、任务结束和错误后边界清晰；
- 误唤醒不会调用工具或执行动作。

---

## P10：打断、AEC、设备恢复与长期运行

> 阶段状态：**in_progress（2026-10-02 起）**。已交付批次：**R1a**（两阶段打断状态机，`app/duplex/barge_in.py`，56 项）、**R1b**（编排语义：平滑恢复/轮次交接/提示失效，`app/duplex/barge_in_orchestration.py`，40 项）、**R2a**（AEC/降噪逐层降级框架，`app/audio/aec/__init__.py`，34 项）、**R2b-1**（AEC 参考时间线 + 逐样本 NLMS，`app/audio/aec/reference.py`、`nlms.py`，39 项；合成回声尾部中位 ERLE ≈ 48 dB）、**R2b-2**（真机实测：**回环参考在本机实测不可用**（四轮证据），改按用户批准实施**方案 A：同进程提交 PCM 参考**——`app/audio/aec/reference_source.py` + `app/tts/waveout_pcm.py` 只读旁路，45 项；**真机 ERLE 无有效数值**：笔记本内置阵列 DSP 不可关闭）、**R3**（设备会话与安全空闲点恢复，`app/devices/session.py`，34 项）、**R4**（会话循环接线：独立打断通道 `app/duplex/barge_in_channel.py` + 安全门 `app/duplex/barge_in_safety.py` + 接进 `app/runtime/auto_voice.py`；可选播放增益钩子 `PlaybackGain`/`gain_provider`；默认全关，135 项；**真机三轮暴露并修掉 3 个真实缺陷**：音量反复跳动（无参考不降音量 + 最小疑似帧 3 + release 800 ms）、确认结构性不可达（窗口粘滞 + 能量门）、**假确认**（没插话也停播 → 无可用回声参考时**整个打断 fail-closed**，启动打印原因）；另在 check-only 真机核查中修掉参考采样率错配；**本机结论：内置麦克风+扬声器不具备安全打断条件，缺"可信判别依据"**，开源调研见 `research/2026-10-02-realtime-voice-barge-in-open-source-research.md`）。交接见 `checkpoints/2026-10-02-P10-R1a-*.md`、`-R1b-*.md`、`-R2a-*.md`、`-R2b-1-*.md`、`-R2b-2-real-device-handoff.md`、`-R3-device-recovery-handoff.md`、`-R4-session-loop-handoff.md`。全量基线 2087 → **2496 tests OK (skipped=3)**（含批次 R4-A：参考同钟 + 对齐估计 + 回声可观测性安全门；真机实测安全门拦下假停播）。**R4-B 前置**（2026-10-03，只读可行性核查，入档 `research/2026-10-03-P10-R4B-aec-frontend-feasibility.md`；首选 `pywebrtc-audio` 0.2.0（cp312-win wheel 免编译），兜底 `pyaec`；排除 webrtc-audio-processing / webrtc-apm / speexdsp / speexdsp-ns）；**R4-B-1**（真 AEC3 后端：`app/audio/aec/webrtc_backend.py` + `reference_source.py` 可选后端（默认 nlms 行为不变）+ `tools/p10_r4b_aec_bench.py`，29 项；离线实测合成回声累计 ERLE 23–26 dB、**本机 0.157 ms/帧 ≈127× 实时**、20 ms 与 2×10 ms 逐样本等价；**离线证伪了一个误判**：参考有能量时即使没有回声路径 AEC3 也有 ≈11.7 dB 抑制 ⇒ 抑制量**不能**当作"回声已消除"，故新增 `suppression_evidence` 分级且**不含"已确认回声路径"档**）；**R4-B-2 阶段 0**（AEC3 **双讲保持**离线 go/no-go，工具 `tools/p10_r4b2_doubletalk_probe.py`：**回声 ≥ 用户档双讲把用户语音压 −5.5 ~ −20.5 dB、残差用户语音 RMS 93–522（5/6 低于能量门 600）**；`near_only` 12/12 次 −0.00 dB；`far_only` ERLE 15.7–37.9 dB；分段增益波动 −1.8 ~ −37.7 dB；**结论=条件性 no-go**，衰减随"回声/用户语音比"变化，本机大概率落在坏档 ⇒ **暂停接线**，AEC3 后端保留但默认关闭，证据 `research/2026-10-03-P10-R4B-2-doubletalk-findings.md`）；**R4-B-2 收口**（方案 C **冷却窗口** `barge_in_cooldown`：`BargeInConfig.cooldown_ms` 默认 0、通道用 `dataclasses.replace` **只覆盖冷却时长**、CLI `--barge-in-cooldown-ms`（建议 1000）、启动提示补冷却与 `echo_proven=false` 提示；**C1 口径：不改 R4-A 的 fail-closed**；语义=收帧/填前置缓冲但不降音量不判疑似、只影响 IDLE、0 等价改动前；28 项新测试，全量 **2553 tests OK (skipped=3)**；交接 `checkpoints/2026-10-03-P10-R4B-2-planC-cooldown-handoff.md`）；**R4-B-3/4 真机量测**（`tools/p10_r4b3_echo_ratio_probe.py` / `tools/p10_r4b4_realtime_doubletalk_probe.py`，纯测量未改生产代码：回声/用户语音比 **−7.1 dB**、ERL 13.7 dB、回声自身 +3.0 dB 超门；真机 AEC3 **ERLE 10.3 dB（实测延迟）/15.5 dB（最佳 350 ms）**，但 **28–33% 的 20 ms 帧残余回声仍超能量门 600**，双讲后用户语音 +8.8 dB 高于门；0–500 ms 内 ERLE **7–15.5 dB**（延迟敏感）⇒ **"AEC3 不是不可用，而是不够用"**，需 **三件套**：AEC3 + 冷却窗口 + **残余地板感知动态门槛**（未做）；证据 `research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`）；**R4-B P1/P2**（真 AEC3 **接入打断通道**：`--aec-engine nlms|webrtc`（默认 nlms 不变）、每帧用残差算电平/门（检测器仍看原帧）、对齐延迟喂 `stream_delay_ms`、只有引擎真在跑才报 `CAPABILITY_SOFTWARE`；**残余回声地板感知动态门** `max(energy_threshold, 地板×margin)`（地板=1 s 残差 p20、只抬不降）；失败路径 `aec_process_failed`/`aec_output_invalid`/`reference_silent` 不冒泡；27 项新测试，全量 **2580 tests OK (skipped=3)**；交接 `checkpoints/2026-10-03-P10-R4B-P1P2-aec-engine-residual-gate-handoff.md`；**R4-A fail-closed 口径未改** ⇒ 本机仍不真停播，三件套真机试听待授权）；**R4-B 真机阻塞溯源**（三件套真机试听 8 轮全 `text_only` ⇒ 自检 `tools/p10_r4b_channel_selfcheck.py` 证明输入侧与裸 speaker 均正常；**根因**：`chat_loop._speak()` 把 `start_stream()` 异常吞成 `text_only` ⇒ 真因不可见；**已修** `speech_start_failed:<ExceptionName>`，5 项新测试，全量 **2585 tests OK (skipped=3)**；会话侧真因仍需一次真机运行读取）；**回声证明收紧**（用户反馈"说一半就停"⇒ 真机 turn 2/4 没插话却 `barge_in_interrupted`、`echo_proven=true`、相关 0.60/0.72 但估计延迟 **746 / −780 / 1589 ms**（本机实测≈390–410 ms）⇒ 伪相关骗过"只看相关性"的旧门槛；**修复**：延迟物理窗口 0–500 ms + 连续 3 次稳定才算证明，新增 `echo_proof_state` 如实上报；15 项新测试含真机伪证明数据回归，全量 **2600 tests OK (skipped=3)**；**复测通过前本机仍用 `--barge-in-mode off`**）；**播报截断事实可视化**（`--barge-in-mode off` 复测仍偶发中断 ⇒ 16 轮全无 `barge_in_interrupted`，但一律记 `speech_finished`；机制：`SessionStream.finish()` 的有界 join 超时后 `close()→cancel()` 取消剩余语音，而 `finish_timed_out`/`late_events` 从未被读取、连公开 `record` 都没有；**修复**：公开 `record` + 记截断事实 + `_speak()` 如实报 `speech_truncated` + `voice_turn` 增 `speech_*` 九项；8 项新测试，全量 **2608 tests OK (skipped=3)**；**截断本身未修**，待真机读新字段定案）；**播报截断修复**（第 4 轮真机点名：133 字 `speech_finished` 6 段全播 vs **249 字 `speech_failed` + `cancelled=true` 且 finish 事实全 null**；根因=句子队列预算 8 段 + 闭环整段一次性推送 ⇒ `streaming_queue_budget_exceeded` ⇒ 取消剩余语音；**修复**=`split_for_push()` 按句切分 + 有界背压 + `speech_failed:<ExceptionName>`；5 项新测试，全量 **2613 tests OK (skipped=3)**；**待真机复测**）。
> 未做（待授权）：**R2b 真机验证的前置条件**（需**外接麦克风**，届时可恢复第一阶段降音量并验证 ERLE）；**R4 残留**（前置音频并入下一轮 ASR 需 audio 层 pre-roll 注入接口；输出设备重连需固定输出设备 id；**打断判别依据**：方案 A（参考同钟）已证本机样本级不可相关；真 AEC 前端（AEC3）后端已离线就位（R4-B-1，**默认关闭**），**会话接线与真机 ERLE 待 R4-B-2**；轮次边界冷却仍未做）；**R5** Provider 冷却/有界探测/恢复待启用；**R6** 长期运行健康（有界队列、临时音频清理、审计轮转、单实例健康）。

### 唯一目标
在 P9 稳定后处理全双工体验复杂度，并使设备、播放、监听和 Provider 恢复有清晰的可观测边界。

### 本阶段不做
- 不以“更自然”为理由改变安全规则。
- 不在半句播放后自动换声重播。
- 不因设备恢复自动切回用户当前输入。
- 不把 AEC 失败归因给 Agent 或授权系统。

### 建议文件范围
```text
audio/aec/
audio/noise/
barge_in/
devices/
runtime/recovery/
tests/duplex/
tests/devices/
```

### 实现批次
1. 疑似插话与真人插话两阶段状态；
2. 播放队列停止、旧回复失效、新 utterance 边界；
3. AEC/降噪逐层降级；
4. 输入/输出设备断开、重连与**恢复策略**（注：`device_epoch` 身份与变化检测已在 P2/P6 落地 —— `app/contracts/audio.py:19`、`app/audio/bus.py:25/108`、`app/audio/vad/auto_capture.py:572`；P10 待做的是"在安全空闲点重连/启用"的恢复策略，不是 epoch 本身）；
5. Provider 冷却、固定探测、恢复待启用；
6. 长期运行内存、临时文件、审计轮转和单实例健康。

### 退出条件
- 打断不复用旧 request、旧授权或旧音频；
- AEC/降噪不可用时仍能退回半双工/文字模式；
- 设备恢复只在安全空闲点启用；
- 长时间运行没有无界队列、无界重试和敏感数据泄漏。

---

## P11：Skill、隐私、手册、测试收口与 Windows 发布

### 唯一目标
把可扩展能力和发布能力放在核心安全闭环之后，完成声明式 Skill、数据清理、使用手册、攻击性测试和 Windows 交付。

### 本阶段不做
- 不允许代码型 Skill 直接运行任意脚本。
- 不允许自动更新 Skill、无人值守执行或递归调用。
- 不通过管理员权限解决安装/运行问题。
- 不把打包当作功能缺陷的掩盖手段。

### Skill 最小生命周期
```text
imported_untrusted
→ validated_draft
→ user_approved
→ enabled
→ revoked/disabled
```

Manifest 至少包含：版本、能力描述、允许工具、参数 Schema、风险上限、路径范围、超时、资源上限、递归/调用上限、依赖、兼容范围、撤销条件和测试摘要。每次 Skill 调用都重新扫描、生成计划、预检、授权；Skill 不保存执行权限。

### 测试收口
- 安全拒绝和路径攻击；
- 授权防重放；
- 崩溃/重启和状态不确定；
- ASR/TTS 乱序、迟到、恢复和取消；
- 工具输出提示注入；
- 诊断音频、日志和历史保留清理；
- 长时间运行、设备热插拔、打包安装/卸载。

### 退出条件
- `USER_MANUAL.md`、`QUICK_REFERENCE.md`、故障排查和恢复报告说明完整；
- MVP-1、MVP-2、MVP-3 的验收指标可复现；
- 版本、模型、配置、隐私目录和卸载规则清楚；
- 发布包不包含 API key、个人路径、声纹、参考音频、历史和模型私有缓存。

## 4. 阶段间 handoff 规则

每阶段结束创建一个只包含事实的 handoff，建议放在：

```text
implementation_plan/checkpoints/<阶段编号>-handoff.md
```

handoff 不复制全部设计，只记录：

- 本阶段实际完成的文件和测试；
- 通过/失败的命令及原因；
- 已冻结的接口版本；
- 发现的实现偏差；
- 未解决风险和明确延期；
- 下一阶段必须读取的文件；
- 下一阶段禁止修改的文件范围。

下一次实现会话最小读取顺序：

```text
implementation_plan/implementation-roadmap.md
→ 当前阶段 handoff
→ 上一阶段 handoff
→ 当前阶段列出的共享契约
→ ../progress.md 最后一条相关记录
```

只有发生跨阶段矛盾、重大故障或安全审计问题时，才回读完整 `shared/error-recovery.md`、`shared/safety-boundaries.md` 或 `pending-decisions.md`。

## 5. 强制停止条件

出现以下任一情况，当前阶段立即停止并记录为 `blocked` 或 `system_aborted`，不得用“先继续看看”绕过：

- 无法确认实际执行状态；
- 安全审计无法追加；
- 计划、授权、目标哈希或工具 Schema 不一致；
- Provider 事件顺序无法判定；
- 旧 session/request/audio 事件可能混入新边界；
- 测试需要真实用户音频、真实授权内容或个人隐私才能通过；
- 需要扩大权限、开启管理员身份或接入未批准 Provider；
- 需要跨越本阶段文件范围才能“顺便修好”；
- 上一阶段退出条件尚未满足。

## 6. 当前状态

- P0 已于 2026-09-19 完成，状态为 `complete`。
- P1 已于 2026-09-19 完成，状态为 `complete`；handoff 为 `checkpoints/P1-handoff.md`。
- P2 已于 2026-09-19 完成，状态为 `complete`；handoff 为 `checkpoints/P2-handoff.md`。
- P3 已于 2026-09-19 完成，状态为 `complete`；handoff 为 `checkpoints/P3-handoff.md`。
- P4 已于 2026-09-20 完成，状态为 `complete`；handoff 为 `checkpoints/P4-handoff.md`。
- P5 已于 2026-09-21 完成，状态为 `complete`；阶段验收为 `checkpoints/2026-09-21-P5-integration-acceptance.md`，handoff 为 `checkpoints/P5-handoff.md`。
- P6 已于 2026-09-21 完成，状态为 `complete`；P7-R-01 至 P7-R-09 已于 2026-09-24 至 2026-09-25 完成。P7-R-09 已固定 DeepSeek `deepseek-route/v1` 离线结构化协议；当前仍停在真实模型稳定输出与 provider-specific adapter 的独立实现授权门前，未接入默认运行时、TTS、P8 或电脑副作用链。

## P7-R: Intent routing contract and ConversationManager seam

> Status: **R-01 through R-09 complete (2026-09-24 to 2026-09-25); DeepSeek `deepseek-route/v1` offline protocol is contract-tested; real model output stability and provider-specific adapter remain NOT READY / STOP.**

### Sole objective
Integrate the intent-routing contract into the existing text and voice design so both inputs enter the same `ConversationManager -> IntentRouter -> RouteHandoff` seam.

### Explicitly out of scope
- For the completed R-01 through R-07 scope, `app/` and `tests/` changes are recorded in the batch handoffs; future Provider evaluation must remain separately authorized.
- Do not connect to real Laya, start a provider, or execute computer side effects.
- Do not implement TTS, continuous listening, wake word, voiceprint, AEC, or the P8 combined loop.
- Do not map the current Laya `billing/technical` classifier directly to computer-assistant intents.

### Design deliverables
1. Versioned `RouteHandoff` contract and required trace fields.
2. Two-level `route + intent` routing and allowed combinations.
3. One `ConversationManager` entry boundary for TextShell and ASR accepted final.
4. One `ambiguous` normalizer for low confidence, timeout, unavailable provider, invalid output, and inconsistent routing.
5. Clear ownership of `slots.confirmed` and its handoff to the existing safety chain.
6. Provider-adapter compatibility rules and separate Laya validation.

### P7-R-09 DeepSeek structured protocol result

- P7-R-09 completed on 2026-09-25 under an offline-only scope. `app/routing.py` now exposes a pure standard-library `parse_deepseek_route_response(...)` for strict `deepseek-route/v1` JSON.
- The parser rejects duplicate fields, non-standard numbers, unknown/missing fields, unsupported status, incorrect types, debug/payload leakage, and unsupported protocol versions. The existing provider-neutral `ProviderIntentRouter` remains the authority for `RouteHandoff` combinations, slots, tool scope, confirmation, and the `0.80` confidence threshold.
- Provider-specific offline tests pass: 9/9; standard full discovery is 616/616. No network, secret, SDK, default runtime, authorization, execution, TTS, P8, ASR-device, or computer-side-effect path was added.
- This proves only an isolated structured protocol boundary. It does not prove that the real DeepSeek model reliably emits the protocol and does not authorize a real adapter.
### P7-R-08 restricted real Provider evaluation result

- P7-R-08 completed on 2026-09-25 with a restricted real evaluation of **DeepSeek** and local **Laya**; the evaluation result is split:
  - **Real call chain: READY** — DeepSeekHarness completed one no-side-effect text request; local Laya health/model/predict evaluation completed.
  - **RouteHandoff/provider-specific adapter: NOT READY / STOP** — neither provider has yet demonstrated a stable, contract-valid computer-assistant route handoff.
- **DeepSeek:** `deepseek-official` + `deepseek-v4-flash`, Python `DeepSeekHarness` (`deepseek-harness-sdk==0.1.5rc1`), authorized `DEEPSEEK_API_KEY`, `sdk-minimal` plus `p3-no-tools.patch.yml`; one bounded request returned the redacted result `READY` with `completed`, with no tool call or computer action. The output is still ordinary text/Harness events, not structured `route/intent/handling_mode/confidence/slots/tool_scope`.
- **Laya:** local deployment at `<STUDY_ROOT>\codex\visualizations\2026\09\24\01a0d133-991b-73f3-a9ff-d1fbc9ea0897\laya-local-test`, `laya==0.3.11`, CPU, `127.0.0.1:8000`; `/health`, `/models`, `/predict` and `/predict/batch` were confirmed. Default `billing/technical` output is not a computer-assistant route. Custom `chat/query/task/ambiguous` probes produced misclassification and confidence approximately `0.2989`–`0.5567`, below the project `0.80` threshold; low confidence must fail closed to `ambiguous`.
- No real output was connected to `ProviderIntentRouter`, `ConversationManager`, the default runtime, ToolRegistry, ExecutionLedger, Executor, Verification or Audit. `provider_ready=false` and the offline route remain unchanged.
- Provider-native `billing`/`technical` categories and free-form DeepSeek text must not be mapped directly to computer-assistant intents, authorization, or execution eligibility.

### Next implementation entry
A separate implementation authorization is required. Before any provider-specific adapter work:

```text
DeepSeek structured-output protocol
or Laya stable intent/question configuration and repeatable evidence
-> field-by-field Provider response to RouteHandoff mapping
-> fail-closed rules for invalid/timeout/low-confidence output
-> provider-specific contract tests
-> adapter default-off and provider_ready=false verification
-> only then consider isolated adapter design
```

The implementation must retain the existing `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit` chain and must not enter TTS, P8, ASR device access, or computer side effects.

The implementation must retain the existing `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit` chain.
