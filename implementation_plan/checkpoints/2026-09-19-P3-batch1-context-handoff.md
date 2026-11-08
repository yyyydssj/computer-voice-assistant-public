# P3 批次 1 上下文交接：Harness adapter 与 no-tools 验证前停止点

> 交接日期：2026-09-19  
> 项目：`<PROJECT_ROOT>`
> 当前状态：P3 已获授权并开始；基础回归已通过，官方 SDK/runtime 安装在 runtime wheel 下载停滞后中止，仍停在批次 1 的官方 SDK/runtime 验证前。  
> 本文件不是 P3 完成 handoff；不得据此进入 P3 批次 2、主界面接线或 P4。

## 阶段与批次状态

- P0：complete。
- P1：complete。
- P2：complete。
- P3：in_progress。
- P3 批次 1：代码和假 SDK 聚焦测试已完成；官方 SDK/runtime 的安全配置兼容性尚未验证，因此不能标记 complete。

## 本轮已完成

新增文件：

- `app/agent/adapter.py`
- `app/agent/__init__.py`
- `app/agent/p3-no-tools.patch.yml`
- `tests/agent/test_adapter.py`
- `tests/agent/__init__.py`

实现事实：

1. `HarnessAdapter` 构造时只校验配置和安全补丁，不创建模型会话、不启动 SDK runtime。
2. 第一次真实 `submit_chat()` 请求才延迟创建 runtime；后续请求复用同一 runtime，并按 `session_id` 调用 Harness。
3. 使用 P2 的 `RequestSubmissionGate` 保证同一 `request_id` 只能提交一次；传输失败、空响应或流中断均不会释放该 ID，也不会隐式重发。
4. 空文本响应或缺少有效 `finish_reason` 的响应不会标记为完成。
5. 已建立故障分类：`connection`、`rate_limited`、`timeout`、`stream_interrupted`、`empty_response`、`unknown_event`、`schema_error`。
6. 所有故障默认 `retry_allowed_automatically=false`、`alternate_agent_allowed_automatically=false`。
7. `close()` 幂等；已创建 runtime 时负责关闭并清理本地 session ID 集合。
8. 只捕获普通 `Exception`，不吞掉 `KeyboardInterrupt` 和 `SystemExit`。
9. 新增 `p3-no-tools.patch.yml`，静态要求禁用默认 Shell、终端、子进程、MCP、自动重试、持久会话和 DeepSeek 会话日志上传等节点；缺项时在创建 SDK runtime 前拒绝启动。

## 已通过的聚焦测试

上一个实现上下文已运行：

```text
<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest tests.agent.test_adapter -v

Ran 8 tests
OK
```

覆盖：

- 构造适配器不创建 runtime。
- 首次请求延迟创建，后续请求复用 runtime。
- `request_id` 防重复提交。
- no-tools 安全补丁缺少必要禁用项时拒绝启动。
- 空响应、不完整响应、流中断和各类 SDK/传输错误映射。
- 不自动重试、不自动切换其他 Agent。
- 不吞进程控制异常。
- 幂等关闭。

本轮已在可启动项目虚拟环境中复跑：P3 聚焦测试 8/8、全量 unittest 80/80、compileall 通过、42 个 Python 文件严格 UTF-8 且无替换字符。

## 尚未执行的验证

- 官方 SDK 实际导入与构造参数兼容测试。
- 官方 Harness runtime 初始化测试。
- no-tools profile 实际暴露工具列表为空的证明。
- 无自动重试、无持久历史、无会话日志上传的 runtime 级证明。
- 真实 API 请求测试。
- 官方 SDK/runtime 安装：上次下载停滞，需在下一轮从安装状态复核后重试；仍只能安装 `deepseek-harness-sdk==0.1.5rc1`。

## 官方 SDK 核验记录

此前已核验的官方来源与版本：

- 权威仓库：`deepseek-ai/deepseek-harness`
- 固定核验提交：`ddefc45fbc7f8e46dd73185e68295696d1297887`
- 官方 Python 包：`deepseek-harness-sdk==0.1.5rc1`
- 对应 runtime 依赖：`deepseek-harness-runtime-bin==0.1.5rc1`

重要：PyPI 的 `deepseek-harness==0.3.1` 是第三方同名项目，不是本方案要求的官方 SDK，绝对不要误装。

当前项目虚拟环境安装状态：

```text
deepseek-harness-sdk=NOT_INSTALLED
deepseek-harness-runtime-bin=NOT_INSTALLED
deepseek-harness=NOT_INSTALLED
```

本轮仅尝试安装 `deepseek-harness-sdk==0.1.5rc1`；下载对应 runtime wheel 至约 27.5 MiB 后长时间无增长，已中止安装。没有安装第三方同名包，没有启动 Harness，没有读取或输出 `.env` 中的真实 API key，也没有发送真实 API 请求。

## 安全决策与未决风险

官方 `sdk-minimal` 默认配置仍包含可能扩大 P3 权限或产生持久副作用的节点，包括 danger-full-access、持久 Bash/PowerShell、terminal/subprocess、JSONL 会话持久化和 DeepSeek session-log 上传。它与 P3 的“纯文字、无电脑工具、不自动重试”边界冲突。

因此当前实现要求：

- 必须加载 `app/agent/p3-no-tools.patch.yml`。
- adapter 创建 runtime 前必须验证必要禁用项全部存在。
- 安全补丁缺失、不完整或不可严格 UTF-8 读取时必须失败关闭。
- 不允许静默退回官方默认 `sdk-minimal` 行为。

尚未解决的关键风险：当前 patch 只经过静态检查和假 SDK 测试。禁用 `jobs`、sandbox 等节点后，官方 runtime 中某些 Agent 节点可能处于 PENDING 或初始化超时。没有完成真实离线初始化验证前，不得接入 `app/main.py`，也不得把批次 1 标记 complete。

## 下一次唯一入口

1. 读取本文件、`implementation_plan/checkpoints/P2-handoff.md`、`progress.md` 最新 P3 记录和路线图 P3 卡片。
2. 复核：
   - `app/agent/adapter.py`
   - `app/agent/p3-no-tools.patch.yml`
   - `tests/agent/test_adapter.py`
3. 检查当前文件实际内容，确认此前两次因换行锚点失败的替换没有留下半写入或重复片段。
4. 上述基础验证已完成；下一轮先复核 pip/虚拟环境安装状态，再只重试官方 SDK 安装，不重复安装第三方同名包。
5. 安装前再次记录准确依赖版本；如需联网安装，只安装 `deepseek-harness-sdk==0.1.5rc1`，必要时使用已验证可用的下载/超时策略；不得安装第三方同名包。
6. 安装后先做离线导入和配置对象/构造参数兼容验证，不发送真实用户文本。
7. 使用项目数据目录之外的隔离 `dsh_home` 验证 no-tools profile 能否初始化。
8. 必须用 runtime 可观察事实证明：暴露工具列表为空、无自动重试、无持久历史、无会话日志上传。
9. 上述条件全部满足后，才可把 P3 批次 1 标记 complete 并进入批次 2：闲聊会话容器、工作会话占位、30 分钟闲聊闲置销毁、工作/闲聊上下文隔离。

## 严格禁止

- 不接电脑工具。
- 不生成或执行 `ActionPlan`。
- 不接 ASR、TTS 或其他语音模块。
- 不自动重试。
- 不自动切换 CLI、Codex、Claude Code 或其他 Agent。
- 不把不完整回复写入正式历史。
- 不读取、输出或记录 `.env` 中真实 API key。
- 官方 runtime 的 no-tools 配置通过验证前，不接 `app/main.py`。
- 不进入 P4。
- 不执行 Git add、commit 或 push，除非用户另行明确要求。

## Git 与工作区说明

- 当前分支：`main`。
- 仓库尚无首次 commit；现有项目文件仍显示为 untracked。
- 本轮没有 add、commit、push，也没有修改项目目录外文件。
