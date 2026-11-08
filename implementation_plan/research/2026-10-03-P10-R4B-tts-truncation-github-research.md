# P10 R4-B 调研：流式 TTS「只播第一段/中途截断」——GitHub 同类问题与处理方式

- 建立时间：2026-10-03
- 触发：本机真机现象——**349 字长回复只播 1 段**（`speech_played_segments=1`、无 error、无丢推、无超时），
  用户实听"才开始就中止"；此前两批修复（按句推送 + 排空等待）解决了各自现象但**没解决这一条**。
- 方法：`web_search` 找到候选后，**逐个用 `api.github.com` 核对仓库/PR/issue 真实存在**并读正文
  （`web_fetch` 无法访问 github.com：网络策略判定为非公网 IP）。**下列结论均来自实际取回的 API 正文。**

---

## 1. 最贴近：Home Assistant「只播第一句」

- 来源：[home-assistant/core #182944](https://github.com/home-assistant/core/issues/182944)（**已关闭**）
  标题：*ElevenLabs: only first sentence spoken with eleven_v3_conversational (previous_request_ids not supported)*
- 事实（正文原文要点）：流式 TTS **只播第一句，之后每句都失败**；`_process_tts_stream` 从第二句起带
  `previous_request_ids`，而该模型**不支持**该参数 → 服务端逐句返回
  `unsupported_model` 校验错误。
- 处理方式：维护"不支持该参数的模型"能力表（`MODELS_PREVIOUS_INFO_NOT_SUPPORTED`），按模型能力决定是否带该参数。
- **对我们的意义（高度吻合）**：我们的现象是"首段成功、后续段消失"，而**本地完全没有段级失败事实**
  （`streaming_session.failure_states` 从未进过 `voice_turn`）⇒ 必须先把"每段的 provider 播放状态"带出来，
  才能判定是"后续段被本地 TTS 服务拒绝"还是"根本没创建那么多段"。

## 2. 收尾/关闭顺序：Pipecat 把"结束帧"串到 TTS 排空之后

- 来源：[pipecat-ai/pipecat PR #4127](https://github.com/pipecat-ai/pipecat/pull/4127)（**已合并**）
  标题：*Fix LLMFullResponseEndFrame racing ahead of final TTSTextFrame*
- 事实（正文原文要点）：`LLMFullResponseEndFrame` 会**抢在最后一个 `TTSTextFrame` 之前**发出
  （顺序变成 …TTSTextFrame#n-1 → EndFrame → TTSTextFrame#n），导致末尾内容被丢。
- 处理方式：**把结束帧改走 TTS 的 `serialization_queue`**，保证"音频上下文完全排空之后"才发出结束帧。
- **对我们的意义**：印证"结束/关闭必须串到会话自己的排空之后"。我们的 `_await_drain()`
  （`pending_sentences` 归零再 `close()`）属于同一类；但若 failure_states 显示段级失败，则光靠排空不够。

## 3. 中文分块/最小长度：Hermes Agent 的硬编码 min_len

- 来源：[NousResearch/hermes-agent #96927](https://github.com/NousResearch/hermes-agent/issues/96927)（**已关闭**，
  仓库与 issue 均核实存在）
  标题：*Streaming TTS: SentenceChunker min_len=20 is hard-coded — CJK first sentences are always buffered, delaying first audio*
- 事实：`SentenceChunker` 的 `min_len=20` 写死，**中文短句总是被缓冲**、首音被推迟。
- 处理方式：把最小长度做成可配置（`tts.streaming.min_len`）。
- **对我们的意义**：中文的"分块策略（最小长度/上限）"必须显式可控。我们新增的 `split_for_push`
  有 80 字上限但**没有 min_len 语义**；若真机显示"段数远少于预期"，就要从这里查。

## 4. 相关但方向不同：LiveKit「强制合成部分文本」

- 来源：[livekit/agents #2417](https://github.com/livekit/agents/issues/2417)（**已关闭**）
  问题：自定义 `llm_node` 想把"先产生的一段文本立刻说出来"，但 TTS 在**等文本达到阈值**才开口。
- **对我们的意义**：与"首音延迟"有关，不是截断；说明各框架都有"文本聚合阈值"这个可调点。

---

## 5. 采纳结论（下一批做什么）

1. **先补段级事实**（对应 §1 的根因类别）：把 `streaming_session.failure_states`（逐 provider 请求的
   播放状态，固定短码）、`provider_failure_count`、`edge_attempts`/`edge_failures` 带进 `voice_turn`
   （**已完成**：`speech_provider_failures` / `speech_provider_failure_count` / `speech_edge_failures`）。
2. **再跑一轮真机长回复**：
   - 若 `speech_provider_failures` 非空（尤其"后续段失败"）⇒ 按 §1 的 HA 思路处理：修本地 TTS 服务
     的逐句请求参数/能力判断（或在会话侧对失败段做有界重试）；
   - 若失败为空且"段数"远少于预期 ⇒ 按 §3 的思路查**中文分块策略**（min_len/上限）；
   - 若都正常但用户仍听到截断 ⇒ 按 §2 的思路继续收紧"结束/关闭与排空的串行化"。
3. 记录口径：本机 TTS 是**本机 VoiceStudio ws_pcm**（非云端），因此 §1 的"服务端拒绝"在我们的情形里
   等价于**本地服务按句拒绝/断连**，需要用 provider 请求状态（同一族事实）来判定。

## 6. 未做（不得伪造）

- 只读了上述 4 个来源的 API 正文（标题/状态/正文摘要），**没有**通读它们的修复 diff 全文；
- 未检索/未核对其他可能更贴近的项目（例如各家 TTS 的 WebSocket 客户端实现）；
- 本机 TTS 服务（VoiceStudio）不是开源仓库，无法查其同类 issue。
