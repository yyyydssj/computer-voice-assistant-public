# P5 批次 2 L3 两阶段确认与防重放完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P5 授权、执行、验证、审计与恢复  
> 状态：`P5 batch 2 complete`；已停在 P5 批次 3 执行前重新校验门前

## 本批次唯一目标

严格接续批次 1 的 L2 模态确认和唯一 `AuthorizationSnapshot`，实现 L3 独立确认状态、固定完整短语“确认执行”、L2/L3 授权的一次性原子消费与进程重启防重放。本批次不实现执行前 PathGuard/参数/哈希复检编排、执行器、Execution Ledger、真实电脑动作、ResultVerifier、审计持久化或恢复。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P5-batch1-l2-confirmation-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次请求授权；
- 未改变既定工具清单、L2/L3 风险等级、Policy、路径边界、固定短语或阶段顺序；
- 按用户要求在上下文接近约 60% 时收口并停止，不进入批次 3。

## 新增与修改文件

新增：

- `app/safety/confirmation/l3.py`
- `app/safety/confirmation/consumption.py`
- `tests/security/test_l3_confirmation.py`
- `implementation_plan/checkpoints/2026-09-20-P5-batch2-l3-consumption-stop-handoff.md`

修改：

- `app/contracts/planning.py`
- `app/safety/confirmation/l2.py`
- `app/safety/confirmation/__init__.py`
- `tests/security/test_l2_confirmation.py`
- `tests/contracts/test_authorization.py`
- `tests/contracts/test_models.py`
- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`

## 已实现的 L3 两阶段行为

1. `L3ConfirmationController` 只接受全部动作为 L3、要求确认且 Policy 为 `require_confirmation` 的 `ReviewableActionPlan`；L2 或混合计划拒绝。
2. 第一阶段仍为固定居中模态语义，按钮固定为“允许执行”和“拒绝”。
3. 第一阶段 `allow` 只进入 `awaiting_phrase`，不生成授权；第二次 `allow` 和第一阶段提前提交短语均以阶段不匹配拒绝。
4. 第二阶段只接受逐字符完整匹配的固定短语“确认执行”。半句、同义词、前后空白、追加语气词和普通聊天肯定词均不授权，窗口保持在独立确认状态。
5. 正确短语到达时再次校验窗口、会话、计划 ID/版本/哈希、动作顺序、全部 L3 策略事实、目标状态和有效期。
6. 错误窗口、错误会话、过期窗口、旧计划、目标状态漂移、停止、拒绝、关闭和 Esc 均不产生授权。
7. 生成的 L3 快照固定为 `risk_level=L3`、`authorization_method=modal_and_phrase`、`confirmation_stage=phrase`、`confirmation_phrase=确认执行`，并保持 `consumed_at=None`。
8. 确认层仍无执行工具、文件副作用、网络、进程启动或持久化入口。

## `AuthorizationSnapshot` 补强

唯一授权模型新增并强制校验：

- `issuer_instance_id`：签发授权的运行实例；
- `operation_id`：本次具体操作边界；
- `confirmation_stage`：`none/modal/phrase`；
- `confirmation_phrase`：仅 L3 固定为“确认执行”，L2 必须为 `None`。

`is_valid_for()` 现在必须精确匹配实例、会话、操作、计划、目标状态、动作集合和时间。L2 固定为 `modal + modal + None`；L3 固定为 `modal_and_phrase + phrase + 确认执行`，不一致的反序列化快照被拒绝。

## 一次性消费与防重放

- 新增 `AuthorizationConsumptionStore`，由 L2/L3 控制器在当前进程实例内登记其签发的未消费授权；
- `consume()` 在单个互斥锁临界区内完成已知授权检查、实例检查、快照哈希检查、冻结事实检查、有效期检查和消费标记；
- 并发提交同一授权时恰好一个调用成功，其余返回 `authorization_replayed`；
- 同一授权不能二次消费，修改后的快照不能消费；
- 新建存储即代表重启后的新内存边界：旧授权未登记，返回 `authorization_unknown`；不同实例返回 `authorization_wrong_instance`；
- 当前存储仅为进程内消费门，不是 Execution Ledger、审计持久化或恢复实现。

## TDD 与验证

先写红测并观察到预期失败：L3/消费公开 API 不存在，既有授权契约缺少实例、操作和确认阶段字段。最小实现后：

- L2/L3 专项：19/19；
- 授权契约、核心模型与 L2/L3 聚焦：32/32；
- 全量 unittest：202/202；
- `compileall`：通过；
- 严格 UTF-8：78 个 Python 文件通过，无 `U+FFFD`；
- 公开导入与 L2/L3 共用消费存储 smoke：通过；
- 精确 AST 审计：`FILE_PROCESS_NETWORK_EXECUTE_CALLS=[]`。

## 安全与 Git 事实

- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未创建或写入真实 `plan.json`、`execution_plan.md` 或 `result.json`；
- 未执行文件创建/写入/复制/移动/重命名/删除/回收站、应用启动或子进程；
- 未实现执行前复检编排、执行器、账本、动作取消、结果验证、审计持久化或恢复；
- 未接入 Agent 工作主链、ASR、TTS 或 P6；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，内容仍为 untracked 基线。

## 错误与修正记录

1. 首次红测写入 PowerShell 命令因字符串引号解析在执行前失败，无文件改动；改用 UTF-8 Python 编辑脚本。
2. 首轮聚焦测试有一个旧 `is_valid_for()` 断言未传新增实例/操作字段；只修正测试调用后通过。
3. 初次 AST 粗筛把 `re.compile` 和 dataclass `replace` 误报为执行/文件替换；改为精确区分调用后审计为空。
4. 收口审查发现消费存储最初只由 L3 登记；随后让 L2/L3 共用同一类存储并增加 L2 单次消费回归。

## 下一批次入口：P5 批次 3

批次 3 只处理：**执行前重新校验路径、哈希、参数、计划版本和目标状态**。

最小读取集合：本文件、路线图 P5 卡片、批次 1 交接、`shared/action-plan.md`、`shared/safety-boundaries.md`、`shared/error-recovery.md`、`app/contracts/planning.py`、`app/safety/path_guard.py`、`app/safety/policy_engine.py`、`app/plans/action_plan.py`、`app/safety/confirmation/` 及对应测试。

下一批次必须继续先写红测，至少覆盖：

- 消费前当前实例/会话/operation/计划版本/plan_hash/动作集合精确匹配；
- 每个动作的 Tool Manifest、Policy 版本、规范化参数和路径快照复检；
- 文件或目录身份、存在性、哈希、目标状态、链接/Junction/父路径竞态变化；
- 任一动作复检失败时整份授权不可消费、不得创建执行事实；
- 复检成功与授权消费必须有明确顺序，失败不得留下“已执行”或可复用授权假象；
- 重复、迟到和旧实例请求不能进入后续执行阶段。

不得在批次 3 顺手实现真实执行器、Execution Ledger、动作副作用、备份、ResultVerifier、审计持久化或恢复。

## 交接后的强制停止点

当前停止在 `P5 batch 3` 门前。不得继续实现执行器、账本、真实电脑动作、验证、审计或恢复；不得进入 P6。
