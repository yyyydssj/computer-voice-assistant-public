# 2026-09-22 P7 批次 15：Provider request lifecycle 提交边界、保存快照身份稳定性与跨 request 公开投影复核交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch14-provider-request-event-sequence-snapshot-binding-public-projection-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在不改变 batch14 已冻结的事件序列、快照字段绑定、事件/音频块共享 ID 边界和公开只读投影语义的前提下，复核并锁定：

1. 事件写入和音频块写入在所有校验失败路径上均先完成校验，再提交新快照和全局 event ID；
2. 被拒绝的写入不得创建新的 request 快照、污染旧快照、改变事件/音频历史或消费全局 event ID；
3. 成功写入返回的新快照与旧快照保持对象身份区分，旧快照及其嵌套 tuple 观察继续稳定；
4. 不同 request 的生命周期快照、事件历史、音频块历史和公开投影保持隔离；
5. 全局 event ID 集合继续跨 request、跨事件/音频块类型共享，任何交叉重复 ID 都按统一重复规则拒绝；
6. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`ProviderRequestLifecycleRegistry.record(...)` 与 `record_audio_chunk(...)` 已经在新快照构造和所有门控校验成功后，才写入 `_snapshots` 与 `_event_ids`。`ProviderRequestLifecycle` 使用 frozen/slotted dataclass，事件历史和音频块历史使用 tuple；成功写入会创建新快照，旧快照不会被就地改写。`get(...)`、`events_for(...)`、`audio_chunks_for(...)` 继续复用当前保存快照及其嵌套 tuple。

跨 request 的 `_event_ids` 是共享集合；公开读取和成功写入均按 request identity 分离快照，不会把一个 request 的事件或音频块投影到另一个 request。现有实现无需产品代码修改。

### 2.2 最小变更

仅在 `tests/tts/test_provider_request_lifecycle.py` 新增 2 项专项测试：

1. `test_cross_request_projection_identity_and_shared_ids_remain_isolated`
   - 第一个 request 成功写入后，第二个 request 使用相同 event ID 被拒绝；
   - 拒绝不创建第二个 request 的快照，不改变第一个 request 的快照 identity 或公开事件 tuple；
   - 第二个 request 成功写入后，事件/音频块历史与第一个 request 保持隔离；
   - 第二个 request 的音频块再次使用第一个 request 的 event ID 时被统一重复规则拒绝；
   - 交叉拒绝不改变第二个 request 当前快照及全局 ID 集合。
2. `test_rejected_writes_preserve_snapshot_projection_identity_and_global_ids`
   - 非法事件序列写入被拒绝后，`get(...)`、`events_for(...)`、`audio_chunks_for(...)` 仍返回同一旧快照及同一 tuple identity；
   - 非法音频块序号写入被拒绝后，当前快照和公开 tuple identity 继续稳定；
   - 两条拒绝路径均不消费候选 event ID。

未修改 `app/tts/lifecycle/__init__.py`；此前 batch14 的生命周期快照校验继续作为本批次提交边界的实现基础。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增跨 request 公开投影隔离、共享 event ID 交叉拒绝和拒绝写入 identity/global-ID 不变性测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch15-provider-request-commit-boundary-cross-request-projection-handoff.md`
  - 本交接文档；未修改 batch14 或更早批次交接文档。

未修改：

- `app/tts/lifecycle/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- 既有批次 handoff；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行。

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**46/46 通过**。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**82/82 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**427/427 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 检查：

- `tests/tts/test_provider_request_lifecycle.py` 与本 handoff 按 UTF-8 严格读取成功；
- `app/tts/lifecycle/__init__.py` 按 UTF-8 严格读取成功；
- 上述变更相关文件的 `U+FFFD` 替换字符数量：**0**；
- 变更文件未发现尾随空白或异常控制字符。

说明：一次覆盖整个历史 `app/tests/implementation_plan/checkpoints` 树的 PowerShell 严格读取扫描遇到仓库既有非 UTF-8 文件；该历史编码问题未由本批次引入，也未改写或扩大处理范围。本批次变更文件已单独完成严格 UTF-8 复核。

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

**P7 批次 16：Provider request lifecycle 全局 event ID 提交顺序、generation fence 观察与终态跨 request 边界复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 复核全局 event ID 在成功事件写入、成功音频块写入和跨 request 交叉写入中的提交顺序与拒绝顺序；
4. 复核 generation fence 推进、旧代/未来代拒绝与跨 request 公开观察之间的可观察一致性；
5. 复核不同 request 在 `FAILED`、`CANCELLED`、`COMPLETED` 终态下的事件/音频投影隔离、旧快照保留和共享 ID 集合不变性；
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