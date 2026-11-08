# P7 Batch 9：流式语音句级时间线打点与离线 p95 统计

- 日期：2026-09-30（本文件已就地更新：追加「起播缓冲闸门」只读观测与 `gate_to_device` 段，前四类打点与 14 项测试为同批原有内容）
- 项目：`<PROJECT_ROOT>`
- 状态：**完成（离线验证通过）；未采集任何真实 p95，未接默认 runtime，未联网，未用真实设备**
- 范围：为流式链路加五类单调时钟打点、只读统计面与显式最近秩百分位纯函数；离线单测覆盖 20 句样本、百分位边界、缺失段、隐私与取消/迟到隔离，并新增「除起播缓冲外的额外开销」段。
- 对应计划：`implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md` 第 19.2 节（19.2.1 实现注记已同步）
- 允许改动的文件（严格限定，实际只改了这些）：`app/tts/streaming_session.py`、`app/tts/pcm_playback.py`、新增 `tests/tts/test_streaming_timing.py`、计划 19.2.1 注记、本 checkpoint。

## 1. 五类打点（`time.monotonic`，可注入）

| 打点 | 插入点（`app/tts/streaming_session.py`） | 说明 |
|---|---|---|
| `sentence_ready_at` | `_enqueue()` 第 506 行 `ready_at = self._clock()` | 分句器产出的完整句进入待合成队列的时刻；随 `_PendingSentence(text, ready_at)` 一起入队 |
| `tts_submitted_at` | `_speak()` 第 571 行 `submitted_at = self._clock()` | 在 `transport_router.stream_sentence()` 之前；provider 生成器首次 `next()` 才真正开始，故这是不触碰 transport 内部的最接近提交点 |
| `first_pcm_at` | `_speak()` 第 585 行 `first_pcm_at = self._clock()` | 该句第一块 PCM 从 provider 返回后（取消检查之后，故取消后到达的块连时间戳都不打） |
| `gate_open_at` | `_speak()` 第 625 行 `self._worker.device_gate_open_at(request)` 只读回读 | worker 在 `_take_submission_locked()`（第 386 行）内、`self._started = True`（第 394 行）的同一分支里于第 401 行取样（起播缓冲闸门首次满足、决定开始向设备提交的那一刻）、第 402 行按 request 身份发布；第 243-244 行随 `begin_sentence`（第 219 行）重置 |
| `device_submit_at` | `_speak()` 第 619 行 `self._worker.device_submit_at(request)` 只读回读 | worker 在 `backend.submit()` 成功返回后用同一单调时钟打点（`app/tts/pcm_playback.py` 第 419 行取样、第 439-440 行发布；第 241-242 行随 `begin_sentence` 重置） |

两处 worker 观测（第 164 行 `device_submit_at`、第 187 行 `device_gate_open_at`）完全同构：同一把 `self._condition` 锁下读写、只返回裸 `float`、按 request 身份匹配（身份不一致或未取到返回 `None`）、`begin_sentence()` 重置、`__init__` 第 118-121 行初始化。闸门阈值、`_started` 语义、背压、stop/ACK、reset、fail-closed 与既有 `device_submit_at` 语义**一行未改**。

时钟注入：`StreamingVoiceSession(..., clock=time.monotonic)`、`PCMPlaybackWorker(..., clock=time.monotonic)`、`build_streaming_session(..., clock=time.monotonic)`（把同一 clock 交给 router/worker/session，保证各跨度可相减）。默认值与原行为逐位一致；未接默认 runtime，`app/main.py`、`app/config/settings.py`、`app/tts/real_playback.py`、`app/tts/completed_reply.py`、`providers/**`、`transport.py`、`tests/tts/test_streaming_session.py` 一行未改。

## 2. 只读统计面（不返回文本/PCM/路径/URL）

```python
SentenceTimingSample(reply_id: str, generation: int, segment_index: int,
                     ready_to_submit_ms: float | None, first_pcm_to_device_ms: float | None,
                     first_pcm_to_gate_ms: float | None, gate_to_device_ms: float | None)  # 冻结 dataclass, slots
TimingPercentiles(count: int, p50: float | None, p95: float | None)                          # 冻结 dataclass, slots
StreamingTimingPercentiles(ready_to_submit: TimingPercentiles,
                           first_pcm_to_device: TimingPercentiles,
                           gate_to_device: TimingPercentiles)                                # 冻结 dataclass, slots

@property
def StreamingVoiceSession.timing_samples(self) -> tuple[SentenceTimingSample, ...]
def StreamingVoiceSession.timing_percentiles(self) -> StreamingTimingPercentiles
def PCMPlaybackWorker.device_submit_at(self, request: VoiceStudioSentenceRequest) -> float | None
def PCMPlaybackWorker.device_gate_open_at(self, request: VoiceStudioSentenceRequest) -> float | None
```

- 样本只包含**播放完成**（`PlaybackState.COMPLETED`）的句子：`_speak()` 第 636-640 行在 `_played.append(sentence)` 的同一临界区里追加，且仅当 `not self._cancelled`；取消、迟到块、背压、传输失败、播放未完成的句子**不入样**，只留在既有 `late_events`。
- 同一句至多一条（唯一追加点），样本顺序即播放顺序；`close()` 后仍可读。
- 新增的两个字段追加在原有 5 个字段之后，原有字段顺序与含义不变；`StreamingTimingPercentiles` 的第三段同样追加在末尾，`ready_to_submit`、`first_pcm_to_device` 字段名与含义不变。
- 无新线程、新队列、新锁：复用既有 `self._condition`；只在既有返回路径上增加会话侧四次打点（ready/submit/first PCM/gate 回读）、worker 侧两次打点（gate/submit）与两次只读回读，`generation`、`late_events`、取消顺序、`overloaded`、stop ACK、fail-closed 语义未动；所有既有公开签名不变（新增均为带默认值的关键字参数）。

## 3. 百分位定义（显式、纯函数、仅标准库）

`nearest_rank_percentile(samples, percentile) -> float | None`（`app/tts/streaming_session.py` 第 82 行）：

1. 丢弃非有限值/非数值（`None` 由调用方先过滤）；
2. 剩余 `count` 个值升序排序；
3. `rank = ceil(percentile * count)`，夹到 `[1, count]`；
4. 结果 = `sorted_values[rank - 1]` —— **不插值、不取中间两值平均**；
5. 空样本返回 `None`；`percentile` 必须在 `(0, 1]`，否则 `ValueError`。

`timing_percentiles()` 用 `0.5` 与 `0.95`；缺失（`None`）的段不计入该段的 `count`，整段为空时 `count=0` 且 `p50/p95` 均为 `None`；**实测的 `0.0` 是有效样本，照常计入 `count`**。

## 4. 三/四段口径（哪一段用于判定必须写死）

| 段 | 定义 | 用途 |
|---|---|---|
| `ready_to_submit_ms` | `tts_submitted_at - sentence_ready_at` | 计划 19.2 第一条：p95 < 20 ms |
| `first_pcm_to_device_ms` | `device_submit_at - first_pcm_at` | **原始观测，含配置的起播缓冲累积**；只作诊断，**不得**直接与 50 ms 比较 |
| `first_pcm_to_gate_ms` | `gate_open_at - first_pcm_at` | 起播缓冲累积本身，纯诊断 |
| `gate_to_device_ms` | `device_submit_at - gate_open_at` | **已排除起播缓冲的「额外开销」**；计划 19.2 第二条「除配置的起播缓冲外 p95 额外开销 < 50 ms」**只能用这一段的 p95 判定** |

闸门与首次 `backend.submit()` 实际同刻时如实记为 `0.0`（不加任何人为差值、不最小化到正数），且该 `0.0` 仍计入 `count`。任一端的打点缺失（`None`）时对应段为 `None`。三处口径一致：本文件、计划 19.2.1 注记、`streaming_session.py` 的模块与 dataclass docstring。

## 5. 测试（`tests/tts/test_streaming_timing.py`，19 项，全部离线）

原有 14 项（20 句精确跨度与百分位、缺失段、取消/迟到隔离、隐私、纯函数边界）全部保留并通过；本轮新增 5 项：

- `test_gate_block_yields_exact_spans_and_third_segment`：两段式 provider（首块 10 ms 非终块 + 末块 10 ms 终块，`start_buffer_ms=15`，故闸门只可能被末块打开）下，逐句精确断言 `first_pcm_to_gate_ms = ((index%5)+1)*TICK_MS`、`gate_to_device_ms = ((index%3)+1)*TICK_MS`、以及 `first_pcm_to_device_ms` **恰好等于两者之和**；三段百分位手算期望：`gate_to_device` 样本 `{1,2,3}` 计数 `{6,7,7}` → p50 = 2 tick = 1.953125 ms、p95 = 3 tick = 2.9296875 ms；`first_pcm_to_device` 20 个值 `[4,6,5,7,4,3,5,7,6,3,5,4,6,8,2,4,6,5,7,4]` → p50 = 5 tick = 4.8828125 ms、p95 = 7 tick；并断言该段 p50 与原始段 p50 不同（证明两者不是同一序列）。
- `test_gate_and_submission_on_the_same_instant_report_exact_zero`：`gate -> device` 步长为 0 的脚本下断言 `gate_to_device_ms == 0.0`、`count == 1`、`p50 == p95 == 0.0` 且 `p50 is not None`。
- `test_missing_gate_observation_only_removes_the_exclusive_segment`：只隐藏闸门观测时，两段新字段均为 `None`、`gate_to_device.count == 0`，而 `first_pcm_to_device` 仍为 20 且 p50/p95 不变。
- `test_missing_submission_keeps_gate_diagnostics_out_of_the_count`：提交观测隔句缺失、闸门观测保留时，`first_pcm_to_gate_ms` 逐句仍在，`gate_to_device.count == 10`（按 10 个样本重算：p50 = 2 tick、p95 = 3 tick）。
- `test_gate_observation_is_identity_matched_and_reset_per_sentence`：直接对 worker 断言身份不匹配返回 `None`、非 `SentenceTimingRequest` 抛 `TypeError`、`submitted_at - opened_at` 精确等于脚本步长、`begin_sentence()` 后旧 request 的闸门与提交戳都取不到。
- 既有 14 项的断言已同步收紧（原取消用例的 3 段 `count=0` 循环、缺失段用例的 `gate_to_device` 计数、单块时间线下 `first_pcm_to_gate_ms == 0.0` 且 `gate_to_device == first_pcm_to_device`）。
- 新测试全部使用注入时钟（`ScriptedClock`/`TICK_S = 2 ** -10`），**无 sleep、无真实时间依赖**；`_ObservationUnavailableWorker` 已同步覆盖新观测面（`available` 隐藏提交戳，`gate_available` 隐藏闸门戳，缺省继承 `available`）。
- 回归：`tests/tts/test_streaming_session.py` 现有 26 项仍通过且该文件未被修改。

## 6. 验证证据（真实输出末行，2026-09-30 本轮重跑）

```text
<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest tests.tts.test_streaming_timing -v
----------------------------------------------------------------------
Ran 19 tests in 1.278s

OK                                        (exit=0)

... -m unittest discover -s tests\tts -t . -q
Ran 487 tests in 3.281s

OK (skipped=2)                            (exit=0)

... -m unittest discover -s tests -t . -p "test_*.py" -q
Ran 1002 tests in 4.353s

OK (skipped=2)                            (exit=0)

... -m compileall -q app tests tools
（无输出）                                  (exit=0)

git diff --check
（无 whitespace 错误；仅既有文件的 CRLF 提示 "LF will be replaced by CRLF"）  (exit=0)
```

同批此前的记录为 14 / 482 / 997 项；本轮新增 5 项后为 19 / 487 / 1002 项。

确定性时间线稳定性：同一新测试模块连续运行 25 次，失败 0 次（runs=25 failures=0）。

AST 离线检查（脚本在仓库外，比较改动文件与 `HEAD` 版本的 import 与调用名，并对诊断结构做 `dataclasses.fields` + `repr` 泄漏扫描）：

```text
== app/tts/pcm_playback.py (vs HEAD)
   added imports : []
   added calls   : ['self._clock']
== app/tts/streaming_session.py (vs HEAD)
   added imports : ['dataclasses', 'math', 'time']
   added calls   : ['SentenceTimingSample', 'StreamingTimingPercentiles', 'TimingPercentiles',
                    '_PendingSentence', '_elapsed_ms', '_finite_sorted', '_timing_percentiles',
                    'ceil', 'isfinite', 'nearest_rank_percentile', 'self._clock',
                    'self._worker.device_gate_open_at', 'self._worker.device_submit_at', ...]
== deny-list hits
   none (no socket/network/subprocess/device/ctypes/registry import or call added)
== diagnostic structure field sets
   SentenceTimingSample: ['reply_id', 'generation', 'segment_index', 'ready_to_submit_ms',
                          'first_pcm_to_device_ms', 'first_pcm_to_gate_ms', 'gate_to_device_ms']
   TimingPercentiles: ['count', 'p50', 'p95']
   StreamingTimingPercentiles: ['ready_to_submit', 'first_pcm_to_device', 'gate_to_device']
== repr leak scan: none of ['\x01', '\', '/', ':\', '第1句', '.wav', 'http://', 'PCMChunk', 'bytes'] present
ast_check_exit=0
```

新增 import 仅标准库 `dataclasses`/`math`/`time`（`time` 原本已在 `pcm_playback.py` 中存在）；本批（含闸门追加）无 socket/网络/子进程/设备/注册表/ctypes 调用；诊断结构字段全部是脱敏 id、整数或浮点/`None`，`repr` 中不含句文本、PCM 字节、路径或 URL。

## 7. 未做与遗留

- **未联网**、**未访问 VoiceStudio**、**未使用扬声器/麦克风**、**未占用任何设备**、**未安装依赖**。
- **未采集真实 p95**：没有 warm 模型、没有真实设备、没有 20 句真实 warm 采集；19.2 的 p95<20 ms 与 <50 ms 两条目标**未被证明**，本批不声称任何性能收益或 regress/improvement。注入时钟下的样本只是打点自检，**不是**真实性能结论。
- **未接默认 runtime**：`build_streaming_session` 之外没有改动任何接线；`clock` 默认 `time.monotonic`，默认路径行为逐位不变。
- 真实采集仍需另行授权；届时判定第二条指标请只读 `timing_percentiles().gate_to_device`，`first_pcm_to_device` 仅作对照。
- 统计面目前只在会话内暴露，未落日志/事件总线/证据 ledger；未做并发句（计划本身禁止单连接并发）与超长回复下的打点压力测试。
- 未执行 `git add`/`commit`/`push`；工作区其它既有改动（含他人/历史 staged 与 untracked 文件）未被本轮触碰。

## 8. 下一入口

1. 用户审阅本轮 diff；
2. 另行授权真实 20 句 warm 采集（同一 VoiceProfile、同一文本集合），用 `timing_percentiles()` 的 `gate_to_device` 段出具「除起播缓冲外 p95 < 50 ms」的真实证据，并用 `ready_to_submit` 段核对 p95 < 20 ms；
3. 或继续 P7 跟踪表后续项。
