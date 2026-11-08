from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tools.read_only import (
    DirectoryItem,
    ProcessSnapshot,
    ReadOnlyToolError,
    get_processes,
    list_directory,
    read_text_file,
    search_files,
)


class StepClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self._last = values[-1] if values else 0.0

    def __call__(self) -> float:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


def allowing_path_validator(raw_path: str, expected_kind: str) -> Path:
    path = Path(raw_path)
    if expected_kind == "file" and not path.is_file():
        raise ReadOnlyToolError("not_found", "文件不存在", path=raw_path)
    if expected_kind == "directory" and not path.is_dir():
        raise ReadOnlyToolError("not_found", "目录不存在", path=raw_path)
    return path


class ReadOnlyFileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_path_validator_is_mandatory_and_denial_is_preserved(self) -> None:
        target = self.root / "note.txt"
        target.write_text("hello", encoding="utf-8")
        with self.assertRaises(TypeError):
            read_text_file(str(target))  # type: ignore[call-arg]

        def deny(raw_path: str, expected_kind: str) -> Path:
            raise ReadOnlyToolError("path_out_of_scope", "路径越界", path=raw_path)

        with self.assertRaises(ReadOnlyToolError) as caught:
            read_text_file(str(target), path_validator=deny)
        self.assertEqual(caught.exception.code, "path_out_of_scope")

    def test_list_directory_hides_hidden_items_sorts_and_caps_results(self) -> None:
        (self.root / "b.txt").write_text("bb", encoding="utf-8")
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / ".secret").write_text("hidden", encoding="utf-8")
        (self.root / "folder").mkdir()

        result = list_directory(
            str(self.root),
            sort_by="name",
            max_items=2,
            path_validator=allowing_path_validator,
        )

        self.assertEqual([item.relative_path for item in result.items], ["a.txt", "b.txt"])
        self.assertTrue(result.truncated)
        self.assertTrue(all(isinstance(item, DirectoryItem) for item in result.items))
        self.assertNotIn(".secret", [item.name for item in result.items])
        self.assertEqual(set(result.to_dict()), {"items", "truncated"})

    def test_list_directory_recursive_depth_limit_and_permission_error(self) -> None:
        deep = self.root / "one" / "two" / "three"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("x", encoding="utf-8")

        result = list_directory(
            str(self.root),
            recursive=True,
            max_depth=2,
            path_validator=allowing_path_validator,
        )
        paths = {item.relative_path for item in result.items}
        self.assertIn("one/two", paths)
        self.assertNotIn("one/two/three", paths)

        with patch("app.tools.read_only.os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(ReadOnlyToolError) as caught:
                list_directory(str(self.root), path_validator=allowing_path_validator)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_read_text_file_supports_utf8_gbk_ranges_and_empty_files(self) -> None:
        utf8 = self.root / "utf8.txt"
        utf8.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
        ranged = read_text_file(
            str(utf8),
            start_line=2,
            max_lines=1,
            path_validator=allowing_path_validator,
        )
        self.assertEqual(ranged.text, "第二行\n")
        self.assertEqual(ranged.encoding, "utf-8")
        self.assertTrue(ranged.truncated)

        gbk = self.root / "gbk.txt"
        gbk.write_bytes("中文内容".encode("gbk"))
        decoded = read_text_file(str(gbk), path_validator=allowing_path_validator)
        self.assertEqual(decoded.text, "中文内容")
        self.assertEqual(decoded.encoding, "gbk")

        empty = self.root / "empty.txt"
        empty.write_bytes(b"")
        empty_result = read_text_file(str(empty), path_validator=allowing_path_validator)
        self.assertEqual(empty_result.text, "")
        self.assertEqual(empty_result.size_bytes, 0)
        self.assertFalse(empty_result.truncated)

    def test_read_text_file_rejects_binary_oversize_and_output_overflow(self) -> None:
        binary = self.root / "binary.bin"
        binary.write_bytes(b"abc\x00def")
        with self.assertRaises(ReadOnlyToolError) as binary_error:
            read_text_file(str(binary), path_validator=allowing_path_validator)
        self.assertEqual(binary_error.exception.code, "binary_file")

        large = self.root / "large.txt"
        large.write_bytes(b"x" * 11)
        with self.assertRaises(ReadOnlyToolError) as size_error:
            read_text_file(
                str(large),
                max_file_bytes=10,
                path_validator=allowing_path_validator,
            )
        self.assertEqual(size_error.exception.code, "file_too_large")

        output = self.root / "output.txt"
        output.write_text("123456789", encoding="utf-8")
        limited = read_text_file(
            str(output),
            max_output_bytes=5,
            path_validator=allowing_path_validator,
        )
        self.assertLessEqual(len(limited.text.encode("utf-8")), 5)
        self.assertTrue(limited.truncated)

    def test_search_files_supports_name_extension_and_content_modes(self) -> None:
        (self.root / "Alpha.TXT").write_text("needle on line one\n", encoding="utf-8")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "beta.md").write_text("prefix NEEDLE suffix\n", encoding="utf-8")
        (nested / "image.bin").write_bytes(b"a\x00needle")

        by_name = search_files(
            str(self.root), "alpha", mode="name", recursive=True,
            path_validator=allowing_path_validator,
        )
        self.assertEqual([match.relative_path for match in by_name.matches], ["Alpha.TXT"])

        by_extension = search_files(
            str(self.root), ".md", mode="extension", recursive=True,
            path_validator=allowing_path_validator,
        )
        self.assertEqual([match.relative_path for match in by_extension.matches], ["nested/beta.md"])

        by_content = search_files(
            str(self.root), "needle", mode="content", recursive=True,
            path_validator=allowing_path_validator,
        )
        self.assertEqual(
            [match.relative_path for match in by_content.matches],
            ["Alpha.TXT", "nested/beta.md"],
        )
        self.assertEqual(by_content.matches[0].line_number, 1)
        self.assertNotIn("image.bin", [match.relative_path for match in by_content.matches])

    def test_search_files_caps_depth_results_and_times_out(self) -> None:
        for index in range(4):
            (self.root / f"match-{index}.txt").write_text("x", encoding="utf-8")
        deep = self.root / "one" / "two" / "three"
        deep.mkdir(parents=True)
        (deep / "match-deep.txt").write_text("x", encoding="utf-8")

        capped = search_files(
            str(self.root), "match", mode="name", recursive=True,
            max_matches=2, max_depth=2,
            path_validator=allowing_path_validator,
        )
        self.assertEqual(len(capped.matches), 2)
        self.assertTrue(capped.truncated)
        self.assertNotIn("one/two/three/match-deep.txt", [m.relative_path for m in capped.matches])

        with self.assertRaises(ReadOnlyToolError) as caught:
            search_files(
                str(self.root), "match", mode="name", recursive=True,
                timeout_ms=1,
                path_validator=allowing_path_validator,
                clock=StepClock(0.0, 1.0),
            )
        self.assertEqual(caught.exception.code, "timeout")


class ReadOnlyProcessToolTests(unittest.TestCase):
    def test_process_results_filter_cap_and_never_expose_sensitive_fields(self) -> None:
        snapshots = (
            ProcessSnapshot(pid=11, name="alpha.exe", memory_bytes=100, executable_path=r"C:\\Apps\\alpha.exe", started_at="2026-09-20T01:00:00Z"),
            ProcessSnapshot(pid=12, name="beta.exe", memory_bytes=200, executable_path=r"C:\\Users\\name\\secret\\beta.exe", started_at=None),
            ProcessSnapshot(pid=13, name="alpha-helper.exe", memory_bytes=None, executable_path=None, started_at=None),
        )

        result = get_processes(
            name_filter="alpha",
            include_details=True,
            max_items=1,
            process_provider=lambda: snapshots,
        )

        self.assertEqual(len(result.processes), 1)
        self.assertTrue(result.truncated)
        payload = result.to_dict()
        serialized = repr(payload).lower()
        self.assertNotIn("command_line", serialized)
        self.assertNotIn("environment", serialized)
        self.assertNotIn("username", serialized)
        self.assertEqual(payload["processes"][0]["executable_summary"], "alpha.exe")

    def test_process_exit_race_and_permission_failure_are_skipped(self) -> None:
        def provider():
            yield ProcessSnapshot(pid=21, name="kept.exe")
            raise ProcessLookupError("process exited")

        result = get_processes(process_provider=provider)
        self.assertEqual([item.pid for item in result.processes], [21])

        def denied_provider():
            raise PermissionError("denied")
            yield  # pragma: no cover

        with self.assertRaises(ReadOnlyToolError) as caught:
            get_processes(process_provider=denied_provider)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_process_timeout_is_structured(self) -> None:
        with self.assertRaises(ReadOnlyToolError) as caught:
            get_processes(
                process_provider=lambda: (ProcessSnapshot(pid=1, name="one.exe"),),
                timeout_ms=1,
                clock=StepClock(0.0, 1.0),
            )
        self.assertEqual(caught.exception.code, "timeout")


if __name__ == "__main__":
    unittest.main()