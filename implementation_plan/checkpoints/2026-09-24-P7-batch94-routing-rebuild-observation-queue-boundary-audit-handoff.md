# P7 Batch 94：路由重建观察队列状态边界只读审计交接（2026-09-24）

## 1. 本批次边界与结论

本批次严格接续 `2026-09-24-P7-batch93-routing-rebuild-observation-playback-boundary-audit-handoff.md`，只审计一个与 Batch 85–93 不同的纯本地 P7 有界子批次：调用者提供的冻结 `queue_pending_provider_request_ids` / `queue_active_provider_request_id` 单独漂移时，路由重建观察比较、窗口消费和最小摘要是否保持稳定的只读队列分类。

结论：既有契约满足该边界。仅新增一个专项回归测试和本交接，不修改应用契约，不重读事实，不执行队列动作，不接真实 Provider、网络、PCM、设备或自动动作执行器。

## 2. 可复核的契约事实

- 以本地 `TTSRoutingRebuildInput` 为基准，由测试调用者使用 `replace(rebuild_input, queue_pending_provider_request_ids=("queue-foreign-pending",), queue_active_provider_request_id="queue-foreign-active")` 构造冻结观察输入；身份、版本、生命周期、playback 和 router snapshot 字段保持原值。队列值是测试提供的冻结输入，不是对真实队列当前状态或执行结果的证明。
- `validate_rebuild_observation(...)` 返回 `INCONSISTENT`，唯一 mismatch 是 `TTSRoutingRebuildMismatch.QUEUE`，不误报身份、版本、生命周期、播放或 router 分类。
- 基准输入与队列状态漂移输入组成两次观察窗口，`consume_rebuild_observation_window(...)` 返回 `DISCARD_AND_REOBSERVE`；`summarize_rebuild_observation_window(...)` 与回执摘要相等，`observation_count=2`，mismatches 只有 `QUEUE`。
- 摘要继续固定 `consumer=SUMMARY_ONLY` 和 `purpose=WINDOW_STABILITY_REVIEW`；摘要不携带原始快照，且 notice 不输出测试使用的 `queue-foreign-pending` 或 `queue-foreign-active` 值。摘要 slots 不含 `observed`、`rebuild_input` 或 `reviews`。
- router snapshot、queue pending、lifecycle snapshot、playback/registry snapshot 在比较、窗口消费和摘要前后相等。测试不执行 enqueue、begin、cancel、清空、重建、播放、重试或状态回写。
- `QUEUE` 分类仅描述调用者已传入的冻结观察与基准快照的差异，不是当前队列事实、队列执行结果、动作许可、自动重建许可、最新事实证明或事实持续有效性保证。

## 3. 文件变更与验证

仅修改/新增：

- `tests/tts/test_routing_binding.py`：新增 `test_rebuild_observation_queue_boundary_discards_without_leaking_or_mutating`，覆盖队列 pending/active 冻结输入同时漂移时的唯一分类、窗口处置、摘要最小边界、测试值不泄漏和无突变约束。
- `implementation_plan/checkpoints/2026-09-24-P7-batch94-routing-rebuild-observation-queue-boundary-audit-handoff.md`：本交接。

验证使用项目嵌入式 Python 3.12、`-B`，显式将项目根加入 `sys.path`：

- 路由绑定专项：**45 passed**；
- TTS 全套：**219 passed**；
- 全量测试：**564 passed**；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 解码（兼容已有 BOM）、无 U+FFFD、AST 解析通过；
- `app.tts` 公开导入及 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 值 smoke test：通过。

未修改 `app/tts/routing.py` 或其他应用代码；未修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 或 Batch 85–93 旧交接。未接真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、状态回写、隐私探测或冷却恢复；未新增时间源、过期判断或生命周期“当前”状态。未执行 commit、push、reset 或 Git `safe.directory` 配置；仓库既有未跟踪文件不归于本批次。

## 4. 强制停止与下一步

本批次在验证并记录结果后停止，仍在 P7，不进入 P8。不得把 `QUEUE` mismatch 当作当前队列事实、队列操作回执、自动重建许可、动作许可、最新事实或生命周期当前结论。

下一批若需继续，只能选择与 Batch 85–94 不同、可验证、纯本地、只读且有界的 P7 子批次；如无安全候选，应明确停下，不凭空扩充状态、摘要或动作边界。对本项目后续写入仍须正式权限升级。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch94-routing-rebuild-observation-queue-boundary-audit-handoff.md。Batch 94 仅审计调用者提供的冻结 queue_pending_provider_request_ids / queue_active_provider_request_id 单独漂移时的 QUEUE 只读分类：窗口 DISCARD_AND_REOBSERVE，最小摘要不携带原始快照且不在 notice 中输出测试队列值，router/queue/lifecycle/registry 不改变，保持 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW；不证明当前队列事实、执行结果、时间或过期。路由专项 45、TTS 219、全量 564 passed；140 个 Python 文件 UTF-8/AST 和 app.tts 导入审计通过。下一次只做一个与 Batch 85–94 不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选明确停下。不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接，不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入须正式权限升级；不得擅自 commit、push、reset 或配置 safe.directory。
```