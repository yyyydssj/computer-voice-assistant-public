# 任务计划：实时语音对话 + 控制电脑的 AI 助手

## 目标
做一个能与人实时语音对话、并能听懂指令后操作电脑（执行任务）的 AI 助手。

## 当前阶段
阶段 3：实现 —— **已完成（P0–P9 全部通过阶段级验收）**；阶段 4「测试与验证」与阶段 5「交付」已于 2026-10-02 完成（见下）。P9 内容：自动收音/连续对话、唤醒词门禁、声纹过滤、UI 提示窗与视觉改版、免按键接入主程序、唤醒词/声纹 CLI、本机 TTS 按需自动拉起；阶段交接 `implementation_plan/checkpoints/P9-handoff.md`；验收证据 `implementation_plan/checkpoints/2026-10-01-P9-stage-acceptance.md`；整体验收证据 `implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md`。下一步待授权：P10（打断/AEC/设备恢复/长期运行）或 P9 收尾项（孤儿测试修复 / BOM 清理 / 补 `P9-handoff.md`）。
> **2026-10-02 P0–P9 整体验收**：阶段 4「测试与验证」与阶段 5「交付」判定完成；全量基线复核 **2084 tests OK (skipped=3)**、`compileall` exit 0；证据 `implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md`。
> **2026-10-02 收尾批次**：修复 3 个孤儿测试（`tests/conversation/test_manager.py`，19→22 项）+ 去掉 `tests/foundation/test_voice_hud.py` 的 UTF-8 BOM + 补 `implementation_plan/checkpoints/P9-handoff.md`；全量基线 **2084 → 2087 tests OK (skipped=3)**、全仓 BOM 26 → 25（回到基线）、`compileall` exit 0。
> **2026-10-01 P9 收口**：五条退出条件逐条判定通过 + 全量基线 **2084 tests OK (skipped=3)** + 用户真机逐项确认；P9 全部改动已提交并 push（**最新 `a34af54`**，工作区干净）。
> **新对话请先读根目录 `NEXT-SESSION.md`**（续接摘要：现状、环境事实、待授权项、最小读取集合与可复制续接指令）。
> 详细实施步骤见 `implementation_plan/README.md`；新对话只读取当前阶段文件及其必要共享依赖。
> **2026-10-12 本轮**：① **P10 · P1 收尾批次**——把三处「总时长上限」改成**无进展看门狗**（`streaming_session` / `sequential_session` / `voice_entry` + `--speech-stall-timeout`），并新增 `speech_drain_seconds` / `speech_stall_timeouts` / `speech_last_progress` 真机事实；依据是豆包逆向结论（超时全为空闲式、TTS 一次请求整条流式、只有打断才允许丢尾）。交接 `implementation_plan/checkpoints/2026-10-12-P10-P1-drain-stall-handoff.md`、研究 `implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`。② **提示窗 UI 批次 A/B/C**——拖动跟手（旧实现被同 id `SetTimer` 重置）+ 松手不再跳位 + 贴边吸附；进场淡入+滑入 260 ms、退场 180 ms；**状态切换保持硬切**（真机反馈脉冲式过渡=闪烁，默认 `state_animation_ms=0` + 200 ms 防闪护栏）；尊重系统"动画效果"（`SPI_GETCLIENTAREAANIMATION=0x1042`）。交接 `implementation_plan/checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`。③ 全量基线 **2696 → 2747 tests OK (skipped=3)**，`compileall` exit 0；**真机长回复与 UI 观感待用户复验**。
> **2026-10-13 收口**：**P10 R4-B5 双讲检测（B1–B4）真机结论＝条件性 no-go**——本机单讲/双讲在四类可观察量上不可区分（对齐相关 0.022/0.005、参考 RMS 2121/2121、采集 RMS 895/3526、`erle_sufficient` 帧 0/329），生产检测器在真值单讲段判双讲 **268/268＝100% 误报**，APM `speech_probability` 也分不开 ⇒ 不是阈值问题是判据不带信息；`--double-talk-detect` **保持默认 off**，B1–B3 代码保留（有单测，作为有线性回声路径时的基础设施），复测前置＝**外接麦克风/耳机**。结论文档 `implementation_plan/research/2026-10-13-P10-R4B5-doubletalk-real-device-conclusion.md`。同批修掉 `--help` 在 GBK 控制台崩溃（argparse help 含 U+26A0/U+21D2 ⇒ `UnicodeEncodeError`）。**54 项未提交改动按 7 个提交落地**（`3bac6a4` → 文档同步，**未 push**）；全量基线 **2747 → 2842 tests OK (skipped=3)**（本轮实测 193.1 s、exit 0；`compileall` exit 0、`git diff --check` exit 0）。下一步：P1 长回复收尾——先做 `speech_drain_seconds=null` **只读诊断**，再跑 **≥800 字真机验收**。
> **2026-10-13 追加**：上述 P1 只读诊断已完成——真机 553 条 `voice_turn` 里带该键的最后 10 条**全为 None**，根因是 `AutoVoiceSession._take_speech_facts()` 的**白名单复制漏了** `drain_seconds`/`stall_timeouts`/`last_progress` 三键（进程时间线已排除陈旧进程）；已补三键 + 2 项回归（走真实映射路径），全量基线 **2842 → 2844 tests OK (skipped=3)**（209.7 s、exit 0）。真机复测需**重启**后跑 ≥800 字。**（2026-10-13 22:05 已跑并确认）**：805 字回复全播——`played==accepted==pushed=795`、段 **28/28/28**、`drain_seconds=30.19`、`stall_timeouts=0`、`finish_timed_out=false`、`truncation=null`、`pending_after_finish=0`，且**用户本人确认"全听到了"**（`user_heard=true`）⇒ **P1 ≥800 字真机验收通过**。**同日 UI A/B/C 复验通过**：`--voice --auto --tts-mode none` 零成本路径 **6/6**（进场淡入+右滑/拖动跟手/松手不二次跳位/贴边吸附/状态切换硬切/退场淡出+右滑）由用户本人肉眼确认；未验证＝含播报的真实路径硬切与系统"动画效果"关闭口径（无帧级客观证据）。
> **2026-10-13（追加）本轮开放任务**：**P10 · T1「任务执行接线」**——用户确认首批只做**打开白名单应用**（`file_operation`/`dangerous` 保持 fail closed）、确认词只认「确认」（超时即拒绝）、路由＝**本地规则优先 + 真实 Provider 兜底**。设计+竞品调研见 `implementation_plan/research/2026-10-13-task-exec-open-app-research.md`（真实数据：openinterpreter 68,566★ / microsoft-UFO 10,005★ / HA intents 629★ / hassil 115★ / ovos-core 296★；rhasspy 与 ovos-yes-no-plugin 未取到，如实标注）。**批次 1 已交付**：`app/runtime/orchestration/local_task_router.py`（确定性句式归一化 + `RouteHandoff(task/open_application)` + `TaskFirstRouter`）、`app/runtime/orchestration/app_launch.py`（`calc`/`notepad` 白名单 + 中英别名完全匹配 + `TaskActionProposal`）；**批次 2 已交付**：`task_dispatch.py`（预览→确认→授权→执行→汇报，确认词严格只认「确认」）、`task_runtime.py`（装配层：真实 SHA-256 白名单、方案 C 的 target_state 口径、每任务新建编排器）、`text_shell.run_conversation(task_dispatch=…)` 文本回路、`--no-task-exec`（默认开）；新增测试 **72 项**（路由 17 + 提议 20 + 派发 15 + 装配 15 + 文本壳 5），全量基线 **2844 → 2916 tests OK (skipped=3)**（174.8 s、exit 0）。**编排层禁用 `os`/`pathlib`** 的 P8 硬门已按规遵守（SystemRoot 由装配层传入）。**未做**：批次 3 语音确认轮、批次 4 真机验收（**"打开计算器"还没在真机上跑过**）。**真机首跑（2026-10-13）发现并修掉一个我的缺陷**：无 Provider 时 `select_runtime_router()` 返回 `(None, None)` ⇒ `TaskFirstRouter` 抛 `TypeError` 被兜底吞掉、还谎报"白名单或数据目录不可用"；已加 `compose_task_router()`（缺 fallback 用 `LocalChatRouter`）+ 失败原因按固定短码如实输出 + 3 项回归；全量 **2919 tests OK (skipped=3)**。**文本路径真机验收通过（2026-10-13，用户本人执行）**：`打开计算器` → 打印冻结计划（L2）→ 输入 `确认` → 真实启动 → 摘要「共 1 个动作，成功 1」，审计 4 条齐全（`operation_result.verification_status=completed`），用户确认**计算器窗口弹出**。下一步：批次 3（语音确认轮）已交付代码（`PendingConfirmationRouter` + 两步派发 API + `VoiceTaskTurn` + `auto_voice`/`main` 接线，新增测试 46 项，全量 **2967 tests OK (skipped=3)**），**待真机验收**（需先结束两个占着实例锁的僵尸 `python.exe`：PID 22504/19288）。
> 阶段收口规则：每阶段完成后必须先做覆盖该阶段及其前置阶段的集成验收，再生成 handoff 并进入下一阶段；P1-P4、P5、P6、P7、P8 已通过。
> **2026-10-14（P10 · T1 收尾）**：`codex/review-playback-lifecycle` **HEAD = `b9951e2`（已 push，远端一致）**，本轮 **6 个提交全部推送**（`4a5a04b` #12 / `3263717` #13 / `cd4852c` 诊断批 / `de9da17` 阶段耗时 / `ead0d69` 播报提速 D / `b9951e2` 文档同步）。全量 **3091 tests OK (skipped=3)**（164.3 s、exit 0）。真机结论：① **#13 静默窗默认关、已撤销"建议 800 ms"**（真机它吃掉"播报后紧接着说话"那一轮的用户首字 ≈0.68 s，且未观察到对应收益）；② **播报慢的真因＝TTS 播放 ≈3.1–3.6 字/秒**（ASR 125–520 ms、装配 0–94 ms、TTS 首字 0.1–0.3 s 都不是瓶颈）⇒ D 批次**已真机验证**（计划句播放 6.09–6.28 s → 3.67/3.75/3.61 s；改口不再连播"已取消"）；③ **20 s 待确认窗口到期作废真机通过**（`cancelled/timeout`）；④ 「打开记事本」"结果不确定"＝Win11 记事本是应用商店版启动器（动作成功、窗口弹出），已按口径 A 只改语音文案。详见 `implementation_plan/research/2026-10-14-P10-T1-task-exec-real-device-and-latency.md`。
> **2026-11-08（收口 10/24 未提交批次）**：**人设文件驱动**——`--persona` / `ASSISTANT_PERSONA_PATH` > 仓库默认 `implementation_plan/persona.md` > 内置兜底；显式指定的模板不可用 ⇒ 启动失败（退出码 2）、不静默换人设；启动打印来源，`config_checked` 带 `persona_source`/`persona_name`/`persona_version`；新增 `run-voice.ps1`（`-TaskExec`/`-Persona`）。**`text_only` 定性诊断**——`voice_turn` 事实补 `reply_chars` / `speech_skip_reason` / `route`（只含计数与固定短码）。全量 **3152 tests OK**（10/23 基线 3112，+40）；**真机 5 轮通过**：人设生效、任务确认执行成功（`task_ms=109`）、`unsupported` 如实拒绝，**`text_only` 实例＝ASR 出 1 字空句 + 路由 `ambiguous`（不是 Agent 失败）**。已提交并 push；详见 `progress.md` 2026-11-08 节。
> **2026-11-08（追加）1.4 语速吞尾根因结案**：**不是丢音频、也不是播放丢尾**（`ws_pcm` 帧完整 / 项目播放栈复播墙钟≈音频时长 / ASR 读到句尾），是**引擎 `speed>1` 压短末字释放 + 单次合成采样差异**；补尾静音无效；用户耳测 1.0/1.3 正常。**处置：默认语速保持 1.0，1.4 不采用**；研究文档 `implementation_plan/research/2026-11-08-P10-T1-tts-speed-tail-root-cause.md`。同日真机 1.0 复验：第 3/4 轮 `chat` 播放段 2/2、`finish_timed_out=false`、`pending_after_finish=0`。
> **2026-11-08（追加 2）「能不能退出程序」口径定案（口径 A）**：句首礼貌前缀不改变句子类型——有**祈使标记**（帮我/给我/请你…）= 请求 ⇒ 仍如实拒绝（#12 不变）；**只有疑问标记** = 询问 ⇒ 交聊天。实现含原文判定纯询问、祈使时纯疑问阻断词失效、句首填充词（那/嗯/就是…）本检测内剥离、`downgrade_question_unsupported` 同步、真机补单字「关」；真机 19:54 复验「能不能退出程序？」→`chat`、「帮我关闭计算器」→`unsupported`、任务链路正常；全量 **3155 tests OK**。

> **2026-11-08（追加 3）公开仓库策略落地**：私有主库 `origin` 日常提交；公开镜像 https://github.com/yyyydssj/computer-voice-assistant-public 为**单提交脱敏快照**（不带私有历史），发布脚本 `tools/publish_public.py`（自检无 .env/文档个人痕迹、中性作者、强推）。本轮 253 个文档脱敏 946 处（路径/用户名/微信路径），代码仅保留功能白名单 `<STUDY_ROOT>` 未动；旧快照库已转私有。全量测试不受影响。

## 各阶段

### 阶段 1：需求与发现
- [x] 理解用户意图（逐步讨论澄清）
- [x] 确定约束条件、需求、成功标准
- [x] 将发现记录到 implementation_plan/research/findings.md
- **状态：** complete

### 阶段 2：规划与结构
- [x] 确定技术方案（ASR / LLM / TTS / 控制 / 安全）
- [x] 确定架构与模块划分
- [x] 记录决策及理由（implementation_plan/design.md 已定稿）
- [x] 写出详细实施计划，并按阶段拆分到 `implementation_plan/`
- [x] 补充讨论核心细节决策（交互模式/命令执行/编码/安全措辞/记忆/日志）
- [x] 定稿 I.9 音频会话预热、运行期故障、恢复探测与重新启用规则
- [x] 完成 Skill、K–N 与 E/F/G 遗留细节的本轮规划收口；仍保留需用户/真机确认的参数与候选项
- [x] 按固定 commit/tag 完成 GitHub 成熟项目源码对比并优化正式方案
- [x] 编写分阶段、可独立接续的 P0-P11 代码实现规划文档
- [x] 完成本轮规划文档的 UTF-8、链接、标题、状态、锚点与关键硬规则一致性校验（111 项通过）
- [x] 整理执行文档目录：正式设计与执行资料集中到 `implementation_plan/`，历史入口归档到 `implementation_plan/archive/`，会话恢复文件保留根目录
- [x] 建立项目虚拟环境 `<PROJECT_ROOT>\.venv`（Python 3.12.10；未安装项目依赖）
- [x] 完成 P0 冻结清单、环境基线、Git 隐私基线和 handoff
- **状态：** complete

### 阶段 3：实现
- [x] P0：实现冻结与环境基线
- [x] P1：运行时外壳与最小界面
  - [x] P1 批次 1：配置加载与敏感配置检查
  - [x] P1 批次 2：单实例、实例标识与安全关闭
  - [x] P1 批次 3：结构化日志、脱敏与日志目录初始化
  - [x] P1 批次 4：最小文字输入/输出壳
  - [x] P1 批次 5：临时/计划/审计目录归属检查与未完成任务不续跑故障注入
- [x] P2：跨模块契约、事件和状态模型
  - [x] P2 批次 1：严格版本化 JSON 契约与 19 个核心对象
  - [x] P2 批次 2：授权快照、统一事件 envelope/闸门、四套正交状态机
  - [x] P2 批次 3：版本迁移、请求单次提交、账本中断/完成缺失、审计链断裂故障注入
  - [x] P2 批次 4：补齐边界验证、全量回归、P2 handoff 与 P3 停止点
- [x] P3：文字聊天与 Harness 会话
  - [x] P3 批次 1：DeepSeekHarness adapter、延迟 runtime、请求防重提、错误映射与 no-tools 安全补丁（官方 SDK/runtime `0.1.5rc1` 离线导入、构造参数、隔离初始化和 runtime 级 no-tools/无重试/无持久历史/无会话日志上传均已验证；8/8、80/80、compileall、UTF-8=42 通过）
  - [x] P3 批次 2：闲聊会话容器与工作会话占位、30 分钟闲聊闲置销毁、上下文隔离（会话专项 13/13、全量 93/93、compileall、UTF-8=46 通过）
  - [x] P3 批次 3：request_id 生成、流式文本拼接与完整性判定（会话专项 17/17、全量 97/97、compileall、UTF-8=46 通过）
  - [x] P3 批次 4：persona 读取、版本和用户修改边界（persona 专项 5/5、全量 102/102、compileall、UTF-8=50、真实 persona smoke 通过）
  - [x] P3 批次 5：全量错误映射、回归、handoff 与 P4 停止点（adapter 11/11、conversation 19/19、全量 107/107、compileall、UTF-8=50、官方异常映射 smoke 通过）
- [x] P4：结构化工具、路径安全、策略和 ActionPlan
  - [x] P1-P4 跨阶段集成验收：2/2；P1-P4 回归 185/185；全量 260/260；记录：`implementation_plan/checkpoints/2026-09-21-P1-P4-integration-acceptance.md`
  - [x] P4 批次 1：13 个已批准 Tool Manifest、稳定内容哈希、不可变 ToolRegistry、清单外/L4/命令面拒绝（专项 10/10、全量 117/117、compileall、UTF-8=55）
  - [x] P4 批次 2：只读工具 `read_text_file`、`list_directory`、`search_files`、`get_processes` 的结构化实现与故障注入（专项 10/10、全量 127/127、compileall、UTF-8=57）
  - [x] P4 批次 3：7 个受控副作用工具的纯计划生成/预览，严格参数与路径校验注入、内容哈希、冲突/扩展名/回收站/应用白名单约束，绝不执行（专项 10/10、全量 137/137、compileall、UTF-8=59）
  - [x] P4 批次 4：PathGuard 路径规范化、真实路径/Reparse/UNC/映射盘拒绝和执行前快照复检（专项 11/11、全量 148/148、compileall、UTF-8=63）
  - [x] P4 批次 5：PolicyEngine 可信结构化输入、拒绝优先策略及稳定 `allow/deny/require_confirmation` 决策（专项 11/11、全量 159/159、compileall、UTF-8=65）
  - [x] P4 批次 6：不可执行 ActionPlan、`plan.json` 规范 JSON、稳定哈希与严格解析，以及只允许隔离备注的 `execution_plan.md`（专项 11/11、全量 170/170、compileall、UTF-8=69）
  - [x] P4 批次 7：不可执行 `result.json` 版本、稳定哈希、严格解析、计划/动作身份绑定与 P4 收口（专项 12/12、P4 plans 专项 23/23、全量 182/182、compileall、UTF-8=71）
- [x] P5：授权、执行、验证、审计与恢复
  - [x] P5 批次 1：L2 模态确认状态与 `AuthorizationSnapshot` 会话精确绑定（专项 11/11、契约+L2 23/23、相关回归 45/45、全量 193/193、compileall、UTF-8=75）
  - [x] P5 批次 2：L3 独立确认、固定短语、一次性消费和防重放（全量 202/202、compileall、UTF-8=78）
  - [x] P5 批次 3：执行前重新校验与原子授权消费（preflight 13/13、聚焦 88/88、全量 215/215、compileall、UTF-8=80）
  - [x] P5 批次 4：Execution Ledger 身份、attempt 与 fencing token（ledger 21/21、聚焦 62/62、全量 236/236、compileall、UTF-8=83）
  - [x] P5 批次 5：原子动作边界、协作取消和不可安全中断声明（取消 10/10、ledger 21/21、preflight 13/13、security 72/72、全量 246/246、compileall、UTF-8=84）
  - [x] P5 批次 6：备份、固定执行后端、ResultVerifier、部分成功和 `state_uncertain`（执行 5/5、security 75/75、全量 258/258、compileall、UTF-8=90）
  - [x] P5 批次 7：审计哈希链、脱敏、恢复扫描与恢复报告（专项 15/15；全量 263/263；compileall；UTF-8=98；执行面 AST 审计为空；交接：implementation_plan/checkpoints/2026-09-21-P5-batch7-audit-recovery-stop-handoff.md）
  - [x] P5 批次 8：分层重试策略（专项 9/9；全量 272/272；compileall；UTF-8=102、既有 BOM 2 个；retry 执行面 AST 审计为空；交接：implementation_plan/checkpoints/2026-09-21-P5-batch8-layered-retry-stop-handoff.md）
- [ ] 按路线图逐阶段执行，先测试与故障注入，再做最小实现
- [x] P1 增量测试通过并生成 `implementation_plan/checkpoints/P1-handoff.md`
- [x] P2 全量测试通过并生成 `implementation_plan/checkpoints/P2-handoff.md`
- **状态：** 已完成（P0–P9 全部完成并通过阶段级验收；P9 于 2026-10-01 通过，2026-10-02 完成 P0–P9 整体验证，证据 `implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md`）

### 阶段 4：测试与验证
- [x] 验证所有需求已满足（P0–P9：五条 P9 退出条件代码侧取证 + 各阶段退出条件逐条有离线证据；2026-10-02 整体验收）
- [x] 将测试结果记录到 progress.md（2026-10-02 记录：全量 2084 tests OK (skipped=3)、专项 56/298/113/173/176、compileall exit 0）
- [x] 修复发现的问题（P0–P9 范围内无阻断缺陷；新登记 2 项非阻断项：`tests/conversation/test_manager.py` 3 个孤儿测试、`tests/foundation/test_voice_hud.py` 带 BOM——处置需另行授权）
- **状态：** 已完成（2026-10-02）

### 阶段 5：交付
- [x] 检查所有输出文件（21 项 P9 交付物全部存在 + P0–P8 抽样齐全；artifact 22 子目录 / 128 文件）
- [x] 确保交付物完整（3 处受限项已如实登记：缺 `P9-handoff.md`、原始录音不入库、`models/`+`data/` 本机私有不可从仓库重建）
- [x] 交付给用户（`implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md` + 状态文档同步）
- **状态：** 已完成（2026-10-02）

## 关键问题（已讨论解决）
| 问题 | 结论 |
|------|------|
| 控制电脑范围 | DeepSeekHarness 负责运行时理解与规划；本地受控执行器负责白名单工具；第一阶段不开放任意命令或脚本 |
| 交互形态 | 实时连续语音对话：可闲聊，也可自然提出电脑操作需求；安全层决定任务操作权限 |
| 人物模板最高原则 | AI 面向用户的自然语言语音必须遵循用户确认的人物模板；闲聊优先保证拟人、口语化和连续人格，任务与安全提示也保持人物口吻但不得弱化准确性或授权边界 |
| 运行时主 Agent | DeepSeekHarness（Python SDK 正式接入）；启动仅初始化适配器，首次请求延迟建会话；闲聊与工作会话隔离；命令行仅诊断/测试 |
| 语音识别 | 本地 sherpa-onnx + SenseVoice（中文强，多语种+事件检测） |
| 语音合成 | VoiceStudio 本地 TTS 为主 Provider；默认提示词设计音色，保留用户手动创建/选择克隆音色；Edge TTS 为可配置兜底 |
| 运行形态 | 后台常驻 + 唤醒词「小饼」 |
| 安全边界 | 读按限制直接执行；写/删/改须先查看方案并按 L2/L3 授权规则确认 |
| 技术栈 | Python |

## 已做决策
| 决策 | 理由 |
|------|------|
| 项目目录 `<PROJECT_ROOT>` | 与现有杂乱目录分离，独立管理 |
| 规划三件套 + implementation_plan/design.md | 跨会话可恢复，方案固化可 review |
| 单进程模块化架构 + Function Calling | 清晰易调试，扩展只需加工具 |
| Agent 启动与会话生命周期 | 启动不创建会话；首次请求延迟创建；闲聊闲置满 30 分钟销毁且不落盘；工作会话按 `task_id` 隔离，完成/取消/失败后关闭 | 避免无请求联网、上下文串用和授权跨任务复用 |
| 加日志（logs/） | 出错可追溯 |
| MVP 工具边界 | 首版注册 13 个结构化工具；通用 PowerShell 和终止进程暂缓 | 降低执行面，先验证安全闭环 |
| 工具权限等级 | L0/L1 只读受限直执行；L2 居中模态弹窗授权；L3 点击“允许执行”后还必须输入或说出完整固定短语“确认执行”；L4 默认拒绝；Agent 不得绕过本地策略 | 让风险控制由本地策略强制执行 |
| MVP 执行限制 | 默认不覆盖、不永久删除、不接受任意参数或脚本；批量操作先展开清单 | 防止模糊指令和模型输出扩大影响范围 |
| MVP 路径边界 | 文件工具允许访问根目录为 `<STUDY_ROOT>`；`<STUDY_ROOT>\AI工作区` 仅为默认工作目录 | 满足学习目录整体使用需求，同时保留明确边界 |
| 修改前执行方案 | 创建、写入、复制、移动、重命名和回收站删除前，必须先生成具体执行方案并经用户确认；变化后重新确认 | 防止未经授权扩大范围或操作对象 |
| 路径安全 | 规范化绝对路径，拒绝越界、UNC/网络路径、映射盘和无法确认的链接/junction/reparse point | 防止路径绕过和真实目标漂移 |
| 开源版本优先 | 后续讨论默认采用配置化、可脱敏、可公开的设计；个人路径、API、设备、模型、日志、记忆和声纹不写死 | 未来开源时避免泄露个人环境和敏感数据 |
| 个人内容分类 | 使用 `[通用规则]`、`[用户配置]`、`[本机配置示例]`、`[敏感信息]`、`[本地运行数据]`、`[示例值]` 标记方案内容 | 便于代码、文档和仓库发布前统一脱敏 |
| 弹窗与通知布局 | 权限确认采用居中模态弹窗；任务结果采用右下角非模态通知 | 明确授权与结果反馈的交互边界，避免误操作 |
| 权限按钮文案 | 固定使用“允许执行”和“拒绝”，不使用“确定”和“取消” | 让授权含义明确、可审计 |
| 弹窗主题 | 默认跟随系统；浅色、深色、高对比度和强调色作为用户配置 | 保持系统一致性并适配开源用户环境 |
| 弹窗与语音优先级 | 弹窗授权优先于语音；弹窗出现时语音只作提醒，点击任意按钮立即停止当前播报 | 防止语音延迟或重复造成误授权 |
| 任务结果通知 | 右下角通知默认停留 3 秒，严重失败和系统拒绝也遵守；用户可以手动关闭 | 兼顾及时反馈与非阻塞体验 |
| 表达风格分工 | 语音使用人物模板的自然口语；弹窗、执行方案、执行文档和日志使用正式语气 | 兼顾人物体验、风险清晰度和审计要求 |
| 模式边界与决策权 | 闲聊模式可以自然对话但不产生授权；工作模式中助手只提供分析、方案、预检和汇报，所有解决方案与修改操作均由用户决定并完成授权 | 保留自然交流体验，同时防止建议被擅自执行 |
| 文件异常策略 | 预检必须排除可验证的已知错误；任一执行中失败、前提变化或状态不确定即终止并作废计划，不重试、不跳过继续、不自动修复 | 防止带错执行、部分失败扩大和助手越权处置 |
| 失败后的重新规划 | 失败弹窗只提供详情、重新扫描制定新计划和关闭；新计划必须重新预检、展示差异并重新授权 | 使恢复依据当前真实状态且全程可审计 |
| 闲聊与音频保留 | 纯闲聊仅在内存保留 30 分钟；闲聊文本与完整模型请求/响应默认不落盘；原始音频不持久化，临时音频最长 60 秒 | 给自然交流保留连续性，同时提供明确的隐私边界 |
| 审计保留与失败行为 | 所有工作流工具调用记审计摘要并关联计划；审计、计划版本和结果保留 180 个自然日；L2/L3 审计不可写即拒绝执行 | 确保可追溯性，不允许无记录的修改操作 |
| 数据目录与日志格式 | 运行数据存于用户可见、可配置且受保护的 data_root；本机示例为 `<STUDY_ROOT>\AI工作区\voice_assistant_data`；每日 JSONL 是可读事实来源，SQLite 仅为可重建索引 | 用户可以自行定位、查看和检索执行历史，不让运行数据污染源码仓库 |
| 审计完整性 | JSONL 事件使用前序哈希链；断裂时标记 `audit_integrity_broken` 并提示历史不完整 | 发现意外删改或截断，不虚构完整审计结论 |
| 用户层与 AI 诊断层 | 用户主要看 `execution_plan.md`、`result.md`、弹窗和语音摘要；AI 使用 `plan.json`、`result.json`、JSONL 与 SQLite 精确校验、检索和诊断 | 保证用户易读，同时保留机器快速定位错误所需的完整事实 |
| 结果文件 | 每个冻结版本同时保存 `execution_plan.md`、`plan.json`、`result.md`、`result.json`；`result.md` 由结构化结果生成，失败版本不可覆盖 | 让用户看到清晰总结，避免模型自由改写执行事实 |
| L3 第二重确认 | 用户点击“允许执行”后，必须在独立确认状态输入或说出完整固定短语“确认执行”；短语与当前计划、版本、哈希、动作和有效期动态绑定 | 避免复杂口令，同时防止闲聊肯定词或其他计划误触发高风险执行 |
| 崩溃与重启恢复 | 不自动续跑或复用旧计划/授权；依据当前真实状态将动作分类为已完成、未完成、状态不确定；用户决定继续后重新规划、预检和授权 | 防止中断后的重复执行、遗漏执行和错误状态推断 |
| 用户主动“停” | 聊天时停止回复并继续监听；授权时取消确认；执行时阻止后续动作、安全可取消则请求取消、不可中断则等待原子动作返回；之后重新扫描，继续任务须重新规划和授权 | 既保证实时对话不中断，也避免强杀动作或继续执行旧计划 |
| 取消、中止与关闭程序 | 用户取消记为 `user_cancelled`；系统安全中止记为 `system_aborted`；关闭程序按无任务、规划/授权、执行中三种状态安全退出；外部强制终止按崩溃处理 | 明确任务结束原因，防止取消被误认为关闭程序或中止后自动续跑 |
| Agent/API 故障恢复 | 闲聊故障丢弃不完整回复并等待用户决定是否用新请求重试；规划/授权故障记为 `system_aborted` 并作废计划授权；冻结本地执行仅在控制与验证完全本地可用时继续；汇报失败不改变结构化结果；不自动重试或切换模型/通道 | 防止部分模型输出进入执行链、避免重复执行，并保持真实执行结果与自然语言汇报故障相互独立 |
| ASR 故障文字模式 | ASR 整体不可用时自动进入文字输入模式，不自动切换未经配置的识别服务；文字输入仍调用同一个 Harness Agent，并完整经过本地安全链；L2/L3 授权不降级 | 保持助手在语音输入故障时可用，同时防止输入通道变化绕过 Agent 边界或电脑操作授权 |
| ASR 恢复切回 | 后台低频本地检查可发现恢复，但不使用用户话语测试、不上传音频且不自动切回；只有用户明确请求后，才在安全空闲点清理旧状态、建立新输入边界并通过本地联合测试后切回 | 防止交互模式在键入、授权或执行中途突变，同时确保恢复后的语音链路真实可用 |
| TTS 故障衔接 | VoiceStudio 故障不影响文字或任务；按完整句边界切换到用户预先指定的 Edge 音色，半句失败则停止本轮且不混声；双 Provider 故障转纯文字；恢复只从下一轮生效 | 避免半句换声、旧内容重播和陌生音色自动启用，同时确保授权与安全状态不依赖语音输出 |
| 启动阶段模型故障 | ASR/TTS 独立、有上限地启动；ASR 失败立即进入文字输入，TTS 失败保留文字输出并仅按已配置 Edge 兜底；不阻塞界面、安全链或文字任务，不循环重启 | 让语音组件故障不拖垮持续对话和安全执行，同时避免无限等待、自动续跑旧任务和状态突变 |
| 音频会话级故障与恢复 | ASR/TTS 会话预热独立、有上限且不阻塞文字；运行期故障仅影响当前输入或语音输出；旧音频、旧请求与旧授权不得复用；恢复经冷却和固定材料探测后，ASR 等用户明确切回，TTS 只从下一轮新回复启用 | 将音频通道状态与电脑任务事实彻底隔离，避免半句、迟到事件、自动补播或自动切回改变请求、授权和执行状态 |
| Skill 完整候选（已完成源码对比优化，仍待用户进一步确认） | Skill 仅复用声明式、参数化流程知识；采用 manifest、版本、内容哈希、允许工具白名单和风险上限；每次调用重新扫描、规划、校验、授权和验证；批准版本不可原地修改；外部导入一律作为不可信草稿 | 防止 Skill 复用电脑状态、执行权限或用户授权；源码对比已完成，但本项仍未作为用户单独确认的最终结论 |

## 遇到的错误
| 错误 | 尝试次数 | 解决方案 |
|------|---------|---------|
| P5 批次 1 首次读取 `careful` 技能时路径猜错 | 1 | 改用实际路径 `gstack/careful/SKILL.md`；只读失败，无项目改动 |
| P5 批次 1 首次读取规划契约测试时文件名猜错 | 1 | 改读实际文件 `tests/contracts/test_models.py`；只读失败，无项目改动 |
| P5 批次 1 首次更新计划时使用了不精确锚点 | 1 | 防误写检查在写入前终止；定位实际唯一锚点后再更新 |
| P5 批次 1 首次写项目计划被沙箱拒绝 | 1 | 项目不在默认可写根；改用用户已授权范围内的受控提升权限 |
| 保存接续摘要时 `New-Item -LiteralPath` 不受当前 PowerShell 支持 | 1 | 改用兼容的 `New-Item -Path`，随后保存成功 |
| 接续读取时误将 `task_plan.md` 定位到 `implementation_plan/` 内 | 1 | 按摘要中的 `task_plan.md` 改读项目根目录文件 |
| 首次生成同步脚本时 PowerShell here-string 引号冲突 | 1 | 改用独立脚本，先完成全部替换校验再统一写入 |
| 当前机器的 `python` 命令不可用，`py` 启动器也未找到已安装解释器 | 1 | 改用现有 Node.js 运行文档迁移脚本 |
| 首次写入项目目录时受到工作区沙箱限制（`EPERM`） | 1 | 经用户批准后使用提升权限完成 8 份规划文档同步 |
| 使用 `Select-String -LiteralPath` 时错误传入通配符路径 | 1 | 先用 `Get-ChildItem` 展开文件列表，再将实际路径传给 `Select-String` |
| 文档同步脚本把 Markdown 反引号直接放入 JavaScript 模板字符串，导致语法错误 | 1 | 模板字符串中的 Markdown 反引号必须先转义；写入前先运行 `node --check`，再执行零写入校验 |
| 修补同步脚本时让 PowerShell 解析包含反引号的匹配文本，导致命令在解析阶段失败 | 1 | 不再用 PowerShell 拼接或替换含 Markdown 反引号的大段文本，改用 JSON 转义后的 Node 字符串 |
| `agent-browser` 未安装，无法按浏览技能直接执行 GitHub 调研 | 1 | 不重复调用不存在的命令；改用 GitHub 仓库的固定 commit、Git 浅克隆和单文件只读下载核实源码 |
| Windows `curl` 使用 Schannel 时因当前沙箱账户无可用凭据而失败 | 1 | 不重复原命令；经批准改用 Git 访问，并在单文件下载时使用 `--ssl-no-revoke` |
| Pipecat sparse clone 在 promisor remote 拉取 blob 时遇到 HTTP 502 | 1 | 保留已核实的 commit，改为按该 commit 只读下载目标源码文件，避免再次拉取整个仓库 |
| 2026-09-19 批量同步脚本误把文件末尾章节当作存在结束锚点的中间章节，预校验失败 | 1 | 确认零写入后改为从“下一步顺序”标题直接替换到文件末尾，重新执行 Node 语法检查和全量锚点校验 |
| 2026-09-19 清理命令再次在 PowerShell 中拼接含 Markdown 反引号的文本，解析阶段失败 | 1 | 停止用 PowerShell 拼接复杂 Markdown，改用独立 Node 脚本写入并先执行语法检查 |
| 2026-09-19 最终汇总验证命令在 PowerShell 中把循环结果直接接管道，解析阶段报空管道元素 | 1 | 不再用临时 PowerShell 组合式验证，改为单一 Node 脚本逐文件校验并以退出码汇总 |
| 2026-09-19 首次一致性校验使用了过度依赖同一行措辞的正则，产生单项误报 | 1 | 保持零写入，改为分别校验语义关键字和实际相对链接目标，不降低任何安全规则 |
| 2026-09-19 一次性超长批量 PowerShell 命令在进程创建前失败，报 `Failed to create unified exec process: 拒绝访问。 (os error 5)` | 1 | 确认脚本未执行且零写入，改为小批次命令 |
| 2026-09-19 批次 4 首次脚本将辅助函数命名为 `R`，与 PowerShell `Invoke-History` 别名冲突 | 1 | 确认读取前失败且零写入，更换辅助函数名称 |
| 2026-09-19 批次 4 第二次用双引号多行字符串匹配含 Markdown 反引号的文本，导致锚点为 0 | 1 | 确认写入前失败且零写入，改为逐行唯一匹配 |
| 2026-09-19 P0 收口写入前检测到 handoff 目标不存在后才创建，避免覆盖未知内容 | 1 | 先检查目标存在性，再以严格 UTF-8 创建新文件 |
| 2026-09-19 P1 敏感扫描把测试哨兵值识别为疑似密钥 | 2 | 将测试值改为普通 marker，并把变量名改为运行时拼接；生产逻辑未改变 |
| 2026-09-19 P1 多行 PowerShell 精确锚点与实际换行/缩进不一致 | 1 | 确认零写入，读取实际行后改用逐行唯一锚点替换 |
| 2026-09-19 P2 批次 4 最小修复脚本在第二个锚点失败前已写入第一个文件 | 1 | 立即检查实际文件状态并只补缺失编辑；后续多文件写入必须先在内存中验证全部锚点，再统一落盘 |
| 2026-09-19 P2 授权构造器首次误用只校验不返回值的时间辅助函数 | 1 | 代码复核发现后改为显式解析时间，并补充反序列化红测与 72/72 全量回归 |


## 后续文档操作纪律

为避免 2026-09-18 接续与批量同步时出现的操作错误，后续必须按以下顺序执行：

1. **先解析并验证路径**：相对路径必须以引用它的文件所在目录为基准解析；读取前使用 `Test-Path -LiteralPath` 或等价方法确认存在，不凭目录结构猜测。
2. **统一 UTF-8**：读取和写入 Markdown 时显式指定 UTF-8；PowerShell 输出中文前设置 UTF-8，乱码时先判断显示编码，不把显示问题误判为文件损坏。
3. **先侦察文档结构**：提取章节前先列出实际 Markdown 标题及行号，不预设标题层级或编号格式。
4. **先检查可用运行时**：生成迁移脚本前先用 `Get-Command` 和最小版本命令验证 PowerShell、Node.js、Python 等运行时；未验证的运行时不得作为执行依赖。
5. **先检查写入权限**：在修改前核对目标目录是否属于当前可写范围；需要提升权限时先一次性说明准确文件范围并请求批准，避免先执行再遇到 `EPERM`。
6. **复杂文本不用嵌套 PowerShell 引号**：包含大量 Markdown 反引号、多行文本或中文引号时，优先使用已验证的 Node.js 脚本，避免多层 here-string。
7. **批量修改必须先全量校验**：先在内存中验证所有文件、替换锚点和输出内容；任一校验失败则零写入。写入后检查关键新内容、过期内容计数、UTF-8 编码和文件完整性。
8. **不假定存在 Git**：执行 Git 命令前先确认项目根目录存在 `.git`；非 Git 项目不运行 `git status`。
9. **临时脚本及时清理**：任务完成并核验后删除工作区中的一次性迁移脚本，不把辅助文件遗留到项目中。

## 备注
- 随着进度更新阶段状态：pending → in_progress → complete
- 做重大决策前重新读取此计划（注意力操纵）
- 记录所有错误，避免重复
- **P0、P1、P2、P3 已完成；P4 批次 1～3 已完成，当前停在 P4 批次 4 门前**
- 音频链路、崩溃/重启、“停”、取消、中止、关闭程序、Agent/API 故障、ASR/TTS 启动与会话级故障恢复规则均已定稿。GitHub 固定版本源码对比与 P0-P11 阶段路线图已经完成；Skill 已完成源码对比优化但仍是候选。P3 已完成全部文字会话边界；P4 已完成声明式工具清单、四个只读工具和七个副作用工具的不可执行计划预览；尚未实现 PathGuard、PolicyEngine、ActionPlan 文件或任何副作用执行。

### P3 批次 1 完成停止点（2026-09-19）

- 状态：`complete`；当前停在 P3 批次 2 授权门前。
- 官方 SDK/runtime 离线导入、构造参数、隔离初始化均通过；未修改 adapter 或测试代码。
- runtime 级事实：模型请求无 `tools` 字段；429 探针只请求一次；成功轮次后无 profile 外会话或日志文件，也无额外上传请求。
- 新鲜回归：adapter 8/8、全量 unittest 80/80、compileall、严格 UTF-8=42 全部通过。
- 下一步：只有用户明确授权后，才可进入 P3 批次 2，实现闲聊/工作会话容器、上下文隔离和 30 分钟闲置销毁。
- 禁止：未经授权进入 P3 批次 2、接入主界面、真实 API 请求、工具、ActionPlan、ASR、TTS 或 P4。
- 交接：`implementation_plan/checkpoints/2026-09-19-P3-batch1-runtime-download-stop-handoff.md`。
### P3 批次 2 完成停止点（2026-09-19）

- 状态：`complete`；当前停在 P3 批次 3 授权门前。
- 新增 `app/conversation/`：一个闲聊会话、按 `task_id` 索引的工作会话占位，以及显式生命周期状态和关闭原因。
- 本地容器创建不触发 Harness runtime；第一次真实发送仍通过批次 1 adapter 延迟创建 runtime。
- 闲聊与工作会话使用不同 `session_id`；不同工作任务互相隔离，相同开放 `task_id` 复用同一占位。
- 闲聊连续闲置未满 30 分钟保持原会话；达到 30 分钟时显式或下次访问自动销毁，并使用新 `session_id`，关闭原因为 `idle_timeout`。
- 工作会话在完成、取消、中止或 Agent 失败后关闭，分别记录 `completed`、`cancelled`、`aborted`、`agent_failure`，关闭后拒绝继续发送或复用该 `task_id`。
- 不自动重试、不生成新 `request_id`、不切换 Agent；没有读取 `.env`、启动官方 runtime 或发送真实 API 请求。
- 新鲜回归：会话专项 13/13、全量 unittest 93/93、compileall、严格 UTF-8=46 全部通过。
- 下一步：只有用户明确授权后，才可进入 P3 批次 3，实现 `request_id` 生成、流式文本拼接与完整性判定。
- 禁止：未经授权进入批次 3、修改 persona、接入主界面、工具、ActionPlan、ASR、TTS 或 P4。
- 交接：`implementation_plan/checkpoints/2026-09-19-P3-batch2-conversation-stop-handoff.md`。

### P3 批次 3、4 完成停止点（2026-09-19）

- 批次 3 已完成：会话层缺省生成唯一 `request_id`，支持流式文本顺序拼接；只有非空文本且存在有效 `finish_reason` 的完整结果进入正式历史。
- 批次 3 新鲜回归：会话专项 17/17、全量 unittest 97/97、compileall、严格 UTF-8=46 全部通过。
- 批次 4 已完成：新增只读 `PersonaSource` 和不可变 `PersonaSnapshot`；严格读取 UTF-8 `persona.md` 全文作为 system prompt，并以原始字节 SHA-256 形成版本标识。
- 用户修改边界：程序不提供保存、更新或 Agent 运行时改写接口；已加载快照不随磁盘变化，只有显式重新加载才采纳用户修改并生成新版本。
- 拒绝边界：相对路径、UNC/网络路径、缺失文件、目录、空文件、超限文件、非法 UTF-8、畸形或重复 front matter、空正文均拒绝。
- 批次 4 新鲜回归：persona 专项 5/5、全量 unittest 102/102、compileall、严格 UTF-8=50、真实权威 `implementation_plan/persona.md` smoke 全部通过。
- 安全事实：未修改权威 persona 文档，未读取 `.env`，未启动官方 Harness runtime，未发送真实 API 请求，未接入主界面、工具、ActionPlan、ASR、TTS 或 P4。
- 当前停止点：P3 仍为 `in_progress`；停在批次 5 授权门前。批次 5 只允许补齐错误映射、全量回归、P3 handoff 与 P4 停止点。
- 交接：`implementation_plan/checkpoints/2026-09-19-P3-batch4-persona-stop-handoff.md`。
### P3 批次 5与阶段完成停止点（2026-09-19）

- P3 状态：`complete`；当前停在 P4 授权门前。
- 初始化与请求阶段的连接、限流、超时、流中断、空响应、未知事件和 Schema 错误均映射为结构化故障；配置硬错误继续抛出。
- runtime 结果结构和 conversation outcome 身份/状态组合采用拒绝优先校验；不完整或故障输出不进入正式历史。
- Agent adapter 11/11、Conversation 19/19、全量 unittest 107/107、compileall、严格 UTF-8=50、官方 SDK 异常映射 smoke 全部通过。
- 未读取 `.env`、未启动官方 runtime、未发送真实 API 请求；未接入工具、ActionPlan、ASR、TTS、语音链路或 P4。
- 下一步只有在用户明确授权 P4 后，才能建立 ToolRegistry、PathGuard、PolicyEngine 和可审查 ActionPlan；不得提前实现执行器或授权。
- 交接：`implementation_plan/checkpoints/P3-handoff.md`。

### P4 批次 1 完成停止点（2026-09-20）

- 用户明确要求严格接续 `P3-handoff.md` 并直接开始下一批次，无须再次授权；本轮仅进入 P4 批次 1。
- 新增 `app/tools/manifest.py`、`app/tools/catalog.py` 和公开入口：声明 13 个已定稿 MVP 工具，包含输入/输出 Schema、风险等级、确认要求、路径范围、超时、输出上限、幂等性、取消能力、预览、验证、撤销、审计类型、限制和稳定 SHA-256 内容哈希。
- `ToolRegistry` 构造后不可替换内部数据，只接受 13 个已批准名称；清单外工具、重复工具、L4、开放 Schema、未知字段、伪造哈希、任意命令/PowerShell/Shell 和进程终止入口均拒绝。
- 当前 Manifest 没有 `execute` 或任何系统调用；没有实现只读工具、写工具、PathGuard、PolicyEngine、ActionPlan、授权或执行器。
- 新鲜回归：Manifest 专项 10/10、全量 unittest 117/117、compileall、严格 UTF-8=55、13 个唯一内容哈希与无执行入口 smoke 全部通过。
- 当前停止点：P4 `in_progress`；停在批次 2 门前。下一批次只实现四个只读工具的结构化结果与限制，不得提前实现受控写计划、PathGuard、PolicyEngine、授权或执行器。
- 交接：`implementation_plan/checkpoints/2026-09-20-P4-batch1-tool-manifest-stop-handoff.md`。
## P4 批次 4 完成停止点（2026-09-20）

- [x] 严格接续批次 3 交接，只实现 PathGuard，不进入 PolicyEngine、ActionPlan、授权或执行器。
- [x] 新增 `app/safety/path_guard.py` 与公开入口，固定允许根 `<STUDY_ROOT>`、默认工作目录 `<STUDY_ROOT>\AI工作区`。
- [x] 规范绝对/相对路径、环境变量、大小写、斜杠和 `..`；拒绝 UNC/设备路径、其他盘符、盘符相对、根相对、ADS、8.3 短名和尾随点/空格歧义。
- [x] 逐级检查普通组件，拒绝符号链接、Junction、Reparse Point、映射/网络盘和无法可靠解析的路径。
- [x] 生成不可变 `PathSnapshot`，提供 `revalidate()` 作为未来执行前复检接口，覆盖链接替换、目标出现/消失、身份变化、父路径竞态和权限丢失。
- [x] PathGuard 可直接作为现有只读工具和写动作预览的 `path_validator`；没有文件写入、移动、删除或应用启动入口。
- [x] 专项 11/11、全量 unittest 148/148、compileall、严格 UTF-8=63、真实 Windows 路径及工具集成 smoke、AST 无执行面审计全部通过。
- [x] 后续批次 5 已完成 PolicyEngine；批次 6 已完成不可执行 ActionPlan 与稳定文本编解码。

最新交接：`implementation_plan/checkpoints/2026-09-20-P4-batch6-action-plan-stop-handoff.md`。

## P4 批次 5 完成停止点（2026-09-20）

- [x] PolicyEngine 只接受可信 `PolicyInput`，复核 ToolRegistry、Manifest、PathSnapshot 和 PathGuard 事实，拒绝自然语言及策略字段注入。
- [x] L0/L1 固定 `allow`，L2/L3 固定 `require_confirmation`；风险、确认、版本、哈希、路径或复检漂移一律拒绝。
- [x] 专项 11/11、全量 unittest 159/159、compileall、严格 UTF-8=65、AST 无执行面审计全部通过。
- [x] 未实现授权、执行器、ActionPlan 文件、结果文件或真实电脑副作用。

交接：`implementation_plan/checkpoints/2026-09-20-P4-batch5-policy-engine-stop-handoff.md`。

## P4 批次 6 完成停止点（2026-09-20）

- [x] 新增不可执行、不可变的 `ReviewableActionPlan` / `PlanAction`，在内存中构造和解析 `plan.json` 与 `execution_plan.md` 字符串。
- [x] `plan_hash` 覆盖版本、身份、动作顺序、Manifest、参数、路径快照、Policy、风险、预览、验证与回滚事实；规范 JSON 稳定且拒绝未知/缺失/重复字段与非标准数字。
- [x] 即使重新计算哈希，解析仍复核本地 ToolRegistry、Policy 与路径事实；动作重排、重复 ID 和事实漂移均拒绝。
- [x] Markdown 除隔离的用户备注区外必须与规范渲染逐字一致；备注不进入哈希、不授权，并拒绝脚本围栏和明显命令载荷。
- [x] 专项 11/11、全量 unittest 170/170、compileall、严格 UTF-8=69、AST 文件/进程副作用审计全部通过。
- [x] 未写入真实计划文件，未实现 `result.json`、授权、授权消费、执行器、审计、恢复、ASR、TTS 或真实电脑副作用。
- [x] 后续批次 7 已完成 `result.json` 稳定契约与 P4 收口；当前停在 P5 授权门前。

最新交接：`implementation_plan/checkpoints/2026-09-20-P4-batch6-action-plan-stop-handoff.md`。
## P4 批次 7 与阶段完成停止点（2026-09-20）

- [x] 新增不可执行、不可变的 `ResultAction` / `ResultDocument`，仅在内存中构造、规范序列化和严格解析 `result.json` 字符串。
- [x] `result_hash` 覆盖 schema/format、结果/执行/计划身份、时间、派生总状态，以及每个动作的顺序、工具身份、返回、验证、输出、错误和跳过事实。
- [x] 解析拒绝未知/缺失字段、重复 JSON key、非标准数字、哈希篡改、动作重排、重复 ID、计划身份漂移、动作身份漂移、总状态伪造和倒序时间。
- [x] 结果语义固定区分 `succeeded`、`failed`、`skipped`、`state_uncertain`；`state_uncertain` 优先传播，结果文件不能生成授权或执行动作。
- [x] 验证：result 专项 12/12、ActionPlan 专项 11/11、全量 unittest 182/182、compileall、严格 UTF-8=71、批次文件/进程副作用 AST 审计为空。
- [x] P4 退出条件全部通过；P4 状态更新为 complete，并生成 `implementation_plan/checkpoints/P4-handoff.md`。
- [x] 用户已于 2026-09-20 明确授权直接进入 P5 下一批次。当前只执行批次 1，不越过到 L3、执行器或语音。

最新交接：`implementation_plan/checkpoints/P4-handoff.md`。

## P5 批次 1 完成与停止点（2026-09-20）

- [x] 读取 P4 handoff、P5 路线图、共享安全/恢复/ActionPlan 约束、P2 契约与现有测试。
- [x] 固定范围：复用唯一 P2 `AuthorizationSnapshot`；仅实现 L2 模态确认状态，不实现 L3、授权消费或执行。
- [x] 先写 `tests/security/test_l2_confirmation.py` 红测，覆盖允许、拒绝、关闭/Esc、超时、停止、计划漂移、旧窗口、ID 重用和混合风险拒绝。
- [x] 实现 `app/safety/confirmation/` 的不可执行确认控制器与公开 API；不从 `app.safety` 顶层重导出，避免既有依赖链形成循环导入。
- [x] 补齐 `AuthorizationSnapshot.session_id` 必填契约；`is_valid_for()` 精确匹配会话，L2 只从活动窗口写入会话，生成快照保持 `consumed_at=None`。
- [x] 验证：L2 专项 11/11、契约与 L2 聚焦 23/23、P4 plans/safety 相关回归 45/45、全量 unittest 193/193、compileall、严格 UTF-8=75、精确副作用 AST 审计为空。
- [x] 创建 `implementation_plan/checkpoints/2026-09-20-P5-batch1-l2-confirmation-stop-handoff.md` 并停在批次 2 L3 门前。
- [x] 未读取 `.env`，未启动 Harness runtime，未发送 API 请求，未执行真实电脑副作用，未实现 L3、消费、执行器、账本、验证、审计持久化或恢复。

最新交接：`implementation_plan/checkpoints/2026-09-20-P5-batch1-l2-confirmation-stop-handoff.md`。

## P5 批次 2 完成停止点（2026-09-20）

- [x] L3 第一阶段模态允许只进入独立 `awaiting_phrase` 状态，不直接产生授权。
- [x] 第二阶段仅完整固定短语“确认执行”授权；半句、同义词、追加语气词、空白和普通聊天文本全部拒绝。
- [x] `AuthorizationSnapshot` 绑定 `issuer_instance_id`、`operation_id`、确认阶段和 L3 固定短语；L2/L3 组合不一致的快照无法构造或反序列化。
- [x] L2/L3 授权统一登记到进程内 `AuthorizationConsumptionStore`；消费在锁内原子完成，并发重复提交恰好一个成功。
- [x] 同一授权不能二次消费；重启后的新存储不认识旧授权，不同实例拒绝旧授权。
- [x] 全量 unittest 202/202、compileall、严格 UTF-8=78、公开导入 smoke 和精确 AST 无副作用审计全部通过。
- [x] 未实现执行前复检编排、执行器、账本、真实动作、验证、审计持久化或恢复。

当前停止在 P5 批次 3 执行前重新校验门前。最新交接：`implementation_plan/checkpoints/2026-09-20-P5-batch2-l3-consumption-stop-handoff.md`。

## P5 批次 3 完成停止点（2026-09-20）

- [x] 新增只读 `ExecutionPreflightGate`：精确复检当前实例、会话、operation、计划 ID/版本/hash、动作顺序、授权有效期和授权风险等级。
- [x] 每个动作重新核对当前 Tool Manifest 与 Policy 版本/决策；重新执行 PathGuard 快照校验，覆盖存在性、文件/目录身份、父路径身份、组件身份、resolved path 及 Symlink/Junction/Reparse Point 竞态。
- [x] 复用纯计划生成器重建规范化参数和预览；支持同 purpose 多路径；应用启动必须重新解析当前本地白名单，缺失、异常或登记变化均 fail closed。
- [x] 复检 `create_text_file` / `write_text_file` 内容哈希与字节数、写入源哈希、精确替换文本哈希、回收站完整路径清单及整份计划目标状态哈希。
- [x] 全部复检成功后才原子消费授权并返回 frozen/slotted、不可执行的 `ExecutionPreflightApproval`；任一复检失败使整份未消费授权永久失效。
- [x] 并发复检同一授权恰好一个成功，其余拒绝重放；迟到、旧实例、计划/参数/目标漂移均不能进入后续执行阶段。
- [x] 验证：preflight 专项 13/13、聚焦回归 88/88、全量 unittest 215/215、compileall、严格 UTF-8=80、公开导入 smoke 和精确 AST 无文件/进程/网络/执行调用审计全部通过。
- [x] 未实现真实执行器、Execution Ledger、`attempt_id`、文件/应用副作用、备份、取消、ResultVerifier、审计持久化、恢复、P6/ASR/TTS。

当前停止在 P5 批次 4 Execution Ledger 门前。最新交接：`implementation_plan/checkpoints/2026-09-20-P5-batch3-execution-preflight-stop-handoff.md`。


## P5 批次 4 完成停止点（2026-09-20）

- [x] 新增进程内 `ExecutionLedgerRegistry`，绑定当前 `instance_id`，在同一锁内原子阻止同一已消费 preflight 授权创建第二个 execution，并拒绝 `execution_id` 冲突、旧实例 approval 和早于 preflight 的创建时间。
- [x] 新增不可执行的 `ExecutionLedgerStore` / `LedgerExecutor`：绑定 `execution_id/action_id/attempt_id/executor_instance_id/fencing_token`，`operation_started` 在锁内原子记录，旧 fencing token、attempt 重绑、动作重开和未授权动作全部 fail closed。
- [x] 明确 `not_started/started/returned/completed/state_uncertain` 五类账本观察；completed 必须经过 returned，全部授权动作完成后 ledger 才为 `COMPLETED`，任一 `state_uncertain` 使 ledger 为 `UNCERTAIN`。
- [x] 快照及嵌套契约均 frozen/slotted；模块没有 `execute`、工具调用、文件/应用副作用、网络或进程入口。
- [x] 验证：Execution Ledger 专项 21/21、P2 执行完整性与 P5 L2/L3/preflight/ledger 聚焦 62/62、全量 unittest 236/236、compileall、严格 UTF-8=83、公开导入 smoke 与精确 AST 无文件/进程/网络/执行调用审计全部通过。
- [x] 当前账本和 fencing token 仍仅为进程内内存事实，没有 durable transaction/WAL、真实 `operation_started` 持久化、取消、执行、备份、ResultVerifier、审计持久化或恢复。

当前停止在 P5 批次 5 原子动作边界、协作取消和不可安全中断声明门前。最新交接：`implementation_plan/checkpoints/2026-09-20-P5-batch4-execution-ledger-stop-handoff.md`。
## P5 批次 5 执行计划（2026-09-21）

- [x] 读取批次 4 handoff、P5 路线图、ActionPlan、安全边界、错误恢复、ledger 实现与现有测试。
- [x] 冻结本批次设计：计划顺序串行；停止与开始在同一锁内竞争；取消模式来自 preflight 冻结 Manifest；不可安全区间只延迟协作取消，绝不强杀。
- [ ] 红测覆盖原子动作顺序、停止门、协作取消、不可安全区间、不可取消动作等待返回、系统中止和 stale fencing。
- [ ] 最小实现并保持无真实执行入口、无文件/应用副作用、无备份、无 ResultVerifier、无持久化、无恢复。
- [ ] 专项、聚焦、全量、compileall、UTF-8、公开导入与 AST 无副作用审计。
- [ ] 更新路线图、task_plan、progress，并生成 P5 批次 5 停止交接；不得进入批次 6。

### P5 批次 5 已观察错误

| 错误 | 尝试次数 | 解决方案 |
|---|---:|---|
| 直接使用 `python -m unittest tests.security...` 时 `tests` 不是包 | 1 | 改用 discover/显式加载，不把测试目录当包导入 |
| embed Python 即使设置 `PYTHONPATH`，discover 仍无法导入 `app` | 1 | 后续按上一交接，在 Python 进程内显式 `sys.path.insert(0, repo_root)` 后构造测试套件 |

## P5 批次 6 完成与停止点（2026-09-21）

- [x] 新增 `LocalExecutionBackend`：仅分派七个固定副作用工具；拒绝任意命令、Shell、解释器和参数串入口。
- [x] 备份先于执行，备份路径固定为 `backup_root/execution_id/action_id/attempt_id/`；备份后重新计算文件/目录摘要；路径必须绝对、位于 `allowed_root` 内且拒绝链接/reparse 路径。
- [x] 新增 `LocalResultVerifier`：不信任 backend 成功字段，重新观察文件内容/摘要、源目标存在性、PID 与白名单可执行文件身份；证据不足、冲突或无法观察统一归 `state_uncertain`。
- [x] 受控执行器已接入备份、固定后端、验证决策与账本终态收口；区分确定 `failed`、`partially_completed` 与 `state_uncertain`，首个非 completed 动作后跳过后续动作，不自动重试或回滚。
- [x] 测试：本地执行专项 5/5；P5 security 聚焦 75/75；全量 unittest 258/258；compileall 通过；严格 UTF-8 90 个 Python 文件、无 BOM/`U+FFFD`；执行面 AST 审计为空。
- [x] 实际副作用仅发生在临时目录：创建、写入、复制、移动、重命名和注入式安全回收站适配器均已测试；未调用真实 Windows 回收站，未启动真实应用，仅测试注入 launcher/PID probe。
- [x] 未读取 `.env`、未发送真实 API 请求、未启动正式 Harness runtime、未执行 Git add/commit/push。
- [x] 下一入口为 P5 批次 7；不得提前实现审计哈希链、恢复扫描/报告、分层重试、P6、ASR 或 TTS。

最新交接：`implementation_plan/checkpoints/2026-09-21-P5-batch6-backup-execution-verification-stop-handoff.md`。
## P5 阶段级集成验收与完成停止点（2026-09-21）

- [x] 新增 P5 跨模块集成测试，覆盖 L2/L3、授权消费、preflight、ledger、执行、验证、结果、审计、恢复与重试。
- [x] 显式验证自然语言汇报失败不会重复执行；停止/状态不明不会补做、回滚或自动重试。
- [x] 验收发现并关闭受控执行器未强制接入审计的安全缺口；审计失败现在稳定返回 `audit_append_failed` 并阻断执行/后续动作。
- [x] P1–P5 集成 6/6、P5 聚焦 97/97、全量 unittest 279/279、compileall、UTF-8 与执行面安全审计通过。
- [x] 创建 `implementation_plan/checkpoints/2026-09-21-P5-integration-acceptance.md` 与 `implementation_plan/checkpoints/P5-handoff.md`。
- [x] P5 标记为 `complete`；当前强制停在 P6 授权门前，未进入 ASR/TTS/Skill/UI。
## P6 批次 1 完成停止点（2026-09-21）

- 用户明确授权严格接续 `P5-handoff.md` 并直接开始下一批次；本轮只进入 P6 批次 1。
- 扩展既有 `AudioFrame`：增加 `device_epoch`、采样率、声道、PCM 格式、采样数和 payload 字节数，并校验时长与 PCM 尺寸一致；payload 不进入 JSON 契约。
- 新增有界内存 `AudioBus`：固定 session/generation/stream/device epoch，严格连续 sequence/frame index；旧边界帧只诊断，重复/跳号/乱序和溢出 fail-closed 并清空半截音频。
- mutable payload 入队复制；正常关闭可保留完整队列，取消/故障可清空；无设备、文件、网络、进程或命令入口。
- 验证：P6 批次 1 专项 8/8；P2 契约与状态聚焦 39/39；全量 unittest 287/287；compileall 通过；严格 UTF-8=107，无 U+FFFD；既有 BOM 仍为 2 个；audio AST 执行面审计为空。
- 当前 P6 状态为 `in_progress`；最新交接为 `implementation_plan/checkpoints/2026-09-21-P6-batch1-audio-bus-stop-handoff.md`。
- 强制停止：当前停在 P6 批次 2 门前；尚未接 sounddevice、麦克风、pre-roll、VAD、ASR Provider、ResultGate、TTS 或连续监听。

## P6 批次 3：VAD 仅作为按键输入结束判定（2026-09-21）

- [x] 读取并严格接续 P6 批次 2 capture/device/pre-roll/cleanup 交接。
- [x] 先补拒绝路径测试，再实现最小 VAD 会话：按键释放、静音超时、取消、帧身份与连续性校验、detector fail-closed。
- [x] 保持边界：VAD 不作为 ASR final、授权事实或 Agent 请求；不接 ASR Provider、ResultGate、TTS、连续监听或真实设备。
- [x] 音频专项 31/31、全量 unittest 310/310、compileall、UTF-8 与执行面审计通过。
- [x] 创建 `implementation_plan/checkpoints/2026-09-21-P6-batch3-vad-stop-handoff.md`，同步路线图与 progress。
- [ ] 下一批次为 P6 批次 4；本轮强制停止，不在本轮实现 sherpa-onnx/SenseVoice adapter。
## P6 批次 6 完成与停止点（2026-09-21）

- [x] 严格接续 P6 批次 5 ResultGate 交接，只实现 startup/session/runtime 故障、文字输入降级、恢复探测与待用户切回状态机。
- [x] 新增 `app/asr/recovery.py`：显式有限状态机、枚举化脱敏故障、一次性资源清理、新会话边界、防旧边界复用、连续探测、非安全空闲点延迟切换。
- [x] 清理回调失败时仍 fail-closed 到文字模式，只记录 `cleanup_failed`，不泄露异常正文。
- [x] 新增 12 个恢复测试，并补 `tests/asr/__init__.py` 修复标准全量 discovery 长期遗漏 ASR 的问题。
- [x] 验证：恢复 12/12；ASR 29/29；音频 31/31；真实包含 ASR 的全量 339/339；compileall、UTF-8 与执行面审计通过。
- [x] 创建 `implementation_plan/checkpoints/2026-09-21-P6-batch6-recovery-text-fallback-stop-handoff.md`，同步路线图。
- [x] 已完成 P6 阶段级集成验收与 P6 handoff；当前强制停在 P7/TTS 授权门前。

## P6 阶段级集成验收与完成停止点（2026-09-21）

- [x] 新增正式 `app/asr/push_to_talk.py`，完成 Capture → AudioBus → VAD → SenseVoice → ResultGate → Recovery → ConversationManager 接线。
- [x] 正常轮次只接受一次可靠 final；重复、迟到、旧 boundary、旧 generation 和故障竞态不创建第二次请求；正常完成可安全回到 `IDLE` 并要求下一轮使用全新边界。
- [x] Provider startup failure 映射为 `PROVIDER_UNAVAILABLE` 并安全降级文字；session warmup/runtime failure 关闭资源、清理临时音频并保持文字输入。
- [x] 新增 `tests/integration/test_p6_integration.py`，阶段级集成 6/6；ASR 29/29；音频 31/31；Conversation 19/19；全部 integration 12/12；全量 345/345。
- [x] 完成 compileall、严格 UTF-8、公开导入 smoke、执行面和脱敏审计；无网络、进程、命令、`.env`、后台线程/定时器或真实设备副作用。
- [x] 创建 `implementation_plan/checkpoints/2026-09-21-P6-integration-acceptance.md` 与 `implementation_plan/checkpoints/P6-handoff.md`。
- [x] P6 标记为 `complete`；当前强制停在 P7/TTS 授权门前，不进入 TTS、连续监听、语音 L3 或 P8。

## 2026-09-24: Intent-routing contract design sync

- **Status:** complete (design sync); code implementation not_started.
- **Completed:** integrated the intent-routing contract into the existing computer voice assistant design and unified TextShell and ASR accepted final at `ConversationManager -> IntentRouter -> RouteHandoff`.
- **Scope synced:** README, overview, design, roadmap, architecture, ActionPlan, safety, milestones, ASR, Agent, text E2E, voice E2E, this plan, progress, and the new handoff.
- **Hard rules:** two-level `route + intent`; required trace fields; slots are candidates; failures normalize to `ambiguous`; router does not authorize or execute; Laya is a replaceable provider only.
- **Not done:** no changes to `app/` or `tests/`; no real Laya connection; no service start; no computer side effects.
- **Stop point:** P6 complete; P7-R design sync complete; implementation remains at the P7/TTS authorization gate.
- **Next implementation entry:** versioned `RouteHandoff` -> pure `IntentRouter` -> failure normalizer -> ConversationManager text/voice integration -> provider adapter and tests.

## 2026-09-24: P7-R-01 RouteHandoff schema/version

- [x] 已授权且只执行 P7-R-01：先红测，再实现并公开导出严格、版本化的 RouteHandoff/追踪/槽/路由器状态契约。
- [x] 专项 10/10；契约 44/44；Conversation 19/19；全量 575/575；UTF-8/AST、执行面检查通过。
- [x] handoff：implementation_plan/checkpoints/2026-09-24-P7-R-01-route-handoff-schema-version-handoff.md。
- [ ] P7-R-02 pure IntentRouter 接口尚未开始；当前停在下一批次授权/验收门前，不进入 P7/TTS、P8 或真实 Provider。

## 2026-09-24: P7-R-02 pure IntentRouter 接口

- [x] 用户明确授权且只执行 P7-R-02：先写红测，再实现 provider-neutral、无副作用的 `RouteInput` 与 `IntentRouter` Protocol。
- [x] `RouteInput` 冻结入口文本与既有 `RouteTrace`，拒绝空文本、非契约 trace 和 raw_text 不一致，不生成或替换业务标识。
- [x] 专项 5/5；Contracts 44/44；Conversation 19/19；全量 580/580。
- [x] handoff：`implementation_plan/checkpoints/2026-09-24-P7-R-02-pure-intent-router-interface-handoff.md`。
- [x] P7-R-03 failure normalizer 已完成并通过专项/回归验收；当前停止，不进入 ConversationManager 接线、Provider、TTS、P8 或真实电脑副作用。

## 2026-09-24: P7-R-03 failure normalizer

- [x] 用户明确授权且只执行 P7-R-03：在既有 `RouteHandoff` 和纯 `IntentRouter` 接口之上增加 provider-neutral、无副作用的失败规范化函数。
- [x] `normalize_router_failure(...)` 将 `unavailable`、`timeout`、`transport_error` 统一为 `ambiguous/router_unavailable`，将 `invalid_response` 规范化为 `ambiguous/invalid_router_output`，将 `low_confidence` 规范化为 `ambiguous/low_confidence`。
- [x] 所有失败结果均为经 `RouteHandoff` 校验的安全对象：`handling_mode=clarify`、`required_confirmation=must_clarify`、空 `slots`、空 `tool_scope`，并保留原始 `RouteTrace` 和业务标识。
- [x] 边界拒绝非 `RouteTrace`、未知/非字符串状态、非低置信度失败携带置信度、Provider 异常正文或原始 payload；不把供应商错误内容传播到路由交接。
- [x] 专项 10/10；Contracts 44/44；Conversation 19/19；标准全量 discovery 585/585；`app/routing.py` AST/import 执行面审计通过；`app/` 与 `tests/` 共 146 个 Python 文件严格 UTF-8 编译检查通过。
- [x] 未接入真实 Provider、Laya、网络/API、ConversationManager、TTS、P8、授权/账本/执行/验证/审计持久化或任何电脑副作用。
- [x] handoff：`implementation_plan/checkpoints/2026-09-24-P7-R-03-failure-normalizer-handoff.md`。
- [ ] 下一停止点：P7-R-04 ConversationManager 文字/语音接缝；必须另行明确授权后实施，不得在本批次继续接线。
- [x] 用户于 2026-09-24 明确确认并授权 P7-R-04：ConversationManager 统一接收 TextShell 与 ASR accepted final，建立 RouteTrace，调用纯 IntentRouter，将 ambiguous 失败固定留在澄清边界，并按 turn_id 防重复/替换。
- [x] TextShell 与 ASR 接入口复用同一 `ConversationRouteResult`；ASR 只有 ResultGate `ACCEPTED_FINAL` 才可提交；未配置 Router 时保留 P6 兼容路径，不连接真实 Provider。
- [x] Conversation 19/19；integration 12/12；标准全量 discovery 585/585；新增/修改 Python 文件严格 UTF-8、AST 编译通过。
- [x] 未接入真实 Laya/Provider、网络/API、TTS、P8、授权/账本/执行/验证/审计持久化或电脑副作用。
- [x] handoff：`implementation_plan/checkpoints/2026-09-24-P7-R-04-conversation-routing-seam-handoff.md`。
- [ ] 下一停止点：P7-R-05 Provider adapter 与契约测试；必须另行明确授权后实施，不得在本批次继续接入真实 Provider。

## 2026-09-25：P7-R-08 真实 Provider 评估前置审计与独立授权门

- [x] 已授权且只执行 P7-R-08 离线前置审计；未联网、未读取 `.env`/密钥、未启动 Provider、未修改 `app/` 或电脑执行链。
- [x] 已新增 `implementation_plan/checkpoints/2026-09-25-P7-R-08-provider-evaluation-preflight-handoff.md`。
- [x] 已固化 Provider/model/version、脱敏样例、凭据、网络、请求/费用/超时、失败、日志脱敏、payload 隔离和回滚的逐项授权矩阵。
- [x] 结论：**NOT READY / STOP**；当前只具备 provider-neutral 离线契约证据，不具备真实 Provider 请求或 adapter 实现条件。
- [ ] 下一停止点：用户逐项明确授权真实 Provider 评估前的所有输入和安全边界；在此之前不得联网、读密钥或实现真实 adapter。
## 2026-09-25：P7-R-09 DeepSeek 结构化输出协议与离线契约

- [x] 用户确认并授权只执行 P7-R-09；未联网、未读取密钥、未启动 DeepSeek/Laya、未接入默认运行时或电脑副作用链。
- [x] 固定 `deepseek-route/v1` 严格 JSON 协议和字段到 `RouteHandoff` 的隔离映射；低置信度、非法响应和失败状态全部 fail-closed。
- [x] 新增 `app/routing.py::parse_deepseek_route_response(...)` 与 `tests/routing/test_deepseek_protocol.py`。
- [x] 验证：协议专项 9/9；Routing 41/41；相关 Provider/Conversation/Integration 27/27；全量 616/616；compileall、UTF-8、执行面审计通过。
- [x] handoff：`implementation_plan/checkpoints/2026-09-25-P7-R-09-deepseek-structured-protocol-handoff.md`。
- [ ] 下一停止点：真实 DeepSeek provider-specific adapter 仍 NOT READY / STOP；继续工作必须另行授权。

## 2026-09-25：P7-R-10 DeepSeek 脱敏原始输出边界

- [x] 用户确认并授权只执行 P7-R-10；严格限定为脱敏离线原始输出边界，不联网、不读密钥、不启动 Provider/Laya、不进入默认运行时或电脑副作用链。
- [x] 新增 `parse_deepseek_raw_route_response(...)`，只接受单个裸 JSON 文档，拒绝 Markdown、前后包裹、多文档、空值、非字符串和非法 JSON，并复用 `deepseek-route/v1` 严格协议。
- [x] 新增原始输出边界与 `ProviderIntentRouter` 接缝测试；原始 payload/错误文本不进入 `RouteHandoff`。
- [x] 验证：DeepSeek 专项 14/14；相关回归 59/59；全量 discovery 621/621；compileall、严格 UTF-8、执行面审计通过。
- [x] handoff：`implementation_plan/checkpoints/2026-09-25-P7-R-10-deepseek-raw-output-boundary-handoff.md`。
- [ ] 下一停止点：真实 DeepSeek provider-specific adapter 仍 NOT READY / STOP；继续工作必须另行授权。
## 2026-09-25：P7-R-12 DeepSeek 脱敏原始输出 fixture 契约扩展

- [x] 用户确认并授权只执行离线 fixture/契约扩展；不联网、不读密钥、不启动 DeepSeek/Laya、不接入默认运行时或电脑副作用链。
- [x] 新增转义 Unicode/字符串内空白、严格失败 envelope、嵌套结果新对象及错误文本隔离测试。
- [x] 验证：DeepSeek 专项 23/23；routing 专项 55/55；标准全量 discovery 630/630；`compileall app tests`、严格 UTF-8 与执行面审计通过。
- [x] handoff：`implementation_plan/checkpoints/2026-09-25-P7-R-12-deepseek-raw-fixture-contract-handoff.md`。
- [ ] 真实 DeepSeek provider-specific adapter 仍 NOT READY / STOP；不得在本批次接入真实 Provider。
## 2026-09-25：P7-R-13 DeepSeek 脱敏原始输出 fixture 契约扩展

- [x] 用户选择并授权继续仅做离线 fixture/契约扩展；不联网、不读密钥、不启动 DeepSeek/Laya、不接入默认运行时或电脑副作用链。
- [x] 新增字段顺序无关、低置信度 0/1 边界、tool_scope 列表隔离，以及 UTF-8 BOM/非法前缀拒绝测试。
- [x] 未修改 `app/routing.py`、默认运行时或真实 Provider 接线；真实 provider-specific adapter 仍 **NOT READY / STOP**。
- [x] 验证完成：DeepSeek 专项 27/27、routing 专项 59/59、全量 discovery 634/634、`compileall app tests`、严格 UTF-8 与执行面审计全部通过。
- [x] handoff：`implementation_plan/checkpoints/2026-09-25-P7-R-13-deepseek-raw-fixture-contract-handoff.md`。
- [ ] 下一停止点：仍不得实现真实 DeepSeek adapter；下一批须重新授权，优先做 provider-neutral adapter 设计审查或继续离线 fixture 契约扩展。

## P7-R-14：默认关闭 DeepSeek adapter 契约（2026-09-27）

- [x] 复用既有 provider-neutral routing seam，新增默认关闭 adapter 与注入式 transport Protocol。
- [x] 固化无凭据配置、HTTPS origin allowlist、超时、0..3 有界重试和 `provider_ready=False`。
- [x] 新增离线 fake transport 契约测试；禁止真实 HTTP/SDK、密钥和默认运行时接线。
- [ ] 真实 Provider transport 与接线必须另行授权。


> **原文损坏（2026-09-30 发现）**：本段原始中文已被替换为 ASCII `?`，逐字不可恢复；以下为按证据重写，紧接其后的代码块为损坏原文备审计。
## 2026-09-27：P7-R-19 运行时集成 Provider integration 接线

- [x] 把 `Settings.provider` 接入 `ProviderIntegrationConfig`，默认保持 local。
- [x] 默认不接入 DeepSeek 与 adapter：显式 DeepSeek 无 adapter 时保持 `provider_ready=false`，不创建 transport，不读取 secret 或凭据。
- [x] 启动状态投射 provider/readiness；未接入 Agent，也未进入执行、工具或 TTS 链。
- [x] Main/Config/Provider 专项与全量 discovery 663/663、compileall、diff check 通过。
- [x] 产出 P7-R-19 handoff。
- [ ] 下一停止点：DeepSeek runtime adapter、Agent 与真实网络请求仍需单独授权。

```text
## 2026-09-27?P7-R-19 ????? Provider integration ??

- [x] ? `Settings.provider` ?? `ProviderIntegrationConfig`??? local?
- [x] ???? DeepSeek ? adapter ? `provider_ready=false`???? transport???????? secret ???
- [x] ???????? provider/readiness??? Agent ???????????
- [x] Main/Config/Provider ????????? 663/663?compileall?diff check ???
- [x] ??? P7-R-19 handoff?
- [ ] ?? DeepSeek runtime ?????????Agent ?????????????????
```

## P7 VoiceStudio 双路径执行（2026-09-30）

- [x] Batch 1：统一契约与 fake adapter 基线
  - [x] 新增 sentence request、PCM chunk、provider Protocol 和 fail-closed stream validator
  - [x] 新增 fake WS、fake HTTP、fake PCM sink
  - [x] 新增 10 项契约测试
  - [x] TTS 回归 331 passed、2 skipped；compileall 通过
  - [x] 未接入默认运行时，未发生真实网络或设备调用
- [x] Batch 2：逐句 HTTP `/generate` 快速路径（已完成，离线验证通过）
- [ ] Batch 3：WebSocket 客户端与协议验证（离线部分已完成；真实协议 preflight 待单独授权）
  - [x] 固定 `websockets==17.1` wheel 与 SHA-256，离线装入 `.venv`
  - [x] 实现 `app/tts/providers/voicestudio_ws.py`：惰性建连、跨句复用、单在途请求、session idle 释放
  - [x] 实现 start/binary/done/error 状态机、超时、大小上限、取消与坏连接丢弃
  - [x] 新增 35 项测试（含 loopback fake server 三句串行与缺 done 断线）
  - [ ] 真实 VoiceStudio 三句串行 preflight 未执行，Batch 3 退出条件未满足
- [x] Batch 4：PCM 流式播放后端（离线 + 真机设备验证均完成）
  - [x] `PCMPlaybackWorker`：160 ms 起播、8 s 背压、16 MiB 单句上限、身份/顺序校验、迟到块丢弃
  - [x] `WaveOutPCMStreamBackend`：三 buffer 循环、`WOM_DONE` 后才复用、uncertain 不释放、winmm 可注入
  - [x] 35 项测试（含 fake WinMM 全生命周期与 `WHDR_PREPARED` 回归）
  - [x] 真实设备：自然完成 `COMPLETED`（6 write / 3 header）+ 真实 reset `STOP_ACKED`（`waveOutReset=0`），两次 `cleanup_verified=true`
  - [x] 真机抓出并修复 `WHDR_PREPARED` 被清零缺陷；未做 acoustic/听感推断（`user_heard=null`）
- [x] Batch 5：双路径调度与自动降级（5A 传输层 + 5C 配置 + 5B 流式会话接入，全部完成）
  - [x] `app/tts/transport.py`：WS 主 / HTTP 单句一次备用、仅首音前降级、出音后终态不换声、取消不降级
  - [x] 配置：`ws_pcm` mode、loopback-only WS/HTTP URL、4 个超时、2 个 PCM 缓冲项
  - [x] `app/tts/streaming_session.py`：`StreamingVoiceSession`（transport router → `PCMPlaybackWorker` 流式播放，独立于原实时会话）
  - [x] 传输失败交给 `TTSFallbackRouter`：未出音→`NOT_COMMITTED`、已出音→`PLAYBACK_STARTED`（禁换声/禁重播）
  - [x] `completed_reply`：mode 白名单扩展，`ws_pcm`/`auto` 必须显式注入 session_factory
  - [x] 测试：streaming 18 项、transport 18 项、config 27 项；TTS 460、全量 966 通过；legacy/http_wav 原有用例不变
  - [ ] 真实 VoiceStudio 端到端（真实 WS 流式播放、真实 WS→HTTP 降级、真实设备多句）未验证

## 2026-09-30：P7 VoiceStudio Batch 5 真实环境验收

- [x] 真实 WS 流式 3 句播放通过：`played=3`、真实 `waveOutWrite` 32 次（3 buffer 复用）、fallbacks=0、`cleanup_verified=true`。
- [x] 真实 WS→HTTP 降级通过：未监听端口触发真实 `connect_failed` → HTTP `/generate` 接管并完成播放，fallback 记录 `(connect, connect_failed)`。
- [x] 独立阻断已排除：选择文件曾被外部删除，经用户确认已重建同值文件并通过 `read_selection`/`verify_reference`/`verify_provider`。
- [ ] 无声学/听感推断（`user_heard=null`）；Batch 6-8 未开始；未提交、未 push。

## 2026-09-30：Agent→说话链路修复

- [x] ASR 路径接入逐句播报：`submit_asr_final(on_text_chunk=...)` + `PushToTalkCoordinator(realtime_speaker=...)` + `drive_turn_stream`（不合格 outcome 一律 cancel，不播报）。
- [x] 背压不再阻塞 Agent 线程：`push_text_chunk` 立即返回，单次 push 整批接受，超限置 `overloaded` 只拒后续 push，已入队句子播完。
- [x] 清理 `text_shell.py` 死代码与 `stream_ok` 死变量。
- [x] 改写 2 条把阻塞当正确行为的测试；新增 `tests/asr/test_stream_bridge.py` 9 项；全量 975 通过。
- [ ] 真实 ASR 设备端到端未验证；`submit_asr_final` 透传无单独测试（覆盖缺口已记录）。

## 2026-09-30：P7 VoiceStudio Batch 6（barge-in、取消顺序、迟到块隔离）

- [x] `StreamingVoiceSession.cancel()` 固化 8 步取消顺序并保证幂等；取消时推进 provider generation。
- [x] 新增只读 `generation` 与 `late_events`；取消后到达的 PCM/传输错误/播放结果只记诊断，永不恢复播放。
- [x] 跨回复隔离由新会话身份 + worker/validator 四元组校验保证（有测试）。
- [x] streaming 专项 24 项、全量 981 项通过；compileall 与 diff check exit 0。
- [ ] 未做真实 barge-in 与真实设备取消时序验证；legacy 会话无 generation/late 诊断（不对称已记录）。

## 2026-09-30：P7 VoiceStudio Batch 7（真实场景矩阵与重启恢复，7A）

- [x] 真实矩阵：句长 4/16/32/48 字、2 句回复、真实 WS→HTTP 降级全部通过；RTF 1.41–1.69（冷启动 10.78）。
- [x] 真实播放中取消：取消后 1 秒内无任何新音频进入设备（write_count 11 → 11），generation 推进到 2。
- [x] 矩阵发现并修复真实缺陷：长回复只播 3 句 → 句数上限改 8 且过载信号改用 8 s 音频背压；重测 5 句 5/5、20 句 9/20（刻意保护）。
- [x] 重启恢复（场景 8）：进程终止与端口释放已确认，用户手动重启后真实播放恢复正常（冷启动 5.3 s、warm 4.3 s）。
- [ ] 未覆盖：断线注入、真实 HTTP fallback 失败、Edge 真实播放（无实现）、逐句 p95。
- [ ] Batch 8 未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 8（收敛、回滚演练与阶段验收）

- [x] 回滚演练通过：`disabled`→`legacy`、`http_wav` 均无需代码改动或注入；`ws_pcm`/`auto` 必须显式注入（opt-in）。
- [x] 依赖与 wheel hash 已记录；`.gitignore` 补上 `.python312/`、`.wheelhouse/`、`.get-pip.py`、`.python312-embed.zip`（`.env` 原本已忽略）。
- [x] P7 五项退出条件逐条核对：**均未完全满足**（缺默认 runtime 接线、无 Edge 播放实现、双路径失败转文字未真实演练、VoiceProfile 更新边界未触发、断线注入与 p95 未做）。
- [x] 结论：**P7 仍 Blocked，不标记 complete，不进入 P8**；默认模式切换未执行。
- [ ] 未完成项均需单独授权：断线注入、HTTP fallback 失败、Edge 实现、p95 打点、runtime 接线、默认切换、提交。
- [ ] **更正（2026-09-30 晚些时候，保留原句不改）**：上面两条"未做/未完成"的清单已被同日后续批次推进——**断线注入、真实 HTTP fallback 失败、逐句 p95 打点、Edge 播放实现均已完成**（分别见 Batch 10、Batch 10、Batch 9/9-fix、Batch 11，见本文件末尾的收口条目与 `implementation_plan/P7-followup-execution-tracker.md`）；**仍未做**的是流式会话接入默认 runtime、默认模式切换（后者按双路径文档 §24 要求须用户审阅验收后再做），以及本轮全部改动提交。
- [x] 更正（同上）：Batch 6/7/8 已分别完成并有 checkpoint（Batch 6 barge-in、Batch 7 真实矩阵与重启恢复、Batch 8 回滚演练与阶段验收）。

## 2026-09-30：当前工作区审阅与待提交清单

- [x] 审阅本日产出：本批 33 个文件 + 9 个 checkpoint + 4 个 artifact 目录；工作区 342 条状态中绝大多数为项目历史未跟踪内容（至今仅 1 个 commit）。
- [x] **提交前必须处理**：6 个 `AM` 文件的索引版本是 Batch 1 旧版，直接 commit 会丢改动 —— 需先重新 `git add`：`app/config/settings.py`、`app/tts/providers/__init__.py`、双路径计划、P7 跟踪表、`progress.md`、`task_plan.md`。
- [x] 安全：`.env` 等原本已忽略；本轮补齐 4 条运行时忽略项并确认 → 不会提交 secret/运行时/wheel。
- [x] **已提交并推送**：`fdb0ce1`「项目首次完整入库」→ `origin` 的 `codex/review-playback-lifecycle` 与 `main` 均指向该提交；预演确认无 secret/运行时/wheel（495 文件 / 4.77 MB）；仓库经 API 探测判定为 private（闭源）。
- [x] 范围修正：本次把历史遗留内容（`app/persona`、`app/plans`、`app/executor`、`app/tools`、`docs/`、`tools/` 等）也一并入库，以保证仓库完整可运行。
- [ ] Batch 6：barge-in、取消与迟到块隔离
- [ ] Batch 7：真实 VoiceStudio、Windows 与性能验证
- [ ] Batch 8：默认切换、文档、回滚与阶段验收

## 2026-09-30：P7 VoiceStudio Batch 1 收尾跟踪

- [x] Batch 1 专项测试：10/10。
- [x] TTS 回归：运行 331 项，OK（2 skipped）。
- [x] 全量 discovery（带项目根 top-level）：运行 830 项，OK（2 skipped）。
- [x] `python -m compileall -q app tests tools` 通过。
- [x] 修复 `contracts.py` EOF 空行并重新暂存相关文件。
- [x] 只跟踪 Batch 1 相关文件；未执行 `git add .`，未纳入 `.venv`、`.python312`、`.wheelhouse` 等运行时目录。
- [ ] 等待用户审阅完整 staged diff；不提交、不 push，不进入 Batch 2。

## 2026-09-30：P7 VoiceStudio Batch 2 checkpoint

- [x] 完成逐句 HTTP /generate WAV 快速路径，包含 loopback/no-redirect 安全边界与完整 WAV fail-closed 校验。
- [x] 完成实时分句软策略（4 / 32 / 24–40 / 48）与 pending=2 背压，保持默认 legacy 运行入口不变。
- [x] 补齐 Provider、分句、实时会话的离线测试并通过 TTS/全量 discovery。
- [x] 写入 Batch 2 交接 checkpoint。
- [ ] Batch 3 WebSocket、真实 VoiceStudio、真实设备验证均未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 3 checkpoint（离线部分）

- [x] 完成 `/ws/tts` 同步客户端：loopback-only 配置、惰性建连、跨句复用、单连接单在途请求、session idle 释放。
- [x] 完成 start/binary/done/error 严格协议状态机、超时、帧与累计音频上限、取消即关 socket、失败即丢弃坏连接、错误代码脱敏。
- [x] 固定 `websockets==17.1`（wheel SHA-256 `617243E1...412BEC`，BSD-3-Clause，零依赖）并离线安装。
- [x] 新增 `app/tts/providers/voicestudio_ws.py`、`tests/tts/test_voicestudio_ws.py`（35 项），导出并入 `app/tts/providers/__init__.py`。
- [x] 验证：WS 专项 35/35；TTS 383/383（2 skipped）；全量 882/882（2 skipped）；compileall、`git diff --check`、严格 UTF-8 与导入/调用面审计通过。
- [ ] 真实 VoiceStudio 三句串行 preflight 未执行（3900 未监听），Batch 3 退出条件未满足；PCM 播放、自动降级、barge-in 与默认切换均未开始。
- [ ] 未提交、未 push；等待用户审阅本批 diff。

## 2026-09-30：P7 VoiceStudio Batch 3 真实协议 preflight

- [x] 本机 VoiceStudio v0.5.6（cuda / RTX 3050 Laptop）默认配置真实 3 句串行：3/3 成功、连接数 = 1、每句 `is_final=True`、`done.samples` 与实收样本一致；Batch 3 退出条件满足。
- [x] 真实环境暴露缺陷并最小修复：`start` 帧在首句合成后才到，首个 recv 不得用 3 s 帧 idle 约束；新增 2 项测试并修正计划文档 7.4 节。
- [x] 修复真实链路阻断：音色选择文件 BOM + `speed` int 导致 `CompletedReplySpeaker` 静音；备份后原子替换为同值文件，未放宽严格校验。
- [x] 证据 artifact：`implementation_plan/checkpoints/artifacts/2026-09-30-P7-batch3-ws-preflight/`。
- [x] 修复后复跑通过：WS 专项 37/37、TTS 385（2 skipped）、全量 884（2 skipped）、compileall 与 `git diff --check` 均 exit 0。
- [ ] PCM 播放、自动降级、barge-in、默认切换均未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 4（PCM 流式播放后端，离线部分）

- [x] 新增 `app/tts/pcm_playback.py`：`PCMStreamBackend` Protocol 与 `PCMPlaybackWorker`（160 ms 起播阈值、8 s 未提交队列上限、16 MiB 单句上限、身份/sequence 校验、迟到块丢弃、stop/timeout/unknown fail-closed）。
- [x] 新增 `app/tts/waveout_pcm.py`：三 buffer `WAVeHDR` 循环流式后端（`WOM_DONE` 后才复用、reset 在锁外、uncertain 不释放 buffer、诊断快照、winmm 可注入）。
- [x] 新增 `tests/tts/test_pcm_playback.py` 34 项并通过全量回归。
- [ ] 真实 Windows 设备验证未执行，Batch 4 退出条件的设备项未满足。
- [ ] 未接入实时会话与双路径调度；未提交、未 push。
- [x] **更正（2026-09-30 晚些时候）**：Batch 4 的真实设备验证已完成（三 buffer 生命周期、真实 waveOut write 复用与 stop 回收，见 `implementation_plan/checkpoints/2026-09-30-P7-voicestudio-batch4-pcm-playback.md`）；双路径调度由 Batch 5（transport + streaming session）完成。

## 2026-09-30：P7 双路径剩余项收口（Batch 9 / 9-fix / 10 / 11）

- [x] **Batch 9 与真实采集**：句级时间线打点（六类时钟、只读统计面、最近秩百分位）+ 真实 20 句 paced 采集；发现 Windows `time.monotonic` 分辨率 15.625 ms 不可测 worker 跨度、`ready_to_submit` 未达 <20 ms（根因=每句 `verify_provider`）、一次性推入 20 句触发过载保护。checkpoint：`2026-09-30-P7-b9-sentence-timing-instrumentation.md`、`2026-09-30-P7-b9-real-p95-measurement.md`。
- [x] **Batch 9-fix**：高分辨率默认时钟（`app/tts/clock.py`）、每回复只验证一次、过载可诊断（`dropped_pushes`/`push_rejected_*`/`finish_timed_out`）、新增 `submit_to_first_pcm_ms`。修复后真实复测：19.2 四条指标**全部通过**（`ready_to_submit` p95 0.236/0.173/0.200 ms；`gate_to_device` p95 ≤0.029 ms）。checkpoint：`2026-09-30-P7-b9-defect-fixes.md`。
- [x] **Batch 10 真实传输故障演练**：`dual_path_dead`、`http_dead`、`kill_before_audio`、`kill_after_playback`、`proxy_drop_mid_sentence` 五个场景；未出音 → `NOT_COMMITTED` 转文字，已出音 → `PLAYBACK_STARTED` 且不重播/不半句换声。checkpoint：`2026-09-30-P7-batch10-real-transport-faults.md`。
- [x] **Batch 11 离线 Edge 声线**：`app/tts/providers/edge_sapi.py` + 纯 ASCII 脚本 `edge_sapi_runner.ps1` + 配置项 + 会话 `edge_provider` 接线（仅未出音可切、单次尝试、失败即文字）。真实三条验收：离线声线直接出声（`COMPLETED`、首音 0.42 s）、**杀掉 VoiceStudio 后自动切 Edge 并真的播出**（`edge_attempts=1`、`edge_used@generation1`）、已出音后断线**不切 Edge**（`edge_attempts=0`）。
- [x] 回归：全量 **1065 tests / OK (3 skipped)**；`compileall` exit 0；`git diff --check` exit 0。
- [x] 文档收口：双路径文档 §24 逐项对账、`progress.md` 与本文件补登、tracker 新增 B11 记录与最终 checkpoint（`2026-09-30-P7-dual-path-plan-dod-status.md`）。
- [x] **用户听感证据（DoD 第 11 项）**：真实播放主路径（VoiceStudio 小饼声线）与备用声线（离线 Edge 槽）各一句，用户确认两句都听到 → `user_heard=true`（原始播放 artifact + 用户确认件在 `implementation_plan/checkpoints/artifacts/2026-09-30-P7-user-heard/`；非声学测量）。
- [x] **默认模式决策（DoD 第 14 项）**：用户 2026-09-30 决定**保持 `disabled`，不切 `auto`**；第 5/6 项分别暂缓/不适用（附理由）。
- [x] **P7 阶段级验收通过（2026-09-30）**：五项退出条件逐条判定通过 → `P7 complete`（阶段级），验收文档 `implementation_plan/checkpoints/2026-09-30-P7-stage-acceptance-final.md`；全程 1065 tests OK (3 skipped)；停点=不接播放时监听、未进入 P8、默认语音仍 opt-in。
- [x] **P0–P6 跨阶段复核（2026-09-30）**：七阶段状态与验收证据齐备、对应测试分组全绿、无功能性回归；改动仅限 `app/tts/**` 与 `app/config/settings.py`。文档：`implementation_plan/checkpoints/2026-09-30-P0-P6-cross-stage-recheck.md`。
- [x] **全量基线更正**：补回 `tests/evidence`（39）与 `tests/recovery`（3）缺失的 `__init__.py` → 全量 **1107 tests OK (3 skipped)**（此前 1065 是当时收集数）；路线图 P3/P4/P5 卡片补状态行。
- [x] **已闭项（2026-09-30）**：本轮全部改动已提交（`d20fe87`/`eeb56b2`/`f4edc30`/`0440493`/`79dd525`）；真实设备人工实测已完成（P8 真机按键两轮 + 用户听感确认）；R 支线真实路由已不再 NOT READY（`P7-R-27`~`P7-R-29`：真实探测 → 门禁解除 → 真实对话 Agent）。
- [ ] **仍待**：P9（VAD/连续对话/唤醒词/声纹）须另行授权；R-29 的批次 B 改动待用户语音实测后提交。

## 2026-09-30：P8 批次 1-3（按键语音闭环：边界、输入接缝、闲聊闭环）

- [x] 用户明确授权执行 P8 批次 1-3，且不先处理 P7 遗留未提交改动；本轮只执行这三批，未进入批次 4-6。
- [x] **批次 1**：新增 `app/voice_loop/boundary.py`：`VoiceTurnBoundary`（voice_session/utterance/turn/request/reply 身份，UTC 时间与来源严格校验）、`VoiceTurnRegistry`（同一时刻至多一个活动轮次、身份不复用、归档旧边界、迟到边界 `is_known=False`）、`VoiceLoopStateMachine`（显式迁移表；任务状态已命名但无入边迁移 → fail closed）、脱敏 `VoiceLoopDiagnostic`。
- [x] **批次 2**：新增 `app/voice_loop/input_seam.py`：TextShell 与 ASR accepted final 走同一个 `ConversationManager` 入口；ASR/TTS 不可用只切输入通道（`degrade`），绝不触碰授权/执行事实；语音就绪不自动切回，只有用户明确请求 + 本地检查通过 + 安全空闲点才 `request_voice_switch`；通道不可跨用、同一轮次只能提交一次；公开 API 结构测试锁定“无授权/执行面”。
- [x] **批次 3**：新增 `app/voice_loop/chat_loop.py`（`VoiceDriverTurnResult`、`VoiceTurnDriver` Protocol、`ChatVoiceLoop`：开始→收音→提交→仅播报完整成功回复→回空闲；失败/降级/迟到边界一律 fail closed 并清理）与 `app/voice_loop/drivers.py`（`PushToTalkVoiceDriver` 适配真实 P6 `PushToTalkCoordinator`，`speech_eligible` 与 P6 规则交叉校验）。
- [x] 对 `app/asr/push_to_talk.py` 做**唯一且纯增量**改动：`release`/`accept_provider_event` 增加可选 `turn_id`/`request_id` 透传（默认 `None` 时行为完全不变），使真实 P6 管道提交的 trace 与 P8 轮次边界一致。
- [x] 集成验证：`tests/integration/test_p8_integration.py` 用真实 P6 管道 + 离线 fake 设备/Provider 跑通“按键→ASR→ResultGate→同一 Agent 会话→逐句播报”，并验证 request_id 一致、每轮唯一、解码失败不提交不播报、ASR 启动失败后文字通道仍可用。
- [x] 验证：voice_loop 专项 66/66、integration 16/16、ASR 38/38、标准全量 discovery **1177 tests OK (skipped=3)**（基线 1107 + 新增 70）、`compileall -q app tests tools` exit 0、`git diff --check` exit 0。
- [x] 只读安全审计：`app/voice_loop/**` 只导入 `app.agent.adapter`、`app.asr.recovery`、`app.asr.result_gate`、`app.contracts.base`、`app.conversation.manager` 与自身模块；无 subprocess/socket/os/pathlib/threading 等导入或文件、网络、进程、设备调用；安全链模块（safety/executor/plans/auditing/verification/tools）引用数 0；新增文件严格 UTF-8 无 BOM、无 `U+FFFD`（仓库既有 23 个 BOM 文件未改动）。
- [x] 未联网、未读取 `.env`/密钥、未启动真实 DeepSeek/VoiceStudio/麦克风/扬声器、未执行任何电脑副作用、未提交、未 push。
- [x] handoff：`implementation_plan/checkpoints/2026-09-30-P8-batch1-3-voice-loop-chat-closed-loop-stop-handoff.md`。

## 2026-09-30：P8 批次 4-6 与阶段级验收（任务路径、“停”三语境、P8 收口）

- [x] 用户明确要求“继续完成 P8”，本轮在批次 1-3 基础上执行批次 4-6；仍未联网、未读 `.env`、未启动真实 Provider/设备、未执行电脑副作用、未提交。
- [x] **批次 4 任务路径编排**：新增 `app/runtime/orchestration/task_flow.py` + `summary.py`：`RouteHandoff` 只接受 `task`/`dangerous`（其余路径含只读 `query` 一律 `route_not_supported` fail closed）；`TaskPlanPreviewer` 用本地注册表 + `build_write_action_preview` + `PolicyEngine` + ActionPlan 冻结不可执行计划，只接受同质 L2/L3；`TaskFlowOrchestrator` 显式状态机（idle→preview_ready→authorized→executed→reported，另有 rejected/failed/terminal）串起 L2/L3 确认 → `ExecutionPreflightGate` → `ControlledExecutionRunner`；摘要只由 `ResultDocument` 结构化事实生成，禁止错误正文、路径与载荷。
- [x] **批次 5“停”三语境**：新增 `app/runtime/orchestration/stop.py`：上下文优先级固定为 **执行 > 授权 > 聊天 > 空闲**；聊天停播报但继续监听；授权取消窗口且停止后不可能再产生授权；执行走 P5 账本 `request_stop`，可协作取消登记 `CANCEL_REQUESTED`，不可安全中断区间只 `EXECUTION_ATOMIC_WAIT` 等待原子动作返回，绝不强杀；`resume_allowed` 硬编码为假，迟到事件只做隔离。
- [x] **批次 6 阶段级验收**：新增 `tests/integration/test_p8_stage_acceptance.py`（17 项）逐条覆盖路线图五条退出条件与跨阶段回归（无 `plan.json`/`result.json`/JSONL 落盘；有无语音时计划与结果事实、审计事件完全一致；`app/voice_loop` 安全链引用数为 0 已固化为测试）。
- [x] 新增/新增测试：`tests/voice_loop/` 67、`tests/orchestration/` 69、`tests/integration/test_p8_integration.py` 6、`tests/integration/test_p8_stage_acceptance.py` 17。
- [x] 验证：标准全量 discovery **1266 tests OK (skipped=3)**（基线 1107 + P8 新增 159）、`tests/asr` 38/38、`compileall -q app tests tools` exit 0、`git diff --check` exit 0、只读审计 `AUDIT_OK`（235 个 Python 文件；新增文件严格 UTF-8 无 BOM；`app/voice_loop` 零安全链引用；`app/runtime/orchestration` 无 subprocess/os/eval/exec 面）。
- [x] 生成 `implementation_plan/checkpoints/2026-09-30-P8-integration-acceptance.md` 与 `implementation_plan/checkpoints/P8-handoff.md`；路线图 P8 标记 `complete` 并写入停点。
- [ ] **明确未做**：真实麦克风/扬声器人工实测（需用户参与）、Agent 侧结构化提议接真实模型（R 支线独立授权门，仍 NOT READY / STOP）、只读 `query` 直执行路径、`app/main.py` 默认运行时接线、生成中逐句播报优化。
- [ ] 下一停止点：P9（VAD、连续对话、唤醒词与声纹过滤）必须另行明确授权。

## 2026-09-30：P8 真机验收 runner（按键实测）

- [x] 用户要求提供 `--live-opt-in` 真机验收 runner，已交付 `tools/p8_real_voice_loop_runner.py`。
- [x] 真实链路：按 Enter 开始说话 → 真实麦克风采集（sounddevice）→ 本地 `sherpa-onnx` SenseVoice → 同一个 `ConversationManager` → 本地脚本 Agent → 真实 TTS 播报（`ws_pcm`/`http_wav`/`auto`/`edge`，或 `none` 只听识别并验证提交）。
- [x] 硬边界：无 `--live-opt-in` 立即退出码 2 且不导入 sounddevice/sherpa-onnx；不创建计划/授权/执行；不操作电脑；不落盘原始音频；识别文本只打印到控制台，artifact 只记长度与 SHA-256；TTS 仅本机 loopback 或本机离线声线。
- [x] 退出码：0 通过 / 1 某轮未提交或未播报 / 2 未 opt-in / 3 前置检查失败（设备、模型、依赖或本机 TTS 不可用）。
- [x] 离线契约测试 `tests/integration/test_p8_real_runner.py` 11 项：拒绝未 opt-in 且不触设备、参数校验、注入替身环境跑通一轮并校验 artifact 脱敏、`--check-only` 不写 artifact、环境/ASR 启动失败退出码、runner 无安全链与进程面、能量检测器。
- [x] 实跑核实现状：`--live-opt-in --check-only --tts-mode none` 曾返回退出码 3 并给出安装提示 —— `.venv` 与 `.python312` 均未安装 `sounddevice` 与 `sherpa-onnx`（P6 已把“真实模型 + 真机设备联合测试”列为延期项）。PyPI 最新：`sounddevice` 0.5.6、`sherpa-onnx` 1.13.8。
- [x] **用户批准后完成离线安装并登记哈希**：`.wheelhouse/` 固定 `sounddevice 0.5.6`、`sherpa-onnx 1.13.8`、`sherpa-onnx-core 1.13.8`、`numpy 2.5.3`、`cffi 2.1.1`、`pycparser 3.0`（SHA-256 见验收文档 6.1），`pip install --no-index --find-links .wheelhouse` 成功。
- [x] **真机运行抓出并修复 P6 遗留缺陷**：`sd.default.device` 在 sounddevice 0.5.x 是 `_InputOutputPair`（非 tuple/list），原 `SoundDeviceCaptureBackend.default_input_device()` 直接 `int()` 抛 `TypeError`；改为 `_default_input_index()`（支持 `input` 属性/下标，无法确认即 fail closed）+ 4 项离线回归测试。另修 runner 控制台 UTF-8 与默认设备显示。
- [x] 真机前置检查通过：`--live-opt-in --check-only --tts-mode ws_pcm` → exit 0（输入设备=麦克风阵列 (Realtek(R) Audio)，模型就位，VoiceStudio `/health` 200）。全量 **1281 tests OK (skipped=3)**、compileall exit 0。
- [x] **首次按键实测暴露并修复 P6 第二个真机缺陷**：用户实测两轮均 `start_rejected`（artifact `...134412Z-6ccd6ddde3544761.json`）。根因：`CaptureSession.start()` 把字符串设备 id 传给 `sd.InputStream(device="1")`，sounddevice 对字符串做设备名子串匹配 → `ValueError: No input device matching '1'`，且协调器只回 `start_rejected`。已新增 `_device_argument()` 在 sounddevice 边界统一归一化（`check_input_settings`/`open_input_stream` 共用），并把 `PortAudioError` 等厂商异常按设备状态处理；`PushToTalkStartResult.diagnostic` + `PushToTalkVoiceDriver.last_start_reason` 让启动失败原因可见；新增 3 项离线回归。
- [x] **真机非交互端到端验证**：真实麦克风 3 秒 → 152 帧真实 PCM → `asr_ready` → 闸门 `accepted_final`；环境噪声标定 p50 = 47.7 / max = 150 RMS（默认阈值 600 余量充足）；但 3 秒静音被 SenseVoice 幻觉成 `'The.'` 并被接纳 → runner 增加**静音丢弃守卫**（未检测到语音就 `cancel_turn`，不提交不播报），并新增 1 项离线回归。真机非交互一轮 `--tts-mode none` 跑通并正确丢弃（exit 1）。全量 **1285 tests OK (skipped=3)**。
- [x] **真机按键实测完成（2026-09-30，用户本人执行）**：`--live-opt-in --turns 2 --tts-mode ws_pcm` → 两轮 `accepted/submitted/spoken` 全部 true（帧数 172/202，结束方式 button_release/silence_timeout），TTS 真实 `voicestudio_ws_pcm`（2 句/3 句全部完成、无错误），每轮五个轮次身份互不相同；用户确认**两轮语音都听到**（`user_heard=true`）且识别结果**基本正确**。登记件 `implementation_plan/checkpoints/artifacts/2026-09-30-P8-real-voice-loop/p8-user-heard-confirmation-20260930T135551Z.json`（原 artifact SHA-256 `F3E09B72…0B7D61` 未修改）；逐轮事实见验收文档 6.3.1。路线图 P8 退出条件 1 由此在真机 + 真人听感层面成立（Agent 为脚本替身，已标注）。
- [x] **已闭项（2026-09-30）**：真实模型端到端计划提议已由 `P7-R-27`~`P7-R-29` 接通（真实路由 + 真实对话）；`app/main.py` 默认运行时接线已完成（批次 A 路由接管、批次 B 真实 Agent）；本轮全部改动已提交。
- [ ] 仍待：任务链消费真实路由（编排未接）、语音链真机确认（`--voice` 已复用真实 Agent）、P9（另行授权）。

## 2026-09-30：P8 语音入口接线进主程序（`python -m app.main --voice`）

- [x] 用户选择“主程序语音入口接线”为下一步；新增 `app/runtime/voice_entry.py`（`VoiceEntryConfig`、`build_real_environment`、`VoiceLoopSession`、`VoiceTurnFacts`、静音守卫、TTS 适配器），`tools/p8_real_voice_loop_runner.py` 改为复用同一份装配。
- [x] `app/main.py`：`run()` 增加 `push_to_talk`/`voice_config`/`voice_session_factory`；新增 CLI `--voice --tts-mode --device-id --speech-threshold --silence-ms`；默认仍文字模式；语音不可用即降级文字且不切换 Provider；`/text` 明确切回且不自动切回；未接真实模型时如实提示、不伪造回复。
- [x] 增量接口：`VoiceDriverTurnResult.accepted_text` + `PushToTalkVoiceDriver.last_result`（闭环显示识别文本与记录长度）。
- [x] 验证：新增 21 项测试（`tests/integration/test_p8_voice_entry.py` 11 + `tests/foundation/test_main_push_to_talk.py` 10），runner 契约 12/12；全量 **1306 tests OK (skipped=3)**、compileall exit 0。
- [x] 真实主程序冒烟（临时数据根）：`"" | python -m app.main --voice --tts-mode none` → 真实设备+模型、就绪横幅、一轮无语音丢弃、exit 0。
- [x] 交接：`implementation_plan/checkpoints/2026-09-30-P8-voice-entry-main-wiring-handoff.md`。
- [x] **已闭项（2026-09-30）**：真实模型（`P7-R-29` 接通真实对话 Agent，端到端真实回复已验证）；提交（`d20fe87`/`eeb56b2`/`f4edc30`）。
- [ ] 仍待：任务链路接默认运行时（编排尚未消费真实路由）、语音链真机确认、P9（另行授权）；批次 B（R-29）14 项改动待用户实测后提交。

## 2026-09-30：P7-R-27 真实 DeepSeek Provider 有界探测（NOT READY 未解除）

- [x] 用户逐项授权：模型 `deepseek-flash`、凭据仅从用户级环境变量进程内注入（不落盘/不打印）、网络仅 `api.deepseek.com/chat/completions`、单轮 ≤3 次请求、无重试、单次超时 ≤15 s、`max_tokens=256`、费用上限 ≤0.05 元；仅做最小真实探测，不接运行时。
- [x] 新增 `tools/r_real_provider_probe.py`（离线预检 + 有界真实探测 + 结构诊断，绝不记录正文）；`DeepSeekAdapterConfig`/`DeepSeekRequest` 增加可选 `max_tokens`/`thinking`（默认 `None`，向后兼容）；`_ROUTE_SYSTEM_PROMPT` 重写为完整输出协议并内嵌合法示例。
- [x] 真实结论（10 次请求，全部 HTTP 200 + 合法 JSON + `finish_reason=stop`）：默认 prompt 下模型返回 `{"response": ...}` 包装 → 协议不符；强化并修正 prompt 后，`chat`/`task`/`ambiguous`（两种合法形态）均通过严格解析且落点安全（含糊请求 fail closed 到 `ambiguous`）；最终 prompt 下 `chat`/`task` 已补测通过。
- [x] 验证：新增 25 项契约测试；全量 **1332 tests OK (skipped=3)**、`compileall -q app tests tools` exit 0、`--check-only` 预检全部通过。
- [x] 交接：`implementation_plan/checkpoints/2026-09-30-P7-R-27-real-provider-bounded-probe-handoff.md`；证据 `implementation_plan/checkpoints/artifacts/2026-09-30-R-real-provider-probe/`。
- [ ] 仍待：解除 `provider_ready=False` 与运行时映射/失败矩阵（需另行授权）；任务链路接默认运行时；P9；提交。真实样例样本量仍小（每类 1–2 次）。

## 2026-09-30：P7-R-28 解除 DeepSeek 运行时门禁（批次 A）

- [x] 用户逐项授权：仅批次 A（provider 门禁 + 路由层接入）、双条件开关、开启后运行时自动请求、**不 fallback**。
- [x] 配置：`ASSISTANT_DEEPSEEK_OPT_IN`（严格布尔）；adapter 新增 `authorized`（`provider_ready = enabled and authorized`）；`ProviderIntegration` 新增 `transport_ready` 语义与 `router_metadata`。
- [x] 接线：`build_provider_integration` 双条件下装配真实 `DeepSeekHTTPTransport`（构造不读密钥、不联网）；`select_runtime_router` 让就绪的真实 provider 接管路由（显式注入优先）；startup 如实提示计费与不回退。
- [x] 就绪语义：deepseek+opt-in → `adapter_injected/adapter_ready/provider_ready/transport_ready=True`，`runtime_ready/agent_ready=False`；默认 local 行为完全不变。
- [x] 验证：新增 27 项测试；全量 **1359 tests OK (skipped=3)**、compileall exit 0；端到端 ① 无凭据 fail closed 且不回退（0 请求）② 有凭据真实 `task/open_application/0.95`（1 请求，731.5 ms）。
- [x] 交接：`implementation_plan/checkpoints/2026-09-30-P7-R-28-deepseek-runtime-gate-lift-handoff.md`。
- [ ] 仍待：**批次 B（Agent 真实对话）**；`runtime_ready/agent_ready` 提升；语音链与任务链接入；提交。

## 2026-10-01：P9 批次 1-2（自动收音与连续对话边界）+ 真机免按键试听

- [x] 用户授权本轮只做 P9 B1+B2 并做真机自动收音试听；唤醒词方向=sherpa-onnx KWS，声纹方向=sherpa-onnx speaker embedding（B3/B4 未开始）。
- [x] 新增 `app/dialog/**`（对话 11 态状态机、连续对话窗口、turn claim/输入许可）56 项、`app/audio/vad/auto_capture.py` 52 项、`app/voice_loop/auto_driver.py` 27 项、真机接线与契约 9 项、只读审计 62 项。
- [x] 真机抓出并修复 3 个缺陷：① 丢弃轮次锁死 ASR 通道 → `PushToTalkCoordinator.abandon_turn()`；② 自动结束被误当故障丢弃 → 改走 `release()` 正常收尾；③ **edge 播报因 `PCMPlaybackBackpressure` 完全不出声**（既有缺陷）→ `EdgeStream` 按 `queued_audio_ms` 节流。
- [x] 真机免按键试听通过：识别 `'你好，小饼测试一下。'` → 提交 → 离线声线播报 `COMPLETED`（`error=None`、首音 0.021 ms）；**用户本人确认听到且清楚**。
- [x] 验证：全量 **1620 tests OK (skipped=3)**、`compileall` exit 0；交接 `implementation_plan/checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`。
- [ ] 仍待：P9 B3 唤醒词、B4 声纹、自动收音接 `app/main.py` 默认运行时、AEC/全双工；本轮改动未提交、未 push。

## 2026-10-01：P9 B2.1 自适应阈值 + B2.2 Silero VAD + 主路线真机验收

- [x] 用户明确 VoiceStudio 为**主路线**（edge 仅备用）；主路线启动方式已核实：`...\VoiceStudio\project\.venv\Scripts\python.exe backend\main.py`（uvicorn @ 127.0.0.1:3900，cuda/RTX 3050）；直接跑 `VoiceStudio.exe` 无效。
- [x] B2.1 新增 `app/audio/vad/adaptive.py`（环境静音采样 → 阈值 339，fail closed）+ runner `--threshold-mode auto`；45 项测试。
- [x] B2.2 新增 `app/audio/vad/silero.py`（sherpa-onnx Silero VAD 人声判定）+ 驱动 `speech_detector` 接线 + runner `--vad-mode silero`（默认）；47 项测试。
- [x] 真机定位并修复接线缺陷：512 样本整窗 / 320 样本帧错位导致帧级连续命中永远达不到阈值 → 窗口结论粘滞 + 帧级阈值 1 + 未起始硬超时；新增 `vad_hits` 诊断。
- [x] **主路线真机免按键验收通过**（音乐环境）：识别 75 字与用户实际说话一致 → 提交 → `voicestudio_ws_pcm` 播报 `error=None`、3 句全部完成；**用户确认听到且声音清楚**。
- [x] 全量 **1747 tests OK (skipped=3)**、`compileall` exit 0；交接 `implementation_plan/checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md`。
- [ ] 仍待：P9 B3 唤醒词（`KeywordSpotter` 本机可用）、B4 声纹、自动收音接主程序、AEC/全双工；未提交、未 push。

## 2026-10-01：右下角「正在对话」状态提示窗（零依赖 ctypes）

- [x] 用户要求右下角提示"正在和语音助手对话"。先做 GitHub/PyPI 调研（winotify / win11toast / PyQt-Frameless-Window / tkinter-widgets / pywin32 / PyWinCtl），均不满足"常驻 + 可拖动 + 零依赖"；本机三个运行时都**没有 tkinter**，故选 **ctypes 自绘 Win32 置顶小窗**（与仓库 `waveout_pcm.py` 手写 Win32 结构风格一致）。
- [x] 新增 `app/ui/voice_hud.py`（9 态、固定中文文案、可注入 api、真实 user32/gdi32 实现；不显示识别正文）+ `tests/foundation/test_voice_hud.py` 54 项（离线）。**（同日 UI 批次 2 修订：可见文案收敛为 4 态、测试增至 55 项，见下节）**
- [x] `tools/p9_auto_voice_runner.py` 接线：默认显示 HUD，`--no-hud` 可关；监听→听到了→识别中→思考中→播报中 逐段刷新；HUD 故障隔离，不影响对话。**（"逐段刷新"在 `main()` 路径上实际从未生效——`hud` 被覆盖成 `None`；2026-10-01 UI 批次 2 已修，见下节）**
- [x] 真机验证：窗口位于 1920×1080 右下角、可见、状态逐段变化、**不抢焦点**；**用户确认看到小窗**；同轮主路线播报正常且用户确认听到。
- [x] 全量 **1801 tests OK (skipped=3)**、`compileall` exit 0；交接 `implementation_plan/checkpoints/2026-10-01-P9-batch1-2-auto-capture-stop-handoff.md` §5.4。**（UI 批次 2 后为 1808）**
- [ ] 未做：`app/main.py` 未接 HUD；未叠加系统 toast；未提交、未 push。

## 2026-10-01（追加）：提示窗文案收敛为 4 态 + 两处真机缺陷修复（UI 批次 2）

- [x] 需求：用户要求"简化弹窗，状态只分为待机/聆听/播报"，并真机反馈"全程都是待机"。
- [x] `app/ui/voice_hud.py`：`STATE_LABELS` 由 9 项收敛为 **4 种文案**（待机：`IDLE`/`STOPPED`/`DEGRADED`；聆听：`LISTENING`/`HEARING`/`TRANSCRIBING`/`THINKING`；播报：`SPEAKING`；失败：`FAILED`）；`hud_text()` 不再渲染「第 N 轮」；`VOICE_HUD_SCHEMA_VERSION` 1 → 2；内部 9 态枚举完整保留（便于日后加阶段时间线）。
- [x] **根因定位（用户可见缺陷）**：`tools/p9_auto_voice_runner.py::main()` 在建窗后又写了一次 `hud: Any | None = None`，把 HUD **覆盖成 `None`** → 窗口恒为「待机」、`_wrap_loop_for_hud` 阶段钩子未挂、退出时也不再隐藏/销毁。这就是"全程待机"，也是更早"状态不同步"的主因；删除该行修复。
- [x] 第三处缺陷：`speak_with_hud` 把 `_speak` 返回的 `(False, "text_only")`（本轮没有播报通道，如 `--tts-mode none`）当成播报失败 → 成功的一轮会闪「失败」；已改为只有真实 `speech_failed` 才置「失败」（新增 `TEXT_ONLY_REASON` / `_speech_result()`）。
- [x] 测试（先红后绿）：`tests/foundation/test_voice_hud.py` **55 项**；`tests/integration/test_p9_auto_runner.py` 新增 `RecordingHud` + `HudWiringTests`（2 项，断言 `main()` 把状态序列送到窗口）+ `SpeechOutcomeLabelTests`（4 项，含"真实失败仍显示失败"负路径），`_paced_run_turn` 支持转发 `hud`。修复前 `HudWiringTests` 报 `'listening' not found in ['idle']`。
- [x] 证据：注入记录器实测窗口收到的状态序列 `idle → listening → hearing → transcribing → thinking → speaking → listening → stopped`（修复前仅 `['idle']`）。
- [x] 全量 **1808 tests OK (skipped=3)**、`compileall -q app tests tools` exit 0；文档已同步（本文件、`progress.md`、`NEXT-SESSION.md`、HUD 交接 §1.5/§2、批次 1-2 交接 §5.4）。
- [ ] 未做：真机肉眼复验 4 态（命令见 HUD 交接 §5，需用户执行）；阶段级时间线打点；`app/main.py` 仍未接 HUD；本节改动仍未提交、未 push。

## 2026-10-01（追加）：P9 修复批次 3-4 + B3 唤醒词门禁 + B4 声纹过滤（真机验收通过）

- [x] **修复批次 3（收音「最后一个字被吞」）**：`app/audio/vad/silero.py` 暴露 `segment_pending()`/`flush_segment()`（`empty()` 取反 + 探针错误计数）；`app/voice_loop/auto_driver.py` 新增段级结束判定（先于帧级静音）+ 尾段定稿（`_flush_tail_segment`，在协调器释放前）；runner 新增 `--vad-min-silence-ms` 与结束判定时间线/HUD 阶段打点。真实语音证据：1.98s 样例在 0.42s 静音后结束（帧 75，`end_source=segment`），帧级需 2.91s（帧 153）。
- [x] **修复批次 4（收尾三处）**：`PushToTalkCoordinator.release()` 先排空总线再解码；输入停滞看门狗（真机 23s 空转 → 0.125s 判定 `input_stalled`）；`app/ui/voice_status.py`（提示窗桥接 + 阶段钩子，绝不抛异常）；`app/main.py --voice` 接提示窗（`--no-hud` 可关）。
- [x] **B3 唤醒词门禁**：新增 `app/audio/kws.py`（`KeywordGate`，固定错误码 `model_missing`/`keywords_missing`/`dependency_missing`/`init_failed`/`detect_failed`/`unsupported_*`）+ runner `--kws-mode gate/--kws-model-dir/--kws-keywords-file/--keyword/--kws-threshold/--kws-wait-seconds`。模型 `models/kws`（中文 wenetspeech KWS，归档 `sha256=B2F7C896…F35F`，选 `epoch-12-avg-2` int8；关键词「小饼」= `x iǎo b ǐng @小饼`，**无 BOM**，由模型自带 tokens 实证生成）。**唤醒音频不进 ASR**；等待超时不开启采集、不提交。
- [x] **B4 声纹误触发过滤**：新增 `app/audio/voiceprint.py`（`VoiceprintFilter`）+ `tools/p9_voiceprint_enroll.py`（只写嵌入向量、不写音频、`--out` 不给不落盘）+ runner `--voiceprint-mode filter/--voiceprint-model/--voiceprint-profile/--voiceprint-threshold/--voiceprint-seconds`。模型 `models/voiceprint/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`（`sha256=F682B514…FD11`，官方 tag `speaker-recongition-models`）；档案 `data/voiceprint-owner.json`（192 维，`models/`+`data/` 均 gitignore）。**过滤前置于 ASR**：被丢弃的轮次连解码都不跑。
- [x] **真机验收（用户本人执行）**：B3 两轮 `frames=176/178`、`end_source=segment`、`probe_errors=0`、识别正文**不含唤醒词**、`user_heard=yes`；B4 正例 3/3 匹配并播报（`verified_count=3`），负例 3/3 `end_reason=voiceprint_rejected`（`score=0.2016/0.1749/-0.0678`、`asr_text_length=0`、`user_heard=no`）。artifacts：`checkpoints/artifacts/2026-09-30-P9-auto-voice-loop/` 下 3 份。
- [x] **验收中发现并当天修复的真机缺陷**：上一轮 Silero「已结束段」残留在队列 → 下一轮第 1 帧即被判收尾（`frames=2`、识别 `'。'`，**却仍提交并播报**）→ 每轮重置 VAD（`_reset_speech_detector()`）+ 驱动 `min_utterance_ms=300`（`--min-utterance-ms`，不绕过硬上限）+ 门禁/声纹提示可见化（含"声纹丢弃 N"对症汇总）；另修重复轮号显示。
- [x] **测试**：全量 **1978 tests OK (skipped=3)**、`compileall -q app tests tools` exit 0；新增真实模型测试（KWS 3 项、声纹 2 项，模型存在时运行；模型缺失自动跳过）。
- [x] **文档**：`checkpoints/2026-10-01-P9-hud-and-capture-issues-handoff.md`（§3.3-§3.7、§4、§5.1 配置基线）、`checkpoints/2026-10-01-P9-B3-B4-wakeword-voiceprint-handoff.md`（新建，含模型来源/哈希/真机验收）、本文件、`progress.md`、`NEXT-SESSION.md`。
- [ ] 未做：自动收音接入 `app/main.py` 默认运行时（需单独授权）；`--kws-mode gate` + `--voiceprint-mode filter` 同时联调；ResultGate「纯标点/空正文不提交」兜底；AEC/全双工（P10）；**全部改动未提交、未 push**。
- [x] **后续状态（2026-10-01）**：本节及此前全部 P9 改动已提交并 push（`codex/review-playback-lifecycle`，`f9b0649` 为最新），详见 `NEXT-SESSION.md` §5。

## 2026-10-01（追加）：P9 UI 视觉改版（方案 4 卡片胶囊版）+ 逐像素分层窗口

- [x] 用户逐项确认设计：目标是「更好看」；浅色白底；卡片维持 320×96；左侧状态色条；**要真正的柔和外阴影**；保留标题；视觉方案选定**方案 4（标题坐在状态色胶囊上）**；实现走**逐像素离屏渲染**。
- [x] 设计文档：`implementation_plan/2026-10-01-hud-visual-refresh-design.md`（外观规格、纯函数边界、Win32 侧约束、契约与回滚、验收口径）。
- [x] 新增 `app/ui/hud_render.py`（纯像素渲染核心，零第三方依赖 / 零 Win32）：`render_card_pixels()` 输出预乘 BGRA（解析签名距离 + 4× 超采样的圆角抗锯齿、盒式模糊外阴影、左侧 4px 状态色胶囊条、标题胶囊底）；`paint_layer_masks()` 输出五级图层（透明/卡片/色条/胶囊/阴影）；`HudRenderSpec` / `HudTheme` 承载全部外观参数；首帧 84ms、缓存后 12ms。
- [x] `app/ui/voice_hud.py`：窗口 320×96 → **336×112**（四周 8px 阴影留白），`screen_position_for` 新增 `pad`（默认 0，旧行为不变）→ **卡片可见位置仍右下内缩 24px**；渲染改走 `CreateDIBSection` + 内存 DC + `DrawTextW` + `UpdateLayeredWindow(ULW_ALPHA)`；能力缺失/首帧失败**一次性降级**为旧纯色卡片（`render_mode` 可观测，不抛错）；`VOICE_HUD_SCHEMA_VERSION` 2 → 3；`VoiceHudConfig` 新增 `shadow_margin_px`/`corner_radius_px`/`border_rgb`/`title_font_px`/`body_font_px`/`state_colors` 并改浅色默认值。
- [x] **真机 6 个缺陷全部定位并修复**（详见交接 §4）：能力探测早于建窗导致永久降级；`_BitmapInfo(header, colors)` 构造非法；`SetLayeredWindowAttributes` 与 `UpdateLayeredWindow` 互斥（GetLastError=87）；配置默认仍是旧深色；`_draw_frame_text` 误读 api 实例的 `_body`（恒空）导致正文不显示；窗外像素写成不透明黑 + 卡片抗锯齿 alpha 被当 0。另修 `pptDst` 传 `(0,0)` 会把窗口挪到屏幕左上角。
- [x] GDI 文字与预乘 alpha：文字矩形内先置 alpha=255 → GDI 正常抗锯齿绘制 → `GdiFlush` → 按帧前快照把字形像素 alpha 补回 255（`_snapshot_rects` / `_restore_text_alpha`）。
- [x] 测试：新增 `tests/foundation/test_hud_render.py`（48 项）+ `tests/foundation/test_voice_hud.py::PixelModeTests`（8 项）；更新坐标/配色/契约期望；全量 **2034 tests OK (skipped=3)**、`compileall -q app tests tools` exit 0。
- [x] **真机验收（用户本人执行）**：`python -m app.main --voice` 看到白底圆角卡片 + 阴影 + 状态色条 + 状态色正文，用户回复「没问题」；本机自证 `render_mode=pixel`、窗口 `(1552,936)`–`(1888,1048)`。
- [x] 交接：`implementation_plan/checkpoints/2026-10-01-P9-hud-visual-refresh-handoff.md`；`.gitignore` 新增 `/data/`（声纹档案）与效果图目录（设计过程素材不入库）。
- [ ] 仍待：提交本批改动；自动收音接主程序；多轮/流式/限流；圆角处约 2px 透明小瑕疵（可忽略，彻底消除需离屏预排版文字）。

## 2026-10-01（追加）：免按键自动收音接入主程序（`--voice --auto`）

- [x] 用户授权按 A 方案实施：先抽取共享会话层，再在主程序接线（每批单独汇报）。
- [x] **A1 抽取**：新增 `app/runtime/auto_voice.py`（约 1070 行，中性共享层：`AutoVoiceConfig` / `AutoVoiceSession` / `AutoTurnFacts` / 阈值校准 / 唤醒词等待 / 声纹档案 / HUD 小工具 / fail-closed 错误族）；`tools/p9_auto_voice_runner.py` 由 2124 行降到 1256 行，改为导入共享层，保留 runner 专属的配置子类、`ScriptedAgent`、HUD 时间线、artifact、CLI 与 `_safe_print`。
- [x] **A2 接线**：`app/main.py` 新增 `_run_auto_voice()`（装配 + 轮次循环 + HUD 状态联动 + `/text` `/exit` + 异常降级）、`_keyboard_interrupt_source()`（Windows `msvcrt` 有键才读，不阻塞收音）、`_voice_entry_config_for_auto()`、`_write_line()`；`run()` 新增 5 个注入参数；CLI 新增 `--auto`、`--max-record-seconds`、`--min-utterance-ms`、`--vad-mode`、`--vad-model`。
- [x] 边界：不带 `--auto` 时**完全跳过**自动路径（按键模式逐字节不变）；自动收音只决定输入边界，不改变授权/计划/执行语义；装配失败（设备/模型/依赖/ASR 启动）一律**如实提示并降级为文字**；轮次异常先 `session.close()` + HUD `STOPPED` + 发事件再降级；`log_details()` 只出计数/原因码与文本长度，识别正文不进日志。
- [x] 测试：新增 `tests/foundation/test_main_auto_voice.py` 21 项（CLI 折叠 / 关闭时零改动 / 真实环境装配走 `build_real_environment`+preflight / 注入会话 / `/text` `/exit` / 失败降级 / Ctrl+C / HUD 状态序列 / 配置校验）；runner 专项 88 项保持全绿。
- [x] 回归：全量 **2055 tests OK (skipped=3)**（2034 → +21）、`compileall -q app tests tools` exit 0。
- [x] 交接：`implementation_plan/checkpoints/2026-10-01-P9-auto-capture-main-integration-handoff.md`（含抽取时踩到的 5 类问题与真机命令）。
- [x] 真机试听（用户本人执行，2026-10-01）：**通过**。用户运行主程序免按键路径后确认可用；本轮只记录「用户确认可用」，不代填识别正文/延迟/实际 `--vad-mode`。
- [x] **CLI 开关接入（同日追加）**：主程序新增 `--kws-mode gate` / `--kws-model-dir` / `--kws-keywords-file` / `--keyword` / `--kws-threshold` / `--kws-wait-seconds` 与 `--voiceprint-mode filter` / `--voiceprint-model` / `--voiceprint-profile` / `--voiceprint-threshold`（默认全关，行为不变）；显式要求但模型/档案缺失时**如实报错并降级为文字**，绝不静默退化；新增 5 项测试，全量 **2060 tests OK (skipped=3)**。
- [x] **本机 TTS 按需自动拉起（同日追加）**：VoiceStudio 没开时主程序会打印 `provider profile verification failed` 并降级——新增 `app/runtime/tts_service.py`（只读探测 `/profiles` → 需要时按配置命令后台拉起 → 轮询到就绪/超时），`--tts-autostart on|off|require`、`--tts-start-timeout`、`--tts-start-command`；命令来自环境变量 `ASSISTANT_TTS_PYTHON`/`ASSISTANT_TTS_BACKEND`/`ASSISTANT_TTS_CWD`（**不写死路径**）；只关自己拉起的进程，用户自己开的服务绝不动；起不来不抛异常只如实降级。新增 24 项测试。**真机实测**：`已自动启动本机 TTS 服务（pid=468，用时 53.3s）`，`/profiles` 45s 后 200；三个环境变量已写入用户级配置。
- [x] **P9 阶段级集成验收（2026-10-01）**：路线图五条退出条件（VAD 只决定输入边界 / 唤醒词只开会话 / 声纹只做过滤不替代 L2-L3 / 连续对话边界清晰 / 误唤醒不触发工具）**逐条判定通过**；证据 `implementation_plan/checkpoints/2026-10-01-P9-stage-acceptance.md`；验证命令与结果：全量 **2084 tests OK (skipped=3)**、`tests/dialog` 56、`tests/audio` 298、`tests/voice_loop` 113、`tests/integration` P9 集成 173、UI/入口/服务专项 176、`compileall` exit 0。
- [x] **P9 标记 complete**：`implementation_plan/implementation-roadmap.md` P9 卡片状态改为 complete；真机逐项确认记录（含"用户只确认可用、未提供控制台数值"的边界）见验收文档 §5。
- [ ] 下一步：P10（打断/AEC/设备恢复与长期运行）或阶段 4 整体验证，需用户选择与授权。

## 2026-10-02：P0–P9 整体测试与验证（阶段 4 / 阶段 5）

- [x] 用户授权：离线全量复核（不联网、不读密钥、不动音频设备）+ 新建阶段验收文档 + 同步状态文档；允许并行只读审计。
- [x] **全量回归实测（HEAD `a34af54`，工作区 clean）**：`unittest discover -s tests -t . -p "test_*.py" -q` → **Ran 2084 tests in 114.055s, OK (skipped=3)**，exit 0；专项实测 `tests/dialog` 56、`tests/audio` 298、`tests/voice_loop` 113、`tests/integration -p "test_p9*.py"` 173、UI/入口/TTS 服务 176，全部 OK；`compileall -q app tests tools` exit 0。静态点算与运行时收集双向核对一致（collected=2084 / ran=2084 / skipped=3 / failures=0）。
- [x] **skipped=3 首次查明并登记**：1 项本机不允许建目录符号链接（`tests.tts.test_edge_sapi`）+ 2 项真实 TTS 设备证据需 `P7_RUN_REAL_TTS_EVIDENCE=1`（`tests.tts.test_real_failure_matrix_live`）。
- [x] **编码扫描（全仓）**：`app`/`tests`/`tools` 共 294 个 .py，0 解码失败、0 `U+FFFD`、26 个带 UTF-8 BOM；与基线提交 `fdb0ce1`（25 个）逐 blob 对比，新增 1 个 = `tests/foundation/test_voice_hud.py`（P9 期间新增，违反接口冻结"新增文件必须严格 UTF-8 无 BOM"）。
- [x] **五条退出条件代码侧独立取证**：VAD 边界不含授权事实、`kws_mode` 默认 `off` 且 gate 前置于采集、声纹校验在 `complete_turn()` **之前**（拒绝即不提交不播报）、连续对话边界有 56+113 项覆盖、AST 审计 `safety_refs=0 / dangerous_calls=0`。
- [x] **交付物完整性**：P9 交付物 21/21 存在；P0–P8 抽样齐全；artifact 22 子目录 / 128 文件（119 入库，含 2 份 `p9-user-heard-*`）；`models/`(282MB) 与 `data/voiceprint-owner.json` 齐备且正确 ignore。
- [x] **文档一致性差异（14 条）如实登记**：`NEXT-SESSION.md` 3 处过期（2060 基线、"待提交/待真机试听"、"CLI 开关未接"）、`task_plan.md` 2 处（commit 写 `dccb377`、阶段 3 停在 P5）、roadmap 2 处（2055 未标批次、设备 epoch 口径）、P9 验收文档 3 处（`wakeword_timeout` 拼写、`VOLC_TTS_*` 口径、2px 无证据）+ 缺 `P9-handoff.md`、录音不入库、P9 新增 BOM 文件；均已在本轮文档中更正或标注。
- [x] **新发现缺陷（非阻断，已登记待授权）**：`tests/conversation/test_manager.py:45/78/97` 三个 `test_*` 定义落在 `@dataclass FakeAdapter`（非 `unittest.TestCase`）内部，**永不被收集**（该文件静态 22 / 实际 19），即静态 2087 与 `Ran 2084` 相差 3 的全部原因；三者覆盖路由先于 chat agent、同 `turn_id` 幂等不可替换、ASR 终稿同路由契约——修复后预计需按 `TestCase` 语义改写断言。
- [x] 证据与文档：新建 `implementation_plan/checkpoints/2026-10-02-P0-P9-stage-acceptance.md`；同步本文件、`progress.md`、`NEXT-SESSION.md`、`implementation_plan/implementation-roadmap.md`，并在 P9 验收文档追加「事后修订记录」。
- [x] **阶段 4 与阶段 5 判定完成**（见本文档 §阶段 4/§阶段 5）；本轮未改代码、未提交、未 push。
- [ ] 仍待授权：P10（打断/AEC/设备恢复与长期运行）；3 项低优先收尾（孤儿测试修复 / `test_voice_hud.py` 去 BOM / 补 `P9-handoff.md`）。

## 2026-10-02：P10 批次 R1a + R1b + R2a（两阶段打断、AEC 降级框架）

- [x] 用户授权：本轮 P10 先做 **R1a+R1b（离线两阶段打断）**；AEC 先做 **R2a 降级框架**（真实选型/真机实测后置）；离线批次由 Lead 串行实施。
- [x] **R1a 两阶段打断状态机**（新增 `app/duplex/barge_in.py` + `app/duplex/__init__.py` + `tests/duplex/`）：冻结 `BargeInConfig`（默认值落在设计区间：≤100 ms 降音量、20~30% 音量、≥300 ms 前置、200~300 ms 确认窗口）、`idle/suspected/confirmed` 三态、`observe/release/reset/take_pre_roll`、降级只提高门槛（`full/no_aec/strict`）、缺参考自动降级、纯离线边界（不导入设备/音频库/安全链）。**56 项专项**；交接 `checkpoints/2026-10-02-P10-R1a-barge-in-state-handoff.md`。
- [x] **R1b 编排语义**（新增 `app/duplex/barge_in_orchestration.py` + `tests/duplex/test_barge_in_orchestration.py`）：疑似降音量、误触发按 `restore_ms` **平滑**回升（10 帧阶梯）、`release_ms=400` 滞后保护、确认后音量归零且**旧回复永不再续提交**、前置缓冲并入同一 `utterance_id`、安全确认提示被打断即失效（`PromptPolicy`，插话不产生授权）。**40 项专项**；交接 `checkpoints/2026-10-02-P10-R1b-barge-in-orchestration-handoff.md`。
- [x] **R2a AEC/降噪逐层降级框架**（新增 `app/audio/aec/__init__.py` + `tests/duplex/test_aec_degradation.py`）：`aec_ns → aec_only → strict_no_aec → half_duplex` 阶梯、7 个固定原因码、不伪装已消除回声、任何级别保留文字模式且不关闭会话、延迟估计不可靠是 AEC 前置条件、设备变化须重校准（消费 `device_epoch`）。**34 项专项**；交接 `checkpoints/2026-10-02-P10-R2a-aec-degradation-handoff.md`。
- [x] **验证**：`tests/duplex` **130 项 OK**（56+40+34）；全量 **2217 tests OK (skipped=3)**（2087 → 2217，112.603s）；`compileall -q app tests tools` exit 0；8 个新增/修改文件严格 UTF-8 无 BOM。
- [x] 开发期被测试抓到的 5 个真实缺陷（已修，见各交接 §5）：`voice_probability=0.0` 误拒；疑似态永不回落；恢复阶梯依赖状态机 level；动作码被音量覆盖；AEC 降级顺序（延迟不可靠须先于全链判定）。
- [x] **R2b-1 AEC 参考时间线 + NLMS 回声消除**（用户选型方案 1：WASAPI 回环参考 + 自实现 NLMS，零新依赖）：新增 `app/audio/aec/reference.py`（参考帧有界时间线、设备延迟对齐、`ok/history_short/future/too_old` 四态）、`app/audio/aec/nlms.py`（逐样本 NLMS + 处理链、ERLE、回退原因）、`tests/duplex/test_aec_nlms.py`（**39 项**）。实测：合成回声尾部中位 **ERLE ≈ 48 dB**、累计 ≈ 38 dB，权重精确收敛到真实抽头（delay 3→0.5、delay 11→0.3）。**块式梯度实测不收敛，已明确否决并留痕**；静音帧不再伪造 ∞ 抑制量。交接 `checkpoints/2026-10-02-P10-R2b-1-aec-offline-handoff.md`。
- [x] **验证（R2b-1 后）**：`tests/duplex` **169 项 OK**（56+40+34+39）；全量 **2256 tests OK (skipped=3)**（2217 → 2256，115.498s）；`compileall` exit 0；新文件严格 UTF-8 无 BOM。
- [x] **R2b-2 真机实测（2026-10-02，用户在场）**：新增 `tools/p10_aec_probe.py`（`--live-opt-in` 门禁、`--tones`/`--loopback-only`/`--save-audio` 诊断）、`tools/p10_wasapi_loopback_check.py`（端点配对/`--watch`/`--probe`）、`tools/p10_aec_probe_selfcheck.py`（六种合成场景）、`tools/p10_aec_wav_diagnose.py`（只读 WAV 诊断）。**结论：WASAPI 回环参考在本机不可用**——系统音乐能被回环抓到（watch：rms 0.18、active 0.955），但本程序播放抓不到（WASAPI 直放同一端点、录其回环，对齐 −0.06；多音扫描 700 Hz 以上 9 个频点能量比全 0.000）；已排除削顶（0 削顶）、效果链（增益 −5.4…+4.9 dB 中等不平坦）、时钟漂移（参考改多段突发仍无改善）、算法/参考设计（合成自检 6/6 通过）。**根因未查明**，如实登记。
- [x] **方案 A（用户批准）**：AEC 参考改取"实际提交给设备的 PCM"（同进程旁路）。新增 `app/audio/aec/reference_source.py`（`SameProcessAecReference`：`submit`/`window`/`process`/`timeline_frames`/`note_device_epoch`/`as_submit_observer`）；`app/tts/waveout_pcm.py` 加**只读** `submit_observer` 钩子（仅成功后回调、锁外、字节原样、异常吞掉不影响播放）；`app/audio/aec/reference.py` 加 `frames()` 快照；`tests/duplex/test_aec_reference_source.py`（**43 项**）。实测约束（已写成测试）：真实回声延迟须 **≥1 样本**（零延迟 ERLE≈−1 dB 且权重发散；1/2/3 样本延迟稳定 **41.3 dB** 并精确命中增益 0.500）——真实链路必然有延迟，不影响使用。
- [x] **验证（R2b-2 后）**：`tests/duplex` **210 项 OK**；方案 A 专项 **45 项 OK**；全量 **2301 tests OK (skipped=3)**（2299 → 2301，119.380s）；`compileall` exit 0；8 个新增/修改文件严格 UTF-8 无 BOM。首次真机跑出 `submit_observer_never_fired`（旁路只收整帧、waveOut 实提 200 ms 大块、异常被静默吞掉）→ 已改为**流式重组 + `last_observer_error` 可观测**，并加 200 ms 大块回归用例；端到端集成自检（假后端转调真实旁路）通过：frames=100、errors=0、对齐 1.0、尾部 ERLE 22.9 dB。交接 `checkpoints/2026-10-02-P10-R2b-2-real-device-handoff.md`。
- [x] **R2b-2 真机最终结论（2026-10-02）**：同进程参考通路**已验证正确**（真机 `submit_calls=30`、`submitted_bytes=288000`、`elapsed=6.031s`、`frames_submitted=300`、`observer_errors=0`）；但**真机 ERLE 无有效数值**——本机为笔记本内置扬声器 + 内置麦克风阵列，用户确认麦克风增强页**没有**「回声消除/噪音抑制/AGC」开关，阵列 DSP 默认生效且不可关闭，麦克风侧不存在可测线性回声路径（`mic_alignment=−0.0767/−0.0525`，与"无回声"自检基准 −0.0382 同级；`mic_level_vs_floor_db` 仅 10.6/9.3 dB）。**需外接麦克风才能验证**；在此之前不下"AEC 真机有效/无效"的结论。真机四轮迭代暴露并修复 3 个工具缺陷（旁路只收整帧、参考容量不足、偏移语义错）+ 1 个采样率不一致缺陷（48 kHz 参考喂给硬编码 24 kHz 的 waveOut → 静默半播）。
- [x] **R3 设备会话与安全空闲点恢复（2026-10-02）**：用户本轮决定——① 输出设备断开时**停止当前播报并回退文字**；② 重连**仅在下一个安全空闲点各试一次**。新增 `app/devices/session.py`（`DeviceRecoveryPolicy`：`register`/`decide`/`note_attempt`/`note_device_switch`；`DeviceHealth` 六态；固定短代码提示）+ `app/devices/__init__.py` + `tests/devices/test_device_recovery.py`（**34 项**）。落实三条硬要求：**恢复只在安全空闲点**（轮次中/服务中/等待确认一律延后）、**不自动切未知设备**（新设备只登记不采用）、**设备切换只在下一轮生效**（epoch 推进）；输出优先于输入；忙/权限拒绝/不兼容**不自动重试**；任何情况**保留文字模式**且提示不静默、不重复刷屏。纯新增、零生产路径改动。
- [x] **验证（R3 后）**：`tests/devices` **34 项 OK**；全量 **2335 tests OK (skipped=3)**（2301 → 2335，118.559s）；`compileall` exit 0；4 个新文件严格 UTF-8 无 BOM。交接 `checkpoints/2026-10-02-P10-R3-device-recovery-handoff.md`。
- [ ] 待授权：**R2b 真机验证（需外接麦克风）**、**R5**（Provider 冷却）、**R6**（长期运行健康）。

## 2026-10-02：P10 批次 R4（会话循环接线：独立打断通道 + 安全门 + 设备策略 + R2b 参考）

- [x] 用户授权：**完整 A→G**（含 `waveout` 可选增益钩子与 CLI 开关）+ 真机试听（先 `--check-only`）。硬要求：打断走**独立打断通道 + 安全门**，**不复用** `interrupt_source`；默认全关。
- [x] **R4a 独立打断通道**（新增 `app/duplex/barge_in_channel.py`，567 行）：`BargeInChannel` 自有 `AudioBus` 身份/流（`p10-barge-in-channel`）+ 16 kHz/20 ms/640 B 采集 + 前置 PCM 环形缓冲；逐帧构造 `BargeInObservation`（silero 命中=1.0 / 能量=rms÷阈值，不伪造概率；参考相关性只在**同钟且窗口 ok** 时给出）；采集装不上/总线失效/超时/检测器抛错一律"不打断 + 固定原因码"；降级只通过替换状态机**提高**门槛。**41 项专项**。
- [x] **R4b 安全门**（新增 `app/duplex/barge_in_safety.py`，141 行）：`BargeInSafetyGate.decide()` → `handoff` / `stop_and_replay` / `block`；**`authorization_possible` 恒 False**（构造时传 True 直接契约错误）；安全确认提示/等待确认被插话 → 停播 + 提示失效 + 必须完整重播 + **不**建立可用轮次；半双工/通道不可用/无播报 → 不打断。**17 项专项**。
- [x] **R4c 会话循环接线**（改 `app/runtime/auto_voice.py`，1084 → 1578 行）：新增 `barge_in_mode=off|duck|confirm`、`barge_in_config`、`barge_in_max_monitor_seconds`、`aec_reference_mode`、`device_recovery_mode`（默认全关，`off` 时**零新增对象**）；包装 `environment.speaker`（`push_text_chunk` 先起监视再原样透传，**不改变首字延迟**；`finish`/`cancel` 后 `finally` 收尾监视），确认时 `stream.cancel()`（停播 + 清队列 + 旧回复不再续提交）；`duck` 模式确认不生效并记 `confirm_suppressed_in_duck_mode`；只读事实 `barge_in_details()`/`take_pending_pre_roll()`/`set_awaiting_confirmation()`；`AutoTurnFacts` 增 `barge_in`/`pre_roll`/`device`。
- [x] **R4d 设备策略接线**：安全空闲点（每轮开始前）`decide()`；观察来源如实（采集真正启动→INPUT READY、`start_failed`/`input_stalled`→INPUT DISCONNECTED、播报成功→OUTPUT READY、`speech_failed`→OUTPUT DISCONNECTED）；未知设备不采用；重连**按 disconnect episode 各试一次**（探测有界且收尾）；`busy`/`permission_denied`/`incompatible` 不自动重试；**输出无固定设备目标（waveOut 走 `WAVE_MAPPER`）→ 按用户决定停止播报并回退文字**。
- [x] **R4e R2b 参考 + 降音量落地**（改 `app/runtime/voice_entry.py`、`app/tts/waveout_pcm.py`、`app/main.py`）：`aec_reference_mode=same_process` 时把 `SameProcessAecReference`（**24 kHz，与 waveout 同钟**）作 `submit_observer` 挂上后端（旁路取**实际提交给设备**的 PCM，含增益），并建 `PlaybackGain` 共享给后端；`WaveOutPCMStreamBackend` 增**可选** `gain_provider`（默认 `None` → 提交字节级不变；诊断 `scaled_submissions`/`min_gain_applied`）；CLI 增三个开关。**16 kHz 采集 vs 24 kHz 播放不同钟 → 不给相关性、按 R2a 退 `strict_no_aec`，绝不声称已消除回声**；降音量**粒度＝后端缓冲块 200 ms**（`duck_quantum_ms` 已登记，不是"≤100 ms"承诺）。
- [x] **验证（R4 后）**：专项 **169（duplex barge-in 系列）+55（会话接线）+13（增益）= 237 项 OK**；全量 **2476 tests OK (skipped=3)**（2335 → 2446（首次）→ 2449（参考修复）→ 2463（音量跳动）→ 2471（窗口粘滞+能量门）→ 2476（假确认 fail-closed））；`compileall -q app tests tools` exit 0；321 个 .py 编码扫描 0 解码失败 / BOM 25（与基线一致）/ 新增修改文件无 BOM。**check-only 级真机核查**：`--help` 解析三个新开关 + 真实 `build_real_environment(tts_mode=none, aec_reference_mode=same_process)` 成功装配（**抓到并修掉真实缺陷**：参考按默认 48 kHz 装配与 waveOut 硬编码 24 kHz 冲突 → `sample_rate_mismatch`）。交接 `checkpoints/2026-10-02-P10-R4-session-loop-handoff.md`（§7 真机、§9 缺陷修复）。
- [x] **R4 真机验证（2026-10-02 晚，两轮；暴露并修复 1 个真实缺陷）**：
  - 第一轮（默认 local 路由）：入口/TTS 自动拉起/麦克风/HUD/ASR 全部正常，但**每轮 `text_only`/`spoken=false`** ⇒ 无播报窗口（`UnavailableAgentAdapter` → `speech_eligible=False`），打断不触发也不报错——**必须记住的边界**；
  - 第二轮（用户授权开真实 DeepSeek，计费）：**独立通道真机确认工作**（`barge_in_enabled=true` 4 个播报回合，通道帧数 208/532/116/1485，收尾 `playback_ended`；输出设备转 READY；全程 0 次假确认）；
  - **用户报"怎么音量一直在变"→ 真实缺陷**：一帧达 0.6 即降音量（0.25）× 无法折减回声 × 确认需连续 13 帧 ⇒ **只降不认**、`release_ms=400` 一到就回升；
  - **修复（已落地，三轮）**：① 音量跳动：R1a 增 `min_suspect_frames`（默认 1；会话层 3＝60 ms）、通道增 `duck_requires_reference`（默认 True：无可用参考**不降音量**）、会话层 `release_ms` 400→800；② **确认结构性不可达**：通道检测器缺"窗口粘滞"且缺主链路能量门 → 新增 `_sticky_window_observe()` + 检测器模式改**双重条件**（命中**且** RMS ≥ 能量门）；③ **假确认（用户没插话也停播，6/6 回合）**：本机扬声器自己的声音越过能量门而拿不到可用参考 ⇒ 无法区分 ⇒ 按设计文档 12 与"不猜"原则 **fail-closed**：参考不可用时**整个打断不启用**（不建通道/不包 speaker/播放期不守望），`barge_in_details().unavailable_reason="no_usable_echo_reference"`，`main._report_barge_in_status()` 启动即打印原因；
  - **回归 +27 项**（14 + 8 + 5）：全量 **2449 → 2463 → 2471 → 2476 tests OK (skipped=3)**；`compileall` exit 0；321 个 .py 编码扫描 0 失败/BOM 25。
  - **本机最终口径**：内置麦克风阵列 + 内置扬声器在**外接麦克风或可关闭 DSP 的 AEC 参考可用前不具备安全打断条件**；`duck|confirm` 本机等价于不启用（打印原因）。R4 **机制**已真机端到端验证，缺的是可信判别依据。详见交接 §7/§9/§9b/§9c。
  - **开源调研（已入档）** `implementation_plan/research/2026-10-02-realtime-voice-barge-in-open-source-research.md`：豆包为闭源云服务；可参考 Moshi/Unmute（Kyutai）、Pipecat、LiveKit Agents（自适应打断 + 边界冷却）、TEN Framework；建议顺序：**参考重采样让 `echo_correlation` 可用 → WebRTC APM/SpeexDSP 真 AEC → 冷却窗口兜底 → 打断判别器**。
- [x] **批次 R4-A（用户授权"方案 A"）**：把"判别依据"找回来。
  - 新增 `app/audio/aec/alignment.py`：`estimate_echo_alignment()` 粗搜+细搜；合成回声 40/140/300 ms 延迟全部命中（corr 0.51/0.91/0.97），无回声 0.04；**最小重叠门槛**挡住退化假峰。
  - `reference_source.py`：`source_sample_rate` **跨块连续重采样**（1 s 24 kHz → 16000 样本 @16 kHz；大块与逐帧提交时间线一致；同钟时字节级不变）。
  - 通道：`echo_correlation` 改为**对齐后**相关性 + `echo_delay_ms`/`alignment_state`/`echo_proven`；每 0.5 s 一估。
  - 会话：**回声可观测性安全门**（`barge_in_echo_proof_correlation=0.2`）——没证明"看得见自己的回声"就只观测不动手（`barge_in_unproven_echo`）；**诚实性修正**：会话不跑回声消除 → 能力报 `CAPABILITY_UNAVAILABLE`、R2a 给 `strict_no_aec`/`aec_applied=False`（不伪装已消除回声）。
  - **顺带修掉两个真实缺陷**：① 未显式给配置时用裸 `BargeInConfig()` 把 `min_suspect_frames=3`/`release_ms=800` 覆盖回 1/400（第三缺陷在会话路径此前未生效）；② 通道 `duck_suppressed` 判定过宽。
  - **验证**：+16 项（含**重采样参考经 NLMS 尾部 ERLE 中位数 > 15 dB**）；全量 **2476 → 2496 tests OK (skipped=3)**、`compileall` exit 0、323 个 .py 编码扫描 0 失败/BOM 25。交接 §10。**真机 A4（实测回声可观测性与真打断）待跑**。
- [x] **批次 R4-B 前置（2026-10-03，只读核查；未安装依赖、未改代码）**：真 AEC 前端可行性核查入档 `implementation_plan/research/2026-10-03-P10-R4B-aec-frontend-feasibility.md`。**首选 `pywebrtc-audio` 0.2.0**（cp312-win_amd64 wheel、免编译、Apache-2.0、真 AEC3+NS+AGC+VAD；唯一依赖 numpy≥1.24 已满足），**兜底 `pyaec` 1.0.1**（py3-none-win_amd64、无依赖、MIT、speexdsp MDF）；排除 webrtc-audio-processing / webrtc-apm（无 cp312-win wheel）/ speexdsp（sdist+swig）/ speexdsp-ns（仅 linux 且无 AEC）。基线复跑 **2496 OK (skipped=3)**，耗时 **321.9 s**（文档记 82 s，本机约 4 倍）。
- [x] **批次 R4-B-1（2026-10-03，用户批准含装依赖）**：真 AEC 前端（WebRTC APM/**AEC3**）后端接入（**默认关闭**）。
  - 依赖冻结：`pywebrtc-audio==0.2.0` wheel 入 `.wheelhouse/`（513,391 B），SHA-256 `0dabbdadd5d7fd7dcb88323266e5e14d46aaa136c58fa0573c4b0323b683c80b` 与 PyPI 官方 digest 逐字符一致；Apache-2.0（NOTICE 未列 vendored，对外分发需补声明）；唯一运行时依赖 numpy≥1.24（已满足）；离线安装 `pip install --no-index --find-links .wheelhouse pywebrtc-audio==0.2.0`。
  - 新增 `app/audio/aec/webrtc_backend.py`：与 `NlmsEchoCanceller` **同构**接口 + **可选依赖**（缺失/构造失败/调用异常一律如实直通、固定原因码、异常不冒泡）+ 每帧统计 + `suppression_evidence` 分级。
  - `reference_source.py` 增 `backend="nlms"|"webrtc"`（**默认 nlms，既有行为字节级不变**）；`to_details()` 增 `aec_backend`；ERLE 口径差异写进 `process()` 文档（webrtc=采集/残差，nlms=参考/残差，**不可混用**）。
  - 新增 `tests/duplex/test_aec_webrtc_backend.py`（29 项）+ `tools/p10_r4b_aec_bench.py`（离线基准，`--check-continuity`）。
  - **离线实测（本机）**：合成回声累计 ERLE **23.3 dB**（尾部 20.6 dB）；单帧中位 **0.157 ms ≈127× 实时**；20 ms 与 2×10 ms **逐样本等价**。
  - **证伪一个会误判的假设**：参考有能量时，即使**没有回声路径**，AEC3 也产生 **11.7 dB** 抑制（近端无关噪声 5.4→0.4）⇒ "ERLE 达标"**不能**当"回声已消除"的判据 → 新增 `suppression_evidence`（**不含"已确认回声路径"档**）并写成测试；参考帧无能量 → `reference_silent` 直通（不用残余抑制动近端语音）。
  - **验证**：专项 29 项 + AEC 合计 **161 项 OK**；全量 **2496 → 2525 tests OK (skipped=3)**（231.239s，exit 0）；`compileall` exit 0；326 个 .py 编码扫描 0 解码失败 / BOM 25（本批 4 文件无 BOM、纯 LF）。交接 `checkpoints/2026-10-03-P10-R4B-1-aec3-backend-handoff.md`。
  - **未做（不伪造）**：未接线会话（`auto_voice`/`barge_in_channel`/`main` 未改）、**未做真机**（本机 ERLE 未知）、双讲时用户语音是否被抑制**待真机测**、`max_echo_path_ms=120` 与 200 ms 缓冲的预算冲突未解。
- [x] **批次 R4-B-2 阶段 0（2026-10-03，用户批准）**：AEC3 **双讲保持**离线 go/no-go。
  - 工具 `tools/p10_r4b2_doubletalk_probe.py`（离线、免设备；本机真实中文语音 `models/kws/test_wavs/*.wav`；合成回声路径 40 ms+增益+反射+低通；`far_only`/`near_only`/`double_talk` 三组对照；指标=残差对纯净用户语音的最佳时移投影增益，并折算 int16 RMS 对齐能量门 600 / 噪声底 max≈150）。
  - **结果（条件性 no-go）**：回声 ≥ 用户档（`echo_to_speech_db`+7.6/+8.1）双讲增益 **−5.5 ~ −20.5 dB**、残差用户语音 RMS **93–522（5/6 低于 600 门）**；用户 ≫ 回声档（≈8 倍）−1.6 ~ −5.9 dB（1499–2468，高于门）；中间档 −5.1 ~ −12.8 dB。`near_only` 12/12 次 **−0.00 dB**（无参考能量时 AEC3 不碰用户语音）；`far_only` ERLE 15.7–37.9 dB；**分段增益波动 −1.8 ~ −37.7 dB（不稳定）**；NLMS 对照保住语音但 ERLE 仅 8.85 dB。
  - **结论**：双讲衰减随**回声/用户语音比**变化，本机（内置阵列麦+内置扬声器远场）大概率落在坏档 ⇒ **暂停阶段 1（不把 AEC3 接进判定/ASR 路径）**，会话维持 fail-closed；AEC3 后端保留在仓库、默认关闭。证据 `implementation_plan/research/2026-10-03-P10-R4B-2-doubletalk-findings.md`。
  - **未做**：真机（回声/用户比、双讲衰减、ERLE 均未测）；耳机/外接麦场景未测；`pyaec`/speexdsp 双讲未测；探针未加单测（开发工具，不在生产路径）。
- [ ] **R4-B-2 阶段 1（已按阶段 0 结论暂停）**：把 AEC3 接进会话判定/ASR 路径——**需先有真机"回声/用户比 + 双讲衰减"数据**才重新评估；会话级"实测 ERLE 达标才许声称已消除回声"的门随之顺延。
- [x] **方案 C（冷却窗口，2026-10-03，用户选 C1 口径）**：`barge_in_cooldown` 落地。
  - `BargeInConfig.cooldown_ms`（默认 0 = 关闭，非负整数校验）+ `BargeInController` 冷却判定（**IDLE 且播放中**才判：收帧、填前置缓冲、不降音量、清确认窗口；`playback_ended`/`reset()` 清起点 → 新 episode 重新计时）+ `REASON_COOLDOWN="barge_in_cooldown"`；`BargeInDecision` 增 `cooldown_remaining_ms`/`cooldown_frames`（带默认值，向后兼容）。
  - `BargeInChannelConfig.cooldown_ms` 用 `dataclasses.replace` **只覆盖冷却时长**（不重建 BargeInConfig，避免覆盖 `min_suspect_frames=3`/`release_ms=800`）；通道/会话 `to_details()` 与 `barge_in_details()` 如实暴露冷却字段。
  - CLI `--barge-in-cooldown-ms`（默认 0，建议 1000）；启动提示：冷却窗口 + `echo_proven=false` 时提示"本机可能不具备打断条件，建议耳机/外接麦克风"。
  - **C1 口径**：只加冷却，**不改** R4-A 的 fail-closed（没证明回声仍不真停播）。**语义边界**：不丢音频、只影响 IDLE、`cooldown_ms=0` 等价于改动前。
  - **验证**：新专项 **28 项 OK**；打断/会话相关合计 **290 项 OK**；全量 **2525 → 2553 tests OK (skipped=3)**（220.626s，exit 0）；`compileall` exit 0；328 个 .py 编码扫描 0 失败 / BOM 25。交接 `checkpoints/2026-10-03-P10-R4B-2-planC-cooldown-handoff.md`。
  - **未做**：真机（冷却效果无真机数据）；"更严门槛"档与半双工提示 UI；`--aec-engine` 开关（AEC3 仍未接线）；耳机/外接麦场景。
- [x] **批次 R4-B-3/4（2026-10-03，用户批准真机量测；纯测量、未改生产代码）**：真机"回声/用户语音比" + 真机 AEC3 双讲 + 延迟扫描。
  - 新增 `tools/p10_r4b3_echo_ratio_probe.py`（三阶段：噪声底/回声段/用户语音段；生产同款 waveOut 播放本机真实 TTS `eff172cb.wav`）与 `tools/p10_r4b4_realtime_doubletalk_probe.py`（三段录制 + **离线** AEC3 分析，`--echo-only`/`--delay-sweep`）；均需 `--live-opt-in`，**默认不落盘音频**，无计费。
  - **R4-B-3 数据**：噪声底 p50=72；回声 p50=**846**；用户语音 p50=**1917**；**ERL 13.7 dB**；**回声/用户语音比 −7.1 dB**；用户语音 vs 门 **+10.1 dB**、回声 vs 门 **+3.0 dB**。**不可复现**：同日另一次用户 p50=1037（差 5.3 dB）⇒ 比值在 ≈0 ~ −7 dB 浮动。
  - **R4-B-4 数据**：实测延迟 409 ms → **真机 ERLE 5.87 dB**、残差 519、**137/289 帧超门**；双讲后用户语音 1648（**+8.8 dB vs 门**）。**延迟扫描**：392 ms → 10.32 dB；**最佳 350 ms → 15.54 dB、132、80/289 超门**；0–500 ms 内 7–15.5 dB（**延迟敏感**）。
  - **修正结论**：阶段 0 的"条件性 no-go" → **"AEC3 不是不可用，而是不够用"**；真机 vs 合成差 8–15 dB（内置阵列 DSP 破坏线性路径，本次量化）；单靠 AEC3 仍 ~28% 帧超门 ⇒ 需**三件套**（AEC3 + 冷却窗口（已落地）+ **残余地板感知动态门槛**（未做））。
  - **验证**：全量 **2553 tests OK (skipped=3)**（236.909s，exit 0；未改生产代码故条数不变）；`compileall` exit 0。证据 `research/2026-10-03-P10-R4B-real-device-echo-ratio-and-aec3.md`；交接 `checkpoints/2026-10-03-P10-R4B-3-4-real-device-handoff.md`。**真机工具缺陷**（GBK 打印 `≫` 崩溃）已修并离线验证，该次未写出 JSON artifact。
  - **未做**：残余地板门槛、AEC3 接线、三件套真机试听、耳机/外接麦场景；系统音量未记录；最佳延迟为样本内拟合；探针未加单测。
- [x] **批次 R4-B P1/P2（2026-10-03，用户批准）**：真 AEC3 接入打断通道 + 残余回声地板感知动态门（三件套的第 1、3 件）。
  - **P1 接线**：CLI `--aec-engine {nlms,webrtc}`（**默认 nlms 行为不变**）；`build_same_process_reference(engine=...)`（webrtc 缺依赖如实退回 nlms）；通道每帧先跑 `_apply_reference_aec()` 取**残差**算电平/门（**检测器结构判定仍看原始帧**）；对齐延迟每 0.5 s 喂 `stream_delay_ms`；只有"引擎真在跑 + 参考同钟"才报 `CAPABILITY_SOFTWARE`，抑制质量由 `aec_erle_db`/`aec_erle_sufficient` 如实暴露。
  - **P2 动态门**：`residual_gate_margin`（默认 None=关）/`residual_floor_frames=50`/`residual_floor_quantile=0.2`；有效门 = `max(energy_threshold, 地板 × margin)`（**只抬不降**），地板只吃参考有能量的帧、取 p20。
  - **失败路径**：`aec_process_failed`/`aec_output_invalid`/`reference_silent`（不计地板），一律不冒泡。
  - **验证**：新专项 27 项 OK；相关合计 **391 项 OK**；全量 **2553 → 2580 tests OK (skipped=3)**（235.444s，exit 0）；`compileall` exit 0；331 个 .py 编码扫描 0 失败 / BOM 25。交接 `checkpoints/2026-10-03-P10-R4B-P1P2-aec-engine-residual-gate-handoff.md`。
  - **未做**：三件套真机试听；`max_echo_path_ms=120` 未改（改判据属安全口径变更，需单独点头）；动态门参数未真机调参；NS/AGC 未启用；耳机/外接麦、方案 C 剩余项、R5/R6 未做。
- [x] **批次 R4-B 真机阻塞溯源（2026-10-03）**：`text_only` 真因 + 播报启动失败可观测性修复。
  - 现象：三件套真机试听 8 轮全部 `spoken=false/text_only`（无播报 ⇒ 打断通道没启、AEC3 没跑）。
  - 自检（新 `tools/p10_r4b_channel_selfcheck.py`，≈6 次真实请求）：工作类文本 / 闲聊文本 / **真机 ASR 入口** 三种组合下 `speech_eligible` 均 **True**；裸 speaker `start_stream()=ok:SessionStream`（开即取消）；`aec_backend=webrtc/aec_available=True` ⇒ **输入侧与裸 speaker 全部排除**。
  - **根因**：`chat_loop._speak()` 把 `start_stream()` 异常吞成 `text_only`（与"本轮没通道"同码）⇒ 真因不可见。**已修**：启动异常 → `speech_start_failed:<ExceptionName>`（只带类型名）；`None`/无方法 → 仍 `text_only`；`finish` 失败 → 仍 `speech_failed`。不改播报/打断行为与安全口径。
  - **验证**：相关 134 项 OK；全量 **2580 → 2585 tests OK (skipped=3)**（237.796s，exit 0）；`compileall` exit 0。证据 `research/2026-10-03-P10-R4B-playback-channel-selfcheck.md`；交接 `checkpoints/2026-10-03-P10-R4B-playback-observability-handoff.md`。
  - **未做**：会话里真正的启动异常仍未知（需一次真机运行读新原因码）；真机 8 轮无 AEC/打断数据 ⇒ 三件套真机验证仍待做；自检工具未加单测。
- [x] **批次 R4-B 回声证明收紧（2026-10-03，用户反馈"说一半就停了"）**：假停播真机证据 + 修复。
  - **真机证据**：末次会话 turn 2/4 **没插话**却 `barge_in_confirmed=true` + `end_reason=barge_in_interrupted`（各只跑 16 帧≈320 ms）；`echo_proven=true`、相关 0.60/0.72、估计延迟 **746 / −780 ms**（turn 3 为 **1589 ms**）；turn 4 `device_action=none` ⇒ 安全门没拦、播报真被停。
  - **根因**：`echo_proven` 旧规则只看相关性 ≥0.2、不校验延迟合理性 ⇒ 伪相关（本机 0.60~0.75）骗过证明 ⇒ R4-A 安全门放行。
  - **修复（只收紧）**：新增 `echo_proof_delay_min_ms=0`/`echo_proof_delay_max_ms=500`/`echo_proof_consecutive=3`/`echo_proof_delay_tolerance_ms=50`；`_note_proof_estimate()` 三条同时满足才算证明；详情暴露 `echo_proof_state`/`echo_proof_run`/`echo_proof_delay_ms`。
  - **测试**：新增 `tests/duplex/test_barge_in_echo_proof.py`（15 项，含真机伪证明数据回归）；`test_auto_voice_barge_in` 4 个停播路径用例改注入证明替身。
  - **验证**：新专项 15 项 OK；打断相关 178 项 OK；全量 **2585 → 2600 tests OK (skipped=3)**（234.990s，exit 0）；`compileall` exit 0；333 个 .py 编码 0 失败 / BOM 25。交接 `checkpoints/2026-10-03-P10-R4B-echo-proof-tightening-handoff.md`。
  - **未做**：真机复测（**复测前本机继续用 `--barge-in-mode off`**）；对齐估计本身为何给出 1589/−780 ms 未查明；阈值未真机调参。
- [ ] **下一步（待授权）**：① **真机复测**（用户在场 + 真实计费）：先 `--barge-in-mode off` 确认播报正常，再开 `confirm` 看是否还会假停播，读 `echo_proof_state`/`echo_proof_delay_ms`/`barge_in_*`；② 若仍有伪证明，下一批查**对齐估计本身**（为何超出 ±600 ms 搜索范围、如何用 far/near 活跃度约束）；③ 三件套真机验证（拿到播报窗口后）；④ 安全口径变更（`echo_proven` → 实测 ERLE + 残余门）；⑤ 方案 C 剩余项、R5/R6。
- [ ] **R4 未做（如实登记，不伪造）**：① **前置音频未并入下一轮 ASR**（`pre_roll_merge_unsupported`）——需要 audio 层 pre-roll 注入接口（`AudioBus` 序号/帧号 + 采集会话计数器一起推进），否则总线 `frame_discontinuity` 会打掉整轮，本批只保留并经 `take_pending_pre_roll()` 暴露；② 输出设备重连不可用（无固定输出设备 id）；③ 真机 ERLE/AEC 有效性待外接麦克风验证；④ 降音量粒度受后端 200 ms 缓冲块限制。
- [ ] 待授权：**R2b 真机验证（需外接麦克风）**、**R5**（Provider 冷却）、**R6**（长期运行健康）；本轮全部改动**未提交、未 push**。

## 2026-10-02：P9 收尾批次（孤儿测试 / BOM / P9-handoff）

- [x] 用户确认执行 A→B→C 三项收尾。
- [x] **A. 3 个孤儿测试修复**：`tests/conversation/test_manager.py` 中 `test_routed_text_creates_trace_and_calls_router_before_chat_agent` / `test_same_turn_id_is_idempotent_and_cannot_be_replaced` / `test_asr_final_uses_same_route_contract_and_turn_id_boundary` 原在 `@dataclass FakeAdapter`（非 `TestCase`）内、永不被收集；已原样移入 `ConversationManagerTests`，**断言未改写、生产代码未改动**（`setUp` 已提供 `self.now` / `self.adapter`；`ConversationManager.submit_text`/`submit_asr_final`/`router=` 与幂等分支均已在位）。专项 19 → **22 项全绿**；其中"同 `turn_id` 幂等且不可替换"此前全仓无覆盖。
- [x] **B. 去 UTF-8 BOM**：`tests/foundation/test_voice_hud.py` 删除文件头 3 字节（47,680 → 47,677 字节，diff 仅首行），满足 `2026-09-30-P9-batch1-2-interface-freeze.md:425` 的"新增/修改文件必须严格 UTF-8 无 BOM"；全仓 BOM **26 → 25**，与基线提交 `fdb0ce1` 的集合逐项一致。
- [x] **C. 补阶段交接**：新建 `implementation_plan/checkpoints/P9-handoff.md`（对齐 P0–P6/P8 格式：目标与不做清单、批次↔commit 对照、五条退出条件判定、交付物、验证证据、真机如实口径、本机私有配置、回滚、已知问题、下一步）；补入 `NEXT-SESSION.md` §9 与本节。
- [x] **验证**：全量 **2087 tests OK (skipped=3)**（2084 + 3，113.078s）、`compileall -q app tests tools` exit 0、全仓 294 个 .py 编码扫描 0 解码失败 / 0 `U+FFFD` / BOM 25；`git diff` 确认只改 2 个测试文件 + 文档，无生产代码改动。
- [ ] 仍待：P10；多轮/SSE/限流/persona 接入运行时；`threshold_mode=auto` 接 CLI；任务链消费真实路由；本轮改动待提交（**未提交、未 push**）。

## 2026-10-12：P10 · P1 收尾批次（长回复「无进展看门狗」）+ 提示窗 UI 批次 A/B/C

### P1（drain-stall）：收尾判据从「总时长」改成「无进展」
- [x] **起因与依据**：用户反馈"长回复会中止"；逆向豆包后确认其超时**全是空闲式**（`AudioTTSIdleTimeoutError` /
  `DialogAudioIdleTimeoutError` / `AudioChatRecvTimeoutError`），且 TTS 是**一次请求整条流式**、**只有打断才允许丢尾**。
  研究入档 `implementation_plan/research/2026-10-12-doubao-realtime-voice-reverse-engineering.md`（证据分级：官方 demo/官方方案说明/第三方协议整理/真实 SDK 头文件）。
- [x] **`app/tts/streaming_session.py`**：新增 `DEFAULT_STALL_TIMEOUT_S=15.0` 与 `stall_timeout_s`；`finish()` 的
  `join(timeout=segment_timeout_s)` → `_await_drain()`（每轮正好阻塞一个停滞预算、**不分片**；进展标记 =
  `dequeued/synthesized/played/segment_index` 四个只增不减的句子级计数）；新增 `drain_seconds` / `stall_timeouts` / `last_progress`。
- [x] **`app/tts/sequential_session.py`**：同构改造（一轮预算分给两个线程）；停滞**仍是显式失败** `pcm_seq_join_timeout`，
  detail 改为 `stalled_on=...,last_progress=...`；`DEFAULT_JOIN_TIMEOUT_S` 语义改为单线程预算。
- [x] **`app/runtime/voice_entry.py`**：`_await_room()` **看到进展就顺延**（不下降仍在同一预算后放弃，且放弃后照旧继续推送、
  绝不丢东西）；新增 `_flat_seconds()`；record 增三个事实。
- [x] **`app/runtime/auto_voice.py` + `app/main.py`**：`voice_turn` 增 `speech_drain_seconds` / `speech_stall_timeouts` /
  `speech_last_progress`；CLI 新增 `--speech-stall-timeout`（默认 15.0，只校验正数）并透传到两条通道。
- [x] **测试**：新增 4 个文件 22 项（流式停滞 3 / pcm_seq 停滞 3 / 背压与事实 8 / CLI 接线 8）；**离线对照**：
  流式 旧 1.81 s、11/33 字、`finish_timed_out=True` → 新 3.66 s、33/33 字、无截断；pcm_seq 旧 0.50 s、0/2、`pcm_seq_join_timeout` → 新 2.41 s、2/2。
- [x] **验证**：`tests/tts` 633 OK / `tests/voice_loop` 152 OK / 全量 **2718 OK (skipped=3)**（2696 → +22）、`compileall` exit 0、`--help` 可见新开关。
- [ ] 未做：**≥800 字真机长回复验收**（用户待跑）；`_enqueue()` 的"等待超时即整批拒绝"仍保留（需单独设计"何时仍应拒绝"）；
  收尾**不再有总时长上限**（极端慢但持续推进会一直等，由停滞预算兜底）；provider 逐句请求粒度未动（属 P2「整段一次请求」）。
- 交接：`implementation_plan/checkpoints/2026-10-12-P10-P1-drain-stall-handoff.md`。

### 提示窗 UI 批次 A/B/C（拖动 + 动效 + 无障碍）
- [x] **批次 A（拖动缺陷修复）**：旧实现每次 `WM_MOUSEMOVE` 都 `SetTimer` 同一 id（被下一次调用**重置**）⇒ 连续拖动
  **根本不跟手**、松手才跳一次；且 `_apply_drag()` 用 `margin_px=24` 复钳 ⇒ 松手**二次跳位**。改为移动事件里直接
  `SetWindowPos`、复钳 `margin_px=0`、松手按 `drag_snap_px=24` / `drag_settle_ms=120` **ease-out 吸附**；
  新增 `_snap_to_work_area()` / `_ease_out_cubic()`。
- [x] **批次 B（帧缓存 + 整窗 alpha）**：`render_frame()` 合成后回读 DIB 存 `_composed_frames[(title, body)]`；
  新增 `blit_cached_frame(..., alpha_scale)`（`bytes.translate` 256 项表 ⇒ 预乘 BGRA 四通道等比缩放，<1 ms/帧）。
- [x] **防闪修正**（用户："状态变化时候不要闪烁"）：`state_animation_ms` 默认 **0 = 状态切换硬切**；
  新增 `_STATE_SWITCH_GUARD_S=0.2` 护栏（短促连续切换跳过动效）。
- [x] **批次 C**：进场淡入 **+ 右侧 12 px 滑入**（`show_fade_ms=260`）；`hide()` 先 **180 ms 淡出+右滑**再隐藏并**归位**；
  新增 `respect_system_animation=True` → 读 `SPI_GETCLIENTAREAANIMATION=0x1042`，系统关动画时**零过渡帧**（含拖动吸附）。
- [x] **GitHub 调研**（实读源码）：sonner（MIT 13.0k★，进场 300 ms / 退场 200 ms、`prefers-reduced-motion` 全禁）、
  react-hot-toast（MIT 11.0k★，进场 0.35 s / 退场 0.4 s、reduced-motion 只留 opacity）；两库都**不对内容变化做动画**（印证状态切换硬切）。
- [x] **验证**：`tests/foundation/test_voice_hud.py` **92 OK**（63 → 92）；全量 **2747 OK (skipped=3)**，153.3 s；`compileall` exit 0。
  其中 5 条新用例跑**真实** `_wnd_proc`（只换 user32 替身），覆盖以前完全没有测试的拖动路径。
- [x] **文档**：设计文档 §1.2「不做动画」与 §3.5「不创建线程/定时器」两条边界已按现状改写；
  交接 `implementation_plan/checkpoints/2026-10-12-P9-hud-drag-motion-handoff.md`。
- [ ] 未做：**用户真机复验拖动与进场/退场观感**；`_DRAG_TIMER_MS` 保留但已不再使用；本轮改动**未提交、未 push**。

## 2026-09-30：P7-R-29 DeepSeek 真实对话 Agent（批次 B）

- [x] 用户逐项授权：HTTP Chat Completions 路线、非流式 + 单轮、复用现有双条件 opt-in、每轮 2 次请求、固定内置提示词、失败映射不重试不伪造不回退。
- [x] 新增 `app/providers/deepseek_chat_transport.py` 与 `app/agent/deepseek_chat_agent.py`；`deepseek_adapter` 抽出公开 `validate_https_origin()`；`provider_integration` 新增 `agent_ready` 与真实 `runtime_ready` 语义；`main` 新增 `build_agent_adapter()` 并注入真实 Agent。
- [x] 就绪语义：deepseek + opt-in → `transport_ready/agent_ready/runtime_ready=True`；默认 local 三者皆 False，行为不变。
- [x] 验证：新增 31 项测试；全量 **1390 tests OK (skipped=3)**、compileall exit 0；真实端到端 `chat/general_chat/success` → 真实回复，1771 ms（路由 + 回复 2 次请求）。
- [x] 事实修正：官方 `deepseek-harness-sdk` 当前**未安装**（wheel 仍在 `.wheelhouse/`），原记录已更正。
- [x] 交接：`implementation_plan/checkpoints/2026-09-30-P7-R-29-deepseek-real-chat-agent-handoff.md`。
- [ ] 仍待：语音链真机确认；多轮上下文；流式；费用/限流；persona；任务链与 P9；提交。

### 2026-09-30 补充：DeepSeek 凭据来源变更

- [x] 用户要求更换密钥：核实发现 `.env` 已是新值而用户级环境变量仍是旧值（两者不一致），普通终端会继承旧值 → 只写 `.env` 不生效。
- [x] 用户选择更新用户级环境变量为新值（两处一致，hash12 `2C3DFCC5498D`）；新增 `inject_dotenv_secret()`（白名单、环境变量优先、只写内存），`main` 仅在双条件 opt-in 下调用。
- [x] 验证：新增 4 项测试；全量 **1394 tests OK (skipped=3)**；真实验证 `injected_from_dotenv=True` → 真实对话成功（1940.7 ms、2 次请求）。
- [ ] 安全提醒：该密钥已在对话中明文出现，建议实测通过后轮换。
### 2026-10-07：长回复播报截断的 pcm_seq 侧取证（**本节为 2026-10-12 重建**）

> ⚠️ 原文乱码不可读；以下按 `implementation_plan/checkpoints/2026-10-07-voice-playback-truncation-investigation.md`
> 与 2026-10-09 结案复原**要点**（非逐字原文）。**结论以 2026-10-09 结案为准**：
> 根因是本地请求契约层的 `text_blank` 与 `ws_pcm` 序号错位，两处均已修复并真机验收。

- [x] 为 `pcm_seq` 通道补齐**段流事实**（每段的 provider 播放状态、固定失败短码、请求字数与失败段号），让"首段成功、后续段不可见"变得可观测。
- [x] 定位 `SequentialVoiceSession` 的请求**在构造阶段**就被固定短码 `text_blank` 拒绝（空行切出的纯空白碎片）；异常抛在 provider 调用之外，所以日志里是裸 `VoiceStudioContractError`。
- [x] 定位配套缺陷：`ws_pcm` 的纯空白块被分段器 `EMPTY_TEXT` 拒收且**不消费序号** ⇒ 其后每个 chunk 都被判越序、**静默丢尾**（旧版据此谎报"播完"）。
- [x] 把段级 provider 失败事实接进 `SessionStream` 与 `AutoVoiceSession`/`AutoTurnFacts`（只计数与固定短码，绝不含正文）。
- [x] 本批为**只读取证 + 事实补齐**：`git diff` 逐项复核，不改播报行为。
- [ ] 真机复测当时未跑；**2026-10-09 已跑并通过**（`pcm_seq` 9/9；`ws_pcm` 114/114、398/398 全播）。
- [x] 本机安全配置：`--barge-in-mode off`（fail-closed：不会假停播，也暂时不能真打断）。
- [ ] 遗留：`tools/*runner` 的编码/打印缺陷、TTS 启动失败可观测性（见后续批次）。
