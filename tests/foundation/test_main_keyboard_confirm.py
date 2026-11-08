"""P10 · T1 #16 红测：键盘确认通道的**按键读取器**（`app.main._keyboard_key_source`）。

只测映射与降级：Enter/空格 ⇒ ``confirm``、Esc ⇒ ``cancel``、其它键/无按键 ⇒ ``None``；
没有 ``msvcrt``（非 Windows 或没有可用控制台）⇒ 恒 ``None``（按键通道静默关闭）。
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from app.main import _KeyEventLog, _keyboard_interrupt_source, _keyboard_key_source


class KeyboardKeySourceTests(unittest.TestCase):
    def test_maps_enter_space_escape_and_ignores_other_keys(self) -> None:
        keys = ["\r", " ", "\x1b", "x"]
        fake = types.SimpleNamespace(
            kbhit=lambda: bool(keys),
            getwch=lambda: keys.pop(0),
        )
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            poll = _keyboard_key_source()
            self.assertEqual(poll(), "confirm")  # Enter
            self.assertEqual(poll(), "confirm")  # 空格
            self.assertEqual(poll(), "cancel")  # Esc
            self.assertIsNone(poll())  # 其它键被丢弃
            self.assertIsNone(poll())  # 没有按键

    def test_missing_msvcrt_disables_the_channel(self) -> None:
        with mock.patch.dict(sys.modules, {"msvcrt": None}):
            poll = _keyboard_key_source()
            self.assertIsNone(poll())

    def test_console_errors_are_treated_as_no_input(self) -> None:
        def boom() -> bool:
            raise OSError("控制台没了")

        fake = types.SimpleNamespace(kbhit=boom, getwch=lambda: "\r")
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            poll = _keyboard_key_source()
            self.assertIsNone(poll())


class KeyboardEventLogTests(unittest.TestCase):
    """诊断（2026-10-14）：每次按键都记原始键码与处置，供定位"没按 Esc 却被取消"。"""

    def test_confirm_cancel_and_ignored_keys_are_recorded_with_codes(self) -> None:
        log = _KeyEventLog()
        keys = ["\x1b", "\r", "x", "\x00", "\x48"]
        fake = types.SimpleNamespace(kbhit=lambda: bool(keys), getwch=lambda: keys.pop(0))
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            poll = _keyboard_key_source(log)
            self.assertEqual(poll(), "cancel")
            self.assertEqual(poll(), "confirm")
            self.assertIsNone(poll())
            self.assertIsNone(poll())  # 扩展键（方向键）前缀被消费，扫描码一并吃掉
        self.assertEqual(
            log.take(),
            [
                {"source": "turn", "code": "0x1b", "action": "cancel"},
                {"source": "turn", "code": "0x0d", "action": "confirm"},
                {"source": "turn", "code": "0x78", "action": "ignored"},
                {"source": "turn", "code": "0x00", "action": "ignored_extended"},
            ],
        )
        self.assertEqual(log.take(), [])  # 读过即清

    def test_between_turn_reader_is_non_blocking_and_only_returns_on_enter(self) -> None:
        """真机教训：旧的 `kbhit()+readline()` 会被单个非回车键卡住整个轮次循环。"""

        log = _KeyEventLog()
        keys = ["/", "e", "x", "i", "t", "\r", "\x1b", "x"]

        def kbhit() -> bool:
            return bool(keys)

        fake = types.SimpleNamespace(kbhit=kbhit, getwch=lambda: keys.pop(0))
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            poll = _keyboard_interrupt_source(log)
            self.assertIsNone(poll())  # 单字符不产生命令（也不阻塞）
            self.assertIsNone(poll())
            self.assertIsNone(poll())
            self.assertIsNone(poll())
            self.assertIsNone(poll())
            self.assertEqual(poll(), "/exit")  # 回车才出命令
            self.assertIsNone(poll())  # Esc 被消费并记流水
            self.assertIsNone(poll())
            self.assertIsNone(poll())  # 缓冲为空
        events = log.take()
        self.assertEqual(events[-2]["action"], "ignored_escape")
        self.assertEqual(events[0]["source"], "between")

    def test_between_turn_reader_without_msvcrt_is_a_noop(self) -> None:
        with mock.patch.dict(sys.modules, {"msvcrt": None}):
            self.assertIsNone(_keyboard_interrupt_source()())


if __name__ == "__main__":
    unittest.main()
