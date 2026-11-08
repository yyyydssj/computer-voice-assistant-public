# P10 批次 R2b-1 交接：AEC 参考信号时间线 + NLMS 回声消除（纯离线）

> 日期：2026-10-02
> 批次：P10 R2b-1（阶段 R2b「真实 AEC」的离线部分；真机实测 R2b-2 另行排期）
> 用户选型：**方案 1 —— WASAPI 回环参考 + 自实现 NLMS**（零新依赖）
> 设计依据：`implementation_plan/12-barge-in-and-aec.md`、`discussions/pending-decisions.md#H.7`；前置 R2a 降级框架（`2026-10-02-P10-R2a-aec-degradation-handoff.md`）

## 1. 本批目标与不做

**目标**：把"AEC 参考信号"与"回声消除"做成**纯离线、可注入、可复核**的实现——参考帧按时间对齐、按设备延迟取窗、逐样本 NLMS 自适应消除，并如实给出抑制量（ERLE）与回退原因。

**本批不做**：
- 不打开真实设备、不放音、不录音（R2b-2 真机实测，需用户在场）；
- 不引入任何新依赖（不装 WebRTC APM/第三方 AEC）——方案 1 选定为零依赖；
- 不做 AEC 级联/NS/AGC 实现（R2a 只做降级判定；NS/AGC 仍为未做项）。

## 2. 交付物

| 文件 | 内容 |
|---|---|
| `app/audio/aec/reference.py`（新增，约 245 行） | `AecReferenceConfig`、`AecReferenceTimeline`（`push`/`window`/`note_device_change`/`clear`）、`ReferenceWindow`、`AecReferenceError`；参考窗口状态 `ok`/`history_short`/`future`/`too_old` |
| `app/audio/aec/nlms.py`（新增，约 330 行） | `NlmsConfig`、`NlmsEchoCanceller`（`cancel`/`reset`/`weights`/`cumulative_erle_db`）、`EchoCancellationResult`、`AecProcessingChain` + `AecChainFrame`、`pcm16_to_float`/`float_to_pcm16` |
| `tests/duplex/test_aec_nlms.py`（新增，**39 项**） | 见 §3 |

## 3. 测试覆盖（39 项）

| 测试类 | 覆盖点 |
|---|---|
| `AecReferenceConfigTests`（4） | 默认可用、采样率必须为正、延迟不得超容量、容量帧数有界 |
| `AecReferenceTimelineTests`（13） | 历史足够时 `ok`；无历史 `history_short`；未来时刻 `future`；过早时刻 `too_old`；设备延迟偏移取窗；新延迟**下一轮生效**；超 max 延迟拒绝；时间戳非单调拒绝；奇数长度/长度不符拒绝；环形缓冲有界；`clear()` 复位；`to_details()` 不含音频 |
| `NlmsConfigTests`（4） | 默认尾长 160 ms@16k（2560 抽头）、零尾长/步长越界/负正则拒绝 |
| `NlmsEchoCancellerTests`（12） | **合成回声（因果 FIR：0.5@delay3 + 0.3@delay11）尾部 100 帧中位 ERLE > 20 dB**；累计 ERLE > 20 dB；尾部残差 RMS 抑制 > 15 dB；输出非静音；双讲不应用消除输出且原因码正确；双讲标志类型校验；静音帧回退；长度不符拒绝；`reset()` 清空自适应状态；权重随自适应增长；`to_details()` 不含音频 |
| `AecProcessingChainTests`（5） | 无参考直通且原因码 `reference_history_short`；未来参考直通；**端到端合成回声尾部抑制 > 15 dB 且应用比例 > 30%**；`to_details()` 不含音频；无授权面 |
| `AecModuleBoundaryTests`（2） | AST：两模块不导入设备/网络/线程/音频库；不导入安全链 |

## 4. 关键设计决定

1. **参考以"实际送往输出设备的 PCM"为准**（设计文档 12）：`push(at_ms, pcm)` 按时间线进入有界环形缓冲；真机侧由 WASAPI 回环提供（R2b-2）。
2. **三种"取不到"必须区分**：`history_short`（样本不够）、`future`（采集时刻晚于最新参考）、`too_old`（早于缓冲最旧）——一律**不得**当成"无回声"，处理链回退直通并写明原因。
3. **设备延迟下一轮生效**：`note_device_change()` 只登记待生效延迟，下一次 `push()` 才切换；设备变化即要求重校准（对接 R2a 的 `device_changed` 判定）。
4. **逐样本递归 NLMS（标准归一化）**：`w += (mu / (||x||² + reg)) · e · x`，regressor 为当前样本之前的滤波器窗口。
5. **块式梯度被明确否决**：开发中先实现"整块窗口梯度"，实测**不收敛**（每个输出样本对应的权重索引不同，用同一段权重做线性梯度在数学上不成立；实测 ERLE ≈ −5~4 dB、权重发散）。改为逐样本递归后：**合成回声尾部中位 ERLE ≈ 48 dB、累计 ≈ 38 dB、权重精确收敛到两个真实抽头（delay 3 的 0.5、delay 11 的 0.3）**。
6. **不伪造抑制量**：没有参考能量就没有"回声被抑制"——静音帧 ERLE 记 0 dB（不是 ∞）；只有参考非零且残差精确为零才可能返回 ∞（理想数值情形）。
7. **回退优先**：`double_talk=True` 或该帧 ERLE 低于门限时，输出**直通原始采集**（不削用户开头），并给出 `fallback_reason`。

## 5. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests/duplex -t . -p "test_*.py" -q   → Ran 169（R1a 56 + R1b 40 + R2a 34 + R2b-1 39）, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q          → 见 §6
.\.venv\Scripts\python.exe -m compileall -q app tests tools                              → exit 0
```

开发过程中被测试/实测抓到的 4 个真实缺陷（全部已修，留痕）：
1. NLMS 权重更新维度错（`window` 是滤波器长度、`error` 是帧长，numpy 广播报错）；
2. 参考时间线对负时间戳直接拒绝（测试用例本身写错，改为非负时刻构造 `too_old`）；
3. **块式梯度不收敛**（见 §4.5，是设计级错误，非笔误）；
4. 静音帧被算成 ∞ 抑制量并误判 `applied=True`（`_erle_db` 口径修正）。

## 6. 全量与下一步

- 本批后全量基线：**2256 tests OK (skipped=3)**（2217 + 39，实测 115.498s）；`compileall -q app tests tools` exit 0；`app/audio/aec/reference.py` 234 行、`nlms.py` 323 行、`tests/duplex/test_aec_nlms.py` 388 行，均严格 UTF-8 无 BOM。
- **R2b-2（真机，需用户在场）**：

  > ⚠️ **R2b 尚未收口**：本批只交付离线实现（选型已定：方案 1），**真机回声实测数值一个都没有**——
  > 回环延迟、未消除回声量级、消除后 ERLE、双讲表现都还没有本机数据。这些必须由真机实测取得，
  > 不得用离线合成值代替。

  1. 用 WASAPI 回环取参考（`PyAudioWPatch`，已装）与麦克风采集；
  2. 播放已知样例（可复用 P9 runner 的离线声线），测**回环延迟**（互相关）、**未消除回声量级**、**消除后 ERLE**；
  3. 双讲（本人同时说话）验证不削开头；输出 artifact 并登记真实数值；
  4. 需要新增 `tools/p10_aec_probe.py`（下一批实现，含 `--live-opt-in` 门禁）。
- 之后：**R3** 设备恢复、**R4** 会话循环接线、**R5/R6**。
- 本批改动**未提交、未 push**。
