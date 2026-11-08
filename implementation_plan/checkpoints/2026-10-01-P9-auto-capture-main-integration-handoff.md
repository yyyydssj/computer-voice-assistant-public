# P9 免按键自动收音接入主程序交接（`--voice --auto`）

> 状态：**已实施 + 离线全量回归 + 真机试听通过**（2026-10-01）
> 批次：A1 抽取共享会话层 → A2 主程序接线 → A3 文档与提交
> 相关模块：`app/runtime/auto_voice.py`（新增）、`app/main.py`、`tools/p9_auto_voice_runner.py`、
> `tests/foundation/test_main_auto_voice.py`（新增）

## 1. 一句话结论

免按键自动收音不再只存在于真机 runner：链路已抽成可复用的 `app/runtime/auto_voice.py`，
主程序用 `--voice --auto` 直接复用同一条实现（Silero VAD + 段级端点 + 连续对话轮次），
runner 改为消费同一份代码，杜绝两处装配漂移。**自动收音只决定输入边界，不改变授权/计划/执行语义。**

## 2. 交付物

| 文件 | 说明 |
|---|---|
| `app/runtime/auto_voice.py`（新增 ~1070 行） | 中性共享层：`AutoVoiceConfig`、`AutoVoiceSession`（装配 + `run_turn` 轮次循环）、`AutoTurnFacts`（含 `log_details()` 脱敏字段）、`calibrate_speech_threshold`、`wait_for_wake_word`、`load_voiceprint_profile`、`_hud_set`/`_hud_tick`、fail-closed 错误族（`AutoVoiceError` / `ThresholdCalibrationError` / `VadUnavailableError` / `WakeWordUnavailableError` / `VoiceprintUnavailableError`） |
| `app/main.py`（改） | `_run_auto_voice()`、`_keyboard_interrupt_source()`、`_voice_entry_config_for_auto()`、`_write_line()`；`run()` 新增 `voice_auto`/`auto_config`/`auto_session_factory`/`auto_environment_factory`/`auto_interrupt_source`；CLI 新增 5 个参数 |
| `tools/p9_auto_voice_runner.py`（改） | 2124 → 1256 行：删除重复实现、改为导入共享层；保留 runner 专属的 `AutoRunnerConfig` 子类、`ScriptedAgent`、HUD 时间线、artifact、CLI、`_safe_print` |
| `tests/foundation/test_main_auto_voice.py`（新增 21 项） | CLI 折叠 / `--auto` 关闭零改动 / 真实环境装配 / 注入会话 / `/text` `/exit` / 失败降级 / Ctrl+C / HUD 状态序列 / 配置校验 |
| `tests/integration/test_p9_auto_runner.py`（改 1 处） | 容量常量的源码断言改指向被 import 的实现 |

## 3. 使用方式与开关

```powershell
cd '<PROJECT_ROOT>'

# 免按键 + Silero VAD（默认 vad-mode；模型默认 models/openwakeword/silero_vad.onnx，无需传路径）
.\.venv\Scripts\python.exe -m app.main --voice --auto

# 免按键 + 能量判定（不需要 VAD 模型）
.\.venv\Scripts\python.exe -m app.main --voice --auto --vad-mode energy

# 按键模式（不带 --auto，行为与改前逐字节一致）
.\.venv\Scripts\python.exe -m app.main --voice
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--auto` | 关 | 与 `--voice` 同用才启用免按键；单独 `--auto` 仍是文字模式 |
| `--vad-mode` | `silero` | `silero`（默认，模型缺失即 fail closed）或 `energy` |
| `--vad-model` | 项目默认模型 | Silero onnx 路径 |
| `--max-record-seconds` | 15 | 单轮最长录音 |
| `--min-utterance-ms` | 300 | 最小轮长（低于此长度不判说完） |
| `--silence-ms` / `--speech-threshold` | 1500 / 600 | 结束静音阈值 / 能量阈值（`energy` 模式） |
| `--device-id` / `--no-hud` | — | 固定输入设备 / 关闭右下角提示窗 |

运行中：直接说话即自动收音；输入 `/text` 切回文字输入，`/exit` 退出，Ctrl+C 干净停止。
**唤醒词门禁与声纹过滤默认关闭**（`kws_mode=off` / `voiceprint_mode=off`），共享层已支持，
但本轮没有加对应 CLI 开关——要开需另开批次，避免一次摊太大。

## 4. 关键约束（别改坏）

1. **不带 `--auto` 时零行为变化**：`run()` 里 `voice_auto` 为假就完全跳过自动路径；
2. **装配失败必须如实降级为文字**：设备枚举失败、模型缺失、依赖缺失、ASR 启动失败、
   依赖注入非法（如 provider 类型错）都走 `_voice_unavailable()` + 回到文字壳，
   **绝不静默假装自动收音可用**；
3. **轮次异常也要收尾再降级**：`run_turn` 抛错时先 `session.close()` + HUD `STOPPED` + 发事件，
   然后降级为文字（不把半开的采集留在后台）；
4. **不阻塞收音**：中断检查走 `msvcrt.kbhit()`（Windows），没有按键就立刻返回；
   非 Windows 返回空实现，绝不因为等键盘输入卡住麦克风循环；
5. **日志脱敏**：`AutoTurnFacts.log_details()` 只出计数/状态/原因码与 `asr_text_length`，
   识别正文不进日志；
6. **runner 与主程序共用同一实现**：不要再把装配逻辑复制回 runner。

## 5. 抽取（A1）时踩到并修掉的问题

| 问题 | 原因 | 处置 |
|---|---|---|
| 首轮 `run_turn` 直接 `NameError: _hud_tick` | HUD 小工具留在 runner，会话类已搬到共享层 | `_hud_set`/`_hud_tick` 一并搬入共享层并导出 |
| `AutoTurnFacts() takes no arguments` | 抽取区间起点漏掉了 `@dataclass` 装饰器 | 补回装饰器 |
| 门禁提示不再打印 | 打印从硬编码改为注入式 | runner 保留 `_safe_print`，用一行子类把 `notify` 默认接上（lambda 调用时解析，测试可替换） |
| `vad_model_path` 仍是字符串 | 搬运时把「字符串转 Path」写成了纯校验 | 恢复转换分支 |
| `threshold_multiplier` / `vad_threshold` 非法值不再报错 | 校验代码随被删块一起丢失 | 校验全部搬入 `AutoVoiceConfig.__post_init__` |

## 6. 测试与验收

- 全量回归：**2055 tests OK (skipped=3)**（改前 2034；新增 21 项）；`compileall -q app tests tools` exit 0；
- runner 专项：**88 tests OK**（行为不变）；
- 新入口专项：`tests.foundation.test_main_auto_voice` **21 tests OK**（不打开任何真实设备）；
- 主程序既有测试（`test_main` / `test_main_voice_opt_in` / `test_main_push_to_talk`）保持全绿；
- **真机试听（用户本人执行，2026-10-01）：通过。** 用户运行主程序免按键路径后确认「可以」
  （免按键一轮闭环可用）。**如实边界**：本轮只记录"用户确认可用"这一事实，不代填识别正文、
  延迟数值或运行模式（未要求用户提供控制台输出）；上表中 `--vad-mode` 的实际取值以用户
  运行的命令为准。

## 7. 唤醒词门禁与声纹过滤的 CLI 开关（同日追加）

主程序现已支持把 runner 真机验收过的两条能力直接打开（默认仍关闭）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--kws-mode` | `off` | `gate` = 必须先听到唤醒词才开启新的输入会话（唤醒音频不进 ASR） |
| `--kws-model-dir` | 无 | KWS 模型目录（gate 必需；含 `tokens.txt` + `encoder/decoder/joiner.onnx`） |
| `--kws-keywords-file` | 自动 | 默认取 `<kws-model-dir>/keywords.txt` |
| `--keyword` | `小饼` | 唤醒词文本 |
| `--kws-threshold` | `0.25` | 命中阈值（0~1） |
| `--kws-wait-seconds` | `30` | 每轮等待唤醒词的最长秒数 |
| `--voiceprint-mode` | `off` | `filter` = 他人/电视说话在 **ASR 之前**丢弃本轮 |
| `--voiceprint-model` | 无 | 声纹嵌入 onnx（filter 必需） |
| `--voiceprint-profile` | 无 | 已注册档案 JSON（filter 必需；只含嵌入向量） |
| `--voiceprint-threshold` | `0.5` | 匹配阈值 (0,1]，调低会放行更多说话人 |

命令示例：

```powershell
# 唤醒词门禁（真机验收过的模型目录）
.\.venv\Scripts\python.exe -m app.main --voice --auto --kws-mode gate --kws-model-dir models\kws --keyword 小饼

# 声纹误触发过滤（需先用 tools\p9_voiceprint_enroll.py 注册档案）
.\.venv\Scripts\python.exe -m app.main --voice --auto --voiceprint-mode filter `
  --voiceprint-model models\voiceprint\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx `
  --voiceprint-profile data\voiceprint-owner.json --voiceprint-threshold 0.5
```

**fail-closed 口径**：显式要求 `gate`/`filter` 但模型、档案缺失或非法时，装配直接失败 →
主程序**如实打印原因并降级为文字输入**，绝不静默退化成「看起来启用了其实没有」。
两条能力都只影响输入边界：唤醒词只开会话、声纹只做误触发过滤，**都不决定授权**。

新增测试 5 项（参数折叠 / 默认关闭 / 非法值拒绝 / gate 模型缺失必须报错而非静默 / 声纹档案缺失报错），
全量 **2060 tests OK (skipped=3)**。

## 8. 本机 TTS（VoiceStudio）按需自动拉起（同日追加）

**动机（真机现象）**：`--voice` 的默认播报通道是 `ws_pcm`，它必须先通过
`GET 127.0.0.1:3900/profiles` 的**身份校验**（档案 id/名字/参考音频哈希）。VoiceStudio 没开时
用户看到的就是 `语音输入不可用：provider profile verification failed` → 降级为文字。

**做法**：新增 `app/runtime/tts_service.py`（纯 stdlib），在语音入口启动前：

1. 只读探测 `GET /profiles`（5s 超时）；已在跑就**什么都不做**；
2. 不可达且允许自动启动时，用**可配置命令**把它作为独立后台进程拉起
   （`Popen` + `argv` 列表 + 无 shell + 不接管它的输出）；
3. 轮询到就绪或超时（默认 90s），如实报告「已在运行 / 已自动启动（pid、耗时）/ 起不来」。

| 开关 | 默认 | 说明 |
|---|---|---|
| `--tts-autostart` | `on` | `on` 自动拉起；`off` 从不拉起；`require` 必须可用否则报错退出（码 3） |
| `--tts-start-timeout` | `90` | 等待就绪的秒数 |
| `--tts-start-command` | 无 | 显式启动命令（空格分隔）；默认读环境变量 |

| 环境变量（本机配置，不入库） | 说明 |
|---|---|
| `ASSISTANT_TTS_PYTHON` | 启动用解释器（如 VoiceStudio 项目自己的 `.venv\Scripts\python.exe`） |
| `ASSISTANT_TTS_BACKEND` | 后端入口脚本（如 `...\project\backend\main.py`） |
| `ASSISTANT_TTS_CWD` | 子进程工作目录（默认取后端脚本所在目录） |
| `ASSISTANT_TTS_AUTOSTART` | `0/false/off` 可整体关闭自动启动（默认开） |

**边界（刻意为之）**：

- 只在**语音模式**且播报通道需要本机服务时触发（`--tts-mode edge` / `none` 从不触发）；
- 不写死任何个人路径：解释器/脚本/工作目录全部来自环境变量，缺配置就**跳过并说明**，不猜路径；
- 只关自己拉起的那个进程（`OwnedProcess`）；**用户自己开着的服务绝不被本模块关掉**；
- 起不来时**不抛异常**，只把事实写进返回值，由语音入口如实降级为文字。

**真机实测（2026-10-01）**：配置好三个环境变量后运行
`python -m app.main --voice --auto --tts-start-timeout 120`，助手打印
`已自动启动本机 TTS 服务（pid=468，用时 53.3s）`，`/profiles` 45s 后返回 200，
VoiceStudio 进程（468 / 6260）确实被拉起；测试后已手动清理，环境恢复。

**已写入本机配置**（用户级环境变量，不入库）：`ASSISTANT_TTS_PYTHON` /
`ASSISTANT_TTS_BACKEND` / `ASSISTANT_TTS_CWD` 三个值均指向本机 VoiceStudio 项目且校验存在。

新增测试 24 项（`tests/foundation/test_tts_service.py` 18 项 + 主程序策略 6 项），
全部注入 fake，不启进程、不发网络请求。

## 9. 未做 / 后续

- 自适应阈值校准（`threshold_mode=auto`）未接 CLI（主程序暂用固定阈值 600 / `--speech-threshold`）；
- AEC/全双工（P10）；多轮上下文 / SSE 流式 / 费用限流仍待授权。
