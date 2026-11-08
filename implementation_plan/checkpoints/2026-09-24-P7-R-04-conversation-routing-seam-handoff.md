# P7-R-04 ConversationManager 文字/语音接缝批次交接

- 日期：2026-09-24
- 项目：`<PROJECT_ROOT>`
- 阶段状态：P0–P6 complete；P7-R-01 complete；P7-R-02 complete；P7-R-03 complete；P7-R-04 complete；P7-R-05 not_started；P7/TTS 未开始
- 当前停止点：**P7-R-04 完成后停止，下一批次须独立授权/验收；不进入真实 Provider/Laya、TTS、P8 或任何电脑副作用**
- 上一交接：`2026-09-24-P7-R-03-failure-normalizer-handoff.md`

## 唯一目标与实际修改

本批次只实现 TextShell 与 ASR accepted final 到 `ConversationManager -> IntentRouter -> RouteHandoff` 的统一接缝；不实现真实 Provider，不把路由提示当成授权或执行资格。

- `app/conversation/manager.py`
  - 新增 `ConversationRouteResult`，统一承载已校验 `RouteHandoff` 和可选 Agent outcome。
  - 新增 `submit_text(...)` 与 `submit_asr_final(...)`。
  - 入口创建 `RouteTrace`，复用当前聊天会话 `session_id` 作为 `conversation_id`，保留 `request_id`、`turn_id`、来源、原话和创建时间。
  - 调用纯 `IntentRouter`；输出非 `RouteHandoff`、trace 身份不一致或异常时，统一经 `normalize_router_failure(...)` 进入安全 `ambiguous`。
  - 同一 `turn_id` 的相同来源/文本/请求身份重复提交返回既有结果；试图替换文本或请求身份拒绝。
  - `ambiguous` 不调用 Agent；`chat` 才进入现有对话适配器路径。
  - 未配置 Router 时保留既有 `send_chat` 行为，供 P6 兼容路径使用。
- `app/conversation/__init__.py`：公开 `ConversationRouteResult`。
- `app/asr/push_to_talk.py`：仅在 ResultGate `ACCEPTED_FINAL` 时调用 `submit_asr_final(...)`；未配置 Router 时保持 P6 旧路径；文字 fallback 同样保留兼容路径。
- `app/ui/text_shell.py`：新增 `run_conversation(...)`，把文字输入交给统一路由入口。
- `tests/conversation/test_manager.py`：新增路由入口、trace 身份、turn 防重、ASR 来源与 ambiguous 不进 Agent 覆盖。
- `task_plan.md`、`progress.md`：同步本批次事实和下一停止点。

## 安全边界

- TextShell 与 ASR accepted final 使用同一 `ConversationManager` 路由契约；`source` 只记录来源，不改变风险、授权、执行要求。
- ASR interim、空 final、重复/迟到/旧边界事件仍由既有 ResultGate 拒绝，不创建 RouteTrace，不调用 Router，不创建 Agent 请求。
- `ambiguous` 结果固定留在澄清边界，不调用 Agent、ToolRegistry、ActionPlan、Safety、AuthorizationSnapshot、ExecutionLedger、Executor、Verification 或 Audit。
- `RouteHandoff` 仍只是路由提示；电脑副作用继续遵守 `Plan → preflight → authorization → ledger → executor → verification → audit`。
- Router 异常正文不进入路由契约；异常仅按 `timeout`、`transport_error` 或 `invalid_response` 枚举化规范化。
- 未配置 Router 的兼容路径仅为前置 P6 回归保留，不代表真实 Provider 已接入，也不绕过未来启用 Router 时的路由边界。
- 本批次无网络、API、进程、命令、线程、设备、工具注册表、授权、账本、执行、验证或审计持久化调用。

## 验证与事实

- Conversation 回归：`python -m unittest discover -s tests/conversation -t . -q`：**19/19**。
- Integration 回归：`python -m unittest discover -s tests/integration -t . -q`：**12/12**。
- 标准全量 discovery：`python -m unittest discover -s tests -t . -q`：**585/585**。
- 修改 Python 文件严格 UTF-8、AST/py_compile 检查通过，无 `U+FFFD`。
- 未访问真实 Laya、云端 Provider、麦克风或电脑工具；未执行电脑副作用。

## 已知风险及明确延期

- 当前仍没有默认 IntentRouter 实现、真实模型或 Provider adapter；路由接缝只接受外部注入的纯接口实现。
- `TextShell.run_conversation(...)` 需要调用方显式提供已配置 Router 的 `ConversationManager`；现有 `app.main.run()` 仍保持 P1 的诚实 unavailable 壳，不自动接入未配置 Agent。
- `turn_id` 结果缓存当前属于单个 `ConversationManager` 内存边界；不负责跨进程、持久化或多控制器协调。
- 本批次未实现用户澄清后的受控重路由、slot 确认、Agent/ActionPlan/Safety 下游，也未修改现有 P5/P6 授权与执行链。

## 下一批次入口

P7-R-05：Provider adapter 与契约测试。目标应限定为实现一个可替换、provider-neutral 的 Router adapter 边界，覆盖合法 RouteHandoff、超时、不可用、非法响应、低置信度、原始 payload 隔离及无网络单元测试；必须另行授权。不得在未获新授权前连接真实 Laya、网络/API、TTS、P8 或电脑副作用。