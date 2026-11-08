"""P3 人物模板公开接口。"""

from .source import (
    DEFAULT_PERSONA_NAME,
    DEFAULT_PERSONA_RELATIVE_PATH,
    PERSONA_SOURCE_BUILTIN,
    PERSONA_SOURCE_FILE,
    PersonaError,
    PersonaSnapshot,
    PersonaSource,
    ResolvedPersona,
    resolve_persona,
    strip_front_matter,
)

__all__ = [
    "DEFAULT_PERSONA_NAME",
    "DEFAULT_PERSONA_RELATIVE_PATH",
    "PERSONA_SOURCE_BUILTIN",
    "PERSONA_SOURCE_FILE",
    "PersonaError",
    "PersonaSnapshot",
    "PersonaSource",
    "ResolvedPersona",
    "resolve_persona",
    "strip_front_matter",
]
