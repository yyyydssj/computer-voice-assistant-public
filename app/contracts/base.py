"""P2 严格、版本化、仅标准库的数据契约基础。"""

from __future__ import annotations

import json
import re
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from types import UnionType
from typing import Any, ClassVar, Mapping, Union, get_args, get_origin, get_type_hints

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ContractError(ValueError):
    """契约载荷缺失、越界或版本不兼容。"""


def require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ContractError(f"{name} 必须是非空、受限长度的标识符")


def require_non_empty(name: str, value: str, *, maximum: int = 4096) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractError(f"{name} 必须是 1..{maximum} 个字符")


def parse_utc_timestamp(name: str, value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractError(f"{name} 必须是以 Z 结尾的 UTC 时间")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{name} 必须是有效 UTC 时间") from exc
    if parsed.tzinfo != timezone.utc:
        raise ContractError(f"{name} 必须是 UTC 时间")
    return parsed


def require_optional_timestamp(name: str, value: str | None) -> None:
    if value is not None:
        parse_utc_timestamp(name, value)


def require_positive(name: str, value: int, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "非负" if allow_zero else "正"
        raise ContractError(f"{name} 必须是{qualifier}整数")


def require_choice(name: str, value: str, choices: set[str] | frozenset[str]) -> None:
    if value not in choices:
        raise ContractError(f"{name} 不支持值 {value!r}")


def require_unique_identifiers(name: str, values: tuple[str, ...]) -> None:
    if not values:
        raise ContractError(f"{name} 不得为空")
    for value in values:
        require_identifier(name, value)
    if len(set(values)) != len(values):
        raise ContractError(f"{name} 不得包含重复标识")


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is tuple:
        if not isinstance(value, list):
            raise ContractError("元组字段必须使用 JSON 数组")
        item_type = args[0] if args else Any
        return tuple(_decode(item, item_type) for item in value)
    if origin in (Union, UnionType) and type(None) in args:
        if value is None:
            return None
        non_none = next(item for item in args if item is not type(None))
        return _decode(value, non_none)
    return value


class StrictContract:
    """拒绝未知字段并固定 schema 版本的 dataclass mixin。"""

    SCHEMA_VERSION: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            raise TypeError("StrictContract 子类必须是 dataclass")
        result = {field.name: _encode(getattr(self, field.name)) for field in fields(self)}
        return {"schema_version": self.SCHEMA_VERSION, **result}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Any:
        if not isinstance(payload, Mapping):
            raise ContractError(f"{cls.__name__} 必须是 JSON 对象")
        if "schema_version" not in payload:
            raise ContractError("缺少字段: schema_version")
        version = payload["schema_version"]
        if version != cls.SCHEMA_VERSION:
            raise ContractError(f"{cls.__name__} 不支持 schema_version={version!r}")
        contract_fields = {field.name: field for field in fields(cls)}
        unknown = sorted(set(payload) - set(contract_fields) - {"schema_version"})
        if unknown:
            raise ContractError(f"未知字段: {', '.join(unknown)}")
        missing = sorted(name for name, field in contract_fields.items() if name not in payload and field.default is MISSING and field.default_factory is MISSING)
        if missing:
            raise ContractError(f"缺少字段: {', '.join(missing)}")
        hints = get_type_hints(cls)
        values = {name: _decode(payload[name], hints.get(name, Any)) for name in contract_fields if name in payload}
        try:
            return cls(**values)
        except ContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{cls.__name__} 载荷无效: {exc}") from exc

    @classmethod
    def from_json(cls, payload: str) -> Any:
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(f"{cls.__name__} JSON 无效") from exc
        return cls.from_dict(decoded)

    @classmethod
    def migrate_legacy_v0(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        if "schema_version" in payload:
            raise ContractError("legacy v0 载荷已经包含 schema_version")
        migrated = {"schema_version": cls.SCHEMA_VERSION, **dict(payload)}
        return cls.from_dict(migrated).to_dict()

def migrate_legacy_v0(contract_type: type[StrictContract], payload: Mapping[str, Any]) -> Any:
    """把明确标记为旧式 v0 的无版本载荷迁移并构造成当前契约。"""

    if not isinstance(contract_type, type) or not issubclass(contract_type, StrictContract):
        raise TypeError("contract_type 必须是 StrictContract 子类")
    migrated = contract_type.migrate_legacy_v0(payload)
    return contract_type.from_dict(migrated)