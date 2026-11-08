# 新对话续接（电脑语音助手）

> 生成时间：2026-09-30（本地；**2026-10-13 最近更新**）
> **新对话第一件事：读本文件 + `task_plan.md` 顶部「当前阶段」**；只按第 6 节的顺序读文档，不要通读全部历史。
> **2026-11-08 本轮（最新，先读这条）**：收口 10/24 那批未提交改动（13 改 3 新），分支
> `codex/review-playback-lifecycle`，**已提交并 push**。① **人设文件驱动**（换人设只改文件）：
> `--persona` / `ASSISTANT_PERSONA_PATH` > 仓库默认 `implementation_plan/persona.md` > 内置兜底；
> **显式指定的模板不可用 ⇒ 启动失败（退出码 2），不静默换人设**；启动打印来源，`config_checked`
> 带 `persona_source`/`persona_name`/`persona_version`；新增 `run-voice.ps1`（`-TaskExec`/`-Persona`）。
> ② **`text_only` 定性**：`voice_turn` 事实补 `reply_chars` / `speech_skip_reason` / `route`
> （只含计数与固定短码，不含正文）。③ 全量 **3152 tests OK**（10/23 基线 3112，+40）。
> ④ **真机 5 轮通过**：人设生效（`chat`/`reply_chars=25`，叫「学长」）、任务确认后执行成功
> （`task_ms=109`）、`unsupported` 如实拒绝；**`text_only` 实例＝ASR 出 1 字空句 + 路由 `ambiguous`，
> 不是 Agent 失败**。⑤ `spoken`/`task_spoken` 口径、到期即时播的结构改动仍待办。
> **⑥ 1.4 语速吞尾已结案（同日追加）**：不是丢音频也不是播放丢尾（`ws_pcm` 帧完整、项目播放栈复播
> 墙钟≈音频时长、ASR 能读到句尾）；是**引擎 `speed>1` 把末字释放压短 + 单次合成采样差异**
> （用户耳测 1.0/1.3 正常、1.1/1.2/1.4 突兀；`seed=7` 能让 1.4 变自然），补尾静音无效。
> **处置：默认语速保持 1.0，1.4 不采用、不进默认配置**；若要提速按同类项目路线走「引擎 1.0 + 句级保音高
> 时间伸缩」（未实施）。研究文档 `implementation_plan/research/2026-11-08-P10-T1-tts-speed-tail-root-cause.md`；
> 真机 1.0 复核：第 3/4 轮 `chat` 播报段 2/2、`finish_timed_out=false`、`pending_after_finish=0`。
> **⑦「能不能退出程序」口径已定案（同日追加，口径 A）**：句首礼貌前缀不改变句子类型——
> **有祈使标记**（帮我/给我/请你…）= 请求 ⇒ 仍如实拒绝（#12 不变）；**只有疑问标记** = 询问 ⇒ 交聊天。
> 实现含：原文判定纯询问、祈使时纯疑问阻断词失效、句首填充词（那/嗯/就是…）在本检测内剥离、
> `downgrade_question_unsupported` 同步、真机补单字「关」（「关计算器」）；真机 19:54 复验：
> 「能不能退出程序？」→ `chat`、「帮我关闭计算器」→ `unsupported`、任务链路正常；全量 **3155 tests OK**。
> 详见 `progress.md` 2026-11-08（追加 4）节。
> **发布流程（2026-11-08 起，用户定）**：私有主库 = `origin`（https://github.com/yyyydssj/computer-voice-assistant，日常提交/推送都在这）；公开镜像 = https://github.com/yyyydssj/computer-voice-assistant-public（**单提交脱敏快照，不携带私有历史**）。发布前先确认私有库已提交并推送，然后在本机执行 `python tools/publish_public.py`：脚本会 `git archive HEAD` 导快照，自检「无 .env / 文档无 <本机用户名>、wxid、私人邮箱、`<STUDY_ROOT>`」，作者固定 `yyyydssj`，再强推覆盖公开库。只自检不推送用 `--dry-run`。旧快照库 `cva-public-snapshot-old` 已转私有（历史遗留，勿用）。代码里的 `<STUDY_ROOT>` 是工具白名单（`app/safety/path_guard.py`、`app/tools/catalog.py`、`app/tools/manifest.py`），属功能代码、保留不改。
> **2026-10-23 本轮**：分支 `codex/review-playback-lifecycle`，**远端 HEAD = 本地 HEAD =
> `d9a7f4c`**（语速 `21f668b` + 疑问句修复 `4f91679` + 文档 `d9a7f4c` 已 push；之后还有第 3 步改动，见下）。
> ① **真机验证 10 项**：#12 不支持句式 3/3 通过、D 计划句 11 字、换目标不连播、
> 20 s 待确认作废通过、Esc/取消未复现、#13 默认关；② **发现并修掉真机缺陷**：Provider 把「你能关闭应用吗？」
> 判成 `unsupported` ⇒ 新增确定性兜底 `downgrade_question_unsupported`（疑问/否定/假设回落聊天）+ DeepSeek
> 路由提示词口径（`4f91679`）；真机复验：疑问句走聊天、明确命令仍如实拒绝；③ **新增 `--tts-speed`**
> （`21f668b`，默认 1.0 行为不变）：离线实测 1.0 = 合成 1.36 s + 音频 1.76 s（RTF 0.77）⇒ **更正"播报慢=播放慢"
> 的旧结论**；真机 1.4 计划句 3.67→2.58 s（3.0→4.26 字/秒），但**1.4 档尾部吞字/发尖** ⇒ 默认仍 1.0；
> ④ **超时交互（口径已定：到期即时播 + 确认有反馈）**：已落地超时专用文案
> `TIMEOUT_TEXT`（"等太久了，已经取消，没有执行。"）+ 裸「确认」固定回应 `NO_PENDING_TASK_TEXT`
> （"现在没有等你确认的任务。"，不发 Provider）；**"到期那一刻主动播"未落地**——硬口径是"任务播报只在
> 轮次 REPLYING 内"，需给驱动加等待期 tick 或独立通知播报通道，属单独批次 + 真机验收；
> ⑤ **待办**：1.4 吞尾根因（`http_wav` vs `ws_pcm` 对比 + SenseVoice 回读合成 WAV）、
> `spoken`/`task_spoken` 口径、`text_only` 7 连复现、「能不能退出程序」口径决定、到期即时播的结构改动；
> ⑥ 全量 **3112 tests OK (skipped=3)**（其中既有并发用例 `test_close_and_cancel_concurrently…` 偶发，
> 单跑 3/3 通过）；本轮结论见
> `implementation_plan/research/2026-10-23-P10-T1-real-device-verify-speed-and-question-route.md`。
> **2026-10-14 本轮（P10 · T1 收尾，先读这条）**：分支 `codex/review-playback-lifecycle`，**HEAD = `b9951e2`（已 push，远端同为 `b9951e2`）**。
> **本轮 6 个提交已全部推送**：`4a5a04b` #12 不支持句式如实拒绝（`unsupported`）→ `3263717` #13 播报后输入静默窗（默认关）
> → `cd4852c` 诊断批次（非阻塞命令读取 + 按键码流水 + 轮次时间线 + 记本身份验证文案）→ `de9da17` 阶段耗时事实（`listen_ms`/`reply_ms`/`task_ms`）
> → `ead0d69` 播报提速 D（换目标不连播"已取消" + 计划句 22→11 字）→ `b9951e2` 文档同步。
> **改动与真机结论全在 `implementation_plan/research/2026-10-14-P10-T1-task-exec-real-device-and-latency.md`**，要点：
> ① **#13 结论更正**：静默窗机制真生效（每轮开头静默 34~36 帧 ≈0.68 s），但它吃掉的正是"播报后紧接着说话"那一轮的**用户首字**，
>    而本机没观察到它要防的尾音误触发 ⇒ **默认关，已撤销"建议 800 ms"**（若要试建议 150~300 ms 并单独验收首字）。
> ② **播报慢的真因（实测）**：ASR 解码 125–520 ms、任务装配 0–94 ms、TTS 首字 0.1–0.3 s 都不是瓶颈；
>    **播放时长是瓶颈（≈3.1–3.6 字/秒）**：计划句 22 字要 6.1 s、取消句 3.5 s；"已取消 + 新计划"连播 ≈9.6 s 最像"没反应"。
>    ⇒ 已做 D 批次（换目标不再连播"已取消" ≈省 3.5 s + 计划句缩到 11 字 ≈省 3 s），**真机已验证**：计划句播放 **6.09–6.28 s → 3.67/3.75/3.61 s**，改口那轮不再出现"已取消+新计划"的合并长播报（最大播放轮 5.17 s 是 15 字重问句）。
> ③ **20 s 待确认窗口到期作废真机通过**（`task_kind=cancelled / task_reason=timeout`）；此前"窗口过了仍执行"经算术属窗口内，非缺陷。
> ④ 「打开记事本」报"结果不确定"根因＝Win11 `System32\notepad.exe` 是**应用商店版启动器**，进程身份必然与白名单不一致（动作其实成功、窗口确实弹出）；
>    已按口径 A 只改语音文案（控制台/审计逐字不变），未改动安全口径。
> ⑤ 键盘：真机 `kbd_codes` 全空（零按键事件），"没按 Esc 却被取消"未复现；诊断已就位（原始键码 + 来源 + 处置）。
> **下一步**：先真机复听 D 批次；再做 C（VoiceStudio 语速参数只读调研）；#11「关闭应用」口径待选；文档已同步；
> 外网调研受限（`web_fetch` 对 GitHub raw 返回非公网 IP），**未新增未经核对的外部引用**。
>
> **2026-10-13 已收口并 push**：54 项改动（30 改 24 新）按 **9 个提交**落地并已推送到 `origin/codex/review-playback-lifecycle`（HEAD `2ec768a`）；**本文件所在批是第 10 个提交**（UI A/B/C 真机复验归档）。关键提交：`3bac6a4` HUD A/B/C → `3d1f169` 长回复收尾（P1 无进展看门狗 + 2026-10-09 截断结案）+ R4-B5 B1–B3 → `9ea7dd3`/`f62167b` 产物补录 → `25bbf08` R4-B5 B4 真机结论 → `b69f784` 豆包逆向研究 → `320ed41` 文档同步 → `2931321` P1 白名单修复 + 回归 → `2ec768a` P1 ≥800 字验收归档。`artifacts/p10-r4b5/doubletalk.json` 已按结论删除不入库；详见 §5。
> **2026-10-09 结案（长回复播报截断）**：真机确证根因是**空行切出的纯空白碎片**，两条通道各自失败——`pcm_seq` 在构造请求时以 `text_blank` 中止（真机 294 字只播 1/13 段，用户"只听到开头"）；`ws_pcm`（**默认通道**）的空白块被分段器 `EMPTY_TEXT` 拒收且**不消费序号** ⇒ 其后每个 chunk 都被判越序、**静默丢尾**（233 字只播 74 字且 `error` 为空，旧版据此谎报"播完"）。**两处都已修复并真机验收**（`pcm_seq` 9/9；`ws_pcm` 114/114、398/398 全播）。同批修好 **HUD"播报"状态**（自动收音路径此前只推 listening/hearing，从不进 `SPEAKING`）与 **`verify_provider` 兜底错误码**（拆成 `provider_unreachable`/`provider_not_ready`/`provider_profile_not_found`/`provider_reference_audio_changed` 等）。
> 证据与**旧结论更正**：`checkpoints/2026-10-07-voice-playback-truncation-investigation.md`（§6 更正 + §12 `pcm_seq` + §13 `ws_pcm` 并案）、`checkpoints/2026-10-03-P10-R4B-long-reply-provider-contract-handoff.md`（§1/§3 更正 + §12/§13）；脱敏事实 `checkpoints/artifacts/2026-10-09-pcm-seq-text-blank/`、`checkpoints/artifacts/2026-10-09-ws-pcm-sequence-desync/`。
> **最近一次收口（2026-10-02）**：`implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md`
> （**P0–P9 整体测试与验证通过**：独立重跑复核全量 tests OK (skipped=3) + `compileall` exit 0 + 文档一致性 14 条差异 + 交付物完整性清点；**阶段 4「测试与验证」与阶段 5「交付」判定完成**）
> **P10 已开工（2026-10-02）**：R1a 两阶段打断状态机 + R1b 编排语义 + R2a AEC 降级框架 + R2b-1 AEC 参考时间线/NLMS + R2b-2 真机实测（**回环参考在本机不可用**，改方案 A：同进程提交 PCM 参考）+ **R3 设备会话与安全空闲点恢复** + **R4 会话循环接线（独立打断通道 + 安全门 + 设备策略 + R2b 参考 + 可选降音量）** + **R4-A 参考同钟/对齐估计/回声可观测性安全门（真机已跑）** + **R4-B-1 真 AEC3 后端（`webrtc_backend.py`，默认关闭）** + **R4-B-2 阶段 0 双讲核查（条件性 no-go）+ 方案 C 冷却窗口（默认关闭）** + **R4-B-3/4 真机量测** + **R4-B P1/P2 真 AEC3 接线 + 残余回声地板动态门（默认关闭）** 已交付，全量基线 **2580 tests OK (skipped=3)**；**R4-B 前置（真 AEC 前端只读可行性核查，2026-10-03）已完成**（`implementation_plan/research/2026-10-03-P10-R4B-aec-frontend-feasibility.md`：首选 `pywebrtc-audio` 0.2.0 / 兜底 `pyaec`）。交接见 §9。**本机打断口径仍为：安全门 fail-closed（不会误停播、也暂时不能真打断）**；**AEC3 已接线到会话（默认 nlms 不变）+ 残余地板动态门；三件套真机试听待授权**。
> 免按键接主程序交接：`implementation_plan/checkpoints/2026-10-01-P9-auto-capture-main-integration-handoff.md`（含 KWS/声纹 CLI 与 TTS 按需自动拉起）
> UI 改版交接：`implementation_plan/checkpoints/2026-10-01-P9-hud-visual-refresh-handoff.md`（浅色卡片胶囊外观 + 逐像素分层窗口）
> 设计规格：`implementation_plan/2026-10-01-hud-visual-refresh-design.md`
> **P9 B3/B4**：`implementation_plan/checkpoints/2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`（唤醒词门禁 + 声纹过滤，含真机验收与模型来源/哈希）
>
> **2026-10-12 本轮新增（优先读这三条）**
> 1. **豆包逆向 + 长回复收尾 P1 批次**：新建 `implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`
>    （实读 sonner/react-hot-toast/官方 demo/协议整理；结论：豆包超时全是**空闲式**、TTS **一次请求整条流式**、**只有打断才允许丢尾**）。
>    据此把本仓三处"总时长上限"改成**无进展看门狗**（`streaming_session` / `sequential_session` / `voice_entry` + `--speech-stall-timeout`，
>    默认 15 s），并新增 `speech_drain_seconds` / `speech_stall_timeouts` / `speech_last_progress` 三个真机事实。
>    交接 `checkpoints/2026-10-12-P10-P1-drain-stall-handoff.md`。**离线对照**：旧 11/33 字并 `finish_timed_out` → 新 33/33 字无截断。
> 2. **提示窗 UI 批次 A/B/C**：拖动**跟手**（旧实现被同 id `SetTimer` 重置 ⇒ 一卡一跳）+ 松手不再二次跳位 + 贴边吸附；
>    进场淡入+滑入 260 ms、退场 180 ms；**状态切换保持硬切**（真机反馈脉冲式过渡=闪烁，`state_animation_ms` 默认 0 + 200 ms 防闪护栏）；
>    尊重系统"动画效果"（`SPI_GETCLIENTAREAANIMATION=0x1042`）⇒ 关掉时零过渡帧。交接 `checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`。
> 3. **当时基线**：全量 **2747 tests OK (skipped=3)**（2696 → 2747，+51）；`compileall` exit 0。
>    **⚠️ 仍未做**：① P1 **≥800 字真机长回复验收**（用户待跑，看 `speech_drain_seconds`/`speech_stall_timeouts`）；
>    ② UI A/B/C **真机手感/观感复验**；③ 所有本轮改动**未提交、未 push**。
>
> **2026-10-13 本轮新增（优先读这三条）**
> 1. **P10 R4-B5 双讲检测 B1–B4 真机结论＝条件性 no-go**：本机（内置麦阵 + 内置扬声器）单讲/双讲在**四类可观察量上不可区分**（对齐相关 0.022/0.005、参考 RMS 2121/2121、采集 RMS 895/3526、`erle_sufficient` 帧 0/329），生产检测器在真值单讲段判双讲 **268/268＝100% 误报**，APM `speech_probability` 中位 0.792/0.822 也分不开 ⇒ **不是阈值问题，是判据不携带信息**。处置：`--double-talk-detect` **保持默认 off**；B1–B3 代码保留（47+21+18+4 项单测，作为"有线性回声路径时"的基础设施）；复测前置＝**外接麦克风/耳机**。结论文档 `implementation_plan/research/2026-10-13-P10-R4B5-doubletalk-real-device-conclusion.md`；脱敏证据 `checkpoints/artifacts/2026-10-13-r4b5-doubletalk/`（README + 数值转录件，**无音频**）。后续与火山对齐的下一步是**服务端/云端判据**，不在端上继续调参。
> 2. **回归抓出并修掉 `--help` 在 GBK 控制台崩溃**：`--double-talk-detect` 的 help 文本含 U+26A0、探针 help 含 U+21D2，argparse 打印时抛 `UnicodeEncodeError`（`python -m app.main --help` 曾 exit 1，`tests.duplex.test_double_talk_wiring` 的 CLI 用例随之失败）；已分别改为「注意：」与 `→`，两条 `--help` 实测 exit 0。
> 3. **当前基线**：全量 **2844 tests OK (skipped=3)**（= 2842 + 白名单修复 2 项；本轮实测 **209.7 s、exit 0**）；`compileall -q app tests tools` exit 0、`git diff --check` exit 0。
> 4. **P1 `speech_drain_seconds=null` 只读诊断已完成、缺陷已修**：根因是 `AutoVoiceSession._take_speech_facts()` 的**白名单复制漏了** `drain_seconds`/`stall_timeouts`/`last_progress` 三键（真机 553 条 `voice_turn` 里带键的最后 10 条 **10/10 全 None**；进程时间线排除陈旧进程）。已补三键 + 2 项回归（走真实映射路径）。详见 `progress.md` 的 2026-10-13（追加）一节。
> 5. **✅ P1 ≥800 字真机长回复验收通过（2026-10-13 22:05，用户本人执行）**：实例 `e4694e25`，`--voice --auto --tts-mode ws_pcm --barge-in-mode off`（provider=deepseek，2 轮共 4 次真实请求）；**805 字**回复 `played==accepted==pushed=795`、段 **28/28/28**、`drain_seconds=30.19`、`stall_timeouts=0`、`last_progress=None`、`finish_timed_out=false`、`truncation_reason=null`、`pending_after_finish=0`、`overloaded=false`、`error=null`、`reason=speech_finished`，无设备回退；**用户本人确认"全听到了"**（`user_heard=true`，未代填）。详见 `progress.md` 2026-10-13（追加 2）。
> 6. **✅ UI A/B/C（提示窗拖动 + 进场/退场）真机复验通过（2026-10-13，零成本路径 6/6）**：`.\.venv\Scripts\python.exe -m app.main --voice --auto --tts-mode none` 逐条——进场淡入+右滑 12 px/260 ms ✅、拖动跟手 ✅、松手不二次跳位 ✅、贴边吸附（≤24 px 松手平滑补边 120 ms）✅、状态切换硬切不闪（`state_animation_ms=0`）✅、退场淡出+右滑 180 ms ✅；**用户本人肉眼确认"全部符合"**（无帧级客观证据）。详见 `progress.md` 2026-10-13（追加 3）。
>    **⚠️ 仍未做**：① 含播报的真实路径里 `SPEAKING→LISTENING` 两方向硬切 + 系统"动画效果"关闭口径（可选）；② `--double-talk-detect` 外接麦克风后复测（本机 DTD 已判条件性 no-go）；③ 后续改动待 push。

## 0. 一句话现状

P0–P8 全部完成并通过阶段级验收；**P9 已于 2026-10-01 完成阶段级集成验收并标记 complete**（验收证据 `implementation_plan/checkpoints/2026-10-01-P9-stage-acceptance.md`）：自动收音与连续对话边界、唤醒词门禁、声纹误触发过滤、UI 提示窗与视觉改版（方案 4 逐像素分层窗口）、免按键自动收音接入主程序（`--voice --auto`）、唤醒词/声纹 CLI、本机 TTS 按需自动拉起——全部实现且逐项真机确认；**P0–P9 整体测试与验证已于 2026-10-02 完成（阶段 4/阶段 5 判定完成）**；**P10 已开工并交付十五批**（R1a/R1b 两阶段打断、R2a AEC 降级框架、R2b-1 参考时间线+NLMS、R2b-2 真机实测+方案 A、R3 设备恢复、R4 会话循环接线、R4-A 参考同钟/对齐/回声安全门、R4-B 前置核查、R4-B-1 真 AEC3 后端、R4-B-2 阶段 0 双讲核查+方案 C 冷却窗口、R4-B-3/4 真机量测+R4-B P1/P2 真 AEC3 接线与残余地板动态门、真机 `text_only` 溯源与播报启动失败可观测性修复），全量基线 **2585 tests OK (skipped=3)**。**P10 剩余**：R5 Provider 冷却、R6 长期运行健康；**R4 残留**：前置音频并入下一轮 ASR（需 audio 层 pre-roll 注入接口）、输出设备重连（需固定输出设备 id）、降音量粒度（后端缓冲 200 ms）；**R2b 真机 ERLE 验证需外接麦克风**。

**P9 关键事实（2026-10-01）**：免按键真机试听通过——自动语音起始 → 真实 SenseVoice 识别 → 同一 `ConversationManager` 提交 → 离线声线播报 `COMPLETED`，**用户本人确认听到播报**（`user_heard=true`）。同批修复三个真机缺陷（丢弃轮次锁死 ASR 通道、自动结束被误当故障丢弃、edge 播报 `PCMPlaybackBackpressure` 完全不出声）；后续又完成 B3/B4、UI 四态与视觉改版、免按键接入主程序、唤醒词/声纹 CLI、TTS 按需自动拉起。**当前缺口**：多轮上下文 / 流式 / 费用限流 / persona、任务链接真实路由、AEC 真机验证（需外接麦克风）；`threshold_mode=auto` 未接 CLI。

**P10 关键事实（2026-10-02，六批已交付）**：
- **R1a/R1b 两阶段打断**：`app/duplex/barge_in.py`（状态机，56 项）+ `app/duplex/barge_in_orchestration.py`（编排语义，40 项）。疑似插话立刻降音量（≤100 ms、20~30%）**且不建立轮次**；持续 200~300 ms 可信人声才确认，确认后音量归零、**旧回复永不再续提交**、前置缓冲并入同一 `utterance_id`；误触发按 200 ms 帧阶梯平滑回升（`release_ms=400` 滞后保护）；安全确认提示被打断即失效（`PromptPolicy`，插话本身永不授权）。
- **R2a AEC/降噪降级框架**：`app/audio/aec/__init__.py`（34 项）。阶梯 `aec_ns → aec_only → strict_no_aec → half_duplex`、7 个固定原因码；**未真正应用 AEC 时绝不声称已消除回声**；任何级别都保留文字模式、不关闭会话；设备变化必须重校准。
- **R2b-1 参考时间线 + NLMS**：`app/audio/aec/reference.py`、`nlms.py`（39 项）。**逐样本递归 NLMS 可用**（连续流真 FIR 回声：尾部 ERLE 16~48 dB、权重精确命中真实增益 0.300/0.500）；**块式梯度实测不收敛，已否决**；**真实回声延迟必须 ≥1 样本**（零延迟 ERLE≈−1 dB 且发散，已写成文档+测试）。
- **R2b-2 真机实测 + 方案 A**：`app/audio/aec/reference_source.py`、`app/tts/waveout_pcm.py`（只读旁路）、`tools/p10_aec_probe.py` 等（45 项）。结论见 §2 表「P10 真机结论」。
- **R3 设备会话与安全空闲点恢复**：`app/devices/session.py`（34 项）。**恢复只在安全空闲点**（轮次中/服务中/等待确认一律延后）、**不自动切未知设备**、**设备切换下一轮生效**（epoch 推进）；输出优先于输入；`busy`/`permission_denied`/`incompatible` 不自动重试；任何情况保留文字模式、提示不静默不重复。用户决定：输出断开 → **停止当前播报并回退文字**；重连**只在下一个安全空闲点各试一次**。
- **R4 会话循环接线（2026-10-02，默认全关）**：
  - **独立打断通道** `app/duplex/barge_in_channel.py`（50 项）：自有 `AudioBus`/流身份 `p10-barge-in-channel` + 16 kHz/20 ms 采集 + 前置 PCM 环形缓冲；人声概率按可观测事实映射（检测器命中**且** RMS ≥ 能量门 / 纯能量=rms÷阈值，不编造）；参考相关性只在**同钟且窗口 ok** 时给出；装不上/失效/超时/检测器抛错一律**不打断**并记固定原因码。
  - **安全门** `app/duplex/barge_in_safety.py`（17 项）：`handoff`/`stop_and_replay`/`block`；`authorization_possible` **恒 False**；安全确认提示被插话 → 停播 + 提示失效 + 必须完整重播 + **不**建立可用轮次。
  - **接线**（`app/runtime/auto_voice.py`，1084 → 1818 行）：`--barge-in-mode off|duck|confirm`、`--aec-reference-mode off|same_process`、`--device-recovery-mode off|safe_point` 默认全关；包装 `environment.speaker`（`push_text_chunk` 原样透传**不增首字延迟**，播报期独立监视线程，确认时 `stream.cancel()`）；`duck` 模式确认不生效（`confirm_suppressed_in_duck_mode`）；`AutoTurnFacts` 增 `barge_in`/`pre_roll`/`device`；`barge_in_details()`/`take_pending_pre_roll()`/`set_awaiting_confirmation()`（50 项）；另有 `_sticky_window_observe()`（窗口粘滞，与主链路同口径）。
  - **R2b 参考 + 降音量**：`voice_entry` 把 `SameProcessAecReference`（**24 kHz，与 waveout 同钟**）作 `submit_observer`；`waveout_pcm` 增**可选** `gain_provider`（默认 `None` → 字节不变，13 项）。**16 kHz 采集 vs 24 kHz 播放不同钟 → 不给相关性 → 无可用参考 → 第一阶段降音量按设计关闭**（见下条真机缺陷），不声称已消除回声；降音量**粒度＝后端缓冲 200 ms**。
  - **真机三轮暴露并修复 3 个真实缺陷（2026-10-02 晚）**：① **音量反复跳动**——一帧达 0.6 即降音量 × 无法折减回声 × 确认需连续 13 帧 ⇒ 只降不认 → R1a 增 `min_suspect_frames`（会话层 3＝60 ms）、通道增 `duck_requires_reference`、`release_ms` 400→800；② **确认结构性不可达**——`SileroVadDetector` 按 512 样本整窗判定而采集帧 320 样本（真/假交替），通道缺主链路的「窗口粘滞 + 能量门」→ 新增 `_sticky_window_observe()` + 双重条件；③ **假确认**（用户没插话也停播，6/6 回合）——本机扬声器自己的声音越过能量门且拿不到可用参考 ⇒ 无法区分 ⇒ **fail-closed：参考不可用时整个打断不启用**（半双工），启动即打印 `no_usable_echo_reference`。**本机结论：内置麦克风+扬声器不具备安全打断条件**；机制已真机验证，缺的是可信判别依据。回归 +27 项（全量 2476 OK）。
  - **开源调研（2026-10-02，已入档 `implementation_plan/research/2026-10-02-realtime-voice-barge-in-open-source-research.md`）**：豆包＝闭源云服务（火山引擎"端到端实时语音-全双工版本"）；可参考 **Moshi**（全双工，MIT/Apache + CC-BY 权重，但仍靠浏览器 AEC、需 24 GB 显存）、**Unmute**、**Pipecat**（`InterruptionFrame`、只提交已播文本、MinWords/Krisp VIVA）、**LiveKit Agents**（自适应打断模型 + `backchannel_boundary` 冷却 1.0 s）、**TEN Framework**（Apache 2.0，走 Agora RTC 承担 AEC）。
  - **批次 R4-A（2026-10-02，用户授权"方案 A"）**：按调研的第一优先级落地——新增 `app/audio/aec/alignment.py`（对齐 + 相关性估计）、`reference_source.py` 增 `source_sample_rate` **跨块连续重采样**（24 kHz 播放参考 → 16 kHz 采集**同钟**）、通道 `echo_correlation` 改为**对齐后**相关性并新增 `echo_proven`/`echo_delay_ms`、会话新增**回声可观测性安全门**（没证明"看得见自己的回声"只观测不动手，记 `barge_in_unproven_echo`）、**诚实性修正**（会话不跑回声消除 → R2a 如实 `strict_no_aec`/`aec_applied=False`）。顺带修掉两个真实缺陷（`barge_in_config=None` 时把 `min_suspect_frames=3`/`release_ms=800` 覆盖回 1/400；通道 `duck_suppressed` 判定过宽）。+16 项，全量 **2496 OK**。**真机 A4 已跑**：安全门成功拦下假停播，但本机回声样本级不可相关（echo_proven=false，相关性始终 <0.2）⇒ 方案 A 拿不到判别依据；下一步方案 B（真 AEC 前端）。
  - **R4-B 前置：真 AEC 前端只读可行性核查（2026-10-03，未安装依赖、未改代码）**：入档 `implementation_plan/research/2026-10-03-P10-R4B-aec-frontend-feasibility.md`。**首选 `pywebrtc-audio` 0.2.0**（有 `cp312-cp312-win_amd64` wheel、`pip --dry-run` 报 `Would install`、免编译；唯一依赖 `numpy>=1.24` 已满足；Apache-2.0；真 **AEC3** + NS + AGC2 + VAD），**兜底 `pyaec` 1.0.1**（`py3-none-win_amd64`、无依赖、MIT、speexdsp MDF）；**排除** `webrtc-audio-processing`（只有 cp27/cp36 linux_armv7l wheel）、`webrtc-apm`（只有 cp311-win）、`speexdsp` 0.1.1（仅 sdist，需 swig + libspeexdsp-dev）、`speexdsp-ns`（仅 linux 且无 AEC）。**接口事实**（读绑定源码）：near/far 同率同长（→ 24 kHz 参考必须重采样，R4-A 的重采样可直接复用）、内部按 10 ms（160 样本）切帧且非整数倍零填充（本项目 320 样本帧＝2 帧整）、`process` 同步返回、`stream_delay_ms` 可运行时调；上游 16 kHz int16 每 10 ms 帧 60 µs。**集成点**：`SameProcessAecReference` 内 `NlmsEchoCanceller` 位置可换后端；`waveout_pcm` 旁路取的是降音量后 payload（与真实播放一致）；**必须补"实测 ERLE 未达标不许声称已消除回声"的诚实性门**。**未做**：本机 CPU 与真机 ERLE 均未实测；`max_echo_path_ms=120` 与 waveOut 200 ms 缓冲粒度冲突。
  - **R4-B-1：真 AEC（WebRTC APM/AEC3）后端接入（2026-10-03，用户批准含装依赖；默认关闭）**：依赖冻结 `pywebrtc-audio==0.2.0`（wheel 入 `.wheelhouse/`，SHA-256 `0dabbdadd5d7fd7dcb88323266e5e14d46aaa136c58fa0573c4b0323b683c80b` 与 PyPI 官方 digest 一致；Apache-2.0；仅依赖 numpy≥1.24）；新增 `app/audio/aec/webrtc_backend.py`（与 `NlmsEchoCanceller` **同构**、**可选依赖**、缺失/构造失败/调用异常一律如实直通且异常不冒泡、双讲与静音帧仍喂底层）、`reference_source.py` 增 `backend="nlms"|"webrtc"`（**默认 nlms 行为不变**）、`tests/duplex/test_aec_webrtc_backend.py`（29 项）、`tools/p10_r4b_aec_bench.py`。**本机离线实测**：合成回声累计 ERLE 23.3 dB（尾部 20.6 dB）、单帧中位 **0.157 ms ≈127× 实时**、20 ms 与 2×10 ms **逐样本等价**。**证伪一个会误判的假设**（最有价值的诚实性发现）：参考有能量时即使**没有回声路径**，AEC3 也让近端无关噪声掉 11.7 dB（5.4→0.4）⇒ "ERLE 达标"**不能**当"已消除回声"的判据 → 新增 `suppression_evidence` 分级（**不含"已确认回声路径"档**）与 `reference_silent` 直通，并写成测试。全量 **2496 → 2525 OK (skipped=3)**（231.2 s）。交接 `checkpoints/2026-10-03-P10-R4B-1-aec3-backend-handoff.md`。**未做**：未接线会话、未真机（本机 ERLE 未知，**双讲时用户语音是否被抑制待真机测**）。
  - **R4-B-2 阶段 0：AEC3 双讲保持离线 go/no-go（2026-10-03，用户批准）——结论：条件性 no-go**。工具 `tools/p10_r4b2_doubletalk_probe.py`（离线免设备；**本机真实中文语音** `models/kws/test_wavs/*.wav`；合成回声路径 40 ms+增益+反射+低通；`far_only`/`near_only`/`double_talk` 三组对照）。**数据**：回声 ≥ 用户档双讲用户语音增益 **−5.5 ~ −20.5 dB**、残差用户语音 RMS **93–522**（能量门 600，**5/6 低于门**，最差组已贴近噪声底 150）；用户 ≫ 回声（≈8 倍）档 −1.6 ~ −5.9 dB（1499–2468 高于门）；`near_only` 12/12 次 **−0.00 dB**（无参考能量时 AEC3 完全不碰用户语音）；`far_only` ERLE 15.7–37.9 dB；**同场景分段增益波动 −1.8 ~ −37.7 dB（不稳定）**；NLMS 对照保留语音但 ERLE 仅 8.85 dB。⇒ 衰减随**回声/用户语音比**变化，本机（内置阵列麦 + 内置扬声器远场）大概率落在坏档 ⇒ **暂停阶段 1：不把 AEC3 接进打断判定/ASR 路径**，会话维持 fail-closed（`strict_no_aec`、不误停播、暂不能真打断）；AEC3 后端保留在仓库、默认关闭。证据 `implementation_plan/research/2026-10-03-P10-R4B-2-doubletalk-findings.md`。**未做**：真机未测（回声/用户比、双讲衰减、ERLE）；耳机/外接麦场景未测；探针未加单测（开发工具）。
  - **方案 C 已落地（2026-10-03，用户选 C1 口径：只加冷却、不改 fail-closed）**：`barge_in_cooldown` = 播放开始后 `cooldown_ms` 内**收帧、填前置缓冲，但不降音量、不建立疑似/确认**（如实记 `barge_in_cooldown`）；`BargeInConfig.cooldown_ms` 默认 0（行为不变）、`BargeInChannelConfig.cooldown_ms` 用 `dataclasses.replace` **只覆盖冷却时长**（不重建配置，避免覆盖 `min_suspect_frames=3`/`release_ms=800`）、CLI `--barge-in-cooldown-ms`（建议 1000）、启动提示补冷却与 `echo_proven=false` 的本机提示。**语义边界**：不丢音频、只影响 IDLE、0 = 等价改动前。**验证**：新专项 28 项、打断/会话相关 290 项、全量 **2525 → 2553 tests OK (skipped=3)**（220.626 s）。交接 `checkpoints/2026-10-03-P10-R4B-2-planC-cooldown-handoff.md`。**未做**：真机效果、"更严门槛"档与半双工提示 UI、`--aec-engine` 开关（AEC3 仍未接线）。
  - **R4-B-3/4 真机量测（2026-10-03，用户批准；纯测量、未改生产代码）**：新增 `tools/p10_r4b3_echo_ratio_probe.py`（回声/用户语音比）与 `tools/p10_r4b4_realtime_doubletalk_probe.py`（真机 AEC3 双讲 + `--echo-only --delay-sweep`），均需 `--live-opt-in`、**默认不落盘音频**、无计费。**数据**：回声/用户语音比 **−7.1 dB**（回声 p50 846 / 用户 p50 1917）、**ERL 13.7 dB**、回声自身 **+3.0 dB 超门**（R4 三轮自触发根因）；但同日另一次用户 p50 只有 1037（差 **5.3 dB**）⇒ 比值在 **≈0 ~ −7 dB** 浮动，**不可当稳定好档**。**真机 AEC3**：实测延迟 409 ms → **真机 ERLE 5.87 dB**、残差 519、**137/289 帧超门**，双讲后用户语音 1648（**+8.8 dB vs 门**）；**延迟扫描**：392 ms → 10.32 dB、**最佳 350 ms → 15.54 dB、80/289 超门**，0–500 ms 内 **7–15.5 dB**。⇒ **结论修正：AEC3 不是不可用，而是不够用**（真机 vs 合成差 8–15 dB = 内置阵列 DSP 破坏线性路径）；需**三件套**（AEC3 + 冷却窗口 + **残余地板感知动态门槛**（未做））。证据 `implementation_plan/research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`；交接 `checkpoints/2026-10-03-P10-R4B-3-4-real-device-handoff.md`。**真机暴露并修掉**工具缺陷：GBK 控制台打印 `≫` 崩溃（`safe_write()`），该次未写出 JSON artifact（数值取自控制台，命令可复现）。
  - **下一步正题（待确认）**：**三件套**——`--aec-engine webrtc` 接线（默认关 + "实测 ERLE 达标才许声称已消除回声"的诚实性门）+ 已落地的冷却窗口 + **播放期动态门**（`max(speech_threshold, 残余回声地板 × 裕量)`，地板=AEC3 残差滑动分位数）+ 启动 ~1 s **延迟探测**；完成后再真机试听（用户在场）。
  - **R4-B P1/P2 已落地（2026-10-03，用户批准）**：三件套的第 1、3 件完成——`--aec-engine {nlms,webrtc}`（**默认 nlms 行为不变**；webrtc 缺依赖如实退回 nlms）、通道每帧先跑 AEC3 取**残差**算电平/门（检测器结构判定仍看原始帧）、对齐延迟每 0.5 s 喂 `stream_delay_ms`；**残余回声地板感知动态门** = `max(energy_threshold, 地板 × margin)`（margin 默认 3.0、只抬不降；地板=最近 1 s 残差 p20，只吃参考有能量的帧）；能力与事实分离（只有引擎真在跑才报 `CAPABILITY_SOFTWARE`，抑制质量由 `aec_erle_db`/`aec_erle_sufficient` 暴露）；失败路径 `aec_process_failed`/`aec_output_invalid`/`reference_silent` 一律不冒泡。**R4-A fail-closed 口径未改**（`echo_proven=false` ⇒ 本机仍不真停播）。新增 `tests/duplex/test_barge_in_aec_engine.py`（27 项）；全量 **2553 → 2580 tests OK (skipped=3)**（235.444s）。交接 `checkpoints/2026-10-03-P10-R4B-P1P2-aec-engine-residual-gate-handoff.md`。**未做**：三件套真机试听；`max_echo_path_ms=120` 未改（改判据属安全口径变更）；动态门参数未真机调参；NS/AGC 未启用。
  - **下一步（待授权）**：① **三件套真机试听**（用户在场）：`--aec-engine webrtc --aec-reference-mode same_process --barge-in-mode confirm --barge-in-cooldown-ms 1000`；② 若达标再决定是否把"能否真停播"的判据从 `echo_proven` 改为"实测 ERLE + 残余门"（**安全口径变更**）；③ 方案 C 剩余项；④ R5/R6。
  - **真机 `text_only` 溯源（2026-10-03）**：三件套真机试听 8 轮**全部** `spoken=false/text_only` ⇒ 无播报、打断通道没启、AEC3 没跑。自检（新 `tools/p10_r4b_channel_selfcheck.py`，≈6 次真实请求；`--router integration`/`--entry asr`）证明：工作类文本 / 闲聊文本 / **真机 ASR 入口**三种组合下 `speech_eligible` 均 **True**，裸 speaker `start_stream()=ok:SessionStream`（开即取消、未出声），`aec_backend=webrtc/aec_available=True` ⇒ **输入侧与裸 speaker 全部排除**。**根因**：`app/voice_loop/chat_loop.py::_speak()` 把 `start_stream()` 异常**吞成 `text_only`**（与"本轮没通道"同码），真因不可见。**已修**：启动异常 → `speech_start_failed:<ExceptionName>`（只带类型名），`None`/无方法 → 仍 `text_only`，`finish` 失败 → 仍 `speech_failed`；不改播报/打断行为与安全口径。**验证**：相关 134 项 OK；全量 **2580 → 2585 tests OK (skipped=3)**（237.796s）。证据 `research/2026-10-03-P10-R4B-playback-channel-selfcheck.md`；交接 `checkpoints/2026-10-03-P10-R4B-playback-observability-handoff.md`。**未做**：会话里真正的启动异常仍未知（需一次真机运行读新原因码）；真机 8 轮无 AEC/打断数据 ⇒ 三件套真机验证仍待做。
  - **回声证明收紧（2026-10-03，用户反馈"一句话说一半，我没打断就停了"）**：末次真机会话 turn 2/4 **没插话**却 `barge_in_confirmed=true` + `end_reason=barge_in_interrupted`（各只跑 **16 帧≈320 ms**）；`echo_proven=true`、相关 **0.60/0.72**、估计延迟 **746 / −780 ms**（turn 3 为 **1589 ms**），而本机实测参考→采集只有 ≈390–410 ms ⇒ **伪相关骗过了"只看相关性"的旧门槛** ⇒ R4-A 安全门放行 ⇒ 假停播。**修复（只收紧、不放松）**：`BargeInChannelConfig` 增 `echo_proof_delay_min_ms=0`/`echo_proof_delay_max_ms=500`/`echo_proof_consecutive=3`/`echo_proof_delay_tolerance_ms=50`；`_note_proof_estimate()` 三条同时满足才算证明；详情暴露 `echo_proof_state`/`echo_proof_run`/`echo_proof_delay_ms`（状态码 `no_estimate`/`alignment_not_ok`/`correlation_below_threshold`/`delay_implausible`/`accumulating`/`echo_proven`）。**测试**：新增 `tests/duplex/test_barge_in_echo_proof.py`（15 项，含真机伪证明数据回归：0.6005/746、0.7172/−780、0.7458/1589 **永远不能**证明）；`test_auto_voice_barge_in` 4 个停播路径用例改注入证明替身。**验证**：新专项 15 项 OK；打断相关 178 项 OK；全量 **2585 → 2600 tests OK (skipped=3)**（234.990s）。交接 `checkpoints/2026-10-03-P10-R4B-echo-proof-tightening-handoff.md`。⚠️**未做**：真机复测——**复测通过前本机继续用 `--barge-in-mode off`**（不监视 ⇒ 不会假停播）；对齐估计本身为何给出 1589/−780 ms **未查明**（超出 R4-A 记录的 ±600 ms 搜索范围）；阈值未真机调参。
  - **播报截断事实可视化（2026-10-03，用户 `--barge-in-mode off` 复测"仍有时中断"）**：`off` 下 16 轮**全部** `barge_in_enabled=false`、**零 `barge_in_interrupted`**（打断路径排除），有播报的轮次一律 `spoken=true / reason=speech_finished`（**应用认为播完**但你听到被截断）。**机制**：`SessionStream.finish()` 调有界 join 的 `StreamingVoiceSession.finish()`（`segment_timeout_s=30s`）后 `finally: close()` ⇒ `close()→cancel()` **取消仍在播放的语音**；超时事实 `finish_timed_out`/`late_events` **从未被 `SessionStream` 读取**（连公开 `record` 都没有）⇒ 只能记 `speech_finished`。**修复（只做事实+如实上报）**：`SessionStream` 增公开 `record`，`finish()` 记 `finish_timed_out`/`late_events`/`dropped_pushes`/`cancelled`；`StreamingSpeaker` record 增单调 `index`；`_speak()` 在 `finish_timed_out` 时返回 **`(False, "speech_truncated")`**；`AutoTurnFacts.speech` + `log_details()` 增 `speech_*` 九项（无文本）。**验证**：新专项 8 项 OK；`tests/tts` 563、`tests/voice_loop` 126、会话/主程序 181 项 OK；全量 **2600 → 2608 tests OK (skipped=3)**（234.872s）。交接 `checkpoints/2026-10-03-P10-R4B-speech-truncation-observability-handoff.md`。⚠️**截断本身未修**——真凶（有界 join 超时后 close 取消 / worker `wait` 提前结束 / `push_text_chunk` 被拒）需一次真机运行读新字段定案。
  - **播报截断修复（2026-10-03，第 4 轮真机点名真凶）**：带 `speech_*` 事实的真机显示——turn 24（**133 字**）`speech_finished` ✅（`played_segments=6`/`completed_sentences=6`/`finish_timed_out=false`/`error=null`）；turn **26（249 字）`speech_failed`** 且 **`cancelled=true`**、`played_segments`/`completed_sentences`/`finish_timed_out`/`dropped_pushes` **全 null**（`finish()` 没走完）；turn 29 `text_only` + **`device_action=fallback_to_text`**（R3 输出设备判定，另一类问题）。**根因**：流式会话句子队列**预算 8 段**，`finish()` 越预算显式抛 `streaming_queue_budget_exceeded`；闭环一直**整段一次性推送**（249 字 → 大量句段 → 越预算 → 抛错 → `_cancel_stream` 取消剩余语音）。**修复**：`split_for_push()`（按句末/换行切分、单块 ≤80 字、不增删字符）+ `SessionStream.push_text_chunk()` **按句推送 + 有界背压**（`pending_sentences`≥4 就等，有界超时后继续，绝不挂死）；`_speak()` except 分支记 **`speech_failed:<ExceptionName>`**。**验证**：相关 147 项 OK；全量 **2608 → 2613 tests OK (skipped=3)**（82.029s）。交接 `checkpoints/2026-10-03-P10-R4B-speech-truncation-fix-handoff.md`。⚠️**未做**：真机复测（修复是否真的消除"说一半就停"必须再跑一次）；`text_only`+`fallback_to_text` 未修；队列预算未改。
  - **下一步（最新，按优先级）**：① **真机复测**（`--barge-in-mode off`，先来一轮长回复确认不再截断，再考虑开 `confirm`）；② 真机复测回声证明收紧（是否仍假停播）；③ 三件套真机验证；④ `text_only`/`fallback_to_text`（R3 输出设备）另开一批；⑤ 安全口径变更、方案 C 剩余项、R5/R6。
  - **未做（不伪造）**：前置音频**未**并入下一轮 ASR（`pre_roll_merge_unsupported`，需 audio 层 pre-roll 注入接口，否则总线 `frame_discontinuity` 会打掉整轮）；输出设备重连不可用（waveOut 走 `WAVE_MAPPER`，无固定输出设备 id）→ 按用户决定回退文字。

**2026-10-01 当日全部收口（UI 批次 2 + 修复批次 3-4 + P9 B3/B4 + UI 视觉改版）**：
- 提示窗可见文案收敛为 **4 态（待机/聆听/播报/失败）**，修掉「全程待机」与「失败」误报，新增**阶段时间线与 artifact**；
- **收音「最后一个字被吞」**已修：改用 Silero **段级端点**（`empty()`/`flush()`）+ 尾段定稿 + 结束判定时间线；
- 追加修复：`release()` 收尾前排空总线、输入停滞看门狗、**每轮重置 VAD**（上一轮残留段曾让下一轮 40ms 就收尾）、**最小轮长 300ms**、`app/main.py --voice` 接提示窗；
- **P9 B3 唤醒词门禁**与 **B4 声纹误触发过滤**已完成：模型已下载并**真机正例+负例均通过**（不喊「小饼」不收音；他人/电视说话被丢弃且不提交不播报）；
- **UI 视觉改版（方案 4）已完成并真机确认**：浅色白底圆角卡片（16px）+ 柔和外阴影 + 左侧状态色条 + 标题胶囊 + 状态色正文；渲染改走 `UpdateLayeredWindow` 逐像素路径（`app/ui/hud_render.py` 纯像素核心 + `voice_hud.py` Win32 胶水），窗口 320×96 → 336×112（含阴影留白）、卡片可见位置不变；失败可一次性降级为旧纯色卡片；
- 全量回归 **2055 tests OK (skipped=3)**（该批次的中间值）、`compileall` exit 0；UI 改版改动已提交并 push；**免按键接主程序批次已于同日完成、提交并真机试听通过（详见下文）**。

## 1. 硬性规则（不要违反）

1. **不伪造**：不得伪造 Agent 回复、测试结果、证据或版本号；设备完成 ≠ 有人听见，听感必须由用户确认。
2. **写操作先确认**：改/写/删文件、安装依赖、执行有副作用命令、提交代码，都要先给方案（做什么/为什么/影响/步骤/回滚）等用户确认；每次改文件单独确认。
3. **真机操作**：麦克风/扬声器/真实 Provider 只在用户明确要求或授权后使用；本轮已获授权安装 `sounddevice`/`sherpa-onnx` 并使用本机设备。
4. **安全链不可绕过**：不降低授权等级、不复用旧授权、不开放任意命令；`app/safety`、`app/executor`、`app/plans` 语义不得被语音或 UI 改动。
5. **回复用简体中文、简洁**。

## 2. 环境事实（已核实）

| 项 | 值 |
|---|---|
| 项目根 | `<PROJECT_ROOT>` |
| Python | `.venv\Scripts\python.exe`（3.12.10）；另有 `.python312\python.exe`（无第三方依赖） |
| 测试命令 | `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q` |
| 当前基线 | **2844 tests OK (skipped=3)**（白名单修复后实测 **209.7 s**、exit 0；2842 + 2 项回归；此前 2842/193.1 s（2026-10-13 收口：R4-B5 B1–B3 90 项 + 探针自检 5 项）、分模块 `tests/tts` 633 / `tests/foundation` 352 / `tests/voice_loop` 152；此前 2696/320.6 s（2026-10-09 播报截断结案，分模块 619/335/144）、2613/82.0 s（2026-10-03 播报截断修复）、R4-B P1/P2 2580/235.4 s、方案 C 2553/220.6 s、R4-B-1 2525/231.2 s；2026-10-02 P10 R4-A 2476 → 2496；更早 2446 → 2335 → 2301 → 2299 → 2256 → 2217 → 2087 → 2084 均已独立重跑复核）；`compileall -q app tests tools` exit 0 |
| 免按键入口 | `python -m app.main --voice --auto`（默认 `--vad-mode silero`，模型 `models/openwakeword/silero_vad.onnx`；`--vad-mode energy` 可免模型）；共享层 `app/runtime/auto_voice.py`，runner 消费同一实现；唤醒词/声纹默认关闭（CLI 开关**已接**：`--kws-mode gate` / `--voiceprint-mode filter`，默认 `off`）；**P10 R4 三个新开关默认全关**：`--barge-in-mode off|duck|confirm`、`--aec-reference-mode off|same_process`、`--device-recovery-mode off|safe_point`；**2026-10-01 真机试听通过（用户确认可用）** |
| 提示窗渲染 | `render_mode=pixel`（`UpdateLayeredWindow` 逐像素）；能力缺失或首帧失败会**一次性降级**为旧纯色卡片，`render_mode=legacy` 可观测；外观规格见 `implementation_plan/2026-10-01-hud-visual-refresh-design.md` |
| 已装依赖（离线 wheel + SHA-256 登记） | `sounddevice 0.5.6`、`sherpa-onnx 1.13.8`、`sherpa-onnx-core 1.13.8`、`numpy 2.5.3`、`cffi 2.1.1`、`pycparser 3.0`、`websockets 17.1`、**`pywebrtc-audio 0.2.0`（2026-10-03 R4-B-1：wheel `pywebrtc_audio-0.2.0-cp312-cp312-win_amd64.whl`，SHA-256 `0dabbdadd5d7fd7dcb88323266e5e14d46aaa136c58fa0573c4b0323b683c80b`，Apache-2.0，仅依赖 numpy≥1.24，安装 `pip install --no-index --find-links .wheelhouse pywebrtc-audio==0.2.0`；`NOTICE` 未列 vendored → 对外分发需补第三方声明）**；wheel 在 `.wheelhouse/`（已 gitignore），其余哈希见验收文档 6.1。**注意：官方 `deepseek-harness-sdk`/`runtime-bin` 的 wheel 虽在 `.wheelhouse/`，但当前 .venv 中未安装**（P7-R-29 已核实，真实对话走 HTTP 而非 SDK） |
| ASR 模型 | `models/sense-voice/model.int8.onnx` + `tokens.txt`（已就位并真机验证） |
| 唤醒词模型 | `models/kws/`（冻结布局 `tokens.txt`+`encoder/decoder/joiner.onnx`+`keywords.txt` + `test_wavs/`）；来源 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`，归档 `sha256=B2F7C896…F35F`，选 `epoch-12-avg-2` int8；用法 `--kws-mode gate --kws-model-dir models/kws --keyword 小饼` |
| 声纹模型 | `models/voiceprint/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`（28,281,138 B，`sha256=F682B514…FD11`）；已注册档案 `data/voiceprint-owner.json`（192 维、只含嵌入向量、无音频） |
| `models/` 归属 | 整个 `models/` 在 `.gitignore`（第 21 行）→ 模型与档案**不会进 Git**；需要时按上述来源+哈希重下 |
| 本机 TTS | VoiceStudio 监听 `127.0.0.1:3900`、`/health` 200；离线声线槽 `Microsoft Huihui Desktop` |
| 音频设备 | 默认输入「麦克风阵列 (Realtek(R) Audio)」（WASAPI 索引 11，48 kHz）；默认输出「扬声器 (Realtek(R) Audio)」（WASAPI 索引 10；回环端点索引 13）；`waveOut` 只有 1 个设备（扬声器）；**笔记本内置**；环境噪声 RMS p50 47.7 / max 150（阈值默认 600） |
| **P10 真机结论（务必先读，避免重复踩坑）** | ① **WASAPI 回环参考在本机不可用**：系统音乐能被回环抓到（rms 0.18/active 0.955），但本程序播放抓不到（WASAPI 直放同一端点 `out[10]` 录其回环 `loop[13]`，对齐 −0.06；多音 200 Hz–5 kHz 逐个频点，700 Hz 以上 9 个频点能量比**全 0.000**）；根因**未查明**。② **真机 ERLE 无有效数值**：内置麦克风阵列 DSP 默认生效且**增强页没有**「回声消除/噪音抑制/AGC」开关可关 → 麦克风侧不存在可测的线性回声路径（`mic_alignment ≈ −0.05`，与"无回声"基准 −0.038 同级；`mic_level_vs_floor_db` 仅 9.3 dB）；**需外接麦克风才能验证**。③ **AEC 参考改走方案 A**（用户批准）：同进程取"提交给设备的 PCM"（`app/tts/waveout_pcm.py` 的只读 `submit_observer` 旁路），已验证通路正确（真机 300 帧、0 错误、`elapsed 6.031s`）。 |
| **`app/tts/waveout_pcm.py` 的硬约束** | `SAMPLE_RATE` **硬编码 24 kHz 单声道 PCM16**（`BYTES_PER_MS = 48`、默认缓冲 200 ms＝9600 字节）。**凡把信号交给该后端的代码必须按 24 kHz 准备数据**，否则会**静默改变播放时长**（本批次真机连踩 3 次：帧数减半、参考只回放一半、`reference_band_ratio` 掉到 0.45）。 |
| 数据目录 | 生产用用户在 `.env` 配置的 `data_root`；测试/冒烟请用 `ASSISTANT_DATA_ROOT` 指向临时目录 |
| 本机 TTS 自动启动 | 播报通道需要本机服务（`ws_pcm`/`http_wav`/`auto`）且服务不可达时，主程序会**自动拉起 VoiceStudio** 并等待就绪（`--tts-autostart on|off|require`，默认 `on`，超时默认 90s）。启动命令来自**用户级环境变量** `ASSISTANT_TTS_PYTHON` / `ASSISTANT_TTS_BACKEND` / `ASSISTANT_TTS_CWD`（本机已配置并校验存在，**不入库**）；只关自己拉起的进程，用户自己开的服务绝不动。**2026-10-09 实测补充**：① 冷启动 **44.5 s / 53.5 s**，`GET http://127.0.0.1:3900/startup/progress` 给出分步耗时（`ml_imports` 冷 17.6 s / 热 9.3 s、**`services_start` 27–29 s 稳定占大头**），就绪后模型还要再加载 ~17 s；② 助手的就绪探测用 `GET /profiles`，而它在后端 `StartupGate` 白名单**之外** ⇒ 必须等全部启动完成才返回 200（只有 `/health`、`/startup/progress`、`/system/shutdown-intent` 在启动期可用）；③ `tts_service._spawn` 把子进程 stdout/stderr 丢进 `DEVNULL`（所以助手控制台永远看不到 VoiceStudio 启动日志；服务自己的日志在 `%APPDATA%\OmniVoice\omnivoice.log`）；④ **助手退出时用 `taskkill /PID <pid> /T /F` 关掉自己拉起的那个 pid** ⇒ 服务若由助手拉起，Ctrl+C 就会连带关掉它；要让它常驻必须**由外部先启动**（`cd 项目根; .\.venv\Scripts\python.exe backend\main.py`）并给助手加 `--tts-autostart off`，否则 `preflight` 会以 `provider_unreachable` 降级为文字模式 |
| 真实 Provider 配置 | 默认 `ASSISTANT_PROVIDER=local`；仅在 `deepseek` + `ASSISTANT_DEEPSEEK_OPT_IN=true` 双条件下装配真实 transport 并接管路由；凭据来源（2026-09-30 起）：**用户级环境变量 + 项目 `.env` 双写同一新值**（hash12 `2C3DFCC5498D`）；`inject_dotenv_secret()` 只对白名单键、仅在进程环境缺失时注入，不打印不落盘。`.env` 已 gitignore 且未被跟踪 |
| Git | 仓库存在；工作区改动见第 5 节 |

## 3. 已完成（证据链接）

| 阶段 | 状态 | 关键证据 |
|---|---|---|
| P0–P4 | complete | `implementation_plan/checkpoints/P0/P1/P2/P3/P4-handoff.md` |
| P5 授权/执行/验证/审计/恢复/重试 | complete | `checkpoints/P5-handoff.md`、`2026-09-21-P5-integration-acceptance.md` |
| P6 ASR 按键输入 | complete | `checkpoints/P6-handoff.md`、`2026-09-21-P6-integration-acceptance.md` |
| P7 TTS 双路径 + 阶段验收 | complete | `checkpoints/2026-09-30-P7-stage-acceptance-final.md` |
| P7-R 意图路由（R-01…R-19 等） | 离线契约完成 | `checkpoints/2026-09-24-P7-R-*`、`2026-09-25-P7-R-*`；真实 Provider 仍 STOP |
| **P7-R-27 真实 Provider 有界探测** | **complete（受限真实探测）** | `checkpoints/2026-09-30-P7-R-27-real-provider-bounded-probe-handoff.md`；证据 `artifacts/2026-09-30-R-real-provider-probe/`（10 次请求，chat/task/ambiguous 全部通过严格契约） |
| **P7-R-28 解除运行时门禁（批次 A）** | **complete** | `checkpoints/2026-09-30-P7-R-28-deepseek-runtime-gate-lift-handoff.md`；双条件开关 + 真实 transport 接管路由 + 不 fallback；端到端真实 `task/open_application/0.95`（731 ms） |
| **P7-R-29 真实对话 Agent（批次 B）** | **complete（端到端真实回复已验证）** | `checkpoints/2026-09-30-P7-R-29-deepseek-real-chat-agent-handoff.md`；HTTP chat completions、非流式单轮、`agent_ready/runtime_ready=True`，真实回复 1771 ms（路由 + 回复 2 次请求） |
| **P8 按键语音闭环** | **complete（阶段级 + 真机）** | `checkpoints/P8-handoff.md`、`2026-09-30-P8-integration-acceptance.md`（6.1 依赖 / 6.2 三个真机缺陷修复 / 6.3 声学回环 / 6.3.1 真人实测） |
| P8 真机验收 runner | 完成 | `tools/p8_real_voice_loop_runner.py`；听感确认件 `artifacts/2026-09-30-P8-real-voice-loop/p8-user-heard-confirmation-20260930T135551Z.json` |
| **P8 语音入口主程序接线** | 完成 | `app/runtime/voice_entry.py`、`app/main.py --voice`；`checkpoints/2026-09-30-P8-voice-entry-main-wiring-handoff.md` |
| **P9 B1+B2 自动收音与连续对话边界（含修复批次 3-4）** | **complete（真机验收通过）** | `checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`；接口冻结 `2026-09-30-P9-batch1-2-interface-freeze.md`；听感登记件 `artifacts/2026-09-30-P9-auto-voice-loop/p9-user-heard-main-route-confirmation-20261001T030624Z.json`；全量 1808 tests OK (skipped=3) |
| **P9 主路线 VoiceStudio（ws_pcm）** | 已接通并真机验收 | 启动方式：`<STUDY_ROOT>\GitHub_pro\VoiceStudio-App\setting_py\VoiceStudio\project\.venv\Scripts\python.exe backend\main.py`（`127.0.0.1:3900`，cuda/RTX 3050）；**直接跑 `VoiceStudio.exe` 无效** |
| **P9 UI：右下角「正在对话」提示窗** | complete（UI 批次 1 真机肉眼确认；批次 2 文案收敛为 4 态并修复两处缺陷） | `app/ui/voice_hud.py`（零依赖 ctypes 自绘，内部 9 态 / 可见 **待机·聆听·播报·失败** 4 态、不抢焦点、可拖动、不显示识别正文、无轮次行；`VOICE_HUD_SCHEMA_VERSION=2`）；runner 默认显示、`--no-hud` 可关；HUD 离线测试 55 项 + runner 侧 `HudWiringTests`/`SpeechOutcomeLabelTests` 6 项；用户确认"看到了，右下角有小窗" |
| **P9 B3 唤醒词门禁** | **complete（真机验收通过）** | `app/audio/kws.py` + runner `--kws-mode gate`；`models/kws`（中文 wenetspeech KWS）；真机 2 轮：等待→命中→开新会话，识别正文**不含唤醒词**（唤醒音频不进 ASR）；artifact `p9-auto-voice-loop-20261001T063603Z-*.json` |
| **P9 B4 声纹误触发过滤** | **complete（真机正例+负例通过）** | `app/audio/voiceprint.py` + `tools/p9_voiceprint_enroll.py` + runner `--voiceprint-mode filter`；`models/voiceprint`；正例 3/3 匹配并播报、负例 3/3 `end_reason=voiceprint_rejected`（score 0.20/0.17/-0.07）、**被丢弃轮次连 ASR 都不跑**；artifacts `…T064855Z` / `…T065251Z` |
| **P9 UI 视觉改版（方案 4 卡片胶囊）** | **complete（真机肉眼确认 + 全量回归）** | `app/ui/hud_render.py`（新增纯像素核心）+ `app/ui/voice_hud.py`（DIB/`UpdateLayeredWindow`/GDI 文字/一次性降级）；`checkpoints/2026-10-01-P9-hud-visual-refresh-handoff.md`；设计规格 `implementation_plan/2026-10-01-hud-visual-refresh-design.md`；`VOICE_HUD_SCHEMA_VERSION=3`；新增 48 项纯像素测试 + 8 项逐像素路径测试；全量 **2034 tests OK** |
| **P9 阶段级集成验收** | **complete（2026-10-01）** | `checkpoints/2026-10-01-P9-stage-acceptance.md`：五条退出条件逐条判定通过；全量 **2084 tests OK (skipped=3)**；`tests/dialog` 56 / `tests/audio` 298 / `tests/voice_loop` 113 / P9 集成 173 / UI+入口+TTS 专项 176；真机逐项确认（含"用户未提供控制台数值"的边界说明） |
| **P0–P9 整体测试与验证（阶段 4 / 阶段 5）** | **complete（2026-10-02）** | `checkpoints/2026-10-02-P0-P9-stage-acceptance.md`：独立重跑复核全量 **2084 tests OK (skipped=3)** + `compileall` exit 0；五条退出条件代码侧重新取证；交付物 21/21 齐备 + artifact 22 目录/128 文件；登记 14 条文档差异、2 项非阻断项与 3 处可复核性受限项 |
| **P9 收尾批次（孤儿测试 / BOM / P9-handoff）** | **complete（2026-10-02）** | ① `tests/conversation/test_manager.py` 3 个孤儿测试移入 `ConversationManagerTests`（19→22 项全绿，生产代码未改；其中"同 `turn_id` 幂等不可替换"此前全仓无覆盖）；② `tests/foundation/test_voice_hud.py` 去 UTF-8 BOM（全仓 BOM 26→25，回到基线）；③ 新建 `checkpoints/P9-handoff.md`。全量基线 **2084 → 2087 tests OK (skipped=3)**、`compileall` exit 0 |
| **P10 R1a+R1b 两阶段打断** | **complete（2026-10-02）** | `app/duplex/barge_in.py`（56 项）+ `barge_in_orchestration.py`（40 项）；疑似降音量不建轮次、确认后旧回复永不续提交、误触发平滑恢复、提示被打断即失效；交接 `checkpoints/2026-10-02-P10-R1a-*.md` / `-R1b-*.md` |
| **P10 R2a AEC/降噪降级框架** | **complete（2026-10-02）** | `app/audio/aec/__init__.py`（34 项）；`aec_ns → aec_only → strict_no_aec → half_duplex`、7 个原因码、**未应用 AEC 绝不声称已消除回声**、任何级别保留文字模式 |
| **P10 R2b-1 参考时间线 + NLMS** | **complete（2026-10-02）** | `app/audio/aec/reference.py`、`nlms.py`（39 项）；真 FIR 回声尾部 ERLE 16~48 dB、权重命中真实增益；**块式梯度不收敛已否决**；延迟须 ≥1 样本（写成测试） |
| **P10 R2b-2 真机 AEC 实测 + 方案 A** | **complete（2026-10-02；真机 ERLE 无有效数值）** | `app/audio/aec/reference_source.py`、`app/tts/waveout_pcm.py` 只读旁路、`tools/p10_*` 四个工具（45 项）；**回环参考在本机实测不可用**（四轮证据）；改方案 A 后播放通路正确（真机 300 帧/0 错误/6.031s），但**麦克风阵列 DSP 不可关闭**导致无可测回声 → **需外接麦克风**；交接 `checkpoints/2026-10-02-P10-R2b-2-real-device-handoff.md` |
| **P10 R3 设备会话与安全空闲点恢复** | **complete（2026-10-02）** | `app/devices/session.py`（34 项）；恢复只在安全空闲点、不切未知设备、切换下一轮生效、输出优先、不自动重试忙/权限/不兼容、保留文字模式；交接 `checkpoints/2026-10-02-P10-R3-device-recovery-handoff.md` |
| **P10 R4 会话循环接线** | **complete（2026-10-02；默认全关；本机打断 fail-closed）** | `app/duplex/barge_in_channel.py`（50 项）+ `app/duplex/barge_in_safety.py`（17 项）+ `app/runtime/auto_voice.py` 接线（55 项）+ `app/tts/waveout_pcm.py` 可选增益钩子（13 项）+ R1a `min_suspect_frames`（6 项）；独立打断通道 + 安全门（插话永不授权）、R2a 降级消费、R3 设备策略安全空闲点、R2b 参考接线；**真机三轮**：① 默认 local 路由无播报（`text_only`）→ 打断不触发也不报错；② 开真实 Provider 后通道真机工作，并**暴露"音量反复跳动"+"确认结构性不可达"两个缺陷 → 均已修**；③ 再暴露**假确认（没插话也停播）→ 已按设计 fail-closed**（无可用回声参考则不启用打断 + 启动打印原因）；交接 `checkpoints/2026-10-02-P10-R4-session-loop-handoff.md` §7/§9/§9b/§9c |

> **当前全量基线：2844 tests OK (skipped=3)**（白名单修复后实测 **209.7 s**、exit 0）、`compileall -q app tests tools` exit 0；分模块本轮未重测（上一轮 2026-10-12：`tests/tts` 633、`tests/foundation` 352、`tests/voice_loop` 152）；本轮新增用例：`tests/duplex` 90 项（R4-B5 B1–B3）、`tests/foundation` 5 项（探针自检）、`tests/voice_loop` 2 项（P1 白名单回归）。

P8 真机结论（如实）：两轮按键实测 `accepted/submitted/spoken` 全 true、VoiceStudio WS 播报全部完成、五类轮次身份互不相同；用户确认两句都听到、识别基本正确。声学回环中真实 SenseVoice 把朗读句识别为「你好，测试一下今天天气怎么样。」。

## 4. 未完成 / 待授权（按优先级）

| # | 事项 | 需要什么 | 说明 |
|---|---|---|---|
| 0 | **免按键自动收音接主程序** | ✅ 已完成 + **真机试听通过** | `app/runtime/auto_voice.py`（共享层）+ `app/main.py --voice --auto`；runner 改为消费同一实现；离线全量 2055 OK；用户 2026-10-01 真机确认可用。命令：`.\.venv\Scripts\python.exe -m app.main --voice --auto`（或加 `--vad-mode energy`） |
| 1 | **P9 B3 唤醒词 / B4 声纹** | ✅ 已完成并真机验收（runner 路径）；**主程序 CLI 已接** | 模型已下载（`models/kws`、`models/voiceprint`）；唤醒词只开启新输入会话（唤醒音频不进 ASR），声纹只做误触发过滤（**前置于 ASR**：被丢弃的轮次连解码都不跑），**都不得决定授权**。证据：`checkpoints/2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md` §4.4/§4.5 |
| 2 | 唤醒词门禁 / 声纹过滤接主程序 CLI | ✅ 已完成 + 真机确认 | 新增 `--kws-mode gate`（配 `--kws-model-dir`/`--kws-keywords-file`/`--keyword`/`--kws-threshold`/`--kws-wait-seconds`）与 `--voiceprint-mode filter`（配 `--voiceprint-model`/`--voiceprint-profile`/`--voiceprint-threshold`），默认全关；要求了但模型/档案缺失时如实报错并降级为文字。示例见免按键交接 §7 |
| 3 | 本机 TTS 按需自动拉起 | ✅ 已完成 + 真机确认 | `app/runtime/tts_service.py` + `--tts-autostart on|off|require`；命令来自用户级环境变量 `ASSISTANT_TTS_PYTHON/BACKEND/CWD`（已配置）；只关自己拉起的进程 |
| 4 | **P9 阶段级集成验收** | ✅ 已完成（`2026-10-01-P9-stage-acceptance.md`） | 五条退出条件逐条通过；P9 已标记 complete |
| 5 | **阶段 4 整体验证** | ✅ 已完成（2026-10-02，无需再授权） | `2026-10-02-P0-P9-stage-acceptance.md`：全量 2084 tests OK 重跑复核 + 交付物清点，阶段 4/5 判定完成 |
| 5b | **P10（已开工）**：R1a/R1b/R2a/R2b-1/R2b-2（含方案 A）/**R3 设备恢复**/**R4 会话循环接线**已完成；**R5/R6 待授权（R2b 真机验证需外接麦克风）** | 需用户授权 / 需外接麦克风 | 已完成：两阶段打断状态机（`app/duplex/barge_in.py`）、编排语义（`barge_in_orchestration.py`）、AEC 降级框架（`app/audio/aec/__init__.py`）、AEC 参考时间线 + NLMS（`reference.py`、`nlms.py`）、**同进程参考源 + waveOut 只读旁路（`reference_source.py`、`app/tts/waveout_pcm.py`）**、**设备会话与恢复策略（`app/devices/session.py`）**、**独立打断通道（`app/duplex/barge_in_channel.py`）+ 安全门（`barge_in_safety.py`）+ 会话循环接线（`app/runtime/auto_voice.py`，默认全关）+ 可选播放增益钩子**。**回环参考在本机实测不可用**（四轮证据，根因未查明）；**真机 ERLE 无有效数值**：笔记本内置麦克风阵列 DSP 不可关闭，`mic_alignment≈−0.05`（与"无回声"基准同级）、`mic_level_vs_floor_db` 仅 9.3 dB。待做：**R5** Provider 冷却；**R6** 长期运行健康；**R4 残留**（前置音频并入下一轮 ASR 需 audio 层 pre-roll 注入接口；输出重连需固定输出设备 id；降音量粒度受 200 ms 缓冲块限制）；R2b 真机验证（需外接麦克风） |
| 5c | **长回复播报截断（2026-10-09 结案）** | ✅ 已修复 + 真机验收 | 根因＝空行切出的**纯空白碎片**：`pcm_seq` 构造请求时 `text_blank` 中止（真机 294 字只播 1/13 段）；`ws_pcm`（默认通道）被分段器 `EMPTY_TEXT` 拒收且**不消费序号** ⇒ 其后全判越序、**静默丢尾**（233 字只播 74 字、`error` 为空，旧版据此谎报"播完"）。修复：`sequential_session` 入队处丢弃纯空白碎片；`streaming_session` 纯空白 push 与空 push 同等对待 + `OUT_OF_ORDER` 升级为记录型致命错误（新固定码 `streaming_push_out_of_order`）。真机验收 `9/9`、`114/114`、`398/398`。同批：HUD 播报状态（`auto_voice._complete_turn_with_hud`）、`verify_provider` 固定短码。证据见 §6 第 8 项 |
| 5d | **本批仍未做（如实记录）** | 待授权 | ① P2 播放链路声学证据（每句 PCM 字节/时长、RMS/peak、waveOut submitted/returned、必要时 loopback）；② `settings.tts_pcm_max_queued_audio_ms=8000` 接入流式路径（须 ≥ 一个句子额度）；③ barge-in 主动取消仍报 `speech_finished`（有意的安全口径取舍，改前需用户拍板）；④ `SPEECH_CHARACTERS_PER_SECOND=4.8` 余量未实测；⑤ "混音"未复现；⑥ 首次 autostart 运行时"卡在输出设备行之前、无任何事件"**未复现未定位**；⑦ 交接文档旧 §11 乱码已重建（2026-10-09） |
| 6 | 多轮上下文 / 流式（SSE）/ 费用限流 / persona | 另行授权 | 当前真实对话为**单轮、非流式、无速率限制**；注：persona 加载器（`app/persona/`）已交付但**未接入运行时** |
| 7 | 任务链路接默认运行时 | 需真实 Agent 计划提议 | `app/runtime/orchestration/` 已就绪（P8 批次 4/5），尚未消费真实路由 |
| 8 | `threshold_mode=auto` 校准接 CLI | 可选 | 共享层已支持（runner 有 `--threshold-mode auto`），主程序暂用固定阈值 600 / `--speech-threshold` |
| 9 | 提示窗已知小瑕疵 | 可选 | GDI 文字阶段会让胶囊圆角约 2px 像素 alpha 归零（**未附像素级证据的自述项**，肉眼不可见）；彻底消除需改为离屏预排版文字 |
| 10 | **P9 收尾三项** | ✅ 已完成（2026-10-02） | ① `tests/conversation/test_manager.py:45/78/97` 三个 `test_*` 原落在非 `TestCase` 的 `FakeAdapter` 内、永不被收集 → 已移入 `ConversationManagerTests`（19→22 项全绿，断言未改写、生产代码未改）；② `tests/foundation/test_voice_hud.py` 去 UTF-8 BOM（全仓 BOM 26→25）；③ 新建 `checkpoints/P9-handoff.md`。全量基线 2084→**2087** |

## 5. 工作区状态

- **HEAD**：本轮在 `978f374` 之后共 **10 个提交，已 push 到 `origin/codex/review-playback-lifecycle`**：`3bac6a4`（HUD A/B/C）、`3d1f169`（长回复收尾 P1 + 2026-10-09 截断结案 + R4-B5 B1–B3）、`9ea7dd3`（AEC 探针产物）、`f62167b`（P7 产物补录）、`25bbf08`（R4-B5 B4 真机结论）、`b69f784`（豆包逆向研究）、`320ed41`（文档同步）、`2931321`（P1 白名单修复 + 回归）、`2ec768a`（P1 ≥800 字验收归档）、**UI A/B/C 真机复验归档（本文件所在批）**。
- **状态**：**54 项改动（30 改 24 新）已全部入库**；`artifacts/p10-r4b5/doubletalk.json` 已按结论删除（不入库）；`task_plan.md` 末尾空行已删；**未 push**。
- 下表 ①–⑨ 为**历史分组记录**（其中 ①–⑦ 早已入库，⑧⑨ 已随本轮提交落地）；本轮的实际分组与合并原因见 2026-10-13 提交信息与 `progress.md` 的 2026-10-13 一节。

| 组 | 文件 |
|---|---|
| ① P9 收尾（代码/测试） | `tests/conversation/test_manager.py`（3 个孤儿测试移入 `ConversationManagerTests`，19→22 项）、`tests/foundation/test_voice_hud.py`（去 BOM） |
| ② P10 打断（新） | `app/duplex/**`（`__init__.py`、`barge_in.py`、`barge_in_orchestration.py`、**`barge_in_channel.py`（R4）**、**`barge_in_safety.py`（R4）**）、`tests/duplex/**`（`__init__.py` + 8 个测试文件） |
| ③ P10 AEC（新） | `app/audio/aec/**`（`__init__.py`、`reference.py`、`nlms.py`、`reference_source.py`）、`app/tts/waveout_pcm.py`（只读旁路钩子 + **R4 可选 `gain_provider`/`PlaybackGain`**） |
| ④ P10 设备恢复（新） | `app/devices/**`、`tests/devices/**` |
| ⑤ P10 R4 会话接线（改+新测试） | `app/runtime/auto_voice.py`（1084 → 1818 行）、`app/runtime/voice_entry.py`（参考/增益接线 + `build_same_process_reference`）、`app/duplex/barge_in.py`（R1a `min_suspect_frames`）、`app/duplex/barge_in_channel.py`（`duck_requires_reference` + 检测器能量门）、`app/main.py`（三个 CLI 开关 + `_report_barge_in_status`）、`tests/foundation/test_auto_voice_barge_in.py`（新，55 项）、`tests/tts/test_waveout_gain.py`（新，13 项） |
| ⑥ 真机诊断工具（新） | `tools/p10_aec_probe.py`、`tools/p10_aec_probe_selfcheck.py`、`tools/p10_wasapi_loopback_check.py`、`tools/p10_aec_wav_diagnose.py` |
| ⑦ 文档 | `task_plan.md`、`progress.md`、`NEXT-SESSION.md`、`implementation_plan/implementation-roadmap.md`、`implementation_plan/checkpoints/2026-10-01-P9-stage-acceptance.md`（事后修订）、新建 9 份文档（P9-handoff、P0–P9 验收、R1a/R1b/R2a/R2b-1/R2b-2/R3/**R4** 交接）、`artifacts/2026-10-02-P10-aec-probe/*.json`（8 份 JSON 摘要，**音频 WAV 已按用户指示全部删除**） |
| ⑧ **2026-10-09 播报截断结案（改+新）** | 代码：`app/tts/sequential_session.py`（入队处丢弃纯空白碎片）、`app/tts/streaming_session.py`（纯空白 push 与空 push 同等对待 + `PUSH_OUT_OF_ORDER` 记录型致命错误）、`app/runtime/auto_voice.py`（`_complete_turn_with_hud`，HUD 播报状态）、`app/main.py`（轮次结束设 `IDLE` + `voice_entry_unavailable.code`）、`app/tts/private_selection.py`（`verify_provider` 固定短码 + `VoiceSelectionError.code`）；测试：`tests/tts/test_pcm_sequential_pipeline.py`、`tests/tts/test_streaming_session.py`、`tests/tts/test_real_playback.py`、`tests/foundation/test_voice_status.py`、`tests/foundation/test_main_auto_voice.py`；文档：新建 `checkpoints/2026-10-07-voice-playback-truncation-investigation.md`、改建 `checkpoints/2026-10-03-P10-R4B-long-reply-provider-contract-handoff.md`（§11 乱码重建 + §12/§13）、新建脱敏证据 `checkpoints/artifacts/2026-10-09-pcm-seq-text-blank/`、`checkpoints/artifacts/2026-10-09-ws-pcm-sequence-desync/` |
| ⑨ **2026-10-12 P1 收尾 + HUD A/B/C（改+新）** | **P1 代码**：`app/tts/streaming_session.py`（`DEFAULT_STALL_TIMEOUT_S` + `_await_drain` + 三个新事实）、`app/tts/sequential_session.py`（同构停滞看门狗）、`app/runtime/voice_entry.py`（`_await_room` 顺延 + `_flat_seconds` + 三个事实）、`app/runtime/auto_voice.py`（三个 `speech_*` 字段）、`app/main.py`（`--speech-stall-timeout`）；**P1 测试（新）**：`tests/tts/test_streaming_drain_stall.py`、`tests/tts/test_pcm_seq_drain_stall.py`、`tests/tts/test_speech_stall_timeout_wiring.py`、`tests/voice_loop/test_drain_stall_backpressure.py`；**HUD 代码**：`app/ui/voice_hud.py`（拖动跟手/去二次跳位/吸附缓动 + 合成帧缓存与 `blit_cached_frame` + 进场滑入/退场 + `respect_system_animation`）；**HUD 测试**：`tests/foundation/test_voice_hud.py`（63 → 92 项）；**文档（新）**：`checkpoints/2026-10-12-P10-P1-drain-stall-handoff.md`、`checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`、`research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`，并修订 `implementation_plan/2026-10-01-hud-visual-refresh-design.md`（§1.2/§3.5 边界） |

- 提交前注意：确认不含 `.env`/`.venv`/`.wheelhouse`/`.python312`/**`models/`**/**`data/`**/**`*.wav`**（均已在 `.gitignore`）。
- 运行产物（证据，可保留）：`artifacts/2026-09-30-R-real-provider-probe/`、`.../2026-09-30-P8-real-voice-loop/`、`.../2026-09-30-P9-auto-voice-loop/`、`.../2026-10-02-P10-aec-probe/`。

## 6. 新对话最小读取集合（按顺序）

1. 本文件（含 §2 的「P10 真机结论」表——**先读它，避免重复踩回环/采样率的坑**）；
2. `task_plan.md`（顶部「当前阶段」+ 末尾「2026-10-02 P10 批次 R1a+R1b+R2a」「R2b-2」「R3」条目）；
3. **P10 批次交接（7 份，按需读）**：
   - `checkpoints/2026-10-02-P10-R1a-barge-in-state-handoff.md`（两阶段打断状态机）
   - `checkpoints/2026-10-02-P10-R1b-barge-in-orchestration-handoff.md`（编排语义：平滑恢复/轮次交接/提示失效）
   - `checkpoints/2026-10-02-P10-R2a-aec-degradation-handoff.md`（AEC 降级阶梯与硬不变量）
   - `checkpoints/2026-10-02-P10-R2b-1-aec-offline-handoff.md`（参考时间线 + NLMS；**块式梯度为何被否决**）
   - `checkpoints/2026-10-02-P10-R2b-2-real-device-handoff.md`（**真机实测全部证据 + 方案 A + 外接麦克风前置条件**）
   - `checkpoints/2026-10-02-P10-R3-device-recovery-handoff.md`（设备恢复：安全空闲点/不切未知设备/下一轮生效）
   - `checkpoints/2026-10-02-P10-R4-session-loop-handoff.md`（**会话循环接线：独立打断通道 + 安全门 + 设备策略 + R2b 参考；含未做项与真机命令**）
4. `checkpoints/P9-handoff.md` + `checkpoints/2026-10-01-P9-stage-acceptance.md`（P9 阶段级交接与验收）；
5. `checkpoints/2026-10-02-P0-P9-stage-acceptance.md`（P0–P9 整体验收：阶段 4/5 判定、14 条文档差异）；
6. P9 细节按需：`2026-10-01-P9-hud-and-capture-issues-handoff.md`、`2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`、`2026-10-01-P9-hud-visual-refresh-handoff.md` + `2026-10-01-hud-visual-refresh-design.md`、`2026-10-01-P9-auto-capture-main-integration-handoff.md`；
7. 需要动某模块时再读该模块与对应测试（`app/duplex/**`、`app/audio/aec/**`、`app/devices/**`、`app/runtime/auto_voice.py`、`app/tts/waveout_pcm.py`、`app/main.py`、`app/ui/**`、`app/audio/{kws,voiceprint,vad}/**`、`app/voice_loop/**`）。
8. **2026-10-09 播报截断结案**：`checkpoints/2026-10-07-voice-playback-truncation-investigation.md`（§6 旧结论更正 + §12 `pcm_seq` 的 `text_blank` 结案 + §13 `ws_pcm` 静默丢尾并案）、`checkpoints/2026-10-03-P10-R4B-long-reply-provider-contract-handoff.md`（§1/§3 更正 + §12/§13）；脱敏原始事实 `checkpoints/artifacts/2026-10-09-pcm-seq-text-blank/voice-turn-evidence.json`、`checkpoints/artifacts/2026-10-09-ws-pcm-sequence-desync/voice-turn-evidence.json`。
9. **2026-10-12（先读这 3 份；**最新**见顶部 2026-10-13 三条与 §9 的 R4-B5 结论文档）**：
   - `implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`（**豆包逆向：超时全为空闲式、TTS 一次请求整条流式、只有打断才允许丢尾**；证据分级 + P1–P4 改造方案）；
   - `checkpoints/2026-10-12-P10-P1-drain-stall-handoff.md`（**P1 收尾批次：三处总时长上限 → 无进展看门狗**；含修复前/后对照数字、`--speech-stall-timeout`、三个新真机事实、未做项与回滚）；
   - `checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`（**提示窗 UI 批次 A/B/C：拖动跟手/去二次跳位/吸附、进场滑入/退场、防闪、尊重系统动画开关**；含 GitHub 调研证据与配置总表）。

按需（不要默认全读）：`implementation_plan/implementation-roadmap.md`（P10/P11 卡片）、`progress.md`（历史细节）、`AGENTS.md`（协作纪律）、`implementation_plan/12-barge-in-and-aec.md`、`13-device-tuning.md`（P10 设计依据）。

## 7. 可复制的续接指令

- **继续 P10（R5/R6）**：`继续：读取 NEXT-SESSION.md 与 task_plan.md 当前阶段；P10 已完成 R1a/R1b/R2a/R2b-1/R2b-2/R3/R4，请授权并实施 R5（Provider 冷却/有界探测/恢复待启用）或 R6（长期运行健康：有界队列、临时音频清理、审计轮转、单实例健康）；先给批次计划再动手。`
- **P1 长回复真机验收（下一步，推荐）**：`继续：读 NEXT-SESSION.md 顶部 2026-10-12 第 1 条；我要跑一轮 ≥800 字长回复验收 P1，给我确切命令与预期现象（重点看 speech_drain_seconds / speech_stall_timeouts / speech_last_progress 与 played==pushed），跑完你只读 runtime.jsonl 对账。`（真实 Provider 计费，每轮 2 次请求）
- **UI 批次 A/B/C 真机看效果**：`继续：给我 --voice --auto --tts-mode none 的确切命令，我要真机看拖动跟手与进场/退场；不满意我按 handoff §6 的配置项调参（吸附/时长/最低点 alpha）。`
- **继续 P2（整段一次请求，对齐豆包）**：`继续：读 research/2026-10-12-doubao-realtime-voice-reverse-engineering.md §4 P2；授权把 VoiceStudio /ws/tts 改为"整条回复一次请求 + 客户端有界缓冲"，先给授权矩阵、批次计划与风险（服务端不背压）再动手。`
- **重建 task_plan.md 末尾乱码节**：`继续：task_plan.md 最后 9 行（2026-10-07 那节）是乱码；按 2026-10-07 交接与 2026-10-09 结案重建该节，并标注"重建（原文不可读）"，先给方案。`
- **真机打断验证（需授权开真实 Provider）**：`继续：授权开启真实 Provider（双条件 opt-in，每轮 2 次真实请求并计费）用于 R4 真机打断验证；先给执行方案与预期现象。`（首次尝试的边界见 `checkpoints/2026-10-02-P10-R4-session-loop-handoff.md` §7）
- **补 R4 残留（前置音频并入下一轮）**：`继续：授权为 audio 层加 pre-roll 注入接口（把确认插话的前置音频并入下一轮同一 utterance_id），先给批次计划与风险说明。`
- **提交本轮改动**：`继续：读取 NEXT-SESSION.md §5；按 7 组拆分给出改动清单与提交方案（先列清单、我确认后再 add/commit，push 前再问我）。`
- **R2b 真机验证（需外接麦克风）**：`继续：我已接上外接麦克风；按 checkpoints/2026-10-02-P10-R2b-2-real-device-handoff.md §7 跑 tools/p10_aec_probe.py --live-opt-in --reference same-process，据输出给我真机 ERLE 结论。`
- **R5/R6**：`继续：授权 P10 R5（Provider 冷却/有界探测/恢复待启用）或 R6（长期运行健康：有界队列、临时音频清理、审计轮转、单实例健康）；先给批次计划。`
- **语音真机确认**：`继续：我想按键实测 voice 模式并确认真实 DeepSeek 回复的听感与延迟，给我确切命令与预期现象（含每轮 2 次请求的计费提示）。`
- **多轮 / 流式 / 限流**：`继续：授权为真实对话加入多轮上下文或 SSE 流式（或费用限流）；先给授权矩阵与批次计划，我逐项确认后再动手。`
- **接着做播报残留项（2026-10-09 收口后）**：`继续：读 NEXT-SESSION.md 顶部 2026-10-09 结案与 §4 的 5c/5d；长回复播报（pcm_seq + ws_pcm）已修复并真机验收，请从 5d 未做清单里挑一项（P2 播放链路声学证据 / 队列预算接入流式路径 / barge-in 取消口径），先给批次计划与风险说明再动手。`

## 8. 常用命令速查

```powershell
cd '<PROJECT_ROOT>'

# 全量回归
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q

# 文字模式（默认，local 路由，不联网）
.\.venv\Scripts\python.exe -m app.main

# 文字模式 + 真实 DeepSeek 路由与对话（双条件 opt-in；每轮 2 次真实请求并计费，失败不回退）
$env:ASSISTANT_PROVIDER = 'deepseek'
$env:ASSISTANT_DEEPSEEK_OPT_IN = 'true'
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
.\.venv\Scripts\python.exe -m app.main

# 按键语音 + 真实 DeepSeek（同一 ConversationManager；需麦克风）
.\.venv\Scripts\python.exe -m app.main --voice --tts-mode ws_pcm

# 回滚：去掉 opt-in（或改回 local）即恢复原行为
Remove-Item Env:ASSISTANT_DEEPSEEK_OPT_IN

# 按键语音模式（需麦克风；TTS：ws_pcm 默认 / edge / none）
.\.venv\Scripts\python.exe -m app.main --voice
.\.venv\Scripts\python.exe -m app.main --voice --tts-mode edge
.\.venv\Scripts\python.exe -m app.main --voice --tts-mode none --speech-threshold 400

# 真机验收 runner（带 artifact 与听感登记）
.\.venv\Scripts\python.exe tools\p8_real_voice_loop_runner.py --live-opt-in --check-only
.\.venv\Scripts\python.exe tools\p8_real_voice_loop_runner.py --live-opt-in --turns 2 --tts-mode ws_pcm

# R 支线真实 Provider 有界探测（先离线预检；真实探测需 --live-opt-in 且逐项授权）
.\.venv\Scripts\python.exe tools\r_real_provider_probe.py --check-only
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
.\.venv\Scripts\python.exe tools\r_real_provider_probe.py --live-opt-in --thinking disabled --diagnose --max-requests 1 --sample-id chat-1

# P9 麦克风电平标定（只报 RMS 分位数；音频不落盘；需 --live-opt-in）
.\.venv\Scripts\python.exe tools\p9_mic_level_calibration.py --live-opt-in --seconds 6 --label quiet
.\.venv\Scripts\python.exe tools\p9_mic_level_calibration.py --live-opt-in --seconds 6 --label speech

# P9 免按键自动收音真机试听（主路线 VoiceStudio：先确认 http://127.0.0.1:3900/health 返回 200）
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --check-only
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 1 --tts-mode ws_pcm
# 备用/只听识别：--tts-mode edge（离线声线）或 --tts-mode none（不播报）
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 1 --tts-mode edge --silence-ms 2000 --max-record-seconds 12

# P9 B3 唤醒词门禁真机（不喊「小饼」不收音；唤醒音频不进 ASR）
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 2 --tts-mode ws_pcm --kws-mode gate --kws-model-dir models\kws --keyword 小饼

# P9 B4 声纹注册（只写嵌入向量、不写音频；--out 不给就只打印摘要）
.\.venv\Scripts\python.exe tools\p9_voiceprint_enroll.py --live-opt-in --check-only
.\.venv\Scripts\python.exe tools\p9_voiceprint_enroll.py --live-opt-in --seconds 6 --model models/voiceprint/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx --speaker owner --out data/voiceprint-owner.json

# P9 B4 声纹过滤真机（本人说话正常；他人/电视说话应 end_reason=voiceprint_rejected）
.\.venv\Scripts\python.exe tools\p9_auto_voice_runner.py --live-opt-in --turns 3 --tts-mode ws_pcm --voiceprint-mode filter --voiceprint-model models/voiceprint/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx --voiceprint-profile data/voiceprint-owner.json --voiceprint-threshold 0.5

# ---------- P10：R4 打断（会话循环接线，默认全关）----------
# ⚠️ 真机已验证边界：默认 local 路由无真实回复 → 每轮 text_only（无播报窗口），打断不触发也不报错。
#    要真机验证打断，必须先开真实 Provider（双条件 opt-in；每轮 2 次真实请求并计费）：
#    $env:ASSISTANT_PROVIDER='deepseek'; $env:ASSISTANT_DEEPSEEK_OPT_IN='true'
#    $env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
# 只降音量（第一阶段）：播报中说话先降到约 25%，确认不生效
.\.venv\Scripts\python.exe -m app.main --voice --auto --barge-in-mode duck --aec-reference-mode same_process
# 确认后停播（+ 安全门）：持续说话 200~300 ms 后停止本次播报
.\.venv\Scripts\python.exe -m app.main --voice --auto --barge-in-mode confirm --aec-reference-mode same_process
# 设备策略（安全空闲点决策 / 各试一次重连 / 输出断开回退文字）
.\.venv\Scripts\python.exe -m app.main --voice --auto --device-recovery-mode safe_point
# 回滚：去掉开关即回到 R4 前的行为（默认全关）

# ---------- P10：AEC 真机探测（新）----------
# 前置检查（不开设备流、不响不录）
.\.venv\Scripts\python.exe tools\p10_aec_probe.py --check-only
# 方案 A 真机测量：参考 = "提交给设备的 PCM"（同进程旁路，**不需要回环**）；会放 6 秒类语音噪声
.\.venv\Scripts\python.exe tools\p10_aec_probe.py --live-opt-in --reference same-process
# 回环路线（本机实测不可用，保留用于换机器复核）
.\.venv\Scripts\python.exe tools\p10_aec_probe.py --live-opt-in --loopback-only
.\.venv\Scripts\python.exe tools\p10_aec_probe.py --live-opt-in --tones --loopback-only     # 多音逐频点增益
# WASAPI 输出端点 ↔ 回环一致性 / 只录回环（用系统放音乐验证回环是否有效）/ 同时录回环+麦克风
.\.venv\Scripts\python.exe tools\p10_wasapi_loopback_check.py --live-opt-in
.\.venv\Scripts\python.exe tools\p10_wasapi_loopback_check.py --live-opt-in --watch 8
.\.venv\Scripts\python.exe tools\p10_wasapi_loopback_check.py --live-opt-in --probe
# 离线自检（不需要设备）：合成场景验证参考/对齐/ERLE 口径
.\.venv\Scripts\python.exe tools\p10_aec_probe_selfcheck.py
# 注意：--save-audio 会把音频写成 WAV（本机落盘）——**默认不写**；用完记得删 audio-* 目录
```

## 9. 关键文档

**2026-10-13（最新）**：
- `implementation_plan/research/2026-10-13-P10-R4B5-doubletalk-real-device-conclusion.md`（**R4-B5 B4 真机结论：条件性 no-go**——四类可观察量不可区分、误报 268/268、APM 也分不开；含行业内做法对照与"外接麦克风"复测前置条件）
- `implementation_plan/checkpoints/artifacts/2026-10-13-r4b5-doubletalk/`（脱敏证据：README 索引 + `real-device-numbers.json` 数值转录件，**不含音频**）

**2026-10-12（先读这 3 份）**：
- `implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`（豆包实时语音逆向：证据分级、6 条做法、与本仓差距表、P1–P4 方案）
- `implementation_plan/checkpoints/2026-10-12-P10-P1-drain-stall-handoff.md`（**P1 收尾批次**：无进展看门狗、`--speech-stall-timeout`、三处总时长上限的前后对照、未做项与回滚）
- `implementation_plan/checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`（**提示窗 UI 批次 A/B/C**：拖动跟手、进场/退场、防闪、尊重系统动画、配置总表与真机复验命令）

**P10（R1a–R4，先读这 7 份）**：
- `implementation_plan/checkpoints/2026-10-02-P10-R1a-barge-in-state-handoff.md`（两阶段打断状态机：配置/状态/前置缓冲/降级/边界）
- `implementation_plan/checkpoints/2026-10-02-P10-R1b-barge-in-orchestration-handoff.md`（编排语义：平滑恢复、轮次交接、提示失效）
- `implementation_plan/checkpoints/2026-10-02-P10-R2a-aec-degradation-handoff.md`（AEC/降噪逐层降级：阶梯、原因码、硬不变量）
- `implementation_plan/checkpoints/2026-10-02-P10-R2b-1-aec-offline-handoff.md`（参考时间线 + 逐样本 NLMS：四态取窗、块式梯度否决留痕、延迟≥1 样本约束）
- `implementation_plan/checkpoints/2026-10-02-P10-R2b-2-real-device-handoff.md`（**真机 AEC 实测：回环不可用四轮证据 + 方案 A + 外接麦克风前置条件 + 调试命令**）
- `implementation_plan/checkpoints/2026-10-02-P10-R3-device-recovery-handoff.md`（设备恢复：六态健康、安全空闲点、不切未知设备、下一轮生效）
- `implementation_plan/checkpoints/2026-10-02-P10-R4-session-loop-handoff.md`（**会话循环接线：独立打断通道 + 安全门 + 设备策略 + R2b 参考 + 可选降音量；未做项与真机命令**）

**P9 / 阶段级**：
- `implementation_plan/checkpoints/P9-handoff.md`（P9 阶段级交接）
- `implementation_plan/checkpoints/2026-10-01-P9-stage-acceptance.md`（P9 阶段验收；含 2026-10-02 事后修订）
- `implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md`（P0–P9 整体验收：阶段 4/5 判定、14 条文档差异、2 项非阻断缺陷）
- `implementation_plan/checkpoints/2026-10-01-P9-hud-and-capture-issues-handoff.md`（HUD 4 态、段级端点修复、配置基线）
- `implementation_plan/checkpoints/2026-10-01-P9-hud-visual-refresh-handoff.md` + `implementation_plan/2026-10-01-hud-visual-refresh-design.md`（UI 改版规格与真机缺陷根因）
- `implementation_plan/checkpoints/2026-10-01-P9-auto-capture-main-integration-handoff.md`（免按键接主程序：共享层、CLI、降级与边界）
- `implementation_plan/checkpoints/2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`（唤醒词/声纹：模型来源与哈希、真机验收）

**P7 / P8（历史，按需）**：
- `implementation_plan/checkpoints/2026-09-30-P7-R-29-deepseek-real-chat-agent-handoff.md`（批次 B：真实对话 Agent、就绪语义、端到端真实回复、残留与回滚）
- `implementation_plan/checkpoints/2026-09-30-P7-R-28-deepseek-runtime-gate-lift-handoff.md`（批次 A：双条件开关、真实 transport 接管路由、不 fallback）
- `implementation_plan/checkpoints/2026-09-30-P7-R-27-real-provider-bounded-probe-handoff.md`（真实 Provider 有界探测：授权矩阵、请求证据、协议根因与修正）
- `implementation_plan/checkpoints/artifacts/2026-09-30-R-real-provider-probe/`（探测证据：check-only 与 live artifact）
- `implementation_plan/checkpoints/P8-handoff.md`、`2026-09-30-P8-integration-acceptance.md`、`2026-09-30-P8-voice-entry-main-wiring-handoff.md`（P8 阶段交接与真机验收）
- `implementation_plan/checkpoints/artifacts/2026-09-30-P8-real-voice-loop/`（P8 真机证据）
- `task_plan.md` / `progress.md`（根目录，已同步）

---

## ⚠️ 最新交接（2026-10-03，优先读这条）：长回复播报失败已在 **provider 契约层**点名

> **⚠️ 本节已过期（2026-10-12 标注）**：这里的"接手基线 HEAD `c44dbeb` / 2678 tests"与"错误码待补"
> 都已被后续批次取代——根因已更正为**本地请求契约层**（`text_blank`）与 `ws_pcm` 序号错位，且**两处已修复
> 并真机验收**（见本文件顶部 2026-10-09 与 2026-10-12 两条）；当前基线 **2747 tests OK (skipped=3)**、
> HEAD 仍为 `978f374`。**保留本节只为追溯当时的推理链，请以上方新条目为准。**

**一句话现状**：短/中文本播报正常，**长文本（≥240 字）必失败**；失败发生在**本机 TTS 服务（VoiceStudio WebSocket）的协议契约层**，表现为 `VoiceStudioContractError`。旧通道 `ws_pcm` 会把这错误**吞掉并谎报"播完"**（已修为一律如实报 `speech_truncated`），新通道 `pcm_seq` **直接抛错**，所以错误终于可见。

**决定性真机证据**（`--tts-mode pcm_seq`）：17 字→17 字 ✅、108 字→108 字 ✅、**242 字→6 字 + `speech_failed:VoiceStudioContractError`** ❌；旧通道同族：394 字→10 字、249 字→74 字（均曾谎报播完）。

**下一步（只两步就能点名）**：
1. 把 provider 的**错误码**与**每次请求文本长度**记进事实（`speech_error_code`/`speech_request_chars`，**只加事实不改行为**）；
2. 跑**一轮三点长回复** ⇒ 得到具体码（`audio_too_large`/`frame_too_large`/`samples_mismatch`/`sentence_timeout`…），再对症修（最可能：把 `pcm_seq` 的 `max_sentence_characters` 从 120 降到 40~48，或调 `sentence_timeout_seconds`）。

**完整交接（必读）**：`implementation_plan/checkpoints/2026-10-03-P10-R4B-long-reply-provider-contract-handoff.md`
（含：逐条证据、本会话 12 个提交的 SHA 与现状、两套通道对照、复测命令、环境坑（Python 音量曾被设为 1、`pycaw` 未装、`sndvol` 可调）、下一步优先级与纪律提醒）。

**接手基线**：HEAD `c44dbeb`，工作区 clean，**未 push**；全量 **2678 tests OK (skipped=3)**；`--barge-in-mode off` 是本机安全配置。
