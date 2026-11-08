# P7 采集与用户听感确认判断交接

- 日期：2026-09-29
- 阶段：P7
- 当前结论：**新批次 `p7-acf9735565a64cac` 已完成真实 Agent → VoiceStudio → Windows 播放链路，且用户听感已精确绑定；停止判定仍未通过，因此 P7 仍为 Blocked / incomplete。**
- 下一步：保留该批次的精确用户听感证据；如需完成 P7 全部收尾，还需建立同批 `backend_stop_ack=true` 与 loopback/acoustic/speaker stop 闭合证据。

## 1. 本次交接范围

本交接只覆盖以下两项：

1. **采集判断**：确认 loopback 与独立 microphone 采集是否有效，并分别判断播放后的输出是否已经安静。
2. **用户听感判断**：只接受用户明确指向同一个精确 `batch_id` 的确认；不得从 `COMPLETED`、`WOM_DONE`、WAV 存在或采集有效自动推导 `user_heard=true`。

不在本交接范围内：

- 不修改历史 artifact；
- 不跨 batch 回填停止字段或 `user_heard`；
- 不修改 VoiceStudio 配置；
- 不读取、修复或复用 `<USER_HOME>\.dsh`；
- 不执行 git stage/commit/push/reset；
- 不进入 P8；
- 不把项目内“音频播放无需逐次授权”扩大解释为任意系统操作授权。

## 2. 最新真实批次

### 2.1 Artifact

```text
<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-agent-tts-device\p7-real-agent-tts-device-20260929T172455Z-e9bb628fd56c.json
```

### 2.2 身份

```text
ledger batch_id: p7-834ae883fba14bad
request_id: req-c536455a97fb4bb682cd
session_id: session-79f6491f3a06439cab30
```

本批的 batch 身份必须以 artifact 顶层 `batch_id` 为准，即：

```text
p7-834ae883fba14bad
```

### 2.3 已建立的证据

| 检查项 | 结果 | 说明 |
|---|---|---|
| Agent 请求 | PASS | `AGENT_REPLY_ACCEPTED` 已记录 |
| Agent 回复 | PASS | `metadata.agent_reply_text` 已记录 |
| VoiceStudio 合成 | PASS | `VOICESTUDIO_AUDIO_ACCEPTED` 已记录 |
| 音频 SHA-256 | PASS | `9e9cbcb75323e914f101ce48d1d622c0968a4f5de1789dc45dfabb0d5454015a` |
| WAV | PASS | 单声道、16-bit、24 kHz，58320 frames |
| Windows 播放 | PASS | `PLAYING → COMPLETED` |
| loopback 读满 | PASS | 采集有效、读满窗口、清理成功 |
| microphone 读满 | PASS | 采集有效、读满窗口、清理成功 |
| 用户听感 | PENDING | 当前批 `user_heard=null` |

## 3. 当前采集判断

当前 artifact 字段：

```text
capture_valid              = true
backend_stop_ack           = null
loopback_output_stopped    = false
acoustic_output_stopped    = false
speaker_output_stopped     = false
user_heard                 = null
status                     = blocked
```

对应原因：

- loopback 尾段 `post_rms_dbfs ≈ -18.86 dBFS`，未达到 `quiet_dbfs=-55.0 dBFS`；
- microphone 尾段 `post_rms_dbfs ≈ -30.36 dBFS`，未达到 `quiet_dbfs=-55.0 dBFS`；
- 当前采集可以证明窗口读满和音频活动存在，但不能证明播放后已经安静；
- `COMPLETED` 只能表示播放 worker 认为播放完成，不能替代 backend stop ACK 或物理静音证据；
- `speaker_output_stopped` 必须由 loopback 与 acoustic 两个独立正向判断共同建立，任何一个为 `false` 时保持 `false`。

因此本批判断为：

```text
采集窗口：有效
播放后停止：未建立
用户听感：待确认
P7：Blocked / incomplete
```

## 4. 用户听感确认规则

### 4.1 可以接受的确认

用户必须明确表达已经听到完整播放，并且确认能够唯一绑定到当前 batch。推荐确认格式：

```text
听到了完整播放，batch_id=p7-834ae883fba14bad
```

如果用户只说“听到了完整播放”，只有在当前对话中刚刚播放且没有并行/后续播放批次产生歧义时，才可由执行者将其绑定到当前明确的 batch；写入 artifact 时仍必须保存：

```json
{
  "user_heard": true,
  "confirmed_batch_id": "p7-834ae883fba14bad",
  "confirmation_text": "用户原文",
  "confirmed_at": "带时区的时间",
  "confirmation_source": "user"
}
```

### 4.2 不可以接受的推导

以下事实都不能自动设置当前批的 `user_heard=true`：

- `playback_state=COMPLETED`；
- `WOM_DONE` 已观察到；
- WAV 文件存在或 SHA-256 已计算；
- loopback/microphone `capture_valid=true`；
- 另一批次已经有 `user_heard=true`；
- 用户确认的是另一个 `batch_id`；
- 用户只确认“程序运行了”或“播放结束了”，没有确认听到完整声音。

## 5. 下一步执行顺序

### A. 先做同批采集判断

1. 使用新的、独立的真实 Agent batch；不得重用本批或覆盖历史 artifact。
2. 使用全新的临时 `DSH_HOME`，不使用 `<USER_HOME>\.dsh`。
3. 保持 Agent、VoiceStudio、Windows 播放的 request/session/batch identity 一致。
4. 使用独立 WASAPI loopback 与独立 microphone 采集；保留端点元数据、窗口参数、RMS、`post_quiet`、清理结果，不保留原始 PCM。
5. 分别判断：
   - `loopback_output_stopped = loopback.valid and loopback.post_quiet`
   - `acoustic_output_stopped = microphone.valid and microphone.post_quiet`
   - `speaker_output_stopped = loopback_output_stopped and acoustic_output_stopped`
6. 若任一采集依赖缺失、读帧不足、尾段不安静或清理失败，则对应字段保持 `false` 或 `null`，并保持 fail-closed。
7. 若自然播放完成但没有 backend STOP ACK，不得伪造 `backend_stop_ack=true`；当前实现下应继续记录 `null`，除非同批取得可复核的真实 ACK。

### B. 再做用户听感确认

1. 播放完成后向用户报告当前精确 `batch_id`，不要使用旧批次 ID。
2. 等待用户明确确认是否听到完整播放。
3. 只在用户确认当前精确 batch 后，将该 batch 的 `user_heard` 记录为 `true`。
4. 若用户没有确认、确认不明确或确认了其他 batch，保持 `user_heard=null`。
5. 更新时只能写入同一个 batch-specific artifact，不创建替代批次，也不修改历史批次字段。

### C. 最终判断

只有在以下条件都满足时，才能把这一批的用户听感判断记为通过：

```text
精确 batch identity 一致
+ Agent 回复成功
+ VoiceStudio 音频成功
+ Windows 播放完成
+ 用户明确确认听到该精确 batch 的完整播放
```

如果还要宣称“设备停止闭合”，还必须额外满足：

```text
backend_stop_ack = true
+ loopback_output_stopped = true
+ acoustic_output_stopped = true
+ speaker_output_stopped = true
```

否则结论应保持：

```text
Real chain established, user-heard judgment may be established per batch,
stop closure incomplete, P7 remains Blocked / incomplete.
```

## 6. 现有相关批次边界

以下批次不得相互合并：

- `p7-direct-audio-playback-20260929T085013Z-d29742a149c7`：用户已确认听到完整播放；只属于该直接播放批次。
- `p7-834ae883fba14bad`：本交接的最新真实 Agent 批次；当前 `user_heard=null`，采集停止字段未闭合。
- 旧的 Agent/VoiceStudio/设备批次：只保留其自身证据，不跨批次回填。

## 7. 交接完成标准

下一位执行者完成本交接后，应至少留下：

1. 一个新的 batch-specific artifact；
2. Agent、VoiceStudio、播放、采集和用户确认的同批 identity 对照；
3. loopback/microphone 的独立判断及原始诊断摘要；
4. 精确 `batch_id` 的 `user_heard` 结果；
5. 若仍 blocked，明确列出未闭合字段和下一步，不得用其他批次补齐；
6. 一份新的 checkpoint/handoff，记录实际命令、结果、未解决问题和继续入口。

## 8. 当前结论

```text
P7 real Agent → VoiceStudio → Windows playback: established
P7 capture validity: established for latest batch
P7 post-playback silence/stop judgment: not established for latest batch
P7 user-heard judgment: pending for latest batch
P7 overall: Blocked / incomplete
P8: Not entered
```

## 9. 2026-09-29 只读诊断补充（未重试合成/播放）

针对最新失败批次 `p7-c084c4e4b03e435b` 的 `voicestudio_call_failed:LocalPortError`，本次只进行了只读诊断，没有再次调用 Agent、VoiceStudio `/generate`、Windows 播放或录音，也没有修改 VoiceStudio 配置。

### 9.1 已观察到的事实

- 失败批次时间窗口：Agent 回复在 `2026-09-29T10:01:37.806846Z`（北京时间 `18:01:37.806846`）接受；批次在 `2026-09-29T10:02:22.833634Z`（北京时间 `18:02:22.833634`）因 VoiceStudio `LocalPortError` 阻塞。
- VoiceStudio 进程/后端在北京时间 `18:01:37` 左右重新启动；本地日志记录了：`Importing PyTorch & VoiceStudio runtime`、`Loading VoiceStudio model on device: cuda`，之后没有记录该次启动完成或新的 `render trace`。
- `GET http://127.0.0.1:3900/health` 返回 HTTP 200，版本 `0.5.6`；这只能证明 HTTP 健康端点可达，不能证明 TTS 模型已经 ready。
- 只读查询 `GET /model/status` 在北京时间 `18:06:10` 至 `18:07:05` 仍为：`status=loading`、`sub_stage=loading_weights`、`loaded=false`、`loading=true`。
- 只读查询 `/model/loaded` 返回 `models=[]`、`count=0`；`/engines/tts` 报告 active engine 为 `omnivoice`，但 `execution_evidence.evidence_state=not_loaded`。
- `/system/diagnose` 明确警告：RTX 3050 仅 4.0 GB VRAM，而 OmniVoice 约需 6 GB；诊断提示应预期生成变慢并可能超时。
- VoiceStudio 最近一次成功生成日志为北京时间 `17:25:00`；失败批次之后没有发现新的成功生成输出。该成功生成发生在此前模型已完成加载的时段。

### 9.2 对 LocalPortError 的边界判断

`LocalVoiceStudioClient` 的 `_request()` 会把 HTTP 传输异常、超时和操作系统错误统一映射为 `LocalPortError("transport_failed")`；当前 artifact 只保存了异常类型，没有保存 `LocalPortError.code`，因此不能把具体 code 追溯性地写入历史 artifact。

结合失败时间与 VoiceStudio 正处于 `loading_weights`、`loaded=false` 的证据，当前最强可复核解释是：本次 `/generate` 请求发生在 VoiceStudio 后端模型加载窗口内，客户端等待超时/传输失败；不是已证明的 HTTP 业务错误，也不是已证明的 WAV 格式错误。该结论是基于时间线和只读状态的诊断推断，不替代未保存的原始异常 code。

### 9.3 当前 P7 判定

```text
p7-c084c4e4b03e435b:
  Agent reply              = established
  VoiceStudio synthesis    = not established (request failed while model loading)
  Windows playback         = not attempted
  loopback/microphone      = not attempted
  user_heard               = null
  status                   = blocked
```

不得把本次失败批次与此前成功 VoiceStudio/设备批次合并，也不得因为 `/health=200`、`/engines/tts` 可用、或历史成功输出存在而回填本批 `audio_sha256`、播放字段或 `user_heard`。

### 9.4 继续入口

只有在不修改配置的前提下，重新只读确认 VoiceStudio `model/status` 已进入 ready/loaded 状态后，才可评估是否有必要发起下一次真实批次。若再次运行，必须使用新的 batch/request/session、全新的临时 `DSH_HOME`，并继续保持同批 identity；在用户明确确认精确当前 `batch_id` 前，`user_heard` 必须保持 `null`。

P8 仍未进入；未执行 git stage/commit/push/reset。

## 10. 2026-09-29 收尾尝试：VoiceStudio readiness 仍未闭合

用户要求继续收尾后，先按安全顺序进行只读 readiness 检查，未在模型未就绪时创建新的真实 batch。

### 检查窗口

- 检查开始：2026-09-29 18:12:56（Asia/Shanghai）
- 检查结束：2026-09-29 18:17:47（Asia/Shanghai）
- 持续时间：约 5 分钟

### 结果

```text
GET /health          = 200，VoiceStudio 0.5.6
GET /model/status    = status=loading
                       sub_stage=loading_weights
                       loaded=false
                       loading=true
GET /model/loaded    = models=[]，count=0
GET /system/errors/recent = errors=[]
```

期间未观察到模型完成加载。进程仍在响应，3900 端口仍监听，但 `/health=200` 不能替代 TTS 模型 ready；本地日志最后仍停留在 18:01:38 左右的 `Loading VoiceStudio model on device: cuda`，没有对应完成日志或新的成功 `render trace`。

### 本次没有执行

- 没有调用真实 Agent；
- 没有调用 VoiceStudio `/generate`；
- 没有播放或录音；
- 没有创建新的 batch-specific artifact；
- 没有修改 VoiceStudio 配置、应用源代码或历史 artifact；
- 没有执行 git stage/commit/push/reset；
- 没有进入 P8。

### 收尾结论

当前不能安全地把 P7 标记为完整通过。既有成功链路和精确用户听感确认只属于各自 batch，不能与未完成的新批次或停止未闭合批次合并。

```text
P7 = Blocked / incomplete
P8 = Not entered
Blocking condition = VoiceStudio model never reached loaded/ready state during bounded readiness check
```

若要再次推进，必须先解决或重新启动 VoiceStudio 的模型 readiness 问题，并再次只读确认 `loaded=true`、`loading=false`；随后才可使用新的 Agent/request/session/batch identity 进行一次有界真实运行。

## 11. 2026-09-29 新批次用户听感确认

### 11.1 精确绑定

用户在北京时间 `2026-09-29T18:40:17.212+08:00` 明确确认：

```text
听到了完整播放，batch_id=p7-acf9735565a64cac
```

该确认只绑定以下 artifact：

```text
batch_id:    p7-acf9735565a64cac
request_id:  req-p7-real-20260929-c7f7db7c1b1c2882
session_id:  session-p7-real-20260929-5cddc80212ec1102
artifact:    <PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-agent-tts-device\p7-real-agent-tts-device-20260929T103716Z-acf9735565a64cac.json
```

artifact 已记录：

```json
{
  "user_heard": true,
  "confirmed_batch_id": "p7-acf9735565a64cac",
  "confirmation_source": "user"
}
```

### 11.2 当前结论

```text
Agent reply              = established
VoiceStudio synthesis    = established
Windows playback         = PLAYING → COMPLETED
loopback/microphone      = capture_valid=true
user_heard               = true（精确绑定 p7-acf9735565a64cac）
backend_stop_ack         = null
loopback_output_stopped  = false
acoustic_output_stopped  = false
speaker_output_stopped   = false
status                   = blocked
```

本次确认已解除该批次的 `user_heard_pending` 阻塞，但不能替代播放完成事件之外的 backend stop ACK 或物理静音证据。因此：

```text
P7 = Blocked / incomplete
P8 = Not entered
```

未修改历史批次，未跨 batch 回填停止字段，未执行 git stage/commit/push/reset。


## 12. 2026-09-29 播放清理与同批停止证据收尾

### 12.1 根因与实现修复

本轮确认并修复了两个独立问题：

1. `PlaybackWorker` 的自然 `COMPLETED` 路径原先直接丢弃 active，没有调用 backend cleanup，因此 Windows `waveOut` 的 `reset/unprepare/close` 诊断无法在同一 ticket 上闭合。
2. 采集 post 窗口原先可能在播放期间或缓冲旧帧中取样，不能证明播放停止后的静音。

最小修复如下：

- `<PROJECT_ROOT>\app\tts\playback_worker.py`
  - 增加 `finalize_completed()` seam；
  - 自然完成后执行真实 backend cleanup；
  - 状态仍保持 `COMPLETED`，不伪造成 `STOP_ACKED`；
  - cleanup 成功只在同 ticket terminal event 记录 `stop_ack=true`；失败保持 `stop_ack=false`/unknown；
  - 兼容旧 backend：没有 completion cleanup seam 时不调用显式 stop，避免把自然完成误当用户停止。
- `<PROJECT_ROOT>\app\evidence\local_ports.py`
  - `backend_stop_ack` 只接受同 ticket terminal event 的 ACK；
  - capture post gate 在播放提交失败时也会释放，避免采集线程悬挂。
- `<PROJECT_ROOT>\app\audio\windows\evidence_capture.py`
  - 增加同步双端采集 `collect_synchronized_summaries()`；
  - lead/active 期间持续读取，播放 cleanup gate 未释放时持续排空流，gate 后才开始 post 窗口；
  - report 明确 `serial=false`。
- `<PROJECT_ROOT>\app\audio\windows\capture.py`
  - loopback 继续使用绝对 quiet threshold；
  - 独立 microphone 在绝对阈值不满足时，只有当 post 不高于本端 lead noise floor + 3 dB 才可判定 quiet；诊断保留 `post_quiet_basis`。
- `<PROJECT_ROOT>\tests\tts\test_playback_worker.py`
- `<PROJECT_ROOT>\tests\audio\test_evidence_capture.py`
- `<PROJECT_ROOT>\tests\evidence\test_local_ports.py`
  - 增加自然完成 cleanup、fail-closed、同 ticket ACK 和 post gate 回归覆盖。

### 12.2 验证结果

定向与全量验证：

```text
full unittest discovery = 817 passed, 2 skipped
compileall app tests      = PASS
git diff --check          = PASS
```

### 12.3 新真实 batch（机器证据已闭合）

未修改旧 batch，重新使用了新的 Agent/request/session/DSH_HOME，并保持 Agent → VoiceStudio → Windows playback → synchronized capture identity 一致：

```text
batch_id:    p7-8a6adeb2c3d744aa
request_id:  req-p7-real-20260929-06420c169b4d7a56
session_id:  session-p7-real-20260929-77ffb528530d69c4
artifact:
<PROJECT_ROOT>\implementation_plan\checkpoints\artifacts\2026-09-29-P7-real-agent-tts-device\p7-real-agent-tts-device-20260929T112524Z-8a6adeb2c3d744aa.json
```

当前同批字段：

```text
backend_stop_ack         = true
loopback_output_stopped  = true
acoustic_output_stopped  = true
speaker_output_stopped   = true
capture_valid             = true
user_heard               = null
status                   = blocked
blocked_reasons          = [user_heard_pending]
```

该 batch 的 `waveout_diagnostic.cleanup_verified=true`，并记录了 `reset_result=0`、`unprepare_result=0`、`close_result=0`。机器证据已闭合，但用户听感仍必须由用户对**精确 batch_id=p7-8a6adeb2c3d744aa**明确确认后才能写入；不得把 `p7-acf9735565a64cac` 的确认迁移过来。

### 12.4 当前结论

```text
P7 = blocked，仅剩同批 user_heard_pending
P8 = not entered
```


> **原文损坏（2026-09-30 发现）**：本段原始中文已被替换为 ASCII `?`，逐字不可恢复；以下为按证据重写，紧接其后的代码块为损坏原文备审计。
## 2026-09-29 复核边界与遗留

- Harness `session.event` 的 `assistant/message` / `text-chunks` 提取边界：`app/agent/adapter.py::_extract_text_chunk` 只接受该事件与 `text-chunks` 流，`reasoning` 等其它 stream 类型不进入回复原文（`tests/agent/test_adapter.py::test_streaming_text_notifications_are_forwarded_without_reasoning`）；本行其余表述未能复核（原文丢失）。
- TTS 侧先由 `ReplySegmenter` 切分出完整句，再提交 VoiceStudio 合成并由 `PlaybackWorker` 播放（`app/tts/segmentation/__init__.py`、`app/tts/playback_worker.py`）；本行其余表述未能复核（原文丢失）。
- `TextShell`/`ConversationManager` 的文字与语音入口共用同一轮对话链（`implementation_plan/00-project-overview.md`、`implementation_plan/design.md`）；本行其余表述未能复核（原文丢失）。
- `speed` 口径：`app/tts/private_selection.py` 固定 `DEFAULT_SPEED = 1.0` 且校验只接受该值，与本行保留的 `speed=1.0` 一致；本行其余表述未能复核（原文丢失）。
- P7 仍为 Blocked：本文件第 12 节的 batch `p7-8a6adeb2c3d744aa` 机器证据已闭合，但同批 `user_heard` 仍为 `null`（`user_heard_pending`），backend stop ACK 未闭合，需要用户对该精确 batch 单独确认；本行其余表述未能复核（原文丢失）。
- 测试计数保留原文锚点 `62 tests`、`817 tests`、`1 skipped`、`2 skipped`：其中 `817 tests` 与本文件 12.2 的 `817 passed, 2 skipped` 一致，`62 tests` 与 `1 skipped` 的具体归属未能复核（原文丢失）；相关文件为 `tests/tts/test_real_waveout_acoustic_runner.py` 与 `tools.p7_real_waveout_fault_acoustic_runner`。

```text
## 2026-09-29 ????????
- ????? Harness `session.event` ? `assistant/message` / `text-chunks` ???????????????? reasoning?
- ????? TTS ??????? `ReplySegmenter`?????????? VoiceStudio ??/PlaybackWorker???????????????????????
- `TextShell`/`ConversationManager` ?????????????????????????????????????
- ??????? `1.0`??????????????? `speed=1.0`?
- P7 ?????????????????????backend stop ACK?????????????????? batch `p7-8a6adeb2c3d744aa` ????
- ????? 62 tests ????? 817 tests ? 1 ??????????`tests/tts/test_real_waveout_acoustic_runner.py` ?? `tools.p7_real_waveout_fault_acoustic_runner`??? 2 skipped?
```
