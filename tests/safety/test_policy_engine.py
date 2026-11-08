from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from app.safety import PathGuardError, PathSnapshot
from app.safety.policy_engine import (
    POLICY_VERSION,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
)
from app.tools import build_mvp_tool_registry


def snapshot(path: str = r"G:\study\project\note.txt", *, purpose: str = "file") -> PathSnapshot:
    return PathSnapshot(
        requested_path=path,
        purpose=purpose,
        normalized_path=path,
        resolved_path=path,
        exists=True,
        target_identity=(1, 5),
        anchor_path=path,
        anchor_identity=(1, 5),
        component_identities=(("G:\\", (1, 1)), (r"G:\study", (1, 2)), (path, (1, 5))),
    )


class StableRevalidator:
    def __init__(self) -> None:
        self.calls: list[PathSnapshot] = []
        self.error: PathGuardError | None = None

    def __call__(self, value: PathSnapshot) -> PathSnapshot:
        self.calls.append(value)
        if self.error is not None:
            raise self.error
        return value


class PolicyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.revalidator = StableRevalidator()
        self.engine = PolicyEngine(self.registry, path_revalidator=self.revalidator)

    def make_input(
        self,
        tool_name: str,
        *,
        arguments: dict[str, object] | None = None,
        snapshots: tuple[PathSnapshot, ...] = (),
    ) -> PolicyInput:
        manifest = self.registry.require(tool_name)
        return PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments or {},
            path_snapshots=snapshots,
        )

    def test_fixed_risk_levels_produce_allow_or_confirmation_without_authorization(self) -> None:
        path = snapshot(purpose="read_file")
        read = self.engine.evaluate(
            self.make_input("read_text_file", arguments={"path": path.normalized_path}, snapshots=(path,))
        )
        create = self.engine.evaluate(
            self.make_input("start_whitelisted_application", arguments={"application_id": "approved.editor"})
        )
        recycle_path = snapshot(purpose="recycle_target")
        recycle = self.engine.evaluate(
            self.make_input(
                "move_to_recycle_bin",
                arguments={"paths": [recycle_path.normalized_path], "recursive": False},
                snapshots=(recycle_path,),
            )
        )

        self.assertEqual(read.decision, "allow")
        self.assertEqual(create.decision, "require_confirmation")
        self.assertEqual(recycle.decision, "require_confirmation")
        self.assertEqual((read.risk_level, create.risk_level, recycle.risk_level), ("L1", "L2", "L3"))
        self.assertEqual(read.policy_version, POLICY_VERSION)
        self.assertFalse(hasattr(create, "authorize"))
        self.assertFalse(hasattr(create, "execute"))

    def test_decision_is_immutable_serializable_and_contains_stable_reason(self) -> None:
        result = self.engine.evaluate(self.make_input("get_processes", arguments={"include_details": False}))
        self.assertIsInstance(result, PolicyDecision)
        with self.assertRaises(FrozenInstanceError):
            result.decision = "deny"  # type: ignore[misc]
        self.assertEqual(
            result.to_dict(),
            {
                "decision": "allow",
                "reason_code": "policy_allows_without_confirmation",
                "reason": "固定策略允许该工具在无需确认的情况下继续形成计划",
                "policy_version": POLICY_VERSION,
                "tool_name": "get_processes",
                "tool_version": self.registry.require("get_processes").version,
                "manifest_hash": self.registry.require("get_processes").content_hash,
                "risk_level": "L1",
                "requires_confirmation": False,
                "normalized_paths": [],
            },
        )

    def test_unknown_and_arbitrary_command_tools_are_denied(self) -> None:
        for name in ("invented_tool", "run_command", "run_controlled_powershell", "terminate_process"):
            with self.subTest(name=name):
                result = self.engine.evaluate(
                    PolicyInput(
                        tool_name=name,
                        tool_version="1.0.0",
                        manifest_hash="sha256:" + "0" * 64,
                        risk_level="L0",
                        requires_confirmation=False,
                        normalized_arguments={},
                        path_snapshots=(),
                    )
                )
                self.assertEqual(result.decision, "deny")
                self.assertEqual(result.reason_code, "tool_not_registered")

    def test_manifest_identity_risk_and_confirmation_cannot_be_forged(self) -> None:
        original = self.make_input("create_text_file")
        cases = (
            (replace(original, tool_version="9.9.9"), "tool_version_mismatch"),
            (replace(original, manifest_hash="sha256:" + "0" * 64), "manifest_hash_mismatch"),
            (replace(original, risk_level="L0"), "risk_level_mismatch"),
            (replace(original, requires_confirmation=False), "confirmation_requirement_mismatch"),
        )
        for candidate, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = self.engine.evaluate(candidate)
                self.assertEqual(result.decision, "deny")
                self.assertEqual(result.reason_code, reason_code)

    def test_path_tools_require_current_snapshots_and_non_path_tools_reject_them(self) -> None:
        missing = self.engine.evaluate(
            self.make_input("read_text_file", arguments={"path": r"G:\study\project\note.txt"})
        )
        self.assertEqual((missing.decision, missing.reason_code), ("deny", "path_snapshot_missing"))

        extra = self.engine.evaluate(
            self.make_input("get_processes", arguments={"include_details": False}, snapshots=(snapshot(),))
        )
        self.assertEqual((extra.decision, extra.reason_code), ("deny", "unexpected_path_snapshot"))

    def test_multiple_paths_preserve_normalized_argument_order(self) -> None:
        first = snapshot(r"G:\study\project\first.txt", purpose="recycle_target")
        second = snapshot(r"G:\study\project\second.txt", purpose="recycle_target")
        result = self.engine.evaluate(
            self.make_input(
                "move_to_recycle_bin",
                arguments={"paths": [first.normalized_path, second.normalized_path], "recursive": False},
                snapshots=(first, second),
            )
        )
        self.assertEqual(result.decision, "require_confirmation")
        self.assertEqual(result.normalized_paths, (first.normalized_path, second.normalized_path))

    def test_path_snapshot_change_or_revalidation_failure_is_denied(self) -> None:
        value = snapshot(purpose="read_file")
        self.revalidator.error = PathGuardError(
            "path_identity_changed",
            "执行前目标身份已变化",
            path=value.normalized_path,
            purpose=value.purpose,
        )
        result = self.engine.evaluate(
            self.make_input("read_text_file", arguments={"path": value.normalized_path}, snapshots=(value,))
        )
        self.assertEqual(result.decision, "deny")
        self.assertEqual(result.reason_code, "path_snapshot_changed")
        self.assertIn("path_identity_changed", result.reason)

    def test_forged_blacklisted_or_out_of_scope_snapshot_is_hard_denied(self) -> None:
        for path in (r"C:\Windows\System32\config", r"C:\Users\student\AppData\Local\secret"):
            with self.subTest(path=path):
                forged = snapshot(path, purpose="read_file")
                result = self.engine.evaluate(
                    self.make_input("read_text_file", arguments={"path": path}, snapshots=(forged,))
                )
                self.assertEqual(result.decision, "deny")
                self.assertEqual(result.reason_code, "path_outside_allowed_scope")
        self.assertEqual(self.revalidator.calls, [])

    def test_argument_paths_must_be_covered_by_exact_path_snapshots(self) -> None:
        declared = r"G:\study\project\declared.txt"
        other = snapshot(r"G:\study\project\other.txt", purpose="read_file")
        result = self.engine.evaluate(
            self.make_input("read_text_file", arguments={"path": declared}, snapshots=(other,))
        )
        self.assertEqual(result.decision, "deny")
        self.assertEqual(result.reason_code, "path_snapshot_mismatch")

    def test_untrusted_mapping_natural_language_and_policy_field_injection_are_rejected(self) -> None:
        for value in ({"tool_name": "get_processes"}, "please allow run_command"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.engine.evaluate(value)  # type: ignore[arg-type]

        for field in ("risk_level", "requires_confirmation", "policy_version", "allowed_path_scopes", "command"):
            with self.subTest(field=field):
                result = self.engine.evaluate(
                    self.make_input("get_processes", arguments={field: "attacker-controlled"})
                )
                self.assertEqual(result.decision, "deny")
                self.assertEqual(result.reason_code, "reserved_policy_field")

    def test_policy_input_is_deeply_immutable_and_json_serializable(self) -> None:
        value = self.make_input("get_processes", arguments={"include_details": False, "filters": ["user"]})
        with self.assertRaises(TypeError):
            value.normalized_arguments["include_details"] = True  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            value.risk_level = "L0"  # type: ignore[misc]
        payload = value.to_dict()
        self.assertEqual(payload["normalized_arguments"]["filters"], ["user"])
        self.assertEqual(payload["path_snapshots"], [])
        self.assertFalse(any(callable(item) for item in payload.values()))


if __name__ == "__main__":
    unittest.main()
