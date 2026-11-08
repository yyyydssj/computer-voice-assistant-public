from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from app.tools import (
    ManifestError,
    ToolManifest,
    ToolRegistry,
    build_mvp_tool_registry,
)


EXPECTED_TOOL_NAMES = (
    "list_directory",
    "read_text_file",
    "search_files",
    "get_file_info",
    "get_system_info",
    "get_processes",
    "create_text_file",
    "write_text_file",
    "copy_file",
    "move_file",
    "rename_file",
    "move_to_recycle_bin",
    "start_whitelisted_application",
)


class ToolManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()

    def test_registry_contains_exactly_the_thirteen_approved_mvp_tools(self) -> None:
        self.assertEqual(self.registry.names(), EXPECTED_TOOL_NAMES)
        self.assertNotIn("run_controlled_powershell", self.registry.names())
        self.assertNotIn("terminate_process", self.registry.names())
        self.assertNotIn("run_command", self.registry.names())

    def test_every_manifest_declares_the_complete_review_contract(self) -> None:
        for manifest in self.registry.manifests():
            with self.subTest(tool=manifest.name):
                self.assertRegex(manifest.name, r"^[a-z][a-z0-9_]{0,63}$")
                self.assertRegex(manifest.version, r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
                self.assertTrue(manifest.description)
                self.assertEqual(manifest.input_schema["type"], "object")
                self.assertIs(manifest.input_schema["additionalProperties"], False)
                self.assertEqual(manifest.output_schema["type"], "object")
                self.assertIs(manifest.output_schema["additionalProperties"], False)
                self.assertIn(manifest.risk_level, {"L0", "L1", "L2", "L3"})
                self.assertGreater(manifest.timeout_ms, 0)
                self.assertGreater(manifest.max_output_bytes, 0)
                self.assertIn(manifest.idempotency, {"idempotent", "conditional", "non_idempotent"})
                self.assertIn(manifest.cancellation, {"not_supported", "cooperative"})
                self.assertTrue(manifest.preview_method)
                self.assertTrue(manifest.verification_method)
                self.assertTrue(manifest.rollback_strategy)
                self.assertTrue(manifest.audit_event_type)
                self.assertRegex(manifest.content_hash, r"^sha256:[0-9a-f]{64}$")
                self.assertFalse(hasattr(manifest, "execute"))
                self.assertFalse(any(callable(value) for value in manifest.to_dict().values()))

    def test_manifest_hash_is_stable_and_covers_security_fields(self) -> None:
        original = self.registry.require("read_text_file")
        restored = ToolManifest.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.content_hash, original.content_hash)
        changed = replace(original, max_output_bytes=original.max_output_bytes + 1)
        self.assertNotEqual(changed.content_hash, original.content_hash)

    def test_registry_and_manifest_are_immutable(self) -> None:
        manifest = self.registry.require("list_directory")
        with self.assertRaises(FrozenInstanceError):
            manifest.risk_level = "L3"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            manifest.input_schema["type"] = "string"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            self.registry._manifests = ()  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            self.registry._by_name = {}  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            self.registry.register(manifest)  # type: ignore[attr-defined]

    def test_risk_levels_and_confirmation_requirements_match_the_approved_table(self) -> None:
        expected = {
            "list_directory": ("L0", False),
            "read_text_file": ("L1", False),
            "search_files": ("L1", False),
            "get_file_info": ("L0", False),
            "get_system_info": ("L1", False),
            "get_processes": ("L1", False),
            "create_text_file": ("L2", True),
            "write_text_file": ("L2", True),
            "copy_file": ("L2", True),
            "move_file": ("L2", True),
            "rename_file": ("L2", True),
            "move_to_recycle_bin": ("L3", True),
            "start_whitelisted_application": ("L2", True),
        }
        self.assertEqual(
            {item.name: (item.risk_level, item.requires_confirmation) for item in self.registry.manifests()},
            expected,
        )

    def test_file_tools_declare_only_the_approved_path_scope(self) -> None:
        non_path_tools = {"get_system_info", "get_processes", "start_whitelisted_application"}
        for manifest in self.registry.manifests():
            with self.subTest(tool=manifest.name):
                if manifest.name in non_path_tools:
                    self.assertEqual(manifest.allowed_path_scopes, ())
                else:
                    self.assertEqual(manifest.allowed_path_scopes, (r"G:\study",))
                    self.assertEqual(manifest.default_working_directory, r"G:\study\AI工作区")

    def test_global_limits_are_encoded_in_the_relevant_manifests(self) -> None:
        self.assertEqual(self.registry.require("list_directory").limits["max_items"], 200)
        self.assertEqual(self.registry.require("search_files").limits["max_matches"], 100)
        self.assertEqual(self.registry.require("search_files").limits["max_depth"], 5)
        self.assertEqual(self.registry.require("read_text_file").limits["max_file_bytes"], 10 * 1024 * 1024)
        self.assertEqual(self.registry.require("create_text_file").limits["max_file_bytes"], 10 * 1024 * 1024)
        self.assertEqual(self.registry.require("move_to_recycle_bin").limits["max_items"], 20)
        self.assertEqual(self.registry.require("copy_file").limits["overwrite"], "deny")
        self.assertEqual(self.registry.require("move_file").limits["overwrite"], "deny")

    def test_registry_rejects_unknown_tools_and_duplicate_names(self) -> None:
        with self.assertRaisesRegex(ManifestError, "未注册工具"):
            self.registry.require("run_controlled_powershell")
        first = self.registry.require("list_directory")
        unapproved = replace(first, name="invented_tool")
        with self.assertRaisesRegex(ManifestError, "未批准工具"):
            ToolRegistry((unapproved,))

        with self.assertRaisesRegex(ManifestError, "重复工具"):
            ToolRegistry((first, first))

    def test_manifest_rejects_l4_invalid_confirmation_and_open_schemas(self) -> None:
        valid = self.registry.require("list_directory").to_dict()
        valid.pop("content_hash")

        with self.assertRaisesRegex(ManifestError, "L4"):
            ToolManifest.from_dict({**valid, "risk_level": "L4"})
        with self.assertRaisesRegex(ManifestError, "确认"):
            ToolManifest.from_dict({**valid, "risk_level": "L2", "requires_confirmation": False})
        with self.assertRaisesRegex(ManifestError, "additionalProperties"):
            ToolManifest.from_dict({
                **valid,
                "input_schema": {**valid["input_schema"], "additionalProperties": True},
            })

    def test_manifest_rejects_unknown_fields_hash_forgery_and_command_surface(self) -> None:
        valid = self.registry.require("list_directory").to_dict()
        with self.assertRaisesRegex(ManifestError, "未知字段"):
            ToolManifest.from_dict({**valid, "executor": "powershell"})
        with self.assertRaisesRegex(ManifestError, "content_hash"):
            ToolManifest.from_dict({**valid, "content_hash": "sha256:" + "0" * 64})
        command_like = {**valid, "name": "run_command"}
        command_like.pop("content_hash")
        with self.assertRaisesRegex(ManifestError, "禁止"):
            ToolManifest.from_dict(command_like)


if __name__ == "__main__":
    unittest.main()
