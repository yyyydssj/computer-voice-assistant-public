# P7 Batch 89：路由重建观察窗口时间/生命周期契约只读审计交接

- 日期：2026-09-24
- 阶段：P7 第 7 项剩余本地编排边界
- 批次：Batch 89（一个只读契约审计子批次）
- 状态：complete（审计结论：现有窗口契约不足以证明时间过期或当前生命周期状态；不新增提示）
- 下一阶段：仍停留在 P7，不进入 P8

## 1. 本批次边界与结论

只读审计 Batch 88 的 `WINDOW_STABILITY_REVIEW` / `SUMMARY_ONLY` 观察窗口契约，判断能否在不伪造事实的前提下增加独立的生命周期或过期提示。结论：**不能证明**。本批次不修改应用代码、测试代码或任何现有交接文件，不新增状态、时间字段、时间源、推断规则或自动行为。此次记录的是可验证的缺失，不把它解释为“已过期”“未过期”或“当前生命周期健康”。

## 2. 证据与约束

- `app/tts/routing.py:339-384`：`TTSRoutingRebuildInput` 包含调用者显式观察的 `lifecycle_state` 等本地快照字段，但不含观察时间、有效期、时钟或读取后事实持续有效的证明。
- `app/tts/routing.py:462-506`：冻结 `TTSRoutingRebuildObservationWindowSummary` 仅有 `disposition`、`observation_count`、`mismatches`、固定 `consumer=SUMMARY_ONLY` 和固定 `purpose=WINDOW_STABILITY_REVIEW`；`notice` 仅描述已提供窗口的一致性与调用者责任，没有过期结论或当前状态。
- `app/tts/routing.py:785-808,915-948`：显式 `observe_rebuild_input` 可读取当时本地事实，`consume_rebuild_observation_window` / `summarize_rebuild_observation_window` 只消费调用者传入的冻结观察；窗口摘要消费本身不重读事实。`mismatches` 中的 `LIFECYCLE` 只说明窗口与基准快照不一致，不等于某个已验证的当前生命周期状态。
- `app/tts/lifecycle/__init__.py:18-28,442-464`：生命周期有本地状态和快照读取能力，但不会为已冻结的路由观察窗口提供观察时间或事后有效性保证。对 `app/tts` 下所有 Python 文件的 `expires_at`、`observed_at`、`created_at`、`monotonic`、`datetime`、`time.time`、`time_source` 字面用法的只读扫描为 0 项；此扫描只是辅助证据，不替代上述类型和调用边界审计。
- 因此不能从计数、窗口一致性、进程时间或普通 lifecycle 枚举推断窗口过期性，也不能将已观察状态标为“当前”。继续保留 Batch 88 的原有只读摘要，不添加无法证明的生命周期提示。

## 3. 验证

使用项目嵌入式 Python 3.12 与 `-B`，通过 `unittest` 显式加入项目根：

- `tests.tts.test_routing_binding`：40 passed；
- TTS 全套：214 passed；
- 全量测试：559 passed；
- `app`/`tests` 共 140 个 Python 文件严格 UTF-8 解码、无 U+FFFD、AST 解析通过（审计兼容已有 UTF-8 BOM）；
- `app.tts` 公开导入，`WINDOW_STABILITY_REVIEW` 与 `SUMMARY_ONLY` 的公开值 smoke test：通过。

## 4. 文件变更与下一停止点

仅新增本交接；没有改动 `implementation-roadmap.md`、`task_plan.md`、`progress.md`、P5、Batch 85–88 或任何已有文件。未 commit、push、reset 或配置 Git safe.directory；不把仓库既有未跟踪文件归于本批次。

下次仍在 P7 第 7 项选择**一个**可验证、只读且不可执行的本地子批次；在存在可证明的观察时间/生命周期事实契约之前，不引入“当前”“已过期”“未过期”等提示。如无新的安全有界候选，可以继续停在此处而不是凭空扩充摘要。不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch89-routing-rebuild-observation-lifecycle-contract-audit-handoff.md。Batch 89 是 P7 第 7 项的一个只读契约审计子批次：现有观察快照/窗口摘要不含可证明的时间与持续有效性事实，不添加生命周期“当前”状态或过期提示，保持 Batch 88 的 SUMMARY_ONLY / WINDOW_STABILITY_REVIEW。路由专项 40、TTS 214、全量 559 passed；140 个 Python 文件 UTF-8/AST 和 app.tts 导入审计通过。下一次只做一个不同的、可验证的纯本地 P7 有界子批次，验证并写新交接后停止；如无安全候选，明确停下而不是造状态。不得重复 Batch 85–89，不得擅改 implementation-roadmap.md、task_plan.md、progress.md、P5 或旧交接文件；不得接真实 Provider、网络、PCM、设备、自动动作执行器、自动重建、状态回写、隐私探测/冷却恢复或 P8。对 <PROJECT_ROOT> 的任何写入都须正式权限升级，未获权限则停下说明；不得擅自 commit、push、reset 或配置 safe.directory。
```