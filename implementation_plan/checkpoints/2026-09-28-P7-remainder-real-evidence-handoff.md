# P7 剩余进度与真实证据执行交接（2026-09-28）

> 用途：交给下一次新对话继续执行 P7。本文是当前状态、剩余工作、真实侧效应授权和下一步验收门的单一交接入口。
>
> 重要边界：本文不宣称真实 VoiceStudio、真实 Windows waveOut、真实 Agent 默认端到端或用户听感已经验收。P7 当前仍为 `Blocked`，P8 未进入。

## 一、当前目标

用户要求继续补齐并最终验收以下四类真实证据：

1. 真实 VoiceStudio 故障注入；
2. 真实 Windows `waveOut` 的 `STOP_ACKED`、`STOP_TIMEOUT`、`UNKNOWN` 设备/后端证据；
3. 真实 Agent → 回复文字 → VoiceStudio → Windows 设备的默认端到端；
4. 真实扬声器停止输出以及用户听见与否的独立证据。

用户在 2026-09-28 明确表示：**“全部授权”**。下一对话仍须先执行具体的 preflight、预算登记和证据边界确认，不得把泛化授权直接当作已经访问服务、读取凭据或开始播放的事实。

## 二、当前已完成

### 1. 可停止播放本地契约

相关文件：

- `app/tts/playback_worker.py`
- `app/tts/real_playback.py`
- `tests/tts/test_playback_worker.py`
- `tests/tts/test_real_playback.py`

已具备：

- 持久化 `PlaybackWorker` 和 FIFO ticket；
- `STOP_ACKED`、`STOP_TIMEOUT`、`UNKNOWN`、`FAILED` 状态；
- timeout/unknown/failed 后 fail-closed，禁止后续语音继续提交；
- stop 发生在 backend 阻塞期间时，恢复后执行一次 best-effort backend stop；
- 迟到 ACK 不得把既有 `STOP_TIMEOUT` 升级为 `STOP_ACKED`；
- queued ticket 取消不触碰当前播放；
- `RealVoicePreview.close()` 幂等，close 后再次 `speak()` 返回 `preview_unavailable`。

现有语义必须保持：

```text
STOP_ACKED = backend reset/buffer-return 协议事实
             ≠ 物理扬声器已经静音
             ≠ 用户已经听见
```

### 2. 默认 Agent → 文字 → 可选 TTS 接缝

相关文件：

- `app/runtime/assistant_runtime.py`
- `app/main.py`
- `app/ui/text_shell.py`
- `app/tts/completed_reply.py`
- `tests/foundation/test_main_voice_opt_in.py`

默认路径保持：

```text
TextShell
→ AssistantRuntime
→ LocalChatRouter
→ UnavailableAgentAdapter
→ 文字失败/不可用结果
→ 先输出并 flush
→ 默认不播报
```

注入已就绪 Agent 且显式 `voice_enabled=True` 时：

```text
Agent completed outcome
→ 原文先输出并 flush
→ 再调用可选 speaker
```

已确认：

- 默认不读取 DeepSeek key；
- 默认不创建 Agent SDK、transport 或 TTS 设备；
- `voice_enabled=False` 不使用注入 speaker；
- failed/incomplete outcome 保留文字但不播报；
- `/exit` 与 EOF 进入 close 生命周期；
- `AssistantRuntime.close()` 幂等，首次关闭异常后不自动重试。

### 3. 本地故障矩阵

相关文件：

- `tests/tts/test_real_failure_matrix.py`
- `tests/tts/test_real_failure_matrix_live.py`

离线矩阵已覆盖：

- Provider timeout / `URLError`；
- HTTP error、非 200、错误 Content-Type；
- 过大音频；
- 坏 WAV、截断 WAV；
- 错误采样率、双声道、超帧数、静音；
- 合成前取消、提交前取消、播放中取消；
- fake stop ACK / timeout / unknown；
- 后续句段禁止提交；
- 原文保留；
- `received_audio`、实际提交数、正常返回数分离；
- `user_heard is None`。

live 文件当前只提供显式 opt-in 的真实成功基线 gate，不能作为真实故障注入证据。

## 三、最新验证结果

日期：2026-09-28

```text
.python312\\python.exe -B -m unittest discover -s tests -t . -q
Ran 773 tests
OK (skipped=2)
```

专项：

```text
main_voice_opt_in：14 tests / OK
playback_worker：11 tests / OK
real_playback：29 tests / OK
real_failure_matrix*.py：20 tests / OK (skipped=2)
```

静态检查：

```text
.python312\\python.exe -m compileall -q app tests     PASS
git diff --check                                      PASS
git diff --cached --check                             PASS
```

未执行：

- 真实网络请求；
- 真实 VoiceStudio；
- 真实 Windows 播放；
- 真实扬声器；
- 真实故障注入；
- `git add`、commit、push、reset、clean。

## 四、P7 当前剩余工作

### A. 真实 VoiceStudio 故障注入

推荐执行结构：

```text
应用 → localhost fault proxy → 真实 VoiceStudio
```

代理不能替代真实 VoiceStudio，只能对真实响应做受控扰动。最小故障矩阵：

| Case | 故障方式 | 预期结果 |
|---|---|---|
| VS-CONN | 连接拒绝/服务不可达 | 文字回退，不提交播放 |
| VS-TIMEOUT | 延迟真实响应超过客户端 timeout | 原文保留，记录 timeout |
| VS-HTTP-500 | 改写真实响应状态码为 500 | 不提交播放 |
| VS-HTTP-429 | 改写真实响应状态码为 429 | 不提交播放，记录限流 |
| VS-CONTENT-TYPE | 改写 Content-Type | WAV 校验前失败 |
| VS-TRUNCATED-WAV | 截断真实 WAV | `invalid_audio`，不提交 |
| VS-OVERSIZE | 超出音频上限 | `audio_too_large`，不提交 |
| VS-BAD-WAV | 修改 RIFF/WAVE 或参数 | 音频校验失败 |

每个 case 必须保存脱敏证据：

```text
correlation_id
真实 VoiceStudio 请求/响应摘要
代理注入动作
HTTP 状态与 Content-Type
客户端错误码
received_audio
实际播放提交数
文字回退文本摘要
```

不能把纯 fake server 异常写成真实 VoiceStudio 故障证据。

### B. 真实 Windows `waveOut` 停止证据

#### B1. 真实 ACK

在真实 Windows 设备上记录：

- `waveOutOpen` 返回值；
- `waveOutWrite` 返回值；
- `waveOutReset` 返回值；
- `WOM_DONE` / buffer return 回调；
- ticket 状态迁移和时间戳；
- 输出设备标识、采样率、声道、时长。

只有以下条件同时满足，才记录为真实 `STOP_ACKED`：

```text
waveOutReset 成功
+ 已提交 buffer 收到 WOM_DONE/buffer return
+ ticket 最终为 STOP_ACKED
```

#### B2. timeout / unknown

不能凭 fake backend 伪装成真实设备故障。应二选一：

1. 经过批准的 native fault shim：真实 Windows API 路径 + 受控抑制/延迟 reset 或 callback；
2. 可恢复的真实虚拟音频设备/驱动故障装置。

如果使用 shim，证据标签必须写成：

```text
真实 Windows waveOut 路径 + 注入式 native fault
```

不得写成自然发生的硬件故障。

每个 stop 结果都要验证：

```text
STOP_ACKED  → 记录 backend stop confirmation
STOP_TIMEOUT → 禁止后续语音
UNKNOWN      → 禁止后续语音
FAILED       → 禁止后续语音
```

迟到 `WOM_DONE` 不得改变已记录的 timeout/unknown。

### C. 真实 Agent → VoiceStudio → Windows 设备端到端

当前默认 Agent 仍是 fail-closed。真实端到端必须使用显式受控 opt-in，不得直接把默认运行时改成无条件联网。

首轮实测限制：

- 固定、无敏感、无副作用提示词；
- 1 个真实 Agent 请求；
- 禁止工具调用；
- 禁止文件、网络、系统操作；
- 最多 1～2 个短句；
- 已确认 VoiceProfile；
- 指定 Windows 输出设备；
- 固定请求、播放和费用上限。

必须验证：

```text
Agent completed outcome
→ 原文先输出并 flush
→ TTS 接收文本与原文完全一致
→ WAV 校验通过
→ playback ticket 顺序正确
→ waveOut backend 事实可追溯
→ 失败时原文仍可用
```

必须同时执行一条默认安全回归：

```text
默认配置 → 不读取 key、不启动真实 Agent、不创建设备
显式 opt-in → 才允许真实链路
```

### D. 扬声器停止输出与用户听感

必须拆为两个独立结果：

```text
backend_stop_ack
speaker_output_stopped
user_heard
```

#### D1. 扬声器停止输出

使用独立测量麦克风或声学采集记录：

- stop 前存在可识别音频信号；
- stop 后在规定窗口内降到环境噪声阈值；
- 连续保持规定时长；
- 声学时间戳与 `waveOutReset`、`WOM_DONE` 对齐。

#### D2. 用户听见

只能通过：

- 测试员明确确认“听到了/没听到”；或
- 经过批准的人因/声学测试流程。

不能由 `waveOutWrite` 成功、`WOM_DONE`、`waveOutReset` 或麦克风有声音自动推断 `user_heard=true`。没有独立证据时必须保持 `None`。

## 五、下一对话执行顺序

```text
1. 只读 preflight：确认 VoiceStudio 版本、端点、设备、Python/Windows 环境
2. 建立证据 ledger：请求数、播放次数、时长、费用、日志脱敏规则
3. 真实 VoiceStudio 成功基线
4. 真实 VoiceStudio fault proxy 故障矩阵
5. 真实 waveOut STOP_ACKED
6. 受控 native fault shim 的 timeout/unknown
7. 真实 Agent → VoiceStudio → Windows 端到端
8. 独立声学停止测量
9. 用户听感确认
10. 写专项 checkpoint，更新 P7 状态
```

任何一步失败都必须：

```text
停止扩大范围
保留文字可用性
保留失败原文和错误事实
不把 fake 结果升级成真实证据
不自动进入 P8
```

## 六、下一对话开始时必须先确认的操作参数

虽然用户已授权全部方向，仍需在真正产生外部侧效应前登记以下具体参数：

- VoiceStudio 本机地址、端口和版本；
- 凭据来源与是否允许读取环境变量/本地凭据存储；
- 最大真实请求数；
- 最大重试数；
- 最大播放次数和单次最长时长；
- 允许的费用上限；
- Windows 输出设备名称；
- 是否允许 native fault shim；
- 是否允许使用虚拟音频设备或设备级故障装置；
- 是否有独立测量麦克风；
- 是否允许保存 WAV 摘要、声学波形摘要和脱敏日志；
- 故障代理的启动/停止和恢复方式。

这些参数确认后，才可进入真实 preflight；本交接文档本身不执行任何真实请求或播放。

## 七、状态结论

```text
L0 fake Agent/TTS/backend              Pass（本地契约）
L1 真实 VoiceStudio 成功基线           待执行
L1 真实 VoiceStudio 故障注入           Blocked/待执行
L2 真实 Windows waveOut STOP_ACKED     待执行
L2 真实 Windows timeout/unknown        待执行，需 fault shim/设备装置
L3 真实 Agent→VoiceStudio→设备默认链路 待执行
独立扬声器停止测量                    待执行
用户听感                            未知，必须独立确认
P7                                  Blocked
P8                                  未进入
```

## 八、相关交接文件

- `implementation_plan/checkpoints/2026-09-28-P7-T12-followup-verification.md`
- `implementation_plan/checkpoints/2026-09-28-P7-T11-stop-default-runtime-failure-matrix.md`
- `implementation_plan/checkpoints/2026-09-28-P7-T06-stage-acceptance.md`
- `implementation_plan/P7-followup-execution-tracker.md`
- `app/tts/playback_worker.py`
- `app/tts/real_playback.py`
- `app/runtime/assistant_runtime.py`
- `app/main.py`
- `tests/tts/test_real_failure_matrix.py`
- `tests/tts/test_real_failure_matrix_live.py`

## 九、给新对话的第一条指令

```text
读取本交接文档，先只做真实 preflight 和证据 ledger，不要直接播放、联网或注入故障。
根据第六节逐项确认具体参数；参数齐全后按第五节顺序执行。
每个真实 case 单独留证，严格区分 backend、设备声学输出和用户听感。
```