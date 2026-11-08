"""Explicit opt-in Agent -> text -> VoiceStudio -> device evidence runner.

The runner is a contract boundary, not a default runtime path.  It never
constructs an Agent, opens a socket, starts playback, or records audio by
itself.  A caller must inject every side-effecting port and explicitly enable
the runner.  Endpoint configuration is fail-closed to localhost/loopback.

Offline fakes must use ``evidence_kind='offline_contract'`` (or a controlled
fixture kind); they are deliberately never promoted to ``real_device``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse
import uuid

from app.agent.adapter import AgentTurnOutcome
from app.evidence.ledger import EvidenceKind, P7EvidenceLedger, sha256_text


class AgentChainConfigurationError(ValueError):
    """Invalid opt-in or non-local endpoint configuration."""


class AgentChainContractError(RuntimeError):
    """An injected port returned a value outside the evidence contract."""


class AgentAdapterPort(Protocol):
    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome: ...


class VoiceStudioClientPort(Protocol):
    def synthesize(self, text: str, *, request_id: str, session_id: str) -> "SynthesisResult": ...


class PlaybackObserverPort(Protocol):
    def observe(self, audio: bytes, *, request_id: str, session_id: str) -> "PlaybackObservation": ...


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Audio returned by an injected local VoiceStudio/TTS client."""

    audio: bytes
    audio_id: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.audio) is not bytes or not self.audio:
            raise AgentChainContractError("synthesis audio must be non-empty bytes")
        if self.audio_id is not None and (type(self.audio_id) is not str or not self.audio_id.strip()):
            raise AgentChainContractError("audio_id must be a non-empty string when present")
        if not isinstance(self.diagnostics, Mapping):
            raise AgentChainContractError("synthesis diagnostics must be a mapping")
        object.__setattr__(self, "diagnostics", dict(self.diagnostics))


@dataclass(frozen=True, slots=True)
class PlaybackObservation:
    """Facts supplied by the injected playback/measurement observer.

    These values are intentionally independent.  Backend acknowledgement is
    not physical speaker silence, and neither is a user-heard assertion.
    """

    backend_stop_ack: bool | None = None
    loopback_output_stopped: bool | None = None
    acoustic_output_stopped: bool | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "backend_stop_ack",
            "loopback_output_stopped",
            "acoustic_output_stopped",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise AgentChainContractError(f"{name} must be bool or None")
        if not isinstance(self.diagnostics, Mapping):
            raise AgentChainContractError("playback diagnostics must be a mapping")
        object.__setattr__(self, "diagnostics", dict(self.diagnostics))


@dataclass(frozen=True, slots=True)
class AgentChainConfig:
    """Explicit activation and local endpoint policy for one bounded run."""

    enabled: bool = False
    agent_endpoint: str = "http://127.0.0.1:3901"
    voicestudio_endpoint: str = "http://127.0.0.1:3900"
    evidence_kind: EvidenceKind = "offline_contract"
    live_opt_in: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.live_opt_in) is not bool:
            raise AgentChainConfigurationError("enabled and live_opt_in must be bool")
        if type(self.evidence_kind) is not str or self.evidence_kind not in {
            "real_device",
            "controlled_fault_fixture",
            "offline_contract",
        }:
            raise AgentChainConfigurationError("unsupported evidence_kind")
        _require_loopback_endpoint("agent_endpoint", self.agent_endpoint)
        _require_loopback_endpoint("voicestudio_endpoint", self.voicestudio_endpoint)
        if self.evidence_kind == "real_device" and not self.live_opt_in:
            raise AgentChainConfigurationError(
                "real_device evidence requires explicit live_opt_in=True"
            )
        if self.live_opt_in and self.evidence_kind != "real_device":
            raise AgentChainConfigurationError(
                "live_opt_in requires evidence_kind='real_device'"
            )


@dataclass(frozen=True, slots=True)
class AgentChainRunResult:
    ledger: P7EvidenceLedger
    agent_outcome: AgentTurnOutcome | None = None
    synthesis: SynthesisResult | None = None
    playback: PlaybackObservation | None = None

    @property
    def blocked(self) -> bool:
        # A complete offline/fixture contract is still blocked as a P7 real-device
        # result.  ``ledger.status`` remains useful for contract completeness,
        # while evidence_kind prevents test doubles from being promoted.
        return self.ledger.status == "blocked" or self.ledger.evidence_kind != "real_device"


class AgentChainEvidenceRunner:
    """Run one explicitly enabled, dependency-injected evidence chain.

    The default constructor is inert.  ``run`` returns a blocked ledger when
    opt-in or any required stage is absent; it does not silently substitute a
    fake result or call a default provider/device.
    """

    def __init__(
        self,
        *,
        config: AgentChainConfig | None = None,
        agent_adapter: AgentAdapterPort | None = None,
        voicestudio: VoiceStudioClientPort | None = None,
        playback_observer: PlaybackObserverPort | None = None,
    ) -> None:
        self.config = config or AgentChainConfig()
        self.agent_adapter = agent_adapter
        self.voicestudio = voicestudio
        self.playback_observer = playback_observer

    def run(
        self,
        text: str,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentChainRunResult:
        if type(text) is not str or not text.strip():
            raise ValueError("text must be a non-empty string")
        if request_id is None:
            request_id = "agent-chain-" + uuid.uuid4().hex[:16]
        elif type(request_id) is not str or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if session_id is None:
            session_id = "agent-chain-session-" + uuid.uuid4().hex[:16]
        elif type(session_id) is not str or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")

        ledger = P7EvidenceLedger(
            evidence_kind=self.config.evidence_kind,
            request_id=request_id,
        )
        ledger.metadata.update({
            "session_id": session_id,
            "input_text_sha256": sha256_text(text),
            "agent_endpoint": self.config.agent_endpoint,
            "voicestudio_endpoint": self.config.voicestudio_endpoint,
            "explicit_opt_in": self.config.enabled,
            "live_opt_in": self.config.live_opt_in,
            "diagnostics": {},
        })

        if not self.config.enabled:
            self._block(ledger, "explicit_opt_in_required")
            return self._finish(ledger)
        if self.config.evidence_kind == "real_device" and not self.config.live_opt_in:
            self._block(ledger, "live_opt_in_required_for_real_device")
            return self._finish(ledger)

        missing = (
            ("agent_adapter_missing", self.agent_adapter),
            ("voicestudio_client_missing", self.voicestudio),
            ("playback_observer_missing", self.playback_observer),
        )
        for reason, dependency in missing:
            if dependency is None:
                self._block(ledger, reason)
        if ledger.blocked_reasons:
            return self._finish(ledger)

        outcome: AgentTurnOutcome
        try:
            outcome = self.agent_adapter.submit_chat(  # type: ignore[union-attr]
                request_id=request_id,
                session_id=session_id,
                text=text,
            )
        except Exception as exc:
            self._block(ledger, f"agent_call_failed:{type(exc).__name__}")
            return self._finish(ledger)
        if not isinstance(outcome, AgentTurnOutcome):
            self._block(ledger, "agent_contract_invalid_type")
            return self._finish(ledger)
        if outcome.request_id != request_id:
            self._block(ledger, "agent_request_id_mismatch")
            return AgentChainRunResult(ledger=self._finish(ledger).ledger, agent_outcome=outcome)
        if outcome.session_id != session_id:
            self._block(ledger, "agent_session_id_mismatch")
            return AgentChainRunResult(ledger=self._finish(ledger).ledger, agent_outcome=outcome)
        if not outcome.completed or outcome.failure is not None:
            self._block(ledger, "agent_reply_incomplete")
            return AgentChainRunResult(ledger=self._finish(ledger).ledger, agent_outcome=outcome)
        if type(outcome.text) is not str or not outcome.text.strip():
            self._block(ledger, "agent_reply_text_missing")
            return AgentChainRunResult(ledger=self._finish(ledger).ledger, agent_outcome=outcome)
        if type(outcome.finish_reason) is not str or not outcome.finish_reason.strip():
            self._block(ledger, "agent_finish_reason_missing")
            return AgentChainRunResult(ledger=self._finish(ledger).ledger, agent_outcome=outcome)

        ledger.source_text_sha256 = sha256_text(outcome.text)
        ledger.add_event("AGENT_REPLY_ACCEPTED", request_id=request_id)

        synthesis: SynthesisResult
        try:
            synthesis = self.voicestudio.synthesize(  # type: ignore[union-attr]
                outcome.text,
                request_id=request_id,
                session_id=session_id,
            )
        except Exception as exc:
            self._block(ledger, f"voicestudio_call_failed:{type(exc).__name__}")
            return AgentChainRunResult(
                ledger=self._finish(ledger).ledger,
                agent_outcome=outcome,
            )
        if not isinstance(synthesis, SynthesisResult):
            self._block(ledger, "voicestudio_contract_invalid_type")
            return AgentChainRunResult(
                ledger=self._finish(ledger).ledger,
                agent_outcome=outcome,
            )
        ledger.set_audio(synthesis.audio)
        ledger.metadata["audio_id"] = synthesis.audio_id
        ledger.metadata["diagnostics"]["voicestudio"] = dict(synthesis.diagnostics)
        ledger.add_event(
            "VOICESTUDIO_AUDIO_ACCEPTED",
            request_id=request_id,
            audio_id=synthesis.audio_id,
            audio_sha256=ledger.audio_sha256,
        )

        playback: PlaybackObservation
        try:
            playback = self.playback_observer.observe(  # type: ignore[union-attr]
                synthesis.audio,
                request_id=request_id,
                session_id=session_id,
            )
        except Exception as exc:
            self._block(ledger, f"playback_observer_failed:{type(exc).__name__}")
            return AgentChainRunResult(
                ledger=self._finish(ledger).ledger,
                agent_outcome=outcome,
                synthesis=synthesis,
            )
        if not isinstance(playback, PlaybackObservation):
            self._block(ledger, "playback_contract_invalid_type")
            return AgentChainRunResult(
                ledger=self._finish(ledger).ledger,
                agent_outcome=outcome,
                synthesis=synthesis,
            )
        ledger.backend_stop_ack = playback.backend_stop_ack
        ledger.loopback_output_stopped = playback.loopback_output_stopped
        ledger.acoustic_output_stopped = playback.acoustic_output_stopped
        ledger.metadata["diagnostics"]["playback"] = dict(playback.diagnostics)
        ledger.add_event(
            "PLAYBACK_OBSERVED",
            request_id=request_id,
            backend_stop_ack=playback.backend_stop_ack,
            loopback_output_stopped=playback.loopback_output_stopped,
            acoustic_output_stopped=playback.acoustic_output_stopped,
        )
        return AgentChainRunResult(
            ledger=self._finish(ledger).ledger,
            agent_outcome=outcome,
            synthesis=synthesis,
            playback=playback,
        )

    @staticmethod
    def _block(ledger: P7EvidenceLedger, reason: str) -> None:
        if reason not in ledger.blocked_reasons:
            ledger.blocked_reasons.append(reason)
        ledger.add_event("BLOCKED", reason=reason)

    @classmethod
    def _finish(cls, ledger: P7EvidenceLedger) -> AgentChainRunResult:
        ledger.finalize()
        return AgentChainRunResult(ledger=ledger)


def _require_loopback_endpoint(name: str, endpoint: str) -> None:
    if type(endpoint) is not str or not endpoint.strip():
        raise AgentChainConfigurationError(f"{name} must be a non-empty URL")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentChainConfigurationError(f"{name} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise AgentChainConfigurationError(f"{name} must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AgentChainConfigurationError(f"{name} must contain a valid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise AgentChainConfigurationError(f"{name} must contain a valid port")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise AgentChainConfigurationError(
            f"{name} must target localhost or a loopback address"
        ) from exc
    if not address.is_loopback:
        raise AgentChainConfigurationError(
            f"{name} must target localhost or a loopback address"
        )


__all__ = [
    "AgentAdapterPort",
    "AgentChainConfigurationError",
    "AgentChainContractError",
    "AgentChainConfig",
    "AgentChainEvidenceRunner",
    "AgentChainRunResult",
    "PlaybackObservation",
    "PlaybackObserverPort",
    "SynthesisResult",
    "VoiceStudioClientPort",
]
