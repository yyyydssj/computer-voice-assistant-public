# P7 Batch 87：路由重建观察摘要消费边界标识交接

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 87
- 状态：complete
- 下一阶段：仍停留在 P7；不得进入 P8

## 1. 本批次边界

本批次只为既有多观察窗口最小摘要增加一个固定的、不可执行的消费边界标识，使调用者能够明确识别该对象是 `summary_only` 投影，而不是原始窗口回执、重建输入、ActionPlan、重建许可、动作许可、Provider 结果或执行结果。

本批次没有引入时间过期判断，也没有伪造生命周期状态。没有接入真实 Provider、网络、PCM、播放设备、UI、ASR、自动动作执行器、自动重建、自动状态回写、隐私探测或冷却恢复。

## 2. 已完成实现

### 2.1 新增固定消费边界枚举

修改 `app/tts/routing.py:37`，新增：

```python
class TTSRoutingRebuildObservationConsumer(str, Enum):
    SUMMARY_ONLY = "summary_only"
```

该枚举只有 `SUMMARY_ONLY` 一个值，明确表示摘要只能作为最小只读投影消费；它不提供任何执行能力，也不暗示 Provider、设备或远端状态。

### 2.2 摘要携带消费边界

修改 `app/tts/routing.py:456` 的冻结 `TTSRoutingRebuildObservationWindowSummary`：

- 新增 `consumer` 字段，固定为 `TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY`；
- 在 `__post_init__` 中拒绝非 `SUMMARY_ONLY` 的伪造值；
- 保持 `disposition`、`observation_count`、`mismatches`、`consistent`、`requires_reobservation` 和 `notice` 既有语义不变；
- 仍为 `frozen=True, slots=True`，不保存 `rebuild_input`、原始 `reviews`、观察快照、binding、ActionPlan 或动作状态。

修改 `app/tts/routing.py:1110` 与 `app/tts/__init__.py:52,99`，公开导出该枚举。

### 2.3 专项测试

修改 `tests/tts/test_routing_binding.py:18,597-620`：

- 验证显式摘要消费入口返回的摘要带有 `SUMMARY_ONLY` 消费边界；
- 验证公开字符串值为 `summary_only`；
- 验证摘要仍与既有窗口回执的 `summary` 投影一致；
- 验证摘要不暴露 `rebuild_input` 或 `reviews`；
- 验证调用前后 router snapshot 与 queue pending 不变；
- 保留并通过既有单观察、list 输入和非法对象窗口输入拒绝测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12，并使用 `-B`：

- 路由绑定专项：**39 passed**；
- TTS 全套：**213 passed**；
- 全量测试：**558 passed**；
- `compileall(app, tests)`：通过；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 审计：通过，无解码失败、无 U+FFFD；
- `app.tts` 公开导入 smoke test：通过。

## 4. 文件变更

本批次修改：

- `<PROJECT_ROOT>\app\tts\routing.py`
- `<PROJECT_ROOT>\app\tts\__init__.py`
- `<PROJECT_ROOT>\tests\tts\test_routing_binding.py`
- 本交接文件

没有修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

## 5. 强制边界与未完成项

- `TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY` 只是调用者消费边界标识，不是重建许可、动作许可、Provider 结果或执行结果。
- 摘要仍来自调用者已经提供的冻结观察窗口；不会重新读取 lifecycle、playback、queue 或 router 当前事实。
- 调用者若得到 `DISCARD_AND_REOBSERVE`，仍必须显式丢弃旧输入与旧计划并重新观察；本方法不会代办。
- 调用者若得到 `RETAIN_READ_ONLY`，也只表示窗口内观察结果一致，不代表 Provider、网络、PCM、设备或远端状态可用。
- 未实现观察窗口的时间过期模型；后续如要处理过期语义，必须先在既有本地契约中明确可验证的时间来源与只读状态，不得用当前时间猜测。
- 不得修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。
- 不得进入 P8。
- 不要 push；当前 Git 状态仍有大量未跟踪文件，且不应擅自配置 safe.directory、commit 或 reset。

## 6. 下一批次建议

继续停留在 P7 第 7 项剩余本地编排边界。下一批优先选择一个独立、纯本地、只读且不可执行的子批次，例如：

- 在既有 `SUMMARY_ONLY` 边界内增加不携带原始观察数据的稳定用途标签；或
- 在已有契约可证明的前提下，增加只读生命周期状态提示。

但不得重复实现 Batch 85/86/87，不得自动重建、执行动作、回写事实、接入真实 Provider/设备或进入 P8。

## 7. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch87-routing-rebuild-observation-summary-consumer-boundary-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 87 已完成第 7 项的一个有界本地子批次：新增 TTSRoutingRebuildObservationConsumer.SUMMARY_ONLY，并让 TTSRoutingRebuildObservationWindowSummary 固定携带该消费边界标识；摘要仍只读、不可执行、不携带原始观察数据、不重读事实、不自动重建、不执行动作、不写回事实、不保存回执。路由专项 39、TTS 213、全量 558 passed，compileall、140 个 Python 文件 UTF-8 审计与 app.tts 公开导入 smoke test 通过。直接开始下一有界批次，继续停留在 P7 第 7 项剩余本地编排边界；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```