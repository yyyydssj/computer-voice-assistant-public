# P7-R-16 DeepSeek HTTP transport 离线实现交接

- 日期：2026-09-27
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**offline transport implementation complete；真实出站 preflight completed；default runtime integration NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-27-P7-R-15-deepseek-adapter-contract-hardening-handoff.md`

## 本批授权与范围

用户先审查并确认 GitHub 调研结论与实现方案，授权新增隔离的 DeepSeek HTTP transport 及离线测试；随后又单独确认执行一次真实 preflight。真实 preflight 使用仓库 `.env` 中的凭据作为一次性凭据源，但不把凭据写入文档或日志；本批不接入默认运行时，不启动 SDK/Laya，不执行工具，不进入 TTS/P8/ASR 设备或电脑副作用链。

## GitHub 调研结论已纳入实现

- `openai/openai-python`：借鉴显式 timeout、错误分类和可控重试边界；未复制 SDK 代码。
- `anthropics/anthropic-sdk-python`：借鉴 transport/timeout 分层；未采用其默认重试值。
- `BerriAI/litellm`：借鉴单一重试所有者，避免 transport 与路由层重试叠加。
- `deepseek-ai/deepseek-harness`：仅借鉴凭据隔离、诊断脱敏和生命周期边界；未接入其 developer-preview runtime。

## 实际变更

- `app/providers/deepseek_http_transport.py`
  - 新增标准库 `urllib.request` HTTPS transport；
  - 只允许现有 `DeepSeekAdapterConfig` 校验过的 HTTPS origin；
  - API key 仅在 `send()` 调用时从指定进程环境变量读取，默认变量名为 `DEEPSEEK_API_KEY`；
  - 不读取 dotenv、不记录密钥、不把密钥放入 JSON payload；
  - 固定发送一次 `POST /chat/completions`，无 transport 内部重试；
  - 传递显式 timeout；限制响应体为 1 MiB；
  - 将超时、连接错误、408/429/5xx 和非 2xx 响应映射到安全错误边界；
  - 只提取 `choices[0].message.content`，交给既有严格 DeepSeek route parser；
  - opener 可注入，便于离线测试；无默认运行时接线。
- `tests/providers/test_deepseek_http_transport.py`
  - 覆盖请求方法、HTTPS endpoint、Authorization、timeout 和 payload 脱敏；
  - 覆盖缺少 key 的 fail-closed 行为；
  - 覆盖 transport 无内部重试；
  - 覆盖 timeout、503、401 的错误分类和文本脱敏；
  - 覆盖 timeout、非法 JSON、空 choices 等响应边界。

## 验证事实

- HTTP transport 专项 + adapter 专项：**17/17** 通过。
- DeepSeek/routing 相关 discovery：**44/44** 通过。
- 标准全量 unittest discovery：**651/651** 通过。
- `compileall app tests`：通过。
- 新增 Python 文件严格 UTF-8 解码：通过。
- `git diff --check`：通过。

## 真实 preflight 结果

- 已按单独确认执行真实 DeepSeek HTTP preflight，**恰好 1 次请求**，未重试；不再重复执行本次真实请求；
- 请求目标为 `https://api.deepseek.com/chat/completions`，凭据来自仓库 `.env` 注入的进程环境变量 `DEEPSEEK_API_KEY`；临时进程变量随后已移除，`.env` 未被修改；
- 脱敏结果：

```json
{
  "request_count": 1,
  "elapsed_ms": 892.9,
  "router_status": "invalid_response",
  "failure_code": "invalid_router_output",
  "route": "ambiguous",
  "intent": "invalid_router_output",
  "slots_count": 0,
  "tool_scope_count": 0,
  "provider_ready": false
}
```

- HTTPS 请求路径已到达 DeepSeek 并收到响应；返回内容不符合既有严格 `deepseek-route/v1` parser；
- 系统按设计 fail-closed 到 `ambiguous / invalid_router_output`；没有 transport 重试、Provider fallback、工具、TTS、ASR 或电脑副作用；
- 请求体、响应原文、API key、请求 ID 和 conversation ID 未写入文档或日志。

## 未执行事项

- 未安装 SDK 或新增依赖；
- 未修改 `app/main.py`、默认运行时、ConversationManager、ToolRegistry、ExecutionLedger、Executor、Verification、Audit、TTS、P8 或 ASR；
- 未执行电脑副作用；
- 未 `git add`、commit 或 push。

## 下一停止点

真实 HTTP preflight 已完成，但默认运行时/provider integration 仍需单独确认。下一阶段应继续保持隔离：不自动接入默认运行时、不切换 Provider、不执行工具或电脑副作用；如需再次联网，必须重新明确授权并重新限定请求次数、目标、凭据来源和日志边界。

当前保持 **DEFAULT RUNTIME INTEGRATION NOT READY / STOP**。
