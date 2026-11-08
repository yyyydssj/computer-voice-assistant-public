# P3 批次 1 官方 runtime 验证完成交接

> 交接日期：2026-09-19  
> 项目：`<PROJECT_ROOT>`
> 阶段：P3 文字聊天与 Harness 会话  
> 状态：`P3 batch 1 complete`；已停在 P3 批次 2 授权门前

## 本次目标

严格接续 P3 批次 1：对已安装的固定版本官方 SDK/runtime 完成离线兼容检查、隔离初始化和 runtime 级 no-tools 安全事实验证；不得进入批次 2。

## 已完成

- 使用项目内 Python.org 官方 3.12.10 嵌入式解释器 `.python312\python.exe` 离线导入成功。
- 安装版本确认：
  - `deepseek-harness-sdk==0.1.5rc1`；
  - `deepseek-harness-runtime-bin==0.1.5rc1`。
- `DeepSeekHarnessConfig` 明确接受 `app/agent/adapter.py` 第 274–285 行传入的全部参数；无需修改 adapter 或测试。
- 使用独立临时 `dsh_home`、`sdk-minimal` profile 和既有 `p3-no-tools.patch.yml` 完成本地 JSON-RPC 初始化；runtime 成功启动和关闭。
- 初始化验证显式移除了子进程环境中的 `DEEPSEEK_API_KEY` 与 `DEEPSEEK_BASE_URL`；未读取 `.env`，未发送真实用户文本或真实 API 请求。
- `--dump-config` 证明补丁中 14 个实际存在的安全目标均为 `disabled: true`：会话日志、插件清单、sandbox、sandbox-policy、subprocess、pty、bash/pwsh terminal、session-title、llm-retry、jobs、persistent bash/pwsh、sessions。
- `mcp-resources` 在固定版本 `sdk-minimal` 默认树中不存在，runtime 对该冗余禁用项输出警告但不阻止组合或初始化。该安全声明保留，未放宽 patch。
- 本机临时 HTTP 捕获器完成两类离线探针：
  - 成功流式响应：只收到 1 次 `/chat/completions` 请求，请求体无 `tools` 字段，轮次 `finish_reason=completed`；
  - 429 响应：只收到 1 次 `/chat/completions` 请求，请求体无 `tools` 字段，轮次 `finish_reason=error`，没有自动重试或 Provider 切换。
- 成功轮次关闭后，临时 `dsh_home` 的 profile 目录之外仅有 `.anonymous-user-id`；没有 `sessions/`、会话数据或日志文件。捕获器没有收到模型请求之外的上传请求。
- 新鲜回归全部通过：
  - adapter：8/8；
  - 全量 unittest：80/80；
  - `compileall`：通过；
  - 严格 UTF-8：42 个 Python 文件通过，无替换字符。

## 可观察退出事实

| 要求 | 证据 | 结果 |
|---|---|---|
| 官方 SDK/runtime 可导入且版本固定 | 两个 distribution 均为 `0.1.5rc1` | 通过 |
| adapter 构造参数兼容 | `DeepSeekHarnessConfig` 字段逐项匹配第 274–285 行 | 通过 |
| 工具列表为空 | 成功与 429 捕获请求均无 `tools` 字段 | 通过 |
| 无自动重试 | 429 探针 HTTP 请求计数为 1 | 通过 |
| 无持久历史 | 成功轮次后无 profile 外 session 文件或 `sessions/` 目录 | 通过 |
| 无会话日志上传 | 无日志文件，捕获器无额外上传请求 | 通过 |
| 无业务回归 | 8/8、80/80、compileall、UTF-8=42 | 通过 |

## 本次未修改

- 没有修改 `app/agent/`、`tests/agent/` 或其他业务代码。
- 没有修改 `app/main.py`、`app/conversation/`、`app/persona/`。
- 没有实现闲聊/工作会话容器、30 分钟闲置销毁、流式拼接、persona、工具、ActionPlan、ASR、TTS 或 P4。
- 没有执行 `git add`、`git commit` 或 `git push`。

## 运行与测试注意事项

- 官方嵌入式 Python 的 `._pth` 隔离不会自动把项目根目录加入 `sys.path`。直接执行 `python -m unittest ...` 会出现 `ModuleNotFoundError: app`；本次通过测试进程内临时插入项目根目录运行，未修改解释器配置。
- `sdk-minimal --dump-config` 对不存在的 `mcp-resources` 禁用项会写 stderr 警告但返回 0；最终组合中不存在该条目，其余安全目标均明确禁用。
- 临时 runtime 验证目录位于系统临时目录，不属于业务源码；`.python312\`、`.wheelhouse\`、`.python312-embed.zip`、`.get-pip.py` 也仍是本地环境产物，不得提交。

## 当前停止点

P3 批次 1 已完成。**不得自动进入 P3 批次 2。** 下一步只有在用户明确授权后，才能实现：

1. 闲聊会话容器；
2. 工作会话占位；
3. 两类上下文隔离；
4. 30 分钟闲聊闲置销毁。

批次 2 开始前仍须遵守先测试、后最小实现，以及 P3 不暴露工具、不生成/执行 ActionPlan、不接语音、不自动重试或切换 Agent 的冻结边界。

## Git 状态

- 当前分支：`main`。
- 仓库仍无首次 commit，项目文件处于 untracked 基线。
- 本次只更新阶段计划、进度和本交接文档；提交前仍须让用户审阅范围。