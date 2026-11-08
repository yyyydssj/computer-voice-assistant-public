# 2026-09-22 P7 批次 22：Provider request 公开读取与显式写入提交边界、空 tuple identity 及未知对象状态矩阵回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch21-provider-request-public-read-lazy-snapshot-unknown-boundary-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch21 已验证未知 request 公开读取 fail-closed、未知对象不会惰性创建 snapshot、合法 request 允许惰性 `PREPARED` snapshot，以及 malformed provider name 读取无副作用的基础上，完成 P7 Provider request lifecycle 公开读取与显式写入提交边界的下一轮回归：

1. 确认公开读取和显式写入共享同一未知 `provider_request_id` fail-closed 边界；
2. 确认失败写入不会替换已有快照、消费 event ID、改变 generation fence 或破坏既有公开 tuple/object identity；
3. 确认空 `events`/`audio_chunks` tuple 的 identity 只在成功写入后随新快照提交，不因失败写入产生副本或漂移；
4. 确认成功写入会创建新的生命周期快照，但保留未变化投影的 tuple identity；
5. 复核合法 request、未知 request、空投影和失败写入之间的状态矩阵，不扩大到真实 Provider、网络、播放设备或 P8。

## 2. 审查结论与最小变更

### 2.1 产品实现结论

`ProviderRequestLifecycleRegistry` 现有实现满足 batch22 的提交边界要求，无需修改产品代码：

- `record(...)` 和 `record_audio_chunk(...)` 都先完成 request 查找、绑定关系、generation fence、序列/状态及快照构造校验，再写入 `_snapshots` 和 `_event_ids`；
- `_record_simple(...)` 在构造事件前通过 `_get_request(...)` 查找 request，未知 request 不会进入 snapshot 创建或 event ID 登记；
- `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 对合法 request 的首次读取仍惰性创建 `PREPARED` snapshot，这是既定公开语义；
- 成功追加生命周期事件时，新的 snapshot 会复用未变化的 `audio_chunks` tuple；成功追加音频块时，新的 snapshot 会复用未变化的 `events` tuple；
- 失败写入在任何状态提交前抛出，既有 snapshot、公开 tuple identity、event ID 集合和 generation fence 保持不变。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项 batch22 专项回归测试；未修改产品实现：

1. `test_empty_projection_identity_tracks_only_successful_write_commits`（第 1962 行）
   - 首次公开读取合法 request，确认 `PREPARED` 快照的空 `events`/`audio_chunks` tuple identity；
   - 提交一个绑定关系错误的事件，确认失败不会替换快照、漂移空 tuple 或消费 event ID；
   - 成功 `accept` 后确认新快照替换成功，但空 `audio_chunks` tuple identity 被保留；
   - 进入 `SYNTHESIZING` 后提交非法音频块，确认失败不改变空音频 tuple identity；
   - 成功记录首个音频块后确认新快照替换、事件 tuple identity 保留、旧快照仍不可变。
2. `test_unknown_request_read_write_matrix_preserves_existing_commit_state`（第 2028 行）
   - 先建立已知 request 的 `PREPARED` 快照及公开 tuple identity；
   - 依次覆盖未知 request 的 `get(...)`、`events_for(...)`、`audio_chunks_for(...)`、`accept(...)`、`synthesis_started(...)`、`audio_produced(...)`、`audio_enqueued(...)`、`complete(...)`、`fail(...)`、`cancel(...)`、`record(...)` 和 `record_audio_chunk(...)`；
   - 确认所有入口均以“未知 provider_request_id” fail closed；
   - 每次失败后确认 `_snapshots`、`_event_ids`、`_active_generations`、已知 request 快照及其公开 tuple identity 均保持原值；
   - 最后以一个已知 request 的成功 `accept` 验证失败矩阵不会污染后续合法提交，也不会错误占用未知操作使用过的 event ID。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch21 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增空 projection identity 与成功/失败写入提交边界回归测试；
  - 新增未知 request 公开读取/显式写入完整状态矩阵回归测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch22-provider-request-public-read-write-commit-empty-tuple-unknown-matrix-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`
- `app/tts/requests/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

### 4.1 Provider request lifecycle 专项

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**60/60 通过**。
- batch21 基线为 58/58，本批次新增 2 项测试。

### 4.2 P7/TTS 专项

```powershell
py -3.12 -m unittest discover -s tests/tts -p "test_*.py"
```

- P7/TTS 专项：**96/96 通过**。
- batch21 基线为 94/94，本批次新增 2 项测试。

### 4.3 全量 unittest

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**441/441 通过**。
- batch21 基线为 439/439，本批次新增 2 项测试。

### 4.4 编译检查

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

### 4.5 严格 UTF-8 与变更文件质量

- 全仓 Python 文件严格 UTF-8 解码：**133 个文件均可严格读取**；
- 全仓未发现 U+FFFD 替换字符；
- 变更文件 `tests/tts/test_provider_request_lifecycle.py` 严格 UTF-8 读取成功；
- 变更文件无 BOM，且以换行符结束；
- 新增测试方法各出现 1 次；
- 全仓既存限制：3 个既有 Python 文件包含 UTF-8 BOM：
  - `tests/recovery/test_recovery_scanner.py`；
  - `tests/security/test_audit_persistence.py`；
  - `tests/tts/test_capabilities.py`；
- 全仓既存尾随空白数量较多，未属于本批次新增逻辑；本批次未扩散修改既有文件。

### 4.6 执行面 AST 审计

针对本批次变更文件及相关产品文件执行 AST 审计：

- `tests/tts/test_provider_request_lifecycle.py`；
- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`。

结果：

- 3 个目标文件均可解析；
- 未发现 `open`、`exec`、`eval`、`compile`、`subprocess`、socket/HTTP 客户端或命令执行入口；
- 未引入网络、进程、文件副作用、真实设备或后台线程入口。

全仓 AST 扫描未作为本批次通过项：被上述既存 BOM 文件中的 `U+FEFF` 首字符阻断；未修改这些既存文件。

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

- 公开只读 API 仍是进程内内存 registry，不提供跨进程、持久化或并发线程安全保证；
- `get(...)` 对合法 request 的首次读取仍允许惰性创建 `PREPARED` snapshot，这是已验证且保留的公开读取语义；
- 未知 request 的公开读取和显式写入均 fail closed，且不会创建未知对象的 snapshot；
- 合法 request 的成功写入才会替换对应 snapshot；未变化的 tuple projection 会按现有实现保留 identity；
- `active_generation(...)` 对格式合法但尚未注册的 provider name 返回 `None`，不会自动建立 fence；
- malformed provider name 读取被拒绝且无状态变化；
- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- 本批次仍未承诺并发线程安全，原子性是同步失败路径原子性；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 23：Provider request lifecycle 成功提交后的跨 request 隔离、共享 event ID 与 generation fence 组合矩阵回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch22 已验证的未知 request 全读写 fail-closed、空 tuple identity 提交边界、成功/失败写入原子性、公开对象 identity 和 generation 观察无副作用为基础，审计跨 request 成功提交后的 projection 隔离、共享 event ID 冲突、generation fence 变更与失败矩阵之间的组合边界；
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch22-provider-request-public-read-write-commit-empty-tuple-unknown-matrix-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话为新交接文档续接指令，以便于新会话直接拷贝续接任务，
```
