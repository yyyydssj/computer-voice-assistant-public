# 2026-09-22 P7 批次 12：Provider request 公开读取、快照/历史一致性与 generation activation 原子性复核交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch11-provider-request-cross-request-event-id-generation-read-boundary-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在不改变 batch11 已冻结的公开读取懒创建语义和 generation fence 语义的前提下，复核并锁定：

1. `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 对已知/未知/非法 request 的一致性；
2. 公开读取返回的不可变快照、事件历史和音频块历史在成功写入前后的一致性；
3. `activate_generation(...)` / `advance_generation(...)` 的非法输入、回退、重复激活和失败后状态原子性；
4. generation fence 按 `provider_name` 隔离；
5. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`app/tts/lifecycle/__init__.py` 的现有实现已满足本批次契约：

- 公开读取先 lookup request；未知 request 统一转为 `ProviderRequestLifecycleError`，不会创建快照；
- 已知 request 的首次 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 允许懒创建 `PREPARED` 快照；
- `events_for(...)` 和 `audio_chunks_for(...)` 始终从当前不可变快照投影；
- 成功事件/音频块写入采用新快照替换，旧快照引用保持历史值不变；
- `activate_generation(...)` 先完成 provider name、正整数 generation 和回退校验，再写入 `_active_generations`；
- 与当前 generation 相同的重复激活保持幂等成功；
- `advance_generation(...)` 复用同一校验和写入路径；
- generation fence 按 provider name 存储并相互隔离。

本批次无需修改产品实现文件；变更仅为专项测试补充和本交接文档。

### 2.2 新增专项测试

在 `tests/tts/test_provider_request_lifecycle.py` 新增 5 项覆盖：

- `test_public_read_views_are_consistent_and_unknown_reads_do_not_mutate`
  - 未知 request 的三个公开读取入口统一拒绝；
  - 已知 request 的读取投影与懒创建快照一致；
  - 事件/音频投影与当前快照保持一致。
- `test_public_reads_reject_malformed_request_ids_without_snapshot_creation`
  - 空字符串、含空格字符串、`None`、`True` 等非法 request ID 在三个公开读取入口均拒绝；
  - `_snapshots` 与 `_event_ids` 不发生变化。
- `test_successful_writes_preserve_old_snapshot_and_keep_public_projections_aligned`
  - 成功写入后旧 `PREPARED` 快照保持不变；
  - 新快照、事件历史、音频块历史保持一致。
- `test_generation_activation_validation_and_repeated_activation_are_atomic`
  - 非法 provider/generation 输入拒绝且不创建 fence；
  - 同代激活幂等成功；
  - 回退拒绝后当前 generation 保持不变；
  - `advance_generation(...)` 与 `activate_generation(...)` 共享语义。
- `test_generation_fences_are_isolated_per_provider`
  - 不同 provider 的 generation fence 独立推进、独立回退校验。

专项测试总数由 32 项增至 37 项。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增上述 5 项公开读取、快照历史和 generation activation 原子性测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch12-provider-request-public-read-generation-atomicity-handoff.md`
  - 本交接文档；未修改 batch11 或更早批次交接文档。

未修改：

- `app/tts/lifecycle/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- 既有批次 handoff。

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行。

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**37/37 通过**。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**73/73 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**418/418 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 检查：

- `app/tts/lifecycle/__init__.py` 与 `tests/tts/test_provider_request_lifecycle.py` 均按 UTF-8 严格读取成功；
- 本交接文档按 UTF-8 严格写入；
- `U+FFFD` 替换字符数量：**0**。

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

- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- `_snapshot_for(...)` 是注册表内部只读构造辅助，不改变公开 `get(...)` 的懒创建 API 语义；
- 本批次原子性仍是同步失败路径原子性，不承诺并发线程安全；
- `activate_generation(...)` 允许对格式合法但尚未注册的 provider name 建立 fence；本批次未改变也未扩大该语义；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 13：Provider request lifecycle 终态快照、事件/音频投影不变量与拒绝路径可观察一致性复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 复核终态 `COMPLETED` / `FAILED` / `CANCELLED` 的 `state`、末事件、`terminal_reason_code`、`audio_produced`、`audio_enqueued` 与音频块投影一致性；
4. 复核拒绝路径对 `get(...)`、`events_for(...)`、`audio_chunks_for(...)`、全局 event ID 和 active generation 的可观察不变性；
5. 必要时补充最小专项测试，但不得扩大到真实 Provider 或网络；
6. 完成后运行 P7/TTS 专项、全量 unittest、compileall、严格 UTF-8 和执行面审计；
7. 完成后新建下一份 batch handoff，再停止。

不得提前：

- 接入 VoiceStudio、Edge TTS、真实 Provider、网络或真实音频流；
- 调用 `synthesize`、`cancel`、播放设备、扬声器或真实音频输出；
- 把本地音频块元数据伪装成远端停止、播放完成或用户已听见；
- 实现播放队列、PCM 时长预算、Provider fallback、冷却、恢复探测或重试；
- 实现播放期间监听、AEC、全双工、实时打断、连续监听、唤醒词、声纹或语音 L3；
- 修改 P5 授权、执行、验证、审计、恢复和重试语义；
- 进入 P8 语音闭环。