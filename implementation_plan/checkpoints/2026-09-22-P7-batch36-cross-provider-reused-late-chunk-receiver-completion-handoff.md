# 2026-09-22 P7 Batch 36: cross-provider reused late-chunk receiver completion handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-22
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch35-cross-provider-completed-late-chunk-isolation-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 35's cross-Provider reuse isolation regression, this batch verifies that the valid receiving `edge_tts` request can continue through its complete local audio-fact chain after reusing the rejected VoiceStudio late-chunk ID, while the original completed VoiceStudio projection remains immutable and isolated:

1. Build a completed VoiceStudio snapshot with two locally received chunks, `audio_produced`, and `audio_enqueued`.
2. Reject a correctly sequenced third late VoiceStudio chunk after terminal lock.
3. Confirm the rejected ID remains unconsumed and the completed VoiceStudio snapshot, event tuple, chunk tuple, and pre-terminal object identities remain unchanged.
4. Reuse the rejected ID as the second valid chunk of an independent `edge_tts` request with its own Provider binding and generation.
5. Continue that `edge_tts` request through `audio_produced`, `audio_enqueued`, and `completed`.
6. Confirm the receiving request preserves exact chunk order, chunk index order, receive order, existing chunk identity, and terminal event identity.
7. Confirm cross-Provider ID reuse does not mutate or reinterpret the original VoiceStudio completion projection.
8. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already supports the required behavior: rejected terminal late-chunk writes do not consume their event IDs; successful writes create immutable successor snapshots; event/chunk metadata remains bound to its own request and Provider; and terminal completion preserves the committed local audio projection.

This batch therefore required no product-code change. It adds one focused regression that continues the valid cross-Provider receiver after reusing the rejected VoiceStudio late-chunk ID. The regression verifies both sides of the boundary: the `edge_tts` receiver completes its own local lifecycle without rebuilding prior chunk objects, while the original completed VoiceStudio request remains unchanged.

This remains a local structural/lifecycle boundary only: `completed`, `audio_produced`, and `audio_enqueued` do not assert that either remote Provider stopped, that playback completed, or that the user heard audio.

### 2.2 Minimal change

Modified files:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_late_chunk_can_finish_receiver_without_mutating_completed_source`.
  - Covers a completed VoiceStudio source after two locally received audio chunks.
  - Covers rejection of a correctly sequenced third late chunk after terminal lock.
  - Covers rejection atomicity and non-consumption of the reusable global event ID.
  - Covers reuse of that ID as the second `edge_tts` chunk.
  - Covers the receiving request's `audio_produced`, `audio_enqueued`, and `completed` progression.
  - Covers exact receiver chunk order, chunk indexes, receive sequences, Provider binding, generation, tuple replacement semantics, and preservation of the first chunk object's identity.
  - Covers preservation of the source completed snapshot, event tuple, chunk tuple, and both pre-terminal chunk object identities after the receiver completes.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 35 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_late_chunk_can_finish_receiver_without_mutating_completed_source -v
```

Result: **1 test passed**.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **112 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **457 passed**.

### 3.4 Compile checks

```powershell
py -3.12 -m py_compile tests\tts\test_provider_request_lifecycle.py
py -3.12 -m compileall -q app tests
```

Result: **both passed**.

### 3.5 Encoding checks for this batch

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.
- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch36-cross-provider-reused-late-chunk-receiver-completion-handoff.md`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.

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
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, state, and Provider constraints.
- Terminal `completed`, `failed`, and `cancelled` snapshots reject correctly sequenced late audio chunks without changing the committed terminal snapshot or its event/chunk projections.
- A rejected VoiceStudio late-chunk ID may be reused by a valid `edge_tts` request because rejection does not consume the global ID.
- After that cross-Provider reuse, the receiving `edge_tts` request may continue through a second chunk, `audio_produced`, `audio_enqueued`, and `completed` while preserving exact local chunk order and existing chunk object identity.
- Reusing a rejected late-chunk ID across Providers does not mutate or reinterpret the original terminal projection.
- Successful terminal transition preserves the committed audio-chunk tuple and chunk object identities; it appends only the terminal lifecycle event.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history, including for `completed`, `failed`, and `cancelled` terminal snapshots.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch36-cross-provider-reused-late-chunk-receiver-completion-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```