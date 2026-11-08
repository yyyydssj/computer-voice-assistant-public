from __future__ import annotations

import os
import stat
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from app.safety.path_guard import (
    DEFAULT_ALLOWED_ROOT,
    DEFAULT_WORKING_DIRECTORY,
    PathGuard,
    PathGuardError,
    PathSnapshot,
)


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class FakeStat:
    def __init__(
        self,
        identity: tuple[int, int],
        *,
        directory: bool = True,
        reparse: bool = False,
    ) -> None:
        self.st_dev, self.st_ino = identity
        self.st_mode = stat.S_IFDIR if directory else stat.S_IFREG
        self.st_file_attributes = FILE_ATTRIBUTE_REPARSE_POINT if reparse else 0


class FakeProbe:
    def __init__(self) -> None:
        self.entries: dict[str, FakeStat | BaseException] = {}
        self.real_paths: dict[str, str | BaseException] = {}
        self.drive_kinds: dict[str, str] = {"G:\\": "fixed"}

    @staticmethod
    def key(path: str) -> str:
        return path.replace("/", "\\").rstrip("\\").casefold() or "\\"

    def add(
        self,
        path: str,
        identity: tuple[int, int],
        *,
        directory: bool = True,
        reparse: bool = False,
        real_path: str | None = None,
    ) -> None:
        key = self.key(path)
        self.entries[key] = FakeStat(identity, directory=directory, reparse=reparse)
        self.real_paths[key] = real_path or path

    def lstat(self, path: str) -> FakeStat:
        value = self.entries.get(self.key(path), FileNotFoundError(path))
        if isinstance(value, BaseException):
            raise value
        return value

    def realpath(self, path: str) -> str:
        value = self.real_paths.get(self.key(path), path)
        if isinstance(value, BaseException):
            raise value
        return value

    def drive_kind(self, drive_root: str) -> str:
        return self.drive_kinds.get(drive_root.upper(), "unknown")


def configured_probe() -> FakeProbe:
    probe = FakeProbe()
    probe.add("G:\\", (1, 1))
    probe.add(r"G:\study", (1, 2))
    probe.add(r"G:\study\AI工作区", (1, 3))
    probe.add(r"G:\study\project", (1, 4))
    probe.add(r"G:\study\project\source.txt", (1, 5), directory=False)
    return probe


class PathGuardNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = configured_probe()
        self.guard = PathGuard(probe=self.probe, environment={"SAFE_SUBDIR": "project"})

    def assert_code(self, code: str, value: str) -> None:
        with self.assertRaises(PathGuardError) as caught:
            self.guard(value, "file")
        self.assertEqual(caught.exception.code, code)

    def test_defaults_are_frozen_and_relative_paths_use_default_workspace(self) -> None:
        self.assertEqual(DEFAULT_ALLOWED_ROOT, r"G:\study")
        self.assertEqual(DEFAULT_WORKING_DIRECTORY, r"G:\study\AI工作区")
        resolved = self.guard(r"notes\todo.txt", "create_target")
        self.assertEqual(str(resolved), r"G:\study\AI工作区\notes\todo.txt")

    def test_normalizes_dotdot_slashes_case_and_environment_without_current_directory(self) -> None:
        resolved = self.guard(r"%SAFE_SUBDIR%/draft/../NOTE.txt", "create_target")
        self.assertEqual(str(resolved), r"G:\study\AI工作区\project\NOTE.txt")
        explicit = self.guard(r"g:/STUDY/project/source.txt", "file")
        self.assertEqual(str(explicit).casefold(), r"g:\study\project\source.txt".casefold())

    def test_rejects_unc_device_other_drive_drive_relative_and_rooted_paths(self) -> None:
        for value in (
            r"\\server\share\x.txt",
            r"\\?\G:\study\x.txt",
            r"\\.\G:\study\x.txt",
        ):
            with self.subTest(value=value):
                self.assert_code("unc_path_denied", value)
        self.assert_code("drive_not_allowed", r"C:\temp\x.txt")
        self.assert_code("ambiguous_path", r"G:relative.txt")
        self.assert_code("ambiguous_path", r"\study\x.txt")

    def test_rejects_unresolved_environment_short_name_ads_and_outside_traversal(self) -> None:
        self.assert_code("unresolved_environment_variable", r"%UNKNOWN%\x.txt")
        self.assert_code("short_name_denied", r"G:\study\PROGRA~1\x.txt")
        self.assert_code("ambiguous_path", r"G:\study\file.txt:secret")
        self.assert_code("ambiguous_path", "G:\\study\\project. \\x.txt")
        self.assert_code("path_outside_allowed_root", r"G:\study\..\outside.txt")

    def test_rejects_mapped_drive_even_when_surface_path_is_under_allowed_root(self) -> None:
        self.probe.drive_kinds["G:\\"] = "remote"
        self.assert_code("mapped_drive_denied", r"G:\study\project\source.txt")


class PathGuardRealPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = configured_probe()
        self.guard = PathGuard(probe=self.probe, environment={})

    def test_rejects_reparse_point_in_any_existing_component(self) -> None:
        self.probe.add(r"G:\study\project", (1, 4), reparse=True)
        with self.assertRaises(PathGuardError) as caught:
            self.guard(r"G:\study\project\source.txt", "file")
        self.assertEqual(caught.exception.code, "reparse_point_denied")
        self.assertEqual(caught.exception.path, r"G:\study\project")

    def test_rejects_real_target_outside_root_and_resolution_permission_failure(self) -> None:
        self.probe.real_paths[self.probe.key(r"G:\study\project\source.txt")] = r"C:\secret\source.txt"
        with self.assertRaises(PathGuardError) as caught:
            self.guard.snapshot(r"G:\study\project\source.txt", "file")
        self.assertEqual(caught.exception.code, "resolved_path_outside_allowed_root")

        self.probe.real_paths[self.probe.key(r"G:\study\project\source.txt")] = r"G:\\"
        with self.assertRaises(PathGuardError) as caught:
            self.guard.snapshot(r"G:\study\project\source.txt", "file")
        self.assertEqual(caught.exception.code, "resolved_path_outside_allowed_root")

        self.probe.real_paths[self.probe.key(r"G:\study\project\source.txt")] = PermissionError("denied")
        with self.assertRaises(PathGuardError) as caught:
            self.guard.snapshot(r"G:\study\project\source.txt", "file")
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_nonexistent_target_anchors_to_nearest_existing_parent(self) -> None:
        snapshot = self.guard.snapshot(r"G:\study\project\new\note.txt", "create_target")
        self.assertFalse(snapshot.exists)
        self.assertEqual(snapshot.anchor_path, r"G:\study\project")
        self.assertEqual(snapshot.anchor_identity, (1, 4))
        self.assertEqual(snapshot.resolved_path, r"G:\study\project\new\note.txt")


class PathGuardRevalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = configured_probe()
        self.guard = PathGuard(probe=self.probe, environment={})

    def test_snapshot_is_immutable_serializable_and_revalidates_unchanged_state(self) -> None:
        snapshot = self.guard.snapshot(r"G:\study\project\source.txt", "copy_source")
        self.assertIsInstance(snapshot, PathSnapshot)
        with self.assertRaises(FrozenInstanceError):
            snapshot.exists = False  # type: ignore[misc]
        self.assertEqual(snapshot.to_dict()["target_identity"], [1, 5])
        refreshed = self.guard.revalidate(snapshot)
        self.assertEqual(refreshed, snapshot)

    def test_revalidation_rejects_target_disappearance_appearance_and_identity_change(self) -> None:
        existing = self.guard.snapshot(r"G:\study\project\source.txt", "move_source")
        self.probe.entries[self.probe.key(r"G:\study\project\source.txt")] = FileNotFoundError("gone")
        with self.assertRaises(PathGuardError) as caught:
            self.guard.revalidate(existing)
        self.assertEqual(caught.exception.code, "target_disappeared")

        missing = self.guard.snapshot(r"G:\study\project\new.txt", "copy_target")
        self.probe.add(r"G:\study\project\new.txt", (1, 8), directory=False)
        with self.assertRaises(PathGuardError) as caught:
            self.guard.revalidate(missing)
        self.assertEqual(caught.exception.code, "target_appeared")

        self.probe = configured_probe()
        self.guard = PathGuard(probe=self.probe, environment={})
        stable = self.guard.snapshot(r"G:\study\project\source.txt", "copy_source")
        self.probe.add(r"G:\study\project\source.txt", (1, 99), directory=False)
        with self.assertRaises(PathGuardError) as caught:
            self.guard.revalidate(stable)
        self.assertEqual(caught.exception.code, "path_identity_changed")

    def test_revalidation_rejects_link_replacement_parent_race_and_permission_loss(self) -> None:
        target = self.guard.snapshot(r"G:\study\project\source.txt", "write_target")
        self.probe.add(r"G:\study\project", (1, 4), reparse=True)
        with self.assertRaises(PathGuardError) as caught:
            self.guard.revalidate(target)
        self.assertEqual(caught.exception.code, "reparse_point_denied")

        self.probe = configured_probe()
        self.guard = PathGuard(probe=self.probe, environment={})
        missing = self.guard.snapshot(r"G:\study\project\future\x.txt", "create_target")
        self.probe.add(r"G:\study\project", (1, 44))
        with self.assertRaises(PathGuardError) as caught:
            self.guard.revalidate(missing)
        self.assertEqual(caught.exception.code, "anchor_identity_changed")

        self.probe = configured_probe()
        self.guard = PathGuard(probe=self.probe, environment={})
        existing = self.guard.snapshot(r"G:\study\project\source.txt", "file")
        self.probe.entries[self.probe.key(r"G:\study\project")] = PermissionError("denied")
        with self.assertRaises(PathGuardError) as caught:
            self.guard.revalidate(existing)
        self.assertEqual(caught.exception.code, "permission_denied")


if __name__ == "__main__":
    unittest.main()