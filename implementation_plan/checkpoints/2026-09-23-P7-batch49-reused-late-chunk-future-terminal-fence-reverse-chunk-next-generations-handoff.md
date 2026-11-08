# 2026-09-23 P7 Batch 49: reused late-chunk future terminal-fence reverse-chunk next-generations handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch48-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-matrix-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 48's reverse reuse of a generation-12 `edge_tts` receiver terminal-late audio-chunk ID as the first generation-10 VoiceStudio `accepted` lifecycle event, this batch verifies the next future-request terminal matrix and reverse cross-kind continuation boundary at the next Provider generations without expanding the product surface:

1. Cover independent generation-10 VoiceStudio future requests ending in `completed`, `failed`, and `cancelled` states.
2. Begin each chain with a generation-12 `edge_tts` terminal source whose correctly sequenced late second audio-chunk ID is rejected without consumption.
3. Advance the `edge_tts` fence to generation 13 and reject that same source chunk as stale without consuming its ID.
4. Reuse the still-unconsumed source late-chunk ID as the future request's first valid `accepted` lifecycle event.
5. Advance the future request through `synthesis_started`, its first audio chunk, and `audio_produced`; preserve terminal-specific `audio_enqueued` facts.
6. Finish the future request separately as `completed`, `failed`, or `cancelled`, verifying terminal kind, reason code, local audio flags, Provider/generation binding, event identity, tuple identity, and chunk-object identity.
7. Reject a correctly sequenced `synthesis_started` lifecycle event after each future request is terminal and confirm its ID remains unconsumed.
8. Advance the Provider-scoped fences to VoiceStudio generation 11 and `edge_tts` generation 14.
9. Reject the same terminal-late lifecycle-event ID as stale on the generation-10 future request and confirm it remains unconsumed without projection mutation.
10. Prepare an independent generation-14 `edge_tts` receiver and confirm that reusing the already-consumed future audio-chunk ID as its `synthesis_started` event is rejected by the global duplicate-ID gate without receiver mutation.
11. Advance the receiver to `synthesizing` and confirm that reusing the already-consumed future `accepted` ID as its first audio chunk is rejected by the global duplicate-ID gate without receiver mutation.
12. Reuse the still-unconsumed future terminal-late lifecycle-event ID as the receiver's first valid audio chunk.
13. Confirm source, future, and receiver projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, terminal state, terminal reason, and local audio flags.
14. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already provides the required chained behavior across all three future terminal outcomes at the next generation pair:

- `record_audio_chunk()` rejects the generation-12 `edge_tts` source's terminal-late chunk before snapshot replacement or global event-ID consumption;
- the Provider generation fence rejects the same old-generation source chunk again after advancement to generation 13 without consuming its ID;
- `record()` permits an independent current-generation VoiceStudio future request to claim that ID as its first `accepted` lifecycle event;
- each generation-10 future request preserves its accepted-event identity and audio-chunk tuple/object identity through its terminal transition while retaining outcome-specific reason and enqueue facts;
- a lifecycle event submitted after the future request is terminal is rejected before its ID is consumed;
- after VoiceStudio advancement to generation 11, the same terminal-late event is rejected as stale and remains unconsumed;
- the already-consumed future audio-chunk ID is rejected by the global duplicate-ID gate before it can mutate an independent generation-14 `edge_tts` receiver;
- after that receiver reaches `synthesizing`, the already-consumed future `accepted` ID is also rejected by the global duplicate-ID gate before receiver mutation;
- the still-unconsumed future terminal-late lifecycle-event ID may then be claimed as the receiver's first valid audio chunk;
- Provider-scoped generation fences remain isolated while all source, future, and receiver projections preserve their original object identities after rejected writes.

These checks preserve synchronous failure-path atomicity, terminal-outcome symmetry, global cross-kind ID uniqueness, duplicate-check priority, and Provider-scoped generation isolation. The batch therefore required no product-code change.

### 2.2 Files changed

Modified test file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `ProviderRequestLifecycleTests.test_reused_receiver_late_chunk_future_terminal_fence_reverse_chunk_matrix_next_generations` at lines 4517-4784.
  - Covers the `completed`, `failed`, and `cancelled` future-request terminal matrix.
  - Covers a generation-12 `edge_tts` source terminal-late audio-chunk rejection and generation-13 stale rejection without ID consumption.
  - Covers reuse of that still-unconsumed chunk ID as a generation-10 VoiceStudio future request's first `accepted` event.
  - Covers preservation of terminal-specific reason codes and distinct `audio_enqueued` facts.
  - Covers preservation of future event tuple, audio-chunk tuple, accepted-event identity, and chunk-object identity through each terminal transition.
  - Covers terminal-late lifecycle-event rejection and later generation-11 stale rejection without ID consumption.
  - Covers duplicate-ID rejection when the consumed future audio-chunk ID is submitted as the generation-14 receiver's `synthesis_started` event.
  - Covers duplicate-ID rejection when the consumed future `accepted` ID is submitted as the receiver's first audio chunk.
  - Covers reverse cross-kind reuse of the still-unconsumed future terminal-late lifecycle-event ID as the receiver's first valid audio chunk.
  - Covers preservation of source, future, and receiver snapshot/event/chunk projections and object identities after rejected writes.

New handoff document:

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch49-reused-late-chunk-future-terminal-fence-reverse-chunk-next-generations-handoff.md`.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 48 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

Python 3.12.10 from the existing project virtual environment was used. No dependency installation or environment mutation was required.

### 3.1 Baseline verification before modification

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle -q
```

Result: Provider request lifecycle baseline: **88 tests passed**.

### 3.2 Batch 49 focused regression

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_reused_receiver_late_chunk_future_terminal_fence_reverse_chunk_matrix_next_generations -v
```

Result: **1 test method passed**, including `completed`, `failed`, and `cancelled` subtests.

The first focused execution exposed three copied generation constants that had not yet been advanced with the new batch. The test fixture was corrected only within the new Batch 49 method (`edge_tts` source 12, VoiceStudio future 10, `edge_tts` receiver 14), after which the focused regression passed. No product-code defect or product-code change was involved.

### 3.3 Lifecycle and P7/TTS suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle
.\.venv\Scripts\python.exe -m unittest discover -s tests\tts -p 'test_*.py'
```

Results:

- Provider request lifecycle module: **89 tests passed**.
- P7/TTS discovery suite: **125 tests passed**.

### 3.4 Full unittest and compile suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Results:

- Full unittest suite: **470 tests passed**.
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

- did not install dependencies;
- did not make network requests;
- did not contact a real Provider;
- did not create a Provider adapter;
- did not send text to a Provider;
- did not receive or decode a real audio stream;
- did not create playback queue or playback-device behavior;
- did not add microphone capture, VAD, ASR, echo cancellation, acoustic probing, AEC, full duplex, real-time interruption, continuous listening, wake word, voiceprint, or voice L3;
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
- A terminal-late audio-chunk ID remains unconsumed, remains unconsumed after stale-generation rejection, and may be reused as an independent current-generation lifecycle event.
- A future request may preserve that reused `accepted` event through `completed`, `failed`, or `cancelled` terminal state without changing its original Provider/generation binding or event identity.
- A future terminal-late lifecycle-event ID remains unconsumed, remains unconsumed after stale-generation rejection, and may be reused as an independent current-generation audio chunk of another Provider.
- Once the earlier source late-chunk ID is consumed as the future request's `accepted` event, duplicate-ID rejection has priority when that ID is later submitted as a receiver audio chunk.
- Once the future request's audio-chunk ID is consumed, duplicate-ID rejection has priority when that ID is later submitted as a lifecycle event of another request.
- Completed, failed, and cancelled future requests retain distinct terminal reasons and local enqueue facts; those facts do not imply remote stop or playback completion.
- Reuse across event kinds does not mutate or reinterpret the source terminal projection, future terminal projection, or receiver projection.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history; each source, future request, and receiver in this batch satisfies that local boundary.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch49-reused-late-chunk-future-terminal-fence-reverse-chunk-next-generations-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```