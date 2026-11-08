from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace

from app.plans import (
    ACTION_PLAN_FORMAT_VERSION,
    ActionPlanError,
    build_plan_action,
    build_reviewable_plan,
    parse_execution_plan_markdown,
    parse_plan_json,
    render_execution_plan_markdown,
    serialize_plan_json,
)
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.tools import build_mvp_tool_registry


HASH_A = "sha256:" + "a" * 64
CREATED_AT = "2026-09-20T08:00:00Z"


def snapshot(path: str, *, purpose: str, exists: bool = True) -> PathSnapshot:
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


class ActionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_mvp_tool_registry()
        self.engine = PolicyEngine(self.registry, path_revalidator=lambda value: value)

    def make_action(
        self,
        *,
        action_id: str = "action-1",
        tool_name: str = "create_text_file",
        path: str = r"G:\study\project\note.txt",
        order: int = 1,
    ):
        manifest = self.registry.require(tool_name)
        if tool_name == "create_text_file":
            path_snapshot = snapshot(path, purpose="create_target", exists=False)
            arguments = {
                "path": path,
                "content": "hello",
                "encoding": "utf-8",
                "create_parent": False,
            }
            snapshots = (path_snapshot,)
            preview = {
                "path": path,
                "content_hash": HASH_A,
                "size_bytes": 5,
                "conflict_policy": "deny",
            }
        elif tool_name == "move_to_recycle_bin":
            path_snapshot = snapshot(path, purpose="recycle_target")
            arguments = {"paths": [path], "recursive": False}
            snapshots = (path_snapshot,)
            preview = {
                "paths": [path],
                "item_count": 1,
                "permanent_delete": False,
            }
        else:
            arguments = {"application_id": "approved.editor"}
            snapshots = ()
            preview = {
                "application_id": "approved.editor",
                "application_name": "Approved Editor",
            }
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=arguments,
            path_snapshots=snapshots,
        )
        decision = self.engine.evaluate(policy_input)
        return build_plan_action(
            action_id=action_id,
            policy_input=policy_input,
            policy_decision=decision,
            preview=preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=order,
        )

    def make_plan(self, *, actions=None, plan_version: int = 1):
        return build_reviewable_plan(
            plan_id="plan-001",
            plan_version=plan_version,
            task_id="task-001",
            created_at=CREATED_AT,
            goal="创建一份说明文件",
            scope=(r"G:\study\project",),
            exclusions=("不覆盖已有文件",),
            file_rules=("只创建 UTF-8 文本",),
            recursive=False,
            conflict_policy="deny",
            actions=tuple(actions or (self.make_action(),)),
        )

    def test_builds_immutable_non_executable_plan_with_stable_hash(self) -> None:
        first = self.make_plan()
        second = self.make_plan()

        self.assertEqual(first.format_version, ACTION_PLAN_FORMAT_VERSION)
        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertRegex(first.plan_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.actions[0].order, 1)
        self.assertFalse(hasattr(first, "execute"))
        self.assertFalse(hasattr(first.actions[0], "execute"))
        with self.assertRaises(FrozenInstanceError):
            first.plan_version = 2  # type: ignore[misc]
        with self.assertRaises(TypeError):
            first.actions[0].normalized_arguments["path"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            first.actions[0].preview["size_bytes"] = 999  # type: ignore[index]

    def test_plan_json_is_canonical_and_round_trips_strictly(self) -> None:
        plan = self.make_plan()
        encoded = serialize_plan_json(plan)
        self.assertTrue(encoded.endswith("\n"))
        self.assertEqual(encoded, serialize_plan_json(plan))
        self.assertEqual(
            encoded,
            json.dumps(json.loads(encoded), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        )

        restored = parse_plan_json(encoded, registry=self.registry)
        self.assertEqual(restored, plan)
        self.assertEqual(serialize_plan_json(restored), encoded)

    def test_hash_covers_version_order_arguments_paths_risk_policy_and_preview(self) -> None:
        plan = self.make_plan()
        payload = json.loads(serialize_plan_json(plan))
        mutations = (
            lambda value: value.__setitem__("plan_version", 2),
            lambda value: value["actions"][0].__setitem__("order", 2),
            lambda value: value["actions"][0]["normalized_arguments"].__setitem__("content", "changed"),
            lambda value: value["actions"][0]["path_snapshots"][0].__setitem__("resolved_path", r"G:\study\other.txt"),
            lambda value: value["actions"][0]["policy_decision"].__setitem__("risk_level", "L1"),
            lambda value: value["actions"][0]["policy_decision"].__setitem__("policy_version", "9.9.9"),
            lambda value: value["actions"][0]["preview"].__setitem__("content_hash", "sha256:" + "b" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = json.loads(json.dumps(payload))
                mutate(changed)
                with self.assertRaises(ActionPlanError) as caught:
                    parse_plan_json(json.dumps(changed, ensure_ascii=False), registry=self.registry)
                self.assertEqual(caught.exception.code, "plan_hash_mismatch")

    def test_parser_rejects_unknown_fields_invalid_schema_and_noncanonical_json_values(self) -> None:
        payload = json.loads(serialize_plan_json(self.make_plan()))
        for field, value in (("executor", "powershell"), ("authorization", True)):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(payload))
                changed[field] = value
                with self.assertRaises(ActionPlanError) as caught:
                    parse_plan_json(json.dumps(changed), registry=self.registry)
                self.assertEqual(caught.exception.code, "unknown_field")

        changed = json.loads(json.dumps(payload))
        changed["schema_version"] = 99
        with self.assertRaises(ActionPlanError) as caught:
            parse_plan_json(json.dumps(changed), registry=self.registry)
        self.assertEqual(caught.exception.code, "unsupported_schema_version")

        with self.assertRaises(ActionPlanError) as caught:
            parse_plan_json('{"schema_version":1,"schema_version":1}', registry=self.registry)
        self.assertEqual(caught.exception.code, "duplicate_json_key")

        with self.assertRaises(ActionPlanError) as caught:
            parse_plan_json('{"schema_version":NaN}', registry=self.registry)
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_rehashed_payload_still_rejects_action_reorder_and_duplicate_ids(self) -> None:
        actions = (
            self.make_action(action_id="action-1"),
            self.make_action(action_id="action-2", tool_name="start_whitelisted_application", order=2),
        )
        plan = self.make_plan(actions=actions)
        reordered = list(reversed(plan.actions))
        with self.assertRaises(ActionPlanError) as caught:
            build_reviewable_plan(
                plan_id=plan.plan_id,
                plan_version=plan.plan_version,
                task_id=plan.task_id,
                created_at=plan.created_at,
                goal=plan.goal,
                scope=plan.scope,
                exclusions=plan.exclusions,
                file_rules=plan.file_rules,
                recursive=plan.recursive,
                conflict_policy=plan.conflict_policy,
                actions=tuple(reordered),
            )
        self.assertEqual(caught.exception.code, "action_order_invalid")

        duplicate = replace(actions[1], action_id="action-1")
        with self.assertRaises(ActionPlanError) as caught:
            self.make_plan(actions=(actions[0], duplicate))
        self.assertEqual(caught.exception.code, "duplicate_action_id")

    def test_build_rejects_denied_policy_and_policy_input_drift(self) -> None:
        manifest = self.registry.require("create_text_file")
        path = r"G:\study\project\note.txt"
        snap = snapshot(path, purpose="create_target", exists=False)
        valid_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments={"path": path, "content": "x", "encoding": "utf-8", "create_parent": False},
            path_snapshots=(snap,),
        )
        denied = replace(self.engine.evaluate(valid_input), decision="deny", reason_code="forced", reason="forced")
        with self.assertRaises(ActionPlanError) as caught:
            build_plan_action(
                action_id="action-1",
                policy_input=valid_input,
                policy_decision=denied,
                preview={},
                verification_method=manifest.verification_method,
                rollback_strategy=manifest.rollback_strategy,
            )
        self.assertEqual(caught.exception.code, "policy_denied")

        drifted = replace(self.engine.evaluate(valid_input), normalized_paths=(r"G:\study\other.txt",))
        with self.assertRaises(ActionPlanError) as caught:
            build_plan_action(
                action_id="action-1",
                policy_input=valid_input,
                policy_decision=drifted,
                preview={},
                verification_method=manifest.verification_method,
                rollback_strategy=manifest.rollback_strategy,
            )
        self.assertEqual(caught.exception.code, "policy_fact_mismatch")

    def test_rehashed_payload_rejects_manifest_policy_and_path_fact_drift(self) -> None:
        payload = json.loads(serialize_plan_json(self.make_plan()))

        def rehash(value: dict[str, object]) -> str:
            material = {key: item for key, item in value.items() if key != "plan_hash"}
            encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return "sha256:" + hashlib.sha256(encoded).hexdigest()

        cases = (
            ("manifest", lambda value: value["actions"][0].__setitem__("manifest_hash", "sha256:" + "0" * 64), "manifest_identity_mismatch"),
            ("policy", lambda value: value["actions"][0]["policy_decision"].__setitem__("policy_version", "9.9.9"), "policy_version_mismatch"),
            ("path", lambda value: value["actions"][0]["policy_decision"].__setitem__("normalized_paths", [r"G:\study\other.txt"]), "policy_fact_mismatch"),
        )
        for name, mutate, code in cases:
            with self.subTest(name=name):
                changed = json.loads(json.dumps(payload))
                mutate(changed)
                changed["plan_hash"] = rehash(changed)
                with self.assertRaises(ActionPlanError) as caught:
                    parse_plan_json(json.dumps(changed, ensure_ascii=False), registry=self.registry)
                self.assertEqual(caught.exception.code, code)

    def test_markdown_round_trip_allows_only_isolated_plain_notes(self) -> None:
        plan = self.make_plan()
        markdown = render_execution_plan_markdown(plan)
        review = parse_execution_plan_markdown(markdown, expected_plan=plan)
        self.assertEqual(review.plan_id, plan.plan_id)
        self.assertEqual(review.plan_version, plan.plan_version)
        self.assertEqual(review.plan_hash, plan.plan_hash)
        self.assertEqual(review.user_notes, "")

        edited = markdown.replace(
            "<!-- USER_NOTES_START -->\n\n<!-- USER_NOTES_END -->",
            "<!-- USER_NOTES_START -->\n请执行前再次检查目标文件。\n<!-- USER_NOTES_END -->",
        )
        review = parse_execution_plan_markdown(edited, expected_plan=plan)
        self.assertEqual(review.user_notes, "请执行前再次检查目标文件。")
        self.assertEqual(render_execution_plan_markdown(plan, user_notes=review.user_notes), edited)

    def test_markdown_cannot_change_security_fields_or_action_table(self) -> None:
        plan = self.make_plan()
        markdown = render_execution_plan_markdown(plan)
        mutations = (
            markdown.replace(plan.plan_hash, "sha256:" + "0" * 64, 1),
            markdown.replace("L2", "L1", 1),
            markdown.replace("create_text_file", "run_command", 1),
            markdown.replace(r"G:\study\project\note.txt", r"C:\Windows\note.txt", 1),
            markdown.replace("action-1", "action-9", 1),
        )
        for changed in mutations:
            with self.subTest(), self.assertRaises(ActionPlanError) as caught:
                parse_execution_plan_markdown(changed, expected_plan=plan)
            self.assertEqual(caught.exception.code, "markdown_security_mismatch")

    def test_markdown_rejects_script_fences_duplicate_sections_and_oversized_notes(self) -> None:
        plan = self.make_plan()
        markdown = render_execution_plan_markdown(plan)
        script = markdown.replace(
            "<!-- USER_NOTES_START -->\n\n<!-- USER_NOTES_END -->",
            "<!-- USER_NOTES_START -->\n```powershell\nRemove-Item x\n```\n<!-- USER_NOTES_END -->",
        )
        with self.assertRaises(ActionPlanError) as caught:
            parse_execution_plan_markdown(script, expected_plan=plan)
        self.assertEqual(caught.exception.code, "markdown_script_denied")

        duplicate = markdown + "\n<!-- USER_NOTES_START -->\nextra\n<!-- USER_NOTES_END -->\n"
        with self.assertRaises(ActionPlanError) as caught:
            parse_execution_plan_markdown(duplicate, expected_plan=plan)
        self.assertEqual(caught.exception.code, "markdown_structure_invalid")

        oversized = markdown.replace(
            "<!-- USER_NOTES_START -->\n\n<!-- USER_NOTES_END -->",
            "<!-- USER_NOTES_START -->\n" + "x" * 4097 + "\n<!-- USER_NOTES_END -->",
        )
        with self.assertRaises(ActionPlanError) as caught:
            parse_execution_plan_markdown(oversized, expected_plan=plan)
        self.assertEqual(caught.exception.code, "markdown_notes_too_large")

    def test_plan_version_and_action_sequence_change_plan_hash(self) -> None:
        first = self.make_plan(plan_version=1)
        second = self.make_plan(plan_version=2)
        extra = self.make_plan(
            actions=(
                self.make_action(action_id="action-1"),
                self.make_action(action_id="action-2", tool_name="start_whitelisted_application", order=2),
            )
        )
        self.assertNotEqual(first.plan_hash, second.plan_hash)
        self.assertNotEqual(first.plan_hash, extra.plan_hash)


if __name__ == "__main__":
    unittest.main()
