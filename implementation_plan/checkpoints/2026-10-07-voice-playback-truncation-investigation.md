# 语音播报中断与长回复尾部缺失问题记录

- 记录日期：2026-10-07
- 项目：电脑语音助手
- 调查范围：VoiceStudio WebSocket、PCM 顺序播放、Windows waveOut、语音打断链路
- 当前状态：已定位到主要问题层级，具体 VoiceStudio 错误码仍待补充
- 本次调查原则：只读排查，不修改代码，不覆盖现有未提交改动

## 1. 用户实际观察

1. 播报音色目前是正常人声，不是杂音。
2. 有时用户没有说话，播报也会自行中断。
3. 长回复播报时，尤其是较长内容，可能只播报开头，听不到结尾。
4. 最近一次明确反馈：长回复中“上海”部分没有听到结尾。
5. 当前需要区分以下三类原因：
   - VoiceStudio 生成或 WebSocket 传输不完整；
   - PCM 播放链路提前结束；
   - 播报回声被误判成用户插话，导致主动停止。

## 2. 本次真机运行配置

当前测试使用的关键参数：

```text
--voice --auto
--tts-mode pcm_seq
--barge-in-mode off
--aec-reference-mode same_process
--aec-engine webrtc
```

实际播放链路：

```text
app/runtime/voice_entry.py
  -> SequentialVoiceSession
  -> PCMPlaybackWorker
  -> WaveOutPCMStreamBackend
  -> Windows waveOut
```

VoiceStudio 通道：

```text
app/tts/providers/voicestudio_ws.py
  -> VoiceStudio WebSocket /ws/tts
```

## 3. 已确认：本次问题不能归因于回声打断

本轮运行中：

```text
barge_in_mode=off
barge_in_enabled=false
```

在此配置下，`_build_barge_in()` 不会创建独立的打断采集线程，播报期间也不会通过 AEC/barge-in 主动停止播放。

因此，当前这次“没有听到长回复结尾”不能解释为：

> VoiceStudio 播放声音被麦克风采集后，被识别为用户插话，从而中断播报。

这条路径在本轮测试中已关闭。后续若恢复 barge-in，仍需单独验证回声误判，但它不是本次 `barge_in_mode=off` 运行的解释。

## 4. 真机日志证据

日志文件：

```text
<USER_HOME>\AppData\Local\ComputerVoiceAssistant\logs\runtime.jsonl
```

同一运行实例中出现了明显的长文本失败规律：

| 请求文本长度 | 实际播报/记录 | 结果 |
|---:|---:|---|
| 17 字 | 17 字 | `speech_finished` |
| 108 字 | 108 字 | `speech_finished` |
| 93 字 | 0 字 | `VoiceStudioContractError` |
| 81 字 | 约 20 字 | `VoiceStudioContractError` |
| 225 字 | 约 6 字 | `VoiceStudioContractError` |
| 242 字 | 约 6 字 | `VoiceStudioContractError` |

当前日志中的典型记录包括：

```text
turn=171, speech_text_length=81, speech_played_chars=20,
speech_error=VoiceStudioContractError

turn=175, speech_text_length=225, speech_played_chars=6,
speech_error=VoiceStudioContractError

turn=183, speech_text_length=242, speech_played_chars=6,
speech_error=VoiceStudioContractError
```

历史旧通道还出现过：

```text
249 字 -> 约 74 字
291 字 -> 约 10 字
394 字 -> 约 10 字
```

旧通道曾经把部分失败错误吞掉并误记为“播完”，后续已调整为尽量如实记录截断状态。新通道 `pcm_seq` 会直接暴露 `VoiceStudioContractError`，因此当前错误更容易观察到。

## 5. VoiceStudio 服务端发送逻辑

服务端文件：

```text
<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\backend\api\routers\tts_stream.py
```

服务端流程：

1. 对整个文本做一次规范化；
2. 使用 `split_stream_sentences()` 切分句子；
3. 每句调用 `render_stream_sentence()` 生成音频；
4. 转换为 16-bit PCM；
5. 以 `CHUNK_SAMPLES = 4800` 分块发送，即 24 kHz 下每块约 200 ms；
6. 累加 `total_samples`；
7. 最后发送 `done`，其中包含 `samples`、`duration_s` 等字段。

客户端文件：

```text
<PROJECT_ROOT>\app\tts\providers\voicestudio_ws.py
```

客户端会校验：

- `start.format == pcm16`；
- `sample_rate == 24000`；
- `channels == 1`；
- PCM 帧长度必须为偶数；
- 单帧和累计音频不能超过限制；
- `done.samples * 2 == 实际收到的 PCM 总字节数`；
- 必须收到最后一个 `is_final=True` 的 PCM 块。

因此，VoiceStudio 服务端只要发生以下任意一种情况，客户端就会报告契约失败：

- 生成中途超时；
- WebSocket 帧间空闲超时；
- 服务端发送的 PCM 字节数与 `done.samples` 不一致；
- 服务端发送空音频或不完整音频；
- 服务端只生成了前面部分，随后发送错误；
- 客户端在完整 `done` 之前断开或收到非法消息。

## 6. 当前根因判断

### 已确定

1. ~~**长回复的主要故障点在 VoiceStudio provider / WebSocket 契约层。**~~
   **2026-10-09 更正（真机确证）**：故障点在**本地请求契约层**。`SequentialVoiceSession._make_request()`
   构造 `VoiceStudioSentenceRequest` 时被 `_require_text` 以固定短码 **`text_blank`** 拒绝（空行被切成的
   纯空白碎片，长度 1），**请求从未发往 VoiceStudio**；因为异常在 provider 调用之外抛出，它没有被
   `_synthesize_one` 包装，所以日志里看到的是裸 `VoiceStudioContractError`。证据与修复见 §12。
2. 当前没有证据表明是用户插话导致播报停止。
3. 当前没有证据表明 `waveOut` 是长回复截断的首要原因；日志中很多请求在进入完整播放前就已经出现 `VoiceStudioContractError`。
4. “杂音”问题与当前的“长回复尾部缺失”不是同一个已确认问题。本轮用户反馈已确认当前是正常人声。

### 尚未确定

当前应用日志只记录了：

```text
VoiceStudioContractError
```

但没有可靠落盘其内部固定错误码。因此还不能确定具体属于（**2026-10-09 已确证：`text_blank`**，其余候选码在真机日志中一次都没有出现，见 §12）：

- `sentence_timeout`
- `frame_idle_timeout`
- `samples_mismatch`
- `audio_too_large`
- `frame_too_large`
- `audio_empty`
- `done_before_start`
- `server_error`
- 其他协议错误

在取得具体错误码前，不能进一步断言是“服务端生成慢”“单帧太大”“总音频过大”还是“samples 计数不一致”。

## 7. 为什么不能把“播放完成”当作“用户听完”

`WaveOutPCMStreamBackend` 使用 Windows `waveOut` 提交 PCM 缓冲区，并通过 `WOM_DONE` 判断缓冲区返回。

但 `WOM_DONE` 只说明：

> waveOut 后端已经归还了缓冲区。

它不等价于：

- 扬声器已经完成全部声学输出；
- 用户实际听到了全部内容；
- 没有输出设备切换或系统混音问题。

因此，`PlaybackState.COMPLETED`、`speech_finished`、`speech_sentences_played` 只能证明软件链路认为播放任务完成，不能单独作为“用户听完”的证据。

不过，在本轮长回复失败案例中，VoiceStudio 在前置阶段已经报告 `VoiceStudioContractError`，所以当前优先级仍然是先解决 provider 长句契约问题，再补充播放链路声学证据。

## 8. 当前排查结论排序

```text
VoiceStudio 长句生成/协议传输失败
  > PCM 播放链路提前结束
  > 播报回声被误判为用户打断
```

这是当前证据支持的优先级，不代表后两项已经永久排除。

## 9. 待办事项

### 优先级 P0：补齐 provider 错误事实

记录以下字段，不改变原有播放行为：

- `speech_error_code`；
- `speech_request_chars`；
- `speech_failed_sentence_index`；
- `speech_failed_request_chars`；
- `speech_sentences_total`；
- `speech_sentences_synthesized`；
- `speech_sentences_played`。

然后用三组长回复重新测试，确认具体错误码。

### 优先级 P1：根据错误码采取最小修复

- 如果是 `audio_too_large`、`frame_too_large` 或 `samples_mismatch`：进一步缩短单句、调整服务端分块或修正 samples 统计；
- 如果是 `sentence_timeout` 或 `frame_idle_timeout`：检查 VoiceStudio 生成耗时和句子拆分长度；
- 如果是 `audio_empty`、`done_before_start` 或 `server_error`：抓取完整 WebSocket 消息顺序，检查服务端异常路径；
- 如果 provider 完整成功但用户仍听不到结尾，再转向 waveOut/Windows 输出链路。

### 优先级 P2：补充播放链路证据

在不保存原始 PCM 的前提下记录：

- 每句 PCM 总字节数；
- 每句 PCM 时长；
- RMS、peak、非静音样本比例；
- waveOut submitted / returned 次数；
- 最后一块提交、返回时间；
- 必要时记录输出设备 loopback 的时间窗口 RMS。

### 优先级 P3：重新验证回声打断

只有在需要重新打开 barge-in 后，才验证：

1. 播报期间用户保持安静；
2. 观察是否出现 `barge_in_confirmed=true`；
3. 检查是否有 echo correlation 证据；
4. 对比关闭和开启 barge-in 两种模式。

## 10. 证据边界

本文件不把以下事项当作已证实事实：

- “用户听到的上海尾部一定是 waveOut 丢失”；
- “VoiceStudio 一定是 samples_mismatch”；
- “用户实际听到了软件日志中的全部已提交 PCM”；
- “任何一次播放中断都来自同一个根因”。

当前最可靠的结论是：

> 在 `pcm_seq + barge-in off` 的真机配置下，长回复经常在 VoiceStudio WebSocket provider 契约层失败，导致只播报开头或无法播报；该轮失败不能归因于播报回声误判成用户插话。

## 11. 相关文件

- `<PROJECT_ROOT>\app\runtime\voice_entry.py`
- `<PROJECT_ROOT>\app\tts\sequential_session.py`
- `<PROJECT_ROOT>\app\tts\pcm_playback.py`
- `<PROJECT_ROOT>\app\tts\waveout_pcm.py`
- `<PROJECT_ROOT>\app\tts\providers\voicestudio_ws.py`
- `<PROJECT_ROOT>\app\tts\transport.py`
- `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\backend\api\routers\tts_stream.py`
- `<USER_HOME>\AppData\Local\ComputerVoiceAssistant\logs\runtime.jsonl`

---

## 12. 2026-10-09 结案：真机确证 + 两处修复 + 验收

### 12.1 决定性真机证据（`--tts-mode pcm_seq --barge-in-mode off`）

| turn | 本地时间 | reason | 文本长度 | 已播/总段 | `speech_error_code` | 失败句序号 | 失败请求字数 |
|---:|---|---|---:|---|---|---:|---:|
| 2 | 18:34:17 | `speech_failed:VoiceStudioContractError` | 294 | 1/13 | **`text_blank`** | 4 | **1** |
| 1 | 18:40:49 | `speech_finished` ✅ | 296 | 9/9 | — | — | — |
| 1 | 18:45:38 | `speech_finished` ✅ | 17 | 2/2 | — | — | — |

- `speech_played_chars = 41` 与用户实际听到的那一句逐字吻合（41 字）；
- `sentences_total=13` / `played=1`：整条回复被中止，后 12 段全丢——这正是"只念了开头"；
- 三条记录都取自 `runtime.jsonl` 的 `voice_turn`（脱敏字段，无任何文本）。

### 12.2 根因（已确证，取代 §6 的旧结论）

`回复里的空行` → `split_for_push` / `split_sentences` 把 `\n` 当句末并把它本身作为独立碎片 →
`SequentialVoiceSession` 入队 → `_make_request` 构造 `VoiceStudioSentenceRequest` → `_require_text`
判 `not text.strip()` → `VoiceStudioContractError(code="text_blank")` → `_fatal` 取消整条播报。
**与内容长短无关，只与"有没有空行"有关**（18 字那句没有空行，2/2 全播）。

### 12.3 已交付的修复（工作区未提交）

1. **播报截断**：`app/tts/sequential_session.py` 在入队处丢弃纯空白碎片（`push_text_chunk` 与
   `finish` 同一判据）；`split_sentences` 的字符精确不变量与 provider 契约均未改动。
   > **更正（2026-10-09 19:0x）**：本小节原先写"默认通道 `ws_pcm` 已由 `ReplySegmenter` 的
   > `EMPTY_TEXT` 防住"——**只对了一半**。18:54:59 的真机复测证明它变成了另一种更坏的表现：
   > 空白块被拒收时**不消费序号**，其后每个 chunk 都被判越序并**静默丢弃**（233 字只播
   > 74 字、`error` 为空）。详见 §13。
2. **HUD 播报状态**：`app/runtime/auto_voice.py` 新增 `_complete_turn_with_hud()`，在真实 `_speak`
   期间推 `SPEAKING`、之后回 `LISTENING`（`STATE_LABELS` 里只有 `SPEAKING` 映射成"播报"）；
   `app/main.py` 删除"整轮结束之后才设 `SPEAKING`"的错误时序，轮次结束改设 `IDLE`。
3. **可观测性**：`app/tts/private_selection.py` 的 `verify_provider` 各类失败各自带固定短码
   （`provider_unreachable` / `provider_not_ready` / `provider_response_too_large` /
   `provider_response_invalid` / `provider_profile_not_found` /
   `provider_reference_audio_unavailable` / `provider_reference_audio_changed`），`str(exc)`
   就是码本身；`voice_entry_unavailable` 事件增加 `code` 字段。

### 12.4 验收

| 检查 | 结果 |
|---|---|
| 播报截断（真机） | 18:40 `9/9`、18:45 `2/2`；`reason=speech_finished`、`speech_error_code=null` |
| HUD 播报状态（真机） | 用户确认：说话"聆听" → 播报期间"播报" |
| `tests/tts` / `tests/foundation` / `tests/voice_loop` | 619 / 335 / 144 全过（含 `ws_pcm` 修复后的 3 条新用例） |
| 默认通道 `ws_pcm`（真机） | 19:00 `114/114`（3 段）、19:02 `398/398`（14 段 / 404 字）全播；见 §13 |
| `compileall -q app tests tools` | exit 0 |
| `git diff --check` | 干净 |

### 12.5 未做（如实记录，不推断）

- 交接文档里"故障在 VoiceStudio 服务端/WebSocket 契约层"的旧结论已在其 §3 加更正标注；
- 首次 autostart 运行出现过"卡在输出设备行之前、无任何事件"的现象，**后续未复现、未定位**；
- ~~`ws_pcm` 通道未复测~~ → **已于 2026-10-09 19:0x 复测、定位并修复**（见 §13）；
- `MAX_SENTENCE_CHARACTERS = 48` 的长回复表现仍未单独复测。

---

## 13. 并案（2026-10-09 晚）：`ws_pcm`（默认通道）的**静默**丢尾

### 13.1 复现（修复前，`--tts-mode ws_pcm`）

| turn | 本地时间 | reason | 文本长度 | pushed | **accepted** | played | 段数 |
|---:|---|---|---:|---:|---:|---:|---:|
| 1 | 18:54:59 | `speech_truncated`（`played_chars_below_pushed`） | 233 | 229 | **74** | **74** | 2 |

其余字段全部"干净"：`error` 空、`provider_failures=[]`、`enqueue_dropped=0`、
`segmenter_buffered_chars=0`、`overloaded=false`、`pending_after_finish=0`——**静默丢掉 155 字**，
这正是旧版"谎报播完"的来源。74 字恰好是第一段，与用户听感（"又只念了一段"）一致。

### 13.2 根因（与 §12 同一病根的第二条通路）

1. `voice_entry` 用 `split_for_push` 切片，而 `\n` 是句末符 ⇒ 空行切出**只有 `\n` 的纯空白片**；
2. `StreamingVoiceSession._submit` **先自增** `self._sequence`，再交给分段器；
3. 分段器对纯空白块返回 `EMPTY_TEXT`，且**不消费序号**（`_last_sequence` 只在接受时推进）；
4. ⇒ 会话序号永远超前 1，其后**每个** chunk 都撞 `OUT_OF_ORDER`，同样不消费 ⇒ **永久卡死**；
5. `_submit` 当时只对 `QUEUE_BUDGET_EXCEEDED` 抛错，对 `EMPTY_TEXT`/`OUT_OF_ORDER` 什么都不做
   ⇒ 无 error、无计数、无残留。

即：`pcm_seq` 是"构造请求时报错中止"（可见），`ws_pcm` 是"序号错乱后静默丢尾"（不可见）。

### 13.3 修复

- `app/tts/streaming_session.py::push_text_chunk`：纯空白 push 与空 push 同等对待（直接返回，
  绝不推进序号）；
- `_submit`：`OUT_OF_ORDER` 升级为记录型致命错误，新固定码
  **`streaming_push_out_of_order`**（与 `streaming_queue_budget_exceeded` 同级，已进 `__all__`）；
- `tests/tts/test_streaming_session.py`：新增 `StreamingVoiceSessionBlankPushTests`（3 条）。

离线"修复前/后"对照（用测试夹具把旧 `push`/`_submit` 装回去跑）：

```text
split_for_push("第一句。\n\n第二句。") -> ('第一句。', '\n', '\n', '第二句。')
修复前 : played = ('第一句。',)             | error = None
修复后 : played = ('第一句。', '第二句。') | error = None
```

### 13.4 验收（真机）

| 时间 | 文本长度 | pushed | accepted | played | 段数 | 结果 |
|---|---:|---:|---:|---:|---:|---|
| 18:54:59（修复前） | 233 | 229 | 74 | 74 | 2 | `speech_truncated` ❌ |
| 19:00:15（修复后） | 118 | 114 | 114 | 114 | 3 | `speech_finished` ✅ |
| 19:02:12（修复后） | 404 | 398 | 398 | 398 | 14 | `speech_finished` ✅ |

判据：`played == accepted == pushed`、`finish_timed_out=false`、`truncation` 空、`error` 空、
`provider_failures=[]`、`pending_after_finish=0`、`segmenter_buffered_chars=0`。404 字那轮正落在
历史截断区间（旧记录 394 字只播 10 字）。

测试：`tests/tts` 619 OK (skipped=3)、`tests/foundation` 335 OK、`tests/voice_loop` 144 OK、
`compileall -q app tests tools` exit 0、`git diff --check` 干净。
