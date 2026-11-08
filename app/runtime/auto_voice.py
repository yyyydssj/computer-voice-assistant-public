"""P9：免按键自动收音的**可复用会话层**（主程序与真机 runner 共用同一实现）。

本模块从 ``tools/p9_auto_voice_runner.py`` 抽出，只负责“输入边界 + 既有关闭闭环”的编排：
自动收音（能量阈值或 Silero VAD + 段级端点）、连续对话轮次（``ChatVoiceLoop`` +
``VoiceInputSeam`` + ``VoiceTurnRegistry``）、可选唤醒词门禁、可选声纹误触发过滤。

硬边界（与抽取前逐条一致）：
- 只决定“何时开始收音 / 何时算说完 / 本轮是否提交”，**不决定授权**，不接触任务、计划、
  执行与结果事实；
- 唤醒词只开启新的输入会话（唤醒音频不进 ASR）；声纹只做误触发过滤且**前置于 ASR**；
- 可选能力（VAD / 唤醒词 / 声纹）装不上一律 fail closed（抛 ``AutoVoiceError`` 子类），
  绝不静默降级成“看起来启用了其实没有”；
- 展示层（右下角提示窗）故障一律隔离，绝不影响收音 / 识别 / 提交 / 播报。

与 runner 的差异：打印不再硬编码（改由调用方通过 ``notify`` 注入）；配置类见
``AutoVoiceConfig``（runner 通过子类补上 turns/tts_mode/edge_voice/hud 等专属项）。

P10 批次 R4（会话循环接线，默认全关）在本模块上追加三条**可选**接线：

- **独立打断通道**（``barge_in_mode``）：播报期间自己开一路采集（``app.duplex.barge_in_channel``，
  自有总线/流身份与自有监视线程），逐帧喂 R1a/R1b 两阶段状态机；**不复用**
  ``interrupt_source``（那是"中止整个会话"的键盘轮询口），也不改 `app/safety`/`app/executor`/`app/plans`；
- **安全门**（``app.duplex.barge_in_safety``）：插话永不授权；安全确认提示被打断即失效、必须
  完整重播，且打断音频**不**建立可用轮次；
- **设备策略**（``device_recovery_mode``）：消费 R3 ``DeviceRecoveryPolicy``，只在**安全空闲点**
  取决策，重连各试一次，输出断开时停止播报并**回退文字**。

``aec_reference_mode=same_process`` 时消费 R2b 同进程参考（方案 A，"实际提交给设备的 PCM"）：
采集 16 kHz 与播放后端 24 kHz **不同钟**，因此本批**不**给出回声相关性，按 R2a 如实降到
``strict_no_aec``，**绝不声称已消除回声**。

R4 明确**未做**（不伪造成功）：把前置缓冲音频并入下一轮 ASR 需要 audio 层的 pre-roll 注入
API（``AudioBus`` 的序号/帧号连续性与采集会话计数器必须一起处理），本批只把该段音频保留
并通过 :meth:`AutoVoiceSession.take_pending_pre_roll` 暴露，轮次事实里如实写
``pre_roll_merge_unsupported``。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import threading
import time
from typing import Any
import uuid

_REPO = Path(__file__).resolve().parents[2]
"""项目根（``app/runtime/auto_voice.py`` → 上溯两级）。"""
FRAME_MS = 20
TTS_MODES = ("ws_pcm", "http_wav", "auto", "edge", "none")
THRESHOLD_MODES = ("fixed", "auto")
VAD_MODES = ("energy", "silero")
KWS_MODES = ("off", "gate")
VOICEPRINT_MODES = ("off", "filter")
BARGE_IN_MODES = ("off", "duck", "confirm")
"""打断模式：``off`` 完全关闭；``duck`` 只做第一阶段降音量（确认不生效）；
``confirm`` 降音量 + 确认后停止播报并把前置音频交给下一轮。"""

AEC_ENGINES = ("nlms", "webrtc")
"""回声消除引擎：``nlms`` = 项目自带逐样本 NLMS（默认，行为不变）；
``webrtc`` = WebRTC AEC3（可选依赖 `pywebrtc-audio`，需 ``aec_reference_mode=same_process``）。"""

AEC_REFERENCE_MODES = ("off", "same_process")
"""AEC 参考来源：``off`` / ``same_process``（方案 A：实际提交给设备的 PCM，只读旁路）。"""

DEVICE_RECOVERY_MODES = ("off", "safe_point")
"""设备恢复：``off`` / ``safe_point``（只在安全空闲点取决策并各试一次重连）。"""

from app.audio.aec.double_talk import DT_MODE_OFF, DT_MODES  # noqa: E402 - 常量块之后

DOUBLE_TALK_MODES = DT_MODES
"""R4-B5 双讲检测档位：``off`` / ``facts`` / ``protect``（默认 ``off``）。"""

BARGE_IN_INTERRUPTED_REASON = "barge_in_interrupted"
"""确认插话中断本次播报时的固定原因码。"""

PRE_ROLL_MERGE_UNSUPPORTED = "pre_roll_merge_unsupported"
"""前置音频**未**并入下一轮 ASR 的如实原因码（本批没有 audio 层注入接口）。"""

DUCK_UNAVAILABLE_REASON = "duck_unavailable"
"""播放链路没有增益钩子时的如实原因码（绝不声称已降音量）。"""

NO_ECHO_REFERENCE_REASON = "no_usable_echo_reference"
"""**无可用回声参考时的如实原因码：整个打断功能按设计不启用（半双工）。**

真机教训（2026-10-02 第三轮）：本机扬声器自己的声音在麦克风处就足以越过能量门，
而 16 kHz 采集 / 24 kHz 播放拿不到可用参考 ⇒ 无法区分"用户插话"与"自己的声音"，
确认阶段会把 AI 自己的声音判成插话（实测 6 个回合全部误停播）。此时任何门槛都只是猜，
按设计 fail-closed：**播放期间不守望输入**（半双工），并如实记录原因。
"""

CONFIRM_SUPPRESSED_REASON = "confirm_suppressed_in_duck_mode"
"""``duck`` 模式下确认插话不生效的如实原因码。"""

BARGE_IN_UNPROVEN_REASON = "barge_in_unproven_echo"
"""尚未证明"自己的回声可被观测"时，确认插话**不动手**的如实原因码（R4-A 安全门）。"""

DEFAULT_BARGE_IN_MAX_MONITOR_SECONDS = 120.0
#: P10 · T1 #16：播报期间键盘监视的轮询间隔（30 ms：够快，且几乎不占 CPU）。
KEY_WATCH_INTERVAL_SECONDS = 0.03
"""单次播报的打断监视硬上限（秒）；到点即收尾，绝不无限守望。"""

# 设备探测（R4 的"各试一次重连"）使用自有的音频总线身份，绝不复用轮次采集身份。
DEVICE_PROBE_AUDIO_SESSION_ID = "p10-device-probe"
DEVICE_PROBE_STREAM_ID = "p10-device-probe-stream"
DEVICE_PROBE_STOP_REASON = "device-probe"
DEVICE_PROBE_CAPACITY_FRAMES = 8
DEFAULT_KEYWORD = "小饼"
# 唤醒词门禁使用自有总线身份；唤醒阶段的音频**不进 ASR**（唤醒词只开启新会话）。
WAKE_AUDIO_SESSION_ID = "p9-auto-wakeword"
WAKE_STREAM_ID = "p9-auto-wakeword-stream"
WAKE_STOP_REASON = "wakeword-gate"
WAKE_CAPACITY_FRAMES = 64
DEFAULT_VAD_MODEL = _REPO / "models" / "openwakeword" / "silero_vad.onnx"
NO_SPEECH_REASON = "no_speech_discarded"

# 单轮自动收音的音频总线容量（帧）；20ms/帧 → 10 秒积压。
# 旧值 64（1.28s）对"消费者暂时落后于采集"没有任何余量：``AudioBus.publish`` 在队列满时
# 走 ``_fail_locked("overflow")``，会把总线置为 FAILED **并清空队列**，本轮已采集音频全部作废、
# 轮次只能被丢弃。这里的容量只用于兜住异常积压（内存上限仍是几十 KB 的 PCM），正常路径由
# "有帧就立刻继续泵、不固定睡满一帧"保证消费者不落后（见 ``_turn_idle_sleep_seconds``）。
AUDIO_CAPACITY_FRAMES = 500

# 已开始收音但连续这么多毫秒**一帧都没有** → 判定输入停滞（设备断开/总线失效）并立即收尾。
# 旧实现只能靠 ``speech_start_timeout_ms + max_utterance_ms``（默认 23s）兜底空转。
# 只在"完全没有帧"时生效：有环境声但没人说话属于正常空轮（帧仍在推进），不受影响。
INPUT_STALL_MS = 5000


def _turn_idle_sleep_seconds(processed_frames: int) -> float:
    """本轮的轮询睡眠策略：泵到帧就不睡（先把积压追平），一帧都没有才让出 CPU 一帧时长。

    固定"每轮睡 20ms"会让消费者永久慢于采集（20ms 命中 + 任何开销 > 采集周期），积压
    只会单调增长并最终撑爆总线；这里让泵在有帧时立即重试，从而与采集同速、不积压。
    """

    return 0.0 if processed_frames > 0 else FRAME_MS / 1000


# 校准采样使用自有的音频总线身份与容量；容量必须容纳整个采样窗口（1200ms/20ms=60 帧）。
CALIBRATION_AUDIO_SESSION_ID = "p9-auto-threshold-calibration"
CALIBRATION_STREAM_ID = "p9-auto-threshold-calibration-stream"
CALIBRATION_STOP_REASON = "threshold-calibration"
CALIBRATION_CAPACITY_FRAMES = 256
CALIBRATION_DEADLINE_FACTOR = 4

@dataclass(frozen=True, slots=True)
class AutoVoiceConfig:
    """免按键自动收音的参数基类（只含与语音边界有关的项；runner 可继承扩展）。"""

    device_id: str | None = None
    speech_threshold: float = 600.0
    silence_ms: int = 2500
    speech_start_timeout_ms: int = 8000
    max_record_seconds: int = 15
    model_path: Path | None = None
    tokens_path: Path | None = None
    threshold_mode: str = "fixed"
    threshold_multiplier: float = 3.0
    vad_mode: str = "silero"
    vad_model_path: Path | None = None
    vad_threshold: float = 0.5
    vad_frames: int = 2
    vad_min_silence_ms: int = 500
    input_stall_ms: int = INPUT_STALL_MS
    min_utterance_ms: int = 300
    kws_mode: str = "off"
    kws_model_dir: Path | None = None
    kws_keywords_file: Path | None = None
    keyword: str = DEFAULT_KEYWORD
    kws_threshold: float = 0.25
    kws_wait_seconds: int = 30
    voiceprint_mode: str = "off"
    voiceprint_model_path: Path | None = None
    voiceprint_profile: Path | None = None
    voiceprint_threshold: float = 0.5
    voiceprint_seconds: int = 8
    barge_in_mode: str = "off"
    barge_in_config: Any | None = None
    barge_in_max_monitor_seconds: float = DEFAULT_BARGE_IN_MAX_MONITOR_SECONDS
    aec_reference_mode: str = "off"
    device_recovery_mode: str = "off"
    #: P10 · T1 #16：语音台词出口（拟人化时注入 persona/LLM 渲染；None = 缺省短句，逐字不变）。
    voice_lines: Any | None = None
    barge_in_alignment_interval_frames: int = 25
    """打断通道做一次"回声对齐 + 相关性"估计的帧间隔（25 帧 = 0.5 s）。

    R4-A：没有对齐结论就没有相关性（`echo_correlation=None`），第一阶段降音量也会按设计
    关闭；该间隔越短越早拿到判别依据，但每次估计约 30~50 ms，故默认 0.5 s 一次。
    """
    barge_in_echo_proof_correlation: float = 0.2
    """**回声可观测性门槛**：对齐后的相关性达到该值才允许把确认插话真正落到停播上。

    R4-A 安全门（默认 fail-closed）：没证明"能看见自己的回声"之前只观测不动手，
    避免真机第三轮那种"自己的声音被当成插话"的假停播。
    """
    barge_in_cooldown_ms: int = 0
    """方案 C 兜底：播放开始后的冷却窗口（毫秒）；**0 = 关闭（默认，行为不变）**。

    R4-B-2 阶段 0 的结论（`research/2026-10-03-P10-R4B-2-doubletalk-findings.md`）：真 AEC3
    在本机会把用户语音压 5.5~20.5 dB，而本机又拿不到可信回声依据。冷却窗口是**不依赖真机**
    的保护层：窗口内收帧、填前置缓冲，但不降音量、不建立疑似/确认。它**不改变** R4-A 的
    fail-closed 口径（没证明回声仍不真停播）。建议启用值 1000 ms（对齐 LiveKit
    `backchannel_boundary`）。
    """
    post_speech_mute_ms: int = 0
    """P10 · T1 #13：播报后的**输入静默窗**（毫秒）；**0 = 关闭（默认，行为逐字节不变）**。

    口径照 Pipecat `AlwaysUserMuteStrategy`（播报期静音用户输入）+ LiveKit
    `backchannel_boundary`（bot 说完后的冷却窗）：窗口内自动收音**只计数、不判语音起始**。

    ⚠️ 真机结论（2026-10-14，`--post-speech-mute-ms 800`）：窗口确实生效（每轮开头静默
    34~36 帧 ≈ 0.68~0.72 s），但它**吃掉的是紧随播报那一轮开头的用户语音**（可能是首字），
    而本机并没有观察到它要防的"尾音被当成用户语音"⇒ **不建议开启**；若要试，建议
    150~300 ms 并单独验收首字是否被吞。
    """
    aec_engine: str = "nlms"
    """回声消除引擎（**默认 nlms，行为不变**）：``nlms`` = 项目自带逐样本 NLMS；
    ``webrtc`` = WebRTC AEC3（`app/audio/aec/webrtc_backend.py`，可选依赖）。

    只有 ``aec_reference_mode=same_process`` 且参考源后端确实是 webrtc、底层依赖可用时，
    会话才**真的逐帧跑 AEC3**并如实上报 `CAPABILITY_SOFTWARE`；否则维持
    `CAPABILITY_UNAVAILABLE`（绝不声称已消除回声）。
    真机实测（`research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`）：
    AEC3 在本机 ERLE 10.3 dB（实测延迟）/ 15.5 dB（最佳延迟提示）。
    """
    tts_mode: str = "ws_pcm"
    """播报通道（``ws_pcm`` 流式 / ``http_wav`` 整段 / ``auto`` / ``edge`` / ``none``）。

    真机教训（2026-10-03）：`_voice_entry_config_for_auto()` 之前**不透传** `tts_mode`，
    于是 `--tts-mode http_wav` 在 `--voice --auto` 下被**静默忽略**、永远走 `ws_pcm` 流式会话
    （命令行接受了却没生效，属诚实性问题）。默认仍是 ``ws_pcm``（行为不变）。
    """

    tts_speed: float | None = None
    """本次运行的播报语速覆盖（``None`` = 沿用声线选择里的值，即 1.0，行为不变）。

    真机实测（2026-10-23）：播报 ≈3.2 字/秒里约一半耗时在**合成**（RTF 0.77）；
    speed 1.0→1.4 可到 4.35 字/秒且用户听感确认自然，2.0 已偏快且 RTF 反升到 0.95。
    区间由 :data:`app.tts.private_selection.SPEED_MIN`/``SPEED_MAX`` 收口（0.5~2.0）。
    """

    speech_stall_timeout_s: float = 15.0
    """收尾的**停滞预算**（秒，P1）：连续这么久毫无进展才判定收尾失败。

    与 ``tts_mode`` 无关地同时作用于两条本机通道（流式 / pcm_seq）。默认 15 s =
    :data:`app.tts.streaming_session.DEFAULT_STALL_TIMEOUT_S`；它替代旧实现里"整条回复最多播
    ``segment_timeout_s`` / ``join_timeout_s``"的总时长上限。只要求正数——测试与现场调参
    （例如 TTS 特别慢时调到 30）都需要这个自由度。
    """

    aec_residual_gate_margin: float = 3.0
    """残余回声地板感知的动态门倍率（≥1.0；仅在 AEC3 真在跑时生效）。

    真机实测：AEC3 最佳情况下残差 p50 已低于能量门，但仍有 **28%** 的帧超门 ⇒ 只靠固定门
    仍会自触发。启用后播放期有效门 = ``max(speech_threshold, 残余地板 × margin)``，
    地板取最近 1 s 残差的 p20（抗用户说话干扰）。**只在 AEC3 真在跑时生效**，否则维持固定门。
    """

    double_talk_mode: str = DT_MODE_OFF
    """R4-B5：双讲检测档位（``off`` / ``facts`` / ``protect``，**默认 off**）。

    - ``off``：与改动前逐字节一致（不建检测器、零逐帧开销）；
    - ``facts``（B2）：打断通道逐帧记录 `double_talk_*` 只读事实，**不改变**任何判定；
    - ``protect``（B3，**本批未接线**）：仍只记事实。

    硬前提：双讲事实上报只发生在打断通道真正启用时（默认 `--barge-in-mode off` 不启用打断，
    因此本开关单独打开**不会**产生任何逐帧事实，也不会改变行为）。
    """
    double_talk_config: Any | None = None
    """双讲判据阈值（``None`` = 用 `DoubleTalkConfig()` 默认值）。"""
    task_router: Any | None = None
    """P10 · T1 批次 3：待确认路由器（`PendingConfirmationRouter`）；``None`` = 不接任务执行。"""
    task_dispatch: Any | None = None
    """P10 · T1 批次 3：任务派发器（`TextTaskDispatcher`）。两者都为 ``None`` 时语音行为不变。"""

    def __post_init__(self) -> None:
        for name in (
            "silence_ms",
            "speech_start_timeout_ms",
            "max_record_seconds",
            "vad_frames",
            "vad_min_silence_ms",
            "input_stall_ms",
            "min_utterance_ms",
            "kws_wait_seconds",
            "voiceprint_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
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
        if self.device_id is not None and (
            not isinstance(self.device_id, str) or not self.device_id
        ):
            raise ValueError("device_id 必须是非空字符串或 None")
        if not isinstance(self.keyword, str) or not self.keyword.strip():
            raise ValueError("keyword 必须是非空字符串")
        for name in ("model_path", "tokens_path", "vad_model_path", "kws_model_dir",
                     "kws_keywords_file", "voiceprint_model_path", "voiceprint_profile"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, Path):
                continue
            if isinstance(value, str) and value:
                object.__setattr__(self, name, Path(value))
                continue
            raise ValueError(f"{name} 必须是路径、非空字符串或 None")
        if self.threshold_mode not in THRESHOLD_MODES:
            raise ValueError("threshold_mode 只支持 fixed 或 auto")
        multiplier = self.threshold_multiplier
        if (isinstance(multiplier, bool) or not isinstance(multiplier, (int, float))
                or not math.isfinite(float(multiplier)) or float(multiplier) <= 1.0):
            raise ValueError("threshold_multiplier 必须是大于 1 的数值")
        object.__setattr__(self, "threshold_multiplier", float(multiplier))
        if self.vad_mode not in VAD_MODES:
            raise ValueError("vad_mode 只支持 energy 或 silero")
        vad_threshold = self.vad_threshold
        if (isinstance(vad_threshold, bool) or not isinstance(vad_threshold, (int, float))
                or not (0.0 < float(vad_threshold) <= 1.0)):
            raise ValueError("vad_threshold 必须是 (0, 1] 的数值")
        object.__setattr__(self, "vad_threshold", float(vad_threshold))
        if self.kws_mode not in KWS_MODES:
            raise ValueError("kws_mode 只支持 off 或 gate")
        kws_threshold = self.kws_threshold
        if (isinstance(kws_threshold, bool) or not isinstance(kws_threshold, (int, float))
                or not (0.0 <= float(kws_threshold) <= 1.0)):
            raise ValueError("kws_threshold 必须是 0~1 的数值")
        object.__setattr__(self, "kws_threshold", float(kws_threshold))
        if self.voiceprint_mode not in VOICEPRINT_MODES:
            raise ValueError("voiceprint_mode 只支持 off 或 filter")
        vp_threshold = self.voiceprint_threshold
        if (isinstance(vp_threshold, bool) or not isinstance(vp_threshold, (int, float))
                or not (0.0 < float(vp_threshold) <= 1.0)):
            raise ValueError("voiceprint_threshold 必须是 (0, 1] 的数值")
        object.__setattr__(self, "voiceprint_threshold", float(vp_threshold))
        if self.barge_in_mode not in BARGE_IN_MODES:
            raise ValueError("barge_in_mode 只支持 off / duck / confirm")
        monitor = self.barge_in_max_monitor_seconds
        if (isinstance(monitor, bool) or not isinstance(monitor, (int, float))
                or not math.isfinite(float(monitor)) or float(monitor) <= 0.0):
            raise ValueError("barge_in_max_monitor_seconds 必须是正数")
        object.__setattr__(self, "barge_in_max_monitor_seconds", float(monitor))
        if self.barge_in_config is not None:
            from app.duplex.barge_in import BargeInConfig

            if not isinstance(self.barge_in_config, BargeInConfig):
                raise ValueError("barge_in_config 必须是 BargeInConfig 或 None")
        if self.aec_reference_mode not in AEC_REFERENCE_MODES:
            raise ValueError("aec_reference_mode 只支持 off 或 same_process")
        if self.device_recovery_mode not in DEVICE_RECOVERY_MODES:
            raise ValueError("device_recovery_mode 只支持 off 或 safe_point")
        interval = self.barge_in_alignment_interval_frames
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            raise ValueError("barge_in_alignment_interval_frames 必须是正整数")
        proof = self.barge_in_echo_proof_correlation
        if (
            isinstance(proof, bool)
            or not isinstance(proof, (int, float))
            or not (0.0 < float(proof) <= 1.0)
        ):
            raise ValueError("barge_in_echo_proof_correlation 必须在 (0,1] 内")
        object.__setattr__(self, "barge_in_echo_proof_correlation", float(proof))
        cooldown = self.barge_in_cooldown_ms
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
            raise ValueError("barge_in_cooldown_ms 必须是非负整数毫秒（0 = 关闭）")
        mute_gap = self.post_speech_mute_ms
        if isinstance(mute_gap, bool) or not isinstance(mute_gap, int) or mute_gap < 0:
            raise ValueError("post_speech_mute_ms 必须是非负整数毫秒（0 = 关闭）")
        speed = self.tts_speed
        if speed is not None:
            if isinstance(speed, bool) or not isinstance(speed, (int, float)):
                raise ValueError("tts_speed 必须是数值或 None")
            from app.tts.private_selection import (  # noqa: PLC0415 - 轻量纯离线模块
                SPEED_MAX,
                SPEED_MIN,
            )

            if not (SPEED_MIN <= float(speed) <= SPEED_MAX):
                raise ValueError(
                    f"tts_speed 必须在 {SPEED_MIN}..{SPEED_MAX} 之间（None = 不覆盖）"
                )
            object.__setattr__(self, "tts_speed", float(speed))
        if self.aec_engine not in AEC_ENGINES:
            raise ValueError("aec_engine 只支持 nlms 或 webrtc")
        margin = self.aec_residual_gate_margin
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not math.isfinite(float(margin))
            or float(margin) < 1.0
        ):
            raise ValueError("aec_residual_gate_margin 必须是 ≥ 1.0 的数值（不得把门降到固定门以下）")
        object.__setattr__(self, "aec_residual_gate_margin", float(margin))
        # R4-B5：双讲档位只接受 off/facts/protect（与通道配置同源校验，避免两处漂移）。
        from app.audio.aec.double_talk import (  # noqa: PLC0415 - 轻量纯离线模块
            double_talk_config_ok,
        )

        if not double_talk_config_ok(self.double_talk_mode):
            raise ValueError("double_talk_mode 只支持 off/facts/protect")


class AutoVoiceError(RuntimeError):
    """真机试听环境不满足前置条件。"""


class ThresholdCalibrationError(AutoVoiceError):
    """环境自适应阈值校准被拒绝。

    这是 fail closed 的结果：调用方必须如实报错并以退出码 3 结束，**不得**静默回退到
    固定默认阈值假装校准成功。
    """


class VadUnavailableError(AutoVoiceError):
    """Silero VAD 不可用（模型缺失 / 依赖缺失 / 初始化失败 / 检测失败）。

    同样是 fail closed：调用方必须如实报错并以退出码 3 结束，**不得**静默回退到能量判定
    假装 VAD 可用（除非用户显式 ``--vad-mode energy``）。
    """


class WakeWordUnavailableError(AutoVoiceError):
    """唤醒词门禁不可用（模型缺失 / 依赖缺失 / 构造失败）。

    fail closed：调用方必须如实报错并以退出码 3 结束，**不得**静默退化成"无需唤醒词"。
    唤醒词只决定"是否开启新的输入会话"，不代表 ASR 可靠性，也不决定授权。
    """


class VoiceprintUnavailableError(AutoVoiceError):
    """声纹过滤不可用（模型缺失 / 档案缺失或非法 / 构造失败）。

    fail closed：用户显式要求 ``--voiceprint-mode filter`` 时，装不上就必须如实报错并以
    退出码 3 结束，**不得**静默变成"看起来过滤了其实没有"。
    """

@dataclass
class AutoTurnFacts:
    """一轮免按键语音的本地事实；`asr_text` 只用于显示，不落盘。"""

    index: int
    boundary: Any
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
    vad_hits: int = 0
    idle_seconds: float = 0.0
    barge_in: dict[str, Any] | None = None
    """本轮播报期的打断事实（只读、只含固定短代码与数值）；未启用/未播报为 ``None``。"""
    pre_roll: dict[str, Any] | None = None
    """前置缓冲音频的去向事实：是否保留、是否并入下一轮、如实原因码。"""
    device: dict[str, Any] | None = None
    # P10 · T1 批次 3：本轮任务事实（固定短码；`task_kind`/`task_status`/`task_reason`/`task_plan_hash`）。
    task: dict[str, Any] | None = None
    """本轮消费的设备策略决策（只读）；未启用为 ``None``。"""
    speech: dict[str, Any] | None = None
    """本轮播报的 **TTS 机械事实**（路由/段数/是否被有界收尾截断/错误类型名）。

    真机动机（R4-B，"说一半就停"）：`SessionStream` 的 `finish()` 之后紧跟 `close()`，
    会取消仍在播放的语音；旧事实里完全看不到，于是轮次一直显示"播完"。
    """
    key: dict[str, Any] | None = None
    """P10 · T1 #16：本轮**键盘动作**事实（`confirm`/`cancel`，以及是否因此打断了播报）。"""
    speech_mute: dict[str, Any] | None = None
    """P10 · T1 #13：本轮输入静默窗事实（是否启用/被静默的帧数/固定原因码）；关闭时为 ``None``。"""
    first_frame_ms: float | None = None
    """诊断（2026-10-14）：本轮 `start_turn()` 成功到**第一帧**的毫秒数；没收到帧为 ``None``。"""
    turn_gap_ms: float | None = None
    """诊断：上一轮结束到本轮 `run_turn` 开始的毫秒数（轮次间空档）；首轮为 ``None``。"""
    listen_ms: float | None = None
    """诊断：本轮收音（`start_turn`→`release_input`）的毫秒数；没走到收音为 ``None``。"""
    reply_ms: float | None = None
    """诊断：收音结束→本轮结束（ASR 解码 + 路由/装配 + 播报播放）的毫秒数。"""
    task_ms: float | None = None
    """诊断：任务轮里 `_handle_task_turn`（计划装配/派发）的毫秒数；非任务轮为 ``None``。"""
    reply_chars: int | None = None
    """诊断（2026-10-24）：本轮 Agent 回复的**字数**（不可播报时也如实给出）；无结果为 ``None``。"""
    speech_skip_reason: str | None = None
    """诊断（2026-10-24）：本轮**没有可播报文本**的固定短码；可播报时为 ``None``。

    真机动机：`text_only` 轮既没有 `speech_*` 事实、也没有回复文本（不可播报时
    `reply_text` 被置为 ``None``），旧事实里"路由没调 Agent"与"Agent 失败"完全同形。
    """
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
        """脱敏日志字段：只有计数、状态与原因码，不含识别文本/回复文本。"""

        barge_in = self.barge_in or {}
        device = self.device or {}
        speech = self.speech or {}
        task = self.task or {}
        key = self.key or {}
        speech_mute = self.speech_mute or {}
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
            "barge_in_enabled": bool(barge_in),
            "barge_in_confirmed": bool(barge_in.get("confirmed", False)),
            "barge_in_reason": barge_in.get("reason"),
            "barge_in_frames": barge_in.get("frames"),
            # R4-A 真机证据：回声是否"看得见"（对齐后的最大相关性）与安全门状态。
            "barge_in_echo_proven": barge_in.get("echo_proven"),
            "barge_in_max_echo_correlation": barge_in.get("max_echo_correlation"),
            "barge_in_echo_delay_ms": barge_in.get("echo_delay_ms"),
            "barge_in_alignment_estimates": barge_in.get("alignment_estimates"),
            "device_action": device.get("action"),
            # R4-B：本轮播报的 TTS 机械事实（只数值/固定码/异常类型名，不含文本）。
            "speech_route": speech.get("route"),
            "speech_text_length": speech.get("text_length"),
            "speech_played_segments": speech.get("played_segments"),
            "speech_completed_sentences": speech.get("completed_sentences"),
            "speech_finish_timed_out": speech.get("finish_timed_out"),
            "speech_late_events": speech.get("late_events"),
            "speech_dropped_pushes": speech.get("dropped_pushes"),
            "speech_cancelled": speech.get("cancelled"),
            "speech_error": speech.get("error"),
            "speech_error_code": speech.get("provider_error_code"),
            "speech_failed_sentence_index": speech.get("failed_sentence_index"),
            "speech_request_chars": speech.get("failed_request_chars"),
            "speech_sentences_total": speech.get("sentences_total"),
            "speech_sentences_synthesized": speech.get("sentences_synthesized"),
            "speech_sentences_played": speech.get("sentences_played"),
            # R4-B 段级事实（GitHub 调研：HA #182944「只播第一句」= 后续段在 provider 侧被拒）。
            # 生命周期事件**只允许扁平基础类型**（真机教训：tuple 会让整个免按键会话被判不可用）。
            "speech_provider_failures": (
                None
                if speech.get("provider_failure_codes") is None
                else "|".join(str(item) for item in speech.get("provider_failure_codes") or ())[:64]
            ),
            "speech_provider_failure_count": speech.get("provider_failure_count"),
            "speech_edge_failures": speech.get("edge_failures"),
            # R4-B 真机（"播完"其实是被静默丢弃）：会话越界过载 / finish 后仍挂着的句段数 /
            # 真正播出的字数——旧事实里完全不可见，于是"已被接受但没播"的段永远查不出来。
            "speech_overloaded": speech.get("overloaded"),
            "speech_late_event_codes": speech.get("late_event_codes"),
            "speech_pending_after_finish": speech.get("pending_sentences_after_finish"),
            "speech_played_chars": speech.get("played_chars"),
            # R4-B 追加（等待式背压 + 三计数）：已接受/已合成/已播出的独立计数与截断固定短码。
            # 全部只读、扁平基础类型；`speech_finish_timed_out` 为真即代表本次回复**不允许**
            # 被当作"完整播报"（`chat_loop._speak()` 会据此记 `speech_truncated`）。
            "speech_accepted_segments": speech.get("accepted_segments"),
            "speech_accepted_chars": speech.get("accepted_chars"),
            "speech_synthesized_segments": speech.get("synthesized_segments"),
            "speech_pending_segments": speech.get("pending_segments"),
            "speech_truncation_reason": (
                None
                if speech.get("truncation_reason") is None
                else str(speech.get("truncation_reason"))[:64]
            ),
            "speech_pushed_chars": speech.get("pushed_chars"),
            # P1 收尾进展事实（只读、扁平）：收尾实际等待秒数 / 判停滞次数 / 停滞时的最远进展码。
            # 旧事实里没有"这次收尾等了多久"，长回复中止时无法区分"等了很久才放弃"与
            # "根本没等到东西"；`speech_last_progress` 是固定短码，绝不含文本。
            "speech_drain_seconds": speech.get("drain_seconds"),
            "speech_stall_timeouts": speech.get("stall_timeouts"),
            "speech_last_progress": (
                None
                if speech.get("last_progress") is None
                else str(speech.get("last_progress"))[:64]
            ),
            # R4-B 追加取证（只读）：本条播报事实的单调 `record_index`，以及段在
            # "segmenter → pending 队列"之间每一步的段数/字数。真机 `voice_turn` 里
            # `speech_*` 到底属于哪条流由 `speech_record_index` 定案；全部为 str/int/None
            # （来源是 `voice_entry` 里的 `_flat_int`），绝不允许 tuple（真机教训：tuple 会让
            # 整个免按键会话被判不可用而降级为文字）。
            "speech_record_index": speech.get("record_index"),
            "speech_dequeued_segments": speech.get("dequeued_segments"),
            "speech_dequeued_chars": speech.get("dequeued_chars"),
            "speech_enqueued_segments": speech.get("enqueued_segments"),
            "speech_enqueued_chars": speech.get("enqueued_chars"),
            "speech_enqueue_dropped_segments": speech.get("enqueue_dropped_segments"),
            "speech_enqueue_dropped_chars": speech.get("enqueue_dropped_chars"),
            "speech_segmenter_buffered_chars": speech.get("segmenter_buffered_chars"),
            # P10 · T1 批次 3：本轮任务事实（全部固定短码/布尔/计数；不含路径与文本）。
            "task_kind": task.get("task_kind"),
            "task_status": task.get("task_status"),
            "task_reason": task.get("task_reason"),
            "task_plan_hash": task.get("task_plan_hash"),
            "task_count": task.get("count"),
            "task_spoken": task.get("spoken"),
            # #16：确认来源（asr / keyboard）——绝不把键盘事件伪装成识别文本。
            "task_confirm_source": task.get("task_confirm_source"),
            # P10 · T1 #16：键盘动作事实（confirm/cancel）与"是否因此打断了播报"。
            "key_action": key.get("action"),
            "speech_interrupted_by_confirm": key.get("interrupted_speech"),
            # P10 · T1 #13：输入静默窗事实（只读、扁平基础类型；0 = 关闭时全为 None/false）。
            "speech_mute_active": speech_mute.get("active"),
            "speech_mute_frames": speech_mute.get("muted_frames"),
            "speech_mute_reason": speech_mute.get("reason"),
            "speech_mute_ms": speech_mute.get("gap_ms"),
            # 诊断（2026-10-14）：轮次时间线 + 控制台按键流水（键码/来源/处置；不含任何正文）。
            "first_frame_ms": self.first_frame_ms,
            "turn_gap_ms": self.turn_gap_ms,
            "kbd_codes": key.get("codes"),
            # 诊断（2026-10-14 真机："说完话后 3.6~10.2 s 没反应"）阶段耗时拆分；播放时长看
            # 既有的 `speech_drain_seconds`。
            "listen_ms": self.listen_ms,
            "reply_ms": self.reply_ms,
            "task_ms": self.task_ms,
            # 诊断（2026-10-24，`text_only` 定性）：本轮回复字数 / 跳过播报的固定短码 /
            # 路由交接短码。三者都只有计数与固定码，**绝不含识别或回复正文**；全部为
            # str/int/None（生命周期事件只允许扁平基础类型，tuple 会让整个会话被判不可用）。
            "reply_chars": self.reply_chars,
            "speech_skip_reason": (
                None if self.speech_skip_reason is None else str(self.speech_skip_reason)[:64]
            ),
            "route": None if self.route is None else str(self.route)[:64],
        }


def _turn_idle_sleep_seconds(processed_frames: int) -> float:
    """本轮的轮询睡眠策略：泵到帧就不睡（先把积压追平），一帧都没有才让出 CPU 一帧时长。

    固定"每轮睡 20ms"会让消费者永久慢于采集（20ms 命中 + 任何开销 > 采集周期），积压
    只会单调增长并最终撑爆总线；这里让泵在有帧时立即重试，从而与采集同速、不积压。
    """

    return 0.0 if processed_frames > 0 else FRAME_MS / 1000

class _KeyboardCancellableSpeaker:
    """P10 · T1 #16：薄包装——只记住"当前播报流"，让键盘能立刻停掉剩余播报。

    对照 Pipecat `InterruptionFrame`：打断即"TTS 清缓冲、传输层丢弃未播音频"。
    这里**不改音频字节**（`push_text_chunk` 等一律原样透传，`start_stream()` 返回**原流对象**），
    只在外部请求时调用该流的 `cancel()`；不启用键盘（`key_source=None`）时**根本不装配**。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._stream: Any | None = None

    def start_stream(self, *args: Any, **kwargs: Any) -> Any:
        stream = self._inner.start_stream(*args, **kwargs)
        with self._lock:
            self._stream = stream
        return stream

    def cancel_active_speech(self) -> bool:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return False
        # 诚实性：已经播完的流**不**声称"被打断"，也不给它打上 `cancelled`（那会污染已完成的事实）。
        if not self._speech_in_flight(stream):
            return False
        cancel = getattr(stream, "cancel", None)
        if not callable(cancel):
            return False
        try:
            cancel()
            return True
        except Exception:  # noqa: BLE001 - 取消失败不得影响任务事实
            return False

    @staticmethod
    def _speech_in_flight(stream: Any) -> bool:
        """用流自己的机械事实判断"还有没有没播出去的内容"；读不到事实时按"可能还在播"处理。"""

        record = getattr(stream, "record", None)
        if not isinstance(record, dict) or not record:
            return True
        if record.get("cancelled") is True:
            return False
        if record.get("finish_timed_out") is True:
            return True
        played = record.get("played_chars")
        total = record.get("text_length")
        if type(played) is int and type(total) is int:
            return played < total
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _forwarding_vad(bus: Any) -> Any:
    """coordinator 自带的 VAD 工厂替身：只做帧连续性转发，不做结束判定。

    实际运行时由 ``AutoCaptureVoiceDriver`` 注入自己的观察 VAD（在 ``VadSession.process``
    之前观察每一帧）；这里保留一个等价实现，避免在 runner 里 import 音频内部类型。
    """

    from app.audio.vad import VadConfig, VadSession

    # 注：既有 VadSession 对 detector=None 会直接判 DETECTOR_UNAVAILABLE 并关闭总线，
    # 因此这里用恒真判定达到"只做连续性转发、静音永不达到阈值"的语义。
    return VadSession(
        bus=bus,
        config=VadConfig(silence_duration_ms=600000),
        detector=lambda packet: True,
    )


def _close_calibration_capture(capture: Any) -> None:
    """停止校准采样并 ``discard=True`` 清空总线残留；清理失败不改变已判定结果。"""

    stop = getattr(capture, "stop", None)
    if not callable(stop):
        return
    try:
        stop(reason=CALIBRATION_STOP_REASON, discard=True)
    except Exception:  # noqa: BLE001 - 采样资源清理尽力而为
        pass


def calibrate_speech_threshold(
    environment: Any,
    *,
    multiplier: float,
    config: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """在第一轮开始之前采样环境静音，返回 ``AdaptiveThresholdDecision``。

    直接使用 ``environment.capture_factory`` 的采集后端：自建校准总线（独立身份与容量），
    喂给 ``AdaptiveThresholdCalibrator``，收满采样窗口后停止采集并清空总线残留。任何
    不可用/被拒绝的情况都抛 ``ThresholdCalibrationError``（fail closed，不回退默认阈值）。
    """

    from app.audio.bus import AudioBus, AudioBusState
    from app.audio.vad.adaptive import (
        AdaptiveThresholdCalibrator,
        AdaptiveThresholdConfig,
        AdaptiveThresholdError,
    )

    if config is None:
        adaptive_config = AdaptiveThresholdConfig(multiplier=multiplier)
    elif isinstance(config, AdaptiveThresholdConfig):
        adaptive_config = config
    else:
        raise TypeError("config 必须是 AdaptiveThresholdConfig 或 None")

    factory = getattr(environment, "capture_factory", None)
    if not callable(factory):
        raise ThresholdCalibrationError("采集后端不可用：environment 没有可调用的 capture_factory")

    bus = AudioBus(
        audio_session_id=CALIBRATION_AUDIO_SESSION_ID,
        provider_generation=1,
        stream_id=CALIBRATION_STREAM_ID,
        device_epoch=1,
        capacity_frames=CALIBRATION_CAPACITY_FRAMES,
    )
    calibrator = AdaptiveThresholdCalibrator(config=adaptive_config)
    capture = factory(bus)
    started = capture.start()
    if not getattr(started, "started", False):
        raise ThresholdCalibrationError("采集后端不可用：无法开始静音采样")
    deadline = clock() + (
        adaptive_config.sample_duration_ms * CALIBRATION_DEADLINE_FACTOR
    ) / 1000.0
    try:
        while calibrator.is_collecting:
            packet = bus.read_nowait()
            if packet is not None:
                calibrator.observe(packet)
                continue
            if bus.state is not AudioBusState.OPEN:
                raise ThresholdCalibrationError("采样期间音频总线已关闭或失效")
            if clock() >= deadline:
                raise ThresholdCalibrationError(
                    "静音采样超时：只收到 "
                    f"{calibrator.frame_count}/{adaptive_config.required_frames} 帧"
                )
            sleep(FRAME_MS / 1000)
        try:
            return calibrator.decide()
        except AdaptiveThresholdError as exc:
            raise ThresholdCalibrationError(f"环境噪声采样被拒绝（{exc}）") from exc
    finally:
        _close_calibration_capture(capture)


def wait_for_wake_word(
    *,
    environment: Any,
    gate: Any,
    timeout_seconds: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """等待唤醒词命中：命中返回 ``True``，超时返回 ``False``。

    唤醒阶段使用**自有总线与采集**，音频只喂唤醒词门禁、**不进 ASR**——唤醒词只开启新的
    输入会话，绝不携带任何音频进识别、更不产生授权/计划/执行事实。采集不可用或门禁报错
    一律抛 :class:`WakeWordUnavailableError`（fail closed，绝不静默跳过门禁）。
    """

    from app.audio.bus import AudioBus
    from app.audio.kws import KeywordGateError

    factory = getattr(environment, "capture_factory", None)
    if not callable(factory):
        raise WakeWordUnavailableError("capture_unavailable")
    bus = AudioBus(
        audio_session_id=WAKE_AUDIO_SESSION_ID,
        provider_generation=1,
        stream_id=WAKE_STREAM_ID,
        device_epoch=1,
        capacity_frames=WAKE_CAPACITY_FRAMES,
    )
    capture = factory(bus)
    started = capture.start()
    if not getattr(started, "started", False):
        raise WakeWordUnavailableError("capture_unavailable")
    deadline = clock() + float(timeout_seconds)
    try:
        while True:
            packet = bus.read_nowait()
            if packet is not None:
                try:
                    hit = gate.observe(packet)
                except KeywordGateError as exc:
                    raise WakeWordUnavailableError(exc.code) from exc
                if hit:
                    return True
                continue
            if clock() >= deadline:
                return False
            sleep(FRAME_MS / 1000)
    finally:
        stop = getattr(capture, "stop", None)
        if callable(stop):
            try:
                stop(reason=WAKE_STOP_REASON, discard=True)
            except Exception:  # noqa: BLE001 - 唤醒阶段资源清理尽力而为
                pass


def load_voiceprint_profile(path: Path | None) -> list[list[float]]:
    """读取声纹档案：``{"speaker": "...", "embeddings": [[float, ...], ...]}``。

    档案里**只有嵌入向量、没有音频**；是否保存、保存在哪里由用户决定。任何结构问题
    （缺文件 / 非法 JSON / 空档案 / 非数值矩阵）一律 fail closed。
    """

    if path is None:
        raise VoiceprintUnavailableError("profile_missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        embeddings = data["embeddings"]
    except VoiceprintUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - 档案不可读/不可解析一律 fail closed
        raise VoiceprintUnavailableError("profile_invalid") from exc
    if not isinstance(embeddings, list) or not embeddings:
        raise VoiceprintUnavailableError("profile_empty")
    rows: list[list[float]] = []
    for row in embeddings:
        if (
            not isinstance(row, list)
            or not row
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in row)
        ):
            raise VoiceprintUnavailableError("profile_invalid")
        rows.append([float(value) for value in row])
    return rows


def _hud_set(hud: Any | None, state_name: str, *, turn_index: int | None = None) -> None:
    """把状态推到右下角提示窗；提示窗故障绝不影响语音链路。"""

    if hud is None:
        return
    try:
        from app.ui.voice_hud import VoiceHudState

        hud.set_state(getattr(VoiceHudState, state_name), turn_index=turn_index)
    except Exception:  # noqa: BLE001 - 提示窗只是展示，不得影响对话
        return


def _hud_tick(hud: Any | None) -> None:
    """非阻塞抽取一次窗口消息（拖动/重绘）；故障时静默停用，不影响对话。"""

    if hud is None:
        return
    try:
        hud.tick()
    except Exception:  # noqa: BLE001 - 展示层故障不得影响对话
        return


def _reference_matches_capture(reference: Any | None, capture_sample_rate: int) -> bool:
    """参考与采集是否**同钟**：不同钟时不许算相关性（也就谈不上消除回声）。"""

    if reference is None:
        return False
    pcm = getattr(reference, "pcm", None)
    rate = getattr(pcm, "sample_rate", None)
    return isinstance(rate, int) and int(rate) == int(capture_sample_rate)


def _sticky_window_observe(
    detector: Any,
    required_windows: int,
    on_hit: Callable[[], None] | None = None,
) -> Callable[[Any], bool]:
    """把 Silero 的**窗口级**结论粘滞到未满窗的采集帧（与主链路同口径）。

    真机教训（2026-10-02，第二轮）：Silero 按 512 样本整窗判定，而采集帧是 320 样本
    （约 1.6 帧/窗），原始 ``observe`` 只在"本帧凑满一个窗"时返回 True → 帧级真/假交替；
    任何"连续 N 帧"的要求（疑似最小帧数、确认窗口）都会**结构性不可达**：真机上表现为
    "音量会动、但永远不确认"。粘滞后窗口结论会延续到未满窗的帧。
    """

    raw = getattr(detector, "observe", None)
    if not callable(raw):
        raise TypeError("detector 必须提供可调用的 observe")
    if (
        isinstance(required_windows, bool)
        or not isinstance(required_windows, int)
        or required_windows < 1
    ):
        raise ValueError("required_windows 必须是正整数")
    consecutive = [0]

    def observe(packet: Any) -> bool:
        hit = bool(raw(packet))
        if hit:
            consecutive[0] += 1
            if on_hit is not None:
                on_hit()
        else:
            consecutive[0] = 0
        if consecutive[0] >= required_windows:
            return True
        # 窗口级已确认事实：未满窗的帧沿用它的结论（不会在帧级交替）。
        return bool(getattr(detector, "speech_started", False))

    observe.required_windows = required_windows  # type: ignore[attr-defined]
    observe.raw_detector = detector  # type: ignore[attr-defined]
    return observe


class _BargeInStreamProxy:
    """播报流代理：透传真实流，并在播报期启动独立打断通道的监视线程。

    - ``push_text_chunk`` 先启动监视再**原样透传**（不改变首字延迟语义）；
    - ``finish`` / ``cancel`` 透传后在 ``finally`` 里收尾监视（幂等、有界 join）；
    - 监视线程任何异常都在 :meth:`AutoVoiceSession._monitor_barge_in` 内部收尾，
      绝不影响播报事实。
    """

    def __init__(self, *, stream: Any, session: Any) -> None:
        self._stream = stream
        self._session = session
        self._playback_done = threading.Event()
        self._monitor: threading.Thread | None = None

    @property
    def real(self) -> Any:
        return self._stream

    @property
    def record(self) -> Any:
        return getattr(self._stream, "record", None)

    def push_text_chunk(self, text: str) -> None:
        self._start_monitor()
        push = getattr(self._stream, "push_text_chunk", None)
        if callable(push):
            push(text)

    def finish(self) -> Any:
        try:
            finish = getattr(self._stream, "finish", None)
            return finish() if callable(finish) else None
        finally:
            self._stop_monitor()

    def cancel(self) -> Any:
        try:
            cancel = getattr(self._stream, "cancel", None)
            return cancel() if callable(cancel) else None
        finally:
            self._stop_monitor()

    def _start_monitor(self) -> None:
        if self._monitor is not None:
            return
        self._monitor = threading.Thread(
            target=self._session._monitor_barge_in,
            args=(self._stream, self._playback_done),
            name="p10-barge-in-monitor",
            daemon=True,
        )
        self._monitor.start()

    def _stop_monitor(self, timeout: float = 2.0) -> None:
        self._playback_done.set()
        monitor = self._monitor
        self._monitor = None
        if monitor is None:
            return
        if monitor.is_alive() and monitor is not threading.current_thread():
            monitor.join(timeout=timeout)


class _BargeInSpeakerProxy:
    """speaker 代理：``start_stream()`` 返回流代理；其余属性（如 ``records``）原样透传。"""

    def __init__(self, *, speaker: Any, session: Any) -> None:
        self._speaker = speaker
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._speaker, name)

    def start_stream(self) -> Any:
        start = getattr(self._speaker, "start_stream", None)
        if not callable(start):
            return None
        stream = start()
        if stream is None:
            return None
        return _BargeInStreamProxy(stream=stream, session=self._session)


class AutoVoiceSession:
    """免按键自动收音会话：装配既有闭环 + 自动收音驱动。

    本类只编排输入边界与既有闭环，不决定授权、不接触任务/计划/执行事实。
    """

    def __init__(
        self,
        *,
        conversation: Any,
        environment: Any,
        config: AutoVoiceConfig,
        source_instance_id: str = "p9-auto-runner",
        notify: Callable[[str], None] | None = None,
    ) -> None:
        from app.asr.recovery import AsrRecoveryState
        from app.asr.push_to_talk import PushToTalkCoordinator
        from app.voice_loop import (
            ChatVoiceLoop,
            VoiceInputSeam,
            VoiceLoopStateMachine,
            VoiceTurnRegistry,
        )
        from app.voice_loop.auto_driver import AutoCaptureDriverConfig, AutoCaptureVoiceDriver

        if not isinstance(config, AutoVoiceConfig):
            raise TypeError("config 必须是 AutoVoiceConfig")
        if notify is not None and not callable(notify):
            raise TypeError("notify 必须可调用或 None")
        self._config = config
        self._notify_sink = notify
        # P10 · T1 批次 3：语音任务轮（待确认 → 确认执行 → 播报）。两者都给才生效。
        self._voice_task: Any | None = None
        self._task_spoken = False
        # P10 · T1 #16：键盘确认通道（Enter/空格=确认、Esc=取消）。默认 None = 完全不启用。
        self._key_source: Callable[[], str | None] | None = None
        self._key_action: str | None = None
        self._key_interrupted = False
        self._key_watch_stop: Any | None = None
        self._key_watch: Any | None = None
        self._key_speaker: Any | None = None
        if config.task_router is not None and config.task_dispatch is not None:
            from app.runtime.orchestration.voice_task_turn import VoiceTaskTurn

            self._voice_task = VoiceTaskTurn(
                router=config.task_router,
                dispatcher=config.task_dispatch,
                voice_lines=getattr(config, "voice_lines", None),
            )
        self._environment = environment
        self._wake_gate: Any | None = None
        # P9 B4：本轮最近若干秒 PCM（声纹判定用）；帧观察口只读、绝不参与判定。
        self._turn_pcm = bytearray()
        self._voiceprint: Any | None = None
        self._voiceprint_errors = 0
        self._last_speaker: dict[str, Any] | None = None
        self._vad_hits = 0
        # 只读事件回调（例如右下角提示窗）；异常一律不得影响语音链路。
        self._event_sink: Callable[[str], None] | None = None
        self._event_sink_failed = False
        # silero 模式：构造真机 Silero VAD 检测器，逐帧人声判定优先于能量阈值，且不跑能量
        # 校准；模型缺失 / 依赖缺失 / 初始化失败一律 fail closed（见 main 退出码 3）。
        threshold_decision: Any | None = None
        speech_threshold = config.speech_threshold
        speech_detector: Callable[[Any], bool] | None = None
        vad_detector: Any | None = None
        if config.vad_mode == "silero":
            from app.audio.vad.silero import (
                SileroVadConfig,
                SileroVadError,
                build_silero_vad_detector,
            )

            try:
                vad_detector = build_silero_vad_detector(
                    config=SileroVadConfig(
                        model_path=config.vad_model_path,
                        threshold=config.vad_threshold,
                        speech_frames_required=config.vad_frames,
                        # 待修 B：段级端点宽限——Silero 自带"人声段结束"判定所需的静音时长。
                        min_silence_duration=config.vad_min_silence_ms / 1000.0,
                    )
                )
            except SileroVadError as exc:
                # 只带固定短代码，不带异常正文/路径。
                raise VadUnavailableError(exc.code) from exc
            except Exception as exc:  # noqa: BLE001 - 未知装配失败同样 fail closed
                raise VadUnavailableError("init_failed") from exc
            # 逐帧统计"本帧是否发生人声命中"，只用于本地诊断（不参与任何判定）。
            # 关键接线：Silero VAD 按 512 样本整窗判定，而采集帧是 320 样本，窗口每 1.6 帧
            # 才凑齐一次；把「本帧是否完成了一次窗口判定」直接交给驱动做"连续命中计数"，
            # 会在帧级交替出现真/假，永远凑不满连续阈值（窗内人声连续 264 次也无济于事）。
            # 因此这里把窗口结论粘滞到后续未满窗的帧（vad_detector.speech_started 是窗口级
            # 已确认事实），并要求连续 required_windows 次窗口命中。
            raw_observe = vad_detector.observe
            consecutive_windows = [0]
            required_windows = max(1, int(config.vad_frames))

            def counting_observe(packet: Any, _raw: Any = raw_observe) -> bool:
                hit = bool(_raw(packet))
                if hit:
                    self._vad_hits += 1
                    consecutive_windows[0] += 1
                else:
                    consecutive_windows[0] = 0
                if consecutive_windows[0] >= required_windows:
                    return True
                # 未达阈值时，用窗口级已确认事实保持粘滞（未确认则为 False）。
                return bool(getattr(vad_detector, "speech_started", False))

            # 只读诊断：让调用方/测试能拿到被包装的真实检测器实例。
            counting_observe._p9_vad_detector = vad_detector  # type: ignore[attr-defined]
            speech_detector = counting_observe
        elif config.threshold_mode == "auto":
            # auto 模式：第一轮开始之前完成一次环境静音采样；失败即 fail closed。
            threshold_decision = calibrate_speech_threshold(
                environment, multiplier=config.threshold_multiplier
            )
            speech_threshold = float(threshold_decision.threshold_rms)
        # 待修 B：段级端点接线（能量模式与不带段级 API 的替身一律为 None，行为不变）。
        segment_probe = getattr(vad_detector, "segment_pending", None)
        segment_flush = getattr(vad_detector, "flush_segment", None)
        if not callable(segment_probe):
            segment_probe = None
        if not callable(segment_flush):
            segment_flush = None
        # P9 B3：唤醒词门禁（默认 off）。模型缺失/依赖缺失/构造失败一律 fail closed（退出码 3），
        # 绝不静默退化成"无需唤醒词"。
        if config.kws_mode == "gate":
            from app.audio.kws import KeywordGate, KeywordGateConfig, KeywordGateError

            try:
                self._wake_gate = KeywordGate(
                    config=KeywordGateConfig(
                        model_dir=config.kws_model_dir,
                        keywords_file=config.kws_keywords_file,
                        keyword=config.keyword,
                        keywords_threshold=config.kws_threshold,
                    )
                )
            except KeywordGateError as exc:
                raise WakeWordUnavailableError(exc.code) from exc
            except Exception as exc:  # noqa: BLE001 - 未知装配失败同样 fail closed
                raise WakeWordUnavailableError("init_failed") from exc
        # P9 B4：声纹误触发过滤（默认 off）。模型/档案缺失一律 fail closed（退出码 3），
        # 绝不静默变成"看起来过滤了其实没有"。
        if config.voiceprint_mode == "filter":
            from app.audio.voiceprint import VoiceprintConfig, VoiceprintError, VoiceprintFilter

            try:
                voiceprint = VoiceprintFilter(
                    config=VoiceprintConfig(
                        model_path=config.voiceprint_model_path,
                        threshold=config.voiceprint_threshold,
                    )
                )
                for embedding in load_voiceprint_profile(config.voiceprint_profile):
                    voiceprint.enroll(embedding)
            except VoiceprintError as exc:
                raise VoiceprintUnavailableError(exc.code) from exc
            except VoiceprintUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - 未知装配失败同样 fail closed
                raise VoiceprintUnavailableError("init_failed") from exc
            self._voiceprint = voiceprint
        # 不要注入朴素 SpeechStartDetector：那会用「无能量门槛」覆盖驱动的能量门控，
        # 使每一帧（含静音）都算命中，静音结束永远不触发。驱动会自建带能量门控的检测器。
        driver_config = AutoCaptureDriverConfig(
            silence_ms=config.silence_ms,
            speech_start_timeout_ms=config.speech_start_timeout_ms,
            max_utterance_ms=config.max_record_seconds * 1000,
            min_utterance_ms=config.min_utterance_ms,
            speech_threshold=speech_threshold,
            speech_detector=speech_detector,
            # Silero 路径：窗口级连续命中已在检测器内判定，帧级只需 1 次粘滞命中。
            speech_detector_frames_required=1 if speech_detector is not None else None,
            segment_probe=segment_probe,
            segment_flush=segment_flush,
            frame_sink=self._append_turn_pcm if self._voiceprint is not None else None,
            # P10 · T1 #13：播报后的输入静默窗（0 = 关闭，行为不变）。
            speech_mute_gap_ms=config.post_speech_mute_ms,
        )
        coordinator = PushToTalkCoordinator(
            conversation=conversation,
            provider=environment.provider,
            capture_factory=environment.capture_factory,
            # coordinator 自己的 VAD 只做帧连续性转发；自动结束由 P9 驱动判定。
            vad_factory=lambda bus: _forwarding_vad(bus),
            source_instance_id=source_instance_id,
            device_epoch=1,
            audio_capacity_frames=AUDIO_CAPACITY_FRAMES,
        )
        self._coordinator = coordinator
        self._driver = AutoCaptureVoiceDriver(
            coordinator=coordinator,
            config=driver_config,
        )
        self._threshold_decision = threshold_decision
        self._vad_detector = vad_detector
        # ---------- P10 R4：独立打断通道 / 安全门 / 设备策略（默认全关）----------
        self._barge_in_mode = config.barge_in_mode
        self._barge_in_stop_on_confirm = config.barge_in_mode == "confirm"
        self._barge_in_config = config.barge_in_config
        self._barge_in_channel: Any | None = None
        self._barge_in_gate: Any | None = None
        self._barge_in_degradation = "full"
        self._barge_in_plan: dict[str, Any] | None = None
        self._barge_in_last: dict[str, Any] | None = None
        self._barge_in_episodes = 0
        self._barge_in_confirmations = 0
        self._barge_in_suppressed = 0
        self._barge_in_unproven = 0
        self._barge_in_stopped_episode = False
        self._barge_in_errors = 0
        self._barge_in_unavailable: str | None = None
        self._barge_in_gain_sink: Callable[[float], None] | None = None
        self._barge_in_duck_available = False
        self._aec_engine_running = False
        self._last_speech_index = -1
        self._awaiting_confirmation = False
        self._pending_pre_roll = b""
        self._speaker_proxy: Any | None = None
        self._device_policy: Any | None = None
        self._device_last: dict[str, Any] | None = None
        self._device_attempted: set[str] = set()
        self._device_epochs: dict[str, int] = {"input": 1, "output": 1}
        self._text_fallback_reason: str | None = None
        registry = VoiceTurnRegistry(source_instance_id=source_instance_id)
        self._machine = VoiceLoopStateMachine(registry)
        self._seam = VoiceInputSeam(conversation=conversation, registry=registry)
        loop_speaker = self._build_barge_in(environment)
        self._build_device_policy(environment)
        self._loop = ChatVoiceLoop(
            registry=registry,
            machine=self._machine,
            seam=self._seam,
            driver=self._driver,
            speaker=loop_speaker,
        )
        self._turns = 0
        self._last_text: str | None = None
        # 诊断（2026-10-14）：轮次时间线（首帧延迟 / 轮次间空档）所需的本地时刻。
        self._turn_started_at: float | None = None
        self._turn_first_frame_at: float | None = None
        self._turn_gap_seconds: float | None = None
        self._last_turn_end: float | None = None
        # 诊断（2026-10-14）：阶段耗时（收音结束时刻 / 任务装配耗时）。
        self._turn_listen_end_at: float | None = None
        self._turn_task_ms: float | None = None
        self._closed = False
        coordinator.startup_succeeded()
        if coordinator.recovery.state is not AsrRecoveryState.READY:
            raise AutoVoiceError(
                "ASR 启动失败，已按设计降级为文字输入；请检查 sherpa-onnx 与模型路径"
            )
        self._seam.note_voice_ready()
        switched = self._seam.request_voice_switch(local_check_passed=True)
        if not switched.switched:
            raise AutoVoiceError(f"无法建立语音输入边界：{switched.reason}")

    # ------------------------------------------------- P10 R4：独立打断通道

    def _turn_facts(self, **kwargs: Any) -> AutoTurnFacts:
        """构造一轮事实，并统一附带"本轮播报的 TTS 机械事实"（只数值/固定码，不含文本）。"""

        kwargs.setdefault("key", self._take_key_facts())
        kwargs.setdefault("speech_mute", self._take_speech_mute_facts())
        kwargs.setdefault("first_frame_ms", self._turn_first_frame_ms())
        kwargs.setdefault("turn_gap_ms", self._turn_gap_ms())
        kwargs.setdefault("listen_ms", self._turn_listen_ms())
        kwargs.setdefault("reply_ms", self._turn_reply_ms())
        kwargs.setdefault("task_ms", getattr(self, "_turn_task_ms", None))
        # 诊断：`_turn_facts` 是每一轮唯一的收口点 ⇒ 在这里记录"本轮结束时刻"供下一轮算空档。
        self._last_turn_end = time.monotonic()
        return AutoTurnFacts(speech=self._take_speech_facts(), **kwargs)

    def _turn_first_frame_ms(self) -> float | None:
        """本轮 `start_turn()` 到第一帧的毫秒数（没收到帧 ⇒ ``None``）。"""

        started = getattr(self, "_turn_started_at", None)
        first = getattr(self, "_turn_first_frame_at", None)
        if started is None or first is None:
            return None
        return round(max(0.0, first - started) * 1000.0, 1)

    def _turn_gap_ms(self) -> float | None:
        """上一轮结束到本轮开始的毫秒数（轮次间空档；首轮为 ``None``）。"""

        gap = getattr(self, "_turn_gap_seconds", None)
        if gap is None:
            return None
        return round(max(0.0, gap) * 1000.0, 1)

    def _turn_listen_ms(self) -> float | None:
        """本轮收音时长（`start_turn` → `release_input`）的毫秒数。"""

        started = getattr(self, "_turn_started_at", None)
        listen_end = getattr(self, "_turn_listen_end_at", None)
        if started is None or listen_end is None:
            return None
        return round(max(0.0, listen_end - started) * 1000.0, 1)

    def _turn_reply_ms(self) -> float | None:
        """收音结束→本轮结束的毫秒数（ASR 解码 + 路由/装配 + 播报播放）。"""

        listen_end = getattr(self, "_turn_listen_end_at", None)
        if listen_end is None:
            return None
        return round(max(0.0, time.monotonic() - listen_end) * 1000.0, 1)

    def _take_key_events(self) -> str | None:
        """取注入按键源的**只读流水**（键码/来源/处置），压成一条短字符串；没有则 ``None``。

        值里不含任何正文，也刻意不出现 ``key`` 字样（结构化日志的密钥正则会把它整段脱敏）。
        """

        take = getattr(getattr(self, "_key_source", None), "take_events", None)
        if not callable(take):
            return None
        try:
            events = take()
        except Exception:  # noqa: BLE001 - 诊断失败不得影响轮次事实
            return None
        if not isinstance(events, list) or not events:
            return None
        parts: list[str] = []
        for item in events[:8]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "?"))[:8]
            code = str(item.get("code", "?"))[:8]
            action = str(item.get("action", "?"))[:16]
            parts.append(f"{source}:{code}:{action}")
        joined = "|".join(parts)[:160]
        return joined or None

    def _take_speech_mute_facts(self) -> dict[str, Any] | None:
        """P10 · T1 #13：取本轮输入静默窗事实；**关闭（gap=0）时恒为 ``None``**（零字段变化）。"""

        if self._config.post_speech_mute_ms <= 0:
            return None
        details = self._driver.mute_details()
        return details if isinstance(details, dict) else None

    def _begin_speech_mute(self) -> None:
        """播报**之前**开始静默（Pipecat `AlwaysUserMuteStrategy` 口径）；故障一律静默忽略。"""

        if self._config.post_speech_mute_ms <= 0:
            return
        begin = getattr(self._driver, "begin_speech_mute", None)
        if not callable(begin):
            return
        try:
            begin()
        except Exception:  # noqa: BLE001 - 附加输入保护故障不得影响播报/轮次事实
            return

    def _end_speech_mute(self) -> None:
        """播报结束后按 ``post_speech_mute_ms`` 起冷却窗；故障一律静默忽略。"""

        if self._config.post_speech_mute_ms <= 0:
            return
        end = getattr(self._driver, "end_speech_mute", None)
        if not callable(end):
            return
        try:
            end(now=time.monotonic())
        except Exception:  # noqa: BLE001 - 附加输入保护故障不得影响播报/轮次事实
            return

    def _take_key_facts(self) -> dict[str, Any] | None:
        """取本轮的键盘动作事实（`confirm`/`cancel` + 是否因此打断播报）；读过即清。

        诊断（2026-10-14）：同时带上按键流水（键码/来源/处置），用于定位"没按 Esc 却被取消"。
        """

        action = getattr(self, "_key_action", None)
        codes = self._take_key_events()
        if action is None and codes is None:
            return None
        facts: dict[str, Any] = {
            "action": action,
            "interrupted_speech": bool(self._key_interrupted),
        }
        if codes is not None:
            facts["codes"] = codes
        self._key_action = None
        self._key_interrupted = False
        return facts

    def _take_speech_facts(self) -> dict[str, Any] | None:
        """取**本轮**新产生的播报事实（按 speaker 记录的单调 ``index`` 判定，读过就不再报）。

        没有新记录（本轮没开播报流，例如 `text_only`）时返回 ``None``——绝不把上一轮的
        TTS 事实当成这一轮的。
        """

        speaker = getattr(self._environment, "speaker", None)
        records = getattr(speaker, "records", None)
        if not isinstance(records, list) or not records:
            return None
        last = records[-1]
        if not isinstance(last, dict):
            return None
        index = last.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index <= self._last_speech_index:
            return None
        self._last_speech_index = index
        late = last.get("late_events") or ()
        return {
            "route": last.get("route"),
            "text_length": last.get("text_length"),
            "played_segments": last.get("played_segments"),
            "completed_sentences": last.get("completed_sentences"),
            "finish_timed_out": last.get("finish_timed_out"),
            "late_events": len(late) if isinstance(late, (tuple, list)) else 0,
            "dropped_pushes": last.get("dropped_pushes"),
            "cancelled": bool(last.get("cancelled", False)),
            "error": last.get("error"),
            "provider_error_code": last.get("provider_error_code"),
            "failed_sentence_index": last.get("failed_sentence_index"),
            "failed_request_chars": last.get("failed_request_chars"),
            "sentences_total": last.get("sentences_total"),
            "sentences_synthesized": last.get("sentences_synthesized"),
            "sentences_played": last.get("sentences_played"),
            "provider_failure_codes": (
                None
                if last.get("provider_failure_codes") is None
                else "|".join(str(item) for item in last.get("provider_failure_codes") or ())[:64]
            ),
            "provider_failure_count": last.get("provider_failure_count"),
            "edge_failures": last.get("edge_failures"),
            # R4-B 真机："播完"其实可能是**已接受未播出**的段被静默丢弃（会话越界过载 +
            # `played == completed` 同源 + `error is None`）⇒ 必须把过载标志、finish 后仍挂着的
            # 句段数、以及真正播出的字数带进本轮事实；`late_events` 的**码**也要带上（旧代码只报
            # 个数，码丢失 ⇒ 真机上完全不可见）。全部只读，**不加任何等待、不改时序**。
            "overloaded": last.get("overloaded"),
            "late_event_codes": (
                None
                if last.get("late_events") is None
                else "|".join(str(item) for item in late)[:64]
                if isinstance(late, (tuple, list))
                else str(late)[:64]
            ),
            "pending_sentences_after_finish": last.get("pending_sentences_after_finish"),
            "played_chars": last.get("played_chars"),
            # R4-B 追加（等待式背压 + 三计数）：`accepted`（已入队）/`synthesized`（音频已生成）
            # /`played`（`played_segments`）三者独立，加上"推送字数"与截断固定短码 ⇒ 真机上
            # "报播完却少播"再也无法藏在 `played == completed` 的同源假象里。全部只读、
            # 不加任何等待、不改时序，且只允许扁平基础类型。
            "accepted_segments": last.get("accepted_segments"),
            "accepted_chars": last.get("accepted_chars"),
            "synthesized_segments": last.get("synthesized_segments"),
            "pending_segments": last.get("pending_segments"),
            "truncation_reason": last.get("truncation_reason"),
            "pushed_chars": last.get("pushed_characters"),
            # P1 收尾事实（2026-10-13 修复）：`drain_seconds` / `stall_timeouts` / `last_progress`
            # 由 `voice_entry.SessionStream._record_speech_facts()` 写进 record，但**本函数是显式
            # 白名单复制**——漏掉这三个键会让 P1 的事实永远为 None。真机证据：553 条 `voice_turn`
            # 里只有 auto_voice 的 P1 映射落地之后的最后 10 条出现该键，且 **10/10 全 None**，
            # 而 `streaming_session.finish()` 是无条件写 `_drain_seconds`、`_stall_timeouts` 初值为 0
            # ⇒ 事实是在这一层被丢掉的。只读、扁平；`log_details()` 负责把 `last_progress` 截到 ≤64。
            "drain_seconds": last.get("drain_seconds"),
            "stall_timeouts": last.get("stall_timeouts"),
            "last_progress": last.get("last_progress"),
            # R4-B 追加取证（同一批只读字段）：本条的**单调 record 序号** + 段在
            # "segmenter → pending 队列"之间每一步的段数/字数。真机那一轮
            # `pushed_chars=282 / accepted_segments=1 / truncation_reason=played_chars_below_pushed`
            # 互相矛盾，只有这几个数才能定案"段到底在哪一步消失"。全部只读、扁平基础类型、
            # 不加任何等待、不改时序；`record_index` 直接取 `start_stream()` 写的原值，语义不变。
            "record_index": index,
            "dequeued_segments": last.get("dequeued_segments"),
            "dequeued_chars": last.get("dequeued_chars"),
            "enqueued_segments": last.get("enqueued_segments"),
            "enqueued_chars": last.get("enqueued_chars"),
            "enqueue_dropped_segments": last.get("enqueue_dropped_segments"),
            "enqueue_dropped_chars": last.get("enqueue_dropped_chars"),
            "segmenter_buffered_chars": last.get("segmenter_buffered_chars"),
        }

    def _build_barge_in(self, environment: Any) -> Any:
        """装配独立打断通道 + 安全门 + R2a 降级；返回闭环应使用的 speaker。

        默认 ``barge_in_mode=off`` 时**零新增对象**，直接返回原 speaker（行为不变）。
        """

        speaker = getattr(environment, "speaker", None)
        if self._barge_in_mode == "off":
            return speaker
        from app.audio.aec import (
            CAPABILITY_SOFTWARE,
            CAPABILITY_UNAVAILABLE,
            AecCapabilityProbe,
            AecDegradationController,
        )
        from app.duplex.barge_in_channel import (
            CAPTURE_SAMPLE_RATE_HZ,
            PROBABILITY_MODE_DETECTOR,
            PROBABILITY_MODE_ENERGY,
            BargeInChannel,
            BargeInChannelConfig,
        )
        from app.duplex.barge_in_safety import BargeInSafetyGate

        start_stream = getattr(speaker, "start_stream", None)
        if speaker is None or not callable(start_stream):
            # 请求了打断但没有播报通道（例如 --tts-mode none）：如实记录，不假装启用。
            self._barge_in_unavailable = "no_playback_channel"
            return speaker
        reference = getattr(environment, "aec_reference", None)
        matches = _reference_matches_capture(reference, CAPTURE_SAMPLE_RATE_HZ)
        # R4-B P1：真 AEC 引擎是否**真的在跑**（配置要 webrtc + 参考源后端确实换成 webrtc
        # + 底层依赖可用）。只有真在跑才允许声称 `CAPABILITY_SOFTWARE`。
        engine_running = False
        if reference is not None and self._config.aec_engine == "webrtc":
            canceller = getattr(reference, "canceller", None)
            engine_running = bool(
                getattr(reference, "backend", "nlms") == "webrtc"
                and getattr(canceller, "available", False)
            )
        self._aec_engine_running = engine_running
        aec_capability = (
            CAPABILITY_SOFTWARE if (engine_running and matches) else CAPABILITY_UNAVAILABLE
        )
        # 未测得回声路径就不估计；参考不同钟/缺失一律按 R2a 如实退到 strict_no_aec。
        #
        # ⚠️ 诚实性：
        # - `nlms` 引擎下本会话**只把参考用于"对齐 + 相关性"**（不调用 `reference.process()`），
        #   因此能力一律报 `CAPABILITY_UNAVAILABLE` → R2a 给 `strict_no_aec`/`aec_applied=False`；
        # - `webrtc` 引擎且依赖可用时，AEC3 **确实逐帧在跑**（通道 `use_reference_aec`），
        #   此时才报 `CAPABILITY_SOFTWARE`；但"抑制够不够"另由通道的实测
        #   `aec_erle_db`/`aec_erle_sufficient` 与残余地板门如实暴露，不靠能力位断言。
        probe = AecCapabilityProbe(
            aec_capability=aec_capability,
            reference_available=reference is not None,
            reference_matches_output=matches,
            device_changed=False,
            estimated_echo_path_ms=0,
            device_epoch=1,
        )
        plan = AecDegradationController().plan(probe)
        self._barge_in_plan = plan.to_details()
        self._barge_in_degradation = plan.barge_in_degradation
        if not (probe.reference_available and probe.reference_matches_output):
            # fail-closed（真机第三轮结论）：没有可用回声参考 ⇒ 无法区分插话与自己的声音，
            # 整个打断功能不启用（播放期不守望输入），如实记录原因，绝不靠猜门槛硬上。
            self._barge_in_unavailable = NO_ECHO_REFERENCE_REASON
            return speaker
        if plan.pause_capture_during_playback:
            self._barge_in_unavailable = plan.reason
            return speaker
        detector = self._build_channel_detector()
        gain = getattr(environment, "playback_gain", None)
        gain_sink = getattr(gain, "set", None)
        self._barge_in_gain_sink = gain_sink if callable(gain_sink) else None
        self._barge_in_duck_available = self._barge_in_gain_sink is not None
        channel_kwargs: dict[str, Any] = {
            "degradation": self._barge_in_degradation,
            "max_monitor_seconds": self._config.barge_in_max_monitor_seconds,
            "energy_threshold": self._config.speech_threshold,
            "probability_mode": (
                PROBABILITY_MODE_DETECTOR if detector is not None else PROBABILITY_MODE_ENERGY
            ),
            "reference_sample_rate": CAPTURE_SAMPLE_RATE_HZ,
            "alignment_interval_frames": self._config.barge_in_alignment_interval_frames,
            "echo_proof_correlation": self._config.barge_in_echo_proof_correlation,
            # R4-B P1/P2：真 AEC 逐帧处理 + 残余回声地板感知动态门（仅引擎真在跑时启用）。
            "use_reference_aec": engine_running,
            # R4-B5（B2）：双讲只读事实（默认 off ⇒ 通道不建检测器、零开销、零行为差异）。
            "double_talk_mode": self._config.double_talk_mode,
            "double_talk_config": self._config.double_talk_config,
        }
        if engine_running:
            channel_kwargs["residual_gate_margin"] = self._config.aec_residual_gate_margin
        if self._config.barge_in_cooldown_ms > 0:
            # 方案 C：只覆盖冷却时长，**不重建** BargeInConfig（避免把会话级修复的
            # min_suspect_frames=3 / release_ms=800 覆盖回默认值——那是真机缺陷的来源）。
            channel_kwargs["cooldown_ms"] = self._config.barge_in_cooldown_ms
        if self._barge_in_config is not None:
            # 显式给了配置才覆盖；否则**沿用通道默认**（min_suspect_frames=3 / release_ms=800 等，
            # 这些正是真机缺陷修复的默认值，早期版本在这里误用裸 BargeInConfig() 把它们覆盖回 1/400）。
            channel_kwargs["barge_in"] = self._barge_in_config
        channel_config = BargeInChannelConfig(**channel_kwargs)
        self._barge_in_channel = BargeInChannel(
            environment=environment,
            config=channel_config,
            voice_detector=detector,
            reference=reference,
            gain_sink=self._barge_in_gain_sink,
        )
        self._barge_in_gate = BargeInSafetyGate()
        if not self._barge_in_duck_available and self._barge_in_mode in ("duck", "confirm"):
            self._barge_in_unavailable = DUCK_UNAVAILABLE_REASON
        self._speaker_proxy = _BargeInSpeakerProxy(speaker=speaker, session=self)
        return self._speaker_proxy

    def _build_channel_detector(self) -> Any | None:
        """打断通道**独立**的人声检测器（与主链路实例分开，避免状态互相污染）。

        silero 模式下与主链路同参数、同 fail-closed 语义；能量模式返回 ``None``
        （通道按 rms÷阈值 映射概率）。
        """

        if self._config.vad_mode != "silero":
            return None
        from app.audio.vad.silero import (
            SileroVadConfig,
            SileroVadError,
            build_silero_vad_detector,
        )

        try:
            detector = build_silero_vad_detector(
                config=SileroVadConfig(
                    model_path=self._config.vad_model_path,
                    threshold=self._config.vad_threshold,
                    speech_frames_required=self._config.vad_frames,
                    min_silence_duration=self._config.vad_min_silence_ms / 1000.0,
                )
            )
        except SileroVadError as exc:
            raise VadUnavailableError(exc.code) from exc
        except Exception as exc:  # noqa: BLE001 - 未知装配失败同样 fail closed
            raise VadUnavailableError("init_failed") from exc
        observe = getattr(detector, "observe", None)
        if not callable(observe):
            raise VadUnavailableError("init_failed")
        # 与主链路同口径：窗口结论粘滞到未满窗的帧，否则"连续 N 帧"在帧级真/假交替下不可达。
        return _sticky_window_observe(detector, max(1, int(self._config.vad_frames)))

    def _barge_in_prompt_kind(self) -> str:
        """当前播报是不是"安全确认提示"；聊天阶段恒为普通回复（不假装有安全提示）。"""

        from app.duplex.barge_in_safety import PROMPT_KIND_CHAT, PROMPT_KIND_SAFETY

        return PROMPT_KIND_SAFETY if self._awaiting_confirmation else PROMPT_KIND_CHAT

    def set_awaiting_confirmation(self, value: bool) -> None:
        """声明"当前处于安全确认等待中"（供将来的任务链接线；插话永不授权）。"""

        if not isinstance(value, bool):
            raise TypeError("value 必须是布尔值")
        self._awaiting_confirmation = value

    def _monitor_barge_in(self, stream: Any, playback_done: threading.Event) -> None:
        """监视线程主体：守望播报期的插话；异常一律收尾为"不打断"。

        本线程**只**影响"音量 / 是否停止本次播报 / 前置音频去向"，不产生授权，也不
        触碰会话轮次与安全链。安全门决定是否允许停止播报。
        """

        channel = self._barge_in_channel
        if channel is None:
            return
        self._barge_in_episodes += 1
        self._barge_in_last = None
        try:
            if not channel.arm():
                self._barge_in_last = {
                    "armed": False,
                    "reason": channel.reason,
                    "confirmed": False,
                    "frames": 0,
                    "level": 1.0,
                    "duck_available": self._barge_in_duck_available,
                }
                return
            try:
                outcome = channel.run(
                    is_playback_done=playback_done.is_set,
                    max_seconds=self._config.barge_in_max_monitor_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - 监视失败必须收尾为"不打断"
                self._barge_in_errors += 1
                self._barge_in_last = {
                    "armed": True,
                    "reason": "monitor_failed",
                    "error": type(exc).__name__,
                    "confirmed": False,
                    "frames": channel.frames,
                    "level": channel.level,
                }
                return
            confirmed = bool(outcome is not None and outcome.handoff_to_turn)
            decision = None
            suppressed = False
            unproven = False
            if confirmed:
                plan = self._barge_in_plan or {}
                decision = self._barge_in_gate.decide(
                    prompt_kind=self._barge_in_prompt_kind(),
                    awaiting_confirmation=self._awaiting_confirmation,
                    playback_active=True,
                    channel_available=True,
                    realtime_available=bool(plan.get("realtime_conversation_available", True)),
                )
                if decision.stop_playback and self._barge_in_stop_on_confirm:
                    if not channel.echo_proven:
                        # R4-A 安全门：还没证明"自己的回声可被观测"之前只观测不动手——
                        # 真机第三轮 6/6 假确认就是这么来的（无法区分插话与自己的声音）。
                        unproven = True
                        self._barge_in_unproven += 1
                    else:
                        self._stop_barge_in_playback(stream)
                        self._barge_in_stopped_episode = True
                        if decision.handoff_pre_roll:
                            self._pending_pre_roll = channel.pre_roll_pcm()
                            self._barge_in_confirmations += 1
                else:
                    # duck 模式（或安全门不允许停播）：确认不生效，恢复原音量并如实记录。
                    suppressed = True
                    self._barge_in_suppressed += 1
            facts = channel.to_details()
            facts["confirmed"] = confirmed
            facts["duck_available"] = self._barge_in_duck_available
            if unproven:
                facts["reason"] = BARGE_IN_UNPROVEN_REASON
            elif suppressed:
                facts["reason"] = CONFIRM_SUPPRESSED_REASON
            facts["decision"] = None if decision is None else decision.to_details()
            self._barge_in_last = facts
        finally:
            try:
                channel.disarm()
            except Exception:  # noqa: BLE001 - 通道收尾失败只计数
                self._barge_in_errors += 1
            self._restore_gain()

    def _stop_barge_in_playback(self, stream: Any) -> None:
        """停止本次播报：清空未播音频队列、取消合成、旧回复不再续提交（由流实现保证）。"""

        cancel = getattr(stream, "cancel", None)
        if not callable(cancel):
            return
        try:
            cancel()
        except Exception:  # noqa: BLE001 - 停播失败只计数，不改变已记录事实
            self._barge_in_errors += 1

    def _restore_gain(self) -> None:
        sink = self._barge_in_gain_sink
        if sink is None:
            return
        try:
            sink(1.0)
        except Exception:  # noqa: BLE001 - 音量恢复失败只计数
            self._barge_in_errors += 1

    def barge_in_details(self) -> dict[str, Any]:
        """打断通道的只读事实（状态/帧数/级别/降级/参考状态）；不含音频与正文。"""

        return {
            "mode": self._barge_in_mode,
            "enabled": self._barge_in_channel is not None,
            "unavailable_reason": self._barge_in_unavailable,
            "degradation": self._barge_in_degradation,
            "aec_plan": dict(self._barge_in_plan) if self._barge_in_plan else None,
            "duck_available": self._barge_in_duck_available,
            "awaiting_confirmation": self._awaiting_confirmation,
            "episodes": self._barge_in_episodes,
            "confirmations": self._barge_in_confirmations,
            "suppressed_confirmations": self._barge_in_suppressed,
            "unproven_confirmations": self._barge_in_unproven,
            "errors": self._barge_in_errors,
            "last": dict(self._barge_in_last) if self._barge_in_last else None,
            "pending_pre_roll_frames": len(self._pending_pre_roll) // 640,
            "pending_pre_roll_ms": (len(self._pending_pre_roll) // 640) * FRAME_MS,
            "pre_roll_merged": False,
            # 方案 C：冷却窗口事实（0 / None / 0 表示未启用或未发生）。
            "cooldown_ms": self._config.barge_in_cooldown_ms,
            "cooldown_remaining_ms": (
                self._barge_in_channel.cooldown_remaining_ms
                if self._barge_in_channel is not None
                else None
            ),
            "cooldown_frames": (
                self._barge_in_channel.cooldown_frames
                if self._barge_in_channel is not None
                else 0
            ),
            # R4-B P1/P2：真 AEC 与动态门的只读事实（引擎未跑时全部为默认/None）。
            "aec_engine_requested": self._config.aec_engine,
            "aec_engine_running": self._aec_engine_running,
            "aec_erle_db": (
                self._barge_in_channel.aec_erle_db if self._barge_in_channel is not None else None
            ),
            "aec_erle_sufficient": (
                self._barge_in_channel.aec_erle_sufficient
                if self._barge_in_channel is not None
                else False
            ),
            "aec_frames_applied": (
                self._barge_in_channel.aec_frames_applied
                if self._barge_in_channel is not None
                else 0
            ),
            "aec_fallback_reason": (
                self._barge_in_channel.aec_fallback_reason or None
                if self._barge_in_channel is not None
                else None
            ),
            "aec_stream_delay_ms": (
                self._barge_in_channel.aec_stream_delay_ms
                if self._barge_in_channel is not None
                else None
            ),
            "residual_echo_floor_pcm16": (
                self._barge_in_channel.residual_echo_floor_pcm16
                if self._barge_in_channel is not None
                else None
            ),
            "gate_in_playback": (
                self._barge_in_channel.gate_in_playback
                if self._barge_in_channel is not None
                else None
            ),
        }

    def pending_pre_roll_pcm(self) -> bytes:
        """确认插话后保留的前置音频（只读）；**本批未**并入下一轮 ASR（见模块说明）。"""

        return bytes(self._pending_pre_roll)

    def take_pending_pre_roll(self) -> bytes:
        """取出并清空前置音频（供将来 audio 层的 pre-roll 注入接口使用）。"""

        data = bytes(self._pending_pre_roll)
        self._pending_pre_roll = b""
        return data

    # ------------------------------------------------- P10 R4：设备策略（R3）

    def _build_device_policy(self, environment: Any) -> None:
        if self._config.device_recovery_mode == "off":
            return
        from app.devices import DeviceHealth, DeviceKind, DeviceObservation, DeviceRecoveryPolicy

        details = getattr(environment, "details", None)
        device_id = None
        if self._config.device_id:
            device_id = self._config.device_id
        elif isinstance(details, dict):
            device_id = details.get("device_id")
        self._device_policy = DeviceRecoveryPolicy(input_device_id=device_id)
        if device_id is not None:
            # 采集设备在装配阶段已通过 check_device（READY 是已核实事实，不是猜测）。
            self._device_policy.register(
                DeviceObservation(DeviceKind.INPUT, DeviceHealth.READY, device_id, 1)
            )
        else:
            self._device_policy.register(
                DeviceObservation(DeviceKind.INPUT, DeviceHealth.READY, None, 1)
            )

    def _device_safe_point(
        self,
        *,
        turn_active: bool,
        service_active: bool = False,
    ) -> dict[str, Any] | None:
        """在安全空闲点取一次设备决策并各试一次重连；不做任何越权动作。"""

        policy = self._device_policy
        if policy is None:
            return None
        from app.devices import (
            ACTION_FALLBACK_TO_TEXT,
            ACTION_NOTIFY_USER,
            ACTION_RECONNECT_INPUT,
            ACTION_RECONNECT_OUTPUT,
            DeviceHealth,
            DeviceKind,
        )

        for kind in (DeviceKind.INPUT, DeviceKind.OUTPUT):
            health = policy.health(kind)
            if health is DeviceHealth.READY:
                self._device_attempted.discard(kind.value)
        if policy.health(DeviceKind.OUTPUT) is DeviceHealth.UNKNOWN:
            # 还没有任何"输出是否可用"的证据：不猜、不重连，也不谎报要回退文字。
            self._device_last = {"action": "skipped", "reason": "output_health_unknown"}
            return self._device_last
        try:
            action = policy.decide(
                turn_active=turn_active,
                service_active=service_active,
                awaiting_confirmation=self._awaiting_confirmation,
            )
        except Exception as exc:  # noqa: BLE001 - 决策失败只记录，绝不影响语音
            self._device_last = {"action": "error", "error": type(exc).__name__}
            return self._device_last
        facts = action.to_details()
        if action.action in (ACTION_RECONNECT_INPUT, ACTION_RECONNECT_OUTPUT) and (
            action.open_request is not None
        ):
            kind = action.open_request.kind
            if kind.value in self._device_attempted:
                facts["action"] = "deferred"
                facts["reason"] = "already_attempted_this_episode"
            else:
                self._device_attempted.add(kind.value)
                succeeded = self._attempt_device_open(kind)
                policy.note_attempt(kind, succeeded=succeeded)
                facts["attempted"] = True
                facts["attempt_succeeded"] = succeeded
                if succeeded:
                    self._device_epochs[kind.value] = policy.epoch(kind)
                    self._device_attempted.discard(kind.value)
        if action.action == ACTION_FALLBACK_TO_TEXT:
            self._text_fallback_reason = action.reason
        elif action.action == ACTION_NOTIFY_USER:
            self._text_fallback_reason = self._text_fallback_reason or action.reason
        else:
            self._text_fallback_reason = None
        self._device_last = facts
        return facts

    def _attempt_device_open(self, kind: Any) -> bool:
        """按设备类型各试一次重连（只做一次有界探测，绝不动别的设备）。"""

        from app.devices import DeviceKind

        if kind is DeviceKind.OUTPUT:
            speaker = getattr(self._environment, "speaker", None)
            start_stream = getattr(speaker, "start_stream", None)
            if not callable(start_stream):
                return False
            stream = None
            try:
                stream = start_stream()
                return stream is not None
            except Exception:  # noqa: BLE001 - 探测失败即"重连失败"，不重试
                return False
            finally:
                cancel = getattr(stream, "cancel", None)
                if callable(cancel):
                    try:
                        cancel()
                    except Exception:  # noqa: BLE001 - 探测收尾失败不影响结论
                        pass
        factory = getattr(self._environment, "capture_factory", None)
        if not callable(factory):
            return False
        bus: Any | None = None
        capture: Any | None = None
        try:
            from app.audio.bus import AudioBus

            bus = AudioBus(
                audio_session_id=DEVICE_PROBE_AUDIO_SESSION_ID,
                provider_generation=1,
                stream_id=DEVICE_PROBE_STREAM_ID,
                device_epoch=self._device_epochs.get("input", 1),
                capacity_frames=DEVICE_PROBE_CAPACITY_FRAMES,
            )
            capture = factory(bus)
            started = capture.start()
            return bool(getattr(started, "started", False))
        except Exception:  # noqa: BLE001 - 探测失败即"重连失败"，不重试
            return False
        finally:
            stop = getattr(capture, "stop", None)
            if callable(stop):
                try:
                    stop(reason=DEVICE_PROBE_STOP_REASON, discard=True)
                except Exception:  # noqa: BLE001 - 探测收尾失败不影响结论
                    pass
            close = getattr(bus, "close", None)
            if callable(close):
                try:
                    close(reason=DEVICE_PROBE_STOP_REASON, discard=True)
                except Exception:  # noqa: BLE001 - 探测收尾失败不影响结论
                    pass

    def _observe_device(
        self,
        kind_name: str,
        health_name: str,
        *,
        device_id: object = None,
    ) -> None:
        """登记一次设备健康观察（只登记事实；不自动采用未知设备）。"""

        policy = self._device_policy
        if policy is None:
            return
        from app.devices import DeviceHealth, DeviceKind, DeviceObservation

        kind = DeviceKind.INPUT if kind_name == "input" else DeviceKind.OUTPUT
        health = DeviceHealth.__members__[health_name.upper()]
        if policy.health(kind) is health:
            # 健康状态没有变化：不制造新事实，也不虚增 epoch。
            return
        self._device_epochs[kind.value] = self._device_epochs.get(kind.value, 1) + 1
        if health is DeviceHealth.DISCONNECTED:
            self._device_attempted.discard(kind.value)
        try:
            policy.register(
                DeviceObservation(kind, health, device_id, self._device_epochs[kind.value])
            )
        except Exception:  # noqa: BLE001 - 登记失败绝不影响语音
            return

    def device_details(self) -> dict[str, Any]:
        """设备策略的只读事实（健康/epoch/尝试次数/文字模式）；未启用时只有 mode。"""

        details: dict[str, Any] = {
            "mode": self._config.device_recovery_mode,
            "enabled": self._device_policy is not None,
            "text_fallback_reason": self._text_fallback_reason,
            "last": dict(self._device_last) if self._device_last else None,
        }
        if self._device_policy is not None:
            details.update(self._device_policy.to_details())
        return details

    @property
    def state(self) -> Any:
        return self._loop.state

    @property
    def turn_count(self) -> int:
        return self._turns

    @property
    def last_recognized_text(self) -> str | None:
        return self._last_text

    @property
    def threshold_decision(self) -> Any | None:
        """本会话使用的自适应阈值决策；``fixed`` 模式为 ``None``（只读诊断）。"""

        return self._threshold_decision

    def threshold_details(self) -> dict[str, Any]:
        """校准事实（阈值、来源、噪声分位数）；校准音频本身绝不在此出现。"""

        decision = self._threshold_decision
        return {} if decision is None else dict(decision.to_details())

    @property
    def vad_mode(self) -> str:
        """本会话使用的 VAD 模式：``energy`` / ``silero``（只读诊断）。"""

        return self._config.vad_mode

    def set_event_sink(self, sink: Callable[[str], None] | None) -> None:
        """注册只读阶段事件回调（如右下角提示窗）；抛错会被隔离，不影响语音链路。"""

        if sink is not None and not callable(sink):
            raise TypeError("sink 必须可调用或 None")
        self._event_sink = sink
        self._event_sink_failed = False

    def _emit(self, phase: str) -> None:
        sink = self._event_sink
        if sink is None or self._event_sink_failed:
            return
        try:
            sink(phase)
        except Exception:  # noqa: BLE001 - 展示层故障不得影响对话
            self._event_sink_failed = True

    def _print(self, text: str) -> None:
        """可选的本地提示输出（构造时注入 ``notify``）；回调抛错一律隔离。"""

        sink = self._notify_sink
        if sink is None:
            return
        try:
            sink(text)
        except Exception:  # noqa: BLE001 - 展示层故障不得影响对话
            return

    @property
    def vad_detector(self) -> Any | None:
        """silero 模式下已构造的 Silero 检测器；``energy`` 模式为 ``None``（只读诊断）。"""

        return self._vad_detector

    def vad_details(self) -> dict[str, Any]:
        """Silero 检测器的机械事实（模型/阈值/窗口/采样率）；``energy`` 模式为空。"""

        detector = self._vad_detector
        to_details = getattr(detector, "to_details", None)
        if not callable(to_details):
            return {}
        details = to_details()
        return dict(details) if isinstance(details, dict) else {}

    def observation_details(self) -> dict[str, Any]:
        """本轮结束判定 + 声纹判定的只读时间线；无驱动时也返回声纹事实。"""

        getter = getattr(self._driver, "observation_details", None)
        details: dict[str, Any] = {}
        if callable(getter):
            raw = getter()
            if isinstance(raw, dict):
                details = dict(raw)
        # 每轮声纹判定（score/reason）落 artifact：调阈值靠它，而不是靠观感。
        details["speaker_verification"] = (
            dict(self._last_speaker) if self._last_speaker is not None else None
        )
        return details

    def voiceprint_details(self) -> dict[str, Any]:
        """声纹过滤的只读事实（是否启用、已注册数、阈值、错误计数）。"""

        details: dict[str, Any] = {
            "mode": self._config.voiceprint_mode,
            "enabled": self._voiceprint is not None,
            "threshold": self._config.voiceprint_threshold,
            "profile": str(self._config.voiceprint_profile) if self._config.voiceprint_profile else None,
            "errors": self._voiceprint_errors,
        }
        if self._voiceprint is not None:
            details["enrolled_speakers"] = self._voiceprint.enrolled_speakers
            details["verified_count"] = self._voiceprint.verified_count
            details["rejected_count"] = self._voiceprint.rejected_count
        return details

    def _append_turn_pcm(self, packet: Any) -> None:
        """只读帧观察口：只保留本轮最近 ``voiceprint_seconds`` 秒 PCM（不参与任何判定）。"""

        payload = getattr(packet, "payload", None)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return
        self._turn_pcm.extend(bytes(payload))
        limit = max(1, int(self._config.voiceprint_seconds)) * 16000 * 2
        excess = len(self._turn_pcm) - limit
        if excess > 0:
            del self._turn_pcm[:excess]

    def _verify_speaker(self) -> tuple[bool, str]:
        """声纹误触发过滤：返回 ``(是否丢弃本轮, 原因码)``。

        - 这一轮没有任何音频 → **不丢弃**（绝不凭缺失证据丢弃）；
        - 已注册但不匹配 → 丢弃（误触发）；
        - 过滤链路报错或未注册 → **不丢弃**，只累加只读错误计数：它不决定授权，
          也绝不因为故障而放行任何计划/执行事实。
        """

        from app.audio.voiceprint import VoiceprintError

        voiceprint = self._voiceprint
        payload = bytes(self._turn_pcm)
        self._turn_pcm.clear()
        if voiceprint is None:
            return False, "disabled"
        if not payload:
            return False, "no_audio"
        try:
            decision = voiceprint.verify_from_pcm(payload)
        except VoiceprintError as exc:
            self._voiceprint_errors += 1
            self._last_speaker = {"matched": False, "score": 0.0, "reason": f"unavailable:{exc}"}
            return False, f"unavailable:{exc}"
        self._last_speaker = decision.to_details()
        if decision.reason == "not_enrolled":
            self._voiceprint_errors += 1
            return False, decision.reason
        return (not decision.matched), decision.reason

    def _reset_speech_detector(self) -> None:
        """每轮开始前清空底层 VAD 内部状态（含 Silero 的"已结束段"队列）。

        真机缺陷（2026-10-01）：Silero 的已结束段一旦产出就留在队列里（本项目从不 pop），
        于是第 2 轮第 1 帧就被 `segment_pending()` 判成"说完了"——实测 `frames=2`、
        识别 `'。'`，却仍然提交并播报。逐轮重置即可清除该残留。

        注入替身没有 ``reset`` 时是安全空操作；重置失败按 ``init_failed`` fail closed。
        """

        detector = self._vad_detector
        reset = getattr(detector, "reset", None)
        if not callable(reset):
            return
        try:
            reset()
        except Exception as exc:  # noqa: BLE001 - VAD 复位失败必须 fail closed
            raise VadUnavailableError("init_failed") from exc

    def _complete_turn_with_hud(self, hud: Any | None, index: int, *, reply_provider: Any = None) -> Any:
        """等 Agent 往返 + 播报完成，并在**播报期间**把提示窗切到"播报"。

        真机事实（2026-10-09，`--voice --auto`）：`STATE_LABELS` 只把 `SPEAKING` 映射成
        "播报"（`TRANSCRIBING`/`THINKING`/`HEARING` 都渲染成"聆听"），而这条路径此前只推
        `listening`/`hearing` 两个阶段，`main.py` 又是在整轮结束**之后**才设 `SPEAKING`
        ⇒ 用户看到的是"播报时窗口一直停在聆听，音频播完才闪一下播报"。

        这里只在提示窗存在时把真实 `_speak` **包一层**：播放前推 `SPEAKING`，播放后回
        `LISTENING`；`_speak` 的返回值与异常原样透传，播报/打断/取消语义一个字符都不改。
        提示窗自身的故障由 `_hud_set`/`_hud_tick` 各自隔离（展示层不得影响对话）。

        `reply_provider`（P10 · T1 批次 3）原样透传给 `complete_turn`：任务路径的计划/结果
        必须在**本轮 REPLYING 状态内**播报，否则会撞上"空闲态不许进 SPEAKING"的非法迁移。
        """

        loop = self._loop
        real_speak = getattr(loop, "_speak", None)
        # #16：启用键盘通道时把 loop 的 speaker 包一层，好让"确认键"能立刻停掉剩余播报。
        wrapper: Any | None = None
        speaker_before: Any | None = None
        if getattr(self, "_key_source", None) is not None:
            speaker_before = getattr(loop, "_speaker", None)
            if speaker_before is not None:
                wrapper = _KeyboardCancellableSpeaker(speaker_before)
                self._key_speaker = wrapper
                loop._speaker = wrapper  # noqa: SLF001 - 只做可取消句柄，音频字节原样透传
        if hud is None or not callable(real_speak):
            # 未接线任务时**不传**新关键字参数，保持与既有替身/旧签名完全兼容。
            try:
                if reply_provider is None:
                    return loop.complete_turn()
                return loop.complete_turn(reply_provider=reply_provider)
            finally:
                if wrapper is not None:
                    loop._speaker = speaker_before  # noqa: SLF001
                    self._key_speaker = None

        def speak_with_hud(text: str) -> Any:
            _hud_set(hud, "SPEAKING", turn_index=index)
            _hud_tick(hud)
            try:
                return real_speak(text)
            finally:
                # 播报结束（成功、失败或被打断取消）后回到"聆听"：这是下一轮收音的真实状态。
                _hud_set(hud, "LISTENING", turn_index=index)
                _hud_tick(hud)

        loop._speak = speak_with_hud  # noqa: SLF001 - 只读接线，不改变播报语义
        try:
            if reply_provider is None:
                return loop.complete_turn()
            return loop.complete_turn(reply_provider=reply_provider)
        finally:
            # 只拆掉"仍是我们装上的那一个"，绝不误删别处的接线或真实实现。
            if vars(loop).get("_speak") is speak_with_hud:
                del vars(loop)["_speak"]
            if wrapper is not None:
                loop._speaker = speaker_before  # noqa: SLF001
                self._key_speaker = None

    # ---- P10 · T1 批次 3：语音任务执行 -------------------------------------

    def _expire_task_turn(self, hud: Any | None) -> Any | None:
        """待确认窗口已过 ⇒ 作废 + 播报"已取消"（在任何收音之前调用，fail closed）。"""

        task = self._voice_task
        if task is None:
            return None
        try:
            outcome = task.expire()
        except Exception:  # noqa: BLE001 - 任务层故障不得影响语音链路
            return None
        if outcome is None:
            return None
        self._speak_task_notice(outcome, hud)
        return outcome

    def _handle_task_turn(self, hud: Any | None) -> list[Any]:
        """本轮的交接若是任务路由，就推进任务状态机（**不在这里播报**）。

        真机教训（2026-10-13）：播报必须发生在**本轮 REPLYING 状态内**（由 `complete_turn`
        的 `reply_provider` 提供文本），否则 `_speak` 会撞上"空闲态不许进 SPEAKING"的非法迁移 ——
        真机表现就是"计划/结果只打印、一个字都没念"。
        """

        task = self._voice_task
        if task is None:
            return []
        # 只有本轮**真的有被接受的识别文本**才处理：否则上一轮残留的 `last_handoff`
        # 会被当成这一轮的路由结果（会重复播报计划，甚至把已确认的任务再跑一次）。
        last = self._driver.last_result
        text = getattr(last, "accepted_text", None)
        if not isinstance(text, str) or not text.strip():
            return []
        handoff = getattr(self._config.task_router, "last_handoff", None)
        if handoff is None:
            return []
        try:
            return list(task.handle(handoff))
        except Exception:  # noqa: BLE001 - 任务层故障不得影响语音链路
            return []

    def _task_reply_provider(self, hud: Any | None) -> Callable[[], str | None]:
        """给 `complete_turn` 的回复提供者：本轮任务文本（多段用换行连接）或 None。"""

        def provide() -> str | None:
            task_started = time.monotonic()
            outcomes = self._handle_task_turn(hud)
            # 诊断：任务装配（计划冻结/派发）耗时。
            self._turn_task_ms = round((time.monotonic() - task_started) * 1000.0, 1)
            self._task_outcomes = outcomes
            for outcome in outcomes:
                # 控制台看**完整**文本（工具名/L2/路径都在里面）；语音只念短文本（#14）。
                text = (
                    getattr(outcome, "console_text", None)
                    or getattr(outcome, "display_text", None)
                    or getattr(outcome, "spoken_text", None)
                )
                if isinstance(text, str) and text.strip():
                    self._print(text)
            if not outcomes and self._is_stray_confirmation():
                # 2026-10-23 真机：待确认窗口已过期后用户再说「确认」——
                # 既不执行、也不该装没听见，更不该把这句发给 Provider 换来一个无关回答。
                from app.runtime.orchestration.task_dispatch import NO_PENDING_TASK_TEXT

                self._print(NO_PENDING_TASK_TEXT)
                return NO_PENDING_TASK_TEXT
            return self._task_reply_text(outcomes)

        return provide

    def _is_stray_confirmation(self) -> bool:
        """本轮是「确认」，但**没有**待确认计划（超时作废之后再补一句「确认」的典型情形）。"""

        router = getattr(self._config, "task_router", None)
        if router is None or not hasattr(router, "pending"):
            return False
        try:
            if getattr(router, "pending", None) is not None:
                return False
        except Exception:  # noqa: BLE001 - 取不到状态就按"不干预"处理
            return False
        last = getattr(self._driver, "last_result", None)
        text = getattr(last, "accepted_text", None)
        if not isinstance(text, str):
            return False
        from app.runtime.orchestration.task_dispatch import is_confirmation

        return is_confirmation(text)

    @staticmethod
    def _task_reply_text(outcomes: list[Any]) -> str | None:
        parts = [
            text
            for text in (getattr(item, "spoken_text", None) for item in outcomes)
            if isinstance(text, str) and text.strip()
        ]
        if not parts:
            return None
        return "\n".join(parts)

    def _rearm_task_window(self) -> None:
        """#15：待确认窗口**从播报结束起算**。

        真机教训（2026-10-13）：`arm()` 发生在播报**之前**，15 s 窗口被 ~13 s 的计划播报吃掉
        ⇒ 计划一念完就超时，用户还没说「确认」计划就作废（真机日志：下一轮直接播"已取消"）。
        这里在本轮播报**结束之后**把窗口重新计时（`router.rearm()`），失败一律静默。
        """

        try:
            from app.runtime.orchestration.voice_task_turn import KIND_AWAITING
        except Exception:  # noqa: BLE001 - 任务层不可用时语音链路照常
            return
        if not any(
            getattr(outcome, "kind", None) == KIND_AWAITING for outcome in self._task_outcomes
        ):
            return
        router = getattr(self._config, "task_router", None)
        rearm = getattr(router, "rearm", None)
        if not callable(rearm):
            return
        try:
            rearm()
        except Exception:  # noqa: BLE001 - 重新计时失败不得影响语音链路
            return

    # ---- P10 · T1 #16：键盘确认通道（Enter/空格=确认、Esc=取消） -------------

    def _key_action_allowed(self) -> bool:
        """只有"已有冻结计划待确认"时键盘动作才生效；其它时刻按键一律丢弃（含清缓冲）。"""

        task = self._voice_task
        if task is None:
            return False
        return getattr(task, "pending", None) is not None

    def _start_key_watch(self) -> None:
        """播报期间监视键盘：命中即**立即打断播报 + 立即执行**（对照 Pipecat `InterruptionFrame`）。

        没有 `key_source`（未启用键盘通道）时**不启动线程**；线程只在"待确认计划存在"时动作。
        """

        source = getattr(self, "_key_source", None)
        if source is None or self._voice_task is None:
            return
        stop = threading.Event()
        self._key_watch_stop = stop

        def watch() -> None:
            while not stop.is_set():
                try:
                    action = source()
                except Exception:  # noqa: BLE001 - 键盘读取失败即视为无输入
                    action = None
                if action in ("confirm", "cancel") and self._key_action_allowed():
                    self._key_action = action
                    self._key_interrupted = self.cancel_active_speech()
                    return
                stop.wait(KEY_WATCH_INTERVAL_SECONDS)

        thread = threading.Thread(target=watch, name="key-confirm-watch", daemon=True)
        self._key_watch = thread
        thread.start()

    def _stop_key_watch(self) -> None:
        stop = self._key_watch_stop
        thread = self._key_watch
        self._key_watch_stop = None
        self._key_watch = None
        if stop is not None:
            stop.set()
        if thread is not None:
            try:
                thread.join(timeout=0.5)
            except Exception:  # noqa: BLE001 - 收尾失败不影响已记录事实
                return

    def cancel_active_speech(self) -> bool:
        """立刻停掉剩余播报（TTS 缓冲/未播音频丢弃）；没有可取消的播报流时返回 ``False``。"""

        speaker = getattr(self, "_key_speaker", None)
        cancel = getattr(speaker, "cancel_active_speech", None)
        if not callable(cancel):
            return False
        try:
            return bool(cancel())
        except Exception:  # noqa: BLE001 - 取消失败不得影响任务事实
            return False

    def _handle_key_action(self, hud: Any | None) -> Any | None:
        """把键盘动作落到任务层（与语音确认**同一条** `execute()` 链路）并播报结果。"""

        action = getattr(self, "_key_action", None)
        if action is None or self._voice_task is None:
            return None
        try:
            if action == "confirm":
                outcome = self._voice_task.confirm_by_keyboard()
            else:
                outcome = self._voice_task.cancel_by_keyboard()
        except Exception:  # noqa: BLE001 - 任务层故障不得影响语音链路
            return None
        if outcome is None:
            return None
        self._speak_task_notice(outcome, hud)
        return outcome

    def _speak_task_notice(self, outcome: Any, hud: Any | None) -> None:
        """**轮次之外**（窗口过期 / 键盘动作）的通知播报：走 `speak_notice`，不改状态机。"""

        text = getattr(outcome, "spoken_text", None)
        if not isinstance(text, str) or not text.strip():
            return
        self._print(text)
        notice = getattr(self._loop, "speak_notice", None)
        if not callable(notice):
            return
        if hud is not None:
            _hud_set(hud, "SPEAKING")
            _hud_tick(hud)
        try:
            result = notice(text)
        except Exception:  # noqa: BLE001 - 通知播报失败不影响任务事实
            return
        finally:
            if hud is not None:
                _hud_set(hud, "LISTENING")
                _hud_tick(hud)
        spoken = bool(result[0]) if isinstance(result, tuple) and result else bool(result)
        if spoken:
            self._task_spoken = True

    def _task_facts(self, outcomes: list[Any]) -> dict[str, Any] | None:
        if not outcomes:
            return None
        details = dict(outcomes[-1].to_details())
        details["count"] = len(outcomes)
        details["spoken"] = self._task_spoken
        return details

    @staticmethod
    def _task_reason_override(outcomes: list[Any]) -> str | None:
        if not outcomes:
            return None
        reason = getattr(outcomes[-1], "reason", None)
        return reason if type(reason) is str and reason else None

    def run_turn(
        self,
        *,
        on_listening: Callable[[], None] | None = None,
        on_speech_started: Callable[[], None] | None = None,
        hud: Any | None = None,
        key_source: Callable[[], str | None] | None = None,
    ) -> AutoTurnFacts:
        """驱动一轮免按键语音；返回可显示/可记录的本地事实。

        ``hud`` 是可选的右下角提示窗（只读展示）；它的任何故障都会被隔离，
        绝不影响收音、识别、提交或播报。

        ``key_source``（P10 · T1 #16）是可选的**非阻塞单键读取器**：返回 ``"confirm"`` /
        ``"cancel"`` / ``None``。有待确认计划时命中即**立即打断播报并立即执行**
        （对照 Pipecat `InterruptionFrame`）；没有待确认计划时按键一律丢弃（含清缓冲）。
        """

        self_hud = hud
        if self._closed:
            raise AutoVoiceError("语音会话已关闭")
        self._key_source = key_source
        self._key_action = None
        self._key_interrupted = False
        self._turns += 1
        index = self._turns
        started_at = time.monotonic()
        # 诊断（2026-10-14）：记录本轮起点与"上一轮结束→本轮开始"的空档；首帧时刻在泵到帧时打点。
        self._turn_started_at = started_at
        self._turn_first_frame_at = None
        self._turn_listen_end_at = None
        self._turn_task_ms = None
        self._turn_gap_seconds = (
            None if self._last_turn_end is None else max(0.0, started_at - self._last_turn_end)
        )
        self._vad_hits = 0
        self._turn_pcm.clear()
        self._last_speaker = None
        self._barge_in_last = None
        # P10 R4：轮次之间是安全空闲点——先取一次设备决策（重连各试一次）。
        self._device_last = self._device_safe_point(turn_active=False)
        # P10 · T1 批次 3：待确认窗口已过 ⇒ 先在**收音之前**作废并播报"已取消"（fail closed）。
        self._task_spoken = False
        self._task_outcomes: list[Any] = []
        task_outcomes: list[Any] = []
        expired_task = self._expire_task_turn(self_hud)
        if expired_task is not None:
            task_outcomes.append(expired_task)
        # P9 B3：gate 模式必须先听到唤醒词才开启新的输入会话；唤醒音频不进 ASR。
        if self._wake_gate is not None:
            self._print(
                f"⏳ 等待唤醒词「{self._config.keyword}」…（最多 {self._config.kws_wait_seconds}s）"
            )
            detected = wait_for_wake_word(
                environment=self._environment,
                gate=self._wake_gate,
                timeout_seconds=self._config.kws_wait_seconds,
            )
            if not detected:
                self._print("✖ 未听到唤醒词：本轮不开启收音、不提交、不播报。")
                return self._turn_facts(
                    index=index,
                    boundary=None,
                    frames=0,
                    speech_seen=False,
                    end_reason="wake_word_timeout",
                    accepted=False,
                    submitted=False,
                    spoken=False,
                    reason="wake_word_timeout",
                    seconds=round(time.monotonic() - started_at, 3),
                )
            self._print("✔ 听到唤醒词：开启新一轮收音（唤醒音频不进识别）。")
        # 每轮开始前清空底层 VAD 状态（尤其是 Silero 的"已结束段"队列）：真机复现过
        # "上一轮的残留段让本轮第 1 帧就被判说完了"（frames=2、识别 `'。'`、仍提交播报）。
        self._reset_speech_detector()
        boundary = self._loop.start_turn()
        if boundary is None:
            detail = self._driver.last_start_reason or "start_rejected"
            # R4：采集启动被拒是"输入设备可能不可用"的**观察事实**（不猜别的）。
            self._observe_device("input", "disconnected")
            return self._turn_facts(
                index=index,
                boundary=None,
                frames=0,
                speech_seen=False,
                end_reason="start_failed",
                accepted=False,
                submitted=False,
                spoken=False,
                reason=f"start_rejected: {detail}",
                seconds=round(time.monotonic() - started_at, 3),
            )
        if on_listening is not None:
            on_listening()
        self._emit("listening")
        # R4：采集真正起来了 → 输入设备 READY 是已核实事实。
        self._observe_device("input", "ready")
        stall_deadline = (
            self._driver.config.speech_start_timeout_ms + self._driver.config.max_utterance_ms
        ) / 1000.0
        input_stall_seconds = self._config.input_stall_ms / 1000.0
        frames = 0
        end_reason = "auto_released"
        speech_seen = False
        cancelled = False
        last_progress = time.monotonic()
        idle_seconds_max = 0.0
        # 会话阶段回调 → 右下角提示窗状态；同时借高频轮询抽一次窗口消息。
        hud_phases = {
            "listening": "LISTENING",
            "hearing": "HEARING",
        }

        def on_phase(phase: str) -> None:
            _hud_set(self_hud, hud_phases.get(phase, "LISTENING"), turn_index=index)

        self.set_event_sink(on_phase if self_hud is not None else None)
        try:
            while True:
                processed = self._loop.pump()
                frames += processed
                _hud_tick(self_hud)
                # #16：键盘通道（非阻塞）。**始终读**（把无计划时的按键丢掉/清缓冲），
                # 只有"已有冻结计划待确认"时才把它当成确认/取消。
                if self._key_source is not None:
                    try:
                        key_action = self._key_source()
                    except Exception:  # noqa: BLE001 - 键盘读取失败即视为无输入
                        key_action = None
                    if key_action in ("confirm", "cancel") and self._key_action_allowed():
                        self._key_action = key_action
                        # 注意：**不要**用 `key_...` 当原因码——结构化日志的密钥正则
                        # （`key[-_ ]?` + 8 字符）会把它整段脱敏成 [REDACTED]（真机已复现）。
                        end_reason = f"kbd_{key_action}"
                        cancelled = True
                        break
                if processed > 0:
                    # 有帧就立刻继续泵（不睡）：把积压追平，消费者不落后于采集。
                    last_progress = time.monotonic()
                    idle_seconds_max = 0.0
                    if self._turn_first_frame_at is None:
                        self._turn_first_frame_at = last_progress
                if self._driver.speech_started and not speech_seen:
                    speech_seen = True
                    if on_speech_started is not None:
                        on_speech_started()
                    self._emit("hearing")
                if self._driver.auto_released:
                    stop_reason = self._driver.last_stop_reason
                    end_reason = stop_reason or "auto_released"
                    break
                if self._driver.last_stop_reason in (
                    "no_speech",
                    "detector_failure",
                ):
                    end_reason = self._driver.last_stop_reason
                    cancelled = True
                    break
                now = time.monotonic()
                if processed == 0:
                    idle_seconds = now - last_progress
                    if idle_seconds > idle_seconds_max:
                        idle_seconds_max = idle_seconds
                    if idle_seconds >= input_stall_seconds:
                        # 已开始收音却一帧都没有：设备断开或总线失效。立即收尾，
                        # 不再空转到 speech_start_timeout + max_utterance（默认 23s）。
                        end_reason = "input_stalled"
                        cancelled = True
                        break
                if now - started_at > stall_deadline:
                    end_reason = "stalled"
                    cancelled = True
                    break
                time.sleep(_turn_idle_sleep_seconds(processed))
        finally:
            self.set_event_sink(None)
        if cancelled or not speech_seen:
            reason = NO_SPEECH_REASON if end_reason == "no_speech" else end_reason
            if end_reason == "input_stalled":
                # 一帧都没有：输入设备/总线不可用的观察事实。
                self._observe_device("input", "disconnected")
            self._cancel_turn()
            # #16：本轮是因为按了确认/取消键而中止的 ⇒ 立刻落地任务动作并播报结果。
            key_outcome = self._handle_key_action(self_hud)
            key_kwargs: dict[str, Any] = {}
            if key_outcome is not None:
                reason = self._task_reason_override([key_outcome]) or reason
                key_kwargs["task"] = self._task_facts([key_outcome])
            return self._turn_facts(
                index=index,
                boundary=boundary,
                frames=frames,
                speech_seen=speech_seen,
                end_reason=end_reason,
                accepted=False,
                submitted=False,
                spoken=False,
                reason=reason,
                seconds=round(time.monotonic() - started_at, 3),
                vad_hits=self._vad_hits,
                idle_seconds=round(idle_seconds_max, 3),
                **key_kwargs,
            )
        self._loop.release_input()
        # 诊断：收音结束时刻（此后是 ASR 解码 + 路由/装配 + 播报）。
        self._turn_listen_end_at = time.monotonic()
        # P9 B4：声纹误触发过滤——不匹配就在**提交之前**丢弃本轮（不产生任何授权/执行事实）。
        if self._voiceprint is not None:
            rejected, vp_reason = self._verify_speaker()
            if rejected:
                self._cancel_turn()
                return self._turn_facts(
                    index=index,
                    boundary=boundary,
                    frames=frames,
                    speech_seen=True,
                    end_reason="voiceprint_rejected",
                    accepted=False,
                    submitted=False,
                    spoken=False,
                    reason=f"voiceprint_rejected:{vp_reason}",
                    seconds=round(time.monotonic() - started_at, 3),
                    vad_hits=self._vad_hits,
                    idle_seconds=round(idle_seconds_max, 3),
                )
        self._start_key_watch()
        # P10 · T1 #13：播报期开始静默用户输入（Pipecat `AlwaysUserMuteStrategy` 口径）。
        self._begin_speech_mute()
        try:
            report = self._complete_turn_with_hud(
                self_hud,
                index,
                reply_provider=self._task_reply_provider(self_hud) if self._voice_task is not None else None,
            )
        finally:
            self._stop_key_watch()
            # #13：播报结束 ⇒ 进入 `post_speech_mute_ms` 冷却窗（0 = 立即恢复）。
            self._end_speech_mute()
        # #16：播报期间按下的确认/取消键在这里落地（已顺手停掉剩余播报）。
        key_outcome = self._handle_key_action(self_hud)
        # #15：计划/重问已经念完 ⇒ 待确认窗口从现在开始计时（而不是从念之前）。
        self._rearm_task_window()
        # P10 · T1 批次 3：任务路由在本轮不会调用 Agent（outcome=None），文本由 reply_provider
        # 在 REPLYING 状态内提供并播报：新任务 ⇒ 计划 + arm（不执行）；确认轮 ⇒ 执行 + 结果摘要；
        # 被丢弃/超时 ⇒ "已取消"。
        task_outcomes.extend(self._task_outcomes)
        if key_outcome is not None:
            task_outcomes.append(key_outcome)
        if self._task_outcomes:
            self._task_spoken = bool(getattr(report, "spoken", False))
        last = self._driver.last_result
        text = getattr(last, "accepted_text", None)
        reply = getattr(last, "reply_text", None)
        self._last_text = text if isinstance(text, str) else None
        # P10 R4：播报期的打断事实 + 前置音频去向（本批不并入下一轮，如实写明）。
        barge_in_facts = dict(self._barge_in_last) if self._barge_in_last else None
        confirmed = bool(barge_in_facts and barge_in_facts.get("confirmed"))
        # R4-A：只有**真的停过播**才算"被插话打断"。安全门拦下（`barge_in_unproven_echo`）
        # 或 duck 模式抑制确认时，播报其实播完了，不能再声称 end_reason=barge_in_interrupted。
        interrupted = bool(self._barge_in_stopped_episode)
        self._barge_in_stopped_episode = False
        pre_roll_facts: dict[str, Any] | None = None
        if confirmed and self._pending_pre_roll:
            frames = len(self._pending_pre_roll) // 640
            pre_roll_facts = {
                "frames": frames,
                "duration_ms": frames * FRAME_MS,
                "merged": False,
                "reason": PRE_ROLL_MERGE_UNSUPPORTED,
            }
        # 输出设备健康：只有真实播报成功/失败才是观察事实（text_only 不代表设备状态）。
        if report.reason == "speech_failed":
            self._observe_device("output", "disconnected")
        elif report.spoken:
            self._observe_device("output", "ready")
        turn_end_reason = BARGE_IN_INTERRUPTED_REASON if interrupted else end_reason
        turn_reason = BARGE_IN_INTERRUPTED_REASON if interrupted else report.reason
        # 任务轮的真实结果优先（任务路由原本只会被记成 rejected/text_only）。
        task_override = self._task_reason_override(task_outcomes)
        if task_override is not None:
            turn_reason = task_override
        return self._turn_facts(
            index=index,
            boundary=boundary,
            frames=frames,
            speech_seen=True,
            end_reason=turn_end_reason,
            accepted=report.accepted,
            submitted=report.submitted,
            spoken=report.spoken,
            reason=turn_reason,
            seconds=round(time.monotonic() - started_at, 3),
            asr_text=self._last_text,
            reply_text=reply if isinstance(reply, str) else None,
            vad_hits=self._vad_hits,
            idle_seconds=round(idle_seconds_max, 3),
            barge_in=barge_in_facts,
            pre_roll=pre_roll_facts,
            device=dict(self._device_last) if self._device_last else None,
            task=self._task_facts(task_outcomes),
            # 诊断（2026-10-24，`text_only` 定性）：回复字数 / 跳过播报的固定短码 / 路由短码。
            # 只读投影，不参与任何播报、授权或执行判断。
            reply_chars=getattr(last, "reply_chars", None),
            speech_skip_reason=getattr(last, "speech_skip_reason", None),
            route=getattr(last, "route", None),
        )

    def _cancel_turn(self) -> None:
        try:
            self._loop.cancel_turn(reason="cancelled")
        except Exception:  # noqa: BLE001 - 取消失败不改变已记录事实
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop.boundary is not None:
            self._cancel_turn()
        channel = self._barge_in_channel
        if channel is not None:
            try:
                channel.disarm()
            except Exception:  # noqa: BLE001 - 收尾失败不影响已记录事实
                self._barge_in_errors += 1
        self._restore_gain()



__all__ = [
    "AEC_REFERENCE_MODES",
    "AUDIO_CAPACITY_FRAMES",
    "AutoTurnFacts",
    "AutoVoiceConfig",
    "AutoVoiceError",
    "AutoVoiceSession",
    "BARGE_IN_INTERRUPTED_REASON",
    "BARGE_IN_MODES",
    "CALIBRATION_AUDIO_SESSION_ID",
    "CALIBRATION_CAPACITY_FRAMES",
    "CALIBRATION_DEADLINE_FACTOR",
    "CALIBRATION_STOP_REASON",
    "CALIBRATION_STREAM_ID",
    "CONFIRM_SUPPRESSED_REASON",
    "DEFAULT_BARGE_IN_MAX_MONITOR_SECONDS",
    "DEFAULT_KEYWORD",
    "DEFAULT_VAD_MODEL",
    "DEVICE_PROBE_AUDIO_SESSION_ID",
    "DEVICE_PROBE_CAPACITY_FRAMES",
    "DEVICE_PROBE_STOP_REASON",
    "DEVICE_PROBE_STREAM_ID",
    "DEVICE_RECOVERY_MODES",
    "DUCK_UNAVAILABLE_REASON",
    "FRAME_MS",
    "INPUT_STALL_MS",
    "KWS_MODES",
    "NO_SPEECH_REASON",
    "PRE_ROLL_MERGE_UNSUPPORTED",
    "THRESHOLD_MODES",
    "ThresholdCalibrationError",
    "VAD_MODES",
    "VOICEPRINT_MODES",
    "VadUnavailableError",
    "VoiceprintUnavailableError",
    "WAKE_AUDIO_SESSION_ID",
    "WAKE_CAPACITY_FRAMES",
    "WAKE_STOP_REASON",
    "WAKE_STREAM_ID",
    "WakeWordUnavailableError",
    "calibrate_speech_threshold",
    "load_voiceprint_profile",
    "wait_for_wake_word",
    "_hud_set",
    "_hud_tick",
]
