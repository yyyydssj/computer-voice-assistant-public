# P7 Batch 75：本地路由决定与 queue/playback/lifecycle 元数据绑定交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-24-P7-batch74-local-routing-fallback-handoff.md`。本批继续按 `implementation_plan/implementation-roadmap.md` 的 P7 第 7 项推进，只完成“路由决定与现有本地 queue/playback/lifecycle 元数据做最小绑定”这一有界批次；不改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

本批仍不接真实 Provider、网络、PCM、播放设备、UI、ASR 或 P8；不把本地动作提示描述为已经执行。

## 2. 完成的工作

- 扩展 `app/tts/routing.py`：
  - `TTSFallbackRouter.edge_failed(...)` 现在可以接收实际 `ProviderRequestPlaybackState`，但保留默认 `NOT_COMMITTED` 以兼容既有调用；Edge 故障仍只转文字，不尝试第三 Provider。
  - 新增冻结元数据 `TTSRoutingBinding`，绑定一次 `TTSRoutingDecision` 与 `provider_request_id`、`request_sequence`、`provider_generation`、lifecycle 状态、playback 状态、队列待播 request 列表和当前活动 request。
  - 新增只读观察/协调层 `TTSRoutingCoordinator`：在推进 VoiceStudio/Edge 故障路由前，校验 request 的 reply/segment/Provider 身份、当前活动 reply/route、Provider generation，以及 queue 的回复边界和活动句一致性；随后只返回 binding，不取消 Provider、不清队列、不停止设备、不写入 playback/lifecycle 事实。
  - 当前 VoiceStudio request 的 Provider 名称必须为 `voicestudio`，当前 Edge request 的 Provider 名称必须为 `edge`；身份或代际不一致时 fail closed，且不改变路由器状态。
- 扩展 `app/tts/__init__.py` 导出 `TTSRoutingBinding`、`TTSRoutingCoordinator`。
- 新增 `tests/tts/test_routing_binding.py`（6 项）：
  - 未提交播放事实绑定到 Edge，保留 lifecycle 与空队列观察；
  - 已提交但未开始的队列句绑定到 Edge，队列不被清理；
  - 已开始且活动的句绑定到文字，活动队列事实不被伪造停止；
  - reply/segment/request 身份错误 fail closed 且路由不变；
  - Edge 故障在错误 Provider 上 fail closed；
  - 不同 registry 绑定被拒绝；
  - `TTSRoutingBinding` 冻结不可变。

## 3. 验证

使用项目嵌入式 Python 3.12、`-B`，显式把项目根加入 `sys.path`：

- 路由绑定专项：**6 passed**。
- TTS 全套：**180 passed**。
- 全量：**525 passed**。
- `compileall`（`app` 和 `tests`）：passed。
- `app`/`tests` 共 **140** 个 Python 文件 UTF-8 严格读取审计：无 U+FFFD、无连续四问号、无 NUL。

## 4. 边界、未完成项和强制停止

- `TTSRoutingCoordinator` 只观察并绑定现有本地元数据；`TTSRoutingDecision.cancel_previous_provider_request`、`drop_remaining_segments` 仍是调用者动作提示，不是 Provider 已取消、队列已清空或设备已停止的确认。
- `PLAYBACK_STARTED`、`PARTIALLY_PLAYED`、`PLAYED`、`ABORTED` 仍是调用者提交的本地播放事实，不证明用户听见。
- 本批没有实现真实 VoiceStudio/Edge、第三 Provider、网络、音频 payload、PCM 实测时长、设备取消/停止、播放事实回写或自动编排执行。
- 本批到此停止；不得在本批交接基础上越过 P7 第 7 项剩余边界进入隐私探测/冷却恢复（第 8 项）或 P8。

## 5. 下一批建议边界

下一新会话仍从 P7 第 7 项剩余的本地编排边界开始，优先处理“binding 决定与 queue cancel / lifecycle cancel / playback abort 的调用者动作顺序契约”中的一个有界子批次；只允许建立本地、显式、可验证的动作计划或状态门控，不执行真实 Provider、网络或设备操作。继续保持：每次只完成一个批次，验证后停止并写交接。

## 6. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch75-routing-metadata-binding-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 75 已完成第 7 项的本地路由决定与 queue/playback/lifecycle 元数据最小绑定：新增 TTSRoutingBinding 与只读 TTSRoutingCoordinator；校验 reply/segment/request/Provider/generation/queue 边界；未提交或已提交未开始可形成 Edge 决定，已开始/部分播放/已播放/ABORTED 仍转文字；协调层不取消 Provider、不清队列、不停止设备、不写 playback/lifecycle 事实。专项 6、TTS 180、全量 525 passed，compileall 与 140 个 Python 文件 UTF-8 审计通过。直接开始下一有界批次，优先建立 binding 决定与 queue cancel / lifecycle cancel / playback abort 的调用者动作顺序契约；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```