# P7-R-24 / R-25 / R-26 真实路由前置门（2026-09-28）

结论：**R-24 已完成决策审查，NOT READY / STOP；R-25、R-26 阻塞，未发起真实请求**。R 支线不阻断 TTS 的本地测试，但不代表 DeepSeek 真实路由已就绪。

- 源码/历史：R-23 handoff、`app/providers/deepseek_http_transport.py`、`app/providers/provider_integration.py`、`app/main.py` 和 `app/routing.py`。本轮未读取任何密钥值；只检查了环境变量**名称**，没有看到可供本批隔离实测的授权配置。没有改变默认 local Provider、启动状态、Agent、工具链或系统行为。
- 待核对的官方文档入口（本轮不进行真实 API 测试，也不冻结动态版本/价格）：DeepSeek [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/) 提供 `/chat/completions`；[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) 为动态计费表。模型别名、可用模型与价格必须在真正探测之前对照当天官方文档和账户配置核实；不能沿用历史模型标识或旧价格作为验证预算。不能把既有离线 `deepseek-route/v1` 契约推断成真实模型稳定输出；既有 R-08 的一次真实文本请求也不证明本批协议。
- 拟定隔离探测的**上限而非执行事实**：仅官方 `api.deepseek.com` 的 HTTPS chat completions，非敏感固定文本，无工具调用/无电脑副作用；总请求不超过 3（包括任何重试，默认不重试），单次网络超时不超过 15 秒，明确 `max_tokens`，消费前必须确认当前定价与余额并封顶；只记录是否结构合法、耗时、错误类型、匿名诊断 ID，不记录请求正文/密钥/原始响应；失败即回到 local、`runtime_ready=false`。不启动默认运行时。
- 缺失条件：选定当前有效模型和兼容性、额度/货币成本上限、经脱敏的实际凭据可用性及付费账户约束、实测命令与泄露边界、真实输出是否符合 `deepseek-route/v1`。这些必须先落实，不能因用户概括性允许就假设凭据/预算存在；本轮实际网络 Provider 请求为 **0**，费用 **0**。
- R-25 因 R-24 门未通过而**阻塞**（未用离线测试代替实测）；R-26 因 R-25 没有真实证据而**阻塞**，不实施路由到 runtime 的接线。重启条件：核定上述条件后有界验证、评估真实输出和失败，再单独审查运行时映射、低置信 fail-closed 与配置回滚。P8 不在范围。