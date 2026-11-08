# 2026-09-23 P7 Batch 52: reused future late-event receiver terminal-fence reverse-lifecycle second follow-up handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch51-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-followup-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 51's generation-12 VoiceStudio source, generation-15 `edge_tts` receiver, and generation-13 VoiceStudio future request chain, this batch repeated the same bounded local lifecycle contract matrix at the next generation pair without expanding the product surface:

1. Cover independent generation-13 VoiceStudio sources ending in the existing completed source terminal projection.
2. Begin each chain with a generation-13 VoiceStudio completed source whose correctly sequenced terminal-late `synthesis_started` event ID is rejected without consumption.
3. Advance the VoiceStudio fence to generation 14 and reject that same source event as stale without consuming its ID.
4. Prepare an independent generation-16 `edge_tts` receiver and confirm that reusing the already-consumed source audio-chunk ID as its `synthesis_started` event is rejected by the global duplicate-ID gate without receiver mutation.
5. Advance the receiver to `synthesizing` and confirm that reusing the already-consumed source `accepted` ID as its first audio chunk is rejected by the global duplicate-ID gate without receiver mutation.
6. Reuse the still-unconsumed source terminal-late lifecycle-event ID as the receiver's first valid audio chunk.
7. Advance the receiver through `audio_produced`; preserve terminal-specific `audio_enqueued` facts.
8. Finish the receiver separately as `completed`, `failed`, or `cancelled`, verifying terminal kind, reason code, local audio flags, Provider/generation binding, tuple identity, and chunk-object identity.
9. Reject a correctly sequenced second audio chunk after each receiver is terminal and confirm its ID remains unconsumed.
10. Advance the `edge_tts` fence to generation 17.
11. Confirm the receiver's already-consumed first chunk ID is rejected by the global duplicate-ID gate before stale-generation or terminal checks, without projection mutation.
12. Reject the same terminal-late second chunk as stale on the generation-16 receiver and confirm its ID remains unconsumed.
13. Reuse the still-unconsumed receiver terminal-late chunk ID as the first valid `accepted` lifecycle event of an independent generation-14 VoiceStudio future request.
14. Confirm a later attempt to reuse the already-consumed receiver first-chunk ID as the future request's `synthesis_started` event is rejected by the global duplicate-ID gate without future projection mutation.
15. Confirm source, receiver, and future projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, terminal state, terminal reason, and local audio flags.
16. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already provides the required chained behavior at this next generation pair; no product implementation change was needed:

- `record()` rejects the generation-13 VoiceStudio source's terminal-late lifecycle event before snapshot replacement or global event-ID consumption;
- the Provider generation fence rejects the same old-generation source event again after advancement to generation 14 without consuming its ID;
- the already-consumed source audio-chunk ID is rejected by the global duplicate-ID gate before it can mutate the generation-16 `edge_tts` receiver;
- after the receiver reaches `synthesizing`, the already-consumed source `accepted` ID is also rejected by the global duplicate-ID gate before receiver mutation;
- `record_audio_chunk()` permits the still-unconsumed source terminal-late lifecycle-event ID to become the receiver's first valid audio chunk;
- each receiver preserves its audio-chunk tuple and chunk-object identity through its `completed`, `failed`, or `cancelled` terminal transition while retaining outcome-specific reason and enqueue facts;
- a correctly sequenced second audio chunk submitted after the receiver is terminal is rejected before its ID is consumed;
- after `edge_tts` advancement to generation 17, the already-consumed first chunk is still rejected by duplicate-ID priority, while the unconsumed terminal-late second chunk is rejected as stale and remains reusable;
- `record()` permits an independent current-generation generation-14 VoiceStudio future request to claim that still-unconsumed late-chunk ID as its first `accepted` lifecycle event;
- the already-consumed receiver first-chunk ID is rejected when later submitted as the future request's `synthesis_started` event, without future snapshot, event tuple, chunk tuple, or global-ID mutation;
- Provider-scoped generation fences remain isolated and source, receiver, and future projections retain their original object identities after rejected writes.

### 2.2 Files changed

Modified test file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `ProviderRequestLifecycleTests.test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_second_followup` at lines 5279-5525.
  - Covers the `completed`, `failed`, and `cancelled` receiver terminal matrix at source generation 13, receiver generation 16, and future generation 14.
  - Covers generation-14 VoiceStudio and generation-17 `edge_tts` fence transitions.
  - Covers terminal-late lifecycle-event rejection, stale-generation rejection, global duplicate-ID priority, reverse cross-kind ID reuse, terminal projection identity, and cross-request isolation.

New handoff document:

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch52-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-second-followup-handoff.md`.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 51 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

Python 3.12.10 from the existing project virtual environment was used. No dependency installation or environment mutation was required.

### 3.1 Baseline verification before modification

The previous Batch 51 handoff recorded the pre-change Provider request lifecycle baseline as **91 tests passed**. This batch began from that verified state and added one test method with three terminal-outcome subtests.

### 3.2 Batch 52 focused regression

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_provider_request_lifecycle.py' -q
```

Result: **92 tests passed**.

The copied next-generation fixture initially exposed only expected generation-specific assertion drift: the new source/receiver/future bindings and the receiver chunk generation assertion were corrected within the new Batch 52 test method. The product implementation was not changed. The corrected regression passed.

### 3.3 Lifecycle and P7/TTS suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests\tts -p 'test_*.py' -q
```

Result: **128 tests passed**.

### 3.4 Full unittest and compile suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Results:

- Full unittest suite: **473 tests passed**.
- Compile audit: **passed**.

### 3.5 Encoding audit

- All 133 Python files under `app` and `tests` passed strict UTF-8 decoding with no `U+FFFD` replacement character or NUL byte.
- Three pre-existing Python files retain their existing UTF-8 BOMs: `tests\recovery\test_recovery_scanner.py`, `tests\security\test_audit_persistence.py`, and `tests\tts\test_capabilities.py`; this batch did not modify them.
- `tests\tts\test_provider_request_lifecycle.py` was written as strict UTF-8 without BOM, NUL byte, or `U+FFFD` replacement character.
- No encoding repair was performed outside this batch.

### 3.6 Git status audit

- The repository still has no Git commit; `main` has no commit history and the worktree remains under its existing untracked baseline.
- Because there is no tracked baseline, `git diff` cannot represent the batch-local edit; the exact modified test and line range are recorded above.
- No `git add`, commit, or push was run.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch52-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-second-followup-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```