# P10 R4-B 交接：播报**截断修复**（长回复一次性推送撞队列预算）

- 建立时间：2026-10-03
- 触发：用户第 4 轮真机复测（`--barge-in-mode off`，带 `speech_*` 事实）直接点名真凶
- 上游：`checkpoints/2026-10-03-P10-R4B-speech-truncation-observability-handoff.md`（上一批：只做事实可视化）

---

## 1. 真机取证（决定性，同一命令 `--barge-in-mode off`）

| 轮 | 回复长度 | 结果 | `speech_*` 事实 |
|---|---|---|---|
| 24 | 133 字 | `speech_finished` ✅ | `route=voicestudio_ws_pcm`、`played_segments=6`、`completed_sentences=6`、`finish_timed_out=false`、`dropped_pushes=0`、`error=null` |
| **26** | **249 字** | **`speech_failed`** ❌ | `route=voicestudio_ws_pcm`、**`cancelled=true`**，而 `played_segments`/`completed_sentences`/`finish_timed_out`/`dropped_pushes` **全为 null** ⇒ **`finish()` 没走完**（那些字段只在 `finish()` 内写） |
| 29 | 3 字 | `text_only` | **`device_action=fallback_to_text`**、`speech_route=null`（另一类问题：R3 设备策略判输出不可用 → 回退文字） |

⇒ **"说一半就停" = 长回复在 `push_text_chunk`/`finish` 抛异常**，`_speak()` 的 except 分支
`_cancel_stream(stream)` **取消剩余语音**。短回复（133 字 / 6 段）完全正常。

---

## 2. 代码层根因

- 流式会话的句子队列**预算默认 8 段**（`segmenter` 的 `max_queued_segments=8`）；`finish()` 在
  「队列 + 尾段」超过预算时**显式抛** `StreamingVoiceSessionError("streaming_queue_budget_exceeded")`。
- 而闭环侧 `SessionStream.push_text_chunk()` 一直是**把整段回复一次性**推给会话 ⇒ 249 字中文
  回复瞬间产生大量句段 → 越过预算 → 抛错 → 播报被取消（用户听到半句）。
- P7 的设计本就是**按句流式**推送（Agent 逐句给文本）；闭环把整段塞进去属于用法走偏。

---

## 3. 修复（本批）

| 文件 | 改动 |
|---|---|
| `app/runtime/voice_entry.py` | 新增 `split_for_push()`（按中文/英文句末与换行切分，单块上限 80 字，**不增删字符**、保持原文与顺序）与三个常量（`MAX_PUSH_CHARS=80`、`PUSH_BACKPRESSURE_PENDING=4`、`PUSH_BACKPRESSURE_TIMEOUT_S=15.0`）；`SessionStream.push_text_chunk()` 改为**按句推送 + 有界背压**（`pending_sentences` ≥ 4 时等待，**有界**超时后继续，绝不挂死），`record` 仍记**整段** `text_length`，并累计 `backpressure_waits`；无 `pending_sentences` 接口时立即返回（行为与旧版一致） |
| `app/voice_loop/chat_loop.py` | `_speak()` 的 except 分支记 **`speech_failed:<ExceptionName>`**（只类型名，不含正文/路径）+ 新增常量 `SPEECH_FAILED_PREFIX`（长回复真机只有 `speech_failed` 无法点名的教训） |
| `tests/voice_loop/test_speech_truncation_facts.py` | +5 项：切分保持原文/顺序、空串与单句、按句推送且记录整段长度、忙队列时**有界等待且全部推送**、无背压接口时行为不变 |
| `tests/voice_loop/test_chat_loop.py` | `test_finish_failure_reason_stays_speech_failed` 断言更新为 `speech_failed:RuntimeError` |

---

## 4. 验证

| 项 | 结果 |
|---|---|
| 相关专项 | `test_speech_truncation_facts` + `test_chat_loop` + `test_voice_status` + `test_p9_auto_runner` 合计 **147 项 OK**（53.1 s） |
| **全量** | **Ran 2613 tests in 82.029s → OK (skipped=3)**，exit 0（2608 → 2613，+5；本次机器负载较轻，耗时与之前 235 s 不可直接对比） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 334 个 .py：**0 解码失败**、BOM **25**（与基线一致）；本批新增/修改文件均无 BOM、纯 LF |

---

## 5. 未做 / 下一步（**不得伪造**）

1. **未做真机复测**：修复是否真的消除"说一半就停"必须再跑一次真机（建议仍用 `--barge-in-mode off`
   先确认播报完整，再考虑开 `confirm`）。
2. **另一类问题未修**：`text_only` + `device_action=fallback_to_text`（R3 设备策略判输出不可用 →
   回退文字）仍在；这是已登记边界（waveOut 无固定输出设备 id），需要时另开一批。
3. 队列预算 `max_queued_segments=8` **未改**（改为按句推送 + 背压后应不再触顶；若真机仍触顶，
   下一批再评估预算或按句推送的节奏）。
4. `speech_failed:<TypeName>` 会按"失败"呈现（比原来更诚实）。

---

## 6. 回滚

`git revert <本批提交>`；回滚后回到"整段一次性推送"，长回复会重新出现截断。

---

## 7. 追加：真机复测（第 5 轮）与**排空修复**

**复测事实（`--barge-in-mode off`）**：`speech_failed` **彻底消失**（队列预算那条修对了）——
turn 3/4/5/6/10 全部 `speech_finished`、`played == completed`、`timedout=false`、`dropped=0`、
`cancelled=false`、`error=null`（其中 **215 字 → 3/3 段**）。

**但用户实听**：三点的回复**只念了第一点**，后两点没有声音 ⇒ 事实与听感冲突，说明截断发生在
**事实统计之后**：`SessionStream.finish()` 的 `finally: close()` → `session.cancel()` →
`worker.stop()` **把设备里还没播完的排队音频丢掉**（`finish_timed_out=false` 只说明会话线程及时
收尾，不代表设备播完）。

**排空修复（本追加）**：`voice_entry.py` 新增 `_await_drain()`（判据就是 `pending_sentences` ——
其语义为 *accepted but not yet finished being spoken*）：`finish()` 在 `close()` 之前**有界等待**
句段清零（`DRAIN_TIMEOUT_S=20 s`、`DRAIN_POLL_S=20 ms`），并如实记录
`drain_pending_at_finish` / `drain_waited_ms` / `drain_pending_after_wait`；
**没排空就把 `finish_timed_out` 置真** ⇒ `_speak()` 报 `speech_truncated`（不许声称播完）。
测试 +3（排空后继续、超时如实记截断且仍有界收尾、无 `pending_sentences` 接口时跳过排空）。

**未做**：这次修复**仍未真机复测**；若复测仍只念第一点，则问题更可能在**会话/后端只合成并播放了
首段**（下一批查 `streaming_session` 的 `_played`/`timing_samples` 语义与 worker 的 `finalize_completed`）。

---

## 8. 彻查与回退（2026-10-03，用户要求"彻查，不要越改越乱"）

用户第 10~11 轮真机仍报"只念第一点"，随即要求**彻查并停止叠加改动**。派出 **3 个只读调查**
（provider/分句层、播放 worker 层、近期提交审查；均未改文件、未跑真机）：

### 8.1 三份调查的一致结论

1. **前几批的"排空"全是空转**：
   - `pending_sentences` 在 `StreamingVoiceSession.finish()` 内部已 `join`、每句已等 `COMPLETED`
     （`streaming_session.py:584-627, 840-845`）⇒ 正常路径**恒为 0**，`_await_drain` 的 while 永不进入；
   - `fa6813f` 调的 `worker.finalize_completed` **在 `PCMPlaybackWorker` 上不存在**（100% no-op，已离线核实）；
   - `pending_bytes`/`queued_audio_ms` 只是"**内存里还没交给设备**"的 PCM（`pcm_playback.py:155-164`），
     为 0 时设备环里最多还有 3×200 ms 在播 ⇒ **不能证明"设备播完"**；
   - `80→200` 切分**无实测收益**（离线段数 Δ=0），却让"无标点超长文本整段进一次合成请求"成为新失效模式
     （服务侧 `sentence_timeout=18s`）。
2. **真凶（证据强度高）**：`streaming_session._enqueue()` 队列越界时**只置 `_overloaded=True`、不清队列**
   （`streaming_session.py:700-702`），`_run` 退出条件含 `self._overloaded`（`:719`）⇒ **已接受、未播出的
   句段被静默丢弃**；同时 `played_segments == completed_sentences`（同源，只统计走到 `COMPLETED` 的句子）、
   `error=None` ⇒ 会话"合规地报播完"。离线可复现（越界后只播第一批、`overloaded=True`、`error=None`）。
3. **盲点**：`SessionStream.record` 里**没有 `overloaded` 字段**，`late_events` 在 `voice_turn` 里只上报
   **个数**（`audio_backpressure`/`push_rejected_overloaded` 这些**码丢失**）⇒ 真机上"未播出"这一最强信号
   从未进入日志，导致前几批在别处反复打转。
4. **已基本证伪**：provider 只回首段音频（WS 客户端 fail-closed，截断必抛 `VoiceStudioTransportError`
   并被记录，`ws_pcm` 不回落 HTTP）。

### 8.2 本批动作（回退 + 收窄 + 只加事实）

| 文件 | 改动 |
|---|---|
| `app/runtime/voice_entry.py` | **删除** `_await_drain()` 与常量 `DRAIN_TIMEOUT_S`/`DRAIN_POLL_S`，`finish()` 回到"`session.finish()` → 写 record → `finally close()`"（`_speak` 最坏 +20s 拖长消失）；record 仍记 `played_segments`/`completed_sentences`/`error`/`finish_timed_out`/`late_events`/`dropped_pushes`/`cancelled`/provider 段级事实，**新增只读** `overloaded`、`pending_sentences_after_finish`、`played_chars`；`MAX_PUSH_CHARS` 80→**120 且为硬上限**（真句末优先 → 到上限在最后一个次级标点后切 → 无次级标点硬切） |
| `app/runtime/auto_voice.py` | `speech` 事实新增 `overloaded`/`late_event_codes`（`\|` 拼接、≤64）/`pending_sentences_after_finish`/`played_chars`；`log_details()` 增 `speech_overloaded`/`speech_late_event_codes`/`speech_pending_after_finish`/`speech_played_chars`（全部扁平基础类型）；删除已失效的 `worker_drain_*`/`drain_*` 映射 |
| `tests/voice_loop/test_speech_truncation_facts.py` | 删除两个排空测试类；新增：`overloaded` 入 record 且 `speech_overloaded is True`、late_events **码**以 `\|` 出现（不再只有个数）、≤64 截断、`None` 保持、切分四例；保留"`log_details()` 只含扁平基础类型"断言 |

**验证**：`test_speech_truncation_facts` + `test_chat_loop` 54 项 OK；全量 **2624 → 2626 tests OK
(skipped=3)**（247.608s，exit 0）；`compileall` exit 0；3 个文件零 BOM / 零 CRLF；离线属性检查 7000 组
随机输入 `''.join(pieces) == 原文` 且单片 ≤120 字。

### 8.3 下一步（**先取证，再动 `_enqueue`**）

1. **真机一轮长回复**：看 `speech_overloaded`、`speech_late_event_codes`（是否含 `push_rejected_overloaded`
   / `audio_backpressure`）、`speech_pending_after_finish`、`speech_played_chars` vs `speech_text_length`；
2. 若确认过载/丢队列 ⇒ 再修 `_enqueue` 的越界分支（要么**拒绝整批**走既有 `dropped_pushes` 语义，
   要么修 `_run` 的退出条件），**不同时改别的**；
3. 未做：**未真机复测**；`_enqueue` 未修；`segmenter max_segment_characters=48 ≈ 10s 语音 > PCM 8s 预算`
   这一隐患未处理。

---

## 9. 根因修复：等待式背压 + 三计数播出事实（2026-10-03，用户："去 GitHub 上找找怎么解决"）

调研见 `research/2026-10-03-P10-R4B-streaming-tts-tail-loss-github-research.md`（8 条 GitHub 条目经
API 核实；**主流框架里没有"有界队列溢出即静默丢弃"这种做法**：Pipecat 队列无界 + 收尾串行化，
LiveKit 有界等待 + 三计数，HA 逐句请求 + 失败上报）。

| 文件 | 改动 |
|---|---|
| `app/tts/streaming_session.py` | ① `_enqueue` 改**等待式背压**（`Condition.wait`，新参数 `queue_wait_timeout_s` 默认取 `segment_timeout_s`）：越界时**等**消费线程取走；超时则**整批拒绝**（`late_events += queue_budget_wait_timeout@generationN` + 抛 `streaming_queue_budget_exceeded`，`_fail_locked` 先写 `_error/_failed/_cancel_event`）⇒ **不可能"部分入队后静默丢弃"**；② **`_overloaded` 从 `_run` 退出条件删除**，退出只剩"显式结束且队列排空"或"致命错误（必须上报）"，`push_text_chunk` 守卫与 `_reject_push_locked` 同步去掉 `overloaded` 原因码；③ **三计数**：`accepted_segments/characters`、`synthesized_segments`、`played_count/characters` + 派生 `pending_segments`；④ **预算自洽**：新增 `SPEECH_CHARACTERS_PER_SECOND=4.8`、`PCM_QUEUE_BUDGET_MARGIN=2.0`、`minimum_pcm_queue_budget_ms()=20000`，成为 `build_streaming_session(max_queued_audio_ms=)` 默认（原 8000 < 单段 10 s），分段字符数未动 |
| `app/runtime/voice_entry.py` | `SessionStream.finish()` 拆出 `_record_speech_facts(played)`（**异常路径也记**，`played` 回退读会话 `played_segments`）；截断判据链 `pending_after_finish → played_below_accepted → played_chars_below_accepted → played_chars_below_pushed → finish_timeout`，任一成立即 `finish_timed_out=True` ⇒ `_speak()` 报 `speech_truncated`；**用户主动取消不算截断**（barge-in 语义不变）；`cancel()` 也记事实；新增扁平字段 `accepted_segments/accepted_chars/synthesized_segments/pending_segments/truncation_reason/pushed_chars` |
| `app/runtime/auto_voice.py` | `speech_*` 映射增补上述 6 项（全部 str/int/None） |
| `tests/tts/test_streaming_queue_backpressure.py`（新，**12 项**） | 长回复 `played_chars == pushed_chars`（含上界=2 的等待式背压变体）；卡死设备变体断言"要么全播、要么 `error` 非空"，**显式断言不出现"无错误 + 未截断 + 少播"**；预算 ≥ 2× 最长句段秒数；装配默认 20000；一段 10 s 音频真的装得进队列 |
| `tests/tts/test_streaming_verification_and_overload.py` | 原"越界推送被计数不丢弃"改为 2 条：**整批接受→4 句全播、sender 阻塞、`dropped_pushes=0`**；**等待超时→整批拒绝 + `error` 即该异常 + `finish()` 重抛** |

**自证（离线前后对比，HEAD=3b49a87 快照 vs 修复后，同一脚本）**：369 字回复、待播上界 1、假 provider
按真实时长出 PCM——

| 事实 | BEFORE | AFTER |
|---|---|---|
| `played_chars` / 总字数 | **120 / 369** | **369 / 369** |
| `dropped_pushes` | 4 | 0 |
| 会话报"播完" | true（**少播 249 字却说播完**） | true（真的播完） |
| 设备卡死场景 | 0 字、`error=null`（等到 3 s join 超时） | **0.27 s 内 push 抛错**、`error` 可见、残留 3 段可见 |

**验证**：`tests/tts` 563→**576 项 OK**；`tests/voice_loop` **141 项 OK**；**全量 2626 → 2639 tests OK
(skipped=3)**（244.630s，exit 0，Lead 独立复跑）；`compileall` exit 0。

**未做 / 不确定（不伪造）**：**未真机复测**；`SPEECH_CHARACTERS_PER_SECOND=4.8` 为新引入常量（引擎更慢则
20000 ms 偏小，已留 2× 余量 + 测试钉住关系）；`app/config/settings.py:95` 的 `tts_pcm_max_queued_audio_ms=8000`
**未接入**流式装配路径（若将来接入必须 ≥20000）；`queue_wait_timeout_s` 默认 30 s 理论上可阻塞 Agent 线程
（实际路径 pending<4、单批 ≤3，不触发）；**barge-in 主动取消仍报 `speech_finished`**（不改安全口径，代价：
取消轮里"取消前已发生的截断"不可见）。
