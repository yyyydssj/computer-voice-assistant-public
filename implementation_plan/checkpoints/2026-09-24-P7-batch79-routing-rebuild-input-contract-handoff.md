# P7 Batch 79：路由门控拒绝后的调用者重建输入合同交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch78-routing-preflight-rejection-classification-handoff.md`。本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：为动作计划门控 `REJECTED` 定义调用者重建所需的当前本地 binding/快照输入合同。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 冻结重建输入对象

扩展 `app/tts/routing.py:303`，新增冻结 `TTSRoutingRebuildInput`，只读携带：

- `provider_request_id`、`reply_id`、`segment_id`；
- `request_sequence`、`provider_generation`；
- 当前 `lifecycle_state` 与 `playback_state`；
- 当前 queue pending request IDs 与 active request ID；
- 当前 `TTSRoutingSnapshot`。

对象只表达调用者重新观察/绑定/规划所需的本地输入，不包含任何动作、执行句柄、Provider cancel、设备 stop 或状态写回接口；字段和枚举均有冻结/类型/标识校验。

### 2.2 拒绝结果携带当前本地输入

扩展 `TTSRoutingActionPreflight` 与 `TTSRoutingCoordinator.preflight_actions`（`app/tts/routing.py:425`、`:544`）：

- `REJECTED` 在当前 request 仍可读时携带 `rebuild_input`；
- 当前事实不可读时返回 `None`，不伪造输入；
- `READY` 明确禁止携带 `rebuild_input`；
- 所有路径仍只读，不自动重建、不执行动作、不回写事实。

同时从 `app/tts/__init__.py` 导出 `TTSRoutingRebuildInput`。

### 2.3 专项测试

扩展 `tests/tts/test_routing_binding.py:173`、`:201`：

- 覆盖 lifecycle 漂移拒绝时提供当前重建输入，并验证输入反映 CANCELLED lifecycle、当前 playback/queue/router snapshot，且 queue 未被清理；
- 覆盖 READY 不提供重建输入；
- 保留并通过既有拒绝分类、重建提示和只读门控测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，显式将项目根加入 `sys.path`：

- 路由绑定专项：**20 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**194 passed**；
- 全量 `tests` discover：**539 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD；
- 包级 `unittest` 入口曾得到 0 tests（该包未提供可直接运行的 discover 入口），已使用明确目录 discover 补跑，194 passed 为有效 TTS 结果。

## 4. 边界、未完成项与强制停止

- `TTSRoutingRebuildInput` 是当前本地事实输入，不是自动重建器、动作计划或执行结果。
- `rebuild_input` 只帮助调用者丢弃旧计划后重新观察/绑定/规划；本批未提供自动 `binding` 重建、自动 `ActionPlan` 重建、重试、执行、事件 ID 分配或状态回写。
- `READY` 仍只表示门控时刻旧计划与当前本地事实一致，不表示任何动作已经执行。
- `REJECTED` 仍只表示旧计划不可继续使用，不表示动作已回滚、queue 已清理、设备已停止或远端已取消。
- 本批不得继续越过 P7 第 7 项边界进入真实 Provider/设备执行或第 8 项隐私探测/冷却恢复，也不得进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。优先选择一个单独的“重建输入与 binding 重新观察之间的不可执行提示/一致性合同”子批次；只允许定义调用者可检查的本地提示或输入一致性校验，不实现自动重建、动作执行、状态回写、真实 Provider/设备接入或 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch79-routing-rebuild-input-contract-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 79 已完成第 7 项的一个有界本地子批次：新增冻结 TTSRoutingRebuildInput，拒绝门控可携带当前 request identity/sequence/generation、lifecycle/playback、queue 与 router snapshot；READY 不带 rebuild_input；当前事实不可读时不伪造输入；不自动重建、不执行动作、不写回事实。路由专项 20、TTS 194、全量 539 passed，compileall 与 140 个 Python 文件 UTF-8 审计通过。直接开始下一有界批次，优先定义“重建输入与 binding 重新观察之间的不可执行提示/一致性合同”，只允许本地只读校验与提示；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```
