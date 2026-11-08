# 2026-09-22 P7 Batch 34: completed multi-partial-audio late-chunk atomicity handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-22
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch33-terminal-multi-partial-audio-late-chunk-atomicity-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 33's `failed`/`cancelled` multi-partial-audio late-chunk atomicity regression, this batch closes the complementary `completed` terminal path without expanding the product surface:

1. Confirm a `completed` snapshot that committed two locally received audio chunks, `audio_produced`, and `audio_enqueued` rejects a correctly sequenced third late audio chunk after terminal lock.
2. Confirm the rejection preserves the committed completed snapshot, event projection, audio-chunk projection, and all pre-terminal event/chunk object identities.
3. Confirm the rejected late chunk does not consume its global event ID.
4. Confirm that rejected ID can be reused by another request whose generation, state, binding, chunk index, and receive sequence satisfy the current contract.
5. Confirm ID reuse on the other request does not mutate the original completed projection or reinterpret the local audio facts as remote stop, playback completion, or user-heard audio.
6. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry.record_audio_chunk` path already checks terminal lock before constructing or committing a new snapshot and before adding the chunk ID to the global event-ID set. The existing immutable snapshot path also preserves the committed event and chunk tuples when `audio_produced`, `audio_enqueued`, and `completed` events are appended.

This batch therefore required no product-code change. It adds one focused regression covering a completed request after two locally received audio chunks. The test attempts a correctly sequenced third chunk after the completed terminal lock, verifies fail-closed atomicity and projection identity, then reuses the rejected ID on a separate valid synthesizing request.

This remains a local structural/lifecycle boundary only: `completed`, `audio_produced`, and `audio_enqueued` do not assert that the remote Provider stopped, that playback completed, or that the user heard audio.

### 2.2 Minimal change

Modified files:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_completed_multi_partial_audio_rejects_late_chunk_atomically_and_reuses_id`.
  - Covers a `completed` terminal snapshot after two locally received audio chunks.
  - Covers committed `audio_produced` and `audio_enqueued` facts before completion.
  - Covers rejection of a correctly sequenced third chunk after terminal lock.
  - Covers preservation of completed snapshot identity, event/chunk tuple identity, and individual pre-terminal event/chunk object identity.
  - Covers preservation of true completed local facts and exact two-chunk order.
  - Covers reuse of the rejected chunk ID on another valid synthesizing request without mutating the original completed projections.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 33 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_completed_multi_partial_audio_rejects_late_chunk_atomically_and_reuses_id -v
```

Result: **1 test passed**.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **110 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **455 passed**.

### 3.4 Compile check

```powershell
py -3.12 -m compileall -q app tests
```

Result: **passed**.

### 3.5 Encoding checks for this batch

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.
- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch34-completed-multi-partial-audio-late-chunk-atomicity-handoff.md`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.

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
- did not modify product implementation code;
- did not run Git add, commit, or push.

## 4. Current contract boundaries

- The public API remains an in-process memory registry with no cross-process, persistence, or concurrent-thread-safety guarantee.
- The generation fence is shared by `provider_name`; after advancement, old-generation snapshots remain readable but new old-generation events and chunks are rejected.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, and state constraints.
- Terminal `completed`, `failed`, and `cancelled` snapshots reject correctly sequenced late audio chunks without changing the committed terminal snapshot or its event/chunk projections.
- Reusing a rejected late-chunk ID on another valid request does not mutate the original terminal projections.
- Successful terminal transition preserves the committed audio-chunk tuple and chunk object identities; it appends only the terminal lifecycle event.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history, including for `completed`, `failed`, and `cancelled` terminal snapshots.
- A failed/cancelled snapshot may preserve one or more partially received local audio chunks in exact local order; that projection remains distinct from `audio_produced`, `audio_enqueued`, playback, and user-heard facts.
- A completed snapshot may preserve multiple locally received chunks in exact local order and may also preserve the explicit local `audio_produced` and `audio_enqueued` facts; these facts remain distinct from playback completion and user-heard facts.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch34-completed-multi-partial-audio-late-chunk-atomicity-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```