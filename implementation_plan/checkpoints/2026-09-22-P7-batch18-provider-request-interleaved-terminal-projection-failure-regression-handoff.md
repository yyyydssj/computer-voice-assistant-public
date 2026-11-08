# 2026-09-22 P7 批次 18：Provider request 多 request 交错终态/投影一致性与失败路径最终回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch17-provider-request-generation-provider-isolation-terminal-projection-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch17 已验证 provider-scoped generation fence、同 provider 代际切换和三类终态隔离的基础上，最终回归多 request 交错提交下的公开终态/投影一致性：

1. 覆盖 request 在 `PREPARED`、`ACCEPTED`、`SYNTHESIZING`、已产生音频和已入队音频阶段发生 `FAILED` 或 `CANCELLED`；
2. 验证交错终态 request 的首次终态事实、终态 reason、事件历史、音频块历史、`audio_produced`/`audio_enqueued` flags 和 `get(...)` identity 保持一致；
3. 验证一个 request 的终态写入不污染其他仍在进行或已完成 request 的公开投影；
4. 验证旧 generation request 的事件/音频块/终态迟到写入在 generation fence 推进后仍被拒绝，并且拒绝不改变任一 request 快照、公开 tuple、全局 event ID 集合或 generation 观察；
5. 验证其他 provider 在自身 generation fence 不变时仍可继续完成完整事件/音频块混合写入；
6. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`ProviderRequestLifecycleRegistry` 当前实现已经满足本批次所需产品语义，无需修改产品代码：

- 每个 request 的不可变 `ProviderRequestLifecycle` 快照独立保存 state、事件 tuple、音频块 tuple、终态事实和音频 flags；
- `record(...)` 与 `record_audio_chunk(...)` 在所有身份、generation、序号、状态、历史和公开快照构造校验完成后才提交 `_snapshots` 与全局 `_event_ids`；
- 终态事件只追加到当前 request，终态后的迟到事件/音频块不会覆盖首次终态；
- generation fence 按 provider name 独立观察，旧代 request 的迟到写入不会改变其他 provider 的 fence 或 request 投影；
- 全局 event ID 的候选消费仍位于成功提交边界之后，失败路径可以安全复用未消费的 event ID。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项专项测试并增加一个测试辅助构造函数：

1. `test_interleaved_terminal_paths_preserve_other_request_projection_and_ids`
   - 交错创建六个 generation 3 request；
   - 分别覆盖 `PREPARED` 失败、`ACCEPTED` 取消、`SYNTHESIZING` 失败、`audio_produced` 后失败、`audio_enqueued` 后取消；
   - 同时让另一个 request 完整完成；
   - 校验各 request 的终态、首次终态事件、reason、音频块 tuple、flags、事件 tuple 和 `get(...)` identity；
   - 校验终态 request 的迟到写入不消费 event ID，并允许其他 request 复用该 ID。
2. `test_interleaved_rejected_writes_keep_snapshots_ids_and_generation_observations_atomic`
   - 交错写入两个 `voicestudio` generation 3 request 与一个 `edge_tts` generation 7 request；
   - 将 `voicestudio` fence 推进到 generation 4；
   - 拒绝旧代 request 的迟到 `audio_produced`、音频块和 `FAILED` 写入；
   - 校验三个 request 的 snapshot/事件 tuple/音频块 tuple identity、全局 event ID 集合和两个 provider 的 generation 观察均保持不变；
   - 随后允许 `edge_tts` request 完整完成，证明另一 provider 的公开投影不受污染。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch17 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增多 request 交错终态路径专项测试；
  - 新增 generation fence 迟到失败/取消/音频写入的原子拒绝回归测试；
  - 新增 `_prepare_batch18_request(...)` 测试辅助构造函数。
- `implementation_plan/checkpoints/2026-09-22-P7-batch18-provider-request-interleaved-terminal-projection-failure-regression-handoff.md`
  - 本交接文档。

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**52/52 通过**。
- batch17 基线 50 项保持通过，本批次新增 2 项均通过。

```powershell
py -3.12 -m unittest discover -s tests\tts -p "test_*.py"
```

- P7/TTS 阶段专项：**88/88 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**433/433 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 与变更文件质量检查：

- `tests/tts/test_provider_request_lifecycle.py` 按 UTF-8 严格读取成功；
- 变更测试文件的 `U+FFFD` 替换字符数量：**0**；
- 变更测试文件未发现尾随空白或异常控制字符。
- 全仓 Python 文件严格 UTF-8 读取：**133 个文件通过**；
- 全仓检查发现既存尾随空白：`tests/tts/test_provider_requests.py`，不属于本批次变更，未修改；
- 全仓未发现 `U+FFFD` 替换字符或异常控制字符。

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
- 本批次仍未承诺并发线程安全，原子性是同步失败路径原子性；
- `activate_generation(...)` 允许对格式合法但尚未注册的 provider name 建立 fence；本批次仅复核现有语义，未改变也未扩大该语义；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 19：Provider request lifecycle 公开投影只读边界、终态 reason/flags 完整矩阵与 generation fence 最终契约回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch18 已验证的多 request 交错终态、失败/取消阶段覆盖、跨 provider generation 隔离和失败路径原子性为基础，检查公开只读投影、终态 reason/flags、事件 tuple、音频块 tuple 与 identity 的最终组合矩阵；
4. 必要时补充最小专项测试，但不得扩大到真实 Provider 或网络；
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch18-provider-request-interleaved-terminal-projection-failure-regression-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话替换旧交接文档为新交接文档，以便于新会话直接拷贝续接任务，
```