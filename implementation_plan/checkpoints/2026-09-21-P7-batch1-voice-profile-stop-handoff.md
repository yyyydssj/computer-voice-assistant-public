# P7 批次 1 VoiceProfile 完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P7  
> 批次：1  
> 阶段状态：`in_progress`  
> 批次状态：`complete`  
> 上一交接：`checkpoints/P6-handoff.md`

## 1. 本批次目标

完成 TTS 输出链路的第一批领域基础：不可变 VoiceProfile 版本、用户唯一修改权限，以及会话对已确认 profile 版本的绑定。

本批次不连接 VoiceStudio、Edge TTS、播放设备、网络、ASR、UI 或真实音频；不实现句子分段、Provider 能力、播放队列、故障切换或 P8 组合闭环。

## 2. 已完成内容

1. 新增 `app/tts/` 公开入口和 `app/tts/domain/` 领域模块；
2. 新增 `VoiceProfile` 不可变对象，支持 `preset`、`designed`、`cloned` 三类；
3. 校验 provider、语言、语速、音高、版本、设计提示词和克隆音色边界；
4. 新增 `VoiceProfileRegistry`，支持用户创建、严格递增版本、启用、选择、禁用、删除、回退和历史查询；
5. 非用户 actor（Agent、LLM、Provider、System）不能修改、选择、启用、删除或回退 VoiceProfile；
6. 新版本追加而不覆盖旧版本；回退通过创建新版本实现；
7. 新增 `VoiceProfileSession`，绑定 `session_id + profile_id + profile_version`，会话快照不会随之后 profile 变更而漂移；
8. `to_public_dict()` 脱敏，不输出 `provider_profile_id` 与 `design_prompt`；默认 repr 也不暴露敏感引用；
9. 创建 profile 不自动启用、不自动选择；删除已选择 profile 时清除选择，不自动切换到其他音色；
10. 未修改路线图、task_plan、progress 或 P6 handoff；本文件是本批次新增的续接入口。

## 3. 关键不变量

- 只有明确的 `VoiceProfileActor.USER` 可以改变 VoiceProfile 注册表；
- Agent、LLM、Provider 和恢复逻辑没有隐式换音色、保存音色、覆盖版本或自动选择权限；
- profile 版本必须严格递增，旧版本保留在历史中；
- 会话绑定到创建时的 profile 版本，后续新版本不改写已绑定会话；
- 只有用户启用后的 profile 才能被用户选择；创建、启用和选择是三个独立动作；
- `designed` 必须包含用户确认的 `design_prompt`；`cloned` 只能引用 VoiceStudio；
- Provider 引用与设计提示词不能进入公开状态视图；
- 本批次没有文件读写、网络请求、进程/命令、线程/定时器、真实音频设备或 Provider 调用。

## 4. 修改文件

- `app/tts/__init__.py`
- `app/tts/domain/__init__.py`
- `app/tts/domain/voice_profile.py`
- `tests/tts/__init__.py`
- `tests/tts/test_voice_profile.py`

## 5. 验证证据

- TTS VoiceProfile 专项：`8/8`；
- 全量 unittest：`353/353`；
- `compileall app tests`：通过；
- 新增 Python 文件严格 UTF-8：`5/5`，替换字符 `U+FFFD`：`0`；
- TTS 模块执行面 AST 审计：`0` 个网络、进程、命令、线程、异步、真实音频设备入口；
- 公开导入与最小 session binding smoke：通过，输出 `smoke_profile 2 smoke_profile`；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实 Provider，未执行电脑副作用，未执行 Git add/commit/push。

## 6. 已知限制

- 目前只有 VoiceProfile 领域对象和内存注册表，不提供持久化；
- 尚未实现文本流到完整句 segment 的切分；
- 尚未实现 Provider capability、VoiceStudio/Edge adapter、播放事实、播放队列、背压、取消或 barge-in；
- 尚未实现 VoiceStudio → Edge → 文字降级、冷却、固定探测和恢复待下一轮启用；
- 尚未实现真实播放设备确认，因此没有把“合成完成”错误解释为“用户已听到”的机会，但播放事实状态机仍待下一批次；
- 本阶段仍未完成，不能进入 P8 半双工语音闭环。

## 7. 下一批次入口与强制停止

下一批次为 **P7 批次 2：文本流到完整句 segment 的分段契约**。

下一会话应：

1. 先读取本文件；
2. 只实现文本缓冲、完整句边界和句段元数据，不接 Provider 或播放设备；
3. 优先覆盖拒绝路径：空文本、极短 token 片段、乱序/重复提交、旧 reply、超过队列预算和取消后的迟到文本；
4. 保持 `reply_id` 边界，不把已生成文字当作已播放事实；
5. 完成专项测试、全量回归、compileall、严格 UTF-8 和执行面审计；
6. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio 或 Edge TTS 网络/真实 Provider；
- 接入播放设备、sounddevice、扬声器或真实音频输出；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。