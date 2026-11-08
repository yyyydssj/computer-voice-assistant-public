from __future__ import annotations

import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.audio import AudioBus, AudioBusDecision, AudioBusState, AudioPacket
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    CaptureSessionState,
    DeviceCheckStatus,
    DeviceInfo,
    DeviceSelection,
    FakeCaptureStream,
    PreRollBuffer,
    SoundDeviceCaptureBackend,
    TemporaryAudioStore,
    TemporaryCleanupStatus,
)
from app.contracts import AudioFrame


class FakeBackend(SoundDeviceCaptureBackend):
    def __init__(self, devices: list[DeviceInfo], default_device: str | None = None) -> None:
        self.devices = devices
        self.default_device = default_device
        self.open_calls: list[dict[str, object]] = []
        self.check_error: Exception | None = None
        self.stream = FakeCaptureStream()

    def list_input_devices(self) -> tuple[DeviceInfo, ...]:
        return tuple(self.devices)

    def default_input_device(self) -> str | None:
        return self.default_device

    def check_input_settings(self, *, device_id: str | None, sample_rate_hz: int, channel_count: int) -> None:
        if self.check_error is not None:
            raise self.check_error

    def open_input_stream(self, **kwargs: object) -> FakeCaptureStream:
        self.open_calls.append(kwargs)
        self.stream.callback = kwargs["callback"]
        return self.stream


class SoundDeviceDefaultDeviceShapeTests(unittest.TestCase):
    """真实设备回归：sounddevice 0.5.x 的默认设备是 _InputOutputPair，不是 list/tuple。"""

    class _Pair:
        """模拟 sounddevice._InputOutputPair：带 input 属性且可下标访问。"""

        def __init__(self, input_index: int, output_index: int) -> None:
            self.input = input_index
            self.output = output_index

        def __getitem__(self, index: int) -> int:
            return (self.input, self.output)[index]

        def __repr__(self) -> str:
            return f"[{self.input}, {self.output}]"

    def backend_for(self, default: object) -> SoundDeviceCaptureBackend:
        module = types.ModuleType("sounddevice")
        module.default = types.SimpleNamespace(device=default)  # type: ignore[attr-defined]
        return module

    def resolve(self, default: object) -> str | None:
        module = self.backend_for(default)
        with patch.dict(sys.modules, {"sounddevice": module}):
            return SoundDeviceCaptureBackend().default_input_device()

    def test_input_output_pair_is_resolved(self) -> None:
        self.assertEqual(self.resolve(self._Pair(1, 4)), "1")
        self.assertEqual(self.resolve(self._Pair(0, 3)), "0")

    def test_plain_index_and_sequence_are_resolved(self) -> None:
        self.assertEqual(self.resolve(2), "2")
        self.assertEqual(self.resolve((3, 5)), "3")
        self.assertEqual(self.resolve([4, 6]), "4")

    def test_negative_or_unresolvable_default_is_fail_closed(self) -> None:
        self.assertIsNone(self.resolve(self._Pair(-1, 4)))
        self.assertIsNone(self.resolve(-1))
        self.assertIsNone(self.resolve("Realtek Microphone"))
        self.assertIsNone(self.resolve(object()))

    def test_tuple_default_without_input_index_is_fail_closed(self) -> None:
        self.assertIsNone(self.resolve(()))


class SoundDeviceArgumentNormalizationTests(unittest.TestCase):
    """真实设备回归：字符串设备 id 必须转成索引，否则 sd 会按名称子串查找并失败。"""

    class _Recorder:
        def __init__(self) -> None:
            self.stream_kwargs: list[dict[str, object]] = []
            self.check_kwargs: list[dict[str, object]] = []

        def InputStream(self, **kwargs: object) -> object:  # noqa: N802 - 模拟 sounddevice API
            self.stream_kwargs.append(kwargs)
            return object()

        def check_input_settings(self, **kwargs: object) -> None:
            self.check_kwargs.append(kwargs)

    def backend_with(self, recorder: "_Recorder") -> SoundDeviceCaptureBackend:
        module = types.ModuleType("sounddevice")
        module.InputStream = recorder.InputStream  # type: ignore[attr-defined]
        module.check_input_settings = recorder.check_input_settings  # type: ignore[attr-defined]
        return module

    def test_digit_device_id_becomes_an_index(self) -> None:
        recorder = self._Recorder()
        with patch.dict(sys.modules, {"sounddevice": self.backend_with(recorder)}):
            backend = SoundDeviceCaptureBackend()
            backend.open_input_stream(device="1", samplerate=16000, channels=1, dtype="int16")
            backend.check_input_settings(device_id="13", sample_rate_hz=16000, channel_count=1)
        self.assertEqual(recorder.stream_kwargs[0]["device"], 1)
        self.assertEqual(recorder.check_kwargs[0]["device"], 13)

    def test_named_device_and_none_are_passed_through(self) -> None:
        recorder = self._Recorder()
        with patch.dict(sys.modules, {"sounddevice": self.backend_with(recorder)}):
            backend = SoundDeviceCaptureBackend()
            backend.open_input_stream(device="Microphone Array", samplerate=16000, channels=1)
            backend.open_input_stream(device=2, samplerate=16000, channels=1)
            backend.check_input_settings(device_id=None, sample_rate_hz=16000, channel_count=1)
        self.assertEqual(recorder.stream_kwargs[0]["device"], "Microphone Array")
        self.assertEqual(recorder.stream_kwargs[1]["device"], 2)
        self.assertIsNone(recorder.check_kwargs[0]["device"])


    def test_incompatible_format_is_a_status_not_an_exception(self) -> None:
        class PortAudioLikeError(Exception):
            """模拟 sounddevice.PortAudioError（不是 OSError/RuntimeError）。"""

        class Recorder:
            default = types.SimpleNamespace(device=0)

            def query_devices(self):
                return [{"name": "Test Mic", "max_input_channels": 2}]

            def check_input_settings(self, **kwargs: object) -> None:
                raise PortAudioLikeError("Invalid sample rate [PaErrorCode -9997]")

        module = types.ModuleType("sounddevice")
        module.query_devices = Recorder().query_devices  # type: ignore[attr-defined]
        module.default = Recorder.default  # type: ignore[attr-defined]
        module.check_input_settings = Recorder().check_input_settings  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"sounddevice": module}):
            check = SoundDeviceCaptureBackend().check_device(
                DeviceSelection.fixed("0"), sample_rate_hz=16000, channel_count=1
            )
        self.assertEqual(check.status, DeviceCheckStatus.INCOMPATIBLE)
        self.assertEqual(check.device_id, "0")
        self.assertIn("sample", check.reason.lower())


class DeviceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.devices = [
            DeviceInfo(device_id="mic-1", name="Test Mic", max_input_channels=1),
            DeviceInfo(device_id="mic-2", name="Other Mic", max_input_channels=1),
        ]

    def test_fixed_selection_does_not_fallback_when_selected_device_is_missing(self) -> None:
        backend = FakeBackend(self.devices, default_device="mic-2")
        check = backend.check_device(DeviceSelection.fixed("missing"), sample_rate_hz=16000, channel_count=1)
        self.assertEqual(check.status, DeviceCheckStatus.UNAVAILABLE)
        self.assertIsNone(check.device_id)
        self.assertEqual(backend.open_calls, [])

    def test_fixed_selection_rejects_inputless_device(self) -> None:
        backend = FakeBackend([DeviceInfo(device_id="output", name="Output", max_input_channels=0)])
        check = backend.check_device(DeviceSelection.fixed("output"), sample_rate_hz=16000, channel_count=1)
        self.assertEqual(check.status, DeviceCheckStatus.INCOMPATIBLE)
        self.assertIn("输入", check.reason)

    def test_system_default_resolves_only_the_current_default(self) -> None:
        backend = FakeBackend(self.devices, default_device="mic-1")
        check = backend.check_device(DeviceSelection.system_default(), sample_rate_hz=16000, channel_count=1)
        self.assertEqual(check.status, DeviceCheckStatus.READY)
        self.assertEqual(check.device_id, "mic-1")

    def test_permission_and_busy_errors_are_explicit(self) -> None:
        for error, expected in (
            (PermissionError("access denied"), DeviceCheckStatus.PERMISSION_DENIED),
            (RuntimeError("device busy"), DeviceCheckStatus.BUSY),
        ):
            with self.subTest(expected=expected):
                backend = FakeBackend(self.devices, default_device="mic-1")
                backend.check_error = error
                check = backend.check_device(DeviceSelection.fixed("mic-1"), sample_rate_hz=16000, channel_count=1)
                self.assertEqual(check.status, expected)


class CaptureSessionTests(unittest.TestCase):
    def make_bus(self) -> AudioBus:
        return AudioBus(
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            device_epoch=1,
            capacity_frames=4,
        )

    def test_unavailable_device_never_opens_stream_or_submits_audio(self) -> None:
        backend = FakeBackend([], default_device=None)
        bus = self.make_bus()
        session = CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("missing"),
            config=CaptureConfig(sample_rate_hz=16000, channel_count=1, frame_duration_ms=20),
        )
        result = session.start()
        self.assertFalse(result.started)
        self.assertEqual(result.check.status, DeviceCheckStatus.UNAVAILABLE)
        self.assertEqual(backend.open_calls, [])
        self.assertEqual(bus.state, AudioBusState.OPEN)

    def test_callback_publishes_audio_and_preroll_keeps_bounded_history(self) -> None:
        backend = FakeBackend([DeviceInfo(device_id="mic-1", name="Test Mic", max_input_channels=1)])
        bus = self.make_bus()
        pre_roll = PreRollBuffer(capacity_frames=1)
        session = CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(sample_rate_hz=16000, channel_count=1, frame_duration_ms=20),
            pre_roll=pre_roll,
        )
        self.assertTrue(session.start().started)
        backend.stream.emit(bytes(640), frames=320)
        backend.stream.emit(bytes([7]) * 640, frames=320)
        self.assertEqual(bus.queued_frames, 2)
        history = pre_roll.snapshot()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].payload[0], 7)
        session.stop(reason="capture_completed", discard=False)
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertTrue(backend.stream.stopped)

    def test_stream_open_failure_fails_closed_without_submitting_audio(self) -> None:
        class OpeningFailureBackend(FakeBackend):
            def open_input_stream(self, **kwargs: object) -> FakeCaptureStream:
                raise RuntimeError("device busy")

        backend = OpeningFailureBackend([DeviceInfo(device_id="mic-1", name="Test Mic", max_input_channels=1)])
        bus = self.make_bus()
        session = CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(sample_rate_hz=16000, channel_count=1, frame_duration_ms=20),
        )
        result = session.start()
        self.assertFalse(result.started)
        self.assertEqual(result.check.status, DeviceCheckStatus.UNAVAILABLE)
        self.assertEqual(session.state, CaptureSessionState.FAILED)
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.queued_frames, 0)

    def test_stream_start_failure_fails_closed(self) -> None:
        class StartFailureStream(FakeCaptureStream):
            def start(self) -> None:
                raise RuntimeError("start failed")

        class StartFailureBackend(FakeBackend):
            def open_input_stream(self, **kwargs: object) -> FakeCaptureStream:
                self.open_calls.append(kwargs)
                stream = StartFailureStream()
                stream.callback = kwargs["callback"]
                return stream

        backend = StartFailureBackend([DeviceInfo(device_id="mic-1", name="Test Mic", max_input_channels=1)])
        bus = self.make_bus()
        session = CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(sample_rate_hz=16000, channel_count=1, frame_duration_ms=20),
        )
        result = session.start()
        self.assertFalse(result.started)
        self.assertEqual(session.state, CaptureSessionState.FAILED)
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.queued_frames, 0)

    def test_callback_status_failure_discards_incomplete_audio(self) -> None:
        backend = FakeBackend([DeviceInfo(device_id="mic-1", name="Test Mic", max_input_channels=1)])
        bus = self.make_bus()
        session = CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(sample_rate_hz=16000, channel_count=1, frame_duration_ms=20),
        )
        self.assertTrue(session.start().started)
        backend.stream.emit(bytes(640), frames=320)
        backend.stream.emit(bytes(640), frames=320, status="input overflow")
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.queued_frames, 0)
        self.assertEqual(session.state, CaptureSessionState.FAILED)

    def test_bad_payload_is_not_submitted_and_capture_fails_closed(self) -> None:
        backend = FakeBackend([DeviceInfo(device_id="mic-1", name="Test Mic", max_input_channels=1)])
        bus = self.make_bus()
        session = CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(sample_rate_hz=16000, channel_count=1, frame_duration_ms=20),
        )
        self.assertTrue(session.start().started)
        backend.stream.emit(bytes(10), frames=320)
        self.assertEqual(bus.queued_frames, 0)
        self.assertEqual(bus.state, AudioBusState.CLOSED)
        self.assertEqual(session.state, CaptureSessionState.FAILED)


class PreRollBufferTests(unittest.TestCase):
    def test_rejects_wrong_stream_identity_and_is_bounded(self) -> None:
        buffer = PreRollBuffer(capacity_frames=2, stream_id="stream-1")
        frame = AudioFrame(
            audio_session_id="audio-1", provider_generation=1, stream_id="stream-1",
            event_sequence=1, frame_index=0, captured_at="2026-09-21T08:00:00Z",
            duration_ms=20,
        )
        other = AudioFrame(
            audio_session_id="audio-1", provider_generation=1, stream_id="other",
            event_sequence=1, frame_index=0, captured_at="2026-09-21T08:00:00Z",
            duration_ms=20,
        )
        buffer.append(AudioPacket(frame, bytes(640)))
        buffer.append(AudioPacket(frame, bytes(640)))
        buffer.append(AudioPacket(frame, bytes([1]) * 640))
        self.assertEqual(len(buffer.snapshot()), 2)
        self.assertEqual(buffer.snapshot()[0].payload[0], 0)
        with self.assertRaises(ValueError):
            buffer.append(AudioPacket(other, bytes(640)))


class TemporaryAudioStoreTests(unittest.TestCase):
    def test_owned_file_is_deleted_immediately_and_foreign_file_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TemporaryAudioStore(root)
            path = store.create(suffix=".pcm")
            store.write(path, bytes(640))
            foreign = root / "foreign.pcm"
            foreign.write_bytes(b"keep")
            self.assertEqual(store.cleanup(path), TemporaryCleanupStatus.DELETED)
            self.assertFalse(path.exists())
            self.assertTrue(foreign.exists())
            self.assertEqual(foreign.read_bytes(), b"keep")

    def test_expired_owned_file_is_cleaned_but_unmarked_file_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TemporaryAudioStore(root, max_age_seconds=60)
            path = store.create(suffix=".pcm")
            store.write(path, b"audio")
            old = (datetime.now(timezone.utc) - timedelta(seconds=61)).timestamp()
            import os
            os.utime(path, (old, old))
            foreign = root / "foreign.pcm"
            foreign.write_bytes(b"keep")
            deleted = store.cleanup_expired(now=datetime.now(timezone.utc))
            self.assertEqual(deleted, (path,))
            self.assertFalse(path.exists())
            self.assertTrue(foreign.exists())

    def test_cleanup_rejects_unowned_path_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TemporaryAudioStore(root)
            foreign = root / "foreign.wav"
            foreign.write_bytes(b"keep")
            self.assertEqual(store.cleanup(foreign), TemporaryCleanupStatus.NOT_OWNED)
            self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
