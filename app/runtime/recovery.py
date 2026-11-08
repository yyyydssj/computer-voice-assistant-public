"""P1 启动恢复哨兵：只发现遗留计划线索，绝不恢复执行。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecoveryScanSummary:
    """启动时只读扫描的安全摘要，不包含个人路径或计划内容。"""

    possible_unfinished_count: int
    auto_resume_allowed: bool = False


def detect_unfinished_tasks(plans_root: Path | str) -> RecoveryScanSummary:
    """发现缺少结果文件的冻结计划线索，但不读取或执行计划内容。"""

    root = Path(plans_root)
    if not root.is_dir() or root.is_symlink():
        return RecoveryScanSummary(possible_unfinished_count=0)

    count = 0
    for task_directory in _owned_directories(root):
        versions_directory = task_directory / "versions"
        if not versions_directory.is_dir() or versions_directory.is_symlink():
            continue
        for version_directory in _owned_directories(versions_directory):
            plan_path = version_directory / "plan.json"
            result_path = version_directory / "result.json"
            if (
                plan_path.is_file()
                and not plan_path.is_symlink()
                and not result_path.is_file()
            ):
                count += 1

    return RecoveryScanSummary(possible_unfinished_count=count)


def _owned_directories(parent: Path) -> tuple[Path, ...]:
    """只返回当前树中的真实目录，不跟随目录符号链接。"""

    try:
        children = tuple(parent.iterdir())
    except OSError:
        return ()
    return tuple(
        child
        for child in children
        if not child.is_symlink() and child.is_dir()
    )
