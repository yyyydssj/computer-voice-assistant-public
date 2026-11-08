"""P9 批次 2.2：sherpa-onnx Silero VAD 人声判定（纯离线、fail closed）。

本模块把「这一帧是不是人声」折算成一个逐帧布尔判定，供自动收音驱动使用。它只描述
**输入边界**：不代表 ASR 可靠性、不构成任何授权或任务事实，也不进入安全链。

硬边界（离线模块）：
- 不打开任何设备、不联网、不创建线程或定时器；
- 不读写任何文件，只在构造时用 ``Path.is_file()`` 检查模型是否存在；
- 模块级只导入 stdlib（``math`` / ``struct`` / ``dataclasses`` / ``pathlib``）与
  ``app.audio.bus`` / ``app.contracts.base``；``sherpa_onnx`` 只在构造时**延迟导入**，
  导入失败按 ``dependency_missing`` fail closed；不导入任何安全链模块。

错误码（``SileroVadError`` 的 ``str()`` 只能是固定短代码，绝不含异常正文、路径或音频）：
- ``model_missing``：模型文件不存在；
- ``dependency_missing``：``import sherpa_onnx`` 失败；
- ``init_failed``：VAD 构造或 ``reset()`` 失败；
- ``detect_failed``：喂入窗口或读取判定结果失败（含判定结果不是布尔）；
- ``unsupported_sample_rate`` / ``unsupported_format``：帧契约与本检测器不符。

窗口聚合规则：内部保留未满窗样本，累积到 ``SILERO_WINDOW_SAMPLES``（512）的整数倍时
按整窗喂给 VAD（``int16 / 32768.0`` 归一为 float）；每个窗口喂完后读一次
``is_speech_detected()``，连续命中达到 ``speech_frames_required`` 才置位
``speech_started``；``observe()`` 返回「本帧内是否发生过一次命中」——本帧没有凑满任何
整窗时返回 ``False``（残余样本保留到下一帧）。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

from app.audio.bus import AudioPacket
from app.contracts.base import ContractError, require_positive

SILERO_WINDOW_SAMPLES = 512
"""Silero VAD 的固定窗口长度（与 ``config.silero_vad.window_size`` 一致）。"""

MODEL_MISSING = "model_missing"
DEPENDENCY_MISSING = "dependency_missing"
INIT_FAILED = "init_failed"
DETECT_FAILED = "detect_failed"
UNSUPPORTED_SAMPLE_RATE = "unsupported_sample_rate"
UNSUPPORTED_FORMAT = "unsupported_format"

_ERROR_CODES = frozenset(
    {
        MODEL_MISSING,
        DEPENDENCY_MISSING,
        INIT_FAILED,
        DETECT_FAILED,
        UNSUPPORTED_SAMPLE_RATE,
        UNSUPPORTED_FORMAT,
    }
)

_PCM_S16LE = "pcm_s16le"
_S16_FULL_SCALE = 32768.0

# 项目内默认模型：models/openwakeword/silero_vad.onnx（app/audio/vad/silero.py -> 仓库根）。
DEFAULT_SILERO_MODEL_PATH = (
    Path(__file__).parents[3] / "models" / "openwakeword" / "silero_vad.onnx"
)


class SileroVadError(RuntimeError):
    """Silero VAD 不可用或检测失败的 fail-closed 结果。

    ``str()`` **只**返回固定短代码；构造时的未知文本一律收敛为 ``init_failed``，避免把
    异常正文、文件路径或音频内容带出模块。
    """

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else INIT_FAILED
        super().__init__(self.code)


def _require_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"{name} 必须是有限数值")
    return float(value)


@dataclass(frozen=True, slots=True)
class SileroVadConfig:
    """Silero VAD 的构造参数；只描述输入边界，不改变任何安全判定。

    ``model_path=None`` 时使用项目默认模型 ``models/openwakeword/silero_vad.onnx``。
    ``speech_frames_required`` 是连续 ``is_speech_detected()`` 命中次数，达到后才置位
    ``SileroVadDetector.speech_started``。
    """

    model_path: Path | None = None
    threshold: float = 0.5
    min_silence_duration: float = 0.5
    min_speech_duration: float = 0.15
    buffer_size_in_seconds: float = 30.0
    sample_rate_hz: int = 16000
    speech_frames_required: int = 2

    def __post_init__(self) -> None:
        path = self.model_path
        if path is not None:
            if isinstance(path, Path):
                resolved_path: Path = path
            elif isinstance(path, str) and path:
                resolved_path = Path(path)
            else:
                raise ContractError("model_path 必须是 Path、非空字符串或 None")
            object.__setattr__(self, "model_path", resolved_path)
        threshold = _require_number("threshold", self.threshold)
        if not 0.0 < threshold <= 1.0:
            raise ContractError("threshold 必须在 (0,1] 内")
        min_silence = _require_number("min_silence_duration", self.min_silence_duration)
        if min_silence <= 0.0:
            raise ContractError("min_silence_duration 必须大于 0")
        min_speech = _require_number("min_speech_duration", self.min_speech_duration)
        if min_speech <= 0.0:
            raise ContractError("min_speech_duration 必须大于 0")
        buffer_seconds = _require_number("buffer_size_in_seconds", self.buffer_size_in_seconds)
        if buffer_seconds <= 0.0:
            raise ContractError("buffer_size_in_seconds 必须大于 0")
        require_positive("sample_rate_hz", self.sample_rate_hz)
        require_positive("speech_frames_required", self.speech_frames_required)
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "min_silence_duration", min_silence)
        object.__setattr__(self, "min_speech_duration", min_speech)
        object.__setattr__(self, "buffer_size_in_seconds", buffer_seconds)

    @property
    def resolved_model_path(self) -> Path:
        """实际使用的模型路径；``model_path=None`` 时是项目默认模型。"""

        return DEFAULT_SILERO_MODEL_PATH if self.model_path is None else self.model_path


class SileroVadDetector:
    """把 PCM16 LE 单声道帧聚合成 512 样本窗口并读取 Silero VAD 判定。

    只持有内存缓冲与计数：不打开设备、不读写文件、不创建线程或定时器。``vad`` 可注入
    替身（离线测试用），注入时不检查模型文件、不导入 ``sherpa_onnx``。
    """

    def __init__(self, *, config: SileroVadConfig | None = None, vad: object | None = None) -> None:
        if config is None:
            config = SileroVadConfig()
        elif not isinstance(config, SileroVadConfig):
            raise ContractError("config 必须是 SileroVadConfig 或 None")
        self._config = config
        self._model_path = config.resolved_model_path
        self._pending: list[int] = []
        self._consecutive_speech = 0
        self._speech_started = False
        self._observed_frames = 0
        # 段级计数是**检测器生命期**的累计诊断（不随 reset() 清零），便于跨轮次复核。
        self._segment_probe_errors = 0
        self._flush_calls = 0
        if vad is None:
            self._injected = False
            self._vad = _build_sherpa_vad(config, self._model_path)
        else:
            for name in ("accept_waveform", "is_speech_detected", "reset"):
                if not callable(getattr(vad, name, None)):
                    raise ContractError(f"注入的 vad 必须提供 {name}()")
            self._injected = True
            self._vad = vad

    @property
    def config(self) -> SileroVadConfig:
        return self._config

    @property
    def speech_started(self) -> bool:
        """连续命中是否已达到 ``speech_frames_required``；``reset()`` 后回到 False。"""

        return self._speech_started

    @property
    def consecutive_speech(self) -> int:
        """当前连续命中的窗口数；未命中即清零。"""

        return self._consecutive_speech

    @property
    def observed_frames(self) -> int:
        """已通过契约校验并观察的帧数。"""

        return self._observed_frames

    @property
    def injected(self) -> bool:
        """是否使用了注入替身（真机路径为 False）。"""

        return self._injected

    @property
    def model_path(self) -> Path:
        """实际使用的模型路径（只读事实，注入替身时同样可读）。"""

        return self._model_path

    @property
    def segment_api_available(self) -> bool:
        """底层 VAD 是否提供段级 API（``empty()``）；注入的旧替身为 ``False``。"""

        return callable(getattr(self._vad, "empty", None))

    @property
    def segment_probe_errors(self) -> int:
        """段级探测/尾段定稿失败的累计次数（只读诊断；绝不参与判定）。"""

        return self._segment_probe_errors

    @property
    def flush_calls(self) -> int:
        """成功调用底层 ``flush()`` 的累计次数（只读诊断）。"""

        return self._flush_calls

    def segment_pending(self) -> bool:
        """底层 VAD 是否已产出一个**已结束的人声段**（``empty()`` 取反）。

        ``empty()`` 为 True 表示"没有已结束的人声段"。**不可判定时一律返回 ``False``**：
        旧替身没有 ``empty()``、调用抛错或返回非布尔都只累加 ``segment_probe_errors``，
        绝不据此判定"人声结束"——宁可多录一段，也不在用户话说到一半时切断输入。
        """

        if not self.segment_api_available:
            return False
        try:
            empty = self._vad.empty()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 段级探测失败不得影响收音
            self._segment_probe_errors += 1
            return False
        if type(empty) is not bool:
            self._segment_probe_errors += 1
            return False
        return not empty

    def flush_segment(self) -> None:
        """把缓冲尾段定稿（``flush()``），保证最后一个字进入已结束段。

        底层没有 ``flush()`` 时是安全空操作；调用失败只累加 ``segment_probe_errors``，
        不抛错（音频收尾问题不得升级为轮次故障）。
        """

        flush = getattr(self._vad, "flush", None)
        if not callable(flush):
            return
        try:
            flush()
        except Exception:  # noqa: BLE001 - 尾段定稿失败不得影响收尾
            self._segment_probe_errors += 1
            return
        self._flush_calls += 1

    def to_details(self) -> dict[str, object]:
        """只读诊断事实：模型、阈值、窗口参数与段级计数；不含任何音频内容。"""

        return {
            "mode": "silero",
            "model_path": str(self._model_path),
            "model_name": self._model_path.name,
            "model_present": bool(self._model_path.is_file()),
            "threshold": self._config.threshold,
            "min_silence_duration": self._config.min_silence_duration,
            "min_speech_duration": self._config.min_speech_duration,
            "speech_frames_required": self._config.speech_frames_required,
            "window_samples": SILERO_WINDOW_SAMPLES,
            "sample_rate_hz": self._config.sample_rate_hz,
            "buffer_size_in_seconds": self._config.buffer_size_in_seconds,
            "injected": self._injected,
            "segment_api_available": self.segment_api_available,
            "segment_probe_errors": self._segment_probe_errors,
            "flush_calls": self._flush_calls,
        }

    def observe(self, packet: AudioPacket) -> bool:
        """聚合一帧并返回「本帧内是否发生过一次语音命中」。

        非 ``AudioPacket``、非字节载荷或奇数字节载荷一律 fail closed（``TypeError`` /
        ``ValueError``），不静默返回 ``False``；采样率/格式/声道不符抛
        ``SileroVadError``；VAD 调用失败抛 ``SileroVadError("detect_failed")``。
        """

        if not isinstance(packet, AudioPacket):
            raise TypeError("observe 只接受 AudioPacket")
        frame = packet.frame
        payload = packet.payload
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload 必须是 bytes/bytearray/memoryview")
        raw = bytes(payload)
        if len(raw) % 2 != 0:
            raise ValueError("payload 长度必须是偶数字节（pcm_s16le）")
        if frame.sample_rate_hz != self._config.sample_rate_hz:
            raise SileroVadError(UNSUPPORTED_SAMPLE_RATE)
        if frame.sample_format != _PCM_S16LE or frame.channel_count != 1:
            raise SileroVadError(UNSUPPORTED_FORMAT)
        self._observed_frames += 1
        if raw:
            self._pending.extend(struct.unpack(f"<{len(raw) // 2}h", raw))
        hit = False
        while len(self._pending) >= SILERO_WINDOW_SAMPLES:
            window = self._pending[:SILERO_WINDOW_SAMPLES]
            del self._pending[:SILERO_WINDOW_SAMPLES]
            detected = self._detect(window)
            if detected:
                self._consecutive_speech += 1
                if self._consecutive_speech >= self._config.speech_frames_required:
                    self._speech_started = True
                hit = True
            else:
                self._consecutive_speech = 0
        return hit

    def reset(self) -> None:
        """清空残余样本与计数，并 ``vad.reset()``；VAD 复位失败按 ``init_failed`` fail closed。"""

        self._pending.clear()
        self._consecutive_speech = 0
        self._speech_started = False
        self._observed_frames = 0
        try:
            self._vad.reset()
        except Exception as exc:  # noqa: BLE001 - VAD 复位失败必须 fail closed
            raise SileroVadError(INIT_FAILED) from exc

    def _detect(self, window: list[int]) -> bool:
        """喂一个整窗并读判定；任何异常或非布尔判定都按 ``detect_failed`` fail closed。"""

        samples = [sample / _S16_FULL_SCALE for sample in window]
        try:
            self._vad.accept_waveform(samples)
            detected = self._vad.is_speech_detected()
        except Exception as exc:  # noqa: BLE001 - VAD 调用失败必须 fail closed
            raise SileroVadError(DETECT_FAILED) from exc
        if type(detected) is not bool:
            raise SileroVadError(DETECT_FAILED)
        return detected


def _build_sherpa_vad(config: SileroVadConfig, model_path: Path) -> object:
    """按冻结 API 构造 ``sherpa_onnx.VoiceActivityDetector``；延迟导入并 fail closed。

    模型不存在、依赖缺失或构造失败分别收敛为 ``model_missing`` / ``dependency_missing``
    / ``init_failed``，异常正文一律不外泄。
    """

    if not model_path.is_file():
        raise SileroVadError(MODEL_MISSING)
    try:
        import sherpa_onnx
    except Exception as exc:  # noqa: BLE001 - 依赖缺失必须 fail closed
        raise SileroVadError(DEPENDENCY_MISSING) from exc
    try:
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(model_path)
        vad_config.silero_vad.threshold = float(config.threshold)
        vad_config.silero_vad.min_silence_duration = float(config.min_silence_duration)
        vad_config.silero_vad.min_speech_duration = float(config.min_speech_duration)
        vad_config.silero_vad.window_size = SILERO_WINDOW_SAMPLES
        vad_config.sample_rate = int(config.sample_rate_hz)
        return sherpa_onnx.VoiceActivityDetector(
            vad_config, buffer_size_in_seconds=float(config.buffer_size_in_seconds)
        )
    except Exception as exc:  # noqa: BLE001 - 构造失败必须 fail closed
        raise SileroVadError(INIT_FAILED) from exc


def build_silero_vad_detector(*, config: SileroVadConfig | None = None) -> SileroVadDetector:
    """构造真机 Silero VAD 检测器；模型缺失/依赖缺失/构造失败即抛 ``SileroVadError``。"""

    return SileroVadDetector(config=config)


__all__ = [
    "DEFAULT_SILERO_MODEL_PATH",
    "DEPENDENCY_MISSING",
    "DETECT_FAILED",
    "INIT_FAILED",
    "MODEL_MISSING",
    "SILERO_WINDOW_SAMPLES",
    "UNSUPPORTED_FORMAT",
    "UNSUPPORTED_SAMPLE_RATE",
    "SileroVadConfig",
    "SileroVadDetector",
    "SileroVadError",
    "build_silero_vad_detector",
]
