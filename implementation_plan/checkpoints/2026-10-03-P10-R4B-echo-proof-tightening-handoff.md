# P10 R4-B 交接：**回声证明收紧**（同机假停播真机证据 + 修复）

- 建立时间：2026-10-03
- 触发：用户真机反馈"**一句话说一半，我没打断就停了**"
- 上游：`checkpoints/2026-10-03-P10-R4B-P1P2-aec-engine-residual-gate-handoff.md`、
  `research/2026-10-03-P10-R4B-playback-channel-selfcheck.md`

---

## 1. 真机证据（决定性）

最后一次真机会话（`instance 917e459f`，命令含 `--barge-in-mode confirm --aec-engine webrtc
--barge-in-cooldown-ms 1000`）：

| 回合 | spoken | 打断确认 | `echo_proven` | 最大相关 | 估计回声延迟 | `device_action` | end_reason |
|---|---|---|---|---|---|---|---|
| 2 | true | **true** | **true** | 0.6005 | **746 ms** | `skipped` | **barge_in_interrupted** |
| 3 | true | false | true | 0.7458 | **1589 ms** | none | silence_timeout |
| 4 | true | **true** | **true** | 0.7172 | **−780 ms** | **none** | **barge_in_interrupted** |

⇒ **没插话也被判成"被打断"**（turn 2/4），turn 4 的 `device_action=none` 表示安全门**没有拦**
⇒ 播报真的被停；两轮都只跑了 **16 帧（≈320 ms）**，与"说一半就停"完全吻合。

**根因**：`echo_proven` 旧规则**只看对齐相关性 ≥ 0.2**，不看延迟是否物理合理。本机相关性能到
0.60~0.75，但估计延迟出现 **746 / 1589 / −780 ms**（本机实测参考→采集延迟只有 ≈390–410 ms），
负值与 1.6 s 在物理上不可能 ⇒ 对齐匹配到了错误的东西（很可能是助手自己的语音/重复语句）。
伪相关骗过证明 ⇒ R4-A 安全门放行 ⇒ 假停播。

---

## 2. 修复（只收紧、不放松）

| 文件 | 改动 |
|---|---|
| `app/duplex/barge_in_channel.py` | `BargeInChannelConfig` 新增 `echo_proof_delay_min_ms=0`、`echo_proof_delay_max_ms=500`、`echo_proof_consecutive=3`、`echo_proof_delay_tolerance_ms=50`（含校验：min<max、非负、consecutive≥1）；新增 `_note_proof_estimate()`：**三条同时满足才证明** —— ① 对齐状态 `ok` 且相关性 ≥ 阈值；② 延迟落在物理窗口内；③ 连续 N 次成立且相邻延迟差 ≤ 容差。新增固定状态码 `PROOF_STATE_*`（`no_estimate` / `alignment_not_ok` / `correlation_below_threshold` / `delay_implausible` / `accumulating` / `echo_proven`），并在 `to_details()` 暴露 `echo_proof_state` / `echo_proof_run` / `echo_proof_delay_ms`；`arm()` 复位证明计数 |
| `tests/duplex/test_barge_in_echo_proof.py`（新，**15 项**） | 真机三条伪证明数据（0.6005/746、0.7172/−780、0.7458/1589）**永远不能**证明；三次稳定合理 → 3 次后证明；边界 0/500 通过、−1/501 拒绝；低相关/对齐非 ok → 拒绝；延迟乱跳重新计数；坏估计清零；已证明不撤销；详情无音频 |
| `tests/foundation/test_auto_voice_barge_in.py` | 4 个"已证明→可停播"路径的用例改为**显式注入证明替身**（`force_echo_proven()`，保留真实相关性统计），证明规则本身由新专项覆盖 |

**口径**：未证明时仍走 R4-A 的 fail-closed（只观测、不动手）；本批**只让"证明"更难拿到**，
不会让判定更敏感。

---

## 3. 验证

| 项 | 结果 |
|---|---|
| 新专项 | `tests.duplex.test_barge_in_echo_proof` **15 项 OK** |
| 打断相关合计 | 178 项 OK（echo_proof + channel + aec_engine + cooldown + auto_voice 全绿） |
| **全量** | **Ran 2600 tests in 234.990s → OK (skipped=3)**，exit 0（2585 → 2600，+15） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 333 个 .py：**0 解码失败**、BOM **25**（与基线一致）；新增/修改文件均无 BOM、纯 LF |

---

## 4. 未做 / 重要边界（**不得伪造**）

1. **本机是否还有假停播，未经真机复测**：本批只是把"伪相关骗过证明"这条路堵住一部分
   （拒绝不合理延迟 + 要求连续稳定），**不能保证**不再出现"延迟合理且稳定但实际是伪相关"的情形。
   ⇒ **在真机复测通过之前，本机应继续用 `--barge-in-mode off`**（不监视 ⇒ 不会假停播）。
2. 对齐估计本身没有修：为什么本机能给出 1589/−780 ms 的延迟（超出 R4-A 记录的 ±600 ms 搜索范围）
   仍未查明——本批只做**门槛侧**的合理性约束。这是下一批的候选题目。
3. 未做真机复测；`echo_proof_consecutive=3`（≈1.5 s）与容差 50 ms 均为**起始值**，未真机调参。
4. 自检工具与真机探针均未加单测（诊断工具，不在生产路径）。

---

## 5. 回滚

`git revert <本批提交>`。回滚后回到"只看相关性"的旧规则——**不建议**（会重现本次假停播）。
