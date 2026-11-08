# 2026-09-22 P7 批次 23：Provider request 跨 request 成功提交、共享 event ID 与 generation fence 组合矩阵回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch22-provider-request-public-read-write-commit-empty-tuple-unknown-matrix-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch22 已验证未知 request 全读写 fail-closed、空 tuple identity 提交边界、成功/失败写入原子性、公开对象 identity 和 generation 观察无副作用的基础上，完成 P7 Provider request lifecycle 的下一轮组合矩阵回归：

1. 确认多个同 Provider、同 generation request 的成功提交彼此隔离；
2. 确认 generation fence 推进后，旧代失败写入不会替换任何已提交 request 快照或 projection；
3. 确认全局共享 event ID 冲突在跨 request、生命周期事件与音频块之间均 fail closed；
4. 确认旧代失败写入未消费的 event ID 可由当前 generation request 成功复用；
5. 确认跨 request 的成功提交、失败矩阵和 generation 观察组合后，公开 snapshot、events、audio_chunks identity 仍保持一致；
6. 不扩大到真实 Provider、网络、播放设备或 P8。

## 2. 审查结论与最小变更

### 2.1 产品实现结论

`ProviderRequestLifecycleRegistry` 现有实现满足 batch23 的组合边界要求，无需修改产品代码：

- `_snapshots` 以 `provider_request_id` 隔离，每次成功提交只替换目标 request 的不可变快照；
- `_event_ids` 是 lifecycle registry 范围内的全局集合，生命周期事件与音频块共享同一 ID 冲突边界；
- `record(...)` 和 `record_audio_chunk(...)` 仅在全部绑定、generation、序列、状态与快照构造校验通过后提交 snapshot 和 event ID；
- `advance_generation(...)` 只推进指定 Provider 的 fence，不重写既有 request 快照；
- 旧代写入因 generation fence 失败时不会消费 event ID，因此该 ID 可由当前 generation 的合法 request 成功提交；
- 共享 event ID 冲突优先 fail closed，且不会为尚无快照的 request 惰性创建 snapshot；
- 成功写入只替换目标 request 快照，其他 request 的公开 snapshot 与 tuple projection identity 保持不变。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项 batch23 专项回归测试；未修改产品实现：

1. `test_cross_request_success_commits_survive_generation_and_shared_id_failures`（第 2137 行）
   - 在 generation 3 下分别成功提交两个 request；
   - 推进同 Provider fence 到 generation 4；
   - 使用第一个 request 已提交的 event ID 写入 generation 4 request，确认全局重复 ID 拒绝且不会创建该 request 快照；
   - 对第二个 generation 3 request 提交旧代事件，确认 generation 拒绝不改变两个既有 request 的 snapshot、events、audio_chunks identity 或全局 ID 集合；
   - 将旧代失败未消费的 event ID 用于 generation 4 request，确认可成功提交且旧 request 投影保持原 identity。
2. `test_stale_audio_id_reuse_and_cross_kind_collision_preserve_request_projections`（第 2193 行）
   - generation 3 request 成功进入 synthesizing 并提交一个音频块；
   - 推进 fence 到 generation 4 后提交旧代后续音频块，确认失败不替换旧 request 快照、不漂移 events/audio_chunks tuple identity，也不消费 ID；
   - 将该未消费 ID 复用于 generation 4 request 的生命周期事件，确认跨 kind 合法复用可成功提交；
   - 再以相同已提交事件 ID 写入音频块，确认跨 kind 全局 ID 冲突 fail closed；
   - 最后以新 ID 成功提交 generation 4 音频块，确认仅目标 request 更新，旧代 request 仍保持原快照与投影 identity。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch22 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增跨 request 成功提交、共享 event ID 与 generation fence 组合矩阵回归测试；
  - 统一该变更文件为 UTF-8 无 BOM、CRLF 换行，未改变既有测试语义。
- `implementation_plan/checkpoints/2026-09-22-P7-batch23-provider-request-cross-request-shared-event-id-generation-fence-matrix-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`
- `app/tts/requests/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

### 4.1 batch23 新增专项

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_cross_request_success_commits_survive_generation_and_shared_id_failures tests.tts.test_provider_request_lifecycle.ProviderRequestLifecycleTests.test_stale_audio_id_reuse_and_cross_kind_collision_preserve_request_projections -v
```

- batch23 新增专项：**2/2 通过**。

### 4.2 Provider request lifecycle 专项

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**62/62 通过**。
- batch22 基线为 60/60，本批次新增 2 项测试。

### 4.3 P7/TTS 专项

```powershell
py -3.12 -m unittest discover -s tests/tts -p "test_*.py"
```

- P7/TTS 专项：**98/98 通过**。
- batch22 基线为 96/96，本批次新增 2 项测试。

### 4.4 全量 unittest

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**443/443 通过**。
- batch22 基线为 441/441，本批次新增 2 项测试。

### 4.5 编译检查

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。
- 换行规范化后对变更文件重新执行 `py_compile`：通过。

### 4.6 严格 UTF-8 与变更文件质量

- 全仓 Python 文件严格 UTF-8 解码：**133 个文件均可严格读取**；
- 全仓未发现 U+FFFD 替换字符；
- 变更文件 `tests/tts/test_provider_request_lifecycle.py` 严格 UTF-8 读取成功；
- 变更文件无 BOM、统一 CRLF，且以换行符结束；
- 两个新增测试方法各出现 1 次；
- 全仓既存限制：3 个既有 Python 文件包含 UTF-8 BOM：
  - `tests/recovery/test_recovery_scanner.py`；
  - `tests/security/test_audit_persistence.py`；
  - `tests/tts/test_capabilities.py`；
- 未修改上述既存 BOM 文件。

### 4.7 执行面 AST 审计

针对本批次变更文件及相关产品文件执行 AST 审计：

- `tests/tts/test_provider_request_lifecycle.py`；
- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`。

结果：

- 3 个目标文件均可解析；
- 未发现 `open`、`exec`、`eval`、`compile`、`__import__` 调用；
- 未发现 `subprocess`、`socket`、`requests`、`httpx`、`urllib` 或 `http` 导入；
- 未引入网络、进程、文件副作用、真实设备或后台线程入口。

## 5. 执行面审计

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

## 6. 环境说明

项目现有 `.venv\Scripts\python.exe` 的 `pyvenv.cfg` 指向不可启动的历史系统 Python 路径；项目内嵌运行时因 `python312._pth` 隔离配置不会自动导入项目根目录，直接用 unittest discovery 也无法解析 `app`。

因此本批次继续使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 ...
```

未修改 `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 7. 已知限制

- 公开 API 仍是进程内内存 registry，不提供跨进程、持久化或并发线程安全保证；
- generation fence 按 `provider_name` 共享，推进后既有旧代快照仍可读取，但旧代新增事件和音频块会被拒绝；
- event ID 在单个 lifecycle registry 内跨 request、跨生命周期事件和音频块全局唯一；
- 失败写入不消费 event ID；该 ID 后续可由任何满足绑定、generation、序列与状态约束的合法写入使用；
- event ID 重复检查当前先于 request 查找、绑定关系和 generation 校验，这是已验证且保留的 fail-closed 顺序；
- 成功提交只替换目标 request 快照，其他 request 的 snapshot、events 与 audio_chunks projection identity 保持不变；
- `active_generation(...)` 只观察 fence，不创建 request snapshot，也不自动建立或推进 fence；
- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- 本批次仍未承诺并发线程安全，原子性是同步失败路径原子性；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 24：Provider request lifecycle 校验优先级、失败 ID 可复用与跨 Provider generation 隔离组合矩阵回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch23 已验证的跨 request 成功提交隔离、共享 event ID、旧代失败 ID 可复用及 generation fence 组合边界为基础，审计重复 event ID、未知 request、绑定错误、generation 错误、序列/状态错误之间的校验优先级，以及不同 Provider fence 的隔离组合；
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

## 9. 新会话直接续接指令（替换旧交接文档中的续接段落）

```text
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch23-provider-request-cross-request-shared-event-id-generation-fence-matrix-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
