# 2026-09-22 P7 Batch 30: terminal audio-chunk synthesis-boundary handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-22
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch29-audio-chunk-synthesis-boundary-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following batch29's constructor guard for `prepared` and `accepted` snapshots, this batch closes the remaining terminal-state variant:

1. A `failed` or `cancelled` snapshot must not carry non-empty `audio_chunks` unless its event history contains `synthesis_started`.
2. Malformed terminal snapshot construction must fail closed without replacing the committed snapshot, event projection, or audio-chunk projection.
3. Rejected snapshot construction must not consume an event ID.
4. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

Batch29 rejected audio chunks in `prepared` and `accepted` snapshots, but the guard was state-based and therefore did not cover manually constructed `failed` or `cancelled` snapshots whose event history omitted `synthesis_started`. The immutable snapshot constructor now derives the presence of `synthesis_started` from event history and rejects any non-empty audio projection without that boundary, regardless of terminal state.

This remains a local structural/lifecycle boundary only: it does not infer Provider behavior, remote stop, playback, playback completion, or user-heard facts.

Registry writes still publish the replacement snapshot and add the global event ID only after construction succeeds. The regression confirms failure-path atomicity for both terminal variants and all public projections.

### 2.2 Minimal change

Modified files:

- `app/tts/lifecycle/__init__.py`
  - Added a constructor guard requiring a `SYNTHESIS_STARTED` event whenever `audio_chunks` is non-empty, covering terminal snapshots as well as non-terminal snapshots.
- `tests/tts/test_provider_request_lifecycle.py`
  - Added `test_terminal_snapshot_audio_chunks_require_synthesis_start_atomically` covering malformed `failed` and `cancelled` snapshots.

The regression covers:

1. failed snapshot construction;
2. cancelled snapshot construction;
3. manually grafted audio chunks onto histories without `synthesis_started`;
4. fail-closed rejection with the synthesis boundary;
5. preservation of the committed snapshot identity;
6. preservation of event and audio-chunk projection identities;
7. preservation of the global event-ID set.

Not modified:

- `app/tts/requests/__init__.py`;
- `implementation_plan/implementation-roadmap.md`;
- `task_plan.md`;
- `progress.md`;
- batch29 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Red test before the fix

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_terminal_snapshot_audio_chunks_require_synthesis_start_atomically -v
```

Result before the product change: **failed as intended** because both malformed terminal snapshots were incorrectly accepted.

### 3.2 Focused regression after the fix

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_terminal_snapshot_audio_chunks_require_synthesis_start_atomically -v
```

Result: **1 test passed; 2 subtests passed**.

### 3.3 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **106 passed**.

### 3.4 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **451 passed**.

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
- Snapshot construction validates structural consistency and deterministic event-history replay, including that audio chunks cannot exist without a `synthesis_started` event; this is synchronous failure-path atomicity and does not promise thread safety.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch30-terminal-audio-chunk-synthesis-boundary-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```