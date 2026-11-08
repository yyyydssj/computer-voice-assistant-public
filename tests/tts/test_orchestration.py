"""P7 opt-in fake-device integration; no microphone, speaker, or network."""
from dataclasses import replace
import unittest

from app.tts.capabilities import AudioFormat, ProviderCapability, ProviderCapabilityRegistry
from app.tts.domain.voice_profile import VoiceProfile, VoiceProfileActor, VoiceProfileRegistry, VoiceProfileType
from app.tts.orchestration import LocalSpeechOrchestrator
from app.tts.routing import TTSRoute


class FakeDevice:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.events = []
        self.speech = []

    def commit(self, entry):
        self.events.append(("commit", entry.segment_id))
        return self.fail_at != "commit"

    def start(self, entry):
        self.events.append(("start", entry.segment_id))
        if self.fail_at == "start":
            return False
        self.speech.append(entry.segment_id)
        return True

    def complete(self, entry):
        self.events.append(("complete", entry.segment_id))
        if self.fail_at == "complete":
            raise TimeoutError("fake device timeout")
        return 100

    def stop(self, entry):
        self.events.append(("stop", entry.segment_id))
        return self.fail_at != "stop"


class FakeSynth:
    def __init__(self, duration=100, fail=False):
        self.duration, self.fail = duration, fail
        self.requests = []

    def synthesize(self, request):
        self.requests.append(request)
        if self.fail:
            raise TimeoutError("fake synthesizer timeout")
        return self.duration


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.fmt = AudioFormat("pcm_s16le", 24000, 1)
        self.profiles = VoiceProfileRegistry()
        profile = VoiceProfile("test-voice", "Test", VoiceProfileType.PRESET,
                               "voicestudio", "fake-ref")
        self.profiles.create(profile, actor=VoiceProfileActor.USER)
        self.profiles.enable(profile.profile_id, actor=VoiceProfileActor.USER)
        self.profiles.select(profile.profile_id, actor=VoiceProfileActor.USER)
        self.capabilities = ProviderCapabilityRegistry((ProviderCapability(
            "voicestudio", True, True, True, True, (self.fmt,), 5),))
        self.device = FakeDevice()
        self.synth = FakeSynth()
        self.orch = LocalSpeechOrchestrator(
            capabilities=self.capabilities, profiles=self.profiles,
            synthesizer=self.synth, device=self.device, audio_format=self.fmt,
            edge_profile_id="edge-preselected")

    def test_fifo_and_identity_match_only_fake_acknowledgements(self):
        result = self.orch.speak(reply_id="reply-1", confirmed_text="你好。再见。")
        self.assertFalse(result.text_fallback)
        self.assertEqual(result.segment_texts, ("你好。", "再见。"))
        self.assertEqual(result.acknowledged_segments, tuple(self.device.speech))
        self.assertEqual([r.text for r in self.synth.requests], list(result.segment_texts))
        self.assertEqual([r.request_sequence for r in self.synth.requests], [1, 2])
        self.assertEqual({r.voice_profile_version for r in self.synth.requests},
                         {self.profiles.selected().version})
        self.assertEqual([e[0] for e in self.device.events],
                         ["commit", "start", "complete"] * 2)
        with self.assertRaises(ValueError):
            self.orch.speak(reply_id="reply-1", confirmed_text="duplicated")

    def test_synthesis_failure_preserves_text_and_never_commits(self):
        self.synth.fail = True
        result = self.orch.speak(reply_id="reply-fail", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(result.text, "第一句。第二句。")
        self.assertFalse(result.acknowledged_segments)
        self.assertFalse(self.device.events)
        self.assertEqual(self.orch.router.snapshot.next_reply_route, TTSRoute.EDGE)
        # An Edge route without explicit user selection must stay text-only.
        next_result = self.orch.speak(reply_id="reply-next", confirmed_text="继续。")
        self.assertTrue(next_result.text_fallback)
        self.assertFalse(self.device.events)

    def test_commit_ambiguity_never_switches_current_segment(self):
        self.device.fail_at = "commit"
        result = self.orch.speak(reply_id="reply-commit", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(len(self.synth.requests), 1)
        self.assertEqual([e[0] for e in self.device.events], ["commit", "stop"])
        self.assertFalse(result.stop_unconfirmed)

    def test_uncertain_device_prevents_subsequent_speech_until_new_instance(self):
        self.device.fail_at = "complete"
        first = self.orch.speak(reply_id="broken-device", confirmed_text="第一句。")
        self.assertTrue(first.text_fallback)
        before = len(self.synth.requests)
        self.device.fail_at = None
        second = self.orch.speak(reply_id="later-reply", confirmed_text="新句。")
        self.assertTrue(second.text_fallback)
        self.assertEqual(len(self.synth.requests), before)

    def test_stop_unconfirmed_cannot_be_reported_as_safely_stopped(self):
        self.device.fail_at = "stop"
        def broken_start(entry):
            self.device.events.append(("start", entry.segment_id))
            return False
        self.device.start = broken_start
        result = self.orch.speak(reply_id="reply-stop", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertTrue(result.stop_unconfirmed)
        self.assertEqual(len(self.synth.requests), 1)

    def test_partial_device_failure_keeps_first_fact_only(self):
        self.device.fail_at = "complete"
        result = self.orch.speak(reply_id="reply-partial", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertFalse(result.acknowledged_segments)
        self.assertEqual(len(self.device.speech), 1)
        self.assertEqual(len(self.synth.requests), 1)
        self.assertEqual(self.orch.router.snapshot.next_reply_route, TTSRoute.EDGE)

    def test_backpressure_is_not_a_device_submission(self):
        self.synth.duration = 8001
        result = self.orch.speak(reply_id="reply-budget", confirmed_text="安全退出。")
        self.assertTrue(result.text_fallback)
        self.assertFalse(self.device.events)

    def test_profile_drift_fails_closed_at_next_sentence(self):
        original = self.device.complete
        def change_profile(entry):
            duration = original(entry)
            current = self.profiles.selected()
            self.profiles.revise(replace(current, version=current.version + 1),
                                 actor=VoiceProfileActor.USER)
            return duration
        self.device.complete = change_profile
        result = self.orch.speak(reply_id="reply-drift", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(len(result.acknowledged_segments), 1)
        self.assertEqual(len(self.device.speech), 1)
        self.assertEqual(len(self.synth.requests), 1)

    def test_edge_only_on_new_reply_after_explicit_user_selection(self):
        self.capabilities.register(ProviderCapability(
            "edge_tts", True, True, True, True, (self.fmt,), 5))
        edge = VoiceProfile("edge-preselected", "Edge", VoiceProfileType.PRESET,
                            "edge_tts", "fake-edge")
        self.profiles.create(edge, actor=VoiceProfileActor.USER)
        self.profiles.enable(edge.profile_id, actor=VoiceProfileActor.USER)
        self.synth.fail = True
        first = self.orch.speak(reply_id="primary-fails", confirmed_text="第一个。第二个。")
        self.assertTrue(first.text_fallback)
        self.assertEqual(len(self.synth.requests), 1)
        self.profiles.select(edge.profile_id, actor=VoiceProfileActor.USER)
        self.assertTrue(self.orch.router.snapshot.edge_configured)
        self.assertEqual(self.orch.router.snapshot.next_reply_route, TTSRoute.EDGE)
        self.synth.fail = False
        second = self.orch.speak(reply_id="edge-next", confirmed_text="下一轮。")
        self.assertFalse(second.text_fallback)
        self.assertEqual(self.synth.requests[-1].provider_name, "edge_tts")
        self.assertEqual(second.acknowledged_segments, tuple(self.device.speech))
        self.synth.fail = True
        third = self.orch.speak(reply_id="edge-fails", confirmed_text="失败轮。")
        self.assertTrue(third.text_fallback)
        self.assertEqual(self.orch.router.snapshot.next_reply_route, TTSRoute.TEXT)
        self.synth.fail = False
        fourth = self.orch.speak(reply_id="text-only", confirmed_text="保持文字。")
        self.assertTrue(fourth.text_fallback)
        self.assertEqual(len(self.synth.requests), 3)

    def test_fail_closed_for_invalid_reply_id(self):
        with self.assertRaises(ValueError):
            self.orch.speak(reply_id="", confirmed_text="安全。")
        self.assertFalse(self.orch._busy)
        self.assertFalse(self.device.events)

    def test_cancel_during_synthesis_blocks_device_and_old_reply(self):
        original = self.synth.synthesize
        def interrupt(request):
            duration = original(request)
            self.orch.cancel(request.reply_id)
            return duration
        self.synth.synthesize = interrupt
        result = self.orch.speak(reply_id="reply-cancel", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertEqual(len(self.synth.requests), 1)
        self.assertFalse(self.device.events)
        with self.assertRaises(ValueError):
            self.orch.cancel("reply-cancel")

    def test_cancel_after_start_stops_and_never_replays(self):
        original = self.device.start
        def interrupt(entry):
            ack = original(entry)
            self.orch.cancel(entry.reply_id)
            return ack
        self.device.start = interrupt
        result = self.orch.speak(reply_id="reply-start", confirmed_text="第一句。第二句。")
        self.assertTrue(result.text_fallback)
        self.assertFalse(result.acknowledged_segments)
        self.assertEqual([e[0] for e in self.device.events], ["commit", "start", "stop"])

    def test_profile_drift_during_synthesis_prevents_device_commit(self):
        original = self.synth.synthesize
        def drift(request):
            duration = original(request)
            current = self.profiles.selected()
            self.profiles.revise(replace(current, version=current.version + 1),
                                 actor=VoiceProfileActor.USER)
            return duration
        self.synth.synthesize = drift
        result = self.orch.speak(reply_id="reply-voice-drift", confirmed_text="一句。")
        self.assertTrue(result.text_fallback)
        self.assertFalse(self.device.events)

    def test_device_callback_reentrant_reply_rejected(self):
        original = self.device.commit
        def reenter(entry):
            with self.assertRaises(ValueError):
                self.orch.speak(reply_id="other-reply", confirmed_text="不允许。")
            return original(entry)
        self.device.commit = reenter
        result = self.orch.speak(reply_id="reply-reentrant", confirmed_text="允许。")
        self.assertFalse(result.text_fallback)


if __name__ == "__main__":
    unittest.main()