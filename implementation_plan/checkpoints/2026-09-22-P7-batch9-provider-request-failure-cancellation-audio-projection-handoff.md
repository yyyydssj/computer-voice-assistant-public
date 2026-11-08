# 2026-09-22 P7 批次 9：Provider request 失败/取消终态与音频块投影交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch8-provider-request-audio-chunk-completion-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

审计 Provider request 在以下阶段发生 `FAILED` 或 `CANCELLED` 时，本地生命周期快照与已经投影音频块集合是否保持一致：

1. 尚未接收音频块的零块阶段；
2. 已接收部分音频块、但尚未报告 `audio_produced` 的阶段；
3. 已报告 `audio_produced`、但尚未报告 `audio_enqueued` 的阶段；
4. 已报告 `audio_enqueued`、但尚未 `COMPLETED` 的阶段；
5. 终态快照被外部构造或替换为不一致 `state` 时的构造门控与原子性。

本批次不定义远端 Provider 的真实失败、真实取消、网络错误、播放中止或音频 payload 语义；只审计已经由本地调用方显式报告的生命周期事实。

## 2. 已完成实现与语义

### 2.1 终态快照一致性校验

`ProviderRequestLifecycle.__post_init__` 现强制校验：

- `events` 中每一项必须是 `ProviderRequestLifecycleEvent`；
- `FAILED`、`CANCELLED`、`COMPLETED` 等终态快照必须至少包含事件；
- `state` 必须与事件历史最后一个事件类型一致：
  - `FAILED` ↔ `FAILED`；
  - `CANCELLED` ↔ `CANCELLED`；
  - `COMPLETED` ↔ `COMPLETED`；
- 终态事件只能出现在事件历史末尾，历史前部不得出现任何终态事件。

因此，不能通过 `dataclasses.replace(...)` 或其他直接构造方式把已失败/已取消快照改写成 `COMPLETED`，也不能把终态事件插入到历史中部。

### 2.2 失败/取消不清除已存在的本地音频块事实

失败和取消沿用已有的生命周期投影规则：终态事件只改变 `state` 与事件历史，不清除此前已经成功写入的 `audio_chunks`，也不伪造新的音频事实。

- **零块失败**：
  - `state == FAILED`；
  - `audio_chunks == ()`；
  - `audio_produced == False`；
  - `audio_enqueued == False`。
- **部分块后取消**：
  - 保留已经接收的连续块及其顺序；
  - 不自动设置 `audio_produced`；
  - 不自动设置 `audio_enqueued`；
  - 最后事件为 `CANCELLED`。
- **`audio_produced` 后失败**：
  - 保留全部已投影块集合；
  - 保留 `audio_produced == True`；
  - 保持 `audio_enqueued == False`（若尚未报告入队）；
  - 不自动追加或伪造 `COMPLETED`。
- **`audio_enqueued` 后取消**：
  - 保留块集合；
  - 保留 `audio_produced == True`；
  - 保留 `audio_enqueued == True`；
  - 最后事件为 `CANCELLED`，历史中不得出现 `COMPLETED`。

这些布尔字段继续表示独立的本地事实：`audio_produced` 不表示播放开始或播放完成，`audio_enqueued` 不表示用户已经听见。

### 2.3 终态后的拒绝边界保持不变

- 终态之后继续拒绝迟到生命周期事件；
- 终态之后继续拒绝迟到音频块；
- 首个终态事实保持不被覆盖；
- 所有失败均在快照、事件历史和全局 event ID 集合写入前完成校验，拒绝路径不污染已有状态。

## 3. 专项测试

在 `tests/tts/test_provider_request_lifecycle.py` 新增/完成以下覆盖：

1. 零块失败保留空音频投影；
2. 部分音频块后取消保留已接收块，但不自动报告 `audio_produced` 或 `audio_enqueued`；
3. `audio_produced` 后失败保留块集合和 `audio_produced`，不伪造入队或完成；
4. `audio_enqueued` 后取消保留全部本地事实，不出现 `COMPLETED`；
5. 终态快照被替换为不一致 `state` 时抛出一致性错误，注册表中的原失败快照保持不变。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
  - 增加终态快照与事件历史末事件的一致性校验。
- `tests/tts/test_provider_request_lifecycle.py`
  - 增加失败/取消在零块、部分块、`audio_produced`、`audio_enqueued` 阶段的投影审计测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch9-provider-request-failure-cancellation-audio-projection-handoff.md`
  - 本交接文档；未修改路线图、任务计划或既有批次交接文档。

## 5. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行：

```powershell
.venv\Scripts\python.exe -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**23/23 通过**。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**59/59 通过**。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**404/404 通过**。

```powershell
.venv\Scripts\python.exe -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 检查：

- `app/tts/lifecycle/__init__.py`、`tests/tts/test_provider_request_lifecycle.py` 及本交接文档均按 UTF-8 严格读取成功；
- `U+FFFD` 替换字符数量：**0**。

## 6. 执行面审计

本批次确认：

- 未读取 `.env` 或其他秘密配置；
- 未访问网络；
- 未下载模型或依赖；
- 未调用真实 Provider；
- 未调用真实 `synthesize` 或远端 `cancel`；
- 未连接播放设备、扬声器、`sounddevice` 或真实音频输出；
- 未引入 PCM/编码 payload、音频时长、播放队列、背压或播放完成语义；
- 未实现重试、fallback、冷却、恢复探测或远端停止；
- 未修改 `implementation_plan/implementation-roadmap.md`、`task_plan.md`、`progress.md` 或既有批次交接文档；
- 未执行 Git add、commit 或 push。

## 7. 已知限制

- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 10：Provider request 终态后的迟到事件、迟到音频块与 reason/identity 原子性审计。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 审计失败/取消终态之后迟到事件、迟到音频块、重复 event ID、跨 request identity 和 reason/reason_code 的拒绝是否继续原子化；
4. 为该批次补充专项测试，并运行 P7/TTS 专项、全量 unittest、compileall、严格 UTF-8 和执行面审计；
5. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio、Edge TTS、真实 Provider、网络或真实音频流；
- 调用 `synthesize`、`cancel`、播放设备、扬声器或真实音频输出；
- 把本地音频块元数据伪装成远端停止、播放完成或用户已听见；
- 实现播放队列、PCM 时长预算、Provider fallback、冷却、恢复探测或重试；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。