# P10 批次 R4-B-1 交接：真 AEC 前端（WebRTC APM/AEC3）后端接入（**默认关闭**）

- 建立时间：2026-10-03
- 上游：`checkpoints/2026-10-02-P10-R4A-handoff-resume.md`（§5 方案 B 决策）、
  `research/2026-10-03-P10-R4B-aec-frontend-feasibility.md`（本批的只读核查结论）
- 权威恢复指引：`checkpoints/2026-10-02-P10-R4A-handoff-resume.md` §9（新会话 5 步）

---

## 1. 一句话结论

按方案 B 的核查结论，**真 AEC3 后端已离线就位**：可选依赖 `pywebrtc-audio==0.2.0` 已冻结进
`.wheelhouse/` 并离线安装；`app/audio/aec/webrtc_backend.py` 提供与 NLMS **同构**的消除器接口；
`SameProcessAecReference(backend="webrtc")` 可切换后端（**默认仍是 nlms，行为不变**）。
离线验收：合成回声累计 ERLE **23.3 dB**（尾部 20.6 dB）、**本机 0.157 ms/帧 ≈ 127× 实时**、
20 ms 与 2×10 ms **逐样本等价**；全量 **2525 tests OK (skipped=3)**。
**尚未接线到会话**，**未做真机验证**——本机能否真消掉回声仍未知（R4-B-2）。

---

## 2. 依赖冻结（按项目既有约定：`.wheelhouse/` + SHA-256 登记）

| 项 | 值 |
|---|---|
| 包 | `pywebrtc-audio==0.2.0`（Apache-2.0；提供 WebRTC **AEC3** 的 Python 绑定） |
| wheel | `.wheelhouse/pywebrtc_audio-0.2.0-cp312-cp312-win_amd64.whl`（513,391 字节） |
| SHA-256 | `0dabbdadd5d7fd7dcb88323266e5e14d46aaa136c58fa0573c4b0323b683c80b` |
| 校验 | 与 PyPI 官方 `digests.sha256` **逐字符一致**（本轮实测） |
| 运行时依赖 | 仅 `numpy>=1.24`（本机 2.5.3 已满足） |
| 安装命令 | `.\.venv\Scripts\python.exe -m pip install --no-index --find-links .wheelhouse pywebrtc-audio==0.2.0` |
| 未安装 | `pyaec`（只做兜底评估，**未装**） |

许可提示：仓库 `LICENSE` = Apache-2.0，但 `NOTICE` 只写了 Amazon 版权、**未列**随源码 vendored 的
WebRTC/abseil/jsoncpp；内部使用无损，**对外分发需自行补齐第三方声明**。

---

## 3. 交付物

| 文件 | 内容 |
|---|---|
| `app/audio/aec/webrtc_backend.py`（新，~470 行） | `WebrtcAecConfig`（采样率/帧长/`stream_delay_ms`/`erle_gate_db`；帧长必须 10 ms 整数倍）、`WebrtcEchoCanceller`（与 `NlmsEchoCanceller` **同构**：`cancel/reset/cumulative_erle_db/frames_processed`，另有 `available/available_reason/suppression_evidence/stream_delay_ms/backend_errors`）、`load_webrtc_module()`、`webrtc_backend_available()` |
| `app/audio/aec/reference_source.py`（改） | 新增 `backend="nlms"|"webrtc"` 与 `webrtc_config`；未知后端 → `unknown_aec_backend`；样率/帧长不匹配 → `sample_rate_mismatch`/`frame_duration_mismatch`；`to_details()` 增 `aec_backend`；**默认 `nlms`，既有行为字节级不变** |
| `tests/duplex/test_aec_webrtc_backend.py`（新，29 项） | 配置校验、依赖缺失/构造失败/调用异常三种如实回退、假实现契约（每帧都调底层、双讲也调、reset 透传、延迟参数下达）、真实现（回声抑制/静音/参考静音/无回声只是"有抑制"/20 ms≡2×10 ms/reset 幂等/实时余量）、接线（默认 nlms、未知后端拒绝、webrtc 可选用重采样链路跑通） |
| `tools/p10_r4b_aec_bench.py`（新） | 离线基准：合成回声 → ERLE/`applied`/耗时；`--check-continuity` 验证 20 ms 与 2×10 ms 等价；只在显式 `--json` 时写数值摘要（无音频） |

---

## 4. 关键设计（诚实性优先）

1. **可选依赖，绝不崩**：未安装 `pywebrtc_audio` 时模块仍可导入；`available=False`、
   `available_reason=webrtc_dependency_missing`，`cancel()` 如实直通。底层**构造失败**→
   `webrtc_backend_error`（记 `init_error`）；底层**调用异常**→ 直通 + `backend_errors` 计数，
   **异常绝不冒泡进采集/播放路径**。
2. **`applied` 只是处理事实**：它表示"本帧输出来自 AEC3"，**不是**"回声已消除"。
3. **ERLE 口径改写**：AEC3 用 **采集能量/残差能量**（真实声学增益未知）；NLMS 仍是
   **参考能量/残差能量**。两者读数**不可混用**（已写进 `reference_source.process()` 文档）。
4. **`suppression_evidence` 分级**（固定短码，**不含"已确认回声路径"档**）：
   `no_processing` / `reference_energy_insufficient` / `suppression_only_without_echo_path_proof` /
   `suppression_below_gate`。判定"能否声称已消除回声"必须结合**外部证据**（对齐相关性、真机 ERLE），
   属 R4-B-2 范围。
5. **参考无能量不应用**：参考帧能量为 0 → `fallback_reason=reference_silent` 直通，
   不用 AEC3 的残余抑制去动近端语音。
6. **连续喂帧**：双讲帧与静音帧**仍然调用底层**（否则 AEC3 自适应状态中断）。

---

## 5. 证据

### 5.1 本机离线实测（`tools/p10_r4b_aec_bench.py`，Windows 11 / Python 3.12.10 / 16 kHz / 20 ms 帧）

| 指标 | 值 |
|---|---|
| 合成回声（40 ms 延迟、0.5 增益、噪声 rms 100） | 累计 ERLE **23.32 dB**，尾部 **20.62 dB**，`erle_sufficient=true`，300/300 帧无异常 |
| 单帧耗时 | 中位 **0.157 ms**、p95 0.200 ms、max 0.573 ms → **≈127× 实时** |
| 20 ms 与 2×10 ms 等价性 | **identical=true**（64,000 样本，0 个不同，max_abs_diff=0） |

### 5.2 **离线证伪了一个会误判的假设**（诊断实验，200 帧/场景）

| 场景 | 累计 ERLE | 输出能量 |
|---|---|---|
| far 静音 + 近端噪声 | 0.07 dB（0/200 applied） | 5.4 → 5.4（不动） |
| far 有能量 + 近端**完全无关**噪声（**没有回声路径**） | **11.73 dB**（200/200 applied） | 5.4 → 0.4 |
| far 有能量 + 真回声（0.5/40 ms + 噪声） | **25.64 dB** | 132.8 → 0.4 |

⇒ **只要参考有能量，AEC3 就会把采集压到"残余抑制地板"，即使根本没有回声路径。**
所以"ERLE 达标"**不能**作为"回声路径已消除"的判据——这正是上面第 4 条的设计依据，
并已写成测试（`test_uncorrelated_capture_with_active_reference_proves_suppression_only`、
`test_evidence_states_never_include_echo_path_confirmation`）。

### 5.3 测试与工程门

| 项 | 结果 |
|---|---|
| 新模块专项 | `tests.duplex.test_aec_webrtc_backend` **29 项 OK** |
| AEC 既有专项 | `test_aec_reference_source` + `test_aec_nlms` + `test_aec_alignment` + `test_aec_degradation` + 新模块合计 **161 项 OK** |
| **全量** | **Ran 2525 tests in 231.239s → OK (skipped=3)**，exit 0（2496 → 2525，+29 全部为本批新增） |
| `compileall -q app tests tools` | exit 0 |
| 编码扫描 | 326 个 .py：**0 解码失败**、BOM **25**（与基线一致）；本批 4 个文件均**无 BOM、纯 LF** |

---

## 6. 未做 / 已知边界（**不得伪造**）

1. **未接线**：`auto_voice.py`/`barge_in_channel.py`/`main.py` 均未改；会话能力仍如实报
   `CAPABILITY_UNAVAILABLE` → `strict_no_aec`。开关与降级口径与 R4-A 收口时完全一致。
2. **未做真机验证**：本机 AEC3 的实际 ERLE 未知；内置麦克风阵列 DSP 的非线性路径可能让 ERLE 有限。
3. **近端语音被抑制的风险**：§5.2 第 2 行说明"参考有能量即抑制"，若真机双讲时 AEC3 把用户语音
   压向地板，会伤害 ASR 与打断判别——**必须真机测**（R4-B-2 的第一项）。
4. **延迟预算冲突未解**：`AecPolicy.max_echo_path_ms=120` 与 waveOut 200 ms 缓冲粒度不匹配；
   AEC3 自带延迟估计，但本批未改该策略。
5. 未评估 `pyaec`/speexdsp 路线的实际抑回效果（仅确认可安装、许可与 API）。
6. 会话级"实测 ERLE 达标才允许声称已消除回声"的门**尚未实现**（属 R4-B-2）。

---

## 7. 回滚

- 代码：删除 `app/audio/aec/webrtc_backend.py`、`tests/duplex/test_aec_webrtc_backend.py`、
  `tools/p10_r4b_aec_bench.py`，`git checkout -- app/audio/aec/reference_source.py`；
  或整体 `git reset --hard <本批前提交>`。
- 依赖：`.\.venv\Scripts\python.exe -m pip uninstall pywebrtc-audio`（`.wheelhouse/` 的 wheel 可留；
  它已在 `.gitignore` 内，不入库）。
- **默认关闭**：不显式传 `backend="webrtc"` 时，代码路径与 R4-A 收口时一致。

---

## 8. 下一步（R4-B-2，需用户授权）

1. **会话接线 + 诚实性门**：`voice_entry.build_same_process_reference()` 支持选后端；
   `AecCapabilityProbe(aec_capability=CAPABILITY_SOFTWARE)` 只在**实测 ERLE 达标**且参考同钟时给出，
   否则如实 `strict_no_aec`；ERLE 口径统一到 `suppression_evidence`。
2. **真机（用户在场）**：先测"双讲时用户语音是否被抑制"→ 再测 ERLE → 再测误停播/漏打断矩阵。
3. 之后才轮到研究文档里的 C（轮次边界冷却）与 D（插话判别器）。
