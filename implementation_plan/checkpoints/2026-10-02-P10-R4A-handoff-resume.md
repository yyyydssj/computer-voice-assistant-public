# 电脑语音助手 · P10 R4-A 交接与恢复指引（一劳永逸版）

- 建立时间：2026-10-02（P10 批次 R4-A 收口）
- 作用：**新会话从这里开始，不需要重新推导**。读完本文件即可接着干。
- 上游细节：`implementation_plan/checkpoints/2026-10-02-P10-R4-session-loop-handoff.md`（§1–§11，含全部缺陷与证据）

---

## 1. 一句话状态

R4-A（方案 A：把 AEC 参考重采样到采集时钟 + 对齐/相关性估计 + 回声可观测性安全门）**已实现、
已离线验收、已真机实测**。结论：**工程链路成立，但本机（内置麦克风阵列 + 内置扬声器）的回声在
样本级不可相关**，方案 A 拿不到可信判别依据 → **本机暂时不会误停播、也暂时不能真打断**。
**下一步决策：走方案 B（真 AEC 前端）**，在它可用之前维持 fail-closed（只观测、不动手）。

---

## 2. 环境与基线（照抄即可复现）

| 项 | 值 |
|---|---|
| 项目根 | `<PROJECT_ROOT>` |
| Python | `<PROJECT_ROOT>\.venv\Scripts\python.exe`（3.12） |
| 分支 / 远端 | `codex/review-playback-lifecycle` → `https://github.com/yyyydssj/computer-voice-assistant.git`（**已 uncommitted 全部提交到本地，未 push**） |
| 全量测试 | `python -m unittest discover -s tests -t . -p "test_*.py" -q` → **Ran 2496 tests, OK (skipped=3)**（约 82 s） |
| 编译检查 | `python -m compileall -q app tests tools` → exit 0 |
| 编码扫描 | 323 个 .py：0 解码失败；BOM 25（与基线一致，别动） |
| 运行时日志 | `<USER_HOME>\AppData\Local\ComputerVoiceAssistant\logs\runtime.jsonl` |
| 真机命令 | `$env:ASSISTANT_PROVIDER='deepseek'; $env:ASSISTANT_DEEPSEEK_OPT_IN='true'; $env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User'); .\.venv\Scripts\python.exe -m app.main --voice --auto --barge-in-mode confirm --aec-reference-mode same_process --device-recovery-mode safe_point --silence-ms 1200 --tts-start-timeout 180` |
| 常驻进程 | pid **21300** = VoiceStudio（用户明确要求保留，**不要杀**）；pid 7888 为既有进程。真机作业 **pwsh-118 / pwsh-123 已由我主动停止** |

---

## 3. R4-A 交付物（代码）

| 文件 | 关键点 |
|---|---|
| `app/audio/aec/alignment.py`（新） | `estimate_echo_alignment()`：粗搜（`np.correlate` + 前缀和归一化挑候选）→ 细搜（±3 ms 逐样本**零均值归一化**相关）；**最小重叠门槛** 0.15 s 或短侧 20%（否则"重叠只剩几毫秒"会给出假的 ±1 相关）；返回 `state/correlation/delay_ms/rms` |
| `app/audio/aec/reference_source.py`（改） | `AecReferencePCM.source_sample_rate` + **跨块连续重采样**（尾样本左锚点 + 绝对样本计数；200 ms 大块与逐帧提交得到同一条时间线）；同钟时**不重采样**（字节级不变） |
| `app/duplex/barge_in_channel.py`（改） | 最近 2 s 采集帧 + 每 25 帧（0.5 s）估一次；`echo_correlation` = **对齐后**相关性（无结论即 `None`）；`echo_delay_ms`/`alignment_state`/`echo_proven`/`max_echo_correlation`；`echo_proof_correlation`（默认 0.2）；`duck_requires_reference` 保留 |
| `app/runtime/auto_voice.py`（改） | `barge_in_alignment_interval_frames`（25）、`barge_in_echo_proof_correlation`（0.2）；**回声可观测性安全门**（未证明就不动手，记 `barge_in_unproven_echo`）；`_barge_in_stopped_episode` 决定是否真记 `barge_in_interrupted`；AEC 能力如实报 `CAPABILITY_UNAVAILABLE`（不伪装已消除回声）；`_safe_write` 隔离控制台编码故障 |
| `app/runtime/voice_entry.py`（改） | `build_same_process_reference()`：**16 kHz 时间线 + 24 kHz 提交侧（`source_sample_rate`）**，`NlmsConfig` 同用 16 kHz |

**关键常量**：采集 16 kHz / 20 ms / 320 样本 / 640 B；播放后端 `app/tts/waveout_pcm.py` 硬编码
24 kHz（提交侧）；确认插话需连续 `min_suspect_frames-1+confirm_frames = 2+13 = 15` 帧（300 ms）；
降音量片段 0.25、粒度 = 后端缓冲 200 ms；能量门 = `speech_threshold` 600；本机麦克风噪声底
p50≈47.7 / max≈150。

---

## 4. 验证证据（可复核）

### 4.1 离线（`tests/duplex/test_aec_alignment.py`，13 项）
- 合成回声延迟 **40 / 140 / 300 ms 全部命中**（相关 0.51 / 0.91 / 0.97），无回声对照 0.04；
- 1 s 的 24 kHz 播放 → 精确 16000 样本 @16 kHz（50 帧）；**大块与逐帧提交时间线一致**；同钟不重采样；
- **重采样后的参考经 NLMS：尾部 ERLE 中位数 > 15 dB**（A3 的关键验收）；
- 退化短重叠不得报满相关（真坑回归）。

### 4.2 真机（2026-10-02 晚，用户在场，真实 DeepSeek，作业 pwsh-118 / pwsh-123）
- 启动口径：`打断已启用：confirm（降级级别 no_aec）`（参考同钟后 `reference_matches_output=True`，
   AEC 级别如实 `strict_no_aec` / `aec_applied=False`）。
- 有播报的回合：`barge_in_confirmed=true` 且 `barge_in_reason="barge_in_unproven_echo"`、
  `device_action="skipped"` → **安全门拦下了假停播，`stream.cancel()` 未发生**。
- 整个 episode `echo_proven=false` → **没有任何一次对齐相关性 ≥ 0.2**（与 R2b-2 的
  `mic_alignment≈−0.05` 一致：麦克风阵列 DSP/非线性路径使"自己放的声音"与参考不可相关）。
- 首个回合仍 `reason="text_only"` / `spoken=false`（§7 已记录的既有边界：该轮回复不可播报 →
  没有播报期也就没有打断窗口），未深究。

### 4.3 真机暴露并已修掉的缺陷（都要保留回归）
1. **GBK 控制台把语音会话打崩**：识别文本含韩文 `응` → `UnicodeEncodeError` 冒泡 → 整个免按键
   会话被判"语音输入不可用"并降级文字。修复：`_safe_write()`；回归 `ConsoleEncodingIsolationTests`（3 项）。
2. **`end_reason` 谎报"被打断"**：改用 `_barge_in_stopped_episode` 判定；回归 2 处断言。
3. `_build_barge_in` 在未显式给配置时用裸 `BargeInConfig()`，把修好的 `min_suspect_frames=3` /
   `release_ms=800` **覆盖回 1/400**（第三个真机缺陷的修复此前在会话路径未生效）。
4. 通道 `duck_suppressed` 判定过宽（`action=idle` 也被记成"被抑制"）。

---

## 5. 决策：下一步走**方案 B（真 AEC 前端）**

**为什么**：方案 A 只能"看见有没有回声"，本机连回声都看不见；相关性折减这条路在本机没有原料。
要真正安全打断，必须让**回声先被消掉**，而不是靠事后折减。

**候选路线（下一批先做只读可行性核查，不装依赖、不改环境）**：
1. **WebRTC APM / AEC3**（`webrtc-audio-processing` 的 Python 绑定，如 `pywebrtc-audio` 一类）——
   工业级 AEC3 + NS + AGC，能直接吃 16 kHz 单声道 + 参考信号；
2. **SpeexDSP**（`speexdsp` / `pyspeexdsp` MDF 自适应滤波）——体积小、许可宽松、易打包；
3. 兜底：**轮次边界冷却**（LiveKit `backchannel_boundary` 式，播报开始后 N 秒内不认插话）+
   更严格阈值（研究文档已记录）。

**核查清单（下一批第一步产出）**：能否在本机 Py3.12 + pip 上安装（含 wheel/编译条件）、
许可、是否支持"提交侧 24 kHz → 目标 16 kHz"的参考对齐、单帧延迟与 CPU 占用、离线可测性。
**先离线测，再真机**；真机必须用户在场且授权真实模型。

**在方案 B 可用之前的口径（不要改动）**：`--barge-in-mode duck|confirm` 在本机 =
「安全门 fail-closed：只观测、不动手」——**不会误停播，也暂时不能真打断**；启动会打印原因/等级。

---

## 6. 未做 / 已知边界（**不得伪造**）

- 前置音频 **未**并入下一轮 ASR：`pre_roll_merge_unsupported`（需要音频层 pre-roll 注入接口，否则
  总线 `frame_discontinuity` 会打掉整轮）；
- 输出设备重连不可用：waveOut 走 `WAVE_MAPPER`，没有固定输出设备 id → 按用户决定回退文字；
- R5 Provider 冷却、R6 长跑健康检查未做；
- R2b 的真实 ERLE 需要外接麦克风才能测（本机耳机/阵列 DSP 不可控）。

---

## 7. 硬规则与踩过的坑（新会话必读）

1. **每次写文件都要先给方案等确认**（五要素：做什么/为什么/影响范围/步骤/风险与回滚）；
2. **严格 UTF-8 无 BOM**；命令行传中文进 `python -c` 极易被 PowerShell 弄坏 → 多行脚本用
   `write` 落盘再执行，或改用 edit 工具做定点替换；
3. **不要用 `Path.write_text` 写文档**（会引入 BOM/CRLF）：用 `read_text` → `replace('\r\n','\n')` →
   `write_bytes(..., encoding='utf-8')`；
4. git 有 `core.autocrlf`，`git add` 会警告 LF→CRLF，属正常（仓库 blob 仍是 LF）；
5. **不要杀别人的进程**（pid 21300 VoiceStudio 是用户要求保留的）；
6. 真机运行必须**用户在场**且授权真实模型（每次对话有真实计费）；我起过的作业要自己收；
7. 诚实性不变量（都有测试断言）：打断永不自我授权；不可用能力永不伪装；**没有可用参考不降音量**；
   前置音频并入 ASR 一律如实报"不支持"；**没证明"看得见自己的回声"就不真停播**；
8. 每批必须：全量测试 + `compileall` + 编码扫描 + 文档同步（NEXT-SESSION / task_plan / progress /
   roadmap + 本目录 checkpoint）。

---

## 8. 本次固化（本地提交，未 push）

| commit | 内容 |
|---|---|
| `49992e7` | 补齐 P0-P9 阶段验收与 P9 交接文档，统一编码为 UTF-8 无 BOM |
| `8545881` | P10 R1a/R1b：两阶段打断状态机、编排与安全门 |
| `bfbb728` | P10 R2a/R2b：AEC 逐层降级、逐样本 NLMS、同进程参考源（含重采样）与真机探针证据 |
| `e66b985` | P10 R3：设备恢复策略 |
| `aced714` | P10 R4/R4-A：会话循环接线、播放增益、参考同钟与回声对齐/可观测性安全门（含真机验证与开源调研） |
| `c48200a` | 同步 NEXT-SESSION/task_plan/progress/路线图（全量 2496 项基线） |
| 本文件 | 交接与恢复指引（本次新增） |

回退方式：`git reset --hard <上一个 commit>`（例：回退本文件 `git reset --hard c48200a`）。
**push 需用户单独同意**。

---

## 9. 新会话开场 5 步

1. 读本文件 + `implementation_plan/checkpoints/2026-10-02-P10-R4-session-loop-handoff.md` §10/§11；
2. `git log --oneline -8` 与 `git status --short`（应为 clean）确认从哪个提交继续；
3. 跑一次基线：`python -m unittest discover -s tests -t . -p "test_*.py" -q`（期望 **2496 OK / skipped=3**）；
4. 若要继续功能：**方案 B 只读可行性核查**（WebRTC APM / SpeexDSP 路线，见 §5），先给五要素方案；
5. 若要动真机：确认用户在电脑前 + 是否允许真实模型计费，再跑 §2 的命令。
