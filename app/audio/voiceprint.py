"""P9 B4：声纹误触发过滤（sherpa-onnx speaker embedding，纯离线、fail closed）。

本模块只回答一个问题：**"这段音频是不是已注册的说话人"**。它的唯一用途是把
"不是我在说话"的轮次当作误触发**丢弃**：

- 它**不**决定授权、**不**作为身份凭证、**不**进入安全链、**不**产生计划或执行；
- 过滤**失败**时绝不"授予"任何东西：调用方只能选择"这次不丢"，正常的 ASR / 授权链一个都不放过；
- 只处理**嵌入向量与 PCM**，不保存音频、不读写任何文件（嵌入向量由调用方决定如何持久化）；
- 不打开设备、不联网、不创建线程或定时器；``sherpa_onnx`` 只在构造时**延迟导入**。

错误码（``VoiceprintError`` 的 ``str()`` 只能是固定短代码，绝不含异常正文、路径或音频）：
- ``model_missing``：声纹模型文件不存在；
- ``dependency_missing``：``import sherpa_onnx``（或注入的模块加载器）失败；
- ``init_failed``：构造提取器 / 管理器失败；
- ``compute_failed``：喂帧、取嵌入或比对失败；
- ``unsupported_sample_rate`` / ``unsupported_format``：输入契约不符。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from app.contracts.base import ContractError, require_positive

MODEL_MISSING = "model_missing"
DEPENDENCY_MISSING = "dependency_missing"
INIT_FAILED = "init_failed"
COMPUTE_FAILED = "compute_failed"
UNSUPPORTED_SAMPLE_RATE = "unsupported_sample_rate"
UNSUPPORTED_FORMAT = "unsupported_format"

_ERROR_CODES = frozenset(
    {
        MODEL_MISSING,
        DEPENDENCY_MISSING,
        INIT_FAILED,
        COMPUTE_FAILED,
        UNSUPPORTED_SAMPLE_RATE,
        UNSUPPORTED_FORMAT,
    }
)

# 判定原因码（进入 ``VoiceprintDecision.reason``，同样是固定短代码）。
MATCHED = "matched"
BELOW_THRESHOLD = "below_threshold"
NOT_ENROLLED = "not_enrolled"
TOO_SHORT = "too_short"

_S16_FULL_SCALE = 32768.0

_EXTRACTOR_METHODS = ("create_stream", "is_ready", "compute")
_MANAGER_METHODS = ("add", "score", "remove")


class VoiceprintError(RuntimeError):
    """声纹过滤不可用或计算失败的 fail-closed 结果。

    ``str()`` **只**返回固定短代码；构造时的未知文本一律收敛为 ``init_failed``。
    """

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else INIT_FAILED
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VoiceprintConfig:
    """声纹模型与判定参数；``threshold`` 是余弦相似度阈值（越大越严）。"""

    model_path: Path | None = None
    threshold: float = 0.5
    speaker_name: str = "owner"
    num_threads: int = 1
    provider: str = "cpu"
    debug: bool = False

    def __post_init__(self) -> None:
        if self.model_path is not None and not isinstance(self.model_path, Path):
            object.__setattr__(self, "model_path", Path(self.model_path))
        threshold = self.threshold
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ContractError("threshold 必须是 (0, 1] 的数值")
        if not (0.0 < float(threshold) <= 1.0):
            raise ContractError("threshold 必须是 (0, 1] 的数值")
        object.__setattr__(self, "threshold", float(threshold))
        require_positive("num_threads", self.num_threads)
        if not isinstance(self.speaker_name, str) or not self.speaker_name:
            raise ContractError("speaker_name 必须是非空字符串")
        if not isinstance(self.provider, str) or not self.provider:
            raise ContractError("provider 必须是非空字符串")
        if type(self.debug) is not bool:
            raise ContractError("debug 必须是布尔值")


@dataclass(frozen=True, slots=True)
class VoiceprintDecision:
    """一次声纹判定：``matched`` 为真才表示"是已注册说话人"。"""

    matched: bool
    score: float
    reason: str

    def to_details(self) -> dict[str, object]:
        return {"matched": self.matched, "score": round(float(self.score), 6), "reason": self.reason}


def _build_extractor(config: VoiceprintConfig, module_loader: Callable[[], Any] | None = None) -> tuple[Any, Any]:
    """构造 ``(SpeakerEmbeddingExtractor, SpeakerEmbeddingManager)``；延迟导入并 fail closed。"""

    if config.model_path is None or not config.model_path.is_file():
        raise VoiceprintError(MODEL_MISSING)
    if module_loader is None:
        try:
            import sherpa_onnx
        except Exception as exc:  # noqa: BLE001 - 依赖缺失必须 fail closed
            raise VoiceprintError(DEPENDENCY_MISSING) from exc
        module = sherpa_onnx
    else:
        try:
            module = module_loader()
        except Exception as exc:  # noqa: BLE001 - 注入加载器失败同样按依赖缺失处理
            raise VoiceprintError(DEPENDENCY_MISSING) from exc
    try:
        extractor_config = module.SpeakerEmbeddingExtractorConfig(
            model=str(config.model_path),
            num_threads=int(config.num_threads),
            debug=bool(config.debug),
            provider=config.provider,
        )
        extractor = module.SpeakerEmbeddingExtractor(extractor_config)
        manager = module.SpeakerEmbeddingManager(extractor.dim)
        return extractor, manager
    except VoiceprintError:
        raise
    except Exception as exc:  # noqa: BLE001 - 构造失败必须 fail closed
        raise VoiceprintError(INIT_FAILED) from exc


class VoiceprintFilter:
    """声纹过滤；``extractor`` / ``manager`` 可注入替身（离线测试用，不导入 ``sherpa_onnx``）。"""

    def __init__(
        self,
        *,
        config: VoiceprintConfig | None = None,
        extractor: Any | None = None,
        manager: Any | None = None,
        module_loader: Callable[[], Any] | None = None,
    ) -> None:
        if config is None:
            config = VoiceprintConfig()
        elif not isinstance(config, VoiceprintConfig):
            raise ContractError("config 必须是 VoiceprintConfig 或 None")
        self._config = config
        self._verified = 0
        self._rejected = 0
        if extractor is None and manager is None:
            self._injected = False
            self._extractor, self._manager = _build_extractor(config, module_loader)
        else:
            if extractor is None or manager is None:
                raise ContractError("extractor 与 manager 必须同时注入")
            for name in _EXTRACTOR_METHODS:
                if not callable(getattr(extractor, name, None)):
                    raise ContractError(f"注入的 extractor 必须提供 {name}()")
            for name in _MANAGER_METHODS:
                if not callable(getattr(manager, name, None)):
                    raise ContractError(f"注入的 manager 必须提供 {name}()")
            self._injected = True
            self._extractor = extractor
            self._manager = manager

    # ---- 只读诊断面 -------------------------------------------------------

    @property
    def config(self) -> VoiceprintConfig:
        return self._config

    @property
    def injected(self) -> bool:
        return self._injected

    @property
    def enrolled_speakers(self) -> list[str]:
        """已注册说话人名（只读）。"""

        names = getattr(self._manager, "all_speakers", None)
        if callable(names):
            names = names()
        return [str(item) for item in names] if isinstance(names, (list, tuple)) else []

    @property
    def enrolled(self) -> bool:
        """是否至少注册了一个声纹；未注册时调用方应**不启用**过滤。"""

        count = getattr(self._manager, "num_speakers", 0)
        if callable(count):
            count = count()
        return bool(isinstance(count, int) and count > 0)

    @property
    def verified_count(self) -> int:
        return self._verified

    @property
    def rejected_count(self) -> int:
        return self._rejected

    def to_details(self) -> dict[str, object]:
        """只读诊断事实：模型路径、阈值与计数；不含任何音频或嵌入内容。"""

        return {
            "mode": "voiceprint",
            "model_path": str(self._config.model_path) if self._config.model_path else None,
            "model_present": bool(
                self._config.model_path is not None and self._config.model_path.is_file()
            ),
            "threshold": self._config.threshold,
            "speaker_name": self._config.speaker_name,
            "enrolled": self.enrolled,
            "enrolled_speakers": self.enrolled_speakers,
            "verified_count": self._verified,
            "rejected_count": self._rejected,
            "injected": self._injected,
        }

    # ---- 注册与判定 -------------------------------------------------------

    def enroll(self, embedding: Sequence[float], *, name: str | None = None) -> bool:
        """注册一个说话人声纹（嵌入向量）；形状非法一律 ``ContractError``。"""

        values = _normalize_embedding(embedding)
        speaker = self._config.speaker_name if name is None else name
        if not isinstance(speaker, str) or not speaker:
            raise ContractError("name 必须是非空字符串")
        try:
            return bool(self._manager.add(speaker, values))
        except Exception as exc:  # noqa: BLE001 - 注册失败必须 fail closed
            raise VoiceprintError(COMPUTE_FAILED) from exc

    def enroll_from_pcm(
        self, payload: bytes | bytearray | memoryview, *, sample_rate_hz: int = 16000, name: str | None = None
    ) -> bool:
        """从一段 PCM16LE 单声道音频注册声纹。"""

        embedding = self._embed(payload, sample_rate_hz)
        if embedding is None:
            return False
        return self.enroll(embedding, name=name)

    def embed_from_pcm(
        self, payload: bytes | bytearray | memoryview, *, sample_rate_hz: int = 16000
    ) -> list[float] | None:
        """只算嵌入向量（不注册）；音频不足返回 ``None``，计算失败抛固定码。

        注册工具用它把嵌入向量写进档案；档案里**只有向量、没有音频**。
        """

        return self._embed(payload, sample_rate_hz)

    def score(self, embedding: Sequence[float]) -> float:
        """与已注册说话人的余弦相似度；未注册或计算失败按 fail closed 抛错。"""

        values = _normalize_embedding(embedding)
        try:
            return float(self._manager.score(self._config.speaker_name, values))
        except Exception as exc:  # noqa: BLE001 - 比对失败必须 fail closed
            raise VoiceprintError(COMPUTE_FAILED) from exc

    def verify_from_pcm(
        self, payload: bytes | bytearray | memoryview, *, sample_rate_hz: int = 16000
    ) -> VoiceprintDecision:
        """判定这段音频是否属于已注册说话人。

        - 未注册任何声纹 → ``matched=False, reason="not_enrolled"``（调用方据此**不启用**过滤）；
        - 音频不足以取嵌入 → ``reason="too_short"``；
        - 相似度低于阈值 → ``reason="below_threshold"``；
        - 计算链路异常 → 抛 ``VoiceprintError("compute_failed")``（绝不返回 matched）。
        """

        if not self.enrolled:
            return VoiceprintDecision(False, 0.0, NOT_ENROLLED)
        embedding = self._embed(payload, sample_rate_hz)
        if embedding is None:
            return VoiceprintDecision(False, 0.0, TOO_SHORT)
        score = self.score(embedding)
        if score >= self._config.threshold:
            self._verified += 1
            return VoiceprintDecision(True, score, MATCHED)
        self._rejected += 1
        return VoiceprintDecision(False, score, BELOW_THRESHOLD)

    # ---- 内部实现 ---------------------------------------------------------

    def _embed(
        self, payload: bytes | bytearray | memoryview, sample_rate_hz: int
    ) -> list[float] | None:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ContractError("payload 必须是 bytes/bytearray/memoryview")
        if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, int) or sample_rate_hz <= 0:
            raise ContractError("sample_rate_hz 必须是正整数")
        raw = bytes(payload)
        if len(raw) % 2 != 0:
            raise ContractError("payload 长度必须是偶数字节（pcm_s16le）")
        samples = [value / _S16_FULL_SCALE for value in struct.unpack(f"<{len(raw) // 2}h", raw)]
        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate_hz, samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            embedding = self._extractor.compute(stream)
        except Exception as exc:  # noqa: BLE001 - 取嵌入失败必须 fail closed
            raise VoiceprintError(COMPUTE_FAILED) from exc
        return _normalize_embedding(embedding)


def _normalize_embedding(embedding: object) -> list[float]:
    """把嵌入归一化成非空浮点列表；空/非数值一律 ``ContractError``。"""

    if isinstance(embedding, (str, bytes, bytearray)) or not isinstance(embedding, Sequence):
        raise ContractError("embedding 必须是数值序列")
    values: list[float] = []
    for item in embedding:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ContractError("embedding 必须是数值序列")
        values.append(float(item))
    if not values:
        raise ContractError("embedding 不能为空")
    return values


__all__ = [
    "BELOW_THRESHOLD",
    "COMPUTE_FAILED",
    "DEPENDENCY_MISSING",
    "INIT_FAILED",
    "MATCHED",
    "MODEL_MISSING",
    "NOT_ENROLLED",
    "TOO_SHORT",
    "UNSUPPORTED_FORMAT",
    "UNSUPPORTED_SAMPLE_RATE",
    "VoiceprintConfig",
    "VoiceprintDecision",
    "VoiceprintError",
    "VoiceprintFilter",
]
