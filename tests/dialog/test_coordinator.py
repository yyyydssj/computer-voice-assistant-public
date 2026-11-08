"""P9 批次 1 红测：turn claim 与输入许可边界（离线、无副作用）。"""

from __future__ import annotations

import pathlib
import re
import unittest
from datetime import datetime, timedelta, timezone

from app.dialog.coordinator import (
    DIALOG_INPUT_REFUSAL_REASONS,
    DialogCoordinator,
    InputPermit,
    TurnClaim,
    TurnClaimDecision,
)
from app.dialog.state import DialogDiagnostic, DialogError, DialogState, DialogStateMachine
from app.dialog.window import (
    DialogWindow,
    DialogWindowCloseReason,
    DialogWindowConfig,
    DialogWindowState,
)
from app.voice_loop.boundary import VoiceInputSource

FIXED_NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


class Clock:
    """测试用可注入时钟；绝不读取真实时间。"""

    def __init__(self, start: datetime = FIXED_NOW) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def build(
    *,
    opened: bool = True,
    idle_timeout_seconds: int = 300,
    max_turns: int = 8,
    max_window_seconds: int = 1800,
    sink=None,
    window: DialogWindow | None = None,
    machine: DialogStateMachine | None = None,
):
    clock = Clock()
    if window is None:
        window = DialogWindow(
            config=DialogWindowConfig(
                idle_timeout_seconds=idle_timeout_seconds,
                max_turns=max_turns,
                max_window_seconds=max_window_seconds,
            ),
            now=clock.now,
        )
    if opened and not window.is_open():
        window.open()
    if machine is None:
        machine = DialogStateMachine(
            source_instance_id="dialog-instance-1", window_id=window.window_id
        )
    coordinator = DialogCoordinator(machine=machine, window=window, diagnostic_sink=sink)
    return clock, window, machine, coordinator


def forged_claim(window_id: str, **overrides: object) -> TurnClaim:
    values: dict[str, object] = {
        "claim_id": "claim-forged-1",
        "window_id": window_id,
        "turn_id": "turn-forged-1",
        "source": None,
        "layer": "voice",
        "issued_at": "2026-10-01T09:00:00.000Z",
    }
    values.update(overrides)
    return TurnClaim(**values)  # type: ignore[arg-type]


class InputPermitTests(unittest.TestCase):
    def test_request_input_granted_only_in_idle_or_listening(self) -> None:
        clock, window, machine, coordinator = build()
        permit = coordinator.request_input(layer="voice", source=VoiceInputSource.ASR)
        self.assertIsInstance(permit, InputPermit)
        self.assertTrue(permit.granted)
        self.assertEqual(permit.reason, "granted")
        self.assertEqual(permit.state, DialogState.IDLE)
        self.assertIsNone(permit.mode)
        self.assertTrue(
            coordinator.request_input(layer="text", source=VoiceInputSource.TEXT_SHELL).granted
        )
        coordinator.resume_listening(reason="listening_started")
        self.assertTrue(coordinator.request_input(layer="voice").granted)

    def test_request_input_refused_when_window_is_closed(self) -> None:
        clock, window, machine, coordinator = build(opened=False)
        permit = coordinator.request_input(layer="voice")
        self.assertFalse(permit.granted)
        self.assertEqual(permit.reason, "window_closed")
        self.assertEqual(machine.state, DialogState.IDLE)
        window.open()
        clock.advance(300)
        window.check_timeouts()
        self.assertEqual(window.state, DialogWindowState.CLOSED)
        self.assertEqual(coordinator.request_input(layer="voice").reason, "window_closed")

    def test_request_input_refused_in_authorization_and_execution_without_migration(self) -> None:
        for reason, target in (
            ("authorization_required", DialogState.AWAITING_AUTHORIZATION),
            ("execution_requested", DialogState.EXECUTING),
        ):
            with self.subTest(target=target):
                clock, window, machine, coordinator = build()
                machine.begin_text_turn()
                if target is DialogState.AWAITING_AUTHORIZATION:
                    coordinator.suspend_for_authorization(reason=reason)
                else:
                    coordinator.suspend_for_execution(reason=reason)
                self.assertEqual(machine.state, target)
                before = machine.transitions
                permit = coordinator.request_input(layer="voice", source=VoiceInputSource.ASR)
                self.assertFalse(permit.granted)
                self.assertEqual(permit.reason, "state_blocked")
                self.assertEqual(machine.state, target)
                self.assertEqual(machine.transitions, before)
                self.assertFalse(machine.accepts_input)

    def test_request_input_refused_after_fail_closed_and_after_close(self) -> None:
        clock, window, machine, coordinator = build()
        coordinator.invalidate(reason="asr_failed")
        self.assertEqual(machine.state, DialogState.FAIL_CLOSED)
        failed = coordinator.request_input(layer="voice")
        self.assertFalse(failed.granted)
        self.assertEqual(failed.reason, "state_blocked")
        _, window2, machine2, coordinator2 = build()
        machine2.close(reason="shutdown")
        closed = coordinator2.request_input(layer="voice")
        self.assertFalse(closed.granted)
        self.assertEqual(closed.reason, "state_blocked")
        self.assertEqual(machine2.state, DialogState.CLOSED)


class TurnClaimTests(unittest.TestCase):
    def test_second_claim_is_refused_while_one_is_active(self) -> None:
        clock, window, machine, coordinator = build()
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(decision.granted)
        claim = decision.claim
        assert claim is not None
        self.assertEqual(claim.window_id, window.window_id)
        self.assertEqual(claim.layer, "voice")
        self.assertIs(claim.source, VoiceInputSource.ASR)
        self.assertTrue(claim.issued_at.endswith("Z"))
        self.assertEqual(coordinator.active_claim, claim)
        second = coordinator.claim_turn(layer="text", source=VoiceInputSource.TEXT_SHELL)
        self.assertFalse(second.granted)
        self.assertIsNone(second.claim)
        self.assertEqual(second.reason, "claim_active")
        self.assertIn(second.reason, DIALOG_INPUT_REFUSAL_REASONS)
        self.assertEqual(coordinator.active_claim, claim)
        self.assertEqual(coordinator.request_input(layer="voice").reason, "claim_active")

    def test_claim_requires_open_window_and_accepting_state(self) -> None:
        clock, window, machine, coordinator = build(opened=False)
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertEqual((decision.granted, decision.claim, decision.reason), (False, None, "window_closed"))
        window.open()
        machine.begin_voice_turn()
        blocked = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertEqual((blocked.granted, blocked.claim, blocked.reason), (False, None, "state_blocked"))
        machine.transition(DialogState.CLOSED, reason="shutdown")
        closed = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertEqual((closed.granted, closed.claim, closed.reason), (False, None, "state_blocked"))

    def test_layer_and_source_rules_are_enforced(self) -> None:
        clock, window, machine, coordinator = build()
        with self.assertRaises(DialogError):
            coordinator.claim_turn(layer="screen")
        with self.assertRaises(DialogError):
            coordinator.claim_turn(layer="voice", source=VoiceInputSource.TEXT_SHELL)
        with self.assertRaises(DialogError):
            coordinator.claim_turn(layer="text", source=VoiceInputSource.ASR)
        with self.assertRaises(DialogError):
            coordinator.request_input(layer="camera")
        # source 只接受 VoiceInputSource 实例或 None，字符串一律拒绝（不得强转）。
        for source in ("asr", "text_shell", "microphone", ""):
            with self.subTest(source=source):
                with self.assertRaises(TypeError):
                    coordinator.request_input(layer="voice", source=source)  # type: ignore[arg-type]
                with self.assertRaises(TypeError):
                    coordinator.claim_turn(layer="voice", source=source)  # type: ignore[arg-type]
        passive = coordinator.claim_turn(layer="voice", source=None)
        self.assertTrue(passive.granted)
        assert passive.claim is not None
        self.assertIsNone(passive.claim.source)

    def test_explicit_and_invalid_turn_ids(self) -> None:
        clock, window, machine, coordinator = build()
        decision = coordinator.claim_turn(
            layer="voice", source=VoiceInputSource.ASR, turn_id="turn-explicit-1"
        )
        self.assertTrue(decision.granted)
        assert decision.claim is not None
        self.assertEqual(decision.claim.turn_id, "turn-explicit-1")
        coordinator.release_turn(decision.claim, reason="turn_completed")
        with self.assertRaises(ValueError):
            coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR, turn_id="bad id")

    def test_release_twice_foreign_and_forged_claims_are_rejected(self) -> None:
        clock, window, machine, coordinator = build()
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        claim = decision.claim
        assert claim is not None
        coordinator.release_turn(claim, reason="turn_completed")
        self.assertIsNone(coordinator.active_claim)
        self.assertEqual(coordinator.closed_claims, (claim,))
        with self.assertRaises(DialogError):
            coordinator.release_turn(claim, reason="turn_completed")
        with self.assertRaises(DialogError):
            coordinator.release_turn(forged_claim(window.window_id), reason="turn_completed")
        with self.assertRaises(DialogError):
            coordinator.release_turn(forged_claim(window.window_id), reason="free text reason")
        with self.assertRaises(TypeError):
            coordinator.release_turn("claim", reason="turn_completed")  # type: ignore[arg-type]
        other_machine = DialogStateMachine(
            source_instance_id="dialog-instance-2", window_id=window.window_id
        )
        other = DialogCoordinator(machine=other_machine, window=window)
        foreign = other.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(foreign.granted)
        assert foreign.claim is not None
        with self.assertRaises(DialogError):
            coordinator.release_turn(foreign.claim, reason="turn_completed")
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=foreign.claim)

    def test_stale_window_claims_are_invalidated_after_reopen(self) -> None:
        clock, window, machine, coordinator = build()
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        stale = decision.claim
        assert stale is not None
        window.close(reason=DialogWindowCloseReason.IDLE_TIMEOUT)
        window.open()
        self.assertNotEqual(window.window_id, stale.window_id)
        self.assertIsNone(coordinator.active_claim)
        self.assertIn(stale, coordinator.closed_claims)
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=stale)
        with self.assertRaises(DialogError):
            coordinator.release_turn(stale, reason="turn_completed")
        self.assertTrue(coordinator.request_input(layer="voice").granted)
        fresh = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(fresh.granted)

    def test_late_claim_events_are_rejected_without_migration(self) -> None:
        clock, window, machine, coordinator = build()
        coordinator.resume_listening(reason="listening_started")
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        claim = decision.claim
        assert claim is not None
        coordinator.note_speech_started(claim=claim)
        self.assertEqual(machine.state, DialogState.USER_SPEAKING)
        coordinator.release_turn(claim, reason="cancelled")
        before_state = machine.state
        before_count = len(machine.transitions)
        with self.assertRaises(DialogError):
            coordinator.note_speech_finished(claim=claim)
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=claim)
        self.assertEqual(machine.state, before_state)
        self.assertEqual(len(machine.transitions), before_count)
        with self.assertRaises(TypeError):
            coordinator.note_speech_started(claim="claim")  # type: ignore[arg-type]

    def test_claim_lifecycle_drives_speech_states(self) -> None:
        clock, window, machine, coordinator = build()
        self.assertEqual(
            coordinator.resume_listening(reason="listening_started").to_state,
            DialogState.LISTENING,
        )
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        claim = decision.claim
        assert claim is not None
        started = coordinator.note_speech_started(claim=claim)
        self.assertEqual(
            (started.from_state, started.to_state, started.reason),
            (DialogState.LISTENING, DialogState.USER_SPEAKING, "speech_started"),
        )
        finished = coordinator.note_speech_finished(claim=claim)
        self.assertEqual(
            (finished.from_state, finished.to_state, finished.reason),
            (DialogState.USER_SPEAKING, DialogState.TRANSCRIBING, "speech_finished"),
        )
        machine.transition(DialogState.THINKING, reason="transcription_ready")
        coordinator.release_turn(claim, reason="route_handled")
        self.assertIsNone(coordinator.active_claim)

    def test_repeated_speech_started_is_rejected(self) -> None:
        clock, window, machine, coordinator = build()
        coordinator.resume_listening(reason="listening_started")
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        claim = decision.claim
        assert claim is not None
        coordinator.note_speech_started(claim=claim)
        before = len(machine.transitions)
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=claim)
        self.assertEqual(machine.state, DialogState.USER_SPEAKING)
        self.assertEqual(len(machine.transitions), before)


class SuspensionTests(unittest.TestCase):
    def test_suspend_paths_block_input_and_retire_claims(self) -> None:
        clock, window, machine, coordinator = build()
        decision = coordinator.claim_turn(layer="text", source=VoiceInputSource.TEXT_SHELL)
        claim = decision.claim
        assert claim is not None
        machine.begin_text_turn()
        transition = coordinator.suspend_for_authorization(reason="authorization_required")
        self.assertEqual(
            (transition.from_state, transition.to_state),
            (DialogState.THINKING, DialogState.AWAITING_AUTHORIZATION),
        )
        self.assertIsNone(coordinator.active_claim)
        self.assertIn(claim, coordinator.closed_claims)
        self.assertFalse(coordinator.request_input(layer="voice").granted)
        self.assertFalse(
            coordinator.request_input(layer="text", source=VoiceInputSource.TEXT_SHELL).granted
        )
        with self.assertRaises(DialogError):
            coordinator.release_turn(claim, reason="cancelled")

    def test_suspend_for_execution_from_thinking(self) -> None:
        clock, window, machine, coordinator = build()
        machine.begin_text_turn()
        transition = coordinator.suspend_for_execution(reason="execution_requested")
        self.assertEqual(transition.to_state, DialogState.EXECUTING)
        self.assertFalse(machine.accepts_input)
        self.assertEqual(coordinator.request_input(layer="voice").reason, "state_blocked")

    def test_suspend_from_illegal_state_or_with_free_text_reason_is_rejected(self) -> None:
        clock, window, machine, coordinator = build()
        with self.assertRaises(DialogError):
            coordinator.suspend_for_authorization(reason="authorization_required")
        with self.assertRaises(DialogError):
            coordinator.suspend_for_execution(reason="free text reason")
        self.assertEqual(machine.state, DialogState.IDLE)
        self.assertEqual(machine.transitions, ())

    def test_resume_listening_requires_open_window_and_allowed_state(self) -> None:
        clock, window, machine, coordinator = build()
        self.assertEqual(
            coordinator.resume_listening(reason="listening_started").to_state,
            DialogState.LISTENING,
        )
        machine.transition(DialogState.IDLE, reason="turn_completed")
        window.close(reason=DialogWindowCloseReason.USER_CANCELLED)
        with self.assertRaises(DialogError):
            coordinator.resume_listening(reason="listening_started")
        _, window2, machine2, coordinator2 = build()
        machine2.begin_text_turn()
        coordinator2.suspend_for_authorization(reason="authorization_required")
        with self.assertRaises(DialogError):
            coordinator2.resume_listening(reason="listening_started")
        with self.assertRaises(DialogError):
            coordinator2.resume_listening(reason="free text reason")


class ConversationFlowTests(unittest.TestCase):
    def test_two_turn_conversation_inside_one_window(self) -> None:
        clock, window, machine, coordinator = build()
        window.note_activity()
        coordinator.resume_listening(reason="listening_started")
        first = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(first.granted)
        first_claim = first.claim
        assert first_claim is not None
        window.begin_turn()
        coordinator.note_speech_started(claim=first_claim)
        coordinator.note_speech_finished(claim=first_claim)
        machine.transition(DialogState.THINKING, reason="transcription_ready")
        coordinator.release_turn(first_claim, reason="route_handled")
        machine.transition(DialogState.SPEAKING, reason="reply_ready")
        machine.transition(DialogState.LISTENING, reason="speech_finished_output")
        window.end_turn()
        second = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(second.granted)
        second_claim = second.claim
        assert second_claim is not None
        self.assertNotEqual(second_claim.claim_id, first_claim.claim_id)
        self.assertEqual(second_claim.window_id, window.window_id)
        window.begin_turn()
        coordinator.note_speech_started(claim=second_claim)
        coordinator.note_speech_finished(claim=second_claim)
        machine.transition(DialogState.THINKING, reason="transcription_ready")
        coordinator.release_turn(second_claim, reason="route_handled")
        machine.transition(DialogState.IDLE, reason="turn_completed")
        self.assertEqual(window.status.turn_count, 2)
        self.assertTrue(window.is_open())
        self.assertTrue(coordinator.request_input(layer="voice").granted)


class InvalidationTests(unittest.TestCase):
    def test_invalidate_clears_claim_and_blocks_input(self) -> None:
        clock, window, machine, coordinator = build()
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        claim = decision.claim
        assert claim is not None
        coordinator.invalidate(reason="user_requested_text")
        self.assertIsNone(coordinator.active_claim)
        self.assertEqual(coordinator.closed_claims, (claim,))
        self.assertEqual(machine.state, DialogState.FAIL_CLOSED)
        self.assertFalse(coordinator.request_input(layer="voice").granted)
        with self.assertRaises(DialogError):
            coordinator.release_turn(claim, reason="cancelled")
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=claim)
        coordinator.invalidate(reason="recovered")
        self.assertEqual(machine.state, DialogState.FAIL_CLOSED)

    def test_invalidate_rejects_unrestricted_reasons(self) -> None:
        clock, window, machine, coordinator = build()
        with self.assertRaises(DialogError):
            coordinator.invalidate(reason="free text reason")
        with self.assertRaises(DialogError):
            coordinator.invalidate(reason="not_a_transition_reason")
        self.assertEqual(machine.state, DialogState.IDLE)
        self.assertEqual(machine.transitions, ())

    def test_invalidate_on_already_closed_machine_is_safe(self) -> None:
        clock, window, machine, coordinator = build()
        machine.close(reason="shutdown")
        coordinator.invalidate(reason="shutdown")
        self.assertEqual(machine.state, DialogState.CLOSED)
        self.assertEqual(coordinator.request_input(layer="voice").reason, "state_blocked")


class RecordValidationTests(unittest.TestCase):
    def test_turn_claim_and_decision_validation(self) -> None:
        claim = TurnClaim(
            claim_id="claim-1",
            window_id="dialog-window-1",
            turn_id="turn-1",
            source=VoiceInputSource.ASR,
            layer="voice",
            issued_at="2026-10-01T09:00:00.000Z",
        )
        self.assertIs(claim.source, VoiceInputSource.ASR)
        with self.assertRaises(ValueError):
            forged_claim("dialog-window-1", claim_id="")
        with self.assertRaises(ValueError):
            forged_claim("bad window id")
        with self.assertRaises(ValueError):
            forged_claim("dialog-window-1", issued_at="2026-10-01T09:00:00+08:00")
        # 字符串来源一律拒绝，不得被强转成枚举（冻结声明为 VoiceInputSource | None）。
        for source in ("asr", "text_shell", "microphone", ""):
            with self.subTest(source=source):
                with self.assertRaises(TypeError):
                    forged_claim("dialog-window-1", source=source)
        with self.assertRaises(DialogError):
            forged_claim("dialog-window-1", layer="screen")
        with self.assertRaises(ValueError):
            TurnClaimDecision(granted=True, claim=None, reason="granted")
        with self.assertRaises(ValueError):
            TurnClaimDecision(granted=False, claim=claim, reason="claim_active")
        with self.assertRaises(ValueError):
            TurnClaimDecision(granted=False, claim=None, reason="whatever")
        with self.assertRaises(TypeError):
            TurnClaimDecision(granted="yes", claim=claim, reason="granted")  # type: ignore[arg-type]
        self.assertTrue(TurnClaimDecision(granted=True, claim=claim, reason="granted").granted)
        with self.assertRaises(Exception):
            claim.turn_id = "other"  # type: ignore[misc]

    def test_input_permit_validation(self) -> None:
        with self.assertRaises(ValueError):
            InputPermit(granted=False, reason="not_a_refusal_reason", state=DialogState.IDLE, mode=None)
        with self.assertRaises(ValueError):
            InputPermit(granted=True, reason="window_closed", state=DialogState.IDLE, mode=None)
        with self.assertRaises(TypeError):
            InputPermit(granted=False, reason="window_closed", state="idle", mode=None)  # type: ignore[arg-type]
        with self.assertRaises(Exception):
            InputPermit(granted=True, reason="granted", state=DialogState.IDLE, mode=None).granted = False  # type: ignore[misc]

    def test_coordinator_argument_validation(self) -> None:
        clock, window, machine, coordinator = build()
        with self.assertRaises(TypeError):
            DialogCoordinator(machine="nope", window=window)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DialogCoordinator(machine=machine, window="nope")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DialogCoordinator(machine=machine, window=window, diagnostic_sink="nope")  # type: ignore[arg-type]
        self.assertIs(coordinator.machine, machine)
        self.assertIs(coordinator.window, window)

    def test_coordinator_diagnostics_are_redacted(self) -> None:
        events: list[DialogDiagnostic] = []
        clock, window, machine, coordinator = build(sink=events.append)
        coordinator.request_input(layer="voice", source=VoiceInputSource.ASR)
        decision = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        claim = decision.claim
        assert claim is not None
        coordinator.release_turn(claim, reason="turn_completed")
        self.assertEqual(
            {event.event_type for event in events},
            {"dialog_input_evaluated", "dialog_turn_claimed", "dialog_turn_released"},
        )
        for event in events:
            self.assertIsInstance(event, DialogDiagnostic)
            self.assertEqual(set(event.to_details()), {"reason", "state", "mode"})
        self.assertEqual(events[0].reason, "granted")
        self.assertEqual(events[-1].reason, "turn_completed")

    def test_dialog_modules_have_no_forbidden_imports_or_entrypoints(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2] / "app" / "dialog"
        forbidden_tokens = (
            "app.safety",
            "app.executor",
            "app.plans",
            "app.auditing",
            "app.verification",
            "app.tools",
            "app.tts",
            "app.providers",
            "app.asr",
            "app.audio",
            "app.runtime",
            "subprocess",
            "socket",
            "threading",
            "multiprocessing",
            "asyncio",
            "os.system",
            "requests",
            "urllib",
            "shutil",
        )
        entry_point = re.compile(r"(?<![\w.])(?<!def )open\s*\(")
        files = sorted(root.glob("*.py"))
        self.assertEqual(len(files), 4)
        for path in files:
            source = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, source)
            with self.subTest(file=path.name, token="open("):
                self.assertIsNone(entry_point.search(source))
        coordinator_source = (root / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("from app.voice_loop.boundary import", coordinator_source)


if __name__ == "__main__":
    unittest.main()
