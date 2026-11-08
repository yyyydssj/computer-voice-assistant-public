"""P8：按键触发的半双工语音闭环。

本包只编排既有本地能力：语音输入边界、轮次状态、统一输入接缝和逐句播报。
它不实现授权、计划、执行、审计或恢复语义，也不把语音变成新的权限通道。
"""

from app.voice_loop.boundary import (
    TURN_CLOSE_REASONS,
    VOICE_LOOP_SCHEMA_VERSION,
    VoiceInputSource,
    VoiceLoopDiagnostic,
    VoiceLoopError,
    VoiceLoopState,
    VoiceLoopStateMachine,
    VoiceLoopTransition,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
)
from app.voice_loop.chat_loop import (
    MAX_REPLY_LENGTH,
    ChatVoiceLoop,
    VoiceChatTurnReport,
    VoiceDriverTurnResult,
    VoiceTurnDriver,
)
from app.voice_loop.drivers import PushToTalkVoiceDriver, speech_eligible
from app.voice_loop.auto_driver import (
    STOP_DETECTOR_FAILURE,
    STOP_MAX_UTTERANCE_DURATION,
    STOP_NO_SPEECH,
    STOP_PROVIDER_FAILED,
    STOP_SILENCE_TIMEOUT,
    AutoCaptureDriverConfig,
    AutoCaptureVoiceDriver,
)
from app.voice_loop.input_seam import (
    DEGRADE_REASONS,
    SWITCH_REFUSAL_REASONS,
    VoiceInputDiagnostic,
    VoiceInputMode,
    VoiceInputSeam,
    VoiceInputSubmission,
    VoiceSwitchResult,
)

__all__ = [
    "DEGRADE_REASONS",
    "MAX_REPLY_LENGTH",
    "STOP_DETECTOR_FAILURE",
    "STOP_MAX_UTTERANCE_DURATION",
    "STOP_NO_SPEECH",
    "STOP_PROVIDER_FAILED",
    "STOP_SILENCE_TIMEOUT",
    "SWITCH_REFUSAL_REASONS",
    "TURN_CLOSE_REASONS",
    "VOICE_LOOP_SCHEMA_VERSION",
    "AutoCaptureDriverConfig",
    "AutoCaptureVoiceDriver",
    "ChatVoiceLoop",
    "PushToTalkVoiceDriver",
    "VoiceChatTurnReport",
    "VoiceDriverTurnResult",
    "VoiceInputDiagnostic",
    "VoiceInputMode",
    "VoiceInputSeam",
    "VoiceInputSource",
    "VoiceInputSubmission",
    "VoiceLoopDiagnostic",
    "VoiceLoopError",
    "VoiceLoopState",
    "VoiceLoopStateMachine",
    "VoiceLoopTransition",
    "VoiceSwitchResult",
    "VoiceTurnBoundary",
    "VoiceTurnDriver",
    "VoiceTurnRegistry",
    "speech_eligible",
]
