from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.executor import (
    ApplicationSpec,
    ExecutionBackendError,
    LocalExecutionBackend,
)
from app.plans import PlanAction
from app.safety import PolicyDecision
from app.tools import build_mvp_tool_registry
from app.verification import LocalResultVerifier


def sha256_file(path: Path) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class LocalExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.backup_root = self.root / ".backups"
        self.registry = build_mvp_tool_registry()

    def action(self, tool_name: str, arguments: dict, preview: dict) -> PlanAction:
        manifest = self.registry.require(tool_name)
        decision = PolicyDecision(
            decision="require_confirmation",
            reason_code="test_approved",
            reason="test",
            policy_version="1.0.0",
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_paths=(),
        )
        return PlanAction(
            order=1,
            action_id="action-1",
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=(),
            policy_decision=decision,
            preview=preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
        )

    def backend(self, **kwargs) -> LocalExecutionBackend:
        return LocalExecutionBackend(
            allowed_root=self.root,
            backup_root=self.backup_root,
            applications=kwargs.pop("applications", {}),
            **kwargs,
        )

    def test_create_and_write_are_executed_and_reobserved(self) -> None:
        target = self.root / "notes.txt"
        create = self.action(
            "create_text_file",
            {"path": str(target), "content": "hello", "encoding": "utf-8", "create_parent": False},
            {"content_hash": "sha256:" + __import__("hashlib").sha256(b"hello").hexdigest()},
        )
        backend = self.backend()
        verifier = LocalResultVerifier(applications={})
        output = backend.execute(action=create, backup=None)
        decision = verifier.verify(action=create, attempt_id="attempt-1", output=output, backup=None, verified_at="2026-09-21T10:00:00Z")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        self.assertEqual(decision.status, "completed")

        write = self.action(
            "write_text_file",
            {"path": str(target), "mode": "append", "content": " world", "expected_source_hash": sha256_file(target)},
            {"expected_source_hash": sha256_file(target)},
        )
        backup = backend.prepare_backup(action=write, execution_id="execution-1", attempt_id="attempt-2", created_at="2026-09-21T10:00:01Z")
        self.assertIsNotNone(backup)
        self.assertEqual(Path(backup.backup_paths[0]).read_text(encoding="utf-8"), "hello")
        output = backend.execute(action=write, backup=backup)
        decision = verifier.verify(action=write, attempt_id="attempt-2", output=output, backup=backup, verified_at="2026-09-21T10:00:02Z")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")
        self.assertEqual(decision.status, "completed")

    def test_verifier_does_not_trust_success_output_after_file_is_tampered(self) -> None:
        target = self.root / "tampered.txt"
        target.write_text("before", encoding="utf-8")
        action = self.action(
            "write_text_file",
            {"path": str(target), "mode": "append", "content": "!", "expected_source_hash": sha256_file(target)},
            {"expected_source_hash": sha256_file(target)},
        )
        backend = self.backend()
        backup = backend.prepare_backup(action=action, execution_id="execution-1", attempt_id="attempt-1", created_at="2026-09-21T10:00:00Z")
        output = backend.execute(action=action, backup=backup)
        target.write_text("external-change", encoding="utf-8")
        decision = LocalResultVerifier(applications={}).verify(
            action=action, attempt_id="attempt-1", output=output, backup=backup, verified_at="2026-09-21T10:00:01Z"
        )
        self.assertEqual(decision.status, "state_uncertain")

    def test_copy_move_and_rename_use_real_files_and_hash_verification(self) -> None:
        backend = self.backend()
        verifier = LocalResultVerifier(applications={})

        source = self.root / "source.txt"
        copied = self.root / "copied.txt"
        source.write_text("payload", encoding="utf-8")
        copy = self.action("copy_file", {"source": str(source), "destination": str(copied), "recursive": False, "conflict_policy": "deny"}, {})
        copy_output = backend.execute(action=copy, backup=None)
        self.assertEqual(verifier.verify(action=copy, attempt_id="a1", output=copy_output, backup=None, verified_at="2026-09-21T10:00:00Z").status, "completed")

        moved = self.root / "moved.txt"
        move = self.action("move_file", {"source": str(copied), "destination": str(moved), "recursive": False, "conflict_policy": "deny"}, {})
        move_backup = backend.prepare_backup(action=move, execution_id="execution-1", attempt_id="a2", created_at="2026-09-21T10:00:01Z")
        move_output = backend.execute(action=move, backup=move_backup)
        self.assertEqual(verifier.verify(action=move, attempt_id="a2", output=move_output, backup=move_backup, verified_at="2026-09-21T10:00:02Z").status, "completed")
        self.assertFalse(copied.exists())

        rename = self.action("rename_file", {"path": str(moved), "new_name": "renamed.txt", "allow_extension_change": False}, {"new_path": str(self.root / "renamed.txt")})
        rename_backup = backend.prepare_backup(action=rename, execution_id="execution-1", attempt_id="a3", created_at="2026-09-21T10:00:03Z")
        rename_output = backend.execute(action=rename, backup=rename_backup)
        self.assertEqual(verifier.verify(action=rename, attempt_id="a3", output=rename_output, backup=rename_backup, verified_at="2026-09-21T10:00:04Z").status, "completed")
        self.assertEqual((self.root / "renamed.txt").read_text(encoding="utf-8"), "payload")

    def test_recycle_requires_explicit_safe_adapter_and_verifies_source_absence(self) -> None:
        target = self.root / "trash.txt"
        target.write_text("trash", encoding="utf-8")
        action = self.action("move_to_recycle_bin", {"paths": [str(target)], "recursive": False}, {})
        def unavailable_recycle(paths: tuple[Path, ...]) -> None:
            raise ExecutionBackendError("recycle_unavailable", "测试环境未注入安全回收站适配器", side_effect_may_have_occurred=False)
        blocked = self.backend(recycle_handler=unavailable_recycle)
        backup = blocked.prepare_backup(action=action, execution_id="execution-1", attempt_id="a1", created_at="2026-09-21T10:00:00Z")
        with self.assertRaises(ExecutionBackendError) as raised:
            blocked.execute(action=action, backup=backup)
        self.assertFalse(raised.exception.side_effect_may_have_occurred)
        self.assertTrue(target.exists())

        calls: list[tuple[str, ...]] = []
        def safe_recycle(paths: tuple[Path, ...]) -> None:
            calls.append(tuple(str(item) for item in paths))
            for item in paths:
                item.unlink()

        backend = self.backend(recycle_handler=safe_recycle)
        backup = backend.prepare_backup(action=action, execution_id="execution-2", attempt_id="a2", created_at="2026-09-21T10:00:01Z")
        output = backend.execute(action=action, backup=backup)
        decision = LocalResultVerifier(applications={}).verify(action=action, attempt_id="a2", output=output, backup=backup, verified_at="2026-09-21T10:00:02Z")
        self.assertEqual(len(calls), 1)
        self.assertEqual(decision.status, "completed")
        self.assertFalse(decision.facts["recoverability_confirmed"])

    def test_application_launch_uses_only_registered_executable_and_pid_probe(self) -> None:
        executable = self.root / "approved.exe"
        executable.write_bytes(b"MZ-test")
        spec = ApplicationSpec(application_id="approved.editor", application_name="Editor", executable_path=str(executable))
        launched: list[str] = []
        def launcher(value: ApplicationSpec) -> int:
            launched.append(value.executable_path)
            return 4321

        action = self.action("start_whitelisted_application", {"application_id": "approved.editor"}, {"application_id": "approved.editor"})
        backend = self.backend(applications={spec.application_id: spec}, process_launcher=launcher)
        output = backend.execute(action=action, backup=None)
        verifier = LocalResultVerifier(
            applications={spec.application_id: spec},
            process_probe=lambda pid: str(executable) if pid == 4321 else None,
        )
        decision = verifier.verify(action=action, attempt_id="a1", output=output, backup=None, verified_at="2026-09-21T10:00:00Z")
        self.assertEqual(launched, [str(executable)])
        self.assertEqual(decision.status, "completed")

        unknown = self.action("start_whitelisted_application", {"application_id": "unknown.app"}, {"application_id": "unknown.app"})
        with self.assertRaises(ExecutionBackendError) as raised:
            backend.execute(action=unknown, backup=None)
        self.assertFalse(raised.exception.side_effect_may_have_occurred)


if __name__ == "__main__":
    unittest.main()
