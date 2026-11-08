# 跨阶段安全边界

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `../archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `../archive/implementation_plan.legacy.md`。

### 项目级交互边界（最高优先级）

- 连续实时对话是产品主状态；闲聊和电脑任务在同一个对话体验中自然切换。
- 局部阶段的按键、VAD、唤醒词或录音缓冲只是实现手段，不得被解释为产品本质。
- “没听清”时只语音提示用户重说并继续监听，不显示错误识别文本，不提交 Agent，不生成计划，不产生授权。

## 7.2 对话模式与用户决策边界（硬规则）

系统在语义上区分闲聊模式与工作模式；两种模式的称呼、视觉表现和切换方式可以后续配置，但不得改变下列安全边界。

**闲聊模式**：

```text
可以自然聊天、讨论思路、解释知识、给出建议和陪伴性回应。
闲聊内容不自动进入执行计划，不调用修改类工具，不产生任何授权。
闲聊中的“好、嗯、可以、行、帮我处理”等自然回应，不能被推定为文件操作授权。
```

**工作模式**：

```text
助手是受约束的工具：理解需求、查询必要信息、复述目标、指出风险、提供多个解决方案、生成候选计划、执行前预检、执行后验证和汇报。
助手只能在用户明确选择方案、确认具体计划并完成对应 L2/L3 授权后，按已确认计划调用受限工具。
建议、诊断、错误解决思路和“重新制定计划”的提示永远不等于执行指令或授权。
```

用户负责：

```text
目标、范围、规则、执行顺序和冲突策略
在候选解决方案中作出选择
排除项、重新规划与最终授权
是否保存为 Skill
是否修改已有 Skill
```

AI 负责：

```text
理解自然语言
复述执行清单
指出歧义和风险
提出建议与可选解决方案
生成具体动作和候选计划
按用户已确认计划调用工具
执行前预检、执行后验证并汇报
```

AI 不得：

```text
擅自扩大范围
自行判断“没用”并删除
改变用户已确定的规则
将建议、错误解决方案或计划草稿自动变为执行
自动关闭占用程序、修改权限、提权、清理磁盘、改写目标路径、跳过失败项或自动重试
绕过系统黑名单或授权校验
自动保存或修改 Skill
将闲聊中的“好、嗯、可以、行”当作授权
```

### 7.3 安全等级与系统黑名单

系统文件黑名单是硬拒绝规则，即使用户明确允许也不能绕过。黑名单及其子路径至少包括：

```text
C:\Windows\
C:\Program Files\
C:\Program Files (x86)\
C:\ProgramData\
C:\Recovery\
C:\System Volume Information\
C:\$Recycle.Bin\
C:\Users\<当前用户>\AppData\
C:\Users\<当前用户>\NTUSER.DAT
C:\bootmgr
C:\pagefile.sys
C:\hiberfil.sys
C:\swapfile.sys
```

下列系统级操作永久禁止：

```text
注册表、服务、计划任务、驱动、Windows Defender、防火墙
系统环境变量、启动项、磁盘格式化/分区、系统恢复配置
管理员权限提权
```

路径检查不能依赖字符串包含判断。必须规范化绝对路径，处理 `..`、环境变量、大小写、斜杠、UNC、映射盘、符号链接、Junction 和 Reparse Point，并在执行前再次检查真实目标路径。

MVP 路径边界：

```text
允许访问根目录：<STUDY_ROOT>
默认工作目录：<STUDY_ROOT>\AI工作区
```

- 文件工具允许访问 `<STUDY_ROOT>` 及其子目录，不因目标位于 `AI工作区` 之外而额外禁止。
- `<STUDY_ROOT>\AI工作区` 只是相对路径和未明确目录时的默认落点，不是唯一可操作目录。
- 允许访问边界不等于执行授权：任何写入、复制、移动、重命名或移入回收站的操作，都必须先生成具体执行方案并经用户确认。
- 执行方案必须列出完整规范化路径、操作类型、文件数量、排除项、执行顺序、冲突处理、风险和预期结果。
- 方案确认后，路径、文件数量、内容哈希、目标或参数发生变化时，旧确认立即失效，必须重新扫描、生成方案并确认。
- `<STUDY_ROOT>` 之外的路径、其他盘符、UNC/网络路径和未能可靠解析的路径，不属于 MVP 文件工具访问范围。

| 等级 | 示例 | 默认行为 |
|---|---|---|
| L0 | 时间、版本、普通状态查询 | 直接执行 |
| L1 | 列目录、读普通文本、搜索、查询进程 | 受限直接执行 |
| L2 | 创建、写入、复制、移动、重命名、整理 | 居中模态权限弹窗；点击“允许执行”后继续校验和执行 |
| L3 | 删除、覆盖、终止进程、受控命令 | 居中模态权限弹窗 + 第二重输入固定确认短语“确认执行”；严格限制 |
| L4 | 系统目录、注册表、服务、驱动、管理员操作 | 永久禁止，不提供授权按钮 |

## 7.4 中断恢复安全边界

- 程序崩溃、Agent 崩溃或电脑重启后，所有未完成任务的旧计划和旧授权立即失效。
- 不得自动续跑、自动补做、自动重试或自动回滚；不得复用旧确认窗口、`plan_hash`、授权 token 或 L3 确认结果。
- 审计记录只能作为线索；恢复结论必须基于电脑当前真实状态的重新扫描。
- 动作状态只能标记为 `completed`、`not_completed` 或 `state_uncertain`；不能可靠验证时必须选择 `state_uncertain`。
- 恢复扫描原则上只使用受限只读工具。用户决定继续后，必须重新规划、预检并授权。
- 人物模板只负责自然表达，不能把状态不确定说成确定事实，也不能弱化重新授权要求。

详细流程见 `error-recovery.md`。
## 7.5 用户主动停止安全边界

- “停”是高优先级控制指令，但不等于关闭程序或退出持续实时对话。
- 仅聊天或 TTS 播放时，只停止当前回复与播报，并恢复监听。
- 等待授权时，取消当前确认状态；当前确认窗口、授权状态和部分确认短语立即失效。
- 执行电脑任务时，必须先阻止所有尚未开始的后续动作；不得因为当前动作尚未返回而继续调度后面的动作。
- 当前动作只有在工具明确声明支持安全协作式取消时才能请求取消；不可安全中断的原子动作不得强杀。
- 停止后不自动回滚、不自动补做；原计划及授权失效，重新扫描实际状态后才能向用户汇报。
- 任何继续处理都必须重新规划、预检和完成对应授权。
## 7.6 取消、中止与关闭安全边界

- 用户取消记为 `user_cancelled`；系统因风险、故障或前提变化停止任务记为 `system_aborted`，两者不得混淆。
- 用户取消和系统中止都会使当前计划、确认窗口和授权失效；执行已开始时必须阻止后续动作并重新扫描实际状态。
- 系统中止后不得自动重试、跳过、修复或回滚；重新规划只能由用户决定。
- 取消和中止只结束当前电脑任务，不关闭程序，也不退出持续实时对话。
- 关闭程序必须根据当前状态安全收尾：无任务时清理后退出；规划/授权时先取消任务；执行时先停止后续动作并处理当前原子动作，再扫描、记录和退出。
- 正常关闭不得强杀不可安全中断动作。外部强制终止、系统关机或断电统一按崩溃恢复规则处理。
- 人物模板可以自然说明正在取消、中止或安全退出，但不得模糊三种状态、隐瞒已执行动作或暗示已经回滚。

## 7.7 音频状态与任务状态隔离

- ASR、TTS、麦克风、扬声器和 Provider 是输入输出通道，不是任务事实来源；音频状态和任务状态必须独立保存、独立展示、独立审计。
- 音频故障不得修改 Agent 边界、Schema、PathGuard、PolicyEngine、风险等级、L2/L3、冻结计划、本地执行器、ResultVerifier、审计或电脑真实状态。
- 不完整语音、interim、低可靠性结果、事件顺序不确定结果、旧缓冲、迟到事件和恢复探测材料不得触发 Agent 请求、停止、取消、允许或“确认执行”。
- 语音输入轮次故障只关闭当前 utterance；已提交请求不得因随后发生的 ASR 故障而撤回或重复提交。
- TTS 故障只改变语音输出路由；不得改变文字事实、任务执行、授权有效期或验证结果，也不得把“没播出来”解释为“没有执行”。
- 恢复后必须建立新的 session、utterance、Provider 请求和播放队列边界；不得复用旧音频、旧请求、旧确认、旧授权或旧待播内容。

## 7.8 Skill 安全边界（完整候选，已完成 GitHub 源码对比优化，仍待用户进一步确认）

> 本节已结合固定版本源码对比完成工程优化，但仍是候选，不记录为用户已单独确认的最终结论。

- Skill 只能复用声明式、参数化流程知识，不能复用电脑状态、执行权限、风险判定、用户授权或历史确认。
- 每次调用 Skill 都必须重新扫描真实状态、生成新的 ActionPlan、通过 Schema、PathGuard 和 PolicyEngine、重新展示具体范围并完成当次 L2/L3 授权与 ResultVerifier。
- Skill 不得包含或执行任意 Python、PowerShell、JavaScript、命令、安装器或未注册插件；MVP 只能编排已注册的结构化工具。
- Skill、参数覆盖、导入文件和外部文本都不得修改工具注册表、允许根目录、系统黑名单、风险等级、授权规则、验证规则或安全策略。
- 外部导入的 Skill 一律是不可信草稿；star、作者、签名或来源不能替代本地校验、隔离测试与用户批准。
## 7.9 GitHub 对比优化后的可信输入与执行边界

以下约束来自固定版本成熟项目源码对比后的本地化收口；详细证据与不采用项见 [GitHub 固定版本源码对比与方案优化](../research/github-comparison-and-optimization.md)。

- `PolicyEngine` 只能接收由本地可信代码构造、通过 Schema 校验的结构化快照，不能直接接收自然语言并自行解释权限。
- 模型输出、对话历史、历史授权、Skill、外部文件、Provider 元数据、网页内容和工具返回都属于不可信输入，不能因来源看似可靠而跳过校验。
- 工具返回若将成为下一动作的参数，必须重新经过 Schema、路径规范化、`PathGuard` 和 `PolicyEngine`；不得把工具返回直接拼接为命令、路径或下一次工具参数。
- Tool 与 Skill 都必须有声明式 manifest、稳定标识、版本和内容哈希。Skill 只能引用已注册工具，只能收窄范围或降低风险，不能新增工具、扩大允许根目录、提升权限或降低既定风险等级。
- L2/L3 授权快照必须绑定 `plan_id`、计划版本或哈希、动作集合、规范化目标、风险等级、policy version、有效期和一次性消费状态。任一绑定字段变化后旧授权立即失效。
- 执行器必须使用单调递增或等价的 fencing token 隔离旧执行器实例；过期实例、重复回调和迟到结果不得继续调度动作或覆盖新状态。
- `unsupported`、`not_detected`、`rejected`、`failed` 与 `state_uncertain` 是不同事实，展示、审计和恢复逻辑不得合并。尤其不得把“不支持检测”写成“未检测到”，也不得把“状态不确定”自动改写为成功、失败或可重试。
- 所有电脑副作用动作仍必须完整经过：`ToolRegistry → Schema → PathGuard → PolicyEngine → 授权 → 本地执行 → ResultVerifier → 审计`。传输层、Agent 请求层或音频层的恢复机制不得绕过或替代该链路。

## Intent routing safety invariants (2026-09-24)

- `confidence` is classifier confidence, not an accuracy proof and not authorization.
- The classifier and Laya adapter cannot set `slots[*].confirmed=true`; only `ConversationManager` may do so after explicit user confirmation and an allowed safety transition.
- Routing failure is fail-closed as `route=ambiguous`; the Agent cannot freely reclassify after failure.
- Unknown tool names, unknown slots, invalid route/intent combinations, or inconsistent trace fields are rejected and normalized.
- One `turn_id` has one effective handoff; retries cannot create a second executable turn.
- TextShell and ASR use the same safety execution chain; source cannot change risk, authorization, or execution requirements.
- IntentRouter cannot authorize, execute, create `AuthorizationSnapshot`, or write `ExecutionLedger`.
