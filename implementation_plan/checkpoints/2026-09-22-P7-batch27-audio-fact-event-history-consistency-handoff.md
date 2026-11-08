# 2026-09-22 P7 Batch 27: audio fact and event-history consistency handoff

> Stage: P7 Provider request local lifecycle contract
>
> Batch status: **complete**
>
> Completion date: 2026-09-22
>
> Continuation basis: `implementation_plan/checkpoints/2026-09-22-P7-batch26-audio-enqueued-terminal-cross-provider-generation-projection-identity-handoff.md`
>
> Mandatory stop: stop after this batch; do not enter real Provider, network, playback device, real audio stream, or P8.

## 1. Batch goals

Following batch26 coverage of terminal combinations after `audio_enqueued`, cross-provider generation transitions, and projection identity, this batch closes the immutable snapshot fact/history consistency gap:

1. `audio_produced=True` must correspond to an `AUDIO_PRODUCED` event in the history.
2. `audio_enqueued=True` must correspond to an `AUDIO_ENQUEUED` event in the history.
3. Removing the corresponding event while retaining the boolean fact, or clearing the boolean fact while retaining the event, must fail closed during snapshot construction.
4. Invalid snapshot construction must not replace the registry snapshot, event projection, or audio-chunk projection, and must not consume an event ID.
5. Do not expand to real Provider, network, playback device, real audio stream, or P8.

## 2. Review conclusion and minimal change

### 2.1 Product implementation conclusion

This batch found and fixed a local snapshot-contract gap. `ProviderRequestLifecycle.__post_init__` already validated the forward prerequisites:

- `audio_produced=True` requires at least one audio chunk;
- `audio_enqueued=True` requires `audio_produced=True`;
- terminal state and event-history structure must be valid.

It did not, however, verify that the two boolean facts were backed by their corresponding events in the immutable event history. The implementation now enforces:

- `audio_produced` equals whether an `AUDIO_PRODUCED` event exists;
- `audio_enqueued` equals whether an `AUDIO_ENQUEUED` event exists.

The checks run during immutable snapshot construction. `ProviderRequestLifecycleRegistry.record(...)` still replaces the snapshot and consumes the event ID only after construction succeeds, so rejected constructions remain atomic.

### 2.2 Minimal change

Modified files:

- `app/tts/lifecycle/__init__.py`
  - Added bidirectional `audio_produced` / `audio_enqueued` event-history consistency checks in `ProviderRequestLifecycle.__post_init__` (currently around lines 283-294).
- `tests/tts/test_provider_request_lifecycle.py`
  - Added `test_snapshot_audio_facts_must_match_event_history_atomically` (currently around line 1790).

The regression covers:

1. accepted -> synthesis_started -> audio chunk -> `audio_produced`;
2. rejection when `audio_produced` is cleared;
3. rejection when the `AUDIO_PRODUCED` history event is removed;
4. progression to `audio_enqueued`;
5. rejection when `audio_enqueued` is cleared;
6. rejection when the `AUDIO_ENQUEUED` history event is removed;
7. preservation of the committed snapshot identity, facts, and history after every rejection.

Not modified:

- `app/tts/requests/__init__.py`;
- `implementation_plan/implementation-roadmap.md`;
- `task_plan.md`;
- `progress.md`;
- batch26 or earlier handoff documents;
- `.venv`, `.python312`, persistent environment configuration, or dependency state.

## 3. Acceptance results

Project directory: `<PROJECT_ROOT>`

### 3.1 Focused regression

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_snapshot_audio_facts_must_match_event_history_atomically -v
```

Result: **1 passed**.

### 3.2 P7/TTS focused suite

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

Result: **103 passed**.

### 3.3 Full unittest suite

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

Result: **448 passed**.

### 3.4 Compile and AST checks

```powershell
py -3.12 -m compileall -q app tests
```

Result: **passed**.

AST parsing passed for the lifecycle module and the focused test file. The lifecycle-module audit found no network, process, command, or file-execution calls.

### 3.5 Encoding checks for this batch

- `app/tts/lifecycle/__init__.py`: strict UTF-8, no BOM, LF line endings, no `U+FFFD`.
- `tests/tts/test_provider_request_lifecycle.py`: strict UTF-8, no BOM, CRLF line endings, no bare LF, no `U+FFFD`.
- This handoff: strict UTF-8, no BOM, CRLF line endings, no `U+FFFD`.

Existing repository-wide encoding issues were not expanded or repaired; unrelated files are outside this batch.

### 3.6 Execution-surface audit

This batch:

- did not call VoiceStudio, Edge TTS, or any Provider adapter;
- did not call the network, `synthesize`, remote `cancel`, or remote stop;
- did not connect a playback device, speaker, `sounddevice`, or real audio output;
- did not add PCM/encoded payloads, duration, playback queue, backpressure, or playback-complete semantics;
- did not implement retry, fallback, cooldown, recovery probing, AEC, full duplex, real-time interruption, continuous listening, wake word, voiceprint, or voice L3;
- did not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- did not enter the P8 voice loop;
- did not run Git add, commit, or push.

## 4. Current contract boundaries

- The public API remains an in-process memory registry with no cross-process, persistence, or concurrent-thread-safety guarantee.
- The generation fence is shared by `provider_name`; after advancement, old-generation snapshots remain readable but new old-generation events and chunks are rejected.
- Event IDs are globally unique within one lifecycle registry across Providers, requests, lifecycle events, and audio chunks.
- Rejected writes do not consume event IDs; an ID may be reused by a later write satisfying the current binding, generation, sequence, and state constraints.
- `completed`, `failed`, and `cancelled` are local lifecycle facts; they do not mean that the remote Provider stopped or that playback stopped.
- `audio_produced` and `audio_enqueued` do not mean playback started, playback completed, or the user heard audio.
- `ProviderRequestAudioChunk` remains immutable audio-chunk metadata and carries no PCM/encoded payload.
- This batch verifies synchronous failure-path atomicity and does not promise thread safety.
- The repository still has no Git commit; add, commit, and push remain unexecuted.
- Real Provider adapters, network calls, streaming audio transport, playback queue, playback facts, real-time interruption, full duplex, AEC, continuous listening, wake word, voiceprint, and voice L3 remain unimplemented.

## 5. Next-batch entry and mandatory stop

This batch is complete and stops here. Any next batch must still use a new explicit handoff document and preserve these boundaries:

- only perform minimal, verifiable work on the existing local Provider request lifecycle contract;
- do not jump to real Provider, network, playback device, real audio stream, or P8;
- do not reinterpret local terminal state, audio chunks, or enqueue facts as remote stop, playback completion, or user-heard facts;
- do not modify P5 authorization, execution, verification, audit, recovery, or retry semantics;
- do not modify `implementation-roadmap.md`, `task_plan.md`, or `progress.md` without explicit scope in a future handoff;
- the next session must read this handoff first; if no new explicit scope exists, stop rather than expanding into Provider adapters or the playback path.

## 6. Direct continuation instruction for a new session

> **原文损坏（2026-09-30 发现）**：本段原始中文已被替换为 ASCII `?`，逐字不可恢复；以下为按证据重写，紧接其后的代码块为损坏原文备审计。

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 未能复核（原文丢失）`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch27-audio-fact-event-history-consistency-handoff.md`    未能复核（原文丢失）
```

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) ????`<STUDY_ROOT>\??????\implementation_plan\checkpoints\2026-09-22-P7-batch27-audio-fact-event-history-consistency-handoff.md`    ?????????????????????????????????????????????????????????????????????????????
```
