# P7 T-06 阶段验收记录（2026-09-28）

**结果：P7 阻塞／未完成；验收停点为 P7，不进入 P8。** 已完成 T-02 的离线 fake 接缝验证；T-05 的离线故障子集通过但完整降级门阻塞，T-03/R-24 真实能力门评审；T-04/R-25/R-26 因前置条件不足阻塞。历史 2026-09-27 T-01/G0 为范围基线，不是当前设备测试。参考 [本地集成与故障交接](2026-09-28-P7-T02-T05-local-integration-handoff.md)、[真实 TTS 门](2026-09-28-P7-T03-T04-real-tts-gate.md)、[DeepSeek 路由门](2026-09-28-P7-R24-R26-real-route-gate.md)。

| 路线图 P7 退出门 | 本轮事实 | 验收 |
|---|---|---|
| 1. 文字按句原序**实际**播放 | FakeDevice 两句 FIFO、request/profile 身份及回执覆盖；`app/main.py` 无接入，真实服务/扬声器回执及听感观察均为 0 | 未通过 |
| 2. 设备提交前切 Edge、提交后不换声/旧回复不续播 | 假设备提交歧义下拒绝同句 Edge；失败只允许下一轮用户选中 Edge；当前句安全切换路径未实现/未实测；无真设备撤销证据 | 未通过 |
| 3. TTS 失败保留文字、任务事实 | 新模块不引用安全链，假合成/设备失败不丢原文；未接主界面/真实故障，不能据此证明最终可用性 | 未通过 |
| 4. VoiceProfile 更新仅许可边界 | 本地 USER、选择、版本漂移测试通过；无试听确认回执/真实声音和多线程端到端观察 | 未通过 |
| 5. 阶段级回归/故障/环境/隐私 | 本轮全量 692/692、compileall、fake 故障；真实服务规格、格式与可听/设备回执缺失 | 未通过 |

## 回归、版本与预算

- 执行：`<PROJECT_ROOT>\.python312\python.exe -m unittest discover -s tests -t . -q`：**692 tests / OK**。覆盖 P2/P3/P5 既有回归集合但未逐一实机复查；`-m compileall -q app tests`：成功。新专项 14 tests / OK。运行环境：Windows PowerShell、项目嵌入 Python 3.12；只使用标准库接口，无新包安装。`git diff --check` 成功；新文件为 untracked，已另行逐一检查路径与内容。
- 实际预算：DeepSeek 请求 0、VoiceStudio 请求 0、Edge 请求 0、设备播放/录音 0，费用 0；仅读源文件并枚举设备名称/状态，未读任何凭据值，不采集私人音频/正文，不输出密钥或原始模型响应。
- 停止条件：未确定的 VoiceStudio 产品/接口、有效可核验账号和限额、用户预先试听 Edge 声音、设备 ID/格式/停止回执、用户听感；任意不满足则停文字且不宣称 P7 complete。提交后设备 stop 未确认的实例禁后续语音；不存在绕过 current-segment 的自动 Edge 重播。真实 Provider/硬件或多线程接入需要隔离验证并以独立证据升级此验收表。
- 保持默认 Provider `local`、DeepSeek runtime/agent/transport NOT READY；P8、ASR/全双工与电脑工具副作用完全独立，未启动。既有工作区 staged/untracked 是先前基线，未做批量暂存、commit/push/reset。此文档记录失败验收，**没有批准 P7 complete**。

> **原文损坏（2026-09-30 发现）**：本段原始中文已被替换为 ASCII `?`，逐字不可恢复；以下为按证据重写，紧接其后的代码块为损坏原文备审计。

## 2026-09-28 持久化温和音色选择与真实试播（按证据重写）

本段原文中文已不可恢复；以下为按现存代码、测试、artifact 与相邻完好文档重写的摘要，**不是原文**。

- 真实服务事实：本机 VoiceStudio `/health` HTTP 200、版本 `0.5.6`、profile `e8572e8e`；本地 WAV 与 provider reference audio 身份核对通过；Windows 播放设备可用。
- 选择文件锚点：schema `version=2`、`speed=0.80`、`pause_ms=400`、`fade_ms=80`。
  - 口径冲突（待对账，禁止静默改写）：当前 `app/tts/private_selection.py` 为 `DEFAULT_SPEED = 1.0` 且 `validate()` 只接受该值；设计文档 `docs/superpowers/specs/2026-09-28-xiaobing-voice-integration-design.md` 写 `0.85`；本文件原文锚点为 `0.80`。三处不一致属状态漂移，需专门对账批次确认真实生效值。
- 当轮播报事实：2 句回复、2 次播放、`text_fallback=false`；属受限真实预览，**不等于** Agent 默认链路出声。
- 当轮证据：全量 **719 tests / OK**；`compileall`、UTF-8 检查、`git diff --check` 与 staged diff 检查通过。
- 当轮遗留：真实 Windows 设备的 stop ACK 未证明；`app/main.py` 与 Agent 回复出口未接线；Edge 无播放实现（无 Edge 兜底）；VoiceStudio / 真实播放设备故障注入、同句安全切换、独立声学与用户听感证据均缺失。
- 结论：T-04/T-05/T-06 未通过；**P7 未 complete**，未进入 P8。

```text
## 2026-09-28 ?????????????????

?????????????????????????

- VoiceStudio `/health` ?? HTTP 200??? `0.5.6`?????? profile `e8572e8e`????? WAV ? provider reference audio ??????Windows ?????????
- ?????????????schema `version=2`?`speed=0.80`?`pause_ms=400`??? `fade_ms=80`????????
- ???????? 2 ?????? 2 ??????`text_fallback=false`???????????????????????????
- ????????? **719 tests / OK**??? `compileall`?UTF-8 ????? diff ?????

**?????** ?????? VoiceStudio ??????????????????? stop ACK?????? `app/main.py` ? Agent ????????????? Edge ?????????/??/????????? T-04/T-05/T-06 ?????/?????P7 ???? complete?P8 ????
```

## 2026-09-28 真实故障矩阵验收口径补充（本轮审查）

本补充不改变默认 runtime，不接入 `app/main.py`，也不把离线测试或一次成功预览升级为 P7 完成证据。

### 证据等级与当前结论

| 等级 | 真实对象 | 当前证据 | 结论 |
|---|---|---|---|
| L0 | fake provider / fake player / fake PlaybackWorker / fake Agent outcome | `tests/tts/test_real_failure_matrix.py`、`test_playback_worker.py`、`test_real_playback.py`、`test_completed_reply.py`、`tests/foundation/test_main_voice_opt_in.py` | **Pass（仅本地契约）** |
| L1 | 真实 VoiceStudio 服务 | 已有健康检查、profile/reference identity 和受控成功预览记录；真实 timeout、HTTP error、坏 WAV、超限响应尚无受控故障注入证据 | **部分证据；故障矩阵 Blocked** |
| L2 | 真实 Windows waveOut/扬声器 | 代码已区分 `STOP_ACKED`、`STOP_TIMEOUT`、`UNKNOWN`，但尚无完整真实 `waveOutReset`/`WOM_DONE` 证据包，也无独立声学/听感证据 | **Blocked** |
| L3 | 真实 Agent → VoiceStudio → 设备 | 当前 `app/main.py` 仍是文字壳；仅有注入式 fake Conversation/Agent seam | **Not implemented / Blocked** |

### 当前可运行测试与正式口径

- 普通离线门：`python -m unittest discover -s tests -t . -q`；当前结果应以本次命令输出为准。
- 真实证据门：新增 `tests/tts/test_real_failure_matrix_live.py`，默认跳过；只有显式设置 `P7_RUN_REAL_TTS_EVIDENCE=1` 才会访问本地 VoiceStudio 并使用真实 Windows 输出设备。
- live 测试只证明两件事：固定非敏感文本可生成受边界约束的 WAV；真实 waveOut 路径返回 backend `COMPLETED`。`user_heard` 必须仍为 `None`，不得由 backend completion 推断听感。
- live 成功基线不覆盖真实故障注入。Provider timeout/HTTP error/坏 WAV/超限、设备打开失败、`waveOutReset` timeout/unknown，必须通过受控 fault proxy、服务测试模式或可复核设备故障装置执行；未具备这些条件时登记 **Blocked**，不得用 `mock` 或故意抛异常冒充真实证据。

### 正式故障矩阵验收口径

| Case | 注入/环境层 | Pass 条件 | 当前状态 |
|---|---|---|---|
| F-01 Provider timeout / `URLError` | fake；真实层需 fault proxy | 原文完整保留；无设备提交；错误正文/凭据不入日志 | L0 Pass；L1 Blocked |
| F-02 HTTP exception / 非 200 / 错误 Content-Type | fake；真实层需 fault proxy | Provider 响应被拒绝；无坏音频进入设备 | L0 Pass；L1 Blocked |
| F-03 坏 WAV / 截断 WAV / 超限音频 | fake；真实层需 fault proxy | 设备提交前拒绝；`playback_calls_returned == 0` | L0 Pass；L1 Blocked |
| F-04 合成前取消 | fake | `error_code=cancelled_before_submission`、`received_audio=0`、无设备提交、无 stop ACK | Pass |
| F-05 全部准备后提交前取消 | fake | `received_audio` 可大于 0；提交数为 0；后续句不提交 | Pass |
| F-06 当前播放期间取消 | fake/backend；真实层需 waveOut 实测 | 后续句不提交；明确 ACK 才记 `STOP_ACKED`；timeout/unknown 不伪造 ACK | L0 Pass；L2 Blocked |
| F-07 设备异常 / stop timeout / unknown | fake/backend；真实层需真实设备故障 | 实例 fail-closed；禁用未来语音；文字保留；任务事实不变 | L0 Pass；L2 Blocked |
| F-08 后续句段不提交 | fake/backend；真实层需设备事件记录 | 设备状态不确定后，任何后续句不得提交；“未提交”不等于“未合成” | L0 Pass；L2 Blocked |
| F-09 Agent 失败/不完整 outcome | fake Agent；真实层需受控真实 Agent | 不播；文字仍输出；任务/授权/账本/执行事实不变 | L0 Pass；L3 Blocked |

### 必须同时记录的事实字段

每个真实 case 至少记录：

```text
case_id
provider_name_and_version
profile_id（可脱敏）
input_sentence_count
synthesized_segments
received_audio
prepared_segments
submitted_segments
playback_calls_returned
not_submitted_segments
playback_state
stop_status
stop_confirmation
voice_disabled
text_fallback
original_text_preserved
user_heard_confirmation
agent_or_task_facts_changed
log_privacy_check
test_command
evidence_artifact
Pass / Blocked / Not Applicable
```

其中：

- `STOP_ACKED`/`WOM_DONE`/buffer returned 只证明 backend 协议事实；
- `user_heard_confirmation` 必须由用户明确确认或独立声学测量产生；
- `received_audio` 表示已从合成器取得并进入准备阶段的音频段数，不表示设备已播放；
- `submitted_segments` 与 `synthesized_segments` 必须分开，避免把“未提交”误写成“未合成”。

### 本轮验收结论

```text
Fake/offline failure matrix：Pass
Fake stop ACK/timeout/unknown 语义：Pass
Fake Agent opt-in seam：Pass
真实 VoiceStudio 成功基线：已有有限证据；可用新增 live gate 复核
真实 VoiceStudio 故障矩阵：Blocked（无受控 fault injector）
真实 Windows 设备 stop ACK：Blocked（缺正式 waveOut 证据包）
真实 Agent → VoiceStudio → 设备默认端到端：Blocked（当前未接入）
P7：Blocked，不进入 P8
```
