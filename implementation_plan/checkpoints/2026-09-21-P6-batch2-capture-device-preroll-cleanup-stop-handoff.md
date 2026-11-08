# P6 批次 2：sounddevice 采集、设备选择、pre-roll 与临时资源清理

- 日期：2026-09-21
- 阶段：P6 ASR 按键输入
- 批次状态：**complete**
- 阶段状态：**in_progress**
- 当前停止点：**P6 批次 3 门前；尚未接入 VAD、ASR Provider、ResultGate 或 Agent 请求**

## 1. 接续、授权与本批次边界

严格接续 `implementation_plan/checkpoints/2026-09-21-P6-batch1-audio-bus-stop-handoff.md`。用户于 2026-09-21 明确要求直接开始下一批次、无须再次授权，因此本批次直接执行。

本批次唯一目标是建立真实 `sounddevice` 输入适配的窄边界，同时在无真实设备的单元测试中证明：固定设备不静默切换、system default 只使用用户显式选择的模式、设备检查失败不打开 stream、callback 状态异常和不完整帧 fail-closed、pre-roll 有界且只保存内存帧、临时音频只清理本程序带归属标记的文件。

明确未做：

- 未调用真实麦克风、未启动真实输入 stream、未执行设备枚举以外的硬件副作用；测试全部使用注入的 fake backend/stream；
- 未接 VAD、固定按键释放结束判定、静音策略、sherpa-onnx、SenseVoice、ASR adapter、ResultGate 或 Agent 请求；
- 未接 TTS、连续监听、唤醒词、声纹、AEC、实时打断、语音 L3 或 P6 批次 4–6；
- 未修改 P2 `AudioFrame` 字段语义、P5 授权/执行/审计安全链、配置默认值或 `.env`；
- 普通实时音频仍只进入内存 `AudioBus` 与有界 `PreRollBuffer`；临时文件仅提供受控 Provider 文件路径原语，不被当前采集链自动使用；
- 未使用 `.env` 凭证，未发送网络请求，未启动 Harness runtime，未执行电脑副作用；未执行 Git add、commit 或 push。

## 2. 已完成实现

### 2.1 输入设备选择与检查

新增 `app/audio/capture/__init__.py`，提供：

- `DeviceSelection.fixed(device_id)`：固定设备不存在、无输入声道、权限拒绝、占用、断开和采样率/声道不兼容均返回明确状态；不自动 fallback；
- `DeviceSelection.system_default()`：只有用户显式选择此模式才读取当前系统默认输入设备；
- `DeviceInfo`、`DeviceCheckResult`、`DeviceCheckStatus`，状态覆盖 `checking/ready/unavailable/busy/disconnected/permission_denied/incompatible/switching`；
- `SoundDeviceCaptureBackend`：仅在方法调用时延迟导入 `sounddevice`，使用 `int16` PCM、固定采样率/声道检查和 `InputStream` 窄适配，不暴露额外设备控制面。

### 2.2 CaptureSession 与拒绝路径

- `CaptureSession.start()` 先检查设备，失败时不打开 stream；stream 创建或启动异常、以及启动期间 callback 先行失败时关闭总线并进入 `FAILED`；
- callback 只接受完整的固定帧：帧数必须等于配置采样数，payload 字节数必须等于 `AudioFrame` 契约；
- callback status 非空、不完整 payload、契约错误、总线拒绝、旧/错误连续性均停止并清空半截音频；不把不完整音频提交给后续模块；
- 采集 callback 在 `AudioBus` 身份字段上使用当前 session/generation/stream/device epoch；时间戳按既有 UTC `Z` 契约生成；
- 正常 `stop(reason, discard=False)` 关闭 stream 并保留完整总线队列；故障路径显式 discard；
- `app/audio/__init__.py` 已公开总线、采集、设备、pre-roll 和临时文件类型。

### 2.3 有界 pre-roll 与临时资源

- `PreRollBuffer` 使用固定容量 deque，入队时复制 payload 为不可变 `bytes`，拒绝其他 stream；
- `TemporaryAudioStore` 使用不可预测随机文件名和相邻 `.owner` 归属标记；只清理能验证内部 magic/name 的本程序文件；
- 正常 cleanup 立即删除音频与标记；不存在、非归属和删除失败分别返回结构化状态；
- `cleanup_expired()` 只扫描本程序专用目录内的归属标记，最长年龄默认 60 秒；不按扩展名、时间或相似名称删除外部文件；
- 归属标记写入失败时回收刚创建的音频文件，避免遗留未标记敏感文件；
- 该临时文件原语不写普通日志、历史、Agent 请求或审计正文。

## 3. 测试与验证

新增 `tests/audio/test_capture.py`，先写拒绝路径测试，再实现。覆盖：

- 固定设备缺失不 fallback；输入声道不足拒绝；system default 显式模式；权限/占用错误分类；
- 不可用设备不打开 stream；stream 创建/启动失败以及启动竞态均 fail-closed；callback status 故障清空音频；不完整 payload 不入总线；
- 完整 callback 进入 `AudioBus`，pre-roll 有界、复制 payload、拒绝其他 stream；正常停止保留完整队列；
- 本程序临时文件立即清理；过期归属文件清理；非归属文件不删除；
- P6 批次 1 `tests/audio/test_bus.py` 回归。

最终结果：

- P6 批次 2 专项：**14/14**；
- P6 批次 1 音频总线回归：**8/8**；合计音频测试：**21/21**；
- 全量 unittest：**300/300**；
- `compileall -q app tests`：通过；
- 测试未打开真实麦克风；所有设备/stream 行为通过 fake backend 注入验证；
- 未读写 `.env`，未发送网络请求，未启动 Harness，未执行电脑副作用。

## 4. 当前实现文件范围

- `app/audio/bus.py`：仅增加只读身份访问器，供采集 callback 绑定当前总线身份；不改变既有 publish/close 连续性语义；
- `app/audio/__init__.py`：公开 P6 批次 1、2 类型；
- `app/audio/capture/__init__.py`：设备选择、sounddevice 窄适配、采集生命周期、pre-roll、临时清理；
- `tests/audio/test_capture.py`：本批次拒绝路径和成功路径测试。

## 5. 已知限制与延期

- 尚未在真实设备上校准 callback 粒度、设备标识稳定性、采样率兼容矩阵或 Windows 权限提示；不得把当前 16 kHz/单声道/20 ms 视为最终硬件参数；
- `system_default` 的设备变化通知和“下一轮才切换”的上层编排尚未建立；本批次不自动切换设备；
- `TemporaryAudioStore` 只提供受控临时文件生命周期原语，尚未接入任何 ASR Provider 文件适配器；
- 删除失败的界面/人物模板提示、有限重试看门狗和程序重启遗留清理的完整交互仍延期到后续音频/ASR 收口；当前实现返回 `cleanup_pending`，不继续读取或复用文件；
- 尚未建立 `utterance_id`、VAD、ASR 事件去重、final 接纳或文字模式降级。

## 6. 下一入口与强制停止

下一批次只能是 **P6 批次 3：VAD 仅作为本阶段的结束判定**。开始前至少读取：

1. 本交接；
2. 路线图 P6 卡片；
3. `implementation_plan/04-asr.md` 中结果边界；
4. 当前 `app/audio/capture/__init__.py` 与 `tests/audio/test_capture.py`；
5. `app/audio/bus.py` 与 `tests/audio/test_bus.py`。

P6 批次 3 仍不得：

- 接入 sherpa-onnx/SenseVoice、ResultGate、Agent 请求或 TTS；
- 实现连续监听、唤醒词、声纹、AEC、实时打断或语音 L3；
- 把 VAD/静音判定当作可靠 ASR final；
- 把不完整、被取消、设备异常或低可靠语音提交给后续模块。