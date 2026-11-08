"""相互正交的任务、Provider、输入和输出状态机。"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar


class StateTransitionError(ValueError):
    """状态变化不在冻结转换图中。"""


class TaskState(str, Enum):
    IDLE = "IDLE"
    UNDERSTANDING = "UNDERSTANDING"
    PLANNING = "PLANNING"
    WAITING_AUTH = "WAITING_AUTH"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ABORTED = "ABORTED"
    UNCERTAIN = "UNCERTAIN"


class ProviderLifecycleState(str, Enum):
    READY = "READY"
    FAILED = "FAILED"
    COOLDOWN = "COOLDOWN"
    PROBING = "PROBING"
    RECOVERED_PENDING = "RECOVERED_PENDING"


class InputState(str, Enum):
    IDLE = "IDLE"
    WARMING = "WARMING"
    ACTIVE = "ACTIVE"
    FINAL_ACCEPTED = "FINAL_ACCEPTED"
    REJECTED = "REJECTED"
    CLOSING = "CLOSING"
    FAILED = "FAILED"


class OutputState(str, Enum):
    QUEUED = "QUEUED"
    SYNTHESIZING = "SYNTHESIZING"
    READY = "READY"
    COMMITTED_NOT_STARTED = "COMMITTED_NOT_STARTED"
    PLAYING = "PLAYING"
    PLAYED = "PLAYED"
    ABORTED = "ABORTED"


_TASK_TRANSITIONS = {
    TaskState.IDLE: {TaskState.UNDERSTANDING},
    TaskState.UNDERSTANDING: {TaskState.PLANNING, TaskState.CANCELLED, TaskState.ABORTED},
    TaskState.PLANNING: {TaskState.WAITING_AUTH, TaskState.CANCELLED, TaskState.ABORTED},
    TaskState.WAITING_AUTH: {TaskState.EXECUTING, TaskState.CANCELLED, TaskState.ABORTED},
    TaskState.EXECUTING: {TaskState.VERIFYING, TaskState.CANCELLED, TaskState.ABORTED, TaskState.UNCERTAIN},
    TaskState.VERIFYING: {TaskState.COMPLETED, TaskState.ABORTED, TaskState.UNCERTAIN},
    TaskState.COMPLETED: set(), TaskState.CANCELLED: set(), TaskState.ABORTED: set(), TaskState.UNCERTAIN: set(),
}
_PROVIDER_TRANSITIONS = {
    ProviderLifecycleState.READY: {ProviderLifecycleState.FAILED},
    ProviderLifecycleState.FAILED: {ProviderLifecycleState.COOLDOWN},
    ProviderLifecycleState.COOLDOWN: {ProviderLifecycleState.PROBING},
    ProviderLifecycleState.PROBING: {ProviderLifecycleState.FAILED, ProviderLifecycleState.COOLDOWN, ProviderLifecycleState.RECOVERED_PENDING},
    ProviderLifecycleState.RECOVERED_PENDING: {ProviderLifecycleState.READY, ProviderLifecycleState.FAILED},
}
_INPUT_TRANSITIONS = {
    InputState.IDLE: {InputState.WARMING, InputState.ACTIVE, InputState.CLOSING, InputState.FAILED},
    InputState.WARMING: {InputState.ACTIVE, InputState.REJECTED, InputState.CLOSING, InputState.FAILED},
    InputState.ACTIVE: {InputState.FINAL_ACCEPTED, InputState.REJECTED, InputState.CLOSING, InputState.FAILED},
    InputState.FINAL_ACCEPTED: {InputState.CLOSING}, InputState.REJECTED: {InputState.CLOSING},
    InputState.CLOSING: {InputState.IDLE, InputState.FAILED}, InputState.FAILED: {InputState.IDLE},
}
_OUTPUT_TRANSITIONS = {
    OutputState.QUEUED: {OutputState.SYNTHESIZING, OutputState.ABORTED},
    OutputState.SYNTHESIZING: {OutputState.READY, OutputState.ABORTED},
    OutputState.READY: {OutputState.COMMITTED_NOT_STARTED, OutputState.ABORTED},
    OutputState.COMMITTED_NOT_STARTED: {OutputState.PLAYING, OutputState.ABORTED},
    OutputState.PLAYING: {OutputState.PLAYED, OutputState.ABORTED},
    OutputState.PLAYED: set(), OutputState.ABORTED: set(),
}
_State = TypeVar("_State", bound=Enum)


def _transition(current: _State, target: _State, graph: dict[_State, set[_State]]) -> _State:
    if target not in graph[current]:
        raise StateTransitionError(f"非法状态转换: {current.value} -> {target.value}")
    return target


def transition_task(current: TaskState, target: TaskState) -> TaskState:
    return _transition(current, target, _TASK_TRANSITIONS)


def transition_provider(current: ProviderLifecycleState, target: ProviderLifecycleState) -> ProviderLifecycleState:
    return _transition(current, target, _PROVIDER_TRANSITIONS)


def transition_input(current: InputState, target: InputState) -> InputState:
    return _transition(current, target, _INPUT_TRANSITIONS)


def transition_output(current: OutputState, target: OutputState) -> OutputState:
    return _transition(current, target, _OUTPUT_TRANSITIONS)