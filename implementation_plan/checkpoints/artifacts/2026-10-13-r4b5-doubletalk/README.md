# 证据索引：2026-10-13 R4-B5 真机双讲检测验收（`tools/p10_r4b5_doubletalk_probe.py`）

> **本目录不含任何音频**（探针默认不落盘；用户运行也未带 `--save-wav`）。
> 数值来源：用户在真机运行 `tools/p10_r4b5_doubletalk_probe.py --live-opt-in` 后回贴的控制台输出，
> 由 Lead 转录为 `real-device-numbers.json`（**转录件**，不是工具直接写出的文件）。
> 工具写出的 JSON 在用户侧路径 `artifacts/p10-r4b5/doubletalk.json`（未随仓库保存；如需可重跑复现）。

## 运行参数

| 项 | 值 |
|---|---|
| 时间 | 2026-10-13（本地） |
| 输入设备 | 麦克风阵列 (Realtek(R) Audio) @ 48 kHz |
| 输出 | 扬声器 (Realtek(R) Audio)，生产同款 `waveout` 后端 |
| 素材 | 项目内真实 TTS wav（`--wav` 默认值）× 3 遍 ≈ 6 s |
| 阈值口径 | `speech_threshold=600`、`residual_margin=3.0`、ERLE 门槛 6 dB、对齐窗口 25 帧 |
| 延迟实测 | **362.25 ms**（5796 样本） |

## 三段真值与结果（全部数值见 `real-device-numbers.json`）

| 段 | 真值 | 生产检测器 | APM `speech_probability` |
|---|---|---|---|
| B：只放音 + 保持安静 | 单讲 | **268/268 帧判双讲（误报 100%）** | 中位 0.792 / p90 0.999 / ≥0.8：143/289 |
| C：放音 + 同时说话 | 双讲 | 268/270 帧判双讲（检出 99.3%） | 中位 0.822 / p90 0.995 / ≥0.8：150/289 |
| A：静音 + 用户说话 | 近端-only | 判双讲 **0** 帧（正确） | 中位 0.715 / p90 1.000 / ≥0.8：71/160 |

**结论**：B 与 C 在 4 类可观察量上均不可区分 ⇒ 本机无可用双讲判据（条件性 no-go）。
完整论证见 `implementation_plan/research/2026-10-13-P10-R4B5-doubletalk-real-device-conclusion.md`。

## 复现命令

```powershell
cd '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe tools\p10_r4b5_doubletalk_probe.py --live-opt-in --json artifacts\p10-r4b5\doubletalk.json
# 只验误报（不需要说话）：加 --echo-only
```

## 同批离线证据（可复现，不含真机）

| 证据 | 位置 |
|---|---|
| 双讲检测器（47 项单测） | `app/audio/aec/double_talk.py`、`tests/duplex/test_double_talk.py` |
| 接线与事实（21 项） | `tests/duplex/test_double_talk_wiring.py` |
| 保护动作（18 项） | `tests/duplex/test_double_talk_protect.py` |
| 冻结代价对照（4 项） | `tests/duplex/test_double_talk_erle_tradeoff.py` |
| 探针自检（5 项） | `tests/foundation/test_r4b5_probe_selfcheck.py` |
