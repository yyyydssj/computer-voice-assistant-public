# P6 批次 3：VAD 仅作为按键输入结束判定

- 日期：2026-09-21
- 阶段：P6 ASR 按键输入
- 批次状态：**complete**
- 阶段状态：**in_progress**
- 当前停止点：**P6 批次 4 门前；尚未接入 sherpa-onnx/SenseVoice、ASR Provider、ResultGate 或 Agent 请求**

## 1. 接续与范围

严格接续 `implementation_plan/checkpoints/2026-09-21-P6-batch2-capture-device-preroll-cleanup-stop-handoff.md`。用户已明确要求直接开始下一批次、无须再次授权。本批次只实现按键输入轮次的结束判定：显式按键释放，或最小静音策略触发输入边界。

本批次没有把 VAD 当作 ASR 结果、可靠性证明、授权事实或执行许可；没有创建 `utterance_id`，没有提交任何 Agent 请求，也没有进入后续 ASR 批次。

## 2. 已完成实现

### 2.1 VAD 会话边界

新增 `app/audio/vad/__init__.py`，提供：

- `VadSessionState`：`idle`、`active`、`ended`、`cancelled`、`failed`；
- `VadFrameStatus`：开始、接纳、结束、取消、关闭、失败等结构化状态；
- `VadEndReason`：按键释放、静音超时、取消、无效帧、旧 session/generation/stream/device epoch、帧不连续、检测器不可用/失败、总线关闭；
- `VadConfig`：默认 1000ms 静音阈值、20ms 帧粒度，并使用向上取整得到静音帧数；
- `VadSession.start()`、`process(packet)`、`finish()`、`cancel()`；
- 会话绑定 `AudioBus` 身份，校验 payload 长度、边界身份及连续 event sequence/frame index；
- detector 异常或返回非 `bool` 时 fail-closed；失败/取消会清空仍为 OPEN 的 `AudioBus`；结束、失败或取消后拒绝迟到帧；
- 静音达到阈值只结束当前按键输入轮次，不声明 ASR final，不触发下游副作用。

更新 `app/audio/__init__.py`，公开上述 VAD 类型。

### 2.2 测试

新增 `tests/audio/test_vad.py`，覆盖：

- 显式按键释放结束；
- 静音阈值触发输入边界；
- 说话帧重置静音计数；
- 取消清空总线音频；
- payload 不完整 fail-closed；
- 跨 session/generation/stream/device epoch 拒绝；
- duplicate/gap/乱序拒绝；
- detector 缺失、异常、非 `bool` 返回拒绝；
- 结束后迟到帧关闭。

## 3. 验证结果

在项目根目录 `<PROJECT_ROOT>` 内，使用嵌入式 Python，并在 Python 进程中显式插入项目根目录（避免中文路径下 embed Python 的 `PYTHONPATH` 异常）：

- 音频专项（`tests/audio`）：**31/31**；
- 全量 unittest：**310/310**；
- `compileall -q app tests`：通过；
- `app` 与 `tests` 下 Python 文件严格 UTF-8：**111** 个文件均可解码且无 `U+FFFD`；既有 BOM 文件仍为 2 个：`tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`，本批次未改动；
- 本批次音频执行面审计：未新增 shell/PowerShell、subprocess、网络、进程启动、真实设备访问或 `.env` 读取入口；VAD 只处理注入的内存 `AudioPacket`。

## 4. 当前实现文件范围

- `app/audio/vad/__init__.py`：按键输入边界 VAD 会话；
- `app/audio/__init__.py`：公开 VAD 类型；
- `tests/audio/test_vad.py`：VAD 拒绝路径与结束判定测试。

## 5. 已知限制与延期

- detector 是注入式最小布尔判定，不是 sherpa-onnx/SenseVoice，也不代表识别可靠性；
- `VadSession.process()` 消费调用方传入的 `AudioPacket`，不自行从 `AudioBus` 取帧，保持采集、总线与边界判定解耦；
- 当前未建立 `utterance_id`、ASR 事件去重、final 接纳、ResultGate、文字模式降级或连续对话；
- 未调用真实麦克风、未启动真实输入 stream、未访问网络、未启动 Harness、未执行电脑副作用；
- 未读取或修改 `.env`；未执行 Git add、commit 或 push。

## 6. 下一入口与强制停止

下一批次只能是 **P6 批次 4：sherpa-onnx/SenseVoice adapter**。开始前应重新读取本交接、P6 路线图、`implementation_plan/04-asr.md`、音频采集/总线契约及共享隐私/错误恢复约束。

P6 批次 4 仍须保持边界：不得把 provider 输出直接当作可靠 final，不得绕过后续 final 接纳/去重/ResultGate，不得提交 Agent 请求或接入 TTS；不得把 VAD 结果当授权事实。