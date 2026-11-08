# 2026-09-22 P7 Batch 28: audio fact and event-history replay consistency handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-22
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch27-audio-fact-event-history-consistency-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following batch27's bidirectional fact/history consistency checks, this batch closes the remaining immutable-snapshot replay gap:

1. An event history must be replayable from `prepared` in its recorded order.
2. `AUDIO_ENQUEUED` must be rejected when replayed before `AUDIO_PRODUCED`, even if a caller manually resequences the immutable event objects.
3. Replay-derived lifecycle state, `audio_produced`, and `audio_enqueued` facts must equal the snapshot fields.
4. Invalid snapshot construction must fail closed without replacing the registry snapshot, public event projection, audio-chunk projection, or consuming an event ID.
5. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

Batch27 verified only whether the two audio fact booleans had matching event kinds somewhere in history. The existing terminal checks verified terminal placement and the final state, but the immutable constructor still allowed a caller to reorder `AUDIO_PRODUCED` and `AUDIO_ENQUEUED` while also rewriting their `event_sequence` values. That malformed history could therefore pass construction despite contradicting the local lifecycle transition rules.

The root cause was that `ProviderRequestLifecycle.__post_init__` validated structural membership and terminal shape, but did not replay the full event-kind sequence from the initial `PREPARED` state.

The implementation now performs a deterministic local replay after event-history structure and terminal-shape validation:

- `ACCEPTED` may only follow `PREPARED`;
- `SYNTHESIS_STARTED` may only follow `ACCEPTED`;
- `AUDIO_PRODUCED` may only follow `SYNTHESIZING`, requires at least one audio chunk, and may occur once;
- `AUDIO_ENQUEUED` may only follow `AUDIO_PRODUCED` and may occur once;
- `COMPLETED` may only follow `SYNTHESIZING`, requires chunks and `audio_produced`;
- `FAILED` and `CANCELLED` may only close a non-terminal local state;
- terminal histories cannot replay any later event;
- replay-derived state and audio facts must exactly equal the immutable snapshot fields.

The replay is constructor-local and side-effect free. Registry writes still replace the snapshot and consume the global event ID only after all validation and snapshot construction succeed.

### 2.2 Minimal change

Modified files:

- `app/tts/lifecycle/__init__.py`
  - Added deterministic event-history replay in `ProviderRequestLifecycle.__post_init__` (currently around lines 323-339).
  - Added `_replay_event(...)` transition helper (currently around lines 341-399).
- `tests/tts/test_provider_request_lifecycle.py`
  - Added `test_snapshot_event_history_must_replay_state_machine_in_order_atomically` (currently around line 1822).

The regression covers:

1. accepted -> synthesis_started -> audio chunk -> `audio_produced` -> `audio_enqueued`;
2. manual reordering of the two audio fact events while rewriting their sequence numbers;
3. fail-closed rejection because `audio_enqueued` appears before `audio_produced` during replay;
4. preservation of the committed snapshot object identity;
5. preservation of the event projection and audio-chunk projection identities;
6. preservation of the global event-ID set after rejection.

Not modified:

- `app/tts/requests/__init__.py`;
- `implementation_plan/implementation-roadmap.md`;
- `task_plan.md`;
- `progress.md`;
- batch27 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Red test before the fix

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_snapshot_event_history_must_replay_state_machine_in_order_atomically -v
```

Result before the product change: **failed as intended** because the reordered history was incorrectly accepted.

### 3.2 Focused regression after the fix

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_snapshot_event_history_must_replay_state_machine_in_order_atomically -v
```

Result: **1 passed**.

### 3.3 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **104 passed**.

### 3.4 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **449 passed**.

### 3.5 Compile and AST checks

```powershell
py -3.12 -m compileall -q app tests
```

Result: **passed**.

The lifecycle module and focused test file parse successfully. The lifecycle-module execution-surface audit found no network client, process launch, filesystem I/O, playback-device, or real-audio invocation.

### 3.6 Encoding checks for this batch

- `app/tts/lifecycle/__init__.py`: strict UTF-8, no BOM, LF line endings, no `U+FFFD`.
- `tests/tts/test_provider_request_lifecycle.py`: strict UTF-8, no BOM, CRLF line endings, no `U+FFFD`.
- This handoff: strict UTF-8, no BOM, CRLF line endings, no `U+FFFD`.

Existing repository-wide encoding issues were not expanded or repaired; unrelated files are outside this batch.

### 3.7 Execution-surface audit

This batch:

- did not call VoiceStudio, Edge TTS, or any Provider adapter;
- did not call the network, `synthesize`, remote `cancel`, or remote stop;
- did not connect a playback device, speaker, `sounddevice`, or real audio output;
- did not add PCM/encoded payloads, duration, playback queue, backpressure, or playback-complete semantics;
- did not implement retry, fallback, cooldown, recovery probing, AEC, full duplex, real-time interruption, continuous listening, wake word, voiceprint, or voice L3;
- did not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- did not enter the P8 voice loop;
- did not run Git add, commit, or push.

## 4. Current contract boundaries

- The public API remains an in-process memory registry with no cross-process, persistence, or concurrent-thread-safety guarantee.
- The generation fence is shared by `provider_name`; after advancement, old-generation snapshots remain readable but new old-generation events and chunks are rejected.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, and state constraints.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- Snapshot construction now validates both structural consistency and deterministic event-history replay; this is synchronous failure-path atomicity and does not promise thread safety.
- The repository still has no Git commit; add, commit, and push remain unexecuted.
- Real Provider adapters, network calls, streaming audio transport, playback queue, playback facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, and voice L3 remain unimplemented.

## 5. Next-batch entry and mandatory stop

This batch is complete and stops here. Any next batch must still use a new explicit handoff document and preserve these boundaries:

- only perform minimal, verifiable work on the existing local Provider request lifecycle contract;
- do not jump to real Provider, network, playback device, real audio stream, or P8;
- do not reinterpret local terminal state, audio chunks, or enqueue facts as remote stop, playback completion, or user-heard facts;
- do not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- do not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md` without explicit scope in a future handoff;
- the next session must read this handoff first; if no new explicit scope exists, stop rather than expanding into Provider adapters or the playback path.

## 6. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch28-audio-fact-event-history-replay-consistency-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
