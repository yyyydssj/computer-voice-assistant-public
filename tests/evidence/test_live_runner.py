from __future__ import annotations

import unittest

from app.agent.adapter import AgentTurnOutcome
from app.evidence.agent_chain import SynthesisResult
from app.evidence.live_runner import (
    LiveEvidenceConfig,
    LiveEvidenceRunner,
    LivePlaybackEvidence,
    LiveRunnerConfigurationError,
)
from app.evidence.ledger import sha256_text


class FakeAgent:
    def __init__(self, outcome: AgentTurnOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str]] = []

    def submit_chat(self, **kwargs: str) -> AgentTurnOutcome:
        self.calls.append(kwargs)
        return self.outcome


class FakeVoiceStudio:
    def __init__(self, result: SynthesisResult) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    def synthesize(self, text: str, **kwargs: str) -> SynthesisResult:
        self.calls.append({"text": text, **kwargs})
        return self.result


class FakeDevice:
    def __init__(self, evidence: LivePlaybackEvidence) -> None:
        self.evidence = evidence
        self.calls: list[dict[str, object]] = []

    def play_and_capture(self, audio: bytes, **kwargs: object) -> LivePlaybackEvidence:
        self.calls.append({"audio": audio, **kwargs})
        return self.evidence


class LiveEvidenceRunnerTests(unittest.TestCase):
    def _ports(self, *, request_id: str = "req-1", session_id: str = "sess-1"):
        agent = FakeAgent(AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text="exact agent reply",
            finish_reason="stop",
        ))
        voice = FakeVoiceStudio(SynthesisResult(
            audio=b"RIFF-local-wav",
            audio_id="audio-1",
            diagnostics={"engine": "local"},
        ))
        device = FakeDevice(LivePlaybackEvidence(
            backend_stop_ack=True,
            loopback_output_stopped=True,
            acoustic_output_stopped=True,
            capture_valid=True,
            diagnostics={"serial": True},
        ))
        return agent, voice, device

    def test_explicit_empty_ids_are_rejected_instead_of_regenerated(self) -> None:
        runner = LiveEvidenceRunner()
        for request_id, session_id in (("", None), ("   ", None), (None, ""), (None, "   ")):
            with self.subTest(request_id=request_id, session_id=session_id):
                with self.assertRaises(ValueError):
                    runner.run("request", request_id=request_id, session_id=session_id)  # type: ignore[arg-type]

    def test_default_runner_is_inert_and_never_calls_ports(self) -> None:
        agent, voice, device = self._ports()
        result = LiveEvidenceRunner(agent=agent, voicestudio=voice, device=device).run(
            "user input", request_id="req-1", session_id="sess-1"
        )
        self.assertEqual(result.ledger.status, "blocked")
        self.assertIn("live_runner_disabled", result.ledger.blocked_reasons)
        self.assertEqual(agent.calls, [])
        self.assertEqual(voice.calls, [])
        self.assertEqual(device.calls, [])
        self.assertIsNone(result.ledger.user_heard)

    def test_real_opt_in_keeps_exact_reply_and_never_infers_user_heard(self) -> None:
        agent, voice, device = self._ports()
        runner = LiveEvidenceRunner(
            LiveEvidenceConfig(enabled=True, evidence_kind="real_device", live_opt_in=True),
            agent=agent, voicestudio=voice, device=device,
        )
        result = runner.run("user input", request_id="req-1", session_id="sess-1")
        self.assertEqual(agent.calls[0]["request_id"], "req-1")
        self.assertEqual(voice.calls[0]["text"], "exact agent reply")
        self.assertEqual(device.calls[0]["batch_id"], result.ledger.batch_id)
        self.assertEqual(result.ledger.source_text_sha256, sha256_text("exact agent reply"))
        self.assertEqual(result.ledger.backend_stop_ack, True)
        self.assertEqual(result.ledger.speaker_output_stopped, True)
        self.assertIsNone(result.ledger.user_heard)
        self.assertEqual(result.ledger.status, "blocked")
        self.assertIn("user_heard_pending", result.ledger.blocked_reasons)

    def test_identity_mismatch_blocks_before_tts(self) -> None:
        agent, voice, device = self._ports(request_id="other", session_id="sess-1")
        runner = LiveEvidenceRunner(
            LiveEvidenceConfig(enabled=True, evidence_kind="real_device", live_opt_in=True),
            agent=agent, voicestudio=voice, device=device,
        )
        result = runner.run("user input", request_id="req-1", session_id="sess-1")
        self.assertIn("agent_identity_mismatch", result.ledger.blocked_reasons)
        self.assertEqual(voice.calls, [])
        self.assertEqual(device.calls, [])

    def test_invalid_capture_cannot_be_promoted(self) -> None:
        agent, voice, _ = self._ports()
        device = FakeDevice(LivePlaybackEvidence(
            backend_stop_ack=True,
            loopback_output_stopped=None,
            acoustic_output_stopped=None,
            capture_valid=False,
        ))
        runner = LiveEvidenceRunner(
            LiveEvidenceConfig(enabled=True, evidence_kind="real_device", live_opt_in=True),
            agent=agent, voicestudio=voice, device=device,
        )
        result = runner.run("user input", request_id="req-1", session_id="sess-1")
        self.assertIn("capture_invalid", result.ledger.blocked_reasons)
        self.assertIsNone(result.ledger.speaker_output_stopped)

    def test_configuration_requires_explicit_live_opt_in(self) -> None:
        with self.assertRaisesRegex(LiveRunnerConfigurationError, "live_opt_in"):
            LiveEvidenceConfig(enabled=True, evidence_kind="real_device")

    def test_text_bound_is_enforced(self) -> None:
        agent, voice, device = self._ports()
        runner = LiveEvidenceRunner(agent=agent, voicestudio=voice, device=device)
        with self.assertRaisesRegex(LiveRunnerConfigurationError, "bound"):
            runner.run("x" * 32769)


if __name__ == "__main__":
    unittest.main()
