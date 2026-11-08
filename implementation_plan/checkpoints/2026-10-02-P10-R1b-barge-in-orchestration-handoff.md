# P10 批次 R1b 交接：两阶段打断的编排语义（平滑恢复、轮次交接、提示失效）

> 日期：2026-10-02
> 批次：P10 R1b（阶段 R1「两阶段打断」的第二半）
> 前置：R1a（`2026-10-02-P10-R1a-barge-in-state-handoff.md`，全量 2143）
> 设计依据：`implementation_plan/12-barge-in-and-aec.md`（第一阶段降音量 / 第二阶段确认 / 安全确认例外）、`discussions/pending-decisions.md#H.7`

## 1. 本批目标与不做

**目标**：把 R1a 的判定状态机接成**可编排的播放侧语义**——降音量与平滑恢复的阶梯、确认后停止播报与轮次交接、以及"确认提示被打断即失效"。

**本批不做**：
- 不接真实音频设备/播放链路（R4 负责接入会话循环）；
- 不改 `app/runtime/auto_voice.py`、不改 `app/safety/**`（提示失效只以**策略对象**表达，安全链后续按需消费）；
- 不实现真实 AEC（R2a 已交付降级框架，真实选型与实测另排期）。

## 2. 交付物

| 文件 | 内容 |
|---|---|
| `app/duplex/barge_in_orchestration.py`（新增，约 390 行） | `BargeInPlaybackController`（`play_frame()` / `playback_ended()` / `mark_prompt_completed()` / `reset()` / `take_pre_roll()`）、`BargeInOutcome`（含 `to_details()`）、`PromptPolicy`、`BargeInOrchestrationError`、固定短代码常量 |
| `app/duplex/barge_in.py`（修改） | 新增 `take_pre_roll()`、`mark_confirmed()`；`BargeInConfig` 新增 `release_ms`（疑似回落滞后，默认 400 ms）；疑似态新增**回落分支**（此前安静帧永不退出疑似态，是真缺陷）；`voice_probability` 校验区间修正为 `[0,1]`（此前把 `0.0` 当非法） |
| `app/duplex/__init__.py`（修改） | 导出 R1b 公开面 |
| `tests/duplex/test_barge_in_orchestration.py`（新增，**40 项**） | 见 §3 |

## 3. 测试覆盖（40 项）

| 测试类 | 覆盖点 |
|---|---|
| `PromptPolicyTests`（6） | 默认提示有效、失效后不可授权、已完成不要求重播、未知状态拒绝、`replay_required` 类型、`to_details()` 不含授权字段 |
| `PlaybackVolumeTests`（7） | 安静播放保持原音量、首个疑似帧立即降音量、连续疑似音量稳定、时长按帧号、非法帧号/类型拒绝、`to_details()` 字段白名单 |
| `RestoreCurveTests`（6） | 误触发按 `restore_ms` **平滑**回升（严格单调、不跳变）、`release_ms` 滞后保护期内保持降音量、恢复恰好占 `fade_steps` 帧、恢复期原因码 `restoring_volume`、恢复期**不**建轮次、播放自然结束立即恢复 |
| `ConfirmedBargeInTests`（9） | 持续人声确认后停止播放、**旧回复永不再续提交**（`resume_reply_submission`）、确认态粘滞、只交出一个 `utterance_id`、前置缓冲交出并入同一轮、确认结果不含授权字段、单次短噪声不确认、队列有音频时窗口保持 |
| `PromptInvalidationTests`（8） | 插话让有效提示失效并要求完整重播、失效提示不可授权、仅疑似不失效、重播不完整保持失效、重播完成清空要求、已完成提示不被后续插话撤销、标志类型校验、`reset()` 清空 |
| `OrchestrationBoundaryTests`（3） | AST：不导入设备/文件/网络/线程、不导入安全链、包导出 R1b 公开面 |

## 4. 关键设计决定

1. **动作与原因由状态机决定，音量只描述结果**：早期实现按"音量<1"覆盖成 DUCK，掩盖了恢复态（测试抓到后修正）。
2. **疑似回落必须有滞后**：`release_ms`（默认 400 ms）内保持降音量，之后才判定误触发；否则噪声一帧就会抖回原音量。回落时清空确认窗口（不累积）。
3. **恢复是帧阶梯而非跳变**：从当前降级音量线性回升，`fade_steps = ⌈restore_ms/frame⌉`，10 帧（200 ms）到原音量。
4. **确认后旧回复彻底终止**：`continue_playback=False` 且 `resume_reply_submission=False` 为终态，直到 `reset()`；已生成的文字仍留在界面由上层决定（本层不删除文本）。
5. **提示失效是策略而非授权**：`PromptPolicy` 只表达"必须完整重播"，`authorization_possible` 只在提示有效时为真；本模块不导入、不产生任何授权事实。

## 5. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests/duplex -t . -p "test_*.py" -q   → Ran 130（R1a 56 + R1b 40 + R2a 34）, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q          → 见 §6
.\.venv\Scripts\python.exe -m compileall -q app tests tools                              → exit 0
```

开发过程中被测试抓到的 4 个真实缺陷（全部已修，留痕）：
1. `voice_probability=0.0` 被区间校验误拒（写出 `(0,1]` 而非 `[0,1)`）；
2. 疑似态**永不回落**：安静帧既不确认也不退出疑似，导致音量永久停在降级值；
3. 音量回升阶梯错误依赖状态机内部 level（状态机在疑似期不更新 level，导致永远 0.25）；
4. `play_frame` 用音量覆盖动作码，把恢复期标成 `suspected_ducking`。

## 6. 全量与下一步

- 本批后全量基线：**2217 tests OK (skipped=3)**（R1a 后 2143 + R1b 40 + R2a 34；实测见 `progress.md`）。
- **R2a 已同时交付**（`2026-10-02-P10-R2a-aec-degradation-handoff.md`）：AEC/降噪逐层降级框架。
- 下一步待授权：**R3**（设备断开/重连/安全空闲点恢复）、**R4**（会话循环接线：独立打断通道 + 安全门）、**R5/R6**（Provider 冷却、长期运行健康）；真机调优与真实 AEC 选型（R2b）另行排期。
- 本批改动**未提交、未 push**。
