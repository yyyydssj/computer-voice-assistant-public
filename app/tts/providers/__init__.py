"""VoiceStudio dual-path provider contracts and offline adapters."""

from .contracts import (
    PCMChunk,
    PCMChunkStreamValidator,
    VoiceStudioAudioProvider,
    VoiceStudioContractError,
    VoiceStudioSentenceRequest,
)
from .edge_sapi import (
    EdgeSapiConfig,
    EdgeSapiError,
    EdgeSapiVoiceProvider,
    list_edge_sapi_voices,
)
from .fakes import FakeHttpProvider, FakePCMSink, FakeWebSocketProvider
from .http_wav import VoiceStudioHttpWavConfig, VoiceStudioHttpWavError, VoiceStudioHttpWavProvider
from .voicestudio_ws import (
    VoiceStudioWebSocketConfig,
    VoiceStudioWebSocketError,
    VoiceStudioWebSocketProvider,
)

__all__ = [
    "EdgeSapiConfig",
    "EdgeSapiError",
    "EdgeSapiVoiceProvider",
    "FakeHttpProvider",
    "FakePCMSink",
    "FakeWebSocketProvider",
    "PCMChunk",
    "PCMChunkStreamValidator",
    "VoiceStudioAudioProvider",
    "VoiceStudioContractError",
    "VoiceStudioHttpWavConfig",
    "VoiceStudioHttpWavError",
    "VoiceStudioHttpWavProvider",
    "VoiceStudioSentenceRequest",
    "VoiceStudioWebSocketConfig",
    "VoiceStudioWebSocketError",
    "VoiceStudioWebSocketProvider",
    "list_edge_sapi_voices",
]
