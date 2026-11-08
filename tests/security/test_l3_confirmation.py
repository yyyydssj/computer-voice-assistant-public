from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from app.plans import build_plan_action, build_reviewable_plan
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.safety.confirmation import AuthorizationConsumptionError, AuthorizationConsumptionStore, ConfirmationError, L3ConfirmationController
from app.tools import build_mvp_tool_registry

HASH_A = "sha256:" + "a" * 64
CREATED_AT = "2026-09-20T09:00:00Z"
EXPIRES_AT = "2026-09-20T09:05:00Z"


def path_snapshot(path: str = r"G:\study\project\old.txt") -> PathSnapshot:
    return PathSnapshot(requested_path=path, purpose="recycle_target", normalized_path=path, resolved_path=path,
        exists=True, target_identity=(1, 5), anchor_path=path, anchor_identity=(1, 5),
        component_identities=(("G:\\", (1, 1)), (r"G:\study", (1, 2)), (path, (1, 5))))


class L3ConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.engine = PolicyEngine(self.registry, path_revalidator=lambda value: value)
        self.store = AuthorizationConsumptionStore(instance_id="instance-001")
        self.controller = L3ConfirmationController(instance_id="instance-001", authorization_store=self.store)

    def make_action(self, tool_name="move_to_recycle_bin", *, action_id="action-1", order=1):
        manifest = self.registry.require(tool_name)
        if tool_name == "move_to_recycle_bin":
            value = path_snapshot(); arguments = {"paths": [value.normalized_path], "recursive": False}; snapshots = (value,)
            preview = {"paths": [value.normalized_path], "item_count": 1, "permanent_delete": False}
        else:
            arguments = {"application_id": "approved.editor"}; snapshots = (); preview = {"application_id": "approved.editor"}
        policy_input = PolicyInput(tool_name=manifest.name, tool_version=manifest.version, manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level, requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments, path_snapshots=snapshots)
        return build_plan_action(action_id=action_id, policy_input=policy_input, policy_decision=self.engine.evaluate(policy_input),
            preview=preview, verification_method=manifest.verification_method, rollback_strategy=manifest.rollback_strategy, order=order)

    def make_plan(self, *, actions=None, version=1):
        return build_reviewable_plan(plan_id="plan-001", plan_version=version, task_id="task-001", created_at=CREATED_AT,
            goal="把指定文件移入回收站", scope=(r"G:\study\project",), exclusions=("禁止永久删除",), file_rules=(),
            recursive=False, conflict_policy="deny", actions=tuple(actions or (self.make_action(),)))

    def begin(self, *, plan=None, window_id="window-001", auth_id="auth-001"):
        return self.controller.begin(plan=plan or self.make_plan(), session_id="session-001", operation_id="operation-001",
            authorization_id=auth_id, confirmation_window_id=window_id, target_state_hash=HASH_A,
            created_at=CREATED_AT, expires_at=EXPIRES_AT)

    def allow(self, window, *, plan=None, at="2026-09-20T09:01:00Z"):
        return self.controller.resolve(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
            event="allow", occurred_at=at, current_plan=plan or self.make_plan(), current_target_state_hash=HASH_A)

    def authorize(self):
        window=self.begin(); self.allow(window)
        result=self.controller.submit_phrase(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
            phrase="确认执行", occurred_at="2026-09-20T09:02:00Z", current_plan=self.make_plan(), current_target_state_hash=HASH_A)
        self.assertIsNotNone(result.authorization); return result.authorization

    def test_two_stage_order_and_exact_phrase(self):
        window=self.begin(); self.assertEqual(window.stage, "modal_pending"); self.assertEqual(window.prompt.confirmation_phrase, "确认执行")
        first=self.allow(window); self.assertEqual(first.status, "awaiting_phrase"); self.assertIsNone(first.authorization)
        for phrase in ("确认", "允许执行", "确认执行吧", " 确认执行", "确认执行 ", "好", "可以"):
            result=self.controller.submit_phrase(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
                phrase=phrase, occurred_at="2026-09-20T09:01:30Z")
            self.assertEqual((result.status, result.reason_code), ("awaiting_phrase", "phrase_mismatch")); self.assertIsNone(result.authorization)
        result=self.controller.submit_phrase(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
            phrase="确认执行", occurred_at="2026-09-20T09:02:00Z", current_plan=self.make_plan(), current_target_state_hash=HASH_A)
        auth=result.authorization; self.assertEqual(result.status, "authorized"); self.assertIsNotNone(auth)
        self.assertEqual((auth.issuer_instance_id, auth.operation_id, auth.risk_level), ("instance-001", "operation-001", "L3"))
        self.assertEqual((auth.authorization_method, auth.confirmation_stage, auth.confirmation_phrase), ("modal_and_phrase", "phrase", "确认执行"))
        self.assertIsNone(auth.consumed_at)

    def test_phrase_before_modal_and_second_allow_are_rejected(self):
        window=self.begin()
        with self.assertRaises(ConfirmationError) as caught:
            self.controller.submit_phrase(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
                phrase="确认执行", occurred_at="2026-09-20T09:00:30Z", current_plan=self.make_plan(), current_target_state_hash=HASH_A)
        self.assertEqual(caught.exception.code, "confirmation_stage_mismatch")
        self.allow(window)
        with self.assertRaises(ConfirmationError) as caught: self.allow(window, at="2026-09-20T09:01:30Z")
        self.assertEqual(caught.exception.code, "confirmation_stage_mismatch")

    def test_wrong_window_session_expiry_old_plan_and_target_drift_fail_closed(self):
        window=self.begin(); self.allow(window)
        for field, value, code in (("confirmation_window_id", "window-other", "confirmation_window_mismatch"), ("session_id", "session-other", "session_mismatch")):
            kwargs=dict(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id, phrase="确认执行", occurred_at="2026-09-20T09:02:00Z")
            kwargs[field]=value
            with self.assertRaises(ConfirmationError) as caught: self.controller.submit_phrase(**kwargs)
            self.assertEqual(caught.exception.code, code)
        expired=self.controller.submit_phrase(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
            phrase="确认执行", occurred_at=EXPIRES_AT, current_plan=self.make_plan(), current_target_state_hash=HASH_A)
        self.assertEqual(expired.status, "expired"); self.assertIsNone(expired.authorization)
        for suffix, plan, target, reason in (("plan", self.make_plan(version=2), HASH_A, "plan_changed"), ("target", self.make_plan(), "sha256:"+"b"*64, "target_state_changed")):
            window=self.begin(window_id="window-"+suffix, auth_id="auth-"+suffix); self.allow(window)
            result=self.controller.submit_phrase(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
                phrase="确认执行", occurred_at="2026-09-20T09:02:00Z", current_plan=plan, current_target_state_hash=target)
            self.assertEqual((result.status, result.reason_code), ("invalidated", reason)); self.assertIsNone(result.authorization)

    def test_cancel_events_work_in_both_stages_and_non_l3_plans_are_rejected(self):
        for stage in ("modal", "phrase"):
            for event in ("deny", "close", "escape", "stop"):
                controller=L3ConfirmationController(instance_id="instance-001")
                window=controller.begin(plan=self.make_plan(), session_id="session-001", operation_id="operation-001",
                    authorization_id=f"auth-{stage}-{event}", confirmation_window_id=f"window-{stage}-{event}", target_state_hash=HASH_A,
                    created_at=CREATED_AT, expires_at=EXPIRES_AT)
                if stage == "phrase": controller.resolve(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
                    event="allow", occurred_at="2026-09-20T09:01:00Z", current_plan=self.make_plan(), current_target_state_hash=HASH_A)
                result=controller.resolve(confirmation_window_id=window.confirmation_window_id, session_id=window.session_id,
                    event=event, occurred_at="2026-09-20T09:02:00Z")
                self.assertIsNone(result.authorization); self.assertIsNone(controller.active_window)
        for actions in ((self.make_action("start_whitelisted_application"),),
                        (self.make_action(), self.make_action("start_whitelisted_application", action_id="action-2", order=2))):
            with self.assertRaises(ConfirmationError) as caught: self.begin(plan=self.make_plan(actions=actions))
            self.assertEqual(caught.exception.code, "l3_plan_required")

    def consume_kwargs(self):
        return dict(issuer_instance_id="instance-001", session_id="session-001", operation_id="operation-001", plan_id="plan-001",
            plan_version=1, plan_hash=self.make_plan().plan_hash, target_state_hash=HASH_A, action_ids=("action-1",),
            consumed_at="2026-09-20T09:03:00Z")

    def test_consumption_is_exact_atomic_single_use_and_restart_safe(self):
        auth=self.authorize(); kwargs=self.consume_kwargs(); consumed=self.store.consume(auth, **kwargs)
        self.assertEqual(consumed.consumed_at, kwargs["consumed_at"])
        with self.assertRaises(AuthorizationConsumptionError) as caught: self.store.consume(auth, **kwargs)
        self.assertEqual(caught.exception.code, "authorization_replayed")
        for store, code in ((AuthorizationConsumptionStore(instance_id="instance-001"), "authorization_unknown"),
                            (AuthorizationConsumptionStore(instance_id="instance-002"), "authorization_wrong_instance")):
            with self.assertRaises(AuthorizationConsumptionError) as caught: store.consume(auth, **kwargs)
            self.assertEqual(caught.exception.code, code)

    def test_consumption_rejects_each_changed_frozen_fact(self):
        auth=self.authorize(); base=self.consume_kwargs()
        changes=(
            {"session_id":"session-other"}, {"operation_id":"operation-other"}, {"plan_id":"plan-other"},
            {"plan_version":2}, {"plan_hash":"sha256:"+"c"*64}, {"target_state_hash":"sha256:"+"d"*64},
            {"action_ids":("action-other",)}, {"consumed_at":EXPIRES_AT},
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(AuthorizationConsumptionError) as caught:
                    self.store.consume(auth, **{**base, **change})
                self.assertEqual(caught.exception.code, "authorization_facts_mismatch")

    def test_concurrent_duplicate_consumption_has_one_winner(self):
        auth=self.authorize(); kwargs=self.consume_kwargs()
        def attempt(_):
            try: self.store.consume(auth, **kwargs); return "consumed"
            except AuthorizationConsumptionError as exc: return exc.code
        with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(attempt, range(8)))
        self.assertEqual(results.count("consumed"), 1); self.assertEqual(results.count("authorization_replayed"), 7)


if __name__ == "__main__": unittest.main()
