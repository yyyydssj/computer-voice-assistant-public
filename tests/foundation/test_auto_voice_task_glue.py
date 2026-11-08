"""P10 · T1 批次 3 红测：`AutoVoiceSession` 里任务轮的**接线**（薄胶水，离线）。

只测胶水本身：`_handle_task_turn` 的入口判据、`_task_reply_provider` 产出的**播报文本**、
事实与 reason 覆盖、以及"轮次之外的通知"（窗口过期）走 `speak_notice`。
任务逻辑本身在 `tests/orchestration/test_voice_task_turn.py`（含真实派发会话）。

真机教训（2026-10-13）：任务文本必须在**本轮 REPLYING 内**播报；
旧实现（轮次外调 `_speak`）在真机上表现为"计划只打印、一个字都没念"。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.routing import RouteInput
from app.runtime.auto_voice import AutoVoiceSession, _KeyboardCancellableSpeaker
from app.runtime.orchestration.local_task_router import LocalTaskRouter
from app.runtime.orchestration.pending_confirmation import PendingConfirmationRouter
from app.runtime.orchestration.task_dispatch import (
    DENIED_TEXT,
    NO_PENDING_TASK_TEXT,
    TIMEOUT_TEXT,
)
from app.runtime.orchestration.voice_task_turn import (
    KIND_AWAITING,
    KIND_CANCELLED,
    KIND_EXECUTED,
    KIND_UNSUPPORTED,
    VoiceTaskTurn,
)
from tests.orchestration.test_voice_task_turn import _FakeDispatcher

CREATED_AT = "2026-10-13T00:00:00Z"


def _trace(text: str) -> RouteTrace:
    return RouteTrace(
        request_id="req-1", conversation_id="conv-1", turn_id="turn-1", source="asr",
        raw_text=text, created_at=CREATED_AT,
    )


class _StubFallback:
    def route(self, request: RouteInput) -> RouteHandoff:
        return RouteHandoff(
            trace=request.trace, route="chat", intent="general_chat", handling_mode="chat_reply",
            confidence=1.0, slots={}, required_confirmation="none", tool_scope=(),
            router=RouterStatus(
                provider="stub", model_id="stub", model_version="1", status="success", failure_code=None
            ),
        )


class _Loop:
    """只记录通知播报（任务文本在轮次内由 `complete_turn(reply_provider=…)` 播）。"""

    def __init__(self, *, raise_on_notice: bool = False) -> None:
        self.notices: list[str] = []
        self.raise_on_notice = raise_on_notice

    def speak_notice(self, text: str) -> object:
        if self.raise_on_notice:
            raise RuntimeError("TTS 崩了")
        self.notices.append(text)
        return (True, "speech_finished")


class _Clock:
    """可推进的假时钟（避免依赖真实 `time.monotonic` 的毫秒分辨率）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class AutoVoiceTaskGlueTests(unittest.TestCase):
    def _session(
        self,
        *,
        accepted: object = "打开计算器",
        voice_task: object | None = None,
        loop: object | None = None,
        clock: object | None = None,
    ):
        router = PendingConfirmationRouter(
            first=LocalTaskRouter(), fallback=_StubFallback(), clock=clock or _Clock()
        )
        dispatcher = _FakeDispatcher()
        session = object.__new__(AutoVoiceSession)
        session._voice_task = (  # type: ignore[attr-defined]
            voice_task if voice_task is not None else VoiceTaskTurn(router=router, dispatcher=dispatcher)
        )
        session._config = SimpleNamespace(task_router=router, task_dispatch=dispatcher)  # type: ignore[attr-defined]
        session._driver = SimpleNamespace(  # type: ignore[attr-defined]
            last_result=SimpleNamespace(accepted_text=accepted)
        )
        session._loop = loop or _Loop()  # type: ignore[attr-defined]
        session._notify_sink = None  # type: ignore[attr-defined]
        session._task_spoken = False  # type: ignore[attr-defined]
        session._task_outcomes = []  # type: ignore[attr-defined]
        return session, router, dispatcher

    def _task_handoff(self, text: str = "打开计算器") -> RouteHandoff:
        handoff = LocalTaskRouter().route(RouteInput(raw_text=text, trace=_trace(text)))
        assert handoff is not None
        return handoff

    def test_task_turn_returns_outcomes_without_speaking_itself(self) -> None:
        session, router, _dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001 - 直接构造接线场景
        outcomes = session._handle_task_turn(None)  # type: ignore[attr-defined]
        self.assertEqual([item.kind for item in outcomes], [KIND_AWAITING])
        self.assertIn("请说「确认」执行", outcomes[0].spoken_text or "")
        self.assertEqual(session._loop.notices, [])  # 自己不播报（由轮次内 reply_provider 播）
        self.assertEqual(session._task_outcomes, [])  # provider 还没跑

    def test_reply_provider_returns_the_announcement_and_records_outcomes(self) -> None:
        session, router, _dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        provider = session._task_reply_provider(None)  # type: ignore[attr-defined]
        text = provider()
        self.assertIsInstance(text, str)
        assert isinstance(text, str)
        self.assertIn("风险等级 L2", text)
        self.assertIn("请说「确认」执行", text)
        self.assertEqual(len(session._task_outcomes), 1)  # type: ignore[attr-defined]
        facts = session._task_facts(session._task_outcomes)  # type: ignore[attr-defined]
        assert facts is not None
        self.assertEqual(facts["task_kind"], KIND_AWAITING)
        self.assertEqual(
            session._task_reason_override(session._task_outcomes),  # type: ignore[attr-defined]
            "task_awaiting_confirmation",
        )

    def test_unsupported_route_is_spoken_and_printed_in_turn(self) -> None:
        """P10 · T1 #12：`unsupported` 路由经 `reply_provider` 播短句、控制台看全文。"""

        session, router, _dispatcher = self._session(accepted="关闭计算器")
        printed: list[str] = []
        session._notify_sink = printed.append  # type: ignore[attr-defined]
        handoff = LocalTaskRouter().route(
            RouteInput(raw_text="关闭计算器", trace=_trace("关闭计算器"))
        )
        assert handoff is not None and handoff.route == "unsupported"
        router._last_handoff = handoff  # noqa: SLF001

        provider = session._task_reply_provider(None)  # type: ignore[attr-defined]
        text = provider()
        self.assertEqual(text, "这条任务本批暂不支持，没有执行。")
        self.assertEqual([item.kind for item in session._task_outcomes], [KIND_UNSUPPORTED])  # type: ignore[attr-defined]
        self.assertTrue(printed)
        self.assertIn("本批暂不支持", printed[-1])
        self.assertIn("close_application", printed[-1])
        self.assertIsNone(router.pending)          # 不 arm
        facts = session._task_facts(session._task_outcomes)  # type: ignore[attr-defined]
        assert facts is not None
        self.assertEqual(facts["task_reason"], "unimplemented_capability")

    def test_window_is_rearmed_only_after_an_awaiting_outcome(self) -> None:
        """#15：播报结束后（`_complete_turn_with_hud` 返回后）把待确认窗口重新计时。"""

        clock = _Clock()
        session, router, _dispatcher = self._session(clock=clock)
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        outcomes = session._handle_task_turn(None)  # type: ignore[attr-defined]
        self.assertEqual([item.kind for item in outcomes], [KIND_AWAITING])
        opened_at = clock.now

        clock.now += 13.0  # 模拟 13 s 的计划播报（#15：这段时间不应吃窗口）
        session._task_outcomes = list(outcomes)  # type: ignore[attr-defined]
        session._rearm_task_window()  # type: ignore[attr-defined]

        pending = router.pending
        assert pending is not None
        self.assertEqual(pending.armed_at, clock.now)
        self.assertAlmostEqual(pending.deadline, clock.now + 20.0, places=6)
        self.assertGreater(pending.deadline, opened_at + 20.0)

    def test_window_is_not_rearmed_for_other_outcomes(self) -> None:
        clock = _Clock()
        session, router, _dispatcher = self._session(clock=clock)
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined]
        pending = router.pending
        assert pending is not None
        before = pending.deadline

        clock.now += 13.0
        session._task_outcomes = [  # type: ignore[attr-defined]
            SimpleNamespace(kind="cancelled"),
            SimpleNamespace(kind="executed"),
        ]
        session._rearm_task_window()  # type: ignore[attr-defined]

        current = router.pending
        assert current is not None
        self.assertEqual(current.deadline, before)

    def test_window_rearm_is_a_noop_for_a_router_without_rearm(self) -> None:
        session, _router, _dispatcher = self._session()
        session._config = SimpleNamespace(task_router=SimpleNamespace(), task_dispatch=None)  # type: ignore[attr-defined]
        session._task_outcomes = [SimpleNamespace(kind=KIND_AWAITING)]  # type: ignore[attr-defined]
        session._rearm_task_window()  # type: ignore[attr-defined] - 不抛即可

    def test_reply_provider_returns_none_for_chat_turns(self) -> None:
        session, _router, _dispatcher = self._session()
        provider = session._task_reply_provider(None)  # type: ignore[attr-defined]
        self.assertIsNone(provider())
        self.assertEqual(session._task_outcomes, [])  # type: ignore[attr-defined]

    def test_turn_without_accepted_text_never_touches_the_task(self) -> None:
        session, router, _dispatcher = self._session(accepted=None)
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        provider = session._task_reply_provider(None)  # type: ignore[attr-defined]
        self.assertIsNone(provider())
        self.assertIsNone(session._task_facts([]))  # type: ignore[attr-defined]
        self.assertIsNone(router.pending)

    def test_task_layer_without_injection_is_a_noop(self) -> None:
        session, router, _dispatcher = self._session(voice_task=None)
        session._voice_task = None  # type: ignore[attr-defined]
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        self.assertEqual(session._handle_task_turn(None), [])  # type: ignore[attr-defined]
        self.assertIsNone(session._expire_task_turn(None))  # type: ignore[attr-defined]

    def test_notice_speech_failure_keeps_the_facts(self) -> None:
        """轮次之外的通知（窗口过期）：播报失败也要留下真实事实。"""

        session, router, dispatcher = self._session(loop=_Loop(raise_on_notice=True))
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined] - 先把待确认 arm 起来
        pending = router.pending
        assert pending is not None
        router._pending = type(pending)(  # noqa: SLF001 - 直接把窗口推到过期
            handoff=pending.handoff, armed_at=pending.armed_at, deadline=0.0
        )
        outcome = session._expire_task_turn(None)  # type: ignore[attr-defined]
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.reason, "task_cancelled")
        self.assertFalse(session._task_spoken)  # type: ignore[attr-defined] - 播报失败如实记录
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertIsNone(session._voice_task.pending)  # type: ignore[attr-defined]

    def test_expired_pending_is_announced_through_speak_notice(self) -> None:
        session, router, dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined]
        pending = router.pending
        assert pending is not None
        router._pending = type(pending)(  # noqa: SLF001
            handoff=pending.handoff, armed_at=pending.armed_at, deadline=0.0
        )
        outcome = session._expire_task_turn(None)  # type: ignore[attr-defined]
        self.assertIsNotNone(outcome)
        self.assertEqual(session._loop.notices, [TIMEOUT_TEXT])  # type: ignore[attr-defined]
        self.assertNotEqual(TIMEOUT_TEXT, DENIED_TEXT)  # 超时与"换目标/按键取消"必须可分
        self.assertTrue(session._task_spoken)  # type: ignore[attr-defined]
        self.assertEqual(len(dispatcher.cancel_calls), 1)

    # ---- 2026-10-23：超时作废后的「裸确认」必须有反馈 ----------------------

    def test_stray_confirmation_without_pending_gets_a_fixed_reply(self) -> None:
        """窗口过期后再说「确认」⇒ 明确回一句，不发给 Provider、不假装执行。"""

        session, router, dispatcher = self._session(accepted="确认")
        self.assertIsNone(router.pending)
        provide = session._task_reply_provider(None)  # type: ignore[attr-defined]
        self.assertEqual(provide(), NO_PENDING_TASK_TEXT)
        self.assertEqual(dispatcher.prepare_calls, [])
        self.assertEqual(session._loop.notices, [])  # 文本交给本轮播报，不走轮次外通知

    def test_stray_confirmation_detection_requires_no_pending_plan(self) -> None:
        session, router, _dispatcher = self._session(accepted="确认")
        self.assertTrue(session._is_stray_confirmation())  # type: ignore[attr-defined]
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined] - 先把计划 arm 起来
        self.assertIsNotNone(router.pending)
        self.assertFalse(session._is_stray_confirmation())  # type: ignore[attr-defined]

    def test_ordinary_chat_text_is_not_treated_as_stray_confirmation(self) -> None:
        session, _router, _dispatcher = self._session(accepted="今天天气怎么样")
        self.assertFalse(session._is_stray_confirmation())  # type: ignore[attr-defined]

    # ---- P10 · T1 #16：键盘确认通道 ----------------------------------------

    def test_key_confirm_executes_and_is_announced_out_of_turn(self) -> None:
        session, router, dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined] - arm
        self.assertTrue(session._key_action_allowed())  # type: ignore[attr-defined]

        session._key_action = "confirm"  # type: ignore[attr-defined]
        outcome = session._handle_key_action(None)  # type: ignore[attr-defined]

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.kind, KIND_EXECUTED)
        self.assertEqual(outcome.confirm_source, "keyboard")
        self.assertEqual(len(dispatcher.execute_calls), 1)
        self.assertEqual(session._loop.notices, [outcome.spoken_text])  # type: ignore[attr-defined]
        self.assertTrue(session._task_spoken)  # type: ignore[attr-defined]
        self.assertIsNone(router.pending)

    def test_key_cancel_cancels_and_is_announced_out_of_turn(self) -> None:
        session, router, dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined]

        session._key_action = "cancel"  # type: ignore[attr-defined]
        outcome = session._handle_key_action(None)  # type: ignore[attr-defined]

        assert outcome is not None
        self.assertEqual(outcome.kind, KIND_CANCELLED)
        self.assertEqual(outcome.reason_code, "key_cancel")
        self.assertEqual(len(dispatcher.cancel_calls), 1)
        self.assertEqual(len(dispatcher.execute_calls), 0)
        self.assertIsNone(router.pending)

    def test_key_action_without_a_pending_plan_is_ignored(self) -> None:
        session, _router, dispatcher = self._session()
        self.assertFalse(session._key_action_allowed())  # type: ignore[attr-defined]
        session._key_action = "confirm"  # type: ignore[attr-defined]
        self.assertIsNone(session._handle_key_action(None))  # type: ignore[attr-defined]
        self.assertEqual(dispatcher.execute_calls, [])
        self.assertEqual(session._loop.notices, [])  # type: ignore[attr-defined]

    def test_key_facts_are_reported_once(self) -> None:
        session, router, _dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined]
        session._key_action = "confirm"  # type: ignore[attr-defined]
        session._key_interrupted = True  # type: ignore[attr-defined]
        session._handle_key_action(None)  # type: ignore[attr-defined]

        self.assertEqual(
            session._take_key_facts(),  # type: ignore[attr-defined]
            {"action": "confirm", "interrupted_speech": True},
        )
        self.assertIsNone(session._take_key_facts())  # type: ignore[attr-defined] - 只报一次

    def test_key_watch_is_skipped_without_a_key_source(self) -> None:
        session, _router, _dispatcher = self._session()
        session._start_key_watch()  # type: ignore[attr-defined]
        self.assertIsNone(getattr(session, "_key_watch", None))
        self.assertFalse(session.cancel_active_speech())  # type: ignore[attr-defined] - 无包装即 False

    def test_key_confirm_source_is_reported_in_the_task_facts(self) -> None:
        session, router, _dispatcher = self._session()
        router._last_handoff = self._task_handoff()  # noqa: SLF001
        session._handle_task_turn(None)  # type: ignore[attr-defined]
        session._key_action = "confirm"  # type: ignore[attr-defined]
        outcome = session._handle_key_action(None)  # type: ignore[attr-defined]
        assert outcome is not None
        facts = session._task_facts([outcome])  # type: ignore[attr-defined]
        assert facts is not None
        self.assertEqual(facts["task_confirm_source"], "keyboard")
        self.assertEqual(facts["task_reason"], "executed")


class KeyboardCancellableSpeakerTests(unittest.TestCase):
    """#16：可取消播报的薄包装（返回**原流对象**，音频字节零变化）。"""

    class _Stream:
        def __init__(self) -> None:
            self.cancelled = 0

        def cancel(self) -> bool:
            self.cancelled += 1
            return True

    class _Speaker:
        def __init__(self, stream: object) -> None:
            self.stream = stream
            self.records: list[dict[str, object]] = []

        def start_stream(self) -> object:
            return self.stream

        def push_text_chunk(self, text: str) -> None:  # pragma: no cover - 仅验证透传
            self.records.append({"text": text})

    def test_start_stream_returns_the_original_stream_and_cancel_works_once(self) -> None:
        stream = self._Stream()
        wrapper = _KeyboardCancellableSpeaker(self._Speaker(stream))
        self.assertIs(wrapper.start_stream(), stream)  # 原对象：loop 行为逐字不变
        self.assertTrue(wrapper.cancel_active_speech())
        self.assertEqual(stream.cancelled, 1)
        self.assertFalse(wrapper.cancel_active_speech())  # 没有活动流了 ⇒ 不再声称取消

    def test_other_attributes_pass_through(self) -> None:
        speaker = self._Speaker(self._Stream())
        wrapper = _KeyboardCancellableSpeaker(speaker)
        wrapper.push_text_chunk("你好")
        self.assertEqual(speaker.records, [{"text": "你好"}])
        self.assertIs(wrapper.records, speaker.records)

    def test_a_finished_stream_is_never_claimed_as_interrupted(self) -> None:
        """诚实性：已经播完的流既不声称"被打断"，也不给它打 `cancelled`。"""

        stream = self._Stream()
        stream.record = {"played_chars": 22, "text_length": 22}
        wrapper = _KeyboardCancellableSpeaker(self._Speaker(stream))
        wrapper.start_stream()
        self.assertFalse(wrapper.cancel_active_speech())
        self.assertEqual(stream.cancelled, 0)

    def test_a_stream_with_unplayed_text_is_cancelled(self) -> None:
        stream = self._Stream()
        stream.record = {"played_chars": 5, "text_length": 22}
        wrapper = _KeyboardCancellableSpeaker(self._Speaker(stream))
        wrapper.start_stream()
        self.assertTrue(wrapper.cancel_active_speech())
        self.assertEqual(stream.cancelled, 1)


if __name__ == "__main__":
    unittest.main()
