# P7-R-02 pure IntentRouter 接口批次交接

- 日期：2026-09-24
- 项目：`<PROJECT_ROOT>`
- 阶段状态：P0–P6 complete；P7-R-01 complete；P7-R-02 complete；P7-R-03 not_started；P7/TTS 未开始
- 当前停止点：**P7-R-02 完成后停止，下一批次须独立授权/验收；不进入 failure normalizer、ConversationManager 接缝、Provider、TTS 或 P8**
- 上一交接：`2026-09-24-P7-R-01-route-handoff-schema-version-handoff.md`

## 唯一目标与实际修改

仅基于 P7-R-01 数据契约增加 provider-neutral、无副作用的 `IntentRouter` 输入/输出接口，不接入业务入口、失败规范化或执行链。

- `app/routing.py`：新增冻结的 `RouteInput` 数据对象和结构化 `IntentRouter` `Protocol`。
  - `RouteInput` 只接受非空 `raw_text`、已校验的 `RouteTrace`，并强制二者完全一致。
  - `RouteInput` 不生成、替换或重写 `request_id`、`conversation_id`、`turn_id`，也不创建新的会话或轮次标识。
  - `IntentRouter.route(request)` 的返回类型固定为已校验的 `RouteHandoff`；接口本身不包含授权、执行、工具调用、Provider 启动或异常规范化职责。
- `tests/routing/__init__.py`：使专项测试目录可被标准 `unittest discover` 导入。
- `tests/routing/test_intent_router.py`：5 个专项测试，覆盖输入不可变、空文本/trace 不一致拒绝、结构化替换实现、无 Provider/执行策略依赖、追踪标识保持不变。
- `task_plan.md`、`progress.md`：同步本批次完成事实和下一停止点。

## 安全边界

- `IntentRouter` 是纯接口，不访问网络、进程、命令、设备、工具注册表、Agent、授权、账本、Executor 或 Verification。
- `RouteInput` 传递入口已创建的业务 trace；本批次不新增 TextShell/ASR 入口，也不实现同一 `turn_id` 的防重或迟到结果处理。
- `RouteHandoff` 仍是路由提示，不是授权或执行资格；任何电脑副作用继续遵守 `Plan → preflight → authorization → ledger → executor → verification → audit`。
- 本批次不实现 provider 异常、超时、非法输出和低置信度的 `ambiguous` 规范化；由后续 P7-R-03 负责。

## 验证与事实

- P7-R-02 专项：`python -m unittest discover -s tests/routing -t . -p test_intent_router.py -q`：**5/5**。
- Contracts 回归：**44/44**。
- Conversation 回归：**19/19**。
- 标准全量 discovery：**580/580**。
- 新增模块未访问真实 Provider、Laya、云端、麦克风或电脑工具；未执行任何电脑副作用。
- 本批次仅使用标准库 `dataclasses`、`typing` 和既有 `app.contracts`；未引入网络、进程、命令、线程或执行链依赖。

## 已知风险及明确延期

- 当前只有接口和输入契约，没有默认路由器实现、真实模型、Provider adapter 或 failure normalizer。
- `IntentRouter` 的具体实现必须在后续批次继续返回经过 `RouteHandoff` 校验的对象；不能把任意 dict 或供应商原始 payload 作为下游结果。
- `ConversationManager` 尚未调用该接口；TextShell、ASR accepted final、trace 创建和统一入口仍留待 P7-R-04。
- 尚未实现同一 `turn_id` 只生效一次、重复/迟到/旧边界结果丢弃规则；不得误认为本接口已提供防重。

## 下一批次入口（本会话不实施）

P7-R-03：实现 failure normalizer，把 Provider 不可用、超时、传输错误、非法响应和低置信度统一规范化为安全的 `ambiguous` `RouteHandoff`；继续保持无真实 Laya、无 ConversationManager 接线、无 TTS/P8 和无电脑副作用。下一批仍需独立授权、专项验证和 handoff。