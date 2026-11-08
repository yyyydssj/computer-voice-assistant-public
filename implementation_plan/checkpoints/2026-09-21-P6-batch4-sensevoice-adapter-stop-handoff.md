# P6 批次 4：sherpa-onnx/SenseVoice adapter

- 日期：2026-09-21
- 阶段：P6 ASR 按键输入
- 批次状态：**complete**
- 阶段状态：**in_progress**
- 当前停止点：**P6 批次 5 门前；尚未接入 ResultGate、final 接纳/去重、Agent 请求、TTS 或文字模式降级**

## 1. 接续与范围

严格接续 `implementation_plan/checkpoints/2026-09-21-P6-batch3-vad-stop-handoff.md`。本批次只实现本机 `sherpa-onnx + SenseVoice` 的惰性窄适配层：加载 Provider、创建单次 ASR session、接收已绑定 AudioBus 边界的 PCM16LE 帧，并在结束时生成带边界身份和能力声明的 provider 事件。

本批次没有实现 final 接纳闸门、interim 丢弃策略的下游编排、事件去重/迟到处理、ResultGate、Agent 请求、TTS、连续监听、唤醒词、声纹、AEC、实时打断、云端 ASR 或真实麦克风访问。

## 2. 已完成实现

### 2.1 `app/asr/__init__.py`

新增最小 `SenseVoiceRecognizerAdapter`：

- Provider 与模型加载惰性发生，仅在显式 `load()` 时导入 `sherpa_onnx`；模块导入本身不访问模型、设备、网络或 `.env`；
- 使用 `OfflineRecognizer.from_sense_voice(...)` 创建 recognizer，并把模型路径、tokens 路径、线程数、采样率、feature 维度、provider、language、ITN 作为显式配置传入；
- Provider 缺失、工厂缺失、初始化异常统一映射为 `AsrProviderUnavailable`，并将 adapter 置为 `FAILED`；失败后要求新建 adapter，不循环重试；
- `SenseVoiceConfig` 当前只接受 16 kHz、单声道 PCM16LE、20 ms/320 samples 的现有 AudioFrame 基线；
- `SenseVoiceCapabilities` 明确区分 `unsupported` 与 `not_detected`：SenseVoice adapter 不虚构 interim、VAD、多人重叠、说话人身份、置信度或语言检测能力；
- `SenseVoiceSession.feed()` 校验 AudioBus 的 session/generation/stream/device epoch、采样率、声道、格式、帧尺寸、payload 长度以及 event sequence/frame index 连续性；任何不一致均 fail-closed，清空仍为 OPEN 的总线并拒绝后续帧；
- PCM16LE 转换为 `[−1.0, 1.0)` 范围的 float samples 后调用 Provider stream；
- `finish()` 只产生 provider 层的 final/empty 事件，不声明可靠 final、不提交 Agent、不创建 `request_id`；文本只做空白和 SenseVoice 特殊标签清理；
- `cancel()`、Provider feed/decode 异常、旧 session/迟到帧均关闭当前 session；不复用旧 utterance 或事件身份。

### 2.2 `tests/asr/test_sensevoice.py`

新增 8 个测试，覆盖：

- 能力矩阵中 `unsupported` 与 `not_detected` 的区分；
- 惰性加载及 SenseVoice 工厂参数；
- Provider 运行时缺失的 fail-closed；
- PCM16LE 转换、final 事件身份和文本规范化；
- duplicate/gap 连续性拒绝且不执行 decode；
- stale stream 拒绝；
- cancel 后迟到帧拒绝；
- 非 16 kHz 配置拒绝。

## 3. 验证结果

在项目根目录 `<PROJECT_ROOT>` 内，使用嵌入式 Python：

- ASR 专项：**8/8**；
- 音频专项回归（P6 批次 1–3）：**31/31**；
- 全量 unittest：**310/310**；
- `compileall -q app tests`：通过；
- `app` 与 `tests` 下 Python 文件严格 UTF-8：**113** 个文件均可解码且无 `U+FFFD`；既有 BOM 文件仍为 2 个：`tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`，本批次未改动；
- 本批次执行面审计：`app/asr` 与 `tests/asr` 未新增 subprocess、socket、requests、urllib/httpx、进程启动、真实设备访问或 `.env` 读取；仅通过注入式 module loader/fake Provider 测试；
- 未读取或修改 `.env`，未下载模型，未访问网络，未启动 Harness，未执行电脑副作用，未执行 Git add/commit/push。

## 4. 当前实现文件范围

- `app/asr/__init__.py`：SenseVoice Provider 惰性加载、PCM session 和 provider 事件模型；
- `tests/asr/test_sensevoice.py`：P6 批次 4 的注入式 Provider 测试。

## 5. 已知限制与延期

- 当前 adapter 只提供 provider 事件，不是 final 接纳层；即使事件 `is_final=True`，也不能直接进入对话主控或安全链；
- 当前未建立正式的 `EventEnvelope`/`EventGate` 接入、重复 final 拒绝、interim 丢弃、旧 generation/迟到事件诊断和 `utterance_id` 终态管理；
- 当前未实现 SenseVoice 的多人重叠、说话人身份、置信度或语言检测证明；这些能力明确标记为 `unsupported`，不得当作“未检测到”；
- 当前未验证真实模型文件、Windows 本机 sherpa-onnx wheel、模型加载资源上限或设备联合测试；这些属于后续受控 Provider/恢复和真机校准批次；
- 当前仍未接入真实麦克风、网络、云端 ASR、Agent、TTS 或电脑副作用。

## 6. 下一入口与强制停止

下一批次只能是 **P6 批次 5：final 接纳、interim 丢弃、重复/迟到/乱序/旧 generation 拒绝**。开始前至少重新读取：

1. 本交接；
2. P6 路线图卡片；
3. `implementation_plan/04-asr.md` 的实时识别、重复事件和运行期故障边界；
4. `app/asr/__init__.py` 与 `tests/asr/test_sensevoice.py`；
5. `app/events/envelope.py`、`app/events/gate.py`、`app/contracts/session.py`；
6. P6 批次 1–3 的 AudioBus、Capture、VAD 交接。

P6 批次 5 仍不得：

- 提交 Agent 请求或接入 TTS；
- 把 provider `is_final` 直接视为可靠 final；
- 开放语音 L3、连续监听、唤醒词、声纹、AEC 或实时打断；
- 把 `unsupported` 能力解释成 `not_detected`；
- 接入真实模型下载、云端服务或真实麦克风。