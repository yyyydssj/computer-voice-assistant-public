# P7 T-03 / T-04 真实 TTS 和设备前置门（2026-09-28）

结论：**T-03 前置评审完成但未通过实测门；T-04 阻塞。** 本轮没有连接语音服务、没有访问扬声器或麦克风、没有产生音频，没有请用户确认听感；真实链路调用 0 次。T-02/T-05 的 fake 回执不等于设备回执，更不等于用户听到。

- 当前项目仅有 `app/tts/` 的本地契约与独立 `LocalSpeechOrchestrator`，无真实 `SynthesisPort`/`DevicePort` 实现；`app/main.py` 仍为文字壳。`implementation_plan/02-tts-voicestudio.md` 称 VoiceStudio 为本地主 Provider，但仓库未给出可复核的服务可执行文件、版本、API 地址、健康检查、授权/价格/隐私政策、输入输出格式和返回/停止语义。本轮未找到对应的已安装 TTS SDK；未读取凭据。
- 官方/原作者资料入口（实现版本及实际部署能力须在下一门现场核验）：[edge-tts `communicate.py`](https://github.com/rany2/edge-tts/blob/master/src/edge_tts/communicate.py) 使用网络 WebSocket，输出格式取决于版本和实际接口；项目假接口现用 PCM，**必须先做格式转换/能力协商**，输出音频片段不是设备播放回执；[edge-playback](https://github.com/rany2/edge-tts/blob/master/src/edge_playback/__main__.py) 是另一播放步骤。Microsoft [TTS quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-text-to-speech?pivots=programming-language-python) 用 `AudioOutputConfig` 控制输出，但 Azure Speech SDK 不等于本项目的 VoiceStudio，也不能假定它与 Edge 非正式客户端有相同计费或许可。尚未确定设计中的“VoiceStudio”到底是哪一款产品/哪个部署实例，故不能假定接口。
- 本机非侵入式设备清单看到 Realtek 扬声器状态 OK，蓝牙设备 Unknown；**仅枚举不能证明设备可独占/打开/播出/可听**。无固定设备 ID、格式转换器、停止回执、音频完成确认方案。缺少已试听并预先选择的 Edge 音色证据；Edge 网络/隐私及使用约束待确认。
- 下一次有界实测的建议上限（**非已执行**）：提供真实 VoiceStudio 产品/版本及官方接口、隐私/计费、有效账号/本地实例和用户选定扬声器；先验证可撤销的提交点与输出格式；用非敏感固定中文两句、每 Provider 最多 2 次含重试、每次 <= 15 秒、不记录正文/音频/密钥；设备操作只限明确选定的输出端，测试结束停止并释放资源；若失败立即文字回退，禁止在疑似发声后用 Edge 补播本句。记录设备 ACK 与人为听感观察为不同字段。
- T-04 **阻塞**：前置条件缺失，不能从声卡枚举、FakeDevice 或 `PLAYED` 状态推断真实播报成功。满足条件后逐句验证生成、设备提交、开始/进度/停止和用户观察；任何缺证都应在阶段验收中保持未通过。不得进入 P8。