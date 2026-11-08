"""Opt-in synchronous P7 local TTS integration seam. No actual provider or device implementation.

The caller supplies explicit fake synthesizer and device acknowledgement ports. This
module never equates a generated chunk or an internal PLAYED state with audibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.contracts.base import require_identifier
from app.tts.capabilities import AudioFormat, ProviderCapabilityRegistry
from app.tts.domain.voice_profile import VoiceProfileRegistry
from app.tts.lifecycle import ProviderRequestAudioChunk, ProviderRequestLifecycleRegistry
from app.tts.playback import ProviderRequestPlaybackRegistry, ProviderRequestPlaybackState
from app.tts.playback.queue import PlaybackQueue, PlaybackQueueEntry
from app.tts.requests import ProviderRequest, ProviderRequestRegistry
from app.tts.routing import TTSFallbackRouter, TTSRoute
from app.tts.segmentation import ReplySegmenter, SegmentationDecision, TextChunk


class SynthesisPort(Protocol):
    def synthesize(self, request: ProviderRequest) -> int:
        """Return strictly positive duration in ms; NEVER implies device playback."""


class DevicePort(Protocol):
    def commit(self, entry: PlaybackQueueEntry) -> bool: ...
    def start(self, entry: PlaybackQueueEntry) -> bool: ...
    def complete(self, entry: PlaybackQueueEntry) -> int: ...
    def stop(self, entry: PlaybackQueueEntry) -> bool: ...


@dataclass(frozen=True, slots=True)
class LocalSpeechResult:
    reply_id: str
    text: str
    segment_texts: tuple[str, ...]
    acknowledged_segments: tuple[str, ...]
    text_fallback: bool
    stop_unconfirmed: bool = False


class LocalSpeechOrchestrator:
    """A one-reply-at-a-time, fail-closed *local* integration for test/future adapters.

    A reply is user-confirmed by the caller. No audio bytes are transferred by
    this seam: do not attach a real provider or a real device without designing
    and verifying a separate bounded, format-aware transport. Profiles are frozen
    per reply; any
    version drift fails closed instead of changing voices. Nothing is registered
    in the default runtime or plugged into task/execution/authorization facts.
    """

    def __init__(self, *, capabilities: ProviderCapabilityRegistry,
                 profiles: VoiceProfileRegistry, synthesizer: SynthesisPort,
                 device: DevicePort, audio_format: AudioFormat,
                 edge_profile_id: str | None = None, max_duration_ms: int = 8000) -> None:
        if not isinstance(capabilities, ProviderCapabilityRegistry) or not isinstance(profiles, VoiceProfileRegistry):
            raise TypeError("capabilities/profiles must be registries")
        if not isinstance(audio_format, AudioFormat) or max_duration_ms <= 0:
            raise ValueError("invalid audio format or queue budget")
        self.capabilities, self.profiles = capabilities, profiles
        self.synthesizer, self.device = synthesizer, device
        self.audio_format = audio_format
        self.max_duration_ms = max_duration_ms
        self.router = TTSFallbackRouter(edge_voice_profile_id=edge_profile_id)
        self._edge_profile_id = edge_profile_id
        self._device_uncertain = False
        self._voice_uncertain = False
        self._seen: set[str] = set()
        self._busy = False
        self._cancelled = False

    def cancel(self, reply_id: str) -> None:
        """Request a cooperative stop; receipt of a device stop is checked in speak."""
        if not self._busy or self.router.snapshot.active_reply_id != reply_id:
            raise ValueError("only the active reply may be cancelled")
        self._cancelled = True

    def speak(self, *, reply_id: str, confirmed_text: str) -> LocalSpeechResult:
        require_identifier("reply_id", reply_id)
        if self._busy or reply_id in self._seen:
            raise ValueError("concurrent or duplicate reply is forbidden")
        if not isinstance(confirmed_text, str) or not confirmed_text.strip():
            raise ValueError("confirmed text must not be empty")
        self._busy = True
        self._cancelled = False
        segmenter = ReplySegmenter(reply_id=reply_id)
        self._seen.add(reply_id)
        acknowledged: list[str] = []
        segments: tuple[str, ...] = ()
        fallback, stop_unconfirmed = False, False
        started_route = False
        try:
            outcome = segmenter.submit(TextChunk(
                chunk_id=f"chunk-{reply_id}", reply_id=reply_id, sequence=1,
                text=confirmed_text, is_final=True,
            ))
            if outcome.decision is not SegmentationDecision.ACCEPTED:
                return LocalSpeechResult(reply_id, confirmed_text, (), (), True)
            segments = tuple(item.text for item in outcome.segments)
            route = self.router.start_reply(reply_id)
            started_route = True
            if route is TTSRoute.TEXT or self._device_uncertain or self._voice_uncertain:
                return LocalSpeechResult(reply_id, confirmed_text, segments, (), True)
            try:
                session = self.profiles.bind_session(f"session-{reply_id}")
                if session.profile.provider != ("edge_tts" if route is TTSRoute.EDGE else route.value) or (route is TTSRoute.EDGE and session.profile_id != self._edge_profile_id):
                    raise ValueError("selected voice differs from active route")
            except (ValueError, TypeError):
                return LocalSpeechResult(reply_id, confirmed_text, segments, (), True)
            requests = ProviderRequestRegistry(self.capabilities, self.profiles)
            lifecycles = ProviderRequestLifecycleRegistry(requests)
            playback = ProviderRequestPlaybackRegistry(lifecycles)
            queue = PlaybackQueue(lifecycles, playback, max_segments=1,
                                  max_duration_ms=self.max_duration_ms)
            for segment in outcome.segments:
                if self._cancelled:
                    fallback = True
                    break
                entry = None
                request = None
                phase = "uncommitted"
                try:
                    request = requests.prepare(
                        provider_request_id=f"request-{segment.segment_id}",
                        reply_id=reply_id, segment_id=segment.segment_id,
                        request_sequence=segment.segment_index, text=segment.text,
                        audio_format=self.audio_format, provider_generation=1,
                        voice_session=session, require_cancellation=True,
                    )
                    rid = request.provider_request_id
                    lifecycles.accept(rid, event_id=f"accept-{rid}")
                    lifecycles.synthesis_started(rid, event_id=f"synth-{rid}")
                    duration = self.synthesizer.synthesize(request)
                    if type(duration) is not int or not 0 < duration <= self.max_duration_ms:
                        raise ValueError("invalid duration or playback budget")
                    chunk_id = f"audio-{rid}"
                    lifecycles.record_audio_chunk(ProviderRequestAudioChunk(
                        event_id=chunk_id, audio_chunk_index=1, receive_sequence=1,
                        provider_request_id=rid, reply_id=reply_id,
                        segment_id=segment.segment_id, provider_name=request.provider_name,
                        provider_generation=request.provider_generation,
                        voice_profile_id=request.voice_profile_id,
                        voice_profile_version=request.voice_profile_version,
                        audio_format=request.audio_format,
                        capability_check=request.capability_check,
                    ))
                    lifecycles.audio_produced(rid, event_id=f"produced-{rid}")
                    entry = queue.enqueue(rid, audio_chunk_event_id=chunk_id,
                                          audio_duration_ms=duration)
                    lifecycles.audio_enqueued(rid, event_id=f"enqueued-{rid}")
                    lifecycles.complete(rid, event_id=f"synthesis-complete-{rid}")
                    queue.begin()
                    if self._cancelled or self.profiles.selected_profile_id != session.profile_id or self.profiles.selected().version != session.profile_version:
                        raise ValueError("cancelled or voice profile drift before device commit")
                    # A false/raised commit is ambiguous; STOP rather than try Edge.
                    phase = "commit-attempted"
                    if self.device.commit(entry) is not True:
                        raise ValueError("device commit not acknowledged")
                    playback.commit(rid, event_id=f"commit-{rid}", audio_chunk_event_id=chunk_id)
                    if self._cancelled:
                        raise ValueError("cancelled after device commit")
                    phase = "committed"
                    if self.device.start(entry) is not True:
                        raise ValueError("device start not acknowledged")
                    playback.start(rid, event_id=f"start-{rid}", audio_chunk_event_id=chunk_id)
                    if self._cancelled:
                        raise ValueError("cancelled after playback start")
                    phase = "started"
                    offset = self.device.complete(entry)
                    if self._cancelled or type(offset) is not int or offset <= 0:
                        raise ValueError("device completion not acknowledged")
                    playback.played(rid, event_id=f"played-{rid}",
                                    audio_chunk_event_id=chunk_id, playback_offset_ms=offset)
                    queue.finish(rid)
                    acknowledged.append(segment.segment_id)
                except Exception:
                    fallback = True
                    if entry is not None and phase != "uncommitted":
                        try:
                            stop_unconfirmed = self.device.stop(entry) is not True
                        except Exception:
                            stop_unconfirmed = True
                        self._device_uncertain = self._device_uncertain or stop_unconfirmed
                    if request is not None:
                        rid = request.provider_request_id
                        state = playback.get(rid).state
                        if state in (ProviderRequestPlaybackState.COMMITTED_NOT_STARTED,
                                     ProviderRequestPlaybackState.PLAYBACK_STARTED,
                                     ProviderRequestPlaybackState.PARTIALLY_PLAYED):
                            # Local abort fact is not proof of device stop.
                            playback.abort(rid, event_id=f"abort-{rid}",
                                           audio_chunk_event_id=entry.audio_chunk_event_id,
                                           playback_offset_ms=playback.get(rid).playback_offset_ms,
                                           reason="local speech failure")
                        if not lifecycles.get(rid).terminal:
                            lifecycles.cancel(rid, event_id=f"cancel-{rid}", reason="local speech failure")
                    if queue.pending:
                        queue.cancel(reply_id)
                    if route is TTSRoute.VOICESTUDIO:
                        self.router.primary_failed(reply_id, segment.segment_id,
                                                   playback_state=playback.get(rid).state if request else ProviderRequestPlaybackState.NOT_COMMITTED)
                    elif route is TTSRoute.EDGE:
                        self.router.edge_failed(reply_id, segment.segment_id,
                                                playback_state=playback.get(rid).state if request else ProviderRequestPlaybackState.NOT_COMMITTED)
                    if phase in ("started", "committed", "commit-attempted"):
                        self._voice_uncertain = True
                    # Never retry a prepared or possibly committed segment in this reply.
                    break
            return LocalSpeechResult(reply_id, confirmed_text, segments,
                                     tuple(acknowledged), fallback, stop_unconfirmed)
        finally:
            segmenter.cancel()
            if started_route:
                self.router.finish_reply(reply_id)
            self._busy = False