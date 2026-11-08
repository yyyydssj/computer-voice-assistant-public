"""P6 批次 2：输入设备检查、采集生命周期、pre-roll 与临时音频清理。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
import secrets
from typing import Any, Callable, Protocol

from app.audio.bus import AudioBus, AudioBusDecision, AudioPacket
from app.contracts import AudioFrame
from app.contracts.base import ContractError, require_identifier, require_non_empty, require_positive


class DeviceCheckStatus(str, Enum):
    CHECKING = "checking"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    DISCONNECTED = "disconnected"
    PERMISSION_DENIED = "permission_denied"
    INCOMPATIBLE = "incompatible"
    SWITCHING = "switching"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    device_id: str
    name: str
    max_input_channels: int

    def __post_init__(self) -> None:
        require_identifier("device_id", self.device_id)
        require_non_empty("name", self.name, maximum=256)
        require_positive("max_input_channels", self.max_input_channels, allow_zero=True)


@dataclass(frozen=True, slots=True)
class DeviceSelection:
    mode: str
    device_id: str | None = None

    @classmethod
    def fixed(cls, device_id: str) -> "DeviceSelection":
        require_identifier("device_id", device_id)
        return cls(mode="fixed", device_id=device_id)

    @classmethod
    def system_default(cls) -> "DeviceSelection":
        return cls(mode="system_default")

    def __post_init__(self) -> None:
        if self.mode not in {"fixed", "system_default"}:
            raise ContractError("输入设备选择模式非法")
        if self.mode == "fixed":
            require_identifier("device_id", self.device_id)
        elif self.device_id is not None:
            raise ContractError("system_default 不得携带 device_id")


@dataclass(frozen=True, slots=True)
class DeviceCheckResult:
    status: DeviceCheckStatus
    device_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    sample_rate_hz: int = 16000
    channel_count: int = 1
    frame_duration_ms: int = 20

    def __post_init__(self) -> None:
        require_positive("sample_rate_hz", self.sample_rate_hz)
        require_positive("channel_count", self.channel_count)
        require_positive("frame_duration_ms", self.frame_duration_ms)
        if self.sample_rate_hz * self.frame_duration_ms % 1000 != 0:
            raise ContractError("frame_duration_ms 必须对应整数采样数")

    @property
    def sample_count(self) -> int:
        return self.sample_rate_hz * self.frame_duration_ms // 1000

    @property
    def payload_size_bytes(self) -> int:
        return self.sample_count * self.channel_count * 2


def _default_input_index(default: object) -> int | None:
    """从 sounddevice 的默认设备描述里取出输入侧索引。

    sounddevice 0.5.x 起 ``sd.default.device`` 是 ``_InputOutputPair``，既不是 tuple
    也不是 list；必须显式支持 ``input`` 属性与下标访问，不能直接 ``int()``。无法确认
    时返回 ``None``，由调用方 fail closed（视为没有可用默认设备）。
    """

    candidate: object | None = getattr(default, "input", None)
    if candidate is None and not isinstance(default, (str, bytes, int)):
        try:
            candidate = default[0]  # type: ignore[index]
        except (TypeError, KeyError, IndexError):
            candidate = None
    if candidate is None:
        candidate = default
    try:
        return int(candidate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _device_argument(value: object) -> object:
    """sounddevice 的 ``device=`` 把字符串当作设备名子串；纯数字字符串必须转成索引。

    本项目内部统一用 ``DeviceInfo.device_id`` 字符串表示索引，若不归一化，
    ``sd.InputStream(device="1")`` 会按名称查找并抛 ``ValueError: No input device
    matching '1'``。
    """

    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


class SoundDeviceCaptureBackend:
    """sounddevice 的窄适配层；导入和设备访问均延迟到调用时。"""
    def list_input_devices(self) -> tuple[DeviceInfo, ...]:
        import sounddevice as sd

        devices = sd.query_devices()
        return tuple(
            DeviceInfo(
                device_id=str(index),
                name=str(device.get("name", "unnamed")),
                max_input_channels=int(device.get("max_input_channels", 0)),
            )
            for index, device in enumerate(devices)
            if int(device.get("max_input_channels", 0)) > 0
        )

    def default_input_device(self) -> str | None:
        import sounddevice as sd

        value = _default_input_index(sd.default.device)
        return None if value is None or value < 0 else str(value)

    def check_input_settings(self, *, device_id: str | None, sample_rate_hz: int, channel_count: int) -> None:
        import sounddevice as sd

        sd.check_input_settings(
            device=None if device_id is None else _device_argument(device_id),
            samplerate=sample_rate_hz,
            channels=channel_count,
            dtype="int16",
        )

    def open_input_stream(self, **kwargs: object) -> Any:
        import sounddevice as sd

        if "device" in kwargs:
            kwargs["device"] = _device_argument(kwargs["device"])
        return sd.InputStream(**kwargs)

    def check_device(
        self,
        selection: DeviceSelection,
        *,
        sample_rate_hz: int,
        channel_count: int,
    ) -> DeviceCheckResult:
        try:
            devices = self.list_input_devices()
            device_id = selection.device_id if selection.mode == "fixed" else self.default_input_device()
        except PermissionError as exc:
            return DeviceCheckResult(DeviceCheckStatus.PERMISSION_DENIED, None, str(exc) or "麦克风权限被拒绝")
        except Exception as exc:  # noqa: BLE001 - 设备 API 可能抛任意厂商异常
            return DeviceCheckResult(DeviceCheckStatus.DISCONNECTED, None, str(exc) or "无法枚举输入设备")
        if device_id is None:
            return DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, None, "没有可用的输入设备")
        device = next((item for item in devices if item.device_id == device_id), None)
        if device is None:
            return DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, None, "用户选择的输入设备不存在")
        if device.max_input_channels < channel_count:
            return DeviceCheckResult(DeviceCheckStatus.INCOMPATIBLE, device_id, "输入设备声道数不足")
        try:
            self.check_input_settings(device_id=device_id, sample_rate_hz=sample_rate_hz, channel_count=channel_count)
        except PermissionError as exc:
            return DeviceCheckResult(DeviceCheckStatus.PERMISSION_DENIED, device_id, str(exc) or "麦克风权限被拒绝")
        except Exception as exc:  # noqa: BLE001 - 设备 API 可能抛任意厂商异常（含 PortAudioError）
            text = str(exc) or "输入设备不可用"
            lowered = text.lower()
            if "busy" in lowered or "使用" in text or "占用" in text:
                status = DeviceCheckStatus.BUSY
            elif "permission" in lowered or "权限" in text or "access" in lowered:
                status = DeviceCheckStatus.PERMISSION_DENIED
            elif "sample" in lowered or "channel" in lowered or "采样" in text or "声道" in text:
                status = DeviceCheckStatus.INCOMPATIBLE
            elif "disconnect" in lowered or "断开" in text:
                status = DeviceCheckStatus.DISCONNECTED
            else:
                status = DeviceCheckStatus.UNAVAILABLE
            return DeviceCheckResult(status, device_id, text)
        return DeviceCheckResult(DeviceCheckStatus.READY, device_id, "输入设备可用")


class CaptureSessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CaptureStartResult:
    started: bool
    check: DeviceCheckResult


class CaptureStream(Protocol):
    def start(self) -> Any: ...
    def stop(self) -> Any: ...
    def close(self) -> Any: ...


class PreRollBuffer:
    def __init__(self, *, capacity_frames: int, stream_id: str | None = None) -> None:
        require_positive("capacity_frames", capacity_frames)
        if stream_id is not None:
            require_identifier("stream_id", stream_id)
        self._stream_id = stream_id
        self._items: deque[AudioPacket] = deque(maxlen=capacity_frames)

    def append(self, packet: AudioPacket) -> None:
        if self._stream_id is not None and packet.frame.stream_id != self._stream_id:
            raise ValueError("pre-roll 帧属于其他 stream")
        self._items.append(AudioPacket(frame=packet.frame, payload=bytes(packet.payload)))

    def snapshot(self) -> tuple[AudioPacket, ...]:
        return tuple(self._items)

    def clear(self) -> None:
        self._items.clear()


class CaptureSession:
    def __init__(
        self,
        *,
        bus: AudioBus,
        backend: SoundDeviceCaptureBackend,
        selection: DeviceSelection,
        config: CaptureConfig,
        pre_roll: PreRollBuffer | None = None,
    ) -> None:
        self._bus = bus
        self._backend = backend
        self._selection = selection
        self._config = config
        self._pre_roll = pre_roll
        self._stream: CaptureStream | None = None
        self._state = CaptureSessionState.IDLE
        self._next_event_sequence = 1
        self._next_frame_index = 0

    @property
    def state(self) -> CaptureSessionState:
        return self._state

    def start(self) -> CaptureStartResult:
        if self._state is not CaptureSessionState.IDLE:
            result = DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, None, "采集会话已结束")
            return CaptureStartResult(False, result)
        self._state = CaptureSessionState.STARTING
        check = self._backend.check_device(
            self._selection,
            sample_rate_hz=self._config.sample_rate_hz,
            channel_count=self._config.channel_count,
        )
        if check.status is not DeviceCheckStatus.READY or check.device_id is None:
            self._state = CaptureSessionState.FAILED
            return CaptureStartResult(False, check)
        try:
            self._stream = self._backend.open_input_stream(
                device=check.device_id,
                samplerate=self._config.sample_rate_hz,
                channels=self._config.channel_count,
                dtype="int16",
                blocksize=self._config.sample_count,
                callback=self._callback,
            )
            self._state = CaptureSessionState.ACTIVE
            self._stream.start()
            if self._state is CaptureSessionState.FAILED:
                return CaptureStartResult(
                    False,
                    DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, check.device_id, "采集启动期间发生故障"),
                )
        except PermissionError as exc:
            self._fail_capture("permission_denied", close_stream=True)
            return CaptureStartResult(False, DeviceCheckResult(DeviceCheckStatus.PERMISSION_DENIED, check.device_id, str(exc)))
        except (OSError, RuntimeError) as exc:
            self._fail_capture("capture_start_failed", close_stream=True)
            return CaptureStartResult(False, DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, check.device_id, str(exc) or "无法启动采集"))
        return CaptureStartResult(True, check)

    def stop(self, *, reason: str, discard: bool) -> None:
        require_identifier("reason", reason)
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None
        if self._state is CaptureSessionState.ACTIVE:
            self._bus.close(reason=reason, discard=discard)
            self._state = CaptureSessionState.STOPPED

    def _callback(self, indata: Any, frames: int, time_info: Any = None, status: Any = None) -> None:
        del time_info
        if self._state is not CaptureSessionState.ACTIVE:
            return
        if status:
            self._fail_capture("capture_callback_status", close_stream=True)
            return
        try:
            payload = indata.tobytes() if hasattr(indata, "tobytes") else bytes(indata)
            if frames != self._config.sample_count or len(payload) != self._config.payload_size_bytes:
                self._fail_capture("incomplete_audio_frame", close_stream=True)
                return
            frame = AudioFrame(
                audio_session_id=self._bus.audio_session_id,
                provider_generation=self._bus.provider_generation,
                stream_id=self._bus.stream_id,
                event_sequence=self._next_event_sequence,
                frame_index=self._next_frame_index,
                captured_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                duration_ms=self._config.frame_duration_ms,
                device_epoch=self._bus.device_epoch,
                sample_rate_hz=self._config.sample_rate_hz,
                channel_count=self._config.channel_count,
                sample_format="pcm_s16le",
                sample_count=frames,
                payload_size_bytes=len(payload),
            )
            decision = self._bus.publish(frame, payload)
            if decision is not AudioBusDecision.ACCEPTED:
                self._fail_capture(f"audio_bus_{decision.value}", close_stream=True)
                return
            if self._pre_roll is not None:
                self._pre_roll.append(AudioPacket(frame=frame, payload=bytes(payload)))
            self._next_event_sequence += 1
            self._next_frame_index += 1
        except (ContractError, TypeError, ValueError, OverflowError):
            self._fail_capture("invalid_audio_frame", close_stream=True)

    def _fail_capture(self, reason: str, *, close_stream: bool) -> None:
        if self._state is CaptureSessionState.FAILED:
            return
        if close_stream and self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._bus.close(reason=reason, discard=True)
        if self._pre_roll is not None:
            self._pre_roll.clear()
        self._state = CaptureSessionState.FAILED


class FakeCaptureStream:
    """仅供单元测试注入的无设备 stream。"""

    def __init__(self) -> None:
        self.callback: Callable[..., Any] | None = None
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def emit(self, payload: bytes, *, frames: int, status: Any = None) -> None:
        if self.callback is None:
            raise RuntimeError("callback 尚未绑定")
        self.callback(payload, frames, None, status)


class TemporaryCleanupStatus(str, Enum):
    DELETED = "deleted"
    NOT_OWNED = "not_owned"
    MISSING = "missing"
    CLEANUP_PENDING = "cleanup_pending"


class TemporaryAudioStore:
    _MAGIC = "assistant-temporary-audio-v1"
    _MARKER_SUFFIX = ".owner"

    def __init__(self, root: Path | str, *, max_age_seconds: int = 60) -> None:
        self._root = Path(root).expanduser().resolve()
        require_positive("max_age_seconds", max_age_seconds)
        self._max_age = timedelta(seconds=max_age_seconds)
        self._root.mkdir(parents=True, exist_ok=True)

    def create(self, *, suffix: str = ".pcm") -> Path:
        if not suffix.startswith(".") or len(suffix) > 16 or any(char in suffix for char in "\\/\r\n"):
            raise ValueError("临时音频后缀非法")
        path = self._root / f"{secrets.token_urlsafe(24)}{suffix}"
        marker = self._marker_path(path)
        path.touch(exist_ok=False)
        try:
            marker.write_text(
                json.dumps({"magic": self._MAGIC, "name": path.name}), encoding="utf-8"
            )
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return path

    def write(self, path: Path | str, payload: bytes | bytearray | memoryview) -> None:
        resolved = self._owned_path(Path(path).expanduser().resolve())
        if resolved is None:
            raise PermissionError("临时音频路径不属于本程序")
        resolved.write_bytes(bytes(payload))

    def cleanup(self, path: Path | str) -> TemporaryCleanupStatus:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists() and not self._marker_path(resolved).exists():
            return TemporaryCleanupStatus.MISSING
        if self._owned_path(resolved) is None:
            return TemporaryCleanupStatus.NOT_OWNED
        try:
            resolved.unlink(missing_ok=True)
            self._marker_path(resolved).unlink(missing_ok=True)
        except OSError:
            return TemporaryCleanupStatus.CLEANUP_PENDING
        return TemporaryCleanupStatus.DELETED

    def cleanup_expired(self, *, now: datetime | None = None) -> tuple[Path, ...]:
        current = now or datetime.now(timezone.utc)
        deleted: list[Path] = []
        for marker in self._root.glob(f"*{self._MARKER_SUFFIX}"):
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                path = (self._root / str(metadata.get("name", ""))).resolve()
                if metadata.get("magic") != self._MAGIC or self._owned_path(path) is None:
                    continue
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if current - modified >= self._max_age and self.cleanup(path) is TemporaryCleanupStatus.DELETED:
                    deleted.append(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return tuple(deleted)

    def _marker_path(self, path: Path) -> Path:
        return path.with_name(path.name + self._MARKER_SUFFIX)

    def _owned_path(self, path: Path) -> Path | None:
        try:
            path.relative_to(self._root)
        except ValueError:
            return None
        marker = self._marker_path(path)
        if not marker.is_file():
            return None
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if metadata.get("magic") != self._MAGIC or metadata.get("name") != path.name:
            return None
        return path


__all__ = [
    "CaptureConfig", "CaptureSession", "CaptureSessionState", "CaptureStartResult",
    "DeviceCheckResult", "DeviceCheckStatus", "DeviceInfo", "DeviceSelection",
    "FakeCaptureStream", "PreRollBuffer", "SoundDeviceCaptureBackend",
    "TemporaryAudioStore", "TemporaryCleanupStatus",
]
