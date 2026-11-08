# P7 批次 8 Provider request 音频块完成/终态一致性与多块生命周期投影门控交接

> 交接日期：2026-09-22  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：8  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-22-P7-batch7-provider-request-audio-chunk-stop-handoff.md`

## 1. 本批次目标

在 P7 批次 7 的不可变 Provider 音频块元数据和连续顺序门控之上，定义多块音频事实与 `audio_produced` / `audio_enqueued` / `completed` 的最小一致性边界。继续只在本地内存中工作，不携带 PCM/编码 payload，不接入真实 Provider、音频管道或播放设备。

本批次选择并实现以下明确边界：

- 不允许零块 `audio_produced`；必须先有至少一个连续且完整绑定的音频块；
- 不允许零块 `completed`；完成前必须已经形成非空音频块集合并报告 `audio_produced`；
- `audio_enqueued` 仍要求先有 `audio_produced`，但不被强制为 `completed` 的额外前置条件，以保持它是独立的后续管道本地事实；
- 一旦报告 `audio_produced`，音频块集合冻结；之后追加块会被拒绝，避免生命周期投影与块集合发生歧义；
- 终态锁定后仍拒绝任何迟到音频块，不覆盖首个终态事实；
- 所有拒绝继续原子化，不污染事件历史、块快照或全局 event ID 集合。

## 2. 已完成内容

1. `ProviderRequestLifecycle` 增加一致性不变量：`audio_produced=True` 时必须存在至少一个音频块；
2. `audio_produced(...)` 只允许在非空、连续、完整绑定的音频块集合已经建立后报告；
3. `complete(...)` 继续要求 `audio_produced`，并显式拒绝零块完成；
4. `record_audio_chunk(...)` 在 `audio_produced` 后拒绝新增块，使已投影的块集合保持稳定；
5. 保持 `audio_enqueued` 与 `audio_produced` 的独立事实边界，不把入队伪装成播放开始、播放完成或用户已听见；
6. 新增专项测试，覆盖零块 `audio_produced`、零块 `completed`、`audio_produced` 后块集合冻结和成功的多块投影；
7. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1/2/3/4/5/6/7 handoff 或更早执行文档。

## 3. 关键不变量

- 音频块仍只表示 Provider 产生的本地块元数据事实，不表示音频已进入后续管道、播放开始、播放完成或用户已听见；
- `audio_chunk_index` 和 `receive_sequence` 仍从 1 开始，在单一 request 内连续递增；
- `audio_produced` 是生命周期级投影事实，但必须建立在至少一个合法音频块之上；
- `audio_enqueued` 仍必须晚于 `audio_produced`，但不新增播放语义；
- `completed` 必须建立在非空音频块集合和 `audio_produced` 之上；
- `audio_produced` 后块集合冻结，终态后任何迟到块拒绝；
- request/reply/segment/provider/generation/voice/audio/capability identity 继续完整绑定；
- 所有失败均原子化，不污染块历史、生命周期事件、快照或全局 event ID 集合；
- 本批次没有网络请求、Provider adapter、真实 `synthesize`/`cancel`、播放设备、音频流、播放队列、重试、fallback、冷却或恢复探测。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
- `tests/tts/test_provider_request_lifecycle.py`
- `implementation_plan/checkpoints/2026-09-22-P7-batch8-provider-request-audio-chunk-completion-handoff.md`

## 5. 验证证据

- Provider request lifecycle 专项：`18/18`，通过；
- P7 专项：`54/54`，通过（本批次前 `43/43`，本批次新增 11 个 P7 生命周期断言/测试路径）；
- 全量 unittest：`399/399`，通过；
- `compileall app tests`：通过；
- 修改文件严格 UTF-8：`4/4`，替换字符 `U+FFFD`：`0`；
- 执行面审计：未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 音频块仍是元数据事实，不接收真实音频 payload，不定义 PCM/编码 chunk schema；
- 不计算音频时长，不实现背压、队列容量、音频管道进入、播放开始、播放完成或用户听见；
- 不执行远端停止，不清理 Provider，不重试，不 fallback，不冷却，不恢复探测；
- 当前仍不实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- P7 尚未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次建议为 **P7 批次 9：Provider request 失败/取消终态与已投影音频块集合的一致性审计**，重点审计失败/取消发生在零块、部分块、已 `audio_produced` 和已 `audio_enqueued` 各阶段时的最小本地事实边界。

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry` 与 `ProviderRequestAudioChunk` 之上工作；
3. 不扩大到真实 Provider、网络、播放或 P8；
4. 明确失败/取消是否允许保留部分块，以及终态投影是否需要额外一致性门控；
5. 完成专项测试、P7 专项、全量回归、compileall、严格 UTF-8 和执行面审计；
6. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio、Edge TTS、真实 Provider、网络或真实音频流；
- 调用 `synthesize`、`cancel`、播放设备、sounddevice、扬声器或真实音频输出；
- 把本地音频块元数据伪装成远端停止、播放完成或用户已听到；
- 实现播放事实、PCM 时长预算、播放队列、Provider fallback、冷却、恢复探测或重试；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。
