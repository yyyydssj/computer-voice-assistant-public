"""P7 批次 4：Provider request 的本地不可变准备契约。

本模块把一个已选择且已启用的 VoiceProfile 版本、一个完整句段、一个已通过
Provider capability 判定的音频格式和一个新的 request identity 绑定起来。它只
产生本地 ``PREPARED`` 状态，不调用 Provider、网络、synthesize、cancel、音频
设备或播放队列。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_identifier, require_non_empty, require_positive
from app.tts.capabilities import (
    AudioFormat,
    CapabilityCheck,
    CapabilityDecision,
    CapabilityError,
    ProviderCapabilityRegistry,
    ProviderCapabilityRequest,
)
from app.tts.domain import VoiceProfileRegistry, VoiceProfileSession


class ProviderRequestError(ValueError):
    """Provider request 准备违反领域约束。"""


class ProviderRequestState(str, Enum):
    """本批次唯一允许的本地 request 状态。"""

    PREPARED = "prepared"


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """已经通过本地 capability 判定、但尚未提交给 Provider 的 request。"""

    provider_request_id: str
    reply_id: str
    segment_id: str
    request_sequence: int
    provider_name: str
    provider_generation: int
    voice_profile_id: str
    voice_profile_version: int
    text: str
    audio_format: AudioFormat
    capability_request: ProviderCapabilityRequest
    capability_check: CapabilityCheck
    state: ProviderRequestState = ProviderRequestState.PREPARED

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_request_id", self.provider_request_id),
            ("reply_id", self.reply_id),
            ("segment_id", self.segment_id),
            ("provider_name", self.provider_name),
            ("voice_profile_id", self.voice_profile_id),
        ):
            try:
                require_identifier(name, value)
            except ContractError as exc:
                raise ProviderRequestError(str(exc)) from exc
        try:
            require_positive("request_sequence", self.request_sequence)
            require_positive("provider_generation", self.provider_generation)
            require_positive("voice_profile_version", self.voice_profile_version)
            require_non_empty("text", self.text, maximum=4096)
        except ContractError as exc:
            raise ProviderRequestError(str(exc)) from exc
        if not self.text.strip():
            raise ProviderRequestError("text 不得为空或只包含空白字符")
        if not isinstance(self.audio_format, AudioFormat):
            raise ProviderRequestError("audio_format 必须是 AudioFormat")
        if not isinstance(self.capability_request, ProviderCapabilityRequest):
            raise ProviderRequestError("capability_request 必须是 ProviderCapabilityRequest")
        if not isinstance(self.capability_check, CapabilityCheck):
            raise ProviderRequestError("capability_check 必须是 CapabilityCheck")
        if not isinstance(self.state, ProviderRequestState):
            raise ProviderRequestError("state 必须是 ProviderRequestState")
        if self.provider_name != self.capability_check.provider_name:
            raise ProviderRequestError("provider_name 必须与 capability_check 一致")
        if self.capability_check.decision is not CapabilityDecision.COMPATIBLE:
            raise ProviderRequestError("Provider request 只能绑定兼容的 capability 判定")
        if not self.capability_request.require_text_stream:
            raise ProviderRequestError("Provider request 必须要求 text stream")
        if not self.capability_request.require_audio_stream:
            raise ProviderRequestError("Provider request 必须要求 audio stream")
        if self.capability_request.audio_format != self.audio_format:
            raise ProviderRequestError("audio_format 必须与 capability_request 一致")


class ProviderRequestRegistry:
    """按 reply/segment 顺序准备并保存不可变 Provider request。

    注册表只保存本地准备状态。它要求调用方显式提供已经由用户选择且启用的
    VoiceProfile 会话，并为每个 reply 强制使用从 1 开始的连续 request_sequence。
    """

    def __init__(
        self,
        capabilities: ProviderCapabilityRegistry,
        voice_profiles: VoiceProfileRegistry,
    ) -> None:
        if not isinstance(capabilities, ProviderCapabilityRegistry):
            raise TypeError("capabilities 必须是 ProviderCapabilityRegistry")
        if not isinstance(voice_profiles, VoiceProfileRegistry):
            raise TypeError("voice_profiles 必须是 VoiceProfileRegistry")
        self._capabilities = capabilities
        self._voice_profiles = voice_profiles
        self._requests: dict[str, ProviderRequest] = {}
        self._requests_by_pair: dict[tuple[str, str], str] = {}
        self._segments: dict[str, tuple[str, str]] = {}
        self._next_sequence: dict[str, int] = {}

    def prepare(
        self,
        *,
        provider_request_id: str,
        reply_id: str,
        segment_id: str,
        request_sequence: int,
        text: str,
        audio_format: AudioFormat,
        provider_generation: int,
        session_id: str | None = None,
        voice_session: VoiceProfileSession | None = None,
        require_cancellation: bool = False,
        require_voice_switching: bool = False,
    ) -> ProviderRequest:
        """创建一个本地 PREPARED request；所有失败均不改变注册表状态。"""

        self._validate_identity_inputs(
            provider_request_id=provider_request_id,
            reply_id=reply_id,
            segment_id=segment_id,
            request_sequence=request_sequence,
            text=text,
            audio_format=audio_format,
            provider_generation=provider_generation,
        )
        if not isinstance(require_cancellation, bool):
            raise ProviderRequestError("require_cancellation 必须是 bool")
        if not isinstance(require_voice_switching, bool):
            raise ProviderRequestError("require_voice_switching 必须是 bool")
        if voice_session is not None and session_id is not None:
            raise ProviderRequestError("session_id 与 voice_session 不得同时提供")
        if voice_session is None:
            if session_id is None:
                raise ProviderRequestError("必须提供已选择音色的 session_id 或 voice_session")
            try:
                voice_session = self._voice_profiles.bind_session(session_id)
            except (ContractError, ValueError) as exc:
                raise ProviderRequestError(f"无法绑定已选择的 VoiceProfile: {exc}") from exc
        self._validate_voice_session(voice_session)
        assert voice_session is not None
        self._validate_identity_uniqueness(
            provider_request_id=provider_request_id,
            reply_id=reply_id,
            segment_id=segment_id,
            request_sequence=request_sequence,
        )

        profile = voice_session.profile
        provider_name = profile.provider
        capability_request = ProviderCapabilityRequest(
            require_text_stream=True,
            require_audio_stream=True,
            require_cancellation=require_cancellation,
            require_voice_switching=require_voice_switching,
            audio_format=audio_format,
        )
        try:
            capability_check = self._capabilities.check(provider_name, capability_request)
        except (CapabilityError, TypeError) as exc:
            raise ProviderRequestError(f"Provider capability 不可用: {exc}") from exc
        if capability_check.decision is not CapabilityDecision.COMPATIBLE:
            raise ProviderRequestError(
                "Provider capability 不兼容: " + "; ".join(capability_check.reasons)
            )

        request = ProviderRequest(
            provider_request_id=provider_request_id,
            reply_id=reply_id,
            segment_id=segment_id,
            request_sequence=request_sequence,
            provider_name=provider_name,
            provider_generation=provider_generation,
            voice_profile_id=profile.profile_id,
            voice_profile_version=profile.version,
            text=text.strip(),
            audio_format=audio_format,
            capability_request=capability_request,
            capability_check=capability_check,
        )
        self._requests[request.provider_request_id] = request
        self._requests_by_pair[(request.reply_id, request.segment_id)] = request.provider_request_id
        self._segments[request.segment_id] = (request.reply_id, request.provider_request_id)
        self._next_sequence[request.reply_id] = request.request_sequence + 1
        return request

    def get(self, provider_request_id: str) -> ProviderRequest:
        try:
            require_identifier("provider_request_id", provider_request_id)
        except ContractError as exc:
            raise ProviderRequestError(str(exc)) from exc
        request = self._requests.get(provider_request_id)
        if request is None:
            raise ProviderRequestError(f"未知 provider_request_id: {provider_request_id}")
        return request

    def requests(self) -> tuple[ProviderRequest, ...]:
        return tuple(self._requests.values())

    def for_reply(self, reply_id: str) -> tuple[ProviderRequest, ...]:
        try:
            require_identifier("reply_id", reply_id)
        except ContractError as exc:
            raise ProviderRequestError(str(exc)) from exc
        return tuple(request for request in self._requests.values() if request.reply_id == reply_id)

    def next_request_sequence(self, reply_id: str) -> int:
        try:
            require_identifier("reply_id", reply_id)
        except ContractError as exc:
            raise ProviderRequestError(str(exc)) from exc
        return self._next_sequence.get(reply_id, 1)

    def _validate_voice_session(self, voice_session: VoiceProfileSession) -> None:
        if not isinstance(voice_session, VoiceProfileSession):
            raise ProviderRequestError("voice_session 必须是 VoiceProfileSession")
        profile = voice_session.profile
        if not profile.enabled:
            raise ProviderRequestError("VoiceProfile 未启用")
        if self._voice_profiles.selected_profile_id != profile.profile_id:
            raise ProviderRequestError("VoiceProfile 未由用户选择")
        try:
            current = self._voice_profiles.selected()
        except ValueError as exc:
            raise ProviderRequestError(f"VoiceProfile 未由用户选择: {exc}") from exc
        if current.version != voice_session.profile_version:
            raise ProviderRequestError("VoiceProfile 会话版本不是当前已选择版本")

    def _validate_identity_uniqueness(
        self,
        *,
        provider_request_id: str,
        reply_id: str,
        segment_id: str,
        request_sequence: int,
    ) -> None:
        if provider_request_id in self._requests:
            previous = self._requests[provider_request_id]
            if (previous.reply_id, previous.segment_id) != (reply_id, segment_id):
                raise ProviderRequestError("provider_request_id 不得跨 reply/segment 复用")
            raise ProviderRequestError("provider_request_id 不得重复")
        if (reply_id, segment_id) in self._requests_by_pair:
            raise ProviderRequestError("同一 reply/segment 不得重复准备 Provider request")
        if segment_id in self._segments:
            previous_reply, _ = self._segments[segment_id]
            raise ProviderRequestError(
                f"segment_id 已绑定 reply {previous_reply}，不得跨 reply 复用"
            )
        expected = self._next_sequence.get(reply_id, 1)
        if request_sequence != expected:
            raise ProviderRequestError(
                f"非法请求序列: reply {reply_id} 期望 {expected}，实际 {request_sequence}"
            )

    @staticmethod
    def _validate_identity_inputs(
        *,
        provider_request_id: str,
        reply_id: str,
        segment_id: str,
        request_sequence: int,
        text: str,
        audio_format: AudioFormat,
        provider_generation: int,
    ) -> None:
        try:
            require_identifier("provider_request_id", provider_request_id)
            require_identifier("reply_id", reply_id)
            require_identifier("segment_id", segment_id)
            require_positive("request_sequence", request_sequence)
            require_positive("provider_generation", provider_generation)
            require_non_empty("text", text, maximum=4096)
        except ContractError as exc:
            raise ProviderRequestError(str(exc)) from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderRequestError("text 不得为空或只包含空白字符")
        if not isinstance(audio_format, AudioFormat):
            raise ProviderRequestError("audio_format 不得为空且必须是 AudioFormat")


__all__ = [
    "ProviderRequest",
    "ProviderRequestError",
    "ProviderRequestRegistry",
    "ProviderRequestState",
]
