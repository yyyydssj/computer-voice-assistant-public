from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError

from app.plans import (
    ResultDocumentError,
    build_plan_action,
    build_result_action,
    build_result_document,
    build_reviewable_plan,
    parse_result_json,
    serialize_result_json,
)
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.tools import build_mvp_tool_registry


HASH_A = "sha256:" + "a" * 64
CREATED_AT = "2026-09-20T09:00:00Z"
COMPLETED_AT = "2026-09-20T09:00:10Z"


def snapshot(path: str, *, purpose: str, exists: bool = False) -> PathSnapshot:
    return PathSnapshot(
        requested_path=path,
        purpose=purpose,
        normalized_path=path,
        resolved_path=path,
        exists=exists,
        target_identity=(1, 5) if exists else None,
        anchor_path=path if exists else r"G:\study\project",
        anchor_identity=(1, 5) if exists else (1, 4),
        component_identities=(
            ("G:\\", (1, 1)),
            (r"G:\study", (1, 2)),
            (r"G:\study\project", (1, 4)),
        ) + (((path, (1, 5)),) if exists else ()),
    )


class ResultDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.engine = PolicyEngine(self.registry, path_revalidator=lambda value: value)
        self.plan = self.make_plan()

    def make_plan_action(self, *, order: int = 1, action_id: str = "action-1", tool_name: str = "create_text_file"):
        manifest = self.registry.require(tool_name)
        if tool_name == "create_text_file":
            path = r"G:\study\project\note.txt"
            arguments = {"path": path, "content": "hello", "encoding": "utf-8", "create_parent": False}
            snapshots = (snapshot(path, purpose="create_target"),)
            preview = {"path": path, "content_hash": HASH_A, "size_bytes": 5, "conflict_policy": "deny"}
        else:
            arguments = {"application_id": "approved.editor"}
            snapshots = ()
            preview = {"application_id": "approved.editor", "application_name": "Approved Editor"}
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=snapshots,
        )
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=self.engine.evaluate(policy_input),
            preview=preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_plan(self, *, actions=None, plan_id: str = "plan-001"):
        return build_reviewable_plan(
            plan_id=plan_id,
            plan_version=1,
            task_id="task-001",
            created_at="2026-09-20T08:00:00Z",
            goal="创建一份说明文件",
            scope=(r"G:\study\project",),
            exclusions=("不覆盖已有文件",),
            file_rules=("只创建 UTF-8 文本",),
            recursive=False,
            conflict_policy="deny",
            actions=tuple(actions or (self.make_plan_action(),)),
        )

    def make_result_action(self, *, plan_action=None, outcome: str = "succeeded", output=None, error_code=None, error_message=None, skip_reason=None):
        action = plan_action or self.plan.actions[0]
        verification_status = {
            "succeeded": "completed",
            "failed": "not_completed",
            "skipped": "not_completed",
            "state_uncertain": "state_uncertain",
        }[outcome]
        attempted = outcome != "skipped"
        return build_result_action(
            plan_action=action,
            outcome=outcome,
            attempt_id=f"attempt-{action.order}" if attempted else None,
            started_at=CREATED_AT if attempted else None,
            finished_at=COMPLETED_AT if outcome in {"succeeded", "failed"} else None,
            verification_status=verification_status,
            verified_at=COMPLETED_AT,
            verifier="local-verifier",
            verification_facts={"observed": outcome, "path": r"G:\study\project\note.txt"},
            output=output if output is not None else ({"bytes_written": 5, "content_hash": HASH_A} if outcome == "succeeded" else {}),
            error_code=error_code,
            error_message=error_message,
            skip_reason=skip_reason,
        )

    def make_document(self, *, plan=None, actions=None, result_id: str = "result-001"):
        selected = plan or self.plan
        return build_result_document(
            plan=selected,
            result_id=result_id,
            execution_id="execution-001",
            created_at=CREATED_AT,
            completed_at=COMPLETED_AT,
            actions=tuple(actions or (self.make_result_action(plan_action=selected.actions[0]),)),
        )

    def test_builds_immutable_non_executable_document_with_stable_hash(self) -> None:
        first = self.make_document()
        second = self.make_document()
        self.assertEqual(first.result_hash, second.result_hash)
        self.assertRegex(first.result_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.overall_status, "completed")
        self.assertFalse(hasattr(first, "execute"))
        self.assertFalse(hasattr(first.actions[0], "authorize"))
        with self.assertRaises(FrozenInstanceError):
            first.execution_id = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            first.actions[0].output["bytes_written"] = 6  # type: ignore[index]

    def test_canonical_json_round_trip_is_stable_and_bound_to_plan(self) -> None:
        document = self.make_document()
        encoded = serialize_result_json(document)
        parsed = parse_result_json(encoded, expected_plan=self.plan)
        self.assertEqual(parsed, document)
        self.assertEqual(serialize_result_json(parsed), encoded)
        self.assertTrue(encoded.endswith("\n"))
        self.assertNotIn(": ", encoded)
        self.assertNotIn(", ", encoded)

    def test_hash_covers_identity_result_verification_error_and_output_facts(self) -> None:
        base = self.make_document()
        variants = (
            self.make_document(result_id="result-002"),
            build_result_document(
                plan=self.plan,
                result_id="result-001",
                execution_id="execution-002",
                created_at=CREATED_AT,
                completed_at=COMPLETED_AT,
                actions=(self.make_result_action(),),
            ),
            self.make_document(actions=(self.make_result_action(output={"bytes_written": 6}),)),
            self.make_document(actions=(self.make_result_action(outcome="failed", error_code="write_failed", error_message="写入失败"),)),
        )
        for changed in variants:
            self.assertNotEqual(base.result_hash, changed.result_hash)

    def test_rejects_unknown_missing_duplicate_nonstandard_and_version_fields(self) -> None:
        payload = json.loads(serialize_result_json(self.make_document()))
        cases = []
        unknown = dict(payload); unknown["authorization"] = "allow"; cases.append((unknown, "unknown_field"))
        missing = dict(payload); missing.pop("execution_id"); cases.append((missing, "missing_field"))
        schema = dict(payload); schema["schema_version"] = 9; cases.append((schema, "unsupported_schema_version"))
        fmt = dict(payload); fmt["format_version"] = "9.0.0"; cases.append((fmt, "unsupported_format_version"))
        for changed, code in cases:
            with self.subTest(code=code), self.assertRaises(ResultDocumentError) as caught:
                parse_result_json(json.dumps(changed, ensure_ascii=False), expected_plan=self.plan)
            self.assertEqual(caught.exception.code, code)

        duplicate = serialize_result_json(self.make_document()).replace('"result_id":"result-001"', '"result_id":"result-001","result_id":"result-002"')
        with self.assertRaises(ResultDocumentError) as caught:
            parse_result_json(duplicate, expected_plan=self.plan)
        self.assertEqual(caught.exception.code, "duplicate_json_key")
        nonstandard = serialize_result_json(self.make_document()).replace('"bytes_written":5', '"bytes_written":NaN')
        with self.assertRaises(ResultDocumentError) as caught:
            parse_result_json(nonstandard, expected_plan=self.plan)
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_rejects_hash_tampering_even_when_json_remains_valid(self) -> None:
        payload = json.loads(serialize_result_json(self.make_document()))
        payload["actions"][0]["output"]["bytes_written"] = 999
        with self.assertRaises(ResultDocumentError) as caught:
            parse_result_json(json.dumps(payload, ensure_ascii=False), expected_plan=self.plan)
        self.assertEqual(caught.exception.code, "result_hash_mismatch")

    def test_recomputed_hash_cannot_hide_plan_or_action_identity_drift(self) -> None:
        payload = json.loads(serialize_result_json(self.make_document()))

        def rehash(value):
            unhashed = {key: item for key, item in value.items() if key != "result_hash"}
            canonical = json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        mutations = (
            (lambda value: value.__setitem__("plan_id", "plan-999"), "plan_identity_mismatch"),
            (lambda value: value["actions"][0].__setitem__("action_id", "action-999"), "action_identity_mismatch"),
            (lambda value: value["actions"][0].__setitem__("tool_name", "start_whitelisted_application"), "action_identity_mismatch"),
            (lambda value: value["actions"][0].__setitem__("manifest_hash", "sha256:" + "0" * 64), "action_identity_mismatch"),
        )
        for mutate, code in mutations:
            with self.subTest(code=code):
                changed = json.loads(json.dumps(payload))
                mutate(changed)
                changed["result_hash"] = rehash(changed)
                with self.assertRaises(ResultDocumentError) as caught:
                    parse_result_json(json.dumps(changed, ensure_ascii=False), expected_plan=self.plan)
                self.assertEqual(caught.exception.code, code)

    def test_recomputed_hash_cannot_hide_action_reordering_or_duplicate_ids(self) -> None:
        plan = self.make_plan(actions=(
            self.make_plan_action(order=1, action_id="action-1"),
            self.make_plan_action(order=2, action_id="action-2", tool_name="start_whitelisted_application"),
        ))
        actions = (self.make_result_action(plan_action=plan.actions[0]), self.make_result_action(plan_action=plan.actions[1]))
        payload = json.loads(serialize_result_json(self.make_document(plan=plan, actions=actions)))

        def rehash(value):
            unhashed = {key: item for key, item in value.items() if key != "result_hash"}
            canonical = json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        reordered = json.loads(json.dumps(payload)); reordered["actions"].reverse(); reordered["result_hash"] = rehash(reordered)
        with self.assertRaises(ResultDocumentError) as caught:
            parse_result_json(json.dumps(reordered, ensure_ascii=False), expected_plan=plan)
        self.assertEqual(caught.exception.code, "action_sequence_mismatch")

        duplicate = json.loads(json.dumps(payload)); duplicate["actions"][1]["action_id"] = "action-1"; duplicate["result_hash"] = rehash(duplicate)
        with self.assertRaises(ResultDocumentError) as caught:
            parse_result_json(json.dumps(duplicate, ensure_ascii=False), expected_plan=plan)
        self.assertEqual(caught.exception.code, "duplicate_action_id")

    def test_outcome_verification_error_and_skip_semantics_are_strict(self) -> None:
        with self.assertRaises(ResultDocumentError) as caught:
            self.make_result_action(outcome="failed")
        self.assertEqual(caught.exception.code, "result_semantics_invalid")

        with self.assertRaises(ResultDocumentError) as caught:
            self.make_result_action(outcome="skipped", skip_reason=None)
        self.assertEqual(caught.exception.code, "result_semantics_invalid")

        with self.assertRaises(ResultDocumentError) as caught:
            build_result_action(
                plan_action=self.plan.actions[0], outcome="succeeded", attempt_id="attempt-1",
                started_at=CREATED_AT, finished_at=COMPLETED_AT,
                verification_status="state_uncertain", verified_at=COMPLETED_AT,
                verifier="local-verifier", verification_facts={}, output={},
            )
        self.assertEqual(caught.exception.code, "result_semantics_invalid")

    def test_overall_status_is_derived_and_tampering_is_rejected(self) -> None:
        plan = self.make_plan(actions=(
            self.make_plan_action(order=1, action_id="action-1"),
            self.make_plan_action(order=2, action_id="action-2", tool_name="start_whitelisted_application"),
        ))
        succeeded = self.make_result_action(plan_action=plan.actions[0])
        failed = self.make_result_action(plan_action=plan.actions[1], outcome="failed", error_code="launch_failed", error_message="启动失败")
        document = self.make_document(plan=plan, actions=(succeeded, failed))
        self.assertEqual(document.overall_status, "partially_completed")
        skipped = self.make_result_action(plan_action=plan.actions[0], outcome="skipped", skip_reason="前置条件不满足")
        failed_only = self.make_document(plan=plan, actions=(skipped, failed))
        self.assertEqual(failed_only.overall_status, "failed")

        payload = json.loads(serialize_result_json(document))
        payload["overall_status"] = "completed"
        unhashed = {key: item for key, item in payload.items() if key != "result_hash"}
        canonical = json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["result_hash"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.assertRaises(ResultDocumentError) as caught:
            parse_result_json(json.dumps(payload, ensure_ascii=False), expected_plan=plan)
        self.assertEqual(caught.exception.code, "overall_status_mismatch")

    def test_state_uncertain_dominates_document_status(self) -> None:
        uncertain = self.make_result_action(outcome="state_uncertain", error_code="verification_inconclusive", error_message="无法确认真实状态")
        document = self.make_document(actions=(uncertain,))
        self.assertEqual(document.overall_status, "state_uncertain")
        self.assertEqual(parse_result_json(serialize_result_json(document), expected_plan=self.plan), document)

    def test_timestamps_cannot_run_backwards(self) -> None:
        with self.assertRaises(ResultDocumentError) as caught:
            build_result_action(
                plan_action=self.plan.actions[0], outcome="succeeded", attempt_id="attempt-1",
                started_at=COMPLETED_AT, finished_at=CREATED_AT,
                verification_status="completed", verified_at=COMPLETED_AT,
                verifier="local-verifier", verification_facts={}, output={},
            )
        self.assertEqual(caught.exception.code, "timestamp_order_invalid")

        with self.assertRaises(ResultDocumentError) as caught:
            build_result_document(
                plan=self.plan, result_id="result-001", execution_id="execution-001",
                created_at=COMPLETED_AT, completed_at=CREATED_AT,
                actions=(self.make_result_action(),),
            )
        self.assertEqual(caught.exception.code, "timestamp_order_invalid")
    def test_non_finite_nested_facts_are_rejected(self) -> None:
        with self.assertRaises(ResultDocumentError) as caught:
            self.make_result_action(output={"value": float("nan")})
        self.assertEqual(caught.exception.code, "invalid_field")


if __name__ == "__main__":
    unittest.main()