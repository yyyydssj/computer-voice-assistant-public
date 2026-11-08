"""Offline Edge-slot provider backed by the local Windows SAPI voice.

The dual-path plan's second fallback layer is ``VoiceStudio -> Edge -> text``.
The Edge slot is implemented **offline** here: the installed Windows SAPI voice
(nominally ``Microsoft Huihui Desktop``, zh-CN) renders the same sentence to a
24000 Hz / 16-bit / mono PCM WAV through one fixed, pure-ASCII PowerShell script
(:mod:`app.tts.providers.edge_sapi_runner`), and this module normalizes that WAV
into the same ordered :class:`PCMChunk` contract the VoiceStudio transports use,
so the playback worker cannot tell the two paths apart.

Privacy and containment rules enforced by this module:

* no network client import and no network call of any kind: the only external
  program this module can start is the fixed, pure-ASCII renderer script;
* the sentence text is **never** placed on a command line: it is written to a
  unique UTF-8 temporary text file and read back by the fixed script, which is
  the only defence needed against argument injection;
* ``subprocess`` is invoked with an argument list only (never through a shell),
  with stdin closed, output captured but **never** surfaced, a bounded timeout
  and no console window on Windows;
* errors, ``repr`` and diagnostics carry a short code only: no sentence text, no
  temporary path and no child output is ever attached to an exception;
* the temporary text and WAV files are always removed, including on failure, and
  the temporary root must be an absolute, non-symlink, local, private directory.

Nothing in this module selects a voice by itself, mutates sentence text, starts
playback, or installs itself into the default runtime: the provider is injected
explicitly, exactly like the other adapters. A real acoustic acceptance run
still needs separate authorization.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Iterator

from app.tts.clock import monotonic_seconds
from app.tts.private_selection import private_data_root

from .contracts import PCMChunk, VoiceStudioContractError, VoiceStudioSentenceRequest


class EdgeSapiError(VoiceStudioContractError):
    """Fail-closed offline SAPI violation; the message is only a short code."""

    def __init__(self, code: str) -> None:
        # The code is both the fixed short code and the message; it goes through the
        # base constructor so the base cannot reset the attribute to ``None``.
        super().__init__(code, code=code)


_VOICE_NAME_RE = re.compile(r"^[A-Za-z0-9 ()\-_.]+$")
_MAX_VOICE_NAME_LENGTH = 128
_AUDIO_FORMAT = (24_000, 1, 2)
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_AUDIO_BYTES = 8 * 1024 * 1024
_DEFAULT_CHUNK_SAMPLES = 4_800  # 200 ms at 24000 Hz, the VoiceStudio block size
_TEMP_SUBDIRECTORY = "temp"
_RUNNER_SCRIPT_NAME = "edge_sapi_runner.ps1"
_RUNNER_SCRIPT_PATH = Path(__file__).resolve().parent / _RUNNER_SCRIPT_NAME
_IS_WINDOWS = os.name == "nt"
# Never open a console window for the fixed script on Windows; ``0`` elsewhere.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def _validated_voice_name(value: object) -> str:
    """Accept only a bounded, shell-neutral installed-voice name."""

    if type(value) is not str or not value:
        raise ValueError("voice_name_must_be_a_non_empty_string")
    if len(value) > _MAX_VOICE_NAME_LENGTH:
        raise ValueError("voice_name_too_long")
    if not _VOICE_NAME_RE.fullmatch(value):
        raise ValueError("voice_name_contains_unsupported_characters")
    return value


def _validated_temp_root(value: object) -> Path:
    """Resolve, create and harden the temporary root for one provider."""

    if value is None:
        candidate = private_data_root() / _TEMP_SUBDIRECTORY
    elif isinstance(value, Path):
        candidate = value
    elif isinstance(value, str) and value:
        candidate = Path(value)
    else:
        raise ValueError("temp_root_must_be_a_path_or_none")
    if str(candidate).startswith(("\\\\", "//")):
        raise ValueError("temp_root_must_not_be_a_network_path")
    if not candidate.is_absolute():
        raise ValueError("temp_root_must_be_an_absolute_path")
    if candidate.is_symlink():
        raise ValueError("temp_root_must_not_be_a_symlink")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("temp_root_must_be_a_directory")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError("temp_root_not_creatable") from None
    if candidate.is_symlink():
        raise ValueError("temp_root_must_not_be_a_symlink")
    try:
        # Best effort only: on Windows this toggles nothing but the read-only
        # bit, and a failure here must never reject a usable private directory.
        os.chmod(candidate, 0o700)
    except OSError:
        pass
    return candidate


@dataclass(frozen=True, slots=True)
class EdgeSapiConfig:
    """Bounded offline configuration for the SAPI-backed Edge slot.

    Only the format the VoiceStudio seam already uses is accepted
    (24000 Hz / 1 channel / 2 bytes per sample); any other combination raises
    ``ValueError`` instead of silently re-sampling. ``temp_root`` is resolved to
    an existing absolute, non-symlink, local private directory and is kept out of
    ``repr`` so no filesystem path can leak into diagnostics.
    """

    voice_name: str = "Microsoft Huihui Desktop"
    sample_rate_hz: int = 24_000
    channels: int = 1
    sample_width: int = 2
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_audio_bytes: int = _DEFAULT_MAX_AUDIO_BYTES
    chunk_samples: int = _DEFAULT_CHUNK_SAMPLES
    temp_root: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "voice_name", _validated_voice_name(self.voice_name))
        for name, value in (
            ("sample_rate_hz", self.sample_rate_hz),
            ("channels", self.channels),
            ("sample_width", self.sample_width),
        ):
            if type(value) is not int:
                raise ValueError(f"{name}_must_be_an_integer")
        if (self.sample_rate_hz, self.channels, self.sample_width) != _AUDIO_FORMAT:
            raise ValueError("edge_sapi_audio_format_must_be_24000_16_mono")
        if (
            type(self.timeout_seconds) not in {int, float}
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(float(self.timeout_seconds))
            or float(self.timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds_must_be_a_positive_number")
        if type(self.max_audio_bytes) is not int or self.max_audio_bytes <= 44:
            raise ValueError("max_audio_bytes_must_exceed_the_wav_header")
        if type(self.chunk_samples) is not int or self.chunk_samples <= 0:
            raise ValueError("chunk_samples_must_be_a_positive_integer")
        object.__setattr__(self, "temp_root", _validated_temp_root(self.temp_root))

    @property
    def chunk_bytes(self) -> int:
        """Bytes of one 200 ms block: ``chunk_samples * channels * sample_width``."""

        return self.chunk_samples * self.channels * self.sample_width


SapiRunner = Callable[[EdgeSapiConfig, str, Path, Path], None]
VoiceListRunner = Callable[[EdgeSapiConfig, str], str]


def _powershell_executable() -> str:
    """Prefer the absolute inbox PowerShell 5.1 path; fall back to ``PATH``."""

    system_root = os.environ.get("SystemRoot")
    if _IS_WINDOWS and system_root:
        candidate = (
            Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
        if candidate.is_file():
            return str(candidate)
    return "powershell.exe"


def _powershell_command(script_path: str, arguments: list[str]) -> list[str]:
    """Build the one fixed, argument-list command shape used by this module."""

    return [
        _powershell_executable(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script_path,
        *arguments,
    ]


def _spawn_bounded(command: list[str], *, timeout: float) -> bytes:
    """Run the fixed script once; child output is captured but never surfaced."""

    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise EdgeSapiError("sapi_timeout") from None
    except (OSError, ValueError):
        raise EdgeSapiError("sapi_launch_failed") from None
    if completed.returncode:
        raise EdgeSapiError(f"sapi_exit_{int(completed.returncode)}")
    return completed.stdout or b""


def _run_sapi_render(
    config: EdgeSapiConfig, script_path: str, text_path: Path, out_path: Path
) -> None:
    """Default runner: render one sentence with the fixed SAPI script."""

    if not Path(script_path).is_file():
        raise EdgeSapiError("sapi_runner_script_missing")
    command = _powershell_command(
        script_path,
        [
            "-TextPath",
            str(text_path),
            "-OutPath",
            str(out_path),
            "-VoiceName",
            config.voice_name,
        ],
    )
    _spawn_bounded(command, timeout=config.timeout_seconds)


def _run_sapi_voice_list(config: EdgeSapiConfig, script_path: str) -> str:
    """Default voices runner: list installed SAPI voices through the script."""

    if not Path(script_path).is_file():
        raise EdgeSapiError("sapi_runner_script_missing")
    output = _spawn_bounded(
        _powershell_command(script_path, ["-ListVoices"]),
        timeout=config.timeout_seconds,
    )
    # Voice names are ASCII; a replacement policy keeps a mis-encoded code page
    # from turning into an exception and never echoes anything back to a caller.
    return output.decode("utf-8", errors="replace")


def _parse_voice_list(output: str) -> tuple[str, ...]:
    """Keep the ``Name`` field of every ``Name|Culture|Enabled`` line, in order."""

    names: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip().lstrip("\ufeff").strip()
        if "|" not in line:
            continue
        name = line.split("|", 1)[0].strip()
        if not name or name in names:
            continue
        names.append(name)
    return tuple(names)


def _remove_file(path: Path) -> None:
    """Best-effort removal that never masks the original failure."""

    try:
        path.unlink()
    except OSError:
        pass


class EdgeSapiVoiceProvider:
    """Synchronous, cancellable, offline SAPI provider for the Edge slot.

    ``stream_sentence`` is structurally identical to the VoiceStudio providers:
    it yields ordered :class:`PCMChunk` values that carry the caller's request
    identity, exactly one of which is final. The render is bounded by
    ``config.timeout_seconds`` and the child is always reaped before the first
    chunk is yielded, so a cancellation between chunks discards already rendered
    audio and removes both temporary files instead of leaving a process behind.
    A run is executed as an argument list, never through a shell.

    ``runner`` and ``clock`` are injection seams: tests replace the runner so no
    PowerShell process is ever started, and the clock shares the session's
    monotonic basis.
    """

    def __init__(
        self,
        config: EdgeSapiConfig | None = None,
        *,
        runner: SapiRunner | None = None,
        clock: Callable[[], float] = monotonic_seconds,
    ) -> None:
        if config is not None and not isinstance(config, EdgeSapiConfig):
            raise TypeError("config 必须是 EdgeSapiConfig")
        if runner is not None and not callable(runner):
            raise TypeError("runner 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self.config = config or EdgeSapiConfig()
        self._runner = runner or _run_sapi_render
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(voice_name={self.config.voice_name!r}, "
            f"closed={self._closed})"
        )

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        *,
        cancelled: Event,
    ) -> Iterator[PCMChunk]:
        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        if not isinstance(cancelled, Event):
            raise TypeError("cancelled 必须是 threading.Event")
        if cancelled.is_set():
            raise EdgeSapiError("cancelled_before_request")
        with self._lock:
            if self._closed:
                raise EdgeSapiError("provider_closed")
        text_path = self._temporary_path(".txt")
        wav_path: Path | None = None
        try:
            wav_path = self._temporary_path(".wav")
            self._write_text(text_path, request.text)
            if cancelled.is_set():
                raise EdgeSapiError("cancelled_before_request")
            try:
                self._runner(self.config, str(_RUNNER_SCRIPT_PATH), text_path, wav_path)
            except EdgeSapiError:
                raise
            except Exception:
                # A runner failure never leaks its own message, the sentence text,
                # a temporary path or child output into this error.
                raise EdgeSapiError("sapi_runner_failed") from None
            pcm = self._read_pcm(wav_path)
            chunk_bytes = self.config.chunk_bytes
            total = len(pcm)
            offset = 0
            sequence = 0
            while offset < total:
                if cancelled.is_set():
                    raise EdgeSapiError("cancelled_during_stream")
                sequence += 1
                block = pcm[offset : offset + chunk_bytes]
                offset += len(block)
                yield PCMChunk(
                    reply_id=request.reply_id,
                    segment_id=request.segment_id,
                    provider_request_id=request.provider_request_id,
                    provider_generation=request.provider_generation,
                    sequence=sequence,
                    sample_rate=self.config.sample_rate_hz,
                    channels=self.config.channels,
                    sample_width=self.config.sample_width,
                    data=block,
                    is_final=offset >= total,
                )
        finally:
            _remove_file(text_path)
            if wav_path is not None:
                _remove_file(wav_path)

    # ----------------------------------------------------------------- internal

    def _temporary_path(self, suffix: str) -> Path:
        temp_root = self.config.temp_root
        try:
            handle, name = tempfile.mkstemp(
                prefix="edge-sapi-", suffix=suffix, dir=str(temp_root)
            )
        except OSError:
            raise EdgeSapiError("temporary_file_unavailable") from None
        os.close(handle)
        return Path(name)

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        try:
            with open(path, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
        except OSError:
            raise EdgeSapiError("temporary_text_unwritable") from None

    def _read_pcm(self, path: Path) -> bytes:
        """Validate the rendered WAV and return its raw PCM16 payload."""

        config = self.config
        try:
            with open(path, "rb") as stream:
                header = stream.read(12)
        except OSError:
            raise EdgeSapiError("sapi_audio_missing") from None
        if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise EdgeSapiError("sapi_audio_invalid")
        try:
            with wave.open(str(path), "rb") as source:
                channels = source.getnchannels()
                width = source.getsampwidth()
                rate = source.getframerate()
                frames = source.getnframes()
                if (rate, channels, width) != (
                    config.sample_rate_hz,
                    config.channels,
                    config.sample_width,
                ):
                    raise EdgeSapiError("sapi_audio_format_invalid")
                expected = frames * channels * width
                if expected > config.max_audio_bytes:
                    raise EdgeSapiError("sapi_audio_too_large")
                pcm = source.readframes(frames)
        except EdgeSapiError:
            raise
        except (wave.Error, EOFError, OSError, ValueError):
            raise EdgeSapiError("sapi_audio_invalid") from None
        if expected % config.sample_width:
            raise EdgeSapiError("sapi_audio_odd_length")
        if not pcm or frames <= 0:
            raise EdgeSapiError("sapi_audio_empty")
        if len(pcm) != expected:
            raise EdgeSapiError("sapi_audio_truncated")
        return pcm


def list_edge_sapi_voices(
    config: EdgeSapiConfig | None = None,
    *,
    runner: VoiceListRunner | None = None,
) -> tuple[str, ...]:
    """Return the installed SAPI voice names reported by the fixed script.

    Read-only diagnostics for configuration review: the result is a tuple of
    ``Name`` fields in script order, and it never contains culture data, a path
    or a URL. The real call is part of the separately authorized acoustic
    acceptance run; offline tests inject ``runner`` so no process is started.
    """

    if config is not None and not isinstance(config, EdgeSapiConfig):
        raise TypeError("config 必须是 EdgeSapiConfig")
    if runner is not None and not callable(runner):
        raise TypeError("runner 必须可调用")
    resolved = config or EdgeSapiConfig()
    active = runner or _run_sapi_voice_list
    try:
        output = active(resolved, str(_RUNNER_SCRIPT_PATH))
    except EdgeSapiError:
        raise
    except Exception:
        raise EdgeSapiError("sapi_voice_list_failed") from None
    if not isinstance(output, str):
        raise EdgeSapiError("sapi_voice_list_invalid")
    return _parse_voice_list(output)


__all__ = [
    "EdgeSapiConfig",
    "EdgeSapiError",
    "EdgeSapiVoiceProvider",
    "list_edge_sapi_voices",
]
