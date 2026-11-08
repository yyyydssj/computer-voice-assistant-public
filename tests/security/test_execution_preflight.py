from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

from app.contracts import AuthorizationSnapshot
from app.plans import build_plan_action, build_reviewable_plan
from app.safety import PathGuardError, PathSnapshot, PolicyEngine, PolicyInput
from app.safety.confirmation import AuthorizationConsumptionError, AuthorizationConsumptionStore
from app.safety.preflight import ExecutionPreflightGate, PreflightError
from app.tools import ToolRegistry, build_mvp_tool_registry

CREATED_AT = "2026-09-20T10:00:00Z"
EXPIRES_AT = "2026-09-20T10:05:00Z"
CHECKED_AT = "2026-09-20T10:03:00Z"
SOURCE_HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64
OTHER_HASH = "sha256:" + "3" * 64


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def snapshot(path: str, *, purpose: str, exists: bool) -> PathSnapshot:
    parent = r"G:\study\project"
    components = (("G:\\", (1, 1)), (r"G:\study", (1, 2)), (parent, (1, 4)))
    if exists:
        components += ((path, (1, 5)),)
    return PathSnapshot(
        requested_path=path,
        purpose=purpose,
        normalized_path=path,
        resolved_path=path,
        exists=exists,
        target_identity=(1, 5) if exists else None,
        anchor_path=path if exists else parent,
        anchor_identity=(1, 5) if exists else (1, 4),
        component_identities=components,
    )


class RecordingStore(AuthorizationConsumptionStore):
    def __init__(self, *, instance_id: str, events: list[str]) -> None:
        super().__init__(instance_id=instance_id)
        self.events = events

    def consume(self, authorization: AuthorizationSnapshot, **kwargs):
        self.events.append("consume")
        return super().consume(authorization, **kwargs)


class ExecutionPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.events: list[str] = []
        self.store = RecordingStore(instance_id="instance-001", events=self.events)
        self.current_source_hash = SOURCE_HASH
        self.current_target_hash = TARGET_HASH
        self.path_error: PathGuardError | None = None
        self.gate = self.make_gate()

    def revalidate(self, value: PathSnapshot) -> PathSnapshot:
        self.events.append(f"path:{value.purpose}")
        if self.path_error is not None:
            raise self.path_error
        return value

    def target_state_hash(self, plan) -> str:
        self.events.append("target_state")
        return self.current_target_hash

    def content_hash(self, path: str) -> str:
        self.events.append(f"content:{path}")
        return self.current_source_hash

    def make_gate(self, *, registry=None, store=None, instance_id="instance-001", application_resolver=None):
        return ExecutionPreflightGate(
            instance_id=instance_id,
            registry=registry or self.registry,
            authorization_store=store or self.store,
            path_revalidator=self.revalidate,
            target_state_hasher=self.target_state_hash,
            content_hash_reader=self.content_hash,
            application_resolver=application_resolver,
        )

    def make_write_action(self, *, registry=None, action_id="action-1", order=1, preview_override=None):
        registry = registry or self.registry
        manifest = registry.require("write_text_file")
        value = snapshot(r"G:\study\project\note.txt", purpose="write_target", exists=True)
        arguments = {
            "path": value.normalized_path,
            "mode": "append",
            "content": "tail",
            "expected_source_hash": SOURCE_HASH,
        }
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=(value,),
        )
        decision = PolicyEngine(registry, path_revalidator=lambda item: item).evaluate(policy_input)
        preview = {
            "path": value.normalized_path,
            "mode": "append",
            "expected_source_hash": SOURCE_HASH,
            "content_size_bytes": 4,
            "content_hash": sha256_text("tail"),
            "whole_file_rewrite": False,
        }
        if preview_override:
            preview.update(preview_override)
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=decision,
            preview=preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_copy_action(self, *, action_id="action-2", order=2):
        manifest = self.registry.require("copy_file")
        source = snapshot(r"G:\study\project\source.txt", purpose="copy_file_source", exists=True)
        destination = snapshot(r"G:\study\project\copy.txt", purpose="copy_file_destination", exists=False)
        arguments = {
            "source": source.normalized_path,
            "destination": destination.normalized_path,
            "recursive": False,
            "conflict_policy": "deny",
        }
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=(source, destination),
        )
        decision = PolicyEngine(self.registry, path_revalidator=lambda item: item).evaluate(policy_input)
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=decision,
            preview={**arguments, "overwrite": False, "max_items": 20, "reparse_target": "deny"},
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_recycle_action(self, *, action_id="action-recycle", order=1):
        manifest = self.registry.require("move_to_recycle_bin")
        first = snapshot(r"G:\study\project\old-1.txt", purpose="recycle_target", exists=True)
        second = snapshot(r"G:\study\project\old-2.txt", purpose="recycle_target", exists=True)
        arguments = {"paths": [first.normalized_path, second.normalized_path], "recursive": False}
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=(first, second),
        )
        decision = PolicyEngine(self.registry, path_revalidator=lambda item: item).evaluate(policy_input)
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=decision,
            preview={
                "paths": arguments["paths"],
                "item_count": 2,
                "recursive": False,
                "permanent_delete": False,
                "reparse_target": "deny",
            },
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_application_action(self, *, application_name="记事本", action_id="action-app", order=1):
        manifest = self.registry.require("start_whitelisted_application")
        arguments = {"application_id": "notepad"}
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=(),
        )
        decision = PolicyEngine(self.registry, path_revalidator=lambda item: item).evaluate(policy_input)
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=decision,
            preview={"application_id": "notepad", "application_name": application_name},
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_plan(self, *, actions=None, version=1):
        return build_reviewable_plan(
            plan_id="plan-001",
            plan_version=version,
            task_id="task-001",
            created_at=CREATED_AT,
            goal="安全修改文件",
            scope=(r"G:\study\project",),
            exclusions=("不覆盖目标",),
            file_rules=("执行前复检",),
            recursive=False,
            conflict_policy="deny",
            actions=tuple(actions or (self.make_write_action(),)),
        )

    def authorize(self, plan=None, *, store=None, issuer="instance-001", risk_level="L2") -> AuthorizationSnapshot:
        plan = plan or self.make_plan()
        authorization = AuthorizationSnapshot(
            authorization_id="auth-001",
            issuer_instance_id=issuer,
            session_id="session-001",
            operation_id="operation-001",
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            target_state_hash=TARGET_HASH,
            allowed_action_ids=tuple(item.action_id for item in plan.actions),
            risk_level=risk_level,
            confirmation_window_id="window-001",
            authorization_method="modal_and_phrase" if risk_level == "L3" else "modal",
            confirmation_stage="phrase" if risk_level == "L3" else "modal",
            confirmation_phrase="确认执行" if risk_level == "L3" else None,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        (store or self.store).register(authorization)
        return authorization

    def check(self, authorization, plan=None, **changes):
        return self.gate.check(
            authorization=authorization,
            session_id=changes.get("session_id", "session-001"),
            operation_id=changes.get("operation_id", "operation-001"),
            current_plan=plan or self.make_plan(),
            checked_at=changes.get("checked_at", CHECKED_AT),
        )

    def assert_invalidated(self, authorization, plan) -> None:
        with self.assertRaises(AuthorizationConsumptionError) as caught:
            self.store.consume(
                authorization,
                issuer_instance_id="instance-001",
                session_id="session-001",
                operation_id="operation-001",
                plan_id=plan.plan_id,
                plan_version=plan.plan_version,
                plan_hash=plan.plan_hash,
                target_state_hash=TARGET_HASH,
                action_ids=tuple(item.action_id for item in plan.actions),
                consumed_at=CHECKED_AT,
            )
        self.assertEqual(caught.exception.code, "authorization_invalidated")

    def test_success_revalidates_everything_before_atomic_consumption(self) -> None:
        plan = self.make_plan(actions=(self.make_write_action(), self.make_copy_action()))
        authorization = self.authorize(plan)
        approval = self.check(authorization, plan)
        self.assertEqual(approval.authorization.consumed_at, CHECKED_AT)
        self.assertEqual(approval.action_ids, ("action-1", "action-2"))
        self.assertFalse(hasattr(approval, "execute"))
        with self.assertRaises(FrozenInstanceError):
            approval.plan_version = 99  # type: ignore[misc]
        self.assertEqual(self.events[-1], "consume")
        self.assertLess(self.events.index("target_state"), self.events.index("consume"))
        self.assertTrue(all(index < self.events.index("consume") for index, value in enumerate(self.events) if value.startswith("path:")))

    def test_exact_authorization_binding_rejects_session_operation_plan_version_and_action_set(self) -> None:
        cases = (
            (self.make_plan(), {"session_id": "session-other"}, "authorization_facts_mismatch"),
            (self.make_plan(), {"operation_id": "operation-other"}, "authorization_facts_mismatch"),
            (self.make_plan(version=2), {}, "authorization_facts_mismatch"),
            (self.make_plan(actions=(self.make_write_action(), self.make_copy_action())), {}, "authorization_facts_mismatch"),
        )
        for index, (current, changes, code) in enumerate(cases):
            with self.subTest(index=index):
                store = AuthorizationConsumptionStore(instance_id="instance-001")
                gate = self.make_gate(store=store)
                original = self.make_plan()
                authorization = self.authorize(original, store=store)
                with self.assertRaises(PreflightError) as caught:
                    gate.check(authorization=authorization, session_id=changes.get("session_id", "session-001"),
                        operation_id=changes.get("operation_id", "operation-001"), current_plan=current, checked_at=CHECKED_AT)
                self.assertEqual(caught.exception.code, code)

    def test_manifest_and_policy_are_re_evaluated_against_current_local_versions(self) -> None:
        manifests = tuple(replace(item, version="2.0.0") if item.name == "write_text_file" else item
                          for item in self.registry.manifests())
        old_registry = ToolRegistry(manifests)
        stale_plan = self.make_plan(actions=(self.make_write_action(registry=old_registry),))
        authorization = self.authorize(stale_plan)
        with self.assertRaises(PreflightError) as caught:
            self.check(authorization, stale_plan)
        self.assertEqual(caught.exception.code, "manifest_revalidation_failed")
        self.assert_invalidated(authorization, stale_plan)

    def test_schema_validated_normalized_arguments_are_rebuilt_exactly(self) -> None:
        manifest = self.registry.require("copy_file")
        source = snapshot(r"G:\study\project\source.txt", purpose="copy_file_source", exists=True)
        destination = snapshot(r"G:\study\project\copy.txt", purpose="copy_file_destination", exists=False)
        arguments = {
            "source": source.normalized_path,
            "destination": destination.normalized_path,
            "recursive": False,
            "conflict_policy": "deny",
            "unexpected_but_policy_ignored": True,
        }
        policy_input = PolicyInput(tool_name=manifest.name, tool_version=manifest.version,
            manifest_hash=manifest.content_hash, risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation, normalized_arguments=arguments,
            path_snapshots=(source, destination))
        decision = PolicyEngine(self.registry, path_revalidator=lambda item: item).evaluate(policy_input)
        action = build_plan_action(action_id="action-1", policy_input=policy_input, policy_decision=decision,
            preview={"source": source.normalized_path, "destination": destination.normalized_path,
                "recursive": False, "conflict_policy": "deny", "overwrite": False,
                "max_items": 20, "reparse_target": "deny"},
            verification_method=manifest.verification_method, rollback_strategy=manifest.rollback_strategy)
        plan = self.make_plan(actions=(action,))
        authorization = self.authorize(plan)
        with self.assertRaises(PreflightError) as caught:
            self.check(authorization, plan)
        self.assertEqual(caught.exception.code, "parameter_revalidation_failed")
        self.assert_invalidated(authorization, plan)


    def test_tampered_plan_arguments_or_plan_hash_never_reach_consumption(self) -> None:
        plan = self.make_plan()
        authorization = self.authorize(plan)
        object.__setattr__(plan.actions[0], "normalized_arguments", {"path": r"G:\study\other.txt"})
        with self.assertRaises(PreflightError) as caught:
            self.check(authorization, plan)
        self.assertEqual(caught.exception.code, "plan_integrity_failed")
        self.assertNotIn("consume", self.events)
        self.assert_invalidated(authorization, plan)

    def test_path_identity_existence_link_and_parent_races_invalidate_whole_authorization(self) -> None:
        for index, code in enumerate(("path_identity_changed", "target_appeared", "reparse_point_denied", "anchor_identity_changed")):
            with self.subTest(code=code):
                self.setUp()
                plan = self.make_plan()
                authorization = self.authorize(plan)
                self.path_error = PathGuardError(code, "changed", path=r"G:\study\project\note.txt", purpose="write_target")
                with self.assertRaises(PreflightError) as caught:
                    self.check(authorization, plan)
                self.assertEqual(caught.exception.code, "path_revalidation_failed")
                self.assert_invalidated(authorization, plan)

    def test_source_content_hash_write_content_hash_and_target_state_are_rechecked(self) -> None:
        cases = (
            ("source", OTHER_HASH, TARGET_HASH, None, "source_hash_changed"),
            ("target", SOURCE_HASH, OTHER_HASH, None, "target_state_changed"),
            ("content", SOURCE_HASH, TARGET_HASH, {"content_hash": OTHER_HASH}, "action_hash_mismatch"),
        )
        for name, source_hash, target_hash, preview_override, expected in cases:
            with self.subTest(name=name):
                self.setUp()
                self.current_source_hash = source_hash
                self.current_target_hash = target_hash
                plan = self.make_plan(actions=(self.make_write_action(preview_override=preview_override),))
                authorization = self.authorize(plan)
                with self.assertRaises(PreflightError) as caught:
                    self.check(authorization, plan)
                self.assertEqual(caught.exception.code, expected)
                self.assert_invalidated(authorization, plan)

    def test_failure_in_any_action_creates_no_approval_and_cannot_be_reused(self) -> None:
        plan = self.make_plan(actions=(self.make_write_action(), self.make_copy_action()))
        authorization = self.authorize(plan)
        calls = 0

        def fail_second(value: PathSnapshot) -> PathSnapshot:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise PathGuardError("target_appeared", "destination appeared", path=value.normalized_path, purpose=value.purpose)
            return value

        gate = ExecutionPreflightGate(instance_id="instance-001", registry=self.registry,
            authorization_store=self.store, path_revalidator=fail_second,
            target_state_hasher=self.target_state_hash, content_hash_reader=self.content_hash)
        with self.assertRaises(PreflightError) as caught:
            gate.check(authorization=authorization, session_id="session-001", operation_id="operation-001",
                current_plan=plan, checked_at=CHECKED_AT)
        self.assertEqual(caught.exception.code, "path_revalidation_failed")
        self.assert_invalidated(authorization, plan)

    def test_repeated_late_and_old_instance_requests_cannot_produce_approval(self) -> None:
        plan = self.make_plan()
        authorization = self.authorize(plan)
        self.check(authorization, plan)
        with self.assertRaises(PreflightError) as caught:
            self.check(authorization, plan)
        self.assertEqual(caught.exception.code, "authorization_replayed")

        store = AuthorizationConsumptionStore(instance_id="instance-001")
        gate = self.make_gate(store=store)
        late = self.authorize(plan, store=store)
        with self.assertRaises(PreflightError) as caught:
            gate.check(authorization=late, session_id="session-001", operation_id="operation-001",
                current_plan=plan, checked_at=EXPIRES_AT)
        self.assertEqual(caught.exception.code, "authorization_facts_mismatch")

        wrong_store = AuthorizationConsumptionStore(instance_id="instance-002")
        wrong_gate = self.make_gate(instance_id="instance-002", store=wrong_store)
        with self.assertRaises(PreflightError) as caught:
            wrong_gate.check(authorization=authorization, session_id="session-001", operation_id="operation-001",
                current_plan=plan, checked_at=CHECKED_AT)
        self.assertEqual(caught.exception.code, "authorization_wrong_instance")

    def test_authorization_risk_must_match_every_action(self) -> None:
        plan = self.make_plan()
        authorization = self.authorize(plan, risk_level="L3")
        with self.assertRaises(PreflightError) as caught:
            self.check(authorization, plan)
        self.assertEqual(caught.exception.code, "authorization_risk_mismatch")
        self.assert_invalidated(authorization, plan)

    def test_multiple_paths_with_same_purpose_are_rebuilt_by_purpose_and_path(self) -> None:
        plan = self.make_plan(actions=(self.make_recycle_action(),))
        authorization = self.authorize(plan, risk_level="L3")
        approval = self.check(authorization, plan)
        self.assertEqual(approval.action_ids, ("action-recycle",))
        self.assertEqual(self.events.count("path:recycle_target"), 2)

    def test_application_whitelist_is_re_resolved_and_fails_closed(self) -> None:
        def unavailable(application_id):
            raise OSError("registry unavailable")

        for mode, resolver, expected in (
            ("missing", None, "parameter_revalidation_failed"),
            ("unavailable", unavailable, "parameter_revalidation_failed"),
            ("changed", lambda application_id: "其他应用", "parameter_revalidation_failed"),
            ("stable", lambda application_id: "记事本", None),
        ):
            with self.subTest(mode=mode):
                self.setUp()
                plan = self.make_plan(actions=(self.make_application_action(),))
                authorization = self.authorize(plan)
                gate = self.make_gate(application_resolver=resolver)
                if expected is None:
                    approval = gate.check(
                        authorization=authorization,
                        session_id="session-001",
                        operation_id="operation-001",
                        current_plan=plan,
                        checked_at=CHECKED_AT,
                    )
                    self.assertEqual(approval.action_ids, ("action-app",))
                else:
                    with self.assertRaises(PreflightError) as caught:
                        gate.check(
                            authorization=authorization,
                            session_id="session-001",
                            operation_id="operation-001",
                            current_plan=plan,
                            checked_at=CHECKED_AT,
                        )
                    self.assertEqual(caught.exception.code, expected)
                    self.assert_invalidated(authorization, plan)

    def test_concurrent_duplicate_preflight_has_exactly_one_success(self) -> None:
        plan = self.make_plan()
        authorization = self.authorize(plan)

        def attempt(_):
            try:
                self.check(authorization, plan)
                return "approved"
            except PreflightError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(results.count("approved"), 1)
        self.assertEqual(results.count("authorization_replayed"), 7)


if __name__ == "__main__":
    unittest.main()
