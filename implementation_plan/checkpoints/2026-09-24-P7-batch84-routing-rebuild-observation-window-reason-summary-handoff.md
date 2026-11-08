# P7 Batch 84：重建观察窗口原因摘要交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch83-routing-rebuild-observation-window-handoff.md`。
本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：为多次显式观察窗口增加稳定的只读不一致原因摘要，去重窗口内重复原因，并按 `TTSRoutingRebuildMismatch` 契约枚举顺序输出；不改变窗口级保留/丢弃处置语义。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 新增稳定原因摘要属性

扩展 `app/tts/routing.py:471` 的 `TTSRoutingRebuildObservationWindowReview`：

- 新增只读 `mismatches` 属性，汇总窗口内所有 `TTSRoutingRebuildConsistency.mismatches`；
- 自动去重，不因多个观察窗口重复发现同一原因而重复展示；
- 按 `TTSRoutingRebuildMismatch` 枚举声明顺序输出，避免依赖观察调用顺序；
- 新增只读 `mismatch_notice`，只描述原因摘要，明确不代表动作许可；
- 空摘要明确返回“未发现不一致原因”的只读提示。

### 2.2 专项测试

扩展 `tests/tts/test_routing_binding.py`：

- 一致窗口返回空原因摘要与稳定空摘要提示；
- lifecycle、request version、queue 等多重漂移原因被去重并按契约顺序输出；
- 重复 lifecycle 原因不会在摘要文本中重复；
- 保留 Batch 83 的窗口一致/不一致处置、至少两个观察结果、冻结与无动作边界测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path`：

- 路由绑定专项：**35 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**209 passed**；
- 全量 `tests` discover：**554 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD、无解码失败；
- `app.tts` 公开导入 smoke test：passed。

## 4. 边界、未完成项与强制停止

- `mismatches` 与 `mismatch_notice` 只是窗口回执的只读原因投影，不是新的动作许可、重建许可、Provider 结果或执行结果。
- 原因摘要只来自调用者提供的冻结观察结果；不会重新读取 lifecycle、playback、queue 或 router 当前事实。
- 原因摘要不自动重试、不自动重建、不执行 cancel/abort/播放、不写回任何事实，也不在 coordinator 保存状态。
- `RETAIN_READ_ONLY` 仍只表示窗口内全部观察载荷与原始重建输入一致；`DISCARD_AND_REOBSERVE` 仍只要求调用者显式丢弃旧输入/旧计划并重新观察。
- router snapshot 仍是进程内元数据比对，不代表 Provider、网络、PCM、设备或远端状态。
- 本批未接入真实 Provider/设备执行、自动动作、自动状态回写、隐私探测/冷却恢复，也未进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。应继续选择一个单独的、纯本地只读子批次；优先围绕原因摘要与窗口级处置的调用者边界或不一致窗口的最小不可执行审阅投影，但不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch84-routing-rebuild-observation-window-reason-summary-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 84 已完成第 7 项的一个有界本地子批次：为冻结 TTSRoutingRebuildObservationWindowReview 新增只读 mismatches 与 mismatch_notice，汇总窗口内不一致原因、去重并按 TTSRoutingRebuildMismatch 枚举顺序稳定输出；不改变 RETAIN_READ_ONLY/DISCARD_AND_REOBSERVE 处置语义，不重新读取事实、不自动重建、不执行动作、不写回事实、不保存回执。路由专项 35、TTS 209、全量 554 passed，compileall、140 个 Python 文件 UTF-8 审计与公开导入 smoke test 通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```