from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from app.tts import (
    AudioFormat,
    CapabilityCheck,
    CapabilityDecision,
    ProviderCapability,
    ProviderCapabilityRegistry,
    ProviderRequest,
    ProviderRequestError,
    ProviderRequestRegistry,
    ProviderRequestState,
    VoiceProfile,
    VoiceProfileActor,
    VoiceProfileRegistry,
    VoiceProfileType,
)


class ProviderRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio_format = AudioFormat("pcm_s16le", 24000, 1)
        self.capabilities = ProviderCapabilityRegistry(
            (
                ProviderCapability(
                    provider_name="voicestudio",
                    supports_text_stream=True,
                    supports_audio_stream=True,
                    supports_cancellation=True,
                    supports_voice_switching=True,
                    audio_formats=(self.audio_format,),
                    estimated_first_audio_latency_ms=350,
                ),
            )
        )
        self.voice_profiles = VoiceProfileRegistry()
        profile = VoiceProfile(
            profile_id="assistant-default",
            name="默认助手",
            profile_type=VoiceProfileType.PRESET,
            provider="voicestudio",
            provider_profile_id="provider-profile-ref",
            language="zh-CN",
            enabled=False,
        )
        self.voice_profiles.create(profile, actor=VoiceProfileActor.USER)
        self.voice_profiles.enable(profile.profile_id, actor=VoiceProfileActor.USER)
        self.voice_profiles.select(profile.profile_id, actor=VoiceProfileActor.USER)
        self.session = self.voice_profiles.bind_session("session-1")
        self.registry = ProviderRequestRegistry(self.capabilities, self.voice_profiles)

    def prepare(self, **overrides: object) -> ProviderRequest:
        values: dict[str, object] = {
            "provider_request_id": "provider-request-1",
            "reply_id": "reply-1",
            "segment_id": "segment-1",
            "request_sequence": 1,
            "text": "你好，世界。",
            "audio_format": self.audio_format,
            "provider_generation": 1,
            "voice_session": self.session,
        }
        values.update(overrides)
        return self.registry.prepare(**values)  # type: ignore[arg-type]

    def test_prepare_binds_reply_segment_voice_capability_and_new_identity(self) -> None:
        request = self.prepare()
        self.assertEqual(request.state, ProviderRequestState.PREPARED)
        self.assertEqual(
            (
                request.reply_id,
                request.segment_id,
                request.provider_request_id,
                request.provider_generation,
                request.voice_profile_id,
                request.voice_profile_version,
            ),
            ("reply-1", "segment-1", "provider-request-1", 1, "assistant-default", 2),
        )
        self.assertEqual(request.capability_check.decision, CapabilityDecision.COMPATIBLE)
        self.assertTrue(request.capability_request.require_text_stream)
        self.assertTrue(request.capability_request.require_audio_stream)
        self.assertEqual(self.registry.next_request_sequence("reply-1"), 2)

    def test_unselected_or_disabled_voice_is_rejected(self) -> None:
        self.voice_profiles.clear_selection(actor=VoiceProfileActor.USER)
        with self.assertRaisesRegex(ProviderRequestError, "选择"):
            self.prepare()

        self.voice_profiles.select("assistant-default", actor=VoiceProfileActor.USER)
        self.voice_profiles.disable("assistant-default", actor=VoiceProfileActor.USER)
        with self.assertRaisesRegex(ProviderRequestError, "选择"):
            self.prepare(provider_request_id="provider-request-2")

    def test_unknown_provider_and_incompatible_capability_fail_closed(self) -> None:
        unknown_capabilities = ProviderCapabilityRegistry()
        registry = ProviderRequestRegistry(unknown_capabilities, self.voice_profiles)
        with self.assertRaisesRegex(ProviderRequestError, "未知 Provider"):
            registry.prepare(
                provider_request_id="provider-request-1",
                reply_id="reply-1",
                segment_id="segment-1",
                request_sequence=1,
                text="你好。",
                audio_format=self.audio_format,
                provider_generation=1,
                voice_session=self.session,
            )

        no_cancel = ProviderCapabilityRegistry(
            (
                ProviderCapability(
                    provider_name="voicestudio",
                    supports_text_stream=True,
                    supports_audio_stream=True,
                    supports_cancellation=False,
                    supports_voice_switching=True,
                    audio_formats=(self.audio_format,),
                    estimated_first_audio_latency_ms=1,
                ),
            )
        )
        registry = ProviderRequestRegistry(no_cancel, self.voice_profiles)
        with self.assertRaisesRegex(ProviderRequestError, "不兼容"):
            registry.prepare(
                provider_request_id="provider-request-1",
                reply_id="reply-1",
                segment_id="segment-1",
                request_sequence=1,
                text="你好。",
                audio_format=self.audio_format,
                provider_generation=1,
                voice_session=self.session,
                require_cancellation=True,
            )

    def test_empty_text_empty_format_and_invalid_numbers_are_rejected(self) -> None:
        for text in ("", "   ", "\n\t"):
            with self.subTest(text=repr(text)):
                with self.assertRaisesRegex(ProviderRequestError, "text"):
                    self.prepare(text=text, provider_request_id=f"request-{len(text)}")
        with self.assertRaisesRegex(ProviderRequestError, "audio_format"):
            self.prepare(audio_format=None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ProviderRequestError, "request_sequence"):
            self.prepare(request_sequence=0)
        with self.assertRaisesRegex(ProviderRequestError, "provider_generation"):
            self.prepare(provider_generation=0)

    def test_request_sequence_and_identities_are_strict_and_atomic(self) -> None:
        with self.assertRaisesRegex(ProviderRequestError, "非法请求序列"): 
            self.prepare(request_sequence=2)
        self.assertEqual(self.registry.requests(), ())
        self.prepare()
        with self.assertRaisesRegex(ProviderRequestError, "provider_request_id 不得重复"): 
            self.prepare(request_sequence=2)
        with self.assertRaisesRegex(ProviderRequestError, "同一 reply/segment"): 
            self.prepare(
                provider_request_id="provider-request-3",
                request_sequence=2,
            )
        with self.assertRaisesRegex(ProviderRequestError, "segment_id 已绑定"):
            self.prepare(
                provider_request_id="provider-request-2",
                reply_id="reply-2",
                segment_id="segment-1",
                request_sequence=1,
            )
        self.prepare(
            provider_request_id="provider-request-2",
            reply_id="reply-2",
            segment_id="segment-2",
            request_sequence=1,
        )
        with self.assertRaisesRegex(ProviderRequestError, "provider_request_id 不得跨 reply/segment 复用"): 
            self.prepare(
                provider_request_id="provider-request-2",
                reply_id="reply-3",
                segment_id="segment-3",
                request_sequence=1,
            )

    def test_voice_session_version_must_be_current_selected_version(self) -> None:
        stale = self.session
        self.voice_profiles.revise(
            VoiceProfile(
                profile_id="assistant-default",
                name="新版助手",
                profile_type=VoiceProfileType.PRESET,
                provider="voicestudio",
                provider_profile_id="provider-profile-ref-v2",
                language="zh-CN",
                version=3,
                enabled=True,
            ),
            actor=VoiceProfileActor.USER,
        )
        with self.assertRaisesRegex(ProviderRequestError, "会话版本"):
            self.prepare(voice_session=stale)

    def test_request_is_immutable_and_does_not_mean_provider_or_playback_fact(self) -> None:
        request = self.prepare()
        with self.assertRaises(FrozenInstanceError):
            request.provider_request_id = "changed"  # type: ignore[misc]
        self.assertNotEqual(request.provider_request_id, request.reply_id)
        self.assertNotEqual(request.provider_request_id, request.segment_id)
        self.assertEqual(self.registry.get(request.provider_request_id), request)
        self.assertEqual(self.registry.for_reply("reply-1"), (request,))

    def test_direct_request_rejects_incompatible_or_mismatched_bound_facts(self) -> None:
        prepared = self.prepare()
        incompatible = CapabilityCheck(
            provider_name="voicestudio",
            decision=CapabilityDecision.INCOMPATIBLE,
            reasons=("test rejection",),
        )
        with self.assertRaisesRegex(ProviderRequestError, "兼容"):
            ProviderRequest(
                provider_request_id="direct-request-1",
                reply_id="reply-2",
                segment_id="segment-2",
                request_sequence=1,
                provider_name="voicestudio",
                provider_generation=1,
                voice_profile_id="assistant-default",
                voice_profile_version=2,
                text="文本。",
                audio_format=self.audio_format,
                capability_request=prepared.capability_request,
                capability_check=incompatible,
            )
        with self.assertRaisesRegex(ProviderRequestError, "audio_format"):
            ProviderRequest(
                provider_request_id="direct-request-2",
                reply_id="reply-2",
                segment_id="segment-2",
                request_sequence=1,
                provider_name="voicestudio",
                provider_generation=1,
                voice_profile_id="assistant-default",
                voice_profile_version=2,
                text="文本。",
                audio_format=AudioFormat("opus", 48000, 2),
                capability_request=prepared.capability_request,
                capability_check=prepared.capability_check,
            )

if __name__ == "__main__":
    unittest.main()
