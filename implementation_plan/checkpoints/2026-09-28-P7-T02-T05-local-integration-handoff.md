# P7 T-02 / T-05 本地播报编排与故障边界交接（2026-09-28）

状态：**T-02 本地集成和假设备测试完成；T-05 仅本地故障子集通过，同句 Edge 降级未完成而阻塞；真实播报与 P7 阶段验收未完成**。基线：2026-09-27 T-01 测试矩阵、G0、`app/tts` 现有契约。独立于 `app/main.py` 和任务/计划/授权/账本/执行链；默认文字运行不加载/实例化本接缝。

## 实现与行为

- 新增 `app/tts/orchestration.py`，只接受调用者显式确认的完整文字；按句分段→用户当前 profile 的 request（固定 reply/segment/request/generation/version）→lifecycle/audio 元数据→单句预算 FIFO→假适配器的 commit/start/complete ACK→本地播放事实。音频字节/设备实现不在此接缝。每回复一次，只允许按连续编号准备请求，无重复旧回复。仅报告 `acknowledged_segments`（设备适配器 ACK，不表示听见），始终保留原文。
- 失败/取消/背压/版本漂移：从未尝试设备提交的失败直接文字；提交竞态或已开始则先请求停止并记录 `stop_unconfirmed`，丢弃本回复后续句，不同 reply 不会续播原音频；未知停止或已尝试播放后的故障在当前编排实例中禁止未来语音（重新实例化需外部设备复核）。**没有在同回复中自动重试或切 Edge**：旧 `ProviderRequestRegistry` 的 `(reply_id, segment_id)` 唯一性不支持同句跨 Provider 再准备，严禁绕过。只有后续**新回复**在 Edge profile 由 USER 已明确选择且绑定的情况下才使用 Edge；Edge 失败后维持文字。`TTSFallbackRouter` 的 `USE_EDGE_CURRENT_SEGMENT` 是领域建议，不是本编排可兑现的设备动作，绝不将其计入退出条件 2 的已验收证据。
- VoiceProfile 在回复开始冻结；准备新句前及设备提交前再次检查用户选择与版本。USER 权限由原注册表约束；未选择、禁用、旧版本均文字回退，不自动选音色。设备 ACK 后的用户改动不会修改已绑定请求，只影响下一回复；尚未准备的下一句遇版本漂移则拒绝。
- `cancel(reply_id)` 为同步/合作式门；在设备回调内部触发会阻止后续事件，尝试设备停止；不是跨线程抢占、硬件完成证明或零延迟停止。T-05 故障矩阵覆盖合成超时、设备提交失败/不确定、开始失败、完成超时、停止失败、预算背压、音色漂移、取消与回调重入。若适配器说谎或抛异常，本地 ACK 不证明实际设备状态。

## 验证与范围

- 新增 `tests/tts/test_orchestration.py`：覆盖顺序、跨段身份、文字保留、双 Provider 顺序在新回复边界、提交歧义、取消、超时/背压、stop 不确认、profile 变化与重入。专项 **14/14 passed**；全量 **692/692 passed**，`python -m compileall -q app tests` 通过，`git diff --check` 通过（注意新文件 untracked 不由 diff --check 覆盖）。
- 无真实 TTS 请求/设备占用/声音采集或音频生成、无凭据读取、费用 0；没有改动 `app/main.py`、UI、P2/P3/P5 安全链，也没有自动接入生产；因此不能声称真实故障回退或 end-to-end 界面验收。保守选择优于在未验证设备撤销点前重播或混声。
- 本批仅新增上述两个文件及本交接，并更新 P7 跟踪表。既有 staged/untracked 内容为前序基线，不整体暂存/提交。真实测试门见 [T-03/T-04](2026-09-28-P7-T03-T04-real-tts-gate.md)。下一步仅停在 P7 验收：检查真实设备及可听证据、同句安全回退方案和文字界面联动后再考虑 P7 complete，不进入 P8。