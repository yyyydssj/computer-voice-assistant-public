# P0 交接：实现冻结与环境基线

> 交接日期：2026-09-19  
> 本文件是 P0 完成后的唯一接续入口之一；下一阶段仍需用户明确授权。

## 阶段编号

P0

## 阶段状态

complete

## 本阶段唯一目标

把现有设计决策整理为可交给独立编码会话执行的冻结基线，明确 MVP 范围、跨模块安全契约、环境约束、延期事项和 P1 的最小读取入口；不创建业务实现。

## 本阶段明确不做

- 不创建业务代码、测试骨架或未来阶段的空实现目录。
- 不安装 Python、项目依赖或运行时服务。
- 不下载、修改或提交模型、音频、声纹、音色数据。
- 不实现 Tool、Agent、ASR、TTS、AudioBus、授权执行器、Skill 或界面。
- 不修复当前 `.venv`，不执行 Git 暂存、commit 或 push。
- 不进入 P1；P1 必须由用户另行明确授权。

## 进入条件

- `implementation_plan/` 中的安全、错误恢复、音频和 ActionPlan 原则已整理。
- GitHub 源码对比已固定到 commit/tag，并记录在 `research/github-comparison-and-optimization.md`。
- MVP-1/2/3 范围、延期功能边界和阶段顺序已明确。
- 用户已明确授权执行 P0；本交接不构成对 P1 的授权。

## 允许修改的文件范围

- `implementation_plan/` 下的规划、冻结清单和 checkpoint 文档。
- 项目根目录的 `task_plan.md`、`progress.md`。
- 项目根目录的 `.gitignore`，且只用于排除敏感信息、本地环境和本地运行数据。
- P0 不允许创建或修改项目源代码目录、依赖清单或模型目录中的内容。

## 稳定契约

1. **标识贯穿链路：** `instance_id`、`session_id`、`utterance_id`、`request_id`、`task_id`、`plan_id`、`execution_id`、`action_id`、`attempt_id`。
2. **授权是事实而非提示：** `plan_hash`、`authorization_snapshot_hash`、规范化目标、目标状态快照、授权动作集合、风险等级、确认窗口和过期时间必须可追溯；计划或目标实质变化即令旧授权失效。
3. **状态正交：** 任务/授权/执行事实与音频/Provider 状态分离；ASR、TTS、麦克风、扬声器或 Provider 故障不得改变任务结果、计划哈希、授权状态或执行事实。
4. **重试分层：** 传输层、Agent 请求层、电脑副作用动作层分别处理；已开始或结果未知的副作用动作不得原地重试，必须先扫描真实状态；无法确认时使用 `state_uncertain`。
5. **崩溃恢复：** 崩溃、强制终止或重启后不自动续跑、补做、回滚或复用旧计划、旧确认、旧授权、旧音频和旧 Provider 请求；继续前必须重新规划、预检和授权。
6. **敏感配置：** API key 只来自环境变量或未提交 `.env`；公开模板只允许 `.env.example`。个人路径、设备、音色和声纹不进入公开实现计划。
7. **降级不扩权：** 语音组件不可用时保留文字与安全闭环，任何输入/输出通道降级都不得绕过 Agent 边界、PathGuard 或 L2/L3 授权。

## 实现批次

- 批次 0.1：建立根目录 `.gitignore`，排除 `.env`、`.venv/`、`models/`、本地日志/计划/记忆、缓存、音频和旧式运行结果文件；保留 `.env.example` 与公开规划文档。
- 批次 0.2：重写 `P0-environment.md`，将 P0 定义为环境基线冻结，不在本阶段安装依赖或下载模型；记录当前环境事实。
- 批次 0.3：创建 `P0-freeze-baseline.md`，冻结 MVP 范围、跨模块契约、A–N 分类、环境规则、固定源码证据入口和 P1 最小读取入口。
- 批次 0.4：更新路线图、导航、共享里程碑和根目录计划状态，明确 P0 complete、P1 授权门和停止点。
- 批次 0.5：生成本交接文件并同步会话进度。

## 测试与故障注入

- 对所有 Markdown 与 `.gitignore` 执行严格 UTF-8 解码检查，拒绝替换字符和截断。
- 检查 `.gitignore` 正例：`.env`、`.venv/`、`models/`、日志、音频和缓存被忽略。
- 检查 `.gitignore` 反例：`.env.example`、`implementation_plan/`、`task_plan.md`、`progress.md` 不被忽略。
- 核对 P0-P11 路线图、P0 handoff、README、P0 冻结清单和任务/进度状态一致。
- 核对 A–N 十四个主题各出现且分类明确，固定源码证据仍覆盖八个项目。
- 核对项目不存在 `src/`、`app/`、`tests/`、`pyproject.toml`、`requirements.txt` 或 `package.json` 等业务实现产物。
- 核对当前 `.venv` 基础解释器缺失和 `py.exe` 无已安装 Python 的事实已记录；确认没有因该事实安装或下载任何内容。

## 退出条件（可观察事实）

- P0 环境文档和冻结清单已存在且与路线图一致。
- P0-P11 每阶段的入口、出口、文件范围和停止点已在路线图定义。
- 固定 commit/tag 的源码对比表存在，且八个项目均可定位。
- A–N 未决主题全部被标记为冻结、默认值+可校准范围，或明确延期。
- `.gitignore` 正反例验证通过；敏感文件、本地环境和模型不出现在 Git 状态的公开候选中。
- 本文件和 `progress.md` 已记录实际完成内容、风险和 P1 入口。
- 以上条件满足后立即停止，不执行 P1 工作。

## 已知风险

- 当前 `<PROJECT_ROOT>\.venv\pyvenv.cfg` 指向的基础解释器 `<USER_HOME>\AppData\Local\Programs\Python\Python312\python.exe` 不存在，因此 `.venv\Scripts\python.exe` 当前不可启动。
- 当前可见的 `<STUDY_ROOT>\python\Launcher\py.exe` 报告没有已安装 Python；本地虽有 `<STUDY_ROOT>\python\python-3.12.10-amd64.exe` 安装包，但 P0 未执行安装。
- P1 开始前必须由用户授权环境预检；在此之前不得重建 `.venv`、安装 Python、安装依赖或下载模型。
- 尚未建立首次 Git commit；本轮不擅自暂存、commit 或 push。

## 未解决但明确延期的问题

- Python 基础解释器修复、依赖安装方式、模型版本/来源和本地数据根目录的最终设备级确认：延期至 P1 环境预检，且需用户授权。
- Provider、ASR、TTS 的真机阈值与设备参数：延期至 P6–P10 校准，不改变已冻结的安全和状态边界。
- Skill 最终用户确认、导入和运行实现：延期至 P11；当前只是完成源码对比优化的候选方案。
- Windows 常驻、打包、热词、手册、隐私清理和发布验收：延期至 P11。
- 具体 P1 实现批次、测试代码和运行时接口：不得在 P0 代替 P1 冻结。

## P1 入口

用户明确授权 P1 后，读取以下最小集合：

1. `implementation_plan/implementation-roadmap.md` 的 P1 卡片；
2. 本文件 `implementation_plan/checkpoints/P0-handoff.md`；
3. `implementation_plan/P0-freeze-baseline.md`；
4. P1 卡片列出的共享契约文件；
5. `task_plan.md` 与 `progress.md` 的最新尾部状态。

P1 首个动作是环境预检和最小运行时外壳的测试设计；先处理 `.venv` 基础解释器缺失事实，再决定是否需要用户授权安装或重建环境。

## P1 禁止提前做的事项

- 未获 P1 明确授权，不创建 `app/`、`tests/`、`src/`、`pyproject.toml`、`requirements.txt` 或任何业务实现文件。
- 不接 DeepSeek、麦克风、ASR、TTS、VoiceStudio 或电脑工具。
- 不创建唤醒词、VAD、AEC、Skill、Windows 常驻或打包逻辑。
- 不把 P0 的环境安装包、个人路径、API key、音色或声纹事实写入公开代码或配置模板。

## 最小读取文件集合

- `implementation_plan/README.md`
- `implementation_plan/implementation-roadmap.md`
- `implementation_plan/P0-freeze-baseline.md`
- `implementation_plan/P0-environment.md`
- `implementation_plan/shared/architecture.md`
- `implementation_plan/shared/safety-boundaries.md`
- `implementation_plan/shared/error-recovery.md`
- `implementation_plan/shared/action-plan.md`
- `task_plan.md`
- `progress.md`（只读取最新尾部，不恢复全部历史）

## 停止声明

P0 已完成。当前停在 P1 授权门前；本交接完成后不得自动开始 P1，也不得把本文件中的 P1 入口误解为用户授权。