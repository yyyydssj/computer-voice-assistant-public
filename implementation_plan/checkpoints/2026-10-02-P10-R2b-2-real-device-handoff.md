# P10 批次 R2b-2 交接：真机 AEC 实测（回环路线判为不可用 → 改用同进程参考）

> 日期：2026-10-02
> 批次：P10 R2b-2（阶段 R2b「真实 AEC」的真机部分）
> 用户选型：**方案 1**（先做方法 1 的回环路线，实测失败后经用户批准改**方案 A：同进程提交 PCM 参考**）
> 前置：R2b-1（`2026-10-02-P10-R2b-1-aec-offline-handoff.md`，全量 2256）

## 1. 结论（先说结果）

1. **WASAPI 回环参考路线在本机实测不可用**——不是算法、不是参考信号设计、不是效果链。
2. 已按用户批准实施 **方案 A**：AEC 参考改取"**实际提交给播放设备的 PCM**"（同进程旁路，带单调时间戳），这是设计文档 12 的首选口径；**通路已验证正确**（真机 300 帧、0 错误、播放时长与参考一致；端到端自检对齐 0.9941、ERLE 16~24 dB）。
3. **真机 ERLE 仍无有效数值，本文件不填任何估计值**。
4. **原因判定（2026-10-02 最终）**：本机是**笔记本内置扬声器 + 内置麦克风阵列**，用户确认麦克风增强页**没有**「回声消除 / 噪音抑制 / AGC」开关 → 阵列内置 DSP（AEC/降噪/波束成形）在驱动层默认生效且**不可关闭**，麦克风侧因此不存在可测的线性回声路径。真机验证需要**外接麦克风**（无内置 DSP 或可关闭增强）。
5. 两条结论**分开登记**：回环"不可用（原因未查明）"与房间/麦克风侧"无可测回声（已量化）"是两件事——回环是数字回灌，不应受房间声学影响。

### 真机四轮关键数值（判定依据）

| 指标 | 有回声（自检基准） | 无回声（自检基准） | 真机实测 |
|---|---|---|---|
| `mic_alignment` | **0.9941** | −0.0382 | **−0.0767 / −0.0525** |
| `top_offsets` 主峰 | 0.994（集中、尖锐） | 0.037 量级（分散） | 0.053 量级（分散） |
| `mic_level_vs_floor_db` | 60.8 dB | — | **10.6 / 9.3 dB** |
| 播放侧 | — | — | `submit_calls=30`、`submitted_bytes=288000`、`elapsed=6.031s`、`frames=300`、`errors=0` ✅ |

## 2. 回环路线失败的四轮证据（全部可复核）

| 实验 | 命令 | 结果 |
|---|---|---|
| ① 回环是否有效 | `p10_wasapi_loopback_check.py --live-opt-in --watch 8`（只录回环，用户用系统放音乐） | `rms=0.1805`、`active_ratio=0.955` → **回环本身有效** ✅ |
| ② 系统音乐 vs 我们的播放 | `p10_aec_probe.py --live-opt-in --loopback-only --save-audio` | 回环 `peak=0.99994`、`band_ratio≈0.88`，但模板对齐仅 **0.0408** |
| ③ WASAPI 直放**同一端点**并录其回环 | `p10_wasapi_loopback_check.py --live-opt-in` | `out[10] 扬声器` ↔ `loop[13] 扬声器[Loopback]`，对齐 **−0.0608** |
| ④ 逐频点能量比对 | 多音扫描 200 Hz–5 kHz（`--tones --loopback-only --save-audio`） | 回环里 **700 Hz 以上 9 个频点能量比全为 0.000**；≤400 Hz 有能量（环境低频） |

附加排除项：
- **削顶**：多音 WAV `clipped_samples=0`、`recorded_peak=0.718` → 不是削顶失真；
- **效果链**：多音逐频点增益 −5.4…+4.9 dB（中等不平坦，不是"2 kHz 以上一刀切"）→ 早期"APO 改写波形"的判断**被证伪**；
- **时钟漂移**：合成实验显示漂移 1.0005 会把 6 s 长参考的对齐分打到 0.03；据此把参考改为"多段 0.6 s 突发"（段内漂移 < 0.05 ms），真机仍不改善 → 漂移不是主因；
- **算法与参考信号**：`p10_aec_probe_selfcheck.py` 六种合成场景（含 1.0005 漂移、更大噪声底、更弱回声）**全部通过**（对齐 0.56–1.0、ERLE 10–11 dB）。

**如实登记**：结论是"本机回环抓不到本程序播放的音频"，**根因未查明**（用户确认用的是扬声器、系统默认设备也是扬声器）。不再继续猜测，改为绕开。

## 3. 方案 A 交付物

| 文件 | 内容 |
|---|---|
| `app/audio/aec/reference_source.py`（新增，约 300 行） | `SameProcessAecReference`（`submit`/`window`/`process`/`timeline_frames`/`note_device_epoch`/`as_submit_observer`）、`AecReferencePCM`（格式契约）、`AecReferenceFrameResult`、`AecReferenceSourceError`；**流式重组**：接受任意长度 PCM 块（waveOut 默认一次提交 200 ms＝9600 字节），按 20 ms 切帧入时间线，余字节留待组装 |
| `app/tts/waveout_pcm.py`（修改，+18 行） | `WaveOutPCMStreamBackend(submit_observer=...)` **只读旁路**：仅在 `waveOutWrite` 成功后回调（锁外、字节原样）；异常一律吞掉，**绝不冒泡到提交路径** |
| `app/audio/aec/reference.py`（修改） | 新增 `frames()` 快照（供离线复盘拼接时间轴） |
| `tools/p10_aec_probe.py`（修改，1060 行） | 新增 `--reference same-process` 模式与 `analyze_same_process()`；保留回环模式与 `--tones`/`--loopback-only`/`--save-audio` 诊断开关 |
| `tools/p10_wasapi_loopback_check.py`（新增，417 行） | 端点配对与一致性实验、`--watch`（只录回环）、`--probe`（回环+麦克风同时录） |
| `tools/p10_aec_probe_selfcheck.py`（新增，127 行） | 六种合成场景的自检脚本（含漂移/噪声底） |
| `tools/p10_aec_wav_diagnose.py`（新增，113 行） | 只读 WAV 诊断（对齐、包络相关、频段占比、过零率） |
| `tests/duplex/test_aec_reference_source.py`（新增，**45 项**） | 见 §4 |

## 4. 测试覆盖（43 项）

| 测试类 | 覆盖点 |
|---|---|
| `AecReferencePCMTests`（4） | 默认 48 kHz/960 样本、非法采样率与帧长拒绝、`to_config()` |
| `SameProcessReferenceConfigTests`（3） | NLMS 采样率/帧长不一致拒绝、一致可接受 |
| `SameProcessReferenceSubmitTests`（9） | 空帧/长度不符/奇数长度/非 bytes/负时间戳/NaN 拒绝；提交计数；**同一毫秒多次提交保持单调**；`to_details()` 无音频字段 |
| `SameProcessReferenceWindowTests`（2） | 历史不足 → `history_short`；历史足够 → `ok` 且长度=960 |
| `SameProcessReferenceEpochTests`（3） | 同 epoch 空操作；新 epoch 清历史 + 重置自适应 + 计数；非法 epoch 拒绝 |
| `SameProcessReferenceProcessTests`（9） | **真 FIR 连续流回声尾部 ERLE > 10 dB**；累计 ERLE > 10 dB；延迟补偿生效；**权重命中真实增益 0.3（±0.08）**；应用比例 > 0.9；输出非静音；细节无音频/无授权字段；无参考回退；错误长度拒绝；`reset()` 清空 |
| `SameProcessReferenceObserverTests`（4） | 回调记录帧；非法帧被吞并计数；坏时间戳不抛出；AST 边界（不导入设备/网络/安全链） |
| `WaveOutSubmitObserverTests`（5） | 回调收到**原样字节**；`waveOutWrite` 失败时**不回调**；回调抛异常**不影响提交**；非 callable 拒绝；无观察者时行为不变 |
| 延迟约束测试（2） | **零延迟是已知约束**（ERLE < 6 dB，断言约束存在）；**1 样本延迟可收敛**（ERLE > 20 dB） |

## 5. 关键设计决定与已证实的实现约束

1. **旁路必须零影响**：钩子在提交成功后、`condition` 锁外调用；回调内异常一律吞掉并计数（上限 8）；`bytes(pcm)` 复制后传递，避免设备缓冲被改写。测试覆盖"失败不回调 / 异常不影响提交"。
2. **参考与采集同一时间轴**：参考帧带"提交时刻"的单调毫秒戳；同一毫秒的重复提交钳到 +1 ms，避免零宽度帧。
3. **epoch 变化即重校准**：清空时间线 + 重置 NLMS + 计数；延迟下一轮生效（沿用 R2b-1 的 `note_device_change` 语义）。
4. **延迟约束（实测得出）**：逐样本 NLMS 的权重索引随帧边界每次位移 1，因此真实回声延迟必须 **≥ 1 样本**；零延迟时 ERLE ≈ −1 dB 且权重发散（实测），延迟 1/2/3 样本时稳定 **41.3 dB** 且精确命中增益。真实设备链路必然有延迟，故不影响实际使用，但**已写成文档与测试**，避免后人误判。
5. **回退优先**：参考窗口不可用（`history_short`/`future`/`too_old`）时输出直通原始采集并写明原因，绝不伪称已消除。
6. **旁路必须可观测**（真机踩坑后补）：首次真机跑出 `submit_observer_never_fired`，根因是 `submit()` 只接受"恰好一帧 960 字节"，而 waveOut 实际提交 **9600 字节（200 ms）大块**，异常被吞掉且**不暴露原因**。修法两条：① 参考源内置流式重组，接受任意长度块；② 异常计数**并记录固定短代码**（`observer_errors` + `last_observer_error`），不再"静默失败"。回归测试覆盖 200 ms 大块切 10 帧的用例。

## 6. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest tests.duplex.test_aec_reference_source -q   → Ran 45, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/duplex -t . -p "test_*.py" -q → Ran 210, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q     → Ran 2301, OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools                        → exit 0
```

端到端集成自检（同一条 `_play()` + 旁路 + `analyze_same_process()` 通路，用假后端转调真实旁路）：

```text
play facts: {'backend': 'fake_waveout', 'bytes': 192000, 'seconds': 4.0, 'drained': True}
旁路：frames=100  errors=0  pending=0
分析：status=ok  mic_alignment=1.0  erl_db=-10.457  erle_tail_db=22.901  cumulative_erle_db=31.78
```

开发过程中被测试/真机抓到的真实缺陷（已修，留痕）：
1. `analyze_same_process` 帧数按"截齐前长度"计算，导致最后一帧长度不足（`invalid_pcm_length`）；
2. 同一处，参考段与麦克风段的相对起点不一致，尾部 ERLE 恒为 −0.97 dB；
3. NLMS 零延迟不收敛（见 §5.4）——由隔离实验定性，写成约束 + 测试而非掩盖；
4. **旁路只接受整帧长度**：waveOut 提交 200 ms 大块 → 真机 `submit_observer_never_fired`（异常被静默吞掉）。已改为流式重组 + 暴露 `last_observer_error`。
5. **参考缓冲容量不足**（真机第二次踩坑）：`AecReferenceConfig.max_capacity_ms` 默认 2000 ms，而 6 秒参考实际提交 3 秒（150 帧）→ 最早的帧被静默挤出，麦克风里存在的那段参考**已不在缓冲里**（真机 `reference_seconds=2.0` 却报了 `reference_frames_submitted=150`）。已改为默认 8000 ms，工具按 `播放时长 + 3000 ms` 设置。
6. **偏移语义错**（同次真机暴露）：`best_offset` 是"参考起点在麦克风时间轴上的位置"（= 需要补偿的延迟），却被再次当作参考内部偏移扣掉，把已对齐的信号推歪（真机 `mic_alignment=0.0366`）。已改为：麦克风从 `best_offset + skip` 起、参考从 `skip` 起，两段逐帧对齐；并新增 `compensation_samples` / `mic_seg_seconds` 便于核对。
7. **采样率不一致（最隐蔽的一个）**：工具把 **48 kHz 采样的参考**交给**固定 24 kHz** 的 waveOut 后端 → 实际播放时长翻倍（12 秒），而工具只等/录 6.8 秒就 `waveOutReset` → **只送进 3 秒（150 帧）**，且没有任何报错。真机三处证据：`reference_seconds=3.0` 而 `bytes=288000`（=6 s@24k）、`reference_band_ratio=0.4497`（缓冲里近一半是空白/未播）、`mic_alignment≈0.04`。修法：参考按**后端原生采样率（24 kHz）**播放，麦克风侧重采样到同一时钟；并给 `_play()` 加 `submit_calls / submitted_bytes / blocked_waits / elapsed_seconds` 提交统计（可观测性）。
   端到端自检（含 1.2 s 麦克风延迟、24 kHz 播放 + 48 kHz 麦克风重采样）：`frames=300`（完整 6 秒）、`mic_alignment=0.9997`、`erle_tail_db=16.2 dB`。

**关键教训（写给后续批次）**：`app/tts/waveout_pcm.py` 的采样率是**硬编码 24 kHz**；任何"把信号交给播放后端"的代码都必须按这个时钟准备数据，否则会静默地改变播放时长。

## 7. 下一步

- **R2b 真机验证的前置条件**：需要**外接麦克风**（不带内置 DSP，或自带可关闭的增强开关）。在满足该条件前，**不下"AEC 真机有效/无效"的结论**，只登记"本机不可验"。
  - 可选自查实验（用户执行，不改代码）：把系统音量调到静音后重跑同一命令；若 `mic_level_vs_floor_db` 仍为 9~10 dB，即证明该差值来自环境/阵列自适应，与我们的播放无关。
- **R3** 设备断开/重连/安全空闲点恢复（消费本模块的 `device_epoch`）。
- **R4** 会话循环接线：把"旁路参考 + 打断状态机"接进 `app/runtime/auto_voice.py`（独立打断通道 + 安全门）。
- **顺带发现的项目事实（写给后续批次）**：`app/tts/waveout_pcm.py` 的 `SAMPLE_RATE` 硬编码 24 kHz；任何把信号交给播放后端的代码都必须按该时钟准备数据，否则会**静默改变播放时长**（本批次真机连续踩坑 3 次：帧数减半、参考只回放一半、`reference_band_ratio` 掉到 0.45）。
- 本批改动**未提交、未 push**。
