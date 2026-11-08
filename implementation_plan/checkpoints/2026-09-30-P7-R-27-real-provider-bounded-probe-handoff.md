# P7-R-27 真实 DeepSeek Provider 有界探测交接（2026-09-30）

- 项目：`<PROJECT_ROOT>`
- 阶段状态：**受限真实探测完成；真实调用链与严格契约已接通；adapter 仍 NOT READY（未解除、未接入默认运行时）**
- 上一交接：`implementation_plan/checkpoints/2026-09-28-P7-R24-R26-real-route-gate.md`
- 证据目录：`implementation_plan/checkpoints/artifacts/2026-09-30-R-real-provider-probe/`

## 1. 本批授权矩阵（用户逐项确认）

| 授权项 | 本轮确认值 |
|---|---|
| 目标范围 | 仅最小真实探测；**不**接入默认运行时、**不**解除 `provider_ready=False` |
| Provider / 模型 | `deepseek-official` / **`deepseek-flash`**（官方当前名；legacy `deepseek-v4-flash` 对应模型已退役，2026-09-30 官方定价页核实） |
| 凭据来源 | 用户级环境变量 `DEEPSEEK_API_KEY`，仅在运行进程内注入；不打印、不落盘、不写入 `.env` |
| 网络目标 | 仅 `https://api.deepseek.com/chat/completions`（HTTPS，`allowed_hosts=("api.deepseek.com",)`） |
| 请求预算 | 单轮 ≤3 次、`max_retries=0`、单次超时 ≤15 s、`max_tokens=256` |
| 费用上限 | 每轮总额 ≤0.05 元（估算上界预检，超限即终止） |
| 日志脱敏 | 只记长度、SHA-256、枚举化状态、结构字段名；不记密钥、Authorization、请求正文、响应正文 |
| payload 隔离 | 输入输出只存在于隔离探测边界，不进入 ConversationManager / ToolRegistry / ExecutionLedger / Executor / Verification / Audit |
| 回滚 | 删除 runner 即可；`DeepSeekAdapterConfig.enabled` 默认仍为 `False`，离线路径与本地默认 Provider 不变 |

## 2. 代码改动（全部向后兼容）

| 文件 | 改动 |
|---|---|
| `app/providers/deepseek_adapter.py` | `DeepSeekAdapterConfig` / `DeepSeekRequest` 新增**可选** `max_tokens`（1..4096，默认 `None`）与 `thinking`（`"disabled"`/`"enabled"`，默认 `None`）；仅非 `None` 时写入 payload；`from_provider_request` 透传；`classify` 从配置透传。`provider_ready` 仍硬编码 `False` |
| `app/providers/deepseek_adapter.py` | `_ROUTE_SYSTEM_PROMPT` 重写为完整输出协议：9 个必填键、route/intent/handling_mode 组合表、confidence 阈值、`required_confirmation` 规则、slots 结构与允许键、tool_scope 允许集合、禁止 `{"response": ...}` 包装与围栏、内嵌合法示例、含糊请求的两种合法形态 |
| `tools/r_real_provider_probe.py`（新增） | 隔离探测 runner：`--check-only` 离线预检 / `--live-opt-in` 真实探测、`--thinking`、`--sample-id`、`--max-requests ≤3`、`--timeout-seconds ≤15`、`--max-tokens ≤4096`、`--cost-cap-cny ≤0.05`、`--diagnose`（HTTP 状态码 + 响应结构指纹 + content 形状指纹，绝不含正文） |
| `tests/providers/test_deepseek_max_tokens.py`（新增 7 项） | `max_tokens` 契约、边界值、非法值、配置透传、默认 payload 不变 |
| `tests/providers/test_deepseek_thinking_mode.py`（新增 7 项） | `thinking` 契约、组合、配置透传、非法值 |
| `tests/providers/test_deepseek_prompt_contract.py`（新增 11 项） | prompt 覆盖全部 route/intent/handling_mode/工具名（从契约常量核对，故障意图除外）；内嵌示例必须通过严格解析；`{"response": ...}` 包装必须被拒且 fail closed；`low_confidence` 极简形态必须通过；`low_confidence` 携带完整字段必须被拒；完整 `ambiguous` 在 ≥0.80 与 <0.80 下的安全归一化 |

## 3. 真实探测记录（共 10 次请求，估算费用 ≈0.022 元）

| # | 请求 | 模型输出形状 | 结果 |
|---|---|---|---|
| 1 | chat-1，默认（thinking 未关闭） | 未知（无诊断） | `invalid_response` → ambiguous 兜底 |
| 2 | chat-1，`thinking=disabled` | content 87 字符 | `invalid_response` |
| 3 | chat-1，形状指纹 | `{"response": "<自由文本>"}`，缺全部 9 个契约字段 | `invalid_response`（根因确认：输出协议不符，非网络/超时/截断） |
| 4 | chat-1 / task-1 / ambiguous-1（强化 prompt 后） | chat：9 键完整；task：9 键完整含 1 个 slot；ambiguous：`low_confidence` 却带完整 9 键 | chat ✅ `chat/general_chat/0.95`；task ✅ `task/open_application/0.95`（tool_scope 候选 `start_whitelisted_application`）；ambiguous ❌（契约只允许极简 3 键） |
| 5 | ambiguous-1（修正 prompt 后） | 形态 (b) 完整 success + ambiguous | ✅ `ambiguous/missing_target/0.85`，`handling_mode=clarify`，tool_scope 空 |
| 6 | ambiguous-1（复跑稳定性） | 形态 (a) 极简 `low_confidence` | ✅ `low_confidence/0.2` → `ambiguous/low_confidence/clarify`，tool_scope 空 |
| 7 | chat-1（最终 prompt 复测） | 9 键完整 | ✅ `chat/general_chat/0.95`，`handling_mode=chat_reply`，tool_scope 空 |
| 8 | task-1（最终 prompt 复测） | 9 键完整含 1 个 slot | ✅ `task/open_application/0.95`，`plan_then_execute`，`defer_to_safety`，tool_scope 候选 `start_whitelisted_application` |

> 第 4 轮为一次运行内连发 3 个样例，故上表 8 行对应 10 次请求。

HTTP 层全程正常：`http_status=200`、响应体为合法 JSON、`finish_reason=stop`、无 `reasoning_content`（`thinking=disabled` 生效）。

## 4. 关键结论

1. **真实链路可用**：HTTPS 直连 `api.deepseek.com` 稳定返回（650–1411 ms），`thinking=disabled` 生效，`response_format=json_object` 被接受。
2. **失败根因是输出协议，不是网络**：默认 system prompt 太弱时，模型退回 `{"response": ...}` 通用包装；强化协议后真实输出可 100% 通过既有严格解析。
3. **契约存在两个易错点，已固化**：
   - `status="low_confidence"` 只允许 `{schema_version, status, confidence}` 三键；
   - `route="ambiguous"` 的完整 success 形态要求 `confidence ≥0.80`，低于阈值会被归一化为 `low_confidence` 失败交接。
4. **全程 fail closed**：任何解析失败都落到 `ambiguous` + `intent=invalid_router_output`，`tool_scope=()`、`slots={}`，不产生任何执行资格。
5. **真实输出仍不构成授权**：`task` 路由只给出候选 `tool_scope`，仍需本地 manifest/policy/确认链；`provider_ready` 保持 `False`。

## 5. 执行面与隔离审计

- 未创建计划、授权、账本或执行事实；未调用工具；未产生任何电脑副作用。
- 未接入 `ConversationManager`、默认运行时、`ToolRegistry`、`ExecutionLedger`、`Executor`、`Verification`、`Audit`、TTS/ASR。
- 未记录密钥值、Authorization header、请求正文或响应正文；artifact 只含长度、SHA-256、字段名、安全枚举字面值与耗时。
- 未修改启动状态、本地默认 Provider 或离线回退路径。

## 6. 验证

- 新增测试 25 项（7 + 7 + 11）全部通过。
- 全量回归：**1332 tests OK (skipped=3)**；`compileall -q app tests tools` exit 0。
- 离线预检 `--check-only`：配置、白名单主机、超时、无重试、`max_tokens` 字段、`thinking` 字段、旧 payload 不变、费用上界 8 项全部通过。

## 7. 残留风险与下一步（均需另行授权）

1. **样本量小**：`chat`/`task` 已在最终 prompt 下复测通过（chat-1 0.95、task-1 0.95，均 `strict=True`），但每类只测 1–2 次，未做稳定性统计或多样例覆盖。
2. **模型名 legacy 风险**：官方推荐 `deepseek-flash`，但 DeepSeek 保留调整权；模型/价格需在每次真实验证前重新核实。
3. **未解除 NOT READY**：`DeepSeekProviderAdapter.provider_ready` 仍为 `False`，`runtime_ready/agent_ready/transport_ready` 仍为 `False`；解除需另行授权并定义运行时映射、失败矩阵与回滚。
4. **未接入语音/任务链**：`python -m app.main --voice` 仍走 `LocalChatRouter`；任务链路接默认运行时依赖第 3 项。
5. **费用与限流**：真实请求仍需人工确认余额；本轮未观测到 429，但未做并发/限流压测。
