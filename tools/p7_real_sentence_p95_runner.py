"""P7 Batch 9 live runner: real sentence-level p95 over VoiceStudio → Windows waveOut.

Opt-in only. Without ``--live-opt-in`` it exits with code 2 and touches nothing.

Clock
-----
``time.monotonic`` on Windows is ``GetTickCount64()`` with a 15.625 ms resolution, so
it cannot resolve the worker-side spans at all (measured: 200k consecutive calls, zero
positive deltas). Real runs therefore inject ``time.perf_counter`` (QueryPerformanceCounter,
1e-7 s) into the session, the transport router and the playback worker, so every stamp
shares one high-resolution monotonic basis. ``--clock monotonic`` is kept only to
reproduce the low-resolution behaviour as evidence.

Pacing
------
``--pace paced`` (default) pushes one sentence only after the previous sentence's
playback sample exists, i.e. the pipeline never queues more than one sentence — the
realistic "Agent streams text as fast as speech consumes it" scenario. ``--pace burst``
pushes the whole sentence set at once to exercise the documented overload protection
(``max_pending_segments``); that scenario is expected to truncate and to set
``overloaded``/``late_events``, and must not be reported as a 20-sentence sample.

Boundaries
----------
* Audio plays through the real default Windows output device and is never written to
  disk; no microphone is opened; the only network target is 127.0.0.1:3900.
* A backend completion is not evidence that a person heard anything: the artifact keeps
  ``user_heard=null`` and ``speaker_acoustically_verified=false``.
* The artifact stores only counts, redacted ids, millisecond spans and environment
  facts — never sentence text, PCM bytes, a path or a URL.
"""
from __future__ import annotations

import argparse
import getpass
import json
import platform
import sys
import time
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.tts.private_selection import (  # noqa: E402
    CONFIRMED_PROFILE,
    VoiceSelection,
    verify_provider,
)
from app.tts.providers.http_wav import VoiceStudioHttpWavProvider  # noqa: E402
from app.tts.providers.voicestudio_ws import VoiceStudioWebSocketProvider  # noqa: E402
from app.tts.streaming_session import build_streaming_session  # noqa: E402
from app.tts.waveout_pcm import WaveOutPCMStreamBackend  # noqa: E402

ARTIFACT_ROOT = _REPO / "implementation_plan" / "checkpoints" / "artifacts" / "2026-09-30-P7-batch9-real-p95"

# Fixed, non-sensitive text; one sentence per entry so pacing can be exact.
SENTENCES = (
    "第一句用于真实延迟测量。",
    "第二句同样不含个人内容。",
    "第三句继续验证顺序播报。",
    "第四句结束第一轮。",
    "第五句开始第二轮推送。",
    "第六句保持同样语速。",
    "第七句不包含任何私人信息。",
    "第八句结束第二轮。",
    "第九句用于稳定统计。",
    "第十句继续测量。",
    "第十一句保持原顺序。",
    "第十二句结束第三轮。",
    "第十三句进入第四轮。",
    "第十四句不改变音色。",
    "第十五句继续。",
    "第十六句结束第四轮。",
    "第十七句进入最后一轮。",
    "第十八句继续。",
    "第十九句接近结束。",
    "第二十句是最后一句。",
)
WARMUP_TEXT = "预热句子，用于让模型进入热态，不计入统计。"


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-opt-in", action="store_true", help="required to run live")
    parser.add_argument("--mode", default="ws_pcm", choices=("ws_pcm", "http_wav", "auto"))
    parser.add_argument("--pace", default="paced", choices=("paced", "burst"))
    parser.add_argument("--clock", default="perf_counter", choices=("perf_counter", "monotonic"))
    parser.add_argument("--start-buffer-ms", type=int, default=160)
    parser.add_argument("--skip-warmup", action="store_true")
    return parser.parse_args(argv)


def _clock_for(name: str):
    return time.perf_counter if name == "perf_counter" else time.monotonic


def _new_session(selection, mode: str, start_buffer_ms: int, clock):
    return build_streaming_session(
        selection,
        mode=mode,
        ws_provider=VoiceStudioWebSocketProvider(clock=clock),
        http_provider=VoiceStudioHttpWavProvider(),
        backend_factory=lambda: WaveOutPCMStreamBackend(clock=clock),
        start_buffer_ms=start_buffer_ms,
        clock=clock,
    )


def _health() -> dict:
    import urllib.request

    with urllib.request.urlopen("http://127.0.0.1:3900/health", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_once(selection, args, *, warmup: bool) -> dict:
    clock = _clock_for(args.clock)
    if warmup:
        session = _new_session(selection, args.mode, args.start_buffer_ms, clock)
        try:
            session.push_text_chunk(WARMUP_TEXT)
            session.finish()
        finally:
            session.close()

    session = _new_session(selection, args.mode, args.start_buffer_ms, clock)
    started_at = clock()
    pace_waits: list[float] = []
    try:
        if args.pace == "burst":
            # A single push is bounded to 8 sentences by the session, so push the set
            # as 4-sentence chunks back-to-back to exercise the overload protection.
            for start in range(0, len(SENTENCES), 4):
                session.push_text_chunk("".join(SENTENCES[start:start + 4]))
        else:
            for index, sentence in enumerate(SENTENCES, start=1):
                session.push_text_chunk(sentence)
                push_at = clock()
                # Wait until this sentence produced its sample (or the session failed)
                # so the pipeline never holds more than one pending sentence.
                while len(session.timing_samples) < index:
                    if session.error is not None or session.finished or session.overloaded:
                        break
                    time.sleep(0.002)
                    if clock() - push_at > 60.0:
                        break
                pace_waits.append((clock() - push_at) * 1000.0)
                if session.overloaded:
                    break
        played = session.finish()
    finally:
        session.close()
    finished_at = clock()

    samples = session.timing_samples
    percentiles = session.timing_percentiles()
    return {
        "mode": args.mode,
        "pace": args.pace,
        "clock": args.clock,
        "start_buffer_ms": args.start_buffer_ms,
        "wall_seconds": round(finished_at - started_at, 3),
        "played_segments": len(played),
        "error": None if session.error is None else type(session.error).__name__,
        "overloaded": session.overloaded,
        "dropped_pushes": getattr(session, "dropped_pushes", None),
        "pending_sentences": getattr(session, "pending_sentences", None),
        "verified": getattr(session, "verified", None),
        "finish_timed_out": getattr(session, "finish_timed_out", None),
        "late_events": list(session.late_events),
        "pace_wait_ms_after_push": pace_waits,
        "samples": [
            {
                "segment_index": sample.segment_index,
                "generation": sample.generation,
                "ready_to_submit_ms": sample.ready_to_submit_ms,
                "submit_to_first_pcm_ms": sample.submit_to_first_pcm_ms,
                "first_pcm_to_device_ms": sample.first_pcm_to_device_ms,
                "first_pcm_to_gate_ms": sample.first_pcm_to_gate_ms,
                "gate_to_device_ms": sample.gate_to_device_ms,
            }
            for sample in samples
        ],
        "percentiles": {
            "ready_to_submit": {
                "count": percentiles.ready_to_submit.count,
                "p50": percentiles.ready_to_submit.p50,
                "p95": percentiles.ready_to_submit.p95,
            },
            "first_pcm_to_device": {
                "count": percentiles.first_pcm_to_device.count,
                "p50": percentiles.first_pcm_to_device.p50,
                "p95": percentiles.first_pcm_to_device.p95,
            },
            "submit_to_first_pcm": {
                "count": percentiles.submit_to_first_pcm.count,
                "p50": percentiles.submit_to_first_pcm.p50,
                "p95": percentiles.submit_to_first_pcm.p95,
            },
            "gate_to_device": {
                "count": percentiles.gate_to_device.count,
                "p50": percentiles.gate_to_device.p50,
                "p95": percentiles.gate_to_device.p95,
            },
        },
    }


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in", file=sys.stderr)
        return 2

    selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
    selection.validate()
    verify_provider(selection)
    health = _health()

    batch_id = f"p7-b9-sentence-p95-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:16]}"
    result = {
        "batch_id": batch_id,
        "evidence_kind": "real_sentence_timing_p95",
        "live_opt_in": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "clock": args.clock,
        "voicestudio": health,
        "voice_profile": {
            "profile_id": selection.profile_id,
            "speed": selection.speed,
            "pause_ms": selection.pause_ms,
            "fade_ms": selection.fade_ms,
            "version": selection.version,
        },
        "sentence_set": {"sentences": len(SENTENCES), "warmup": not args.skip_warmup},
        "runs": [_run_once(selection, args, warmup=not args.skip_warmup)],
        "boundary": {
            "audio_written_to_disk": False,
            "microphone_opened": False,
            "network_target": "127.0.0.1:3900 only",
            "speaker_acoustically_verified": False,
            "user_heard": None,
            "note": "backend completion is not evidence that a person heard the audio",
        },
    }

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / f"{batch_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    run = result["runs"][0]
    print(f"artifact={path}")
    print(
        f"mode={run['mode']} pace={run['pace']} clock={run['clock']} "
        f"played={run['played_segments']} wall={run['wall_seconds']}s "
        f"overloaded={run['overloaded']} dropped_pushes={run['dropped_pushes']} "
        f"verified={run['verified']} finish_timed_out={run['finish_timed_out']} "
        f"error={run['error']}"
    )
    print(f"ready_to_submit: {run['percentiles']['ready_to_submit']}")
    print(f"submit_to_first_pcm: {run['percentiles']['submit_to_first_pcm']}")
    print(f"gate_to_device : {run['percentiles']['gate_to_device']}")
    print(f"first_pcm_to_device: {run['percentiles']['first_pcm_to_device']}")
    print(f"late_events={run['late_events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
