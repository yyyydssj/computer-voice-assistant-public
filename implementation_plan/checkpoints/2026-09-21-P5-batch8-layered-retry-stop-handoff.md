# P5 批次 8：分层重试策略停止交接

- 日期：2026-09-21
- 阶段：P5 授权、执行、验证、审计与恢复
- 批次状态：**complete**
- 强制边界：本交接之后不得进入 P6、ASR、TTS、Skill、UI 或任意命令/PowerShell/Shell/解释器入口。

## 1. 接续与范围

严格接续：`implementation_plan/checkpoints/2026-09-21-P5-batch7-audit-recovery-stop-handoff.md`。用户已明确允许直接进入下一批次，无须再次授权。

本批次只完成分层重试策略：

1. 只读查询的有限、有上限重试；
2. Agent 生成失败时禁止复用原 `request_id`，重试必须创建新的 request；
3. 副作用动作默认不得自动重试；未知结果必须先进入只读扫描决策；
4. `state_uncertain` 保持终止阻断，不得改写为成功、失败或可重试；
5. 对应拒绝路径、边界、回归、编码和执行面审计。

明确未做：自动回滚、自动续跑、真实 Windows 回收站、真实应用启动、P6、ASR、TTS、Skill、UI、任意命令/PowerShell/Shell/解释器入口。

## 2. 已完成实现

### 2.1 分层策略契约

新增 `app/retry/policy.py` 与 `app/retry/__init__.py`，公开：

- `RetryLayer`：`read_only_query`、`agent_generation`、`side_effect_action` 三层；
- `RetryDisposition`：有限重试、新 request 要求、禁止重试、扫描前决策和 `state_uncertain` 阻断；
- `RetryPolicy`：只输出策略决策，不执行网络、等待、扫描或副作用动作；
- `ReadOnlyRetryRunner`：只接受注入的只读查询和等待函数，按固定上限执行；
- `RetryableReadOnlyError`：只有调用方明确标记的只读瞬态故障才允许进入有限重试；普通异常原样失败，不自动重试。

### 2.2 关键安全语义

- 只读查询默认最多 3 次尝试，指数等待有明确上限；策略不允许无限重试；
- Agent 生成的 `new_request_required` 决策可先返回阻断事实；实际替代请求必须显式提供与原 request 不同的新 `request_id`；
- 副作用状态 `completed`、`not_completed` 均返回 `no_retry`；`unknown_result` 返回 `scan_before_decision`；`state_uncertain` 返回 `blocked_state_uncertain`；
- 跨层事实组合会被拒绝，避免用只读/Agent 层策略替代副作用安全链；
- retry 模块不拥有执行、扫描、网络、进程、Shell 或解释器入口。

### 2.3 测试

新增 `tests/retry/test_policy.py`，覆盖：

- 只读重试次数上限、指数等待封顶和非瞬态拒绝；
- Agent 原 request 禁止复用、新 request 要求及相同 request 拒绝；
- 副作用默认不重试、未知结果先扫描和 `state_uncertain` 终止阻断；
- 跨层事实拒绝；
- 普通只读异常不重试；
- 只读 runner 的硬上限。

## 3. 验证与实际执行事实

已通过：

- P5 批次 8 专项：9/9；
- 全量 unittest：272/272；
- `compileall app tests`：通过；
- retry 模块 AST 执行面审计：`EXECUTION_CALLS=[]`；
- 严格 UTF-8 检查：102 个 Python 文件中无 `U+FFFD`，但发现既有 BOM 文件 2 个：
  - `tests/recovery/test_recovery_scanner.py`
  - `tests/security/test_audit_persistence.py`

BOM 只读发现未在本批次修复，因为它们不属于批次 8 的最小文件范围；不得把该既有编码债务误记为批次 8 新增错误。

测试和审计均未：

- 读取 `.env` 或发送真实 API 请求；
- 启动正式 Harness runtime；
- 调用真实 Windows 回收站或启动真实应用；
- 执行真实电脑副作用；
- 执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit。

## 4. 已观察错误与修复

| 错误 | 尝试次数 | 解决方案 |
|---|---:|---|
| 首次专项启动器未显式加入项目根，无法导入 `tests`/`app` | 2 | 使用项目根显式加入 `sys.path` 的标准 unittest runner；未改环境配置 |
| Agent `new_request_required` 阻断决策初次被错误要求必须立即携带替代 request | 1 | 允许先返回阻断事实；实际替代 request 仍必须不同于原 request |
| 只读 runner 初次将普通异常视为瞬态并重试 | 1 | 新增 `RetryableReadOnlyError`，仅明确标记瞬态故障可重试 |
| 未开始与未知副作用结果初次共用扫描分支 | 1 | 增加 `UNKNOWN_RESULT`，`NOT_STARTED` 保持已知事实并禁止自动重试 |
| 读取核对命令误插入重复状态枚举成员 | 1 | 立即清理重复成员并重跑专项与全量回归 |

## 5. 当前未解决/延期

- `RetryPolicy` 尚未接入 `ControlledExecutionRunner`、`RecoveryScanner` 或真实 Agent 调度编排；本批次只提供可注入、不可越权的策略原语；
- 只读查询是否属于瞬态故障仍由上层调用方通过 `RetryableReadOnlyError` 明确标记，不做自动猜测；
- Agent 新 request 的生成和用户重新要求仍由上层会话/请求生命周期负责；策略本身不创建 request；
- 副作用未知结果的真实扫描适配器仍未接入；`scan_before_decision` 不是执行许可；
- 项目既有 2 个 BOM 文件未在本批次范围内修复；
- P5 尚未完成阶段级跨阶段集成验收，因此不得标记 P5 complete 或进入 P6。

## 6. 下一入口与强制停止

下一步只允许进行 **P5 阶段级集成验收、全量回归和安全审计收口**，确认批次 1-8 与 P1-P4 前置链路之间的身份、授权、执行、验证、审计、恢复和重试边界没有冲突。

在该验收通过并生成新的阶段 handoff 之前：

- 不进入 P6；
- 不接入 ASR、TTS、Skill 或 UI；
- 不实现自动回滚、自动续跑或真实副作用恢复；
- 不新增任意命令、PowerShell、Shell、解释器、网络或进程入口；
- 若无法确认真实执行状态、无法追加审计或发现计划/授权/目标哈希/Manifest 不一致，立即停止并记录为 `blocked/system_aborted`。