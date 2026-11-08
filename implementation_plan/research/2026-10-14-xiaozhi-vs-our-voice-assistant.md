# 小智 AI（Xiaozhi）vs 本项目「电脑语音助手」对比调研（2026-10-14）

> 目的：给"下一步要借鉴什么"提供依据，不是评优劣排行榜。
>
> **核实边界（重要）**
> - 本会话网络策略**拦截 GitHub 域名**（`web_fetch` 对 `github.com`/`raw.githubusercontent.com`/`huggingface.co`
>   一律返回"解析到非公网 IP"）⇒ **star/fork 数、源码、协议细节、提交活跃度我都没有取到，本档一律留空，绝不估算**。
> - 小智侧事实来源（全部可点开核对）：[AI 原生全景图项目页](https://landscape.jimmysong.io/zh/projects/xiaozhi-esp32/)、
>   [乐鑫官方组件 `espressif/esp_xiaozhi` v0.1.2 文档](https://components.espressif.com/components/espressif/esp_xiaozhi/versions/0.1.2/readme)、
>   [立创 wiki「小智 AI」章节](https://openkits-wiki.easyeda.com/zh-hans/hspi-sf32lb52/lckfb-hspi-sf32lb52/xiaozhi.html)、
>   上游仓库链接 [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32)、[`78/xiaozhi-sf32`](https://github.com/78/xiaozhi-sf32)、
>   [`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server)、[`huangjunsen0406/py-xiaozhi`](https://github.com/huangjunsen0406/py-xiaozhi)。
> - 本项目侧事实来源：本仓代码与文档 + 2026-10-14 真机日志
>   （`%LOCALAPPDATA%\ComputerVoiceAssistant\logs\runtime.jsonl`、`audit\task-execution.jsonl`）+
>   `implementation_plan/research/2026-10-14-P10-T1-task-exec-real-device-and-latency.md`。

## 0. 一句话对比

- **小智**：`虾哥（GitHub 78）`2024-08-31 开源的**硬件 AI 聊天机器人**（MIT），以 **MCP 协议**为核心，
  固件跑在 ESP32（及思澈 SF32 等）上，配 `xiaozhi.me` 云端控制台与社区服务端，形成一个**硬件+云+插件**生态。
- **本项目**：**Windows 桌面**上的免按键语音助手（自研主链路 + 真实 Provider），重心不在硬件与云，而在
  **任务执行的安全链、审计、可解释的诚实性事实、以及真机可复现的故障定位**。
- 二者**形态、协议、目标用户几乎不重叠**；真正可比的是"**语音链路的工程口径**"与"**工具/控制面怎么做**"。

## 1. 逐项对照

| 维度 | 小智 AI | 本项目（电脑语音助手） | 备注 |
|---|---|---|---|
| 形态 | 硬件小盒子（黄山派 SF32LB52 / 多款 ESP32），配喇叭 + AMOLED | Windows 桌面进程（Python 3.12 venv），用系统麦克风/扬声器 | 完全不重叠 |
| 作者/协议/时间 | `78`（社区称虾哥），**MIT**，2024-08-31 | 私有仓库 `yyyydssj/computer-voice-assistant` | — |
| 官方背书 | 被**乐鑫收进官方组件** `espressif/esp_xiaozhi`（**Apache-2.0**，ESP-IDF ≥5.5） | 无 | 组件页显示累计下载 2.5k / 本版本 992（真实数字） |
| 唤醒 | 离线唤醒 **ESP-SR**（应用层集成），协议有 `esp_xiaozhi_chat_send_wake_word`；另有按键 | **sherpa-onnx KWS**（wenetspeech 3.3M）`--kws-mode gate` + **声纹过滤**（CAM++ `--voiceprint-mode filter`） | 两边都是端侧唤醒；我们多了声纹门禁 |
| ASR | 服务端侧流式 ASR（具体实现取决于所选服务端/云） | **SenseVoice int8**（sherpa-onnx）本地解码，实测 **125–520 ms**（中位 ~250 ms） | 我们的数是真机实测 |
| LLM | 接 `xiaozhi.me` 服务，支持 Qwen / DeepSeek 等 | 真实 **DeepSeek** transport（双条件 env 开关）+ 本地规则路由兜底；无 Provider 时如实降级 | 我们要"没模型就明说" |
| TTS | 服务端侧（协议支持 OPUS/G.711/PCM 回传） | 本机 **VoiceStudio `ws_pcm`** 流式（24 kHz PCM16 → waveOut），另有 `http_wav`/`edge`/`none` | 我们的瓶颈就在这：**≈2.9–3.4 字/秒** |
| 音频编解码 | **OPUS / G.711 / PCM** | 固定 **PCM16 24 kHz**（waveOut 硬约束） | 它的编解码面更宽 |
| 传输协议 | **WebSocket** 与 **MQTT+UDP** 可共存，服务端都提供时**优先 MQTT** | 本地进程内 16 kHz 采集 + WebSocket 连本机 TTS；无云端协议 | 它是"设备↔云"，我们是"进程内" |
| 打断 | 协议提供 `send_abort_speaking`（停播） | **barge-in 两阶段状态机** + 安全门（`echo_proven` 不成立就 **fail-closed 不停播**）+ **键盘 Enter/Esc 立即执行/取消**（#16）；**实测 AEC3 ERLE 5.87 dB（实测延迟）/ 15.54 dB（最佳延迟），双讲检测 268/268 误报 ⇒ 定条件性 no-go** | 我们"宁可不停播，也不假停播" |
| 工具/控制面 | **设备侧 MCP 工具**（`self.audio_speaker.set_volume`、LED、舵机、GPIO…），LLM 直接调用 | **静态白名单**（`start_whitelisted_application`，calc/notepad）+ 路径/哈希校验；`file_operation`/`dangerous` **fail closed**；不支持的句式**如实拒绝**（#12） | 它是**动态能力暴露**，我们是**静态最小授权** |
| 权限/安全链 | 未见公开的授权分级/审计链说明（未取到源码，不下结论） | P4/P5：ToolManifest + PathGuard + PolicyEngine + L1/L2/L3 授权 + 一次性消费 + 执行前复检 + 原子消费 | 这是我们最厚的部分 |
| 审计 | 未核实 | **哈希链审计** `audit/task-execution.jsonl`（专用文件 + 固定来源 id，跨进程可续追加）+ 运行日志脱敏 | — |
| 设备恢复 | 未核实（硬件侧应有联网重连/OTA） | **只在安全空闲点恢复**、不切未知设备、`busy`/`permission_denied` 不自动重试；输出断开→停播回退文字 | R3 交付 |
| UI | OLED/LCD + **表情** + 连接状态页 | 右下角 **336×112 逐像素卡片 HUD**（4 态：待机/聆听/播报/失败）+ 控制台文本 | 它表情更丰富，我们状态更"可解释" |
| 配网/绑定 | **BLE-PAN 共享手机网络** → `xiaozhi.me` 控制台**设备码 + 新建智能体** | 单实例锁 + `.env` 配置 + 环境变量；HUD 显示状态 | 它面向普通消费者，我们面向本机运维 |
| 延迟 | 官方仅称"低延迟"（未给数字） | **ASR 125–520 ms / 任务装配 0–94 ms / TTS 首字 0.1–0.3 s / 播放 ≈3 字每秒**（全部真机实测） | 我们连"首字延迟"都有独立字段 |
| 失败可解释性 | 有 `CHAT_ERROR` 事件与 TTS 状态事件；"极简协议层：只报协议事实，状态机/UI 归应用层" | 大量固定短码事实：`echo_proven`、`aec_erle_db/sufficient`、`truncation_reason`、`finish_timed_out`、`state_uncertain`、`speech_mute_*`、`kbd_codes`、`listen/reply/task_ms`… | **设计哲学高度一致** |
| 部署/运维 | 固件烧录（sftool / `uart_download.bat`）+ releases/网盘；服务端自建或云 | 单进程 `python -m app.main --voice --auto`；TTS 服务按需拉起（冷启 44–53 s） | 它要刷机，我们要装环境 |
| 测试与工程 | 未核实 | 全量 **3091 tests OK (skipped=3)**（164.3 s、exit 0）；关键口径都有回归（含"伪造已消除回声"这类诚实性反例） | 硬件项目通常不会这么测 |
| PC 客户端 | 有 **`py-xiaozhi`**（社区，让没硬件的人体验） | 我们本体就是 PC | 交集：都在 Windows 上跑 |

## 2. 各自真正强在哪

**小智强在**：① **硬件生态**（多芯片 + 70+ 开源硬件 + 现成外壳/PCB 教程，[立创 wiki](https://openkits-wiki.easyeda.com/zh-hans/hspi-sf32lb52/lckfb-hspi-sf32lb52/xiaozhi.html) 整章教学）；② **协议与工具面的标准化**（WS/MQTT 双通道择优、设备侧 MCP、事件面齐全）；③ **面向普通用户的心智**（设备码绑定、手机配网、表情 UI）；④ **官方背书与社区规模**（被乐鑫收编）。

**本项目强在**：① **安全链完整度**（授权分级、一次性消费、执行前复检、策略引擎、审计哈希链）；② **诚实性纪律**（能力≠事实：没测到就不许声称；不确定状态原样透出；不支持的句式直接拒绝而不是变成聊天）；③ **真机可复现的度量**（阶段耗时、`truncation_reason`、`echo_proven`、`kbd_codes`）；④ **测试密度**（3091 项）。

## 3. 可借鉴清单（按优先级）

1. **协议/工具面的"极简层"分工**（小智官方组件明确写"只报协议事实，状态机/UI 归应用层"）
   → 与我们既有"事实与能力分离"同源；可据此把 `auto_voice` 的事实面整理成**一层显式的协议事实**（连接/会话/TTS 状态/错误），
   让 UI/HUD/日志只消费事实，不再各自推断。
2. **停播/打断的显式控制面**：小智用 `send_abort_speaking` 一个语义明确的动作。
   → 我们已有键盘 Esc 与 barge-in 安全门，但**缺少一个统一的"停播"内部接口**（目前分散在 `cancel_active_speech`/`_stop_barge_in_playback`）。
   可考虑收敛成一个 `abort_speaking(reason)`，并把原因码写进事实。
3. **传输择优**（服务端同时给 WS 与 MQTT 时优先 MQTT）
   → 我们已有 `--tts-mode {ws_pcm,http_wav,pcm_seq,edge,auto}` 的同构思路；可参考它的"**探测 → 择优 → 如实记录选中项**"写法，
   把 `tts` 选择过程也落成事实（现在 `speech_route` 只记结果）。
4. **设备能力清单化**（MCP 工具 = 名字 + 描述 + 参数区间）
   → 我们的 `ToolManifest` 已有稳定内容哈希与 L1–L4 分类；可参考其对**参数区间**（如 volume 0–100）的显式建模，
   用于 #11「关闭应用」这类新工具的参数校验。
5. **面向用户的配网/绑定体验**：不适用于桌面，但"**一次扫码/一个设备码把设备接进云端 agent**"的简化思路，
   可借鉴到我们"首次运行向导"（现在要手写 `.env`）。

## 4. 不能照搬的点（会破坏我们既有口径）

- **动态工具面**：MCP 让 LLM 运行时决定调用哪个工具；我们现行口径是**静态白名单 + 哈希身份**（安全链依赖它）。
  若要引入 MCP，必须让它只做**候选生成**，授权/执行仍走 P5 安全链，否则等于把"谁来执行"交给模型。
- **云端服务依赖**：小智以 `xiaozhi.me` 为中心；我们的目标之一是**本机可用、离线可解释**，不能把关键路径挪到云。
- **硬件形态**：BLE-PAN 配网、ESP-SR 唤醒、OPUS 硬件解码，与 Windows 桌面栈无关。
- **未核实的部分（授权/审计/重试语义）**：源码取不到，**不能**据此评价它"没有安全链"，也不能照抄其协议字段。

## 5. 与本项目待办的衔接

| 我们的待办 | 小智能给的信息 | 结论 |
|---|---|---|
| **C：TTS 语速**（当前 ≈2.9–3.4 字/秒，比正常中文 TTS 慢） | 它的 TTS 在服务端，未知语速参数 | **无直接可借鉴**，仍需查本机 VoiceStudio 接口 |
| **#11「关闭应用」** | 设备侧工具的参数区间建模 | 借鉴其"工具名 + 参数区间 + 描述"的形式，用于关闭目标的选择与澄清 |
| **#13 静默窗** | 未见对应机制（未取到源码） | 保持默认关 |
| **"没反应"体验** | 它有明确的事件面（TTS_STATE 等），UI 可据事件即刻反馈 | 我们可参考"**事件先于音频**"：先给 HUD/提示，再播报 |
| 协议文档化 | 有 `docs/PROTOCOL_API_DRAFT.md`（仅见于组件页引用，**本会话取不到正文**） | 留作后续（需能访问 GitHub 时再读） |

## 6. 未核实项（明确列出，避免以后被误当成结论）

- 小智固件/服务端的 **star、fork、issue、contributor、最近提交** —— 未取到（GitHub 被拦），索引站显示为 `--`。
- 小智是否具备**授权分级、审计链、重试策略、设备恢复语义** —— 未取到源码，**不作评价**。
- 其 **ASR/LLM/TTS 具体实现与延迟数字** —— 官方只说"低延迟"。
- `esp_xiaozhi` 组件与其主仓 `78/xiaozhi-esp32` 的**版本对应关系** —— 组件页只给了 esp-iot-solution 仓库路径与 commit。
- 本项目侧数字均为 2026-10-14 真机/全量测试实测，未做跨机型复现。

## 7. 结论

两者**不是竞品**：小智解决"把语音 AI 塞进硬件并让普通人用起来"，本项目解决"在 Windows 上让语音真的能**安全地执行任务**且**失败可解释**"。
可借鉴的是它的**协议/事件分层**与**工具参数建模**；**不能**借鉴的是动态工具授权与云端依赖。
最值得抄的一句设计是它组件文档里的：**"只报协议事实，状态机、UI、系统命令执行归应用层"** —— 这正是我们这几天在 #13/#16/诊断批次里反复实践的东西。
