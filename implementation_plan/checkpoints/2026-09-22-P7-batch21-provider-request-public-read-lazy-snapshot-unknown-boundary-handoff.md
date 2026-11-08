# 2026-09-22 P7 批次 21：Provider request 公开读取空值、未知对象与惰性 snapshot 边界回归交接

> 阶段：P7 Provider request 本地生命周期契约
>
> 批次状态：**complete**
>
> 完成日期：2026-09-22
>
> 续接依据：`implementation_plan/checkpoints/2026-09-22-P7-batch20-provider-request-public-read-only-boundary-audit-handoff.md`
>
> 强制停止：本批次完成后停止，不进入真实 Provider、网络、播放设备、真实音频流或 P8。

## 1. 本批次目标

在 batch20 已验证不可变公开对象图、公开 tuple/object identity、`active_generation(...)` 只读无副作用和 malformed provider name fail-closed 的基础上，完成 P7 Provider request lifecycle 公开读取边界的下一轮回归：

1. 确认 `get(...)`、`events_for(...)`、`audio_chunks_for(...)` 对未知 `provider_request_id` 继续 fail closed；
2. 确认未知 request 的失败读取不会惰性创建 `PREPARED` snapshot，不消费 event ID，也不建立或推进 generation fence；
3. 确认 `active_generation(...)` 对空字符串、含空格字符串、`None`、`True` 等 malformed provider name 拒绝，并保持全部公开观察不变；
4. 复核合法 request 的惰性 `PREPARED` snapshot、未知 request 与空 provider name 边界，不扩大到真实 Provider、网络或播放设备。

## 2. 审查结论与最小变更

### 2.1 产品实现结论

`ProviderRequestLifecycleRegistry` 现有实现满足 batch21 的边界要求，无需修改产品代码：

- `get(...)` 先通过 `_get_request(...)` 校验并查找 request，未知 request 在 `_snapshot_for(...)` 前 fail closed，因此不会创建未知 request 的 snapshot；
- `events_for(...)` 与 `audio_chunks_for(...)` 通过 `get(...)` 读取，继承相同的未知 request 与 malformed request ID 边界；
- 合法 request 的首次公开读取仍只会惰性建立 `PREPARED` snapshot，这是既定且已验证的公开语义；
- `active_generation(...)` 只执行 `require_identifier(...)` 和 `_active_generations.get(...)`，不会创建 snapshot、消费 event ID、建立 generation fence 或推进 generation；
- malformed provider name 在读取前被拒绝，失败读取不会改变 `_snapshots`、`_event_ids` 或 `_active_generations`。

### 2.2 最小变更

仅修改 `tests/tts/test_provider_request_lifecycle.py`，新增 2 项 batch21 专项回归测试；未修改产品实现：

1. `test_unknown_public_reads_preserve_all_observations_without_lazy_snapshot`（第 558-571 行）
   - 对未知 `provider_request_id` 依次调用 `get(...)`、`events_for(...)`、`audio_chunks_for(...)`；
   - 确认三类读取均抛出 `ProviderRequestLifecycleError` 且包含“未知 provider_request_id”；
   - 确认 `_snapshots`、`_event_ids`、`_active_generations` 均保持原值，未创建未知 request 的惰性 snapshot。
2. `test_generation_observation_rejects_malformed_provider_names_without_side_effects`（第 573-582 行）
   - 使用空字符串、含空格字符串、`None`、`True` 四类 malformed provider name 调用 `active_generation(...)`；
   - 确认读取均 fail closed；
   - 确认 `_snapshots`、`_event_ids`、`_active_generations` 均保持原值。

没有修改：

- `app/tts/lifecycle/__init__.py`；
- `app/tts/requests/__init__.py`；
- `implementation_plan/implementation-roadmap.md`；
- `task_plan.md`；
- `progress.md`；
- batch20 或更早批次交接文档；
- `.venv`、`.python312`、环境变量持久配置或依赖安装状态。

## 3. 修改文件

- `tests/tts/test_provider_request_lifecycle.py`
  - 新增未知 request 公开读取不创建 snapshot 且不改变任何公开观察的回归测试；
  - 新增 malformed provider name 的 generation 观察 fail-closed 且无副作用回归测试。
- `implementation_plan/checkpoints/2026-09-22-P7-batch21-provider-request-public-read-lazy-snapshot-unknown-boundary-handoff.md`
  - 本交接文档。

产品实现文件未修改：

- `app/tts/lifecycle/__init__.py`
- `app/tts/requests/__init__.py`

## 4. 验证证据

以下命令均在 `<PROJECT_ROOT>` 执行，使用机器上可正常启动的 Python 3.12.10。

### 4.1 Provider request lifecycle 专项

```powershell
py -3.12 -m unittest tests.tts.test_provider_request_lifecycle -v
```

- Provider request lifecycle 专项：**58/58 通过**。
- batch20 基线为 56/56，本批次新增 2 项测试。

### 4.2 P7/TTS 专项

```powershell
py -3.12 -m unittest discover -s tests/tts -p "test_*.py"
```

- P7/TTS 专项：**94/94 通过**。

### 4.3 全量 unittest

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py"
```

- 全量 unittest：**439/439 通过**。
- batch20 基线为 437/437，本批次新增 2 项测试。

### 4.4 编译检查

```powershell
py -3.12 -m compileall app tests
```

- `compileall app tests`：通过。

### 4.5 严格 UTF-8 与变更文件质量

- 全仓 Python 文件严格 UTF-8 读取：**133 个文件通过**；
- 全仓未发现 U+FFFD 替换字符；
- 全仓未发现异常控制字符；
- 变更文件 `tests/tts/test_provider_request_lifecycle.py` 严格 UTF-8 读取成功；
- 变更文件无 BOM、无替换字符、无尾随空白；
- 全仓既存 BOM 仍为 3 个文件：`tests/recovery/test_recovery_scanner.py`、`tests/security/test_audit_persistence.py`、`tests/tts/test_capabilities.py`，均不属于本批次变更，未修改；
- 全仓既存尾随空白仍仅为 `tests/tts/test_provider_requests.py`，不属于本批次变更，未修改。

### 4.6 执行面 AST 审计

- 变更测试文件仅导入 `unittest`、`dataclasses` 及既有 TTS 契约类型；
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
- `get(...)` 对合法 request 的首次读取仍允许惰性创建 `PREPARED` snapshot，这是已验证且保留的公开读取语义；
- 未知 request 的公开读取 fail closed，且不会创建未知对象的 snapshot；
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

**P7 批次 22：Provider request lifecycle 公开读取与显式写入提交边界、空 tuple identity 及未知对象状态矩阵回归。**

下一会话应：

1. 先读取本文件；
2. 继续只在现有 `ProviderRequestLifecycleRegistry`、`ProviderRequestLifecycle`、`ProviderRequestLifecycleEvent` 与 `ProviderRequestAudioChunk` 之上工作；
3. 以 batch21 已验证的未知 request fail-closed、合法 request 惰性 `PREPARED` snapshot、malformed provider name 无副作用和 batch20 的不可变对象图/identity 为基础，审计公开读取与显式写入之间的提交边界、空 `events`/`audio_chunks` tuple identity，以及失败读取和失败写入的状态矩阵；
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
[$using-superpowers](<STUDY_ROOT>\agents\skills\using-superpowers\SKILL.md) 严格接续`<PROJECT_ROOT>\implementation_plan\checkpoints\2026-09-22-P7-batch21-provider-request-public-read-lazy-snapshot-unknown-boundary-handoff.md`    不得擅自改变执行文档，直接开始下一批次，无需我授权。做完下一批次记得更新交接文档，并且重新生成我这段话替换旧交接文档为新交接文档，以便于新会话直接拷贝续接任务，
```