# P5 批次 7：审计哈希链、脱敏、恢复扫描与恢复报告停止交接

- 日期：2026-09-21
- 阶段：P5 授权、执行、验证、审计与恢复
- 批次状态：**complete**
- 下一入口：P5 批次 8 分层重试
- 强制边界：本交接之后不得进入 P6、ASR、TTS、Skill、UI 或任意命令执行入口。

## 1. 接续与范围

严格接续：`implementation_plan/checkpoints/2026-09-21-P5-batch6-backup-execution-verification-stop-handoff.md`。用户已明确允许直接进入下一批次，无须再次授权。

本批次只完成：

1. 审计事件的脱敏与哈希链持久化；
2. 审计追加/读取失败的 fail-closed 语义；
3. 基于当前真实观察的恢复扫描；
4. 遗留计划目录的只读发现与恢复报告；
5. 对应安全测试、全量回归、UTF-8 和执行面审计。

明确未做：分层重试、自动回滚、自动续跑、真实 Windows 回收站、真实应用启动、P6、ASR、TTS、Skill、UI、任意命令/PowerShell/Shell/解释器入口。

## 2. 已完成实现

### 2.1 审计契约与哈希链

- `app/contracts/execution.py` 的 `AuditEvent` 增加结构化 `details` 字段，字段参与既有规范 JSON 与 `event_hash` 计算；兼容原有无详情事件。
- 既有 `seal_audit_event()` / `validate_audit_chain()` 继续负责前序哈希、序号连续性与内容完整性校验。

### 2.2 审计持久化

新增 `app/auditing/store.py` 与 `app/auditing/__init__.py`，公开 `AuditStore`、`AuditPersistenceError`：

- JSONL 追加存储，路径由调用方注入；单实例锁保护序号和前序哈希；重新加载前验证完整链；
- 写入后 `flush + fsync`；写入/读取/解析/链校验失败均抛出稳定 `AuditPersistenceError`，调用方必须 fail-closed；
- 审计文本按字段名和值进行最小脱敏：secret/token/key/authorization 等记为 `[REDACTED]`，路径记为 `[PATH_OMITTED]`，正文/音频等记为 `[OMITTED]`；不持久化原始路径、密钥、文件内容或未分类对象原文；
- 不提供任何执行、网络、Shell、PowerShell、解释器或进程入口。

### 2.3 恢复扫描与报告

新增 `app/recovery/scanner.py` 与 `app/recovery/__init__.py`，公开 `RecoveryAction`、`RecoveryReport`、`RecoveryScanner`：

- 审计提示只作线索；每个动作必须由注入的只读 `observe()` 重新观察当前真实状态；
- 只允许 `completed`、`not_completed`、`state_uncertain` 三类；观察异常、证据无效或无法可靠判断统一为 `state_uncertain`；
- `RecoveryReport.auto_resume_allowed` 固定为 `False`；报告明确旧计划/旧授权不自动续跑、已完成不自动回滚、未完成不自动补做，继续前必须重新规划、预检并授权；
- `scan_legacy_plan_root()` 只读发现存在 `plan.json` 但缺少 `result.json` 的遗留版本，不读取计划正文、不写 `result.json`、不执行任何动作。

## 3. 测试与实际执行事实

已通过：

- P5 批次 7 专项：15/15；
- 其中既有审计契约/哈希链回归：9/9；审计持久化：3/3；恢复扫描：3/3；
- 全量 unittest：263/263；
- `compileall`：通过；
- 严格 UTF-8：98 个 Python 文件通过，无 BOM、无 `U+FFFD`；
- `app/auditing/*.py` 与 `app/recovery/*.py` AST 执行面审计：`EXECUTION_CALLS=[]`。

测试事实：

- 审计测试在临时目录写入 JSONL 并验证真实 reload、哈希链、篡改拒绝与 `fsync` 失败后的 fail-closed；
- 恢复测试仅使用临时目录和注入式只读观察函数；未执行任何真实电脑副作用；
- 未读取 `.env`，未发送真实 API 请求，未启动正式 Harness runtime，未调用真实 Windows 回收站，未启动真实应用；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit。

## 4. 已观察错误与修复

| 错误 | 尝试次数 | 解决方案 |
|---|---:|---|
| 审计默认时间使用 `+00:00`，不符合现有 UTC `Z` 契约 | 1 | 默认时间规范化为 `Z` 后缀，并重跑专项 |
| `AuditEvent` 初次构造缺少结构化详情字段 | 1 | 在契约中增加 `details`，纳入稳定序列化和哈希计算 |
| 契约字段插入时误影响既有 `subject_id` 位置 | 1 | 精确恢复 `ExecutionLedger` 与 `AuditEvent` 字段边界后重跑既有 9 项审计契约回归 |

## 5. 当前未解决/延期

- 当前审计文件仍是本地 JSONL，不是跨进程 durable WAL；未实现轮转、压缩、跨文件链锚定或崩溃恢复事务扫描；
- `AuditStore` 已提供 fail-closed 原语，但尚未接入 `ControlledExecutionRunner` 的每个执行阶段事件；完整执行审计接线与 P5 批次 8 之前的进一步集成仍需后续独立批次确认；
- `RecoveryScanner` 已提供只读分类和报告模型，但尚未接入真实 ResultVerifier/ToolRegistry 扫描适配器；不允许据此自动执行或自动续跑；
- 真实 Windows 适配器仍未在本轮运行。

## 6. 下一批次入口与强制停止

下一次只允许读取并实现 P5 批次 8 所需的最小文件集合，重点是分层重试策略：

- 只读查询的有限、有上限重试；
- Agent 生成必须创建新的 request；
- 副作用动作默认不得自动重试；未知结果先做只读扫描；
- `state_uncertain` 不得改写为成功、失败或可重试。

不得在下一次接续中顺手实现自动回滚、P6、ASR、TTS、Skill、UI 或任意命令入口。若无法确认真实执行状态、无法追加审计或发现计划/授权/目标哈希/Manifest 不一致，应立即停止并记录为 `blocked/system_aborted`。