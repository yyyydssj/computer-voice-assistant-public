# P7-R-06 离线 Provider schema fixture / 契约扩展交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 状态：**complete**；本批完成后停止，下一批须独立授权
- 上一交接：`2026-09-25-P7-R-05-provider-adapter-handoff.md`

## 本批唯一目标

只在无网络 stub 边界扩展 Provider 响应 schema 的契约测试，并修复测试证实的 adapter 缺口。不连接真实 Laya、模型或网络；不进入 TTS、P8 或电脑副作用链。

## 变更

- `tests/routing/test_provider_schema_fixtures.py`：新增 5 个离线测试方法，分别覆盖 chat/query/task 成功 fixture、payload 返回后的隔离、恶意/非法 schema 的安全降级、低置信度清空候选，以及 Provider 直接返回失败 `RouteHandoff` 时的规范化。恶意 fixture 包括未知字段、route/intent 不匹配、布尔/非有限置信度、危险工具与错误数组类型、已确认 slot、不合法状态和附带调试字段的失败响应。
- `app/routing.py`：直接对象分支在核对 trace 身份和 adapter 元数据后，对非 success handoff 统一调用既有 `_failure(...)` / `normalize_router_failure(...)`；只保留 low_confidence 分数，清除失败对象的 slots、tool_scope 和候选 payload。成功对象分支不变。
- 未修改 `ConversationManager`、默认运行时、ASR、TTS、P8 或安全执行链。

## 验证事实

- 修复前红测：直接失败 handoff 的 timeout、low_confidence 两种 case 因保留 `content` slot 失败；其他新测试通过。
- 修复后 routing 专项：**26/26** 通过；标准全量 discovery：**601/601** 通过。
- 两个变更的 Python 文件严格 UTF-8 解码、`py_compile` 通过，未发现 U+FFFD。
- 未调用真实 Provider/Laya、网络 API、麦克风、TTS 或电脑工具。

## 边界与下一入口

- 本批 fixture 是 provider-neutral 的合成数据，不代表已验证任何真实 Laya 意图模型的响应格式；未修改 `timeout_seconds` 的责任边界。
- `RouteHandoff` 始终只是未受信路由提示，不是授权或执行资格；失败交接固定 `ambiguous/clarify`、空 slots 和空 tool_scope。
- 下一批如需研究真实 Provider 的 schema，先明确模型、响应样例、密钥及网络权限边界并取得独立授权；未经授权不得连接真实 Provider、TTS、P8 或电脑副作用。