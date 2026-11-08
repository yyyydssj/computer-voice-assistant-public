"""P10 批次 R4-B-1：WebRTC APM（AEC3）回声消除后端（纯离线、可注入、**可选依赖**）。

背景：方案 A（把参考同钟后用对齐相关性折减）在本机拿不到判别依据——真机
`echo_proven` 恒为 false。按 `implementation_plan/research/2026-10-03-P10-R4B-aec-frontend-feasibility.md`
的结论，接入**真正的回声消除前端**：WebRTC APM 的 AEC3（`pywebrtc-audio` 提供
cp312-win_amd64 wheel，免编译）。

本模块只做三件事：
1. 用 `pywebrtc_audio.EchoCanceller`（AEC3）消除**同一时钟**的参考/采集帧里的回声；
2. 给出**可复核**的抑制量（ERLE）与是否真正应用；
3. 依赖缺失或底层调用异常一律**如实回退直通**并写固定原因码（绝不伪装已消除回声、
   绝不把异常冒泡进音频/播放路径）。

硬边界：
- 不访问设备/文件/网络、不创建线程、不导入安全链模块；
- **可选依赖**：未安装 `pywebrtc_audio` 时本模块仍可导入，能力如实报
  `webrtc_dependency_missing`；
- 参考与采集必须**同采样率、同长度**（由调用方保证，例如 `SameProcessAecReference`
  的 16 kHz 时间线 + 24 kHz 提交侧重采样）；
- **每一帧都必须调用底层 `process()`**（含双讲帧与静音帧），否则 AEC3 的自适应状态会中断；
- 本批次**只跑 AEC，不跑 NS/AGC**：NS/AGC 会改变帧能量口径，直接影响 ERLE 判定与
  打断阈值，留待有实测证据后再单独评估。

ERLE 口径（与 `nlms.py` 的差别必须记住）：
- `nlms.NlmsEchoCanceller` 用 **参考能量 / 残差能量**（只在"回声 ≈ 参考"时成立）；
- 本模块用 **采集能量 / 残差能量**（`mic / residual`），因为真实声学增益未知，
  不能用"参考能量"替回声能量。

**本机离线实测的诚实结论（必须遵守，别把抑制量当成"已消除回声"）**
（2026-10-03，`tools/p10_r4b_aec_bench.py` + 诊断脚本，200 帧 20 ms、far 白噪 rms≈3000）：

| 场景 | 累计 ERLE | 输出能量变化 |
|---|---|---|
| far 静音 + 近端噪声 | **0.07 dB**（0/200 帧 applied） | 5.4 → 5.4（不动） |
| far 有能量 + 近端**完全无关**噪声（**没有回声路径**） | **11.7 dB**（200/200 applied） | 5.4 → 0.4 |
| far 有能量 + 真回声（0.5 增益 / 40 ms 延迟 / 近端噪声） | **25.6 dB** | 132.8 → 0.4 |

⇒ **只要参考有能量，AEC3 就会把采集压到"残余抑制地板"（与 far 电平挂钩），哪怕根本没有回声路径。**

因此：
- `applied` 只是**处理事实**（这一帧输出来自 AEC3），**不是**"回声已消除"的证明；
- `suppression_evidence` 才说明证据强度，且本后端的取值**永远不含"已确认回声路径"**
  —— 确认需要外部证据（例如参考与采集的对齐相关性、真机 ERLE 实测），属 R4-B-2 范围；
- 参考帧本身无能量时**不得**应用 AEC3 输出（没有可消除的回声，直通保护近端语音）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.audio.aec.nlms import (
    DEFAULT_ERLE_GATE_DB,
    EchoCancellationResult,
)
from app.contracts.base import ContractError, require_positive

# WebRTC APM 内部帧长固定 10 ms；我们的帧长必须是它的整数倍。
APM_FRAME_QUANTUM_MS = 10

# 固定原因码（与 `nlms.py` 中同义条件的字面值保持一致，便于上层消费）
REASON_DOUBLE_TALK = "double_talk_detected"
REASON_ERLE_BELOW_GATE = "erle_below_gate"
REASON_REFERENCE_SILENT = "reference_silent"
REASON_DEPENDENCY_MISSING = "webrtc_dependency_missing"
REASON_BACKEND_ERROR = "webrtc_backend_error"

# 抑制证据强度（固定短码）。**本后端永远不会给出"已确认回声路径"**：
# 实测证明"参考有能量时即使没有回声路径也会产生 ≥6 dB 抑制"（见模块 docstring 表格）。
EVIDENCE_NO_PROCESSING = "no_processing"
EVIDENCE_REFERENCE_INSUFFICIENT = "reference_energy_insufficient"
EVIDENCE_SUPPRESSION_ONLY = "suppression_only_without_echo_path_proof"
EVIDENCE_BELOW_GATE = "suppression_below_gate"

EVIDENCE_STATES = (
    EVIDENCE_NO_PROCESSING,
    EVIDENCE_REFERENCE_INSUFFICIENT,
    EVIDENCE_SUPPRESSION_ONLY,
    EVIDENCE_BELOW_GATE,
)

OUTPUT_ENCODING_SCALE = 32768.0

# 哨兵：区分"没传 module"（走真实惰性导入）与"显式传 None"（强制按依赖不可用处理，供测试）。
_UNSET: object = object()


def load_webrtc_module() -> object | None:
    """惰性加载可选依赖 `pywebrtc_audio`；未安装返回 ``None``（**不抛异常**）。"""

    try:  # noqa: PLC0415 - 可选依赖必须惰性导入，缺失时模块仍可用
        import pywebrtc_audio  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - 任何导入失败都按"依赖不可用"如实处理
        return None
    return pywebrtc_audio


def webrtc_backend_available() -> bool:
    """当前环境是否具备真 AEC 后端（只做导入探测，不创建实例）。"""

    return load_webrtc_module() is not None


@dataclass(frozen=True, slots=True)
class WebrtcAecConfig:
    """AEC3 后端参数；``frame_duration_ms`` 必须是 10 ms 的整数倍。"""

    sample_rate: int = 16_000
    frame_duration_ms: int = 20
    stream_delay_ms: int = 0
    erle_gate_db: float = DEFAULT_ERLE_GATE_DB

    def __post_init__(self) -> None:
        require_positive("sample_rate", self.sample_rate)
        require_positive("frame_duration_ms", self.frame_duration_ms)
        if self.frame_duration_ms % APM_FRAME_QUANTUM_MS:
            raise ContractError(
                f"frame_duration_ms 必须是 {APM_FRAME_QUANTUM_MS} ms 的整数倍"
            )
        if isinstance(self.stream_delay_ms, bool) or not isinstance(self.stream_delay_ms, int):
            raise ContractError("stream_delay_ms 必须是整数毫秒")
        if self.stream_delay_ms < 0:
            raise ContractError("stream_delay_ms 必须非负")
        gate = self.erle_gate_db
        if isinstance(gate, bool) or not isinstance(gate, (int, float)):
            raise ContractError("erle_gate_db 必须是数值")
        object.__setattr__(self, "erle_gate_db", float(gate))

    @property
    def frame_samples(self) -> int:
        return -(-self.sample_rate * self.frame_duration_ms // 1000)


def _pcm16_samples(payload: object, *, expected: int) -> np.ndarray:
    """PCM16 LE → ``int16`` 数组；长度非法一律 ``ContractError``（与 nlms 同口径）。"""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ContractError("pcm 必须是 bytes/bytearray/memoryview")
    raw = bytes(payload)
    if len(raw) % 2:
        raise ContractError("invalid_pcm_length")
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size != expected:
        raise ContractError("invalid_pcm_length")
    return samples


def _energy(samples: np.ndarray) -> float:
    array = np.asarray(samples, dtype=np.float32)
    return float(np.dot(array, array))


def _mic_erle_db(mic_energy: float, residual_energy: float) -> float:
    """回声抑制量（dB），口径为 **采集能量 / 残差能量**。

    没有采集能量就没有"回声被抑制"这回事：静音帧返回 0 dB，而不是伪造 ∞；
    只有采集非零且残差精确为零（理想数值情形）时才返回 ∞。
    """

    if mic_energy <= 0.0:
        return 0.0
    if residual_energy <= 0.0:
        return float("inf")
    return float(10.0 * np.log10(mic_energy / residual_energy))


class WebrtcEchoCanceller:
    """AEC3 回声消除器（可选依赖、纯离线、确定性受底层实现约束）。

    与 `NlmsEchoCanceller` **接口同构**（`cancel`/`reset`/`cumulative_erle_db`/
    `frames_processed`），因此调用方可以只换后端、不改调用点。
    """

    def __init__(
        self,
        *,
        config: WebrtcAecConfig | None = None,
        module: object | None = _UNSET,
        module_loader: object | None = None,
        stream_delay_ms: int | None = None,
    ) -> None:
        self._config = config or WebrtcAecConfig()
        if stream_delay_ms is not None:
            self._config = WebrtcAecConfig(
                sample_rate=self._config.sample_rate,
                frame_duration_ms=self._config.frame_duration_ms,
                stream_delay_ms=stream_delay_ms,
                erle_gate_db=self._config.erle_gate_db,
            )
        if module is not _UNSET:
            # 显式传入（含 None）→ 以传入值为准（None 表示"依赖不可用"）。
            self._module: object | None = module
        elif module_loader is not None:
            self._module = module_loader()  # type: ignore[operator]
        else:
            self._module = load_webrtc_module()
        self._mic_energy_total = 0.0
        self._residual_energy_total = 0.0
        self._reference_energy_total = 0.0
        self._frames = 0
        self._frames_seen = 0
        self._backend_errors = 0
        self._last_backend_error = ""
        self._init_error = ""
        self._canceller: object | None = None
        if self._module is not None:
            try:
                self._canceller = self._module.EchoCanceller(  # type: ignore[attr-defined]
                    sample_rate=self._config.sample_rate,
                    num_channels=1,
                    stream_delay_ms=self._config.stream_delay_ms,
                )
            except Exception as exc:  # noqa: BLE001 - 底层构造失败按能力不可用如实处理
                self._canceller = None
                self._init_error = type(exc).__name__

    # ---------------------------------------------------------------- properties

    @property
    def config(self) -> WebrtcAecConfig:
        return self._config

    @property
    def frame_samples(self) -> int:
        return self._config.frame_samples

    @property
    def available(self) -> bool:
        """底层 AEC3 是否真的可用（依赖存在且实例构造成功）。"""

        return self._canceller is not None

    @property
    def available_reason(self) -> str | None:
        """不可用的固定原因码；可用时为 ``None``。"""

        if self.available:
            return None
        if self._module is None:
            return REASON_DEPENDENCY_MISSING
        return REASON_BACKEND_ERROR

    @property
    def frames_processed(self) -> int:
        """真正跑过底层 AEC3 的帧数（依赖不可用时恒为 0）。"""

        return self._frames

    @property
    def stream_delay_ms(self) -> int:
        """当前延迟提示；底层可用时以底层实际值为准（配置只是我们的期望值）。"""

        if self._canceller is not None:
            try:
                return int(self._canceller.stream_delay_ms)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 读不到就退回配置值，不影响音频路径
                return self._config.stream_delay_ms
        return self._config.stream_delay_ms

    @property
    def frames_seen(self) -> int:
        """`cancel()` 被调用的总帧数（含不可用与异常回退的帧）。"""

        return self._frames_seen

    @property
    def backend_errors(self) -> int:
        return self._backend_errors

    @property
    def last_backend_error(self) -> str:
        """最近一次底层失败的**类型名或初始化错误**（固定短字符串，不含音频内容）。"""

        return self._last_backend_error or self._init_error

    @property
    def cumulative_erle_db(self) -> float:
        """累计 ERLE（采集能量总和 / 残差能量总和）。"""

        return _mic_erle_db(self._mic_energy_total, self._residual_energy_total)

    @property
    def erle_sufficient(self) -> bool:
        """累计抑制量是否已达门槛。

        **注意**：这**不等于**"回声已被消除"——参考有能量时，即使没有回声路径，
        AEC3 也会产生 ≥ 门槛的抑制量（见模块 docstring 实测表）。能否声称"已消除回声"
        必须结合 `suppression_evidence` 与外部证据（对齐相关性 / 真机 ERLE）。
        """

        return self.cumulative_erle_db >= self._config.erle_gate_db

    @property
    def suppression_evidence(self) -> str:
        """抑制证据强度（固定短码），**永不返回"已确认回声路径"**。

        - ``no_processing``：还没跑过底层 AEC3；
        - ``reference_energy_insufficient``：参考（播放）侧没有能量 → 不构成任何证据；
        - ``suppression_only_without_echo_path_proof``：有参考能量且抑制量达标，
          但这只证明"有抑制"，**不证明存在回声路径**；
        - ``suppression_below_gate``：抑制量未达门槛。
        """

        if self._frames == 0:
            return EVIDENCE_NO_PROCESSING
        if self._reference_energy_total <= 0.0:
            return EVIDENCE_REFERENCE_INSUFFICIENT
        if self.cumulative_erle_db >= self._config.erle_gate_db:
            return EVIDENCE_SUPPRESSION_ONLY
        return EVIDENCE_BELOW_GATE

    def to_details(self) -> dict[str, object]:
        cumulative = self.cumulative_erle_db
        return {
            "backend": "webrtc_aec3",
            "available": self.available,
            "available_reason": self.available_reason,
            "init_error": self._init_error or None,
            "sample_rate": self._config.sample_rate,
            "frame_duration_ms": self._config.frame_duration_ms,
            "frame_samples": self._config.frame_samples,
            "stream_delay_ms": self._config.stream_delay_ms,
            "erle_gate_db": self._config.erle_gate_db,
            "frames_processed": self._frames,
            "frames_seen": self._frames_seen,
            "backend_errors": self._backend_errors,
            "last_backend_error": self.last_backend_error or None,
            "cumulative_erle_db": None if cumulative == float("inf") else round(cumulative, 3),
            "erle_sufficient": self.erle_sufficient,
            "suppression_evidence": self.suppression_evidence,
        }

    def set_stream_delay_ms(self, stream_delay_ms: int) -> None:
        """运行时更新延迟提示（AEC3 支持；不可用或无实例时如实不生效）。"""

        if isinstance(stream_delay_ms, bool) or not isinstance(stream_delay_ms, int):
            raise ContractError("stream_delay_ms 必须是整数毫秒")
        if stream_delay_ms < 0:
            raise ContractError("stream_delay_ms 必须非负")
        self._config = WebrtcAecConfig(
            sample_rate=self._config.sample_rate,
            frame_duration_ms=self._config.frame_duration_ms,
            stream_delay_ms=stream_delay_ms,
            erle_gate_db=self._config.erle_gate_db,
        )
        if self._canceller is not None:
            try:
                self._canceller.stream_delay_ms = stream_delay_ms  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - 设置失败如实计数，不影响音频路径
                self._backend_errors += 1
                self._last_backend_error = type(exc).__name__

    def reset(self) -> None:
        """清空自适应状态与统计（设备变化/新会话时必须重校准）。"""

        if self._canceller is not None:
            try:
                self._canceller.reset()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - 重置失败如实计数
                self._backend_errors += 1
                self._last_backend_error = type(exc).__name__
        self._mic_energy_total = 0.0
        self._residual_energy_total = 0.0
        self._reference_energy_total = 0.0
        self._frames = 0
        self._frames_seen = 0

    # ---------------------------------------------------------------- processing

    def cancel(
        self,
        *,
        reference: object,
        capture: object,
        double_talk: bool = False,
    ) -> EchoCancellationResult:
        """用 ``reference`` 消除 ``capture`` 中的回声。

        - 依赖不可用 → 直通 + ``fallback_reason=webrtc_dependency_missing``；
        - 底层异常 → 直通 + ``fallback_reason=webrtc_backend_error``（异常绝不冒泡）；
        - ``double_talk=True`` → 输出直通（**仍会调用底层**以保持自适应），原因如实记录；
        - 参考帧无能量 → 直通 + ``fallback_reason=reference_silent``（没有可消除的回声）；
        - 逐帧 ERLE 低于门槛 → 直通 + ``fallback_reason=erle_below_gate``。

        **`applied=True` 只说明"本帧输出来自 AEC3"，不说明"回声已消除"**；
        证据强度看 `suppression_evidence`。
        """

        expected = self._config.frame_samples
        mic_i16 = _pcm16_samples(capture, expected=expected)
        ref_i16 = _pcm16_samples(reference, expected=expected)
        if not isinstance(double_talk, bool):
            raise ContractError("double_talk 必须是布尔值")

        self._frames_seen += 1
        mic_float = mic_i16.astype(np.float32) / OUTPUT_ENCODING_SCALE

        if self._canceller is None:
            return EchoCancellationResult(
                output=mic_float.astype(np.float32, copy=True),
                applied=False,
                fallback_reason=self.available_reason,
                erle_db=0.0,
                cumulative_erle_db=self.cumulative_erle_db,
                double_talk=double_talk,
            )

        try:
            cleaned = self._canceller.process(mic_i16, ref_i16)  # type: ignore[attr-defined]
            residual_i16 = np.asarray(cleaned, dtype="<i2")
            if residual_i16.size != expected:
                raise ValueError("unexpected_output_length")
        except Exception as exc:  # noqa: BLE001 - 底层失败绝不影响采集/播放路径
            self._backend_errors += 1
            self._last_backend_error = type(exc).__name__
            return EchoCancellationResult(
                output=mic_float.astype(np.float32, copy=True),
                applied=False,
                fallback_reason=REASON_BACKEND_ERROR,
                erle_db=0.0,
                cumulative_erle_db=self.cumulative_erle_db,
                double_talk=double_talk,
            )

        residual = residual_i16.astype(np.float32) / OUTPUT_ENCODING_SCALE
        mic_energy = _energy(mic_float)
        residual_energy = _energy(residual)
        reference_energy = _energy(ref_i16.astype(np.float32) / OUTPUT_ENCODING_SCALE)
        self._mic_energy_total += mic_energy
        self._residual_energy_total += residual_energy
        self._reference_energy_total += reference_energy
        self._frames += 1

        frame_erle = _mic_erle_db(mic_energy, residual_energy)
        gate_ok = frame_erle >= self._config.erle_gate_db
        # 参考无能量 → 没有可消除的回声：不得用 AEC3 的残余抑制去动近端语音。
        reference_active = reference_energy > 0.0
        should_apply = (not double_talk) and reference_active and gate_ok
        reason: str | None = None
        if double_talk:
            reason = REASON_DOUBLE_TALK
        elif not reference_active:
            reason = REASON_REFERENCE_SILENT
        elif not gate_ok:
            reason = REASON_ERLE_BELOW_GATE
        output = residual if should_apply else mic_float.astype(np.float32, copy=True)

        return EchoCancellationResult(
            output=output.astype(np.float32, copy=True),
            applied=should_apply,
            fallback_reason=reason,
            erle_db=frame_erle,
            cumulative_erle_db=self.cumulative_erle_db,
            double_talk=double_talk,
        )


__all__ = [
    "APM_FRAME_QUANTUM_MS",
    "EVIDENCE_BELOW_GATE",
    "EVIDENCE_NO_PROCESSING",
    "EVIDENCE_REFERENCE_INSUFFICIENT",
    "EVIDENCE_STATES",
    "EVIDENCE_SUPPRESSION_ONLY",
    "REASON_BACKEND_ERROR",
    "REASON_DEPENDENCY_MISSING",
    "REASON_DOUBLE_TALK",
    "REASON_ERLE_BELOW_GATE",
    "REASON_REFERENCE_SILENT",
    "WebrtcAecConfig",
    "WebrtcEchoCanceller",
    "load_webrtc_module",
    "webrtc_backend_available",
]
