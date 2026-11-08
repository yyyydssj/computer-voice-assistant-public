# P7-R-12 DeepSeek 脱敏原始输出 fixture 契约交接

- 日期：2026-09-25
- 项目：`<PROJECT_ROOT>`
- 阶段状态：**implementation complete；真实 Provider-specific adapter 仍 NOT READY / STOP**
- 上一交接：`implementation_plan/checkpoints/2026-09-25-P7-R-11-deepseek-raw-fixture-contract-handoff.md`

## 本批唯一目标

在不联网、不读取密钥、不启动 DeepSeek SDK/Laya、不接入默认运行时、不进入 TTS/P8/ASR 设备或电脑副作用链的前提下，继续扩展脱敏、离线的 DeepSeek 原始输出边界契约；不改变既有解析实现，不宣称 fixture 来自真实模型。

## 实际变更

- `tests/routing/test_deepseek_protocol.py`
  - 新增转义 Unicode 与 JSON 字符串内换行/引号的保真测试，确认外围空白允许但字符串内容不被改写；
  - 新增 `unavailable`、`transport_error`、`invalid_response` 三类严格失败 envelope 的逐项接受测试；
  - 新增重复解析结果为全新嵌套 mapping 的测试，确认 fixture 解析不会共享可变对象；
  - 新增错误文本隔离测试，确认包含脱敏 debug/candidate 内容的非法 fixture 不会把内容带入 `ValueError`。
- `progress.md`、`task_plan.md`
  - 记录本批授权范围、测试结果和下一停止点。
- 未修改 `app/routing.py`，未修改默认运行时或任何真实 Provider 接线。

## 验证事实

- DeepSeek 协议专项：**23/23** 通过。
- Routing 专项：**55/55** 通过。
- 标准全量 discovery：**630/630** 通过。
- `compileall app tests`：通过。
- `app/` 与 `tests/` Python 文件严格 UTF-8 解码：通过，未发现 `U+FFFD`。
- `app/routing.py` 与 DeepSeek 测试执行面审计：通过，无网络、socket、HTTP client、subprocess、进程/线程启动或电脑副作用调用。
- 本批只增加离线 unittest fixture；没有网络、SDK、进程、线程、密钥或电脑副作用依赖。

## 执行面审计与安全边界

- 未读取 `.env`、`DEEPSEEK_API_KEY` 或任何凭据。
- 未发起网络请求，未启动真实 Provider/Laya 服务，未调用 SDK。
- 未修改默认 `app.main.run()`；`provider_ready=false` 保持不变。
- 未接入 `ConversationManager`、ToolRegistry、ExecutionLedger、Executor、Verification、Audit、TTS、P8 或 ASR 设备。
- 未执行电脑副作用；fixture 只能证明离线契约边界，不能产生授权或执行资格。
- 仍必须遵守 `Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit`。

## 当前结论与下一入口

本批继续证明：脱敏的 DeepSeek 原始输出边界能够保留合法 JSON 字符串内容、逐项接受严格失败 envelope，并在解析结果与错误边界上保持隔离；该证据不证明真实模型稳定输出，也不授权实现或启用真实 Provider-specific adapter。

下一批若要继续，必须另行授权，优先选择：

1. 继续仅做脱敏离线 fixture/契约扩展；或
2. 单独授权设计默认关闭的 DeepSeek provider-specific adapter，并先补齐超时、重试、成本、出站域名、日志脱敏和删除/禁用回滚测试。

在新授权前不得联网、读密钥、启动 DeepSeek、接入默认运行时或进入电脑副作用链。