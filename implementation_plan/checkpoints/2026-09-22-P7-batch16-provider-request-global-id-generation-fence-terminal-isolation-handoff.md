# 2026-09-22 P7 批次 16：Provider request 全局 event ID 提交顺序、generation fence 观察与终态跨 request 边界复核交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch15-provider-request-commit-boundary-cross-request-projection-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在不改变 batch15 已冻结的注册表提交边界、保存快照 identity、跨 request 公开投影隔离和共享 event ID 语义的前提下，复核并锁定：

1. 成功事件写入、成功音频块写入和跨 request 写入继续共享同一个全局 event ID 集合；
2. generation fence 推进后，旧代 request 的事件被拒绝，拒绝不创建新快照、不消费候选 event ID，也不污染其他 request 的公开观察；
3. generation fence 推进后的新代 request 可以复用此前被旧代拒绝、尚未提交的候选 event ID；
4. `FAILED`、`CANCELLED`、`COMPLETED` 三类终态在不同 request 之间保持快照、事件历史和音频块历史隔离；
5. 一个 request 的终态锁定不会阻止另一个 request 使用新的 event ID 成功写入；终态 request 的迟到写入失败仍保持全局 ID 集合不变；
6. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`ProviderRequestLifecycleRegistry` 当前已经满足本批次所需实现语义，无需修改产品代码：

- `record(...)` 和 `record_audio_chunk(...)` 都先完成 request 绑定、generation fence、序号/状态和快照构造校验，再写入 `_snapshots` 和 `_event_ids`；
- `_event_ids` 是注册表级共享集合，因此事件 ID 与音频块 ID 跨 request、跨类型统一去重；
- `_assert_generation_current(...)` 在当前 fence 下拒绝旧代和未来代事件，`activate_generation(...)`/`advance_generation(...)` 只允许 fence 单调前进；
- 失败路径不会提交新快照，也不会消费候选 event ID；
- 终态快照通过不可变 `ProviderRequestLifecycle` 保存，终态之后的迟到事件和音频块继续被拒绝；
- 不同 request 的快照和嵌套 tuple 投影保持独立，终态 request 不会改变其他 request 的生命周期观察。

### 2.2 最小变更

仅在 `tests/tts/test_provider_request_lifecycle.py` 新增 2 项专项测试，未修改 `app/tts/lifecycle/__init__.py`：

1. `test_generation_fence_observations_are_cross_request_and_rejections_are_reusable`
   - 激活 generation 3 后写入第一个 request；
   - 推进同一 provider 的 generation fence 到 4；
   - 第二个 generation 3 request 的写入被认定为旧代并拒绝；
   - 拒绝不创建第二个 request 快照、不改变第一个 request 的快照、不消费候选 event ID；
   - generation 4 request 随后成功复用该候选 event ID，证明拒绝不会错误污染全局 ID 集合；
   - 当前 generation 观察保持为 4。
2. `test_terminal_states_are_isolated_across_requests_and_share_commit_boundary`
   - 第一个 request 完整走到 `COMPLETED` 并保留音频块投影；
   - 第二个 request 走到 `FAILED`；第三个 request 走到 `CANCELLED`；
   - 三个终态 request 的 state、事件历史、音频块历史和 `get(...)` identity 相互隔离；
   - 已完成 request 的迟到写入被拒绝，不消费 `terminal-reusable`；
   - 第四个 request 随后成功使用 `terminal-reusable`，证明终态拒绝保持共享提交边界原子性。

没有扩大范围，也未修改：

- `app/tts/lifecycle/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch15 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增 generation fence 跨 request 可观察性、旧代拒绝后候选 ID 可复用、三类终态跨 request 隔离和终态提交边界测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch16-provider-request-global-id-generation-fence-terminal-isolation-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**48/48 通过**。
- 其中 batch15 基线 46 项保持通过，本批次新增 2 项均通过。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**84/84 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**429/429 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 与变更文件质量检查：

- `app/tts/lifecycle/__init__.py` 按 UTF-8 严格读取成功；
- `tests/tts/test_provider_request_lifecycle.py` 按 UTF-8 严格读取成功；
- 本 handoff 按 UTF-8 严格写入并复读成功；
- 上述变更相关文件的 `U+FFFD` 替换字符数量：**0**；
- 上述变更相关文件未发现尾随空白或异常控制字符；
- 对历史 batch6–batch15 handoff 做的扩展扫描发现 batch6–batch8 原有文档存在尾随空白；该历史编码/格式问题未由本批次引入，未修改或扩大处理范围。

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

**P7 批次 17：Provider request lifecycle 事件/音频块跨 provider generation 与公开终态投影的最终边界复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch16 已验证的全局 event ID 提交顺序和 generation fence 观察为基础，检查不同 provider name 的 fence 隔离、同一 provider 的代际切换，以及事件/音频块混合写入的公开终态一致性；
4. 复核 `COMPLETED`、`FAILED`、`CANCELLED` 终态在 generation 推进前后仍保持首次终态事实、历史 tuple 和跨 request identity 隔离；
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