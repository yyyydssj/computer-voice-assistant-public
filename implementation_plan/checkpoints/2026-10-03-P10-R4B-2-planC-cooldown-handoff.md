# P10 批次 R4-B-2 收口交接：真 AEC 结论 + 方案 C 冷却窗口（`barge_in_cooldown`）

- 建立时间：2026-10-03
- 上游：`research/2026-10-03-P10-R4B-2-doubletalk-findings.md`（阶段 0 的 go/no-go 证据）、
  `checkpoints/2026-10-03-P10-R4B-1-aec3-backend-handoff.md`（AEC3 后端）
- 恢复指引：`checkpoints/2026-10-02-P10-R4A-handoff-resume.md` §9（新会话 5 步）

---

## 1. 一句话结论

**真 AEC3 不接进会话**（阶段 0 判定"条件性 no-go"：本机回声/用户语音比下，AEC3 把用户语音
压 5.5~20.5 dB，残差里的用户语音经常低于 600 能量门）；作为兜底，**方案 C 冷却窗口已落地**：
播放开始后 `cooldown_ms` 内收帧、填前置缓冲，但**不降音量、不建立疑似/确认**，
如实记 `barge_in_cooldown`。**R4-A 的 fail-closed 口径未改**：没证明"看得见自己的回声"
仍然不真停播。默认 `cooldown_ms=0` → 不显式开启时行为与之前完全一致。

---

## 2. 阶段 0（离线 go/no-go）结论

| 档位（回声/用户语音比） | 双讲用户语音增益 | 残差用户语音 RMS vs 600 门 |
|---|---|---|
| 回声 ≥ 用户（+7.6 / +8.1 dB） | **−5.5 ~ −20.5 dB** | 93–522（**5/6 低于门**） |
| 中间档（≈0 ~ +3 dB） | −5.1 ~ −12.8 dB | 449–1632（2/4 低于门） |
| 用户 ≫ 回声（≈−10 dB） | −1.6 ~ −5.9 dB | 1499–2468（高于门） |

稳定事实：`near_only` 12/12 次 −0.00 dB（无参考能量时 AEC3 不碰用户语音）；`far_only` ERLE
15.7–37.9 dB；同场景分段增益波动 −1.8 ~ −37.7 dB（不稳定）；NLMS 对照保住语音但 ERLE 8.85 dB（不达标）。
工具：`tools/p10_r4b2_doubletalk_probe.py`；证据：`research/2026-10-03-P10-R4B-2-doubletalk-findings.md`。

---

## 3. 方案 C 交付物（`barge_in_cooldown`）

| 文件 | 改动 |
|---|---|
| `app/duplex/barge_in.py` | 新增 `REASON_COOLDOWN="barge_in_cooldown"`；`BargeInConfig.cooldown_ms`（**默认 0**，非负整数校验）；`BargeInController` 新增冷却起点/帧计数 + `cooldown_frames`/`cooldown_remaining_ms`/`cooldown_ms` 属性；`observe()` 在 **IDLE 且播放中**时先判冷却（收帧 + 填前置缓冲 + 不降音量 + 清空确认窗口）；`playback_ended` 与 `reset()` 清冷却起点（新 episode 重新计时）；`BargeInDecision` 增 `cooldown_remaining_ms`/`cooldown_frames`（**带默认值，向后兼容**） |
| `app/duplex/barge_in_channel.py` | `BargeInChannelConfig.cooldown_ms: int \| None = None`：**只**用 `dataclasses.replace` 覆盖冷却时长，其余字段原样保留（避免重演"重建配置把 `min_suspect_frames=3`/`release_ms=800` 覆盖回 1/400"的真机缺陷）；新增 `cooldown_ms`/`cooldown_remaining_ms`/`cooldown_frames` 属性；两处 `to_details()` 增冷却字段 |
| `app/runtime/auto_voice.py` | `AutoVoiceConfig.barge_in_cooldown_ms`（默认 0，非负整数校验）；`_build_barge_in` 仅在 `> 0` 时下发 `channel_kwargs["cooldown_ms"]`（**不重建 BargeInConfig**）；`barge_in_details()` 增 `cooldown_ms`/`cooldown_remaining_ms`/`cooldown_frames` |
| `app/main.py` | 新增 CLI `--barge-in-cooldown-ms`（默认 0，建议 1000）；启动提示补冷却窗口与"尚未证明能看见自己的回声（echo_proven=false），本机可能不具备打断条件；建议戴耳机或改用外接麦克风" |
| `tests/duplex/test_barge_in_cooldown.py`（新，**28 项**） | 配置校验（0/正/负/bool）、窗口内不疑似不降音量、窗口后恢复判定、前置缓冲不丢、剩余毫秒倒计时、episode 重新计时、`reset` 清窗口、窗口后仍能确认、`not in_playback` 不误记冷却、queued 音频算播放中、详情含冷却且无音频、`BargeInDecision` 向后兼容、通道"只覆盖冷却"、会话装配真的下发且不覆盖 3/800、`AutoVoiceConfig` 校验 |

**语义边界（不要误解）**：
- 冷却**不改变** fail-closed：本机仍 `strict_no_aec`、`echo_proven=false` → 不会真停播；
- 冷却**不丢音频**：窗口内的帧照样进前置缓冲；
- 冷却**只影响 IDLE**：已经在疑似/确认态时不会再回到冷却（不会打断既成事实）；
- 冷却时长对齐 LiveKit `backchannel_boundary` 的 1.0 s 建议值（本机未真机验证）。

---

## 4. 验证

| 项 | 结果 |
|---|---|
| 新模块专项 | `tests.duplex.test_barge_in_cooldown` **28 项 OK** |
| 打断/会话相关专项 | `test_barge_in_cooldown` + `test_barge_in_state` + `test_barge_in_channel` + `test_barge_in_orchestration` + `test_barge_in_safety` + `test_auto_voice_barge_in` + `test_main_auto_voice` 合计 **290 项 OK**（96.5 s） |
| **全量** | **Ran 2553 tests in 220.626s → OK (skipped=3)**，exit 0（2525 → 2553，+28） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 328 个 .py：**0 解码失败**、BOM **25**（与基线一致）；本批文件无 BOM、纯 LF |
| CLI | `app.main --help` 能解析 `--barge-in-cooldown-ms` |

---

## 5. 未做 / 已知边界（**不得伪造**）

1. **未做真机**：冷却窗口、提示文案、以及"接 AEC3 会不会更好"都**没有**真机数据；
   阶段 0 用的是合成回声路径。
2. **未实现"更严门槛"档**：本批只落地冷却窗口；研究文档 §4.C 里的"更严门槛（提高确认帧数/能量门）"
   与"半双工提示 UI"未做（启动提示文字已加）。
3. **AEC3 仍未接线**（`--aec-engine` 开关未做）：R4-B-1 的后端保持默认关闭。
4. 会话级"实测 ERLE 达标才许声称已消除回声"的门未做（前提是真机能用）。
5. 耳机/外接麦克风场景（预期落在"用户 ≫ 回声"档）**未测**。

---

## 6. 回滚

- 一键关闭：不传 `--barge-in-cooldown-ms`（默认 0）→ 状态机 `cooldown_ms=0` 分支等价于改动前；
- 代码回滚：`git revert <本批提交>` 或 `git reset --hard <本批前提交>`（本批前 = `0b9e053`）。

---

## 7. 下一步（按性价比）

1. **真机（需用户在场 + 授权真实模型）**：量本机"回声/用户语音比"与冷却窗口的实际效果；
   若真机落在"用户 ≫ 回声"档，再评估接线 AEC3（先测双讲削不削用户语音）。
2. **方案 C 剩余项**：更严门槛档（确认帧数/能量门）与半双工提示 UI。
3. **R5/R6**（Provider 冷却、长期运行健康）仍未做。
