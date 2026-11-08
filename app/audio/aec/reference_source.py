"""P10 批次 R2b-2（方案 A）：把**提交给设备的 PCM** 当作 AEC 参考信号。

背景（真机实测结论）：本机 WASAPI 回环参考路线**不可用**——系统音乐能被回环抓到
（watch 实验 rms=0.18），但本程序播放的音频抓不到：WASAPI 直放同一端点 `out[10]` 并录其
回环 `loop[13]`，模板对齐仅 −0.06；多音扫描里 700 Hz 以上 9 个频点能量比全为 0.000。
因此改回设计文档 12 的首选口径：**优先获取实际送往输出设备的 PCM 作为参考信号**，
且在同进程内取得。

本模块只做三件事：
1. 接收"提交给播放设备的 PCM + 单调时间戳"（由 `WaveOutPCMStreamBackend.submit` 的只读
   旁路钩子提供，默认关闭）；
2. 按设备 epoch 变化重校准（旧的延迟估计与历史一律作废）；
3. 把参考窗口交给逐样本 NLMS（R2b-1 的 `NlmsEchoCanceller`）并给出可复核结果。

硬边界：不访问设备/文件/网络，只依赖 stdlib、项目契约与 `app.audio.aec` 自身；
不导入安全链模块。

R4-B-1 增补：`SameProcessAecReference(backend="webrtc")` 把消除器换成 WebRTC APM(AEC3)
后端（可选依赖 `pywebrtc-audio`；未安装时**如实**报 `webrtc_dependency_missing` 并直通）。
**默认仍是 `nlms`**，因此不显式指定后端时行为与之前完全一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.audio.aec.nlms import (
    DEFAULT_ERLE_GATE_DB,
    NlmsConfig,
    NlmsEchoCanceller,
    pcm16_to_float,
)
from app.audio.aec.reference import AecReferenceConfig, AecReferenceTimeline, ReferenceWindow
from app.audio.aec.webrtc_backend import WebrtcAecConfig, WebrtcEchoCanceller
from app.contracts.base import ContractError, require_identifier, require_positive

# 消除器后端（固定短标识）
BACKEND_NLMS = "nlms"
BACKEND_WEBRTC = "webrtc"
AEC_BACKENDS = (BACKEND_NLMS, BACKEND_WEBRTC)

# 旁路钩子不得改变播放行为：异常一律吞掉并计数（原因以固定短代码暴露），绝不冒泡到提交路径。


class AecReferenceSourceError(ContractError):
    """参考源拒绝输入；``str()`` 只返回固定短代码。"""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code else "invalid_reference_frame"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AecReferencePCM:
    """提交给设备的 PCM 的格式契约（本方案要求与采集/消除同一采样率）。

    ``source_sample_rate`` 是**提交侧**（播放后端）的时钟；``sample_rate`` 是**目标时钟**
    （采集/AEC 所用）。两者不同时，``submit()`` 会做**跨块连续重采样**——这是 R4 真机
    第三轮暴露"没有可用回声参考"的直接修复：waveout 硬编码 24 kHz，而采集是 16 kHz，
    参考不重采样就永远无法与采集对齐（`reference_matches_output=False` → 无判别依据）。
    ``None`` 表示与目标同钟、**不做任何重采样**（字节级行为不变）。
    """

    sample_rate: int = 48_000
    frame_duration_ms: int = 20
    max_capacity_ms: int = 8000
    source_sample_rate: int | None = None

    def __post_init__(self) -> None:
        require_positive("sample_rate", self.sample_rate)
        require_positive("frame_duration_ms", self.frame_duration_ms)
        require_positive("max_capacity_ms", self.max_capacity_ms)
        if self.source_sample_rate is not None:
            require_positive("source_sample_rate", self.source_sample_rate)

    @property
    def frame_samples(self) -> int:
        return -(-self.sample_rate * self.frame_duration_ms // 1000)

    @property
    def resampling(self) -> bool:
        """是否需要重采样（提交侧时钟 ≠ 目标时钟）。"""

        return (
            self.source_sample_rate is not None
            and int(self.source_sample_rate) != int(self.sample_rate)
        )

    @property
    def resample_ratio(self) -> float:
        """目标/提交 样本比（>1 升采样，<1 降采样）。"""

        if not self.resampling:
            return 1.0
        return float(self.sample_rate) / float(self.source_sample_rate)  # type: ignore[arg-type]

    def to_config(self) -> AecReferenceConfig:
        return AecReferenceConfig(
            sample_rate=self.sample_rate,
            frame_duration_ms=self.frame_duration_ms,
            max_capacity_ms=max(self.max_capacity_ms, self.frame_duration_ms * 2),
        )


@dataclass(frozen=True, slots=True)
class AecReferenceFrameResult:
    """一帧处理结果；``output`` 为处理后的采集音频，其余只含数值与固定短代码。"""

    output: np.ndarray
    applied: bool
    fallback_reason: str
    reference_state: str
    erle_db: float
    cumulative_erle_db: float
    frames_submitted: int
    device_epoch: int

    def to_details(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "fallback_reason": self.fallback_reason,
            "reference_state": self.reference_state,
            "erle_db": None if math.isinf(self.erle_db) else round(self.erle_db, 3),
            "cumulative_erle_db": (
                None
                if math.isinf(self.cumulative_erle_db)
                else round(self.cumulative_erle_db, 3)
            ),
            "frames_submitted": self.frames_submitted,
            "device_epoch": self.device_epoch,
            "samples": int(self.output.size),
        }


class SameProcessAecReference:
    """同进程参考源：消费"提交给设备的 PCM"，产出对齐的参考窗口与消除结果。

    输入是**任意长度的 PCM16 块**（waveOut 默认一次提交 200 ms 的大块），内部做流式重组：
    按 ``frame_samples`` 切成整帧后进入时间线，余字节留在待组装的缓冲里。
    """

    def __init__(
        self,
        *,
        pcm: AecReferencePCM | None = None,
        nlms_config: NlmsConfig | None = None,
        webrtc_config: WebrtcAecConfig | None = None,
        backend: str = BACKEND_NLMS,
        device_epoch: int = 1,
    ) -> None:
        self._pcm = pcm or AecReferencePCM()
        self._config = self._pcm.to_config()
        if backend not in AEC_BACKENDS:
            raise AecReferenceSourceError("unknown_aec_backend")
        self._backend = backend
        self._nlms_config: NlmsConfig | None = None
        self._webrtc_config: WebrtcAecConfig | None = None
        self._timeline = AecReferenceTimeline(config=self._config)
        if backend == BACKEND_WEBRTC:
            remote = webrtc_config or WebrtcAecConfig(
                sample_rate=self._pcm.sample_rate,
                frame_duration_ms=self._pcm.frame_duration_ms,
            )
            if remote.sample_rate != self._pcm.sample_rate:
                raise AecReferenceSourceError("sample_rate_mismatch")
            if remote.frame_duration_ms != self._pcm.frame_duration_ms:
                raise AecReferenceSourceError("frame_duration_mismatch")
            self._webrtc_config = remote
            self._canceller: NlmsEchoCanceller | WebrtcEchoCanceller = WebrtcEchoCanceller(
                config=remote
            )
        else:
            base = nlms_config or NlmsConfig()
            if base.sample_rate != self._pcm.sample_rate:
                raise AecReferenceSourceError("sample_rate_mismatch")
            if base.frame_duration_ms != self._pcm.frame_duration_ms:
                raise AecReferenceSourceError("frame_duration_mismatch")
            self._nlms_config = base
            self._canceller = NlmsEchoCanceller(config=base)
        self._device_epoch = device_epoch
        self._last_ms: int | None = None
        self._frames_submitted = 0
        self._chunks_submitted = 0
        self._pending = bytearray()
        self._observer_errors = 0
        self._last_observer_error = ""
        self._resets = 0
        # 跨块重采样状态（仅 source_sample_rate 与目标不同钟时使用）
        self._src_tail: np.ndarray | None = None
        self._src_total = 0
        self._out_total = 0

    # ---------------------------------------------------------------- properties

    @property
    def pcm(self) -> AecReferencePCM:
        return self._pcm

    @property
    def device_epoch(self) -> int:
        return self._device_epoch

    @property
    def frames_submitted(self) -> int:
        return self._frames_submitted

    @property
    def observer_errors(self) -> int:
        return self._observer_errors

    @property
    def last_observer_error(self) -> str:
        """最近一次旁路错误的原因（固定短代码，便于定位，不带音频内容）。"""

        return self._last_observer_error

    @property
    def pending_bytes(self) -> int:
        """还没凑够一整帧的余字节数。"""

        return len(self._pending)

    @property
    def chunks_submitted(self) -> int:
        return self._chunks_submitted

    @property
    def resets(self) -> int:
        return self._resets

    @property
    def frames_buffered(self) -> int:
        return self._timeline.frames_buffered

    @property
    def backend(self) -> str:
        """当前消除器后端（固定短标识：``nlms`` / ``webrtc``）。"""

        return self._backend

    @property
    def canceller(self) -> NlmsEchoCanceller | WebrtcEchoCanceller:
        return self._canceller

    def to_details(self) -> dict[str, object]:
        return {
            "sample_rate": self._pcm.sample_rate,
            "frame_duration_ms": self._pcm.frame_duration_ms,
            "frame_samples": self._pcm.frame_samples,
            "frames_submitted": self._frames_submitted,
            "chunks_submitted": self._chunks_submitted,
            "frames_buffered": self._timeline.frames_buffered,
            "pending_bytes": len(self._pending),
            "device_epoch": self._device_epoch,
            "observer_errors": self._observer_errors,
            "last_observer_error": self._last_observer_error,
            "resets": self._resets,
            # R4-B-1：消除器后端（默认 nlms；webrtc 为可选依赖）。
            "aec_backend": self._backend,
            # R4-A：参考是否被重采样到采集时钟（真机"无可用参考"的直接修复）。
            "resampling": self._pcm.resampling,
            "source_sample_rate": self._pcm.source_sample_rate,
            "resample_ratio": round(self._pcm.resample_ratio, 6),
        }

    # ---------------------------------------------------------------- observer hook

    def as_submit_observer(self):
        """返回可直接交给 ``WaveOutPCMStreamBackend(submit_observer=...)`` 的回调。

        回调内**绝不抛出**：异常只累计计数并记录固定短代码，避免上层播放路径受影响。
        """

        def observe(frame: bytes, submitted_at_seconds: float) -> None:
            try:
                self.submit(frame, submitted_at_seconds=submitted_at_seconds)
            except Exception as exc:  # noqa: BLE001 - 旁路不得影响播放
                self._observer_errors += 1
                self._last_observer_error = (
                    exc.code if isinstance(exc, AecReferenceSourceError) else type(exc).__name__
                )

        return observe

    # ---------------------------------------------------------------- write side

    def submit(self, chunk: bytes, *, submitted_at_seconds: float) -> int:
        """登记一块"实际提交给设备"的 PCM（任意长度），返回参考缓冲帧数。

        内部先把块追加到待组装缓冲，再按整帧切分进入时间线；同一提交时刻切出的多帧
        时间戳按帧时长递增，保证时间线严格单调。
        """

        if not isinstance(chunk, bytes) or not chunk:
            raise AecReferenceSourceError("invalid_reference_frame")
        if len(chunk) % 2:
            raise AecReferenceSourceError("invalid_reference_frame")
        if isinstance(submitted_at_seconds, bool) or not isinstance(submitted_at_seconds, (int, float)):
            raise AecReferenceSourceError("invalid_submitted_at")
        value = float(submitted_at_seconds)
        if not math.isfinite(value) or value < 0.0:
            raise AecReferenceSourceError("invalid_submitted_at")

        self._chunks_submitted += 1
        chunk = self._resample_to_target_clock(chunk)
        self._pending.extend(chunk)
        frame_bytes = self._pcm.frame_samples * 2
        frame_ms = self._pcm.frame_duration_ms
        first = True
        while len(self._pending) >= frame_bytes:
            frame = bytes(self._pending[:frame_bytes])
            del self._pending[:frame_bytes]
            at_ms = int(value * 1000.0)
            if first:
                # 同一毫秒内的多次提交钳到 +1 ms，避免零宽度帧。
                if self._last_ms is not None and at_ms <= self._last_ms:
                    at_ms = self._last_ms + 1
                first = False
            else:
                at_ms = self._last_ms + frame_ms
            self._timeline.push(at_ms, frame, samples=self._pcm.frame_samples)
            self._last_ms = at_ms
            self._frames_submitted += 1
        return self._timeline.frames_buffered

    def _resample_to_target_clock(self, chunk: bytes) -> bytes:
        """把提交侧时钟的 PCM 连续重采样到目标（采集）时钟；不需要时**原样返回**。

        连续性：保留上一块的最后一个样本作为左锚点，并用绝对样本计数决定输出样本位置，
        因此任意长度块（waveout 实际一次提交 200 ms）拼起来与逐帧提交的结果一致，
        不会在块边界产生相位跳变或丢样。
        """

        if not self._pcm.resampling:
            return chunk
        ratio = self._pcm.resample_ratio
        if ratio <= 0.0 or not math.isfinite(ratio):
            raise AecReferenceSourceError("invalid_reference_frame")
        src = pcm16_to_float(chunk)
        if src.size == 0:
            return b""
        if self._src_tail is None:
            base = src
            base_start = 0
        else:
            base = np.concatenate((self._src_tail, src))
            base_start = self._src_total - 1
        max_position = self._src_total + src.size - 1
        last_out = int(math.floor(max_position * ratio))
        first_out = self._out_total
        self._src_total += src.size
        self._src_tail = src[-1:]
        if last_out < first_out:
            return b""
        targets = np.arange(first_out, last_out + 1, dtype=np.float64)
        positions = targets / ratio - base_start
        resampled = np.interp(
            positions, np.arange(base.size, dtype=np.float64), base
        )
        self._out_total = last_out + 1
        return _float_to_pcm16(resampled)

    def note_device_epoch(self, *, device_epoch: int, estimated_delay_ms: int = 0) -> bool:
        """设备/epoch 变化：清空历史与自适应状态并重校准；返回是否真的换了 epoch。"""

        require_positive("device_epoch", device_epoch)
        if device_epoch == self._device_epoch:
            return False
        self._device_epoch = device_epoch
        self._timeline.clear()
        self._timeline.note_device_change(
            device_epoch=device_epoch, estimated_delay_ms=estimated_delay_ms
        )
        self._canceller.reset()
        self._last_ms = None
        self._src_tail = None
        self._src_total = 0
        self._out_total = 0
        self._resets += 1
        return True

    # ---------------------------------------------------------------- read side

    def window(self, capture_at_ms: int) -> ReferenceWindow:
        """取出与采集时刻对齐的参考窗口（延迟由时间线按设备延迟换算）。"""

        return self._timeline.window(capture_at_ms, frame_samples=self._pcm.frame_samples)

    def timeline_frames(self, *, limit: int = 0) -> tuple[tuple[int, np.ndarray], ...]:
        """返回缓冲中的参考帧 ``(提交时刻ms, float32样本)``，供离线实测工具按时间轴拼接。

        ``limit > 0`` 时只返回最近的 ``limit`` 帧；默认返回全部（由时间线容量决定上界）。
        """

        frames = tuple(self._timeline.frames())
        if limit and len(frames) > limit:
            return frames[-limit:]
        return frames

    def process(
        self, *, capture_at_ms: int, capture: bytes, double_talk: bool = False
    ) -> AecReferenceFrameResult:
        """处理一帧采集音频；参考不可用时如实回退直通并写明原因。

        ``double_talk``（R4-B5/B3）：上游双讲检测认定"近端也在说话"时传 ``True`` ⇒
        **不应用消除输出**，且 NLMS 权重按 `NlmsConfig.double_talk_step_scale` 缩放
        （默认冻结）。AEC3 后端不支持外部双讲提示，本参数对它只是一个"输出直通 + 如实登记"
        的开关（其内部自适应不可控——这一点在 `webrtc_backend` 的文档里已写明）。

        约束（已实测记录于 `2026-10-02-P10-R2b-2-real-device-handoff.md`）：真实回声路径的
        延迟必须**至少 1 个样本**。逐样本 NLMS 的权重索引随帧边界每次位移 1，若回声与参考完全
        零延迟对齐，滤波器会追不上移动的抽头（实测 ERLE ≈ −1 dB、权重发散）；存在
        1 个样本以上延迟时收敛良好（实测尾部 ERLE ≈ 41 dB，权重精确落到真实增益）。
        真实设备链路必然有至少一个样本的延迟，因此该约束不影响实际使用。
        **该约束只针对 NLMS**：`backend="webrtc"`（AEC3）自带延迟估计，不要求 ≥1 样本。

        ERLE 口径随后端不同（**读数时不要混用**）：`nlms` 为"参考能量/残差能量"，
        `webrtc` 为"采集能量/残差能量"（真实声学增益未知，只能用采集能量）。
        """

        samples = self._pcm.frame_samples
        mic = pcm16_to_float(capture, expected_samples=samples)
        window = self.window(capture_at_ms)
        if not window.ok or window.pcm is None:
            return AecReferenceFrameResult(
                output=mic.astype(np.float32, copy=True),
                applied=False,
                fallback_reason=f"reference_{window.state}",
                reference_state=window.state,
                erle_db=0.0,
                cumulative_erle_db=self._canceller.cumulative_erle_db,
                frames_submitted=self._frames_submitted,
                device_epoch=self._device_epoch,
            )
        reference_pcm = _float_to_pcm16(window.pcm)
        result = self._canceller.cancel(
            reference=reference_pcm, capture=capture, double_talk=double_talk
        )
        return AecReferenceFrameResult(
            output=result.output,
            applied=result.applied,
            fallback_reason=result.fallback_reason or "",
            reference_state=window.state,
            erle_db=result.erle_db,
            cumulative_erle_db=result.cumulative_erle_db,
            frames_submitted=self._frames_submitted,
            device_epoch=self._device_epoch,
        )

    def reset(self) -> None:
        self._timeline.clear()
        self._canceller.reset()
        self._last_ms = None
        self._src_tail = None
        self._src_total = 0
        self._out_total = 0


def _float_to_pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).round().astype("<i2").tobytes()


__all__ = [
    "AEC_BACKENDS",
    "BACKEND_NLMS",
    "BACKEND_WEBRTC",
    "AecReferenceFrameResult",
    "AecReferencePCM",
    "AecReferenceSourceError",
    "SameProcessAecReference",
]