# P7 Batch 93：路由重建观察播放状态边界只读审计交接（2026-09-24）

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 93（一个纯本地、只读契约审计子批次）
- 状态：complete（播放状态边界审计通过；不新增应用契约）
- 下一阶段：仍停留在 P7，不进入 P8

## 1. 本批次边界与结论

接续 `2026-09-24-P7-batch92-routing-rebuild-observation-identity-version-composite-boundary-audit-handoff.md`。本批次只审计与 Batch 85–92 不同的边界：调用者显式提供的冻结观察仅发生 `playback_state` 漂移时，既有只读比较和观察窗口能否稳定返回 `PLAYBACK` 分类与丢弃后重新观察的提示，最小摘要是否保持不携带原始快照且不泄漏测试使用的播放状态值。

结论：既有契约满足上述边界。只新增一个专项回归测试和本交接，不修改应用契约，不重读事实，不接播放设备，也不证明当前播放状态或允许动作。

## 2. 可复核的契约事实

- 以本地 `TTSRoutingRebuildInput` 为基准，由测试调用者构造 `replace(rebuild_input, playback_state=PLAYBACK_STARTED)`；身份、版本、生命周期、队列和 router snapshot 字段保持原值。该状态仅是测试提供的冻结输入，不是对真实播放状态的观测。
- `validate_rebuild_observation(...)` 返回 `INCONSISTENT`，唯一 mismatch 是 `PLAYBACK`，不误报其他分类。
- 基准输入与播放状态漂移输入组成两次观察窗口，`consume_rebuild_observation_window(...)` 返回 `DISCARD_AND_REOBSERVE`；`summarize_rebuild_observation_window(...)` 与回执摘要相等，`observation_count=2`，mismatches 只有 `PLAYBACK`。
- 摘要继续固定 `consumer=SUMMARY_ONLY` 和 `purpose=WINDOW_STABILITY_REVIEW`；摘要 notice 不含测试使用的播放状态值，摘要 slots 不携带 `observed`、`rebuild_input` 或 `reviews`。
- router snapshot、queue pending、lifecycle snapshot、playback/registry snapshot 在比较、窗口消费和摘要前后相等。测试不执行重建、cancel、abort、播放、重试或状态回写。
- `PLAYBACK` 分类仅描述调用者已传入的冻结观察与基准快照的差异，不是设备播放结果、当前状态、动作许可、最新事实证明或事实持续有效性保证。

## 3. 文件变更与验证

仅修改/新增：

- `tests/tts/test_routing_binding.py`：从第 658 行开始新增 `test_rebuild_observation_playback_boundary_discards_without_leaking_or_mutating`。
- `implementation_plan/checkpoints/2026-09-24-P7-batch93-routing-rebuild-observation-playback-boundary-audit-handoff.md`：本交接。

使用项目嵌入式 Python 3.12、`-B`，显式将项目根加入 `sys.path` 验证：

- 修改前路由专项基线：43 passed；修改后路由专项：44 passed。
- TTS 全套：218 passed；全量测试：563 passed。
- `app`/`tests` 共 140 个 Python 文件严格 UTF-8 解码（兼容已有 BOM）、无 U+FFFD、AST 解析通过。
- `app.tts` 公开导入及 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 值 smoke test 通过。

未修改 `app/tts/routing.py` 或其他应用代码；未修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 或 Batch 85–92 旧交接。未接真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、状态回写、隐私探测或冷却恢复；未新增时间源、过期判断或生命周期“当前”状态。未执行 commit、push、reset 或 Git `safe.directory` 配置；仓库既有未跟踪文件不归于本批次。

## 4. 强制停止与下一步

本批次在验证并记录结果后停止，仍在 P7，不进入 P8。不得把 `PLAYBACK` mismatch 当作播放设备回执、自动重建许可、动作许可、最新事实或生命周期当前结论。

下一批若需继续，只能选择与 Batch 85–93 不同、可验证、纯本地、只读且有界的 P7 子批次；如无安全候选，应明确停下，不凭空扩充状态、摘要或动作边界。对本项目后续写入仍须正式权限升级。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch93-routing-rebuild-observation-playback-boundary-audit-handoff.md。Batch 93 仅审计调用者提供的冻结 playback_state 单独漂移时的 PLAYBACK 只读分类：窗口 DISCARD_AND_REOBSERVE，最小摘要不携带原始快照且不在 notice 中输出测试状态值，router/queue/lifecycle/registry 不改变，保持 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW；不证明真实播放、当前事实、时间或过期。路由专项 44、TTS 218、全量 563 passed；140 个 Python 文件 UTF-8/AST 和 app.tts 导入审计通过。下一次只做一个与 Batch 85–93 不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选明确停下。不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接，不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入须正式权限升级；不得擅自 commit、push、reset 或配置 safe.directory。
```
