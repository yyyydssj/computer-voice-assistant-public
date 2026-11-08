# P7 VoiceStudio 双路径 Batch 3：WebSocket 客户端与真实协议验证

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**Batch 3 完成（离线实施 + 真实三句串行协议 preflight 通过）；PCM 播放仍未接入**
- 范围：`/ws/tts` 同步客户端与严格协议状态机；只输出 Batch 1 `PCMChunk`，不接入播放、不接入默认 runtime。

## 1. 本批次目标与边界

Batch 3 实现 VoiceStudio v0.5.6 `ws://127.0.0.1:3900/ws/tts` 的同步、可取消、无自动重试客户端：惰性建连、跨句复用同一 socket、单连接单在途请求、session idle 释放、`start`/binary/`done`/`error` 协议状态机、超时与大小上限、取消即关 socket。接收到的 PCM 只映射为 Batch 1 `PCMChunk`，交给调用方；本批不实现 PCM 播放后端、双路径自动降级、barge-in 或默认模式切换。

## 2. VoiceStudio 协议核对（本机 v0.5.6 源码，只读）

依据本机已安装源码 `<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\backend\api\routers\tts_stream.py`（477 行）：

- 路由：`@router.websocket("/ws/tts")`；`accept()` 后循环 `receive_json()`，连接跨请求保持。
- 客户端请求 JSON：`text`、`voice`（profile_id）、`language`、`speed`、`engine`，以及本项目未使用的 `instruct`/`description`/`emo_*` 字段。
- 服务端顺序：可选一次性 `routing` JSON → 首句合成完成后发 `start`（`sample_rate`、`channels`、`format="pcm16"`、`engine`）→ 一个或多个 binary PCM16 LE 块（`CHUNK_SAMPLES=4800`，24 kHz 下约 200 ms）→ `done`（`duration_s`、`gen_time_s`、`ttfa_ms`、`rtf`、`samples`、`sample_rate`、`engine`）。
- **`start` 在首句整句合成之后才发送**：client 在 `start` 之前的等待只能由单句总预算约束，不能用帧间隔超时截断（见第 7 节缺陷记录）。
- `error` 帧：`{"type":"error","detail":...}`，**发送后不关闭连接**，继续等待下一请求（`continue`）。

## 3. 依赖固定

- 包：`websockets==17.1`
- wheel：`.wheelhouse\websockets-17.1-cp312-cp312-win_amd64.whl`（217,786 bytes）
- SHA-256：`617243E19A0992095956F406EE9CD3BC4BA92862D83CB1D83BB59CE574412BEC`
- 元数据：`License-Expression: BSD-3-Clause`、`Requires-Python: >=3.11`、无 `Requires-Dist`（零依赖）
- 安装方式：`pip install --no-index --find-links .wheelhouse websockets==17.1`，安装到项目 `.venv`（Python 3.12.10）。
- 客户端使用 `connect(..., legacy=True)`：一个 socket 被刻意跨句复用并由 provider 自身生命周期关闭，因此不采用 context-manager 形式。

## 4. 实现文件与行为

文件：`app/tts/providers/voicestudio_ws.py`

- `VoiceStudioWebSocketConfig`：endpoint 仅允许 `ws`/`wss` 且 host 为 `localhost` 或 loopback IP，拒绝远端 host、用户信息、query、fragment、内嵌路径与非法端口；`path` 必须为不含 query/fragment 的绝对路径；connect 2.0 s、单句 18.0 s、帧 idle 3.0 s、session idle 30.0 s、单帧与累计音频各 16 MB，全部要求为正数。
- `WebSocketConnection` Protocol 与注入式 `ConnectionFactory(url, open_timeout, max_size)`：默认实现才延迟导入 `websockets`，因此模块在未安装依赖时仍可导入，测试不触网。
- 协议状态机：`start` 之前出现 binary 即失败；`format != "pcm16"`、`sample_rate != 24000`、`channels != 1`、重复 `start`、未知/缺失 `type`、非 JSON 文本帧全部 fail-closed。
- `done` 校验：必须先有 `start`；`samples` 缺失或非非负整数即失败；`samples` 与实收 PCM 样本数不一致即失败；完全没有音频时失败。
- 音频边界：空帧、奇数字节帧、超过单帧上限、累计超过总量上限均失败。
- PCM 映射：每个 binary 帧映射为一个 `PCMChunk`，sequence 从 1 连续递增；采用延迟一块策略，最后一个实际音频块才标 `is_final=True`，不制造空 final 块；身份继承 request 的 reply/segment/request/generation。
- 超时语义：收到 `start` **之前**的单次 recv 等待上限为单句剩余预算；收到 `start` **之后**才启用帧间隔 idle 上限。两种情况的超时分别映射为 `sentence_timeout` 与 `frame_idle_timeout`。
- 取消：请求前、接收中均有检查；取消后立即关闭 socket 并抛出脱敏错误，不提交新的 chunk；`done` 之后的取消不影响已完成流。
- 生命周期：惰性建连、跨句复用、`maybe_close_idle()` 在空闲超阈值且无在途请求时释放 socket、下一次请求前也会先做空闲释放；`close()` 幂等；协议失败、传输失败、取消或生成器被提前关闭都会丢弃 socket，下一句重新建连，坏连接绝不复用。
- 并发：同一 provider 同一时刻只允许一个在途请求，第二个请求立即以 `concurrent_request_not_allowed` 拒绝，不做排队或流水线。
- 错误脱敏：所有对外错误只暴露短代码；socket 异常文本、服务端 `detail`、连接失败原因都不进入错误消息。

文件：`app/tts/providers/__init__.py` — 导出 `VoiceStudioWebSocketConfig`、`VoiceStudioWebSocketError`、`VoiceStudioWebSocketProvider`。

## 5. 离线测试

文件：`tests/tts/test_voicestudio_ws.py`（37 项）

- 配置边界：默认 loopback URL、远端 host、凭据/query/fragment、非 ws scheme、内嵌路径、非法端口、非正超时与上限、IPv6 loopback、非法 path。
- 协议：正常两句块与 final 标记、identity 继承、请求 JSON 字段（含 `engine` 可选）、`routing` 容忍、start 前 binary、三类非法 start 元数据、重复 start、未知 type、非 JSON、`samples` 不匹配与缺失、`error` 后停止音频且连接被关闭、`done` 早于 `start`、空音频、空帧、奇数帧、单帧与累计上限、缺 `done` 断线、连接失败不泄露原因。
- 超时：帧 idle 超时、单句预算耗尽、recv 超时不超过配置预算、**start 前等待使用单句预算而非帧 idle 上限**、**首个 recv 超时归入单句预算**。
- 取消：请求前取消不触碰传输、接收中取消关闭 socket 且停止输出、`done` 后取消保留完整流。
- 生命周期：三句串行复用同一连接、第二个在途请求被拒、`maybe_close_idle()` 释放与重连、空闲间隔先释放再复用、协议失败丢连接后重建、`close()` 幂等并拒绝后续请求、参数类型校验。
- loopback 集成：进程内 `websockets.sync.server` 起 `127.0.0.1` 临时端口 fake `/ws/tts`，验证三句串行只建立 1 个连接、请求字段与 PCM 字节统计一致；以及服务端在 `done` 之前关闭连接时 fail-closed。

## 6. 真实协议 preflight（已执行）

条件：VoiceStudio v0.5.6 本机运行（`/health` 200，`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`），确认音色 `e8572e8e`（speed 1.0，`design` 类型，参考音频 sha256 与本地选择一致）。

执行方式：用本批交付的 `VoiceStudioWebSocketProvider`（**默认配置**：connect 2 s / 单句 18 s / 帧 idle 3 s / session idle 30 s）对 `ws://127.0.0.1:3900/ws/tts` 发 3 句固定非敏感文本，串行、只收集 PCM 到内存，**不播放、不落盘音频**。

结果（batch_id `p7-batch3-ws-preflight-default-84c7b9344590edee`，artifact 见第 8 节）：

| 句 | 字符 | chunks | samples | 音频 | 首块延迟 | 总耗时 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 11 | 9 | 39,840 | 1.66 s | 1688 ms | 1688 ms |
| 2 | 15 | 12 | 54,000 | 2.25 s | 1562 ms | 1562 ms |
| 3 | 13 | 11 | 49,680 | 2.07 s | 1625 ms | 1641 ms |

- **连接数 = 1**：三句严格串行复用同一 socket。
- 每句 `is_final=True`，且 `done.samples` 与实收 PCM 样本数完全一致（不一致会被 provider 判为 `samples_mismatch`）。
- 未发生播放、未写任何音频文件、未产生 `user_heard` 结论。

Batch 3 退出条件（真实 VoiceStudio 至少三句串行、连接复用正确、PCM 格式与样本统计一致）**已满足**。

## 7. 真实环境发现的缺陷与修复

**缺陷：首个 recv 被帧间隔超时截断。** 修复前的默认配置在真实 VoiceStudio 上第 1 句必然失败：实测首个 binary 在 2218 ms 后到达，而客户端对首个 recv 应用了 3 s 帧 idle 上限并计入等待，因此在 `start` 到达前即判 `frame_idle_timeout`（实测 `elapsed_ms=3344`）。

修复：`_recv_timeout(deadline, *, audio_started)` —— `start` 之前的 recv 等待上限改为单句剩余预算；`start` 之后才使用 `min(frame_idle, remaining)`。同时新增 2 项测试固定该语义，并修正计划文档第 7.4 节的超时说明。

修复后同一默认配置在真实 VoiceStudio 上 3/3 句成功（第 6 节）。

## 8. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest tests.tts.test_voicestudio_ws -q
37 tests, OK            (修复首帧超时语义后复跑)

.\.venv\Scripts\python.exe -m unittest discover -s tests/tts -p "test_*.py" -q
385 tests, OK (2 skipped)    (修复首帧超时语义后复跑)

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
884 tests, OK (2 skipped)    (修复后复跑)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

9 个本批新增/修改文件严格 UTF-8 解码通过，无 BOM、无 `U+FFFD` 替换字符。

证据 artifact：`implementation_plan/checkpoints/artifacts/2026-09-30-P7-batch3-ws-preflight/p7-batch3-ws-preflight-default-84c7b9344590edee.json`（含默认配置成功结果、修复前缺陷复现、放宽 idle 的中间证据）。

## 9. 真实链路阻断项（已修复，属 Agent→说话链路）

真实 preflight 过程中发现 `<USER_HOME>\AppData\Local\ComputerVoiceAssistant\xiaobing_voice_selection.json` 有两个问题：

1. 文件带 **UTF-8 BOM**，`read_selection()` 的 `read_text(encoding="utf-8")` + `json.loads` 抛错；
2. `"speed":1` 为 **int**，而 `VoiceSelection.validate()` 要求 `type(speed) is float`。

两者任一都使 `read_selection()` 失败，导致 `CompletedReplySpeaker.start_stream()` 返回 `None`、`CompletedReplySpeaker.__call__` 置 `_disabled=True` —— **真实说话链路无法产生任何音频**（与本次 WS 修复无关）。

处理：备份原文件为 `xiaobing_voice_selection.json.bak-20260930`，随后原子替换为同值文件（去掉 BOM、`speed=1.0`）。修复后 `read_selection`、`verify_reference`、`verify_provider` 三项全部通过。

未放宽 `private_selection.py` 的任何校验（BOM 与类型问题被判定为数据文件问题，不是契约该放宽的部分）。

## 10. 未执行与遗留风险

- 本批仍未接入 PCM 播放后端，因此真实 preflight 只证明「客户端 + 协议 + 样本统计」，**不代表扬声器出声或用户听到**。
- 真实 VoiceStudio 的 `error` 帧路径、断线注入、取消注入未在真实服务上演练（仅离线测试覆盖）。
- 冷启动首句耗时未单独测量（本次 `/health` 已就绪，首句 1688–2218 ms 属 warm 状态）。
- WS→HTTP 自动降级（Batch 5）、barge-in/迟到块隔离（Batch 6）、真实设备与性能（Batch 7）均未开始。
- `TTS_VOICESTUDIO_MODE` 默认仍为 `disabled`，本批没有任何代码读取 WS 分支进入默认 runtime。
- 工作区仍包含大量既有 staged/untracked 文件；本轮未执行 `git add .`、commit 或 push。

## 11. 下一入口

1. 用户审阅本批 diff；
2. 之后单独授权 Batch 4（PCM 流式播放后端），或先处理 Agent→说话链路的其余两处问题（ASR 路径未接逐句播报、TTS 背压阻塞 Agent 线程）。
