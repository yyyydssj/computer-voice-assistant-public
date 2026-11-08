from __future__ import annotations

import unittest

from app.state import (
    InputState,
    OutputState,
    ProviderLifecycleState,
    StateTransitionError,
    TaskState,
    transition_input,
    transition_output,
    transition_provider,
    transition_task,
)


class OrthogonalStateTests(unittest.TestCase):
    def test_state_enum_sets_match_the_frozen_roadmap(self) -> None:
        self.assertEqual(
            {state.value for state in TaskState},
            {"IDLE", "UNDERSTANDING", "PLANNING", "WAITING_AUTH", "EXECUTING", "VERIFYING", "COMPLETED", "CANCELLED", "ABORTED", "UNCERTAIN"},
        )
        self.assertEqual(
            {state.value for state in ProviderLifecycleState},
            {"READY", "FAILED", "COOLDOWN", "PROBING", "RECOVERED_PENDING"},
        )
        self.assertEqual(
            {state.value for state in InputState},
            {"IDLE", "WARMING", "ACTIVE", "FINAL_ACCEPTED", "REJECTED", "CLOSING", "FAILED"},
        )
        self.assertEqual(
            {state.value for state in OutputState},
            {"QUEUED", "SYNTHESIZING", "READY", "COMMITTED_NOT_STARTED", "PLAYING", "PLAYED", "ABORTED"},
        )

    def test_task_happy_path_is_explicit(self) -> None:
        state = TaskState.IDLE
        for target in (TaskState.UNDERSTANDING, TaskState.PLANNING, TaskState.WAITING_AUTH, TaskState.EXECUTING, TaskState.VERIFYING, TaskState.COMPLETED):
            state = transition_task(state, target)
        self.assertEqual(state, TaskState.COMPLETED)

    def test_illegal_task_transition_is_rejected(self) -> None:
        with self.assertRaisesRegex(StateTransitionError, "IDLE.*EXECUTING"):
            transition_task(TaskState.IDLE, TaskState.EXECUTING)

    def test_uncertain_is_not_implicitly_success_failure_or_retry(self) -> None:
        for target in (TaskState.COMPLETED, TaskState.ABORTED, TaskState.EXECUTING):
            with self.subTest(target=target):
                with self.assertRaises(StateTransitionError):
                    transition_task(TaskState.UNCERTAIN, target)

    def test_provider_input_and_output_are_independent_machines(self) -> None:
        self.assertEqual(transition_provider(ProviderLifecycleState.FAILED, ProviderLifecycleState.COOLDOWN), ProviderLifecycleState.COOLDOWN)
        self.assertEqual(transition_input(InputState.ACTIVE, InputState.FINAL_ACCEPTED), InputState.FINAL_ACCEPTED)
        self.assertEqual(transition_output(OutputState.READY, OutputState.COMMITTED_NOT_STARTED), OutputState.COMMITTED_NOT_STARTED)
        with self.assertRaises(StateTransitionError):
            transition_output(OutputState.PLAYING, OutputState.SYNTHESIZING)


if __name__ == "__main__":
    unittest.main()