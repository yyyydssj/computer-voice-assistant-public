# P7 STOP_TIMEOUT / UNKNOWN 只读能力复核

- 日期：2026-09-29
- 目的：复核是否存在可安全执行的真实 Windows 音频后端故障注入入口
- 操作性质：只读；未执行播放、录音、故障注入或进程/配置修改

## 复核结果

### VoiceStudio

- 健康端点：`http://127.0.0.1:3900/health`
- 服务可达，版本：`0.5.6`
- OpenAPI 中发现的音频相关端点包括 `/generate`、`/v1/audio/speech`、`/engines/sonitranslate/stop` 等。
- 未发现针对当前 waveOut 播放 worker 的受支持 `STOP_TIMEOUT`、`UNKNOWN`、reset-error、drop-callback 或 buffer-corruption 注入端点。
- `/engines/sonitranslate/stop` 属于其他服务能力，不能据此证明当前 waveOut 后端故障语义，未调用。

### 项目 fault fixture

文件：

```text
<PROJECT_ROOT>\app\tts\fault_fixtures.py
```

该文件明确声明：

```text
evidence_kind = controlled_fault_fixture
```

fixture 的 backend：

- 不打开真实音频设备
- 不调用 WinMM / PortAudio
- 不发送音频到扬声器
- 仅记录 bytes 并返回离线协议结果

支持的离线模式：

```text
RESET_ERROR
DROP_CALLBACK
DELAY_CALLBACK
WRONG_BUFFER_CALLBACK
```

因此这些模式可以支持离线回归测试，但不能升级为真实 `STOP_TIMEOUT / UNKNOWN` 证据。

## 判定

```text
真实 STOP_TIMEOUT：Blocked
真实 UNKNOWN：Blocked
controlled_fault_fixture：仅离线回归，不是设备证据
```

原因：当前没有安全、可复核、真实触达 Windows 音频后端的 fault shim。未经明确且可恢复的真实后端注入能力，不执行 DLL 替换、进程终止、设备破坏或非幂等配置修改。

## 未执行

- 未调用任何 OpenAPI 音频端点
- 未调用 `/engines/sonitranslate/stop`
- 未启动故障 fixture 作为真实证据
- 未杀进程
- 未替换 DLL
- 未修改 VoiceStudio 配置
- 未播放、录音或触碰音频设备状态