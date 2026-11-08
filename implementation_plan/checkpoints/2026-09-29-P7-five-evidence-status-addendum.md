# P7 五项真实证据状态补充（2026-09-29）

本文件是对以下 2026-09-28 历史 handoff 的补充，不改写其历史记录：

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-28-P7-five-evidence-consolidated-handoff.md`
- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-28-P7-five-evidence-completion-handoff.md`

## 当前总判定

```text
P7 = Blocked / incomplete
P8 = Not entered
```

原因不是 Agent 链路缺失，而是：

1. 真实 `STOP_TIMEOUT / UNKNOWN` 仍没有安全、可复核、真实触达 Windows 音频后端的故障注入证据。
2. 真实 Agent batch `p7-d4afff8c3ad3402e` 的 backend stop ACK 和该 batch 的声学停止字段没有闭合。

## 五项证据当前状态

| 证据 | 当前状态 | 真实依据与边界 |
|---|---|---|
| `waveOut STOP_ACKED` | 有限通过 | `<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-28-P7-stop-acked\2026-09-28-P7-waveout-stop-acked.json`；只证明 backend buffer-return 协议，不证明物理扬声器静音 |
| `STOP_TIMEOUT / UNKNOWN` | Blocked | 只有 controlled fault fixture；没有真实 Windows 音频后端 fault shim |
| `loopback_output_stopped` / `acoustic_output_stopped` / `speaker_output_stopped` | 有限通过，绑定独立声学 batch | `p7-acoustic-20260929-1c3751cbde2daf2d` 的三个字段均为 `true`；不得回填 Agent batch |
| `user_heard` | 已建立，但逐 batch 绑定 | `p7-acoustic-20260929-1c3751cbde2daf2d` 与 `p7-d4afff8c3ad3402e` 均有用户明确确认；确认不能跨 batch 扩展 |
| Agent → 原文 → VoiceStudio → Windows 设备 | 真实链路已建立，但停止闭合不完整 | `p7-d4afff8c3ad3402e` 的 identity、原文、音频哈希、设备协议、事件顺序均一致；该 batch 的 stop 字段仍为 Blocked |

## 真实 Agent 链路批次

- artifact：`<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-agent-tts-device\p7-d4afff8c3ad3402e.json`
- request：`p7-real-20260929-request-8000a324c52f4e69af00548952403677`
- session：`p7-real-20260929-session-4fbc0b160aef4ac3abb9f22ff33b5b2a`
- Agent reply：`确认：这是一次真实语音助手证据批次。`
- `source_text_sha256`：`232477a810ded5ae8be58cbde5f5a15966d43327222cb6ee0713d4b957ed767d`
- `audio_sha256`：`0fd927c795e3d8b294aadc48f915cd4113eed0064e657861f4451321c94b636c`
- 事件顺序：`RUN_CREATED → AGENT_REQUEST_SUBMITTED → AGENT_REPLY_ACCEPTED → VOICESTUDIO_AUDIO_ACCEPTED → DEVICE_EVIDENCE_ACCEPTED → user_heard_confirmed`
- 该 batch 的当前设备停止字段：`backend_stop_ack=null`、`loopback_output_stopped=false`、`acoustic_output_stopped=false`、`speaker_output_stopped=false`

## 独立声学批次

- artifact：`<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-speaker-stop-acoustic\p7-acoustic-20260929-1c3751cbde2daf2d.json`
- 设备停止字段：`loopback_output_stopped=true`、`acoustic_output_stopped=true`、`speaker_output_stopped=true`
- 该批次的 `user_heard=true` 只绑定 `p7-acoustic-20260929-1c3751cbde2daf2d`
- 它不是 Agent batch，不能覆盖 Agent batch 的旧设备字段

## 结论

```text
第 5 项：Real chain established, stop closure incomplete
STOP_TIMEOUT / UNKNOWN：Blocked
P7：Blocked / incomplete
```

未执行：重复 Harness、重复 VoiceStudio、重新播放/录音、故障注入、杀进程、替换 DLL、修改 VoiceStudio 配置、stage/reset/clean/commit/push。
## 2026-09-29：声学 fault runner 分层字段修正与重测

本次按既定方案修正 `tools/p7_real_waveout_fault_acoustic_runner.py`：

- `loopback_output_stopped` 只由 loopback endpoint 自身的 `valid + post_quiet` 决定；
- `acoustic_output_stopped` 只由独立麦克风 endpoint 自身的 `valid + post_quiet` 决定；
- `speaker_output_stopped` 仍严格为两者的安全合取，任一采集错误仍保持 `false`；
- 未放宽 `quiet_dbfs=-55.0`，未把 Stereo Mix（index 12）当作独立声学传感器。

新增 `tests/tts/test_real_waveout_acoustic_runner.py`，覆盖分层字段、双通道合取、采集错误 fail-closed 和无效端点场景。

专项验证：

```text
16 tests / OK
compileall app tests tools / OK
```

真实 opt-in 重测结果：

| fault | batch_id | loopback post | microphone post | loopback | acoustic | speaker | status |
|---|---|---:|---:|---|---|---|---|
| timeout | `p7-real-waveout-timeout-acoustic-20260929-dd0186f813664a1a` | `-96.70 dBFS` | `-56.07 dBFS` | `true` | `true` | `true` | `established` |
| unknown | `p7-real-waveout-unknown-acoustic-20260929-c1b42be7114e420c` | `-96.70 dBFS` | `-56.46 dBFS` | `true` | `true` | `true` | `established` |

两个 batch 的故障语义仍分别为 `timeout` / `unknown`，原因均为 `fault_shim_after_real_waveOutReset`。两个 artifact 的 `waveout_diagnostic.cleanup_verified=false`，`user_heard=null`；因此它们证明了真实 fault batch 的双通道停止事实，但不证明 cleanup 已闭合，也不自动建立人工听感证据。

当前判定更新为：

```text
STOP_TIMEOUT / UNKNOWN：真实 fault + 双通道声学停止证据已建立
fault batch cleanup_verified：仍未闭合
user_heard：本批次未人工确认，保持 null
P7：仍 Blocked / incomplete
P8：Not entered
```
