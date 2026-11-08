from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.evidence.ledger import (
    EvidenceEvent,
    P7EvidenceLedger,
    sha256_bytes,
    sha256_text,
)


class EvidenceLedgerTests(unittest.TestCase):
    def test_hash_helpers_match_standard_sha256(self) -> None:
        self.assertEqual(sha256_bytes(b"hello"), hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(sha256_text("你好"), hashlib.sha256("你好".encode("utf-8")).hexdigest())

    def test_for_text_requires_a_non_blank_exact_string(self) -> None:
        ledger = P7EvidenceLedger.for_text("  hello  ", evidence_kind="offline_contract")
        self.assertEqual(ledger.evidence_kind, "offline_contract")
        self.assertEqual(ledger.source_text_sha256, sha256_text("  hello  "))
        for value in ("", "   ", b"hello", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    P7EvidenceLedger.for_text(value)  # type: ignore[arg-type]

    def test_set_audio_requires_non_empty_bytes_and_records_hash(self) -> None:
        ledger = P7EvidenceLedger()
        ledger.set_audio(b"audio")
        self.assertEqual(ledger.audio_sha256, sha256_bytes(b"audio"))
        for value in (b"", bytearray(b"audio"), "audio", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ledger.set_audio(value)  # type: ignore[arg-type]

    def test_add_event_and_to_dict_are_json_serializable(self) -> None:
        ledger = P7EvidenceLedger(batch_id="p7-fixed")
        ledger.add_event("PLAYING", monotonic_seconds=1.25, source="unit-test")
        payload = ledger.to_dict()
        self.assertEqual(payload["batch_id"], "p7-fixed")
        self.assertEqual(payload["events"][0]["name"], "PLAYING")
        self.assertEqual(payload["events"][0]["monotonic_seconds"], 1.25)
        self.assertEqual(payload["events"][0]["details"], {"source": "unit-test"})
        json.dumps(payload)
        self.assertIsInstance(ledger.events[0], EvidenceEvent)

    def test_write_json_creates_parent_and_round_trips(self) -> None:
        ledger = P7EvidenceLedger.for_text("reply")
        ledger.set_audio(b"wav-bytes")
        ledger.add_event("STOP_ACKED")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "nested" / "ledger.json"
            ledger.write_json(path)
            self.assertTrue(path.is_file())
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["source_text_sha256"], sha256_text("reply"))
        self.assertEqual(written["audio_sha256"], sha256_bytes(b"wav-bytes"))
        self.assertEqual(written["events"][0]["name"], "STOP_ACKED")

    def test_derive_speaker_stop_requires_both_positive_measurements(self) -> None:
        cases = (
            (None, None, None),
            (True, None, None),
            (None, True, None),
            (False, None, False),
            (None, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        )
        for loopback, acoustic, expected in cases:
            with self.subTest(loopback=loopback, acoustic=acoustic):
                ledger = P7EvidenceLedger(
                    loopback_output_stopped=loopback,
                    acoustic_output_stopped=acoustic,
                )
                self.assertIs(ledger.derive_speaker_stop(), expected)
                self.assertIs(ledger.speaker_output_stopped, expected)

    def test_finalize_blocks_missing_evidence_and_is_idempotent(self) -> None:
        ledger = P7EvidenceLedger()
        self.assertEqual(ledger.finalize(), "blocked")
        reasons = list(ledger.blocked_reasons)
        self.assertEqual(
            reasons,
            [
                "user_heard_pending",
                "backend_stop_ack_missing",
                "speaker_output_stop_not_established",
            ],
        )
        self.assertEqual(ledger.finalize(), "blocked")
        self.assertEqual(ledger.blocked_reasons, reasons)

    def test_invalid_evidence_kind_and_non_boolean_facts_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported evidence_kind"):
            P7EvidenceLedger(evidence_kind="fake")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "backend_stop_ack"):
            P7EvidenceLedger(backend_stop_ack=1)  # type: ignore[arg-type]

    def test_finalize_completes_only_when_all_required_fields_are_explicit(self) -> None:
        ledger = P7EvidenceLedger(
            backend_stop_ack=True,
            loopback_output_stopped=True,
            acoustic_output_stopped=True,
        )
        ledger.record_user_heard(batch_id=ledger.batch_id, heard=True)
        self.assertEqual(ledger.finalize(), "complete")
        self.assertEqual(ledger.speaker_output_stopped, True)
        self.assertEqual(ledger.blocked_reasons, [])

    def test_negative_user_heard_confirmation_cannot_complete_p7(self) -> None:
        ledger = P7EvidenceLedger(
            backend_stop_ack=True,
            loopback_output_stopped=True,
            acoustic_output_stopped=True,
        )
        ledger.record_user_heard(batch_id=ledger.batch_id, heard=False)
        self.assertEqual(ledger.finalize(), "blocked")
        self.assertIn("user_heard_not_confirmed", ledger.blocked_reasons)

    def test_user_heard_requires_exact_batch_and_bool(self) -> None:
        ledger = P7EvidenceLedger(batch_id="p7-batch-1")
        with self.assertRaisesRegex(ValueError, "batch_id mismatch"):
            ledger.record_user_heard(batch_id="p7-other", heard=True)
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ledger.record_user_heard(batch_id=ledger.batch_id, heard=value)  # type: ignore[arg-type]
        ledger.record_user_heard(batch_id=ledger.batch_id, heard=True)
        self.assertTrue(ledger.user_heard)
        with self.assertRaisesRegex(ValueError, "already_recorded"):
            ledger.record_user_heard(batch_id=ledger.batch_id, heard=True)
        self.assertEqual(ledger.events[-1].name, "user_heard_confirmed")
        self.assertEqual(ledger.events[-1].details, {"batch_id": "p7-batch-1", "heard": True})


if __name__ == "__main__":
    unittest.main()