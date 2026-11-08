# P5 批次 3 执行前复检完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P5 授权、执行、验证、审计与恢复  
> 状态：`P5 batch 3 complete`；已停在 P5 批次 4 Execution Ledger 门前

## 本批次唯一目标

在任何执行事实和真实副作用出现前，重新校验授权绑定、计划完整性、当前 Tool Manifest、Policy、路径快照、规范化参数、内容哈希和目标状态；只有全部复检成功后才最终原子消费授权。本批次不实现 Execution Ledger、真实执行器、`attempt_id`、文件或应用副作用、备份、取消、ResultVerifier、审计持久化或恢复。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P5-batch2-l3-consumption-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次请求本批次授权；
- 未改变既定工具清单、风险等级、Policy、路径边界、固定短语或阶段顺序；
- 当前交接后强制停止，不越过到 P5 批次 4。

## 新增与修改文件

新增：

- `app/safety/preflight.py`
- `tests/security/test_execution_preflight.py`

修改：

- `app/safety/confirmation/consumption.py`
- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`

## 执行前复检顺序

`ExecutionPreflightGate.check()` 固定执行：

1. 精确匹配当前实例、会话、operation、计划 ID/版本/hash、动作 ID 与顺序、授权有效期和授权风险等级；
2. 通过 `serialize_plan_json()` 验证计划结构和稳定哈希；
3. 逐动作重新读取当前 Tool Manifest，核对版本、内容哈希、风险、确认要求、验证和回滚声明；
4. 使用当前 `POLICY_VERSION` 和当前 `PolicyEngine` 复评；
5. 由注入的路径复检器重新验证路径存在性、目标/锚点/组件身份、resolved path 以及 Symlink/Junction/Reparse Point；
6. 重新校验写入内容哈希、字节数、源文件哈希、精确替换文本哈希和回收站完整路径清单；
7. 将冻结 JSON tuple 解冻为 JSON list 后，复用 `build_write_action_preview()` 重建参数与预览，并精确比较冻结事实；
8. 应用启动动作必须通过当前白名单解析器重新解析；解析器缺失、读取异常、应用不存在或登记名称变化均 fail closed；
9. 重新计算整份计划的 `target_state_hash`；
10. 以上全部成功后，才调用 `AuthorizationConsumptionStore.consume()`；
11. 返回 frozen/slotted 的 `ExecutionPreflightApproval`，它没有执行方法，不是 Execution Ledger 或执行记录。

## 整份授权失效与并发语义

- `AuthorizationConsumptionStore` 新增进程内 `_invalidated` 集合和 `invalidate()`；
- 任一执行前复检失败时，复检门尝试永久失效整份尚未消费授权；修复环境后不能复用旧授权，必须重新规划/确认；
- 消费检查顺序保持已消费优先于已失效，因此并发复检同一授权时恰好一个成功，其余返回 `authorization_replayed`；
- 新实例仍不认识旧实例授权；过期、篡改、错误会话/operation/计划/动作集合均不得进入后续阶段；
- 当前存储仍只是进程内授权门，不是账本、审计持久化或恢复实现。

## 哈希与目标状态边界

- `create_text_file`：按声明编码重算内容哈希与字节数，并要求目标继续为 absent；
- `write_text_file`：重算写入内容哈希与字节数，校验参数/预览中的 `expected_source_hash`，并通过注入读取器复检当前源文件哈希；`replace_exact` 额外校验目标文本哈希；
- `move_to_recycle_bin`：校验完整路径清单、数量和 `permanent_delete=False`；
- copy/move/rename 沿用既定 ActionPlan：由路径身份快照、参数/预览重建和整份 `target_state_hash` 共同覆盖；本批次没有擅自增加新的计划字段或改动既有契约。

## 公开 API 与循环导入边界

公开入口维持子模块：

```python
from app.safety.preflight import ExecutionPreflightApproval, ExecutionPreflightGate, PreflightError
```

不要从 `app.safety` 顶层重导出。既有依赖链 `app.plans.action_plan → app.safety.path_guard → app.safety.__init__` 会使顶层重导出 `preflight → app.plans` 形成循环导入风险。

## TDD 与验证

已观察的红灯：

1. 初始模块不存在：`ModuleNotFoundError: No module named 'app.safety.preflight'`；
2. 参数重建未实现时：`PreflightError not raised`；
3. 同 purpose 多路径复检暴露冻结 tuple 与计划生成器 JSON list 边界，得到 `parameter_revalidation_failed`；最小修复为重建前只解冻 JSON 容器；
4. 白名单解析器抛出 `OSError` 时曾逃逸结构化边界；最小修复为统一映射成 `parameter_revalidation_failed` 并使授权失效。

最终验证：

- execution preflight 专项：13/13；
- L2/L3、preflight、授权契约、核心模型、ActionPlan、PolicyEngine、PathGuard、write plans 聚焦：88/88；
- 全量 unittest：215/215；
- `compileall`：通过；
- 严格 UTF-8：80 个 Python 文件通过，无 `U+FFFD`；
- 公开子模块导入 smoke：通过；
- 精确 AST 审计：`FILE_PROCESS_NETWORK_EXECUTE_CALLS=[]`。

## 安全与 Git 事实

- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未创建或写入真实 `plan.json`、`execution_plan.md` 或 `result.json`；
- 未创建 Execution Ledger、`execution_id`、`attempt_id`、执行记录或审计记录；
- 未执行文件创建/写入/复制/移动/重命名/删除/回收站、应用启动、子进程或网络调用；
- 未实现备份、动作取消、ResultVerifier、审计持久化、恢复、P6、ASR 或 TTS；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，内容仍为 untracked 基线。

## 错误与修正记录

1. 最后一次补强后先运行专项测试，原有 10 项全部通过；随后补风险、多路径和应用白名单覆盖。
2. 第一次 PowerShell 大字符串测试补丁因引号解析在执行前失败，未产生项目写入；第二次嵌套 here-string 也在解析前失败。改用工作区内 UTF-8 Python 补丁脚本后成功。
3. 同 purpose 多路径红测发现冻结 tuple 未解冻，修复后专项 13/13。
4. 白名单解析器异常红测发现 `OSError` 逃逸，修复为结构化 fail closed。
5. 首次聚焦回归误写模块名 `tests.security.test_policy_engine` / `test_path_guard`，得到两个只读 ImportError；改用实际 `tests.safety.*` 后 88/88 通过。

## 下一批次入口：P5 批次 4

批次 4 只处理：**Execution Ledger：`execution_id/action_id/attempt_id/executor_instance_id/fencing_token`**。

最小读取集合：本文件、路线图 P5 卡片、批次 2 交接、`shared/action-plan.md`、`shared/safety-boundaries.md`、`shared/error-recovery.md`、P2 执行完整性契约、`app/contracts/execution.py`（若实际路径不同则读取唯一对应文件）、`app/safety/preflight.py`、授权消费存储及对应测试。

下一批次必须继续先写红测，至少覆盖：账本动作身份绑定、attempt 不可重绑、executor instance 与 fencing token、防旧实例写入、operation_started 的原子记录边界、重放/并发竞争、崩溃前后 `not_started/started/returned/completed/state_uncertain` 分类。批次 4 只能建立账本状态机与不可执行记录边界，不得顺手调用真实工具或实现文件/应用副作用、备份、取消、ResultVerifier、审计持久化、恢复或 P6。

## 交接后的强制停止点

当前停止在 `P5 batch 4` Execution Ledger 门前。不得继续实现账本、`attempt_id`、执行器、真实电脑动作、验证、审计或恢复；不得进入 P6。
