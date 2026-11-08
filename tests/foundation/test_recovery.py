from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtime.recovery import detect_unfinished_tasks


class RecoveryDetectionTests(unittest.TestCase):
    def test_detects_plan_without_result_as_non_resumable_clue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plans_root = Path(temp_dir) / "plans"
            version = plans_root / "task-1" / "versions" / "v001"
            version.mkdir(parents=True)
            (version / "plan.json").write_text("{}\n", encoding="utf-8")

            summary = detect_unfinished_tasks(plans_root)

            self.assertEqual(summary.possible_unfinished_count, 1)
            self.assertFalse(summary.auto_resume_allowed)

    def test_completed_plan_and_unrelated_json_are_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plans_root = Path(temp_dir) / "plans"
            completed = plans_root / "task-1" / "versions" / "v001"
            completed.mkdir(parents=True)
            (completed / "plan.json").write_text("{}\n", encoding="utf-8")
            (completed / "result.json").write_text("{}\n", encoding="utf-8")
            unrelated = plans_root / "notes"
            unrelated.mkdir()
            (unrelated / "plan.json").write_text("{}\n", encoding="utf-8")

            summary = detect_unfinished_tasks(plans_root)

            self.assertEqual(summary.possible_unfinished_count, 0)
            self.assertFalse(summary.auto_resume_allowed)

    def test_missing_plans_directory_is_safe_and_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plans_root = Path(temp_dir) / "missing-plans"

            summary = detect_unfinished_tasks(plans_root)

            self.assertEqual(summary.possible_unfinished_count, 0)
            self.assertFalse(summary.auto_resume_allowed)
            self.assertFalse(plans_root.exists())


if __name__ == "__main__":
    unittest.main()
