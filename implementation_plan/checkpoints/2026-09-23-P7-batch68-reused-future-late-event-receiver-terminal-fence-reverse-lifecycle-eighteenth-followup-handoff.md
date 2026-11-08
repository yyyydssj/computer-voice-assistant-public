# 2026-09-23 P7 Batch 68: reused future late-event receiver terminal-fence reverse-lifecycle eighteenth follow-up handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch67-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-seventeenth-followup-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goal and result

Repeat Batch 67's local lifecycle contract matrix at the next independent generation pair: generation-29 `voicestudio` completed source, generation-32 `edge_tts` receiver, and generation-30 `voicestudio` future request. Keep the three receiver terminal outcomes (`completed`, `failed`, `cancelled`) and do not expand product scope.

The existing `ProviderRequestLifecycleRegistry` already satisfies this matrix; **no product implementation change** was required. Added one focused test method that shifts Batch 67's `(28, 31, 29)` source/receiver/future matrix to `(29, 32, 30)` with fences `30/33`. In each independent terminal-outcome subtest:

1. A correctly sequenced source terminal-late `synthesis_started` event is rejected without consuming its ID; after advancing the source Provider fence to generation 30, the same ID is rejected as stale and remains unconsumed.
2. The receiver rejects the source's already-consumed audio-chunk ID as a `synthesis_started` ID and the source's consumed `accepted` ID as its first audio-chunk ID. The duplicate-ID rejections leave receiver projections unchanged.
3. The source's still-unconsumed terminal-late event ID becomes the receiver's valid first audio-chunk ID. The receiver records `audio_produced`, conditionally records `audio_enqueued`, then completes, fails, or cancels while preserving audio-chunk tuple/object identity, Provider/generation binding, lifecycle state/reason, and local audio flags.
4. A sequenced second receiver chunk is rejected after terminal state without ID consumption. After the receiver Provider fence advances to generation 33, the consumed first-chunk ID fails the duplicate-ID gate before stale/terminal checks, while the still-unconsumed second-chunk ID is rejected as stale without consumption.
5. The unconsumed second-chunk ID becomes the future generation-30 `voicestudio` request's `accepted` lifecycle-event ID. Reusing the receiver's consumed first-chunk ID as the future `synthesis_started` ID fails the duplicate-ID gate without mutation.
6. Source, receiver, and future snapshots and event/chunk projections remain isolated; rejected writes preserve the previously captured tuple and object identities.

This remains **local metadata only**. No terminal state, audio flag, fence rejection, or ID reuse asserts remote Provider stop, playback completion, or user-heard audio.

## 2. Exact changes

- Changed only `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: added `ProviderRequestLifecycleTests.test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_eighteenth_followup`, lines **7502–7748**, before the Batch 67 method now beginning at line **7749**. It uses three terminal-outcome subtests and shifts the source/receiver/future generations to `(29,32,30)` with fences `30/33`.
- Added this handoff at `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch68-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-eighteenth-followup-handoff.md`.
- Did not change product implementation, older checkpoints, `implementation-roadmap.md`, `task_plan.md`, `progress.md`, or P5 authorization/execution/verification/audit/recovery/retry semantics. Did not install dependencies or change persistent configuration. Did not run Git add, commit, or push; repository branch `main` still has no Git commit.

## 3. Verification

Project directory: `<PROJECT_ROOT>`. Verification used project-local embedded Python at `<PROJECT_ROOT>\.python312\python.exe` without installing dependencies. Python runners supplied via stdin inserted `.` into `sys.path` before unittest discovery, so the local `app` package resolved; no runner file was left behind. Runners used `-B` to avoid writing bytecode.

- **1 focused test passed**, including three independent `completed` / `failed` / `cancelled` subtests.
- **105 lifecycle tests passed**.
- **141 TTS tests passed**.
- **486 full-suite tests passed**.
- Python AST/strict UTF-8/content audit across **133** files under `app` and `tests` passed with **0** parse/decode failures, **0** NUL bytes, and **0** replacement characters. The three pre-existing Python BOM files were decoded using UTF-8-sig before AST parsing.
- Strict UTF-8/content audit across all **106** P7 checkpoint Markdown files passed with **0** decode failures, **0** NUL bytes, and **0** replacement characters; this handoff is UTF-8 without BOM.

During verification, the first focused runner invocation hit a PowerShell quoting error; switching to a stdin-fed Python runner resolved it. The first successful focused run then identified two stale generation assertions in the copied test (`31` and `29`), which were corrected to `32` and `30`; focused and broader suites then passed. No product-code defect was observed.

## 4. Contract boundaries and mandatory stop

- In-process registry only; no persistence, cross-process or concurrent-thread-safety guarantee. Provider-scoped generation fences leave old snapshots readable but reject old-generation writes.
- Event IDs are globally unique within one registry across Providers, requests, lifecycle events, and audio chunks. Rejected writes and rejected immutable snapshot construction do not consume IDs. A rejected unconsumed ID may later be used for a different valid request/event kind; a consumed ID cannot.
- `ProviderRequestAudioChunk` is immutable metadata without PCM or encoded payload. A non-empty audio-chunk projection requires a `synthesis_started` event in its own deterministic history. Snapshot construction and accepted writes have synchronous failure-path atomicity, not a concurrency guarantee.
- No real Provider adapter, network request, streaming audio transport, playback queue/device/facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, voice L3, or P8 voice loop was implemented.
- **Stop here.** A next batch needs a new explicit handoff, must remain minimal and verifiable within the same local Provider request lifecycle contract, and must not alter roadmap/task-plan/progress or P5 semantics without explicit new scope. If no bounded new scope exists, stop rather than broadening into Provider or playback work.

## 5. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch68-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-eighteenth-followup-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
