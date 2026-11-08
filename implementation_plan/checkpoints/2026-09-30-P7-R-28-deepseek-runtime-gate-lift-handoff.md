# P7-R-28 解除 DeepSeek 运行时门禁（批次 A）交接（2026-09-30）

- 项目：`<PROJECT_ROOT>`
- 状态：**provider 门禁已解除；真实 transport 已装配并接管路由；`runtime_ready`/`agent_ready` 仍为 False（Agent 真实对话属批次 B）**
- 上一交接：`implementation_plan/checkpoints/2026-09-30-P7-R-27-real-provider-bounded-probe-handoff.md`

## 1. 本轮授权（用户逐项确认）

| 授权项 | 确认值 |
|---|---|
| 解除范围 | **仅批次 A**：provider 门禁 + 路由层接入；`runtime_ready`/`agent_ready` 保持 `False`；不接语音/任务执行链 |
| 启用条件 | **双条件**：`ASSISTANT_PROVIDER=deepseek` **且** 新增 `ASSISTANT_DEEPSEEK_OPT_IN=true`；默认全关 |
| 运行时网络 | 配置开启后按使用自动请求（不逐次询问），并由 startup 如实提示计费与不回退 |
| 凭据 | 仅从进程环境读 `DEEPSEEK_API_KEY`；缺失即 fail closed |
| 失败行为 | **不 fallback**：显式选 deepseek 后遇到失败只报不可用，不静默切回 local |
| 模型/参数 | `deepseek-flash`、`thinking=disabled`、`max_tokens=256`、超时 15 s、`max_retries=0`、仅 `api.deepseek.com` |
| 边界不动 | `app/safety`、`app/executor`、`app/plans` 语义与授权链不变；provider 只影响路由，永不授权执行 |
| 回滚 | 去掉 opt-in 或把 `ASSISTANT_PROVIDER` 改回 `local` 即恢复；代码向后兼容 |

## 2. 改动清单

| 文件 | 改动 |
|---|---|
| `app/config/settings.py` | 新增 `deepseek_opt_in`（env `ASSISTANT_DEEPSEEK_OPT_IN`）+ 严格 `_read_bool`（true/false/1/0/yes/no/on/off，非法值报 `ConfigError`）；`public_summary` 增加该字段 |
| `app/providers/deepseek_adapter.py` | `DeepSeekAdapterConfig` 新增 `authorized`（bool；`authorized` 必须 `enabled`）；`provider_ready` 由恒 `False` 改为 `enabled and authorized` |
| `app/providers/provider_integration.py` | `ProviderIntegration` 新增 `deepseek_transport_ready` 入参（无 adapter 时拒绝）；`capabilities.transport_ready` 仅在 `provider=deepseek` 且注入 adapter 且标志为真时为 `True`；新增 `router_metadata`；启动边界审计的 readiness 断言只针对 `runtime_ready`/`agent_ready` |
| `app/main.py` | `build_provider_integration` 按双条件装配真实 `DeepSeekHTTPTransport` + adapter（构造不读密钥、不联网）；新增 `select_runtime_router`（显式注入优先，其次 transport 就绪时接管）；新增 `provider_startup_notice`（就绪计费提示 / 未 opt-in fail-closed 提示） |
| `app/runtime/assistant_runtime.py` | `AssistantRuntime.build` 新增可选 `router_metadata`，使 ConversationManager 的失败交接元数据与真实 provider 一致（默认值不变） |
| `tests/foundation/test_config.py` | 新增 5 项 opt-in 解析测试（默认、真/假 token、非法值、不泄露密钥） |
| `tests/providers/test_deepseek_runtime_gate.py`（新增 22 项） | adapter 就绪语义、`transport_ready` 语义、传输标志无 adapter 时拒绝、**缺 adapter 时不回退 local**、`build_provider_integration` 三种组合、装配参数=授权参数、构造不需要凭据、`router_metadata`、路由接管选择、startup 提示 |

## 3. 就绪语义（本轮之后）

| 字段 | local（默认） | deepseek 未 opt-in | deepseek + opt-in |
|---|---:|---:|---:|
| `adapter_injected` | False | False | **True** |
| `adapter_ready` | False | False | **True** |
| `provider_ready` | True | False | **True** |
| `transport_ready` | False | False | **True** |
| `runtime_ready` | False | False | **False**（批次 B 才可能为真） |
| `agent_ready` | False | False | **False** |

`transport_ready=True` 只表示"真实 HTTPS transport 已装配且被授权"，**不等于端到端运行时接通**：Agent 侧真实回复生成尚未实现（缺失时仍如实提示不可用）。

## 4. 验证

### 离线
- 全量回归：**1359 tests OK (skipped=3)**（P7-R-27 后 1332 → 本轮新增 27 项）；`compileall -q app tests tools` exit 0。
- 既有 1332 项全部保持通过：默认 local 路径行为、`ConversationManager`/`AssistantRuntime` 默认元数据、P8 语音链均未改变。

### 端到端（真实配置，`ASSISTANT_PROVIDER=deepseek` + `ASSISTANT_DEEPSEEK_OPT_IN=true`）
1. **无凭据（fail closed）**：`capabilities.transport_ready=True`、路由接管生效；真实路由返回 `invalid_response` → `ambiguous`/`invalid_router_output`，`tool_scope=()`；**未回退到 local**（否则应为 `chat/general_chat`）。真实请求数 **0**。
2. **有凭据（真实请求 1 次）**：输入「帮我打开计算器」→ `router_status=success`、`route=task`、`intent=open_application`、`handling_mode=plan_then_execute`、`confidence=0.95`、`slots=1`、`tool_scope=('start_whitelisted_application',)`、`required_confirmation=defer_to_safety`，耗时 731.5 ms。仅产生路由候选，未进入计划/授权/账本/执行/验证链。

累计真实请求：R-27 的 10 次 + 本轮 1 次 = **11 次**，估算 ≈0.024 元。

## 5. 边界与隔离

- 未改动 `app/safety`、`app/executor`、`app/plans`、`ToolRegistry`、`ExecutionLedger`、`Verification`、`Audit`。
- provider 仅决定路由交接；`RouteHandoff` 仍是非授权建议，`tool_scope` 只是候选，须经本地 manifest/policy/确认链。
- 未记录密钥、Authorization、请求/响应正文；startup 与 capabilities 投影只含非敏感字段。
- 未创建计划、授权、账本或执行事实；未产生电脑副作用。

## 6. 残留与下一步

1. **语音/文字仍不会真正对话**：Agent 层真实回复生成未实现（`UnavailableAgentAdapter` 仍如实报不可用）。需要**批次 B**。
2. **`runtime_ready`/`agent_ready` 仍为 False**，符合本批次范围。
3. **费用随使用增长**：开启 opt-in 后每次路由都会真实请求；未做速率限制实现（仅 `max_retries=0`），如需限流需另行设计。
4. **凭据缺失被归类为 `invalid_response`**：现有 transport 把"key 缺失"抛为 `ValueError`，路由层归一化为 `invalid_response`（而非 `unavailable`）。安全上仍是 fail closed，但语义可更精确，留作后续小改。
5. **回滚**：删除 `ASSISTANT_DEEPSEEK_OPT_IN`（或设 false）即回到 NOT READY；将 `ASSISTANT_PROVIDER` 改回 `local` 即完全恢复原行为。
