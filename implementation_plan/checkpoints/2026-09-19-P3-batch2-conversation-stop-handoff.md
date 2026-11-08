# P3 批次 2 会话容器完成交接

> 交接日期：2026-09-19  
> 项目：`<PROJECT_ROOT>`
> 阶段：P3 文字聊天与 Harness 会话  
> 状态：`P3 batch 2 complete`；已停在 P3 批次 3 授权门前

## 本次目标

严格接续 P3 批次 1，在不接主界面、不发真实 API 请求、不开放工具的前提下，实现闲聊会话容器、工作会话占位、两类上下文隔离和 30 分钟闲聊闲置销毁。

## 已完成

- 新增 `app/conversation/manager.py` 和公开入口 `app/conversation/__init__.py`。
- `ConversationManager` 维护：
  - 一个闲聊会话；
  - 按 `task_id` 索引的工作会话占位；
  - 各会话独立的 `session_id`、创建时间、最后活动时间、状态和关闭原因。
- 创建本地闲聊/工作容器不会调用 adapter，也不会创建 Harness runtime；第一次 `send_chat` 或 `send_work` 才进入批次 1 的延迟 adapter。
- 闲聊与工作会话使用不同 `session_id`；不同 `task_id` 互相隔离，相同且仍开放的 `task_id` 返回同一工作会话。
- 闲聊连续闲置未满 30 分钟不会销毁；达到 30 分钟时：
  - 可由 `expire_idle_chat()` 显式清理；
  - 下一次获取或发送闲聊也会先自动清理；
  - 旧会话标记为 `expired`，关闭原因为 `idle_timeout`；
  - 后续创建新的 `session_id`，不恢复旧上下文。
- 成功闲聊轮次刷新最后活动时间；失败结果不被本地自动重试，也不会生成新的 request。
- 工作会话在完成、取消、中止时关闭，关闭原因分别为 `completed`、`cancelled`、`aborted`。
- 工作轮次收到 Agent 失败结果时关闭，关闭原因为 `agent_failure`；只调用 adapter 一次。
- 已关闭工作任务拒绝继续发送，也拒绝以相同 `task_id` 重新创建或复用。
- `ConversationManager.close()` 幂等：关闭仍开放的本地会话，并只关闭 adapter 一次。

## 可观察退出事实

| 要求 | 证据 | 结果 |
|---|---|---|
| 容器创建不启动 runtime | fake adapter 在只创建闲聊/工作容器后仍为 `runtime_created=False` | 通过 |
| 闲聊与工作上下文隔离 | 两类会话及不同工作任务的 `session_id` 均不同 | 通过 |
| 相同工作任务稳定复用 | 开放状态下相同 `task_id` 返回同一对象 | 通过 |
| 30 分钟前不销毁 | 29 分 59 秒仍复用原闲聊会话 | 通过 |
| 30 分钟时销毁 | 显式清理和下一次访问/发送均淘汰旧会话 | 通过 |
| 销毁后不恢复旧上下文 | 后续闲聊使用新的 `session_id` | 通过 |
| 工作终态关闭 | 完成、取消、中止和 Agent 失败均关闭并记录原因 | 通过 |
| 无自动重试 | Agent 失败测试中 adapter 调用计数为 1，request_id 未改变 | 通过 |
| 无业务回归 | 会话 13/13、全量 93/93、compileall、UTF-8=46 | 通过 |

## 新增文件

- `app/conversation/__init__.py`
- `app/conversation/manager.py`
- `tests/conversation/__init__.py`
- `tests/conversation/test_manager.py`

## 本次未修改或未实现

- 没有修改 `app/agent/adapter.py`、`app/agent/p3-no-tools.patch.yml` 或批次 1 测试。
- 没有修改 `app/main.py`、`app/persona/` 或人物模板。
- 没有实现批次 3 的 `request_id` 生成、流式文本拼接或完整性判定。
- 没有实现工具、ActionPlan、授权、ASR、TTS、语音链路或 P4。
- 没有读取 `.env`，没有启动官方 runtime，没有发送真实 API 请求。
- 没有执行 `git add`、`git commit` 或 `git push`。

## 测试结果

- `tests.conversation.test_manager`：13/13 通过。
- 全量 unittest：93/93 通过。
- `python -m compileall -q app tests`：通过。
- 严格 UTF-8：46 个 Python 文件通过，无替换字符。

## 运行与设计注意事项

- 嵌入式 `.python312` 的 `._pth` 隔离仍不会自动加入项目根目录；测试通过进程内临时插入项目根路径运行，未修改解释器配置。
- 会话容器只保存本地生命周期元数据；Harness 上下文隔离依赖传入不同 `session_id`。
- 工作会话当前只是批次 2 占位，不包含计划、授权、工具或执行状态机。
- 当前没有后台定时器；30 分钟淘汰由显式清理调用或下一次闲聊访问/发送触发，均保证过期上下文不会继续使用。
- 闲聊失败不会刷新最后成功活动时间；是否把失败尝试算作活动不在批次 2 既定范围内，不得无授权改变。

## 当前停止点

P3 批次 2 已完成。**不得自动进入 P3 批次 3。** 下一步只有在用户明确授权后，才能实现：

1. `request_id` 生成；
2. 流式文本拼接；
3. 完整性判定；
4. 不完整输出不进入正式历史所需的批次 3 边界。

批次 3 仍须遵守先测试、后最小实现；不得提前进入 persona、主界面接线、工具、ActionPlan、ASR、TTS 或 P4。

## Git 状态

- 当前分支：`main`。
- 仓库仍无首次 commit，项目文件处于 untracked 基线。
- 本批次未执行 add、commit 或 push；提交前仍须让用户审阅范围。