# 实施计划导航

> 最近重组：2026-09-18  
> 当前状态：**P6 已完成；意图路由契约设计已同步；实现仍停在 P7/TTS 授权门前。**

## 文档布局

- implementation-roadmap.md：P0-P11 唯一实施路线权威。
- P0-freeze-baseline.md：P1-P11 必须继承的 P0 冻结清单。
- `design.md`：总体设计细节。`docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md` 是意图路由契约的唯一权威来源。
- `persona.md`：当前人物模板权威来源。
- `research/findings.md`：早期调研与结论。
- `research/`：专题调研和固定版本源码对比。
- `archive/`：迁移前入口、快速索引和完整旧版计划，仅用于追溯核对。
- 项目根目录的 `../task_plan.md`、`../progress.md`：阶段状态和跨会话恢复，不移动。

## 最小读取规则

1. 先读本文件，确定当前主题对应哪个阶段。
2. 只读一个阶段文件。
3. 仅在该阶段的“按需阅读的共享依赖”中打开必要文件。
4. 需要查看尚未定稿问题时，定位 `discussions/pending-decisions.md` 的对应标题，不读全部历史。
5. 迁移核对或追溯原文时，才读取 `archive/implementation_plan.legacy.md`。

## 项目最高原则：实时语音 AI 伙伴

本项目的本质不是“录音 → ASR → 播报”的单轮语音工具，而是：

> **用户与 AI 持续实时对话；既可以闲聊，也可以在提出需求时安全地操控电脑。**

产品层必须始终保持一个连续对话体验：

```text
用户说话
→ AI 实时理解
→ 判断是闲聊还是电脑任务
→ 闲聊：自然回应
→ 电脑任务：理解、规划、按安全规则请求授权、执行并汇报
→ 回到持续对话
```

“按键触发”“VAD 自动收音”“唤醒词”“连续对话”“实时打断”等是**开发阶段和底层实现顺序**，不是产品定位，也不是互相割裂的产品模式。任何阶段性降级都不得把项目重新定义成单轮录音应用。

## 并列最高原则：人物模板驱动的拟人表达

AI 面向用户说出的自然语言必须遵循用户选择并确认的人物模板。人物感不是后期装饰或可选功能，而是核心交互的一部分；由于闲聊是主要使用场景，设计和验收必须优先保证自然、口语化、有连续人格和情绪温度的对话体验，不能退化成客服腔、工具播报腔或机械状态提示。

- 闲聊、追问、澄清、没听清提示、错误说明、任务进度和执行结果的**语音表达**都要经过人物模板约束。
- `persona.md` 是当前人物模板的权威来源；只有用户可以修改、切换或停用人物模板，Agent 不得自行改写人设、关系、称呼或表达风格。
- VoiceProfile 决定“声音听起来怎样”，人物模板决定“AI 说什么、怎么说”；两者必须同时生效，但相互独立，不能用换音色代替人物表达。
- 人物表达不得削弱真实性、安全性和授权清晰度。路径、动作、风险、失败原因、确认范围以及固定授权短语必须准确完整；可以拟人化措辞，但不能撒娇诱导授权、淡化风险或省略关键信息。
- `plan.json`、审计日志、执行方案、结构化错误码等机器或正式文档不强制使用人物口吻；人物模板主要约束面向用户的对话和语音层。

## 最新接续摘要

- 新开对话时读取：[`checkpoints/2026-09-24-intent-routing-design-sync-handoff.md`](./checkpoints/2026-09-24-intent-routing-design-sync-handoff.md)
- P4 批次 1 已完成声明式 Tool Manifest 与不可变 ToolRegistry；尚无工具执行能力。当前 `.venv` 仍不可启动，验证使用项目内 `.python312\python.exe`。

## 当前讨论位置

- I.9 音频会话级故障与恢复已定稿：会话预热、运行期故障、任务隔离、Provider 冷却、固定材料探测和重新启用均已有硬规则。
- 已完成固定版本 GitHub 源码对比，覆盖 Temporal Python SDK、MCP Python SDK、LangGraph、OPA、sherpa-onnx、Wyoming Piper，并合并已有 LiveKit Agents、Pipecat 的 TTS fallback 结论。
- 已形成 P0-P11 分阶段实现路线图；该路线图是未来编码入口，主题编号仍只作设计导航，不作实现依赖顺序。
- Skill 已补充声明式 manifest、版本、哈希和风险上限建议，但仍标记为“完整候选，已完成源码对比优化，待用户进一步确认”，不记作用户已单独确认的最终结论。
- P0-P3 已完成；P4 批次 1 全量 unittest 117/117 通过，已进入工具声明层，但尚未实现任何工具调用、ActionPlan、执行器或语音链路。
- P6 已完成；P7-R 意图路由契约设计同步已完成；实现仍停在 P7/TTS 授权门前。
- 阶段化实现入口：[`implementation-roadmap.md`](./implementation-roadmap.md)
- GitHub 对比入口：[`research/github-comparison-and-optimization.md`](./research/github-comparison-and-optimization.md)
- 当前接续入口：[`checkpoints/2026-09-24-intent-routing-design-sync-handoff.md`](./checkpoints/2026-09-24-intent-routing-design-sync-handoff.md)
- 待定稿与候选入口：[`discussions/pending-decisions.md`](./discussions/pending-decisions.md)

## 阶段化编码入口

- [`implementation-roadmap.md`](./implementation-roadmap.md)：P0-P11 的唯一未来编码入口，规定每阶段目标、不做事项、文件范围、批次、测试、退出条件、停止点和最小读取文件。
- [`research/github-comparison-and-optimization.md`](./research/github-comparison-and-optimization.md)：固定 commit/tag 的源码对比、借鉴与明确不采用内容，以及新增验收测试。
- [`research/tts-fallback-github-comparison.md`](./research/tts-fallback-github-comparison.md)：LiveKit Agents/Pipecat 的 TTS fallback 专题证据。

## 阶段文件

> 下表是设计主题导航，不等于未来代码实现顺序；实际实现以 P0-P11 路线图为准。
| 阶段 | 文件 | 主要内容 |
|---|---|---|
| 总览 | [`00-project-overview.md`](./00-project-overview.md) | 项目目标、开源规则、运行时主 Agent |
| P0 | [`P0-environment.md`](./P0-environment.md) / [`P0-freeze-baseline.md`](./P0-freeze-baseline.md) | 环境基线、冻结契约与延期分类 |
| 1 | [`01-project-foundation.md`](./01-project-foundation.md) | 骨架、配置、日志 |
| 2 | [`02-tts-voicestudio.md`](./02-tts-voicestudio.md) | VoiceStudio、VoiceProfile、TTS 与兜底 |
| 3 | [`03-audio-capture.md`](./03-audio-capture.md) | 录音、AudioFrame、pre-roll |
| 4 | [`04-asr.md`](./04-asr.md) | SenseVoice 与可选流式 ASR |
| 5 | [`05-agent-integration.md`](./05-agent-integration.md) | DeepSeekHarness Python SDK |
| 6 | [`06-tools-and-policy.md`](./06-tools-and-policy.md) | 工具注册、路径和策略 |
| 7 | [`07-confirmation-and-stop.md`](./07-confirmation-and-stop.md) | L2/L3 授权与紧急停止 |
| 8 | [`08-text-end-to-end.md`](./08-text-end-to-end.md) | 文字端到端闭环 |
| 9 | [`09-voice-end-to-end.md`](./09-voice-end-to-end.md) | 按键触发语音闭环 |
| 10 | [`10-wake-word-and-voiceprint.md`](./10-wake-word-and-voiceprint.md) | 唤醒词与声纹过滤 |
| 11 | [`11-vad-and-dialog-state.md`](./11-vad-and-dialog-state.md) | VAD、连续对话、状态机 |
| 12 | [`12-barge-in-and-aec.md`](./12-barge-in-and-aec.md) | 实时打断、AEC、降噪 |
| 13 | [`13-device-tuning.md`](./13-device-tuning.md) | 真机调优和发布验收 |

## 共享文档

- [`shared/architecture.md`](./shared/architecture.md)：公共配置、架构和降级方案。
- [`shared/safety-boundaries.md`](./shared/safety-boundaries.md)：跨阶段硬安全边界。
- [`shared/action-plan.md`](./shared/action-plan.md)：执行计划、确认、文档和执行链路。
- [`shared/error-recovery.md`](./shared/error-recovery.md)：崩溃、重启、状态核验和重新授权。
- [`shared/data-and-privacy.md`](./shared/data-and-privacy.md)：数据分类、音频和音色隐私。
- [`shared/milestones.md`](./shared/milestones.md)：MVP 路线、里程碑和实施顺序。
- [`shared/manual-and-hotwords.md`](./shared/manual-and-hotwords.md)：手册和热词要求。
- [`discussions/pending-decisions.md`](./discussions/pending-decisions.md)：尚未定稿问题。

## 不变的阶段策略

```text
最终目标：实时、连续、可打断的语音对话

实施顺序：
按键触发语音闭环
→ VAD 自动判断说完
→ 唤醒词与声纹过滤
→ 连续对话
→ 实时打断、降噪与 AEC
```

VoiceStudio 是可替换的语音数据面；它不参与 Agent 决策、安全确认或电脑操作。默认采用提示词设计音色，保留用户手动创建和选择克隆音色的能力。




## 意图路由契约（2026-09-24）

- 权威契约：[`../docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md`](../docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md)
- 统一入口：`TextShell / ASR accepted final → ConversationManager → IntentRouter → RouteHandoff`。
- 两层结构：`route + intent`；高层 route 为 `chat / query / task / dangerous / ambiguous / unsupported`。
- 每个交接对象必须携带 `request_id`、`conversation_id`、`turn_id`、`source`、`raw_text`、`created_at`；`conversation_id` 复用现有 `Conversation.session_id`。
- 路由失败、低置信度、超时、不可用和非法输出统一规范化为 `route=ambiguous`，不得让 Agent 在路由失败后自由重判。
- `slots` 只是候选参数；只有 `ConversationManager` 在明确确认且安全流程允许时，才能将候选标记为 `confirmed=true`。分类器置信度不等于授权。
- 意图路由不授权、不执行、不创建 `AuthorizationSnapshot`、不写 `ExecutionLedger`；电脑副作用继续经过完整安全链。
- 当前设计同步已完成，但代码实现仍未开始；下一实现入口为 **P7-R → IntentRouter/RouteHandoff → ConversationManager 接缝**。
