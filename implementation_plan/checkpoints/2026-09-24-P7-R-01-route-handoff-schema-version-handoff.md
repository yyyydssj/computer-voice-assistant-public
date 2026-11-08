# P7-R-01 RouteHandoff schema/version 实现批次交接

- 日期：2026-09-24
- 项目：`<PROJECT_ROOT>`
- 阶段状态：P0–P6 complete；P7-R-01 complete；P7-R-02 not_started；P7/TTS 未开始
- 当前停止点：**P7-R-01 完成后停止，下一批次须独立授权/验收；不进入 TTS 或 P8**
- 上一交接：`2026-09-24-P7-R-intent-routing-implementation-entry-handoff.md`

## 唯一目标与实际修改

仅实现严格、版本化、不可变的路由交接**数据契约**，不接入业务入口或执行链。

- `app/contracts/routing.py`：`RouteTrace`、`SlotCandidate`、`RouterStatus`、`RouteHandoff`；schema_version 固定为整数 1。嵌套对象严格拒绝未知/缺失字段；JSON 拒绝重复 key 与 NaN/Infinity；候选值与槽映射冻结且序列化返回独立副本。
- `app/contracts/__init__.py`：公开导出上述四个模型。
- `tests/contracts/test_routing.py`：10 个专项测试方法（含多个成功/拒绝子用例）。红测先证明缺失公开模型，再覆盖类型、来源、追踪、状态、工具范围、置信度、未知字段、不可变与重复键。
- `task_plan.md`、`progress.md`：同步本批次完成事实和下一停止点。

## 安全边界

- `route + intent`、`handling_mode`、`required_confirmation`、`tool_scope` 和现有 13 个工具清单的名称/用途组合均作契约校验；只读 route 不可携带副作用工具；`ambiguous` / `unsupported` 无工具范围。
- 路由器失败状态只能形成对应的 `ambiguous` 对象，不能以成功任务交接；低于 0.80 的非 `ambiguous` 路由被拒绝；`low_confidence` 状态不得声称达到 0.80。
- 分类器候选 slot 的 `confirmed` 必须为 `false`，包含直接构造与反序列化；本批次**未**实现 ConversationManager 用户确认后设置 true 的受控入口。
- 契约是路由提示，不能创建授权或绕过 `Plan → preflight → authorization → ledger → executor → verification → audit`。
- 解析失败抛出 `ContractError`，**不是**本批次已实现 failure normalizer；由后续 P7-R-03 负责规范化为 `ambiguous/invalid_router_output`。

## 验证与事实

- 专项：`python -m unittest discover -s tests/contracts -t . -p test_routing.py -q`：**10/10**。
- Contracts 回归：**44/44**；Conversation 回归：**19/19**；标准全量 discovery：**575/575**。
- `app/` 与 `tests/` Python 文件 142 个：严格 UTF-8 解码、`utf-8-sig` AST 解析通过，无 U+FFFD；历史测试文件 3 个带 BOM，均非本批次新增/修改。曾用直接 `ast.parse(utf-8)` 全量检查在既有 BOM 上失败，改为 `utf-8-sig` 后通过；不改动旧文件。
- 新路由模块 AST 执行面审计：仅标准库数据模块和现有 `base` 契约；未发现网络、进程、命令、设备、线程、工具或执行链导入和执行调用。
- 未访问真实 Provider、Laya、云端、麦克风或电脑工具；未执行 Git add/commit/push/reset/safe.directory。

## 已知风险及明确延期

- 当前仅数据结构；无 `IntentRouter`、failure normalizer、ConversationManager/TextShell/ASR 集成，也没有同一 turn 只生效一次的业务防重闸门。
- `tool_scope` 是当前工具名称的契约快照，工具 manifest 风险和参数在后续下游再次校验；不能据此取得执行资格。
- Laya `billing/technical` 模型不是电脑助手意图模型；尚未连接任何真实 Provider。
- 旧测试 BOM 保持原状，不跨范围改写。

## 下一批次入口（本会话不实施）

P7-R-02：纯 `IntentRouter` 接口，仅基于本数据契约添加纯接口与专项测试。再下一批次 P7-R-03 才实现失败规范化；后续依次 ConversationManager 接缝、Provider 适配器与验证。**不得提前**实现 TTS、P8、Laya 真实请求或电脑副作用；下一批仍需独立验证和 handoff。