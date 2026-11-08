# P7 真实 Agent → 原文 → VoiceStudio → Windows 设备补充交接

- 日期：2026-09-29
- 范围：只读审计既有真实批次；未重新调用 Agent、VoiceStudio、播放、录音或故障注入
- 结论：第 5 项的真实链路已建立；该批次的停止确认字段仍然未闭合，因此 P7 仍为 `Blocked / incomplete`

## 1. 真实批次

- artifact：`<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-agent-tts-device\p7-d4afff8c3ad3402e.json`
- `batch_id`：`p7-d4afff8c3ad3402e`
- `request_id`：`p7-real-20260929-request-8000a324c52f4e69af00548952403677`
- `session_id`：`p7-real-20260929-session-4fbc0b160aef4ac3abb9f22ff33b5b2a`
- Agent provider：`deepseek-official`
- Agent model：`deepseek-v4-flash`
- Harness profile：`sdk-minimal`
- VoiceStudio：`http://127.0.0.1:3900`
- Voice profile：`e8572e8e`

## 2. 一致性审计结果

| 检查项 | 结果 | 依据 |
|---|---|---|
| batch identity | PASS | 根对象、`metadata.device_evidence.diagnostics.batch_id` 均为 `p7-d4afff8c3ad3402e` |
| request identity | PASS | 根对象、VoiceStudio diagnostics、device diagnostics 三处一致 |
| session identity | PASS | `metadata.session_id` 与 device diagnostics 一致；事件中的 RUN_CREATED/AGENT_REQUEST_SUBMITTED 也携带同一 session |
| Agent 原文 | PASS | `metadata.agent_reply_text = 确认：这是一次真实语音助手证据批次。` |
| 原文哈希 | PASS | `source_text_sha256 = 232477a810ded5ae8be58cbde5f5a15966d43327222cb6ee0713d4b957ed767d` |
| VoiceStudio 音频哈希 | PASS | `audio_sha256 = 0fd927c795e3d8b294aadc48f915cd4113eed0064e657861f4451321c94b636c`；`metadata.audio_id` 与 device diagnostics 同值 |
| 设备采集协议 | PASS | `p7-synchronized-independent-wasapi.v1`；loopback index 13，独立麦克风 index 11，均为 WASAPI |
| 播放状态 | PASS（仅限播放完成） | `PLAYING → COMPLETED`；`capture_valid = true`；这不等同于 STOP_ACKED 或物理静音 |
| 事件顺序 | PASS | `RUN_CREATED → AGENT_REQUEST_SUBMITTED → AGENT_REPLY_ACCEPTED → VOICESTUDIO_AUDIO_ACCEPTED → DEVICE_EVIDENCE_ACCEPTED → user_heard_confirmed` |
| 用户听感 | PASS（精确 batch） | 用户明确确认听到 `batch_id=p7-d4afff8c3ad3402e`；artifact 中 `user_heard = true` |

## 3. 必须保留的边界

该真实 Agent batch 的设备停止字段仍为：

```text
backend_stop_ack          = null
loopback_output_stopped   = false
acoustic_output_stopped   = false
speaker_output_stopped    = false
status                    = blocked
blocked_reasons           = backend_stop_ack_missing, speaker_output_stop_not_established
```

因此，本 checkpoint **不能**把该 batch 标为完整通过，也不能把 `COMPLETED` 推断为 `STOP_ACKED` 或物理扬声器静音。

独立声学批次：

- artifact：`<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-speaker-stop-acoustic\p7-acoustic-20260929-1c3751cbde2daf2d.json`
- `batch_id`：`p7-acoustic-20260929-1c3751cbde2daf2d`
- 该批次的 `loopback_output_stopped`、`acoustic_output_stopped`、`speaker_output_stopped` 为 `true`，且用户听感只绑定这个精确 batch。

上述两个 batch **不得合并**。独立声学 batch 的停止事实不能回填或覆盖 `p7-d4afff8c3ad3402e` 的旧设备字段；Agent batch 的用户听感也不能扩展为独立声学 batch 的用户听感。

## 4. 第五项验收判定

```text
真实 Agent → 原文 → VoiceStudio → Windows 设备链路：Established
精确原文/音频/设备身份关联：Passed
精确 batch 的 user_heard：Passed
该 batch 的 backend STOP_ACK：Blocked
该 batch 的 loopback/acoustic/speaker stop：Blocked
第 5 项整体：Real chain established, stop closure incomplete
P7：Blocked / incomplete
```

## 5. 下一步

1. 保留上述 artifact 与本补充交接，不重跑已完成的真实 Agent 批次。
2. 若要闭合第 5 项停止字段，必须针对同一真实 Agent batch 建立可复核的 backend stop ACK 与独立声学停止事实；不能跨 batch 借用结果。
3. 若要建立 `STOP_TIMEOUT / UNKNOWN`，需要安全、可复核且真实触达 Windows 音频后端的 fault shim；当前不存在可用接口，因此继续保持 `Blocked`。
4. 不修改 2026-09-28 历史 handoff 的原始结论；本文件作为 2026-09-29 的补充事实记录。

## 6. 未执行的操作

- 未重复调用 DeepSeek Harness
- 未重复调用 VoiceStudio
- 未重新播放或录音
- 未执行故障注入
- 未杀进程、替换 DLL、修改 VoiceStudio 配置或破坏音频设备
- 未 stage、reset、clean、commit 或 push