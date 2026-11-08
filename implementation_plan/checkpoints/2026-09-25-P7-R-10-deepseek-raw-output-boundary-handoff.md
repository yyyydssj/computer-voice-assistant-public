# P7-R-10 DeepSeek 脱敏原始输出边界交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**complete；真实 Provider-specific adapter 仍 NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-09-deepseek-structured-protocol-handoff.md`

## 本批唯一目标

在不联网、不读取密钥、不启动 DeepSeek SDK/Laya、不接入默认运行时、不进入 TTS/P8/ASR 设备或电脑副作用链的前提下，为脱敏的离线 DeepSeek 原始模型输出增加严格 JSON 边界：只接受单个裸 JSON 文档，拒绝 Markdown、前后说明、多文档和非法输入，并将成功结果交给既有 `deepseek-route/v1` 与 provider-neutral `ProviderIntentRouter` 契约。

## 实际变更

- `app/routing.py`
  - 新增纯标准库 `parse_deepseek_raw_route_response(payload)`；
  - 非空字符串才可进入解析；
  - 复用既有严格 `parse_deepseek_route_response`，因此拒绝 Markdown code fence、前置/后置自然语言、多段 JSON、未知字段、重复字段、非标准数字、错误 schema 和非法类型；
  - 对外只返回去除 `schema_version` 后的全新 mapping；解析失败统一为不含原文的边界错误；
  - 不保存原始文本，不引入网络、SDK、日志、授权、执行或 Provider 运行时依赖。
- `tests/routing/test_deepseek_protocol.py`
  - 新增 5 项离线边界测试，覆盖裸 JSON、允许外围空白、Markdown/前后包裹拒绝、多文档/空值/非字符串拒绝、错误文本不泄露以及进入既有 `ProviderIntentRouter` 后的原文隔离。

## 验证事实

- DeepSeek 协议专项：**14/14** 通过。
- Provider/Conversation/Integration 相关回归：**59/59** 通过。
- 标准全量 discovery：**621/621** 通过。
- `compileall app tests`：通过。
- `app/` 与 `tests/` Python 文件严格 UTF-8 解码：150 个文件通过，未发现 `U+FFFD`。
- 既有 BOM 文件 3 个仍存在：`tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`、`tests/tts/test_capabilities.py`；本批未改动。
- `app/routing.py` import/调用面审计：无 forbidden import，无 `connect/run/Popen/system/start` 调用；源文本中的 `requests` 仅来自测试审计用的禁用词列表，不是 import 或执行依赖。

## 执行面审计与安全边界

- 未读取 `.env`、`DEEPSEEK_API_KEY` 或任何凭据。
- 未发起网络请求，未启动真实 Provider/Laya 服务，未调用 SDK。
- 未修改默认 `app.main.run()`；`provider_ready=false` 保持不变。
- 未接入 `ConversationManager`、ToolRegistry、ExecutionLedger、Executor、Verification、Audit、TTS、P8 或 ASR 设备。
- 未执行电脑副作用；Provider 输出不能产生授权或执行资格。
- 仍必须遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。

## 当前结论与下一入口

本批证明：脱敏的 DeepSeek 原始输出只能以严格、单文档、裸 JSON 形式进入已冻结的 `deepseek-route/v1` 离线协议；包裹文本和非法响应会 fail closed，且原始文本不会进入路由交接。该证据不证明真实模型稳定输出，也不授权实现或启用真实 Provider-specific adapter。

下一批若要继续，必须另行授权，优先选择：

1. 在默认关闭的隔离边界设计 DeepSeek provider-specific adapter，并补齐超时、重试、成本、出站域名、日志脱敏和删除/禁用回滚测试；或
2. 继续仅做离线 fixture/契约扩展，验证更多脱敏真实响应形态，但不得接入默认运行时。

在新授权前不得联网、读密钥、启动 DeepSeek、接入默认运行时或进入电脑副作用链。