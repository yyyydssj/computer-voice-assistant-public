# P7 Batch 83：重建观察多窗口稳定提示交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch82-routing-rebuild-observation-consumer-disposition-handoff.md`。
本批只完成 P7 第 7 项剩余本地编排边界中的一个有界子批次：为拒绝后的 `TTSRoutingRebuildInput` 增加多次显式观察窗口的最小稳定提示，要求调用者提供至少两个已经取得的冻结观察快照；只有窗口内全部观察结果与原始重建输入一致时，才返回只读保留提示，否则返回丢弃并重新观察提示。

本批不改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件；不接真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8。

## 2. 已完成工作

### 2.1 新增多观察窗口只读回执

扩展 `app/tts/routing.py:451`，新增冻结 `TTSRoutingRebuildObservationWindowReview`：

- 携带统一的原始 `rebuild_input`、至少两个 `TTSRoutingRebuildObservationReview` 和窗口级处置分类；
- 提供只读 `observations`、`consistencies`、`consistent`、`requires_reobservation` 与 `notice` 属性；
- 要求窗口内所有 review 绑定同一个 `rebuild_input`；
- 窗口级 `RETAIN_READ_ONLY` 仅在全部显式观察结果一致时成立；任一观察结果不一致即为 `DISCARD_AND_REOBSERVE`；
- `frozen=True, slots=True`，不保存观察缓存，不重新读取事实，不包含 binding、ActionPlan 或执行状态。

### 2.2 新增 coordinator 多窗口消费入口

扩展 `app/tts/routing.py:825`，新增 `TTSRoutingCoordinator.consume_rebuild_observation_window(rebuild_input, observed_window)`：

- 仅接受冻结 `TTSRoutingRebuildInput` 与至少两个元素的 tuple；
- 拒绝 list 或其他可变/非 tuple 容器，避免把调用者尚未冻结的观察集合误当作稳定窗口；
- 逐个复用既有 `consume_rebuild_observation`，不改变单次一致性分类；
- 只聚合窗口级处置提示，不重新观察当前事实，不创建 binding，不生成 ActionPlan，不执行 cancel/abort/播放动作，不重试，不写回事实，不在 coordinator 保存回执。

### 2.3 公开导出与专项测试

扩展 `app/tts/__init__.py` 与 `app/tts/routing.py::__all__` 导出新增契约。

扩展 `tests/tts/test_routing_binding.py`：

- 两次稳定显式观察结果全部一致时返回 `RETAIN_READ_ONLY`；
- 两次观察之间发生 lifecycle 漂移时，窗口返回 `DISCARD_AND_REOBSERVE`，并保留每次观察的独立一致性结果；
- 验证窗口至少需要两个观察结果；
- 验证回执冻结、路由与队列未被消费入口改变；
- 保留 Batch 82 的单次消费、旧观察非新读取、伪造一致性拒绝与无动作边界测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path`：

- 路由绑定专项：**33 passed**；
- 明确 `tests/tts` discover 的 TTS 全套：**207 passed**；
- 全量 `tests` discover：**552 passed**；
- `compileall`（`app` 和 `tests`）：passed；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 读取审计：无 U+FFFD、无解码失败；
- `app.tts` 公开导入 smoke test：passed。

第一次全量 discover 直接从 C: 驱动器调用、以 G: 项目目录作为 start_dir，因 Python `unittest` 跨盘 `relpath` 限制失败；随后切换到 G: 项目目录内执行，正式全量结果为 **552 passed**。该失败是测试启动方式问题，不是产品测试失败。

## 4. 边界、未完成项与强制停止

- `TTSRoutingRebuildObservationWindowReview` 是一次性只读窗口回执，不是观察缓存、稳定性探测器、自动重建器、binding 工厂、ActionPlan 工厂或执行器。
- 多窗口 `RETAIN_READ_ONLY` 只表示调用者提供的全部冻结观察载荷与原始本地快照一致；不表示 Provider 成功、音频生成、播放开始、用户已听见或任何动作已经执行。
- 任一窗口观察结果不一致，只提示调用者丢弃旧输入/旧计划并重新读取本地事实；本批不自动循环、不自动重建、不自动重试、不写回状态。
- 消费入口不重新读取当前事实；窗口中的观察结果仍可能在返回后过期，调用者必须显式获取新的观察快照。
- router snapshot 仍是进程内元数据比对，不代表 Provider、网络、PCM、设备或远端状态。
- 本批未接入真实 Provider/设备执行、自动动作、自动状态回写、隐私探测/冷却恢复，也未进入 P8。

## 5. 下一批建议边界

下一新会话继续停留在 P7 第 7 项剩余本地编排边界。应继续选择一个单独的、纯本地只读子批次；优先围绕多窗口不一致结果的调用者边界或观察窗口中最小稳定原因摘要，但不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch83-routing-rebuild-observation-window-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 83 已完成第 7 项的一个有界本地子批次：新增冻结 TTSRoutingRebuildObservationWindowReview；新增 TTSRoutingCoordinator.consume_rebuild_observation_window，仅消费调用者已取得的至少两个冻结 TTSRoutingRebuildInput 观察快照，逐个复用单次一致性回执，全部一致才返回 RETAIN_READ_ONLY，否则返回 DISCARD_AND_REOBSERVE；拒绝可变/不足两个元素的观察集合；不重新读取事实、不自动重建、不执行动作、不写回事实、不保存回执。路由专项 33、TTS 207、全量 552 passed，compileall、140 个 Python 文件 UTF-8 审计与公开导入 smoke test 通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```