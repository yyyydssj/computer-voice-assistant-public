# P7-R-29 DeepSeek 真实对话 Agent（批次 B）交接（2026-09-30）

- 项目：`<PROJECT_ROOT>`
- 状态：**Agent 层真实对话已接通；`agent_ready`/`runtime_ready` 在双条件 opt-in 下为 True；默认 local 路径完全不变**
- 上一交接：`implementation_plan/checkpoints/2026-09-30-P7-R-28-deepseek-runtime-gate-lift-handoff.md`

## 1. 本轮授权（用户逐项确认）

| 授权项 | 确认值 |
|---|---|
| 路线 | **HTTP Chat Completions**（新写轻量 transport/adapter），不安装官方 SDK、不启动 DSH runtime |
| 输出与上下文 | **非流式**（整段返回，不调用 `on_text_chunk`）+ **单轮**（不带历史） |
| 开关 | **复用现有双条件**：`ASSISTANT_PROVIDER=deepseek` 且 `ASSISTANT_DEEPSEEK_OPT_IN=true`；默认全关 |
| 模型/参数 | `deepseek-flash`、`thinking=disabled`、`max_tokens=512`、超时 30 s、`max_retries=0`、仅 `api.deepseek.com` |
| 凭据 | 仅从进程环境读 `DEEPSEEK_API_KEY`；缺失即失败并如实提示 |
| 每轮请求数 | **2 次**（1 次路由 + 1 次回复），startup 如实提示计费 |
| 系统提示词 | 固定内置简体中文助手提示词（不接 persona 文件） |
| 失败行为 | 映射到 `AgentFailureCategory` 并如实返回；**不重试、不伪造、不回退脚本替身** |
| 边界 | 不接工具/计划/授权/账本/执行/验证链；Agent 只产文本 |
| 回滚 | 关闭 opt-in 即恢复"如实不可用" |

## 2. 改动清单

| 文件 | 改动 |
|---|---|
| `app/providers/deepseek_adapter.py` | 抽出并公开 `validate_https_origin()`（HTTPS origin + 白名单校验，错误文案不变），供路由与对话共用，避免两处漂移 |
| `app/providers/deepseek_chat_transport.py`（新增） | 对话专用 HTTPS 传输：`DeepSeekChatConfig`（默认 `enabled=False`/`authorized=False`）/`DeepSeekChatRequest`/`DeepSeekChatTransport`；**不使用** `response_format=json_object`；错误枚举化为 `DeepSeekChatRateLimited`/`DeepSeekChatUnavailable`/`ValueError`；凭据缺失 → `DeepSeekChatUnavailable`（修正 R-28 记录的"缺凭据被误归 invalid_response"问题） |
| `app/agent/deepseek_chat_agent.py`（新增） | `DeepSeekChatAgentAdapter`：实现既有 `submit_chat(request_id, session_id, text) -> AgentTurnOutcome`；请求防重提（`RequestSubmissionGate`）；8 类失败映射；非流式不回调 `on_text_chunk`；`close()` 后 fail closed |
| `app/providers/provider_integration.py` | `ProviderIntegration` 新增 `agent_ready` 入参（要求 transport 就绪）；`capabilities` 改为 `agent_ready = agent_ready and transport_ready`、`runtime_ready = transport_ready and agent_ready`；启动边界审计的 `readiness_preserved` 改为"投影必须与真实能力一致"（任何未授权提升仍记为 mismatch） |
| `app/main.py` | 新增 `_deepseek_route_config()`/`_deepseek_chat_config()`/`build_agent_adapter()`；`build_provider_integration(settings, *, agent_ready=False)`；`run()` 先算真实对话就绪再构建 provider，并据此投影就绪、注入 Agent、如实提示"每轮 2 次请求"；`agent_wired` 改用实际生效的 Agent |
| `tests/providers/test_deepseek_chat_transport.py`（新增 13 项） | 配置校验、payload 结构（无 JSON 模式）、成功/限流/不可用/拒绝/超时/连接失败/非法响应、凭据缺失不发请求、不回显密钥 |
| `tests/agent/test_deepseek_chat_agent.py`（新增 11 项） | 就绪语义、成功回合、未就绪不发请求、8 类失败映射、空响应、防重提、空白输入、非流式不回调、关闭后 fail closed |
| `tests/providers/test_deepseek_runtime_gate.py`（+7 项） | `build_agent_adapter` 双条件、装配参数=授权参数、就绪投影、`agent_ready` 依赖 transport、`chat_ready` 提示文案 |

## 3. 就绪语义（批次 B 之后）

| 字段 | local（默认） | deepseek 未 opt-in | deepseek + opt-in |
|---|---:|---:|---:|
| `adapter_injected` / `adapter_ready` | False | False | **True** |
| `provider_ready` | True | False | **True** |
| `transport_ready` | False | False | **True** |
| `agent_ready` | False | False | **True** |
| `runtime_ready` | False | False | **True** |

## 4. 验证

### 离线
- 全量回归：**1390 tests OK (skipped=3)**（R-28 后 1359 → 本轮 +31）；`compileall -q app tests tools` exit 0。
- 既有 1359 项全部保持通过：默认 local 路径、`ConversationManager`/`AssistantRuntime` 默认元数据、P8 语音链均未改变。

### 真实端到端（`ASSISTANT_PROVIDER=deepseek` + `ASSISTANT_DEEPSEEK_OPT_IN=true`，单轮）
```
capabilities: provider=deepseek, adapter_ready=True, provider_ready=True,
              transport_ready=True, agent_ready=True, runtime_ready=True
router_taken_over: True   metadata=(deepseek-official, deepseek-flash, runtime-2026-09-30)
route: chat | intent: general_chat | router_status: success
completed: True | finish_reason: stop
assistant_text: 我是小饼，一个运行在你本机的简体中文语音助手。
elapsed_ms: 1771.1   （路由 1 次 + 回复 1 次）
```
真实请求：本轮 **2 次**；累计（R-27 起）**13 次**，估算 ≈0.028 元。

### fail-closed（无凭据）
路由先失败并归一化为 `ambiguous`，`ConversationManager` 不会调用 Agent → **0 次请求**、无伪造回复（R-28 已实测同一路径）。

## 5. 边界与隔离

- 未改动 `app/safety`、`app/executor`、`app/plans`、`ToolRegistry`、`ExecutionLedger`、`Verification`、`Audit`；Agent 只返回文本，不产生任何执行资格。
- 未记录密钥、Authorization、请求/响应正文；`AgentTurnOutcome` 与失败诊断只含枚举与随机诊断 ID。
- 未使用工具调用（payload 中无 `tools`）、未启用 JSON 模式、单轮无历史。

## 6. 事实修正（重要）

**NEXT-SESSION 环境事实中"官方 Harness SDK/runtime 0.1.5rc1 已安装"与现状不符**：`pip list` 无该包、`import deepseek_harness` 报 `ModuleNotFoundError`，但两个 wheel 仍在 `.wheelhouse/`（`deepseek_harness_sdk-0.1.5rc1`、`deepseek_harness_runtime_bin-0.1.5rc1`）。本批次**未安装**它们（走 HTTP 路线）。若要恢复 Harness 路线，需另行授权安装并重新做隔离验证。

## 7. 残留与下一步（均需另行授权）

1. **语音链真机确认**：`--voice` 复用同一 `ConversationManager`，因此按键说话现在也应得到真实回复（每轮 2 次请求），但需要**用户真机实测**确认听感与延迟。
2. **多轮上下文**：当前单轮无历史；多轮需定义窗口、费用与隐私边界。
3. **流式**：当前非流式，首音延迟 ≈ 生成完整体时间（本轮 1.77 s 含路由）；流式需新增 SSE 实现与测试。
4. **费用/限流**：无速率限制实现；连续对话按轮计费（2 次/轮）。
5. **系统提示词**：固定内置；接入 persona 文件属后续批次。
6. **回滚**：关闭 `ASSISTANT_DEEPSEEK_OPT_IN` 即回到"如实不可用"；`ASSISTANT_PROVIDER=local` 完全恢复原行为。

## 8. 凭据来源变更（2026-09-30，用户指令）

- 用户要求把 DeepSeek 凭据换成新密钥。核实发现：项目 `.env` 中**已存在新值**（用户写入），而**用户级环境变量仍是旧值**（hash12 `5A171BD79569`），两者不一致。由于普通终端启动时会继承用户级旧值，**只写 `.env` 不会生效**（transport 从 `os.environ` 读密钥，而 `load_settings` 不注入进程环境）。
- 用户选择方案 A：把用户级环境变量更新为新值（`.env` 同步保留新值）。执行后两处一致（hash12 `2C3DFCC5498D`）。**全过程只打印哈希，未打印明文。**
- 为使 `.env` 成为可用来源，新增 `app/config/settings.py::inject_dotenv_secret()`：
  - **白名单**：目前仅允许 `DEEPSEEK_API_KEY`，其它键一律拒绝；
  - **环境变量优先**：进程环境已有非空值时不覆盖；
  - 只写内存，不打印、不记录、不返回密钥值；解析复用既有 `_read_dotenv`（不执行表达式）。
- `app/main.py` 仅在**双条件 opt-in**（`ASSISTANT_PROVIDER=deepseek` + `ASSISTANT_DEEPSEEK_OPT_IN=true`）且存在 `.env` 时调用该函数；默认 local 路径不读任何密钥。
- **边界变化（如实记录）**：R-27 的原始授权边界是"凭据仅从进程环境读、不落盘"；本轮按用户指令允许项目 `.env` 作为本地来源（`.env` 已在 `.gitignore`，`git ls-files .env` 为空，未被跟踪）。
- 真实验证（进程环境无密钥）：`injected_from_dotenv=True` → 真实对话成功（`chat/general_chat/success`、`completed=True`、1940.7 ms、2 次请求）。
- **安全提醒**：该密钥已在对话中明文出现，建议实测通过后到 DeepSeek 控制台轮换；`.env` 为磁盘明文，注意本机访问控制。
