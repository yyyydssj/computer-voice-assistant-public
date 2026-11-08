# P7 Batch 92：路由重建观察身份+版本复合边界只读审计交接（2026-09-24）

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 92（一个纯本地、只读契约审计子批次）
- 状态：complete（身份与版本复合边界审计通过；不新增应用契约）
- 下一阶段：仍停留在 P7，不进入 P8

## 1. 本批次边界与结论

接续 `2026-09-24-P7-batch91-routing-rebuild-observation-version-boundary-audit-handoff.md`。本批次选择一个不同于 Batch 85–91 的复合边界：当调用者提供的冻结观察同时发生跨请求身份漂移和请求版本漂移时，现有只读比较是否稳定聚合既有不一致分类、窗口是否继续要求丢弃并重新观察，以及最小摘要是否继续避免泄漏身份值和版本数值。

结论：现有契约满足该复合边界。仅新增一个专项回归测试及本交接，不修改应用契约，不增加时间、过期判断、生命周期“当前”结论、状态字段或动作行为。

## 2. 可复核的契约事实

- 从既有本地 `TTSRoutingRebuildInput` 出发，构造一个调用者显式提供的冻结观察：同时替换 `provider_request_id`、`reply_id`、`segment_id`，并同时递增 `request_sequence`、`provider_generation`；其他生命周期、播放、队列和 router snapshot 字段保持原值。
- `validate_rebuild_observation(...)` 返回 `INCONSISTENT`，且 mismatch 按现有 `TTSRoutingRebuildMismatch` 契约顺序稳定聚合为 `(REQUEST_IDENTITY, REQUEST_VERSION)`；不误报生命周期、播放、队列或 router 分类。
- 由基准输入和复合漂移输入构成两次观察窗口，`consume_rebuild_observation_window(...)` 的处置为 `DISCARD_AND_REOBSERVE`；`summarize_rebuild_observation_window(...)` 与回执摘要相等，`observation_count=2`，仅保留上述两类 mismatch。
- 最小摘要的 `consumer=SUMMARY_ONLY`、`purpose=WINDOW_STABILITY_REVIEW` 不变；摘要 notice 不包含测试使用的外部身份值或漂移版本数值。
- router snapshot、queue pending、lifecycle snapshot、playback/registry snapshot 在调用前后相等；本测试不执行重建、cancel、abort、播放、重试或状态回写。
- 该复合分类只描述调用者已传入窗口与基准快照的比较，不是动作许可、重新获取最新事实的证明、Provider 结果、执行结果或事实持续有效性保证。

## 3. 文件变更与验证

本批次仅新增/修改：

- `tests/tts/test_routing_binding.py`：新增 `test_rebuild_observation_identity_and_version_boundary_aggregates_without_leaking_or_mutating`，覆盖身份与版本同时漂移的分类聚合、窗口处置、摘要边界和无突变约束。
- `implementation_plan/checkpoints/2026-09-24-P7-batch92-routing-rebuild-observation-identity-version-composite-boundary-audit-handoff.md`：本交接。

未修改：

- `app/tts/routing.py` 及其他应用代码；
- `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 或 Batch 85–91 旧交接。

使用项目嵌入式 Python 3.12、`-B`，并显式将项目根加入 `sys.path` 验证：

- 路由专项：43 passed。
- TTS 全套：217 passed。
- 全量测试：562 passed。
- `app`/`tests` 共 140 个 Python 文件严格 UTF-8 解码（兼容已有 BOM）、无 U+FFFD、AST 解析通过。
- `app.tts` 公开导入及 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 值 smoke test 通过。

未接入真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、状态回写、隐私探测或冷却恢复；未新增时间源、过期判断或生命周期“当前”状态。未执行 commit、push、reset 或 Git `safe.directory` 配置。

## 4. 强制停止与下一步

本批次在验证并记录结果后停止，仍在 P7。不得进入 P8，不得把身份+版本复合 mismatch 解释为动作许可、自动重建许可、最新事实证明或生命周期当前结论。

下一批如需继续，只能再选择一个与 Batch 85–92 不同、可验证、纯本地、只读且有界的 P7 子批次；如无安全候选，应明确停下，不凭空扩充状态、摘要或动作边界。对本项目的任何后续写入仍须正式权限升级。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch92-routing-rebuild-observation-identity-version-composite-boundary-audit-handoff.md。Batch 92 仅审计 provider_request_id/reply_id/segment_id 与 request_sequence/provider_generation 同时漂移的 REQUEST_IDENTITY + REQUEST_VERSION 复合只读边界：窗口 DISCARD_AND_REOBSERVE，摘要按既有契约顺序聚合两类 mismatch 且不泄漏身份或版本数值，router/queue/lifecycle/playback 不改变，维持 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW；无时间、过期或生命周期“当前”结论。路由专项 43、TTS 217、全量 562 passed；140 个 Python 文件 UTF-8/AST 和 app.tts 导入审计通过。下一次仅做一个与 Batch 85–92 不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选明确停下。不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接，不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入须正式权限升级；不得擅自 commit、push、reset 或配置 safe.directory。
```