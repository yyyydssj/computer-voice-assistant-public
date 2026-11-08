# 豆包（Doubao）实时语音做法逆向对照与改造方案

- 日期：2026-10-12（本地）
- 项目：电脑语音助手
- 触发：用户反馈「长回复会中止」；要求用 REA 逆向豆包等实时 AI 对话软件，看人家怎么优化，并结合本项目代码给出改善方案
- 方法：REA 服务侦察（Ghidra 12.1.4 / Windows x64；本会话未打开任何 target）+ 只读真机日志取证 + 公开可复现证据（官方开源 demo / 官方方案说明 / 第三方协议整理）
- 本次纪律：**不改代码、不安装第三方软件、不下载不明来源客户端、不抓取豆包网络流量**；所有结论标注来源等级

---

## 0. 一句话结论

豆包把「长回复」当成**一条流**处理：整条回复文本**一次下发**（`ChatTTSText` 的 `start`/`content…`/`end` 三段式帧），服务端**连续推音频增量**，超时一律按**空闲**判定，并且**只有显式打断才允许丢尾**。

本项目相反：**逐句段请求 + 三处「总时长」上限 + 收尾超时即取消在播音频**——这正是「长回复中止」的结构性来源。

---

## 1. 逆向范围与证据边界

### 1.1 为什么没有直接逆向豆包客户端

| 核查项 | 实测结果 |
|---|---|
| 本机是否安装豆包客户端 | **未安装**（`%LOCALAPPDATA%\Programs`、开始菜单、运行进程均无 Doubao/豆包） |
| 官方下载页形态 | `https://www.doubao.com/download` HTTP 200，实际加载 `lf-flow-web-cdn.doubao.com/obj/flow-doubao/doubao/desktop_online_web/static/js/*.js` ⇒ 「电脑版」是 **Web 客户端**，不存在可直接分析的 Electron `app.asar` |
| 第三方「免安装绿色版」 | **未采用**：来源不可信、无法核对完整性，且不属于用户授权范围 |
| REA 可用性 | 服务已连通；Ghidra 12.1.4 / win32 / x64 检查全通过；本会话**未 open_binary**（没有可信 target 就不打开） |

### 1.2 证据等级表（下文每条结论都按此标注）

| 等级 | 来源 | 说明 |
|---|---|---|
| 官方·源码 | [`volcengine/rtc-aigc-demo`](https://github.com/volcengine/rtc-aigc-demo)（BSD-3-Clause，文件头 SPDX 标注） | 实读到 `web/src/pages/MainPage/MainArea/Room/AudioController.tsx`、`web/src/lib/listenerHooks.ts` 等源码 |
| 官方·方案说明 | [`volcengine/ai-app-lab` `demohouse/rtc_conversational_ai/README.md`](https://github.com/volcengine/ai-app-lab/blob/main/demohouse/rtc_conversational_ai/README.md) | 火山引擎官方仓库；描述智能打断、全双工、帧级 VAD、端上降噪、多端支持 |
| 第三方·协议整理 | [`GizClaw/doubao-speech-go` `docs/realtime_speech.md`](https://github.com/GizClaw/doubao-speech-go/blob/main/docs/realtime_speech.md) | 事件号、字段、错误码；**其自述部分字段不在官方公开文档内**，本文只作旁证 |
| 未取得 | 火山引擎 API 文档正文 | [`端到端实时语音大模型API接入文档`](https://docs.volcengine.com/docs/DoubaoVoice/End-to-endreal-timespeechlargemodelAPIaccessdocument?lang=zh) 与全双工版本页均为 JS 渲染，抓取只得到导航 |
| 本项目 | 实读代码 + 只读真机日志 | 见 §3 的 `文件:行` 引用 |

---

## 2. 豆包的做法（逐条带证据）

### 2.1 一条 WebSocket 跑完整通对话
`StartSession`（事件 `100`）建立会话后，ASR / Chat / TTS / 上下文全在同一会话上以数字事件多路复用：`TaskRequest(200)`、`ChatTTSText(500)`、`ChatTextQuery(501)`、`TTSResponse(352)`、`ChatResponse(550)`、`ChatEnded(559)` 等。
⇒ **不存在「每句一次请求」这种客户端行为**。*（第三方协议整理：事件表）*

### 2.2 TTS：一次请求 + `start`/`content`/`end` 三段式 + 服务端连续推
`ChatTTSText(500)` 的下发形态是三段式帧：

```json
{"start": true,  "content": "",                     "end": false}
{"start": false, "content": "text to synthesize",   "end": false}
{"start": false, "content": "",                     "end": true }
```

服务端随后回 `TTSSentenceStart(350)` → `TTSResponse(352，音频字节)` → `TTSSentenceEnd(351)` → `TTSEnded(359)`。
⇒ **整条回复由服务端切句并连续推音频，客户端只消费一条流**。*（第三方协议整理）*

### 2.3 只有打断才允许丢尾
原文（协议整理）：**「If a new user query interrupts playback before the final end packet is sent, the upstream document says the final packet does not need to be sent.」**
⇒ 收尾包可以缺席的**唯一**合法场景是「用户新的查询打断了播放」。*（第三方协议整理，转述上游文档）*

### 2.4 打断判据来自识别侧，不靠本地自证
- 显式打断事件：`ClientInterrupt(515)` —— *Interrupts server response in push-to-talk mode*。
- 服务端事件 `ASRInfo(450)` 明确写着：**「First ASR signal, useful to interrupt local playback.」**
- 官方 demo 客户端里，打断是一条**控制命令**，不做本地缓冲猜测：

```ts
// web/src/pages/MainPage/MainArea/Room/AudioController.tsx
const handleInterrupt = () => {
  RtcClient.commandAgent({ agentName: botName, command: COMMAND.INTERRUPT });
  dispatch(setInterruptMsg());
};
```

*（第三方协议整理 + 官方 demo 源码）*

### 2.5 超时是「空闲式」，不是「总时长式」
第三方整理的错误码表里，与时长相关的全是**空闲/无数据**判定：

| 码 | 名称 | 含义 |
|---|---|---|
| `52000016` | `AudioTTSIdleTimeoutError` | TTS 合成超时（空闲式） |
| `52000042` | `DialogAudioIdleTimeoutError` | 静音填充失败，上游建议长静音改用 `dialog.extra.input_mod=keep_alive` |
| `52000011` | `AudioChatRecvTimeoutError` | 模型文本响应超时（空闲式） |
| `50700000` | `CallWithTimeout: stream recv timeout` | 模型侧超时 |
| `45000003` | `Abnormal silence audio` | 超过 10 分钟无对话交互才释放连接 |

⇒ **没有「一条回复最多播 N 秒」这类上限**；只有「多久没有新数据」才判失败。*（第三方协议整理）*

### 2.6 音频管线外包给 RTC SDK
官方方案说明：**「支持全双工通信及音频帧级别的人声检测（VAD），随时插话打断」**；**「通过 RTC SDK 实现对复杂环境的音频降噪能力……提高用户语音打断的准确性」**；全链路 RTC+ASR+LLM+TTS 延时 <1 s；支持 Web/Electron/iOS/Android/Windows 等多端。
demo 客户端只做 UI 与房间/流订阅（`listenerHooks.ts` 里是 `onUserJoined/onUserPublishStream/RemoteAudioPropertiesInfo/AutoPlayFailedEvent` 这类 RTC 事件），**PCM 队列、抖动缓冲、AEC 都不在应用层**。*（官方方案说明 + 官方 demo 源码）*

---

## 3. 与本项目代码的差距（「长回复会中止」的根）

| 维度 | 豆包 | 本项目（实读） | 后果 |
|---|---|---|---|
| TTS 请求粒度 | **1 次/整条回复**（start/content/end） | **N 次/句段**：`app/tts/providers/voicestudio_ws.py:216` `stream_sentence()` 一次只发一句 | 请求数、失败面、句间重连被放大 |
| 超时语义 | **空闲式**（无数据才超时） | **总时长式**三处并存：`finish()` join `segment_timeout_s=30 s`（`app/tts/streaming_session.py:924`）、`_enqueue` 队列等待（默认同上 30 s）、`_await_room` 背压等待 15 s（`app/runtime/voice_entry.py:624`、常量 :515/:518） | 长回复被时间上限砍 |
| 收尾 | 服务端 `end` 标记 → 播完 | `SessionStream.finish()` 的 `finally: close()` → `cancel()` **取消仍在播放的音频**（`app/runtime/voice_entry.py:765-777`） | 半静默截断（只报 `truncation_reason`） |
| 打断判据 | 识别侧事件（`ASRInfo 450`）+ RTC AEC/端上降噪 | 本地「回声相关性证明」（`barge_in_channel` 的 `echo_proven`），本机证不出来 ⇒ fail-closed | 打断实际不可用（有意的安全取舍） |
| 服务端能力利用 | —— | VoiceStudio `/ws/tts` **本来就支持整段文本**：服务端 `split_stream_sentences()` 自己切句、连续推 PCM、最后一个 `done`（`VoiceStudio-App/.../backend/api/routers/tts_stream.py:231-477`），客户端却按句请求 | 已具备的能力没被使用 |
| 不变量 | **只有打断才丢尾** | 只保证「少播必定有 `truncation_reason`」 | 长回复仍会丢尾 |

> 行业旁证（本项目既有调研，2026-10-03）：Pipecat 把「结束帧」串到音频排空之后（[PR #4127](https://github.com/pipecat-ai/pipecat/pull/4127)，已合并）；LiveKit 用 `wait_for_playout()` 的播出事实；HA 的「只播第一句」根因是逐句请求被上游拒（[#182944](https://github.com/home-assistant/core/issues/182944)）。见 `2026-10-03-P10-R4B-streaming-tts-tail-loss-github-research.md`。

---

## 4. 改造方案（对齐豆包，落到具体文件）

### P1（先做）把「总时长上限」换成「无进展看门狗」
- **改什么**：`app/tts/streaming_session.py`、`app/tts/sequential_session.py`、`app/runtime/voice_entry.py`、`app/main.py`。
- **怎么改**：收尾等待期间，只要观察到**任一进展**就重置计时——`pending_sentences` 下降 / `synthesized_segments` 增加 / 设备已提交音频增加 / 已播段数增加；**连续 `stall_timeout_s`（建议默认 15 s）无进展**才判 `finish_timed_out`。`_enqueue` 的「等待超时即整批拒绝」同样改为「只在无进展时拒绝」。新增 CLI `--speech-stall-timeout`。
- **为什么**：对齐豆包第 2.5 条（空闲式）。
- **验收**：`played == accepted == pushed`、`finish_timed_out == false`；真机 ≥800 字长回复不丢尾；卡死时仍在 `stall_timeout_s` 内失败。
- **风险**：真卡死时轮次最多多等一个 stall 窗口；回滚＝把该值调回旧常量或 revert 该批。

### P2（再改粒度）整段一次请求，客户端只做有界缓冲
- **改什么**：新增/改造 provider 调用路径——把整条回复文本**一次**发给 VoiceStudio `/ws/tts`（对应豆包 `ChatTTSText` start/content/end），客户端只保留「接收 → 有界缓冲 → 串行播放」，段级账本移到客户端。
- **为什么**：请求数 N→1，消除逐句 18 s 超时、逐句序号账本、句间重连与 `session_idle_seconds` 掉线；首音更早。
- **验收**：长回复全播；打断仍能立即停；`provider_failures` 为空。
- **风险**：服务端不背压（每块 `await asyncio.sleep(0)`），慢客户端会使内存增长——按 400 字 ≈ 83 s 音频 ≈ 4 MB 估算可接受，但需实测并设上限。

### P3（可选）打断判据增加「识别侧」证据
- 借豆包第 2.4 条：我们已有 Silero 段首、KWS、声纹、能量门，可组合成**一条独立于回声相关性的证据**。
- **不放松**现有 fail-closed：`--barge-in-mode confirm` 仍要求证据成立才停；只是判据来源从「本地自证回声」扩展为「识别侧确认有人说话」。

### P4（验收不变量，直接抄豆包）
1. `played == accepted == pushed`；
2. **只有打断才允许丢尾**——非打断情形下若丢尾，`truncation_reason` 必须非空且进 `voice_turn`。

---

## 5. 真机事实更正（2026-10-12 晚）

只读 `%LOCALAPPDATA%\ComputerVoiceAssistant\logs\runtime.jsonl`：

| 时间（UTC） | 模式 | 字数 | 推送/接受/已播 | 结果 |
|---|---|---:|---|---|
| 2026-10-12 14:58:29 | ws_pcm | 17 | 17/17/17 | `speech_finished` |
| 2026-10-12 14:59:14 | ws_pcm | **173** | 169/169/169 | `speech_finished` |
| 2026-10-12 14:59:46 | ws_pcm | 92 | 92/92/92 | `speech_finished` |

- 用户反馈「这次全念出来了」**与日志一致**；但**最长仅 173 字**，未进入长回复区间；
- ⇒ 2026-10-09 的修复（空白碎片 / 序号错位）在短中回复上**确证有效**；
- ⇒ **§3 的收尾上限风险尚未被真机触发**，属结构性推导，需一轮 ≥800 字或尾部≈30 s 的真机轮次才能证伪/证实。

---

## 6. 未做 / 不得伪造

- **未**安装、未运行、未抓取豆包任何客户端或其网络流量；**未**分析其 minified 前端 bundle（`desktop_online_web/static/js/*.js` 仅发现，未下载）。
- **未**读到火山引擎官方 API 文档正文；事件号 / 字段 / 错误码均来自**第三方**协议整理，已在文中逐条标注。
- 第三方明确标注「不在官方公开文档内」的字段（如 `output_modalities`、`dialog.extra.scene_id`）本文只作旁证，**不作为改造依据**。
- **未**实测本项目长尾截断（需要 ≥800 字真机轮次）；**未**改任何代码或测试。
- REA 未打开任何 target：没有可信本地 artifact 时不开会话，避免把示例目标当作用户目标。

---

## 7. 参考链接

- 官方开源 demo（BSD-3-Clause）：<https://github.com/volcengine/rtc-aigc-demo>
- 官方方案说明：<https://github.com/volcengine/ai-app-lab/blob/main/demohouse/rtc_conversational_ai/README.md>
- 第三方协议整理（豆包实时语音）：<https://github.com/GizClaw/doubao-speech-go/blob/main/docs/realtime_speech.md>
- 火山引擎 API 文档（JS 渲染，正文未取得）：<https://docs.volcengine.com/docs/DoubaoVoice/End-to-endreal-timespeechlargemodelAPIaccessdocument?lang=zh>
- 本项目既有调研：`implementation_plan/research/2026-10-03-P10-R4B-streaming-tts-tail-loss-github-research.md`、`2026-10-03-P10-R4B-tts-truncation-github-research.md`、`2026-10-02-realtime-voice-barge-in-open-source-research.md`
- 本项目结案记录：`implementation_plan/checkpoints/2026-10-07-voice-playback-truncation-investigation.md`
