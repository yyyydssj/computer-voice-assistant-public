# P8 阶段级集成验收：按键触发的半双工语音闭环

> 日期：2026-09-30  
> 验收范围：P8 批次 1-6（轮次边界、统一输入接缝、闲聊闭环、任务路径编排、“停”三语境、阶段级验收）及其前置阶段 P5/P6/P7 回归。  
> 关联文档：`../implementation-roadmap.md` P8 节、`2026-09-30-P8-batch1-3-voice-loop-chat-closed-loop-stop-handoff.md`、`P8-handoff.md`、`2026-09-30-P7-stage-acceptance-final.md`。

## 1. 授权与范围

- 用户于 2026-09-30 明确要求“继续完成 P8”，在此前已授权批次 1-3 的基础上，本轮执行批次 4-6。
- 全程离线：未联网、未读取 `.env` 或密钥、未启动真实 DeepSeek/VoiceStudio/麦克风/扬声器、未执行真实电脑副作用、未 `git add`/`commit`/`push`。
- 未改动 P5 授权语义；未新增工具、命令面或任意脚本入口。

## 2. 退出条件逐条判定（路线图 P8）

| # | 退出条件 | 判定 | 证据 |
|---|---|---|---|
| 1 | 闲聊：按键 → ASR → Agent → TTS | **通过（离线阶段级）** | 真实 P6 `PushToTalkCoordinator` + 离线 fake 采集/Provider：`test_button_chat_turn_reaches_spoken_reply_once`、`test_button_voice_turn_uses_p8_identity_and_speaks_the_reply`；一次提交、一次播报、轮末回 `idle` |
| 2 | 任务：按键 → ASR → Agent → ActionPlan → 授权 → 执行 → 验证 → 播报摘要 | **通过（离线阶段级）** | `test_l2_task_reaches_verified_execution_and_spoken_summary`、`test_l3_task_requires_the_fixed_phrase_before_execution`、`test_asr_task_route_reaches_the_full_task_chain_and_speaks_a_summary`；真实 `PolicyEngine`/ActionPlan/复检门/账本/受控执行器/验证/审计，结果 `completed`、账本 `COMPLETED`、4 条审计事件齐全、摘要只由结构化事实生成 |
| 3 | ASR/TTS 任一不可用时仍可切换文字，不影响任务事实 | **通过** | `test_asr_unavailable_still_completes_a_task_through_text`、`test_tts_failure_never_changes_the_structured_task_result`、`test_plan_and_result_facts_are_identical_with_or_without_speech`（有无语音时 `plan_hash`、`overall_status`、动作结果与审计事件完全一致）、`test_asr_startup_failure_switches_to_text_without_changing_the_agent`、`test_decode_failure_never_submits_or_speaks_and_text_still_works` |
| 4 | 每轮有独立 session/utterance/request/reply 边界 | **通过** | `test_three_turns_never_reuse_any_identity`（三轮五类身份全不重复）、`test_retired_boundary_cannot_be_reused_or_resubmitted`、`test_foreign_boundary_is_not_accepted_by_the_seam`、`test_registry_refuses_a_second_active_turn`、`test_late_boundary_from_previous_turn_is_not_known` |
| 5 | 说“停”在聊天、授权、执行三种上下文分别符合规则 | **通过** | `test_chat_stop_stops_speech_and_keeps_listening`（停播报后可继续下一轮）、`test_authorization_stop_invalidates_the_window_and_blocks_execution`（窗口作废且不可能产生授权）、`test_execution_stop_cancels_cooperatively_and_never_resumes`、`test_unsafe_atomic_interval_waits_instead_of_force_killing`、`test_stop_before_any_action_attempt_cancels_the_ledger`、`test_late_events_never_revive_playback_execution_or_authorization` |

结论：**五条退出条件全部有可复核的离线证据 → P8 阶段级验收通过**。

## 3. 验证命令与结果（实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
  → Ran 1266 tests, OK (skipped=3)      # 基线 1107 + P8 新增 159
.\.venv\Scripts\python.exe -m unittest discover -s tests/integration -t . -p "test_p8*.py" -q
  → Ran 23, OK                          # 批次 3 集成 6 + 阶段级验收 17
.\.venv\Scripts\python.exe -m unittest discover -s tests/voice_loop -t . -p "test_*.py" -q   → Ran 67, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/orchestration -t . -p "test_*.py" -q → Ran 69, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/asr -t . -p "test_*.py" -q          → Ran 38, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/integration -t . -p "test_*.py" -q  → Ran 35, OK
.\.venv\Scripts\python.exe -m compileall -q app tests tools                                    → exit 0
git diff --check                                                                               → exit 0
```

P8 新增测试分布：`tests/voice_loop/` 67、`tests/orchestration/` 69、`tests/integration/test_p8_integration.py` 6、`tests/integration/test_p8_stage_acceptance.py` 17。

## 4. 安全与边界审计

- **新增/修改文件严格 UTF-8**：无 BOM、无 `U+FFFD`；仓库既有 23 个 BOM 文件本轮未改动（`app/tts/**`、`app/evidence/**`、部分 `tests/**`）。
- **`app/voice_loop/**` 对安全链引用数 = 0**：只导入 `app.agent.adapter`、`app.asr.recovery`、`app.asr.result_gate`、`app.contracts.base`、`app.conversation.manager` 与自身模块；无 `subprocess`/`socket`/`http`/`os`/`pathlib`/`threading`/`ctypes` 等导入，无文件、网络、进程、设备调用。该不变量已固化为测试 `test_voice_loop_never_imports_the_safety_chain`。
- **`app/runtime/orchestration/**` 只编排既有安全链**：无被禁模块导入，源码中无 `subprocess`/`os.system`/`Popen`/`eval(`/`exec(`；已固化为 `test_orchestration_layer_has_no_arbitrary_execution_surface`。
- **不写文件**：`test_voice_layer_creates_no_plan_or_result_files`、`test_documents_are_not_written_to_disk_by_the_orchestrator` 确认编排与语音层不落盘计划、结果或审计文件。
- **对外接口无执行/命令面**：`TaskFlowOrchestrator`、`VoiceStopRouter`、`VoiceInputSeam`、`ChatVoiceLoop` 均有公开 API 结构测试，禁止出现 shell/command/execute/force/retry/rollback 等入口。
- **授权不可复用**：单次 `AuthorizationSnapshot` 仍由 P5 `AuthorizationConsumptionStore` + `ExecutionPreflightGate` 保证；语音与编排层未新增任何绕过路径。

## 5. 前置阶段回归（P5/P6/P7）

| 前置阶段 | 回归证据 |
|---|---|
| P5 安全链 | 任务路径全程复用真实 `PolicyEngine`/`ExecutionPreflightGate`/`ExecutionLedgerRegistry`/`ControlledExecutionRunner`/`ResultVerifier`/`AuditStore`；P5 集成 4/4 与 security 分组长绿；`Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit` 顺序与审计事件序列未变 |
| P6 按键输入 | `tests/asr` 38/38；真实 `PushToTalkCoordinator` 被 P8 闭环复用；唯一改动为 `release`/`accept_provider_event` 增加可选 `turn_id`/`request_id` 透传（默认 `None` 行为不变，+19/-4 行） |
| P7 播报 | 播报仍只在“完整、无故障回复”时发生（`test_agent_failure_is_not_spoken`、`test_speaker_failure_never_changes_the_conversation_fact`）；`speech_eligible` 与 P6 `_speech_eligible` 交叉校验一致 |
| P2/P3 契约与会话 | 全量 1266 通过，`ConversationManager` 路由接缝与 `RouteHandoff` 契约未改动 |

## 6. 明确未做与延期项

1. **真实麦克风 + 扬声器的人工实测**：需要用户在场按键说话并确认听到回复（P6/P7 的真机验证也由用户参与，P8 未在本轮做）。
2. **真实 DeepSeek Provider 的结构化工具提议**：默认运行时为 no-tools 边界，Agent 侧结构化动作提议以注入端口实现并离线验证；真实模型提议仍属 R 支线独立授权门。
3. **只读 `query` 路线的直执行**：P8 任务编排只接受 `task`/`dangerous`（两者都经本地确认）；只读直执行路径按 fail-closed 拒绝（`route_not_supported`），留待后续批次。
4. **主程序接线**：`app/main.py` 尚未把 P8 闭环接到默认运行时；常驻与唤醒属于 P9/P10 范围。
5. **生成中逐句播报**：闭环当前在 `release()` 返回完整回复后整段推入流式会话（会话内部再分句）；P6 自带的增量播报路径未被闭环采用。

## 6. 真机验收 runner（2026-09-30 追加交付）

用户要求提供可按键实测的真机 runner，已交付 `tools/p8_real_voice_loop_runner.py`（`--live-opt-in` 必需）：

- 真实链路：按 Enter 开始 → 真实麦克风采集 → 本地 `sherpa-onnx` SenseVoice → 同一个 `ConversationManager` → 本地脚本 Agent 回复 → 真实 TTS 播报（`ws_pcm`/`http_wav`/`auto`/`edge`，或 `none` 只听识别）。
- 硬边界：无 `--live-opt-in` 立即退出码 2 且**不导入** sounddevice/sherpa-onnx；不创建计划/授权/执行；不操作电脑；不落盘原始音频；识别文本只打印到控制台，artifact 仅记长度与 SHA-256；TTS 仅本机 loopback 或本机离线声线。
- 退出码：0 = 通过；1 = 某轮未提交/未播报；2 = 未 opt-in；3 = 前置检查失败（设备/模型/依赖/本机 TTS 不可用）。
- 离线契约测试：`tests/integration/test_p8_real_runner.py` 11 项（拒绝未 opt-in 且不触设备、参数校验、注入替身环境跑通一轮并校验 artifact 已脱敏、`--check-only` 不写 artifact、环境失败与 ASR 启动失败的退出码、runner 无安全链/进程面、能量检测器行为）。
- **本机现状（阻塞真实按键实测）**：`.venv` 与 `.python312` **均未安装** `sounddevice` 与 `sherpa-onnx`（P6 曾把“真实模型 + 真机设备联合测试”明确列为延期项）。实跑 `--live-opt-in --check-only --tts-mode none` 的现况输出为退出码 3 与提示：
  `缺少音频采集依赖 sounddevice（PortAudio）：No module named 'sounddevice'；请先安装 …`。

### 6.1 依赖安装（2026-09-30，用户批准后离线安装）

`.wheelhouse/` 固定 wheel 与 SHA-256（`pip install --no-index --find-links .wheelhouse`）：

| wheel | SHA-256 |
|---|---|
| `sounddevice-0.5.6-py3-none-win_amd64.whl` | `7F4162F514F007B0BF25A3CCFED3F1705BC2EC311888A90232729EEC4F57A4F4` |
| `sherpa_onnx-1.13.8-cp312-cp312-win_amd64.whl` | `566E49AD3FB2AA8CEB723A52F9809AA6C2EE0A5EB3B0946C3FD2374AF807A5C6` |
| `sherpa_onnx_core-1.13.8-py3-none-win_amd64.whl` | `5579E80196D516E6DAE23C8F629292CE3142AB8869925D32B94612FD86F93733` |
| `numpy-2.5.3-cp312-cp312-win_amd64.whl` | `0A59A421A32580A009E8A1751345BF829631B990DC1794B80514AB722B435DEF` |
| `cffi-2.1.1-cp312-cp312-win_amd64.whl` | `F53E442B08449D42821FA4A4FBA000095AF9F62742A500F978A9F557EC44339A` |
| `pycparser-3.0-py3-none-any.whl` | `B727414169A36B7D524C1C3E31839A521725078D7B2FF038656844266160A992` |

安装后核实：`sounddevice 0.5.6`、`sherpa_onnx 1.13.8`、`numpy 2.5.3`、`cffi 2.1.1` 可导入；项目 own adapter `SenseVoiceRecognizerAdapter(...).load()` → `state=ready`；真实 SenseVoice 对 1 s 静音推理可执⾏（输出为空/非中文字符，见 6.3）。

### 6.2 真机运行发现并修复的缺陷

1. **`sd.default.device` 形态缺陷（P6 遗留）**：`sounddevice 0.5.x` 返回 `_InputOutputPair`（既不是 `tuple` 也不是 `list`），`SoundDeviceCaptureBackend.default_input_device()` 直接 `int(value)` 抛 `TypeError`，导致真机前置检查失败。已在 `app/audio/capture/__init__.py` 增加 `_default_input_index()`：显式支持 `input` 属性与下标访问，无法确认时返回 `None` 并 fail closed；新增 4 项离线回归（`tests/audio/test_capture.py::SoundDeviceDefaultDeviceShapeTests`）。
2. **字符串设备 id 被 sounddevice 当名称匹配（P6 遗留，首次真机按键实测暴露）**：`check_input_settings` 内部把 `"1"` 转成索引，但 `CaptureSession.start()` 把 `check.device_id`（字符串）原样传给 `sd.InputStream(device="1")`，sounddevice 对字符串做**设备名子串匹配**，于是抛 `ValueError: No input device matching '1'`；`PushToTalkCoordinator.start()` 吞掉该异常并只返回 `start_rejected`。用户首次实测 2 轮均为 `start_rejected`（artifact 证据：`p8-real-voice-loop-20260930T134412Z-6ccd6ddde3544761.json`）。已修复：新增 `_device_argument()` 在 sounddevice 边界统一归一化（数字字符串→索引，名称/`None`/整数原样），`check_input_settings` 与 `open_input_stream` 共用；新增 2 项离线回归（`SoundDeviceArgumentNormalizationTests`）。
3. **`PortAudioError` 未按状态处理**：设备不支持 16 kHz/单声道时 `check_input_settings` 抛 `sounddevice.PortAudioError`，它不是 `OSError`/`RuntimeError`，于是穿透 `check_device`。已把设备边界的两个 `except` 放宽为 `except Exception`（保留原有 busy/permission/incompatible/disconnected 文本映射），新增 1 项离线回归。
4. **控制台编码**：SenseVoice 在静音上可能输出非中文（韩文/日文）幻觉字符，GBK 控制台 `print` 会抛 `UnicodeEncodeError`。runner 现在在 `main()` 起始把 stdout/stderr 重新配置为 UTF-8（`errors="replace"`）。
5. **runner 默认设备显示错误**：原实现把设备列表首项当作“系统默认设备”显示；现改为先解析 `default_input_device()` 再查找，缺失即前置失败。
6. **启动失败原因被隐藏**：`PushToTalkStartResult` 新增 `diagnostic`（本地固定代码 + 设备原文），`PushToTalkVoiceDriver` 新增 `last_start_reason`，runner 现在会打印并记录 `start_reason`，不再只给 `start_rejected`。

### 6.3 真机链路验证（非交互）

```text
# 前置检查
--live-opt-in --check-only --tts-mode ws_pcm → exit 0
  输入设备：麦克风阵列 (Realtek(R) Audio)（1）；模型就位；VoiceStudio /health 200

# 真实采集 + 真实 ASR 端到端（临时诊断脚本，3 秒）
recovery_state = asr_ready；capture_started = True
frames = 152（≈50 帧/秒，真实 PCM 在流动）
gate_decision = accepted_final；recognized = 'The.'
→ 结论：链路通；但 **3 秒纯静音被 SenseVoice 幻觉成 'The.' 并被闸门接纳**

# 环境噪声标定（2.5 秒静音）
frames = 122；ambient_rms p50 = 47.7 / p95 = 124.0 / max = 150.0
→ 默认 --speech-threshold 600 对语音有充足余量（约为环境最大值的 4 倍）

# runner 真机非交互一轮（无语音）
--live-opt-in --turns 1 --tts-mode none --speech-timeout-seconds 3 → exit 1
  「未检测到语音（能量始终低于阈值）：本轮丢弃，不提交、不播报」+ 阈值提示
```

**新增静音守卫**：runner 在 `release()` 之前检查是否检测到语音；未检测到就 `cancel_turn` 丢弃本轮，**不提交、不播报**（否则静音幻觉会被当真实请求送进 Agent）。新增 1 项离线回归（`test_silence_only_turn_is_discarded_without_submitting_or_speaking`）。

> 产品链路的同类风险（静音/噪声导致幻觉被接纳）属于 P9 的 VAD 范围；P8 按键阶段的验收 runner 以能量阈值 + 丢弃守卫处理，并在 artifact 中记录 `speech_seen`。

仍在进行中：需要用户本人在终端按键说话的 2 轮真实实测（runner 会逐轮打印识别文本，结束时询问是否听到）。

#### 6.3.1 真实按键实测结果（2026-09-30，用户本人执行）

命令：`tools/p8_real_voice_loop_runner.py --live-opt-in --turns 2 --tts-mode ws_pcm`
artifact：`artifacts/2026-09-30-P8-real-voice-loop/p8-real-voice-loop-20260930T135551Z-85388f3ce1264d88.json`
（SHA-256 `F3E09B72DBA2F31D6D44A4BB8410502A63110189C73E22EAC586B02BDA0B7D61`，2666 字节，未修改）

| 轮次 | 帧数 | 检测到语音 | 结束方式 | accepted / submitted / spoken | ASR 文本 | TTS（VoiceStudio WS） | 用时 |
|---|---:|---|---|---|---|---|---:|
| 1 | 172 | 是 | 再按 Enter（button_release） | true / true / true | 10 字符（仅存 SHA-256） | `voicestudio_ws_pcm`，2 句播放 / 2 句完成，无错误 | 12.12 s |
| 2 | 202 | 是 | 静音自动结束（silence_timeout） | true / true / true | 12 字符（仅存 SHA-256） | `voicestudio_ws_pcm`，3 句播放 / 3 句完成，无错误 | 14.59 s |

- 两轮的 `voice_session_id / utterance_id / turn_id / request_id / reply_id` **全部不同** → 真机上验证“每轮独立边界”。
- 未检测到语音丢弃 0；`start_rejected` 为 0（采集缺陷已修复）。
- 用户听感确认（`p8-user-heard-confirmation-20260930T135551Z.json`，未改动原始 artifact）：**两轮语音回复都听到**，`user_heard=true`。
- 用户对识别质量的判断：**基本正确**（与实际所说一致或仅差个别字），`asr_accuracy=basic_correct`。
- 边界如实登记：这是操作员听感判断，**不是声学测量**；Agent 为本地脚本回显替身（非真实模型）；本轮不涉及任务链路与任何电脑副作用；识别文本未落盘。

> 由此，路线图 P8 退出条件 1（闲聊：按键 → ASR → Agent → TTS）在**真机 + 真人听感**层面成立（模型替身已明确标注）。退出条件 2 的任务链路仍为离线阶段级证据。

#### 6.3.2 真机声学回环（2026-09-30，完整 `--voice` 单轮）

用本机离线声线朗读「你好，测试一下。今天天气怎么样？」→ 真实麦克风采集 → 真实 SenseVoice → 同一个 `ConversationManager` → 真实默认 Agent（**未接模型**）。原始输出：`artifacts/2026-09-30-P8-real-voice-loop/p8-loopback-demo-output.txt`。

```text
设备：麦克风阵列 (Realtek(R) Audio)（1）；模型：models/sense-voice/model.int8.onnx
[播放] chunks=26 state=COMPLETED error=None 用时 5.70s
帧数=274 检测到语音=True 结束方式=silence_timeout 用时=6.191s
accepted=True submitted=True spoken=False reason=text_only
识别结果='你好，测试一下今天天气怎么样。'      # 与朗读内容一致（仅少一个句内标点）
回复文本=None；播报记录=[]
```

- **识别链路真机成立**：真实采集 274 帧（≈5.5 s）、能量阈值判定有语音、真实 SenseVoice 识别准确。
- **提交正常**：`accepted/submitted = True`，识别文本经 ResultGate 进入同一个会话入口。
- **不播报是设计行为**：真实 Agent 未接模型（`completed=False`），P8 只播报“完整且无故障”的回复，因此不播报、也不伪造内容；播报链路本身已由 6.3.1 的真人实测证明。
- 结论：`app.main --voice` 的完整单轮在真机上跑通；“听得到回复”只差 R 支线真实模型授权。

## 7. 停点

- P8 阶段级验收通过；阶段状态更新为 `complete`。
- 强制停点：不进入 P9（VAD 连续对话、唤醒词、声纹）、不做 AEC/全双工、不切换默认语音模式、不接入真实 Provider、不提交。
- 下一步入口见 `P8-handoff.md` 第 12/13 节。
