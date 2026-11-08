# 阶段 5：DeepSeekHarness Python SDK 接入（纯文字，无工具）

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/safety-boundaries.md`](./shared/safety-boundaries.md)
- [`discussions/pending-decisions.md#g-llmagent-边界`](./discussions/pending-decisions.md#g-llmagent-边界)

- 目标：文字进去、文字出来，能对话，并验证启动、会话隔离和失败停止规则。
- 步骤：
  - [ ] `llm/` 封装 DeepSeekHarness Python SDK；程序启动时只初始化适配器并检查配置，不创建会话或发起模型请求
  - [ ] 第一次收到实际请求时延迟创建会话；闲聊会话和按 `task_id` 建立的工作会话分开管理
  - [ ] 管理 `request_id`、`session_id`、`task_id`，形成计划后再关联 `plan_id` 与 `plan_version`
  - [ ] 闲聊会话连续闲置满 30 分钟后销毁且默认不落盘；工作会话在任务完成、取消或失败后关闭
  - [ ] 读 `persona.md` 作为 system message，并将其作为所有面向用户自然语言回复的强制表达约束；工作模式只传入当前明确任务和必要的最小上下文
  - [ ] 闲聊、追问、澄清、错误说明、任务进度和执行结果均保持人物模板；安全内容必须准确完整，人物表达不得诱导授权或淡化风险
  - [ ] 人物模板只能由用户修改或切换；Agent 只能读取当前已确认版本，不能在运行中自行改写人设、称呼、关系或风格
  - [ ] 将网络错误、限流、首包/总响应超时、流中断、空响应、未知事件和 Schema/格式错误映射为结构化故障类别，并记录故障发生阶段与诊断标识
  - [ ] 闲聊故障时停止本次生成和 TTS，丢弃未完成回复且不写入正式历史；保持监听，只有用户明确要求后才用新的 `request_id` 重试
  - [ ] 分析、规划或等待授权时故障，将任务记为 `system_aborted`；拒绝部分或未通过 Schema 的输出，使计划与授权失效，继续时重新分析、规划、预检和授权
  - [ ] 本地执行期间仅允许冻结计划在不依赖 Agent 决策、且本地停止控制、安全校验和结果验证均完整可用时继续；否则阻止后续动作、安全处理当前原子动作、重新扫描并中止
  - [ ] 执行已完成但自然语言汇报失败时，以 `ResultVerifier`、`result.json` 和审计事实为准，使用本地正式模板展示，不重复执行电脑动作
  - [ ] 所有 Agent/API 故障均不自动重试、不自动遵循 `Retry-After`、不自动切换 CLI/Claude Code/Codex/其他模型或 Provider；敏感错误详情只在本地脱敏显示
  - [ ] 命令行仅作为诊断/测试通道：输入文字 → 得到小饼风格回复（验证 SDK 接入和人设生效）
- 产出：`llm/` 基础调用、会话生命周期管理、标识关联、结构化故障映射、错误停止机制和 persona 加载
- 验收：首次请求前没有模型会话；闲聊与工作上下文不串用；闲聊闲置满 30 分钟后销毁；工作任务结束后授权不可复用；模拟网络、限流、超时、流中断、空响应和格式错误时均不自动重试或切换；部分流式输出不能形成计划或授权；汇报失败不会重复执行已完成动作

## Agent receives RouteHandoff (2026-09-24)

The Agent is not the first receiver of an unrouted natural-language computer request. The boundary is:

```text
ConversationManager
-> IntentRouter
-> schema/trace/route-validated RouteHandoff
-> AgentAdapter / ToolRegistry / ActionPlan / Safety
```

`chat` enters the existing chat path; `query` is read-only; `task` and `dangerous` can only create a plan and enter preflight and authorization; `ambiguous` is clarified or safely rejected by the conversation layer; `unsupported` returns an unsupported explanation. Provider, confidence, and tool scope are untrusted hints, not local safety conclusions. The Agent must not freely reclassify after routing failure.
