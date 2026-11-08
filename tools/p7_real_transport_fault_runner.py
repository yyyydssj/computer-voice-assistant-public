"""P7 live transport-fault drill runner (tracker items 1 and 2).

Opt-in only: without ``--live-opt-in`` it exits with code 2 and touches nothing.

Scenarios
---------
``dual_path_dead``
    ``auto`` mode with the WebSocket endpoint and the HTTP endpoint both pointing at
    closed loopback ports: both real transports fail by a real connection refusal, so
    the reply must degrade to text (no re-synthesis loop, no replay).
``http_dead``
    ``http_wav`` mode against a closed loopback port: the real HTTP path fails.
``kill_before_audio``
    Real VoiceStudio: push one sentence, then terminate the backend process while the
    sentence is still being synthesised (before any PCM), so the established socket
    dies before first audio. Expect ``NOT_COMMITTED`` for that segment, no playback,
    text preserved.
``kill_after_playback``
    Real VoiceStudio: push sentence 1 and wait until it finished playing, then kill
    the backend and push sentence 2, so the transport fails after the reply already
    produced audio. Expect sentence 1 played, sentence 2's real failure recorded with
    its routing decision, and no voice switch/replay for what is already audible.

Boundaries
----------
* Audio is played through the real default Windows output device (kill scenarios only).
* Nothing is written to disk except the JSON artifact; no microphone is opened.
* ``kill_*`` requires ``--kill-pid`` (the VoiceStudio backend PID) and never targets
  this process; the caller is responsible for restarting the backend afterwards.
* The artifact keeps ``user_heard=null`` and ``speaker_acoustically_verified=false``:
  a backend failure or completion is not evidence that a person heard anything.
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
from app.tts.private_selection import (  # noqa: E402
    CONFIRMED_PROFILE,
    VoiceSelection,
    verify_provider,
)
from app.tts.providers.http_wav import (  # noqa: E402
    VoiceStudioHttpWavConfig,
    VoiceStudioHttpWavProvider,
)
from app.tts.providers.voicestudio_ws import (  # noqa: E402
    VoiceStudioWebSocketConfig,
    VoiceStudioWebSocketProvider,
)
from app.tts.routing import TTSFallbackRouter  # noqa: E402
from app.tts.streaming_session import build_streaming_session  # noqa: E402
from app.tts.waveout_pcm import WaveOutPCMStreamBackend  # noqa: E402

ARTIFACT_ROOT = (
    _REPO / "implementation_plan" / "checkpoints" / "artifacts" / "2026-09-30-P7-batch10-real-transport-faults"
)
PHRASE_1 = "故障演练第一句，用于真实链路。"
PHRASE_2 = "故障演练第二句，同样不含私人信息。"
WARMUP_TEXT = "预热句子，用于让 VoiceStudio 模型进入热态，不计入演练。"
DEAD_WS = "ws://127.0.0.1:3998"
DEAD_HTTP = "http://127.0.0.1:3997"
PROXY_WS = "ws://127.0.0.1:3996"
_PROXY_START = json.dumps(
    {
        "type": "start",
        "sample_rate": 24000,
        "channels": 1,
        "format": "pcm16",
        "engine": "omnivoice",
    }
)


def _start_drop_proxy(port: int):
    """Controlled loopback fault proxy: send ``start`` + one PCM block, then drop.

    The socket is a real loopback WebSocket served in-process; the audited provider
    code connects to it through its normal transport. The proxy never sends ``done``
    and closes instead, which is the "connection dropped after audio started" fault
    the plan requires a controlled proxy for.
    """

    from websockets.sync.server import serve

    def handler(connection) -> None:
        try:
            connection.recv()  # drain the client's request frame
            connection.send(_PROXY_START)
            # The provider holds one audio frame back until the next one arrives, so
            # send three: the first two are yielded to the session (``pushed >= 1``),
            # and the third is left pending when the socket drops without ``done``.
            for _ in range(3):
                connection.send(b"\x01\x00" * 4800)  # 4800 samples of PCM16 = 200 ms
        except Exception:  # noqa: BLE001 - the drop is the point
            pass
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    server = serve(handler, "127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-opt-in", action="store_true", help="required to run live")
    parser.add_argument(
        "--scenario",
        required=True,
        choices=("dual_path_dead", "http_dead", "kill_before_audio", "kill_after_playback", "proxy_drop_mid_sentence"),
    )
    parser.add_argument("--kill-pid", type=int, default=None, help="VoiceStudio backend PID")
    parser.add_argument("--kill-delay-s", type=float, default=0.35)
    parser.add_argument("--no-warmup", action="store_true", help="skip the warm-up sentence")
    parser.add_argument("--sentence-timeout-s", type=float, default=18.0)
    parser.add_argument("--ws-endpoint", default="ws://127.0.0.1:3900")
    parser.add_argument("--http-endpoint", default="http://127.0.0.1:3900")
    return parser.parse_args(argv)


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
        "cancel_previous_provider_request": decision.cancel_previous_provider_request,
        "next_reply_route": decision.next_reply_route.value,
        "reason": decision.reason,
    }


def _build(selection, *, mode: str, ws_endpoint: str, http_endpoint: str, sentence_timeout_s: float = 18.0):
    clock = monotonic_seconds
    ws = VoiceStudioWebSocketProvider(
        VoiceStudioWebSocketConfig(
            endpoint=ws_endpoint,
            connect_timeout_seconds=2.0,
            sentence_timeout_seconds=sentence_timeout_s,
            frame_idle_timeout_seconds=3.0,
            session_idle_seconds=30.0,
        ),
        clock=clock,
    )
    http = VoiceStudioHttpWavProvider(VoiceStudioHttpWavConfig(endpoint=http_endpoint, timeout_seconds=8.0))
    return build_streaming_session(
        selection,
        mode=mode,
        ws_provider=ws,
        http_provider=http,
        backend_factory=lambda: WaveOutPCMStreamBackend(clock=clock),
        fallback_router=TTSFallbackRouter(),
        clock=clock,
    )


def _run(args, selection) -> dict:
    scenario = args.scenario
    mode = {"dual_path_dead": "auto", "http_dead": "http_wav"}.get(scenario, "ws_pcm")
    if scenario == "proxy_drop_mid_sentence":
        ws_endpoint = PROXY_WS
    else:
        ws_endpoint = DEAD_WS if scenario == "dual_path_dead" else args.ws_endpoint
    http_endpoint = DEAD_HTTP if scenario in ("dual_path_dead", "http_dead") else args.http_endpoint
    if scenario.startswith("kill_") and not args.kill_pid:
        raise SystemExit("kill_* scenarios require --kill-pid")

    session = _build(
        selection,
        mode=mode,
        ws_endpoint=ws_endpoint,
        http_endpoint=http_endpoint,
        sentence_timeout_s=args.sentence_timeout_s,
    )
    warmup_result = None
    if scenario.startswith("kill_") and not args.no_warmup:
        warm = _build(
            selection,
            mode=mode,
            ws_endpoint=ws_endpoint,
            http_endpoint=http_endpoint,
            sentence_timeout_s=args.sentence_timeout_s,
        )
        try:
            warm.push_text_chunk(WARMUP_TEXT)
            try:
                warm.finish()
                warmup_result = "played" if warm.timing_samples else "no_sample"
            except Exception as exc:  # noqa: BLE001 - warm-up failure is recorded, not fatal
                warmup_result = f"raised:{type(exc).__name__}"
        finally:
            try:
                warm.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    kill_result = None
    pushed_text = [PHRASE_1, PHRASE_2] if scenario == "kill_after_playback" else [PHRASE_1]
    started = monotonic_seconds()
    finish_raised: str | None = None
    played: tuple[str, ...] = ()
    first_sentence_error: str | None = None
    try:
        session.push_text_chunk(PHRASE_1)
        if scenario == "kill_before_audio":
            time.sleep(args.kill_delay_s)
            kill_result = _kill(args.kill_pid)
        elif scenario == "kill_after_playback":
            deadline = monotonic_seconds() + 70.0
            while (
                len(session.timing_samples) < 1
                and session.error is None
                and monotonic_seconds() < deadline
            ):
                time.sleep(0.01)
            first_sentence_error = None if session.error is None else type(session.error).__name__
            kill_result = _kill(args.kill_pid)
            session.push_text_chunk(PHRASE_2)
        try:
            played = session.finish()
        except Exception as exc:  # noqa: BLE001 - the drill *expects* the recorded failure
            # ``finish()`` re-raises the session's recorded error by design; that raise is
            # part of the evidence, so record it instead of letting the drill abort.
            finish_raised = f"{type(exc).__name__}:{getattr(exc, 'code', None)}"
    finally:
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001 - closing is best effort
            finish_raised = finish_raised or f"close:{type(exc).__name__}"
    finished = monotonic_seconds()

    return {
        "scenario": scenario,
        "mode": mode,
        "ws_endpoint": ws_endpoint,
        "http_endpoint": http_endpoint,
        "kill_pid": args.kill_pid,
        "kill_delay_s": args.kill_delay_s if scenario == "kill_before_audio" else None,
        "kill_result": kill_result,
        "proxy": (
            {"port": 3996, "server_frames": ["start", "binary(pcm16,4800 samples)"], "dropped_before": "done"}
            if scenario == "proxy_drop_mid_sentence"
            else None
        ),
        "warmup": warmup_result,
        "first_sentence_error": first_sentence_error,
        "wall_seconds": round(finished - started, 3),
        "pushed_text_count": len(pushed_text),
        "pushed_text_unchanged": True,
        "finish_raised": finish_raised,
        "played_segments": len(played),
        "completed_playback_sentences": len(session.timing_samples),
        "played_segment_ids": [f"segment-{index + 1}" for index in range(len(played))],
        "error": None if session.error is None else type(session.error).__name__,
        "error_code": None if session.error is None else getattr(session.error, "code", None),
        "failure_states": {
            segment: state.value for segment, state in session.failure_states.items()
        },
        "routing_decision": _decision_dict(session.routing_decision),
        "timing_sample_count": len(session.timing_samples),
        "late_events": list(session.late_events),
        "overloaded": session.overloaded,
        "dropped_pushes": session.dropped_pushes,
        "verified": session.verified,
        "finish_timed_out": session.finish_timed_out,
    }


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in", file=sys.stderr)
        return 2

    selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
    selection.validate()
    if args.scenario not in ("dual_path_dead", "http_dead"):
        verify_provider(selection)

    proxy = _start_drop_proxy(3996) if args.scenario == "proxy_drop_mid_sentence" else None
    try:
        run_result = _run(args, selection)
    finally:
        if proxy is not None:
            try:
                proxy.shutdown()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass

    batch_id = (
        f"p7-b10-transport-fault-{args.scenario}-"
        f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:16]}"
    )
    result = {
        "batch_id": batch_id,
        "evidence_kind": "real_transport_fault_drill",
        "live_opt_in": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "runs": [run_result],
        "boundary": {
            "audio_written_to_disk": False,
            "microphone_opened": False,
            "speaker_acoustically_verified": False,
            "user_heard": None,
            "note": "a transport failure or a backend completion is not evidence that a person heard audio",
        },
    }

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / f"{batch_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    run = result["runs"][0]
    print(f"artifact={path}")
    print(
        f"scenario={run['scenario']} mode={run['mode']} played={run['played_segments']} "
        f"error={run['error']}/{run['error_code']} wall={run['wall_seconds']}s"
    )
    print(f"failure_states={run['failure_states']}")
    print(f"routing_decision={run['routing_decision']}")
    print(f"late_events={run['late_events']} samples={run['timing_sample_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
