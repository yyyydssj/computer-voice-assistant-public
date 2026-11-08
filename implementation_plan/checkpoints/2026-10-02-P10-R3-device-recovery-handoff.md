# P10 批次 R3 交接：音频设备会话与安全空闲点恢复（纯离线决策层）

> 日期：2026-10-02
> 批次：P10 R3（R3a 设备会话状态 + R3b 恢复策略 + R3c 降级与提示）
> 设计依据：`implementation_plan/13-device-tuning.md`、路线图 P10 §实现批次 4 与 §退出条件
> 用户本轮决定：① 输出设备断开时**停止当前播报并回退到文字**；② 重连**仅在下一个安全空闲点各试一次**

## 1. 本批目标与不做

**目标**：把"设备断开/重连/该不该现在恢复"做成**纯离线、可复核的决策层**，落实路线图三条硬要求：
设备恢复**只在安全空闲点**启用、**不自动切换到未知设备**、**设备切换只在下一轮生效**；
设备异常时**保留文字模式**且**不改变执行计划**。

**本批不做**：
- 不打开设备、不做任何 I/O（本批**完全不碰真机**）；
- 不改 `app/runtime/auto_voice.py`（把策略接进会话循环是 **R4** 的事）；
- 不改 `app/safety`/`app/executor`/`app/plans`（设备事件不得产生或失效授权事实）。

## 2. 交付物

| 文件 | 内容 |
|---|---|
| `app/devices/session.py`（新增，**441 行**） | `DeviceRecoveryPolicy`（`register`/`decide`/`note_attempt`/`note_device_switch`）、`DeviceObservation`、`DeviceHealth`（`unknown/ready/disconnected/busy/permission_denied/incompatible`）、`DeviceKind`、`DeviceAction`、`DeviceOpenRequest`、`DeviceRecoveryError` + 固定短代码常量 |
| `app/devices/__init__.py`（新增，66 行） | 公开面导出 |
| `tests/devices/test_device_recovery.py`（新增，351 行，**34 项**） | 见 §3 |
| `tests/devices/__init__.py`（新增） | 测试包标识 |

## 3. 测试覆盖（34 项）

| 测试类 | 覆盖点 |
|---|---|
| `DeviceObservationTests`（4） | 未知设备种类/非法健康值/零 epoch 拒绝；`to_details()` 无音频与授权字段 |
| `DeviceAdoptionTests`（4） | 首次 READY 设备被采用；**枚举到新设备一律不自动采用**（只记 `new_device_seen`）；手动切换更新固定目标并推进 epoch；切换必须给设备 |
| `DeviceHealthDecisionTests`（11） | 双 READY 才算健康（只有输入或只有输出 READY 都不算）；**从未确认过 ≠ 正常**；输出断开→`reconnect_output`；输入断开→`reconnect_input`；**输出优先于输入**；`busy`/`permission_denied`/`incompatible` **不自动重试**；没有固定目标时明确报 `no_fixed_device` |
| `DeviceSafetyPointTests`（5） | 轮次中/服务中/等待确认**一律延后**（`notify_user`，`safety_point_reached=False`）；任何状态都**保留文字模式**；标志位必须布尔 |
| `DeviceAttemptTests`（4） | 成功重连→READY 且 epoch+1；失败保持断开并可再试；失败在提示里留痕；成功标志必须布尔 |
| `DeviceBehaviourDetailsTests`（4） | 决策详情含安全点与打开指令；提示词是**固定短代码**（无空格、全小写）；**同一条提示不重复刷**；详情无授权面 |
| `DeviceModuleBoundaryTests`（3） | AST：不导入设备/网络/线程/文件库；不导入安全链（含 `app.recovery`）；包导出公开面 |

## 4. 关键设计决定

1. **`UNKNOWN` 与 `READY` 严格区分**：设备"还没确认过"不等于"正常"；从未确认时不做任何"已恢复"判断，提示 `device_unknown`。
2. **恢复只在安全空闲点**：`turn_active` / `service_active` / `awaiting_confirmation` 任一为真 → 只 `notify_user` 并延后，**绝不在用户正在输入、查看方案或授权时抢设备**。
3. **输出优先于输入**：先救播放通道（用户能听到反馈），再救收音。
4. **只重连固定设备**：`DeviceOpenRequest.preferred_device_id` 只能是用户固定的那个；枚举到不同 id 只记事实、不改目标（不切未知设备）。
5. **忙/权限拒绝/不兼容不自动重试**：这类是环境问题，自动重试只会刷屏；如实提示并保持文字模式。
6. **不静默**：每个决策都带固定短代码（`output_lost`/`deferred_active_turn`/`reconnect_failed`/`text_mode_kept`…）；同一提示不会在连续 `decide()` 中重复。
7. **重连成功 = epoch 推进**：恢复后 epoch+1，让下游（`device_epoch` 陈旧帧拒绝、AEC 参考源）自然把旧帧作废；**设备切换只在下一轮生效**由 `note_device_switch` 的 epoch 推进表达。

## 5. 三条硬不变量（有测试断言）

1. **恢复 ≠ 授权**：模块不导入安全链（AST 断言），任何设备事件都不产生/失效授权、计划或执行事实；
2. **不切未知设备**：新设备只登记不采用（测试断言固定目标不变）；
3. **不静默**：所有延后/降级/未恢复都有固定短代码与可读提示，`text_mode_available` 恒为真。

## 6. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests/devices -t . -p "test_*.py" -q → Ran 34, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q         → Ran 2335, OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools                              → exit 0
```

## 7. 下一步

- **R4 会话循环接线**（下一步正题）：把本策略 + R2b 旁路参考 + R1 打断状态机接进 `app/runtime/auto_voice.py`：
  - 独立**打断通道 + 安全门**（**不能**复用 `interrupt_source`，那是"中止整个会话"的轮询口）；
  - 用独立输入设备时按本策略的 `decide()` 取"现在能不能重连"；
  - 播放侧断开时按用户决定**停止当前播报并回退文字**（`ACTION_FALLBACK_TO_TEXT`）。
- 之后 **R5** Provider 冷却/有界探测、**R6** 长期运行健康。
- 本批改动**未提交、未 push**。
