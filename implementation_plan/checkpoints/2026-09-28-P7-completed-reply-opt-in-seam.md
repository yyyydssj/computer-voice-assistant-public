# P7 第 1 项：完成回复到文字优先的显式播报接缝（2026-09-28）

## 本批变更

- `app/ui/text_shell.py` 的 `run_conversation` 只将 `AgentTurnOutcome` 的已完成、无失败、非空文字且有终止标志的结果传给显式注入的播报回调；在回调前先将完整原文写入文字输出并 flush。无结果、失败或部分回复只保持原有文字处理，不播报。
- `app/tts/completed_reply.py` 增加按 `(session_id, request_id)` 防重的独立入口；以当前用户私有配置创建 `RealVoicePreview`，将完全相同的文字传给真实试播端口。异常、音频回退、原文不一致时禁用该实例的后续播报，原 Agent 回复/文字不被改写；不把播放调用完成当用户听到。
- 不更改 `app/main.py` 的默认入口或 `voice_available=False`；不建立 Agent、路由、工具/授权链，不启用 Edge，不替换既有 fake TTS 端口。调用方未来须显式注入已就绪的 ConversationManager 与播报器。当前不能声称正式助手已会自动讲话。

## 验证

- 新增 `tests/tts/test_completed_reply.py` 7 个离线测试：先显示文字、失败/缺失/部分回复不播、重复回复不重播、私有选择故障/设备回退后实例禁声、默认无注入不播。
- 本次执行全量 `python -B -m unittest discover -s tests -t . -q`：**715 tests / OK**；相关模块 `compileall` 与 `git diff --check` 成功。离线 fake 回调不是设备播放或用户听感证据。
- 工作区此前已存在大量 staged/untracked；本批未批量 add/reset/commit/push，未归属旧改动。此前真实试播 708/708 与旧 T-06 692/692 是不同批次。

## 剩余停点

第 1 项仅“已完成回复的文字优先播报接缝”可验收；真实 Agent 在默认运行时未就绪，不能宣称第 1 项端到端验收或 P7 T-04/T-06 通过。下一顺序为真实设备/停止事实；在真实 Agent 回复链路和必要的设备中断安全性另经验证前，不把显式入口接入默认主程序。P7 阶段仍阻塞，未进入 P8。