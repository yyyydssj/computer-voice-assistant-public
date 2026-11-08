# P10 批次 R1a 交接：两阶段打断状态机（纯离线）

> 日期：2026-10-02
> 批次：P10 R1a（阶段 R1「两阶段打断」的第一半）
> 前置：P9 complete（`2026-10-01-P9-stage-acceptance.md`）、P0–P9 整体验收（`2026-10-02-P0-P9-stage-acceptance.md`，基线 2087）
> 设计依据：`implementation_plan/12-barge-in-and-aec.md`「已确认设计：两阶段打断」；调优范围见 `13-device-tuning.md`

## 1. 本批目标与不做

**目标**：把"播放期间用户开口 → 先降音量 → 持续可信人声才确认打断"的**状态与时间语义**做成纯离线、可注入、可确定性验证的核心，为后续接线与真机调优提供冻结点。

**本批不做**：
- 不碰麦克风/扬声器/真实设备，不接 AEC/降噪（R2a 负责降级框架）；
- 不改 `app/runtime/auto_voice.py` 的会话循环（R4 负责接线）；不改 `app/safety`/`app/executor`/`app/plans`；
- 不做真实 barge-in 效果承诺（真机调优见 `13-device-tuning.md`，另行排期）。

## 2. 交付物

| 文件 | 内容 |
|---|---|
| `app/duplex/barge_in.py`（新增，约 480 行） | `BargeInConfig`（冻结参数 + fail-closed 校验）、`BargeInObservation`、`BargeInState`（idle/suspected/confirmed）、`BargeInController`（`observe()` / `release()` / `reset()` / `pre_roll()`）、`BargeInDecision`（含 `to_details()` 只读诊断）、`BargeInPreRoll`、`BargeInError` |
| `app/duplex/__init__.py`（新增） | 公开导出上述类型与固定短代码 |
| `tests/duplex/__init__.py`（新增） | 测试包标识（与 `tests/*` 其它目录一致） |
| `tests/duplex/test_barge_in_state.py`（新增，**56 项**） | 见 §3 |

## 3. 测试覆盖（56 项）

| 测试类 | 覆盖点 |
|---|---|
| `BargeInConfigTests`（10） | 默认值落在设计区间（≤100 ms 降音量 / 20~30% 音量 / ≥300 ms 前置 / 200~300 ms 确认窗口）；容量换算；非法参数拒绝（前置低于 300 ms、音量=1.0、确认阈值低于疑似阈值、降级阈值低于确认阈值、NaN） |
| `BargeInObservationTests`（8） | 时间戳（负数/布尔）、概率与相关性取值范围、布尔字段类型、队列毫秒、`in_playback` 语义（队列有音频即算在播） |
| `BargeInStageOneTests`（11） | 首帧疑似即降音量且期限=100 ms、音量落在 20~30%、疑似期间**不建立轮次**、安静帧保持原音量、期限递减且不为负、非单调时间戳拒绝、前置缓冲容量与不丢帧 |
| `BargeInStageTwoTests`（9） | 持续人声才确认、确认时刻落在 200~300 ms、单次短噪声不确认、确认态终态、误触发 `release()` 平滑恢复原音量且不建轮次、`release()` 清空确认窗口、`reset()` 语义 |
| `BargeInPlaybackWindowTests`（3） | 播放结束立即恢复且回 idle（即使处于疑似）、队列尚有音频时窗口保持打开、没有音频时不判定打断 |
| `BargeInDegradationTests`（7） | 回声相关性折减人声得分、回声主导不判疑似、缺参考时从 `full` 降级为 `strict`、`strict` 提高确认门槛（0.8 不确认、0.98 确认）、非法降级级别拒绝、降级只提高门槛 |
| `BargeInDecisionDetailsTests`（3） | `to_details()` 只含数值/短代码、无线索泄漏字段（pcm/payload/text 等）、帧计数 |
| `BargeInBoundaryTests`（5） | 模块不导入设备/文件/网络/线程（AST 禁用根）、不导入安全链模块（AST）、确认态只建轮次**绝不**带授权字段、50 帧耗时远低于 100 ms 期限 |

## 4. 关键设计决定（可复核）

1. **疑似 ≠ 轮次**：`BargeInDecision.user_turn_created` 只在 `CONFIRMED` 为真；疑似阶段只改 `level`，不产生任何输入/授权事实。
2. **降级只提高门槛**：`BargeInConfig.confirm_threshold_for()` 对 `strict` 返回更高阈值；`DEGRADE_FULL` 却拿不到参考相关性时**自动降级**为 `DEGRADE_STRICT`，绝不把"未知"当"已确认"。
3. **回声风险折减而非归零**：`score = max(0, voice_probability - echo_correlation)`，保留人声特征作用的同时压低回声误判。
4. **前置缓冲容量 = ⌈pre_roll_ms/frame⌉ + 1**：疑似触发那一帧也占位，保证触发后仍能取出 ≥ `pre_roll_ms` 的音频；`duration_ms` 按"帧数 × 帧时长"计（不是首末帧跨度），确认窗口（250 ms）早于前置（300 ms）时不会丢帧。
5. **时间语义确定性**：观察带 `at_ms`，控制器只按帧时长与时间戳推进，不使用墙上时钟（唯一例外是边界测试里对 50 帧处理耗时的上界断言）。
6. **纯离线硬边界**：只导入 stdlib 与 `app.contracts.base`，不导入 `app.audio.*`（避免拉起音频依赖），无文件/网络/设备/线程面。

## 5. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests/duplex -t . -p "test_*.py" -q   → Ran 56,  OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q          → Ran 2143, OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools                              → exit 0
```

开发过程中真被测试抓到的两个问题（已修，留痕）：
- 前置缓冲容量按"纯时长除法"算，导致触发后取不足 300 ms → 改为 `⌈ms/frame⌉ + 1`；
- 前置时长用首末帧跨度会把每帧少算一帧时长 → 改为帧数 × 帧时长。

## 6. 全量与下一步

- 本批后全量基线：**2143 tests OK (skipped=3)**（2087 + 56，实测 112.451s；`skipped=3` 仍为既定门禁三项）、`compileall -q app tests tools` exit 0。
- **R1b（下一批）**：确认后的编排语义——ducking/恢复阶梯、前置缓冲与后续音频合并为同一 `utterance_id`、旧回复剩余文本不再提交、安全确认提示被打断即失效、`restore_ms` 平滑恢复曲线。
- R2a（AEC 降级框架）、R3（设备恢复）、R4（会话循环接线）按顺序推进；真机调优（`13-device-tuning.md`）与 AEC 选型（R2b）另行排期。
- 本批改动**未提交、未 push**。
