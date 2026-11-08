# 2026-09-23 P7 Batch 37: cross-provider reused late-chunk receiver terminal isolation handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch36-cross-provider-reused-late-chunk-receiver-completion-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 36's successful receiver-completion regression, this batch verifies the remaining local terminal outcomes after a rejected completed VoiceStudio late-chunk ID is reused by an independent `edge_tts` receiver:

1. Build a completed VoiceStudio source with two locally received chunks, `audio_produced`, and `audio_enqueued`.
2. Reject a correctly sequenced third late VoiceStudio chunk after terminal lock.
3. Confirm the rejected ID remains unconsumed and the source completed snapshot, event tuple, chunk tuple, and chunk identities remain unchanged.
4. Reuse the rejected ID as the second valid chunk of an independent `edge_tts` receiver.
5. Verify a receiving request may terminate as `failed` after `audio_produced` without claiming `audio_enqueued`.
6. Verify a receiving request may terminate as `cancelled` after both `audio_produced` and `audio_enqueued`.
7. Confirm both receiver terminal projections preserve exact chunk order, chunk index order, receive order, Provider binding, generation, existing chunk identities, terminal event identity, and reason code.
8. Confirm neither receiver terminal path mutates or reinterprets the original completed VoiceStudio projection.
9. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already supports the required behavior. A rejected terminal late-chunk write does not consume its global event ID; a later valid request may reuse that ID; `failed` and `cancelled` terminal transitions preserve the receiver's committed chunk tuple and local audio flags; and all request projections remain isolated by request and Provider binding.

This batch therefore required no product-code change. It adds a focused two-case regression for the receiver terminal matrix after cross-Provider ID reuse:

- `failed` after `audio_produced`, preserving `audio_enqueued == False`;
- `cancelled` after `audio_produced` and `audio_enqueued`, preserving `audio_enqueued == True`.

These remain local structural/lifecycle facts only. Failure, cancellation, production, and enqueue events do not assert that a remote Provider stopped, that playback started or completed, or that the user heard audio.

### 2.2 Minimal change

Modified files:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Extended `_prepare_other_provider_request` with an optional identity suffix so independent subtests can prepare distinct `edge_tts` requests without changing existing callers.
  - Added `test_cross_provider_reused_late_chunk_receiver_terminal_projection_isolated`.
  - Covers a completed VoiceStudio source with two locally received chunks.
  - Covers atomic rejection and non-consumption of a correctly sequenced third late source chunk ID.
  - Covers cross-Provider reuse of that ID as the second `edge_tts` chunk.
  - Covers receiver `failed` after `audio_produced` without enqueue.
  - Covers receiver `cancelled` after both `audio_produced` and `audio_enqueued`.
  - Covers exact receiver chunk order, chunk indexes, receive sequences, Provider/generation binding, tuple replacement semantics, existing chunk object identity, terminal state, terminal event kind, reason code, and audio flags.
  - Covers preservation of the source completed snapshot, event tuple, chunk tuple, event IDs, chunk IDs, and both source chunk identities after each receiver terminal path.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 36 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_late_chunk_receiver_terminal_projection_isolated -v
```

Result: **1 test passed**, including both `failed` and `cancelled` subtests.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **113 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **458 passed**.

### 3.4 Compile checks

```powershell
py -3.12 -m py_compile tests\tts\test_provider_request_lifecycle.py
py -3.12 -m compileall -q app tests
```

Result: **both passed**.

### 3.5 Encoding checks for this batch

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.
- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch37-cross-provider-reused-late-chunk-receiver-terminal-isolation-handoff.md`: strict UTF-8, no BOM, no `U+FFFD`, CRLF line endings.

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
- After cross-Provider reuse, the receiving request may terminate as `completed`, `failed`, or `cancelled` while preserving its own committed local audio projection.
- A failed receiver after `audio_produced` may preserve chunks and `audio_produced == True` while keeping `audio_enqueued == False`.
- A cancelled receiver after `audio_enqueued` may preserve chunks with both `audio_produced == True` and `audio_enqueued == True`.
- Cross-Provider ID reuse and either receiver terminal path do not mutate or reinterpret the original completed source projection.
- Successful terminal transition preserves the committed audio-chunk tuple and chunk object identities; it appends only the terminal lifecycle event.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history, including for `completed`, `failed`, and `cancelled` terminal snapshots.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch37-cross-provider-reused-late-chunk-receiver-terminal-isolation-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
