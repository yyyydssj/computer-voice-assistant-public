# P7 T12：可停止播放、默认接缝与故障矩阵复核补充（2026-09-28）

> 本文是 T11 的结果校正与补充记录；不改写 T06–T11 的历史验收事实。最新测试计数和本批边界以本文为准。

## 本批复核范围

按子对话交接复核以下三项：

1. **可停止播放**：持久化播放 worker、FIFO ticket、取消/停止确认、超时/未知/失败后的 fail-closed、迟到事件安全边界。
2. **默认 Agent → 回复文字 → 可选 TTS 接缝**：默认运行时保持安全降级；注入已就绪 Agent 时先输出并 flush 原文，再在显式 `voice_enabled=True` 时调用可选播报。
3. **真实故障矩阵**：补齐离线契约矩阵，同时明确 fake/离线证据不能冒充真实 VoiceStudio、真实 Windows waveOut、真实扬声器或真实 Agent 证据。

## 已复核的实现文件

- `app/tts/playback_worker.py`
- `app/tts/real_playback.py`
- `app/tts/completed_reply.py`
- `app/runtime/assistant_runtime.py`
- `app/main.py`
- `app/ui/text_shell.py`
- `tests/tts/test_playback_worker.py`
- `tests/tts/test_real_playback.py`
- `tests/tts/test_real_failure_matrix.py`
- `tests/tts/test_real_failure_matrix_live.py`
- `tests/foundation/test_main_voice_opt_in.py`

## 复核结论

### 1. 可停止播放：本地契约通过

- worker 持久化并串行处理播放 ticket；
- `STOP_ACKED` 只在 backend 返回明确 stop/reset/buffer-return 协议确认时产生；
- `STOP_TIMEOUT`、`UNKNOWN`、`FAILED` 后进入 fail-closed，禁止后续语音继续合成/提交；
- stop 发生在 backend 阻塞期间时，worker 恢复后执行一次 best-effort backend stop；
- 已记录 `STOP_TIMEOUT` 后，迟到 ACK 不得升级为 `STOP_ACKED`；
- 取消后后续句段不得提交；queued ticket 可取消且不触碰当前设备；
- `RealVoicePreview.close()` 永久关闭自身实例，幂等；close 后再次 `speak()` 返回 `preview_unavailable`；
- backend 协议确认不等于物理扬声器静音，也不等于用户听见，`user_heard` 仍为 `None`。

### 2. 默认 Agent 接缝：默认安全降级，显式语音才播报

默认运行时：

```text
TextShell → AssistantRuntime → LocalChatRouter → UnavailableAgentAdapter
        → 文字失败/不可用 outcome → 输出并 flush → 默认不播报
```

注入已就绪 Agent 时：

```text
TextShell → ConversationManager → Agent adapter → completed outcome
        → 先输出并 flush 原文 → voice_enabled=True 时调用可选 speaker
```

已确认：

- 默认不读取 DeepSeek key；
- 默认不构造 Agent SDK、transport 或 TTS 设备；
- `voice_enabled=False` 时不使用注入的 runtime speaker；
- failed/incomplete Agent outcome 保留文字但不播报；
- `/exit` 与 EOF 都进入 runtime close 生命周期；
- `AssistantRuntime.close()` 幂等；关闭第一次抛错后不自动重试。

### 3. 故障矩阵：离线契约通过，真实故障证据仍阻塞

离线矩阵覆盖：

- Provider timeout / `URLError`；
- HTTP error、非 200、错误 Content-Type；
- 过大音频；
- 坏 WAV、截断 WAV；
- 错误采样率、双声道、超帧数、静音；
- 合成前取消；
- 全部准备完成但提交前取消；
- 当前播放中取消；
- fake stop ACK / timeout / unknown；
- 后续句段禁止提交；
- 原文保留；
- `user_heard is None`；
- `received_audio`、实际提交数、正常返回数分离。

`tests/tts/test_real_failure_matrix_live.py` 仅提供显式 opt-in 的真实成功基线 gate；未把 timeout、HTTP error、坏 WAV、设备 stop timeout/unknown 伪装成真实证据。

## 验证结果（2026-09-28）

### 全量

```text
.python312\\python.exe -B -m unittest discover -s tests -t . -q
Ran 773 tests
OK (skipped=2)
```

### 专项

```text
main_voice_opt_in：14 tests / OK
playback_worker：11 tests / OK
real_playback：29 tests / OK
real_failure_matrix*.py：20 tests / OK (skipped=2)
```

### 静态检查

```text
.python312\\python.exe -m compileall -q app tests     PASS
git diff --check                                      PASS
git diff --cached --check                             PASS
```

## 真实证据分层与阶段状态

| 层级 | 当前结论 |
|---|---|
| L0 fake Agent/TTS/backend | Pass，仅本地契约 |
| L1 真实 VoiceStudio 成功基线 | 有显式 opt-in live gate；本次未运行 |
| L1 真实 VoiceStudio 故障注入 | Blocked；缺 fault proxy、服务端故障模式或等价可复核装置 |
| L2 真实 Windows waveOut stop ACK/timeout/unknown | Blocked；缺正式设备证据包 |
| L3 真实 Agent → VoiceStudio → 设备默认端到端 | Blocked；当前仅完成可注入 seam，默认 Agent 仍 fail-closed |
| P7 | Blocked |
| P8 | 未进入 |

## 本批未执行

- 未读取真实凭据；
- 未运行真实网络请求；
- 未连接或调用真实 VoiceStudio；
- 未运行真实 Windows 播放或真实扬声器测试；
- 未执行真实故障注入；
- 未执行 `git add`、commit、push、reset、clean。

## 下一停止点

如继续推进 L1/L2/L3，必须另行确认并记录：真实 VoiceStudio 访问授权、网络/凭据授权、Windows 输出设备授权、真实 Agent 链路授权、故障注入方式与证据采集预算。在这些条件满足前，保持 P7 `Blocked`，不以 fake 或人为异常替代真实故障证据，不进入 P8。