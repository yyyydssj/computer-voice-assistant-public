# VoiceStudio 双路径逐句语音执行文档

- 日期：2026-09-29
- 项目：`<PROJECT_ROOT>`
- 文档性质：实现前执行计划；本文不表示代码已经修改或功能已经验收
- 状态对账（2026-09-30）：**Batch 1–8 与 B9/B9-fix/B10/B11 均已执行**；§24 的 14 项 DoD 中 **12 项满足、2 项未满足**（用户听感证据、默认模式切换）；§19.2 性能指标已由真实数据证明通过。**P7 整体仍为 `Blocked`，未进入 P8。** 详见 [`checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md`](checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md) 与 [P7 跟踪表](P7-followup-execution-tracker.md)。
- 目标方案：**WebSocket PCM 主路径 + HTTP `/generate` 完整 WAV 备用路径**
- 当前阶段：P7/TTS 增量优化；不得借此进入 P8，不改变任务执行、安全、授权或审计语义

## 1. 文档目的

本计划把 VoiceStudio 的两种能力同时保留下来，并明确各自职责：

1. **推荐版主路径**：连接 `ws://127.0.0.1:3900/ws/tts`，按完整句发送文本，接收 PCM16 音频块并尽快播放。
2. **快速版备用路径**：连接失败、协议异常或首音前失败时，按同一个完整句调用 `POST http://127.0.0.1:3900/generate`，收到完整 WAV 后播放。
3. **现有跨 Provider 降级**：VoiceStudio 的两条传输路径均不可用时，才进入用户预先配置的 Edge TTS；Edge 也不可用时保留文字输出。

最终目标不是让 VoiceStudio 一次合成整段约 100 字，而是让 LLM 回复一旦形成完整句，就立即提交该句；下一句继续生成文本时，上一句可以合成或播放。

## 2. 决策摘要

采用以下长期结构：

```text
LLM 增量文本
  → ReplySegmenter 完整句切分
  → 句子级调度与背压
  → VoiceStudio WebSocket PCM（主路径）
  → VoiceStudio HTTP WAV（同 Provider、同音色的传输备用）
  → Edge TTS（用户预先配置的跨 Provider 备用）
  → 纯文字
```

核心决策：

- WebSocket 和 HTTP 不是两个互斥版本，而是同一 VoiceStudio Provider 的两种传输方式。
- 正常运行使用 WebSocket；HTTP `/generate` 永久保留为兼容、诊断和自动降级路径。
- 一次只向 VoiceStudio 提交一个完整句；第一版不发送半个词、单个 token 或无标点的极短片段。
- WebSocket 连接可跨句复用，但**同一连接只允许一个在途请求**。VoiceStudio 当前协议没有客户端 request ID，禁止流水线并发，否则无法可靠区分返回块属于哪一句。
- 最终架构中，两种传输都转换为统一 PCM chunk，播放层不判断音频来自 WebSocket 还是 WAV；但 Batch 2 的快速版先复用现有完整 WAV worker，统一 PCM 改造从 Batch 3–5 渐进完成。
- 当前句已经实际开始播放后，不允许切换传输或 Provider，不允许重播整句，不允许半句换声。
- 功能通过显式配置渐进启用；任何时候都能回退到现有逐句 HTTP/WAV 路径。

## 3. 已验证事实与依据

### 3.1 VoiceStudio 版本与来源

2026-09-29 核对结果：

- GitHub 仓库：`https://github.com/debpalash/VoiceStudio`
- GitHub Release：`v0.5.6`，发布时间 2026-09-23
- Release commit：`3915a62cb482bb43117aaebab57d22745bd1cc21`
- 本机应用版本：`0.5.6`
- 本机服务地址：`http://127.0.0.1:3900`
- License：AGPL-3.0
- 本机已检查源码：
  - `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\backend\api\routers\tts_stream.py`
  - `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\backend\api\routers\openai_compat.py`
  - `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\backend\services\tts_backend.py`

本项目只通过本地公开 API 与 VoiceStudio 通信，不复制 VoiceStudio 内部实现。若未来修改并分发 VoiceStudio 本体，必须单独复核 AGPL-3.0 义务。

### 3.2 `/ws/tts` 的实际协议

VoiceStudio v0.5.6 的 WebSocket 路由为：

```text
ws://127.0.0.1:3900/ws/tts
```

客户端发送示例：

```json
{
  "text": "这是一句完整的话。",
  "voice": "profile_id",
  "language": "zh",
  "speed": 1.0,
  "engine": "omnivoice"
}
```

服务端顺序为：

1. 返回 `start` JSON，声明 `sample_rate`、`channels`、`format` 和 engine；
2. 返回一个或多个 binary frame；内容为 24 kHz、单声道、PCM16 little-endian；
3. 返回 `done` JSON，包含 `duration_s`、`gen_time_s`、`ttfa_ms`、`rtf`、`samples` 等；
4. 连接保持打开，可继续处理下一句。

默认 `CHUNK_SAMPLES=4800`，即每块约 200 ms 音频。

必须明确：当前 v0.5.6 内部仍执行类似以下流程：

```python
wav = backend.generate(sentence_text, **kw)
```

也就是先生成完整句的波形，再把波形切成 PCM 块发送。它不是模型内部逐帧生成。因此本方案的主要低延迟收益来自：

- 上游提前按句提交，而不是等待整段回复；
- 复用 WebSocket 连接；
- 省去完整 WAV 封装、读取和再次解析；
- PCM 块到达后直接进入播放缓冲；
- 为未来 VoiceStudio 真正增量生成保留兼容接口。

不得把源码注释中的 `<100ms TTFA` 目标写成本机承诺值；验收以真实设备测量结果为准。

### 3.3 `/generate` 的实际行为

现有项目 `app/tts/real_playback.py::_generate()` 已调用：

```text
POST http://127.0.0.1:3900/generate
stream=false
Accept: audio/wav
```

该路径会等待完整 WAV 返回，校验为 24 kHz、mono、16-bit 后再播放。它已经支持逐句调用，是最快能稳定落地的路径，应保留为：

- WebSocket 建连或协议失败时的同音色备用；
- WebSocket 功能开关关闭时的默认兼容模式；
- 对比性能和定位故障的基准路径；
- 不支持 WebSocket 的旧版 VoiceStudio 兼容路径。

### 3.4 不采用 `/v1/audio/speech` 作为主路径

VoiceStudio 的 OpenAI 兼容接口即使设置 `stream_format="audio"` 或 `stream_format="sse"`，当前源码也是先生成 `audio_bytes`，再把完成的音频分块返回。它没有比 `/ws/tts` 更强的真实增量能力，且会增加编码格式和 SSE 解析分支，因此本轮不接入。

## 4. 当前项目链路与可复用能力

项目已有以下基础，不应重写：

- `app/agent/adapter.py`：Agent 已支持 `on_text_chunk`；
- `app/conversation/manager.py`：已把增量文本回调传给 Agent；
- `app/ui/text_shell.py`：已创建实时语音 stream；
- `app/tts/completed_reply.py`：已创建 `RealtimeVoiceSession`；
- `app/tts/segmentation/__init__.py`：已有完整句边界、长度边界、软标点回退、顺序检查和队列预算；
- `app/tts/real_playback.py`：已有逐句合成和播放线程；
- `app/tts/playback_worker.py`：已有长生命周期播放 worker、Windows `waveOutReset`、buffer return 和 stop ACK；
- `app/tts/requests/`、`lifecycle/`、`playback/`：已有 provider request、generation fence、迟到事件隔离和播放事实；
- `app/tts/routing.py`：已有 VoiceStudio → Edge → 文字的安全降级契约；
- `tests/tts/`：已有分句、播放、停止、故障矩阵和路由测试。

当前实时默认配置为：

```python
min_segment_characters = 4
max_segment_characters = 120
max_queued_segments = 8
```

`RealtimeVoiceSession` 已能在 `finish()` 前合成第一句，并把 pending 句子限制为 2。当前主要缺口不是“能否逐句”，而是：

1. 实时路径仍只使用完整 WAV；
2. 120 字的硬上限对实时对话偏长；
3. 播放 worker 以完整 WAV ticket 为单位，不能消费持续到达的 PCM；
4. VoiceStudio 内部传输降级尚未与现有跨 Provider 降级分层；
5. WebSocket 取消、断线、迟到块和 generation fence 尚未接入真实播放链。

## 5. 目标运行时架构

```text
┌──────────────────────┐
│ Agent on_text_chunk  │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ ReplySegmenter       │  硬标点 / 长度边界 / final tail
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ SentenceScheduler    │  顺序、pending=2、generation fence
└──────────┬───────────┘
           ▼
┌─────────────────────────────────────────────────────┐
│ VoiceStudioTransportRouter                          │
│  1. VoiceStudioWebSocketProvider                    │
│  2. VoiceStudioHttpWavProvider（首音前失败才接管）   │
└──────────┬──────────────────────────────────────────┘
           ▼
┌──────────────────────┐
│ PCM normalization    │  24k / mono / s16le / seq
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ PCMPlaybackWorker    │  起播缓冲、waveOut 多缓冲、stop ACK
└──────────┬───────────┘
           ▼
        Windows 音频设备

VoiceStudio 两条路径均失败
           ▼
现有 TTSFallbackRouter → Edge → 文字
```

## 6. 统一数据契约

### 6.1 句子请求

新增不可变请求对象，字段至少包括：

```python
@dataclass(frozen=True, slots=True)
class VoiceStudioSentenceRequest:
    reply_id: str
    segment_id: str
    provider_request_id: str
    provider_generation: int
    text: str
    profile_id: str
    language: str
    speed: float
    engine: str | None
```

规则：

- `text` 必须是一个已经被 `ReplySegmenter` 接受的句段；
- request 必须绑定现有 `reply_id`、`segment_id`、provider request 和 generation；
- 传输层不得自行重切句、拼接回复或修改文字；
- VoiceProfile 快照在句子提交前固定，句子播放期间不得换音色。

### 6.2 标准化 PCM 块

```python
@dataclass(frozen=True, slots=True)
class PCMChunk:
    reply_id: str
    segment_id: str
    provider_request_id: str
    provider_generation: int
    sequence: int
    sample_rate: int
    channels: int
    sample_width: int
    data: bytes
    is_final: bool
```

初始约束：

- `sequence` 从 1 开始严格递增；
- `sample_rate == 24000`；
- `channels == 1`；
- `sample_width == 2`；
- `data` 长度必须为偶数；
- 普通 chunk 不得为空；
- `is_final=True` 标在最后一个实际音频块上，不额外制造空音频；
- generation、request、segment 任一不匹配时，块只能进入脱敏诊断，不能进入播放队列。

### 6.3 Provider 接口

传输适配器统一暴露同步、可取消的迭代接口，便于接入当前线程模型：

```python
class VoiceStudioAudioProvider(Protocol):
    def stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        *,
        cancelled: threading.Event,
    ) -> Iterator[PCMChunk]: ...

    def close(self) -> None: ...
```

选择同步接口的原因：现有 `RealtimeVoiceSession`、播放 worker 和取消逻辑都以线程和阻塞等待为主。第一版不引入额外 asyncio event loop，也不在 UI 线程运行网络或音频操作。

## 7. 主路径：VoiceStudio WebSocket Provider

### 7.1 客户端依赖

新增一个 WebSocket 客户端依赖：

```text
websockets==17.1
```

采用 `websockets.sync.client.connect`，不同时引入 `websocket-client`、`httpx` 或 `aiohttp`。执行批次中将 wheel 放入项目离线 wheelhouse，并记录版本、SHA-256、Python 兼容范围和许可证。本文编写阶段不安装依赖。

### 7.2 连接生命周期

- 每个活动实时语音会话拥有一个 WebSocket provider；
- 首句到来时惰性建连，避免启动阶段阻塞 UI；
- 正常情况下跨句复用连接；
- 同一连接一次只能有一个句子请求；
- 回复结束后可短暂保留连接，session 关闭或空闲超时后主动关闭；
- 网络异常、协议异常、取消或 stop 发生时关闭当前 socket；下一轮需要语音时新建连接；
- 不自动无限重连；每个句子最多执行一次 WebSocket 尝试和一次 HTTP 备用尝试。

### 7.3 严格协议状态机

```text
DISCONNECTED
  → CONNECTING
  → READY
  → REQUEST_SENT
  → START_RECEIVED
  → AUDIO_RECEIVING
  → DONE_RECEIVED
  → READY
```

失败状态：

```text
CONNECT_FAILED
PROTOCOL_FAILED
TIMED_OUT_BEFORE_AUDIO
FAILED_AFTER_AUDIO
CANCELLED
CLOSED
```

验证规则：

- binary frame 出现在 `start` 之前：协议失败；
- `start.format` 不是 `pcm16`：协议失败；
- `sample_rate != 24000` 或 `channels != 1`：协议失败；
- JSON 类型不是允许的 `start`、`routing`、`done`、`error`：记录并 fail-closed；
- `done.samples` 与收到 PCM 样本数不一致：该句失败并记录，不把不完整句标为完成；
- 收到 `error` 后不得继续接受该请求的 audio；
- socket 关闭但未收到 `done`：句子不完整；
- 单帧和累计音频都设置上限，防止异常服务无限占用内存。

### 7.4 初始超时

以下是首版工程值，后续只能基于真机指标调整：

| 参数 | 初始值 | 行为 |
|---|---:|---|
| WebSocket connect timeout | 2.0 s | 超时后若未播放，转 HTTP `/generate` |
| 单句总超时 | 18.0 s | 与现有 `/generate` 上限保持同一量级 |
| 相邻 frame idle timeout | 3.0 s | 防止连接半开永久等待；**仅在收到 `start` 之后生效** |
| session idle close | 30.0 s | 无新句则释放 socket |
| 最大单句 PCM | 16 MB | 超过即失败并关闭 socket |

这些值不改变 VoiceStudio 自身生成 timeout，只约束本项目客户端等待。

> 2026-09-30 真实 preflight 修正：VoiceStudio 只在**首句整句合成完成后**才发送 `start`（本机 v0.5.6 `tts_stream.py` 已确认），因此 `start` 之前的首次 recv 等待上限由**单句总超时**约束，`3.0 s` 的帧间隔 idle 只应用于 `start` 之后的帧间隔。修复前把帧 idle 套在首个 recv 上，会让默认配置在真实 VoiceStudio 上第 1 句即 `frame_idle_timeout`。实测详见 `checkpoints/2026-09-30-P7-voicestudio-batch3-websocket-client.md`。

### 7.5 取消行为

VoiceStudio v0.5.6 的 `/ws/tts` 没有显式 request cancel frame，因此取消采用：

1. 设置当前 session 的 cancelled flag；
2. 停止向播放层提交新块；
3. 关闭当前 WebSocket；
4. 推进 provider generation；
5. 清空旧 generation 的 PCM 队列；
6. 调用播放 worker stop，保留现有 `waveOutReset + buffer return` 语义；
7. 下一轮新回复创建新 socket，不复用已取消连接。

## 8. 备用路径：VoiceStudio HTTP WAV Provider

`VoiceStudioHttpWavProvider` 从现有 `_generate()` 和 `_checked_wave()` 提取，不改变 VoiceStudio 请求字段和格式校验。

处理流程：

```text
完整句
→ POST /generate, stream=false
→ 完整 WAV
→ 严格校验 RIFF/WAVE、24k、mono、16-bit、帧数和大小
→ 提取 raw PCM
→ 按 4,800 samples（约 200 ms）切为 PCMChunk
→ 进入与 WebSocket 相同的播放 worker
```

HTTP 路径切块不是为了提前获得音频；WAV 仍需完整返回。切块的目的只是统一播放层和取消逻辑。

### 8.1 VoiceStudio 内部传输降级

只有满足以下全部条件时，WebSocket 失败才能由 HTTP 重做当前句：

- 当前 `provider_generation` 仍有效；
- 当前句的本地播放事实尚未进入 `PLAYBACK_STARTED`；
- 用户没有取消或插话；
- 本句还没有执行过 HTTP 备用尝试；
- VoiceProfile 快照仍与原请求一致。

如果 PCM 已收到但仍只在本地起播缓冲中，且设备没有开始播放，可以丢弃缓冲后由 HTTP 重做该句。

### 8.2 已播放后的处理

一旦当前句实际播放了任何 PCM：

- WebSocket 失败不得切换 HTTP；
- 不重播当前句；
- 不用 HTTP 或 Edge 从句中间续播；
- 停止当前回复的剩余语音；
- 完整文字仍正常展示；
- 下一轮回复再根据健康状态选择可用路径。

## 9. 完整句切分规则

### 9.1 第一版边界

硬边界：

```text
。！？；.!?;\n
```

软边界：

```text
，、：, :
```

首版采用确定性规则，不加入 300–500 ms 定时 flush，避免引入计时器竞态、短语气碎片和取消边界复杂度。

### 9.2 实时语音专用初始值

不全局改写所有分句调用方，而是给实时语音会话传入专用配置：

| 参数 | 初始值 | 说明 |
|---|---:|---|
| 最小可发字符数 | 4 | 极短 token 继续缓冲；final tail 例外 |
| 软切分目标 | 32 个字符 | 长句优先在目标附近找软标点 |
| 软切分搜索区间 | 24–40 个字符 | 优先自然停顿 |
| 绝对硬上限 | 48 个字符 | 没有软标点时强制有界切分 |
| pending 文本句 | 2 | 与现有 `RealtimeVoiceSession` 保持一致 |
| segment admission budget | 8 | 保留现有防失控上限 |

规则优先级：

1. 遇到硬标点且片段达到最小长度，立即发句；
2. 长度达到 32 后，在 24–40 范围内优先选择最后一个软标点；
3. 达到 48 仍无自然边界时执行长度切分；
4. 回复完成时发送剩余 final tail；
5. 任何顺序错误、重复 chunk、旧 reply 或取消后的 chunk 继续按现有契约拒绝。

## 10. PCM 播放与 Windows waveOut 改造

### 10.1 两阶段迁移

**快速阶段**仍可使用现有完整 WAV `PlaybackWorker`，先证明逐句调度和 fallback。

**推荐阶段**新增 PCM 流式 worker，不直接破坏现有完整 WAV worker。通过测试后，实时会话切到 PCM worker；预览和证据工具可继续使用原 worker，直到单独迁移。

### 10.2 PCM worker 职责

新增 `PCMPlaybackWorker` 和 `WaveOutPCMStreamBackend`，职责为：

- 固定格式 24 kHz、mono、PCM16；
- 为一个句子维护有界 PCM 队列；
- 达到起播阈值后提交第一批 buffer；
- 使用 3 个循环 `WAVEHDR` buffer，只有收到 `WOM_DONE` 后才能复用或释放；
- 保持 chunk 顺序，拒绝重复、跳号和旧 generation；
- 最后一块播放完成后把句子标为 `COMPLETED`；
- stop 时调用真实 `waveOutReset`，等待所有已提交 buffer 返回；
- 延续现有事实边界：`STOP_ACKED` 仅表示 backend reset/buffer return，不代表扬声器物理静音或用户听见。

### 10.3 缓冲与背压初始值

| 参数 | 初始值 | 说明 |
|---|---:|---|
| 起播目标 | 160 ms | 收到 200 ms 默认块时可立即起播 |
| 起播最大等待 | 首块到达即计算 | 不额外等待第二块 |
| 单句内存上限 | 16 MB | 与 provider 上限一致 |
| 总待播音频上限 | 8 s | 超过则暂停上游句子提交 |
| waveOut buffer 数量 | 3 | 一个播放、一个待播、一个回收 |
| stop ACK 等待 | 继承现有 worker 上限 | 不另造第二套停止语义 |

播放队列不使用“收到音频”等价“用户已听到”。必须继续区分：

```text
NOT_COMMITTED
COMMITTED_NOT_STARTED
PLAYBACK_STARTED
PARTIALLY_PLAYED
PLAYED
```

## 11. 调度、队列与背压

句子调度器遵循：

- 文本 pending 最多 2 句；
- VoiceStudio 同一时刻最多 1 个在途句子请求；
- 播放可以与下一句合成重叠，但不得并发播放两个句子；
- 总待播 PCM 达到 8 秒后，不再向 Provider 提交下一句，直到播放消耗；
- 不丢弃已经接受的文字；语音压力过大时宁可停止当前回复语音并保留文字，也不无限堆积；
- 回复结束不代表等待所有旧音频无限完成；所有 wait 都有上限；
- UI、Agent 和电脑任务执行线程不得被 TTS 网络或播放等待阻塞。

推荐并行关系：

```text
播放句 1  ────────────────┐
合成句 2       ───────────┤  允许
合成句 3 同时开始          │  禁止：VoiceStudio 单请求串行
播放句 2 在句 1 未完成开始 │  禁止
```

## 12. 取消、barge-in 与 generation fence

用户插话、显式停止、会话关闭或回复失效时，按以下固定顺序执行：

1. 标记 reply cancelled，拒绝后续旧文本 chunk；
2. 停止提交新的句子请求；
3. 关闭当前 WebSocket 或取消 HTTP 等待结果的消费资格；
4. 推进 VoiceStudio provider generation；
5. 清空旧 generation 的待合成句和 PCM；
6. 停止当前 playback ticket/stream；
7. 记录 stop 请求和 backend ACK；
8. 旧线程、旧 socket、迟到 HTTP 响应、迟到 PCM 和迟到 done 只能写诊断，不能恢复播放。

取消必须是幂等的。重复调用不得创建新请求、重复推进状态或把未知 stop 写成 ACK。

## 13. 分层 fallback 状态机

### 13.1 第一层：VoiceStudio 内部传输

```text
WS_READY
  → WS_SENTENCE_ACTIVE
  → 成功：继续 WS
  → 首音前失败：HTTP_WAV_ONCE
  → 已播放后失败：STOP_CURRENT_REPLY_VOICE
```

### 13.2 第二层：跨 Provider

```text
VoiceStudio WS 失败
  → VoiceStudio HTTP 也失败
  → 当前句尚未播放且 Edge 已配置：按现有路由规则进入 Edge
  → 当前句已播放：停止本轮，不跨 Provider 续播
  → Edge 失败或未配置：纯文字
```

#### 13.2.1 实现注记（2026-09-30）：Edge 槽由离线 SAPI 声线实现

- Edge 槽当前由**离线 Windows SAPI 声线**实现（`app/tts/providers/edge_sapi.py` + 纯 ASCII 固定脚本 `app/tts/providers/edge_sapi_runner.ps1`），默认声线 `Microsoft Huihui Desktop`（zh-CN）。**不联网、不依赖 `edge-tts`、不访问 VoiceStudio**；在线 edge-tts 属另一批工作。
- 输出格式与 VoiceStudio 契约逐字段一致：**24000 Hz / 16-bit / 单声道 PCM WAV**，按 `chunk_samples=4800`（200 ms，与 WS 分块同尺寸）切片成有序 `PCMChunk`，交给**同一个** `PCMPlaybackWorker`；`is_final` 只出现在最后一块。
- 句文本**不经命令行**（避免参数注入）：写入私有临时目录下唯一 UTF-8 文本文件，脚本用 `[System.IO.File]::ReadAllText($TextPath, UTF8)` 读回。随包 `.ps1` 为**纯 ASCII**，因为 PowerShell 5.1 会把无 BOM 的 `.ps1` 按 ANSI(GBK) 解析，中文会乱码并使脚本解析失败。临时 txt/wav 无论成功或失败都被清理，清理失败不掩盖原异常。
- 切换规则严格按本节：仅当**当前句尚未开始播放**（`NOT_COMMITTED` / `COMMITTED_NOT_STARTED`）且 Edge 已配置时，才用 Edge 重做**同一句一次**：不循环、不重试、不重播；已出音（`PLAYBACK_STARTED`）绝不换声、绝不重播；`CONTINUE_TEXT_NO_REPLAY` 保持既有转文字路径。
- SAPI 失败（声线不可用、超时、非 0 退出码、WAV 缺失/非法/超限、取消）一律 `EdgeSapiError(<短代码>)`：**不把子进程 stderr、句文本或临时路径写进异常、`repr` 或日志**。失败后调用 `router.edge_failed(reply_id, segment_id, playback_state=...)` 得到文字决定，本回复剩余语音停止，**不尝试第三个 Provider**。
- 会话侧新增只读诊断：`edge_attempts`、`edge_failures` 与 `late_events` 中的 `edge_used@generationN` / `edge_failed@generationN`（脱敏，无文本）。`app/tts/routing.py` 未改动，`app/main.py` 未接线。
- 配置项 `TTS_EDGE_VOICE` 默认**空字符串 = Edge 未配置 = 路由到文字**；非空时按 `EdgeSapiConfig.voice_name` 的字符集规则校验，不含 URL/凭据字段。
- 离线测试证据与逐文件改动见 checkpoint `implementation_plan/checkpoints/2026-09-30-P7-b11-offline-edge-voice.md`；**真实出声验收由 Lead 之后单独执行**。

### 13.3 恢复规则

- VoiceStudio 恢复后只对下一轮新回复生效；
- 当前 reply 不从 HTTP/Edge 中途切回 WebSocket；
- 不补播旧回复；
- 不循环探测或重试；
- 健康探测使用固定无隐私文本，并与正式播放队列隔离；
- 语音故障不改变计划、授权、执行、验证、账本或任务结果事实。
## 14. 配置与默认值

在项目配置层新增以下设置：

```text
TTS_VOICESTUDIO_MODE=http_wav
TTS_WS_URL=ws://127.0.0.1:3900/ws/tts
TTS_HTTP_GENERATE_URL=http://127.0.0.1:3900/generate
TTS_WS_CONNECT_TIMEOUT_S=2.0
TTS_SENTENCE_TIMEOUT_S=18.0
TTS_WS_FRAME_IDLE_TIMEOUT_S=3.0
TTS_WS_SESSION_IDLE_S=30.0
TTS_REALTIME_SOFT_TARGET_CHARS=32
TTS_REALTIME_HARD_MAX_CHARS=48
TTS_REALTIME_MAX_PENDING_SEGMENTS=2
TTS_PCM_START_BUFFER_MS=160
TTS_PCM_MAX_QUEUED_AUDIO_MS=8000
```

`TTS_VOICESTUDIO_MODE` 取值：

- `http_wav`：只使用快速版，作为第一批上线默认；
- `ws_pcm`：只使用 WebSocket，用于协议和性能诊断；
- `auto`：WebSocket 主路径，首音前失败时自动转 HTTP；最终验收后成为默认。

发布顺序必须先 `http_wav`，再 opt-in `ws_pcm`，最后才把默认改为 `auto`。

## 15. 诊断事件与隐私

至少记录以下时间点，全部使用单调时钟：

```text
llm_first_chunk_at
sentence_ready_at
tts_submitted_at
ws_connected_at
start_frame_at
first_pcm_received_at
playback_committed_at
playback_started_at
playback_completed_at
stop_requested_at
stop_acked_at
```

派生指标：

```text
sentence_wait_ms
tts_queue_ms
ttfa_ms
first_pcm_to_playback_ms
queue_audio_ms
sentence_total_ms
fallback_latency_ms
```

诊断还需记录：

- transport：`ws_pcm` / `http_wav` / `edge` / `text`；
- fallback 原因和发生阶段；
- reply、segment、provider request、generation 和 chunk sequence；
- WebSocket close code、协议错误类别和 timeout 类别；
- stop ACK 状态；
- VoiceStudio 返回的 `ttfa_ms`、`rtf` 仅作为服务端指标，不能代替本地起播指标。

隐私规则：

- 默认不记录完整回复文本和 PCM；
- 错误 detail 必须脱敏和限长；
- 不记录 VoiceProfile 参考音频内容；
- 不把健康探测音频写入正式对话历史；
- 不从 `COMPLETED`、WAV 存在或设备调用返回推导 `user_heard=true`。

## 16. 文件级修改清单

以下是预计写入范围。实施时若现有接口足以承载，应优先减少文件，而不是扩大重构。

### 16.1 新增文件

| 文件 | 作用 |
|---|---|
| `app/tts/providers/__init__.py` | 导出 VoiceStudio 传输适配器 |
| `app/tts/providers/contracts.py` | `VoiceStudioSentenceRequest`、`PCMChunk`、provider Protocol |
| `app/tts/providers/voicestudio_ws.py` | `/ws/tts` 同步客户端、协议状态机、取消和超时 |
| `app/tts/providers/voicestudio_http.py` | 从现有 `/generate` 提取 WAV 请求、校验和 PCM 切块 |
| `app/tts/pcm_playback.py` | PCM worker、起播缓冲、队列和 backend Protocol |
| `app/tts/waveout_pcm.py` | Windows 多 buffer `waveOut` 实现和 stop ACK |
| `tests/tts/test_voicestudio_ws.py` | fake WebSocket 协议、超时、断线、迟到块测试 |
| `tests/tts/test_voicestudio_http_provider.py` | WAV 校验和统一 PCM chunk 测试 |
| `tests/tts/test_pcm_playback.py` | PCM 顺序、缓冲、背压、stop 和 generation 测试 |
| `tests/tts/test_dual_path_realtime.py` | 主备切换和端到端离线契约测试 |
| `requirements-tts.txt`（暂定） | 项目当前没有明确的依赖清单文件；Batch 3 按项目最终依赖锁定策略确定文件名，并固定 `websockets==17.1`、wheel hash 与离线安装方式 |

### 16.2 修改文件

| 文件 | 修改范围 |
|---|---|
| `app/config/settings.py` | 增加双路径、timeout、分句和 PCM 缓冲配置；严格校验范围 |
| `app/tts/segmentation/__init__.py` | 增加实时软目标/硬上限能力，不破坏现有默认调用方 |
| `app/tts/real_playback.py` | 保留兼容 API；把 HTTP 合成委托给 provider；让实时 session 调用双路径调度器 |
| `app/tts/playback_worker.py` | 只抽取可复用 stop/事件语义；不一次性重写现有 WAV worker |
| `app/tts/completed_reply.py` | 注入 mode、provider、PCM worker 和 session 生命周期 |
| `app/tts/orchestration.py` | 绑定句子请求、PCM 流和现有 request/lifecycle 事实 |
| `app/tts/routing.py` | 增加 VoiceStudio 内部 WS→HTTP 决策，再调用现有 VoiceStudio→Edge→文字决策 |
| `app/tts/lifecycle/__init__.py` | 如现有 chunk 事件不足，只补充 PCM sequence/final 验证 |
| `app/tts/playback/queue.py` | 加入按毫秒计算的音频背压，不改变旧 generation 拒绝规则 |
| `tests/tts/test_segmentation.py` | 24–40 软切分、48 硬上限和 final tail |
| `tests/tts/test_realtime_voice.py` | 第一完整句提前提交、pending=2、取消和不重复播报 |
| `tests/tts/test_routing.py` | WS→HTTP→Edge→文字的分层 fallback |
| `tests/tts/test_playback_worker.py` | 共享 stop 事实不回归 |

### 16.3 不应修改

本功能不应修改：

- Agent 的任务规划、安全策略和授权逻辑；
- 电脑工具执行器、账本、路径安全和结果验证；
- 用户的 VoiceProfile 内容或默认音色；
- 历史真实证据 artifact；
- VoiceStudio 本机安装源码；
- P8 相关代码。

## 17. 分批实施步骤

每个批次都必须先写针对性测试，再实施，再更新 checkpoint。不得把八个批次合并成一次大改。

### Batch 1：统一契约与 fake adapter 基线（0.5–1 天）

目标：建立不依赖真实 VoiceStudio 和音频设备的统一边界。

执行：

1. 新增 request、PCM chunk 和 provider Protocol；
2. 把 reply/segment/request/generation/sequence 验证写成纯单元测试；
3. 建立 fake WS provider、fake HTTP provider 和 fake PCM sink；
4. 证明旧 generation、重复 sequence、跳号、格式错误和取消后块均被拒绝；
5. 不接入默认运行时。

退出条件：契约测试全绿，现有 TTS 回归全绿，未发生真实网络或设备调用。

### Batch 2：逐句 HTTP `/generate` 快速路径（1–1.5 天）

目标：先用现有稳定能力把“一句一句生成”体验落地。

执行：

1. 提取 `VoiceStudioHttpWavProvider`；
2. 保持 multipart 字段、VoiceProfile 校验和 WAV 安全上限；
3. 为实时 session 使用 32 软目标、48 硬上限、pending=2；
4. 继续使用现有完整 WAV worker，避免提前引入 PCM 设备复杂度；
5. 增加两句及长句测试，证明第一句在整段回复 finish 前开始合成；
6. 配置 `TTS_VOICESTUDIO_MODE=http_wav`。

退出条件：用户已经可以获得稳定的逐句 HTTP 版本；失败时保留完整文字；停止和旧回复隔离不回归。

### Batch 3：WebSocket 客户端与协议验证（1–1.5 天）

目标：完成 `/ws/tts` 客户端，但先输出到 fake PCM sink。

执行：

1. 固定并离线保存 `websockets==17.1`；
2. 实现惰性连接、单在途请求、跨句复用和 session idle close；
3. 实现 start/binary/done/error 状态机；
4. 实现 timeout、大小限制、格式校验和关闭行为；
5. 用 fake server 覆盖乱序 frame、缺 done、错误 samples、断线和取消；
6. 做一次显式 opt-in 的本机 VoiceStudio 协议 preflight，只收集 PCM，不播放。

退出条件：真实 VoiceStudio 至少完成三句串行请求，连接复用正确，收到的 PCM 格式与样本统计一致。

### Batch 4：PCM 流式播放后端（2–3 天）

目标：让到达的 PCM 块直接进入 Windows 播放。

执行：

1. 实现 `PCMPlaybackWorker`；
2. 实现三个 `WAVEHDR` 的轮转和回收；
3. 实现 160 ms 起播和 8 s 背压；
4. 实现自然完成、stop、reset、buffer return、timeout 和 unknown；
5. 用 fake WinMM 测试所有生命周期；
6. opt-in 真实设备短音频验证；
7. 保留原 WAV worker，不立即删除。

退出条件：PCM 顺序播放、自然完成、取消 stop ACK、迟到块丢弃和内存上限均有自动化测试；真实设备无 buffer 生命周期错误。

### Batch 5：双路径调度与自动降级（1–2 天）

目标：把 WebSocket 主路径和 HTTP 备用路径接入同一实时 session。

执行：

1. `auto` 模式先尝试 WS；
2. 建连、协议或首音前失败时，仅一次转 HTTP；
3. HTTP 也失败后再进入现有 Edge/文字路由；
4. 已开始播放后禁止当前句重试或换声；
5. 记录 transport、失败阶段、fallback 原因和延迟；
6. 保持默认配置为 `http_wav`，`auto` 只 opt-in。

退出条件：完整故障矩阵通过；不存在无限重试、重复播放、半句换声和跨 reply 续播。

### Batch 6：barge-in、取消与迟到块隔离（1–2 天）

目标：在用户插话时可靠终止旧回复语音。

执行：

1. 固化取消顺序；
2. 关闭 socket 并推进 generation；
3. 清空文本 pending、PCM pending 和播放队列；
4. 注入迟到 binary、done、HTTP WAV 和 worker callback；
5. 验证旧 generation 永远不能进入新回复；
6. 保持 stop ACK 与物理静音/用户听感的事实边界。

退出条件：取消幂等；所有迟到事件仅记录诊断；新回复不受旧线程和旧 socket 污染。

### Batch 7：真实 VoiceStudio、Windows 与性能验证（1–2 天）

目标：在本机 RTX 3050 Laptop GPU 和实际扬声器上形成可复核数据。

场景：

1. 冷启动第一句；
2. warm 模型连续 20 句；
3. 两句和五句回复；
4. 4、16、32、48 字句长；
5. WS 正常、WS 建连失败、首音前断线、播放中断线；
6. HTTP fallback 成功和失败；
7. 播放中 barge-in；
8. VoiceStudio 重启与下一轮恢复；
9. Edge 已配置和未配置两种状态。

输出：JSON artifact、指标汇总、服务端和本地时间线、stop ACK 事实、用户听感单独确认。

退出条件：全部真实场景有 batch_id 和证据文件；不从程序完成状态推导用户已听见。

### Batch 8：默认切换、文档、回滚与阶段验收（0.5–1 天）

目标：在证据充分后把默认从 `http_wav` 改为 `auto`。

执行：

1. 更新配置示例、运行说明和故障排查；
2. 记录依赖版本与 wheel hash；
3. 跑 TTS 专项、集成和全量回归；
4. 验证 `TTS_VOICESTUDIO_MODE=http_wav` 可一键回滚；
5. 更新 P7 tracker、progress 和 checkpoint；
6. 只有原 P7 全部退出条件通过时才改变 P7 状态；本功能完成本身不自动解除其他 P7 阻塞。

退出条件：`auto` 可默认启用，回滚经过演练，维护文档完整，无历史 artifact 被覆盖。

## 18. 测试矩阵

### 18.1 单元测试

| 模块 | 必测行为 |
|---|---|
| 分句 | 硬标点、软标点、48 硬上限、final tail、极短 fragment、中文引号尾随 |
| WS 协议 | start/binary/done、routing notice、error、乱序、未知 JSON、缺 done、错误 samples |
| HTTP | multipart 字段、Content-Type、大小、WAV header、采样率、声道、位宽、空音频 |
| PCM | sequence、final、格式、偶数字节、generation fence、内存上限 |
| 播放 | 起播阈值、FIFO、WOM_DONE、自然完成、reset、stop timeout、unknown、cleanup |
| fallback | WS→HTTP、HTTP→Edge、Edge→文字、播放后禁止 fallback |
| 取消 | 建连中、合成中、首块后未播放、播放中、done 后、重复 cancel |

### 18.2 集成测试

- Agent chunk → ReplySegmenter → fake WS → fake PCM worker；
- fake WS 首音前失败 → fake HTTP 成功；
- WS 和 HTTP 均失败 → fake Edge；
- Edge 未配置 → 文字；
- 播放开始后 WS 失败 → 当前 reply 剩余语音全部停止；
- reply A 取消后迟到 PCM 不得进入 reply B；
- 五句回复的连接复用、串行请求和播放顺序；
- 队列达到 8 秒时上游背压且 UI 不阻塞。

### 18.3 真实 opt-in 测试

真实网络、GPU、VoiceStudio 和扬声器测试继续使用显式开关，不纳入普通单元测试自动执行。每次真实运行记录：

- VoiceStudio 版本；
- VoiceProfile ID 的脱敏标识；
- mode；
- 句长和句数；
- batch_id、request_id、reply_id、segment_id；
- 服务端和客户端时间线；
- playback 和 stop 事实；
- 用户听感是否由用户明确确认。

## 19. 性能指标与验收标准

### 19.1 功能验收

必须全部满足：

- 两句以上回复不等待完整回复结束；第一句形成后立即进入 TTS 调度；
- 正常 WebSocket 模式连续三句只建立一个连接，且请求严格串行；
- HTTP fallback 保持相同 VoiceProfile、language 和 speed；
- 当前句未播放时允许 WS→HTTP；播放后绝不重播或换声；
- 取消后旧 reply 的任何音频都不能播放；
- VoiceStudio 双路径失败仍保留完整文字；
- 恢复只影响下一轮回复；
- TTS 失败不改变电脑任务事实和授权状态；
- `http_wav` 回滚模式始终可用。

### 19.2 工程性能验收

在 warm 模型、同一 VoiceProfile、同一测试文本集合下分别运行至少 20 句：

- `sentence_ready_at → tts_submitted_at` 的 p95 小于 20 ms；
- 第一 PCM 到达后，除配置的起播缓冲外，提交设备的 p95 额外开销小于 50 ms；
- 内存中的待播 PCM 永不超过配置上限；
- `auto` 模式相对 `http_wav` 的中位起播延迟不得恶化超过 10%；
- 若 WebSocket 没有显著加速，也仍需证明连接复用、PCM 统一播放和未来兼容价值；不得伪造性能收益；
- barge-in 的 stop 请求必须立即进入本地 worker，最终 ACK 继续按现有有界等待和事实语义判定；
- 无重复句、丢句、乱序句和旧 generation 播放。

本计划不承诺固定的端到端首音数字。模型加载、句长、GPU 竞争和 VoiceStudio 当前“先生成整句”实现都会显著影响结果。

#### 19.2.1 实现注记：句级时间线打点（2026-09-30，仅打点与统计，未采集真实 p95）

已落地的观测面（不改任何路由、generation、背压、取消与 stop 语义）：

- 六类单调时钟打点都在 `app/tts/streaming_session.py` 与 `app/tts/pcm_playback.py`，默认时钟为 `app/tts/clock.py` 的 `monotonic_seconds`（Windows 为 `time.perf_counter`，其他平台为 `time.monotonic`，可注入以便离线测试）：
  - `sentence_ready_at`：`_enqueue()` 中分句器产出的完整句进入待合成队列时；
  - `tts_submitted_at`：`_speak()` 中该句交给 `transport_router.stream_sentence()` 之前（provider 生成器在首次 `next()` 才真正开始，故这是不触碰 transport 内部的最接近提交点）；
  - `first_pcm_at`：`_speak()` 循环中该句第一块 PCM 从 provider 返回后；
  - `gate_open_at`：由 `PCMPlaybackWorker.device_gate_open_at(request)` 只读回读，worker 在 `_take_submission_locked()` 中起播缓冲闸门**首次满足**（`self._started` 翻为 `True`，即决定开始向设备提交）的那一刻用同一单调时钟、同一把 `self._condition` 锁打点；闸门阈值、`_started` 语义与提交路径一行未改；
  - `device_submit_at`：由 `PCMPlaybackWorker.device_submit_at(request)` 只读回读，worker 在把该句首批 PCM 交给后端（`backend.submit()` 返回成功）时用同一单调时钟打点。
- 只读统计面：`StreamingVoiceSession.timing_samples`（每句一条，仅含脱敏 `reply_id`、`generation`、`segment_index`、`ready_to_submit_ms`、`first_pcm_to_device_ms`、`first_pcm_to_gate_ms`、`gate_to_device_ms`、`submit_to_first_pcm_ms`）与 `StreamingVoiceSession.timing_percentiles()`（四段的 `count/p50/p95`）。样本只包含**播放完成**（`PlaybackState.COMPLETED`）的句子；取消、迟到块、背压与传输失败的句子不入样，只留在既有 `late_events` 诊断面；同一句至多一条；`close()` 后仍可读。
- 百分位定义（显式纯函数 `nearest_rank_percentile`，仅标准库）：丢弃非有限值后升序排序，`rank = ceil(p * count)` 并夹到 `[1, count]`，取 `sorted[rank-1]`；**不插值、不取中间两值平均**；空样本返回 `None`。`p50` 用 `0.5`，`p95` 用 `0.95`。缺失（`None`）的段不计入该段 `count`；`0.0` 是实测值，照常计入。
- 四/五段口径（判定用哪一段必须写死）：
  - `ready_to_submit_ms = tts_submitted_at - sentence_ready_at`（对应本节 p95 < 20 ms）；
  - `submit_to_first_pcm_ms = first_pcm_at - tts_submitted_at`：**合成/首音延迟**，本节第 3/4 条（`auto` 相对 `http_wav` 的中位起播延迟比较）**只能用这一段的 `p50`/`p95` 评估**，不得用墙钟或 `first_pcm_to_device` 代替；
  - `first_pcm_to_device_ms = device_submit_at - first_pcm_at`：**原始观测，含配置的起播缓冲累积**，只作诊断，**不得**直接与 50 ms 比较；
  - `first_pcm_to_gate_ms = gate_open_at - first_pcm_at`：起播缓冲累积本身，纯诊断；
  - `gate_to_device_ms = device_submit_at - gate_open_at`：**已排除起播缓冲的“额外开销”**，本节“除配置的起播缓冲外 p95 额外开销 < 50 ms”只能用这一段的 p95 判定；闸门与首次提交同刻时如实记为 `0.0`，不人为制造差值。
- 当前状态：只有离线确定性单测（注入时钟、fake provider/backend，20 句样本、四段百分位边界、缺失段、隐私与取消/迟到隔离）；真实采集结果见 19.2.2。

##### 19.2.1.1 口径补丁：B9 缺陷修复批次（2026-09-30，仅离线；真实复测另行授权）

对应真实采集（19.2.2）暴露的四个缺陷，本轮只改允许清单内的文件，`app/main.py`、`app/config/settings.py`、`app/tts/real_playback.py`、`app/tts/completed_reply.py`、`providers/**`、`tests/tts/test_streaming_session.py` 一行未改：

- **A 时钟分辨率**：新增 `app/tts/clock.py`，`monotonic_seconds` 在 `sys.platform == "win32"` 时为 `time.perf_counter`，否则为 `time.monotonic`；`clock_resolution_seconds()` 只读返回默认时钟分辨率（Windows 上 `monotonic` = `0.015625` s = 15.625 ms、`perf_counter` = `1e-07` s）。`StreamingVoiceSession`、`build_streaming_session`、`PCMPlaybackWorker`、`VoiceStudioTransportRouter`、`WaveOutPCMStreamBackend` 的 `clock` **默认值**改为 `monotonic_seconds`；显式注入的 `clock` 优先级与全部语义不变（两者都是单调时钟）。
- **B 每回复只验证一次（行为变更）**：`StreamingVoiceSession` 新增 `_verified`/`verified`；`_speak()` 的每句 `self._verifier(self.selection)` 改为整个会话（= 整次回复）只执行一次。依据：VoiceProfile/Provider 变更只允许在**句子/回复边界**生效，回复开始时验证一次已满足该边界；每句验证会把 `verify_provider()` 的 2 次 HTTP 往返加参考音频 SHA-256（实测 18.4 / 29.7 / 56.4 ms min/p50/max）计入 `ready_to_submit`。失败仍在同一处抛出并走既有 fail-closed 路径（`_error`/`_failed`、不播报、不产生样本、不产生 `played_segments`）。**新回复由新会话驱动，因此仍会重新验证一次**。
- **C 过载可诊断 + `finish()` 不假装说完**：`push_text_chunk()` 因 `_overloaded`/`_finished`/`_cancelled`/`_failed`/`_closed` 被拒时不再静默——用既有 `_record_late(...)` 记录脱敏 `push_rejected_<reason>@generationN`（**不写文本**）并累加只读计数 `dropped_pushes`；新增只读 `pending_sentences`。`finish()` 在 join 到期且会话线程仍在运行时记录 `finish_timeout` 诊断并置只读 `finish_timed_out = True`（默认 `False`），**不得**读作“已全部说完”；`finish()` 返回类型仍是 `tuple[str, ...]`。`close()` 仍按既有语义取消剩余语音（设计行为，但调用方现在可以察觉）。
- **D 合成/首音延迟打点**：`SentenceTimingSample` **追加**第 8 个字段 `submit_to_first_pcm_ms`（= `first_pcm_at - tts_submitted_at`，缺失为 `None`），`StreamingTimingPercentiles` **追加**第四段 `submit_to_first_pcm`；既有 7 个字段与三段统计的名字/含义/顺序不变（向后兼容）。本节第 3/4 条即用该段的 `p50`/`p95` 评估。
- 离线证据与逐文件清单见 [`checkpoints/2026-09-30-P7-b9-defect-fixes.md`](checkpoints/2026-09-30-P7-b9-defect-fixes.md)；本批**未联网、未访问 VoiceStudio、未使用扬声器/麦克风、未安装依赖、未改默认 runtime 接线、未 git add/commit/push**，也未采集任何真实数字：19.2 的第一条（p95 < 20 ms）与第 3/4 条仍需真实 20 句 warm 复测。

#### 19.2.2 真实采集结果（2026-09-30，真实 VoiceStudio + 真实 Windows 设备）

- 环境与预算、完整证据表、根因分析见 [`checkpoints/2026-09-30-P7-b9-real-p95-measurement.md`](checkpoints/2026-09-30-P7-b9-real-p95-measurement.md)；artifact 在 `checkpoints/artifacts/2026-09-30-P7-batch9-real-p95/`；runner `tools/p7_real_sentence_p95_runner.py`（无 `--live-opt-in` 则 exit 2）。
- **时钟口径（必须遵守）**：Windows `time.monotonic` 是 `GetTickCount64()`，分辨率 **15.625 ms**，实测 20 万次连续调用零正差值，用它打点会把 worker 侧跨度显示成 `0.000 ms`（读作“测不出”）。**真实采集必须注入 `time.perf_counter`**（QPC，1e-7 s），由 session/router/worker 共用同一时钟。**处置（2026-09-30，B9 缺陷修复批次）**：`app/tts/clock.py` 的 `monotonic_seconds` 现已是五个构造点的默认时钟（Windows = `time.perf_counter`），真实复测不再依赖 runner 额外注入，但显式注入仍优先；`clock_resolution_seconds()` 可当场核对分辨率。
- **场景口径**：标准测量用 paced（一句一句推入，pending ≤ 1，20 句）。一次推入 5×4=20 句会触发过载保护（`overloaded=True`、只播 9/20、`late_events=["playback:STOP_ACKED@generation2"]`，两次独立运行一致），单次 push 超过 8 句直接抛 `streaming_queue_budget_exceeded`；突发场景只能作为过载证据。
- **判定**：`gate_to_device` p95 = **0.0204 ms**（`ws_pcm`）/ **0.0229 ms**（`http_wav`）→ 第二条（除起播缓冲外 p95 < 50 ms）**通过**；`ready_to_submit` p50/p95 = **20.79 / 30.40 ms**（`ws_pcm`）、**29.23 / 31.44 ms**（`http_wav`）→ 第一条（p95 < 20 ms）**未通过**。
- **第一条的根因（真实缺陷）**：`_speak()` 在 `tts_submitted_at` 之前每句调用 `_verify_selection` → `verify_provider()` 每句做 2 次 HTTP 往返并下载参考音频做 SHA-256；实测单次 18.4 / 29.7 / 56.4 ms（min/p50/max），与实测跨度量级一致。必须把身份验证移出每句路径才能达成 < 20 ms。**处置（2026-09-30，B9 缺陷修复批次）**：已改为**每次回复只验证一次**（新会话再次验证一次），见 19.2.1.1 的 B 条；离线单测已证明 20 句只调用 1 次、失败仍 fail-closed、新会话再次调用。真实 p95 需按 19.2.1.1 另行复测，本批不声称已达标。
- **缺口**：缺 `tts_submitted_at → first_pcm_at`（合成/首音延迟）跨度，第 3/4 条（`auto` 与 `http_wav` 起播延迟比较）尚无法评估；本轮只比较了墙钟（61.56 s vs 61.15 s）。**处置（2026-09-30，B9 缺陷修复批次）**：已新增 `submit_to_first_pcm_ms` 与 `timing_percentiles().submit_to_first_pcm` 第四段，见 19.2.1.1 的 D 条；**第 3/4 条今后只读该段的 `p50`/`p95`**（不得用墙钟或 `first_pcm_to_device` 代替），真实比较待另行授权的 20 句 warm 复测。
- 边界：`user_heard=null`、`speaker_acoustically_verified=false`（后端完成 ≠ 有人听见）；未做声学测量、断线注入、HTTP 失败演练、默认切换。

#### 19.2.3 修复后真实复测结果（2026-09-30，同环境同场景，20 句 paced）

- 来源：[B9 缺陷修复 checkpoint](checkpoints/2026-09-30-P7-b9-defect-fixes.md) 第 9 节；artifact 三个（`ws_pcm`/`http_wav`/`auto`）在 `checkpoints/artifacts/2026-09-30-P7-batch9-real-p95/`。每个模式 **20/20 播完**、`verified=True`、`dropped_pushes=0`、`finish_timed_out=False`、`error=None`、`late_events=[]`。

| 模式 | `ready_to_submit` p50/p95 (ms) | 修复前 p50/p95 | `submit_to_first_pcm` p50/p95 (ms) | `gate_to_device` p95 (ms) |
|---|---|---|---|---|
| `ws_pcm` | **0.0919 / 0.2357** | 20.79 / 30.40 | 1446.8 / 1580.0 | 0.0241 |
| `http_wav` | **0.0985 / 0.1734** | 29.23 / 31.44 | 1488.7 / 1614.6 | 0.0290 |
| `auto` | **0.1083 / 0.1999** | （未测） | 1462.3 / 1739.9 | 0.0155 |

- **19.2 四条判定（真实数据，全部通过）**：① `ready_to_submit` p95 < 20 ms **通过**（最差 0.236 ms，较修复前下降约 130 倍）；② `gate_to_device` p95 < 50 ms **通过**（最差 0.029 ms）；③ `auto` 相对 `http_wav` 中位起播延迟不恶化 >10% **通过**（`submit_to_first_pcm` p50 1462.3 vs 1488.7 ms；p95 恶化 7.8%，如实登记）；④ WebSocket **未显著加速**（`ws_pcm` p50 仅快 2.8%），因此**不声称性能收益**，价值在连接复用、统一 PCM 播放与兼容性。
- 说明：`submit_to_first_pcm` 真实 p50 ≈ 1.45–1.49 s，印证 VoiceStudio 仍“先生成整句再出音”；本计划不承诺固定首音数字。
- 边界：`user_heard=null`、`speaker_acoustically_verified=false`；真实设备出声但无独立声学/听感证据。

## 20. 回滚方案

回滚不删除代码，按配置逐级降级：

1. `auto` 出现问题：改为 `TTS_VOICESTUDIO_MODE=http_wav`；
2. PCM worker 出现设备问题：实时路径恢复现有完整 WAV worker；
3. VoiceStudio HTTP 也不稳定：使用现有 Edge/文字路由；
4. stop 结果为 timeout/unknown：禁用本 session 语音，保留文字，不继续尝试旧音频；
5. 任何回滚都不覆盖 VoiceProfile、不重播旧回复、不修改历史 evidence。

发布过程中保留以下兼容面，直到 `auto` 至少经过一次完整阶段验收：

- 原 `_generate` 行为的兼容包装；
- 原完整 WAV `PlaybackWorker`；
- `http_wav` 配置模式；
- 原真实证据 runner 的输入/输出结构。

## 21. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| VoiceStudio WS 仍先生成整句 | 首音改善可能有限 | 缩短句段；以实测为准；保留 HTTP 基准 |
| WS 无 request ID | 并发响应无法归属 | 单连接单在途请求；取消即关闭 socket |
| Windows 多 buffer 生命周期复杂 | 崩溃、悬空 buffer、停止不可靠 | 独立新 backend；fake WinMM；WOM_DONE 后才释放 |
| 过短分句导致语调破碎 | 听感下降 | 最小 4 字；32 字目标；硬标点优先；首版无定时 flush |
| 过长句导致等待 | 实时感下降 | 24–40 软切分；48 硬上限 |
| fallback 重复播放 | 用户听到重复内容 | 以 `PLAYBACK_STARTED` 为唯一切换边界 |
| 迟到 PCM 污染新回复 | 错播旧内容 | generation fence、request/segment/sequence 四重验证 |
| 依赖增加 | 离线环境失败 | 只引入 `websockets`；固定版本和 wheel hash |
| VoiceStudio 升级改协议 | 主路径中断 | 严格协议检测；自动回 HTTP；版本/协议诊断 |
| AGPL 边界不清 | 分发合规风险 | 只调用本地 API；修改或分发 VoiceStudio 时单独法务复核 |

## 22. 非目标

本轮明确不做：

- 修改 VoiceStudio 模型，让模型真正逐帧生成；
- 在一个 WebSocket 上并发或流水线多个句子；
- 按 token、单字或任意 300 ms 强制播报；
- 在当前句中途切换音色或 Provider；
- 自动修改用户 VoiceProfile；
- 新增第三个 TTS Provider；
- 根据程序状态推导用户已经听到；
- 进入 P8 或扩展电脑任务权限；
- 重写整个 P7 TTS 契约和证据体系。

## 23. 预计工期

以一名熟悉当前代码的开发者、现有测试基线可运行、VoiceStudio 和 Windows 设备可用为前提：

| 里程碑 | 预计时间 |
|---|---:|
| 可用的逐句 HTTP 快速版（Batch 1–2） | 2–3 个开发日 |
| 可 opt-in 的 WS + PCM 推荐版（Batch 3–5） | 再增加 4–6 个开发日 |
| 取消、真实设备、证据和默认切换（Batch 6–8） | 再增加 2–3 个开发日 |
| 完整双路径长期版 | 合计约 8–12 个开发日 |

如果真实 `waveOut` 多 buffer、VoiceStudio 协议或本机驱动出现额外问题，优先保持快速版可用，不以赶工为由跳过 stop、迟到块和重复播放测试。

## 24. Definition of Done

只有以下全部成立，本计划才算实施完成：

> **2026-09-30 逐项对账：14 项全部处理完毕（13 项满足，第 14 项经用户决定"不切换"）。** 用户听感证据已于 2026-09-30 采集：真实播放两句（主路径 VoiceStudio 小饼声线 + 备用声线离线 Edge 槽），用户确认**两句都听到**。完整对账与证据链接见 [`checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md`](checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md)。P7 不因本文改变状态（仍 `Blocked`，未进入 P8）。

- [x] `http_wav` 模式稳定按句生成，不等待约 100 字整段；— [Batch 2](checkpoints/2026-09-30-P7-voicestudio-batch2-http-wav.md)、[Batch 5](checkpoints/2026-09-30-P7-voicestudio-batch5-transport-and-config.md)、[B9 真实采集](checkpoints/2026-09-30-P7-b9-real-p95-measurement.md)（真实 20 句 20/20 播完）
- [x] `ws_pcm` 模式通过 fake 和真实 VoiceStudio 协议测试；— [Batch 3](checkpoints/2026-09-30-P7-voicestudio-batch3-websocket-client.md)（37 项 + 真实三句串行 preflight：连接数 1、`done.samples` 与实收一致）
- [x] `auto` 模式实现 WS→HTTP 的首音前一次性降级；— [Batch 5 真实环境验收](checkpoints/2026-09-30-P7-voicestudio-batch5-transport-and-config.md) 阶段 B
- [x] VoiceStudio 两条路径失败后正确进入 Edge/文字；— 文字：[B10](checkpoints/2026-09-30-P7-batch10-real-transport-faults.md)（真实连接拒绝 → `NOT_COMMITTED` → `text/continue_text_no_replay`）；Edge：[B11](checkpoints/2026-09-30-P7-b11-offline-edge-voice.md)（真实杀后端 → `route=edge`、`edge_attempts=1`，该句由离线声线真的播出）
- [x] PCM worker 在真实 Windows 设备上完成播放、停止和资源回收验证；— [Batch 4](checkpoints/2026-09-30-P7-voicestudio-batch4-pcm-playback.md)、[Batch 7](checkpoints/2026-09-30-P7-voicestudio-batch7-real-scenario-matrix.md)
- [x] barge-in 后旧 generation 音频无法进入新回复；— [Batch 6](checkpoints/2026-09-30-P7-voicestudio-batch6-barge-in-cancellation.md) + [Batch 7](checkpoints/2026-09-30-P7-voicestudio-batch7-real-scenario-matrix.md)（真实播放中取消：11 次 write 后 1 秒仍为 11 次）
- [x] 播放后故障不重播、不半句换声；— [B10](checkpoints/2026-09-30-P7-batch10-real-transport-faults.md) `proxy_drop_mid_sentence`（`PLAYBACK_STARTED` → `replay_current_segment=False`）；[B11](checkpoints/2026-09-30-P7-b11-offline-edge-voice.md) 已出音时 `edge_attempts=0`
- [x] 所有新增配置有范围校验、默认值和回滚说明；— [Batch 5](checkpoints/2026-09-30-P7-voicestudio-batch5-transport-and-config.md)、[Batch 8](checkpoints/2026-09-30-P7-voicestudio-batch8-rollback-and-stage-acceptance.md)、[B11](checkpoints/2026-09-30-P7-b11-offline-edge-voice.md)（`TTS_EDGE_VOICE` 默认空 = 未配置）
- [x] 新增依赖已固定版本、wheel hash、许可证和离线安装方式；— [Batch 8](checkpoints/2026-09-30-P7-voicestudio-batch8-rollback-and-stage-acceptance.md)（`websockets==17.1` + SHA-256 + BSD-3-Clause + 离线安装）；**B11 未新增依赖**（本机 SAPI）
- [x] TTS 专项、集成和全量回归通过；— 2026-09-30 复跑：TTS **550 OK (3 skipped)**、全量 **1065 OK (3 skipped)**、`compileall` exit 0、`git diff --check` exit 0
- [x] 真实测试 artifact、指标和用户听感证据按 batch 独立记录；— artifact 与指标按批独立记录（20+ 个 JSON）；**用户听感证据已于 2026-09-30 采集**：真实播放 [原始 artifact](checkpoints/artifacts/2026-09-30-P7-user-heard/p7-user-heard-20260930T112602Z-f840ea69258a46d2.json)（主路径 1 句完整播放 + 备用声线 32 块 `COMPLETED`），用户确认两句都听到 → [确认件](checkpoints/artifacts/2026-09-30-P7-user-heard/2026-09-30-P7-user-heard-confirmation.json)（`user_heard=true`；未做声学测量，`speaker_acoustically_verified=false`）
- [x] `progress.md`、`task_plan.md`、P7 tracker 和最终 checkpoint 已更新；— `progress.md` 2026-09-30（续）、`task_plan.md` 收口条目与更正、tracker §8/§9/§10/§11、[最终对账 checkpoint](checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md)
- [x] 未修改 VoiceStudio 本体、用户 VoiceProfile、历史 artifact 或任务安全语义；— 本批只改 `app/tts/**`（含新增 `clock.py`/`edge_sapi*`）、`app/config/settings.py`（仅新增 `TTS_EDGE_VOICE`）、测试与文档；用户听感确认另立新 artifact，**未修改**原始播放 artifact
- [x] 用户审阅验收后，才把默认模式从 `http_wav` 切换为 `auto`。— **经用户 2026-09-30 审阅决定：保持 `TTS_VOICESTUDIO_MODE=disabled`（不切换为 `auto`）**。本条的约束"未经用户审阅不得切换"已满足，且"切换"被明确否决；同时已澄清文档原假设（`http_wav`）与仓库实际默认（`disabled`，opt-in）不一致

## 25. 建议的下一执行入口

用户审阅本文后，下一步只授权 **Batch 1：统一契约与 fake adapter 基线**。Batch 1 完成并验证后，再单独进入 Batch 2。不要直接从本文跳到真实 WebSocket、真实扬声器或默认运行时切换。

## 26. 调研链接

- VoiceStudio GitHub：`https://github.com/debpalash/VoiceStudio`
- VoiceStudio v0.5.6：`https://github.com/debpalash/VoiceStudio/releases/tag/v0.5.6`
- v0.5.6 commit：`https://github.com/debpalash/VoiceStudio/commit/3915a62cb482bb43117aaebab57d22745bd1cc21`
- `/ws/tts` 源码固定版本：`https://github.com/debpalash/VoiceStudio/blob/3915a62cb482bb43117aaebab57d22745bd1cc21/backend/api/routers/tts_stream.py`
- websockets 项目：`https://pypi.org/project/websockets/`
- websockets 同步客户端文档：`https://websockets.readthedocs.io/en/stable/reference/sync/client.html`
