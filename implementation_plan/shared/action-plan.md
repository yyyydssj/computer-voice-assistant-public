# ActionPlan、确认与执行文档

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `../archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `../archive/implementation_plan.legacy.md`。

### 7.4 双重确认、权限弹窗与语音播报

普通文件改动包括创建、写入、追加、覆盖、复制、移动、重命名、删除和移入回收站。所有需要授权的操作统一遵守以下规则：

- 权限确认采用**居中模态弹窗**；弹窗未处理前，不继续执行对应任务。
- 权限按钮固定使用“允许执行”和“拒绝”，不使用“确定”和“取消”。
- 权限弹窗出现时，使用人物模板进行简短语音提醒；语音只提醒用户查看弹窗，不代替弹窗授权。
- 用户点击弹窗中的任意按钮后，立即停止当前语音播报，不等待当前句子播报完毕。
- 点击“允许执行”后，仍须重新校验计划、路径、数量、哈希、参数和目标状态；校验失败时拒绝执行。
- 关闭弹窗、按 Esc、点击“拒绝”或弹窗超时均不产生授权；弹窗超时不得自动执行。

L2 普通修改操作采用：

```text
生成执行方案
→ 用户确认方案
→ 居中模态弹窗展示正式执行摘要
→ 用户点击“允许执行”
→ 重新校验
→ 执行并验证
```

L3 高风险操作采用：

```text
居中模态弹窗展示风险与完整方案
→ 用户点击“允许执行”进入第二重确认
→ 用户在独立确认状态输入或说出完整固定短语“确认执行”
→ 系统检查当前确认窗口、plan_hash、版本和有效期
→ 重新校验
→ 执行并验证
```

L3 的用户可见确认短语固定为：

```text
确认执行
```

这里的“动态”体现在确认窗口、`session_id`、`plan_id`、`plan_version`、`plan_hash`、`operation_id` 和过期时间的动态绑定，而不是每次让用户记忆复杂随机数字。短语必须完整匹配，不接受同义词、缩写、追加语气词或模糊识别结果。

确认只在独立确认状态解析。普通聊天中的“好、嗯、可以、行、对、没问题”永远不产生授权。

确认必须绑定：

```text
session_id
plan_id
plan_version
plan_hash
operation_id
规范化路径
源文件哈希
写入内容哈希
确认阶段
授权方式
confirmation_phrase（L3 固定为“确认执行”）
confirmation_window_id
创建时间和过期时间
```

用户提出新任务、取消/停止、编辑文档、文件哈希变化、参数变化、计划过期、弹窗被关闭或播报被打断时，确认立即失效，并从第一阶段重新开始。

任务结果采用右下角非模态通知，默认停留 3 秒，用户可以手动关闭；全部成功、部分成功、失败、严重失败和系统拒绝必须使用正式、明确的状态说明。结果通知同时进行人物模板语音播报，语音只播报简短摘要，完整详情保留在通知详情和执行文档中。严重失败和系统拒绝同样默认停留 3 秒。

### 7.5 用户负责规划，AI 只复述和校验

遇到“整理一下”“弄好一点”“清理没用的”等模糊表达：

1. 先判断是闲聊、咨询还是授权操作；
2. 低风险只读操作可以主动执行；
3. AI 可以主动给出 1~2 个建议方案；
4. 不得自行删除、改名、覆盖或大范围移动；
5. 用户确定规则后再生成具体动作计划；
6. “没用”不能由 AI 自行判断并删除。

推荐对话方式：

```text
我先帮你看看，不会动文件。
我建议按文件类型整理，只移动、不改名、不删除。要按这个方案吗？
我把具体动作写进执行文档了，请学长检查。
```

### 7.6 双层执行文档

复杂或有文件改动的任务建立：

```text
plans/<plan-id>/
├─ execution_plan.md
├─ plan.json
└─ result.json
```

- `execution_plan.md`：用户阅读、检查、语音修改和手动编辑；
- `plan.json`：程序校验、确认和执行的唯一依据；
- `result.json`：记录每个动作的成功、失败、跳过、验证和错误。

生成复杂方案后打开 Markdown，语音只做自然提醒：

> 执行文档我写好了，请学长检查。

打开、查看或编辑文档都不等于授权。

Markdown 可编辑：

```text
目标、范围、排除项、文件规则、递归设置、冲突处理
具体动作表中的增加、删除、修改和跳过
```

Markdown 不可用于改变：

```text
系统黑名单、永久删除禁用、管理员权限禁用、分级授权与 L3 第二重确认要求
路径安全检查、确认状态、计划 ID、计划哈希和执行结果
```

Markdown 不允许嵌入任意 PowerShell、Python、Shell、批处理或外部脚本。任何文档保存都生成新版本并使旧确认失效；程序必须等待保存稳定、重新解析、重新扫描和重新校验后才能重新确认。

### 7.7 执行链路

```text
用户计划
→ AI 复述执行清单
→ 本地生成具体动作
→ ToolRegistry
→ Schema 校验
→ PathGuard
→ PolicyEngine
→ 生成并打开执行文档
→ 用户语音修改或直接编辑
→ 重新解析和校验 plan.json
→ 第一次确认
→ 执行前重算路径和文件哈希
→ 备份
→ 第二次确认
→ 执行
→ ResultVerifier 验证
→ 写入 result.json 和文档
→ 自然语言汇报
```

每个工具必须声明名称、输入/输出 Schema、风险级别、是否需要确认、超时、输出大小、允许路径、幂等性、预览方式和撤销策略。

第一版只开放结构化工具，不让模型直接执行任意 PowerShell。删除默认移入回收站；永久删除禁用。修改、覆盖、重命名和移动应尽量保存备份并记录撤销信息。

### 7.8 崩溃与重启后的计划恢复

程序、Agent 崩溃或电脑重启后，原任务立即进入不可续跑状态。系统不得从中断点继续，也不得复用旧计划、旧 `plan_hash`、确认窗口、授权 token 或旧 L3 确认结果。

恢复时只把审计记录和冻结计划作为线索，使用受限只读工具重新检查电脑当前真实状态，并把每个动作分类为：

```text
completed
not_completed
state_uncertain
```

不能验证时必须使用 `state_uncertain`。系统不自动回滚已完成动作，不自动补做未完成动作。只有在向用户展示恢复报告、用户明确选择继续后，才能创建新计划或新版本，重新扫描、预检、展示差异并重新完成 L2/L3 授权。详细规则见 `error-recovery.md`。
### 7.9 用户主动停止

用户说“停”或点击停止时，系统根据当前状态执行以下规则：

```text
聊天/播报 → 停止当前回复与 TTS，保持实时对话
等待授权 → 取消确认，关闭确认窗口，授权状态失效
任务执行 → 阻止后续动作；安全可取消则请求取消，否则等待原子动作返回
```

任务执行被停止后，原 `plan_id` 对应的执行资格和全部授权立即失效；计划文件可保留为只读审计材料，但不得继续调度。系统必须重新扫描已完成、未完成和状态不确定动作，并生成中断报告。继续处理时创建新计划或新版本，重新预检并授权。
### 7.10 取消、中止与安全退出

任务终止原因必须结构化区分：

```text
user_cancelled  = 用户主动放弃当前任务
system_aborted  = 系统因风险、故障、前提变化或状态不确定而停止
```

规划或等待授权阶段的用户取消不需要恢复扫描，前提是没有任何有副作用动作开始。执行开始后的取消和所有系统中止都必须阻止后续动作、使计划与授权失效，并扫描 `completed`、`not_completed`、`state_uncertain`。

程序关闭分三种路径：无任务时直接清理退出；规划/授权时先取消任务；执行时先进入安全退出，按停止规则处理当前动作，扫描并记录最终可确认状态后退出。外部强制终止按崩溃处理。

## Intent routing and ActionPlan seam (2026-09-24)

`RouteHandoff` is a routing handoff, not an `ActionPlan`, and has no execution authority:

- `task` and `dangerous` may only start structured planning and preflight. Manifest, Schema, PathGuard, Policy/Safety, and preflight determine the final tools, parameters, paths, risk, and confirmation requirements.
- Slots must pass parameter Schema, path safety, allowlist, and Policy checks again; never concatenate candidate slots into commands or execution parameters.
- `dangerous` cannot determine the final risk level and cannot be downgraded to an ordinary task.
- `query` cannot carry side-effect tools; `ambiguous` cannot carry an executable tool scope.
- No route result can skip `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`.
