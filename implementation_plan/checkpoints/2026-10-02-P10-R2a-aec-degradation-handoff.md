# P10 批次 R2a 交接：AEC/降噪逐层降级框架（纯离线）

> 日期：2026-10-02
> 批次：P10 R2a（阶段 R2「AEC/降噪」的第一半；真实选型与真机实测 R2b 另行排期）
> 设计依据：`implementation_plan/12-barge-in-and-aec.md`「AEC、降噪与降级顺序」、`discussions/pending-decisions.md#H.7`（2026-09-xx 已定稿）

## 1. 本批目标与不做

**目标**：把"AEC/降噪不可用或不足时如何**如实降级**"做成可复核的离线判定——决定当前条件允许用哪一级处理链、为什么、以及必须遵守的边界，供后续真实 AEC 接入时直接消费。

**本批不做**：
- 不实现任何真实音频处理（不做 AEC/NS 算法、不接 WebRTC APM、不加载模型）；
- 不访问设备、不测延迟、不做真机回声实测（R2b）；
- 不碰会话循环与播放链路（R4）。

## 2. 交付物

| 文件 | 内容 |
|---|---|
| `app/audio/aec/__init__.py`（新增，约 220 行） | `AecDegradationController`（`plan()`）、`AecCapabilityProbe`（能力/参考/设备变化/回声路径/epoch）、`AecPolicy`（NS 可用、延迟估计可信、回声预算）、`AecProcessingPlan`（级别/原因/是否应用 AEC/降级字符串/是否暂停收音/是否保留实时与文字/epoch）、`AecError` |
| `tests/duplex/test_aec_degradation.py`（新增，**34 项**） | 见 §3 |

## 3. 处理链级别与降级阶梯（可复核）

```text
aec_ns        （AEC + NS 全链，reference 匹配输出，延迟估计可信）
   ↓ 缺 NS
aec_only      （有 AEC 无 NS）
   ↓ 参考缺失 / 设备变化需重校准 / 回声路径超出 AEC 预算
strict_no_aec （无 AEC：更严格的持续人声阈值，保留实时会话）
   ↓ 回声路径过长且无 AEC / 延迟估计不可靠
half_duplex   （播放期间暂停收音的兜底；仍不关闭会话、保留文字模式）
```

| 级别 | `barge_in_degradation` | 暂停播放期间收音 | 实时会话可用 | 应用 AEC |
|---|---|---|---|---|
| `aec_ns` | `full` | 否 | 是 | 是 |
| `aec_only` | `full` | 否 | 是 | 是 |
| `strict_no_aec` | `no_aec` | 否 | 是 | 否 |
| `half_duplex` | `strict` | **是** | 否 | 否 |

原因码（固定短代码）：`full_chain_available`、`noise_suppression_unavailable`、`echo_path_exceeds_aec_budget`、`speaker_reference_missing`、`device_changed_recalibration_required`、`echo_risk_without_safe_delay`、`device_delay_unreliable`。

## 4. 测试覆盖（34 项）

| 测试类 | 覆盖点 |
|---|---|
| `AecPolicyTests`（4） | 默认值、零预算拒绝、非布尔标志拒绝 |
| `AecProbeTests`（5） | 未知能力拒绝、负数/非整数回声路径拒绝、参考标志类型、设备 epoch 必须为正 |
| `AecFullChainTests`（5） | 硬件/软件 AEC 全链、缺 NS 退 `aec_only`、零回声路径仍全链、`to_details()` 字段白名单（无 pcm/payload/audio/path） |
| `AecDegradationLadderTests`（11） | 无 AEC → `strict_no_aec`；参考缺失/不匹配 → strict；**有 AEC 但回声路径超预算 → strict（不谎称已消除回声）**；无 AEC 且路径过长 → half_duplex；延迟估计不可靠 → half_duplex；设备变化重校准；epoch 变化跨次判定；同 epoch 稳定；无 AEC 无回声风险仍降级；缺能力绝不升到全链；类型校验 |
| `AecHardInvariantTests`（6） | 所有级别都保留文字模式；未应用 AEC 绝不声称已消除回声；`half_duplex` 只与"暂停收音"同时出现；降级字符串限于 R1 词表；原因码白名单；返回类型稳定 |
| `AecBoundaryTests`（2） | AST：不导入设备/文件/网络/线程/音频库（含 numpy）、不导入安全链 |

## 5. 关键设计决定

1. **不伪装**：`aec_applied=False` 时 `echo_cancellation_claimed` 必为 False；`noise_suppression_applied` 也随之为 False。
2. **降级不关闭会话**：即使 `half_duplex` 仍 `text_mode_available=True`；实时不可用只体现在 `realtime_conversation_available=False` 与暂停收音，不产生任何"拒绝服务"式后果。
3. **延迟估计可信是 AEC 的前置条件**：估计不可靠时无法对齐参考信号 → 直接退半双工（即使 AEC 存在）。这是开发中测试抓到的顺序缺陷（原实现会先判全链）。
4. **有 AEC 但回声路径超出预算时不放行全链**：新增专用原因码 `echo_path_exceeds_aec_budget` 并退到 `strict_no_aec`（收紧打断灵敏度），而不是继续声称"已消除回声"。
5. **设备变化要重校准**：`device_changed` 或 `device_epoch` 跨次变化 → `strict_no_aec` + 专用原因码；同 epoch 保持稳定判定。
6. **纯离线硬边界**：只导入 stdlib 与 `app.contracts.base`、`app.duplex.barge_in`（取降级词表），不导入设备/音频库/安全链。

## 6. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests/duplex -t . -p "test_*.py" -q   → Ran 130（含本批 34）, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q          → 见 R1b 交接 §6 / progress.md
.\.venv\Scripts\python.exe -m compileall -q app tests tools                              → exit 0
```

## 7. 下一步

- **R2b（另行排期）**：真实 AEC 选型（WebRTC APM vs 操作系统/硬件 AEC）、参考帧对齐与设备延迟实测、键盘/咳嗽/音乐/近讲远讲矩阵；需要真机与用户配合。
- **R3**：设备断开/重连/安全空闲点恢复（消费本模块的 `device_epoch`）。
- **R4**：会话循环接线（把 R1a/R1b 接进 `app/runtime/auto_voice.py`，独立打断通道 + 安全门）。
- 本批改动**未提交、未 push**。
