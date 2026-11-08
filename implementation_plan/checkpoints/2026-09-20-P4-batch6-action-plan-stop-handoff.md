# P4 批次 6 ActionPlan 稳定编解码完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P4 结构化工具、路径安全、策略和 ActionPlan  
> 状态：`P4 batch 6 complete`；已停在 P4 批次 7 `result.json` 与阶段收口门前

## 本批次唯一目标

在已有 ToolRegistry、Manifest、PathGuard 快照和 PolicyDecision 之上，实现不可执行、可审查且可稳定往返的 ActionPlan 事实；固定 `plan.json` 与 `execution_plan.md` 的版本、哈希、序列化和严格解析规则。本批次只在内存中构造和解析字符串，不创建计划目录、不写计划文件，不实现 `result.json`、授权、授权消费或执行器。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P4-batch5-policy-engine-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次授权；
- 按用户要求在上下文接近约 60% 时完成最新交接并停止，不进入下一批次；
- 未改变固定 13 项 MVP 工具、允许根、默认工作目录、风险等级、确认规则、`POLICY_VERSION=1.0.0`、冲突策略 `deny` 或阶段顺序。

## 新增公开 API

新增 `app/plans/action_plan.py` 与 `app/plans/__init__.py`，公开：

- `ACTION_PLAN_SCHEMA_VERSION = 1`
- `ACTION_PLAN_FORMAT_VERSION = "1.0.0"`
- `ActionPlanError`
- `PlanAction`
- `ReviewableActionPlan`
- `ExecutionPlanReview`
- `build_plan_action()`
- `build_reviewable_plan()`
- `serialize_plan_json()`
- `parse_plan_json()`
- `render_execution_plan_markdown()`
- `parse_execution_plan_markdown()`

名称采用 `ReviewableActionPlan`，避免与 P2 已有轻量契约 `app.contracts.planning.ActionPlan` 冲突。所有计划对象只承载事实，不含 `execute`、授权、授权消费、文件操作或进程启动能力。

## 稳定 `plan.json` 规则

- 计划和动作采用 frozen/slotted dataclass；参数、预览和路径事实深度冻结；
- 规范 JSON 使用 UTF-8、`sort_keys=True`、紧凑分隔符和 `allow_nan=False`；
- `plan_hash` 是规范计划事实的 SHA-256，覆盖：
  - schema/format version；
  - `plan_id`、`plan_version`、`task_id` 和创建时间；
  - 目标、范围、排除项、文件规则、递归与固定冲突策略；
  - 动作顺序及 `action_id`；
  - Manifest 名称、版本和内容哈希；
  - 规范化参数和完整 `PathSnapshot`；
  - `PolicyDecision`、风险等级、确认要求和 Policy 版本；
  - 预览、验证方法和回滚策略；
- 动作 `order` 必须从 1 连续递增，`action_id` 不得重复；多动作构造时调用方必须显式传入后续动作顺序；
- 严格解析拒绝未知或缺失字段、重复 JSON key、`NaN` 等非标准 JSON、哈希篡改、动作重排和重复 ID；
- 解析先校验 `plan_hash`，再复核本地 ToolRegistry、Policy 和路径事实；即使输入重新计算哈希，Manifest、Policy 或路径事实漂移仍会被语义校验拒绝。

## `execution_plan.md` 安全边界

- Markdown 是可审查展示，不是执行或授权来源；`plan.json` 仍是未来程序校验与执行的唯一依据；
- 除用户备注区外，Markdown 必须与当前计划的规范渲染结果逐字一致；
- 唯一可变区由 `<!-- USER_NOTES_START -->` 与 `<!-- USER_NOTES_END -->` 包围；
- 用户备注最多 4096 字符，不进入 `plan_hash`，不能改变计划安全事实，也不产生授权；
- 用户备注拒绝代码围栏、脚本标签和明显 PowerShell、Python、Shell、批处理或外部命令载荷。

## TDD 与故障注入

新增 `tests/plans/test_action_plan.py`，共 11 个测试，覆盖：

- 不可变、不可执行计划和稳定哈希；
- 规范 JSON 严格往返；
- 版本、动作顺序、参数、路径、风险、Policy、预览和计划版本进入哈希；
- 未知/缺失字段、重复 key、非标准 JSON 和 schema/format 错误；
- 哈希篡改、重新哈希后的动作重排、重复 ID、Manifest/Policy/路径事实漂移；
- 拒绝 `deny` 决策和 PolicyInput/PolicyDecision 事实不一致；
- Markdown 往返、隔离备注、脚本载荷、重复区段、超限备注和安全字段篡改。

首次红灯为 `ModuleNotFoundError: No module named 'app.plans'`，确认目标模块缺失而非测试夹具或环境问题后才写最小实现。

## 验证结果

- ActionPlan 专项：`11/11` 通过；
- 全量 unittest：`170/170` 通过，`FAILURES=0`、`ERRORS=0`；
- `compileall`：通过；
- 严格 UTF-8：`69` 个 Python 源文件通过，无解码错误和替换字符；
- 精确 AST 文件/进程副作用审计：`AST_FILE_PROCESS_CALLS=[]`；
- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未创建计划目录或写入计划文件，未执行真实文件创建、写入、复制、移动、重命名、删除或回收站操作；
- 未启动应用或子进程；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，项目内容仍为 untracked 基线。

## 错误与修正记录

1. 首次写测试时沙箱拒绝访问项目目录，命令零写入；随后使用已授权的提升权限完成写入。
2. 首次尝试一次性写入完整模块触发 Windows `文件名或扩展名太长 (os error 206)`，进程启动前失败且零写入；随后改为分段写入。
3. 首轮专项测试暴露四项测试问题：两个多动作夹具未给第二动作设置 `order=2`；一个测试用 `dataclasses.replace` 构造非法对象，但对象已在构造期正确拒绝；“规范 JSON 无空格”断言误把字符串值内合法空格当作格式空格。修正测试语义后通过。
4. 一次 PowerShell 修补把字面量 `` `n `` 写入 Python，造成 SyntaxError；随后使用逐行精确编辑修复。
5. 初版 AST 审计把字符串 `.replace()` 误报为文件替换；收紧为明确模块归属的文件/进程 API 后通过。

## 新增/修改文件

### 实现与测试

- `app/plans/action_plan.py`
- `app/plans/__init__.py`
- `tests/plans/test_action_plan.py`
- `tests/plans/__init__.py`

### 文档收口

- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`
- `implementation_plan/checkpoints/2026-09-20-P4-batch6-action-plan-stop-handoff.md`

## 本批次明确未做

- 未创建真实计划目录，未把 `plan.json` 或 `execution_plan.md` 写入磁盘；
- 未实现 `result.json` 的版本、哈希、解析、持久化或审计规则；
- 未实现 L2/L3 授权弹窗、固定短语、授权快照、一次性消费或防重放；
- 未实现执行器、执行账本、验证器、审计链、停止/取消/恢复；
- 未执行任何电脑副作用动作；
- 未开放任意 PowerShell、Python、Shell、批处理、外部命令或进程终止；
- 未启动应用；
- 未接入 Agent 主链、主界面、ASR、TTS 或语音链路；
- 未执行 Git 提交或推送。

## 下一批次入口：P4 批次 7

下一批次只处理 **`result.json` 的稳定契约与 P4 阶段收口**：

- 先根据路线图、`shared/action-plan.md` 和现有 P2 执行结果契约确认本批次精确边界；
- 为 `result.json` 固定版本、身份、哈希和严格解析规则，并覆盖字段注入、重复 key、哈希篡改和身份漂移；
- 保持结果事实与 ActionPlan/未来执行账本的边界清晰，不借结果文件生成授权，不执行动作；
- 运行 P4 专项与全量回归，核对阶段退出条件并完成 P4 handoff。

批次 7 不得顺手实现授权、授权消费、执行器、审计写入、恢复或语音链路；不得进入 P5。

## 交接后的强制停止点

当前停止在 `P4 batch 7` 门前。后续会话最小读取本文件、路线图 P4 卡片、`shared/action-plan.md`、`shared/safety-boundaries.md`、P2 相关结果契约、现有 `app/plans/action_plan.py` 及测试后，可按用户当前指令直接进入下一批次；不得从授权、执行器、审计或语音链路跳阶段。
