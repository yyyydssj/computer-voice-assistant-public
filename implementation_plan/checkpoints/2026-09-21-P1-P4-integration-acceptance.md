# P1–P4 跨阶段集成验收记录

> 验收日期：2026-09-21  
> 验收范围：P1、P2、P3、P4 已完成能力的跨模块基线集成  
> 验收性质：**P1–P4 阶段级集成验收通过**；不等同于 P5 安全执行闭环或全项目端到端验收。

## 1. 验收目标

验证 P1–P4 已完成模块可以在不绕过既有契约和安全边界的前提下串联：

```text
P1 RuntimeLifecycle / TextShell
→ P3 ConversationManager / request_id / AgentTurnOutcome
→ P4 ToolRegistry / 只读工具 / PolicyEngine
→ ReviewableActionPlan / plan.json
→ ResultDocument / result.json
```

P2 的契约、事件和状态边界作为跨模块约束参与验收；本次不接入 ASR、TTS 或 P5 执行链。

## 2. 新增验收测试

- `tests/integration/__init__.py`
- `tests/integration/test_p1_p4_integration.py`

测试覆盖：

1. P1 生命周期启动、结构化事件顺序和安全关闭；
2. P1 文字壳输入/退出行为；
3. P3 工作会话、固定 `request_id`、Agent 结果和历史记录；
4. P4 只读目录工具的结构化结果；
5. ToolRegistry → Path/Policy 输入 → `require_confirmation` 决策；
6. 受控写操作只生成不可执行预览，不执行文件副作用；
7. PolicyDecision → `PlanAction` → `ReviewableActionPlan`；
8. `plan.json` 规范序列化与严格解析往返；
9. `ResultAction` → `ResultDocument`；
10. `result.json` 规范序列化与严格解析往返，并验证总状态使用 P4 冻结的 `completed` 契约。

## 3. 验收结果

| 检查项 | 结果 |
|---|---:|
| 跨阶段集成专项 | **2/2 通过** |
| P1–P4 回归（foundation/contracts/agent/conversation/persona/tools/safety/plans/state + integration） | **185/185 通过** |
| 全量 unittest（含 P5 已有测试） | **260/260 通过** |
| `compileall -q app tests` | **通过** |
| 严格 UTF-8 检查 | **92 个 Python 文件通过** |
| UTF-8 替换字符 `U+FFFD` | **0 个文件** |
| UTF-8 BOM | **0 个文件** |
| 集成范围执行面 AST 审计 | **无文件/进程/网络/解释器执行调用** |
| 真实 API 请求 | **未发送** |
| `.env` | **未读取** |
| 真实电脑副作用 | **未执行** |

## 4. 发现与处理

- 首次直接运行 `python -m unittest discover -s tests` 时，既有测试因没有在测试进程内显式加入仓库根目录而出现 `ModuleNotFoundError: app`。这属于测试启动方式问题，不是产品代码失败；后续按仓库既有约定，在测试进程内 `sys.path.insert(0, repo_root)` 后构造并执行测试套件。
- 首轮集成脚本把 `result.json` 的总状态误断言为 `succeeded`；依据 P4 已冻结契约修正为 `completed`，修正后集成专项全绿。
- `tests/integration` 已加入 `__init__.py`，因此可被全量 discover 自动发现；最终全量结果为 260/260。

## 5. 通过结论

P1–P4 的跨阶段基线集成验收**通过**。这证明文字运行外壳、会话/请求边界、结构化工具、策略、计划和结果文档之间能够按既定契约串联；不表示以下能力已完成：

- L2/L3 授权消费与防重放的完整执行闭环；
- preflight、Execution Ledger、fencing token 与真实执行；
- 备份、ResultVerifier、`state_uncertain` 的完整阶段闭环；
- 审计哈希链、持久化、崩溃恢复扫描与恢复报告；
- 分层重试；
- ASR、TTS 或 P6 以后阶段。

## 6. 后续强制规则

从本记录起，每个阶段必须按以下顺序收口：

```text
完成该阶段全部批次
→ 阶段内专项测试与故障注入
→ 跨该阶段及其前置阶段的集成验收
→ 全量回归、compileall、编码与安全审计
→ 记录验收证据
→ 生成该阶段 handoff
→ 才允许进入下一阶段
```

若阶段级集成验收失败，阶段不得标记 `complete`，不得进入下一阶段；应记录失败证据并在当前阶段修复或明确阻塞。

本规则不改变 P5 当前范围，也不提前实现 P5 批次 7/8或 P6。