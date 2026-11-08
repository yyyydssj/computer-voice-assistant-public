# P7 Batch 74：本地 VoiceStudio→Edge→文字路由/降级交接（2026-09-24）

## 1. 接续依据与批次范围

接续 `implementation_plan/checkpoints/2026-09-23-P7-batch73-local-playback-queue-handoff.md`。本批按 `implementation_plan/implementation-roadmap.md` 的 P7 第 7 项推进，但只实现适配当前本地元数据边界的路由/降级状态契约：不接真实 Provider、网络、PCM、播放设备或 P8；不修改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

## 2. 完成的工作

- 新增 `app/tts/routing.py`：同步、进程内、无副作用的 `TTSFallbackRouter`、`TTSRoute`、`TTSRoutingAction`、不可变 `TTSRoutingDecision` 和 `TTSRoutingSnapshot`。
- 固定本地路由顺序为 VoiceStudio → 已配置 Edge → 文字；Edge 只有在构造时提供合法 `edge_voice_profile_id` 时才可用，不会临时选择陌生音色。
- 回复边界栅栏：同一 reply 活动期间不得中途启动另一 reply；降级只生成调用者动作，不调用 Provider、不停止设备、不提交播放事实。
- VoiceStudio 故障：
  - 当前播放状态为 `NOT_COMMITTED` 或 `COMMITTED_NOT_STARTED` 且已配置 Edge 时，允许当前完整句切到 Edge；该决定标记取消旧 Provider request、丢弃当前回复剩余旧句，并明确不重播当前句。
  - 已 `PLAYBACK_STARTED`、`PARTIALLY_PLAYED`、`PLAYED` 或 `ABORTED` 时，转为文字；不在半句中混声、不整句重播，并丢弃当前回复剩余旧句。
  - 未配置 Edge 时直接转文字；不尝试第三 Provider。
- Edge 故障：同一当前 segment 可进入文字模式一次；重复 Edge 故障、错误 reply 或错误路由均 fail closed；不尝试第三 Provider。
- 恢复边界：`primary_recovered()` 不切换当前活动 reply；只有结束当前 reply 后，下一新 reply 才恢复 VoiceStudio。Edge 或文字已输出的旧内容不重播。
- `app/tts/__init__.py` 导出上述公开类型。

## 3. 验证

- 路由专项：**10 passed**。
- TTS 全套：**174 passed**。
- 全量：**519 passed**。
- `compileall`（`app` 和 `tests`）：passed。
- 本批新增/修改 Python 文件严格 UTF-8（无 U+FFFD、无连续四问号）；全 `app`/`tests` Python 文件 UTF-8 审计通过。
- 使用项目嵌入式 Python 3.12.10、`-B`，并显式将项目根加入 `sys.path`；没有把任何红灯计为通过。

## 4. 边界、未完成项和强制停止

本批只建立本地路由决定和状态快照。`cancel_previous_provider_request=True` 是调用者应采取的动作提示，不是 Provider 已取消确认；`drop_remaining_segments=True` 是队列编排意图，不等于设备已停止；`PLAYED` 仍不证明用户听见。没有音频 payload、PCM 时长实测、网络调用、真实 VoiceStudio/Edge、第三 Provider、播放设备或 P8。

本批到此停止。下一批仍从 P7 第 7 项剩余的本地编排边界开始，优先把路由决定与现有 queue/playback/lifecycle 元数据做最小绑定；不得把本批描述为已接通真实 Provider 或已完成设备取消。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-24-P7-batch74-local-routing-fallback-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 74 已完成第 7 项的本地 VoiceStudio→Edge→文字路由/降级状态契约；专项 10、TTS 174、全量 519 passed，compileall 与 UTF-8 审计通过。路由仅返回本地动作提示：未开始播放且已配置 Edge 才允许当前句切 Edge，半句已开始/部分播放/已播放/ABORTED 均转文字、不混声、不重播并丢弃当前回复剩余旧句；Edge 失败不尝试第三 Provider；恢复不切当前 reply，只影响下一新 reply。直接开始下一有界批次，优先把该路由决定与现有 queue/playback/lifecycle 元数据做最小绑定；每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；真实 Provider、网络、PCM、播放设备或 P8 均不得未经既定阶段边界直接接入。对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。
```
