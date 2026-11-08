# 电脑语音助手意图路由契约设计

- 日期：2026-09-24
- 设计状态：已确定；等待实现阶段授权
- 适用项目：`<PROJECT_ROOT>`
- 关联能力：Laya 中文分类客户端（仅作为可替换的意图分类提供方）
- 本文性质：接口与行为契约；不包含实现代码，不改变现有执行链

## 1. 目标与边界

本契约解决的问题是：让文字输入和 ASR final 文本在进入现有 Agent、计划、安全和执行链之前，经过同一个、可追踪、可验证的意图路由边界，避免 Laya 和电脑语音助手各自判断、各自执行，最后无法接通。

目标链路：

```text
TextShell / ASR final
        ↓
ConversationManager
        ↓
IntentRouter
        ↓
RouteHandoff
        ↓
AgentAdapter / 只读工具 / ActionPlan / Safety
        ↓
授权 → Execution Ledger → Executor → Verification → Audit
```

硬边界：

1. `IntentRouter` 只负责识别意图和提供路由提示，不负责授权、不负责执行、不直接调用电脑工具。
2. `ConversationManager` 是文字输入与语音输入的统一业务入口；ASR 只提交已经接受的 final 文本，不另建语音专用路由契约。
3. `RouteHandoff` 的安全字段是提示或约束，不是最终安全结论。最终风险等级、工具允许范围、确认要求和执行资格仍由工具 manifest、Policy/Safety、preflight、授权消费和 Execution Ledger 决定。
4. 所有正常结果、低置信度结果、超时、不可用和格式错误都返回同一种 `RouteHandoff`。失败统一表现为 `route=ambiguous`，不得绕过路由器让 Agent 自行猜测。
5. 当前 Laya 客户端里的固定 `billing/technical` 分类模型不能直接充当本契约的电脑助手意图模型；接入时必须使用契约兼容、经过单独验证的意图模型或适配器。

## 2. 设计决策

### 2.1 两层路由

第一层 `route` 决定进入哪条高层处理流程；第二层 `intent` 描述具体请求类型。

```text
route = chat | query | task | dangerous | ambiguous | unsupported
intent = 由 route 约束的具体枚举
```

采用两层结构而不是只使用一个大枚举，原因是高层流程稳定，未来增加具体意图时不会把 `ConversationManager` 的主分流逻辑改成大量字符串判断。

### 2.2 单一交接对象

第一版只定义一个下游交接对象：`RouteHandoff`。它内部嵌套追踪信息、路由结果、参数候选和路由器状态。

- 成功分类：返回正常 `RouteHandoff`。
- 语义不明确：返回 `route=ambiguous` 的 `RouteHandoff`。
- Laya 不可用、超时、输出非法或置信度不足：同样返回 `route=ambiguous` 的 `RouteHandoff`。

这样 `ConversationManager` 不需要维护“正常返回类型”和“分类器异常类型”两套处理分支。

### 2.3 追踪字段属于业务契约

路由器不得自行生成并替代电脑助手的业务标识。`request_id`、`conversation_id`、`turn_id` 由电脑语音助手的会话入口创建或传入，并贯穿后续计划、授权、账本、执行、验证和审计。

在现有项目中：

- `conversation_id` 对应现有 `Conversation.session_id`，第一版不再创建第二套平行会话 ID。
- `request_id` 继续沿用现有 `ConversationManager` / `AgentRequest` 的请求 ID。
- `turn_id` 是每个已接收输入轮次的稳定 ID；同一轮的路由重试、追问关联和审计不得重新生成。
- 现有 `Utterance.input_mode` 继续保留其 `text/voice` 语义；路由契约另提供更精确的 `source=text_shell/asr`。

### 2.4 参数候选与确认分离

分类器可以提取 `slots`，但只能输出候选值。所有分类器输出的 `confirmed` 必须为 `false`。

只有 `ConversationManager` 在收到用户明确确认，并且现有安全/计划流程允许继续时，才可以在后续交接对象中把相应候选标记为 `confirmed=true`。模型判断“表达很明确”不等于用户确认，也不等于授权。

## 3. 契约模型

以下为逻辑结构。实际实现应使用项目现有的严格、版本化契约基础，并拒绝未知字段。

### 3.1 `RouteHandoff`

```json
{
  "schema_version": 1,
  "trace": {
    "request_id": "req-01H...",
    "conversation_id": "chat-session-01H...",
    "turn_id": "turn-01H...",
    "source": "asr",
    "raw_text": "打开微信",
    "created_at": "2026-09-24T12:00:00Z"
  },
  "route": "task",
  "intent": "open_application",
  "handling_mode": "plan_then_execute",
  "confidence": 0.94,
  "slots": {
    "application_name": {
      "value": "微信",
      "source": "utterance",
      "confidence": 0.98,
      "confirmed": false
    }
  },
  "required_confirmation": "defer_to_safety",
  "tool_scope": ["start_whitelisted_application"],
  "router": {
    "provider": "laya",
    "model_id": "assistant-intent-v1",
    "model_version": "2026-09-24",
    "status": "success",
    "failure_code": null
  }
}
```

### 3.2 字段规则

#### `schema_version`

- 固定为整数 `1`。
- 采用现有严格版本化契约规则。
- 未知字段、缺失必填字段或版本不兼容必须使结果进入 `ambiguous`，不得把未经验证的对象交给下游。

#### `trace`

必填字段：

| 字段 | 类型 | 规则 |
|---|---|---|
| `request_id` | string | 现有请求标识；必须可关联 `AgentRequest`、计划和执行记录 |
| `conversation_id` | string | 对应现有 `Conversation.session_id` |
| `turn_id` | string | 当前输入轮次的稳定标识；同轮不可变 |
| `source` | enum | 只能是 `text_shell` 或 `asr` |
| `raw_text` | string | 用户输入的原始文本；不得用模型改写文本替代 |
| `created_at` | UTC string | 必须是以 `Z` 结尾的 UTC 时间 |

`raw_text` 只用于会话关联、审计和后续解析；不得把它当作已授权的执行参数。

#### `route`

允许值：

- `chat`
- `query`
- `task`
- `dangerous`
- `ambiguous`
- `unsupported`

#### `intent`

第一版只覆盖当前项目已有处理能力或已有安全边界能够接住的意图：

| `route` | `intent` | 含义 |
|---|---|---|
| `chat` | `general_chat` | 普通对话，不请求电脑副作用 |
| `query` | `system_status` | 查询系统信息，例如系统状态 |
| `query` | `process_query` | 查询进程信息 |
| `query` | `file_read` | 读取文本文件内容 |
| `query` | `directory_listing` | 列出目录内容 |
| `query` | `file_search` | 搜索文件 |
| `query` | `file_info` | 查询文件信息 |
| `task` | `open_application` | 请求启动白名单应用 |
| `task` | `file_operation` | 创建、写入、复制、移动或重命名等文件任务 |
| `task` | `planning_request` | 明确要求先生成计划或预览，不直接执行 |
| `dangerous` | `delete_file` | 删除或移入回收站等破坏性文件操作 |
| `dangerous` | `overwrite_file` | 明确要求覆盖已有内容或目标 |
| `ambiguous` | `unknown` | 无法确定请求类别 |
| `ambiguous` | `missing_target` | 缺少文件、应用或操作对象 |
| `ambiguous` | `low_confidence` | 置信度不足以安全路由 |
| `ambiguous` | `router_unavailable` | 分类器不可用或请求失败 |
| `ambiguous` | `invalid_router_output` | 分类器响应无法通过契约校验 |
| `unsupported` | `unsupported_request` | 当前项目没有对应能力或工具 |
| `unsupported` | `policy_sensitive` | 请求触碰不可由普通路由处理的策略边界 |
| `unsupported` | `unimplemented_capability` | 例如尚未实现的连续监听、唤醒词或系统设置控制 |

本表不宣称已经实现每个意图的完整端到端处理；它规定的是“路由器可以表达、下游必须明确接住或拒绝”的边界。新增意图前，必须先有对应的工具 manifest、计划处理方式、安全策略和验收测试。

#### `handling_mode`

允许值及其固定含义：

- `chat_reply`：交给现有对话 Agent；不得调用副作用工具。
- `read_only_tool`：只允许进入只读工具选择/调用流程；不得携带写入工具范围。
- `plan_then_execute`：必须先生成结构化计划，再经过 preflight、授权、账本、执行和验证。
- `plan_then_safety`：高风险请求；必须先生成计划并进入 Safety 决策，不能直接执行。
- `clarify`：只生成澄清问题，不得进入工具或执行流程。
- `reject`：说明不支持或拒绝，不得进入执行流程。

#### `confidence`

- 数值范围为 `0.0..1.0`。
- 这是路由器对本次分类结果的置信度，不是准确率、成功率或授权结论。
- 第一版采用 `0.80` 作为最低可路由阈值；低于阈值必须返回 `ambiguous/low_confidence`。
- 即使超过阈值，也不能绕过安全链；高风险请求仍必须经过 `plan_then_safety`。

#### `slots`

`slots` 是键到 `SlotCandidate` 的映射。每个候选包含：

| 字段 | 规则 |
|---|---|
| `value` | 结构化候选值；不得被视为已授权参数 |
| `source` | `utterance`（原话直接取得）、`normalization`（安全归一化）或 `classifier`（模型推断） |
| `confidence` | `0.0..1.0` 的字段级置信度 |
| `confirmed` | 第一版分类器输出必须为 `false`；仅 `ConversationManager` 可设置为 `true` |

第一版允许的常见 slot 名称：

- `application_name`
- `source_path`
- `destination_path`
- `target_path`
- `directory_path`
- `file_pattern`
- `operation`
- `content`

下游必须再次依据工具 manifest、PathGuard、白名单、参数 schema 和用户确认验证 slot；不存在的 slot 名称不得静默忽略，必须使交接失败并转为 `ambiguous/invalid_router_output`。

#### `required_confirmation`

该字段只表达路由器建议，不代表授权。允许值：

- `none`：聊天或只读查询通常不提出确认建议；下游仍可依据安全策略要求澄清。
- `defer_to_safety`：存在副作用或风险，必须交给现有 Safety/Policy 判定。
- `must_clarify`：参数不足或语义不清，必须先追问。

分类器不得输出“已确认”“已授权”“可直接执行”等状态。

#### `tool_scope`

- 表示分类器建议的工具范围，不是工具调用清单。
- 只能使用 `app/tools/catalog.py` 已注册的 manifest 名称。
- 当前可映射的只读工具包括：`list_directory`、`read_text_file`、`search_files`、`get_file_info`、`get_system_info`、`get_processes`。
- 当前可映射的副作用工具包括：`create_text_file`、`write_text_file`、`copy_file`、`move_file`、`rename_file`、`move_to_recycle_bin`、`start_whitelisted_application`。
- `ConversationManager` 或下游路由校验器发现未知工具、路由与工具风险不一致或只读路由携带副作用工具时，必须丢弃原结果并生成 `ambiguous/invalid_router_output`。
- 工具 manifest 的风险等级和参数约束优先于该字段。

#### `router`

用于诊断、审计和回放，但不保存未经脱敏的供应商异常正文：

| 字段 | 允许值/规则 |
|---|---|
| `provider` | 例如 `laya`；是适配器标识，不是业务授权主体 |
| `model_id` | 实际使用的意图模型标识；禁止伪装成当前 billing/technical 模型 |
| `model_version` | 模型或适配器版本 |
| `status` | `success`、`unavailable`、`timeout`、`transport_error`、`invalid_response`、`low_confidence` |
| `failure_code` | 枚举化诊断码；成功时为 `null` |

路由器内部响应、API 密钥、异常正文和供应商原始 payload 不进入 `RouteHandoff`。

## 4. 路由与现有模块的交接矩阵

| 路由结果 | `ConversationManager` 动作 | 下游边界 | 禁止事项 |
|---|---|---|---|
| `chat/general_chat` | 调用现有聊天提交路径 | `AgentAdapter` | 不得启用副作用工具 |
| `query/*` | 交给只读工具选择/查询路径 | ToolRegistry + 只读工具 | 不得进入写入工具或执行账本 |
| `task/open_application` | 创建计划请求 | 白名单 manifest + preflight + 授权链 | 不得因分类结果直接启动应用 |
| `task/file_operation` | 创建文件操作计划 | PathGuard + manifest + preflight + 授权链 | 不得直接改文件 |
| `task/planning_request` | 只生成计划或预览 | ActionPlan/ResultDocument | 不得自动消费授权 |
| `dangerous/*` | 创建计划并强制进入 Safety | Policy/Safety + L3/L2 现有链 | 不得降低风险级别或跳过确认 |
| `ambiguous/*` | 生成澄清问题或安全失败提示 | ConversationManager | 不得让 Agent 重新自由猜测意图 |
| `unsupported/*` | 返回不支持/拒绝说明 | 会话层 | 不得尝试相近工具绕过不支持结论 |

## 5. `ConversationManager` 集成生命周期

1. TextShell 或 ASR final 把文本提交给 `ConversationManager`，同时提供 `source`。
2. `ConversationManager` 校验文本非空、创建/取得当前会话，生成本轮稳定的 `turn_id` 和 `request_id`，形成 `trace`。
3. `IntentRouter` 接收包含 `raw_text` 和 `trace` 的输入，只返回一个 `RouteHandoff`。
4. `ConversationManager` 校验 schema、trace 一致性、route/intent 组合、slot 结构、工具名和处理模式。
5. 校验失败、路由器失败、超时或低置信度时，统一规范化为 `ambiguous` handoff；失败码写入 `router.failure_code`，异常正文不向会话或审计泄露。
6. `ConversationManager` 根据交接矩阵选择聊天、只读、计划、安全、澄清或拒绝路径。
7. 任何涉及电脑副作用的路径都继续执行现有：

   ```text
   Plan → preflight → authorization → ledger → executor → verification → audit
   ```

8. `turn_id`、`request_id`、`conversation_id` 在整个请求生命周期保持不变；ASR 失败或未接受的 final 不得生成可执行 handoff。
9. 路由结果应在现有审计关联中可检索，但必须遵守现有脱敏规则；不保存模型原始输出或敏感凭证。

## 6. 一致性与安全不变量

以下规则是契约的强制不变量：

1. `route=chat` 时 `intent=general_chat`，`handling_mode=chat_reply`，`tool_scope` 必须为空。
2. `route=query` 时只能使用只读工具，`handling_mode=read_only_tool`；不能携带副作用工具。
3. `route=task` 时 `handling_mode` 必须是 `plan_then_execute`；副作用是否需要 L2/L3 由 manifest 和 Safety 决定。
4. `route=dangerous` 时 `handling_mode=plan_then_safety` 且 `required_confirmation=defer_to_safety`。
5. `route=ambiguous` 时 `handling_mode=clarify`，不得携带可执行工具范围；`confirmed` 必须全部为 `false`。
6. `route=unsupported` 时 `handling_mode=reject`，不得进入 Agent 工具调用或执行链。
7. 所有 `slots[*].confirmed=true` 必须由 `ConversationManager` 的明确用户确认流程产生，不能来自分类器或 Laya。
8. 分类器不能创建 `AuthorizationSnapshot`，不能写入 `ExecutionLedger`，不能调用 Executor。
9. 任何风险结论只能由现有 Safety/manifest/preflight 覆盖或升级，不能被路由器降低。
10. 同一个 `turn_id` 只能有一个生效的路由交接；迟到、重复或旧边界结果不得覆盖当前结果。

## 7. 失败与降级策略

| 故障 | 对外结果 | 是否允许继续 |
|---|---|---|
| Laya 不可用 | `ambiguous/router_unavailable` | 只允许追问或文字说明 |
| 请求超时 | `ambiguous/router_unavailable`，状态 `timeout` | 不允许猜测执行 |
| 响应不是合法 JSON/契约 | `ambiguous/invalid_router_output` | 不允许进入 Agent/工具 |
| route/intent 不匹配 | `ambiguous/invalid_router_output` | 不允许进入 Agent/工具 |
| 置信度低于 0.80 | `ambiguous/low_confidence` | 只允许澄清 |
| slot 缺失关键目标 | `ambiguous/missing_target` | 只允许追问 |
| 模型给出未知工具 | `ambiguous/invalid_router_output` | 不允许调用未知工具 |
| ASR 只产生 interim 或 final 未被接受 | 不生成可执行 handoff | 保持现有文字输入降级 |

不得采用“路由器失败后交给 Agent 自行判断”的降级方式；这会产生两套不一致的意图系统。

## 8. 审计与隐私

最少必须能通过以下键关联路由结果：

```text
request_id + conversation_id + turn_id + source + created_at
```

审计记录可以保存：

- 路由结果、意图、处理模式和置信度；
- slot 名称、来源、置信度和确认状态；
- provider、model_id、model_version、status、failure_code；
- 后续计划、授权、账本和执行结果的关联 ID。

审计记录不得保存：

- API 密钥或认证头；
- 未脱敏的供应商异常正文；
- 未经必要性判断的完整模型原始 payload；
- 把分类概率写成“准确率”的宣传性字段。

`raw_text` 是否持久化遵循现有会话/审计脱敏与保留策略；契约要求它在本轮交接中存在，不代表必须永久保存。

## 9. 非目标

第一版契约不包含：

- TTS、连续监听、唤醒词、声纹、AEC、实时打断；
- 真实云端 ASR 或新的语音会话状态机；
- 系统设置修改、任意命令行、未注册工具；
- 把 Laya 桌面客户端复制进电脑语音助手；
- 用分类器替代 Agent、ActionPlan、Safety、授权、Ledger、Executor 或 Verification；
- 通过规则或模型概率直接授予执行权限。

## 10. 实现前验收标准

在进入实现阶段前，必须能为契约单独准备测试覆盖以下内容：

1. 合法 `RouteHandoff` 可序列化、反序列化，严格拒绝未知字段和错误版本。
2. `trace` 六个必填字段完整，并能与现有会话/请求对象关联。
3. `route/intent/handling_mode/tool_scope` 的合法组合和非法组合均有测试。
4. `slots` 的来源、字段级置信度和 `confirmed` 单向边界均有测试。
5. 分类器失败、超时、非法响应、低置信度均规范化为 `ambiguous`。
6. 重复、迟到或旧 `turn_id` 的结果不能覆盖当前轮次。
7. query 路由不能携带副作用工具；dangerous 路由不能降级成普通 task。
8. 路由结果能继续关联到现有 Plan、Safety、AuthorizationSnapshot、ExecutionLedger、Verification 和 Audit。
9. ASR final 与 TextShell 对同一段文本使用同一契约，只在 `source` 上区分来源。
10. 执行面审计证明：路由契约测试不会调用真实电脑副作用、命令、网络或后台服务。

## 11. 实现顺序（待后续授权）

1. 先在 `app/contracts/` 增加严格版本化的路由契约模型和枚举校验。
2. 再增加纯函数式的 `IntentRouter` 接口与失败规范化器，不连接真实 Laya。
3. 在 `ConversationManager` 增加统一输入入口的路由交接，但保持现有聊天行为和安全执行链不变。
4. 增加 TextShell/ASR 两个入口的 trace 传递测试。
5. 用 stub/provider contract tests 验证 Laya 适配器的 schema、超时和错误映射。
6. 最后才评估接入经过验证的 Laya 意图模型；当前 `billing/technical` 模型不得直接上线为助手路由模型。

在用户明确授权实现前，本文不要求、也不执行任何代码改动。