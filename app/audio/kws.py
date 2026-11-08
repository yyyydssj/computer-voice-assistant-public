"""P9 B3：唤醒词门禁（sherpa-onnx ``KeywordSpotter``，纯离线、fail closed）。

本模块只回答一个布尔问题：**"刚刚这一帧里是否出现了唤醒词"**。它的唯一用途是
"只让唤醒词开启一个新的输入会话"：

- 它**不**决定授权、**不**决定计划或执行、**不**进入安全链、**不**构成身份凭证；
- 它**不**打开设备、不读写文件（只在构造时 ``Path.is_file()`` 检查模型存在）、不联网、
  不创建线程或定时器；
- ``sherpa_onnx`` 只在构造时**延迟导入**，导入失败按 ``dependency_missing`` fail closed。

错误码（``KeywordGateError`` 的 ``str()`` 只能是固定短代码，绝不含异常正文、路径或音频）：
- ``model_missing``：四个模型文件（tokens/encoder/decoder/joiner）缺失；
- ``keywords_missing``：没有可用的关键词文件（既未显式给出，``model_dir/keywords.txt`` 也不存在）；
- ``dependency_missing``：``import sherpa_onnx`` 失败；
- ``init_failed``：构造或 ``reset()`` 失败；
- ``detect_failed``：喂帧 / 解码 / 读结果失败，或结果不是字符串；
- ``unsupported_sample_rate`` / ``unsupported_format``：帧契约与本门禁不符。

判定口径：把每帧 PCM16LE 单声道样本（``int16 / 32768.0``）原样喂给在线流，然后
``while is_ready(stream): decode_stream(stream); result = get_result(stream)``；
一旦拿到非空结果就 ``reset_stream(stream)`` 并做**文本匹配**——
配置了 ``keyword`` 时要求归一化后完全相等，未配置时任意非空结果都算命中。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.audio.bus import AudioPacket
from app.contracts.base import ContractError, require_positive

MODEL_MISSING = "model_missing"
KEYWORDS_MISSING = "keywords_missing"
DEPENDENCY_MISSING = "dependency_missing"
INIT_FAILED = "init_failed"
DETECT_FAILED = "detect_failed"
UNSUPPORTED_SAMPLE_RATE = "unsupported_sample_rate"
UNSUPPORTED_FORMAT = "unsupported_format"

_ERROR_CODES = frozenset(
    {
        MODEL_MISSING,
        KEYWORDS_MISSING,
        DEPENDENCY_MISSING,
        INIT_FAILED,
        DETECT_FAILED,
        UNSUPPORTED_SAMPLE_RATE,
        UNSUPPORTED_FORMAT,
    }
)

_PCM_S16LE = "pcm_s16le"
_S16_FULL_SCALE = 32768.0

#: 唤醒词模型的四个文件在 ``model_dir`` 下的固定文件名（sherpa-onnx KWS 预训练模型的约定布局）。
KWS_MODEL_FILES = ("tokens.txt", "encoder.onnx", "decoder.onnx", "joiner.onnx")

_SPOTTER_METHODS = ("create_stream", "is_ready", "decode_stream", "get_result", "reset_stream")

#: 单帧内最多推进的解码步数：防止注入替身（或异常模型）让语音循环永远出不来。
MAX_DECODE_STEPS_PER_FRAME = 64


class KeywordGateError(RuntimeError):
    """唤醒词门禁不可用或检测失败的 fail-closed 结果。

    ``str()`` **只**返回固定短代码；构造时的未知文本一律收敛为 ``init_failed``，
    避免把异常正文、文件路径或音频内容带出模块。
    """

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else INIT_FAILED
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class KeywordGateConfig:
    """唤醒词模型与在线流的只读参数；不描述任何授权或身份语义。"""

    model_dir: Path | None = None
    tokens_path: Path | None = None
    encoder_path: Path | None = None
    decoder_path: Path | None = None
    joiner_path: Path | None = None
    keywords_file: Path | None = None
    keyword: str | None = None
    sample_rate_hz: int = 16000
    feature_dim: int = 80
    num_threads: int = 1
    max_active_paths: int = 4
    keywords_score: float = 1.0
    keywords_threshold: float = 0.25
    num_trailing_blanks: int = 1
    provider: str = "cpu"

    def __post_init__(self) -> None:
        for name in ("sample_rate_hz", "feature_dim", "num_threads"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractError(f"{name} 必须是正整数")
        require_positive("max_active_paths", self.max_active_paths)
        if isinstance(self.num_trailing_blanks, bool) or not isinstance(
            self.num_trailing_blanks, int
        ) or self.num_trailing_blanks < 0:
            raise ContractError("num_trailing_blanks 必须是非负整数")
        for name in ("keywords_score", "keywords_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContractError(f"{name} 必须是数值")
        if float(self.keywords_threshold) < 0.0:
            raise ContractError("keywords_threshold 必须是非负数")
        for name in (
            "model_dir",
            "tokens_path",
            "encoder_path",
            "decoder_path",
            "joiner_path",
            "keywords_file",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Path):
                object.__setattr__(self, name, Path(value))
        if self.keyword is not None and (not isinstance(self.keyword, str) or not self.keyword.strip()):
            raise ContractError("keyword 必须是非空字符串或 None")
        if not isinstance(self.provider, str) or not self.provider:
            raise ContractError("provider 必须是非空字符串")

    def resolved_paths(self) -> dict[str, Path | None]:
        """四个模型文件 + 关键词文件的解析结果。

        显式路径优先于 ``model_dir``；``keywords_file`` 未显式给出时，若
        ``model_dir/keywords.txt`` **确实存在**则自动采用它（避免"模型齐备但缺关键词文件"
        这种一上手就退出的坑）。
        """

        base = self.model_dir
        resolved: dict[str, Path | None] = {}
        explicit = {
            "tokens.txt": self.tokens_path,
            "encoder.onnx": self.encoder_path,
            "decoder.onnx": self.decoder_path,
            "joiner.onnx": self.joiner_path,
        }
        for file_name, value in explicit.items():
            resolved[file_name] = value if value is not None else (base / file_name if base else None)
        keywords = self.keywords_file
        if keywords is None and base is not None:
            candidate = base / "keywords.txt"
            keywords = candidate if candidate.is_file() else None
        resolved["keywords.txt"] = keywords
        return resolved


def _build_spotter(config: KeywordGateConfig, module_loader: Callable[[], Any] | None = None) -> Any:
    """按冻结参数构造 ``sherpa_onnx.KeywordSpotter``；延迟导入并 fail closed。"""

    paths = config.resolved_paths()
    for file_name in KWS_MODEL_FILES:
        path = paths.get(file_name)
        if path is None or not path.is_file():
            raise KeywordGateError(MODEL_MISSING)
    if paths.get("keywords.txt") is None:
        # 没有关键词文件时 sherpa 无法构造（它至少需要一份关键词）：给出专用短码，
        # 而不是让调用方只看到含混的 init_failed。
        raise KeywordGateError(KEYWORDS_MISSING)
    if module_loader is None:
        try:
            import sherpa_onnx
        except Exception as exc:  # noqa: BLE001 - 依赖缺失必须 fail closed
            raise KeywordGateError(DEPENDENCY_MISSING) from exc
        module = sherpa_onnx
    else:
        try:
            module = module_loader()
        except Exception as exc:  # noqa: BLE001 - 注入的模块加载器失败同样按依赖缺失处理
            raise KeywordGateError(DEPENDENCY_MISSING) from exc
    try:
        kwargs: dict[str, Any] = {
            "tokens": str(paths["tokens.txt"]),
            "encoder": str(paths["encoder.onnx"]),
            "decoder": str(paths["decoder.onnx"]),
            "joiner": str(paths["joiner.onnx"]),
            "num_threads": int(config.num_threads),
            "sample_rate": int(config.sample_rate_hz),
            "feature_dim": int(config.feature_dim),
            "max_active_paths": int(config.max_active_paths),
            "keywords_score": float(config.keywords_score),
            "keywords_threshold": float(config.keywords_threshold),
            "num_trailing_blanks": int(config.num_trailing_blanks),
            "provider": config.provider,
        }
        if paths.get("keywords.txt") is not None:
            kwargs["keywords_file"] = str(paths["keywords.txt"])
        return module.KeywordSpotter(**kwargs)
    except KeywordGateError:
        raise
    except Exception as exc:  # noqa: BLE001 - 构造失败必须 fail closed
        raise KeywordGateError(INIT_FAILED) from exc


class KeywordGate:
    """逐帧唤醒词门禁；``spotter`` 可注入替身（离线测试用，不导入 ``sherpa_onnx``）。"""

    def __init__(
        self,
        *,
        config: KeywordGateConfig | None = None,
        spotter: Any | None = None,
        module_loader: Callable[[], Any] | None = None,
    ) -> None:
        if config is None:
            config = KeywordGateConfig()
        elif not isinstance(config, KeywordGateConfig):
            raise ContractError("config 必须是 KeywordGateConfig 或 None")
        self._config = config
        self._stream: Any | None = None
        self._observed_frames = 0
        self._detected = 0
        self._decoded_steps = 0
        if spotter is None:
            self._injected = False
            self._spotter = _build_spotter(config, module_loader)
        else:
            for name in _SPOTTER_METHODS:
                if not callable(getattr(spotter, name, None)):
                    raise ContractError(f"注入的 spotter 必须提供 {name}()")
            self._injected = True
            self._spotter = spotter
        self._open_stream()

    # ---- 只读诊断面 -------------------------------------------------------

    @property
    def config(self) -> KeywordGateConfig:
        return self._config

    @property
    def injected(self) -> bool:
        """是否使用注入替身（真机路径为 False）。"""

        return self._injected

    @property
    def keyword(self) -> str | None:
        """配置的唤醒词文本；``None`` 表示任意命中都算。"""

        return self._config.keyword

    @property
    def detected_count(self) -> int:
        """累计命中次数（只读诊断）。"""

        return self._detected

    @property
    def observed_frames(self) -> int:
        """已通过契约校验并喂入的帧数（只读诊断）。"""

        return self._observed_frames

    @property
    def decoded_steps(self) -> int:
        """累计解码步数（只读诊断；用于确认在线流确实在被推进）。"""

        return self._decoded_steps

    def to_details(self) -> dict[str, object]:
        """只读诊断事实：模型路径与参数；不含任何音频内容。"""

        paths = self._config.resolved_paths()
        return {
            "mode": "keyword",
            "keyword": self._config.keyword,
            "model_dir": str(self._config.model_dir) if self._config.model_dir else None,
            "model_present": all(
                (paths.get(name) is not None and paths[name].is_file()) for name in KWS_MODEL_FILES
            ),
            "keywords_file": str(paths["keywords.txt"]) if paths.get("keywords.txt") else None,
            "keywords_threshold": self._config.keywords_threshold,
            "keywords_score": self._config.keywords_score,
            "num_trailing_blanks": self._config.num_trailing_blanks,
            "sample_rate_hz": self._config.sample_rate_hz,
            "feature_dim": self._config.feature_dim,
            "injected": self._injected,
        }

    # ---- 生命周期与判定 ---------------------------------------------------

    def reset(self) -> None:
        """开一个新流（检测到唤醒词后调用）；失败按 ``init_failed`` fail closed。"""

        self._open_stream()

    def observe(self, packet: AudioPacket) -> bool:
        """喂一帧并返回"本帧内是否命中唤醒词"。

        非 ``AudioPacket``、非字节载荷或奇数字节载荷一律 fail closed（``TypeError`` /
        ``ValueError``）；采样率/格式/声道不符抛 ``KeywordGateError``；解码失败抛
        ``KeywordGateError("detect_failed")``。
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
            raise KeywordGateError(UNSUPPORTED_SAMPLE_RATE)
        if frame.sample_format != _PCM_S16LE or frame.channel_count != 1:
            raise KeywordGateError(UNSUPPORTED_FORMAT)
        self._observed_frames += 1
        samples = [value / _S16_FULL_SCALE for value in struct.unpack(f"<{len(raw) // 2}h", raw)]
        stream = self._stream
        if stream is None:
            raise KeywordGateError(INIT_FAILED)
        try:
            stream.accept_waveform(self._config.sample_rate_hz, samples)
            hit = self._drain(stream)
        except KeywordGateError:
            raise
        except Exception as exc:  # noqa: BLE001 - 喂帧/解码失败必须 fail closed
            raise KeywordGateError(DETECT_FAILED) from exc
        return hit

    # ---- 内部实现 ---------------------------------------------------------

    def _open_stream(self) -> None:
        try:
            self._stream = self._spotter.create_stream()
        except Exception as exc:  # noqa: BLE001 - 建流失败必须 fail closed
            raise KeywordGateError(INIT_FAILED) from exc
        if self._stream is None:
            raise KeywordGateError(INIT_FAILED)

    def _drain(self, stream: Any) -> bool:
        detected = False
        steps = 0
        while True:
            ready = self._spotter.is_ready(stream)
            if type(ready) is not bool:
                raise KeywordGateError(DETECT_FAILED)
            if not ready:
                return detected
            if steps >= MAX_DECODE_STEPS_PER_FRAME:
                # 单帧不可能需要这么多步；注入替身/异常模型下必须 fail closed 而非死循环。
                raise KeywordGateError(DETECT_FAILED)
            steps += 1
            self._spotter.decode_stream(stream)
            self._decoded_steps += 1
            result = self._spotter.get_result(stream)
            if not isinstance(result, str):
                raise KeywordGateError(DETECT_FAILED)
            if result == "":
                continue
            # 只有命中才重置在线流；未命中的中间结果不重置（KWS 可能连续产出）。
            matched = self._matches(result)
            if matched:
                self._spotter.reset_stream(stream)
                self._detected += 1
                detected = True

    def _matches(self, result: str) -> bool:
        configured = self._config.keyword
        if configured is None:
            return True
        return _normalize(result) == _normalize(configured)


def _normalize(text: str) -> str:
    """唤醒词比较前归一化：去掉空白与大小写差异（不做任何模糊匹配）。"""

    return "".join(text.split()).casefold()


__all__ = [
    "DEPENDENCY_MISSING",
    "DETECT_FAILED",
    "INIT_FAILED",
    "KEYWORDS_MISSING",
    "KWS_MODEL_FILES",
    "MODEL_MISSING",
    "UNSUPPORTED_FORMAT",
    "UNSUPPORTED_SAMPLE_RATE",
    "KeywordGate",
    "KeywordGateConfig",
    "KeywordGateError",
]
