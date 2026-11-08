# P7-R-09 DeepSeek 结构化输出协议与离线契约交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**complete；真实 Provider adapter 仍 NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-08-provider-evaluation-preflight-handoff.md`

## 本批唯一目标

在不联网、不读取密钥、不启动 DeepSeek SDK、不连接 Laya、不接入默认运行时、不进入 TTS/P8/ASR 设备或电脑副作用链的前提下，为 DeepSeek 固定 `deepseek-route/v1` 结构化输出协议，并以离线 fixture/契约测试证明该协议可安全进入现有 provider-neutral `ProviderIntentRouter`。

## 实际变更

- `app/routing.py`
  - 新增纯标准库 `parse_deepseek_route_response(payload)`；
  - 固定 `schema_version=deepseek-route/v1`；
  - 拒绝重复 JSON 字段、非标准数字、未知字段、缺失字段、未知状态和错误类型；
  - 成功响应只剥离协议版本字段后交给既有 `ProviderIntentRouter`，继续由 `RouteHandoff` 校验 route/intent/handling_mode、slot、tool_scope、确认和置信度；
  - 失败响应只允许 `timeout/unavailable/transport_error/invalid_response/low_confidence`，并通过现有失败规范化边界；
  - 不保存原始 JSON、异常正文、授权、密钥或 Provider 元数据；
  - 新解析器无网络、进程、线程、ToolRegistry、ExecutionLedger、Executor、Verification 或 Audit 依赖。
- `tests/routing/test_deepseek_protocol.py`
  - 新增 9 项 provider-specific 离线测试：chat/query/task 成功、schema/version、未知/重复字段、严格类型、低置信度、失败状态、payload/debug 隔离、路由/工具/confirmed slot 拒绝、执行面审计。

## 协议摘要

### 成功

必须是严格 JSON 对象，字段为：

- `schema_version`: 精确 `deepseek-route/v1`
- `status`: `success`
- `route`
- `intent`
- `handling_mode`
- `confidence`
- `slots`
- `required_confirmation`
- `tool_scope`

协议版本字段只在隔离解析边界消费；其余字段仍需通过现有 provider-neutral 契约。`confidence < 0.80` 自动 fail-closed 为 `ambiguous/low_confidence`，并清空 slots/tool_scope。

### 失败

- `timeout`
- `unavailable`
- `transport_error`
- `invalid_response`
- `low_confidence`（必须携带有限 `0..1` 的 `confidence`）

失败响应不得携带 debug、原始 payload、slots 或 tool_scope；失败最终固定为安全的 `ambiguous` 交接。

## 验证事实

- DeepSeek 协议专项：**9/9** 通过。
- Routing 专项（现有 + 新协议）：**41/41** 通过。
- Provider/Conversation/Integration 相关回归：**27/27** 通过。
- 标准全量 discovery：**616/616** 通过。
- `compileall`：`app` 与 `tests` 通过。
- `app`/`tests` Python 文件严格 UTF-8 解码通过，未发现 `U+FFFD`。
- import/执行面审计通过；未引入网络、socket、HTTP client、subprocess、线程/进程或执行链依赖。

## 执行面审计与安全边界

- 未读取 `.env`、`DEEPSEEK_API_KEY` 或任何凭据。
- 未发起网络请求，未启动真实 Provider/Laya 服务，未调用 SDK。
- 未修改默认 `app.main.run()`；`provider_ready=false` 保持不变。
- 未接入 `ConversationManager`、ToolRegistry、ExecutionLedger、Executor、Verification、Audit、TTS、P8 或 ASR 设备。
- 真实 adapter 尚未实现；当前代码只是隔离的结构化解析与 provider-neutral 契约测试。
- 仍必须遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`，Provider 输出不能产生授权或执行资格。

## 当前结论与下一入口

本批证明：DeepSeek 可以在离线边界拥有严格、可拒绝、可 fail-closed 的 `deepseek-route/v1` 输出协议；不证明真实 DeepSeek 模型已经稳定地产生该协议，也不授权实现真实 Provider adapter。

下一批若要继续，必须另行授权，且应优先选择以下受限范围：

1. 只用脱敏的离线原始模型输出 fixture，验证 JSON 提取/包裹文本拒绝策略；或
2. 单独授权实现默认关闭的 DeepSeek provider-specific adapter，并先补齐超时、重试、成本、出站域名、日志脱敏和删除/禁用回滚测试。

在新授权前不得联网、读密钥、启动 DeepSeek、接入默认运行时或进入电脑副作用链。