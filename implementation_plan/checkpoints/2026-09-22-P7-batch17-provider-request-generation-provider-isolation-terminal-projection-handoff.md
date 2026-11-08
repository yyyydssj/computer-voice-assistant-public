# 2026-09-22 P7 批次 17：Provider request generation/provider 隔离与终态公开投影边界复核交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch16-provider-request-global-id-generation-fence-terminal-isolation-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch16 已验证全局 event ID 提交边界和 generation fence 跨 request 观察的基础上，最终复核：

1. 不同 provider name 的 generation fence 独立推进、独立观察和独立回退拒绝；
2. 同一 provider 从 generation 3 推进到 generation 4 后，旧代 request 的事件/音频块写入被拒绝，且拒绝不改变快照、公开投影、事件 ID 集合或 generation 观察；
3. 另一 provider 在自身 generation fence 不变时，仍可完成事件/音频块混合写入并形成一致的公开终态投影；
4. 同一 provider 的 generation 4 新 request 可以在代际切换后成功提交；
5. `COMPLETED`、`FAILED`、`CANCELLED` 终态在 generation 推进前后保持首次终态事实、历史 tuple、音频块 tuple 和跨 request identity 隔离；
6. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`ProviderRequestLifecycleRegistry` 当前实现已经满足本批次所需产品语义，无需修改产品代码：

- `_active_generations` 按 `provider_name` 保存独立 generation fence；`activate_generation(...)`/`advance_generation(...)` 对每个 provider 独立执行单调推进；
- `record(...)` 和 `record_audio_chunk(...)` 都在写入 `_snapshots`、`_event_ids` 前完成 request identity、provider generation、序号/状态和公开快照构造校验；
- generation 3 的旧代写入在 fence 推进到 4 后被拒绝；拒绝不提交新快照，也不消费候选 event ID；
- 不同 provider 的事件与音频块可以在各自 fence 下交错写入，不会互相改变 generation 观察；
- 不可变 `ProviderRequestLifecycle` 快照把首次终态、事件历史和音频块历史锁定在单一 request 内，终态 request 的迟到写入不会污染其他 request。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项专项测试并扩展测试 fixture：

1. `test_generation_fences_are_provider_scoped_and_mixed_history_stays_consistent`
   - 注册 `voicestudio` generation 3 和 `edge_tts` generation 7；
   - 在两个 provider request 之间交错提交 accepted、synthesis_started 和 audio chunk；
   - 推进 `voicestudio` 到 generation 4 后，旧代 `audio_produced` 被拒绝且快照、事件 ID 集合保持不变；
   - `edge_tts` generation 7 request 继续完成 audio_produced、audio_enqueued、completed；
   - generation 4 的新 `voicestudio` request 成功 accepted，证明代际切换后的公开观察和提交边界一致。
2. `test_terminal_projections_keep_first_facts_across_generation_transition`
   - generation 3 request 完整走到 `COMPLETED` 并保留音频块投影；
   - 推进同一 provider 到 generation 4 后，旧 request 的迟到音频块和事件均被拒绝，不消费候选 ID；
   - generation 4 下分别创建 `FAILED` 和 `CANCELLED` request；
   - 三类终态的 state、terminal reason、事件历史、音频块历史和 `get(...)` identity 保持隔离。

测试 fixture 同时注册现有合法的 `edge_tts` Provider capability 和 VoiceProfile，仅用于本地 fence 隔离测试。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch16 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 扩展 `setUp()` fixture，增加合法 `edge_tts` capability/profile；
  - 新增 provider-scoped generation fence、混合事件/音频块写入和代际切换后终态公开投影专项测试；
  - 新增对应的 request/audio chunk 测试辅助构造函数。
- `implementation_plan/checkpoints/2026-09-22-P7-batch17-provider-request-generation-provider-isolation-terminal-projection-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**50/50 通过**。
- batch16 基线 48 项保持通过，本批次新增 2 项均通过。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**86/86 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**431/431 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 与变更文件质量检查：

- `app/tts/lifecycle/__init__.py` 按 UTF-8 严格读取成功；
- `tests/tts/test_provider_request_lifecycle.py` 按 UTF-8 严格读取成功；
- 上述变更相关 Python 文件的 `U+FFFD` 替换字符数量：**0**；
- 上述变更相关 Python 文件未发现尾随空白或异常控制字符。

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
- `activate_generation(...)` 允许对格式合法但尚未注册的 provider name 建立 fence；本批次仅复核现有语义，未改变也未扩大该语义；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 18：Provider request lifecycle 多 request 交错写入下的公开终态/投影一致性与失败路径最终回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch17 已验证的 provider-scoped generation fence、同 provider 代际切换和三类终态隔离为基础，检查多 request 交错提交时事件序列、音频块序列、公开 state/flags、终态 reason 和 identity 的一致性；
4. 重点复核失败/取消发生在 `PREPARED`、`ACCEPTED`、`SYNTHESIZING`、已产生音频和已入队音频等阶段时，其他 request 的公开投影与全局 event ID 集合不被污染；
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