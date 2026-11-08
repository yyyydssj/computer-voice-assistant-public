# 2026-09-23 P7 Batch 41: cross-provider reused rejected lifecycle-ID future audio-chunk handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch40-cross-provider-reused-receiver-late-id-future-lifecycle-event-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 40's cross-event-kind reuse from a rejected terminal receiver audio-chunk ID into a future `accepted` lifecycle event, this batch verifies the reverse **lifecycle-event rejection -> audio-chunk reuse** boundary without expanding the product surface:

1. Activate local generation fences for `voicestudio` generation 3 and `edge_tts` generation 7.
2. Advance a VoiceStudio source to `accepted`.
3. Construct a correctly sequenced `synthesis_started` lifecycle event whose `reply_id` is intentionally bound to another request, and reject it before snapshot replacement and global event-ID consumption.
4. Confirm the rejected lifecycle-event ID remains unconsumed and the source request's snapshot, event projection, and audio-chunk projection identities remain unchanged.
5. Reuse that rejected ID as the first valid audio chunk of an independent generation-7 `edge_tts` receiver.
6. Confirm the receiver chunk binds to `edge_tts` generation 7, preserves the receiver's lifecycle-event projection identity, and consumes the ID exactly once.
7. Prepare an independent future VoiceStudio request and reject an attempt to reuse the already-consumed receiver chunk ID as its first `accepted` lifecycle event.
8. Confirm duplicate rejection preserves the future request's empty snapshot/event/chunk projections and the committed global-ID set.
9. Confirm source, receiver, and future-request projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, and local audio flags.
10. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` path already provides the required behavior. `record()` validates event binding before snapshot replacement and before global event-ID consumption. A rejected lifecycle event therefore leaves its ID reusable. The same registry shares event IDs across lifecycle events and audio chunks, so an independently bound generation-7 `edge_tts` request may claim the rejected ID as its first audio chunk. Once that chunk commits, the duplicate-ID gate rejects reuse of the same ID as a lifecycle event for another request before creating a future snapshot.

This preserves synchronous failure-path atomicity and keeps source, receiver, and future request projections isolated. The batch therefore required no product-code change.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_rejected_lifecycle_id_can_become_future_audio_chunk` at lines 3056-3130.
  - Covers a correctly sequenced but identity-mismatched lifecycle event rejection.
  - Covers non-consumption of the rejected lifecycle-event ID.
  - Covers preservation of the source snapshot, event tuple, and audio-chunk tuple identities.
  - Covers cross-Provider reuse of the rejected ID as the first `edge_tts` generation-7 audio chunk.
  - Covers receiver Provider/generation binding and preservation of its lifecycle-event tuple identity.
  - Covers duplicate rejection when the consumed audio-chunk ID is reused as a future VoiceStudio `accepted` lifecycle event.
  - Covers preservation of the future request's empty projection and all prior request projections after rejection.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 40 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_rejected_lifecycle_id_can_become_future_audio_chunk -v
```

Result: **1 test passed**.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **117 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **462 passed**.

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
- A rejected lifecycle event ID may be reused by an independent generation-7 `edge_tts` audio chunk because rejection does not consume the global ID.
- Once that ID is consumed by the receiver audio chunk, reuse as a future lifecycle event is rejected as a duplicate before snapshot mutation.
- Reuse across event kinds does not mutate or reinterpret the original source projection, receiver projection, or future request projection.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history; this batch's receiver satisfies that local boundary.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- Snapshot construction validates structural consistency and deterministic event-history replay; this is synchronous failure-path atomicity and does not promise thread safety.
- Local lifecycle state, audio production, audio enqueue, and generation-fence rejection do not assert remote Provider stop, playback completion, or user-heard audio.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch41-cross-provider-reused-lifecycle-id-future-audio-chunk-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
