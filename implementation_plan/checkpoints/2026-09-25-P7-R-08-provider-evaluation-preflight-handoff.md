# P7-R-08 真实 Provider 评估前置审计与独立授权门交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**complete（真实 Provider 评估已执行；provider-specific adapter 仍 NOT READY / STOP）**
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-07-provider-compatibility-seam-handoff.md`

## 本批唯一目标

在已获得本轮 Provider、模型、凭据读取和无副作用请求授权的范围内，完成 DeepSeek 与本地 Laya 的真实调用评估，并判断其输出是否具备进入当前 `RouteHandoff` 契约或实现 provider-specific adapter 的条件。本批不接入默认运行时，不实现 adapter，不进入电脑副作用链。

## 评估结论

本轮结论分为两层：

1. **真实调用条件：READY（已完成受限评估）**
   - DeepSeek 的 SDK、Provider、模型和授权凭据读取路径已确认，并完成 1 次无副作用文本请求；
   - Laya 的本地部署、模型、HTTP schema 和真实本地响应已确认，并完成一组无副作用分类请求。
2. **RouteHandoff / provider-specific adapter：NOT READY / STOP**
   - DeepSeek 返回的是普通文本与 Harness 事件，不是当前契约所需的结构化路由交接；
   - Laya 虽支持自定义 `questions`，但当前真实结果在 `chat/query/task/ambiguous` 上不稳定，且置信度低于项目阈值；
   - 因此不得把任一 Provider 的原始输出直接映射为电脑助手意图、授权或执行资格。

截至 **2026-09-25**，本批只证明“受限真实调用链可用”，没有证明任何真实 Provider 已与当前 `RouteHandoff` schema 兼容，也没有授权实现真实 adapter。

## 输入与授权矩阵

| 项目 | 本轮状态 | 结论/动作 |
|---|---|---|
| DeepSeek Provider/model | `deepseek-official` / `deepseek-v4-flash` | SDK 与模型初始化成功 |
| DeepSeek SDK | `DeepSeekHarness`，`deepseek-harness-sdk==0.1.5rc1` | 1 次真实纯文本请求成功 |
| DeepSeek 凭据 | 已授权读取 `DEEPSEEK_API_KEY`；仅作运行时输入 | 不记录密钥、Authorization header 或完整凭据 |
| Laya 部署 | 本地 `laya==0.3.11`，CPU，`127.0.0.1:8000` | `/health`、`/models`、`/predict` 可用 |
| Laya schema | `state + questions + model`；返回 `answers/usage/routing` | schema 已确认，但语义兼容性未通过 |
| 网络范围 | DeepSeek 仅限本次单次 SDK 请求；Laya 仅访问本机回环地址 | 未接入默认运行时或其他出站链路 |
| 请求/重试上限 | DeepSeek 1 次、无自动重试、60 秒请求超时；Laya 为受限本地评估 | 不启动持续客户端、不批量接入产品链 |
| 日志与诊断 | 仅保留脱敏摘要和枚举化结果 | 不记录密钥、原始 payload、异常正文或完整敏感输入 |
| payload 隔离 | 保持 | Provider 输入输出不进入 `RouteHandoff` 以外的执行链 |
| 回滚 | 保持 | 未修改产品代码，`provider_ready=false` 和既有离线路径不变 |
| provider-specific adapter | 未授权且未实现 | 继续 NOT READY / STOP |

## 离线兼容性审计矩阵

当前 `ProviderIntentRouter` 只接受 provider-neutral 的严格交接字段：`status`、`route`、`intent`、`handling_mode`、`confidence`、候选 `slots`、`required_confirmation`、`tool_scope`，并由现有 `RouteHandoff` 再次校验 trace、路由组合、置信度、工具范围和未确认 slot。

| 真实响应来源字段 | 当前可映射目标 | 是否可直接当作电脑助手意图 | 规则 |
|---|---|---:|---|
| route | `RouteHandoff.route` | 否 | 必须是当前允许的 `chat/query/task/ambiguous` 之一 |
| intent | `RouteHandoff.intent` | 否 | 必须与 route 组合一致；需有真实样例证据 |
| confidence | `RouteHandoff.confidence` | 否 | 低于 0.80 只能进入 `ambiguous/low_confidence` |
| slots/entities/arguments | 候选 `slots` | 否 | 只能是不确认候选；不得生成授权或执行参数 |
| tool/function/action name | `tool_scope` 候选 | 否 | 必须通过本地 manifest/policy 再验证，不能由 Provider 授权 |
| confirmation/approval | `required_confirmation` 的提示 | 否 | Provider 不能替代本地确认、授权消费或安全链 |
| billing | 无直接映射 | **否** | Provider 原生计费分类不是电脑助手意图 |
| technical/error/status | router failure 状态 | **否** | 只能映射为枚举化失败；不得透传原文或 payload |
| raw content/debug/metadata | 无 | **否** | 必须隔离、脱敏或丢弃 |

本矩阵只是离线规则，不是对任何真实 Provider schema 的兼容性证明。真实样例到达后，必须逐字段记录：来源字段、类型、是否必需、是否允许未知字段、脱敏规则、目标字段、拒绝条件和测试 fixture；不能把 `billing`、`technical` 或其他 Provider 原生分类直接当作 `route + intent`。

本矩阵只是边界规则；本轮真实调用结果如下。

## 本轮真实评估记录

### DeepSeek

- Provider：`deepseek-official`；模型：`deepseek-v4-flash`。
- 通道：`DeepSeekHarness` Python SDK，版本 `deepseek-harness-sdk==0.1.5rc1`。
- 安全配置：`sdk-minimal` + `p3-no-tools.patch.yml`；禁用工具、subprocess、PTY、shell、自动重试、持久会话和会话日志。
- 请求：仅 1 次无副作用文本请求，提示为要求返回 `READY`，请求超时 60 秒。
- 脱敏结果：`final_response='READY'`，`finish_reason='completed'`，事件数量 11；未发生工具调用或电脑操作。
- 兼容性结论：真实调用链可用；输出仍是普通文本/Harness 事件，不足以直接产生 `route/intent/handling_mode/confidence/slots/tool_scope`。

### Laya

- 部署：`<STUDY_ROOT>\codex\visualizations\2026\09\24\01a0d133-991b-73f3-a9ff-d1fbc9ea0897\laya-local-test`；`laya==0.3.11`；默认模型 `multilingual`；CPU。
- 端点：仅访问 `http://127.0.0.1:8000` 的 `/health`、`/models`、`/predict`/`/predict/batch`。
- 默认问题配置曾返回 `billing/technical`；该分类不能直接映射电脑助手 `chat/query/task/ambiguous`。
- 使用自定义 `route` choice 问题进行多条无副作用样例评估：聊天、信息查询、明确电脑操作和含糊请求均出现误分类或低置信度；观测置信度约 `0.2989` 至 `0.5567`，均低于项目 `0.80` 阈值。
- 兼容性结论：本地部署与真实 HTTP schema 已确认；当前模型/问题配置不足以证明稳定的电脑助手路由兼容性，低置信度必须 fail-closed 为 `ambiguous`。

本轮没有修改 `app/` 或 `tests/`，没有把任何真实输出接入 `ProviderIntentRouter`、`ConversationManager`、默认运行时、ToolRegistry、ExecutionLedger、Executor、Verification 或 Audit。

## 后续 adapter 实现前的强制门槛

本轮真实调用评估已经完成；后续若要解除 adapter 的 `NOT READY / STOP`，必须另行获得实现授权，并先完成以下检查：

- Provider、模型/版本、用途和测试输入已书面固定；
- 样例已脱敏，且不含密钥、个人凭据、完整敏感对话或未授权文件内容；
- 密钥来源和最小读取范围已明确；未经明确授权不得读取环境变量或 `.env`；
- 允许域名、端点、方法、区域和出站网络范围已固定；
- 单次/总请求数、费用上限、单请求超时、总耗时和重试策略已固定；
- 原始 Provider payload 只存在于隔离的评估边界，不进入默认运行时、ConversationManager、执行器、账本或审计持久化；
- 日志只保留枚举化状态、请求关联 ID 和脱敏摘要；异常正文、Authorization header、完整 payload 和响应中的秘密不得记录；
- 任何超时、不可用、传输错误、非法响应和低置信度都必须 fail closed 为安全的 `ambiguous` 交接；
- 真实 adapter 默认关闭，支持立即禁用和删除，不改变 `provider_ready=false`，不改变既有离线路径；
- 真实评估和 adapter 实现均不得绕过 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit` 链。

## 执行面审计与验证

- 本批仅更新交接文档和 `progress.md`，没有修改 `app/` 或 `tests/`。
- 已按用户授权读取 `DEEPSEEK_API_KEY` 作为 DeepSeekHarness 运行时输入；未记录密钥、Authorization header 或完整凭据。
- DeepSeek 仅执行 1 次受限真实 SDK 文本请求；Laya 仅访问本机 `127.0.0.1:8000` 做健康检查和无副作用分类评估。
- 未接入默认运行时、ConversationManager、ToolRegistry、ExecutionLedger、Executor、Verification 或 Audit；未执行电脑副作用、TTS、P8、ASR 设备操作。
- DeepSeek 请求使用无工具补丁、无自动重试和有界超时；Laya 评估使用本地服务，不启动新的产品执行链。
- 既有离线 Provider routing、Conversation 和 integration 回归仍为：32/32、19/19、12/12；标准全量 discovery：607/607。本轮真实结果不替代后续 adapter 的契约测试。

## 下一批入口

下一批不得直接进入真实 adapter 实现。若要解除当前 `NOT READY / STOP`，必须另行授权并先完成：

1. 为 DeepSeek 固定结构化输出协议、字段类型、拒绝条件和低置信度规则；或为 Laya 固定训练/问题配置、样例集和可重复的 `chat/query/task/ambiguous` 兼容性证据；
2. 逐字段记录 Provider 响应到 `RouteHandoff` 的映射、脱敏规则和 fail-closed 条件；
3. 为 adapter 增加 provider-specific 契约测试，并证明失败、超时、非法响应和低置信度不会进入执行链；
4. 保持 adapter 默认关闭，不改变 `provider_ready=false`，不接入默认运行时和电脑副作用链。

在上述条件确认前：

- 不实现真实 Provider adapter；
- 不把真实输出接入 `ProviderIntentRouter` 或默认运行时；
- 不进入 TTS、P8、ASR 设备或电脑副作用链；
- 继续遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。
