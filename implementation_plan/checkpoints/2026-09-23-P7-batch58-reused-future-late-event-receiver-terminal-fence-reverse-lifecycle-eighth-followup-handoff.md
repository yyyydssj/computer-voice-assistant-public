# 2026-09-23 P7 Batch 58: reused future late-event receiver terminal-fence reverse-lifecycle eighth follow-up handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch57-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-seventh-followup-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goal and result

Repeat the existing Batch 57 local lifecycle contract matrix at the next independent generation pair: generation-19 `voicestudio` completed source, generation-22 `edge_tts` receiver, and generation-20 `voicestudio` future request. Keep the three receiver terminal outcomes (`completed`, `failed`, `cancelled`) and do not expand the product surface.

The existing `ProviderRequestLifecycleRegistry` already satisfies the matrix; **no product implementation change** was required. Added one focused test method that shifts Batch 57's `(18, 21, 19)` source/receiver/future matrix to `(19, 22, 20)` with fences `20/23`. Each independent outcome subtest verifies:

1. The source's correctly sequenced terminal-late `synthesis_started` event is rejected before consuming its ID; after the `voicestudio` fence advances to generation 20, the same ID is rejected as stale and remains unconsumed.
2. A consumed source audio-chunk ID is rejected as the receiver's `synthesis_started` event, and a consumed source `accepted` ID is rejected as its first audio chunk. Both are global duplicate-ID rejections without receiver mutation.
3. The still-unconsumed source terminal-late event ID becomes the receiver's valid first audio-chunk ID. The receiver records `audio_produced`, optionally records `audio_enqueued` according to the terminal outcome, and completes, fails, or cancels while preserving its original audio-chunk tuple and chunk-object identity, Provider/generation binding, state, reason, and local audio flags.
4. A correctly sequenced second chunk is rejected after receiver terminal state without ID consumption. After `edge_tts` advances to generation 23, the already-consumed first chunk ID fails the duplicate-ID gate before stale/terminal checks, whereas the unconsumed second chunk ID is rejected as stale without consumption.
5. The second chunk's still-unconsumed ID becomes the future generation-20 `voicestudio` request's `accepted` lifecycle-event ID. Reusing the receiver's consumed first-chunk ID as the future `synthesis_started` event fails the duplicate-ID gate without mutation.
6. Source, receiver, and future projections remain isolated with preserved snapshot, event-tuple, and chunk-tuple identities after rejected writes.

This remains **local metadata only**. Terminal state, audio production, enqueue, generation-fence rejection, and cross-kind event-ID reuse do not imply remote Provider stop, playback completion, or user-heard audio.

## 2. Exact changes

- Changed only `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: added `ProviderRequestLifecycleTests.test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_eighth_followup`, lines **6267–6513**, before `test_snapshot_event_history_must_replay_state_machine_in_order_atomically` at line **6514**. It contains three terminal-outcome subtests and shifts the Batch 57 `(18,21,19)` source/receiver/future matrix to `(19,22,20)` with fences `20/23`.
- Added this handoff at `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch58-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-eighth-followup-handoff.md`.
- Did not modify product implementation, the Batch 57 or earlier handoffs, `implementation-roadmap.md`, `task_plan.md`, `progress.md`, or P5 authorization/execution/verification/audit/recovery/retry semantics. Did not install dependencies or change persistent configuration. Did not run Git add, commit, or push; the repository still has no Git commit.

## 3. Verification

Project directory: `<PROJECT_ROOT>`. Existing Python 3.12 virtual environment; no dependency installation.

```powershell
Set-Location -LiteralPath '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_eighth_followup
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_provider_request_lifecycle.py' -q
.\.venv\Scripts\python.exe -m unittest discover -s tests\tts -p 'test_*.py' -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Results: **1 focused test passed** (three subtests); **96 lifecycle tests passed**; **132 TTS tests passed**; **477 full-suite tests passed**; compile audit passed. Strict UTF-8 audit across 228 Python/P7 checkpoint files: **0** decode failures, **0** NUL bytes, **0** replacement characters. The new handoff is UTF-8 without BOM and is included in the audit.

No product-code defect was observed. The existing registry implementation satisfies the next generation pair without modification.

## 4. Contract boundaries and mandatory stop

- In-process memory registry only; no persistence, cross-process or concurrent-thread-safety guarantee. Provider-scoped generation fences leave old snapshots readable but reject old-generation writes.
- Event IDs are globally unique within one registry across Providers, requests, lifecycle events, and audio chunks. Rejected writes and rejected immutable snapshot construction do not consume IDs. A still-unconsumed rejected ID may later be used for a different valid request/event kind; a consumed ID cannot.
- `ProviderRequestAudioChunk` is immutable metadata with no PCM/encoded payload. A non-empty audio-chunk projection requires a `synthesis_started` event in its own deterministic history. Snapshot construction and accepted writes are synchronous failure-path atomicity, not a concurrency guarantee.
- No real Provider adapter, network request, streaming audio transport, playback queue/device/facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, voice L3, or P8 voice loop was implemented.
- **Stop here.** A next batch needs a new explicit handoff, must stay with minimal verifiable work on the same local Provider request lifecycle contract, and must not change roadmap/task-plan/progress or P5 semantics without explicit new scope. If no new bounded scope exists, stop rather than broadening into Provider or playback work.

## 5. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch58-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-eighth-followup-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```