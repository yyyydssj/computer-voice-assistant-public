"""P9 批次 2 集成：无需按键的自动收音驱动。

本模块把既有 P6 ``PushToTalkCoordinator`` 适配成“被动拾音、自动判定输入结束”的
``AutoCaptureVoiceDriver``：语音起始复用 ``app.audio.vad.auto_capture`` 的
``SpeechStartDetector``（能量门槛 + 连续命中计数），静音结束、无语音超时与
最长轮次时长由本驱动按帧时长累计判定。它只决定**输入边界**：不识别、不提交、
不播报、不建立授权或执行事实，也没有文件/进程/网络/设备入口。

与 ``PushToTalkVoiceDriver`` 的分工：
- 按键驱动只做身份映射与结果归一化，自身不观察帧；
- 本驱动必须自行观察每一帧，因此它在 ``start()`` 里把自己构造的
  ``vad_factory`` 注入被驱动的 coordinator：``VadSession(bus=bus,
  config=VadConfig(silence_duration_ms=<超长>), detector=<恒真判定>)``。该 VAD 只承担帧
  连续性转发，静音计数恒为 0，**不会**自行结束轮次；自动结束只由本驱动判定
  （``detector`` 取恒真而非 ``None`` 的原因见 ``_ObservingVadSession`` 文档：
  既有 ``VadSession`` 把 ``None`` 判为 ``DETECTOR_UNAVAILABLE`` 并 FAILED 关闭总线）。
  注入方式：构造时若给了 ``coordinator_factory``，由它带着该 factory 造出
  coordinator（真机 runner 推荐路径）；若直接给了现成的 ``coordinator``，则在
  ``start()`` 时替换它的 ``_vad_factory`` 属性，并在轮次结束（release/cancel）时恢复原值。

帧可见性（关键实现约束）：``PushToTalkCoordinator.pump()`` 只返回 ``processed_frames``
计数而不是帧本身，所以能量判定不能放在 ``pump()`` 之后。这里在 ``vad_factory`` 里
包一层 ``_ObservingVadSession``：它在 ``VadSession.process(packet)`` **之前**对每个
packet 调用本驱动的起始判定与静音计数，从而在不修改任何既有文件的前提下，
既能拿到每一帧，又完整复用既有 VAD 的连续性校验与转发语义。每个进入
``VadSession.process`` 的 packet 恰好被观察一次（包括被 VAD 判为失败的那一帧）。

判定分工（冻结契约 ④）：
- **起始确认**由 ``SpeechStartDetector`` 的连续命中阈值给出（默认由驱动自建，也可注入）；
- **每帧是否语音**默认由能量判定给出（``AutoCaptureDriverConfig.speech_threshold`` 或注入的
  ``energy_detector``），静音计时 = 连续未命中帧数 × 当前帧真实 ``duration_ms``；
  配置了 ``AutoCaptureDriverConfig.speech_detector``（silero 模式）时改由该人声判定给出，
  **优先于能量阈值**且能量判定完全不参与；
- 起始后连续未命中 ≥ ``silence_ms`` → 自动结束；未起始 ≥ ``speech_start_timeout_ms`` →
  ``no_speech``（不自动 release）；总时长 ≥ ``max_utterance_ms`` → 自动结束。
  自动结束表示“输入边界正常结束”，仍由 ``release()`` 走 coordinator 正常收尾并提交；
  只有检测器故障 / provider 故障 / no_speech 这类**输入不可用**判定才不提交。

轮次收尾分类（真机免按键连续对话的关键）：
- **本地判定“本轮没有可用输入”**（``no_speech``、调用方在收音中直接取消、以及正常结束
  后未提交的放弃路径）→ 调 ``coordinator.abandon_turn(reason=...)``：只回收资源，**不**产生
  ASR 故障事实，恢复状态保持/回到 ``READY``，下一轮可以立即 ``start()``。
  若 coordinator 没有 ``abandon_turn``（旧替身）则回退到 ``runtime_failed(CANCELLED)``。
  调用后必须确认恢复状态已回到 ``READY``，否则 fail closed：``last_stop_reason`` 记为
  ``coordinator_not_ready`` 并让后续 ``start()`` 直接失败（不伪造恢复）。
- **真实 ASR/provider 故障**（``detector_failure``、``provider_failed``）→ 保持
  ``runtime_failed(code=CANCELLED)``，通道按既有 fail-closed 语义锁死，等待外部探测恢复。

``READY`` 校验优先读 ``coordinator.recovery.state``（既有 coordinator 的公开属性）；不暴露
``recovery`` 的替身则以 ``abandon_turn`` 的返回值是否严格为 ``True`` 为准。
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass
from typing import Callable

from app.asr.recovery import AsrFailureCode, AsrRecoveryState, AsrSessionBoundary
from app.asr.result_gate import ResultGateDecision
from app.audio.bus import AudioBus, AudioPacket
from app.audio.vad import VadConfig, VadSession
from app.audio.vad.auto_capture import SpeechStartConfig, SpeechStartDecision, SpeechStartDetector
from app.contracts.base import ContractError, require_identifier
from app.voice_loop.boundary import VoiceLoopError, VoiceTurnBoundary
from app.voice_loop.chat_loop import VoiceDriverTurnResult
from app.voice_loop.drivers import reply_chars, route_code, speech_eligible, speech_skip_reason

# coordinator 注入的 VAD 只做帧连续性转发：静音阈值取一个正常轮次不可能达到的值，
# 并用恒真判定让静音计数恒为 0（既有 VadSession 把 detector=None 直接判为
# DETECTOR_UNAVAILABLE 并 FAILED 关闭总线，因此不能用 None）。
_FORWARDING_VAD_SILENCE_MS = 600000

_COORDINATOR_METHODS = ("start", "pump", "release", "runtime_failed")

STOP_SILENCE_TIMEOUT = "silence_timeout"
STOP_MAX_UTTERANCE_DURATION = "max_utterance_duration"
STOP_NO_SPEECH = "no_speech"
STOP_DETECTOR_FAILURE = "detector_failure"
STOP_PROVIDER_FAILED = "provider_failed"
STOP_COORDINATOR_NOT_READY = "coordinator_not_ready"

# 结束来源（只读诊断）：说明本轮输入边界是怎么被关掉的，便于真机复核"最后一个字被谁切掉"。
END_SOURCE_SEGMENT = "segment"
END_SOURCE_SILENCE_FRAMES = "silence_frames"
END_SOURCE_MAX_UTTERANCE = "max_utterance"
END_SOURCE_SPEECH_START_TIMEOUT = "speech_start_timeout"
END_SOURCE_DETECTOR_FAILURE = "detector_failure"
END_SOURCE_PROVIDER_FAILED = "provider_failed"

# 真实 ASR/provider 故障：收尾必须走 runtime_failed，把通道按 fail-closed 锁死。
_ASR_FAILURE_STOP_REASONS = frozenset({STOP_DETECTOR_FAILURE, STOP_PROVIDER_FAILED})

# P10 · T1 #13：输入静默窗的固定原因码（只读事实；绝不含文本）。
MUTE_REASON_PLAYBACK = "playback_mute"
MUTE_REASON_POST_SPEECH = "post_speech_mute"


@dataclass(frozen=True, slots=True)
class AutoCaptureDriverConfig:
    """自动收音驱动的本地边界参数；数值只描述输入边界，不代表 ASR 可靠性。

    ``auto_release=False`` 时本驱动永不自动判定输入结束（纯手动/按键兼容路径），
    只累积帧计数，由调用方在按键释放时显式 ``release()``。

    ``speech_detector``（可选，末尾新增字段）是逐帧人声判定（例如 Silero VAD 的
    ``SileroVadDetector.observe``）：不为 ``None`` 时**优先于能量阈值**，每帧命中与起始
    计数都由它决定，``speech_threshold`` / ``energy_detector`` 在该模式下完全不参与；
    为 ``None`` 时保持既有能量判定语义逐字不变。
    """

    auto_release: bool = True
    silence_ms: int = 1200
    speech_start_timeout_ms: int = 8000
    max_utterance_ms: int = 30000
    #: 已确认起始后，至少观察这么久才允许"说话结束"类判定（段级端点 / 帧级静音）收尾。
    #: 真机缺陷（2026-10-01）：上一轮结束时底层 VAD 的"已结束段"仍留在队列里，若不在每轮开始
    #: 重置、又没有这个下限，下一轮第 1 帧就会被判"说完了"（实测 frames=2、识别 `'。'`，却仍提交播报）。
    min_utterance_ms: int = 300
    speech_threshold: float = 600.0
    speech_detector: Callable[[AudioPacket], bool] | None = None
    # 仅在 ``speech_detector`` 非 None 时生效：起始判定的连续命中帧阈值来源。
    # Silero 路径按 512 样本整窗判定并把结论粘滞到帧级，窗口级阈值已在检测器内部
    # 由 ``speech_frames_required`` 承担，因此这里用 1（一次粘滞命中即确认起始）。
    speech_detector_frames_required: int | None = None
    # --- 待修 B：段级端点（两个可选钩子；都是 None 时行为与既有帧级判定逐字一致） ---
    # ``segment_probe``：零参可调用，返回"底层 VAD 是否已产出**已结束的人声段**"
    # （sherpa-onnx ``VoiceActivityDetector.empty()`` 取反）。返回 True 且起始已确认时，
    # 它作为第一顺位结束依据（早于帧级静音；仍走正常 ``release()`` 收尾提交，不丢弃）。
    # ``segment_flush``：``release()`` 前调用一次，把缓冲尾段定稿（``flush()``），
    # 保证最后一个字进入 provider 要解码的那一段。
    segment_probe: Callable[[], bool] | None = None
    segment_flush: Callable[[], None] | None = None
    # --- P9 B4：只读帧观察口（可选）。声纹过滤需要本轮 PCM；它只观察、不改任何判定。---
    frame_sink: Callable[[AudioPacket], None] | None = None
    # --- P10 · T1 #13：播报后的**输入静默窗**（毫秒；0 = 关闭，行为逐字节不变）---
    # 真机动机：播报结束后麦克风里仍有本机 TTS 的尾音/混响，自动收音可能把它当成用户语音的
    # 开头（"自己听自己"）。口径照 Pipecat `AlwaysUserMuteStrategy`（播报期静音用户输入）+
    # LiveKit `backchannel_boundary`（bot 说完后的冷却窗）。窗口内**只计数、不判起始**，
    # 不改任何 fail-closed 语义，也不碰 ASR/授权/安全链。
    speech_mute_gap_ms: int = 0

    def __post_init__(self) -> None:
        if type(self.auto_release) is not bool:
            raise ValueError("auto_release 必须是布尔值")
        for name in ("silence_ms", "speech_start_timeout_ms", "max_utterance_ms", "min_utterance_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        threshold = self.speech_threshold
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("speech_threshold 必须是正数")
        if not math.isfinite(float(threshold)) or float(threshold) <= 0:
            raise ValueError("speech_threshold 必须是正数")
        if self.speech_detector is not None and not callable(self.speech_detector):
            raise ValueError("speech_detector 必须是可调用对象或 None")
        required = self.speech_detector_frames_required
        if required is not None and (
            isinstance(required, bool) or not isinstance(required, int) or required <= 0
        ):
            raise ValueError("speech_detector_frames_required 必须是正整数或 None")
        for name in ("segment_probe", "segment_flush"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise ValueError(f"{name} 必须是可调用对象或 None")
        if self.frame_sink is not None and not callable(self.frame_sink):
            raise ValueError("frame_sink 必须是可调用对象或 None")
        gap = self.speech_mute_gap_ms
        if isinstance(gap, bool) or not isinstance(gap, int) or gap < 0:
            raise ValueError("speech_mute_gap_ms 必须是非负整数毫秒（0 = 关闭）")


def _pcm16_rms(payload: bytes | bytearray | memoryview) -> float:
    """按 ``pcm_s16le`` 计算 PCM16 RMS 幅值；空载荷返回 0.0。

    与 ``app.runtime.voice_entry.energy_detector`` 同一算法，但本模块**不导入**
    ``voice_entry``（那会拉起音频/TTS 依赖）。
    """

    raw = bytes(payload)
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        return 0.0
    samples = struct.unpack(f"<{usable // 2}h", raw[:usable])
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples))


def _energy_detector(threshold: float) -> Callable[[AudioPacket], bool]:
    """基于 PCM16 RMS 的本地能量判定；返回布尔值或抛出异常。"""

    def detect(packet: AudioPacket) -> bool:
        return _pcm16_rms(packet.payload) >= threshold

    return detect


class _GatedSpeechStartDetector(SpeechStartDetector):
    """把注入的能量判定接进既有 ``SpeechStartDetector`` 的起始计数。

    既有 ``SpeechStartDetector`` 通过 ``config.min_rms_dbfs`` 只用一种固定的内部 RMS
    门槛；自动收音驱动需要可注入的能量判定（便于离线替身与阈值调整），因此这里保持
    ``min_rms_dbfs=None``（其内部门槛恒真）并用注入的能量判定重写单帧命中语义：
    ``config.speech_frame_threshold`` 次连续命中才确认起始。计数、连续/累计语义与
    ``reset()`` 完全沿用父类，不重复实现起始判定。
    """

    def __init__(
        self, *, config: SpeechStartConfig, energy_detector: Callable[[AudioPacket], bool]
    ) -> None:
        super().__init__(config=config)
        self._energy = energy_detector

    def _meets_energy_gate(self, packet: AudioPacket) -> bool:
        # 注入的能量判定必须原样返回（True/False）；布尔强制转换会掩盖非布尔返回值。
        return self._energy(packet)  # type: ignore[return-value]


class _ObservingVadSession(VadSession):
    """把每一帧先交给自动收音判定、再交给既有 VAD 转发的 VAD 会话。

    只覆盖 ``process``：``start`` / ``finish`` / ``cancel`` 与连续性台账完全沿用既有
    ``VadSession`` 语义。观察器不改变 VAD 结果，也不缓存音频。

    转发语义（与冻结契约 ③ 的偏离及原因）：既有 ``VadSession.process`` 把
    ``detector is None`` 直接判为 ``DETECTOR_UNAVAILABLE`` 并 **FAILED 关闭总线**，
    因此 ``detector=None`` 既不能转发帧、也不能“静音永不达到阈值”。这里用
    ``detector=lambda packet: True`` 达到契约 ③ 真正要求的语义：每帧都判为有语音，
    静音计数恒为 0，VAD **永不**自行结束轮次，只做连续性校验与帧转发；自动结束
    只由本驱动的计数判定（在 ``process`` 之前观察，见模块 docstring）。
    """

    def __init__(
        self, *, bus: AudioBus, config: VadConfig, observer: Callable[[AudioPacket], None]
    ) -> None:
        if not callable(observer):
            raise TypeError("observer 必须可调用")
        super().__init__(bus=bus, config=config, detector=lambda packet: True)
        self._observer = observer
        self.seen_frames = 0

    def process(self, packet: AudioPacket):
        self._observer(packet)
        self.seen_frames += 1
        return super().process(packet)


class AutoCaptureVoiceDriver:
    """P6 coordinator 之上的自动收音驱动；不拥有识别、提交或播报。"""

    def __init__(
        self,
        *,
        coordinator: object | None = None,
        coordinator_factory: Callable[[Callable[[AudioBus], VadSession]], object] | None = None,
        config: AutoCaptureDriverConfig | None = None,
        start_detector: SpeechStartDetector | None = None,
        speech_handled_by_driver: bool = False,
        audio_session_id: str = "voice-loop-auto-audio",
        stream_id: str = "voice-loop-auto-stream",
        provider_generation: int = 1,
        energy_detector: Callable[[AudioPacket], bool] | None = None,
    ) -> None:
        if coordinator is None and coordinator_factory is None:
            raise TypeError("必须提供 coordinator 或 coordinator_factory")
        if coordinator is not None and coordinator_factory is not None:
            raise TypeError("coordinator 与 coordinator_factory 只能提供一个")
        if coordinator is not None:
            for name in _COORDINATOR_METHODS:
                if not callable(getattr(coordinator, name, None)):
                    raise TypeError(f"coordinator 必须提供 {name}")
        if coordinator_factory is not None and not callable(coordinator_factory):
            raise TypeError("coordinator_factory 必须可调用")
        if config is None:
            config = AutoCaptureDriverConfig()
        elif not isinstance(config, AutoCaptureDriverConfig):
            raise TypeError("config 必须是 AutoCaptureDriverConfig 或 None")
        if start_detector is not None and not isinstance(start_detector, SpeechStartDetector):
            raise TypeError("start_detector 必须是 SpeechStartDetector 或 None")
        if energy_detector is not None and not callable(energy_detector):
            raise TypeError("energy_detector 必须是可调用对象或 None")
        if not isinstance(audio_session_id, str) or not audio_session_id:
            raise ValueError("audio_session_id 必须是非空字符串")
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError("stream_id 必须是非空字符串")
        if (
            isinstance(provider_generation, bool)
            or not isinstance(provider_generation, int)
            or provider_generation < 1
        ):
            raise ValueError("provider_generation 必须是正整数")
        if type(speech_handled_by_driver) is not bool:
            raise TypeError("speech_handled_by_driver 必须是布尔值")
        self._coordinator_factory = coordinator_factory
        self._coordinator = coordinator
        self._audio_session_id = audio_session_id
        self._stream_id = stream_id
        self._provider_generation = provider_generation
        self._config = config
        self._injected_start_detector = start_detector
        self._speech_handled_by_driver = speech_handled_by_driver
        self._speech_detector = config.speech_detector
        self._energy = (
            energy_detector if energy_detector is not None else _energy_detector(config.speech_threshold)
        )
        self._boundary: VoiceTurnBoundary | None = None
        self._start_detector: SpeechStartDetector | None = None
        self._original_vad_factory: object | None = None
        self._detector_failed = False
        self._speech_started = False
        self._consecutive_silence = 0
        self._frames = 0
        self._elapsed_ms = 0
        self._frame_ms = 0
        self._stop_reason: str | None = None
        self._ended = False
        self._discard_on_release = False
        # 会话级 fail-closed 闩锁：abandon 后恢复状态没有回到 READY 时置位，后续 start 一律失败。
        self._coordinator_unavailable = False
        self._last_pump: object | None = None
        self._last_start_reason: str | None = None
        self._last_result: VoiceDriverTurnResult | None = None
        # 结束判定时间线（只读、有界）：供 artifact/真机复核，绝不参与任何判定。
        self._hits = 0
        self._silence_run = 0
        self._max_silence_run = 0
        self._last_hit_elapsed_ms = 0
        self._segment_ends = 0
        self._segment_probe_failures = 0
        self._end_source: str | None = None
        self._tail_flush_done = False
        self._frame_sink_disabled = False
        # P10 · T1 #13：播报期/播报后的输入静默窗（默认 0 ⇒ 永不为真、零行为变化）。
        self._mute_gap_ms = config.speech_mute_gap_ms
        self._speech_mute_active = False
        self._speech_mute_until: float | None = None
        self._speech_mute_reason: str | None = None
        self._muted_frames = 0
        #: 本轮被静默的帧数（#13 修正：按**轮**归属，避免后续无播报的轮次重复上报旧计数）。
        self._turn_muted_frames = 0

    # ---- 只读诊断面 -------------------------------------------------------

    @property
    def config(self) -> AutoCaptureDriverConfig:
        """本驱动的自动收音参数；只读。"""

        return self._config

    @property
    def speech_started(self) -> bool:
        """本驱动是否已判定语音起始；``start()`` 新轮次时回到 False。"""

        return self._speech_started

    @property
    def auto_released(self) -> bool:
        """本驱动是否已自行判定输入结束（只读事实，由调用方据此结束收音）。"""

        return self._ended

    @property
    def last_pump(self) -> object | None:
        """最近一次 ``pump`` 的原始结果；仅供只读诊断，不参与任何安全判断。"""

        return self._last_pump

    @property
    def last_start_reason(self) -> str | None:
        """最近一次启动失败的具体原因；仅供诊断。"""

        return self._last_start_reason

    @property
    def last_result(self) -> VoiceDriverTurnResult | None:
        """最近一次 ``release`` 的归一化结果；仅供只读诊断。"""

        return self._last_result

    @property
    def last_stop_reason(self) -> str | None:
        """本驱动判定输入结束的原因；未结束或未开始时为 ``None``。"""

        return self._stop_reason

    @property
    def frames_seen(self) -> int:
        """本驱动已观察到的帧数；仅供只读诊断。"""

        return self._frames

    def observation_details(self) -> dict[str, object]:
        """本轮结束判定的机械事实（只读、有界；不含任何音频、正文或路径）。

        用途（待修 B）：真机复核"最后一个字是被谁切掉的"——``end_source`` 说明本轮输入边界
        由哪条分支关闭；``max_silence_run_ms`` / ``last_hit_elapsed_ms`` 说明静音累计与最后
        一次人声命中的真实位置；``segment_ends`` / ``segment_probe_failures`` 说明段级端点是否
        真正生效。全部计数都是标量，不随帧数增长。
        """

        return {
            "frames": self._frames,
            "elapsed_ms": self._elapsed_ms,
            "frame_ms": self._frame_ms,
            "hits": self._hits,
            "last_hit_elapsed_ms": self._last_hit_elapsed_ms,
            "max_silence_run_frames": self._max_silence_run,
            "max_silence_run_ms": self._max_silence_run * self._frame_ms,
            "silence_ms": self._config.silence_ms,
            "spoken_silence_ms": self._consecutive_silence * self._frame_ms,
            "speech_started": self._speech_started,
            "segment_end_available": self._config.segment_probe is not None,
            "segment_ends": self._segment_ends,
            "segment_probe_failures": self._segment_probe_failures,
            "end_source": self._end_source,
            "stop_reason": self._stop_reason,
        }

    # ---- 驱动生命周期 -----------------------------------------------------

    def start(self, boundary: VoiceTurnBoundary) -> bool:
        if not isinstance(boundary, VoiceTurnBoundary):
            raise TypeError("boundary 必须是 VoiceTurnBoundary")
        if self._boundary is not None:
            raise VoiceLoopError("语音管道已在进行中")
        if self._coordinator_unavailable:
            # abandon 后恢复状态没有回到 READY：不伪造恢复，后续轮次一律 fail closed。
            self._last_start_reason = STOP_COORDINATOR_NOT_READY
            return False
        self._reset_turn_state()
        if self._speech_detector is not None:
            # 注入的人声判定（silero 模式）优先于能量阈值：每帧命中与起始连续计数都由它
            # 驱动，能量判定在该模式下完全不参与；起始计数复用既有 SpeechStartDetector。
            self._start_detector = _GatedSpeechStartDetector(
                config=SpeechStartConfig(
                    min_rms_dbfs=None,
                    speech_frame_threshold=(
                        self._config.speech_detector_frames_required or 3
                    ),
                ),
                energy_detector=self._speech_gate,
            )
        elif self._injected_start_detector is not None:
            self._injected_start_detector.reset()
            self._start_detector = self._injected_start_detector
        else:
            self._start_detector = _GatedSpeechStartDetector(
                config=SpeechStartConfig(min_rms_dbfs=None),
                energy_detector=self._energy,
            )

        def vad_factory(bus: AudioBus) -> VadSession:
            return _ObservingVadSession(
                bus=bus,
                config=VadConfig(silence_duration_ms=_FORWARDING_VAD_SILENCE_MS),
                observer=self._observe,
            )

        if self._coordinator_factory is not None:
            coordinator = self._coordinator_factory(vad_factory)
            for name in _COORDINATOR_METHODS:
                if not callable(getattr(coordinator, name, None)):
                    raise TypeError(f"coordinator 必须提供 {name}")
        else:
            coordinator = self._coordinator
            if coordinator is None:
                raise TypeError("必须提供 coordinator 或 coordinator_factory")
            self._install_vad_factory(coordinator, vad_factory)
        self._coordinator = coordinator
        asr_boundary = AsrSessionBoundary(
            session_id=boundary.voice_session_id,
            audio_session_id=self._audio_session_id,
            provider_generation=self._provider_generation,
            stream_id=self._stream_id,
            utterance_id=boundary.utterance_id,
        )
        result = coordinator.start(asr_boundary)
        started = bool(getattr(result, "started", False))
        if started:
            self._boundary = boundary
            return True
        reason = getattr(result, "diagnostic", None) or getattr(result, "reason", None)
        self._last_start_reason = str(reason) if reason is not None else "start_rejected"
        if self._coordinator_factory is None:
            self._restore_vad_factory(coordinator)
        return False

    def pump(self) -> int:
        if self._boundary is None or self._ended:
            return 0
        coordinator = self._coordinator
        if coordinator is None:
            return 0
        result = coordinator.pump()
        self._last_pump = result
        frames = getattr(result, "processed_frames", 0)
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
            frames = 0
        if self._config.auto_release and not self._ended and self._stop_reason is None:
            if self._detector_failed:
                self._finish_locally(
                    STOP_DETECTOR_FAILURE,
                    auto_released=False,
                    discard=True,
                    source=END_SOURCE_DETECTOR_FAILURE,
                )
            elif bool(getattr(result, "provider_failed", False)):
                self._finish_locally(
                    STOP_PROVIDER_FAILED,
                    auto_released=False,
                    discard=True,
                    source=END_SOURCE_PROVIDER_FAILED,
                )
            else:
                self._evaluate_auto_end()
        return frames

    def release(self) -> VoiceDriverTurnResult:
        if self._boundary is None:
            raise VoiceLoopError("语音管道尚未开始")
        if self._last_result is not None:
            return self._last_result
        boundary = self._boundary
        # 待修 B：先让底层 VAD 把缓冲尾段定稿（最后一个字）；随后才让 provider 解码。
        self._flush_tail_segment()
        result = self._run_coordinator_release(boundary)
        self._boundary = None
        self._last_result = result
        coordinator = self._coordinator
        if coordinator is not None:
            self._restore_vad_factory(coordinator)
        return result

    def cancel(self, *, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串")
        if self._boundary is None:
            raise VoiceLoopError("语音管道尚未开始")
        coordinator = self._coordinator
        if coordinator is not None:
            self._discard_turn(reason=self._stop_reason or reason)
            self._restore_vad_factory(coordinator)
        self._boundary = None
        self._ended = False

    # ---- 内部实现 ---------------------------------------------------------

    def _install_vad_factory(
        self, coordinator: object, vad_factory: Callable[[AudioBus], VadSession]
    ) -> None:
        """把本驱动的帧观察 VAD 注入现成 coordinator。

        默认路径是构造时的 ``coordinator_factory``（驱动自己造 coordinator）；只有直接
        注入现成 coordinator 时才需要替换它的 ``_vad_factory``。无论哪种方式，本驱动的
        观察器都必须在 ``VadSession.process`` 之前看到每一帧，否则自动结束判定失去依据。
        原工厂在轮次结束（``release`` / ``cancel``）时恢复，避免把驱动注入永久留在
        既有 coordinator 上。
        """

        current = getattr(coordinator, "_vad_factory", None)
        if not callable(current):
            raise TypeError("coordinator 必须提供可调用的 _vad_factory 才能注入帧观察 VAD")
        if self._original_vad_factory is None:
            self._original_vad_factory = current
        coordinator._vad_factory = vad_factory  # noqa: SLF001 - 必须复用既有 coordinator

    def _restore_vad_factory(self, coordinator: object) -> None:
        original = self._original_vad_factory
        if original is None:
            return
        coordinator._vad_factory = original  # noqa: SLF001 - 恢复既有 coordinator
        self._original_vad_factory = None

    def _reset_turn_state(self) -> None:
        self._boundary = None
        self._last_pump = None
        self._last_start_reason = None
        self._last_result = None
        self._start_detector = None
        self._detector_failed = False
        self._speech_started = False
        self._consecutive_silence = 0
        self._frames = 0
        self._elapsed_ms = 0
        self._frame_ms = 0
        self._stop_reason = None
        self._ended = False
        self._discard_on_release = False
        self._hits = 0
        self._silence_run = 0
        self._max_silence_run = 0
        self._last_hit_elapsed_ms = 0
        self._segment_ends = 0
        self._segment_probe_failures = 0
        self._end_source = None
        self._tail_flush_done = False
        # #13 修正：静默帧数按**轮**计（窗口跨轮，计数不跨轮）。
        self._turn_muted_frames = 0

    def _observe(self, packet: AudioPacket) -> None:
        """观察一帧：起始确认 + 语音命中 + 静音/时长累计。异常与非布尔一律 fail closed。

        - 起始确认由 ``SpeechStartDetector`` 负责（连续命中阈值）。默认由驱动自建一个
          ``min_rms_dbfs=None`` + 注入能量判定的检测器；调用方也可注入自己的
          ``start_detector``。
        - “这一帧是否是语音”在默认模式下由驱动的能量判定决定（``speech_threshold`` 或
          注入的 ``energy_detector``）；配置了 ``speech_detector`` 时改由它决定，能量判定
          **完全不参与**（silero 模式）。
        - 注入的 ``start_detector`` 若不带能量门槛（例如 ``SpeechStartConfig()``），也不会
          让静音计时失效——否则真机自动结束只能靠最长轮次时长兜底。
        - P10 · T1 #13：静默窗内**只计帧数、不判起始、不计命中**（见 `begin_speech_mute`）。
        """

        detector = self._start_detector
        if detector is None or self._detector_failed:
            return
        if self.speech_mute_active:
            # #13：窗口内的帧不进入起始判定，也不给只读观察者（避免把播报/尾音当用户语音，
            # 也避免声纹缓冲混入 TTS）。时间轴同样不推进 ⇒ 不影响既有结束判定口径。
            self._muted_frames += 1
            self._turn_muted_frames += 1
            return
        self._emit_frame(packet)
        try:
            decision = detector.observe(packet)
        except Exception:  # noqa: BLE001 - 检测器异常必须 fail closed
            self._detector_failed = True
            return
        if not isinstance(decision, SpeechStartDecision):
            self._detector_failed = True
            return
        duration_ms = getattr(getattr(packet, "frame", None), "duration_ms", None)
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            duration_ms = 0
        self._frames += 1
        self._elapsed_ms += duration_ms
        if duration_ms > 0:
            self._frame_ms = duration_ms
        observed = decision.observed
        if type(observed) is not bool:
            self._detector_failed = True
            return
        hit = observed
        if self._injected_start_detector is not None and self._speech_detector is None:
            # 注入检测器只负责起始确认，语音/静音仍由能量判定决定。
            try:
                energy_hit = self._energy(packet)
            except Exception:  # noqa: BLE001 - 能量判定异常必须 fail closed
                self._detector_failed = True
                return
            if type(energy_hit) is not bool:
                self._detector_failed = True
                return
            hit = observed and energy_hit
        if decision.started:
            self._speech_started = True
        if hit:
            self._consecutive_silence = 0
            self._silence_run = 0
            self._hits += 1
            self._last_hit_elapsed_ms = self._elapsed_ms
        elif self._speech_started:
            self._consecutive_silence += 1
            self._silence_run += 1
            if self._silence_run > self._max_silence_run:
                self._max_silence_run = self._silence_run

    def _emit_frame(self, packet: AudioPacket) -> None:
        """把帧交给可选的只读观察者（例如声纹缓冲）。

        它**只观察**，不参与任何判定；观察者一旦抛错就整体停用（避免每帧重复失败），
        绝不影响收音、结束判定或 ASR 收尾。
        """

        sink = self._config.frame_sink
        if sink is None or self._frame_sink_disabled:
            return
        try:
            sink(packet)
        except Exception:  # noqa: BLE001 - 观察者故障不得影响语音链路
            self._frame_sink_disabled = True

    # ---- P10 · T1 #13：输入静默窗 -----------------------------------------

    def begin_speech_mute(self, *, reason: str = MUTE_REASON_PLAYBACK) -> None:
        """开始**无期限**输入静默（播报前调用）：窗口内不判语音起始。

        播报期本就没有进行中的输入轮次，这里是 Pipecat `AlwaysUserMuteStrategy` 的口径
        （bot 说话时静音用户输入）；播报结束必须调 `end_speech_mute()` 收口，否则会一直静音。
        """

        if not isinstance(reason, str) or not reason:
            raise ValueError("reason 必须是非空字符串")
        self._speech_mute_active = True
        self._speech_mute_until = None
        self._speech_mute_reason = reason
        self._muted_frames = 0
        self._reset_start_counters()

    def end_speech_mute(self, *, now: float) -> None:
        """播报结束：按 ``speech_mute_gap_ms`` 继续静默（0 = 立即恢复），并如实记原因。

        ``now`` 由调用方提供（本模块不读时钟），单位秒，通常是 ``time.monotonic()``。
        """

        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("now 必须是数值秒")
        gap_ms = self._mute_gap_ms
        if gap_ms <= 0:
            self._speech_mute_active = False
            self._speech_mute_until = None
            self._speech_mute_reason = None
        else:
            self._speech_mute_active = False
            self._speech_mute_until = float(now) + gap_ms / 1000.0
            self._speech_mute_reason = MUTE_REASON_POST_SPEECH
        self._reset_start_counters()

    @property
    def speech_mute_active(self) -> bool:
        """当前是否处于静默窗（过期即自动结束，不读外部时钟以外的东西）。"""

        if self._speech_mute_active:
            return True
        until = self._speech_mute_until
        if until is None:
            return False
        if time.monotonic() >= until:
            self._speech_mute_until = None
            self._speech_mute_reason = None
            self._reset_start_counters()
            return False
        return True

    @property
    def muted_frames(self) -> int:
        """**本轮**被静默的帧数（#13 修正：按轮归属，不继承上一轮窗口的旧计数）。"""

        return self._turn_muted_frames

    @property
    def window_muted_frames(self) -> int:
        """自最近一次 `begin_speech_mute()` 起、该静默窗累计吃掉的帧数（诊断用）。"""

        return self._muted_frames

    def mute_details(self) -> dict[str, object]:
        """只读事实：是否在窗口内 / **本轮**被静默的帧数 / 窗口累计帧数 / 原因码 / 窗口长度。"""

        return {
            "active": self.speech_mute_active,
            "muted_frames": self._turn_muted_frames,
            "window_muted_frames": self._muted_frames,
            "reason": self._speech_mute_reason,
            "gap_ms": self._mute_gap_ms,
        }

    def _reset_start_counters(self) -> None:
        """清空起始判定的连续/累计计数（注入的检测器没有 ``reset()`` 时静默跳过）。

        窗口内的帧不进判定，若不重置，窗口前攒下的命中可能在恢复后立刻凑够起始。
        重置失败**不**改 `detector_failed`（绝不因为附加保护把一个正常轮次判成故障）。
        """

        reset = getattr(self._start_detector, "reset", None)
        if not callable(reset):
            return
        try:
            reset()
        except Exception:  # noqa: BLE001 - 重置失败不影响既有判定语义
            return

    def _speech_gate(self, packet: AudioPacket) -> bool:
        """把注入的人声判定原样返回（不强制转布尔；非布尔由 ``_observe`` fail closed）。

        只在 ``config.speech_detector is not None`` 时通过 ``_GatedSpeechStartDetector`` 调用；
        判定抛错会沿 ``SpeechStartDetector.observe`` 冒泡，由 ``_observe`` 统一置
        ``detector_failed``（既有 fail-closed 语义不变）。
        """

        detector = self._speech_detector
        if detector is None:
            raise VoiceLoopError("speech_detector 未配置")
        return detector(packet)  # type: ignore[return-value]

    def _evaluate_auto_end(self) -> None:
        """自动结束判定：正常结束输入边界，随后仍由 ``release()`` 正常收尾提交。

        ``silence_timeout`` / ``max_utterance_duration`` 都表示“本轮说话结束”，必须让
        provider 解码已缓存的语音，因此这里**不**丢弃、**不**触碰 coordinator。

        判定顺序（待修 B）：未起始 → ``speech_start_timeout`` 硬上限；已起始 → 先
        ``max_utterance_ms``（硬上限，任何情况下都不被绕过），再**段级人声段结束**
        （Silero 自带 ``min_silence_duration`` 宽限，比帧级静音与最长上限都更贴近真人说完），
        最后才是既有的帧级静音累计。
        """

        if self._stop_reason is not None:
            return
        if not self._speech_started:
            # 未确认语音起始时，"开口超时"是硬上限：此时 max_utterance_ms 不参与判定，
            # 否则把 --max-record-seconds 设得比开口超时小时，空轮会被最长上限截断，
            # 而不是按设计以 no_speech 丢弃。
            if self._elapsed_ms >= self._config.speech_start_timeout_ms:
                # 未起始超时不算正常结束：不自动 release，由调用方取消本轮。
                self._finish_locally(
                    STOP_NO_SPEECH,
                    auto_released=False,
                    discard=True,
                    source=END_SOURCE_SPEECH_START_TIMEOUT,
                )
            return
        if self._elapsed_ms >= self._config.max_utterance_ms:
            self._finish_locally(
                STOP_MAX_UTTERANCE_DURATION,
                auto_released=True,
                source=END_SOURCE_MAX_UTTERANCE,
            )
            return
        if self._elapsed_ms < self._config.min_utterance_ms:
            # 太短：不允许"说话结束"类判定（段级端点/帧级静音）在此刻收尾；硬上限不受影响。
            return
        if self._segment_end_pending():
            self._segment_ends += 1
            self._finish_locally(
                STOP_SILENCE_TIMEOUT, auto_released=True, source=END_SOURCE_SEGMENT
            )
            return
        if self._consecutive_silence * self._frame_ms >= self._config.silence_ms:
            self._finish_locally(
                STOP_SILENCE_TIMEOUT, auto_released=True, source=END_SOURCE_SILENCE_FRAMES
            )

    def _segment_end_pending(self) -> bool:
        """段级结束探测：不可用 / 抛错 / 返回非布尔一律视为“无信息”。

        探测失败**绝不**结束本轮（宁可多录，由 ``max_utterance_ms`` 与帧级静音兜底），
        只累加只读计数 ``segment_probe_failures``；异常正文不参与判定也不外泄。
        """

        probe = self._config.segment_probe
        if probe is None:
            return False
        try:
            pending = probe()
        except Exception:  # noqa: BLE001 - 段级探测失败不得影响收音
            self._segment_probe_failures += 1
            return False
        if type(pending) is not bool:
            self._segment_probe_failures += 1
            return False
        return pending

    def _flush_tail_segment(self) -> None:
        """``release()`` 前把缓冲尾段定稿，保证最后一个字进入 provider 要解码的段。

        每轮只做一次；失败只累加只读计数，绝不把音频收尾问题升级成轮次故障。
        """

        if self._tail_flush_done:
            return
        self._tail_flush_done = True
        flush = self._config.segment_flush
        if flush is None:
            return
        try:
            flush()
        except Exception:  # noqa: BLE001 - 尾段定稿失败不得影响收尾
            self._segment_probe_failures += 1

    def _finish_locally(
        self,
        reason: str,
        *,
        auto_released: bool,
        discard: bool = False,
        source: str | None = None,
    ) -> None:
        """记录本地判定结果；``discard`` 表示输入不可用，``release()`` 必须 fail closed。"""

        if self._stop_reason is not None:
            return
        self._stop_reason = reason
        self._ended = auto_released
        self._discard_on_release = discard
        self._end_source = source if isinstance(source, str) and source else reason

    def _discard_turn(self, *, reason: str) -> None:
        """收尾一轮不可用输入：本地无输入走 ``abandon_turn``，真实 ASR 故障走 ``runtime_failed``。

        - ``no_speech`` / 调用方直接取消 / 正常结束后的放弃路径：调
          ``coordinator.abandon_turn(reason=...)``，不产生 ASR 故障事实。
        - ``detector_failure`` / ``provider_failed``：保持 ``runtime_failed(CANCELLED)``。
        - coordinator 没有 ``abandon_turn``（旧替身）：回退到 ``runtime_failed(CANCELLED)``。

        abandon 之后必须确认恢复状态回到 ``READY``；否则置位会话级 fail-closed 闩锁，
        把 ``last_stop_reason`` 记为 ``coordinator_not_ready`` 并让后续 ``start()`` 失败。
        """

        coordinator = self._coordinator
        if coordinator is None:
            return
        if self._stop_reason in _ASR_FAILURE_STOP_REASONS:
            # 真实 ASR/provider 故障：锁死通道，等待外部探测恢复。
            coordinator.runtime_failed(code=AsrFailureCode.CANCELLED)
            return
        abandon = getattr(coordinator, "abandon_turn", None)
        if not callable(abandon):
            # 兼容没有 abandon_turn 的旧 coordinator/替身：保持既有 runtime_failed 行为。
            coordinator.runtime_failed(code=AsrFailureCode.CANCELLED)
            return
        resolved = self._stop_reason or reason or "cancelled"
        try:
            require_identifier("reason", resolved)
        except ContractError:
            resolved = "cancelled"
        try:
            accepted = abandon(reason=resolved)
        except Exception:  # noqa: BLE001 - abandon 失败即视为通道未就绪
            self._latch_not_ready()
            return
        recovery_ready = self._recovery_ready(coordinator)
        if recovery_ready is True:
            return
        if recovery_ready is False:
            self._latch_not_ready()
            return
        # 无法读取 recovery 的替身：只接受 abandon_turn 的明确成功返回值。
        if accepted is not True:
            self._latch_not_ready()

    @staticmethod
    def _recovery_ready(coordinator: object) -> bool | None:
        """读 coordinator 的恢复状态；``True``/``False`` 表示可判定，``None`` 表示未暴露。"""

        recovery = getattr(coordinator, "recovery", None)
        if recovery is None:
            return None
        return getattr(recovery, "state", None) is AsrRecoveryState.READY

    def _latch_not_ready(self) -> None:
        """abandon 后未回到 READY：记录事实并锁死后续轮次（不伪造恢复）。"""

        self._stop_reason = STOP_COORDINATOR_NOT_READY
        self._coordinator_unavailable = True

    def _run_coordinator_release(self, boundary: VoiceTurnBoundary) -> VoiceDriverTurnResult:
        coordinator = self._coordinator
        if coordinator is None:
            return self._rejected_result(self._stop_reason or "cancelled")
        if self._discard_on_release:
            # 本地已判定输入不可用：先丢弃本轮资源，绝不提交、绝不携带任何正文。
            self._discard_turn(reason=self._stop_reason or "cancelled")
            return self._rejected_result(self._stop_reason or "cancelled")
        turn = coordinator.release(
            turn_id=boundary.turn_id,
            request_id=boundary.request_id,
        )
        decision = getattr(getattr(turn, "gate_result", None), "decision", None)
        accepted = decision is ResultGateDecision.ACCEPTED_FINAL
        outcome = getattr(turn, "outcome", None)
        event = getattr(getattr(turn, "gate_result", None), "event", None)
        recognized = getattr(event, "text", None) if accepted else None
        eligible = speech_eligible(outcome)
        # 诊断（2026-10-24）：与 `PushToTalkVoiceDriver.release()` 同口径 —— 非 chat 路由
        # 不会调用 Agent（`outcome is None`），Agent 失败也会让本轮不可播报；旧事实里两者
        # 都只剩一个 `text_only`。这里带上路由短码与跳过播报的固定短码，供真机定性。
        route = route_code(getattr(turn, "route", None))
        skip_reason = (
            None if eligible or not accepted else speech_skip_reason(outcome, route=route)
        )
        return VoiceDriverTurnResult(
            accepted=accepted,
            # 归一化语义与 PushToTalkVoiceDriver.release() 完全一致：被接纳即已提交。
            submitted=accepted,
            speech_eligible=eligible,
            speech_handled=accepted and self._speech_handled_by_driver,
            reply_text=outcome.text if eligible else None,
            reason=decision.value if isinstance(decision, ResultGateDecision) else "unknown",
            accepted_text=recognized if isinstance(recognized, str) else None,
            reply_chars=reply_chars(outcome),
            speech_skip_reason=skip_reason,
            route=route,
        )

    def _rejected_result(self, reason: str) -> VoiceDriverTurnResult:
        """本地已判定输入不可用（fail closed）：不接纳、不提交、不携带任何正文。"""

        return VoiceDriverTurnResult(
            accepted=False,
            submitted=False,
            speech_eligible=False,
            speech_handled=False,
            reply_text=None,
            reason=reason if reason else "cancelled",
            accepted_text=None,
        )


__all__ = [
    "AutoCaptureDriverConfig",
    "AutoCaptureVoiceDriver",
    "END_SOURCE_DETECTOR_FAILURE",
    "END_SOURCE_MAX_UTTERANCE",
    "END_SOURCE_PROVIDER_FAILED",
    "END_SOURCE_SEGMENT",
    "END_SOURCE_SILENCE_FRAMES",
    "END_SOURCE_SPEECH_START_TIMEOUT",
    "STOP_COORDINATOR_NOT_READY",
    "STOP_DETECTOR_FAILURE",
    "STOP_MAX_UTTERANCE_DURATION",
    "STOP_NO_SPEECH",
    "STOP_PROVIDER_FAILED",
    "STOP_SILENCE_TIMEOUT",
]
