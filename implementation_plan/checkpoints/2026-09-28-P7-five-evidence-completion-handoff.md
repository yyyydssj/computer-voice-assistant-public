# P7 Five-Evidence Completion Handoff (2026-09-28)

## Acceptance conclusion

This round completed the offline implementation, evidence boundaries, and regression coverage for all five evidence areas. It did **not** manufacture real-device evidence. P7 remains `Blocked / incomplete` and P8 has not started.

| Evidence area | Current result | Boundary |
|---|---|---|
| `waveOut STOP_ACKED` | WinMM diagnostic implementation and fake lifecycle tests pass; the pre-existing live record is limited backend buffer-return evidence | `waveOutReset + current WAVEHDR WOM_DONE` is not physical speaker silence and is not user-heard evidence |
| `STOP_TIMEOUT / UNKNOWN` | Controlled fault fixtures are implemented and pass; real-device fault observations remain Blocked | Fixtures prove safe state-machine behavior only and are tagged `controlled_fault_fixture` |
| WASAPI loopback + independent microphone | Bounded, serial, explicit-opt-in capture implementation is complete; default dependency-disabled path remains Blocked | All-zero active capture is invalid/None; Stereo Mix/What U Hear is excluded as an independent microphone; no live capture was run |
| `user_heard` | Only the user can set it to `True` for the exact current `batch_id` | `None`, `False`, wrong batch, or duplicate confirmation cannot complete P7 |
| Agent -> original text -> VoiceStudio -> Windows device | Explicit opt-in localhost-only runner and offline contract tests are complete; the real chain remains Blocked | Fake/mock/offline contracts never become `real_device`; defaults do not network, play, or construct an Agent |

## Implementation completed

### 1. WinMM diagnostics

Files:

- `app/tts/playback_worker.py`
- `tests/tts/test_waveout_diagnostics.py`

The diagnostic snapshot records `waveOutOpen`, `waveOutPrepareHeader`, `waveOutWrite`, `waveOutReset`, `waveOutUnprepareHeader`, and `waveOutClose` return codes and timestamps, the current header address, WOM_DONE time, and cleanup status.

The ctypes/fake-handle validity check now requires a non-null handle with `.value != 0`. The stop contract is unchanged:

- reset error -> `UNKNOWN`;
- missing or wrong-header WOM_DONE -> `STOP_TIMEOUT`;
- reset success plus WOM_DONE for the current header -> `backend_buffer_returned`.

When device state is uncertain, backend/header/buffer/callback references are retained rather than released early.

### 2. Controlled failure fixtures

Files:

- `app/tts/fault_fixtures.py`
- `tests/tts/test_fault_fixtures.py`

Covered modes: `reset_error`, `drop_callback`, `delay_callback`, and `wrong_buffer_callback`. Fixtures do not open an audio device or play real audio. After a worker failure, the worker is disabled and pending playback is rejected.

### 3. Capture

Files:

- `app/audio/windows/capture.py`
- `app/audio/windows/evidence_capture.py`
- `app/audio/windows/__init__.py`
- `tests/audio/test_windows_capture.py`
- `tests/audio/test_evidence_capture.py`

The default is `device_io_enabled=False`; `pyaudiowpatch` is imported only after explicit opt-in. Loopback and independent microphone capture is bounded and serial. All-zero active capture is invalid rather than silence evidence. System-mix endpoints are excluded. JSON summaries are retained instead of raw recordings.

### 4. Evidence ledger

Files:

- `app/evidence/ledger.py`
- `app/evidence/__init__.py`
- `tests/evidence/test_ledger.py`

The ledger keeps independent fields for `backend_stop_ack`, `loopback_output_stopped`, `acoustic_output_stopped`, `speaker_output_stopped`, and `user_heard`. Evidence-kind validation is strict. Offline contracts and controlled fixtures cannot be promoted to real P7 completion. User confirmation must match the unique batch and can be recorded only once; only positive confirmation completes the hearing requirement.

### 5. Agent chain

Files:

- `app/evidence/agent_chain.py`
- `tests/evidence/test_agent_chain.py`

Only localhost/loopback endpoints are accepted. The runner requires explicit `enabled=True`; a real-device run additionally requires `evidence_kind='real_device'` and `live_opt_in=True`. It validates request/session identity, preserves the Agent reply text, records text/audio hashes and diagnostics, and remains Blocked if any required port or contract is missing.

## Tests and checks

Executed in `<PROJECT_ROOT>`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test*.py' -q
```

Result:

```text
Ran 804 tests in 2.620s
OK (skipped=2)
```

Also passed:

- `python -m compileall -q app tests`
- `git diff --check`
- focused ledger, Agent-chain, capture, WinMM diagnostic, and fault-fixture tests

## GitHub and official-reference boundary

The implementation was checked against Microsoft WinMM API semantics, Python `ctypes` callback lifetime rules, the PyAudioWPatch WASAPI loopback example, and mature open-source waveOut lifecycle patterns. These references inform API/lifecycle design only; they do not substitute for local evidence. The implementation keeps WOM_DONE scoped to buffer-return, keeps loopback separate from air-acoustic evidence, requires a distinct microphone endpoint, and retains callback/header/buffer references after timeout/unknown.

## Real acceptance still outstanding

- `pyaudiowpatch` is not installed in the current environment; no real loopback or independent-microphone capture was executed.
- No real `STOP_TIMEOUT` or `UNKNOWN` device fault was injected.
- No real Agent -> VoiceStudio -> Windows-device binding chain was executed.
- No new real live batch was generated.
- `user_heard` was not set and was not answered on the user's behalf.
- Therefore P7 must not be declared passed.

## Safety boundary

Default tests do not network, play, record, or open devices. No new VoiceStudio POST was executed. DeepSeek, cloud providers, Edge TTS, and tools were not enabled. No commit or push was performed, and no bulk stage/reset/clean operation was used.

## Final state

```text
backend_stop_ack                 limited backend protocol evidence
STOP_TIMEOUT / UNKNOWN           controlled fixture only; real device Blocked
loopback_output_stopped          not real-tested
acoustic_output_stopped          not established
speaker_output_stopped           None / Blocked
user_heard                       None / awaiting explicit user confirmation
Agent -> exact reply -> TTS -> device  Blocked / offline contract only
P7                               Blocked / incomplete
P8                               Not entered
```

## 2026-09-29 follow-up acceptance addendum

本次复核在不播放、不录音、不联网请求 Agent 的前提下完成了实现层补强：

### Implementation follow-up

- `app/audio/windows/capture.py`
  - `AudioEndpoint` 现在保留 `host_api_index`、`native_channels`、`native_sample_rate_hz`。
  - 独立证据路径只接受 Windows WASAPI loopback + Windows WASAPI 独立麦克风；MME/DirectSound 候选不会被误选。
  - Stereo Mix / What U Hear 仍被排除。
- `app/audio/windows/evidence_capture.py`
  - 真实 PyAudioWPatch 路径按端点原生格式解析；本机枚举确认 loopback 为 2 声道/48 kHz，独立 Realtek 麦克风为 2 声道/48 kHz。
  - loopback/麦克风仍采用有界、串行、cleanup fail-closed 采集；格式缺失、打开失败、短读、全零 active、cleanup 失败均保持 Blocked。
- `app/evidence/live_runner.py`
  - 新增显式 opt-in 的单进程 Agent → 原文 → VoiceStudio → device evidence contract。
  - 默认 inert；不创建 Agent、不联网、不播放、不自动设置 `user_heard`。
  - request/session/batch identity、文本/音频上限、端口契约、无效采集和异常均 fail-closed。
- 新增/补充离线回归覆盖：native endpoint format、非 WASAPI 拒绝、默认 inert、身份错配、无效采集、用户听感不推断。

### Verification

```text
python -m unittest discover -s tests -p 'test*.py' -q
Ran 808 tests in 2.453s
OK (skipped=2)

python -m compileall -q app tests        OK
git diff --check                           OK
```

### Live preflight boundary

- `http://127.0.0.1:3900/health`：VoiceStudio 本地服务可用，返回 version `0.5.6`；这只证明服务健康，不证明 Agent 链路或设备证据。
- `http://127.0.0.1:3901/health`：无可用本地 Agent 服务。
- 未执行新的真实播放、录音、Agent 请求、故障注入或用户听感确认。

### Acceptance status after this follow-up

```text
1. backend_stop_ack                 有限通过：既有 waveOut backend buffer-return artifact
2. STOP_TIMEOUT / UNKNOWN           controlled fixture only；真实硬件故障 Blocked
3. loopback_output_stopped          未建立真实播放相关证据；Blocked
4. acoustic_output_stopped          未建立有效独立声学证据；Blocked
5. speaker_output_stopped           None / Blocked
6. user_heard                       None；未由用户针对新 batch 明确确认
7. Agent → exact reply → TTS → device  本地 Agent 端口不可用；Blocked
P7                                  Blocked / incomplete
P8                                  Not entered
```

本增补不改变既有 artifact 的事实边界，也不将离线测试、VoiceStudio 健康或端点枚举升级为 P7 真实通过。

## 2026-09-29 Implementation Hardening and Acceptance Result

Without issuing a real Agent request, playback, recording, fault injection, or user-hearing confirmation, this follow-up completed the implementation-layer hardening for the P7 evidence chain. Changes stayed inside this project:

- `app/evidence/local_ports.py`
  - Added localhost/loopback-only local Agent and VoiceStudio adapters.
  - Agent uses one submission per request, rejects duplicate `request_id`, verifies response identity, and fails closed.
  - VoiceStudio uses bounded multipart `/generate` and validates mono/16-bit/24 kHz/non-empty/non-zero WAV bytes.
  - Added explicit `DeviceCaptureFacts` injection boundary. Natural completion, `STOP_ACKED`, `STOP_TIMEOUT`, `UNKNOWN`, or quiet capture cannot be promoted to acoustic-stop evidence.
  - `LocalWindowsDeviceEvidence` is inert by default and does not create a playback worker or touch a device without live opt-in.
- `app/evidence/live_runner.py`
  - Restored text and request/session ID validation before ledger creation.
  - Preserves Agent text, request/session/batch correlation, and fail-closed error handling; it never sets `user_heard` automatically.
- `tests/evidence/test_local_ports.py` and `tests/evidence/test_live_runner.py`
  - Cover local address restrictions, HTTP wire fields, exact reply handling, response identity mismatch, duplicate request IDs, WAV boundaries, inert defaults, natural completion, timeout/unknown states, and explicit capture-fact boundaries.

### Verification

```text
python -m unittest discover -s tests -p 'test*.py' -q
Ran 808 tests in 2.352s
OK (skipped=2)

python -m compileall -q app tests
OK

git diff --check
OK
```

### Real evidence still not established

- `local-agent.v1` `POST /chat` is an adapter contract and has not been confirmed by a real `127.0.0.1:3901` Agent service. Passing adapter tests is not a real Agent end-to-end pass.
- A healthy VoiceStudio `127.0.0.1:3900/health` endpoint does not establish the Agent -> exact reply -> VoiceStudio -> Windows device chain.
- No new real playback, correlated loopback capture, valid independent-microphone capture, hardware STOP_TIMEOUT/UNKNOWN fault, or user-hearing confirmation was performed.
- Therefore `backend_stop_ack` remains limited backend buffer-return evidence. `STOP_TIMEOUT / UNKNOWN`, `loopback_output_stopped`, `acoustic_output_stopped`, `speaker_output_stopped`, real Agent end-to-end evidence, and `user_heard` must not be upgraded.

### Final state

```text
P7 = Blocked / incomplete
P8 = Not entered
user_heard = None
```

No bulk stage, reset, clean, commit, or push was performed, and no VoiceStudio installation outside the project was modified.
