# P7 批次 4 Provider request 契约完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：4  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/2026-09-21-P7-batch3-provider-capability-stop-handoff.md`

## 1. 本批次目标

完成 TTS 输出链路的第四批领域基础：在已注册 `ProviderCapability`、已选择且已启用的 `VoiceProfile` 和一个完整句段文本之上，准备一个本地不可变的 Provider request 契约。

本批次不连接 VoiceStudio、Edge TTS、真实 Provider、播放设备、网络、ASR、UI 或真实音频；不调用 `synthesize`、`cancel`、`play`、`stop`，不接收音频流，不实现取消执行、重试、fallback、播放事实、PCM 时长预算、播放队列或 P8 组合闭环。

## 2. 已完成内容

1. 新增 `app/tts/requests/` 纯内存 Provider request 模块，并从 `app.tts` 公开导出；
2. 新增不可变 `ProviderRequestState`，当前只允许本地准备态 `PREPARED`；
3. 新增不可变 `ProviderRequest`，显式绑定 `reply_id`、`segment_id`、`provider_request_id`、`request_sequence`、`provider_name`、`provider_generation`、音色 profile ID/版本、文本、音频格式、能力需求和兼容性判定；
4. 新增 `ProviderRequestRegistry`，只允许基于显式注册 capability 和当前用户已选择且已启用的 VoiceProfile 会话准备 request；
5. VoiceProfile 必须是当前选择的 profile、已启用且会话版本仍是当前最新选择版本；未选择、未启用或旧版本会话 fail-closed；
6. Provider capability 必须来自已注册 Provider；未知 Provider、能力不兼容、未声明文本/音频流、未声明取消或未声明音色切换均 fail-closed；
7. request 文本必须是非空、非纯空白且不超过 4096 字符；音频格式不能为空且必须是 `AudioFormat`；provider generation、request sequence、音色版本均必须为正整数；
8. 每个 reply 的 request sequence 必须从 1 开始连续递增；同一 reply/segment 不得重复准备；`segment_id` 不得跨 reply 复用；
9. `provider_request_id` 必须全局唯一，不得重复或跨 reply/segment 复用；失败准备不得污染注册表状态；
10. `ProviderRequest` 与其绑定的 capability request/check 均不可变，并明确区分 request identity、reply/segment identity、provider generation 与未来的播放事实；
11. 新增 8 个专项测试，覆盖成功绑定、未选择/未启用音色、未知 Provider、能力不兼容、空文本/空格式/非法序列、重复和跨 reply/segment 身份、旧 VoiceProfile 会话版本、不可变性及直接构造一致性校验；
12. 未修改路线图、`task_plan.md`、`progress.md`、P7 批次 1/2/3 handoff 或更早执行文档；本文件是本批次新增的交接入口。

## 3. 关键不变量

- Provider request 只能由本地已注册 capability 判定和当前用户已选择、已启用的 VoiceProfile 版本准备；未知 Provider 不得被当作支持；
- `ProviderRequestState.PREPARED` 仅表示本地准备完成，不表示 Provider 已接纳、已创建远端任务、已开始合成、已返回音频或用户已听到；
- `reply_id`、`segment_id`、`provider_request_id`、`provider_generation`、`voice_profile_version` 和 `request_sequence` 是不同事实，不得互相替代；
- capability check 必须是 `COMPATIBLE`，且 request 中的音频格式必须与 capability request 一致；
- request 文本不得为空；音频格式不得为空；request sequence 必须按 reply 从 1 连续递增；
- 同一 reply/segment 只允许一个本地 request；segment 不得跨 reply 复用；provider request identity 不得重复或跨 reply/segment 复用；
- 所有拒绝必须在写入注册表前发生，失败准备不得改变既有 request、sequence 或绑定索引；
- 本批次没有文件读写运行时、网络请求、进程/命令、线程/定时器、真实音频设备、Provider 调用或播放事实。

## 4. 修改文件

- `app/tts/__init__.py`
- `app/tts/requests/__init__.py`
- `tests/tts/test_provider_requests.py`
- `implementation_plan/checkpoints/2026-09-21-P7-batch4-provider-request-stop-handoff.md`

## 5. 验证证据

- Provider request 专项：`8/8`；
- P7 TTS VoiceProfile + segmentation + capability + request 专项：`36/36`；
- 全量 unittest：`381/381`；
- `compileall app tests`：通过；
- 本批次新增/修改 Python 文件严格 UTF-8：`3/3`，替换字符 `U+FFFD`：`0`；
- Provider request 模块执行面 AST 审计：`0` 个网络、进程、命令、线程、异步、synthesize/cancel/play/stop 或真实音频设备入口；
- 覆盖拒绝路径：未选择音色、未启用音色、旧音色版本会话、未知 Provider、能力不兼容、空文本、空格式、非法序列、重复 request identity、同一 reply/segment 重复、segment 跨 reply、provider_request_id 跨 reply/segment 复用、错误绑定 capability check/format；
- 覆盖成功路径：当前已选择且启用的 VoiceProfile 版本 + 已注册兼容 capability + 完整文本 + 新 request identity 的本地 PREPARED 准备；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 当前只有 Provider request 的本地准备契约，不包含具体 Provider adapter 实现；
- 当前不调用 synthesize/cancel，不接收 Provider 音频流，也不记录真实 Provider 返回或远端任务状态；
- `provider_generation` 当前是调用方提供并绑定的本地代际事实，不实现 generation 分配、切换、迟到事件门控或恢复策略；
- 当前音频格式采用已有固定本地白名单，未实现编码器协商、转码、PCM 时长计算或音频块 schema；
- 当前 VoiceProfile 会话只校验当前选择的最新版本，不实现会话持久化、跨进程恢复或 Provider 侧 profile 解析；
- 当前不实现取消执行、Provider fallback、冷却、恢复探测、重试或“已有音频后不得切换”策略；
- 当前不实现播放事实、播放队列、背压、barge-in、AEC、连续监听或 P8 语音闭环；
- 本阶段仍未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次为 **P7 批次 5：Provider request 生命周期门控**。

下一会话应：

1. 先读取本文件；
2. 只在现有 `ProviderRequest` 本地准备契约之上，定义不调用真实 Provider 的本地生命周期门控，明确 prepared/accepted/synthesizing/completed/failed/cancelled 等事实边界；
3. 严格区分本地 request 已准备、Provider 已接纳、Provider 已产生音频、音频已进入后续管道与用户已听到；不得把本地状态伪装成远端事实；
4. 继续绑定 `reply_id`、`segment_id`、`provider_request_id`、`provider_generation`、音色版本和 capability check；拒绝未知 request、跨 reply/segment 事件、重复终态、旧 generation 迟到事件和非法状态跳转；
5. 生命周期门控只能是本地不可变事件/状态契约，不调用网络、synthesize、cancel、真实 Provider、音频设备或播放队列；
6. 完成专项测试、P7 专项、全量回归、compileall、严格 UTF-8 和执行面审计；
7. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio 或 Edge TTS 网络/真实 Provider；
- 调用 `synthesize`、`cancel`、建立网络连接、接收音频块或实现重试；
- 把 Provider request 准备态、生命周期事件或 capability 判定伪装成 Provider 已接纳、合成完成或用户已听到；
- 接入播放设备、sounddevice、扬声器或真实音频输出；
- 实现播放事实、PCM 时长预算、播放队列、Provider fallback、冷却或恢复探测；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。