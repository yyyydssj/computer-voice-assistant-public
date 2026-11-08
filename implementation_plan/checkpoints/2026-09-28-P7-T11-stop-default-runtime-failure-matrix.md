# P7 T11：可停止播放、默认 Agent 接缝与真实故障矩阵补齐（2026-09-28）

## 本批范围

本批只继续 P7 验收，不进入 P8：

1. 可停止播放生命周期与迟到事件安全边界；
2. 默认 `Agent → 回复文字 → 可选 TTS` 接缝；
3. 真实故障矩阵的离线契约补强与真实证据分层。

## 实施与修复

- `PlaybackWorker` 已补齐 stop 超时后 worker 恢复时的一次 best-effort backend stop；迟到 ACK 不得升级已记录的 `STOP_TIMEOUT`。
- 默认运行时使用 local chat-only router 与 fail-closed unavailable Agent adapter；默认不读取 DeepSeek key、不创建 SDK/transport/TTS 设备。
- 修复 `app/main.py` 的 opt-in 边界：注入 runtime 的 speaker 只有在 `voice_enabled=True` 时才注册；`voice_enabled=False` 不播报。
- 故障矩阵补强 stop ACK/timeout/unknown 的离线断言，明确 backend 事实不等于物理扬声器事实或用户听感。

## 验证

专项：

```text
.python312\\python.exe -B -m unittest discover -s tests -t . -p 'test_main_voice_opt_in.py' -q
13 tests / OK

.python312\\python.exe -B -m unittest discover -s tests -t . -p 'test_playback_worker.py' -q
11 tests / OK

.python312\\python.exe -B -m unittest discover -s tests -t . -p 'test_real_playback.py' -q
22 tests / OK

.python312\\python.exe -B -m unittest discover -s tests -t . -p 'test_real_failure_matrix*.py' -q
18 tests / OK (skipped=2)
```

全量：

```text
.python312\\python.exe -B -m unittest discover -s tests -t . -q
Ran 763 tests
OK (skipped=2)
```

另通过：

```text
.python312\\python.exe -m compileall -q app tests
git diff --check
git diff --cached --check
```

## 证据边界

| 层级 | 结论 |
|---|---|
| L0 fake Agent/TTS/backend | Pass，仅本地契约 |
| L1 真实 VoiceStudio 成功基线 | 有 opt-in live gate，但本批未运行 |
| L1 真实 VoiceStudio 故障注入 | Blocked，无 fault proxy/服务故障模式 |
| L2 真实 Windows waveOut stop ACK/timeout/unknown | Blocked，缺正式设备证据包 |
| L3 真实 Agent → VoiceStudio → 设备默认端到端 | Blocked，当前仅注入 seam |

`STOP_ACKED` 仅表示 backend stop/reset/buffer-return 协议事实；不表示物理扬声器静音，也不表示用户听见。`user_heard` 仍必须为 `None`，除非有用户明确确认或独立声学测量。

## 当前阶段结论

```text
P7：Blocked
P8：未进入
```

本批完成的是：可注入默认接缝、可停止播放生命周期修复、离线故障矩阵补强；不宣称真实 Agent、真实 VoiceStudio、真实 Windows 设备或默认真实端到端已验收。

## 下一停止点

- 如要推进 L1/L2/L3，必须另行确认真实 VoiceStudio、网络/凭据、Windows 输出设备与真实 Agent 的专项授权和证据采集预算。
- 未获得这些真实条件前，继续保持 P7 `Blocked`，不以 fake 或人为异常替代真实故障证据，不进入 P8。

未执行：真实网络、真实 VoiceStudio、真实 Windows 播放、commit、push、reset、clean。
