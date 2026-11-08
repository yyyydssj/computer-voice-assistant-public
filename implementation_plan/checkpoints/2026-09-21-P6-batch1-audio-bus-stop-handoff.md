# P6 批次 1：AudioFrame 与 AudioBus 停止交接

- 日期：2026-09-21
- 阶段：P6 ASR 按键输入
- 批次状态：**complete**
- 阶段状态：**in_progress**
- 当前停止点：**P6 批次 2 门前；尚未接入 sounddevice、麦克风或 ASR Provider**

## 1. 接续、授权与本批次边界

严格接续 `implementation_plan/checkpoints/P5-handoff.md`。用户于 2026-09-21 明确要求直接开始下一批次、无须再次授权，因此只进入路线图 P6 批次 1。

本批次唯一目标是建立内存中的 `AudioFrame` 元数据契约和有界 `AudioBus`，把采集 callback 与未来 ASR 解码循环隔离，并让旧设备、旧流、丢帧、重复帧和溢出 fail-closed。

明确未做：

- 未导入或调用 `sounddevice`、PortAudio、麦克风、扬声器或真实音频设备；
- 未实现设备枚举/选择、pre-roll、临时音频文件、VAD、ASR adapter、ResultGate 或 Agent 请求提交；
- 未接云端 ASR、Windows 语音服务、TTS、连续监听、唤醒词、声纹、AEC、实时打断或语音 L3；
- 未使用 `.env` 中的凭证，未启动 Harness runtime，未发送网络请求，未执行电脑副作用；但首次查找项目指令文件时，PowerShell 通配筛选异常地把根目录 `.env` 内容输出到本次受控工具日志，未复述、未复制到项目文件、未用于任何调用；应轮换其中凭证；
- 未执行 Git add、commit 或 push；仓库仍无首次 commit。

## 2. 已完成实现

### 2.1 AudioFrame 契约扩展

在既有 P2 `AudioFrame` 身份字段基础上增加并严格校验：

- `device_epoch`：设备代际，旧设备回调不能进入当前总线；
- `sample_rate_hz`、`channel_count`、`sample_format`；
- `sample_count`、`payload_size_bytes`；
- `sample_format` 当前只允许固定的 `pcm_s16le`；
- `duration_ms` 必须与采样率和采样数精确一致；
- PCM 字节数必须与采样数、声道数和 16-bit 样本宽度一致。

PCM payload 不进入 `StrictContract` JSON，不进入日志、审计或持久化契约；`AudioFrame` 继续保持 frozen/slotted，并为既有 P2 构造调用提供 16 kHz、单声道、20 ms 的兼容默认值。

### 2.2 AudioBus

新增 `app/audio/bus.py` 与公开入口 `app/audio/__init__.py`：

- 总线固定绑定 `audio_session_id + provider_generation + stream_id + device_epoch`；
- 只接受从 event sequence 1、frame index 0 开始的严格连续帧；
- 跨 audio session、旧 generation、旧 stream、旧 device epoch 只返回诊断决策，不污染当前队列；
- payload 类型/长度错误不消费序号，调用方可用正确 payload 重新提交同一帧；
- 重复、跳号或乱序帧视为连续性破坏，立即清空半截音频并进入 `FAILED`；
- 有界队列溢出不丢最旧帧、不阻塞 callback、不悄悄续传，而是清空并进入 `FAILED`；
- mutable payload 在入队时复制为 `bytes`，防止采集缓冲复用后篡改已入队音频；
- 正常完成可关闭写端并保留完整队列供消费者排空；取消/故障关闭可显式丢弃队列；
- 模块只使用标准库内存结构和锁，无文件、网络、进程、命令或设备入口。

## 3. 测试与验证

新增 `tests/audio/test_bus.py`，覆盖：

- AudioFrame 严格往返、冻结属性、格式/长度/时长拒绝；
- 连续帧接纳和 mutable payload 防复用复制；
- 跨 session/generation/stream/device epoch 拒绝；
- 重复、跳号、乱序导致 fail-closed 并丢弃半截音频；
- payload 长度错误不消费序号；
- 队列溢出 fail-closed；
- 正常关闭保留完整队列，取消关闭清空队列，关闭后拒绝新写入。

最终结果：

- P6 批次 1 专项：8/8；
- P2 契约与状态聚焦回归：39/39；
- 全量 unittest：287/287；
- `compileall -q app tests`：通过；
- 严格 UTF-8：107 个 Python 文件均可解码，无 `U+FFFD`；
- 既有 BOM 债务仍为 2 个文件：`tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`；本批次未新增 BOM；
- `app/audio/` AST 执行面审计：无文件、网络、进程、命令、解释器或音频设备导入/调用。

## 4. 已观察错误与修复

| 错误 | 尝试次数 | 解决方案 |
|---|---:|---|
| 嵌入式 Python 的默认 unittest discover 未把项目根加入 `sys.path` | 1 | 沿用 P5 已确认的显式 `sys.path.insert(0, repo_root)` runner；未改系统或项目环境配置 |
| 红测在正确 runner 下因 `app.audio` 尚不存在而失败 | 1 | 确认真正红测后才写最小实现 |
| 首轮最小实现漏导入新增校验使用的 `ContractError` | 1 | 补齐单行导入，专项从 7/8 恢复为 8/8，并重跑聚焦与全量回归 |
| 查找 AGENTS/CLAUDE/GEMINI 指令文件时 PowerShell -Include 未按预期限缩，意外输出根目录 .env | 1 | 立即停止依赖该输出；未使用或复述凭证，后续使用精确 Test-Path/Get-Content -LiteralPath；建议用户轮换已暴露到本次工具日志的凭证 |

## 5. 已知限制与延期

- `AudioBus` 当前是进程内单生产者/单消费者友好的同步原语，不是跨进程队列；不持久化，也不恢复旧音频；
- 当前 PCM 契约只开放 `pcm_s16le`；其他格式必须在未来通过明确转换或扩展契约接入，不能默认为兼容；
- 20 ms、16 kHz、单声道只是当前兼容默认值，不等于最终设备 callback 粒度或 ASR 解码块大小；后续仍须按路线图分离并真机校准；
- 尚未建立真实采集 lifecycle、设备状态、pre-roll 或临时资源清理；
- 尚未建立 `utterance_id`、VAD、ASR 事件、final 接纳或请求提交链；
- 两个既有测试 BOM 文件未在本批次范围内修复。

## 6. 下一入口与强制停止

下一批次只能是 **P6 批次 2：sounddevice 采集、设备选择、pre-roll 与临时资源清理**。开始时至少读取：

1. 本交接；
2. 路线图 P6 卡片；
3. `implementation_plan/03-audio-capture.md`；
4. `implementation_plan/shared/data-and-privacy.md`；
5. 当前 `app/audio/bus.py` 与 `tests/audio/test_bus.py`。

批次 2 必须继续先写拒绝路径测试，并保持以下边界：

- 不进入 P6 批次 3–6；
- 不接 VAD、sherpa-onnx/SenseVoice、ResultGate 或 Agent 请求；
- 不接 TTS、连续监听、唤醒词、声纹、AEC、实时打断或语音 L3；
- 不静默切换输入设备，不把不完整音频提交给后续模块；
- 普通实时音频默认只在内存中处理；仅 Provider 明确要求文件时才允许进入受控临时文件路径，但批次 2 不得借此扩大持久化范围。
