# P7 Agent runtime 隔离预检交接（2026-09-29）

## 范围

本次仅执行只读/隔离运行时预检，目标是确认下一次真实 Agent → VoiceStudio → Windows 设备证据批次的启动前置条件。

未执行：

- 真实 Agent 请求；
- API 网络调用；
- TTS 播放、录音或设备副作用；
- 历史 artifact 修改；
- 用户 DSH 配置修改；
- 进程终止、DLL 替换、VoiceStudio 配置修改；
- git stage/commit/push/reset。

## 预检事实

### 1. VoiceStudio

- `http://127.0.0.1:3900/health` 当前可用，HTTP 200。
- 版本为 `0.5.6`。
- 不需要修改 VoiceStudio 配置。

### 2. 项目默认 Agent HTTP 端口

- 项目适配器默认 endpoint：`http://127.0.0.1:3901/chat`。
- 2026-09-29 预检时 `3901` 无监听进程，连接会被拒绝。
- 当前没有证据表明可直接通过该 endpoint 启动真实 Agent batch。

### 3. 项目内官方 SDK/runtime

已确认存在：

- `<PROJECT_ROOT>\.python312\Scripts\dsh.exe`
- `<PROJECT_ROOT>\.python312\Lib\site-packages\deepseek_harness`
- `<PROJECT_ROOT>\.python312\Lib\site-packages\deepseek_harness_runtime`
- `<PROJECT_ROOT>\app\agent\p3-no-tools.patch.yml`

### 4. 隔离 DSH_HOME 启动预检

使用全新临时目录作为显式 `DSH_HOME`，配置：

- provider：`deepseek-official`
- model：`deepseek-v4-flash`
- profile：`sdk-minimal`
- patch：`app\agent\p3-no-tools.patch.yml`
- workspace/runtime cwd：项目根目录
- API key：仅使用占位值完成启动路径预检，不用于请求

结果：

```text
SDK_START=ok
```

该结果只证明本地 runtime 可以在干净的显式 `DSH_HOME` 下完成初始化；没有调用 `run()`，没有发送真实请求，也没有产生 Agent reply。

### 5. 现有用户 DSH_HOME 不可复用

对现有用户目录 `<USER_HOME>\.dsh` 进行启动预检时，runtime 初始化超时。stderr 指向旧 profile 中的缺失 UI skin 包：

```text
failed to import loader entry ui-skin-whale-song
@linxin666/dsh-client-ui-skin-whale-song
```

该目录包含用户本地 Harness 状态/凭据边界，本次不读取、不修复、不覆盖，也不把它作为新的真实证据运行目录。

### 6. patch 兼容性观察

当前 `p3-no-tools.patch.yml` 包含 `mcp-resources` 禁用项，而现版 `sdk-minimal` profile 不再提供该 entry。`dsh --dump-config` 会报告：

```text
patch: entry "mcp-resources" not found
```

但在全新的隔离 `DSH_HOME` 下，SDK 初始化仍返回成功。该兼容性提示尚未被提升为成功证据，也未直接修改 patch；后续如需清理，必须单独审查 patch 行为并跑离线契约测试。

## 当前判断

```text
P7 = Blocked / incomplete
P8 = Not entered
```

真实 Agent batch 的直接阻塞仍是：

1. 没有正在监听的项目默认 Agent endpoint `127.0.0.1:3901`；
2. 当前 PowerShell 进程没有经过授权注入的 `DEEPSEEK_API_KEY`；
3. 可用的用户 DSH profile 存在缺失 UI skin 的本地状态，不应未经授权修复或复用。

因此本次不能安全生成新的真实 batch，也不能为历史 batch `p7-d4afff8c3ad3402e` 回填任何 stop 字段。

## 下一项入口

要继续闭合同一真实 Agent batch 的五项证据，下一次运行必须先由用户提供或注入经过授权的真实 Agent runtime 环境，并使用全新的隔离 `DSH_HOME`。随后才可以串行执行：

```text
真实 Agent identity
→ exact reply
→ VoiceStudio audio hash
→ waveOut backend stop ACK
→ WASAPI loopback stop
→ 独立 microphone stop
→ speaker_output_stopped
→ 该 batch 的用户确认
```

所有结果必须写入新的 batch-specific artifact；不得跨 batch 覆盖历史字段。