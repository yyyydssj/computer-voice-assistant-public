# P7 Evidence 1 — Windows waveOut STOP_ACKED handoff (2026-09-28)

## Scope and execution note

This handoff covers only the backend-scoped Windows `waveOut` stop-acknowledgement evidence. No application code or tests were changed or run in this handoff. No further playback/stop attempt is authorized or performed here.

**Important chronology:** the single bounded local playback followed by `waveOutReset` had already been executed by the immediately preceding command before the user's interruption/instruction to avoid further real stop tests. That command completed and recorded `STOP_ACKED`; this document preserves that result rather than repeating it. There were no concurrent playback commands started by this session after that instruction. The user identified parallel audio-test conflict risk; do not perform any additional device test until audio activity is confirmed serialized and separately authorized.

Bounds of that one completed operation:

- One already-saved VoiceStudio WAV; no synthesis/network request in the stop test.
- One Windows `WAVE_MAPPER`/`waveOut` submission; source audio duration 2.0 s, under the 5 s cap.
- Stop requested after the worker reached `PLAYING` (about 0.20 s later).
- Backend wait limit 1.0 s; cost 0; no cloud Provider, DeepSeek, or Edge TTS.
- No application source modifications and no Git commit.

## Read-only implementation audit

Inspected `app/tts/playback_worker.py` and existing tests. The implementation's backend-scoped acknowledgement path is coherent:

1. `PlaybackWorker.stop()` marks an active ticket `STOP_REQUESTED`, waits for its terminal result, and only returns the backend worker's terminal state.
2. `_stop_internal()` emits `STOP_ACKED` only when `StopResult.acknowledged` is true and its confirmation is not `unknown`, `timeout`, or `callable_returned`.
3. `WaveOutPlaybackBackend.stop()` calls `waveOutReset`; a nonzero result returns `unknown`. On success it waits for the callback event; no buffer return by the deadline returns `timeout`. Only reset success plus callback/buffer return yields `StopResult(True, "backend_buffer_returned", "waveOutReset")`.
4. `_on_message()` sets that event only for `WOM_DONE` on the currently submitted `WAVEHDR`.
5. The module documentation explicitly limits this fact to the backend/buffer-return protocol; it is not physical-speaker silence or evidence that a person heard anything.

Relevant code locations: `app/tts/playback_worker.py`, especially `PlaybackWorker.stop`, `_stop_internal`, `WaveOutPlaybackBackend.stop`, and `_on_message`.

The existing unit tests are not real-device proof: `tests/tts/test_playback_worker.py` uses `FakeBackend`, while `tests/tts/test_real_playback.py` primarily exercises fakes/mocks. They can verify state-machine policy and fail-closed mappings, not WinMM or hardware behavior. The pre-existing real baseline proves natural completion (`WOM_DONE`/`COMPLETED`) only and explicitly did not prove `STOP_ACKED`.

## One bounded real run — recorded outcome

Evidence record:

`implementation_plan/checkpoints/artifacts/2026-09-28-P7-stop-acked/2026-09-28-P7-waveout-stop-acked.json`

Source audio:

`implementation_plan/checkpoints/artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav`

The command output and saved record show:

- `state_before_stop = PLAYING`
- event order: `PLAYING` → `STOP_REQUESTED` → `STOP_ACKED`
- backend: `windows_waveout`
- final stop return and ticket state: `STOP_ACKED`
- `stop_ack = true`
- `reason = waveOutReset`
- `confirmation = backend_buffer_returned`
- no timeout/error; worker was not disabled
- one play submission; local existing artifact only; 0 cost

This is positive evidence for the **software/backend protocol** `STOP_ACKED` on the one observed run. The JSON is an application-level event record, not an independent WinMM trace or acoustic measurement.

## Strictly unproven

```text
backend_stop_ack       = observed true for this bounded run
speaker_output_stopped = None / not measured
user_heard             = None / not asked or independently confirmed
```

Do not promote the backend acknowledgement to physical transducer silence. No WASAPI loopback, microphone, external acoustic instrument, or other independent output measurement was performed. No user-heard claim is made.

This run does not establish reliability across devices, drivers, repeated attempts, timeout/unknown cases, or concurrency. No concurrent-test conflict assessment beyond stopping further tests was made.

## Next safe action / status

- No further live playback/stop in parallel with other audio tests.
- If additional device evidence is needed, schedule exactly one serialized test window after confirming no other audio test owns the output device; obtain explicit authorization for that window.
- Timeout/UNKNOWN hardware paths remain untested; do not induce faults without a separately reviewed safe shim and authorization.
- P7 remains `Blocked`; P8 was not entered.
