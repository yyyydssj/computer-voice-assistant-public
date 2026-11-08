# P7 Batch 81：重建输入最小重新观察证据链交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch80-routing-rebuild-observation-consistency-handoff.md`。
本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：为拒绝后的 `TTSRoutingRebuildInput` 增加一次当前本地事实重新观察入口，并允许调用者把该观察快照交给一致性校验，形成最小、可审计、不可执行的证据链。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 新增只读重新观察入口

扩展 `app/tts/routing.py:594`，新增 `TTSRoutingCoordinator.observe_rebuild_input(rebuild_input)`：

- 只接受冻结的 `TTSRoutingRebuildInput`；
- 读取当前 lifecycle request、playback fact、queue pending/active 与 router snapshot；
- 返回新的冻结 `TTSRoutingRebuildInput` 观察快照；
- 不创建 `TTSRoutingBinding`，不生成 `TTSRoutingActionPlan`，不执行 cancel/abort/播放动作，不重试，不写回任何事实；
- 当前事实读取失败时仅抛出本地 `TTSRoutingError`，不伪造观察输入。

### 2.2 扩展一致性校验消费边界

扩展 `TTSRoutingCoordinator.validate_rebuild_observation`：

- 第二参数现在接受调用者重新读取的 `TTSRoutingRebuildInput`，同时保留 Batch 80 的 `TTSRoutingBinding` 兼容校验入口；
- 仍按 request identity、request sequence/generation、lifecycle、playback、queue 和 router snapshot 稳定分类；
- 仅返回 `TTSRoutingRebuildConsistency`；一致或不一致都不获得动作许可，不代表重建结果或执行结果；
- 对 binding 兼容路径仍额外确认当前 router active reply/route 与 binding 一致，router drift 仍归类为 `ROUTER`。

### 2.3 专项测试

扩展 `tests/tts/test_routing_binding.py:331`、`:345`：

- 当前事实重新观察后与原始输入一致，返回 `CONSISTENT`，并验证 router/queue 未改变；
- lifecycle 在重新观察前漂移时，观察快照携带当前 lifecycle，校验返回 `LIFECYCLE`，且只产生重新读取本地事实的提示；
- 保留 Batch 80 的 binding 兼容一致性、request version/lifecycle/queue/router drift、READY 不带 `rebuild_input`、事实不可读不伪造输入及拒绝门控测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path`：

- 路由绑定专项：**25 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**199 passed**；
- 全量 `tests` discover：**544 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD；
- `app.tts` 公开导入 smoke test：passed。

## 4. 边界、未完成项与强制停止

- `observe_rebuild_input` 是一次当前本地事实快照读取，不是自动重建器、binding 工厂、ActionPlan 工厂或执行器。
- `validate_rebuild_observation` 的 `CONSISTENT` 只表示两个本地观察载荷在校验字段上相同；不表示 Provider 成功、音频生成、播放开始、用户已听见或任何动作已经执行。
- `INCONSISTENT` 只要求调用者丢弃旧计划并重新读取本地事实；本批不自动循环观察、不自动重建、不自动重试、不写回状态。
- router snapshot 仍是进程内元数据比对，不代表 Provider、网络、PCM、设备或远端状态。
- 本批未接入真实 Provider/设备执行、自动动作、自动状态回写、隐私探测/冷却恢复，也未进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。应继续选择一个单独的、纯本地只读子批次；优先围绕观察快照的调用者消费/丢弃边界或多次观察窗口的最小稳定提示，但不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch81-routing-rebuild-observation-evidence-chain-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 81 已完成第 7 项的一个有界本地子批次：新增 TTSRoutingCoordinator.observe_rebuild_input，从当前 lifecycle/playback/queue/router 只读生成冻结 TTSRoutingRebuildInput 观察快照；validate_rebuild_observation 接受该观察快照并保持 Batch 80 的 binding 兼容校验，只返回一致/不一致提示，不自动重建、不执行动作、不写回事实。路由专项 25、TTS 199、全量 544 passed，compileall、140 个 Python 文件 UTF-8 审计与公开导入 smoke test 通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```