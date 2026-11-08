# 阶段 1：项目骨架 + 配置 + 日志

> 文档拆分日期：2026-09-18  
> 状态：规划阶段；未经用户明确要求，不编写功能代码。  
> 本文件由原 `archive/implementation_plan.md` 归并拆分而来；迁移前全文保存在 `archive/implementation_plan.legacy.md`。

## 按需阅读的共享依赖

- [`shared/architecture.md`](./shared/architecture.md)
- [`shared/data-and-privacy.md`](./shared/data-and-privacy.md)

- 目标：搭出目录和可运行的空骨架。
- 步骤：
  - [ ] 建目录：`wake/ audio/ asr/ llm/ tools/ safety/ tts/ logs/ models/`
  - [ ] 写 `config.py`：唤醒词、灵敏度、VAD 静音时长、确认超时、命令超时、API key、允许访问根目录、默认工作目录、模型路径、日志路径；个人值必须作为 `[用户配置]`，不写死 `[本机配置示例]`
  - [ ] 写日志模块（logging，输出到 `logs/` + 控制台）
  - [ ] 写 `main.py` 空入口（能打印配置、能退出）
- 产出：目录结构、`config.py`、`main.py`、日志模块
- 验收：`python main.py` 能启动、打印日志、正常退出
