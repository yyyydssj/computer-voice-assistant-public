# P7-R 意图路由实现入口续接交接

- 日期：2026-09-24
- 项目：`<PROJECT_ROOT>`
- 阶段：P7-R（意图路由契约与 ConversationManager 接缝）
- 文档性质：实现入口续接文档
- 当前状态：**设计同步已完成；代码实现尚未开始；仍停在 P7/TTS 授权门前**
- 上一正式交接：`implementation_plan/checkpoints/2026-09-24-intent-routing-design-sync-handoff.md`

## 1. 本交接的目的

本文件替代不存在的 Batch 96 路径，作为当前项目后续实现会话的正式接续入口。它把已经完成的意图路由设计同步结果转换为一个可执行、但仍受阶段授权门约束的实现起点，避免继续沿用 P7 Batch 85–95 的只读 routing-rebuild observation 审计线，也避免把 Laya 独立模型接成第二条业务链。

本交接不表示已经开始代码实现，也不表示已经授权连接真实 Laya 或执行电脑动作。

## 2. 已完成事实

### 2.1 设计同步已完成

已经将权威设计契约

`docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md`

同步到现有电脑语音助手设计文档，统一主链：

```text
TextShell / ASR accepted final
→ ConversationManager
→ IntentRouter
→ RouteHandoff
→ AgentAdapter / 只读工具 / ActionPlan / Safety
→ authorization
→ Execution Ledger
→ Executor
→ Verification
→ Audit
```

### 2.2 已冻结的契约决策

- 路由采用两层结构：`route + intent`。
- 高层 `route` 固定为：`chat`、`query`、`task`、`dangerous`、`ambiguous`、`unsupported`。
- `RouteHandoff` 必须携带：`request_id`、`conversation_id`、`turn_id`、`source`、`raw_text`、`created_at`。
- `conversation_id` 复用现有 `Conversation.session_id`，不创建第二套平行会话 ID。
- `source` 为 `text_shell | asr`；现有 `Utterance.input_mode=text|voice` 继续保留。
- 正常分类、低置信度、超时、Provider 不可用、非法输出和路由不一致，都必须返回同一种交接对象；失败统一规范化为 `route=ambiguous`。
- `slots` 只能是候选参数；分类器输出的 `confirmed` 必须为 `false`。
- 只有 `ConversationManager` 在明确用户确认且安全流程允许时，才能产生 `confirmed=true`。
- `IntentRouter` 不得授权、执行、调用电脑工具、创建 `AuthorizationSnapshot` 或写入 `ExecutionLedger`。
- 任何电脑副作用仍必须严格经过：

  ```text
  Plan → preflight → authorization → ledger → executor → verification → audit
  ```

- 当前 Laya `billing/technical` 分类模型不能直接映射成电脑助手意图；Laya 只能作为可替换 Provider，通过契约兼容的适配器接入并单独验证。

## 3. 项目阶段位置

```text
P0-P6：complete
P7-R 意图路由契约设计同步：complete
P7-R 代码实现：not_started
P7 TTS：未开始
P8 语音组合闭环：未开始
```

当前强制停止点仍为：

```text
P7/TTS 授权门前
```

不得把 Batch 95 的“只读观察审计已通过”误认为意图路由代码已经实现，也不得把不存在的 Batch 96 当成有效实现交接。

## 4. 下一实现批次

获得明确实现授权后，只执行下面的第一批，不跨批次：

### Batch P7-R-01：RouteHandoff schema/version 最小实现

唯一目标：把设计文档中的 `RouteHandoff` 变成现有项目可复用、严格校验、版本化且不可被下游误用的契约对象。

建议实现顺序：

1. 先写失败测试，覆盖成功结构、必填追踪字段、时间格式、来源枚举、route/intent 组合和未知字段拒绝。
2. 实现 `RouteHandoff` 及其嵌套追踪、slot、router 状态结构；优先复用现有项目的严格契约/不可变数据模型风格。
3. 实现规范化序列化与严格解析；拒绝缺失字段、未知字段、重复 JSON key、非标准数字、非法枚举和不一致组合。
4. 固化 `ambiguous` 交接的安全边界：不得携带可执行工具范围，`handling_mode=clarify`，所有候选 slot 的 `confirmed=false`。
5. 完成该批次专项测试、既有 Conversation 相关回归、编码和执行面审计。
6. 生成下一份批次 handoff 后停止，不顺手实现 IntentRouter、Laya 适配器、TTS 或 P8。

### 本批次不得实现

- 不实现真实 Laya Provider、网络请求、服务启动或模型下载。
- 不修改 TTS、连续监听、唤醒词、声纹、AEC、实时打断和 P8 组合闭环。
- 不让 `RouteHandoff` 直接调用 Agent、ToolRegistry、电脑工具、授权、账本、Executor 或 Verification。
- 不修改现有 P5/P6 安全执行链的语义。
- 不把 `billing/technical` 作为电脑助手意图模型。
- 不凭空添加路由状态、过期时间、自动重试或后台线程。

## 5. 后续批次顺序

完成并验收 P7-R-01 后，才可按以下顺序继续：

```text
P7-R-01 RouteHandoff schema/version
→ P7-R-02 pure IntentRouter interface
→ P7-R-03 failure normalizer
→ P7-R-04 ConversationManager 接入 TextShell + ASR accepted final
→ P7-R-05 provider adapter 与契约测试
→ P7-R-06 评估契约兼容的 Laya intent model
→ P7/TTS
→ P8 按键触发的半双工语音闭环
```

每一批都必须独立验证、生成 handoff，并在未满足退出条件前停止。

## 6. 实现时必须读取的最小文件集合

```text
implementation_plan/implementation-roadmap.md
implementation_plan/checkpoints/2026-09-24-intent-routing-design-sync-handoff.md
implementation_plan/shared/architecture.md
implementation_plan/shared/action-plan.md
implementation_plan/shared/safety-boundaries.md
implementation_plan/05-agent-integration.md
implementation_plan/08-text-end-to-end.md
implementation_plan/09-voice-end-to-end.md
docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md
```

实现前还必须确认现有 `Conversation`、`ConversationManager`、`AgentRequest`、`Utterance` 及其测试的实际定义，不能根据本文件臆造字段或目录。

## 7. 可观察退出条件

P7-R-01 只有同时满足以下条件才算完成：

- `RouteHandoff` schema/version 已实现并有公开导入入口。
- 必填追踪字段贯穿对象创建、规范序列化和严格解析。
- route/intent/handling_mode/tool_scope/required_confirmation 组合校验覆盖成功与拒绝路径。
- `ambiguous` 规范化对象不可进入可执行工具范围。
- 分类器或外部输入无法伪造 `confirmed=true` 的来源归属。
- 未引入真实网络、Provider、进程、命令、设备或电脑副作用入口。
- 现有 P0-P6 测试和 P7-R 专项测试保持通过。
- 代码与测试文件严格 UTF-8，无替换字符；执行面 AST 审计无越权入口。
- 已生成新的批次 handoff，记录实际文件、测试数量、风险和下一入口。

## 8. 当前明确未完成事项

- `RouteHandoff` 尚未在 `app/` 中实现。
- `IntentRouter` 尚未实现。
- failure normalizer 尚未实现。
- `ConversationManager` 尚未接入路由交接。
- Laya Provider/适配器尚未实现或连接。
- 尚未做真实语音闭环、TTS、连续监听或 P8 集成。

## 9. 安全与交接纪律

- 任何副作用请求仍由现有安全链决定，路由结果不是授权结果。
- 一个 `turn_id` 只能有一个生效路由交接；迟到、重复或旧边界结果不得覆盖当前结果。
- ASR 未接受的 final 不得生成可执行 handoff。
- 路由失败不得让 Agent 自由重新判断；只能进入 `ambiguous` 澄清或安全失败路径。
- Provider 异常正文、原始 payload、API key 和敏感凭证不得进入交接对象、公开会话或审计。
- 未经明确实现授权，不得从设计文档进入 `app/`、`tests/` 代码实现。
- 未经用户明确要求，不执行 Git commit、push、reset 或 `safe.directory` 配置。

## 10. 下一会话复制续接指令

```text
严格接续
<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-R-intent-routing-implementation-entry-handoff.md。

当前事实：P0-P6 complete；P7-R 意图路由契约设计同步 complete；P7-R 代码实现 not_started；仍停在 P7/TTS 授权门前。不要寻找或假设不存在的 Batch 96。获得明确实现授权后，只执行 P7-R-01：先写 RouteHandoff schema/version 的失败测试，再实现严格、版本化、不可变的契约对象；完成专项验证后生成新 handoff 并停止。不得提前实现 IntentRouter、failure normalizer、ConversationManager 接入、真实 Laya、TTS、P8 或电脑副作用。必须保持 Plan → preflight → authorization → ledger → executor → verification → audit 安全链。未经用户明确要求不得 commit、push、reset 或配置 safe.directory。
```

## 11. 本批次停止结论

本交接文档已根据截至 **2026-09-24** 的项目事实建立。它只定义后续实现入口，不宣称实现已经开始或已经完成。下一次应以本文件为入口，在授权明确后从 P7-R-01 开始。