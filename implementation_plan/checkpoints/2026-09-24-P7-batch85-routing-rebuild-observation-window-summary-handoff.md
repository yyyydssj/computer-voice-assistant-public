# P7 Batch 85：重建观察窗口最小审阅摘要交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch84-routing-rebuild-observation-window-reason-summary-handoff.md`。
本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：为多次显式观察窗口增加最小、不可执行的只读审阅投影，携带窗口处置分类、观察次数和去重后的不一致原因；不携带原始观察快照、binding、ActionPlan 或执行状态。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 新增最小只读审阅摘要

扩展 `app/tts/routing.py:451`，新增冻结 `TTSRoutingRebuildObservationWindowSummary`：

- 携带 `disposition`、`observation_count` 和去重且按契约顺序排列的 `mismatches`；
- 提供只读 `consistent`、`requires_reobservation` 与 `notice` 属性；
- 强制观察次数至少为 2；
- 强制原因 tuple 不重复且按 `TTSRoutingRebuildMismatch` 枚举顺序排列；
- 一致摘要不得携带不一致原因；需要重新观察的摘要必须至少携带一个原因；
- `frozen=True, slots=True`，不保存原始 `rebuild_input`、观察窗口、binding、ActionPlan 或动作状态。

### 2.2 扩展窗口回执摘要入口

扩展 `TTSRoutingRebuildObservationWindowReview`：

- 新增只读 `summary` 属性；
- 每次按现有窗口回执现场投影最小摘要，不重新读取 lifecycle、playback、queue 或 router；
- 不改变既有 `RETAIN_READ_ONLY` / `DISCARD_AND_REOBSERVE` 处置语义。

### 2.3 公开导出与专项测试

扩展 `app/tts/__init__.py` 与 `app/tts/routing.py::__all__` 导出新增契约。

扩展 `tests/tts/test_routing_binding.py`：

- 验证一致窗口产生最小、冻结、无原始快照字段的摘要；
- 验证不一致窗口保留观察次数、处置分类和 request version/lifecycle/queue 原因；
- 验证摘要不可修改；
- 保留 Batch 84 的原因去重、稳定排序、窗口处置与无动作边界测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path`：

- 路由绑定专项：**37 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**211 passed**；
- 全量 `tests` discover：**556 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD、无解码失败；
- `app.tts` 公开导入 smoke test：passed。

## 4. 边界、未完成项与强制停止

- `TTSRoutingRebuildObservationWindowSummary` 是不可执行的审阅投影，不是新的动作许可、重建许可、Provider 结果或执行结果。
- 摘要只来自调用者已经提供的冻结观察窗口；不会重新读取当前本地事实。
- 摘要不自动重试、不自动重建、不执行 cancel/abort/播放、不写回任何事实，也不在 coordinator 保存状态。
- `RETAIN_READ_ONLY` 仍只表示窗口内全部观察载荷与原始重建输入一致；`DISCARD_AND_REOBSERVE` 仍只要求调用者显式丢弃旧输入/旧计划并重新观察。
- router snapshot 仍是进程内元数据比对，不代表 Provider、网络、PCM、设备或远端状态。
- 本批未接入真实 Provider/设备执行、自动动作、自动状态回写、隐私探测/冷却恢复，也未进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。应继续选择一个单独的、纯本地只读子批次；优先围绕最小审阅摘要与原始窗口回执的显式调用者分层，或摘要在过期观察窗口下的不可执行提示，但不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch85-routing-rebuild-observation-window-summary-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 85 已完成第 7 项的一个有界本地子批次：新增冻结 TTSRoutingRebuildObservationWindowSummary，携带窗口 disposition、观察次数和去重/稳定排序后的 mismatches，提供只读 consistent、requires_reobservation、notice；TTSRoutingRebuildObservationWindowReview 新增 summary 投影；摘要不携带原始快照、binding、ActionPlan 或执行状态，不重新读取事实、不自动重建、不执行动作、不写回事实、不保存回执。路由专项 37、TTS 211、全量 556 passed，compileall、140 个 Python 文件 UTF-8 审计与公开导入 smoke test 通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```