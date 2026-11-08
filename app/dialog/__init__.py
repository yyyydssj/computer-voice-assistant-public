"""P9 批次 1：对话状态机与连续对话输入边界（app/dialog）。

边界：本包只提供本地对话状态与输入许可事实；VAD、唤醒词、声纹都不决定授权，本包
也不接触任务/计划/授权/执行/结果事实，不读写文件、不联网、不创建线程。
"""

from app.dialog.coordinator import (
    CLAIM_LAYERS,
    DIALOG_INPUT_REFUSAL_REASONS,
    DialogCoordinator,
    InputPermit,
    TurnClaim,
    TurnClaimDecision,
)
from app.dialog.state import (
    DIALOG_SCHEMA_VERSION,
    DIALOG_TRANSITION_REASONS,
    DialogDiagnostic,
    DialogError,
    DialogInputMode,
    DialogState,
    DialogStateMachine,
    DialogTransition,
    require_transition_reason,
)
from app.dialog.window import (
    DialogWindow,
    DialogWindowCloseReason,
    DialogWindowConfig,
    DialogWindowState,
    DialogWindowStatus,
)

__all__ = [
    "CLAIM_LAYERS",
    "DIALOG_INPUT_REFUSAL_REASONS",
    "DIALOG_SCHEMA_VERSION",
    "DIALOG_TRANSITION_REASONS",
    "DialogCoordinator",
    "DialogDiagnostic",
    "DialogError",
    "DialogInputMode",
    "DialogState",
    "DialogStateMachine",
    "DialogTransition",
    "DialogWindow",
    "DialogWindowCloseReason",
    "DialogWindowConfig",
    "DialogWindowState",
    "DialogWindowStatus",
    "InputPermit",
    "TurnClaim",
    "TurnClaimDecision",
    "require_transition_reason",
]
