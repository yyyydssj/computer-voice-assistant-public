# P6 批次 6：ASR 故障与文字模式降级停止交接

- 日期：2026-09-21
- 阶段：P6 ASR 按键输入
- 批次状态：**complete**
- 阶段状态：**in_progress**
- 当前停止点：**P6 六个实现批次均已完成；停在 P6 阶段级集成验收门前，尚未进入 P7/TTS**

## 1. 接续与范围

严格接续 `implementation_plan/checkpoints/2026-09-21-P6-batch5-result-gate-stop-handoff.md`。本批次只实现 ASR startup/session/runtime 故障、文字输入降级、恢复探测与“恢复后仍等待用户切回”的有限状态机。

本批次没有提交 Agent 请求、创建 `request_id`、接入 TTS、实现 P6 跨模块闭环验收、访问真实麦克风、加载真实模型、启动后台线程/定时器、自动重试、自动切换 Provider、自动切回语音、开放连续监听/唤醒词/声纹/AEC/实时打断或语音 L3。

## 2. 已完成实现

### 2.1 `app/asr/recovery.py`

新增纯内存、显式驱动的 `AsrRecoveryController`：

- 工程状态覆盖 `ASR_UNAVAILABLE_STARTUP`、`ASR_READY`、`ASR_SESSION_WARMING`、`ASR_SESSION_ACTIVE`、`ASR_SESSION_FAILED_RUNTIME`、`ASR_COOLDOWN`、`ASR_PROBING`、`ASR_RECOVERED_PENDING_USER`，并保留初始 `ASR_STARTUP_LOADING`；
- 启动加载失败或超时只接纳一次故障事实，立即保持/进入文字输入，不无限等待、不循环重试；
- 会话预热失败或取消会清除本轮资源，保持文字输入，不把预热材料补识别；
- 运行期故障关闭当前语音边界并丢弃残留资源，不创建请求、不重开旧 utterance；
- 故障状态只保存枚举化 `stage + code + occurred_at`，公开状态不包含异常正文、路径、堆栈或用户音频内容；
- 恢复探测必须连续达到配置次数；失败回到 cooldown，成功只进入 `RECOVERED_PENDING_USER`，仍保持文字输入；
- 用户请求切回但不在安全空闲点时返回 `deferred`；到达安全空闲点后只回到可预热的 `READY`，不会直接启用麦克风；
- 正式语音激活必须提供全新的 `session_id + audio_session_id + provider_generation + stream_id + utterance_id`；已使用边界不能复用；
- 恢复切换前再次清理旧资源；注入式清理回调异常只形成 `cleanup_failed` 诊断事实，不泄露异常正文，也不能阻止逻辑上关闭语音和进入文字模式；
- 模块无后台任务、网络、设备、文件、进程、命令或 `.env` 入口，期限和低频调度由未来上层编排显式实施。

### 2.2 `app/asr/__init__.py`

公开导出恢复状态、失败分类、会话边界、转换决策和控制器类型；模块说明更新为覆盖 SenseVoice adapter、ResultGate 与故障降级状态机。

### 2.3 `tests/asr/test_recovery.py`

新增 12 个测试，覆盖：

- startup 失败/超时的一次性文字降级与脱敏状态；
- startup ready 后仍不自动抢占文字输入；
- warmup 失败、直接取消及重复取消；
- 只有新会话边界可进入 active，旧边界恢复后拒绝；
- runtime 故障关闭当前轮次且重复故障 no-op；
- 连续恢复探测、失败重置和恢复后待用户；
- 非安全空闲点延迟切回，安全空闲后仍须重新预热；
- 清理回调异常时 fail-closed；
- 非法状态转换不触发资源动作。

### 2.4 `tests/asr/__init__.py`

新增测试包标记，修复标准 `unittest discover -s tests -t .` 过去会遗漏整个 `tests/asr` 目录的问题。此前交接中的“全量 310/310”实际未包含 ASR 目录；本批次之后的标准全量计数已包含全部 ASR 测试。

## 3. 最终验证结果

在项目根目录 `<PROJECT_ROOT>` 内使用嵌入式 Python：

- P6 批次 6 恢复专项：**12/12**；
- ASR 全专项（SenseVoice + ResultGate + Recovery）：**29/29**；
- 音频专项（AudioBus + Capture + VAD）：**31/31**；
- 标准全量 `unittest discover -s tests -t .`：**339/339**；
- `compileall -q app tests`：通过；
- `app` 与 `tests` 下 Python 文件严格 UTF-8：**118** 个文件均可解码且无 `U+FFFD`；既有 BOM 文件仍为 `tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`，本批次未修改；
- `app/asr` 与 `tests/asr` AST/文本执行面审计：**7** 个 Python 文件，无 `subprocess`、socket、requests/httpx/urllib、sounddevice/pyaudio、进程/命令、PowerShell/cmd 或 `.env` 入口；
- 未读取或修改 `.env`，未下载模型，未访问网络，未启动 Harness，未执行真实麦克风或电脑副作用，未执行 Git add/commit/push。

## 4. 已观察的工具/测试错误

| 错误 | 尝试次数 | 处理 |
|---|---:|---|
| 猜测读取 `app/audio/capture.py`，实际模块位于 `app/audio/capture/__init__.py` | 1 | 改用目录清单定位，不重复猜测路径 |
| 嵌入式 Python 对 `discover('tests/asr')` 的中文绝对路径可导入性失败 | 1 | 改用标准包 discovery 或 `Path.cwd()` 显式路径 |
| 动态全量 loader 未先注册 `sys.modules`，既有 dataclass 测试加载失败 | 1 | 识别为 runner 问题；最终证据改用标准 unittest discovery |
| 直接按模块名运行专项时项目根不在模块搜索路径 | 1 | 改用 `discover -s tests/asr -t .` |
| 标准全量 discovery 原先遗漏 `tests/asr` | 1 | 新增 `tests/asr/__init__.py`，全量计数由 310 变为真实包含 ASR 的 339 |

## 5. 已知限制与延期

- 控制器接收“期限已超时/Provider 已失败/探测已通过”等事实，本批次不创建线程、定时器或任务调度器；具体时间上限与真机资源阈值仍留待后续编排和阶段 13 校准；
- 本批次没有把 `SenseVoiceRecognizerAdapter`、Capture/VAD、ResultGate、RecoveryController 和文字 ConversationManager 组合成端到端 P6 主控；该组合必须由下一次 **P6 阶段级集成验收**验证；
- `accepted_final` 仍未创建 `request_id`；P6 退出条件中的“一次 final 只提交一个新 request”必须在阶段级集成验收中通过受控编排测试证明，不能由本状态机暗示；
- 恢复切回前的真实本机麦克风与 ASR 联合测试、真实模型文件、Windows wheel、资源阈值和设备行为尚未验证；
- 未实现低置信度、多人重叠或说话人身份可靠判定；SenseVoice 对这些能力仍为 `unsupported`，不得解释成 `not_detected`；
- P7 TTS、P8 语音闭环、P9 连续监听和 P10 打断/AEC 均未进入。

## 6. 下一入口与强制停止

下一批次只能是 **P6 阶段级集成验收与 P6 handoff**，不是 P7。

验收至少应证明：

1. Capture/VAD/Provider/ResultGate/Recovery 的一次按键语音路径只产生一次可提交 final；
2. final 与故障竞态、重复/迟到/旧边界事件不得创建第二个请求；
3. ASR 启动、预热或运行期不可用时，文字输入仍可走既有 P3/P4/P5 安全链；
4. ASR 故障不改变任务、计划、授权、执行或结果事实；
5. 临时音频和旧 Provider 事件按规则清理且不进入 Agent/日志/审计正文；
6. 阶段级回归、安全审计和验收文档通过后，才可把 P6 标记 `complete` 并停在 P7 授权门前。

下一轮仍不得提前接入 TTS、连续监听、唤醒词、声纹、AEC、实时打断、语音 L3、真实云端 ASR 或未经受控边界允许的真实设备副作用。
## 7. 2026-09-21 P6 阶段级集成验收记录

本次严格按第 6 节的六项要求执行验收。验收只使用内存 fake provider、fake capture stream、受控 ConversationManager adapter 和临时目录；未访问真实麦克风、真实模型、网络、`.env` 或电脑副作用。

### 7.1 已执行的回归与静态证据

- P6 批次 6 恢复专项：**12/12**；
- ASR 全专项（SenseVoice + ResultGate + Recovery）：**29/29**；
- 音频专项（AudioBus + Capture + VAD）：**31/31**；
- 现有集成专项：**6/6**；
- 标准全量 `unittest discover -s tests -t .`：**339/339**；
- `compileall -q app tests`：通过；
- `app` 与 `tests` 下 Python 文件严格 UTF-8：**118** 个文件均可解码且无 `U+FFFD`；既有 BOM 文件仍为 `tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`；
- `app/asr`、`tests/asr`、`app/conversation`、`tests/conversation` 执行面审计：未发现网络、进程、命令、`.env` 读取入口；音频真实 `sounddevice` 只保留在延迟导入适配层，组合验收未调用真实设备；
- 代码检索确认当前没有仓库内持久化的 P6 阶段级主控测试，把 Capture/VAD/SenseVoice/ResultGate/Recovery 与 ConversationManager 组合起来的临时脚本未写入仓库。

### 7.2 临时受控组合验收结果

使用一次 fake 按键音频帧，按以下顺序组合：

```text
CaptureSession
→ AudioBus
→ VadSession
→ SenseVoiceRecognizerAdapter(fake recognizer)
→ ResultGate
→ ConversationManager(fake Agent adapter)
```

结果：

- 一次有效 final 被接纳并进入 ConversationManager；输出 `ONE_PATH_ACCEPTED_FINAL=1`；
- 同一 final 重放返回 `duplicate`；
- 终态后的迟到事件返回 `after_terminal`；
- 新建的旧 generation 事件返回 `stale_generation`；
- 初始有效 final 只导致 **1** 次 Agent adapter 调用；随后注入运行期 ASR 故障，调用数保持 **1**；
- startup 故障后输入模式为 `text`，文字提交仍可通过既有 ConversationManager 完成；
- 受控资源清理事实记录为 `runtime_failure`、`startup_failure`；
- 未创建任务、计划、授权或执行副作用，输出 `TASK_PLAN_AUTH_EXECUTION_MUTATION=0`。

该脚本只证明组件在受控内存边界内的组合行为，不替代应提交到仓库并纳入标准 discovery 的阶段级集成测试。

### 7.3 六项 P6 阶段级退出条件核对

| 退出条件 | 本次证据 | 结果 |
|---|---|---:|
| 1. Capture/VAD/Provider/ResultGate/Recovery 的一次按键语音路径只产生一次可提交 final | 临时组合链路 + ASR/音频专项；但 Recovery 未作为仓库内主控编排接入 | **部分通过** |
| 2. final 与故障竞态、重复/迟到/旧边界事件不得创建第二个请求 | ResultGate 专项、Recovery 专项、临时组合中的 duplicate/after-terminal/stale-generation 与调用次数断言 | **通过（受控边界）** |
| 3. startup、预热或运行期不可用时，文字输入仍可走既有 P3/P4/P5 安全链 | startup/运行期到文字模式与 ConversationManager 已验证；ASR fallback 到完整 P4/P5 链未形成仓库内集成证据 | **部分通过** |
| 4. ASR 故障不改变任务、计划、授权、执行或结果事实 | 本次未创建这些对象，也未发生真实改变；但未通过阶段级跨模块断言证明“不改变” | **未证明** |
| 5. 临时音频和旧 Provider 事件按规则清理且不进入 Agent/日志/审计正文 | 临时音频/旧事件未进入 Agent 已受控验证；未形成日志/审计正文的仓库内断言 | **部分通过** |
| 6. 阶段级回归、安全审计和验收文档完整通过后标记 P6 `complete` | 组件回归和静态审计通过，但阶段级集成测试与完整安全断言尚未入库 | **不通过** |

## 8. 验收结论

- **P6 批次 6：保持 `complete`。**
- **P6 阶段：保持 `in_progress`，不得标记 `complete`。**
- 本次未发现需要立即修改的组件级故障；未修改功能代码。
- 当前缺口是阶段级验收证据与主控编排测试缺失，不应通过修改交接文字把“临时组合通过”提升为“阶段完成”。
- 下一工作入口仍只能是：新增并纳入标准 discovery 的 **P6 阶段级集成验收与 P6 handoff**，至少覆盖文字 P3/P4/P5 安全链、任务/计划/授权/执行/结果不变性、临时音频与日志/审计脱敏边界。

## 9. 验收期间的工具/测试装配记录

| 事项 | 次数 | 处理 |
|---|---:|---|
| 临时脚本直接从系统临时目录启动导致 `ModuleNotFoundError: app` | 1 | 显式插入项目根目录后重跑；属于 runner 装配问题 |
| 临时 fake Agent 返回缺少 `finish_reason` 导致 ConversationManager 将结果视为不完整 | 1 | 补齐受控 fake 返回的 `finish_reason='stop'`；属于测试装配问题 |
| 在已终态 ResultGate 上验证旧 generation，先被终态规则优先返回 | 1 | 改为在独立 gate 上验证旧 generation；同时在终态 gate 上单独验证 `after_terminal` |

## 10. 强制停止（更新后）

继续停在 **P6 阶段级集成验收门前**，尚未进入 P7/TTS。

下一轮不得：

- 把本次临时受控组合脚本当作已入库的阶段级验收；
- 将 P6 阶段标记为 `complete`；
- 接入 TTS、连续监听、唤醒词、声纹、AEC、实时打断、语音 L3、真实云端 ASR 或未经受控边界允许的真实设备副作用。

## 11. 2026-09-21 追加复核（本轮）

本轮在严格接续本交接文档的强制停止点后重新执行验收复核；未进入 P7/TTS，未修改功能代码，未接入真实设备、真实模型、网络、`.env` 或电脑副作用。

### 11.1 可复现回归结果

- P6 ASR 全专项：**29/29**；
- 音频专项：**31/31**；
- 现有集成专项：**6/6**；
- 标准全量 `unittest discover -s tests -t .`：**339/339**；
- `compileall -q app tests`：**通过**；
- `app` 与 `tests` 下 Python 文件严格 UTF-8：**118/118**，无 `U+FFFD`；
- ASR、音频、现有集成专项均可独立通过标准 discovery；
- ASR/Conversation 受控执行面扫描未发现网络、进程、命令、`.env` 或真实音频设备入口。

### 11.2 阶段级结论复核

本轮结果与第 7、8 节保持一致：组件专项与既有集成回归通过，但仓库仍没有正式入库、纳入标准 discovery 的 P6 阶段级主控集成测试；`RecoveryController` 仍未在仓库内与 Capture/VAD/Provider/ResultGate/ConversationManager 形成正式编排。因此：

- 退出条件 1：**部分通过**；
- 退出条件 2：**通过（受控边界）**；
- 退出条件 3：**部分通过**；
- 退出条件 4：**未证明**；
- 退出条件 5：**部分通过**；
- 退出条件 6：**不通过**。

### 11.3 更新后的交接与停止点

- P6 批次 6：保持 **`complete`**；
- P6 阶段：保持 **`in_progress`**，不得标记 `complete`；
- 当前验收结论：**组件级通过，P6 阶段级验收未通过/证据不足**；
- 下一入口仍是新增并入库 P6 阶段级集成验收，覆盖 Recovery 正式编排、P3/P4/P5 文字安全链、事实不变性、临时音频清理及日志/审计脱敏边界；
- 继续强制停止于 P6 阶段级集成验收门前，**不得进入 P7/TTS**。

### 11.4 本轮命令记录

```text
.venv\\Scripts\\python.exe -m unittest discover -s tests/asr -t .       -> 29/29 OK
.venv\\Scripts\\python.exe -m unittest discover -s tests/audio -t .     -> 31/31 OK
.venv\\Scripts\\python.exe -m unittest discover -s tests/integration -t . -> 6/6 OK
.venv\\Scripts\\python.exe -m unittest discover -s tests -t .            -> 339/339 OK
.venv\\Scripts\\python.exe -m compileall -q app tests                  -> OK
严格 UTF-8 扫描 app/tests Python 文件                                  -> 118/118 OK
ASR/Conversation 受控执行面扫描                                          -> 0 命中
```