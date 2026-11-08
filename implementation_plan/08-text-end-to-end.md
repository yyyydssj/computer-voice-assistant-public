# 阶段 8：文字版端到端（文字→LLM→工具→文字）

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/safety-boundaries.md`](./shared/safety-boundaries.md)
- [`shared/action-plan.md`](./shared/action-plan.md)
- [`shared/error-recovery.md`](./shared/error-recovery.md)

## 目标

不依赖语音输入，先跑通“理解请求→生成结构化计划→本地安全校验→用户授权→受控执行→结果验证→文字汇报”的核心链路。该链路既是实现阶段的测试入口，也是 ASR 整体不可用时的正式降级通道。

文字模式不是备用 AI，也不是允许 Harness 自由操作电脑的特殊模式。键盘输入仍使用与语音模式相同的 DeepSeekHarness Agent、会话边界、工具 Schema 和本地安全层。

## 文字输入与 Agent 链路

每次键盘提交都创建新的 `request_id`：

```text
用户键盘输入
→ DeepSeekHarness Python SDK
→ 同一个 Harness Agent
→ 闲聊文字回复，或结构化电脑任务计划
```

如果输入是电脑任务，必须继续经过完整本地执行链：

```text
DeepSeekHarness Agent
→ Schema 校验
→ PathGuard
→ PolicyEngine
→ 用户 L2/L3 授权
→ 本地受控执行器
→ ResultVerifier
→ 结构化结果与文字汇报
```

Harness Agent 只负责理解、规划和汇报。它不能直接调用任意 Shell、PowerShell、Python、批处理或脚本，也不能绕过本地安全层操作电脑。

## ASR 故障时的输入降级

- ASR 整体不可用时自动进入文字输入模式，界面显示故障原因和当前输入状态。
- 停止语音识别并清除当前未完成识别轮次；残留音频不得提交给其他未经配置的 ASR Provider。
- 不自动切换 Windows 语音识别、云端 ASR 或其他服务。
- 用户可继续键盘输入闲聊或电脑任务。
- TTS 可用时采用“文字输入、语音输出”；TTS 不可用时采用纯文字交互。
- 后台低频健康检查可以发现 ASR 恢复，但不得使用用户正在说的话做测试，也不得上传测试音频。
- ASR 恢复后只提示用户，不自动退出文字模式；用户点击“切回语音”或通过文字明确要求后，才进入切换流程。
- 切换只在安全空闲点发生，不得打断键入、Agent 回复、方案查看/修改、L2/L3 授权、任务执行或恢复扫描。
- 切回前清理旧音频和事件状态、创建新的输入会话边界，并完成仅限本机的麦克风与 ASR 联合测试；失败则继续使用文字模式。

## 文字模式授权

- L0/L1：继续执行原有路径、数量、内容、超时和敏感字段限制。
- L2：用户查看方案后，仍须在正式弹窗中点击“允许执行”。
- L3：先点击“允许执行”，再在独立确认输入框完整键入 `确认执行`。
- L3 确认必须绑定当前 `plan_id`、版本、哈希、动作和有效期。
- 计划变化、超时、用户停止/取消、程序异常或任务中止后，确认和授权立即失效。
- ASR 故障不能降低权限等级，也不能复用语音模式中遗留的旧授权。

## 实施步骤

- [ ] `llm/` 通过 DeepSeekHarness Python SDK 的受限 profile/插件接入结构化工具调用，只暴露已批准的只读或受控工具。
- [ ] 实现键盘输入入口，并为每次提交生成新的 `request_id`。
- [ ] 测试闲聊输入：文字输入→同一个 Harness Agent→文字回复；TTS 可用时可同时播报。
- [ ] 测试只读任务：“帮我把示例目录列出来”→结构化工具请求→本地安全层校验→返回结果→文字回复。
- [ ] 测试 L2 任务：Harness 只提出结构化计划→本地预检→用户点击“允许执行”→受控执行和验证。
- [ ] 测试 L3 任务：点击“允许执行”→在独立输入框键入“确认执行”→重新校验→受控执行和验证。
- [ ] 测试非法工具、任意命令、越界路径、旧授权复用和不完整计划均被本地安全层拒绝。
- [ ] 测试 ASR 故障进入文字模式后，不切换未经配置的 Provider，不降低授权等级。
- [ ] 测试 ASR 恢复后只提示、不自动切回；用户请求后只在安全空闲点完成状态清理、联合测试和模式切换。

## 产出

- 完整的 LLM、工具、安全、授权、执行和验证文字链路；
- ASR 故障时可用的键盘输入界面；
- 文字模式 L2/L3 授权界面与审计事件。

## 验收

- 纯文字输入能够与语音输入调用同一个 Harness Agent，并保持闲聊/工作会话隔离。
- Harness 只能提出结构化回复或计划，不能直接自由操作电脑。
- 所有电脑任务都经过 Schema、PathGuard、PolicyEngine、授权、本地执行和 ResultVerifier。
- ASR 不可用时仍可完成文字闲聊和合规电脑任务；TTS 可用时可继续语音输出。
- 文字模式不会自动切换未经配置的 ASR，也不会降低、跳过或复用 L2/L3 授权。
- ASR 恢复不会自动改变输入模式；只有用户明确请求且本地联合测试通过后，系统才在安全空闲点切回语音输入。

## Text path intent-routing seam (2026-09-24)

Text input cannot bypass intent routing:

```text
TextShell
-> ConversationManager(source=text_shell)
-> IntentRouter
-> RouteHandoff
-> Agent / ActionPlan / Safety
-> Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit
```

Each turn retains `request_id`, `conversation_id`, `turn_id`, `source=text_shell`, `raw_text`, and `created_at`. Routing failure enters `ambiguous` for clarification or rejection; text mode is not a bypass channel.
