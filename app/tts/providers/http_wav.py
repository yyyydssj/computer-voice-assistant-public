"""Offline-testable VoiceStudio HTTP ``/generate`` WAV provider.

This adapter owns only the bounded HTTP/WAV transport boundary. It does not
select voices, alter sentence text, start playback, or install itself into the
default runtime. A complete WAV response is normalized to one final PCMChunk so
later playback code can share the Batch 1 contract with the WebSocket path.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import io
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Any, Callable, Iterator, Protocol

from .contracts import PCMChunk, VoiceStudioContractError, VoiceStudioSentenceRequest


class VoiceStudioHttpWavError(VoiceStudioContractError):
    """Fail-closed HTTP/WAV transport or response violation."""

    def __init__(self, code: str) -> None:
        # The code is both the fixed short code and the message; it goes through the
        # base constructor so the base cannot reset the attribute to ``None``.
        super().__init__(code, code=code)


class HttpWavResponse(Protocol):
    status: int
    headers: Any

    def __enter__(self) -> "HttpWavResponse": ...
    def __exit__(self, *args: object) -> object: ...
    def read(self, limit: int = -1) -> bytes: ...


HttpWavOpener = Callable[..., HttpWavResponse]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise VoiceStudioHttpWavError("redirect_not_allowed")


_DEFAULT_HTTP_OPENER = urllib.request.build_opener(_NoRedirectHandler())

_DEFAULT_MAX_AUDIO_BYTES = 500_000
_DEFAULT_MAX_FRAMES = 192_000


def _loopback_endpoint(name: str, value: str) -> str:
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


def _path(base: str, path: str) -> str:
    if type(path) is not str or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("path_must_be_absolute_without_query")
    return base.rstrip("/") + path


def _status(response: Any) -> int | None:
    value = getattr(response, "status", None)
    if value is None and hasattr(response, "getcode"):
        value = response.getcode()
    return value if type(value) is int else None


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get_content_type", None)
    if callable(getter):
        try:
            return str(getter()).casefold()
        except Exception:
            pass
    try:
        return str(headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
    except Exception:
        return ""


@dataclass(frozen=True, slots=True)
class VoiceStudioHttpWavConfig:
    """Bounded localhost configuration for one HTTP/WAV request."""

    endpoint: str = "http://127.0.0.1:3900"
    path: str = "/generate"
    timeout_seconds: float = 18.0
    max_audio_bytes: int = _DEFAULT_MAX_AUDIO_BYTES
    max_frames: int = _DEFAULT_MAX_FRAMES

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _loopback_endpoint("voicestudio_endpoint", self.endpoint))
        _path(self.endpoint, self.path)
        if type(self.timeout_seconds) not in {int, float} or isinstance(self.timeout_seconds, bool):
            raise ValueError("timeout_seconds_must_be_number")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        if type(self.max_audio_bytes) is not int or self.max_audio_bytes <= 44:
            raise ValueError("max_audio_bytes_must_be_greater_than_wav_header")
        if type(self.max_frames) is not int or self.max_frames <= 0:
            raise ValueError("max_frames_must_be_positive")

    @property
    def url(self) -> str:
        return _path(self.endpoint, self.path)


class VoiceStudioHttpWavProvider:
    """Synchronous, cancellable, no-retry HTTP/WAV VoiceStudio provider."""

    def __init__(
        self,
        config: VoiceStudioHttpWavConfig | None = None,
        *,
        opener: HttpWavOpener = _DEFAULT_HTTP_OPENER.open,
    ) -> None:
        if config is not None and not isinstance(config, VoiceStudioHttpWavConfig):
            raise TypeError("config 必须是 VoiceStudioHttpWavConfig")
        if not callable(opener):
            raise TypeError("opener 必须可调用")
        self.config = config or VoiceStudioHttpWavConfig()
        self._opener = opener
        self._closed = False
        self._lock = threading.Lock()

    def stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        *,
        cancelled: threading.Event,
    ) -> Iterator[PCMChunk]:
        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        if not isinstance(cancelled, threading.Event):
            raise TypeError("cancelled 必须是 threading.Event")
        if cancelled.is_set():
            raise VoiceStudioHttpWavError("cancelled_before_request")
        with self._lock:
            if self._closed:
                raise VoiceStudioHttpWavError("provider_closed")
        body, boundary = self._multipart_body(request)
        if cancelled.is_set():
            raise VoiceStudioHttpWavError("cancelled_before_request")
        http_request = urllib.request.Request(
            self.config.url,
            data=body,
            method="POST",
            headers={
                "Accept": "audio/wav",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-P7-Reply-ID": request.reply_id,
                "X-P7-Segment-ID": request.segment_id,
                "X-P7-Request-ID": request.provider_request_id,
                "X-P7-Generation": str(request.provider_generation),
            },
        )
        status, content_type, audio = self._request(http_request, cancelled=cancelled)
        if status != 200:
            raise VoiceStudioHttpWavError(f"http_status_{status}")
        if content_type != "audio/wav":
            raise VoiceStudioHttpWavError("content_type_invalid")
        pcm, _frames = self._decode_wav(audio)
        if cancelled.is_set():
            raise VoiceStudioHttpWavError("cancelled_after_response")
        with self._lock:
            if self._closed:
                raise VoiceStudioHttpWavError("provider_closed")
        yield PCMChunk(
            reply_id=request.reply_id,
            segment_id=request.segment_id,
            provider_request_id=request.provider_request_id,
            provider_generation=request.provider_generation,
            sequence=1,
            sample_rate=24_000,
            channels=1,
            sample_width=2,
            data=pcm,
            is_final=True,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _request(
        self,
        request: urllib.request.Request,
        *,
        cancelled: threading.Event,
    ) -> tuple[int, str, bytes]:
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                status = _status(response)
                content_type = _content_type(response)
                body = response.read(self.config.max_audio_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise VoiceStudioHttpWavError(f"http_status_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise VoiceStudioHttpWavError("transport_failed") from None
        except VoiceStudioHttpWavError:
            raise
        except Exception as exc:
            raise VoiceStudioHttpWavError(f"transport_failed_{type(exc).__name__}") from None
        if cancelled.is_set():
            raise VoiceStudioHttpWavError("cancelled_after_response")
        if status is None:
            raise VoiceStudioHttpWavError("response_status_missing")
        if type(body) is not bytes:
            raise VoiceStudioHttpWavError("response_body_not_bytes")
        if len(body) > self.config.max_audio_bytes:
            raise VoiceStudioHttpWavError("audio_too_large")
        return status, content_type, body

    @staticmethod
    def _multipart_body(request: VoiceStudioSentenceRequest) -> tuple[bytes, str]:
        boundary = "----P7VoiceStudioHttpWav" + request.provider_request_id[-16:]
        fields: list[tuple[str, str]] = [
            ("text", request.text),
            ("profile_id", request.profile_id),
            ("language", request.language),
            ("speed", str(request.speed)),
            ("stream", "false"),
        ]
        if request.engine is not None:
            fields.append(("engine", request.engine))
        body = b"".join(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode("utf-8")
            for name, value in fields
        ) + f"--{boundary}--\r\n".encode("ascii")
        return body, boundary

    def _decode_wav(self, audio: bytes) -> tuple[bytes, int]:
        if not audio:
            raise VoiceStudioHttpWavError("audio_empty")
        try:
            with wave.open(io.BytesIO(audio), "rb") as source:
                channels = source.getnchannels()
                width = source.getsampwidth()
                rate = source.getframerate()
                frames = source.getnframes()
                if channels != 1 or width != 2 or rate != 24_000:
                    raise VoiceStudioHttpWavError("audio_format_invalid")
                if not 0 < frames <= self.config.max_frames:
                    raise VoiceStudioHttpWavError("audio_frames_invalid")
                pcm = source.readframes(frames)
        except VoiceStudioHttpWavError:
            raise
        except (EOFError, ValueError, wave.Error):
            raise VoiceStudioHttpWavError("audio_invalid") from None
        if len(pcm) != frames * 2 or not pcm:
            raise VoiceStudioHttpWavError("audio_truncated")
        if not any(pcm):
            raise VoiceStudioHttpWavError("audio_silent")
        return pcm, frames


__all__ = [
    "VoiceStudioHttpWavConfig",
    "VoiceStudioHttpWavError",
    "VoiceStudioHttpWavProvider",
]