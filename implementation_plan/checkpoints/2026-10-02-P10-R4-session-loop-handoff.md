# P10 批次 R4 交接：会话循环接线（独立打断通道 + 安全门 + 设备策略 + R2b 参考）

> 日期：2026-10-02
> 批次：P10 R4（R4a 独立打断通道 / R4b 安全门 / R4c 会话循环接线 / R4d 设备策略接线 / R4e 参考与降音量）
> 设计依据：`implementation_plan/12-barge-in-and-aec.md`（两阶段打断 + 安全确认例外）、`13-device-tuning.md`
> 上游交接：`2026-10-02-P10-R1a-barge-in-state-handoff.md`、`-R1b-`、`-R2a-`、`-R2b-1-`、`-R2b-2-`、`-R3-`
> 用户本轮授权：完整 A→G（含 `waveout` 增益钩子与 CLI 开关）；**真机试听按用户后续指示推迟（本轮不做）**

## 1. 本批目标与不做

**目标**：把 R1（两阶段打断）、R2a/R2b（降级阶梯 + 同进程参考）、R3（设备恢复策略）接进
`app/runtime/auto_voice.py` 的**会话循环**，形成**独立打断通道 + 安全门**；打断**不复用**
`interrupt_source`（那是 main 的「中止整个会话」键盘轮询口）；默认全关，行为与现状一致。

**本批不做（如实登记，绝不伪造）**：
1. **前置音频未并入下一轮 ASR**。确认插话后前置 PCM 只被保留并通过
   `AutoVoiceSession.take_pending_pre_roll()` 暴露，轮次事实里如实写
   `pre_roll_merge_unsupported`。原因：要并入必须同时处理 `AudioBus` 的
   `event_sequence`/`frame_index` 连续性与采集会话计数器（否则总线 `frame_discontinuity`
   fail closed 会**打掉整轮**），这属于 audio 层的 pre-roll 注入接口，另行排期。
2. **输出设备重连在本机不可用**：waveOut 走 `WAVE_MAPPER`，没有"固定输出设备 id"，
   R3 策略因此不给 `reconnect_output`，输出断开按用户决定 **回退文字**（`text_mode_kept`）。
3. **真机 ERLE / AEC 有效性**仍未验证（需外接麦克风）；本机 16 kHz 采集与 24 kHz 播放
   **不同钟**，所以本批**不给** `echo_correlation`，按 R2a 如实降到 `strict_no_aec`。
4. 不改 `app/safety`/`app/executor`/`app/plans`；打断不产生任何授权事实。

## 2. 交付物

| 文件 | 内容 |
|---|---|
| `app/duplex/barge_in_channel.py`（新增，**706 行**） | `BargeInChannel`（`arm`/`observe_once`/`run`/`request_stop`/`disarm`/`take_pre_roll_pcm`/`pre_roll_pcm`/`to_details`）、`BargeInChannelConfig`、固定原因码；自有 `AudioBus` 身份 `p10-barge-in-channel`（16 kHz/20 ms/640 B）；音量经 `gain_sink` 输出；参考相关性只在**同钟且窗口可用**时计算；**无可用参考时按设计不降音量**（真机修复，见 §9） |
| `app/duplex/barge_in.py`（改，R1a；+`min_suspect_frames`） | 「疑似」需连续命中帧数（默认 1 保持既有语义；会话层用 3）；`reset`/`release`/播报结束都会清空待定计数 |
| `app/duplex/barge_in_safety.py`（新增，**169 行**） | `BargeInSafetyGate.decide()` → `handoff` / `stop_and_replay` / `block`；`BargeInSafetyDecision`（`authorization_possible` **恒 False**） |
| `app/duplex/__init__.py`（改） | 导出新公开面（60 项） |
| `app/runtime/auto_voice.py`（改，1084 → **1773 行**） | `AutoVoiceConfig` 五个新开关；`_BargeInSpeakerProxy`/`_BargeInStreamProxy`（播报期守望）；`_monitor_barge_in`（监视线程主体）；`barge_in_details`/`pending_pre_roll_pcm`/`take_pending_pre_roll`/`set_awaiting_confirmation`；设备策略（`_build_device_policy`/`_device_safe_point`/`_attempt_device_open`/`_observe_device`/`device_details`）；`AutoTurnFacts` 增 `barge_in`/`pre_roll`/`device` |
| `app/runtime/voice_entry.py`（改） | `VoiceEntryConfig.aec_reference_mode`；`VoiceEntryEnvironment.aec_reference`/`playback_gain`；`build_same_process_reference()`（**24 kHz，与后端同钟**，`NlmsConfig` 同钟）作 `submit_observer`，同时建 `PlaybackGain` |
| `app/tts/waveout_pcm.py`（改，365 → **449 行**） | 新增 `PlaybackGain`（读写分离、钳位、异常退回 1.0）与**可选** `gain_provider`（默认 `None` → 提交字节级不变）；提交前按增益缩放 PCM16；诊断增 `scaled_submissions`/`min_gain_applied`；旁路参考改为取**实际提交给设备**的 PCM（含增益） |
| `app/main.py`（改） | `--barge-in-mode {off,duck,confirm}`、`--aec-reference-mode {off,same_process}`、`--device-recovery-mode {off,safe_point}`；`_voice_entry_config_for_auto` 转发参考开关 |
| `tests/duplex/test_barge_in_channel.py`（新增，783 行，**50 项**） | 见 §3 |
| `tests/duplex/test_barge_in_safety.py`（新增，193 行，**17 项**） | 见 §3 |
| `tests/foundation/test_auto_voice_barge_in.py`（新增，1206 行，**55 项**） | 见 §3 |
| `tests/tts/test_waveout_gain.py`（新增，170 行，**13 项**） | 见 §3 |
| `tests/duplex/test_barge_in_state.py`（改，R1a；**+6 项**） | `min_suspect_frames` 语义与非法值（见 §9） |

## 3. 测试覆盖（R4 本批 135 项；含真机缺陷回归共 +141）

| 测试类 | 项数 | 覆盖点 |
|---|---|---|
| `BargeInChannelConfigTests` | 4 | 默认值落在设计区间；帧时长必须与采集 20 ms 一致；非法参数（降级码/概率模式/容量/超时/阈值/参考钟）；详情无音频 |
| `BargeInChannelArmTests` | 6 | 缺工厂/工厂抛错/采集未启动 → `capture_unavailable` 且**不打断**；成功 arm 用**自有身份**；arm 幂等；disarm 带 `discard=True` 且幂等；未 arm 观察是空操作 |
| `BargeInChannelStageTests` | 6 | **单帧不再降音量**（最小连续帧）；连续 3 帧才降且不建轮次；持续人声确认后音量归零；误触发先滞后保持再**平滑**回到原音量；静音从不降音量；能量概率有上界 |
| `BargeInNoReferenceDuckTests` | 6 | **真机缺陷回归**：无可用参考/异钟参考 → **不降音量**（`duck_disabled_no_reference`）且全程只有 1.0；确认仍可用；显式 opt-out 可恢复降音量；**零星回声命中不再让音量来回跳**；有可用参考时降音量照旧 |
| `BargeInChannelPreRollTests` | 3 | 前置缓冲受容量约束且 ≥300 ms；取出后清空；详情只含计数 |
| `BargeInChannelRunTests` | 6 | 播报已结束→恢复原音量；确认路径返回 `handoff_to_turn`；总线失效→`capture_stopped` 且返回 `None`；超时如实返回；`request_stop` 快速返回；非法参数/未 arm |
| `BargeInChannelReferenceTests` | 5 | 无参考/异钟/窗口异常/同钟的四种参考状态；**相关性折减人声得分**（同样人声在高相关时不再确认） |
| `BargeInChannelDegradationTests` | 4 | 同一声学条件下 `full`(0.7) 确认而 `strict`(0.9) 不确认；缺参考自动降为 strict；`no_aec` 可用；未知降级码拒绝 |
| `BargeInChannelDetectorTests` | 3 | 注入检测器被使用；**检测器抛错按"没有人声"**（绝不放行）；声称检测器却没检测器 → 退回能量并计错 |
| `BargeInChannelBoundaryTests` | 3 | AST：不导入安全链 / 不导入 `app.main` / 代码里不出现 `interrupt_source` 标识符 |
| `BargeInSafetyGate` 四组 | 13 | 普通回复 `handoff`；安全提示/等待确认 → `stop_and_replay` + 必须完整重播 + 不建立可用轮次；半双工/通道不可用/无播报 → `block` 且不停播；能力先于播放窗口判定 |
| `ValidationTests`（安全门） | 5 | 未知提示类型/非布尔标志拒绝；`authorization_possible=True` **被契约拒绝**；未知动作拒绝 |
| `BoundaryTests`（安全门） | 3 | AST：不导入安全链/音频设备；源码中不存在 `authorization_possible=True` |
| `ConfigTests`（会话） | 5 | 默认全关；三套词表冻结；非法模式/超时/config 类型拒绝；真 `BargeInConfig` 可用 |
| `BargeInOffTests` | 2 | `off` 时不建任何对象、闭环仍用**原 speaker**；构造期不碰采集设备 |
| `BargeInUnavailableTests` | 4 | 无播报通道 → `no_playback_channel`；无增益钩子 → `duck_unavailable`；无参考 → `strict_no_aec`（`aec_applied=False`）；24 kHz 参考 → 仍 `strict_no_aec` 且原因 `speaker_reference_missing` |
| `MonitorWiringTests` | 10 | 采集不可用**绝不停播**；`confirm` 确认后停播 + 保留前置 + 增益复位；`duck` 确认被抑制且不停播；**有可用参考时**疑似经 `PlaybackGain` 降到 0.25；**无参考时不降音量**（真机缺陷回归）；等待确认时停播但**不交接**；`set_awaiting_confirmation` 类型校验；前置可取出一次；详情无音频/正文；`close()` 收尾 |
| `DevicePolicyTests` | 9 | `off` 不建策略；输出健康未知时**跳过决策**（不谎报回退）；双 READY 无需动作；轮次中延后；有固定目标时输出重连各试一次（成功→READY、失败→同一 episode 不重试）；输入重连成功→READY 且探测收尾；`busy` 保留文字不自动重试；重复观察不虚增 epoch；详情含策略视图 |
| `FullTurnTests` | 2 | **默认关**时一整轮 `run_turn` 照常完成（`submitted`/`spoken`）且 `barge_in`/`device`/`pre_roll` 全为 `None`；采集装不上时快速如实收尾 |
| `RealReferenceFactoryTests` | 3 | 方案 A 参考源必须与播放后端**同钟**（24 kHz/480 样本）；提交的播放 PCM 进入参考时间线；旁路观察者吞掉非法输入并留 `last_observer_error` |
| `CliWiringTests` | 4 | 三个新开关默认 `off`；显式开关进入共享配置；参考开关转发到 `VoiceEntryConfig`；非法取值被 argparse 拒绝 |
| `BoundaryTests`（会话） | 3 | 会话接线不触碰 `interrupt_source`/`app.main`；打断通道用自有身份；前置并入未被谎报为已完成 |
| `PlaybackGainTests`/`ScalePcmTests`/`BackendGainTests` | 13 | 增益钳位与读取不抛错；`gain>=1` 字节级不变；0.5 缩放精确；0 增益等长静音；无钩子时不改字节；增益异常/非法返回值退回原音量；**旁路参考取实际提交字节**；非法钩子类型拒绝 |

## 4. 关键设计决定

1. **独立打断通道**：自有 `AudioBus`/`stream_id`（`p10-barge-in-channel`）、自有采集、自有监视线程；
   与轮次采集、与 `interrupt_source` 完全不同域。打断只影响"本次播报的音量与停止"。
2. **包装 speaker 而不是 `_speak`**：`_BargeInSpeakerProxy.start_stream()` 返回流代理，
   `push_text_chunk` 先起监视再**原样透传**（不改变首字延迟），`finish`/`cancel` 透传后在
   `finally` 收尾监视。这样任何调用方（含 HUD 的 `_speak` 包装）都自动获得打断能力。
3. **两种模式的语义边界**：`duck` = 只做第一阶段降音量，**确认不生效**（停播被抑制并记
   `confirm_suppressed_in_duck_mode`）；`confirm` = 降音量 + 确认后停播（+ 安全门）。
4. **安全门先于动作**：确认后由 `BargeInSafetyGate` 决定动作；安全提示/等待确认 → 停播 +
   `PromptPolicy` 失效 + 必须完整重播 + **不**把打断音频交接为可用轮次；插话永不授权。
5. **降级来源是 R2a**：`AecCapabilityProbe`（参考是否存在/是否同钟/是否换设备）→
   `AecDegradationController.plan()` → `barge_in_degradation` 交给 R1a/R1b 状态机。
   本机 16 kHz 采集 vs 24 kHz 播放 ⇒ `reference_matches_output=False` ⇒ `strict_no_aec`。
6. **降音量真实落地**：`WaveOutPCMStreamBackend(gain_provider=...)`，默认 `None`（字节不变）。
   如实说明粒度：增益作用于**已提交的那块缓冲**，本项目后端缓冲 200 ms，因此
   "≤100 ms 内降音量"**不是**本批的可验证承诺（`duck_quantum_ms=200` 已登记进环境详情与交接）。
7. **设备策略只消费事实**：输入 READY 来自采集真正启动；输出 READY/DISCONNECTED 来自播报
   成功/`speech_failed`；重连各试一次（按 disconnect episode），未知设备不采用，输出无固定目标
   时如实回退文字。

## 5. 硬不变量（有测试断言）

1. **插话永不授权**：安全门 `authorization_possible` 恒 `False`，构造时传 `True` 直接 `ContractError`；
   会话接线不导入安全链。
2. **拒绝即不打断**：通道装不上/总线失效/超时/检测器抛错，一律"保持原播报 + 固定原因码"。
3. **不伪装能力**：未接参考就不给相关性、不声称消除回声；没有增益钩子就记 `duck_unavailable`；
   前置音频未并入就写 `pre_roll_merge_unsupported`。
4. **默认全关零行为变化**：`barge_in_mode=aec_reference_mode=device_recovery_mode=off` 时
   不建任何新对象、闭环仍直接持有原 speaker，`run_turn` 照常完成。

## 6. 验证（2026-10-02 实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests/duplex -t . -p "test_barge_in*.py" -q → Ran 169, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/foundation -t . -p "test_auto_voice_barge_in.py" -q → Ran 55, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/tts -t . -p "test_waveout_gain.py" -q → Ran 13, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q → Ran 2476, OK (skipped=3)
.\.venv\Scripts\python.exe -m compileall -q app tests tools → exit 0
编码扫描：321 个 .py，0 解码失败，BOM 25（与基线一致；新增/修改文件全部严格 UTF-8 无 BOM）
```

基线：**2335 → 2446（首次）→ 2449（参考修复）→ 2463（音量跳动）→ 2471（窗口粘滞+能量门）→ 2476（假确认 fail-closed）→ 2492（批次 R4-A：参考同钟 + 对齐估计 + 回声安全门）tests OK (skipped=3)**（+157）。

全量口径说明（如实）：R4 首次全量 2446 OK；check-only 真机核查阶段发现并修掉参考采样率错配（+3 项回归测试）后重跑 2449 OK。另有一次**后台高负载**下 4891s 的全量出现 **1 个失败**，前台独立重跑两次均通过；该次失败用例名未能取回（后台任务输出只保留尾部），因此**不宣称**它一定是已知 flaky，只记录事实；本批新增测试的等待超时已从 5s 提到 20s 以降低时序敏感度。

## 6b. check-only 真机核查（不开采集/播放流）

- `python -m app.main --help`：三个新开关正常解析；
- `build_real_environment(VoiceEntryConfig(tts_mode="none", aec_reference_mode="same_process"))` 成功：
  `aec_reference` = `SameProcessAecReference`（**24000 Hz / 20 ms / 480 样本**）、
  `playback_gain` = `PlaybackGain(1.0)`、详情含 `aec_reference_mode` 与 `duck_quantum_ms=200`；
- **抓到并修掉的真实缺陷**：参考源曾按 `AecReferencePCM` 默认 48 kHz 装配，与 waveout 硬编码
  24 kHz 冲突 → 构造直接抛 `sample_rate_mismatch`。已抽出 `build_same_process_reference()`
  统一按后端时钟装配（`NlmsConfig` 也传同钟），并加 3 项离线回归测试钉住该约束。
- **真机试听按用户 2026-10-02 指示推迟**：本轮**不**开麦克风/扬声器验证听感与打断效果，
  因此**不**声称任何听感、真机打断或真机 ERLE 结论。

- 用户先在 2026-10-02 指示"真机验证先不做"；**同日晚用户改口"开始真机验证"**，随即做了首次真机尝试（见 §7）。

## 7. 真机验证（2026-10-02 首次尝试；**已暴露一个真实边界**）

**首次真机尝试（用户已授权，后台运行真实入口）**：

```powershell
.\.venv\Scripts\python.exe -m app.main --voice --auto --barge-in-mode confirm `
    --aec-reference-mode same_process --device-recovery-mode safe_point --tts-start-timeout 180
```

实测结果（结构化日志 `%LOCALAPPDATA%\ComputerVoiceAssistant\logs\runtime.jsonl`，实例 `4f95376e`）：

- 前置全部通过：TTS 自动拉起成功（pid=18864，57.0s）、输入设备=麦克风阵列（Realtek(R) Audio）（id 1）、
  HUD 已启用、ASR 正常出字（多轮 `silence_timeout`）；
- **但每一轮都是 `reason="text_only"`、`spoken=false`** ⇒ **根本没有进入播报**，
  因此打断通道没有守望对象（`log_details().barge_in_enabled=false`）。

**根因（已定位到代码，不是本批新缺陷）**：`--voice --auto` 在**默认 local 路由**下走
`UnavailableAgentAdapter`（`app/runtime/assistant_runtime.py`：`completed=False` + `failure`）→
`voice_loop.drivers.speech_eligible()` 为假 → `ChatVoiceLoop._speak` **不会被调用** →
无播报、无打断窗口。

**必须记住的边界**：R4 的打断只在**存在真实播报**时才有意义；主程序默认（无真实模型）路径下
`--barge-in-mode` 开与不开都不会触发打断，也不会报错（如实走 `text_only`）。

## 7b. 真机试听命令（待用户决定是否开启真实模型后执行）

```powershell
cd '<PROJECT_ROOT>'
$env:ASSISTANT_PROVIDER = 'deepseek'
$env:ASSISTANT_DEEPSEEK_OPT_IN = 'true'
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
# 播报中插话（确认后停播）+ 同进程参考 + 设备安全点
.\.venv\Scripts\python.exe -m app.main --voice --auto --barge-in-mode confirm `
    --aec-reference-mode same_process --device-recovery-mode safe_point
# 只想看第一阶段降音量：--barge-in-mode duck
```

预期现象：① 不插话时与 `--barge-in-mode off` 无可感知差异；② 播报中说话 → 音量先降到约 25%
（`duck` 模式到此为止）；③ `confirm` 模式持续说话 200~300 ms 后**播报停止**，控制台继续下一轮收音；
④ 咳嗽/敲桌等短噪声不应停播（只会短暂降音量后回升）；⑤ 打断通道不可用只记录原因码、播报继续。

**计费提示**：真实 Provider 每轮 2 次真实请求并计费；失败不回退。
**听感必须由用户确认**；本交接不声称任何"已听到/已生效"。

## 8. 残留与下一步

- **打断的"判别依据"是唯一未解的核心问题**（本机 fail-closed 中）。已做开源调研并写入
  `implementation_plan/research/2026-10-02-realtime-voice-barge-in-open-source-research.md`
  （Moshi / Unmute / Pipecat / LiveKit Agents / TEN / Fay + WebRTC APM 绑定）。要点：
  ① 级联方案一律把 AEC 或耳机当前提；② 没有 AEC 时工程界用"训练出的打断判别器"而非音量阈值；
  ③ 即便有判别器也要有回合边界冷却与最短语音门槛（LiveKit `backchannel_boundary` 默认 1.0 s）；
  ④ 端到端全双工（Moshi）也依赖浏览器 AEC，且需 24 GB 级显存。
  **建议顺序**：A 参考重采样让 `echo_correlation` 可用（最小改动、可离线验证）→ B 引入
  WebRTC APM/SpeexDSP 真 AEC → C 冷却窗口 + 更严门槛（兜底）→ D 打断判别器（长期）。
- **前置音频并入同一 `utterance_id`**（最高优先）：需要给 audio 层加 pre-roll 注入接口
  （`AudioBus` 序号/帧号 + 采集会话计数器一起推进），完成后把 `pre_roll_merged` 改为真实值。
- **输出设备重连**：需要播放链路支持固定输出设备 id（当前 waveOut `WAVE_MAPPER`）。
- **R2b 真机 ERLE**（需外接麦克风）：条件满足后按 `-R2b-2-` §7 复跑；届时才谈
  `echo_correlation` 是否可用与是否切回 `full`。
- **R5 Provider 冷却 / R6 长期运行健康**：另行授权。
- 本批改动**未提交、未 push**。

## 9. 真机缺陷与修复（2026-10-02 晚，用户报"怎么音量一直在变"）

**现象**：开启 `--barge-in-mode confirm` 后，用户在真实播报中听到音量**反复降低又回升**；
日志中 `barge_in_enabled=true` 的播报回合 4 次（第 3/4/9/10 轮，通道帧数 208/532/116/1485），
而 **`barge_in_confirmed=true` 全日志 0 次**。

**根因（逐条可核）**：
1. R1a 第一阶段**一帧**达到 `suspect_threshold(0.6)` 就降音量到 `duck_fraction(0.25)`；
2. 本机参考不可用（24 kHz 播放 vs 16 kHz 采集）⇒ `echo_correlation=None`，**无法把扬声器回声折减**；
3. 第二阶段要**连续 13 帧**（250 ms）≥0.7 才确认；回声产生的零星命中正好落在「≥1 帧、<13 帧」之间
   ⇒ **只降不认**；
4. `release_ms=400` 一到就回升 ⇒ 听感上是音量一直在变。

**修复（已落地）**：
1. **R1a 增 `min_suspect_frames`**（默认 1，保持既有语义；会话层传 **3＝60 ms**）：
   连续命中不足时不降音量、不建立任何疑似事实；`reset`/`release`/播报结束都会清空待定计数。
2. **通道增 `duck_requires_reference`（默认 True）**：没有可用回声参考时**不降音量**，
   只保留"确认后停播"，并如实记录 `duck_suppressed=true` / `duck_disabled_no_reference`；
   显式 `duck_requires_reference=False` 可恢复（供将来调优）。
3. **会话层默认 `release_ms` 400 → 800 ms**（减少往复次数）。

**回归测试（+14 项）**：`BargeInChannelStageTests`（单帧不降/3 帧才降）、
`BargeInNoReferenceDuckTests`（6 项：无参考/异钟不降音量、确认仍可用、显式 opt-out、
**零星回声不再让音量来回跳**、有参考时降音量照旧）、`MinimumSuspectRunTests`（6 项）、
`MonitorWiringTests`（无参考不降音量）。

**修后口径**：本机（16 kHz 采集 / 24 kHz 播放）**第一阶段降音量按设计关闭**，用户能得到的
打断行为是"持续说话 ~300 ms 后停止播报"；要恢复降音量体验必须外接麦克风并让参考可用。

## 9b. 真机第二轮暴露的第二个缺陷：通道检测器缺"窗口粘滞 + 能量门"（已修）

**证据**：修复 §9 后重跑真机（真实 DeepSeek + `confirm`），播报回合 2 次（通道帧数 760/688）
仍然 **`barge_in_confirmed=0`**。

**根因（读源码 + 与主链路对照）**：
1. `SileroVadDetector.observe()` 只在"本帧凑满一个 512 样本窗"时返回 True，而采集帧是 320 样本
   （≈1.6 帧/窗）→ **帧级真/假交替**；因此"连续 13 帧"（`confirm_frames`）与"连续 3 帧"
   （`min_suspect_frames`）都**结构性不可达**；
2. 主链路早就为此做了两件事：① 窗口结论**粘滞**到未满窗的帧；② 人声判定取
   `observed and energy_hit`（检测器管结构、能量管电平）。通道两样都缺。
3. 还差一层风险控制：若只补粘滞而不加能量门，检测器的 latch 会让回声被判成"连续人声"，
   反而**用 AI 自己的声音触发停播**。

**修复**：
1. `app/runtime/auto_voice.py` 新增 `_sticky_window_observe(detector, required_windows, on_hit)`：
   与主链路同口径把窗口结论粘滞到未满窗帧；通道检测器改用它（`required_windows=vad_frames`）。
2. `app/duplex/barge_in_channel.py` 的检测器模式改为**双重条件**：`检测器命中 且 RMS ≥ 能量门`
   才算人声；并在 `to_details()` 暴露 `detector_energy_gate_applied`（可观测）。
   本机实测回声约在能量门之下（`mic_level_vs_floor_db` 仅 9.3 dB），因此不会被自己的声音确认。

**回归（+8 项）**：`StickyWindowObserveTests`（5：粘滞、无命中恒假、latch 语义、
非法参数、`on_hit` 计数）+ `BargeInChannelDetectorTests`（检测器命中但电平低于能量门不算人声、
双重条件满足才确认）。

**基线**：2449 → 2463（§9）→ 2471（§9b）→ **2476 tests OK (skipped=3)**（+141）。

## 9c. 真机第三轮暴露的第三个缺陷：**假确认**（用户"没打断也中止了"）→ fail-closed

**现象**：修好 §9b 后重跑真机（真实 DeepSeek + `confirm`），**用户明确表示自己没有插话**，
但 6 个播报回合全部 `CONFIRMED=True` / `end_reason=barge_in_interrupted`
（通道帧数 81/89/102/107/113/130，即**播报开始后约 1.6~2.6 秒**恒定触发）。

**根因（也是本机硬件结论）**：本机扬声器自己的声音在麦克风处**越过了能量门**，
而 16 kHz 采集 / 24 kHz 播放拿不到可用回声参考 ⇒ **无法区分"用户插话"与"自己的声音"**。
此时"疑似门槛、能量门、确认窗口"怎么调都只是猜；按设计文档 12「无法可靠判断 → 宁可不打断」
与项目"不猜测"原则，不能靠调参硬上。

**修复（fail-closed）**：
1. `AutoVoiceSession._build_barge_in`：AEC 计划判定参考不可用（`reference_available` /
   `reference_matches_output` 任一为 false）或 `pause_capture_during_playback` 为真时，
   **整个打断功能不启用**——不建通道、不包 speaker、播放期不守望输入（半双工），
   如实记 `barge_in_details().unavailable_reason = "no_usable_echo_reference"`；
2. `app/main.py` 新增 `_report_barge_in_status()`：用户显式请求了打断但未启用时，
   **启动即打印原因**（绝不静默假装启用了）；
3. 通道层原有的"无参考不降音量"（§9）保留；`--barge-in-mode off` 时零新增对象不变。

**回归（+5 项）**：无可用参考 → 不建通道/不包 speaker/不打开采集 + 如实原因；
异钟参考同样视为不可用；有可用参考时功能照常启用；`_report_barge_in_status` 三态
（关闭时静默、未启用时报原因、启用时报等级）。

**本机最终口径（如实）**：这台笔记本（内置麦克风阵列 + 内置扬声器）在**外接麦克风或可关闭
DSP 的 AEC 参考可用之前，不具备安全打断条件**；`--barge-in-mode duck|confirm` 在本机等价于
不启用（并打印原因）。R4 的**机制**已在真机端到端验证过（独立通道守望 → 安全门 → `stream.cancel()`
→ 轮次标记 `barge_in_interrupted` 都真实发生），缺的是**可信的判别依据**。

**下一步（基于开源调研，见 §8 与研究文档）**：让参考同钟（重采样）→ 拿回 `echo_correlation` →
再决定是否恢复打断；其次引入 WebRTC APM（AEC3/NS）等真 AEC 前端。→ **已在本文件 §10 落地（批次 R4-A）**。

## 10. 批次 R4-A：把"判别依据"找回来（参考同钟 + 对齐估计 + 回声可观测性安全门）

**背景**：§9c 的 fail-closed 结论是"缺的是可信判别依据"。本批按开源调研（研究文档）的第一
优先级方案 A 实施：**让 AEC 参考与采集同钟**，从而恢复 `echo_correlation`。

| 文件 | 内容 |
|---|---|
| `app/audio/aec/reference_source.py`（改） | `AecReferencePCM.source_sample_rate`：**跨块连续重采样**（尾样本作左锚点 + 绝对样本计数，任意长度块拼接与逐帧提交等价）；详情增 `resampling`/`source_sample_rate`/`resample_ratio`；epoch 变化与 `reset()` 一并复位重采样状态 |
| `app/audio/aec/alignment.py`（新增） | `estimate_echo_alignment()`：粗搜（`np.correlate` + 前缀和归一化挑候选）→ 细搜（±3 ms 逐样本**零均值归一化**相关）；`EchoAlignment(state/correlation/delay_ms/rms/...)`；**最小重叠门槛**（0.15 s 或短侧 20%）挡住"重叠只剩几毫秒时任何两段都完全相关"的假峰 |
| `app/duplex/barge_in_channel.py`（改） | 维护最近 2 s 采集帧、每 25 帧（0.5 s）估一次；`echo_correlation` 改为**对齐后**的相关性（无结论即 `None`）；新增 `echo_delay_ms`/`alignment_state`/`alignment_estimates`/`echo_proven`/`max_echo_correlation`；`echo_proof_correlation`（默认 0.2）判定"看得见自己的回声" |
| `app/runtime/auto_voice.py`（改） | `barge_in_alignment_interval_frames`（25）、`barge_in_echo_proof_correlation`（0.2）；**回声可观测性安全门**：确认插话只在 `channel.echo_proven` 为真时才真正停播，否则记 `barge_in_unproven_echo`；**诚实性修正**：会话只把参考用于对齐/相关性、**不跑回声消除** → 能力一律报 `CAPABILITY_UNAVAILABLE`，R2a 给 `strict_no_aec` + `aec_applied=False` |
| `app/runtime/voice_entry.py`（改） | `build_same_process_reference()` 改为 **16 kHz 时间线 + 24 kHz 提交侧（`source_sample_rate`）**，`NlmsConfig` 同用 16 kHz |
| `tests/duplex/test_aec_alignment.py`（新增，13 项） | 延迟 40/140/300 ms 全命中、无回声低相关、短历史/静音如实、**退化短重叠不得报满相关**、尺度不变性、参数校验、详情无音频；`ReferenceResamplingTests`：1 s 播放→1 s 参考、**大块与逐帧提交时间线一致**、同钟不重采样、epoch 复位、**重采样后的参考经 NLMS 尾部 ERLE 中位数 > 15 dB** |

**顺带修掉的两个真实缺陷（离线测试 + 会话级调试各抓到一次）**：
1. `_build_barge_in` 在 `barge_in_config=None` 时用裸 `BargeInConfig()`，把 §9 修好的
   **`min_suspect_frames=3` / `release_ms=800` 覆盖回 1/400** —— 即第三个缺陷的修复此前在
   **会话路径上其实没生效**；现改为"只有显式给配置才覆盖"；
2. 通道 `duck_suppressed` 判定过宽：`action=idle`（本就不该降音量）的帧也被记成"被抑制"。

**本批口径（如实）**：本机参考经重采样后**同钟**（`reference_matches_output=True`），但会话
**没有真正应用回声消除**，因此 AEC 级别仍是 `strict_no_aec`/`aec_applied=False`；打断能否
真正停播取决于真机实测的**回声可观测性**（相关性 ≥0.2）。

**验证（R4-A 离线）**：全量 **2492 tests OK (skipped=3)**（2476 → 2492，+16）；`compileall -q app tests tools`
exit 0；323 个 .py 编码扫描 0 解码失败 / BOM 25。

## 11. 批次 R4-A 真机实测（2026-10-02 晚，用户在场、真实 DeepSeek）

命令：`--voice --auto --barge-in-mode confirm --aec-reference-mode same_process
--device-recovery-mode safe_point --silence-ms 1200 --tts-start-timeout 180`
（后台作业 `pwsh-118` / `pwsh-123`，均在取足证据后由我主动停止，避免继续计费）。

**启动口径（修复生效）**：`打断已启用：confirm（降级级别 no_aec）` —— 参考经重采样后
`reference_matches_output=True`，会话不再 fail-closed；AEC 级别如实为 `strict_no_aec` /
`aec_applied=False`（我们**没有**跑回声消除，只做对齐 + 相关性）。

**决定性事实（`runtime.jsonl` 的 `voice_turn`）**：
- 有播报的回合（`spoken=true`）里打断通道**确实确认到了插话**：`barge_in_confirmed=true`、
  `barge_in_reason="barge_in_unproven_echo"`、`device_action="skipped"` → **回声安全门把这次
  确认拦下了：`stream.cancel()` 没有发生、播报没有被停**。这正是 §9c 的复现条件（自己的声音
  越过能量门触发确认），但这次**没有误停播**——安全门把"假停播"从结构性可能变成结构性不可能。
- **本机回声在样本级不可相关**：整个 episode 内 `echo_proven=false`，即没有任何一次对齐估计
  的相关性达到 0.2。即使参考已同钟、即使做了 ±600 ms 延迟搜索，麦克风阵列的 DSP/非线性路径
  仍让"自己放的声音"与参考不可相关（与 R2b-2 实测 `mic_alignment≈−0.05` 一致）。
- 因此**方案 A 在本机不足以提供可信判别依据**：能拿到"没有依据"这个结论，却拿不到"依据"。
  本批最有价值的真机收获是**安全门 + 失败即不动手**。
- 首个回合仍出现 `reason="text_only"`/`spoken=false`（§7 已记录的边界：该轮回复不可播报，
  于是没有播报期、也没有打断窗口）；未深究，属既有边界。

**同一次真机暴露并已修掉的两个缺陷**：
1. **控制台编码把语音会话打崩**：GBK(936) 下识别文本出现韩文 `응` → `output_stream.write` 抛
   `UnicodeEncodeError` → 沿 `run_turn` 冒泡 → 整个免按键会话被判"语音输入不可用"并降级为文字
   （真机日志 `voice_entry_unavailable`）。修复：`_safe_write()`（编码不可表示按 `replace` 重试、
   流坏了也不抛）+ `_render_turn`/`_write_line` 改用它；+3 项回归（`ConsoleEncodingIsolationTests`）。
2. **`end_reason` 会谎报"被打断"**：原来用 `confirmed` 判定，于是被安全门拦下（播报其实播完）
   的回合仍被记成 `barge_in_interrupted`。修复：新增"本 episode 是否真的停过播"
   （`_barge_in_stopped_episode`），只有它为真才记 `barge_in_interrupted`；+2 项断言。
3. 另新增真机取证字段（脱敏日志）：`barge_in_echo_proven` / `barge_in_max_echo_correlation` /
   `barge_in_echo_delay_ms` / `barge_in_alignment_estimates`（+1 项测试）。

**A3 离线验收（合成回声）**：40/140/300 ms 延迟全部命中（相关 0.51/0.91/0.97）、无回声 0.04、
大块与逐帧提交时间线一致、**重采样后的参考经 NLMS 尾部 ERLE 中位数 > 15 dB**。

**结论与下一步（方案 B）**：本机要真正"安全打断"需要**真 AEC 前端**（WebRTC APM/AEC3 或
SpeexDSP）承担回声消除，而不是靠相关性折减；在其可用前维持「安全门 fail-closed：只观测、
不动手」——即**不会误停播，也暂时不能真打断**（如实告知用户）。后续顺序（研究文档）：
真 AEC 前端 → 轮次边界冷却（LiveKit `backchannel_boundary` 式）→ 插话判别器。

**最终验证**：全量 **2496 tests OK (skipped=3)**（2476 → 2492 → 2496）；`compileall -q app tests tools`
exit 0；323 个 .py 编码扫描 0 解码失败 / BOM 25。
