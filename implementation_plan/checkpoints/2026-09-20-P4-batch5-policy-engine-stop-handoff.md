# P4 批次 5 PolicyEngine 完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P4 结构化工具、路径安全、策略和 ActionPlan  
> 状态：`P4 batch 5 complete`；已停在 P4 批次 6 门前

## 本批次唯一目标

实现拒绝优先的 `PolicyEngine`：只接收本地可信代码构造、已经过 Schema 与 PathGuard 的结构化快照，输出稳定的 `allow` / `deny` / `require_confirmation` 决策；本批次不生成 ActionPlan 文件、不授权、不执行。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P4-batch4-path-guard-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次授权；
- 按用户要求在本轮上下文约 60% 时更新最新交接并停止，不进入 P4 批次 6；
- 未改变固定 13 项 MVP 工具、允许根、默认工作目录、风险等级、确认规则或阶段顺序。

## 已完成

### PolicyInput 与 PolicyDecision

新增 `app/safety/policy_engine.py`，公开：

- `POLICY_VERSION = "1.0.0"`
- `PolicyInput`
- `PolicyDecision`
- `PolicyEngine`

`PolicyInput` 是 frozen/slotted 的可信结构化快照，记录：

- 工具稳定名称、Manifest 版本和内容哈希；
- 来自本地 Manifest 的风险等级和确认要求；
- 已规范化、深度冻结的参数；
- `PathSnapshot` 元组。

`PolicyDecision` 是 frozen/slotted、可序列化且不携带授权或执行能力的策略事实，记录：

- `allow` / `deny` / `require_confirmation`；
- 稳定 `reason_code`、人类可读原因和 `policy_version`；
- 本地 Manifest 身份、固定风险等级、确认要求；
- 稳定顺序的规范化路径列表。

### 固定策略与拒绝优先

- 未注册工具直接 `deny/tool_not_registered`；任意命令、PowerShell、Shell、Python 和进程终止名称无法通过固定 `ToolRegistry`；
- Manifest 版本、内容哈希、风险等级或确认要求不一致时拒绝；调用方不能伪造低风险或关闭确认；
- `L0`、`L1` 固定输出 `allow`；`L2`、`L3` 固定输出 `require_confirmation`；PolicyEngine 不消费授权，也不创建授权；
- 路径工具必须提供 PathGuard 快照；非路径工具携带快照时拒绝；
- 规范化参数路径必须与快照逐项完全匹配，重复、缺失、额外、顺序不稳定或重命名目标不完整均拒绝；
- 快照的规范化路径和真实路径必须位于固定 `<STUDY_ROOT>` 内；越界快照在调用 PathGuard 复检前硬拒绝；
- 有路径工具必须注入执行前复检器；复检身份变化、链接/权限状态变化、返回不一致或未知异常均按拒绝处理；
- 参数递归扫描保留的策略字段（风险、确认、允许根、授权、Policy 版本、命令、Shell、黑名单等）一律拒绝，模型、Skill、工具返回和外部文本不能注入策略；
- `PolicyEngine.evaluate()` 不接受自然语言或普通 Mapping，只接受 `PolicyInput`。

### 稳定性与边界修正

- 多路径键改为固定元组顺序，不再使用 `frozenset` 迭代，确保 `normalized_paths` 可复现；
- PathGuard 复检器的未知异常不向上泄漏为执行前成功，统一归类为 `path_snapshot_changed` 拒绝；
- 未新增 `execute`、授权消费、文件写入、复制、移动、删除、应用启动或子进程入口。

## TDD 与故障注入

新增 `tests/safety/test_policy_engine.py`，当前共 11 个测试，覆盖：

- 固定 L1/L2/L3 决策和无授权表面；
- 决策不可变、可序列化和稳定原因字段；
- 未知工具和任意命令硬拒绝；
- Manifest 版本、哈希、风险等级和确认要求伪造；
- 路径快照缺失、额外、重复、不匹配、越界和复检变化；
- 多路径输出顺序稳定；
- 自然语言、普通 Mapping 和保留策略字段注入；
- 参数与快照深度不可变。

## 验证结果

- PolicyEngine 专项：`11/11` 通过；
- 全量 unittest：`159/159` 通过；
- `compileall`：使用可写工作区 `PYTHONPYCACHEPREFIX` 通过；
- 严格 UTF-8：`65` 个 Python 源文件通过，无替换字符；
- AST 审计：`policy_engine.py` 未发现执行、文件写入、复制、移动、删除、进程启动或终止入口；
- 未读取 `.env`，未启动官方 Harness runtime，未发送真实 API 请求；
- 未创建、写入、复制、移动、重命名或移入回收站任何真实目标；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，项目文件仍为 untracked 基线。

## 错误与修正记录

1. 首次红测夹具使用了不能以单反斜杠结尾的 Python 原始字符串，修正为普通字符串后确认真正红灯为 PolicyEngine 模块缺失。
2. 首次 PolicyEngine 写入命令因 PowerShell 导出字符串引号未闭合在解析阶段失败，命令零写入；随后拆分为模块写入和公开导出更新并成功。
3. 嵌入式 Python 的 `._pth` 隔离仍使普通 unittest 命令无法发现仓库根包；按前批次方式仅在测试进程内插入项目根，未修改解释器配置。
4. 复核发现多路径使用 `frozenset` 会造成输出顺序不稳定；改为固定元组并补回归测试，专项与全量回归通过。

## 新增/修改文件

- `app/safety/policy_engine.py`
- `app/safety/__init__.py`
- `tests/safety/test_policy_engine.py`
- `implementation_plan/implementation-roadmap.md`
- `progress.md`
- 本交接文档

## 本批次明确未做

- 未实现 `plan.json`、`execution_plan.md`、`result.json` 的版本、哈希和解析规则；
- 未实现 ActionPlan 聚合、持久化、Markdown 展示或 Markdown 安全字段冻结；
- 未实现 L2/L3 授权弹窗、固定短语、授权快照、一次性消费或防重放；
- 未实现执行器、执行账本、验证器、审计链、停止/取消/恢复；
- 未执行任何电脑副作用动作；
- 未开放任意 PowerShell、Python、Shell、批处理、外部命令或进程终止；
- 未启动应用；
- 未接入 Agent 主链、主界面、ASR、TTS 或语音链路；
- 未执行 Git 提交或推送。

## 下一批次入口：P4 批次 6

下一批次只实现 **ActionPlan 文件与稳定序列化规则**：

- 在已有 Manifest、PathGuard 快照和 PolicyDecision 之上，构造不可执行、可审查的计划事实；
- 固定 `plan_id`、`plan_version`、`plan_hash`、动作顺序、规范化参数、路径事实、风险、策略版本和冲突规则；
- 生成/解析 `plan.json` 与 `execution_plan.md` 的稳定规则，且 Markdown 不能改变安全字段；
- 覆盖字段注入、哈希篡改、动作重排、路径/风险/Policy 事实漂移和 Markdown 往返一致性。

批次 6 不得顺手实现授权、执行器、结果文件、审计或语音链路。

## 交接后的强制停止点

当前停止在 `P4 batch 6` 门前。后续会话读取本文件、路线图 P4 卡片、`shared/action-plan.md`、`shared/safety-boundaries.md`、现有 `manifest.py` / `write_plans.py` / `path_guard.py` / `policy_engine.py` 及相关测试后，可按用户当前指令直接进入下一批次；不得从授权、执行器或语音链路跳阶段。