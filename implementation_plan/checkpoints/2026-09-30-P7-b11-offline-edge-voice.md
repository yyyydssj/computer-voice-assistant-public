# P7 Batch 11：离线 Edge 槽（本机 SAPI 声线）实现与离线验收（2026-09-30）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 工作目录（仓库根）：`<PROJECT_ROOT>`
- 状态：**离线实现 + 离线测试完成**（真实出声验收**未做**，由 Lead 之后单独执行）
- 范围：只做本批授权的 9 个文件；`app/main.py`、`app/tts/routing.py`、`app/tts/real_playback.py`、`app/tts/completed_reply.py`、`app/tts/private_selection.py`、既有 provider、tracker、既有 artifact、`tests/tts/test_streaming_session.py`、`tools/p7_real_*_runner.py` **均未改动**（见第 5 节证据）
- 未提交、未 push、未 `git add`

## 1. 逐文件改动（行号为本批完成后的工作副本行号）

### 1.1 新增 `app/tts/providers/edge_sapi.py`（486 行，纯标准库 + `subprocess`）

| 行 | 内容 |
|---|---|
| 1–47 | 模块契约与隐私说明（无网络客户端、文本不入命令行、参数列表调用、错误只带短代码、临时文件必清理） |
| 51–57 | `class EdgeSapiError(VoiceStudioContractError)`：`code` + 消息即短代码 |
| 57–70 | 常量：`_VOICE_NAME_RE`、`_MAX_VOICE_NAME_LENGTH=128`、`_AUDIO_FORMAT=(24000,1,2)`、`_DEFAULT_TIMEOUT_SECONDS=10.0`、`_DEFAULT_MAX_AUDIO_BYTES=8 MiB`、`_DEFAULT_CHUNK_SAMPLES=4800`、`_RUNNER_SCRIPT_PATH`、`_CREATE_NO_WINDOW` |
| 73–83 | `_validated_voice_name()`：非空、≤128、只允许 `[A-Za-z0-9 ()\-_.]`（禁引号/换行/分隔符） |
| 85–117 | `_validated_temp_root()`：`None → private_data_root()/"temp"`；拒绝 UNC/相对路径/符号链接/非目录；不存在则创建；`chmod 0700` 尽力而为 |
| 120–167 | `EdgeSapiConfig`（frozen/slots）：字段默认值与校验；`temp_root` 以 `repr=False` 隐藏；`chunk_bytes` 属性 |
| 170–171 | `SapiRunner`、`VoiceListRunner` 注入类型 |
| 174–200 | `_powershell_executable()`（优先 `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`，否则 `powershell.exe`）、`_powershell_command()`（固定 `-NoProfile -NonInteractive -ExecutionPolicy Bypass -File <固定脚本> ...`） |
| 202–220 | `_spawn_bounded()`：`subprocess.run(command, stdin=DEVNULL, capture_output=True, timeout=..., creationflags=CREATE_NO_WINDOW)`；超时→`sapi_timeout`、启动失败→`sapi_launch_failed`、非 0→`sapi_exit_<code>`，**stdout/stderr 从不外泄** |
| 223–242 | 默认渲染 runner `_run_sapi_render(config, script_path, text_path, out_path)` |
| 244–256 | 默认声线列表 runner `_run_sapi_voice_list(config, script_path) -> str` |
| 258–270 | `_parse_voice_list()`：只取 `Name|Culture|Enabled` 的 `Name`，跳过畸形行/重复项，保持脚本顺序 |
| 273–279 | `_remove_file()`：清理失败不掩盖原异常 |
| 282–448 | `EdgeSapiVoiceProvider`：`__init__(config=None, *, runner=None, clock=monotonic_seconds)`、脱敏 `__repr__`、`closed`、`close()`、`stream_sentence()`（写 UTF-8 文本 → 调 runner → 校验 WAV → 按 `chunk_samples` 切片 → 块间查取消）、私有 `_temporary_path/_write_text/_read_pcm` |
| 451–483 | `list_edge_sapi_voices(config=None, *, runner=None) -> tuple[str, ...]` |
| 486 | `__all__` |

`stream_sentence` 具体顺序（332–390）：类型校验 → `cancelled.is_set()` → `EdgeSapiError("cancelled_before_request")`；`close()` 后 → `provider_closed`；在 `temp_root` 下用 `tempfile.mkstemp` 建唯一 `.txt`/`.wav`；写 UTF-8 文本（`newline=""`，字节等于 `text.encode("utf-8")`）；调 runner（`EdgeSapiError` 原样透传，其他异常一律折叠为 `EdgeSapiError("sapi_runner_failed")`，不带原消息）；`_read_pcm` 校验（RIFF+WAVE、fmt 24000/16/1、data 偶数、≤`max_audio_bytes`）；每块前查取消（`cancelled_during_stream`）；`finally` 无条件删除两个临时文件。

### 1.2 新增 `app/tts/providers/edge_sapi_runner.ps1`（2344 字节，**非 ASCII 字节数 = 0**，无 BOM，LF）

- `param(-TextPath, -OutPath, -VoiceName, [switch]$ListVoices)`；
- `Add-Type -AssemblyName System.Speech`；
- `-ListVoices`：逐条 `Write-Output ("{0}|{1}|{2}" -f Name, Culture.Name, Enabled)`，无其他输出，`exit 0`；
- 渲染：`[System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)` → `SelectVoice($VoiceName)` → `New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList @(24000, Sixteen, Mono)` → `SetOutputToWaveFile($OutPath, $format)` → `Speak($text)` → `SetOutputToNull()`；
- 任何失败 `exit 1` 且**不打印句文本**；`$synth.Dispose()` 在 `finally` 中尽力执行。

### 1.3 修改 `app/tts/providers/__init__.py`（+13 行）

- 10–15：`from .edge_sapi import EdgeSapiConfig, EdgeSapiError, EdgeSapiVoiceProvider, list_edge_sapi_voices`；
- 25–27、42：`__all__` 增加四个新符号。

### 1.4 修改 `app/tts/streaming_session.py`

- 15–28：模块 docstring 增补 Edge 槽语义（仅未出音可切、每句一次、失败即文字、`edge_provider=None` 逐位不变）；
- 78：导入 `TTSRoutingAction`；
- 251：`__init__` 新增关键字参数 `edge_provider: object | None = None`；
- 267–270：`edge_provider` 必须提供可调用 `stream_sentence`，否则 `TypeError`；
- 283：保存 `self._edge_provider`；
- 319–323：新增 `_edge_attempted: set[str]`、`_edge_attempts=0`、`_edge_failures=0`；
- 441–473：`late_events` docstring 说明新增两种 kind；新增只读属性 `edge_attempts`（453）、`edge_failures`（465）；
- 815–833：`_speak` 的传输失败分支——先按既有顺序处理，再按 `USE_EDGE_CURRENT_SEGMENT` 决定是否走 Edge；
- 878–1000：`_handle_transport_failure` 现在**返回** `decision`（原逻辑不变，仅新增返回值）；新增 `_edge_may_render`（910）、`_speak_over_edge`（929）、`_handle_edge_failure`（976）；
- 1011：`build_streaming_session` 新增 `edge_provider=None` 并透传（1050）；
- 既有取消/背压/stop ACK/generation/计时语义均未改动。

### 1.5 修改 `app/config/settings.py`（+约 26 行）

- 39–43：`_EDGE_VOICE_RE`、`_EDGE_VOICE_MAX_LENGTH` 及"与 `EdgeSapiConfig.voice_name` 规则一致"的说明（本模块保持只依赖标准库，因此有意保留这份小型重复规则）；
- 81：`Settings.tts_edge_voice: str = ""`（默认空 = 未配置 = 路由到文字）；
- 100：`__post_init__` 调 `_edge_voice_name("TTS_EDGE_VOICE", self.tts_edge_voice)`；
- 132：`public_summary()` 增加 `"tts_edge_voice_configured": bool(...)`（仅布尔，无 URL/凭据）；
- 229：`load_settings` 读 `TTS_EDGE_VOICE`；
- 293–316：`_read_edge_voice()` / `_edge_voice_name()`；
- 未新增任何 URL、凭据、端点类字段；`TTS_VOICESTUDIO_MODE` 默认仍为 `disabled`。

### 1.6 新增 `tests/tts/test_edge_sapi.py`（30 项）

配置校验（voice_name/格式/超时/上限/chunk/临时目录/符号链接/repr 脱敏/settings 规则一致性）；注入 fake runner 的切片、`sequence`、`is_final`、格式字段、身份字段、UTF-8 文本内容与路径归属；失败短代码（缺失/非法/格式/超限/空/截断/退出码/超时/启动失败）；临时文件清理（成功、失败、生成器被放弃、流中取消）；取消语义（前置/流中）；隐私（repr、异常消息、模块 import 面、`.ps1` 纯 ASCII）；`list_edge_sapi_voices` 解析与失败码。

### 1.7 新增 `tests/tts/test_streaming_edge_fallback.py`（16 项）

未出音失败 + Edge 已配置 → **恰好一次** `edge_attempts==1`、`edge_used@generation1`、同句同身份、进入 `played` 与计时样本；已出音（`PLAYBACK_STARTED`）→ `edge_attempts==0`、不调用 Edge、不重播；Edge 也失败 → `edge_failures==1`、`edge_failed@generation1`、决定为文字、剩余语音停止、不碰第三个 Provider（http 调用为 0）；`edge_provider=None` 与未配置 Edge 的 router → 行为与既有完全一致；取消后到达的 Edge 块不进播放且记为 `pcm_chunk`；成功/失败都不改写既有事实（`failure_states`、`generation`、`dropped_pushes`、`overloaded`、`verified`、计时样本）；`build_streaming_session(edge_provider=...)` 透传；`edge_provider` 契约校验 `TypeError`。

### 1.8 文档

- `implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md`：13.2 节后新增 **13.2.1 实现注记（2026-09-30）**（离线 SAPI 声线、24k/16/mono、固定 ASCII 脚本 + UTF-8 文本文件、单次尝试、仅未出音可切、SAPI 失败即文字、默认不接线、真实验收归属）；
- 本文件。

## 2. 新公开接口（签名）

```python
# app/tts/providers/edge_sapi.py
class EdgeSapiError(VoiceStudioContractError):
    def __init__(self, code: str) -> None: ...

@dataclass(frozen=True, slots=True)
class EdgeSapiConfig:
    voice_name: str = "Microsoft Huihui Desktop"
    sample_rate_hz: int = 24_000
    channels: int = 1
    sample_width: int = 2
    timeout_seconds: float = 10.0
    max_audio_bytes: int = 8 * 1024 * 1024
    chunk_samples: int = 4800
    temp_root: pathlib.Path | None = None          # repr=False
    @property
    def chunk_bytes(self) -> int: ...

class EdgeSapiVoiceProvider:
    def __init__(self, config: EdgeSapiConfig | None = None, *,
                 runner: Callable[[EdgeSapiConfig, str, Path, Path], None] | None = None,
                 clock: Callable[[], float] = monotonic_seconds) -> None: ...
    def stream_sentence(self, request: VoiceStudioSentenceRequest, *,
                        cancelled: Event) -> Iterator[PCMChunk]: ...
    def close(self) -> None: ...
    @property
    def closed(self) -> bool: ...

def list_edge_sapi_voices(config: EdgeSapiConfig | None = None, *,
                          runner: Callable[[EdgeSapiConfig, str], str] | None = None) -> tuple[str, ...]: ...

# app/tts/providers/__init__.py
EdgeSapiConfig / EdgeSapiError / EdgeSapiVoiceProvider / list_edge_sapi_voices

# app/tts/streaming_session.py
StreamingVoiceSession(..., fallback_router=None, edge_provider=None, ...) -> ...
build_streaming_session(..., fallback_router=None, edge_provider=None, ...) -> StreamingVoiceSession
@property StreamingVoiceSession.edge_attempts -> int
@property StreamingVoiceSession.edge_failures -> int
# late_events 新增 kind：edge_used@generationN / edge_failed@generationN

# app/config/settings.py
Settings.tts_edge_voice: str = ""        # TTS_EDGE_VOICE，空 = 未配置
public_summary()["tts_edge_voice_configured"]: bool
```

`runner` 的第二个参数是**固定的随包脚本路径**（`<repo>/app/tts/providers/edge_sapi_runner.ps1`），便于测试断言"只调用固定脚本"；声线名在 `config` 内。

## 3. 语义与边界

- **切换条件**：只有 `decision.action is TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT`（router 仅在"未开始播放 + Edge 已配置"时给出）**且**注入了 `edge_provider` **且**该句未尝试过 **且** 会话未取消，才用 Edge 重做同一句。
- **单次**：`_edge_attempted` 按 `provider_request_id` 记账，尝试前即登记；不循环、不重试；第二句在路由已切 Edge 后再次 VoiceStudio 失败时不会二次尝试（router 会拒绝 `primary_failed`，会话按既有 fail-closed 路径停止本轮）。
- **不重播**：`pushed > 0`（VoiceStudio 已出音）→ `PLAYBACK_STARTED` → `CONTINUE_TEXT_NO_REPLAY` → 不切 Edge、不重播，行为与既有逐位一致。
- **同一播放上下文**：Edge 块推入**同一个** `PCMPlaybackWorker` 的**同一个** ticket（同一 request 身份），`is_final` 由 provider 决定；成功后该句照常进入 `_played` 与 `SampleTimingSample`（`first_pcm_at` 取 Edge 首块时间戳）。
- **Edge 失败**：`edge_failures += 1` → `late_events` 增 `edge_failed@generationN` → `router.edge_failed(reply_id, segment_id, playback_state=<NOT_COMMITTED 或 PLAYBACK_STARTED>)` → 得到文字决定并写入 `routing_decision` → 由 `_speak` 重抛原 `VoiceStudioTransportError`（`_run` 记录 `error/_failed`、清空 pending），即**本回复剩余语音停止、不尝试第三个 Provider**；Edge 异常的原文一律丢弃。
- **取消**：块间检查 `_cancel_event`；取消后到达的 Edge 块只记 `pcm_chunk`（沿用既有 late 语义），由取消驱动的 provider 异常记 `edge_cancelled`，都不进播放；取消优先级不变。
- **背压**：Edge 推送同样捕获 `PCMPlaybackBackpressure` → `overloaded=True`、置取消、记 `audio_backpressure`（与 VoiceStudio 路径一致）。
- **无网络 / 无新依赖**：`edge_sapi.py` 不 import `socket/urllib/requests/http`（见第 5 节），只用标准库 + `subprocess`；未新增第三方依赖（`comtypes/win32com/pyttsx3/pydub` 全未使用）。
- **默认不启用**：`TTS_EDGE_VOICE` 默认空 → Edge 未配置；`TTS_VOICESTUDIO_MODE` 仍 `disabled`；`app/main.py` 未接线；`build_streaming_session` 的 `edge_provider` 默认 `None`。
- **隐私**：异常消息/`repr`/`late_events` 中不含句文本、临时路径明文、URL、子进程输出；`EdgeSapiConfig` 的 `temp_root` 不出现在 `repr`。
- **未改动**：`app/tts/routing.py`、`app/tts/real_playback.py`、`app/tts/completed_reply.py`、`app/tts/private_selection.py`、既有四个 provider 文件、`app/main.py`、tracker、既有 artifact、`tests/tts/test_streaming_session.py`、`tools/p7_real_sentence_p95_runner.py`、`tools/p7_real_transport_fault_runner.py`。

## 4. 测试证据（真实输出，2026-09-30 19:2x，`.venv` Python）

```
===== unittest tests.tts.test_edge_sapi -v (exit=0) =====
Ran 30 tests in 0.158s
OK (skipped=1)          # 唯一 skip：本机无权限创建目录符号链接

===== unittest tests.tts.test_streaming_edge_fallback -v (exit=0) =====
Ran 16 tests in 0.012s
OK

===== unittest discover -s tests\tts -t . -q (exit=0) =====
Ran 550 tests in 3.539s
OK (skipped=3)

===== unittest discover -s tests -t . -q (exit=0) =====
Ran 1065 tests in 4.683s
OK (skipped=3)

===== compileall -q app tests tools (exit=0) =====
（无输出）
```

- `1065 = 既有 1019 + 本批 46`（30 + 16），既有用例全部通过 → `edge_provider=None` 的既有行为未被破坏。
- `git diff --check` → exit 0（无空白/冲突标记问题）。
- 全部离线：无网络、无 VoiceStudio、无真实 PowerShell（默认 runner 的退出码/超时/启动失败路径用 `mock.patch.object(edge_sapi.subprocess, "run")` 覆盖）、无 `sleep`、无音频设备、未写任何 artifact。

## 5. 静态与隐私检查（脚本化，全部 PASS，exit 0）

- `edge_sapi.py` 源码中 `socket`/`urllib`/`requests`/`http` 四个 token（大小写不敏感）**出现次数均为 0**；
- AST 结果：模块 import 仅 `__future__/math/os/re/subprocess/tempfile/threading/wave/dataclasses/pathlib/typing` + 包内相对导入；
- AST 结果：唯一的外进程调用是 `subprocess.run(command, ...)`，首参为**列表变量**，关键字仅有 `stdin/capture_output/timeout/check/creationflags`，**无 `shell`**；源码中无 `shell=True`、无 `subprocess.Popen`；
- `edge_sapi_runner.ps1`：2344 字节中 **非 ASCII 字节数 = 0**，无 BOM；含 `-TextPath/-OutPath/-VoiceName/-ListVoices`、`Add-Type -AssemblyName System.Speech`、`[System.IO.File]::ReadAllText`、`System.Text.Encoding]::UTF8`、`24000/Sixteen/Mono`；无"打印 $text"式语句；
- 禁止改动文件：`git status --porcelain -- <禁止清单>` 只列出**本批之前就已存在**的脏项（`M implementation_plan/P7-followup-execution-tracker.md`、`?? tools/p7_real_sentence_p95_runner.py`、`?? tools/p7_real_transport_fault_runner.py`），且这些文件 mtime 全部**早于**本批第一个写入文件（`edge_sapi_runner.ps1`，2026-09-30 19:16:47）；本批写入的文件只有 8 个（6 个代码/测试 + 2 个文档），逐文件 sha256 已记录在检查脚本输出中；
- 本批未使用 `git add/commit/push`。

> 并发事实（非本批产物）：同一工作区另有会话在 19:21:37 新增 `tools/p7_real_edge_fallback_runner.py`（真实声线验收 runner，本批未创建、未修改）。其代码只使用本批公开接口 `EdgeSapiConfig/EdgeSapiVoiceProvider/edge_provider=/edge_attempts/edge_failures/late_events`，与本批签名一致，可作为接口兼容的旁证。

## 6. 未做清单（诚实边界）

1. **真实出声验收未做**：本批没有启动过 `powershell.exe`、没有渲染过真实 WAV、没有打开过音频设备、没有采集过真实 p95/首音延迟；`Microsoft Huihui Desktop` 是否真的可用、SAPI 输出是否真的 24000/16/mono 仍由 Lead 之后的真实运行确认（本批只用注入 runner 的离线构造 WAV 验证了校验与切片逻辑）。
2. **在线 edge-tts 属另一批**：本批的"Edge 槽"是本机离线 SAPI 声线，不是 Edge 云端/`edge-tts`。
3. **默认 runtime 未接线**：`app/main.py` 未改，`TTS_EDGE_VOICE` 默认空，`TTS_VOICESTUDIO_MODE` 仍 `disabled`。
4. **未做真实故障注入**：未杀服务、未注入半开连接；Edge 失败路径只用离线 fake provider 触发。
5. **未覆盖**：多次回复级别的 Edge 恢复策略（router 的 `primary_recovered`）、并发多句 Edge 压力、`-ListVoices` 的真实输出解析（仅离线样本行）。
6. **未提交、未 push**。
7. 一个已知的实现取舍：由于渲染是"先完整产出 WAV、再逐块 yield"，"流中取消即终止子进程"落地为"子进程已由 `timeout` 有界回收 + 丢弃已渲染音频 + 清理两个临时文件"；不存在遗留子进程，但也没有"渲染中途 kill"的即时中断。真实长文本渲染的取消时延需 Lead 验收时观察。

## 7. 下一入口

1. Lead 执行真实出声验收（`Microsoft Huihui Desktop`，24k/16/mono，含一次真实 Edge 接管与一次真实 SAPI 失败转文字）；
2. 真实验收通过后再讨论默认 runtime 接线与 `TTS_EDGE_VOICE` 的实际取值；
3. 在线 edge-tts 若需要，另立批次（本批不涉及网络实现）。
