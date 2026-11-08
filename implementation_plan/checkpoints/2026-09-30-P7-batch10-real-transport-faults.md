# P7 Batch 10：真实传输故障演练（断线注入 + HTTP fallback 失败，2026-09-30）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 状态：**已完成 5 个真实/受控故障演练**；对应跟踪表第 1、2 项。P7 整体仍为 `Blocked`（其余退出条件未达成），未进入 P8。
- runner（本批新增）：`tools/p7_real_transport_fault_runner.py`（无 `--live-opt-in` 则 exit 2；`--scenario` 必选）
- artifact：`implementation_plan/checkpoints/artifacts/2026-09-30-P7-batch10-real-transport-faults/`（JSON，UTF-8）

## 1. 环境与预算

- VoiceStudio `0.5.6`、`/health` 200、`cuda (RTX 3050 Laptop)`；后端为本会话启动的 `project\.venv` uvicorn（`--app-dir backend`，端口 3900），演练中被**真实杀死并重启**（每次杀后本轮立即重启并复核 `/health` 200）。
- 预算：5 个演练 + 2 次冷启动重试；真实 HTTP/WS 请求仅到 `127.0.0.1:39xx`；无外部网络；未写音频到磁盘；未开麦克风；费用 0。
- 杀进程范围：**只杀本会话自己启动的 uvicorn 后端**（PID 由 3900 监听者取得），从不触碰 VoiceStudio GUI、桌面桥或其他进程。

## 2. 演练与结果（权威 artifact）

| # | 场景 | 故障方式 | 权威 artifact（时间戳尾） | 播放 | 错误码 | segment 状态 | 路由决定 |
|---|---|---|---|---|---|---|---|
| 1 | `dual_path_dead` | mode `auto`，WS(3998)+HTTP(3997) 均无监听 → 真实连接拒绝 | `…110508Z-3e3d875a72224c8e` | 0 | `transport_failed` | `segment-1=NOT_COMMITTED` | `text / continue_text_no_replay`，`replay=False`，`drop_remaining=True` |
| 2 | `http_dead` | mode `http_wav`，HTTP(3997) 无监听 → 真实连接拒绝 | `…110512Z-0b0f635345204635` | 0 | `transport_failed` | `segment-1=NOT_COMMITTED` | 同上 |
| 3 | `kill_before_audio` | 真实服务：push 后 0.4 s 杀掉后端，socket 在首块 PCM 前断开 | `…110521Z-4c321ab157814048` | 0 | `connection_closed_before_done` | `segment-1=NOT_COMMITTED` | 同上 |
| 4 | `kill_after_playback` | 真实服务：第 1 句**播放完成**后杀后端，再推第 2 句 | `…110736Z-e0f36b7385974829`（最终 `…110820Z-52085b0aa1d141f3`） | `completed_playback_sentences=1` | `connection_closed_before_done` | `segment-2=NOT_COMMITTED` | `playback_state=NOT_COMMITTED`，`replay=False`，`drop_remaining=True` |
| 5 | `proxy_drop_mid_sentence` | **受控 loopback WS fault proxy**（真实 socket）：`start` + 3 块 PCM 后直接断线、不发 `done` | `…110951Z-5dbc22f578ac4c66` | 0（但已接受 ≥1 块） | `connection_closed_before_done` | **`segment-1=PLAYBACK_STARTED`** | `playback_state=PLAYBACK_STARTED`，`replay=False`，`drop_remaining=True`，`next_reply_route=text` |

- 路由决定的完整 `reason`（artifact 内为 UTF-8）：`VoiceStudio 已开始或已完成本句边界，禁止半句换声和整句重播；当前回复转文字`。
- 每个 artifact 都记录：`played_segments`、`completed_playback_sentences`、`error/error_code`、`failure_states`、`routing_decision`、`timing_sample_count`、`late_events`、`overloaded`、`dropped_pushes`、`verified`、`finish_timed_out`、`wall_seconds`。

## 3. 逐条结论

- **第 2 项（真实 HTTP fallback 失败）——通过**：演练 1、2 均为真实连接拒绝（非模拟异常），`failure_states=NOT_COMMITTED`，路由到 `text` 且 `replay_current_segment=False`、`drop_remaining_segments=True`，0 句播放，文字保留（runner 自己持有的推送文本未被改动）。
- **第 1 项（首音前断线）——通过**：演练 3 是**真实服务被杀**，错误码 `connection_closed_before_done` 证明 socket 在 `done` 前断开；该句从未产出 PCM，因此 `NOT_COMMITTED`，允许后续另选路径，且本轮没有播放任何音频。
- **第 1 项（播放中断线）——通过（受控 proxy）**：演练 5 在**已接受音频之后**断线，`PLAYBACK_STARTED` 被如实标记，路由决定 `replay_current_segment=False`（不重播当前句）、`drop_remaining_segments=True`（停止本回复剩余语音），文字继续——即计划要求的“已出音即终态、不重播、不半句换声”。
- **演练 4 补充事实**：回复中第 1 句已完整播放（`completed_playback_sentences=1`、`timing_sample_count=1`），第 2 句失败后**没有**任何重播或换声动作；`finish()` 按设计重抛记录的错误（artifact 记录 `finish_raised`），`played_segments=0` 仅表示 `finish()` 的返回值被异常中断，**不是**“没有播过”——因此 artifact 另设 `completed_playback_sentences` 以免误读。

## 4. 过程中的三个真实发现

1. **provider 会滞留一帧**：`VoiceStudioWebSocketProvider` 在收到下一帧时才 yield 上一帧（`pending` 机制）。因此“只发一块 PCM 再断线”时该块**从未被接受**（`NOT_COMMITTED`，演练 5 的第一次运行 `…110928Z-7fe05e65dce54bb5` 即此结果）；要构造“已出音后断线”必须至少发两块（本批发 3 块）。
2. **冷启动 + 过短的句超时会误判**：后端刚重启（torch/权重加载中）时，把 `sentence_timeout_seconds` 设成 8 s 会让第 1 句以 `sentence_timeout` 失败（artifact `…110542Z-4b0adb74981d4f25` 即该情形）；runner 因此增加 warm-up 句与默认 18 s 句超时，并在等待循环里检查 `session.error` 以免空等 70 s。
3. **`finish()` 会重抛记录的错误**：这是既有设计（“仍然抛出会话记录的错误，除非用户取消”），演练 runner 必须捕获它并记录 `finish_raised`，否则会误判为 runner 崩溃。

## 5. 边界与未做

- `user_heard=null`、`speaker_acoustically_verified=false`：没有独立声学测量或用户听感确认；“播放中断线”中用户是否听到半句**未被证明**。
- 演练 3/4 的故障是**杀服务**造成的整机断线，不是网络层半开（half-open）或丢包；未做 TCP 重传/防火墙/网线级注入。
- 未做：真实 Edge（仍无实现）、默认 runtime 接线、默认模式切换、P8。
- 未覆盖：HTTP fallback **成功接管之后**再失败（第 1 项之外的组合）、并发多句取消压力。
- 未提交、未 push。

## 6. 下一入口

1. 跟踪表第 3 项（Edge 播放实现，属新功能，需单独设计与授权）；
2. 第 5、6 项（流式接入默认 runtime、默认切 `auto`）——需要用户审阅验收后再做；
3. P7 阶段验收（第 4 章五项退出条件）仍需 Edge 项与默认接线落地后才可能成立；P8 必须另行授权。
