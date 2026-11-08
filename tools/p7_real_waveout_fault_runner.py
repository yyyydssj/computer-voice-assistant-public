from __future__ import annotations

"""Explicit opt-in real waveOut fault evidence runner."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.tts.playback_worker import WaveOutFaultMode, WaveOutPlaybackBackend  # noqa: E402

MODES = {
    "timeout": WaveOutFaultMode.TIMEOUT_AFTER_REAL_RESET,
    "unknown": WaveOutFaultMode.UNKNOWN_AFTER_REAL_RESET,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-opt-in", action="store_true",
                        help="required acknowledgement that a real speaker playback will occur")
    parser.add_argument("--fault", choices=sorted(MODES), required=True)
    parser.add_argument("--audio", type=Path, default=ROOT / "implementation_plan/checkpoints/artifacts/2026-09-28-P7-local-voicestudio/eff172cb.wav")
    parser.add_argument("--stop-timeout", type=float, default=0.25)
    parser.add_argument("--output-root", type=Path, default=ROOT / "implementation_plan/checkpoints/artifacts")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.live_opt_in:
        print("拒绝执行：真实 waveOut fault runner 必须显式提供 --live-opt-in", file=sys.stderr)
        return 2
    if args.stop_timeout <= 0 or args.stop_timeout > 10:
        print("拒绝执行：--stop-timeout 必须在 (0, 10] 秒内", file=sys.stderr)
        return 2
    audio_path = args.audio.resolve()
    if not audio_path.is_file():
        print(f"拒绝执行：音频不存在: {audio_path}", file=sys.stderr)
        return 2
    audio = audio_path.read_bytes()
    if not audio:
        print("拒绝执行：音频为空", file=sys.stderr)
        return 2

    batch_id = f"p7-real-waveout-{args.fault}-20260929-{uuid.uuid4().hex[:16]}"
    backend = WaveOutPlaybackBackend(fault_mode=MODES[args.fault])
    result = None
    error = None
    try:
        backend.start(audio)
        result = backend.stop(args.stop_timeout)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        backend.close()

    diagnostic = backend.diagnostics
    artifact = {
        "batch_id": batch_id,
        "evidence_kind": "controlled_real_waveout_fault",
        "fault_mode": args.fault,
        "fault_mode_value": MODES[args.fault].value,
        "live_opt_in": True,
        "real_backend": "windows_waveout",
        "audio_path": str(audio_path),
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "audio_bytes": len(audio),
        "stop_timeout_s": args.stop_timeout,
        "backend_stop_result": None if result is None else {
            "acknowledged": result.acknowledged,
            "confirmation": result.confirmation,
            "reason": result.reason,
        },
        "backend_error": error,
        "waveout_diagnostic": {
            "open_result": diagnostic.open_result,
            "prepare_result": diagnostic.prepare_result,
            "write_result": diagnostic.write_result,
            "reset_result": diagnostic.reset_result,
            "unprepare_result": diagnostic.unprepare_result,
            "close_result": diagnostic.close_result,
            "wom_done_observed": diagnostic.wom_done_at is not None,
            "cleanup_verified": diagnostic.cleanup_verified,
        },
        "loopback_output_stopped": None,
        "acoustic_output_stopped": None,
        "speaker_output_stopped": None,
        "user_heard": None,
        "boundary": "real waveOut backend fault classification only; no physical silence or user-heard claim",
        "status": "established" if result is not None and not result.acknowledged and result.confirmation in {"timeout", "unknown"} else "blocked",
    }
    out_dir = args.output_root.resolve() / f"2026-09-29-P7-real-waveout-fault-{args.fault}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{batch_id}.json"
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"batch_id": batch_id, "artifact": str(out_path), "status": artifact["status"],
                      "backend_stop_result": artifact["backend_stop_result"],
                      "waveout_diagnostic": artifact["waveout_diagnostic"]}, ensure_ascii=False, indent=2))
    return 0 if artifact["status"] == "established" else 1


if __name__ == "__main__":
    raise SystemExit(main())
