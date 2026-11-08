# P7 Batch 91：路由重建观察请求版本边界只读审计交接（2026-09-24）

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 91（一个纯本地、只读契约审计子批次）
- 状态：complete（版本边界审计通过；不新增应用契约）
- 下一阶段：仍停留在 P7，不进入 P8

## 1. 本批次边界与结论

接续 `2026-09-24-P7-batch90-routing-rebuild-observation-identity-boundary-audit-handoff.md`。仅审计与 Batch 90 的跨请求身份漂移不同的版本边界：当调用者提供的冻结观察里 `request_sequence`、`provider_generation` 单独或同时漂移，原有只读比较能否只归类为 `REQUEST_VERSION`、窗口能否要求丢弃并重新观察、摘要是否避免输出具体版本数值。结论：现有契约满足该边界；仅新增专项测试及本交接，不修改应用契约，不增加时间、过期判断或“当前生命周期”证明。

## 2. 可复核的契约事实

- 从既有本地 `TTSRoutingRebuildInput` 出发，分别只替换 `request_sequence`、只替换 `provider_generation`、同时替换二者，所有其他字段保持原值；这些冻结输入由测试调用者显式提供，不接入真实 Provider、网络、PCM、设备或自动动作。
- 三个子场景各自调用 `validate_rebuild_observation(...)` 均得到 `INCONSISTENT`，且唯一 mismatch 为 `TTSRoutingRebuildMismatch.REQUEST_VERSION`；不误报 `REQUEST_IDENTITY` 或其他分类。
- 由基准输入和版本漂移输入构成两次观察窗口，`consume_rebuild_observation_window(...)` 的处置均为 `DISCARD_AND_REOBSERVE`。`summarize_rebuild_observation_window(...)` 与回执摘要相等，`observation_count=2` 且仅保留 `REQUEST_VERSION`。
- 最小摘要的 `consumer=SUMMARY_ONLY`、`purpose=WINDOW_STABILITY_REVIEW` 不变，`notice` 不含测试使用的漂移版本数值。router snapshot、queue pending、lifecycle snapshot、playback snapshot 在调用前后相等。
- 摘要只描述已传入窗口与基准的比较；版本漂移分类不是动作许可、重新获取最新事实的证明或事实持续有效性保证。

## 3. 文件变更与验证

仅修改：

- `tests/tts/test_routing_binding.py`：在原 658 行插入一个版本边界专项测试（含三个子场景）。
- `implementation_plan/checkpoints/2026-09-24-P7-batch91-routing-rebuild-observation-version-boundary-audit-handoff.md`：新增本交接。

使用项目嵌入式 Python 3.12、`-B`、显式加入项目根：

- 修改前路由专项基线：41 passed；修改后路由专项：42 passed。
- TTS 全套：216 passed；全量测试：561 passed。
- `app`/`tests` 共 140 个 Python 文件严格 UTF-8 解码（兼容已有 BOM）、无 U+FFFD、AST 解析通过；`app.tts` 公开导入及 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 值 smoke test 通过。

未修改应用代码、`implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 或 Batch 85–90 等已有交接。仓库现有未跟踪文件不归于本批次；未 commit、push、reset 或配置 Git `safe.directory`。

## 4. 强制停止与下一步

本批次在验证并记录结果后停止，仍在 P7。不得进入 P8，或接真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复；不得新增时间源、过期判断或生命周期“当前”状态。

下一批如需继续，只选择一个与 Batch 85–91 不同且可证明的纯本地 P7 有界子批次；如无安全候选，应明确停下，不凭空扩充状态或摘要。对本项目的任何写入须正式权限升级；未获权限则停下说明。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch91-routing-rebuild-observation-version-boundary-audit-handoff.md。Batch 91 仅审计 request_sequence/provider_generation 单独或同时漂移的 REQUEST_VERSION 只读边界：窗口 DISCARD_AND_REOBSERVE，摘要不泄漏版本数值，router/queue/lifecycle/playback 不改变，维持 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW；无时间、过期或生命周期“当前”结论。路由专项 42、TTS 216、全量 561 passed；140 个 Python 文件 UTF-8/AST 和 app.tts 导入审计通过。下一次仅做一个与 Batch 85–91 不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选明确停下。不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接，不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入须正式权限升级；不得擅自 commit、push、reset 或配置 safe.directory。
```