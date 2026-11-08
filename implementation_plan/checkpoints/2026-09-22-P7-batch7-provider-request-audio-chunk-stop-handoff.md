# P7 批次 7 Provider request 音频块契约与顺序/重复/迟到门控完成交接

> 交接日期：2026-09-22  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：7  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-22-P7-batch6-provider-request-generation-stop-handoff.md`

## 1. 本批次目标

在 P7 批次 6 的 `ProviderRequestLifecycleRegistry` 之上，增加只发生在本地内存中的不可变 Provider 音频块元数据契约。音频块继续完整绑定 request、reply、segment、Provider、generation、音色版本、音频格式和 capability check，并由注册表门控连续块序号、本地接收顺序、重复、乱序、跨 request、旧 generation、未来 generation 和终态后的迟到块。

本批次不携带 PCM/编码 payload，不进入真实音频管道，不实现播放开始、播放完成或用户听见事实。

## 2. 已完成内容

1. 新增不可变 `ProviderRequestAudioChunk`，字段包括 `event_id`、从 1 开始的 `audio_chunk_index`、从 1 开始的本地 `receive_sequence`，以及完整 request identity 与 capability/audio metadata 绑定；
2. `ProviderRequestLifecycle` 新增不可变 `audio_chunks` 快照，校验块序号和接收顺序连续，并拒绝跨 request 或 metadata 不一致的块；
3. `ProviderRequestLifecycleRegistry.record_audio_chunk(...)` 只允许 `synthesizing` request 接收块，先完成 request identity、generation fence、终态、块序号、接收顺序和 event ID 校验，再原子写入快照；
4. 明确块序号与本地接收顺序都不得重复、不得跳号、不得乱序；失败不会污染 request 快照、块历史或全局 event ID 集合；
5. 旧 generation、尚未激活的未来 generation 和终态后的迟到块全部拒绝；
6. `audio_chunks_for(...)` 提供只读 tuple 视图；音频块事实与 `audio_produced`、`audio_enqueued`、播放或用户听见保持分离；
7. `app/tts/__init__.py` 导出 `ProviderRequestAudioChunk`；
8. 新增专项测试，覆盖不可变绑定元数据、连续顺序、重复/跳号/跨 request 原子拒绝、generation 迟到/未来门控、终态迟到块和 metadata 错配；
9. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1/2/3/4/5/6 handoff 或更早执行文档。

## 3. 关键不变量

- 音频块只表示 Provider 产生的本地块元数据事实，不表示音频已进入后续管道、播放开始、播放完成或用户已听见；
- 音频块不携带 PCM/编码 payload，不计算时长，不实现背压、队列容量或播放事实；
- `audio_chunk_index` 和 `receive_sequence` 均从 1 开始，并在单一 request 内连续递增；重复、跳号、乱序和跨 request 块拒绝；
- 块的 request/reply/segment/provider/generation/voice/audio/capability identity 不得互相替代；
- generation fence 仍按 Provider 名称显式维护，旧代和未来代块拒绝，未设置 fence 时按既有 request 绑定规则记录；
- 终态锁定后，任何迟到音频块拒绝，不覆盖首个终态事实；
- 所有失败均原子化，不污染块历史、生命周期事件、快照或全局 event ID 集合；
- 本批次没有网络请求、Provider adapter、真实 `synthesize`/`cancel`、播放设备、音频流、播放队列、重试、fallback、冷却或恢复探测。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
- `app/tts/__init__.py`
- `tests/tts/test_provider_request_lifecycle.py`
- `implementation_plan/checkpoints/2026-09-22-P7-batch7-provider-request-audio-chunk-stop-handoff.md`

## 5. 验证证据

- Provider request lifecycle 专项：`15/15`，通过；
- 全量 unittest：`396/396`，通过；
- `compileall app tests`：通过；
- 修改文件严格 UTF-8：`4/4`，替换字符 `U+FFFD`：`0`；
- 专项覆盖不可变音频块元数据、连续块序号、本地接收顺序、重复/跳号/跨 request 原子拒绝、旧代迟到、未来代未激活、终态后迟到块和 metadata 错配；
- 中途验证曾发现并修正两个实现问题：音频块 generation fence 必须先于 request identity 绑定检查；生命周期事件重建快照时必须保留已有 `audio_chunks`；修正后专项与全量均通过；
- 执行面审计：未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 当前音频块是元数据事实，不接收真实音频 payload，不定义 PCM/编码 chunk schema；
- 当前不计算音频时长，不实现背压、队列容量、音频管道进入、播放开始、播放完成或用户听见；
- `audio_produced` 仍是生命周期级本地事实，与一个或多个音频块的存在保持显式分离；
- 失败/取消仍不执行远端停止、不清理 Provider、不重试、不 fallback、不冷却、不恢复探测；
- 当前不实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- P7 尚未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次建议为 **P7 批次 8：Provider request 音频块完成/终态一致性与多块生命周期投影门控**。

下一会话应：

1. 先读取本文件；
2. 继续只在现有本地 `ProviderRequestLifecycleRegistry` 与 `ProviderRequestAudioChunk` 之上工作；
3. 定义多块音频事实与生命周期 `audio_produced` / `audio_enqueued` / `completed` 的最小一致性边界，明确是否允许零块完成、完成前块集合要求及终态后的迟到块拒绝；
4. 保持所有 request identity、reply/segment identity、generation、音色版本、音频格式和 capability check 绑定；
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