# P7 阶段级验收：TTS 输出与人物表达（2026-09-30 最终）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 结论：**P7 五项路线图退出条件全部有可复核证据 → P7 阶段级验收通过（`P7 complete`，阶段级）**。
- 停点：**不接"播放时监听"；未进入 P8**（P8 组合闭环必须另行授权）；真实智能路由（R-25/R-26）仍为 `NOT READY / STOP`，属独立支线，与本次 P7 验收无关。
- 本验收替代并收口 2026-09-28 的 [T-06 阻塞记录](2026-09-28-P7-T06-stage-acceptance.md)（当时五项均未通过：Edge 无实现、断线/HTTP 失败未演练、p95 未采集、默认接线未做）；当时的阻塞项已于 2026-09-30 全部处置，见下。

## 1. 范围与口径

- 只验收路线图 P7 的五项退出条件 + 阶段级回归/故障/环境/隐私/停止条件；**不验收 P8**，不改变任务执行、授权、账本、审计语义。
- 证据分两类，分开标注：**真实链路**（真实 VoiceStudio `0.5.6` / 真实 Windows waveOut / 真实本机 SAPI / 真实 socket）与**离线契约**（fake provider/backend、注入时钟）。
- 一切"设备完成播放"与"用户听见"分开：只有 `user_heard=true` 的那一次是人为确认，其余均为设备事实。

## 2. 路线图五项退出条件判定

| # | 退出条件（路线图第 490–495 行） | 判定 | 证据 |
|---|---|---|---|
| 1 | 文字回复可按原序播放 | ✅ 通过 | **真实**：B9 paced 三模式各 20/20 播完、顺序与分句一致（[B9 真实采集](2026-09-30-P7-b9-real-p95-measurement.md)、[19.2.3](2026-09-29-voicestudio-dual-path-streaming-execution.md)）；Batch 5 真实三句 WS 流式（[checkpoint](2026-09-30-P7-voicestudio-batch5-transport-and-config.md)）；Batch 7 句长矩阵 4/16/32/48 字 → 1/1/1/2 句、5 句→5/5（[checkpoint](2026-09-30-P7-voicestudio-batch7-real-scenario-matrix.md)）；**用户听感**：主路径 1 句完整播放后确认听到（[确认件](artifacts/2026-09-30-P7-user-heard/2026-09-30-P7-user-heard-confirmation.json)）。**离线**：`test_pcm_playback` 35 项（FIFO/顺序/身份）、`test_streaming_session` 26 项 |
| 2 | 未提交播放设备前可按配置切 Edge；播放已开始后不在当前 segment 换声 | ✅ 通过 | **真实**：[B11](2026-09-30-P7-b11-offline-edge-voice.md) `edge_fallback_after_kill`——真实杀掉 VoiceStudio（首块 PCM 前）→ `route=edge/use_edge_current_segment`、`edge_attempts=1`、`edge_used@generation1`、该句由离线 Edge 声线**真的播出**；同批 `proxy_drop_no_edge`——已接受音频后断线 → `PLAYBACK_STARTED`、**`edge_attempts=0`**、转文字、不重播。**离线**：`test_streaming_edge_fallback` 16 项、`test_routing::test_reply_boundary_prevents_mid_reply_provider_switch`（10 项模块）、`test_orchestration::test_edge_only_on_new_reply_after_explicit_user_selection`（14 项模块） |
| 3 | 被打断的旧回复不跨 Provider 续播 | ✅ 通过 | **真实**：Batch 7 播放中取消（11 次 `waveOutWrite` 后 1 秒仍为 11 次，`late_events=["playback:STOP_ACKED@generation2"]`）；[B10](2026-09-30-P7-batch10-real-transport-faults.md) `proxy_drop_mid_sentence`（`PLAYBACK_STARTED` → `replay_current_segment=False`、`drop_remaining_segments=True`）；[B11](2026-09-30-P7-b11-offline-edge-voice.md) 已出音不切 Edge。**离线**：Batch 6 generation fence + 迟到块只进诊断（`test_streaming_session` 26 项）、`test_routing_binding` 46 项（边界丢弃且不改状态） |
| 4 | TTS 故障不改变任务、授权、计划和结果事实 | ✅ 通过 | **结构**（本验收脚本，28 个文件、0 解析失败）：`app/tts/**` 对 `app.plans/executor/auditing/verification/runtime/recovery/retry/state/tools` 的引用数 = **0**；唯一外部类型依赖是只读的 `from app.agent.adapter import AgentTurnOutcome`（`app/agent/adapter.py` 自身也不引用安全链、无 subprocess/os.system）。**离线**：`test_completed_reply::test_selection_failure_disables_instance_without_changing_text`；TTS 故障矩阵（`test_real_failure_matrix` 20 项 + `test_streaming_edge_fallback` 16 项）只改 TTS 局部状态。**真实**：B10/B11 全部失败场景均为"文字继续、语音停止"，未触发任何任务/授权/账本路径 |
| 5 | VoiceProfile 变更只在允许的句子/回复边界生效 | ✅ 通过 | **离线契约**：`test_orchestration::test_profile_drift_fails_closed_at_next_sentence`、`test_profile_drift_during_synthesis_prevents_device_commit`；[B9-fix](2026-09-30-P7-b9-defect-fixes.md) 的 `_verify_once()` 把身份验证固定为**每次回复一次**（回复边界即允许边界）；`private_selection.validate()` 强制 owner/version/profile/speed/参考音频 SHA-256（`test_real_playback` 29 项，含 `rejects_tampered_owner`、`fail_closed_on_bad_version_speed_reference_and_extra_fields`、`existing_selection_never_overwritten`）。**真实**：live 选择文件从未被 TTS 写入（`speed=1.0`、mtime 停在 Batch 5 重建时刻） |

## 3. 阶段级回归、安全与结构性审计

- 全量回归（2026-09-30 复跑）：`unittest discover -s tests -t . -p "test_*.py" -q` → **1107 tests, OK (skipped=3)**；`compileall -q app tests tools` → exit 0；`git diff --check` → exit 0。
  - **数字更正（2026-09-30 复核时发现）**：此前各批次记录的"全量 719/773/983/1019/1065"是**当时被 `unittest discover` 收集到的数量**；`tests/evidence/`（39 项）与 `tests/recovery/`（3 项）因缺 `__init__.py` 从未被收集。补齐后全量为 **1107**，新纳入的 42 项全部通过。详见 [`2026-09-30-P0-P6-cross-stage-recheck.md`](2026-09-30-P0-P6-cross-stage-recheck.md) 第 3 节。
- 关键专项：`test_pcm_playback` 35/35、`test_streaming_session` 26/26、`test_routing_binding` 46/46、`test_real_playback` 29/29、`test_orchestration` 14/14、`test_routing` 10/10、`test_completed_reply` 11/11、`test_streaming_edge_fallback` 16/16、`test_edge_sapi` 30/30（1 skip：本机无权限建目录符号链接）。
- P2/P3/P5 安全回归：未改动 `app/plans`、`app/executor`、`app/auditing`、`app/verification`、`app/runtime`、`app/recovery`、`app/retry`、`app/state`、`app/tools`；全量套件包含这些阶段的既有用例并全部通过。
- 结构性审计（本验收新增，可复现）：
  - `app/tts/**` 网络导入仅 **loopback**（`private_selection`/`http_wav`/`real_playback` 的 urllib、`voicestudio_ws` 的 urllib+websockets），端点校验强制 loopback 且拒绝带凭据/query/fragment 的 URL；
  - 进程/设备调用：`waveout_pcm`、`playback_worker` 的 `ctypes`（winmm，既有）；`real_playback` 的 `subprocess`（既有 live 路径）；**新增** `providers/edge_sapi.py` 的 `subprocess`（固定纯 ASCII 脚本、参数列表、无 `shell=True`、stdin=DEVNULL、`CREATE_NO_WINDOW`、超时回收、临时文件必清理）；
  - 敏感面命中 10 处全部是**防御性**语义：拒绝含 username/password/query/fragment 的 URL、注释"从不打印文本/音频/响应体/凭据"、`secrets.token_hex` 生成 reply id。
  - 4 个既有文件带 UTF-8 BOM（`fault_fixtures.py`、`playback_worker.py`、`providers/contracts.py`、`providers/fakes.py`），Python 导入无碍；审计脚本按 `utf-8-sig` 解码后 0 解析失败。

## 4. 故障注入与降级（真实）

| 场景 | 结果 | 来源 |
|---|---|---|
| WS 建连失败 → HTTP 一次性接管 | 真实降级成功并完成播放 | Batch 5 阶段 B |
| 双路径均真实连接拒绝 | `NOT_COMMITTED` → `text/continue_text_no_replay`，0 句播放 | [B10](2026-09-30-P7-batch10-real-transport-faults.md) |
| 真实杀掉 VoiceStudio（首块 PCM 前） | `connection_closed_before_done` → `NOT_COMMITTED` → 文字；**配置了 Edge 时改由离线 Edge 声线真的播出** | B10 / [B11](2026-09-30-P7-b11-offline-edge-voice.md) |
| 已出音后失败（受控 proxy / 杀服务） | `PLAYBACK_STARTED` → `replay_current_segment=False`、`drop_remaining_segments=True`、不换声、不重播 | B10 / B11 |
| 播放中取消 | 取消后 1 秒内无新 `waveOutWrite`；`late_events` 只记诊断 | Batch 7 |
| 后端重启恢复 | 恢复只影响下一轮；冷启动首句 1.97–5.3 s | Batch 7 |

## 5. 工程性能指标（双路径计划 19.2，真实数据）

| 模式 | `ready_to_submit` p50/p95 (ms) | `gate_to_device` p95 (ms) | `submit_to_first_pcm` p50/p95 (ms) |
|---|---|---|---|
| `ws_pcm` | 0.0919 / **0.2357** | **0.0241** | 1446.8 / 1580.0 |
| `http_wav` | 0.0985 / **0.1734** | **0.0290** | 1488.7 / 1614.6 |
| `auto` | 0.1083 / **0.1999** | **0.0155** | 1462.3 / 1739.9 |

- 四条指标全部通过：① p95 < 20 ms ✅；② 除起播缓冲外 p95 < 50 ms（用 `gate_to_device`）✅；③ `auto` 相对 `http_wav` 中位起播延迟不恶化 >10% ✅（p50 1462.3 vs 1488.7 ms；p95 恶化 7.8% 已如实登记）；④ **不声称 WebSocket 加速**（`ws_pcm` p50 仅快 2.8%）。
- 口径纪律：Windows `time.monotonic` 分辨率 15.625 ms 不可用于这些跨度，真实采集必须用 `perf_counter`（已设为默认时钟）；`first_pcm_to_device` 含起播缓冲，只作诊断。

## 6. 用户听感

- 2026-09-30 真实播放两句：主路径（VoiceStudio 小饼声线）1 句完整播放 5.395 s；备用声线（离线 Edge 槽）32 块 `COMPLETED` 6.666 s。用户确认**两句都听到** → [`user_heard=true` 确认件](artifacts/2026-09-30-P7-user-heard/2026-09-30-P7-user-heard-confirmation.json)（原始播放件未修改）。
- 边界：**一次人为确认**，不是声学测量（`speaker_acoustically_verified=false`），不得推广到其它文本/音量/设备/环境。

## 7. 环境、版本、预算、隐私与停止条件

- 环境：Windows + `.venv` Python 3.12；VoiceStudio `0.5.6`（`/health` 200，CUDA RTX 3050 Laptop）；离线声线 `Microsoft Huihui Desktop`（zh-CN）。
- 依赖：唯一新增第三方依赖 `websockets==17.1`（wheel SHA-256、BSD-3-Clause、离线安装，Batch 8）；B11 未新增依赖。
- 预算：本阶段真实请求仅 loopback（`127.0.0.1:3900` 等）与本机 SAPI；无外部网络、无费用、音频不落盘、未开麦克风；曾真实杀掉/重启**本会话自己启动的** uvicorn 后端（未触碰 GUI、桌面桥或其它进程）。
- 隐私/日志：诊断只含脱敏 id、计数与毫秒值；不含句文本、PCM、路径、URL；`public_summary` 不暴露 URL；错误只用短代码。
- 停止条件：设备 stop 未确认的实例禁后续语音；fail-closed 不伪造 ACK；`STOP_ACKED` 只表示 backend 归还 buffer，不等于扬声器静音或用户听见。

## 8. 已知限制（不推断、不外推）

- **Edge 槽是离线本机 SAPI 声线，不是微软 Edge 神经声线**；在线 edge-tts 未实现（属另一批）。
- 真实智能路由（DeepSeek 真 Provider、R-25/R-26）仍 `NOT READY / STOP`，与 P7 无关。
- 断线注入为"杀服务/受控 proxy"级别，未做 half-open/TCP 丢包级注入；Edge 渲染是"先整段 WAV 再分块 yield"，长文本渲染中途取消时延未测量。
- 未做：播放期间监听/AEC/全双工打断（路线图明示的下一阶段之前不接）；P8 组合闭环；并发多句压力。
- 默认 runtime 未接线、默认模式保持 `disabled`（用户 2026-09-30 决定）：语音仍为 opt-in；因此"默认说话链路"未被验收，本轮只验收**显式启用的流式/双路径/Edge 路径**。

## 9. 结论与停点

- **P7 阶段级验收通过**：路线图五项退出条件全部有真实或可复核契约证据；阶段回归 **1107 项全绿**（含复核时补回的 42 项 evidence/recovery 用例）；故障注入、性能指标、用户听感、环境/预算/隐私/停止条件均已记录。
- **停点**：不接"播放时监听"；**不进入 P8**（须另行授权）；不因本验收启用默认语音。
- 后续可选（均需单独授权）：流式会话接入默认 runtime（tracker 第 5 项，暂缓）、在线 Edge TTS、R 支线真实路由、P8 组合闭环。

## 10. 复核命令

```powershell
cd <PROJECT_ROOT>
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q      # 1107 tests, OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools                          # exit 0
git diff --check                                                                     # exit 0
:: 结构性审计（本验收脚本，仓库外）：扫描 app/tts/** 的安全链引用 / 网络 / 进程调用
```
