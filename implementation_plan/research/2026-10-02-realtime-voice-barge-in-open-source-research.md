# 调研：开源实时对话 AI 的「打断 / 全双工 / 回声」做法（2026-10-02）

> 触发原因：P10 R4 真机三轮暴露的问题不是我们独有的——**没有可用回声参考时，任何基于
> 音量+时长阈值的打断都会把"自己的声音"判成用户插话**（本机实测 6 个回合全部误停播）。
> 本文件记录真实可查的开源参照与官方文档，并给出对我们项目的具体建议。
>
> 口径：只记录**实抓到**的仓库/文档/许可证；抓取受限未能核实的一律标注"未核实"。
> 检索时间：2026-10-02；抓取方式：web_search + web_fetch（github.com 直连被本机网络策略
> 拦截，改用 `ghfast.top` 镜像读 raw README）。

## 1. 先说结论（4 条共性）

1. **级联方案（STT→LLM→TTS）一律把"回声消除（AEC）或耳机"当作硬前提**；差异只是 AEC 由谁做：
   浏览器 WebRTC / Agora RTC 传输层 / 云侧推理服务。
2. **拿不到 AEC 时，工程界不用"音量+时长阈值"硬顶**，而是用**训练出来的"打断判别器"**：
   LiveKit 的 adaptive interruption model、Pipecat 里的 Krisp VIVA 打断预测。
3. **即便有判别器，也必须有回合边界冷却与最短语音门槛**：LiveKit 的
   `backchannel_boundary` 默认「回合开始 1.0 s 内退化为 VAD-only、回合结束 1.0 s 内把迟到转写算作真轮次」；
   Pipecat 有 `MinWordsUserTurnStartStrategy(min_words=3)` 与 mute 策略。
4. **端到端全双工模型**（如 Moshi）把"抢话"内建进模型，但**依然推荐用 Web UI 的浏览器 AEC**，
   且硬件门槛高（README 明确 24 GB 显存级）。豆包这类是**闭源云服务**，只能参考交互语义。

## 2. 豆包（Doubao）能不能参考？

- 豆包官方有一条 **「端到端实时语音-全双工版本」** 文档（火山引擎 DoubaoVoice 文档站，
  标题即"端到端实时语音-全双工版本"）：<https://docs.volcengine.com/docs/DoubaoVoice/endtoend-realtime-voice-full-duplex-version?lang=zh>
  （页面为 JS 渲染，抓取只拿到空正文，**内容细节未核实**）。
- **它是闭源云服务**：没有可复用的开源实现、没有可读的算法源码。可借鉴的只有产品语义
  （全双工、可随时插话、服务端做 VAD/打断判定）——算法与实现无从参考。
- 因此下面列的开源项目才是真正能"读源码/照抄做法"的对象。

## 3. 值得读的开源项目（真实仓库 + 许可证 + 关键做法）

### 3.1 Moshi（Kyutai）——全双工语音-文本基础模型
- 仓库：<https://github.com/kyutai-labs/moshi>；论文 arXiv:2410.00037；权重见 HuggingFace `kyutai/moshika-*`、`kyutai/moshiko-*`。
- 许可证（README 原文）：**Python 部分 MIT、Rust 后端 Apache、web client MIT；模型权重 CC-BY 4.0**。
- 架构：两个音频流（"模型说话"与"用户说话"分开建模）+ 模型自身语音的文本 token（inner monologue）；
  Mimi 神经编解码 24 kHz→12.5 Hz；理论延迟 160 ms。
- **关键事实（对我们要紧）**：README 明说命令行客户端 **"does not perform any echo cancellation"**，
  并**推荐使用 Web UI，因为浏览器侧提供了额外的回声消除**。
- 硬件：PyTorch 版需 ≥24 GB 显存；官方不支持 Windows（"we hope it will work… we do not provide official support"）。
- 对我们的意义：**"上全双工模型"并不能免除回声问题**；而且本机（RTX 3050 级）跑不动。

### 3.2 Unmute（Kyutai）——级联式开源语音对话系统
- 仓库：<https://github.com/kyutai-labs/unmute>（README 经镜像实抓）。
- 结构：浏览器前端 + WebSocket 后端 → STT 服务 + LLM + TTS 服务；后端↔前端协议**仿 OpenAI Realtime API**。
- 部署：Docker(Compose)/Dockerless；**仅 Linux 或 Windows+WSL**（原生 Windows 不支持，见其 issue #84）。
- 对我们的意义：它把麦克风/AEC 交给了**浏览器**（getUserMedia/WebRTC），这正是我们原生桌面端缺的那一层。

### 3.3 Pipecat（Daily）——语音 Agent 流水线框架
- 文档：<https://docs.pipecat.ai/pipecat/fundamentals/interruptions>（本轮实抓）。
- 打断语义（可照抄的设计）：
  1. 用户回合启动策略触发且 `enable_interruptions=True`（默认）→ 广播 `InterruptionFrame`（SystemFrame）；
  2. 每个处理器**立即**取消当前任务并丢弃排队帧（`interruptible=False` 的帧如 `FunctionCallResultFrame` 保留）；
  3. LLM 中途取消、TTS 清缓冲、**输出传输清空未播音频**；
  4. **只把"真正播出去的文本"写入上下文**（按播放进度推 `TTSTextFrame`），被打断时提交"已说出口的部分"，
     这样下一轮 LLM 看到的历史是"说到哪儿被切断"，而不是"本来生成了多少"。
- 防误打断手段：`MinWordsUserTurnStartStrategy(min_words=3)`、`AlwaysUserMuteStrategy`（播放期丢弃用户音频）、
  **Krisp VIVA 打断预测策略**（区分真打断与"嗯/好"这类 backchannel）。
- 一条容易踩的产品语义：**"禁用打断"≠忽略用户**——用户压着你说的话仍会被转写并排队回复；
  要真正丢弃，得用 mute 策略。

### 3.4 LiveKit Agents——自适应打断处理（最贴近我们问题的做法）
- 文档：<https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling.md>（本轮实抓）。
- 做法：VAD 之后**再过一层"打断判别模型"**，用声学信号判断"用户是否真的要让出轮次"，
  以过滤 backchannel（"uh-huh/okay/right"）与噪声；不需要等转写 → 延迟更低。
  · 该模型托管在 LiveKit Cloud 推理（本地开发有 40,000 次/月免费额度）→ **模型本身不是开源的**，但流程是公开的。
- **回合边界冷却（`backchannel_boundary`，默认 `(1.0, 1.0)` 秒）**：
  · 回合开始后 1.0 s 内**抑制自适应判定，退回 VAD-only**（因为刚开口时真打断容易被误判成 backchannel）；
  · 回合结束前 1.0 s 内的迟到转写要算作真实用户轮次（避免把回答吞掉）。
- 开关：`mode: "adaptive" | "vad"`；整个打断可 `enabled: false` 关闭。
- 对我们的意义：① 我们的假打断发生在播报开始后 1.6~2.6 s，**单靠冷却窗口挡不住**，但它挡得住
  "刚开口就被自己的声音触发"这一类；② 真正管用的是"有判别依据"（他们的模型，我们的 `echo_correlation`）。

### 3.5 TEN Framework / TEN-Agent（声网生态，中文社区常用）
- 仓库：<https://github.com/TEN-framework/TEN-Agent>、框架 <https://github.com/TEN-framework/ten-framework>（README 经镜像实抓）。
- 许可证：**Apache License 2.0（含附加限制条款）**；`packages` 目录内组件为 Apache 2.0。
- 生态里有两个直接相关的项目：**TEN VAD**（流式 VAD）、**TEN Turn Detection**（自称"enables full-duplex dialogue communication"）。
- 传输：示例走 **Agora RTC**（WebRTC 系）→ **AEC 由 RTC SDK 负责**；另有 websocket 与 speech-to-speech realtime 示例。
- 对我们的意义：架构分层的参照（VAD / Turn Detection / 传输各一层），以及"桌面端要自己补 AEC"的反证。

### 3.6 中文生态与其它
- **Fay**（`xszyou/Fay`，数字人/Agent 框架）：社区文章讨论其"多通道打断逻辑"
  （<https://jishuzhan.net/article/2043176453374803969>，**二手来源，未读源码核实**）。
- **端到端全双工/omni 开源模型**（备选路线，本轮**未核实**，抓取被网络策略拦截或仅有二手描述）：
  Qwen3-Omni（HF 标注 `license: other`）、MiniCPM-o 2.6、GLM-4-Voice、Step-Audio 2 等。
  采用前必须逐一核实：许可证、显存/延迟、是否真支持打断、中文效果。

## 4. 对我们的具体建议（按性价比排序）

**A.（最高性价比，最小改动）让 AEC 参考"同钟"，把判别依据找回来。**
现在 24 kHz 播放参考 vs 16 kHz 采集被判定为 `reference_matches_output=False`，于是
`echo_correlation` 恒为 `None` → R1a 没有任何回声依据 → 只能靠音量阈值 → 假打断。
改法：在 `app/audio/aec/reference_source.py` 的提交旁路里把播放 PCM **重采样到采集时钟**
（numpy 线性/多相，10 ms 帧对齐），`reference_matches_output` 才能为真，R2a 才会给
`aec_ns`/`aec_only`，R1a 才能用相关性折减。
**验收**：离线合成回声（16000 Hz 参考 + 合成延迟/增益）先跑到 ERLE>15 dB 且相关性可用于判定；
再看真机 `echo_correlation` 是否有效（若无效，说明本机确实不存在可测线性回声路径，转 C）。

**B.（根治）引入真正的 AEC/NS 前端。**
- `pywebrtc-audio`：WebRTC 音频处理管线（NS/AEC/AGC/VAD）的 Python 绑定，pybind11
  （<https://pypi.org/project/pywebrtc-audio/>、<https://github.com/strands-labs/pywebrtc-audio>）；
- `webrtc_audio_processing`（xiongyihui 绑定，piwheels 有 wheel）：<https://www.piwheels.org/project/webrtc-audio-processing/>；
- 这两者都需要**参考与采集同采样率、10 ms 帧**，所以仍要先做 A 的重采样与分帧。
- 社区实践佐证：有仓库在提交里写"AEC3 上线麦克风路径，**收敛期抑制打断**"
  （<https://github.com/kouhxp/cheap-im/commit/8bdcbb44a9723a7e28119e41e5b5f02fc4c7f285>，第三方仓库，仅作实践示例）。

**C.（兜底，立即能用）冷却窗口 + 更严门槛 + 半双工提示。**
- 播放开始后 `cooldown_ms`（参考 LiveKit 的 1.0 s）内不进入疑似/确认；
- 疑似要求"连续命中 ≥ N 帧（我们已有 `min_suspect_frames`）+ 能量高于回声基线";
- 拿不到参考时**保持现在的 fail-closed（半双工）**，并在启动时如实打印原因（已实现）；
- 界面上明确提示"当前机器不具备打断条件（建议戴耳机或外接麦克风）"。

**D.（长期）打断判别器。**
拿到 A/B 之后，再考虑用"人声概率 + 回声相关性 + 能量相对基线 + 最短时长"组合规则，
或接入/自训一个小的打断判别模型（LiveKit 的做法证明这条路有效，但其模型不可自托管复用）。

## 5. 不采纳的路线（及原因）

- **直接搬 Moshi / Unmute**：硬件（24 GB 显存 / Linux-WSL）与平台（原生 Windows）不符，
  且它们同样依赖浏览器 AEC；对本机没有解决问题。
- **豆包全双工**：闭源云服务，无法本地化、无法读实现、也无法满足"电脑语音助手本地控制"的隐私边界。

## 6. 参考链接（全部本轮实抓或实检到）

- Moshi 仓库与 README：<https://github.com/kyutai-labs/moshi>（镜像实抓 raw README；许可/MIT+Apache+CC-BY 4.0 见 README「License」节）
- Unmute：<https://github.com/kyutai-labs/unmute>（镜像实抓 raw README）
- Pipecat 打断文档：<https://docs.pipecat.ai/pipecat/fundamentals/interruptions>（实抓）
- LiveKit 自适应打断：<https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling.md>（实抓；含 `backchannel_boundary` 默认 (1.0, 1.0)）
- TEN-Agent：<https://github.com/TEN-framework/TEN-Agent>、TEN Framework：<https://github.com/TEN-framework/ten-framework>（镜像实抓 raw README；Apache 2.0 with certain conditions）
- 豆包全双工实时语音（闭源云服务文档）：<https://docs.volcengine.com/docs/DoubaoVoice/endtoend-realtime-voice-full-duplex-version?lang=zh>
- `pywebrtc-audio`：<https://pypi.org/project/pywebrtc-audio/>、<https://github.com/strands-labs/pywebrtc-audio>
- `webrtc_audio_processing`：<https://www.piwheels.org/project/webrtc-audio-processing/>
- 社区实践（AEC 收敛期抑制打断）：<https://github.com/kouhxp/cheap-im/commit/8bdcbb44a9723a7e28119e41e5b5f02fc4c7f285>
- Fay 多通道打断逻辑（二手）：<https://jishuzhan.net/article/2043176453374803969>
