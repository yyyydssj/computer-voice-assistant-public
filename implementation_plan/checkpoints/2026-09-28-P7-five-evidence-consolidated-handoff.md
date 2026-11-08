# P7 五项真实证据并行核验汇总与新会话验收交接（2026-09-28）

## 给新会话的首要指令

请先完整阅读本文件及下列五份独立 handoff，再逐项审阅对应 artifact。用户希望在新的 Codex 对话中亲自验收本轮结果。不要重放、不要重新合成、不要运行故障注入，也不要把任何 `None`/Blocked 项自动升级为通过。P7 保持 `Blocked`，不进入 P8。

本轮由五个独立子会话并行调查五个证据项；它们共用同一台 Windows 主机和音频设备，因此**并行会话不代表音频硬件测试天然互斥**。stop ACK 会话在收到停止进一步实测的消息之前已经完成了一次真实播放+reset；之后 acoustic 会话又执行一次尝试。时间重叠与共享设备风险应作为证据限制审阅。不得将 acoustic 无效读数解释为静音。

## 验收总览

| # | 证据项 | 本轮结果 | 验收口径 |
|---|---|---|---|
| 1 | Windows `waveOut STOP_ACKED` | 单次本机运行记录 `PLAYING → STOP_REQUESTED → STOP_ACKED`，确认 `backend_buffer_returned` | **有限通过：仅软件/backend buffer-return 协议**。非独立 WinMM trace；不证明扬声器静音或听感；并行设备活动需复核 |
| 2 | `STOP_TIMEOUT` / `UNKNOWN` 和真实 stop failure matrix | 未发现已支持的安全 fault shim；未注入 | **Blocked**。没有真实 timeout/unknown 观察；离线 fake 不计真实证据 |
| 3 | `speaker_output_stopped` 独立声学测量 | 做过一次尝试；麦克风播放与播放后全零，采集无效 | **Blocked / None**。无可用播放相关信号；原始录音因无效且可能有环境语音已删除 |
| 4 | `user_heard` | 没有用户本人对唯一播放批次的听感确认 | **None / 待用户本人确认**。自动化/执行者不能代答 |
| 5 | 真实 Agent → 回复原文 → VoiceStudio → Windows 设备 | 默认 Agent 为 `UnavailableAgentAdapter`；只验证默认安全回归/显式 opt-in seam | **Blocked**。无真实 Agent 回复及对应 TTS/设备事件；禁止云端、DeepSeek、Edge 和工具调用 |

## 五份独立交接文档

1. [waveOut STOP_ACKED](2026-09-28-P7-evidence-stop-acked-handoff.md)
2. [STOP_TIMEOUT / UNKNOWN 故障矩阵](2026-09-28-P7-evidence-stop-failure-handoff.md)
3. [speaker_output_stopped 声学测量](2026-09-28-P7-evidence-speaker-stop-handoff.md)
4. [user_heard 听感确认](2026-09-28-P7-evidence-user-heard-handoff.md)
5. [Agent → TTS → 设备端到端](2026-09-28-P7-evidence-agent-tts-device-handoff.md)

## Artifact 索引

- 已有本机合成 WAV：`artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav`
- 合成 WAV 摘要：`artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav.json`
- STOP_ACKED 事件记录：`artifacts/2026-09-28-P7-stop-acked/2026-09-28-P7-waveout-stop-acked.json`
- 声学无效测量摘要：`artifacts/2026-09-28-P7-speaker-stop-acoustic/mic-array-controlled-measurement.json`
- 声学原始录音：已删除；不保留私人环境音。

## 证据边界

- `STOP_ACKED` 只表示这次运行中实现收到 `waveOutReset` 成功后的 buffer-return/`WOM_DONE` 回调确认。它不等于物理换能器停止，不等于环境声学静音，也不等于用户听到了。
- 声学尝试的播放/播放后输入数据全零，而播放前环境 RMS 有值；因此该采集链未能捕获播放相关输出。结论必须保持 `speaker_output_stopped=None`，不能从零数据推出扬声器已停止。
- `user_heard` 只能由用户本人对已识别的播放内容/批次明确确认。本轮没有该确认；保持 `None`。
- 离线测试通过、真实 VoiceStudio 成功基线、既有 WAV、默认安全接缝测试，都不能代替真实 Agent 到设备的绑定端到端链路。
- 全程边界：本机 VoiceStudio/Windows；费用 0 元；不得使用云端 Provider、DeepSeek、Edge TTS；不得调用工具产生电脑副作用。

## 新会话建议验收顺序

1. 先只读核对五份 handoff 和 JSON artifact 的一致性、时间戳、播放批次与事件顺序。
2. 询问用户是否将第 1 项限定通过（backend 协议层），同时明确不代表 acoustic/user heard。
3. 将第 2、3、5 项保留 Blocked；第 4 项等待用户针对可识别的新试听批次亲自确认。不要要求用户追忆未记录的历史播放。
4. 若用户选择继续补证，按顺序串行执行硬件相关操作；先解决独立声学采集路径，再单独进行 stop/fault 测试。无安全、可控、可恢复的 fault fixture 时，timeout/unknown 继续 Blocked。
5. Agent 端到端只有在存在被明确授权的本地真实 Agent 时才可执行；不得为拼凑证据开启 DeepSeek/云 Provider/Edge。
6. 更新总验收记录前重新检查失败回退、原文保留、默认 opt-in 关闭和工作区基线；不要批量 stage/commit。

## 最终状态

```text
L1 local VoiceStudio generation             Pass（此前已有单独成功基线）
L2 waveOut natural completion               Pass（此前已有）
L2 waveOut STOP_ACKED backend protocol      Observed once; limited evidence
L2 STOP_TIMEOUT / UNKNOWN                  Blocked
speaker_output_stopped                     None / not established
user_heard                                 None / awaiting user's own confirmation
Agent → exact reply → VoiceStudio → device  Blocked
P7                                         Blocked / incomplete
P8                                         Not entered
```

本轮没有满足 P7 全部阶段退出条件。该汇总用于新对话审阅，不是 P7 完成声明，也不授权新的播放、服务请求或故障注入。

## 2026-09-29 follow-up acceptance addendum

本次复核在不播放、不录音、不联网请求 Agent 的前提下完成了实现层补强：

### Implementation follow-up

- `app/audio/windows/capture.py`
  - `AudioEndpoint` 现在保留 `host_api_index`、`native_channels`、`native_sample_rate_hz`。
  - 独立证据路径只接受 Windows WASAPI loopback + Windows WASAPI 独立麦克风；MME/DirectSound 候选不会被误选。
  - Stereo Mix / What U Hear 仍被排除。
- `app/audio/windows/evidence_capture.py`
  - 真实 PyAudioWPatch 路径按端点原生格式解析；本机枚举确认 loopback 为 2 声道/48 kHz，独立 Realtek 麦克风为 2 声道/48 kHz。
  - loopback/麦克风仍采用有界、串行、cleanup fail-closed 采集；格式缺失、打开失败、短读、全零 active、cleanup 失败均保持 Blocked。
- `app/evidence/live_runner.py`
  - 新增显式 opt-in 的单进程 Agent → 原文 → VoiceStudio → device evidence contract。
  - 默认 inert；不创建 Agent、不联网、不播放、不自动设置 `user_heard`。
  - request/session/batch identity、文本/音频上限、端口契约、无效采集和异常均 fail-closed。
- 新增/补充离线回归覆盖：native endpoint format、非 WASAPI 拒绝、默认 inert、身份错配、无效采集、用户听感不推断。

### Verification

```text
python -m unittest discover -s tests -p 'test*.py' -q
Ran 808 tests in 2.453s
OK (skipped=2)

python -m compileall -q app tests        OK
git diff --check                           OK
```

### Live preflight boundary

- `http://127.0.0.1:3900/health`：VoiceStudio 本地服务可用，返回 version `0.5.6`；这只证明服务健康，不证明 Agent 链路或设备证据。
- `http://127.0.0.1:3901/health`：无可用本地 Agent 服务。
- 未执行新的真实播放、录音、Agent 请求、故障注入或用户听感确认。

### Acceptance status after this follow-up

```text
1. backend_stop_ack                 有限通过：既有 waveOut backend buffer-return artifact
2. STOP_TIMEOUT / UNKNOWN           controlled fixture only；真实硬件故障 Blocked
3. loopback_output_stopped          未建立真实播放相关证据；Blocked
4. acoustic_output_stopped          未建立有效独立声学证据；Blocked
5. speaker_output_stopped           None / Blocked
6. user_heard                       None；未由用户针对新 batch 明确确认
7. Agent → exact reply → TTS → device  本地 Agent 端口不可用；Blocked
P7                                  Blocked / incomplete
P8                                  Not entered
```

本增补不改变既有 artifact 的事实边界，也不将离线测试、VoiceStudio 健康或端点枚举升级为 P7 真实通过。

## 2026-09-29 Implementation Hardening and Acceptance Result

Without issuing a real Agent request, playback, recording, fault injection, or user-hearing confirmation, this follow-up completed the implementation-layer hardening for the P7 evidence chain. Changes stayed inside this project:

- `app/evidence/local_ports.py`
  - Added localhost/loopback-only local Agent and VoiceStudio adapters.
  - Agent uses one submission per request, rejects duplicate `request_id`, verifies response identity, and fails closed.
  - VoiceStudio uses bounded multipart `/generate` and validates mono/16-bit/24 kHz/non-empty/non-zero WAV bytes.
  - Added explicit `DeviceCaptureFacts` injection boundary. Natural completion, `STOP_ACKED`, `STOP_TIMEOUT`, `UNKNOWN`, or quiet capture cannot be promoted to acoustic-stop evidence.
  - `LocalWindowsDeviceEvidence` is inert by default and does not create a playback worker or touch a device without live opt-in.
- `app/evidence/live_runner.py`
  - Restored text and request/session ID validation before ledger creation.
  - Preserves Agent text, request/session/batch correlation, and fail-closed error handling; it never sets `user_heard` automatically.
- `tests/evidence/test_local_ports.py` and `tests/evidence/test_live_runner.py`
  - Cover local address restrictions, HTTP wire fields, exact reply handling, response identity mismatch, duplicate request IDs, WAV boundaries, inert defaults, natural completion, timeout/unknown states, and explicit capture-fact boundaries.

### Verification

```text
python -m unittest discover -s tests -p 'test*.py' -q
Ran 808 tests in 2.352s
OK (skipped=2)

python -m compileall -q app tests
OK

git diff --check
OK
```

### Real evidence still not established

- `local-agent.v1` `POST /chat` is an adapter contract and has not been confirmed by a real `127.0.0.1:3901` Agent service. Passing adapter tests is not a real Agent end-to-end pass.
- A healthy VoiceStudio `127.0.0.1:3900/health` endpoint does not establish the Agent -> exact reply -> VoiceStudio -> Windows device chain.
- No new real playback, correlated loopback capture, valid independent-microphone capture, hardware STOP_TIMEOUT/UNKNOWN fault, or user-hearing confirmation was performed.
- Therefore `backend_stop_ack` remains limited backend buffer-return evidence. `STOP_TIMEOUT / UNKNOWN`, `loopback_output_stopped`, `acoustic_output_stopped`, `speaker_output_stopped`, real Agent end-to-end evidence, and `user_heard` must not be upgraded.

### Final state

```text
P7 = Blocked / incomplete
P8 = Not entered
user_heard = None
```

No bulk stage, reset, clean, commit, or push was performed, and no VoiceStudio installation outside the project was modified.
