# P1 交接：运行时外壳与最小界面

> 交接日期：2026-09-19  
> 本文件是 P1 完成后的唯一接续入口之一；当前停在 P2 授权门前。

## 阶段编号

P1

## 阶段状态

complete

## 本阶段唯一目标

程序能够安全启动、显示正式文字界面、接受键盘输入、正常关闭，并记录不含敏感内容的结构化生命周期事件；同时建立本地私有目录归属边界和启动时“发现遗留任务但绝不自动续跑”的安全哨兵。

## 本阶段明确不做

- 不接 DeepSeek 请求或任何 Agent。
- 不接麦克风、ASR、TTS、VoiceStudio、唤醒词、VAD 或 AEC。
- 不实现电脑工具、动作执行、授权、计划执行器或恢复执行器。
- 不创建 Session、Request、Plan、Auth、Action、Result、Audio、Provider 等 P2 契约。
- 不实现 Skill、Windows 常驻、打包或自动更新。
- 不安装第三方依赖；实现与测试只使用 Python 标准库。
- 不执行 Git 暂存、commit 或 push。

## 进入条件

- P0 已完成并生成 `checkpoints/P0-handoff.md`。
- Python 3.12.10 虚拟环境可运行。
- 用户已于 2026-09-19 明确授权直接执行 P1。
- 本地数据根、模型目录和用户配置保持可配置，敏感数据不得进入公开仓库。

## 允许修改的文件范围

- `app/main.py`
- `app/config/`
- `app/runtime/`
- `app/logging/`
- `app/ui/`
- `tests/foundation/`
- `implementation_plan/checkpoints/P1-handoff.md`
- `implementation_plan/implementation-roadmap.md`
- 根目录 `task_plan.md`、`progress.md`

没有创建 `audio/`、`asr/`、`tts/`、`tools/`、Agent 或其他未来阶段实现目录。

## 已实现内容

### 1. 配置与私有目录

- `.env` 与环境变量配置加载，环境变量优先。
- API key 等敏感配置只记录“是否配置”，不保存或展示原值。
- 数据根必须是本地绝对路径，且不得位于项目仓库、UNC 或网络路径。
- `logs/`、`plans/`、`audit/`、`temp/` 必须位于当前 `data_root` 内，彼此不得相同或互相包含。
- 自定义运行目录会检查仓库越界、UNC、相对路径、文件冒充目录和不可写故障。
- 私有目录只在显式准备阶段创建，不下载模型或创建未来模块数据。

### 2. 生命周期与单实例

- Windows 文件锁保证单实例运行。
- 每个进程实例生成 UUID `instance_id`。
- 生命周期事件具有从 1 开始的单调序号。
- 支持安全启动、事件记录和幂等关闭。
- 生命周期字段拒绝敏感键、嵌套对象、超长文本和不支持类型。

### 3. 结构化日志

- 标准库 JSONL 日志，默认文件为 `runtime.jsonl`。
- 日志写入线程安全，目录会被初始化并检查可写。
- API key、authorization、token、password、音频、声纹、embedding、完整文件内容和原始本地路径不会进入普通日志。
- 日志关闭后拒绝继续写入。

### 4. 最小文字界面

- 显示“文字模式 / 语音不可用 / 任务空闲”。
- 接受键盘文字输入。
- 支持 `/exit`、`:q`、`退出`、`关闭` 和 EOF 安全关闭。
- Agent 未接入时只说明“本阶段不执行任务”，不伪造成功事实。

### 5. 遗留任务安全哨兵

- 启动时只读检查 `plans/<task>/versions/<version>/plan.json` 是否缺少同目录 `result.json`。
- 发现线索时只记录匿名计数和 `auto_resume=false`，不记录计划路径或内容。
- 界面明确提示“发现可能未完成的旧任务；本次启动未自动续跑”。
- 检测逻辑不解析、不修改、不删除、不执行旧计划，不创建结果文件，也不跟随目录符号链接。
- P1 不生成正式恢复报告、不扫描电脑真实状态；这些仍按后续阶段设计执行。

## 稳定契约

1. 配置与本地私有数据必须和公开代码分离。
2. `instance_id` 在同一运行实例的所有生命周期日志中保持一致。
3. 生命周期事件序号严格单调递增。
4. 普通日志不得包含 secret、音频内容、参考音频路径、完整文件内容或原始个人路径。
5. 日志、计划、审计和临时目录必须由同一 `data_root` 归属并相互隔离。
6. 启动发现旧计划线索时，唯一允许的 P1 行为是报告存在性与数量；`auto_resume` 永远为 `false`。
7. 语音不可用或 Agent 未接入不得改变任务事实、扩大权限或伪造执行成功。

## 测试与故障注入

使用标准库 `unittest`，最终全量结果为 34/34 通过，覆盖：

- 缺少 Provider key 时安全进入文字模式。
- `.env` 格式错误、非法数值和环境变量优先级。
- 数据根位于仓库、UNC/网络路径和相对路径时拒绝。
- 自定义 logs/plans/audit/temp 位于数据根之外或仓库内时拒绝。
- logs/plans/audit/temp 使用 UNC、文件路径、不可写路径或互相重叠时拒绝。
- 单实例重复启动、UUID、单调事件序号、启动前/关闭后保护和幂等关闭。
- 日志脱敏、音频/文件内容省略、关闭后写入拒绝。
- 最小文字壳状态、退出命令、EOF 和未接 Agent 的真实提示。
- 启动发现缺少 `result.json` 的旧 `plan.json` 时只报告、不续跑、不修改计划、不创建结果。
- 已有 `result.json`、无计划目录和无关目录不会误报。

## 验证结果

```text
<PROJECT_ROOT>\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test*.py' -q
Ran 34 tests
OK

<PROJECT_ROOT>\.venv\Scripts\python.exe -m compileall -q app tests
通过

严格 UTF-8 解码
18 个 app/tests Python 源文件全部通过

真实命令行烟测
退出码 0；事件顺序 runtime_started → config_checked → runtime_ready → runtime_closing
```

## 退出条件（可观察事实）

- 程序可以启动、接受文字输入并正常关闭。
- 生命周期事件包含一致的 `instance_id` 和单调序号。
- 配置、日志、计划、审计、临时数据与公开代码分离。
- UI 显示“文字模式 / 语音不可用 / 任务空闲”，且不伪造任务成功。
- 强制结束模拟能够在下次启动发现未完成任务线索，但不会自动续跑。
- 全量测试、编译、UTF-8 和真实启动烟测均通过。
- P1 handoff、路线图、任务计划和进度记录已同步。

## 已知风险与后续边界

- P1 的遗留任务检测只是安全哨兵，不是正式恢复扫描；它不能判断动作是 `completed`、`not_completed` 还是 `state_uncertain`。
- 旧计划的真实性、Schema、哈希、授权和实际电脑状态尚未实现，也不得由 P1 推断。
- 当前没有 Provider、Agent、电脑工具或音频能力；文字输入不会执行任务。
- 当前项目尚无首次 Git commit；本阶段未擅自 commit 或 push。
- 控制台中文在当前外层 PowerShell 捕获中可能显示乱码，但 Python 文件、日志和测试均严格 UTF-8；这不影响 P1 程序逻辑，后续正式 Windows 交付阶段再处理终端编码适配。

## P2 入口

只有用户明确授权 P2 后，才读取以下最小集合：

1. `implementation_plan/implementation-roadmap.md` 的 P2 卡片；
2. 本文件 `implementation_plan/checkpoints/P1-handoff.md`；
3. `implementation_plan/checkpoints/P0-handoff.md`；
4. P2 卡片列出的共享契约文件；
5. `task_plan.md` 与 `progress.md` 的最新尾部状态。

P2 的唯一目标是跨模块契约、事件和状态模型；必须先写契约测试，再实现最小对象。不得跳到文字主链、Agent、电脑工具或音频模块。

## P2 禁止提前做的事项

- 不接 DeepSeek 或其他 Provider。
- 不写文字主链、工具注册表、执行动作或授权 UI。
- 不接麦克风、ASR、TTS、VoiceStudio、唤醒词、VAD、AEC。
- 不创建 Skill、Windows 常驻、打包或发布逻辑。
- 不把 P1 的遗留任务安全哨兵扩展成自动恢复、自动补做、自动重试或自动回滚。

## 停止声明

P1 已完成。当前停在 P2 授权门前；不得把本文件中的 P2 入口误解为用户授权，也不得顺手接入 Agent。
