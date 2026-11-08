"""Explicitly opt-in, single-process P7 live evidence orchestration.

This module is an orchestration boundary only.  It never constructs a real
Agent, imports a network client, opens an audio device, or infers that a user
heard anything.  A caller must inject all side-effecting ports and explicitly
opt into ``real_device`` evidence.  The injected device port is responsible for
serially starting playback, observing backend stop, and capturing the loopback
and independent microphone windows while the unique batch is active.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import threading
from typing import Any, Mapping, Protocol
import uuid

from app.agent.adapter import AgentTurnOutcome
from app.evidence.agent_chain import SynthesisResult
from app.evidence.ledger import EvidenceKind, P7EvidenceLedger, sha256_text


class LiveRunnerConfigurationError(ValueError):
    """Invalid live-evidence activation or bounded-run configuration."""


class LiveRunnerContractError(RuntimeError):
    """An injected live-evidence port violated its contract."""


class AgentPort(Protocol):
    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome: ...


class VoiceStudioPort(Protocol):
    def synthesize(self, text: str, *, request_id: str, session_id: str) -> SynthesisResult: ...


@dataclass(frozen=True, slots=True)
class LivePlaybackEvidence:
    """Facts returned by one serial playback/capture operation."""

    backend_stop_ack: bool | None = None
    loopback_output_stopped: bool | None = None
    acoustic_output_stopped: bool | None = None
    capture_valid: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "backend_stop_ack",
            "loopback_output_stopped",
            "acoustic_output_stopped",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise LiveRunnerContractError(f"{name} must be bool or None")
        if type(self.capture_valid) is not bool:
            raise LiveRunnerContractError("capture_valid must be bool")
        if not isinstance(self.diagnostics, Mapping):
            raise LiveRunnerContractError("diagnostics must be a mapping")
        object.__setattr__(self, "diagnostics", dict(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DeviceEvidencePort(Protocol):
    def play_and_capture(
        self,
        audio: bytes,
        *,
        request_id: str,
        session_id: str,
        batch_id: str,
    ) -> LivePlaybackEvidence: ...


@dataclass(frozen=True, slots=True)
class LiveEvidenceConfig:
    """Activation gate for one bounded P7 run.

    The safe default is inert and cannot be promoted to real-device evidence.
    """

    enabled: bool = False
    evidence_kind: EvidenceKind = "offline_contract"
    live_opt_in: bool = False
    max_text_chars: int = 32_768
    max_audio_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.live_opt_in) is not bool:
            raise LiveRunnerConfigurationError("enabled and live_opt_in must be bool")
        if self.evidence_kind not in {
            "real_device",
            "controlled_fault_fixture",
            "offline_contract",
        }:
            raise LiveRunnerConfigurationError("unsupported evidence_kind")
        if self.evidence_kind == "real_device" and not self.live_opt_in:
            raise LiveRunnerConfigurationError(
                "real_device evidence requires explicit live_opt_in=True"
            )
        if self.live_opt_in and self.evidence_kind != "real_device":
            raise LiveRunnerConfigurationError(
                "live_opt_in requires evidence_kind='real_device'"
            )
        if self.max_text_chars <= 0 or self.max_audio_bytes <= 0:
            raise LiveRunnerConfigurationError("live evidence bounds must be positive")


@dataclass(slots=True)
class LiveEvidenceRunResult:
    ledger: P7EvidenceLedger
    agent_outcome: AgentTurnOutcome | None = None
    synthesis: SynthesisResult | None = None
    playback: LivePlaybackEvidence | None = None


class LiveEvidenceRunner:
    """Run Agent -> exact reply -> VoiceStudio -> one device evidence batch."""

    def __init__(
        self,
        config: LiveEvidenceConfig | None = None,
        *,
        agent: AgentPort | None = None,
        voicestudio: VoiceStudioPort | None = None,
        device: DeviceEvidencePort | None = None,
    ) -> None:
        self.config = config or LiveEvidenceConfig()
        self.agent = agent
        self.voicestudio = voicestudio
        self.device = device
        self._run_lock = threading.Lock()

    def run(
        self,
        text: str,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> LiveEvidenceRunResult:
        if request_id is None:
            request_id = "p7-request-" + uuid.uuid4().hex[:16]
        if session_id is None:
            session_id = "p7-session-" + uuid.uuid4().hex[:16]
        self._validate_text_and_ids(text, request_id, session_id)
        ledger = P7EvidenceLedger.for_text(text, evidence_kind=self.config.evidence_kind)
        ledger.request_id = request_id
        ledger.metadata.update({
            "session_id": session_id,
            "runner": "live_evidence_runner",
            "serial": True,
            "live_opt_in": self.config.live_opt_in,
        })
        ledger.add_event("RUN_CREATED", request_id=request_id, session_id=session_id)

        if not self.config.enabled:
            return self._blocked(ledger, "live_runner_disabled")
        if self.config.evidence_kind != "real_device" or not self.config.live_opt_in:
            return self._blocked(ledger, "real_device_opt_in_missing")
        if self.agent is None or self.voicestudio is None or self.device is None:
            return self._blocked(ledger, "live_runner_port_missing")
        if not self._run_lock.acquire(blocking=False):
            return self._blocked(ledger, "live_runner_busy")

        try:
            ledger.add_event("AGENT_REQUEST_SUBMITTED", request_id=request_id, session_id=session_id)
            try:
                outcome = self.agent.submit_chat(
                    request_id=request_id,
                    session_id=session_id,
                    text=text,
                )
            except Exception as exc:  # noqa: BLE001 - evidence must fail closed.
                return self._blocked(ledger, f"agent_call_failed:{type(exc).__name__}")
            if not isinstance(outcome, AgentTurnOutcome):
                return self._blocked(ledger, "agent_contract_invalid_type")
            if outcome.request_id != request_id or outcome.session_id != session_id:
                return self._blocked(ledger, "agent_identity_mismatch")
            if not outcome.completed or type(outcome.text) is not str or not outcome.text.strip():
                return self._blocked(ledger, "agent_reply_not_completed")
            if outcome.failure is not None:
                return self._blocked(ledger, "agent_reply_contains_failure")
            if len(outcome.text) > self.config.max_text_chars:
                return self._blocked(ledger, "agent_reply_exceeds_bound")
            ledger.source_text_sha256 = sha256_text(outcome.text)
            ledger.metadata["agent_reply_text"] = outcome.text
            ledger.add_event("AGENT_REPLY_ACCEPTED", request_id=request_id)

            try:
                synthesis = self.voicestudio.synthesize(
                    outcome.text,
                    request_id=request_id,
                    session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001
                return self._blocked(ledger, f"voicestudio_call_failed:{type(exc).__name__}")
            if not isinstance(synthesis, SynthesisResult):
                return self._blocked(ledger, "voicestudio_contract_invalid_type")
            if len(synthesis.audio) > self.config.max_audio_bytes:
                return self._blocked(ledger, "audio_exceeds_bound")
            ledger.set_audio(synthesis.audio)
            ledger.metadata["audio_id"] = synthesis.audio_id
            ledger.metadata["voicestudio_diagnostics"] = dict(synthesis.diagnostics)
            ledger.add_event("VOICESTUDIO_AUDIO_ACCEPTED", audio_id=synthesis.audio_id)

            try:
                playback = self.device.play_and_capture(
                    synthesis.audio,
                    request_id=request_id,
                    session_id=session_id,
                    batch_id=ledger.batch_id,
                )
            except Exception as exc:  # noqa: BLE001
                return self._blocked(ledger, f"device_evidence_failed:{type(exc).__name__}")
            if not isinstance(playback, LivePlaybackEvidence):
                return self._blocked(ledger, "device_evidence_contract_invalid_type")
            ledger.backend_stop_ack = playback.backend_stop_ack
            ledger.loopback_output_stopped = playback.loopback_output_stopped
            ledger.acoustic_output_stopped = playback.acoustic_output_stopped
            ledger.metadata["device_evidence"] = playback.to_dict()
            ledger.add_event(
                "DEVICE_EVIDENCE_ACCEPTED",
                batch_id=ledger.batch_id,
                capture_valid=playback.capture_valid,
                backend_stop_ack=playback.backend_stop_ack,
                loopback_output_stopped=playback.loopback_output_stopped,
                acoustic_output_stopped=playback.acoustic_output_stopped,
            )
            if not playback.capture_valid:
                self._add_block(ledger, "capture_invalid")
            ledger.finalize()
            return LiveEvidenceRunResult(ledger, outcome, synthesis, playback)
        finally:
            self._run_lock.release()

    def _validate_text_and_ids(self, text: str, request_id: str, session_id: str) -> None:
        if type(text) is not str or not text.strip():
            raise LiveRunnerConfigurationError("text must be a non-empty string")
        if len(text) > self.config.max_text_chars:
            raise LiveRunnerConfigurationError("text exceeds live evidence bound")
        for name, value in (("request_id", request_id), ("session_id", session_id)):
            if type(value) is not str or not value.strip():
                raise LiveRunnerConfigurationError(f"{name} must be a non-empty string")

    @classmethod
    def _blocked(cls, ledger: P7EvidenceLedger, reason: str) -> LiveEvidenceRunResult:
        cls._add_block(ledger, reason)
        ledger.finalize()
        return LiveEvidenceRunResult(ledger)

    @staticmethod
    def _add_block(ledger: P7EvidenceLedger, reason: str) -> None:
        if reason not in ledger.blocked_reasons:
            ledger.blocked_reasons.append(reason)
        ledger.add_event("BLOCKED", reason=reason)


__all__ = [
    "AgentPort",
    "DeviceEvidencePort",
    "LiveEvidenceConfig",
    "LiveEvidenceRunResult",
    "LiveEvidenceRunner",
    "LivePlaybackEvidence",
    "LiveRunnerConfigurationError",
    "LiveRunnerContractError",
    "VoiceStudioPort",
]

