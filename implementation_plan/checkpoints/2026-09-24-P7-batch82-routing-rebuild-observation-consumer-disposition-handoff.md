# P7 Batch 82：重建观察结果调用者消费处置交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch81-routing-rebuild-observation-evidence-chain-handoff.md`。
本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：为拒绝后的 `TTSRoutingRebuildInput` 重新观察结果增加显式的、冻结的调用者消费回执，区分“一致快照可只读保留”和“不一致快照必须丢弃并重新观察”。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 新增只读消费处置分类

扩展 `app/tts/routing.py:44`，新增冻结枚举 `TTSRoutingRebuildObservationDisposition`：

- `RETAIN_READ_ONLY`：一致性回执可供调用者只读审阅；
- `DISCARD_AND_REOBSERVE`：不一致时丢弃旧重建输入/旧计划，并重新观察当前本地事实。

该枚举只是调用者处置提示，不是动作许可、重建许可或执行结果。

### 2.2 新增冻结消费回执

扩展 `app/tts/routing.py:408`，新增 `TTSRoutingRebuildObservationReview`：

- 携带原始 `rebuild_input`、调用者提供的 `observed`、既有 `TTSRoutingRebuildConsistency` 与处置分类；
- 提供只读 `consistent`、`requires_reobservation` 与 `notice` 属性；
- 构造时确认处置分类与一致性状态匹配，并拒绝“两个快照不相同却伪造为一致”的回执；
- `frozen=True, slots=True`，不保存到 coordinator，不包含 binding、ActionPlan 或执行状态。

### 2.3 新增 coordinator 消费入口

扩展 `app/tts/routing.py:751`，新增 `TTSRoutingCoordinator.consume_rebuild_observation(rebuild_input, observed)`：

- 仅接受两个冻结的 `TTSRoutingRebuildInput`；
- 复用 `validate_rebuild_observation` 得到一致性结果；
- 返回 `TTSRoutingRebuildObservationReview`；
- 不重新读取当前事实，不创建 binding，不生成 ActionPlan，不执行 cancel/abort/播放动作，不重试，不写回任何事实，不在 coordinator 保存回执。

### 2.4 公开导出与专项测试

扩展 `app/tts/__init__.py:52-99` 导出新增契约。

扩展 `tests/tts/test_routing_binding.py:347-434`：

- 一致观察结果返回 `RETAIN_READ_ONLY`；
- lifecycle 漂移观察结果返回 `DISCARD_AND_REOBSERVE`；
- 验证 coordinator 不保存回执、不执行动作；
- 验证冻结属性不可改写；
- 验证伪造一致性回执被拒绝；
- 验证消费旧观察结果不会隐含重新读取当前事实，调用者必须显式调用 `observe_rebuild_input` 获取新快照。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path`：

- 路由绑定专项：**30 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**204 passed**；
- 全量 `tests` discover：**549 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD；
- `app.tts` 公开导入 smoke test：passed。

说明：直接使用 `unittest discover -s tests/tts` 会因项目包导入方式丢失项目根路径；最终验证采用显式 `sys.path.insert(0, '.')` 与 `top_level_dir='.'`，按既有交接约定完成。

## 4. 边界、未完成项与强制停止

- `TTSRoutingRebuildObservationReview` 是一次只读消费回执，不是观察缓存、自动重建器、binding 工厂、ActionPlan 工厂或执行器。
- `RETAIN_READ_ONLY` 只表示两个本地观察载荷相同且可供审阅；不表示 Provider 成功、音频生成、播放开始、用户已听见或任何动作已经执行。
- `DISCARD_AND_REOBSERVE` 只要求调用者丢弃旧输入/旧计划并显式重新调用本地观察入口；本批不自动循环、不自动重建、不自动重试、不写回状态。
- 消费入口不重新读取当前事实，因此旧观察窗口在事实变化后仍只是旧快照；调用者必须显式获取新快照。
- router snapshot 仍是进程内元数据比对，不代表 Provider、网络、PCM、设备或远端状态。
- 本批未接入真实 Provider/设备执行、自动动作、自动状态回写、隐私探测/冷却恢复，也未进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。应继续选择一个单独的、纯本地只读子批次；优先围绕多次显式观察窗口的最小稳定提示或旧回执与新观察快照的调用者边界，但不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch82-routing-rebuild-observation-consumer-disposition-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 82 已完成第 7 项的一个有界本地子批次：新增冻结 TTSRoutingRebuildObservationDisposition 与 TTSRoutingRebuildObservationReview；新增 TTSRoutingCoordinator.consume_rebuild_observation，仅消费调用者已取得的两个 TTSRoutingRebuildInput，复用一致性校验并返回 RETAIN_READ_ONLY 或 DISCARD_AND_REOBSERVE 提示；不重新读取事实、不自动重建、不执行动作、不写回事实、不保存回执。路由专项 30、TTS 204、全量 549 passed，compileall、140 个 Python 文件 UTF-8 审计与公开导入 smoke test 通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```