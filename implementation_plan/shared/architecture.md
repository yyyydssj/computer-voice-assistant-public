# 公共架构、配置与降级方案

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `../archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `../archive/implementation_plan.legacy.md`。

## 项目最高原则：实时语音 AI 伙伴

本项目的本质不是“录音 → ASR → 播报”的单轮语音工具，而是：

> **用户与 AI 持续实时对话；既可以闲聊，也可以在提出需求时安全地操控电脑。**

产品层必须始终保持一个连续对话体验：

```text
用户说话
→ AI 实时理解
→ 判断是闲聊还是电脑任务
→ 闲聊：自然回应
→ 电脑任务：理解、规划、按安全规则请求授权、执行并汇报
→ 回到持续对话
```

“按键触发”“VAD 自动收音”“唤醒词”“连续对话”“实时打断”等是**开发阶段和底层实现顺序**，不是产品定位，也不是互相割裂的产品模式。任何阶段性降级都不得把项目重新定义成单轮录音应用。

## 并列最高原则：人物模板驱动的拟人表达

AI 面向用户说出的自然语言必须遵循用户选择并确认的人物模板。人物感不是后期装饰或可选功能，而是核心交互的一部分；由于闲聊是主要使用场景，设计和验收必须优先保证自然、口语化、有连续人格和情绪温度的对话体验，不能退化成客服腔、工具播报腔或机械状态提示。

- 闲聊、追问、澄清、没听清提示、错误说明、任务进度和执行结果的**语音表达**都要经过人物模板约束。
- `../persona.md` 是当前人物模板的权威来源；只有用户可以修改、切换或停用人物模板，Agent 不得自行改写人设、关系、称呼或表达风格。
- VoiceProfile 决定“声音听起来怎样”，人物模板决定“AI 说什么、怎么说”；两者必须同时生效，但相互独立，不能用换音色代替人物表达。
- 人物表达不得削弱真实性、安全性和授权清晰度。路径、动作、风险、失败原因、确认范围以及固定授权短语必须准确完整；可以拟人化措辞，但不能撒娇诱导授权、淡化风险或省略关键信息。
- `plan.json`、审计日志、执行方案、结构化错误码等机器或正式文档不强制使用人物口吻；人物模板主要约束面向用户的对话和语音层。

## 5. 关键配置项（config.py）

| 配置 | 默认值 | 说明 |
|---|---|---|
| wake_word `[用户配置/示例值]` | 小饼 | 唤醒词，用户可替换 |
| wake_sensitivity | 0.5 | 唤醒灵敏度（0~1，越低越灵敏） |
| voiceprint_threshold | 0.7 | 声纹相似度阈值 |
| vad_silence_sec | 1.0 | 判说完的静音秒数 |
| max_record_sec | 30 | 单次录音上限 |
| audio_input_device_mode | fixed | 默认固定使用用户选择的输入设备；另有 `system_default` 配置模式 |
| audio_input_device_id `[用户配置]` | 未设置 | 固定输入设备的稳定标识；不得写死具体个人设备 |
| audio_output_device_mode | fixed | 默认固定使用用户选择的输出设备；另有 `system_default` 配置模式 |
| audio_output_device_id `[用户配置]` | 未设置 | 固定输出设备的稳定标识；不得写死具体个人设备 |
| audio_device_check_timeout_sec | 2 | 设备枚举和初始化检查超时 |
| idle_timeout_sec | 30 | 静默回待机时长（连续对话/误唤醒） |
| confirm_timeout_sec | 10 | 安全确认超时 |
| command_timeout_sec | 30 | 命令执行超时 |
| l3_confirmation_phrase | 确认执行 | L3 独立确认状态的固定完整短语，不可配置 |
| cancel_words | 取消/不/别/算了 | 取消词集 |
| stop_words | 停/停止 | 紧急停止词 |
| blacklist_dirs | C:\Windows、Program Files、AppData | 写/删黑名单 |
| max_file_mb | 10 | 读文件大小上限 |
| history_rounds | 20 | 会话历史轮数 |
| allowed_root `[用户配置/本机配置示例]` | <STUDY_ROOT> | 当前用户示例；开源版本由用户填写允许访问根目录 |
| default_work_dir `[用户配置/本机配置示例]` | <STUDY_ROOT>\AI工作区 | 当前用户示例；开源版本由用户填写默认工作目录 |
| deepseek_api_key `[敏感信息]` | 环境变量或未提交的 `.env` | 用户自行填写；真实值不得提交 |

## 6. 风险与降级方案

| 风险 | 影响 | 降级方案 |
|---|---|---|
| 回声消除（AEC）Python 库不成熟 | AI 误唤醒自己 | 先做「播放时降灵敏度」，AEC 后补 |
| 声纹误拒（主人喊不醒） | 用不了 | 调低阈值；提供「声纹关闭」开关 |
| ASR 误识别 | 听错导致操作偏差 | SenseVoice 事件检测过滤非人声；置信度低重问；危险操作走确认 |
| DeepSeek 工具调用不稳定 | 乱调工具 | 工具描述写清楚；重试；限制工具数 |
| 唤醒词模型中文支持 | 识别差 | 用 openWakeWord 自定义模型；或按键兜底 |

---

## Intent routing boundary (2026-09-24)

Text and voice inputs share one business chain:

```text
TextShell / ASR accepted final
-> ConversationManager
-> IntentRouter
-> RouteHandoff
-> AgentAdapter / read-only tools / ActionPlan / Safety
-> authorization -> Execution Ledger -> Executor -> Verification -> Audit
```

- Text and voice are different `source` values only: `text_shell` and `asr`. Keep the existing `Utterance.input_mode=text|voice` as-is.
- ASR interim, unaccepted final, empty recognition, duplicate, late, out-of-order, or old-generation events cannot create an executable handoff.
- Every handoff carries `request_id`, `conversation_id`, `turn_id`, `source`, `raw_text`, and `created_at`.
- The router classifies only. It cannot authorize, execute, or call computer tools. Any failure normalizes to `route=ambiguous`; the Agent cannot freely reclassify after failure.
- Slots are candidate values, not confirmation or authorization. Downstream must re-run Schema, PathGuard, ToolRegistry, and Policy/Safety checks.
- Laya is only a replaceable provider; its current `billing/technical` model is not a computer-assistant intent model.

Authoritative contract: `docs/superpowers/specs/2026-09-24-intent-routing-contract-design.md`.
