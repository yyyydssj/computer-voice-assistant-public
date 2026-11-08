# P7 证据项 2：真实 VoiceStudio / Windows stop failure matrix 交接（2026-09-28）

## 结论

**状态：Blocked；本轮仅完成只读调查，未执行真实故障注入。**

在当前已检查的项目测试、VoiceStudio 安装/运行 API 和 Windows 设备枚举中，没有发现可安全启用且可复核的 VoiceStudio stop fault proxy/shim，也没有服务端 stop 故障模式。没有通过模拟响应、 monkey patch、替换 DLL、改服务配置、杀进程或人为破坏设备来制造真实故障。未发起音频播放或采集；未调用云端 Provider、DeepSeek、Edge TTS；费用 0 元；未修改应用代码；未提交 Git。

因此本轮没有真实 `STOP_TIMEOUT` 或 `UNKNOWN` 观测值。项目离线 fake 测试不作为真实 VoiceStudio/Windows 证据。

## 范围与边界

- 只调查 VoiceStudio 本机安装/loopback 服务、项目中既有测试和实现、Windows 音频设备/采集硬件能力。
- 允许读取 `http://127.0.0.1:3900/openapi.json`（只读 GET）；没有 POST/生成/播放/停止请求。
- 不停止、不重启、不重配现有 VoiceStudio 服务。
- 不调用云端/DeepSeek/Edge；不读取密钥或凭据。
- 不修改应用代码。唯一写入是本交接文档；没有创建摘要 artifact。
- 声学测量存在并行音频测试冲突风险，故按用户要求**没有启动播放，也没有启动任何录音/loopback 采集**。硬件盘点仅为只读枚举，不证明采集链路已验证。

## 可复核事实

### 1. VoiceStudio 当前运行状况（只读）

- 可执行文件：`<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\VoiceStudio.exe`。
- 版本：`0.5.6`；loopback backend：`127.0.0.1:3900`。
- 进程查询显示 VoiceStudio Electron 进程及 Python backend 进程在运行；TCP 3900 监听进程为 Python PID `27956`（检查时间：2026-09-28，本机时间）。
- `GET /openapi.json` 成功；枚举的相关路径包括 `/generate`、`/tasks/cancel/{task_id}`、`/workers/tasks/{task_id}/cancel`、`/batch/jobs/{job_id}/cancel` 等；未发现音频播放 stop/reset 控制 API 或 fault 注入 API。OpenAPI 是服务 API 事实，不证明本机所有 UI/内部能力均不存在。
- VoiceStudio `/generate` 属于合成请求，不等价于客户端 Windows `waveOut` stop 控制；服务端取消任务端点也不等价于 WaveOut reset ACK。

### 2. 项目已有 stop 实现和测试边界

- `<PROJECT_ROOT>\app\tts\playback_worker.py` 定义 `PlaybackState.STOP_TIMEOUT`、`UNKNOWN` 和 `WaveOutPlaybackBackend`。
- 默认 WaveOut 停止路径调用 `waveOutReset`，再等 `WOM_DONE` buffer-return callback；代码把 `STOP_ACKED` 限定为 backend buffer-return 协议确认，不宣称物理扬声器静音或用户听见。
- 同文件 `CallablePlaybackBackend.stop()` 固定返回 `unknown`，并明确是 callable/test compatibility seam，不是硬件证据。
- `<PROJECT_ROOT>\tests\tts\test_real_failure_matrix.py` 中的 `FakeStopBackend`、fake timeout/unknown case 是离线注入，证明项目契约，不是 VoiceStudio/WinMM 真实故障观测。
- `<PROJECT_ROOT>\tests\tts\test_real_failure_matrix_live.py` 文档和测试只提供显式 opt-in 的 VoiceStudio 成功 WAV / 正常 WaveOut 完成基线；其说明明确 failure cases 需要受控 fault proxy 或 provider-supported fault mode，不能伪装成真实证据。
- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-28-P7-T11-stop-default-runtime-failure-matrix.md` 与 `...\2026-09-28-P7-T12-followup-verification.md` 已把真实 VoiceStudio fault injection、真实 Windows timeout/unknown 标为 Blocked。

### 3. Windows 音频硬件只读枚举

只执行设备/API 能力枚举，无播放、无捕获：

- WinMM `waveOutGetNumDevs()`：`1`。
- WinMM `waveInGetNumDevs()`：`2`。
- PnP AudioEndpoint 状态为 OK：`扬声器 (Realtek(R) Audio)`、`麦克风阵列 (Realtek(R) Audio)`、`立体声混音 (Realtek(R) Audio)`。
- 另枚举到 Realtek 音频设备、NVIDIA High Definition Audio 与 NVIDIA Virtual Audio Device；仅为驱动/端点存在性事实。
- 项目 `.venv` 未安装 `sounddevice`、`pyaudio`、`pyaudiowpatch`、`soundcard`、`av`、`numpy`。

限制：`waveIn` 数量和 PnP 端点存在不证明某麦克风可独立并行采集、权限可用、loopback 关联正确、采样时钟同步或能产生可审计的声学测量。检测到立体声混音端点也不证明它与物理麦克风独立，不能据此给出 `speaker_output_stopped`。

## Failure matrix 结果

| Case | 本轮执行 | 真实结果 | 结论 |
|---|---:|---|---|
| VoiceStudio stop fault proxy 可行性 | 只读调查 | 在已检查的安装 API、已检查测试/实现中未发现可用的受支持 fault proxy/shim；未发现 stop/reset API | 未执行故障注入；若要构造代理需单独设计/授权，不能当作现有真实 VoiceStudio 故障功能 |
| `STOP_TIMEOUT` | 否 | 无真实观测 | Blocked：缺少安全、可控且具代表性的 WinMM/设备 fault fixture；随意阻塞或延迟 callback 会制造声音残留/设备句柄不确定风险 |
| `UNKNOWN` | 否 | 无真实观测 | Blocked：缺少经批准的真实 `waveOutReset` 错误/不可判定设备状态注入方式；Callable/fake backend 结果不合格 |
| 声学停止测量 | 否 | 无测量 | 用户要求并行期间不启动播放；也未启动录音。需有可证明独立的采集设备并安排不冲突的后续顺序授权 |

这不是“已证明不存在任何可开发代理”的结论，而是：**当前只读检查范围内没有发现现成、受支持并可安全启用的 VoiceStudio fault proxy。**

## 是否执行及改动清单

- 只读：项目测试/实现、既有 P7 交接、VoiceStudio OpenAPI、运行进程/端口、Windows PnP/WinMM 设备数量、Python 音频模块存在性。
- 未执行：VoiceStudio POST、TTS 合成、播放、录音、waveOut reset、故障注入、服务停止/重启、配置修改、应用代码测试运行。
- 文件改动：仅本文件 `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-28-P7-evidence-stop-failure-handoff.md`。
- Git：没有 add/commit/push；仓库已有大量 staged/untracked 基线状态未触碰。

## 后续推进建议 / 解阻条件

1. 先单独决定测试对象：要证明的是 VoiceStudio HTTP/任务取消故障，还是本项目 Windows `waveOutReset` stop backend 的 `STOP_TIMEOUT`/`UNKNOWN`。两者是不同层，不能互相替代。
2. 若目标是 VoiceStudio 服务故障，需由 VoiceStudio 支持的测试模式/受控代理明示注入点；代理必须 loopback-only、可一键旁路/恢复、具有独立日志和请求 ID，并验证恢复后 `/health` 正常。不得改生产运行服务或假称真实原生故障。
3. 若目标是 WinMM `STOP_TIMEOUT`/`UNKNOWN`，需批准独立测试进程中的受控设备/shim 方案、单次播放时长上限、超时恢复与设备清理步骤；如测试会发声，必须与所有并行声音测试错开并另行获得顺序执行授权。`UNKNOWN` 若只能通过 mock 返回错误，只能标为 fake contract，不可提升为真实设备证据。
4. 声学测量需先证明采集源独立于被测输出（例如独立物理麦克风/录音设备，而非未经验证的 stereo mix），确认权限、设备名、通道和时间同步；并行阶段禁止播放。若无法证明独立性，待并行声音测试结束后申请一次明确的顺序测量授权。
5. 取得上述安全 fixture/授权后，分别记录 `backend_stop_ack`、`speaker_output_stopped`、`user_heard`；任何一个不得由另一个推断。实际失败即停止扩大范围，保留原始错误事实和文字回退。
6. 在真实 `STOP_TIMEOUT` 与 `UNKNOWN` 分别有可复核证据之前，P7 保持 `Blocked`，P8 不进入。

## 阶段状态

```text
Offline fake stop matrix contract       已有；非真实设备证据
VoiceStudio stop fault proxy            当前未发现可用现成方案；Blocked
真实 VoiceStudio stop failure matrix     未执行；Blocked
真实 Windows STOP_TIMEOUT               未执行；Blocked
真实 Windows UNKNOWN                    未执行；Blocked
独立声学采集能力                         只读盘点；未验证并行独立采集
声学播放/采集                           未执行（遵守并行冲突限制）
P7                                      Blocked
P8                                      未进入
费用                                    0 元
```
