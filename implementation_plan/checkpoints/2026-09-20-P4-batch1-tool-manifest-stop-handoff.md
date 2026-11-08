# P4 批次 1 Tool Manifest 完成交接

> 交接日期：2026-09-20  
> 项目：`<PROJECT_ROOT>`
> 阶段：P4 结构化工具、路径安全、策略和 ActionPlan  
> 状态：`P4 batch 1 complete`；已停在 P4 批次 2 门前

## 本批次唯一目标

建立声明式、稳定、不可由模型修改的 Tool Manifest 与 ToolRegistry，使后续工具能力只能来自已批准结构化清单；本批次不执行任何电脑动作。

## 授权与接续事实

- 上一交接：`implementation_plan/checkpoints/P3-handoff.md`；
- 用户于 2026-09-20 明确要求严格接续并直接开始下一批次，无须再次授权；
- 本轮只完成 P4 批次 1，并按用户要求在上下文约 60% 时更新交接后停止。

## 已完成

### 冻结 Tool Manifest

`app/tools/manifest.py` 提供：

- `ToolManifest`：冻结、slotted、只含 JSON 数据的声明对象；
- `ManifestError`：Manifest、注册表和安全边界错误；
- 稳定 `sha256:` 内容哈希，覆盖全部安全字段；
- 严格 `from_dict()`：拒绝未知/缺失字段和伪造内容哈希；
- 输入/输出 Schema 必须是 `type=object` 且 `additionalProperties=false`；
- 风险只允许 L0-L3，L4 永不注册；
- L0/L1 必须无需确认，L2/L3 必须声明需要确认；
- 文件工具只能声明 `<STUDY_ROOT>`，默认工作目录固定为 `<STUDY_ROOT>\AI工作区`；
- 任意 command、PowerShell、Shell、Python 和进程终止名称在边界处拒绝；
- Manifest 不含 `execute`、回调、函数或系统调用入口。

### 构造后封闭 ToolRegistry

- 只接受 `ToolManifest`；
- 只接受已定稿的 13 个名称；
- 拒绝重复名称和清单外工具；
- 内部 tuple 与 mapping proxy 构造后不可替换；
- 没有 `register`、覆盖、删除或运行时扩展接口；
- 模型、Skill、执行文档和普通配置不能修改注册表。

### 固定 MVP Catalog

`app/tools/catalog.py` 以固定顺序声明：

1. `list_directory` — L0；
2. `read_text_file` — L1；
3. `search_files` — L1；
4. `get_file_info` — L0；
5. `get_system_info` — L1；
6. `get_processes` — L1；
7. `create_text_file` — L2；
8. `write_text_file` — L2；
9. `copy_file` — L2；
10. `move_file` — L2；
11. `rename_file` — L2；
12. `move_to_recycle_bin` — L3；
13. `start_whitelisted_application` — L2。

每项均声明：名称、版本、说明、输入/输出 Schema、风险、确认、路径范围、默认目录、超时、最大输出、幂等性、取消能力、预览、验证、撤销策略、审计事件类型、限制和内容哈希。

## 本地拒绝事实

以下情况已有测试并在本地拒绝：

- `run_controlled_powershell`、`run_command`、Shell/Python/命令面；
- `terminate_process`；
- 任意第 14 个自造工具；
- L4 工具；
- 重复名称；
- 开放输入 Schema；
- 未知字段或伪造 `content_hash`；
- 风险等级和确认要求矛盾；
- 非 `<STUDY_ROOT>` 路径范围或错误默认工作目录；
- 修改 Manifest 的 frozen 字段或嵌套 mapping；
- 替换 ToolRegistry 的内部清单或映射；
- 调用不存在的运行时 `register`。

## 测试与验证

- `tests/tools/test_manifest.py`：10/10 通过；
- 全量 unittest：117/117 通过；
- `python -m compileall -q app tests`：通过；
- 严格 UTF-8：55 个 Python 源文件通过，无替换字符；
- catalog smoke：13 个 Manifest、13 个唯一内容哈希、全部无 `execute`；
- 风险分布：L0=2、L1=4、L2=6、L3=1。

## 新增文件

- `app/tools/__init__.py`
- `app/tools/manifest.py`
- `app/tools/catalog.py`
- `tests/tools/__init__.py`
- `tests/tools/test_manifest.py`

## 本批次明确未做

- 未实现 `list_directory`、`read_text_file`、`search_files`、`get_processes` 或任何其他工具逻辑；
- 未访问文件系统、进程表或系统信息作为工具行为；
- 未实现写工具的计划生成或预览；
- 未实现 PathGuard、SchemaValidator、PolicyEngine、ActionPlan 文件解析；
- 未实现授权弹窗、执行器、ResultVerifier、审计执行链；
- 未接入 Agent、主界面、ASR、TTS 或语音链路；
- 未读取 `.env`，未启动官方 runtime，未发送真实 API 请求。

## 已知风险与注意事项

- JSON Schema 当前是 Manifest 数据，批次 1 只验证 Schema 自身的基础封闭结构；真正的参数实例校验器属于后续 P4 工作。
- Path scope 当前仅是不可变声明；真实路径、UNC、映射盘、Reparse/Junction 和执行前重解析属于 P4 批次 4 PathGuard。
- `write_text_file` 等写工具当前只有声明，不得被误认为已有计划或执行能力。
- 路线图批次 2 的历史简称已与已定稿工具名称对齐为 `read_text_file`、`list_directory`、`search_files`、`get_processes`。
- `.venv` 仍引用不可用外部解释器；继续使用项目内 `.python312\python.exe` 并显式把项目根加入 `sys.path`。

## 下一批次入口

P4 批次 2 只实现以下四个只读工具的结构化行为：

1. `list_directory`；
2. `read_text_file`；
3. `search_files`；
4. `get_processes`。

批次 2 应继续测试先行，至少覆盖正常结果、数量/大小/深度/超时限制、权限不足、路径越界占位接口、二进制/超大文件、进程退出竞态和敏感字段过滤。

## 下一批次禁止提前做的事项

- 不实现 `get_file_info` 或 `get_system_info`，除非批次 2 的既定范围被正式调整；
- 不实现任何写操作或写计划；
- 不实现 PathGuard、PolicyEngine 或 ActionPlan；
- 不创建授权弹窗或执行器；
- 不开放任意 PowerShell、Python、Shell、外部命令或动态工具；
- 不接入 Agent、主界面、ASR、TTS 或语音链路。

## 需要读取的最小文件集合

下一次接续只需读取：

1. 本交接文档；
2. `implementation_plan/implementation-roadmap.md` 的 P4 卡片；
3. `app/tools/manifest.py`、`app/tools/catalog.py`；
4. `tests/tools/test_manifest.py`；
5. `implementation_plan/discussions/pending-decisions.md` 的 A.2/A.4 中四个只读工具限制；
6. `task_plan.md` 最新状态。

## Git 事实

- 当前分支：`main`；
- 仓库无首次 commit，项目文件仍处于 untracked 基线；
- 未执行 `git add`、`git commit` 或 `git push`。
