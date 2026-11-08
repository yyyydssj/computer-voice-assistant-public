# P7-R-03 failure normalizer 批次交接

- 日期：2026-09-24
- 项目：`<PROJECT_ROOT>`
- 阶段状态：P0–P6 complete；P7-R-01 complete；P7-R-02 complete；P7-R-03 complete；P7-R-04 not_started；P7/TTS 未开始
- 当前停止点：**P7-R-03 完成后停止，下一批次须独立授权/验收；不进入 ConversationManager 接线、真实 Provider、TTS 或 P8**
- 上一交接：`2026-09-24-P7-R-02-pure-intent-router-interface-handoff.md`

## 唯一目标与实际修改

本批次只实现路由器/Provider 失败的 provider-neutral 规范化：把受限的失败状态转换为安全、可校验、不可执行的 `ambiguous` `RouteHandoff`。不实现 Provider 调用，不把供应商原始错误内容带入契约，不接入业务入口或执行链。

- `app/routing.py`：新增 `normalize_router_failure(...)`，并通过 `__all__` 公开导出。
- `tests/routing/test_failure_normalizer.py`：新增 5 个测试方法，覆盖 Provider 不可用/超时/传输错误、非法响应、低置信度、trace 身份保持、非法输入、状态类型污染、置信度边界和 Provider payload 隔离。
- `task_plan.md`、`progress.md`：同步本批次完成事实与下一停止点。

公开入口：

```python
normalize_router_failure(
    trace,
    provider="laya",
    model_id="intent-router-v1",
    model_version="1",
    status="timeout",
)
```

其中 `trace` 必须是已校验的 `RouteTrace`；`status` 只允许既有契约枚举：`unavailable`、`timeout`、`transport_error`、`invalid_response`、`low_confidence`。失败码映射如下：

| 输入 status | 输出 route/intent | `router.failure_code` |
|---|---|---|
| `unavailable` | `ambiguous/router_unavailable` | `router_unavailable` |
| `timeout` | `ambiguous/router_unavailable` | `router_unavailable` |
| `transport_error` | `ambiguous/router_unavailable` | `router_unavailable` |
| `invalid_response` | `ambiguous/invalid_router_output` | `invalid_router_output` |
| `low_confidence` | `ambiguous/low_confidence` | `low_confidence` |

## 安全边界

- 每个成功返回值都由既有 `RouteHandoff` 和 `RouterStatus` 构造器再次校验，不返回未经校验的 dict，不暴露供应商原始 payload。
- 失败结果固定为 `route="ambiguous"`、对应失败 `intent`、`handling_mode="clarify"`、`required_confirmation="must_clarify"`、`slots={}`、`tool_scope=()`；它只能触发澄清，不能取得授权、计划、账本或执行资格。
- 直接复用调用方传入的原始 `RouteTrace`，保留 `request_id`、`conversation_id`、`turn_id`、来源、原话和创建时间；本批次不创建、不替换、不重写业务标识。
- `error_text` 仅作为拒绝边界存在：传入任何 Provider 异常正文都会抛出 `ContractError`，异常正文不会进入 `RouteHandoff`、会话或用户可见流程。
- 非 `RouteTrace`、未知 status、非字符串 status、非 `low_confidence` 失败携带非零 confidence 均拒绝；confidence 仍由 `RouteHandoff` 约束在 0..1，低置信度不能伪装成可路由成功。
- 本批次没有网络、API、进程、命令、线程、设备、工具注册表、授权、账本、Executor、Verification 或审计持久化调用。
- `RouteHandoff` 仍然只是路由提示，不是授权或执行资格；电脑副作用继续遵守 `Plan → preflight → authorization → ledger → executor → verification → audit`。

## 验证与事实

- P7-R-03 专项：`python -m unittest discover -s tests/routing -t . -q`：**10/10**。
- Contracts 回归：`python -m unittest discover -s tests/contracts -t . -q`：**44/44**。
- Conversation 回归：`python -m unittest discover -s tests/conversation -t . -q`：**19/19**。
- 标准全量 discovery：`python -m unittest discover -s tests -t . -q`：**585/585**。
- `app/routing.py` 精确 AST/import 执行面审计通过：仅 `dataclasses`、`typing` 和既有 `app.contracts`，无网络/进程/命令/设备/工具/执行链导入或调用。
- `app/` 与 `tests/` 共 146 个 Python 文件按严格 UTF-8（兼容既有 BOM 的 `utf-8-sig` 解析）编译检查通过，无 `U+FFFD`。
- 未访问真实 Laya、云端 Provider、麦克风或电脑工具；未执行任何电脑副作用。

## 已知风险及明确延期

- 当前只有失败规范化函数和专项测试，没有默认 IntentRouter 实现、真实模型或 Provider adapter。
- `normalize_router_failure` 要求调用方先提供已创建且已校验的 `RouteTrace`；本批次不负责 TextShell/ASR 入口、ConversationManager trace 创建、同一 `turn_id` 防重或迟到结果处理。
- 本批次没有把 `RouteHandoff` 接入 ConversationManager，也没有实现用户澄清后的受控重路由、slot 确认或 Agent/ActionPlan/Safety 下游。
- `provider="laya"` 只是契约中的来源元数据示例；本批次不代表已连接 Laya，也不代表已验证任何云端模型协议。

## 下一批次入口（本会话不实施）

P7-R-04：ConversationManager 文字/语音接缝。目标应限定为复用入口已创建的 `RouteTrace`，调用纯 `IntentRouter`，将成功/失败结果统一交给 `RouteHandoff`，并保持文本输入与 ASR accepted final 的单次提交、防重、澄清边界和现有安全链不变。该批次必须另行明确授权、专项验证和 handoff。

不得在未获新授权前实现真实 Provider/Laya、网络请求、TTS、P8、自动执行或任何电脑副作用。