# P5 批次 1 L2 模态确认完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P5 授权、执行、验证、审计与恢复  
> 状态：`P5 batch 1 complete`；已停在 P5 批次 2 L3 独立确认、固定短语、一次性消费与防重放门前

## 本批次唯一目标

复用 P2 唯一的 `AuthorizationSnapshot`，在文字模式下建立不可执行的 L2 正式模态确认状态。只有固定本地“允许执行”事件到达，且当前计划与目标状态仍和窗口冻结事实精确一致时，才生成尚未消费的授权快照。本批次不实现 L3、授权消费、执行前复检、执行器、账本、验证器、审计持久化或恢复。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/P4-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次请求本批次授权；
- 未改变既定工具清单、风险等级、Policy、路径边界、冲突策略或阶段顺序；
- 当前交接后停止，不越过到 P5 批次 2。

## 新增与修改文件

新增：

- `app/safety/confirmation/l2.py`
- `app/safety/confirmation/__init__.py`
- `tests/security/__init__.py`
- `tests/security/test_l2_confirmation.py`

修改：

- `app/contracts/planning.py`
- `tests/contracts/test_authorization.py`
- `tests/contracts/test_models.py`
- `task_plan.md`
- `progress.md`

## 公开 API 与循环导入边界

确认 API 只从 `app.safety.confirmation` 公开：`ConfirmationError`、`ConfirmationActionSummary`、`L2ConfirmationPrompt`、`L2ConfirmationWindow`、`ConfirmationResolution`、`L2ConfirmationController`。

不要重导出到 `app.safety` 顶层。既有依赖链 `app.plans.action_plan → app.safety.path_guard → app.safety.confirmation → app.plans` 会形成循环导入。

## 已实现的安全行为

1. 只接受 `ReviewableActionPlan`。
2. 所有动作必须均为 L2、要求确认，且 Policy 决策为 `require_confirmation`；L1/L3/deny/混合计划被拒绝。
3. 提示固定为模态窗口，按钮固定为“允许执行”和“拒绝”。
4. 摘要、提示、窗口和结果为 frozen/slotted dataclass，参数与预览深度冻结。
5. 单控制器同一时刻只允许一个活动窗口。
6. 只有固定本地 `allow` 才可能生成授权；“可以”“好”等自然语言和未知事件不授权，也不关闭窗口。
7. `deny`、`close`、`escape`、`stop` 和过期均不生成授权。
8. `allow` 前必须提供当前计划和当前目标状态哈希。
9. 计划 ID/版本/哈希、动作 ID/顺序或目标状态变化会使窗口失效。
10. 错误会话、错误窗口、早到、提前 timeout 和迟到事件被拒绝或按过期处理。
11. 已出现过的 `confirmation_window_id` 和 `authorization_id` 在控制器实例内不得复用。
12. 成功快照保持 `consumed_at=None`；确认层无执行、消费、持久化、网络或进程启动入口。

## `AuthorizationSnapshot.session_id` 补强

正式规划要求授权绑定会话。本批次按 TDD 完成：

- 新增必填 `session_id: str`，由 `require_identifier()` 校验；
- `is_valid_for()` 必须提供并精确匹配当前 `session_id`；
- `stable_hash()` 通过既有 `to_dict()` 自动覆盖该字段；
- L2 创建快照时写入活动窗口的 `window.session_id`；
- 没有新增第二套授权模型，也没有扩展消费逻辑。

## TDD 与验证

L2 专项 11 项覆盖正式提示、显式允许、拒绝/关闭/Esc/停止/过期、自然语言拒绝、计划和目标漂移、风险混入、单活动窗口、旧事件、ID 防重用、错误会话和时间边界。

会话契约红灯为 23 项中 7 项错误，全部精准来自契约缺失 `session_id` 或 L2 未写入该字段；最小实现后结果如下：

- L2 专项：11/11；
- 授权契约、核心模型与 L2 聚焦：23/23；
- P4 ActionPlan、ResultDocument、PolicyEngine 与 L2 回归：45/45；
- 全量 unittest：193/193；
- `compileall`：通过；
- 严格 UTF-8：75 个 Python 文件通过，无 `U+FFFD`；
- 精确 AST 审计：`FILE_PROCESS_NETWORK_CONSUME_EXECUTE_CALLS=[]`。

## 安全与 Git 事实

- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未创建或写入真实 `plan.json`、`execution_plan.md` 或 `result.json`；
- 未执行文件副作用、应用启动或子进程；
- 未实现 L3、授权消费、执行前复检、执行器、账本、验证、审计持久化或恢复；
- 未接入 Agent 工作主链、ASR、TTS 或 P6；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，内容仍为 untracked 基线。

## 错误与修正记录

1. `careful` 技能路径和规划契约测试文件名首次猜错，随后改用实际路径；只读失败，无项目改动。
2. 项目不在默认可写根，首次写入被沙箱拒绝；之后使用受控提升权限。
3. 首次完整实现命令因 PowerShell 引号解析在执行前失败，无部分写入。
4. 初版从 `app.safety` 顶层重导出导致循环导入；恢复原顶层导出。
5. 防 ID 重用测试暴露真实缺陷；增加已用窗口/授权 ID 集合，并修正夹具生成唯一授权 ID。
6. `session_id` 修复脚本按 CRLF 匹配 LF 文件，锚点检查在写入前终止；改为逐行先验证全部锚点再写入。
7. 首次文档收口脚本因 here-string 空行差异在写入前终止；改为逐行定位末尾区块。

## 下一批次入口：P5 批次 2

批次 2 只处理：**L3 独立确认状态、固定短语“确认执行”、一次性消费和防重放**。正式固定短语仍以规划为准，即完整文本“确认执行”，不接受同义词、半句、追加语气词或普通聊天文本。

最小读取集合：本文件、路线图 P5 卡片、`shared/action-plan.md`、`shared/safety-boundaries.md`、`shared/error-recovery.md`、`07-confirmation-and-stop.md`、`app/contracts/planning.py`、`app/safety/confirmation/l2.py` 及对应测试。

下一批次必须继续先写红测，至少覆盖固定短语完整匹配、半句/同义词/追加语气词拒绝、错误或过期窗口、旧计划、两阶段顺序、授权单次消费、并发重复提交和重启后旧授权不可用。不得在批次 2 顺手进入执行前复检或执行器。

## 交接后的强制停止点

当前停止在 `P5 batch 2` 门前。不得继续实现 L3、消费、执行前复检、执行器、账本、验证、审计或恢复；不得进入 P6。
