"""P1 · ``--speech-stall-timeout`` / ``speech_stall_timeout_s`` 的接线。

一条链上四段，全部只读断言，不建音频设备、不联网：

1. ``VoiceEntryConfig`` 接受该值并做正数校验（默认 = ``DEFAULT_STALL_TIMEOUT_S``）；
2. ``AutoVoiceConfig`` 同样接受，并**真的**透传到 ``VoiceEntryConfig``
   （``app.main._voice_entry_config_for_auto``，真机踩过"CLI 接受了却没生效"的坑）；
3. 流式通道（``build_streaming_session`` 包装）把预算传给 ``StreamingVoiceSession``；
4. pcm_seq 通道（``SequentialVoiceSession``）同样接住。

CLI 参数本身（``--speech-stall-timeout``）由 ``app.main`` 的 argparse 定义；这里断言的是
它落到配置对象之后的每一段，参数存在性由 ``--help`` 与 ``main.main`` 的解析结果手工核对。
"""
from __future__ import annotations

import unittest

from app.main import _voice_entry_config_for_auto
from app.runtime.auto_voice import AutoVoiceConfig
from app.runtime.voice_entry import VoiceEntryConfig
from app.tts.streaming_session import DEFAULT_STALL_TIMEOUT_S, build_streaming_session
from tests.tts.test_pcm_sequential_pipeline import (
    FakePlaybackWorker,
    FakeRouter,
    build_session,
)
from tests.tts.test_streaming_queue_backpressure import (
    DurationProvider,
    TimedPCMBackend,
    make_selection,
)


class VoiceEntryConfigTests(unittest.TestCase):
    def test_default_is_the_p1_default(self) -> None:
        self.assertEqual(VoiceEntryConfig().speech_stall_timeout_s, DEFAULT_STALL_TIMEOUT_S)

    def test_explicit_value_is_kept_as_float(self) -> None:
        config = VoiceEntryConfig(speech_stall_timeout_s=7)
        self.assertEqual(config.speech_stall_timeout_s, 7.0)
        self.assertIsInstance(config.speech_stall_timeout_s, float)

    def test_non_positive_or_non_numeric_values_are_rejected(self) -> None:
        for bad in (0, -1, 0.0, True, "15", None):
            with self.assertRaises(ValueError, msg=f"{bad!r} 不该被接受"):
                VoiceEntryConfig(speech_stall_timeout_s=bad)  # type: ignore[arg-type]


class AutoVoiceConfigTests(unittest.TestCase):
    def test_default_matches_the_shared_default(self) -> None:
        self.assertEqual(AutoVoiceConfig().speech_stall_timeout_s, DEFAULT_STALL_TIMEOUT_S)

    def test_value_is_forwarded_to_the_voice_entry_config(self) -> None:
        forwarded = _voice_entry_config_for_auto(AutoVoiceConfig(speech_stall_timeout_s=9.0))
        self.assertEqual(forwarded.speech_stall_timeout_s, 9.0)  # type: ignore[attr-defined]

    def test_non_positive_values_are_rejected(self) -> None:
        for bad in (0, -1, True):
            with self.assertRaises(ValueError, msg=f"{bad!r} 不该被接受"):
                AutoVoiceConfig(speech_stall_timeout_s=bad)  # type: ignore[arg-type]


class ChannelWiringTests(unittest.TestCase):
    def test_streaming_wrapper_forwards_the_budget(self) -> None:
        provider = DurationProvider()
        session = build_streaming_session(
            make_selection(),
            mode="ws_pcm",
            ws_provider=provider,
            http_provider=provider,
            backend_factory=lambda: TimedPCMBackend(),
            verifier=lambda selection: None,
            stall_timeout_s=6.5,
        )
        self.addCleanup(session.close)
        self.assertEqual(session.stall_timeout_s, 6.5)

    def test_pcm_seq_channel_keeps_the_budget(self) -> None:
        session = build_session(
            FakeRouter(),
            FakePlaybackWorker(),
            stall_timeout_s=6.5,
        )
        self.addCleanup(session.close)
        self.assertEqual(session.stall_timeout_s, 6.5)


if __name__ == "__main__":
    unittest.main()
