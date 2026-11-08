# P4 批次 2 只读工具完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P4 结构化工具、路径安全、策略和 ActionPlan  
> 状态：`P4 batch 2 complete`；已停在 P4 批次 3 门前

## 本批次唯一目标

在不实现 PathGuard、PolicyEngine、ActionPlan、写工具或执行器的前提下，完成四个只读工具的结构化行为与故障注入：

1. `list_directory`
2. `read_text_file`
3. `search_files`
4. `get_processes`

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P4-batch1-tool-manifest-stop-handoff.md`；用户明确要求直接进入下一批次，无须再次授权；
- 用户给出的路径包含不存在的 `implementation\_plan`，同名文件实际位于 `implementation_plan`，按唯一匹配文件接续；
- 本轮按用户要求在上下文约 60% 时更新最新交接并停止，不进入 P4 批次 3；
- 未改变既定 MVP 工具清单、风险等级、路径范围或阶段顺序。

## 已完成

### 只读工具模块

新增 `app/tools/read_only.py`，提供：

- `ReadOnlyToolError`：稳定错误码与结构化错误信息；
- `list_directory`：名称/大小/修改时间排序，隐藏项过滤，递归和深度上限，最多 200 项，超限使用 `truncated`；
- `read_text_file`：UTF-8 优先、GBK 回退，空文件支持，二进制拒绝，单文件大小上限，行范围和 UTF-8 输出字节上限，统一换行，`truncated` 标记；
- `search_files`：名称、扩展名、普通文本三种模式，MVP 不支持正则，递归深度最多 5 层，最多 100 项，内容搜索拒绝二进制和超大文件，超时/输出上限结构化处理；
- `get_processes`：受限进程摘要、名称过滤、最多 200 项、进程退出竞态容忍、权限失败结构化返回；
- `ProcessSnapshot` 仅作为内部提供方快照，公开输出只允许 PID、名称、受限资源摘要、可执行文件名摘要和启动时间。

### 安全边界

- 所有路径工具都必须显式接收 `path_validator`；缺失时直接 `TypeError`，拒绝形成未校验文件访问通道；
- 路径校验拒绝结果会保留结构化 `path_out_of_scope` 等错误，不在本模块内绕过；
- 搜索发现的每个文件会再次调用路径校验接口，链接、越界或竞态失败项不会直接进入结果；
- 不暴露命令行、环境变量、完整用户信息或任意进程终止入口；
- 本模块没有写入、移动、删除、启动应用、PowerShell、Shell、Python、外部命令或动态注册入口。

### 公开入口

更新 `app/tools/__init__.py`，公开四个只读函数、结果类型、快照类型和错误类型；不改变 13 项固定 ToolRegistry。

## TDD 与故障注入

新增 `tests/tools/test_read_only.py`，覆盖：

- 正常目录、隐藏项、排序、空目录/结果；
- 递归深度与数量上限；
- 权限不足；
- 必传路径校验接口与路径越界占位拒绝；
- UTF-8、GBK、空文件、行范围和换行；
- 二进制文件、超大文件、输出字节上限；
- 文件名、扩展名、普通文本搜索；
- 搜索数量/深度/超时限制；
- 进程过滤、数量上限、退出竞态、权限失败、敏感字段过滤和超时。

首次实现后红测定位并修复两项缺口：

1. 递归深度边界错误地把超出层级的目录纳入结果；
2. Windows `CRLF` 未统一为结构化结果约定的 `LF`。

## 验证结果

- 只读工具专项：`10/10` 通过；
- 全量 unittest：`127/127` 通过；
- `compileall`：使用可写工作区的 `PYTHONPYCACHEPREFIX` 通过；
- 严格 UTF-8：`57` 个 Python 源文件通过，无替换字符；
- 公开入口 smoke：四个只读函数均可从 `app.tools` 导入；
- 固定 catalog smoke：仍为 13 个工具，顺序与 P4 批次 1 一致；
- 真实本机只读 smoke：`get_processes()` 返回最多 200 个摘要，公开字段仅为 `name`、`pid`（默认 `include_details=False`）；
- 未读取 `.env`，未启动官方 Harness runtime，未发送真实 API 请求。

### 环境权限记录

项目目录既有 `__pycache__` 目录由当前沙箱身份不可写，直接在项目根运行 `compileall` 会出现 `WinError 5`；这不是语法错误。改用可写工作区目录作为 `PYTHONPYCACHEPREFIX` 后，`compileall` 成功。未删除、移动或修改既有缓存目录。

## 新增/修改文件

- `app/tools/read_only.py`
- `app/tools/__init__.py`
- `tests/tools/test_read_only.py`
- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`
- 本交接文档

## 本批次明确未做

- 未实现 `get_file_info` 或 `get_system_info`；
- 未实现任何写工具、写计划、预览执行、授权弹窗或执行器；
- 未实现 PathGuard 的真实路径规范化、UNC/映射盘、Reparse/Junction、执行前重解析；
- 未实现 SchemaValidator、PolicyEngine、ActionPlan 文件、ResultVerifier 或审计执行链；
- 未接入 Agent、主界面、ASR、TTS 或语音链路；
- 未开放任意 PowerShell、Python、Shell、批处理、外部命令或进程终止；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，项目文件仍为 untracked 基线。

## 下一批次入口：P4 批次 3

下一批次应严格从既定路线图进入**受控写工具的计划生成/预览，不执行写操作**。建议先测试后实现，至少保持：

- 仅处理已注册的 `create_text_file`、`write_text_file`、`copy_file`、`move_file`、`rename_file`、`move_to_recycle_bin`、`start_whitelisted_application` 声明；
- 只生成结构化计划，不写真实文件、不移动、不删除、不启动应用；
- 继续禁止 PathGuard、PolicyEngine、ActionPlan 持久化和最终授权执行链提前混入，除非路线图明确进入对应批次；
- 继续使用项目内 `.python312\python.exe`，并显式把项目根加入 `sys.path`；
- 继续按 TDD、故障注入和约 60% 上下文纪律工作。

## 交接后的强制停止点

当前停止在 `P4 batch 3` 门前。后续会话读取本文件、路线图 P4 卡片、`app/tools/read_only.py`、相关测试和 `task_plan.md` 后再继续；不得从 PathGuard、授权弹窗、执行器或语音链路跳阶段。