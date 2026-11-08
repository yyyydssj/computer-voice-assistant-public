# P10 · T1 任务执行：真机结论与播报耗时拆解（2026-10-14）

> 范围：本文件只记录**本轮实测到的事实**与据此做出的决定，不重述历史设计。
> 证据来源（只读）：
> - 运行日志 `%LOCALAPPDATA%\ComputerVoiceAssistant\logs\runtime.jsonl`（`voice_turn` 事件）
> - 任务审计 `%LOCALAPPDATA%\ComputerVoiceAssistant\audit\task-execution.jsonl`
> - 最新一次会话 `instance_id = a7c979dc-6a27-4d8c-926b-1cb411f2527b`（21:31:21–21:34:30，本地时区）
> 运行方式：`cd <PROJECT_ROOT>; .\.venv\Scripts\python.exe -m app.main --voice --auto [--post-speech-mute-ms 800]`
> 基线：全量 **3091 tests OK (skipped=3)**（164.3 s、exit 0）。

## 1. 待确认窗口（20 s）到期作废：真机通过

- `app/main.py` 构造 `PendingConfirmationRouter` **未传** `window_seconds` ⇒ 用默认 `DEFAULT_CONFIRM_WINDOW_SECONDS = 20.0`
  （注意：`app/runtime/task_runtime.py` 的 `CONFIRM_WINDOW_SECONDS = 300` 是 **L2 授权窗口**，与待确认窗口不是一回事）。
- 真机 turn 15：`task_kind=cancelled, task_reason=timeout` ⇒ **窗口到期会作废计划并播「已取消，未执行任何操作。」**
- 反例澄清：用户此前反映"等了一会儿说「确认执行」仍然执行"，按日志算术属于**窗口内**：
  turn 12 计划播报结束（日志 21:33:08）⇒ deadline ≈ 21:33:28；turn 14 的说话时刻上界 ≈ 21:33:24（轮末 21:33:31 − 6.36 s 音频）⇒ 窗口内执行属设计行为。

## 2. #13 播报后输入静默窗：机制生效，但**会吃掉用户首字**，故默认关

带 `--post-speech-mute-ms 800` 的会话（`instance` 前一次，21:31–21:34 之前）：

| 现象 | 数据 |
|---|---|
| 窗口确实生效 | 紧随播报的下一轮开头 `speech_mute_frames = 34~36`（≈0.68–0.72 s，20 ms/帧） |
| **吃过用户语音** | 轮 2/3/4/5 —— 恰好是"播报后紧接着说话"的轮次 —— 每轮开头 34~36 帧被静默，`asr_text_length` 只有 2/5/6/6 |
| 无对应收益 | 本机**未观察到**尾音被当成用户语音起始（`no_speech` 轮没有额外触发） |
| 默认关 = 零变化 | 不带开关的会话 17 轮 `speech_mute_ms` **全为 null**、无 `mute_frames > 0` |

处置（已落地）：默认 `0`（关）；`--help` 与配置 docstring 的"建议 800 ms"**撤销**，改为如实写出上述风险与
"若要试，建议 150~300 ms 并单独验收首字"。

补充口径：窗口以**墙钟**从播报结束起算，本机"上一轮结束→下一轮首帧"只隔 ~140 ms（`turn_gap_ms≈78 ms`、
`first_frame_ms≈62 ms`），因此窗口总会作用在"紧接着的那一轮"；若该轮没人说话，它就毫无作用。

## 3. 播报耗时拆解：瓶颈是 TTS 语速（≈3.1–3.6 字/秒），不是 ASR

新增阶段事实后（`listen_ms` / `reply_ms` / `task_ms`）的实测（最新会话）：

| 轮 | `listen_ms` | `reply_ms` | `task_ms` | `speech_drain_seconds` | ASR ≈ reply−task−drain |
|---|---|---|---|---|---|
| 1（计划 22 字） | 3344 | 6375 | 0 | **6.091** | ~284 ms |
| 2（结果 9 字） | 4141 | 3203 | 94 | **2.801** | ~308 ms |
| 11（计划 22 字） | 1953 | 6469 | 0 | **6.276** | ~193 ms |
| 12（重问 15 字） | 2672 | 5641 | 0 | **5.404** | ~237 ms |
| 15（取消 12 字） | 9344 | 4062 | 0 | **3.542** | ~520 ms |
| 3/5/6/7/9/13/16（无播报） | — | 125–422 | 0 | 0 | 125–422 ms |

结论：

- **ASR 解码 125–520 ms（中位 ~250 ms）**：不是瓶颈。
- **任务装配 `task_ms` 0–94 ms**：不是瓶颈。
- **TTS 首字延迟 ≈ `reply_ms − drain − ASR − task` ≈ 0.1–0.3 s**：不是瓶颈。
- **播放时长是瓶颈**：计划句 22 字要 **6.09–6.28 s**、重问 15 字 5.40 s、结果 9 字 2.80 s、取消 12 字 3.54 s
  ⇒ 稳定在 **≈3.1–3.6 字/秒**（正常中文 TTS 约 4–5 字/秒）。
- 反过来解释了更早的观感："说完话后 3.6–10.2 s 没有音频"**不是卡顿**——那是**播报本身在放**
  （播放期不采集音频，所以帧数少）；"已取消 + 新计划"连播 ≈ 3.5 + 6.1 = **9.6 s**，最像"没反应"。

### 据此落地的 D 批次（A+B，**真机已验证**）

- **A**：`DROPPED_REPLACED` 且本轮交接**自己会播报**（新任务计划 / 本批不支持说明）时，取消结果的
  `spoken_text = None`（`display_text` 仍保留"已取消，未执行任何操作。"）⇒ 控制台/审计不变，语音只念新内容，
  省 ≈3.5 s。超时、按键取消等不自己播报的路径**仍照旧播报**"已取消"。
- **B**：`DEFAULT_PLAN_TEMPLATE` 22 字 → **11 字**（`要打开{target}，说确认。`），省 ≈3 s。
- **附带诚实性修正**：旧句承诺"其它回答取消"与实现不符（含糊回答只重问一次、**不作废**计划；
  真机 turn 12 `task_reason=confirm_reprompt` 复现）⇒ 删除该承诺；`CONFIRM_HINT` 改为
  「请说「确认」执行；按 Esc 取消。」并把它放进**控制台文本**（语音只念短句，提示不再丢失）。
- **真机复听结果（8 轮）**：计划句 `speech_text_length=11`、播放 **3.669 / 3.747 / 3.605 s**（旧 22 字 6.09–6.28 s）；
  结果句 9 字 2.767 s、重问 15 字 5.170 s、取消 12 字 3.590 s；速率 2.90–3.34 字/秒。
  **没有任何合并长播报**（若 A 未生效，改口那轮应为 ≈3.59+3.6≈7.2 s）⇒ A 生效。
  同轮 `speech_mute_ms` 全空（#13 默认关零变化）、`kbd_codes=turn:0x1b:cancel` + `end_reason=kbd_cancel`
  ⇒ 键盘诊断可准确归因 Esc。

## 4. 「打开记事本」报"结果不确定"：Store 版重定向导致，口径 A（只改文案）

- 审计：`operation_result.error_code=application_identity_mismatch`、`verification_status=state_uncertain`
  （fail-closed，未自动重试、未回滚）；**用户确认记事本窗口真的弹出来了**（动作成功）。
- 判据在 `app/verification/result_verifier.py`：按 PID 解析进程可执行文件路径与哈希，必须等于白名单
  `C:\Windows\System32\notepad.exe`（实测 SHA-256 `CE5896BB…8CB9`；`calc.exe` `81BD4898…6B2A`）。
  Win11 的 `System32\notepad.exe` 只是 **应用商店版 Notepad 的启动器** ⇒ 观察到的进程路径必然不同。
- 处置（口径 A，零安全口径变更）：**仅当结构化结果里确有 `application_identity_mismatch`** 时，语音念
  「已启动{target}，但身份验证没通过（可能是应用商店版本），没有自动重试。」；`text`（控制台/审计）
  逐字不变。其它 `state_uncertain` 维持原口径。
- 未采纳（需明确批准才能做的安全口径变更）：白名单声明 Store 重定向前缀 `redirect_prefixes`；或把 notepad 移出白名单。

## 5. 键盘通道：本会话零按键事件；"没按 Esc 却被取消"未复现

- 最新会话 `kbd_codes` **全空**（一次按键事件都没有），也没有 `key_cancel`。用户报告的"不按键时没有出现「已取消」"与日志一致。
- 历史 `key_cancel`（20:23:52 / 20:32:04）来自更早会话，**尚未复现其来源**。
- 本批已把诊断做上：`_KeyEventLog` 记录**原始键码 + 来源（轮次内/轮次间）+ 处置**（值形如
  `between:0x1b:ignored_escape|turn:0x0d:confirm`，刻意不含 `key` 字样以免被结构化日志的密钥正则脱敏），
  并把轮次之间命令读取从 `kbhit()+readline()`（**会被单个非回车键卡住**）改成**非阻塞单键 + 回车出命令**。
- 下次复现时的判据：若 `kbd_codes` 出现 `turn:0x1b:cancel` ⇒ 确有 Esc 事件（查键盘/宏）；
  若没有 cancel 记录却出现取消 ⇒ 不在键盘通道，继续查路由/状态机。

## 6. 其它本轮验证过的事实

- 播报完整性：`speech_text_length = played = accepted = pushed`、无 `stall_timeouts`、无截断。
- 轮次间无空档：`turn_gap_ms` 62–94 ms；首帧延迟 `first_frame_ms` 62–79 ms。
- `--help` 在 GBK 控制台 exit 0（本轮我一度在 help 里写了 `⇒`，属 P10 已知会崩的非 GBK 字符，已改回中文标点并复验）。
- **外网调研受限的如实说明**：本会话 `web_fetch` 对 `raw.githubusercontent.com` 返回"解析到非公网 IP"、无法取源码，
  因此 #12/#13 的"借用公开实现"只**沿用已入档**出处（HA intents/HassIL、Mycroft/OVOS `get_response(num_retries)`、
  AgenticSeek `sources/echo_filter.py`、docker-talkies），**没有**新增未经核对的外部引用。

## 7. 未验证 / 待办

- **D 批次真机复听**：✅ 已完成并验证（见 §3 末段数据）。
- **C 待调研**：VoiceStudio 侧是否存在语速（rate/speed）参数；若能把 2.9–3.3 字/秒提到 4–5 字/秒，所有播报再省约 30%。
- **#13 若要重启**：需真机验收"不吃首字"，建议 150–300 ms，或把起算点改为"下一轮首帧"。
- **#11「关闭应用」口径**未选。
- **#12 句式清单**未在真机逐条验收（代码与单测已覆盖）。
- 提交状态：本轮 6 个提交（`4a5a04b` / `3263717` / `cd4852c` / `de9da17` / `ead0d69` / `b9951e2`）
  **已 push 到 `origin/codex/review-playback-lifecycle`**，远端 HEAD = `b9951e2`。
