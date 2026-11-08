# P10 R4-B 调研：流式 TTS「长回复只播开头/丢尾」——GitHub 主流框架怎么解决

- 建立时间：2026-10-03
- 触发：真机长回复（249 字）**只播出 74 字**，而会话报"播完"（`played_segments == completed_sentences`、
  `error=None`、队列残留 0）。用户要求"去 GitHub 上找找怎么解决"。
- 方法：`web_fetch` 访问 github.com 被网络策略拒绝（非公网 IP），改用 **`api.github.com`** 逐条拉取
  标题/状态/正文核实；**未采信任何搜索摘要**。

---

## 1. 已核实条目（8 条）

| 来源 | 状态 | 要点 |
|---|---|---|
| [pipecat #4111](https://github.com/pipecat-ai/pipecat/issues/4111) | closed | 多句 TTS 后"结束"提交过早：**能完整说出，但下一轮 LLM 收到不完整的 assistant 消息** ⇒ 帧序问题 |
| [pipecat PR #4127](https://github.com/pipecat-ai/pipecat/pull/4127) | **merged** | 修法：让结束帧走 **serialization queue**，排在最后一段音频**排空之后** |
| [pipecat #2789](https://github.com/pipecat-ai/pipecat/issues/2789) | closed | 用户实测多句送 ElevenLabs **只出前几句/首末句**（与本机症状同族） |
| [pipecat #5305](https://github.com/pipecat-ai/pipecat/issues/5305) / [PR #5323](https://github.com/pipecat-ai/pipecat/pull/5323) | open / 修 | "已接受但零播出"的文本必须标 `append_to_context=False` + **warning**；不许算成功 |
| [pipecat #4647](https://github.com/pipecat-ai/pipecat/issues/4647) | closed | 官方表述：**"生成 TTS（文本已提交）≠ 音频已播出"**，优雅收尾**不等待**在途音频（15 s 音频只听到 2 s） |
| [livekit/agents #2417](https://github.com/livekit/agents/issues/2417) | closed | 结束必须由**显式 flush 标记**驱动，不靠"没数据了"隐式判断 |
| [livekit/agents #5742](https://github.com/livekit/agents/issues/5742) | open | 收尾**同步关掉在途音频通道** ⇒ 中途被砍；`wait_for_playout()` 变 no-op |
| [home-assistant/core #182944](https://github.com/home-assistant/core/issues/182944) / [PR #183055](https://github.com/home-assistant/core/pull/183055) | **merged** | 逐句请求**第 2..n 句被上游 API 拒** ⇒ 只播第一句；修法=能力表一行 + 补测试（原先被 monkeypatch 掩盖） |

## 2. 主流三种做法

- **A 串行化 + 显式排空**（Pipecat）：单 FIFO 串行队列，**无界**、不丢帧；收尾帧在同一队列尾部。
  代价：内存无界；用 `stop_frame_timeout_s≈3 s` watchdog 判"音频流死了"，**超时即静默不播**（其残余丢尾面）。
- **B 全链路有界"等待式"背压 + 播出事实**（LiveKit）：`AudioSource.capture_frame` **等**到有空间、
  `Chan.send()` 阻塞；并维护 `captured_playout_segments`（已接受）/`_pending_playback_count`（在途）/
  `PlaybackFinishedEvent.playback_position`（实际播出）+ `wait_for_playout()`。代价：慢 TTS 会反压到 LLM 文本流。
- **C 无背压、逐句请求 + 失败上报**（Home Assistant / Dify）：实现简单，但**丢尾是常态**。

## 3. 结论：我们该走 B（为主）+ A（收尾串行化）

**最关键的行业共识**：主流框架里**没有**"有界 TTS 队列溢出 → 静默丢弃已接受句段"这种官方修法
（Pipecat 的 `FrameQueue` 经核实**无界**；LiveKit 是 `await`；HA 是逐句请求 + 失败上报）。

## 4. 据此落地到本仓（本批已实施，见 `checkpoints/2026-10-03-P10-R4B-speech-truncation-fix-handoff.md` §9）

1. `_enqueue` 越界改为**等待式背压**（`Condition.wait`，超时**整批拒绝 + 明确错误**，绝不部分入队后静默丢）；
2. `_overloaded` **从 `_run` 退出条件删除**（退出只剩"显式结束且队列排空"或"致命错误必须上报"）；
3. **三计数事实**：`accepted` / `synthesized` / `played`（+`pending`），`played < accepted` 一律 `speech_truncated`；
4. **预算自洽**：最长句段（48 字 ≈10 s）× 2 倍余量 ⇒ PCM 队列预算 8000 → **20000 ms**；
5. **确定性慢 TTS 回归**：长回复断言 `played_chars == 总字数`，或失败且原因非空。

## 5. 未核实 / 不得伪造

- 未找到主流框架里"越界即丢"的对应物（即与本仓旧写法等价的官方做法不存在）；
- Pipecat 无"TTS 队列背压/上限"的公开文档或 issue（`backpressure in:title` 0 命中）；LiveKit 无"只出第一句"类 issue
  （`"first sentence" tts` 命中的是分词/标点切分问题，与播放丢失无关）；
- HA #182944 的根因是**模型能力表**（`previous_request_ids` 被拒），**不是**队列溢出；
- 本调研只读 GitHub API，未运行这些框架的代码；本仓实现由本地离线复现与测试自证（见 §9）。
