"""Explicit opt-in, text-first speech for completed Agent replies.

Does not install an Agent, wire the default runtime, or acknowledge audible playback.
"""
from __future__ import annotations

from collections.abc import Callable

from app.agent.adapter import AgentTurnOutcome
from app.tts.private_selection import VoiceSelection, private_data_root, read_selection
from app.tts.real_playback import PreviewResult, RealVoicePreview, RealtimeVoiceSession
from app.tts.streaming_session import StreamingVoiceSession


class CompletedReplySpeaker:
    """Speak at most once per request; failures never change the Agent outcome."""

    def __init__(self, *, selection_reader: Callable[[], VoiceSelection] | None = None,
                 preview_factory: Callable[[VoiceSelection], RealVoicePreview] = RealVoicePreview,
                 session_factory: Callable[[VoiceSelection], RealtimeVoiceSession] | None = None,
                 voicestudio_mode: str = "legacy") -> None:
        self._selection_reader = selection_reader or (lambda: read_selection(private_data_root()))
        self._preview_factory = preview_factory
        if voicestudio_mode not in {"legacy", "http_wav", "ws_pcm", "auto"}:
            raise ValueError("voicestudio_mode must be legacy, http_wav, ws_pcm or auto")
        if voicestudio_mode in {"ws_pcm", "auto"} and session_factory is None:
            # The streaming session needs explicitly injected providers and a
            # device backend; never build a network or device path implicitly.
            raise ValueError("ws_pcm/auto requires an explicit session_factory")
        self._session_factory = session_factory or (
            lambda selection: RealtimeVoiceSession(selection, provider_mode=voicestudio_mode)
        )
        self._seen: set[tuple[str, str]] = set()
        self._disabled = False

    def start_stream(self) -> RealtimeVoiceSession | StreamingVoiceSession | None:
        """Start low-latency sentence playback for one in-flight Agent reply."""
        if self._disabled:
            return None
        try:
            return self._session_factory(self._selection_reader())
        except Exception:
            self._disabled = True
            return None

    def __call__(self, outcome: AgentTurnOutcome) -> PreviewResult | None:
        if not isinstance(outcome, AgentTurnOutcome) or not outcome.completed or outcome.failure is not None:
            return None
        if (type(outcome.text) is not str or not outcome.text.strip()
                or type(outcome.finish_reason) is not str or not outcome.finish_reason.strip()
                or type(outcome.request_id) is not str or not outcome.request_id
                or type(outcome.session_id) is not str or not outcome.session_id):
            return None
        key = outcome.session_id, outcome.request_id
        if key in self._seen or self._disabled:
            return None
        # Mark before any external I/O: even an interrupted call cannot repeat a reply.
        self._seen.add(key)
        try:
            preview = self._preview_factory(self._selection_reader())
            try:
                result = preview.speak(outcome.text)
            finally:
                close = getattr(preview, "close", None)
                if callable(close):
                    close()
            if not isinstance(result, PreviewResult) or result.text != outcome.text:
                self._disabled = True
                return None
            if result.text_fallback:
                self._disabled = True
            return result
        except Exception:
            self._disabled = True
            return None