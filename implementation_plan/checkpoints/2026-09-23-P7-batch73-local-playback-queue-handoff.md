# P7 Batch 73：本地播放队列与背压交接（2026-09-23）

## 1. 接续依据与批次范围

接续 `2026-09-23-P7-batch72-playback-facts-handoff.md`。本批按 `implementation_plan/implementation-roadmap.md` 中 P7 实现批次第 6 项“队列句数、PCM 时长预算、背压、取消和清空”推进；Batch 72 的停止文字在本次用户澄清后按阶段路线理解为**每次完成一个实际批次就停止并交接**，不是要求用户每批重新选题。项目目录的每次写入均通过正式权限升级执行。

## 2. 完成的工作

- 新增 `app/tts/playback/queue.py`：进程内、同步、无 payload 的单回复 FIFO 播放队列；`PlaybackQueueEntry` 为冻结元数据，绑定 request/reply/segment/sequence/generation 和 lifecycle 中的音频块 ID，附带调用者显式报告的待播 `audio_duration_ms`。
- 默认最多 3 句、报告时长合计最多 8000ms；容量或时长超额抛 `PlaybackQueueBackpressure`，不插队、不消耗句序。预算可在构造时显式配置，拒绝非正整数和 bool。时长仅为**调用者报告的预算值**，没有 PCM payload，也未校验实际音频时长。
- 入队前检查 request 句序、回复身份、Provider generation、生命周期失败/取消、`audio_produced` 与当前 request 音频块绑定；相同 request 不重复入队。`begin` 只允许队首单句活动并再次核实生命周期和代际，不生成播放事实；`finish` 必须观察到 Batch 72 playback registry 显式记录的 `PLAYED` 或 `ABORTED`，随后才释放预算。ABORTED 清空同轮待播并禁止旧回复再入队。
- `cancel(reply_id)` 原子清空当前回复队列并禁止旧回复再入队；不伪造 playback.abort、设备停止、Provider 取消或用户听见。队列见空后可开始下一新回复；历史 request 不能重播。
- 在 `app/tts/__init__.py` 导出公开队列类型。新增 `tests/tts/test_playback_queue.py` 10 项专项测试：预算与背压失败原子性、FIFO 与终态释放、取消/ABORTED 清空、跨 request 音频块拒绝、代际栅栏、生命周期失败和 `audio_produced` 门控、下一回复进入、冻结数据等。

## 3. 验证

- 新增队列专项：**10 passed**。先复现模块不存在的红灯，随后复现“audio_produced 之前可入队”的断言红灯，再修正门控并重跑通过；不把红灯计为通过。
- TTS：**164 passed**；全量：**509 passed**（均用项目嵌入式 Python `-B`、项目根加入 `sys.path` 的 unittest discovery）。
- `compileall`（app 和 tests）：**passed**。
- 严格 UTF-8(-sig)、AST、NUL、U+FFFD 和连续四问号审计：`app`、`tests` 共 **137** 个 Python 文件，**0 failures**。
- 本批未执行 Git add/commit/push；仓库 `main` 仍无首次提交，原有未跟踪工作树未被清理或重置。

## 4. 边界、未完成项和强制停止

本批的 `begin/finish/cancel` 都是**本地队列控制**，不是播放设备回执或实际取消确认；`PLAYED` 仍是调用者提交的本地事实，非用户听见证明。缺少 PCM 和音频时长实测，故 8000ms 为报告值上限，真实设备/音频测量须留到后续实际集成。取消活动句后，真实设备停止、Provider 取消及 playback.abort 由未来编排层另行负责；不能声称已实现。没有实现真实 Provider、网络、音频 payload、播放设备、P8 或阶段验收；未修改路线图、`task_plan.md`、`progress.md`、P5 语义或旧交接文件。

**本批到此停止。** 下一新会话从 P7 实现批次第 7 项“VoiceStudio→Edge→文字降级；半句已提交/已播放时不混声、不重播”选一个与现有本地元数据契约匹配的有界子批次开始；不得把当前队列契约描述为已经解决实际设备停止或真实 PCM 预算。第 7 项工作量大，优先从本地路由/降级状态契约切入，统一测试与交接，不因批次号重复 Batch 66–71 的代际矩阵。写入本目录继续通过正式权限升级。

## 5. 下一新会话可复制续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续 <PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch73-local-playback-queue-handoff.md。按现有 implementation-roadmap.md 的 P7 既定顺序，Batch 73 已完成第 6 项的本地无 payload 队列契约，专项 10、TTS 164、全量 509 passed；队列时长只是调用者报告的预算值，cancel 不等于设备停止或 Provider 取消，PLAYED 不证明用户听见。直接开始下一有界批次（第 7 项 VoiceStudio→Edge→文字降级及半句不混声/不重播，先做适合当前边界的本地路由/降级状态契约），每次只完成一个批次就停止、验证并写新交接与可复制续接指令，无需再次向我征集新需求。不得擅改路线图/task_plan/progress/P5，不重复旧代际矩阵；对 <PROJECT_ROOT> 写入时使用正式权限升级，未获权限则停下说明，不要绕过。真实 Provider、网络、PCM、播放设备或 P8 均不得未经既定阶段边界直接接入。
```