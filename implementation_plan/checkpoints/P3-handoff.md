# P3 文字聊天与 Harness 会话完成交接

> 交接日期：2026-09-19  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P3  
> 阶段状态：`complete`  
> 当前停止点：P4 授权门前

## 本阶段唯一目标

用键盘完成自然文字闲聊和最小 Agent 请求，验证 persona、会话隔离、请求标识、流式完整性以及 Agent/API 故障停止规则。

## 本阶段明确不做

- 不暴露或调用电脑工具；
- 不生成、授权或执行 ActionPlan；
- 不接入 ASR、TTS 或语音链路；
- 不把 P3 组件接入主界面形成可执行电脑任务；
- 不自动重试、不遵循 Retry-After 自动重发、不自动切换 Agent、模型或 Provider；
- 不进入 P4。

## 进入条件

- P0、P1、P2 均已完成并有 handoff；
- 用户已明确授权 P3 及本次批次 5；
- P3 批次 1-4 的 adapter、会话、request/流式完整性和 persona 边界均已通过回归。

## 允许修改的文件范围

```text
app/agent/
app/conversation/
app/persona/
tests/agent/
tests/conversation/
implementation_plan/checkpoints/
task_plan.md
progress.md
implementation_plan/README.md
implementation_plan/implementation-roadmap.md
```

## 依赖的稳定契约

- `RequestSubmissionGate` 保证同一 `request_id` 只提交一次；
- 闲聊与工作会话用不同 `session_id`，工作会话按 `task_id` 隔离；
- 只有非空文本且有有效终止原因的完整结果可进入正式历史；
- persona 只从用户权威文件读取，Agent 无写回能力；
- Agent/API 故障不得自动重试或自动换 Agent。

## 实现批次

1. DeepSeekHarness adapter、延迟 runtime、请求防重提和 no-tools 安全补丁；
2. 闲聊/工作会话容器、上下文隔离和 30 分钟闲聊闲置销毁；
3. `request_id` 生成、流式文本拼接和完整性判定；
4. persona 全文读取、版本标识和用户修改边界；
5. 连接、限流、超时、流中断、空响应、未知事件和 Schema 错误映射及全量回归。

## 批次 5 实际完成

- `app/agent/adapter.py`：
  - 将 runtime 创建/初始化阶段的连接、限流、超时、未知事件和 Schema 故障映射为结构化 `AgentFailure`，并明确标记 `stage="initialize"`；
  - 保留 `HarnessConfigurationError` 为本地配置硬错误，不降级成普通 Agent 故障；
  - 对 runtime 结果的 `events`、`notifications`、文本和终止原因执行拒绝优先结构校验；
  - 请求阶段继续映射连接、429、超时、传输关闭、SDK 协议错误和 JSON-RPC Schema 错误；
  - 所有故障结果默认禁止自动重试和自动切换 Agent。
- `app/conversation/manager.py`：
  - 拒绝 request/session 身份与当前提交不一致的 outcome；
  - 拒绝“完成但携带 failure”或“失败但没有 failure”的矛盾 outcome；
  - 保留流式部分文本仅用于当前结果观察，不写入正式历史；
  - 工作会话发生 Agent 故障后仍关闭为 `agent_failure`，不重试。
- `tests/agent/test_adapter.py`、`tests/conversation/test_manager.py`：
  - 新增初始化阶段故障注入、配置硬错误、畸形 runtime 结果、流式故障保留类别、身份错配拒绝等测试。

## 测试与故障注入

- Agent adapter 专项：11/11 通过；
- Conversation 专项：19/19 通过；
- 全量 unittest：107/107 通过；
- `python -m compileall -q app tests`：通过；
- 严格 UTF-8：50 个 Python 文件通过，无替换字符；
- 官方 SDK `0.1.5rc1` 异常类映射 smoke：`TransportClosedError`、`SdkProtocolError`、`JsonRpcError(429)`、`JsonRpcError(-32602)` 全部得到预期类别；
- 所有故障注入均只调用一次 factory 或 runtime，没有隐式第二次提交。

## 退出条件（可观察事实）

| 退出条件 | 证据 | 结果 |
|---|---|---|
| 首次请求前不创建模型会话 | adapter 延迟创建测试 | 通过 |
| 闲聊与工作上下文不串 | 不同 `session_id`、按 `task_id` 隔离测试 | 通过 |
| 不完整输出不进入正式历史 | 空响应、缺终止、流中断、非法片段和故障流测试 | 通过 |
| Agent 故障不自动重试/换 Agent | factory/runtime 调用次数均为 1；failure 默认字段为 false | 通过 |
| 只有新用户请求才产生新 request_id | 同 ID 防重提及会话生成测试 | 通过 |
| persona 为用户权威只读版本 | 全文、SHA-256、不可变与显式重载测试 | 通过 |
| 七类错误均有结构化映射 | adapter 和 conversation 故障注入 | 通过 |

## 已知风险

- 当前 P3 仍是纯文字底层组件，尚未接入 `app/main.py` 的真实交互组装；该接线不能绕过 P4/P5 的工具、策略、授权和执行边界。
- 官方 runtime 的真实联网 Provider 行为仍依赖用户环境和凭据；本批次未读取 `.env`、未发送真实 API 请求。
- `.venv` 仍引用不可用的外部解释器；验证继续使用项目内 `.python312\python.exe`，并显式把项目根加入 `sys.path`。
- 仓库尚无首次 commit，当前文件仍处于 untracked 基线。

## 未解决但明确延期的问题

- ToolRegistry、工具 Manifest、PathGuard、PolicyEngine 和 ActionPlan：延期到 P4；
- 授权、执行、验证、审计与恢复：延期到 P5；
- ASR、TTS、按键语音闭环及连续对话：延期到 P6-P10；
- 发布、长期运行和 Skill 收口：延期到 P11。

## 下一阶段入口

P4 的唯一目标是建立结构化工具、路径安全、策略和 ActionPlan，使 Agent 只能提出可审查计划，不能直接执行命令。

## 下一阶段禁止提前做的事项

- 未经用户明确授权不得进入 P4；
- 不得提前实现授权弹窗或执行器；
- 不得开放任意 PowerShell、Python、Shell、管理员权限或动态 Skill；
- 不得接入 ASR、TTS 或语音链路；
- 不得用自然语言工具输出直接驱动下一步参数。

## 需要读取的最小文件集合

下一次仅在用户授权 P4 后读取：

1. `implementation_plan/checkpoints/P3-handoff.md`；
2. `implementation_plan/implementation-roadmap.md` 的 P4 卡片；
3. P4 卡片列出的最小共享契约；
4. `task_plan.md` 最新状态。

## 安全与 Git 事实

- 未修改 `implementation_plan/persona.md`；
- 未读取 `.env`，未启动官方 runtime，未发送真实 API 请求；
- 未接入工具、ActionPlan、ASR、TTS、语音链路或 P4；
- 未执行 `git add`、`git commit` 或 `git push`；
- 当前分支为 `main`，仓库无首次 commit。
