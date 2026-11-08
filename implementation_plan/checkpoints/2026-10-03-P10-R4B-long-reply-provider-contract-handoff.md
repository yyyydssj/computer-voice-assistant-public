# P10 R4-B 交接：**长回复播报失败已在本机请求契约层点名**（2026-10-09 已修复，见 §12）

- 建立时间：2026-10-03
- 目的：把用户实测"**三点长回复还是不行**"的现状、已确证的证据、已交付的改动、以及下一步**最小路径**完整记录，供下次会话直接接手。
- 状态：**问题未解决**。但失败点已从"说不清"推进到"**有名字的错误 + 稳定复现条件**"。
- 代码状态：HEAD `c44dbeb`，工作区 clean，**未 push**；真机进程已全部停止（含自动拉起的 VoiceStudio）。

---

## 1. 一句话现状

**短/中文本播报正常，长文本必失败**；失败发生在**本机 TTS 服务（VoiceStudio WebSocket）的协议契约层**，
表现为 `VoiceStudioContractError`。旧通道（`ws_pcm`）会**把这类错误吞掉并谎报"播完"**（已修为一律如实报
`speech_truncated`），新通道（`pcm_seq`）**直接抛错**，因此错误终于可见。

> **2026-10-09 更正（真机确证，见 §12）**：失败点**不是** VoiceStudio 服务端/WebSocket，而是**本地请求
> 契约层**——`VoiceStudioSentenceRequest` 在构造时以固定短码 **`text_blank`** 拒绝空行切出的纯空白碎片
> （`failed_request_chars = 1`），**请求从未发出**。"长文本必失败"的真实规律是"**含空行的回复必失败**"。

---

## 2. 决定性真机证据（最新一轮 `--tts-mode pcm_seq`）

| 轮次 | 回复字数 | 实际播出字数 | 结果 |
|---|---|---|---|
| 2 | 17 | 17 | `speech_finished` ✅ |
| 27 | 17 | 17 | `speech_finished` ✅ |
| 31 | 108 | 108 | `speech_finished` ✅ |
| **183** | **242（三点长回复）** | **6** | **`speech_failed:VoiceStudioContractError`** ❌ |

同族历史证据（旧通道 `ws_pcm`，均已记录在 `runtime.jsonl`）：

| 事实 | 数值 |
|---|---|
| 394 字 → 只播 10 字 | `pushed=367 → dequeued=10(1 段) → played=10`，旧版**谎报 `speech_finished`**；修复后如实报 `speech_truncated`（`played_chars_below_pushed`） |
| 249 字 → 播 74 字 | 同上，`overloaded=false`、无 late 事件 |
| 293 字 → 播 52 字 / 349 字 → 1 段 | 同上（早期批次） |

⇒ **规律**：短文本（≤108 字）正常；**长文本（240 字以上）稳定失败**。

---

## 3. 根因归属（当前结论 + 未确定项）

**已确定**：
1. ~~失败点在 **provider 契约层**（`VoiceStudioContractError` 由 `app/tts/providers/voicestudio_ws.py` /
   `app/tts/transport.py` 抛出），**不是**我们的播放层、不是队列背压、不是分段策略；~~
   **2026-10-09 更正**：异常由**本地** `app/tts/providers/contracts.py::_require_text` 以
   `text_blank` 抛出，触发点是 `app/tts/sequential_session.py::_make_request`；
   **请求从未发往 VoiceStudio**（证据与修复见 §12）。
2. 旧通道 `StreamingVoiceSession` **会把该错误吞掉**（这是"只念开头却报播完"的机制来源）；
3. 新通道 `pcm_seq` 不吞错 ⇒ 错误类型名可见。

**尚未确定（下一步唯一要做的事）**：`VoiceStudioContractError` 内部的**具体错误码**（协议里有
`frame_too_large` / `audio_too_large` / `samples_mismatch` / `audio_empty` / `done_before_start` /
`frame_idle_timeout` / `sentence_timeout` 等固定码）。拿到码才能判断是"**单次请求超长/超大**"，
还是"**服务端超时**"，还是"**协议字段不符**"。

**同时未定案**：用户听到的"混了别的声音"——音量合成器里**没有** VoiceStudio 条目；
Python 条目**音量原为 1/100（几乎静音）**，用户已用经典 `sndvol` 拉高，之后短句听感正常。
⇒ "混音"与"截断"是**两件独立的事**，"混音"待复现。

---

## 4. 本次长会话已交付并提交的改动（按时间）

| SHA | 内容 | 现状 |
|---|---|---|
| `beab7d4` | **回声证明收紧**：真机假停播证据（turn 2/4 未插话却 `barge_in_interrupted`；`echo_proven=true` 而估计延迟 746/−780 ms）⇒ 新增"延迟物理窗口 0–500 ms + 连续 3 次稳定"才算证明 + `echo_proof_state` 事实；15 项测试 | 有效 |
| `ba86aec` | **播报截断事实可视化**：`SessionStream.record` 公开、记 `finish_timed_out`/`late_events`/`dropped_pushes`；`_speak()` 不再谎报播完（`speech_truncated`）；`voice_turn` 增 9 个 `speech_*` | 有效 |
| `5d24d7f` | `_speak()` 记 `speech_failed:<ExceptionName>`；`--tts-mode` 可观测性 | 有效 |
| `d0dc229` / `fa6813f` | 两批"排空"（`_await_drain`、`finalize_completed` 探针） | **已被回退**（空转，见 `3b49a87`） |
| `257e5d3` | 段级字段扁平化（tuple 曾让免按键会话被判"只允许扁平基础类型"而降级为文字） | 有效（真 bug 修复） |
| `f268c8d` | `--voice --auto` 下 `--tts-mode` **静默忽略**的真 bug 修复 | 有效 |
| `84ae2ba` | 段级 provider 失败事实（`failure_states` 等）+ GitHub 调研落盘 | 有效 |
| `3b49a87` | **彻查后回退**：删空转排空/探针、切分收窄为"真句末优先 + 硬上限 120"、新增 `overloaded`/`late_event_codes`/`played_chars` | 有效 |
| `8e656b2` | **根因修复**：`_enqueue` 改**等待式背压**（超时**整批拒绝 + 明确错误**）、`_overloaded` 从 `_run` 退出条件删除、三计数、PCM 预算 8000→20000 ms | 有效（消除"静默丢队列"） |
| `5f28f4b` | **段流取证字段**（`dequeued_*`/`enqueued_*`/`enqueue_dropped_*`/`segmenter_buffered_chars`/`speech_record_index`） | 有效（零行为变更） |
| `c44dbeb` | **新通道 `tts_mode=pcm_seq`**：有界预取 K=2 + 串行播放等 `ticket COMPLETED`；`finish()` 强制 `played == synthesized == total`；默认通道零改动 | 有效（短中文本 108/108 全播） |

相关文档：
- 调研 `implementation_plan/research/2026-10-03-P10-R4B-streaming-tts-tail-loss-github-research.md`（8 条 GitHub 条目经 API 核实；主流做法：Pipecat 无界串行 + 收尾排空；LiveKit 有界等待背压 + 三计数）
- 调研 `implementation_plan/research/2026-10-03-P10-R4B-tts-truncation-github-research.md`
- 交接 `implementation_plan/checkpoints/2026-10-03-P10-R4B-speech-truncation-fix-handoff.md`（§8 彻查与回退、§9 根因修复）
- 交接 `implementation_plan/checkpoints/2026-10-03-P10-R4B-echo-proof-tightening-handoff.md`

---

## 5. 两套播报通道现状

| 通道 | 启用方式 | 现状 | 判据 |
|---|---|---|---|
| `ws_pcm`（**默认，未改**） | 默认 | 长文本**失败但不再说谎**：如实记 `speech_truncated` | `speech_played_chars < speech_pushed_chars` |
| `pcm_seq`（**新增**） | `--tts-mode pcm_seq` | 短/中文本**全文播出**（108/108）；长文本**抛 `VoiceStudioContractError`** | `sentences_total == sentences_synthesized == sentences_played` |
| `edge` / `http_wav` / `none` | CLI | 未在真机验证 | — |

---

## 6. 复测命令（可复制）

```powershell
cd '<PROJECT_ROOT>'
$env:ASSISTANT_PROVIDER='deepseek'
$env:ASSISTANT_DEEPSEEK_OPT_IN='true'
$env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
$env:PYTHONUNBUFFERED='1'
.\.venv\Scripts\python.exe -u -m app.main --voice --auto `
  --tts-mode pcm_seq `
  --barge-in-mode off `
  --aec-reference-mode same_process --aec-engine webrtc `
  --device-recovery-mode safe_point --silence-ms 1200 --tts-start-timeout 180
```
- **每轮 2 次真实 DeepSeek 请求（计费）**，必须用户在场并授权；
- 日志：`$env:TEMP\cva-live-*.log`（`-u` 才有实时 `识别/回复` 文本）；
- 事实：`$env:LOCALAPPDATA\ComputerVoiceAssistant\logs\runtime.jsonl`（脱敏、无文本）；
- **`--barge-in-mode` 必须 `off`**：`confirm` 会重现"没插话也停播"（`echo_proven` 在真机会被伪相关骗过，虽已收紧但**未真机复测**）；
- TTS 服务启动耗时波动大（6 s ~ 65 s）；健康检查 `http://127.0.0.1:3900/health`。

---

## 7. 环境事实（踩过的坑）

1. **Windows 每应用音量**：`Python` 条目曾被设为 **1/100**（几乎静音），且**在 Win11 音量合成器里灰置不可调**；用经典 **`sndvol`** 可以拉高。⇒ 听感问题先查这里，别先怀疑代码。
2. `pycaw` **未安装**（若要程序化读写各应用音量，需装依赖——需用户确认）。
3. VoiceStudio 由本应用自动拉起（`--tts-mode` 属流式时）；进程名 `python.exe`；音量合成器里**没有**它的独立条目。
4. `waveOut` 走 `WAVE_MAPPER`（44100），系统默认为 WASAPI(48000)；5 个"输出"条目其实只有 1 个物理端点（Realtek）。
5. 用户曾在场并授权计费；**不得**未经授权启动真机。

---

## 8. 下一步（按优先级，**先取证再改**）

1. **【最小、最高价值】把 provider 错误码记进事实**：`VoiceStudioContractError` / `VoiceStudioWebSocketError`
   携带固定短码；同时记录**每次请求的文本长度**（例如 `speech_request_chars` / `speech_error_code`），
   **只加事实、不改行为**。
2. 跑**一轮三点长回复** ⇒ 点名错误码。可能的码与对应方向：
   - `audio_too_large` / `frame_too_large` / `samples_mismatch` ⇒ **单次请求文本/音频超服务端上限**：
     把 `pcm_seq` 的 `max_sentence_characters`（当前 120）降到 40~48，或按服务端上限拆更细；
   - `sentence_timeout` / `frame_idle_timeout` ⇒ 服务端合成超时：拆更细或调 `sentence_timeout_seconds`（当前 18 s）；
   - `audio_empty` / `done_before_start` ⇒ 服务端行为异常，需抓帧核对协议。
3. 把 `speech_sentences_total` / `_synthesized` / `_played`（`pcm_seq` 会话已有，日志未映射）补进 `voice_turn`。
4. 复现/排除"混音"：用"**不自动拉起 TTS**"的方式启动助手 + 用户手动启动 VoiceStudio，对比听感；
   并在播放时用 `sndvol` 看**是否有两个条目同时跳**。
5. 未做（不得伪造）：
   - `pcm_seq` **长回复真机成功**未验证（当前只证明短中文本全播）；
   - `ws_pcm` 的 `echo_proven` 收紧**未真机复测**（`off` 仍是本机安全配置）；
   - `app/config/settings.py` 的 `tts_pcm_max_queued_audio_ms=8000` **未接入**流式路径（接入时必须 ≥20000）；
   - `SPEECH_CHARACTERS_PER_SECOND=4.8` 为新引入常量（引擎更慢则预算偏小，已留 2× 余量）；
   - barge-in 主动取消仍报 `speech_finished`（不改安全口径，代价：取消轮里"取消前截断"不可见）。

---

## 9. 验证基线（接手即可复跑）

| 项 | 值 |
|---|---|
| 全量测试 | **2678 tests OK (skipped=3)**（HEAD=`c44dbeb`，256.7 s，exit 0） |
| `tests/tts` / `tests/voice_loop` | 606 / 143 全绿 |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 339 个 .py；0 解码失败；BOM 25 |
| 仓库 | 分支 `codex/review-playback-lifecycle`；工作区 clean；**未 push** |

---

## 10. 纪律提醒（下次会话必须遵守）

- 真机必须**用户在场 + 授权计费**；`--barge-in-mode off`；
- **不要 push**（除非用户明确要求）；
- 不杀别人的进程（本机音量合成器里的其他应用与本项目无关）；
- 文档一律用 `edit`/`write` 工具写（UTF-8 无 BOM、LF），**不要**用 `Set-Content -NoNewline`/`Path.write_text`（曾把 `NEXT-SESSION.md` 写空、曾引入 CRLF）；
- 每个批次：全量测试 + `compileall` + 编码扫描 + 文档同步 + 本地提交。

## 11. 2026-10-07 追加（本节为 2026-10-09 依据仓库证据重建）

> **重建说明**：本节原文在 2026-10-07 追加时，非 ASCII 字符被写入路径整体替换成 `?`——该节 0 个中文字符存活，其余章节未受影响，原文措辞已不可恢复。以下内容依据**幸存的 ASCII 骨架**（文件名、常量、字段名、测试名、数字、命令）与仓库原物（未提交 `git diff`、离线测试、`runtime.jsonl`）重建，**不是原文照录**。

### 11.1 本轮改动（截至 2026-10-07 均为未提交状态）

- `app/tts/sequential_session.py`：`MAX_SENTENCE_CHARACTERS` 由 120 降为 48；异常对象携带固定短码 `.code`；新增只读取证字段 `provider_error_code` / `failed_sentence_index` / `failed_request_chars`；异常类型与 fail-closed 行为不变。
- `app/runtime/voice_entry.py`：新增 `_flat_code()` 校验网关，把 provider 错误码与 total / synthesized / played 三计数写进 record。
- `app/runtime/auto_voice.py`：把上述字段映射进 `AutoTurnFacts`，使其出现在 `voice_turn` 日志中。
- 当时的假设：失败来自 provider 侧 `audio_too_large`（单次请求过大），拟把 `max_chars=120` 继续下调。
  - ⚠️ **该假设已被 2026-10-09 的只读排查修正**：6 条 `pcm_seq` 失败记录里的异常类型是**裸 `VoiceStudioContractError`**（而不是被 provider 调用包装后的 `SequentialVoiceSessionError`），说明失败发生在 `_make_request` **构造本地请求**这一步、请求从未发到 VoiceStudio；逐段唯一变化的判据是“文本只含空白”，对应固定短码应为 `text_blank`（回复里的空行被切成纯空白句段）。证据见 `2026-10-07-voice-playback-truncation-investigation.md`。

### 11.2 当轮验证

| 项 | 结果 |
|---|---|
| （原骨架前缀 2 字已丢失）`pcm_sequential_pipeline` + `voicestudio_ws` + `speech_truncation_facts` + `pcm_seq_mode_registration` | **96 tests OK** |
| `tests/tts` | **607 tests OK (skipped=3)** |
| `tests/voice_loop` | **144 tests OK** |
| （原骨架标签已丢失） | **56 tests OK** |
| `compileall -q app tests tools` | **OK** |
| `git diff --check` | **OK** |

全量 `unittest discover` 2509 项中有 9 项非通过，出现在与以下四个 runner 相关处：`tools.p9_auto_voice_runner`、`tools.p8_real_voice_loop_runner`、`tools.p9_voiceprint_enroll`、`tools.p7_real_waveout_fault_acoustic_runner`。（该句其余中文说明已丢失，只保留幸存的文件名与数字。）

### 11.3 下一步（当时记录）

- 真机验证的前置条件涉及 VoiceStudio 与 DeepSeek（原文细节已丢失）。
- 真机 A/B 使用 `--tts-mode pcm_seq --barge-in-mode off`，对比 `speech_sentences_total` / `speech_sentences_synthesized` / `speech_sentences_played` 与 provider 错误码。
- push 侧仍有改动待 review。

---

## 12. 2026-10-09 结案：真机确证 `text_blank`，两处修复 + 验收

### 12.1 决定性证据（`--tts-mode pcm_seq --barge-in-mode off`）

| turn | 本地时间 | reason | 文本长度 | 已播/总段 | `speech_error_code` | 失败句序号 | 失败请求字数 |
|---:|---|---|---:|---|---|---:|---:|
| 2 | 18:34:17 | `speech_failed:VoiceStudioContractError` | 294 | 1/13 | **`text_blank`** | 4 | **1** |
| 1 | 18:40:49 | `speech_finished` ✅ | 296 | 9/9 | — | — | — |
| 1 | 18:45:38 | `speech_finished` ✅ | 17 | 2/2 | — | — | — |

### 12.2 根因

回复里的空行被 `split_sentences` 切成**纯空白碎片**（换行本身就是独立碎片）→ 入队 →
`_make_request` 构造 `VoiceStudioSentenceRequest` → `_require_text` 判 `not text.strip()` →
`VoiceStudioContractError(code="text_blank")` → `_fatal` 取消整条播报。
**与长短无关，只与有没有空行有关。**

### 12.3 修复

1. `app/tts/sequential_session.py`：入队处丢弃纯空白碎片（`push_text_chunk` + `finish`）；
   `split_sentences` 的字符精确不变量与 provider 契约不动。
   > **更正（2026-10-09 19:0x）**：这里原先写"`ws_pcm` 早由 `ReplySegmenter` 的 `EMPTY_TEXT`
   > 防住"——**只对了一半**。真机复测证明它在默认通道上变成了**静默丢尾**（空白块拒收时
   > **不消费序号** ⇒ 其后全部被判越序并丢弃），已另行修复，见 §13。
2. `app/runtime/auto_voice.py` + `app/main.py`：HUD 在真实 `_speak` 期间显示"播报"（真机确认），
   删掉"整轮结束之后才设 `SPEAKING`"的错误时序。
3. `app/tts/private_selection.py` + `app/main.py`：`verify_provider` 六类失败各自带固定短码
   （`provider_unreachable` / `provider_not_ready` / `provider_response_*` /
   `provider_profile_not_found` / `provider_reference_audio_*`），`voice_entry_unavailable` 增加 `code`。

### 12.4 验收

`tests/tts` 616 / `tests/foundation` 335 / `tests/voice_loop` 144 全过；`compileall -q app tests tools`
exit 0；`git diff --check` 干净；真机两轮全文播出（9/9、2/2）且 HUD 播报状态经用户确认。

详细记录（含 §6/§9 旧结论的更正）见同日检查点
`2026-10-07-voice-playback-truncation-investigation.md` §12；脱敏原始事实见
`implementation_plan/checkpoints/artifacts/2026-10-09-pcm-seq-text-blank/voice-turn-evidence.json`。

---

## 13. 并案（2026-10-09 晚）：`ws_pcm`（默认通道）的**静默**丢尾

`pcm_seq` 修好之后按要求对默认通道做了真机复测，**复现了同一病根的另一个分支**：

| 时间 | 通道 | 文本 | pushed | accepted | played | 段 | 结果 |
|---|---|---:|---:|---:|---:|---:|---|
| 18:54:59 | `ws_pcm` | 233 | 229 | **74** | **74** | 2 | `speech_truncated`，`error` 空 ❌ |
| 19:00:15 | `ws_pcm` | 118 | 114 | 114 | 114 | 3 | `speech_finished` ✅ |
| 19:02:12 | `ws_pcm` | 404 | 398 | 398 | 398 | 14 | `speech_finished` ✅ |

**根因**：空行经 `split_for_push` 切出纯空白片 → 分段器以 `EMPTY_TEXT` 拒收且**不消费序号**
（`_last_sequence` 只在接受时推进）→ 会话序号永远超前 1 → 其后每个 chunk 都被判
`OUT_OF_ORDER` → 而 `_submit` 对越序**什么都不做** ⇒ 155 字静默消失（旧版据此谎报"播完"）。
`pcm_seq` 是"构造请求时报错中止"（可见），这里是"序号错乱后静默丢尾"（不可见）。

**修复**：`app/tts/streaming_session.py` 把纯空白 push 与空 push 同等对待（绝不推进序号）；
`OUT_OF_ORDER` 升级为记录型致命错误，新固定码 **`streaming_push_out_of_order`**；
`tests/tts/test_streaming_session.py` 新增 `StreamingVoiceSessionBlankPushTests`（3 条）。

**验收**：`tests/tts` 619 OK (skipped=3) / `tests/foundation` 335 OK / `tests/voice_loop` 144 OK、
`compileall` exit 0、`git diff --check` 干净；真机 `114/114`、`398/398` 全播。完整记录（含离线
"修复前/后"对照）见 `2026-10-07-voice-playback-truncation-investigation.md` §13；脱敏事实见
`implementation_plan/checkpoints/artifacts/2026-10-09-ws-pcm-sequence-desync/voice-turn-evidence.json`。
