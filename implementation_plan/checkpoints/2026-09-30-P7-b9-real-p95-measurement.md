# P7 B9 真实句级 p95 采集（2026-09-30，真实链路）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 状态：**真实采集已完成**；`gate_to_device`（19.2 第二条）**通过**，`ready_to_submit`（19.2 第一条）**未通过**；另发现两个必须登记的真实缺陷/口径问题。P7 仍为 **Blocked**，未进入 P8。
- 前序：[B9 离线打点 checkpoint](2026-09-30-P7-b9-sentence-timing-instrumentation.md)；计划注记：`implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md` 第 19.2 / 19.2.1 节。
- runner（本批新增）：`tools/p7_real_sentence_p95_runner.py`（无 `--live-opt-in` 时 exit 2，拒绝执行）

## 1. 真实环境与预算

- 服务：本机 VoiceStudio `0.5.6`，`GET /health` → HTTP 200，`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`。
- **本会话启动后端的过程（如实登记）**：开始时 GUI 进程存在（18:07 启动）但 3900 无监听、`/health` 连接失败；按打包版启动计划（`runtimePython + -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 3900`，cwd = `setting_py\VoiceStudio\project`）用 `project\.venv\Scripts\python.exe` 启动，18:41 起 `/health` 200。日志：`%TEMP%\voicestudio-p7-backend-20260930.log`。
- VoiceProfile：`e8572e8e`，`speed=1.0`、`pause_ms=400`、`fade_ms=80`、`version=2`（与 `private_selection.py` 常量一致）。
- 预算与实际：**4 次真实运行**（monotonic 突发 1 次、paced `ws_pcm` 1 次、paced `http_wav` 1 次、perf_counter 突发 1 次）+ 5 次 `verify_provider` 计时；合计约 160 句合成/真实播放（含 4 次预热句）。外部网络 0、麦克风 0、音频落盘 0、费用 0；全部请求仅到 `127.0.0.1:3900`。

## 2. 结果（artifact 均在 `checkpoints/artifacts/2026-09-30-P7-batch9-real-p95/`）

| # | 模式 / 节奏 / 时钟 | 播放 | 墙钟 | `ready_to_submit` p50 / p95 (ms) | `gate_to_device` p50 / p95 (ms) | `first_pcm_to_device` p95 (ms) | 过载 / late |
|---|---|---|---|---|---|---|---|
| 1 | `ws_pcm` / 突发(5×4) / monotonic | 9/20 | 30.1 s | 13594 / 26203 | 0.0 / 0.0（**测不出**，见发现 A） | 0.0 | `True` / `playback:STOP_ACKED@generation2` |
| 2 | `ws_pcm` / **paced** / perf_counter | **20/20** | 61.56 s | **20.79 / 30.40** | **0.0134 / 0.0204** | 0.0811 | `False` / 无 |
| 3 | `http_wav` / **paced** / perf_counter | **20/20** | 61.15 s | **29.23 / 31.44** | **0.0147 / 0.0229** | 0.1610 | `False` / 无 |
| 4 | `ws_pcm` / 突发(5×4) / perf_counter | 9/20 | 30.01 s | 12608 / 25052 | 0.0130 / 0.0177 | 0.0778 | `True` / `playback:STOP_ACKED@generation2` |

- 判定：**19.2 第二条（除起播缓冲外 p95 < 50 ms）通过**——`gate_to_device` p95 = 0.0204 ms（ws_pcm）/ 0.0229 ms（http_wav），比目标低三个数量级，原因是首个 PCM 块本身就 ≥ 起播阈值，闸门与提交落在同一循环迭代。
- 判定：**19.2 第一条（`sentence_ready → tts_submitted` p95 < 20 ms）未通过**——paced（pending ≤ 1）下仍为 p50 20.8/29.2 ms、p95 30.4/31.4 ms（ws_pcm/http_wav）。
- 边界：`user_heard=null`、`speaker_acoustically_verified=false`；后端完成不代表有人听见；未做声学测量；音频不落盘。

## 3. 发现 A：`time.monotonic` 在 Windows 上无法测量这些跨度（口径缺陷）

- `time.get_clock_info("monotonic")` → `implementation='GetTickCount64()'`, `resolution=0.015625`（15.625 ms）。
- 实测 20 万次连续调用：**0 次正差值**；同一个脚本里 `time.perf_counter`（`QueryPerformanceCounter()`，`resolution=1e-07`）最小正差值 `9.999985195463523e-08 s`。
- 后果：用默认 `time.monotonic` 打点，worker 侧 `gate_to_device` / `first_pcm_to_device` 会稳定显示 `0.000 ms`（见结果表第 1 行）——那是**测不出来**，不是真的为 0。因此：**真实采集必须注入 `time.perf_counter`**（runner 默认如此；`--clock monotonic` 仅用于复现该缺陷）。离线单测用脚本时钟，不受影响。
- 处置：库默认时钟保持不变（不改既有行为），口径要求写进计划 19.2.1 与本 checkpoint。

## 4. 发现 B：19.2 第一条的真实根因 = 每句都做一次 provider 身份验证

- 代码事实：`StreamingVoiceSession._speak()` 第 546 行在 `tts_submitted_at` 打点**之前**调用 `self._verifier(self.selection)`；默认 verifier 为 `_verify_selection`，其 `verify_provider()`（`app/tts/private_selection.py:115`）每句执行 **两次 HTTP 往返**：`GET /profiles` 与 `GET /profiles/{id}/audio`（下载参考音频，上限 4 MB）再做 SHA-256 比对。
- 实测开销（真实服务，5 次）：`56.378 / 29.030 / 30.428 / 18.428 / 29.662 ms`，min/p50/max = **18.4 / 29.7 / 56.4 ms**。
- 与结果吻合：paced `ws_pcm` p50 20.79 ms、`http_wav` p50 29.23 ms、两者 p95 30–31 ms，量级与 p50 29.7 ms 的验证开销一致。
- 结论：**这不是线程抖动，而是每句一次的外呼与哈希开销被计入了“句就绪→提交”跨度**。要达成 < 20 ms，必须把身份验证从每句路径上移走（例如每次回复/每次会话验证一次，或把验证结果按 profile+reference 哈希缓存，或移出计时跨度并单独度量）。

## 5. 发现 C：突发推送的过载保护行为（非缺陷，但必须登记）

- 结果表第 1、4 行两次独立运行一致：一次性推入 5×4=20 句 → `overloaded=True`、只播 **9/20**、并留下 `late_events=["playback:STOP_ACKED@generation2"]`；`error=None`（文字不丢）。
- 更极端情况：**单次 `push_text_chunk` 超过 8 句**被直接拒绝，抛 `StreamingVoiceSessionError("streaming_queue_budget_exceeded")`（`app/tts/streaming_session.py:489`）。
- 含义：真实 Agent 若把整段长回复一次性塞入，会触发防失控截断（保留文字、停止后续语音）。因此**标准测量场景应为 paced（一句一句推，pending ≤ 1）**；突发场景只作为过载证据。

## 6. 发现 D：19.2 第 3/4 条当前无法评估（打点缺口）

- 缺少 `tts_submitted_at → first_pcm_at`（合成/首音延迟）跨度，因此无法按 19.2 第 3/4 条比较 `auto` 与 `http_wav` 的“中位起播延迟”（本轮只能比较墙钟：61.56 s vs 61.15 s，几乎一致；以及已测得的 gate/submit 跨度）。
- 需要增补一个只读跨度（`submit_to_first_pcm_ms`）才能评估；本批未擅自扩大改动范围。

## 7. 未做与边界

- 未做声学测量、未做用户听感确认（`user_heard=null`）；未验证“扬声器是否真的出声”之外的事实。
- 未做断线注入、HTTP 失败演练、Edge（仍无实现）、未接默认 runtime、未改 `TTS_VOICESTUDIO_MODE` 默认值。
- 未提交、未 push。
- 未覆盖：`auto` 模式的真实降级、连续多轮长回复、并发取消压力。

## 8. 下一入口（均需单独授权）

1. **B9-fix**：把 provider 身份验证移出每句路径（每回复一次 / 缓存 / 移出跨度），再按本 checkpoint 的 paced 场景复测 19.2 第一条；
2. 增补 `submit_to_first_pcm_ms` 打点，评估 19.2 第 3/4 条；
3. 之后才谈默认切换与 P7 阶段验收（第 5、6 项），P8 仍需另行授权。
