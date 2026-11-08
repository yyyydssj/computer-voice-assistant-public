# P7-R-15 DeepSeek adapter 契约加固交接

- 日期：2026-09-27
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**implementation complete；真实 Provider transport / SDK / 默认运行时仍 NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-27-P7-R-14-deepseek-default-off-adapter-contract-handoff.md`

## 本批授权与唯一目标

用户确认继续执行推荐路线中的离线 P7-R-15：只加固默认关闭 DeepSeek adapter 的配置、错误边界、重试边界、数据隔离和无副作用契约。不联网、不读取密钥、不启动 DeepSeek SDK/Laya、不接入默认运行时、不进入 TTS/P8/ASR 设备或电脑副作用链。

## 实际变更

- `app/providers/deepseek_adapter.py`
  - 将可重试 transport 异常固定为 `TimeoutError`、`RouterProviderUnavailable`、`ConnectionError` 和 `OSError`；未知 `Exception` 不在 adapter 层重试，由既有 `ProviderIntentRouter` 统一归一化为 `invalid_response`；
  - 加强 `base_url` 校验：拒绝非法 URL、端口、路径、凭据、query、fragment 和非 HTTPS origin；
  - 加强 `allowed_hosts` 校验：必须是非空 hostname tuple，拒绝空值、空白、路径分隔符、端口和凭据字符；host 比较保持大小写不敏感；
  - 保持 `enabled=False` 默认值、`provider_ready=False`、无 SDK/HTTP/凭据读取和无默认运行时接线。
- `tests/providers/test_deepseek_adapter_contract.py`
  - 新增 URL origin、allowlist、timeout、`NaN`/`inf`、bool 类型和重试上下界测试；
  - 新增未知 transport 异常不重试且不泄露文本测试；
  - 新增 unavailable 重试后仍为不可执行失败测试；
  - 使用 `ContractError` 断言配置边界，避免宽泛异常断言。
- `implementation_plan/checkpoints/2026-09-27-P7-R-15-deepseek-adapter-contract-hardening-handoff.md`
  - 新增本批次事实、验证、执行面审计和下一停止点。
- `progress.md`
  - 追加本批次完成记录和真实 Provider 独立授权门。

## 冻结的契约

- `DeepSeekAdapterConfig.enabled` 默认必须为 `False`；
- `DeepSeekProviderAdapter.provider_ready` 固定返回 `False`；
- 只有明确分类的暂态 transport 错误允许按 `0..3` 的 `max_retries` 重试；
- 非字符串响应、非法 JSON、协议不匹配和未知异常不得重试，必须 fail-closed；
- 失败结果只能通过现有 `ProviderIntentRouter` / `normalize_router_failure` 进入安全的 `RouteHandoff`；
- 失败结果的 `slots` 和 `tool_scope` 必须为空，不能产生授权或执行资格；
- payload 不携带 API key、request ID、conversation ID 或其他凭据/追踪字段；
- `base_url` 只能是 allowlist 中的 HTTPS origin，不允许端口、路径、query、fragment 或内嵌凭据。

## 验证事实

- DeepSeek adapter 专项：**12/12** 通过。
- 标准全量 unittest discovery：**646/646** 通过。
- `compileall app tests`：通过。
- `app/providers` 与 `tests/providers` Python 文件严格 UTF-8 解码：通过。
- 未发现 Provider 目录中的 HTTP client、SDK、socket、凭据读取或进程启动实现。

## 安全与范围结论

- 未读取 `.env`、`DEEPSEEK_API_KEY` 或任何凭据。
- 未发起网络请求，未启动真实 Provider/Laya 服务，未调用 SDK。
- 未修改 `app/main.py` 或默认运行时。
- 未接入 `ConversationManager`、ToolRegistry、ExecutionLedger、Executor、Verification、Audit、TTS、P8 或 ASR 设备。
- 未执行电脑副作用；`tool_scope` 仍只是候选路由字段，不能直接视为授权。
- 仍必须遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。

## 下一停止点

真实 DeepSeek HTTP/SDK transport、凭据注入、出站请求、响应 envelope 兼容性、成本/限流实测和默认运行时接线均需要另行逐项授权。下一批建议单独授权一个隔离的真实 transport preflight，并明确凭据来源、网络目标、HTTP/SDK 选择、最大请求次数、费用、日志和回滚边界。在获得授权前保持 **NOT READY / STOP**。