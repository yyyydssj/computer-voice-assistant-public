# P0–P6 跨阶段复核（2026-09-30，P7 收尾同时执行）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 目的：在 P7 阶段级验收通过后，回头复核**前七个阶段（P0–P6）**是否仍然成立、有无被 2026-09-30 的 TTS/Edge 改动破坏，并核对各阶段状态与证据。
- 结论：**P0–P6 全部仍然成立**；期间发现并修复 2 个文档/覆盖率问题（见第 3 节），无功能性回归。

## 1. 逐阶段复核

| 阶段 | 路线图状态 | 验收/交接证据 | 对应测试分组 | 结果 |
|---|---|---|---|---|
| P0 实现冻结与环境基线 | complete（2026-09-19） | `checkpoints/P0-handoff.md`、`P0-freeze-baseline.md` | —（基线文档/环境） | ✅ 证据在位 |
| P1 运行时外壳与最小界面 | complete（2026-09-19） | `checkpoints/P1-handoff.md`；`2026-09-21-P1-P4-integration-acceptance.md` | `tests/foundation` **61** | ✅ OK |
| P2 跨模块契约与状态模型 | complete（2026-09-19） | `checkpoints/P2-handoff.md`；P1–P4 集成验收 | `tests/contracts` **44** | ✅ OK |
| P3 文字聊天与 Harness 会话 | complete（2026-09-21，阶段级） | `checkpoints/P3-handoff.md`；P1–P4 集成验收 | `tests/conversation` **19** + `tests/persona` **5** + `tests/agent` **12** | ✅ OK |
| P4 工具/路径/策略/ActionPlan | complete（2026-09-21，阶段级） | `checkpoints/P4-handoff.md`；P1–P4 集成验收 | `tests/tools` **30** + `tests/plans` **23** + `tests/safety` **22** | ✅ OK |
| P5 授权/执行/验证/审计/恢复 | complete（2026-09-21） | `checkpoints/P5-handoff.md`；`2026-09-21-P5-integration-acceptance.md` | `tests/security` **81** + `tests/retry` **9** + `tests/state` **5** + `tests/providers` **38** + `tests/evidence` **39** + `tests/recovery` **3** | ✅ OK（39/3 为本轮补回，见第 3 节） |
| P6 ASR 按键输入 | complete（2026-09-21） | `checkpoints/P6-handoff.md`；`2026-09-21-P6-integration-acceptance.md` | `tests/asr` **38** + `tests/audio` **57** | ✅ OK |
| 跨阶段 | — | `tests/integration`（P1–P4、P5、P6 集成）、`tests/routing` | **12 + 59** | ✅ OK |
| 现行阶段 P7 | complete（2026-09-30，阶段级） | `2026-09-30-P7-stage-acceptance-final.md` | `tests/tts` **550**（3 skipped） | ✅ OK |

- **全量回归（更正后基线）**：`unittest discover -s tests -t . -p "test_*.py" -q` → **1107 tests, OK (skipped=3)**；`compileall -q app tests tools` exit 0；`git diff --check` exit 0。
- **改动范围核查**：2026-09-30 的改动只落在 `app/tts/**`（含新增 `clock.py`/`providers/edge_sapi*`）与 `app/config/settings.py`（仅新增 `TTS_EDGE_VOICE`），**没有触碰任何 P0–P6 模块**（`app/plans|executor|auditing|verification|runtime|recovery|retry|state|tools|audio|asr|agent` 的 `git status` 为空）。

## 2. P5 安全链在 P7 改动后的隔离性

- 结构性审计（28 个 TTS 文件、0 解析失败）：`app/tts/**` 对 `app.plans/executor/auditing/verification/runtime/recovery/retry/state/tools` 的引用数 **0**；唯一外部类型依赖是只读的 `from app.agent.adapter import AgentTurnOutcome`。
- 结论：P7 的播放/Edge/计时改动**在结构上不可能**修改 P5 的授权、账本、执行、验证、审计事实；P5 的 81+39+9+5+38+3 项测试全部通过。

## 3. 本轮发现并修复的两个问题

1. **42 项测试此前未被收集（覆盖率缺口，已修）**：
   - `tests/evidence/`（4 个文件、39 项：`test_ledger` 11、`test_agent_chain` 11、`test_local_ports` 10、`test_live_runner` 7）与 `tests/recovery/test_recovery_scanner.py`（3 项）缺少 `__init__.py`，因此 `unittest discover -s tests -t .` **不会收集**它们（用 `-s tests/evidence` 直接作为起点时会报 "Start directory is not importable"）。
   - 实测：这 5 个模块单独按模块路径运行 **42/42 全 OK**；但 2026-09-30 之前所有"全量"数字（719/773/983/1019/1065…）都**不含**这 42 项。
   - 处置：补齐 `tests/evidence/__init__.py`、`tests/recovery/__init__.py`（空文件，与其它测试目录一致）→ 全量变为 **1107 tests OK (3 skipped)**，新纳入的 42 项全部通过。
   - 影响：今天各 checkpoint 里"全量 1019/1065"的表述是"当时被收集到的数量"，**不是**可执行测试集的全部；历史记录不追改，本文件与 tracker 登记更正。
2. **路线图 P3/P4/P5 卡片缺状态行（已补）**：P0/P1/P2/P6/P7 有 `> 阶段状态：…`，而 P3/P4/P5 没有（其完成状态只存在于 P1–P4/P5 集成验收文档）。已在三张卡片补上状态行并指向对应验收文档与 handoff。

## 4. 未做与边界

- 本次复核只做**只读核对 + 补齐缺失的 `__init__.py` 与路线图状态行**；未改动任何 P0–P6 的实现、测试内容或既有验收文档的结论。
- 未重新执行 P0–P6 的真实设备/网络场景（这些阶段本身不含真实 TTS/设备链路）；P6 的 ASR 仍为 fake 设备契约，真实麦克风未验证，与 2026-09-21 的结论一致。
- P7 的其余边界（Edge 为本机 SAPI、听感为一次性人为确认、R 支线 NOT READY、P8 未进入）见 `2026-09-30-P7-stage-acceptance-final.md`。

## 5. 复核命令

```powershell
cd <PROJECT_ROOT>
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q   # 1107 tests, OK (skipped=3)
.\.venv\Scripts\python.exe -m unittest tests.evidence.test_ledger tests.evidence.test_agent_chain tests.evidence.test_local_ports tests.evidence.test_live_runner tests.recovery.test_recovery_scanner -q
.\.venv\Scripts\python.exe -m compileall -q app tests tools
git diff --check
```
