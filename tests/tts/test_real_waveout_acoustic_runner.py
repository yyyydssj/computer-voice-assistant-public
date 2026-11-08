from __future__ import annotations

import unittest

from tools.p7_real_waveout_fault_acoustic_runner import derive_acoustic_stop_flags


def summary(*, valid: bool, post_quiet: bool | None) -> dict:
    return {"endpoint": {"valid": valid, "post_quiet": post_quiet}}


class AcousticStopFlagTests(unittest.TestCase):
    def test_flags_are_derived_independently_and_speaker_is_conjunction(self) -> None:
        flags = derive_acoustic_stop_flags(
            {
                "loopback": summary(valid=True, post_quiet=True),
                "microphone": summary(valid=True, post_quiet=False),
            },
            {},
        )
        self.assertEqual(flags, (True, False, False))

    def test_both_quiet_establishes_all_stop_fields(self) -> None:
        flags = derive_acoustic_stop_flags(
            {
                "loopback": summary(valid=True, post_quiet=True),
                "microphone": summary(valid=True, post_quiet=True),
            },
            {},
        )
        self.assertEqual(flags, (True, True, True))

    def test_capture_error_fails_closed_without_erasing_endpoint_fact(self) -> None:
        flags = derive_acoustic_stop_flags(
            {
                "loopback": summary(valid=True, post_quiet=True),
                "microphone": summary(valid=True, post_quiet=True),
            },
            {"microphone": "capture_failed"},
        )
        self.assertEqual(flags, (True, False, False))

    def test_invalid_endpoint_cannot_establish_stop(self) -> None:
        flags = derive_acoustic_stop_flags(
            {
                "loopback": summary(valid=False, post_quiet=True),
                "microphone": summary(valid=True, post_quiet=True),
            },
            {},
        )
        self.assertEqual(flags, (False, True, False))


if __name__ == "__main__":
    unittest.main()
