"""P7 批次 1：VoiceProfile 版本与用户唯一修改权限。

本模块只管理已经由用户确认的音色引用。它不创建音色、不调用 Provider、不读取参考音频，
也不把 LLM、Provider 或恢复逻辑变成 VoiceProfile 的隐式修改者。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import re
from typing import Any

from app.contracts.base import require_identifier, require_non_empty, require_positive


class VoiceProfileError(ValueError):
    """VoiceProfile 不满足领域约束，或请求违反当前状态。"""


class VoiceProfilePermissionError(VoiceProfileError):
    """只有用户可以修改、选择或启用 VoiceProfile。"""


class VoiceProfileType(str, Enum):
    PRESET = "preset"
    DESIGNED = "designed"
    CLONED = "cloned"


class VoiceProfileActor(str, Enum):
    USER = "user"
    AGENT = "agent"
    LLM = "llm"
    PROVIDER = "provider"
    SYSTEM = "system"


_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$")
_ALLOWED_PROVIDERS = frozenset({"voicestudio", "edge_tts"})


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """已确认的不可变音色版本。

    ``provider_profile_id`` 与 ``design_prompt`` 只在本地领域对象中保存，默认 repr 和
    ``to_public_dict`` 都不暴露它们，避免进入普通诊断、日志或用户可见状态。
    """

    profile_id: str
    name: str
    profile_type: VoiceProfileType
    provider: str
    provider_profile_id: str = field(repr=False)
    language: str = "zh-CN"
    speed: float = 1.0
    pitch: float = 0.0
    version: int = 1
    enabled: bool = False
    design_prompt: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        require_identifier("profile_id", self.profile_id)
        require_non_empty("name", self.name, maximum=128)
        if not isinstance(self.profile_type, VoiceProfileType):
            try:
                object.__setattr__(self, "profile_type", VoiceProfileType(self.profile_type))
            except (TypeError, ValueError) as exc:
                raise VoiceProfileError("profile_type 必须是 preset/designed/cloned") from exc
        if self.provider not in _ALLOWED_PROVIDERS:
            raise VoiceProfileError("provider 只允许已接入的本地 Provider 标识")
        require_non_empty("provider_profile_id", self.provider_profile_id, maximum=512)
        if not isinstance(self.language, str) or not _LANGUAGE_PATTERN.fullmatch(self.language):
            raise VoiceProfileError("language 必须是 BCP-47 风格标识")
        if isinstance(self.speed, bool) or not isinstance(self.speed, (int, float)) or not 0.25 <= self.speed <= 4.0:
            raise VoiceProfileError("speed 必须位于 0.25..4.0")
        if isinstance(self.pitch, bool) or not isinstance(self.pitch, (int, float)) or not -24.0 <= self.pitch <= 24.0:
            raise VoiceProfileError("pitch 必须位于 -24..24")
        require_positive("version", self.version)
        if not isinstance(self.enabled, bool):
            raise VoiceProfileError("enabled 必须是布尔值")
        if self.design_prompt is not None:
            require_non_empty("design_prompt", self.design_prompt, maximum=2048)
        if self.profile_type is VoiceProfileType.DESIGNED and not self.design_prompt:
            raise VoiceProfileError("designed 音色必须保留用户确认的 design_prompt")
        if self.profile_type is VoiceProfileType.CLONED and self.provider != "voicestudio":
            raise VoiceProfileError("cloned 音色只能引用 VoiceStudio")

    def to_public_dict(self) -> dict[str, Any]:
        """返回可用于界面状态的脱敏视图，不含 Provider 引用和设计提示词。"""

        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "profile_type": self.profile_type.value,
            "provider": self.provider,
            "language": self.language,
            "speed": self.speed,
            "pitch": self.pitch,
            "version": self.version,
            "enabled": self.enabled,
        }

    def to_internal_dict(self) -> dict[str, Any]:
        """返回本地持久化/Provider 适配层所需的完整领域值。"""

        return {
            **self.to_public_dict(),
            "provider_profile_id": self.provider_profile_id,
            "design_prompt": self.design_prompt,
        }


@dataclass(frozen=True, slots=True)
class VoiceProfileSession:
    """绑定到单一 profile 版本的会话快照。"""

    session_id: str
    profile_id: str
    profile_version: int
    profile: VoiceProfile

    def __post_init__(self) -> None:
        require_identifier("session_id", self.session_id)
        require_identifier("profile_id", self.profile_id)
        require_positive("profile_version", self.profile_version)
        if self.profile.profile_id != self.profile_id or self.profile.version != self.profile_version:
            raise VoiceProfileError("VoiceProfileSession 必须绑定同一 profile 版本")


class VoiceProfileRegistry:
    """保存用户确认的 profile 版本，并拒绝非用户修改。

    该注册表只维护内存领域状态。它不做文件读写、Provider 调用、网络请求、音频操作或
    自动选择；调用方必须显式传入 ``VoiceProfileActor.USER`` 才能改变状态。
    """

    def __init__(self, profiles: tuple[VoiceProfile, ...] = ()) -> None:
        if not isinstance(profiles, tuple):
            raise TypeError("profiles 必须是 tuple")
        self._revisions: dict[str, list[VoiceProfile]] = {}
        self._selected_profile_id: str | None = None
        for profile in profiles:
            if not isinstance(profile, VoiceProfile):
                raise TypeError("profiles 只能包含 VoiceProfile")
            if profile.profile_id in self._revisions:
                raise VoiceProfileError("profile_id 不得重复")
            self._revisions[profile.profile_id] = [profile]

    @property
    def selected_profile_id(self) -> str | None:
        return self._selected_profile_id

    def profiles(self) -> tuple[VoiceProfile, ...]:
        return tuple(self._latest(profile_id) for profile_id in sorted(self._revisions))

    def history(self, profile_id: str) -> tuple[VoiceProfile, ...]:
        require_identifier("profile_id", profile_id)
        revisions = self._revisions.get(profile_id)
        if revisions is None:
            raise VoiceProfileError("未找到 VoiceProfile")
        return tuple(revisions)

    def create(self, profile: VoiceProfile, *, actor: VoiceProfileActor) -> VoiceProfile:
        self._require_user(actor)
        if not isinstance(profile, VoiceProfile):
            raise TypeError("profile 必须是 VoiceProfile")
        if profile.profile_id in self._revisions:
            raise VoiceProfileError("profile_id 已存在；请创建新版本而不是覆盖")
        if profile.version != 1:
            raise VoiceProfileError("新 VoiceProfile 必须从 version=1 开始")
        self._revisions[profile.profile_id] = [profile]
        return profile

    def revise(self, profile: VoiceProfile, *, actor: VoiceProfileActor) -> VoiceProfile:
        self._require_user(actor)
        if not isinstance(profile, VoiceProfile):
            raise TypeError("profile 必须是 VoiceProfile")
        revisions = self._revisions.get(profile.profile_id)
        if revisions is None:
            raise VoiceProfileError("未找到 VoiceProfile")
        latest = revisions[-1]
        if profile.version != latest.version + 1:
            raise VoiceProfileError("新版本必须严格递增且不可复用旧版本号")
        revisions.append(profile)
        return profile

    def enable(self, profile_id: str, *, actor: VoiceProfileActor) -> VoiceProfile:
        self._require_user(actor)
        latest = self._latest(profile_id)
        if latest.enabled:
            return latest
        return self._append_revision(replace(latest, version=latest.version + 1, enabled=True))

    def disable(self, profile_id: str, *, actor: VoiceProfileActor) -> VoiceProfile:
        self._require_user(actor)
        latest = self._latest(profile_id)
        if not latest.enabled:
            return latest
        disabled = self._append_revision(replace(latest, version=latest.version + 1, enabled=False))
        if self._selected_profile_id == profile_id:
            self._selected_profile_id = None
        return disabled

    def select(self, profile_id: str, *, actor: VoiceProfileActor) -> VoiceProfile:
        self._require_user(actor)
        latest = self._latest(profile_id)
        if not latest.enabled:
            raise VoiceProfileError("只能选择已由用户启用的 VoiceProfile")
        self._selected_profile_id = profile_id
        return latest

    def clear_selection(self, *, actor: VoiceProfileActor) -> None:
        self._require_user(actor)
        self._selected_profile_id = None

    def delete(self, profile_id: str, *, actor: VoiceProfileActor) -> None:
        self._require_user(actor)
        require_identifier("profile_id", profile_id)
        if profile_id not in self._revisions:
            raise VoiceProfileError("未找到 VoiceProfile")
        del self._revisions[profile_id]
        if self._selected_profile_id == profile_id:
            self._selected_profile_id = None

    def rollback(self, profile_id: str, *, to_version: int, actor: VoiceProfileActor) -> VoiceProfile:
        self._require_user(actor)
        require_identifier("profile_id", profile_id)
        require_positive("to_version", to_version)
        revisions = self._revisions.get(profile_id)
        if revisions is None:
            raise VoiceProfileError("未找到 VoiceProfile")
        if to_version > revisions[-1].version:
            raise VoiceProfileError("不能回退到不存在的版本")
        target = next((item for item in revisions if item.version == to_version), None)
        if target is None:
            raise VoiceProfileError("只能回退到已有的完整版本")
        return self._append_revision(replace(target, version=revisions[-1].version + 1))

    def selected(self) -> VoiceProfile:
        if self._selected_profile_id is None:
            raise VoiceProfileError("尚未由用户选择 VoiceProfile")
        return self._latest(self._selected_profile_id)

    def bind_session(self, session_id: str) -> VoiceProfileSession:
        profile = self.selected()
        return VoiceProfileSession(
            session_id=session_id,
            profile_id=profile.profile_id,
            profile_version=profile.version,
            profile=profile,
        )

    def _latest(self, profile_id: str) -> VoiceProfile:
        require_identifier("profile_id", profile_id)
        revisions = self._revisions.get(profile_id)
        if revisions is None:
            raise VoiceProfileError("未找到 VoiceProfile")
        return revisions[-1]

    def _append_revision(self, profile: VoiceProfile) -> VoiceProfile:
        revisions = self._revisions.get(profile.profile_id)
        if revisions is None:
            raise VoiceProfileError("未找到 VoiceProfile")
        if profile.version != revisions[-1].version + 1:
            raise VoiceProfileError("版本必须严格递增")
        revisions.append(profile)
        return profile

    @staticmethod
    def _require_user(actor: VoiceProfileActor) -> None:
        if actor is not VoiceProfileActor.USER:
            raise VoiceProfilePermissionError("只有用户可以修改、选择、启用或删除 VoiceProfile")