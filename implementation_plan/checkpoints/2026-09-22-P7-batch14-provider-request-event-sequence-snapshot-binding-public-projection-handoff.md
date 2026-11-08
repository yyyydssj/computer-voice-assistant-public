# 2026-09-22 P7 批次 14：Provider request 事件序列、快照字段绑定与公开投影不可变性复核交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch13-provider-request-terminal-projection-observability-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在不改变 batch13 已冻结的终态快照、事件/音频历史、generation fence 和公开读取语义的前提下，复核并锁定：

1. 生命周期事件历史的 `event_sequence` 必须从 1 开始连续；
2. 快照中的每个生命周期事件必须完整绑定当前 `ProviderRequest` 的 request identity、reply/segment、provider/generation、voice profile、audio format 和 capability check；
3. 快照中的事件 ID 在事件历史内不得重复，并且不得与音频块 ID 跨类型碰撞；
4. 音频块历史继续保持 request 完整绑定、块序号与本地接收顺序连续；
5. `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 返回的快照及嵌套 tuple 只能提供不可变观察，不得被调用方就地追加、替换或回写；
6. 所有直接构造/`dataclasses.replace(...)` 的非法快照都必须在写入注册表前拒绝，不污染保存快照、历史或全局 event ID 观察结果；
7. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

注册表写入路径已经在提交前检查单个事件的 request 绑定、generation fence、事件序列和全局 event ID；公开读取也已经返回不可变 dataclass 与 tuple。此前不可变快照的 `__post_init__` 仍缺少以下统一自洽检查：

- 直接构造或 `replace(...)` 的事件历史可以携带跳号的 `event_sequence`；
- 事件历史中的事件字段未由快照层统一重新验证完整 request 绑定；
- 直接构造或 `replace(...)` 的快照可以让事件 ID 与音频块 ID 在跨类型投影中碰撞；
- 公开读取返回的嵌套 tuple 缺少显式专项测试来证明调用方无法追加或替换元素。

### 2.2 最小实现修复

在 `app/tts/lifecycle/__init__.py` 的 `ProviderRequestLifecycle.__post_init__` 增加：

1. 用快照位置校验事件 `event_sequence == 1..N`；
2. 对每个事件重新校验 `provider_request_id`、`reply_id`、`segment_id`、`provider_name`、`provider_generation`、`voice_profile_id`、`voice_profile_version`、`audio_format` 和 `capability_check` 与当前 request 一致；
3. 在快照构造期间维护事件/音频块共享的 `seen_event_ids`，拒绝事件历史内重复 ID 与跨类型 ID 碰撞；
4. 保留既有音频块连续性、完整绑定、完成态/终态、音频事实门控和终态末事件校验。

没有改变：

- `ProviderRequestLifecycleRegistry` 的公开读取懒创建语义；
- event/audio chunk 的全局 event ID 去重与提交顺序；
- generation fence 的 provider 隔离、单调推进和拒绝顺序；
- 失败/取消可在无音频块或已有局部音频事实后终止的语义；
- lifecycle 事件和音频块都是绑定 request 的不可变本地元数据事实；
- tuple 返回值的 identity 稳定性和不可变观察语义。

## 3. 新增专项测试

在 `tests/tts/test_provider_request_lifecycle.py` 新增 3 项覆盖：

- `test_snapshot_rejects_non_contiguous_or_misbundled_event_history`
  - 直接 `replace(...)` 构造跳号事件历史时拒绝；
  - 直接改写事件 `reply_id` 时拒绝完整绑定破坏；
  - 直接构造重复事件 ID 时拒绝；
  - 所有拒绝不改变注册表当前快照和公开事件投影。
- `test_snapshot_rejects_event_and_audio_chunk_id_collision`
  - 直接改写音频块 `event_id` 使其与事件 ID 碰撞时拒绝；
  - 原快照、事件历史和音频块历史保持不变。
- `test_public_projection_nested_tuples_are_immutable_observations`
  - `events_for(...)` 和 `audio_chunks_for(...)` 返回当前快照中相同的 tuple 观察；
  - 调用方无法对 tuple 执行 `append` 或索引赋值；
  - 拒绝尝试后注册表仍返回原快照和原 tuple。

## 4. 修改文件

- `app/tts/lifecycle/__init__.py`
  - 补充生命周期快照的事件序列连续性、事件字段完整绑定和跨类型 event ID 碰撞校验。
- `tests/tts/test_provider_request_lifecycle.py`
  - 新增快照历史拒绝、事件/音频块 ID 边界和公开嵌套 tuple 不可变观察专项测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch14-provider-request-event-sequence-snapshot-binding-public-projection-handoff.md`
  - 本交接文档；未修改 batch13 或更早批次交接文档。

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

- Provider request lifecycle 专项：**44/44 通过**。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**80/80 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**425/425 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 检查：

- `app/tts/lifecycle/__init__.py`、`tests/tts/test_provider_request_lifecycle.py` 均按 UTF-8 严格读取成功；
- `U+FFFD` 替换字符数量：**0**；
- 本 handoff 文档按 UTF-8 严格写入并复读验证；
- 当前仓库尚无 Git commit，全部文件仍为未跟踪状态；`git diff --check` 无索引差异可比较，受影响文件未发现尾随空白或异常控制字符。

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

**P7 批次 15：Provider request lifecycle 注册表提交边界、保存快照身份稳定性与跨 request 公开投影复核。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 复核事件写入和音频块写入在所有校验失败路径上的提交边界、旧快照保留和全局 ID 观察一致性；
4. 复核不同 request 之间公开读取、事件历史、音频块历史和共享 event ID 集合的隔离与交叉拒绝顺序；
5. 复核成功写入后旧快照、当前快照和 `get(...)`/`events_for(...)`/`audio_chunks_for(...)` 返回对象的 identity 稳定性；
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