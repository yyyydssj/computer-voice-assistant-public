# 2026-09-22 P7 批次 20：Provider request 公开只读边界审计交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch19-provider-request-public-projection-terminal-reason-flags-generation-fence-final-regression-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch19 已验证终态 `reason`/`reason_code`、音频 flags、公开 tuple/object identity 与 generation fence 迟到写入原子拒绝的基础上，完成 P7 Provider request lifecycle 的公开只读 API 边界审计：

1. 确认 `ProviderRequestLifecycle`、嵌套 `ProviderRequest`、事件、音频块及 capability 投影形成不可变公开对象图；
2. 确认 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 继续返回保存快照及其嵌套 tuple 的同一 identity，而不是可变副本或脱离快照的重建对象；
3. 确认公开的 generation 观察 `active_generation(...)` 是纯读取，不创建 request snapshot、不消费 event ID、不建立或推进 generation fence；
4. 确认 malformed provider name 读取仍 fail closed，且失败读取不改变任何公开观察；
5. 如审计发现缺口，只补最小回归测试；不扩大到真实 Provider、网络、播放设备、音频 payload 或 P8。

## 2. 审查结论与最小变更

### 2.1 现有实现结论

`ProviderRequestLifecycleRegistry` 与相关不可变数据类已经满足 batch20 的公开只读边界要求，无需修改产品实现：

- `ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent`、`ProviderRequestAudioChunk`、`ProviderRequest` 及其 capability/domain 嵌套对象均为 `@dataclass(frozen=True, slots=True)`；
- `events` 与 `audio_chunks` 是保存于快照中的 tuple，事件和音频块对象不会通过公开读取变为可变副本；
- `get(...)` 对已保存 request 返回同一快照；`events_for(...)` 与 `audio_chunks_for(...)` 直接返回该快照持有的 tuple；
- 公开读取只会在合法 request 首次读取时惰性建立 `PREPARED` 快照，不会建立全局 event ID，也不会修改 generation fence；
- `active_generation(...)` 只校验 provider name 并读取 `_active_generations`，对未知但格式合法的 provider name 返回 `None`，不创建 fence；非法 provider name 被拒绝且不改变任何注册表状态；
- 生成代际仍由显式 `activate_generation(...)` / `advance_generation(...)` 推进，读取 API 不具备推进或回退副作用。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项 batch20 专项回归测试；未修改产品代码：

1. `test_public_snapshot_object_graph_is_read_only_and_preserves_identity`（第 1879-1910 行）
   - 构造 accepted、synthesis_started 和一个音频块；
   - 检查快照、request、事件 tuple、音频块 tuple、嵌套事件/capability/audio format 对象的 identity；
   - 尝试修改快照、request、capability request、事件和音频块，确认均抛出 `FrozenInstanceError`；
   - 再次公开读取确认快照及 tuple identity 未被改变。
2. `test_generation_observation_reads_are_side_effect_free_and_do_not_create_or_advance_fences`（第 1912-1934 行）
   - 在无快照、无 event ID、无 generation fence 的初始状态读取已注册 provider 和未知 provider；
   - 检查非法 provider name 被拒绝；
   - 校验所有读取和拒绝读取均不创建快照、不消费 event ID、不建立 fence；
   - 显式激活 generation 后再次读取，确认只观察现有 fence，不推进、不污染其他 provider。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `app/tts/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch19 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增公开快照对象图只读和 identity 回归测试；
  - 新增 generation 观察读取无副作用回归测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch20-provider-request-public-read-only-boundary-audit-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10：

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**56/56 通过**。
- batch19 基线为 54/54，本批次新增 2 项测试。

```powershell
py -3.12 -m unittest discover -s tests/tts -p "test_*.py"
```

- P7/TTS 专项：**92/92 通过**。

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**437/437 通过**。

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

严格 UTF-8 与变更文件质量检查：

- 全仓 Python 文件严格 UTF-8 读取：**133 个文件通过**；
- 全仓未发现 U+FFFD 替换字符；
- 全仓未发现 BOM；
- 变更文件 `tests/tts/test_provider_request_lifecycle.py` 严格 UTF-8 读取成功，未发现替换字符、异常控制字符或尾随空白；
- 全仓唯一尾随空白仍为既存 `tests/tts/test_provider_requests.py`，不属于本批次变更，未修改。

执行面审计：

- 变更测试文件仅导入 `unittest` 及既有 TTS 契约类型；
- 未引入网络、进程、命令执行、文件副作用、真实设备或后台线程入口；
- AST 检查未发现 `subprocess`、socket/HTTP 客户端、`open`、`exec`、`eval` 或 `compile` 调用。

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
- `get(...)` 对合法 request 的首次读取仍允许惰性创建 `PREPARED` 快照，这是已验证且保留的公开读取语义；
- `active_generation(...)` 对格式合法但尚未注册的 provider name 返回 `None`，不会自动建立 fence；
- `ProviderRequestAudioChunk` 仍是绑定 request 的不可变音频块元数据，不承载 PCM/编码 payload；
- 失败/取消只表示本地生命周期事实，不表示远端 Provider 已停止，也不表示播放已停止；
- `audio_produced` 和 `audio_enqueued` 不等价于播放开始、播放完成或用户听见；
- 本批次仍未承诺并发线程安全，原子性是同步失败路径原子性；
- 当前仓库没有 Git commit，仍未执行 add、commit 或 push；
- 尚未实现真实 Provider adapter、网络调用、流式音频传输、播放队列、播放事实、实时打断、全双工、AEC、连续监听、唤醒词、声纹或语音 L3。

## 8. 下一批次入口与强制停止

建议下一批次为：

**P7 批次 21：Provider request lifecycle 只读公开 API 的空值/未知对象/惰性 snapshot 边界回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch20 已验证的不可变对象图、公开 tuple/object identity、generation 观察无副作用为基础，审计合法 request 的惰性 `PREPARED` snapshot、未知 request、空 provider name 和未注册 provider generation 的边界；
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch20-provider-request-public-read-only-boundary-audit-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话替换旧交接文档为新交接文档，以便于新会话直接拷贝续接任务，
```
