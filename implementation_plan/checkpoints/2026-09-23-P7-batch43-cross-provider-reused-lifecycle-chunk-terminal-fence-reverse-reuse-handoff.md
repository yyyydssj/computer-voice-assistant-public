# 2026-09-23 P7 Batch 43: cross-provider reused lifecycle chunk terminal-fence reverse-reuse handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch42-cross-provider-reused-lifecycle-id-generation-fence-future-audio-chunk-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following Batch 42's reuse of a rejected VoiceStudio lifecycle-event ID as a generation-8 `edge_tts` audio chunk, this batch verifies the terminal and reverse cross-kind continuation boundary without expanding the product surface:

1. Reproduce the Batch 42 identity-rejection and generation-fence path under active VoiceStudio generation 3 and `edge_tts` generation 7.
2. Reuse the still-unconsumed rejected lifecycle-event ID as the first valid audio chunk of a generation-8 `edge_tts` receiver.
3. Advance that receiver through local `audio_produced`, `audio_enqueued`, and `completed` facts while preserving the reused chunk projection.
4. Reject a correctly sequenced second audio chunk after the receiver is terminal and confirm that new late-chunk ID remains unconsumed.
5. Advance the Provider-scoped fences to `edge_tts` generation 9 and VoiceStudio generation 5.
6. Confirm the already-consumed reused chunk ID is still rejected by the global duplicate-ID gate before stale-generation or terminal checks, without mutating the terminal receiver.
7. Confirm the terminal-late chunk ID is then rejected as stale for the generation-8 receiver and remains unconsumed.
8. Reuse that still-unconsumed late-chunk ID as the first valid `accepted` lifecycle event of an independent generation-5 VoiceStudio request.
9. Confirm a later attempt to reuse the already-consumed receiver chunk ID as that future request's `synthesis_started` event is rejected as a duplicate without snapshot mutation.
10. Confirm source, receiver, and future-request projections remain isolated by request, Provider binding, generation, event kind, event sequence, chunk order, receive order, tuple identity, terminal state, and local audio flags.
11. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

The existing `ProviderRequestLifecycleRegistry` already provides the required chained behavior:

- `record()` rejects the initially identity-mismatched lifecycle event and the later stale-generation rebound event before snapshot replacement or global event-ID consumption;
- `record_audio_chunk()` allows an independent current-generation receiver to claim that still-unconsumed ID as an audio chunk;
- the receiver can progress through local audio facts to `completed` without changing the chunk identity;
- a terminal-late chunk is rejected before its ID is consumed;
- after generation advancement, the global duplicate-ID check still runs before request binding, generation freshness, and terminal checks for an already-consumed ID;
- the unconsumed late-chunk ID is rejected as stale on the old receiver but remains reusable as a lifecycle event on an independent current-generation request;
- once an ID has been consumed by the receiver chunk, cross-kind reuse as a later lifecycle event is rejected without mutating the target snapshot.

These checks preserve synchronous failure-path atomicity and Provider-scoped generation isolation. The batch therefore required no product-code change.

This batch adds one focused regression that closes the round-trip chain from rejected lifecycle event to valid audio chunk, terminal late-chunk rejection, generation-fence rejection, and valid future lifecycle event. All facts remain local registry facts only. They do not assert remote Provider stop, playback completion, or user-heard audio.

### 2.2 Minimal change

Modified file:

- `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`
  - Added `test_cross_provider_reused_lifecycle_chunk_terminal_fence_and_reverse_reuse_isolated` at lines 3226-3353.
  - Covers identity-mismatched lifecycle-event rejection and old-generation stale rejection without ID consumption.
  - Covers cross-Provider reuse of that ID as a generation-8 `edge_tts` audio chunk.
  - Covers receiver progression through `audio_produced`, `audio_enqueued`, and `completed` while preserving the audio-chunk projection.
  - Covers terminal rejection and later stale-generation rejection of a second chunk without consuming its ID.
  - Covers validation priority for the already-consumed receiver chunk ID after both Provider fences advance.
  - Covers reverse cross-kind reuse of the still-unconsumed late-chunk ID as a generation-5 VoiceStudio `accepted` lifecycle event.
  - Covers duplicate rejection when the consumed receiver chunk ID is reused as the future request's `synthesis_started` lifecycle event.
  - Covers preservation of source, receiver, and future-request snapshot/event/chunk projections and object identities after each rejection.

New handoff document:

- `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch43-cross-provider-reused-lifecycle-chunk-terminal-fence-reverse-reuse-handoff.md`.

Product implementation not modified:

- `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`;
- `<PROJECT_ROOT>\app\tts\requests\__init__.py`.

Not modified:

- `<PROJECT_ROOT>\implementation_plan\implementation-roadmap.md`;
- `<PROJECT_ROOT>\task_plan.md`;
- `<PROJECT_ROOT>\progress.md`;
- Batch 42 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

The project-local Python 3.12 virtual environment was used directly from the project root. No dependency installation or environment mutation was required.

### 3.1 Baseline verification before modification

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_rejected_lifecycle_id_survives_generation_fences -v
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle -v
```

Results:

- Batch 42 focused regression: **1 test passed**.
- Lifecycle test module baseline: **82 tests passed**.

### 3.2 Batch 43 focused regression

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_reused_lifecycle_chunk_terminal_fence_and_reverse_reuse_isolated -v
```

Result: **1 test passed**.

### 3.3 Lifecycle and P7/TTS suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\tts -p 'test_*.py'
```

Results:

- Provider request lifecycle module: **83 tests passed**.
- P7/TTS discovery suite: **119 tests passed**.

### 3.4 Full unittest and compile suites

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Results:

- Full unittest suite: **464 tests passed**.
- Compile audit: **passed**.

### 3.5 Encoding audit

- All 133 Python files under `app` and `tests` passed strict UTF-8 decoding with no `U+FFFD` replacement character.
- `tests\tts\test_provider_request_lifecycle.py` has no UTF-8 BOM and no NUL byte.
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
- Reuse across event kinds does not mutate or reinterpret the original source projection, terminal receiver projection, or future request projection.
- A non-empty audio-chunk projection requires a `synthesis_started` event in the same deterministic event history; this batch's receiver satisfies that local boundary.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- Snapshot construction validates structural consistency and deterministic event-history replay; this is synchronous failure-path atomicity and does not promise thread safety.
- Local lifecycle state, audio production, audio enqueue, completion, and generation-fence rejection do not assert remote Provider stop, playback completion, or user-heard audio.
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch43-cross-provider-reused-lifecycle-chunk-terminal-fence-reverse-reuse-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
