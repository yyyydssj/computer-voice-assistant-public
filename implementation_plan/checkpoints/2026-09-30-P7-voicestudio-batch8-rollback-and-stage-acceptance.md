# P7 VoiceStudio Batch 8：收敛、回滚演练与阶段验收（默认切换未执行）

- 日期：2026-09-30
- 项目：`<PROJECT_ROOT>`
- 分支：`codex/review-playback-lifecycle`
- 状态：**Batch 8 可执行部分完成（回滚演练、依赖记录、忽略规则加固、阶段验收）；默认切换与 P7 阶段完成均未执行**

## 1. 本批做了什么、故意没做什么

**做了**：

1. **回滚演练**（计划 Batch 8 第 4 步）：四种 `TTS_VOICESTUDIO_MODE` 取值全部可解析；`disabled` 解析为 `legacy`、`http_wav` 保持自身，两者**无需任何代码改动或注入即可构造 `CompletedReplySpeaker`**；`ws_pcm`/`auto` 必须显式注入 `session_factory`（否则 `ValueError`）。结论：**回滚只需改配置**。
2. **依赖与可复现性记录**：`websockets==17.1`，wheel `websockets-17.1-cp312-cp312-win_amd64.whl`，SHA-256 `617243E19A0992095956F406EE9CD3BC4BA92862D83CB1D83BB59CE574412BEC`，`License-Expression: BSD-3-Clause`，`Requires-Python >=3.11`，无运行时依赖；离线安装命令 `pip install --no-index --find-links .wheelhouse websockets==17.1`。
3. **忽略规则加固**：`.gitignore` 原本已忽略 `/.env`、`/.venv/`、`__pycache__/`、`/models/`、媒体文件；本次补上 `/.python312/`、`/.python312-embed.zip`、`/.wheelhouse/`、`/.get-pip.py` —— 否则一次 `git add .` 会把约 240 MB 的嵌入式运行时与 wheel 带入仓库。`git check-ignore` 已逐项确认。
4. **阶段验收核对**（第 3 节）。

**故意没做**：**默认模式切换**与 **P7 标记完成**。

理由：
- 流式会话**尚未接入默认 runtime**（`app/main.py` 不创建 `StreamingVoiceSession`），因此"把默认从 `http_wav` 改成 `auto`"目前没有可切换的消费者；
- 计划明确要求"**用户审阅验收后，才把默认模式从 `http_wav` 切换为 `auto`**"，且"只有原 P7 全部退出条件通过时才改变 P7 状态"；
- 第 3 节显示 P7 五项退出条件仍有未满足项。

## 2. 回滚演练证据（原文摘录）

```text
--- ignore probes after fix ---
IGNORED  .python312/
IGNORED  .wheelhouse/
IGNORED  .get-pip.py
IGNORED  .python312-embed.zip
IGNORED  .env
--- rollback drill ---
mode=disabled settings_ok resolved=legacy   speaker_built_without_injection=yes
mode=http_wav settings_ok resolved=http_wav speaker_built_without_injection=yes
mode=ws_pcm   settings_ok opt_in_required=yes (ws_pcm/auto requires an explicit session)
mode=auto     settings_ok opt_in_required=yes (ws_pcm/auto requires an explicit session)
rollback_drill: PASS (no code change needed to revert)
```

## 3. P7 阶段退出条件核对（未勾选 = 未证明）

- [ ] **文字回复按句边界原顺序实际播放，有端到端/设备证据**
  部分满足：真实设备证据存在（2026-09-28 完整 WAV 单 buffer 播放；2026-09-30 Batch 5 三句 WS 流式 + 真实 WS→HTTP 降级；Batch 7 句长矩阵/多句/重启恢复/播放中取消）。**但默认 runtime 仍未把流式路径接上**，当前真实链路走的是 `legacy`/`http_wav` 单 buffer 完整 WAV 路径。
- [ ] **播放提交前可切 Edge；播放开始后不换声/混声/重播；旧回复不跨 Provider 续播**
  **未满足**：项目只有 `TTSFallbackRouter` 的**路由契约**，**没有 Edge 播放实现**，因此"切 Edge"只能产出决策、不能真实出声。不换声/不重播的决策与真实取消（取消后无新音频进入设备）已分别验证。
- [ ] **TTS 失败不改变任务/计划/授权/账本/执行/结果事实；不可用时安全回落文字**
  部分满足：离线契约与真实 HTTP 接管已验证；真实"VoiceStudio 双路径均失败→转文字"的端到端未在真实服务上演练。
- [ ] **VoiceProfile 更新仅在许可句子/回复边界生效，用户权限与版本边界经测试**
  部分满足：`VoiceSelection.validate`、`read_selection`、`verify_reference`、`verify_provider` 均被真实执行并通过；"更新边界"本身未在本次流程中触发验证。
- [ ] **阶段级回归、故障注入、环境/版本/预算、隐私/日志/停止条件已记录**
  部分满足：环境/版本/依赖/隐私边界/停止语义均已记录；**故障注入（首音前断线、播放中断线）未执行**，逐句 p95 未采集。

**结论：P7 仍为 Blocked / 未完成。不标记 complete，不进入 P8。**

## 4. 未完成清单（全部如实登记）

| 项 | 状态 | 前置条件 |
|---|---|---|
| 首音前断线、播放中断线故障注入 | 未做 | 需授权人为干扰 socket |
| 真实 HTTP fallback 失败演练 | 未做 | 需构造真实 HTTP 失败（当前只验证了成功接管） |
| Edge 真实播放 | **无实现** | 属新功能，不是验收项 |
| 逐句 `sentence_ready→submit`、`first_PCM→device` p95 | 未采集 | 需先加句级时间线打点 |
| 流式会话接入默认 runtime | 未做 | 需单独授权（`app/main.py` + `CompletedReplySpeaker` 接线） |
| 默认模式切换 `http_wav → auto` | 未做 | 需先接线 + 用户审阅验收 |
| 本轮全部改动提交 | 未做 | 等用户审阅；`.env` 已确认被忽略，无 secret 风险 |

## 5. 验证证据

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
983 tests, OK (2 skipped)

.\.venv\Scripts\python.exe -m compileall -q app tests tools
PASS (exit 0)

git diff --check
PASS (exit 0；仅 CRLF 提示)
```

## 6. 下一入口

1. 用户审阅并决定提交范围（`.env` 已忽略；建议排除运行时目录，现已由 `.gitignore` 保证）；
2. 若要让流式路径真正生效：单独授权**流式会话接入默认 runtime**（opt-in），之后才谈默认切换；
3. 其余未完成项（断线注入、HTTP 失败、p95 打点、Edge 实现）各自单独授权；
4. P8 仍未进入，且必须另行授权。
