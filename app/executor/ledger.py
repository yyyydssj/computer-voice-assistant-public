"""P5 批次 5：原子动作边界与协作取消账本。

本模块只记录执行控制事实，不执行工具、不触碰文件系统、不启动应用、不持久化账本，
也不提供强杀或真实取消入口。不可安全中断区间只延迟协作式取消到安全点。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from threading import Lock

from app.contracts import ActionAttempt, ActionResult, ExecutionLedger
from app.contracts.base import parse_utc_timestamp, require_choice, require_identifier, require_unique_identifiers
from app.safety.preflight import ExecutionPreflightApproval, PreflightActionBoundary

_CREATION_PERMIT = object()


class ExecutionLedgerError(ValueError):
    """账本身份、并发、动作边界或停止控制拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class LedgerClassification(str, Enum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    RETURNED = "returned"
    COMPLETED = "completed"
    STATE_UNCERTAIN = "state_uncertain"


class CancellationDisposition(str, Enum):
    BLOCKED_BEFORE_START = "blocked_before_start"
    COOPERATIVE_REQUESTED = "cooperative_requested"
    DEFERRED_UNSAFE_ATOMIC = "deferred_unsafe_atomic"
    WAIT_FOR_ATOMIC_RETURN = "wait_for_atomic_return"


@dataclass(frozen=True, slots=True)
class StopRequest:
    reason: str
    requested_at: str
    disposition: CancellationDisposition
    action_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        require_choice("StopRequest.reason", self.reason, {"user_stopped", "user_cancelled", "system_aborted"})
        parse_utc_timestamp("StopRequest.requested_at", self.requested_at)
        if self.action_id is not None:
            require_identifier("StopRequest.action_id", self.action_id)
        if self.attempt_id is not None:
            require_identifier("StopRequest.attempt_id", self.attempt_id)


@dataclass(frozen=True, slots=True)
class ExecutionLedgerSnapshot:
    ledger: ExecutionLedger
    action_ids: tuple[str, ...]
    action_boundaries: tuple[PreflightActionBoundary, ...]
    attempts: tuple[ActionAttempt, ...]
    results: tuple[ActionResult, ...]
    stop_request: StopRequest | None


class ExecutionLedgerRegistry:
    __slots__ = ("instance_id", "_authorization_executions", "_execution_stores", "_lock")

    def __init__(self, *, instance_id: str) -> None:
        require_identifier("instance_id", instance_id)
        self.instance_id = instance_id
        self._authorization_executions: dict[str, str] = {}
        self._execution_stores: dict[str, ExecutionLedgerStore] = {}
        self._lock = Lock()

    def create(self, *, execution_id: str, approval: ExecutionPreflightApproval,
               created_at: str) -> "ExecutionLedgerStore":
        require_identifier("execution_id", execution_id)
        if not isinstance(approval, ExecutionPreflightApproval):
            raise TypeError("approval 必须是 ExecutionPreflightApproval")
        created = parse_utc_timestamp("created_at", created_at)
        if approval.instance_id != self.instance_id:
            raise ExecutionLedgerError("approval_wrong_instance", "preflight 通过事实不属于当前进程实例")
        if created < parse_utc_timestamp("checked_at", approval.checked_at):
            raise ExecutionLedgerError("ledger_before_preflight", "账本创建时间不得早于 preflight 通过时间")
        authorization_id = approval.authorization.authorization_id
        with self._lock:
            if authorization_id in self._authorization_executions:
                raise ExecutionLedgerError("authorization_replayed", "同一 preflight 授权不得创建第二个 execution")
            if execution_id in self._execution_stores:
                raise ExecutionLedgerError("execution_id_collision", "execution_id 已绑定其他 execution")
            store = ExecutionLedgerStore(
                ledger=ExecutionLedger(
                    execution_id=execution_id,
                    plan_id=approval.plan_id,
                    plan_version=approval.plan_version,
                    plan_hash=approval.plan_hash,
                    authorization_snapshot_hash=approval.authorization.stable_hash(),
                    status="CREATED",
                    created_at=created_at,
                ),
                action_ids=approval.action_ids,
                action_boundaries=approval.action_boundaries,
                creation_permit=_CREATION_PERMIT,
            )
            self._authorization_executions[authorization_id] = execution_id
            self._execution_stores[execution_id] = store
            return store


class ExecutionLedgerStore:
    __slots__ = (
        "_ledger", "_action_ids", "_action_boundaries", "_attempts", "_results", "_lock",
        "_fencing_counter", "_current_fencing_token", "_stop_request", "_unsafe_attempts",
    )

    def __init__(self, *, ledger: ExecutionLedger, action_ids: tuple[str, ...],
                 action_boundaries: tuple[PreflightActionBoundary, ...],
                 creation_permit: object) -> None:
        if creation_permit is not _CREATION_PERMIT:
            raise TypeError("ExecutionLedgerStore 必须由 ExecutionLedgerRegistry 创建")
        if not isinstance(ledger, ExecutionLedger):
            raise TypeError("ledger 必须是 ExecutionLedger")
        require_unique_identifiers("action_ids", action_ids)
        if tuple(item.action_id for item in action_boundaries) != action_ids:
            raise ValueError("action_boundaries 必须与 action_ids 保持相同顺序")
        self._ledger = ledger
        self._action_ids = action_ids
        self._action_boundaries = action_boundaries
        self._attempts: dict[str, ActionAttempt] = {}
        self._results: dict[str, ActionResult] = {}
        self._lock = Lock()
        self._fencing_counter = 0
        self._current_fencing_token: str | None = None
        self._stop_request: StopRequest | None = None
        self._unsafe_attempts: set[str] = set()

    def acquire_executor(self, executor_instance_id: str) -> "LedgerExecutor":
        require_identifier("executor_instance_id", executor_instance_id)
        with self._lock:
            if self._stop_request is not None:
                raise ExecutionLedgerError("execution_stopped", "已请求停止的 execution 不得签发新的 executor 句柄")
            if self._ledger.status in {"COMPLETED", "FAILED", "PARTIALLY_COMPLETED", "CANCELLED", "ABORTED", "UNCERTAIN"}:
                raise ExecutionLedgerError("ledger_terminal", "终态账本不得签发新的 executor 句柄")
            self._fencing_counter += 1
            token = f"fence-{self._fencing_counter}"
            self._current_fencing_token = token
            return LedgerExecutor(self, executor_instance_id, token)

    def snapshot(self) -> ExecutionLedgerSnapshot:
        with self._lock:
            return ExecutionLedgerSnapshot(
                ledger=self._ledger,
                action_ids=self._action_ids,
                action_boundaries=self._action_boundaries,
                attempts=tuple(self._attempts.values()),
                results=tuple(self._results.values()),
                stop_request=self._stop_request,
            )

    def classify(self, action_id: str) -> LedgerClassification:
        require_identifier("action_id", action_id)
        with self._lock:
            self._require_allowed_action(action_id)
            return self._classify_locked(action_id)

    def request_stop(self, *, reason: str, requested_at: str) -> StopRequest:
        try:
            require_choice("reason", reason, {"user_stopped", "user_cancelled", "system_aborted"})
            requested = parse_utc_timestamp("requested_at", requested_at)
        except Exception as exc:
            raise ExecutionLedgerError("invalid_stop_time", "停止请求时间或原因无效") from exc
        with self._lock:
            if requested < parse_utc_timestamp("created_at", self._ledger.created_at):
                raise ExecutionLedgerError("invalid_stop_time", "停止请求不得早于账本创建时间")
            if self._stop_request is not None:
                raise ExecutionLedgerError("stop_replayed", "execution 已经记录停止请求")
            active = next((item for item in self._attempts.values() if item.status in {"STARTED", "CANCEL_REQUESTED"}), None)
            if active is None:
                disposition = CancellationDisposition.BLOCKED_BEFORE_START
                self._ledger = replace(self._ledger, status=self._terminal_status(reason))
                request = StopRequest(reason, requested_at, disposition)
            else:
                boundary = self._boundary_for(active.action_id)
                if active.attempt_id in self._unsafe_attempts:
                    disposition = CancellationDisposition.DEFERRED_UNSAFE_ATOMIC
                elif boundary.cancellation_mode == "cooperative":
                    disposition = CancellationDisposition.COOPERATIVE_REQUESTED
                    self._attempts[active.attempt_id] = replace(
                        active, status="CANCEL_REQUESTED", cancellation_requested_at=requested_at
                    )
                else:
                    disposition = CancellationDisposition.WAIT_FOR_ATOMIC_RETURN
                request = StopRequest(reason, requested_at, disposition, active.action_id, active.attempt_id)
            self._stop_request = request
            return request

    @staticmethod
    def _terminal_status(reason: str) -> str:
        return "ABORTED" if reason == "system_aborted" else "CANCELLED"

    def _require_current_executor(self, fencing_token: str) -> None:
        if self._current_fencing_token != fencing_token:
            raise ExecutionLedgerError("stale_fencing_token", "旧 fencing token 不得写入账本")

    def _require_allowed_action(self, action_id: str) -> None:
        if action_id not in self._action_ids:
            raise ExecutionLedgerError("action_not_authorized", "action_id 不在本次授权动作集合中")

    def _boundary_for(self, action_id: str) -> PreflightActionBoundary:
        return next(item for item in self._action_boundaries if item.action_id == action_id)

    def _require_start_order(self, action_id: str) -> None:
        index = self._action_ids.index(action_id)
        for previous in self._action_ids[:index]:
            if self._classify_locked(previous) != LedgerClassification.COMPLETED:
                raise ExecutionLedgerError("action_out_of_order", "动作必须按冻结计划顺序开始")

    def _start(self, *, executor_instance_id: str, fencing_token: str, action_id: str,
               attempt_id: str, started_at: str) -> ActionAttempt:
        require_identifier("action_id", action_id)
        require_identifier("attempt_id", attempt_id)
        parse_utc_timestamp("started_at", started_at)
        with self._lock:
            self._require_current_executor(fencing_token)
            self._require_allowed_action(action_id)
            existing = self._attempts.get(attempt_id)
            if existing is not None:
                if existing.action_id != action_id or existing.executor_instance_id != executor_instance_id:
                    raise ExecutionLedgerError("attempt_rebind", "attempt_id 不得重绑到不同动作或执行器")
                raise ExecutionLedgerError("attempt_replayed", "attempt_id 已记录 operation_started")
            if self._stop_request is not None:
                raise ExecutionLedgerError("execution_stopped", "停止请求后不得开始新动作")
            self._require_start_order(action_id)
            classification = self._classify_locked(action_id)
            if classification == LedgerClassification.STATE_UNCERTAIN:
                raise ExecutionLedgerError("state_uncertain", "state_uncertain 是终态观察，不得隐式重新开始")
            if self._ledger.status in {"COMPLETED", "FAILED", "PARTIALLY_COMPLETED", "CANCELLED", "ABORTED", "UNCERTAIN"}:
                raise ExecutionLedgerError("ledger_terminal", "终态账本不得记录新的 operation_started")
            if classification != LedgerClassification.NOT_STARTED:
                raise ExecutionLedgerError("action_already_recorded", "动作已有账本事实，不得重复开始")
            attempt = ActionAttempt(
                execution_id=self._ledger.execution_id,
                action_id=action_id,
                attempt_id=attempt_id,
                executor_instance_id=executor_instance_id,
                fencing_token=fencing_token,
                status="STARTED",
                started_at=started_at,
                finished_at=None,
                cancellation_requested_at=None,
            )
            self._attempts[attempt_id] = attempt
            if self._ledger.status == "CREATED":
                self._ledger = replace(self._ledger, status="RUNNING")
            return attempt

    def _return(self, *, executor_instance_id: str, fencing_token: str, action_id: str,
                attempt_id: str, finished_at: str) -> ActionAttempt:
        require_identifier("action_id", action_id)
        require_identifier("attempt_id", attempt_id)
        finished = parse_utc_timestamp("finished_at", finished_at)
        with self._lock:
            self._require_current_executor(fencing_token)
            self._require_allowed_action(action_id)
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.action_id != action_id or attempt.executor_instance_id != executor_instance_id:
                raise ExecutionLedgerError("attempt_identity_mismatch", "返回记录与 attempt_id 身份冲突")
            if attempt.fencing_token != fencing_token:
                raise ExecutionLedgerError("attempt_fencing_mismatch", "attempt 绑定的 fencing token 与当前句柄不一致")
            if attempt.status not in {"STARTED", "CANCEL_REQUESTED"}:
                raise ExecutionLedgerError("invalid_attempt_transition", "只有活动 attempt 才能记录 returned")
            if attempt.started_at is None or finished < parse_utc_timestamp("started_at", attempt.started_at):
                raise ExecutionLedgerError("invalid_finished_at", "finished_at 不得早于 started_at")
            updated = replace(attempt, status="RETURNED", finished_at=finished_at)
            self._attempts[attempt_id] = updated
            self._unsafe_attempts.discard(attempt_id)
            if self._stop_request is not None:
                self._ledger = replace(self._ledger, status=self._terminal_status(self._stop_request.reason))
            return updated

    def _record_result(self, *, executor_instance_id: str, fencing_token: str,
                       result: ActionResult) -> ActionResult:
        if not isinstance(result, ActionResult):
            raise TypeError("result 必须是 ActionResult")
        completed = parse_utc_timestamp("completed_at", result.completed_at)
        with self._lock:
            self._require_current_executor(fencing_token)
            self._require_allowed_action(result.action_id)
            attempt = self._attempts.get(result.attempt_id)
            if attempt is None or (attempt.execution_id, attempt.action_id, attempt.executor_instance_id) != (
                    result.execution_id, result.action_id, executor_instance_id):
                raise ExecutionLedgerError("result_identity_mismatch", "结果记录与 attempt_id 身份冲突")
            if attempt.fencing_token != fencing_token:
                raise ExecutionLedgerError("attempt_fencing_mismatch", "attempt 绑定的 fencing token 与当前句柄不一致")
            if result.attempt_id in self._results:
                raise ExecutionLedgerError("result_replayed", "attempt_id 已记录结果，不得重复完成")
            if result.status == "completed" and attempt.status != "RETURNED":
                raise ExecutionLedgerError("completion_before_return", "completed 结果必须在 operation_returned 之后记录")
            reference_at = attempt.finished_at or attempt.started_at
            if reference_at is None or completed < parse_utc_timestamp("attempt_time", reference_at):
                raise ExecutionLedgerError("invalid_completed_at", "completed_at 不得早于 attempt 的已记录时间")
            self._results[result.attempt_id] = result
            if result.status == "state_uncertain":
                self._ledger = replace(self._ledger, status="UNCERTAIN")
            elif self._stop_request is None and all(
                self._classify_locked(action_id) == LedgerClassification.COMPLETED
                for action_id in self._action_ids
            ):
                self._ledger = replace(self._ledger, status="COMPLETED")
            return result

    def _finalize(self, *, executor_instance_id: str, fencing_token: str,
                  overall_status: str) -> ExecutionLedger:
        require_choice("overall_status", overall_status, {"failed", "partially_completed"})
        with self._lock:
            self._require_current_executor(fencing_token)
            if self._stop_request is not None:
                raise ExecutionLedgerError("execution_stopped", "停止后的账本不得改写为执行结果终态")
            if self._ledger.status == "UNCERTAIN":
                raise ExecutionLedgerError("uncertain_terminal", "state_uncertain 不得降级为其他终态")
            if self._ledger.status in {"COMPLETED", "FAILED", "PARTIALLY_COMPLETED", "CANCELLED", "ABORTED"}:
                raise ExecutionLedgerError("ledger_terminal", "终态账本不得重复终结")
            if any(item.status in {"STARTED", "CANCEL_REQUESTED"} for item in self._attempts.values()):
                raise ExecutionLedgerError("active_attempt", "存在活动 attempt 时不得终结 execution")
            if any(item.attempt_id not in self._results for item in self._attempts.values()):
                raise ExecutionLedgerError("result_missing", "所有已开始动作都必须有结果才能终结 execution")
            statuses = tuple(item.status for item in self._results.values())
            completed_count = statuses.count("completed")
            failed_count = statuses.count("not_completed")
            if "state_uncertain" in statuses:
                raise ExecutionLedgerError("uncertain_terminal", "state_uncertain 不得降级为其他终态")
            expected = "partially_completed" if completed_count and failed_count else "failed"
            if failed_count == 0 or expected != overall_status:
                raise ExecutionLedgerError("overall_status_mismatch", "整体终态与动作结果事实不一致")
            ledger_status = "PARTIALLY_COMPLETED" if overall_status == "partially_completed" else "FAILED"
            self._ledger = replace(self._ledger, status=ledger_status)
            return self._ledger
    def _enter_unsafe_atomic(self, *, executor_instance_id: str, fencing_token: str,
                             action_id: str, attempt_id: str, entered_at: str) -> ActionAttempt:
        parse_utc_timestamp("entered_at", entered_at)
        with self._lock:
            self._require_current_executor(fencing_token)
            attempt = self._require_attempt(executor_instance_id, fencing_token, action_id, attempt_id)
            if attempt.status != "STARTED":
                raise ExecutionLedgerError("invalid_attempt_transition", "只有 STARTED attempt 才能进入不可安全中断区间")
            self._unsafe_attempts.add(attempt_id)
            return attempt

    def _leave_unsafe_atomic(self, *, executor_instance_id: str, fencing_token: str,
                             action_id: str, attempt_id: str, exited_at: str) -> ActionAttempt:
        parse_utc_timestamp("exited_at", exited_at)
        with self._lock:
            self._require_current_executor(fencing_token)
            attempt = self._require_attempt(executor_instance_id, fencing_token, action_id, attempt_id)
            self._unsafe_attempts.discard(attempt_id)
            boundary = self._boundary_for(action_id)
            if self._stop_request is not None and attempt.status == "STARTED" and boundary.cancellation_mode == "cooperative":
                updated = replace(
                    attempt,
                    status="CANCEL_REQUESTED",
                    cancellation_requested_at=self._stop_request.requested_at,
                )
                self._attempts[attempt_id] = updated
                self._stop_request = replace(
                    self._stop_request,
                    disposition=CancellationDisposition.COOPERATIVE_REQUESTED,
                )
                return updated
            return attempt

    def _require_attempt(self, executor_instance_id: str, fencing_token: str,
                         action_id: str, attempt_id: str) -> ActionAttempt:
        require_identifier("action_id", action_id)
        require_identifier("attempt_id", attempt_id)
        self._require_allowed_action(action_id)
        attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.action_id != action_id or attempt.executor_instance_id != executor_instance_id:
            raise ExecutionLedgerError("attempt_identity_mismatch", "attempt_id 身份冲突")
        if attempt.fencing_token != fencing_token:
            raise ExecutionLedgerError("attempt_fencing_mismatch", "attempt 绑定的 fencing token 与当前句柄不一致")
        return attempt

    def assert_attempt_identity(self, *, attempt_id: str, execution_id: str, action_id: str) -> None:
        require_identifier("attempt_id", attempt_id)
        require_identifier("execution_id", execution_id)
        require_identifier("action_id", action_id)
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None:
                raise ExecutionLedgerError("attempt_unknown", "账本中不存在该 attempt_id")
            if attempt.execution_id != execution_id:
                raise ExecutionLedgerError("execution_identity_mismatch", "attempt_id 与 execution_id 身份冲突")
            if attempt.action_id != action_id:
                raise ExecutionLedgerError("attempt_identity_mismatch", "attempt_id 与 action_id 身份冲突")

    def _classify_locked(self, action_id: str) -> LedgerClassification:
        result = next((item for item in self._results.values() if item.action_id == action_id), None)
        if result is not None:
            if result.status == "completed":
                return LedgerClassification.COMPLETED
            if result.status == "state_uncertain":
                return LedgerClassification.STATE_UNCERTAIN
            return LedgerClassification.RETURNED
        attempts = [item for item in self._attempts.values() if item.action_id == action_id]
        if not attempts:
            return LedgerClassification.NOT_STARTED
        return LedgerClassification.STARTED if attempts[-1].status in {"STARTED", "CANCEL_REQUESTED"} else LedgerClassification.RETURNED


class LedgerExecutor:
    __slots__ = ("_store", "executor_instance_id", "fencing_token")

    def __init__(self, store: ExecutionLedgerStore, executor_instance_id: str, fencing_token: str) -> None:
        self._store = store
        self.executor_instance_id = executor_instance_id
        self.fencing_token = fencing_token

    def operation_started(self, *, action_id: str, attempt_id: str, started_at: str) -> ActionAttempt:
        return self._store._start(
            executor_instance_id=self.executor_instance_id, fencing_token=self.fencing_token,
            action_id=action_id, attempt_id=attempt_id, started_at=started_at,
        )

    def operation_returned(self, *, action_id: str, attempt_id: str, finished_at: str) -> ActionAttempt:
        return self._store._return(
            executor_instance_id=self.executor_instance_id, fencing_token=self.fencing_token,
            action_id=action_id, attempt_id=attempt_id, finished_at=finished_at,
        )

    def record_result(self, result: ActionResult) -> ActionResult:
        return self._store._record_result(
            executor_instance_id=self.executor_instance_id,
            fencing_token=self.fencing_token,
            result=result,
        )

    def finalize(self, *, overall_status: str) -> ExecutionLedger:
        return self._store._finalize(
            executor_instance_id=self.executor_instance_id,
            fencing_token=self.fencing_token,
            overall_status=overall_status,
        )
    def enter_unsafe_atomic(self, *, action_id: str, attempt_id: str, entered_at: str) -> ActionAttempt:
        return self._store._enter_unsafe_atomic(
            executor_instance_id=self.executor_instance_id, fencing_token=self.fencing_token,
            action_id=action_id, attempt_id=attempt_id, entered_at=entered_at,
        )

    def leave_unsafe_atomic(self, *, action_id: str, attempt_id: str, exited_at: str) -> ActionAttempt:
        return self._store._leave_unsafe_atomic(
            executor_instance_id=self.executor_instance_id, fencing_token=self.fencing_token,
            action_id=action_id, attempt_id=attempt_id, exited_at=exited_at,
        )
