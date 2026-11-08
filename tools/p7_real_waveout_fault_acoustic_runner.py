from __future__ import annotations

"""Opt-in acoustic measurement for controlled real waveOut fault batches."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audio.windows.capture import AudioEndpoint, choose_independent_endpoints, summarize_pcm16  # noqa: E402
from app.tts.playback_worker import WaveOutFaultMode, WaveOutPlaybackBackend  # noqa: E402

MODES = {"timeout": WaveOutFaultMode.TIMEOUT_AFTER_REAL_RESET, "unknown": WaveOutFaultMode.UNKNOWN_AFTER_REAL_RESET}


def derive_acoustic_stop_flags(
    summaries: dict[str, dict], errors: dict[str, str],
) -> tuple[bool, bool, bool]:
    """Return independent endpoint flags and their fail-closed conjunction."""
    loopback = summaries["loopback"]["endpoint"]
    microphone = summaries["microphone"]["endpoint"]
    loopback_stopped = (
        bool(loopback["valid"])
        and loopback["post_quiet"] is True
        and "loopback" not in errors
    )
    acoustic_stopped = (
        bool(microphone["valid"])
        and microphone["post_quiet"] is True
        and "microphone" not in errors
    )
    speaker_stopped = loopback_stopped and acoustic_stopped and not errors
    return loopback_stopped, acoustic_stopped, speaker_stopped


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--live-opt-in", action="store_true", required=True)
    p.add_argument("--fault", choices=sorted(MODES), required=True)
    p.add_argument("--audio", type=Path, default=ROOT / "implementation_plan/checkpoints/artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav")
    p.add_argument("--output-root", type=Path, default=ROOT / "implementation_plan/checkpoints/artifacts")
    return p.parse_args()


def rms_dbfs(pcm: bytes, channels: int) -> float:
    if not pcm: return -float("inf")
    vals = struct.unpack("<" + "h" * (len(pcm)//2), pcm)
    if not vals: return -float("inf")
    return 20 * math.log10(max(1e-12, math.sqrt(sum(v*v for v in vals)/len(vals))/32768.0))


def main() -> int:
    a = args()
    audio_path = a.audio.resolve()
    if not audio_path.is_file(): raise SystemExit(f"audio_missing:{audio_path}")
    wav = audio_path.read_bytes()
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    streams = []
    captured: dict[str, bytearray] = {}
    errors: dict[str, str] = {}
    try:
        endpoints = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            raw = info.get("hostApi")
            endpoints.append(AudioEndpoint(i, str(info.get("name", "")), str(raw), bool(info.get("isLoopbackDevice", False)), int(info.get("maxInputChannels", 0)), float(info.get("defaultSampleRate", 0)), raw if isinstance(raw, int) else None))
        loopback, microphone = choose_independent_endpoints(tuple(endpoints))
        capture_rate = loopback.native_sample_rate_hz
        lead_s, post_s = 0.7, 1.5
        # Capture long enough to include lead-in, active output, and post-fault quiet.
        total_s = lead_s + 3.0 + post_s
        configs = [("loopback", loopback), ("microphone", microphone)]
        for name, ep in configs:
            channels = ep.native_channels if ep.is_loopback else min(2, ep.native_channels)
            stream = pa.open(format=pyaudio.paInt16, channels=channels, rate=capture_rate, input=True, frames_per_buffer=1024, input_device_index=ep.index)
            streams.append((name, ep, channels, stream))
            captured[name] = bytearray()
        started = threading.Event()
        def reader(name, stream, channels):
            try:
                started.wait()
                deadline = time.monotonic() + total_s
                while time.monotonic() < deadline:
                    captured[name].extend(stream.read(1024, exception_on_overflow=False))
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}:{exc}"
        threads = [threading.Thread(target=reader, args=(n,s,c), daemon=True) for n,e,c,s in streams]
        for t in threads: t.start()
        started.set()
        time.sleep(lead_s)
        backend = WaveOutPlaybackBackend(fault_mode=MODES[a.fault])
        play_error = None
        result = None
        try:
            backend.start(wav)
            time.sleep(0.45)
            result = backend.stop(0.25)
        except Exception as exc:
            play_error = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            backend.close()
        for t in threads: t.join(timeout=total_s + 1)
        for name, ep, channels, stream in streams:
            try: stream.stop_stream()
            except Exception: pass
            try: stream.close()
            except Exception: pass
        diagnostic = backend.diagnostics
        summaries = {}
        for name, ep, channels, stream in streams:
            frames = len(captured[name]) // (channels * 2)
            lead = round(capture_rate * lead_s)
            active = round(capture_rate * 2.0)
            post = min(round(capture_rate * post_s), max(1, frames - lead - active))
            summary = summarize_pcm16(bytes(captured[name]), endpoint=ep, sample_rate_hz=capture_rate, channels=channels, lead_frames=lead, active_frames=active, post_frames=post, quiet_dbfs=-55.0)
            summaries[name] = {"endpoint": summary.to_dict(), "raw_audio_retained": False, "raw_bytes": len(captured[name]), "rms_dbfs": {"active": rms_dbfs(bytes(captured[name])[lead*channels*2:(lead+active)*channels*2], channels), "post": rms_dbfs(bytes(captured[name])[(lead+active)*channels*2:(lead+active+post)*channels*2], channels)}}
        loopback_stopped, acoustic_stopped, speaker_stopped = derive_acoustic_stop_flags(summaries, errors)
        out = {"batch_id": f"p7-real-waveout-{a.fault}-acoustic-20260929-{uuid.uuid4().hex[:16]}", "evidence_kind": "real_device_acoustic_fault_correlation", "fault_batch_kind": a.fault, "live_opt_in": True, "fault_backend": ({"acknowledged": result.acknowledged, "confirmation": result.confirmation, "reason": result.reason} if result else None), "backend_error": play_error, "waveout_diagnostic": {"open_result": diagnostic.open_result, "prepare_result": diagnostic.prepare_result, "write_result": diagnostic.write_result, "reset_result": diagnostic.reset_result, "wom_done_observed": diagnostic.wom_done_at is not None, "cleanup_verified": diagnostic.cleanup_verified}, "audio_path": str(audio_path), "audio_sha256": hashlib.sha256(wav).hexdigest(), "endpoint_pair": {n: {"index": e.index, "name": e.name, "host_api": e.host_api, "is_loopback": e.is_loopback, "channels": c, "sample_rate_hz": capture_rate} for n,e,c,s in streams}, "capture_config": {"lead_s": lead_s, "active_s": 2.0, "post_s": post_s, "quiet_dbfs": -55.0}, "capture_errors": errors, "loopback_summary": summaries["loopback"], "microphone_summary": summaries["microphone"], "loopback_output_stopped": loopback_stopped, "acoustic_output_stopped": acoustic_stopped, "speaker_output_stopped": speaker_stopped, "user_heard": None, "status": "established" if speaker_stopped else "blocked"}
        outdir = a.output_root.resolve() / f"2026-09-29-P7-real-waveout-fault-acoustic-{a.fault}"; outdir.mkdir(parents=True, exist_ok=True); path = outdir / f"{out['batch_id']}.json"; path.write_text(json.dumps(out, ensure_ascii=False, indent=2)+"\n", encoding="utf-8"); print(json.dumps({"batch_id":out["batch_id"],"artifact":str(path),"status":out["status"],"stop":out["fault_backend"],"loopback_output_stopped":out["loopback_output_stopped"],"acoustic_output_stopped":out["acoustic_output_stopped"],"speaker_output_stopped":out["speaker_output_stopped"]},ensure_ascii=False,indent=2)); return 0 if out["status"]=="established" else 1
    finally:
        try: pa.terminate()
        except Exception: pass

if __name__ == "__main__": raise SystemExit(main())


