# P5 批次 4 Execution Ledger 完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P5 授权、执行、验证、审计与恢复  
> 状态：`P5 batch 4 complete`；已停在 P5 批次 5 原子动作边界、协作取消和不可安全中断声明门前

## 本批次唯一目标

建立不可执行、仅进程内的 Execution Ledger 状态机，绑定 `execution_id/action_id/attempt_id/executor_instance_id/fencing_token`，在锁内原子记录 `operation_started`，拒绝授权重放、ID 冲突、attempt 重绑和旧 fencing token 写入，并明确 `not_started/started/returned/completed/state_uncertain` 分类。本批次不实现真实动作、文件或应用副作用、备份、取消、ResultVerifier、审计持久化或恢复。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P5-batch3-execution-preflight-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次申请授权；
- 未改变既定工具清单、风险等级、Policy、路径边界、固定短语或阶段顺序；
- 当前交接后强制停止，不越过到 P5 批次 5。

## 新增与修改文件

新增：

- `app/executor/ledger.py`
- `app/executor/__init__.py`
- `tests/security/test_execution_ledger.py`

修改：

- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`

## 创建门与重放边界

`ExecutionLedgerRegistry` 绑定当前进程 `instance_id`，并在一把锁内完成以下检查与登记：

1. approval 必须是已通过 preflight、授权已消费的 `ExecutionPreflightApproval`；
2. approval 必须属于当前进程实例；
3. ledger `created_at` 不得早于 `checked_at`；
4. 同一 `authorization_id` 只能创建一个 execution；顺序或并发重放均返回 `authorization_replayed`；
5. 同一 `execution_id` 不得绑定另一份 approval，返回 `execution_id_collision`；
6. ledger 自动绑定 preflight 的 plan ID、版本、hash、授权快照 hash 和精确动作集合。

当前 registry 仅为内存对象；进程重启后不具备 durable 去重能力。

## Attempt 与 fencing 边界

- `acquire_executor()` 每次在账本内签发单调的 `fence-1`、`fence-2`……；
- 新 token 立即使旧句柄失去写入资格；
- attempt 还永久绑定创建时的 executor ID 与 fencing token；即使复用相同 executor ID，新 token 也不能接管旧 attempt；
- `attempt_id` 不能重绑其他动作或执行器；同一 attempt 的并发开始恰好一个成功；
- 同一动作一旦存在 started/returned/completed/state_uncertain 事实，不能以另一 attempt 隐式重开；
- `operation_started` 的检查和记录在同一锁内完成，拒绝不会留下部分记录；
- 未授权动作不能创建任何 attempt 事实。

这里的 fencing token 只是单进程账本中的单调序列，不是跨进程锁、租约或持久化 fencing 机制。

## 状态分类与迁移

固定分类：

```text
not_started → started → returned → completed
                   └────────────→ state_uncertain
```

- `completed` 结果必须在 `operation_returned` 后记录，不能从 started 直接跳过 returned；
- `finished_at` 不得早于 `started_at`，`completed_at` 不得早于已有 attempt 时间；
- 全部授权动作均为 completed 后，ledger 才进入 `COMPLETED`；
- 任一 `state_uncertain` 使 ledger 进入 `UNCERTAIN`，并禁止隐式重开；
- 分类只是账本已观察事实，不代表恢复、重试、回滚或真实电脑状态判断；
- frozen/slotted 快照及嵌套契约不能被调用方改写。

## 不可执行边界

- `ExecutionLedgerStore` 与 `LedgerExecutor` 均无 `execute()`；
- 无工具调用、文件读写/复制/移动/删除、回收站、应用启动、子进程或网络入口；
- 不写真实 `plan.json`、`execution_plan.md`、`result.json` 或 ledger 文件；
- 不实现 durable transaction、WAL、数据库、审计追加或恢复扫描。

## TDD 与验证

已观察的红灯与修正：

1. 初始模块不存在：`ModuleNotFoundError: No module named 'app.executor'`；
2. 测试要求从 preflight 创建账本时：`ExecutionLedgerStore.create` 缺失，随后收敛为原子 `ExecutionLedgerRegistry.create()`；
3. 相同 `executor_instance_id` 用新 token 接管旧 attempt 时曾未抛错，最小修复为 attempt 永久绑定原始 fencing token；
4. registry 与状态迁移补强后，`state_uncertain` 动作重开最初返回一般 `ledger_terminal`；调整检查顺序后保留具体 `state_uncertain` 拒绝。

最终验证：

- Execution Ledger 专项：21/21；
- P2 执行完整性 + P5 L2/L3/preflight/ledger 聚焦：62/62；
- 全量 unittest：236/236；
- `compileall`：通过；
- 严格 UTF-8：83 个 Python 文件通过，无 `U+FFFD`；
- 公开导入 smoke：通过；
- `app/executor/` 精确 AST 审计：`FILE_PROCESS_NETWORK_EXECUTE_CALLS=[]`。

验证环境说明：项目 `.venv` 仍指向已不存在的 `<USER_HOME>\AppData\Local\Programs\Python\Python312\python.exe`，因此无法启动；本批次使用仓库内 `.python312\python.exe`，并在测试进程内显式插入仓库根。未修改虚拟环境或系统配置。

## 安全与 Git 事实

- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未执行文件创建/写入/复制/移动/重命名/删除/回收站、应用启动、子进程或网络调用；
- 未实现真实执行器、备份、取消、ResultVerifier、审计持久化、恢复、P6、ASR 或 TTS；
- ledger 仍仅进程内，fencing token 仍仅进程内单调序列；
- 没有 durable transaction/WAL，也没有真实持久化的 `operation_started`；
- 未执行 `git add`、`git commit` 或 `git push`；仓库 `main` 仍无首次 commit，项目内容仍为 untracked 基线。

## 下一批次入口：P5 批次 5

批次 5 只处理：**原子动作边界、协作取消和不可安全中断声明**。

下一批次必须先读取本文件、路线图 P5 卡片、P2 执行完整性契约、`shared/action-plan.md`、`shared/safety-boundaries.md`、`shared/error-recovery.md`、`app/executor/ledger.py` 及对应测试；继续拒绝路径优先和 TDD。

批次 5 可以定义动作在开始前、可安全中断点和不可安全中断区间内的取消事实与状态边界，但不得误认为当前账本已经可以执行真实动作；不得顺手实现文件/应用副作用、备份、ResultVerifier、审计持久化、恢复或 P6。

## 交接后的强制停止点

当前停止在 `P5 batch 5` 原子动作边界、协作取消和不可安全中断声明门前。不得继续实现取消语义、真实电脑动作、验证、审计或恢复；不得进入 P6。