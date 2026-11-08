# P4 批次 4 PathGuard 完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P4 结构化工具、路径安全、策略和 ActionPlan  
> 状态：`P4 batch 4 complete`；已停在 P4 批次 5 门前

## 本批次唯一目标

实现拒绝优先的 Windows `PathGuard`：把不可信路径规范化到固定 MVP 边界，检查真实路径和 Reparse/Junction/映射盘风险，并提供未来执行前再次解析的稳定接口；本批次不执行任何文件动作。

## 授权与接续事实

- 严格接续 `implementation_plan/checkpoints/2026-09-20-P4-batch3-write-preview-stop-handoff.md`；
- 用户明确要求直接开始下一批次，无须再次授权；
- 按用户要求在本轮上下文约 60% 时更新最新交接并停止，不进入 P4 批次 5；
- 未改变固定 13 项 MVP 工具、允许根、默认工作目录、风险等级、确认规则或阶段顺序。

## 已完成

### PathGuard 模块

新增 `app/safety/path_guard.py` 与 `app/safety/__init__.py`，公开：

- `PathGuard`
- `PathGuardError`
- `PathSnapshot`
- `WindowsPathProbe`
- `DEFAULT_ALLOWED_ROOT`
- `DEFAULT_WORKING_DIRECTORY`

固定产品边界仍为：

```text
允许根：<STUDY_ROOT>
默认工作目录：<STUDY_ROOT>\AI工作区
```

`PathGuard` 实现 `__call__(value, purpose) -> pathlib.Path`，可直接注入批次 2/3 已建立的 `path_validator` 窄接口；没有修改已有工具清单或 Manifest。

### 规范化和歧义拒绝

- 相对路径只相对固定默认工作目录解析，不使用进程当前目录；
- 处理 `%NAME%` 环境变量、正反斜杠、大小写和 `..`；
- 未定义或畸形环境变量拒绝；
- UNC、设备路径、网络路径、其他盘符、盘符相对路径和当前盘符根相对路径拒绝；
- 备用数据流或其他含歧义冒号的路径拒绝；
- Windows 8.3 短文件名别名拒绝；
- Win32 会折叠的尾随点或空格组件拒绝；
- 规范化结果必须位于 `<STUDY_ROOT>` 内，不能使用字符串包含判断绕过。

### 驱动器、真实路径和链接边界

- 默认 Windows 探针使用 `GetDriveTypeW`；只允许固定本地驱动器；映射/远程盘、移动盘、未知或无法可靠确认的驱动器拒绝；
- 对从驱动器根到目标的每个既有组件逐级 `lstat`；
- `st_file_attributes` 的 Reparse 标记和符号链接模式均拒绝，因此 Junction、symlink 和其他 Reparse Point 默认不可作为目标或中间目录；
- 每一级真实路径必须与允许根保持合法祖先/后代关系；既有最终目标和不存在目标拼接后的真实路径必须严格位于允许根内；
- 权限不足、状态读取失败或真实路径解析失败均采用拒绝策略。

### 规划快照和未来执行前复检

`PathSnapshot` 是 frozen/slotted 的不可变事实，记录：

- 原始请求、用途、规范化路径和真实路径；
- 当前存在性与目标 `(st_dev, st_ino)` 身份；
- 最近既有父路径及其身份；
- 每个既有路径组件的身份序列。

`PathGuard.revalidate()` 会重新读取真实状态并拒绝：

- 规划后目标消失或出现；
- 目标身份变化；
- 最近既有父路径身份变化；
- 任一路径组件身份变化；
- 链接/Junction/Reparse Point 替换；
- 真实路径变化；
- 权限丢失或路径状态无法确认。

该接口只提供执行前复检能力，不执行动作、不产生授权，也不实现自动重试或恢复。

## TDD 与故障注入

新增 `tests/safety/test_path_guard.py`，共 11 个测试，覆盖：

- 固定允许根和默认目录；
- 相对路径基准、环境变量、斜杠、大小写和 `..`；
- UNC/设备路径、其他盘符、盘符相对、根相对；
- 未解析环境变量、8.3 短名、ADS、尾随点/空格和越界；
- 映射/远程盘拒绝；
- 中间组件 Reparse Point 拒绝；
- 真实目标越界和解析权限不足；
- 不存在目标锚定最近既有父目录；
- 快照不可变和可序列化；
- 目标出现、消失、身份变化；
- 链接替换、父路径竞态和权限丢失。

测试通过注入的文件系统探针模拟危险状态，不依赖管理员权限，不创建真实链接或修改真实文件系统。

## 验证结果

- PathGuard 专项：`11/11` 通过；
- 全量 unittest：`148/148` 通过；
- `compileall`：使用可写工作区 `PYTHONPYCACHEPREFIX` 通过；
- 严格 UTF-8：`63` 个 Python 源文件通过，无替换字符；
- AST 审计：PathGuard 没有 `execute`、文件写入、复制、移动、删除、目录创建或子进程入口；
- 真实 Windows smoke：当前项目文件可形成快照并通过 `revalidate()`，相对路径固定落到默认工作目录，其他盘符、UNC 和越界路径被拒绝；
- 现有工具集成 smoke：`read_text_file` 和 `build_write_action_preview` 均可直接注入 `PathGuard`；写动作仍只生成 `preview_only=True` 的预览；
- 未读取 `.env`，未启动官方 Harness runtime，未发送真实 API 请求。

## 错误与修正记录

1. 红测文件首次使用了不能以单反斜杠结尾的 Python 原始字符串 `r"G:\"`，修正为普通转义字符串后继续；没有产品代码写入风险。
2. 项目嵌入式 Python 的 `._pth` 隔离使普通 discovery 无法导入仓库根包，改为只在测试进程内插入项目根，不修改解释器配置。
3. 首版实现误把允许根的必要祖先 `G:\` 当成越界；修正为逐级真实路径允许“允许根的祖先或后代”，同时对既有最终目标保持“必须严格位于允许根内”。
4. 一个竞态测试复用了前一子场景已修改的探针状态，导致预期身份变化却实际检测为目标出现；重建干净探针隔离子场景。
5. 写预览 smoke 使用了不存在的字段 `target_must_not_exist`，核对真实稳定字段后改验 `conflict_policy=deny`；未修改实现迎合错误断言。
6. 首次 AST 审计把字符串 `.replace()` 误报为文件替换 API；收紧为明确文件/进程 API 后通过。
7. 首次文档收口命令因 PowerShell `$count:` 变量边界歧义在解析阶段失败；命令零写入，改用 `${count}:` 后重跑。

## 新增/修改文件

- `app/safety/path_guard.py`
- `app/safety/__init__.py`
- `tests/safety/test_path_guard.py`
- `tests/safety/__init__.py`
- `implementation_plan/implementation-roadmap.md`
- `task_plan.md`
- `progress.md`
- 本交接文档

## 本批次明确未做

- 未实现 PolicyEngine；
- 未生成或持久化 `plan.json`、`execution_plan.md`、`result.json`；
- 未实现授权弹窗、L2/L3 消费、执行器、验证器、审计执行链或恢复；
- 未执行文件创建、写入、复制、移动、重命名或回收站动作；
- 未启动应用；
- 未开放任意 PowerShell、Python、Shell、批处理、外部命令或进程终止；
- 未接入 Agent 主链、主界面、ASR、TTS 或语音链路；
- 未执行 `git add`、`git commit` 或 `git push`；仓库仍无首次 commit，项目文件仍为 untracked 基线。

## 下一批次入口：P4 批次 5

下一批次只实现 **PolicyEngine**：

- 只接收本地可信代码构造、已经过 Schema 与 PathGuard 的结构化快照；
- 输出稳定的 `allow` / `deny` / `require_confirmation` 决策；
- 固定工具风险等级、确认要求、允许根和硬拒绝规则，模型、Skill、外部文本、工具返回或普通配置不能提升权限；
- 明确拒绝原因、policy version 和可序列化决策事实；
- 覆盖伪造风险等级、路径快照缺失/变化、未知工具、任意命令、系统黑名单、确认降级和不可信字段注入。

批次 5 不得顺手实现 ActionPlan 文件、授权、执行器或语音链路。

## 交接后的强制停止点

当前停止在 `P4 batch 5` 门前。后续会话读取本文件、路线图 P4 卡片、`shared/safety-boundaries.md`、现有 `manifest.py` / `write_plans.py` / `path_guard.py` 及相关测试后，可按用户当前指令直接进入下一批次；不得从 ActionPlan、授权、执行器或语音链路跳阶段。