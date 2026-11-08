# P7 双路径执行文档：§24 Definition of Done 对账（2026-09-30）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 对象文档：`implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md`（§24 Definition of Done，14 项）
- 结论：**14 项全部处理完毕**——13 项满足；第 14 项"默认模式切换"经用户 2026-09-30 审阅后**决定保持 `disabled`（不切换为 `auto`）**，该条约束（未经审阅不得切换）已满足、切换被明确否决，故标记为已处理（理由见第 1 节第 14 行与第 3 节）。
- 用户听感证据（第 11 项）已于 2026-09-30 采集：真实播放两句（主路径 VoiceStudio 小饼声线 + 备用声线离线 Edge 槽），**用户确认两句都听到**（`user_heard=true`，未做声学测量）。
- 本文只做状态对账，不改变任何代码或默认值；P7 整体仍为 `Blocked`，未进入 P8。

## 1. 逐项对账

| # | §24 条目 | 状态 | 证据 |
|---|---|---|---|
| 1 | `http_wav` 模式稳定按句生成，不等待约 100 字整段 | ✅ | [Batch 2](2026-09-30-P7-voicestudio-batch2-http-wav.md)、[Batch 5](2026-09-30-P7-voicestudio-batch5-transport-and-config.md)；[B9 真实采集](2026-09-30-P7-b9-real-p95-measurement.md)（真实 20 句 paced，20/20 播完） |
| 2 | `ws_pcm` 模式通过 fake 和真实 VoiceStudio 协议测试 | ✅ | [Batch 3](2026-09-30-P7-voicestudio-batch3-websocket-client.md)（37 项 + 真实三句串行 preflight：连接数 1、`done.samples` 与实收一致） |
| 3 | `auto` 模式实现 WS→HTTP 的首音前一次性降级 | ✅ | [Batch 5 真实环境验收](2026-09-30-P7-voicestudio-batch5-transport-and-config.md) 阶段 B（真实建连失败 → 本句一次 HTTP 接管） |
| 4 | VoiceStudio 两条路径失败后正确进入 **Edge/文字** | ✅ | 文字：[B10](2026-09-30-P7-batch10-real-transport-faults.md)（真实连接拒绝 → `NOT_COMMITTED` → `text/continue_text_no_replay`，0 句播放）；**Edge：[B11](2026-09-30-P7-b11-offline-edge-voice.md)**（真实杀后端 → `route=edge/use_edge_current_segment`、`edge_attempts=1`、`edge_used@generation1`、该句由离线声线真的播出） |
| 5 | PCM worker 在真实 Windows 设备上完成播放、停止和资源回收 | ✅ | [Batch 4](2026-09-30-P7-voicestudio-batch4-pcm-playback.md)（三 buffer 生命周期、真实 `waveOutWrite` 复用、真实 stop 回收）、[Batch 7](2026-09-30-P7-voicestudio-batch7-real-scenario-matrix.md) |
| 6 | barge-in 后旧 generation 音频无法进入新回复 | ✅ | [Batch 6](2026-09-30-P7-voicestudio-batch6-barge-in-cancellation.md)（离线 generation fence）+ [Batch 7](2026-09-30-P7-voicestudio-batch7-real-scenario-matrix.md)（真实播放中取消：11 次 write 后 1 秒仍为 11 次，`late_events=["playback:STOP_ACKED@generation2"]`） |
| 7 | 播放后故障不重播、不半句换声 | ✅ | [B10](2026-09-30-P7-batch10-real-transport-faults.md) `proxy_drop_mid_sentence`：`PLAYBACK_STARTED` → `replay_current_segment=False`、`drop_remaining_segments=True`；[B11](2026-09-30-P7-b11-offline-edge-voice.md) `proxy_drop_no_edge`：已出音时 `edge_attempts=0` |
| 8 | 所有新增配置有范围校验、默认值和回滚说明 | ✅ | [Batch 5](2026-09-30-P7-voicestudio-batch5-transport-and-config.md)（`TTS_*` 范围校验、`public_summary` 不暴露 URL）、[Batch 8](2026-09-30-P7-voicestudio-batch8-rollback-and-stage-acceptance.md)（`disabled/http_wav` 一键回滚、`ws_pcm/auto` 需显式注入）、[B11](2026-09-30-P7-b11-offline-edge-voice.md)（`TTS_EDGE_VOICE` 默认空 = 未配置） |
| 9 | 新增依赖已固定版本、wheel hash、许可证和离线安装方式 | ✅ | [Batch 8](2026-09-30-P7-voicestudio-batch8-rollback-and-stage-acceptance.md)：`websockets==17.1`、wheel 名与 SHA-256、BSD-3-Clause、`>=3.11`、离线安装命令；**B11 未新增任何依赖**（离线 SAPI） |
| 10 | TTS 专项、集成和全量回归通过 | ✅ | 2026-09-30 复跑：TTS discovery **550 OK (3 skipped)**、全量 **1065 OK (3 skipped)**、`compileall -q app tests tools` exit 0、`git diff --check` exit 0 |
| 11 | 真实测试 artifact、指标和**用户听感证据**按 batch 独立记录 | ✅ | artifact 与指标按批独立记录；**用户听感证据**：[原始播放 artifact](artifacts/2026-09-30-P7-user-heard/p7-user-heard-20260930T112602Z-f840ea69258a46d2.json)（主路径 1 句完整播放 5.4 s；备用声线 32 块 `COMPLETED` 6.7 s）+ [确认件](artifacts/2026-09-30-P7-user-heard/2026-09-30-P7-user-heard-confirmation.json)（`user_heard=true`，主路径与备用声线都听到；`speaker_acoustically_verified=false`，仅人为确认） |
| 12 | `progress.md`、`task_plan.md`、P7 tracker 和最终 checkpoint 已更新 | ✅ | 同日完成：`progress.md` 新增 2026-09-30（续）条目；`task_plan.md` 新增收口条目并更正过时陈述；tracker 新增 §8/§9/§10 与第 3/9/10 项状态；本 checkpoint |
| 13 | 未修改 VoiceStudio 本体、用户 VoiceProfile、历史 artifact 或任务安全语义 | ✅ | 本批全部改动只涉及 `app/tts/**`（含新增 `clock.py`/`edge_sapi*`）、`app/config/settings.py`（仅新增 `TTS_EDGE_VOICE`）、测试、文档；未改 `app/main.py`、授权/账本/执行/审计链；未改用户 VoiceProfile（live 选择文件未被写入） |
| 14 | **用户审阅验收后**，才把默认模式从 `http_wav` 切换为 `auto` | ✅ 已决策（不切换） | 用户 2026-09-30 审阅后决定**保持 `TTS_VOICESTUDIO_MODE=disabled`**：语音继续 opt-in，不切成 `auto`。本条约束（未经用户审阅不得切换）已满足；同时澄清文档原假设 `http_wav` 与仓库实际默认 `disabled` 不一致。默认值未改动（`git diff` 可见 `settings.py` 只新增了 `TTS_EDGE_VOICE`） |

## 2. 本轮补充完成的三件事（对应上表 4、10、12）

- **B11 离线 Edge 声线**（第 3 项 tracker / DoD 第 4 项的 Edge 半）：真实杀 VoiceStudio 后，未出音的句子由本机 `Microsoft Huihui Desktop` 声线合成并真的经 waveOut 播出；已出音的句子绝不切 Edge。
- **B9/B9-fix**：19.2 四条性能指标由"未证明"变为**真实通过**（`ready_to_submit` p95 0.236/0.173/0.200 ms；`gate_to_device` p95 ≤0.029 ms；`auto` vs `http_wav` 中位起播延迟不恶化）。
- **文档收口**：`progress.md`、`task_plan.md`、tracker、本 checkpoint。

## 3. 剩余项处置（两项均已关闭）

1. **用户听感证据（第 11 项）——已关闭**：2026-09-30 真实播放两句（主路径 VoiceStudio 小饼声线；备用声线离线 Edge 槽），用户确认**两句都听到**；`user_heard=true` 记入[确认件](artifacts/2026-09-30-P7-user-heard/2026-09-30-P7-user-heard-confirmation.json)。注意：这是**一次人为确认**，不是声学测量（`speaker_acoustically_verified=false`），不得推广到其它文本、音量、设备或环境。
2. **默认模式切换（第 14 项）——已决策：不切换**：用户 2026-09-30 决定保持 `TTS_VOICESTUDIO_MODE=disabled`（opt-in）。因此不涉及"默认开启语音"，也不需要先做流式会话接入默认 runtime；tracker 第 5/6 项保持未做并注明原因。
3. P7 阶段退出条件（路线图五项）与 P8 仍需另行授权；`Blocked` 状态不因本文改变。

## 3.1 已知取舍（如实登记）

- **Edge 渲染粒度**：离线 SAPI 的 `stream_sentence` 是"先完整渲染 WAV、再按 4800 samples 分块 yield"（单句约 0.4 s），所以"渲染中途取消"不是即时 kill 子进程，而是**有界超时回收 + 丢弃已渲染音频 + 清理临时文件**；无遗留进程，但长文本取消时延未测量。
- **Edge 槽的语义边界**：它只在本句**尚未出音**且路由判定 `USE_EDGE_CURRENT_SEGMENT` 时启用，且每句只尝试一次；已出音（`PLAYBACK_STARTED`）一律转文字，不重播、不换声。
- **在线 edge-tts 未实现**：本批按用户选择实现"离线 Windows 声线"，微软云 Edge TTS 属另一批，需先给出依赖/许可/出网与隐私方案。

## 4. 复核命令

```powershell
<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q   # 1065 OK (3 skipped)
<PROJECT_ROOT>\.venv\Scripts\python.exe -m compileall -q app tests tools                       # exit 0
git diff --check                                                                                       # exit 0
```
