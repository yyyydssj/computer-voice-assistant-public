"""Persistent user-owned VoiceStudio selection; never an engine-wide default."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import getpass
import json
import math
import os
import tempfile
import urllib.request


class VoiceSelectionError(ValueError):
    """Invalid private selection or unavailable/mismatched provider profile.

    ``code`` 是**可选的固定短码**（例如 ``provider_unreachable``）。真机教训
    （2026-10-09）：`verify_provider` 曾把内部六类原因统一改写成一句
    ``provider profile verification failed``，于是"本机服务没在跑"和"参考音频被换过"
    在控制台上长得一模一样，诊断时白花了一轮时间。句子式消息保持原样，既有调用方
    （含 `assertRaisesRegex`）不受影响。
    """

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


FILE_NAME = "xiaobing_voice_selection.json"
SELECTION_VERSION = 2
DEFAULT_SPEED = 1.0
SPEED_MIN = 0.5
SPEED_MAX = 2.0
"""可接受的播报语速区间（``--tts-speed`` 与本文件校验共用）。

真机实测（2026-10-23，11 字计划句）：speed 1.0 合成 1.36 s + 音频 1.76 s = 3.53 字/秒；
1.4 = 4.35 字/秒；1.6 = 4.71；2.0 = 5.21 但 RTF 反而从 0.71 升到 0.95。用户听感确认
**1.4 自然可用**，故默认值仍是 :data:`DEFAULT_SPEED`（1.0，行为不变），区间按实测收口到
0.5~2.0，避免误传极端值把播报变成快进音。
"""
DEFAULT_PAUSE_MS = 400
DEFAULT_FADE_MS = 80


def private_data_root() -> Path:
    """Stable across desktop-app environment overrides and ordinary Python."""
    return Path.home() / "AppData/Local/ComputerVoiceAssistant"

CONFIRMED_PROFILE = "e8572e8e"
CONFIRMED_NAME = "小饼·饼姐（沉稳声线候选3）"
CONFIRMED_REFERENCE_SHA256 = "456896c040ef8993b946c5d14c0c7569c06c9fe2b3f7b343a6c10edf5179d5ea"
REFERENCE_PATH = Path.home() / "AppData/Roaming/OmniVoice/voices/e8572e8e.wav"


@dataclass(frozen=True, slots=True)
class VoiceSelection:
    profile_id: str
    owner: str
    speed: float = DEFAULT_SPEED
    pause_ms: int = DEFAULT_PAUSE_MS
    fade_ms: int = DEFAULT_FADE_MS
    reference_sha256: str = CONFIRMED_REFERENCE_SHA256
    version: int = SELECTION_VERSION

    def validate(self) -> None:
        if (self.version != SELECTION_VERSION or self.profile_id != CONFIRMED_PROFILE
                or self.owner != getpass.getuser()
                or type(self.speed) is not float or not math.isfinite(self.speed)
                or not (SPEED_MIN <= self.speed <= SPEED_MAX) or type(self.pause_ms) is not int
                or self.pause_ms != DEFAULT_PAUSE_MS or type(self.fade_ms) is not int
                or self.fade_ms != DEFAULT_FADE_MS
                or self.reference_sha256 != CONFIRMED_REFERENCE_SHA256):
            raise VoiceSelectionError("selection does not match confirmed user/version/voice")


def selection_path(data_root: Path) -> Path:
    if not data_root.is_absolute() or data_root.is_symlink() or (data_root.exists() and not data_root.is_dir()):
        raise VoiceSelectionError("private data root must be an absolute non-symlink directory")
    return data_root / FILE_NAME


def read_selection(data_root: Path) -> VoiceSelection:
    path = selection_path(data_root)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise VoiceSelectionError("selection missing, unsafe or oversized")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"profile_id", "owner", "speed", "pause_ms", "fade_ms", "reference_sha256", "version"}:
            raise VoiceSelectionError("invalid selection schema")
        selection = VoiceSelection(**raw)
        selection.validate()
        return selection
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise VoiceSelectionError("invalid private selection") from exc


def write_selection(data_root: Path, selection: VoiceSelection) -> None:
    selection.validate()
    path = selection_path(data_root)
    if not data_root.is_dir() or path.is_symlink():
        raise VoiceSelectionError("unsafe or absent private data directory")
    if path.exists():
        raise VoiceSelectionError("selection already exists; explicit migration required")
    fd, temporary = tempfile.mkstemp(prefix=".voice-selection-", dir=data_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump({"profile_id": selection.profile_id, "owner": selection.owner,
                       "speed": selection.speed, "pause_ms": selection.pause_ms,
                       "fade_ms": selection.fade_ms,
                       "reference_sha256": selection.reference_sha256,
                       "version": selection.version}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic no-clobber creation: a second writer must never replace an
        # existing (possibly different-user) selection after the check above.
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise VoiceSelectionError("selection already exists; explicit migration required") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_reference(selection: VoiceSelection, reference_path: Path = REFERENCE_PATH) -> None:
    selection.validate()
    if reference_path.is_symlink() or not reference_path.is_file() or reference_path.stat().st_size > 4_000_000:
        raise VoiceSelectionError("reference audio unavailable or unsafe")
    if sha256(reference_path.read_bytes()).hexdigest() != selection.reference_sha256:
        raise VoiceSelectionError("reference audio changed")


def _fixed(code: str) -> VoiceSelectionError:
    """固定短码错误：``str()`` **就是码本身**，控制台/日志不需要再从长句里解析原因。

    码只用一小组小写短码，正文、路径与 profile 内容绝不进码。
    """

    return VoiceSelectionError(code, code=code)


def verify_provider(selection: VoiceSelection, base_url: str = "http://127.0.0.1:3900") -> None:
    """Read-only identity plus exact reference audio, without logging profile contents.

    每一类失败都带自己的**固定短码**（见 :func:`_fixed`）：真机上"本机服务没在跑"
    与"参考音频被换过"必须一眼可分，不许再共用一句兜底文案（2026-10-09 真机教训）。
    """

    selection.validate()
    if base_url != "http://127.0.0.1:3900":
        raise _fixed("provider_endpoint_not_loopback")
    try:
        try:
            with urllib.request.urlopen(base_url + "/profiles", timeout=5) as response:
                if response.status != 200:
                    # 端口在听、但后端还在启动（`StartupGate` 返回 503）或报错：与"连不上"分开。
                    raise _fixed("provider_not_ready")
                data = response.read(256_001)
        except OSError:
            raise _fixed("provider_unreachable") from None
        if len(data) > 256_000:
            raise _fixed("provider_response_too_large")
        try:
            profiles = json.loads(data)
        except (TypeError, ValueError):
            raise _fixed("provider_response_invalid") from None
        if not isinstance(profiles, list):
            raise _fixed("provider_response_invalid")
        matches = [p for p in profiles if isinstance(p, dict) and p.get("id") == selection.profile_id]
        if (len(matches) != 1 or matches[0].get("name") != CONFIRMED_NAME
                or matches[0].get("ref_audio_path") != "e8572e8e.wav"
                or matches[0].get("kind") != "design"):
            raise _fixed("provider_profile_not_found")
        # The provider can retain a name while replacing the underlying audio.
        try:
            with urllib.request.urlopen(
                base_url + "/profiles/" + selection.profile_id + "/audio", timeout=5
            ) as response:
                if response.status != 200:
                    raise _fixed("provider_reference_audio_unavailable")
                audio = response.read(4_000_001)
        except OSError:
            raise _fixed("provider_unreachable") from None
        if len(audio) > 4_000_000:
            raise _fixed("provider_response_too_large")
        if sha256(audio).hexdigest() != selection.reference_sha256:
            raise _fixed("provider_reference_audio_changed")
    except VoiceSelectionError:
        # 上面抛出的已经是最终固定短码：绝不再被下面的兜底改写。
        raise
    except (ValueError, TypeError) as exc:
        # 只有真正意外的异常才落到这里；仍留一个固定码，便于日志分桶。
        raise VoiceSelectionError(
            "provider profile verification failed", code="provider_verification_failed"
        ) from exc






