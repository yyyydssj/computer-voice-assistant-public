# P7 VoiceStudio 双路径 Batch 1：统一契约与 fake adapter 基线

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 批次：Batch 1
- 状态：完成，停止在 Batch 2 授权边界

## 1. 本批目标与边界

本批只建立不依赖真实 VoiceStudio、网络或音频设备的本地边界：

- 固定完整句请求的 reply / segment / provider request / generation / VoiceProfile 快照字段；
- 固定 WS PCM 与 HTTP WAV 归一后的 `PCMChunk` 数据形状；
- 固定 24 kHz、mono、PCM16、偶数字节和非空音频约束；
- 固定 sequence 从 1 开始连续递增、单一 final 块和取消/generation fence；
- 建立 fake WebSocket provider、fake HTTP provider 与 fake PCM sink；
- 不接入默认运行时，不修改现有 TTS 路由、播放 worker、VoiceProfile 或 VoiceStudio 本体。

## 2. 修改文件

- `app/tts/providers/contracts.py`
  - 新增不可变 `VoiceStudioSentenceRequest`；
  - 新增不可变 `PCMChunk`；
  - 新增 `VoiceStudioAudioProvider` 同步可取消 Protocol；
  - 新增 `PCMChunkStreamValidator`，统一执行身份、generation、sequence、final 和取消校验。
- `app/tts/providers/fakes.py`
  - 新增离线 `FakeWebSocketProvider`、`FakeHttpProvider` 和 `FakePCMSink`；
  - fake provider 只消费预置脚本，不创建 socket、HTTP 请求、进程或音频设备。
- `app/tts/providers/__init__.py`
  - 导出 Batch 1 契约和 fake 实现。
- `tests/tts/test_voicestudio_contracts.py`
  - 新增 10 个测试，覆盖合法请求、不可变性、格式错误、重复/跳号、旧 generation、缺 final、取消后块、fake WS/HTTP 共用 PCM 契约和 sink 边界。

## 3. 验证证据

```text
python -m unittest tests.tts.test_voicestudio_contracts -q
10 tests, OK

python -m unittest discover -s tests/tts -p 'test_*.py' -q
331 passed, 2 skipped

python -m compileall -q app tests
PASS
```

未执行真实网络、VoiceStudio、GPU、扬声器或 waveOut 操作。

## 4. 设计决策

- Batch 1 不复制第三方实现；只沿用项目现有 `app.contracts.base` 的标识符、非空文本和正整数校验风格。
- WS 与 HTTP fake 必须输出同一种 `PCMChunk`，播放层未来不依赖传输来源。
- 取消和旧 generation 是 fail-closed：迟到块抛出契约错误，不进入 sink 的 `received` 列表。
- `is_final=True` 必须标在最后一个实际音频块上；缺 final 或 final 后继续输出都拒绝。
- 本批没有新增 `websockets` 依赖，Batch 3 再单独处理真实 WS 客户端依赖和协议状态机。

## 5. 退出与下一停止点

Batch 1 退出条件已满足：契约测试全绿，TTS 回归全绿，未发生真实网络或设备调用。

下一步只能在用户单独授权后进入 **Batch 2：逐句 HTTP `/generate` 快速路径**。本 checkpoint 不代表已实现真实 WebSocket、PCM 播放或自动 fallback。

## 6. 收尾验证与跟踪

- Batch 1 专项：`python -m unittest tests.tts.test_voicestudio_contracts -q` → **10/10 passed**。
- TTS 回归：`python -m unittest discover -s tests/tts -p 'test_*.py' -q` → **331 tests, OK (2 skipped)**。
- 全量回归：`python -m unittest discover -s tests -t . -p 'test_*.py' -q` → **830 tests, OK (2 skipped)**。
- 编译检查：`python -m compileall -q app tests tools` → **通过**。
- 修复了 `app/tts/providers/contracts.py` EOF 多余空行；重新暂存后 `git diff --cached --check` 检查通过（无输出）。
- 本批相关文件已单独跟踪；没有执行 `git add .`，没有纳入虚拟环境、wheelhouse、运行时二进制或无关未跟踪历史文件。
- 等待用户审阅完整 staged diff 后再决定是否提交；本批不 push，不进入 Batch 2。
