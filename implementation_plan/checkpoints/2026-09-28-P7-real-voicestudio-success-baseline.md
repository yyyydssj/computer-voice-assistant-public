# P7 真实 VoiceStudio 成功基线证据（2026-09-28）

## 范围

本 checkpoint 只记录本机 VoiceStudio/OmniVoice 的真实成功基线和一次真实 Windows waveOut 自然完成播放。禁止云端 Provider、DeepSeek、Edge TTS；费用上限 0 元；未读取凭据；未改默认 runtime；未进入 P8。

## 环境事实

- 可执行文件：`<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\VoiceStudio.exe`
- 版本：`0.5.6`
- loopback backend：`127.0.0.1:3900`
- active engine：`omnivoice`
- active model：`k2-fsa/OmniVoice`
- device：`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`
- GPU 警告：RTX 3050 Laptop GPU 4 GB VRAM；引擎提示约需 6 GB，实际请求成功但可能较慢。

## 真实证据

1. `GET /health`、`/startup/progress`、`/engines`、`/engines/omnivoice/health` 全部通过。
2. 真实 `/generate` 请求返回 HTTP 200、`audio/wav`，生成约 2.0 秒音频。
3. 产物已复制到：`implementation_plan/checkpoints/artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav`。
4. 产物摘要见同目录 `eff172cb.wav.json`：单声道、16-bit、24 kHz、2.0 秒、SHA-256 `2d4af518275bbfafe019321c13ba603cee5cf4b9a79508eabcdefa9f976249da`。
5. opt-in live gate 运行结果：`test_real_failure_matrix_live.py` 共 4 tests / OK；其中真实 VoiceStudio WAV gate 和真实 WaveOut 自然完成 gate 通过。
6. WinMM 枚举到 1 个 waveOut 设备，代码通过 `WAVE_MAPPER` 选择系统映射设备，真实播放收到 `WOM_DONE` 并进入 `PlaybackState.COMPLETED`。

## 严格不推断

- `WOM_DONE`/自然完成不等于 `STOP_ACKED`。
- `waveOutWrite` 或 `WOM_DONE` 不等于物理扬声器已停止。
- 没有声学测量，所以 `speaker_output_stopped=None`。
- 没有人工确认，所以 `user_heard=None`。
- 没有 fault proxy/shim，所以真实 timeout/HTTP error/坏 WAV/超限矩阵仍未证明。

## 状态与下一停止点

- P7：`Blocked`。
- P8：未进入。
- 下一步需要独立授权预算下的 stop/reset 实测；若无安全 fault shim，则 `STOP_TIMEOUT`/`UNKNOWN` 和真实 VoiceStudio 故障注入继续保持阻塞。
