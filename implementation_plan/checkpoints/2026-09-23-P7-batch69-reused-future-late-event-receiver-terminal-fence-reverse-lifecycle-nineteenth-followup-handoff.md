# 2026-09-23 P7 Batch 69: reused future late-event receiver terminal-fence reverse-lifecycle nineteenth follow-up handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch68-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-eighteenth-followup-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goal and result

Repeat Batch 68's existing local lifecycle contract matrix with an independent source/receiver/future generation pair: generation-30 `voicestudio` completed source, generation-33 `edge_tts` receiver, and generation-31 `voicestudio` future request, with Provider fences 31/34. Retain three independent receiver terminal outcomes (`completed`, `failed`, `cancelled`) without changing product scope or production implementation.

The new focused test verifies, in each terminal-outcome subtest:

1. A source terminal-late `synthesis_started` ID fails without being consumed; advancing the source fence causes the same ID to fail as stale, still without consumption.
2. The receiver rejects the consumed source chunk ID as a lifecycle event ID and the consumed source `accepted` ID as its first chunk ID without changing its projections. The unconsumed source terminal-late event ID is then accepted as the receiver's first audio-chunk ID.
3. The receiver records `audio_produced`, conditionally `audio_enqueued`, then terminates as completed, failed, or cancelled. Snapshot state/reason, audio flags, Provider/generation binding, event/chunk projections, and tuple/object identity remain consistent.
4. A sequenced receiver second chunk rejected after terminal remains unconsumed. After the receiver fence advances, retrying its consumed first chunk fails the duplicate-ID gate; retrying its unconsumed second chunk fails the stale-generation gate.
5. The unconsumed second-chunk ID becomes the future generation-31 request's `accepted` lifecycle ID; trying to reuse the receiver's consumed first-chunk ID for future `synthesis_started` fails the duplicate-ID gate without mutation. All three requests' captured snapshots and projections remain isolated.

All assertions describe **in-process metadata only**, not remote Provider stop, actual audio transmission, playback completion, or user-heard audio.

## 2. Exact changes

- Changed only `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: inserted `ProviderRequestLifecycleTests.test_reused_future_late_event_receiver_terminal_fence_reverse_lifecycle_matrix_nineteenth_followup` at lines **7502–7748**, directly before Batch 68 (now starting at line **7749**). It uses independent `batch69-*` IDs and shifts the source/receiver/future generations from `(29,32,30)` to `(30,33,31)`, with fences `31/34`.
- Added this handoff at `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch69-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-nineteenth-followup-handoff.md`.
- Did not alter implementation, prior checkpoints, `implementation-roadmap.md`, `task_plan.md`, `progress.md`, P5 semantics, dependencies, or persistent configuration. No Git add, commit, or push was run; the `main` branch had no commit at the start of this batch.

## 3. Verification

Ran project-local embedded Python `<PROJECT_ROOT>\.python312\python.exe` with `-B` and stdin-fed unittest runners that add `.` to `sys.path` (no runner file written):

- Batch 68 baseline focused test: **1 passed**.
- Batch 69 focused test: **1 passed**, with three `completed` / `failed` / `cancelled` subtests.
- Provider request lifecycle: **106 passed**.
- TTS: **142 passed**.
- Full suite: **487 passed**.
- Strict UTF-8(-sig where BOM exists), AST, NUL-byte, and replacement-character audit: **133** Python files under `app` and `tests` passed. Before this handoff was written, **68** P7-named checkpoint Markdown files passed strict UTF-8/content audit. The new handoff should be included in the final post-write audit.

## 4. Contract boundaries and mandatory stop

- Registry is in-process only; no persistence, cross-process or concurrent-thread-safety guarantee. Provider-scoped generation fences keep old snapshots readable but reject stale writes.
- IDs are unique within the registry across Providers, requests, lifecycle events, and audio chunks. Rejected writes do not consume IDs; consumed IDs cannot be reused. Audio chunks represent immutable metadata, not PCM or encoded payload. Non-empty audio-chunk projection requires a `synthesis_started` event in the request's own deterministic history. The contract guarantees synchronous failure-path atomicity, not concurrency safety.
- No real Provider adapter, network request, streaming audio transport, playback queue/device/facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, voice L3, or P8 voice loop was implemented.
- **Stop here.** Future work must remain bounded and verifiable within this same local Provider request lifecycle contract. **Organize delivery by actual workload:** combine several small, related tasks into one delivery with one handoff and unified verification; handle a large or high-risk task separately. Do not add another batch merely to repeat the same matrix with incremented generations. Record the included tasks and verification in the handoff. Do not change the roadmap, task plan, progress, or P5 semantics without explicit new scope. If no meaningful bounded scope exists, stop instead of broadening to Provider or playback work.

## 5. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch69-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-nineteenth-followup-handoff.md`    不得擅自改变执行文档，无需我授权，直接按实际工作量组织下一次交付：几个相关小任务合并实施、统一验证并写一份交接文档；工作量大或风险高的任务单独实施和验证。不要仅为增加批次而重复同一代际测试；若没有有意义且符合既定边界的任务，就停止并说明。做完后在新交接文档列清完成的任务、验证结果，并重新生成这段可直接拷贝的新会话续接指令。
```