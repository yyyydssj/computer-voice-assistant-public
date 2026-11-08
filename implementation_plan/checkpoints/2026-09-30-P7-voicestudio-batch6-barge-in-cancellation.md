# P7 VoiceStudio Batch 6：barge-in、取消顺序与迟到块隔离

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**完成（离线验证通过）；未做真实插话/真实 socket 时序验证**
- 范围：固化流式会话的取消顺序、推进 provider generation、把迟到事件降为诊断、验证跨回复隔离。

## 1. 取消顺序（计划第 12 节 → 实现映射）

`StreamingVoiceSession.cancel()` 现在按固定顺序执行，且**幂等**（第二次调用直接返回）：

| 计划步骤 | 实现 |
|---|---|
| 1. 标记 reply cancelled，拒绝旧文本 chunk | `_cancelled=True`；`push_text_chunk` 在 cancelled/finished/failed/overloaded/closed 时直接返回 |
| 2. 停止提交新的句子请求 | `_finished=True` + 清空 `_pending`；语音线程见 `_cancelled` 即退出 |
| 3. 关闭当前 WebSocket / 取消 HTTP 消费资格 | `_cancel_event.set()` 使 `VoiceStudioWebSocketProvider` 在其接收循环内关闭 socket（Batch 3 既有路径），HTTP provider 同样按取消事件放弃等待 |
| 4. 推进 provider generation | `_generation += 1`；后续 request 使用新 generation |
| 5. 清空旧 generation 的待合成句与 PCM | `_pending.clear()` + worker 的 `stop()` 丢弃队列 |
| 6. 停止当前 playback ticket/stream | `worker.stop(timeout=...)`（未提交不触设备、已提交走 `waveOutReset` + buffer 归还） |
| 7. 记录 stop 请求与 backend ACK | 沿用 `PlaybackState`/`StopResult`，`STOP_ACKED` 语义不变 |
| 8. 迟到事件只写诊断 | `_record_late(...)`：`pcm_chunk` / `transport:<code>` / `playback:<state>` / `sentence_completed_after_cancel` |

## 2. generation 与迟到诊断

- 新增只读 `generation`：每句 request 携带它（`provider_generation`），因此取消后到达的块天然属于**旧 generation**。
- 新增只读 `late_events`：脱敏字符串元组（形如 `pcm_chunk@generation2`），记录取消边界之后到达的 PCM、传输错误、播放结果与"取消后才完成"的句子。**这些事件永远不能恢复播放**。
- 跨回复隔离：新回复由新的 `StreamingVoiceSession`（新 `reply_id`、`generation=1`）驱动；`PCMPlaybackWorker` 与 `PCMChunkStreamValidator` 的身份（reply/segment/request/generation）校验保证旧块被拒，测试直接验证了这一拒绝。

## 3. 测试（`tests/tts/test_streaming_session.py` 新增 6 项，共 24 项）

- `test_cancel_is_idempotent_and_advances_generation_once`：连续两次 cancel，generation 只 +1，无已播句。
- `test_cancel_drops_pending_and_returns_no_new_segments`：取消后 `finish()` 返回空元组。
- `test_late_chunk_after_cancel_is_diagnostic_only`：provider 在流中途触发取消、随后再产出一个迟到块 —— `late_events` 非空、`played_segments` 为空、**迟到块未进入设备**（提交字节不超过取消前那一块）。
- `test_old_generation_chunk_is_rejected_by_a_new_sentence`：旧回复身份的块交给当前句 worker 时被拒绝。
- `test_generation_is_used_by_sentence_requests`：句子请求携带会话当前 generation。
- `test_cancelled_session_never_reports_completion`：取消的会话不会被当作完成。

## 4. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest tests.tts.test_streaming_session -q
24 tests, OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
981 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

本批 2 个文件严格 UTF-8、无 BOM、无 `U+FFFD`。

## 5. 未执行与遗留风险

- **未做真实 barge-in**：没有真实麦克风插话、没有真实按键中断、没有在真实播放中途取消；因此"真实设备在取消后立即静音"仍然**未被证明**，`STOP_ACKED` 依旧只表示 backend 归还 buffer。
- 未在真实 VoiceStudio 上验证取消时的 socket 关闭时序与 HTTP 迟到响应（离线用确定性 provider 覆盖）。
- `late_events` 目前不落日志/事件总线，只作为会话内诊断暴露。
- `RealtimeVoiceSession`（`legacy`/`http_wav`）本批**未改**：它的 `cancel()` 已存在且清理 pending/关闭 worker 与 provider，但没有 generation 与 `late_events` 诊断；其迟到块由 provider 取消检查与 worker 身份校验拒绝。这是本批的已知不对称，未声称两套会话行为完全一致。
- 未做长回复/连续取消的压力测试。
- 工作区仍有大量既有 staged/untracked 文件；本轮未执行 `git add .`、commit 或 push。

## 6. 下一入口

1. 用户审阅本轮 diff；
2. 可单独授权真实 barge-in 与真实设备取消时序验证（需要麦克风/按键与扬声器，另行建立证据 ledger）；
3. 或继续 Batch 7（真实 VoiceStudio、Windows 与性能验证）、Batch 8（默认切换、回滚与阶段验收）。
