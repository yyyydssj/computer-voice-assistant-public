"""P5 批次 8：分层、有限且 fail-closed 的重试策略。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.contracts.base import ContractError, require_identifier, require_positive


class RetryLayer(str, Enum):
    """重试所属层；三层策略不得互相替代。"""

    READ_ONLY_QUERY = "read_only_query"
    AGENT_GENERATION = "agent_generation"
    SIDE_EFFECT_ACTION = "side_effect_action"


class RetryDisposition(str, Enum):
    RETRY = "retry"
    NEW_REQUEST_REQUIRED = "new_request_required"
    NO_RETRY = "no_retry"
    SCAN_BEFORE_DECISION = "scan_before_decision"
    BLOCKED_STATE_UNCERTAIN = "blocked_state_uncertain"


class ObservedState(str, Enum):
    """只读观察到的动作状态。"""

    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    NOT_COMPLETED = "not_completed"
    UNKNOWN_RESULT = "unknown_result"
    STATE_UNCERTAIN = "state_uncertain"



class RetryableReadOnlyError(RuntimeError):
    """调用方明确标记的只读瞬态故障；其他异常不得自动重试。"""



@dataclass(frozen=True, slots=True)
class RetryDecision:
    layer: RetryLayer
    disposition: RetryDisposition
    attempt: int
    max_attempts: int
    delay_seconds: float = 0.0
    request_id: str | None = None
    replacement_request_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.layer, RetryLayer):
            raise ContractError("RetryDecision.layer 无效")
        if not isinstance(self.disposition, RetryDisposition):
            raise ContractError("RetryDecision.disposition 无效")
        require_positive("RetryDecision.attempt", self.attempt)
        require_positive("RetryDecision.max_attempts", self.max_attempts)
        if self.attempt > self.max_attempts:
            raise ContractError("RetryDecision.attempt 不得超过 max_attempts")
        if self.delay_seconds < 0:
            raise ContractError("RetryDecision.delay_seconds 不得为负数")
        if self.request_id is not None:
            require_identifier("RetryDecision.request_id", self.request_id)
        if self.replacement_request_id is not None:
            require_identifier("RetryDecision.replacement_request_id", self.replacement_request_id)
        if self.layer == RetryLayer.AGENT_GENERATION:
            if self.disposition == RetryDisposition.NEW_REQUEST_REQUIRED and self.request_id is None:
                raise ContractError("Agent 生成重试必须保留原 request_id 事实")
            if self.request_id is not None and self.replacement_request_id == self.request_id:
                raise ContractError("Agent 生成重试不得复用原 request_id")
        elif self.replacement_request_id is not None:
            raise ContractError("只有 Agent 生成层允许 replacement_request_id")
        if self.disposition in {
            RetryDisposition.SCAN_BEFORE_DECISION,
            RetryDisposition.BLOCKED_STATE_UNCERTAIN,
        } and self.layer != RetryLayer.SIDE_EFFECT_ACTION:
            raise ContractError("扫描或 state_uncertain 阻断只适用于副作用动作")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """只输出策略决策，不执行网络、等待、扫描或副作用动作。"""

    read_only_max_attempts: int = 3
    read_only_base_delay_seconds: float = 0.25
    read_only_max_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        require_positive("read_only_max_attempts", self.read_only_max_attempts)
        if self.read_only_base_delay_seconds < 0:
            raise ContractError("read_only_base_delay_seconds 不得为负数")
        if self.read_only_max_delay_seconds < 0:
            raise ContractError("read_only_max_delay_seconds 不得为负数")
        if self.read_only_base_delay_seconds > self.read_only_max_delay_seconds:
            raise ContractError("只读重试基础等待不得超过等待上限")

    def read_only(self, *, attempt: int, transient: bool) -> RetryDecision:
        """只读查询仅在瞬态故障且未超过上限时重试。"""
        require_positive("attempt", attempt)
        if not transient or attempt >= self.read_only_max_attempts:
            reason = "非瞬态故障" if not transient else "已达到只读查询重试上限"
            return RetryDecision(
                layer=RetryLayer.READ_ONLY_QUERY,
                disposition=RetryDisposition.NO_RETRY,
                attempt=attempt,
                max_attempts=self.read_only_max_attempts,
                reason=reason,
            )
        delay = min(
            self.read_only_max_delay_seconds,
            self.read_only_base_delay_seconds * (2 ** (attempt - 1)),
        )
        return RetryDecision(
            layer=RetryLayer.READ_ONLY_QUERY,
            disposition=RetryDisposition.RETRY,
            attempt=attempt,
            max_attempts=self.read_only_max_attempts,
            delay_seconds=delay,
            reason="只读查询瞬态故障，允许有界重试",
        )

    def agent_generation(
        self,
        *,
        request_id: str,
        attempt: int,
        transient: bool,
        replacement_request_id: str | None = None,
    ) -> RetryDecision:
        """Agent 生成失败不得复用原请求；重试必须由调用方显式创建新 request。"""
        require_identifier("request_id", request_id)
        require_positive("attempt", attempt)
        if not transient:
            return RetryDecision(
                layer=RetryLayer.AGENT_GENERATION,
                disposition=RetryDisposition.NO_RETRY,
                attempt=attempt,
                max_attempts=attempt,
                request_id=request_id,
                reason="Agent 生成故障不是可自动重试的瞬态故障",
            )
        if not replacement_request_id:
            return RetryDecision(
                layer=RetryLayer.AGENT_GENERATION,
                disposition=RetryDisposition.NEW_REQUEST_REQUIRED,
                attempt=attempt,
                max_attempts=attempt + 1,
                request_id=request_id,
                reason="必须先创建新的 request_id，禁止隐式重提原请求",
            )
        require_identifier("replacement_request_id", replacement_request_id)
        if replacement_request_id == request_id:
            raise ContractError("replacement_request_id 必须不同于原 request_id")
        return RetryDecision(
            layer=RetryLayer.AGENT_GENERATION,
            disposition=RetryDisposition.NEW_REQUEST_REQUIRED,
            attempt=attempt,
            max_attempts=attempt + 1,
            request_id=request_id,
            replacement_request_id=replacement_request_id,
            reason="仅允许使用新的 request_id 重新生成",
        )

    def side_effect(
        self,
        *,
        attempt: int,
        observed_state: ObservedState,
    ) -> RetryDecision:
        """副作用动作默认不自动重试；未知结果必须先扫描，不能改写状态。"""
        require_positive("attempt", attempt)
        if not isinstance(observed_state, ObservedState):
            raise ContractError("observed_state 无效")
        if observed_state == ObservedState.STATE_UNCERTAIN:
            return RetryDecision(
                layer=RetryLayer.SIDE_EFFECT_ACTION,
                disposition=RetryDisposition.BLOCKED_STATE_UNCERTAIN,
                attempt=attempt,
                max_attempts=attempt,
                reason="state_uncertain 不得改写为成功、失败或可重试",
            )
        if observed_state in {ObservedState.COMPLETED, ObservedState.NOT_COMPLETED}:
            return RetryDecision(
                layer=RetryLayer.SIDE_EFFECT_ACTION,
                disposition=RetryDisposition.NO_RETRY,
                attempt=attempt,
                max_attempts=attempt,
                reason="副作用动作默认不得自动重试",
            )
        if observed_state == ObservedState.UNKNOWN_RESULT:
            return RetryDecision(
                layer=RetryLayer.SIDE_EFFECT_ACTION,
                disposition=RetryDisposition.SCAN_BEFORE_DECISION,
                attempt=attempt,
                max_attempts=attempt,
                reason="副作用动作结果未知，必须先只读扫描真实状态",
            )
        return RetryDecision(
            layer=RetryLayer.SIDE_EFFECT_ACTION,
            disposition=RetryDisposition.NO_RETRY,
            attempt=attempt,
            max_attempts=attempt,
            reason="副作用动作已知未开始，默认不得自动重试",
        )

    def decide(
        self,
        *,
        layer: RetryLayer,
        attempt: int,
        transient: bool = False,
        request_id: str | None = None,
        replacement_request_id: str | None = None,
        observed_state: ObservedState | None = None,
    ) -> RetryDecision:
        """统一入口；要求每层显式提供该层所需事实，避免策略串层。"""
        if layer == RetryLayer.READ_ONLY_QUERY:
            if request_id is not None or replacement_request_id is not None or observed_state is not None:
                raise ContractError("只读查询决策不得携带 Agent 或副作用事实")
            return self.read_only(attempt=attempt, transient=transient)
        if layer == RetryLayer.AGENT_GENERATION:
            if request_id is None or observed_state is not None:
                raise ContractError("Agent 生成决策必须携带 request_id 且不得携带副作用状态")
            return self.agent_generation(
                request_id=request_id,
                attempt=attempt,
                transient=transient,
                replacement_request_id=replacement_request_id,
            )
        if layer == RetryLayer.SIDE_EFFECT_ACTION:
            if observed_state is None or request_id is not None or replacement_request_id is not None:
                raise ContractError("副作用决策必须只携带 observed_state")
            return self.side_effect(attempt=attempt, observed_state=observed_state)
        raise ContractError("不支持的重试层")


class ReadOnlyRetryRunner:
    """只读查询的有限执行器；注入 query 与 sleep，绝不接受副作用回调。"""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self._sleep = sleep or (lambda _seconds: None)

    def run(self, query: Callable[[], object]) -> object:
        if not callable(query):
            raise TypeError("query 必须可调用")
        attempt = 1
        while True:
            try:
                return query()
            except RetryableReadOnlyError:
                decision = self.policy.read_only(attempt=attempt, transient=True)
                if decision.disposition != RetryDisposition.RETRY:
                    raise
                self._sleep(decision.delay_seconds)
                attempt += 1
