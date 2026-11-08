# 2026-09-22 P7 批次 13：Provider request 终态快照、事件/音频投影不变量与拒绝路径可观察一致性复核交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch12-provider-request-public-read-generation-atomicity-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在不改变 batch12 已冻结的公开读取、不可变快照、事件/音频历史和 generation fence 语义的前提下，复核并锁定：

1. `COMPLETED` / `FAILED` / `CANCELLED` 终态快照的 `state`、末事件、`terminal_reason_code`、`audio_produced`、`audio_enqueued` 与音频块投影一致性；
2. 完成态额外要求已有音频块和 `audio_produced` 事实；
3. 非终态快照不得携带终态事件历史；
4. 终态读取投影与注册表保存快照、事件历史、音频块历史一致；
5. 终态之后的迟到事件/音频块、generation 回退和拒绝路径不消费 event ID、不改变快照或 active generation 观察结果；
6. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

现有注册表写入路径已经在构造 next snapshot 前校验终态事件顺序、音频块门控和 generation fence，但 `ProviderRequestLifecycle.__post_init__` 对直接构造/`dataclasses.replace(...)` 的终态快照约束不完整：

- 原先 `COMPLETED` 状态只间接依赖 `audio_produced` 与 `audio_chunks` 约束；直接改写 `audio_produced=False` 可能绕过完成态专属不变量；
- 原先非终态 `state` 搭配含终态事件的历史未被统一拒绝；
- 这会削弱不可变快照作为公开投影时的自洽性检查。

### 2.2 最小实现修复

在 `app/tts/lifecycle/__init__.py` 的 `ProviderRequestLifecycle.__post_init__` 增加：

1. `state == COMPLETED` 时必须有至少一个音频块；
2. `state == COMPLETED` 时必须已经有 `audio_produced=True`；
3. `state` 非终态时，历史中不得出现 `COMPLETED`、`FAILED` 或 `CANCELLED` 终态事件；
4. 保留已有终态末事件与 `state` 一致、终态事件只能在历史末尾、`audio_enqueued` 依赖 `audio_produced` 等检查。

没有改变：

- `ProviderRequestLifecycleRegistry` 的公开读取懒创建语义；
- event/audio chunk 的全局 event ID 去重；
- generation fence 的 provider 隔离、单调推进和拒绝顺序；
- 失败/取消可在无音频块时终止，或在已有局部音频事实后保留事实；
- 本地音频块不等同于播放开始、播放完成或用户听见。

## 3. 新增专项测试

在 `tests/tts/test_provider_request_lifecycle.py` 新增 4 项覆盖：

- `test_completed_terminal_projection_exposes_consistent_state_and_audio_facts`
  - 完成态的末事件、state、`terminal_reason_code`、音频标志、事件序列和公开投影一致。
- `test_terminal_projection_rejects_missing_completed_audio_and_nonterminal_terminal_history`
  - `COMPLETED` 快照缺失 `audio_produced` 或音频块时拒绝；
  - 非终态携带终态历史时拒绝；
  - 所有拒绝不污染注册表当前快照和公开投影。
- `test_failed_and_cancelled_terminal_projections_preserve_reason_and_audio_flags`
  - 失败/取消终态保留原因码、局部音频块、`audio_produced` 与 `audio_enqueued` 事实；
  - 不伪造 `COMPLETED` 事件。
- `test_terminal_rejections_do_not_consume_ids_or_change_generation_observations`
  - 终态迟到 accepted/audio chunk 和 generation 回退均拒绝；
  - snapshot、事件历史、音频历史、全局 event ID 与 active generation 保持不变。

同时调整既有：

- `test_terminal_projection_rejects_inconsistent_state_rewrite_atomically`
  - 使用带局部音频事实的失败终态验证 `state` 改写为 `COMPLETED` 仍按末事件不一致拒绝，且原快照历史保持不变。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
  - 补充 `ProviderRequestLifecycle.__post_init__` 的完成态与非终态历史不变量。
- `tests/tts/test_provider_request_lifecycle.py`
  - 新增终态投影、原因码/音频标志、拒绝路径可观察一致性专项测试；
  - 调整终态改写测试以覆盖更完整的终态事实。
- `implementation_plan/checkpoints/2026-09-22-P7-batch13-provider-request-terminal-projection-observability-handoff.md`
  - 本交接文档；未修改 batch12 或更早批次交接文档。

未修改：

- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- 既有批次 handoff；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 5. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行。

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**41/41 通过**。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**77/77 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**422/422 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 检查：

- `app/tts/lifecycle/__init__.py`、`tests/tts/test_provider_request_lifecycle.py` 与本交接文档均按 UTF-8 严格读取成功；
- `U+FFFD` 替换字符数量：**0**；
- `git diff --check`：通过。

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
- 未修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 未进入 P8 语音闭环；
- 未执行 Git add、commit 或 push。

## 7. 环境说明

项目现有 `.venv\Scripts\python.exe` 的 `pyvenv.cfg` 指向不可启动的历史系统 Python 路径；项目内嵌运行时因 `python312._pth` 隔离配置不会自动导入项目根目录，直接用 unittest discovery 也无法解析 `app`。

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
- 本批次原子性仍是同步失败路径原子性，不承诺并发线程安全；
- `activate_generation(...)` 允许对格式合法但尚未注册的 provider name 建立 fence；本批次未改变也未扩大该语义；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 9. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 14：Provider request lifecycle 事件序列、快照字段绑定与公开投影不可变性复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 复核事件历史 `event_sequence` 连续性与 event/audio chunk ID 的跨类型投影边界；
4. 复核每个事件及音频块与 request 的完整字段绑定在快照、历史和公开读取中的一致性；
5. 复核 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 返回对象及其嵌套 tuple 的不可变观察语义；
6. 必要时补充最小专项测试，但不得扩大到真实 Provider 或网络；
7. 完成后运行 P7/TTS 专项、全量 unittest、compileall、严格 UTF-8 和执行面审计；
8. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio、Edge TTS、真实 Provider、网络或真实音频流；
- 调用 `synthesize`、`cancel`、播放设备、扬声器或真实音频输出；
- 把本地音频块元数据伪装成远端停止、播放完成或用户已听见；
- 实现播放队列、PCM 时长预算、Provider fallback、冷却、恢复探测或重试；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。