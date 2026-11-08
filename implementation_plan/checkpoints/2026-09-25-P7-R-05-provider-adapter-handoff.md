# P7-R-05 Provider adapter 与契约测试交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**complete**；P7-R-05 完成后停止，等待下一批独立授权
- 上一交接：`implementation_plan/checkpoints/2026-09-24-P7-R-04-conversation-routing-seam-handoff.md`

## 本批次唯一目标

在不连接真实 Laya、网络 API、TTS、P8 或电脑副作用链的前提下，实现一个 provider-neutral 的 `IntentRouter` adapter，并以无网络 stub 契约测试验证正常响应、超时、不可用、传输错误、非法响应、低置信度、trace 身份和原始 payload 隔离。

## 实际修改

- `app/routing.py`
  - 新增 `ProviderRequest`：只携带已校验的 `RouteTrace` 与原始文本，保持 trace 身份，不携带 provider payload、密钥或执行上下文。
  - 新增 `RoutingProvider` Protocol 与 `RouterProviderError` / `RouterProviderUnavailable`。
  - 新增 `ProviderIntentRouter`（别名 `RouterProviderAdapter`）：
    - 接收 callable 或 `classify()` provider；保存 provider/model 元数据与正数 timeout 配置。
    - 将 provider mapping 严格转换为 `RouteHandoff`；成功响应要求固定字段，slot 通过 `SlotCandidate` 校验，工具范围必须是字符串数组。
    - provider 低于 0.80 的成功置信度归一化为 `ambiguous/low_confidence`。
    - `TimeoutError`、不可用、传输错误和非法响应统一进入现有 `normalize_router_failure(...)`，不把异常文本或 payload 带入交接对象。
    - 拒绝 provider 替换调用方 trace，拒绝 provider handoff 的 provider/model/version 元数据漂移。
    - 不引入网络、进程、线程、ToolRegistry、ExecutionLedger 或执行调用。
  - 公开 adapter/request/error 类型。
- `tests/routing/test_provider_adapter.py`
  - 新增 11 项无网络契约测试：成功 handoff、请求不可变、失败归一化、超时/传输/不可用、非法 payload 隔离、trace 替换、低置信度、未知字段、confirmed slot 拒绝、metadata 漂移和依赖面审计。

## 验证事实

- Routing 专项：`21/21` 通过。
- Conversation 回归：`19/19` 通过。
- Integration 回归：`12/12` 通过。
- 全量 discovery：`596/596` 通过。
- `py_compile`：`app/routing.py` 与新测试通过。
- `app/`、`tests/` Python 文件严格 UTF-8 读取，无 `U+FFFD`。
- 未访问真实 Provider/Laya、网络 API、麦克风、TTS、电脑工具或执行链。

## 安全边界与未完成事项

- adapter 的 `timeout_seconds` 是配置/契约字段；实际超时由 provider 边界负责并以 `TimeoutError` 报告，本批次不启动线程、网络客户端或计时执行器。
- `RouteHandoff` 仍然只是路由提示，不是授权、计划、ledger、executor、verification 或 audit 结果。
- 尚未实现真实 Laya/provider adapter，也没有接入默认运行时；未改变 `ConversationManager`、ASR ResultGate、TTS 或 P8。
- provider 的 JSON-like mapping 只允许契约字段；原始 mapping 会先复制后解析，不会保存在结果中。

## 下一入口

下一批必须再次独立授权；建议入口为 provider-specific schema fixture / contract expansion 或在明确授权后评估真实 provider 接入。未经新授权不得连接真实 Laya、网络 API、TTS、P8 或电脑副作用。
