# P7 Batch 95：冻结 router snapshot 路由重建观察边界只读审计交接（2026-09-24）

## 1. 本批次边界与结论

严格接续 `2026-09-24-P7-batch94-routing-rebuild-observation-queue-boundary-audit-handoff.md`。本批次仅审计与 Batch 85–94 不同的一个纯本地、有界场景：由调用者提供的冻结 `TTSRoutingRebuildInput.router_snapshot` 单独漂移时，重建观察比较、窗口消费与最小摘要是否稳定保持只读 `ROUTER` 分类。本批次不改变实际 router 状态；与已有基于实际路由状态漂移的测试不同。

结论：既有契约满足该边界。只增加专项回归测试和本交接，不修改应用契约，不调用重新观察事实的入口，不执行路由/队列动作，不接真实 Provider、网络、PCM、播放设备或自动执行器。

## 2. 可复核的契约事实

- 使用本地 `TTSRoutingRebuildInput` 作为基准；测试调用者通过 `replace(rebuild_input, router_snapshot=replace(rebuild_input.router_snapshot, active_reply_id="router-foreign-reply"))` 构造冻结观察输入。反向替换 snapshot 后与基准输入相等，确保身份、版本、生命周期、playback 和 queue 字段未漂移。测试值是调用者提供的冻结输入，不是实际路由器当前状态。
- `validate_rebuild_observation(...)` 返回 `INCONSISTENT`，唯一 mismatch 为 `TTSRoutingRebuildMismatch.ROUTER`，不误报请求身份、版本、生命周期、播放或队列分类。
- 基准输入与冻结路由漂移输入形成两次观察窗口；`consume_rebuild_observation_window(...)` 返回 `DISCARD_AND_REOBSERVE`；`summarize_rebuild_observation_window(...)` 与回执摘要相等，`observation_count=2`，mismatches 仅有 `ROUTER`。
- 摘要固定 `consumer=SUMMARY_ONLY`、`purpose=WINDOW_STABILITY_REVIEW`；摘要 notice 不输出测试用的 `router-foreign-reply`；slots 不含 `observed`、`rebuild_input` 或 `reviews`，不携带原始快照。
- 比较、窗口消费及摘要前后的实际 router snapshot、queue pending/active、lifecycle snapshot、playback/registry snapshot 保持相等。测试通过前后读取这些本地对象验证无突变；被测比较和消费路径不调用 `observe_rebuild_input`，不执行 enqueue、begin、cancel、路由切换、重建、播放、重试或状态回写。
- `ROUTER` 仅描述调用者提供的冻结快照之间的差异；不证明实际 router 当前事实、动作回执、自动重建许可、最新状态、观察时间或事实持续有效性。

## 3. 文件变更与验证

仅修改/新增：

- `tests/tts/test_routing_binding.py`：第 701 行起新增 `test_rebuild_observation_frozen_router_snapshot_boundary_discards_without_leaking_or_mutating`。
- `implementation_plan/checkpoints/2026-09-24-P7-batch95-routing-rebuild-observation-frozen-router-snapshot-boundary-audit-handoff.md`：本交接。

使用项目嵌入式 Python 3.12、`-B`，显式将项目根加入 `sys.path`，结果如下：

- 修改前路由专项基线：**45 passed**；修改后：**46 passed**；
- TTS 全套：**220 passed**；全量测试：**565 passed**，无失败、错误或跳过；
- `app`/`tests` 共 **140** 个 Python 文件严格 UTF-8 解码（兼容已有 BOM）、无 U+FFFD、AST 解析通过；
- `app.tts` 公开导入及 `SUMMARY_ONLY` / `WINDOW_STABILITY_REVIEW` 值 smoke test：通过。

未修改 `app/tts/routing.py` 或其他应用代码，未修改 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5 或 Batch 85–94 旧交接。未接真实 Provider、网络、PCM、播放设备、自动动作执行器、自动重建、状态回写、隐私探测或冷却恢复；未新增时间源、过期判断或生命周期“当前”状态。未执行 commit、push、reset 或 Git `safe.directory` 配置；仓库既有未跟踪文件不归于本批次。

## 4. 强制停止与下一步

本批次在验证并记录后停止，仍在 P7，不进入 P8。不得将冻结 `ROUTER` mismatch 当成实际路由器当前状态、路由执行回执、动作/自动重建许可、最新事实或生命周期当前结论。

下一批如继续，只选择与 Batch 85–95 不同、可验证、纯本地、只读且有界的 P7 子批次；如无安全候选应明确停下，不凭空扩充状态、摘要或动作边界。对本项目后续写入仍须正式权限升级。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch95-routing-rebuild-observation-frozen-router-snapshot-boundary-audit-handoff.md。Batch 95 仅审计调用者提供的冻结 router_snapshot 单独漂移时的 ROUTER 只读分类：窗口 DISCARD_AND_REOBSERVE，最小摘要不携带原始快照且 notice 不输出测试的 router-foreign-reply，实际 router/queue/lifecycle/registry 不改变，保持 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW；不证明当前路由事实、执行结果、时间或过期。路由专项 46、TTS 220、全量 565 passed；140 个 Python 文件 UTF-8/AST 和 app.tts 导入审计通过。下一次只做一个与 Batch 85–95 不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选明确停下。不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接；不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入须正式权限升级；不得擅自 commit、push、reset 或配置 safe.directory。
```
