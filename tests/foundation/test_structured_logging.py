from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.logging.structured import StructuredLogError, StructuredLogger


class StructuredLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name) / "logs"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_initializes_directory_and_writes_jsonl(self) -> None:
        logger = StructuredLogger(self.log_dir)
        record = logger.write(
            "runtime_started",
            instance_id="instance-1",
            event_sequence=1,
            details={"mode": "text", "provider_ready": False},
        )
        logger.close()
        self.assertTrue(self.log_dir.is_dir())
        lines = (self.log_dir / "runtime.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), record)

    def test_redacts_secrets_and_omits_audio_or_file_content(self) -> None:
        logger = StructuredLogger(self.log_dir)
        logger.write(
            "config_checked",
            instance_id="instance-1",
            event_sequence=1,
            details={
                "api_key": "not-real-marker",
                "authorization": "Bearer marker-value",
                "audio_path": Path("C:/private/test.wav"),
                "file_content": "private document text",
                "normal": "safe",
            },
        )
        record = json.loads((self.log_dir / "runtime.jsonl").read_text(encoding="utf-8"))
        details = record["details"]
        self.assertEqual(details["api_key"], "[REDACTED]")
        self.assertEqual(details["authorization"], "[REDACTED]")
        self.assertEqual(details["audio_path"], "[OMITTED]")
        self.assertEqual(details["file_content"], "[OMITTED]")
        self.assertEqual(details["normal"], "safe")
        self.assertNotIn("private document text", json.dumps(record))
        self.assertNotIn("test.wav", json.dumps(record))

    def test_rejects_invalid_event_and_write_after_close(self) -> None:
        logger = StructuredLogger(self.log_dir)
        with self.assertRaisesRegex(StructuredLogError, "事件类型"):
            logger.write(
                "bad event",
                instance_id="instance-1",
                event_sequence=1,
            )
        logger.close()
        with self.assertRaisesRegex(StructuredLogError, "已经关闭"):
            logger.write(
                "runtime_ready",
                instance_id="instance-1",
                event_sequence=1,
            )

    def test_rejects_nested_non_mapping_values_only_by_sanitizing(self) -> None:
        logger = StructuredLogger(self.log_dir)
        logger.write(
            "runtime_ready",
            instance_id="instance-1",
            event_sequence=1,
            details={"values": ["a", {"nested": "b"}]},
        )
        record = json.loads((self.log_dir / "runtime.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["details"]["values"][1]["nested"], "b")


if __name__ == "__main__":
    unittest.main()