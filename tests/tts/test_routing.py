import unittest

from app.tts import (
    ProviderRequestPlaybackState,
    TTSFallbackRouter,
    TTSRoute,
    TTSRoutingAction,
    TTSRoutingError,
)


class TTSFallbackRouterTests(unittest.TestCase):
    def test_unconfigured_edge_falls_back_to_text_without_third_provider(self) -> None:
        router = TTSFallbackRouter()
        self.assertEqual(router.start_reply("reply-1"), TTSRoute.VOICESTUDIO)
        decision = router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.NOT_COMMITTED
        )
        self.assertEqual(decision.route, TTSRoute.TEXT)
        self.assertEqual(decision.action, TTSRoutingAction.CONTINUE_TEXT_NO_REPLAY)
        self.assertEqual(decision.next_reply_route, TTSRoute.TEXT)
        self.assertFalse(decision.replay_current_segment)

    def test_not_started_primary_failure_uses_configured_edge_for_current_segment(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        router.start_reply("reply-1")
        decision = router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.COMMITTED_NOT_STARTED
        )
        self.assertEqual(decision.route, TTSRoute.EDGE)
        self.assertEqual(decision.action, TTSRoutingAction.USE_EDGE_CURRENT_SEGMENT)
        self.assertTrue(decision.cancel_previous_provider_request)
        self.assertTrue(decision.drop_remaining_segments)
        self.assertFalse(decision.replay_current_segment)
        self.assertEqual(router.snapshot.active_route, TTSRoute.EDGE)

    def test_started_or_partial_primary_failure_never_mixes_voice_or_replays(self) -> None:
        for state in (
            ProviderRequestPlaybackState.PLAYBACK_STARTED,
            ProviderRequestPlaybackState.PARTIALLY_PLAYED,
        ):
            router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
            router.start_reply("reply-1")
            decision = router.primary_failed("reply-1", "segment-1", playback_state=state)
            self.assertEqual(decision.route, TTSRoute.TEXT)
            self.assertFalse(decision.replay_current_segment)
            self.assertTrue(decision.drop_remaining_segments)
            self.assertEqual(router.snapshot.active_route, TTSRoute.TEXT)

    def test_played_segment_is_not_replayed_and_old_reply_remainder_is_dropped(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        router.start_reply("reply-1")
        decision = router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.PLAYED
        )
        self.assertEqual(decision.route, TTSRoute.TEXT)
        self.assertFalse(decision.replay_current_segment)
        self.assertTrue(decision.drop_remaining_segments)
        self.assertEqual(decision.next_reply_route, TTSRoute.EDGE)

    def test_edge_failure_is_text_only_and_does_not_try_third_provider(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        router.start_reply("reply-1")
        router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.NOT_COMMITTED
        )
        decision = router.edge_failed("reply-1", "segment-1")
        self.assertEqual(decision.route, TTSRoute.TEXT)
        self.assertEqual(decision.next_reply_route, TTSRoute.TEXT)
        self.assertEqual(router.snapshot.next_reply_route, TTSRoute.TEXT)

    def test_recovery_does_not_switch_active_reply_but_next_reply_uses_primary(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        router.start_reply("reply-1")
        router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.NOT_COMMITTED
        )
        recovered = router.primary_recovered()
        self.assertEqual(recovered.active_route, TTSRoute.EDGE)
        self.assertEqual(recovered.next_reply_route, TTSRoute.VOICESTUDIO)
        router.finish_reply("reply-1")
        self.assertEqual(router.start_reply("reply-2"), TTSRoute.VOICESTUDIO)

    def test_reply_boundary_prevents_mid_reply_provider_switch(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        self.assertEqual(router.start_reply("reply-1"), TTSRoute.VOICESTUDIO)
        with self.assertRaises(TTSRoutingError):
            router.start_reply("reply-2")
        router.finish_reply("reply-1")
        self.assertEqual(router.start_reply("reply-2"), TTSRoute.VOICESTUDIO)

    def test_duplicate_segment_failure_and_wrong_reply_fail_closed(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        router.start_reply("reply-1")
        router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.NOT_COMMITTED
        )
        with self.assertRaises(TTSRoutingError):
            router.primary_failed(
                "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.NOT_COMMITTED
            )
        edge_decision = router.edge_failed("reply-1", "segment-1")
        self.assertEqual(edge_decision.route, TTSRoute.TEXT)
        with self.assertRaises(TTSRoutingError):
            router.edge_failed("reply-1", "segment-1")
        with self.assertRaises(TTSRoutingError):
            router.edge_failed("reply-2", "segment-2")

    def test_abort_is_not_treated_as_permission_to_replay(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        router.start_reply("reply-1")
        decision = router.primary_failed(
            "reply-1", "segment-1", playback_state=ProviderRequestPlaybackState.ABORTED
        )
        self.assertEqual(decision.route, TTSRoute.TEXT)
        self.assertFalse(decision.replay_current_segment)
        self.assertEqual(decision.next_reply_route, TTSRoute.EDGE)

    def test_snapshot_and_public_contract_are_immutable(self) -> None:
        router = TTSFallbackRouter(edge_voice_profile_id="edge-profile")
        snapshot = router.snapshot
        self.assertTrue(snapshot.edge_configured)
        with self.assertRaises(TTSRoutingError):
            TTSFallbackRouter(edge_voice_profile_id=" ")
        self.assertIsNone(snapshot.active_reply_id)


if __name__ == "__main__":
    unittest.main()
