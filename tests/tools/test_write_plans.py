from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.tools import (
    WritePlanError,
    build_mvp_tool_registry,
    build_write_action_preview,
)


HASH_A = "sha256:" + "a" * 64
WRITE_TOOLS = {
    "create_text_file",
    "write_text_file",
    "copy_file",
    "move_file",
    "rename_file",
    "move_to_recycle_bin",
    "start_whitelisted_application",
}


class RecordingPathValidator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, value: str, purpose: str) -> Path:
        self.calls.append((value, purpose))
        return Path(value)


class WriteActionPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.validator = RecordingPathValidator()
        self.registry = build_mvp_tool_registry()

    def build(self, tool_name: str, arguments: dict[str, object], **kwargs):
        return build_write_action_preview(
            tool_name,
            arguments,
            registry=self.registry,
            path_validator=self.validator,
            **kwargs,
        )

    def test_create_text_file_builds_hash_only_preview_without_writing(self) -> None:
        target = self.root / "nested" / "note.txt"
        proposal = self.build(
            "create_text_file",
            {
                "path": str(target),
                "content": "你好\n",
                "encoding": "utf-8",
                "create_parent": True,
            },
        )

        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())
        self.assertEqual(proposal.tool_name, "create_text_file")
        self.assertEqual(proposal.risk_level, "L2")
        self.assertTrue(proposal.requires_confirmation)
        self.assertEqual(proposal.preview["size_bytes"], len("你好\n".encode("utf-8")))
        self.assertEqual(
            proposal.preview["content_hash"],
            "sha256:" + hashlib.sha256("你好\n".encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("content", proposal.preview)
        self.assertEqual(self.validator.calls, [(str(target), "create_target")])

    def test_create_text_file_rejects_unknown_fields_bad_encoding_and_byte_limit(self) -> None:
        base = {"path": str(self.root / "a.txt"), "content": "x", "encoding": "utf-8"}
        with self.assertRaises(WritePlanError) as caught:
            self.build("create_text_file", {**base, "overwrite": True})
        self.assertEqual(caught.exception.code, "invalid_argument")

        with self.assertRaises(WritePlanError) as caught:
            self.build("create_text_file", {**base, "encoding": "utf-16"})
        self.assertEqual(caught.exception.code, "invalid_argument")

        with self.assertRaises(WritePlanError) as caught:
            self.build("create_text_file", {**base, "content": "x" * (10 * 1024 * 1024 + 1)})
        self.assertEqual(caught.exception.code, "limit_exceeded")

        with self.assertRaises(WritePlanError) as caught:
            self.build("create_text_file", {**base, "content": "😀", "encoding": "gbk"})
        self.assertEqual(caught.exception.code, "encoding_error")

    def test_write_text_file_enforces_modes_hash_and_exact_target_rules(self) -> None:
        target = self.root / "source.txt"
        append = self.build(
            "write_text_file",
            {
                "path": str(target),
                "mode": "append",
                "content": "tail",
                "expected_source_hash": HASH_A,
            },
        )
        self.assertFalse(target.exists())
        self.assertEqual(append.preview["mode"], "append")
        self.assertEqual(append.preview["expected_source_hash"], HASH_A)
        self.assertNotIn("content", append.preview)

        with self.assertRaises(WritePlanError):
            self.build(
                "write_text_file",
                {
                    "path": str(target),
                    "mode": "replace_exact",
                    "content": "new",
                    "expected_source_hash": HASH_A,
                },
            )
        with self.assertRaises(WritePlanError):
            self.build(
                "write_text_file",
                {
                    "path": str(target),
                    "mode": "append",
                    "content": "new",
                    "expected_source_hash": HASH_A,
                    "target_text": "old",
                },
            )
        exact = self.build(
            "write_text_file",
            {
                "path": str(target),
                "mode": "replace_exact",
                "content": "new",
                "expected_source_hash": HASH_A,
                "target_text": "old",
            },
        )
        self.assertEqual(exact.preview["target_text_hash"], "sha256:" + hashlib.sha256(b"old").hexdigest())

    def test_copy_and_move_preview_conflict_policy_defaults_and_same_path_rejection(self) -> None:
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        copy = self.build(
            "copy_file",
            {"source": str(source), "destination": str(destination), "conflict_policy": "deny"},
        )
        self.assertEqual(copy.preview["recursive"], False)
        self.assertEqual(copy.preview["conflict_policy"], "deny")
        self.assertFalse(source.exists())
        self.assertFalse(destination.exists())

        move = self.build(
            "move_file",
            {
                "source": str(source),
                "destination": str(destination),
                "recursive": True,
                "conflict_policy": "deny",
            },
        )
        self.assertTrue(move.preview["recursive"])
        self.assertEqual(move.preview["cross_volume_strategy"], "copy_verify_then_remove_source")

        with self.assertRaises(WritePlanError) as caught:
            self.build(
                "copy_file",
                {"source": str(source), "destination": str(source), "conflict_policy": "deny"},
            )
        self.assertEqual(caught.exception.code, "source_equals_destination")

    def test_rename_is_same_parent_rejects_illegal_names_and_controls_extension_change(self) -> None:
        source = self.root / "report.txt"
        normal = self.build("rename_file", {"path": str(source), "new_name": "final.txt"})
        self.assertEqual(Path(normal.preview["new_path"]).parent, source.parent)
        self.assertFalse(normal.preview["extension_changed"])

        with self.assertRaises(WritePlanError):
            self.build("rename_file", {"path": str(source), "new_name": "nested/final.txt"})
        with self.assertRaises(WritePlanError) as caught:
            self.build("rename_file", {"path": str(source), "new_name": "final.md"})
        self.assertEqual(caught.exception.code, "extension_change_denied")

        changed = self.build(
            "rename_file",
            {"path": str(source), "new_name": "final.md", "allow_extension_change": True},
        )
        self.assertTrue(changed.preview["extension_changed"])
        self.assertEqual(self.validator.calls[-1][1], "rename_target")

    def test_recycle_preview_is_l3_full_list_capped_and_deduplicated(self) -> None:
        paths = [str(self.root / "a.txt"), str(self.root / "b.txt")]
        proposal = self.build("move_to_recycle_bin", {"paths": paths, "recursive": False})
        self.assertEqual(proposal.risk_level, "L3")
        self.assertEqual(list(proposal.preview["paths"]), paths)
        self.assertEqual(proposal.preview["item_count"], 2)
        self.assertFalse(proposal.preview["permanent_delete"])

        with self.assertRaises(WritePlanError) as caught:
            self.build("move_to_recycle_bin", {"paths": [paths[0], paths[0]]})
        self.assertEqual(caught.exception.code, "duplicate_path")
        with self.assertRaises(WritePlanError) as caught:
            self.build("move_to_recycle_bin", {"paths": [str(self.root / f"{i}.txt") for i in range(21)]})
        self.assertEqual(caught.exception.code, "limit_exceeded")

    def test_application_preview_requires_registered_id_and_accepts_no_path_or_arguments(self) -> None:
        def resolver(application_id: str) -> str:
            if application_id == "approved.editor":
                return "Approved Editor"
            raise KeyError(application_id)

        proposal = build_write_action_preview(
            "start_whitelisted_application",
            {"application_id": "approved.editor"},
            registry=self.registry,
            application_resolver=resolver,
        )
        self.assertEqual(proposal.preview, {"application_id": "approved.editor", "application_name": "Approved Editor"})

        with self.assertRaises(WritePlanError) as caught:
            build_write_action_preview(
                "start_whitelisted_application",
                {"application_id": "unknown.app"},
                registry=self.registry,
                application_resolver=resolver,
            )
        self.assertEqual(caught.exception.code, "application_not_whitelisted")
        with self.assertRaises(WritePlanError):
            build_write_action_preview(
                "start_whitelisted_application",
                {"application_id": "approved.editor", "arguments": ["--unsafe"]},
                registry=self.registry,
                application_resolver=resolver,
            )

    def test_read_unknown_and_command_tools_never_form_write_proposals(self) -> None:
        for name in ("read_text_file", "invented_tool", "run_command", "run_controlled_powershell"):
            with self.subTest(tool=name), self.assertRaises(WritePlanError) as caught:
                self.build(name, {})
            self.assertIn(caught.exception.code, {"tool_not_plannable", "unknown_tool"})
        self.assertEqual(
            {name for name in self.registry.names() if name in WRITE_TOOLS},
            WRITE_TOOLS,
        )

    def test_path_tools_require_validator_and_preserve_validator_rejection(self) -> None:
        with self.assertRaises(TypeError):
            build_write_action_preview(
                "create_text_file",
                {"path": str(self.root / "x.txt"), "content": "x", "encoding": "utf-8"},
                registry=self.registry,
            )

        class RejectedPath(RuntimeError):
            pass

        def rejecting_validator(value: str, purpose: str) -> Path:
            raise RejectedPath(f"{purpose}:{value}")

        with self.assertRaises(RejectedPath):
            build_write_action_preview(
                "create_text_file",
                {"path": str(self.root / "x.txt"), "content": "x", "encoding": "utf-8"},
                registry=self.registry,
                path_validator=rejecting_validator,
            )

    def test_proposal_is_immutable_serializable_and_has_no_execution_surface(self) -> None:
        proposal = self.build(
            "create_text_file",
            {"path": str(self.root / "x.txt"), "content": "x", "encoding": "utf-8"},
        )
        with self.assertRaises(TypeError):
            proposal.preview["size_bytes"] = 999  # type: ignore[index]
        with self.assertRaises(TypeError):
            proposal.normalized_arguments["path"] = "changed"  # type: ignore[index]
        self.assertFalse(hasattr(proposal, "execute"))
        payload = proposal.to_dict()
        self.assertEqual(payload["tool_name"], "create_text_file")
        self.assertFalse(any(callable(value) for value in payload.values()))


if __name__ == "__main__":
    unittest.main()