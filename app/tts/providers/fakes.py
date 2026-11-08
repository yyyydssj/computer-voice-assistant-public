from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from threading import Event, Lock

from .contracts import (
    PCMChunk,
    PCMChunkStreamValidator,
    VoiceStudioContractError,
    VoiceStudioSentenceRequest,
)


ChunkScript = Callable[[VoiceStudioSentenceRequest], Iterable[PCMChunk]]


@dataclass(slots=True)
class _FakeProviderBase:
    """Deterministic offline provider used only by Batch 1 tests."""

    script: ChunkScript | Iterable[PCMChunk]
    name: str
    calls: list[VoiceStudioSentenceRequest] = field(default_factory=list)
    close_calls: int = 0
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def _chunks(self, request: VoiceStudioSentenceRequest) -> Iterable[PCMChunk]:
        if callable(self.script):
            return self.script(request)
        return self.script

    def stream_sentence(
        self,
        request: VoiceStudioSentenceRequest,
        *,
        cancelled: Event,
    ) -> Iterator[PCMChunk]:
        if not isinstance(request, VoiceStudioSentenceRequest):
            raise TypeError("request 必须是 VoiceStudioSentenceRequest")
        with self._lock:
            if self._closed:
                raise VoiceStudioContractError(f"{self.name} provider 已关闭")
            self.calls.append(request)
        validator = PCMChunkStreamValidator(request)
        for chunk in self._chunks(request):
            if cancelled.is_set():
                validator.cancel()
                raise VoiceStudioContractError("fake provider 在取消后停止输出")
            yield validator.accept(chunk)
        validator.finish()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.close_calls += 1


class FakeWebSocketProvider(_FakeProviderBase):
    """Fake WS/PCM provider; no socket or network operation is possible."""

    def __init__(self, script: ChunkScript | Iterable[PCMChunk]) -> None:
        super().__init__(script=script, name="fake_ws_pcm")


class FakeHttpProvider(_FakeProviderBase):
    """Fake HTTP/WAV-normalized provider; it emits the same PCM contract."""

    def __init__(self, script: ChunkScript | Iterable[PCMChunk]) -> None:
        super().__init__(script=script, name="fake_http_wav")


@dataclass(slots=True)
class FakePCMSink:
    """In-memory sink that enforces the same identity and ordering fence."""

    received: list[PCMChunk] = field(default_factory=list)
    _validators: dict[str, PCMChunkStreamValidator] = field(default_factory=dict, init=False, repr=False)

    def start(self, request: VoiceStudioSentenceRequest) -> None:
        if request.provider_request_id in self._validators:
            raise VoiceStudioContractError("同一 request 不得重复启动 sink")
        self._validators[request.provider_request_id] = PCMChunkStreamValidator(request)

    def write(self, chunk: PCMChunk) -> PCMChunk:
        try:
            validator = self._validators[chunk.provider_request_id]
        except KeyError as exc:
            raise VoiceStudioContractError("PCM sink 尚未启动该 request") from exc
        accepted = validator.accept(chunk)
        self.received.append(accepted)
        return accepted

    def cancel(self, request: VoiceStudioSentenceRequest) -> None:
        try:
            validator = self._validators[request.provider_request_id]
        except KeyError as exc:
            raise VoiceStudioContractError("PCM sink 尚未启动该 request") from exc
        validator.cancel()

    def finish(self, request: VoiceStudioSentenceRequest) -> tuple[PCMChunk, ...]:
        try:
            validator = self._validators[request.provider_request_id]
        except KeyError as exc:
            raise VoiceStudioContractError("PCM sink 尚未启动该 request") from exc
        return validator.finish()


__all__ = ["FakeHttpProvider", "FakePCMSink", "FakeWebSocketProvider"]
