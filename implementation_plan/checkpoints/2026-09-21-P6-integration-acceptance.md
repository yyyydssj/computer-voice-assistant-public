# P6 阶段级集成验收记录

> 验收日期：2026-09-21  
> 项目：`<PROJECT_ROOT>`
> 验收范围：P6 批次 1–6、Capture → AudioBus → VAD → SenseVoice → ResultGate → Recovery → ConversationManager 按键语音编排，以及 P5 文字安全链兼容性  
> 验收结论：**通过；P6 标记为 `complete`，当前强制停在 P7/TTS 授权门前。**

## 1. 验收目标与边界

本次验收证明一次半双工按键语音轮次能够在受控 fake 设备、fake SenseVoice Provider 和既有 `ConversationManager` 上完成闭环：

```text
CaptureSession
→ AudioBus
→ VadSession
→ SenseVoiceSession
→ ResultGate
→ ConversationManager.send_chat()
```

同时证明：

- 只有 `ACCEPTED_FINAL` 才能产生一次 Agent/Conversation 请求；
- 重复、迟到、旧 boundary、旧 generation、runtime failure 事件不能产生第二次请求；
- startup、session warmup、runtime 失败均保持文字输入可用；
- ASR 故障不拥有任务、计划、授权、执行或结果事实；
- 临时 PCM、临时路径、用户识别文本和异常正文不进入诊断、日志或审计正文；
- 不接入 TTS、连续监听、唤醒词、声纹、AEC、实时打断、语音 L3、真实云端 ASR、真实模型或真实麦克风。

## 2. 正式实现与阶段级接线

### 2.1 正式编排器

新增：`app/asr/push_to_talk.py`

- `PushToTalkCoordinator.start()` 为每次按键轮次建立全新的 `AsrSessionBoundary`、`AudioBus`、`ResultGate`、Capture、VAD 和 Provider session；
- `pump()` 只消费当前 AudioBus，不创建后台线程或定时器；
- `release()` 结束当前 utterance，最多提交一次 final；
- 只有 `ResultGateDecision.ACCEPTED_FINAL` 才调用 `ConversationManager.send_chat()`；
- 正常轮次完成后回到可建立新边界的 `IDLE`，故障轮次由 Recovery 保持不可用并继续文字模式；
- 失败路径关闭 ResultGate、Provider、VAD、Capture、AudioBus，并清理临时音频；
- 诊断 sink 只接收枚举化状态，不携带 PCM、识别文本、路径或异常正文；
- `submit_text()` 复用既有 ConversationManager 文字链，不改变 P3/P4/P5 契约。

### 2.2 故障与终态修复

- `AsrRecoveryController.session_completed()` 关闭成功语音边界、清理本轮 failure 事实并回到 `READY`；
- `ResultGate.close()` 使迟到事件返回 `AFTER_TERMINAL`，重复 event_id 仍优先返回 `DUPLICATE`；
- Provider 加载失败由 `PushToTalkCoordinator.startup_succeeded()` 捕获并转为枚举化 `PROVIDER_UNAVAILABLE`，不会把异常正文暴露给用户或诊断；
- 正常完成后的 coordinator 状态已修复为 `IDLE`，允许下一次按键使用全新边界；
- 故障轮次不会自动重试、切换 Provider、切回语音或补识别预热音频。

## 3. 阶段级集成测试

新增并纳入标准 discovery：`tests/integration/test_p6_integration.py`，共 **6/6**：

1. Capture/VAD/Provider/ResultGate/Recovery/Conversation 一次完整链路只提交一个请求并清理临时音频；
2. 重复、迟到、旧 boundary、旧 generation 和 runtime failure 竞态不创建第二次请求；
3. Provider startup failure 安全降级到文字输入，且异常正文不进入公开状态；
4. startup/warmup failure 后同一 `ConversationManager` 仍可用文字提交；
5. ASR 故障不改变 task/plan/authorization/execution/result 事实；
6. PCM、临时路径、识别文本和异常正文不进入 logger、audit 或 diagnostic sink。

测试只使用 fake Capture backend、fake `sounddevice` stream、fake SenseVoice recognizer、fake Conversation adapter、临时目录和受控审计存储；不访问真实麦克风、网络、`.env`、模型或电脑副作用。

## 4. P6 六项退出条件

| 退出条件 | 仓库内证据 | 结果 |
|---|---|---:|
| 1. Capture/VAD/Provider/ResultGate/Recovery 一次按键语音路径只产生一次可靠 final | `test_capture_vad_provider_gate_recovery_submit_one_request_and_cleanup`；final 只在 `ACCEPTED_FINAL` 分支调用 `ConversationManager.send_chat()` | 通过 |
| 2. final 与故障竞态、重复、迟到、旧 boundary 事件不得创建第二个请求 | `test_duplicate_late_old_boundary_and_fault_race_never_create_second_request`；ResultGate duplicate/after-terminal/stale 边界断言 | 通过 |
| 3. ASR startup/session/runtime 不可用时，文字输入仍能走既有 Conversation/P5 安全链 | Provider startup failure、capture warmup failure、runtime failure 后的 `submit_text()` 集成断言；P1–P5 集成回归同时通过 | 通过 |
| 4. ASR 故障不改变任务、计划、授权、执行或结果事实 | `test_asr_failure_cannot_mutate_plan_authorization_execution_or_result_facts`；故障前后结构化事实哈希一致，执行次数为 0 | 通过 |
| 5. 临时音频按规则清理，不进入 Agent、日志、审计正文 | 临时 PCM/owner 文件清理断言；`test_temp_audio_pcm_and_exception_body_never_enter_diagnostics_logs_or_audit`；识别文本不进入 diagnostic details | 通过 |
| 6. 阶段级集成、全量回归、安全审计和文档完整 | 本文、`P6-handoff.md`、标准 discovery、compileall、UTF-8 扫描和执行面审计均完成 | 通过 |

## 5. 最终验证结果

| 检查项 | 结果 |
|---|---:|
| P6 阶段级集成专项 | **6/6 通过** |
| 全部 integration discovery（P1–P6） | **12/12 通过** |
| ASR 专项（SenseVoice + ResultGate + Recovery） | **29/29 通过** |
| 音频专项（AudioBus + Capture + VAD） | **31/31 通过** |
| Conversation 专项 | **19/19 通过** |
| 标准全量 `unittest discover -s tests -t .` | **345/345 通过** |
| `compileall -q app tests` | **通过** |
| 严格 UTF-8 解码 | **120/120 个 Python 文件通过** |
| UTF-8 替换字符 `U+FFFD` | **0 个文件** |
| `app.asr` 公开导入 smoke | **通过**（显式项目根路径） |
| 新增/修改 P6 执行面审计 | **无 subprocess、socket、requests/urllib/httpx、进程/命令、`.env`、后台线程/定时器或真实音频设备调用** |
| 真实 API、真实模型、真实麦克风、真实电脑副作用 | **未执行** |
| Git add/commit/push | **未执行** |

已知既有编码债务保持不变：`tests/recovery/test_recovery_scanner.py` 与 `tests/security/test_audit_persistence.py` 含 UTF-8 BOM；严格 UTF-8 解码与替换字符扫描仍通过。

## 6. 验收过程中的装配事实

- 直接从非项目根目录运行嵌入式 Python 的 import smoke 会出现 `ModuleNotFoundError: app`；标准 unittest discovery 与 compileall 均从项目根目录执行，公开导入 smoke 通过显式加入项目根路径复核。这是 runner 装配问题，不是产品失败。
- P6 集成测试初次发现正常轮次结束后 coordinator 仍为 `TERMINAL`，导致第二轮 fake backend 不生成；已将正常完成路径改为 `IDLE`，并由 6/6 阶段级测试验证。
- Provider startup failure 初次未在 coordinator 处理；已补齐 `AsrProviderUnavailable → startup_failed(PROVIDER_UNAVAILABLE)` 映射，并新增集成断言。

## 7. 安全与隐私结论

- ASR 只改变输入通道，不取得任务、计划、授权、执行或结果写权限；
- 语音 final 进入同一个 `ConversationManager`，不会绕过 P3 Agent 契约或 P4/P5 安全链；
- 语音 L3 不在本阶段开放；高风险确认仍遵循文字/UI 边界；
- 临时音频仅作为本轮受控资源，成功或失败清理；不进入 Agent、日志、审计正文；
- 所有公开故障事实均为 stage/code/状态枚举，异常正文、路径、PCM 和识别文本不外泄；
- 未新增网络、命令、进程、管理员权限、`.env`、后台监听或真实设备副作用。

## 8. 验收结论与强制停止

**P6 阶段验收通过。** P6 状态更新为 `complete`。

本次只完成 ASR 按键输入和文字 fallback 集成。完成后强制停止在 **P7/TTS 授权门前**：

- 不接入 TTS、Sentence、Playback 或 VoiceProfile；
- 不实现连续监听、唤醒词、声纹、AEC、播放期间监听或实时打断；
- 不开放语音 L3、真实云端 ASR、Windows 语音服务或未经批准 Provider；
- 不创建自动化交互或进入 P8 组合语音闭环；
- 后续必须由用户明确授权 P7 后，才可读取 P7 卡片并开始 TTS 实现。
