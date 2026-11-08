from __future__ import annotations

import io
import json
import unittest
import wave
from email.message import Message
from urllib.request import Request

from app.agent.adapter import AgentTurnOutcome
from app.evidence.agent_chain import SynthesisResult
from app.evidence.local_ports import (
    CaptureRequest,
    DeviceCaptureFacts,
    LocalAgentAdapter,
    LocalAgentConfig,
    LocalPortError,
    LocalVoiceStudioClient,
    LocalVoiceStudioConfig,
    LocalWindowsDeviceEvidence,
)
from app.tts.playback_worker import PlaybackEvent, PlaybackState


def wav_bytes(*, pcm: bytes = b"\x01\x00" * 12, channels: int = 1, width: int = 2, rate: int = 24000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(pcm)
    return stream.getvalue()


class FakeResponse:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class RecordingOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[Request, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.requests.append((request, timeout))
        return self.response


class FakeWorker:
    def __init__(self, state: PlaybackState = PlaybackState.COMPLETED) -> None:
        self.state = state
        self.submissions: list[tuple[bytes, dict[str, str]]] = []
        self.closed = False

    def submit(self, audio: bytes, *, metadata: dict[str, str]):
        self.submissions.append((audio, metadata))
        return FakeTicket(self.state)

    def wait(self, ticket: "FakeTicket", *, timeout: float) -> PlaybackState:
        return ticket.state

    def stop(self, ticket: "FakeTicket") -> PlaybackState:
        ticket.state = PlaybackState.STOP_ACKED
        return ticket.state

    def close(self) -> None:
        self.closed = True


class FakeTicket:
    def __init__(self, state: PlaybackState) -> None:
        self.state = state
        self.ticket_id = "ticket-1"
        self.events: list[object] = []

    @property
    def terminal(self) -> bool:
        return self.state in {
            PlaybackState.STOP_ACKED,
            PlaybackState.COMPLETED,
            PlaybackState.STOP_TIMEOUT,
            PlaybackState.FAILED,
            PlaybackState.UNKNOWN,
        }


class LocalPortsTests(unittest.TestCase):
    def test_local_urls_reject_public_hosts_and_unsafe_parts(self) -> None:
        for endpoint in (
            "https://example.com",
            "http://192.168.1.10:3900",
            "http://user:pass@127.0.0.1:3900",
            "http://127.0.0.1:3900?token=secret",
            "ftp://127.0.0.1:3900",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    LocalAgentConfig(endpoint=endpoint)
        self.assertEqual(LocalAgentConfig(endpoint="http://localhost:3901").endpoint, "http://localhost:3901")
        self.assertEqual(LocalVoiceStudioConfig(endpoint="http://[::1]:3900").endpoint, "http://[::1]:3900")

    def test_agent_sends_bounded_json_headers_and_preserves_exact_reply(self) -> None:
        body = json.dumps({
            "version": "local-agent.v1",
            "request_id": "req-1",
            "session_id": "sess-1",
            "completed": True,
            "text": " exact reply ",
            "finish_reason": "stop",
        }).encode("utf-8")
        opener = RecordingOpener(FakeResponse(200, "application/json; charset=utf-8", body))
        adapter = LocalAgentAdapter(opener=opener)
        result = adapter.submit_chat(request_id="req-1", session_id="sess-1", text="hello")
        self.assertIsInstance(result, AgentTurnOutcome)
        self.assertEqual(result.text, " exact reply ")
        request, _timeout = opener.requests[0]
        self.assertEqual(request.get_header("X-p7-request-id"), "req-1")
        self.assertEqual(request.get_header("X-p7-session-id"), "sess-1")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload, {
            "version": "local-agent.v1", "request_id": "req-1", "session_id": "sess-1", "text": "hello",
        })

    def test_agent_errors_and_identity_mismatch_fail_closed_and_duplicates_are_rejected(self) -> None:
        for response in (
            FakeResponse(500, "application/json", b"{}"),
            FakeResponse(200, "application/json", b"not-json"),
        ):
            adapter = LocalAgentAdapter(opener=RecordingOpener(response))
            with self.assertRaises(LocalPortError):
                adapter.submit_chat(request_id="req-1", session_id="sess-1", text="hello")
        mismatch = json.dumps({
            "version": "local-agent.v1", "request_id": "other", "session_id": "sess-1",
            "completed": True, "text": "reply",
        }).encode()
        adapter = LocalAgentAdapter(opener=RecordingOpener(FakeResponse(200, "application/json", mismatch)))
        with self.assertRaisesRegex(LocalPortError, "identity"):
            adapter.submit_chat(request_id="req-1", session_id="sess-1", text="hello")
        response = FakeResponse(200, "application/json", json.dumps({
            "version": "local-agent.v1", "request_id": "req-1", "session_id": "sess-1",
            "completed": True, "text": "reply",
        }).encode())
        adapter = LocalAgentAdapter(opener=RecordingOpener(response))
        adapter.submit_chat(request_id="req-1", session_id="sess-1", text="hello")
        with self.assertRaisesRegex(ValueError, "already"):
            adapter.submit_chat(request_id="req-1", session_id="sess-1", text="hello")

    def test_voicestudio_sends_multipart_exact_text_and_validates_wav(self) -> None:
        audio = wav_bytes()
        opener = RecordingOpener(FakeResponse(200, "audio/wav", audio))
        client = LocalVoiceStudioClient(LocalVoiceStudioConfig(profile_id="xiaobing"), opener=opener)
        result = client.synthesize("exact ?? reply", request_id="req-1", session_id="sess-1")
        self.assertIsInstance(result, SynthesisResult)
        self.assertEqual(result.audio, audio)
        request, _timeout = opener.requests[0]
        payload = request.data.decode("utf-8")
        self.assertIn('name="text"', payload)
        self.assertIn("exact ?? reply", payload)
        self.assertIn('name="profile_id"', payload)
        self.assertIn("xiaobing", payload)
        self.assertIn('name="stream"', payload)
        self.assertIn("false", payload)
        self.assertEqual(request.get_header("X-p7-request-id"), "req-1")

    def test_voicestudio_rejects_bad_content_type_silence_format_and_oversize(self) -> None:
        cases = (
            (FakeResponse(200, "application/octet-stream", wav_bytes()), "content_type"),
            (FakeResponse(200, "audio/wav", wav_bytes(pcm=b"\x00" * 24)), "silent"),
            (FakeResponse(200, "audio/wav", wav_bytes(rate=16000)), "format"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                client = LocalVoiceStudioClient(LocalVoiceStudioConfig(), opener=RecordingOpener(response))
                with self.assertRaisesRegex(LocalPortError, expected):
                    client.synthesize("reply", request_id="req-1", session_id="sess-1")
        client = LocalVoiceStudioClient(
            LocalVoiceStudioConfig(max_audio_bytes=32),
            opener=RecordingOpener(FakeResponse(200, "audio/wav", wav_bytes())),
        )
        with self.assertRaises(LocalPortError):
            client.synthesize("reply", request_id="req-1", session_id="sess-1")

    def test_device_default_is_inert_and_does_not_create_worker(self) -> None:
        factory_calls: list[bool] = []
        device = LocalWindowsDeviceEvidence(worker_factory=lambda: factory_calls.append(True))
        self.assertIsNone(device.worker)
        self.assertEqual(factory_calls, [])
        evidence = device.play_and_capture(b"audio", request_id="req", session_id="sess", batch_id="batch")
        self.assertEqual(evidence.diagnostics["blocked_reason"], "device_live_opt_in_required")
        device.close()

    def test_completed_playback_uses_same_ticket_cleanup_ack(self) -> None:
        worker = FakeWorker(PlaybackState.COMPLETED)
        original_submit = worker.submit

        def submit(audio: bytes, *, metadata: dict[str, str]):
            ticket = original_submit(audio, metadata=metadata)
            ticket.events.append(PlaybackEvent(
                event_id="event-1", ticket_id=ticket.ticket_id,
                state=PlaybackState.COMPLETED, observed_at=0.0,
                backend="fake", stop_ack=True, confirmation="backend_buffer_returned",
            ))
            return ticket

        worker.submit = submit  # type: ignore[method-assign]
        device = LocalWindowsDeviceEvidence(enabled=True, live_opt_in=True, worker=worker)
        evidence = device.play_and_capture(b"audio", request_id="req", session_id="sess", batch_id="batch")
        self.assertTrue(evidence.backend_stop_ack)
        self.assertFalse(evidence.capture_valid)
        device.close()

    def test_completed_playback_does_not_infer_stop_without_capture_facts(self) -> None:
        worker = FakeWorker(PlaybackState.COMPLETED)
        device = LocalWindowsDeviceEvidence(
            enabled=True, live_opt_in=True, worker=worker,
        )
        evidence = device.play_and_capture(b"audio", request_id="req", session_id="sess", batch_id="batch")
        self.assertIsNone(evidence.backend_stop_ack)
        self.assertIsNone(evidence.loopback_output_stopped)
        self.assertIsNone(evidence.acoustic_output_stopped)
        self.assertFalse(evidence.capture_valid)
        device.close()

    def test_timeout_or_unknown_does_not_create_acoustic_stop(self) -> None:
        for state in (PlaybackState.STOP_TIMEOUT, PlaybackState.UNKNOWN):
            with self.subTest(state=state):
                worker = FakeWorker(state)
                device = LocalWindowsDeviceEvidence(enabled=True, live_opt_in=True, worker=worker)
                evidence = device.play_and_capture(b"audio", request_id="req", session_id="sess", batch_id="batch")
                self.assertFalse(evidence.backend_stop_ack)
                self.assertIsNone(evidence.loopback_output_stopped)
                self.assertIsNone(evidence.acoustic_output_stopped)
                self.assertFalse(evidence.capture_valid)
                device.close()

    def test_only_explicit_valid_capture_facts_can_supply_output_stop_fields(self) -> None:
        worker = FakeWorker(PlaybackState.COMPLETED)
        received: list[CaptureRequest] = []

        def capture(request: CaptureRequest) -> DeviceCaptureFacts:
            received.append(request)
            return DeviceCaptureFacts(
                backend_stop_ack=True,
                loopback_output_stopped=True,
                acoustic_output_stopped=True,
                capture_valid=True,
                diagnostics={"independent": True},
            )

        device = LocalWindowsDeviceEvidence(
            enabled=True, live_opt_in=True, worker=worker, capture_hook=capture,
        )
        evidence = device.play_and_capture(b"audio", request_id="req", session_id="sess", batch_id="batch")
        self.assertTrue(evidence.capture_valid)
        self.assertTrue(evidence.loopback_output_stopped)
        self.assertTrue(evidence.acoustic_output_stopped)
        self.assertEqual(received[0].batch_id, "batch")
        device.close()


if __name__ == "__main__":
    unittest.main()
