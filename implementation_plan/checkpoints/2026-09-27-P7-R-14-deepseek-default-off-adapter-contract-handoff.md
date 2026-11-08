# P7-R-14 DeepSeek 默认关闭 adapter 契约交接

- 日期：2026-09-27
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**implementation complete；真实 Provider transport / SDK / 默认运行时仍 NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-13-deepseek-raw-fixture-contract-handoff.md`

## 本批目标

在不联网、不读取密钥、不启动 DeepSeek SDK/Laya、不接入默认运行时、不进入 TTS/P8/ASR 设备或电脑副作用链的前提下，设计并实现一个默认关闭的 DeepSeek provider-specific adapter 契约。优先复用既有 `ProviderRequest`、`ProviderIntentRouter`、`normalize_router_failure`、`parse_deepseek_raw_route_response` 和 `RouteHandoff`，不复制第三方 SDK 代码。

## 实际变更

- `app/providers/deepseek_adapter.py`
  - 新增无凭据 `DeepSeekAdapterConfig`，校验 HTTPS origin、allowlist、超时和 0..3 重试边界；
  - 新增不可变 `DeepSeekRequest`，生成 fresh 的 OpenAI-compatible payload-shaped mapping；
  - 新增注入式 `DeepSeekTransport` Protocol；
  - 新增 `DeepSeekProviderAdapter`，默认关闭，复用现有 `ProviderIntentRouter` 和 DeepSeek 原始响应解析器；
  - 仅对 timeout/unavailable/transport error 做有界重试；非法响应不重试；
  - `provider_ready` 固定为 `False`。
- `tests/providers/test_deepseek_adapter_contract.py`
  - 覆盖默认关闭、成功 fixture、重试、超时归一化、非法响应、低置信度、payload 隔离和配置边界。
- 未修改 `app/main.py`，未读取 `.env` 或 API key，未实现 HTTP/SDK transport，未接入真实 Provider。

## 复用与参考原则

本批复用本项目既有 provider-neutral routing seam；OpenAI-compatible payload 的字段形状仅作为注入式 transport 契约，不代表已实现真实网络客户端。未复制外部项目代码、未新增第三方依赖、未宣称 fixture 来自真实模型。

## 安全结论

- adapter 默认 `enabled=False`，默认 route 不调用 transport；
- adapter 不读取凭据，不创建网络 client，不启动线程/进程，不写 ledger，不产生授权或执行资格；
- Provider 输出仍必须经过 `deepseek-route/v1` 原始 JSON 解析和既有路由校验；
- Provider 输出的 `tool_scope` 只是候选路由字段，不能直接视为授权；
- 仍必须遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。

## 下一停止点

真实 DeepSeek HTTP/SDK transport、密钥注入、出站请求、成本/限流实测、真实响应 envelope 兼容和默认运行时接线都需要另行逐项授权。在获得授权前，保持 `NOT READY / STOP`。
## GitHub 先行调研与代码复用规则

- 后续遇到实现不确定、接口设计不确定或安全边界不确定时，必须先到 GitHub 查找同类成熟项目、官方实现或主流框架的核心源码，不能直接凭空创造方案。
- 优先复用本项目已有代码和契约；项目内没有合适实现时，优先复用许可证允许的成熟代码、接口模式、测试模式和错误处理方式。
- GitHub 调研必须查看核心源码，而不是只看 README；记录真实仓库、文件、许可证和可复用点，不编造项目、star 数、URL 或实现结论。
- 只有在现有代码和成熟开源实现均不适用时，才自行设计；此时必须说明为什么不能直接复用，以及新设计与参考实现的差异。
- 复用外部代码时保留必要的许可证和版权信息；只借鉴设计时不得声称复制或复用源码。
- 该规则不改变本项目安全停止点：真实 Provider、网络、密钥、SDK、默认运行时接线仍须单独授权。