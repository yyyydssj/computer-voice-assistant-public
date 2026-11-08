# P7 G0：退出条件差距与工作区基线交接（2026-09-27）

> 结论：G0 **只读盘点与文档登记完成**，不是 P7 阶段验收或后续批次授权。取证日期 2026-09-27。仅核查工作区内文档、源码、测试名与 Git 状态；不运行应用、不读凭据、不连接网络/Provider/设备、不修改应用代码。

## 1. 口径与证据级别

- `implementation_plan/implementation-roadmap.md` 第 459–501 行是 P7/TTS 的阶段标准，其中第 490–495 行列出五项退出条件；第 505 行起 P8 是独立阶段。P7-R 是路由/Provider 支线，不等于 TTS 阶段级验收。
- `implementation_plan/checkpoints/2026-09-27-P7-R-23-offline-preflight-startup-boundary-audit-handoff.md`：R-23 完成离线审计，真实 DeepSeek runtime、Agent、transport、network 仍为 NOT READY / STOP；先前 R-08 受限真实评估不等于当前默认运行时可用。
- `implementation_plan/checkpoints/2026-09-24-P7-batch95-routing-rebuild-observation-frozen-router-snapshot-boundary-audit-handoff.md` 记录当时 TTS 220 passed；R-23 记录当时全量 678/678 passed。**本轮没有重跑测试**，这两个计数均不是 G0 实测或真设备证据。
- `app/tts/playback/__init__.py` 的顶部契约明确：播放事实仅为本地元数据，不调用设备、不携带音频数据、不证明用户听到；状态名 `PLAYED` 不能代表实际设备播报。

## 2. 五项退出条件逐项核对

| 路线图条件 | 已有证据 | 缺口及后续证据入口 |
|---|---|---|
| 1. 文字回复按原序播放 | `app/tts/segmentation/__init__.py` 分句、顺序和取消；`app/tts/playback/queue.py` 连续顺序、背压；`tests/tts/test_segmentation.py` 与 `test_playback_queue.py` 本地测试。 | `app/main.py` 仍为文字壳，未连接已确认回复→分句→TTS→播放设备；需 T-02 假设备集成及 T-04 真设备原序证据。 |
| 2. 提交前可切 Edge，开始播放后当前句不换声 | `app/tts/routing.py` 的 `TTSFallbackRouter.primary_failed/edge_failed` 基于本地状态路由；`tests/tts/test_routing.py` 与 `test_routing_binding.py` 测试。 | 缺真实 VoiceStudio/Edge、设备提交回执和故障切换证据；T-03 先评审，T-04/T-05 再专项验收。 |
| 3. 打断的旧回复不跨 Provider 续播 | 分句取消、队列终态/旧回复隔离及路由丢弃剩余句的本地契约；`tests/tts/test_playback_queue.py` 等。 | 缺 Provider 请求取消、设备停止、迟到音频隔离的编排回执；T-02/T-05 假设备并在 T-04 核对真设备。 |
| 4. TTS 故障不改变任务、授权、计划、结果事实 | `app/tts/` 与既有 P5 安全链分离；`implementation_plan/checkpoints/2026-09-21-P5-integration-acceptance.md` 记录既有安全回归；文字入口保留。 | 不能因尚未接线就推断跨模块故障隔离已验收；需 T-02/T-05 故障注入、任务/授权/计划/账本/结果前后比较与 T-06 阶段回归。 |
| 5. VoiceProfile 只在允许的句子/回复边界生效 | `app/tts/domain/voice_profile.py` 有 USER 修改者限制、版本与会话快照；`tests/tts/test_voice_profile.py` 有本地测试。 | 无用户确认/试听与实际播放句/回复边界的集成证据；T-01 固定切换语义，T-02/T-05 验证，T-06 复核。 |

**阶段结论：五项都有局部契约或旧安全证据，但五项都缺阶段级完整验收；P7 未完成。** fake 测试、离线观察、音频生成均不能冒充设备播报或“用户已听到”。

## 3. 依赖关系和最小验收切片

```text
G0 盘点（本批，完成）
 ├─ T-01 冻结 P7 退出标准与测试矩阵（需单独确认）
 │   ├─ T-02 固定已确认文字→分句→路由→队列→假设备事实（需单批授权）
 │   └─ T-03 VoiceStudio/Edge/设备、凭据、隐私、计费前置评审（另行授权）
 │       └─ T-04 有界真播报及设备回执（再获专项授权）
 │   └─ T-05 故障降级/版本/任务事实回归（单批授权，次序由调研定）
 │       └─ T-06 汇总五项条件并独立验收（不自动进入 P8）
 └─ R-24 DeepSeek transport 文档决策门（独立支线，不阻塞 T-02）
     └─ R-25/R-26 均为条件批次，权限不得从 TTS 继承
```

- **推荐下一步 T-01**：冻结现有证据、假/真设备测试矩阵与非目标；之后 T-02 才能按单批方案做最小本地编排。建议 T-02 用固定非敏感文字，验证 `reply_id/segment_id/provider_request_id/profile_version` 身份一致、原序、背压、未配置 Edge、提前/部分失败、取消迟到、文字回退及安全链事实不变。该切片仅证实本地编排，不算 P7 真播报通过；具体文件、fixture、失败回退上限及测试须由 T-02 单独冻结并获授权。
- `implementation_plan/02-tts-voicestudio.md` 第 153–175 行要求五层事实分离和真机验证；`implementation_plan/research/tts-fallback-github-comparison.md` 记录 2026-09-18 固定版本开源比较。拟引入 SDK/Provider/设备前由 T-03 核查官方版本、接口与本机兼容性，本 G0 不作实时可用性断言。
- P7 不做播放期间监听、AEC、全双工打断、自动改 VoiceProfile、ASR 新增或 P8；R 支线真模型评估不能替代 TTS 验收。

## 4. Git 基线、文档漂移与本次验证

- 本轮写入前 Git HEAD：`f2b112fbf67f0d9621d1710d29ead0c360d38720`（2026-09-25）；状态已有 11 个 staged 新文件和一个 staged `app/routing.py` 修改，另有大量未跟踪文件，包括本计划与 R-23 handoff。以上先前基线**均不归属 G0**；禁止 bulk stage/stash/reset/commit。本批仅新增本 checkpoint，并更新 `implementation_plan/P7-followup-execution-tracker.md` 的 G0 台账行。
- `implementation_plan/README.md` 第 4、66、137 行仍写 P7/TTS 或意图路由停在未实施阶段；`task_plan.md` 顶部停在 R-09；路线图 P7-R 第 696 行的摘要只到 R-09。与新 handoff/源码有状态漂移；应另开状态对账批次，不在本批静默改历史。
- 本轮不新增 unittest/compileall 结果；仅校验本批文档 UTF-8、台账替换、引用的本地路径与 Git 精确范围；历史 TTS 220 / 全量 678 不写成新实测。

## 5. 停止点

G0 完成的是差距盘点与文档登记。**P7 阶段未完成，最终检查清单全不勾选。** T-01、R-24 均未开始；后续需各自调研、展示边界并取得单批明确授权。不得借本交接访问真实 Provider、读取密钥、操作设备、修改应用代码或进入 P8。