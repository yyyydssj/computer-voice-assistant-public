# P7 VoiceStudio Batch 7：真实场景与延迟矩阵（7A）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**7A 完成（真实场景矩阵 + 重启恢复通过）；部分计划场景未覆盖，见第 4 节**
- 环境：本机 VoiceStudio `0.5.6`、`/health` 200、`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`；确认音色从私有选择文件读取。

## 1. 真实场景矩阵

固定非敏感文本，全部真实合成 + 真实 Windows 播放，不落盘音频。artifact：`checkpoints/artifacts/2026-09-30-P7-batch7-real-matrix/p7-batch7-real-matrix-5b668ba759918251.json`。

| 场景 | 播放 | 耗时 | 音频 | RTF |
|---|---|---|---|---|
| 4 字 | 1/1 | 16.1 s | 1.49 s | 10.78（冷启动首句） |
| 16 字 | 1/1 | 4.4 s | 2.59 s | 1.69 |
| 32 字 | 1/1 | 7.4 s | 5.23 s | 1.41 |
| 48 字 | 2/2（被分句器切成 2 句） | 11.8 s | 8.05 s | 1.46 |
| 2 句回复 | 2/2 | 11.4 s | 7.5 s | 1.52 |
| WS→HTTP 降级（真实 `connect_failed`） | 1/1 | 6.3 s | 2.72 s | 2.30 |
| 冷启动第一句（重启后） | 1/1 | 5.3 s | 2.69 s | 1.97 |
| warm 第二句（重启后） | 1/1 | 4.3 s | 2.72 s | 1.57 |

指标定义：`elapsed_ms` 为首次 `push_text_chunk` 到 `finish()` 返回的墙钟时间；`audio_seconds` 为 provider 边界观察到的 PCM 总时长；`RTF = elapsed / audio`。**未采集**计划 19.2 的逐句 `sentence_ready→submit` 与 `first_PCM→device` p95（需要句级时间线打点），已在 artifact 的 `not_measured` 中显式登记。

## 2. 真实设备与取消

- 每条路径的 waveOut 都完成 buffer 回收（`cleanup_verified=true`、`uncertain=false`）。
- **播放中取消（真实）**：等待 3.5 s 后取消，此时首句已合成 2.62 s 音频并已有 **11 次真实 `waveOutWrite`**；取消后 1 秒 `write_count` 仍为 11 → **无任何新音频进入设备**；`late_events=["playback:STOP_ACKED@generation2"]`（generation 已推进）。artifact：`…/p7-batch7-overload-fix-fcd51f5dcec1f33e.json`。

## 3. 矩阵暴露的缺陷与修复（真实发现）

首轮矩阵发现 **5 句回复只播 3 句、20 句只播 3 句**。根因是上一轮修"背压阻塞 Agent"时把 `max_pending_segments=2`（原本只用于排队）当成了过载阈值，于是第 3 句之后一律拒收 —— 方向对（计划第 11 节允许"停止剩余语音、保留文字"），但阈值错（计划用的是**待播音频 8 s**）。

修复：

- `StreamingVoiceSession` / `RealtimeVoiceSession` 的句数上限默认从 2 提到 **8**，仅作防失控硬上限；
- 流式会话的真正过载信号改为 **`PCMPlaybackBackpressure`**（Batch 4 `PCMPlaybackWorker` 已实现的 `max_queued_audio_ms=8000`）：命中即置 `overloaded`、停止本回复剩余语音、完整保留文字，并记 `late_events=["audio_backpressure@generationN"]`；
- 重测：5 句 → **5/5**（`overloaded=False`）；20 句 → **9/20** 后触发过载保护（20 句文本瞬间推入、播放需 50+ 秒，属刻意行为）。

新增测试：`test_audio_backpressure_stops_remaining_speech`、`test_long_reply_is_not_truncated_before_audio_backpressure`；`test_realtime_voice` 的 pending 测试改为显式指定上限 2。

## 4. 未覆盖的计划场景（如实登记）

| 场景 | 状态 | 原因 |
|---|---|---|
| 首音前断线、播放中断线 | **未做** | 需要人为干扰 socket；本轮授权范围为 7A，未获断线注入授权 |
| HTTP fallback 失败 | **未做** | 只验证了 HTTP 成功接管；失败路径在 Batch 5 离线覆盖，未在真实服务上演练 |
| Edge 已配置/未配置 | **未做真实播放** | 项目只有跨 Provider 路由契约（`TTSFallbackRouter`），**没有 Edge 播放实现**；只能验证决策，不能验证真实 Edge 出声 |
| 逐句 p95 指标 | **未采集** | 缺少句级时间线打点 |

## 5. 重启恢复（场景 8）

- 由本会话终止全部 VoiceStudio 进程（5 个 `VoiceStudio.exe` + desktop-bridge + uvicorn 后端）→ 端口 3900 释放确认。
- **代理无法从 shell 启动该 GUI 应用**：两次 `Start-Process` 均立即以 exit code 0 退出、未留下进程、`/health` 180 s 未恢复；原因未确认（疑似单实例锁或非交互式会话限制），随后由**用户手动启动**。
- 恢复验证（用户启动后）：`/health` 200；真实冷启动第一句 `played=1`、5.3 s、音频 2.69 s、14 次 `waveOutWrite`；warm 第二句 4.3 s、音频 2.72 s。**重启后下一轮恢复正常**。artifact：`…/p7-batch7-restart-recovery-54c36491aa8aa4f0.json`。

## 6. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
983 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

## 7. 事实边界

- 无声学测量、无人工确认：`speaker_acoustically_verified=false`、`user_heard=null`。
- `STOP_ACKED` 仍只表示 backend 归还了 buffer，不代表物理扬声器静音或用户听见。
- 所有真实播放均使用合成/固定非敏感文本，未落盘任何音频。
- 未从"程序完成/设备调用返回"推导任何听感结论。

## 8. 下一入口

1. 用户审阅本轮 diff；
2. 剩余计划场景需单独授权：断线注入（首音前/播放中）、真实 HTTP fallback 失败、Edge 播放实现（当前不存在，属新功能）；
3. 逐句 p95 指标可在 Batch 8 或单独的性能批次中补齐（需先加句级时间线打点）；
4. Batch 8（默认切换、回滚与阶段验收）仍未开始。
