"""P10：音频设备会话与恢复策略（设备断开、重连、安全空闲点恢复）。

本包只做**决策**：什么时候允许重连、什么时候必须延后、该怎么如实提示。
它不打开设备、不做 I/O、不触碰授权与计划。
"""

from __future__ import annotations

from app.devices.session import (
    ACTION_FALLBACK_TO_TEXT,
    ACTION_NONE,
    ACTION_NOTIFY_USER,
    ACTION_RECONNECT_INPUT,
    ACTION_RECONNECT_OUTPUT,
    DEVICE_KINDS,
    REASON_BUSY_ELSEWHERE,
    REASON_DEFERRED_ACTIVE_TURN,
    REASON_DEFERRED_AWAITING_CONFIRMATION,
    REASON_DEFERRED_NOT_IDLE,
    REASON_DEVICE_UNKNOWN,
    REASON_HEALTHY,
    REASON_INCOMPATIBLE,
    REASON_INPUT_LOST,
    REASON_NEW_DEVICE_IGNORED,
    REASON_NO_FIXED_DEVICE,
    REASON_OUTPUT_LOST,
    REASON_PERMISSION_DENIED,
    REASON_RECONNECT_ATTEMPTED,
    REASON_RECONNECT_FAILED,
    REASON_TEXT_MODE_KEPT,
    DeviceAction,
    DeviceHealth,
    DeviceKind,
    DeviceObservation,
    DeviceOpenRequest,
    DeviceRecoveryError,
    DeviceRecoveryPolicy,
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
