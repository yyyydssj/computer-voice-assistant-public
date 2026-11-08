# 2026-09-23 P7 Batch 44: cross-provider reused lifecycle chunk non-completed terminal matrix handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch43-cross-provider-reused-lifecycle-chunk-terminal-fence-reverse-reuse-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 43's completed-receiver round trip from a rejected lifecycle-event ID to an audio chunk and back to a future lifecycle event, this batch verifies the remaining non-completed terminal matrix without expanding the product surface:

1. Run the complete Batch 43 identity-rejection and generation-fence path separately for `failed` and `cancelled` generation-8 `edge_tts` receivers.
2. Reject an identity-mismatched VoiceStudio `synthesis_started` event and confirm its ID remains unconsumed.
3. Advance the initial Provider fences and reject the correctly rebound old-generation event as stale without consuming the ID.
4. Reuse the still-unconsumed lifecycle-event ID as the first valid audio chunk of an independent generation-8 `edge_tts` receiver.
5. Preserve distinct local fact boundaries: the failed receiver records `audio_produced` without `audio_enqueued`, while the cancelled receiver records both facts before cancellation.
6. Verify terminal kind, terminal reason code, local audio flags, Provider/generation binding, tuple identity, and chunk-object identity for both receivers.
7. Reject a correctly sequenced second audio chunk after each receiver is terminal and confirm its ID remains unconsumed.
8. Advance the Provider-scoped fences to `edge_tts` generation 9 and VoiceStudio generation 5.
9. Confirm the already-consumed reused chunk ID is rejected by the global duplicate-ID gate before stale-generation or terminal checks, without snapshot mutation.
10. Confirm each terminal-late chunk ID is rejected as stale for the generation-8 receiver and remains unconsumed.
11. Reuse each still-unconsumed late-chunk ID as the first valid `accepted` lifecycle event of an independent generation-5 VoiceStudio request.
12. Confirm a later attempt to reuse the already-consumed receiver chunk ID as the future request's `synthesis_started` event is rejected as a duplicate without projection mutation.
13. Confirm source, receiver, and future-request projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, terminal state, terminal reason, and local audio flags.
14. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already provides the required symmetric behavior for the two remaining terminal outcomes:

- `record()` rejects the initially identity-mismatched lifecycle event and the later stale-generation rebound event before snapshot replacement or global event-ID consumption;
- `record_audio_chunk()` permits the independent current-generation receiver to claim that still-unconsumed ID as an audio chunk;
- both failed and cancelled receivers preserve the consumed chunk projection and its object identity while retaining their distinct local audio facts;
- a correctly sequenced terminal-late chunk is rejected before its ID is consumed;
- after generation advancement, the global duplicate-ID gate still has priority for an already-consumed ID;
- the unconsumed terminal-late ID is rejected as stale on the old receiver but remains reusable as a lifecycle event on an independent current-generation request;
- once the original rejected lifecycle-event ID has been consumed as a receiver chunk, cross-kind reuse as a future lifecycle event is rejected without mutating the future request.

These checks preserve synchronous failure-path atomicity, terminal-outcome symmetry, and Provider-scoped generation isolation. The batch therefore required no product-code change.

This batch adds one focused two-case regression that extends the Batch 43 round-trip chain from `completed` to `failed` and `cancelled`. All facts remain local registry facts only. They do not assert remote Provider stop, playback completion, or user-heard audio.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_lifecycle_chunk_noncompleted_terminal_matrix_isolated` at lines 3355-3537.
  - Covers both `failed` and `cancelled` generation-8 `edge_tts` receivers.
  - Covers identity-mismatched lifecycle-event rejection and old-generation stale rejection without ID consumption.
  - Covers cross-Provider reuse of that ID as the receiver's first valid audio chunk.
  - Covers terminal-specific reason codes and distinct `audio_enqueued` facts.
  - Covers terminal-late chunk rejection and later stale-generation rejection without ID consumption.
  - Covers validation priority for the already-consumed receiver chunk ID after both Provider fences advance.
  - Covers reverse cross-kind reuse of the still-unconsumed late-chunk ID as a generation-5 VoiceStudio `accepted` lifecycle event.
  - Covers duplicate rejection when the consumed receiver chunk ID is reused as the future request's `synthesis_started` lifecycle event.
  - Covers preservation of source, receiver, and future-request snapshot/event/chunk projections and object identities after every rejection.

New handoff document:

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch44-cross-provider-reused-lifecycle-chunk-noncompleted-terminal-matrix-handoff.md`.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 43 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

The project-local Python 3.12 virtual environment was used directly from the project root. No dependency installation or environment mutation was required.

### 3.1 Baseline verification before modification

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_lifecycle_chunk_terminal_fence_and_reverse_reuse_isolated -v
```

Result: Batch 43 focused regression **1 test passed**.

### 3.2 Batch 44 focused regression

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_lifecycle_chunk_noncompleted_terminal_matrix_isolated -v
```

Result: **1 test method passed**, including both `failed` and `cancelled` subtests.

### 3.3 Lifecycle and P7/TTS suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\tts -p 'test_*.py'
```

Results:

- Provider request lifecycle module: **84 tests passed**.
- P7/TTS discovery suite: **120 tests passed**.

### 3.4 Full unittest and compile suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Results:

- Full unittest suite: **465 tests passed**.
- Compile audit: **passed**.

### 3.5 Encoding audit

- All 133 Python files under `app` and `tests` passed strict UTF-8 decoding with no `U+FFFD` replacement character or NUL byte.
- `tests\tts\test_provider_request_lifecycle.py` has no UTF-8 BOM, NUL byte, or `U+FFFD` replacement character.
- Three pre-existing Python files retain their existing UTF-8 BOMs: `tests\recovery\test_recovery_scanner.py`, `tests\security\test_audit_persistence.py`, and `tests\tts\test_capabilities.py`; this batch did not modify them.
- This handoff is written as strict UTF-8 without BOM; final verification also checks for NUL and `U+FFFD`.
- No encoding repair was performed outside this batch.

### 3.6 Git status audit

- The repository still has no Git commit; `main` has no commit history and the worktree remains under its existing untracked baseline.
- Because there is no tracked baseline, `git diff` cannot represent the batch-local edit; the exact modified test and line range are recorded above.
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
- Generation fences are Provider-scoped: advancing `edge_tts` does not advance VoiceStudio, and advancing VoiceStudio does not advance `edge_tts`.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, state, and Provider constraints.
- A rejected lifecycle-event ID may be reused as an independent current-generation audio chunk after identity and stale-generation rejections.
- After that audio-chunk ID is consumed, duplicate-ID rejection has priority over stale-generation, terminal-state, and target-request transition validation.
- A terminal-late audio-chunk ID remains unconsumed, remains unconsumed after a later stale-generation rejection, and may be reused as an independent current-generation lifecycle event.
- The complete reverse-reuse chain is covered for `completed`, `failed`, and `cancelled` receiver outcomes.
- Failed and cancelled terminal receivers retain distinct local facts and terminal reasons; those facts do not imply remote stop or playback completion.
- Reuse across event kinds does not mutate or reinterpret the original source projection, terminal receiver projection, or future request projection.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history; each receiver in this batch satisfies that local boundary.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- Snapshot construction validates structural consistency and deterministic event-history replay; this is synchronous failure-path atomicity and does not promise thread safety.
- Local lifecycle state, audio production, audio enqueue, completion, failure, cancellation, and generation-fence rejection do not assert remote Provider stop, playback completion, or user-heard audio.
- The repository still has no Git commit; add, commit, and push remain unexecuted.
- Real Provider adapters, network calls, streaming audio transport, playback queue, playback facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, and voice L3 remain unimplemented.

## 5. Next-batch entry and mandatory stop

This batch is complete and stops here. Any next batch must still use a new explicit handoff document and preserve these boundaries:

- only perform minimal, verifiable work on the existing local Provider request lifecycle contract;
- do not jump to real Provider, network, playback device, real audio stream, or P8;
- do not reinterpret local terminal state, audio chunks, enqueue facts, lifecycle-event reuse, duplicate-ID rejection, or generation-fence rejection as remote stop, playback completion, or user-heard facts;
- do not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- do not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md` without explicit scope in a future handoff;
- the next session must read this handoff first; if no new explicit scope exists, stop rather than expanding into Provider adapters or the playback path.

## 6. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch44-cross-provider-reused-lifecycle-chunk-noncompleted-terminal-matrix-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
