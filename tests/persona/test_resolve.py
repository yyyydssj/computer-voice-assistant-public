"""2026-10-24：人物模板改成**文件驱动**（换人设只改文件，不改代码）。

本批只新增解析层与投影：``resolve_persona()`` 按「显式路径 > 仓库默认 > 内置兜底」
选出一份 system prompt，并如实给出 ``source``/``name``/``version`` 三个只读事实。
关键约束（与本项目一贯的 fail-closed 口径一致）：

- **显式指定**的模板不可用 ⇒ 直接抛 :class:`PersonaError`（启动失败），
  绝不静默换成另一个人设；
- 只有**仓库默认文件不存在**时才回落到内置兜底，并把 ``source`` 记为 ``builtin``；
- system prompt 用**正文**（front matter 之后的整段），元数据只留给程序看。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.config import load_settings
from app.main import (
    _deepseek_chat_config,
    _persona_notice,
    build_agent_adapter,
    resolve_run_persona,
)
from app.persona import (
    DEFAULT_PERSONA_RELATIVE_PATH,
    PERSONA_SOURCE_BUILTIN,
    PERSONA_SOURCE_FILE,
    PersonaError,
    ResolvedPersona,
    resolve_persona,
    strip_front_matter,
)
from app.providers.deepseek_chat_transport import DEFAULT_SYSTEM_PROMPT

PERSONA_TEXT = """---
name: persona-test-voice
description: 测试人设
how_to_swap: 改正文即可
---

# 测试人设

你是小学妹，叫他学长。
"""

BODY = "# 测试人设\n\n你是小学妹，叫他学长。"
BUILTIN = "内置兜底 prompt"


class PersonaFixtureMixin:
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        (self.root / "implementation_plan").mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def write_default(self, text: str = PERSONA_TEXT) -> Path:
        path = self.root / DEFAULT_PERSONA_RELATIVE_PATH
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def write_custom(self, name: str, text: str = PERSONA_TEXT) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path


class StripFrontMatterTests(unittest.TestCase):
    def test_body_is_returned_without_metadata(self) -> None:
        self.assertEqual(strip_front_matter(PERSONA_TEXT), BODY)

    def test_document_without_front_matter_is_returned_as_is(self) -> None:
        self.assertEqual(strip_front_matter("# 只有正文\n"), "# 只有正文\n")

    def test_unterminated_front_matter_is_returned_as_is(self) -> None:
        broken = "---\nname: x\n"
        self.assertEqual(strip_front_matter(broken), broken)


class ResolvePersonaTests(PersonaFixtureMixin, unittest.TestCase):
    def test_repository_default_file_wins(self) -> None:
        path = self.write_default()

        persona = resolve_persona(project_root=self.root, fallback_prompt=BUILTIN)

        self.assertEqual(persona.source, PERSONA_SOURCE_FILE)
        self.assertEqual(persona.name, "persona-test-voice")
        self.assertEqual(persona.prompt, BODY)
        self.assertEqual(persona.path, path.resolve())
        self.assertTrue(persona.version.startswith("sha256:"))
        self.assertEqual(
            persona.version,
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_missing_default_falls_back_to_builtin(self) -> None:
        """仓库里没有人物模板文件时不能直接崩：如实降级并标记来源。"""

        persona = resolve_persona(project_root=self.root, fallback_prompt=BUILTIN)

        self.assertEqual(persona.source, PERSONA_SOURCE_BUILTIN)
        self.assertEqual(persona.prompt, BUILTIN)
        self.assertIsNone(persona.path)
        self.assertTrue(persona.version.startswith("builtin:"))

    def test_explicit_path_wins_over_the_repository_default(self) -> None:
        self.write_default()
        custom = self.write_custom(
            "mine.md", PERSONA_TEXT.replace("你是小学妹，叫他学长。", "你是大姐姐。")
        )

        persona = resolve_persona(
            project_root=self.root, fallback_prompt=BUILTIN, explicit_path=custom
        )

        self.assertEqual(persona.path, custom.resolve())
        self.assertIn("你是大姐姐。", persona.prompt)
        self.assertNotIn("你是小学妹", persona.prompt)

    def test_relative_explicit_path_is_resolved_against_project_root(self) -> None:
        self.write_custom("mine.md")

        persona = resolve_persona(
            project_root=self.root, fallback_prompt=BUILTIN, explicit_path="mine.md"
        )

        self.assertEqual(persona.source, PERSONA_SOURCE_FILE)
        self.assertEqual(persona.name, "persona-test-voice")

    def test_broken_explicit_persona_fails_closed(self) -> None:
        """显式指定却读不到：必须报错，绝不静默回落到内置或仓库默认人设。"""

        self.write_default()
        bad = self.root / "broken.md"
        bad.write_text("# 没有 front matter\n", encoding="utf-8", newline="\n")
        for value in (self.root / "missing.md", bad):
            with self.subTest(value=value):
                with self.assertRaises(PersonaError):
                    resolve_persona(
                        project_root=self.root, fallback_prompt=BUILTIN, explicit_path=value
                    )

    def test_blank_explicit_path_is_rejected(self) -> None:
        with self.assertRaises(PersonaError):
            resolve_persona(project_root=self.root, fallback_prompt=BUILTIN, explicit_path="  ")

    def test_works_with_a_relative_project_root(self) -> None:
        """``project_root="."`` 也要能用：解析基准先转绝对路径。"""

        persona = resolve_persona(project_root=".", fallback_prompt=BUILTIN)

        self.assertIn(persona.source, (PERSONA_SOURCE_FILE, PERSONA_SOURCE_BUILTIN))
        if persona.path is not None:
            self.assertTrue(persona.path.is_absolute())

    def test_edited_file_changes_the_version(self) -> None:
        first = resolve_persona(
            project_root=self.root, fallback_prompt=BUILTIN
        )
        path = self.write_default(PERSONA_TEXT.replace("description: 测试人设", "description: 改过"))
        second = resolve_persona(project_root=self.root, fallback_prompt=BUILTIN)

        self.assertEqual(first.source, PERSONA_SOURCE_BUILTIN)
        self.assertNotEqual(first.version, second.version)
        self.assertEqual(second.path, path.resolve())

    def test_bom_prefixed_persona_is_still_accepted(self) -> None:
        """记事本会写 BOM：带 BOM 的模板必须照常可用（否则第一行不是 ``---``）。"""

        path = self.write_default()
        path.write_bytes(b"\xef\xbb\xbf" + PERSONA_TEXT.encode("utf-8"))

        persona = resolve_persona(project_root=self.root, fallback_prompt=BUILTIN)

        self.assertEqual(persona.prompt, BODY)


class ResolvedPersonaContractTests(unittest.TestCase):
    def test_builtin_source_cannot_claim_a_file(self) -> None:
        with self.assertRaises(PersonaError):
            ResolvedPersona(
                prompt=BUILTIN,
                name="x",
                version="builtin:0",
                source=PERSONA_SOURCE_BUILTIN,
                path=Path("C:/x.md"),
            )

    def test_unknown_source_and_blank_prompt_are_rejected(self) -> None:
        for kwargs in (
            {"source": "network"},
            {"source": PERSONA_SOURCE_FILE, "prompt": "   "},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PersonaError):
                    ResolvedPersona(
                        prompt=kwargs.get("prompt", BUILTIN),
                        name="x",
                        version="sha256:0",
                        source=kwargs["source"],
                        path=None,
                    )


class SettingsAndWiringTests(PersonaFixtureMixin, unittest.TestCase):
    def settings(self, **environment: str):
        return load_settings(project_root=self.root, environ=dict(environment))

    def test_persona_path_comes_from_the_environment(self) -> None:
        settings = self.settings(ASSISTANT_PERSONA_PATH="mine.md")
        self.assertEqual(settings.persona_path, "mine.md")
        self.assertEqual(self.settings().persona_path, "")

    def test_run_persona_prefers_the_explicit_argument_then_the_environment(self) -> None:
        self.write_default()
        explicit = self.write_custom(
            "explicit.md", PERSONA_TEXT.replace("description: 测试人设", "description: 显式")
        )
        env = self.write_custom(
            "env.md", PERSONA_TEXT.replace("description: 测试人设", "description: 环境")
        )

        by_argument = resolve_run_persona(
            self.settings(ASSISTANT_PERSONA_PATH=str(env)), str(explicit)
        )
        by_environment = resolve_run_persona(self.settings(ASSISTANT_PERSONA_PATH=str(env)))
        by_default = resolve_run_persona(self.settings())

        self.assertEqual(by_argument.path, explicit.resolve())
        self.assertEqual(by_environment.path, env.resolve())
        self.assertEqual(by_default.path, (self.root / DEFAULT_PERSONA_RELATIVE_PATH).resolve())

    def test_chat_config_uses_the_persona_body(self) -> None:
        """真正生效的地方：对话配置的 system_prompt 必须是人物模板正文。"""

        self.write_default()
        persona = resolve_run_persona(self.settings())

        config = _deepseek_chat_config(system_prompt=persona.prompt)
        self.assertEqual(config.system_prompt, BODY)
        self.assertNotIn("description:", config.system_prompt)
        fallback = _deepseek_chat_config()
        self.assertEqual(fallback.system_prompt, DEFAULT_SYSTEM_PROMPT)

    def test_agent_adapter_carries_the_persona_prompt(self) -> None:
        settings = self.settings(
            ASSISTANT_PROVIDER="deepseek", ASSISTANT_DEEPSEEK_OPT_IN="true"
        )
        self.write_default()
        persona = resolve_run_persona(settings)

        agent = build_agent_adapter(settings, persona=persona)

        self.assertIsNotNone(agent)
        self.assertEqual(agent.config.system_prompt, BODY)
        self.assertIsNone(build_agent_adapter(self.settings()))

    def test_startup_notice_names_the_template(self) -> None:
        self.write_default()
        persona = resolve_run_persona(self.settings())

        notice = _persona_notice(persona)
        self.assertIn("persona-test-voice", notice)
        self.assertIn("重启生效", notice)

        builtin = resolve_persona(
            project_root=self.root / "missing-root", fallback_prompt=BUILTIN
        )
        self.assertIn("内置兜底", _persona_notice(builtin))


if __name__ == "__main__":
    unittest.main()
