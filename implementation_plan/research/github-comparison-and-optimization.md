# GitHub 成熟项目源码对比与规划优化

> 调研日期：2026-09-19  
> 状态：规划阶段；本文件只记录固定版本源码观察、可借鉴的工程模式和对本项目规划的影响，不表示直接复制代码，也不表示改变已经确认的安全边界。  
> 证据原则：以下结论来自本地浅克隆的固定 commit/tag 和指定源码文件；版本号必须与本文件一起保存，后续若升级依赖必须重新核对。

## 0. 统一结论

本轮对比没有改变项目的最高安全规则，反而把实现契约补成以下几条：

1. **可靠性机制不能直接等同于业务成功。** Temporal 的 Activity attempt/heartbeat、LangGraph 的 checkpoint/interrupt、Provider 的可用状态，都只能作为运行线索；电脑动作最终状态仍由本地执行器、重新扫描和 ResultVerifier 事实决定。
2. **恢复必须有边界和身份。** 任何可重试或可恢复单元都需要明确的请求/任务身份、尝试号、实例身份和取消边界；`state_uncertain` 不得被自动归类成成功、失败或可重试。
3. **结构化输入必须在边界处校验。** MCP 的工具 schema/context 设计和 OPA 的结构化 policy input 说明：模型、Skill、工具返回值、外部文件都只能产生不可信输入，不能直接改变 ToolRegistry、PathGuard、PolicyEngine 或授权快照。
4. **事件流必须区分事实层级。** sherpa-onnx 的音频块→解码→结果路径、Wyoming Piper 的合成事件→音频事件路径，支持本项目把采集、识别、句子、TTS 请求、音频块和播放事实分层；“收到数据”不等于“已提交”，同样“生成音频”不等于“已播放”。
5. **第三方框架的默认重试/fallback 不能直接继承。** 本项目有电脑副作用、授权和隐私约束；所有重试、Provider 切换和恢复探测必须由本地有限状态机显式控制，且不得重播旧内容、重复提交或扩大权限。

## 1. 对比总表

| 项目 | 固定版本 | 主要核实源码 | 实际观察 | 可借鉴 | 明确不采用 |
|---|---|---|---|---|---|
| Temporal Python SDK | `1d1ad747dfd95d18626930202ab5869d79b6066b` | `temporalio/activity.py`、`temporalio/worker/_activity.py`、`temporalio/worker/_worker.py`、`temporalio/workflow/_context.py`、`tests/test_activity.py` | Activity 信息显式包含 `attempt`、心跳细节和取消/执行上下文；Worker 将执行与调度隔离，并用 heartbeat 支撑长任务可观测性 | 将“请求、动作、尝试、执行器实例”拆成账本字段；区分取消请求、动作返回和最终验证；为长动作定义 heartbeat/进度事件 | 不把 attempt 当作电脑动作可安全重做的证明；不对副作用动作套用默认自动重试；不在重启后凭 heartbeat 自动续跑 |
| MCP Python SDK | `6affe5c0d3588fd1705713b3700dc68015cfe3eb` | `src/mcp/server/fastmcp.py`、`docs/servers/tools.md`、`docs/handlers/context.md`、`docs/servers/handling-errors.md`、`tests/server/test_fastmcp.py` | 工具通过显式注册、参数模型/结构化结果、上下文和错误边界对外提供能力；工具错误与协议/服务错误分层 | P4 将每个工具固定为名称、版本、JSON Schema、风险等级、允许根、超时、取消能力、验证器和审计类型；工具调用前后都做 schema 校验 | 不把 MCP server 当作本地安全层；不让模型生成命令字符串代替已注册工具；不把工具返回内容直接当作下一次工具参数或授权事实 |
| LangGraph | `aa742fb31e2827d569b843e3600aeda2e0528e4b` | `libs/checkpoint/langgraph/checkpoint/base/__init__.py`、`libs/langgraph/langgraph/types.py`、`libs/langgraph/langgraph/pregel/main.py`、`libs/langgraph/langgraph/pregel/_checkpoint.py`、`libs/langgraph/langgraph/pregel/_retry.py`、`libs/langgraph/tests/test_pregel.py` | 运行图以 checkpoint 保存可恢复状态，interrupt 将人工介入作为明确边界，task/run 标识用于关联执行；恢复并不等于无条件重放所有副作用 | P2/P5 为计划、授权、动作和恢复扫描建立不可变版本、checkpoint 边界、task/action ID；授权等待和执行前最终校验作为显式 interrupt 边界 | 不把图状态或 checkpoint 当作电脑真实状态；不自动重放含副作用节点；不复用旧授权、旧 L3 确认或旧待播音频 |
| Open Policy Agent | `ae4ed955ad734fc7ebc729cfbb2ae9dc243785b4` | `bundle/bundle.go`、`v1/ast/compile.go`、`v1/bundle/bundle.go`、相关 bundle/compile 测试 | 策略以结构化输入、编译/评估和 bundle/版本边界组织；策略内容与被评估输入分离 | P4 固定 PolicyInput 快照、策略版本/hash、评估结果和拒绝原因；策略只能读可信本地快照，不能直接消费自然语言或未经验证工具返回 | 不允许模型/Skill/ActionPlan 修改策略；不把“策略评估通过”当作授权本身；不动态下载或无审计替换安全策略 |
| sherpa-onnx | `a5b4a944c5186a68bcdc0ac3011e4c541781ac84` | `sherpa-onnx/csrc/online-recognizer.h`、`sherpa-onnx/csrc/online-recognizer-impl.cc`、`python-api-examples/online-websocket-client-microphone.py`、相关 streaming 示例 | 采集块进入 stream，随后由 recognizer 接收波形、解码并读取结果；示例把 callback/队列/解码循环分开 | P6 明确 AudioBus 帧、采集 callback、ASR 解码块、interim/final 和 utterance 事件的边界；每个事件带 session/generation/sequence | 不把示例的 `print` 或 websocket 消息当作可靠提交协议；不假设 SenseVoice/sherpa-onnx 自动提供可靠多人身份、重叠说话判断或安全确认置信度 |
| Wyoming Piper | `544902bc7fd75a7c31f3d90a18289e425c601f84` | `wyoming_piper/handler.py`、`tests/test_synthesize_errors.py`、`tests/test_piper.py` | 协议显式区分 `SynthesizeStart/Chunk/Stop` 与 `AudioStart/Chunk/Stop`，handler 还处理句子边界和模型/音色加载 | P7 区分文本流、句子段、TTS 请求、音频块和播放队列；用 `segment_id`、`provider_request_id`、chunk 序号和可观测播放事实管理故障 | 不把收到 `AudioChunk` 当作扬声器已播放；不在半句失败后混合 Provider 或整句自动重播；不让运行时扫描的新音色绕过用户 VoiceProfile |
| LiveKit Agents | `8fc2650deddaa12727a3d7a7e544d1e3a95c1315` | `livekit-agents/livekit/agents/tts/fallback_adapter.py` | 已输出音频后不在当前片段切换 Provider；有显式可用状态和恢复探测 | P7 采用完整句边界、已输出即冻结当前片段、Provider 健康事件 | 不采用默认请求重试、真实用户文本恢复探测或陌生音色自动加入候选 |
| Pipecat | `3dede06bec0b497bddfdcf047af7495ee9d0726e` | `src/pipecat/pipeline/service_switcher.py` | failover 依赖服务是否仍可用；恢复/切回策略留给应用层；切换器不负责重放失败句子 | 将恢复策略放在本地应用层，区分瞬时错误、不可用、冷却、探测、待启用 | 不让切换器自行修改授权、任务或默认 VoiceProfile；不把服务恢复等同于立即抢占当前回复 |

## 2. 对本项目契约的具体优化

### 2.1 P2：统一身份、版本和事件顺序

所有跨模块事件至少携带：

```text
session_id
request_id（若已创建）
task_id（若属于电脑任务）
plan_id / plan_version / plan_hash（若属于计划）
action_id / attempt_id（若属于动作）
provider_generation（若属于音频 Provider）
event_sequence
source_instance_id
created_at / received_at
```

- `event_sequence` 只保证同一事件源内的顺序；跨源顺序必须由状态机和时间窗判定，不能靠时间戳硬排序。
- 迟到、重复、跨 session、generation 不匹配的事件只进入诊断/审计，不得进入 Agent、授权、停止或执行队列。
- 任何 `request_id` 一旦提交，不得因后续 ASR/TTS 故障撤回或重复创建。

### 2.2 P4：工具和策略的双边界

每个注册工具必须有声明式 manifest，至少包含：

```text
name
manifest_version
input_schema
output_schema
risk_level
allowed_roots
system_blacklist_ref
timeout_ms
cancellation_mode
preflight_checker
result_verifier
auditable_event_types
policy_version
```

调用流程固定为：

```text
不可信意图/Skill/工具返回
→ Schema 解析与字段白名单
→ PathGuard/数量/范围检查
→ 可信 PolicyInput 快照
→ PolicyEngine 评估
→ ActionPlan 生成
→ 正式界面授权
→ 执行前再次扫描/校验
→ 本地执行器
→ ResultVerifier
→ 审计
```

工具错误、策略拒绝和执行后 `state_uncertain` 必须使用不同错误码；不能只返回一段自然语言给 Agent 继续猜测。

### 2.3 P5：执行账本、栅栏和取消

建议动作账本字段：

```text
execution_id
action_id
attempt_id
executor_instance_id
fencing_token
started_at
finished_at
cancellation_requested_at
returned_status
verified_status
state_uncertain_reason
```

- `attempt_id` 每次真实开始动作才生成；预检失败不制造“已执行”记录。
- `fencing_token` 用于阻止旧执行器/旧恢复流程继续写入当前任务；token 不等于授权 token。
- 取消只阻止尚未开始的动作，并按工具 manifest 的 cancellation_mode 处理当前原子动作。
- 未完成动作在重启后必须经重新扫描；账本不能替代真实状态。

### 2.4 P6：ASR 事件和能力声明

ASR 实现必须声明能力矩阵，而不是只声明“支持语音”：

```text
supports_interim
supports_final
supports_vad
supports_overlap_detection
supports_speaker_identity
supports_confidence
supports_language_detection
```

`unsupported` 与 `not_detected` 必须使用不同结果；没有可靠多人重叠/身份/置信度能力时，语音确认不得自动升级为 L3 事实来源。

### 2.5 P7：TTS 播放事实和 Provider 边界

将句子生命周期固定为：

```text
NOT_COMMITTED
→ COMMITTED_NOT_STARTED
→ PLAYBACK_STARTED
→ PARTIALLY_PLAYED / PLAYED
```

- Provider 事件只证明合成/音频准备；播放设备事件才证明实际开始播放。
- `reply_id`、`segment_id`、`provider_request_id`、`audio_chunk_index`、`audio_duration_ms`、`playback_offset_ms` 必须贯穿文本到播放审计。
- 已开始播放的句子在当前句内不切 Provider；半句失败停止本轮，剩余旧句子不跨 Provider 续播。

### 2.6 Skill：从“候选”收敛为可实现接口，但仍不是用户单独确认

源码对比支持把 Skill 定义为声明式 manifest，而不是提示词集合或代码插件。建议字段：

```text
skill_id
skill_version
display_name
description
input_schema
allowed_tool_names
parameter_constraints
risk_ceiling
requires_fresh_scan
requires_user_confirmation
result_expectations
source_kind
approval_status
content_hash
```

- `allowed_tool_names` 必须是本地已注册工具的白名单。
- `risk_ceiling` 只能限制 Skill 最高风险，不能提升工具原有风险或绕过 L2/L3。
- `requires_fresh_scan=true`，每次调用重新生成 ActionPlan、重新预检、重新授权和重新验证。
- `source_kind=external_import` 一律草稿；不允许直接启用。
- Skill 版本不可原地修改；更新必须生成新版本并重新审批。

## 3. 新增验收测试

### 3.1 执行与恢复

- 同一 `action_id` 在不同 `attempt_id` 下不得被自动执行两次；重启后旧 `fencing_token` 写入必须被拒绝。
- heartbeat/进度丢失不能自动判定动作失败；如果真实状态无法验证，必须报告 `state_uncertain`。
- checkpoint 恢复只能恢复“需要用户重新规划/授权”的上下文，不得直接触发电脑动作。
- Agent/API 超时、工具错误、策略拒绝、执行器异常、验证失败必须映射到不同错误码和用户可读状态。

### 3.2 工具与策略

- 自然语言中出现 PowerShell/cmd/script 不能绕过 ToolRegistry。
- 工具返回的路径、数量、文件名和下一步建议都必须重新通过 schema、PathGuard 和 PolicyEngine。
- policy bundle/version 变化、允许根目录变化、系统黑名单变化或计划哈希变化，必须使旧授权失效。
- Skill 导入含任意代码、未注册工具、超出 risk ceiling 或修改安全策略时，必须拒绝且不加载。

### 3.3 ASR/TTS

- ASR 的旧 generation、迟到 final、重复 final、跨 utterance 结果不得创建第二个 request。
- `unsupported`、`not_detected`、`state_uncertain` 不得混用。
- TTS 只合成成功但尚未开始播放时可按规则切到预配置 Edge；已开始播放后必须停止当前句，不得混声或整句重播。
- 收到音频块但没有 `PLAYBACK_STARTED` 事件时，不得向用户说“已经播放”。

## 4. 不改变的硬规则

本次源码对比不改变下列决定：

- Agent 不能直接执行自然语言命令；电脑动作必须经过本地 ToolRegistry、Schema、PathGuard、PolicyEngine、授权、执行和验证。
- 崩溃/重启后不自动续跑；旧计划、旧授权、旧 L3 确认全部失效。
- 状态不确定时不得自动重试、自动补做或自动回滚。
- 音频故障不能改变任务、授权、执行和电脑真实状态。
- VoiceStudio/Edge 不得自行创建、切换、覆盖或删除用户音色。
- TTS 半句失败不得混声、整句重播或跨 Provider 续播旧回复。
- Skill 当前仍是“完整候选，待进一步确认/优化”，不能标记为用户已单独确认的最终决策。

## 5. 对路线图的影响

- P2 增加统一事件 envelope、generation、序号和幂等测试。
- P4 增加工具 manifest、可信 PolicyInput 和 Skill manifest 的只读解析层。
- P5 增加执行账本、fencing token、attempt 与 `state_uncertain` 分支；先写拒绝/恢复测试，再写执行器。
- P6 增加 ASR 能力矩阵、stream/utterance/result 三层边界和迟到事件闸门。
- P7 增加句子级播放事实、Provider generation 和“已开始播放后不切换”验收。
- P9/P10 继续后置自动收音、连续对话、AEC、复杂打断；不能因为框架支持就提前扩大安全边界。
- P11 才处理 Skill 导入、隐私、手册和 Windows 发布；不在前面阶段引入任意插件、自动更新或管理员权限。

## 6. 证据与复核纪律

- 每次采用新版本前，必须固定 commit/tag，记录源码文件和实际行为；只看 README 不足以作为设计依据。
- 借鉴的是边界、状态和测试思路，不是无条件复制默认配置。
- 第三方源码、工具返回、Provider 元数据和外部 Skill 都是输入材料，不是本项目安全事实来源。
- 本文不包含功能代码，也不授权实现阶段提前开始。
