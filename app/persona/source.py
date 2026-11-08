"""P3 人物模板的只读加载、校验与版本快照。

2026-10-24：人物模板改成**文件驱动**——换人设只改文件，不改代码。解析顺序见
:func:`resolve_persona`：显式路径（``--persona`` / ``ASSISTANT_PERSONA_PATH``）>
仓库默认 ``implementation_plan/persona.md`` > 内置兜底 prompt。显式路径不可用一律
fail closed，绝不静默换成别人设。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PERSONA_RELATIVE_PATH = Path("implementation_plan") / "persona.md"
"""仓库内默认人物模板（改它即换人设）。"""

DEFAULT_PERSONA_NAME = "persona-xiaobing-voice"
"""内置兜底 prompt 的人设标识（配置文件缺失时使用）。"""

PERSONA_SOURCE_FILE = "file"
PERSONA_SOURCE_BUILTIN = "builtin"


class PersonaError(ValueError):
    """人物模板路径或内容不满足 P3 边界。"""


@dataclass(frozen=True, slots=True)
class PersonaSnapshot:
    """一次显式读取形成的不可变人物模板版本。"""

    source_path: Path
    name: str
    description: str
    system_prompt: str
    version: str


@dataclass(frozen=True, slots=True)
class PersonaSource:
    """只读人物模板来源；不提供保存或运行时改写接口。"""

    path: Path
    maximum_bytes: int = 131072

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser()
        if not path.is_absolute():
            raise PersonaError("人物模板必须使用本地绝对路径")
        text = str(path)
        if text.startswith(("\\\\", "//")) or path.drive == "":
            raise PersonaError("人物模板不得使用 UNC 或网络路径")
        resolved = path.resolve(strict=False)
        if not resolved.is_file():
            raise PersonaError("人物模板必须是已存在文件")
        if self.maximum_bytes <= 0:
            raise PersonaError("人物模板大小上限必须为正数")
        object.__setattr__(self, "path", resolved)

    def load(self) -> PersonaSnapshot:
        """严格读取当前文件；只有调用方再次调用才会采纳用户修改。"""

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise PersonaError("人物模板不可读取") from exc
        if not raw:
            raise PersonaError("人物模板不能为空")
        if len(raw) > self.maximum_bytes:
            raise PersonaError("人物模板超过允许大小")
        try:
            # 允许 BOM：人物模板很可能被记事本之类的编辑器改动，带 BOM 的 UTF-8 仍应能直接用。
            # 用 ``utf-8-sig`` 自动剥掉 BOM，否则第一行会变成 ``\ufeff---`` 而判定"缺少 front matter"。
            document = raw.decode("utf-8-sig", errors="strict")
        except UnicodeError as exc:
            raise PersonaError("人物模板必须是严格 UTF-8") from exc

        name, description = _parse_front_matter(document)
        return PersonaSnapshot(
            source_path=self.path,
            name=name,
            description=description,
            system_prompt=document,
            version=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        )


def _parse_front_matter(document: str) -> tuple[str, str]:
    lines = document.splitlines()
    if not lines or lines[0] != "---":
        raise PersonaError("人物模板缺少起始 front matter")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as exc:
        raise PersonaError("人物模板缺少结束 front matter") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:closing_index]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            raise PersonaError("人物模板 front matter 格式无效")
        if key in metadata:
            raise PersonaError(f"人物模板 front matter 字段重复: {key}")
        metadata[key] = value

    name = metadata.get("name")
    description = metadata.get("description")
    if not name or not description:
        raise PersonaError("人物模板必须包含 name 和 description")
    if not "\n".join(lines[closing_index + 1 :]).strip():
        raise PersonaError("人物模板正文不能为空")
    return name, description


def strip_front_matter(document: str) -> str:
    """返回去掉 front matter 的正文；没有 front matter 时原样返回。

    system prompt 只需要正文：``name``/``description`` 是给程序看的元数据，混进提示词
    只会让模型看到一段与对话无关的键值对。原有 ``PersonaSnapshot.system_prompt``
    （全文）契约保持不变，本节只做投影。
    """

    lines = document.splitlines()
    if not lines or lines[0] != "---":
        return document
    try:
        closing_index = lines.index("---", 1)
    except ValueError:
        return document
    return "\n".join(lines[closing_index + 1 :]).strip("\n")


@dataclass(frozen=True, slots=True)
class ResolvedPersona:
    """一次启动**实际采用**的 system prompt 及其来源事实（只读）。"""

    prompt: str
    name: str
    version: str
    source: str
    path: Path | None

    def __post_init__(self) -> None:
        if self.source not in {PERSONA_SOURCE_FILE, PERSONA_SOURCE_BUILTIN}:
            raise PersonaError("人物模板来源不受支持")
        if type(self.prompt) is not str or not self.prompt.strip():
            raise PersonaError("人物模板正文不能为空")
        if self.source == PERSONA_SOURCE_BUILTIN and self.path is not None:
            raise PersonaError("内置兜底人设不得声称来自文件")


def resolve_persona(
    *,
    project_root: Path | str,
    fallback_prompt: str,
    explicit_path: str | Path | None = None,
) -> ResolvedPersona:
    """解析本轮 system prompt：显式路径 > 仓库默认文件 > 内置兜底。

    - **显式路径**（``--persona`` / ``ASSISTANT_PERSONA_PATH``）必须可用：文件缺失、非严格
      UTF-8、缺 front matter、正文为空或超过大小上限都会抛 :class:`PersonaError`。
      换人设是用户的决定，程序不得静默替换成另一个人设。
    - **仓库默认** ``implementation_plan/persona.md`` 不存在时才回落到内置兜底，
      并如实把 ``source`` 记为 ``builtin``。
    - 相对路径按 ``project_root`` 解析（``PersonaSource`` 自身仍只接受本地绝对文件）。
    """

    if type(fallback_prompt) is not str or not fallback_prompt.strip():
        raise PersonaError("内置兜底 prompt 不能为空")
    # 解析基准必须是绝对路径：``PersonaSource`` 只接受本地绝对文件，而调用方可能传入
    # 相对的项目根（例如脚本里写 ``project_root="."``）。
    root = Path(project_root).expanduser().resolve()
    builtin = ResolvedPersona(
        prompt=fallback_prompt,
        name=DEFAULT_PERSONA_NAME,
        version=f"builtin:{hashlib.sha256(fallback_prompt.encode('utf-8')).hexdigest()}",
        source=PERSONA_SOURCE_BUILTIN,
        path=None,
    )
    if explicit_path is not None:
        value = str(explicit_path).strip()
        if not value:
            raise PersonaError("人物模板路径不能为空")
        return _load_persona(value, root)
    if not root.is_dir():
        return builtin
    candidate = root / DEFAULT_PERSONA_RELATIVE_PATH
    if not candidate.is_file():
        return builtin
    return _load_persona(str(candidate), root)


def _load_persona(value: str, root: Path) -> ResolvedPersona:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        snapshot = PersonaSource(path).load()
    except PersonaError as exc:
        raise PersonaError(f"{path}：{exc}") from exc
    return ResolvedPersona(
        prompt=strip_front_matter(snapshot.system_prompt),
        name=snapshot.name,
        version=snapshot.version,
        source=PERSONA_SOURCE_FILE,
        path=snapshot.source_path,
    )
