"""P5 批次 7：只读恢复扫描与不可自动续跑的恢复报告。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.contracts.base import parse_utc_timestamp, require_choice, require_identifier


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    action_id: str
    status: str
    evidence: Mapping[str, Any]
    reason: str | None = None

    def __post_init__(self) -> None:
        require_identifier("RecoveryAction.action_id", self.action_id)
        require_choice("RecoveryAction.status", self.status, {"completed", "not_completed", "state_uncertain"})
        if not isinstance(self.evidence, Mapping):
            raise TypeError("evidence 必须是对象")
        object.__setattr__(self, "evidence", dict(self.evidence))
        if self.status == "state_uncertain" and not self.reason:
            raise ValueError("state_uncertain 必须说明原因")


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    execution_id: str
    plan_id: str
    plan_version: int
    interruption_reason: str
    last_known_at: str | None
    actions: tuple[RecoveryAction, ...]
    auto_resume_allowed: bool = False

    def __post_init__(self) -> None:
        require_identifier("execution_id", self.execution_id)
        require_identifier("plan_id", self.plan_id)
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version <= 0:
            raise ValueError("plan_version 必须是正整数")
        if not self.interruption_reason or "\n" in self.interruption_reason:
            raise ValueError("interruption_reason 无效")
        if self.last_known_at is not None:
            parse_utc_timestamp("last_known_at", self.last_known_at)
        object.__setattr__(self, "actions", tuple(self.actions))
        if self.auto_resume_allowed:
            raise ValueError("恢复报告不得允许自动续跑")

    def render_text(self) -> str:
        lines = [
            f"恢复报告：execution={self.execution_id}，plan={self.plan_id} v{self.plan_version}",
            f"中断原因：{self.interruption_reason}",
            "旧计划和旧授权不会自动续跑。",
        ]
        for action in self.actions:
            lines.append(f"动作 {action.action_id}：{action.status}")
        lines.append("已完成动作不会自动回滚；未完成动作不会自动补做。")
        lines.append("如需继续，必须重新规划、预检并授权。")
        return "\n".join(lines)


Observe = Callable[[Any], tuple[str, Mapping[str, Any]]]


class RecoveryScanner:
    """只读扫描当前真实状态；审计提示只作线索，永不自动执行。"""

    def scan(self, *, execution_id: str, plan_id: str, plan_version: int,
             interruption_reason: str, last_known_at: str | None,
             actions: Sequence[Any], audit_hints: Mapping[str, Any], observe: Observe) -> RecoveryReport:
        del audit_hints
        results: list[RecoveryAction] = []
        for raw in actions:
            action_id = self._action_id(raw)
            try:
                status, evidence = observe(raw)
                require_choice("observed status", status, {"completed", "not_completed", "state_uncertain"})
                if not isinstance(evidence, Mapping):
                    raise ValueError("证据不是对象")
                reason = None if status != "state_uncertain" else "当前状态证据不足或冲突"
                results.append(RecoveryAction(action_id, status, evidence, reason))
            except Exception:
                results.append(RecoveryAction(action_id, "state_uncertain", {"observed": False}, "无法可靠观察当前状态"))
        return RecoveryReport(execution_id, plan_id, plan_version, interruption_reason, last_known_at, tuple(results))

    def scan_legacy_plan_root(self, plans_root: Path | str) -> tuple[RecoveryReport, ...]:
        root = Path(plans_root)
        if not root.is_dir() or root.is_symlink():
            return ()
        reports: list[RecoveryReport] = []
        for task in self._owned_dirs(root):
            versions = task / "versions"
            for version in self._owned_dirs(versions) if versions.is_dir() and not versions.is_symlink() else ():
                if (version / "plan.json").is_file() and not (version / "plan.json").is_symlink() and not (version / "result.json").is_file():
                    reports.append(RecoveryReport(
                        execution_id=f"legacy-{task.name}-{version.name}", plan_id=f"legacy-{task.name}", plan_version=1,
                        interruption_reason="legacy_execution_without_result", last_known_at=None, actions=(),
                    ))
        return tuple(reports)

    @staticmethod
    def _action_id(raw: Any) -> str:
        if isinstance(raw, Mapping):
            value = raw.get("action_id")
        elif isinstance(raw, (tuple, list)) and raw:
            value = raw[0]
        else:
            value = None
        if not isinstance(value, str) or not value:
            raise ValueError("action_id 无效")
        return value

    @staticmethod
    def _owned_dirs(parent: Path) -> tuple[Path, ...]:
        try:
            return tuple(child for child in parent.iterdir() if not child.is_symlink() and child.is_dir())
        except OSError:
            return ()