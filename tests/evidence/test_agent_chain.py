from __future__ import annotations

import hashlib
import unittest

from app.agent.adapter import AgentTurnOutcome
from app.evidence.agent_chain import (
    AgentChainConfigurationError,
    AgentChainConfig,
    AgentChainEvidenceRunner,
    PlaybackObservation,
    SynthesisResult,
)
from app.evidence.ledger import P7EvidenceLedger, sha256_text


class FakeAgent:
    def __init__(self, outcome: AgentTurnOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str]] = []

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.calls.append((request_id, session_id, text))
        return self.outcome


class FakeVoiceStudio:
    def __init__(self, result: SynthesisResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def synthesize(self, text: str, *, request_id: str, session_id: str) -> SynthesisResult:
        self.calls.append((text, request_id, session_id))
        return self.result


class FakePlaybackObserver:
    def __init__(self, observation: PlaybackObservation) -> None:
        self.observation = observation
        self.calls: list[tuple[bytes, str, str]] = []

    def observe(self, audio: bytes, *, request_id: str, session_id: str) -> PlaybackObservation:
        self.calls.append((audio, request_id, session_id))
        return self.observation


def completed_outcome(
    *,
    request_id: str = "req-1",
    session_id: str = "session-1",
    text: str = "Agent 原文",
) -> AgentTurnOutcome:
    return AgentTurnOutcome(
        request_id=request_id,
        session_id=session_id,
        completed=True,
        text=text,
        finish_reason="stop",
    )


class AgentChainEvidenceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AgentChainConfig(
            enabled=True,
            evidence_kind="offline_contract",
            agent_endpoint="http://127.0.0.1:3901",
            voicestudio_endpoint="http://localhost:3900",
        )

    def test_default_runner_is_inert_and_blocked(self) -> None:
        result = AgentChainEvidenceRunner().run("不会触发任何默认路径")

        self.assertTrue(result.blocked)
        self.assertEqual(result.ledger.evidence_kind, "offline_contract")
        self.assertIn("explicit_opt_in_required", result.ledger.blocked_reasons)
        self.assertIsNone(result.agent_outcome)
        self.assertIsNone(result.synthesis)
        self.assertIsNone(result.playback)

    def test_only_localhost_or_loopback_endpoints_are_accepted(self) -> None:
        for endpoint in (
            "https://example.com",
            "http://192.168.1.10:3900",
            "http://user:pass@127.0.0.1:3900",
            "ftp://127.0.0.1:3900",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(AgentChainConfigurationError):
                    AgentChainConfig(agent_endpoint=endpoint)

        for endpoint in (
            "http://localhost:3900",
            "http://127.0.0.1:3900/path",
            "https://[::1]:3900",
        ):
            with self.subTest(endpoint=endpoint):
                config = AgentChainConfig(agent_endpoint=endpoint)
                self.assertEqual(config.agent_endpoint, endpoint)

    def test_real_device_requires_an_explicit_live_opt_in(self) -> None:
        with self.assertRaisesRegex(AgentChainConfigurationError, "live_opt_in"):
            AgentChainConfig(evidence_kind="real_device")
        with self.assertRaisesRegex(AgentChainConfigurationError, "real_device"):
            AgentChainConfig(evidence_kind="offline_contract", live_opt_in=True)

    def test_injected_chain_preserves_exact_text_and_records_all_hashes_and_diagnostics(self) -> None:
        agent = FakeAgent(completed_outcome(request_id="request-42", session_id="session-42", text="这是 Agent 返回的原文。"))
        voice = FakeVoiceStudio(SynthesisResult(
            b"fake-wav-bytes",
            audio_id="audio-42",
            diagnostics={"client": "fake-local-voicestudio", "http_status": 200},
        ))
        playback = FakePlaybackObserver(PlaybackObservation(
            backend_stop_ack=True,
            loopback_output_stopped=True,
            acoustic_output_stopped=True,
            diagnostics={"backend": "fake-observer", "frames": 1234},
        ))
        runner = AgentChainEvidenceRunner(
            config=self.config,
            agent_adapter=agent,
            voicestudio=voice,
            playback_observer=playback,
        )

        result = runner.run("用户请求", request_id="request-42", session_id="session-42")
        ledger = result.ledger

        self.assertEqual(agent.calls, [("request-42", "session-42", "用户请求")])
        self.assertEqual(voice.calls, [("这是 Agent 返回的原文。", "request-42", "session-42")])
        self.assertEqual(playback.calls, [(b"fake-wav-bytes", "request-42", "session-42")])
        self.assertEqual(ledger.request_id, "request-42")
        self.assertEqual(ledger.source_text_sha256, sha256_text("这是 Agent 返回的原文。"))
        self.assertEqual(ledger.audio_sha256, hashlib.sha256(b"fake-wav-bytes").hexdigest())
        self.assertEqual(ledger.backend_stop_ack, True)
        self.assertEqual(ledger.loopback_output_stopped, True)
        self.assertEqual(ledger.acoustic_output_stopped, True)
        self.assertEqual(ledger.speaker_output_stopped, True)
        self.assertEqual(ledger.metadata["audio_id"], "audio-42")
        self.assertEqual(
            ledger.metadata["diagnostics"],
            {
                "voicestudio": {"client": "fake-local-voicestudio", "http_status": 200},
                "playback": {"backend": "fake-observer", "frames": 1234},
            },
        )
        self.assertEqual(
            [event.name for event in ledger.events],
            ["AGENT_REPLY_ACCEPTED", "VOICESTUDIO_AUDIO_ACCEPTED", "PLAYBACK_OBSERVED"],
        )
        # The fake chain proves the seam only.  No user-heard fact is inferred.
        self.assertEqual(ledger.evidence_kind, "offline_contract")
        self.assertTrue(result.blocked)
        self.assertIn("user_heard_pending", ledger.blocked_reasons)

    def test_missing_any_required_injected_port_is_blocked_without_substitution(self) -> None:
        agent = FakeAgent(completed_outcome())
        voice = FakeVoiceStudio(SynthesisResult(b"audio"))
        playback = FakePlaybackObserver(PlaybackObservation(
            backend_stop_ack=True,
            loopback_output_stopped=True,
            acoustic_output_stopped=True,
        ))
        cases = (
            (None, voice, playback, "agent_adapter_missing"),
            (agent, None, playback, "voicestudio_client_missing"),
            (agent, voice, None, "playback_observer_missing"),
        )
        for injected_agent, injected_voice, injected_playback, reason in cases:
            with self.subTest(reason=reason):
                result = AgentChainEvidenceRunner(
                    config=self.config,
                    agent_adapter=injected_agent,
                    voicestudio=injected_voice,
                    playback_observer=injected_playback,
                ).run("request")
                self.assertTrue(result.blocked)
                self.assertIn(reason, result.ledger.blocked_reasons)
                self.assertEqual(result.ledger.evidence_kind, "offline_contract")
                self.assertIsNone(result.ledger.audio_sha256)

    def test_agent_failure_or_id_mismatch_never_reaches_synthesis(self) -> None:
        agent = FakeAgent(completed_outcome(request_id="different-request"))
        voice = FakeVoiceStudio(SynthesisResult(b"audio"))
        playback = FakePlaybackObserver(PlaybackObservation())
        runner = AgentChainEvidenceRunner(
            config=self.config,
            agent_adapter=agent,
            voicestudio=voice,
            playback_observer=playback,
        )

        result = runner.run("request", request_id="expected-request")

        self.assertTrue(result.blocked)
        self.assertIn("agent_request_id_mismatch", result.ledger.blocked_reasons)
        self.assertEqual(voice.calls, [])
        self.assertEqual(playback.calls, [])
        self.assertIsNone(result.ledger.audio_sha256)

    def test_invalid_synthesis_or_playback_contract_is_blocked(self) -> None:
        request_id = "request-validated"
        agent = FakeAgent(completed_outcome(request_id=request_id, session_id="session-validated"))

        class InvalidVoiceStudio:
            def synthesize(self, text: str, *, request_id: str, session_id: str) -> object:
                return {"audio": b"not-a-contract"}

        result = AgentChainEvidenceRunner(
            config=self.config,
            agent_adapter=agent,
            voicestudio=InvalidVoiceStudio(),
            playback_observer=FakePlaybackObserver(PlaybackObservation()),
        ).run("request", request_id=request_id, session_id="session-validated")
        self.assertIn("voicestudio_contract_invalid_type", result.ledger.blocked_reasons)
        self.assertIsNone(result.ledger.audio_sha256)

        class InvalidPlaybackObserver:
            def observe(self, audio: bytes, *, request_id: str, session_id: str) -> object:
                return {"backend_stop_ack": True}

        result = AgentChainEvidenceRunner(
            config=self.config,
            agent_adapter=agent,
            voicestudio=FakeVoiceStudio(SynthesisResult(b"audio")),
            playback_observer=InvalidPlaybackObserver(),
        ).run("request", request_id=request_id, session_id="session-validated")
        self.assertIn("playback_contract_invalid_type", result.ledger.blocked_reasons)
        self.assertIsNotNone(result.ledger.audio_sha256)


    def test_explicit_empty_or_non_string_ids_are_rejected_instead_of_regenerated(self) -> None:
        runner = AgentChainEvidenceRunner()
        for request_id, session_id in (("", None), ("   ", None), (123, None), (None, ""), (None, "   "), (None, 123)):
            with self.subTest(request_id=request_id, session_id=session_id):
                with self.assertRaises(ValueError):
                    runner.run("request", request_id=request_id, session_id=session_id)  # type: ignore[arg-type]

    def test_agent_session_id_mismatch_blocks_before_synthesis(self) -> None:
        request_id = "request-session-check"
        agent = FakeAgent(AgentTurnOutcome(
            request_id=request_id,
            session_id="different-session",
            completed=True,
            text="Agent ??",
            finish_reason="stop",
        ))
        voice = FakeVoiceStudio(SynthesisResult(b"audio"))
        result = AgentChainEvidenceRunner(
            config=self.config,
            agent_adapter=agent,
            voicestudio=voice,
            playback_observer=FakePlaybackObserver(PlaybackObservation()),
        ).run("request", request_id=request_id, session_id="expected-session")
        self.assertTrue(result.blocked)
        self.assertIn("agent_session_id_mismatch", result.ledger.blocked_reasons)
        self.assertEqual(voice.calls, [])
        self.assertIsNone(result.ledger.audio_sha256)

    def test_loopback_endpoint_rejects_invalid_ports(self) -> None:
        for endpoint in (
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:not-a-port",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(AgentChainConfigurationError):
                    AgentChainConfig(agent_endpoint=endpoint)

    def test_offline_contract_is_never_promoted_to_real_device(self) -> None:
        ledger = P7EvidenceLedger(
            evidence_kind="offline_contract",
            backend_stop_ack=True,
            loopback_output_stopped=True,
            acoustic_output_stopped=True,
        )
        ledger.record_user_heard(batch_id=ledger.batch_id, heard=False)
        self.assertEqual(ledger.finalize(), "blocked")
        self.assertEqual(ledger.evidence_kind, "offline_contract")
        self.assertIn("evidence_kind_not_real_device", ledger.blocked_reasons)



if __name__ == "__main__":
    unittest.main()

