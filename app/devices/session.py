"""P10 批次 R3：音频设备会话与恢复策略（纯离线决策层）。

设计依据：`implementation_plan/13-device-tuning.md` 与路线图 P10 退出条件——
**设备恢复只在安全空闲点启用**、**不自动切换到未知设备**、**设备切换只在下一轮生效**、
设备异常时**保留文字模式**、**失效未完成语音确认且不改变执行计划**。

本模块只做**决策**：什么时候允许重连、什么时候必须延后、该怎么如实提示。
它**不**打开设备、**不**播放/录制、**不**触碰授权与计划（由调用方按 `DeviceAction` 执行）。

三条硬不变量（有测试断言）：
1. **恢复 ≠ 授权**：本模块不导入也不修改安全链，任何设备事件都不产生/失效授权或计划事实；
2. **不切未知设备**：重连目标只能是"用户固定的那个设备"；枚举到**新**设备一律不自动采用；
3. **不静默**：任何延后/降级/未恢复都有固定短代码与可读提示，绝不假装已恢复。

硬边界：不访问设备/文件/网络、不创建线程；只依赖 stdlib 与项目契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.contracts.base import ContractError, require_positive

# 动作（固定短代码；由调用方执行，本模块不做 I/O）
ACTION_NONE = "none"
ACTION_RECONNECT_INPUT = "reconnect_input"
ACTION_RECONNECT_OUTPUT = "reconnect_output"
ACTION_FALLBACK_TO_TEXT = "fallback_to_text"
ACTION_NOTIFY_USER = "notify_user"

# 状态/原因（固定短代码）
REASON_HEALTHY = "healthy"
REASON_INPUT_LOST = "input_lost"
REASON_OUTPUT_LOST = "output_lost"
REASON_BUSY_ELSEWHERE = "busy_elsewhere"
REASON_PERMISSION_DENIED = "permission_denied"
REASON_INCOMPATIBLE = "incompatible"
REASON_DEVICE_UNKNOWN = "device_unknown"
REASON_DEFERRED_NOT_IDLE = "deferred_not_safe_point"
REASON_DEFERRED_AWAITING_CONFIRMATION = "deferred_awaiting_confirmation"
REASON_DEFERRED_ACTIVE_TURN = "deferred_active_turn"
REASON_RECONNECT_ATTEMPTED = "reconnect_attempted"
REASON_RECONNECT_FAILED = "reconnect_failed"
REASON_NO_FIXED_DEVICE = "no_fixed_device_target"
REASON_NEW_DEVICE_IGNORED = "new_device_ignored"
REASON_TEXT_MODE_KEPT = "text_mode_kept"

DEVICE_KINDS = ("input", "output")

_ERROR_CODES = frozenset({"unknown_device_kind", "invalid_health"})


class DeviceRecoveryError(ContractError):
    """设备策略拒绝输入；``str()`` 只返回固定短代码。"""

    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "unknown_device_kind"
        super().__init__(self.code)


class DeviceHealth(str, Enum):
    """设备健康状态；``UNKNOWN`` 表示"还没确认过"，与"正常"严格区分。"""

    UNKNOWN = "unknown"
    READY = "ready"
    DISCONNECTED = "disconnected"
    BUSY = "busy"
    PERMISSION_DENIED = "permission_denied"
    INCOMPATIBLE = "incompatible"


class DeviceKind(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


# 允许自动重连的状态：只有"断开/尚未确认"才值得重试；
# 忙（被别的程序占用）与权限拒绝属于环境问题，重试只会刷屏。
_RECONNECTABLE = (DeviceHealth.DISCONNECTED, DeviceHealth.UNKNOWN)
_NO_AUTO_RETRY = (DeviceHealth.BUSY, DeviceHealth.PERMISSION_DENIED, DeviceHealth.INCOMPATIBLE)


def _require_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} 必须是布尔值")
    return value


@dataclass(frozen=True, slots=True)
class DeviceOpenRequest:
    """调用方拿到的重连指令：只含"连哪个"，不含任何授权语义。"""

    kind: DeviceKind
    health: DeviceHealth
    reason: str
    preferred_device_id: object

    def to_details(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "health": self.health.value,
            "reason": self.reason,
            "preferred_device_id": self.preferred_device_id,
        }


@dataclass(frozen=True, slots=True)
class DeviceAction:
    """一个决策结果：``action`` 是目前该做的事，``messages`` 是给用户的固定短代码提示。"""

    action: str
    reason: str
    input_health: DeviceHealth
    output_health: DeviceHealth
    already_ready: bool
    safety_point_reached: bool
    text_mode_available: bool
    open_request: DeviceOpenRequest | None
    messages: tuple[str, ...]
    device_epoch: int

    def to_details(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "input_health": self.input_health.value,
            "output_health": self.output_health.value,
            "already_ready": self.already_ready,
            "safety_point_reached": self.safety_point_reached,
            "text_mode_available": self.text_mode_available,
            "open_request": None if self.open_request is None else self.open_request.to_details(),
            "messages": list(self.messages),
            "device_epoch": self.device_epoch,
        }


@dataclass(frozen=True, slots=True)
class DeviceObservation:
    """一次设备状态观察；``epoch`` 由调用方按设备枚举结果递增。"""

    kind: DeviceKind
    health: DeviceHealth
    device_id: object = None
    epoch: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DeviceKind):
            raise DeviceRecoveryError("unknown_device_kind")
        if not isinstance(self.health, DeviceHealth):
            raise DeviceRecoveryError("invalid_health")
        require_positive("epoch", self.epoch)

    def to_details(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "health": self.health.value,
            "device_id": self.device_id,
            "epoch": self.epoch,
        }


class DeviceRecoveryPolicy:
    """设备恢复决策器（纯离线、确定性）。

    典型用法::

        policy = DeviceRecoveryPolicy(input_device_id="1")
        action = policy.register(DeviceObservation(DeviceKind.INPUT, DeviceHealth.READY, "1", 1))
        # 设备掉了：先登记，再在**安全空闲点**取决策
        policy.register(DeviceObservation(DeviceKind.INPUT, DeviceHealth.DISCONNECTED, "1", 2))
        action = policy.decide(
            turn_active=False, service_active=False, awaiting_confirmation=False
        )
        if action.action == ACTION_RECONNECT_INPUT:
            ok = try_open_device(action.open_request)   # 由调用方执行
            policy.note_attempt(DeviceKind.INPUT, succeeded=ok)
    """

    def __init__(self, *, input_device_id: object = None, output_device_id: object = None) -> None:
        self._fixed: dict[DeviceKind, object] = {
            DeviceKind.INPUT: input_device_id,
            DeviceKind.OUTPUT: output_device_id,
        }
        self._health: dict[DeviceKind, DeviceHealth] = {
            DeviceKind.INPUT: DeviceHealth.UNKNOWN,
            DeviceKind.OUTPUT: DeviceHealth.UNKNOWN,
        }
        self._epoch: dict[DeviceKind, int] = {DeviceKind.INPUT: 1, DeviceKind.OUTPUT: 1}
        self._messages: dict[DeviceKind, tuple[str, ...]] = {
            DeviceKind.INPUT: (),
            DeviceKind.OUTPUT: (),
        }
        self._new_device_seen = False
        self._text_mode_available = True
        self._input_id: object = input_device_id
        self._output_id: object = output_device_id
        self._attempts = 0

    # ---------------------------------------------------------------- properties

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def new_device_seen(self) -> bool:
        return self._new_device_seen

    @property
    def text_mode_available(self) -> bool:
        return self._text_mode_available

    def health(self, kind: DeviceKind) -> DeviceHealth:
        return self._health[kind]

    def epoch(self, kind: DeviceKind) -> int:
        return self._epoch[kind]

    def fixed_device_id(self, kind: DeviceKind) -> object:
        return self._fixed[kind]

    def to_details(self) -> dict[str, object]:
        return {
            "fixed_input_device": self._fixed[DeviceKind.INPUT],
            "fixed_output_device": self._fixed[DeviceKind.OUTPUT],
            "input_health": self._health[DeviceKind.INPUT].value,
            "output_health": self._health[DeviceKind.OUTPUT].value,
            "input_epoch": self._epoch[DeviceKind.INPUT],
            "output_epoch": self._epoch[DeviceKind.OUTPUT],
            "attempts": self._attempts,
            "new_device_seen": self._new_device_seen,
            "text_mode_available": self._text_mode_available,
        }

    # ---------------------------------------------------------------- observations

    def register(self, observation: DeviceObservation) -> dict[str, object]:
        """登记一次观察；返回结构化事实（含是否需要 epoch 变更）。"""

        kind = observation.kind
        previous = self._health[kind]
        previous_id = self._fixed[kind]
        self._health[kind] = observation.health

        if observation.device_id is not None:
            if previous_id is None:
                # 首次确认"用户固定的设备"：允许采用。
                self._fixed[kind] = observation.device_id
            elif observation.device_id != previous_id:
                # 枚举到**新**设备：不自动采用，只登记事实（不切未知设备）。
                self._new_device_seen = True

        epoch_changed = observation.epoch != self._epoch[kind]
        if epoch_changed:
            self._epoch[kind] = observation.epoch

        messages: list[str] = []
        if previous is not observation.health:
            if observation.health is DeviceHealth.DISCONNECTED:
                messages.append(
                    REASON_INPUT_LOST if kind is DeviceKind.INPUT else REASON_OUTPUT_LOST
                )
            elif observation.health is DeviceHealth.BUSY:
                messages.append(REASON_BUSY_ELSEWHERE)
            elif observation.health is DeviceHealth.PERMISSION_DENIED:
                messages.append(REASON_PERMISSION_DENIED)
            elif observation.health is DeviceHealth.INCOMPATIBLE:
                messages.append(REASON_INCOMPATIBLE)
            elif observation.health is DeviceHealth.READY:
                messages.append(REASON_HEALTHY)
        if self._new_device_seen and observation.device_id is not None and observation.device_id != previous_id:
            messages.append(REASON_NEW_DEVICE_IGNORED)
        self._messages[kind] = tuple(messages) if messages else self._messages[kind]

        return {
            "kind": kind.value,
            "previous_health": previous.value,
            "health": observation.health.value,
            "epoch_changed": epoch_changed,
            "device_epoch": self._epoch[kind],
            "adopted_device_id": self._fixed[kind],
            "new_device_ignored": self._new_device_seen,
        }

    # ---------------------------------------------------------------- decisions

    def decide(
        self,
        *,
        turn_active: bool,
        service_active: bool,
        awaiting_confirmation: bool,
    ) -> DeviceAction:
        """在**安全空闲点**给出该做什么；任何忙/确认中的状态一律延后。"""

        turn_active = _require_bool("turn_active", turn_active)
        service_active = _require_bool("service_active", service_active)
        awaiting_confirmation = _require_bool("awaiting_confirmation", awaiting_confirmation)

        self._text_mode_available = True
        input_health = self._health[DeviceKind.INPUT]
        output_health = self._health[DeviceKind.OUTPUT]
        healthy = input_health is DeviceHealth.READY and output_health is DeviceHealth.READY
        messages: list[str] = list(self._messages[DeviceKind.INPUT]) + list(
            self._messages[DeviceKind.OUTPUT]
        )
        self._messages = {DeviceKind.INPUT: (), DeviceKind.OUTPUT: ()}

        if healthy:
            return self._action(
                action=ACTION_NONE,
                reason=REASON_HEALTHY,
                healthy=True,
                safety_point=not (turn_active or service_active or awaiting_confirmation),
                messages=tuple(dict.fromkeys(messages)),
            )

        # 不安全点：延后（设备异常本身不改变任何授权/计划事实）。
        if awaiting_confirmation:
            return self._action(
                action=ACTION_NOTIFY_USER,
                reason=REASON_DEFERRED_AWAITING_CONFIRMATION,
                healthy=False,
                safety_point=False,
                messages=tuple(dict.fromkeys((*messages, REASON_DEFERRED_AWAITING_CONFIRMATION, REASON_TEXT_MODE_KEPT))),
            )
        if turn_active or service_active:
            return self._action(
                action=ACTION_NOTIFY_USER,
                reason=REASON_DEFERRED_ACTIVE_TURN,
                healthy=False,
                safety_point=False,
                messages=tuple(dict.fromkeys((*messages, REASON_DEFERRED_ACTIVE_TURN, REASON_TEXT_MODE_KEPT))),
            )

        # 安全空闲点：**输出优先**（先救播放，再救收音）。
        for kind, action_name in (
            (DeviceKind.OUTPUT, ACTION_RECONNECT_OUTPUT),
            (DeviceKind.INPUT, ACTION_RECONNECT_INPUT),
        ):
            health = self._health[kind]
            if health in _NO_AUTO_RETRY:
                messages.append(
                    {
                        DeviceHealth.BUSY: REASON_BUSY_ELSEWHERE,
                        DeviceHealth.PERMISSION_DENIED: REASON_PERMISSION_DENIED,
                        DeviceHealth.INCOMPATIBLE: REASON_INCOMPATIBLE,
                    }[health]
                )
                continue
            if health in _RECONNECTABLE:
                fixed = self._fixed[kind]
                if fixed is None:
                    messages.append(REASON_NO_FIXED_DEVICE)
                    continue
                return self._action(
                    action=action_name,
                    reason=REASON_RECONNECT_ATTEMPTED,
                    healthy=False,
                    safety_point=True,
                    open_request=DeviceOpenRequest(
                        kind=kind,
                        health=health,
                        reason=REASON_RECONNECT_ATTEMPTED,
                        preferred_device_id=fixed,
                    ),
                    messages=tuple(dict.fromkeys((*messages, REASON_RECONNECT_ATTEMPTED, REASON_TEXT_MODE_KEPT))),
                )

        # 没有可自动重连的设备：如实提示 + 保持文字模式。
        unknown_present = (
            input_health is DeviceHealth.UNKNOWN
            or output_health is DeviceHealth.UNKNOWN
        )
        if unknown_present:
            messages.append(REASON_DEVICE_UNKNOWN)
        return self._action(
            action=ACTION_FALLBACK_TO_TEXT,
            reason=REASON_TEXT_MODE_KEPT,
            healthy=False,
            safety_point=True,
            messages=tuple(dict.fromkeys((*messages, REASON_TEXT_MODE_KEPT))),
        )

    def note_attempt(self, kind: DeviceKind, *, succeeded: bool) -> DeviceHealth:
        """登记一次重连结果；成功即恢复 READY 并把 epoch 推进到下一轮生效。"""

        succeeded = _require_bool("succeeded", succeeded)
        self._attempts += 1
        if succeeded:
            self._health[kind] = DeviceHealth.READY
            self._epoch[kind] = self._epoch[kind] + 1
        else:
            self._messages[kind] = (*self._messages[kind], REASON_RECONNECT_FAILED)
        return self._health[kind]

    def note_device_switch(self, kind: DeviceKind, *, device_id: object) -> None:
        """用户手动切换设备：**只在下一轮生效**，这里只更新固定目标。"""

        if device_id is None:
            raise ContractError("device_id 不得为 None")
        self._fixed[kind] = device_id
        self._epoch[kind] = self._epoch[kind] + 1

    # ---------------------------------------------------------------- internals

    def _action(
        self,
        *,
        action: str,
        reason: str,
        healthy: bool,
        safety_point: bool,
        messages: tuple[str, ...],
        open_request: DeviceOpenRequest | None = None,
    ) -> DeviceAction:
        return DeviceAction(
            action=action,
            reason=reason,
            input_health=self._health[DeviceKind.INPUT],
            output_health=self._health[DeviceKind.OUTPUT],
            already_ready=healthy,
            safety_point_reached=safety_point,
            text_mode_available=self._text_mode_available,
            open_request=open_request,
            messages=messages,
            device_epoch=max(self._epoch[DeviceKind.INPUT], self._epoch[DeviceKind.OUTPUT]),
        )


__all__ = [
    "ACTION_FALLBACK_TO_TEXT",
    "ACTION_NONE",
    "ACTION_NOTIFY_USER",
    "ACTION_RECONNECT_INPUT",
    "ACTION_RECONNECT_OUTPUT",
    "DEVICE_KINDS",
    "REASON_BUSY_ELSEWHERE",
    "REASON_DEFERRED_ACTIVE_TURN",
    "REASON_DEFERRED_AWAITING_CONFIRMATION",
    "REASON_DEFERRED_NOT_IDLE",
    "REASON_DEVICE_UNKNOWN",
    "REASON_HEALTHY",
    "REASON_INCOMPATIBLE",
    "REASON_INPUT_LOST",
    "REASON_NEW_DEVICE_IGNORED",
    "REASON_NO_FIXED_DEVICE",
    "REASON_OUTPUT_LOST",
    "REASON_PERMISSION_DENIED",
    "REASON_RECONNECT_ATTEMPTED",
    "REASON_RECONNECT_FAILED",
    "REASON_TEXT_MODE_KEPT",
    "DeviceAction",
    "DeviceHealth",
    "DeviceKind",
    "DeviceObservation",
    "DeviceOpenRequest",
    "DeviceRecoveryError",
    "DeviceRecoveryPolicy",
]
