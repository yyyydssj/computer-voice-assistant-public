from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent import AgentTurnOutcome
from app.conversation import ConversationManager
from app.plans import (
    build_plan_action,
    build_result_action,
    build_result_document,
    build_reviewable_plan,
    parse_plan_json,
    parse_result_json,
    serialize_plan_json,
    serialize_result_json,
)
from app.runtime import RuntimeLifecycle
from app.safety import PathSnapshot, PolicyEngine, PolicyInput
from app.tools import (
    build_mvp_tool_registry,
    build_write_action_preview,
    list_directory,
)
from app.ui import TextShell


HASH_A = "sha256:" + "a" * 64
CREATED_AT = "2026-09-21T08:00:00Z"
COMPLETED_AT = "2026-09-21T08:00:01Z"


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


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.close_calls = 0

    def submit_chat(self, *, request_id: str, session_id: str, text: str) -> AgentTurnOutcome:
        self.calls.append((request_id, session_id, text))
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=f"已收到：{text}",
            finish_reason="completed",
        )

    def close(self) -> None:
        self.close_calls += 1


class P1ToP4IntegrationTests(unittest.TestCase):
    def test_runtime_text_shell_conversation_and_plan_result_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[Any] = []
            lifecycle = RuntimeLifecycle(
                lock_path=root / "instance.lock",
                instance_id="00000000-0000-4000-8000-000000000001",
                event_sink=events.append,
            )
            lifecycle.start()
            lifecycle.emit("runtime_ready", {"mode": "text", "provider_ready": False})

            adapter = _FakeAdapter()
            conversations = ConversationManager(adapter)
            output = io.StringIO()
            shell = TextShell(
                input_stream=io.StringIO("创建说明\n/exit\n"),
                output_stream=output,
                status_provider=lambda: {
                    "mode": "text",
                    "voice_available": False,
                    "task_state": "idle",
                },
            )
            shell.run(
                on_text=lambda text: conversations.send_work(
                    task_id="task-001", request_id="request-001", text=text
                ).text,
                on_exit=conversations.close,
            )

            self.assertIn("文字模式", output.getvalue())
            self.assertIn("已收到：创建说明", output.getvalue())
            self.assertEqual(adapter.calls[0][0], "request-001")
            self.assertEqual(len(conversations.work_sessions), 1)
            self.assertEqual(conversations.work_sessions[0].history[0].assistant_text, "已收到：创建说明")
            self.assertEqual([event.event_type for event in events], ["runtime_started", "runtime_ready"])
            lifecycle.close()

    def test_read_only_policy_plan_and_result_are_bound_across_p4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            listed = list_directory(str(root), path_validator=lambda value, _purpose: Path(value))
            self.assertEqual([item.name for item in listed.items], ["note.txt"])

        registry = build_mvp_tool_registry()
        engine = PolicyEngine(registry, path_revalidator=lambda value: value)
        target = r"G:\study\project\note.txt"
        arguments = {
            "path": target,
            "content": "hello",
            "encoding": "utf-8",
            "create_parent": False,
        }
        preview = build_write_action_preview(
            "create_text_file",
            arguments,
            registry=registry,
            path_validator=lambda value, _purpose: Path(value),
        )
        manifest = registry.require("create_text_file")
        path_fact = snapshot(target, purpose="create_target", exists=False)
        policy_input = PolicyInput(
            tool_name=manifest.name,
            tool_version=manifest.version,
            manifest_hash=manifest.content_hash,
            risk_level=manifest.risk_level,
            requires_confirmation=manifest.requires_confirmation,
            normalized_arguments=preview.normalized_arguments,
            path_snapshots=(path_fact,),
        )
        decision = engine.evaluate(policy_input)
        self.assertEqual(decision.decision, "require_confirmation")

        action = build_plan_action(
            action_id="action-001",
            policy_input=policy_input,
            policy_decision=decision,
            preview=preview.preview,
            verification_method=manifest.verification_method,
            rollback_strategy=manifest.rollback_strategy,
            order=1,
        )
        plan = build_reviewable_plan(
            plan_id="plan-001",
            plan_version=1,
            task_id="task-001",
            created_at=CREATED_AT,
            goal="创建一份说明文件",
            scope=(r"G:\study\project",),
            exclusions=("不覆盖已有文件",),
            file_rules=("只创建 UTF-8 文本",),
            recursive=False,
            conflict_policy="deny",
            actions=(action,),
        )
        plan_round_trip = parse_plan_json(serialize_plan_json(plan), registry=registry)
        self.assertEqual(plan_round_trip.plan_hash, plan.plan_hash)
        self.assertEqual(plan_round_trip.actions[0].action_id, "action-001")

        result_action = build_result_action(
            plan_action=plan.actions[0],
            outcome="succeeded",
            attempt_id="attempt-001",
            started_at=CREATED_AT,
            finished_at=COMPLETED_AT,
            verification_status="completed",
            verified_at=COMPLETED_AT,
            verifier="integration-test-verifier",
            verification_facts={"target_exists": True},
            output={"size_bytes": 5},
        )
        result = build_result_document(
            result_id="result-001",
            execution_id="execution-001",
            plan=plan,
            created_at=CREATED_AT,
            completed_at=COMPLETED_AT,
            actions=(result_action,),
        )
        result_round_trip = parse_result_json(
            serialize_result_json(result), expected_plan=plan
        )
        self.assertEqual(result_round_trip.overall_status, "completed")
        self.assertEqual(result_round_trip.actions[0].action_id, "action-001")


if __name__ == "__main__":
    unittest.main()
