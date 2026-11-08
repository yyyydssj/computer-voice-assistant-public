# P7 本机 VoiceStudio preflight 证据 ledger

- 日期：2026-09-28
- 范围：仅本机 VoiceStudio 与 Windows 音频链路
- 网络/费用边界：禁止云端 Provider、禁止 DeepSeek、禁止 Edge TTS；费用上限 0 元
- 当前阶段：真实 VoiceStudio 成功基线已完成；停止回执、故障注入、独立声学测量和用户听感仍未证明

## 1. 主机与运行时

| 项目 | 证据 | 结果 |
|---|---|---|
| 操作系统 | Windows 10.0.26100.0，AMD64 | Pass |
| 项目 Python | `<PROJECT_ROOT>\.python312\python.exe`，Python 3.12.10 | Pass |
| 项目回归 | `773 tests`，`OK (skipped=2)` | Pass |
| VoiceStudio 可执行文件 | `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\VoiceStudio.exe` | Found |
| VoiceStudio 版本 | 文件版本/产品版本 `0.5.6.0` | Pass |
| VoiceStudio 运行数据目录 | `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio` | Found |
| VoiceStudio 用户数据目录 | `<USER_HOME>\AppData\Roaming\VoiceStudio` | Found |

## 2. 启动 preflight

- 启动时间：2026-09-28 18:30:21 +08:00
- 主进程：`VoiceStudio.exe`，PID `5964`
- Electron 子进程：GPU PID `9488`、network utility PID `8440`、renderer PID `24896`
- 主窗口标题：`VoiceStudio`
- 主进程响应：`True`
- 当前观察：进程已启动且响应正常
- TCP 监听：未发现由 VoiceStudio 暴露的本机 TCP listener
- 解释：当前实例更可能通过 Electron 内部 IPC/本地运行时通信工作；不得猜测 HTTP 端口

## 3. 尚未执行项目

- [x] VoiceStudio 健康状态/本机运行时状态读取
- [ ] VoiceProfile 与输出设备确认
- [x] 单条短文本真实合成
- [x] Windows 播放开始证据（真实 waveOut 自然完成基线）
- [ ] `STOP_ACKED` 的真实 waveOut/等价协议证据
- [ ] `STOP_TIMEOUT` / `UNKNOWN`（仅在存在安全 fault shim 时）
- [ ] 独立声学测量
- [ ] 用户听感确认

## 4. 安全边界

- 本次 preflight 未读取或输出任何凭据。
- 本次 preflight 未向网络服务发送请求。
- 本次 preflight 后续基线已播放 1 次；未进行额外播放。
- 本次 preflight 未注入故障。
- `user_heard` 在获得独立人工或声学证据前必须保持 `None`。
- P7 仍为 `Blocked`；P8 未进入。

## 5. 下一步

1. 通过 VoiceStudio 本机 UI/应用内接口确认当前 VoiceProfile、引擎状态和输出设备。
2. 仅使用一条短文本执行一次本机合成/播放基线。
3. 分离记录 backend ACK、扬声器输出停止和用户听感三个结果。

## 6. 本机 backend 与模型状态复核

- VoiceStudio backend 已由桌面程序自动启动。
- backend 进程：Python 3.11.16 bundled runtime，`uvicorn main:app --app-dir backend --host 127.0.0.1 --port 3900`。
- 监听地址：`127.0.0.1:3900`，仅 loopback。
- `GET http://127.0.0.1:3900/health`：HTTP 200，`status=ok`，`version=0.5.6`，设备为 `cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`。
- `GET http://127.0.0.1:3900/startup/progress`：HTTP 200，`status=ready`，PyTorch、API routes、数据库和后台服务均为 `done`。
- `GET http://127.0.0.1:3900/engines`：当前 TTS active engine 为 `omnivoice`，active model 为 `k2-fsa/OmniVoice`，该 engine 标记为 `available=true`。
- `GET http://127.0.0.1:3900/engines/omnivoice/health`：HTTP 200，`ok=true`，`message=Healthy`。
- 当前证据结论：本地 VoiceStudio backend、OmniVoice engine 和 CUDA 运行链均已安装并可用；尚未把“模型已成功完成一次真实合成”误记为完成。模型权重是否在首次合成时懒加载，需通过一次受控短文本合成确认。

## 7. 真实本机 VoiceStudio 成功基线（2026-09-28 19:19–19:52 +08:00）

本节记录一次真实本机合成和一次真实 Windows 播放基线；不把它升级为 P7 完成。

- VoiceStudio 地址：`http://127.0.0.1:3900`，仅 loopback。
- 引擎：`omnivoice`；模型：`k2-fsa/OmniVoice`；CUDA：`NVIDIA GeForce RTX 3050 Laptop GPU`。
- 固定文本：`P7真实链路基线。`（live gate）；另有 preflight 文本 `Local VoiceStudio test.` 产物。
- 请求数：1 次 live gate；重试：0；云端请求：0；费用：0 元。
- 音频产物：`implementation_plan/checkpoints/artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav`。
- WAV 摘要：96044 bytes，SHA-256 `2d4af518275bbfafe019321c13ba603cee5cf4b9a79508eabcdefa9f976249da`，单声道、16-bit、24 kHz、48000 frames、2.0 s。
- 真实播放基线：`P7_RUN_REAL_TTS_EVIDENCE=1`；`WaveOutPlaybackBackend` 使用 Windows `winmm.waveOutOpen(WAVE_MAPPER)`、`waveOutWrite`，播放自然完成并收到 `WOM_DONE`。
- 设备枚举：`waveOutGetNumDevs=1`；设备 0 的驱动名称为 `Realtek(R) Audio`（控制台中文名称显示受编码影响）；播放代码使用 `WAVE_MAPPER`，未强制切换系统默认设备。
- 自动测试：`test_real_failure_matrix_live.py` 4 tests / OK；其中 2 个 live case 实际访问本机 VoiceStudio，1 个 live case 实际通过 Windows waveOut 播放。

### 证据分离

| 事实 | 结果 | 证据等级 |
|---|---|---|
| backend 合成成功 | Pass；HTTP 200，audio/wav，WAV 校验通过 | L1 真实本机 VoiceStudio |
| backend 播放提交/完成 | Pass；`received_audio=1`、`playback_calls_returned=1`、`PlaybackState.COMPLETED` | L2 backend 协议事实 |
| backend stop ACK | 未执行；本次是自然完成，不是 stop/reset | 未证明 |
| speaker_output_stopped | `None`；没有独立声学测量 | 未证明 |
| user_heard | `None`；没有人工听感确认 | 未证明 |
| 真实故障注入 | 未执行；没有 fault proxy/shim | 阻塞 |

### 结论

这证明了本机 VoiceStudio 模型已经下载/可用并完成了真实合成，且真实 Windows waveOut 播放链路能自然完成；不证明停止回执、物理扬声器静音、用户听见、真实故障矩阵或 Agent 端到端。P7 保持 `Blocked`，P8 未进入。

