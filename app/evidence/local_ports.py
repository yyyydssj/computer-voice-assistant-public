"""Explicit localhost adapters for the P7 live evidence runner.

These adapters are opt-in and fail closed. HTTP success, a WAV file, a backend
callback, or a quiet capture is never promoted to acoustic or user-heard fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import io
import ipaddress
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Any, Callable, Mapping

from app.agent.adapter import AgentFailure, AgentFailureCategory, AgentTurnOutcome
from app.evidence.agent_chain import SynthesisResult
from app.evidence.live_runner import LivePlaybackEvidence
from app.evidence.ledger import sha256_bytes
from app.tts.playback_worker import PlaybackState, PlaybackWorker, WaveOutPlaybackBackend


class LocalPortError(RuntimeError):
    """A bounded local port operation failed without exposing response data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _local_url(name: str, value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name}_required")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name}_must_be_http_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name}_unsafe_url")
    host = parsed.hostname.rstrip(".").casefold()
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(f"{name}_loopback_required") from exc
        if not address.is_loopback:
            raise ValueError(f"{name}_loopback_required")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name}_invalid_port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name}_invalid_port")
    return value.rstrip("/")


def _url(base: str, path: str) -> str:
    if type(path) is not str or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("path_must_be_absolute_without_query")
    return base.rstrip("/") + path


def _response_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        return str(headers.get_content_type()).casefold()
    except Exception:
        return str(headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()


def _status(response: Any) -> int | None:
    value = getattr(response, "status", None)
    if value is None and hasattr(response, "getcode"):
        value = response.getcode()
    return value if type(value) is int else None


def _request(
    opener: Callable[..., Any], request: urllib.request.Request, *, timeout: float, limit: int
) -> tuple[int, str, bytes]:
    try:
        with opener(request, timeout=timeout) as response:
            status = _status(response)
            kind = _response_type(response)
            body = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise LocalPortError(f"http_status_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LocalPortError("transport_failed") from None
    except LocalPortError:
        raise
    except Exception as exc:
        raise LocalPortError(f"transport_failed:{type(exc).__name__}") from None
    if status is None:
        raise LocalPortError("response_status_missing")
    if type(body) is not bytes:
        raise LocalPortError("response_body_not_bytes")
    if len(body) > limit:
        raise LocalPortError("response_body_too_large")
    return status, kind, body


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LocalPortError("response_json_invalid") from None
    if not isinstance(value, dict):
        raise LocalPortError("response_json_not_object")
    return value


@dataclass(frozen=True, slots=True)
class LocalAgentConfig:
    endpoint: str = "http://127.0.0.1:3901"
    path: str = "/chat"
    timeout_seconds: float = 30.0
    max_request_bytes: int = 131072
    max_response_bytes: int = 1000000

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _local_url("agent_endpoint", self.endpoint))
        _url(self.endpoint, self.path)
        if self.timeout_seconds <= 0 or self.max_request_bytes <= 0 or self.max_response_bytes <= 0:
            raise ValueError("agent_limits_must_be_positive")


class LocalAgentAdapter:
    """One-request/no-retry implementation of the local-agent.v1 port."""

    def __init__(
        self, config: LocalAgentConfig | None = None, *, opener: Callable[..., Any] = urllib.request.urlopen
    ) -> None:
        self.config = config or LocalAgentConfig()
        self._opener = opener
        self._submitted: set[str] = set()
        self._lock = threading.Lock()

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        if not all(type(x) is str and x.strip() for x in (request_id, session_id, text)):
            raise ValueError("request_session_text_required")
        payload = {"version": "local-agent.v1", "request_id": request_id, "session_id": session_id, "text": text}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self.config.max_request_bytes:
            raise ValueError("agent_request_too_large")
        with self._lock:
            if request_id in self._submitted:
                raise ValueError("request_id_already_submitted")
            self._submitted.add(request_id)
        request = urllib.request.Request(
            _url(self.config.endpoint, self.config.path), data=body, method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
                     "X-P7-Request-ID": request_id, "X-P7-Session-ID": session_id},
        )
        status, kind, response_body = _request(
            self._opener, request, timeout=self.config.timeout_seconds, limit=self.config.max_response_bytes
        )
        if status != 200:
            raise LocalPortError(f"agent_http_status_{status}")
        if kind and kind != "application/json":
            raise LocalPortError("agent_content_type_invalid")
        result = _json_object(response_body)
        if result.get("version") not in (None, "local-agent.v1"):
            raise LocalPortError("agent_protocol_version_invalid")
        if result.get("request_id") != request_id or result.get("session_id") != session_id:
            raise LocalPortError("agent_identity_mismatch")
        completed = result.get("completed")
        text_value = result.get("text", "")
        if type(completed) is not bool or type(text_value) is not str:
            raise LocalPortError("agent_response_schema_invalid")
        failure = result.get("failure")
        failure_obj = None
        if failure is not None:
            if not isinstance(failure, dict):
                raise LocalPortError("agent_failure_invalid")
            try:
                category = AgentFailureCategory(str(failure.get("category", "schema_error")))
            except ValueError:
                category = AgentFailureCategory.SCHEMA_ERROR
            failure_obj = AgentFailure(
                category=category, stage=str(failure.get("stage") or "request"),
                diagnostic_id=str(failure.get("diagnostic_id") or "local-agent-" + secrets.token_hex(8)),
            )
        if completed and (not text_value.strip() or failure_obj is not None):
            raise LocalPortError("agent_success_schema_invalid")
        if not completed and failure_obj is None:
            failure_obj = AgentFailure(AgentFailureCategory.SCHEMA_ERROR, "request", "local-agent-" + secrets.token_hex(8))
        finish = result.get("finish_reason")
        return AgentTurnOutcome(
            request_id, session_id, completed, text_value,
            finish if isinstance(finish, str) else None, failure_obj
        )

@dataclass(frozen=True, slots=True)
class LocalVoiceStudioConfig:
    endpoint: str = "http://127.0.0.1:3900"
    path: str = "/generate"
    profile_id: str = "default"
    language: str = "Chinese"
    speed: str = "1.0"
    timeout_seconds: float = 30.0
    max_text_chars: int = 32768
    max_audio_bytes: int = 500000

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _local_url("voicestudio_endpoint", self.endpoint))
        _url(self.endpoint, self.path)
        if not self.profile_id or any(c in self.profile_id for c in "\r\n\""):
            raise ValueError("profile_id_invalid")
        if self.timeout_seconds <= 0 or self.max_text_chars <= 0 or self.max_audio_bytes <= 0:
            raise ValueError("voicestudio_limits_must_be_positive")


class LocalVoiceStudioClient:
    """Bounded localhost VoiceStudio client returning the exact WAV bytes."""

    def __init__(
        self, config: LocalVoiceStudioConfig, *, opener: Callable[..., Any] = urllib.request.urlopen
    ) -> None:
        self.config = config
        self._opener = opener

    def synthesize(self, text: str, *, request_id: str, session_id: str) -> SynthesisResult:
        if type(text) is not str or not text.strip() or len(text) > self.config.max_text_chars:
            raise ValueError("voicestudio_text_invalid")
        if not all(type(x) is str and x.strip() for x in (request_id, session_id)):
            raise ValueError("request_session_required")
        boundary = "----P7LocalVoiceStudio" + secrets.token_hex(12)
        fields = (
            ("text", text), ("profile_id", self.config.profile_id),
            ("language", self.config.language), ("speed", self.config.speed), ("stream", "false"),
        )
        body = b"".join(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
            for name, value in fields
        ) + f"--{boundary}--\r\n".encode("ascii")
        request = urllib.request.Request(
            _url(self.config.endpoint, self.config.path), data=body, method="POST",
            headers={"Accept": "audio/wav", "Content-Type": f"multipart/form-data; boundary={boundary}",
                     "X-P7-Request-ID": request_id, "X-P7-Session-ID": session_id},
        )
        status, content_type, audio = _request(
            self._opener, request, timeout=self.config.timeout_seconds, limit=self.config.max_audio_bytes
        )
        if status != 200:
            raise LocalPortError(f"voicestudio_http_status_{status}")
        if content_type != "audio/wav":
            raise LocalPortError("voicestudio_content_type_invalid")
        _validate_wav(audio, max_bytes=self.config.max_audio_bytes)
        return SynthesisResult(
            audio=audio, audio_id="sha256:" + sha256_bytes(audio),
            diagnostics={"protocol": "local-voicestudio.v1", "request_id": request_id,
                         "session_id": session_id, "content_type": content_type, "audio_bytes": len(audio)},
        )


def _validate_wav(audio: bytes, *, max_bytes: int) -> None:
    if type(audio) is not bytes or not audio or len(audio) > max_bytes:
        raise LocalPortError("voicestudio_audio_invalid_or_too_large")
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            channels, width, rate, frames = source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()
            if channels != 1 or width != 2 or rate != 24000 or not 0 < frames <= 192000:
                raise LocalPortError("voicestudio_audio_format_invalid")
            pcm = source.readframes(frames)
    except (EOFError, ValueError, wave.Error):
        raise LocalPortError("voicestudio_audio_invalid") from None
    if len(pcm) != frames * 2 or not any(pcm):
        raise LocalPortError("voicestudio_audio_silent_or_truncated")


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    audio: bytes
    request_id: str
    session_id: str
    batch_id: str
    playback_finished: threading.Event | None = None


@dataclass(frozen=True, slots=True)
class DeviceCaptureFacts:
    """Facts supplied by an explicitly synchronized capture hook."""

    backend_stop_ack: bool | None = None
    loopback_output_stopped: bool | None = None
    acoustic_output_stopped: bool | None = None
    capture_valid: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("backend_stop_ack", "loopback_output_stopped", "acoustic_output_stopped"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name}_must_be_bool_or_none")
        if type(self.capture_valid) is not bool or not isinstance(self.diagnostics, Mapping):
            raise ValueError("capture_facts_invalid")
        object.__setattr__(self, "diagnostics", dict(self.diagnostics))


CaptureHook = Callable[[CaptureRequest], DeviceCaptureFacts]


class LocalWindowsDeviceEvidence:
    """Opt-in WaveOut port with conservative synchronized-capture seam."""

    def __init__(
        self, *, enabled: bool = False, live_opt_in: bool = False,
        worker: PlaybackWorker | None = None, worker_factory: Callable[[], PlaybackWorker] | None = None,
        capture_hook: CaptureHook | None = None, playback_timeout_seconds: float = 2.0,
        capture_join_timeout_seconds: float = 3.0,
    ) -> None:
        if type(enabled) is not bool or type(live_opt_in) is not bool:
            raise ValueError("device_opt_in_must_be_bool")
        if live_opt_in and not enabled:
            raise ValueError("live_opt_in_requires_enabled")
        if playback_timeout_seconds <= 0 or capture_join_timeout_seconds <= 0:
            raise ValueError("device_timeouts_must_be_positive")
        if worker is not None and worker_factory is not None:
            raise ValueError("worker_and_factory_are_mutually_exclusive")
        self.enabled, self.live_opt_in = enabled, live_opt_in
        self.capture_hook = capture_hook
        self.playback_timeout_seconds = playback_timeout_seconds
        self.capture_join_timeout_seconds = capture_join_timeout_seconds
        self._backend_holder: dict[str, Any] = {}
        if worker is not None:
            self.worker, self._owns_worker = worker, False
        elif enabled and live_opt_in:
            self.worker, self._owns_worker = (
                (worker_factory or self._default_worker_factory)(),
                True,
            )
        else:
            self.worker, self._owns_worker = None, False
        self._lock = threading.Lock()

    def _default_worker_factory(self) -> PlaybackWorker:
        def make_backend() -> WaveOutPlaybackBackend:
            backend = WaveOutPlaybackBackend()
            self._backend_holder["backend"] = backend
            return backend
        return PlaybackWorker(make_backend)

    def play_and_capture(self, audio: bytes, *, request_id: str, session_id: str, batch_id: str) -> LivePlaybackEvidence:
        if not self.enabled or not self.live_opt_in:
            return LivePlaybackEvidence(diagnostics={"blocked_reason": "device_live_opt_in_required"})
        if self.worker is None:
            return LivePlaybackEvidence(diagnostics={"blocked_reason": "device_worker_missing"})
        if type(audio) is not bytes or not audio:
            return LivePlaybackEvidence(diagnostics={"blocked_reason": "audio_invalid"})
        if not all(type(x) is str and x.strip() for x in (request_id, session_id, batch_id)):
            return LivePlaybackEvidence(diagnostics={"blocked_reason": "identity_invalid"})
        if not self._lock.acquire(blocking=False):
            return LivePlaybackEvidence(diagnostics={"blocked_reason": "device_busy"})
        facts_box: dict[str, Any] = {}
        capture_thread = None
        playback_finished = threading.Event()
        try:
            request = CaptureRequest(audio, request_id, session_id, batch_id, playback_finished)
            if self.capture_hook is not None:
                def capture_target() -> None:
                    try:
                        facts_box["facts"] = self.capture_hook(request)
                    except Exception as exc:
                        facts_box["error"] = f"capture_hook_failed:{type(exc).__name__}"
                capture_thread = threading.Thread(target=capture_target, name="p7-capture-hook", daemon=True)
                capture_thread.start()
            try:
                ticket = self.worker.submit(audio, metadata={"request_id": request_id, "session_id": session_id, "batch_id": batch_id})
            except Exception as exc:
                # Do not strand a capture thread waiting for a playback event
                # that can never occur after submission failure.
                playback_finished.set()
                if capture_thread is not None:
                    capture_thread.join(self.capture_join_timeout_seconds)
                return LivePlaybackEvidence(diagnostics={"blocked_reason": f"playback_submit_failed:{type(exc).__name__}"})
            state = self.worker.wait(ticket, timeout=self.playback_timeout_seconds)
            if not ticket.terminal:
                state = self.worker.stop(ticket)
            # Release the capture post window only after the same ticket has
            # reached its terminal state and any backend cleanup confirmation
            # has been recorded.  This is a timing gate, not acoustic evidence.
            playback_finished.set()
            if capture_thread is not None:
                capture_thread.join(self.capture_join_timeout_seconds)
            facts = facts_box.get("facts")
            if not isinstance(facts, DeviceCaptureFacts):
                facts = DeviceCaptureFacts(diagnostics={"blocked_reason": facts_box.get("error", "capture_not_available")})
            terminal_event = ticket.events[-1] if getattr(ticket, "events", None) else None
            event_stop_ack = getattr(terminal_event, "stop_ack", None)
            if state is PlaybackState.STOP_ACKED:
                backend_stop_ack = True
            elif state in {PlaybackState.STOP_TIMEOUT, PlaybackState.UNKNOWN, PlaybackState.FAILED}:
                backend_stop_ack = False
            elif state is PlaybackState.COMPLETED and type(event_stop_ack) is bool:
                # Natural completion remains COMPLETED; only the worker's
                # same-ticket cleanup event may supply backend ACK evidence.
                backend_stop_ack = event_stop_ack
            else:
                backend_stop_ack = facts.backend_stop_ack
            diagnostics: dict[str, Any] = {
                "playback_state": state.value, "ticket_id": ticket.ticket_id,
                "ticket_events": [event.state.value for event in ticket.events],
                "audio_sha256": sha256_bytes(audio), **dict(facts.diagnostics),
            }
            backend = self._backend_holder.get("backend")
            diagnostic = getattr(backend, "diagnostic", None)
            if diagnostic is not None:
                try:
                    from dataclasses import asdict
                    diagnostics["waveout_diagnostic"] = asdict(diagnostic)
                except Exception:
                    diagnostics["waveout_diagnostic"] = {"available": False}
            return LivePlaybackEvidence(
                backend_stop_ack=backend_stop_ack,
                loopback_output_stopped=facts.loopback_output_stopped if facts.capture_valid else None,
                acoustic_output_stopped=facts.acoustic_output_stopped if facts.capture_valid else None,
                capture_valid=facts.capture_valid, diagnostics=diagnostics,
            )
        finally:
            self._lock.release()

    def close(self) -> None:
        if self._owns_worker and self.worker is not None:
            self.worker.close()


__all__ = [
    "CaptureRequest", "DeviceCaptureFacts", "LocalAgentAdapter", "LocalAgentConfig", "LocalPortError",
    "LocalVoiceStudioClient", "LocalVoiceStudioConfig", "LocalWindowsDeviceEvidence",
]
