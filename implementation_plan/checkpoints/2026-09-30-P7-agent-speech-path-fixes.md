# P7 Agent→说话链路修复：ASR 逐句播报、背压不阻塞 Agent、死代码

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**完成（离线验证通过）；未做真实 ASR 设备验证**
- 范围：修三处此前发现的 Agent→说话链路问题；不改变任务/授权/审计语义。

## 1. 修复内容

### 1.1 ASR（语音输入）路径没有逐句播报

此前逐句流式只对文字输入生效：`ConversationManager.submit_asr_final` 不接受文本回调，`PushToTalkCoordinator` 也没有任何实时语音注入点。

- `app/conversation/manager.py`：`submit_asr_final(..., on_text_chunk=None)` 新增并按 `submit_text` 的同一路径透传给 `_submit_routed`。
- `app/asr/push_to_talk.py`：
  - 新增可选注入 `realtime_speaker`（沿用 `TextShell` 的 duck typing：只要求可 `start_stream()`）；
  - 新增模块级 `drive_turn_stream(speaker, submit)`，在提交前 `start_stream()`，把 `stream.push_text_chunk` 作为 `on_text_chunk` 传入 `submit_asr_final`；只有**完整且无失败**的 outcome 才 `finish()`，否则 `cancel()`；
  - `accept_provider_event` 改用该函数。

边界：语音是可选的——speaker 缺失、`start_stream` 抛错、流没有 `push_text_chunk`、`finish()` 抛错，全部只降级为"不播报"，**绝不阻塞本轮对话、绝不改变返回的 outcome**。`submit` 抛错时先 `cancel()` 再原样传播。

### 1.2 TTS 背压会阻塞 Agent 线程（行为变更）

原实现：`push_text_chunk` 在 pending 达上限时 `condition.wait()`，而该调用发生在 Agent 通知回调线程（即 UI 主线程）上，于是播放慢会拖住 Agent 文本输出——与计划第 11 节"UI/Agent 线程不得被 TTS 等待阻塞""语音压力过大宁可停止剩余语音并保留文字"冲突。

现在（`RealtimeVoiceSession` 与 `StreamingVoiceSession` 同步修改）：

- `push_text_chunk` **永不阻塞**；单次 Agent push 的整批句子**整体接受**（分句器已用 `max_queued_segments=8` 界定单批规模）；
- 若接受后 pending 超过上限，置 `overloaded=True`：**只拒绝后续 push**，不截断当前批次；
- 已入队的句子**继续按序播完**，然后语音线程退出；
- 新增只读属性 `overloaded`，供诊断与调用方判断；完整文字始终保留。

### 1.3 `TextShell` 死代码

`app/ui/text_shell.py` 里 `if eligible and stream is None:` 内又判断 `stream is not None and stream_ok:`（永假），`stream_ok` 只在该死分支被使用。已删除该死分支与 `stream_ok` 变量；"无流式路径时由完成回调合成一次"的语义显式写明，`legacy` 行为不变。

## 2. 测试

- 改写 `tests/tts/test_realtime_voice.py`：原 `test_http_pending_queue_never_exceeds_two` 断言 `sender.is_alive()`（"输入必须背压"），属**刻意的旧设计**；现改为 `test_pending_bound_never_blocks_the_agent_thread`，断言 `push_text_chunk` 立即返回、`overloaded=True`，并保留"后台仍继续合成"的检查。
- 改写 `tests/tts/test_streaming_session.py`：`test_agent_push_never_blocks_and_overload_is_recorded` —— push 立即返回（<1 s）、整批三句仍按序播完、`overloaded=True`。
- 新增 `tests/asr/test_stream_bridge.py`（9 项）：合规 outcome → `finish`；不合规/失败 outcome → `cancel`；speaker 缺失 → 仍以 `on_chunk=None` 提交；`start_stream` 抛错不影响本轮；`submit` 抛错先 `cancel` 再传播；流无 `push_text_chunk` → 传 `None`；`finish` 抛错 → `cancel` 且 outcome 不变；无 `start_stream` 的 speaker 被容忍。

## 3. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest tests.asr.test_stream_bridge -q
9 tests, OK

.\.venv\Scripts\python.exe -m unittest discover -s tests/tts -p "test_*.py" -q
460 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
975 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

## 4. 覆盖缺口与遗留

- `ConversationManager.submit_asr_final → _submit_routed` 的 `on_text_chunk` 透传**没有单独测试**：它走的是与 `submit_text` 完全相同的 `_submit_routed` 分支（该分支已有既有测试覆盖），本次新增的 `DriveTurnStreamTests` 覆盖了 bridge 侧的调用契约。该缺口如实记录，未声称已直接覆盖。
- **未做真实 ASR 设备验证**：没有真实麦克风/按键轮次，因此"真实语音提问后逐句播报"未验证；本批全部为离线测试。
- 过载策略的语义是"停止本回复剩余语音、保留文字"，因此**高负载下用户可能少听几句**（这是刻意的取舍，替代阻塞 Agent）；`overloaded` 目前只做诊断暴露，尚未接入 UI/日志事件。
- 未做长回复压力测试与延迟指标。
- 工作区仍有大量既有 staged/untracked 文件；本轮未执行 `git add .`、commit 或 push。

## 5. 下一入口

1. 用户审阅本轮 diff；
2. 可单独授权：真实 ASR 按键轮次的端到端验证（需要麦克风与真实按键，且不与"用户已听见"混淆）；
3. 或继续 Batch 6（barge-in、取消顺序、迟到块跨回复隔离）/ Batch 7（真实设备与性能）。
