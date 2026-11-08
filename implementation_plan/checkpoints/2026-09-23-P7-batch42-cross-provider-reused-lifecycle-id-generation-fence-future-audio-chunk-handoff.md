# 2026-09-23 P7 Batch 42: cross-provider reused rejected lifecycle-ID generation-fence future audio-chunk handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch41-cross-provider-reused-lifecycle-id-future-audio-chunk-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 41's cross-Provider reuse from a rejected lifecycle-event ID into a generation-7 `edge_tts` audio chunk, this batch verifies the remaining generation-fence continuation boundary without expanding the product surface:

1. Activate local generation fences for `voicestudio` generation 3 and `edge_tts` generation 7.
2. Advance a VoiceStudio source to `accepted` and capture its snapshot, event projection, and audio-chunk projection identities.
3. Construct a correctly sequenced `synthesis_started` lifecycle event whose `reply_id` is intentionally bound to another request, and reject it before snapshot replacement and global event-ID consumption.
4. Rebind the same rejected lifecycle-event ID to the source request and reject it again after both Provider generation fences advance, proving the old-generation write is stale and still non-consuming.
5. Prepare an independent generation-8 `edge_tts` receiver, reuse the still-unconsumed ID as its first valid audio chunk, and verify Provider/generation binding plus lifecycle-event projection identity.
6. Confirm that reusing the consumed receiver chunk ID as a lifecycle event for another request is rejected as a duplicate before future-request snapshot mutation.
7. Confirm source, receiver, and future-request projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, and local audio flags.
8. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` path already provides the required behavior. `record()` validates global-ID uniqueness, request identity, Provider binding, generation freshness, event sequence, and state transition before replacing the snapshot or consuming the global event ID. Therefore:

- the first identity-mismatched lifecycle event is rejected without consuming its ID;
- after the `voicestudio` fence advances from generation 3 to 4, the correctly rebound old-generation event is rejected as stale without consuming the same ID;
- the still-unconsumed ID can be claimed by a valid generation-8 `edge_tts` receiver audio chunk after the `edge_tts` fence advances from generation 7 to 8;
- once the receiver chunk commits, global event-ID uniqueness rejects reuse of that ID as a lifecycle event for another request before any future snapshot mutation.

The Provider-scoped generation fence remains isolated: advancing `edge_tts` does not mutate or reject the source's stored generation-3 snapshot until a new source write is attempted, and advancing `voicestudio` does not block the valid generation-8 receiver. The batch therefore required no product-code change.

This batch adds one focused regression for the cross-Provider, cross-event-kind, post-generation-fence reuse path. All facts remain local registry facts only. They do not assert remote Provider stop, playback completion, or user-heard audio.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Extended `_prepare_other_provider_request` at lines 1448-1467 with an optional keyword-only `provider_generation: int = 7`; existing callers retain the previous generation-7 behavior.
  - Added `test_cross_provider_reused_rejected_lifecycle_id_survives_generation_fences` at lines 3136-3225.
  - Covers identity-mismatched lifecycle-event rejection and non-consumption of the reusable ID.
  - Covers stale rejection of the correctly rebound old-generation lifecycle event after both Provider fences advance.
  - Covers cross-Provider reuse of the still-unconsumed ID as the first generation-8 `edge_tts` audio chunk.
  - Covers receiver Provider/generation binding and preservation of the receiver lifecycle-event tuple identity.
  - Covers duplicate rejection when the consumed receiver audio-chunk ID is reused as a future lifecycle event.
  - Covers preservation of source, receiver, and future-request snapshot/event/chunk projections and object identities after each rejection.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 41 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

The canonical project test commands require the system `py -3.12` launcher with the project root temporarily present in `PYTHONPATH`. The embedded `.python312\python.exe` is isolated and does not expose the project root as an import path; attempts using it failed at import discovery (`tests`/`app` not found), before test execution. No product assertion failed.

### 3.1 Focused regression

```powershell
$env:PYTHONPATH='<PROJECT_ROOT>'; Set-Location -LiteralPath '<PROJECT_ROOT>'; py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_rejected_lifecycle_id_survives_generation_fences -v
```

Result: **1 test passed**.

### 3.2 P7/TTS focused suite

```powershell
$env:PYTHONPATH='<PROJECT_ROOT>'; Set-Location -LiteralPath '<PROJECT_ROOT>'; py -3.12 -m unittest discover -s tests\tts -p 'test_*.py'
```

Result: **118 passed**.

### 3.3 Full unittest suite

```powershell
$env:PYTHONPATH='<PROJECT_ROOT>'; Set-Location -LiteralPath '<PROJECT_ROOT>'; py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **463 passed**.

### 3.4 Encoding audit

- `tests\tts\test_provider_request_lifecycle.py` passed strict UTF-8 decoding with no BOM requirement and no NUL byte.
- This handoff is written as strict UTF-8 without BOM and without `U+FFFD`.
- No encoding repair was performed outside this batch.

### 3.5 Git status audit

- The repository still has no Git commit available for this batch; the worktree remains under its existing untracked baseline.
- No `git add`, `commit`, or `push` was run.
- No attempt was made to clean, stage, commit, or push the repository's existing untracked files.

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
- did not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md`;
- did not run Git add, commit, or push.

## 4. Current contract boundaries

- The public API remains an in-process memory registry with no cross-process, persistence, or concurrent-thread-safety guarantee.
- The generation fence is shared by `provider_name`; after advancement, old-generation snapshots remain readable but new old-generation events and chunks are rejected.
- Generation fences are Provider-scoped: advancing `edge_tts` does not advance or reject a current-generation VoiceStudio request, and advancing `voicestudio` does not advance or reject a current-generation edge request.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, state, and Provider constraints.
- A rejected lifecycle event ID remains reusable after both an identity-binding rejection and a later stale-generation rejection.
- That still-unconsumed ID may be reused by an independent generation-8 `edge_tts` audio chunk after the `edge_tts` generation fence advances to 8.
- Once consumed by the receiver audio chunk, reuse as a future lifecycle event is rejected as a duplicate before snapshot mutation.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch42-cross-provider-reused-lifecycle-id-generation-fence-future-audio-chunk-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```