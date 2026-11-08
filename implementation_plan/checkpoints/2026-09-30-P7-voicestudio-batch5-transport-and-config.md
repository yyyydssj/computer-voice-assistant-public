# P7 VoiceStudio 双路径 Batch 5：传输层、配置与流式会话接入（5A + 5C + 5B）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**Batch 5 完成（5A 传输层 + 5C 配置 + 5B 流式会话接入，全部离线验证通过）**
- 范围：新增传输路由与流式会话；`legacy`/`http_wav` 实时路径**未改动**；默认 runtime 未接线。

## 1. 目标与边界

计划的降级分两层：**VoiceStudio 内部**是 WS PCM 主路径 + HTTP WAV 备用（首音前一次）；**跨 Provider** 才是 VoiceStudio→Edge→文字。后一层由既有 `TTSFallbackRouter` 负责。本批把前一层建起来，并把它接到一个**新的**实时会话上。

本批不修改 `RealtimeVoiceSession`（`legacy`/`http_wav` 行为因此天然逐位不变），不接线默认 runtime，不改任务/授权/审计语义。

## 2. 5A：传输层（`app/tts/transport.py`）

- `VoiceStudioTransport`（`ws_pcm`/`http_wav`）、`TransportFailureStage`（`connect`/`protocol`/`timeout_before_audio`/`after_audio`/`cancelled`/`http`）。
- 模式规则：`http_wav` 只用 HTTP 且不碰 WS；`ws_pcm` 只用 WS 且**不降级**；`auto` 先 WS，**仅在尚未产出任何 PCM 时**失败才用 HTTP 重做**本句一次**（预算可设 0）。
- **出音后失败即终态**：不重做、不换声、不重播；取消永不降级；每句独立重新先试 WS。
- 诊断：`TransportSnapshot`/`TransportFallbackRecord` 记录 transport、阶段、原因、`latency_ms`；**不记录句子文本与 PCM**；错误只暴露短码。

## 3. 5C：配置（`app/config/settings.py`）

`TTS_VOICESTUDIO_MODE` 增加 `ws_pcm`；新增 `TTS_WS_URL`、`TTS_HTTP_GENERATE_URL`（仅 loopback，拒绝凭据/query/fragment/非法端口）、`TTS_WS_CONNECT_TIMEOUT_S`(2.0)、`TTS_SENTENCE_TIMEOUT_S`(18.0)、`TTS_WS_FRAME_IDLE_TIMEOUT_S`(3.0)、`TTS_WS_SESSION_IDLE_S`(30.0)、`TTS_PCM_START_BUFFER_MS`(160)、`TTS_PCM_MAX_QUEUED_AUDIO_MS`(8000)，全部范围校验。`public_summary()` 只暴露 mode、loopback 布尔与数值，不暴露 URL 明文；默认 mode 仍是 `disabled`。

## 4. 5B：流式会话接入（`app/tts/streaming_session.py`）

- `StreamingVoiceSession`：**独立于 `RealtimeVoiceSession`** 的新会话类型。一句一句走 `VoiceStudioTransportRouter`，到达的 PCM chunk 交给 Batch 4 的 `PCMPlaybackWorker`；分句复用 `ReplySegmenter`（4/32/24–40/48，与 Batch 2 实时参数一致）。
- 接口与既有实时会话一致，因此可被 `TextShell` 直接驱动：`push_text_chunk` / `finish` / `cancel` / `close`，另有 `error` / `routing_decision` / `played_segments` / `failure_states` / `finished` / `cancelled` / `closed` / `reply_id`。
- **跨 Provider 决策**：传输失败时按事实映射播放状态并交给 `TTSFallbackRouter.primary_failed`：
  - 本句**从未产出** chunk → `NOT_COMMITTED`（Edge 已配置时可切 Edge）；
  - 已产出 chunk → `PLAYBACK_STARTED`（保守：禁止换声、禁止重播，直接转文字）。
  决策以 `routing_decision` 暴露；本模块只产出决策，不自行播放 Edge。
- 用户取消与内部失败分开记录（`cancel()` vs `_failed`），因此 `finish()` 在失败时抛出、在用户取消时返回已播句。
- `build_streaming_session(...)` 只接受**显式注入**的 ws/http provider 与设备 backend factory；不读环境、不隐式建连接。
- `CompletedReplySpeaker`：`voicestudio_mode` 白名单扩展为 `legacy`/`http_wav`/`ws_pcm`/`auto`；**`ws_pcm`/`auto` 必须显式提供 `session_factory`**，否则直接 `ValueError`（不允许隐式建网络或设备路径）。默认仍是 `legacy`，原有构造与行为不变。

## 5. 测试

- `tests/tts/test_transport_router.py`（18 项）：模式选择、建连/协议/首音前超时分别降级、**出音后失败不降级不重播**、HTTP 备用失败只上报、预算 0 保留原错误、每句重新先试 WS、取消不降级、快照与最近失败、`close()` 幂等、错误不泄漏底层细节。
- `tests/tts/test_streaming_session.py`（18 项）：单句合成播放、多句原序、final tail、PCM 身份继承、传输失败记录 error、无 Edge→文字、有 Edge→EDGE 决策、**出音后失败→`PLAYBACK_STARTED` 且不换声**、无 router 只记 error、`ws_pcm` 不碰 HTTP、`auto` 同句降级 HTTP、取消/重复取消、取消后 finish 返回空、close 幂等、`owns_worker` 关闭属主 worker、空/非字符串忽略、背压、干净结束的 error/played/selection/language。
- `tests/foundation/test_config.py` 新增 7 项；`tests/tts/test_completed_reply.py` 新增 4 项（`ws_pcm`/`auto` 必须注入、非法 mode 报完整列表、注入 factory 生效、legacy/http_wav 不要求注入）。

## 6. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest tests.tts.test_streaming_session -q
18 tests, OK

.\.venv\Scripts\python.exe -m unittest tests.tts.test_transport_router -q
18 tests, OK

.\.venv\Scripts\python.exe -m unittest tests.foundation.test_config -q
27 tests, OK

.\.venv\Scripts\python.exe -m unittest discover -s tests/tts -p "test_*.py" -q
460 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
966 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

只读审计：本批 4 个文件（`transport.py`、`settings.py`、`streaming_session.py`、`completed_reply.py`）及对应测试均严格 UTF-8、无 BOM、无 `U+FFFD`；AST 调用面**无** `WinDLL`/`Popen`/`system`/`urlopen`/`socket`/`subprocess`/`connect`/`create_string_buffer`/`memmove` 等入口。

**`legacy`/`http_wav` 不变性证据**：`RealtimeVoiceSession` 未被修改；`test_realtime_voice`（30 项）与 `test_voicestudio_http_wav`、`test_completed_reply` 原有用例全部保持通过。

## 7. 未执行与遗留风险

- **未做真实 VoiceStudio 端到端**：没有真实 WS 流式播放、没有真实 WS→HTTP 降级演练、没有真实设备上的多句流式播放；本批全部为离线（fake provider + fake PCM backend）。
- 没有性能/延迟指标采集（计划 19.2 的 p95 目标）。
- `StreamingVoiceSession` 尚未接入默认 runtime 或 `main.py`；`TTS_VOICESTUDIO_MODE` 仍默认 `disabled`，因此新路径只有显式注入才会运行。
- 计划 8.1 允许"PCM 已在本地起播缓冲、设备未开始播放"时用 HTTP 重做；本实现更保守（**出音即终态**），这一点与计划的宽松分支不同，已在第 4 节明确记录。
- 未处理 barge-in/迟到块（Batch 6）；未做 Edge Provider 的实际播放（跨 Provider 决策只产出契约）。
- 工作区仍包含大量既有 staged/untracked 文件；本轮未执行 `git add .`、commit 或 push。

## 8. 下一入口

1. 用户审阅本批 diff；
2. 单独授权 Batch 6（barge-in、取消顺序、迟到块跨回复隔离）；
3. 或先做真实环境验收（真实 WS 流式播放 + 真实 WS→HTTP 降级 + 真实设备多句流式），需要单独授权与证据 ledger；
4. Batch 7（真实设备与性能）、Batch 8（默认切换与回滚）仍未开始。

## 9. 真实环境验收（已执行）

环境：本机 VoiceStudio `0.5.6`、`/health` 200、`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`。方式：真实 `VoiceStudioWebSocketProvider` + 真实 `VoiceStudioHttpWavProvider` + `VoiceStudioTransportRouter(mode="auto")` + 真实 `WaveOutPCMStreamBackend` + `PCMPlaybackWorker` + `StreamingVoiceSession`，播放确认音色；固定非敏感文本，不落盘音频。batch_id `p7-batch5-live-streaming-3c16284ba9e8453d`。

| 阶段 | 场景 | 结果 |
|---|---|---|
| A | 真实 WS 流式播放 3 句 | `played=3`、`error=None`、**真实 `waveOutWrite` 32 次**（3 buffer 轮转复用）、**fallbacks=0**（全部走 WS）、`cleanup_verified=true` |
| B | 用未监听端口 `ws://127.0.0.1:3999` 触发真实 WS 建连失败 | `played=1`、`error=None`、**fallback 记录 `(stage=connect, reason=connect_failed)`**、真实 HTTP `/generate` 接管并完成播放、`cleanup_verified=true` |

- 证明：真实 VoiceStudio 的 WS PCM 流能经 transport router 进入 PCM worker 并按句播放；真实建连失败会触发**本句一次**的 HTTP 降级且最终成功播放；两条路径的 waveOut buffer 都完整回收（`uncertain=false`）。
- 严格不推断：无声学测量、无人工确认，`speaker_acoustically_verified=false`、`user_heard=null`；`STOP_ACKED` 仍只表示 backend 归还 buffer。
- 证据 artifact：`implementation_plan/checkpoints/artifacts/2026-09-30-P7-batch5-live-streaming/p7-batch5-live-streaming-3c16284ba9e8453d.json`。

### 9.1 验收过程中发现的独立阻断（未修）

`%LOCALAPPDATA%\ComputerVoiceAssistant\xiaobing_voice_selection.json` **及此前的备份 `.bak-20260930` 均已不在磁盘上**（目录中只剩 `audit`/`logs`/`models`/`plans`/`temp`）。项目代码中不存在删除该文件的逻辑（`private_selection.py` 只清理自己的临时文件），因此判定为**外部删除**，原因未确认。

后果：`read_selection()` 当前必然失败，`CompletedReplySpeaker.start_stream()` 会返回 `None`、`__call__` 置 `_disabled=True` —— **真实默认说话链路（Agent→说话）当前无法出声**。本次验收为绕开该阻断，使用内存中项目已确认的值构造 `VoiceSelection` 并**仍然执行了真实 `verify_reference` 与 `verify_provider`**（provider 身份与参考音频 sha256 均通过）；`selection_source` 在 artifact 中标记为 `in_memory_confirmed_values_file_missing`。

该阻断与本批代码无关。**已于 2026-09-30 经用户确认完成重建**：以项目已确认的值写入同值文件（179 bytes、UTF-8 无 BOM、`speed=1.0`、`pause_ms=400`、`fade_ms=80`、`version=2`、权限 0600、临时文件 + `os.replace` 原子替换），随后 `read_selection` / `verify_reference` / `verify_provider` 三项全部通过。删除原因仍未查明。
