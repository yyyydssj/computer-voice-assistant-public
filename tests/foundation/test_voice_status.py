"""P9 UI：语音阶段 → 右下角提示窗的**共享接线**（离线；不建真实窗口）。

覆盖：``speech_result`` 归一化、``attach_stage_hooks`` 的阶段顺序与阻塞段前后抽消息、
``text_only`` 不算失败、真实失败算失败、``restore_stage_hooks`` 还原、``StatusBridge``
在"没有窗口 / 窗口抛错"时永不抛错、``build_status_hud`` 失败即返回 ``None``。
"""

from __future__ import annotations

import unittest

from app.ui import voice_status
from tests.foundation.test_voice_hud import FakeHudApi


class FakeLoop:
    """最小 ``ChatVoiceLoop`` 替身：只提供被包装的三个动作与 ``_speaker``。

    ``log`` 与注入的 ``set_state``/``tick`` 共用一个列表，从而能断言**真实顺序**。
    """

    def __init__(
        self,
        *,
        speak_result: object = (False, "text_only"),
        speaker: object = None,
        log: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._speaker = speaker
        self.speak_result = speak_result
        self.log = log if log is not None else []

    def _record(self, item: str) -> None:
        self.calls.append(item)
        self.log.append(item)

    def release_input(self) -> object:
        self._record("release")
        return None

    def complete_turn(self) -> object:
        self._record("complete")
        return "report"

    def _speak(self, text: str) -> object:
        self._record(f"speak:{text}")
        return self.speak_result


class FakeStream:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.finished = 0

    def push_text_chunk(self, chunk: str) -> None:
        self.chunks.append(chunk)

    def finish(self) -> None:
        self.finished += 1


class FakeSpeaker:
    def __init__(self, stream: object) -> None:
        self._stream = stream

    def start_stream(self) -> object:
        return self._stream


class _FakeHud:
    """记录 ``VoiceStatusHud`` 调用形状的最简替身。"""

    def __init__(self, *, raising: bool = False) -> None:
        self.states: list[str] = []
        self.ticks = 0
        self.hidden = 0
        self.destroyed = 0
        self.raising = raising

    def set_state(self, state: object, *, turn_index: int | None = None) -> str:
        if self.raising:
            raise RuntimeError("win32 exploded: C:\\secret\\path")
        del turn_index
        self.states.append(getattr(state, "value", str(state)))
        return self.states[-1]

    def tick(self) -> int:
        if self.raising:
            raise RuntimeError("pump exploded")
        self.ticks += 1
        return 0

    def hide(self) -> None:
        if self.raising:
            raise RuntimeError("hide exploded")
        self.hidden += 1

    def destroy(self) -> None:
        if self.raising:
            raise RuntimeError("destroy exploded")
        self.destroyed += 1


class SpeechResultTests(unittest.TestCase):
    def test_known_shapes_are_passed_through(self) -> None:
        self.assertEqual(
            voice_status.speech_result((True, "speech_finished")), (True, "speech_finished")
        )
        self.assertEqual(voice_status.speech_result((False, "text_only")), (False, "text_only"))

    def test_malformed_shapes_count_as_real_failure(self) -> None:
        for bad in ("boom", (False,), (1, "x"), (True, 3), None, [True, "speech_finished"]):
            with self.subTest(bad=bad):
                self.assertEqual(
                    voice_status.speech_result(bad),
                    (False, voice_status.SPEECH_FAILURE_REASON),
                )


class AttachStageHooksTests(unittest.TestCase):
    def _attach(self, **loop_kwargs: object) -> tuple[FakeLoop, list[str], dict]:
        log: list[str] = []
        loop = FakeLoop(log=log, **loop_kwargs)  # type: ignore[arg-type]
        trace = voice_status.attach_stage_hooks(
            loop,
            set_state=lambda name: log.append(f"set:{name}"),
            tick=lambda: log.append("tick"),
            index=1,
        )
        return loop, log, trace

    def test_ticks_bracket_every_blocking_phase(self) -> None:
        loop, log, trace = self._attach()
        loop.release_input()
        loop.complete_turn()
        loop._speak("你好")
        self.assertEqual(
            log,
            [
                "set:TRANSCRIBING", "tick", "release", "tick",
                "set:THINKING", "tick", "complete", "tick",
                "set:SPEAKING", "tick", "speak:你好", "set:LISTENING",
            ],
        )
        self.assertFalse(trace["spoken"])

    def test_missing_speech_channel_is_not_a_failure(self) -> None:
        loop, log, _trace = self._attach(speak_result=(False, "text_only"))
        loop._speak("你好")
        self.assertIn("set:SPEAKING", log)
        self.assertIn("set:LISTENING", log)
        self.assertNotIn("set:FAILED", log)

    def test_real_speech_failure_is_shown_as_failure(self) -> None:
        loop, log, trace = self._attach(speak_result=(False, "speech_failed"))
        loop._speak("你好")
        self.assertEqual(log[-1], "set:FAILED")
        self.assertFalse(trace["spoken"])

    def test_malformed_speak_result_is_shown_as_failure(self) -> None:
        loop, log, _trace = self._attach(speak_result="unexpected")
        loop._speak("你好")
        self.assertEqual(log[-1], "set:FAILED")

    def test_stream_playback_ticks_per_chunk_and_reports_spoken(self) -> None:
        stream = FakeStream()
        loop, log, trace = self._attach(speaker=FakeSpeaker(stream))
        result = loop._speak("你好")
        self.assertEqual(result, (True, "speech_finished"))
        self.assertTrue(trace["spoken"])
        self.assertEqual(stream.chunks, ["你好"])
        self.assertEqual(stream.finished, 1)
        self.assertEqual(log[-1], "set:LISTENING")
        self.assertEqual(loop.calls, [])  # 走真实 stream 时不再调用原 _speak

    def test_restore_returns_the_original_methods(self) -> None:
        loop, _log, _trace = self._attach()
        self.assertIn("release_input", vars(loop))
        voice_status.restore_stage_hooks(loop)
        self.assertNotIn("release_input", vars(loop))
        self.assertNotIn("complete_turn", vars(loop))
        self.assertNotIn("_speak", vars(loop))
        loop.release_input()
        self.assertEqual(loop.calls, ["release"])


class StatusBridgeTests(unittest.TestCase):
    def test_without_a_window_every_call_is_a_safe_noop(self) -> None:
        bridge = voice_status.StatusBridge(None)
        self.assertFalse(bridge.available)
        bridge.set_state("LISTENING", turn_index=1)
        bridge.tick()
        bridge.close()

    def test_forwards_known_states_and_drops_unknown_names(self) -> None:
        hud = _FakeHud()
        bridge = voice_status.StatusBridge(hud)
        self.assertTrue(bridge.available)
        bridge.set_state("LISTENING", turn_index=2)
        bridge.set_state("NOPE")
        bridge.set_state("SPEAKING")
        bridge.tick()
        self.assertEqual(hud.states, ["listening", "speaking"])
        self.assertEqual(hud.ticks, 1)

    def test_window_failures_never_escape(self) -> None:
        bridge = voice_status.StatusBridge(_FakeHud(raising=True))
        bridge.set_state("LISTENING")
        bridge.tick()
        bridge.close()

    def test_close_hides_and_destroys_once(self) -> None:
        hud = _FakeHud()
        voice_status.StatusBridge(hud).close()
        self.assertEqual((hud.hidden, hud.destroyed), (1, 1))


class BuildStatusHudTests(unittest.TestCase):
    def test_returns_a_hud_when_the_platform_api_is_available(self) -> None:
        hud = voice_status.build_status_hud(api_factory=lambda: FakeHudApi())
        self.assertIsNotNone(hud)
        self.assertTrue(voice_status.StatusBridge(hud).available)
        hud.destroy()

    def test_returns_none_when_the_window_cannot_be_built(self) -> None:
        def broken() -> object:
            raise RuntimeError("unsupported_platform: C:\\secret\\path")

        self.assertIsNone(voice_status.build_status_hud(api_factory=broken))

    def test_returns_none_when_the_api_returns_garbage(self) -> None:
        self.assertIsNone(voice_status.build_status_hud(api_factory=lambda: object()))


class _RecordingLoop:
    """最小 ``ChatVoiceLoop`` 替身：``complete_turn()`` 内部**真的**调用 ``_speak``。"""

    def __init__(
        self,
        log: list[str],
        *,
        speak_result: object = (True, "speech_finished"),
        speak_error: BaseException | None = None,
    ) -> None:
        self.log = log
        self.speak_result = speak_result
        self.speak_error = speak_error

    def complete_turn(self) -> object:
        self.log.append("complete")
        return self._speak("你好")

    def _speak(self, text: str) -> object:
        self.log.append(f"speak:{text}")
        if self.speak_error is not None:
            raise self.speak_error
        return self.speak_result


class _RecordingHud:
    """记录状态与消息抽取顺序的提示窗替身（可选抛错，用于验证展示层隔离）。"""

    def __init__(self, log: list[str], *, raising: bool = False) -> None:
        self.log = log
        self.raising = raising
        self.states: list[str] = []

    def set_state(self, state: object, *, turn_index: int | None = None) -> str:
        if self.raising:
            raise RuntimeError("hud_failed")
        name = getattr(state, "name", str(state))
        self.states.append(name)
        self.log.append(f"set:{name}")
        return name

    def tick(self) -> int:
        if self.raising:
            raise RuntimeError("hud_failed")
        self.log.append("tick")
        return 0


class AutoVoiceSpeakingStateTests(unittest.TestCase):
    """真机事实（2026-10-09）：免按键路径的"播报"状态必须出现在播报**期间**。

    这条路径此前只推 ``listening``/``hearing``（都渲染成"聆听"），而 ``SPEAKING`` 是
    ``main.py`` 在整轮结束**之后**才设，于是"播报时窗口一直停在聆听、播完才闪一下播报"。
    """

    def _session(self, loop: object) -> object:
        """只借 ``_complete_turn_with_hud`` 一个方法：不构造真实会话（它会起线程与设备）。"""

        from app.runtime.auto_voice import AutoVoiceSession

        class _Session:
            _complete_turn_with_hud = AutoVoiceSession._complete_turn_with_hud

        session = _Session()
        session._loop = loop
        return session

    def test_speaking_is_pushed_while_the_real_speak_runs(self) -> None:
        log: list[str] = []
        loop = _RecordingLoop(log)
        hud = _RecordingHud(log)

        report = self._session(loop)._complete_turn_with_hud(hud, 7)

        self.assertEqual(report, (True, "speech_finished"))
        self.assertEqual(hud.states, ["SPEAKING", "LISTENING"])
        self.assertEqual(
            log,
            ["complete", "set:SPEAKING", "tick", "speak:你好", "set:LISTENING", "tick"],
        )
        # 包装必须被拆掉：之后直接调 `_speak` 不再产生任何提示窗状态。
        hud.states.clear()
        log.clear()
        loop._speak("世界")
        self.assertEqual(hud.states, [])
        self.assertNotIn("_speak", vars(loop))

    def test_without_a_window_the_turn_runs_unwrapped(self) -> None:
        log: list[str] = []
        loop = _RecordingLoop(log)

        self.assertEqual(
            self._session(loop)._complete_turn_with_hud(None, 1),
            (True, "speech_finished"),
        )
        self.assertEqual(log, ["complete", "speak:你好"])
        self.assertNotIn("_speak", vars(loop))

    def test_speak_failure_propagates_and_state_returns_to_listening(self) -> None:
        log: list[str] = []
        loop = _RecordingLoop(log, speak_error=RuntimeError("synthetic_failure"))
        hud = _RecordingHud(log)

        with self.assertRaises(RuntimeError):
            self._session(loop)._complete_turn_with_hud(hud, 2)

        # 异常原样透传，但窗口不得停留在"播报"。
        self.assertEqual(hud.states, ["SPEAKING", "LISTENING"])
        self.assertNotIn("_speak", vars(loop))

    def test_window_failure_never_breaks_the_turn(self) -> None:
        log: list[str] = []
        loop = _RecordingLoop(log)
        hud = _RecordingHud(log, raising=True)

        self.assertEqual(
            self._session(loop)._complete_turn_with_hud(hud, 3),
            (True, "speech_finished"),
        )
        self.assertEqual(log, ["complete", "speak:你好"])
        self.assertNotIn("_speak", vars(loop))


if __name__ == "__main__":
    unittest.main()
