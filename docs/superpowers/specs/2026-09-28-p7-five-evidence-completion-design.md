# P7 五项真实证据补齐设计（2026-09-28）

## 目标

在不伪造证据、不把离线 fake 提升为真实硬件事实的前提下，补齐 P7 的证据采集与验收工具链：

1. `waveOut STOP_ACKED`：记录 WinMM reset、当前 buffer 返回、事件时间线和 cleanup 结果；
2. `STOP_TIMEOUT` / `UNKNOWN`：提供隔离的 test-only fault fixture，并明确区分 fixture 与真实设备故障；
3. `speaker_output_stopped`：同时支持 WASAPI loopback 数字输出测量和独立麦克风声学测量，任一链路无效则综合结果保持 `None`；
4. `user_heard`：保留为人工确认字段，只允许用户本人针对唯一播放批次确认；
5. Agent → 原文 → VoiceStudio → Windows 设备：提供显式 opt-in 的真实链路 runner，默认 Agent、云 Provider、工具和语音输出仍关闭。

## 非目标

- 不以 GitHub 示例、离线 fake、`WOM_DONE`、WAV 文件存在或 HTTP 200 冒充物理声学或人工听感证据。
- 不自动启用 DeepSeek、云 Provider、Edge TTS 或工具调用。
- 不在默认测试发现中播放、录音、访问 VoiceStudio 或占用 Windows 音频设备。
- 不自动推断 `user_heard`。

## 设计

### 1. 证据模型与账本

新增本地、可序列化的 evidence ledger：

- `run_id`、`batch_id`、设备标识、音频 SHA-256、source text hash；
- 单调时间线：playback start、stop request、backend ack、loopback capture windows、acoustic capture windows、cleanup；
- 分层字段：`backend_stop_ack`、`loopback_output_stopped`、`acoustic_output_stopped`、`speaker_output_stopped`、`user_heard`；
- `evidence_kind` 明确为 `real_device`、`controlled_fault_fixture` 或 `offline_contract`；
- 原始录音默认不保留，除非后续明确授权；保存前先脱敏摘要和哈希。

综合 `speaker_output_stopped=true` 的条件：loopback 与独立麦克风两条测量都有效、播放相关信号被观测到、stop 后窗口满足停止阈值。任一项缺失、全零或设备关联不可靠，结果为 `None`。

### 2. WinMM backend 诊断

扩展 `WaveOutPlaybackBackend` 的只读诊断：

- 记录 `waveOutOpen`、`waveOutPrepareHeader`、`waveOutWrite`、`waveOutReset`、`waveOutUnprepareHeader`、`waveOutClose` 的返回码；
- 记录当前 `WAVEHDR` 地址、提交/回调时间和 callback message；
- 将 `STOP_ACKED` 限定为 reset 成功 + 当前 buffer 返回；
- reset 非零映射为 `UNKNOWN`，等待超时映射为 `STOP_TIMEOUT`；
- 诊断只写入 evidence ledger，不改变默认安全策略。

### 3. WASAPI loopback 与独立麦克风

新增可选 Windows capture provider：

- 优先使用 PyAudioWPatch 的 WASAPI loopback 设备枚举；
- 独立麦克风必须按设备 index/name/host API 记录，不能将 loopback 或 stereo mix 当作独立声学传感器；
- 采集器支持 bounded lead-in / playback / post windows；
- 计算 RMS、源信号相关性、有效帧数、全零检测和时间窗口；
- 采集依赖缺失、权限失败、全零或端点不稳定时返回 `None` 并保留原因；
- 默认只保存 JSON 摘要，原始音频在无明确授权时删除。

### 4. Failure fixture

新增仅测试使用的 backend fixture：

- `reset_error`：返回 unknown；
- `drop_callback`：返回 timeout；
- `delay_callback`：可验证边界超时；
- `wrong_buffer_callback`：不能错误升级为 ACK。

fixture 的所有记录必须带 `evidence_kind=controlled_fault_fixture`，禁止进入真实设备通过统计。

### 5. 真实本地 Agent 链路

新增显式 opt-in 的本地 Agent adapter/runner：

- 仅允许 loopback/本地地址和明确配置的 endpoint；
- 返回 `agent_request_id`、原文、finish reason；
- TextShell 先 flush 原文，再把相同原文交给 VoiceStudio；
- 记录 VoiceStudio audio id/WAV 哈希、Windows backend 事件和双通道采集结果；
- 没有可用本地 Agent 时生成 Blocked 结果，不使用 fake adapter 伪造端到端通过；
- 默认 runtime 保持 `UnavailableAgentAdapter`、`voice_enabled=False`。

## 验收门槛

- 默认全量测试通过，且默认测试不产生网络、播放、录音副作用；
- fixture 测试证明 timeout/unknown/failure-safe 行为；
- live runner 只在明确环境变量 opt-in 时执行，并且单进程、串行、bounded；
- STOP_ACKED 必须包含 backend diagnostics 和 cleanup verified；
- loopback 与独立麦克风都有效且 stop 后停止，才可写入 `speaker_output_stopped=true`；
- `user_heard` 只有用户本人对新的唯一 batch 明确答复后才更新；
- 真实 Agent 链路缺少任何一环时保持 Blocked；
- P7 只有五项均满足各自证据门槛后才允许进入 P8。

## 风险与回退

- 无 PyAudioWPatch/可用端点：保持 Blocked，不降级为全零通过；
- 麦克风与扬声器不是独立路径：只保留 loopback 结果，综合声学结果为 None；
- 真实故障 fixture 不等于真实硬件故障：在报告中分栏；
- VoiceStudio 或本地 Agent 不可用：保留完整文字、生成安全失败记录，不改变默认运行安全边界；
- 任何设备句柄不确定：停止扩大测试，优先清理/禁用 worker。
