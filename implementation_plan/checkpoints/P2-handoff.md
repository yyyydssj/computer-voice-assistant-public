# P2 交接：跨模块契约、事件和状态模型

> 交接日期：2026-09-19  
> 本文件是 P2 完成后的唯一接续入口之一；当前停在 P3 授权门前。

## 阶段编号

P2

## 阶段状态

complete

## 本阶段唯一目标

建立后续模块共同使用的严格、可版本化、可序列化数据契约，统一事件身份与边界判定，并把任务、Provider、输入和输出保持为四套正交状态机；在接入任何真实 Agent、工具或音频组件前，先用测试证明旧授权、旧音频、旧请求和不确定执行事实不能跨边界复用。

## 本阶段明确不做

- 不调用或接入 DeepSeekHarness、其他 Agent 或外部模型 API。
- 不实现 P3 文字聊天、persona、流式回复或会话容器。
- 不实现 ToolRegistry、PathGuard、PolicyEngine、ActionPlan 执行器或电脑副作用动作。
- 不实现真实审计持久化、自动恢复、自动续跑、自动重试、自动补做或自动回滚。
- 不接入麦克风、ASR、TTS、VoiceStudio、唤醒词、VAD、AEC 或音频设备。
- 不实现 Skill、Windows 常驻、打包、发布或自动更新。
- 不执行 Git 暂存、commit 或 push。

## 进入条件

- P1 已完成并生成 `checkpoints/P1-handoff.md`。
- 用户已于 2026-09-19 明确授权进入 P2，并在同日授权继续批次 3、批次 4。
- P0 冻结的身份、授权、状态正交、恢复和降级安全规则保持不变。
- 本阶段只允许修改 `app/contracts/`、`app/events/`、`app/state/`、对应测试和阶段进度文档。

## 允许修改的文件范围

- `app/contracts/`
- `app/events/`
- `app/state/`
- `tests/contracts/`
- `tests/state/`
- `implementation_plan/checkpoints/P2-handoff.md`
- `implementation_plan/implementation-roadmap.md`
- 根目录 `task_plan.md`、`progress.md`

没有创建 `app/agent/`、`app/conversation/`、`app/persona/`、工具执行器、真实音频模块或其他未来阶段实现目录。

## 依赖的稳定契约

1. 标识链使用 `session_id`、`utterance_id`、`request_id`、`task_id`、`plan_id`、`execution_id`、`action_id`、`attempt_id` 等受限标识符。
2. 所有 P2 核心对象采用 schema version 1、严格字段集合和 UTC 时间校验；未知字段、缺失字段和不支持版本必须拒绝。
3. 授权快照绑定计划 ID、版本、哈希、目标状态哈希、动作集合、风险等级、确认窗口、授权方式、有效期和一次性消费状态。
4. `request_id` 一经提交，不因传输、ASR 或 TTS 故障释放或允许隐式重发。
5. 副作用事实必须绑定 `execution_id + action_id + attempt_id`，旧 fencing token 和 attempt 身份重绑定必须拒绝。
6. `state_uncertain` 不能自动改写为成功、失败、可重试或可回滚。
7. 音频事件边界由 session、audio session、provider generation、stream、utterance、sequence 和 event ID 共同限定。
8. TTS 播放状态只表达可观测事实，不表达“用户一定听见”。

## 实现批次

### 批次 1：严格版本化契约与核心对象

- 建立 `StrictContract`、`ContractError`、严格 JSON 编解码和 schema version 1。
- 建立 19 个冻结且使用 slots 的核心 dataclass：`SessionContext`、`Utterance`、`AgentRequest`、`AgentResponse`、`TaskContext`、`ActionPlan`、`PlanVersion`、`AuthorizationSnapshot`、`ExecutionLedger`、`ActionAttempt`、`ActionResult`、`ResultVerification`、`AudioFrame`、`AudioSession`、`ProviderState`、`TTSReply`、`TTSSegment`、`PlaybackFact`、`AuditEvent`。
- 覆盖往返序列化、未知字段、缺失字段、版本、标识、时间和枚举拒绝路径。

### 批次 2：授权、事件闸门与正交状态机

- 授权快照实现精确事实匹配、有效期和单次消费。
- 统一 `EventEnvelope` 和 `EventGate`，拒绝重复、乱序、跨 session、旧 audio session、旧 generation、旧 stream。
- 建立任务、Provider、输入、输出四套独立枚举和显式转换图；非法转换抛出 `StateTransitionError`。
- 建立 `RequestSubmissionGate` 的最小边界。

### 批次 3：迁移、请求边界、执行记录与审计链

- 新增显式 `migrate_legacy_v0`；只接受不带 `schema_version` 的旧载荷，并复用当前严格契约。
- 请求提交闸门校验 ID，同一 ID 提交后永久保持已消费状态。
- 新增纯契约 `assess_execution_records`，验证执行身份、attempt 绑定、结果绑定、重复完成记录和当前 fencing token。
- 新增确定性 SHA-256 `seal_audit_event` 与 `validate_audit_chain`。
- 事件闸门新增旧 utterance 和终态后迟到诊断；`audio.closed`、`audio.failed`、`asr.rejected` 关闭当前 gate/stream，`asr.final` 不关闭音频流。

### 批次 4：边界补齐与收口

- `COMPLETED` 账本只接受状态为 `completed` 的动作结果；完成记录缺失或含 `not_completed`/`state_uncertain` 时拒绝。
- `RETURNED` attempt 没有结果时分类为 `state_uncertain`，不伪装为一致。
- 授权在 `consumed_at >= expires_at` 时拒绝消费；构造和反序列化路径同样不能绕过，且 `consumed_at` 不得早于创建时间。
- 审计事件被修改 `subject_id` 等哈希内容后，即使保留旧 `event_hash` 也必须判定链断裂。
- 测试精确固定四套状态值集合、19 个核心对象清单、frozen/slots 属性和播放事实可观测状态集合。

## 测试与故障注入

最终使用 Python 标准库 `unittest`，全量结果为 72/72 通过，覆盖：

- 19 个核心对象严格 JSON 往返、schema version、未知/缺失字段和 v0 显式迁移。
- 授权计划/目标/动作变化、到期、重复消费、构造和反序列化绕过。
- 请求 ID 重复提交，证明传输、ASR、TTS 故障不能使同一请求隐式重发。
- 事件重复、同序号不同 event ID、乱序、跨 session、旧 audio session、旧 generation、旧 stream、旧 utterance 和终态后迟到。
- `asr.final` 不关闭流；显式音频关闭/失败/拒绝是 gate/stream 级终态，跨来源迟到事件仍被拒绝驱动处理。
- 四套状态机精确枚举集合、合法路径和非法转换；`UNCERTAIN` 无隐式成功、失败或重试出口。
- attempt 身份冲突、旧 fencing token、重复结果、已开始/中断/返回但无结果、完成账本缺记录和完成状态与结果冲突。
- 审计链连续性、前序哈希断裂和已哈希内容篡改。
- 播放事实只允许 `COMMITTED_NOT_STARTED`、`PLAYBACK_STARTED`、`PARTIALLY_PLAYED`、`PLAYED`、`ABORTED`，拒绝主观“用户一定听见”状态。

验证命令与结果：

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
Ran 72 tests
OK

.\.venv\Scripts\python.exe -m compileall -q app tests
通过
```

附加核验：

- 38 个 `app/`、`tests/` Python 源文件严格 UTF-8 解码通过，无 Unicode 替换字符。
- 实际公开发现的核心契约对象恰为 19 个；全部为 frozen dataclass 且使用 slots。
- 四套枚举值集合与路线图逐项一致。
- P2 公开 API 包含迁移、执行记录评估、审计封装与审计链验证入口。

## 退出条件（可观察事实）

- 19 个核心对象均可严格序列化和反序列化。
- 未知字段、缺失字段、不支持版本和非法枚举被拒绝。
- 四套正交状态机存在，非法状态转换被拒绝。
- 测试证明旧授权、旧请求、旧音频 session/generation/stream/utterance 和迟到终态事件不能跨边界复用。
- 测试证明不确定执行事实不会自动变成成功，完成账本不能接受不确定结果。
- 播放事实只描述可观测边界。
- 全量测试、编译、UTF-8、对象清单、状态集合和公开 API 核验均通过。
- 本 handoff、路线图、任务计划和进度记录已同步。

以上条件全部满足，P2 标记为 complete。

## 已知风险

- `RequestSubmissionGate`、`EventGate`、执行记录评估和审计链校验当前均为进程内纯契约实现；跨进程持久化、真实审计存储和执行恢复属于后续阶段，P2 不实现。
- schema version 当前固定为 1；仅提供明确的 legacy v0 迁移入口，不存在自动猜测或多级迁移框架。
- `assess_execution_records` 只校验和分类已提供记录，不扫描电脑真实状态，也不决定重试、恢复、回滚或补做。
- 项目尚无首次 Git commit，全部项目文件仍显示为 untracked；本阶段未执行 add、commit 或 push。

## 未解决但明确延期的问题

- DeepSeekHarness adapter、文字闲聊、persona、工作/闲聊会话隔离和 Agent 错误映射：延期至 P3，且需用户明确授权。
- ToolRegistry、Schema、PathGuard、PolicyEngine 和受控 ActionPlan：延期至 P4。
- 授权 UI、真实执行器、ResultVerifier、持久审计和崩溃恢复扫描：延期至 P5。
- ASR、TTS、AudioBus、设备、唤醒、VAD、AEC 和长期运行：延期至 P6–P10。
- Skill、隐私清理、手册、打包和发布：延期至 P11。

## 下一阶段入口

当前停在 **P3 授权门前**。只有用户明确授权 P3 后，才允许建立文字聊天与 Harness 会话。

P3 开始时按以下最小顺序读取：

1. `implementation_plan/implementation-roadmap.md` 的 P3 卡片；
2. 本文件 `implementation_plan/checkpoints/P2-handoff.md`；
3. `implementation_plan/checkpoints/P1-handoff.md`；
4. `implementation_plan/05-agent-integration.md`；
5. `implementation_plan/00-project-overview.md`；
6. `implementation_plan/persona.md`；
7. `implementation_plan/shared/error-recovery.md` 中与 Agent 请求层有关的规则；
8. `progress.md` 最后一条 P2 收口记录。

## 下一阶段禁止提前做的事项

- 未授权不得创建或修改 `app/agent/`、`app/conversation/`、`app/persona/` 或 P3 测试。
- P3 即使获授权，也不得暴露电脑工具、生成或执行 ActionPlan、接入语音、自动重试或自动切换 Agent。
- 不得修改 P2 契约来绕过 request ID、授权、事件或不确定状态边界；若 P3 发现契约冲突，先记录阻塞并回到阶段审查。
- 不得顺手进入 P4–P11。
