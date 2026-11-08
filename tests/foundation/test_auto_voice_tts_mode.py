"""R4-B：免按键路径必须**透传 CLI 的播报通道**（`--tts-mode`）。

真机教训（2026-10-03）：`_voice_entry_config_for_auto()` 之前不传 `tts_mode`，
于是 `--tts-mode http_wav` 在 `--voice --auto` 下被**静默忽略**、永远走默认 `ws_pcm`
（命令行接受了却没生效，属诚实性问题）。默认值必须保持 `ws_pcm`（行为不变）。
"""

from __future__ import annotations

import unittest

from app.main import _voice_entry_config_for_auto
from app.runtime.auto_voice import AutoVoiceConfig


class AutoVoiceTtsModeTests(unittest.TestCase):
    def test_default_stays_ws_pcm(self) -> None:
        self.assertEqual(_voice_entry_config_for_auto(AutoVoiceConfig()).tts_mode, "ws_pcm")

    def test_explicit_mode_is_forwarded(self) -> None:
        for mode in ("http_wav", "edge", "none", "auto"):
            with self.subTest(mode=mode):
                config = AutoVoiceConfig(tts_mode=mode)
                self.assertEqual(_voice_entry_config_for_auto(config).tts_mode, mode)

    def test_auto_config_keeps_the_documented_default(self) -> None:
        self.assertEqual(AutoVoiceConfig().tts_mode, "ws_pcm")

    # ------------------------------------------------ --tts-speed（2026-10-23 批次）
    def test_tts_speed_defaults_to_none_so_behaviour_is_unchanged(self) -> None:
        self.assertIsNone(AutoVoiceConfig().tts_speed)
        self.assertIsNone(_voice_entry_config_for_auto(AutoVoiceConfig()).tts_speed)

    def test_explicit_tts_speed_is_forwarded_to_the_entry_config(self) -> None:
        config = AutoVoiceConfig(tts_speed=1.4)
        self.assertEqual(config.tts_speed, 1.4)
        self.assertEqual(_voice_entry_config_for_auto(config).tts_speed, 1.4)

    def test_tts_speed_outside_measured_range_is_rejected(self) -> None:
        for speed in (0.4, 2.1, True, "1.4"):
            with self.subTest(speed=speed):
                with self.assertRaises(ValueError):
                    AutoVoiceConfig(tts_speed=speed)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
