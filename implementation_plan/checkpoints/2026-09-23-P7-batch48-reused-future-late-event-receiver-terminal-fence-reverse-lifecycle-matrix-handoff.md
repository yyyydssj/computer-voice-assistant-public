# 2026-09-23 P7 Batch 48: reused future late-event receiver terminal-fence reverse-lifecycle matrix handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch47-reused-receiver-late-chunk-future-terminal-fence-reverse-chunk-matrix-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 47's reuse of a generation-8 VoiceStudio future request's terminal-late lifecycle-event ID as the first generation-12 `edge_tts` receiver audio chunk, this batch verifies the receiver terminal matrix and the next reverse cross-kind continuation boundary without expanding the product surface:

1. Cover independent generation-12 `edge_tts` receivers ending in `completed`, `failed`, and `cancelled` states.
2. Begin each chain with a generation-9 VoiceStudio completed source whose correctly sequenced terminal-late `synthesis_started` event ID is rejected without consumption.
3. Advance the VoiceStudio fence to generation 10 and reject that same source event as stale without consuming its ID.
4. Prepare an independent generation-12 `edge_tts` receiver and confirm that reusing the already-consumed source audio-chunk ID as its `synthesis_started` event is rejected by the global duplicate-ID gate without receiver mutation.
5. Advance the receiver to `synthesizing` and confirm that reusing the already-consumed source `accepted` ID as its first audio chunk is rejected by the global duplicate-ID gate without receiver mutation.
6. Reuse the still-unconsumed source terminal-late lifecycle-event ID as the receiver's first valid audio chunk.
7. Advance the receiver through `audio_produced`; preserve terminal-specific `audio_enqueued` facts.
8. Finish the receiver separately as `completed`, `failed`, or `cancelled`, verifying terminal kind, reason code, audio flags, Provider/generation binding, tuple identity, and chunk-object identity.
9. Reject a correctly sequenced second audio chunk after each receiver is terminal and confirm its ID remains unconsumed.
10. Advance the `edge_tts` fence to generation 13.
11. Confirm the receiver's already-consumed first chunk ID is rejected by the global duplicate-ID gate before stale-generation or terminal checks, without projection mutation.
12. Reject the same terminal-late second chunk as stale on the generation-12 receiver and confirm its ID remains unconsumed.
13. Reuse the still-unconsumed receiver terminal-late chunk ID as the first valid `accepted` lifecycle event of an independent generation-10 VoiceStudio future request.
14. Confirm a later attempt to reuse the already-consumed receiver first-chunk ID as the future request's `synthesis_started` event is rejected by the global duplicate-ID gate without future projection mutation.
15. Confirm source, receiver, and future projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, terminal state, terminal reason, and local audio flags.
16. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already provides the required chained behavior across all three receiver terminal outcomes:

- `record()` rejects the generation-9 VoiceStudio source's terminal-late lifecycle event before snapshot replacement or global event-ID consumption;
- the Provider generation fence rejects the same old-generation source event again without consuming its ID;
- the already-consumed source audio-chunk ID is rejected by the global duplicate-ID gate before it can mutate the generation-12 `edge_tts` receiver;
- after the receiver reaches `synthesizing`, the already-consumed source `accepted` ID is also rejected by the global duplicate-ID gate before receiver mutation;
- `record_audio_chunk()` permits the still-unconsumed source terminal-late lifecycle-event ID to become the receiver's first valid audio chunk;
- each receiver preserves its audio-chunk tuple and chunk-object identity through its terminal transition while retaining outcome-specific reason and enqueue facts;
- a correctly sequenced second audio chunk submitted after the receiver is terminal is rejected before its ID is consumed;
- after `edge_tts` generation advancement, the already-consumed first chunk is still rejected by duplicate-ID priority, while the unconsumed terminal-late second chunk is rejected as stale and remains reusable;
- `record()` permits an independent current-generation VoiceStudio future request to claim that still-unconsumed late-chunk ID as its first `accepted` lifecycle event;
- the already-consumed receiver first-chunk ID is rejected by the global duplicate-ID gate before it can mutate the future request.

These checks preserve synchronous failure-path atomicity, terminal-outcome symmetry, global cross-kind ID uniqueness, duplicate-check priority, and Provider-scoped generation isolation. The batch therefore required no product-code change.

This batch adds one focused three-case regression that continues the Batch 47 chain: a future terminal-late lifecycle-event ID becomes a receiver audio chunk, the receiver reaches each terminal outcome, and its terminal-late audio-chunk ID becomes a later Provider's accepted lifecycle event after another generation fence. All facts remain local registry facts only. They do not assert remote Provider stop, playback completion, or user-heard audio.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix` at lines 4270-4516.
  - Covers `completed`, `failed`, and `cancelled` generation-12 `edge_tts` receivers.
  - Covers a generation-9 VoiceStudio source's terminal-late lifecycle-event ID surviving a generation-10 stale rejection.
  - Covers duplicate-ID rejection when the consumed source audio-chunk ID is submitted as the receiver's `synthesis_started` event.
  - Covers duplicate-ID rejection when the consumed source `accepted` ID is submitted as the receiver's first audio chunk.
  - Covers cross-kind reuse of the still-unconsumed source terminal-late lifecycle-event ID as the receiver's first valid audio chunk.
  - Covers receiver terminal-specific reason codes and distinct `audio_enqueued` facts.
  - Covers preservation of receiver event tuple, audio-chunk tuple, and chunk-object identity through each terminal transition.
  - Covers receiver terminal-late second-chunk rejection and later generation-13 stale rejection without ID consumption.
  - Covers duplicate-ID priority for the receiver's already-consumed first chunk after generation advancement.
  - Covers reverse cross-kind reuse of the still-unconsumed receiver terminal-late chunk ID as a generation-10 VoiceStudio future request's first valid `accepted` event.
  - Covers duplicate-ID rejection when the consumed receiver first-chunk ID is submitted as the future request's `synthesis_started` event.
  - Covers preservation of source, receiver, and future snapshot/event/chunk projections and object identities after rejected writes.

New handoff document:

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch48-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-matrix-handoff.md`.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 47 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

Python 3.12.10 from the existing project virtual environment was used. No dependency installation or environment mutation was required.

### 3.1 Baseline verification before modification

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle -q
```

Result: Provider request lifecycle baseline: **87 tests passed**.

### 3.2 Batch 48 focused regression

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix -v
```

Result: **1 test method passed**, including `completed`, `failed`, and `cancelled` subtests. The focused regression was rerun after final method naming and formatting normalization and passed again.

### 3.3 Lifecycle and P7/TTS suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle
.\.venv\Scripts\python.exe -m unittest discover -s tests\tts -p 'test_*.py'
```

Results:

- Provider request lifecycle module: **88 tests passed**.
- P7/TTS discovery suite: **124 tests passed**.

### 3.4 Full unittest and compile suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Results:

- Full unittest suite: **469 tests passed**.
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
- The configured remote remains `origin` at the existing GitHub repository; it was not contacted.
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
- Generation fences are Provider-scoped: advancing VoiceStudio does not advance `edge_tts`, and advancing `edge_tts` does not advance VoiceStudio.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes and rejected immutable snapshot construction do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, state, and Provider constraints.
- A terminal-late lifecycle-event ID remains unconsumed, remains unconsumed after stale-generation rejection, and may be reused as an independent current-generation audio chunk.
- A receiver may preserve that reused first audio chunk through `completed`, `failed`, or `cancelled` terminal state without changing its original Provider/generation binding or chunk identity.
- A receiver terminal-late audio-chunk ID remains unconsumed, remains unconsumed after stale-generation rejection, and may be reused as an independent current-generation lifecycle event of another Provider.
- Once the earlier source lifecycle-event ID is consumed as the receiver's first audio chunk, duplicate-ID rejection has priority when that ID is later submitted as a lifecycle event of another request.
- Once the source audio-chunk ID or source accepted-event ID is consumed, duplicate-ID rejection has priority when either ID is later submitted across event kinds to the receiver.
- Completed, failed, and cancelled receivers retain distinct terminal reasons and local enqueue facts; those facts do not imply remote stop or playback completion.
- Reuse across event kinds does not mutate or reinterpret the source terminal projection, receiver terminal projection, or future accepted projection.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history; the source and receiver in this batch satisfy that local boundary.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch48-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-matrix-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
