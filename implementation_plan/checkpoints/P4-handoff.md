# P4 结构化工具、路径安全、策略和 ActionPlan 完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P4  
> 阶段状态：`complete`  
> 当前停止点：P5 授权门前

## 本阶段唯一目标

建立只允许结构化、可审查计划的本地安全边界：Agent 不能直接执行自然语言命令；所有未来电脑动作必须经过 ToolRegistry、Schema、PathGuard、PolicyEngine 和冻结 ActionPlan。P4 只提供清单、只读能力、写操作预览、路径/策略事实、`plan.json`、`execution_plan.md` 和 `result.json` 的不可执行契约，不提供授权或执行能力。

## 本阶段完成的 7 个批次

1. 固定 13 个 MVP Tool Manifest、风险等级、路径范围、预览/验证/回滚元数据和不可变 ToolRegistry；
2. 实现四个只读结构化工具，并限制编码、深度、结果数、输出大小、超时和敏感字段；
3. 实现七个受控副作用工具的纯计划生成/预览，绝不执行；
4. 实现 PathGuard 的 Windows 路径规范化、允许根、真实路径/Reparse/Junction/UNC/映射盘拒绝和快照复检；
5. 实现只接受可信结构化快照的拒绝优先 PolicyEngine；
6. 实现不可执行 ActionPlan、规范 `plan.json` 和只允许隔离备注的 `execution_plan.md`；
7. 实现不可执行 `result.json` 稳定契约、哈希、严格解析与阶段收口。

## 批次 7 新增公开 API

`app/plans/result_document.py` 与 `app/plans/__init__.py` 公开：

- `RESULT_DOCUMENT_SCHEMA_VERSION = 1`
- `RESULT_DOCUMENT_FORMAT_VERSION = "1.0.0"`
- `ResultDocumentError`
- `ResultAction`
- `ResultDocument`
- `build_result_action()`
- `build_result_document()`
- `serialize_result_json()`
- `parse_result_json()`

名称与 P2 的轻量 `app.contracts.execution.ActionResult` 区分。上述对象仅承载事实，不含 `execute`、`authorize`、授权消费、文件持久化或进程启动能力。

## result.json 固定规则

- `ResultAction` / `ResultDocument` 均为 frozen/slotted dataclass；输出和验证事实深度冻结；
- 规范 JSON 使用 UTF-8、`sort_keys=True`、紧凑分隔符、`allow_nan=False`；
- `result_hash` 覆盖 schema/format、`result_id`、`execution_id`、冻结计划身份、创建/完成时间、派生总状态和全部动作结果事实；
- 动作必须与冻结计划逐项匹配 order、`action_id`、工具名/版本和 Manifest 哈希；数量、顺序或身份变化全部拒绝；
- outcome 固定为 `succeeded`、`failed`、`skipped`、`state_uncertain`；验证状态固定为 `completed`、`not_completed`、`state_uncertain`；
- 错误、跳过、尝试和时间字段必须与 outcome 一致；完成/验证时间不得倒序；
- 总状态由动作结果派生，不接受调用方自行声明；`state_uncertain` 优先传播；
- 严格解析拒绝未知/缺失字段、重复 JSON key、非标准数字、哈希篡改、身份漂移、动作重排、重复 ID 和总状态伪造；
- `expected_plan` 是解析必需输入。即使攻击者重算 `result_hash`，也不能把结果绑定到其他计划或动作；
- 结果文件不能产生授权、不能驱动执行、不能自动重试、补做、回滚或把 `state_uncertain` 改写为成功/失败。

## P4 退出条件核对

| 退出条件 | 证据 | 结果 |
|---|---|---|
| 非法工具名、参数、路径、风险等级和任意命令均被本地拒绝 | Manifest/Registry、预览、PathGuard、PolicyEngine 测试 | 通过 |
| 工具返回自然语言不能直接成为下一步参数 | 结构化 schema 与 PolicyInput 边界，无任意命令或隐式串接入口 | 通过 |
| 只读工具可生成结构化结果 | `tests/tools/test_read_only.py` | 通过 |
| 计划哈希稳定且 Markdown 不能改变安全字段 | `tests/plans/test_action_plan.py` 11/11 | 通过 |
| 模型不能修改黑名单、授权规则和工具注册表 | Registry sealed、保留字段拒绝、Policy 只读事实测试 | 通过 |
| result.json 严格绑定计划并稳定往返 | `tests/plans/test_result_document.py` 12/12 | 通过 |

## 最终验证

- `tests.plans.test_result_document`：12/12；
- `tests.plans.test_action_plan`：11/11；
- 全量 `unittest discover -s tests`：182/182；
- `python -m compileall -q app tests`：通过；
- 严格 UTF-8：71 个 Python 文件通过，无替换字符；
- 本批次实现与测试精确 AST 文件/进程副作用审计：`RESULT_DOCUMENT_FILE_PROCESS_CALLS=[]`；
- 未读取 `.env`，未启动 Harness runtime，未发送真实 API 请求；
- 未创建计划目录，未写入真实 `plan.json`、`execution_plan.md` 或 `result.json`；
- 未执行电脑副作用动作，未启动应用或子进程。

## 错误与修正记录

1. 首次写红测的 PowerShell `WriteAllText` 参数组合错误，命令未写入文件；随后改为先构造文本再三参数写入。
2. 初次更新 `app/plans/__init__.py` 时把字面量 `\n` 写入文件，导致 SyntaxError；立即整文件按 UTF-8 重写修复。
3. 首轮结果专项通过 11/11 后补充总状态和时间顺序故障注入，发现“skipped + failed”被错误分类为部分完成，以及倒序时间未拒绝；修复为无成功动作但存在失败时 `failed`，并加入动作/文档时间单调校验，专项最终 12/12。
4. 初次全仓宽泛 AST 审计误报既有 `.replace()`、测试文件写入和 P1 运行入口；收紧到本批次文件和明确模块归属后结果为空。

## 本阶段明确未做

- 未实现 L2/L3 授权 UI、固定短语、授权快照消费或防重放；
- 未实现真实本地执行器、执行账本、fencing token、ResultVerifier 或审计持久化；
- 未实现停止、取消、中止、崩溃恢复或自动续跑；
- 未接入 Agent 工作主链、主界面、ASR、TTS 或任何语音链路；
- 未开放 PowerShell、Python、Shell、批处理、外部命令、管理员权限或动态 Skill；
- 未执行 Git 提交或推送；仓库仍无首次 commit，内容仍为 untracked 基线。

## 下一阶段入口：P5

P5 只在用户明确授权后进入。唯一目标是在文字模式下建立最小、可停止、可验证的安全执行闭环：L2/L3 授权、授权精确绑定与单次消费、执行账本/fencing、单个结构化动作执行、ResultVerifier、`result.json` 持久化、审计、停止/取消/中止与崩溃恢复扫描。

P5 不得接入麦克风或 TTS，不得自动续跑崩溃任务，不得开放任意命令、管理员权限或动态 Skill。

## P5 最小读取集合

1. 本文件 `implementation_plan/checkpoints/P4-handoff.md`；
2. `implementation_plan/implementation-roadmap.md` 的 P5 卡片；
3. `implementation_plan/shared/action-plan.md`；
4. `implementation_plan/shared/safety-boundaries.md`；
5. `implementation_plan/shared/error-recovery.md`；
6. P2 授权、执行结果、审计链契约；
7. 当前 `app/plans/`、`app/safety/`、`app/tools/` 及其专项测试。

## 交接后的强制停止点

当前停止在 **P5 授权门前**。未经用户明确授权不得创建或修改 P5 授权、执行、验证、审计或恢复代码；不得进入 P6 及以后阶段。