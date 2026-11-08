from __future__ import annotations

import unittest
from dataclasses import replace

from app.tts import (
    AudioFormat,
    ProviderCapability,
    ProviderCapabilityRegistry,
    ProviderRequestLifecycleRegistry,
    ProviderRequestPlaybackError,
    ProviderRequestPlaybackRegistry,
    ProviderRequestPlaybackState,
    ProviderRequestRegistry,
    ProviderRequestAudioChunk,
    VoiceProfile,
    VoiceProfileActor,
    VoiceProfileRegistry,
    VoiceProfileType,
)


class ProviderRequestPlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio_format = AudioFormat("pcm_s16le", 24000, 1)
        capabilities = ProviderCapabilityRegistry(
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
        profiles = VoiceProfileRegistry()
        profile = VoiceProfile(
            profile_id="assistant-default",
            name="默认助手",
            profile_type=VoiceProfileType.PRESET,
            provider="voicestudio",
            provider_profile_id="provider-profile-ref",
            language="zh-CN",
            enabled=False,
        )
        profiles.create(profile, actor=VoiceProfileActor.USER)
        profiles.enable(profile.profile_id, actor=VoiceProfileActor.USER)
        profiles.select(profile.profile_id, actor=VoiceProfileActor.USER)
        session = profiles.bind_session("session-playback")
        requests = ProviderRequestRegistry(capabilities, profiles)
        self.requests = requests
        request = requests.prepare(
            provider_request_id="provider-request-playback-1",
            reply_id="reply-playback-1",
            segment_id="segment-playback-1",
            request_sequence=1,
            text="你好，播放事实。",
            audio_format=self.audio_format,
            provider_generation=1,
            voice_session=session,
        )
        lifecycles = ProviderRequestLifecycleRegistry(requests)
        lifecycles.accept(request.provider_request_id, event_id="lifecycle-accepted")
        lifecycles.synthesis_started(request.provider_request_id, event_id="lifecycle-started")
        self.request = request
        self.profiles = profiles
        lifecycles.record_audio_chunk(self._chunk("audio-chunk-1", 1, 1))
        lifecycles.record_audio_chunk(self._chunk("audio-chunk-2", 2, 2))
        lifecycles.audio_produced(request.provider_request_id, event_id="lifecycle-produced")
        self.lifecycle = lifecycles
        self.registry = ProviderRequestPlaybackRegistry(lifecycles)

    def _chunk(self, event_id: str, index: int, receive_sequence: int) -> ProviderRequestAudioChunk:
        return ProviderRequestAudioChunk(
            event_id=event_id,
            audio_chunk_index=index,
            receive_sequence=receive_sequence,
            provider_request_id=self.request.provider_request_id,
            reply_id=self.request.reply_id,
            segment_id=self.request.segment_id,
            provider_name=self.request.provider_name,
            provider_generation=self.request.provider_generation,
            voice_profile_id=self.request.voice_profile_id,
            voice_profile_version=self.request.voice_profile_version,
            audio_format=self.request.audio_format,
            capability_check=self.request.capability_check,
        )

    def test_playback_facts_progress_in_order_and_preserve_snapshot_identity(self) -> None:
        prepared = self.registry.get(self.request.provider_request_id)
        self.assertEqual(prepared.state, ProviderRequestPlaybackState.NOT_COMMITTED)
        self.assertEqual(prepared.playback_offset_ms, 0)

        committed = self.registry.commit(
            self.request.provider_request_id,
            event_id="playback-commit",
            audio_chunk_event_id="audio-chunk-1",
        )
        started = self.registry.start(
            self.request.provider_request_id,
            event_id="playback-start",
            audio_chunk_event_id="audio-chunk-1",
        )
        partial = self.registry.progress(
            self.request.provider_request_id,
            event_id="playback-partial",
            audio_chunk_event_id="audio-chunk-2",
            playback_offset_ms=120,
        )
        played = self.registry.played(
            self.request.provider_request_id,
            event_id="playback-played",
            audio_chunk_event_id="audio-chunk-2",
            playback_offset_ms=240,
        )

        self.assertEqual(committed.state, ProviderRequestPlaybackState.COMMITTED_NOT_STARTED)
        self.assertEqual(started.state, ProviderRequestPlaybackState.PLAYBACK_STARTED)
        self.assertEqual(partial.state, ProviderRequestPlaybackState.PARTIALLY_PLAYED)
        self.assertEqual(played.state, ProviderRequestPlaybackState.PLAYED)
        self.assertEqual(played.playback_offset_ms, 240)
        self.assertEqual(played.audio_chunk_index, 2)
        self.assertIs(self.registry.get(self.request.provider_request_id), played)
        self.assertIs(self.registry.events_for(self.request.provider_request_id), played.events)
        self.assertEqual(tuple(event.event_id for event in played.events), (
            "playback-commit", "playback-start", "playback-partial", "playback-played"
        ))

    def test_invalid_transition_offset_and_unknown_chunk_are_atomic(self) -> None:
        before = self.registry.get(self.request.provider_request_id)
        with self.assertRaisesRegex(ProviderRequestPlaybackError, "audio_chunk_event_id"):
            self.registry.commit(
                self.request.provider_request_id,
                event_id="reusable-unknown",
                audio_chunk_event_id="missing-chunk",
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), before)

        committed = self.registry.commit(
            self.request.provider_request_id,
            event_id="commit-valid",
            audio_chunk_event_id="audio-chunk-1",
        )
        with self.assertRaisesRegex(ProviderRequestPlaybackError, "played"):
            self.registry.played(
                self.request.provider_request_id,
                event_id="reusable-transition",
                audio_chunk_event_id="audio-chunk-1",
                playback_offset_ms=10,
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), committed)
        self.assertNotIn("reusable-transition", self.registry._event_ids)  # type: ignore[attr-defined]

    def test_offset_must_increase_and_chunk_index_cannot_go_back(self) -> None:
        self.registry.commit(
            self.request.provider_request_id,
            event_id="commit-monotonic",
            audio_chunk_event_id="audio-chunk-2",
        )
        self.registry.start(
            self.request.provider_request_id,
            event_id="start-monotonic",
            audio_chunk_event_id="audio-chunk-2",
        )
        partial = self.registry.progress(
            self.request.provider_request_id,
            event_id="partial-monotonic",
            audio_chunk_event_id="audio-chunk-2",
            playback_offset_ms=100,
        )
        with self.assertRaisesRegex(ProviderRequestPlaybackError, "strictly increasing"):
            self.registry.progress(
                self.request.provider_request_id,
                event_id="same-offset",
                audio_chunk_event_id="audio-chunk-2",
                playback_offset_ms=100,
            )
        with self.assertRaisesRegex(ProviderRequestPlaybackError, "audio_chunk_index 不得倒退"):
            self.registry.progress(
                self.request.provider_request_id,
                event_id="reverse-chunk",
                audio_chunk_event_id="audio-chunk-1",
                playback_offset_ms=120,
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), partial)
        self.assertNotIn("same-offset", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertNotIn("reverse-chunk", self.registry._event_ids)  # type: ignore[attr-defined]

    def test_abort_locks_snapshot_and_rejects_late_events_without_consuming_id(self) -> None:
        self.registry.commit(
            self.request.provider_request_id,
            event_id="commit-abort",
            audio_chunk_event_id="audio-chunk-1",
        )
        aborted = self.registry.abort(
            self.request.provider_request_id,
            event_id="abort-fact",
            audio_chunk_event_id="audio-chunk-1",
            playback_offset_ms=0,
            reason="用户打断",
        )
        with self.assertRaisesRegex(ProviderRequestPlaybackError, "terminal"):
            self.registry.start(
                self.request.provider_request_id,
                event_id="late-after-abort",
                audio_chunk_event_id="audio-chunk-1",
            )
        self.assertIs(self.registry.get(self.request.provider_request_id), aborted)
        self.assertNotIn("late-after-abort", self.registry._event_ids)  # type: ignore[attr-defined]
        self.assertEqual(aborted.state, ProviderRequestPlaybackState.ABORTED)

    def test_cross_request_chunk_and_event_identity_are_isolated(self) -> None:
        other_request = self._prepare_other_request()
        self.lifecycle.accept(other_request.provider_request_id, event_id="other-accepted")
        self.lifecycle.synthesis_started(other_request.provider_request_id, event_id="other-started")
        self.lifecycle.record_audio_chunk(self._chunk_for(other_request, "other-chunk-1"))
        self.lifecycle.audio_produced(other_request.provider_request_id, event_id="other-produced")

        with self.assertRaisesRegex(ProviderRequestPlaybackError, "未绑定当前"):
            self.registry.commit(
                self.request.provider_request_id,
                event_id="cross-request-chunk",
                audio_chunk_event_id="other-chunk-1",
            )
        self.registry.commit(
            other_request.provider_request_id,
            event_id="other-commit",
            audio_chunk_event_id="other-chunk-1",
        )
        self.assertEqual(
            self.registry.get(self.request.provider_request_id).state,
            ProviderRequestPlaybackState.NOT_COMMITTED,
        )
        self.assertEqual(
            self.registry.get(other_request.provider_request_id).reply_id,
            other_request.reply_id,
        )

    def test_failed_record_never_creates_snapshot_or_consumes_id(self) -> None:
        request_id = self.request.provider_request_id
        self.assertNotIn(request_id, self.registry._snapshots)
        with self.assertRaises(ProviderRequestPlaybackError):
            self.registry.commit(request_id, event_id="missing-atomic", audio_chunk_event_id="missing")
        self.assertNotIn(request_id, self.registry._snapshots)
        self.assertNotIn("missing-atomic", self.registry._event_ids)

    def test_generation_fence_rejects_late_playback_without_mutation(self) -> None:
        request_id = self.request.provider_request_id
        committed = self.registry.commit(request_id, event_id="fenced-commit", audio_chunk_event_id="audio-chunk-1")
        self.lifecycle.advance_generation(self.request.provider_name, 2)
        with self.assertRaisesRegex(ProviderRequestPlaybackError, "generation"):
            self.registry.start(request_id, event_id="fenced-start", audio_chunk_event_id="audio-chunk-1")
        self.assertIs(self.registry.get(request_id), committed)
        self.assertNotIn("fenced-start", self.registry._event_ids)

    def test_cancelled_lifecycle_rejects_new_playback_but_allows_abort_of_commit(self) -> None:
        request_id = self.request.provider_request_id
        committed = self.registry.commit(request_id, event_id="before-cancel", audio_chunk_event_id="audio-chunk-1")
        self.lifecycle.cancel(request_id, event_id="lifecycle-cancel", reason="cancelled")
        with self.assertRaises(ProviderRequestPlaybackError):
            self.registry.start(request_id, event_id="after-cancel", audio_chunk_event_id="audio-chunk-1")
        self.assertIs(self.registry.get(request_id), committed)
        self.assertNotIn("after-cancel", self.registry._event_ids)
        aborted = self.registry.abort(request_id, event_id="abort-cancel", audio_chunk_event_id="audio-chunk-1", playback_offset_ms=0, reason="stopped")
        self.assertEqual(aborted.state, ProviderRequestPlaybackState.ABORTED)

    def test_failed_lifecycle_cannot_create_playback_snapshot_or_abort_uncommitted(self) -> None:
        request_id = self.request.provider_request_id
        self.lifecycle.fail(request_id, event_id="lifecycle-fail", reason="local failure")
        for action in (
            lambda: self.registry.commit(request_id, event_id="after-fail-commit", audio_chunk_event_id="audio-chunk-1"),
            lambda: self.registry.abort(request_id, event_id="after-fail-abort", audio_chunk_event_id="audio-chunk-1", playback_offset_ms=0, reason="stop"),
        ):
            with self.assertRaises(ProviderRequestPlaybackError):
                action()
        self.assertNotIn(request_id, self.registry._snapshots)
        self.assertNotIn("after-fail-commit", self.registry._event_ids)
        self.assertNotIn("after-fail-abort", self.registry._event_ids)

    def test_direct_played_event_requires_positive_offset(self) -> None:
        committed = self.registry.commit(self.request.provider_request_id, event_id="for-zero", audio_chunk_event_id="audio-chunk-1")
        with self.assertRaises(ProviderRequestPlaybackError):
            replace(committed.events[0], state=ProviderRequestPlaybackState.PLAYED, playback_offset_ms=0)

    def test_direct_snapshot_rejects_skipped_transition_terminal_append_and_chunk_alias(self) -> None:
        request_id = self.request.provider_request_id
        commit = self.registry.commit(request_id, event_id="replay-commit", audio_chunk_event_id="audio-chunk-1")
        event = commit.events[0]
        with self.assertRaises(ProviderRequestPlaybackError):
            replace(commit, state=ProviderRequestPlaybackState.PLAYED,
                    events=(replace(event, state=ProviderRequestPlaybackState.PLAYED, playback_offset_ms=10),),
                    playback_offset_ms=10)
        self.registry.start(request_id, event_id="replay-start", audio_chunk_event_id="audio-chunk-1")
        played = self.registry.played(request_id, event_id="replay-played", audio_chunk_event_id="audio-chunk-1", playback_offset_ms=10)
        with self.assertRaises(ProviderRequestPlaybackError):
            replace(played, events=played.events + (replace(played.events[-1], event_id="after-played", event_sequence=4, state=ProviderRequestPlaybackState.ABORTED, reason="late"),),
                    state=ProviderRequestPlaybackState.ABORTED)
        with self.assertRaises(ProviderRequestPlaybackError):
            replace(played, events=(event, replace(played.events[1], audio_chunk_event_id="audio-chunk-2"), played.events[-1]))

    def _prepare_other_request(self):
        request = self.requests.prepare(
            provider_request_id="provider-request-playback-2",
            reply_id="reply-playback-2",
            segment_id="segment-playback-2",
            request_sequence=1,
            text="另一句。",
            audio_format=self.audio_format,
            provider_generation=1,
            voice_session=self._new_session("session-playback-2"),
        )
        return request

    def _new_session(self, session_id: str):
        return self.profiles.bind_session(session_id)

    def _chunk_for(self, request, event_id: str):
        return ProviderRequestAudioChunk(
            event_id=event_id,
            audio_chunk_index=1,
            receive_sequence=1,
            provider_request_id=request.provider_request_id,
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_name=request.provider_name,
            provider_generation=request.provider_generation,
            voice_profile_id=request.voice_profile_id,
            voice_profile_version=request.voice_profile_version,
            audio_format=request.audio_format,
            capability_check=request.capability_check,
        )


if __name__ == "__main__":
    unittest.main()