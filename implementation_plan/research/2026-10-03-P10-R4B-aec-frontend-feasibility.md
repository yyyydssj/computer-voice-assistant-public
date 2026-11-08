# P10 R4-B 前置：真 AEC 前端（方案 B）只读可行性核查

- 建立时间：2026-10-03
- 上游：`implementation_plan/checkpoints/2026-10-02-P10-R4A-handoff-resume.md`（§5 方案 B 决策、§9 恢复清单）
- 前序调研：`implementation_plan/research/2026-10-02-realtime-voice-barge-in-open-source-research.md`（§4 方案 A/B/C/D）
- **本轮性质：只读核查**。未安装任何依赖、未改环境、未改代码、未动真机。
- 唯一产出：本文件（新建）。基线复跑证据见 §2。

---

## 1. 结论速览

| 候选 | 本机 Py3.12/Win 可安装 | 许可 | 决策 |
|---|---|---|---|
| **pywebrtc-audio 0.2.0** | ✅ 有 `cp312-cp312-win_amd64` wheel，免编译 | Apache-2.0（仓库），NOTICE 未列 vendored | **首选**：真 AEC3 + NS + AGC + VAD |
| **pyaec 1.0.1** | ✅ 有 `py3-none-win_amd64` wheel，免编译、无依赖 | MIT（上游 aec-rs），内含 speexdsp（BSD-3） | **次选/兜底**：speexdsp MDF 自适应滤波 |
| webrtc-audio-processing 0.1.3 | ❌ 只有 cp27/cp36 的 linux_armv7l wheel | BSD | 排除（须自行编译） |
| webrtc-apm 0.1.6 | ⚠️ 只有 `cp311-win_amd64`，3.12 装不上 | MIT | 排除（无 cp312 wheel） |
| speexdsp 0.1.1（2018-07-17 发布） | ❌ 仅 sdist，需 swig + 编译工具链 + libspeexdsp-dev | BSD | 排除（Windows 上成本过高） |
| speexdsp-ns 0.1.2 | ❌ 只有 manylinux wheel，且**无 AEC（仅 NS）** | BSD | 排除 |

**结论**：方案 B 在**不装编译器**的前提下有两条可用路线；首选 `pywebrtc-audio`（真正工业级 AEC3），
`pyaec` 作为许可更宽松、体积更小的兜底。两条都必须**先离线接、再真机**；真机 ERLE 未达标前
不得声称"已消除回声"。

---

## 2. 本轮基线复跑（实测，非引用）

| 项 | 实测值 |
|---|---|
| 命令 | `.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q` |
| 结果 | **Ran 2496 tests in 321.948s → OK (skipped=3)**，exit code 0 |
| 墙钟 | 324.1 s |
| 与交接文档差异 | 文档记 ~82 s；本机当前约 **4 倍**（VoiceStudio 常驻 pid 10344/20720 + 冷跑）。**条数一致，耗时数字不可直接引用** |
| 环境副作用 | 无：`pip list` 无 `pywebrtc`/`pyaec`；`find_spec` 均为 `None` |

本轮只执行了 `pip install --dry-run --no-cache-dir --only-binary=:all: <pkg>`，**未安装**。

---

## 3. 本机环境事实

| 项 | 值 |
|---|---|
| 解释器（venv） | `<PROJECT_ROOT>\.venv\Scripts\python.exe` → 3.12.10 AMD64 |
| 基础解释器（venv 启动器实际拉起） | `<USER_HOME>\AppData\Local\Programs\Python\Python312\python.exe` |
| pip | 25.0.1 |
| numpy（venv 内） | 2.5.3（已满足 `pywebrtc-audio` 的 `numpy>=1.24`） |
| `cl.exe` | **不在 PATH**（vswhere 报出的安装路径为 `<本地目录>\C++`，本轮未使用） |
| 依赖管理约定 | 根目录**没有** `requirements*.txt`/`pyproject.toml`/lock（glob 无命中），但项目有**既定约定**：离线 wheel 放 `.wheelhouse/`（已 gitignore）+ **SHA-256 登记**（`NEXT-SESSION.md` §2 表、P8 验收文档 6.1、各批次 checkpoint）。`.wheelhouse/` 现有 9 个 wheel（含 `numpy-2.5.3-cp312-cp312-win_amd64.whl`）；有 `.python312-embed.zip` → 对外分发用嵌入式 3.12 |
| 结论 | 新增依赖**沿用既有约定**：固定版本 + wheel 入 `.wheelhouse/` + 登记 SHA-256/许可证/离线安装命令（`pip install --no-index --find-links .wheelhouse`） |

---

## 4. 六项核查（逐项证据）

### 4.1 能否在本机 Py3.12 + pip 上安装

`pywebrtc-audio` 0.2.0（上传时间 2026-09-03T19:02:25Z）：

```
pip install --dry-run --no-cache-dir --only-binary=:all: pywebrtc-audio
  Downloading pywebrtc_audio-0.2.0-cp312-cp312-win_amd64.whl.metadata (14 kB)
  Requirement already satisfied: numpy>=1.24 ... (2.5.3)
  Downloading pywebrtc_audio-0.2.0-cp312-cp312-win_amd64.whl (513 kB)
  Would install pywebrtc-audio-0.2.0
```

→ 有 Windows x86_64 预编译 wheel（py3.10–3.14 全覆盖），运行时依赖仅 numpy，**不需要编译器**。

`pyaec` 1.0.1（上传时间 2024-12-08）：

```
Downloading pyaec-1.0.1-py3-none-win_amd64.whl.metadata (320 bytes)
Downloading pyaec-1.0.1-py3-none-win_amd64.whl (82 kB)
Would install pyaec-1.0.1
```

→ `py3-none-win_amd64`（与 CPython 小版本无关），**不需要编译器**，无第三方依赖。

### 4.2 许可

- `pywebrtc-audio`：仓库 `LICENSE` = **Apache-2.0**；`NOTICE` 正文仅一行
  "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."，
  **没有**枚举随源码 vendored 的组件；仓库内实际带有
  `vendor/webrtc_audio/PATENTS`、`vendor/webrtc_audio/abseil/LICENSE`、`vendor/webrtc_audio/jsoncpp/LICENSE`。
  → 内部使用无损；**若对外分发**，需自行补齐 WebRTC APM（BSD-3）等第三方声明（NOTICE 不完整是已知合规小缺口）。
- `pyaec` 1.0.1：上游 `thewh1teagle/aec-rs` = **MIT**，内部捆绑 speexdsp（BSD-3），同样需要保留出处声明。

### 4.3 是否支持"提交侧 24 kHz → 目标 16 kHz"的参考对齐

- APM/`EchoCanceller` 实例**只绑定一个采样率**，near（采集）与 far（参考）必须同率同长度
  （上游测试 `test_process_mismatched_lengths` 断言长度不一致抛 `ValueError`；`EchoCanceller(sample_rate=8000)` 抛 `ValueError`）。
- 因此**提交侧 24 kHz 必须重采样到 16 kHz**。项目 R4-A 已有该项能力：
  `AecReferencePCM.source_sample_rate` + `SameProcessAecReference._resample_to_target_clock()`
  （跨块连续重采样：尾样本作左锚点 + 绝对样本计数，任意长度块与逐帧提交等价），
  以及按 20 ms 切帧的时间线（`frame_samples=320`）。**无需新造重采样/分帧**。
- far 信号必须是**实际送往设备**的 PCM：已满足——`WaveOutPCMStreamBackend.submit()` 先按 duck 增益
  `_scale_pcm16`，再以 `payload` 通知 `submit_observer`（`app/tts/waveout_pcm.py:287 / :322-345`）。

### 4.4 分帧与延迟

读的是绑定源码（`bindings/webrtc_audio_bindings.cpp`）与类型桩（`src/pywebrtc_audio/_webrtc_audio.pyi`），非 README 转述：

- `frame_size_ = sample_rate / 100` → 16 kHz 下 **10 ms = 160 样本**；
- 输入长度非帧整数倍时 `padded_len = ceil(original_len / frame_stride_) * frame_stride_`，
  即**零填充到帧整数倍**后逐 10 ms 帧处理，输出再按原长度拷贝（`copy_len = min(frame_stride_, ...)`）。
  → 本项目采集帧 **320 样本（20 ms）= 恰好 2 帧，无填充损失**；
- `process(near, far)` 为**同步调用、立即返回**，不是异步/流式缓冲，不引入额外整块延迟；
- `stream_delay_ms` 可构造时设置、也可运行时改（`ap.stream_delay_ms = 50`），作为延迟提示；
- 真机链路延迟仍由播放后端缓冲（本机 waveOut 200 ms 粒度）+ 声学路径决定；AEC3 自带延迟估计，
  但**参考必须每帧都喂**（含静音期）才能维持自适应收敛。
- 上游 e2e 示例口径：`RATE=16000, FRAME_SIZE=160`（10 ms）；
  `pyaec` 示例口径：`Aec(frame_size=160, filter_length=1600, sample_rate=16000, True)`。

### 4.5 CPU 与实时余量

上游 `benchmarks/BENCHMARK.md`（Apple M3 Pro / macOS / Py3.12.12 / numpy 2.2.6，16 kHz int16）：

| 组件 | 10 ms 帧 | 实时倍数 |
|---|---|---|
| EchoCanceller | 60 µs | 161× |
| AudioProcessor(AEC+NS+AGC) | 62 µs | 154× |
| AudioProcessor(全特性) | 64 µs | 146× |

→ 20 ms 帧约 120–260 µs，占单核 <3%，**余量充足**。
**本机 CPU 未实测**（需先安装才可测，属"未做"项，见 §7）。

### 4.6 离线可测性

- ✅ 纯内存 API：numpy `int16`/`float32` 进、同 dtype 出，不访问设备/文件/网络。
- 上游自带 pytest（`tests/test_echo_canceller.py`：200 帧后输出能量低于输入、任意长度、reset 后与新建实例逐样本一致等）。
- 本项目可沿用 R4-A 的离线验收口径：合成延迟回声（40/140/300 ms）→ ERLE 断言；无回声直通；静音；
  帧间连续性；`reset`/epoch 幂等；**全程不需要麦克风**。
- 真机 ERLE 仍**必须真机 + 用户在场**（内置麦克风阵列 DSP 的非线性路径离线不可复现）。

---

## 5. 集成点定位（只读）

| 位置 | 现状 | AEC3 接入方式 |
|---|---|---|
| `app/audio/aec/reference_source.py` | `SameProcessAecReference`：提交旁路 → 重采样 → 20 ms 时间线 → `window()` → `NlmsEchoCanceller.cancel()` | **替换消除器后端即可**，重采样/切帧/时间线/epoch 复位全部保留 |
| `app/tts/waveout_pcm.py:287,322-345` | 先应用 duck 增益，再通知 observer（post-gain payload） | 不改（保证 far = 实际播放） |
| `app/runtime/voice_entry.py` | `build_same_process_reference()`：16 kHz 时间线 + 24 kHz 提交侧 | 不改时钟口径 |
| `app/audio/aec/__init__.py:182-200` | `CAPABILITY_SOFTWARE` + `reference_ok` + `echo_path ≤ 120 ms` → `LEVEL_FULL(aec_ns)`/`aec_applied=True` | 可直接承接；**但需新增"实测 ERLE 达标才允许声称已消回声"的诚实性门** |
| `app/duplex/barge_in_channel.py` | `echo_proven`（对齐相关性 ≥ `echo_proof_correlation=0.2`）安全门 | AEC3 可用后，判别依据应从"相关性"升级为"消除后的残余能量"，安全门语义需同步修订 |

---

## 6. 风险与不可承诺项

1. **ERLE 不可承诺**：本机内置麦克风阵列 DSP（波束成形/NS/非线性）可能使 AEC3 的 ERLE 显著低于耳机/外接麦场景。
   离线合成回声只能证明"实现正确"，**不能证明本机能消掉回声**；因此"真打断"必须在真机 ERLE 达标后才启用。
2. **诚实性门缺口**：现有判定只要"capability + 参考同钟"就置 `aec_applied=True`；
   接入真 AEC 后必须补"实测 ERLE 未达标 → 如实降级、不声称已消除回声"（延续 §7 不变量 7）。
3. **延迟预算冲突**：`AecPolicy.max_echo_path_ms=120` 与 waveOut 200 ms 缓冲粒度不匹配，
   需按实测重新定档或改用 AEC3 自身延迟估计。
4. **供应方风险**：0.2.0 发布于 2026-09-03，仓库 19 stars、单一组织（strands-labs）；
   项目用 `.wheelhouse/` + SHA-256 登记、**没有 lock 文件** → 建议按既有约定固定版本
   （wheel 入 `.wheelhouse/`、登记 SHA-256/许可证/离线安装命令），`pyaec` 轮子 2024-12-08 更新，作为兜底。
5. **NOTICE 不完整**（见 §4.2）：仅影响对外分发合规，不影响内部使用。

---

## 7. 未做（**不得伪造**）

- **未安装任何依赖**，未改 `site-packages`、未改环境变量、未改代码；
- **未实测本机 CPU 占用**与单帧耗时（§4.5 的数字来自上游 M3 Pro 基准）；
- **未做真机验证**：本机 AEC3 的实际 ERLE、是否达到可安全打断的门槛，均未知；
- 未评估 `pyaec`/speexdsp 在本机的实际抑回效果（仅确认可安装、许可与 API）；
- 未接入 `AecCapabilityProbe`/打断通道（本轮范围只到"可行性"）。

---

## 8. 建议的下一步顺序

1. **R4-B-1（离线）**：新增可选 AEC3 后端（默认关闭、能力如实上报），替换 `NlmsEchoCanceller` 位置；
   离线用例：合成回声 ERLE、无回声直通、静音、帧间连续性、reset/epoch；
   跑全量回归 + `compileall` + 编码扫描。
2. **R4-B-2（真机，用户在场）**：真机测 ERLE 与误停播/漏打断矩阵；ERLE 未达标则维持 fail-closed（只观测、不动手）。
3. 之后才轮到研究文档里的 C（轮次边界冷却）与 D（插话判别器）。

---

## 9. 参考链接（本轮实抓或实检）

- `pywebrtc-audio` PyPI：<https://pypi.org/pypi/pywebrtc-audio/json>；仓库：<https://github.com/strands-labs/pywebrtc-audio>
  （实抓 `README.md`、`src/pywebrtc_audio/_webrtc_audio.pyi`、`bindings/webrtc_audio_bindings.cpp`、`tests/test_echo_canceller.py`、`benchmarks/BENCHMARK.md`、`examples/e2e_speech.py`、`LICENSE`、`NOTICE`）
- `pyaec` PyPI：<https://pypi.org/pypi/pyaec/json>；上游：<https://github.com/thewh1teagle/aec-rs>（实抓 `examples/python-examples/wav.py`）
- `webrtc-audio-processing` PyPI：<https://pypi.org/pypi/webrtc-audio-processing/json>
- `webrtc-apm` PyPI：<https://pypi.org/pypi/webrtc-apm/json>
- `speexdsp` PyPI：<https://pypi.org/pypi/speexdsp/json>（xiongyihui/speexdsp-python）
- `speexdsp-ns` PyPI：<https://pypi.org/pypi/speexdsp-ns/json>
