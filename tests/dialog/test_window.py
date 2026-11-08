"""P9 批次 1 红测：连续对话窗口的闲置/超时/轮数边界（注入时钟、无副作用）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.dialog.state import DialogDiagnostic, DialogError
from app.dialog.window import (
    DialogWindow,
    DialogWindowCloseReason,
    DialogWindowConfig,
    DialogWindowState,
    DialogWindowStatus,
)

FIXED_NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


class Clock:
    """测试用可注入时钟；绝不读取真实时间。"""

    def __init__(self, start: datetime = FIXED_NOW) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def config(**overrides: object) -> DialogWindowConfig:
    values: dict[str, object] = {
        "idle_timeout_seconds": 300,
        "max_turns": 8,
        "max_window_seconds": 1800,
        "listen_timeout_seconds": 8,
    }
    values.update(overrides)
    return DialogWindowConfig(**values)  # type: ignore[arg-type]


def window(clock: Clock, **overrides: object) -> DialogWindow:
    values: dict[str, object] = {"config": config(), "now": clock.now}
    values.update(overrides)
    return DialogWindow(**values)  # type: ignore[arg-type]


class DialogWindowConfigTests(unittest.TestCase):
    def test_config_rejects_non_positive_values(self) -> None:
        for name in (
            "idle_timeout_seconds",
            "max_turns",
            "max_window_seconds",
            "listen_timeout_seconds",
        ):
            for value in (0, -1):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValueError):
                        config(**{name: value})
        with self.assertRaises(ValueError):
            config(max_turns=True)

    def test_config_defaults_are_frozen(self) -> None:
        item = DialogWindowConfig()
        self.assertEqual(item.idle_timeout_seconds, 300)
        self.assertEqual(item.max_turns, 8)
        self.assertEqual(item.max_window_seconds, 1800)
        self.assertEqual(item.listen_timeout_seconds, 8)
        with self.assertRaises(Exception):
            item.max_turns = 4  # type: ignore[misc]


class DialogWindowLifecycleTests(unittest.TestCase):
    def test_fresh_window_is_closed_until_explicitly_opened(self) -> None:
        clock = Clock()
        item = window(clock)
        self.assertEqual(item.state, DialogWindowState.CLOSED)
        self.assertFalse(item.is_open())
        self.assertIsNone(item.close_reason)
        self.assertTrue(item.window_id)
        status = item.open()
        self.assertEqual(status.state, DialogWindowState.OPEN)
        self.assertTrue(item.is_open())
        self.assertEqual(status.turn_count, 0)
        self.assertIsNone(status.close_reason)
        self.assertFalse(status.expired)
        with self.assertRaises(DialogError):
            item.open()

    def test_turn_counting_and_user_close(self) -> None:
        clock = Clock()
        item = window(clock)
        item.open()
        self.assertEqual(item.begin_turn().turn_count, 1)
        self.assertEqual(item.end_turn().turn_count, 1)
        self.assertEqual(item.begin_turn().turn_count, 2)
        status = item.close(reason=DialogWindowCloseReason.USER_CANCELLED)
        self.assertEqual(status.state, DialogWindowState.CLOSED)
        self.assertEqual(status.close_reason, DialogWindowCloseReason.USER_CANCELLED)
        self.assertFalse(status.expired)
        self.assertFalse(item.is_open())

    def test_idle_timeout_closes_at_threshold(self) -> None:
        clock = Clock()
        item = window(clock)
        item.open()
        clock.advance(299)
        self.assertEqual(item.check_timeouts().state, DialogWindowState.OPEN)
        clock.advance(1)
        status = item.check_timeouts()
        self.assertEqual(status.state, DialogWindowState.CLOSED)
        self.assertEqual(status.close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)
        self.assertTrue(status.expired)

    def test_max_window_duration_closes_even_with_activity(self) -> None:
        clock = Clock()
        item = window(clock, config=config(idle_timeout_seconds=10000, max_window_seconds=60))
        item.open()
        clock.advance(59)
        item.note_activity()
        clock.advance(1)
        status = item.check_timeouts()
        self.assertEqual(status.close_reason, DialogWindowCloseReason.MAX_WINDOW_DURATION)
        self.assertTrue(status.expired)

    def test_both_timeouts_expired_uses_fixed_precedence(self) -> None:
        clock = Clock()
        item = window(clock, config=config(idle_timeout_seconds=10, max_window_seconds=10))
        item.open()
        clock.advance(10)
        status = item.check_timeouts()
        self.assertEqual(status.close_reason, DialogWindowCloseReason.MAX_WINDOW_DURATION)
        self.assertTrue(status.expired)

    def test_max_turns_closes_and_caps_turn_count(self) -> None:
        clock = Clock()
        item = window(clock, config=config(max_turns=2))
        item.open()
        self.assertEqual(item.begin_turn().turn_count, 1)
        self.assertEqual(item.begin_turn().turn_count, 2)
        status = item.begin_turn()
        self.assertEqual(status.state, DialogWindowState.CLOSED)
        self.assertEqual(status.close_reason, DialogWindowCloseReason.MAX_TURNS)
        self.assertEqual(status.turn_count, 2)
        self.assertTrue(status.expired)

    def test_closed_window_never_revives_or_rewrites_reason(self) -> None:
        clock = Clock()
        item = window(clock, config=config(idle_timeout_seconds=10))
        item.open()
        identity = item.window_id
        item.begin_turn()
        clock.advance(10)
        item.check_timeouts()
        self.assertEqual(item.close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)
        for action in (item.note_activity, item.begin_turn, item.end_turn, item.check_timeouts):
            with self.subTest(action=action):
                status = action()
                self.assertEqual(status.state, DialogWindowState.CLOSED)
                self.assertEqual(status.close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)
        self.assertEqual(item.state, DialogWindowState.CLOSED)
        self.assertEqual(item.window_id, identity)
        self.assertFalse(item.is_open())
        self.assertEqual(item.begin_turn().turn_count, 1)
        item.close(reason=DialogWindowCloseReason.USER_CANCELLED)
        self.assertEqual(item.close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)

    def test_reopen_after_close_issues_a_new_identity_every_time(self) -> None:
        clock = Clock()
        item = window(clock)
        identities = []
        for _ in range(3):
            item.open()
            identities.append(item.window_id)
            item.begin_turn()
            item.close(reason=DialogWindowCloseReason.SHUTDOWN)
        self.assertEqual(len(set(identities)), 3)
        item.open()
        self.assertEqual(item.status.turn_count, 0)
        self.assertIsNone(item.status.close_reason)
        self.assertNotIn(item.window_id, identities)

    def test_note_activity_refreshes_idle_clock(self) -> None:
        clock = Clock()
        item = window(clock, config=config(idle_timeout_seconds=300))
        item.open()
        clock.advance(200)
        item.note_activity()
        clock.advance(299)
        self.assertEqual(item.check_timeouts().state, DialogWindowState.OPEN)
        clock.advance(1)
        self.assertEqual(item.check_timeouts().close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)

    def test_timeouts_use_injected_clock_only(self) -> None:
        clock = Clock(datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        item = window(clock, config=config(idle_timeout_seconds=1, max_window_seconds=1))
        item.open()
        self.assertEqual(item.check_timeouts().state, DialogWindowState.OPEN)
        clock.advance(1)
        self.assertEqual(
            item.check_timeouts().close_reason, DialogWindowCloseReason.MAX_WINDOW_DURATION
        )

    def test_close_rejects_str_coercion_and_wrong_types(self) -> None:
        clock = Clock()
        item = window(clock)
        item.open()
        # 等值枚举字符串也不得被强转接受（规格 §3：非 DialogWindowCloseReason 值抛错）。
        for reason in ("idle_timeout", "user_cancelled", "shutdown", "finished", "user said stop", ""):
            with self.subTest(reason=reason):
                with self.assertRaises(DialogError):
                    item.close(reason=reason)
        with self.assertRaises(TypeError):
            item.close(reason=None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            item.close(reason=3)  # type: ignore[arg-type]
        with self.assertRaises(DialogError):
            item.close(reason=DialogWindowCloseReason.IDLE_TIMEOUT.value)  # type: ignore[arg-type]
        self.assertEqual(item.state, DialogWindowState.OPEN)
        self.assertIsNone(item.close_reason)
        self.assertEqual(
            item.close(reason=DialogWindowCloseReason.SHUTDOWN).close_reason,
            DialogWindowCloseReason.SHUTDOWN,
        )

    def test_status_details_are_redacted_and_complete(self) -> None:
        clock = Clock()
        item = window(clock)
        item.open()
        details = item.status.to_details()
        self.assertEqual(
            set(details), {"window_id", "state", "turn_count", "close_reason", "expired"}
        )
        self.assertTrue(all(isinstance(value, str) for value in details.values()))
        self.assertEqual(details["state"], "open")
        self.assertEqual(details["turn_count"], "0")
        self.assertEqual(details["close_reason"], "none")
        self.assertEqual(details["expired"], "false")
        item.begin_turn()
        item.close(reason=DialogWindowCloseReason.IDLE_TIMEOUT)
        closed = item.status.to_details()
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["turn_count"], "1")
        self.assertEqual(closed["close_reason"], "idle_timeout")
        self.assertEqual(closed["expired"], "true")
        self.assertIsInstance(item.status, DialogWindowStatus)

    def test_window_diagnostics_are_emitted(self) -> None:
        clock = Clock()
        events: list[DialogDiagnostic] = []
        item = window(clock, diagnostic_sink=events.append)
        item.open()
        item.close(reason=DialogWindowCloseReason.MAX_TURNS)
        self.assertEqual(
            [event.event_type for event in events],
            ["dialog_window_opened", "dialog_window_closed"],
        )
        for event in events:
            self.assertEqual(set(event.to_details()), {"reason", "state", "mode"})
        self.assertEqual(events[1].reason, "max_turns")
        self.assertEqual(events[1].state.value, "closed")

    def test_constructor_argument_validation(self) -> None:
        clock = Clock()
        with self.assertRaises(TypeError):
            DialogWindow(config="nope", now=clock.now)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DialogWindow(config=config(), now="nope")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DialogWindow(config=config(), now=clock.now, diagnostic_sink="nope")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            DialogWindow(config=config(), now=clock.now, window_id="bad id")
        item = DialogWindow(config=config(), now=clock.now, window_id="dialog-window-fixed")
        self.assertEqual(item.window_id, "dialog-window-fixed")


if __name__ == "__main__":
    unittest.main()
