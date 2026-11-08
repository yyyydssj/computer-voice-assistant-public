from __future__ import annotations

import json
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

from app.agent import AgentTurnOutcome
from app.asr import (
    AsrEvent,
    AsrEventKind,
    AsrFailureCode,
    AsrProviderUnavailable,
    AsrFailureStage,
    AsrRecoveryState,
    AsrSessionBoundary,
    DetectionStatus,
    PushToTalkCoordinator,
    PushToTalkDiagnostic,
    ResultGateDecision,
    SenseVoiceConfig,
    SenseVoiceRecognizerAdapter,
)
from app.audio.capture import (
    CaptureConfig,
    CaptureSession,
    DeviceCheckResult,
    DeviceCheckStatus,
    DeviceInfo,
    DeviceSelection,
    FakeCaptureStream,
    TemporaryAudioStore,
)
from app.audio.vad import VadConfig, VadSession
from app.auditing import AuditStore
from app.conversation import ConversationManager
from app.logging import StructuredLogger
from tests.integration.test_p5_integration import _build_plan, _build_reviewable_plan


class _ConversationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.calls.append((request_id, session_id, text))
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"已接收：{text}",
            finish_reason="stop",
        )

    def close(self) -> None:
        pass


class _CaptureBackend:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.stream = FakeCaptureStream()

    def check_device(self, selection, *, sample_rate_hz: int, channel_count: int) -> DeviceCheckResult:
        del selection, sample_rate_hz, channel_count
        if not self.ready:
            return DeviceCheckResult(DeviceCheckStatus.UNAVAILABLE, None, "sensitive device detail")
        return DeviceCheckResult(DeviceCheckStatus.READY, "mic-1", "ready")

    def open_input_stream(self, **kwargs: object) -> FakeCaptureStream:
        self.stream.callback = kwargs["callback"]
        return self.stream


class _ProviderStream:
    def __init__(self, text: str) -> None:
        self.result = types.SimpleNamespace(text=text)
        self.waveforms: list[tuple[int, list[float]]] = []

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        self.waveforms.append((sample_rate, samples))


class _Recognizer:
    def __init__(self, text: str, *, fail_decode: bool = False) -> None:
        self.text = text
        self.fail_decode = fail_decode
        self.streams: list[_ProviderStream] = []

    def create_stream(self) -> _ProviderStream:
        stream = _ProviderStream(self.text)
        self.streams.append(stream)
        return stream

    def decode(self, stream: _ProviderStream) -> None:
        del stream
        if self.fail_decode:
            raise RuntimeError(r"secret pcm G:\private\voice.pcm")


class _OfflineRecognizer:
    recognizer = _Recognizer("打开编辑器")

    @classmethod
    def from_sense_voice(cls, **kwargs: object) -> _Recognizer:
        del kwargs
        return cls.recognizer


class _DiagnosticRecorder:
    def __init__(self, *, logger: StructuredLogger | None = None, audit: AuditStore | None = None) -> None:
        self.items: list[PushToTalkDiagnostic] = []
        self.logger = logger
        self.audit = audit
        self.sequence = 0

    def __call__(self, item: PushToTalkDiagnostic) -> None:
        self.items.append(item)
        self.sequence += 1
        details = item.to_details()
        if self.logger is not None:
            self.logger.write(
                item.event_type,
                instance_id="instance-1",
                event_sequence=self.sequence,
                details=details,
            )
        if self.audit is not None:
            self.audit.append(
                item.event_type,
                subject_id=item.subject_id,
                details=details,
                created_at="2026-09-21T14:00:00Z",
            )


class P6IntegrationTests(unittest.TestCase):
    def make_coordinator(
        self,
        *,
        result_text: str = "打开编辑器",
        capture_ready: bool = True,
        provider_load_fail: bool = False,
        fail_decode: bool = False,
        temporary_store: TemporaryAudioStore | None = None,
        diagnostic_sink=None,
    ) -> tuple[PushToTalkCoordinator, _ConversationAdapter, list[_CaptureBackend]]:
        _OfflineRecognizer.recognizer = _Recognizer(result_text, fail_decode=fail_decode)
        module = types.ModuleType("sherpa_onnx")
        module.OfflineRecognizer = _OfflineRecognizer  # type: ignore[attr-defined]
        def load_module():
            if provider_load_fail:
                raise RuntimeError("secret provider startup failure")
            return module

        provider = SenseVoiceRecognizerAdapter(
            SenseVoiceConfig(model_path=Path("model.onnx"), tokens_path=Path("tokens.txt")),
            module_loader=load_module,
        )
        adapters: list[_CaptureBackend] = []

        def capture_factory(bus):
            backend = _CaptureBackend(ready=capture_ready)
            adapters.append(backend)
            return CaptureSession(
                bus=bus,
                backend=backend,
                selection=DeviceSelection.fixed("mic-1"),
                config=CaptureConfig(),
            )

        def vad_factory(bus):
            return VadSession(bus=bus, config=VadConfig(silence_duration_ms=200), detector=lambda packet: True)

        conversation_adapter = _ConversationAdapter()
        coordinator = PushToTalkCoordinator(
            conversation=ConversationManager(conversation_adapter),
            provider=provider,
            capture_factory=capture_factory,
            vad_factory=vad_factory,
            source_instance_id="instance-1",
            device_epoch=1,
            audio_capacity_frames=16,
            temporary_audio_store=temporary_store,
            diagnostic_sink=diagnostic_sink,
        )
        return coordinator, conversation_adapter, adapters

    @staticmethod
    def boundary(index: int, *, generation: int | None = None) -> AsrSessionBoundary:
        value = str(index)
        return AsrSessionBoundary(
            session_id=f"voice-session-{value}",
            audio_session_id=f"audio-session-{value}",
            provider_generation=generation or index,
            stream_id=f"stream-{value}",
            utterance_id=f"utterance-{value}",
        )

    def test_capture_vad_provider_gate_recovery_submit_one_request_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TemporaryAudioStore(Path(temp_dir) / "audio")
            temp_audio = store.create()
            store.write(temp_audio, b"private-pcm")
            coordinator, adapter, backends = self.make_coordinator(temporary_store=store)
            coordinator.startup_succeeded()

            started = coordinator.start(self.boundary(1), temporary_audio_path=temp_audio)
            self.assertTrue(started.started)
            backends[0].stream.emit(bytes(640), frames=320)
            self.assertEqual(coordinator.pump().processed_frames, 1)
            completed = coordinator.release()

            self.assertEqual(completed.gate_result.decision, ResultGateDecision.ACCEPTED_FINAL)
            self.assertIsNotNone(completed.outcome)
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(adapter.calls[0][2], "打开编辑器")
            self.assertEqual(coordinator.recovery.state, AsrRecoveryState.READY)
            self.assertFalse(temp_audio.exists())
            self.assertFalse(temp_audio.with_name(temp_audio.name + ".owner").exists())

            replay = coordinator.accept_provider_event(completed.provider_event)
            self.assertEqual(replay.gate_result.decision, ResultGateDecision.DUPLICATE)
            self.assertEqual(len(adapter.calls), 1)

    def test_release_drains_queued_frames_before_decoding(self) -> None:
        """收尾前必须把总线里已采集的帧全部喂给 provider，否则语音尾巴在解码前被丢掉。"""

        coordinator, adapter, backends = self.make_coordinator()
        coordinator.startup_succeeded()
        self.assertTrue(coordinator.start(self.boundary(1)).started)
        # 采集 3 帧但**故意不 pump**：只有 release() 的排空路径能把它们送进 provider。
        for _ in range(3):
            backends[0].stream.emit(bytes(640), frames=320)
        completed = coordinator.release()

        self.assertEqual(completed.gate_result.decision, ResultGateDecision.ACCEPTED_FINAL)
        recognizer = _OfflineRecognizer.recognizer
        self.assertEqual(len(recognizer.streams), 1)
        self.assertEqual(len(recognizer.streams[0].waveforms), 3)
        self.assertEqual(len(adapter.calls), 1)

    def test_duplicate_late_old_boundary_and_fault_race_never_create_second_request(self) -> None:
        coordinator, adapter, backends = self.make_coordinator()
        coordinator.startup_succeeded()
        coordinator.start(self.boundary(1))
        backends[0].stream.emit(bytes(640), frames=320)
        coordinator.pump()
        first = coordinator.release()
        event = first.provider_event
        assert event is not None
        self.assertEqual(len(adapter.calls), 1)

        coordinator.start(self.boundary(2, generation=2))
        stale = replace(event, event_id="old-provider-event")
        rejected = coordinator.accept_provider_event(stale)
        self.assertNotEqual(rejected.gate_result.decision, ResultGateDecision.ACCEPTED_FINAL)
        self.assertEqual(len(adapter.calls), 1)

        backends[1].stream.emit(bytes(640), frames=320)
        coordinator.pump()
        coordinator.runtime_failed(code=AsrFailureCode.STREAM_FAILED)
        late = AsrEvent(
            event_id="late-current-event",
            kind=AsrEventKind.FINAL,
            audio_session_id="audio-session-2",
            provider_generation=2,
            stream_id="stream-2",
            utterance_id="utterance-2",
            event_sequence=1,
            text="不应提交",
            is_final=True,
            overlap_detection=DetectionStatus.UNSUPPORTED,
            speaker_identity=DetectionStatus.UNSUPPORTED,
            confidence=DetectionStatus.UNSUPPORTED,
            provider_timestamp="2026-09-21T14:00:00Z",
            received_at="2026-09-21T14:00:00Z",
        )
        raced = coordinator.accept_provider_event(late)
        self.assertEqual(raced.gate_result.decision, ResultGateDecision.AFTER_TERMINAL)
        self.assertEqual(len(adapter.calls), 1)

    def test_provider_startup_failure_falls_back_to_text_without_exception_body(self) -> None:
        coordinator, adapter, _ = self.make_coordinator(provider_load_fail=True)
        coordinator.startup_succeeded()

        self.assertEqual(coordinator.recovery.state, AsrRecoveryState.UNAVAILABLE_STARTUP)
        self.assertEqual(coordinator.recovery.input_mode.value, "text")
        self.assertIsNotNone(coordinator.recovery.last_failure)
        assert coordinator.recovery.last_failure is not None
        self.assertEqual(coordinator.recovery.last_failure.code, AsrFailureCode.PROVIDER_UNAVAILABLE)
        self.assertNotIn("secret provider startup failure", coordinator.recovery.public_status)

        outcome = coordinator.submit_text("Provider 失败后继续文字")
        self.assertTrue(outcome.completed)
        self.assertEqual(adapter.calls[-1][2], "Provider 失败后继续文字")

    def test_startup_and_warmup_failures_keep_same_text_conversation_available(self) -> None:
        coordinator, adapter, _ = self.make_coordinator(capture_ready=False)
        coordinator.startup_failed(code=AsrFailureCode.TIMEOUT)
        text_outcome = coordinator.submit_text("文字任务")
        self.assertTrue(text_outcome.completed)
        self.assertEqual(adapter.calls[-1][2], "文字任务")

        second, second_adapter, _ = self.make_coordinator(capture_ready=False)
        second.startup_succeeded()
        failed = second.start(self.boundary(1))
        self.assertFalse(failed.started)
        self.assertEqual(second.recovery.input_mode.value, "text")
        fallback = second.submit_text("继续使用文字")
        self.assertTrue(fallback.completed)
        self.assertEqual(second_adapter.calls[-1][2], "继续使用文字")

    def test_asr_failure_cannot_mutate_plan_authorization_execution_or_result_facts(self) -> None:
        _registry, action = _build_plan()
        plan = _build_reviewable_plan(action, plan_id="plan-p6-stable")
        facts = {
            "task_id": plan.task_id,
            "plan_hash": plan.plan_hash,
            "authorization_id": "authorization-stable",
            "execution_id": "execution-stable",
            "result_id": "result-stable",
            "execution_count": 0,
        }
        before = json.dumps(facts, sort_keys=True, ensure_ascii=False)

        coordinator, adapter, backends = self.make_coordinator()
        coordinator.startup_succeeded()
        coordinator.start(self.boundary(1))
        backends[0].stream.emit(bytes(640), frames=320)
        coordinator.pump()
        coordinator.runtime_failed(code=AsrFailureCode.DECODE_FAILED)

        self.assertEqual(json.dumps(facts, sort_keys=True, ensure_ascii=False), before)
        self.assertEqual(adapter.calls, [])
        text = coordinator.submit_text("继续既有文字安全链")
        self.assertTrue(text.completed)
        self.assertEqual(facts["execution_count"], 0)

    def test_temp_audio_pcm_and_exception_body_never_enter_diagnostics_logs_or_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            logger = StructuredLogger(root / "logs")
            audit = AuditStore(root / "audit", source_instance_id="instance-1")
            recorder = _DiagnosticRecorder(logger=logger, audit=audit)
            store = TemporaryAudioStore(root / "audio")
            temp_audio = store.create()
            secret_payload = (b"supersecret-pcm" * 50)[:640].ljust(640, b"x")
            store.write(temp_audio, secret_payload)
            coordinator, adapter, backends = self.make_coordinator(
                fail_decode=True,
                temporary_store=store,
                diagnostic_sink=recorder,
            )
            coordinator.startup_succeeded()
            coordinator.start(self.boundary(1), temporary_audio_path=temp_audio)
            backends[0].stream.emit(secret_payload, frames=320)
            coordinator.pump()
            outcome = coordinator.release()

            self.assertIsNone(outcome.outcome)
            self.assertEqual(adapter.calls, [])
            combined = (logger.log_path.read_text(encoding="utf-8") + audit.path.read_text(encoding="utf-8"))
            self.assertNotIn(str(temp_audio), combined)
            self.assertNotIn("supersecret", combined)
            self.assertNotIn("G:\\private", combined)
            self.assertNotIn("打开编辑器", combined)
            self.assertTrue(recorder.items)
            for item in recorder.items:
                self.assertNotIn("text", item.to_details())
                self.assertNotIn("path", item.to_details())
                self.assertNotIn("audio", item.to_details())


if __name__ == "__main__":
    unittest.main()
