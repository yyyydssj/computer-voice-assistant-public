# 2026-09-23 P7 Batch 70: terminal reason projection handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-23
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-23-P7-batch69-reused-future-late-event-receiver-terminal-fence-reverse-lifecycle-nineteenth-followup-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goal and result

Deliver one bounded, non-repetitive contract improvement inside the existing P7 Provider request lifecycle registry: expose the terminal event's local human-readable reason through a read-only `ProviderRequestLifecycle.terminal_reason` projection.

The projection is deliberately local metadata only:

- `prepared` and other non-terminal snapshots return `None`;
- `completed` returns `None`, because completion has no failure/cancellation reason;
- `failed` and `cancelled` return the exact immutable `reason` carried by their terminal event;
- the projection does not imply Provider stop, remote failure truth, playback outcome, or user-heard audio.

The focused test covers non-terminal, failed, cancelled, and completed snapshots, verifies identity stability through the registry, and confirms the projection is the same immutable string object held by the terminal event. Existing reason-code and reason-code-kind-mismatch tests continue to pass.

## 2. Exact changes

- Changed only `<PROJECT_ROOT>\app\tts\lifecycle\__init__.py`: added the read-only `ProviderRequestLifecycle.terminal_reason` property directly after `terminal_reason_code` at lines **435–439**. It returns `terminal_event.reason` when a terminal event exists and otherwise `None`; no state transition, registry write, ID consumption, or implementation boundary changed.
- Changed only `<PROJECT_ROOT>\tests\tts\test_provider_request_lifecycle.py`: inserted `ProviderRequestLifecycleTests.test_terminal_reason_projection_is_read_only_and_identity_stable` at lines **348–388**, covering prepared/failed/cancelled/completed projections and snapshot identity.
- Added this handoff at `<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch70-terminal-reason-projection-handoff.md`.
- Did not alter `implementation-roadmap.md`, `task_plan.md`, `progress.md`, prior checkpoints, P5 semantics, dependencies, persistent configuration, Provider adapters, network code, playback code, or P8. No Git add, commit, or push was run; the repository remains an uncommitted initial `main` worktree.

## 3. Verification

Project directory: `<PROJECT_ROOT>`. Verification used project-local embedded Python at `<PROJECT_ROOT>\.python312\python.exe` with `-B`, stdin-fed unittest runners, and `.` inserted into `sys.path`; no runner file was written.

- Focused Batch 70 plus existing reason/atomicity checks: **3 passed**.
- Provider request lifecycle: **107 passed**.
- Full suite: **488 passed**.
- `compileall -q app tests`: **passed**.
- Strict UTF-8(-sig where BOM exists), AST, NUL-byte, and replacement-character audit: **133** Python files under `app` and `tests` passed with **0** failures.
- Strict UTF-8/content audit: **34** P7-named checkpoint Markdown files passed with **0** decode failures, **0** NUL bytes, and **0** replacement characters, including this handoff.

## 4. Contract boundaries and mandatory stop

- The registry remains in-process only; there is no persistence, cross-process, or concurrent-thread-safety guarantee.
- `terminal_reason` is an observation of the accepted local terminal event's reason text. It is not a remote Provider error explanation, a playback fact, an interruption acknowledgment, or proof that a user heard audio.
- IDs remain unique within one registry across Providers, requests, lifecycle events, and audio chunks. Rejected writes do not consume IDs; consumed IDs cannot be reused. Audio chunks remain immutable metadata without PCM or encoded payload.
- No real Provider adapter, network request, streaming audio transport, playback queue/device/facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, voice L3, or P8 voice loop was implemented.
- **Stop here.** Future work must remain bounded and verifiable within this same local Provider request lifecycle contract. Organize delivery by actual workload: combine several small, related tasks into one delivery with one handoff and unified verification; handle a large or high-risk task separately. Do not add another batch merely to repeat the same generation matrix. Do not change the roadmap, task plan, progress, or P5 semantics without explicit new scope. If no meaningful bounded scope exists, stop instead of broadening to Provider or playback work.

## 5. Direct continuation instruction for a new session

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-23-P7-batch70-terminal-reason-projection-handoff.md`    不得擅自改变执行文档，无需我授权，直接按实际工作量组织下一次交付：几个相关小任务合并实施、统一验证并写一份交接文档；工作量大或风险高的任务单独实施和验证。不要仅为增加批次而重复同一代际测试；若没有有意义且符合既定边界的任务，就停止并说明。做完后在新交接文档列清完成的任务、验证结果，并重新生成这段可直接拷贝的新会话续接指令。
```