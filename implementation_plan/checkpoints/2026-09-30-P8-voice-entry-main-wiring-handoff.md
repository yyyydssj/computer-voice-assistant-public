# P8 语音入口主程序接线交接（2026-09-30）

> 前置：`P8-handoff.md`、`2026-09-30-P8-integration-acceptance.md`（含真机实测 6.3.1）
> 授权：用户 2026-09-30 选择“主程序语音入口接线”作为 P8 之后的下一步（此前该项列为延期）。

## 1. 目标与产出

把已在真机验证过的按键语音闭环接进默认运行时，使 `python -m app.main --voice` 即可按键说话；
同时把 runner 里的真机装配逻辑提升为**唯一一份**可复用实现，避免两处漂移。

| 文件 | 变更 |
|---|---|
| `app/runtime/voice_entry.py` | **新增**：`VoiceEntryConfig`（本地参数，严格校验）、`energy_detector`、`VoiceEntryEnvironment`、`build_real_environment`（真实采集/ASR/TTS）、`VoiceLoopSession`（装配 + `run_turn()` + `close()`）、`VoiceTurnFacts`（含 `boundary_facts()`/`log_details()`）、`StreamingSpeaker`/`EdgeSpeaker` 等适配器、`NO_SPEECH_REASON` 静音守卫 |
| `app/main.py` | `run()` 新增 `push_to_talk` / `voice_config` / `voice_session_factory`；新增 `_run_push_to_talk()`、`_render_turn()`、`_voice_unavailable()`；CLI 支持 `--voice`、`--tts-mode`、`--device-id`、`--speech-threshold`、`--silence-ms` |
| `app/voice_loop/chat_loop.py` | `VoiceDriverTurnResult` 新增 `accepted_text`（仅被接纳时携带，长度受限） |
| `app/voice_loop/drivers.py` | `PushToTalkVoiceDriver` 新增 `last_result`，`release()` 记录被接纳的 final 文本 |
| `tools/p8_real_voice_loop_runner.py` | 改为复用 `voice_entry`（装配减半）；保留自身 CLI、prompt、artifact 与听感登记；对外名字 `Environment`/`build_environment`/`energy_detector`/`EXIT_*` 保持不变 |

## 2. 行为边界（与既有规则一致）

- **默认仍是文字模式**：`--voice` 才启用语音；不改变 `TTS_VOICESTUDIO_MODE=disabled` 等设置，`--tts-mode` 只影响本次运行。
- **语音不可用即降级文字**：设备、模型、依赖或 ASR 启动失败时打印具体原因并进入文字壳；不自动切换 Provider，也不擅自重试。
- **`/text` 是用户明确请求**：切回文字输入后不会自动切回语音；`/exit` 或输入结束（EOF）退出。
- **未接真实模型如实说明**：运行时未注入 Agent 时打印“未接入真实模型…不会伪造内容”，语音轮次仍会识别并提交给同一个会话，回复如实显示不可用。
- **静音不提交**：能量阈值内无语音就丢弃本轮（不提交、不播报），避免 SenseVoice 静音幻觉进入会话。
- **不碰安全链**：不创建计划/授权/执行，不调用工具；任务路由需要真实 IntentRouter（当前仅 `LocalChatRouter` → chat），任务能力仍由 P8 任务编排与 R 支线负责。
- **日志脱敏**：结构化日志只记 `voice_entry_ready` / `voice_turn`（`log_details()`：计数、状态、原因码、识别文本长度）/ `voice_entry_unavailable` / `voice_entry_switched_to_text`；不记识别文本。

## 3. 验证（实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
  → Ran 1306 tests, OK (skipped=3)          # P8 语音入口新增 21 项
.\.venv\Scripts\python.exe -m compileall -q app tests tools  → exit 0

# 新增测试
tests/integration/test_p8_voice_entry.py   11 项：真实 P6 管道 + 离线替身环境的会话级
   轮次（提交/文本/回复/播报）、静音丢弃、每轮新边界、ASR 启动失败、关闭幂等、
   日志脱敏字段、能量检测器、配置校验
tests/foundation/test_main_push_to_talk.py 10 项：回车驱动一轮、未接模型提示、
   /text 切回文字且不自动切回、语音不可用降级文字、注入工厂不会构建真实环境、
   静音丢弃显示、EOF 退出、默认不开语音、CLI 参数校验

# 真实主程序冒烟（临时数据根，避免污染用户数据目录）
$env:ASSISTANT_DATA_ROOT=<temp>; "" | python -m app.main --voice --tts-mode none
  → 输入设备=麦克风阵列 (Realtek(R) Audio)（1）；ASR 模型=sense-voice/model.int8.onnx
  → “语音模式已就绪…”+“未接入真实模型…”；一轮无语音 → 丢弃且未提交未播报；exit 0
```

runner 契约测试在重构后仍为 12/12。

## 4. 已知限制与下一步

1. **真实模型仍未接入**：Agent 为运行时注入点（默认 `UnavailableAgentAdapter`），真实 DeepSeek provider adapter 属 R 支线授权门（`NOT READY / STOP`）。
2. **任务链路未接默认运行时**：`LocalChatRouter` 只产生 chat 路由；把 `TaskFlowOrchestrator` 接进主程序需要真实计划提议（同样在 R 支线授权门后）。
3. 语音入口为**前台按键模式**；后台常驻、唤醒词、连续收音属 P9/P10。
4. 未提交：本轮及此前 P7/P8 改动仍在工作区。

## 5. 建议的下一步

- 你可以在终端实跑按键对话：`.\.venv\Scripts\python.exe -m app.main --voice`（默认 `ws_pcm`，VoiceStudio 需运行；无模型时回复会显示不可用）。
- 若要真正对话，需要单独授权 R 支线（真实 Provider：模型/版本、凭据、网络、预算、日志脱敏逐项确认）。
- 或先整理提交本轮全部改动。
