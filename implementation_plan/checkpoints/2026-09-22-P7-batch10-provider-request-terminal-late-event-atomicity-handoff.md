# 2026-09-22 P7 批次 10：Provider request 终态迟到事件与 reason/identity 原子性审计交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch9-provider-request-failure-cancellation-audio-projection-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

审计 Provider request 在失败/取消终态之后，以及在失败原因码、request identity 和全局 event ID 校验失败时，本地生命周期注册表是否保持原子性：

1. 终态之后的迟到生命周期事件继续拒绝；
2. 终态之后的迟到音频块继续拒绝；
3. 重复 `event_id` 不得污染快照、事件历史或全局 event ID 集合；
4. 跨 request 的 `reply_id` 等 identity 不得污染现有状态；
5. `reason` / `reason_code` 与 `FAILED`、`CANCELLED` 的类型约束拒绝必须保持原子性；
6. 拒绝一个尚未显式读取过的已知 request 时，不得因为内部懒创建而写入空生命周期快照。

本批次仍只审计本地调用方显式报告的生命周期事实，不定义远端 Provider 失败、远端取消、网络错误、播放中止或音频 payload 语义。

## 2. 根因与最小修复

### 2.1 根因

`ProviderRequestLifecycleRegistry.record(...)`、`record_audio_chunk(...)` 和 `_record_simple(...)` 原先在校验前调用公开的 `get(...)`。公开 `get(...)` 具有懒创建语义：当 request 尚无快照时，会把 `PREPARED` 空快照写入 `_snapshots`。

因此，以下拒绝路径虽然没有追加事件，也没有加入 event ID，但会产生内部状态副作用：

- lifecycle event 的 `reply_id` 等 identity 不匹配；
- audio chunk 的 `reply_id` 等 identity 不匹配；
- `FAILED` 携带取消类 `reason_code`；
- `CANCELLED` 携带失败类 `reason_code`；
- 其他在新快照提交前失败的本地门控。

这不符合“所有校验在快照和全局 event ID 写入前完成”的原子性要求。

### 2.2 最小修复

在 `app/tts/lifecycle/__init__.py` 增加内部只读 `_snapshot_for(request)`：

- 已有快照：只返回现有快照；
- 尚无快照：只构造内存中的 `PREPARED` 快照，不写入 `_snapshots`。

保留公开 `get(...)` 的既有懒创建语义；只有公开读取或所有校验成功并构造 `next_snapshot` 后才写入注册表。

注册表写入顺序保持为：

1. request lookup；
2. identity、generation、event sequence、状态、reason/reason_code、音频块顺序等校验；
3. 构造不可变 next snapshot；
4. 写入 `_snapshots`；
5. 写入全局 `_event_ids`。

任何拒绝路径都不改变已有 snapshot、events、audio_chunks 或 `_event_ids`。

## 3. 专项测试

在 `tests/tts/test_provider_request_lifecycle.py` 新增/完成以下覆盖：

1. lifecycle event identity 拒绝不会创建空快照，也不会消费 event ID；随后复用相同 event ID 的合法 `ACCEPTED` 可以成功；
2. audio chunk identity 拒绝不会创建空快照，也不会消费 event ID；随后复用相同 event ID 的合法生命周期事件可以成功；
3. `FAILED` 的 reason/reason_code 错配拒绝不会创建空快照，也不会消费 event ID；随后使用同一 event ID 的合法失败原因码可以成功；
4. `CANCELLED` 终态后的迟到 audio chunk 拒绝不会修改终态快照、音频块集合或 event ID 集合；
5. 终态后的跨 request identity 迟到 event 与重复 event ID 均拒绝，首次终态事实保持不变；
6. 终态后的重复 event ID 拒绝保持首个失败快照和完整事件历史不变。

既有覆盖继续验证：

- `FAILED` / `CANCELLED` 在零块、部分块、`audio_produced`、`audio_enqueued` 阶段的音频投影；
- generation fence、request/reply/segment/provider/voice/audio/capability identity；
- 终态 state 与末事件一致性；
- 终态后迟到事件和迟到音频块拒绝；
- `reason` / `reason_code` 本地事实边界。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
  - 增加内部只读 `_snapshot_for(...)`；
  - 将事件、音频块和简单终态记录路径改为先使用只读快照，再在全部校验成功后提交。
- `tests/tts/test_provider_request_lifecycle.py`
  - 增加 identity、reason/reason_code、迟到音频块和重复 event ID 的原子性测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch10-provider-request-terminal-late-event-atomicity-handoff.md`
  - 本交接文档；未修改 batch9 或更早批次交接文档。

## 5. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行。

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**29/29 通过**。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**65/65 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**410/410 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 检查：

- `app/tts/lifecycle/__init__.py`、`tests/tts/test_provider_request_lifecycle.py` 与本交接文档均按 UTF-8 严格读取成功；
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
- 未实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 未修改 `implementation_plan/implementation-roadmap.md`、`task_plan.md`、`progress.md` 或既有批次交接文档；
- 未执行 Git add、commit 或 push。

## 7. 环境说明

项目现有 `.venv\Scripts\python.exe` 的 `pyvenv.cfg` 指向不可启动的历史系统 Python 路径；直接运行该解释器失败。项目内嵌运行时因 `python312._pth` 隔离配置不会自动导入项目根目录，直接用 unittest discovery 也无法解析 `app`。

因此本批次使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 ...
```

未修改 `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 8. 已知限制

- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- `_snapshot_for(...)` 是注册表内部只读构造辅助，不改变公开 `get(...)` 的懒创建 API 语义；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 9. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 11：Provider request 生命周期事件与音频块的跨 request 全局 event ID、generation fence 及公开读取副作用边界复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 复核跨 request 共享 `event_id` 的拒绝优先级与原子性、generation fence 与 identity 校验的顺序，以及公开读取 API 与内部写入路径的边界；
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