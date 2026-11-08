# P8 批次 1-3 停止交接（按键触发的半双工语音闭环：边界 / 输入接缝 / 闲聊闭环）

> 日期：2026-09-30  
> 前置：`checkpoints/P7-handoff.md`、`checkpoints/2026-09-30-P7-stage-acceptance-final.md`、`implementation-roadmap.md` P8 节、`08-text-end-to-end.md`、`09-voice-end-to-end.md`、`P6-handoff.md`、`2026-09-21-P6-integration-acceptance.md`。

## 1. 授权与范围

- 用户 2026-09-30 明确授权：**执行 P8 批次 1-3**，并选择**先不处理** P7 遗留的 51 项未提交改动，P8 改动叠加在同一工作区。
- 本轮只执行批次 1-3，并在批次 3 完成后停止；未进入批次 4-6（任务路径编排、“停”的三语境、阶段级集成验收）。
- 未联网、未读取 `.env` 或任何密钥、未启动真实 DeepSeek/VoiceStudio/麦克风/扬声器、未执行任何电脑副作用、未 `git add`/`commit`/`push`。

## 2. 阶段编号与状态

- 阶段编号：P8（按键触发的半双工语音闭环）
- 阶段状态：`in_progress`
- 本轮完成：批次 1、批次 2、批次 3
- 当前停止点：**批次 4 授权门**（任务路径编排）

## 3. 本阶段唯一目标（路线图原文）

组合 P5、P6、P7：按键开始说话，得到文字，调用同一个 Harness，文字回复可播报，电脑任务仍走完全相同的安全链。

## 4. 本阶段明确不做（仍有效）

- 不增加自动唤醒、不增加连续多轮自动收音。
- 不增加播放期间监听、AEC 或声纹认证。
- 不改变 P5 的授权规则；语音不得成为新的权限通道。
- 真实 DeepSeek provider-specific adapter 仍为 `NOT READY / STOP`，须单独授权。

## 5. 本轮交付物

### 5.1 新增 `app/voice_loop/`（5 个文件，全部只读/无副作用）

| 文件 | 内容 |
|---|---|
| `__init__.py` | 包说明与公开导出；明确不实现授权/计划/执行/审计/恢复语义 |
| `boundary.py` | `VoiceTurnBoundary`（voice_session_id / utterance_id / turn_id / request_id / reply_id / source / created_at，全部严格校验）、`VoiceTurnRegistry`（同一时刻至多一个活动轮次；身份一经签发不复用；关闭即归档；迟到/未知边界 `is_known=False`）、`VoiceLoopStateMachine`（显式迁移表，非法迁移与自迁移 fail closed；任务状态 `planning/awaiting_authorization/executing/verifying` 已命名但**无入边迁移**）、`VoiceLoopDiagnostic` / `VoiceLoopTransition`（脱敏，仅原因码与状态） |
| `input_seam.py` | `VoiceInputSeam`：TextShell 与 ASR accepted final 都只经同一个 `ConversationManager` 入口；`degrade(reason)` 只切换输入通道并关闭未完成语音轮次，**不触碰授权/执行/结果事实**；`note_voice_ready()` 只记录可用性、**绝不自动切回**；`request_voice_switch(local_check_passed)` 需同时满足“已就绪 + 本地检查通过 + 安全空闲（无活动轮次且不忙）”；`open_text_turn`/`open_voice_turn`/`submit`/`close_voice_turn` 保证通道不串用、同一轮次只提交一次 |
| `chat_loop.py` | `VoiceDriverTurnResult`（不变量：未提交不得称已提交、未接纳不得播报）、`VoiceTurnDriver` Protocol、`ChatVoiceLoop`（开始→收音→提交→只播报完整成功回复→回空闲；驱动失败、启动拒绝、迟到/降级边界一律清理并 fail closed）、`VoiceChatTurnReport` |
| `drivers.py` | `PushToTalkVoiceDriver`：把 `VoiceTurnBoundary` 映射为 P6 `AsrSessionBoundary`，`release()` 透传 `turn_id`/`request_id`，把 `ResultGateDecision` 与 `AgentTurnOutcome` 归一化为 `VoiceDriverTurnResult`；`speech_eligible()` 与 P6 `_speech_eligible` 语义一致（专项交叉校验） |

### 5.2 既有模块唯一改动（纯增量）

`app/asr/push_to_talk.py`：

- `release(..., *, turn_id=None, request_id=None)` 与 `accept_provider_event(event, *, turn_id=None, request_id=None)` 增加可选身份透传；
- 未提供时 `effective_turn_id = self._boundary.utterance_id`、`request_id=None`，**与改动前行为完全一致**；
- 目的：让真实 P6 管道提交给 `ConversationManager` 的 `RouteTrace` 与 P8 轮次边界（request_id/turn_id）一致，杜绝“本地边界一套、真实 trace 另一套”。
- diff 规模：`1 file changed, 19 insertions(+), 4 deletions(-)`；`git diff --check` exit 0。

### 5.3 测试

| 测试文件 | 项数 | 覆盖 |
|---|---:|---|
| `tests/voice_loop/test_turn_boundary.py` | 20 | 边界字段/时间/来源拒绝、身份唯一与不复用、单活动轮次、外键关闭拒绝、迟到边界不可信、合法迁移路径、非法迁移与任务状态 fail closed、终态吸收、诊断脱敏 |
| `tests/voice_loop/test_input_seam.py` | 21 | 初始文字态、就绪不自动切回、切回四道门槛、降级只切通道、拒绝原因码、固定脱敏状态文案、两通道同一会话入口、通道不串用、同轮次单提交、`close_voice_turn` 拒绝路径、公开 API 结构锁定（无授权/执行面） |
| `tests/voice_loop/test_chat_loop.py` | 25 | `VoiceDriverTurnResult` 不变量与 P6 播报规则交叉校验、完整路径 6 次迁移、驱动拒绝/异常、闸门拒绝不播报、speaker 失败不改会话事实、finish 失败取消流、迟到边界清理、降级中捕获检测、驱动侧播报不重复、调用顺序拒绝、公开 API 结构锁定、适配器身份映射与归一化、取消映射 `AsrFailureCode.CANCELLED` |
| `tests/integration/test_p8_integration.py` | 4 | 真实 P6 `PushToTalkCoordinator` + 离线 fake 设备/Provider：按键轮次使用 P8 身份提交且只提交一次、逐句播报、每轮唯一身份、ASR 启动失败后文字通道仍可用、解码失败不提交不播报 |
| `tests/voice_loop/support.py` | — | 共享假件（记录型会话入口、fake PCM 流、fake speaker）；非 `test_*.py`，不被收集 |

## 6. 验证结果（实际执行）

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q
  → Ran 1177 tests, OK (skipped=3)          # 基线 1107 + 本轮新增 70（66 专项 + 4 集成）
.\.venv\Scripts\python.exe -m unittest discover -s tests/voice_loop -t . -p "test_*.py" -q → Ran 66, OK
.\.venv\Scripts\python.exe -m unittest tests.integration.test_p8_integration -v        → Ran 4, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/asr -t . -p "test_*.py" -q    → Ran 38, OK
.\.venv\Scripts\python.exe -m unittest discover -s tests/integration -t . -p "test_*.py" -q → Ran 16, OK
.\.venv\Scripts\python.exe -m compileall -q app tests tools                             → exit 0
git diff --check                                                                        → exit 0
```

## 7. 只读安全审计（本轮新增面）

- 依赖面：`app/voice_loop/**` 仅导入 `app.agent.adapter`、`app.asr.recovery`、`app.asr.result_gate`、`app.contracts.base`、`app.conversation.manager` 与自身模块；标准库仅 `__future__`、`dataclasses`、`datetime`、`enum`、`typing`、`uuid`。
- 执行面：AST 审计未发现 `open`/`system`/`popen`/`exec`/`eval`/`__import__`/`urlopen`/`socket`/`Thread`/文件删除移动等调用；未导入 `subprocess`/`socket`/`http`/`urllib`/`os`/`pathlib`/`shutil`/`threading`/`ctypes`/`winreg`/`asyncio`/`sounddevice`/`sherpa_onnx`/`websockets`。
- 安全链隔离：`app.voice_loop` 对 `app.safety`、`app.executor`、`app.plans`、`app.auditing`、`app.verification`、`app.tools`、`app.recovery`、`app.retry`、`app.state`、`app.ui` 的引用数 = 0；语音闭环无法创建授权、计划或执行事实。
- 编码：本轮新增/修改文件严格 UTF-8、无 BOM、无 `U+FFFD`；仓库既有 23 个 BOM 文件（`app/tts/**`、`app/evidence/**`、部分 `tests/**`）本轮未改动。

## 8. 退出条件对照（路线图 P8，逐条现状）

| # | 退出条件 | 现状 |
|---|---|---|
| 1 | 闲聊：按键→ASR→Agent→TTS | 离线端到端已通过（真实 P6 管道 + fake 设备/Provider 集成测试）；未做真机麦克风/扬声器实测 |
| 2 | 任务：按键→ASR→Agent→ActionPlan→授权→执行→验证→播报摘要 | **未实现**（批次 4 范围） |
| 3 | ASR/TTS 任一不可用时仍可切换文字，不影响任务事实 | 已在接缝层实现并测试（降级只切通道、不自动切回、不触碰安全事实）；TTS 失败不改会话结果已有测试 |
| 4 | 每轮有独立 session/utterance/request/reply 边界 | 已实现并测试（身份不复用 + 单活动轮次 + 迟到边界不可信） |
| 5 | 说“停”在聊天、授权、执行三种上下文分别符合规则 | **未实现**（批次 5 范围；仅具备 `cancel_turn` 基础原语） |

结论：P8 保持 `in_progress`，不得标记 `complete`。

## 9. 已知风险与遗留

- 播报是“整段回复推入流式会话”而非“生成中逐句推送”：`ChatVoiceLoop` 目前只在 `driver.release()` 返回完整回复后推送全文（P7 会话内部再分句）。P6 自带的 `drive_turn_stream` 增量播报路径未被闭环采用（`PushToTalkVoiceDriver.speech_handled_by_driver=False`），因此“首音延迟”未优化。
- `PushToTalkVoiceDriver` 依赖 P6 协调器的 `start/pump/release/runtime_failed` 四个方法（鸭子类型）；P6 若改名会在此处暴露为 TypeError。
- 语音提交由 P6 管道完成、由 `VoiceInputSeam.close_voice_turn` 回收边界；“单次提交”由 `ResultGate` + 边界活动性共同保证，而非接缝单点保证（文字通道仍由接缝单点保证）。
- 任务路径状态（planning/awaiting_authorization/executing/verifying）已在状态机命名但无入边迁移；调用即 `VoiceLoopError`（有意 fail closed）。
- P7 遗留的 51 项工作区改动仍未提交，P8 新文件亦未跟踪；本轮未做任何提交动作。

## 10. 下一阶段入口

- **P8 批次 4：任务路径编排**（建议新建 `app/runtime/orchestration/`）：
  `RouteHandoff(task|dangerous|query)` → 只读工具/计划预览 → `PolicyEngine` → L2/L3 授权交互 → `ExecutionPreflightGate` → `ExecutionLedger` → `LocalExecutionBackend` → `ResultVerifier` → 摘要播报；
  仅新增编排与语音表达接线，**不得修改 P5 授权语义**，不得新增工具、不得开放任意命令。
- **P8 批次 5**：“停”的三语境（聊天：停播报继续监听；授权：取消确认；执行：协作取消/不可中断等待）+ 迟到事件隔离。
- **P8 批次 6**：阶段级集成验收（覆盖 P5/P6/P7 前置阶段）+ P8 handoff + 文档收口。

## 11. 下一批次禁止提前做的事项

- 不得进入批次 5、6 或 P9（VAD 连续对话、唤醒词、声纹、AEC）。
- 不得接入真实 DeepSeek/Laya Provider 或任何网络请求、不得读取密钥、不得默认切换语音模式。
- 不得绕过 `PathGuard`/`PolicyEngine`/授权/`ExecutionLedger` 执行任何电脑副作用。
- 不得降低授权等级、不得复用旧授权、不得把语音识别成功当作确认。
- 不得修改 P5 语义、不得改动 `app/safety`、`app/executor`、`app/plans` 的既有约束。

## 12. 需要读取的最小文件集合（下一批次）

1. 本交接文件；
2. `implementation_plan/implementation-roadmap.md` P8 节；
3. `implementation_plan/08-text-end-to-end.md`（任务链安全顺序）；
4. `implementation_plan/shared/action-plan.md`、`shared/safety-boundaries.md`、`07-confirmation-and-stop.md`；
5. `app/voice_loop/`（本批新增 5 个模块）；
6. `app/safety/preflight.py`、`app/executor/controlled.py`、`app/plans/action_plan.py`、`tests/integration/test_p5_integration.py`（既有安全链的真实用法）。
