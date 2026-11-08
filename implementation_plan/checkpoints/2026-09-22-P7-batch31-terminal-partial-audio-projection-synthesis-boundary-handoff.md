# 2026-09-22 P7 Batch 31: terminal partial-audio projection synthesis-boundary handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-22
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch30-terminal-audio-chunk-synthesis-boundary-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following batch30's terminal-state synthesis-boundary guard, this batch hardens the remaining regression matrix without expanding the product surface:

1. Confirm a valid `failed` snapshot with partially received audio chunks preserves the `synthesis_started` event boundary and the exact local audio-chunk projection.
2. Confirm a valid `cancelled` snapshot with partially received audio chunks has the same local projection semantics.
3. Confirm manually removing `synthesis_started` from either terminal history while retaining audio chunks is rejected fail-closed.
4. Confirm rejected terminal snapshot construction preserves the committed snapshot identity, event projection identity, audio-chunk projection identity, and global event-ID set.
5. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing immutable `ProviderRequestLifecycle` constructor already enforces that a non-empty `audio_chunks` projection requires a `synthesis_started` event in the event history, including `failed` and `cancelled` terminal snapshots. The registry also publishes a new snapshot and global event ID only after construction succeeds.

This batch therefore required no product-code change. It adds a focused regression that exercises both terminal variants after partial local audio reception, then attempts to graft the same audio projection onto a terminal history with the synthesis boundary removed. The test confirms that the current contract remains fail-closed and atomic.

This remains a local structural/lifecycle boundary only: it does not infer Provider behavior, remote stop, playback, playback completion, or user-heard facts.

### 2.2 Minimal change

Modified files:

- `tests/tts/test_provider_request_lifecycle.py`
  - Added `test_terminal_partial_audio_projection_replays_synthesis_boundary_atomically`.
  - Covers `failed` and `cancelled` terminal snapshots after one locally received audio chunk.
  - Covers rejection when `synthesis_started` is removed from the manually supplied terminal history while the chunk projection remains non-empty.
  - Covers preservation of snapshot, events, chunks, and global event-ID identities after rejection.

Not modified:

- `app/tts/lifecycle/__init__.py`;
- `app/tts/requests/__init__.py`;
- `implementation_plan/implementation-roadmap.md`;
- `task_plan.md`;
- `progress.md`;
- batch30 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_terminal_partial_audio_projection_replays_synthesis_boundary_atomically -v
```

Result: **1 test passed**.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **107 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **452 passed**.

### 3.4 Compile check

```powershell
py -3.12 -m compileall -q app tests
```

Result: **passed**.

### 3.5 Encoding checks for this batch

- `app/tts/lifecycle/__init__.py`: strict UTF-8, no BOM, no `U+FFFD`, LF line endings.
- `tests/tts/test_provider_request_lifecycle.py`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.
- This handoff: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.

Existing repository-wide encoding issues were not expanded or repaired; unrelated files are outside this batch.

### 3.6 Execution-surface audit

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
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, and state constraints.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history, including for `failed` and `cancelled` terminal snapshots.
- A failed/cancelled snapshot may preserve partially received local audio chunks; that projection remains distinct from `audio_produced`, `audio_enqueued`, playback, and user-heard facts.
- Snapshot construction validates structural consistency and deterministic event-history replay; this is synchronous failure-path atomicity and does not promise thread safety.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch31-terminal-partial-audio-projection-synthesis-boundary-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```