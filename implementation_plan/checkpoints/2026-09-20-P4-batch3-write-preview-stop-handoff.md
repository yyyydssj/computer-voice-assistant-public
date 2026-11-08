# P4 批次 3 受控写动作预览完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P4 结构化工具、路径安全、策略和 ActionPlan  
> 状态：`P4 batch 3 complete`；已停在 P4 批次 4 门前

## 本批次唯一目标

为七个已注册的副作用工具生成严格、结构化、可审查的候选动作预览，同时保证绝不发生文件写入、移动、删除或应用启动：

1. `create_text_file`
2. `write_text_file`
3. `copy_file`
4. `move_file`
5. `rename_file`
6. `move_to_recycle_bin`
7. `start_whitelisted_application`

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P4-batch2-read-only-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次授权；
- 本轮按用户要求在上下文约 60% 时更新最新交接并停止，不进入 P4 批次 4；
- 未改变既定 13 项 MVP 工具清单、风险等级、路径根、确认要求或阶段顺序。

## 已完成

### 纯计划生成模块

新增 `app/tools/write_plans.py`，提供：

- `WritePlanError`：稳定错误码、消息和可选字段位置；
- `WriteActionPreview`：冻结的不可执行候选动作，绑定工具名、版本、Manifest 哈希、风险、确认要求、规范化参数、预览、验证方式和撤销策略；
- `build_write_action_preview()`：只接受固定 ToolRegistry 中的七个副作用工具；
- `to_dict()` 明确输出 `preview_only: true`，内部映射和序列递归冻结，序列化时恢复 JSON 对象/数组。

### 各工具预览语义

- `create_text_file`：校验 UTF-8/GBK 编码和 10 MiB 字节上限，预览路径、编码、大小、内容哈希、父目录策略、目标必须不存在和冲突拒绝；不暴露正文到 preview；
- `write_text_file`：只允许 `append` 与 `replace_exact`，强制小写 SHA-256 源哈希；精确替换必须提供 `target_text`，追加模式禁止该字段；预览正文/目标文本哈希和字节数，不允许整文件任意重写；
- `copy_file` / `move_file`：源和目标分别校验，拒绝相同路径和覆盖策略，只允许 `conflict_policy=deny`；移动预览固定声明跨盘为复制、验证、再移除源；
- `rename_file`：只接受单个 Windows 文件名，拒绝路径分隔符、保留名、尾随点/空格和同名；目标必须保持同一父目录；默认禁止扩展名变化；
- `move_to_recycle_bin`：完整列出规范化清单，拒绝重复路径，最多 20 项，永久删除固定为禁用；
- `start_whitelisted_application`：只接受合法 `application_id`，必须由本地 resolver 命中登记项；不接受可执行路径、命令行参数、终端或解释器。

### 安全边界

- 路径工具必须显式接收 `path_validator`，缺失直接 `TypeError`；校验器的越界/拒绝异常不会被吞掉或绕过；
- 当前只建立 PathGuard 的窄调用接口，不实现真实路径规范化、UNC、映射盘、Reparse/Junction 或执行前重解析；
- 应用工具必须显式接收 `application_resolver`，不在本模块内维护或扩大白名单；
- 只读工具、未知工具、`run_command`、`run_controlled_powershell` 等不能形成副作用候选动作；
- 模块没有 `execute`、文件 I/O、目录创建、复制/移动/重命名、回收站、子进程或应用启动入口；
- 本批次未生成 `plan.json`、`execution_plan.md` 或 `result.json`，未混入批次 6 的 ActionPlan 文件规则。

### 公开入口

更新 `app/tools/__init__.py`，公开：

- `WriteActionPreview`
- `WritePlanError`
- `build_write_action_preview`

固定 ToolRegistry 仍恰含原有 13 项，顺序和 Manifest 内容未改变。

## TDD 与故障注入

新增 `tests/tools/test_write_plans.py`，覆盖：

- 创建文本的编码、字节上限、内容哈希、父目录策略和无实际创建；
- 写文本的追加/精确替换模式、源哈希和目标文本规则；
- 复制/移动的冲突拒绝、递归默认、源目标相同和跨盘策略；
- 重命名的同父目录、Windows 名称、扩展名变化控制；
- 回收站 L3、完整清单、去重和 20 项上限；
- 应用白名单 ID、未知应用、参数注入拒绝；
- 只读、未知和命令工具拒绝；
- 必传路径校验接口及拒绝异常保留；
- 预览对象不可变、可序列化且无执行表面。

首次红测按预期因公开入口不存在而失败。实现后专项复核发现内部冻结序列为元组、测试却直接与列表比较；保留不可变设计并把断言修正为序列内容比较。

## 验证结果

- 写动作预览专项：`10/10` 通过；
- 全量 unittest：`137/137` 通过；
- `compileall`：使用可写工作区的 `PYTHONPYCACHEPREFIX` 通过；
- 严格 UTF-8：`59` 个 Python 源文件通过，无替换字符；
- 静态 AST 审计：无 `os`、`shutil`、`subprocess`、`ctypes` 副作用导入，无 open/write/move/delete/start 等调用；
- 公开入口 smoke：三个新入口均可从 `app.tools` 导入；
- 固定 catalog smoke：仍为 13 项，顺序不变；
- 真实无副作用 smoke：目标未创建、源文件未改变、移动目标未出现、应用只形成预览，全部 `preview_only=True`；
- 未读取 `.env`，未启动官方 Harness runtime，未发送真实 API 请求。

## 环境与错误记录

- 项目既有 `__pycache__` 对当前沙箱身份不可写，继续使用可写工作区作为 `PYTHONPYCACHEPREFIX`；未删除或修改项目缓存；
- 首次实现脚本已成功写入 `write_plans.py`，随后因 `app/tools/__init__.py` 使用 LF、脚本只搜索 CRLF 锚点而在公开导出前停止；核对文件状态后采用 LF 幂等更新，没有覆盖既有实现；
- Git 仍因仓库所有权提示需要命令级 `safe.directory`，仅用 `git -c safe.directory=...` 做只读检查，没有修改全局 Git 配置。

## 新增/修改文件

- `app/tools/write_plans.py`
- `app/tools/__init__.py`
- `tests/tools/test_write_plans.py`
- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`
- 本交接文档

## 本批次明确未做

- 未实现 PathGuard、SchemaValidator 或 PolicyEngine；
- 未实现真实文件创建、追加、替换、复制、移动、重命名或回收站操作；
- 未启动任何应用；
- 未实现应用白名单存储或配置界面；
- 未生成或持久化 `plan.json`、`execution_plan.md`、`result.json`；
- 未实现授权弹窗、L2/L3 消费、执行器、验证器、审计执行链或恢复；
- 未接入 Agent、主界面、ASR、TTS 或语音链路；
- 未开放任意 PowerShell、Python、Shell、批处理、外部命令或进程终止；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，项目文件仍为 untracked 基线。

## 下一批次入口：P4 批次 4

下一批次应严格实现 **PathGuard**，至少按路线图和已定稿安全边界覆盖：

- Windows 路径规范化与绝对路径；
- 允许根目录 `<STUDY_ROOT>` 和默认工作目录 `<STUDY_ROOT>\AI工作区`；
- `..`、大小写、斜杠、环境变量与相对路径处理；
- UNC、网络路径、其他盘符和不可可靠解析路径拒绝；
- 符号链接、Junction、Reparse Point 与真实目标边界；
- 映射盘处理；
- 规划时校验与未来执行前再次解析所需的稳定接口；
- 故障注入：链接替换、路径竞态、目标消失、权限不足和路径越界。

批次 4 不得顺手实现 PolicyEngine、ActionPlan 文件、授权或执行器。

## 交接后的强制停止点

当前停止在 `P4 batch 4` 门前。后续会话读取本文件、路线图 P4 卡片、`shared/safety-boundaries.md`、现有 `read_only.py` / `write_plans.py` 及相关测试后，可按用户当前指令直接进入下一批次；不得从 PolicyEngine、ActionPlan、授权、执行器或语音链路跳阶段。