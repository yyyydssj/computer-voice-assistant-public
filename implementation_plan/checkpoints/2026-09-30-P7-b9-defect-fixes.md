# P7 Batch 9 缺陷修复：高分辨率时钟、每回复一次验证、过载可诊断、合成首音跨度

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 状态：**完成（仅离线实现与离线验证）；未联网、未访问 VoiceStudio、未使用扬声器/麦克风、未安装依赖、未改默认 runtime 接线、未 git add/commit/push、未采集任何真实数字**
- 范围：修复真实采集（`2026-09-30-P7-b9-real-p95-measurement.md`）确认的四个缺陷 A/B/C/D，并补计划 19.2.1/19.2.2 口径
- 真实复测：由 Lead 之后执行；本 checkpoint 不声称 19.2 任何一条指标已达标

## 1. 四个缺陷与修法（逐个对应）

| 缺陷 | 症状（真实采集） | 修法 | 落点 |
|---|---|---|---|
| **A 时钟分辨率** | Windows `time.monotonic` = `GetTickCount64()`，分辨率 15.625 ms（实测 20 万次连续调用零正差值），worker 侧跨度显示 `0.000 ms` | 新增 `app/tts/clock.py`：`monotonic_seconds`（`sys.platform == "win32"` → `time.perf_counter`，否则 `time.monotonic`）＋只读 `clock_resolution_seconds()`；五个构造点的 `clock` **默认值**改成它 | `app/tts/clock.py`；`streaming_session.py:242,853`；`pcm_playback.py:95`；`transport.py:122`；`waveout_pcm.py:116` |
| **B 每句重复验证** | 每句 2 次 HTTP 往返 + 参考音频 SHA-256，实测 18.4 / 29.7 / 56.4 ms（min/p50/max），是 `ready_to_submit` p95 < 20 ms 未通过的直接原因 | `StreamingVoiceSession` 新增 `_verified`/`verified`；`_speak()` 的每句 `self._verifier(self.selection)` 改为 `_verify_once()`——**每次会话（= 每次回复）只执行一次**；失败仍抛错并走既有 fail-closed 路径 | `streaming_session.py:298,366,681,709` |
| **C 过载截断不可诊断** | `push_text_chunk()` 在 `_overloaded` 为真时静默丢弃；`finish()` 在 `segment_timeout_s` 到期后仍返回，`close()` 随后取消剩余语音而没有任何“未说完”标记 | 拒绝时不再静默：`_record_late("push_rejected_<reason>")` + 只读计数 `dropped_pushes`；新增只读 `pending_sentences`；`finish()` join 后线程仍存活则记 `finish_timeout` 并置 `finish_timed_out = True` | `streaming_session.py:379,392,399,429,513,559,566` |
| **D 缺合成延迟跨度** | 无 `tts_submitted_at → first_pcm_at` 跨度，19.2 第 3/4 条（`auto` vs `http_wav` 中位起播延迟）无法评估 | `SentenceTimingSample` **追加**第 8 字段 `submit_to_first_pcm_ms`；`StreamingTimingPercentiles` **追加**第四段 `submit_to_first_pcm` | `streaming_session.py:185,215,501,804` |

## 2. 逐文件改动（含行号）

### 2.1 新增 `app/tts/clock.py`（66 行）

- `monotonic_seconds: Callable[[], float]`（第 35-37 行）：`time.perf_counter if sys.platform == "win32" else time.monotonic`。模块 docstring（第 1-31 行）写明原因与 **15.625 ms** 事实（`time.get_clock_info("monotonic").resolution == 0.015625`）以及 20 万次连续调用零正差值的实测。
- `clock_resolution_seconds(clock=None) -> float`（第 43-59 行）：默认时钟（或显式传入的时钟）的分辨率；用 `time.get_clock_info` 查询，未知可调用对象抛 `ValueError`、非可调用对象抛 `TypeError`；只读、不调用时钟、不存状态。
- 只 import 标准库 `sys`/`time`/`typing`；无 socket/网络/子进程/设备/注册表/ctypes 调用。

### 2.2 `app/tts/streaming_session.py`（896 行）

- 第 26 行起：docstring 增加“默认时钟 = `app.tts.clock.monotonic_seconds`（Windows = `perf_counter`）”“身份验证每次回复一次”“`submit_to_first_pcm` 段用途”。
- 第 39 行：删除 `import time`（改动后不再使用），新增 `from app.tts.clock import monotonic_seconds`。
- 第 185 行：`SentenceTimingSample.submit_to_first_pcm_ms: float | None`（**追加在末尾，原 7 个字段名字/含义/顺序不变**）。
- 第 215 行：`StreamingTimingPercentiles.submit_to_first_pcm: TimingPercentiles`（**追加在末尾，原三段不变**）。
- 第 242 行：`StreamingVoiceSession.__init__(..., clock: Callable[[], float] = monotonic_seconds)`。
- 第 298-300 行：`self._verified = False` / `self._dropped_pushes = 0` / `self._finish_timed_out = False`。
- 第 366/379/392/399 行：新增只读属性 `verified` / `dropped_pushes` / `pending_sentences` / `finish_timed_out`。
- 第 429-451 行：`_reject_push_locked()`——在已持锁（`Condition` 用 `RLock`，嵌套 `_record_late` 安全）时按 `closed > failed > cancelled > finished > overloaded` 顺序选原因，`dropped_pushes += 1`，并记录 `push_rejected_<reason>@generationN`（**不写文本**）。
- 第 513-530 行：`push_text_chunk()` 命中拒绝分支时调用 `_reject_push_locked()`（空串/非字符串仍静默忽略，因为无内容可播）。
- 第 532-570 行：`finish()` 新增 docstring；第 559-569 行 join 后若 `self._thread.is_alive()` 则置 `_finish_timed_out = True` 并 `_record_late("finish_timeout")`；返回类型仍是 `tuple[str, ...]`。
- 第 681-707 行：`_verify_once()`——持锁读标志、**锁外**调用 verifier（网络 I/O 不得持锁）、成功后置 `_verified`。
- 第 709 行起：`_speak()` 首行改为 `self._verify_once()`。
- 第 804 行：样本新增 `submit_to_first_pcm_ms=_elapsed_ms(submitted_at, first_pcm_at)`。
- 第 501-502 行：`timing_percentiles()` 增加第四段归约。
- 第 853 行：`build_streaming_session(..., clock: Callable[[], float] = monotonic_seconds)`（同一 clock 继续传给 router/worker/session）。

### 2.3 `app/tts/pcm_playback.py`（531 行）

- 第 31 行新增 `from .clock import monotonic_seconds`；第 95 行 `clock` 默认值改为 `monotonic_seconds`；第 116-118 行注释同步。`import time` 保留（第 510 行 `time.time()` 事件时间戳仍用）。
- `device_submit_at` / `device_gate_open_at`、队列、闸门、背压、stop/ACK、reset、fail-closed **一行未改**。

### 2.4 `app/tts/transport.py`（308 行）

- 第 30 行新增 `from .clock import monotonic_seconds`；删除 `import time`（唯一用途就是旧默认值）；第 122 行默认值改为 `monotonic_seconds`。路由规则、fallback 记录、stage 判定未动。

### 2.5 `app/tts/waveout_pcm.py`（343 行）

- 第 22 行新增 `from .clock import monotonic_seconds`；删除 `import time`；第 116 行默认值改为 `monotonic_seconds`。`ctypes`（65 处）与 `waveOutOpen`（4 处）与 HEAD 完全一致（未新增），WinMM 生命周期、stop/ACK 语义未动。

### 2.6 计划文档

- `implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md`
  - 19.2.1：五类打点 → 六类；统计面补 `submit_to_first_pcm_ms` 与第四段；把“三/四段口径”改写为“四/五段口径”并明确 **19.2 第 3/4 条只能用 `submit_to_first_pcm` 的 `p50`/`p95` 评估**（不得用墙钟或 `first_pcm_to_device` 代替）。
  - 新增 **19.2.1.1 口径补丁：B9 缺陷修复批次**：A/B/C/D 四条逐条写清，含 B 的行为变更与语义依据、C 的新只读面、D 的向后兼容承诺，并声明本批未联网/未采数。
  - 19.2.2：时钟口径补“默认已是 `perf_counter`”；第一条根因标记为已处置（待真实复测）；缺口标记为已补 `submit_to_first_pcm`（待真实比较）。
- 本文件（新增）。

## 3. 新的公开签名 / 属性与行为变更

```python
# app/tts/clock.py（新增）
monotonic_seconds: Callable[[], float]                 # perf_counter@win32 / monotonic@其它
def clock_resolution_seconds(clock: Callable[[], float] | None = None) -> float

# app/tts/streaming_session.py
SentenceTimingSample(reply_id, generation, segment_index, ready_to_submit_ms,
                     first_pcm_to_device_ms, first_pcm_to_gate_ms, gate_to_device_ms,
                     submit_to_first_pcm_ms)           # 第 8 字段追加，前 7 个不变
StreamingTimingPercentiles(ready_to_submit, first_pcm_to_device, gate_to_device,
                           submit_to_first_pcm)        # 第 4 段追加，前 3 段不变

@property StreamingVoiceSession.verified -> bool           # 新增，默认 False
@property StreamingVoiceSession.dropped_pushes -> int      # 新增，默认 0
@property StreamingVoiceSession.pending_sentences -> int   # 新增
@property StreamingVoiceSession.finish_timed_out -> bool   # 新增，默认 False
def StreamingVoiceSession.finish() -> tuple[str, ...]      # 返回类型不变

# 默认值变更（唯一签名层面的变化；显式注入优先级不变）
StreamingVoiceSession(..., clock=monotonic_seconds)
build_streaming_session(..., clock=monotonic_seconds)
PCMPlaybackWorker(..., clock=monotonic_seconds)
VoiceStudioTransportRouter(..., clock=monotonic_seconds)
WaveOutPCMStreamBackend(..., clock=monotonic_seconds)
```

**行为变更（必须知道的两条）**

1. **B：每句验证 → 每次回复验证一次。** 依据：VoiceProfile/Provider 变更只允许在句子/回复边界生效，回复开始时验证一次已满足该边界；每句验证会把 2 次 HTTP 往返加参考音频 SHA-256（18.4 / 29.7 / 56.4 ms）计入 `ready_to_submit`。失败仍在同一处抛出，走既有 fail-closed 路径（`_error`/`_failed`、不播报、无 `played_segments`、无样本）。**新回复由新会话驱动，因此仍会重新验证一次**（已有离线测试覆盖）。
2. **C：拒绝推送与 `finish()` 超时不再静默。** `push_text_chunk()` 因 `_overloaded`/`_finished`/`_cancelled`/`_failed`/`_closed` 被拒时计入 `dropped_pushes` 并留 `push_rejected_<reason>@generationN` 脱敏诊断；`finish()` 的 join 到期且线程仍在运行时置 `finish_timed_out=True` 并留 `finish_timeout`，**不得**读作“已全部说完”。`close()` 仍按既有语义取消剩余语音（设计行为），但现在调用方可以察觉。`late_events` 因此同时承载“停止边界后到达的事件”和“被拒绝的推送”。

## 4. 测试（全部离线、注入时钟、无 sleep、无真实时长依赖）

- 新增 `tests/tts/test_tts_clock.py`（217 行，8 项）：win32 上 `monotonic_seconds is time.perf_counter` 且 `is not time.monotonic`；默认时钟单调不减；`clock_resolution_seconds()` 为正；显式时钟分辨率 == `time.get_clock_info(...)`；win32 专属断言 `monotonic == 0.015625`、`perf_counter == 1e-07`、默认 == `1e-07` 且 `monotonic > 默认`；`TypeError`/`ValueError` 边界；用 `inspect.signature` 断言**五个构造点**的 `clock` 默认值 `is monotonic_seconds`；显式注入仍生效（router 白盒 + worker 用固定值时钟打点，`device_submit_at`/`device_gate_open_at` 必须等于注入值 7.0）。
- 新增 `tests/tts/test_streaming_verification_and_overload.py`（398 行，8 项）：
  - B：20 句 paced（Event 驱动，无 sleep）后 verifier **恰好调用 1 次**、20 句全部播报、20 条样本；首次验证抛错 → `error` 被记录、`verified` 仍为 `False`、无 `played_segments`、无样本、backend 未提交、provider 未被调用、verifier 恰好 1 次；**新会话再次验证恰好一次**（累计 2 次、`reply_id` 不同）。
  - C：`max_pending_segments=1` 下推 3 句（批次被接受并 `overloaded=True`），等待 provider 进入后 `pending_sentences == 2`；再推第 4 句被拒 → `dropped_pushes == 1`、`push_rejected_overloaded@generation1`、`pending_sentences` 仍 2、文字不混入已播报；`finish` 后 3 句全播、被拒文字不在 `played_segments`。另有 `cancelled`/`finished`/`closed` 三种原因各自的诊断断言（并断言诊断里不含“句”字）。`finish_timed_out` 用极小 `segment_timeout_s=0.05` + 阻塞在 Event 上的 fake provider 构造：`finish()` 返回 `()`、`finish_timed_out` 为真、`late_events` 含 `finish_timeout`、`finish()` 自身不取消；随后 `close()` 仍取消剩余语音。对照用例证明正常 `finish()` 不置该标志、`dropped_pushes == 0`。
- 扩展 `tests/tts/test_streaming_timing.py`（1055 行，19 → 20 项）：仅新增/收紧 D 相关字段与段落断言（逐句 `submit_to_first_pcm_ms`、第四段 `count/p50/p95`、隐私字段集加入 `submit_to_first_pcm_ms`/`submit_to_first_pcm`、取消用例第四段 `count == 0`），并新增 `StreamingSynthesisLatencyTests`（脚本 `submit -> first PCM` 为 `{1,2,3,4}` 各 5 次 → p50 = 2 tick = 1.953125 ms、p95 = 4 tick = 3.90625 ms，且与 device 段 p95 = 3 tick 不同，证明不是同一序列）。
  - 必要的 B 适配（不改断言期望值）：验证改为每回复一次后，`on_verify` 只负责放行闸门，逐句 `ready → submit` 打点移到 `PCMPlaybackWorker.begin_sentence` 覆写（仍在 session 线程、仍在 `tts_submitted_at` 之前），既有 20 句精确数值一字未改；`_ObservationUnavailableWorker` 增加 `on_begin` 并对非 request 参数继续抛 `TypeError`。
- 未修改 `tests/tts/test_streaming_session.py`（26 项仍全通过）。

## 5. 验证证据（真实输出末行，2026-09-30 本机）

```text
<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest tests.tts.test_tts_clock -v
----------------------------------------------------------------------
Ran 8 tests in 0.002s

OK                                        (exit=0)

<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest tests.tts.test_streaming_verification_and_overload -v
----------------------------------------------------------------------
Ran 8 tests in 0.060s

OK                                        (exit=0)

<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest tests.tts.test_streaming_timing -v
----------------------------------------------------------------------
Ran 20 tests in 1.279s

OK                                        (exit=0)

<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest discover -s tests\tts -t . -q
Ran 504 tests in 3.295s

OK (skipped=2)                            (exit=0)

<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
Ran 1019 tests in 4.559s

OK (skipped=2)                            (exit=0)

<PROJECT_ROOT>\.venv\Scripts\python.exe -m compileall -q app tests tools
（无输出）                                  (exit=0)

git diff --check
（无 whitespace 错误；仅既有文件的 CRLF 提示 "LF will be replaced by CRLF"）  (exit=0)
```

基线为 1002 / 487 项；本轮新增 17 项后为 **1019 / 504** 项（`tests/tts/test_streaming_timing.py` 19 → 20）。

确定性：`test_tts_clock` + `test_streaming_verification_and_overload` + `test_streaming_timing` 三个模块连续运行 **25 次，失败 0 次**（runs=25 failures=0，每次 36 项）。

## 6. 静态与隐私结论

仓库外 AST 检查（比较改动文件与 `HEAD` 的 import/调用名，并对诊断结构做 `dataclasses.fields` + `repr` 泄漏扫描，脚本在 `%TEMP%`，未进仓库）：

```text
== app/tts/pcm_playback.py (vs HEAD)
   added imports  : ['clock']            removed imports: []
   added calls    : ['self._clock']      removed calls  : []
== app/tts/streaming_session.py (vs HEAD)
   added imports  : ['app.tts.clock', 'dataclasses', 'math']   removed imports: []
   added calls    : ['self._verify_once', 'self._reject_push_locked', 'self._clock', ...]
   removed calls  : []
== app/tts/transport.py (vs HEAD)
   added imports  : ['clock']            removed imports: ['time']
== app/tts/waveout_pcm.py (vs HEAD)
   added imports  : ['clock']            removed imports: ['time']
== app/tts/clock.py (new file)
   imports : ['__future__', 'sys', 'time', 'typing']
   calls   : ['TypeError', 'ValueError', 'callable', 'float', 'getattr', 'time.get_clock_info']
== deny-list scan over changed + new app/tts sources
   hits: ['app/tts/waveout_pcm.py: ctypes', 'app/tts/waveout_pcm.py: waveOutOpen']   # 均为 HEAD 既有
== diagnostic structure field sets
   SentenceTimingSample: ['reply_id', 'generation', 'segment_index', 'ready_to_submit_ms',
                          'first_pcm_to_device_ms', 'first_pcm_to_gate_ms', 'gate_to_device_ms',
                          'submit_to_first_pcm_ms']
   TimingPercentiles: ['count', 'p50', 'p95']
   StreamingTimingPercentiles: ['ready_to_submit', 'first_pcm_to_device', 'gate_to_device',
                                'submit_to_first_pcm']
== repr leak scan: none of ['\x01', '\', '/', ':\', '第1句', '.wav', 'http://'] present
ast_check_exit=0
```

- **本批未新增任何 socket/网络/子进程/设备/ctypes/注册表调用**：deny-list 只命中 `app/tts/waveout_pcm.py` 的 `ctypes`（HEAD 65 处、现在 65 处）与 `waveOutOpen`（HEAD 4 处、现在 4 处）——二者都是既有 WinMM 封装，本批对其**只改了 `clock` 默认值**；`clock.py` 只 import 标准库且只调用 `time.get_clock_info`。唯一新增的文件读写行为是文档，无任何 runtime 接线变化。
- 诊断结构全部为脱敏 id、整数或浮点/`None`；`repr` 不含句文本、PCM 字节、路径或 URL；`push_rejected_*`/`finish_timeout` 只带短原因与 generation。
- **未被修改**（`git diff --numstat` 为空、mtime 早于本批）：`app/main.py`、`app/config/settings.py`、`app/tts/real_playback.py`、`app/tts/completed_reply.py`、`app/tts/providers/**`、`tests/tts/test_streaming_session.py`、tracker 与既有 artifact（工作区里它们的改动均为本批开始前既有状态，本批一行未碰）。
- 未执行 `git add`/`commit`/`push`。

## 7. 未做与遗留

- **未采集真实数字**：没有真实 VoiceStudio、没有真实设备、没有 20 句 warm 复测。19.2 第一条（`ready_to_submit` p95 < 20 ms）**未被证明已达成**，只是移除了已知的每句验证开销；第 2 条（`gate_to_device` p95 < 50 ms）沿用上一批结果；第 3/4 条（`auto` vs `http_wav` 中位起播延迟）现在**可以**用 `timing_percentiles().submit_to_first_pcm` 评估，但尚无真实数据。
- 未接默认 runtime：`StreamingVoiceSession`/`build_streaming_session` 的默认 `clock` 变了，但没有任何生产调用方（`app/main.py`、`settings.py`、`real_playback.py`、`completed_reply.py`）被改动或新增接线；默认路径仍不被启用。
- `dropped_pushes`/`pending_sentences`/`verified`/`finish_timed_out`/`late_events` 只在会话内暴露，未落日志/事件总线/证据 ledger；`tests/tts/test_streaming_timing.py` 的时间线脚本用 `TICK_S = 2 ** -10`，其“精确”只说明打点与百分位定义正确，**不是**真实性能结论。
- `app/tts/playback_worker.py`（不在本批允许清单）仍用 `time.monotonic` 打其内部生命周期诊断，未动；`app/tools/read_only.py` 等其它模块的 `time.monotonic` 默认值同样未动。
- `tests/tts/test_streaming_timing.py` 的时间线除 D 断言外还包含 B 适配（verifier 放行闸门 + `begin_sentence` 逐句打点），因为“每句验证”这一打点时机已随 B 消失；适配保持既有期望值不变。

## 8. 下一入口

1. Lead 审阅本批 diff；
2. 另行授权真实 20 句 warm 复测（同一 VoiceProfile、同一文本集合，`--clock perf_counter` 或直接用新默认），核对：`ready_to_submit` p95 < 20 ms（B 的效果）、`gate_to_device` p95 < 50 ms（沿用）、`submit_to_first_pcm` 的 `p50` 用于 `auto` vs `http_wav` 第 3/4 条；
3. 突发场景复测时可读 `dropped_pushes`/`pending_sentences`/`finish_timed_out`/`late_events` 得到可诊断的过载事实（runner `tools/p7_real_sentence_p95_runner.py` 已在 artifact 中带上这些字段）。

## 9. 真实复测结果（Lead 于 2026-09-30 执行；本修正批次由此收口）

- 环境：与 [真实采集 checkpoint](2026-09-30-P7-b9-real-p95-measurement.md) 同一真实环境（VoiceStudio `0.5.6`、`/health` 200、CUDA RTX 3050、后端为本会话 18:41 启动的 uvicorn）。
- 场景：paced（一句一句推入，pending ≤ 1）20 句，`--clock perf_counter`；每个模式均 **20/20 播完**、`verified=True`、`dropped_pushes=0`、`finish_timed_out=False`、`error=None`、`late_events=[]`。

| 模式 | artifact | `ready_to_submit` p50/p95 (ms) | `submit_to_first_pcm` p50/p95 (ms) | `gate_to_device` p50/p95 (ms) | 墙钟 |
|---|---|---|---|---|---|
| `ws_pcm` | `…T105816Z-27ecc4fe309941ac.json` | **0.0919 / 0.2357** | 1446.8 / 1580.0 | 0.0146 / 0.0241 | 64.71 s |
| `http_wav` | `…T105931Z-e22acf008a7a4d89.json` | **0.0985 / 0.1734** | 1488.7 / 1614.6 | 0.0159 / 0.0290 | 63.76 s |
| `auto` | `…T110040Z-23f60aef1d9b4e4a.json` | **0.1083 / 0.1999** | 1462.3 / 1739.9 | 0.0125 / 0.0155 | 65.10 s |

（artifact 均在 `implementation_plan/checkpoints/artifacts/2026-09-30-P7-batch9-real-p95/`。）

- 修复前对照（同环境、同场景、同 20 句）：`ws_pcm` 20.79 / 30.40 ms、`http_wav` 29.23 / 31.44 ms → 修复后 `ready_to_submit` p95 约 **下降 130 倍**（`ws_pcm`），B 的效果被真实数据证实。
- **19.2 四条真实判定**：
  1. `ready_to_submit` p95 < 20 ms → **通过**（三种模式最差 0.236 ms）；
  2. `gate_to_device` p95 < 50 ms → **通过**（最差 0.029 ms）；
  3. `auto` 相对 `http_wav` 的中位起播延迟不得恶化超过 10% → **通过**（`submit_to_first_pcm` p50：`auto` 1462.3 ms vs `http_wav` 1488.7 ms，`auto` 反快 1.8%；**p95 如实登记**：`auto` 1739.9 vs `http_wav` 1614.6，恶化 7.8%，仍在 10% 内）；
  4. WebSocket 未显著加速 → **不声称性能收益**：`ws_pcm` p50 1446.8 ms vs `http_wav` 1488.7 ms（快 2.8%），价值在连接复用、统一 PCM 播放与兼容性，而非速度。
- 边界不变：`user_heard=null`、`speaker_acoustically_verified=false`——真实设备出声，但没有独立声学测量或用户听感确认。

## 10. 收口后的遗留

- **B 是行为变更**：身份验证从“每句一次”改为“每回复一次”。若将来要求句级身份复核，必须重新评估该跨度成本（每句 18–56 ms）并重新采集，不能假定仍满足 < 20 ms。
- **C 只修“可诊断性”**：`max_pending_segments`（默认 8）这一防失控上限与 `close()` 取消剩余语音仍是设计行为；本批只保证它不再静默（`dropped_pushes` + `push_rejected_<reason>@generationN` + `finish_timed_out` + `finish_timeout`）。突发截断阈值是否调整属产品决策，未改。
- `submit_to_first_pcm` 真实 p50 ≈ 1.45–1.49 s，说明 VoiceStudio 仍是“先生成整句再出音”（与计划第 3.2 节一致）；本轮只度量，不优化。
- 新只读面仍未落日志/事件总线/证据 ledger；`app/tts/playback_worker.py` 内部诊断仍用 `time.monotonic`（不在本批范围）。
- 未提交、未 push。
