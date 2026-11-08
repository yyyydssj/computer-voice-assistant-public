"""P7 user-heard confirmation runner: play two distinguishable sentences out loud.

Opt-in only: without ``--live-opt-in`` it exits with code 2 and touches nothing.

It plays, through the real default Windows output device:

1. **主路径**：真实 VoiceStudio WebSocket PCM 路径（小饼声线 profile `e8572e8e`）；
2. **备用声线**：离线 Edge 槽（本机 SAPI `Microsoft Huihui Desktop`）。

The two sentences are deliberately different in wording so the operator can tell them
apart, and they use two different voices. The artifact records every mechanical fact
with ``user_heard=null``: only the operator can set it, in a separate confirmation
step, because device completion is not evidence that a person heard anything.
"""
from __future__ import annotations

import argparse
import getpass
import json
import platform
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
from app.tts.private_selection import (  # noqa: E402
    CONFIRMED_PROFILE,
    VoiceSelection,
    verify_provider,
)
from app.tts.providers import VoiceStudioSentenceRequest  # noqa: E402
from app.tts.providers.edge_sapi import EdgeSapiConfig, EdgeSapiVoiceProvider  # noqa: E402
from app.tts.providers.http_wav import VoiceStudioHttpWavConfig, VoiceStudioHttpWavProvider  # noqa: E402
from app.tts.providers.voicestudio_ws import VoiceStudioWebSocketConfig, VoiceStudioWebSocketProvider  # noqa: E402
from app.tts.routing import TTSFallbackRouter  # noqa: E402
from app.tts.streaming_session import build_streaming_session  # noqa: E402
from app.tts.waveout_pcm import WaveOutPCMStreamBackend  # noqa: E402

ARTIFACT_ROOT = _REPO / "implementation_plan" / "checkpoints" / "artifacts" / "2026-09-30-P7-user-heard"
VOICE_PRIMARY = "主路径听感确认第一句：这是 VoiceStudio 的小饼声线。"
VOICE_EDGE = "备用声线听感确认第二句：这是本机离线的 Edge 槽声线。"
EDGE_VOICE = "Microsoft Huihui Desktop"


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-opt-in", action="store_true", help="required to run live")
    return parser.parse_args(argv)


def _play_primary(selection) -> dict:
    clock = monotonic_seconds
    session = build_streaming_session(
        selection,
        mode="ws_pcm",
        ws_provider=VoiceStudioWebSocketProvider(
            VoiceStudioWebSocketConfig(endpoint="ws://127.0.0.1:3900", sentence_timeout_seconds=18.0), clock=clock
        ),
        http_provider=VoiceStudioHttpWavProvider(VoiceStudioHttpWavConfig(timeout_seconds=8.0)),
        backend_factory=lambda: WaveOutPCMStreamBackend(clock=clock),
        fallback_router=TTSFallbackRouter(),
        clock=clock,
    )
    started = clock()
    try:
        session.push_text_chunk(VOICE_PRIMARY)
        played = session.finish()
    finally:
        session.close()
    return {
        "route": "voicestudio_ws_pcm",
        "text": VOICE_PRIMARY,
        "played_segments": len(played),
        "completed_playback_sentences": len(session.timing_samples),
        "error": None if session.error is None else type(session.error).__name__,
        "wall_seconds": round(clock() - started, 3),
    }


def _play_edge(selection) -> dict:
    clock = monotonic_seconds
    provider = EdgeSapiVoiceProvider(EdgeSapiConfig(voice_name=EDGE_VOICE), clock=clock)
    worker = PCMPlaybackWorker(lambda: WaveOutPCMStreamBackend(clock=clock), clock=clock)
    request = VoiceStudioSentenceRequest(
        reply_id="user-heard-edge",
        segment_id="segment-1",
        provider_request_id="user-heard-edge-1",
        provider_generation=1,
        text=VOICE_EDGE,
        profile_id=selection.profile_id,
        language="Chinese",
        speed=1.0,
    )
    started = clock()
    chunks = 0
    try:
        ticket = worker.begin_sentence(request)
        for chunk in provider.stream_sentence(request, cancelled=threading.Event()):
            worker.push_chunk(chunk)
            chunks += 1
        state = worker.wait(ticket, 30.0)
    finally:
        worker.close()
    return {
        "route": "edge_sapi_local",
        "voice": EDGE_VOICE,
        "text": VOICE_EDGE,
        "chunks": chunks,
        "playback_state": state.value,
        "wall_seconds": round(clock() - started, 3),
    }


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.live_opt_in:
        print("refusing to run without --live-opt-in", file=sys.stderr)
        return 2

    selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
    selection.validate()
    verify_provider(selection)

    print("即将播放两句（请听扬声器）：")
    print(f"  1) 主路径  ：{VOICE_PRIMARY}")
    print(f"  2) 备用声线：{VOICE_EDGE}")
    primary = _play_primary(selection)
    print(f"  [1] 完成：{primary}")
    time.sleep(0.8)
    edge = _play_edge(selection)
    print(f"  [2] 完成：{edge}")

    batch_id = f"p7-user-heard-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:16]}"
    result = {
        "batch_id": batch_id,
        "evidence_kind": "user_heard_confirmation",
        "live_opt_in": True,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.node(),
        "platform": platform.platform(),
        "runs": [primary, edge],
        "user_heard": None,
        "user_heard_note": "仅操作员本人确认后填写；设备完成不等于有人听见",
        "speaker_acoustically_verified": False,
        "boundary": {
            "audio_written_to_disk": False,
            "microphone_opened": False,
            "external_network_used": False,
            "network_target": "127.0.0.1:3900 (primary) / local SAPI (edge)",
        },
    }
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / f"{batch_id}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"artifact={path}")
    print("请确认是否听到这两句（artifact 里 user_heard 仍为 null，待你确认后另行登记）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
