# P7 Batch 90：路由重建观察跨请求身份边界只读审计交接（2026-09-24）

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 90（一个纯本地、只读契约审计子批次）
- 状态：complete（身份边界审计通过；不新增应用契约）
- 下一阶段：仍停留在 P7，不进入 P8

## 1. 本批次边界与结论

接续 `2026-09-24-P7-batch89-routing-rebuild-observation-lifecycle-contract-audit-handoff.md`。本批次只审计一个不同于 Batch 85–89 的边界：当重建输入与观察结果的 `provider_request_id`、`reply_id`、`segment_id` 发生跨请求身份漂移时，现有只读窗口消费是否稳定归类为 `REQUEST_IDENTITY`，是否要求丢弃并重新观察，以及最小摘要是否避免泄漏外部身份值。

结论：现有契约满足该边界。未修改应用代码、未增加时间或生命周期事实、未增加“当前”或过期提示，继续保持 Batch 88 的 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW`。本批次只增加专项回归测试和本交接，不把身份分类解释为动作许可、重建结果或事实持续有效性证明。

## 2. 验证的契约事实

- 以既有本地 `TTSRoutingRebuildInput` 为基准，仅替换观察输入的 `provider_request_id`、`reply_id`、`segment_id` 为外部身份值；不接入 Provider、网络、PCM、设备或自动动作。
- `TTSRoutingCoordinator.validate_rebuild_observation(...)` 返回 `INCONSISTENT`，且唯一 mismatch 为 `TTSRoutingRebuildMismatch.REQUEST_IDENTITY`。
- 将基准输入与身份漂移输入组成两次观察窗口后，`consume_rebuild_observation_window(...)` 返回 `DISCARD_AND_REOBSERVE`，摘要只保留 `REQUEST_IDENTITY` 分类。
- `TTSRoutingRebuildObservationWindowSummary.notice` 不包含外部 `provider_request_id`、`reply_id` 或 `segment_id` 的具体值；摘要仍是 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 的只读投影。
- router snapshot、queue pending、lifecycle snapshot 和 playback snapshot 在消费前后保持相等；未执行动作、未重读事实、未写回事实。

## 3. 文件变更

仅修改：

- `tests/tts/test_routing_binding.py`：新增一个跨请求身份边界专项测试。
- `implementation_plan/checkpoints/2026-09-24-P7-batch90-routing-rebuild-observation-identity-boundary-audit-handoff.md`：新增本交接。

未修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5、Batch 85–89 或任何旧交接文件。未 commit、push、reset，也未配置 Git `safe.directory`。

## 4. 验证结果

使用项目嵌入式 Python 3.12 与 `-B`，显式加入项目根：
- Batch 90 新增身份边界专项测试：41 passed（包含新增测试）。
- TTS 全套：215 passed；全量测试：560 passed。
- `app.tts` 公开导入及 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 值 smoke test：通过。
- `app`/`tests` 共 140 个 Python 文件严格 UTF-8 解码、无 U+FFFD、AST 解析通过。
- 本交接在上述验证完成后视为 complete。

## 5. 强制停止与下一步

本批次不进入 P8，不接真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、自动状态回写、隐私探测/冷却恢复，也不新增时间源、过期判断或生命周期“当前”状态。

验证并记录结果后停止。下一批仍须是一个不同的、可验证的纯本地 P7 有界子批次；如无安全候选，明确停下，不凭空扩充状态或摘要。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch90-routing-rebuild-observation-identity-boundary-audit-handoff.md。Batch 90 是 P7 第 7 项的一个纯本地只读契约审计子批次：验证跨请求 provider_request_id/reply_id/segment_id 身份漂移稳定归类为 REQUEST_IDENTITY，窗口处置为 DISCARD_AND_REOBSERVE，摘要不泄漏外部身份值且不改变 router/queue/lifecycle/playback；不新增时间、生命周期“当前”或过期提示，保持 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW。下一次只做一个不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选，明确停下而不是造状态。不得重复 Batch 85–90，不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接文件；不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入都须正式权限升级，未获权限则停下说明；不得擅自 commit、push、reset 或配置 safe.directory。
```
