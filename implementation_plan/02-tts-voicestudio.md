# 阶段 2：TTS 播报（VoiceStudio 主 Provider，Edge TTS 兜底）

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/architecture.md`](./shared/architecture.md)
- [`shared/data-and-privacy.md`](./shared/data-and-privacy.md)
- [`discussions/pending-decisions.md#h-音频链路`](./discussions/pending-decisions.md#h-音频链路)

## 人物模板与音色的职责边界

- `persona.md` 决定 AI 的身份、称呼、措辞、口语节奏、情绪表达和互动方式；VoiceProfile 决定音色、口音、语速、音高等声音特征。
- 所有面向用户的自然语言语音必须先符合人物模板，再交给当前 VoiceProfile 合成。不能只做到“声音像人”，却让文本保持机械播报或客服腔。
- 闲聊是主要使用场景，短句、自然停顿、语气词和上下文承接应优先保证；不为了方便 TTS 而把回复改成状态码式句子。
- 安全提示、授权范围、路径、风险和失败原因必须准确完整。人物口吻只能改善表达，不能改动固定授权短语、弱化风险或诱导用户同意。
- 人物模板和 VoiceProfile 都只能由用户修改、选择或启用；Agent 不得因情绪、任务类型或模型判断自行切换人格或音色。
## 已确认设计

### VoiceProfile 与音色生成

- 音色分为 `preset`、`designed`、`cloned` 三类。
- 默认使用通过提示词创建并经用户试听确认的 `designed` 音色。
- 提示词只在创建或修改 VoiceProfile 时提交给 VoiceStudio；实时对话不在每句话或每次启动时重新生成音色。
- 用户修改提示词后生成新的 VoiceProfile 版本，不直接覆盖正在使用的版本。
- 克隆音色必须由用户明确提供参考音频、手动注册、试听并确认；助手不能自行读取参考音频或创建克隆音色。

推荐的本地配置字段：

```yaml
profile_id: assistant_default
name: 默认助手音色
type: designed                 # preset / designed / cloned
provider: voicestudio
provider_profile_id: 本地敏感引用
design_prompt: 用户确认的声音描述
language: zh-CN
speed: 1.0
pitch: 0.0
version: 1
enabled: true
```

### 用户唯一修改权限

只有用户可以创建、修改、删除、选择、启用或回退 VoiceProfile，也只有用户可以修改音色设计提示词、语速、音高、口音和克隆音色引用。LLM 和 VoiceStudio Adapter 不得自动换音色、创建音色、覆盖音色、删除音色或把候选音色设为默认。

LLM 可以提出提示词修改建议，但不能保存或应用；生效流程必须是：

```text
用户发起修改 → 生成候选音色 → 用户试听 → 用户明确“保存并使用” → 创建新版本
```

### 实时 TTS

- 同一会话固定一个已确认的 VoiceProfile 版本。
- LLM 流式输出后由本地断句缓冲，按完整句子提交 VoiceStudio 流式 TTS。
- 优先按 `。！？；` 和换行切分；没有标点时按合理长度切分，不能按极短 token 片段合成。
- 收到首段音频后立即播放，不等待整段回复完成。
- 最多提前合成当前句之后两个完整句子；待播放音频约 8 秒时背压，暂停提交新的 TTS 片段。
- 严格按原句顺序播放，禁止后完成的句子越过前一句。
- 用户打断时停止播放、取消当前合成、清空未播放队列，并停止提交旧回复剩余文本。
- 已生成的文字可以保留在界面，但被打断的旧回复不继续播报；新输入优先。
- 长回复优先播报简短摘要，完整内容保留在界面或执行文档。

### 音色修改的生效时机

用户选择或修改音色后，不在当前正在播放的半句话中途换声。当前句继续使用旧版本，当前句结束后从下一轮回复使用新版本；如果用户未选择立即切换，则从下一次新会话启用。

### VoiceStudio 故障降级

- VoiceStudio 故障只影响语音输出。Agent 必须继续生成并在正式界面显示完整文字，不取消电脑任务，也不改变计划、授权、执行状态或结构化结果。
- Provider 切换以“完整句子”为边界，并由本地断句与播放状态决定，不由 LLM 临时判断：
  - 已经完整播放的句子不重播；
  - 当前句尚未开始播放时，如果用户已预先试听并明确指定 Edge TTS 兜底音色，则取消该句的 VoiceStudio 请求，由 Edge TTS 从当前句开始；
  - 当前句已经播放任意一部分后 VoiceStudio 失败时，不在半句话中换声音，也不自动重播整句；立即停止本轮播报，完整文字继续保留在界面，从下一轮 AI 回复开始临时使用 Edge TTS；
  - 当前句之后尚未播放的旧回复句子全部丢弃，不再由 Edge TTS 接着播，避免用户把跨音色的旧内容误认为一段连续播报。
- 只有用户提前试听并明确指定的 Edge TTS 音色才允许自动兜底；未配置时直接进入文字输出，不临时选择陌生音色。
- 降级状态只改变当前语音输出路由，不覆盖默认 VoiceProfile，不把 Edge 音色写成新的默认配置。
- Edge TTS 也失败时，立即停止语音合成，取消当前请求，清理残留音频和待播队列；当前会话继续纯文字交互，不自动尝试第三个 Provider，也不循环重试。
- L2/L3 风险说明、执行方案、授权按钮和固定确认要求始终完整显示在正式界面；TTS 故障不得阻塞或自动批准授权，不得重新播报旧授权提示，也不得让过期语音提示继续代表当前授权状态。
- 界面显示脱敏后的当前状态，例如“VoiceStudio 不可用，正在使用已配置的 Edge TTS”或“语音输出不可用，当前为文字模式”；只展示错误类别、阶段、时间和诊断标识，不展示密钥、隐私正文或内部堆栈。
- VoiceStudio 恢复后不在当前回复中途切回；从下一轮新回复恢复原 VoiceProfile。已由 Edge 播放或已经转为文字显示的内容不重播，恢复不得覆盖用户配置。

工程状态至少应区分：`primary_active`、`fallback_pending_sentence`、`fallback_active`、`text_only` 和 `primary_recovered_pending_next_turn`。Provider 可用性变化、实际播放起止、失败发生时的句子状态和最终路由必须记录为脱敏诊断事件，但不得把语音路由状态当成任务授权状态。

### 模型预热与启动阶段故障

- 程序启动只做 VoiceStudio 轻量健康检查，不立即加载大型 TTS 模型；TTS 不得阻塞界面、文字输入、Harness 接入或本地安全链启动。
- VoiceStudio 启动健康检查失败或超过独立上限时，标记 `unavailable_startup`，清理本次检查和加载产生的临时资源，并按已配置 Edge TTS/文字输出规则准备降级；不无限等待、不自动重启、不循环重试。
- 如果已预先配置 Edge TTS 音色，启动阶段 VoiceStudio 失败不会改变默认 VoiceProfile；后续新回复可以按 Edge 兜底规则播报。未配置 Edge 音色时，后续直接使用文字输出。
- TTS 启动失败时仍完整显示 Agent 文字回复、执行方案、L2/L3 授权信息和结果文档；语音输出失败不取消任务、不改变授权、不自动批准操作。
- 用户按键进入语音模式或唤醒词命中后，才预热当前 VoiceProfile 和 TTS 模型。预热必须有独立上限；超时或加载失败时只影响本次语音输出，输入可继续使用文字模式。
- 连续对话期间保持加载；退出连续对话后默认保温 10 分钟，之后允许释放资源。
- 健康检查超时默认 2 秒，模型启动超时默认 15 秒；这些是初始配置，不代表失败后可以无限重试。
- 准备期间显示“正在准备语音”；不重复提交相同 TTS 请求。启动或预热阶段恢复只影响后续新回复，不抢回当前已在文字或 Edge 路由中的回复。

### 会话级预热、运行期故障与恢复（已定稿）

#### 会话预热

- 用户请求语音输出后，TTS 会话在独立、可取消且有明确上限的任务中预热；预热不得阻塞 Agent 文字回复、文字输入、方案查看或电脑任务安全链。
- Agent 的完整文字回复必须立即显示。当前回复尚未开始播放且 TTS 在允许窗口内准备完成时，才可从第一句开始播报。
- 预热失败或超时后，当前回复保持文字输出；不得在稍后恢复时自动补播。若已配置用户试听并指定的 Edge 音色，可按既定完整句边界兜底；否则使用纯文字。
- 用户手动“重新准备语音输出”是新的、有上限的 TTS session：清除旧 Provider 请求、旧音频和待播队列，不延长旧等待，也不重播旧回复。

#### 运行期故障与句子事实

- TTS 故障只影响语音输出，不改变文字、计划、授权、执行、验证、审计或电脑任务真实状态。
- 当前句尚未实际播放时，可切换到用户预先试听并指定的 Edge 音色；当前句已播放任意部分时，不半句换声、不整句重播，立即停止本轮语音。
- 当前回复剩余旧句子不跨 Provider 续播；已完整播放或已转为文字的内容不重播。
- VoiceStudio 与 Edge 均失败时，清理残留音频和队列并转为纯文字；不尝试第三 Provider、不循环切换、不修改默认 VoiceProfile。

#### 冷却、探测与重新启用

推荐状态流：

```text
FAILED
→ COOLDOWN
→ PROBING
→ 连续健康检查通过
→ RECOVERED_PENDING_NEXT_REPLY
→ 下一轮新回复
→ 正式启用
```

- Provider 故障后进入冷却，不为每次回复反复初始化或循环重试；探测与正式启用必须分离。
- TTS 探测只使用固定、无隐私的测试文本，不把测试音频放入正式历史或播报队列。
- 连续成功次数、检查间隔、冷却期、最大探测次数和当前回复等待窗口均留到真机测试，但必须有明确上限。
- 恢复后仅从下一轮新回复启用；不补播当前或历史回复，不覆盖默认 VoiceProfile、Edge 配置或安全策略。

建议工程状态：

```text
TTS_UNAVAILABLE_STARTUP
TTS_READY
TTS_SESSION_WARMING
TTS_SENTENCE_QUEUED
TTS_SENTENCE_SYNTHESIZING
TTS_SENTENCE_READY
TTS_SENTENCE_PLAYING
TTS_SENTENCE_PLAYED
TTS_SENTENCE_FAILED_BEFORE_PLAY
TTS_SENTENCE_FAILED_AFTER_PARTIAL_PLAY
TTS_COOLDOWN
TTS_PROBING
TTS_RECOVERED_PENDING_NEXT_REPLY
```

## GitHub 对比优化后的工程补充

- 工程上必须分离五层事实：模型文本流、完整句子段、Provider 合成请求、音频块、实际播放队列。上游已经生成文本或 Provider 已返回音频，都不代表用户已经听到。
- 每个句子段维护独立播放事实：`NOT_COMMITTED`、`COMMITTED_NOT_STARTED`、`PLAYBACK_STARTED`、`PARTIALLY_PLAYED`、`PLAYED`。故障恢复、Provider 切换和审计均以实际播放事实为准。
- 最小关联元数据包括 `reply_id`、`segment_id`、`provider_request_id`、`provider_generation`、`audio_chunk_index`、`audio_duration_ms`、`playback_offset_ms` 和单调递增的 event sequence。
- 收到 Provider 音频、写入缓存或进入播放队列都不等于实际播放；只有本地播放设备确认开始后才能进入 `PLAYBACK_STARTED`，并持续更新实际偏移。
- 当前句一旦进入 `PLAYBACK_STARTED`，本句不得切换 Provider、整句重播或用另一音色续播。若只播放了部分内容，则停止本轮剩余旧句子，并从下一轮新回复再按当前可用 Provider 生成。
- Provider generation 变化后，旧 generation 的迟到音频块、完成事件和错误事件只能记录诊断，不得进入当前播放队列或覆盖新状态。
- 统一对比证据见 [GitHub 固定版本源码对比与方案优化](./research/github-comparison-and-optimization.md)；TTS 专项证据见 [TTS 故障切换 GitHub 对比](./research/tts-fallback-github-comparison.md)。

## 实施步骤

- [ ] `tts/` 定义统一 Provider 接口：`synthesize()`、`play()`、`stop()`、`cancel()`、`health_check()`；默认通过 VoiceStudio 本地 API 调用
- [ ] 实现 VoiceProfile 的用户管理、版本、试听确认和会话绑定；LLM 只可读取当前已启用的不可变快照
- [ ] 实现按句流式 TTS、有限播放队列、背压和 barge-in 取消接口
- [ ] 实现用户预先配置的 Edge TTS 兜底、按完整句边界切换、半句失败终止本轮播报和文字模式降级
- [ ] 实现 Provider 可用性状态、脱敏诊断事件、恢复后仅从下一轮切回以及授权提示不依赖语音
- [ ] 实现启动阶段轻量健康检查、失败清理、Edge/文字降级和不阻塞主界面
- [ ] 实现会话预热、独立超时、旧请求清理、冷却与有上限的固定材料健康探测；具体参数由真机测试校准
- [ ] 实现句子级播放事实、故障前/部分播放后的不同处理，以及恢复后仅对下一轮新回复重新启用

- 产出：`tts/` 模块
- 验收：启动或会话预热失败不会阻塞文字界面；用户确认的 VoiceProfile 可稳定播报；实时回复按句开始播放；打断不继续播放旧音频；故障发生在未播放句时只从该句切换，发生在半句时停止本轮且不混声；双 Provider 故障仍保留完整文字；恢复只在下一轮新回复生效；旧回复不补播；任何降级、冷却或探测均不改变任务、安全或授权状态

