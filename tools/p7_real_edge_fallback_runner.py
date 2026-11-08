"""P7 B11 live acceptance runner: offline Edge voice route (SAPI) on the real device.

Opt-in only: without ``--live-opt-in`` it exits with code 2 and touches nothing.

Scenarios
---------
``edge_direct``
    Synthesise one sentence with the real offline SAPI voice and play it through the
    real Windows waveOut path (no VoiceStudio involved).
``edge_fallback_after_kill``
    Real VoiceStudio + real offline Edge voice configured: push one sentence, then
    terminate the VoiceStudio backend before the first PCM block, so the routing
    contract switches the still-unplayed sentence to Edge and the reply is spoken by
    the offline voice instead of degrading to text.
``proxy_drop_no_edge``
    Controlled loopback fault proxy that emits ``start`` plus PCM and then drops the
    socket: the sentence already produced audio (``PLAYBACK_STARTED``), so the Edge
    route must NOT be used, the current sentence must not be replayed and the reply
    must continue as text.

Boundaries
----------
* Audio plays through the real default Windows output device; nothing is written to
  disk except this artifact; no microphone; the only network use is 127.0.0.1.
* ``kill_*`` requires ``--kill-pid`` and never targets this process; the caller
  restarts the backend afterwards.
* ``user_heard=null`` and ``speaker_acoustically_verified=false`` always: a backend
  completion is not evidence that a person heard anything.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.tts.clock import monotonic_seconds  # noqa: E402
from app.tts.pcm_playback import PCMPlaybackWorker  # noqa: E402
from app.tts.playback_worker import PlaybackState  # noqa: E402
from app.tts.private_selection import (  # noqa: E402
    CONFIRMED_PROFILE,
    VoiceSelection,
    verify_provider,
)
from app.tts.providers import VoiceStudioSentenceRequest  # noqa: E402
from app.tts.providers.edge_sapi import EdgeSapiConfig, EdgeSapiVoiceProvider  # noqa: E402
from app.tts.routing import TTSFallbackRouter  # noqa: E402
from app.tts.streaming_session import build_streaming_session  # noqa: E402
from app.tts.waveout_pcm import WaveOutPCMStreamBackend  # noqa: E402

ARTIFACT_ROOT = (
    _REPO / "implementation_plan" / "checkpoints" / "artifacts" / "2026-09-30-P7-b11-offline-edge-voice"
)
EDGE_VOICE = "Microsoft Huihui Desktop"
PHRASE_1 = "离线备用声线验收第一句。"
PHRASE_2 = "离线备用声线验收第二句。"
PROXY_WS = "ws://127.0.0.1:3996"
_PROXY_START = json.dumps(
    {"type": "start", "sample_rate": 24000, "channels": 1, "format": "pcm16", "engine": "omnivoice"}
)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-opt-in", action="store_true", help="required to run live")
    parser.add_argument("--scenario", required=True, choices=("edge_direct", "edge_fallback_after_kill", "proxy_drop_no_edge"))
    parser.add_argument("--kill-pid", type=int, default=None)
    parser.add_argument("--kill-delay-s", type=float, default=0.4)
    parser.add_argument("--voice", default=EDGE_VOICE)
    return parser.parse_args(argv)


def _start_drop_proxy(port: int):
    from websockets.sync.server import serve

    def handler(connection) -> None:
        try:
            connection.recv()
            connection.send(_PROXY_START)
            for _ in range(3):
                connection.send(b"\x01\x00" * 4800)
        except Exception:  # noqa: BLE001 - the drop is the point
            pass
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    server = serve(handler, "127.0.0.1", port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _kill(pid: int) -> str:
    if pid <= 0 or pid == os.getpid():
        raise ValueError("refusing to kill this process or an invalid PID")
    try:
        os.kill(pid, signal.SIGTERM)
        return "terminated"
    except OSError as exc:
        return f"kill_failed:{type(exc).__name__}"


def _decision_dict(decision) -> dict | None:
    if decision is None:
        return None
    return {
        "route": decision.route.value,
        "action": decision.action.value,
        "playback_state": decision.playback_state.value,
        "replay_current_segment": decision.replay_current_segment,
        "drop_remaining_segments": decision.drop_remaining_segments,
        "next_reply_route": decision.next_reply_route.value,
        "reason": decision.reason,
    }


def _run_edge_direct(selection, args) -> dict:
    """Real SAPI synthesis played through the real waveOut path, without VoiceStudio."""

    provider = EdgeSapiVoiceProvider(EdgeSapiConfig(voice_name=args.voice))
    worker = PCMPlaybackWorker(lambda: WaveOutPCMStreamBackend(clock=monotonic_seconds), clock=monotonic_seconds)
    request = VoiceStudioSentenceRequest(
        reply_id="edge-direct",
        segment_id="segment-1",
        provider_request_id="edge-direct-1",
        provider_generation=1,
        text=PHRASE_1,
        profile_id=selection.profile_id,
        language="Chinese",
        speed=1.0,
    )
    started = monotonic_seconds()
    first_pcm_at = None
    chunks = 0
    try:
        ticket = worker.begin_sentence(request)
        for chunk in provider.stream_sentence(request, cancelled=threading.Event()):
            if first_pcm_at is None:
                first_pcm_at = monotonic_seconds()
            worker.push_chunk(chunk)
            chunks += 1
        state = worker.wait(ticket, 30.0)
    finally:
        worker.close()
    finished = monotonic_seconds()
    return {
        "scenario": "edge_direct",
        "voice": args.voice,
        "chunks": chunks,
        "playback_state": state.value,
        "first_pcm_after_s": None if first_pcm_at is None else round(first_pcm_at - started, 3),
        "wall_seconds": round(finished - started, 3),
    }


def _run_session_scenario(selection, args, session) -> dict:
    if args.scenario == "edge_fallback_after_kill":
        if not args.kill_pid:
            raise SystemExit("edge_fallback_after_kill requires --kill-pid")
        session.push_text_chunk(PHRASE_1)
        time.sleep(args.kill_delay_s)
        kill_result = _kill(args.kill_pid)
    else:
        session.push_text_chunk(PHRASE_1)
        kill_result = None

    finish_raised = None
    played: tuple[str, ...] = ()
    try:
        played = session.finish()
    except Exception as exc:  # noqa: BLE001 - the drill expects the recorded failure
        finish_raised = f"{type(exc).__name__}:{getattr(exc, 'code', None)}"
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - best effort
            pass

    return {
        "scenario": args.scenario,
        "kill_pid": args.kill_pid,
        "kill_result": kill_result,
        "pushed_sentences": 1,
        "played_segments": len(played),
        "completed_playback_sentences": len(session.timing_samples),
        "edge_attempts": session.edge_attempts,
        "edge_failures": session.edge_failures,
        "error": None if session.error is None else type(session.error).__name__,
        "error_code": None if session.error is None else getattr(session.error, "code", None),
        "finish_raised": finish_raised,
        "failure_states": {segment: state.value for segment, state in session.failure_states.items()},
        "routing_decision": _decision_dict(session.routing_decision),
        "late_events": list(session.late_events),
        "timing_sample_count": len(session.timing_samples),
    }


def _build_session(selection, args, *, ws_endpoint: str, mode: str):
    from app.tts.providers.http_wav import VoiceStudioHttpWavConfig, VoiceStudioHttpWavProvider
    from app.tts.providers.voicestudio_ws import VoiceStudioWebSocketConfig, VoiceStudioWebSocketProvider

    clock = monotonic_seconds
    return build_streaming_session(
        selection,
        mode=mode,
        ws_provider=VoiceStudioWebSocketProvider(
            VoiceStudioWebSocketConfig(endpoint=ws_endpoint, sentence_timeout_seconds=18.0), clock=clock
        ),
        http_provider=VoiceStudioHttpWavProvider(VoiceStudioHttpWavConfig(timeout_seconds=8.0)),
        backend_factory=lambda: WaveOutPCMStreamBackend(clock=clock),
        fallback_router=TTSFallbackRouter(edge_voice_profile_id="edge-sapi-local"),
        edge_provider=EdgeSapiVoiceProvider(EdgeSapiConfig(voice_name=args.voice)),
        clock=clock,
    )


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in", file=sys.stderr)
        return 2

    selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
    selection.validate()

    proxy = None
    if args.scenario == "edge_direct":
        run = _run_edge_direct(selection, args)
    elif args.scenario == "edge_fallback_after_kill":
        verify_provider(selection)
        session = _build_session(selection, args, ws_endpoint="ws://127.0.0.1:3900", mode="ws_pcm")
        run = _run_session_scenario(selection, args, session)
    else:
        verify_provider(selection)
        proxy = _start_drop_proxy(3996)
        try:
            session = _build_session(selection, args, ws_endpoint=PROXY_WS, mode="ws_pcm")
            run = _run_session_scenario(selection, args, session)
        finally:
            try:
                proxy.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass

    batch_id = (
        f"p7-b11-offline-edge-{args.scenario}-"
        f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:16]}"
    )
    result = {
        "batch_id": batch_id,
        "evidence_kind": "offline_edge_voice_acceptance",
        "live_opt_in": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "voice": args.voice,
        "runs": [run],
        "boundary": {
            "audio_written_to_disk": False,
            "microphone_opened": False,
            "external_network_used": False,
            "speaker_acoustically_verified": False,
            "user_heard": None,
            "note": "a backend completion is not evidence that a person heard the audio",
        },
    }
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / f"{batch_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"artifact={path}")
    for key, value in run.items():
        print(f"  {key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
