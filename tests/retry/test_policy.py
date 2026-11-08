from __future__ import annotations

import unittest

from app.contracts import ContractError
from app.retry import (
    ObservedState,
    ReadOnlyRetryRunner,
    RetryDisposition,
    RetryLayer,
    RetryPolicy,
    RetryableReadOnlyError,
)


class RetryPolicyTests(unittest.TestCase):
    def test_read_only_retries_are_bounded_with_capped_backoff(self) -> None:
        policy = RetryPolicy(read_only_max_attempts=3, read_only_base_delay_seconds=1.0, read_only_max_delay_seconds=1.5)
        first = policy.read_only(attempt=1, transient=True)
        second = policy.read_only(attempt=2, transient=True)
        terminal = policy.read_only(attempt=3, transient=True)
        self.assertEqual(first.disposition, RetryDisposition.RETRY)
        self.assertEqual(first.delay_seconds, 1.0)
        self.assertEqual(second.delay_seconds, 1.5)
        self.assertEqual(terminal.disposition, RetryDisposition.NO_RETRY)

    def test_read_only_non_transient_failure_is_not_retried(self) -> None:
        decision = RetryPolicy().read_only(attempt=1, transient=False)
        self.assertEqual(decision.layer, RetryLayer.READ_ONLY_QUERY)
        self.assertEqual(decision.disposition, RetryDisposition.NO_RETRY)

    def test_agent_retry_requires_a_new_request(self) -> None:
        policy = RetryPolicy()
        blocked = policy.agent_generation(request_id="request-1", attempt=1, transient=True)
        allowed = policy.agent_generation(
            request_id="request-1", attempt=1, transient=True, replacement_request_id="request-2"
        )
        self.assertEqual(blocked.disposition, RetryDisposition.NEW_REQUEST_REQUIRED)
        self.assertIsNone(blocked.replacement_request_id)
        self.assertEqual(allowed.replacement_request_id, "request-2")
        with self.assertRaises(ContractError):
            policy.agent_generation(
                request_id="request-1", attempt=1, transient=True, replacement_request_id="request-1"
            )

    def test_side_effect_is_never_automatically_retried(self) -> None:
        policy = RetryPolicy()
        for state in (ObservedState.COMPLETED, ObservedState.NOT_COMPLETED):
            with self.subTest(state=state):
                decision = policy.side_effect(attempt=1, observed_state=state)
                self.assertEqual(decision.disposition, RetryDisposition.NO_RETRY)

    def test_unknown_side_effect_result_requires_read_only_scan(self) -> None:
        decision = RetryPolicy().side_effect(attempt=1, observed_state=ObservedState.UNKNOWN_RESULT)
        self.assertEqual(decision.disposition, RetryDisposition.SCAN_BEFORE_DECISION)

    def test_state_uncertain_is_a_terminal_block_not_retry_or_status_rewrite(self) -> None:
        decision = RetryPolicy().side_effect(attempt=1, observed_state=ObservedState.STATE_UNCERTAIN)
        self.assertEqual(decision.disposition, RetryDisposition.BLOCKED_STATE_UNCERTAIN)
        self.assertNotIn(decision.disposition, {RetryDisposition.RETRY, RetryDisposition.NO_RETRY})

    def test_unified_entry_point_rejects_cross_layer_facts(self) -> None:
        policy = RetryPolicy()
        with self.assertRaises(ContractError):
            policy.decide(layer=RetryLayer.READ_ONLY_QUERY, attempt=1, transient=True, request_id="request-1")
        with self.assertRaises(ContractError):
            policy.decide(layer=RetryLayer.SIDE_EFFECT_ACTION, attempt=1, observed_state=ObservedState.STATE_UNCERTAIN, request_id="request-1")

    def test_unclassified_read_only_exception_is_not_retried(self) -> None:
        calls = 0

        def query() -> object:
            nonlocal calls
            calls += 1
            raise ValueError("not transient")

        with self.assertRaises(ValueError):
            ReadOnlyRetryRunner(RetryPolicy(), sleep=lambda _seconds: self.fail("must not sleep")).run(query)
        self.assertEqual(calls, 1)
    def test_read_only_runner_has_a_hard_attempt_bound(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def query() -> object:
            nonlocal calls
            calls += 1
            raise RetryableReadOnlyError("temporary")

        with self.assertRaises(RetryableReadOnlyError):
            ReadOnlyRetryRunner(RetryPolicy(read_only_max_attempts=3), sleep=sleeps.append).run(query)
        self.assertEqual(calls, 3)
        self.assertEqual(len(sleeps), 2)


if __name__ == "__main__":
    unittest.main()
