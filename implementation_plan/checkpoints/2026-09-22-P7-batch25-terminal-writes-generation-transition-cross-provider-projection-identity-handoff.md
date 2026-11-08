# 2026-09-22 P7 批次 25：Provider request 终态写入、generation transition 与跨 Provider projection identity 组合矩阵回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch24-provider-request-validation-priority-cross-provider-generation-isolation-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch24 已验证校验优先级、失败 event ID 可复用、跨 Provider generation fence 隔离及全局 event ID 边界的基础上，完成：

1. 组合回归 completed、failed、cancelled 三类终态写入；
2. 确认每个 request 的首个终态事实保持不变，终态后的迟到写入 fail closed；
3. 确认 generation transition 不改写既有终态 snapshot、events、audio_chunks projection；
4. 确认推进 `voicestudio` generation 不阻塞 `edge_tts` 的合法终态推进；
5. 确认失败写入留下的 event ID 不被消费，后续可由新 generation request 的合法写入复用；
6. 确认失败、迟到写入和 generation transition 交错后，跨 request、跨 Provider 的公开 projection identity 仍稳定；
7. 不扩大到真实 Provider、网络、播放设备、真实音频流或 P8。

## 2. 审查结论与最小变更

### 2.1 产品实现结论

`ProviderRequestLifecycleRegistry` 现有实现满足 batch25 目标，无需修改产品代码：

- `record(...)` 与 `record_audio_chunk(...)` 在完整校验、下一状态计算和不可变快照构造成功后才替换目标 snapshot 并消费 event ID；
- `_next_state(...)` 对 completed、failed、cancelled 终态执行一次性锁定，终态后的任何生命周期事件均 fail closed；
- `_assert_generation_current(...)` 按 `provider_name` 观察 generation fence，旧代和未激活代输入不会提交；
- `activate_generation(...)` / `advance_generation(...)` 只推进 fence，不创建或改写 request snapshot；
- 成功写入只替换目标 request snapshot，其他 request 的公开 snapshot、events、audio_chunks identity 不变；
- 失败路径不消费 event ID，因此未使用的失败 ID 可被满足当前绑定、generation、序列和状态约束的合法 request 复用；
- 本批次未发现需要新增或修改产品契约的缺口。

### 2.2 最小变更

仅修改：

- `tests/tts/test_provider_request_lifecycle.py`

新增专项回归测试：

- `test_terminal_writes_and_generation_transitions_preserve_cross_provider_projection_identity`（当前约第 2450 行）

测试覆盖：

1. `voicestudio` request 成功经历 accepted → synthesis_started → audio chunk → audio_produced → completed；
2. 独立 request 直接进入 failed；
3. 独立 request 直接进入 cancelled；
4. `edge_tts` request 在 `voicestudio` generation 推进前后继续合法写入并完成；
5. completed request 在 generation transition 前后的迟到 failed 写入均 fail closed；
6. failed/cancelled/completed request 在 `voicestudio=4` 后的迟到写入均 fail closed；
7. 迟到失败 ID 未消费，并被 generation-4 request 的合法 accepted 写入复用；
8. generation-4 request 再次被推进为旧代后，迟到 cancelled 写入不改变其公开 projection identity；
9. 各 request 的 snapshot、events、audio_chunks identity 和两 Provider 的 active generation 观察保持稳定。

未修改：

- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch24 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 验收结果

项目目录：`<PROJECT_ROOT>`

### 3.1 单项专项回归

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_terminal_writes_and_generation_transitions_preserve_cross_provider_projection_identity
```

结果：**1 项通过**。

### 3.2 P7/TTS 专项测试

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

结果：**101 项通过**。

### 3.3 全量 unittest

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

结果：**446 项通过**。

### 3.4 编译检查

```powershell
py -3.12 -m compileall -q app tests
```

结果：**通过**。

### 3.5 本批次修改文件编码检查

`tests/tts/test_provider_request_lifecycle.py`：

- 严格 UTF-8：通过；
- 无 BOM：通过；
- CRLF 换行：通过；
- 无裸 LF：通过。

仓库级扫描同时发现其他既有且未修改的 Python 文件中存在 3 个 BOM 文件和 120 个裸 LF 文件；这些不属于本批次变更范围，未擅自修复。

### 3.6 执行面审计

本批次：

- 未调用真实 VoiceStudio、Edge TTS 或任何 Provider adapter；
- 未调用真实网络、`synthesize`、远端 `cancel` 或远端停止；
- 未连接播放设备、扬声器、`sounddevice` 或真实音频输出；
- 未引入 PCM/编码 payload、音频时长、播放队列、背压或播放完成语义；
- 未实现 retry、fallback、冷却、恢复探测、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 未修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 未进入 P8 语音闭环；
- 未执行 Git add、commit 或 push。

## 4. 当前契约边界

- 公开 API 仍是进程内内存 registry，不提供跨进程、持久化或并发线程安全保证；
- generation fence 按 `provider_name` 共享，推进后既有旧代快照仍可读取，但旧代新增事件和音频块会被拒绝；
- event ID 在单个 lifecycle registry 内跨 Provider、跨 request、跨生命周期事件和音频块全局唯一；
- 失败写入不消费 event ID；该 ID 可由满足当前绑定、generation、序列和状态约束的合法写入使用；
- completed、failed、cancelled 是本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- `ProviderRequestAudioChunk` 仍是不可变音频块元数据，不承载 PCM/编码 payload；
- 本批次验证的是同步失败路径原子性，不承诺并发线程安全；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 5. 下一批次入口与强制停止

本批次完成后停止。下一批次如继续，必须仍以新的明确交接文档为入口，并保持以下边界：

- 只在现有本地 Provider request lifecycle 契约之上做最小、可验证的专项工作；
- 不得跳入真实 Provider、网络、播放设备、真实音频流或 P8；
- 不得把本地终态、音频块或 enqueue 事实扩展解释为远端停止、播放完成或用户已听见；
- 不得修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 若下一批次无新的交接文档授权范围，停止并等待新的交接指令。

## 6. 新会话直接续接指令

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch25-terminal-writes-generation-transition-cross-provider-projection-identity-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```