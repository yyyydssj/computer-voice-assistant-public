# P6 ASR 按键输入完成交接

> 交接日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 阶段编号：P6  
> 阶段状态：`complete`  
> 当前停止点：**P7/TTS 授权门前；本次未进入 TTS。**  
> 阶段验收：`checkpoints/2026-09-21-P6-integration-acceptance.md`

## 1. 本阶段目标

完成一次按键触发、半双工、显式驱动的语音输入链路：

```text
Capture → AudioBus → VAD → SenseVoice → ResultGate → ConversationManager
```

只有当前 boundary 内通过 ResultGate 的可靠 final 才能提交一次 Conversation 请求；任何故障或不可靠事件均关闭当前语音边界并保持文字输入。

## 2. 已完成内容

1. AudioBus：设备 epoch、sequence、采样率、声道、格式和时间戳契约；
2. Capture：受控设备选择、pre-roll、callback 入总线、临时音频 owner/cleanup；
3. VAD：按键释放/最小静音结束判定，不承担身份或安全确认；
4. SenseVoice adapter：惰性 Provider load、session stream、PCM 解码块、final 事件和能力矩阵；
5. ResultGate：current session/audio session/generation/stream/utterance/event sequence 验证，interim 丢弃，重复/迟到/旧 boundary/旧 generation 拒绝；
6. Recovery：startup/session/runtime 失败、文字降级、显式 probe、恢复后等待用户切回；
7. 正式 `PushToTalkCoordinator`：显式 start/pump/release，完成资源收口、一次性 final 提交和文字 fallback；
8. 阶段级集成测试：正式纳入 `tests/integration` discovery，覆盖六项 P6 退出条件。

## 3. 关键不变量

- 新按键轮次必须提供全新的 `session_id + audio_session_id + provider_generation + stream_id + utterance_id`；
- 正常轮次最多产生一次 `ACCEPTED_FINAL` 和一次 Conversation 请求；
- ResultGate 关闭后迟到事件不得提交；已见 event_id 的 replay 优先返回 `DUPLICATE`；
- startup/provider/session/runtime failure 只改变输入通道，不改变任务、计划、授权、执行或结果事实；
- 文字输入始终可用；ASR 不可用不会阻塞既有文字安全链；
- 临时 PCM、临时路径、异常正文和识别文本不进入 Agent、日志、审计正文；
- 不自动重试、不自动切换 Provider、不自动切回语音、不补识别 warmup 音频；
- 不产生 TTS、连续监听、唤醒词、声纹、AEC、实时打断或语音 L3 权限。

## 4. 验证证据

- P6 集成：6/6；
- 全部 integration：12/12；
- ASR：29/29；音频：31/31；Conversation：19/19；
- 全量 unittest：345/345；
- compileall：通过；严格 UTF-8：120/120，无 `U+FFFD`；
- P6 新增/修改文件执行面审计：无网络、进程、命令、`.env`、后台线程/定时器或真实音频设备调用；
- 未读取 `.env`，未下载模型，未访问网络，未调用真实麦克风，未执行电脑副作用，未执行 Git add/commit/push。

## 5. 已知限制

- 本阶段使用 fake backend/recognizer 完成阶段级证据；真实模型、真实设备和真实云端 Provider 不在本阶段验收范围；
- SenseVoice 能力矩阵中 `unsupported` 不得解释为 `not_detected`；多人重叠、声纹、AEC 和实时打断仍未实现；
- `PushToTalkCoordinator` 是显式驱动器，不含后台 loop、定时器或 UI 按键绑定；上层未来必须显式驱动 start/pump/release；
- 文字 fallback 复用已有 ConversationManager/P5 安全链，但本阶段不修改 P5 授权、执行、审计和恢复规则；
- 仓库当前仍为未跟踪基线，未创建 commit。

## 6. 下一阶段入口与强制停止

下一阶段只有在用户明确授权后才能开始：**P7 TTS 输出与人物表达**。

在新的授权前不得：

- 接入 TTS、Sentence、Playback、VoiceProfile；
- 将 ASR 与 TTS 组合成语音闭环；
- 开启连续监听、唤醒词、AEC、全双工或实时打断；
- 开放语音 L3 或真实云端/Windows ASR；
- 修改 P5 授权、执行、审计、恢复和重试语义。
