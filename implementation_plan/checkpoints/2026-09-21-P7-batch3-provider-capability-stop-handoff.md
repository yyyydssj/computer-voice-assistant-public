# P7 批次 3 Provider capability 契约完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：3  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-21-P7-batch2-segmentation-stop-handoff.md`

## 1. 本批次目标

完成 TTS 输出链路的第三批领域基础：把 Provider adapter 的能力声明建模为显式、不可变、可严格校验的本地事实，并在真正发起 Provider 请求前提供兼容性判定。

本批次不连接 VoiceStudio、Edge TTS、真实 Provider、播放设备、网络、ASR、UI 或真实音频；不创建 `provider_request_id`，不调用 `synthesize`，不接收音频流，不实现取消执行、重试、fallback、播放事实、PCM 时长预算或 P8 组合闭环。

## 2. 已完成内容

1. 新增 `app/tts/capabilities/` 纯内存 Provider capability 模块，并从 `app.tts` 公开导出；
2. 新增不可变 `AudioFormat`，显式描述格式名、采样率和声道数；
3. 新增不可变 `ProviderCapability`，覆盖文本流、音频流、取消、音色切换、音频格式和首音频估计延迟；
4. 新增不可变 `ProviderCapabilityRequest`，只描述调用前的能力需求，不是 Provider 请求且不产生请求 ID；
5. 新增不可变 `CapabilityCheck` 与 `CapabilityDecision`，明确区分兼容和不兼容，不把判定结果解释为 Provider 已接纳、已合成或已播放；
6. 新增 `ProviderCapabilityRegistry`，只允许显式注册 Provider capability；未知 Provider fail-closed，不默认假定支持；
7. 音频格式只允许固定白名单（`pcm_s16le`、`pcm_f32le`、`opus`、`mp3`），采样率、声道数和估计延迟均有正值、上限和一致性校验；
8. 支持音频流时必须声明至少一种格式；不支持音频流时不得声明格式；重复格式拒绝；
9. 兼容性判定覆盖文本流、音频流、取消、音色切换、请求格式，以及“指定格式必须同时请求音频流”；
10. 未声明取消或音色切换时，尝试请求对应能力会 fail-closed；未声明请求格式时，格式兼容性判定拒绝；
11. 新增 9 个专项测试，覆盖空格式、重复格式、非法采样参数、负数/无界延迟、未知 Provider、重复注册、兼容请求、未声明取消/音色切换、格式与音频流不一致及不可变性；
12. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1 handoff、P7 批次 2 handoff 或更早执行文档；本文件是本批次唯一新增的交接入口。

## 3. 关键不变量

- Provider capability 必须来自 adapter 的显式声明；未知 Provider 不得被当作支持；
- `AudioFormat` 的格式名、采样率和声道数经过严格校验，不接受空值、负数、零值、超限值或未知格式；
- 一个 `ProviderCapability` 不得声明重复音频格式；支持音频流与格式声明必须一致；
- 估计首音频延迟必须是有限、非负且有上限的本地估计值，不得使用负数、无界值或隐式默认的无限值；
- `ProviderCapabilityRequest` 只表达能力需求，不代表已创建 Provider request，不产生 `provider_request_id`、`provider_generation`、音频 chunk 或播放事实；
- `CapabilityCheck.COMPATIBLE` 不含拒绝原因，`INCOMPATIBLE` 必须包含至少一个拒绝原因；
- 未声明文本流、音频流、取消、音色切换或指定格式时，兼容性判定必须拒绝，不得猜测 Provider 行为；
- 本批次没有文件读写运行时、网络请求、进程/命令、线程/定时器、真实音频设备、Provider 调用或播放事实。

## 4. 修改文件

- `app/tts/__init__.py`
- `app/tts/capabilities/__init__.py`
- `tests/tts/test_capabilities.py`
- `implementation_plan/checkpoints/2026-09-21-P7-batch3-provider-capability-stop-handoff.md`

## 5. 验证证据

- Provider capability 专项：`9/9`；
- P7 TTS VoiceProfile + segmentation + capability 专项：`28/28`；
- 全量 unittest：`373/373`；
- `compileall app tests`：通过；
- 本批次新增/修改 Python 文件严格 UTF-8：`3/3`，替换字符 `U+FFFD`：`0`；
- capability 模块执行面 AST 审计：`0` 个网络、进程、命令、线程、异步或真实音频设备入口；
- 覆盖拒绝路径：未知 Provider、重复注册、空/未知/重复格式、非法采样率、非法声道数、负数/无界/超限延迟、声明与请求不兼容、未声明取消、未声明音色切换、格式未声明或未请求音频流；
- 覆盖兼容路径：文本流 + 音频流 + 取消 + 已声明格式的本地兼容性判定；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 当前只有 Provider capability 声明、注册和本地兼容性判定，不包含具体 Provider adapter 实现；
- 当前不产生 `provider_request_id`，不执行 synthesize，不接收 Provider 音频流，也不记录 provider generation；
- 当前音频格式采用固定本地白名单，未实现编码器协商、转码、PCM 时长计算或音频块 schema；
- 当前估计延迟只是声明字段，不代表真实网络测量、首包到达或用户听到时间；
- 当前取消能力只是兼容性事实，不执行 cancel，不停止 Provider，不清理播放队列；
- 当前不实现 Provider fallback、冷却、恢复探测、重试或“已有音频后不得切换”策略；
- 当前不实现播放事实、播放队列、背压、barge-in、AEC、连续监听或 P8 语音闭环；
- 本阶段仍未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次为 **P7 批次 4：Provider request 契约**。

下一会话应：

1. 先读取本文件；
2. 只实现基于已注册 `ProviderCapability` 的 Provider request 领域契约，绑定 `reply_id`、`segment_id`、选定音色版本、能力兼容结果和新请求身份；
3. 严格拒绝未知 Provider、未选择/未启用音色、能力不兼容、空文本、空格式、非法请求序列、重复 request 身份和跨 reply/segment 复用；
4. 明确区分 `reply_id`、`segment_id`、`provider_request_id`、`provider_generation` 与后续播放事实；
5. Provider request 只能是本地不可变准备状态，不调用网络、synthesize、cancel、真实 Provider、音频设备或播放队列；
6. 完成专项测试、P7 专项、全量回归、compileall、严格 UTF-8 和执行面审计；
7. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio 或 Edge TTS 网络/真实 Provider；
- 调用 `synthesize`、`cancel`、建立网络连接、接收音频块或实现重试；
- 把 capability 判定伪装成 Provider 已接纳、合成完成或用户已听到；
- 接入播放设备、sounddevice、扬声器或真实音频输出；
- 实现播放事实、PCM 时长预算、播放队列、Provider fallback、冷却或恢复探测；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。