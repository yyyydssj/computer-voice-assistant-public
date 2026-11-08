# P10 · P1 批次交接：收尾判据从「总时长」改为「无进展看门狗」

- 日期：2026-10-12（本地）
- 批次简称：**P1（drain-stall）**；对应豆包逆向的结论项（见 §1）
- 触发：用户反馈「长回复会中止」；并在真机日志中确认残余机制
- 范围：**只改收尾/背压判据**，不动 provider 协议、分段策略、PCM 预算、打断/安全门、设备策略
- 全量基线（本批结束）：**2718 tests OK (skipped=3)**（2696 → 2718，+22）

---

## 1. 为什么改：三处「总时长」上限

逆向结论（`implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`）：
豆包实时语音的**超时全部是空闲式**（`AudioTTSIdleTimeoutError(52000016)` /
`DialogAudioIdleTimeoutError(52000042)` / `AudioChatRecvTimeoutError(52000011)`），**不存在
"一条回复最多播 N 秒"这种上限**。本仓却有三处按总时长判定的上限：

| # | 位置 | 旧行为 | 后果 |
|---|---|---|---|
| 1 | `StreamingVoiceSession.finish()` | `join(timeout=segment_timeout_s)`（默认 30 s） | 尾部剩余音频超过它 ⇒ `finish_timed_out=True`；随后 `SessionStream.finish()` 的 `finally: close()` → `cancel()` **取消仍在播放的语音** |
| 2 | `SequentialVoiceSession.finish()`（pcm_seq） | 两个线程**各** `join(timeout=join_timeout_s)`（默认 40 s ⇒ 最长 80 s） | 超时直接抛硬错误 `pcm_seq_join_timeout` ⇒ 整条 `speech_failed` |
| 3 | `_enqueue()` 队列等待 / `SessionStream._await_room()` 背压等待 | 固定 deadline | 长回复只要播放慢于合成，等待到期后就继续推送/整批拒绝 |

---

## 2. 改了什么（5 个生产文件 + 4 个测试文件）

### 2.1 流式通道 `app/tts/streaming_session.py`
- 新增模块常量 **`DEFAULT_STALL_TIMEOUT_S = 15.0`** 与构造参数 `stall_timeout_s`；
- `finish()` 的 `join(timeout=segment_timeout_s)` → **`_await_drain()`**：
  - 每轮**正好阻塞一个停滞预算**（**刻意不分片轮询**：分片会引入最多一个切片的超时溢出，对
    0.05 s 级预算就是 100%，会把"有界收尾"推到 worker 自己的超时分支上）；
  - 醒来比对**只增不减的句子级进展计数** `(dequeued_segments, synthesized_segments, len(played), segment_index)`；
    变了就再等一轮，**没变才判停滞**；
  - 判停滞时语义**不变**：仍置 `finish_timed_out=True` + `late_events` 追加 `finish_timeout`，
    由 `close()` 按设计取消剩余语音（调用方记 `speech_truncated`）。
- 新增只读事实：`drain_seconds`、`stall_timeouts`、`last_progress`（固定短码
  `segment_played`/`segment_synthesized`/`segment_dequeued`/`none`）；
- `segment_timeout_s` 恢复单一含义（**一个句段**的预算）；`build_streaming_session()` 一并透传停滞预算。

### 2.2 pcm_seq 通道 `app/tts/sequential_session.py`
- 同构改造：`_progress_mark_locked()` / `_progress_stage_locked()` / `_await_drain()`（一轮预算平均分给两个线程）；
- 停滞**仍是显式失败**（`pcm_seq_join_timeout`，口径未放松），detail 改为
  `stalled_on=<synth|play|both>,last_progress=<固定码>`；
- `DEFAULT_JOIN_TIMEOUT_S` 语义改为**单线程**预算；新增 `drain_seconds` / `last_progress`。

### 2.3 闭环适配 `app/runtime/voice_entry.py`
- `_await_room()`：**看到进展（`pending_sentences` 下降）就顺延**预算；不下降仍在同一预算后放弃；
  **放弃后照旧继续推送、绝不丢东西**（失败仍由 `_enqueue` 显式给出）；
- 新增可注入 `backpressure_timeout_s`（只为让"顺延"可确定性测试）；
- 新增 `_flat_seconds()`（只接受有限非负秒数，NaN/±inf/负数/bool → `None`）；
- `_record_speech_facts()` 增 `drain_seconds` / `stall_timeouts` / `last_progress`。

### 2.4 `app/runtime/auto_voice.py` + `app/main.py`
- `voice_turn` 增 **`speech_drain_seconds` / `speech_stall_timeouts` / `speech_last_progress`**（只扁平基础类型，短码 ≤64）；
- CLI 新增 **`--speech-stall-timeout`**（float，默认 15.0，只校验正数，help 写明建议区间 5~120），
  经 `VoiceEntryConfig` / `AutoVoiceConfig` / `_voice_entry_config_for_auto()` 透传到**两条通道**。

### 2.5 测试（新增 4 个文件、共 22 项）
| 文件 | 项数 | 内容 |
|---|---:|---|
| `tests/tts/test_streaming_drain_stall.py` | 3 | 慢但在推进必须全播（P1 前红）/ 停滞预算小于进展间隔仍须判停滞 / 卡死必须有界失败 |
| `tests/tts/test_pcm_seq_drain_stall.py` | 3 | 同上，pcm_seq 通道（复用既有假件） |
| `tests/voice_loop/test_drain_stall_backpressure.py` | 8 | 背压顺延 / 卡住仍放弃 / 缺计数立即返回 / 预算校验 / 三个事实的扁平化与 None 语义 |
| `tests/tts/test_speech_stall_timeout_wiring.py` | 8 | 默认值 / 取值 / 非法值拒绝 / auto→voice_entry 透传 / 两条通道接住预算 |

另更新 2 处既有断言：`tests/foundation/test_main_auto_voice.py` 等因新字段/新开关需要的同步（见各文件注释）。

---

## 3. 修复前 / 修复后对照（离线，可复现）

**流式通道**（`tests/tts/test_streaming_drain_stall.py`，同一夹具、3 句共 33 字）：
```
修复前(旧 join=segment_timeout_s=1.8s): 1.81s played=11/33 段=1 finish_timed_out=True truncation=pending_after_finish
修复后(P1 停滞看门狗 8s)             : 3.66s played=33/33 段=3 finish_timed_out=False truncation=None
```
**pcm_seq 通道**（`tests/tts/test_pcm_seq_drain_stall.py`）：
```
修复前(旧两次 join=0.5s): 0.50s 已播=0/2 last_progress=sentence_synthesized error=pcm_seq_join_timeout
修复后(停滞看门狗 5s)   : 2.41s 已播=2/2 drain=2.40s error=None
```

对照脚本（把旧收尾装回去）在两份测试文件的模块 docstring 里有等价描述；执行方式：monkeypatch
`_await_drain` 为旧 `join(timeout=...)` 后跑同一场景。

---

## 4. 验证

| 检查 | 结果 |
|---|---|
| `tests/tts` | **633 OK (skipped=3)** |
| `tests/voice_loop` | **152 OK** |
| `tests/foundation` | **335 OK**（P1 结束时；UI 批次后又增） |
| 全量 `discover` | **2718 OK (skipped=3)**，148.2 s（2696 → +22） |
| `compileall -q app tests tools` | exit 0 |
| CLI 自检 `--help` | 出现 `--speech-stall-timeout`，help 文案正确 |

---

## 5. 风险 / 未做（如实）

1. **收尾不再有总时长上限**：只要流水线一直在推进就会一直等（这正是豆包的语义）；极端慢但持续
   推进的 TTS 会让单轮变长，由 `--speech-stall-timeout`（默认 15 s）兜底；
2. `_enqueue()` 的"等待超时即整批拒绝"**本批未改**（`test_stalled_device_refuses_the_whole_reply_visibly`
   要求"卡死必须显式拒绝"）；长回复在**推入阶段**仍可能撞上它 —— 需要单独设计"何时仍应拒绝"；
3. **真机 ≥800 字长回复验收尚未跑**（用户待执行）；本批只有离线证据 + 2026-10-12 22:58 那轮
   173 字（未进入长回复区间）；
4. 未改 provider 逐句请求粒度（→ 那是 P2「整段一次请求」的范围，见研究文档 §4 P2）。

---

## 6. 回滚

- 单批 revert 本批的 5 个生产文件即可；判据回到"总时长"（旧行为）。
- 想**只关闭**新语义而不改代码：把 `--speech-stall-timeout` 设成旧常量（ws_pcm 30 s / pcm_seq 40 s）
  可复现旧上限；`VoiceEntryConfig(speech_stall_timeout_s=...)` 同理。
- 新增的 `speech_*` 三个事实字段是**附加的**，回滚生产代码时 `voice_turn` 里会变成 `None`，不破坏既有解析。
