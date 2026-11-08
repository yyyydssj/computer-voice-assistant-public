# P7 Evidence 3 - Independent acoustic speaker-stop handoff

- Date: 2026-09-28 (local)
- Evidence item: `speaker_output_stopped` from an independent acoustic sensor
- Result: **Blocked / None - not established**
- Scope: local-only; zero cost; no cloud, DeepSeek, or Edge TTS; no application code changed; no Git commit.

## Read-only capability survey

Windows reports a healthy Realtek High Definition Audio device and an active `Microphone Array (Realtek Audio)` input endpoint. PortAudio enumerated that microphone input separately from `Speakers (Realtek Audio)` output. This confirms OS-level endpoint availability only; it does not prove physical acoustic coupling to the speaker sound field or a usable capture path.

VoiceStudio remained local at `127.0.0.1:3900`. The test reused the existing local artifact `eff172cb.wav` (2.0 seconds); no new synthesis request was made. No competing ffplay/ffmpeg playback process was found before the bounded trial. Several VoiceStudio GUI processes were present, but the trial did not invoke their playback UI.

## Bounded controlled attempt

One attempt was made: 1 second microphone lead-in, one 2-second playback through the Realtek speaker endpoint, and 3 seconds post-observation, captured at 48 kHz stereo from the separate Realtek microphone-array endpoint. No further playback attempt was made.

The microphone capture had measurable lead-in ambient energy (RMS -51.30 dBFS), but the entire playback and post-play intervals were exactly zero-valued. This is not a valid playback-correlated acoustic trace. It cannot show either that the speakers stopped or that they did not stop. Possible causes include microphone routing/mute/DSP isolation or capture-stream failure; available evidence does not distinguish them.

The invalid raw recording was deleted immediately because it could include unintended ambient speech. Only the redacted numeric summary remains at:

`<PROJECT_ROOT>/implementation_plan/checkpoints/artifacts/2026-09-28-P7-speaker-stop-acoustic/mic-array-controlled-measurement.json`

## Evidence fields

```text
speaker_output_stopped = None (Blocked; no valid independent acoustic measurement)
user_heard = None (not assessed; no user confirmation)
backend_stop_ack = None (out of scope for this evidence item)
```

Neither `waveOut` status nor `WOM_DONE` was used as acoustic evidence. Do not promote this attempt to a pass. P7 remains blocked on this evidence item. Suggested next safe step: have the user verify microphone privacy/mute and physical speaker-to-microphone placement, then authorize a new single controlled measurement if desired.
