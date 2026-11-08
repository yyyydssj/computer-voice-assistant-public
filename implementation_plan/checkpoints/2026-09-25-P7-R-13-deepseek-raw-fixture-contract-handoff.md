# P7-R-13 DeepSeek 脱敏原始输出 fixture 契约交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**implementation complete；真实 Provider-specific adapter 仍 NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-12-deepseek-raw-fixture-contract-handoff.md`

## 本批唯一目标

在不联网、不读取密钥、不启动 DeepSeek SDK/Laya、不接入默认运行时、不进入 TTS/P8/ASR 设备或电脑副作用链的前提下，继续扩展脱敏、离线的 DeepSeek 原始输出边界契约；不改变既有解析实现，不宣称 fixture 来自真实模型。

## 实际变更

- `tests/routing/test_deepseek_protocol.py`
  - 新增 JSON 字段顺序无关测试，确认对象字段排列不影响合法 fixture 解析；
  - 新增 `low_confidence` 置信度 `0` 与 `1` 边界测试，确认协议边界值可接受，后续路由语义仍由既有 Router 决定；
  - 新增 `tool_scope` 列表隔离测试，确认每次原始解析返回新列表，调用方变更不会污染后续解析；
  - 新增 UTF-8 BOM、BOM 后空白和非法前缀拒绝测试，确认统一返回固定错误且不回显 fixture 内容。
- `progress.md`、`task_plan.md`
  - 记录本批范围、验证结果、停止点和下一批入口。
- 新增本交接文档，供新对话直接恢复上下文。
- 未修改 `app/routing.py`，未修改默认运行时或任何真实 Provider 接线。

## 验证事实

- DeepSeek 协议专项：**27/27** 通过。
- 本批新增测试：**4/4** 通过。
- Routing 专项：**59/59** 通过。
- 标准全量 discovery：**634/634** 通过。
- `compileall app tests`：通过。
- `app/` 与 `tests/` Python 文件严格 UTF-8 解码：150 个文件通过，未发现 `U+FFFD`。
- `app/routing.py` 与 DeepSeek 测试执行面 AST 审计：通过，无网络、socket、HTTP client、subprocess、进程/线程启动或电脑副作用调用。

## 执行面审计与安全边界

- 未读取 `.env`、`DEEPSEEK_API_KEY` 或任何凭据。
- 未发起网络请求，未启动真实 Provider/Laya 服务，未调用 SDK。
- 未修改默认 `app.main.run()`；`provider_ready=false` 保持不变。
- 未接入 `ConversationManager`、ToolRegistry、ExecutionLedger、Executor、Verification、Audit、TTS、P8 或 ASR 设备。
- 未执行电脑副作用；fixture 只能证明离线契约边界，不能产生授权或执行资格。
- 仍必须遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。

## 当前结论与下一入口

本批继续证明：脱敏的 DeepSeek 原始输出边界对字段顺序、低置信度边界值、嵌套/列表对象隔离以及 BOM/非法前缀具有明确契约；错误边界保持固定且不泄露 fixture 内容。该证据不证明真实模型稳定输出，也不授权实现或启用真实 Provider-specific adapter。

### 下一批次建议

新对话开始时先读取本文件，然后必须重新确认批次授权。下一批优先二选一：

1. **继续离线 fixture/契约扩展**：只修改脱敏测试和文档，继续覆盖 JSON 解析边界、错误隔离和 provider-neutral 接缝；仍不得联网、读密钥、启动 Provider/Laya 或修改默认运行时。
2. **设计默认关闭的 DeepSeek provider-specific adapter**：仅做离线设计/接口与契约测试，先固定超时、重试、成本、出站域名、日志脱敏、payload 隔离、删除/禁用回滚和 `provider_ready=false` 验证；未经单独授权不得联网、读密钥、调用真实 Provider 或接入默认运行时。

无论选择哪一项，都不得把 fixture 证据当作真实模型稳定性证明，不得将 Provider 输出直接视为授权、工具范围或执行资格，不得绕过 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。