from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from app.runtime.lifecycle import (
    EventRecord,
    InstanceAlreadyRunningError,
    LifecycleError,
    RuntimeLifecycle,
)


class RuntimeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temp_dir.name) / "runtime" / "instance.lock"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_start_emits_uuid_and_monotonic_sequences(self) -> None:
        events: list[EventRecord] = []
        lifecycle = RuntimeLifecycle(lock_path=self.lock_path, event_sink=events.append)
        started = lifecycle.start()
        heartbeat = lifecycle.emit("runtime_ready", {"mode": "text", "provider_ready": False})
        lifecycle.close()
        self.assertEqual(started.sequence, 1)
        self.assertEqual(heartbeat.sequence, 2)
        self.assertEqual(events[0].instance_id, lifecycle.instance_id)
        self.assertEqual(len(uuid.UUID(lifecycle.instance_id).hex), 32)
        self.assertLess(started.sequence, heartbeat.sequence)

    def test_second_instance_is_rejected_until_first_closes(self) -> None:
        first = RuntimeLifecycle(lock_path=self.lock_path)
        second = RuntimeLifecycle(lock_path=self.lock_path)
        first.start()
        with self.assertRaises(InstanceAlreadyRunningError):
            second.start()
        first.close()
        second.start()
        second.close()

    def test_close_is_safe_before_start_and_idempotent_after_start(self) -> None:
        not_started = RuntimeLifecycle(lock_path=self.lock_path)
        first = not_started.close()
        second = not_started.close()
        self.assertEqual(first.event_type, "runtime_closed")
        self.assertTrue(first.details["started"] is False)
        self.assertTrue(second.details["idempotent"] is True)

        lifecycle = RuntimeLifecycle(lock_path=self.lock_path)
        lifecycle.start()
        closing = lifecycle.close()
        repeated = lifecycle.close()
        self.assertEqual(closing.event_type, "runtime_closing")
        self.assertEqual(repeated.sequence, closing.sequence)
        self.assertTrue(repeated.details["idempotent"] is True)

    def test_emit_rejects_sensitive_or_nested_details(self) -> None:
        lifecycle = RuntimeLifecycle(lock_path=self.lock_path)
        lifecycle.start()
        with self.assertRaisesRegex(LifecycleError, "敏感字段"):
            lifecycle.emit("bad_event", {"api_key": "not-real"})
        with self.assertRaisesRegex(LifecycleError, "扁平基础类型"):
            lifecycle.emit("bad_event", {"nested": {"value": 1}})
        lifecycle.close()

    def test_invalid_instance_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(LifecycleError, "UUID"):
            RuntimeLifecycle(lock_path=self.lock_path, instance_id="not-a-uuid")

    def test_emit_requires_started_lifecycle(self) -> None:
        lifecycle = RuntimeLifecycle(lock_path=self.lock_path)
        with self.assertRaisesRegex(LifecycleError, "尚未启动"):
            lifecycle.emit("runtime_ready")


if __name__ == "__main__":
    unittest.main()