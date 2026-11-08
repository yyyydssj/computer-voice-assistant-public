# 2026-09-23 P7 Batch 39: cross-provider reused receiver terminal late-chunk generation-fence reuse handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch38-cross-provider-reused-receiver-terminal-late-chunk-atomicity-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 38's cross-Provider reused-receiver terminal late-chunk atomicity regression, this batch verifies the remaining generation-fence boundary without expanding the product surface:

1. Build a completed VoiceStudio source with two locally received chunks, `audio_produced`, and `audio_enqueued` under active generation 3.
2. Reject a correctly sequenced third late VoiceStudio chunk after terminal lock and confirm its ID remains unconsumed.
3. Reuse that rejected ID as the second valid chunk of an independent generation-7 `edge_tts` receiver.
4. Cover all three receiver terminal outcomes: `completed`, `failed`, and `cancelled`.
5. Reject a correctly sequenced third late chunk from each terminal receiver and confirm its ID remains unconsumed.
6. Advance the `edge_tts` fence to generation 8 and the `voicestudio` fence to generation 4.
7. Confirm the same rejected receiver late-chunk ID is still rejected as stale for the old terminal receiver and remains reusable.
8. Reuse that ID as the first valid chunk of a new generation-4 VoiceStudio request after the fence transition.
9. Confirm source, receiver, and future-request projections remain isolated by request, Provider binding, generation, chunk order, receive order, tuple identity, chunk identity, terminal event kind, reason code, and local audio flags.
10. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry.record_audio_chunk` path already performs the required checks before committing a successor snapshot or consuming the global event ID: global ID uniqueness, request/Provider binding, current generation, terminal lock, synthesizing state, chunk index, and receive sequence.

The existing generation fence is Provider-scoped. Advancing `edge_tts` to generation 8 rejects a generation-7 receiver write as stale without mutating the receiver terminal projection, while advancing `voicestudio` to generation 4 independently rejects generation-3 writes. A rejected stale write remains non-consuming, so the same event ID can be claimed by a valid generation-4 VoiceStudio request with its own request identity and first-chunk sequence.

This batch therefore required no product-code change. It adds one focused three-case regression covering:

- a completed VoiceStudio source and a cross-Provider generation-7 receiver;
- receiver `completed`, `failed`, and `cancelled` terminal projections;
- atomic terminal late-chunk rejection before any generation transition;
- Provider-scoped generation-fence advancement for both Providers;
- stale rejection after the fence transition without consuming the rejected ID;
- reuse of the rejected ID by a new generation-4 VoiceStudio request;
- preservation of the source and receiver snapshot/event/chunk projections and object identities after the future request commits.

These remain local structural/lifecycle facts only. Terminal state, audio production, audio enqueue, and generation-fence rejection do not assert remote Provider stop, playback completion, or user-heard audio.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_receiver_terminal_late_id_survives_generation_fences`.
  - Covers `completed`, `failed`, and `cancelled` receiver subcases.
  - Covers a completed VoiceStudio source with two local audio chunks and committed local audio facts.
  - Covers rejection and non-consumption of the source terminal late-chunk ID.
  - Covers cross-Provider reuse of that ID as the second `edge_tts` receiver chunk.
  - Covers receiver terminal late-chunk rejection and non-consumption of a second ID.
  - Covers independent `edge_tts` generation advancement from 7 to 8 and `voicestudio` advancement from 3 to 4.
  - Covers stale rejection of the receiver ID after the generation transition.
  - Covers reuse of that still-unconsumed ID as the first chunk of a new generation-4 VoiceStudio request.
  - Covers exact Provider/generation binding, event/chunk IDs, chunk indexes, receive sequences, immutable tuple replacement, existing chunk identities, terminal event kind, reason code, and local audio flags.
  - Extended the test-only `_prepare_generation_four_request` helper with an optional `reply_id`; existing callers retain the previous default behavior.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 38 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_receiver_terminal_late_id_survives_generation_fences -v
```

Result: **1 test passed**, including the `completed`, `failed`, and `cancelled` subcases.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **115 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **460 passed**.

### 3.4 Compile checks

```powershell
py -3.12 -m compileall -q app tests
```

Result: **passed**.

The focused test file also passed direct `py_compile` validation.

### 3.5 Encoding check for this batch

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: strict UTF-8 decode passed; no BOM; no `U+FFFD` replacement characters.
- The file is currently LF-terminated after the test-only edit; no encoding repair was performed outside this batch.
- This handoff is written as strict UTF-8 without BOM and without `U+FFFD`.

### 3.6 Git status audit

- The repository has no Git commit available for this batch; `git log` remains unavailable under the repository's current untrusted-ownership configuration unless the user explicitly changes Git safe-directory configuration.
- No `git add`, `commit`, or `push` was run.
- The working tree contains the repository's existing untracked baseline; no attempt was made to clean, stage, commit, or push it.

### 3.7 Execution-surface audit

This batch:

- did not call VoiceStudio, Edge TTS, or any Provider adapter;
- did not call the network, `synthesize`, remote `cancel`, or remote stop;
- did not connect a playback device, speaker, `sounddevice`, or real audio output;
- did not add PCM/encoded payloads, duration, playback queue, backpressure, or playback-complete semantics;
- did not implement retry, fallback, cooldown, recovery probing, AEC, full duplex, real-time interruption, continuous listening, wake word, voiceprint, or voice L3;
- did not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- did not enter the P8 voice loop;
- did not modify product implementation code;
- did not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md`;
- did not run Git add, commit, or push.

## 4. Current contract boundaries

- The public API remains an in-process memory registry with no cross-process, persistence, or concurrent-thread-safety guarantee.
- The generation fence is shared by `provider_name`; after advancement, old-generation snapshots remain readable but new old-generation events and chunks are rejected.
- Generation fences are Provider-scoped: advancing `edge_tts` does not advance or reject a current-generation VoiceStudio request, and advancing `voicestudio` does not advance or reject a current-generation edge request.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, state, and Provider constraints.
- A rejected VoiceStudio late-chunk ID may be reused by an independent generation-7 `edge_tts` receiver because rejection does not consume the global ID.
- After cross-Provider reuse, the receiver may terminate as `completed`, `failed`, or `cancelled` while preserving its own committed local audio projection.
- A correctly sequenced late chunk is rejected from each terminal receiver before snapshot or global-ID mutation.
- After both Provider fences advance, the rejected receiver late-chunk ID remains unconsumed after stale rejection and may be reused by a valid generation-4 VoiceStudio request.
- Reuse by the future request does not mutate or reinterpret either the original completed source projection or the terminal receiver projection.
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
- do not reinterpret local terminal state, audio chunks, enqueue facts, or generation-fence rejection as remote stop, playback completion, or user-heard facts;
- do not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- do not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md` without explicit scope in a future handoff;
- the next session must read this handoff first; if no new explicit scope exists, stop rather than expanding into Provider adapters or the playback path.

## 6. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch39-cross-provider-reused-receiver-terminal-late-chunk-generation-fence-reuse-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```