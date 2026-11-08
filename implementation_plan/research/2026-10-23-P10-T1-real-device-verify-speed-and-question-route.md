# P10 · T1 真机验证（2026-10-23）：待验证项、播报语速实测、疑问句误拒修复

> 用途：接续 `2026-10-14-P10-T1-task-exec-real-device-and-latency.md`。本文**更正**该档的一处结论
> （"播报慢的瓶颈是播放 ≈3 字/秒"），并记录本轮新增的真机缺陷与修复。
>
> **证据来源（全部可复现）**
> - 真机运行日志：`%LOCALAPPDATA%\ComputerVoiceAssistant\logs\runtime.jsonl`
> - 审计：`%LOCALAPPDATA%\ComputerVoiceAssistant\audit\task-execution.jsonl`
> - 真机命令：`.\.venv\Scripts\python.exe -m app.main --voice --auto --tts-mode ws_pcm --barge-in-mode off`
>   （`ASSISTANT_PROVIDER=deepseek`、`ASSISTANT_DEEPSEEK_OPT_IN=true`）
> - 离线测量：本机 VoiceStudio `POST http://127.0.0.1:3900/generate`（multipart：text/profile_id
>   `e8572e8e`/language `Chinese`/speed/stream=false），同一句 11 字
>   `要打开记事本，说确认。`；F0 用 40 ms 窗自相关估计
> - **运行方式教训**：真机进程必须用**分离进程**（`Start-Process -RedirectStandardOutput`）启动。
>   本轮第一次用受管后台任务启动，被会话打断一起 `TerminateProcess`（`exit 1`、无
>   `runtime_closing`、TTS 子进程未清理）——日志会缺收尾事件，容易误判成崩溃。

## 1. 待验证项真机结论（10 项）

| 项 | 结论 | 证据（轮次/字段） |
|---|---|---|
| #12 不支持句式 | ✅ **3/3 通过** | run1 turn8/9/10：`关闭记事本`→`close_application`、`关闭这个窗口`→`close_application`、`删除桌面文件`→`delete_files`；各播 16 字拒绝语、`task_kind=unsupported`、审计零执行 |
| #12 反例（疑问句） | ❌ **真机缺陷**（已修，见 §3） | 「你能关闭应用吗？」→ `task_state=unsupported`、播 16 字拒绝语；而离线 `match_unsupported_command` 对同句返回 `None` ⇒ 不是本地句式，是 **Provider 路由**判的 |
| D 计划句 | ✅ 11 字 | `speech_text_length=11`（改造前 22 字） |
| 换目标不连播 | ✅ 通过 | 计划「记事本」→ 改口「计算器」：控制台仍打印「已取消，未执行任何操作。」，但语音字符数 **11**（只播新计划句），未连播 12 字取消句 |
| 20 s 待确认作废 | ✅ 通过 | 12:36:46 计划 → 12:37:23 才说「确认」（37 s，超窗）⇒ `task_kind=cancelled / status=denied / reason=timeout`，记事本**未执行** |
| 超时轮播报事实 | ⚠️ 口径混淆 | 该轮 `task_spoken=true`、`speech_played_chars=12`、`speech_drain_seconds=3.748`，但**轮级** `spoken=false`（代码：`spoken=report.spoken`，任务播报记 `task_spoken`）⇒ 日志易被读成"没播" |
| 超时后说确认 | ✅ 已修（见 §4.1） | 原来是"作废只在下一轮播、之后说「确认」没反馈"；现在：到期文案改成"等太久了…"，且无待确认计划时的「确认」固定回 `NO_PENDING_TASK_TEXT`（不发 Provider） |
| Esc/取消归因 | ✅ 未复现异常 | 全程 `kbd_codes` 空、零 `kbd_cancel`，全部 `silence_timeout`；未再出现"没按 Esc 却被取消" |
| #13 静默窗 | ✅ 默认关 | 全程 `speech_mute_ms`/`speech_mute_frames` 为空 |
| 打开应用链路 | ✅ 符合已知口径 | 记事本 → `state_uncertain/application_identity_mismatch`（Win11 商店版启动器，已知）；计算器 → `executed/completed`（审计 `plan-6719c247d2ef`） |
| 闲聊轮播报 | ⚠️ 待定性 | 12:34:30–12:35:19 连续 7 轮 `text_only`（有识别文本、有回复、`spoken=false`、`speech_error=null`）；用户确认那几轮是闲聊/环境声，同会话后段又恢复正常播报 |

## 2. 播报语速：**修正 2026-10-14 的结论**

### 2.1 离线实测（同一句 11 字，本机 `/generate`）

| speed | 合成墙钟 | 音频时长 | RTF | 端到端字/秒 |
|---|---|---|---|---|
| 1.0 | 1.36 s | 1.76 s | 0.77 | **3.53** |
| 1.4 | 1.05 s | 1.48 s | 0.71 | **4.35** |
| 1.6 | 1.05 s | 1.29 s | 0.81 | **4.71** |
| 2.0 | 1.03 s | 1.08 s | 0.95 | **5.21** |

- 真机 speed=1.0 的 12 字 drain 3.748 s = 3.20 字/秒，与离线 3.53 同量级 ⇒ 数据自洽。
- **结论更正**：真机 ≈3.2 字/秒里 **44% 是合成耗时**（RTF 0.77），"瓶颈就是播放 ≈3 字/秒"不成立；
  提高语速会**同时**压缩合成与音频时长（1.0→1.6：1.36+1.76 → 1.05+1.29）。
- speed=2.0 时 RTF 反升到 0.95 ⇒ **最佳区间 1.4–1.6**。

### 2.2 真机实测（`--tts-speed 1.4`，新参数）

| 轮次 | 文本 | 字数 | drain | 字/秒 |
|---|---|---|---|---|
| plan | 要打开记事本，说确认。 | 11 | **2.581 s** | **4.26**（speed 1.0 时 3.669/3.747/3.605 s ≈3.0） |
| result | 任务结果不确定… | 34 | 5.037 s | 6.75 |

计划句 4.26 与离线预测 4.35 几乎一致 ⇒ **speed 确实传到 VoiceStudio 并生效**。

### 2.3 听感问题（未定性，默认仍 1.0）

- 用户反馈：1.4 档**尾部吞字 + 轻微发尖**（"通常在播报最后几个字"）。
- 客观：F0 中位 240 Hz(1.0) → 259 Hz(1.4) → 277 Hz(1.6)，**+8%/+15%，远小于 1.4 倍**
  ⇒ 不是简单变调重采样（否则会是 336 Hz），是时间伸缩类实现，但仍带轻微升调。
- 未定性：尾部吞字是**引擎侧**（快档截尾）还是**播放侧**（waveOut 200 ms 缓冲在 finish 后立即 close）。
  下一步探针：同一句用 `--tts-mode http_wav`（整段）与 `ws_pcm` 对比；再用本地 SenseVoice 回读合成 WAV
  的尾字（不需人耳）判断是音频本身缺内容还是播放丢失。
- **处置**：`--tts-speed` 默认 **1.0（行为不变）**，1.4 作为可选实验参数，不进默认配置。

## 3. 真机缺陷：疑问句被判"不支持"（已修）

### 3.1 根因

- 本地句式层有 `UNSUPPORTED_BLOCKERS`（`吗/？/怎么/为什么/能不能/不要/如果/假设…`）白名单，
  命中即放行 → 「你能关闭应用吗？」本地返回 `None`（离线可复现）。
- **Provider 路由不受这个白名单约束**：DeepSeek 按 `deepseek-route/v1` 自行判成
  `unsupported/unimplemented_capability` ⇒ 上层如实播报"本批暂不支持"。
- 旁证：`--help` 与全量测试都覆盖不到这条链路（属于"模型判定"而非句式匹配）。

### 3.2 修复（提交 `4f91679`）

- `local_task_router.downgrade_question_unsupported`：`unsupported` 交接 + 原文含疑问/否定/假设标记
  ⇒ **确定性回落 `chat`**（`router.provider=question_fallback`，复制模型 id/version、confidence≥0.80、
  `tool_scope=()`）——与模型无关，"宁可多聊一句，也不误拒"。
- `TaskFirstRouter` 与 `PendingConfirmationRouter` 的两条 **fallback 出口**都过这道兜底；
  本地句式命中的 `unsupported` 不受影响（口径不变）。
- DeepSeek 路由提示词补口径：疑问/否定/假设**不是**不支持，只有明确命令句才算
  （借 `xinnan-tech/xiaozhi-esp32-server` 意图提示词的 `continue_chat` 弃权默认；
  见 `core/providers/intent/intent_llm/intent_llm.py`）。

### 3.3 真机复验（新代码，默认语速）

| 说 | 轮次事实 | 结论 |
|---|---|---|
| 「你能关闭应用吗？」 | turn6：`reason=speech_finished`、`task=null`、播报 64 字 | ✅ 走普通聊天，不再误拒 |
| 「关闭应用」 | turn7：`task_unsupported`/`unimplemented_capability`、16 字 | ✅ 明确命令仍如实拒绝 |

### 3.4 已知局限（本次**未改**，需单独定口径）

- `normalize_command` 会把「能不能/可以」当**礼貌前缀**剥掉，于是「能不能退出程序」在**本地句式层**
  就被当成命令（`match_unsupported_command` 返回 `close_application`）。
- 这与 #12 既有口径「你能不能帮我关闭计算器 **必须命中**」直接冲突：
  中文里"能不能+帮我"是常见礼貌命令，一刀切会让真实命令漏判。
- 已写成回归用例 `test_known_limitation_honorific_question_prefix_is_matched_locally` 固化现状；
  改动需要先决定"能不能 X"算请求还是询问。
- **2026-11-08 已定案（口径 A）**：句首礼貌前缀不改变句子类型——**有祈使标记**（帮我/帮忙/给我/
  替我/请你/麻烦你）= 请求 ⇒ 仍如实拒绝（#12 不变）；**只有疑问标记** = 询问 ⇒ 交聊天。
  原用例已替换为 `test_bare_honorific_question_is_a_question_not_a_command` /
  `test_imperative_honorific_still_matches`；详见 `progress.md` 2026-11-08（追加 4）节。

## 4. 超时交互（口径已定，部分落地）

用户口径（2026-10-23）：「到期即时播 + 确认有反馈」。

### 4.1 已落地

1. **超时专用文案**：`task_dispatch.TIMEOUT_TEXT = "等太久了，已经取消，没有执行。"`。
   `voice_task_turn.expire()` 不再复用 `DENIED_TEXT`（"已取消，未执行任何操作。"）——
   真机用户无法区分"是我取消的"还是"窗口过期了"；审计仍用 `DROPPED_TIMEOUT` 区分，不靠文案。
2. **裸「确认」有反馈**：没有待确认计划时听到「确认」⇒ 固定回一句
   `NO_PENDING_TASK_TEXT = "现在没有等你确认的任务。"`（`auto_voice._is_stray_confirmation`）。
   此前这种句子会被当成闲聊发给 Provider（换来一个与确认无关的回答），超时作废后更是"没反应"。
   该路径不发 Provider 请求（省 2 次计费），也不假装执行。

### 4.2 **未落地：到期那一刻主动播**（需要结构性改动）

- 现状：到期检测发生在**下一轮开头**（`AutoVoiceSession._expire_task_turn`，再经
  `_speak_task_notice` → `speak_notice` 播报）。
- 为什么不能"到期即在空闲期播"：项目有硬口径——**任务播报只发生在轮次 REPLYING 状态内**
  （`_task_reply_provider` 提供文本）。历史真机教训：轮次外直接 `_speak` 会撞非法状态迁移，
  表现就是"只打印、一个字都不念"。
- 可行方向（任选其一，需单开批次 + 真机验收）：
  a. 给自动收音驱动的**等待期**（LISTENING 内）加一个 tick 回调，过期时在状态机许可下播一次；
  b. 新增**独立通知播报通道**（与轮次内 speaker 互斥、可取消），`_speak_task_notice` 改走它。
- 风险：都会碰状态机/打断/AEC 参考；必须配真机验收（"计划→等 20 s→主动播"与"播报中不抢话"）。

## 5. 下一步（按优先级，均未开工）

1. **1.4 尾部吞字根因**：`http_wav` vs `ws_pcm` 对比 + SenseVoice 回读合成 WAV 尾字。
2. **超时即时播报**：窗口到期在**到期时刻**播（现在要等下一轮），并明确"超时后说确认"该回什么；
   参考 OVOS `converse`/`response_mode`（保留意图 + 跟随等待窗口）口径。
3. **`spoken` 与 `task_spoken` 口径**：轮级 `spoken` 只表示"闲聊回复是否播报"，任务播报在
   `task_spoken`；建议日志说明或字段改名，避免误读。
4. **`text_only` 7 连**：下一次复现时读 `speech_route`/`speech_error` 定性。
5. **"能不能退出程序"**：口径决定后统一本地句式与 Provider 兜底。

## 6. 本轮参考（GitHub，已核实部分）

| 项目 | 可借鉴点 | 核实方式 |
|---|---|---|
| [`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server) | 意图提示词的 `continue_chat` 弃权默认 + "疑问词问退出≠退出" | 本地 clone 读源码：`core/providers/intent/intent_llm/intent_llm.py`、`core/handle/intentHandler.py`、`core/handle/abortHandle.py` |
| [`OHF-Voice/hassil`](https://github.com/OHF-Voice/hassil) | 模板句式 + `{list}` 槽 + `skip_words` + `requires/excludes_context`（句式工程化） | `ghfast.top` 代理读 README |
| [`MycroftAI/padatious`](https://github.com/MycroftAI/padatious) | 神经网络意图解析（Apache-2.0），只适合做**候选生成** | 同上 |
| [`OpenVoiceOS/architecture`](https://github.com/OpenVoiceOS/architecture) | `pipeline-1.md`：多级 pipeline、首个命中即止、未命中 `ovos.intent.unmatched`；`response_mode` 跟随窗口 | `ghfast.top` 代理读 `pipeline-1.md`（commit `5f9fbea`） |
| [`rany2/edge-tts`](https://github.com/rany2/edge-tts) | `--rate` 语速参数口径（行业一致：TTS 引擎侧调速） | 代理读 README |

**核实边界**：github.com 直连在本机被拦（解析到非公网 IP），以上 README/源码均经 `ghfast.top` 代理
取回；**star/fork/活跃度未取到，不估**。OVOS-CONVERSE-1 全文未取，`response_mode` 只在
`pipeline-1.md` 里被引用。

## 7. 本轮产物

| 提交 | 内容 |
|---|---|
| `21f668b` | 播报语速 `--tts-speed`（默认 1.0）+ 语速实测与瓶颈更正（8 文件，全量 3100 OK） |
| `4f91679` | 疑问句误拒修复（确定性兜底 + 提示词 + 9 项测试）（7 文件，全量 3109 OK） |
| 本轮第 3 步（未提交时先记） | 超时专用文案 `TIMEOUT_TEXT` + 裸「确认」固定回应 `NO_PENDING_TASK_TEXT`（含测试），见 §4.1 |

三处改动前两批**已 push**（远端 HEAD = `d9a7f4c`，即含语速与疑问句修复 + 文档）；第 3 步改动待提交。
