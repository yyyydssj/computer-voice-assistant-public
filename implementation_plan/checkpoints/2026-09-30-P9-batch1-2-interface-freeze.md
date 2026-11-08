# P9 批次 1-2 接口冻结（自动收音与连续对话边界）

> 日期：2026-09-30
> 状态：接口冻结；实现进行中
> 范围：只新增输入边界模块，**不修改任何既有运行时文件**（`app/main.py`、`app/runtime/voice_entry.py`、`app/asr/**`、`app/voice_loop/**` 均不动）
> 依据：`../implementation-roadmap.md` P9 卡、`../11-vad-and-dialog-state.md`、`../shared/safety-boundaries.md` §7.7、`P8-handoff.md` §14

## 0. 不可违反的硬规则（P9）

1. VAD / 唤醒词 / 声纹 / 会话恢复**都不能决定授权**；它们只决定输入边界与是否开启新输入会话。
2. 误唤醒、误触发、声纹不匹配时：不调用工具、不生成计划、不产生授权、不创建执行。
3. 不得自动恢复或复用旧 `utterance` / `request` / 授权 / 音频缓冲；`invalidate()` 之后旧数据一律不可用。
4. 不得在用户键入、查看方案或等待授权时抢占输入。授权/执行期间对话协调器必须 `FAIL_CLOSED`。
5. 音频/对话状态与任务、授权、计划、结果事实完全隔离；音频故障不得写成任务事实。
6. 本批次不联网、不读 `.env`、不写文件（测试临时目录除外）、不启动真实设备（除真机试听由用户手动执行）。

## 1. 新增文件与写入范围

```text
app/dialog/__init__.py            新增（状态机 + 连续对话边界）
app/dialog/state.py               新增（状态、迁移表、对话状态机）
app/dialog/window.py              新增（连续对话窗口与闲置/超时边界）
app/dialog/coordinator.py         新增（turn claim 与输入许可）
app/audio/vad/auto_capture.py     新增（语音起始检测 + 被动帧缓冲 + 自动边界）
tests/dialog/__init__.py          新增
tests/dialog/test_state.py        新增（红测）
tests/dialog/test_window.py       新增（红测）
tests/dialog/test_coordinator.py  新增（红测）
tests/audio/test_auto_capture.py  新增（红测）
tests/audio/test_vad.py           既有（允许追加用例，不得削弱既有断言）
tests/integration/test_p9_batch1_2.py  新增（Lead 负责）
```

允许的最小增量修改（仅由 Lead 在集成批次执行，本批次内不得发生）：

```text
app/audio/vad/__init__.py  仅追加导出，不修改既有 VadSession/VadConfig 语义
```

## 2. 冻结接口：`app/dialog/state.py`

```python
DIALOG_SCHEMA_VERSION = 1
DIALOG_TRANSITION_REASONS: frozenset[str]  # 受限原因码白名单，见 §2.3

class DialogState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    CLARIFYING = "clarifying"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    FAIL_CLOSED = "fail_closed"
    CLOSED = "closed"

class DialogInputMode(str, Enum):
    PASSIVE = "passive"
    VOICE_TURN = "voice_turn"
    TEXT = "text"

@dataclass(frozen=True, slots=True)
class DialogDiagnostic:
    event_type: str
    subject_id: str
    reason: str
    state: DialogState
    mode: DialogInputMode | None
    def to_details(self) -> dict[str, str]: ...

@dataclass(frozen=True, slots=True)
class DialogTransition:
    subject_id: str
    from_state: DialogState
    to_state: DialogState
    reason: str
    mode: DialogInputMode | None

class DialogError(RuntimeError): ...

class DialogStateMachine:
    def __init__(
        self,
        *,
        source_instance_id: str,
        window_id: str | None = None,
        diagnostic_sink: Callable[[DialogDiagnostic], None] | None = None,
    ) -> None: ...
    @property
    def state(self) -> DialogState: ...
    @property
    def mode(self) -> DialogInputMode | None: ...
    @property
    def window_id(self) -> str: ...
    @property
    def source_instance_id(self) -> str: ...
    @property
    def transitions(self) -> tuple[DialogTransition, ...]: ...
    @property
    def is_terminal(self) -> bool: ...          # CLOSED 为 True
    @property
    def accepts_input(self) -> bool: ...       # 见 §2.2
    def transition(self, target: DialogState, *, reason: str) -> DialogTransition: ...
    def begin_voice_turn(self, *, reason: str = "voice_turn_started") -> DialogTransition: ...  # IDLE->USER_SPEAKING(mode=VOICE_TURN)
    def begin_text_turn(self, *, reason: str = "text_turn_started") -> DialogTransition: ...    # IDLE->THINKING(mode=TEXT)
    def fail_closed(self, *, reason: str) -> DialogTransition: ...   # 任意非终态 -> FAIL_CLOSED
    def recover(self, *, reason: str) -> DialogTransition: ...       # FAIL_CLOSED->IDLE，且必须换新 window_id
    def close(self, *, reason: str) -> DialogTransition: ...         # 任意非终态 -> CLOSED
```

### 2.1 迁移表（白名单；未列出的迁移一律抛 `DialogError`）

```text
IDLE                  -> LISTENING, USER_SPEAKING, THINKING, FAIL_CLOSED, CLOSED
LISTENING             -> USER_SPEAKING, THINKING, IDLE, FAIL_CLOSED, CLOSED
USER_SPEAKING         -> TRANSCRIBING, IDLE, FAIL_CLOSED, CLOSED
TRANSCRIBING          -> THINKING, CLARIFYING, IDLE, FAIL_CLOSED, CLOSED
THINKING              -> SPEAKING, CLARIFYING, AWAITING_AUTHORIZATION, EXECUTING, IDLE, FAIL_CLOSED, CLOSED
CLARIFYING            -> LISTENING, USER_SPEAKING, IDLE, FAIL_CLOSED, CLOSED
AWAITING_AUTHORIZATION-> IDLE, FAIL_CLOSED, CLOSED          # 只允许取消/恢复；不得自行继续
EXECUTING             -> IDLE, FAIL_CLOSED, CLOSED          # 执行事实由 P5 链负责，这里只挡输入
SPEAKING              -> LISTENING, IDLE, FAIL_CLOSED, CLOSED
FAIL_CLOSED           -> IDLE, CLOSED
CLOSED                -> （终态，无迁移）
```

硬约束（必须有红测）：

- `AWAITING_AUTHORIZATION` 与 `EXECUTING` 之外的状态不得迁移到这两个状态；从 `LISTENING` 直接进 `EXECUTING` 必须被拒绝。
- 进入 `AWAITING_AUTHORIZATION` / `EXECUTING` 后，`accepts_input` 必须为 `False`。
- `CLOSED` 之后任何迁移（含 `fail_closed`/`close`）必须被拒绝。
- 不允许自迁移（`target is self.state` 抛错）。
- `transition()` 目标不是 `DialogState` 时抛 `TypeError`。
- `reason` 必须是受限标识符且属于 `DIALOG_TRANSITION_REASONS`，否则抛 `DialogError`（不得接受自由文本）。

### 2.2 `accepts_input` 定义（唯一权威）

```python
accepts_input == state in {IDLE, LISTENING} and state is not CLOSED
```

其余状态（`USER_SPEAKING`/`TRANSCRIBING`/`THINKING`/`CLARIFYING`/`SPEAKING`/`AWAITING_AUTHORIZATION`/`EXECUTING`/`FAIL_CLOSED`）一律 `False`。必须覆盖 `AWAITING_AUTHORIZATION` 与 `EXECUTING` 为 `False` 的红测。

### 2.3 原因码白名单（`DIALOG_TRANSITION_REASONS`，至少包含）

```text
turn_started, voice_turn_started, text_turn_started, listening_started,
speech_started, speech_finished, transcription_ready, route_handled,
clarify_required, reply_ready, authorization_required, execution_requested,
speech_started_output, speech_finished_output, turn_completed, idle_timeout,
window_timeout, cancelled, stopped, input_degraded, audio_failed, asr_failed,
agent_failed, provider_unavailable, user_requested_text, recovered, shutdown
```

## 3. 冻结接口：`app/dialog/window.py`

```python
@dataclass(frozen=True, slots=True)
class DialogWindowConfig:
    idle_timeout_seconds: int = 300        # != 0；最长闲置
    max_turns: int = 8                     # >= 1；超限需要重新唤醒
    max_window_seconds: int = 1800         # >= 1；整段会话硬上限
    listen_timeout_seconds: int = 8        # >= 1；一次 LISTENING 内等待语音起始的上限

class DialogWindowState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"

class DialogWindowCloseReason(str, Enum):
    IDLE_TIMEOUT = "idle_timeout"
    MAX_TURNS = "max_turns"
    MAX_WINDOW_DURATION = "max_window_duration"
    USER_CANCELLED = "user_cancelled"
    FAIL_CLOSED = "fail_closed"
    SHUTDOWN = "shutdown"

@dataclass(frozen=True, slots=True)
class DialogWindowStatus:
    window_id: str
    state: DialogWindowState
    turn_count: int
    close_reason: DialogWindowCloseReason | None
    expired: bool
    def to_details(self) -> dict[str, str]: ...

class DialogError(...)  # 复用 §2

class DialogWindow:
    def __init__(self, *, config: DialogWindowConfig, now: Callable[[], datetime] | None = None,
                 window_id: str | None = None, diagnostic_sink=None) -> None: ...
    @property
    def window_id(self) -> str: ...
    @property
    def status(self) -> DialogWindowStatus: ...
    @property
    def state(self) -> DialogWindowState: ...
    @property
    def close_reason(self) -> DialogWindowCloseReason | None: ...
    def open(self) -> DialogWindowStatus: ...                 # 只有 CLOSED 才能重新 open
    def note_activity(self) -> DialogWindowStatus: ...        # 刷新闲置计时；已关闭则返回关闭状态且不改写
    def begin_turn(self) -> DialogWindowStatus: ...           # turn_count += 1；超 max_turns 即关闭
    def end_turn(self) -> DialogWindowStatus: ...
    def check_timeouts(self) -> DialogWindowStatus: ...       # 同时判闲置与整段窗口上限
    def close(self, *, reason: DialogWindowCloseReason) -> DialogWindowStatus: ...
    def is_open(self) -> bool: ...
```

硬约束（必须有红测）：

- `now` 可注入；所有时间判定基于注入时钟，不使用真实 `datetime.now()`（除非未注入）。
- `idle_timeout_seconds` 达到即关闭，原因 `idle_timeout`；`max_window_seconds` 达到即关闭，原因 `max_window_duration`（两者同时到期时判定顺序固定并写进测试）。
- `begin_turn` 超过 `max_turns` 时关闭，原因 `max_turns`，且 `turn_count` 不得超过 `max_turns`。
- 已关闭窗口的 `note_activity` / `begin_turn` / `end_turn` 不得复活窗口，也不得改写既有 `close_reason`。
- `open()` 必须产生新的 `window_id`（不得复用旧身份）。
- 非 `DialogWindowCloseReason` 值抛错；不得接受自由文本原因。

## 4. 冻结接口：`app/dialog/coordinator.py`

```python
@dataclass(frozen=True, slots=True)
class TurnClaim:
    claim_id: str
    window_id: str
    turn_id: str
    source: VoiceInputSource | None      # 复用 app.voice_loop.boundary.VoiceInputSource；被动收音为 None
    layer: str                           # "voice" | "text"
    issued_at: str
    def __post_init__(self) -> None: ...  # 标识符/来源/UTC 时间严格校验

@dataclass(frozen=True, slots=True)
class TurnClaimDecision:
    granted: bool
    claim: TurnClaim | None
    reason: str
    def __post_init__(self) -> None: ...  # granted 与 claim 必须一致

@dataclass(frozen=True, slots=True)
class InputPermit:
    granted: bool
    reason: str
    state: DialogState
    mode: DialogInputMode | None

class DialogCoordinator:
    def __init__(
        self,
        *,
        machine: DialogStateMachine,
        window: DialogWindow,
        diagnostic_sink: Callable[[DialogDiagnostic], None] | None = None,
    ) -> None: ...
    @property
    def machine(self) -> DialogStateMachine: ...
    @property
    def window(self) -> DialogWindow: ...
    @property
    def active_claim(self) -> TurnClaim | None: ...
    @property
    def closed_claims(self) -> tuple[TurnClaim, ...]: ...
    def request_input(self, *, layer: str, source: VoiceInputSource | None = None) -> InputPermit: ...
    def claim_turn(self, *, layer: str, source: VoiceInputSource | None = None,
                   turn_id: str | None = None) -> TurnClaimDecision: ...
    def release_turn(self, claim: TurnClaim, *, reason: str) -> None: ...
    def suspend_for_authorization(self, *, reason: str) -> DialogTransition: ...   # -> FAIL_CLOSED? 见下
    def suspend_for_execution(self, *, reason: str) -> DialogTransition: ...
    def resume_listening(self, *, reason: str) -> DialogTransition: ...
    def note_speech_started(self, *, claim: TurnClaim) -> DialogTransition: ...
    def note_speech_finished(self, *, claim: TurnClaim) -> DialogTransition: ...
    def invalidate(self, *, reason: str) -> None: ...
```

`layer` 只允许 `"voice"` / `"text"`；其他值抛 `DialogError`。`suspend_for_authorization` / `suspend_for_execution` 把状态机迁到 `AWAITING_AUTHORIZATION` / `EXECUTING`（走 §2.1 白名单），并保证此时 `request_input` 返回 `granted=False`。

硬约束（必须有红测）：

- 同一时刻最多一个活动 claim；第二个 `claim_turn` 必须被拒绝（`granted=False`，`claim=None`，原因码受限）。
- claim 已 `release` 后再次 `release` 抛 `DialogError`；外来 claim（另一 coordinator 或伪造 `claim_id`）一律拒绝。
- claim 的 `window_id` 必须等于当前窗口；`window_id` 变化（超时关闭后重新 `open()`）后旧 claim 全部失效。
- `request_input` 在 `AWAITING_AUTHORIZATION`、`EXECUTING`、`FAIL_CLOSED`、`CLOSED` 下必须 `granted=False`，且**不得**改变状态机状态。
- 迟到/旧 claim 的 `note_speech_started` / `note_speech_finished` 必须被拒绝，且不得迁移状态。
- `invalidate(reason)` 后：`active_claim is None`、所有旧 claim 可用性为假、`request_input` 为 `granted=False`。
- 若 `window` 已关闭（闲置/超时/轮数上限），`request_input` 必须失败并要求重新唤醒（原因码区分 `window_closed`）。
- 模块不得导入 `app.safety`、`app.executor`、`app.plans`、`app.auditing`、`app.verification`、`app.tools`（集成测试会断言引用数为 0）。

## 5. 冻结接口：`app/audio/vad/auto_capture.py`

复用既有 `SpeechDetector` Protocol（`app.audio.vad.SpeechDetector`），不新增检测器协议。

```python
@dataclass(frozen=True, slots=True)
class SpeechStartConfig:
    speech_frame_threshold: int = 3        # >= 1；连续命中才认为开始
    reset_on_miss: bool = True             # 命中中断时是否清零连续计数（False 表示累计）
    min_rms_dbfs: float | None = None      # 可选：低于该能量视为无语音；None 表示不使用

@dataclass(frozen=True, slots=True)
class SpeechStartDecision:
    observed: bool
    started: bool
    speech_frames: int
    observed_frames: int

class SpeechStartDetector:
    def __init__(self, *, config: SpeechStartConfig) -> None: ...
    @property
    def config(self) -> SpeechStartConfig: ...
    @property
    def started(self) -> bool: ...
    @property
    def speech_frames(self) -> int: ...
    @property
    def observed_frames(self) -> int: ...
    def observe(self, packet: AudioPacket) -> SpeechStartDecision: ...
    def reset(self) -> None: ...

class PassiveFrameDecision(str, Enum):
    BUFFERED = "buffered"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_DEVICE_EPOCH = "stale_device_epoch"
    INVALID_AUDIO_FRAME = "invalid_audio_frame"
    FRAME_DISCONTINUITY = "frame_discontinuity"
    OVERFLOW = "overflow"
    INVALIDATED = "invalidated"

@dataclass(frozen=True, slots=True)
class PassiveFrameObservation:
    decision: PassiveFrameDecision
    buffered_frames: int
    speech_started: bool

class AutoCaptureConfig:
    """组合既有 VadConfig 与被动缓冲参数；全部 fail closed。"""
    def __init__(self, *, vad: VadConfig, passive_capacity_frames: int = 25,
                 speech_start: SpeechStartConfig | None = None,
                 max_utterance_ms: int = 30000,
                 speech_start_timeout_ms: int = 8000) -> None: ...
    # 属性：vad / passive_capacity_frames / speech_start / max_utterance_ms / speech_start_timeout_ms
    # __post_init__ 级别校验：所有数值必须为正，且 passive_capacity_frames >= 1

class AutoCaptureEndReason(str, Enum):
    SILENCE_TIMEOUT = "silence_timeout"
    MAX_UTTERANCE_DURATION = "max_utterance_duration"
    SPEECH_START_TIMEOUT = "speech_start_timeout"
    CANCELLED = "cancelled"
    INVALID_AUDIO_FRAME = "invalid_audio_frame"
    FRAME_DISCONTINUITY = "frame_discontinuity"
    STALE_AUDIO_SESSION = "stale_audio_session"
    STALE_GENERATION = "stale_generation"
    STALE_STREAM = "stale_stream"
    STALE_DEVICE_EPOCH = "stale_device_epoch"
    DETECTOR_UNAVAILABLE = "detector_unavailable"
    DETECTOR_FAILURE = "detector_failure"
    BUS_CLOSED = "bus_closed"
    INVALIDATED = "invalidated"

class AutoCaptureState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"     # 被动拾音，等待语音起始
    CAPTURING = "capturing"     # 已确认语音起始，累积 utterance
    ENDED = "ended"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class AutoCaptureDecision:
    state: AutoCaptureState
    reason: AutoCaptureEndReason | None
    speech_started: bool
    buffered_frames: int
    captured_frames: int
    emitted: tuple[AudioPacket, ...]   # 只在 ENDED 或明确交付时非空
    def to_details(self) -> dict[str, str]: ...

class AutoCaptureSession:
    """P9 自动收音：语音起始 -> 复用 VadSession 结束判定 -> 一次性交付 pre-roll + 语音帧。"""
    def __init__(self, *, bus: AudioBus, config: AutoCaptureConfig,
                 detector: SpeechDetector | None = None,
                 start_detector: SpeechStartDetector | None = None) -> None: ...
    @property
    def state(self) -> AutoCaptureState: ...
    @property
    def terminal_reason(self) -> AutoCaptureEndReason | None: ...
    @property
    def buffered_frames(self) -> int: ...
    @property
    def captured_frames(self) -> int: ...
    @property
    def speech_started(self) -> bool: ...
    def listen(self) -> AutoCaptureDecision: ...          # IDLE -> LISTENING
    def observe(self, packet: AudioPacket) -> AutoCaptureDecision: ...
    def finish(self) -> AutoCaptureDecision: ...          # 外部要求结束（如唤醒窗口关闭）
    def deliver(self) -> tuple[AudioPacket, ...]: ...     # 一次性交付；只能成功一次
    def cancel(self) -> AutoCaptureDecision: ...
    def invalidate(self, *, reason: str) -> AutoCaptureDecision: ...   # 交付前作废；之后 deliver 返回空且 state=FAILED
```

硬约束（必须有红测）：

- `observe` 在非 `LISTENING`/`CAPTURING` 状态下必须被拒绝（`reason` 对应 `INVALIDATED` 或状态错，且不迁移）。
- 语音起始前：帧写入被动缓冲且**不得**进入 `VadSession`；`buffered_frames <= passive_capacity_frames`（有界，溢出丢弃最旧帧）。
- 语音起始后：先按序回放被动缓冲（pre-roll），再继续累积；`captured_frames` 与交付顺序必须严格按 `event_sequence` 递增，不得重复、不得跳号。
- 被动缓冲中的旧边界帧（`audio_session_id` / `provider_generation` / `stream_id` / `device_epoch` 不匹配）必须 `fail closed`，不得进入交付。
- `speech_start_timeout_ms` 到期仍未起始 → `SPEECH_START_TIMEOUT`；`max_utterance_ms` 达到 → `MAX_UTTERANCE_DURATION`（二者都必须被测试）。
- `deliver()` 只能成功一次；第二次返回空。
- `invalidate(reason)` 之后：`deliver()` 返回空、`state=FAILED`、被动缓冲与已捕获帧全部清除。
- 检测器返回非布尔或抛异常 → `DETECTOR_FAILURE`；未提供检测器 → `DETECTOR_UNAVAILABLE`（不得默认放行）。
- `cancel()` 关闭 bus 且 `deliver()` 返回空。
- 模块不得新增文件/进程/网络/线程入口。

## 6. 测试与验收要求（每个写入范围）

- 先写红测（拒绝路径优先），再写最小实现；红测必须能在实现前失败（提交前用 `git stash` 不必验证，但需在报告中说明红测先于实现）。
- 每个范围至少覆盖：成功路径 1-2 条 + 拒绝路径为主。
- 命令：

```powershell
cd '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q   # 全量，基线 1394 OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools
```

- 新增/修改文件必须是严格 UTF-8（无 BOM、无 `U+FFFD`）。
- 不得修改 `app/safety/**`、`app/executor/**`、`app/plans/**`、`app/auditing/**`、`app/verification/**`、`app/tools/**`、`app/tts/**`、`app/providers/**`、`app/main.py`、`app/runtime/**`、`app/asr/**`、`app/voice_loop/**`。

## 6.1 实现期澄清（Lead 裁定，2026-09-30，与冻结接口同等效力）

`app/dialog/**`：

1. `DialogWindow` 构造后为 `CLOSED`；会话开始必须显式 `open()`。已 `OPEN` 时再 `open()` 抛 `DialogError`；**每次成功 `open()` 都必须生成新的 `window_id`**。未开窗时 `request_input()` 为 `granted=False`、`reason="window_closed"`。
2. `DialogWindowStatus.expired` = 窗口已关闭且 `close_reason ∈ {idle_timeout, max_window_duration, max_turns}`；`user_cancelled` / `fail_closed` / `shutdown` 不算 `expired`。
3. 新增 `DIALOG_INPUT_REFUSAL_REASONS = {granted, claim_active, window_closed, state_blocked, closed, invalidated}` 并纳入 `app/dialog/__init__.py` 的 `__all__`。`transition` / `recover` / `close` / `invalidate` 的 `reason` 仍严格限定在 `DIALOG_TRANSITION_REASONS`；`release_turn` 的 `reason` 只做受限标识符校验。
4. `TurnClaim.issued_at` 使用真实 UTC（`Z` 结尾、可被 `parse_utc_timestamp` 解析）。
5. claim 有效性以 `window.window_id` 为准；`machine.window_id` 仅作诊断标签，`recover()` 时必须轮换。
6. `suspend_for_authorization` / `suspend_for_execution` 必须作废当前活动 claim；状态迁到 `AWAITING_AUTHORIZATION` / `EXECUTING`（不是 `FAIL_CLOSED`）。
7. 这些状态下 `request_input()` 必须是**只读拒绝**：`granted=False` 且不得改变状态机状态；原因码必须能区分 `state_blocked` 与 `window_closed`。
8. 三个模块只发出 `DialogDiagnostic`；窗口事件映射 `OPEN→IDLE`、`CLOSED→CLOSED`，`mode=None`。

`app/audio/vad/auto_capture.py`：

9. `finish()` → `state=ENDED`、`terminal_reason=None`，已捕获帧可 `deliver()` 一次；`cancel()` 才丢弃并关闭 bus。
10. `min_rms_dbfs=None` = 不启用能量门槛，任何有效帧都计为命中；不得视为 `DETECTOR_UNAVAILABLE`。
11. 连续性台账由 `AutoCaptureSession` 自持：以 `observe()` 收到的**第一帧**为基线，后续 `event_sequence` 与 `frame_index` 必须严格 +1，否则 `FRAME_DISCONTINUITY` fail closed。**不得用反射修改 `VadSession` 的私有台账**；`VadSession` 只承担静音结束判定，其输入本身即连续。
12. pre-roll **必须**完整回放并计入交付（不得为迁就台账丢弃最旧帧而截断用户开头的话）；交付顺序严格按收到顺序递增，无重复、无跳号。
13. `PassiveFrameDecision` / `PassiveFrameObservation` 保持内部记录类型，只经 `to_details()["passive_decision"]` 暴露，不新增公开访问器。
14. `invalidate()` 之后 `observe()` 与 `deliver()` 必须被拒绝/返回空，被动缓冲与已捕获帧全部清空，且不得关闭或复用旧边界。
15. 喂给 `VadSession` 的必须是**连续性重锚定副本**（原因：`VadSession` 台账固定从 `event_sequence=1`/`frame_index=0` 起算，而被动缓冲溢出或流中途接入会让首帧序号不为 1，原始帧直喂会导致整轮 `FRAME_DISCONTINUITY` 误伤）。副本只重编号 `event_sequence`/`frame_index`，其余身份字段与 payload 与原始帧一致；`deliver()`/`emitted` 仍输出**原始帧**且顺序严格递增；`AutoCaptureSession` 自有台账仍对原始帧严格校验，重锚定不得削弱任何拒绝路径。模块 docstring 必须写明副本是“仅给 VAD 使用的逻辑视图，不代表真实采集时间”。

## 7. 本批次明确不做（延期到 P9 B3/B4 或更后）

- 唤醒词检测（B3，sherpa-onnx KWS 方向已定，未实现）。
- 声纹过滤（B4，sherpa-onnx speaker embedding 方向已定，未实现）。
- 真实设备自动收音真机试听（需用户参与；B2 完成后另行安排）。
- 主程序 `--voice-auto` 接线（需单独授权，本轮不改 `app/main.py`）。
- AEC / 播放期间监听 / 全双工（P10）。
- 任何授权、计划、执行、验证、审计改动。
