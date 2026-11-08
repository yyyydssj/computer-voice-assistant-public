# P5 批次 5 原子动作与协作取消完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段：P5 授权、执行、验证、审计与恢复  
> 状态：`P5 batch 5 complete`；已停在 P5 批次 6 备份、执行、ResultVerifier、部分成功与 `state_uncertain` 门前

## 本批次唯一目标

在不执行任何真实工具的前提下，建立动作顺序、原子开始/停止竞争、协作取消、不可安全中断区间和不可强杀声明。取消只能依据执行前已冻结的动作边界，不得把“请求停止”误报为“动作已经停止”，也不得把不可安全中断动作强杀成未知的部分写入。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P5-batch4-execution-ledger-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次申请授权；
- 未改变既定工具清单、风险等级、Policy、路径边界、固定短语或阶段顺序；
- 本批次完成后强制停止，不越过到 P5 批次 6 的实现。

## 新增与修改文件

新增：

- `tests/security/test_execution_cancellation.py`

修改：

- `app/safety/preflight.py`
- `app/executor/ledger.py`
- `app/executor/__init__.py`
- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`

## Preflight 动作边界冻结

`PreflightActionBoundary` 与 `ExecutionPreflightApproval.action_boundaries` 对每个授权动作冻结：

- `action_id`；
- 当前 `tool_name`；
- 当前 Tool Manifest hash；
- `cooperative` 或 `not_supported` 取消能力声明。

边界集合的顺序必须与 `action_ids` 完全一致，账本只使用这份 preflight 快照，不在停止期间重新推断工具能力。为兼容旧测试，手工构造且未提供边界的旧 approval 使用 `legacy_unbound` / `not_supported` 默认边界；新的 preflight 通过路径会写入实际 Manifest 事实。

## 开始与停止的原子边界

- 动作只能严格按冻结计划顺序开始，前序动作必须已完成；
- `request_stop()` 与 `operation_started()` 共用同一把锁，竞态只产生一个原子事实：要么先开始后停止，要么先停止后阻止开始；
- 一旦记录停止请求，不得签发新的 executor，也不得开始后续动作；
- 停止前的无效时间、非法原因和越权请求在写入控制事实前拒绝，不留下部分停止记录；
- 用户停止/取消在当前动作返回后账本进入 `CANCELLED`；系统中止进入 `ABORTED`。

## 协作取消与不可安全中断

- `cooperative` 动作收到停止请求后标记 `CANCEL_REQUESTED`，由动作自身在安全点返回；停止请求不是已返回事实；
- 进入不可安全中断区间后不强杀动作；停止请求记录为 `DEFERRED_UNSAFE_ATOMIC`，动作仍保持 `STARTED`；
- 离开不可安全区间后，协作动作才转换为 `CANCEL_REQUESTED`；
- `not_supported` 动作不伪造取消，不强制修改动作状态，使用 `WAIT_FOR_ATOMIC_RETURN` 等待原子动作返回；
- stale fencing token 不能改变动作的不可安全中断边界。

## 不可执行边界

- `ExecutionLedgerStore` 与 `LedgerExecutor` 仍没有 `execute()`；
- 无工具调用、文件读写/复制/移动/删除、回收站、应用启动、子进程或网络入口；
- 不写真实 `plan.json`、`execution_plan.md`、`result.json` 或账本文件；
- 不实现 durable transaction、WAL、数据库、审计追加、备份、ResultVerifier、恢复扫描或恢复报告。

## TDD 与验证

已观察并修正的错误：

1. embed Python 直接按 `tests.security...` 运行失败，因测试目录导入方式与外部 `PYTHONPATH` 不适配；改用进程内显式插入仓库根并用 discover 构造 suite；
2. 第一次最小回归修补误将 `attempt_id` 重绑检查插入 `acquire_executor()`，随后又发现相同逻辑误留在 `classify()`；均已删除，检查只保留在 `_start()` 锁内；
3. 修正后取消、ledger、preflight 和全量测试均通过。

最终验证：

- 取消专项：10/10；
- Execution Ledger 专项：21/21；
- preflight 专项：13/13；
- P5 security 聚焦：72/72；
- 全量 unittest：246/246；
- `compileall`：通过；
- 严格 UTF-8：84 个 Python 文件通过，无 `U+FFFD`；
- 公开导入 smoke：通过；
- `app/executor/` 精确 AST 审计：`FILE_PROCESS_NETWORK_EXECUTE_CALLS=[]`。

## 安全与 Git 事实

- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未执行文件创建/写入/复制/移动/重命名/删除/回收站、应用启动、子进程或网络调用；
- 未实现真实执行器、备份、ResultVerifier、审计持久化、恢复、P6、ASR 或 TTS；
- ledger、fencing token、停止请求和原子边界仍仅为进程内内存事实；没有 durable transaction/WAL 或真实持久化的 `operation_started`；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit。

## 下一批次入口：P5 批次 6

批次 6 只处理：**备份、真实执行边界、ResultVerifier、部分成功和 `state_uncertain` 结果分类**。下一批次必须先读取本 handoff、P5 路线图、`shared/action-plan.md`、`shared/safety-boundaries.md`、`shared/error-recovery.md`、相关 contracts、preflight、ledger 及对应测试；继续拒绝路径优先和 TDD。除非后续范围明确允许，不得顺手实现审计持久化、恢复扫描、分层重试、P6、ASR 或 TTS。

## 交接后的强制停止点

当前停止在 `P5 batch 6` 备份、执行、验证、部分成功和 `state_uncertain` 门前。不得继续实现批次 6；等待下一次会话严格读取本文件后再接续。不得进入 P6。
