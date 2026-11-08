# P3 批次 3 request、流式拼接与历史完整性完成交接

> 交接日期：2026-09-19  
> 项目：`<PROJECT_ROOT>`
> 阶段：P3 文字聊天与 Harness 会话  
> 状态：`P3 batch 3 complete`；已停在下一批次授权门前

## 本次授权范围

本次用户已明确确认进入 P3 批次 3，仅实现以下边界：

1. 会话层缺省生成唯一 `request_id`；
2. 流式文本片段按接收顺序拼接；
3. 只有非空文本且存在有效 `finish_reason` 时才判定为完整；
4. 不完整、空响应、schema 错误或流中断结果不得进入正式历史。

未授权范围仍保持冻结：persona、主界面接线、工具、ActionPlan、ASR、TTS、语音链路和 P4。

## 已完成

- `app/conversation/manager.py`：
  - 新增 `AgentStreamChunk`，表示文本片段、终止原因和可选故障；
  - 新增 `ConversationTurn`，作为已确认完整、可进入正式历史的轮次记录；
  - `Conversation` 新增本地 `history`，只追加完整轮次；
  - `send_chat()` / `send_work()` 的 `request_id` 改为可选，缺省时按会话类型生成 `chat-request-*` 或 `work-request-*`；
  - 支持既有 `AgentTurnOutcome` 和 `Iterable[AgentStreamChunk]` 两种 adapter 返回形态；
  - 流式片段按顺序拼接；
  - 缺少终止原因、空文本、非法片段、迭代异常均返回未完成结果；
  - 只有完整且非空的结果才写入 `Conversation.history`；
  - 未增加自动重试，仍复用 adapter 传入的单一 `request_id`。
- `app/conversation/__init__.py`：公开 `AgentStreamChunk` 和 `ConversationTurn`。
- `tests/conversation/test_manager.py`：新增批次 3 聚焦测试。

## 可观察退出事实

| 要求 | 证据 | 结果 |
|---|---|---|
| 缺省 request_id 唯一生成 | 聚焦测试和 smoke test 均验证连续两轮 request_id 不同 | 通过 |
| request_id 传递给 adapter | 测试验证 adapter 收到的 request_id 与 outcome 一致 | 通过 |
| 流式片段顺序拼接 | `第一段` + `第二段` 形成 `第一段第二段` | 通过 |
| 完整输出进入正式历史 | 完整、非空且有 `finish_reason` 的轮次生成一条 `ConversationTurn` | 通过 |
| 不完整输出不进入正式历史 | 缺少终止片段的结果 history 为空 | 通过 |
| 空完成流不进入正式历史 | 空文本即使带 `finish_reason=completed` 也不写入 history | 通过 |
| 既有非流式 outcome 兼容 | 原有 FakeAdapter 测试继续通过 | 通过 |
| 不自动重试 | 原有失败测试仍验证 adapter 调用次数为 1 | 通过 |

## 测试结果

- `tests.conversation.test_manager`：17/17 通过。
- 全量 unittest：97/97 通过。
- `python -m compileall -q app tests`：通过。
- 严格 UTF-8：46 个 Python 文件通过，无替换字符。
- request_id/history 无联网 smoke test：通过。

## 本次未修改或未实现

- 没有修改 `app/agent/adapter.py`、`app/agent/p3-no-tools.patch.yml` 或 Harness 安全边界。
- 没有启动官方 runtime，没有发送真实 API 请求，没有读取或输出 `.env`。
- 没有接入 `app/main.py`、persona、主界面、工具、ActionPlan、ASR、TTS、语音链路或 P4。
- 没有实现后台定时器、持久化历史、跨进程恢复或 UI 展示。
- 没有执行 `git add`、`git commit` 或 `git push`。
- 一次单行 Python smoke-test 命令因把 `@dataclass` 放在 `-c` 单行中而语法失败；该命令未执行项目代码，随后改用临时脚本验证并通过。

## 设计与兼容注意事项

- `Conversation.history` 是当前 P3 会话容器内存中的正式历史，不等同于持久化存储。
- 不完整输出可以作为返回值中的预览文本存在，但不会进入 `Conversation.history`。
- `finish_reason` 只要是非空字符串即可作为当前批次的完整性终止信号；具体 provider 语义仍由 adapter 负责映射。
- 现有显式传入 `request_id` 的调用继续兼容；只有省略时由会话层生成。
- 闲聊成功仍刷新 30 分钟闲置计时；失败和不完整结果不刷新最后成功活动时间，延续批次 2 既定约束。

## 当前停止点

P3 批次 3 已完成。**不得自动进入下一批次。**

下一步必须等待用户明确授权后再确定，至少不得自动进入：

- persona 或主界面接线；
- 工具、ActionPlan、授权或执行状态机；
- ASR、TTS、语音链路；
- P4。

如果后续继续 P3，应先由用户明确指定下一批次目标和范围，并继续遵守先测试、后最小实现。

## Git 状态

- 当前分支：`main`。
- 仓库仍无首次 commit；项目文件处于 untracked 基线。
- 本批次未执行 add、commit 或 push；提交前仍须让用户审阅范围。
