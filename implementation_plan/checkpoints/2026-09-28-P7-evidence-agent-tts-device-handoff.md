# P7 证据项 5：Agent → 回复文字 → VoiceStudio → Windows 设备端到端交接

- 执行日期：2026-09-28
- 结论：**Blocked / 未取得真实端到端证据**
- 范围：只核验现存 fail-closed runtime 和显式 opt-in seam；未改应用代码、未连接真实 Agent、未请求真实 TTS、未播放音频、未提交 Git。

## 1. 安全边界与执行范围

严格遵守本项授权：默认 runtime 保持 fail-closed；只查看/验证已有显式 opt-in 接缝。未启用 DeepSeek、任何云端 Provider 或 Edge TTS；未调用应用工具、Agent 工具能力、文件/网络/系统动作。测试仅运行仓库既有离线单元测试；测试本身使用临时目录/内存流模拟隔离输入输出。唯一持久写入是本交接文档。

本项没有向 `127.0.0.1:3900` 发 VoiceStudio 请求，也没有接触 Windows 播放设备。此前的本地 VoiceStudio 成功基线（见 `2026-09-28-P7-real-voicestudio-success-baseline.md`）只能证明本地 TTS 单独可用，不能替代 Agent 端到端证据。

## 2. 核查到的实现边界

- `app/runtime/assistant_runtime.py`：默认使用本地 `LocalChatRouter` 和 `UnavailableAgentAdapter`。后者返回 `completed=False` 的明确不可用结果，不生成伪造 Agent 回复；`voice_enabled=True` 但没有注入 conversation/adapter 时直接拒绝构造。只有调用者显式注入适配器/会话，并显式传入播报回调及启用语音，才会进入 opt-in 接缝。
- `app/main.py`：默认 `voice_enabled=False`；不接 VoiceStudio 设备，不创建 Provider transport。文字回复由 TextShell 优先输出；语音回调异常不抹除已显示的文字。
- `app/ui/text_shell.py`：仅完整、无失败、非空且有结束标志的 `AgentTurnOutcome` 可被转交回调；先输出并 flush 原文，再触发播报回调。
- `app/tts/completed_reply.py`：将相同 outcome.text 交给已注入 speaker；重复请求防重；失败/回退后禁用该实例后续播报。该模块自身不会安装/启动 Agent，也不会接入默认 runtime。

以上是代码边界和离线接缝事实，不是本机真实 Agent 或设备事实。当前仓库默认 Agent 明确 unavailable；离线测试里的 Adapter、Conversation、Speaker 都是测试替身，不可升级为“真实 Agent”。

## 3. 执行证据

在项目根目录运行：

```text
.\.venv\Scripts\python.exe -B -m unittest tests.foundation.test_main_voice_opt_in tests.tts.test_completed_reply -v
```

结果：**21 tests / OK**（运行时间约 0.293 秒）。涵盖的关键边界：

- 默认 runtime 在伪设 `DEEPSEEK_API_KEY` 存在时仍显示 Agent unavailable，输出不暴露 DeepSeek 字样；测试断言默认不构造 Agent SDK 或 TTS device。
- 未显式 opt-in 时，即使提供测试 conversation 与回调，也不触发播报回调。
- `voice_enabled=True` 且注入测试 conversation/speaker 时，先检查文字已经进入输出，再触发回调；原文相同。
- 显式注入的测试 Adapter 链路事件顺序为 Agent adapter → speaker，且原文显示在输出中。
- 失败 Agent 结果不播报；播报回调异常不影响已显示的回复文字。
- 完整回复经过独立 `CompletedReplySpeaker` 测试时，只有 fake Preview 接到完全相同的文字；重复、失败和 mismatch 有阻断/防重检查。

这些全是离线 fake/mock/测试替身；没有证明真正 Agent 请求、VoiceStudio 音频生成、Windows waveOut 设备输出，也没有证明用户听见。测试绿色不等于端到端通过。

## 4. 真实端到端证据账本

| 证据环节 | 本次状态 | 事实 |
|---|---|---|
| 真实 Agent 可用/实际回复 | **Blocked** | 默认实现是 `UnavailableAgentAdapter`；没有授权且可用的本机真实 Agent 可供本项调用。未伪造回复。 |
| 回复文字先呈现 | 离线 seam **Pass**；真实 Agent 链 **未证** | 21 项离线测试覆盖文字先 flush；没有真实 Agent 回复。 |
| VoiceStudio 收到该真实回复 | **未执行 / 未证** | 没有调用本机 API；无 request/audio id。 |
| VoiceStudio 合成结果 | **未执行 / 未证（本项）** | 既有单独 TTS 成功基线不能归因于 Agent 回复。 |
| Windows 输出设备播放 | **未执行 / 未证（本项）** | 没有打开 waveOut/设备，无 playback event。 |
| backend stop ACK / 声学停止 / user_heard | **None / 本项不涉及** | 不得从离线测试或其他 case 推断。 |

## 5. 文字回退事实

默认运行路径没有真实 Agent 时显示清楚的 unavailable 文本，不声称任务完成；语音未启用。显式 opt-in seam 即使回调失败，也保留已 flush 的原始回复文字。失败或不完整 Agent outcome 不进入播报回调。因此本项的文字可用性有离线回归覆盖，但不存在真实 Agent 回复可供验证其真实 TTS 失败后的回退。

## 6. 未完成原因及安全停点

当前阻塞是**真实 Agent 不可用/未授权连接**，而不是 VoiceStudio 本地模型未安装。用户明确禁止云端 Provider、DeepSeek 和工具/系统副作用，故不得通过网络 Provider 拼出所谓真实 Agent 证据。没有真实 Agent 回复就不能安全或诚实地调用 opt-in TTS 来声称完成 Agent→文字→VoiceStudio→设备链路。

后续若要解阻，须在不违反本项禁令前提下提供明确可用、已授权且本地/离线的真实 Agent 实例及其启动/调用方式；并另行明确允许一次受限 VoiceStudio 请求和 Windows 播放。不得把当前测试替身冒充真实 Agent。完成后要分别记录 Agent reply id、文字输出事实、VoiceStudio audio id、waveOut 设备结果；`user_heard` 仍须由用户独立确认。

## 7. 变更与提交

- 应用代码：无修改。
- 新建：本交接文档。
- Git commit：无。
- 仓库执行前已有大量 staged/untracked 项；`git status --short` 用于识别该基线，本次不对其进行 add/reset/commit，也不将既有状态归因于本项。

**最终判定：证据项 5 仍 Blocked；只验证了默认安全回归与显式 opt-in 接缝边界，未取得真实 Agent → 回复文字 → VoiceStudio → Windows 设备端到端证据。P7 继续 Blocked，不进入 P8。**