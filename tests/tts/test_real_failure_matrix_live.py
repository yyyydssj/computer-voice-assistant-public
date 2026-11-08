"""Opt-in live evidence tests for the VoiceStudio/device boundary.

These tests are intentionally excluded from ordinary discovery. They require an
explicit operator opt-in because they contact the local VoiceStudio instance and
may emit audio through the selected Windows output device. A backend completion is
not evidence that a person heard the audio.

They establish only a live success baseline. Provider timeout/HTTP-error/bad-WAV
cases still require a controlled fault proxy or a provider-supported fault mode;
those cases must not be simulated here and reported as real VoiceStudio evidence.
"""
from __future__ import annotations

import getpass
import os
import unittest

from app.tts.private_selection import CONFIRMED_PROFILE, VoiceSelection, verify_provider
from app.tts.playback_worker import PlaybackState
from app.tts.real_playback import RealVoicePreview, _checked_wave, _generate


def live_opt_in_enabled(environ=None) -> bool:
    """Return true only for the explicit live-evidence opt-in value."""
    values = os.environ if environ is None else environ
    return values.get("P7_RUN_REAL_TTS_EVIDENCE") == "1"


_RUN_LIVE = live_opt_in_enabled()
_FIXED_TEXT = "P7真实链路基线。"


@unittest.skipUnless(
    _RUN_LIVE,
    "set P7_RUN_REAL_TTS_EVIDENCE=1 to run live VoiceStudio/device evidence",
)
class LiveVoiceStudioDeviceEvidenceTests(unittest.TestCase):
    """Evidence-producing tests; never part of the default CI gate.

    A backend completion is not evidence that a person heard the audio.
    """

    def setUp(self) -> None:
        self.selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())

    def test_live_voicestudio_returns_bounded_valid_wav(self):
        verify_provider(self.selection)
        audio = _generate(_FIXED_TEXT, self.selection)
        pcm, frames = _checked_wave(audio)
        self.assertLessEqual(len(audio), 500_000)
        self.assertGreater(frames, 0)
        self.assertEqual(len(pcm), frames * 2)

    def test_live_voicestudio_to_waveout_reports_backend_completion(self):
        preview = RealVoicePreview(self.selection)
        try:
            result = preview.speak(_FIXED_TEXT)
        finally:
            preview.close()
        self.assertFalse(result.text_fallback, result)
        self.assertEqual(result.received_audio, 1)
        self.assertEqual(result.playback_calls_returned, 1)
        self.assertLessEqual(result.playback_calls_returned, result.received_audio)
        self.assertEqual(result.playback_state, PlaybackState.COMPLETED)
        # A backend completion is not evidence that a person heard the audio.
        self.assertIsNone(result.user_heard)
        self.assertIsNone(result.stop_status)
        self.assertIsNone(result.stop_confirmation)
        self.assertFalse(result.stopped_acknowledged)
        self.assertFalse(result.stop_unconfirmed)
        self.assertFalse(result.voice_disabled)


class LiveEvidenceBoundaryTests(unittest.TestCase):
    """Offline checks preventing accidental live execution or overclaiming."""

    def test_live_gate_requires_exact_explicit_opt_in(self):
        for value in (None, "", "0", "true", "yes", "TRUE"):
            env = {} if value is None else {"P7_RUN_REAL_TTS_EVIDENCE": value}
            with self.subTest(value=value):
                self.assertFalse(live_opt_in_enabled(env))
        self.assertTrue(live_opt_in_enabled({"P7_RUN_REAL_TTS_EVIDENCE": "1"}))

    def test_live_module_is_success_baseline_not_failure_matrix(self):
        self.assertIn("live success baseline", __doc__)
        self.assertIn("fault proxy", __doc__)
        self.assertIn("not be simulated", __doc__)
        self.assertNotIn("failure evidence", __doc__)


if __name__ == "__main__":
    unittest.main()
