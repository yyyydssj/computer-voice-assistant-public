# P7 VoiceStudio 双路径 Batch 4：PCM 流式播放后端

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**Batch 4 完成（离线实施 + 真实 Windows 设备三 buffer 生命周期验证通过）**
- 范围：`PCMPlaybackWorker` + 三 buffer `waveOut` 流式后端；未接入实时会话、未改默认 runtime、未动原 WAV worker。

## 1. 本批次目标与边界

Batch 3 之后客户端已能拿到持续到达的 PCM 流，但播放层仍以「完整 WAV ticket」为单位。Batch 4 建立 PCM 消费接缝：一句一个**有界**队列、**起播阈值**、以及可复用的设备缓冲契约，并在真实设备上验证三 buffer 轮转与 reset 回收。

本批不实现：WS→HTTP 自动降级（Batch 5）、barge-in/迟到块跨回复隔离（Batch 6）、性能与长时间验收（Batch 7）、默认切换（Batch 8）。也**没有**把 PCM worker 接到 `RealtimeVoiceSession`、`settings.py` 或任何默认运行时路径。

## 2. 新增实现

### 2.1 `app/tts/pcm_playback.py`

- `PCMStreamBackend`（Protocol）：`backend_name`、`max_buffer_bytes`、`open()`、`writable()`、`submit(pcm)`、`poll_drained()`、`stop(timeout)`、`close()`。契约明确：设备仍持有的 buffer 绝不能被重写。
- `PCMPlaybackWorker`：单句 PCM 消费与状态机
  - `begin_sentence(request)` / `push_chunk(chunk) -> bool` / `wait(ticket, timeout)` / `stop(ticket, timeout)` / `close()`
  - **起播阈值** `start_buffer_ms=160`：未达阈值不向设备提交；`is_final=True` 到达时立即 flush
  - **背压**：未提交队列超过 `max_queued_audio_ms=8000` 抛 `PCMPlaybackBackpressure("queued_audio_limit")`
  - **单句内存上限** `max_sentence_bytes=16 MiB` → `sentence_memory_limit`
  - **身份与顺序**：reply/segment/request/generation 不符 → `identity_mismatch`；sequence 必须从 1 连续 → `sequence_invalid`
  - **迟到块**：终态后同身份的块返回 `False` 并丢弃，绝不进入设备
  - 自然完成：`is_final` 已收到、队列已空且 `poll_drained()` 为真 → `COMPLETED`
  - stop 复用既有 `PlaybackState`：未提交过设备 → `STOP_ACKED(confirmation="not_submitted")` 且不调用 backend；已提交 → backend 归还 buffer 后才 `STOP_ACKED`；`timeout` → `STOP_TIMEOUT`；`unknown`/异常 → `UNKNOWN`；后两者 disable 后续语音
  - 设备 `submit` 异常 → `FAILED` + disable（fail-closed）
  - 只读计数：`pending_bytes`、`queued_audio_ms`（仅未提交 PCM；设备在飞量由固定 buffer 数约束）
  - 事实边界：`STOP_ACKED` 仍只表示 backend 归还了 buffer，不代表扬声器静音或用户听见

### 2.2 `app/tts/waveout_pcm.py`

- `WaveOutPCMStreamBackend`：3 个（可配置 2–8）循环 `WAVEHDR`，默认 `buffer_bytes=9600`（200 ms）
  - `waveOutOpen` 一次（24 kHz / mono / PCM16 / `WAVE_MAPPER`），每个 header `waveOutPrepareHeader` 一次后循环复用
  - `writable()`：存在未提交或已 `WOM_DONE` 的 header；无可用 header 时 `submit()` 抛 `PCMBufferBusy`
  - 提交前 `memmove` 新 PCM、设置 `dwBufferLength`、**只清 `WHDR_DONE` 并校验 `WHDR_PREPARED` 仍在**（否则驱动下次 write 会拒绝，见第 5 节）
  - `poll_drained()`：没有 header 处于 SUBMITTED
  - `stop(timeout)`：`waveOutReset` 在条件锁**外**调用（避免与 `WOM_DONE` 回调互锁）→ 等待所有已提交 header 返回；超时 → `timeout` + `uncertain`；reset 非 0 → `unknown` + `uncertain`
  - `close()`：`uncertain` 或仍有 SUBMITTED header 时**绝不** `unprepare`/`close`，把 backend 保留到进程结束（`_uncertain_pcm_backends`）；正常路径才 unprepare 全部 header + close，并置 `cleanup_verified=True`
  - `PCMWaveOutDiagnostic`：各 WinMM 调用结果与时间、`header_addresses`、`submitted_count`、`returned_count`、`cleanup_verified`
  - `winmm` 门面可注入，全部三 buffer 生命周期可离线测试

### 2.3 未修改

`playback_worker.py`（原完整 WAV worker 保留）、`real_playback.py`、`completed_reply.py`、`routing.py`、`settings.py`、`app/main.py`、VoiceStudio 本体、任务与授权语义。

## 3. 测试

文件：`tests/tts/test_pcm_playback.py`（35 项）

- Worker 层（离线 fake backend）：起播阈值、final flush、FIFO 顺序、自然完成需 final、重复/跳号 sequence、外部身份拒绝、同类第二句拒绝、迟到块丢弃、单句内存上限、8 s 背压、未提交 stop 不调用设备、stop 需 buffer 返回、`STOP_TIMEOUT`/`UNKNOWN` 终态并 disable、backend submit 失败 fail-closed、close 幂等并拒绝新句、无活动句 push 拒绝、队列计数语义。
- Backend 层（fake WinMM，镜像驱动行为）：open 准备 3 个 header、无返回时提交停止且第 4 次抛 `PCMBufferBusy`、`WOM_DONE` 后才可复用同一 header、未知 header 的 `WOM_DONE` 被忽略、**`WHDR_PREPARED` 在复用后仍保留且 `WHDR_DONE` 已清**、超 buffer/空提交/未 open 或已 close 的提交拒绝、stop reset 并等待**全部** buffer 返回、buffer 不返回则 timeout + uncertain、reset 失败 → unknown、open 失败上报、uncertain 时不 unprepare/close、正常 close unprepare ×3 + close、诊断快照不可变。
- 集成：worker + 真 `WaveOutPCMStreamBackend` + fake WinMM 端到端 `COMPLETED`，close 时完成 3 次 unprepare 与 1 次 close。

## 4. 真实 Windows 设备验证（已执行）

方式：真实 `WaveOutPCMStreamBackend`（`ctypes.WinDLL("winmm")`、`WAVE_MAPPER`、3 × 9600 bytes）+ 本批 `PCMPlaybackWorker`，播放**合成 440 Hz 正弦音调**（幅度 0.1，约 -20 dBFS），不播放任何语音、不落盘音频。batch_id `p7-batch4-pcm-stream-device-a7f5f884aa3e1aa4`。

| 阶段 | 场景 | 结果 |
|---|---|---|
| 1 | 1.2 s 音频 / 12 块 → 自然完成 | `COMPLETED`；**6 次 `waveOutWrite` 用 3 个 header**（header 复用真实发生）；`returned_count=3`；`uncertain=false`；`cleanup_verified=true` |
| 2 | 2.0 s 音频推送 10 块后在飞 4 次 write 时 stop | `STOP_ACKED`；真实 `waveOutReset=0` 回收全部在飞 buffer；`uncertain=false`；`cleanup_verified=true` |

- 证明：三 buffer 在真实驱动下正确轮转复用、真实 `waveOutReset` 回收多个在飞 header、close 时 3 次 `waveOutUnprepareHeader` + 1 次 `waveOutClose` 全部成功。
- artifact：`implementation_plan/checkpoints/artifacts/2026-09-30-P7-batch4-pcm-stream-device/p7-batch4-pcm-stream-device-a7f5f884aa3e1aa4.json`。
- 严格不推断：本次没有声学测量、没有人工确认，因此 `speaker_acoustically_verified=false`、`user_heard=null`；`STOP_ACKED` 仍只表示 backend 归还 buffer。此前 P7 收尾的单 buffer 完整 WAV 真机证据（2026-09-28 成功基线、2026-09-29 `waveOutReset` 的 timeout/unknown）覆盖的是**另一个后端**，本批的多 buffer 轮转风险不在其覆盖范围。

## 5. 真机验证发现的缺陷与修复

**缺陷：提交时把 `dwFlags` 整体清零，抹掉了 `WHDR_PREPARED`。** 真机第一次运行阶段 1 立即 `FAILED`、`write_count=1`：首个 `waveOutWrite` 成功后，第二次提交因为 header 失去 `WHDR_PREPARED`（0x2）被驱动拒绝。`waveOutPrepareHeader` 设置的该标志必须跨次提交保留，只能清 `WHDR_DONE`（0x1）。

修复：`submit()` 改为 `dwFlags &= ~WHDR_DONE`，并在提交前校验 `WHDR_PREPARED` 仍在（否则抛 `waveout_header_not_prepared` 并置 uncertain）。同时让离线 fake WinMM **镜像驱动行为**——`waveOutPrepareHeader` 设置 PREPARED，`waveOutWrite` 在缺失该位时返回 `WAVERR_UNPREPARED(34)`，并新增回归测试 `test_prepared_flag_survives_resubmission`。修复后真机阶段 1/2 均通过（第 4 节）。

这条缺陷此前 34 项离线测试全部漏掉，是真机验证的直接收益。

## 6. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest tests.tts.test_pcm_playback -q
35 tests, OK

.\.venv\Scripts\python.exe -m unittest discover -s tests/tts -p "test_*.py" -q
420 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
919 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

只读审计：3 个新增文件严格 UTF-8、无 BOM、无 `U+FFFD`；AST 调用面显示 `pcm_playback.py` 无设备/网络/文件/子进程入口，`waveout_pcm.py` 的设备入口仅默认参数路径的 `ctypes.WinDLL("winmm")`，测试通过注入 fake WinMM 完成全部设备生命周期。

## 7. 未执行与遗留风险

- PCM worker 尚未接入 `RealtimeVoiceSession` / 双路径调度（Batch 5）；默认 runtime 行为与本批之前完全一致。
- 未新增 `TTS_PCM_*` 配置项（刻意留到 Batch 5 接线时再加）。
- 未做真实 VoiceStudio PCM 流式播放的端到端（WS → PCM worker → 设备）；未做长时间/连续多句设备压力验证；未做 `waveOutReset` 与 `WOM_DONE` 在驱动内部的并发时序统计。
- 未做声学测量与人工确认，`user_heard` 保持 `null`。
- 工作区仍包含大量既有 staged/untracked 文件；本轮未执行 `git add .`、commit 或 push。

## 8. 下一入口

1. 用户审阅本批 diff；
2. 单独授权 Batch 5（双路径调度与 WS→HTTP 首音前一次性降级，并把 PCM worker 接入实时会话）；
3. 或先处理 Agent→说话链路的其余问题（ASR 路径未接逐句播报、TTS 背压阻塞 Agent 线程）。
