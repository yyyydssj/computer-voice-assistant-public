# P7 T-01：退出条件与测试矩阵冻结交接（2026-09-27）

> 状态：**T-01 文档冻结完成；P7 五项阶段退出条件仍未验收**。本批只读调研本地源码/测试及两份固定 commit 的 GitHub 上游源码，仅新增本交接、更新 P7 跟踪表；不改应用/测试代码、不运行应用/测试、不访问凭据、真实 Provider 或播放设备。取证日期 2026-09-27。T-02、T-03、T-04、T-05、T-06 和 R-24 各自另行授权。

## 1. 基线、证据级别与上游源码核查

- 阶段标准：`implementation_plan/implementation-roadmap.md` P7「退出条件」五项；`implementation_plan/02-tts-voicestudio.md` 的五层事实、设备回执、故障和文字回退要求。前序：[G0 差距交接](2026-09-27-P7-G0-exit-gap-baseline-handoff.md)、[Batch 95 只读路由观察交接](2026-09-24-P7-batch95-routing-rebuild-observation-frozen-router-snapshot-boundary-audit-handoff.md)、[R-23 离线边界交接](2026-09-27-P7-R-23-offline-preflight-startup-boundary-audit-handoff.md)。R 支线不等于 TTS 验收。
- 本轮 **重新阅读固定提交**（不声称已确认 GitHub 当前最新版本，不安装依赖）：[LiveKit Agents `8fc2650d…` 的 fallback_adapter.py](https://github.com/livekit/agents/blob/8fc2650deddaa12727a3d7a7e544d1e3a95c1315/livekit-agents/livekit/agents/tts/fallback_adapter.py) 的 `pushed_duration()>0` 是**已向上游输出音频**的边界，出现后该片段不继续 fallback；`max_retry_per_tts` 默认 2，流恢复路径会复制已输入文本进行探测。此处**不是本项目设备开始播放/用户听见的回执**；不能继承默认重试或用私密文本做探测。
- 本轮重新阅读 [Pipecat `3dede06b…` 的 service_switcher.py](https://github.com/pipecat-ai/pipecat/blob/3dede06bec0b497bddfdcf047af7495ee9d0726e/src/pipecat/pipeline/service_switcher.py)：failover 策略仅在服务不可用时切换，恢复策略留给应用；该服务切换器不能替本项目提供设备播放事实或旧句禁止续播保证。2026-09-18 的既有 [TTS GitHub 对比](../research/tts-fallback-github-comparison.md) 仅作历史背景；本轮固定源码核查不替代 T-03 对真实 VoiceStudio/Edge API、版本、授权、计费、隐私与设备兼容性的官方评审。
- 证据层级必须分开记录：① 本地领域契约/单测；② T-02/T-05 假 Provider/假设备编排回执；③ 经 T-03 前置评审与 T-04 专项授权的真实 Provider/设备回执；④ 用户确实听到的可核验观察。③ 不自动证明④，①②不自动证明③。**状态名 `PLAYED`、音频生成、入队、上游输出音频都不能单独声称真实设备播报或用户听到。**
- 历史 Batch 95 TTS **220 passed**、R-23 全量 **678/678 passed** 是各自批次记载；本 T-01 **未重跑测试**，不把历史结果冒充本批实测。

## 2. 五项退出条件：证据与验收矩阵（每项仍为未通过）

| 路线图条件 / 状态 | 已有本地证据（仅①） | 假设备与故障注入所需证据（②） | 真实链路及阶段验收所需证据（③/④） |
|---|---|---|---|
| 1. 已确认文字按句原序实际播放 / **未通过** | `app/tts/segmentation/__init__.py`、`tests/tts/test_segmentation.py` 有完整句、序号、取消；`app/tts/playback/queue.py`、`tests/tts/test_playback_queue.py` 有单回复 FIFO、连续 request 序列和预算背压；`app/main.py` 仍是文字壳。 | T-02 用固定非敏感的**已确认**文字，跨文字→分句→request→lifecycle→队列→假设备回执贯通；逐句比对 `reply_id/segment_id/provider_request_id/provider_generation/voice_profile_version`、原文、次序、背压拒绝不丢不重；回执与元数据一一绑定。 | T-03 先确定设备/格式/错误语义；T-04 在许可预算内记录每句设备提交、开始、进度、完成或停止及观察方式；T-06 核对实际原序与“设备报告完成≠用户已听到”，无真实证据不得勾选。 |
| 2. 提交设备前可按配置切 Edge；已开始播放的当前句不换声 / **未通过** | `app/tts/routing.py`、`tests/tts/test_routing.py` 与 `test_routing_binding.py` 只给本地决策；`app/tts/playback/__init__.py` 的提交/开始/进度是元数据调用，不操作设备。 | T-02/T-05 注入未配置 Edge、合成前失败、队列前失败、设备提交竞态、已开始/半句失败、Edge 失败；对每次决策核对实际取消/停止及没有混声、整句重播；失败后文字可用。**本地 `COMMITTED_NOT_STARTED` 不等于“设备未提交”：这时禁止宣称满足“提交前切 Edge”；若设备已提交须先证明撤销/停止确实完成且零播放，否则 fail-closed 转文字。** | T-03 评审预先试听/配置、格式、设备回执和撤销能力；T-04 在严格次数限制下核查设备提交线性化点和开始回执；T-05/T-06 校验只在证实的安全窗口切换、播放后不跨音色续同句。 |
| 3. 被打断旧回复不跨 Provider 续播 / **未通过** | `ReplySegmenter.cancel`、`PlaybackQueue.cancel` 清本地缓冲/关闭旧回复；生命周期 generation fence、`app/tts/routing.py` 的 `drop_remaining_segments` 为本地约束；清队列**不停止设备/Provider**。 | T-02/T-05 故障注入取消与回执交错、迟到音频/完成事件、旧 generation、队列已激活；须有 Provider 取消、设备停止、队列清除及迟到拒绝的分层回执，旧 `reply_id` 从此不可出声；禁止旧句在 Edge 上续播。 | T-04 获授权后检查真设备可观察的停止/无补播；T-06 再核对旧回复与新回复隔离。 |
| 4. TTS 故障不改变任务、授权、计划和结果事实 / **未通过** | `app/tts/` 本地契约不等于跨模块验收；P5 既有集成验收不能证明新 TTS 接线无影响；`app/main.py` 目前文字可用。 | T-02/T-05 在 TTS 超时、掉线、半句失败、双 Provider 故障、取消和重入前后比对 task、plan、authorization、ledger、executor、verification、audit **原始可观察事实**；没有新的授权/执行副作用；文字响应仍完整可达。 | T-04 有界真实故障观察；T-06 跑 P2/P3/P5 相关回归与阶段端到端，禁止用“还没接线”代替故障隔离证据。 |
| 5. VoiceProfile 仅在允许的句/回复边界生效 / **未通过** | `app/tts/domain/voice_profile.py` 与 `tests/tts/test_voice_profile.py` 有 USER 权限、不可变版本及会话快照；`ProviderRequestRegistry._validate_voice_session` 要求快照仍是当前已选择版本；无实际播放时切换证据、试听确认回执。 | 安全冻结语义：一旦某句 request 已准备/提交/播出，该句的 profile 版本不可更改；默认将**下一新回复**作为最保守的生效边界。若后续主张同回复下一句可变，须 T-02/T-05 单独定义并证明：上一句终态、新句尚未准备、显式用户确认版本、没有队列或旧音频复用，且并发更新 fail-closed；不能从当前会话快照直接推断支持。测试未选择/禁用、非 USER 修改、版本过期、准备后修改和迟到回执。 | T-03 评审试听/用户确认与目标音色实际映射；T-04/T-05 核验可听边界、已播放句不变声；T-06 复核权限、版本与生效点。 |

## 3. 阻断 T-02/T-05 的具体契约接缝，不以路由决定冒充执行

1. `ProviderRequestRegistry.prepare()` 的 `_requests_by_pair` 拒绝同一个 `(reply_id, segment_id)` 再次准备请求；本地路由 `primary_failed()` 的 `USE_EDGE_CURRENT_SEGMENT` 只是决策，**没有实现 VoiceStudio→Edge 同句请求重建、取消/隔离及身份迁移**。T-02 方案必须先定义 request 身份、唯一性、句段身份和旧 generation 迟到拒绝的兼容策略；不可绕过检查或复用旧 request id，未解决则记录阻塞，不把切换算通过。
2. `PlaybackQueue` 以 request sequence 连续入队并拒绝重复 request；`cancel()` 清元数据但不取消 Provider/停止设备；`begin()`/`finish()` 不自动生成真实播放回执。T-02 需先界定单一“当前回复”和终态；真设备提交/撤销行为留 T-03/T-04 验证。未经专项确认不修改现有队列契约。
3. 路由中 `NOT_COMMITTED` 与 `COMMITTED_NOT_STARTED` 均可选 Edge，是本地“尚未开始播放”规则；路线图更严格的“**未提交播放设备前**”为阶段验收门槛。后续需用设备适配器回执判定安全提交窗口；若提交已发生且不能证实无声撤销，则按保守分支停止本句语音、保留文字，不能直接兑现当前句 Edge。
4. VoiceProfile 的 `bind_session()` 为快照，但 request 准备时要求版本仍为当前选择版本；不能宣称“已绑定旧快照可跨所有后续句持续使用”。T-02 明确回复/句段冻结时点和并发更新冲突处理，T-05 注入版本漂移；遇不一致拒绝或降级，不能静默变声。

## 4. 后续批次入口与停止条件

- **T-02（另获授权）**：仅本地假 Provider/设备；候选最小范围 `app/tts/` 新编排接缝、对应 `tests/tts/` 与必要的纯文字入口，具体文件/fixture/设备接口/失败回退上限、兼容迁移与专项测试须 T-02 单批调研冻结。优先做矩阵②与第 3 节四处接缝；禁止从本交接直接接真实设备、网络或修改安全链。若不可安全贯通，记录阻塞并保留文字模式。
- **T-03（另获授权，只评审）**：核对真实 VoiceStudio/Edge 官方版本与接口、本机依赖和设备、凭据、费用、隐私/日志脱敏、格式和延迟、播放提交/停止回执、失败回滚及非敏感固定探测材料；未经新专项授权不能实测。T-04 仅在该门通过且得到 Provider/设备有界授权后执行；设备不可用即明确阻塞，不拿②替代③。
- **T-05、T-06（各自另获授权）**：先完成故障/边界回归，再汇总五项逐条验收；保留路线图最终清单全不勾选直至阶段证据充足。R-24 DeepSeek transport 决策门独立；不因 TTS 工作开启 R-25/R-26、默认 Agent 或 P8。
- 本批非目标：第 96+ 次无新增验收收益的路由只读审计、真实 Provider 请求/密钥/计费、占用设备或音频试听、ASR/播放期间监听/AEC/全双工、自动更改 VoiceProfile、工具执行或电脑副作用。未经授权不 commit/push/reset/bulk-stage。

## 5. 本批验证与工作区归属

- 本轮只核对本地源码/测试/文档以及上述两个 GitHub **固定 commit** 的相关源码；没有运行 unittest、compileall、应用、网络 Provider 或设备。上游 GitHub 读取仅针对公开源码，不含任何凭据/真实服务调用。
- 本批允许写入：本 checkpoint、`implementation_plan/P7-followup-execution-tracker.md` 中 T-01 台账行；不修改路线图、`task_plan.md`、`progress.md`、旧 handoff 或应用/测试文件。写入后检查 UTF-8、引用存在、台账唯一、Git 精确变更范围。
- 预先存在的 staged 新文件、`app/routing.py` staged 修改及大量 untracked 项目文件均为工作区基线，不归属 T-01；不得整体 stage/commit。