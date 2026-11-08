from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.recovery import RecoveryScanner


class RecoveryScannerTests(unittest.TestCase):
    def test_classifies_each_action_from_current_observation_not_audit_claim(self) -> None:
        scanner = RecoveryScanner()
        report = scanner.scan(
            execution_id="execution-1",
            plan_id="task-1",
            plan_version=1,
            interruption_reason="process_crash",
            last_known_at="2026-09-21T12:00:00Z",
            actions=(
                {"action_id": "action-1", "tool_name": "copy_file"},
                {"action_id": "action-2", "tool_name": "write_text_file"},
                {"action_id": "action-3", "tool_name": "move_file"},
            ),
            audit_hints={"action-1": "completed", "action-2": "completed"},
            observe=lambda action: {
                "action-1": ("completed", {"target_exists": True}),
                "action-2": ("state_uncertain", {"source_changed": True}),
                "action-3": ("not_completed", {"target_exists": False}),
            }[action["action_id"]],
        )
        self.assertEqual([item.status for item in report.actions], ["completed", "state_uncertain", "not_completed"])
        self.assertFalse(report.auto_resume_allowed)
        self.assertIn("未完成动作不会自动补做", report.render_text())

    def test_observation_failure_is_state_uncertain_and_report_is_safe(self) -> None:
        scanner = RecoveryScanner()
        report = scanner.scan(
            execution_id="execution-1",
            plan_id="task-1",
            plan_version=1,
            interruption_reason="unknown",
            last_known_at=None,
            actions=(("action-1", "copy_file"),),
            audit_hints={},
            observe=lambda action: (_ for _ in ()).throw(OSError("unreadable")),
        )
        self.assertEqual(report.actions[0].status, "state_uncertain")
        self.assertFalse(report.auto_resume_allowed)
        self.assertNotIn("unreadable", report.render_text())

    def test_scan_legacy_execution_directories_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unfinished = root / "task-1" / "versions" / "v001"
            unfinished.mkdir(parents=True)
            (unfinished / "plan.json").write_text("{}", encoding="utf-8")
            reports = RecoveryScanner().scan_legacy_plan_root(root)
            self.assertEqual(len(reports), 1)
            self.assertFalse((unfinished / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
