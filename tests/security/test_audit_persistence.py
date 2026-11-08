from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.auditing import AuditPersistenceError, AuditStore
from app.contracts import validate_audit_chain


class AuditStoreTests(unittest.TestCase):
    def test_appends_redacted_hashed_chain_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AuditStore(Path(temp_dir), source_instance_id="instance-1")
            first = store.append(
                "execution_started",
                subject_id="execution-1",
                details={
                    "api_key": "secret-marker",
                    "path": r"C:\Users\private\notes.txt",
                    "content": "private body",
                    "safe": "ok",
                },
            )
            second = AuditStore(Path(temp_dir), source_instance_id="instance-1").append(
                "execution_finished", subject_id="execution-1", details={"safe": True}
            )
            events = AuditStore(Path(temp_dir), source_instance_id="instance-1").read_events()
            self.assertEqual([event.event_sequence for event in events], [1, 2])
            self.assertTrue(validate_audit_chain(events))
            self.assertEqual(first.event_hash, second.previous_event_hash)
            text = (Path(temp_dir) / "audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("secret-marker", text)
            self.assertNotIn("private body", text)
            self.assertNotIn("notes.txt", text)
            self.assertIn("[REDACTED]", text)

    def test_write_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AuditStore(Path(temp_dir), source_instance_id="instance-1")
            with patch("app.auditing.store.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(AuditPersistenceError):
                    store.append("execution_started", subject_id="execution-1")
            self.assertEqual((Path(temp_dir) / "audit.jsonl").read_text(encoding="utf-8"), "")

    def test_tampered_chain_is_rejected_on_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AuditStore(Path(temp_dir), source_instance_id="instance-1")
            store.append("execution_started", subject_id="execution-1")
            path = Path(temp_dir) / "audit.jsonl"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["subject_id"] = "execution-2"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaises(AuditPersistenceError):
                store.read_events()


if __name__ == "__main__":
    unittest.main()
