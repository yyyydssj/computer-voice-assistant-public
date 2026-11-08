# P7 小饼音色私有选择与真实试播进展（2026-09-28）

## 本轮已证实

- 此前本轮只读核验：VoiceStudio `/health` 为 200，版本 0.5.6；`/profiles` 中指定 ID `e8572e8e` 名称为「小饼·饼姐（沉稳声线候选3）」。试听参考 WAV 为单声道 24 kHz PCM16，4.53 秒，非静音；本轮后续在真实接缝中核对本地参考文件和服务端 `/profiles/e8572e8e/audio` 哈希完全一致。
- 用户现场试播确认 `speed=0.85` 及双句间 `[pause 400ms]` 的听感。一次原型播放的 Windows `PlaySound` 同步调用正常返回；此为用户听感确认和设备调用事实的不同证据，不能代表所有设备环境。
- 新增专有 `app/tts/private_selection.py`，在当前用户 `AppData/Local/ComputerVoiceAssistant/xiaobing_voice_selection.json`（仓库外）保存 Profile ID、用户、速度、句间停顿、参考 WAV 哈希及 schema version 1；首次写入前确认目标不存在，后读回校验通过。配置不修改 VoiceStudio 引擎默认声线，也不包含凭据/正文。既有文件不可自动覆盖；移除该新文件即可回滚这一私有选择，不影响其他配置。
- 新增真实字节隔离层 `app/tts/real_playback.py` 和显式文字试播入口 `app/tts/voice_preview.py`。每句 `/generate` 显式指定 Profile ID 和 speed，仅本机 loopback，限定三句/180 字，每句不超过 80 字；按序预先合成并校验 WAV 大小/PCM16/24kHz/时长，第二句起 PCM 增加 400ms 静音，之后按序通过独立子进程 Windows 默认输出设备播放。收到音频、播放调用返回和用户听到分开记录；失败时原文返回、禁用该实例不确定的后续声音。无 Edge 兜底、无 VoiceStudio 引擎默认改动、无 `.env` 更改。
- 新入口实机两次独立两句试播均返回：`received_audio=2`、`playback_calls_returned=2`、`text_fallback=False`。本次新增入口播放**没有再次采集用户听感回执**，不能借用前一次原型听感作为这次入口的用户确认。生成后音频只在内存/子进程 stdin，不写 WAV 文件。
- 新增离线用例 16 项；全量执行 `<PROJECT_ROOT>\.venv\Scripts\python.exe -B -m unittest discover -s tests -t . -q` 得 **708 tests / OK**；`compileall` 和 `git diff --check` 通过。此是新批次实跑结果，不回填此前验收的 692 项记录。

## 未完成与验收边界

- `app/main.py` 仍无 Agent 真实回复出口；本次**未**让生产助手自动说话，真实模块仅显式试播入口，未偷换 `app/tts/orchestration.py` 原 fake 端口。
- 合成有逐请求超时，但并非强保证总请求墙钟时长；预合成会在播放前等待，不支持低延迟流式播报。Windows 输出是当前默认设备，没有设备独占/设备 ID 固定/外部可观测的硬件完成 ACK；12 秒播放器子进程超时后终止进程不能证明声卡已经停止。合作式 cancel 能挡住未提交音频，不能作为设备硬中断证据。真实设备故障/停止/用户新入口听感仍待进一步验收。
- 同句 Edge 降级没有用户预先试听批准，也没有实现；VoiceStudio 不可用时只保留文字。P7 T-04/T-05/T-06 退出门未全通过；**P7 阻塞／未完成，不进入 P8**。旧验收文档不改写。
- 现有 staged/untracked 先前基线未重置、未批量暂存或提交。新增代码/文档仍是工作区 untracked 文件。

## 显式试播与回退

仅在用户有意测试、设备可用且短句不敏感时运行：

`<PROJECT_ROOT>\.venv\Scripts\python.exe -B -m app.tts.voice_preview --text "你好。接下来稍作停顿。"`

该入口不改变 Agent 事实或任务授权。回退私有选择应只删除 `<USER_HOME>\AppData\Local\ComputerVoiceAssistant\xiaobing_voice_selection.json`，保留其他文件；删除前由用户确认。代码侧模块不加载即不会在普通文字运行时播报。


