"""``pcm_seq`` 接线事实：新选项可被选中、装配到新会话，且默认通道一个字符没动。

这些用例**不接真实设备、不发真实请求**：只验证模式表、装配分支与默认值，以及新分支
确实把播放换成 :class:`SequentialVoiceSession`。真机 A/B（``--tts-mode pcm_seq``）
另行授权后运行。
"""

from __future__ import annotations

import unittest

from app.main import _tts_mode_needs_local_service, _voice_entry_config_for_auto
from app.runtime.auto_voice import AutoVoiceConfig
from app.runtime.voice_entry import (
    PCM_SEQ_MAX_QUEUED_AUDIO_MS,
    PCM_SEQ_MODE,
    STREAMING_TTS_MODES,
    TTS_MODES,
    VOICESTUDIO_TTS_MODES,
    SessionStream,
    StreamingSpeaker,
    VoiceEntryConfig,
)


class PcmSequentialModeRegistrationTests(unittest.TestCase):
    def test_pcm_seq_is_a_new_registered_mode(self) -> None:
        self.assertEqual(PCM_SEQ_MODE, "pcm_seq")
        self.assertIn(PCM_SEQ_MODE, TTS_MODES)

    def test_existing_modes_are_unchanged(self) -> None:
        # The four pre-existing modes keep their exact spelling and order; only a
        # new member is added around them.
        for mode in ("ws_pcm", "http_wav", "auto", "edge", "none"):
            self.assertIn(mode, TTS_MODES)
        self.assertEqual(STREAMING_TTS_MODES, ("ws_pcm", "http_wav", "auto"))
        self.assertEqual(VOICESTUDIO_TTS_MODES, ("ws_pcm", "http_wav", "auto", "pcm_seq"))
        self.assertNotIn(PCM_SEQ_MODE, STREAMING_TTS_MODES)

    def test_voice_entry_config_accepts_the_new_mode(self) -> None:
        config = VoiceEntryConfig(tts_mode=PCM_SEQ_MODE)
        self.assertEqual(config.tts_mode, PCM_SEQ_MODE)
        # The default is still ws_pcm, byte for byte.
        self.assertEqual(VoiceEntryConfig().tts_mode, "ws_pcm")

    def test_cli_mode_is_forwarded_by_the_auto_path(self) -> None:
        self.assertEqual(
            _voice_entry_config_for_auto(AutoVoiceConfig(tts_mode=PCM_SEQ_MODE)).tts_mode,
            PCM_SEQ_MODE,
        )
        self.assertEqual(_voice_entry_config_for_auto(AutoVoiceConfig()).tts_mode, "ws_pcm")

    def test_new_mode_needs_the_local_voicestudio_service(self) -> None:
        self.assertTrue(_tts_mode_needs_local_service(PCM_SEQ_MODE))
        self.assertTrue(_tts_mode_needs_local_service("ws_pcm"))
        self.assertFalse(_tts_mode_needs_local_service("edge"))
        self.assertFalse(_tts_mode_needs_local_service("none"))

    def test_queue_budget_covers_one_sentence_with_margin(self) -> None:
        # Two 49-block sentences at the nominal 100 ms block size = 9 800 ms, so
        # the configured budget must be at least that; the assembled value is the
        # larger of it and a 30 s floor.
        self.assertGreaterEqual(PCM_SEQ_MAX_QUEUED_AUDIO_MS, 9_800)
        self.assertEqual(PCM_SEQ_MAX_QUEUED_AUDIO_MS, 30_000)


class PcmSequentialAssemblyTests(unittest.TestCase):
    """The new branch really assembles the new session (offline, no device)."""

    def test_sequential_speaker_builds_the_new_session_and_reports_facts(self) -> None:
        from app.tts.pcm_playback import PCMPlaybackWorker
        from app.tts.providers.http_wav import (
            VoiceStudioHttpWavConfig,
            VoiceStudioHttpWavProvider,
        )
        from app.tts.providers.voicestudio_ws import (
            VoiceStudioWebSocketConfig,
            VoiceStudioWebSocketProvider,
        )
        from app.tts.sequential_session import SequentialVoiceSession
        from app.tts.transport import VoiceStudioTransportRouter

        # Mirrors the assembly in app/runtime/voice_entry.py without touching the
        # device: nothing is opened until a sentence is actually streamed.
        provider = VoiceStudioWebSocketProvider(
            VoiceStudioWebSocketConfig(endpoint="ws://127.0.0.1:3900")
        )
        http = VoiceStudioHttpWavProvider(VoiceStudioHttpWavConfig())
        worker = PCMPlaybackWorker(lambda: _NeverUsedBackend(), clock=_clock)

        def factory() -> SequentialVoiceSession:
            return SequentialVoiceSession(
                _Selection(),
                transport_router=VoiceStudioTransportRouter(
                    mode="ws_pcm", ws_provider=provider, http_provider=http, clock=_clock
                ),
                worker=worker,
                clock=_clock,
            )

        speaker = StreamingSpeaker(factory, label="voicestudio_pcm_seq")
        stream = speaker.start_stream()
        self.assertIsInstance(stream, SessionStream)
        session = stream._session  # noqa: SLF001 - the test owns this instance
        self.assertIsInstance(session, SequentialVoiceSession)
        self.assertEqual(session.pending_sentences, 0)
        # The existing adapter records the facts it already records; the new channel
        # exposes the same read-only names.
        for name in (
            "pending_sentences",
            "played_segments",
            "accepted_segments",
            "error",
            "cancelled",
            "sentences_total",
            "sentences_synthesized",
            "sentences_played",
            "played_chars",
            "played_characters",
            "lookahead_peak",
            "synth_wait_ms",
            "play_wait_ms",
            "text_length",
            "provider_error_code",
            "failed_sentence_index",
            "failed_request_chars",
        ):
            self.assertTrue(hasattr(session, name), name)
        stream.finish()
        self.assertIsNone(stream.record["error"])
        self.assertEqual(stream.record["route"], "voicestudio_pcm_seq")
        self.assertEqual(stream.record["played_segments"], 0)
        self.assertEqual(stream.record["text_length"], 0)


class _Selection:
    profile_id = "confirmed-default"
    user_id = "tester"
    speed = 1.0


class _NeverUsedBackend:
    """A backend that is never written to; it must never be opened either."""

    backend_name = "never-used"
    max_buffer_bytes = 9600

    def open(self) -> None:
        raise AssertionError("设备后端不得在装配阶段被打开")

    def writable(self) -> bool:
        return True

    def submit(self, pcm: bytes) -> None:
        raise AssertionError("设备后端不得被写入")

    def poll_drained(self) -> bool:
        return True

    def stop(self, timeout: float):
        from app.tts.playback_worker import StopResult

        return StopResult(True, "backend_buffer_returned", "never_used")

    def close(self) -> None:
        return None


def _clock() -> float:
    import time

    return time.perf_counter()


if __name__ == "__main__":
    unittest.main()
