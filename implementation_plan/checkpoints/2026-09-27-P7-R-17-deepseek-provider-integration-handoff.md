# P7-R-17 DeepSeek provider integration 离线交接

- 日期：2026-09-27
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**offline provider integration complete；default runtime remains local / DeepSeek NOT READY**
- 上一交接：`implementation_plan/checkpoints/2026-09-27-P7-R-16-deepseek-http-transport-preflight-handoff.md`

## 本批授权与范围

用户确认先调研后实现，并授权按方案 A 执行离线、默认关闭的 provider integration seam。本批不联网、不读取 `.env` 或 `DEEPSEEK_API_KEY`、不再次执行真实 HTTP 请求、不修改 `app/main.py` 或默认运行时，不进入工具、ExecutionLedger、Executor、Verification、Audit、TTS、P8、ASR、Laya 或电脑副作用链。

## 调研结论

- `openai/openai-python`：继续采用显式 timeout、错误分类和单一 retry owner。
- `anthropics/anthropic-sdk-python`：继续保持 transport/client 分层和注入式 transport，不新增 SDK 依赖。
- `BerriAI/litellm`：避免 transport 与路由层重试叠加，本批不增加 fallback。
- DeepSeek/OpenAI-compatible API：继续只提取 `choices[0].message.content`，并交给既有严格 `deepseek-route/v1` parser；普通 JSON 合法不等于路由协议成功。
- `deepseek-ai/deepseek-harness`：仅借鉴生命周期和凭据隔离，不接入其 runtime。

## 实际变更

- `app/providers/provider_integration.py`
  - 新增显式 `ProviderIntegrationConfig`，选择仅允许 `local` 或 `deepseek`；
  - 默认选择 `local`，默认路径只调用注入的 local router；
  - 只有显式选择 `deepseek` 且注入 `DeepSeekProviderAdapter` 时才进入 DeepSeek seam；
  - 缺少显式 adapter 或非法 `RouteInput` 时 fail-closed；
  - 不创建 transport、不读取凭据、不实现 fallback、不改变默认运行时。
- `app/providers/__init__.py`
  - 导出 provider integration seam。
- `tests/providers/test_provider_integration.py`
  - 覆盖默认 local 选择不调用 DeepSeek；
  - 覆盖显式 DeepSeek + fake adapter 路由；
  - 覆盖缺少 adapter、非法 provider、非法请求边界；
  - 覆盖 DeepSeek 失败结果保持 `ambiguous`、空 `slots` 和空 `tool_scope`。

## 验证事实

- Provider integration 专项：**5/5** 通过。
- DeepSeek transport + adapter 回归：**22/22** 通过。
- 标准全量 unittest discovery：**656/656** 通过。
- `compileall app tests`：通过。
- 新增 Python 文件严格 UTF-8 解码：通过。
- `git diff --check`：通过。
- 执行面审计：新增 integration seam 与测试未发现 HTTP、socket、进程启动、凭据读取或执行调用。

## 安全与范围结论

- 默认 provider 仍为 local；`app/main.py` 未修改。
- `DeepSeekProviderAdapter.provider_ready` 仍固定为 `False`；显式 DeepSeek seam 不会把它提升为真实可用 Provider。
- DeepSeek 失败只通过既有 adapter/router/parser 边界归一化，不能产生工具授权或执行资格。
- 未联网、未读取密钥、未执行电脑副作用；未再次执行真实 preflight。
- 本批未 `git add`、commit 或 push。

## 下一停止点

P7-R-17 离线 integration 已完成。默认运行时/provider integration 仍 **NOT READY / STOP**。如需将显式 DeepSeek seam 接入配置、启动入口或真实运行时，必须另行确认 provider 选择、凭据来源、网络请求次数、费用/限流、日志脱敏、回滚和副作用隔离边界；在新授权前不得接线或联网。
