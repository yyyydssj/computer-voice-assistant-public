"""P8 批次 1 红测：语音轮次边界与显式状态机（离线、无副作用）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.voice_loop.boundary import (
    VoiceInputSource,
    VoiceLoopDiagnostic,
    VoiceLoopError,
    VoiceLoopState,
    VoiceLoopStateMachine,
    VoiceTurnBoundary,
    VoiceTurnRegistry,
)

FIXED_NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def registry(**overrides: object) -> VoiceTurnRegistry:
    values: dict[str, object] = {
        "source_instance_id": "instance-1",
        "now": lambda: FIXED_NOW,
    }
    values.update(overrides)
    return VoiceTurnRegistry(**values)  # type: ignore[arg-type]


def boundary(**overrides: object) -> VoiceTurnBoundary:
    values: dict[str, object] = {
        "voice_session_id": "voice-session-1",
        "utterance_id": "utterance-1",
        "turn_id": "asr-turn-1",
        "request_id": "asr-request-1",
        "reply_id": "asr-reply-1",
        "source": "asr",
        "created_at": "2026-10-01T09:00:00.000Z",
    }
    values.update(overrides)
    return VoiceTurnBoundary(**values)  # type: ignore[arg-type]


class VoiceTurnBoundaryTests(unittest.TestCase):
    def test_valid_boundary_exposes_trace_fields(self) -> None:
        item = boundary()
        self.assertEqual(item.source, VoiceInputSource.ASR)
        self.assertEqual(
            item.to_trace_fields(),
            {
                "request_id": "asr-request-1",
                "turn_id": "asr-turn-1",
                "source": "asr",
                "created_at": "2026-10-01T09:00:00.000Z",
            },
        )

    def test_matches_trace_requires_every_field(self) -> None:
        item = boundary()
        self.assertTrue(
            item.matches_trace(request_id="asr-request-1", turn_id="asr-turn-1", source="asr")
        )
        self.assertFalse(
            item.matches_trace(request_id="asr-request-2", turn_id="asr-turn-1", source="asr")
        )
        self.assertFalse(
            item.matches_trace(request_id="asr-request-1", turn_id="asr-turn-1", source="text_shell")
        )

    def test_boundary_rejects_malformed_identifiers(self) -> None:
        for name in (
            "voice_session_id",
            "utterance_id",
            "turn_id",
            "request_id",
            "reply_id",
        ):
            with self.subTest(field=name):
                with self.assertRaises(ValueError):
                    boundary(**{name: ""})
                with self.assertRaises(ValueError):
                    boundary(**{name: "bad id"})

    def test_boundary_rejects_unknown_source_and_bad_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            boundary(source="microphone")
        with self.assertRaises(ValueError):
            boundary(created_at="2026-10-01T09:00:00+08:00")
        with self.assertRaises(ValueError):
            boundary(created_at="")

    def test_boundary_is_immutable(self) -> None:
        item = boundary()
        with self.assertRaises(Exception):
            item.turn_id = "other"  # type: ignore[misc]


class VoiceTurnRegistryTests(unittest.TestCase):
    def test_open_turn_issues_fresh_unique_identities(self) -> None:
        reg = registry()
        first = reg.open_turn(source="asr")
        self.assertEqual(first.source, VoiceInputSource.ASR)
        self.assertEqual(first.created_at, "2026-10-01T09:00:00.000Z")
        self.assertTrue(first.voice_session_id.startswith("voice-session-"))
        reg.close_turn(first, reason="completed")
        second = reg.open_turn(source="asr", voice_session_id=first.voice_session_id)
        self.assertEqual(second.voice_session_id, first.voice_session_id)
        for name in ("utterance_id", "turn_id", "request_id", "reply_id"):
            self.assertNotEqual(getattr(second, name), getattr(first, name))
        self.assertNotIn(second.turn_id, {first.turn_id, first.request_id, first.utterance_id})

    def test_registry_refuses_a_second_active_turn(self) -> None:
        reg = registry()
        reg.open_turn(source="asr")
        with self.assertRaises(VoiceLoopError):
            reg.open_turn(source="text_shell")

    def test_registry_refuses_unknown_and_foreign_boundary_close(self) -> None:
        reg = registry()
        active = reg.open_turn(source="asr")
        foreign = boundary()
        with self.assertRaises(VoiceLoopError):
            reg.close_turn(foreign, reason="completed")
        with self.assertRaises(VoiceLoopError):
            reg.close_turn(active, reason="not_a_reason")
        with self.assertRaises(TypeError):
            reg.close_turn("active", reason="completed")  # type: ignore[arg-type]

    def test_closed_turn_is_retired_and_never_active_again(self) -> None:
        reg = registry()
        first = reg.open_turn(source="asr")
        reg.close_turn(first, reason="completed")
        self.assertIsNone(reg.active)
        self.assertFalse(reg.is_active(first))
        self.assertTrue(reg.is_retired(first))
        self.assertTrue(reg.is_known(first))
        with self.assertRaises(VoiceLoopError):
            reg.close_turn(first, reason="completed")

    def test_late_boundary_from_previous_turn_is_not_known(self) -> None:
        reg = registry()
        reg.open_turn(source="asr")
        stale = boundary()
        self.assertFalse(reg.is_known(stale))
        self.assertFalse(reg.is_active(stale))
        self.assertFalse(reg.is_retired(stale))

    def test_text_source_turn_uses_text_identifiers(self) -> None:
        reg = registry()
        item = reg.open_turn(source="text_shell")
        self.assertEqual(item.source, VoiceInputSource.TEXT_SHELL)
        self.assertTrue(item.turn_id.startswith("text_shell-turn-"))
        self.assertTrue(item.request_id.startswith("text_shell-request-"))
        with self.assertRaises(ValueError):
            registry().open_turn(source="telnet")

    def test_cancel_all_retires_the_active_turn(self) -> None:
        reg = registry()
        active = reg.open_turn(source="asr")
        retired = reg.cancel_all(reason="cancelled")
        self.assertEqual(tuple(item.identity for item in retired), (active.identity,))
        self.assertIsNone(reg.active)
        self.assertEqual(reg.cancel_all(reason="cancelled"), ())


class VoiceLoopStateMachineTests(unittest.TestCase):
    def machine(self, *, sink=None):
        reg = registry()
        return reg, VoiceLoopStateMachine(reg, diagnostic_sink=sink)

    def test_voice_turn_walks_the_declared_path(self) -> None:
        reg, machine = self.machine()
        item = reg.open_turn(source="asr")
        self.assertEqual(machine.state, VoiceLoopState.IDLE)
        machine.begin_turn(item)
        self.assertEqual(machine.state, VoiceLoopState.CAPTURING)
        machine.transition(VoiceLoopState.TRANSCRIBING, reason="released")
        machine.transition(VoiceLoopState.ROUTING, reason="accepted_final")
        machine.transition(VoiceLoopState.REPLYING, reason="route_chat")
        machine.transition(VoiceLoopState.SPEAKING, reason="speech_started")
        machine.transition(VoiceLoopState.IDLE, reason="speech_finished")
        self.assertIsNone(machine.boundary)
        self.assertEqual(len(machine.transitions), 6)

    def test_text_turn_begins_at_routing(self) -> None:
        reg, machine = self.machine()
        item = reg.open_turn(source="text_shell")
        machine.begin_turn(item)
        self.assertEqual(machine.state, VoiceLoopState.ROUTING)
        machine.transition(VoiceLoopState.CLARIFYING, reason="route_ambiguous")
        machine.transition(VoiceLoopState.IDLE, reason="clarified")

    def test_illegal_transitions_fail_closed(self) -> None:
        reg, machine = self.machine()
        with self.assertRaises(VoiceLoopError):
            machine.transition(VoiceLoopState.SPEAKING, reason="too_early")
        item = reg.open_turn(source="asr")
        machine.begin_turn(item)
        with self.assertRaises(VoiceLoopError):
            machine.transition(VoiceLoopState.REPLYING, reason="skip")
        with self.assertRaises(VoiceLoopError):
            machine.transition(VoiceLoopState.IDLE, reason="bad reason!")

    def test_task_states_have_no_inbound_transition_yet(self) -> None:
        reg, machine = self.machine()
        item = reg.open_turn(source="asr")
        machine.begin_turn(item)
        machine.transition(VoiceLoopState.TRANSCRIBING, reason="released")
        machine.transition(VoiceLoopState.ROUTING, reason="accepted_final")
        for target in (
            VoiceLoopState.PLANNING,
            VoiceLoopState.AWAITING_AUTHORIZATION,
            VoiceLoopState.EXECUTING,
            VoiceLoopState.VERIFYING,
        ):
            with self.subTest(target=target):
                with self.assertRaises(VoiceLoopError):
                    machine.transition(target, reason="not_implemented")

    def test_begin_turn_requires_active_boundary_and_idle_state(self) -> None:
        reg, machine = self.machine()
        stale = boundary()
        with self.assertRaises(VoiceLoopError):
            machine.begin_turn(stale)
        active = reg.open_turn(source="asr")
        machine.begin_turn(active)
        with self.assertRaises(VoiceLoopError):
            machine.begin_turn(active)
        with self.assertRaises(TypeError):
            machine.begin_turn("active")  # type: ignore[arg-type]

    def test_terminal_state_is_absorbing(self) -> None:
        reg, machine = self.machine()
        item = reg.open_turn(source="asr")
        machine.begin_turn(item)
        machine.terminate(reason="shutdown")
        self.assertTrue(machine.is_terminal)
        with self.assertRaises(VoiceLoopError):
            machine.transition(VoiceLoopState.IDLE, reason="recover")
        with self.assertRaises(VoiceLoopError):
            machine.terminate(reason="shutdown")

    def test_transitions_are_recorded_and_redacted(self) -> None:
        events: list[VoiceLoopDiagnostic] = []
        reg, machine = self.machine(sink=events.append)
        item = reg.open_turn(source="asr")
        machine.begin_turn(item)
        self.assertEqual(len(events), 1)
        first = events[0]
        self.assertEqual(first.event_type, "voice_loop_transition")
        self.assertEqual(first.subject_id, item.turn_id)
        self.assertEqual(first.state, VoiceLoopState.CAPTURING)
        self.assertEqual(first.source, VoiceInputSource.ASR)
        details = first.to_details()
        self.assertEqual(set(details), {"reason", "state", "source"})
        self.assertNotIn("text", details)

    def test_boundary_is_bound_only_to_the_active_turn(self) -> None:
        reg, machine = self.machine()
        first = reg.open_turn(source="asr")
        machine.begin_turn(first)
        self.assertTrue(machine.owns(first))
        machine.transition(VoiceLoopState.IDLE, reason="cancelled")
        self.assertFalse(machine.owns(first))


if __name__ == "__main__":
    unittest.main()
