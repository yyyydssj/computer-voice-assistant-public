# P7-R-07 离线 Provider 兼容性矩阵与 ConversationManager 接缝回归交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**complete**；本批完成后停止，下一批须独立授权
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-06-provider-schema-fixtures-handoff.md`

## 本批唯一目标

在不连接真实 Laya、模型、网络、密钥、TTS、P8 或电脑副作用链的前提下，使用本地 stub/fixture 固化 Provider 兼容性矩阵，并验证 `ProviderIntentRouter -> ConversationManager` 的统一文字/ASR 路由接缝。只有测试证实产品缺口时才修改 `app/`；本批最终未发现需要修改产品代码的缺口。

## 实际修改

- `tests/routing/test_provider_compatibility_matrix.py`
  - 新增 6 个离线契约测试方法。
  - 覆盖 `chat/query/task` 三种成功路由及其确认/工具范围边界。
  - 覆盖 `timeout/unavailable/transport_error/invalid_response/low_confidence` 失败矩阵。
  - 覆盖 Provider 异常正文隔离。
  - 覆盖 `ProviderIntentRouter` 与 `ConversationManager` 共享同一 `RouteTrace` 身份边界。
  - 覆盖 TextShell 与 ASR accepted-final 进入同一 Conversation 路由入口；chat 可进入现有 Conversation adapter，task 仍停在未执行的路由交接边界。
  - 覆盖 ambiguous Provider 结果不进入 Conversation adapter，以及 Provider 直接返回失败 `RouteHandoff` 时保持安全失败交接。
- `implementation_plan/checkpoints/2026-09-25-P7-R-07-provider-compatibility-seam-handoff.md`
  - 记录本批范围、实际变更、验证结果、执行面审计和下一批入口。
- `progress.md`
  - 追加 P7-R-07 完成事实。
- `implementation_plan/implementation-roadmap.md`
  - 将 P7-R 当前状态从过时的“代码尚未开始”同步为 R-01 至 R-07 已完成、真实 Provider 评估尚未授权。
  - 保留真实 Provider 评估必须单独授权的边界。

未修改：`app/`、`ConversationManager`、默认运行时、ASR、TTS、P8、安全执行链、授权、账本、Executor、Verification、Audit。

## 验证事实

- Provider routing 专项：**32/32** 通过。
- Conversation 回归：**19/19** 通过。
- Integration 回归：**12/12** 通过。
- 标准全量 discovery：**607/607** 通过。
- 新测试文件 `py_compile` 通过。
- 本批新增测试及相关 routing/conversation Python 文件严格 UTF-8 解码通过，未发现 `U+FFFD`。
- 执行面审计：本批测试和 `app/routing.py` 未引入 socket、requests、urllib/httpx、subprocess、进程/线程启动、`.env` 读取、ToolRegistry、ExecutionLedger、Executor 或 Verification 调用。
- 未调用真实 Provider/Laya、网络 API、麦克风、TTS、电脑工具或执行电脑副作用。

## 冻结事实与边界

- `ProviderIntentRouter` 仍是 provider-neutral、注入式 adapter；`timeout_seconds` 仍只是配置/契约字段，实际超时由 Provider 边界报告 `TimeoutError`，本批未实现网络客户端或计时线程。
- `RouteHandoff` 仍只是未受信路由提示，不是授权、ActionPlan、ExecutionLedger、Executor、Verification 或 Audit 资格。
- `task` 成功 fixture 只证明路由 schema 和接缝边界，不代表已创建计划、完成预检、取得授权或执行电脑动作。
- fixture 是 provider-neutral 合成数据，不代表任何真实 Laya 意图模型已经兼容；本批没有真实 schema 证据。
- 默认 `app.main.run()` 仍不自动创建或启用 Provider；`provider_ready` 保持 false。

## 下一批明确入口

### P7-R-08：真实 Provider 评估前置审计与独立授权门

下一批**不是自动联网接入**，也不是直接实现真实 Laya adapter。唯一目标是先把真实 Provider 评估所需的输入和安全边界写清楚，并判断是否具备进入真实请求的条件：

1. 明确目标 Provider、具体模型/版本和用途；
2. 收集一份经脱敏的真实响应样例，或明确样例尚不可得；
3. 明确密钥来源、是否允许读取环境变量、允许访问的网络域名、请求/费用/超时上限；
4. 将真实响应字段与当前 `RouteHandoff` schema 做兼容性矩阵，不把 `billing/technical` 等 Provider 原生分类直接当作电脑助手意图；
5. 明确真实请求前的失败、日志脱敏、payload 隔离和回滚方案；
6. 只有用户对上述边界逐项确认后，才可另行授权真实 Provider 请求或实现 provider-specific adapter。

若上述任一条件未满足，P7-R-08 必须停留在离线审计/方案阶段，不连接网络、不读取密钥、不启动 Provider。P7-R-08 不进入 TTS、P8、ASR 设备或电脑副作用链。

### P7-R-08 禁止事项

- 不凭空选择模型或 Provider；
- 不读取 `.env`、系统密钥或个人凭据；
- 不发起真实网络请求；
- 不下载模型或启动服务；
- 不把真实 Provider 输出直接接入默认运行时；
- 不绕过 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit` 链；
- 不修改 TTS、P8、ASR、默认运行时或电脑执行链。