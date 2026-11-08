"""P8 语音入口：把按键语音闭环装配成可复用的运行时会话。

本模块是 `app/main.py` 与 `tools/p8_real_voice_loop_runner.py` 共用的唯一装配点：

```text
按键（或 Enter）→ 真实麦克风采集 → VAD 帧判定 → 本地 sherpa-onnx SenseVoice
→ ResultGate → 同一个 ConversationManager → Agent（由运行时注入）→ 真实 TTS 播报
```

硬边界（与 P8 阶段验收一致）：

- 不创建计划、授权、账本或执行事实，不调用任何工具，不操作电脑；
- 原始音频不落盘；识别文本只交给调用方显示，不写日志、不写 artifact；
- Agent 由调用方注入的 `ConversationManager` 决定：运行时未接真实模型时，语音链路会
  如实回报“Agent 不可用”，绝不伪造回复；
- 只有被 ResultGate 接纳、且本轮确实检测到语音时才提交与播报（静音幻觉一律丢弃）。

本模块不做交互提示，也不决定退出方式；由调用方驱动 `VoiceLoopSession.run_turn()`。
`app/runtime/__init__.py` 刻意不重导出本模块，以免默认运行时导入链路背上音频/TTS 依赖。
"""

from __future__ import annotations

import array
import getpass
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.asr import SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.asr.push_to_talk import PushToTalkCoordinator
from app.asr.recovery import AsrRecoveryState
from app.audio.bus import AudioPacket
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceSelection,
    SoundDeviceCaptureBackend,
)
from app.audio.vad import VadConfig, VadSession
from app.contracts.base import require_choice, require_positive
from app.conversation import ConversationManager
from app.tts.clock import monotonic_seconds
from app.tts.pcm_playback import PCMPlaybackBackpressure, PCMPlaybackWorker
from app.tts.private_selection import (
    CONFIRMED_PROFILE,
    SPEED_MAX,
    SPEED_MIN,
    VoiceSelection,
    private_data_root,
    read_selection,
    verify_provider,
)
from app.tts.providers import VoiceStudioSentenceRequest
from app.tts.providers.edge_sapi import EdgeSapiConfig, EdgeSapiVoiceProvider
from app.tts.providers.http_wav import VoiceStudioHttpWavConfig, VoiceStudioHttpWavProvider
from app.tts.providers.voicestudio_ws import VoiceStudioWebSocketConfig, VoiceStudioWebSocketProvider
from app.tts.routing import TTSFallbackRouter
from app.tts.sequential_session import SequentialVoiceSession, minimum_pcm_queue_ms
from app.tts.streaming_session import DEFAULT_STALL_TIMEOUT_S, build_streaming_session
from app.tts.transport import VoiceStudioTransportRouter
from app.tts.waveout_pcm import PlaybackGain, WaveOutPCMStreamBackend
from app.voice_loop import (
    ChatVoiceLoop,
    PushToTalkVoiceDriver,
    VoiceInputSeam,
    VoiceLoopState,
    VoiceLoopStateMachine,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
)

TTS_MODES = ("ws_pcm", "http_wav", "auto", "pcm_seq", "edge", "none")
STREAMING_TTS_MODES = ("ws_pcm", "http_wav", "auto")
PCM_SEQ_MODE = "pcm_seq"
"""有界预取流水线播报通道（新增选项，默认通道行为不变）。

与 ``ws_pcm`` 走同一个 provider，但播放实现完全不同：合成线程逐句预取（待播队列
容量 K=2，满则等待、绝不丢弃），播放线程**串行**播放并只在 ticket 到达
``PlaybackState.COMPLETED`` 后才推进下一句 ⇒ 既不慢（合成超前 1~2 句），也绝不静默丢尾
（任何失败/超时/未达 COMPLETED 都抛 ``SequentialVoiceSessionError``）。
"""
VOICESTUDIO_TTS_MODES = STREAMING_TTS_MODES + (PCM_SEQ_MODE,)
"""需要本机 VoiceStudio 服务 + 声线校验的通道（``pcm_seq`` 与 ``ws_pcm`` 同源）。"""
PCM_SEQ_MAX_QUEUED_AUDIO_MS = max(30_000, minimum_pcm_queue_ms())
"""``pcm_seq`` 播放 worker 的 PCM 队列预算（毫秒）。

取 :func:`app.tts.sequential_session.minimum_pcm_queue_ms`（一句 120 字 × K=2 的估计，
24200 ms）与 30 s 的较大者：:class:`app.tts.pcm_playback.PCMPlaybackWorker` 会**拒绝**
（``PCMPlaybackBackpressure``）放不下的音频，预算小于一句就会让设备队列本身变成截断
来源。只影响新通道，``ws_pcm`` 仍用它自己的默认预算。
"""
AEC_REFERENCE_MODES = ("off", "same_process")
AEC_ENGINES = ("nlms", "webrtc")
"""AEC 参考来源：``off`` 不接参考；``same_process`` 取"实际提交给设备的 PCM"（方案 A）。"""

AEC_REFERENCE_MAX_CAPACITY_MS = 8000
"""同进程参考的容量上界（毫秒）；打断判定只看最近窗口，8 秒足够且内存有界。"""

_DUCK_QUANTUM_MS = 200
"""降音量的**生效粒度**（毫秒）＝waveOut 后端缓冲块大小（``_DEFAULT_BUFFER_BYTES``）。

如实说明：增益在"提交给设备"那一刻作用于整块缓冲，因此降音量不是"≤100 ms 内生效"
的承诺；这里把它作为可观测量登记，真机调优（阶段 13）另行验证。
"""
FRAME_MS = 20
VOICE_WS_ENDPOINT = "ws://127.0.0.1:3900"
VOICE_HTTP_BASE = "http://127.0.0.1:3900"
# VAD 只提供“这一帧有没有语音”的事实；静音结束由本模块按 silence_ms 决定，
# 避免用户还来不及开口就被 VAD 的固定静音阈值判结束。
VAD_CONFIG_SILENCE_MS = 60000
NO_SPEECH_REASON = "no_speech_discarded"
# Edge 离线播报的队列水位节流参数（见 EdgeStream.push_text_chunk）：
# 排空到安全水位以下再推下一片，避免合成速度快于实时导致 PCMPlaybackBackpressure。
_EDGE_QUEUE_SAFE_WATERMARK_MS = 6000
_EDGE_QUEUE_POLL_SECONDS = 0.02
_EDGE_QUEUE_TOTAL_WAIT_SECONDS = 60.0

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL_PATH = _REPO_ROOT / "models" / "sense-voice" / "model.int8.onnx"
_TOKENS_PATH = _REPO_ROOT / "models" / "sense-voice" / "tokens.txt"


class VoiceEntryError(RuntimeError):
    """语音入口不满足本地前置条件（依赖、设备、模型或 ASR 启动失败）。"""


@dataclass(frozen=True, slots=True)
class VoiceEntryConfig:
    """一次语音入口运行的本地参数；不读 `.env`，不改变任何默认设置。"""

    tts_mode: str = "ws_pcm"
    device_id: str | None = None
    silence_ms: int = 1500
    speech_timeout_seconds: int = 10
    max_record_seconds: int = 20
    speech_threshold: float = 600.0
    model_path: Path | None = None
    tokens_path: Path | None = None
    edge_voice: str = "Microsoft Huihui Desktop"
    aec_reference_mode: str = "off"
    aec_engine: str = "nlms"
    """回声消除引擎：``nlms``（默认，行为不变）/ ``webrtc``（AEC3，可选依赖）。"""
    speech_stall_timeout_s: float = DEFAULT_STALL_TIMEOUT_S
    """收尾的**停滞预算**（秒，P1）：连续这么久毫无进展才判定收尾失败。

    与 ``tts_mode`` 无关地同时作用于两条本机通道（流式会话与 pcm_seq 会话）。默认
    :data:`app.tts.streaming_session.DEFAULT_STALL_TIMEOUT_S`（15 s）= P1 默认行为；
    它替代了旧实现里"整条回复最多播 ``segment_timeout_s`` / ``join_timeout_s``"的总时长上限。
    """

    tts_speed: float | None = None
    """本次运行的播报语速覆盖（``None`` = 沿用声线选择里的值，即默认 1.0，行为不变）。

    P10 · T1（2026-10-23）：真机实测真机播报 ≈3.2 字/秒的一半耗时在**合成**（RTF 0.77），
    提高语速可同时压缩合成与音频时长（1.0→1.4：3.53→4.35 字/秒，用户听感确认自然）。
    覆盖只作用于**本次运行**，**不改**持久化的声线选择文件（那是身份 pin，`speed` 只是
    运行参数）；取值区间由 :data:`app.tts.private_selection.SPEED_MIN`/``SPEED_MAX`` 收口。
    """

    def __post_init__(self) -> None:
        require_choice("tts_mode", self.tts_mode, set(TTS_MODES))
        require_choice("aec_reference_mode", self.aec_reference_mode, set(AEC_REFERENCE_MODES))
        require_choice("aec_engine", self.aec_engine, set(AEC_ENGINES))
        for name in ("silence_ms", "speech_timeout_seconds", "max_record_seconds"):
            require_positive(name, getattr(self, name))
        if isinstance(self.speech_threshold, bool) or not isinstance(
            self.speech_threshold, (int, float)
        ) or self.speech_threshold <= 0:
            raise ValueError("speech_threshold 必须是正数")
        object.__setattr__(self, "speech_threshold", float(self.speech_threshold))
        if (
            isinstance(self.speech_stall_timeout_s, bool)
            or not isinstance(self.speech_stall_timeout_s, (int, float))
            or self.speech_stall_timeout_s <= 0
        ):
            raise ValueError("speech_stall_timeout_s 必须是正数")
        object.__setattr__(
            self, "speech_stall_timeout_s", float(self.speech_stall_timeout_s)
        )
        if self.tts_speed is not None:
            speed = self.tts_speed
            if isinstance(speed, bool) or not isinstance(speed, (int, float)):
                raise ValueError("tts_speed 必须是数值或 None")
            if not (SPEED_MIN <= float(speed) <= SPEED_MAX):
                raise ValueError(
                    f"tts_speed 必须在 {SPEED_MIN}..{SPEED_MAX} 之间"
                    "（None = 沿用声线选择里的值）"
                )
            object.__setattr__(self, "tts_speed", float(speed))
        if self.device_id is not None and (
            not isinstance(self.device_id, str) or not self.device_id
        ):
            raise ValueError("device_id 必须是非空字符串或 None")
        if not isinstance(self.edge_voice, str) or not self.edge_voice:
            raise ValueError("edge_voice 必须是非空字符串")
        for name in ("model_path", "tokens_path"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(value))

    @property
    def model(self) -> Path:
        return self.model_path or _MODEL_PATH

    @property
    def tokens(self) -> Path:
        return self.tokens_path or _TOKENS_PATH


@dataclass(frozen=True, slots=True)
class VoiceTurnFacts:
    """一轮按键语音的本地事实；`asr_text` 只用于显示，不落盘。"""

    index: int
    boundary: VoiceTurnBoundary | None
    frames: int
    speech_seen: bool
    end_reason: str
    accepted: bool
    submitted: bool
    spoken: bool
    reason: str
    seconds: float
    asr_text: str | None = None
    reply_text: str | None = None
    reply_chars: int | None = None
    """诊断（2026-10-24）：本轮 Agent 回复的**字数**（不可播报时也如实给出）；无结果为 ``None``。"""
    speech_skip_reason: str | None = None
    """诊断（2026-10-24）：本轮**没有可播报文本**的固定短码（如 ``no_outcome:route:task``）。"""
    route: str | None = None
    """诊断（2026-10-24）：本轮意图路由交接的固定短码（``chat``/``task``/…）；未路由为 ``None``。"""

    def boundary_facts(self) -> dict[str, str]:
        if self.boundary is None:
            return {}
        return {
            "voice_session_id": self.boundary.voice_session_id,
            "utterance_id": self.boundary.utterance_id,
            "turn_id": self.boundary.turn_id,
            "request_id": self.boundary.request_id,
            "reply_id": self.boundary.reply_id,
        }

    def log_details(self) -> dict[str, Any]:
        """脱敏日志字段：只有计数、状态与原因码，不含识别文本。"""

        return {
            "turn": self.index,
            "frames": self.frames,
            "speech_seen": self.speech_seen,
            "end_reason": self.end_reason,
            "accepted": self.accepted,
            "submitted": self.submitted,
            "spoken": self.spoken,
            "reason": self.reason,
            "asr_text_length": len(self.asr_text or ""),
            # 诊断（2026-10-24，`text_only` 定性）：同 auto_voice 口径，只有计数与固定码。
            "reply_chars": self.reply_chars,
            "speech_skip_reason": (
                None if self.speech_skip_reason is None else str(self.speech_skip_reason)[:64]
            ),
            "route": None if self.route is None else str(self.route)[:64],
        }


def energy_detector(threshold: float) -> Callable[[AudioPacket], bool]:
    """基于 PCM16 RMS 的本地语音活动判断；不引入任何外部依赖。"""

    def detect(packet: AudioPacket) -> bool:
        payload = packet.payload
        if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) < 2:
            return False
        samples = array.array("h")
        samples.frombytes(bytes(payload[: len(payload) - (len(payload) % 2)]))
        if not samples:
            return False
        total = 0
        for sample in samples:
            total += sample * sample
        return (total / len(samples)) ** 0.5 >= threshold

    return detect


_selection: VoiceSelection | None = None


def selection_for_run() -> VoiceSelection:
    """读取本机已确认的声线选择；缺失时回退到已确认 profile。"""

    global _selection
    if _selection is None:
        try:
            selection = read_selection(private_data_root())
        except Exception:  # noqa: BLE001 - 缺失时回退到已确认声线
            selection = VoiceSelection(CONFIRMED_PROFILE, getpass.getuser())
        selection.validate()
        _selection = selection
    return _selection


def selection_with_speed(
    selection: VoiceSelection, speed: float | None
) -> VoiceSelection:
    """把本次运行的语速覆盖叠到声线选择上（``None`` = 原样返回，零行为变化）。

    只替换**内存里**这一份选择；持久化的 ``xiaobing_voice_selection.json`` 一个字节都不动
    （身份 pin 归它，语速是运行参数）。``speed`` 的区间由 ``VoiceEntryConfig`` 先行校验。
    """

    if speed is None or float(speed) == selection.speed:
        return selection
    return replace(selection, speed=float(speed))


def build_same_process_reference(engine: str = "nlms") -> tuple[Any, Any]:
    """方案 A 的参考源 + 播放增益（可离线构造，不打开任何设备）。

    **两个时钟事实**：
    1. `app/tts/waveout_pcm.py` 的采样率硬编码 24 kHz（`SAMPLE_RATE`）——那是**提交侧**时钟；
    2. AEC/打断需要参考与**采集**同钟（16 kHz）：参考时间线按 16 kHz 建，
       由 `AecReferencePCM.source_sample_rate=24000` 触发**跨块连续重采样**。

    R4-A 之前这里把 24 kHz 的 PCM 原样塞进 16 kHz 时间线，于是
    `reference_matches_output=False` → 打断完全没有判别依据（真机三轮的根因）。

    ``engine``（R4-B P1）：``nlms``（默认，行为不变）或 ``webrtc``（WebRTC AEC3；
    可选依赖缺失时**如实**退回 nlms，由调用方从 `reference.backend` 读实际后端）。
    """

    from app.audio.aec.nlms import NlmsConfig
    from app.audio.aec.reference_source import (
        BACKEND_NLMS,
        BACKEND_WEBRTC,
        AEC_BACKENDS,
        AecReferencePCM,
        SameProcessAecReference,
    )
    from app.tts.waveout_pcm import SAMPLE_RATE as PLAYBACK_SAMPLE_RATE

    if engine not in AEC_BACKENDS:
        raise ValueError("engine 只支持 nlms 或 webrtc")
    capture_rate = CaptureConfig().sample_rate_hz
    pcm = AecReferencePCM(
        sample_rate=capture_rate,
        frame_duration_ms=FRAME_MS,
        max_capacity_ms=AEC_REFERENCE_MAX_CAPACITY_MS,
        source_sample_rate=PLAYBACK_SAMPLE_RATE,
    )
    if engine == BACKEND_WEBRTC:
        from app.audio.aec.webrtc_backend import WebrtcAecConfig

        return (
            SameProcessAecReference(
                pcm=pcm,
                backend=BACKEND_WEBRTC,
                webrtc_config=WebrtcAecConfig(
                    sample_rate=capture_rate, frame_duration_ms=FRAME_MS
                ),
            ),
            PlaybackGain(),
        )
    return (
        SameProcessAecReference(
            pcm=pcm,
            backend=BACKEND_NLMS,
            nlms_config=NlmsConfig(
                sample_rate=capture_rate,
                frame_duration_ms=FRAME_MS,
            ),
        ),
        PlaybackGain(),
    )


@dataclass
class VoiceEntryEnvironment:
    """真实语音环境：真实采集、真实 ASR、真实播报；离线测试可整体注入替身。"""

    capture_factory: Callable[[Any], CaptureSession]
    provider: SenseVoiceRecognizerAdapter
    speaker: Any | None
    devices: tuple[str, ...] = ()
    tts_mode: str = "ws_pcm"
    details: dict[str, Any] = field(default_factory=dict)
    aec_reference: Any | None = None
    """同进程 AEC 参考源（``aec_reference_mode=same_process`` 时存在；只读旁路）。"""
    playback_gain: Any | None = None
    """播放增益共享状态（打断降音量用）；``None`` 表示该链路不支持降音量。"""

    def preflight(self) -> None:
        """只在需要本机 VoiceStudio 时做一次真实健康检查；不打开麦克风。

        ``pcm_seq`` 与 ``ws_pcm`` 用同一个 VoiceStudio 服务与同一个已确认声线，所以它
        也走这次校验；``edge``/``none`` 照旧跳过（``STREAMING_TTS_MODES`` 本身一个字符
        都没改）。
        """

        if self.tts_mode not in VOICESTUDIO_TTS_MODES:
            return
        verify_provider(selection_for_run(), base_url=VOICE_HTTP_BASE)


def build_real_environment(config: VoiceEntryConfig) -> VoiceEntryEnvironment:
    """真实环境装配；调用方必须已完成显式设备/本机服务授权。"""

    backend = SoundDeviceCaptureBackend()
    try:
        devices = backend.list_input_devices()
    except ImportError as exc:
        raise VoiceEntryError(
            "缺少音频采集依赖 sounddevice（PortAudio）："
            f"{exc}；请先安装，例如 "
            r".\.venv\Scripts\python.exe -m pip install sounddevice==0.5.6"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 设备枚举失败统一按前置失败处理
        raise VoiceEntryError(f"无法枚举输入设备：{type(exc).__name__}: {exc}") from exc
    if not devices:
        raise VoiceEntryError("没有可用的输入设备")

    selection = (
        DeviceSelection.system_default()
        if config.device_id is None
        else DeviceSelection.fixed(config.device_id)
    )
    try:
        default_id = backend.default_input_device()
    except Exception as exc:  # noqa: BLE001 - 默认设备不可解析时按前置失败处理
        raise VoiceEntryError(f"无法解析系统默认输入设备：{type(exc).__name__}: {exc}") from exc
    selected_id = config.device_id if config.device_id is not None else default_id
    chosen = next((item for item in devices if item.device_id == selected_id), None)
    if chosen is None:
        raise VoiceEntryError(f"找不到输入设备 {selected_id!r}（系统默认：{default_id!r}）")

    check = backend.check_device(
        selection,
        sample_rate_hz=CaptureConfig().sample_rate_hz,
        channel_count=CaptureConfig().channel_count,
    )
    if check.status.value != "ready":
        raise VoiceEntryError(f"输入设备不可用：{check.status.value}")

    for path in (config.model, config.tokens):
        if not path.is_file():
            raise VoiceEntryError(f"ASR 模型文件缺失：{path}")

    provider = SenseVoiceRecognizerAdapter(
        SenseVoiceConfig(model_path=config.model, tokens_path=config.tokens)
    )
    clock = monotonic_seconds
    # 方案 A（用户批准）：AEC 参考取"实际提交给设备的 PCM"（同进程只读旁路）。
    # 采样率必须与播放后端**同钟**（waveout 硬编码 24 kHz）；后端会在锁外回调旁路，
    # 并且**只在提交成功后**回调，异常被吞掉、绝不影响播放。
    aec_reference: Any | None = None
    playback_gain: Any | None = None
    submit_observer: Callable[[bytes, float], None] | None = None
    gain_provider: Callable[[], float] | None = None
    if config.aec_reference_mode == "same_process":
        aec_reference, playback_gain = build_same_process_reference(config.aec_engine)
        submit_observer = aec_reference.as_submit_observer()
        gain_provider = playback_gain.read
    speaker: Any | None = None
    if config.tts_mode in STREAMING_TTS_MODES:
        selection_model = selection_with_speed(selection_for_run(), config.tts_speed)

        def factory() -> Any:
            return build_streaming_session(
                selection_model,
                mode=config.tts_mode,
                ws_provider=VoiceStudioWebSocketProvider(
                    VoiceStudioWebSocketConfig(
                        endpoint=VOICE_WS_ENDPOINT, sentence_timeout_seconds=18.0
                    ),
                    clock=clock,
                ),
                http_provider=VoiceStudioHttpWavProvider(
                    VoiceStudioHttpWavConfig(timeout_seconds=8.0)
                ),
                backend_factory=lambda: WaveOutPCMStreamBackend(
                    clock=clock,
                    submit_observer=submit_observer,
                    gain_provider=gain_provider,
                ),
                fallback_router=TTSFallbackRouter(),
                # P1：收尾判据（连续无进展）由 CLI/配置统一注入，两条本机通道同源。
                stall_timeout_s=config.speech_stall_timeout_s,
                clock=clock,
            )

        speaker = StreamingSpeaker(factory, label=f"voicestudio_{config.tts_mode}")
    elif config.tts_mode == PCM_SEQ_MODE:
        # 新增通道：同一个 provider（逐句请求），但播放换成"有界预取 + 串行播放"的
        # SequentialVoiceSession。默认通道（ws_pcm/http_wav/auto）的分支一个字符都没动。
        selection_model = selection_with_speed(selection_for_run(), config.tts_speed)

        def sequential_factory() -> Any:
            return SequentialVoiceSession(
                selection_model,
                transport_router=VoiceStudioTransportRouter(
                    mode="ws_pcm",
                    ws_provider=VoiceStudioWebSocketProvider(
                        VoiceStudioWebSocketConfig(
                            endpoint=VOICE_WS_ENDPOINT, sentence_timeout_seconds=18.0
                        ),
                        clock=clock,
                    ),
                    http_provider=VoiceStudioHttpWavProvider(
                        VoiceStudioHttpWavConfig(timeout_seconds=8.0)
                    ),
                    clock=clock,
                ),
                worker=PCMPlaybackWorker(
                    lambda: WaveOutPCMStreamBackend(
                        clock=clock,
                        submit_observer=submit_observer,
                        gain_provider=gain_provider,
                    ),
                    start_buffer_ms=160,
                    max_queued_audio_ms=PCM_SEQ_MAX_QUEUED_AUDIO_MS,
                    clock=clock,
                ),
                language="Chinese",
                # P1：与流式通道同一个停滞预算（``pcm_seq`` 的 join 只留作单线程预算）。
                stall_timeout_s=config.speech_stall_timeout_s,
                clock=clock,
            )

        speaker = StreamingSpeaker(sequential_factory, label="voicestudio_pcm_seq")
    elif config.tts_mode == "edge":
        selection_model = selection_for_run()
        speaker = EdgeSpeaker(
            lambda: EdgeStream(
                EdgeSapiVoiceProvider(EdgeSapiConfig(voice_name=config.edge_voice), clock=clock),
                PCMPlaybackWorker(
                    lambda: WaveOutPCMStreamBackend(
                        clock=clock,
                        submit_observer=submit_observer,
                        gain_provider=gain_provider,
                    ),
                    clock=clock,
                ),
                selection_model,
            )
        )

    def capture_factory(bus: Any) -> CaptureSession:
        return CaptureSession(bus=bus, backend=backend, selection=selection, config=CaptureConfig())

    return VoiceEntryEnvironment(
        capture_factory=capture_factory,
        provider=provider,
        speaker=speaker,
        devices=tuple(item.name for item in devices),
        tts_mode=config.tts_mode,
        aec_reference=aec_reference,
        playback_gain=playback_gain,
        details={
            "device": chosen.name,
            "device_id": chosen.device_id,
            "model": str(config.model),
            "aec_reference_mode": config.aec_reference_mode,
            "duck_quantum_ms": _DUCK_QUANTUM_MS,
        },
    )


MAX_PUSH_CHARS = 120
"""单次合成请求的**硬上限**（字）：缓冲区到此长度必须切一刀，不再无限等句末。

真机教训（R4-B，2026-10-03）：此前按 80 字硬切会把句子切碎，碎片落不到句末 ⇒ 流式会话把它们
**凝成一段长文本**（真机 364 字 → 只算 1 段），本机 TTS 服务对单次长请求只回开头那段音频
（用户听到"只说了个开头"，而会话报告"播完"）。⇒ 切分必须落在**真正的句末**。
但"只等句末"同样会失效（`52c4a61` 把上限放到 200 且只在真句末切，离线实测段数毫无收益）：
**无标点的超长文本会整段进一次合成请求**。⇒ 真句末优先，硬上限照旧生效。
"""

PUSH_BACKPRESSURE_PENDING = 4
"""队列里待播句段达到该数量就先等一等（默认队列预算是 8 段）。"""

PUSH_BACKPRESSURE_TIMEOUT_S = 15.0
"""等待队列腾位置的**有界**上限：超时后继续推送，绝不挂死调用方。"""

_SENTENCE_ENDERS = "。！？!?；;…\n"
_SECONDARY_BREAKS = "，、,：:）) "  # 仅在"没有真句末可切"时才用


def _last_secondary_cut(buffer: str) -> int:
    """返回缓冲区内"最后一个次级标点之后"的下标；找不到返回 0（＝没有可切点）。"""

    best = 0
    for char in _SECONDARY_BREAKS:
        position = buffer.rfind(char)
        if position >= 0 and position + 1 > best:
            best = position + 1
    return best


def split_for_push(text: str, *, max_chars: int = MAX_PUSH_CHARS) -> tuple[str, ...]:
    """把整段回复切成适合流式推送的小块：**真句末优先 + 硬上限生效**。

    规则（保持原文与顺序，**不增删字符**，`"".join(pieces) == text` 恒真）：

    1. 真句末（``。！？!?；;…`` 与换行）**优先**切——切点总是落在真句末之后，碎片不落句末
       会被流式会话凝成一段长文本（真机 364 字只算 1 段）；
    2. 一旦缓冲区达到硬上限 ``max_chars``，就在缓冲区内**最后一个次级标点**
       （``，、,：:）)`` 与空格）之后切；
    3. 缓冲区里连次级标点都没有时**硬切在 ``max_chars`` 字**——无标点超长文本绝不允许整段
       塞进一次合成请求（`52c4a61` 的失效模式）。

    因此任何一片的长度都不超过 ``max_chars + 1``。

    真机教训（R4-B，2026-10-03）：长回复（249 字）一次性推送时会一次性越过流式会话的
    队列预算（`max_queued_segments=8`）→ `finish()` 抛 `streaming_queue_budget_exceeded`
    → 播报取消（用户听到"说一半就停"，短回复 133 字则 6 段全播）。P7 的设计本就是
    **按句流式**；这里按句/按上限切分，配合 :meth:`SessionStream._await_room` 做有界背压。
    """

    if not text:
        return ()
    pieces: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in _SENTENCE_ENDERS:
            pieces.append(buffer)
            buffer = ""
            continue
        # 硬上限：真句末等不到就退而求其次（次级标点），再没有就硬切（仍不增删字符）。
        if len(buffer) >= max_chars:
            cut = _last_secondary_cut(buffer)
            if cut:
                pieces.append(buffer[:cut])
                buffer = buffer[cut:]
            else:
                pieces.append(buffer)
                buffer = ""
    if buffer:
        pieces.append(buffer)
    return tuple(pieces)


def _flat_code(value: Any) -> str | None:
    """Accept only a short fixed lowercase code for flat runtime facts."""

    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    if not all(char.islower() or char.isdigit() or char == "_" for char in value):
        return None
    return value


def _flat_int(value: Any) -> int | None:
    """只接受真正的整数事实（``bool`` 不算）；其余一律 ``None``。

    真机教训（2026-10-03）：生命周期事件只允许 str/int/float/bool/None，任何 tuple
    都会让整个免按键会话被判不可用。record 因此只能携带扁平基础类型，读不到的字段就
    如实记 ``None``，绝不猜。
    """

    return value if type(value) is int else None


def _flat_seconds(value: Any) -> float | None:
    """只接受有限、非负的秒数（``bool`` 不算）；其余一律 ``None``。

    P1 新事实 ``drain_seconds`` 专用：NaN / ±inf / 负数都不能当作"等了多久"的事实，
    读不到就如实记 ``None``，绝不猜；只保留毫秒精度，避免把浮点噪声写进日志。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)


class SessionStream:
    """把 P7 流式会话适配成闭环需要的流对象。"""

    def __init__(
        self,
        session: Any,
        record: dict[str, Any],
        *,
        backpressure_timeout_s: float = PUSH_BACKPRESSURE_TIMEOUT_S,
    ) -> None:
        if type(backpressure_timeout_s) not in {int, float} or backpressure_timeout_s <= 0:
            raise ValueError("backpressure_timeout_s 必须为正数")
        self._session = session
        self._record = record
        # P1：等待预算可注入，只为了能**确定性**测试"看到进展就顺延"这条语义。
        self._backpressure_timeout_s = float(backpressure_timeout_s)

    @property
    def record(self) -> dict[str, Any]:
        """本流的机械事实（只数值/固定码/异常类型名）。

        语音闭环用它判断"是否被有界收尾截断"（`finish_timed_out`）——没有这个公开入口时，
        截断永远不会被发现（R4-B 真机"说一半就停"而轮次仍记"播完"的直接原因）。
        """

        return self._record

    def _await_room(self) -> None:
        """队列里待播句段过多时**有界**等待；拿不到 `pending_sentences` 就立即返回。

        P1：截止时间在**看到进展**（``pending_sentences`` 变小）时顺延。旧实现是固定
        :data:`PUSH_BACKPRESSURE_TIMEOUT_S`——与收尾上限同属"按总时长"的判据：长回复只要
        播放比合成慢，生产者就会在 15 s 后放弃等待并继续推送，把压力全推给 ``_enqueue``
        的整批拒绝。现在只要队列在下降就一直等；队列**不下降**（真卡住）仍在同一个预算后
        放弃。**放弃后照旧继续推送**：这里从不丢任何东西，失败一律由 ``_enqueue`` 显式给出
        （"卡死必须显式拒绝"的语义因此原样保留）。
        """

        pending = getattr(self._session, "pending_sentences", None)
        if not isinstance(pending, int):
            return
        deadline = time.monotonic() + self._backpressure_timeout_s
        waits = 0
        while pending >= PUSH_BACKPRESSURE_PENDING and time.monotonic() < deadline:
            waits += 1
            time.sleep(0.02)
            current = getattr(self._session, "pending_sentences", pending)
            if not isinstance(current, int):
                break
            if current < pending:
                # 进展：整条流水线还在动，把等待预算整体顺延（不改变"不下降就放弃"）。
                deadline = time.monotonic() + self._backpressure_timeout_s
            pending = current
        if waits:
            self._record["backpressure_waits"] = int(self._record.get("backpressure_waits", 0) or 0) + waits

    def push_text_chunk(self, text: str) -> None:
        self._record["text_length"] = len(text)
        # R4-B 真机（"249 字只播 74 字却报播完"）：会话内部只报"已接受"的段，被拒/被丢的
        # 推送字数在 record 里完全不可见 ⇒ 这里累计**非空白字符数**（切分与分段都会 strip
        # 首尾空白，所以只有非空白字符数才可作为"应该播出的字数"下界），`finish()` 用它
        # 做端到端的截断判据。只记整数，绝不记正文。
        if isinstance(text, str):
            pushed = _flat_int(self._record.get("pushed_characters")) or 0
            self._record["pushed_characters"] = pushed + sum(
                1 for char in text if not char.isspace()
            )
        # R4-B：按句推送 + 有界背压，避免长回复一次性撞上队列预算（真机"说一半就停"）。
        for piece in split_for_push(text):
            self._session.push_text_chunk(piece)
            self._await_room()

    def _record_speech_facts(self, played: Any) -> None:
        """把本次播报的机械事实写进 record（只数值/固定码/异常类型名，不含文本）。

        判据（R4-B，真机"播完"其实是被静默丢弃）：**只有 `played == accepted` 且队列无残留
        时**才允许当作"完整播报"。三计数（`accepted`/`synthesized`/`played`）互相独立，
        再加上"推送字数 vs 播出字数"的端到端比较；任一条不成立就把 `finish_timed_out`
        置真（`chat_loop._speak()` 据此记 `speech_truncated`），并给出 `truncation_reason`
        固定短码。用户主动取消（barge-in）是**设计内的停止**，不算截断，语义不变。
        """

        session = self._session
        if played is None:
            # 异常路径：会话自己已经播出的内容才是事实，绝不当作"一句都没播"。
            played = getattr(session, "played_segments", ()) or ()
        played_items = tuple(played)
        played_chars = sum(len(item) for item in played_items)
        self._record["played_segments"] = len(played_items)
        self._record["played_chars"] = played_chars
        self._record["completed_sentences"] = len(getattr(session, "timing_samples", ()) or ())
        error = getattr(session, "error", None)
        self._record["error"] = None if error is None else type(error).__name__
        self._record["provider_error_code"] = _flat_code(
            getattr(session, "provider_error_code", None)
        )
        self._record["failed_sentence_index"] = _flat_int(
            getattr(session, "failed_sentence_index", None)
        )
        self._record["failed_request_chars"] = _flat_int(
            getattr(session, "failed_request_chars", None)
        )
        self._record["sentences_total"] = _flat_int(getattr(session, "sentences_total", None))
        self._record["sentences_synthesized"] = _flat_int(
            getattr(session, "sentences_synthesized", None)
        )
        self._record["sentences_played"] = _flat_int(getattr(session, "sentences_played", None))
        accepted = _flat_int(getattr(session, "accepted_segments", None))
        accepted_chars = _flat_int(getattr(session, "accepted_characters", None))
        synthesized = _flat_int(getattr(session, "synthesized_segments", None))
        pending_segments = _flat_int(getattr(session, "pending_segments", None))
        pending_after = int(getattr(session, "pending_sentences", 0) or 0)
        session_timed_out = bool(getattr(session, "finish_timed_out", False))
        cancelled = bool(getattr(session, "cancelled", False))
        pushed_chars = _flat_int(self._record.get("pushed_characters"))
        truncation: str | None = None
        if not cancelled:
            if pending_after > 0:
                truncation = "pending_after_finish"
            elif accepted is not None and len(played_items) != accepted:
                truncation = "played_below_accepted"
            elif accepted_chars is not None and played_chars != accepted_chars:
                truncation = "played_chars_below_accepted"
            elif pushed_chars is not None and played_chars < pushed_chars:
                # 端到端：推送过的正文多于真正播出的字数（例如被拒的推送、被丢的尾巴）。
                truncation = "played_chars_below_pushed"
        if truncation is None and session_timed_out:
            truncation = "finish_timeout"
        # R4-B 真机教训（"说一半就停"）：`SessionStream.finish()` 之后紧跟的 `close()`
        # 会**取消仍在播放的语音**（设计如此：有界收尾）。旧代码只记 played/completed，
        # 调用方因此无法区分"真的播完"与"被有界收尾截断"——轮次事实一直显示播完。
        # 这里把机械事实如实记下（只数值/固定码/异常类型名，不含文本）。
        self._record["finish_timed_out"] = bool(session_timed_out or truncation is not None)
        self._record["truncation_reason"] = truncation
        self._record["accepted_segments"] = accepted
        self._record["accepted_chars"] = accepted_chars
        self._record["synthesized_segments"] = synthesized
        self._record["pending_segments"] = pending_segments
        late = getattr(session, "late_events", ()) or ()
        self._record["late_events"] = tuple(str(item) for item in late)[:8]
        self._record["dropped_pushes"] = int(getattr(session, "dropped_pushes", 0) or 0)
        self._record["cancelled"] = cancelled
        # P1 收尾进展事实（只读、扁平）：本次收尾实际等了多久、判了几次停滞、停滞时流水线
        # 停在哪一步。`SequentialVoiceSession` 没有 `stall_timeouts`（它直接显式失败），
        # 该项如实为 None；三个字段都只允许 str/int/float/None。
        self._record["drain_seconds"] = _flat_seconds(getattr(session, "drain_seconds", None))
        self._record["stall_timeouts"] = _flat_int(getattr(session, "stall_timeouts", None))
        self._record["last_progress"] = _flat_code(getattr(session, "last_progress", None))
        # R4-B 段级事实（GitHub 调研：HA #182944「只播第一句」= 后续段在 provider 侧被拒）：
        # `failure_states` 是本次回复每个 provider 请求的播放状态，旧代码从不读它 ⇒
        # "首段成功、后续段失败"在本地完全不可见。这里只记固定短码与计数（无文本）。
        failure_states = getattr(session, "failure_states", None) or {}
        codes = sorted(str(getattr(state, "value", state)) for state in failure_states.values())
        self._record["provider_failure_codes"] = tuple(codes)[:8]
        self._record["provider_failure_count"] = len(failure_states)
        self._record["edge_attempts"] = int(getattr(session, "edge_attempts", 0) or 0)
        self._record["edge_failures"] = int(getattr(session, "edge_failures", 0) or 0)
        # R4-B 真机教训（"播完"其实是被静默丢弃）：`streaming_session._enqueue()` 越界时只置
        # `_overloaded=True` 并让 `_run` 退出，**已接受未播出的句段被静默丢弃**；而
        # `played == completed` 同源、`error=None`，对外看起来就是"播完"。现在 `_enqueue`
        # 改为等待式背压（越界只记事实、绝不丢段），并且上面的三计数 + 字数判据会把任何
        # "少播"如实记成截断；这里照旧记下 `overloaded` 与 finish 之后的 `pending_sentences`。
        self._record["overloaded"] = bool(getattr(session, "overloaded", False))
        self._record["pending_sentences_after_finish"] = pending_after
        # R4-B 追加取证（真机自相矛盾的一轮：`text=291 / pushed_chars=282 /
        # accepted_segments=1`，而 282 字只要真到过 `ReplySegmenter` 就必然切出 ≥5 段）。
        # ⇒ 把"段从 segmenter 到 pending 队列的路上在哪里消失"如实记下：已出段/已入队/
        # 被早退丢弃/segmenter 缓冲区残留。**只读、累加、不参与任何判定**：截断判据、
        # 原因码、等待与超时全部原样，读不到就照 `_flat_int` 记 `None`，绝不猜。
        self._record["dequeued_segments"] = _flat_int(
            getattr(session, "dequeued_segments_total", None)
        )
        self._record["dequeued_chars"] = _flat_int(
            getattr(session, "dequeued_characters_total", None)
        )
        self._record["enqueued_segments"] = _flat_int(
            getattr(session, "enqueued_segments_total", None)
        )
        self._record["enqueued_chars"] = _flat_int(
            getattr(session, "enqueued_characters_total", None)
        )
        self._record["enqueue_dropped_segments"] = _flat_int(
            getattr(session, "enqueue_dropped_segments_total", None)
        )
        self._record["enqueue_dropped_chars"] = _flat_int(
            getattr(session, "enqueue_dropped_characters_total", None)
        )
        self._record["segmenter_buffered_chars"] = _flat_int(
            getattr(session, "segmenter_buffered_characters", None)
        )

    def finish(self) -> Any:
        try:
            try:
                played = self._session.finish()
            except BaseException:
                # 会话以明确错误结束（例如 `streaming_queue_budget_exceeded`）：机械事实照样
                # 记下来，绝不让"失败但 record 一片空白"成为新的看不见的路径。
                self._record_speech_facts(None)
                raise
            self._record_speech_facts(played)
            return played
        finally:
            self._session.close()

    def cancel(self) -> None:
        self._record["cancelled"] = True
        try:
            self._session.cancel()
        finally:
            # 真实链路（`chat_loop._speak`）在**推送**抛错时会走 `_cancel_stream(stream)`
            # ⇒ 那是"整条回复失败"的路径，绝不留下"record 一片空白、只有原因码"的
            # 不可见状态。事实照记（含 `error` 与已播字数），`finish()` 之后若被调用会覆盖。
            self._record_speech_facts(None)
            self._session.close()


class StreamingSpeaker:
    """每次回复新建一个真实流式会话，并记录机械事实。"""

    def __init__(self, factory: Callable[[], Any], *, label: str) -> None:
        self._factory = factory
        self._label = label
        self.records: list[dict[str, Any]] = []

    def start_stream(self) -> SessionStream:
        # `index` 单调递增：调用方据此判断"本轮是否真的开了播报流"（避免读到上一轮的旧事实）。
        record: dict[str, Any] = {
            "route": self._label,
            "index": len(self.records),
            "text_length": 0,
            "error": None,
        }
        self.records.append(record)
        return SessionStream(self._factory(), record)


class EdgeStream:
    """离线 Edge 声线：直接合成并在本机播放一句。"""

    def __init__(
        self,
        provider: EdgeSapiVoiceProvider,
        worker: PCMPlaybackWorker,
        selection: VoiceSelection,
    ) -> None:
        self._provider = provider
        self._worker = worker
        self._selection = selection
        self.record: dict[str, Any] = {"route": "edge_sapi_local", "chunks": 0, "error": None}
        self._throttle_waits = 0
        self._queued_peak_ms = 0

    def push_text_chunk(self, text: str) -> None:
        request = VoiceStudioSentenceRequest(
            reply_id=f"p8-edge-{uuid.uuid4().hex[:8]}",
            segment_id="segment-1",
            provider_request_id=f"p8-edge-{uuid.uuid4().hex[:8]}",
            provider_generation=1,
            text=text,
            profile_id=self._selection.profile_id,
            language="Chinese",
            speed=self._selection.speed,
        )
        try:
            ticket = self._worker.begin_sentence(request)
            # 离线 SAPI 合成远快于实时（实测 0.38s 产出 8.4s 音频），一次性推入全部
            # 分片会立刻顶到 PCM 队列预算并抛 PCMPlaybackBackpressure，播放从未开始。
            # 这里按队列水位节流：水位降到安全线以下再推下一片，最多重试一次。
            self._push_chunks_throttled(request, ticket)
            self.record["queued_audio_ms_peak"] = self._queued_peak_ms
            self.record["throttle_waits"] = self._throttle_waits
            submit_at = self._worker.device_submit_at(request)
            gate_at = self._worker.device_gate_open_at(request)
            if submit_at is not None and gate_at is not None:
                self.record["gate_to_device_ms"] = round((submit_at - gate_at) * 1000, 3)
            self.record["playback_state"] = self._worker.wait(ticket, 30.0).value
        except Exception as exc:  # noqa: BLE001 - 播报失败不得改变会话事实
            self.record["error"] = type(exc).__name__

    def _push_chunks_throttled(self, request: VoiceStudioSentenceRequest, ticket: Any) -> None:
        deadline = time.monotonic() + _EDGE_QUEUE_TOTAL_WAIT_SECONDS
        for chunk in self._provider.stream_sentence(request, cancelled=threading.Event()):
            while self._worker.queued_audio_ms >= _EDGE_QUEUE_SAFE_WATERMARK_MS:
                if ticket.terminal or time.monotonic() > deadline:
                    raise RuntimeError("edge_playback_stalled")
                self._throttle_waits += 1
                self._queued_peak_ms = max(
                    self._queued_peak_ms, int(self._worker.queued_audio_ms)
                )
                time.sleep(_EDGE_QUEUE_POLL_SECONDS)
            try:
                self._worker.push_chunk(chunk)
            except PCMPlaybackBackpressure:
                # 并发竞态下再让出一次；仍失败则交给上层按播报失败处理。
                if ticket.terminal or time.monotonic() > deadline:
                    raise
                self._throttle_waits += 1
                time.sleep(_EDGE_QUEUE_POLL_SECONDS * 5)
                self._worker.push_chunk(chunk)
            self.record["chunks"] += 1
            self._queued_peak_ms = max(self._queued_peak_ms, int(self._worker.queued_audio_ms))

    def finish(self) -> None:
        self._worker.close()

    def cancel(self) -> None:
        self.record["cancelled"] = True
        self._worker.close()


class EdgeSpeaker:
    def __init__(self, factory: Callable[[], EdgeStream], *, label: str = "edge_sapi_local") -> None:
        self._factory = factory
        self._label = label
        self.records: list[dict[str, Any]] = []

    def start_stream(self) -> EdgeStream:
        stream = self._factory()
        self.records.append(stream.record)
        return stream


class VoiceLoopSession:
    """一次语音入口运行：装配闭环并逐轮驱动（不做交互提示）。"""

    def __init__(
        self,
        *,
        conversation: ConversationManager,
        environment: VoiceEntryEnvironment,
        config: VoiceEntryConfig | None = None,
        source_instance_id: str = "voice-entry",
    ) -> None:
        if not isinstance(conversation, ConversationManager):
            raise TypeError("conversation 必须是 ConversationManager")
        if not isinstance(environment, VoiceEntryEnvironment):
            raise TypeError("environment 必须是 VoiceEntryEnvironment")
        self._config = config or VoiceEntryConfig()
        if not isinstance(self._config, VoiceEntryConfig):
            raise TypeError("config 必须是 VoiceEntryConfig")
        self._environment = environment
        coordinator = PushToTalkCoordinator(
            conversation=conversation,
            provider=environment.provider,
            capture_factory=environment.capture_factory,
            vad_factory=lambda bus: VadSession(
                bus=bus,
                config=VadConfig(silence_duration_ms=VAD_CONFIG_SILENCE_MS),
                detector=energy_detector(self._config.speech_threshold),
            ),
            source_instance_id=source_instance_id,
            device_epoch=1,
            audio_capacity_frames=64,
        )
        self._coordinator = coordinator
        self._driver = PushToTalkVoiceDriver(coordinator=coordinator)
        registry = VoiceTurnRegistry(source_instance_id=source_instance_id)
        self._machine = VoiceLoopStateMachine(registry)
        self._seam = VoiceInputSeam(conversation=conversation, registry=registry)
        self._loop = ChatVoiceLoop(
            registry=registry,
            machine=self._machine,
            seam=self._seam,
            driver=self._driver,
            speaker=environment.speaker,
        )
        self._turns = 0
        self._last_text: str | None = None
        self._closed = False
        coordinator.startup_succeeded()
        if coordinator.recovery.state is not AsrRecoveryState.READY:
            raise VoiceEntryError(
                "ASR 启动失败，已按设计降级为文字输入；请检查 sherpa-onnx 是否已安装、模型路径是否正确"
            )
        self._seam.note_voice_ready()
        switched = self._seam.request_voice_switch(local_check_passed=True)
        if not switched.switched:
            raise VoiceEntryError(f"无法切回语音输入：{switched.reason}")

    @property
    def state(self) -> VoiceLoopState:
        return self._loop.state

    @property
    def loop(self) -> ChatVoiceLoop:
        """内部 ``ChatVoiceLoop``（只读）：供展示层（如右下角提示窗）挂阶段钩子。

        展示层只允许包装 ``release_input`` / ``complete_turn`` / ``_speak`` 三个动作并在
        ``restore`` 后复原；不得据此改变输入边界、提交或授权语义。
        """

        return self._loop

    @property
    def turn_count(self) -> int:
        return self._turns

    @property
    def last_recognized_text(self) -> str | None:
        return self._last_text

    @property
    def speaker_records(self) -> list[dict[str, Any]]:
        records = getattr(self._environment.speaker, "records", None)
        return list(records) if isinstance(records, list) else []

    def run_turn(self, *, stop_requested: Callable[[], bool] | None = None) -> VoiceTurnFacts:
        """驱动一轮按键语音；返回可显示/可记录的事实（含识别文本，仅内存）。"""

        if self._closed:
            raise VoiceEntryError("语音入口已关闭")
        self._turns += 1
        index = self._turns
        started_at = monotonic_seconds()
        boundary = self._loop.start_turn()
        if boundary is None:
            detail = self._driver.last_start_reason or "start_rejected"
            return VoiceTurnFacts(
                index=index,
                boundary=None,
                frames=0,
                speech_seen=False,
                end_reason="start_failed",
                accepted=False,
                submitted=False,
                spoken=False,
                reason=f"start_rejected: {detail}",
                seconds=round(monotonic_seconds() - started_at, 3),
            )

        speech_seen = False
        last_speech_at = started_at
        frames = 0
        end_reason = "max_seconds"
        while True:
            frames += self._loop.pump()
            decision = getattr(self._driver.last_pump, "last_vad", None)
            now = monotonic_seconds()
            if getattr(decision, "speech_detected", None) is True:
                speech_seen = True
                last_speech_at = now
            if speech_seen and (now - last_speech_at) * 1000 >= self._config.silence_ms:
                end_reason = "silence_timeout"
                break
            if stop_requested is not None and stop_requested():
                end_reason = "button_release"
                break
            if not speech_seen and now - started_at >= self._config.speech_timeout_seconds:
                end_reason = "no_speech"
                break
            if now - started_at >= self._config.max_record_seconds:
                end_reason = "max_seconds"
                break
            time.sleep(FRAME_MS / 1000)

        if not speech_seen:
            # 静音幻觉守卫：SenseVoice 对纯静音可能产出非中文幻觉文本，未检测到
            # 语音时直接丢弃本轮，绝不提交、绝不播报。
            self._loop.cancel_turn(reason="cancelled")
            return VoiceTurnFacts(
                index=index,
                boundary=boundary,
                frames=frames,
                speech_seen=False,
                end_reason=end_reason,
                accepted=False,
                submitted=False,
                spoken=False,
                reason=NO_SPEECH_REASON,
                seconds=round(monotonic_seconds() - started_at, 3),
            )

        self._loop.release_input()
        report = self._loop.complete_turn()
        last = self._driver.last_result
        text = getattr(last, "accepted_text", None)
        reply = getattr(last, "reply_text", None)
        self._last_text = text if isinstance(text, str) else None
        return VoiceTurnFacts(
            index=index,
            boundary=boundary,
            frames=frames,
            speech_seen=True,
            end_reason=end_reason,
            accepted=report.accepted,
            submitted=report.submitted,
            spoken=report.spoken,
            reason=report.reason,
            seconds=round(monotonic_seconds() - started_at, 3),
            asr_text=self._last_text,
            reply_text=reply if isinstance(reply, str) else None,
            # 诊断（2026-10-24）：与 auto_voice 同口径，只从本轮驱动结果里只读取值。
            reply_chars=getattr(last, "reply_chars", None),
            speech_skip_reason=getattr(last, "speech_skip_reason", None),
            route=getattr(last, "route", None),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop.boundary is not None:
            try:
                self._loop.cancel_turn(reason="shutdown")
            except Exception:  # noqa: BLE001 - 收尾失败不影响已记录事实
                pass


__all__ = [
    "FRAME_MS",
    "NO_SPEECH_REASON",
    "PCM_SEQ_MAX_QUEUED_AUDIO_MS",
    "PCM_SEQ_MODE",
    "STREAMING_TTS_MODES",
    "TTS_MODES",
    "VOICESTUDIO_TTS_MODES",
    "EdgeSpeaker",
    "EdgeStream",
    "SessionStream",
    "StreamingSpeaker",
    "VoiceEntryConfig",
    "VoiceEntryEnvironment",
    "VoiceEntryError",
    "VoiceLoopSession",
    "VoiceTurnFacts",
    "build_real_environment",
    "energy_detector",
    "selection_for_run",
]
