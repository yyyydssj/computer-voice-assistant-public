from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from app.tts import (
    VoiceProfile,
    VoiceProfileActor,
    VoiceProfilePermissionError,
    VoiceProfileRegistry,
    VoiceProfileType,
)


class VoiceProfileTests(unittest.TestCase):
    def make_profile(self, *, version: int = 1, enabled: bool = False, name: str = "默认助手音色") -> VoiceProfile:
        return VoiceProfile(
            profile_id="assistant_default",
            name=name,
            profile_type=VoiceProfileType.DESIGNED,
            provider="voicestudio",
            provider_profile_id="local-sensitive-profile-ref",
            design_prompt="温和、清晰、自然的中文陪伴音色",
            language="zh-CN",
            speed=1.0,
            pitch=0.0,
            version=version,
            enabled=enabled,
        )

    def test_profile_is_immutable_and_public_view_redacts_sensitive_fields(self) -> None:
        profile = self.make_profile()
        with self.assertRaises(FrozenInstanceError):
            profile.name = "changed"  # type: ignore[misc]
        public = profile.to_public_dict()
        self.assertNotIn("provider_profile_id", public)
        self.assertNotIn("design_prompt", public)
        self.assertEqual(repr(profile).find("local-sensitive-profile-ref"), -1)

    def test_profile_validation_requires_design_prompt_for_designed_voice(self) -> None:
        with self.assertRaisesRegex(ValueError, "design_prompt"):
            VoiceProfile(
                profile_id="profile-1",
                name="设计音色",
                profile_type=VoiceProfileType.DESIGNED,
                provider="voicestudio",
                provider_profile_id="ref-1",
            )

    def test_non_user_cannot_mutate_or_select(self) -> None:
        registry = VoiceProfileRegistry()
        profile = self.make_profile()
        for actor in (VoiceProfileActor.AGENT, VoiceProfileActor.LLM, VoiceProfileActor.PROVIDER, VoiceProfileActor.SYSTEM):
            with self.subTest(actor=actor):
                with self.assertRaises(VoiceProfilePermissionError):
                    registry.create(profile, actor=actor)
        registry.create(profile, actor=VoiceProfileActor.USER)
        registry.enable(profile.profile_id, actor=VoiceProfileActor.USER)
        for actor in (VoiceProfileActor.AGENT, VoiceProfileActor.LLM, VoiceProfileActor.PROVIDER, VoiceProfileActor.SYSTEM):
            with self.subTest(actor=actor):
                with self.assertRaises(VoiceProfilePermissionError):
                    registry.select(profile.profile_id, actor=actor)
        self.assertIsNone(registry.selected_profile_id)

    def test_create_does_not_auto_enable_or_auto_select(self) -> None:
        registry = VoiceProfileRegistry()
        profile = registry.create(self.make_profile(), actor=VoiceProfileActor.USER)
        self.assertFalse(profile.enabled)
        self.assertIsNone(registry.selected_profile_id)
        with self.assertRaisesRegex(ValueError, "启用"):
            registry.select(profile.profile_id, actor=VoiceProfileActor.USER)

    def test_enable_select_and_bind_pin_one_confirmed_version(self) -> None:
        registry = VoiceProfileRegistry()
        registry.create(self.make_profile(), actor=VoiceProfileActor.USER)
        enabled = registry.enable("assistant_default", actor=VoiceProfileActor.USER)
        selected = registry.select("assistant_default", actor=VoiceProfileActor.USER)
        session = registry.bind_session("session-1")
        self.assertEqual((enabled.version, selected.version, session.profile_version), (2, 2, 2))
        self.assertEqual(session.profile.profile_id, "assistant_default")

        revised = registry.revise(self.make_profile(version=3, name="新版助手音色", enabled=True), actor=VoiceProfileActor.USER)
        self.assertEqual(revised.version, 3)
        self.assertEqual(registry.selected().version, 3)
        self.assertEqual(session.profile_version, 2)
        self.assertEqual(session.profile.name, "默认助手音色")

    def test_revision_history_never_overwrites_and_rollback_creates_new_version(self) -> None:
        registry = VoiceProfileRegistry()
        registry.create(self.make_profile(), actor=VoiceProfileActor.USER)
        registry.enable("assistant_default", actor=VoiceProfileActor.USER)
        registry.revise(self.make_profile(version=3, name="第二版", enabled=True), actor=VoiceProfileActor.USER)
        rolled_back = registry.rollback("assistant_default", to_version=2, actor=VoiceProfileActor.USER)
        self.assertEqual(rolled_back.version, 4)
        self.assertEqual(rolled_back.name, "默认助手音色")
        self.assertEqual([item.version for item in registry.history("assistant_default")], [1, 2, 3, 4])

    def test_disable_clears_selection_and_delete_clears_registry(self) -> None:
        registry = VoiceProfileRegistry()
        registry.create(self.make_profile(), actor=VoiceProfileActor.USER)
        registry.enable("assistant_default", actor=VoiceProfileActor.USER)
        registry.select("assistant_default", actor=VoiceProfileActor.USER)
        disabled = registry.disable("assistant_default", actor=VoiceProfileActor.USER)
        self.assertFalse(disabled.enabled)
        self.assertIsNone(registry.selected_profile_id)
        registry.delete("assistant_default", actor=VoiceProfileActor.USER)
        self.assertEqual(registry.profiles(), ())

    def test_selection_does_not_automatically_switch_to_another_profile(self) -> None:
        first = self.make_profile()
        second = VoiceProfile(
            profile_id="backup_profile",
            name="备用音色",
            profile_type=VoiceProfileType.PRESET,
            provider="edge_tts",
            provider_profile_id="edge-zh-CN-XiaoxiaoNeural",
            language="zh-CN",
            enabled=True,
        )
        registry = VoiceProfileRegistry((first, second))
        registry.enable(first.profile_id, actor=VoiceProfileActor.USER)
        registry.select(first.profile_id, actor=VoiceProfileActor.USER)
        registry.delete(first.profile_id, actor=VoiceProfileActor.USER)
        self.assertIsNone(registry.selected_profile_id)
        with self.assertRaisesRegex(ValueError, "选择"):
            registry.selected()


if __name__ == "__main__":
    unittest.main()