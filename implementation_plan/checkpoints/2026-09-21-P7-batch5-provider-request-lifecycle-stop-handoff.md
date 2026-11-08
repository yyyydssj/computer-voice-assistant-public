# P7 批次 5 Provider request 生命周期门控完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：5  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-21-P7-batch4-provider-request-stop-handoff.md`

## 1. 本批次目标

在 P7 批次 4 的本地不可变 `ProviderRequest` 准备契约之上，建立不调用真实 Provider 的生命周期门控。生命周期只记录上层显式报告的本地事件，并严格区分：request 已准备、Provider 已接纳、合成已开始、Provider 已产生音频、音频已进入后续管道、合成生命周期已完成、失败和取消。任何一个状态都不表示用户已经听到。

本批次不连接 VoiceStudio、Edge TTS、真实 Provider、网络、播放设备、ASR、UI 或 P8；不调用 `synthesize`、`cancel`、`play`、`stop`，不接收真实音频流，不实现重试、fallback、PCM 时长预算、播放队列、播放事实或用户听见事实。

## 2. 已完成内容

1. 新增 `app/tts/lifecycle/` 纯内存生命周期模块，并从 `app.tts` 公开导出；
2. 新增不可变 `ProviderRequestLifecycleState`：`PREPARED`、`ACCEPTED`、`SYNTHESIZING`、`COMPLETED`、`FAILED`、`CANCELLED`；
3. 新增不可变 `ProviderRequestLifecycleEventKind`：`ACCEPTED`、`SYNTHESIS_STARTED`、`AUDIO_PRODUCED`、`AUDIO_ENQUEUED`、`COMPLETED`、`FAILED`、`CANCELLED`；
4. 新增不可变 `ProviderRequestLifecycleEvent`，每个事件显式绑定 `provider_request_id`、`reply_id`、`segment_id`、`provider_name`、`provider_generation`、音色 profile ID/版本、音频格式和兼容 capability check；
5. 新增不可变 `ProviderRequestLifecycle` 快照，明确区分当前生命周期状态、是否产生音频、是否进入后续管道，且不暴露播放或用户听见字段；
6. 新增 `ProviderRequestLifecycleRegistry`，只接受已注册的 `ProviderRequest`，未知 request、跨 reply/segment 事件、旧 generation、旧音色版本、错误格式或错误 capability check 均 fail-closed；
7. 固定合法跳转：`prepared -> accepted -> synthesizing -> completed`，音频产生与进入后续管道是 synthesizing 阶段的独立事实；`failed/cancelled` 可在 prepared、accepted 或 synthesizing 阶段进入终态；终态不可再次接收事件；
8. `audio_produced` 必须先于 `audio_enqueued`，`completed` 必须在 `audio_produced` 后发生；重复音频事实、重复终态和非法状态跳转均拒绝；
9. 所有事件要求从 1 开始连续递增的 `event_sequence` 和全局唯一 `event_id`；拒绝事件不会污染快照、事件历史或事件 ID 集合；
10. 失败/取消事件必须有非空原因，其他事件不得携带原因；成功状态与事件对象均不可变；
11. 新增 7 个专项测试，覆盖 prepared-only、完整生命周期、非法跳转和重复终态、提前失败/取消、未知 request、跨身份事件、旧 generation、错误 capability/格式、不可变性及“不等于播放/用户听见”边界；
12. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1/2/3/4 handoff 或更早执行文档；本文件是本批次新增的交接入口。

## 3. 关键不变量

- 生命周期注册表只接受已存在的本地 `ProviderRequest`；未知 request 不得创建生命周期事实；
- `PREPARED` 只表示本地 request 已准备；`ACCEPTED` 只表示上层报告 Provider 已接纳，不由本模块调用或验证 Provider；
- `SYNTHESIZING` 只表示上层报告合成已开始；`AUDIO_PRODUCED` 只表示上层报告已产生音频；`AUDIO_ENQUEUED` 只表示音频进入后续管道；这些都不表示播放开始或用户已听到；
- `COMPLETED` 必须在 `AUDIO_PRODUCED` 之后；没有音频产生事实不得伪装为完成；
- `FAILED` 和 `CANCELLED` 是本地终态门控事实，不调用 Provider 的 cancel，不代表远端已停止；
- 每个事件必须绑定同一 `provider_request_id` 的 reply、segment、provider generation、音色版本、音频格式和 capability check；迟到 generation 或跨身份事件拒绝；
- 所有拒绝在写入快照、事件历史和 event ID 集合前发生，失败操作原子且不可观察；
- 本批次没有文件读写运行时、网络请求、进程/命令、线程/定时器、真实音频设备、Provider 调用、播放事实或用户听见事实。

## 4. 修改文件

- `app/tts/__init__.py`
- `app/tts/lifecycle/__init__.py`
- `tests/tts/test_provider_request_lifecycle.py`
- `implementation_plan/checkpoints/2026-09-21-P7-batch5-provider-request-lifecycle-stop-handoff.md`

## 5. 验证证据

- Provider request lifecycle 专项：`7/7`；
- P7 TTS VoiceProfile + segmentation + capability + request + lifecycle 专项：`43/43`；
- 全量 unittest：`388/388`；
- `compileall app tests`：通过；
- 本批次新增/修改 Python 文件严格 UTF-8：`3/3`，替换字符 `U+FFFD`：`0`；
- 生命周期模块执行面 AST 审计：仅包含 dataclass、Enum、字典/元组内存状态和本地校验；`0` 个网络、进程、命令、线程、异步、synthesize/cancel/play/stop 或真实音频设备入口；
- 覆盖拒绝路径：未知 request、跨 reply/segment、旧 provider generation、旧音色版本、错误 capability check、错误音频格式、非法状态跳转、提前 audio_enqueued、提前 completed、重复音频事实、重复终态、重复 event_id、错误 event_sequence、失败/取消缺少原因、非终态携带原因；
- 覆盖成功路径：prepared-only、accepted、synthesizing、audio_produced、audio_enqueued、completed，以及 prepared/accepted/synthesizing 阶段的 failed/cancelled；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 当前只有 Provider request 的本地生命周期事件和状态门控，不包含具体 Provider adapter、远端确认或事件来源真实性校验；
- `ACCEPTED`、`SYNTHESIS_STARTED`、`AUDIO_PRODUCED`、`AUDIO_ENQUEUED` 都是上层显式报告的本地事实，不是本模块自行观测的 Provider 或设备事实；
- 当前不接收真实音频块，不定义 PCM/编码音频 chunk schema，不计算时长，不实现背压或队列容量；
- 当前 `FAILED/CANCELLED` 不执行远端 cancel、不清理 Provider、不做重试、fallback、冷却、恢复探测或 generation 分配；
- 当前 `AUDIO_ENQUEUED` 不表示播放开始，未建模播放开始、播放完成、播放中断或用户听见；
- 当前不实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 本阶段仍未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次为 **P7 批次 6：Provider request 取消/失败与 generation 迟到事件门控**。

下一会话应：

1. 先读取本文件；
2. 只在现有 `ProviderRequestLifecycleRegistry` 之上，细化取消/失败原因、终态优先级、迟到事件拒绝和 generation 代际门控；
3. 明确区分本地记录取消/失败与 Provider 远端实际停止，不调用真实 `cancel`，不执行重试或 fallback；
4. 保持 `reply_id`、`segment_id`、`provider_request_id`、`provider_generation`、音色版本、音频格式和 capability check 的绑定；
5. 继续保持不可变事件/状态契约和失败原子性；
6. 完成专项测试、P7 专项、全量回归、compileall、严格 UTF-8 和执行面审计；
7. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio 或 Edge TTS 网络/真实 Provider；
- 调用 `synthesize`、`cancel`、建立网络连接、接收真实音频块或实现重试；
- 把本地 lifecycle 事件伪装成远端接纳、合成完成、播放完成或用户已听到；
- 接入播放设备、sounddevice、扬声器或真实音频输出；
- 实现播放事实、PCM 时长预算、播放队列、Provider fallback、冷却或恢复探测；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。
