# P7 Batch 76：路由 binding 的本地动作顺序计划交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch75-routing-metadata-binding-handoff.md`。本批继续按 `implementation_plan/implementation-roadmap.md` 的 P7 第 7 项推进，只完成“binding 决定与 queue cancel / lifecycle cancel / playback abort 的调用者动作顺序契约”这一有界子批次；不改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

本批仍不接真实 Provider、网络、PCM、播放设备、UI、ASR 或 P8；不把动作计划描述为已经执行，不把 lifecycle cancel、playback abort 或 queue cancel 描述为已经发生。

## 2. 完成的工作

- 扩展 `app/tts/routing.py`：
  - 新增冻结枚举 `TTSRoutingActionStep`，只允许三类调用者动作标识：`CANCEL_LIFECYCLE`、`ABORT_PLAYBACK`、`CANCEL_QUEUE`。
  - 新增冻结 `TTSRoutingActionPlan`，把一次 `TTSRoutingBinding` 关联到不可执行的本地动作顺序、取消/中止原因和目标 reply；计划对象不调用 registry、queue、Provider 或设备。
  - 固定并校验动作顺序为：`lifecycle cancel → playback abort → queue cancel`。原因是先锁定旧 request 的本地生命周期，再为已开始/部分播放的句子安排播放事实中止，最后才允许调用者清理当前 reply 的 queue；任何乱序、重复步骤、缺少 reason 或错误 reply 均 fail closed。
  - `TTSRoutingCoordinator.plan_actions(...)` 在生成计划前重新核对 binding 的 request 身份、request sequence、provider generation、lifecycle/playback 快照、queue pending/active 快照和当前路由；binding 过期或事实发生变化时拒绝生成计划。
  - 未终止 lifecycle 才安排 `CANCEL_LIFECYCLE`；`PLAYBACK_STARTED`/`PARTIALLY_PLAYED` 才安排 `ABORT_PLAYBACK`；当前 queue 非空才安排 `CANCEL_QUEUE`。没有需要动作的终态/空队列情况允许返回空计划。
- 扩展 `app/tts/__init__.py` 导出 `TTSRoutingActionStep`、`TTSRoutingActionPlan`。
- 扩展 `tests/tts/test_routing_binding.py`：
  - 未提交播放事实只生成 lifecycle cancel；不伪造 queue cancel，也不改变 lifecycle/queue。
  - 已开始播放的句子生成 lifecycle cancel → playback abort → queue cancel 的严格顺序。
  - 已终止 lifecycle 不重复安排 lifecycle cancel；无 queue 时返回空计划。
  - binding 发生事实漂移后不能生成动作计划。
  - 动作计划冻结，乱序步骤被拒绝。

## 3. 验证

使用项目嵌入式 Python 3.12、`-B`，显式把项目根加入 `sys.path`：

- 路由绑定专项：**11 passed**。
- TTS 全套：**185 passed**。
- 全量：**530 passed**。
- `compileall`（`app` 和 `tests`）：passed。
- `app`/`tests` 共 **140** 个 Python 文件 UTF-8 严格读取审计：无 U+FFFD、无连续四问号、无 NUL。

## 4. 边界、未完成项和强制停止

- `TTSRoutingActionPlan` 只是调用者动作顺序合同，不执行 `lifecycle.cancel(...)`、`playback.abort(...)`、`queue.cancel(...)`，不取消远端 Provider，不停止设备，不清理 queue，也不写回任何 playback/lifecycle 事实。
- `CANCEL_LIFECYCLE` 不是 Provider 已取消确认；`ABORT_PLAYBACK` 不是设备已停止或用户不再听见确认；`CANCEL_QUEUE` 不是旧音频已经从设备缓冲区消失的确认。
- 本批没有实现计划执行器、事件 ID 分配、真实 Provider cancel、设备 stop、PCM 实测时长、播放设备回调、自动编排、隐私探测/冷却恢复或 P8。
- 本批到此停止；不得在本批交接基础上越过 P7 第 7 项剩余边界进入隐私探测/冷却恢复（第 8 项）或 P8。

## 5. 下一批建议边界

下一新会话仍从 P7 第 7 项剩余的本地编排边界开始，优先选择动作计划的一个有界“调用者预检/执行前门控”子批次：只验证当前 lifecycle、playback、queue 是否仍与计划一致，并返回不可执行的门控结果；不得在同一批实现真实动作执行器或自动状态回写。继续保持：每次只完成一个批次，验证后停止并写交接。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch76-routing-action-plan-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 76 已完成第 7 项的本地动作顺序计划：新增 TTSRoutingActionStep 与冻结 TTSRoutingActionPlan；TTSRoutingCoordinator.plan_actions 只在 binding 仍与 request/lifecycle/playback/queue/router 快照一致时生成计划；顺序固定为 lifecycle cancel → playback abort → queue cancel；不执行任何动作、不写回事实。专项 11、TTS 185、全量 530 passed，compileall 与 140 个 Python 文件 UTF-8 审计通过。直接开始下一有界批次，优先做动作计划的调用者预检/执行前门控，只返回不可执行的门控结果；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```
