# 2026-09-22 P7 批次 11：Provider request 跨 request event ID、generation fence 与公开读取边界交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch10-provider-request-terminal-late-event-atomicity-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

复核 `ProviderRequestLifecycleRegistry` 在多个 Provider request 共用同一生命周期注册表时的边界：

1. 全局 `_event_ids` 是否跨 request、跨生命周期 event/audio chunk 统一去重；
2. event 与 audio chunk 同时存在 identity 错配和 generation 错配时，拒绝优先级是否一致；
3. generation fence 是否仍只接受当前已激活 generation，且拒绝路径不写入快照或 event ID；
4. 公开 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 的懒创建语义是否与内部写入路径清晰分离；
5. 公开读取创建 PREPARED 快照后，后续被拒绝的写入是否仍保持该快照、事件历史、音频块和全局 event ID 不变。

本批次仍只审计本地调用方显式报告的生命周期事实，不定义远端 Provider 失败、远端取消、网络错误、播放中止或音频 payload 语义。

## 2. 复核结果与最小修复

### 2.1 复核结果

`record(...)` 的 lifecycle event 路径原本先执行 `_assert_bound_to_request(...)`，再执行 `_assert_generation_current(...)`；`record_audio_chunk(...)` 原本反过来先执行 generation fence，再执行 audio chunk identity 校验。

因此，当同一个输入同时包含 identity 错配和 stale/future generation 时，event 与 audio chunk 会产生不一致的拒绝原因。这会让调用方无法依赖统一的本地门控顺序，也削弱跨事件类型的原子性审计可读性。

### 2.2 最小修复

在 `app/tts/lifecycle/__init__.py` 的 `ProviderRequestLifecycleRegistry.record_audio_chunk(...)` 中，将校验顺序统一为：

1. request lookup；
2. audio chunk 与 Provider request 的 identity/capability/audio format 校验；
3. provider generation fence；
4. 终态、synthesizing 状态、音频块顺序和冻结门控；
5. 构造不可变 next snapshot；
6. 写入 `_snapshots`；
7. 写入全局 `_event_ids`。

没有改变公开 `get(...)` 的懒创建语义，也没有改变 lifecycle event 路径既有的原子提交顺序。

## 3. 专项测试

在 `tests/tts/test_provider_request_lifecycle.py` 新增/调整以下覆盖：

1. `test_global_event_ids_are_shared_across_requests_and_event_kinds`
   - 同一 `event_id` 在不同 request 之间拒绝；
   - lifecycle event 使用过的 ID 不能被 audio chunk 复用；
   - audio chunk 使用过的 ID 不能被另一 request 复用；
   - 拒绝不污染第二个 request 的快照、事件或音频块。
2. `test_identity_rejection_precedes_generation_fence_for_event_and_audio_chunk`
   - event 同时 identity 错配与 generation 错配时先拒绝 identity；
   - audio chunk 同时 identity 错配与 generation 错配时也先拒绝 identity；
   - 两条拒绝路径均不消费 event ID、不创建写入快照；
   - 复用相同 ID 的合法 accepted event 可以成功。
3. `test_public_reads_may_lazily_create_prepared_snapshot_but_rejected_writes_do_not_mutate_it`
   - `events_for(...)`、`audio_chunks_for(...)` 和 `get(...)` 继续允许公开读取懒创建 PREPARED 快照；
   - 公开读取后 identity 错配的拒绝写入不会修改该快照或全局 event ID 集合。
4. 调整 `test_audio_chunks_reject_stale_future_and_terminal_late_chunks`
   - stale/future generation 场景使用 identity 一致的 chunk，专门验证 generation fence；
   - 终态迟到 chunk 继续验证终态快照和音频块集合保持不变。

既有覆盖继续验证：

- 跨 request 的 request/reply/segment/provider/voice/audio/capability identity；
- 全局 event ID 重复、跨 event kind 重复和拒绝原子性；
- generation fence 的 stale/future/rollback 行为；
- 终态迟到事件与迟到音频块拒绝；
- `reason` / `reason_code` 本地事实边界；
- 音频块元数据不等同于播放或用户已听见。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
  - 统一 audio chunk identity 校验与 generation fence 的顺序。
- `tests/tts/test_provider_request_lifecycle.py`
  - 新增跨 request 全局 event ID、identity/generation 顺序、公开读取懒创建边界测试；
  - 调整 generation fence 测试输入，使其不混入 identity 错配。
- `implementation_plan/checkpoints/2026-09-22-P7-batch11-provider-request-cross-request-event-id-generation-read-boundary-handoff.md`
  - 本交接文档；未修改 batch10 或更早批次交接文档。

## 5. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行。

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**32/32 通过**。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**68/68 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**413/413 通过**。

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

因此本批次继续使用机器上可正常启动的 Python 3.12.10：

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

**P7 批次 12：Provider request lifecycle 公开读取 API、快照/历史一致性与 generation activation 原子性复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 复核 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 在未知 request、公开懒创建、只读快照复用和拒绝写入后的可观察一致性；
4. 复核 `activate_generation(...)` / `advance_generation(...)` 在非法输入、回退 generation 和重复激活时的状态原子性，并补充专项测试；
5. 完成后运行 P7/TTS 专项、全量 unittest、compileall、严格 UTF-8 和执行面审计；
6. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio、Edge TTS、真实 Provider、网络或真实音频流；
- 调用 `synthesize`、`cancel`、播放设备、扬声器或真实音频输出；
- 把本地音频块元数据伪装成远端停止、播放完成或用户已听见；
- 实现播放队列、PCM 时长预算、Provider fallback、冷却、恢复探测或重试；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。