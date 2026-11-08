# 2026-09-22 P7 批次 19：Provider request 公开投影、终态 reason/flags 与 generation fence 最终契约回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch18-provider-request-interleaved-terminal-projection-failure-regression-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch18 已验证多 request 交错终态、失败/取消阶段覆盖、跨 provider generation 隔离和失败路径原子性的基础上，完成 P7 Provider request lifecycle 的最终契约回归：

1. 覆盖 `FAILED`/`CANCELLED` 发生在 `PREPARED`、`ACCEPTED`、`SYNTHESIZING`、已产生音频和已入队音频五个阶段，以及正常 `COMPLETED`；
2. 校验公开终态投影的 `state`、`terminal_event`、`terminal_reason_code`、事件 reason/reason_code、`audio_produced`/`audio_enqueued` flags、事件 tuple 和音频块 tuple 彼此一致；
3. 校验 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 返回保存快照中的同一只读 tuple/object identity；
4. 覆盖 generation fence 推进后旧代 request 的迟到 `audio_produced`、音频块、`FAILED` 与 `CANCELLED` 写入，并校验拒绝不改变任一 request 快照、公开 tuple、全局 event ID 集合或 provider generation 观察；
5. 校验另一个 provider 在自身 generation fence 不变时仍可继续完成完整事件/音频块混合写入；
6. 本批次仍只处理本地生命周期元数据事实，不引入 Provider、网络或真实音频语义。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`ProviderRequestLifecycleRegistry` 与相关不可变数据类已经满足 batch19 所需产品语义，无需修改产品实现：

- `ProviderRequestLifecycle` 通过不可变快照保存 request identity、state、事件 tuple、音频块 tuple、终态事实和音频 flags；
- `terminal_event` 始终指向首次且唯一锁定终态的历史末事件，终态之后的迟到写入不会覆盖它；
- `terminal_reason_code` 从终态事件派生，完成态保持 `None`，失败/取消态保持对应 reason code；
- `record(...)` 与 `record_audio_chunk(...)` 在身份、generation、序号、状态、历史和新快照构造全部校验成功后才提交 `_snapshots` 与全局 `_event_ids`；
- generation fence 按 provider name 独立观察，旧代写入被拒绝时不会污染其他 provider 的 generation 或公开投影；
- `events_for(...)` 与 `audio_chunks_for(...)` 直接返回保存快照中的 tuple，公开读取与 `get(...)` 保持 identity 一致。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项 batch19 专项回归测试；未修改产品代码：

1. `test_terminal_projection_matrix_exposes_reason_flags_and_read_only_identity`
   - 交错构造五个失败/取消阶段和一个正常完成 request；
   - 逐项检查终态 state、终态事件 kind、reason、reason_code、`terminal_reason_code`、flags、事件 ID tuple、音频块 ID tuple；
   - 检查 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 与快照/嵌套 tuple 的 identity。
2. `test_generation_fence_final_rejection_matrix_preserves_public_projections_and_observations`
   - 构造两个 `voicestudio` generation 3 request 与一个 `edge_tts` generation 7 request；
   - 将 `voicestudio` fence 推进到 generation 4；
   - 拒绝旧代 request 的迟到 `audio_produced`、音频块、`FAILED` 与 `CANCELLED` 写入；
   - 校验三个 request 的快照 identity、事件/音频 tuple identity、全局 event ID 集合与两个 provider 的 generation 观察不变；
   - 随后完成 `edge_tts` request，确认另一 provider 的公开投影不受污染。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch18 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增终态 reason/flags/公开只读 identity 完整矩阵测试；
  - 新增 generation fence 迟到事件/音频/失败/取消写入最终原子拒绝矩阵测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch19-provider-request-public-projection-terminal-reason-flags-generation-fence-final-regression-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**54/54 通过**。
- batch18 基线 52 项保持通过，本批次新增 2 项均通过。

```powershell
py -3.12 -m unittest tests.tts.test_provider_requests
```

- Provider request 基础专项：**8/8 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**435/435 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 与变更文件质量检查：

- 全仓 Python 文件严格 UTF-8 读取：**133 个文件通过**；
- 全仓未发现 U+FFFD 替换字符；
- 全仓未发现异常控制字符；
- 未发现 UTF-8 BOM；
- 变更文件 `tests/tts/test_provider_request_lifecycle.py` 严格 UTF-8 读取成功，未发现替换字符、异常控制字符或尾随空白；
- 全仓唯一尾随空白仍为既存 `tests/tts/test_provider_requests.py`，不属于本批次变更，未修改。

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

**P7 批次 20：Provider request lifecycle 公开只读 API 与终态/代际拒绝契约的边界审计。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch19 已验证的终态 reason/flags 完整矩阵、公开 tuple/object identity、generation fence 迟到事件/音频/失败/取消拒绝原子性为基础，审计是否还存在最小的公开只读 API 边界缺口；
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch19-provider-request-public-projection-terminal-reason-flags-generation-fence-final-regression-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话替换旧交接文档为新交接文档，以便于新会话直接拷贝续接任务，
```