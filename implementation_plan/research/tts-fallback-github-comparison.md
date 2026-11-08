# TTS Provider 故障切换：GitHub 源码对比

> 调研日期：2026-09-18  
> 用途：为电脑语音助手的 VoiceStudio → Edge TTS → 文字输出策略提供工程参考；不表示直接采用社区项目的全部默认行为。

## 1. 调研范围与固定版本

| 项目 | 核实版本 | 主要源码 |
|---|---|---|
| LiveKit Agents | `8fc2650deddaa12727a3d7a7e544d1e3a95c1315` | [`fallback_adapter.py`](https://github.com/livekit/agents/blob/8fc2650deddaa12727a3d7a7e544d1e3a95c1315/livekit-agents/livekit/agents/tts/fallback_adapter.py) |
| Pipecat | `3dede06bec0b497bddfdcf047af7495ee9d0726e` | [`service_switcher.py`](https://github.com/pipecat-ai/pipecat/blob/3dede06bec0b497bddfdcf047af7495ee9d0726e/src/pipecat/pipeline/service_switcher.py) |

## 2. 对比结论

| 项目 | Provider 切换粒度 | 已输出部分如何处理 | 重试与恢复 | 对本项目的启示 |
|---|---|---|---|---|
| LiveKit Agents | 一个 TTS 请求或流片段内按配置顺序尝试 Provider；不可用实例会被跳过 | 如果失败前还没有推出音频，可继续尝试下一 Provider；一旦已推出任意音频，当前请求/片段不再 fallback，避免同一片段混合两个声音 | 每个 Provider 可配置请求内重试，默认值为 2；失败后标记不可用、发出 `tts_availability_changed`，并用输入文本做后台恢复探测；探测成功后重新标记可用，后续请求会再次优先使用排序靠前的 Provider | 采用“已有音频输出就不在当前片段换声”、显式可用性事件和后台恢复探测；不照搬默认自动重试，也不使用真实回复全文做无提示恢复测试 |
| Pipecat | `ServiceSwitcher` 在服务实例层维护唯一 active service；failover 策略在当前服务变为 `is_usable=False` 后切到列表中的下一可用服务 | 切换器负责改变后续帧路由；源码中没有缓存并重放导致错误的当前工作，因此不能假定失败中的 TTS 句子会自动由新服务重做 | 短暂但仍可用的错误不会触发切换；恢复、冷却和何时切回由应用通过 `on_service_switched` 等机制自行实现；服务恢复后需重新 `set_usable`，也支持显式手动切换 | 采用“瞬时错误与不可用分开”、恢复策略留在应用层和用户可见状态；本项目还要额外固定完整句边界，并禁止未经用户配置的音色自动加入候选 |

## 3. LiveKit Agents 源码要点

1. `FallbackAdapter` 接收有顺序的 TTS 列表，失败时依次尝试；各实例有独立 `available` 状态。
2. 非流式和流式路径都检查 `output_emitter.pushed_duration()`：只要已经输出过音频，就把当前 Provider 视为“部分服务过该请求”，停止该请求的 fallback。
3. Provider 状态变化通过 `tts_availability_changed` 事件向上层报告。
4. 恢复任务在后台重新合成失败请求使用的文本，成功后将 Provider 设回可用；这是通用实时媒体框架的选择，但本项目不应拿包含隐私或授权内容的真实句子反复做探测。
5. Adapter 的 `max_retry_per_tts` 默认是 2。电脑语音助手已经确认“不循环重试”；后续实现应明确设为 0 或由本地有限状态机控制，不能继承框架默认值。

## 4. Pipecat 源码要点

1. `ServiceSwitcher` 通过并行分支和过滤器只把普通帧路由给当前 active service。
2. `ServiceSwitcherStrategyFailover` 只有在错误使当前服务 `is_usable=False` 时才切换；仍可继续工作的瞬时错误不会触发 failover。
3. 切换顺序是当前服务之后的下一可用服务，并支持绕回列表开头。
4. 失败服务仍保留在列表中；应用修复后调用 `set_usable`，再由应用决定是否以及何时切回。官方源码注释明确把 recovery/fallback policy 留给应用层。
5. 切换器没有保存 TTS 句子并在切换后自动重放的机制。对本项目而言，句子状态、是否已经实际播放以及是否允许重做，必须由 TTS 调度层单独管理。

## 5. 本项目最终采用与不采用

### 采用

- Provider 使用显式可用状态与状态变化事件；
- 已经实际输出音频后，不在当前句中切换 Provider；
- 将短暂错误、Provider 不可用和恢复待启用分成不同状态；
- 恢复与切回由本地应用状态机控制，不由 Provider Adapter 擅自决定；
- Provider 恢复只影响后续新回复，不重放旧内容。

### 不采用

- 不采用每个 Provider 默认自动多次重试；
- 不采用任意 Provider 列表中“有谁可用就用谁”，Edge 音色必须由用户预先试听并指定；
- 不使用用户真实回复、授权提示或隐私文本作为后台恢复探测材料；
- 不在半句话中切换声音，不把失败句自动整句重播；
- 不在 VoiceStudio 恢复后立即抢回当前播报，只从下一轮新回复恢复。

## 6. 对实现与测试的直接要求

- 每个句子必须有 `queued`、`synthesizing`、`ready`、`playing`、`played`、`failed_before_play`、`failed_after_partial_play`、`discarded` 等可判定状态。
- fallback 判断必须依据播放器确认的实际输出时长，而不是只依据“合成请求是否返回”。
- Provider 状态事件必须与任务授权状态完全隔离。
- 测试至少覆盖：首字节前失败、已有音频后失败、Edge 再失败、恢复发生在当前轮中途、旧授权提示位于待播队列、用户打断与 Provider 故障同时发生。
- 后续模型加载与超时专题再确定健康检查间隔、连续成功次数、冷却和手动重新检测入口。
