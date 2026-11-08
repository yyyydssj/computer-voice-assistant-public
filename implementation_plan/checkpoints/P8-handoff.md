# P8 阶段交接：按键触发的半双工语音闭环

> 日期：2026-09-30  
> 阶段状态：`complete`（阶段级，离线证据齐备）  
> 验收文档：`2026-09-30-P8-integration-acceptance.md`  
> 前置交接：`2026-09-30-P8-batch1-3-voice-loop-chat-closed-loop-stop-handoff.md`、`P7-handoff.md`、`2026-09-30-P7-stage-acceptance-final.md`
> 权威文档：[../docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md](<PROJECT_ROOT>\docs\superpowers\specs\2026-09-24-intent-routing-contract-design.md)

## 1. 阶段编号

P8（按键触发的半双工语音闭环），实现批次 1-6 全部完成。

## 2. 阶段状态

`complete`（阶段级）。五条退出条件逐条通过，证据见验收文档第 2 节；真实设备人工实测与真实模型提议为显式延期项。

## 3. 本阶段唯一目标

组合 P5、P6、P7：按键开始说话，得到文字，调用同一个 Harness，文字回复可播报，电脑任务仍走完全相同的安全链。

## 4. 本阶段明确不做（仍然有效）

- 不增加自动唤醒、不增加连续多轮自动收音（P9）。
- 不增加播放期间监听、AEC 或声纹认证（P10）。
- 不改变 P5 的授权规则；语音不得成为新的权限通道。
- 不开放任意命令、PowerShell、脚本或未注册工具。
- 不接入真实 DeepSeek Provider（R 支线独立授权门）。

## 5. 进入条件（实际满足）

- P5、P6、P7 均已阶段级验收通过，且全量回归为绿。
- 用户 2026-09-30 明确要求完成 P8（批次 1-3 已单独授权，批次 4-6 随本轮授权）。

## 6. 允许修改的文件范围（实际改动）

```text
app/voice_loop/                          (新增 5 个模块)
app/runtime/orchestration/               (新增 4 个模块)
app/asr/push_to_talk.py                  (+19/-4 行，纯增量身份透传)
tools/p8_real_voice_loop_runner.py       (新增，真机验收 runner，--live-opt-in 必需)
tests/voice_loop/                        (新增，67 项)
tests/orchestration/                     (新增，69 项)
tests/integration/test_p8_integration.py (新增，6 项)
tests/integration/test_p8_stage_acceptance.py (新增，17 项)
tests/integration/test_p8_real_runner.py (新增，11 项，runner 离线契约)
implementation_plan/、task_plan.md、progress.md (文档收口)
```

未触碰：`app/safety/**`、`app/executor/**`、`app/plans/**`、`app/auditing/**`、`app/verification/**`、`app/tools/**`、`app/tts/**`。

## 7. 依赖的稳定契约

- `app/contracts/routing.py`：`RouteHandoff`/`RouteTrace`/`RouterStatus`（route+intent、handling_mode、tool_scope、required_confirmation）。
- `app/conversation/manager.py`：`submit_text` / `submit_asr_final` 统一入口与 `ConversationRouteResult`。
- `app/asr/push_to_talk.py`：`PushToTalkCoordinator.start/pump/release/runtime_failed` 与 `ResultGateDecision`。
- `app/safety/policy_engine.py`、`app/plans/action_plan.py`：`PolicyInput`/`PolicyDecision`/`build_plan_action`/`build_reviewable_plan`。
- `app/safety/confirmation/**`：`L2ConfirmationController`/`L3ConfirmationController`/`AuthorizationConsumptionStore`/`ConfirmationResolution`。
- `app/safety/preflight.py`：`ExecutionPreflightGate.check` → `ExecutionPreflightApproval`。
- `app/executor/**`：`ExecutionLedgerRegistry`/`ExecutionLedgerStore.request_stop`/`CancellationDisposition`/`ControlledExecutionRunner`。
- `app/plans/result_document.py`：`ResultDocument`/`ResultAction`。

## 8. 实现批次（全部完成）

| 批次 | 内容 | 关键文件 |
|---|---|---|
| 1 | 轮次边界与显式状态机（单活动轮次、身份不复用、迟到边界不可信、任务状态 fail closed） | `app/voice_loop/boundary.py` |
| 2 | 统一输入接缝与文字降级（同一会话入口、只切通道、不自动切回、公开 API 无授权面） | `app/voice_loop/input_seam.py` |
| 3 | 闲聊闭环接线（真实 P6 管道 + 逐句播报 + 失败清理） | `app/voice_loop/chat_loop.py`、`drivers.py` |
| 4 | 任务路径编排（冻结计划 → L2/L3 → 复检 → 执行 → 验证 → 摘要） | `app/runtime/orchestration/task_flow.py`、`summary.py` |
| 5 | “停”三语境（聊天/授权/执行）与迟到事件隔离 | `app/runtime/orchestration/stop.py` |
| 6 | 阶段级集成验收、跨阶段回归、审计与文档收口 | `tests/integration/test_p8_stage_acceptance.py`、验收文档 |

## 9. 测试与故障注入

- 正向：闲聊闭环、任务闭环（L2/L3）、文字通道、摘要播报、计划口播。
- 拒绝路径优先：路由越界/处理模式不匹配、工具范围外动作、未知工具、越界路径、策略拒绝、混合风险、重复动作 ID、非 plannable 工具、计划漂移、旧窗口、空短语/错误短语、重复提交、通道串用、foreign/retired 边界、复用已消费授权、预检失败、确定性执行失败、状态不确定、审计不可写、speaker 失败、迟到事件。
- 故障注入：Provider 启动失败、解码失败、审计追加失败（`audit_append_failed`）、backend 失败与 `side_effect_may_have_occurred`、不可安全中断区间、第二次停止、停止后签发新 executor 句柄。
- 结果：全量 **1266 tests OK (skipped=3)**、`compileall` exit 0、`git diff --check` exit 0。

## 10. 退出条件（可观察事实）

见验收文档第 2 节；五条全部通过（离线阶段级证据）。

## 11. 已知风险

1. 播报为“完整回复后整段推入流式会话”，未使用 P6 的增量播报路径，首音延迟未优化。
2. `PushToTalkVoiceDriver` 依赖 P6 协调器的四个方法名（鸭子类型），改名会以 `TypeError` 暴露。
3. 语音提交由 P6 管道执行、由 `VoiceInputSeam.close_voice_turn` 回收边界；“单次提交”由 `ResultGate` + 边界活动性共同保证。
4. 只读 `query` 直执行路径未实现（fail closed），只读任务目前必须经同一条确认链或被拒绝。
5. Agent 侧结构化动作提议端口未接真实模型（no-tools 运行时），当前以注入实现验证。
6. 未做真实麦克风/扬声器人工实测；未接默认运行时（`app/main.py`）。

## 12. 未解决但明确延期的问题

- **真机按键实测已完成（2026-09-30，用户本人）**：`tools/p8_real_voice_loop_runner.py --live-opt-in --turns 2 --tts-mode ws_pcm` 两轮全部 `accepted/submitted/spoken = true`，TTS 走真实 `voicestudio_ws_pcm`（2 句 / 3 句全部完成、无错误），每轮五个轮次身份互不相同；**用户确认两轮语音都听到**（`user_heard=true`），并判断识别结果**基本正确**。登记件：`checkpoints/artifacts/2026-09-30-P8-real-voice-loop/p8-user-heard-confirmation-20260930T135551Z.json`（原始 artifact 未修改，SHA-256 `F3E09B72…0B7D61`）。测试前置：`sounddevice 0.5.6`、`sherpa-onnx 1.13.8`(+core)、`numpy 2.5.3`、`cffi 2.1.1`、`pycparser 3.0` 已离线安装并登记 SHA-256（验收文档 6.1）。
- 真机运行顺带修复的 P6 遗留缺陷（均为真实设备才暴露）：① `sd.default.device` 在 sounddevice 0.5.x 是 `_InputOutputPair`，原 `default_input_device()` 直接 `int()` 抛 `TypeError`；② `CaptureSession.start()` 把字符串设备 id 传给 `sd.InputStream(device="1")`，sounddevice 按名称子串匹配 → `ValueError: No input device matching '1'`（首轮实测 `start_rejected` 的真因），已用 `_device_argument()` 归一化，并让启动失败原因可见；③ `PortAudioError` 现在按设备状态返回而非穿透。runner 另修复控制台 UTF-8、默认设备显示，并新增“未检测到语音即丢弃本轮”的静音幻觉守卫。离线回归合计 +7 项。
- 仍待办：任务链路的真实端到端（需要真实模型的计划提议 = R 支线授权门）；**主程序语音入口已接线**（见下条）。
- **主程序语音入口接线已完成（2026-09-30，用户选择）**：新增 `app/runtime/voice_entry.py`（真实装配 + `VoiceLoopSession` + 静音守卫，runner 与之共用同一份装配）；`app/main.py` 支持 `run(push_to_talk=..., voice_config=..., voice_session_factory=...)` 与 CLI `--voice --tts-mode --device-id --speech-threshold --silence-ms`；默认仍为文字模式，语音不可用即降级文字，`/text` 为用户明确切回且不自动切回。真机冒烟：`python -m app.main --voice --tts-mode none` 识别到真实输入设备与模型、一轮无语音正确丢弃、exit 0。全量 **1306 tests OK (skipped=3)**。交接：`2026-09-30-P8-voice-entry-main-wiring-handoff.md`。
- 真实 DeepSeek Provider 的真实路由与结构化工具提议（R 支线，独立授权门，仍 `NOT READY / STOP`）。
- 只读任务的直执行路径与非确认型任务流。
- 主程序默认运行时接线与默认真人交互开关。
- 生成中逐句播报与首音延迟优化。

## 13. 下一阶段入口

- 下一阶段：**P9（VAD、连续对话、唤醒词与声纹过滤）**，必须另行明确授权。
- 进入 P9 前的最小读取集合：本文件、`../implementation-roadmap.md` P9 节、`../11-vad-and-dialog-state.md`、`../10-wake-word-and-voiceprint.md`、`../shared/safety-boundaries.md`、`app/asr/recovery.py`、`app/voice_loop/**`、`app/runtime/orchestration/**`。

## 14. 下一阶段禁止提前做的事项

- 不得让 VAD/唤醒词/声纹决定授权，不得把误唤醒变成任务或工具调用。
- 不得自动恢复旧 utterance/request/授权，不得在用户键入、查看方案或授权时抢占输入。
- 不得进入 P10 的 AEC/全双工、不得切换默认语音模式、不得接入真实 Provider。
- 不得降低授权等级、不得复用旧授权、不得绕过 `PathGuard`/`PolicyEngine`/复检/账本/审计。
- 不得把语音或 TTS 故障写成任务事实，也不得让摘要改写结构化结果。

## 15. 需要读取的最小文件集合（下一批次）

1. 本文件与 `2026-09-30-P8-integration-acceptance.md`；
2. `../implementation-roadmap.md` P9 节；
3. `../11-vad-and-dialog-state.md`、`../10-wake-word-and-voiceprint.md`；
4. `app/voice_loop/`（5 个模块）与 `app/runtime/orchestration/`（4 个模块）；
5. `app/asr/recovery.py`、`app/asr/push_to_talk.py`；
6. `tests/integration/test_p8_stage_acceptance.py`。
