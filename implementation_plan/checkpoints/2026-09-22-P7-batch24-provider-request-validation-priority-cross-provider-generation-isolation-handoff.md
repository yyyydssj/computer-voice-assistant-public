# 2026-09-22 P7 批次 24：Provider request 校验优先级、失败 ID 可复用与跨 Provider generation 隔离组合矩阵回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch23-provider-request-cross-request-shared-event-id-generation-fence-matrix-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch23 已验证跨 request 成功提交隔离、全局共享 event ID、旧代失败 ID 可复用及 generation fence 组合边界的基础上，完成 P7 Provider request lifecycle 的下一轮校验顺序与跨 Provider 隔离回归：

1. 锁定重复 event ID、未知 request、绑定错误、generation 错误、事件序列错误之间的 fail-closed 校验优先级；
2. 确认每一种失败路径都不会消费 event ID、创建未知 request snapshot 或改变既有 request projection；
3. 确认失败写入留下的 event ID 可由满足当前绑定、generation、序列和状态约束的合法写入复用；
4. 确认不同 Provider 的 generation fence 彼此隔离，推进一个 Provider 不阻塞另一 Provider 的合法写入；
5. 确认旧代音频块失败留下的 event ID 可以在另一 Provider 的合法音频块写入中复用，但随后仍受 lifecycle registry 全局 ID 冲突约束；
6. 确认组合失败后，公开 snapshot、events、audio_chunks identity 以及 generation 观察仍保持一致；
7. 不扩大到真实 Provider、网络、播放设备、真实音频流或 P8。

## 2. 审查结论与最小变更

### 2.1 产品实现结论

`ProviderRequestLifecycleRegistry` 现有实现满足 batch24 的校验顺序与跨 Provider 隔离要求，无需修改产品代码：

- `record(...)` 和 `record_audio_chunk(...)` 先检查 lifecycle registry 范围内的全局 event ID 重复，再查找 request；
- 未知 request 在 event ID 未重复时 fail closed，不创建 snapshot，也不消费 event ID；
- request 绑定字段校验早于 generation fence，保证错绑输入不会被错误归因成 generation 错误；
- generation fence 校验早于事件序列和状态推进，旧代或尚未激活代的输入不会消费 ID；
- 只有完整校验、下一状态计算和不可变快照构造均成功后，才替换目标 request snapshot 并写入全局 event ID；
- `_active_generations` 按 `provider_name` 隔离，推进 `voicestudio` 不改变 `edge_tts` 的观察或合法写入能力；
- `_event_ids` 仍是整个 lifecycle registry 共享的集合，因此跨 Provider、跨 request、跨 lifecycle event/audio chunk 的重复 ID 均 fail closed；
- 失败路径不创建目标 request 的惰性 snapshot，且成功复用失败 ID 时只替换目标 request 的快照，其他 request projection identity 保持不变。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项 batch24 专项回归测试；未修改产品实现：

1. `test_validation_priority_is_fail_closed_and_rejected_ids_remain_reusable`（第 2261 行）
   - 构造重复 ID + 未知 request + 错误绑定 + 错误 generation + 非法序列的组合输入；
   - 确认校验顺序依次命中：`event_id 不得重复`、未知 request、绑定字段和 generation fence、最终才是非法事件序列；
   - 确认每个失败 ID 均未进入 `_event_ids`；
   - 确认未知/错绑/旧代失败 ID 可由不同的合法 request 成功复用；
   - 确认原 request、第二 request、第三 request、generation 4 request 的公开 snapshot 保持隔离。
2. `test_cross_provider_generation_fences_are_isolated_and_failed_audio_ids_reuse_globally`（第 2374 行）
   - 同时激活 `voicestudio=3` 与 `edge_tts=7`；
   - 两个 Provider 各自成功进入 synthesizing 并提交音频块；
   - 推进 `voicestudio` 到 generation 4，确认旧代 `voicestudio` 音频块失败且不消费 ID；
   - 使用该未消费 ID 在 `edge_tts` 合法后续音频块中成功复用，证明 generation fence 按 Provider 隔离；
   - 再用同一已提交 ID 向 generation 4 request 写入生命周期事件，确认 lifecycle registry 全局重复 ID 冲突且不创建 snapshot；
   - 使用新 ID 提交 generation 4 request，确认仅目标 request 更新，两个既有 Provider projection 保持 identity。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch23 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增校验优先级、失败 event ID 可复用、跨 Provider generation fence 隔离和全局 ID 冲突专项回归；
  - 文件保持 UTF-8 无 BOM、CRLF 换行；
  - 文件末尾保留标准 `unittest.main()` 入口。
- `implementation_plan/checkpoints/2026-09-22-P7-batch24-provider-request-validation-priority-cross-provider-generation-isolation-handoff.md`
  - 新建本批次交接文档；
  - 未修改既有执行路线图和历史 handoff。

## 4. 测试与验收结果

工作目录：`<PROJECT_ROOT>`

运行时：机器上可正常启动的 Python 3.12.10，通过 `py -3.12` 执行。

### 4.1 Batch24 专项测试

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_validation_priority_is_fail_closed_and_rejected_ids_remain_reusable tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_provider_generation_fences_are_isolated_and_failed_audio_ids_reuse_globally -v
```

结果：2/2 通过。

### 4.2 P7/TTS 专项回归

```powershell
py -3.12 -m unittest discover -s tests/tts -p 'test_*.py'
```

结果：100 项通过。

### 4.3 全量 unittest

```powershell
py -3.12 -m unittest discover -s tests -p 'test_*.py'
```

结果：445 项通过。

### 4.4 编译检查

```powershell
py -3.12 -m compileall -q app tests
```

结果：通过，`COMPILEALL_OK`。

### 4.5 编码与换行检查

目标文件 `tests/tts/test_provider_request_lifecycle.py`：

- 严格 UTF-8 解码：通过；
- BOM：无；
- 裸 LF：0；
- CRLF：完整保持。

### 4.6 执行面审计

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
- 未修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 未进入 P8 语音闭环；
- 未执行 Git add、commit 或 push。

## 5. 环境说明

项目现有 `.venv\Scripts\python.exe` 的 `pyvenv.cfg` 指向不可启动的历史系统 Python 路径；项目内嵌运行时因 `python312._pth` 隔离配置不会自动导入项目根目录，直接用 unittest discovery 也无法解析 `app`。

因此本批次继续使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 ...
```

未修改 `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 6. 已知限制

- 公开 API 仍是进程内内存 registry，不提供跨进程、持久化或并发线程安全保证；
- generation fence 按 `provider_name` 共享，推进后既有旧代快照仍可读取，但旧代新增事件和音频块会被拒绝；
- event ID 在单个 lifecycle registry 内跨 Provider、跨 request、跨生命周期事件和音频块全局唯一；
- 失败写入不消费 event ID；该 ID 后续可由任何满足绑定、generation、序列与状态约束的合法写入使用；
- event ID 重复检查当前先于 request 查找、绑定关系和 generation 校验，这是已验证且保留的 fail-closed 顺序；
- 绑定字段校验早于 generation fence，generation fence 早于事件序列和状态推进；
- 成功提交只替换目标 request 快照，其他 request 的 snapshot、events 与 audio_chunks projection identity 保持不变；
- `active_generation(...)` 只观察 fence，不创建 request snapshot，也不自动建立或推进 fence；
- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- 本批次仍未承诺并发线程安全，原子性是同步失败路径原子性；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 7. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 25：Provider request lifecycle 终态写入、generation transition 与跨 Provider 公开 projection identity 组合矩阵回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch24 已验证的校验优先级、失败 ID 可复用、跨 Provider generation fence 隔离及全局 event ID 边界为基础，审计终态成功/失败/取消写入与 generation transition 交错时，首个终态事实、公开 snapshot、events、audio_chunks identity 和不同 Provider fence 观察是否保持稳定；
4. 如确有缺口，只补充最小专项测试或最小产品契约，不得扩大到真实 Provider、网络或播放设备；
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

## 8. 新会话直接续接指令（替换旧交接文档中的续接段落）

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch24-provider-request-validation-priority-cross-provider-generation-isolation-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```