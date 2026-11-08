# 2026-09-23 P7 Batch 40: cross-provider reused receiver late-ID future lifecycle-event handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch39-cross-provider-reused-receiver-terminal-late-chunk-generation-fence-reuse-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 39's cross-Provider reused receiver terminal late-chunk generation-fence reuse regression, this batch verifies the remaining **cross-event-kind reuse** boundary without expanding the product surface:

1. Build a completed VoiceStudio source with two locally received chunks, `audio_produced`, and `audio_enqueued` under active generation 3.
2. Reject a correctly sequenced third late VoiceStudio chunk and confirm its ID remains unconsumed.
3. Reuse that rejected ID as the second valid chunk of an independent generation-7 `edge_tts` receiver.
4. Cover all three receiver terminal outcomes: `completed`, `failed`, and `cancelled`.
5. Reject a correctly sequenced third late chunk from each terminal receiver and confirm its ID remains unconsumed.
6. Advance the `edge_tts` fence to generation 8 and the `voicestudio` fence to generation 4.
7. Confirm the receiver late-chunk ID is still rejected as stale for the old terminal receiver and remains reusable after both Provider fences advance.
8. Reuse that same ID as the **first lifecycle event** (`accepted`) of an independent generation-4 VoiceStudio request, proving that rejected audio-chunk IDs remain globally reusable across event kinds.
9. Confirm a later attempt to use the now-consumed ID as a future audio chunk is rejected as a duplicate without mutating the future request.
10. Confirm source, receiver, and future-request projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, chunk identity, terminal event kind, reason code, and local audio flags.
11. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` path already provides the required behavior. Rejected writes are validated before snapshot replacement and before global event-ID consumption. The Provider-scoped generation fence rejects old-generation receiver writes after advancement, while a valid generation-4 VoiceStudio request may claim the still-unconsumed ID. Global event IDs are shared across lifecycle events and audio chunks, so a rejected audio-chunk ID can be validly reused as an `accepted` lifecycle event for another request once its request identity, Provider binding, generation, and event sequence satisfy the current contract.

The duplicate check then prevents the same ID from being reused again as an audio chunk after the future lifecycle event has committed. This preserves failure-path atomicity for the future request and keeps all prior source and receiver projections unchanged.

This batch therefore required no product-code change. It adds one focused three-case regression covering:

- a completed VoiceStudio source and a cross-Provider generation-7 receiver;
- receiver `completed`, `failed`, and `cancelled` terminal projections;
- atomic terminal late-chunk rejection before any generation transition;
- Provider-scoped generation-fence advancement for both Providers;
- stale rejection after the fence transition without consuming the rejected ID;
- reuse of the rejected receiver late-chunk ID as the first `accepted` lifecycle event of a new generation-4 VoiceStudio request;
- duplicate rejection when that same ID is subsequently offered as the future request's audio chunk;
- preservation of source and receiver snapshot/event/chunk projections and object identities after the future lifecycle event commits.

These remain local structural/lifecycle facts only. Terminal state, audio production, audio enqueue, and generation-fence rejection do not assert remote Provider stop, playback completion, or user-heard audio.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_receiver_terminal_late_id_can_become_future_lifecycle_event` at lines 2880-3054.
  - Covers `completed`, `failed`, and `cancelled` receiver subcases.
  - Covers completed VoiceStudio source construction with two local audio chunks and committed local audio facts.
  - Covers rejection and non-consumption of the source terminal late-chunk ID.
  - Covers cross-Provider reuse of that ID as the second `edge_tts` receiver chunk.
  - Covers receiver terminal late-chunk rejection and non-consumption of a second ID.
  - Covers independent `edge_tts` generation advancement from 7 to 8 and `voicestudio` advancement from 3 to 4.
  - Covers stale rejection after the generation transition without consuming the receiver late-chunk ID.
  - Covers reuse of that ID as the first `accepted` event of a new generation-4 VoiceStudio request.
  - Covers exact event kind (`accepted`), event sequence (`1`), Provider binding (`voicestudio`), generation (`4`), empty initial audio-chunk projection, and event/chunk projection identities.
  - Covers duplicate rejection when the consumed lifecycle-event ID is reused as the future request's audio chunk.
  - Covers preservation of source and receiver snapshot/event/chunk identities, chunk identities, event IDs, chunk IDs, terminal kind, reason code, and local audio flags.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 39 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_receiver_terminal_late_id_can_become_future_lifecycle_event -v
```

Result: **1 test passed**, including the `completed`, `failed`, and `cancelled` subcases.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **116 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **461 passed**.

### 3.4 Compile checks

```powershell
py -3.12 -m compileall -q app tests
py -3.12 -m py_compile tests/tts/test_provider_request_lifecycle.py
```

Result: **passed**.

### 3.5 Encoding check for this batch

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: strict UTF-8 decode passed; no BOM; no `U+FFFD` replacement characters; LF-only line endings.
- This handoff is written as strict UTF-8 without BOM and without `U+FFFD`.
- No encoding repair was performed outside this batch.

### 3.6 Git status audit

- The repository still has no Git commit available for this batch; the worktree remains under its existing untracked baseline.
- No `git add`, `commit`, or `push` was run.
- No attempt was made to clean, stage, commit, or push the repository's existing untracked files.

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
- After both Provider fences advance, the rejected receiver late-chunk ID remains unconsumed after stale rejection and may be reused as the first `accepted` lifecycle event of a valid generation-4 VoiceStudio request.
- Global ID uniqueness applies across event kinds: once the future request consumes the reused ID as an `accepted` event, offering the same ID as an audio chunk is rejected as a duplicate before snapshot or audio-projection mutation.
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
- do not reinterpret local terminal state, audio chunks, enqueue facts, lifecycle-event reuse, or generation-fence rejection as remote stop, playback completion, or user-heard facts;
- do not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- do not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md` without explicit scope in a future handoff;
- the next session must read this handoff first; if no new explicit scope exists, stop rather than expanding into Provider adapters or the playback path.

## 6. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch40-cross-provider-reused-receiver-late-id-future-lifecycle-event-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```