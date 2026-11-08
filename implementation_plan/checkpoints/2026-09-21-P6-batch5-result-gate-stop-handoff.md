# P6 批次 5：ResultGate final 接纳与旧事件拒绝停止交接

- 日期：2026-09-21
- 阶段：P6 ASR 按键输入
- 批次状态：**complete**
- 阶段状态：**in_progress**
- 当前停止点：**P6 批次 6 门前；尚未实现 ASR startup/session/runtime 故障和文字模式降级**

## 1. 接续与范围

严格接续 `implementation_plan/checkpoints/2026-09-21-P6-batch4-sensevoice-adapter-stop-handoff.md`。本批次只实现 Provider adapter 与对话主控之间的 `ResultGate`：接纳当前有效 utterance 的一次非空 final，丢弃 interim，并拒绝重复、迟到、乱序、跨 session、旧 audio session、旧 generation、旧 stream、旧 utterance 和错误 Provider 实例事件。

本批次没有提交 Agent 请求、创建 `request_id`、接入 TTS、文字模式降级、startup/session/runtime 故障恢复、真实麦克风、真实模型下载、云端 ASR、连续监听、唤醒词、声纹、AEC、实时打断或语音 L3。

## 2. 已完成实现

### 2.1 `app/asr/result_gate.py`

新增：

- `AsrResultCandidate`：把 Provider 事件补齐为统一 `EventEnvelope` 所需的 session、audio session、generation、stream、utterance、event sequence、Provider timestamp 和 received timestamp；支持从现有 `AsrEvent` 显式转换；
- `ResultGateDecision`：区分 `accepted_final`、`drop_interim`、`reject_empty`、重复、乱序、跨边界、旧实例和终态后迟到等结果；
- `ResultGateState`：`open`、`accepted`、`rejected`；
- `ResultGateResult`：只返回接纳结果和可靠性事实，不创建请求、不触发下游副作用；
- `ResultGate`：复用 P2 `EventGate` 做边界、event identity、来源序号和终态校验；只对 `is_final=True`、类型为 `asr.final`/`asr.empty`、规范化后非空且尚未终态的结果返回 `accepted_final`；
- interim 事件会进入顺序闸门但只返回 `drop_interim`，不返回文本、不进入正式对话；空 final 返回 `reject_empty` 并关闭当前 utterance；
- 相同文本但不同 utterance 仍可独立接纳；相同 event identity 或同来源同序号不能第二次接纳；Provider `is_final` 不越过边界、顺序、文本和终态校验。

### 2.2 `app/asr/__init__.py`

公开导出 ResultGate 相关类型；未改变 SenseVoice adapter 的 Provider 层边界。

### 2.3 `tests/asr/test_result_gate.py`

新增 9 个测试，覆盖：

- 非空 final 一次接纳；
- interim 消费顺序但静默丢弃；
- 空 final 与空白文本拒绝；
- 重复 final 与不同 utterance 的相同文本区分；
- 乱序、同序号重放和终态后迟到；
- session/audio session/generation/stream/utterance 旧边界拒绝；
- Provider `AsrEvent` 转换后仍须经过 ResultGate；
- 非法/不完整事件 fail-closed。

## 3. 验证结果

在项目根目录 `<PROJECT_ROOT>` 内，使用嵌入式 Python，并在 Python 进程中显式插入项目根目录：

- ResultGate 专项：**9/9**；
- P6 ASR + 音频回归（`tests.asr.test_sensevoice`、`tests.asr.test_result_gate`、`tests.audio`）：**17/17**；
- 全量 unittest：**310/310**；
- `compileall -q app tests`：通过；
- `app` 与 `tests` 下 Python 文件严格 UTF-8：**115** 个文件均可解码且无 `U+FFFD`；既有 BOM 文件仍为 2 个：`tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`，本批次未改动；
- `app/asr` 与 `tests/asr` 执行面审计：**无** `subprocess`、`socket`、`requests`、`urllib/httpx`、`sounddevice`、`pyaudio`、进程启动或 `.env` 读取入口；
- 未读取或修改 `.env`，未下载模型，未访问网络，未启动 Harness，未执行真实麦克风或电脑副作用，未执行 Git add/commit/push。

## 4. 当前实现文件范围

- `app/asr/result_gate.py`：ResultGate 与候选事件模型；
- `app/asr/__init__.py`：ResultGate 类型公开导出及模块说明调整；
- `tests/asr/test_result_gate.py`：P6 批次 5 测试。

## 5. 已知限制与延期

- `accepted_final` 仍只是 ASR 数据面接纳事实，不是 Agent 已提交、文字历史已写入或任务已创建事实；本批次未生成 `request_id`；
- ResultGate 未实现低置信度、多人重叠、语言检测或说话人身份判定；SenseVoice 当前这些能力仍是 `unsupported`，不得解释为 `not_detected`；
- 当前未实现 ASR startup/session/runtime 故障、模型超时、Provider 重建、临时音频清理、文字模式降级或恢复后安全切回；
- `EventGate` 的终态语义仍由 `audio.closed`、`audio.failed`、`asr.rejected` 驱动；`asr.final` 的接纳不会替代后续对话主控和请求提交边界；
- 未验证真实模型文件、Windows 本机 sherpa-onnx wheel、真实设备联合测试或阈值校准。

## 6. 下一入口与强制停止

下一批次只能是 **P6 批次 6：ASR startup/session/runtime 故障和文字模式降级**。开始前至少重新读取：

1. 本交接；
2. P6 路线图卡片；
3. `implementation_plan/04-asr.md` 的 startup/session/runtime 故障、文字降级、预热和恢复边界；
4. `app/asr/__init__.py`、`app/asr/result_gate.py` 及其测试；
5. `app/events/envelope.py`、`app/events/gate.py`、`app/contracts/session.py`；
6. P6 批次 1–3 AudioBus/Capture/VAD 交接与 P6 批次 4 SenseVoice 交接。

P6 批次 6 仍不得：

- 提交 Agent 请求或接入 TTS；
- 开放语音 L3、连续监听、唤醒词、声纹、AEC 或实时打断；
- 把 `accepted_final` 直接当作 Agent 已提交；
- 把 `unsupported` 能力解释成 `not_detected`；
- 接入真实模型下载、云端服务或真实麦克风，除非后续批次明确允许并有本地受控测试边界。