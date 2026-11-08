from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from app.persona import PersonaError, PersonaSource


PERSONA_TEXT = """---
name: persona-xiaobing-voice
description: 小饼测试人设
---

# 小饼

## 人设正文（system prompt）

你是小饼，每次回复都叫用户学长。
"""


class PersonaSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.path = self.root / "persona.md"
        self.path.write_text(PERSONA_TEXT, encoding="utf-8", newline="\n")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reads_exact_utf8_document_and_derives_stable_version(self) -> None:
        raw = self.path.read_bytes()

        persona = PersonaSource(self.path).load()

        self.assertEqual(persona.source_path, self.path)
        self.assertEqual(persona.name, "persona-xiaobing-voice")
        self.assertEqual(persona.description, "小饼测试人设")
        self.assertEqual(persona.system_prompt, PERSONA_TEXT)
        self.assertEqual(persona.version, f"sha256:{hashlib.sha256(raw).hexdigest()}")

    def test_source_is_read_only_and_snapshot_is_immutable(self) -> None:
        before = self.path.read_bytes()
        source = PersonaSource(self.path)
        persona = source.load()

        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(hasattr(source, "save"))
        self.assertFalse(hasattr(source, "update"))
        with self.assertRaises(FrozenInstanceError):
            persona.system_prompt = "Agent 试图改写人设"
        with self.assertRaises(FrozenInstanceError):
            source.path = self.root / "other.md"

    def test_user_file_change_requires_explicit_reload_and_creates_new_version(self) -> None:
        source = PersonaSource(self.path)
        first = source.load()
        changed = PERSONA_TEXT.replace("小饼测试人设", "用户修改后的人设").replace(
            "每次回复都叫用户学长", "保持自然口语"
        )
        self.path.write_text(changed, encoding="utf-8", newline="\n")

        self.assertEqual(first.system_prompt, PERSONA_TEXT)
        second = source.load()

        self.assertNotEqual(second.version, first.version)
        self.assertEqual(second.description, "用户修改后的人设")
        self.assertEqual(second.system_prompt, changed)

    def test_rejects_relative_missing_directory_and_network_paths(self) -> None:
        with self.assertRaisesRegex(PersonaError, "绝对路径"):
            PersonaSource(Path("persona.md"))
        with self.assertRaisesRegex(PersonaError, "已存在文件"):
            PersonaSource(self.root / "missing.md")
        with self.assertRaisesRegex(PersonaError, "已存在文件"):
            PersonaSource(self.root)
        with self.assertRaisesRegex(PersonaError, "网络路径"):
            PersonaSource(Path(r"\\server\share\persona.md"))

    def test_rejects_invalid_utf8_oversize_or_malformed_front_matter(self) -> None:
        cases = {
            "invalid-utf8.md": b"\xff\xfe\x00",
            "oversize.md": ("---\nname: x\ndescription: y\n---\n" + "字" * 50000).encode("utf-8"),
            "missing-front-matter.md": "# 没有元数据\n".encode("utf-8"),
            "missing-name.md": "---\ndescription: x\n---\n正文\n".encode("utf-8"),
            "duplicate-name.md": "---\nname: a\nname: b\ndescription: x\n---\n正文\n".encode("utf-8"),
            "empty-body.md": "---\nname: a\ndescription: x\n---\n\n".encode("utf-8"),
        }
        for filename, payload in cases.items():
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_bytes(payload)
                with self.assertRaises(PersonaError):
                    PersonaSource(path).load()


if __name__ == "__main__":
    unittest.main()
