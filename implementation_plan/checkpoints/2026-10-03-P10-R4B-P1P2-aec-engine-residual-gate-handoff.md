# P10 批次 R4-B P1/P2 交接：真 AEC3 接入打断通道 + 残余回声地板感知动态门

- 建立时间：2026-10-03
- 上游：`research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`（真机 ERLE 10.3/15.5 dB、
  28% 帧超门）、`checkpoints/2026-10-03-P10-R4B-2-planC-cooldown-handoff.md`（冷却窗口）
- 恢复指引：`checkpoints/2026-10-02-P10-R4A-handoff-resume.md` §9

---

## 1. 一句话结论

**三件套已落地两件半**：① AEC3 真接入打断通道（`--aec-engine webrtc`，默认 nlms 行为不变）+
实测 ERLE/残余地板如实上报；② 冷却窗口（上一批）；③ **残余回声地板感知动态门**
（`max(能量门, 残余地板 × margin)`，只在 AEC3 真在跑时生效）。**R4-A 的 fail-closed 口径未改**
（没证明"看得见自己的回声"仍不真停播），所以本机当前仍不会真打断——本批解决的是
"引擎真的在跑 + 门槛跟着残余回声走 + 事实如实暴露"。

---

## 2. 交付物

| 文件 | 改动 |
|---|---|
| `app/duplex/barge_in_channel.py` | `BargeInChannelConfig` 新增 `use_reference_aec`（默认 False）/ `residual_gate_margin`（默认 None）/ `residual_floor_frames=50` / `residual_floor_quantile=0.2`（含校验：margin ≥ 1、quantile ∈ (0,1]）；`observe_once()` 先用 `_apply_reference_aec()` 拿**残差**再算电平/概率（**检测器结构判定仍看原始帧**）；`_effective_energy_threshold()` 实现动态门；`_note_alignment()` 把对齐延迟喂给 AEC3 的 `stream_delay_ms`；新增 8 个只读属性与 9 个详情字段 |
| `app/runtime/auto_voice.py` | `AEC_ENGINES`；`AutoVoiceConfig.aec_engine`（默认 nlms）+ `aec_residual_gate_margin`（默认 3.0，≥1）；`_build_barge_in()` 判定"引擎真在跑"后才报 `CAPABILITY_SOFTWARE`、才传 `use_reference_aec`/`residual_gate_margin`；`barge_in_details()` 增引擎/ERLE/地板/门字段 |
| `app/runtime/voice_entry.py` | `VoiceEntryConfig.aec_engine`；`build_same_process_reference(engine=...)`（webrtc 缺依赖时**如实**退回 nlms，实际后端看 `reference.backend`） |
| `app/main.py` | CLI `--aec-engine {nlms,webrtc}`；启动提示如实报告"AEC3 是否真在跑 + 实测抑制量 + 当前门"，请求了但没跑时明确说明原因 |
| `tests/duplex/test_barge_in_aec_engine.py`（新，**27 项**） | 配置校验、AEC 注入六条路径（未启用/无参考/无 process/异常/输出非法/成功）、`reference_silent` 不计地板、时钟即 `capture_at_ms`、ERLE 透传、动态门四条（margin=None/空地板/低地板/高地板 + 低分位抗用户说话）、`_analyze` 真用动态门、延迟提示只下发一次/无 setter 容忍、会话配置校验、会话接线（无 webrtc 后端不跑；有则 `use_reference_aec=True` 且 `aec_plan.level=aec_ns`） |

---

## 3. 关键语义（不要误解）

1. **默认不变**：`--aec-engine nlms`（默认）+ `residual_gate_margin=None`（默认）→ 与上一批
   逐行为等价（通道只用参考做对齐/相关性）。
2. **能量看残差、结构看原帧**：AEC3 残差只用于**帧电平与能量门**；VAD/检测器的结构判定仍看原始帧
   （避免把处理后的音频喂给模型引入新的不确定性）。
3. **动态门只抬不降**：`max(energy_threshold, floor × margin)`，`margin ≥ 1` ⇒ 绝不会比固定门更敏感。
4. **地板只吃"参考有能量"的帧**：`reference_silent` 的帧不计入（没有回声可测）；用 **p20** 低分位，
   用户连续说话也不会把地板抬到把自己挡住（有测试）。
5. **延迟提示来自已有的对齐估计**（每 0.5 s 一次），不新增启动探测；真机实测该提示敏感
   （0–500 ms 内 ERLE 7–15.5 dB），且同一延迟不重复下发。
6. **能力与事实分离**：`CAPABILITY_SOFTWARE` 只表示"AEC3 真在跑"；"抑制够不够"由
   `aec_erle_db` / `aec_erle_sufficient` / `gate_in_playback` 如实暴露，不用能力位断言。
7. **fail-closed 未改**：`echo_proven`（对齐相关性 ≥ 0.2）仍是"能否真停播"的最终闸门；
   本机该值为 false ⇒ 仍不会真打断。

---

## 4. 验证

| 项 | 结果 |
|---|---|
| 新专项 | `tests.duplex.test_barge_in_aec_engine` **27 项 OK** |
| 打断/会话/AEC 相关合计 | **391 项 OK**（110.2 s） |
| **全量** | **Ran 2580 tests in 235.444s → OK (skipped=3)**，exit 0（2553 → 2580，+27） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 331 个 .py：**0 解码失败**、BOM **25**（与基线一致）；本批文件无 BOM、纯 LF |
| CLI | `app.main --help` 可解析 `--aec-engine {nlms,webrtc}` |

---

## 5. 未做 / 已知边界（**不得伪造**）

1. **未做真机**：三件套没有一起上真机试听；真机 ERLE/延迟是上一批的独立测量（10.3/15.5 dB、
   392–409 ms），本批只保证"接线正确 + 门槛按残余回声走"。
2. **未改 `AecPolicy.max_echo_path_ms=120`**：本机实测参考→采集延迟 ~400 ms（主要来自 waveOut 200 ms
   缓冲），若要按"路径长度预算"判定，本机会被判 `strict`；把判据从"路径预算"改成"实测 ERLE 门"
   是**安全口径变更**，需要单独点头（本批未做，`estimated_echo_path_ms` 仍传 0）。
3. **动态门未调参**：`margin=3.0`（≈+9.6 dB）是起始值，未真机验证；地板窗口 1 s、分位 p20 同理。
4. **未做**：耳机/外接麦场景；`pyaec`/speexdsp 对照；方案 C 剩余项（"更严门槛"档、半双工提示 UI）；
   R5/R6。
5. AEC3 的 NS/AGC 未启用（只用 EchoCanceller），避免改变能量口径。

---

## 6. 回滚

- 一键回退：`--aec-engine nlms`（默认）→ 引擎不接线；`residual_gate_margin` 默认 `None` → 动态门关闭；
  两者都不给时与上一批（`f6d6a19`）行为等价。
- 代码回退：`git revert <本批提交>` 或 `git reset --hard f6d6a19`。

---

## 7. 下一步

1. **真机试听（需用户在场）**：`--aec-engine webrtc --aec-reference-mode same_process
   --barge-in-mode confirm --barge-in-cooldown-ms 1000`，观察 `aec_erle_db`/`gate_in_playback`/
   `aec_stream_delay_ms` 与是否仍有自触发；据此决定是否把"能否真停播"的判据从 `echo_proven`
   改为"实测 ERLE + 残余门"（安全口径变更，需用户点头）。
2. 方案 C 剩余项（更严门槛档、半双工提示 UI）。
3. R5 Provider 冷却 / R6 长期运行健康。
