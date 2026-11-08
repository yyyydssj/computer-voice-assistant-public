# P7 批次 6 Provider request 取消/失败与 generation 迟到事件门控完成交接

> 交接日期：2026-09-22  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：6  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-21-P7-batch5-provider-request-lifecycle-stop-handoff.md`

## 1. 本批次目标

在 P7 批次 5 的本地 `ProviderRequestLifecycleRegistry` 之上，细化失败/取消的本地原因码、终态优先级与终态锁定，并增加显式 generation fence，拒绝旧代迟到事件、尚未激活的未来代事件和 generation 回退。所有事实仍只发生在本地内存，不调用真实 Provider，不调用远端 `cancel`，不实现重试、fallback、播放或用户听见语义。

## 2. 已完成内容

1. 新增不可变 `ProviderRequestLifecycleReasonCode`，区分失败原因：`local_gate_rejected`、`provider_error`、`provider_timeout`、`internal_error`，以及取消原因：`user_cancelled`、`superseded`、`session_closed`、`shutdown`；
2. `failed` / `cancelled` 事件继续要求非空 `reason`，并要求原因码与事件类型匹配；未提供原因码时分别使用本地默认 `local_gate_rejected` / `user_cancelled`；
3. 扩展生命周期快照 `terminal_event` 与 `terminal_reason_code` 投影，明确首次终态事件是唯一终态事实；终态之后的失败、取消、接受、合成、音频和完成事件全部拒绝；
4. 新增显式 `activate_generation(provider_name, provider_generation)` 与 `advance_generation(...)`；generation 只能保持或前进，不能回退；
5. 生命周期事件写入前检查 generation fence：旧 generation 作为迟到事件拒绝，尚未激活的未来 generation 拒绝；拒绝不会污染快照、事件历史、event ID 集合或 generation fence；
6. 保持事件与 request 的 `reply_id`、`segment_id`、`provider_request_id`、`provider_name`、`provider_generation`、音色版本、音频格式和 capability check 完整绑定；显式 generation fence 需先由上层激活，不由本模块猜测或自动切代；
7. 保持失败原子性、不可变快照/事件、连续 `event_sequence`、全局唯一 `event_id` 和“不等于 Provider 远端停止/播放/用户听见”边界；
8. 新增 4 个专项测试，覆盖原因码和终态投影、原因码类型错配原子拒绝、迟到/未来/回退 generation 拒绝、首个终态锁定及迟到事件拒绝；
9. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1/2/3/4/5 handoff 或更早执行文档；本文件是本批次新增的交接入口。

## 3. 关键不变量

- `FAILED` 与 `CANCELLED` 只记录本地生命周期终态，不调用 Provider 的 `cancel`，不证明远端已经停止；
- 失败原因码只能来自失败集合，取消原因码只能来自取消集合；原因码和文本原因均不可在终态后改写；
- 终态事件具有优先级：同一 request 第一个合法终态事件锁定快照，之后任何事件只拒绝，不覆盖首个终态事实；
- generation fence 是按 `provider_name` 维护的显式本地边界；只有 `activate_generation` / `advance_generation` 能推进它；
- 事件 generation 小于当前 fence 时视为迟到并拒绝，大于当前 fence 时视为尚未激活并拒绝；没有 fence 时仍可按既有 request 绑定规则记录本地事件；
- generation 推进不能回退；generation fence 的拒绝不污染已注册 request、快照、事件历史或全局 event ID 集合；
- request identity、reply/segment identity、provider generation、音色版本、音频格式和 capability check 不得互相替代；
- 本批次没有网络请求、Provider adapter、真实 `synthesize`/`cancel`、音频流、播放设备、播放队列、PCM 时长、重试、fallback、冷却或恢复探测。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
- `app/tts/__init__.py`
- `tests/tts/test_provider_request_lifecycle.py`
- `implementation_plan/checkpoints/2026-09-22-P7-batch6-provider-request-generation-stop-handoff.md`

## 5. 验证证据

- Provider request lifecycle 专项：`11/11`；
- 全量 unittest：`392/392`；
- `compileall app tests`：通过；
- 本批次 Python 文件严格 UTF-8：`3/3`，替换字符 `U+FFFD`：`0`；
- 覆盖原因码与终态投影、原因码/事件类型错配、旧代迟到、未来代未激活、generation 回退、终态后的迟到事件和失败原子性；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- generation fence 当前是按 Provider 名称维护的显式本地内存边界，不负责分配 generation，不验证远端 generation 的真实性，也不自动为 request 激活新代；
- 没有 generation fence 时，request 仍按其自身绑定的 `provider_generation` 校验并可记录本地生命周期事件；调用方若需要拒绝旧代，必须先显式激活当前 generation；
- 失败/取消仍不执行远端停止、不清理 Provider、不重试、不 fallback、不冷却、不恢复探测；
- 当前不接收真实音频块，不定义 PCM/编码 chunk schema，不计算时长，不实现背压、队列容量或播放事实；
- `AUDIO_ENQUEUED` 仍不表示播放开始，未建模播放开始、播放完成、播放中断或用户听见；
- 当前不实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 本阶段仍未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次为 **P7 批次 7：Provider request 音频块契约与顺序/重复/迟到门控**。

下一会话应：

1. 先读取本文件；
2. 只在现有 `ProviderRequestLifecycleRegistry` 之上定义本地不可变音频块元数据/事实契约，继续绑定 request、reply、segment、provider generation、音色版本、音频格式和 capability check；
3. 为音频块定义从 1 开始连续的 `audio_chunk_index`、事件身份和本地接收顺序，拒绝重复、乱序、跨 request、旧 generation、终态后的迟到音频块；
4. 明确“Provider 产生音频块”与“音频进入后续管道”仍是不同事实，不实现播放开始、播放完成或用户听见；
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