# P7 后续执行总计划与批次跟踪（2026-09-27）

> 状态：2026-09-28 P7 验收已执行但 **阻塞/未完成**；参见 [阶段验收记录](checkpoints/2026-09-28-P7-T06-stage-acceptance.md)。原规划基线不代替真实验收。此文件是后续 P7 工作的**跟踪入口**，不能代替每批调研、方案确认、明确授权与独立验收。
> 更新原则：每批完成即在本表登记真实证据和交接链接；不得先勾选、跳过门槛或将离线通过记为真实链路通过。
> 依据：`implementation_plan/implementation-roadmap.md` 的 P7「TTS 输出与人物表达」退出条件、P7-R 设计/接缝，以及 `implementation_plan/checkpoints/2026-09-27-P7-R-23-offline-preflight-startup-boundary-audit-handoff.md`。若旧版 `README.md`/`task_plan.md` 仍写 P7-R-09 或 TTS 未开始，以可复核的最新交接与代码/测试事实为准；需要在专门的状态对账批次修正文档，不在本计划中静默改写历史。

## 一、现状及“完成”的含义

> 本节日期为 2026-09-27 的历史基线；2026-09-28 的最新结果以台账行和 T-06 阶段验收为准。**2026-09-30 更新：P7 已通过阶段级验收，见 [P7 阶段级验收（最终）](checkpoints/2026-09-30-P7-stage-acceptance-final.md)；本文下方 T-06 行与第四节清单已同步。**

- P0–P6 已有阶段验收。P7-R 已做到 **R-23 离线 preflight→startup 审计**；R-24 当时尚未开始；2026-09-28 已完成门检但未通过。DeepSeek 默认未启用真实执行，`runtime_ready/agent_ready/transport_ready` 仍为 false；真 transport、Agent 与网络为 **NOT READY / STOP**。R-08 曾有受限真实 Provider 评估，但那不等于当前 RouteHandoff、默认 runtime 或真实路线已验收。
- P7/TTS 已存在 VoiceProfile、分句、能力与请求/播放生命周期、队列和只读降级路由等本地契约/测试（Batch 95 交接；2026-09-27 的 TTS discovery **220/220** 通过）；未据此认定 VoiceStudio/Edge、真实 PCM/播放设备、实际播报、退化体验或 P7 阶段级集成通过。
- 2026-09-27 重新执行全量 discovery **678/678** 通过；2026-09-28 加入 14 个本地专项后全量 **719/719** 通过；这只证明当前测试集合通过，不证明 P7 退出条件已满足。P7-R-23 文档仍未提交。工作区存在事先 staged/untracked 的依赖基线与无关 `app/routing.py` 变更，禁止整体暂存或误归属。
- **口径分离**：P7-R 是路由/Provider 支线，P7 是路线图规定的 TTS 安全输出阶段；P8 是组合语音闭环。P7 阶段级完成以路线图 P7 退出条件和阶段集成验收为准，不把“R-24 做完”或“很多离线审计通过”当作 P7 完成；P8 必须另行授权。真实 DeepSeek 路由若是后续产品目标，另按 R 支线独立验收，不能借 TTS 验收偷渡接入。

## 二、状态、顺序与更新规则

状态仅用 `未开始 / 调研中 / 待授权 / 实施中 / 阻塞 / 已完成 / 不适用（附理由）`。下表的建议编号是**规划标签**，不是预先授权的固定实施批次；调研可拆分/合并，但必须在表中记录映射及原因。历史上优先做 G0 差距盘点和工作区对账，再分别推进 R 与 T；二者可独立进行离线研究，真实网络/设备权限互不继承。T6 阶段验收以实际 TTS 依赖批次通过为前提，不必等待所有可选 R 支线完成，但不得声称真实智能路由已就绪。

每批标准工作流（必须在该批次内执行，不能凭此文档跳过）：

1. **读与调研**：读本表、最新版 handoff、路线图、相关设计和现有源码/测试；对拟引入的 SDK/Provider/硬件行为核对官方资料、兼容性与可复现证据；必要时做开源方案对比。记录信息日期、来源、适用版本、发现和仍未知事项。只有总计划本身不代替具体批次的调研。
2. **给方案和边界**：写明目标/非目标、精确文件范围、测试与失败/回退条件、网络/凭据/费用/设备/副作用上限；展示调研结论与可选方案，取得该批**明确确认**，随后才可改代码或发真实请求。规划文档不授予网络、设备或侧效应权限；若权限不足，记录 `待授权/阻塞`，不靠模拟成功冒充完成。
3. **先验收标准后实施**：尽量先红测/fixture；仅做当批范围，保留 `Plan → preflight → authorization → ledger → executor → verification → audit` 安全链；失败默认文字可用、不可误执行，不能由路由/TTS 推断授权。
4. **验证与审计**：运行批次专项、相关集成、全量 unittest、`compileall`、UTF-8 与差异检查；需设备/网络时以明确预算隔离实测；记清实际计数、版本、环境、未测项与脱敏证据。检查默认关闭、无凭据泄漏、未新增 P8/电脑副作用。
5. **交接与登记**：先写独立 checkpoint（修改范围、调研链接或记录、测试结果、剩余风险、下一停止点），再将下表该行从原状态更新到真实状态、日期和交接路径；如未达到验收，登记阻塞而非勾选。Git 提交前单独审阅具体文件与既有工作区基线；未经明确指示不批量 stage/commit/push/reset。

## 三、建议批次台账（随工作推进维护）

| 批次 | 目标与验收证据（细节调研后冻结） | 状态 | 完成日期 / handoff / 阻塞原因 |
|---|---|---|---|
| G0：退出条件和现状对账 | 对照路线图五条 P7 退出条件及 P7-R 接缝逐项列出现有代码、测试、缺口、真实/模拟边界；检查旧 README/计划状态漂移和既有 Git 基线；输出依赖图与最小可验收切片，不写应用代码。 | 已完成 | 2026-09-27；[G0 只读差距与基线交接](checkpoints/2026-09-27-P7-G0-exit-gap-baseline-handoff.md)。仅完成盘点及文档登记；P7 五项退出条件未验收，T-01/R-24 尚未授权。 |
| R-24：真实 transport 授权前置评审 | 接续 R-23，只做文档决策门：凭据来源、目标/端点、HTTP/SDK、总请求数含重试、费用/速率、超时、日志脱敏、失败回滚和禁止侧效应；信息缺失即 STOP；不读密钥、不联网。 | 已完成 | 2026-09-28；[R-24..R-26 真实路由决策门](checkpoints/2026-09-28-P7-R24-R26-real-route-gate.md)。完成评审但真实请求门 NOT READY/STOP；请求 0 次。 |
| R-25：隔离 Provider 契约验证（条件批次） | **仅在 R-24 通过且获得另一份真实请求专项授权后**：按预算做最小真实 `deepseek-route/v1` 输出/失败/超时验证；记录脱敏结果与重复性，仍不启动默认 runtime/Agent/工具；未授权则阻塞而不是离线替代。 | 阻塞 | 2026-09-28；[真实路由决策门](checkpoints/2026-09-28-P7-R24-R26-real-route-gate.md)。R-24 未通过真实请求门；未读密钥、未发真实请求，不能用离线协议测试代替。 |
| R-26：路由到真实运行时的闭环评估（条件批次） | 根据 R-25 证据先定实现/测试方案：严格字段映射、低置信/非法响应 fail-closed、会话接缝、就绪状态与可回滚配置；如需 Agent/默认 Provider/网络，再独立授权，未获授权不接线、不宣布 Provider 可用。 | 阻塞 | 2026-09-28；[真实路由决策门](checkpoints/2026-09-28-P7-R24-R26-real-route-gate.md)。R-25 无真实证据，未接线，runtime/Agent/transport 保持 NOT READY。 |
| T-01：TTS 退出条件差距冻结 | 接续 G0 与 Batch 95，逐一核验分句/VoiceProfile/请求生命周期/队列/路由的已有证据；写真实播放、回退和设备测试矩阵以及本阶段非目标；抑制无验收收益的第 96+ 次纯只读审计。 | 已完成 | 2026-09-27；[T-01 退出条件与测试矩阵交接](checkpoints/2026-09-27-P7-T-01-tts-exit-criteria-test-matrix-handoff.md)。本批重新核对两处 GitHub 固定提交源码及本地契约，仅冻结矩阵；未运行测试/应用、未接真实 Provider/设备。五项阶段退出条件仍未验收；T-02/T-03 均需另行授权。 |
| T-02：本地播报编排与假设备集成 | 经本批调研/授权后，把已确认文字回复→分句→路由→队列→播放事实按原序贯通；用 fake TTS/设备测试取消、背压、身份一致性、旧回复不跨 Provider 续播、不影响任务事实；不访问真实设备/网络。 | 已完成 | 2026-09-28；[T-02/T-05 本地假设备交接](checkpoints/2026-09-28-P7-T02-T05-local-integration-handoff.md)。仅 opt-in 接缝、假设备回执；无真实播报，缺真实设备同句回退。 |
| T-03：真实 VoiceStudio/Edge/设备前置门 | 单独调研官方/本机能力、版本、隐私、凭据与计费、格式/延迟、播放设备和回滚；提出小范围探测方案并获得专门授权；仅评审不连接服务、不占用设备。若本地/网络环境不可用，记录阻塞和文字降级，不声称 P7 真实播报完成。 | 已完成 | 2026-09-28；[T-03/T-04 真实播报决策门](checkpoints/2026-09-28-P7-T03-T04-real-tts-gate.md)。完成评审但未通过实测门：VoiceStudio 产品/接口及设备/凭据/费用缺证，未访问设备/服务。 |
| T-04：有界真实播报与状态事实验证（条件批次） | T-03 通过且获真实 Provider/设备专项授权后，用非敏感固定文字、有限次数验证原序播放、设备提交点、取消/中断及播放事实；记录“生成成功≠听到”证据，设备或 Provider 不可用即明确阻塞。 | 阻塞 | 2026-09-28；[真实 VoiceStudio 成功基线](checkpoints/2026-09-28-P7-real-voicestudio-success-baseline.md)。已完成 1 次本机 VoiceStudio/OmniVoice 真实合成和 1 次 WAVE_MAPPER waveOut 自然完成播放；stop/reset、speaker_output_stopped、user_heard、真实故障注入仍未证明，不能宣布本批完成。 |
| T-05：安全降级与故障/用户边界 | 经调研和单批授权验证 VoiceStudio→已配置 Edge→文字回退；提交前才可换 Provider、播放后不在当前句混声/重播；VoiceProfile 仅在允许的句/回复边界生效；注入超时、掉线、打断、半句与过期身份，TTS 不修改任务/授权/结果事实。 | 阻塞 | 2026-09-28；[T-02/T-05 本地故障交接](checkpoints/2026-09-28-P7-T02-T05-local-integration-handoff.md)。本地故障矩阵已测；同句提交前 Edge 切换未实现、真实设备/服务故障未测，不能宣布本批完成。 |
| T-06：P7 阶段级验收与移交 | 汇总 T-01..T-05 与 G0 的证据，覆盖路线图五项退出条件和 P2/P3/P5 安全回归；本地+经授权的必要真实链路逐项复核；记录剩余限制、覆盖范围和阶段验收报告。全部门槛成立才标记 `P7 complete`，否则 `阻塞/未完成`；交接后仍不自动进入 P8。 | 已完成（阶段级） | 2026-09-30；[P7 阶段级验收（最终）](checkpoints/2026-09-30-P7-stage-acceptance-final.md)。**五项退出条件全部通过**（真实+离线证据逐条链接）；全量 **1065 tests OK (skipped=3)**；故障注入（B10）、Edge（B11）、性能指标（B9/B9-fix）、用户听感均已记录 → 标记 **`P7 complete`（阶段级）**。历史：2026-09-28 曾为 `阻塞`（[T-06 记录](checkpoints/2026-09-28-P7-T06-stage-acceptance.md)，五项均未通过），其阻塞项已于 2026-09-30 全部处置。**未进入 P8**。 |

具体批次可以调整先后：R-24 与 T-01 在 G0 后可分别准备；R-25/26 为条件支线，不得挡住 TTS 的本地批次；T-04 与 T-05 先后由调研决定，但 T-06 不能绕过真实播报/降级缺口。任何表中“完成”都要有日期、可追溯 handoff 和验收记录。

## 四、P7 阶段最终检查清单（未勾选 = 未证明）

- [x] 已确认文字回复按句子边界、原顺序实际播放；有可复核的 end-to-end/设备证据，而非仅分句或 fake 测试。— 真实：B9 paced 三模式各 20/20、Batch 5 真实三句、Batch 7 句长矩阵；用户听感 `user_heard=true`（主路径）。见[最终验收](checkpoints/2026-09-30-P7-stage-acceptance-final.md)第 2 节。
- [x] 播放提交前按配置可切 Edge；播放开始后当前 segment 不换声、不混声、不重播；旧回复被打断后不跨 Provider 续播。— 真实：B11 杀 VoiceStudio → `route=edge`/`edge_attempts=1` 且真的出声；已出音 → `edge_attempts=0`/`replay=False`；B10 proxy 演练同结论；Batch 7 播放中取消无新 write。
- [x] TTS 失败不改变任务、计划、授权、账本、执行和结果事实；不可用时安全回落至文字并保留可用性。— 结构：`app/tts/**` 对安全链模块引用数 **0**（唯一外部类型为只读 `AgentTurnOutcome`）；真实：B10/B11 全部失败场景文字继续、语音停止；离线：`test_completed_reply` 11/11、故障矩阵。
- [x] VoiceProfile 更新仅在许可句子/回复边界生效，且用户权限与版本边界经测试。— `_verify_once()` 每回复一次（B9-fix）；`test_orchestration` 的 profile drift 两例 + `test_real_playback` 29 项（tampered owner / bad version / 不可覆盖）；live 选择文件从未被写入。
- [x] 阶段级回归、故障注入、环境/版本/预算、隐私/日志/停止条件已在阶段验收文档中记录；后续 P8 与真实智能路由权限仍独立。— [最终验收文档](checkpoints/2026-09-30-P7-stage-acceptance-final.md)第 3–8 节；全量 1065 OK (3 skipped)；R 支线仍 `NOT READY`，P8 未进入。

## 五、验收停点：P7 阶段级验收已完成（2026-09-30）；未进入 P8

- **P7 状态：`P7 complete`（阶段级）**，依据 [P7 阶段级验收（最终）](checkpoints/2026-09-30-P7-stage-acceptance-final.md)（路线图五项退出条件全部有证据）。
- **停点**：不接"播放时监听/AEC/全双工"；**不进入 P8**（组合闭环须另行授权）；**不启用默认语音**（用户 2026-09-30 决定保持 `TTS_VOICESTUDIO_MODE=disabled`）。
- 独立支线：真实 DeepSeek 路由（R-24..R-26）仍 `NOT READY / STOP`，与本次验收无关，不得借 P7 结论接线。

历史下一入口（仅供追溯）：

**以下为历史 G0 起点：先执行 G0 的只读调研与差距报告**：从本文件、最新版 R-23 handoff、Batch 95 handoff 和路线图 P7 退出条件开始；分清 P7-R 与 P7/TTS；先形成范围与验收条目，再申请该批确认。R-24 仍是 R 支线的下一入口，并非启动 DeepSeek 真请求；T-01 是 TTS 支线的下一入口。该句为 2026-09-27 规划原文；2026-09-28 已执行部分本地 TTS 和门检，但**真实 Provider、设备、TTS 播放与 P8 均未实施**；实际停止点为本表 T-06 阻塞。

## 2026-09-28：持久化温和音色选择与 T07–T09（本节原文损坏，按证据重写）

> **数据损坏登记（2026-09-30 发现）**：本节及其后表格的原始中文文本在本仓库首次入库（提交 `fdb0ce1`）之前就已被替换为 ASCII `?`（逐字内容不可恢复，git 历史中同样损坏）。下列内容是**按现存可复核证据重写的摘要，不是原文**：证据来源为 [T-06 阶段验收记录](checkpoints/2026-09-28-P7-T06-stage-acceptance.md)、[持久化温和音色选择交接](checkpoints/2026-09-28-P7-persisted-gentle-voice-selection-handoff.md)（该文件同样损坏，仅数字与英文锚点可读）、本文件 2026-09-28 T12 节与当前仓库代码。引用本节时不得当作原文逐字引用。

- **音色选择的原文锚点**：schema `version=2`、profile `e8572e8e`、`speed=0.80`、`pause_ms=400`、`fade_ms=80`；选择文件 `<USER_HOME>\AppData\Local\ComputerVoiceAssistant\xiaobing_voice_selection.json`（另有 `.bak-20260928-085` 备份）。
- **speed 口径冲突（待对账，禁止静默改写）**：当前 `app/tts/private_selection.py` 固定 `DEFAULT_SPEED = 1.0` 且 `validate()` 拒绝其他 speed；2026-09-30 Batch 3/5 记录同为 `speed=1.0`；`docs/superpowers/specs/2026-09-28-xiaobing-voice-integration-design.md` 写作 `0.85`；本节原文锚点为 `0.80`。三处数值不一致属状态漂移，需专门对账批次确认真实生效值，本表不预判。（**2026-09-30 对账结案：当前生效值 = `1.0`；`0.80`/`0.85` 属不可复原的历史差异，见第 8 项与第 5 节。原句保留不改，以存历史。**）
- **真实服务事实**：本机 VoiceStudio `/health` HTTP 200、版本 `0.5.6`、profile `e8572e8e`，本地 WAV 与 provider reference audio 身份核对通过。
- **当轮播报事实**：2 句回复、2 次播放、`text_fallback=false`；属受限真实预览，**不等于** Agent 默认链路出声。
- **当轮证据**：unittest **719/719 OK**、`compileall`、UTF-8 AST 检查、`git diff --check` 与 staged diff 检查。
- **当轮遗留**：stop ACK 未在真机证明；`app/main.py` 与 Agent 未接线；**Edge 无播放实现**；跨 Provider 切换、取消与迟到块未验证；T-04/T-05/T-06 未通过。结论：P7 未 complete，未进入 P8。

### 2026-09-28 T07–T09 交接表（同一损坏段落，按 checkpoint 可读事实重写）

| 批次 | 主题 | 结果与证据（当轮） |
|---|---|---|
| T07 | 播放停止 / stop ACK | `app/tts/playback_worker.py` 增加 ticket 化 stop 与 fail-closed 路径，`app/tts/real_playback.py` 区分 Windows waveOut 路径；`STOP_ACKED` 仅表示 backend 归还 buffer，真实静音与听感未证明 |
| T08 | Agent 回复 opt-in | `app/main.py` 增加 `voice_enabled=False` 的 opt-in 接缝与 `CompletedReplySpeaker` 构造（含 `TextShell.run_conversation` 的 flush 与 `AgentTurnOutcome` 失败分支）；默认关闭，未接真实 Agent 出声 |
| T09 | 阶段验收矩阵 | 逐项矩阵 + **746/746 OK**（当轮基线）；仅离线证据，真实 Provider/Edge/真机 stop ACK 缺失 |

T04/T05/T06 当时仍阻塞，P7 保持未完成、未进入 P8；上列 719/719 与 746/746 均为当轮基线，当前基线见本文件末节。

## 2026-09-28 T12：三项补齐后的结果复核

- [T12 可停止播放、默认接缝与故障矩阵复核补充](checkpoints/2026-09-28-P7-T12-followup-verification.md)
- 本地专项结果校正为：全量 **773 tests / OK (skipped=2)**；`main_voice_opt_in` 14、`playback_worker` 11、`real_playback` 29、`real_failure_matrix*.py` 20（其中 2 个 live gate 跳过）。
- 本批结论：可停止播放、本地默认安全接缝、离线真实故障矩阵契约均已补齐；真实 VoiceStudio 故障注入、真实 Windows waveOut stop 证据、真实 Agent → VoiceStudio → 设备默认端到端仍为 `Blocked`。
- `STOP_ACKED` 仍只表示 backend reset/buffer-return 协议确认；不表示扬声器已静音，也不表示用户已听见。`user_heard` 没有独立证据时必须为 `None`。
- P7 继续保持 `Blocked`，P8 未进入。
## 2026-09-28：真实证据执行交接

- [P7 剩余进度与真实证据执行交接](checkpoints/2026-09-28-P7-remainder-real-evidence-handoff.md)
- 用户已明确表示全部授权；交接文档记录了真实 VoiceStudio 故障注入、真实 Windows `waveOut` stop 证据、真实 Agent→VoiceStudio→设备端到端、独立声学/用户听感证据的执行顺序和边界。
- 下一对话必须先做具体 preflight、证据 ledger 和请求/播放/费用预算登记；本次没有执行真实网络、设备、播放或故障注入。
- 在真实证据完成前，P7 保持 `Blocked`，P8 未进入。
## 2026-09-30：VoiceStudio 双路径 Batch 1（离线契约基线）

- 状态：**Batch 1 完成；Batch 2 NOT READY / STOP**。此记录只更新 P7/TTS 支线，不将 P7 整体标记为完成，也不进入 P8。
- 来源：[双路径逐句执行计划](2026-09-29-voicestudio-dual-path-streaming-execution.md)；[Batch 1 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch1-baseline.md)。
- 已完成：sentence request、统一 PCM chunk、provider Protocol、fail-closed validator、fake WS/HTTP 和 fake sink；未接线默认运行时。
- 证据：Batch 1 专项 10 tests OK；TTS 331 tests OK（2 skipped）；全量 830 tests OK（2 skipped）；`python -m compileall -q app tests tools` 与 `git diff --cached --check` 通过。
- 边界：未执行真实 HTTP/WebSocket、VoiceStudio、扬声器、GPU 或 waveOut；未安装 `websockets`，未修改安全、授权、审计或任务执行语义。
- 下一入口：Batch 2 逐句 HTTP `/generate` 快速路径，需用户单独授权；不提交、不 push，提交前先审阅完整暂存 diff。

## 2026-09-30：P7 VoiceStudio Batch 2 HTTP WAV checkpoint

- [x] 用户已授权继续 Batch 2；本批次范围限定为逐句 HTTP /generate，不进入 Batch 3 WebSocket。
- [x] pp/tts/providers/http_wav.py：loopback-only、no-redirect opener、multipart 请求、身份 header、完整 WAV 校验、取消/关闭/transport fail-closed、单 final PCMChunk。
- [x] pp/tts/segmentation/__init__.py：可选实时软长度策略与配置校验；历史默认配置行为保持不变。
- [x] pp/tts/real_playback.py：RealtimeVoiceSession(provider_mode="http_wav") 显式 opt-in、逐句 provider 调用、validator、WAV playback 复用、取消 fence、pending=2 背压。
- [x] 测试结果：Batch 2 定向 30/30；HTTP 8/8；TTS 348/348（2 skipped）；全量 847/847（2 skipped）；compileall、git diff --check 通过。
- [x] checkpoint：implementation_plan/checkpoints/2026-09-30-P7-voicestudio-batch2-http-wav.md。
- [ ] 真实 HTTP/VoiceStudio/GPU/Windows waveOut/扬声器证据未执行；Batch 3 继续 NOT READY / STOP。
- [ ] 工作区包含大量既有 staged/untracked 文件；本轮未执行 git add .、commit 或 push。

## 2026-09-30：P7 VoiceStudio Batch 3（WebSocket 客户端，离线部分）

- 状态：**Batch 3 完成（离线实施 + 真实三句串行协议 preflight 通过）**。本记录只更新 P7/TTS 支线，不将 P7 整体标记完成，也不进入 P8。
- 来源：[双路径逐句执行计划](2026-09-29-voicestudio-dual-path-streaming-execution.md)；[Batch 3 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch3-websocket-client.md)。
- 协议核对：只读本机 VoiceStudio v0.5.6 `tts_stream.py`（477 行），确认 `routing`（可选，per socket）→ `start`（首句合成后）→ binary PCM16 LE（4800 samples/块）→ `done`；`error` 帧后服务端**保持连接**，客户端必须自行 fail-closed。
- 依赖：`websockets==17.1`，wheel `websockets-17.1-cp312-cp312-win_amd64.whl`，SHA-256 `617243E19A0992095956F406EE9CD3BC4BA92862D83CB1D83BB59CE574412BEC`，`License-Expression: BSD-3-Clause`，`Requires-Python>=3.11`，零运行时依赖；`pip install --no-index --find-links .wheelhouse` 装入 `.venv`。
- 已完成：`app/tts/providers/voicestudio_ws.py`（loopback-only、惰性建连、跨句复用、单在途请求、session idle 释放、严格状态机、大小与超时上限、取消与坏连接丢弃、错误脱敏、延迟一块标记 `is_final`）；`app/tts/providers/__init__.py` 导出；`tests/tts/test_voicestudio_ws.py` 35 项（含进程内 loopback fake server 集成）。
- 证据：WS 专项 35/35；TTS 383 tests OK（2 skipped）；全量 882 tests OK（2 skipped）；`compileall -q app tests tools` 通过；`git diff --check` 通过；3 个文件严格 UTF-8 无 `U+FFFD`；无顶层 `websockets` 导入，AST 网络/进程调用面仅默认 factory 的 `connect`。
- 边界：未执行真实 WebSocket/HTTP、VoiceStudio、GPU、扬声器、waveOut 或 PCM 播放；未修改 `settings.py`、`real_playback.py`、`completed_reply.py`、播放 worker、路由、VoiceStudio 本体、任务执行或授权语义；`TTS_VOICESTUDIO_MODE` 默认仍为 `disabled`。
- 遗留：真实三句串行 preflight 需单独授权（3900 未监听）；Batch 4 PCM 播放、Batch 5 自动降级、Batch 6 barge-in、Batch 7 真实设备与性能、Batch 8 默认切换均未开始。
- 未提交、未 push；审阅时只关注本批 3 个文件与文档，不使用全仓库无选择 staging。

## 2026-09-30：P7 VoiceStudio Batch 3 真实协议 preflight（Batch 3 收口）

- 状态：**真实协议 preflight 通过，Batch 3 退出条件满足**；PCM 播放未接入，P7 整体仍未完成，未进入 P8。
- [Batch 3 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch3-websocket-client.md)；证据 artifact `checkpoints/artifacts/2026-09-30-P7-batch3-ws-preflight/p7-batch3-ws-preflight-default-84c7b9344590edee.json`。
- 环境：本机 VoiceStudio `0.5.6`，`/health` 200，`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`；确认音色 `e8572e8e`（`design`，speed 1.0，参考音频 sha256 与本地选择一致）。
- 真实结果（默认客户端配置，connect 2 s / 单句 18 s / 帧 idle 3 s / session idle 30 s）：3 句串行全部成功，**连接数 = 1**；chunks 9/12/11，samples 39,840/54,000/49,680，音频 1.66/2.25/2.07 s，首块 1688/1562/1625 ms；每句 `is_final=True`，`done.samples` 与实收 PCM 样本一致。只收 PCM、未播放、未落盘音频、`user_heard=null`。
- 真实环境缺陷（已修）：`start` 帧在首句整句合成后才发送，修复前首个 recv 被 3 s 帧 idle 截断（实测 `frame_idle_timeout`，elapsed 3344 ms）。现 `start` 之前由单句预算约束、`start` 之后才启用帧 idle；新增 2 项测试，计划文档 7.4 节已同步。
- 真实链路阻断（已修，属 Agent→说话链路）：`xiaobing_voice_selection.json` 带 UTF-8 BOM 且 `speed` 为 int 1，使 `read_selection()` 必然失败 → `CompletedReplySpeaker.start_stream()` 返回 None、`__call__` 置 `_disabled=True`，真实链路此前不可能出声。已备份为 `.bak-20260930` 并原子替换为同值文件（去 BOM、`speed=1.0`）；`private_selection.py` 的严格校验未放宽。
- 修复后验证（已复跑通过）：WS 专项 37/37；TTS discovery 385 tests OK（2 skipped）；全量 discovery 884 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0；9 个本批文件严格 UTF-8、无 BOM、无 `U+FFFD`。中途一次 DSH 执行后端故障已排除，无待补验证项。
- 边界：真实 preflight 只证明客户端与协议、样本统计；不代表扬声器出声或用户听见。PCM 播放（Batch 4）、自动降级（Batch 5）、barge-in（Batch 6）、真实设备与性能（Batch 7）、默认切换（Batch 8）均未开始；`TTS_VOICESTUDIO_MODE` 默认仍为 `disabled`。

## 2026-09-30：P7 VoiceStudio Batch 4（PCM 流式播放后端，离线 + 真机）

- 状态：**Batch 4 完成（离线实施 + 真实 Windows 设备三 buffer 生命周期验证通过）**。不将 P7 整体标记完成，也不进入 P8。
- [Batch 4 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch4-pcm-playback.md)；真机 artifact `checkpoints/artifacts/2026-09-30-P7-batch4-pcm-stream-device/p7-batch4-pcm-stream-device-a7f5f884aa3e1aa4.json`。
- 已完成：`app/tts/pcm_playback.py`（`PCMStreamBackend` Protocol、`PCMPlaybackWorker`：160 ms 起播阈值、8 s 未提交队列背压、16 MiB 单句上限、身份与 sequence 连续校验、迟到块丢弃、自然完成 `COMPLETED`、stop ACK 需 backend 归还 buffer、timeout/unknown fail-closed 并 disable、backend 异常 fail-closed）；`app/tts/waveout_pcm.py`（三 buffer `WAVEHDR` 循环、`WOM_DONE` 后才复用、只清 `WHDR_DONE` 并保留 `WHDR_PREPARED`、`waveOutReset` 在条件锁外调用、uncertain 时保留 backend 到进程结束、`PCMWaveOutDiagnostic`、`winmm` 可注入）；`tests/tts/test_pcm_playback.py` 35 项。
- 真实设备证据（合成 440 Hz 正弦、幅度 0.1、不落盘音频）：阶段 1 自然完成 `COMPLETED`（12 块 → 6 次 `waveOutWrite` 复用 3 个 header，`returned_count=3`）；阶段 2 在飞 4 次 write 时真实 stop `STOP_ACKED`（真实 `waveOutReset=0` 回收全部在飞 buffer）；两次 `cleanup_verified=true`、`uncertain=false`。
- 真机缺陷（已修）：`submit()` 曾整体清零 `dwFlags`、抹掉驱动设置的 `WHDR_PREPARED`，第二次 `waveOutWrite` 被拒（真机 `FAILED`、`write_count=1`）；现只清 `WHDR_DONE` 并校验 `WHDR_PREPARED`，fake WinMM 改为镜像驱动的 `WAVERR_UNPREPARED` 拒绝并新增回归测试。该缺陷此前 34 项离线测试全部漏掉。
- 证据：PCM 专项 35/35；TTS 420 tests OK（2 skipped）；全量 919 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0；3 个新文件严格 UTF-8、无 BOM、无 `U+FFFD`。
- 边界：未接入 `RealtimeVoiceSession` 或双路径调度；未改 `settings.py`（`TTS_PCM_*` 留到 Batch 5 接线时再加）、`playback_worker.py`、`real_playback.py`、路由与默认 runtime；本次无 acoustic 测量与人工确认，`speaker_acoustically_verified=false`、`user_heard=null`；`STOP_ACKED` 仍只表示 backend 归还 buffer。
- 遗留：真实 VoiceStudio PCM 流式端到端、长时间/连续多句设备压力、驱动内部 reset/WOM_DONE 时序统计均未做；Batch 5-8 未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 5（5A 传输层 + 5C 配置）

- 状态：**5A 与 5C 完成；5B（接入实时会话与 PCM worker）未执行**。不将 P7 整体标记完成，也不进入 P8。
- [Batch 5 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch5-transport-and-config.md)。
- 已完成：`app/tts/transport.py`（`VoiceStudioTransportRouter`：`http_wav` 只用 HTTP 且不碰 WS；`ws_pcm` 只用 WS 且不降级；`auto` 仅在尚未产出任何 PCM 时失败才用 HTTP 重做本句一次；**出音后失败即终态、不重做不换声**；取消不降级；每句独立重新先试 WS；`TransportSnapshot`/`TransportFallbackRecord` 记录 transport、阶段、原因与 latency，不记文本与 PCM）。
- 配置：`TTS_VOICESTUDIO_MODE` 增加 `ws_pcm`；新增 `TTS_WS_URL`、`TTS_HTTP_GENERATE_URL`（仅 loopback，拒绝凭据/query/fragment/非法端口）、`TTS_WS_CONNECT_TIMEOUT_S`、`TTS_SENTENCE_TIMEOUT_S`、`TTS_WS_FRAME_IDLE_TIMEOUT_S`、`TTS_WS_SESSION_IDLE_S`、`TTS_PCM_START_BUFFER_MS`、`TTS_PCM_MAX_QUEUED_AUDIO_MS`（范围校验）；`public_summary` 不暴露 URL 明文。
- 证据：transport 专项 18/18；config 27/27；TTS 438 tests OK（2 skipped）；全量 944 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0；4 个本批文件严格 UTF-8、无 BOM、无 `U+FFFD`；AST 审计无设备/网络/文件/子进程入口。
- 边界：本批**未接线任何运行时**；未改 `real_playback.py`、`completed_reply.py`、`routing.py`、`playback_worker.py`、`pcm_playback.py`、`app/main.py`；默认 mode 仍为 `disabled`；`TTSFallbackRouter` 的跨 Provider 决策未被改动，也未与 transport 层串接验证。
- 遗留：5B（transport router 接入实时会话 + `PCMPlaybackWorker` 流式播放 + 传输层失败交给跨 Provider 决策）；真实 WS→HTTP 降级演练；延迟/性能指标；Batch 6-8 未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 5B（流式会话接入，Batch 5 收口）

- 状态：**Batch 5 完成（5A 传输层 + 5C 配置 + 5B 流式会话接入，全部离线验证通过）**。不将 P7 整体标记完成，也不进入 P8。
- [Batch 5 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch5-transport-and-config.md)（已含 5A/5C/5B）。
- 已完成：`app/tts/streaming_session.py`（`StreamingVoiceSession`：独立于 `RealtimeVoiceSession`，逐句经 `VoiceStudioTransportRouter` 合成、PCM chunk 交给 `PCMPlaybackWorker` 流式播放、分句复用 `ReplySegmenter` 实时参数；接口兼容 `TextShell`；`build_streaming_session` 只接受显式注入的 provider/backend）。
- 跨 Provider 决策：传输失败时按事实映射——**从未产出 chunk → `NOT_COMMITTED`**（Edge 已配置可切 Edge）、**已产出 chunk → `PLAYBACK_STARTED`**（禁止换声与重播，直接转文字）；决策经 `TTSFallbackRouter.primary_failed` 产出并以 `routing_decision` 暴露，本模块不自行播放 Edge。用户取消与内部失败分开记录。
- `app/tts/completed_reply.py`：mode 白名单扩展为 `legacy`/`http_wav`/`ws_pcm`/`auto`；`ws_pcm`/`auto` 必须显式提供 `session_factory`，否则 `ValueError`；默认仍 `legacy`。
- 证据：streaming 专项 18/18；transport 18/18；config 27/27；TTS 460 tests OK（2 skipped）；全量 966 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0；4 个本批文件严格 UTF-8、无 BOM、无 `U+FFFD`；AST 无设备/网络/文件/子进程入口。
- **`legacy`/`http_wav` 不变性**：`RealtimeVoiceSession` 未被修改；`test_realtime_voice`（30 项）、`test_voicestudio_http_wav`、`test_completed_reply` 原有用例全部保持通过。
- 边界：未接入默认 runtime/`main.py`；`TTS_VOICESTUDIO_MODE` 默认仍为 `disabled`，新路径只有显式注入才运行。
- 遗留：真实 VoiceStudio 端到端（真实 WS 流式播放、真实 WS→HTTP 降级、真实设备多句流式）未验证；无性能指标；计划 8.1 的"已起播缓冲但设备未开始"宽松降级分支本实现未采用（出音即终态，差异已记录）；Batch 6-8 未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 5 真实环境验收

- 状态：**Batch 5 真实环境验收通过**（真实 WS 流式播放 + 真实 WS→HTTP 降级 + 真实设备 buffer 回收）。不将 P7 整体标记完成，也不进入 P8。
- 环境：本机 VoiceStudio `0.5.6`、`/health` 200、`cuda (NVIDIA GeForce RTX 3050 Laptop GPU)`；artifact `checkpoints/artifacts/2026-09-30-P7-batch5-live-streaming/p7-batch5-live-streaming-3c16284ba9e8453d.json`。
- 阶段 A（真实 WS 流式 3 句）：`played=3`、`error=None`、真实 `waveOutWrite` **32 次**（3 buffer 轮转复用）、**fallbacks=0**、`cleanup_verified=true`。
- 阶段 B（`ws://127.0.0.1:3999` 未监听触发真实建连失败）：`played=1`、`error=None`、fallback `(stage=connect, reason=connect_failed)`、真实 HTTP `/generate` 接管并完成播放、`cleanup_verified=true`。
- 证明范围：真实 WS PCM 流经 transport router 进入 `PCMPlaybackWorker` 逐句播放；真实首音前建连失败触发**本句一次** HTTP 降级并成功；两条路径 waveOut buffer 均完整回收、`uncertain=false`。
- 严格不推断：无声学测量与人工确认，`speaker_acoustically_verified=false`、`user_heard=null`；`STOP_ACKED` 仍只表示 backend 归还 buffer。
- **独立阻断已排除**：`xiaobing_voice_selection.json` 与此前备份曾被外部删除（项目代码无删除逻辑，原因未查明）；经用户确认已于 2026-09-30 重建同值文件（179 bytes、UTF-8 无 BOM、`speed=1.0`、权限 0600、原子替换），`read_selection`/`verify_reference`/`verify_provider` 三项全部通过，真实默认说话链路恢复可读。本次验收使用的 `selection_source=in_memory_confirmed_values_file_missing` 仅记录当时状态。
- 遗留：性能指标（计划 19.2 p95）未采集；真实 barge-in/迟到块（Batch 6）、真实设备与性能（Batch 7）、默认切换与回滚（Batch 8）未开始；未提交、未 push。

## 2026-09-30：Agent→说话链路修复（ASR 逐句播报 + 背压不阻塞 Agent + 死代码）

- 状态：**完成（离线验证通过）**；未做真实 ASR 设备验证。不将 P7 整体标记完成。
- [checkpoint](checkpoints/2026-09-30-P7-agent-speech-path-fixes.md)。
- 修复一（ASR 逐句播报缺失）：`ConversationManager.submit_asr_final(..., on_text_chunk=None)` 新增并透传；`PushToTalkCoordinator` 新增可选 `realtime_speaker`；新增 `drive_turn_stream(speaker, submit)`（提交前启动流、以 `push_text_chunk` 作为回调、仅完整无失败 outcome 才 `finish`，否则 `cancel`），`accept_provider_event` 改用它。语音始终可选：speaker 缺失/启动失败/无 push 接口/finish 失败都只降级为不播报，绝不阻塞本轮对话或改变 outcome。
- 修复二（背压阻塞 Agent 线程，**行为变更**）：`RealtimeVoiceSession` 与 `StreamingVoiceSession` 的 `push_text_chunk` 永不阻塞；单次 push 整批接受（分句器已界定单批 ≤8 句），超过 pending 上限即置 `overloaded=True` 并**只拒绝后续 push**；已入队句子继续按序播完；新增只读 `overloaded`。此前该行为被 `test_http_pending_queue_never_exceeds_two` 以"输入必须背压"固定，本轮按计划第 11 节改写该测试为 `test_pending_bound_never_blocks_the_agent_thread`。
- 修复三：删除 `app/ui/text_shell.py` 中 `if eligible and stream is None` 内永为假的分支与 `stream_ok` 死变量，语义显式注释，`legacy` 行为不变。
- 证据：`tests/asr/test_stream_bridge.py` 9/9；TTS 460 tests OK（2 skipped）；全量 975 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0。
- 覆盖缺口：`submit_asr_final` 的 `on_text_chunk` 透传无单独测试（与 `submit_text` 共用 `_submit_routed` 分支）；真实 ASR 设备端到端未验证；`overloaded` 仅诊断暴露，未接入 UI/日志。
- 未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 6（barge-in、取消顺序与迟到块隔离）

- 状态：**完成（离线验证通过）**；未做真实插话/真实 socket 时序验证。不将 P7 整体标记完成。
- [checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch6-barge-in-cancellation.md)。
- 已完成：`app/tts/streaming_session.py` 的 `cancel()` 按计划第 12 节固定顺序执行且幂等（标记取消 → 停止提交 → `_cancel_event` 使 provider 关闭 socket / HTTP 放弃等待 → `_generation += 1` → 清 pending 与 worker 队列 → `worker.stop()` → 记录 stop/ACK → 迟到事件只写诊断）；新增只读 `generation`（每句 request 携带）与 `late_events`（形如 `pcm_chunk@generation2` 的脱敏诊断）；取消后到达的 PCM、传输错误、播放结果与"取消后才完成"的句子永不恢复播放。
- 跨回复隔离：新回复由新会话（新 `reply_id`、`generation=1`）驱动，`PCMPlaybackWorker`/`PCMChunkStreamValidator` 的 reply/segment/request/generation 四元组校验拒绝旧块（有直接测试）。
- 证据：`tests/tts/test_streaming_session.py` 24/24（新增 6 项）；全量 981 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0；2 个本批文件严格 UTF-8、无 BOM、无 `U+FFFD`。
- 边界与遗留：**未做真实 barge-in**（无真实麦克风插话、按键中断或播放中途取消），"取消后真实设备立即静音"未被证明，`STOP_ACKED` 仍只表示 backend 归还 buffer；未在真实 VoiceStudio 上验证取消时的 socket 关闭时序与 HTTP 迟到响应；`RealtimeVoiceSession`（legacy/http_wav）本批未改，因此没有 generation 与 `late_events` 诊断（迟到块仍由 provider 取消检查与 worker 身份校验拒绝），两套会话存在已知不对称；`late_events` 未接日志/事件总线；未做连续取消压力测试；Batch 7-8 未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 7（真实场景矩阵与重启恢复，7A）

- 状态：**7A 完成**；部分计划场景未覆盖（见下）。不将 P7 整体标记完成，也不进入 P8。
- [checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch7-real-scenario-matrix.md)；artifacts `checkpoints/artifacts/2026-09-30-P7-batch7-real-matrix/`（矩阵、过载修复重测、重启恢复共 3 个 JSON）。
- 真实矩阵：4/16/32/48 字 → 1/1/1/2 句（48 字被分句器切成 2 句）；2 句回复 2/2；真实 `connect_failed` → HTTP 接管 1/1；耗时 4.4–16.1 s、RTF 1.41–1.69（冷启动首句 10.78、重启后冷启动 1.97）；各路径 `cleanup_verified=true`、`uncertain=false`。
- 真实播放中取消（等待 3.5 s）：首句已合成 2.62 s 音频且已有 **11 次真实 `waveOutWrite`**；取消后 1 秒仍为 11 次 → **无新音频进入设备**；`late_events=["playback:STOP_ACKED@generation2"]`。
- 真实缺陷与修复：首轮矩阵 5 句只播 3 句、20 句只播 3 句；根因是把 `max_pending_segments=2` 当作过载阈值。现句数上限 8（仅防失控），过载信号改为 `PCMPlaybackBackpressure`（`PCMPlaybackWorker` 的 `max_queued_audio_ms=8000`）；重测 5 句 → 5/5、20 句 → 9/20（瞬间推入 20 句、播放需 50+ 秒，属刻意的过载保护）。新增 2 项测试。
- 重启恢复（场景 8）：本会话终止全部 VoiceStudio 进程（5×`VoiceStudio.exe` + desktop-bridge + uvicorn）并确认 3900 释放；**代理无法从 shell 启动该 GUI 应用**（两次 `Start-Process` 立即 exit 0、`/health` 180 s 未恢复，原因未确认，疑似单实例锁或非交互式会话限制），由用户手动启动；恢复后 `/health` 200，真实冷启动首句 `played=1`、5.3 s、音频 2.69 s、14 次 write，warm 第二句 4.3 s、音频 2.72 s → **下一轮恢复成立**。
- 未覆盖场景（如实登记）：首音前断线与播放中断线（需人为干扰 socket，未获授权）、真实 HTTP fallback 失败、Edge 真实播放（项目只有 `TTSFallbackRouter` 路由契约，**没有 Edge 播放实现**）、逐句 `sentence_ready→submit` 与 `first_PCM→device` p95（缺句级时间线打点，已在 artifact 的 `not_measured` 登记）。
- 证据：全量 discovery 983 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0。
- 事实边界：`speaker_acoustically_verified=false`、`user_heard=null`；`STOP_ACKED` 仍只表示 backend 归还 buffer；未从程序完成或设备调用推导听感。
- 遗留：Batch 8（默认切换、回滚与阶段验收）未开始；未提交、未 push。

## 2026-09-30：P7 VoiceStudio Batch 8（收敛、回滚演练与阶段验收）

- 状态：**Batch 8 可执行部分完成；默认切换与 P7 阶段完成均未执行**。P7 保持 **Blocked**，不进入 P8。
- [checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch8-rollback-and-stage-acceptance.md)。
- 已完成：**回滚演练**（`disabled`→`legacy`、`http_wav` 无需代码改动或注入即可构造 `CompletedReplySpeaker`；`ws_pcm`/`auto` 必须显式注入 `session_factory`，否则 `ValueError` → 回滚只需改配置）；**依赖与可复现记录**（`websockets==17.1`、wheel `websockets-17.1-cp312-cp312-win_amd64.whl`、SHA-256 `617243E1…412BEC`、BSD-3-Clause、`>=3.11`、零依赖、离线安装命令）；**忽略规则加固**（`/.env`、`/.venv/`、`__pycache__/`、`/models/`、媒体原本已忽略；补 `/.python312/`、`/.python312-embed.zip`、`/.wheelhouse/`、`/.get-pip.py`，`git check-ignore` 逐项确认，无 secret 泄露风险）；**P7 五项退出条件核对**。
- 未执行默认切换的原因：流式会话尚未接入默认 runtime（`app/main.py` 不创建 `StreamingVoiceSession`），计划要求"用户审阅验收后才切默认"，且退出条件仍有未满足项。
- P7 退出条件核对结果：均**未完全满足**。缺：(1) 默认 runtime 的流式接线；(2) **Edge 播放实现不存在**（仅有 `TTSFallbackRouter` 路由契约，无法真实切 Edge 出声）；(3) 真实"双路径失败→转文字"端到端未演练；(4) VoiceProfile 更新边界未触发验证；(5) 断线注入（首音前/播放中）与逐句 `sentence_ready→submit`、`first_PCM→device` p95 未做。
- 证据：全量 discovery 983 tests OK（2 skipped）；`compileall -q app tests tools` exit 0；`git diff --check` exit 0。
- 未完成清单（各需单独授权）：断线注入、真实 HTTP fallback 失败、Edge 实现（新功能）、p95 打点、流式会话接入默认 runtime、默认模式 `http_wav → auto` 切换、本轮全部改动提交（等用户审阅）。

## 2026-09-30：当前工作区审阅与待提交清单

- 审阅范围：P7 VoiceStudio 双路径 Batch 1-8 + Agent→说话链路修复（即本日全部产出）。
- 工作区总状态：`git status --short` 共 **342 条**（21 staged / 6 unstaged / 321 untracked）。项目至今只有 1 个 commit（`f2b112f`），**几乎全部源码仍是未跟踪状态**，因此 342 条不等于 342 个改动。
- 本批工作规模：**33 个文件**（6 个 `AM` + 2 个 `A` + 1 个 `A`(tests) + 22 个 `??` + `.gitignore`）+ **9 个 checkpoint**（batch1–8 与 agent-speech-path-fixes）+ **4 个 artifact 目录**（batch3 WS preflight、batch4 设备、batch5 真实流式、batch7 真实矩阵）。
- **审阅发现的关键风险（提交前必须处理）**：以下 6 个文件处于 `AM` 状态 —— **staged 里是 Batch 1 时期的旧版本，工作区已有 Batch 2-8 的后续改动**：
  - `app/config/settings.py`
  - `app/tts/providers/__init__.py`
  - `implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md`
  - `implementation_plan/P7-followup-execution-tracker.md`
  - `progress.md`
  - `task_plan.md`

  此刻若直接 `git commit`，提交的是**过时内容**并会丢掉后续改动。**提交前必须先对这 6 个文件重新 `git add`**，使索引与工作区一致。
- 建议的提交分组（功能交织，建议 1–2 个提交，不要按目录硬拆）：
  - **语音链路（主体）**：`app/tts/`（`providers/contracts.py`、`providers/fakes.py`、`providers/http_wav.py`、`providers/voicestudio_ws.py`、`providers/__init__.py`、`segmentation/__init__.py`、`real_playback.py`、`completed_reply.py`、`pcm_playback.py`、`waveout_pcm.py`、`transport.py`、`streaming_session.py`）、`app/config/settings.py`、`tests/tts/`（7 个文件）、`tests/foundation/test_config.py`、`implementation_plan/2026-09-29-voicestudio-dual-path-streaming-execution.md`、`implementation_plan/checkpoints/2026-09-30-P7-voicestudio-*`、`implementation_plan/checkpoints/artifacts/2026-09-30-P7-*`、`progress.md`、`task_plan.md`、本跟踪表。
  - **Agent→说话链路修复**：`app/asr/push_to_talk.py`、`app/conversation/manager.py`、`app/ui/text_shell.py`、`tests/asr/test_stream_bridge.py`、`implementation_plan/checkpoints/2026-09-30-P7-agent-speech-path-fixes.md`。
  - **仓库卫生**：`.gitignore`（新增 `/.python312/`、`/.python312-embed.zip`、`/.wheelhouse/`、`/.get-pip.py`）。
  - 注意：`app/tts/real_playback.py` 同时被 Agent 修复（背压语义）与 Batch 7（上限与过载信号修正）修改，两组若拆分必须保证顺序与相互依赖，**建议与之同批提交**。
- **明确不在本批范围（历史遗留，需单独审阅）**：`app/persona/`、`app/plans/`、`app/executor/`、`app/tools/`、`app/audio/`、`app/auditing/`、`app/verification/`、`app/runtime/`、`app/recovery/`、`app/retry/`、`app/state/`、`app/ui/`（除 `text_shell.py`）、`app/agent/`、`app/asr/` 其他文件、`tests/` 其他目录、`implementation_plan/checkpoints/` 其他日期文件、`implementation_plan/shared|research|discussions|archive/`、`docs/`、`tools/`。
- 安全复核：`/.env`、`/.venv/`、`__pycache__/`、`/models/`、`*.wav|*.mp3|*.log` 原本已被 `.gitignore` 覆盖；本轮补齐 4 条运行时忽略项并经 `git check-ignore` 逐项确认 → **staging 不会包含 secret、嵌入式运行时或 wheel**。
- 未执行：`git add`、`commit`、`push` —— 等用户确认提交范围后再做。（**已于同日执行，见下节**）

## 2026-09-30：首次完整入库与推送（GitHub 闭源仓库）

- 用户指令：把目前做好的上传到 GitHub 闭源仓库。
- 远端：`origin` = `https://github.com/yyyydssj/computer-voice-assistant.git`；推送前 `git ls-remote --heads origin` 返回空 → **远端原本没有任何分支**。
- **入库前安全预演**：`git add -n .` 显示将添加 480 个文件；敏感项探针（`.env`、`.venv/`、`.python312`、`.wheelhouse`、`*.whl`、`*.wav`、`__pycache__`、`*.log`、`models/`、`*.zip`、`.get-pip`）**全部为空**。
- 提交：`git add .` → staged **495 文件 / 4.77 MB**（无大文件）→ 提交 **`fdb0ce1`**「项目首次完整入库：P0-P7 源码、测试与实施文档」。
- 推送：`codex/review-playback-lifecycle` 与 `main` **均指向 `fdb0ce1`**（`git ls-remote --heads origin` 已复核）；本地 `main` 同步至同一提交；提交后工作区为 **0 改动 / 0 未跟踪**。
- **范围说明（重要修正）**：本次 `git add .` 把此前列为"不在本批范围"的历史遗留内容（`app/persona/`、`app/plans/`、`app/executor/`、`app/tools/`、`app/audio/`、`app/auditing/`、`app/verification/`、`app/runtime/`、`app/recovery/`、`app/retry/`、`app/state/`、`app/agent/`、`docs/`、`tools/` 等）**也一并入库**。理由是项目此前仅 1 个 commit、几乎全部源码从未被跟踪，只推本批 33 个文件会让仓库不完整、无法运行。该决定已如实登记。
- 闭源确认：GitHub API 未认证探测 `https://api.github.com/repos/yyyydssj/computer-voice-assistant` 返回 **404**；配合 `git push`（需认证且成功）判定仓库为 **private**。此为推论而非直接读取 `private` 字段——本机 `github.com` 被解析到非公网 IP，`web_fetch` 外网核验被安全策略拒绝。
- 推送后复核：全量 unittest 983 tests OK（2 skipped）；`git status` 干净。

## 2026-09-30：入库后的真实状态与剩余工作（对账章节）

> 本节为 2026-09-30 15:19 的只读对账结果；不改变任何批次的历史结论，也不授予真实网络/设备权限。结论：**P7 仍为 Blocked，未进入 P8。**

### 1. 已验证事实

- Git：`HEAD = 52893ca`（「记录首次完整入库与推送结果（含闭源确认与提交范围修正）」），`origin/main` 与 `origin/codex/review-playback-lifecycle` 同为 `52893ca`；`git status --short` **0 条**改动/未跟踪 → 此前“待提交清单”已关闭。
- 测试基线（本次复跑，非引用旧值）：`python -m unittest discover -s tests -t . -p "test_*.py" -q` → **983 tests, OK (skipped=2)**，耗时 3.3 s（`.venv` Python 3.12）。此数字只代表当前测试集合通过，不代表 P7 退出条件满足。
- P7 五项退出条件仍**全部未通过**：以本文件第四节清单与 [Batch 8 checkpoint](checkpoints/2026-09-30-P7-voicestudio-batch8-rollback-and-stage-acceptance.md) 第 3 节为准。

### 2. 剩余工作清单（每项各自前置授权，权限互不继承）

| # | 项 | 现状 | 前置条件 / 类型 |
|---|---|---|---|
| 1 | 首音前断线、播放中断线的真实故障注入 | **已完成（2026-09-30 批次 B10）**：首音前=真实杀后端（`connection_closed_before_done`→`NOT_COMMITTED`）；播放中断线=受控 loopback WS proxy 已接受音频后断线（`PLAYBACK_STARTED`，不重播/不换声） | 见第 9 节；无独立声学/听感证据 |
| 2 | 真实 HTTP fallback **失败**演练 | **已完成（2026-09-30 批次 B10）**：`http_wav` 真实连接拒绝 + `auto` 双路径均死 → `text/continue_text_no_replay`，0 句播放、文字保留 | 见第 9 节 |
| 3 | Edge 播放实现 | **已完成（2026-09-30 批次 B11）**：Edge 槽由**离线 Windows SAPI 声线**实现（无新依赖、不出外网），已接入会话并按路由契约单次切换；真实三条验收通过 | 见第 10 节；在线 edge-tts（微软云）仍**未实现**，属另一批 |
| 4 | 逐句 `sentence_ready→submit`、`first_PCM→device` p95 | **真实已采集（2026-09-30）**：`gate_to_device` p95 **0.0204/0.0229 ms 通过**（<50 ms）；`ready_to_submit` p50 20.79/29.23、p95 30.40/31.44 ms（ws_pcm/http_wav）**未通过**（<20 ms） | 见第 6、7 节；修复见第 9 项 |
| 5 | 流式会话接入默认 runtime（opt-in） | **暂缓**：未接线（`app/main.py` 不创建 `StreamingVoiceSession`）；用户 2026-09-30 决定保持 `disabled`，故当前无默认消费者 | 如将来要默认出声再单独授权 |
| 6 | 默认模式 `http_wav → auto` 切换 | **不适用（用户 2026-09-30 决定）**：保持 `TTS_VOICESTUDIO_MODE=disabled`，不切换为 `auto`；文档原假设（`http_wav`）与仓库实际默认不一致，已澄清 | 见第 11 节；如需变更须重新审阅 |
| 11 | 用户听感证据（路线图/双路径 DoD 第 11 项） | **已完成（2026-09-30）**：真实播放主路径 + 备用声线各一句，用户确认**两句都听到**（`user_heard=true`，非声学测量） | 见第 11 节 |
| 7 | 文档损坏修复（本次新发现） | **已修复（2026-09-30）**：15 个 `.md` 按证据重写并保留损坏原文；1 个 JSON artifact 不可恢复、本体未改并另附说明 | 见第 3 节；`.py`/`tests/` 源码未受影响 |
| 8 | VoiceProfile `speed` 口径对账 | **已结案（2026-09-30 只读对账）**：当前生效值可确定为 `1.0`；`0.80`/`0.85` 仅为历史差异，无法复原，见第 5 节 | 已执行；只读，未改任何配置或代码 |
| 9 | **B9-fix：把 provider 身份验证移出每句路径** | **已修复（2026-09-30）**：改为每次回复验证一次（回复边界即计划允许的边界），失败仍 fail-closed；真实复测 `ready_to_submit` p95 由 30.40 ms 降到 **0.236 ms** | 见第 8 节；新增只读 `verified` |
| 10 | 增补 `submit_to_first_pcm_ms`（合成/首音延迟）打点 | **已完成（2026-09-30）**：新增第 8 字段与第四段统计，并用真实数据完成 19.2 第 3/4 条对照 | 见第 8 节 |

### 3. 本次新发现的文档数据损坏：范围、修复方式与仍然不可复核的事项（2026-09-30 已修复）

- 范围（括号内为修复前文件内 `?????` 连续串出现次数）：`P7-followup-execution-tracker.md`(59)、`checkpoints/2026-09-28-P7-persisted-gentle-voice-selection-handoff.md`(90)、`checkpoints/2026-09-28-P7-T10-default-runtime-stop-failure-matrix.md`(82)、`checkpoints/2026-09-28-P7-T07-playback-stop-ack.md`(57)、`checkpoints/2026-09-29-P7-real-waveout-fault-evidence-handoff.md`(56)、`checkpoints/2026-09-29-P7-real-waveout-fault-shim-implementation-handoff.md`(51)、`checkpoints/2026-09-29-P7-real-waveout-fault-acoustic-handoff.md`(45)、`checkpoints/2026-09-28-P7-T09-acceptance-matrix.md`(40)、`checkpoints/2026-09-28-P7-T06-stage-acceptance.md`(38)、`checkpoints/2026-09-29-P7-collection-and-user-heard-judgment-handoff.md`(35)、`checkpoints/2026-09-28-P7-T08-opt-in-agent-reply.md`(31)、`checkpoints/2026-09-22-P7-batch27-audio-fact-event-history-consistency-handoff.md`(17)、`progress.md`(14)、`task_plan.md`(10)、`2026-09-28-P7-five-evidence-completion-handoff.md`(1)、`2026-09-27-P7-R-22-...-handoff.md`(1)；外加 1 个 artifact JSON（21 处）。
- 性质：中文被 ASCII `?` 替换（**逐字不可恢复**），在 `fdb0ce1` 中已损坏，git 无完好版本；`app/`、`tests/`、`tools/` 的 `.py` 源码全部未受影响。
- **修复方式（2026-09-30 执行）**：15 个 `.md` 分三类处理——① 近乎整篇损坏（T07/T08/T09/T10、persisted-gentle、waveOut 三件）：H1 后加「原文损坏登记」横幅 + 按代码/测试/artifact/相邻完好文档重写正文 + 文末 ````text 附录逐字保留损坏原文；② 局部损坏（`progress.md` 1605–1613、`task_plan.md` 509–516、collection 424–430、T06 20–29、batch27 第 154 行）：在原位置插入一行损坏声明 + 重写摘要 + 紧邻代码块保留损坏原文；③ 纯路径损坏（`<STUDY_ROOT>\??????`，共 3 处）：直接还原为 `<PROJECT_ROOT>`，不加横幅。
- **审计不变量（已脚本校验通过）**：修复前 HEAD 中每一行含 `?` 的损坏行，除 3 处故意修正的路径外，都仍逐字存在于工作区文件中（`diff` 无删除或仅删除围栏行）；T10 修复过程中一度丢失的 2 行完好命令（`.python312\python.exe -B -m unittest discover …`、`.python312\python.exe -m compileall …`）已在复检时从 HEAD 逐字补回；编码均为 UTF-8、无 `U+FFFD`；换行风格与修复前一致（CRLF 文件仍 CRLF、`task_plan.md` 仍 LF、`T06` 保持 BOM+LF）。
- **artifact 例外（未修改本体）**：`artifacts/2026-09-29-P7-agent-runtime-preflight/p7-agent-preflight-20260929T062315Z-71e501feb039.json` 的 4 处字段（`.request_text`、两处消息副本、1 处 system 消息）原文含中文，其记录的 `request_text_sha256=0655322f…` 与同目录未损坏姊妹件所用英文常量（`b7b97e5f…`）不同，且仓库、`.git`、`<USER_HOME>\.dsh`、该轮 DSH_HOME 与 `%TEMP%` 均无原文 → **不可逐字恢复**。按“校验不通过就不写入”的规则，artifact 保持字节不变，另在同目录新增 `2026-09-30-P7-agent-preflight-artifact-damage-note.md` 登记损坏字段、判定依据与仍可核验事实。
- **重写后仍然不可复核的事项（已在各文件内标注，不得补写为事实）**：`speed` 的历史差异（`0.80` vs `0.85`；**当前生效值已确定 = `1.0`**，见第 5 节）；collection 段中 `62 tests` 与 `1 skipped` 无任何旁证（同文件 12 节仅能佐证 `817 passed, 2 skipped`，两者 skipped 数不一致）；acoustic 批次「`loopback_output_stopped=false` 而 `loopback post_quiet=true` / 三字段同时 false」的原始推导规则（产生该 artifact 的旧版判定代码不在入库历史中）；T09 三列表头列名；T10 中 `Ran 753 tests in 1.990s` 的耗时来源。
- 引用这些文件时：正文是可复核证据重写的摘要，**不是原文**；需要逐字原文时只能看文末/紧邻的损坏原文代码块，并把它当作损坏数据而非可引用内容。

### 4. 下一入口（建议）

- 第 4 项已完成打点与真实采集（第 6、7 节）；**下一步优先第 9 项（把每句身份验证移出计时路径，即 19.2 第一条的真实修复）与第 10 项（补 `submit_to_first_pcm_ms` 跨度）**。
- 第 1、2 项（断线注入、HTTP fallback 失败演练）、第 3 项（Edge 播放实现，属新功能）、第 5、6 项（流式接入默认 runtime、默认切 `auto`）各自仍需单独评审。
- **真实链路授权状态（2026-09-30 用户明确）**：本项目内的真实链路无需逐次授权即可调动；但每个真实批次仍必须在本表/checkpoint 登记请求、播放、费用预算与副作用边界，并保持 fail-closed、不落盘音频、不使用麦克风（除非该批次明确需要）。
- 顺序可变，但：**P7 未通过退出条件前不标记 complete、不进入 P8。**

### 5. VoiceProfile `speed` 口径对账结论（2026-09-30，只读，第 8 项结案）

- **当前生效值 = `1.0`，可确定**：私有选择文件 `%LOCALAPPDATA%\ComputerVoiceAssistant\xiaobing_voice_selection.json`（179 B，mtime `2026-09-30 14:36:50`，即 Batch 5 重建该文件的那一刻）内容为 `{"profile_id":"e8572e8e","owner":"<user>","speed":1.0,"pause_ms":400,"fade_ms":80,"reference_sha256":"456896c0…","version":2}`；`app/tts/private_selection.py` 的 `DEFAULT_SPEED = 1.0`，`validate()` 只接受该值。
- **代码从未使用过 0.8/0.85**：`git grep` 全树、`git log -S` 与 `HEAD` blob 均确认 `private_selection.py` 内没有任何 `0.8x`；这两个数值只存在于文档。
- **`0.85` 的来源**：`docs/superpowers/specs/2026-09-28-xiaobing-voice-integration-design.md` 与 `2026-09-28-P7-xiaobing-real-preview-progress.md` 均记录「用户现场试播确认 `speed=0.85`」。
- **`0.80` 的来源**：只出现在 2026-09-28 那份后来损坏的持久化交接（正文锚点 `0.80`、同段字段锚点 `0.8`）；该轮 `.bak-20260928-085` 备份现已不存在，无法直接核对当时落盘值。
- **结论**：`0.85`（试播）与 `0.80`（持久化记录）之间 0.05 的差异**不可复原**，属历史差异；它**不影响当前行为**——当前代码、当前 live 选择文件、Batch 3/5 记录三者一致为 `1.0`。后续文档引用时按此口径，不得再把 `0.80`/`0.85` 当作现状。

### 6. 批次 B9：流式句级时间线打点与离线统计（2026-09-30，第 4 项离线部分完成）

- 状态：**完成（纯离线）**；**未联网、未访问 VoiceStudio、未用扬声器/麦克风、未接默认 runtime、未提交**；**真实 p95 未采集**。不改变 P7 的 `Blocked` 结论，也不进入 P8。
- 来源：[B9 checkpoint](checkpoints/2026-09-30-P7-b9-sentence-timing-instrumentation.md)；文档注记：双路径计划第 19.2.1 节。
- 改动范围（严格限定）：`app/tts/streaming_session.py`、`app/tts/pcm_playback.py`（**仅新增只读观测面**）、新增 `tests/tts/test_streaming_timing.py`（19 项）、计划 19.2.1 注记、B9 checkpoint。`app/main.py`、`app/config/settings.py`、`app/tts/real_playback.py`、`app/tts/completed_reply.py`、`providers/**`、`transport.py` **一行未改**。
- 五类单调时钟打点（可注入 `clock`，默认 `time.monotonic`）：`sentence_ready_at`（入队）、`tts_submitted_at`（交给 transport 前）、`first_pcm_at`（首块 PCM 返回后、取消检查之后）、`gate_open_at`（worker 起播缓冲闸门首次满足）、`device_submit_at`（首块交给 backend 成功之后）。worker 两处观测与 `begin_sentence` 重置共用同一把 `self._condition`，闸门时刻是在 `_started` 翻转点的**纯新增 if 块**内取样，未改起播阈值/背压/stop ACK/reset/fail-closed 语义。
- 只读统计面：`timing_samples`（每句一条冻结样本）、`timing_percentiles()` 返回三段 `count/p50/p95`；`nearest_rank_percentile()` 为纯函数，**最近秩法、不插值**，空样本为 `None`，缺失段不计入该段 `count`；样本只覆盖 `COMPLETED` 且未取消的句子，取消/迟到/失败不入样（只留既有 `late_events`）。
- **口径纪律（三处一致：docstring、计划 19.2.1、checkpoint）**：`ready_to_submit_ms` = 19.2 第一条（p95 < 20 ms）；`gate_to_device_ms = device_submit_at - gate_open_at` **是排除起播缓冲后“额外开销”的唯一可用段**（19.2 第二条 50 ms 只能用它的 p95 判定）；`first_pcm_to_device_ms` 与 `first_pcm_to_gate_ms` **含起播缓冲累积，仅作诊断，不得直接与 50 ms 比较**；闸门与提交同刻如实记 `0.0`。
- 证据（Lead 独立复跑，非引用）：`tests.tts.test_streaming_timing` **19/19 OK**；TTS discovery **487 tests OK (skipped=2)**；全量 discovery **1002 tests OK (skipped=2)**；`compileall -q app tests tools` exit 0；`git diff --check` exit 0。独立抽查：字段集精确为脱敏 id + int + float/None；`repr` 无文本/PCM/路径/URL 泄漏；纯函数行为与文档一致（`1..20` 的 p50=10、p95=19；`[1,2,3,4]` 的 p50=2 而非 2.5；非有限值被丢弃；`percentile=0` 抛 `ValueError`）。
- 未做与遗留：真实 20 句 warm 采集未执行（19.2 两条性能目标**仍未被证明**，本轮不声称任何性能收益）；统计面未接日志/事件总线/证据 ledger；未做超长回复与取消压力下的打点压力测试；`tests/tts/test_streaming_session.py` 现为 26 项（工作树/HEAD 事实，非 24 项），本批未修改该文件。
- 下一入口：如需真实口径，须单独授权一次真实采集（同一 VoiceProfile、同一文本集合、极小 `start_buffer_ms`，并登记请求/播放/费用预算）。**该项已于 2026-09-30 执行，见第 7 节。**

### 7. 真实链路采集：句级 p95（2026-09-30，真实证据）

- 状态：**已执行**（用户当日明确：本项目内真实链路无需逐次授权）。P7 仍为 `Blocked`，未进入 P8。来源：[B9 真实采集 checkpoint](checkpoints/2026-09-30-P7-b9-real-p95-measurement.md)；artifact 目录 `checkpoints/artifacts/2026-09-30-P7-batch9-real-p95/`（4 个 JSON）；新 runner `tools/p7_real_sentence_p95_runner.py`（无 `--live-opt-in` 则 exit 2）。
- 环境：VoiceStudio `0.5.6`、`/health` 200、`cuda (RTX 3050 Laptop)`；profile `e8572e8e`、`speed=1.0`、`pause_ms=400`、`fade_ms=80`、`version=2`。**本会话按打包版启动计划自行启动了后端**（GUI 18:07 已在运行但 3900 无监听）：`project\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 3900`，18:41 起 `/health` 200；日志 `%TEMP%\voicestudio-p7-backend-20260930.log`。预算：4 次真实运行 + 5 次验证计时，约 160 句真实合成/播放；外部网络 0、麦克风 0、音频落盘 0、费用 0。
- 结果（paced = 一句一句推入，pending ≤ 1，20 句全部播完）：

| 模式 | 时钟 | 播放 | 墙钟 | `ready_to_submit` p50/p95 (ms) | `gate_to_device` p50/p95 (ms) | 19.2 第一条 <20 ms | 19.2 第二条 <50 ms |
|---|---|---|---|---|---|---|---|
| `ws_pcm` paced | perf_counter | 20/20 | 61.56 s | 20.79 / 30.40 | 0.0134 / 0.0204 | **未通过** | **通过** |
| `http_wav` paced | perf_counter | 20/20 | 61.15 s | 29.23 / 31.44 | 0.0147 / 0.0229 | **未通过** | **通过** |

- **真实发现 A（口径缺陷）**：Windows `time.monotonic` = `GetTickCount64()`，分辨率 **15.625 ms**，实测 20 万次连续调用零正差值 → 用它打点时 `gate_to_device`/`first_pcm_to_device` 稳定显示 `0.000 ms`，那是**测不出来**。真实采集必须注入 `time.perf_counter`（QPC，1e-7 s）。库默认值保持不变，口径要求已写入计划 19.2.1 与 checkpoint。
- **真实发现 B（19.2 第一条根因）**：`StreamingVoiceSession._speak()` 在 `tts_submitted_at` 之前每句调用 `_verify_selection` → `verify_provider()` 每句做 **2 次 HTTP 往返**（`/profiles` + `/profiles/{id}/audio`，下载参考音频并 SHA-256）；真实实测单次 `18.4 / 29.7 / 56.4 ms`（min/p50/max），与实测跨度量级一致。**结论：不是线程抖动，而是每句外呼+哈希被计入跨度**；要达成 <20 ms 必须把验证移出每句路径（第 9 项）。
- **真实发现 C（过载保护，非缺陷）**：一次推入 5×4=20 句 → `overloaded=True`、只播 **9/20**、留下 `late_events=["playback:STOP_ACKED@generation2"]`（两次独立运行一致）；**单次 push 超过 8 句**直接抛 `streaming_queue_budget_exceeded`。真实 Agent 若一次性灌入整段长回复会截断后续语音（文字保留）。标准测量场景因此定为 paced。
- **真实发现 D（打点缺口）**：缺少 `tts_submitted_at → first_pcm_at`（合成/首音延迟）跨度，19.2 第 3/4 条（`auto` 与 `http_wav` 起播延迟比较）无法评估；本轮只能比较墙钟（61.56 s vs 61.15 s，几乎一致）。见第 10 项。
- 边界：`user_heard=null`、`speaker_acoustically_verified=false`（后端完成≠有人听见）；未做声学测量、未做断线注入/HTTP 失败演练、未接默认 runtime、未提交。
- **上述四条缺陷已于同日修复并复测，19.2 四条指标现全部通过——见第 8 节。**

### 8. B9-fix 批次：四个缺陷的修复与修复后真实复测（2026-09-30）

- 状态：**已修复并复测**（离线实现 + 真实复测）。P7 仍为 `Blocked`（其余退出条件未达成），未进入 P8。
- 来源：[B9 缺陷修复 checkpoint](checkpoints/2026-09-30-P7-b9-defect-fixes.md)（含第 9 节真实复测）；新 artifact 3 个在 `checkpoints/artifacts/2026-09-30-P7-batch9-real-p95/`。
- **A 时钟分辨率**：新增 `app/tts/clock.py`（Windows 用 `time.perf_counter`/QPC，其他平台 `time.monotonic`，附只读 `clock_resolution_seconds()`），并把它设为 `StreamingVoiceSession`、`build_streaming_session`、`PCMPlaybackWorker`、`VoiceStudioTransportRouter`、`WaveOutPCMStreamBackend` 的默认时钟（显式注入优先级不变）。Lead 独立复核：本机 `monotonic_seconds is time.perf_counter` 为真、分辨率 `1e-07`（`time.monotonic` 为 `0.015625`）、五处默认值全部切换。
- **B 每句验证 → 每回复一次**（**行为变更**）：`_verify_once()` 在回复开始时验证一次；失败仍走原 fail-closed 路径（记录 `_error`/`_failed`、不播报、无样本）；新回复由新会话重新验证一次。只读 `verified` 暴露事实。
- **C 过载可诊断**：被拒推送不再静默——`dropped_pushes` 计数 + `late_events` 里 `push_rejected_<closed|failed|cancelled|finished|overloaded>@generationN`；新增只读 `pending_sentences`；`finish()` 在 join 到期而语音仍在飞时置 `finish_timed_out=True` 并记 `finish_timeout`，**不得**读作“已全部说完”。防失控上限与 `close()` 取消语义未改（产品决策，未调阈值）。
- **D 合成/首音跨度**：`SentenceTimingSample` 追加 `submit_to_first_pcm_ms`（第 8 字段）、`StreamingTimingPercentiles` 追加 `submit_to_first_pcm`（第 4 段）；前 7 字段与前 3 段顺序/含义不变。
- **修复后真实复测（同环境、paced、20 句、`perf_counter`、各模式 20/20 播完、`verified=True`、`dropped_pushes=0`、`finish_timed_out=False`）**：

| 模式 | `ready_to_submit` p50/p95 (ms) | 修复前 p50/p95 | `submit_to_first_pcm` p50/p95 (ms) | `gate_to_device` p95 (ms) |
|---|---|---|---|---|
| `ws_pcm` | **0.0919 / 0.2357** | 20.79 / 30.40 | 1446.8 / 1580.0 | 0.0241 |
| `http_wav` | **0.0985 / 0.1734** | 29.23 / 31.44 | 1488.7 / 1614.6 | 0.0290 |
| `auto` | **0.1083 / 0.1999** | （未测） | 1462.3 / 1739.9 | 0.0155 |

- **19.2 四条指标判定（真实数据）**：① `ready_to_submit` p95 < 20 ms **通过**（最差 0.236 ms）；② `gate_to_device` p95 < 50 ms **通过**（最差 0.029 ms）；③ `auto` vs `http_wav` 中位起播延迟不恶化 >10% **通过**（`submit_to_first_pcm` p50：1462.3 vs 1488.7 ms，auto 反快 1.8%；p95 恶化 7.8%，如实登记）；④ **不声称 WebSocket 加速**（`ws_pcm` p50 1446.8 vs `http_wav` 1488.7 ms，仅快 2.8%；价值在连接复用/统一 PCM 播放/兼容性）。
- 顺带事实：`submit_to_first_pcm` 真实 p50 ≈ 1.45–1.49 s，印证 VoiceStudio 仍“先生成整句再出音”（计划第 3.2 节）；本轮只度量不优化。
- 边界：`user_heard=null`、`speaker_acoustically_verified=false`；未做声学测量、未接默认 runtime、未提交。
- 说明：第 8 节写“未做断线注入/HTTP 失败演练”仅指该批次范围；这两项已于同日由第 9 节完成。

### 9. B10 批次：真实传输故障演练（2026-09-30，第 1、2 项收口）

- 状态：**5 个演练全部完成**。P7 仍为 `Blocked`（Edge 与默认接线未落地），未进入 P8。
- 来源：[B10 checkpoint](checkpoints/2026-09-30-P7-batch10-real-transport-faults.md)；runner `tools/p7_real_transport_fault_runner.py`（无 `--live-opt-in` exit 2）；artifact 8 个在 `checkpoints/artifacts/2026-09-30-P7-batch10-real-transport-faults/`（含 3 个中间过程件，权威件见 checkpoint 第 2 节）。
- 环境：真实 VoiceStudio `0.5.6`（`/health` 200、CUDA RTX 3050）；**只杀本会话自己启动的 uvicorn 后端**，每次杀后立即重启并复核 `/health` 200；未触碰 GUI/桌面桥/其他进程。预算：仅 loopback 请求、无外部网络、无费用、音频不落盘、未开麦克风。

| # | 场景 | 故障方式 | 播放 / 句状态 | 路由决定 |
|---|---|---|---|---|
| 1 | `dual_path_dead` | `auto`：WS+HTTP 均无监听（真实连接拒绝） | 0 句；`NOT_COMMITTED` | `text / continue_text_no_replay`，`replay=False` |
| 2 | `http_dead` | `http_wav`：HTTP 无监听 | 0 句；`NOT_COMMITTED` | 同上 |
| 3 | `kill_before_audio` | 真实服务被杀（首块 PCM 前，socket 未收 `done`） | 0 句；`NOT_COMMITTED` | 同上 |
| 4 | `kill_after_playback` | 真实服务被杀：第 1 句**已播完**后推第 2 句 | `completed_playback_sentences=1`；`segment-2=NOT_COMMITTED` | 同上（无重播/无换声） |
| 5 | `proxy_drop_mid_sentence` | 受控 loopback WS proxy：`start`+3 块 PCM 后断线 | 已接受音频；**`PLAYBACK_STARTED`** | `text / continue_text_no_replay`，`replay=False`，`drop_remaining=True` |

- 结论：**第 2 项（真实 HTTP fallback 失败）通过**——真实连接拒绝即转文字、0 句播放、`replay_current_segment=False`；**第 1 项（断线注入）通过**——首音前真实杀服务得到 `connection_closed_before_done`/`NOT_COMMITTED`，已出音后断线得到 `PLAYBACK_STARTED` 且**不重播、不半句换声**、剩余语音停止、文字继续。
- 过程发现（已写入 checkpoint）：provider 会**滞留一帧**（只发一块再断线时该块从未被接受，故“已出音后断线”至少需两块）；后端冷启动时过短的句超时会误判（runner 增加 warm-up + 默认 18 s 超时 + 检查 `session.error`）；`finish()` 按设计重抛记录的错误，故 artifact 另记 `completed_playback_sentences`，避免把“异常中断的返回值 0”误读为“没播过”。
- 边界：`user_heard=null`、`speaker_acoustically_verified=false`——**“用户是否听到半句”未被证明**；未做 half-open/丢包级注入；HTTP fallback 成功后再失败、并发取消压力未覆盖；未提交。

### 10. B11 批次：离线 Edge 声线（双路径第二 Provider，2026-09-30，第 3 项收口）

- 状态：**已完成并通过真实三条验收**。P7 仍为 `Blocked`（默认接线与阶段验收未做），未进入 P8。
- 来源：B11 checkpoint（`checkpoints/2026-09-30-P7-b11-offline-edge-voice.md`）；验收 runner `tools/p7_real_edge_fallback_runner.py`（无 `--live-opt-in` exit 2）；artifact 3 个在 `checkpoints/artifacts/2026-09-30-P7-b11-offline-edge-voice/`；实现注记已写入双路径文档 13.2 节。
- **选型（用户 2026-09-30 决定）**：Edge 槽用**离线 Windows SAPI 声线**（`Microsoft Huihui Desktop` zh-CN）实现——无新依赖、不出外网、SAPI 原生输出 24000 Hz/16-bit/mono PCM；**在线 edge-tts（微软云）未实现**，属另一批（需先给依赖/许可/出网隐私方案）。
- 实现：`app/tts/providers/edge_sapi.py`（`EdgeSapiConfig`/`EdgeSapiVoiceProvider.stream_sentence`/`list_edge_sapi_voices`，runner 可注入，失败短代码、临时文件必清理、诊断不含文本/路径）+ 纯 ASCII 脚本 `app/tts/providers/edge_sapi_runner.ps1`（PowerShell 5.1 的 ANSI 坑：脚本纯 ASCII、文本经 UTF-8 文件传入）+ `TTS_EDGE_VOICE` 配置（默认空=未配置 Edge）+ 会话 `edge_provider` 接线（`StreamingVoiceSession`/`build_streaming_session`）。
- 接线语义（按双路径文档 13.2）：**仅当**该句尚未出音（`NOT_COMMITTED`）且路由决定 `USE_EDGE_CURRENT_SEGMENT` 时，用 Edge 重做**同一句一次**；**已出音（`PLAYBACK_STARTED`）绝不切 Edge、绝不重播**；Edge 也失败 → `edge_failed()` → 文字，**不尝试第三个 Provider**；只读诊断 `edge_attempts`/`edge_failures`/`late_events` 的 `edge_used@generationN`、`edge_failed@generationN`；`edge_provider=None` 时行为逐位不变。
- 真实三条验收（同机、真实设备出声）：
  1. `edge_direct`：离线声线直接合成并播放 1 句 → `chunks=17`、`playback_state=COMPLETED`、首块 PCM 0.42 s、墙钟 3.69 s；
  2. `edge_fallback_after_kill`：**真实杀掉 VoiceStudio 后端**（首块 PCM 前）→ `NOT_COMMITTED` → `route=edge/use_edge_current_segment`、`edge_attempts=1`、`late_events=['edge_used@generation1']`、`completed_playback_sentences=1`、`error=None`——**该回复改用离线 Edge 声线真的播出，而不是退化成文字**；
  3. `proxy_drop_no_edge`：受控 proxy 在已接受音频后断线 → `PLAYBACK_STARTED`、**`edge_attempts=0`**、`route=text/continue_text_no_replay`、`replay_current_segment=False`（`next_reply_route=edge` 只影响下一轮）。
- 离线证据：`tests/tts/test_edge_sapi.py`、`tests/tts/test_streaming_edge_fallback.py` 共 46 项（1 个真实门跳过）；全量 **1065 tests OK (3 skipped)**；`compileall` exit 0；`git diff --check` exit 0；静态复核：`edge_sapi.py` 仅 import 标准库（无 socket/urllib/http），`subprocess` 只用参数列表、无 `shell=True`、stdin=DEVNULL、`CREATE_NO_WINDOW`，`.ps1` 非 ASCII 字节数 = 0。
- 已知取舍：Edge 渲染是"先完整产出 WAV、再按 4800 samples 分块 yield"（SAPI 单句约 0.4 s），因此"流中取消"落地为**有界超时回收子进程 + 丢弃已渲染音频 + 清理两临时文件**，不是渲染中途即时 kill——无遗留进程，但长文本取消时延未测量。
- 边界：**离线本地声线不是微软 Edge 神经声线**（命名与文档如实标注）；`user_heard=null`、`speaker_acoustically_verified=false`；未接默认 runtime、未做默认切换、未提交。
- 下一入口：双路径文档 §24 收口（另见 `checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md`）→ 用户听感证据 → 默认切换（须用户审阅验收）。**该三项已于同日全部处理，见第 11 节。**

### 11. 用户听感证据与默认模式决策（2026-09-30，双路径 DoD 第 11、14 项收口）

- **用户听感证据（DoD 第 11 项）已完成**：用 runner `tools/p7_real_user_heard_runner.py`（无 `--live-opt-in` exit 2）真实播放两句——
  1. **主路径**：VoiceStudio WebSocket PCM（小饼声线 profile `e8572e8e`）1 句完整播放，5.395 s；
  2. **备用声线**：离线 Edge 槽（SAPI `Microsoft Huihui Desktop`）32 块、`COMPLETED`，6.666 s。
  - artifact：[原始播放件](checkpoints/artifacts/2026-09-30-P7-user-heard/p7-user-heard-20260930T112602Z-f840ea69258a46d2.json)（`user_heard=null`，记录机械事实）+ [用户确认件](checkpoints/artifacts/2026-09-30-P7-user-heard/2026-09-30-P7-user-heard-confirmation.json)（`user_heard=true`，主路径与备用声线都听到；`confirmed_by=user`，2026-09-30T11:27:11Z）。
  - **原始 artifact 未被修改**（历史 artifact 不可改）；确认另立新文件。
  - 边界：这是**一次人为确认**，不是声学测量（`speaker_acoustically_verified=false`），不得推广到其它文本、音量、设备或环境。
- **默认模式决策（DoD 第 14 项）**：用户 2026-09-30 审阅后决定**保持 `TTS_VOICESTUDIO_MODE=disabled`，不切换为 `auto`**。因此：不涉及"默认开启语音"；tracker 第 5 项（流式接入默认 runtime）暂缓、第 6 项标为**不适用（附理由）**；`settings.py` 默认值未改动（本批只新增 `TTS_EDGE_VOICE`）。
- 双路径文档 §24 现状：**14 项全部处理完毕**（13 项满足 + 第 14 项经用户决定不切换），逐条证据见 [DoD 对账 checkpoint](checkpoints/2026-09-30-P7-dual-path-plan-dod-status.md)。
- 仍未做（与本文档无关的更大范围）：P7 路线图五项阶段退出条件的独立验收与 P8，均须另行授权；本轮全部改动未提交。**（更正：P7 五项退出条件已于同日通过，见 T-06 行与第五节；本行保留作历史。）**

### 12. P0–P6 跨阶段复核与全量基线更正（2026-09-30）

- 来源：[P0–P6 跨阶段复核](checkpoints/2026-09-30-P0-P6-cross-stage-recheck.md)。
- 结论：**P0–P6 七个阶段全部仍然成立**，无功能性回归；2026-09-30 的改动只落在 `app/tts/**` 与 `app/config/settings.py`（仅新增 `TTS_EDGE_VOICE`），未触碰任何 P0–P6 模块。各阶段对应测试分组全部通过（foundation 61、contracts 44、conversation 19+persona 5+agent 12、tools 30+plans 23+safety 22、security 81+retry 9+state 5+providers 38+evidence 39+recovery 3、asr 38+audio 57、tts 550、routing 59、integration 12）。
- **发现并修复 1：42 项测试从未被收集**（覆盖率缺口）。`tests/evidence/`（39 项）与 `tests/recovery/`（3 项）缺 `__init__.py`，标准 `unittest discover -s tests -t .` 不收集它们；单独按模块路径运行 42/42 全 OK。已补齐两个空 `__init__.py` → **全量基线由"1065"更正为 1107 tests OK (2+1 skipped)**。
  - 影响与口径：今天（及此前）各 checkpoint 中的"全量 719/773/983/1019/1065"是**当时实际被收集的数量**，不是可执行测试集的全集；历史 checkpoint 不追改，本行与复核文档登记更正。
- **发现并修复 2：路线图 P3/P4/P5 卡片缺 `> 阶段状态：` 行**（其完成状态此前只存在于 P1–P4/P5 集成验收文档）；已补状态行并指向验收文档与 handoff。
- 复核命令：见复核文档第 5 节（全量 1107 OK；`compileall` exit 0；`git diff --check` exit 0）。
