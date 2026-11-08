# 阶段 9：语音闭环（录音→ASR→LLM→工具→TTS）

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/architecture.md`](./shared/architecture.md)
- [`shared/safety-boundaries.md`](./shared/safety-boundaries.md)
- [`discussions/pending-decisions.md#h-音频链路`](./discussions/pending-decisions.md#h-音频链路)

- 目标：先用按键触发跑通稳定的“说话 → 回复语音”闭环；最终产品形态仍是实时、连续、可打断的语音对话。
- 步骤：
  - [ ] 串起：录音 → ASR（默认 SenseVoice，可选 VoiceStudio 流式 Provider）→ LLM(工具) → TTS（VoiceStudio 主 Provider，可选 Edge TTS 兜底）
  - [ ] MVP-1 先用按键触发代替唤醒词（按一下开始录音），跑通闭环；音频层仍按流式帧设计
  - [ ] TTS 播放期间暂停普通录音，避免没有 AEC 时把助手自己的声音送入 ASR；此阶段不支持语音 barge-in
  - [ ] 安全确认改用语音：TTS 问；确认提示播放完成后再接收用户回答，用户回答不直接等同授权，仍由安全状态机校验
  - [ ] 后续阶段再启用按句流式 TTS、连续监听和 barge-in，不改变 MVP-1 的按键触发策略
- 产出：可用的按键触发语音对话原型
- 验收：按住触发 → 说话 → 听到当前 VoiceProfile 的语音回复，能指挥电脑；TTS 故障时按已确认规则降级

## Voice path intent-routing seam (2026-09-24)

The button-triggered voice loop is:

```text
recording
-> ASR accepted final
-> ConversationManager(source=asr)
-> IntentRouter
-> RouteHandoff
-> Agent / ActionPlan / Safety
-> TTS
```

TTS can express an established conversation result, clarification, authorization prompt, or execution result. It cannot change route, intent, trace fields, authorization facts, or execution facts. Voice and text use the same safety chain; successful recognition is not confirmation or authorization.
