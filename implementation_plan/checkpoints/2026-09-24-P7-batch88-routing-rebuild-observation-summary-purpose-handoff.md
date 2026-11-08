# P7 Batch 88：路由重建观察摘要固定用途标签交接

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 88
- 状态：complete
- 下一阶段：仍停留在 P7；不得进入 P8

## 1. 本批次边界

在 Batch 87 的 `SUMMARY_ONLY` 消费边界内，给多观察窗口的冻结最小摘要新增固定、纯本地、只读的用途标签 `window_stability_review`。该标签仅说明摘要可用于审阅调用者已提供窗口的稳定性；不表示当前事实、重建/动作许可或执行结果。未加入观察数据、时间过期或生命周期状态提示。

## 2. 已完成实现

- `app/tts/routing.py:42-46`：新增并公开 `TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW = "window_stability_review"`；只有这一种用途。
- `app/tts/routing.py:462-504`：`TTSRoutingRebuildObservationWindowSummary` 固定携带 `purpose`，构造后校验只接受该枚举成员；保持 `frozen=True, slots=True`、`consumer=SUMMARY_ONLY`、处置、计数、不一致原因及提示语义不变。不携带 `rebuild_input`、原始 `reviews`、观察快照、binding、ActionPlan 或动作状态。
- `app/tts/routing.py:1120`、`app/tts/__init__.py:53,101`：公开导出用途枚举。
- `tests/tts/test_routing_binding.py:594,614-645`：验证一致与漂移摘要的固定用途、公开字符串值、无法伪造或修改用途、摘要不暴露原始输入、现有回执投影一致，且调用前后 router snapshot 和 queue pending 不变。保留旧窗口非法输入拒绝测试。

## 3. 验证结果

使用项目嵌入式 Python 3.12、`-B` 和 `unittest`（嵌入式环境未装 `pytest`，通过显式 `sys.path` 加入项目根运行）：

- 路由绑定专项：**40 passed**；
- TTS 全套：**214 passed**；
- 全量测试：**559 passed**；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 解码、无 U+FFFD 且 AST 语法检查通过；AST 审计允许项目已有文件的 UTF-8 BOM（不修改它）；
- `app.tts` 公开导入与枚举值 smoke test：通过。

## 4. 文件变更

本批次只修改 `app/tts/routing.py`、`app/tts/__init__.py`、`tests/tts/test_routing_binding.py`，并新增本交接文件。未修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。未 commit、push、reset，也未配置 Git safe.directory；当前仓库仍有大量既有未跟踪文件。

## 5. 强制边界与未完成项

- `WINDOW_STABILITY_REVIEW` 仅是稳定的摘要用途标签，不表示已经读取最新本地事实；`SUMMARY_ONLY` 继续限定消费边界。
- `DISCARD_AND_REOBSERVE` 仍要求调用者显式丢弃旧输入与旧计划并重新观察；`RETAIN_READ_ONLY` 仍只说明已提供窗口内的一致性。该摘要不自动重新观察、自动重建、执行动作、写回事实或保存回执。
- 未提供可验证的时间来源与过期契约，因此不得猜测窗口是否过期；未添加生命周期状态提示。
- 不接真实 Provider、网络、PCM、播放设备、UI、ASR、隐私探测、冷却恢复或 P8；不得擅改路线图/task_plan/progress/P5。

## 6. 下一有界批次建议

继续停在 P7 第 7 项，仅在既有冻结契约能证明的前提下考虑独立的只读生命周期提示；如无法证明，不应伪造状态，可先做契约审计并停下。每次仅完成一个可验证、不可执行的本地子批次；不得重复 Batch 85–88，不得越过阶段边界。

## 7. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch88-routing-rebuild-observation-summary-purpose-handoff.md。Batch 88 已完成 P7 第 7 项的一个只读子批次：窗口摘要固定携带 TTSRoutingRebuildObservationPurpose.WINDOW_STABILITY_REVIEW = window_stability_review，与 SUMMARY_ONLY 并存，不能伪造或执行，不携带原始观察数据，不重读事实。路由专项 40、TTS 214、全量 559 passed；140 个 Python 文件 UTF-8/AST 与 app.tts 导入审计通过。直接继续下一个有界 P7 本地子批次，每次只做一个、验证并写新交接后停止；不要重复 Batch 85–88。没有可证明的时间/生命周期事实来源时，不得增加伪造的当前状态或过期提示。不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接文件；不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入都须正式权限升级，未获权限则停下说明；不要擅自 commit、push、reset 或配置 safe.directory。
```