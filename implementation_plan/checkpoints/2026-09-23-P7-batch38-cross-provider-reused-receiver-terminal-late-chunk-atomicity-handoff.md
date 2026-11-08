# 2026-09-23 P7 Batch 38: cross-provider reused receiver terminal late-chunk atomicity handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch37-cross-provider-reused-late-chunk-receiver-terminal-isolation-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 37's receiver terminal-isolation regression, this batch closes the corresponding second late-chunk boundary after cross-Provider ID reuse without expanding the product surface:

1. Build a completed VoiceStudio source with two locally received chunks, `audio_produced`, and `audio_enqueued`.
2. Reject a correctly sequenced third late VoiceStudio chunk after terminal lock and confirm its ID remains unconsumed.
3. Reuse that rejected source ID as the second valid chunk of an independent `edge_tts` receiver.
4. Cover all three receiver terminal outcomes: `completed`, `failed`, and `cancelled`.
5. After each receiver reaches terminal state, reject its correctly sequenced third late chunk atomically.
6. Confirm the second rejected ID remains unconsumed and can be reused by a third valid VoiceStudio request.
7. Confirm source, receiver, and third-request projections remain isolated by request, Provider binding, generation, chunk order, receive order, tuple identity, chunk identity, terminal event kind, reason code, and local audio flags.
8. Confirm neither rejected write mutates the committed source or receiver projection or reinterprets local audio facts as remote stop, playback completion, or user-heard audio.
9. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry.record_audio_chunk` path already checks global ID uniqueness, current generation, request binding, and terminal lock before constructing or committing a successor snapshot and before adding the chunk ID to the global event-ID set. Therefore a late chunk rejected from a terminal receiver remains unconsumed and may be reused by a later request that satisfies the current lifecycle contract.

The existing immutable snapshot behavior also preserves the receiver's committed event tuple and chunk tuple across all three terminal outcomes. The original completed VoiceStudio source remains independent from the `edge_tts` receiver, while a third valid VoiceStudio request can claim the second rejected ID without mutating either prior projection.

This batch therefore required no product-code change. It adds one focused three-case regression covering:

- a completed receiver after `audio_produced` and `audio_enqueued`;
- a failed receiver after `audio_produced` without enqueue;
- a cancelled receiver after `audio_produced` and `audio_enqueued`;
- atomic late-chunk rejection from each terminal receiver;
- global ID non-consumption after rejection;
- reuse of the rejected receiver ID by a third valid VoiceStudio request;
- preservation of both the original completed source and terminal receiver projections after the third request commits its chunk.

### 2.2 Minimal change

Modified files:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_receiver_terminal_rejects_late_chunk_atomically_and_reuses_id`.
  - Covers the `completed`, `failed`, and `cancelled` receiver terminal matrix.
  - Covers the first rejected VoiceStudio late-chunk ID being reused by the `edge_tts` receiver.
  - Covers a second correctly sequenced late chunk being rejected from the terminal receiver without consuming its ID.
  - Covers reuse of the second rejected ID by a third valid VoiceStudio request.
  - Covers exact receiver chunk event IDs, chunk indexes, receive sequences, Provider binding, generation, tuple replacement semantics, existing chunk identities, terminal event identity, reason code, and local audio flags.
  - Covers preservation of the original completed source snapshot, event tuple, chunk tuple, event IDs, chunk IDs, and chunk identities.
  - Covers preservation of the terminal receiver snapshot, event tuple, chunk tuple, event IDs, and chunk IDs after the third request reuses the rejected ID.

New handoff:

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch38-cross-provider-reused-receiver-terminal-late-chunk-atomicity-handoff.md`.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 37 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_receiver_terminal_rejects_late_chunk_atomically_and_reuses_id -v
```

Result: **1 test passed**, including `completed`, `failed`, and `cancelled` subtests.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle tests.tts.test_provider_requests tests.tts.test_capabilities tests.tts.test_segmentation tests.tts.test_voice_profile -v
```

Result: **114 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **459 passed**.

### 3.4 Compile checks

```powershell
py -3.12 -m py_compile tests\tts\test_provider_request_lifecycle.py
py -3.12 -m compileall -q app tests
```

Result: **both passed**.

### 3.5 Encoding checks for this batch

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: strict UTF-8, no BOM, no `U+FFFD`.
- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch38-cross-provider-reused-receiver-terminal-late-chunk-atomicity-handoff.md`: strict UTF-8, no BOM, no `U+FFFD`.

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
- A rejected VoiceStudio late-chunk ID may be reused by an independent `edge_tts` receiver because rejection does not consume the global ID.
- After cross-Provider reuse, the receiver may terminate as `completed`, `failed`, or `cancelled` while preserving its own committed local audio projection.
- A correctly sequenced late chunk is rejected from each terminal receiver before snapshot or global-ID mutation.
- The rejected terminal receiver chunk ID may be reused by a third valid VoiceStudio request with its own request identity and valid first-chunk sequence.
- Reuse by the third request does not mutate or reinterpret either the original completed source projection or the terminal receiver projection.
- A failed receiver after `audio_produced` may preserve chunks and `audio_produced == True` while keeping `audio_enqueued == False`.
- A completed or cancelled receiver after `audio_enqueued` may preserve chunks with both `audio_produced == True` and `audio_enqueued == True`.
- Successful terminal transition preserves the committed audio-chunk tuple and chunk object identities; it appends only the terminal lifecycle event.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history, including for terminal snapshots.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch38-cross-provider-reused-receiver-terminal-late-chunk-atomicity-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
