"""P7 批次 3：Provider 能力声明与兼容性判定。

本模块只描述 Provider adapter 明确声明的本地能力，不连接网络、不发起合成、
不接收音频流，也不产生 provider_request_id 或播放事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from app.contracts.base import (
    ContractError,
    require_choice,
    require_identifier,
    require_positive,
)


class CapabilityError(ValueError):
    """Provider 能力声明、注册或兼容性请求违反约束。"""


class CapabilityDecision(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


_ALLOWED_AUDIO_FORMATS = frozenset({"pcm_s16le", "pcm_f32le", "opus", "mp3"})
_MAX_SAMPLE_RATE_HZ = 192_000
_MAX_CHANNEL_COUNT = 8
_MAX_ESTIMATED_LATENCY_MS = 300_000


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Provider 明确支持的一种音频格式与采样参数。"""

    format_name: str
    sample_rate_hz: int
    channel_count: int

    def __post_init__(self) -> None:
        try:
            require_choice("format_name", self.format_name, _ALLOWED_AUDIO_FORMATS)
            require_positive("sample_rate_hz", self.sample_rate_hz)
            require_positive("channel_count", self.channel_count)
        except ContractError as exc:
            raise CapabilityError(str(exc)) from exc
        if self.sample_rate_hz > _MAX_SAMPLE_RATE_HZ:
            raise CapabilityError("sample_rate_hz 不得超过 192000")
        if self.channel_count > _MAX_CHANNEL_COUNT:
            raise CapabilityError("channel_count 不得超过 8")


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """单个 Provider adapter 的显式、不可变能力事实。"""

    provider_name: str
    supports_text_stream: bool
    supports_audio_stream: bool
    supports_cancellation: bool
    supports_voice_switching: bool
    audio_formats: tuple[AudioFormat, ...]
    estimated_first_audio_latency_ms: int

    def __post_init__(self) -> None:
        try:
            require_identifier("provider_name", self.provider_name)
            require_positive(
                "estimated_first_audio_latency_ms",
                self.estimated_first_audio_latency_ms,
                allow_zero=True,
            )
        except ContractError as exc:
            raise CapabilityError(str(exc)) from exc
        for field_name, value in (
            ("supports_text_stream", self.supports_text_stream),
            ("supports_audio_stream", self.supports_audio_stream),
            ("supports_cancellation", self.supports_cancellation),
            ("supports_voice_switching", self.supports_voice_switching),
        ):
            if not isinstance(value, bool):
                raise CapabilityError(f"{field_name} 必须是 bool")
        if not isinstance(self.audio_formats, tuple):
            raise CapabilityError("audio_formats 必须是 tuple")
        if any(not isinstance(item, AudioFormat) for item in self.audio_formats):
            raise CapabilityError("audio_formats 只能包含 AudioFormat")
        if len(set(self.audio_formats)) != len(self.audio_formats):
            raise CapabilityError("duplicate audio format is not allowed")
        if self.supports_audio_stream and not self.audio_formats:
            raise CapabilityError("audio stream requires at least one audio format")
        if not self.supports_audio_stream and self.audio_formats:
            raise CapabilityError("audio format requires audio stream support")
        if self.estimated_first_audio_latency_ms > _MAX_ESTIMATED_LATENCY_MS:
            raise CapabilityError("estimated_first_audio_latency_ms 不得超过 300000")

    def check_compatibility(self, request: "ProviderCapabilityRequest") -> "CapabilityCheck":
        if not isinstance(request, ProviderCapabilityRequest):
            raise TypeError("request 必须是 ProviderCapabilityRequest")
        reasons: list[str] = []
        if request.require_text_stream and not self.supports_text_stream:
            reasons.append("text stream capability is not declared")
        if request.require_audio_stream and not self.supports_audio_stream:
            reasons.append("audio stream capability is not declared")
        if request.require_cancellation and not self.supports_cancellation:
            reasons.append("cancellation capability is not declared")
        if request.require_voice_switching and not self.supports_voice_switching:
            reasons.append("voice switching capability is not declared")
        if request.audio_format is not None:
            if not request.require_audio_stream:
                reasons.append("audio_format requires audio_stream request")
            elif not self.supports_audio_stream:
                reasons.append("audio stream capability is not declared")
            elif request.audio_format not in self.audio_formats:
                reasons.append("requested audio format is not declared")
        return CapabilityCheck(
            provider_name=self.provider_name,
            decision=(
                CapabilityDecision.COMPATIBLE
                if not reasons
                else CapabilityDecision.INCOMPATIBLE
            ),
            reasons=tuple(reasons),
        )

    def assert_compatible(self, request: "ProviderCapabilityRequest") -> None:
        result = self.check_compatibility(request)
        if result.decision is CapabilityDecision.INCOMPATIBLE:
            raise CapabilityError(
                f"Provider {self.provider_name!r} is incompatible with request: "
                + "；".join(result.reasons)
            )


@dataclass(frozen=True, slots=True)
class ProviderCapabilityRequest:
    """调用前的能力需求；不是 Provider 请求，也不产生请求 ID。"""

    require_text_stream: bool = False
    require_audio_stream: bool = False
    require_cancellation: bool = False
    require_voice_switching: bool = False
    audio_format: AudioFormat | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("require_text_stream", self.require_text_stream),
            ("require_audio_stream", self.require_audio_stream),
            ("require_cancellation", self.require_cancellation),
            ("require_voice_switching", self.require_voice_switching),
        ):
            if not isinstance(value, bool):
                raise CapabilityError(f"{field_name} 必须是 bool")
        if self.audio_format is not None and not isinstance(self.audio_format, AudioFormat):
            raise CapabilityError("audio_format 必须是 AudioFormat 或 None")


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """能力兼容性判定结果；不代表 Provider 已接纳或已生成音频。"""

    provider_name: str
    decision: CapabilityDecision
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            require_identifier("provider_name", self.provider_name)
        except ContractError as exc:
            raise CapabilityError(str(exc)) from exc
        if not isinstance(self.decision, CapabilityDecision):
            raise CapabilityError("decision 必须是 CapabilityDecision")
        if not isinstance(self.reasons, tuple):
            raise CapabilityError("reasons 必须是 tuple")
        if self.decision is CapabilityDecision.COMPATIBLE and self.reasons:
            raise CapabilityError("兼容结果不得包含拒绝原因")
        if self.decision is CapabilityDecision.INCOMPATIBLE and not self.reasons:
            raise CapabilityError("不兼容结果必须包含拒绝原因")

    @property
    def compatible(self) -> bool:
        return self.decision is CapabilityDecision.COMPATIBLE


class ProviderCapabilityRegistry:
    """Provider adapter 能力事实的显式注册表；未知 Provider 默认不支持。"""

    def __init__(self, capabilities: Iterable[ProviderCapability] = ()) -> None:
        self._capabilities: dict[str, ProviderCapability] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: ProviderCapability) -> ProviderCapability:
        if not isinstance(capability, ProviderCapability):
            raise TypeError("capability 必须是 ProviderCapability")
        if capability.provider_name in self._capabilities:
            raise CapabilityError("Provider capability 不得重复注册")
        self._capabilities[capability.provider_name] = capability
        return capability

    def get(self, provider_name: str) -> ProviderCapability:
        try:
            require_identifier("provider_name", provider_name)
        except ContractError as exc:
            raise CapabilityError(str(exc)) from exc
        capability = self._capabilities.get(provider_name)
        if capability is None:
            raise CapabilityError(f"未知 Provider: {provider_name}")
        return capability

    def check(
        self,
        provider_name: str,
        request: ProviderCapabilityRequest,
    ) -> CapabilityCheck:
        return self.get(provider_name).check_compatibility(request)

    def assert_compatible(
        self,
        provider_name: str,
        request: ProviderCapabilityRequest,
    ) -> None:
        self.get(provider_name).assert_compatible(request)

    def registered_provider_names(self) -> tuple[str, ...]:
        return tuple(self._capabilities)


__all__ = [
    "AudioFormat",
    "CapabilityCheck",
    "CapabilityDecision",
    "CapabilityError",
    "ProviderCapability",
    "ProviderCapabilityRegistry",
    "ProviderCapabilityRequest",
]
