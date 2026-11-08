"""P8 集成：真实 P6 按键管道 + P8 闭环编排 + 任务路径（离线 fake 设备/Provider）。

本测试不联网、不读密钥、不打开真实麦克风/扬声器，也不执行真实电脑副作用。
它验证：真实 PushToTalkCoordinator 产出的 accepted final 使用 P8 轮次身份提交给
同一个 ConversationManager；闲聊只播报完整回复；task 路由进入本地冻结计划、
L2 授权、复检、受控执行、验证与摘要播报的完整链路。
"""

from __future__ import annotations

import types
import unittest
from dataclasses import dataclass
from pathlib import Path

from app.asr import PushToTalkCoordinator, SenseVoiceConfig, SenseVoiceRecognizerAdapter
from app.audio.capture import CaptureConfig, CaptureSession, DeviceSelection
from app.audio.vad import VadConfig, VadSession
from app.conversation import ConversationManager
from app.contracts.routing import RouteHandoff, RouterStatus
from app.runtime.assistant_runtime import LocalChatRouter
from app.runtime.orchestration import TaskFlowError
from app.voice_loop import (
    ChatVoiceLoop,
    PushToTalkVoiceDriver,
    VoiceInputSeam,
    VoiceLoopError,
    VoiceLoopState,
    VoiceLoopStateMachine,
    VoiceTurnRegistry,
)
from tests.integration.test_p6_integration import (
    _CaptureBackend,
    _ConversationAdapter,
    _OfflineRecognizer,
    _Recognizer,
)
from tests.orchestration.test_task_flow import (
    authorization_request,
    build_orchestrator,
    plan_request,
    previewer,
)
from tests.voice_loop.support import FIXED_NOW, FakeSpeaker, FakeStream


@dataclass
class VoicePipeline:
    loop: ChatVoiceLoop
    coordinator: PushToTalkCoordinator
    adapter: _ConversationAdapter
    backends: list[_CaptureBackend]
    seam: VoiceInputSeam
    speaker: FakeSpeaker
    stream: FakeStream
    conversation: ConversationManager


def build_pipeline(
    *,
    result_text: str = "打开编辑器",
    capture_ready: bool = True,
    provider_load_fail: bool = False,
    fail_decode: bool = False,
    router: object | None = None,
    router_model_id: str = "chat-only-router",
) -> VoicePipeline:
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
    backends: list[_CaptureBackend] = []

    def capture_factory(bus):
        backend = _CaptureBackend(ready=capture_ready)
        backends.append(backend)
        return CaptureSession(
            bus=bus,
            backend=backend,
            selection=DeviceSelection.fixed("mic-1"),
            config=CaptureConfig(),
        )

    def vad_factory(bus):
        return VadSession(
            bus=bus,
            config=VadConfig(silence_duration_ms=200),
            detector=lambda packet: True,
        )

    adapter = _ConversationAdapter()
    conversation = ConversationManager(
        adapter,
        router=router or LocalChatRouter(),
        router_provider="local",
        router_model_id=router_model_id,
        router_model_version="1",
    )
    coordinator = PushToTalkCoordinator(
        conversation=conversation,
        provider=provider,
        capture_factory=capture_factory,
        vad_factory=vad_factory,
        source_instance_id="instance-1",
        device_epoch=1,
        audio_capacity_frames=16,
    )
    registry = VoiceTurnRegistry(source_instance_id="instance-1", now=lambda: FIXED_NOW)
    machine = VoiceLoopStateMachine(registry)
    seam = VoiceInputSeam(conversation=conversation, registry=registry)
    stream = FakeStream()
    speaker = FakeSpeaker(stream)
    loop = ChatVoiceLoop(
        registry=registry,
        machine=machine,
        seam=seam,
        driver=PushToTalkVoiceDriver(coordinator=coordinator),
        speaker=speaker,
    )
    return VoicePipeline(
        loop=loop,
        coordinator=coordinator,
        adapter=adapter,
        backends=backends,
        seam=seam,
        speaker=speaker,
        stream=stream,
        conversation=conversation,
    )


def enable_voice(pipeline: VoicePipeline) -> None:
    """把 ASR 就绪事实写入接缝，并走一次用户明确请求的切回语音流程。"""

    pipeline.coordinator.startup_succeeded()
    pipeline.seam.note_voice_ready()
    switched = pipeline.seam.request_voice_switch(local_check_passed=True)
    assert switched.switched, switched.reason


def drive_one_voice_turn(pipeline: VoicePipeline):
    """一次按键语音轮次：开始 → 推进音频 → 结束收音 → 提交。"""

    boundary = pipeline.loop.start_turn()
    assert boundary is not None
    pipeline.backends[-1].stream.emit(bytes(640), frames=320)
    pipeline.loop.pump()
    pipeline.loop.release_input()
    return boundary, pipeline.loop.complete_turn()


class P8IntegrationTests(unittest.TestCase):
    @staticmethod
    def enable_voice(pipeline: VoicePipeline) -> None:
        enable_voice(pipeline)

    def test_button_voice_turn_uses_p8_identity_and_speaks_the_reply(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        boundary, report = drive_one_voice_turn(pipeline)

        self.assertTrue(report.accepted)
        self.assertTrue(report.submitted)
        self.assertTrue(report.spoken)
        self.assertEqual(len(pipeline.adapter.calls), 1)
        request_id, _session_id, text = pipeline.adapter.calls[0]
        self.assertEqual(request_id, boundary.request_id)
        self.assertEqual(text, "打开编辑器")
        self.assertEqual(pipeline.stream.chunks, ["已接收：打开编辑器"])
        self.assertEqual(pipeline.stream.finished, 1)
        self.assertEqual(pipeline.stream.cancelled, 0)
        self.assertEqual(pipeline.speaker.starts, 1)
        self.assertEqual(pipeline.loop.state, VoiceLoopState.IDLE)
        self.assertEqual(pipeline.loop.last_report.boundary.identity, boundary.identity)

    def test_each_button_turn_gets_fresh_identity_and_one_submission(self) -> None:
        pipeline = build_pipeline()
        enable_voice(pipeline)
        boundaries = []
        for _ in range(2):
            boundary, report = drive_one_voice_turn(pipeline)
            boundaries.append(boundary)
            self.assertTrue(report.submitted)

        self.assertEqual(len(pipeline.adapter.calls), 2)
        self.assertEqual(len({call[0] for call in pipeline.adapter.calls}), 2)
        self.assertNotEqual(boundaries[0].turn_id, boundaries[1].turn_id)
        self.assertEqual(len(pipeline.stream.chunks), 2)

    def test_asr_startup_failure_switches_to_text_without_changing_the_agent(self) -> None:
        pipeline = build_pipeline(provider_load_fail=True)
        enable_voice(pipeline)
        self.assertEqual(pipeline.coordinator.recovery.input_mode.value, "text")

        pipeline.seam.degrade(reason="asr_startup_failed")
        with self.assertRaises(VoiceLoopError):
            pipeline.loop.start_turn()

        text_boundary = pipeline.seam.open_text_turn()
        submission = pipeline.seam.submit(text_boundary, "用文字继续说")
        self.assertTrue(submission.submitted)
        self.assertEqual(len(pipeline.adapter.calls), 1)
        self.assertEqual(pipeline.adapter.calls[0][2], "用文字继续说")
        self.assertEqual(pipeline.stream.chunks, [])
        self.assertEqual(pipeline.loop.state, VoiceLoopState.IDLE)

    def test_decode_failure_never_submits_or_speaks_and_text_still_works(self) -> None:
        pipeline = build_pipeline(fail_decode=True)
        enable_voice(pipeline)
        _boundary, report = drive_one_voice_turn(pipeline)

        self.assertFalse(report.accepted)
        self.assertFalse(report.submitted)
        self.assertFalse(report.spoken)
        self.assertEqual(pipeline.adapter.calls, [])
        self.assertEqual(pipeline.stream.chunks, [])
        self.assertEqual(pipeline.loop.state, VoiceLoopState.IDLE)

        pipeline.seam.degrade(reason="asr_session_failed")
        fallback = pipeline.seam.open_text_turn()
        self.assertTrue(pipeline.seam.submit(fallback, "文字兜底").submitted)
        self.assertEqual(pipeline.adapter.calls[-1][2], "文字兜底")


class TaskIntentRouter:
    """测试用本地路由器：把一次 ASR final 判为 task 路由。

    真实 IntentRouter 属于 P7-R 支线；本类只提供同样的 provider-neutral 契约，
    使 P8 的「ASR → 统一会话入口 → RouteHandoff(task)」接缝可离线验证。
    """

    def __init__(self, tool_scope: tuple[str, ...] = ("start_whitelisted_application",)) -> None:
        self._tool_scope = tool_scope

    def route(self, request):
        return RouteHandoff(
            trace=request.trace,
            route="task",
            intent="open_application",
            handling_mode="plan_then_execute",
            confidence=1.0,
            slots={},
            required_confirmation="defer_to_safety",
            tool_scope=self._tool_scope,
            router=RouterStatus(
                provider="local",
                model_id="test-task-router",
                model_version="1",
                status="success",
                failure_code=None,
            ),
        )


def build_task_pipeline() -> VoicePipeline:
    return build_pipeline(router=TaskIntentRouter(), router_model_id="test-task-router")


class P8TaskPathIntegrationTests(unittest.TestCase):
    def test_asr_task_route_reaches_the_full_task_chain_and_speaks_a_summary(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        boundary, chat_report = drive_one_voice_turn(pipeline)

        # 任务路由只记录 RouteHandoff，不产生闲聊回复，也不播报半截对话。
        self.assertTrue(chat_report.accepted)
        self.assertTrue(chat_report.submitted)
        self.assertFalse(chat_report.spoken)
        self.assertEqual(pipeline.stream.chunks, [])
        self.assertEqual(pipeline.adapter.calls, [])
        handoff = pipeline.conversation.route_results[-1].handoff
        self.assertEqual(handoff.route, "task")
        self.assertEqual(handoff.trace.request_id, boundary.request_id)

        # Agent 侧结构化提议（本批次以注入实现代表）→ 本地冻结 → 授权 → 执行 → 汇报。
        preview = previewer().preview(plan_request(handoff_value=handoff))
        orchestrator, backend, audit = build_orchestrator(speaker=FakeSpeaker(pipeline.stream))
        orchestrator.accept_preview(preview)
        self.assertIn("start_whitelisted_application", orchestrator.announce_plan())
        authorization = orchestrator.authorize(authorization_request(preview))
        self.assertEqual(authorization.status, "authorized")
        execution = orchestrator.execute(
            execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
        )
        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.ledger_status, "COMPLETED")
        report = orchestrator.report()

        self.assertEqual(report.status, "completed")
        self.assertTrue(report.spoken)
        self.assertIn("任务执行完成", report.text)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(
            audit.events,
            ["execution_started", "operation_started", "operation_result", "execution_finished"],
        )
        self.assertEqual(pipeline.adapter.calls, [])

    def test_task_route_is_not_executed_without_authorization(self) -> None:
        pipeline = build_task_pipeline()
        enable_voice(pipeline)
        _boundary, chat_report = drive_one_voice_turn(pipeline)
        self.assertTrue(chat_report.submitted)
        handoff = pipeline.conversation.route_results[-1].handoff

        preview = previewer().preview(plan_request(handoff_value=handoff))
        orchestrator, backend, _ = build_orchestrator()
        orchestrator.accept_preview(preview)
        with self.assertRaises(TaskFlowError) as ctx:
            orchestrator.execute(
                execution_id="execution-1", executor_instance_id="executor-1", result_id="result-1"
            )
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(backend.calls, 0)
        denied = orchestrator.authorize(authorization_request(preview, user_decision="deny"))
        self.assertEqual(denied.status, "denied")
        self.assertIsNone(orchestrator.authorization)


if __name__ == "__main__":
    unittest.main()
